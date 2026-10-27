"""pytest-board.ini: the headlines of the test library ([SECTION]) and the machines ([machine:NAME])."""
import configparser
import re
import shlex
import sys
import threading
from pathlib import Path

from .envfiles import env_file_info, parse_env_lines, resolve_env_file
from .model import MACHINE_NAME_RX, OPTION_VALUE_RX, normalize_branch, normalize_machine, normalize_optset

GROUP_COLORS = ["#3b5bdb", "#0f9d8a", "#c2410c", "#9333ea", "#be185d", "#0369a1", "#4d7c0f", "#a16207"]
GROUP_KEY_RX = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")


class Groups:
    """
    Library headlines, read from an INI file. Every [SECTION] is one headline:

        [TEST_SYSTEM]
        title = System under test       ; optional subtitle
        color = #3b5bdb                 ; optional
        dir = tests                     ; optional: pytest runs in <root>/<dir> for this headline
        branch = main                   ; optional: git branch checked out before this headline's tests
        args = -ra                      ; optional: pytest args added before each test's own args
        env =                           ; optional: KEY=value lines, a test's own env overrides them
            BASE_URL=https://system.staging
        env_file = secrets/system.env   ; optional: a .env file on this server (relative to this file)
        tests =                         ; optional: seeds the library (target, or name | target | args)
            test_api.py
            Health check | test_api.py::test_health | -v

    The file is re-read automatically when it changes.
    """

    def __init__(self, path, root):
        self.path = Path(path) if path else None
        self.root = root
        self.mtime = None
        self.items = []
        self.by_key = {}
        self.machines = []  # from [machine:NAME] sections
        self.error = None
        self.lock = threading.Lock()

    def load(self):
        """(Re)load if the file changed. Returns True when the headlines changed."""
        if not self.path:
            return False
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return False
        self.mtime = mtime
        if mtime is None:
            with self.lock:
                self.items, self.by_key, self.machines, self.error = [], {}, [], None
            return True
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",), strict=False)
        try:
            cp.read(str(self.path), encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError) as e:
            with self.lock:
                self.error = f"{self.path.name}: {e}".replace("\n", " ")
            print("headline config error:", self.error, file=sys.stderr)
            return True  # keep the last good headlines, but show the error
        items, problems, machines = [], [], []
        for key in cp.sections():
            s = cp[key]
            if key.lower().startswith("machine:"):  # [machine:NAME] -> an SSH execution host
                if "slots" in s:
                    print(f"[{key}] slots is ignored: a machine runs one test at a time", file=sys.stderr)
                try:
                    machines.append(normalize_machine(dict(s), key.split(":", 1)[1].strip(), "config",
                                                      base_dir=self.base_dir()))
                except ValueError as e:
                    problems.append(f"[{key}]: {e}")
                continue
            if not GROUP_KEY_RX.match(key):
                problems.append(f"[{key}]: use letters, digits, _ . - or spaces")
                continue
            g = {
                "key": key,
                "title": s.get("title", "").strip(),
                "color": s.get("color", "").strip() or GROUP_COLORS[len(items) % len(GROUP_COLORS)],
                "dir": s.get("dir", "").strip(),
                "machine": s.get("machine", "").strip() or None,
                "machines": [],  # machines = a, b: the only machines this headline's tests run on (first = default)
                "args": s.get("args", "").strip(),
                "env": parse_env_lines(s.get("env", "")),
                "env_file": resolve_env_file(s.get("env_file", ""), self.base_dir()),
                "branch": None,
                "tests": [ln.strip() for ln in s.get("tests", "").splitlines()
                          if ln.strip() and not ln.strip().startswith("#")],
                "options": [],  # option sets offered when a test is put on the board: label | pytest args
                # also offer the options the headline's conftest.py files declare (conftest_options = no: don't)
                "conftest_options": s.get("conftest_options", "yes").strip().lower() not in ("no", "false", "off", "0"),
            }
            for ln in s.get("options", "").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                label, sep, args = ln.partition("|")
                if not sep:
                    label, args = "", label
                try:
                    args = normalize_optset(args)
                except ValueError as e:
                    problems.append(f"[{key}] options: {e}")
                    continue
                # an option that takes a value has a <placeholder> where the value goes: --set_temperature <degrees>,
                # --device=<name>. The page asks for the value when the option is ticked.
                holes = OPTION_VALUE_RX.findall(args or "")
                if len(holes) > 1:
                    problems.append(f"[{key}] options: {args}: one <value> per option")
                    continue
                if args and all(o["args"] != args for o in g["options"]):
                    g["options"].append({"label": label.strip() or args, "args": args,
                                         "value": holes[0] if holes else None})
            for name in re.split(r"[\s,]+", s.get("machines", "").strip()):
                if not name or name in g["machines"]:
                    continue
                if name == "local":
                    problems.append(f"[{key}] machines: 'local' is gone (the board server doesn't run tests itself)")
                elif not MACHINE_NAME_RX.match(name):
                    problems.append(f"[{key}] machines: invalid machine name {name!r}")
                else:
                    g["machines"].append(name)
            if g["machines"]:
                if g["machine"] and g["machine"] not in g["machines"]:
                    problems.append(f"[{key}] machine = {g['machine']} isn't one of machines = "
                                    f"{', '.join(g['machines'])}; the default is {g['machines'][0]}")
                    g["machine"] = None
                g["machine"] = g["machine"] or g["machines"][0]
            try:
                shlex.split(g["args"])
            except ValueError as e:
                problems.append(f"[{key}] args: {e}")
                g["args"] = ""
            try:
                g["branch"] = normalize_branch(s.get("branch", ""))
            except ValueError as e:
                problems.append(f"[{key}] {e}")
            items.append(g)
        with self.lock:
            self.items = items
            self.by_key = {g["key"]: g for g in items}
            self.machines = machines
            self.error = "; ".join(problems) or None
        print(f"loaded {len(items)} headline(s), {len(machines)} machine(s) from {self.path}"
              + (f" — {self.error}" if self.error else ""), file=sys.stderr)
        return True

    def get(self, key):
        return self.by_key.get(key) if key else None

    def base_dir(self):
        """Relative paths in the config file (env_file = …) start here."""
        return self.path.parent if self.path else self.root

    def public(self):
        """What the UI may see: env values are left out (they may hold secrets), only their names."""
        with self.lock:
            return [{"key": g["key"], "title": g["title"], "color": g["color"], "dir": g["dir"], "args": g["args"],
                     "machine": g["machine"], "machines": g["machines"], "env_keys": sorted(g["env"]),
                     "seed_count": len(g["tests"]),
                     "options": g["options"], "conftest_options": g["conftest_options"],
                     "env_file": env_file_info(g["env_file"]), "branch": g["branch"]}
                    for g in self.items]
