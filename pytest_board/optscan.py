"""Command line options that a project's conftest.py files declare, found without running anything.

pytest only accepts options declared in a pytest_addoption() hook, as parser.addoption("--device", ...) or
parser.getgroup("x").addoption(...). The board reads the conftest.py files pytest would load for a headline (in its
folder, a few levels below it, and the folders above it up to the machine's workdir) and parses them with Python's
ast module: they are never imported or run. Every addoption() call whose names and settings are written out as plain
values becomes an option the option dialog offers, next to the headline's own options = list:

    action="store_true"                     -> --fail                     (a checkbox)
    default="A", metavar="NAME"             -> --device=<name>            (a checkbox with a box for one value)
    nargs="+", metavar="DEGREES"            -> --set_temperature <degrees> (a box for one or more values)
"""
import ast
import re

FLAG_ACTIONS = {"store_true", "store_false", "store_const", "append_const", "count"}  # take no value
SKIP_ACTIONS = {"help", "version"}
OPTION_NAME_RX = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_.\-]*$")
MAX_FILES = 40
MAX_BYTES = 300_000  # per file
_UNSET = object()


def _lit(node, default=None):
    """The value of a plain literal in the source (a string, number, list, ...), else `default`."""
    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return default


def _label(help_text, name):
    """A short label: the help text up to its first aside or clause ("temperatures to test at, one test each, e.g.
    ..." -> "Temperatures to test at"), else the option's name ("--set_temperature" -> "Set temperature")."""
    t = " ".join(str(help_text or "").split())
    if t:
        cut = [i for i in (t.find(s, 8) for s in (" (", ", ", "; ", ": ", ". ", " - ")) if i >= 8]
        if cut:
            t = t[:min(cut)]
        t = t.rstrip(" .,;:")
        if len(t) > 60:
            t = t[:59].rstrip() + "…"
    if not t:
        t = name.lstrip("-").replace("_", " ").replace("-", " ").strip()
    return t[:1].upper() + t[1:]


def _text(v):
    """A default or choice as it would be typed on the command line."""
    if isinstance(v, (list, tuple, set)):
        return " ".join(_text(x) for x in v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def option_from_call(names, kw):
    """One parser.addoption(*names, **kw) call (kw: argument name -> ast node) as a board option, or None.

    {"label", "args", "value", "flag", "help", "default", "choices"}: args is what the card's pytest command gets,
    with a <placeholder> where a typed value goes (as in options = in pytest-board.ini); value is the placeholder's
    name, None for an option that takes no value."""
    action = _lit(kw.get("action"), _UNSET) if "action" in kw else None
    if action in SKIP_ACTIONS:
        return None
    flag = next((n for n in names if n.startswith("--")), names[0])
    nargs = _lit(kw.get("nargs"))
    default = _lit(kw.get("default"), {"store_true": False, "store_false": True}.get(action))
    help_text = _lit(kw.get("help"), "")
    help_text = " ".join(help_text.split()) if isinstance(help_text, str) else ""
    if "%(" in help_text:  # argparse fills these in for --help
        help_text = help_text.replace("%(default)s", _text(default)).replace("%(dest)s", flag.lstrip("-"))
    help_text = help_text.replace("%%", "%")
    takes_value = action not in FLAG_ACTIONS and nargs != 0  # a custom Action class (not a plain value): takes one
    dest = _lit(kw.get("dest"))
    dest = dest if isinstance(dest, str) and dest.strip() else None
    out = {"label": _label(help_text, flag if flag.startswith("--") or not dest else dest), "args": flag, "value": None, "flag": flag, "help": help_text,
           "default": None, "choices": None}
    if not takes_value:
        return out
    meta = _lit(kw.get("metavar"))
    if isinstance(meta, (list, tuple)):
        meta = meta[0] if meta else None
    name = meta if isinstance(meta, str) and meta.strip() else dest or flag.lstrip("-")
    name = re.sub(r"[\s<>]+", "_", name.strip()).lower() or "value"
    several = nargs in ("+", "*") or (isinstance(nargs, int) and nargs > 1)
    # several values: --set_temperature 0 10 20. One value: --device=B (stuck to the option, so a value with spaces
    # stays one argument); a short option takes it after a space: -D B
    out["args"] = f"{flag} <{name}>" if several or not flag.startswith("--") else f"{flag}=<{name}>"
    out["value"] = name
    if default not in (None, "", [], (), False):
        out["default"] = _text(default)
    choices = _lit(kw.get("choices"))
    if isinstance(choices, (list, tuple, set)) and 0 < len(choices) <= 50:
        out["choices"] = [_text(c) for c in choices]
    return out


def parse_conftest(text, path="conftest.py"):
    """The options one conftest.py declares -> (options, problem or None)."""
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as e:
        where = f" (line {e.lineno})" if getattr(e, "lineno", None) else ""
        return [], f"{path}: can't be read{where}: {e.__class__.__name__}"
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "addoption"):
            continue
        names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        names = [n for n in names if OPTION_NAME_RX.match(n)]
        if not names:
            continue  # not written out (addoption(*names)): can't be offered
        o = option_from_call(names, {k.arg: k.value for k in node.keywords if k.arg})
        if o:
            o["file"] = path
            found.append((getattr(node, "lineno", 0), o))
    return [o for _, o in sorted(found, key=lambda x: x[0])], None


