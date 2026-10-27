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


def test_options_that_take_a_value(board):
    """Temperature | --set_temperature <degrees>: the page fills in the value; a card never gets the <placeholder>."""
    board.write_ini(DEFAULT_INI.replace("    Smoke | -m smoke\n", "    Smoke | -m smoke\n    Temp | --set_temperature <degrees>\n"),
                    wait=True)
    opts = board.api("GET", "/board")["groups"][0]["options"]
    assert opts[-1] == {"label": "Temp", "args": "--set_temperature <degrees>", "value": "degrees"}
    tid = board.test_id("Quick")
    assert "type a value for <degrees>" in fails(board, 400, "POST", f"/tests/{tid}/copy",
                                                  {"status": "backlog", "optsets": ["--set_temperature <degrees>"],
                                                   "combine": True})
    d = board.api("POST", f"/tests/{tid}/copy",
                  {"status": "backlog", "optsets": ["-m smoke --set_temperature 0 10"], "combine": True})
    assert board.card(d["id"])["optset"] == "-m smoke --set_temperature 0 10"
    fails(board, 400, "PATCH", f"/cards/{d['id']}", {"optset": "--set_temperature <degrees>"})


def test_options_from_conftest_files(board):
    """The board reads the options a headline's conftest.py files declare on the machine (never running them) and
    offers them next to options =; the result is kept and shown with the headline."""
    (board.work / "conftest.py").write_text(
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--device', default='A', metavar='NAME', help='device under test')\n"
        "    parser.addoption('--fail', action='store_true')\n"
        "    raise SystemExit('never run')\n")
    (board.work / "tests").mkdir()
    (board.work / "tests" / "conftest.py").write_text(
        "def pytest_addoption(parser):\n    parser.addoption('--set_temperature', nargs='+', metavar='DEGREES')\n")
    (board.work / "venv" / "lib").mkdir(parents=True)
    (board.work / "venv" / "lib" / "conftest.py").write_text(
        "def pytest_addoption(parser):\n    parser.addoption('--from-venv')\n")
    e = board.api("POST", "/scan-options", {"grp": "SYS", "machine": "nosc"})
    assert e["error"] is None and e["machine"] == "nosc" and e["where"].startswith("nosc.lab:")
    assert [o["args"] for o in e["options"]] == ["--device=<name>", "--fail", "--set_temperature <degrees>"]
    assert [p.rsplit("/", 2)[-2:] for p in e["files"]] == [[board.work.name, "conftest.py"], ["tests", "conftest.py"]]
    sys_ = board.api("GET", "/board")["groups"][0]
    assert sys_["found"]["options"] == e["options"] and sys_["options"][0]["args"] == "-x"  # options = unchanged
    c = board.new_card("Quick", optsets=["--device=B --set_temperature 0 10"], combine=True)
    assert board.card(c)["optset"] == "--device=B --set_temperature 0 10"
    board.restart()  # kept in the database
    assert board.api("GET", "/board")["groups"][0]["found"]["options"] == e["options"]


def test_options_from_conftest_problems(board):
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machine = pi\ndir = nope\n"), wait=True)
    e = board.api("POST", "/scan-options", {"grp": "SYS"})
    assert e["machine"] == "pi" and "doesn't exist on pi" in e["error"]
    board.network(False)
    assert "can't reach nosc" in board.api("POST", "/scan-options", {"grp": "SYS", "machine": "nosc"})["error"]
    board.network(True)
    fails(board, 404, "POST", "/scan-options", {"grp": "NOPE"})
    board.write_ini(DEFAULT_INI.replace("machine = pi\n", "machine = pi\nconftest_options = no\n"), wait=True)
    assert "conftest_options = no" in fails(board, 409, "POST", "/scan-options", {"grp": "SYS"})
    assert board.api("GET", "/board")["groups"][0]["found"] is None


def test_options_are_looked_for_by_themselves(board):
    """Headlines nobody looked at yet are looked at in the background (LOOSE has no machine: its folder here)."""
    (board.root / "conftest.py").write_text("def pytest_addoption(parser):\n    parser.addoption('--here')\n")
    board.write_ini(DEFAULT_INI.replace("[LOOSE]\n", "[LOOSE2]\n"), wait=True)  # a headline it hasn't seen
    got = board.until(lambda: next(g for g in board.api("GET", "/board")["groups"] if g["key"] == "LOOSE2")["found"],
                      "the new headline to be looked at")
    assert [o["args"] for o in got["options"]] == ["--here=<here>"] and got["machine"] is None


