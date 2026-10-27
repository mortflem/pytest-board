"""The JSON API under /api/: every action of the page, as App's methods."""
import json
import re
import shlex
import socket
import sys
import threading
import time
from pathlib import Path

from . import BOARD_VERSION, page_stamp
from .config import GROUP_KEY_RX
from .cron import Cron
from .envfiles import env_file_info
from .model import (
    COLUMNS,
    LOCAL_GONE,
    MACHINE_FIELDS,
    NO_MACHINE,
    SYSTEM_ONLY,
    ApiError,
    fill_hidden_env,
    normalize_branch,
    normalize_machine,
    normalize_optset,
    parse_optsets,
)
from .shells import Shells
from .system import IS_WIN, SERVICE_USER


def _grp(body):
    """Normalise a headline key from a request body ('' / null -> no headline)."""
    v = body.get("grp")
    v = str(v).strip() if v is not None else ""
    if v and not GROUP_KEY_RX.match(v):
        raise ApiError(400, "invalid headline name")
    return v or None


def _default_name(target):
    t = target.strip().strip("'\"").rstrip("/")
    if t in ("", "."):
        return "all tests"
    return t.split("::")[-1] if "::" in t else (Path(t).stem or t)


def validate_schedule(body):
    """Return (run_at, cron) from a move/schedule body."""
    cron = (body.get("cron") or "").strip() or None
    if cron:
        try:
            return Cron(cron).next_after(time.time()), cron
        except ValueError as e:
            raise ApiError(400, str(e))
    run_at = body.get("run_at")
    if run_at is None:
        raise ApiError(400, "scheduling needs run_at (epoch seconds) or cron")
    return float(run_at), None


