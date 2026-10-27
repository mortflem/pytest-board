"""The board's vocabulary and the rules for its values: columns, messages, option sets, branches, machines."""
import re
import shlex

from .envfiles import parse_env_lines, resolve_env_file

NO_MACHINE = ("this card has no machine to run on: drag it to a machine's row, or set machine = for its headline "
              "in pytest-board.ini")
LOCAL_GONE = "the board server doesn't run tests itself any more: pick one of the machines"
COLUMNS = ["backlog", "scheduled", "queued", "running", "passed", "failed"]
SYSTEM_ONLY = {"running", "passed", "failed"}  # cannot be dropped into by hand


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
    env = parse_env_lines(env_text)
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


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
