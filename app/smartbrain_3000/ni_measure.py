"""The live measure (release P4, T 2026-10-24): how each card was built, whether the user kept it, and whether it
still works seven days later. A card the user keeps and that keeps working is the outcome that matters, so this
replaces the gold list as the measure of "great".

One row per verdict on a card: Keep or No on its first reading, or deleting a card still held for Keep. The rows live
in the plaintext ``meta`` row ``ni_outcomes`` as a bounded JSON list, ``{item, path, verdict, at, day7}``: ``path``
is the card's sealed ``_built_from.path`` (a closed word, never a host or a title) and ``item`` the card's random id.
Seven days after a Keep the scheduler stamps ``day7`` from the card as it stands then: ``live``, ``failing``,
``paused`` or ``gone``. Local only: nothing here is sent anywhere.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta

from . import db, ni

log = logging.getLogger(__name__)

META_KEY = "ni_outcomes"
MAX_ROWS = 500           # the newest verdicts kept; older ones drop off
SURVIVAL_DAYS = 7
MAX_CHECKS = 20          # day-7 stamps per scheduler pass
VERDICTS = ("kept", "dismissed")
DAY7 = ("live", "failing", "paused", "gone")
# How the card was built, in the plan's words, from its sealed path: the Library (an auto-pick, a Library row's
# declared answers, the bundled seed), a source the user kept before, research, or a link the user pasted (a page or
# a mapped dataset).
SOURCES: dict[str, str] = {"auto": "library", "declared": "library", "seed": "library", "kept": "own",
                           "research": "research", "page": "paste", "mapping": "paste"}
SOURCE_ORDER = ("library", "own", "research", "paste", "other")
_LOCK = threading.Lock()


def _now() -> datetime:
    return ni._clock().astimezone(UTC)


def _when(text: object) -> datetime | None:
    """A stored timestamp as an aware time, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _row_ok(row: object) -> bool:
    """A row this version can read (the meta row is plaintext an older or newer version may have written)."""
    return (isinstance(row, dict) and isinstance(row.get("item"), str) and row.get("verdict") in VERDICTS
            and isinstance(row.get("path"), str) and _when(row.get("at")) is not None
            and row.get("day7") in (None, *DAY7))


def _rows(conn) -> list[dict]:
    """The readable rows, oldest first, at most ``MAX_ROWS``."""
    try:
        raw = json.loads(db.meta_get(conn, META_KEY) or "[]")
    except ValueError:
        return []
    return [r for r in raw[-MAX_ROWS:] if _row_ok(r)] if isinstance(raw, list) else []


def _write(conn, rows: list[dict]) -> None:
    assert len(rows) <= MAX_ROWS, "bounded"
    db.meta_set(conn, META_KEY, json.dumps(rows, separators=(",", ":")))


def record(store: ni.NIStore, item: dict, *, kept: bool) -> None:
    """One verdict on ``item`` (its spec as it was when the user decided)."""
    assert store is not None and isinstance(item, dict) and item.get("id"), "store + item required"
    built = (item.get("spec") or {}).get("_built_from") or {}
    path = built.get("path") if built.get("path") in ni._BUILT_FROM_PATHS else "other"
    row = {"item": str(item["id"])[:64], "path": path, "verdict": "kept" if kept else "dismissed",
           "at": _now().isoformat(timespec="seconds"), "day7": None}
    with _LOCK:
        _write(store.conn, [*_rows(store.conn), row][-MAX_ROWS:])


def record_best_effort(store: ni.NIStore, item: dict, *, kept: bool) -> None:
    """``record`` that never fails the user's tap: the measure is a reading, not a gate."""
    try:
        record(store, item, kept=kept)
    except Exception as exc:  # a store fault must never turn a Keep into an error
        log.warning("ni measure: recording a verdict failed: %s", type(exc).__name__)


def _day7_of(item: dict | None) -> str:
    """What a kept card is seven days on: still live, paused by the user, gone, or anything else (failing)."""
    if item is None:
        return "gone"
    state = item.get("state")
    return "live" if state == "live" else "paused" if state == "paused" else "failing"


def check_due(store: ni.NIStore) -> int:
    """One scheduler pass: stamp ``day7`` on kept rows seven days old (at most ``MAX_CHECKS``). Returns how many."""
    assert store is not None, "store required"
    cutoff = _now() - timedelta(days=SURVIVAL_DAYS)
    stamped = 0
    with _LOCK:
        rows = _rows(store.conn)
        for row in rows:  # bounded: MAX_ROWS
            if stamped >= MAX_CHECKS:
                break
            if row["verdict"] != "kept" or row["day7"] is not None or _when(row["at"]) > cutoff:
                continue
            row["day7"] = _day7_of(store.get_item(row["item"]))
            stamped += 1
        if stamped:
            _write(store.conn, rows)
    return stamped


def _counts() -> dict:
    return {"kept": 0, "dismissed": 0, "waiting": 0, **{k: 0 for k in DAY7}}


def view(store: ni.NIStore) -> dict:
    """The measure as the diagnostics show it and the eval exports it: counts per way the card was built and in
    total (``kept``, ``dismissed``, and for kept cards ``waiting`` for day 7 or one of ``DAY7``), plus the rows."""
    assert store is not None, "store required"
    rows = _rows(store.conn)
    by = {source: _counts() for source in SOURCE_ORDER}
    for row in rows:  # bounded: MAX_ROWS
        cell = by[SOURCES.get(row["path"], "other")]
        cell[row["verdict"]] += 1
        if row["verdict"] == "kept":
            cell[row["day7"] or "waiting"] += 1
    total = {k: sum(cell[k] for cell in by.values()) for k in _counts()}
    return {"since": rows[0]["at"] if rows else None, "by_source": by, "total": total, "rows": rows}
