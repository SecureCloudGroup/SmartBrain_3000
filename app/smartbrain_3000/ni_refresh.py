"""Refresh upgrades for recipe cards (plan 6.1, S9): the constraint re-check and when a failure may ask for a new source.

A card built by the researcher carries a sealed ``recipe``. Every successful fetch re-runs the checks that
passed when the user kept it (``drift_reasons``); a source that answers but no longer the question fails the
run as ``ni.CONSTRAINT_DRIFT``. A drifted or dead source may then ask for a re-research (``reason_of``,
``trigger_met``, ``may_research``): the worker in ``ni`` runs the researcher and PARKS the result as a proposal
the user approves. Pure code, no model, no network. A spec with no ``recipe`` never reaches any of it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ni, ni_recipe
from .ni_research import tools
from .ni_research.ctx import ResearchCtx

# the verifier classes a refresh can re-run on the outputs alone (units, numbers, freshness, subject and
# stable need the research context or a second fetch, so they are not re-run here)
RECHECKED = frozenset({"place", "window", "threshold", "compare", "empty"})
# attempt rows in ni_runs: the day limit and the one-proposal-per-streak rule read these, so a crash mid-run
# still counts as the attempt
RUN_STARTED = "repair_research_started"
RUN_PROPOSED = "repair_research_proposed"
RUN_DECLINED = "repair_research_declined"
RESEARCH_EVERY = timedelta(hours=24)
# how readily each quality tier asks for a re-research, per reason: "first" = on the first such failure,
# "ladder" = only once the card is failing (the backoff walked; no local repair applies to these statuses)
TRIGGER = {
    "curated": {"constraint_drift": "ladder", "source_dead": "ladder"},
    "verified_endpoint": {"constraint_drift": "first", "source_dead": "ladder"},
    "page_reading": {"constraint_drift": "first", "source_dead": "ladder"},
}
_STATES = {"first": ("degraded", "failing"), "ladder": ("failing",)}


def _rechecked(recipe: dict) -> set[str]:
    """The checks to re-run: those that passed at Keep (the recipe's own verification) or that the recipe
    states as constraints, less any that did not pass then."""
    assert isinstance(recipe, dict), "a recipe"
    checks = (recipe.get("verification") or {}).get("checks") or []
    passed = {c["check"] for c in checks if c.get("ok")}
    failed = {c["check"] for c in checks if not c.get("ok")}
    stated = {c["check"] for c in recipe.get("constraints") or []}
    wanted = ((passed | stated) - failed) & RECHECKED
    assert wanted <= RECHECKED, "only re-runnable checks"
    return wanted


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
    found = tools.checks_for(ctx, outputs, str((spec.get("source") or {}).get("url") or ""), {})
    return [f"{c['check']}: {c['reason']}" if c["reason"] else c["check"]
            for c in found if c["check"] in wanted and not c["ok"]]


def reason_of(item: dict) -> str | None:
    """Why this failing card could ask for a new source: ``constraint_drift`` (its last status), ``source_dead``
    (the fetch keeps failing), else None."""
    assert isinstance(item, dict), "item required"
    status = str(item.get("last_status") or "")
    out = {ni.CONSTRAINT_DRIFT: "constraint_drift", "fetch_failed": "source_dead"}.get(status)
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
    """A ``ni_runs`` timestamp as an aware instant, or None."""
    assert ts is not None, "a timestamp"
    assert RESEARCH_EVERY.total_seconds() > 0, "a positive window"
    try:
        out = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return out if out.tzinfo is not None else out.replace(tzinfo=UTC)


def may_research(runs: list[dict], first_failure: datetime | None, now: datetime) -> bool:
    """False when an attempt started within the last day, or a proposal was already parked in this failure
    streak (a dismissed proposal is not asked for again until the streak resets). ``runs`` is newest first."""
    assert now.tzinfo is not None, "now must be aware"
    assert isinstance(runs, list), "runs required"
    for run in runs:  # bounded: the store keeps 50 per card
        at = _when(run.get("ts"))
        if at is None:
            continue
        if run.get("status") == RUN_STARTED and now - at < RESEARCH_EVERY:
            return False
        if run.get("status") == RUN_PROPOSED and (first_failure is None or at >= first_failure):
            return False
    return True
