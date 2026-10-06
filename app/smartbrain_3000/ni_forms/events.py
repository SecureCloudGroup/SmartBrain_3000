"""Event helpers shared by event_curve, next_event, day_table and agenda: time-ordered
events with an optional value and a structural extremum kind (local max/min by
neighbours, never by words), card-zone days, and the next event after `now`."""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime

from . import fmt
from .rec import R as RView
from .rec import is_numeric


@dataclass
class Ev:
    row: int
    t: float                 # epoch seconds (dates: card-zone noon, flagged all_day)
    iso: str
    value: float | None
    ext: str | None          # "hi" | "lo" | None (alternating extrema only)
    day: date                # card-zone date
    all_day: bool = False


def time_field(R: RView):
    """The event time: a time/date role, else the only datetime/date column that is not
    the record's own as-of (a structural fallback when role binding left it as meta)."""
    f = R.first("time", "date")
    if f is not None:
        return f
    cands = [x for x in R.rec.fields if x.type in ("datetime", "date") and x.role in ("meta", "secondary", "unknown")]
    return cands[0] if len(cands) == 1 else None


def value_field(R: RView):
    return next((f for f in R.rec.fields if is_numeric(f) and f.role in ("value", "measure")), None)


def events(R: RView, tf: str, vf: str | None = None) -> list[Ev]:
    tz = R.tz
    out = []
    for i in range(len(R.rows)):
        v = R.cell(i, tf)
        t = fmt.parse_t(v)
        if t is None:
            continue
        if isinstance(t, datetime):
            e = Ev(i, t.timestamp(), fmt.iso(t), R.num(i, vf) if vf else None, None, t.astimezone(fmt.zone(tz)).date())
        else:
            noon = datetime(t.year, t.month, t.day, 12, tzinfo=fmt.zone(tz))
            e = Ev(i, noon.timestamp(), t.isoformat(), R.num(i, vf) if vf else None, None, t, True)
        out.append(e)
    out.sort(key=lambda e: (e.t, e.row))
    if vf:
        vals = [e.value for e in out]
        for k, e in enumerate(out):
            if e.value is None:
                continue
            nb = [vals[j] for j in (k - 1, k + 1) if 0 <= j < len(out) and vals[j] is not None]
            if nb and all(e.value > x for x in nb):
                e.ext = "hi"
            elif nb and all(e.value < x for x in nb):
                e.ext = "lo"
        if not alternates(out):          # highs/lows only when the series really alternates
            for e in out:
                e.ext = None
    return out


def alternates(evs: list[Ev]) -> bool:
    ks = [e.ext for e in evs]
    return len(ks) >= 4 and all(k in ("hi", "lo") for k in ks) and all(a != b for a, b in itertools.pairwise(ks))


def next_after(evs: list[Ev], now: datetime) -> int | None:
    n = now.timestamp()
    return next((k for k, e in enumerate(evs) if e.t > n), None)


def today(now: datetime, tz: str) -> date:
    return now.astimezone(fmt.zone(tz)).date()


def iso_at(t: float) -> str:
    return fmt.iso(datetime.fromtimestamp(t, UTC))


def kind_rc(e: Ev, R: RView, kf: str | None):
    """The event's kind word: the structural extremum word, else the kind cell."""
    if e.ext == "hi":
        return ["tpl", "high", {}]
    if e.ext == "lo":
        return ["tpl", "low", {}]
    if kf and R.cell(e.row, kf) is not None:
        return ["raw", kf, e.row]
    return None
