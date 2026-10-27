"""Interactive terminals on a machine (Open shell, Attach): ssh in a pty, or pipes on Windows."""
import base64
import os
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time

from .model import ApiError
from .runner import Runner
from .system import CHILD_KW, IS_WIN, kill_tree

# Becomes the session leader's controlling terminal, then execs ssh. Done in a fresh process instead of a
# preexec_fn, which isn't safe in this multi-threaded server.
# Signals ignored by whoever started the board (nohup, `&` in a script) are reset, so Ctrl+C works.
_CTTY_EXEC = ("import fcntl, os, signal, sys, termios\n"
              "for n in ('SIGINT', 'SIGQUIT', 'SIGTSTP', 'SIGTTIN', 'SIGTTOU', 'SIGPIPE', 'SIGHUP'):\n"
              "    signal.signal(getattr(signal, n), signal.SIG_DFL)\n"
              "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
              "os.execvp(sys.argv[1], sys.argv[1:])")


# Windows has no pseudo-terminal in the standard library. There the shell talks to ssh through plain pipes:
# ssh -tt still gives the remote side a real terminal (line editing, Ctrl+C, vim), and the window size is set
# there with stty; resizing the window afterwards isn't passed on, and login must be key-based.
SHELL_PTY = hasattr(os, "openpty") and not IS_WIN and not os.environ.get("PYTEST_BOARD_SHELL_PIPES")


class Shell:
    def __init__(self, sid, machine, proc, fd, wfd=None):
        self.id, self.machine, self.proc, self.fd = sid, machine, proc, fd
        self.wfd = fd if wfd is None else wfd  # where keystrokes go (the pty, or ssh's stdin pipe)
        self.pty = wfd is None
        self.buf = bytearray()
        self.base = 0  # byte offset of buf[0] in the whole output (old output is dropped past MAX_BUF)
        self.closed = False
        self.exit_code = None
        self.seen = time.time()
        self.cond = threading.Condition()


