"""The board's rules, through its HTTP API (a real server, no runs needed)."""
import pytest

from conftest import ApiFailed, DEFAULT_INI


def fails(board, status, method, path, body=None):
    with pytest.raises(ApiFailed) as e:
        board.api(method, path, body)
    assert e.value.status == status, e.value.body
    return e.value.body.get("error", "") if isinstance(e.value.body, dict) else e.value.body


def test_needs_the_token(board):
    with pytest.raises(ApiFailed) as e:
        board.api("GET", "/board", auth=False)
    assert e.value.status == 401


def test_board_overview(board):
    d = board.api("GET", "/board")
    assert [m["name"] for m in d["machines"]] == ["pi", "nosc"]  # no "This server"
    assert all(m["source"] == "config" for m in d["machines"])
    assert [g["key"] for g in d["groups"]] == ["SYS", "LOOSE"]
    assert d["groups_config"]["error"] is None
    assert {t["name"] for t in d["tests"]} == {"Quick", "Other", "Loose"}  # seeded from tests =
    assert "machine" not in d["server"] and "workers" not in d["server"]


def test_copies_run_on_their_row_or_the_headline_machine(board):
    c = board.new_card("Quick")
    assert board.card(c)["machine"] is None and board.card(c)["run_machine"] == "pi"  # SYS: machine = pi
    c = board.new_card("Quick", machine="nosc")
    assert board.card(c)["machine"] == "nosc"


def test_option_sets_make_one_card(board):
    d = board.api("POST", f"/tests/{board.test_id('Quick')}/copy",
                  {"status": "backlog", "optsets": ["-x", "-m smoke"], "combine": True, "extra_args": "--n 3"})
    assert d["created"] == 1
    c = board.card(d["id"])
    assert c["optset"] == "-x -m smoke" and c["args"] == "--n 3"


def test_library_tests_have_no_machine(board):
    tid = board.test_id("Quick")
    board.api("PATCH", f"/tests/{tid}", {"machine": "nosc"})  # ignored (an older page may still send it)
    assert next(t for t in board.api("GET", "/board")["tests"] if t["id"] == tid)["machine"] is None


def test_no_machine(board):
    """LOOSE has no machine: its cards wait in the "No machine" row and can't be queued or run there."""
    assert "no machine" in fails(board, 409, "POST", f"/tests/{board.test_id('Loose')}/copy", {"status": "queued"})
    c = board.new_card("Loose")
    assert board.card(c)["run_machine"] is None
    assert "no machine" in fails(board, 409, "POST", f"/cards/{c}/run")
    assert "no machine" in fails(board, 409, "POST", f"/cards/{c}/move", {"status": "queued", "machine": ""})
    board.api("POST", f"/cards/{c}/move", {"status": "backlog", "machine": "", "position": 0.5})  # rearranging is fine
    board.api("POST", f"/cards/{c}/move", {"status": "queued", "machine": "pi"})  # onto a machine's row: runs there
    assert board.card(c)["machine"] == "pi"


def test_local_is_gone(board):
    c = board.new_card("Quick")
    assert "doesn't run tests" in fails(board, 400, "PATCH", f"/cards/{c}", {"machine": "local"})
    fails(board, 404, "POST", "/machines/local/run", {})


def test_binding_limits_where_tests_run(board):
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machine = pi\nmachines = pi\n"), wait=True)
    assert board.api("GET", "/board")["groups"][0]["machines"] == ["pi"]
    msg = fails(board, 409, "POST", f"/tests/{board.test_id('Quick')}/copy", {"status": "backlog", "machine": "nosc"})
    assert "run only on pi" in msg
    c = board.new_card("Quick", machine="pi")
    fails(board, 409, "POST", f"/cards/{c}/move", {"status": "backlog", "machine": "nosc"})
    fails(board, 409, "PATCH", f"/cards/{c}", {"machine": "nosc"})
    loose = board.new_card("Loose")  # an unbound headline can still go anywhere
    board.api("PATCH", f"/cards/{loose}", {"machine": "nosc"})
    fails(board, 409, "PATCH", f"/cards/{loose}", {"grp": "SYS"})  # ...but not into SYS while it's on nosc
    assert board.api("GET", "/discover?grp=SYS&machine=nosc")["error"].startswith("SYS tests run only on pi")


def test_run_lane_skips_cards_of_bound_headlines(board):
    on_nosc = board.new_card("Quick", machine="nosc")
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machine = pi\nmachines = pi\n"), wait=True)
    board.new_card("Loose", machine="nosc")
    d = board.api("POST", "/machines/nosc/run", {})
    assert (d["queued"], d["skipped"]) == (1, 1)
    assert board.card(on_nosc)["status"] == "backlog"


def test_clear_one_row(board):
    """Clear on a row removes only that row's cards; "" is the "No machine" row, not every row."""
    on_pi, no_machine = board.new_card("Quick"), board.new_card("Loose")
    assert board.api("POST", "/cards/clear", {"statuses": ["backlog"], "machine": "nosc"})["cleared"] == 0
    assert board.api("POST", "/cards/clear", {"statuses": ["backlog"], "machine": ""})["cleared"] == 1
    left = {c["id"] for c in board.api("GET", "/board")["cards"]}
    assert on_pi in left and no_machine not in left


def test_ui_machines(board):
    board.api("POST", "/machines", {"name": "lab", "host": "lab.local", "workdir": "/tmp", "slots": 4})
    m = next(m for m in board.api("GET", "/board")["machines"] if m["name"] == "lab")
    assert m["source"] == "ui" and m["host"] == "lab.local" and "slots" not in m
    board.api("PATCH", "/machines/lab", {"host": "lab2.local"})
    assert next(m for m in board.api("GET", "/board")["machines"] if m["name"] == "lab")["host"] == "lab2.local"
    fails(board, 409, "POST", "/machines", {"name": "pi", "host": "other"})  # the config file has pi
    board.api("DELETE", "/machines/lab")
    assert "lab" not in [m["name"] for m in board.api("GET", "/board")["machines"]]


def test_discover_on_a_machine(board):
    d = board.api("GET", "/discover?grp=SYS&machine=pi")
    assert d["error"] is None and d["ids"] == ["test_a.py::test_one", "test_a.py::test_two", "sub/test_b.py::test_three"]
    assert board.api("GET", "/discover?grp=LOOSE")["error"] == "pick the machine to discover tests on"
    added = board.api("POST", "/tests/bulk", {"grp": "SYS", "targets": d["ids"][:2], "machine": "nosc"})
    assert added == {"created": 2, "skipped": 0}
    assert board.api("POST", "/tests/bulk", {"grp": "SYS", "targets": d["ids"][:2]}) == {"created": 0, "skipped": 2}


def test_config_problems_are_reported(board):
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machines = pi, ghost\n"), wait=True)
    assert "'ghost' isn't defined" in board.api("GET", "/board")["groups_config"]["error"]
