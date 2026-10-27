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
    assert sys_["options"] == [{"label": "Smoke", "args": "-m smoke"}, {"label": "-x", "args": "-x"}]
    assert [m["name"] for m in g.machines] == ["lab-a"]
    assert "slots" not in g.machines[0]  # a machine runs one test at a time; the old key is ignored


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
