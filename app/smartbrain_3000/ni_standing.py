"""Standing orders: an ask no source answers yet that the user asked SmartBrain to keep looking for (release P2).

An ask answers in seconds from the Library and the user's own sources; when nothing fits, the card declines at once
(``no_ready_source``) and offers **Keep looking**. Keep looking seals an order on the card's flow record,
``_looking = {since, runs, next_at, free?, free_at?}``, and runs the ask again with research allowed. While the order
is open the card stays declined and says it is still looking; the scheduler runs the ask again when ``next_at``
comes (1, 2, 4, then every 7 days, each research run under the shared daily ceiling ``ni_refresh.take_budget``),
and a source the user keeps (``ni_intake.remember``) makes every open order due at once as a free run: kept and
Library sources only, no model. A run that finds a source lands the card held for Keep, as any research card. An
order that finds nothing for ``LOOK_DAYS`` closes, and the card keeps its decline and links.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from . import ni, ni_flow, ni_master, ni_refresh

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
    assert isinstance(record, dict), "a record"
    return (record.get("state") == "declined"
            and ni_master.decline_view(record)["reason"] in ni_master.LOOKABLE)


def _reseed(store: ni.NIStore, item_id: str, record: dict, looking: dict, note: str) -> dict | None:
    """The card's sealed request as a fresh record with ``looking`` as its order (the user's own answers and the
    intent it was understood under ride along, so looking again asks no model to read the words again), written
    and ready for ``run_flow``; None when the record lost its request."""
    request = str(record.get("request") or "")
    if not request:
        return None
    state = "source" if isinstance(record.get("intent"), dict) else "intent"
    fresh = ni_flow._fresh_record({**record, "_looking": looking}, request, state, note, keep_named=True)
    ni_flow._flow_write(store, item_id, fresh, revive=True)
    return fresh


def _rerun(store: ni.NIStore, item_id: str, record: dict, looking: dict, note: str) -> bool:
    """Run the card's sealed request again, in the background, with ``looking`` as its order."""
    fresh = _reseed(store, item_id, record, looking, note)
    return fresh is not None and ni_flow.start_flow_worker(store, item_id, source_url=fresh.get("source_url"))


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
    """Keep looking: seal the order and look now, in the background. Returns whether the build started."""
    fresh = seal(store, item_id, record)
    return fresh is not None and ni_flow.start_flow_worker(store, item_id, source_url=fresh.get("source_url"))


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
    """One scheduler pass: close the orders past ``LOOK_DAYS`` and run the first one due (a free re-match first).
    Returns the id of the card started, or None. A research run waits for the shared daily ceiling."""
    assert store is not None, "store required"
    now = _now()
    for item, record in _open(store):  # bounded: MAX_OPEN
        looking = record["_looking"]
        since = _when(looking.get("since")) or now
        if now - since > timedelta(days=LOOK_DAYS):
            _declined(store, item["id"], record, None, f"stopped looking after {LOOK_DAYS} days without finding a source")
            continue
        if ni_flow.is_working(item["id"]):
            continue
        if looking.get("free_at") and isinstance(record.get("intent"), dict):   # free: no model reads the ask again
            order = {k: v for k, v in looking.items() if k != "free_at"} | {"free": True}
            if _rerun(store, item["id"], record, order, "a new source of yours: looking again (no model)"):
                return item["id"]
            continue
        due = _when(looking.get("next_at"))
        if due is None or due > now:
            continue
        if not ni_refresh.take_budget(store.conn, now):
            return None   # the day's research is spent: every later order waits too
        runs = int(looking.get("runs") or 0) + 1
        order = {"since": looking["since"], "runs": runs, "next_at": _after(runs, now)}   # stamped before the run
        if _rerun(store, item["id"], record, order, f"still looking: run {runs}"):
            return item["id"]
    return None
