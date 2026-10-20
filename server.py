#!/usr/bin/env python3
"""
pytest-board — a Jira-style drag & drop board for scheduling pytest runs.

Pure Python standard library (3.8+). No pip installs needed except pytest itself
in whatever environment runs your tests.

    python3 server.py --root /path/to/your/project --host 0.0.0.0 --port 8765 --token s3cret

Columns are run states (Backlog, Scheduled, Queued, Running, Passed, Failed);
swimlanes are the machines the tests run on (this server, plus each ssh machine).
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
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
try:
    SERVICE_USER = getpass.getuser()
except Exception:  # no passwd entry / login name (unusual containers)
    SERVICE_USER = str(os.getuid()) if hasattr(os, "getuid") else ""
STATIC = HERE / "static"
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
  created REAL, updated REAL, grp TEXT, machine TEXT, optsets TEXT);
CREATE TABLE IF NOT EXISTS seeded(grp TEXT NOT NULL, spec TEXT NOT NULL, PRIMARY KEY(grp, spec));
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL, user TEXT NOT NULL DEFAULT '',
  port INTEGER, identity TEXT NOT NULL DEFAULT '', workdir TEXT NOT NULL DEFAULT '',
  python TEXT NOT NULL DEFAULT 'python3 -m pytest', slots INTEGER NOT NULL DEFAULT 1, sync INTEGER NOT NULL DEFAULT 0,
  env TEXT NOT NULL DEFAULT '', ssh_options TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  position REAL NOT NULL DEFAULT 0, created REAL, updated REAL);
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
  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL, grp TEXT, machine TEXT, optset TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY,
  card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL,
  card_title TEXT, lane_name TEXT,
  trigger TEXT, command TEXT, started REAL, finished REAL, exit_code INTEGER,
  outcome TEXT, total INTEGER, passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER, machine TEXT,
  optset TEXT);
"""
RUN_COLS_V1 = ("id, card_id, trigger, command, started, finished, exit_code, outcome, "
               "total, passed, failed, errors, skipped")
INDEXES = """
CREATE INDEX IF NOT EXISTS runs_card ON runs(card_id, id);
CREATE INDEX IF NOT EXISTS runs_test ON runs(test_id, id);
CREATE INDEX IF NOT EXISTS cards_test ON cards(test_id);
CREATE INDEX IF NOT EXISTS tests_grp ON tests(grp, position);
"""


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
        args = -ra                      ; optional: pytest args added before each test's own args
        env =                           ; optional: KEY=value lines, a test's own env overrides them
            BASE_URL=https://system.staging
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
                try:
                    machines.append(normalize_machine(dict(s), key.split(":", 1)[1].strip(), "config"))
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
                "args": s.get("args", "").strip(),
                "env": Runner.parse_env(s.get("env", "")),
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
            try:
                shlex.split(g["args"])
            except ValueError as e:
                problems.append(f"[{key}] args: {e}")
                g["args"] = ""
            if g["dir"] and not self.cwd(g).is_dir():
                problems.append(f"[{key}] dir not found: {g['dir']}")
            items.append(g)
        names = {m["name"] for m in machines}
        for g in items:
            if g["machine"] and g["machine"] != "local" and g["machine"] not in names:
                problems.append(f"[{g['key']}] machine '{g['machine']}' has no [machine:{g['machine']}] section "
                                "(fine if it was added in the UI)")
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

    def cwd(self, g):
        return (self.root / g["dir"]).resolve() if g and g.get("dir") else self.root

    def public(self):
        """What the UI may see: env values are left out (they may hold secrets), only their names."""
        with self.lock:
            return [{"key": g["key"], "title": g["title"], "color": g["color"], "dir": g["dir"], "args": g["args"],
                     "machine": g["machine"], "env_keys": sorted(g["env"]), "seed_count": len(g["tests"]),
                     "options": g["options"]}
                    for g in self.items]


