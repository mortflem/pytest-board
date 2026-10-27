"""Cron expressions (5 fields or @daily & co.), evaluated in the server's local time."""
from datetime import datetime, timedelta

CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}
_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


def _cron_field(spec, lo, hi, name):
    spec = spec.lower()
    for k, v in _NAMES.items():
        spec = spec.replace(k, str(v))
    values = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(f"cron {name} field out of range: {spec!r}")
        values.update(range(a, b + 1, step))
    return values


class Cron:
    """Standard 5-field cron: minute hour day-of-month month day-of-week (server local time)."""

    def __init__(self, expr):
        expr = CRON_ALIASES.get(expr.strip().lower(), expr.strip())
        f = expr.split()
        if len(f) != 5:
            raise ValueError("cron needs 5 fields: minute hour day month weekday (or @hourly/@daily/@weekly)")
        try:
            self.minute = _cron_field(f[0], 0, 59, "minute")
            self.hour = _cron_field(f[1], 0, 23, "hour")
            self.dom = _cron_field(f[2], 1, 31, "day")
            self.month = _cron_field(f[3], 1, 12, "month")
            self.dow = {d % 7 for d in _cron_field(f[4], 0, 7, "weekday")}
        except ValueError as e:
            raise ValueError(str(e) if "cron" in str(e) else f"invalid cron expression: {expr!r}")
        # as in Vixie cron, a day field starting with * (also */2) counts as unrestricted for the day OR rule
        self.dom_any, self.dow_any = f[2].startswith("*"), f[4].startswith("*")

    def _day_ok(self, d):
        dom_ok = d.day in self.dom
        dow_ok = (d.isoweekday() % 7) in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # classic cron: either restriction matches

    def next_after(self, ts):
        d = datetime.fromtimestamp(ts).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = d + timedelta(days=366 * 5)
        while d < limit:
            if d.month not in self.month:
                d = (d.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_ok(d):
                d = d.replace(hour=0, minute=0) + timedelta(days=1)
            elif d.hour not in self.hour:
                d = d.replace(minute=0) + timedelta(hours=1)
            elif d.minute not in self.minute:
                d += timedelta(minutes=1)
            else:
                return d.timestamp()
        raise ValueError("cron expression never fires")
