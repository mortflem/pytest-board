"""RemoteMixin: runs that go on on their machine by themselves (screen session or background process), followed over
ssh, reconnecting when the connection drops; plus Discover and the command preview.
"""
import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time

from . import optscan
from .system import CHILD_KW, QUIET_KW


class _Detached(Exception):
    """The board is shutting down while a run goes on on its machine: leave it 'running' to follow it again."""


class RemoteMixin:
    """Part of Runner (see runner.py); uses its attributes."""

    # --- runs on machines
    # A run goes on on its machine by itself, so a dropped connection, a long network outage or a restart of
    # the board doesn't end it. It lives in ~/.pytest-board/runs/<board id>-<run id>/ there: run.sh (the
    # command), pid, log (pytest's output), junit.xml and rc (the exit code, written last). With screen
    # installed on the machine it runs in a screen session (Attach in the card dialog opens it), otherwise as a
    # background process. The board follows the log over ssh from the byte it has got to, reconnects whenever
    # the connection drops, and collects rc and junit.xml at the end.
    RUNS_DIR = ".pytest-board/runs"   # under the ssh user's home on the machine
    HELLO = b"\x02PB_HELLO\x03\n"     # first bytes of every follow connection: the script there is running
    RC_START = 98                     # the run could not be started on the machine

    # sh functions used on the machine. alive: the run's shell still runs (pid not reused by something else).
    _SH_ALIVE = r'''alive() {
  P=$(cat "$D/pid" 2>/dev/null) && [ -n "$P" ] && kill -0 "$P" 2>/dev/null || return 1
  [ ! -r "/proc/$P/cmdline" ] || grep -q run.sh "/proc/$P/cmdline" 2>/dev/null
}
'''
    # stop_run: TERM to the run's process group (pytest, xdist workers, tee), KILL after 10 s
    _SH_STOP = r'''stop_run() {
  alive || return 0
  kill -s TERM -- "-$P" 2>/dev/null || kill -s TERM "$P" 2>/dev/null
  i=0; while [ $i -lt 10 ] && alive; do sleep 1; i=$((i+1)); done
  alive && { kill -s KILL -- "-$P" 2>/dev/null || kill -s KILL "$P" 2>/dev/null; }
  return 0
}
'''
    # print the log from byte O on as it grows. exit 0: finished (rc exists, so the log is complete),
    # exit 4: the run's shell is gone without an exit code. A heartbeat on stderr every 10 s makes this loop
    # end soon after its connection is gone.
    _SH_FOLLOW = r'''out() {
  S=$(wc -c < "$D/log" 2>/dev/null) || S=0; S=$((S+0))
  if [ "$S" -gt "$O" ]; then tail -c +$((O+1)) "$D/log" 2>/dev/null | head -c $((S-O)); O=$S; fi
}
while :; do
  F=0; [ -f "$D/rc" ] && F=1
  out
  [ $F = 1 ] && exit 0
  if ! alive && { [ -f "$D/pid" ] || [ $n -ge 30 ]; }; then
    sleep 1
    [ -f "$D/rc" ] && continue
    out; exit 4
  fi
  n=$((n+1)); [ $((n % 10)) = 0 ] && printf '\006' >&2
  sleep 1
done
'''
    # kill_dir TAG: end the run in that folder if it still goes on, then remove the folder
    _SH_KILL_DIR = r'''kill_dir() {
  D="$R/$1"
  if [ -d "$D" ]; then
    P=$(cat "$D/pid" 2>/dev/null)
    if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && { [ ! -r "/proc/$P/cmdline" ] || grep -q run.sh "/proc/$P/cmdline" 2>/dev/null; }; then
      kill -s TERM -- "-$P" 2>/dev/null || kill -s TERM "$P" 2>/dev/null
      i=0; while [ $i -lt 5 ] && kill -0 "$P" 2>/dev/null; do sleep 1; i=$((i+1)); done
      kill -s KILL -- "-$P" 2>/dev/null || kill -s KILL "$P" 2>/dev/null
    fi
    rm -rf "$D"
  fi
  [ -d "$D" ] || echo "PB_GONE $1"
}
'''

    def _tag(self, run_id):
        return f"{self.store.board_id}-{run_id}"

    def _session(self, run_id):
        return f"pb-run-{run_id}-{self.store.board_id}"

    @staticmethod
    def _remote_info(run):
        try:
            return json.loads(run.get("remote") or "null")
        except ValueError:
            return None

    def _save_info(self, run_id, info):
        self.store.x("UPDATE runs SET remote=? WHERE id=?", (json.dumps(info) if info else None, run_id))

    def _offline(self, m, detail):
        """A run couldn't reach its machine: hold the machine's queued cards until a health check gets through."""
        if (self.machines.status.get(m["name"]) or {}).get("state") == "offline":
            return  # keep the health checks' rhythm (they retry every RECHECK seconds)
        self.machines.set_status(m["name"], state="offline", detail=str(detail)[:300], checked=time.time())

    def _set_link(self, cid, state, **kw):
        """Connection state of a run that is followed on its machine, for the board ('reconnecting')."""
        with self.store.lock:
            if state:
                self.links[cid] = dict(kw, state=state)
            elif self.links.pop(cid, None) is None:
                return
            self.store.version += 1

    def _note(self, run_id, info, log, st, text):
        """A line from the board in the middle of a run's log. Counted, so that after a restart the board knows
        how much of its log came from the machine, and asks for the rest from the right byte."""
        with st["lock"]:
            b = (b"" if st["nl"] else b"\n") + f"[pytest-board] {text}\n".encode()
            info["notes"] = info.get("notes", 0) + len(b)
            self._save_info(run_id, info)
            log.write(b)
            log.flush()
            st["nl"] = True

    @staticmethod
    def _span(sec):
        sec = int(sec)
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s" \
            if sec >= 60 else f"{sec}s"

    def _run_remote(self, card, run_id, m, log, junit_path, deadline=None):
        g = self.groups.get(card.get("grp"))
        dest = self.dest(m)
        wd = self.remote_dir(m, g)
        argv = self.remote_argv(card, m, g)
        env = self.remote_env_layers(card, m, g, run_id)
        shown = " ".join(shlex.quote(a) for a in argv)
        self.store.x("UPDATE runs SET command=? WHERE id=?", (f"ssh {dest} · cd {wd} && {shown}", run_id))
        log.write(f"[pytest-board] machine {m['name']} ({dest}{':' + str(m['port']) if m['port'] else ''})\n"
                  f"{self.env_file_notes(g, m)}"
                  f"[pytest-board] env: {', '.join(sorted(set(env[0]) | set(env[1])))}\n"
                  f"$ ssh {dest}\n$ cd {wd}\n$ {shown}\n\n".encode())
        log.flush()
        if m["sync"]:
            rc, timed_out = self._sync(m, log, card, deadline)
            if timed_out:
                log.write(b"[pytest-board] timed out during the project sync; tests were not started\n")
                return rc, True
            if card["id"] in self.cancelled:
                return rc, False
            if rc == 255:  # ssh couldn't get through: wait for the machine instead of failing
                self._offline(m, "not reachable for the project sync")
                return self.RC_WAIT, False
            if rc != 0:
                log.write(f"[pytest-board] project sync failed (exit {rc}); tests were not started\n".encode())
                return self.RC_SYNC_FAILED, False
        if card["id"] in self.cancelled:
            return -1, False
        branch = self.resolve_branch(card)[0]
        if branch and m["sync"]:
            log.write(f"[pytest-board] can't check out branch {branch} on {m['name']}: it gets a copy of the project "
                      f"(sync = yes), not a git checkout. Turn sync off and keep a git checkout in its project "
                      f"folder, or remove the branch from this card.\n".encode())
            return self.RC_CHECKOUT, False
        info, rc = self._launch(run_id, m, wd, argv, env, log, branch)
        if info is None:
            return rc, False
        return self._follow(card, run_id, m, info, log, junit_path, deadline)

    # co(): switch the machine's project folder to the latest @B@ from its git remote: refuses to touch uncommitted
    # changes or local commits that aren't on the remote
    _SH_CHECKOUT = r'''co() {
  W=@W@; WS=@WS@; B=@B@
  echo "[pytest-board] git: switching $WS to branch $B"
  cd "$W" 2>/dev/null || { printf '[pytest-board] project folder not found: %s\n' "$WS"; return 97; }
  git rev-parse --git-dir >/dev/null 2>&1 || { echo "[pytest-board] $WS is not a git checkout, so there is no branch to switch to"; return 95; }
  if [ -n "$(git status --porcelain --untracked-files=no 2>&1)" ]; then
    echo "[pytest-board] $WS has uncommitted changes, so the board won't switch branches there:"
    git status --porcelain --untracked-files=no 2>&1 | head -n 20
    echo "[pytest-board] commit or stash them, or discard them with: git -C $WS checkout -- ."
    return 95
  fi
  GR=$(git remote | grep -x origin || git remote | head -n 1)
  [ -n "$GR" ] || { echo "[pytest-board] $WS has no git remote to fetch from"; return 95; }
  echo "\$ git fetch --prune $GR"
  GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}" git fetch --prune "$GR" || { echo "[pytest-board] git fetch $GR failed (see above)"; return 95; }
  if ! git rev-parse --verify --quiet "refs/remotes/$GR/$B" >/dev/null; then
    case "$B" in "$GR"/*) H=" (use the name without $GR/)";; *) H=;; esac
    echo "[pytest-board] branch $B isn't on $GR$H"; return 95
  fi
  if git rev-parse --verify --quiet "refs/heads/$B" >/dev/null; then
    git checkout --quiet "$B" || { echo "[pytest-board] git checkout $B failed (see above)"; return 95; }
  else
    git checkout --quiet -b "$B" --track "$GR/$B" || { echo "[pytest-board] git checkout $B failed (see above)"; return 95; }
  fi
  git merge --ff-only --quiet "$GR/$B" || { echo "[pytest-board] the local branch $B has commits that aren't on $GR, so it can't be brought up to date. Push or remove them (git -C $WS log $GR/$B..$B)"; return 95; }
  echo "[pytest-board] at $(git log -1 --format='%h %s (%an, %cr)')"
}'''

    def _launch(self, run_id, m, wd, argv, env, log, branch=None):
        """Start the run on the machine, detached from this connection. env: (low, high), see remote_env_layers.
        branch: switch the project folder to it first. Returns (remote info, None), or (None, exit code) when it
        didn't start."""
        tag, session = self._tag(run_id), self._session(run_id)
        cmd = " ".join(self._rword(a) for a in argv) + ' "--junitxml=$D/junit.xml"'
        co = (self._SH_CHECKOUT.replace("@W@", self._rpath(m["workdir"] or "~"))
              .replace("@WS@", shlex.quote(m["workdir"] or "~")).replace("@B@", shlex.quote(branch))) if branch else ""
        run_sh = "\n".join([
            f"# pytest-board run {run_id} on {m['name']}. Written by the board; removed when the run is collected.",
            'D=$(cd "$(dirname "$0")" && pwd) || exit 1',
            'echo $$ > "$D/pid"',
            "trap : INT  # Ctrl+C (in the screen session) interrupts pytest, which still reports and exits",
            # pytest gets the default Ctrl+C handling even if it was ignored on the way here (background start)
            'SIGDFL=; env --default-signal=INT true 2>/dev/null && SIGDFL="env --default-signal=INT"',
            co,
            "run() {",
            "  co || return $?" if branch else "  :",
            f"  cd {self._rpath(wd)} 2>/dev/null || {{ printf '[pytest-board] remote folder not found: %s\\n' "
            f"{shlex.quote(wd)}; return {self.RC_NO_WORKDIR}; }}",
            f"  $SIGDFL {cmd}",
            "}",
            '{ trap : INT; run; echo $? > "$D/rc.tmp"; } 2>&1 | (trap \'\' INT; exec tee -a "$D/log")',
            '[ -f "$D/rc.tmp" ] && mv -f "$D/rc.tmp" "$D/rc"',
            "",
        ])
        eof = "PB_EOF_" + secrets.token_hex(6)
        # The script travels over ssh's stdin; env values only exist in the environment of the run's processes
        # (not on a command line, not in a file).
        script = "".join([
            "umask 077\n",
            f'R="$HOME/{self.RUNS_DIR}"; D="$R/{tag}"\n',
            'mkdir -p "$R" && rm -rf "$D" && mkdir "$D" || { echo "PB_ERR cannot create $D"; exit 3; }\n',
            f"cat > \"$D/run.sh\" <<'{eof}'\n{run_sh}{eof}\n",
            ': > "$D/log"\n',
            *(f"export {k}={shlex.quote(v)}\n" for k, v in env[0].items()),
            self._source_machine_env(m),
            *(f"export {k}={shlex.quote(v)}\n" for k, v in env[1].items()),
            # in the run folder (a .screenrc with "deflog on" writes its screenlog there), with a TERM even when
            # ssh gave none (screen then names the session's terminal "screen" anyway)
            f'if command -v screen >/dev/null 2>&1 && (cd "$D" && TERM="${{TERM:-xterm}}" '
            f'screen -dmS {session} sh "$D/run.sh" </dev/null >/dev/null) 2>"$D/screen.err"; then\n',
            '  echo "PB_STARTED screen"\n',
            "else\n",
            '  [ -s "$D/screen.err" ] && echo "PB_NOTE screen did not start: $(head -c 300 "$D/screen.err")"\n',
            '  if command -v setsid >/dev/null 2>&1; then setsid sh "$D/run.sh" </dev/null >/dev/null 2>&1 &\n',
            '  else nohup sh "$D/run.sh" </dev/null >/dev/null 2>&1 &\n',
            "  fi\n",
            '  echo "PB_STARTED background"\n',
            "fi\n",
        ])
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                               capture_output=True, timeout=90, cwd=str(self.root), **QUIET_KW)
            out, err, rc = r.stdout.decode("utf-8", "replace"), self._ssh_error(r.stderr), r.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "no answer within 90 s", 255
        except OSError as e:
            log.write(f"failed to start ssh ({self.ssh_cmd[0]}): {e}\n".encode())
            return None, self.RC_SSH
        lines = out.splitlines()
        mode = next((ln.split()[1] for ln in lines if ln.startswith("PB_STARTED ")), None)
        for ln in lines:
            if ln.startswith("PB_NOTE "):
                log.write(f"[pytest-board] {ln[8:]}\n".encode())
        if not mode:
            # it may have got as far as creating the folder: tidy that away once the machine answers
            self._save_info(run_id, {"tag": tag, "session": session})
            if rc == 255:
                log.write(f"[pytest-board] can't reach {m['name']}: {err or 'ssh exit code 255'}\n".encode())
                self._offline(m, err or "unreachable")
                return None, self.RC_WAIT
            msg = next((ln[7:] for ln in lines if ln.startswith("PB_ERR ")), None) or err or f"exit code {rc}"
            log.write(f"[pytest-board] could not start the run on {m['name']}: {msg}\n".encode())
            return None, self.RC_START
        info = {"tag": tag, "session": session if mode == "screen" else None, "base": None, "notes": 0}
        if mode == "screen":
            log.write(f"[pytest-board] started on {m['name']} in screen session {session}. It goes on by itself if "
                      f"the connection drops; Attach (in the card) opens the session.\n\n".encode())
        else:
            log.write(f"[pytest-board] started on {m['name']} as a background process. It goes on by itself if the "
                      f"connection drops. (Install screen there to be able to attach to runs.)\n\n".encode())
        log.flush()
        info["base"] = os.fstat(log.fileno()).st_size  # the machine's output starts here in the local log
        self._save_info(run_id, info)
        return info, None

    def _follow(self, card, run_id, m, info, log, junit_path, deadline, resumed=False):
        """Copy a run's log from its machine until the run ends, reconnecting whenever the connection drops,
        then collect its exit code and junit.xml. Returns (exit code, timed_out)."""
        cid = card["id"]
        self.following[cid] = m["name"]
        ev = self.nudge[cid] = threading.Event()
        try:
            return self._follow_loop(card, run_id, m, info, log, junit_path, deadline, ev, resumed)
        finally:
            self.following.pop(cid, None)
            self.nudge.pop(cid, None)
            self.killing.discard(cid)
            self.kill_req.discard(cid)
            self._set_link(cid, None)

    def _follow_loop(self, card, run_id, m, info, log, junit_path, deadline, ev, resumed):
        cid, name = card["id"], m["name"]
        log.flush()
        st = {"nl": True, "down": None, "lock": threading.Lock()}
        size = os.fstat(log.fileno()).st_size
        if size:
            with open(log.name, "rb") as fh:
                fh.seek(size - 1)
                st["nl"] = fh.read(1) == b"\n"
        offset = max(0, size - info["base"] - info.get("notes", 0))  # bytes of the machine's log we have
        if resumed:
            self._note(run_id, info, log, st, f"the board restarted; following the run on {name} again")
        why, phase, delay = None, "follow", 5
        while True:
            if self.stopping:
                raise _Detached()
            if why is None:
                if cid in self.cancelled:
                    why = "stop"
                elif deadline is not None and time.time() >= deadline:
                    why = "timeout"
                if why:
                    phase = "follow"
                    self._note(run_id, info, log, st, f"{'Stop' if why == 'stop' else 'Timeout'}: "
                                                      f"ending the run on {name}…")
            m = self.machines.get(name) or m  # pick up edits (host, port, key) between connections
            if phase == "follow":
                res = self._follow_once(card, run_id, m, info, log, st, offset, why is not None,
                                        None if why else deadline)
                offset += res["got"]
            else:
                res = self._collect_once(m, info, junit_path)
                if res["hello"]:
                    self._back(cid, run_id, m, info, log, st)
            if res["hello"]:
                delay = 5
            kind = res["kind"]
            if kind == "finished":  # the exit code is there, so the log is complete
                phase = "collect"
                continue
            if kind == "collected":
                self._tidy_soon(m, [run_id])
                return res["rc"], why == "timeout"
            if kind in ("gone", "nofolder"):
                self._tidy_soon(m, [run_id])
                if why:
                    return -1, why == "timeout"
                self._note(run_id, info, log, st,
                           f"the run ended on {name} without an exit code: the machine may have restarted, or its "
                           f"processes were killed there" if kind == "gone" else
                           f"the run's folder on {name} is gone (~/{self.RUNS_DIR}/{info['tag']})")
                return self.RC_LOST, False
            if kind == "killed":
                continue  # Stop or the timeout ended this connection; the next one ends the run there
            # the connection failed or dropped
            time.sleep(0.3)
            if self.stopping:
                raise _Detached()
            err = res["err"] or "connection closed"
            if why:  # can't get through to end the run: it's ended there once the machine answers again
                self._note(run_id, info, log, st, f"can't reach {name} ({err}). The run there is ended as soon "
                                                  f"as {name} answers again.")
                return -1, why == "timeout"
            if st["down"] is None:
                st["down"] = time.time()
                self._note(run_id, info, log, st, f"no connection to {name} ({err}) since "
                                                  f"{time.strftime('%H:%M:%S')}. The run goes on there by itself; "
                                                  f"reconnecting…")
                self._set_link(cid, "reconnecting", since=st["down"], detail=err)
            self._offline(m, err)
            wait = delay if deadline is None else max(0.0, min(delay, deadline - time.time()))
            ev.wait(wait)
            ev.clear()
            delay = min(60, delay * 2)

    def _back(self, cid, run_id, m, info, log, st):
        """A connection got through after an outage."""
        if st["down"] is None:
            return
        gone = time.time() - st["down"]
        st["down"] = None
        self._note(run_id, info, log, st, f"reconnected to {m['name']} after {self._span(gone)}; "
                                          f"catching up on the output")
        self._set_link(cid, None)
        threading.Thread(target=self._check_safe, args=(m,), daemon=True).start()  # machine shows green again

    def _follow_once(self, card, run_id, m, info, log, st, offset, kill, deadline):
        """One ssh connection that streams the log from `offset` (with kill: ends the run first)."""
        cid = card["id"]
        res = {"kind": "conn", "got": 0, "hello": False, "err": "", "closed": False}
        ses = info.get("session")
        script = ("{\n"
                  "printf '\\002PB_HELLO\\003\\n'\n"
                  f'D="$HOME/{self.RUNS_DIR}/{info["tag"]}"; O={int(offset)}; n=0\n'
                  'cd "$D" 2>/dev/null || exit 3\n'
                  + self._SH_ALIVE
                  + (self._SH_STOP + "stop_run\n"
                     + (f"screen -S {shlex.quote(ses)} -X quit >/dev/null 2>&1\n" if ses else "") + "n=30\n"
                     if kill else "")
                  + self._SH_FOLLOW + "}\n")
        try:
            # quicker keepalive than other connections: a dead link is noticed in about 30 s
            p = subprocess.Popen(self.ssh_base(m, keepalive=(10, 3)) + [self.dest(m), "sh -s"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=str(self.root), **CHILD_KW)
        except OSError as e:
            res["err"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return res
        errbuf = bytearray()

        def pump_err():
            for chunk in iter(lambda: p.stderr.read1(4096), b""):
                if len(errbuf) < 8192:
                    errbuf.extend(chunk.replace(b"\x06", b""))  # minus the heartbeats

        def pump_out():
            pending = b""
            for chunk in iter(lambda: p.stdout.read1(65536), b""):
                if res["closed"]:
                    return
                if not res["hello"]:  # skip anything a login script printed before the marker
                    pending += chunk
                    i = pending.find(self.HELLO)
                    if i < 0:
                        pending = pending[-(len(self.HELLO) - 1):]
                        continue
                    chunk = pending[i + len(self.HELLO):]
                    res["hello"] = True
                    self._back(cid, run_id, m, info, log, st)
                    if not chunk:
                        continue
                with st["lock"]:
                    log.write(chunk)
                    log.flush()
                    st["nl"] = chunk.endswith(b"\n")
                res["got"] += len(chunk)

        threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err)]
        for t in threads:
            t.start()
        if kill:
            self.killing.add(cid)
        try:
            try:
                p.stdin.write(script.encode())
                p.stdin.close()
            except OSError:
                pass
            rc, timed_out = self._wait(card, p, deadline)
        finally:
            self.killing.discard(cid)
        threads[0].join(15)
        threads[1].join(2)
        res["closed"] = True
        on_purpose = timed_out or cid in self.kill_req
        self.kill_req.discard(cid)
        res["err"] = self._ssh_error(bytes(errbuf))
        if res["hello"] and rc in (0, 3, 4):
            res["kind"] = {0: "finished", 3: "nofolder", 4: "gone"}[rc]
        elif on_purpose:
            res["kind"] = "killed"
        return res

    def _collect_once(self, m, info, junit_path):
        """Fetch a finished run's exit code and junit.xml."""
        res = {"kind": "conn", "hello": False, "err": "", "rc": None}
        script = ("{\n"
                  "printf '\\002PB_HELLO\\003\\n'\n"
                  f'D="$HOME/{self.RUNS_DIR}/{info["tag"]}"\n'
                  'cd "$D" 2>/dev/null || exit 3\n'
                  'printf "PB_RC %s\\n" "$(cat rc 2>/dev/null)"\n'
                  '[ -f junit.xml ] && { echo PB_JUNIT; cat junit.xml; }\n'
                  "exit 0\n}\n")
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                               capture_output=True, timeout=120, cwd=str(self.root), **QUIET_KW)
        except subprocess.TimeoutExpired:
            res["err"] = "no answer within 120 s"
            return res
        except OSError as e:
            res["err"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return res
        out, i = r.stdout, r.stdout.find(self.HELLO)
        res["err"] = self._ssh_error(r.stderr)
        if i < 0:
            return res
        res["hello"] = True
        out = out[i + len(self.HELLO):]
        if r.returncode == 3:
            res["kind"] = "nofolder"
            return res
        if r.returncode != 0 or not out.startswith(b"PB_RC "):
            return res  # dropped half-way: try again
        line, _, rest = out.partition(b"\n")
        v = line[6:].strip()
        if rest.startswith(b"PB_JUNIT\n") and rest[9:].strip():
            junit_path.write_bytes(rest[9:])
        res["kind"], res["rc"] = "collected", int(v) if v.lstrip(b"-").isdigit() else self.RC_LOST
        return res

    def _tidy_soon(self, m, run_ids):
        threading.Thread(target=self.tidy, args=(m, run_ids), daemon=True).start()

    def tidy(self, m, run_ids=None, sweep=False):
        """
        Remove run folders on a machine: those of the given runs, or (None) of every finished run that still has
        one there, e.g. a run stopped while the machine was unreachable (it is ended first if it still goes on).
        sweep: also this board's folders that no running run owns (left behind by a crash), and the temp files
        of older pytest-board versions. Returns True when the machine answered.
        """
        s = self.store
        if run_ids is None:
            rows = s.q("SELECT id, remote FROM runs WHERE machine=? AND remote IS NOT NULL AND outcome<>'running'",
                       (m["name"],))
        else:
            rows = s.q(f"SELECT id, remote FROM runs WHERE remote IS NOT NULL AND id IN "
                       f"({','.join('?' * len(run_ids))})", list(run_ids)) if run_ids else []
        items = [(r["id"], self._remote_info(r)) for r in rows]
        items = [(rid, x) for rid, x in items if x and x.get("tag")]
        if not items and not sweep:
            return True
        script = "{\n" + f'R="$HOME/{self.RUNS_DIR}"\n' + self._SH_KILL_DIR
        for _, x in items:
            script += f"kill_dir {shlex.quote(x['tag'])}\n"
            if x.get("session"):  # a session kept open by the user's .screenrc (zombie)
                script += (f"command -v screen >/dev/null 2>&1 && "
                           f"screen -S {shlex.quote(x['session'])} -X quit >/dev/null 2>&1\n")
        if sweep:
            keep = " ".join(self._tag(r["id"]) for r in s.q("SELECT id FROM runs WHERE outcome='running'"))
            script += (f'for D in "$R"/{self.store.board_id}-*; do [ -d "$D" ] || continue; t=${{D##*/}}; '
                       f'case " {keep} " in *" $t "*) ;; *) kill_dir "$t" ;; esac; done\n')
            script += ("find /tmp -maxdepth 1 -name 'pytest-board-*' -user \"$(id -u)\" -mmin +1440 "
                       "-exec rm -f {} + 2>/dev/null\n")
        script += "echo PB_TIDY_OK\n}\n"
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                               capture_output=True, timeout=120, cwd=str(self.root), **QUIET_KW)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if b"PB_TIDY_OK" not in r.stdout:
            return False
        gone = set(re.findall(rb"PB_GONE (\S+)", r.stdout))
        for rid, x in items:
            if x["tag"].encode() in gone:
                s.x("UPDATE runs SET remote=NULL WHERE id=?", (rid,))
        if sweep:
            self.swept.add(m["name"])
        return True

    def preview(self, item):
        """Where and how a card/test would run (for the UI)."""
        pv = self._preview(item)
        pv["run_branch"], pv["run_branch_source"] = self.resolve_branch(item)  # the card's own stays in "branch"
        return pv

    def _preview(self, item):
        mname, source = self.resolve_machine(item)
        g = self.groups.get(item.get("grp"))
        if not mname:
            return {"run_machine": None, "run_machine_source": source, "machine_missing": True, "where": "?",
                    "command_preview": "(no machine: drop it into a machine's row, or set machine = for its headline)"}
        m = self.machines.get(mname)
        if not m:
            return {"run_machine": mname, "run_machine_source": source, "machine_missing": True, "where": "?",
                    "command_preview": f"(machine '{mname}' is not defined)"}
        return {"run_machine": mname, "run_machine_source": source, "ssh": self.dest(m),
                "where": f"{self.dest(m)}{':' + str(m['port']) if m['port'] else ''}:{self.remote_dir(m, g)}",
                "command_preview": " ".join(shlex.quote(a) for a in self.remote_argv(item, m, g)),
                "machine_env_keys": sorted(m["env"])}

    def discover(self, grp=None, machine=None):
        """pytest --collect-only for a headline on a machine, over ssh (in its project folder, with its pytest command
        and env; default: the headline's machine). The folder is used as it is: no branch is checked out."""
        if not machine or machine == "local":
            machine = self.resolve_machine({"grp": grp})[0]
        if not machine:
            return {"ids": [], "error": "pick the machine to discover tests on", "grp": grp, "machine": None}
        m = self.machines.get(machine)
        if not m:
            return {"ids": [], "error": f"machine '{machine}' is not defined", "grp": grp, "machine": machine}
        why = self.not_bound({"grp": grp}, machine)
        if why:
            return {"ids": [], "error": why, "grp": grp, "machine": machine}
        return self._discover_remote(m, grp)

    def scan_options(self, grp, machine=None):
        """The command line options the headline's conftest.py files declare (see optscan.py), read on the machine
        its tests run on (default: the headline's machine); for a machine with sync = yes, or without a machine,
        from the project on this server. -> {"options", "files", "where", "machine", "dir", "scanned", "error",
        "problems"}"""
        g = self.groups.get(grp)
        if not g:
            return None
        if not machine or machine == "local":
            machine = self.resolve_machine({"grp": grp})[0]
        m = self.machines.get(machine) if machine else None
        out = {"options": [], "files": [], "machine": m["name"] if m else None, "dir": g["dir"],
               "scanned": time.time(), "error": None, "problems": []}
        if not m or m["sync"]:  # the project here is what runs (synced to the machine before each run)
            here = self.root / g["dir"] if g["dir"] else self.root
            out["where"] = str(here)
            files = optscan.local_files(here, self.root)
            if files is None:
                out["error"] = f"the folder {here} isn't here" + ("" if m else " (and the headline has no machine)")
                return out
        else:
            wd = self.remote_dir(m, g)
            out["where"] = f"{self.dest(m)}:{wd}"
            marker = "PB_CONFTEST_" + secrets.token_hex(8)
            script = optscan.remote_script(self._rpath(wd), self._rpath(m["workdir"] or "~"), marker)
            try:
                r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(),
                                   capture_output=True, timeout=60, cwd=str(self.root), **QUIET_KW)
            except subprocess.TimeoutExpired:
                out["error"] = f"no answer from {m['name']} within a minute"
                return out
            except OSError as e:
                out["error"] = f"cannot run {self.ssh_cmd[0]}: {e}"
                return out
            text = r.stdout.decode("utf-8", "replace")
            if r.returncode == 255:
                out["error"] = f"can't reach {m['name']}: {self._ssh_error(r.stderr) or 'ssh exit code 255'}"
                return out
            if "PB_NO_DIR" in text.splitlines():
                out["error"] = f"the project folder {wd} doesn't exist on {m['name']}"
                return out
            files = optscan.split_files(text, marker)
        out["files"] = [p for p, _ in files]
        out["options"], out["problems"] = optscan.options_from_files(files)
        return out

    def branches(self, grp=None, machine=None):
        """The git branches a card of this headline can run on there: the ones the project folder on the machine
        knows from its remote (as of its last git fetch; a run fetches first) and its local ones.
        -> {"branches": [...], "current": checked-out branch, "git": False when the folder isn't a git checkout,
        "machine", "where", "error"}"""
        if not machine or machine == "local":
            machine = self.resolve_machine({"grp": grp})[0]
        m = self.machines.get(machine) if machine else None
        out = {"branches": [], "current": None, "git": None, "machine": m["name"] if m else None, "where": None,
               "error": None}
        if not m:
            out["error"] = "no machine" if not machine else f"machine '{machine}' is not defined"
            return out
        if m["sync"]:
            out.update(git=False, error=f"{m['name']} has sync = yes: its copy of the project has no git")
            return out
        wd = self.remote_dir(m, self.groups.get(grp))
        out["where"] = f"{self.dest(m)}:{wd}"
        script = (f"cd {self._rpath(wd)} 2>/dev/null || {{ echo PB_NO_DIR; exit 97; }}\n"
                  "git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo PB_NO_GIT; exit 98; }\n"
                  "printf 'PB_HEAD %s\\n' \"$(git rev-parse --abbrev-ref HEAD 2>/dev/null)\"\n"
                  "git for-each-ref --format='PB_REF %(refname)' refs/remotes refs/heads\n")
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(), capture_output=True,
                               timeout=30, cwd=str(self.root), **QUIET_KW)
        except subprocess.TimeoutExpired:
            out["error"] = f"no answer from {m['name']} within 30 seconds"
            return out
        except OSError as e:
            out["error"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return out
        lines = r.stdout.decode("utf-8", "replace").splitlines()
        if r.returncode == 255:
            out["error"] = f"can't reach {m['name']}: {self._ssh_error(r.stderr) or 'ssh exit code 255'}"
        elif "PB_NO_DIR" in lines:
            out["error"] = f"the project folder {wd} doesn't exist on {m['name']}"
        elif "PB_NO_GIT" in lines:
            out.update(git=False, error=f"{wd} on {m['name']} isn't a git checkout")
        else:
            names = set()
            for ln in lines:
                if ln.startswith("PB_REF refs/heads/"):
                    names.add(ln[len("PB_REF refs/heads/"):])
                elif ln.startswith("PB_REF refs/remotes/"):
                    name = ln[len("PB_REF refs/remotes/"):].split("/", 1)  # origin/feature/x -> feature/x
                    if len(name) == 2 and name[1] != "HEAD":
                        names.add(name[1])
            head = next((ln[8:].strip() for ln in lines if ln.startswith("PB_HEAD ")), "")
            out.update(git=True, branches=sorted(names), current=head if head and head != "HEAD" else None)
        return out

    def _discover_remote(self, m, grp):
        item = {"grp": grp}
        g = self.groups.get(grp)
        wd = self.remote_dir(m, g)
        argv = self.remote_argv(item, m, g) + ["--collect-only", "-q", "-p", "no:cacheprovider"]
        low, high = self.remote_env_layers(item, m, g)
        shown = " ".join(shlex.quote(a) for a in argv)
        out = {"ids": [], "error": None, "command": f"ssh {self.dest(m)} · cd {wd} && {shown}",
               "cwd": f"{self.dest(m)}:{wd}", "grp": grp, "machine": m["name"], "branch": ""}
        script = "".join([
            'E="${TMPDIR:-/tmp}/pytest-board-discover-$$.err"; trap \'rm -f "$E"\' EXIT\n',
            *(f"export {k}={shlex.quote(v)}\n" for k, v in low.items()),
            self._source_machine_env(m, errfile='"$E"'),
            *(f"export {k}={shlex.quote(v)}\n" for k, v in high.items()),
            f"cd {self._rpath(wd)} 2>/dev/null || {{ echo PB_NO_DIR; exit 97; }}\n",
            "printf 'PB_BRANCH %s\\n' \"$(git rev-parse --abbrev-ref HEAD 2>/dev/null)\"\n",
            " ".join(self._rword(a) for a in argv) + "\n",
        ])
        try:
            r = subprocess.run(self.ssh_base(m) + [self.dest(m), "sh -s"], input=script.encode(), capture_output=True,
                               timeout=600, cwd=str(self.root), **QUIET_KW)
        except subprocess.TimeoutExpired:
            out["error"] = f"no answer from {m['name']} within 10 minutes"
            return out
        except OSError as e:
            out["error"] = f"cannot run {self.ssh_cmd[0]}: {e}"
            return out
        text, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
        lines = text.splitlines()
        out["branch"] = next((ln[10:].strip() for ln in lines if ln.startswith("PB_BRANCH ")), "")
        bad = next((ln[7:] for ln in lines if ln.startswith("PB_ERR ")), None)
        if r.returncode == 255:
            out["error"] = f"can't reach {m['name']}: {self._ssh_error(r.stderr) or 'ssh exit code 255'}"
        elif "PB_NO_DIR" in lines:
            out["error"] = f"the project folder {wd} doesn't exist on {m['name']}"
        elif bad:
            out["error"] = bad
        else:
            out["ids"] = [ln.strip() for ln in lines if "::" in ln and not ln.startswith((" ", "=", "PB_"))]
            if r.returncode not in (0, 5):
                out["error"] = (text[-4000:] + "\n" + err[-4000:]).strip()
        return out
