"""Standing orders: an ask no source answers yet that the user asked SmartBrain to keep looking for (release P2).

An ask answers in seconds from the Library and the user's own sources; when nothing fits, research runs at once under
the card's consent (``ni_research_flow.fallback``) and, when it finds nothing it can trust, the card declines and
offers **Keep looking**. Keep looking seals an order on the card's flow record,
``_looking = {since, runs, next_at, free?, free_at?}``, and runs the ask again with research allowed. While the order
is open the card stays declined and says it is still looking; the scheduler runs the ask again when ``next_at``
comes (1, 2, 4, then every 7 days, each research run under the shared daily ceiling ``ni_refresh.take_budget``),
and a source the user keeps (``ni_intake.remember``) makes every open order due at once as a free run: kept and
Library sources only, no model. A run that finds a source lands the card held for Keep, as any research card. An
order that finds nothing for ``LOOK_DAYS`` closes, and the card keeps its decline and links.

A run starts only with a free worker slot, and nothing is written or charged before that is known: an order whose
run cannot start now stays open, unchanged, and the next pass tries again. An order that can never run (its start
time is unreadable, its request is gone, research is off) closes rather than staying open without the user.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from . import ni, ni_flow, ni_master, ni_refresh, ni_research_flow

log = logging.getLogger(__name__)

LOOK_DAYS = 14                 # an order that finds nothing this long closes
BACKOFF_DAYS = (1, 2, 4, 7)    # the wait after the n-th run (the last repeats)
MAX_OPEN = 20                  # open orders a pass reads (the oldest due first)


def _now() -> datetime:
    return ni._clock().astimezone(UTC)


def _iso(moment: datetime) -> str:
    assert moment.tzinfo is not None, "an aware time"
    return moment.isoformat(timespec="seconds")


def _when(text: object) -> datetime | None:
    """A stored timestamp as an aware time, or None when it is missing or not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _after(runs: int, now: datetime) -> str:
    """The next run of an order that has run ``runs`` times."""
    assert runs >= 1, "an order that ran"
    return _iso(now + timedelta(days=BACKOFF_DAYS[min(runs, len(BACKOFF_DAYS)) - 1]))


def lookable(record: dict) -> bool:
    """Can this declined record become a standing order? Only a decline that looking longer could answer."""
    return ni_master.lookable(record)


def _reseed(store: ni.NIStore, item_id: str, record: dict, looking: dict, note: str) -> dict | None:
    """The card's sealed request as a fresh record with ``looking`` as its order (the user's own answers and the
    intent it was understood under ride along, so looking again asks no model to read the words again), written
    and ready for ``run_flow``; None when the record lost its request."""
    request = str(record.get("request") or "")
    if not request:
        return None
    state = "source" if isinstance(record.get("intent"), dict) else "intent"
    fresh = {**ni_flow._fresh_record(record, request, state, note, keep_named=True), "_looking": looking}
    skipped = sorted(ni_flow.skipped_sites(record))
    if skipped:  # a site skipped for want of a key stays skipped while the card looks: each look asked for it again
        fresh["_skipped"] = skipped
    ni_flow._flow_write(store, item_id, fresh, revive=True)
    return fresh


def _put_back(store: ni.NIStore, item_id: str, record: dict, looking: dict | None) -> None:
    """The declined record as it was, with ``looking`` as its order: a run that could not start changes nothing
    else (the refused start had sealed ``failed(busy)`` over the reseeded record)."""
    assert record.get("state") == "declined", "a declined record"
    ni_flow._flow_write(store, item_id, {**record, "_looking": looking}, revive=True)


def _rerun(store: ni.NIStore, item_id: str, record: dict, looking: dict, note: str) -> bool:
    """Run the card's sealed request again, in the background, with ``looking`` as its order. Only with a free
    worker slot; a start refused anyway (the slot went between the check and the start) puts the declined record
    back as it was, so the order stays open and the next pass runs it."""
    if not ni_flow.has_slot(item_id):
        return False
    fresh = _reseed(store, item_id, record, looking, note)
    if fresh is None:
        return False
    if ni_flow.start_flow_worker(store, item_id, source_url=fresh.get("source_url")):
        return True
    _put_back(store, item_id, record, record.get("_looking"))
    return False


def seal(store: ni.NIStore, item_id: str, record: dict) -> dict | None:
    """Keep looking, without running: the order sealed and the record reset to run the ask again with research
    allowed (asking for model consent first when the card has none). ``start`` runs it in the background; the eval
    harnesses call ``run_flow`` themselves, as a person tapping Keep looking."""
    assert store is not None and item_id, "args required"
    assert lookable(record), "a decline looking could answer"
    now = _now()
    looking = {"since": _iso(now), "runs": 1, "next_at": _after(1, now)}
    return _reseed(store, item_id, record, looking, "keep looking: running the ask again, research allowed")