def options_from_files(files):
    """[(path, text)] in the order pytest reads them -> (options, problems): each option once (by its flag; the
    first file that declares it wins)."""
    options, problems, seen = [], [], set()
    for path, text in files:
        opts, problem = parse_conftest(text, path)
        if problem:
            problems.append(problem)
        for o in opts:
            if o["flag"] not in seen:
                seen.add(o["flag"])
                options.append(o)
    return options, problems


def remote_script(here, top, marker):
    """sh for the machine: prints every conftest.py pytest would read in `here` (sh words: the headline's folder and
    the machine's workdir), each as "<marker> <path>", its text, then a line with the marker alone."""
    return f"""cd {here} 2>/dev/null || {{ echo PB_NO_DIR; exit 97; }}
HERE=$(pwd -P); TOP=$(cd {top} 2>/dev/null && pwd -P)
{{
  d=$HERE
  while :; do
    [ -f "$d/conftest.py" ] && printf '%s\\n' "$d/conftest.py"
    if [ -z "$TOP" ] || [ "$d" = "$TOP" ] || [ "$d" = / ]; then break; fi
    d=$(dirname "$d")
  done
  find "$HERE" -mindepth 2 -maxdepth 4 -name conftest.py -not -path '*/.*' -not -path '*/venv/*' \\
    -not -path '*/site-packages/*' -not -path '*/node_modules/*' 2>/dev/null | sort
}} | head -n {MAX_FILES} | while IFS= read -r f; do
  printf '%s %s\\n' {marker} "$f"; head -c {MAX_BYTES} "$f"; printf '\\n%s\\n' {marker}
done
"""


def split_files(text, marker):
    """remote_script's output -> [(path, text)]."""
    files, path, buf = [], None, []
    for line in text.split("\n"):
        if path is None:
            if line.startswith(marker + " "):
                path, buf = line[len(marker) + 1:], []
        elif line == marker:
            files.append((path, "\n".join(buf[:-1] if buf and buf[-1] == "" else buf)))
            path = None
        else:
            buf.append(line)
    return files


def local_files(here, top):
    """The same files from a folder on this server (a machine with sync = yes gets its copy from here), or None when
    the folder isn't there."""
    import os
    from pathlib import Path
    here, top = Path(here).resolve(), Path(top).resolve()
    if not here.is_dir():
        return None
    paths, d = [], here
    while True:
        if (d / "conftest.py").is_file():
            paths.append(d / "conftest.py")
        if d == top or d.parent == d or top not in d.parents:
            break
        d = d.parent
    below, skip = [], {"venv", "site-packages", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(here):
        depth = len(Path(dirpath).relative_to(here).parts)
        dirnames[:] = [] if depth >= 3 else sorted(n for n in dirnames if not n.startswith(".") and n not in skip)
        if depth and "conftest.py" in filenames:
            below.append(Path(dirpath) / "conftest.py")
    out = []
    for p in (paths + sorted(below))[:MAX_FILES]:
        try:
            with open(p, "rb") as f:
                out.append((str(p), f.read(MAX_BYTES).decode("utf-8", "replace")))
        except OSError:
            pass
    return out
