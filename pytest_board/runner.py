"""Runner: the queue (which card runs next, where), the scheduler, and a run's life from start to result.

The parts that talk to machines are mixins in their own modules: ssh (connections and env), remote (runs on a
machine: start, follow, collect, tidy, Discover), sync (copying the project) and health (machine checks).
"""
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import Groups
from .cron import Cron
from .envfiles import parse_env_lines
from .health import HealthMixin
from .machines import Machines
from .model import NO_MACHINE
from .remote import RemoteMixin, _Detached
from .ssh import SshMixin
from .sync import SyncMixin
from .system import kill_tree


class Runner(SshMixin, RemoteMixin, SyncMixin, HealthMixin):
    def __init__(self, store, root, data_dir, groups=None, machines=None,
                 ssh_cmd=("ssh",), rsync_cmd=("rsync",), check_interval=120, env_file=None):
        self.store = store
        self.root = root
        self.groups = groups or Groups(None, root)
        self.machines = machines or Machines(store, self.groups)
        self.ssh_cmd = list(ssh_cmd)
        self.rsync_cmd = list(rsync_cmd)
        self.check_interval = check_interval
        self.env_file = Path(env_file) if env_file else None  # .env on this server for every run (--env-file)
        self.logs = data_dir / "logs"
        self.junit = data_dir / "junit"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.junit.mkdir(parents=True, exist_ok=True)
        self.procs = {}   # card_id -> Popen of its run's current step on the machine (ssh connection, sync)
        self.busy = {}    # machine name -> runs in progress there (one at a time; more only for runs resumed after an
                          # upgrade from a version that ran several at once)
        self.active = set()  # card ids from dispatch until the run is recorded (covers sync / ssh start-up)
        self.cancelled = set()
        self.checking = set()
        self.checking_lock = threading.Lock()
        self.wake = threading.Event()
        # runs on machines go on there by themselves (screen session or background process); the board follows
        # their log over ssh and reconnects whenever the connection drops
        self.following = {}   # card id -> machine name, while the board follows a run there
        self.killing = set()  # card ids whose current connection is the one stopping the run on the machine
        self.kill_req = set() # card ids whose connection was ended on purpose (Stop, timeout)
        self.links = {}       # card id -> {"state": "reconnecting", "since": t, "detail": ...} while cut off
        self.nudge = {}       # card id -> Event that wakes a waiting reconnect (Stop, machine back online)
        self.swept = set()    # machines whose leftover run folders were tidied since the board started
        self.stopping = False # the board is shutting down: runs on machines carry on, they are picked up later

    # --- lifecycle
    def start(self):
        s = self.store
        now = time.time()
        # Runs on machines kept going while the board was stopped: follow them again. Anything else that was
        # "running" (not started on its machine yet, or from a version that ran tests on this server) was interrupted.
        resume = []
        for r in s.q("SELECT * FROM runs WHERE outcome='running'"):
            info = self._remote_info(r)
            card = s.one("SELECT * FROM cards WHERE id=?", (r["card_id"],)) if r["card_id"] else None
            m = self.machines.get(r["machine"])
            if info and info.get("base") is not None and m and card and card["status"] == "running" \
                    and card["last_run_id"] == r["id"]:
                resume.append((card, r, info))
            else:
                s.x("UPDATE runs SET outcome='interrupted', finished=? WHERE id=?", (now, r["id"]))
        keep = [c["id"] for c, _, _ in resume]
        s.x(f"UPDATE cards SET status='failed' WHERE status='running' AND id NOT IN ({','.join('?' * len(keep))})",
            keep)
        for card, r, info in resume:
            self.busy[r["machine"]] = self.busy.get(r["machine"], 0) + 1
            self.active.add(card["id"])
            threading.Thread(target=self._job, args=(card, r["id"], r["machine"], (r, info)), daemon=True).start()
        if resume:
            print(f"following {len(resume)} run(s) that went on on their machines while the board was stopped",
                  file=sys.stderr)
        threading.Thread(target=self._scheduler, name="scheduler", daemon=True).start()
        threading.Thread(target=self._dispatch, name="dispatcher", daemon=True).start()
        threading.Thread(target=self._health, name="machine-health", daemon=True).start()

    def _scheduler(self):
        while True:
            try:
                now = time.time()
                with self.store.lock:
                    due = self.store.q(
                        "SELECT id, cron FROM cards WHERE status='scheduled' AND run_at IS NOT NULL AND run_at<=?",
                        (now,))
                    for c in due:
                        self.store.x(
                            "UPDATE cards SET status='queued', queued_at=?, pending_trigger=?, updated=? WHERE id=?",
                            (now, "cron" if c["cron"] else "schedule", now, c["id"]))
                if due:
                    self.wake.set()
            except Exception as e:  # never let the scheduler die
                print("scheduler error:", e, file=sys.stderr)
            time.sleep(2)

    # --- which machine runs a card: the card's own choice > its headline's machine. None: no machine yet (the card
    # waits in the "No machine" row until it gets one; the board server doesn't run tests itself)
    def resolve_machine(self, item):
        if item.get("machine") and item["machine"] != "local":
            return item["machine"], "card"
        g = self.groups.get(item.get("grp"))
        if g and g.get("machine") and g["machine"] != "local":
            return g["machine"], "headline"
        return None, "none"

    def not_bound(self, item, mname=None):
        """Why `item` can't run on machine `mname` (default: the machine it would run on), or None. A headline with
        machines = a, b in pytest-board.ini runs its tests only on those machines."""
        g = self.groups.get(item.get("grp"))
        bound = g.get("machines") if g else None
        if not bound:
            return None
        if mname is None:
            mname = self.resolve_machine(item)[0]
        if not mname or mname in bound:
            return None
        return (f"{g['key']} tests run only on {', '.join(bound)} (machines = in pytest-board.ini), "
                f"not on {mname}")

    # --- which git branch a card runs on: its own > its headline's > none (the checkout is used as it is)
    def resolve_branch(self, item):
        if item.get("branch"):
            return item["branch"], "card"
        g = self.groups.get(item.get("grp"))
        if g and g.get("branch"):
            return g["branch"], "headline"
        return None, "default"

    def _dispatch(self):
        while True:
            try:
                started = self._try_start()
            except Exception as e:
                print("dispatcher error:", e, file=sys.stderr)
                started = False
            if not started:
                self.wake.wait(1.0)
                self.wake.clear()

    def available(self, name):
        """False while a machine is known to be unreachable: its queued cards wait instead of failing."""
        if not name:
            return True  # no machine: let it through so it fails with a clear message
        st = self.machines.status.get(name)
        return not st or st.get("state") != "offline"

    def _try_start(self):
        """Start the oldest queued card whose machine is reachable and isn't running a test (a machine runs one test
        at a time). A card without a machine, or with one that isn't defined, starts too, so it fails with a clear
        message."""
        s = self.store
        with s.lock:
            waiting = set()  # machines with an earlier card waiting: later cards for them wait too
            for c in s.q("SELECT * FROM cards WHERE status='queued' ORDER BY queued_at, position, id"):
                mname, _ = self.resolve_machine(c)
                if mname in waiting:
                    continue
                if self.busy.get(mname) or not self.available(mname):
                    waiting.add(mname)
                    continue
                branch = self.resolve_branch(c)[0]
                now = time.time()
                run_id = s.x("INSERT INTO runs(card_id, test_id, card_title, lane_name, trigger, started, outcome, "
                             "machine, optset, branch) VALUES(?,?,?,?,?,?, 'running', ?, ?, ?)",
                             (c["id"], c["test_id"], c["title"], mname, c["pending_trigger"] or "manual",
                              now, mname, c.get("optset"), branch))
                s.x("UPDATE cards SET status='running', last_run_id=?, updated=? WHERE id=?", (run_id, now, c["id"]))
                self.busy[mname] = self.busy.get(mname, 0) + 1
                self.active.add(c["id"])
                threading.Thread(target=self._job, args=(c, run_id, mname), daemon=True).start()
                return True
        return False

    def _job(self, card, run_id, mname, resume=None):
        try:
            self._execute(card, run_id, mname, resume)
        except _Detached:
            pass  # the board is stopping; the run goes on on its machine and is followed again after the restart
        except Exception as e:
            print("run error:", e, file=sys.stderr)
            self.store.x("UPDATE runs SET outcome='error', finished=? WHERE id=?", (time.time(), run_id))
            self.store.x("UPDATE cards SET status='failed' WHERE id=? AND status='running'", (card["id"],))
        finally:
            with self.store.lock:
                self.active.discard(card["id"])
                self.cancelled.discard(card["id"])
                self.busy[mname] = max(0, self.busy.get(mname, 1) - 1)
                self.store.version += 1
            self.wake.set()

    parse_env = staticmethod(parse_env_lines)  # KEY=value lines (see envfiles)

    # exit codes the remote wrapper uses on top of pytest's own (0-5)
    RC_NO_MACHINE, RC_SYNC_FAILED, RC_NO_WORKDIR, RC_SSH = -2, 96, 97, 255
    RC_WAIT = -3   # the machine could not be reached before anything started: the card goes back to Queued
    RC_LOST = -4   # the run ended on its machine without an exit code (machine restarted, killed from outside)
    RC_CHECKOUT = 95  # the card's git branch couldn't be checked out; pytest didn't start
    RC_NOT_BOUND = -5  # the card's machine isn't one its headline is bound to (machines =); nothing started

    def _execute(self, card, run_id, mname, resume=None):
        """Run a card (or, with resume=(run row, remote info), follow a run that went on while the board was
        stopped) and record the result."""
        s = self.store
        cid = card["id"]
        log_path = self.logs / f"run-{run_id}.log"
        junit_path = self.junit / f"run-{run_id}.xml"
        # the timeout covers the whole run, including a project sync before it
        started = resume[0]["started"] if resume else time.time()
        deadline = started + card["timeout"] if card["timeout"] else None
        with open(log_path, "ab" if resume else "wb") as log:
            m = self.machines.get(mname)
            if resume:
                rc, timed_out = self._follow(card, run_id, m, resume[1], log, junit_path, deadline, resumed=True)
            else:
                if card.get("grp"):
                    log.write(f"[pytest-board] headline {card['grp']}\n".encode())
                if card.get("optset"):
                    log.write(f"[pytest-board] option set: {card['optset']}\n".encode())
                if self.resolve_branch(card)[0]:
                    log.write(f"[pytest-board] git branch: {self.resolve_branch(card)[0]}\n".encode())
                if cid in self.cancelled:
                    rc, timed_out = -1, False  # stopped before it got going
                elif m and self.not_bound(card, mname):
                    log.write(f"[pytest-board] {self.not_bound(card, mname)}. Drag the card to one of those "
                              "machines' rows.\n".encode())
                    rc, timed_out = self.RC_NOT_BOUND, False
                elif m:
                    rc, timed_out = self._run_remote(card, run_id, m, log, junit_path, deadline)
                elif not mname:
                    log.write(f"[pytest-board] {NO_MACHINE}\n".encode())
                    rc, timed_out = self.RC_NO_MACHINE, False
                else:
                    log.write(f"[pytest-board] machine '{mname}' is not defined any more "
                              "(removed from the config file or the UI). Pick another machine for this card.\n"
                              .encode())
                    rc, timed_out = self.RC_NO_MACHINE, False
            finished = time.time()
            counts = self._parse_junit(junit_path)
            cancelled = cid in self.cancelled
            self.cancelled.discard(cid)
            requeue = False
            if cancelled:
                outcome = "cancelled"
            elif timed_out:
                outcome = "timeout"
            elif rc == 0:
                outcome = "passed"
            elif rc == 5:
                outcome = "no-tests"
            elif rc == self.RC_NO_MACHINE:
                outcome = "no-machine"
            elif rc == self.RC_NOT_BOUND:
                outcome = "not-bound"
            elif rc == self.RC_WAIT:
                outcome, requeue = "unreachable", True
            elif rc == self.RC_LOST:
                outcome = "lost"
            elif rc == self.RC_START:
                outcome = "start-failed"
            elif rc == self.RC_CHECKOUT:
                outcome = "checkout-failed"
            elif rc == self.RC_SSH:
                outcome = "unreachable"
            elif rc == self.RC_NO_WORKDIR:
                outcome = "no-workdir"
            elif rc == self.RC_SYNC_FAILED:
                outcome = "sync-failed"
            elif rc == 127:
                outcome = "not-found"
            elif rc == 4:
                outcome = "usage-error"  # pytest rejected the command line
                log.write(b"[pytest-board] pytest didn't accept the command line (exit code 4). Check the card's args "
                          b"and option set: options your own tests add need a pytest_addoption() in conftest.py "
                          b"(see README, Option sets).\n")
            else:
                outcome = "failed"
            if requeue:
                log.write(f"\n[pytest-board] not started: {mname} can't be reached. The card waits in Queued and "
                          f"starts as soon as {mname} answers again.\n".encode())
            elif rc == self.RC_LOST and not (cancelled or timed_out):
                log.write(f"\n[pytest-board] no exit code · run lost · on {mname}\n".encode())
            else:
                log.write(f"\n[pytest-board] exit code {rc} · {outcome}"
                          f"{' · on ' + mname if mname else ''}\n".encode())

        s.x("""UPDATE runs SET finished=?, exit_code=?, outcome=?, total=?, passed=?, failed=?, errors=?, skipped=?
               WHERE id=?""",
            (finished, None if rc in (self.RC_WAIT, self.RC_LOST) else rc, outcome, counts.get("total"),
             counts.get("passed"), counts.get("failed"), counts.get("errors"), counts.get("skipped"), run_id))

        with s.lock:
            now_card = s.one("SELECT * FROM cards WHERE id=?", (cid,))
            if not now_card:
                return  # deleted while running
            if cancelled:
                s.x("UPDATE cards SET status='backlog', run_at=NULL, updated=? WHERE id=?", (finished, cid))
            elif requeue:  # keeps its place in the queue (queued_at), and its schedule
                s.x("UPDATE cards SET status='queued', updated=? WHERE id=?", (finished, cid))
            elif now_card["cron"]:
                try:
                    nxt = Cron(now_card["cron"]).next_after(finished)
                    s.x("UPDATE cards SET status='scheduled', run_at=?, updated=? WHERE id=?", (nxt, finished, cid))
                except ValueError:
                    s.x("UPDATE cards SET status=?, cron=NULL, run_at=NULL WHERE id=?",
                        ("passed" if outcome == "passed" else "failed", cid))
            else:
                s.x("UPDATE cards SET status=?, run_at=NULL, updated=? WHERE id=?",
                    ("passed" if outcome == "passed" else "failed", finished, cid))

    def _wait(self, card, p, deadline=None):
        """Wait for a process of a run until the run's deadline. Returns (exit code, timed_out)."""
        self.procs[card["id"]] = p
        if card["id"] in self.cancelled:  # Stop arrived while the process was starting
            self._terminate(card["id"], p)
        try:
            return p.wait(timeout=None if deadline is None else max(0.0, deadline - time.time())), False
        except subprocess.TimeoutExpired:
            self._terminate(card["id"], p)
            return p.wait(), True
        finally:
            self.procs.pop(card["id"], None)

    @staticmethod
    def _parse_junit(path):
        try:
            root = ET.parse(str(path)).getroot()
        except (OSError, ET.ParseError):
            return {}
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        c = {"total": 0, "failed": 0, "errors": 0, "skipped": 0}
        for su in suites:
            c["total"] += int(su.get("tests", 0))
            c["failed"] += int(su.get("failures", 0))
            c["errors"] += int(su.get("errors", 0))
            c["skipped"] += int(su.get("skipped", 0))
        c["passed"] = c["total"] - c["failed"] - c["errors"] - c["skipped"]
        return c

    @staticmethod
    def _kill(p):
        kill_tree(p)

    def _terminate(self, card_id, p):
        """Stop a run's process (group). For a run on a machine that's the ssh connection following it: the next
        connection then ends the run on the machine (see _follow_loop)."""
        if card_id in self.following:
            if card_id in self.killing:
                return  # this connection is the one ending the run there; let it finish
            self.kill_req.add(card_id)
        self._kill(p)

    def stop(self, card_id):
        with self.store.lock:
            if card_id not in self.active:
                return False
            self.cancelled.add(card_id)  # also seen by a run that hasn't started its process yet
            p = self.procs.get(card_id)
            ev = self.nudge.get(card_id)
        if ev:
            ev.set()  # a run waiting to reconnect tries right away (and ends the run if it gets through)
        if p:
            threading.Thread(target=self._terminate, args=(card_id, p), daemon=True).start()
        return True
