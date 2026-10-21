"""The sealed ``recipe`` block (plan 6.1, 6.2, 6.4): what a researched card is, beside the spec that runs it.

``recipe`` = ``{question, sources, constraints, tier}`` plus optional ``verification``, ``provenance`` and
``research`` (a size-capped trace of the research loop). Every key is closed and every size is bounded.
The block is system-owned: stripped on export and refused in template packs (``ni_library`` / ``ni_routes``),
so a template can neither claim a quality tier nor carry a research trace. Nothing writes it yet (S4c).

The researcher-draft rules (plan 6.4) live here too: a draft marked researcher-authored may not carry an
``llm`` stage, a ``$secret``, a model or MCP source, or a model-chosen interval.
"""
from __future__ import annotations

import json
import math

QUALITY_TIERS = frozenset({"curated", "verified_endpoint", "page_reading"})
AUTHORS = frozenset({"researcher", "flow", "user"})
QUESTION_KINDS = frozenset({"current_value", "forecast", "next_event", "schedule", "result", "latest_items",
                            "ranking", "trend", "status", "alerts", "count", "lookup", "map", "image",
                            "text_brief", "compare"})
CADENCES = frozenset({"realtime", "hourly", "daily", "weekly"})
CHECKS = frozenset({"place", "window", "threshold", "compare", "units", "numbers", "freshness", "subject",
                    "empty", "stable"})
SOURCE_KINDS = frozenset({"library", "search", "endpoint", "page"})
VERDICTS = frozenset({"pass", "partial", "fail"})
TOOLS = frozenset({"library_search", "resolve_entity", "web_search", "fetch_sample", "try_recipe",
                   "propose_card"})
OPS = frozenset({"=", "!=", "<", "<=", ">", ">=", "in", "contains"})
DIRS = frozenset({"asc", "desc"})
MAX_RECIPE_BYTES = 16 * 1024
MAX_RESEARCH_BYTES = 3 * 1024
MAX_SOURCES = 6
MAX_CONSTRAINTS = 12
MAX_LIST = 8
MAX_TURNS = 12
_QUESTION_KEYS = frozenset({"kind", "subject", "places", "entities", "window", "threshold", "order", "limit",
                            "compare_count", "units", "cadence"})
_RECIPE_KEYS = frozenset({"question", "sources", "constraints", "tier", "verification", "provenance", "research"})
_RESEARCH_KEYS = frozenset({"turns", "budget"})
_BUDGET_KEYS = frozenset({"turns", "fetches", "searches", "seconds"})


def _text(v: object, what: str, n: int, *, empty: bool = False) -> str:
    """A one-line printable string of at most ``n`` characters."""
    assert n > 0, "a positive bound"
    if not isinstance(v, str) or len(v) > n or (not v and not empty) or not v.isprintable():
        raise ValueError(f"{what} must be one printable line of 1..{n} characters")
    return v


def _closed(d: object, keys: frozenset[str], what: str, *, required: frozenset[str] | None = None) -> dict:
    """A dict whose keys are all in ``keys`` and include ``required`` (default: all of ``keys``)."""
    if not isinstance(d, dict):
        raise ValueError(f"{what} must be an object")  # noqa: TRY004
    extra = set(d) - keys
    missing = (keys if required is None else required) - set(d)
    if extra or missing:
        raise ValueError(f"{what} keys are closed to {sorted(keys)} (unknown {sorted(extra)}, missing {sorted(missing)})")
    return d


