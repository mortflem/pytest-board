"""Writing machines into pytest-board.ini: Add appends a [machine:NAME] section, Save changes its lines in place,
Delete removes it. Only that section's lines change, so the rest of the file, comments and spacing stay as they are;
inside the section, a key that is changed keeps its inline comment.

Every edit is checked before it's saved: the new text is parsed the way the board reads the file, and the machine has
to come out exactly as it was meant to. Otherwise nothing is written.
"""
import configparser
import os
import re
import shutil
import tempfile
from pathlib import Path

from .model import normalize_machine

# key = value lines the board writes for a machine, in this order (the defaults are left out)
KEYS = ("host", "user", "port", "identity", "workdir", "python", "sync", "ssh_options", "env_file",
        "remote_env_file", "notes", "env")
DEFAULTS = {"workdir": "~/git", "python": "python3 -m pytest"}
COMMENT_RX = re.compile(r"\s;")  # where an inline comment starts (configparser: ";" after whitespace)
HEADER_RX = re.compile(r"^\[(?P<key>[^\]]+)\]\s*$")
KEY_RX = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[=:]")

NEW_FILE = """\
# pytest-board configuration: headlines of the test library ([SECTION]) and machines ([machine:NAME]).
# The board reads it again within a few seconds of a change. pytest-board.example.ini describes every key.
"""


def values(m, explicit=()):
    """The key = value lines to write for machine m (a normalize_machine() result): {key: text}, env as a list of
    KEY=value lines. Keys with their default value are left out, except the ones in `explicit`."""
    out = {"host": m["host"]}
    for k in ("user", "identity", "ssh_options", "env_file", "remote_env_file", "notes"):
        if m[k]:
            out[k] = m[k]
    if m["port"]:
        out["port"] = str(m["port"])
    for k, default in DEFAULTS.items():
        if (m[k] or default) != default or k in explicit:
            out[k] = m[k] or default
    if m["sync"]:
        out["sync"] = "yes"
    env = [ln.strip() for ln in (m["env_text"] or "").splitlines() if ln.strip()]
    if env:
        out["env"] = env
    return out


def check_values(m):
    """Values the file can't hold as they are (ValueError): they would be cut off at " ;" or at a line break."""
    for k, v in values(m, DEFAULTS).items():
        for line in (v if isinstance(v, list) else [v]):
            if "\n" in line or "\r" in line:
                raise ValueError(f"{k}: one line only")
            if COMMENT_RX.search(line):
                raise ValueError(f"{k}: a ; after a space starts a comment in pytest-board.ini, so the value would "
                                 f"be cut off there ({line!r})")


def _lines_for(key, value, width=8):
    if isinstance(value, list):
        return [f"{key} =\n"] + [f"    {ln}\n" for ln in value]
    return [f"{key.ljust(width)} = {value}\n"]


def section_text(name, m):
    v = values(m, DEFAULTS)  # a new section lists workdir and python too, so they're easy to find and change
    width = max(len(k) for k in v)
    lines = [f"[machine:{name}]\n"]
    for k in KEYS:
        if k in v:
            lines += _lines_for(k, v[k], width)
    return lines


def _find(lines, name):
    """(start, end) of the section [machine:NAME]: start = its header line, end = the index after its last line,
    without the blank lines and comments at its end (those belong to whatever comes next)."""
    start = None
    for i, ln in enumerate(lines):
        h = HEADER_RX.match(ln)
        if not h:
            continue
        if start is not None:
            end = i
            break
        key = h.group("key").strip()
        if key.lower().startswith("machine:") and key.split(":", 1)[1].strip() == name:
            start = i
    else:
        end = len(lines)
    if start is None:
        return None
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith(("#", ";"))):
        end -= 1
    return start, end


def _keys(lines, start, end):
    """{key: (first line, end line)} of the key = value entries in lines[start:end] (continuation lines included)."""
    out, i = {}, start + 1
    while i < end:
        k = KEY_RX.match(lines[i])
        if not k:
            i += 1
            continue
        j = i + 1
        while j < end and lines[j][:1] in (" ", "\t") and lines[j].strip():
            j += 1
        out[k.group("key").lower()] = (i, j)
        i = j
    return out


