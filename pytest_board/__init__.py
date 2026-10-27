"""pytest-board: a drag & drop board that runs pytest on machines over ssh.

server.py starts it; the modules here are its parts (see the README, "How the code is laid out").
"""
from pathlib import Path

# Bump together with PAGE_VERSION in static/index.html when the page needs this server (the page asks for a
# restart when the running server is older than the page).
BOARD_VERSION = "2026.10.29.1"

SERVER_DIR = Path(__file__).resolve().parent.parent  # the board's own folder: server.py, pytest_board/, static/
STATIC = SERVER_DIR / "static"  # the page (static/index.html)


def page_stamp():
    """Changes whenever static/index.html changes on disk: an open page then reloads itself."""
    try:
        return str((STATIC / "index.html").stat().st_mtime_ns)
    except OSError:
        return ""
