"""SyncMixin: copying the project to a machine before a run (sync = yes): rsync, or tar over ssh."""
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
import time

from .system import CHILD_KW, IS_WIN


class SyncMixin:
    """Part of Runner (see runner.py); uses its attributes."""

    # never copied to a machine (.env: the machine keeps its own; this server's values are sent with each run)
    SYNC_SKIP = {".pytest-board", ".git", "__pycache__", ".pytest_cache", ".env"}

    def sync_method(self):
        """rsync when it's installed (fast: only changed files are sent); otherwise a tar stream over ssh (stdlib
        only, copies everything each time). Windows always uses tar: rsync builds there can't read C:\\ paths."""
        if IS_WIN or not shutil.which(self.rsync_cmd[0]):
            return "tar"
        return "rsync"

    def _sync(self, m, log, card, deadline=None):
        """
        Copy the project (--root) to the machine's workdir before the run.
        Returns (exit code, timed_out). Stop and the card's timeout interrupt it.
        """
        base, dest = self.ssh_base(m), self.dest(m)
        try:
            mk = subprocess.Popen(base + [dest, f"mkdir -p {self._rpath(m['workdir'])}"], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, **CHILD_KW)
            try:
                rc, timed_out = self._wait(card, mk, deadline if deadline is not None else time.time() + 60)
            finally:
                self.procs.pop(card["id"], None)
            if timed_out or rc != 0 or card["id"] in self.cancelled:
                return (rc or 1), (timed_out and deadline is not None and time.time() >= deadline)
            limit = time.time() + 1800
            if self.sync_method() == "rsync":
                cmd = list(self.rsync_cmd) + [
                    "-az", *(f"--exclude={x}" for x in sorted(self.SYNC_SKIP)),
                    "-e", " ".join(shlex.quote(x) for x in base),
                    f"{self.root}/", f"{dest}:{m['workdir'].rstrip('/')}/"]
                log.write(f"$ rsync -az {self.root}/ {dest}:{m['workdir'].rstrip('/')}/\n".encode())
                log.flush()
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **CHILD_KW)
                rc, timed_out = self._wait(card, p, limit if deadline is None else min(deadline, limit))
            else:
                rc, timed_out = self._sync_tar(m, base, dest, log, card,
                                               limit if deadline is None else min(deadline, limit))
            if timed_out and (deadline is None or time.time() < deadline):
                log.write(b"sync error: the copy took longer than 30 minutes\n")
                return 1, False
            return rc, timed_out
        except OSError as e:
            log.write(f"sync error: {e}\n".encode())
            return 1, False

    def _sync_tar(self, m, base, dest, log, card, deadline):
        """Stream the project as a .tar.gz into `tar -x` on the machine (needs only ssh here and tar there)."""
        wd = self._rpath(m["workdir"])
        log.write(f"$ (tar of {self.root}) | ssh {dest} 'tar -xzf - -C {m['workdir']}'\n".encode())
        log.flush()
        p = subprocess.Popen(base + [dest, f"tar -xzf - -C {wd}"], stdin=subprocess.PIPE, stdout=log,
                             stderr=subprocess.STDOUT, **CHILD_KW)
        sent = {"files": 0, "error": None}

        def skip(info):
            parts = info.name.replace("\\", "/").split("/")
            if any(x in self.SYNC_SKIP for x in parts) or info.name.endswith(".pyc"):
                return None
            if info.isfile():
                sent["files"] += 1
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            return info

        def writer():
            try:
                with tarfile.open(fileobj=p.stdin, mode="w|gz") as tf:
                    for entry in sorted(os.listdir(self.root)):
                        if entry not in self.SYNC_SKIP:
                            tf.add(str(self.root / entry), arcname=entry, filter=skip)
            except (OSError, ValueError) as e:  # ssh went away (Stop, timeout, no connection)
                sent["error"] = e
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        rc, timed_out = self._wait(card, p, deadline)
        t.join(5)
        if rc == 0 and sent["error"] is None:
            log.write(f"[pytest-board] copied {sent['files']} file(s) to {m['workdir']}\n".encode())
        elif sent["error"] is not None and rc == 0:
            log.write(f"sync error: {sent['error']}\n".encode())
            rc = 1
        return rc, timed_out
