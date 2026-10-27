"""HealthMixin: machine checks (can we connect, is the folder there, does pytest start)."""
import shlex
import subprocess
import sys
import threading
import time

from .system import QUIET_KW


class HealthMixin:
    """Part of Runner (see runner.py); uses its attributes."""

    def check_machine(self, m):
        """Connect, look for the workdir and pytest. Updates machines.status."""
        name = m["name"]
        with self.checking_lock:
            already = name in self.checking
            if not already:
                self.checking.add(name)
        if already:  # a check is already running: wait for its result
            for _ in range(90):
                time.sleep(0.5)
                if name not in self.checking:
                    break
            return self.machines.status.get(name)
        if (self.machines.status.get(name) or {}).get("state") != "offline":
            self.machines.set_status(name, state="checking")  # an offline machine stays offline until it answers
        try:
            wd = m["workdir"] or "~"
            py = " ".join(self._rword(a) for a in shlex.split(m["python"]))
            mf = self._machine_env_file(m)
            script = (f"echo PB_OK; uname -srm; if cd {self._rpath(wd)} 2>/dev/null; then echo PB_DIR_OK; "
                      + (f"[ -f {mf[0]} ] && echo PB_ENV_FILE; " if mf else "")
                      + f"else echo PB_DIR_MISSING; fi; "
                      f"{py} --version 2>&1 | tail -n 8\n")  # tail: wrappers like poetry may warn first
            try:
                # bytes, not text: text mode would turn \n into \r\n on Windows, which breaks the remote sh
                r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                                   capture_output=True, timeout=40, **QUIET_KW)
                out, err, rc = (r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"),
                                r.returncode)
            except subprocess.TimeoutExpired:
                out, err, rc = "", "timed out after 40s", -1
            except OSError as e:
                out, err, rc = "", f"cannot run {self.ssh_cmd[0]}: {e}", -1
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if "PB_OK" not in lines:
                errl = [ln for ln in err.splitlines() if ln.strip() and not ln.startswith("Warning: Permanently")]
                st = {"state": "offline", "detail": (errl[-1] if errl else f"ssh exit code {rc}")[:300], "system": ""}
            else:
                i = lines.index("PB_OK")
                system = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].startswith("PB_") else ""
                ver = next((ln for ln in lines if ln.lower().startswith("pytest ")), None)
                if "PB_DIR_OK" not in lines and not m["sync"]:
                    st = {"state": "warn", "detail": f"remote folder not found: {wd}", "system": system}
                elif not ver:
                    tail = [ln for ln in lines if not ln.startswith("PB_") and ln != system]
                    st = {"state": "warn", "detail": "pytest not found" + (f": {tail[-1]}" if tail else ""),
                          "system": system}
                else:
                    note = " · folder is created on first sync" if "PB_DIR_OK" not in lines else ""
                    if "PB_ENV_FILE" in lines:
                        note += f" · loads {mf[1]}"
                    st = {"state": "online", "detail": ver + note, "system": system}
            if st["state"] != "offline":
                # before new runs may start there: end and remove what a run stopped while the machine was
                # unreachable left behind, and once per board start sweep up orphaned run folders
                try:
                    self.tidy(m, sweep=name not in self.swept)
                except Exception as e:
                    print(f"tidying up on {name}: {e}", file=sys.stderr)
            self.machines.set_status(name, checked=time.time(), **st)
            if st["state"] != "offline":
                for cid, mname in list(self.following.items()):  # runs waiting to reconnect: try now
                    ev = self.nudge.get(cid)
                    if mname == name and ev:
                        ev.set()
                self.wake.set()  # queued cards waiting for this machine
            return self.machines.status[name]
        finally:
            with self.checking_lock:
                self.checking.discard(name)

    RECHECK = 30  # seconds between checks of an unreachable machine that has work waiting

    def _waiting_on(self):
        """Machines with something waiting for them: queued cards, runs to reconnect to, leftovers to tidy."""
        names = {self.resolve_machine(c)[0] for c in self.store.q("SELECT machine, grp FROM cards WHERE status='queued'")}
        names.update(self.following.values())
        names.update(r["machine"] for r in self.store.q(
            "SELECT DISTINCT machine FROM runs WHERE remote IS NOT NULL AND outcome<>'running'"))
        return names

    def _health(self):
        """Every --check-interval seconds check each machine; machines that are down and have work waiting are
        checked every RECHECK seconds (also with --check-interval 0), so waiting cards start soon after."""
        time.sleep(1)
        while True:
            try:
                now, waiting = time.time(), None
                for m in self.machines.all():
                    st = self.machines.status.get(m["name"]) or {}
                    age = now - (st.get("checked") or 0)
                    due = self.check_interval > 0 and age >= self.check_interval
                    if not due and st.get("state") == "offline" and age >= self.RECHECK:
                        waiting = self._waiting_on() if waiting is None else waiting
                        due = m["name"] in waiting
                    if due and m["name"] not in self.checking:
                        threading.Thread(target=self._check_safe, args=(m,), daemon=True).start()
            except Exception as e:
                print("health check error:", e, file=sys.stderr)
            time.sleep(5)

    def _check_safe(self, m):
        try:
            self.check_machine(m)
        except Exception as e:
            print(f"health check {m['name']}: {e}", file=sys.stderr)
