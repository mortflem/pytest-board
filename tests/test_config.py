"""Parsing and validation, without a server: .env files, cron, option sets, branches, machines, the config file."""
import textwrap
from datetime import datetime

import pytest

import server as pb


# ---- .env files
def test_env_file_shell_rules():
    env, problems = pb.parse_env_file(textwrap.dedent("""\
        # a comment
        export BASE_URL=https://staging.example.com
        TOKEN='s3 cr$t'            # single quotes: taken literally
        GREETING="hi $USER_NAME"   # double quotes: $VAR expands
        WITH_DEFAULT=${MISSING:-fallback}
        export ONLY_NAME
        """), {"USER_NAME": "fm"})
    assert problems == []
    assert env == {"BASE_URL": "https://staging.example.com", "TOKEN": "s3 cr$t", "GREETING": "hi fm",
                   "WITH_DEFAULT": "fallback"}


def test_env_file_refers_to_earlier_lines():
    env, _ = pb.parse_env_file("A=1\nB=${A}2\n")
    assert env["B"] == "12"


@pytest.mark.parametrize("line, problem", [
    ("X=$(whoami)", "commands"),
    ("X=`whoami`", "commands"),
    ("X=two words", "quotes"),
    ("X='unclosed", "unclosed"),
    ("not an assignment", "NAME=value"),
])
def test_env_file_problems(line, problem):
    env, problems = pb.parse_env_file(line + "\n")
    assert env == {}
    assert len(problems) == 1 and problem in problems[0]


# ---- cron
def ts(*args):
    return datetime(*args).timestamp()


def test_cron_next_after():
    assert pb.Cron("*/15 * * * *").next_after(ts(2026, 10, 26, 9, 7)) == ts(2026, 10, 26, 9, 15)
    assert pb.Cron("0 2 * * *").next_after(ts(2026, 10, 26, 9, 7)) == ts(2026, 10, 27, 2, 0)
    assert pb.Cron("@daily").next_after(ts(2026, 10, 26, 9, 7)) == ts(2026, 10, 27, 0, 0)
    assert pb.Cron("0 7 * * 1-5").next_after(ts(2026, 10, 31, 12, 0)) == ts(2026, 11, 2, 7, 0)  # Sat -> Mon


@pytest.mark.parametrize("expr", ["* * *", "61 * * * *", "nonsense", "0 0 31 2 *"])
def test_cron_invalid(expr):
    with pytest.raises(ValueError):
        pb.Cron(expr).next_after(ts(2026, 1, 1))


# ---- option sets and branches
def test_optsets_normalized():
    assert pb.normalize_optset("  -m   smoke   -x ") == "-m smoke -x"
    assert pb.normalize_optset("") is None
    assert pb.parse_optsets(["-x", None, "-x", "-m 'not slow'"]) == ["-x", None, "-m 'not slow'"]
    with pytest.raises(ValueError):
        pb.normalize_optset("-m 'unclosed")


def test_card_optsets_have_their_values():
    """An option's <placeholder> is filled in by the page; a card can't run with one left in."""
    assert pb.filled_optset("--set_temperature 0 10") == "--set_temperature 0 10"
    for unfilled in ("--set_temperature <degrees>", "-x --device=<name>"):
        with pytest.raises(ValueError, match="type a value"):
            pb.filled_optset(unfilled)
    with pytest.raises(ValueError):
        pb.parse_optsets(["-x", "--set_temperature <degrees>"])


@pytest.mark.parametrize("name, ok", [("main", True), ("feature/x", True), ("release-1.2", True),
                                      ("bad..name", False), ("-x", False), ("a b", False), ("x.lock", False)])
def test_branch_names(name, ok):
    if ok:
        assert pb.normalize_branch(name) == name
    else:
        with pytest.raises(ValueError):
            pb.normalize_branch(name)


# ---- machines
def test_machine_defaults():
    m = pb.normalize_machine({"host": "10.0.0.72"}, "pi", "config")
    assert (m["workdir"], m["python"], m["sync"], m["port"]) == ("~/git", "python3 -m pytest", False, None)