def test_branch_on_the_way(board):
    c = board.new_card("Quick")
    assert "not a git branch name" in fails(board, 400, "POST", f"/cards/{c}/move",
                                            {"status": "queued", "branch": "no good"})
    assert board.card(c)["status"] == "backlog"  # nothing changed
    board.api("POST", f"/cards/{c}/move", {"status": "scheduled", "run_at": 4102444800, "branch": "feature/y"})
    assert (board.card(c)["status"], board.card(c)["branch"]) == ("scheduled", "feature/y")
    d = board.api("GET", "/branches?grp=SYS&machine=pi")  # the workdir isn't a git checkout here
    assert d["git"] is False and "isn't a git checkout" in d["error"]
    fails(board, 404, "GET", "/branches?grp=SYS&machine=ghost")
    assert board.api("GET", "/branches?grp=LOOSE")["error"] == "no machine"


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


def ini(board):
    return (board.root / "pytest-board.ini").read_text()


def machine(board, name):
    return next((m for m in board.api("GET", "/board")["machines"] if m["name"] == name), None)


def test_add_writes_the_machine_into_the_config_file(board):
    before = ini(board)
    board.api("POST", "/machines", {"name": "lab", "host": "lab.local", "workdir": "/tmp", "env": "T_X=1", "slots": 4})
    m = machine(board, "lab")
    assert m["source"] == "config" and m["host"] == "lab.local" and m["env_keys"] == ["T_X"] and "slots" not in m
    after = ini(board)
    assert after.startswith(before.rstrip("\n"))  # the rest of the file is as it was; the section comes at the end
    assert "[machine:lab]" in after and "lab.local" in after and "T_X=1" in after
    fails(board, 409, "POST", "/machines", {"name": "pi", "host": "other"})  # pi exists


def test_save_changes_only_that_line(board):
    # (only a comment is added, so the board shows nothing new; Save reads the file from disk anyway)
    board.write_ini(DEFAULT_INI.replace("host    = nosc.lab", "host    = nosc.lab      ; the second box"))
    before = ini(board)
    board.api("PATCH", "/machines/nosc", {"host": "nosc2.lab"})
    after = ini(board)
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [("host    = nosc.lab      ; the second box", "host    = nosc2.lab     ; the second box")]
    assert len(before.splitlines()) == len(after.splitlines())
    assert machine(board, "nosc")["host"] == "nosc2.lab"


def test_save_keeps_hidden_env_values(board):
    """The page sees KEY=… for env values from the file; saving those lines as they are keeps the real values."""
    board.api("PATCH", "/machines/pi", {"env": "T_MACHINE=…\nT_SECRET=…\nT_NEW=3"})
    text = ini(board)
    assert "T_MACHINE=from the machine" in text and "T_SECRET=s3 cr$et'x" in text and "T_NEW=3" in text
    assert "…" not in text


def test_rename_and_delete(board):
    c = board.new_card("Quick", machine="nosc")
    board.api("PATCH", "/machines/nosc", {"name": "nosc2"})
    assert "[machine:nosc2]" in ini(board) and "[machine:nosc]" not in ini(board)
    assert board.card(c)["machine"] == "nosc2"  # cards follow the new name
    assert "rename it there" in fails(board, 409, "PATCH", "/machines/pi", {"name": "pi9"})  # SYS names pi
    assert "change that first" in fails(board, 409, "DELETE", "/machines/pi")
    board.api("DELETE", "/machines/nosc2")
    assert "nosc2" not in ini(board) and machine(board, "nosc2") is None
    assert board.card(c)["machine"] is None


def test_values_the_file_cant_hold_are_refused(board):
    before = ini(board)
    assert "comment" in fails(board, 400, "POST", "/machines", {"name": "x", "host": "h", "notes": "old ; new"})
    assert ini(board) == before


def test_machines_from_the_database_still_work(board):
    """Machines an older version stored in board.db stay there, editable as before."""
    import sqlite3
    db = sqlite3.connect(board.root / ".pytest-board" / "board.db")
    db.execute("INSERT INTO machines(name, host, workdir, python, position, created, updated) "
               "VALUES('old', 'old.lab', '/tmp', 'python3 -m pytest', 1, 0, 0)")
    db.commit()
    db.close()
    assert machine(board, "old")["source"] == "ui"
    board.api("PATCH", "/machines/old", {"host": "old2.lab"})
    assert machine(board, "old")["host"] == "old2.lab" and "old2.lab" not in ini(board)
    board.api("DELETE", "/machines/old")
    assert machine(board, "old") is None


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
