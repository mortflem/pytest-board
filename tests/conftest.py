"""Test helpers: a real board server on a free port, with fake ssh, screen and pytest standing in for the machines.

Each test that uses the `board` fixture gets its own folders (in pytest's tmp_path) and its own server:

  root/        the board's --root: pytest-board.ini, the board's data (.pytest-board/)
  work/        the machines' workdir (both test machines share it)
  remote/      FAKE_SSH_ROOT: the machines' HOME, the netdown switch, ssh.log

The machines in the default config: pi (has screen) and nosc (no screen). Their "pytest" is fakes/fakepytest.py.
"""
import base64
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(REPO))  # import server (the board) in the unit tests

if sys.platform == "win32":  # the fakes are sh scripts and the runs use POSIX tools
    collect_ignore_glob = ["test_runs.py", "test_api.py"]

TOKEN = "test-token"

DEFAULT_INI = """\
[SYS]
title   = System
machine = pi
options =
    Stop at first failure | -x
    Smoke | -m smoke
tests   =
    Quick | test_a.py
    Other | test_b.py | -q

[LOOSE]
title = a headline without a machine
tests =
    Loose | test_c.py

[machine:pi]
host    = pi.lab
user    = fm
workdir = {work}
python  = {python}
env     =
    T_MACHINE=from the machine
    T_SECRET=s3 cr$et'x

[machine:nosc]
host    = nosc.lab
workdir = {work}
python  = {python}
"""


@pytest.fixture(scope="session")
def fake_bin(tmp_path_factory):
    """The fakes, executable, in a folder of their own (that folder goes first on PATH: screen)."""
    d = tmp_path_factory.mktemp("bin")
    for name in ("ssh", "screen", "fakepytest.py"):
        shutil.copy(FAKES / name, d / name)
        (d / name).chmod((d / name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiFailed(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


class Board:
    def __init__(self, base: Path, fake_bin: Path):
        self.base, self.fake_bin = base, fake_bin
        self.root, self.work, self.remote = base / "root", base / "work", base / "remote"
        for d in (self.root, self.work, self.remote / "home"):
            d.mkdir(parents=True, exist_ok=True)
        self.python = f"{sys.executable} {fake_bin / 'fakepytest.py'}"
        self.proc = None
        self.port = free_port()
        self.write_ini(DEFAULT_INI)

    # ---- the config file
    def write_ini(self, text, wait=False):
        """Write pytest-board.ini ({work} and {python} are filled in). wait=True: until the board shows the change
        (its headlines, machines or config error look different from before)."""
        def seen():  # (not the options found in conftest.py files: those change by themselves)
            d = self.api("GET", "/board")
            return [dict(g, found=None) for g in d["groups"]], d["machines"], d["groups_config"]["error"]
        before = seen() if wait else None
        path = self.root / "pytest-board.ini"
        old = path.stat().st_mtime_ns if path.exists() else 0
        path.write_text(text.format(work=self.work, python=self.python))
        if path.stat().st_mtime_ns == old:  # same timestamp as before: the board would not notice
            os.utime(path, ns=(old + 1_000_000_000, old + 1_000_000_000))
        if wait:
            self.until(lambda: seen() != before, "the board to read the changed config file")

    # ---- the server
    def start(self):
        env = dict(os.environ, FAKE_SSH_ROOT=str(self.remote), PYTEST_BOARD_TOKEN=TOKEN,
                   PATH=f"{self.fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        self.log_file = open(self.base / "server.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "server.py"), "--root", str(self.root), "--port", str(self.port),
             "--data", str(self.root / ".pytest-board"),  # (by default it would go next to server.py: the real one)
             "--ssh", f"{sys.executable} {self.fake_bin / 'ssh'}", "--check-interval", "0"],
            env=env, stdout=self.log_file, stderr=subprocess.STDOUT)
        self.until(self._up, "server start", timeout=20)
        return self

    def _up(self):
        if self.proc.poll() is not None:
            raise RuntimeError("the board server exited:\n" + self.server_log())
        try:
            self.api("GET", "/board")
            return True
        except (OSError, ApiFailed):
            return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self.log_file.close()

    def restart(self):
        self.stop()
        return self.start()

    def server_log(self):
        p = self.base / "server.log"
        return p.read_text(errors="replace") if p.exists() else ""

    # ---- the API
    def api(self, method, path, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{TOKEN}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = raw.decode(errors="replace")
            raise ApiFailed(e.code, raw) from None

    def get_page(self, path="/"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{TOKEN}".encode()).decode())
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode()

    # ---- helpers for tests
    def until(self, check, what, timeout=30, interval=0.2):
        end = time.monotonic() + timeout
        while True:
            got = check()
            if got:
                return got
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for {what}\n--- server log ---\n{self.server_log()[-4000:]}")
            time.sleep(interval)

    def test_id(self, name):
        return next(t["id"] for t in self.api("GET", "/board")["tests"] if t["name"] == name)

    def card(self, cid):
        return self.api("GET", f"/cards/{cid}")

    def new_card(self, test="Quick", **body):
        """A copy of a library test on the board (in Backlog unless status= says otherwise)."""
        body.setdefault("status", "backlog")
        return self.api("POST", f"/tests/{self.test_id(test)}/copy", body)["id"]

    def wait_status(self, cid, *statuses, timeout=40):
        return self.until(lambda: (c := self.card(cid))["status"] in statuses and c, f"card {cid} in {statuses}",
                          timeout=timeout)

    def run_log(self, cid):
        """The log of the card's latest run."""
        return self.api("GET", f"/runs/{self.card(cid)['runs'][0]['id']}/log")["text"]

    def last_run(self, cid):
        return self.card(cid)["runs"][0]

    def network(self, up):
        flag = self.remote / "netdown"
        if up:
            flag.unlink(missing_ok=True)
        else:
            flag.touch()


@pytest.fixture
def board(tmp_path, fake_bin):
    b = Board(tmp_path, fake_bin).start()
    yield b
    b.network(True)
    b.stop()


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid"))


@pytest.fixture
def git_work(board):
    """Makes the machines' workdir a git checkout with a remote that has the branches main and feature/x (the file
    MARK says which one is checked out)."""
    if not shutil.which("git"):
        pytest.skip("git isn't installed")
    origin, seed = board.base / "origin.git", board.base / "seed"
    git(board.base, "init", "-q", "--bare", str(origin))
    git(board.base, "init", "-q", "-b", "main", str(seed))
    (seed / "MARK").write_text("main\n")
    git(seed, "add", "MARK")
    git(seed, "commit", "-q", "-m", "main")
    git(seed, "checkout", "-q", "-b", "feature/x")
    (seed / "MARK").write_text("feature\n")
    git(seed, "commit", "-q", "-am", "feature")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main", "feature/x")
    shutil.rmtree(board.work)
    git(board.base, "clone", "-q", "-b", "main", str(origin), str(board.work))
    return board.work