class Shells:
    """ssh sessions on a pseudo-terminal, driven from the browser (output by long-poll, input by POST)."""
    MAX = 8            # open shells at once
    IDLE = 90          # seconds without a poll before a shell is closed (browser tab gone)
    MAX_BUF = 1 << 21  # output kept for a reconnecting poll

    def __init__(self, runner):
        self.r = runner
        self.items = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, name="shell-reaper", daemon=True).start()

    @staticmethod
    def _winsize(fd, cols, rows):
        import fcntl
        import struct
        import termios
        cols, rows = max(20, min(int(cols or 80), 500)), max(5, min(int(rows or 24), 200))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def start(self, m, cols, rows, script=None):
        """A terminal on a machine: a login shell in its project folder, or `script` (e.g. screen -x …)."""
        with self.lock:
            if sum(1 for x in self.items.values() if not x.closed) >= self.MAX:
                raise ApiError(429, f"{self.MAX} shells are open already; close one first")
        if script is None:
            wd = m["workdir"] or "~"
            shown = "~/" + shlex.quote(wd[2:]) if wd.startswith("~/") and shlex.quote(wd[2:]) != wd[2:] else (
                wd if wd.startswith("~") else shlex.quote(wd))
            hint = (f"[pytest-board] {wd} does not exist on {m['name']} yet, so you are in your home folder. "
                    f"Create it with:  mkdir -p {shown}")
            script = (f"cd {Runner._rpath(wd)} 2>/dev/null || echo {shlex.quote(hint)}; "
                      'exec "${SHELL:-/bin/sh}" -l')
        if not SHELL_PTY:
            return self._start_pipes(m, cols, rows, script)
        cmd = self.r.ssh_base(m, interactive=True) + [self.r.dest(m), "sh -c " + shlex.quote(script)]
        master, slave = os.openpty()
        try:
            self._winsize(master, cols, rows)
            env = dict(os.environ, TERM="xterm-256color")
            env.pop("PYTEST_BOARD_TOKEN", None)
            proc = subprocess.Popen([sys.executable, "-c", _CTTY_EXEC] + cmd, stdin=slave, stdout=slave,
                                    stderr=slave, start_new_session=True, env=env, cwd=str(self.r.root))
        except OSError as e:
            os.close(master)
            raise ApiError(500, f"cannot start {self.r.ssh_cmd[0]}: {e}")
        finally:
            os.close(slave)
        sh = Shell("s" + secrets.token_hex(8), m["name"], proc, master)  # never all digits (the router ints those)
        with self.lock:
            self.items[sh.id] = sh
        threading.Thread(target=self._pump, args=(sh,), name=f"shell-{sh.id}", daemon=True).start()
        print(f"shell opened on {m['name']} ({self.r.dest(m)})", file=sys.stderr)
        return sh

    def _start_pipes(self, m, cols, rows, script):
        cols, rows = max(20, min(int(cols or 80), 500)), max(5, min(int(rows or 24), 200))
        script = f"stty cols {cols} rows {rows} 2>/dev/null; " + script
        # BatchMode: without a terminal here ssh can't ask for a password; fail fast instead of hanging
        cmd = (self.r.ssh_base(m, interactive=True) + ["-o", "BatchMode=yes"]
               + [self.r.dest(m), "sh -c " + shlex.quote(script)])
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("PYTEST_BOARD_TOKEN", None)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=env, cwd=str(self.r.root), bufsize=0, **CHILD_KW)
        except OSError as e:
            raise ApiError(500, f"cannot start {self.r.ssh_cmd[0]}: {e}")
        sh = Shell("s" + secrets.token_hex(8), m["name"], proc, proc.stdout.fileno(), proc.stdin.fileno())
        with self.lock:
            self.items[sh.id] = sh
        threading.Thread(target=self._pump, args=(sh,), name=f"shell-{sh.id}", daemon=True).start()
        print(f"shell opened on {m['name']} ({self.r.dest(m)}, pipe mode)", file=sys.stderr)
        return sh

    def _pump(self, sh):
        while True:
            try:
                data = os.read(sh.fd, 65536)
            except OSError:  # EIO: ssh exited and the terminal closed
                data = b""
            if not data:
                break
            with sh.cond:
                sh.buf += data
                if len(sh.buf) > self.MAX_BUF:
                    drop = len(sh.buf) - self.MAX_BUF
                    del sh.buf[:drop]
                    sh.base += drop
                sh.cond.notify_all()
        rc = sh.proc.wait()
        if sh.pty:
            try:
                os.close(sh.fd)
            except OSError:
                pass
        else:
            for f in (sh.proc.stdin, sh.proc.stdout):
                try:
                    f.close()
                except OSError:
                    pass
        with sh.cond:
            sh.closed, sh.exit_code = True, rc
            sh.cond.notify_all()
        print(f"shell on {sh.machine} closed (exit {rc})", file=sys.stderr)

    def get(self, sid):
        sh = self.items.get(str(sid))
        if not sh:
            raise ApiError(404, "no such shell (it was closed)")
        return sh

    def read(self, sid, offset, wait=20.0):
        sh = self.get(sid)
        sh.seen = time.time()
        with sh.cond:
            end = sh.base + len(sh.buf)
            if offset >= end and not sh.closed:
                sh.cond.wait(timeout=wait)
                end = sh.base + len(sh.buf)
            start = max(offset, sh.base)
            data = bytes(sh.buf[start - sh.base:]) if start < end else b""
            sh.seen = time.time()
            return {"data": base64.b64encode(data).decode(), "offset": start + len(data),
                    "closed": sh.closed and start + len(data) >= end, "exit_code": sh.exit_code}

    def write(self, sid, text):
        sh = self.get(sid)
        if sh.closed:
            raise ApiError(409, "the shell has ended")
        data = str(text).encode()
        try:
            while data:
                n = os.write(sh.wfd, data)
                data = data[n:]
        except OSError as e:
            raise ApiError(409, f"the shell has ended ({e})")

    def resize(self, sid, cols, rows):
        sh = self.get(sid)
        if not sh.closed and sh.pty:  # pipe mode (Windows): the size was set once, at the start
            try:
                self._winsize(sh.fd, cols, rows)  # the kernel tells ssh (SIGWINCH), ssh tells the remote side
            except OSError:
                pass

    def close(self, sid):
        sh = self.items.pop(str(sid), None)
        if sh and not sh.closed:
            threading.Thread(target=self._kill, args=(sh,), daemon=True).start()

    @staticmethod
    def _kill(sh):
        if IS_WIN:
            kill_tree(sh.proc, grace=3)
            return
        for sig, wait in ((signal.SIGHUP, 3), (signal.SIGKILL, 0)):  # SIGHUP: like closing a terminal window
            try:
                os.killpg(sh.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                sh.proc.wait(timeout=wait or None)
                return
            except subprocess.TimeoutExpired:
                continue

    def close_all(self):
        for sid in list(self.items):
            sh = self.items.pop(sid, None)
            if sh and not sh.closed:
                try:
                    if IS_WIN:
                        kill_tree(sh.proc, grace=1)
                    else:
                        os.killpg(sh.proc.pid, signal.SIGHUP)
                except (ProcessLookupError, PermissionError):
                    pass

    def _reaper(self):
        while True:
            time.sleep(15)
            now = time.time()
            for sid, sh in list(self.items.items()):
                if now - sh.seen > (self.IDLE if not sh.closed else 60):
                    print(f"closing idle shell on {sh.machine}", file=sys.stderr)
                    self.close(sid)
