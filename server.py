#!/usr/bin/env python3
"""
pytest-board — a Jira-style drag & drop board for scheduling pytest runs.

Pure Python standard library (3.8+). No pip installs needed except pytest itself
in whatever environment runs your tests.

    python3 server.py --root /path/to/your/project --host 0.0.0.0 --port 8765 --token s3cret

Columns are run states (Backlog, Scheduled, Queued, Running, Passed, Failed);
swimlanes are the machines the tests run on (reached over ssh; the board server
itself doesn't run tests).
Drag a card to Queued to run it now, or to Scheduled to run it once at a time
or on a cron schedule.
"""
import argparse
import base64
import configparser
import getpass
import hmac
import json
import mimetypes
import os
import posixpath
import shutil
import re
import secrets
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
NO_MACHINE = ("this card has no machine to run on: drag it to a machine's row, or set machine = for its headline "
              "in pytest-board.ini")
LOCAL_GONE = "the board server doesn't run tests itself any more: pick one of the machines"
try:
    SERVICE_USER = getpass.getuser()
except Exception:  # no passwd entry / login name (unusual containers)
    SERVICE_USER = str(os.getuid()) if hasattr(os, "getuid") else ""
STATIC = HERE / "static"
# Bump together with PAGE_VERSION in static/index.html when the page needs this server (the page asks for a
# restart when the running server is older than the page).
BOARD_VERSION = "2026.10.26.4"


def page_stamp():
    """Changes whenever static/index.html changes on disk: an open page then reloads itself."""
    try:
        return str((STATIC / "index.html").stat().st_mtime_ns)
    except OSError:
        return ""

# --------------------------------------------------------------------------- platform (Linux / macOS / Windows)
IS_WIN = os.name == "nt"
# How child processes are started: in their own session/process group (so Stop can end pytest and everything it
# started), and on Windows without popping up console windows and out of reach of Ctrl+C in the board's console.
if IS_WIN:
    CHILD_KW = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    QUIET_KW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}  # short helper commands (ssh checks)
else:
    CHILD_KW = {"start_new_session": True}
    QUIET_KW = {}


def split_local(text):
    """Split a command line that runs on THIS computer. On Windows backslashes are path separators, not escapes:
    C:\\venv\\Scripts\\python.exe stays intact, and "quoted parts" may contain spaces."""
    text = text or ""
    if not IS_WIN:
        return shlex.split(text)
    lex = shlex.shlex(text, posix=False)
    lex.whitespace_split = True
    lex.commenters = ""
    out = []
    for tok in lex:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def kill_tree(p, grace=5):
    """End a child process and everything it started (pytest-xdist workers, ssh, rsync, …)."""
    if p.poll() is not None:
        return
    if IS_WIN:
        try:  # /T: the whole tree, /F: console programs can't be asked to close
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=30, **QUIET_KW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            p.kill()
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
COLUMNS = ["backlog", "scheduled", "queued", "running", "passed", "failed"]
SYSTEM_ONLY = {"running", "passed", "failed"}  # cannot be dropped into by hand


# --------------------------------------------------------------------------- cron
CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}
_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


def _cron_field(spec, lo, hi, name):
    spec = spec.lower()
    for k, v in _NAMES.items():
        spec = spec.replace(k, str(v))
    values = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(f"cron {name} field out of range: {spec!r}")
        values.update(range(a, b + 1, step))
    return values


