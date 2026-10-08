"""Clock-pass boundary detection (R19 Phase 1b): a faithful port of the prototype's
`paint/refresh.py: next_boundary` over the app's DataRecord + bound CLIR dicts.

A sealed form keeps rendering true between fetches only if something re-lays it out
when the CLOCK, not the data, makes it wrong: "Today" turning into "Yesterday" at
card-tz midnight, a countdown crossing zero, a daylight-saving change moving every
card-zone hour, a `live` binding's edge (`paint.live.live_boundaries`), or the as-of
age passing the stale threshold. `next_boundary` finds the earliest such instant
strictly after `now`, or None when nothing in the record or its laid-out CLIRs is
clock-sensitive. Pure function: no model, no store, no clock of its own — `now`,
`card_tz` and `cadence_s` are always passed in by the caller (the card's own sealed
zone and cadence, never re-derived here).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from . import fmt
from .paint import live as LV
from .types import DataRecord

# Mirrors ctx.TIME_TYPES; kept local so this module has no dependency beyond fmt/live/types.
_TIME_TYPES = frozenset({"time", "date", "datetime"})
# Mirrors the proto's EVENT_ROLES.
_EVENT_ROLES = frozenset({"time", "time_end", "date"})
# Day-ish time formats: the same four the proto treats as midnight-sensitive. "MMM yyyy" /
# "MMM d, yyyy" (added to TIME_FMTS for fmt.coarse_fmt) print an explicit year — an absolute
# label, never a relabelled "Today" — so they are deliberately NOT included here.
_DAY_FMTS = frozenset({"EEE", "EEE d", "MMM d", "EEE MMM d"})


def _is_timey(record: DataRecord) -> bool:
    """True when the record (or a part, e.g. the history series) carries a time/date/
    datetime field anywhere — the record itself can go stale across a card-tz midnight."""
    if any(f.type in _TIME_TYPES for f in record.fields):
        return True
    return any(f.type in _TIME_TYPES for part in record.parts.values() for f in part.fields)


def _has_day_prims(clirs: list[dict]) -> bool:
    """True when a laid-out CLIR carries a day-ish time prim — a derived "Today" / weekday
    label can be clock-sensitive even when the sealed field type check above misses it."""
    for clir in clirs:
        for p in clir.get("prims", []):
            if p.get("k") == "time" and p.get("fmt") in _DAY_FMTS:
                return True
    return False


def _event_instant(value: object, card_tz: str) -> datetime | None:
    """A field cell's instant: an already-aware datetime cell as is; a bare calendar date
    (the `date` type) at its midnight in the card zone — never UTC, never the host's
    (ctx.ts's rule); None when the cell is empty or unparseable."""
    parsed = fmt.parse_t(value)
    if parsed is None:
        return None
    if isinstance(parsed, datetime):
        return parsed
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=fmt.zone(card_tz)).astimezone(UTC)


def _next_event_instant(record: DataRecord, now: datetime, card_tz: str) -> datetime | None:
    """The soonest FUTURE instant among time/datetime/date fields carrying an event role
    (time, time_end, date), across the record's own rows and every part's rows."""
    best: datetime | None = None
    for part in (record, *record.parts.values()):              # bounded: parts is one level only
        idx = [i for i, f in enumerate(part.fields)              # bounded: _FORM_MAX_FIELDS
               if f.type in ("time", "datetime", "date") and f.role in _EVENT_ROLES]
        if not idx:
            continue
        for row in part.rows:                                   # bounded: DataRecord caps rows at 500
            for i in idx:
                t = _event_instant(row[i], card_tz)
                if t is not None and t > now and (best is None or t < best):
                    best = t
    return best


def next_boundary(record: DataRecord, clirs: Iterable[dict], *, now: datetime,
                  card_tz: str, cadence_s: int) -> datetime | None:
    """The earliest future instant at which `record` must be re-laid out with no fetch.

    `clirs` is the bind's own laid-out CLIR dicts (e.g. ``[out_desktop.clir,
    out_phone.clir]``) — read only for their `live` bindings and day-ish time prims,
    never for field data. `card_tz` / `cadence_s` are the card's own sealed zone and
    cadence (the caller resolves "data zone else viewer's" beforehand — this function
    never falls back on its own). Returns None when nothing here is clock-sensitive
    (no time anywhere in the record, no live bindings, no cadence).
    """
    assert isinstance(record, DataRecord), "record must be a DataRecord"
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be an aware datetime"
    assert isinstance(card_tz, str) and card_tz, "card_tz required"
    assert isinstance(cadence_s, int) and not isinstance(cadence_s, bool) and cadence_s >= 0, \
        "cadence_s must be a non-negative int"
    clir_list = [c for c in clirs if c]
    cands: list[datetime] = []
    if _is_timey(record) or _has_day_prims(clir_list):
        cands.append(LV.next_midnight(now, card_tz))
        cands.extend(LV.dst_transitions(now, card_tz, 48))
    event = _next_event_instant(record, now, card_tz)
    if event is not None:
        cands.append(event)
    for clir in clir_list:
        cands.extend(LV.live_boundaries(clir, now))
    as_of = record.context.as_of or record.context.fetched_at
    if cadence_s and as_of:
        parsed = fmt.parse_t(as_of)
        if isinstance(parsed, datetime):
            stale = parsed + timedelta(seconds=2 * cadence_s)
            if stale > now:
                cands.append(stale)
    future = [c for c in cands if c > now]
    return min(future) if future else None
