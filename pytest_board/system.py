"""Operating-system details: the service user, Windows vs. POSIX process flags, command lines, ending process trees."""
import getpass
import os
import shlex
import signal
import subprocess

try:
    SERVICE_USER = getpass.getuser()
except Exception:  # no passwd entry / login name (unusual containers)
    SERVICE_USER = str(os.getuid()) if hasattr(os, "getuid") else ""

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
