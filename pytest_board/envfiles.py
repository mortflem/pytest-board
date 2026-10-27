""".env files read on the board server, and KEY=value lines."""
import os
import re
import threading
from pathlib import Path

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


def parse_env_lines(text):
    """KEY=value lines (env = in the config file, a machine's or a test's env): no quoting, # starts a comment line."""
    env = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


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