def start(store: ni.NIStore, item_id: str, record: dict) -> bool:
    """Keep looking: seal the order and look now, in the background. Returns whether the look started. With no
    worker slot free the order is sealed on the declined card as a first run due now (``runs`` 0, the user's own
    tap, so not charged to the daily ceiling), and the scheduler's next pass runs it."""
    assert lookable(record), "a decline looking could answer"
    if ni_flow.has_slot(item_id):
        fresh = seal(store, item_id, record)
        if fresh is not None and ni_flow.start_flow_worker(store, item_id, source_url=fresh.get("source_url")):
            return True
    now = _iso(_now())
    _put_back(store, item_id, record, {"since": now, "runs": 0, "next_at": now})
    return False


def _declined(store: ni.NIStore, item_id: str, record: dict, looking: dict | None, note: str | None = None) -> None:
    """Rewrite the order on a declined record; the decline itself (its error, seal and links) stays as it was."""
    ni_flow._transition(store, item_id, "declined", error=record.get("error"), _looking=looking,
                        **({"note": note} if note else {}))


def stop(store: ni.NIStore, item_id: str) -> None:
    """Stop looking: the order closes; the card keeps its decline and links."""
    assert store is not None and item_id, "args required"
    _declined(store, item_id, ni_flow._flow_read(store, item_id) or {}, None, "you stopped looking for a source")


def _open(store: ni.NIStore) -> list[tuple[dict, dict]]:
    """The shells with an open order: (item, flow record), at most ``MAX_OPEN``, the soonest due first."""
    found: list[tuple[dict, dict]] = []
    for item in store.list_items():  # bounded by ni._MAX_ITEMS
        if item["spec"].get("_shell") is not True:
            continue
        record = ni_flow._flow_read(store, item["id"]) or {}
        if record.get("state") == "declined" and isinstance(record.get("_looking"), dict):
            found.append((item, record))
    found.sort(key=lambda pair: str(pair[1]["_looking"].get("free_at") or pair[1]["_looking"].get("next_at") or ""))
    return found[:MAX_OPEN]


def rematch(store: ni.NIStore) -> int:
    """A new source of the user's own exists: every open order looks again at once, for free (no model). Returns
    how many orders were marked."""
    assert store is not None, "store required"
    marked = 0
    stamp = _iso(_now())
    for item, record in _open(store):  # bounded: MAX_OPEN
        _declined(store, item["id"], record, {**record["_looking"], "free_at": stamp})
        marked += 1
    return marked


def run_due(store: ni.NIStore) -> str | None:
    """One scheduler pass: close the orders past ``LOOK_DAYS`` (or that can never run) and run the first one due (a
    free re-match first). Returns the id of the card started, or None. A scheduled research run waits for a free
    worker slot and the shared daily ceiling, and is charged only once both are there."""
    assert store is not None, "store required"
    now = _now()
    for item, record in _open(store):  # bounded: MAX_OPEN
        looking = record["_looking"]
        since = _when(looking.get("since"))
        if since is None or now - since > timedelta(days=LOOK_DAYS):   # an unreadable start closes: none runs forever
            _declined(store, item["id"], record, None, f"stopped looking after {LOOK_DAYS} days without finding a source")
            continue
        if not str(record.get("request") or ""):
            _declined(store, item["id"], record, None, "stopped looking: this card lost its request")
            continue
        if ni_flow.is_working(item["id"]):
            continue
        if not ni_flow.has_slot(item["id"]):
            return None   # every worker slot is busy: nothing is written or charged, and the next pass tries again
        if looking.get("free_at") and isinstance(record.get("intent"), dict):   # free: no model reads the ask again
            order = {k: v for k, v in looking.items() if k != "free_at"} | {"free": True}
            started = _rerun(store, item["id"], record, order, "a new source of yours: looking again (no model)")
            return item["id"] if started else None
        due = _when(looking.get("next_at"))
        if due is None or due > now:
            continue
        if ni_flow._resolve_library() is None:
            return None   # the Library is not there this tick (a fault, not the user's choice): every order waits
        if not ni_research_flow.can_research(store, item["id"], record):
            _declined(store, item["id"], record, None, "stopped looking: research is off")
            continue
        count = looking.get("runs")
        runs = count if isinstance(count, int) and count >= 0 else 1   # an unreadable count is a scheduled run
        asks = ni_research_flow.plan_for(store, item["id"], record).mode == "ask"   # stops at the consent question
        if runs >= 1 and not asks and not ni_refresh.take_budget(store.conn, now):
            return None   # the day's research is spent: every later order waits too
        order = {"since": looking["since"], "runs": runs + 1, "next_at": _after(runs + 1, now)}
        started = _rerun(store, item["id"], record, order, f"still looking: run {runs + 1}")
        return item["id"] if started else None
    return None