@pytest.mark.parametrize("fields, name, problem", [
    ({}, "pi", "host is required"),
    ({"host": "a b"}, "pi", "host"),
    ({"host": "h", "port": "70000"}, "pi", "port"),
    ({"host": "h"}, "local", "machine name"),
    ({"host": "h"}, "bad name!", "machine name"),
    ({"host": "h", "env": "1BAD=x"}, "pi", "env name"),
])
def test_machine_invalid(fields, name, problem):
    with pytest.raises(ValueError, match=problem):
        pb.normalize_machine(fields, name, "ui")


# ---- the config file
def load(tmp_path, text):
    path = tmp_path / "pytest-board.ini"
    path.write_text(textwrap.dedent(text))
    g = pb.Groups(path, tmp_path)
    g.load()
    return g


# ---- options declared in conftest.py files (read with ast, never run)
CONFTEST = """\
import pytest

def pytest_addoption(parser):
    g = parser.getgroup("example")
    g.addoption("--device", default="A", metavar="NAME", help="the device under test (default: %(default)s)")
    g.addoption("--set_temperature", nargs="+", type=float, default=[25.0], metavar="DEGREES",
                help="temperatures to test at, one test each")
    parser.addoption("--fail", action="store_true", help="make a test fail")
    parser.addoption("--env", choices=["dev", "prod"], help=f"where to run {pytest.__name__}")
    parser.addoption("-D", dest="dut")
    parser.addoption("--version-x", action="version")
    parser.addoption(*NAMES)
    parser.addini("timeout", "an ini setting, not an option")
"""


def test_options_from_conftest():
    opts, problem = pb.optscan.parse_conftest(CONFTEST)
    assert problem is None
    got = {o["flag"]: (o["label"], o["args"], o["value"], o["default"], o["choices"]) for o in opts}
    assert got == {
        "--device": ("The device under test", "--device=<name>", "name", "A", None),
        "--set_temperature": ("Temperatures to test at", "--set_temperature <degrees>", "degrees", "25.0", None),
        "--fail": ("Make a test fail", "--fail", None, None, None),
        "--env": ("Env", "--env=<env>", "env", None, ["dev", "prod"]),  # (an f-string help can't be read)
        "-D": ("Dut", "-D <dut>", "dut", None, None),
    }
    assert [o["flag"] for o in opts][:3] == ["--device", "--set_temperature", "--fail"]  # in file order
    assert opts[0]["help"] == "the device under test (default: A)"


def test_options_from_several_conftest_files():
    opts, problems = pb.optscan.options_from_files([
        ("a/conftest.py", CONFTEST),
        ("a/tests/conftest.py", "def pytest_addoption(parser):\n    parser.addoption('--device', help='again')\n"
                                "    parser.addoption('--slow', action='store_true')\n"),
        ("a/broken/conftest.py", "def pytest_addoption(parser:\n"),
    ])
    assert [o["flag"] for o in opts].count("--device") == 1 and opts[0]["file"] == "a/conftest.py"
    assert "--slow" in [o["flag"] for o in opts]
    assert len(problems) == 1 and "a/broken/conftest.py" in problems[0]


def test_conftest_files_from_the_machine_output():
    text = ("noise\nM /w/conftest.py\nline 1\nM line that isn't the end\n\nM\n"
            "M /w/tests/conftest.py\nx = 1\nM\n")
    assert pb.optscan.split_files(text, "M") == [("/w/conftest.py", "line 1\nM line that isn't the end"),
                                                ("/w/tests/conftest.py", "x = 1")]


def test_conftest_files_here(tmp_path):
    top, here = tmp_path / "work", tmp_path / "work" / "proj"
    for d in ("", "proj", "proj/tests", "proj/tests/unit", "proj/venv/lib", "proj/.tox/x", "proj/a/b/c", "proj/a/b/c/d"):
        (top / d).mkdir(parents=True, exist_ok=True)
        (top / d / "conftest.py").write_text(f"# {d}\n")
    (tmp_path / "conftest.py").write_text("# above the workdir: not read\n")
    got = [p for p, _ in pb.optscan.local_files(here, top)]
    rel = [str(__import__("pathlib").Path(p).relative_to(top)) for p in got]
    assert rel == ["proj/conftest.py", "conftest.py", "proj/a/b/c/conftest.py", "proj/tests/conftest.py",  # not d/
                   "proj/tests/unit/conftest.py"]
    assert pb.optscan.local_files(tmp_path / "nope", top) is None