class Cron:
    """Standard 5-field cron: minute hour day-of-month month day-of-week (server local time)."""

    def __init__(self, expr):
        expr = CRON_ALIASES.get(expr.strip().lower(), expr.strip())
        f = expr.split()
        if len(f) != 5:
            raise ValueError("cron needs 5 fields: minute hour day month weekday (or @hourly/@daily/@weekly)")
        try:
            self.minute = _cron_field(f[0], 0, 59, "minute")
            self.hour = _cron_field(f[1], 0, 23, "hour")
            self.dom = _cron_field(f[2], 1, 31, "day")
            self.month = _cron_field(f[3], 1, 12, "month")
            self.dow = {d % 7 for d in _cron_field(f[4], 0, 7, "weekday")}
        except ValueError as e:
            raise ValueError(str(e) if "cron" in str(e) else f"invalid cron expression: {expr!r}")
        # as in Vixie cron, a day field starting with * (also */2) counts as unrestricted for the day OR rule
        self.dom_any, self.dow_any = f[2].startswith("*"), f[4].startswith("*")

    def _day_ok(self, d):
        dom_ok = d.day in self.dom
        dow_ok = (d.isoweekday() % 7) in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # classic cron: either restriction matches

    def next_after(self, ts):
        d = datetime.fromtimestamp(ts).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = d + timedelta(days=366 * 5)
        while d < limit:
            if d.month not in self.month:
                d = (d.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_ok(d):
                d = d.replace(hour=0, minute=0) + timedelta(days=1)
            elif d.hour not in self.hour:
                d = d.replace(minute=0) + timedelta(hours=1)
            elif d.minute not in self.minute:
                d += timedelta(minutes=1)
            else:
                return d.timestamp()
        raise ValueError("cron expression never fires")


# --------------------------------------------------------------------------- option sets
def normalize_optset(v):
    """One option set: extra pytest arguments for one card, e.g. '-m slow --maxfail=3'. None/'' -> None."""
    if v is None:
        return None
    t = " ".join(str(v).split())  # one line, single spaces
    if not t:
        return None
    try:
        shlex.split(t)
    except ValueError as e:
        raise ValueError(f"option set {v!r}: {e}")
    return t


# a git branch name as `git check-ref-format --branch` would take it (a safe subset)
BRANCH_RX = re.compile(r"^(?![-/.])(?!.*\.\.)(?!.*//)(?!.*/\.)(?!.*@\{)(?!.*\.lock$)(?!.*[/.]$)[A-Za-z0-9._/+\-]{1,200}$")


def normalize_branch(v):
    """A card's / test's / headline's git branch. None/'' -> None (the checkout is used as it is)."""
    t = str(v or "").strip()
    if not t:
        return None
    if not BRANCH_RX.match(t):
        raise ValueError(f"branch {t!r}: not a git branch name (letters, digits and . _ - + /, e.g. feature/login)")
    return t


def parse_optsets(values):
    """A list of option sets (None or '' = a card without extra options), or text with one set per line ->
    normalized, in the given order, without repeats."""
    if isinstance(values, str):
        values = [ln for ln in values.splitlines() if ln.strip()]
    out = []
    for v in values or []:
        t = normalize_optset(v)
        if t not in out:
            out.append(t)
    return out


# --------------------------------------------------------------------------- store
SCHEMA = """
CREATE TABLE IF NOT EXISTS tests(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '',
  env TEXT NOT NULL DEFAULT '', timeout INTEGER NOT NULL DEFAULT 0, position REAL NOT NULL DEFAULT 0,
  created REAL, updated REAL, grp TEXT, machine TEXT, optsets TEXT, branch TEXT, extra_args TEXT);
CREATE TABLE IF NOT EXISTS seeded(grp TEXT NOT NULL, spec TEXT NOT NULL, PRIMARY KEY(grp, spec));
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL, user TEXT NOT NULL DEFAULT '',
  port INTEGER, identity TEXT NOT NULL DEFAULT '', workdir TEXT NOT NULL DEFAULT '',
  python TEXT NOT NULL DEFAULT 'python3 -m pytest', sync INTEGER NOT NULL DEFAULT 0,
  env TEXT NOT NULL DEFAULT '', ssh_options TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  position REAL NOT NULL DEFAULT 0, created REAL, updated REAL, env_file TEXT NOT NULL DEFAULT '',
  remote_env_file TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS lanes(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,
  collapsed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY,
  lane_id INTEGER NOT NULL REFERENCES lanes(id) ON DELETE CASCADE,
  title TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '',
  env TEXT NOT NULL DEFAULT '', timeout INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'backlog', position REAL NOT NULL DEFAULT 0,
  run_at REAL, cron TEXT, queued_at REAL, pending_trigger TEXT, last_run_id INTEGER,
  created REAL, updated REAL,
  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL, grp TEXT, machine TEXT, optset TEXT, branch TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY,
  card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL,
  card_title TEXT, lane_name TEXT,
  trigger TEXT, command TEXT, started REAL, finished REAL, exit_code INTEGER,
  outcome TEXT, total INTEGER, passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER, machine TEXT,
  optset TEXT, remote TEXT, branch TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""
RUN_COLS_V1 = ("id, card_id, trigger, command, started, finished, exit_code, outcome, "
               "total, passed, failed, errors, skipped")
INDEXES = """
CREATE INDEX IF NOT EXISTS runs_card ON runs(card_id, id);
CREATE INDEX IF NOT EXISTS runs_test ON runs(test_id, id);
CREATE INDEX IF NOT EXISTS cards_test ON cards(test_id);
CREATE INDEX IF NOT EXISTS tests_grp ON tests(grp, position);
"""


# --------------------------------------------------------------------------- .env files
# Shell-style files with one variable per line, as you'd `source` them:
#     export BASE_URL=https://staging.example.com
#     API_TOKEN='s3cr3t'          # export is optional; comments and blank lines are fine
#     DATA_DIR="${HOME}/data"     # $VAR / ${VAR}: earlier lines, then the board's own environment
# Read on the board server (values are sent to runs like env =, never shown in the UI).
_ENV_LINE_RX = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_ENV_NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NO_CMDS = "commands ($(…) or `…`) aren't run in .env files read on the board server"
_env_cache = {}
_env_cache_lock = threading.Lock()


def _env_expand(s, i, lookup):
    """$NAME or ${NAME} (or ${NAME:-default}) at s[i]. Returns (value, index after it)."""
    if s.startswith("${", i):
        j = s.find("}", i)
        if j < 0:
            raise ValueError("unclosed ${")
        name, sep, default = s[i + 2:j].partition(":-")
        if not _ENV_NAME_RX.fullmatch(name):
            raise ValueError(f"can't expand ${{{s[i + 2:j]}}}")
        v = lookup.get(name, "")
        return (v if v or not sep else default), j + 1
    m = _ENV_NAME_RX.match(s, i + 1)
    if not m:
        return "$", i + 1
    return lookup.get(m.group(0), ""), m.end()


def _env_value(s, lookup, i=0):
    """The value of NAME=value starting at s[i], with sh quoting rules: '…' literal, "…" and bare text expand
    $VAR. Returns (value, index where it ended: unquoted whitespace or the end)."""
    out = []
    while i < len(s):
        c = s[i]
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ValueError("unclosed '")
            out.append(s[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= len(s):
                    raise ValueError('unclosed "')
                c = s[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < len(s) and s[i + 1] in '"\\$`':
                    out.append(s[i + 1])
                    i += 2
                elif c == "$":
                    if s.startswith("$(", i):
                        raise ValueError(_NO_CMDS)
                    v, i = _env_expand(s, i, lookup)
                    out.append(v)
                else:
                    out.append(c)
                    i += 1
        elif c == "`" or s.startswith("$(", i):
            raise ValueError(_NO_CMDS)
        elif c == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
        elif c == "$":
            v, i = _env_expand(s, i, lookup)
            out.append(v)
        elif c in " \t":
            break
        else:
            out.append(c)
            i += 1
    return "".join(out), i


def parse_env_file(text, base=None):
    """Returns (env, problems). base: values $VAR can refer to besides the file's earlier lines."""
    env, problems = {}, []
    lookup = {k: v for k, v in (base or {}).items() if k != "PYTEST_BOARD_TOKEN"}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RX.match(line)
        if not m:
            if not re.fullmatch(r"export(\s+[A-Za-z_][A-Za-z0-9_]*)+", line):  # `export NAME` alone: nothing to set
                problems.append(f"line {n}: expected NAME=value")
            continue
        try:
            got, name, rest = {}, m.group(1), m.group(2)
            while True:  # several assignments on one line, as sh allows: export A=1 B=2
                v, i = _env_value(rest, lookup)
                got[name] = lookup[name] = v
                rest = rest[i:].lstrip()
                if not rest or rest.startswith("#"):
                    break
                nxt = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", rest)
                if not nxt:
                    raise ValueError("space in an unquoted value: put the value in quotes")
                name, rest = nxt.group(1), rest[nxt.end():]
        except ValueError as e:
            problems.append(f"line {n}: {e}")
            continue
        if got.pop("PYTEST_BOARD_TOKEN", None) is not None:
            problems.append(f"line {n}: PYTEST_BOARD_TOKEN is ignored (tests never get the board's password)")
        env.update(got)
    return env, problems


def read_env_file(path):
    """(env, error or None, exists) for a .env file on this server; cached until the file changes."""
    if not path:
        return {}, None, False
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return {}, None, False
    if not path.is_file():  # e.g. a virtualenv folder called .env
        return {}, None, False
    key = (st.st_mtime_ns, st.st_size)
    with _env_cache_lock:
        hit = _env_cache.get(str(path))
        if hit and hit[0] == key:
            return hit[1], hit[2], True
    try:
        env, problems = parse_env_file(path.read_text(encoding="utf-8-sig"), os.environ)
        err = f"{path.name}: " + "; ".join(problems) if problems else None
    except (OSError, UnicodeDecodeError) as e:
        env, err = {}, f"{path.name}: {e}"
    with _env_cache_lock:
        _env_cache[str(path)] = (key, env, err)
    return env, err, True


def resolve_env_file(value, base_dir):
    """A path from pytest-board.ini or the UI: ~ expanded, relative to the config file's folder."""
    value = str(value or "").strip()
    if not value:
        return None
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else Path(base_dir) / p


def env_file_info(path):
    """What the UI may see about a .env file: where it is, and the names in it (never the values)."""
    if not path:
        return None
    env, err, exists = read_env_file(path)
    return {"path": str(path), "exists": exists, "keys": sorted(env), "error": err}


# --------------------------------------------------------------------------- headlines (config file)
GROUP_COLORS = ["#3b5bdb", "#0f9d8a", "#c2410c", "#9333ea", "#be185d", "#0369a1", "#4d7c0f", "#a16207"]
GROUP_KEY_RX = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")


class Groups:
    """
    Library headlines, read from an INI file. Every [SECTION] is one headline:

        [TEST_SYSTEM]
        title = System under test       ; optional subtitle
        color = #3b5bdb                 ; optional
        dir = tests                     ; optional: pytest runs in <root>/<dir> for this headline
        branch = main                   ; optional: git branch checked out before this headline's tests
        args = -ra                      ; optional: pytest args added before each test's own args
        env =                           ; optional: KEY=value lines, a test's own env overrides them
            BASE_URL=https://system.staging
        env_file = secrets/system.env   ; optional: a .env file on this server (relative to this file)
        tests =                         ; optional: seeds the library (target, or name | target | args)
            test_api.py
            Health check | test_api.py::test_health | -v

    The file is re-read automatically when it changes.
    """

    def __init__(self, path, root):
        self.path = Path(path) if path else None
        self.root = root
        self.mtime = None
        self.items = []
        self.by_key = {}
        self.machines = []  # from [machine:NAME] sections
        self.error = None
        self.lock = threading.Lock()

    def load(self):
        """(Re)load if the file changed. Returns True when the headlines changed."""
        if not self.path:
            return False
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return False
        self.mtime = mtime
        if mtime is None:
            with self.lock:
                self.items, self.by_key, self.machines, self.error = [], {}, [], None
            return True
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",), strict=False)
        try:
            cp.read(str(self.path), encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError) as e:
            with self.lock:
                self.error = f"{self.path.name}: {e}".replace("\n", " ")
            print("headline config error:", self.error, file=sys.stderr)
            return True  # keep the last good headlines, but show the error
        items, problems, machines = [], [], []
        for key in cp.sections():
            s = cp[key]
            if key.lower().startswith("machine:"):  # [machine:NAME] -> an SSH execution host
                if "slots" in s:
                    print(f"[{key}] slots is ignored: a machine runs one test at a time", file=sys.stderr)
                try:
                    machines.append(normalize_machine(dict(s), key.split(":", 1)[1].strip(), "config",
                                                      base_dir=self.base_dir()))
                except ValueError as e:
                    problems.append(f"[{key}]: {e}")
                continue
            if not GROUP_KEY_RX.match(key):
                problems.append(f"[{key}]: use letters, digits, _ . - or spaces")
                continue
            g = {
                "key": key,
                "title": s.get("title", "").strip(),
                "color": s.get("color", "").strip() or GROUP_COLORS[len(items) % len(GROUP_COLORS)],
                "dir": s.get("dir", "").strip(),
                "machine": s.get("machine", "").strip() or None,
                "machines": [],  # machines = a, b: the only machines this headline's tests run on (first = default)
                "args": s.get("args", "").strip(),
                "env": Runner.parse_env(s.get("env", "")),
                "env_file": resolve_env_file(s.get("env_file", ""), self.base_dir()),
                "branch": None,
                "tests": [ln.strip() for ln in s.get("tests", "").splitlines()
                          if ln.strip() and not ln.strip().startswith("#")],
                "options": [],  # option sets offered when a test is put on the board: label | pytest args
            }
            for ln in s.get("options", "").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                label, sep, args = ln.partition("|")
                if not sep:
                    label, args = "", label
                try:
                    args = normalize_optset(args)
                except ValueError as e:
                    problems.append(f"[{key}] options: {e}")
                    continue
                if args and all(o["args"] != args for o in g["options"]):
                    g["options"].append({"label": label.strip() or args, "args": args})
            for name in re.split(r"[\s,]+", s.get("machines", "").strip()):
                if not name or name in g["machines"]:
                    continue
                if name == "local":
                    problems.append(f"[{key}] machines: 'local' is gone (the board server doesn't run tests itself)")
                elif not MACHINE_NAME_RX.match(name):
                    problems.append(f"[{key}] machines: invalid machine name {name!r}")
                else:
                    g["machines"].append(name)
            if g["machines"]:
                if g["machine"] and g["machine"] not in g["machines"]:
                    problems.append(f"[{key}] machine = {g['machine']} isn't one of machines = "
                                    f"{', '.join(g['machines'])}; the default is {g['machines'][0]}")
                    g["machine"] = None
                g["machine"] = g["machine"] or g["machines"][0]
            try:
                shlex.split(g["args"])
            except ValueError as e:
                problems.append(f"[{key}] args: {e}")
                g["args"] = ""
            try:
                g["branch"] = normalize_branch(s.get("branch", ""))
            except ValueError as e:
                problems.append(f"[{key}] {e}")
            items.append(g)
        with self.lock:
            self.items = items
            self.by_key = {g["key"]: g for g in items}
            self.machines = machines
            self.error = "; ".join(problems) or None
        print(f"loaded {len(items)} headline(s), {len(machines)} machine(s) from {self.path}"
              + (f" — {self.error}" if self.error else ""), file=sys.stderr)
        return True

    def get(self, key):
        return self.by_key.get(key) if key else None

    def base_dir(self):
        """Relative paths in the config file (env_file = …) start here."""
        return self.path.parent if self.path else self.root

    def public(self):
        """What the UI may see: env values are left out (they may hold secrets), only their names."""
        with self.lock:
            return [{"key": g["key"], "title": g["title"], "color": g["color"], "dir": g["dir"], "args": g["args"],
                     "machine": g["machine"], "machines": g["machines"], "env_keys": sorted(g["env"]),
                     "seed_count": len(g["tests"]),
                     "options": g["options"], "env_file": env_file_info(g["env_file"]), "branch": g["branch"]}
                    for g in self.items]


# --------------------------------------------------------------------------- machines (SSH execution hosts)
MACHINE_NAME_RX = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
ENV_KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_WORKDIR = "~/git"  # project folder on a machine when none is given ("~" = the home folder)
MACHINE_FIELDS = ("host", "user", "port", "identity", "workdir", "python", "sync", "env", "ssh_options",
                  "notes", "env_file", "remote_env_file")
NO_ENV_FILE = ("none", "off", "no", "-")  # remote_env_file values that turn the machine's own .env off


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def normalize_machine(d, name, source, mid=None, base_dir="."):
    """Validate a machine definition (from the config file or the UI). Raises ValueError."""
    name = str(name or "").strip()
    if not MACHINE_NAME_RX.match(name) or name.lower() == "local":
        raise ValueError("machine name: 1-64 letters, digits, _ . - (and not 'local')")
    host = str(d.get("host") or "").strip()
    if not host or host.startswith("-") or any(c.isspace() or c == "@" for c in host):
        raise ValueError("host is required (a hostname or IP, or an alias from ~/.ssh/config)")
    user = str(d.get("user") or "").strip()
    if user and (user.startswith("-") or any(c.isspace() or c == "@" for c in user)):
        raise ValueError("invalid user name")
    port = str(d.get("port") or "").strip()
    port = int(port) if port else None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535")
    python = str(d.get("python") or "").strip() or "python3 -m pytest"
    ssh_options = str(d.get("ssh_options") or "").strip()
    for label, val in (("python", python), ("ssh_options", ssh_options)):
        try:
            shlex.split(val)
        except ValueError as e:
            raise ValueError(f"{label}: {e}")
    workdir = str(d.get("workdir") or "").strip() or DEFAULT_WORKDIR
    sync = _truthy(d.get("sync"))
    env_text = str(d.get("env") or "")
    env = Runner.parse_env(env_text)
    bad = [k for k in env if not ENV_KEY_RX.match(k)]
    if bad:
        raise ValueError(f"invalid env name(s): {', '.join(bad)}")
    env_file = str(d.get("env_file") or "").strip()  # a .env file on this server
    remote_env_file = str(d.get("remote_env_file") or "").strip()  # one on the machine ('' = .env in workdir)
    if any(c in remote_env_file for c in "\n\r\0'\"$`\\"):
        raise ValueError("remote_env_file: a plain path, e.g. .env or ~/secrets/lab.env")
    return {"name": name, "host": host, "user": user, "port": port, "identity": str(d.get("identity") or "").strip(),
            "workdir": workdir, "python": python, "sync": sync, "env": env, "env_text": env_text,
            "ssh_options": ssh_options, "notes": str(d.get("notes") or "").strip(), "source": source, "id": mid,
            "env_file": env_file, "env_file_path": resolve_env_file(env_file, base_dir),
            "remote_env_file": remote_env_file}


ENV_HIDDEN = "…"  # the UI shows KEY=… for env values it may not see (machines from the config file)


def fill_hidden_env(text, source_env):
    """Env copied in the UI from a config-file machine: KEY=… lines get that machine's real value here."""
    out = []
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and v.strip() == ENV_HIDDEN and k.strip() in source_env:
            line = f"{k.strip()}={source_env[k.strip()]}"
        out.append(line)
    return "\n".join(out)


class Machines:
    """Config-file machines ([machine:NAME]) plus machines added in the UI (stored in the DB)."""

    def __init__(self, store, groups):
        self.s = store
        self.groups = groups
        self.status = {}  # name -> {state, checked, detail, system}

    def all(self):
        cfg = list(self.groups.machines)
        names = {m["name"] for m in cfg}
        ui = []
        for r in self.s.q("SELECT * FROM machines ORDER BY position, id"):
            if r["name"] in names:
                continue  # the config file wins on a name clash
            try:
                ui.append(normalize_machine(r, r["name"], "ui", r["id"], base_dir=self.groups.base_dir()))
            except ValueError as e:
                print(f"machine {r['name']}: {e}", file=sys.stderr)
        return cfg + ui

    def get(self, name):
        if not name or name == "local":
            return None
        return next((m for m in self.all() if m["name"] == name), None)

    def set_status(self, name, **kw):
        self.status[name] = dict(self.status.get(name, {}), **kw)
        self.s.version += 1


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.executescript(INDEXES)
        self.lock = threading.RLock()
        self.version = int(time.time())  # bumps on every write; the UI polls it
        # names this board's run folders and screen sessions on the machines, so boards sharing a machine
        # (or a fresh board.db) never mix up each other's runs
        row = self.db.execute("SELECT v FROM meta WHERE k='board_id'").fetchone()
        if row:
            self.board_id = row[0]
        else:
            self.board_id = secrets.token_hex(3)
            self.db.execute("INSERT INTO meta(k, v) VALUES('board_id', ?)", (self.board_id,))

    def _cols(self, table):
        return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self):
        """Upgrade a board.db created by an earlier version."""
        for table in ("tests", "cards"):  # v3: headlines
            if "grp" not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN grp TEXT")
        if "test_id" not in self._cols("cards"):
            self.db.execute("ALTER TABLE cards ADD COLUMN test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL")
        if "test_id" not in self._cols("runs"):
            # runs used to be deleted with their card; rebuild so history outlives cards
            self.db.execute("PRAGMA foreign_keys=OFF")
            self.db.executescript(f"""
                BEGIN;
                ALTER TABLE runs RENAME TO runs_v1;
                CREATE TABLE runs(
                  id INTEGER PRIMARY KEY,
                  card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL,
                  card_title TEXT, lane_name TEXT,
                  trigger TEXT, command TEXT, started REAL, finished REAL, exit_code INTEGER,
                  outcome TEXT, total INTEGER, passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER);
                INSERT INTO runs({RUN_COLS_V1}) SELECT {RUN_COLS_V1} FROM runs_v1;
                UPDATE runs SET card_title=(SELECT title FROM cards WHERE cards.id=runs.card_id),
                                lane_name=(SELECT l.name FROM cards c JOIN lanes l ON l.id=c.lane_id
                                           WHERE c.id=runs.card_id);
                DROP TABLE runs_v1;
                COMMIT;""")
            self.db.execute("PRAGMA foreign_keys=ON")
            print("migrated board.db: added test library", file=sys.stderr)
        for table in ("tests", "cards", "runs"):  # v4: machines (after the runs rebuild above)
            if "machine" not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN machine TEXT")
        for table, col in (("cards", "optset"), ("runs", "optset"), ("tests", "optsets"),  # v6: option sets
                           ("runs", "remote")):  # v7: runs that live on a machine (screen / background)
            if col not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        for table in ("tests", "cards", "runs"):  # v9: git branches
            if "branch" not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN branch TEXT")
        if "extra_args" not in self._cols("tests"):  # v10: extra arguments last used for a test's copies
            self.db.execute("ALTER TABLE tests ADD COLUMN extra_args TEXT")
        # v11: a git branch is set on cards (or a headline), not on library tests any more
        self.db.execute("UPDATE tests SET branch=NULL WHERE branch IS NOT NULL")
        # v12: the board server doesn't run tests itself any more: 'local' (this server) becomes "no machine"
        for table in ("tests", "cards"):
            self.db.execute(f"UPDATE {table} SET machine=NULL WHERE machine='local'")
        # v13: library tests have no machine of their own: a copy runs on the row it is dropped into, else on its
        # headline's machine (cards keep theirs)
        self.db.execute("UPDATE tests SET machine=NULL WHERE machine IS NOT NULL")
        # v14: a machine runs one test at a time, so machines lose their "how many at once" column
        if "slots" in self._cols("machines"):
            try:
                self.db.execute("ALTER TABLE machines DROP COLUMN slots")
            except sqlite3.OperationalError:
                pass  # SQLite before 3.35 can't drop a column; it just stays unused
        for col in ("env_file", "remote_env_file"):  # v8: .env files
            if col not in self._cols("machines"):
                self.db.execute(f"ALTER TABLE machines ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # v5 had temperatures: a card at -20 becomes a card with the option set --temperature=-20
        for table in ("cards", "runs"):
            if "temp" in self._cols(table):
                n = self.db.execute(f"UPDATE {table} SET optset='--temperature=' || temp "
                                    "WHERE temp IS NOT NULL AND optset IS NULL").rowcount
                if table == "cards":
                    self.db.execute("UPDATE cards SET temp=NULL WHERE temp IS NOT NULL")
                    if n:
                        print(f"migrated board.db: {n} temperature card(s) now use --temperature=... option sets",
                              file=sys.stderr)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.version += 1
            return cur.lastrowid


# --------------------------------------------------------------------------- runner
class _Detached(Exception):
    """The board is shutting down while a run goes on on its machine: leave it 'running' to follow it again."""


class Runner:
    def __init__(self, store, root, data_dir, groups=None, machines=None,
                 ssh_cmd=("ssh",), rsync_cmd=("rsync",), check_interval=120, env_file=None):
        self.store = store
        self.root = root
        self.groups = groups or Groups(None, root)
        self.machines = machines or Machines(store, self.groups)
        self.ssh_cmd = list(ssh_cmd)
        self.rsync_cmd = list(rsync_cmd)
        self.check_interval = check_interval
        self.env_file = Path(env_file) if env_file else None  # .env on this server for every run (--env-file)
        self.logs = data_dir / "logs"
        self.junit = data_dir / "junit"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.junit.mkdir(parents=True, exist_ok=True)
        self.procs = {}   # card_id -> Popen of its run's current step on the machine (ssh connection, sync)
        self.busy = {}    # machine name -> runs in progress
        self.active = set()  # card ids from dispatch until the run is recorded (covers sync / ssh start-up)
        self.cancelled = set()
        self.checking = set()
        self.checking_lock = threading.Lock()
        self.wake = threading.Event()
        # runs on machines go on there by themselves (screen session or background process); the board follows
        # their log over ssh and reconnects whenever the connection drops
        self.following = {}   # card id -> machine name, while the board follows a run there
        self.killing = set()  # card ids whose current connection is the one stopping the run on the machine
        self.kill_req = set() # card ids whose connection was ended on purpose (Stop, timeout)
        self.links = {}       # card id -> {"state": "reconnecting", "since": t, "detail": ...} while cut off
        self.nudge = {}       # card id -> Event that wakes a waiting reconnect (Stop, machine back online)
        self.swept = set()    # machines whose leftover run folders were tidied since the board started
        self.run_branch = {}  # card id -> (machine, git branch or None) of every run in progress
        self.switching = {}   # machine -> card id of the run switching its project folder to a branch right now
        self.co_skip = set()  # card ids whose branch is checked out already (by a run in progress on it)
        self.stopping = False # the board is shutting down: runs on machines carry on, they are picked up later

    # --- lifecycle
    def start(self):
        s = self.store
        now = time.time()
        # Runs on machines kept going while the board was stopped: follow them again. Anything else that was
        # "running" (not started on its machine yet, or from a version that ran tests on this server) was interrupted.
        resume = []
        for r in s.q("SELECT * FROM runs WHERE outcome='running'"):
            info = self._remote_info(r)
            card = s.one("SELECT * FROM cards WHERE id=?", (r["card_id"],)) if r["card_id"] else None
            m = self.machines.get(r["machine"])
            if info and info.get("base") is not None and m and card and card["status"] == "running" \
                    and card["last_run_id"] == r["id"]:
                resume.append((card, r, info))
            else:
                s.x("UPDATE runs SET outcome='interrupted', finished=? WHERE id=?", (now, r["id"]))
        keep = [c["id"] for c, _, _ in resume]
        s.x(f"UPDATE cards SET status='failed' WHERE status='running' AND id NOT IN ({','.join('?' * len(keep))})",
            keep)
        for card, r, info in resume:
            self.busy[r["machine"]] = self.busy.get(r["machine"], 0) + 1
            self.active.add(card["id"])
            self.run_branch[card["id"]] = (r["machine"], r.get("branch"))
            threading.Thread(target=self._job, args=(card, r["id"], r["machine"], (r, info)), daemon=True).start()
        if resume:
            print(f"following {len(resume)} run(s) that went on on their machines while the board was stopped",
                  file=sys.stderr)
        threading.Thread(target=self._scheduler, name="scheduler", daemon=True).start()
        threading.Thread(target=self._dispatch, name="dispatcher", daemon=True).start()
        threading.Thread(target=self._health, name="machine-health", daemon=True).start()

    def _scheduler(self):
        while True:
            try:
                now = time.time()
                with self.store.lock:
                    due = self.store.q(
                        "SELECT id, cron FROM cards WHERE status='scheduled' AND run_at IS NOT NULL AND run_at<=?",
                        (now,))
                    for c in due:
                        self.store.x(
                            "UPDATE cards SET status='queued', queued_at=?, pending_trigger=?, updated=? WHERE id=?",
                            (now, "cron" if c["cron"] else "schedule", now, c["id"]))
                if due:
                    self.wake.set()
            except Exception as e:  # never let the scheduler die
                print("scheduler error:", e, file=sys.stderr)
            time.sleep(2)

    # --- which machine runs a card: the card's own choice > its headline's machine. None: no machine yet (the card
    # waits in the "No machine" row until it gets one; the board server doesn't run tests itself)
    def resolve_machine(self, item):
        if item.get("machine") and item["machine"] != "local":
            return item["machine"], "card"
        g = self.groups.get(item.get("grp"))
        if g and g.get("machine") and g["machine"] != "local":
            return g["machine"], "headline"
        return None, "none"

    def not_bound(self, item, mname=None):
        """Why `item` can't run on machine `mname` (default: the machine it would run on), or None. A headline with
        machines = a, b in pytest-board.ini runs its tests only on those machines."""
        g = self.groups.get(item.get("grp"))
        bound = g.get("machines") if g else None
        if not bound:
            return None
        if mname is None:
            mname = self.resolve_machine(item)[0]
        if not mname or mname in bound:
            return None
        return (f"{g['key']} tests run only on {', '.join(bound)} (machines = in pytest-board.ini), "
                f"not on {mname}")

    # --- which git branch a card runs on: its own > its headline's > none (the checkout is used as it is)
    def resolve_branch(self, item):
        if item.get("branch"):
            return item["branch"], "card"
        g = self.groups.get(item.get("grp"))
        if g and g.get("branch"):
            return g["branch"], "headline"
        return None, "default"

    def branch_conflict(self, mname, branch):
        """Why a card that wants `branch` (None: the folder as it is) can't start on this machine now, or None.
        Runs switch the machine's project folder itself, so the runs going on there at the same time all use the
        same branch, and nothing starts while a run is still switching the folder."""
        cid = self.switching.get(mname)
        if cid is not None:
            return self.run_branch.get(cid, (None, None))[1] or "another branch"
        for m, b in self.run_branch.values():
            if m == mname and b != branch:
                return b or "(the folder as it is)"
        return None

    def _switch_done(self, cid):
        """A run finished checking out its branch: other runs on that branch may start next to it."""
        with self.store.lock:
            for m, c in list(self.switching.items()):
                if c == cid:
                    del self.switching[m]
                    self.store.version += 1
        self.wake.set()

    def capacity(self, name):
        """A machine runs one test at a time (an unknown machine, or none: one too, so it fails with a clear message)."""
        return 1

    def _dispatch(self):
        while True:
            try:
                started = self._try_start()
            except Exception as e:
                print("dispatcher error:", e, file=sys.stderr)
                started = False
            if not started:
                self.wake.wait(1.0)
                self.wake.clear()

    def available(self, name):
        """False while a machine is known to be unreachable: its queued cards wait instead of failing."""
        if not name:
            return True  # no machine: let it through so it fails with a clear message
        st = self.machines.status.get(name)
        return not st or st.get("state") != "offline"

    def _try_start(self):
        """Start the oldest queued card whose machine is reachable and isn't running a test."""
        s = self.store
        with s.lock:
            full = set()
            for c in s.q("SELECT * FROM cards WHERE status='queued' ORDER BY queued_at, position, id"):
                mname, _ = self.resolve_machine(c)
                if mname in full:
                    continue
                branch = self.resolve_branch(c)[0]
                if self.busy.get(mname, 0) >= self.capacity(mname) or not self.available(mname) \
                        or self.branch_conflict(mname, branch):
                    full.add(mname)  # first in the queue first: later cards for this machine wait too
                    continue
                now = time.time()
                run_id = s.x("INSERT INTO runs(card_id, test_id, card_title, lane_name, trigger, started, outcome, "
                             "machine, optset, branch) VALUES(?,?,?,?,?,?, 'running', ?, ?, ?)",
                             (c["id"], c["test_id"], c["title"], mname, c["pending_trigger"] or "manual",
                              now, mname, c.get("optset"), branch))
                if branch and any(m == mname and b == branch for m, b in self.run_branch.values()):
                    self.co_skip.add(c["id"])  # a run in progress just checked that branch out: use it as it is
                elif branch:
                    self.switching[mname] = c["id"]
                self.run_branch[c["id"]] = (mname, branch)
                s.x("UPDATE cards SET status='running', last_run_id=?, updated=? WHERE id=?", (run_id, now, c["id"]))
                self.busy[mname] = self.busy.get(mname, 0) + 1
                self.active.add(c["id"])
                threading.Thread(target=self._job, args=(c, run_id, mname), daemon=True).start()
                return True
        return False

    def _job(self, card, run_id, mname, resume=None):
        try:
            self._execute(card, run_id, mname, resume)
        except _Detached:
            pass  # the board is stopping; the run goes on on its machine and is followed again after the restart
        except Exception as e:
            print("run error:", e, file=sys.stderr)
            self.store.x("UPDATE runs SET outcome='error', finished=? WHERE id=?", (time.time(), run_id))
            self.store.x("UPDATE cards SET status='failed' WHERE id=? AND status='running'", (card["id"],))
        finally:
            with self.store.lock:
                self.active.discard(card["id"])
                self.cancelled.discard(card["id"])
                self.run_branch.pop(card["id"], None)
                self.co_skip.discard(card["id"])
                for m, c in list(self.switching.items()):
                    if c == card["id"]:
                        del self.switching[m]
                self.busy[mname] = max(0, self.busy.get(mname, 1) - 1)
                self.store.version += 1
            self.wake.set()

    # --- execution
    @staticmethod
    def remote_env_path(m):
        """The machine's own .env (a path on the machine, relative to its workdir), or None when turned off."""
        v = m.get("remote_env_file") or ".env"
        return None if v.lower() in NO_ENV_FILE else v

    def env_file_notes(self, g, m=None):
        """Log lines: which .env files on this server a run uses (names only), and problems with them."""
        out = []
        for path, what in ((self.env_file, None), (m and m.get("env_file_path"), m and f"machine {m['name']}"),
                           (g and g.get("env_file"), g and f"headline {g['key']}")):
            if not path:
                continue
            env, err, exists = read_env_file(path)
            if exists:
                out.append(f"[pytest-board] env file {path}: {', '.join(sorted(env)) or 'no variables'}")
            elif what:  # named in the config, but not there (the board-wide .env is optional)
                out.append(f"[pytest-board] WARNING: env file {path} (env_file of {what}) not found")
            if err:
                out.append(f"[pytest-board] WARNING: {err}")
        return "".join(ln + "\n" for ln in out)

    @staticmethod
    def parse_env(text):
        env = {}
        for line in (text or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        return env

    # exit codes the remote wrapper uses on top of pytest's own (0-5)
    RC_NO_MACHINE, RC_SYNC_FAILED, RC_NO_WORKDIR, RC_SSH = -2, 96, 97, 255
    RC_WAIT = -3   # the machine could not be reached before anything started: the card goes back to Queued
    RC_LOST = -4   # the run ended on its machine without an exit code (machine restarted, killed from outside)
    RC_CHECKOUT = 95  # the card's git branch couldn't be checked out; pytest didn't start
    RC_NOT_BOUND = -5  # the card's machine isn't one its headline is bound to (machines =); nothing started

    def _execute(self, card, run_id, mname, resume=None):
        """Run a card (or, with resume=(run row, remote info), follow a run that went on while the board was
        stopped) and record the result."""
        s = self.store
        cid = card["id"]
        log_path = self.logs / f"run-{run_id}.log"
        junit_path = self.junit / f"run-{run_id}.xml"
        # the timeout covers the whole run, including a project sync before it
        started = resume[0]["started"] if resume else time.time()
        deadline = started + card["timeout"] if card["timeout"] else None
        with open(log_path, "ab" if resume else "wb") as log:
            m = self.machines.get(mname)
            if resume:
                rc, timed_out = self._follow(card, run_id, m, resume[1], log, junit_path, deadline, resumed=True)
            else:
                if card.get("grp"):
                    log.write(f"[pytest-board] headline {card['grp']}\n".encode())
                if card.get("optset"):
                    log.write(f"[pytest-board] option set: {card['optset']}\n".encode())
                if self.resolve_branch(card)[0]:
                    log.write(f"[pytest-board] git branch: {self.resolve_branch(card)[0]}\n".encode())
                if cid in self.cancelled:
                    rc, timed_out = -1, False  # stopped before it got going
                elif m and self.not_bound(card, mname):
                    log.write(f"[pytest-board] {self.not_bound(card, mname)}. Drag the card to one of those "
                              "machines' rows.\n".encode())
                    rc, timed_out = self.RC_NOT_BOUND, False
                elif m:
                    rc, timed_out = self._run_remote(card, run_id, m, log, junit_path, deadline)
                elif not mname:
                    log.write(f"[pytest-board] {NO_MACHINE}\n".encode())
                    rc, timed_out = self.RC_NO_MACHINE, False
                else:
                    log.write(f"[pytest-board] machine '{mname}' is not defined any more "
                              "(removed from the config file or the UI). Pick another machine for this card.\n"
                              .encode())
                    rc, timed_out = self.RC_NO_MACHINE, False
            finished = time.time()
            counts = self._parse_junit(junit_path)
            cancelled = cid in self.cancelled
            self.cancelled.discard(cid)
            requeue = False
            if cancelled:
                outcome = "cancelled"
            elif timed_out:
                outcome = "timeout"
            elif rc == 0:
                outcome = "passed"
            elif rc == 5:
                outcome = "no-tests"
            elif rc == self.RC_NO_MACHINE:
                outcome = "no-machine"
            elif rc == self.RC_NOT_BOUND:
                outcome = "not-bound"
            elif rc == self.RC_WAIT:
                outcome, requeue = "unreachable", True
            elif rc == self.RC_LOST:
                outcome = "lost"
            elif rc == self.RC_START:
                outcome = "start-failed"
            elif rc == self.RC_CHECKOUT:
                outcome = "checkout-failed"
            elif rc == self.RC_SSH:
                outcome = "unreachable"
            elif rc == self.RC_NO_WORKDIR:
                outcome = "no-workdir"
            elif rc == self.RC_SYNC_FAILED:
                outcome = "sync-failed"
            elif rc == 127:
                outcome = "not-found"
            elif rc == 4:
                outcome = "usage-error"  # pytest rejected the command line
                log.write(b"[pytest-board] pytest didn't accept the command line (exit code 4). Check the card's args "
                          b"and option set: options your own tests add need a pytest_addoption() in conftest.py "
                          b"(see README, Option sets).\n")
            else:
                outcome = "failed"
            if requeue:
                log.write(f"\n[pytest-board] not started: {mname} can't be reached. The card waits in Queued and "
                          f"starts as soon as {mname} answers again.\n".encode())
            elif rc == self.RC_LOST and not (cancelled or timed_out):
                log.write(f"\n[pytest-board] no exit code · run lost · on {mname}\n".encode())
            else:
                log.write(f"\n[pytest-board] exit code {rc} · {outcome}"
                          f"{' · on ' + mname if mname else ''}\n".encode())

        s.x("""UPDATE runs SET finished=?, exit_code=?, outcome=?, total=?, passed=?, failed=?, errors=?, skipped=?
               WHERE id=?""",
            (finished, None if rc in (self.RC_WAIT, self.RC_LOST) else rc, outcome, counts.get("total"),
             counts.get("passed"), counts.get("failed"), counts.get("errors"), counts.get("skipped"), run_id))

        with s.lock:
            now_card = s.one("SELECT * FROM cards WHERE id=?", (cid,))
            if not now_card:
                return  # deleted while running
            if cancelled:
                s.x("UPDATE cards SET status='backlog', run_at=NULL, updated=? WHERE id=?", (finished, cid))
            elif requeue:  # keeps its place in the queue (queued_at), and its schedule
                s.x("UPDATE cards SET status='queued', updated=? WHERE id=?", (finished, cid))
            elif now_card["cron"]:
                try:
                    nxt = Cron(now_card["cron"]).next_after(finished)
                    s.x("UPDATE cards SET status='scheduled', run_at=?, updated=? WHERE id=?", (nxt, finished, cid))
                except ValueError:
                    s.x("UPDATE cards SET status=?, cron=NULL, run_at=NULL WHERE id=?",
                        ("passed" if outcome == "passed" else "failed", cid))
            else:
                s.x("UPDATE cards SET status=?, run_at=NULL, updated=? WHERE id=?",
                    ("passed" if outcome == "passed" else "failed", finished, cid))

    def _wait(self, card, p, deadline=None):
        """Wait for a process of a run until the run's deadline. Returns (exit code, timed_out)."""
        self.procs[card["id"]] = p
        if card["id"] in self.cancelled:  # Stop arrived while the process was starting
            self._terminate(card["id"], p)
        try:
            return p.wait(timeout=None if deadline is None else max(0.0, deadline - time.time())), False
        except subprocess.TimeoutExpired:
            self._terminate(card["id"], p)
            return p.wait(), True
        finally:
            self.procs.pop(card["id"], None)

    # --- SSH
    def ssh_base(self, m, interactive=False, keepalive=(30, 4)):
        """
        ssh client arguments for a machine. interactive=True is for a person at a terminal (Open shell):
        a remote tty, and ssh may ask for a password or passphrase there instead of failing.
        keepalive: (seconds, count) after which a silent connection counts as dropped.
        """
        # user options go first: for ssh the first value given for an option wins
        cmd = list(self.ssh_cmd) + shlex.split(m["ssh_options"] or "")
        cmd += ["-tt"] if interactive else ["-T", "-o", "BatchMode=yes"]
        cmd += ["-o", "ConnectTimeout=10", "-o", f"ServerAliveInterval={keepalive[0]}",
                "-o", f"ServerAliveCountMax={keepalive[1]}", "-o", "StrictHostKeyChecking=accept-new"]
        if m["port"]:
            cmd += ["-p", str(m["port"])]
        if m["identity"]:
            cmd += ["-i", os.path.expanduser(m["identity"])]
        return cmd

    @staticmethod
    def dest(m):
        return f"{m['user']}@{m['host']}" if m["user"] else m["host"]

    @staticmethod
    def _rpath(path):
        """A remote path for sh, with a leading ~ expanded by the remote shell."""
        if path in ("", "~"):
            return '"$HOME"'
        if path.startswith("~/"):
            return '"$HOME"/' + shlex.quote(path[2:])
        return shlex.quote(path)

    @staticmethod
    def _rword(word):
        """One word of a remote command for sh. A leading ~ or ~/ is expanded by the remote shell, as when
        you type it there (e.g. python = ~/git/venv/bin/pytest); everything else is quoted."""
        if word == "~":
            return '"$HOME"'
        if word.startswith("~/"):
            return '"$HOME"/' + shlex.quote(word[2:])
        return shlex.quote(word)

    def remote_dir(self, m, g):
        wd = m["workdir"] or "~"
        if g and g.get("dir"):
            wd = posixpath.join(wd, g["dir"])
        return wd

    def remote_argv(self, item, m, g):
        argv = (shlex.split(m["python"]) + shlex.split(g["args"] if g else "")
                + shlex.split(item.get("target") or "") + shlex.split(item.get("args") or ""))
        argv += shlex.split(item.get("optset") or "")  # the card's option set
        return argv

    def remote_env_layers(self, item, m, g, run_id=None):
        """
        Env for a run on a machine, as (low, high): low is set before the machine's own .env is loaded there,
        high after it, so later values win. From least to most specific:
        .env on this server < the machine's .env < machine env_file / env < headline env_file / env < test env.
        """
        low = {"PYTHONUNBUFFERED": "1", "PYTEST_BOARD_MACHINE": m["name"]}
        if run_id is not None:
            low["PYTEST_BOARD_RUN_ID"] = str(run_id)
        if item.get("grp"):
            low["PYTEST_BOARD_GROUP"] = item["grp"]
        low.update(read_env_file(self.env_file)[0])          # .env next to pytest-board.ini
        high = dict(read_env_file(m.get("env_file_path"))[0])  # machine env_file
        high.update(m["env"])                                # machine env
        if g:
            high.update(read_env_file(g["env_file"])[0])      # headline env_file
            high.update(g["env"])                            # headline env
        high.update(self.parse_env(item.get("env")))         # the test's own env wins
        ok = lambda d: {k: v for k, v in d.items() if ENV_KEY_RX.match(k) and k != "PYTEST_BOARD_TOKEN"}
        return ok(low), ok(high)

    def remote_env(self, item, m, g, run_id=None):
        low, high = self.remote_env_layers(item, m, g, run_id)
        return dict(low, **high)

    # --- runs on machines
    # A run goes on on its machine by itself, so a dropped connection, a long network outage or a restart of
    # the board doesn't end it. It lives in ~/.pytest-board/runs/<board id>-<run id>/ there: run.sh (the
    # command), pid, log (pytest's output), junit.xml and rc (the exit code, written last). With screen
    # installed on the machine it runs in a screen session (Attach in the card dialog opens it), otherwise as a
    # background process. The board follows the log over ssh from the byte it has got to, reconnects whenever
    # the connection drops, and collects rc and junit.xml at the end.
    RUNS_DIR = ".pytest-board/runs"   # under the ssh user's home on the machine
    HELLO = b"\x02PB_HELLO\x03\n"     # first bytes of every follow connection: the script there is running
    RC_START = 98                     # the run could not be started on the machine

    # sh functions used on the machine. alive: the run's shell still runs (pid not reused by something else).
    _SH_ALIVE = r'''alive() {
  P=$(cat "$D/pid" 2>/dev/null) && [ -n "$P" ] && kill -0 "$P" 2>/dev/null || return 1
  [ ! -r "/proc/$P/cmdline" ] || grep -q run.sh "/proc/$P/cmdline" 2>/dev/null
}
'''
    # stop_run: TERM to the run's process group (pytest, xdist workers, tee), KILL after 10 s
    _SH_STOP = r'''stop_run() {
  alive || return 0
  kill -s TERM -- "-$P" 2>/dev/null || kill -s TERM "$P" 2>/dev/null
  i=0; while [ $i -lt 10 ] && alive; do sleep 1; i=$((i+1)); done
  alive && { kill -s KILL -- "-$P" 2>/dev/null || kill -s KILL "$P" 2>/dev/null; }
  return 0
}
'''
    # print the log from byte O on as it grows. exit 0: finished (rc exists, so the log is complete),
    # exit 4: the run's shell is gone without an exit code. A heartbeat on stderr every 10 s makes this loop
    # end soon after its connection is gone.
    _SH_FOLLOW = r'''out() {
  S=$(wc -c < "$D/log" 2>/dev/null) || S=0; S=$((S+0))
  if [ "$S" -gt "$O" ]; then tail -c +$((O+1)) "$D/log" 2>/dev/null | head -c $((S-O)); O=$S; fi
}
while :; do
  F=0; [ -f "$D/rc" ] && F=1
  out
  [ $F = 1 ] && exit 0
  if ! alive && { [ -f "$D/pid" ] || [ $n -ge 30 ]; }; then
    sleep 1
    [ -f "$D/rc" ] && continue
    out; exit 4
  fi
  n=$((n+1)); [ $((n % 10)) = 0 ] && printf '\006' >&2
  sleep 1
done
'''
    # kill_dir TAG: end the run in that folder if it still goes on, then remove the folder
    _SH_KILL_DIR = r'''kill_dir() {
  D="$R/$1"
  if [ -d "$D" ]; then
    P=$(cat "$D/pid" 2>/dev/null)
    if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && { [ ! -r "/proc/$P/cmdline" ] || grep -q run.sh "/proc/$P/cmdline" 2>/dev/null; }; then
      kill -s TERM -- "-$P" 2>/dev/null || kill -s TERM "$P" 2>/dev/null
      i=0; while [ $i -lt 5 ] && kill -0 "$P" 2>/dev/null; do sleep 1; i=$((i+1)); done
      kill -s KILL -- "-$P" 2>/dev/null || kill -s KILL "$P" 2>/dev/null
    fi
    rm -rf "$D"
  fi
  [ -d "$D" ] || echo "PB_GONE $1"
}
'''

    def _tag(self, run_id):
        return f"{self.store.board_id}-{run_id}"

    def _session(self, run_id):
        return f"pb-run-{run_id}-{self.store.board_id}"

    @staticmethod
    def _remote_info(run):
        try:
            return json.loads(run.get("remote") or "null")
        except ValueError:
            return None

    def _save_info(self, run_id, info):
        self.store.x("UPDATE runs SET remote=? WHERE id=?", (json.dumps(info) if info else None, run_id))

    def _offline(self, m, detail):
        """A run couldn't reach its machine: hold the machine's queued cards until a health check gets through."""
        if (self.machines.status.get(m["name"]) or {}).get("state") == "offline":
            return  # keep the health checks' rhythm (they retry every RECHECK seconds)
        self.machines.set_status(m["name"], state="offline", detail=str(detail)[:300], checked=time.time())

    def _set_link(self, cid, state, **kw):
        """Connection state of a run that is followed on its machine, for the board ('reconnecting')."""
        with self.store.lock:
            if state:
                self.links[cid] = dict(kw, state=state)
            elif self.links.pop(cid, None) is None:
                return
            self.store.version += 1

    def _note(self, run_id, info, log, st, text):
        """A line from the board in the middle of a run's log. Counted, so that after a restart the board knows
        how much of its log came from the machine, and asks for the rest from the right byte."""
        with st["lock"]:
            b = (b"" if st["nl"] else b"\n") + f"[pytest-board] {text}\n".encode()
            info["notes"] = info.get("notes", 0) + len(b)
            self._save_info(run_id, info)
            log.write(b)
            log.flush()
            st["nl"] = True

    @staticmethod
    def _span(sec):
        sec = int(sec)
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s" \
            if sec >= 60 else f"{sec}s"

    @staticmethod
    def _ssh_error(err):
        lines = [ln.strip() for ln in err.decode("utf-8", "replace").splitlines()
                 if ln.strip() and not ln.startswith("Warning: Permanently")]
        return lines[-1][:300] if lines else ""

    def _run_remote(self, card, run_id, m, log, junit_path, deadline=None):
        g = self.groups.get(card.get("grp"))
        dest = self.dest(m)
        wd = self.remote_dir(m, g)
        argv = self.remote_argv(card, m, g)
        env = self.remote_env_layers(card, m, g, run_id)
        shown = " ".join(shlex.quote(a) for a in argv)
        self.store.x("UPDATE runs SET command=? WHERE id=?", (f"ssh {dest} · cd {wd} && {shown}", run_id))
        log.write(f"[pytest-board] machine {m['name']} ({dest}{':' + str(m['port']) if m['port'] else ''})\n"
                  f"{self.env_file_notes(g, m)}"
                  f"[pytest-board] env: {', '.join(sorted(set(env[0]) | set(env[1])))}\n"
                  f"$ ssh {dest}\n$ cd {wd}\n$ {shown}\n\n".encode())
        log.flush()
        if m["sync"]:
            rc, timed_out = self._sync(m, log, card, deadline)
            if timed_out:
                log.write(b"[pytest-board] timed out during the project sync; tests were not started\n")
                return rc, True
            if card["id"] in self.cancelled:
                return rc, False
            if rc == 255:  # ssh couldn't get through: wait for the machine instead of failing
                self._offline(m, "not reachable for the project sync")
                return self.RC_WAIT, False
            if rc != 0:
                log.write(f"[pytest-board] project sync failed (exit {rc}); tests were not started\n".encode())
                return self.RC_SYNC_FAILED, False
        if card["id"] in self.cancelled:
            return -1, False
        branch = self.resolve_branch(card)[0]
        if branch and m["sync"]:
            log.write(f"[pytest-board] can't check out branch {branch} on {m['name']}: it gets a copy of the project "
                      f"(sync = yes), not a git checkout. Turn sync off and keep a git checkout in its project "
                      f"folder, or remove the branch from this card.\n".encode())
            return self.RC_CHECKOUT, False
        if branch and card["id"] in self.co_skip:
            log.write(f"[pytest-board] branch {branch} is checked out already on {m['name']} for a run in progress; "
                      f"using it as it is\n".encode())
            branch = None
        info, rc = self._launch(run_id, m, wd, argv, env, log, branch)
        if info is None:
            return rc, False
        return self._follow(card, run_id, m, info, log, junit_path, deadline)

    # co(): switch the machine's project folder to the latest @B@ from its git remote: refuses to touch uncommitted
    # changes or local commits that aren't on the remote
    _SH_CHECKOUT = r'''co() {
  W=@W@; WS=@WS@; B=@B@
  echo "[pytest-board] git: switching $WS to branch $B"
  cd "$W" 2>/dev/null || { printf '[pytest-board] project folder not found: %s\n' "$WS"; return 97; }
  git rev-parse --git-dir >/dev/null 2>&1 || { echo "[pytest-board] $WS is not a git checkout, so there is no branch to switch to"; return 95; }
  if [ -n "$(git status --porcelain --untracked-files=no 2>&1)" ]; then
    echo "[pytest-board] $WS has uncommitted changes, so the board won't switch branches there:"
    git status --porcelain --untracked-files=no 2>&1 | head -n 20
    echo "[pytest-board] commit or stash them, or discard them with: git -C $WS checkout -- ."
    return 95
  fi
  GR=$(git remote | grep -x origin || git remote | head -n 1)
  [ -n "$GR" ] || { echo "[pytest-board] $WS has no git remote to fetch from"; return 95; }
  echo "\$ git fetch --prune $GR"
  GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}" git fetch --prune "$GR" || { echo "[pytest-board] git fetch $GR failed (see above)"; return 95; }
  if ! git rev-parse --verify --quiet "refs/remotes/$GR/$B" >/dev/null; then
    case "$B" in "$GR"/*) H=" (use the name without $GR/)";; *) H=;; esac
    echo "[pytest-board] branch $B isn't on $GR$H"; return 95
  fi
  if git rev-parse --verify --quiet "refs/heads/$B" >/dev/null; then
    git checkout --quiet "$B" || { echo "[pytest-board] git checkout $B failed (see above)"; return 95; }
  else
    git checkout --quiet -b "$B" --track "$GR/$B" || { echo "[pytest-board] git checkout $B failed (see above)"; return 95; }
  fi
  git merge --ff-only --quiet "$GR/$B" || { echo "[pytest-board] the local branch $B has commits that aren't on $GR, so it can't be brought up to date. Push or remove them (git -C $WS log $GR/$B..$B)"; return 95; }
  echo "[pytest-board] at $(git log -1 --format='%h %s (%an, %cr)')"
}'''

    def _launch(self, run_id, m, wd, argv, env, log, branch=None):
        """Start the run on the machine, detached from this connection. env: (low, high), see remote_env_layers.
        branch: switch the project folder to it first. Returns (remote info, None), or (None, exit code) when it
        didn't start."""
        tag, session = self._tag(run_id), self._session(run_id)
        cmd = " ".join(self._rword(a) for a in argv) + ' "--junitxml=$D/junit.xml"'
        co = (self._SH_CHECKOUT.replace("@W@", self._rpath(m["workdir"] or "~"))
              .replace("@WS@", shlex.quote(m["workdir"] or "~")).replace("@B@", shlex.quote(branch))) if branch else ""
        run_sh = "\n".join([
            f"# pytest-board run {run_id} on {m['name']}. Written by the board; removed when the run is collected.",
            'D=$(cd "$(dirname "$0")" && pwd) || exit 1',
            'echo $$ > "$D/pid"',
            "trap : INT  # Ctrl+C (in the screen session) interrupts pytest, which still reports and exits",
            # pytest gets the default Ctrl+C handling even if it was ignored on the way here (background start)
            'SIGDFL=; env --default-signal=INT true 2>/dev/null && SIGDFL="env --default-signal=INT"',
            co,
            "run() {",
            "  co || return $?" if branch else "  :",
            f"  cd {self._rpath(wd)} 2>/dev/null || {{ printf '[pytest-board] remote folder not found: %s\\n' "
            f"{shlex.quote(wd)}; return {self.RC_NO_WORKDIR}; }}",
            f"  $SIGDFL {cmd}",
            "}",
            f'{{ trap : INT; run; echo $? > "$D/rc.tmp"; }} 2>&1 | (trap \'\' INT; exec tee -a "$D/log")',
            '[ -f "$D/rc.tmp" ] && mv -f "$D/rc.tmp" "$D/rc"',
            "",
        ])
        eof = "PB_EOF_" + secrets.token_hex(6)
        # The script travels over ssh's stdin; env values only exist in the environment of the run's processes
        # (not on a command line, not in a file).
        script = "".join([
            "umask 077\n",
            f'R="$HOME/{self.RUNS_DIR}"; D="$R/{tag}"\n',
            'mkdir -p "$R" && rm -rf "$D" && mkdir "$D" || { echo "PB_ERR cannot create $D"; exit 3; }\n',
            f"cat > \"$D/run.sh\" <<'{eof}'\n{run_sh}{eof}\n",
            ': > "$D/log"\n',
            *(f"export {k}={shlex.quote(v)}\n" for k, v in env[0].items()),
            self._source_machine_env(m),
            *(f"export {k}={shlex.quote(v)}\n" for k, v in env[1].items()),
            # in the run folder (a .screenrc with "deflog on" writes its screenlog there), with a TERM even when
            # ssh gave none (screen then names the session's terminal "screen" anyway)
            f'if command -v screen >/dev/null 2>&1 && (cd "$D" && TERM="${{TERM:-xterm}}" '
            f'screen -dmS {session} sh "$D/run.sh" </dev/null >/dev/null) 2>"$D/screen.err"; then\n',
            '  echo "PB_STARTED screen"\n',
            "else\n",
            '  [ -s "$D/screen.err" ] && echo "PB_NOTE screen did not start: $(head -c 300 "$D/screen.err")"\n',
            '  if command -v setsid >/dev/null 2>&1; then setsid sh "$D/run.sh" </dev/null >/dev/null 2>&1 &\n',
            '  else nohup sh "$D/run.sh" </dev/null >/dev/null 2>&1 &\n',
            "  fi\n",
            '  echo "PB_STARTED background"\n',
            "fi\n",
        ])
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                               capture_output=True, timeout=90, cwd=str(self.root), **QUIET_KW)
            out, err, rc = r.stdout.decode("utf-8", "replace"), self._ssh_error(r.stderr), r.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "no answer within 90 s", 255
        except OSError as e:
            log.write(f"failed to start ssh ({self.ssh_cmd[0]}): {e}\n".encode())
            return None, self.RC_SSH
        lines = out.splitlines()
        mode = next((ln.split()[1] for ln in lines if ln.startswith("PB_STARTED ")), None)
        for ln in lines:
            if ln.startswith("PB_NOTE "):
                log.write(f"[pytest-board] {ln[8:]}\n".encode())
        if not mode:
            # it may have got as far as creating the folder: tidy that away once the machine answers
            self._save_info(run_id, {"tag": tag, "session": session})
            if rc == 255:
                log.write(f"[pytest-board] can't reach {m['name']}: {err or 'ssh exit code 255'}\n".encode())
                self._offline(m, err or "unreachable")
                return None, self.RC_WAIT
            msg = next((ln[7:] for ln in lines if ln.startswith("PB_ERR ")), None) or err or f"exit code {rc}"
            log.write(f"[pytest-board] could not start the run on {m['name']}: {msg}\n".encode())
            return None, self.RC_START
        info = {"tag": tag, "session": session if mode == "screen" else None, "base": None, "notes": 0}
        if mode == "screen":
            log.write(f"[pytest-board] started on {m['name']} in screen session {session}. It goes on by itself if "
                      f"the connection drops; Attach (in the card) opens the session.\n\n".encode())
        else:
            log.write(f"[pytest-board] started on {m['name']} as a background process. It goes on by itself if the "
                      f"connection drops. (Install screen there to be able to attach to runs.)\n\n".encode())
        log.flush()
        info["base"] = os.fstat(log.fileno()).st_size  # the machine's output starts here in the local log
        self._save_info(run_id, info)
        return info, None

    def _machine_env_file(self, m):
        """(sh expression, shown path) of the machine's own .env, relative paths from its workdir; or None."""
        f = self.remote_env_path(m)
        if not f:
            return None
        if f.startswith(("/", "~")):
            return self._rpath(f), f
        return "./" + shlex.quote(f), posixpath.join(m["workdir"] or "~", f)

    def _source_machine_env(self, m, errfile='"$D/env.err"'):
        """sh lines for the launcher: load the machine's own .env (as `set -a; . file`), if it has one."""
        mf = self._machine_env_file(m)
        if not mf:
            return ""
        f, shown = mf
        return (f"if cd {self._rpath(m['workdir'] or '~')} 2>/dev/null && [ -f {f} ]; then\n"
                f'  if ( . {f} ) >/dev/null 2>{errfile}; then\n'
                f"    set -a; . {f} >/dev/null 2>&1; set +a\n"
                f"    printf 'PB_NOTE loaded %s on {m['name']}\\n' {shlex.quote(shown)}\n"
                f"  else\n"
                f"    printf 'PB_ERR %s on {m['name']} has an error: %s\\n' {shlex.quote(shown)} "
                f'"$(head -c 300 {errfile} | tr \'\\n\' \' \')"; exit 3\n'
                f"  fi\n"
                f"fi\n"
                f"cd\n")

    def _follow(self, card, run_id, m, info, log, junit_path, deadline, resumed=False):
        """Copy a run's log from its machine until the run ends, reconnecting whenever the connection drops,
        then collect its exit code and junit.xml. Returns (exit code, timed_out)."""
        cid = card["id"]
        self.following[cid] = m["name"]
        ev = self.nudge[cid] = threading.Event()
        try:
            return self._follow_loop(card, run_id, m, info, log, junit_path, deadline, ev, resumed)
        finally:
            self.following.pop(cid, None)
            self.nudge.pop(cid, None)
            self.killing.discard(cid)
            self.kill_req.discard(cid)
            self._set_link(cid, None)

    def _follow_loop(self, card, run_id, m, info, log, junit_path, deadline, ev, resumed):
        cid, name = card["id"], m["name"]
        log.flush()
        st = {"nl": True, "down": None, "lock": threading.Lock()}
        size = os.fstat(log.fileno()).st_size
        if size:
            with open(log.name, "rb") as fh:
                fh.seek(size - 1)
                st["nl"] = fh.read(1) == b"\n"
        offset = max(0, size - info["base"] - info.get("notes", 0))  # bytes of the machine's log we have
        if resumed:
            self._note(run_id, info, log, st, f"the board restarted; following the run on {name} again")
        why, phase, delay = None, "follow", 5
        while True:
            if self.stopping:
                raise _Detached()
            if why is None:
                if cid in self.cancelled:
                    why = "stop"
                elif deadline is not None and time.time() >= deadline:
                    why = "timeout"
                if why:
                    phase = "follow"
                    self._note(run_id, info, log, st, f"{'Stop' if why == 'stop' else 'Timeout'}: "
                                                      f"ending the run on {name}…")
            m = self.machines.get(name) or m  # pick up edits (host, port, key) between connections
            if phase == "follow":
                res = self._follow_once(card, run_id, m, info, log, st, offset, why is not None,
                                        None if why else deadline)
                offset += res["got"]
            else:
                res = self._collect_once(m, info, junit_path)
                if res["hello"]:
                    self._back(cid, run_id, m, info, log, st)
            if res["hello"]:
                delay = 5
            kind = res["kind"]
            if kind == "finished":  # the exit code is there, so the log is complete
                phase = "collect"
                continue
            if kind == "collected":
                self._tidy_soon(m, [run_id])
                return res["rc"], why == "timeout"
            if kind in ("gone", "nofolder"):
                self._tidy_soon(m, [run_id])
                if why:
                    return -1, why == "timeout"
                self._note(run_id, info, log, st,
                           f"the run ended on {name} without an exit code: the machine may have restarted, or its "
                           f"processes were killed there" if kind == "gone" else
                           f"the run's folder on {name} is gone (~/{self.RUNS_DIR}/{info['tag']})")
                return self.RC_LOST, False
            if kind == "killed":
                continue  # Stop or the timeout ended this connection; the next one ends the run there
            # the connection failed or dropped
            time.sleep(0.3)
            if self.stopping:
                raise _Detached()
            err = res["err"] or "connection closed"
            if why:  # can't get through to end the run: it's ended there once the machine answers again
                self._note(run_id, info, log, st, f"can't reach {name} ({err}). The run there is ended as soon "
                                                  f"as {name} answers again.")
                return -1, why == "timeout"
            if st["down"] is None:
                st["down"] = time.time()
                self._note(run_id, info, log, st, f"no connection to {name} ({err}) since "
                                                  f"{time.strftime('%H:%M:%S')}. The run goes on there by itself; "
                                                  f"reconnecting…")
                self._set_link(cid, "reconnecting", since=st["down"], detail=err)
            self._offline(m, err)
            wait = delay if deadline is None else max(0.0, min(delay, deadline - time.time()))
            ev.wait(wait)
            ev.clear()
            delay = min(60, delay * 2)

    def _back(self, cid, run_id, m, info, log, st):
        """A connection got through after an outage."""
        if st["down"] is None:
            return
        gone = time.time() - st["down"]
        st["down"] = None
        self._note(run_id, info, log, st, f"reconnected to {m['name']} after {self._span(gone)}; "
                                          f"catching up on the output")
        self._set_link(cid, None)
        threading.Thread(target=self._check_safe, args=(m,), daemon=True).start()  # machine shows green again

    def _follow_once(self, card, run_id, m, info, log, st, offset, kill, deadline):
        """One ssh connection that streams the log from `offset` (with kill: ends the run first)."""
        cid = card["id"]
        res = {"kind": "conn", "got": 0, "hello": False, "err": "", "closed": False}
        ses = info.get("session")
        script = ("{\n"
                  "printf '\\002PB_HELLO\\003\\n'\n"
                  f'D="$HOME/{self.RUNS_DIR}/{info["tag"]}"; O={int(offset)}; n=0\n'
                  'cd "$D" 2>/dev/null || exit 3\n'
                  + self._SH_ALIVE
                  + (self._SH_STOP + "stop_run\n"
                     + (f"screen -S {shlex.quote(ses)} -X quit >/dev/null 2>&1\n" if ses else "") + "n=30\n"
                     if kill else "")
                  + self._SH_FOLLOW + "}\n")
        try:
            # quicker keepalive than other connections: a dead link is noticed in about 30 s
            p = subprocess.Popen(self.ssh_base(m, keepalive=(10, 3)) + [self.dest(m), "sh -s"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=str(self.root), **CHILD_KW)
        except OSError as e:
            res["err"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return res
        errbuf = bytearray()

        def pump_err():
            for chunk in iter(lambda: p.stderr.read1(4096), b""):
                if len(errbuf) < 8192:
                    errbuf.extend(chunk.replace(b"\x06", b""))  # minus the heartbeats

        def pump_out():
            pending = b""
            for chunk in iter(lambda: p.stdout.read1(65536), b""):
                if res["closed"]:
                    return
                if not res["hello"]:  # skip anything a login script printed before the marker
                    pending += chunk
                    i = pending.find(self.HELLO)
                    if i < 0:
                        pending = pending[-(len(self.HELLO) - 1):]
                        continue
                    chunk = pending[i + len(self.HELLO):]
                    res["hello"] = True
                    self._back(cid, run_id, m, info, log, st)
                    if not chunk:
                        continue
                with st["lock"]:
                    log.write(chunk)
                    log.flush()
                    st["nl"] = chunk.endswith(b"\n")
                res["got"] += len(chunk)
                watch(chunk)

        def watch(chunk):  # the run script prints "[pytest-board] at <commit>" once the branch is checked out
            if self.switching.get(m["name"]) == cid:
                st["co_tail"] = buf = st.get("co_tail", b"") + chunk
                if b"[pytest-board] at " in buf:
                    self._switch_done(cid)
                st["co_tail"] = buf[-24:]

        threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err)]
        for t in threads:
            t.start()
        if kill:
            self.killing.add(cid)
        try:
            try:
                p.stdin.write(script.encode())
                p.stdin.close()
            except OSError:
                pass
            rc, timed_out = self._wait(card, p, deadline)
        finally:
            self.killing.discard(cid)
        threads[0].join(15)
        threads[1].join(2)
        res["closed"] = True
        on_purpose = timed_out or cid in self.kill_req
        self.kill_req.discard(cid)
        res["err"] = self._ssh_error(bytes(errbuf))
        if res["hello"] and rc in (0, 3, 4):
            res["kind"] = {0: "finished", 3: "nofolder", 4: "gone"}[rc]
        elif on_purpose:
            res["kind"] = "killed"
        return res

    def _collect_once(self, m, info, junit_path):
        """Fetch a finished run's exit code and junit.xml."""
        res = {"kind": "conn", "hello": False, "err": "", "rc": None}
        script = ("{\n"
                  "printf '\\002PB_HELLO\\003\\n'\n"
                  f'D="$HOME/{self.RUNS_DIR}/{info["tag"]}"\n'
                  'cd "$D" 2>/dev/null || exit 3\n'
                  'printf "PB_RC %s\\n" "$(cat rc 2>/dev/null)"\n'
                  '[ -f junit.xml ] && { echo PB_JUNIT; cat junit.xml; }\n'
                  "exit 0\n}\n")
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                               capture_output=True, timeout=120, cwd=str(self.root), **QUIET_KW)
        except subprocess.TimeoutExpired:
            res["err"] = "no answer within 120 s"
            return res
        except OSError as e:
            res["err"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return res
        out, i = r.stdout, r.stdout.find(self.HELLO)
        res["err"] = self._ssh_error(r.stderr)
        if i < 0:
            return res
        res["hello"] = True
        out = out[i + len(self.HELLO):]
        if r.returncode == 3:
            res["kind"] = "nofolder"
            return res
        if r.returncode != 0 or not out.startswith(b"PB_RC "):
            return res  # dropped half-way: try again
        line, _, rest = out.partition(b"\n")
        v = line[6:].strip()
        if rest.startswith(b"PB_JUNIT\n") and rest[9:].strip():
            junit_path.write_bytes(rest[9:])
        res["kind"], res["rc"] = "collected", int(v) if v.lstrip(b"-").isdigit() else self.RC_LOST
        return res

    def _tidy_soon(self, m, run_ids):
        threading.Thread(target=self.tidy, args=(m, run_ids), daemon=True).start()

    def tidy(self, m, run_ids=None, sweep=False):
        """
        Remove run folders on a machine: those of the given runs, or (None) of every finished run that still has
        one there, e.g. a run stopped while the machine was unreachable (it is ended first if it still goes on).
        sweep: also this board's folders that no running run owns (left behind by a crash), and the temp files
        of older pytest-board versions. Returns True when the machine answered.
        """
        s = self.store
        if run_ids is None:
            rows = s.q("SELECT id, remote FROM runs WHERE machine=? AND remote IS NOT NULL AND outcome<>'running'",
                       (m["name"],))
        else:
            rows = s.q(f"SELECT id, remote FROM runs WHERE remote IS NOT NULL AND id IN "
                       f"({','.join('?' * len(run_ids))})", list(run_ids)) if run_ids else []
        items = [(r["id"], self._remote_info(r)) for r in rows]
        items = [(rid, x) for rid, x in items if x and x.get("tag")]
        if not items and not sweep:
            return True
        script = "{\n" + f'R="$HOME/{self.RUNS_DIR}"\n' + self._SH_KILL_DIR
        for _, x in items:
            script += f"kill_dir {shlex.quote(x['tag'])}\n"
            if x.get("session"):  # a session kept open by the user's .screenrc (zombie)
                script += (f"command -v screen >/dev/null 2>&1 && "
                           f"screen -S {shlex.quote(x['session'])} -X quit >/dev/null 2>&1\n")
        if sweep:
            keep = " ".join(self._tag(r["id"]) for r in s.q("SELECT id FROM runs WHERE outcome='running'"))
            script += (f'for D in "$R"/{self.store.board_id}-*; do [ -d "$D" ] || continue; t=${{D##*/}}; '
                       f'case " {keep} " in *" $t "*) ;; *) kill_dir "$t" ;; esac; done\n')
            script += ("find /tmp -maxdepth 1 -name 'pytest-board-*' -user \"$(id -u)\" -mmin +1440 "
                       "-exec rm -f {} + 2>/dev/null\n")
        script += "echo PB_TIDY_OK\n}\n"
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                               capture_output=True, timeout=120, cwd=str(self.root), **QUIET_KW)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if b"PB_TIDY_OK" not in r.stdout:
            return False
        gone = set(re.findall(rb"PB_GONE (\S+)", r.stdout))
        for rid, x in items:
            if x["tag"].encode() in gone:
                s.x("UPDATE runs SET remote=NULL WHERE id=?", (rid,))
        if sweep:
            self.swept.add(m["name"])
        return True

    # never copied to a machine (.env: the machine keeps its own; this server's values are sent with each run)
    SYNC_SKIP = {".pytest-board", ".git", "__pycache__", ".pytest_cache", ".env"}

    def sync_method(self):
        """rsync when it's installed (fast: only changed files are sent); otherwise a tar stream over ssh (stdlib
        only, copies everything each time). Windows always uses tar: rsync builds there can't read C:\\ paths."""
        if IS_WIN or not shutil.which(self.rsync_cmd[0]):
            return "tar"
        return "rsync"

    def _sync(self, m, log, card, deadline=None):
        """
        Copy the project (--root) to the machine's workdir before the run.
        Returns (exit code, timed_out). Stop and the card's timeout interrupt it.
        """
        base, dest = self.ssh_base(m), self.dest(m)
        try:
            mk = subprocess.Popen(base + [dest, f"mkdir -p {self._rpath(m['workdir'])}"], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, **CHILD_KW)
            try:
                rc, timed_out = self._wait(card, mk, deadline if deadline is not None else time.time() + 60)
            finally:
                self.procs.pop(card["id"], None)
            if timed_out or rc != 0 or card["id"] in self.cancelled:
                return (rc or 1), (timed_out and deadline is not None and time.time() >= deadline)
            limit = time.time() + 1800
            if self.sync_method() == "rsync":
                cmd = list(self.rsync_cmd) + [
                    "-az", *(f"--exclude={x}" for x in sorted(self.SYNC_SKIP)),
                    "-e", " ".join(shlex.quote(x) for x in base),
                    f"{self.root}/", f"{dest}:{m['workdir'].rstrip('/')}/"]
                log.write(f"$ rsync -az {self.root}/ {dest}:{m['workdir'].rstrip('/')}/\n".encode())
                log.flush()
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **CHILD_KW)
                rc, timed_out = self._wait(card, p, limit if deadline is None else min(deadline, limit))
            else:
                rc, timed_out = self._sync_tar(m, base, dest, log, card,
                                               limit if deadline is None else min(deadline, limit))
            if timed_out and (deadline is None or time.time() < deadline):
                log.write(b"sync error: the copy took longer than 30 minutes\n")
                return 1, False
            return rc, timed_out
        except OSError as e:
            log.write(f"sync error: {e}\n".encode())
            return 1, False

    def _sync_tar(self, m, base, dest, log, card, deadline):
        """Stream the project as a .tar.gz into `tar -x` on the machine (needs only ssh here and tar there)."""
        wd = self._rpath(m["workdir"])
        log.write(f"$ (tar of {self.root}) | ssh {dest} 'tar -xzf - -C {m['workdir']}'\n".encode())
        log.flush()
        p = subprocess.Popen(base + [dest, f"tar -xzf - -C {wd}"], stdin=subprocess.PIPE, stdout=log,
                             stderr=subprocess.STDOUT, **CHILD_KW)
        sent = {"files": 0, "error": None}

        def skip(info):
            parts = info.name.replace("\\", "/").split("/")
            if any(x in self.SYNC_SKIP for x in parts) or info.name.endswith(".pyc"):
                return None
            if info.isfile():
                sent["files"] += 1
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info

        def writer():
            try:
                with tarfile.open(fileobj=p.stdin, mode="w|gz") as tf:
                    for entry in sorted(os.listdir(self.root)):
                        if entry not in self.SYNC_SKIP:
                            tf.add(str(self.root / entry), arcname=entry, filter=skip)
            except (OSError, ValueError) as e:  # ssh went away (Stop, timeout, no connection)
                sent["error"] = e
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        rc, timed_out = self._wait(card, p, deadline)
        t.join(5)
        if rc == 0 and sent["error"] is None:
            log.write(f"[pytest-board] copied {sent['files']} file(s) to {m['workdir']}\n".encode())
        elif sent["error"] is not None and rc == 0:
            log.write(f"sync error: {sent['error']}\n".encode())
            rc = 1
        return rc, timed_out

    def check_machine(self, m):
        """Connect, look for the workdir and pytest. Updates machines.status."""
        name = m["name"]
        with self.checking_lock:
            already = name in self.checking
            if not already:
                self.checking.add(name)
        if already:  # a check is already running: wait for its result
            for _ in range(90):
                time.sleep(0.5)
                if name not in self.checking:
                    break
            return self.machines.status.get(name)
        if (self.machines.status.get(name) or {}).get("state") != "offline":
            self.machines.set_status(name, state="checking")  # an offline machine stays offline until it answers
        try:
            wd = m["workdir"] or "~"
            py = " ".join(self._rword(a) for a in shlex.split(m["python"]))
            mf = self._machine_env_file(m)
            script = (f"echo PB_OK; uname -srm; if cd {self._rpath(wd)} 2>/dev/null; then echo PB_DIR_OK; "
                      + (f"[ -f {mf[0]} ] && echo PB_ENV_FILE; " if mf else "")
                      + f"else echo PB_DIR_MISSING; fi; "
                      f"{py} --version 2>&1 | tail -n 8\n")  # tail: wrappers like poetry may warn first
            try:
                # bytes, not text: text mode would turn \n into \r\n on Windows, which breaks the remote sh
                r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                                   capture_output=True, timeout=40, **QUIET_KW)
                out, err, rc = (r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"),
                                r.returncode)
            except subprocess.TimeoutExpired:
                out, err, rc = "", "timed out after 40s", -1
            except OSError as e:
                out, err, rc = "", f"cannot run {self.ssh_cmd[0]}: {e}", -1
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if "PB_OK" not in lines:
                errl = [ln for ln in err.splitlines() if ln.strip() and not ln.startswith("Warning: Permanently")]
                st = {"state": "offline", "detail": (errl[-1] if errl else f"ssh exit code {rc}")[:300], "system": ""}
            else:
                i = lines.index("PB_OK")
                system = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].startswith("PB_") else ""
                ver = next((ln for ln in lines if ln.lower().startswith("pytest ")), None)
                if "PB_DIR_OK" not in lines and not m["sync"]:
                    st = {"state": "warn", "detail": f"remote folder not found: {wd}", "system": system}
                elif not ver:
                    tail = [ln for ln in lines if not ln.startswith("PB_") and ln != system]
                    st = {"state": "warn", "detail": "pytest not found" + (f": {tail[-1]}" if tail else ""),
                          "system": system}
                else:
                    note = " · folder is created on first sync" if "PB_DIR_OK" not in lines else ""
                    if "PB_ENV_FILE" in lines:
                        note += f" · loads {mf[1]}"
                    st = {"state": "online", "detail": ver + note, "system": system}
            if st["state"] != "offline":
                # before new runs may start there: end and remove what a run stopped while the machine was
                # unreachable left behind, and once per board start sweep up orphaned run folders
                try:
                    self.tidy(m, sweep=name not in self.swept)
                except Exception as e:
                    print(f"tidying up on {name}: {e}", file=sys.stderr)
            self.machines.set_status(name, checked=time.time(), **st)
            if st["state"] != "offline":
                for cid, mname in list(self.following.items()):  # runs waiting to reconnect: try now
                    ev = self.nudge.get(cid)
                    if mname == name and ev:
                        ev.set()
                self.wake.set()  # queued cards waiting for this machine
            return self.machines.status[name]
        finally:
            with self.checking_lock:
                self.checking.discard(name)

    RECHECK = 30  # seconds between checks of an unreachable machine that has work waiting

    def _waiting_on(self):
        """Machines with something waiting for them: queued cards, runs to reconnect to, leftovers to tidy."""
        names = {self.resolve_machine(c)[0] for c in self.store.q("SELECT machine, grp FROM cards WHERE status='queued'")}
        names.update(self.following.values())
        names.update(r["machine"] for r in self.store.q(
            "SELECT DISTINCT machine FROM runs WHERE remote IS NOT NULL AND outcome<>'running'"))
        return names

    def _health(self):
        """Every --check-interval seconds check each machine; machines that are down and have work waiting are
        checked every RECHECK seconds (also with --check-interval 0), so waiting cards start soon after."""
        time.sleep(1)
        while True:
            try:
                now, waiting = time.time(), None
                for m in self.machines.all():
                    st = self.machines.status.get(m["name"]) or {}
                    age = now - (st.get("checked") or 0)
                    due = self.check_interval > 0 and age >= self.check_interval
                    if not due and st.get("state") == "offline" and age >= self.RECHECK:
                        waiting = self._waiting_on() if waiting is None else waiting
                        due = m["name"] in waiting
                    if due and m["name"] not in self.checking:
                        threading.Thread(target=self._check_safe, args=(m,), daemon=True).start()
            except Exception as e:
                print("health check error:", e, file=sys.stderr)
            time.sleep(5)

    def _check_safe(self, m):
        try:
            self.check_machine(m)
        except Exception as e:
            print(f"health check {m['name']}: {e}", file=sys.stderr)

    @staticmethod
    def _parse_junit(path):
        try:
            root = ET.parse(str(path)).getroot()
        except (OSError, ET.ParseError):
            return {}
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        c = {"total": 0, "failed": 0, "errors": 0, "skipped": 0}
        for su in suites:
            c["total"] += int(su.get("tests", 0))
            c["failed"] += int(su.get("failures", 0))
            c["errors"] += int(su.get("errors", 0))
            c["skipped"] += int(su.get("skipped", 0))
        c["passed"] = c["total"] - c["failed"] - c["errors"] - c["skipped"]
        return c

    @staticmethod
    def _kill(p):
        kill_tree(p)

    def _terminate(self, card_id, p):
        """Stop a run's process (group). For a run on a machine that's the ssh connection following it: the next
        connection then ends the run on the machine (see _follow_loop)."""
        if card_id in self.following:
            if card_id in self.killing:
                return  # this connection is the one ending the run there; let it finish
            self.kill_req.add(card_id)
        self._kill(p)

    def stop(self, card_id):
        with self.store.lock:
            if card_id not in self.active:
                return False
            self.cancelled.add(card_id)  # also seen by a run that hasn't started its process yet
            p = self.procs.get(card_id)
            ev = self.nudge.get(card_id)
        if ev:
            ev.set()  # a run waiting to reconnect tries right away (and ends the run if it gets through)
        if p:
            threading.Thread(target=self._terminate, args=(card_id, p), daemon=True).start()
        return True

    def preview(self, item):
        """Where and how a card/test would run (for the UI)."""
        pv = self._preview(item)
        pv["run_branch"], pv["run_branch_source"] = self.resolve_branch(item)  # the card's own stays in "branch"
        return pv

    def _preview(self, item):
        mname, source = self.resolve_machine(item)
        g = self.groups.get(item.get("grp"))
        if not mname:
            return {"run_machine": None, "run_machine_source": source, "machine_missing": True, "where": "?",
                    "command_preview": "(no machine: drop it into a machine's row, or set machine = for its headline)"}
        m = self.machines.get(mname)
        if not m:
            return {"run_machine": mname, "run_machine_source": source, "machine_missing": True, "where": "?",
                    "command_preview": f"(machine '{mname}' is not defined)"}
        return {"run_machine": mname, "run_machine_source": source, "ssh": self.dest(m),
                "where": f"{self.dest(m)}{':' + str(m['port']) if m['port'] else ''}:{self.remote_dir(m, g)}",
                "command_preview": " ".join(shlex.quote(a) for a in self.remote_argv(item, m, g)),
                "machine_env_keys": sorted(m["env"])}

    def discover(self, grp=None, machine=None):
        """pytest --collect-only for a headline on a machine, over ssh (in its project folder, with its pytest command
        and env; default: the headline's machine). The folder is used as it is: no branch is checked out."""
        if not machine or machine == "local":
            machine = self.resolve_machine({"grp": grp})[0]
        if not machine:
            return {"ids": [], "error": "pick the machine to discover tests on", "grp": grp, "machine": None}
        m = self.machines.get(machine)
        if not m:
            return {"ids": [], "error": f"machine '{machine}' is not defined", "grp": grp, "machine": machine}
        why = self.not_bound({"grp": grp}, machine)
        if why:
            return {"ids": [], "error": why, "grp": grp, "machine": machine}
        return self._discover_remote(m, grp)

    def _discover_remote(self, m, grp):
        item = {"grp": grp}
        g = self.groups.get(grp)
        wd = self.remote_dir(m, g)
        argv = self.remote_argv(item, m, g) + ["--collect-only", "-q", "-p", "no:cacheprovider"]
        low, high = self.remote_env_layers(item, m, g)
        shown = " ".join(shlex.quote(a) for a in argv)
        out = {"ids": [], "error": None, "command": f"ssh {self.dest(m)} · cd {wd} && {shown}",
               "cwd": f"{self.dest(m)}:{wd}", "grp": grp, "machine": m["name"], "branch": ""}
        script = "".join([
            'E="${TMPDIR:-/tmp}/pytest-board-discover-$$.err"; trap \'rm -f "$E"\' EXIT\n',
            *(f"export {k}={shlex.quote(v)}\n" for k, v in low.items()),
            self._source_machine_env(m, errfile='"$E"'),
            *(f"export {k}={shlex.quote(v)}\n" for k, v in high.items()),
            f"cd {self._rpath(wd)} 2>/dev/null || {{ echo PB_NO_DIR; exit 97; }}\n",
            "printf 'PB_BRANCH %s\\n' \"$(git rev-parse --abbrev-ref HEAD 2>/dev/null)\"\n",
            " ".join(self._rword(a) for a in argv) + "\n",
        ])
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(), capture_output=True,
                               timeout=600, cwd=str(self.root), **QUIET_KW)
        except subprocess.TimeoutExpired:
            out["error"] = f"no answer from {m['name']} within 10 minutes"
            return out
        except OSError as e:
            out["error"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return out
        text, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
        lines = text.splitlines()
        out["branch"] = next((ln[10:].strip() for ln in lines if ln.startswith("PB_BRANCH ")), "")
        bad = next((ln[7:] for ln in lines if ln.startswith("PB_ERR ")), None)
        if r.returncode == 255:
            out["error"] = f"can't reach {m['name']}: {self._ssh_error(r.stderr) or 'ssh exit code 255'}"
        elif "PB_NO_DIR" in lines:
            out["error"] = f"the project folder {wd} doesn't exist on {m['name']}"
        elif bad:
            out["error"] = bad
        else:
            out["ids"] = [ln.strip() for ln in lines if "::" in ln and not ln.startswith((" ", "=", "PB_"))]
            if r.returncode not in (0, 5):
                out["error"] = (text[-4000:] + "\n" + err[-4000:]).strip()
        return out


# --------------------------------------------------------------------------- interactive shells (Open shell)
# Becomes the session leader's controlling terminal, then execs ssh. Done in a fresh process instead of a
# preexec_fn, which isn't safe in this multi-threaded server.
# Signals ignored by whoever started the board (nohup, `&` in a script) are reset, so Ctrl+C works.
_CTTY_EXEC = ("import fcntl, os, signal, sys, termios\n"
              "for n in ('SIGINT', 'SIGQUIT', 'SIGTSTP', 'SIGTTIN', 'SIGTTOU', 'SIGPIPE', 'SIGHUP'):\n"
              "    signal.signal(getattr(signal, n), signal.SIG_DFL)\n"
              "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
              "os.execvp(sys.argv[1], sys.argv[1:])")


# Windows has no pseudo-terminal in the standard library. There the shell talks to ssh through plain pipes:
# ssh -tt still gives the remote side a real terminal (line editing, Ctrl+C, vim), and the window size is set
# there with stty; resizing the window afterwards isn't passed on, and login must be key-based.
SHELL_PTY = hasattr(os, "openpty") and not IS_WIN and not os.environ.get("PYTEST_BOARD_SHELL_PIPES")


class Shell:
    def __init__(self, sid, machine, proc, fd, wfd=None):
        self.id, self.machine, self.proc, self.fd = sid, machine, proc, fd
        self.wfd = fd if wfd is None else wfd  # where keystrokes go (the pty, or ssh's stdin pipe)
        self.pty = wfd is None
        self.buf = bytearray()
        self.base = 0  # byte offset of buf[0] in the whole output (old output is dropped past MAX_BUF)
        self.closed = False
        self.exit_code = None
        self.seen = time.time()
        self.cond = threading.Condition()


class Shells:
    """ssh sessions on a pseudo-terminal, driven from the browser (output by long-poll, input by POST)."""
    MAX = 8            # open shells at once
    IDLE = 90          # seconds without a poll before a shell is closed (browser tab gone)
    MAX_BUF = 1 << 21  # output kept for a reconnecting poll

    def __init__(self, runner):
        self.r = runner
        self.items = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, name="shell-reaper", daemon=True).start()

    @staticmethod
    def _winsize(fd, cols, rows):
        import fcntl, struct, termios
        cols, rows = max(20, min(int(cols or 80), 500)), max(5, min(int(rows or 24), 200))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def start(self, m, cols, rows, script=None):
        """A terminal on a machine: a login shell in its project folder, or `script` (e.g. screen -x …)."""
        with self.lock:
            if sum(1 for x in self.items.values() if not x.closed) >= self.MAX:
                raise ApiError(429, f"{self.MAX} shells are open already; close one first")
        if script is None:
            wd = m["workdir"] or "~"
            shown = "~/" + shlex.quote(wd[2:]) if wd.startswith("~/") and shlex.quote(wd[2:]) != wd[2:] else (
                wd if wd.startswith("~") else shlex.quote(wd))
            hint = (f"[pytest-board] {wd} does not exist on {m['name']} yet, so you are in your home folder. "
                    f"Create it with:  mkdir -p {shown}")
            script = (f"cd {Runner._rpath(wd)} 2>/dev/null || echo {shlex.quote(hint)}; "
                      'exec "${SHELL:-/bin/sh}" -l')
        if not SHELL_PTY:
            return self._start_pipes(m, cols, rows, script)
        cmd = self.r.ssh_base(m, interactive=True) + [self.r.dest(m), "sh -c " + shlex.quote(script)]
        master, slave = os.openpty()
        try:
            self._winsize(master, cols, rows)
            env = dict(os.environ, TERM="xterm-256color")
            env.pop("PYTEST_BOARD_TOKEN", None)
            proc = subprocess.Popen([sys.executable, "-c", _CTTY_EXEC] + cmd, stdin=slave, stdout=slave,
                                    stderr=slave, start_new_session=True, env=env, cwd=str(self.r.root))
        except OSError as e:
            os.close(master)
            raise ApiError(500, f"cannot start {self.r.ssh_cmd[0]}: {e}")
        finally:
            os.close(slave)
        sh = Shell("s" + secrets.token_hex(8), m["name"], proc, master)  # never all digits (the router ints those)
        with self.lock:
            self.items[sh.id] = sh
        threading.Thread(target=self._pump, args=(sh,), name=f"shell-{sh.id}", daemon=True).start()
        print(f"shell opened on {m['name']} ({self.r.dest(m)})", file=sys.stderr)
        return sh

    def _start_pipes(self, m, cols, rows, script):
        cols, rows = max(20, min(int(cols or 80), 500)), max(5, min(int(rows or 24), 200))
        script = f"stty cols {cols} rows {rows} 2>/dev/null; " + script
        # BatchMode: without a terminal here ssh can't ask for a password; fail fast instead of hanging
        cmd = (self.r.ssh_base(m, interactive=True) + ["-o", "BatchMode=yes"]
               + [self.r.dest(m), "sh -c " + shlex.quote(script)])
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("PYTEST_BOARD_TOKEN", None)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=env, cwd=str(self.r.root), bufsize=0, **CHILD_KW)
        except OSError as e:
            raise ApiError(500, f"cannot start {self.r.ssh_cmd[0]}: {e}")
        sh = Shell("s" + secrets.token_hex(8), m["name"], proc, proc.stdout.fileno(), proc.stdin.fileno())
        with self.lock:
            self.items[sh.id] = sh
        threading.Thread(target=self._pump, args=(sh,), name=f"shell-{sh.id}", daemon=True).start()
        print(f"shell opened on {m['name']} ({self.r.dest(m)}, pipe mode)", file=sys.stderr)
        return sh

    def _pump(self, sh):
        while True:
            try:
                data = os.read(sh.fd, 65536)
            except OSError:  # EIO: ssh exited and the terminal closed
                data = b""
            if not data:
                break
            with sh.cond:
                sh.buf += data
                if len(sh.buf) > self.MAX_BUF:
                    drop = len(sh.buf) - self.MAX_BUF
                    del sh.buf[:drop]
                    sh.base += drop
                sh.cond.notify_all()
        rc = sh.proc.wait()
        if sh.pty:
            try:
                os.close(sh.fd)
            except OSError:
                pass
        else:
            for f in (sh.proc.stdin, sh.proc.stdout):
                try:
                    f.close()
                except OSError:
                    pass
        with sh.cond:
            sh.closed, sh.exit_code = True, rc
            sh.cond.notify_all()
        print(f"shell on {sh.machine} closed (exit {rc})", file=sys.stderr)

    def get(self, sid):
        sh = self.items.get(str(sid))
        if not sh:
            raise ApiError(404, "no such shell (it was closed)")
        return sh

    def read(self, sid, offset, wait=20.0):
        sh = self.get(sid)
        sh.seen = time.time()
        with sh.cond:
            end = sh.base + len(sh.buf)
            if offset >= end and not sh.closed:
                sh.cond.wait(timeout=wait)
                end = sh.base + len(sh.buf)
            start = max(offset, sh.base)
            data = bytes(sh.buf[start - sh.base:]) if start < end else b""
            sh.seen = time.time()
            return {"data": base64.b64encode(data).decode(), "offset": start + len(data),
                    "closed": sh.closed and start + len(data) >= end, "exit_code": sh.exit_code}

    def write(self, sid, text):
        sh = self.get(sid)
        if sh.closed:
            raise ApiError(409, "the shell has ended")
        data = str(text).encode()
        try:
            while data:
                n = os.write(sh.wfd, data)
                data = data[n:]
        except OSError as e:
            raise ApiError(409, f"the shell has ended ({e})")

    def resize(self, sid, cols, rows):
        sh = self.get(sid)
        if not sh.closed and sh.pty:  # pipe mode (Windows): the size was set once, at the start
            try:
                self._winsize(sh.fd, cols, rows)  # the kernel tells ssh (SIGWINCH), ssh tells the remote side
            except OSError:
                pass

    def close(self, sid):
        sh = self.items.pop(str(sid), None)
        if sh and not sh.closed:
            threading.Thread(target=self._kill, args=(sh,), daemon=True).start()

    @staticmethod
    def _kill(sh):
        if IS_WIN:
            kill_tree(sh.proc, grace=3)
            return
        for sig, wait in ((signal.SIGHUP, 3), (signal.SIGKILL, 0)):  # SIGHUP: like closing a terminal window
            try:
                os.killpg(sh.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                sh.proc.wait(timeout=wait or None)
                return
            except subprocess.TimeoutExpired:
                continue

    def close_all(self):
        for sid in list(self.items):
            sh = self.items.pop(sid, None)
            if sh and not sh.closed:
                try:
                    if IS_WIN:
                        kill_tree(sh.proc, grace=1)
                    else:
                        os.killpg(sh.proc.pid, signal.SIGHUP)
                except (ProcessLookupError, PermissionError):
                    pass

    def _reaper(self):
        while True:
            time.sleep(15)
            now = time.time()
            for sid, sh in list(self.items.items()):
                if now - sh.seen > (self.IDLE if not sh.closed else 60):
                    print(f"closing idle shell on {sh.machine}", file=sys.stderr)
                    self.close(sid)


# --------------------------------------------------------------------------- API
def _grp(body):
    """Normalise a headline key from a request body ('' / null -> no headline)."""
    v = body.get("grp")
    v = str(v).strip() if v is not None else ""
    if v and not GROUP_KEY_RX.match(v):
        raise ApiError(400, "invalid headline name")
    return v or None


def _default_name(target):
    t = target.strip().strip("'\"").rstrip("/")
    if t in ("", "."):
        return "all tests"
    return t.split("::")[-1] if "::" in t else (Path(t).stem or t)


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def validate_schedule(body):
    """Return (run_at, cron) from a move/schedule body."""
    cron = (body.get("cron") or "").strip() or None
    if cron:
        try:
            return Cron(cron).next_after(time.time()), cron
        except ValueError as e:
            raise ApiError(400, str(e))
    run_at = body.get("run_at")
    if run_at is None:
        raise ApiError(400, "scheduling needs run_at (epoch seconds) or cron")
    return float(run_at), None


class App:
    def __init__(self, store, runner, token):
        self.s = store
        self.r = runner
        self.token = token
        self.loopback_only = False  # set by main(): True when bound to localhost (enables the Host check)
        self.shells = Shells(runner)
        self.routes = []
        R = self.route
        R("GET", r"/api/board", self.board)
        R("POST", r"/api/tests", self.test_create)
        R("POST", r"/api/tests/bulk", self.test_bulk)
        R("GET", r"/api/tests/(\d+)", self.test_get)
        R("PATCH", r"/api/tests/(\d+)", self.test_update)
        R("DELETE", r"/api/tests/(\d+)", self.test_delete)
        R("POST", r"/api/tests/(\d+)/copy", self.test_copy)
        R("POST", r"/api/cards/clear", self.cards_clear)
        R("POST", r"/api/cards/(\d+)/save-to-library", self.card_to_test)
        R("POST", r"/api/cards", self.card_create)
        R("POST", r"/api/cards/bulk", self.card_bulk)
        R("GET", r"/api/cards/(\d+)", self.card_get)
        R("PATCH", r"/api/cards/(\d+)", self.card_update)
        R("DELETE", r"/api/cards/(\d+)", self.card_delete)
        R("POST", r"/api/cards/(\d+)/move", self.card_move)
        R("POST", r"/api/cards/(\d+)/run", self.card_run)
        R("POST", r"/api/cards/(\d+)/stop", self.card_stop)
        R("GET", r"/api/runs/(\d+)/log", self.run_log)
        R("GET", r"/api/runs/(\d+)/junit", self.run_junit)
        R("POST", r"/api/runs/(\d+)/attach", self.run_attach)  # a terminal in the run's screen session
        R("GET", r"/api/discover", self.discover)
        R("POST", r"/api/machines", self.machine_create)
        R("POST", r"/api/machines/check-all", self.machines_check_all)
        R("PATCH", r"/api/machines/([A-Za-z0-9_.\-]+)", self.machine_update)
        R("DELETE", r"/api/machines/([A-Za-z0-9_.\-]+)", self.machine_delete)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/check", self.machine_check)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/run", self.machine_run)  # "Run lane"
        R("POST", r"/api/cron/preview", self.cron_preview)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/shell", self.shell_open)
        R("GET", r"/api/shells/(s[0-9a-f]+)", self.shell_read)
        R("POST", r"/api/shells/(s[0-9a-f]+)/input", self.shell_input)
        R("POST", r"/api/shells/(s[0-9a-f]+)/resize", self.shell_resize)
        R("DELETE", r"/api/shells/(s[0-9a-f]+)", self.shell_close)

    def route(self, method, pattern, fn):
        self.routes.append((method, re.compile(pattern + r"$"), fn))

    # ---- headline config file
    def reload_groups(self):
        """Called at start-up and every few seconds; re-reads the INI when it changed."""
        if not self.r.groups.load():
            return
        with self.s.lock:
            for g in self.r.groups.items:
                for spec in g["tests"]:
                    if self.s.one("SELECT 1 FROM seeded WHERE grp=? AND spec=?", (g["key"], spec)):
                        continue  # seeded before; if someone deleted it in the UI it stays deleted
                    # target  |  name | target  |  name | target | args  |  name | target | args | machine
                    parts = [p.strip() for p in spec.split("|")]
                    if len(parts) == 1:
                        name, target, args, machine = None, parts[0], "", ""
                    else:
                        name, target = parts[0], parts[1]
                        args = parts[2] if len(parts) > 2 else ""
                        machine = parts[3] if len(parts) > 3 else ""
                    if machine:
                        print(f"[{g['key']}] test {spec!r}: the machine field ({machine}) is ignored: library tests "
                              "have no machine of their own (copies run on the row they are dropped into)",
                              file=sys.stderr)
                    if not self.s.one("SELECT 1 FROM tests WHERE grp=? AND target=? AND args=?",
                                      (g["key"], target, args)):
                        try:
                            t = self.test_create({}, {"name": name or _default_name(target), "target": target,
                                                      "args": args, "grp": g["key"]})
                        except ApiError as e:
                            print(f"[{g['key']}] test {spec!r}: {e}", file=sys.stderr)
                            continue
                    self.s.x("INSERT OR IGNORE INTO seeded(grp, spec) VALUES(?,?)", (g["key"], spec))
            self.s.version += 1  # make every open browser re-render

    def watch_groups(self):
        while True:
            try:
                self.reload_groups()
            except Exception as e:
                print("headline reload error:", e, file=sys.stderr)
            time.sleep(2)

    # ---- board
    def board(self, q, body):
        since = q.get("since")
        if since and since == str(self.s.version):
            return {"version": self.s.version, "unchanged": True, "page": page_stamp(), "board_version": BOARD_VERSION}
        cards = self.s.q("""
            SELECT c.*, r.outcome AS last_outcome, r.started AS last_started, r.finished AS last_finished,
                   r.total AS last_total, r.passed AS last_passed, r.failed AS last_failed,
                   r.errors AS last_errors, r.skipped AS last_skipped, r.exit_code AS last_exit
            FROM cards c LEFT JOIN runs r ON r.id = c.last_run_id
            ORDER BY c.position, c.id""")
        tests = self.s.q("""
            SELECT t.*,
                   (SELECT COUNT(*) FROM cards c WHERE c.test_id=t.id) AS copies,
                   (SELECT COUNT(*) FROM cards c WHERE c.test_id=t.id AND c.status IN ('queued','running')) AS active,
                   (SELECT COUNT(*) FROM runs WHERE test_id=t.id) AS run_count,
                   r.outcome AS last_outcome, r.started AS last_started, r.finished AS last_finished,
                   r.total AS last_total, r.passed AS last_passed, r.failed AS last_failed,
                   r.errors AS last_errors, r.skipped AS last_skipped, r.exit_code AS last_exit
            FROM tests t
            LEFT JOIN runs r ON r.id = (SELECT MAX(id) FROM runs WHERE test_id=t.id AND outcome<>'running')
            ORDER BY t.position, t.id""")
        for c in cards:  # a run on a machine whose connection is down: "reconnecting" on the card
            link = self.r.links.get(c["id"])
            if link and c["status"] == "running":
                c["link"] = link
        return {
            "version": self.s.version,
            "columns": COLUMNS,
            "cards": cards,
            "tests": tests,
            "groups": self.r.groups.public(),
            "machines": self.machines_public(),
            "groups_config": {
                "path": str(self.r.groups.path) if self.r.groups.path else None,
                "exists": self.r.groups.mtime is not None,
                "error": self._config_error(),
                "env_file": env_file_info(self.r.env_file),  # .env for every run (names only)
            },
            "server": {
                "root": str(self.r.root),
                "now": time.time(),
                "tz": time.strftime("%Z"),
                "user": SERVICE_USER,  # ssh to machines runs as this account (shown in "Help setup")
                "os": "windows" if IS_WIN else sys.platform,  # Help setup shows PowerShell commands on Windows
                "sync": self.r.sync_method(),                 # rsync, or tar over ssh
                "hostname": socket.gethostname(),
                "board_version": BOARD_VERSION,
            },
            "page": page_stamp(),
        }

    def _config_error(self):
        """Problems in the config file, plus headlines naming a machine (machine = / machines =) that exists neither
        in the file nor in the UI (machines added in the UI are fine to name)."""
        problems = [self.r.groups.error] if self.r.groups.error else []
        for g in self.r.groups.items:
            if g["machine"] == "local":
                problems.append(f"[{g['key']}] machine = local: the board server doesn't run tests itself any more; "
                                "name one of your machines")
            # machines = a, b (the default is the first, or machine = if it is one of them); else just machine =
            for name in g["machines"] or ([g["machine"]] if g["machine"] and g["machine"] != "local" else []):
                if not self.r.machines.get(name):
                    problems.append(f"[{g['key']}] machine '{name}' isn't defined: add it in the Machines column "
                                    f"or as a [machine:{name}] section")
        return "; ".join(problems) or None

    # ---- swimlanes: one per machine, plus "No machine" for cards without one. A card's lane is the machine it runs on.
    def _default_lane(self):
        """cards.lane_id is internal now (lanes are machines); every card sits in one hidden lane row."""
        row = self.s.one("SELECT id FROM lanes ORDER BY position, id LIMIT 1")
        return row["id"] if row else self.s.x("INSERT INTO lanes(name, position) VALUES('board', 1)")

    def _lane_machine(self, value, current=None):
        """Validate a lane (machine) a card is dropped into. Returns the value to store in cards.machine
        (None: keep it as it is)."""
        v = str(value or "").strip()
        if v == (current or ""):
            return None  # already runs there (maybe via its headline), or has no machine and stays in that row
        if not v:
            raise ApiError(400, "drop the card into a machine's row")
        if not self.r.machines.get(v):
            raise ApiError(400, LOCAL_GONE if v == "local" else f"unknown machine '{v}'")
        return v

    def _check_bound(self, item, mname=None):
        why = self.r.not_bound(item, mname)
        if why:
            raise ApiError(409, why)

    def machine_run(self, q, body, name):
        """Queue every card in a machine's lane (optionally only one headline's: {"grp": ...})."""
        name = str(name)
        if not self.r.machines.get(name) and not any(
                self.r.resolve_machine(c)[0] == name for c in self.s.q("SELECT machine, grp FROM cards")):
            raise ApiError(404, "no such machine")
        only_grp = "grp" in body
        grp = _grp(body) if only_grp else None
        only_set = "optset" in body  # one option-set group (null = the cards without an option set)
        try:
            optset = normalize_optset(body.get("optset")) if only_set else None
        except ValueError as e:
            raise ApiError(400, str(e))
        statuses = body.get("statuses")  # e.g. ["backlog"]: only the cards in those columns
        if statuses is not None and (not isinstance(statuses, list) or not set(statuses) <= set(COLUMNS)):
            raise ApiError(400, "statuses must be a list of columns")
        now = time.time()
        n = skipped = 0
        with self.s.lock:
            for c in self.s.q("SELECT id, machine, grp, optset, status FROM cards WHERE status NOT IN ('running','queued') "
                              "ORDER BY position, id"):
                if self.r.resolve_machine(c)[0] != name or (only_grp and c["grp"] != grp) or (
                        only_set and c["optset"] != optset) or (statuses is not None and c["status"] not in statuses):
                    continue
                if self.r.not_bound(c, name):  # its headline's tests don't run on this machine (machines =)
                    skipped += 1
                    continue
                self.s.x("UPDATE cards SET status='queued', queued_at=?, pending_trigger='manual' WHERE id=?",
                         (now + n * 1e-6, c["id"]))
                n += 1
        self.r.wake.set()
        return {"queued": n, "skipped": skipped}

    # ---- machines
    def _machine_ref(self, body):
        """'' / null -> inherit (the headline's machine); else a known machine."""
        v = str(body.get("machine") or "").strip()
        if not v:
            return None
        if not self.r.machines.get(v):
            raise ApiError(400, LOCAL_GONE if v == "local" else f"unknown machine '{v}'")
        return v

    def machines_public(self):
        st = self.r.machines.status
        out = []
        for m in self.r.machines.all():
            d = {k: m[k] for k in ("name", "host", "user", "port", "identity", "workdir", "python", "sync",
                                   "ssh_options", "notes", "source", "env_file", "remote_env_file")}
            d["env_file_info"] = env_file_info(m["env_file_path"])
            d["env_keys"] = sorted(m["env"])
            if m["source"] == "ui":
                d["env"] = m["env_text"]  # typed in the UI, so it may be shown there again
            d["busy"] = self.r.busy.get(m["name"], 0)
            d["status"] = st.get(m["name"], {"state": "unknown"})
            d["used_by"] = self.s.one("SELECT (SELECT COUNT(*) FROM cards WHERE machine=?) + "
                                      "(SELECT COUNT(*) FROM tests WHERE machine=?) n", (m["name"], m["name"]))["n"]
            out.append(d)
        return out

    def _machine_row(self, name):
        name = str(name)
        row = self.s.one("SELECT * FROM machines WHERE name=?", (name,))
        if not row or any(m["name"] == name for m in self.r.groups.machines):
            if self.r.machines.get(name):
                raise ApiError(409, f"'{name}' is defined in {self.r.groups.path.name}; edit it there")
            raise ApiError(404, "no such machine")
        return row

    def machine_create(self, q, body):
        if body.get("env_from"):  # a copy of another machine ("Copy settings from" / Duplicate)
            src = self.r.machines.get(str(body["env_from"]))
            if not src:
                raise ApiError(400, f"can't copy settings from unknown machine '{body['env_from']}'")
            body = dict(body, env=fill_hidden_env(body.get("env"), src["env"]))
        m = normalize_machine(body, body.get("name"), "ui", base_dir=self.r.groups.base_dir())
        if self.r.machines.get(m["name"]):
            raise ApiError(409, f"a machine called '{m['name']}' already exists")
        pos = (self.s.one("SELECT MAX(position) p FROM machines") or {}).get("p") or 0
        now = time.time()
        self.s.x("INSERT INTO machines(name, host, user, port, identity, workdir, python, sync, env, "
                 "ssh_options, notes, env_file, remote_env_file, position, created, updated) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (m["name"], m["host"], m["user"], m["port"], m["identity"], m["workdir"], m["python"],
                  int(m["sync"]), m["env_text"], m["ssh_options"], m["notes"], m["env_file"], m["remote_env_file"],
                  pos + 1, now, now))
        threading.Thread(target=self.r._check_safe, args=(self.r.machines.get(m["name"]),), daemon=True).start()
        return {"ok": True, "name": m["name"]}

    def machine_update(self, q, body, name):
        row = self._machine_row(name)
        merged = dict(row, **{k: v for k, v in body.items() if k in MACHINE_FIELDS})
        new_name = str(body.get("name") or row["name"]).strip()
        m = normalize_machine(merged, new_name, "ui", row["id"], base_dir=self.r.groups.base_dir())
        if new_name != row["name"] and self.r.machines.get(new_name):
            raise ApiError(409, f"a machine called '{new_name}' already exists")
        if new_name != row["name"] and self.r.busy.get(row["name"]):
            raise ApiError(409, "can't rename a machine while it is running tests")
        with self.s.lock:
            self.s.x("UPDATE machines SET name=?, host=?, user=?, port=?, identity=?, workdir=?, python=?, "
                     "sync=?, env=?, ssh_options=?, notes=?, env_file=?, remote_env_file=?, updated=? WHERE id=?",
                     (m["name"], m["host"], m["user"], m["port"], m["identity"], m["workdir"], m["python"],
                      int(m["sync"]), m["env_text"], m["ssh_options"], m["notes"], m["env_file"],
                      m["remote_env_file"], time.time(), row["id"]))
            if new_name != row["name"]:
                for t in ("cards", "tests"):
                    self.s.x(f"UPDATE {t} SET machine=? WHERE machine=?", (new_name, row["name"]))
                self.r.machines.status.pop(row["name"], None)
        threading.Thread(target=self.r._check_safe, args=(self.r.machines.get(new_name),), daemon=True).start()
        self.r.wake.set()  # a changed machine may let queued cards start
        return {"ok": True, "name": new_name}

    def machine_delete(self, q, body, name):
        row = self._machine_row(name)
        if self.r.busy.get(row["name"]):
            raise ApiError(409, "this machine is running tests; stop them first")
        with self.s.lock:
            self.s.x("DELETE FROM machines WHERE id=?", (row["id"],))
            n = 0
            for t in ("cards", "tests"):
                n += self.s.one(f"SELECT COUNT(*) n FROM {t} WHERE machine=?", (row["name"],))["n"]
                self.s.x(f"UPDATE {t} SET machine=NULL WHERE machine=?", (row["name"],))
        self.r.machines.status.pop(row["name"], None)
        return {"ok": True, "reset": n}

    def machine_check(self, q, body, name):
        m = self.r.machines.get(str(name))
        if not m:
            raise ApiError(404, "no such machine")
        return self.r.check_machine(m) or {}

    def machines_check_all(self, q, body):
        ms = self.r.machines.all()
        for m in ms:
            threading.Thread(target=self.r._check_safe, args=(m,), daemon=True).start()
        return {"checking": len(ms)}

    # ---- test library (templates; dragging one onto the board makes a card copy)
    def _test_fields(self, body, partial=False):
        f = self._card_fields({k: v for k, v in body.items() if k in ("target", "args", "env", "timeout")},
                              partial=True)
        if "name" in body:
            f["name"] = str(body["name"] or "").strip()
        if "grp" in body:
            f["grp"] = _grp(body)
        # no "machine": library tests have none of their own (an older page may still send it: ignored)
        if not partial:
            f.setdefault("target", "")
            if not f.get("name"):
                f["name"] = f["target"] or "all tests"
        elif "name" in f and not f["name"]:
            raise ApiError(400, "name can't be empty")
        return f

    def test_create(self, q, body):
        f = self._test_fields(body)
        m = self.s.one("SELECT MAX(position) m FROM tests")
        f["position"] = ((m and m["m"]) or 0) + 1
        f["created"] = f["updated"] = time.time()
        tid = self.s.x(f"INSERT INTO tests({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return self.test_get(q, body, tid)

    def test_bulk(self, q, body):
        grp = _grp(body)
        existing = {r["target"] for r in self.s.q("SELECT target FROM tests WHERE grp IS ?", (grp,))}
        n = 0
        for t in body.get("targets") or []:
            target = shlex.quote(t)
            if target in existing:
                continue  # already under this headline
            self.test_create(q, {"name": _default_name(t), "target": target, "grp": grp})
            existing.add(target)
            n += 1
        return {"created": n, "skipped": len(body.get("targets") or []) - n}

    def test_get(self, q, body, tid):
        t = self.s.one("SELECT * FROM tests WHERE id=?", (tid,))
        if not t:
            raise ApiError(404, "no such test")
        t["runs"] = self.s.q("SELECT * FROM runs WHERE test_id=? ORDER BY id DESC LIMIT 100", (tid,))
        t["copies"] = self.s.q("SELECT id, status, cron, run_at, machine, grp, optset FROM cards WHERE test_id=? ORDER BY id",
                               (tid,))
        for c in t["copies"]:
            c["lane"] = self.r.resolve_machine(c)[0]
        self._describe_run_setup(t)
        return t

    def _describe_run_setup(self, item):
        pv = self.r.preview(item)
        item.update(pv)
        item["cwd"] = pv["where"]
        g = self.r.groups.get(item.get("grp"))
        item["group_env_keys"] = sorted(g["env"]) if g else []
        item["group_known"] = bool(g) or not item.get("grp")

    def test_update(self, q, body, tid):
        f = self._test_fields(body, partial=True)
        if f:
            f["updated"] = time.time()
            self.s.x(f"UPDATE tests SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), tid))
        return self.test_get(q, body, tid)

    def test_delete(self, q, body, tid):
        # copies already on the board stay (they are independent); run history is kept but unlinked
        self.s.x("DELETE FROM tests WHERE id=?", (tid,))
        return {"ok": True}

    def test_copy(self, q, body, tid):
        t = self.s.one("SELECT * FROM tests WHERE id=?", (tid,))
        if not t:
            raise ApiError(404, "no such test")
        status = body.get("status", "backlog")
        if status not in ("backlog", "scheduled", "queued"):
            raise ApiError(409, "copies can go to Backlog, Scheduled or Queued")
        if status == "scheduled":
            validate_schedule(body)  # fail before creating anything
        # dropped into a machine's lane -> the copy runs there; otherwise (Run / Schedule in the test's window) it
        # follows the headline's machine
        machine = None
        if body.get("machine"):
            machine = self._lane_machine(body["machine"], self.r.resolve_machine(t)[0]) or machine
        if status != "backlog" and not self.r.resolve_machine({"machine": machine, "grp": t["grp"]})[0]:
            raise ApiError(409, NO_MACHINE)
        self._check_bound({"machine": machine, "grp": t["grp"]})
        # option sets: one copy each, queued in the order given ({"optsets": ["-x", "-m slow", null]})
        try:
            if "optsets" in body:
                sets = parse_optsets(body["optsets"])
                self.s.x("UPDATE tests SET optsets=? WHERE id=?", (json.dumps(sets) if sets else None, tid))  # next time
                if body.get("combine"):  # one card with all of them ({"combine": true}, what the UI sends)
                    sets = [normalize_optset(" ".join(x for x in sets if x))]
            else:
                sets = [normalize_optset(body.get("optset"))]
        except ValueError as e:
            raise ApiError(400, str(e))
        # extra pytest arguments for every copy ({"extra_args": "--set_temperature 0 10 20"}): they go into each
        # card's own args, after the test's args, and are offered again next time
        args = t["args"]
        if "extra_args" in body:
            extra = str(body.get("extra_args") or "").strip()
            try:
                shlex.split(extra)
            except ValueError as e:
                raise ApiError(400, f"extra arguments: {e}")
            self.s.x("UPDATE tests SET extra_args=? WHERE id=?", (extra or None, tid))
            args = " ".join(x for x in (t["args"], extra) if x)
        move = {k: v for k, v in body.items() if k not in ("machine", "optsets", "optset", "extra_args")}
        made, base = [], time.time()
        with self.s.lock:
            for i, optset in enumerate(sets or [None]):
                card = self.card_create(q, {"title": t["name"], "target": t["target"], "args": args,
                                            "env": t["env"], "timeout": t["timeout"], "grp": t["grp"]})
                self.s.x("UPDATE cards SET test_id=?, machine=?, optset=? WHERE id=?",
                         (tid, machine, optset, card["id"]))
                res = self.card_move(q, move, card["id"])
                if status == "queued":  # keep the list order in the queue
                    self.s.x("UPDATE cards SET queued_at=? WHERE id=?", (base + i * 1e-6, card["id"]))
                made.append(res)
        res = dict(made[-1], created=len(made), cards=[c["id"] for c in made], optsets=sets)
        return res

    def card_to_test(self, q, body, cid):
        c = self.s.one("SELECT * FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        grp = _grp(body) if "grp" in body else c["grp"]
        t = self.test_create(q, {"name": c["title"], "target": c["target"], "args": c["args"],
                                 "env": c["env"], "timeout": c["timeout"], "grp": grp})
        if c.get("optset"):  # pre-ticked next time the test is put on the board
            self.s.x("UPDATE tests SET optsets=? WHERE id=?", (json.dumps([c["optset"]]), t["id"]))
        if grp == c["grp"]:
            # same headline: the card now counts as a copy of the new library test (and brings its history)
            self.s.x("UPDATE cards SET test_id=? WHERE id=?", (t["id"], cid))
            self.s.x("UPDATE runs SET test_id=? WHERE card_id=? AND test_id IS NULL", (t["id"], cid))
        # different headline: the card itself is left untouched (it keeps running with its own headline)
        return t

    def cards_clear(self, q, body):
        """Remove finished cards from the board (their run history stays). Optional filters: "machine" (one row
        of the board: the cards that run there; "" or null = the "No machine" row) and "grp" (one headline; null =
        cards without one)."""
        statuses = [s for s in (body.get("statuses") or ["passed", "failed"]) if s in ("passed", "failed", "backlog")]
        if not statuses:
            raise ApiError(400, "nothing to clear")
        only_machine = "machine" in body
        machine = str(body.get("machine") or "") or None
        only_grp = "grp" in body
        grp = _grp(body) if only_grp else None
        with self.s.lock:
            ids = [c["id"] for c in self.s.q(f"SELECT id, machine, grp FROM cards WHERE status IN "
                                              f"({','.join('?' * len(statuses))})", statuses)
                   if (not only_machine or self.r.resolve_machine(c)[0] == machine)
                   and (not only_grp or c["grp"] == grp)]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.s.x(f"DELETE FROM cards WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        return {"cleared": len(ids)}

    # ---- cards
    def _card_fields(self, body, partial=False):
        out = {}
        for f in ("title", "target", "args", "env"):
            if f in body:
                out[f] = str(body[f] or "").strip() if f != "env" else str(body[f] or "")
        if "timeout" in body:
            out["timeout"] = max(0, int(body["timeout"] or 0))
        if "grp" in body:
            out["grp"] = _grp(body)
        if "machine" in body:
            out["machine"] = self._machine_ref(body)
        if "optset" in body:
            try:
                out["optset"] = normalize_optset(body["optset"])
            except ValueError as e:
                raise ApiError(400, str(e))
        if "branch" in body:
            try:
                out["branch"] = normalize_branch(body["branch"])
            except ValueError as e:
                raise ApiError(400, str(e))
        for f in ("target", "args"):
            if f in out:
                try:
                    shlex.split(out[f])
                except ValueError as e:
                    raise ApiError(400, f"{f}: {e}")
        if not partial:
            if not out.get("target") and not out.get("args"):
                out.setdefault("target", "")
            out.setdefault("title", out.get("target") or "all tests")
        return out

    def _end_pos(self, status):
        m = self.s.one("SELECT MAX(position) m FROM cards WHERE status=?", (status,))
        return (m and m["m"] or 0) + 1

    def card_create(self, q, body):
        f = self._card_fields(body)
        self._check_bound(f)
        if "lane_id" not in f:
            f["lane_id"] = self._default_lane()
        f["position"] = self._end_pos("backlog")
        f["created"] = f["updated"] = time.time()
        cols = ",".join(f)
        cid = self.s.x(f"INSERT INTO cards({cols}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return self.card_get(q, body, cid)

    def card_bulk(self, q, body):
        made = []
        for t in body.get("targets") or []:
            title = t.split("::")[-1] if "::" in t else Path(t).stem
            made.append(self.card_create(q, {"target": shlex.quote(t), "title": title, "grp": body.get("grp"),
                                             "machine": body.get("machine")}))
        return {"created": len(made)}

    def card_get(self, q, body, cid):
        c = self.s.one("SELECT * FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        t = self.s.one("SELECT name FROM tests WHERE id=?", (c["test_id"],)) if c.get("test_id") else None
        c["test_name"] = t and t["name"]
        c["runs"] = self.s.q("SELECT * FROM runs WHERE card_id=? ORDER BY id DESC LIMIT 50", (cid,))
        for r in c["runs"]:  # the screen session a running run can be attached to
            info = self.r._remote_info(r) if r["outcome"] == "running" else None
            r.pop("remote", None)
            if info and info.get("base") is not None:
                r["session"] = info.get("session")
                r["detached"] = True
        if self.r.links.get(cid) and c["status"] == "running":
            c["link"] = self.r.links[cid]
        self._describe_run_setup(c)
        if c["cron"]:
            try:
                cr, t, nxt = Cron(c["cron"]), time.time(), []
                for _ in range(3):
                    t = cr.next_after(t)
                    nxt.append(t)
                c["next_fires"] = nxt
            except ValueError:
                pass
        return c

    def card_update(self, q, body, cid):
        if "optset" in body:
            c = self.s.one("SELECT status, optset FROM cards WHERE id=?", (cid,))
            try:
                new = normalize_optset(body["optset"])
            except ValueError as e:
                raise ApiError(400, str(e))
            if c and c["status"] == "running" and new != c["optset"]:
                raise ApiError(409, "card is running — stop it first, then change its option set")
        if "branch" in body:
            c = self.s.one("SELECT status, branch FROM cards WHERE id=?", (cid,))
            try:
                new = normalize_branch(body["branch"])
            except ValueError as e:
                raise ApiError(400, str(e))
            if c and c["status"] == "running" and new != c["branch"]:
                raise ApiError(409, "card is running — stop it first, then change its branch")
        if "cron" in body or "run_at" in body:  # check before changing anything
            c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
            if not c:
                raise ApiError(404, "no such card")
            if c["status"] != "scheduled":
                raise ApiError(409, "only Scheduled cards have a schedule; move the card to Scheduled instead")
            validate_schedule(body)
        f = self._card_fields(body, partial=True)
        if "machine" in f or "grp" in f:  # a headline with machines = runs its tests only there
            c = self.s.one("SELECT machine, grp FROM cards WHERE id=?", (cid,))
            if c:
                self._check_bound({**c, **f})
        if f:
            f["updated"] = time.time()
            sets = ",".join(f"{k}=?" for k in f)
            self.s.x(f"UPDATE cards SET {sets} WHERE id=?", (*f.values(), cid))
        if "cron" in body or "run_at" in body:
            c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
            if c and c["status"] == "scheduled":
                run_at, cron = validate_schedule(body)
                self.s.x("UPDATE cards SET run_at=?, cron=? WHERE id=?", (run_at, cron, cid))
        return self.card_get(q, body, cid)

    def card_delete(self, q, body, cid):
        self.r.stop(cid)
        self.s.x("DELETE FROM cards WHERE id=?", (cid,))
        return {"ok": True}

    def card_move(self, q, body, cid):
        with self.s.lock:
            c = self.s.one("SELECT * FROM cards WHERE id=?", (cid,))
            if not c:
                raise ApiError(404, "no such card")
            status = body.get("status", c["status"])
            if status not in COLUMNS:
                raise ApiError(400, "unknown column")
            if c["status"] == "running" and status != "running":
                raise ApiError(409, "card is running — stop it first")
            if status in SYSTEM_ONLY and status != c["status"]:
                raise ApiError(409, f"cards move to '{status}' by themselves")
            pos = body.get("position")
            if pos is None:
                pos = self._end_pos(status)
            now = time.time()
            upd = {"status": status, "position": float(pos), "updated": now}
            if "machine" in body:  # dropped into another machine's lane: it runs there from now on
                machine = self._lane_machine(body["machine"], self.r.resolve_machine(c)[0])
                if machine is not None:
                    if c["status"] == "running":
                        raise ApiError(409, "card is running — stop it first")
                    upd["machine"] = machine
            if "optset" in body:  # dropped on an option-set group
                try:
                    optset = normalize_optset(body["optset"])
                except ValueError as e:
                    raise ApiError(400, str(e))
                if optset != c.get("optset"):
                    if c["status"] == "running":
                        raise ApiError(409, "card is running — stop it first")
                    upd["optset"] = optset
            if status in ("scheduled", "queued") and not self.r.resolve_machine({**c, **upd})[0]:
                raise ApiError(409, NO_MACHINE)
            if "machine" in upd or status in ("scheduled", "queued"):
                self._check_bound({**c, **upd})
            if status == "scheduled":
                upd["run_at"], upd["cron"] = validate_schedule(body)
            elif status == "queued" and c["status"] != "queued":
                upd.update(queued_at=now, pending_trigger="manual")
            elif status == "backlog":
                upd.update(run_at=None, cron=None)
            sets = ",".join(f"{k}=?" for k in upd)
            self.s.x(f"UPDATE cards SET {sets} WHERE id=?", (*upd.values(), cid))
        if status == "queued":
            self.r.wake.set()
        return self.card_get(q, body, cid)

    def card_run(self, q, body, cid):
        c = self.s.one("SELECT status, machine, grp FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        if c["status"] in ("running", "queued"):
            raise ApiError(409, f"already {c['status']}")
        if not self.r.resolve_machine(c)[0]:
            raise ApiError(409, NO_MACHINE)
        self._check_bound(c)
        # keeps any cron: after this ad-hoc run the card goes back to Scheduled
        self.s.x("UPDATE cards SET status='queued', queued_at=?, pending_trigger='manual', position=? WHERE id=?",
                 (time.time(), self._end_pos("queued"), cid))
        self.r.wake.set()
        return {"ok": True}

    def card_stop(self, q, body, cid):
        c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
        if c and c["status"] == "queued":
            # same as dragging it to Backlog: it is unscheduled, a repeating card stops repeating
            self.s.x("UPDATE cards SET status='backlog', run_at=NULL, cron=NULL, updated=? WHERE id=?",
                     (time.time(), cid))
            return {"ok": True}
        if not self.r.stop(cid):
            raise ApiError(409, "not running")
        return {"ok": True}

    # ---- runs
    def run_log(self, q, body, rid):
        run = self.s.one("SELECT outcome FROM runs WHERE id=?", (rid,))
        if not run:
            raise ApiError(404, "no such run")
        path = self.r.logs / f"run-{rid}.log"
        offset = int(q.get("offset") or 0)
        data = b""
        if path.exists():
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(2_000_000)
        # don't split a multi-byte UTF-8 character across two polls: hold back an incomplete tail
        # (at most 3 bytes); the next poll starts from it
        text = None
        for cut in range(min(4, len(data) + 1) if run["outcome"] == "running" else 0):
            try:
                text = data[:len(data) - cut].decode("utf-8")
                data = data[:len(data) - cut]
                break
            except UnicodeDecodeError:
                continue
        if text is None:  # finished, or not valid UTF-8 anyway (binary output): show it with replacement characters
            text = data.decode("utf-8", "replace")
        return {"text": text, "offset": offset + len(data),
                "done": run["outcome"] != "running"}

    def run_junit(self, q, body, rid):
        path = self.r.junit / f"run-{rid}.xml"
        if not path.exists():
            raise ApiError(404, "no junit report for this run")
        return ("raw", path.read_bytes(), "application/xml")

    def run_attach(self, q, body, rid):
        run = self.s.one("SELECT * FROM runs WHERE id=?", (rid,))
        if not run:
            raise ApiError(404, "no such run")
        if run["outcome"] != "running":
            raise ApiError(409, "this run is over; its output is in the card")
        info = self.r._remote_info(run)
        if not info or info.get("base") is None:
            raise ApiError(409, "this run doesn't run on its own on a machine, so there is nothing to attach to")
        if not info.get("session"):
            raise ApiError(409, f"screen isn't installed on {run['machine']}, so this run has no session to attach "
                                f"to. Install screen there (e.g. sudo apt install screen); later runs get one.")
        m = self.r.machines.get(run["machine"])
        if not m:
            raise ApiError(404, f"machine '{run['machine']}' is not defined any more")
        ses = shlex.quote(info["session"])
        gone = shlex.quote(f"[pytest-board] screen session {info['session']} isn't there any more: the run has "
                           f"just ended (its output is in the card).")
        sh = self.shells.start(m, body.get("cols"), body.get("rows"),
                               script=f"screen -x {ses} || {{ printf '%s\\n' {gone}; sleep 2; exit 1; }}")
        return {"id": sh.id, "machine": m["name"], "session": info["session"],
                "dest": self.r.dest(m) + (f":{m['port']}" if m["port"] else "")}

    def discover(self, q, body):
        return self.r.discover(q.get("grp") or None, q.get("machine") or None)

    # ---- interactive ssh shell on a machine ("Open shell" in the machine dialog)
    def shell_open(self, q, body, name):
        m = self.r.machines.get(str(name))
        if not m:
            raise ApiError(404, "no such machine (save it first)")
        sh = self.shells.start(m, body.get("cols"), body.get("rows"))
        return {"id": sh.id, "machine": m["name"], "dest": self.r.dest(m) + (f":{m['port']}" if m["port"] else ""),
                "workdir": m["workdir"] or "~"}

    def shell_read(self, q, body, sid):
        return self.shells.read(str(sid), int(q.get("offset") or 0))

    def shell_input(self, q, body, sid):
        self.shells.write(str(sid), body.get("data") or "")
        return {"ok": True}

    def shell_resize(self, q, body, sid):
        self.shells.resize(str(sid), body.get("cols"), body.get("rows"))
        return {"ok": True}

    def shell_close(self, q, body, sid):
        self.shells.close(str(sid))
        return {"ok": True}

    def cron_preview(self, q, body):
        try:
            cr, t, out = Cron(body.get("cron") or ""), time.time(), []
            for _ in range(5):
                t = cr.next_after(t)
                out.append(t)
            return {"ok": True, "next": out}
        except ValueError as e:
            return {"ok": False, "error": str(e)}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pytest-board/1.0"

        def log_message(self, fmt, *args):
            if not self.path.startswith("/api/board"):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _authed(self):
            if not app.token:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    if hmac.compare_digest(pw, app.token):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="pytest-board"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _cross_site(self, method):
            """
            Refuse requests another website makes the browser send (CSRF), and requests whose Host header
            doesn't fit a loopback-only bind (DNS rebinding). Returns an error message, or None when fine.
            Clients like curl send no Origin / Sec-Fetch-Site headers and are not affected.
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if app.loopback_only:
                hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
                if hostname not in ("localhost", "127.0.0.1", "[::1]", "::1", ""):
                    return f"unexpected Host header {host!r} (server is bound to localhost)"
            if method == "GET":
                return None
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site in ("cross-site", "same-site"):
                return "cross-site request refused"
            origin = self.headers.get("Origin")
            if origin is not None:
                o = urlparse(origin)
                if origin == "null" or not o.netloc or o.netloc.lower() != host:
                    return "cross-origin request refused"
            return None

        def _dispatch(self, method):
            bad = self._cross_site(method)
            if bad:
                return self._send(403, {"error": bad})
            if not self._authed():
                return
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
            for m, rx, fn in app.routes:
                mt = rx.match(url.path)
                if mt and m == method:
                    try:
                        res = fn(q, body, *(int(g) if g.isdigit() else g for g in mt.groups()))
                        if isinstance(res, tuple) and res[0] == "raw":
                            return self._send(200, res[1], res[2])
                        return self._send(200, res)
                    except ApiError as e:
                        return self._send(e.status, {"error": str(e)})
                    except (ValueError, TypeError, KeyError) as e:
                        return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Drag & drop scheduler board for pytest runs")
    ap.add_argument("--root", default=".", help="the project folder: where pytest-board.ini and .pytest-board/ are by "
                    "default, and what machines with sync = true get a copy of (default: cwd)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address; use 0.0.0.0 to reach it from other machines")
    ap.add_argument("--port", type=int, default=8765)
    # no longer used: tests run on the machines (each has its own pytest command). Still accepted, so
    # service files that pass them keep working
    ap.add_argument("--workers", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--pytest", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--data", default=None, help="where the database and logs live (default: <root>/.pytest-board)")
    ap.add_argument("--token", default=os.environ.get("PYTEST_BOARD_TOKEN"),
                    help="require this password (HTTP basic auth, any username). Also read from PYTEST_BOARD_TOKEN")
    ap.add_argument("--groups", default=None,
                    help="INI file defining the library headlines, one [SECTION] each "
                         "(default: <root>/pytest-board.ini). Re-read automatically when it changes.")
    ap.add_argument("--ssh", default="ssh", help='ssh client used to reach machines (default: "ssh")')
    ap.add_argument("--rsync", default="rsync", help='rsync used by machines with sync=true (default: "rsync")')
    ap.add_argument("--check-interval", type=int, default=120,
                    help="seconds between automatic machine health checks (0 = only when asked)")
    ap.add_argument("--env-file", default=None,
                    help="a .env file (export NAME=value lines) applied to every run "
                         "(default: .env next to the headline config file)")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # a Windows console or log file may not take every character
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    for flag, v in (("--workers", a.workers), ("--pytest", a.pytest)):
        if v is not None:
            print(f"note: {flag} is ignored: the board server doesn't run tests itself any more (each machine has its "
                  "own pytest command and runs one test at a time)", file=sys.stderr)
    root = Path(a.root).resolve()
    data = Path(a.data).resolve() if a.data else root / ".pytest-board"
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "board.db")
    if not store.one("SELECT 1 FROM lanes"):  # internal; the board's swimlanes are the machines
        store.x("INSERT INTO lanes(name, position) VALUES('board', 1)")
    groups = Groups(Path(a.groups).resolve() if a.groups else root / "pytest-board.ini", root)
    env_file = Path(a.env_file).expanduser().resolve() if a.env_file else groups.base_dir() / ".env"
    runner = Runner(store, root, data, groups,
                    Machines(store, groups), split_local(a.ssh), split_local(a.rsync), a.check_interval, env_file)
    app = App(store, runner, a.token)
    app.loopback_only = a.host in ("127.0.0.1", "localhost", "::1")
    app.reload_groups()  # load headlines before the first run can start
    if not groups.path.exists():
        print(f"no headline file at {groups.path} — library shows a single list (see pytest-board.example.ini)",
              file=sys.stderr)
    threading.Thread(target=app.watch_groups, name="headlines", daemon=True).start()
    runner.start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    srv.daemon_threads = True
    if a.host not in ("127.0.0.1", "localhost") and not a.token:
        print("WARNING: listening on a network interface without --token; anyone who can reach this port "
              "can run commands as this user.", file=sys.stderr)
    ef = env_file_info(env_file)
    if ef["exists"]:
        print(f"env file {env_file}: {len(ef['keys'])} variable(s) for every run" + (f" — {ef['error']}" if ef["error"]
                                                                                    else ""), file=sys.stderr)
    elif a.env_file:
        print(f"WARNING: --env-file {env_file} not found", file=sys.stderr)
    print(f"pytest-board on http://{a.host}:{a.port}  root={root}  data={data}")
    def _term(*_):
        runner.stopping = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)  # systemctl stop -> clean shutdown
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stopping = True  # runs on machines go on there; they're followed again after the restart
        app.shells.close_all()
        for p in list(runner.procs.values()):  # don't leave orphaned ssh / sync processes behind
            kill_tree(p, grace=2)


if __name__ == "__main__":
    main()
