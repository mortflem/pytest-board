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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the pytest_board package next to this file

from pytest_board import BOARD_VERSION  # noqa: E402,F401
from pytest_board.cli import main  # noqa: E402
from pytest_board.config import Groups  # noqa: E402,F401
from pytest_board.cron import Cron  # noqa: E402,F401
from pytest_board.envfiles import parse_env_file, parse_env_lines, read_env_file  # noqa: E402,F401
from pytest_board.model import normalize_branch, normalize_machine, normalize_optset, parse_optsets  # noqa: E402,F401

if __name__ == "__main__":
    main()