def _int(v: object, what: str, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{what} must be an integer {lo}..{hi}")
    return v


def _strs(v: object, what: str, n: int, item: int) -> None:
    if not isinstance(v, list) or len(v) > n:
        raise ValueError(f"{what} must be a list of at most {n}")
    for x in v:  # bounded by n
        _text(x, what, item)


def _scalar(v: object, what: str) -> None:
    ok = isinstance(v, (bool, int, float)) and not (isinstance(v, float) and not math.isfinite(v))
    if not (ok or isinstance(v, str)):
        raise ValueError(f"{what} must be a scalar")
    if isinstance(v, str):
        _text(v, what, 200, empty=True)


def validate_question(q: object) -> None:
    """The ask as understood: kind, subject, places, entities, window, threshold, order, limit, compare
    count, units and cadence (the ``ni_query`` / frame fields), every one bounded."""
    d = _closed(q, _QUESTION_KEYS, "recipe.question")
    if d["kind"] not in QUESTION_KINDS:
        raise ValueError(f"recipe.question.kind must be one of {sorted(QUESTION_KINDS)}")
    _text(d["subject"], "recipe.question.subject", 120)
    _strs(d["places"], "recipe.question.places", MAX_LIST, 80)
    _strs(d["entities"], "recipe.question.entities", MAX_LIST, 80)
    w = d["window"]
    if w is not None:
        _closed(w, frozenset({"from", "to"}), "recipe.question.window")
        for t in w.values():
            if t is not None:
                _text(t, "recipe.question.window", 20)
    th = d["threshold"]
    if th is not None:
        _closed(th, frozenset({"op", "value"}), "recipe.question.threshold")
        if th["op"] not in OPS:
            raise ValueError("recipe.question.threshold.op is not a closed op")
        _scalar(th["value"], "recipe.question.threshold.value")
    order = d["order"]
    if not isinstance(order, list) or len(order) > 3:
        raise ValueError("recipe.question.order must be at most 3 {col, dir}")
    for o in order:  # bounded by 3
        _closed(o, frozenset({"col", "dir"}), "recipe.question.order")
        _text(o["col"], "recipe.question.order.col", 200)
        if o["dir"] not in DIRS:
            raise ValueError("recipe.question.order.dir must be asc or desc")
    if d["limit"] is not None:
        _int(d["limit"], "recipe.question.limit", 1, 100)
    _int(d["compare_count"], "recipe.question.compare_count", 0, MAX_LIST)
    if d["units"] is not None:
        _text(d["units"], "recipe.question.units", 20)
    if d["cadence"] is not None and d["cadence"] not in CADENCES:
        raise ValueError(f"recipe.question.cadence must be null or one of {sorted(CADENCES)}")


def _validate_sources(v: object) -> None:
    if not isinstance(v, list) or not v or len(v) > MAX_SOURCES:
        raise ValueError(f"recipe.sources must hold 1..{MAX_SOURCES} entries")
    for s in v:  # bounded by MAX_SOURCES
        _closed(s, frozenset({"kind", "ref", "host", "why"}), "recipe.sources[]")
        if s["kind"] not in SOURCE_KINDS:
            raise ValueError(f"recipe.sources[].kind must be one of {sorted(SOURCE_KINDS)}")
        _text(s["ref"], "recipe.sources[].ref", 300)
        _text(s["host"], "recipe.sources[].host", 253)
        _text(s["why"], "recipe.sources[].why", 200)


def _validate_constraints(v: object) -> None:
    if not isinstance(v, list) or len(v) > MAX_CONSTRAINTS:
        raise ValueError(f"recipe.constraints must hold at most {MAX_CONSTRAINTS} entries")
    for c in v:  # bounded by MAX_CONSTRAINTS
        _closed(c, frozenset({"check", "value"}), "recipe.constraints[]")
        if c["check"] not in CHECKS:
            raise ValueError(f"recipe.constraints[].check must be one of {sorted(CHECKS)}")
        if c["value"] is not None:
            _scalar(c["value"], "recipe.constraints[].value")


def _validate_verification(v: object) -> None:
    d = _closed(v, frozenset({"verdict", "checks", "verified_at", "fetches"}), "recipe.verification")
    if d["verdict"] not in VERDICTS:
        raise ValueError(f"recipe.verification.verdict must be one of {sorted(VERDICTS)}")
    if not isinstance(d["checks"], list) or len(d["checks"]) > MAX_CONSTRAINTS:
        raise ValueError("recipe.verification.checks is bounded")
    for c in d["checks"]:  # bounded by MAX_CONSTRAINTS
        _closed(c, frozenset({"check", "ok", "reason"}), "recipe.verification.checks[]")
        if c["check"] not in CHECKS or not isinstance(c["ok"], bool):
            raise ValueError("recipe.verification.checks[] must be {check: closed, ok: bool, reason}")
        _text(c["reason"], "recipe.verification.checks[].reason", 120, empty=True)
    _text(d["verified_at"], "recipe.verification.verified_at", 40)
    _int(d["fetches"], "recipe.verification.fetches", 0, 8)


def _validate_provenance(v: object) -> None:
    d = _closed(v, frozenset({"author", "built_at"}), "recipe.provenance")
    if d["author"] not in AUTHORS:
        raise ValueError(f"recipe.provenance.author must be one of {sorted(AUTHORS)}")
    _text(d["built_at"], "recipe.provenance.built_at", 40)


def _validate_research(v: object) -> None:
    """The sealed research trace: tool, bounded argument and outcome per turn, plus the budget used."""
    d = _closed(v, _RESEARCH_KEYS, "recipe.research")
    if not isinstance(d["turns"], list) or len(d["turns"]) > MAX_TURNS:
        raise ValueError(f"recipe.research.turns must hold at most {MAX_TURNS} entries")
    for t in d["turns"]:  # bounded by MAX_TURNS
        _closed(t, frozenset({"tool", "arg", "outcome"}), "recipe.research.turns[]")
        if t["tool"] not in TOOLS:
            raise ValueError(f"recipe.research.turns[].tool must be one of {sorted(TOOLS)}")
        _text(t["arg"], "recipe.research.turns[].arg", 200, empty=True)
        _text(t["outcome"], "recipe.research.turns[].outcome", 200, empty=True)
    b = _closed(d["budget"], _BUDGET_KEYS, "recipe.research.budget")
    for k in _BUDGET_KEYS:  # bounded: four keys
        _int(b[k], f"recipe.research.budget.{k}", 0, 100000)
    if len(json.dumps(d, sort_keys=True)) > MAX_RESEARCH_BYTES:
        raise ValueError(f"recipe.research exceeds {MAX_RESEARCH_BYTES} bytes")


def validate_recipe(value: object) -> None:
    """Shape-check ``spec.recipe``: absent, or the closed block above within ``MAX_RECIPE_BYTES``."""
    if value is None:
        return
    d = _closed(value, _RECIPE_KEYS, "spec.recipe", required=frozenset({"question", "sources", "constraints", "tier"}))
    if d["tier"] not in QUALITY_TIERS:
        raise ValueError(f"spec.recipe.tier must be one of {sorted(QUALITY_TIERS)}")
    validate_question(d["question"])
    _validate_sources(d["sources"])
    _validate_constraints(d["constraints"])
    for key, check in (("verification", _validate_verification), ("provenance", _validate_provenance),
                       ("research", _validate_research)):  # bounded: three optional slots
        if d.get(key) is not None:
            check(d[key])
    if len(json.dumps(d, sort_keys=True)) > MAX_RECIPE_BYTES:
        raise ValueError(f"spec.recipe exceeds {MAX_RECIPE_BYTES} bytes")


def _has_secret(node: object, depth: int = 0) -> bool:
    """True when ``node`` holds a ``$secret`` ref or a secret-kind param anywhere (depth-bounded)."""
    if depth > 24:
        return True  # absurd nesting is refused, not walked
    if isinstance(node, dict):
        if "$secret" in node or node.get("kind") == "secret":
            return True
        return any(_has_secret(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_has_secret(v, depth + 1) for v in node)
    return False


def is_researcher_draft(spec: dict) -> bool:
    """True when the spec marks itself researcher-authored (``recipe.provenance.author``)."""
    assert isinstance(spec, dict), "spec must be a dict"
    recipe = spec.get("recipe")
    prov = recipe.get("provenance") if isinstance(recipe, dict) else None
    return isinstance(prov, dict) and prov.get("author") == "researcher"


def check_researcher_draft(spec: dict) -> None:
    """Plan 6.4: a researcher draft may not carry an ``llm`` stage, a ``$secret`` (or secret param), a model
    or MCP source, or a model-chosen interval (the cadence comes from the question, stamped by code)."""
    assert isinstance(spec, dict), "spec must be a dict"
    if any(isinstance(s, dict) and s.get("op") == "llm" for s in (spec.get("pipeline") or [])):
        raise ValueError("researcher draft: llm stages are refused")
    if (spec.get("source") or {}).get("type") in ("mcp_tool", "model"):
        raise ValueError("researcher draft: mcp_tool and model sources are refused")
    if _has_secret(spec.get("source")) or _has_secret(spec.get("params")) or _has_secret(spec.get("pipeline")):
        raise ValueError("researcher draft: $secret and secret params are refused")
    if "interval_minutes" in spec:
        raise ValueError("researcher draft: interval_minutes is code-stamped from the question cadence, not chosen")