# ---- where the board keeps its data
def test_data_goes_next_to_the_server(tmp_path):
    from pytest_board.cli import data_dir
    server, project = tmp_path / "pytest-board", tmp_path / "project"
    assert data_dir(None, project, server) == (server / ".pytest-board", None)  # not with the project or its ini
    assert data_dir(str(tmp_path / "elsewhere"), project, server) == (tmp_path / "elsewhere", None)
    assert data_dir(None, server, server) == (server / ".pytest-board", None)
    (project / ".pytest-board").mkdir(parents=True)
    (project / ".pytest-board" / "board.db").write_text("")  # an older version's data in the project: kept
    path, note = data_dir(None, project, server)
    assert path == project / ".pytest-board" and "move" in note
    (server / ".pytest-board").mkdir(parents=True)
    (server / ".pytest-board" / "board.db").write_text("")  # once it's been moved, that one is used
    assert data_dir(None, project, server) == (server / ".pytest-board", None)


def test_config_headlines_and_machines(tmp_path):
    g = load(tmp_path, """\
        [SYS]
        title   = System
        machine = lab-a
        options =
            Smoke | -m smoke
            -x
        tests   =
            Quick | test_a.py
        [machine:lab-a]
        host  = 10.0.4.21
        slots = 2
        """)
    assert g.error is None
    sys_ = g.get("SYS")
    assert (sys_["title"], sys_["machine"], sys_["machines"]) == ("System", "lab-a", [])
    assert sys_["options"] == [{"label": "Smoke", "args": "-m smoke", "value": None},
                               {"label": "-x", "args": "-x", "value": None}]
    assert [m["name"] for m in g.machines] == ["lab-a"]
    assert "slots" not in g.machines[0]  # a machine runs one test at a time; the old key is ignored


def test_config_options_that_take_a_value(tmp_path):
    g = load(tmp_path, """\
        [SYS]
        options =
            Temperature | --set_temperature   <degrees>
            Device | --device=<name>
            Two | --a <x> --b <y>
        """)
    assert g.get("SYS")["options"] == [
        {"label": "Temperature", "args": "--set_temperature <degrees>", "value": "degrees"},
        {"label": "Device", "args": "--device=<name>", "value": "name"}]
    assert "one <value> per option" in g.error


def test_config_binding(tmp_path):
    g = load(tmp_path, """\
        [A]
        machines = lab-b, lab-c
        [B]
        machine  = lab-c
        machines = lab-b, lab-c
        [C]
        machine  = lab-x
        machines = lab-b
        """)
    assert (g.get("A")["machine"], g.get("A")["machines"]) == ("lab-b", ["lab-b", "lab-c"])  # first = default
    assert g.get("B")["machine"] == "lab-c"  # machine = is the default when it's one of them
    assert g.get("C")["machine"] == "lab-b" and "isn't one of machines" in g.error


def test_config_rejects_local(tmp_path):
    g = load(tmp_path, """\
        [A]
        machines = local
        """)
    assert g.get("A")["machines"] == [] and "local" in g.error


def test_config_error_keeps_last_good(tmp_path):
    g = load(tmp_path, "[A]\ntitle = first\n")
    (tmp_path / "pytest-board.ini").write_text("[A\nbroken")
    g.load()
    assert g.get("A")["title"] == "first" and g.error


# ---- writing machines into the config file (pytest_board/inifile.py)
def test_inifile_round_trips():
    from pytest_board import inifile
    text = "# my notes\n[SYS]\nmachine = a\n\n# the boxes\n[machine:a]\nhost = a.lab   ; first\n\n[machine:b]\nhost = b\n"
    a = inifile.parse_machine(text, "a", ".")
    assert inifile.update_section(text, "a", "a", a) == text  # nothing changed: byte for byte the same
    added = inifile.add_section(text, "c", pb.normalize_machine({"host": "c.lab", "sync": "yes"}, "c", "ui"))
    assert added.startswith(text) and inifile.parse_machine(added, "c", ".")["sync"] is True
    removed = inifile.remove_section(text, "a")
    assert "[machine:a]" not in removed and "# my notes" in removed and "# the boxes" in removed
    assert inifile.parse_machine(removed, "b", ".")["host"] == "b"
    assert inifile.add_section("", "x", pb.normalize_machine({"host": "x"}, "x", "ui")).startswith("[machine:x]\n")
