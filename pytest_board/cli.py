"""The command line: python3 server.py --root ... starts the board."""
import argparse
import os
import signal
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import SERVER_DIR
from .api import App
from .config import Groups
from .envfiles import env_file_info
from .machines import Machines
from .runner import Runner
from .store import Store
from .system import kill_tree, split_local
from .web import make_handler


def data_dir(arg, root, server_dir=SERVER_DIR):
    """Where the database, logs and junit reports go -> (folder, note to print or None).

    --data if given, else .pytest-board in the board's own folder (next to server.py): wherever the project and
    its pytest-board.ini are, the board's data stays with the board. Older versions kept it in <root>/.pytest-board;
    a board that has its data there keeps using it until it's moved."""
    if arg:
        return Path(arg).expanduser().resolve(), None
    new, old = server_dir / ".pytest-board", root / ".pytest-board"
    if old != new and (old / "board.db").exists() and not (new / "board.db").exists():
        return old, (f"note: using the board's data in {old}, where older versions kept it. New boards keep it in "
                     f"{new}: to move it there, stop the board and move the folder (or pass --data {old} to keep it).")
    return new, None


def main():
    ap = argparse.ArgumentParser(description="Drag & drop scheduler board for pytest runs")
    ap.add_argument("--root", default=".", help="the project folder: where pytest-board.ini is by default, and what "
                    "machines with sync = true get a copy of (default: cwd)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address; use 0.0.0.0 to reach it from other machines")
    ap.add_argument("--port", type=int, default=8765)
    # no longer used: tests run on the machines (each has its own pytest command). Still accepted, so
    # service files that pass them keep working
    ap.add_argument("--workers", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--pytest", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--data", default=None, help="where the database and logs live (default: .pytest-board in the "
                    "board's own folder, next to server.py)")
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
    data, note = data_dir(a.data, root)
    if note:
        print(note, file=sys.stderr)
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"can't create the board's data folder {data}: {e.strerror or e}. Let this user write there, "
                 "or pass --data with a folder it can write to.")
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
