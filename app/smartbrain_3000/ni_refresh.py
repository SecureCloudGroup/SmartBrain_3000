"""Refresh upgrades for recipe cards (plan 6.1, S9): the constraint re-check and when a failure may ask for a new source.

A card built by the researcher carries a sealed ``recipe``. Every successful fetch re-runs the checks that
passed when the user kept it (``drift_reasons``); a source that answers but no longer the question fails the
run as ``ni.CONSTRAINT_DRIFT``. A drifted or dead source may then ask for a re-research (``reason_of``,
``trigger_met``, ``may_research``): the worker in ``ni`` runs the researcher and PARKS the result as a proposal
the user approves. Pure code, no model, no network. A spec with no ``recipe`` never reaches any of it.
"""
from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, ni, ni_compare, ni_recipe
from .ni_research import tools
from .ni_research.ctx import ResearchCtx

# the verifier classes a refresh can re-run on the outputs alone (units, numbers, freshness, subject and
# stable need the research context or a second fetch, so they are not re-run here)
RECHECKED = frozenset({"place", "window", "threshold", "compare", "empty"})
# attempt rows in ni_runs: telemetry only (the ring is pruned to 50). The bounds live in the sealed spec marker
# ``_research_last_attempt`` (written before the model call, so a crash still counts as the attempt)
RUN_STARTED = "repair_research_started"
RUN_PROPOSED = "repair_research_proposed"
RUN_DECLINED = "repair_research_declined"
RESEARCH_EVERY = timedelta(hours=24)
DECLINE_CAP_DAYS = 7  # after a decline in a streak: wait 1 day, then 2, 4, ... never more than this
# the process-wide ceiling: background re-researches per UTC day, all cards together (0 = off)
ENV_DAILY = "SMARTBRAIN_NI_RERESEARCH_DAILY"
META_DAILY = "ni_reresearch_daily"
META_COUNT = "ni_reresearch_count"
DEFAULT_DAILY = 10
_BUDGET_LOCK = threading.Lock()
# how readily each quality tier asks for a re-research, per reason: "first" = on the first such failure,
# "ladder" = only once the card is failing (the backoff walked; no local repair applies to these statuses)
TRIGGER = {
    "curated": {"constraint_drift": "ladder", "source_dead": "ladder"},
    "verified_endpoint": {"constraint_drift": "first", "source_dead": "ladder"},
    "page_reading": {"constraint_drift": "first", "source_dead": "ladder"},
}
_STATES = {"first": ("degraded", "failing"), "ladder": ("failing",)}


def _rechecked(recipe: dict) -> set[str]:
    """The checks to re-run: only those with a passing verification record from Keep. A stated constraint with
    no record was never checked then, so a drift on it would be a first verdict, not a regression (S9 review L4)."""
    assert isinstance(recipe, dict), "a recipe"
    checks = (recipe.get("verification") or {}).get("checks") or []
    passed = {c["check"] for c in checks if c.get("ok")}
    failed = {c["check"] for c in checks if not c.get("ok")}
    wanted = (passed - failed) & RECHECKED
    assert wanted <= RECHECKED, "only re-runnable checks"
    return wanted


def _source_info(recipe: dict) -> dict:
    """What the place check had about the source at Keep (S9 review L2): the Library scope recorded on the
    recipe's first source. Cards kept before it was recorded have none, and read as before (the URL alone)."""
    sources = recipe.get("sources")
    first = sources[0] if isinstance(sources, list) and sources and isinstance(sources[0], dict) else {}
    scope = first.get("scope")
    return {"scope": scope} if isinstance(scope, str) and scope else {}


def drift_reasons(spec: dict, outputs: dict, now: datetime) -> list[str]:
    """One line per constraint that passed at Keep and fails on these fresh outputs; [] when all hold (or
    the spec has no recipe). Runs the research verifier on the outputs, exactly as ``try_recipe`` did."""
    assert isinstance(spec, dict), "spec must be a dict"
    assert now.tzinfo is not None, "now must be aware"
    recipe = spec.get("recipe")
    if not isinstance(recipe, dict) or not isinstance(outputs, dict):
        return []
    wanted = _rechecked(recipe)
    if not wanted:
        return []
    ctx = ResearchCtx(request=str(spec.get("goal") or ""), question=recipe["question"], now=now,
                      zone=ZoneInfo(ni.user_timezone_name()))
    found = tools.checks_for(ctx, outputs, str((spec.get("source") or {}).get("url") or ""), _source_info(recipe))
    return [f"{c['check']}: {c['reason']}" if c["reason"] else c["check"]
            for c in found if c["check"] in wanted and not c["ok"]]


def reason_of(item: dict) -> str | None:
    """Why this failing card could ask for a new source: ``constraint_drift`` (its last status), ``source_dead``
    (the fetch keeps failing, or one compared thing's address does: ``compare_partial``), else None."""
    assert isinstance(item, dict), "item required"
    status = str(item.get("last_status") or "")
    out = {ni.CONSTRAINT_DRIFT: "constraint_drift", "fetch_failed": "source_dead",
           ni_compare.PARTIAL: "source_dead"}.get(status)
    assert out is None or out in ni.RESEARCH_REASONS, "a closed reason"
    return out