# --------------------------------------------------------------------------- machines (SSH execution hosts)
MACHINE_NAME_RX = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
ENV_KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_WORKDIR = "~/git"  # project folder on a machine when none is given ("~" = the home folder)
MACHINE_FIELDS = ("host", "user", "port", "identity", "workdir", "python", "slots", "sync", "env", "ssh_options",
                  "notes")


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def normalize_machine(d, name, source, mid=None):
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
    slots = int(str(d.get("slots") or "1").strip() or 1)
    if not 1 <= slots <= 64:
        raise ValueError("slots must be 1-64")
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
    return {"name": name, "host": host, "user": user, "port": port, "identity": str(d.get("identity") or "").strip(),
            "workdir": workdir, "python": python, "slots": slots, "sync": sync, "env": env, "env_text": env_text,
            "ssh_options": ssh_options, "notes": str(d.get("notes") or "").strip(), "source": source, "id": mid}


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
                ui.append(normalize_machine(r, r["name"], "ui", r["id"]))
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
        for table, col in (("cards", "optset"), ("runs", "optset"), ("tests", "optsets")):  # v6: option sets
            if col not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
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
class Runner:
    def __init__(self, store, root, pytest_cmd, workers, data_dir, groups=None, machines=None,
                 ssh_cmd=("ssh",), rsync_cmd=("rsync",), check_interval=120):
        self.store = store
        self.root = root
        self.groups = groups or Groups(None, root)
        self.machines = machines or Machines(store, self.groups)
        self.pytest_cmd = pytest_cmd
        self.workers = workers          # parallel runs on this server ("local")
        self.ssh_cmd = list(ssh_cmd)
        self.rsync_cmd = list(rsync_cmd)
        self.check_interval = check_interval
        self.logs = data_dir / "logs"
        self.junit = data_dir / "junit"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.junit.mkdir(parents=True, exist_ok=True)
        self.procs = {}   # card_id -> Popen (local pytest, or the ssh client for remote runs)
        self.remote = {}  # card_id -> (machine, remote pid file) while a remote run is active
        self.busy = {}    # machine name -> runs in progress
        self.active = set()  # card ids from dispatch until the run is recorded (covers sync / ssh start-up)
        self.cancelled = set()
        self.checking = set()
        self.checking_lock = threading.Lock()
        self.wake = threading.Event()

    # --- lifecycle
    def start(self):
        s = self.store
        # anything "running" when we were last stopped was interrupted
        s.x("UPDATE runs SET outcome='interrupted', finished=? WHERE outcome='running'", (time.time(),))
        s.x("UPDATE cards SET status='failed' WHERE status='running'")
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

    # --- which machine runs a card: the card's own choice > its headline's machine > this server
    def resolve_machine(self, item):
        if item.get("machine"):
            return item["machine"], "card"
        g = self.groups.get(item.get("grp"))
        if g and g.get("machine"):
            return g["machine"], "headline"
        return "local", "default"

    def capacity(self, name):
        if name == "local":
            return self.workers
        m = self.machines.get(name)
        return m["slots"] if m else 1  # unknown machine: let one through so it fails with a clear message

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

    def _try_start(self):
        """Start the oldest queued card whose machine has a free slot."""
        s = self.store
        with s.lock:
            full = set()
            for c in s.q("SELECT * FROM cards WHERE status='queued' ORDER BY queued_at, position, id"):
                mname, _ = self.resolve_machine(c)
                if mname in full:
                    continue
                if self.busy.get(mname, 0) >= self.capacity(mname):
                    full.add(mname)
                    continue
                now = time.time()
                run_id = s.x("INSERT INTO runs(card_id, test_id, card_title, lane_name, trigger, started, outcome, "
                             "machine, optset) VALUES(?,?,?,?,?,?, 'running', ?, ?)",
                             (c["id"], c["test_id"], c["title"], mname, c["pending_trigger"] or "manual",
                              now, mname, c.get("optset")))
                s.x("UPDATE cards SET status='running', last_run_id=?, updated=? WHERE id=?", (run_id, now, c["id"]))
                self.busy[mname] = self.busy.get(mname, 0) + 1
                self.active.add(c["id"])
                threading.Thread(target=self._job, args=(c, run_id, mname), daemon=True).start()
                return True
        return False

    def _job(self, card, run_id, mname):
        try:
            self._execute(card, run_id, mname)
        except Exception as e:
            print("run error:", e, file=sys.stderr)
            self.store.x("UPDATE runs SET outcome='error', finished=? WHERE id=?", (time.time(), run_id))
            self.store.x("UPDATE cards SET status='failed' WHERE id=? AND status='running'", (card["id"],))
        finally:
            with self.store.lock:
                self.active.discard(card["id"])
                self.cancelled.discard(card["id"])
                self.busy[mname] = max(0, self.busy.get(mname, 1) - 1)
                self.store.version += 1
            self.wake.set()

    # --- execution
    def build_command(self, card, junit_path=None):
        g = self.groups.get(card.get("grp"))
        cmd = (list(self.pytest_cmd) + shlex.split(g["args"] if g else "")
               + shlex.split(card.get("target") or "") + shlex.split(card.get("args") or ""))
        cmd += shlex.split(card.get("optset") or "")  # the card's option set
        if junit_path:
            cmd.append(f"--junitxml={junit_path}")
        return cmd

    def cwd_for(self, item):
        return self.groups.cwd(self.groups.get(item.get("grp")))

    def env_for(self, item, run_id=None):
        g = self.groups.get(item.get("grp"))
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        env.pop("PYTEST_BOARD_TOKEN", None)  # the board's password is not the tests' business
        if run_id is not None:
            env["PYTEST_BOARD_RUN_ID"] = str(run_id)
        if item.get("grp"):
            env["PYTEST_BOARD_GROUP"] = item["grp"]
        env.update(g["env"] if g else {})          # headline env from the config file
        env.update(self.parse_env(item.get("env")))  # the test's own env wins
        return env

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

    def _execute(self, card, run_id, mname="local"):
        s = self.store
        cid = card["id"]
        log_path = self.logs / f"run-{run_id}.log"
        junit_path = self.junit / f"run-{run_id}.xml"
        remote = mname != "local"
        # the timeout covers the whole run, including a project sync before it
        deadline = time.time() + card["timeout"] if card["timeout"] else None
        with open(log_path, "wb") as log:
            if card.get("grp"):
                log.write(f"[pytest-board] headline {card['grp']}\n".encode())
            if card.get("optset"):
                log.write(f"[pytest-board] option set: {card['optset']}\n".encode())
            if cid in self.cancelled:
                rc, timed_out = -1, False  # stopped before it got going
            elif not remote:
                rc, timed_out = self._run_local(card, run_id, log, junit_path, deadline)
            else:
                m = self.machines.get(mname)
                if m:
                    rc, timed_out = self._run_remote(card, run_id, m, log, junit_path, deadline)
                else:
                    log.write(f"[pytest-board] machine '{mname}' is not defined any more "
                              "(removed from the config file or the UI). Pick another machine for this card.\n"
                              .encode())
                    rc, timed_out = self.RC_NO_MACHINE, False
            finished = time.time()
            counts = self._parse_junit(junit_path)
            cancelled = cid in self.cancelled
            self.cancelled.discard(cid)
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
            elif remote and rc == self.RC_SSH:
                outcome = "unreachable"
            elif remote and rc == self.RC_NO_WORKDIR:
                outcome = "no-workdir"
            elif remote and rc == self.RC_SYNC_FAILED:
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
            log.write(f"\n[pytest-board] exit code {rc} · {outcome}"
                      f"{' · on ' + mname if remote else ''}\n".encode())

        s.x("""UPDATE runs SET finished=?, exit_code=?, outcome=?, total=?, passed=?, failed=?, errors=?, skipped=?
               WHERE id=?""",
            (finished, rc, outcome, counts.get("total"), counts.get("passed"), counts.get("failed"),
             counts.get("errors"), counts.get("skipped"), run_id))

        with s.lock:
            now_card = s.one("SELECT * FROM cards WHERE id=?", (cid,))
            if not now_card:
                return  # deleted while running
            if cancelled:
                s.x("UPDATE cards SET status='backlog', run_at=NULL, updated=? WHERE id=?", (finished, cid))
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

    def _run_local(self, card, run_id, log, junit_path, deadline=None):
        cmd = self.build_command(card, junit_path)
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        self.store.x("UPDATE runs SET command=? WHERE id=?", (cmd_str, run_id))
        cwd = self.cwd_for(card)
        log.write(f"$ cd {cwd}\n$ {cmd_str}\n\n".encode())
        log.flush()
        try:
            p = subprocess.Popen(cmd, cwd=str(cwd), env=self.env_for(card, run_id), stdout=log,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            log.write(f"failed to start: {e}\n".encode())
            return -1, False
        return self._wait(card, p, deadline)

    # --- SSH
    def ssh_base(self, m, interactive=False):
        """
        ssh client arguments for a machine. interactive=True is for a person at a terminal (Open shell):
        a remote tty, and ssh may ask for a password or passphrase there instead of failing.
        """
        # user options go first: for ssh the first value given for an option wins
        cmd = list(self.ssh_cmd) + shlex.split(m["ssh_options"] or "")
        cmd += ["-tt"] if interactive else ["-T", "-o", "BatchMode=yes"]
        cmd += ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=accept-new"]
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

    def remote_env(self, item, m, g, run_id=None):
        env = {"PYTHONUNBUFFERED": "1", "PYTEST_BOARD_MACHINE": m["name"]}
        if run_id is not None:
            env["PYTEST_BOARD_RUN_ID"] = str(run_id)
        if item.get("grp"):
            env["PYTEST_BOARD_GROUP"] = item["grp"]
        env.update(m["env"])                         # machine env
        env.update(g["env"] if g else {})            # headline env
        env.update(self.parse_env(item.get("env")))  # the test's own env wins
        return {k: v for k, v in env.items() if ENV_KEY_RX.match(k)}

    def _run_remote(self, card, run_id, m, log, junit_path, deadline=None):
        g = self.groups.get(card.get("grp"))
        base, dest = self.ssh_base(m), self.dest(m)
        wd = self.remote_dir(m, g)
        tag = f"{os.getpid()}-{run_id}"
        junit_r, pid_r = f"/tmp/pytest-board-{tag}.xml", f"/tmp/pytest-board-{tag}.pid"
        argv = self.remote_argv(card, m, g) + [f"--junitxml={junit_r}"]
        env = self.remote_env(card, m, g, run_id)
        shown = " ".join(shlex.quote(a) for a in argv)
        self.store.x("UPDATE runs SET command=? WHERE id=?", (f"ssh {dest} · cd {wd} && {shown}", run_id))
        log.write(f"[pytest-board] machine {m['name']} ({dest}{':' + str(m['port']) if m['port'] else ''})\n"
                  f"[pytest-board] env: {', '.join(sorted(env))}\n$ ssh {dest}\n$ cd {wd}\n$ {shown}\n\n".encode())
        log.flush()
        if m["sync"]:
            rc, timed_out = self._sync(m, log, card, deadline)
            if timed_out:
                log.write(b"[pytest-board] timed out during the project sync; tests were not started\n")
                return rc, True
            if card["id"] in self.cancelled:
                return rc, False
            if rc != 0:
                log.write(f"[pytest-board] project sync failed (exit {rc}); tests were not started\n".encode())
                return (self.RC_SSH if rc == 255 else self.RC_SYNC_FAILED), False
        # The script travels over stdin, so env values never show up in a process list.
        script = "".join([
            f"echo $$ > {shlex.quote(pid_r)}\n",
            *(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()),
            f"cd {self._rpath(wd)} 2>/dev/null || {{ printf '[pytest-board] remote folder not found: %s\\n' "
            f"{shlex.quote(wd)} >&2; exit {self.RC_NO_WORKDIR}; }}\n",
            "exec " + " ".join(self._rword(a) for a in argv) + "\n",
        ])
        try:
            p = subprocess.Popen(base + [dest, "sh -s"], stdin=subprocess.PIPE, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True, cwd=str(self.root))
        except OSError as e:
            log.write(f"failed to start ssh ({self.ssh_cmd[0]}): {e}\n".encode())
            return self.RC_SSH, False
        self.remote[card["id"]] = (m, pid_r)
        try:
            p.stdin.write(script.encode())
            p.stdin.close()
        except BrokenPipeError:
            pass
        try:
            rc, timed_out = self._wait(card, p, deadline)
        finally:
            self.remote.pop(card["id"], None)
        if rc != self.RC_SSH:  # fetch the junit report and tidy up on the machine
            try:
                r = subprocess.run(base + [dest, f"cat {shlex.quote(junit_r)} 2>/dev/null; "
                                                 f"rm -f {shlex.quote(junit_r)} {shlex.quote(pid_r)}"],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
                if r.stdout.strip():
                    junit_path.write_bytes(r.stdout)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.write(f"[pytest-board] could not fetch the junit report: {e}\n".encode())
        return rc, timed_out

    def _sync(self, m, log, card, deadline=None):
        """
        Copy the project (--root) to the machine's workdir with rsync before the run.
        Returns (exit code, timed_out). Stop and the card's timeout interrupt it.
        """
        base, dest = self.ssh_base(m), self.dest(m)
        try:
            mk = subprocess.Popen(base + [dest, f"mkdir -p {self._rpath(m['workdir'])}"], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc, timed_out = self._wait(card, mk, deadline if deadline is not None else time.time() + 60)
            finally:
                self.procs.pop(card["id"], None)
            if timed_out or rc != 0 or card["id"] in self.cancelled:
                return (rc or 1), (timed_out and deadline is not None and time.time() >= deadline)
            cmd = list(self.rsync_cmd) + [
                "-az", "--exclude=.pytest-board", "--exclude=.git", "--exclude=__pycache__",
                "--exclude=.pytest_cache", "-e", " ".join(shlex.quote(x) for x in base),
                f"{self.root}/", f"{dest}:{m['workdir'].rstrip('/')}/"]
            log.write(f"$ rsync -az {self.root}/ {dest}:{m['workdir'].rstrip('/')}/\n".encode())
            log.flush()
            limit = time.time() + 1800
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            rc, timed_out = self._wait(card, p, limit if deadline is None else min(deadline, limit))
            if timed_out and (deadline is None or time.time() < deadline):
                log.write(b"sync error: rsync took longer than 30 minutes\n")
                return 1, False
            return rc, timed_out
        except OSError as e:
            log.write(f"sync error: {e}\n".encode())
            return 1, False

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
        self.machines.set_status(name, state="checking")
        try:
            wd = m["workdir"] or "~"
            py = " ".join(self._rword(a) for a in shlex.split(m["python"]))
            script = (f"echo PB_OK; uname -srm; cd {self._rpath(wd)} 2>/dev/null && echo PB_DIR_OK "
                      f"|| echo PB_DIR_MISSING; {py} --version 2>&1 | tail -n 8\n")  # tail: wrappers like poetry may warn first
            try:
                r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script, capture_output=True,
                                   text=True, timeout=40)
                out, err, rc = r.stdout, r.stderr, r.returncode
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
                    st = {"state": "online", "detail": ver + note, "system": system}
            self.machines.set_status(name, checked=time.time(), **st)
            return self.machines.status[name]
        finally:
            with self.checking_lock:
                self.checking.discard(name)

    def _health(self):
        time.sleep(1)
        while True:
            if self.check_interval > 0:
                for m in self.machines.all():
                    threading.Thread(target=self._check_safe, args=(m,), daemon=True).start()
                time.sleep(self.check_interval)
            else:
                time.sleep(30)

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
        try:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _terminate(self, card_id, p):
        """Stop a run: on a machine, kill the remote pytest first, then the local process (group)."""
        r = self.remote.get(card_id)
        if r:
            m, pid_r = r
            pf = shlex.quote(pid_r)
            kill = (f'P=$(cat {pf} 2>/dev/null); [ -n "$P" ] || exit 0; kill -TERM "$P" 2>/dev/null; i=0; '
                    f'while [ $i -lt 5 ] && kill -0 "$P" 2>/dev/null; do sleep 1; i=$((i+1)); done; '
                    f'kill -KILL "$P" 2>/dev/null; true')
            try:
                subprocess.run(self.ssh_base(m) + [self.dest(m), kill], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._kill(p)

    def stop(self, card_id):
        with self.store.lock:
            if card_id not in self.active:
                return False
            self.cancelled.add(card_id)  # also seen by a run that hasn't started its process yet
            p = self.procs.get(card_id)
        if p:
            threading.Thread(target=self._terminate, args=(card_id, p), daemon=True).start()
        return True

    def preview(self, item):
        """Where and how a card/test would run (for the UI)."""
        mname, source = self.resolve_machine(item)
        g = self.groups.get(item.get("grp"))
        if mname == "local":
            return {"run_machine": "local", "run_machine_source": source, "where": str(self.cwd_for(item)),
                    "command_preview": " ".join(shlex.quote(x) for x in self.build_command(item))}
        m = self.machines.get(mname)
        if not m:
            return {"run_machine": mname, "run_machine_source": source, "machine_missing": True, "where": "?",
                    "command_preview": f"(machine '{mname}' is not defined)"}
        return {"run_machine": mname, "run_machine_source": source, "ssh": self.dest(m),
                "where": f"{self.dest(m)}{':' + str(m['port']) if m['port'] else ''}:{self.remote_dir(m, g)}",
                "command_preview": " ".join(shlex.quote(a) for a in self.remote_argv(item, m, g)),
                "machine_env_keys": sorted(m["env"])}

    def discover(self, grp=None):
        item = {"grp": grp}
        g = self.groups.get(grp)
        cmd = (list(self.pytest_cmd) + shlex.split(g["args"] if g else "")
               + ["--collect-only", "-q", "-p", "no:cacheprovider"])
        cwd = self.cwd_for(item)
        try:
            p = subprocess.run(cmd, cwd=str(cwd), env=self.env_for(item), capture_output=True, text=True,
                               timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ids": [], "error": str(e)}
        ids = [ln.strip() for ln in p.stdout.splitlines() if "::" in ln and not ln.startswith((" ", "="))]
        err = None
        if p.returncode not in (0, 5):
            err = (p.stdout[-4000:] + "\n" + p.stderr[-4000:]).strip()
        return {"ids": ids, "error": err, "command": " ".join(shlex.quote(c) for c in cmd), "cwd": str(cwd),
                "grp": grp}


# --------------------------------------------------------------------------- interactive shells (Open shell)
# Becomes the session leader's controlling terminal, then execs ssh. Done in a fresh process instead of a
# preexec_fn, which isn't safe in this multi-threaded server.
# Signals ignored by whoever started the board (nohup, `&` in a script) are reset, so Ctrl+C works.
_CTTY_EXEC = ("import fcntl, os, signal, sys, termios\n"
              "for n in ('SIGINT', 'SIGQUIT', 'SIGTSTP', 'SIGTTIN', 'SIGTTOU', 'SIGPIPE', 'SIGHUP'):\n"
              "    signal.signal(getattr(signal, n), signal.SIG_DFL)\n"
              "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
              "os.execvp(sys.argv[1], sys.argv[1:])")


class Shell:
    def __init__(self, sid, machine, proc, fd):
        self.id, self.machine, self.proc, self.fd = sid, machine, proc, fd
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

    def start(self, m, cols, rows):
        import secrets
        with self.lock:
            if sum(1 for x in self.items.values() if not x.closed) >= self.MAX:
                raise ApiError(429, f"{self.MAX} shells are open already; close one first")
        wd = m["workdir"] or "~"
        shown = "~/" + shlex.quote(wd[2:]) if wd.startswith("~/") and shlex.quote(wd[2:]) != wd[2:] else (
            wd if wd.startswith("~") else shlex.quote(wd))
        hint = (f"[pytest-board] {wd} does not exist on {m['name']} yet, so you are in your home folder. "
                f"Create it with:  mkdir -p {shown}")
        script = (f"cd {Runner._rpath(wd)} 2>/dev/null || echo {shlex.quote(hint)}; "
                  'exec "${SHELL:-/bin/sh}" -l')
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
        try:
            os.close(sh.fd)
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
                n = os.write(sh.fd, data)
                data = data[n:]
        except OSError as e:
            raise ApiError(409, f"the shell has ended ({e})")

    def resize(self, sid, cols, rows):
        sh = self.get(sid)
        if not sh.closed:
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
        for sig, wait in ((signal.SIGHUP, 3), (signal.SIGKILL, 0)):
            try:
                os.killpg(sh.proc.pid, sig)
            except ProcessLookupError:
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
                    os.killpg(sh.proc.pid, signal.SIGHUP)
                except ProcessLookupError:
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
        R("GET", r"/api/discover", self.discover)
        R("POST", r"/api/machines", self.machine_create)
        R("POST", r"/api/machines/check-all", self.machines_check_all)
        R("PATCH", r"/api/machines/([A-Za-z0-9_.\-]+)", self.machine_update)
        R("DELETE", r"/api/machines/([A-Za-z0-9_.\-]+)", self.machine_delete)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/check", self.machine_check)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/run", self.machine_run)  # "Run lane"; 'local' = this server
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
                    if machine and machine != "local" and not MACHINE_NAME_RX.match(machine):
                        print(f"[{g['key']}] test {spec!r}: invalid machine name {machine!r}", file=sys.stderr)
                        continue
                    if not self.s.one("SELECT 1 FROM tests WHERE grp=? AND target=? AND args=?",
                                      (g["key"], target, args)):
                        try:
                            t = self.test_create({}, {"name": name or _default_name(target), "target": target,
                                                      "args": args, "grp": g["key"]})
                        except ApiError as e:
                            print(f"[{g['key']}] test {spec!r}: {e}", file=sys.stderr)
                            continue
                        if machine:
                            # stored as written, even if that machine isn't defined (yet): the UI flags it
                            self.s.x("UPDATE tests SET machine=? WHERE id=?", (machine, t["id"]))
                            if machine != "local" and not self.r.machines.get(machine):
                                print(f"[{g['key']}] test {spec!r}: machine '{machine}' is not defined yet",
                                      file=sys.stderr)
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
            return {"version": self.s.version, "unchanged": True}
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
                "error": self.r.groups.error,
            },
            "server": {
                "root": str(self.r.root),
                "workers": self.r.workers,
                "running": len(self.r.procs),
                "pytest": " ".join(self.r.pytest_cmd),
                "now": time.time(),
                "tz": time.strftime("%Z"),
                "user": SERVICE_USER,  # ssh to machines runs as this account (shown in "Help setup")
                "hostname": socket.gethostname(),
            },
        }

    # ---- swimlanes: one per machine ('local' = this server). A card's lane is the machine it runs on.
    def _default_lane(self):
        """cards.lane_id is internal now (lanes are machines); every card sits in one hidden lane row."""
        row = self.s.one("SELECT id FROM lanes ORDER BY position, id LIMIT 1")
        return row["id"] if row else self.s.x("INSERT INTO lanes(name, position) VALUES('board', 1)")

    def _lane_machine(self, value, current=None):
        """Validate a lane (machine) a card is dropped into. Returns the value to store in cards.machine."""
        v = str(value or "").strip() or "local"
        if v == current:
            return None  # already runs there (maybe via its headline): keep it as it is
        if v != "local" and not self.r.machines.get(v):
            raise ApiError(400, f"unknown machine '{v}'")
        return v

    def machine_run(self, q, body, name):
        """Queue every card in a machine's lane (optionally only one headline's: {"grp": ...})."""
        name = str(name)
        if name != "local" and not self.r.machines.get(name) and not any(
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
        n = 0
        with self.s.lock:
            for c in self.s.q("SELECT id, machine, grp, optset, status FROM cards WHERE status NOT IN ('running','queued') "
                              "ORDER BY position, id"):
                if self.r.resolve_machine(c)[0] != name or (only_grp and c["grp"] != grp) or (
                        only_set and c["optset"] != optset) or (statuses is not None and c["status"] not in statuses):
                    continue
                self.s.x("UPDATE cards SET status='queued', queued_at=?, pending_trigger='manual' WHERE id=?",
                         (now + n * 1e-6, c["id"]))
                n += 1
        self.r.wake.set()
        return {"queued": n}

    # ---- machines
    def _machine_ref(self, body):
        """'' / null -> inherit (headline, then this server); 'local' -> this server; else a known machine."""
        v = str(body.get("machine") or "").strip()
        if not v:
            return None
        if v != "local" and not self.r.machines.get(v):
            raise ApiError(400, f"unknown machine '{v}'")
        return v

    def machines_public(self):
        st = self.r.machines.status
        out = [{"name": "local", "host": socket.gethostname(), "slots": self.r.workers,
                "busy": self.r.busy.get("local", 0), "source": "local",
                "status": {"state": "online", "detail": "this server"}}]
        for m in self.r.machines.all():
            d = {k: m[k] for k in ("name", "host", "user", "port", "identity", "workdir", "python", "slots", "sync",
                                   "ssh_options", "notes", "source")}
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
        m = normalize_machine(body, body.get("name"), "ui")
        if self.r.machines.get(m["name"]):
            raise ApiError(409, f"a machine called '{m['name']}' already exists")
        pos = (self.s.one("SELECT MAX(position) p FROM machines") or {}).get("p") or 0
        now = time.time()
        self.s.x("INSERT INTO machines(name, host, user, port, identity, workdir, python, slots, sync, env, "
                 "ssh_options, notes, position, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (m["name"], m["host"], m["user"], m["port"], m["identity"], m["workdir"], m["python"], m["slots"],
                  int(m["sync"]), m["env_text"], m["ssh_options"], m["notes"], pos + 1, now, now))
        threading.Thread(target=self.r._check_safe, args=(self.r.machines.get(m["name"]),), daemon=True).start()
        return {"ok": True, "name": m["name"]}

    def machine_update(self, q, body, name):
        row = self._machine_row(name)
        merged = dict(row, **{k: v for k, v in body.items() if k in MACHINE_FIELDS})
        new_name = str(body.get("name") or row["name"]).strip()
        m = normalize_machine(merged, new_name, "ui", row["id"])
        if new_name != row["name"] and self.r.machines.get(new_name):
            raise ApiError(409, f"a machine called '{new_name}' already exists")
        if new_name != row["name"] and self.r.busy.get(row["name"]):
            raise ApiError(409, "can't rename a machine while it is running tests")
        with self.s.lock:
            self.s.x("UPDATE machines SET name=?, host=?, user=?, port=?, identity=?, workdir=?, python=?, slots=?, "
                     "sync=?, env=?, ssh_options=?, notes=?, updated=? WHERE id=?",
                     (m["name"], m["host"], m["user"], m["port"], m["identity"], m["workdir"], m["python"],
                      m["slots"], int(m["sync"]), m["env_text"], m["ssh_options"], m["notes"], time.time(), row["id"]))
            if new_name != row["name"]:
                for t in ("cards", "tests"):
                    self.s.x(f"UPDATE {t} SET machine=? WHERE machine=?", (new_name, row["name"]))
                self.r.machines.status.pop(row["name"], None)
        threading.Thread(target=self.r._check_safe, args=(self.r.machines.get(new_name),), daemon=True).start()
        self.r.wake.set()  # more slots may let queued cards start
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
        if "machine" in body:
            f["machine"] = self._machine_ref(body)
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
        # dropped into a machine's lane -> the copy runs there; otherwise it follows the test's machine
        machine = t["machine"]  # copied as stored (even if that machine was removed since; the run will say so)
        if body.get("machine"):
            machine = self._lane_machine(body["machine"], self.r.resolve_machine(t)[0]) or machine
        # option sets: one copy each, queued in the order given ({"optsets": ["-x", "-m slow", null]})
        try:
            if "optsets" in body:
                sets = parse_optsets(body["optsets"])
                self.s.x("UPDATE tests SET optsets=? WHERE id=?", (json.dumps(sets) if sets else None, tid))  # next time
            else:
                sets = [normalize_optset(body.get("optset"))]
        except ValueError as e:
            raise ApiError(400, str(e))
        move = {k: v for k, v in body.items() if k not in ("machine", "optsets", "optset")}
        made, base = [], time.time()
        with self.s.lock:
            for i, optset in enumerate(sets or [None]):
                card = self.card_create(q, {"title": t["name"], "target": t["target"], "args": t["args"],
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
        if c["machine"]:
            self.s.x("UPDATE tests SET machine=? WHERE id=?", (c["machine"], t["id"]))
            t["machine"] = c["machine"]
        if c.get("optset"):  # pre-ticked next time the test is put on the board
            self.s.x("UPDATE tests SET optsets=? WHERE id=?", (json.dumps([c["optset"]]), t["id"]))
        if grp == c["grp"]:
            # same headline: the card now counts as a copy of the new library test (and brings its history)
            self.s.x("UPDATE cards SET test_id=? WHERE id=?", (t["id"], cid))
            self.s.x("UPDATE runs SET test_id=? WHERE card_id=? AND test_id IS NULL", (t["id"], cid))
        # different headline: the card itself is left untouched (it keeps running with its own headline)
        return t

    def cards_clear(self, q, body):
        statuses = [s for s in (body.get("statuses") or ["passed", "failed"]) if s in ("passed", "failed", "backlog")]
        if not statuses:
            raise ApiError(400, "nothing to clear")
        sql = f"DELETE FROM cards WHERE status IN ({','.join('?' * len(statuses))})"
        args = list(statuses)
        with self.s.lock:
            n = self.s.one(sql.replace("DELETE FROM", "SELECT COUNT(*) n FROM"), args)["n"]
            self.s.x(sql, args)
        return {"cleared": n}

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
        if "cron" in body or "run_at" in body:  # check before changing anything
            c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
            if not c:
                raise ApiError(404, "no such card")
            if c["status"] != "scheduled":
                raise ApiError(409, "only Scheduled cards have a schedule; move the card to Scheduled instead")
            validate_schedule(body)
        f = self._card_fields(body, partial=True)
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
        c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        if c["status"] in ("running", "queued"):
            raise ApiError(409, f"already {c['status']}")
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

    def discover(self, q, body):
        return self.r.discover(q.get("grp") or None)

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
    ap.add_argument("--root", default=".", help="project directory pytest runs in (default: cwd)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address; use 0.0.0.0 to reach it from other machines")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workers", type=int, default=2, help="how many pytest runs may execute at the same time")
    ap.add_argument("--pytest", default=f"{shlex.quote(sys.executable)} -m pytest",
                    help='command used to invoke pytest (default: this python -m pytest), e.g. "/opt/venv/bin/pytest"')
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
    a = ap.parse_args()

    root = Path(a.root).resolve()
    data = Path(a.data).resolve() if a.data else root / ".pytest-board"
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "board.db")
    if not store.one("SELECT 1 FROM lanes"):  # internal; the board's swimlanes are the machines
        store.x("INSERT INTO lanes(name, position) VALUES('board', 1)")
    groups = Groups(Path(a.groups).resolve() if a.groups else root / "pytest-board.ini", root)
    runner = Runner(store, root, shlex.split(a.pytest), max(1, a.workers), data, groups,
                    Machines(store, groups), shlex.split(a.ssh), shlex.split(a.rsync), a.check_interval)
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
    print(f"pytest-board on http://{a.host}:{a.port}  root={root}  data={data}  workers={runner.workers}")
    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)  # systemctl stop -> clean shutdown
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shells.close_all()
        for p in list(runner.procs.values()):  # don't leave orphaned pytest processes behind
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    main()
