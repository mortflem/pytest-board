"""The machines: the ones in the config file plus the ones added in the UI."""
import sys

from .model import normalize_machine


class Machines:
    """Config-file machines ([machine:NAME]) plus machines added in the UI (stored in the DB)."""

    def __init__(self, store, groups):
        self.s = store
        self.groups = groups
        self.status = {}  # name -> {state, checked, detail, system}

    def all(self):
        cfg = list(self.groups.machines)
        names = {m["name"] for m in cfg}
        ui = []
        for r in self.s.q("SELECT * FROM machines ORDER BY position, id"):
            if r["name"] in names:
                continue  # the config file wins on a name clash
            try:
                ui.append(normalize_machine(r, r["name"], "ui", r["id"], base_dir=self.groups.base_dir()))
            except ValueError as e:
                print(f"machine {r['name']}: {e}", file=sys.stderr)
        return cfg + ui

    def get(self, name):
        if not name or name == "local":
            return None
        return next((m for m in self.all() if m["name"] == name), None)

    def set_status(self, name, **kw):
        self.status[name] = dict(self.status.get(name, {}), **kw)
        self.s.version += 1