def trigger_met(item: dict, spec: dict) -> bool:
    """True when this card's tier lets this failure ask for a re-research now (the small table above)."""
    assert isinstance(item, dict) and isinstance(spec, dict), "item + spec required"
    assert set(TRIGGER) == ni_recipe.QUALITY_TIERS, "the table covers every tier"
    recipe = spec.get("recipe")
    reason = reason_of(item)
    when = TRIGGER.get((recipe or {}).get("tier"), {}).get(reason or "")
    if when is None or item.get("state") not in _STATES[when] or item.get("first_failure_at") is None:
        return False
    return not isinstance(spec.get("_l1_trial"), dict)  # a repair trial in flight has not scored yet


def _when(ts: object) -> datetime | None:
    """A stored timestamp as an aware instant, or None."""
    assert ts is not None, "a timestamp"
    assert RESEARCH_EVERY.total_seconds() > 0, "a positive window"
    try:
        out = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return out if out.tzinfo is not None else out.replace(tzinfo=UTC)


def decline_wait(declines: int) -> timedelta:
    """How long the next attempt waits after ``declines`` declines in one streak: 1 day, then 2, then 4, capped at 7."""
    assert isinstance(declines, int), "a count"
    assert DECLINE_CAP_DAYS >= 1, "a positive cap"
    return timedelta(days=min(2 ** max(declines - 1, 0), DECLINE_CAP_DAYS))


def may_research(marker: object, first_failure: datetime | None, now: datetime) -> bool:
    """False when the last attempt (the card's sealed ``_research_last_attempt``, which outlives the pruned
    ``ni_runs`` ring) started within its wait, or already parked a proposal in this failure streak (a dismissed
    proposal is not asked for again until the streak resets). The wait is a day, longer after declines in this
    streak (``decline_wait``). No marker: allowed."""
    assert now.tzinfo is not None, "now must be aware"
    if not isinstance(marker, dict):
        return True
    at = _when(marker.get("at") or "")
    if at is None:
        return True
    streak = _when(marker["streak"]) if marker.get("streak") else None
    same = streak is not None and first_failure is not None and streak == _when(first_failure)
    if same and marker.get("outcome") == "proposed":
        return False
    declines = marker.get("declines") if same and isinstance(marker.get("declines"), int) else 0
    return now - at >= max(RESEARCH_EVERY, decline_wait(declines) if declines else RESEARCH_EVERY)


def next_marker(prev: object, first_failure: datetime | None, now: datetime, outcome: str) -> dict:
    """The ``_research_last_attempt`` for an attempt that starts now (``started``) or ends (``proposed`` /
    ``declined``, keeping the start time): the streak it ran in and the declines so far in that streak."""
    assert outcome in ni.RESEARCH_OUTCOMES, "a closed outcome"
    assert now.tzinfo is not None, "now must be aware"
    ff = _when(first_failure) if first_failure is not None else None
    streak = ff.isoformat() if ff is not None else None
    old = prev if isinstance(prev, dict) else {}
    declines = old.get("declines") if old.get("streak") == streak and isinstance(old.get("declines"), int) else 0
    at = now.isoformat()
    if outcome != "started" and old.get("streak") == streak and old.get("at"):
        at = str(old["at"])  # the start stays the clock for the wait
    if outcome == "declined":
        declines += 1
    return {"at": at, "streak": streak, "outcome": outcome, "declines": min(int(declines), 1000)}


def daily_limit(conn) -> int:
    """Background re-researches allowed per UTC day across all cards: env ``SMARTBRAIN_NI_RERESEARCH_DAILY``, then
    meta ``ni_reresearch_daily``; default 10. 0 turns background re-research off. Unreadable values read as the default."""
    assert conn is not None, "conn required"
    raw = os.environ.get(ENV_DAILY)
    if raw is None or not raw.strip():
        raw = db.meta_get(conn, META_DAILY)
    try:
        return max(0, min(int(str(raw).strip()), 10_000)) if raw is not None and str(raw).strip() else DEFAULT_DAILY
    except ValueError:
        return DEFAULT_DAILY


def _spent(conn, now: datetime) -> int:
    """Re-researches started today (UTC), from the durable meta counter ``ni_reresearch_count`` = ``day:count``."""
    assert now.tzinfo is not None, "now must be aware"
    day, _, count = str(db.meta_get(conn, META_COUNT) or "").partition(":")
    return int(count) if day == now.astimezone(UTC).date().isoformat() and count.isdigit() else 0


def budget_left(conn, now: datetime) -> bool:
    """True when today's background re-research budget has room (a read; ``take_budget`` spends it)."""
    assert conn is not None, "conn required"
    return _spent(conn, now) < daily_limit(conn)


def take_budget(conn, now: datetime) -> bool:
    """Spend one of today's background re-researches; False (nothing spent) when the day's budget is used up."""
    assert conn is not None, "conn required"
    with _BUDGET_LOCK:
        used = _spent(conn, now)
        if used >= daily_limit(conn):
            return False
        db.meta_set(conn, META_COUNT, f"{now.astimezone(UTC).date().isoformat()}:{used + 1}")
        return True
