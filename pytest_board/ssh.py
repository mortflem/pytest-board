"""SshMixin: ssh command lines, paths and commands on the machine, and the env layers a run gets."""
import os
import posixpath
import shlex

from .envfiles import read_env_file
from .model import ENV_KEY_RX, NO_ENV_FILE


class SshMixin:
    """Part of Runner (see runner.py); uses its attributes."""

    @staticmethod
    def remote_env_path(m):
        """The machine's own .env (a path on the machine, relative to its workdir), or None when turned off."""
        v = m.get("remote_env_file") or ".env"
        return None if v.lower() in NO_ENV_FILE else v

    def env_file_notes(self, g, m=None):
        """Log lines: which .env files on this server a run uses (names only), and problems with them."""
        out = []
        for path, what in ((self.env_file, None), (m and m.get("env_file_path"), m and f"machine {m['name']}"),
                           (g and g.get("env_file"), g and f"headline {g['key']}")):
            if not path:
                continue
            env, err, exists = read_env_file(path)
            if exists:
                out.append(f"[pytest-board] env file {path}: {', '.join(sorted(env)) or 'no variables'}")
            elif what:  # named in the config, but not there (the board-wide .env is optional)
                out.append(f"[pytest-board] WARNING: env file {path} (env_file of {what}) not found")
            if err:
                out.append(f"[pytest-board] WARNING: {err}")
        return "".join(ln + "\n" for ln in out)

    # --- SSH
    def ssh_base(self, m, interactive=False, keepalive=(30, 4)):
        """
        ssh client arguments for a machine. interactive=True is for a person at a terminal (Open shell):
        a remote tty, and ssh may ask for a password or passphrase there instead of failing.
        keepalive: (seconds, count) after which a silent connection counts as dropped.
        """
        # user options go first: for ssh the first value given for an option wins
        cmd = list(self.ssh_cmd) + shlex.split(m["ssh_options"] or "")
        cmd += ["-tt"] if interactive else ["-T", "-o", "BatchMode=yes"]
        cmd += ["-o", "ConnectTimeout=10", "-o", f"ServerAliveInterval={keepalive[0]}",
                "-o", f"ServerAliveCountMax={keepalive[1]}", "-o", "StrictHostKeyChecking=accept-new"]
        if m["port"]:
            cmd += ["-p", str(m["port"])]
        if m["identity"]:
            cmd += ["-i", os.path.expanduser(m["identity"])]
        return cmd

    @staticmethod
    def dest(m):
        return f"{m['user']}@{m['host']}" if m["user"] else m["host"]

    @staticmethod
    def _rpath(path):
        """A remote path for sh, with a leading ~ expanded by the remote shell."""
        if path in ("", "~"):
            return '"$HOME"'
        if path.startswith("~/"):
            return '"$HOME"/' + shlex.quote(path[2:])
        return shlex.quote(path)

    @staticmethod
    def _rword(word):
        """One word of a remote command for sh. A leading ~ or ~/ is expanded by the remote shell, as when
        you type it there (e.g. python = ~/git/venv/bin/pytest); everything else is quoted."""
        if word == "~":
            return '"$HOME"'
        if word.startswith("~/"):
            return '"$HOME"/' + shlex.quote(word[2:])
        return shlex.quote(word)

    def remote_dir(self, m, g):
        wd = m["workdir"] or "~"
        if g and g.get("dir"):
            wd = posixpath.join(wd, g["dir"])
        return wd

    def remote_argv(self, item, m, g):
        argv = (shlex.split(m["python"]) + shlex.split(g["args"] if g else "")
                + shlex.split(item.get("target") or "") + shlex.split(item.get("args") or ""))
        argv += shlex.split(item.get("optset") or "")  # the card's option set
        return argv

    def remote_env_layers(self, item, m, g, run_id=None):
        """
        Env for a run on a machine, as (low, high): low is set before the machine's own .env is loaded there,
        high after it, so later values win. From least to most specific:
        .env on this server < the machine's .env < machine env_file / env < headline env_file / env < test env.
        """
        low = {"PYTHONUNBUFFERED": "1", "PYTEST_BOARD_MACHINE": m["name"]}
        if run_id is not None:
            low["PYTEST_BOARD_RUN_ID"] = str(run_id)
        if item.get("grp"):
            low["PYTEST_BOARD_GROUP"] = item["grp"]
        low.update(read_env_file(self.env_file)[0])          # .env next to pytest-board.ini
        high = dict(read_env_file(m.get("env_file_path"))[0])  # machine env_file
        high.update(m["env"])                                # machine env
        if g:
            high.update(read_env_file(g["env_file"])[0])      # headline env_file
            high.update(g["env"])                            # headline env
        high.update(self.parse_env(item.get("env")))         # the test's own env wins
        ok = lambda d: {k: v for k, v in d.items() if ENV_KEY_RX.match(k) and k != "PYTEST_BOARD_TOKEN"}
        return ok(low), ok(high)

    @staticmethod
    def _ssh_error(err):
        lines = [ln.strip() for ln in err.decode("utf-8", "replace").splitlines()
                 if ln.strip() and not ln.startswith("Warning: Permanently")]
        return lines[-1][:300] if lines else ""

    def _machine_env_file(self, m):
        """(sh expression, shown path) of the machine's own .env, relative paths from its workdir; or None."""
        f = self.remote_env_path(m)
        if not f:
            return None
        if f.startswith(("/", "~")):
            return self._rpath(f), f
        return "./" + shlex.quote(f), posixpath.join(m["workdir"] or "~", f)

    def _source_machine_env(self, m, errfile='"$D/env.err"'):
        """sh lines for the launcher: load the machine's own .env (as `set -a; . file`), if it has one."""
        mf = self._machine_env_file(m)
        if not mf:
            return ""
        f, shown = mf
        return (f"if cd {self._rpath(m['workdir'] or '~')} 2>/dev/null && [ -f {f} ]; then\n"
                f'  if ( . {f} ) >/dev/null 2>{errfile}; then\n'
                f"    set -a; . {f} >/dev/null 2>&1; set +a\n"
                f"    printf 'PB_NOTE loaded %s on {m['name']}\\n' {shlex.quote(shown)}\n"
                f"  else\n"
                f"    printf 'PB_ERR %s on {m['name']} has an error: %s\\n' {shlex.quote(shown)} "
                f'"$(head -c 300 {errfile} | tr \'\\n\' \' \')"; exit 3\n'
                f"  fi\n"
                f"fi\n"
                f"cd\n")
