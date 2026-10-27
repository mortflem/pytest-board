"""Real runs on the (fake) machines: over the fake ssh, in a screen session or as a background process."""
import re
import time

import pytest

from conftest import DEFAULT_INI


def ticks(log):
    return [int(n) for n in re.findall(r"^tick (\d+)$", log, re.M)]


def test_a_passing_run(board):
    c = board.new_card("Quick", status="queued", optsets=["-x"], combine=True)
    card = board.wait_status(c, "passed", "failed")
    run = board.last_run(c)
    log = board.run_log(c)
    assert card["status"] == "passed", log
    assert (run["outcome"], run["machine"], run["passed"], run["failed"]) == ("passed", "pi", 3, 0)
    assert "ARGS: test_a.py -x" in log  # target, then the option set
    assert f"CWD: {board.work}" in log
    assert "ENV T_MACHINE=from the machine" in log
    assert "ENV T_SECRET=s3 cr$et'x" in log  # quotes and $ arrive intact
    assert "ENV PYTEST_BOARD_MACHINE=pi" in log and f"ENV PYTEST_BOARD_RUN_ID={run['id']}" in log
    assert "ENV PYTEST_BOARD_GROUP=SYS" in log
    assert "PYTEST_BOARD_TOKEN" not in log  # the board's password never reaches the tests
    assert "[pytest-board] exit code 0 · passed · on pi" in log


@pytest.mark.parametrize("rc, status, outcome", [(1, "failed", "failed"), (4, "failed", "usage-error"),
                                                 (5, "failed", "no-tests")])
def test_failing_runs(board, rc, status, outcome):
    c = board.new_card("Quick")
    board.api("PATCH", f"/cards/{c}", {"env": f"RC={rc}"})
    board.api("POST", f"/cards/{c}/run")
    assert board.wait_status(c, "passed", "failed")["status"] == status
    assert board.last_run(c)["outcome"] == outcome


def test_env_layers(board):
    """.env next to the config file < the machine's env < the headline's env < the card's own env."""
    (board.root / ".env").write_text("export T_BOARD=board\nT_ALL=board\n")
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machine = pi\nenv =\n    T_ALL=headline\n    T_HEAD=headline\n"),
                    wait=True)
    c = board.new_card("Quick")
    board.api("PATCH", f"/cards/{c}", {"env": "T_CARD=card\nT_HEAD=card"})
    board.api("POST", f"/cards/{c}/run")
    board.wait_status(c, "passed")
    log = board.run_log(c)
    for line in ("T_BOARD=board", "T_ALL=headline", "T_HEAD=card", "T_CARD=card", "T_MACHINE=from the machine"):
        assert f"ENV {line}" in log, log


def test_without_screen(board):
    c = board.new_card("Quick", machine="nosc", status="queued")
    assert board.wait_status(c, "passed", "failed")["status"] == "passed", board.run_log(c)


def test_one_test_at_a_time_per_machine(board):
    cards = [board.new_card("Quick") for _ in range(3)]
    for c in cards:
        board.api("PATCH", f"/cards/{c}", {"env": "TICKS=2"})
        board.api("POST", f"/cards/{c}/run")
    most = 0
    end = time.monotonic() + 40
    while time.monotonic() < end:
        st = [c["status"] for c in board.api("GET", "/board")["cards"] if c["id"] in cards]
        most = max(most, st.count("running"))
        if all(s == "passed" for s in st):
            break
        time.sleep(0.1)
    assert all(board.card(c)["status"] == "passed" for c in cards)
    assert most == 1


def test_stop(board):
    c = board.new_card("Quick")
    board.api("PATCH", f"/cards/{c}", {"env": "TICKS=40"})
    board.api("POST", f"/cards/{c}/run")
    board.until(lambda: ticks(board.run_log(c)), "the run to start")
    board.api("POST", f"/cards/{c}/stop")
    assert board.wait_status(c, "backlog")["status"] == "backlog"
    assert board.last_run(c)["outcome"] == "cancelled"


def test_network_drop_doesnt_end_the_run(board):
    c = board.new_card("Quick")
    board.api("PATCH", f"/cards/{c}", {"env": "TICKS=10"})
    board.api("POST", f"/cards/{c}/run")
    board.until(lambda: len(ticks(board.run_log(c))) >= 2, "the run to get going")
    board.network(False)
    board.until(lambda: board.card(c).get("link"), "the card to show it's reconnecting")
    time.sleep(1)
    board.network(True)
    assert board.wait_status(c, "passed", "failed", timeout=60)["status"] == "passed"
    assert ticks(board.run_log(c)) == list(range(10))  # caught up: no gaps, no repeats


def test_board_restart_doesnt_end_the_run(board):
    c = board.new_card("Quick")
    board.api("PATCH", f"/cards/{c}", {"env": "TICKS=8"})
    board.api("POST", f"/cards/{c}/run")
    board.until(lambda: len(ticks(board.run_log(c))) >= 2, "the run to get going")
    board.restart()
    assert board.wait_status(c, "passed", "failed", timeout=60)["status"] == "passed"
    log = board.run_log(c)
    assert "following the run on pi again" in log
    assert ticks(log) == list(range(8))


def test_scheduled_once(board):
    c = board.new_card("Quick")
    board.api("POST", f"/cards/{c}/move", {"status": "scheduled", "run_at": time.time() + 2})
    assert board.card(c)["status"] == "scheduled"
    card = board.wait_status(c, "passed", "failed", timeout=30)
    assert card["status"] == "passed" and card["run_at"] is None


def test_git_branch(git_work, board):
    c = board.new_card("Quick")
    board.api("PATCH", f"/cards/{c}", {"branch": "feature/x"})
    board.api("POST", f"/cards/{c}/run")
    assert board.wait_status(c, "passed", "failed")["status"] == "passed", board.run_log(c)
    log = board.run_log(c)
    assert "BRANCH: feature/x | MARK: feature" in log
    assert board.last_run(c)["branch"] == "feature/x"
    # a branch that isn't on the remote: nothing runs
    board.api("PATCH", f"/cards/{c}", {"branch": "nope"})
    board.api("POST", f"/cards/{c}/run")
    board.wait_status(c, "failed")
    assert board.last_run(c)["outcome"] == "checkout-failed"
    assert "isn't on origin" in board.run_log(c)


def test_headline_branch(git_work, board):
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machine = pi\nbranch = feature/x\n"), wait=True)
    c = board.new_card("Quick", status="queued")
    board.wait_status(c, "passed", "failed")
    assert "BRANCH: feature/x | MARK: feature" in board.run_log(c)


def test_card_with_a_removed_machine(board):
    c = board.new_card("Quick", machine="nosc")
    board.write_ini(DEFAULT_INI.split("[machine:nosc]")[0], wait=True)
    board.api("POST", f"/cards/{c}/run")
    board.wait_status(c, "failed")
    assert board.last_run(c)["outcome"] == "no-machine"