class App:
    def __init__(self, store, runner, token):
        self.s = store
        self.r = runner
        self.token = token
        self.loopback_only = False  # set by main(): True when bound to localhost (enables the Host check)
        self.shells = Shells(runner)
        self.routes = []
        R = self.route
        R("GET", r"/api/board", self.board)
        R("POST", r"/api/tests", self.test_create)
        R("POST", r"/api/tests/bulk", self.test_bulk)
        R("GET", r"/api/tests/(\d+)", self.test_get)
        R("PATCH", r"/api/tests/(\d+)", self.test_update)
        R("DELETE", r"/api/tests/(\d+)", self.test_delete)
        R("POST", r"/api/tests/(\d+)/copy", self.test_copy)
        R("POST", r"/api/cards/clear", self.cards_clear)
        R("POST", r"/api/cards/(\d+)/save-to-library", self.card_to_test)
        R("POST", r"/api/cards", self.card_create)
        R("POST", r"/api/cards/bulk", self.card_bulk)
        R("GET", r"/api/cards/(\d+)", self.card_get)
        R("PATCH", r"/api/cards/(\d+)", self.card_update)
        R("DELETE", r"/api/cards/(\d+)", self.card_delete)
        R("POST", r"/api/cards/(\d+)/move", self.card_move)
        R("POST", r"/api/cards/(\d+)/run", self.card_run)
        R("POST", r"/api/cards/(\d+)/stop", self.card_stop)
        R("GET", r"/api/runs/(\d+)/log", self.run_log)
        R("GET", r"/api/runs/(\d+)/junit", self.run_junit)
        R("POST", r"/api/runs/(\d+)/attach", self.run_attach)  # a terminal in the run's screen session
        R("GET", r"/api/discover", self.discover)
        R("POST", r"/api/machines", self.machine_create)
        R("POST", r"/api/machines/check-all", self.machines_check_all)
        R("PATCH", r"/api/machines/([A-Za-z0-9_.\-]+)", self.machine_update)
        R("DELETE", r"/api/machines/([A-Za-z0-9_.\-]+)", self.machine_delete)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/check", self.machine_check)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/run", self.machine_run)  # "Run lane"
        R("POST", r"/api/cron/preview", self.cron_preview)
        R("POST", r"/api/machines/([A-Za-z0-9_.\-]+)/shell", self.shell_open)
        R("GET", r"/api/shells/(s[0-9a-f]+)", self.shell_read)
        R("POST", r"/api/shells/(s[0-9a-f]+)/input", self.shell_input)
        R("POST", r"/api/shells/(s[0-9a-f]+)/resize", self.shell_resize)
        R("DELETE", r"/api/shells/(s[0-9a-f]+)", self.shell_close)

    def route(self, method, pattern, fn):
        self.routes.append((method, re.compile(pattern + r"$"), fn))

    # ---- headline config file
    def reload_groups(self):
        """Called at start-up and every few seconds; re-reads the INI when it changed."""
        if not self.r.groups.load():
            return
        with self.s.lock:
            for g in self.r.groups.items:
                for spec in g["tests"]:
                    if self.s.one("SELECT 1 FROM seeded WHERE grp=? AND spec=?", (g["key"], spec)):
                        continue  # seeded before; if someone deleted it in the UI it stays deleted
                    # target  |  name | target  |  name | target | args  |  name | target | args | machine
                    parts = [p.strip() for p in spec.split("|")]
                    if len(parts) == 1:
                        name, target, args, machine = None, parts[0], "", ""
                    else:
                        name, target = parts[0], parts[1]
                        args = parts[2] if len(parts) > 2 else ""
                        machine = parts[3] if len(parts) > 3 else ""
                    if machine:
                        print(f"[{g['key']}] test {spec!r}: the machine field ({machine}) is ignored: library tests "
                              "have no machine of their own (copies run on the row they are dropped into)",
                              file=sys.stderr)
                    if not self.s.one("SELECT 1 FROM tests WHERE grp=? AND target=? AND args=?",
                                      (g["key"], target, args)):
                        try:
                            self.test_create({}, {"name": name or _default_name(target), "target": target,
                                                  "args": args, "grp": g["key"]})
                        except ApiError as e:
                            print(f"[{g['key']}] test {spec!r}: {e}", file=sys.stderr)
                            continue
                    self.s.x("INSERT OR IGNORE INTO seeded(grp, spec) VALUES(?,?)", (g["key"], spec))
            self.s.version += 1  # make every open browser re-render

    def watch_groups(self):
        while True:
            try:
                self.reload_groups()
            except Exception as e:
                print("headline reload error:", e, file=sys.stderr)
            time.sleep(2)

    # ---- board
    def board(self, q, body):
        since = q.get("since")
        if since and since == str(self.s.version):
            return {"version": self.s.version, "unchanged": True, "page": page_stamp(), "board_version": BOARD_VERSION}
        cards = self.s.q("""
            SELECT c.*, r.outcome AS last_outcome, r.started AS last_started, r.finished AS last_finished,
                   r.total AS last_total, r.passed AS last_passed, r.failed AS last_failed,
                   r.errors AS last_errors, r.skipped AS last_skipped, r.exit_code AS last_exit
            FROM cards c LEFT JOIN runs r ON r.id = c.last_run_id
            ORDER BY c.position, c.id""")
        tests = self.s.q("""
            SELECT t.*,
                   (SELECT COUNT(*) FROM cards c WHERE c.test_id=t.id) AS copies,
                   (SELECT COUNT(*) FROM cards c WHERE c.test_id=t.id AND c.status IN ('queued','running')) AS active,
                   (SELECT COUNT(*) FROM runs WHERE test_id=t.id) AS run_count,
                   r.outcome AS last_outcome, r.started AS last_started, r.finished AS last_finished,
                   r.total AS last_total, r.passed AS last_passed, r.failed AS last_failed,
                   r.errors AS last_errors, r.skipped AS last_skipped, r.exit_code AS last_exit
            FROM tests t
            LEFT JOIN runs r ON r.id = (SELECT MAX(id) FROM runs WHERE test_id=t.id AND outcome<>'running')
            ORDER BY t.position, t.id""")
        for c in cards:  # a run on a machine whose connection is down: "reconnecting" on the card
            link = self.r.links.get(c["id"])
            if link and c["status"] == "running":
                c["link"] = link
        return {
            "version": self.s.version,
            "columns": COLUMNS,
            "cards": cards,
            "tests": tests,
            "groups": self.r.groups.public(),
            "machines": self.machines_public(),
            "groups_config": {
                "path": str(self.r.groups.path) if self.r.groups.path else None,
                "exists": self.r.groups.mtime is not None,
                "error": self._config_error(),
                "env_file": env_file_info(self.r.env_file),  # .env for every run (names only)
            },
            "server": {
                "root": str(self.r.root),
                "now": time.time(),
                "tz": time.strftime("%Z"),
                "user": SERVICE_USER,  # ssh to machines runs as this account (shown in "Help setup")
                "os": "windows" if IS_WIN else sys.platform,  # Help setup shows PowerShell commands on Windows
                "sync": self.r.sync_method(),                 # rsync, or tar over ssh
                "hostname": socket.gethostname(),
                "board_version": BOARD_VERSION,
            },
            "page": page_stamp(),
        }

    def _config_error(self):
        """Problems in the config file, plus headlines naming a machine (machine = / machines =) that exists neither
        in the file nor in the UI (machines added in the UI are fine to name)."""
        problems = [self.r.groups.error] if self.r.groups.error else []
        for g in self.r.groups.items:
            if g["machine"] == "local":
                problems.append(f"[{g['key']}] machine = local: the board server doesn't run tests itself any more; "
                                "name one of your machines")
            # machines = a, b (the default is the first, or machine = if it is one of them); else just machine =
            for name in g["machines"] or ([g["machine"]] if g["machine"] and g["machine"] != "local" else []):
                if not self.r.machines.get(name):
                    problems.append(f"[{g['key']}] machine '{name}' isn't defined: add it in the Machines column "
                                    f"or as a [machine:{name}] section")
        return "; ".join(problems) or None

    # ---- swimlanes: one per machine, plus "No machine" for cards without one. A card's lane is the machine it runs on.
    def _default_lane(self):
        """cards.lane_id is internal now (lanes are machines); every card sits in one hidden lane row."""
        row = self.s.one("SELECT id FROM lanes ORDER BY position, id LIMIT 1")
        return row["id"] if row else self.s.x("INSERT INTO lanes(name, position) VALUES('board', 1)")

    def _lane_machine(self, value, current=None):
        """Validate a lane (machine) a card is dropped into. Returns the value to store in cards.machine
        (None: keep it as it is)."""
        v = str(value or "").strip()
        if v == (current or ""):
            return None  # already runs there (maybe via its headline), or has no machine and stays in that row
        if not v:
            raise ApiError(400, "drop the card into a machine's row")
        if not self.r.machines.get(v):
            raise ApiError(400, LOCAL_GONE if v == "local" else f"unknown machine '{v}'")
        return v

    def _check_bound(self, item, mname=None):
        why = self.r.not_bound(item, mname)
        if why:
            raise ApiError(409, why)

    def machine_run(self, q, body, name):
        """Queue every card in a machine's lane (optionally only one headline's: {"grp": ...})."""
        name = str(name)
        if not self.r.machines.get(name) and not any(
                self.r.resolve_machine(c)[0] == name for c in self.s.q("SELECT machine, grp FROM cards")):
            raise ApiError(404, "no such machine")
        only_grp = "grp" in body
        grp = _grp(body) if only_grp else None
        only_set = "optset" in body  # one option-set group (null = the cards without an option set)
        try:
            optset = normalize_optset(body.get("optset")) if only_set else None
        except ValueError as e:
            raise ApiError(400, str(e))
        statuses = body.get("statuses")  # e.g. ["backlog"]: only the cards in those columns
        if statuses is not None and (not isinstance(statuses, list) or not set(statuses) <= set(COLUMNS)):
            raise ApiError(400, "statuses must be a list of columns")
        now = time.time()
        n = skipped = 0
        with self.s.lock:
            for c in self.s.q("SELECT id, machine, grp, optset, status FROM cards WHERE status NOT IN ('running','queued') "
                              "ORDER BY position, id"):
                if self.r.resolve_machine(c)[0] != name or (only_grp and c["grp"] != grp) or (
                        only_set and c["optset"] != optset) or (statuses is not None and c["status"] not in statuses):
                    continue
                if self.r.not_bound(c, name):  # its headline's tests don't run on this machine (machines =)
                    skipped += 1
                    continue
                self.s.x("UPDATE cards SET status='queued', queued_at=?, pending_trigger='manual' WHERE id=?",
                         (now + n * 1e-6, c["id"]))
                n += 1
        self.r.wake.set()
        return {"queued": n, "skipped": skipped}

    # ---- machines
    def _machine_ref(self, body):
        """'' / null -> inherit (the headline's machine); else a known machine."""
        v = str(body.get("machine") or "").strip()
        if not v:
            return None
        if not self.r.machines.get(v):
            raise ApiError(400, LOCAL_GONE if v == "local" else f"unknown machine '{v}'")
        return v

    def machines_public(self):
        st = self.r.machines.status
        out = []
        for m in self.r.machines.all():
            d = {k: m[k] for k in ("name", "host", "user", "port", "identity", "workdir", "python", "sync",
                                   "ssh_options", "notes", "source", "env_file", "remote_env_file")}
            d["env_file_info"] = env_file_info(m["env_file_path"])
            d["env_keys"] = sorted(m["env"])
            if m["source"] == "ui":
                d["env"] = m["env_text"]  # typed in the UI, so it may be shown there again
            d["busy"] = self.r.busy.get(m["name"], 0)
            d["status"] = st.get(m["name"], {"state": "unknown"})
            d["used_by"] = self.s.one("SELECT (SELECT COUNT(*) FROM cards WHERE machine=?) + "
                                      "(SELECT COUNT(*) FROM tests WHERE machine=?) n", (m["name"], m["name"]))["n"]
            out.append(d)
        return out

    def _machine_row(self, name):
        name = str(name)
        row = self.s.one("SELECT * FROM machines WHERE name=?", (name,))
        if not row or any(m["name"] == name for m in self.r.groups.machines):
            if self.r.machines.get(name):
                raise ApiError(409, f"'{name}' is defined in {self.r.groups.path.name}; edit it there")
            raise ApiError(404, "no such machine")
        return row

    def machine_create(self, q, body):
        if body.get("env_from"):  # a copy of another machine ("Copy settings from" / Duplicate)
            src = self.r.machines.get(str(body["env_from"]))
            if not src:
                raise ApiError(400, f"can't copy settings from unknown machine '{body['env_from']}'")
            body = dict(body, env=fill_hidden_env(body.get("env"), src["env"]))
        m = normalize_machine(body, body.get("name"), "ui", base_dir=self.r.groups.base_dir())
        if self.r.machines.get(m["name"]):
            raise ApiError(409, f"a machine called '{m['name']}' already exists")
        pos = (self.s.one("SELECT MAX(position) p FROM machines") or {}).get("p") or 0
        now = time.time()
        self.s.x("INSERT INTO machines(name, host, user, port, identity, workdir, python, sync, env, "
                 "ssh_options, notes, env_file, remote_env_file, position, created, updated) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (m["name"], m["host"], m["user"], m["port"], m["identity"], m["workdir"], m["python"],
                  int(m["sync"]), m["env_text"], m["ssh_options"], m["notes"], m["env_file"], m["remote_env_file"],
                  pos + 1, now, now))
        threading.Thread(target=self.r._check_safe, args=(self.r.machines.get(m["name"]),), daemon=True).start()
        return {"ok": True, "name": m["name"]}

    def machine_update(self, q, body, name):
        row = self._machine_row(name)
        merged = dict(row, **{k: v for k, v in body.items() if k in MACHINE_FIELDS})
        new_name = str(body.get("name") or row["name"]).strip()
        m = normalize_machine(merged, new_name, "ui", row["id"], base_dir=self.r.groups.base_dir())
        if new_name != row["name"] and self.r.machines.get(new_name):
            raise ApiError(409, f"a machine called '{new_name}' already exists")
        if new_name != row["name"] and self.r.busy.get(row["name"]):
            raise ApiError(409, "can't rename a machine while it is running tests")
        with self.s.lock:
            self.s.x("UPDATE machines SET name=?, host=?, user=?, port=?, identity=?, workdir=?, python=?, "
                     "sync=?, env=?, ssh_options=?, notes=?, env_file=?, remote_env_file=?, updated=? WHERE id=?",
                     (m["name"], m["host"], m["user"], m["port"], m["identity"], m["workdir"], m["python"],
                      int(m["sync"]), m["env_text"], m["ssh_options"], m["notes"], m["env_file"],
                      m["remote_env_file"], time.time(), row["id"]))
            if new_name != row["name"]:
                for t in ("cards", "tests"):
                    self.s.x(f"UPDATE {t} SET machine=? WHERE machine=?", (new_name, row["name"]))
                self.r.machines.status.pop(row["name"], None)
        threading.Thread(target=self.r._check_safe, args=(self.r.machines.get(new_name),), daemon=True).start()
        self.r.wake.set()  # a changed machine may let queued cards start
        return {"ok": True, "name": new_name}

    def machine_delete(self, q, body, name):
        row = self._machine_row(name)
        if self.r.busy.get(row["name"]):
            raise ApiError(409, "this machine is running tests; stop them first")
        with self.s.lock:
            self.s.x("DELETE FROM machines WHERE id=?", (row["id"],))
            n = 0
            for t in ("cards", "tests"):
                n += self.s.one(f"SELECT COUNT(*) n FROM {t} WHERE machine=?", (row["name"],))["n"]
                self.s.x(f"UPDATE {t} SET machine=NULL WHERE machine=?", (row["name"],))
        self.r.machines.status.pop(row["name"], None)
        return {"ok": True, "reset": n}

    def machine_check(self, q, body, name):
        m = self.r.machines.get(str(name))
        if not m:
            raise ApiError(404, "no such machine")
        return self.r.check_machine(m) or {}

    def machines_check_all(self, q, body):
        ms = self.r.machines.all()
        for m in ms:
            threading.Thread(target=self.r._check_safe, args=(m,), daemon=True).start()
        return {"checking": len(ms)}

    # ---- test library (templates; dragging one onto the board makes a card copy)
    def _test_fields(self, body, partial=False):
        f = self._card_fields({k: v for k, v in body.items() if k in ("target", "args", "env", "timeout")},
                              partial=True)
        if "name" in body:
            f["name"] = str(body["name"] or "").strip()
        if "grp" in body:
            f["grp"] = _grp(body)
        # no "machine": library tests have none of their own (an older page may still send it: ignored)
        if not partial:
            f.setdefault("target", "")
            if not f.get("name"):
                f["name"] = f["target"] or "all tests"
        elif "name" in f and not f["name"]:
            raise ApiError(400, "name can't be empty")
        return f

    def test_create(self, q, body):
        f = self._test_fields(body)
        m = self.s.one("SELECT MAX(position) m FROM tests")
        f["position"] = ((m and m["m"]) or 0) + 1
        f["created"] = f["updated"] = time.time()
        tid = self.s.x(f"INSERT INTO tests({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return self.test_get(q, body, tid)

    def test_bulk(self, q, body):
        grp = _grp(body)
        existing = {r["target"] for r in self.s.q("SELECT target FROM tests WHERE grp IS ?", (grp,))}
        n = 0
        for t in body.get("targets") or []:
            target = shlex.quote(t)
            if target in existing:
                continue  # already under this headline
            self.test_create(q, {"name": _default_name(t), "target": target, "grp": grp})
            existing.add(target)
            n += 1
        return {"created": n, "skipped": len(body.get("targets") or []) - n}

    def test_get(self, q, body, tid):
        t = self.s.one("SELECT * FROM tests WHERE id=?", (tid,))
        if not t:
            raise ApiError(404, "no such test")
        t["runs"] = self.s.q("SELECT * FROM runs WHERE test_id=? ORDER BY id DESC LIMIT 100", (tid,))
        t["copies"] = self.s.q("SELECT id, status, cron, run_at, machine, grp, optset FROM cards WHERE test_id=? ORDER BY id",
                               (tid,))
        for c in t["copies"]:
            c["lane"] = self.r.resolve_machine(c)[0]
        self._describe_run_setup(t)
        return t

    def _describe_run_setup(self, item):
        pv = self.r.preview(item)
        item.update(pv)
        item["cwd"] = pv["where"]
        g = self.r.groups.get(item.get("grp"))
        item["group_env_keys"] = sorted(g["env"]) if g else []
        item["group_known"] = bool(g) or not item.get("grp")

    def test_update(self, q, body, tid):
        f = self._test_fields(body, partial=True)
        if f:
            f["updated"] = time.time()
            self.s.x(f"UPDATE tests SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), tid))
        return self.test_get(q, body, tid)

    def test_delete(self, q, body, tid):
        # copies already on the board stay (they are independent); run history is kept but unlinked
        self.s.x("DELETE FROM tests WHERE id=?", (tid,))
        return {"ok": True}

    def test_copy(self, q, body, tid):
        t = self.s.one("SELECT * FROM tests WHERE id=?", (tid,))
        if not t:
            raise ApiError(404, "no such test")
        status = body.get("status", "backlog")
        if status not in ("backlog", "scheduled", "queued"):
            raise ApiError(409, "copies can go to Backlog, Scheduled or Queued")
        if status == "scheduled":
            validate_schedule(body)  # fail before creating anything
        # dropped into a machine's lane -> the copy runs there; otherwise (Run / Schedule in the test's window) it
        # follows the headline's machine
        machine = None
        if body.get("machine"):
            machine = self._lane_machine(body["machine"], self.r.resolve_machine(t)[0]) or machine
        if status != "backlog" and not self.r.resolve_machine({"machine": machine, "grp": t["grp"]})[0]:
            raise ApiError(409, NO_MACHINE)
        self._check_bound({"machine": machine, "grp": t["grp"]})
        # option sets: one copy each, queued in the order given ({"optsets": ["-x", "-m slow", null]})
        try:
            if "optsets" in body:
                sets = parse_optsets(body["optsets"])
                self.s.x("UPDATE tests SET optsets=? WHERE id=?", (json.dumps(sets) if sets else None, tid))  # next time
                if body.get("combine"):  # one card with all of them ({"combine": true}, what the UI sends)
                    sets = [normalize_optset(" ".join(x for x in sets if x))]
            else:
                sets = [normalize_optset(body.get("optset"))]
        except ValueError as e:
            raise ApiError(400, str(e))
        # extra pytest arguments for every copy ({"extra_args": "--set_temperature 0 10 20"}): they go into each
        # card's own args, after the test's args, and are offered again next time
        args = t["args"]
        if "extra_args" in body:
            extra = str(body.get("extra_args") or "").strip()
            try:
                shlex.split(extra)
            except ValueError as e:
                raise ApiError(400, f"extra arguments: {e}")
            self.s.x("UPDATE tests SET extra_args=? WHERE id=?", (extra or None, tid))
            args = " ".join(x for x in (t["args"], extra) if x)
        move = {k: v for k, v in body.items() if k not in ("machine", "optsets", "optset", "extra_args")}
        made, base = [], time.time()
        with self.s.lock:
            for i, optset in enumerate(sets or [None]):
                card = self.card_create(q, {"title": t["name"], "target": t["target"], "args": args,
                                            "env": t["env"], "timeout": t["timeout"], "grp": t["grp"]})
                self.s.x("UPDATE cards SET test_id=?, machine=?, optset=? WHERE id=?",
                         (tid, machine, optset, card["id"]))
                res = self.card_move(q, move, card["id"])
                if status == "queued":  # keep the list order in the queue
                    self.s.x("UPDATE cards SET queued_at=? WHERE id=?", (base + i * 1e-6, card["id"]))
                made.append(res)
        res = dict(made[-1], created=len(made), cards=[c["id"] for c in made], optsets=sets)
        return res

    def card_to_test(self, q, body, cid):
        c = self.s.one("SELECT * FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        grp = _grp(body) if "grp" in body else c["grp"]
        t = self.test_create(q, {"name": c["title"], "target": c["target"], "args": c["args"],
                                 "env": c["env"], "timeout": c["timeout"], "grp": grp})
        if c.get("optset"):  # pre-ticked next time the test is put on the board
            self.s.x("UPDATE tests SET optsets=? WHERE id=?", (json.dumps([c["optset"]]), t["id"]))
        if grp == c["grp"]:
            # same headline: the card now counts as a copy of the new library test (and brings its history)
            self.s.x("UPDATE cards SET test_id=? WHERE id=?", (t["id"], cid))
            self.s.x("UPDATE runs SET test_id=? WHERE card_id=? AND test_id IS NULL", (t["id"], cid))
        # different headline: the card itself is left untouched (it keeps running with its own headline)
        return t

    def cards_clear(self, q, body):
        """Remove finished cards from the board (their run history stays). Optional filters: "machine" (one row
        of the board: the cards that run there; "" or null = the "No machine" row) and "grp" (one headline; null =
        cards without one)."""
        statuses = [s for s in (body.get("statuses") or ["passed", "failed"]) if s in ("passed", "failed", "backlog")]
        if not statuses:
            raise ApiError(400, "nothing to clear")
        only_machine = "machine" in body
        machine = str(body.get("machine") or "") or None
        only_grp = "grp" in body
        grp = _grp(body) if only_grp else None
        with self.s.lock:
            ids = [c["id"] for c in self.s.q(f"SELECT id, machine, grp FROM cards WHERE status IN "
                                              f"({','.join('?' * len(statuses))})", statuses)
                   if (not only_machine or self.r.resolve_machine(c)[0] == machine)
                   and (not only_grp or c["grp"] == grp)]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.s.x(f"DELETE FROM cards WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        return {"cleared": len(ids)}

    # ---- cards
    def _card_fields(self, body, partial=False):
        out = {}
        for f in ("title", "target", "args", "env"):
            if f in body:
                out[f] = str(body[f] or "").strip() if f != "env" else str(body[f] or "")
        if "timeout" in body:
            out["timeout"] = max(0, int(body["timeout"] or 0))
        if "grp" in body:
            out["grp"] = _grp(body)
        if "machine" in body:
            out["machine"] = self._machine_ref(body)
        if "optset" in body:
            try:
                out["optset"] = normalize_optset(body["optset"])
            except ValueError as e:
                raise ApiError(400, str(e))
        if "branch" in body:
            try:
                out["branch"] = normalize_branch(body["branch"])
            except ValueError as e:
                raise ApiError(400, str(e))
        for f in ("target", "args"):
            if f in out:
                try:
                    shlex.split(out[f])
                except ValueError as e:
                    raise ApiError(400, f"{f}: {e}")
        if not partial:
            if not out.get("target") and not out.get("args"):
                out.setdefault("target", "")
            out.setdefault("title", out.get("target") or "all tests")
        return out

    def _end_pos(self, status):
        m = self.s.one("SELECT MAX(position) m FROM cards WHERE status=?", (status,))
        return (m and m["m"] or 0) + 1

    def card_create(self, q, body):
        f = self._card_fields(body)
        self._check_bound(f)
        if "lane_id" not in f:
            f["lane_id"] = self._default_lane()
        f["position"] = self._end_pos("backlog")
        f["created"] = f["updated"] = time.time()
        cols = ",".join(f)
        cid = self.s.x(f"INSERT INTO cards({cols}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return self.card_get(q, body, cid)

    def card_bulk(self, q, body):
        made = []
        for t in body.get("targets") or []:
            title = t.split("::")[-1] if "::" in t else Path(t).stem
            made.append(self.card_create(q, {"target": shlex.quote(t), "title": title, "grp": body.get("grp"),
                                             "machine": body.get("machine")}))
        return {"created": len(made)}

    def card_get(self, q, body, cid):
        c = self.s.one("SELECT * FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        t = self.s.one("SELECT name FROM tests WHERE id=?", (c["test_id"],)) if c.get("test_id") else None
        c["test_name"] = t and t["name"]
        c["runs"] = self.s.q("SELECT * FROM runs WHERE card_id=? ORDER BY id DESC LIMIT 50", (cid,))
        for r in c["runs"]:  # the screen session a running run can be attached to
            info = self.r._remote_info(r) if r["outcome"] == "running" else None
            r.pop("remote", None)
            if info and info.get("base") is not None:
                r["session"] = info.get("session")
                r["detached"] = True
        if self.r.links.get(cid) and c["status"] == "running":
            c["link"] = self.r.links[cid]
        self._describe_run_setup(c)
        if c["cron"]:
            try:
                cr, t, nxt = Cron(c["cron"]), time.time(), []
                for _ in range(3):
                    t = cr.next_after(t)
                    nxt.append(t)
                c["next_fires"] = nxt
            except ValueError:
                pass
        return c

    def card_update(self, q, body, cid):
        if "optset" in body:
            c = self.s.one("SELECT status, optset FROM cards WHERE id=?", (cid,))
            try:
                new = normalize_optset(body["optset"])
            except ValueError as e:
                raise ApiError(400, str(e))
            if c and c["status"] == "running" and new != c["optset"]:
                raise ApiError(409, "card is running — stop it first, then change its option set")
        if "branch" in body:
            c = self.s.one("SELECT status, branch FROM cards WHERE id=?", (cid,))
            try:
                new = normalize_branch(body["branch"])
            except ValueError as e:
                raise ApiError(400, str(e))
            if c and c["status"] == "running" and new != c["branch"]:
                raise ApiError(409, "card is running — stop it first, then change its branch")
        if "cron" in body or "run_at" in body:  # check before changing anything
            c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
            if not c:
                raise ApiError(404, "no such card")
            if c["status"] != "scheduled":
                raise ApiError(409, "only Scheduled cards have a schedule; move the card to Scheduled instead")
            validate_schedule(body)
        f = self._card_fields(body, partial=True)
        if "machine" in f or "grp" in f:  # a headline with machines = runs its tests only there
            c = self.s.one("SELECT machine, grp FROM cards WHERE id=?", (cid,))
            if c:
                self._check_bound({**c, **f})
        if f:
            f["updated"] = time.time()
            sets = ",".join(f"{k}=?" for k in f)
            self.s.x(f"UPDATE cards SET {sets} WHERE id=?", (*f.values(), cid))
        if "cron" in body or "run_at" in body:
            c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
            if c and c["status"] == "scheduled":
                run_at, cron = validate_schedule(body)
                self.s.x("UPDATE cards SET run_at=?, cron=? WHERE id=?", (run_at, cron, cid))
        return self.card_get(q, body, cid)

    def card_delete(self, q, body, cid):
        self.r.stop(cid)
        self.s.x("DELETE FROM cards WHERE id=?", (cid,))
        return {"ok": True}

    def card_move(self, q, body, cid):
        with self.s.lock:
            c = self.s.one("SELECT * FROM cards WHERE id=?", (cid,))
            if not c:
                raise ApiError(404, "no such card")
            status = body.get("status", c["status"])
            if status not in COLUMNS:
                raise ApiError(400, "unknown column")
            if c["status"] == "running" and status != "running":
                raise ApiError(409, "card is running — stop it first")
            if status in SYSTEM_ONLY and status != c["status"]:
                raise ApiError(409, f"cards move to '{status}' by themselves")
            pos = body.get("position")
            if pos is None:
                pos = self._end_pos(status)
            now = time.time()
            upd = {"status": status, "position": float(pos), "updated": now}
            if "machine" in body:  # dropped into another machine's lane: it runs there from now on
                machine = self._lane_machine(body["machine"], self.r.resolve_machine(c)[0])
                if machine is not None:
                    if c["status"] == "running":
                        raise ApiError(409, "card is running — stop it first")
                    upd["machine"] = machine
            if "optset" in body:  # dropped on an option-set group
                try:
                    optset = normalize_optset(body["optset"])
                except ValueError as e:
                    raise ApiError(400, str(e))
                if optset != c.get("optset"):
                    if c["status"] == "running":
                        raise ApiError(409, "card is running — stop it first")
                    upd["optset"] = optset
            if status in ("scheduled", "queued") and not self.r.resolve_machine({**c, **upd})[0]:
                raise ApiError(409, NO_MACHINE)
            if "machine" in upd or status in ("scheduled", "queued"):
                self._check_bound({**c, **upd})
            if status == "scheduled":
                upd["run_at"], upd["cron"] = validate_schedule(body)
            elif status == "queued" and c["status"] != "queued":
                upd.update(queued_at=now, pending_trigger="manual")
            elif status == "backlog":
                upd.update(run_at=None, cron=None)
            sets = ",".join(f"{k}=?" for k in upd)
            self.s.x(f"UPDATE cards SET {sets} WHERE id=?", (*upd.values(), cid))
        if status == "queued":
            self.r.wake.set()
        return self.card_get(q, body, cid)

    def card_run(self, q, body, cid):
        c = self.s.one("SELECT status, machine, grp FROM cards WHERE id=?", (cid,))
        if not c:
            raise ApiError(404, "no such card")
        if c["status"] in ("running", "queued"):
            raise ApiError(409, f"already {c['status']}")
        if not self.r.resolve_machine(c)[0]:
            raise ApiError(409, NO_MACHINE)
        self._check_bound(c)
        # keeps any cron: after this ad-hoc run the card goes back to Scheduled
        self.s.x("UPDATE cards SET status='queued', queued_at=?, pending_trigger='manual', position=? WHERE id=?",
                 (time.time(), self._end_pos("queued"), cid))
        self.r.wake.set()
        return {"ok": True}

    def card_stop(self, q, body, cid):
        c = self.s.one("SELECT status FROM cards WHERE id=?", (cid,))
        if c and c["status"] == "queued":
            # same as dragging it to Backlog: it is unscheduled, a repeating card stops repeating
            self.s.x("UPDATE cards SET status='backlog', run_at=NULL, cron=NULL, updated=? WHERE id=?",
                     (time.time(), cid))
            return {"ok": True}
        if not self.r.stop(cid):
            raise ApiError(409, "not running")
        return {"ok": True}

    # ---- runs
    def run_log(self, q, body, rid):
        run = self.s.one("SELECT outcome FROM runs WHERE id=?", (rid,))
        if not run:
            raise ApiError(404, "no such run")
        path = self.r.logs / f"run-{rid}.log"
        offset = int(q.get("offset") or 0)
        data = b""
        if path.exists():
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(2_000_000)
        # don't split a multi-byte UTF-8 character across two polls: hold back an incomplete tail
        # (at most 3 bytes); the next poll starts from it
        text = None
        for cut in range(min(4, len(data) + 1) if run["outcome"] == "running" else 0):
            try:
                text = data[:len(data) - cut].decode("utf-8")
                data = data[:len(data) - cut]
                break
            except UnicodeDecodeError:
                continue
        if text is None:  # finished, or not valid UTF-8 anyway (binary output): show it with replacement characters
            text = data.decode("utf-8", "replace")
        return {"text": text, "offset": offset + len(data),
                "done": run["outcome"] != "running"}

    def run_junit(self, q, body, rid):
        path = self.r.junit / f"run-{rid}.xml"
        if not path.exists():
            raise ApiError(404, "no junit report for this run")
        return ("raw", path.read_bytes(), "application/xml")

    def run_attach(self, q, body, rid):
        run = self.s.one("SELECT * FROM runs WHERE id=?", (rid,))
        if not run:
            raise ApiError(404, "no such run")
        if run["outcome"] != "running":
            raise ApiError(409, "this run is over; its output is in the card")
        info = self.r._remote_info(run)
        if not info or info.get("base") is None:
            raise ApiError(409, "this run doesn't run on its own on a machine, so there is nothing to attach to")
        if not info.get("session"):
            raise ApiError(409, f"screen isn't installed on {run['machine']}, so this run has no session to attach "
                                f"to. Install screen there (e.g. sudo apt install screen); later runs get one.")
        m = self.r.machines.get(run["machine"])
        if not m:
            raise ApiError(404, f"machine '{run['machine']}' is not defined any more")
        ses = shlex.quote(info["session"])
        gone = shlex.quote(f"[pytest-board] screen session {info['session']} isn't there any more: the run has "
                           f"just ended (its output is in the card).")
        sh = self.shells.start(m, body.get("cols"), body.get("rows"),
                               script=f"screen -x {ses} || {{ printf '%s\\n' {gone}; sleep 2; exit 1; }}")
        return {"id": sh.id, "machine": m["name"], "session": info["session"],
                "dest": self.r.dest(m) + (f":{m['port']}" if m["port"] else "")}

    def discover(self, q, body):
        return self.r.discover(q.get("grp") or None, q.get("machine") or None)

    # ---- interactive ssh shell on a machine ("Open shell" in the machine dialog)
    def shell_open(self, q, body, name):
        m = self.r.machines.get(str(name))
        if not m:
            raise ApiError(404, "no such machine (save it first)")
        sh = self.shells.start(m, body.get("cols"), body.get("rows"))
        return {"id": sh.id, "machine": m["name"], "dest": self.r.dest(m) + (f":{m['port']}" if m["port"] else ""),
                "workdir": m["workdir"] or "~"}

    def shell_read(self, q, body, sid):
        return self.shells.read(str(sid), int(q.get("offset") or 0))

    def shell_input(self, q, body, sid):
        self.shells.write(str(sid), body.get("data") or "")
        return {"ok": True}

    def shell_resize(self, q, body, sid):
        self.shells.resize(str(sid), body.get("cols"), body.get("rows"))
        return {"ok": True}

    def shell_close(self, q, body, sid):
        self.shells.close(str(sid))
        return {"ok": True}

    def cron_preview(self, q, body):
        try:
            cr, t, out = Cron(body.get("cron") or ""), time.time(), []
            for _ in range(5):
                t = cr.next_after(t)
                out.append(t)
            return {"ok": True, "next": out}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