def _split(line):
    """A key = value line -> (the part up to the value, the value, the inline comment with the space before it)."""
    m = re.match(r"^(\s*[A-Za-z_][A-Za-z0-9_]*\s*[=:]\s?)(.*?)\n?$", line)
    head, rest = m.group(1), m.group(2)
    c = COMMENT_RX.search(rest)
    return (head, rest, "") if not c else (head, rest[:c.start()], rest[c.start():])


def _current(lines, i, j):
    """What an entry holds now: its value text, or (several lines) the list of its lines."""
    _, value, _ = _split(lines[i])
    more = [ln.strip() for ln in lines[i + 1:j] if ln.strip() and not ln.strip().startswith(("#", ";"))]
    if value.strip():
        more.insert(0, value.strip())
    return more if j - i > 1 else value.strip()


def _replace_value(line, value):
    """A key = value line with a new value; an inline comment after it stays where it was if the value fits."""
    head, old, comment = _split(line)
    if not comment:
        return f"{head}{value}\n"
    return f"{head}{value.ljust(len(old)) if len(value) < len(old) else value + ' '}{comment}\n"


def add_section(text, name, m):
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    while lines and not lines[-1].strip():
        lines.pop()
    gap = ["\n"] if lines else []
    return "".join(lines + gap + section_text(name, m))


def update_section(text, old_name, name, m):
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    found = _find(lines, old_name)
    if not found:
        raise KeyError(old_name)
    start, end = found
    if name != old_name:
        lines[start] = f"[machine:{name}]\n"
    have = _keys(lines, start, end)
    want = values(m, explicit=set(have))  # a default value is written only where the key is already
    width = max([len(k) for k in want] + [len(k) for k in have])
    # change or remove what's there (from the bottom up, so the line numbers above stay right); an entry whose
    # value doesn't change keeps its line exactly as it is
    for k, (i, j) in sorted(have.items(), key=lambda kv: -kv[1][0]):
        if k not in KEYS:
            continue  # a key the board doesn't write: left alone
        if k not in want:
            new = []
        elif _current(lines, i, j) == want[k]:
            continue
        elif isinstance(want[k], list):
            new = _lines_for(k, want[k], width)
        else:
            new = [_replace_value(lines[i], want[k])]
        lines[i:j] = new
        end += len(new) - (j - i)
    # add what's missing, after the section's last key
    have = _keys(lines, start, end)
    at = max([j for _, j in have.values()], default=start + 1)
    new = []
    for k in KEYS:
        if k in want and k not in have:
            new += _lines_for(k, want[k], width)
    lines[at:at] = new
    return "".join(lines)


def remove_section(text, name):
    lines = text.splitlines(keepends=True)
    found = _find(lines, name)
    if not found:
        raise KeyError(name)
    start, end = found
    del lines[start:end]
    # no runs of blank lines where the section was
    while start < len(lines) and start > 0 and not lines[start].strip() and not lines[start - 1].strip():
        del lines[start]
    while lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines)


def parse_machine(text, name, base_dir):
    """The machine NAME as the board would read it from this text (None when it isn't there)."""
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",), strict=False)
    cp.read_string(text)
    for key in cp.sections():
        if key.lower().startswith("machine:") and key.split(":", 1)[1].strip() == name:
            return normalize_machine(dict(cp[key]), name, "config", base_dir=base_dir)
    return None


def same(a, b):
    """The fields a machine is written with: equal in a and b?"""
    keys = ("host", "user", "port", "identity", "workdir", "python", "sync", "ssh_options", "env_file",
            "remote_env_file", "notes", "env")
    return all(a[k] == b[k] for k in keys), [k for k in keys if a[k] != b[k]]


def save(path, text):
    """Replace the file's text (through a temporary file in the same folder, so a crash leaves the old or the new
    file, never half of one). Follows a symlink, and keeps the file's permissions."""
    path = Path(path).resolve()
    fd, tmp = tempfile.mkstemp(prefix=".pytest-board-", suffix=".ini", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(str(path), tmp)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
