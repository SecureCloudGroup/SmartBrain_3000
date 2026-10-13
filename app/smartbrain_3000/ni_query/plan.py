"""PLAN (Round 20, Q3a): an information ask → a closed Query IR over ONE declared Library answer
(``plan_query``), with a coverage record per clause, a code-rendered interpretation line and
closed notes. Not wired into the flow in this step: Q3b replaces ``ni_flow.select_answers``, the
window regex table and the subject row filter with it, one per measured delta.

Ownership (plan §E):
- model-owned — one local call (few-shot k = 3 from ``pool``, the literal skeleton): answer,
  time, where, order; then checked (``normalize.structural_errors``) and cut down (the v1
  rules + the recognizer validator, ``normalize``);
- code-owned (``clauses``): select, limit, agg; params = {} in this step;
- recognizers (``recognize``): an explicit time phrase decides time — except a counted range the model
  read against the recognizers' calendar reading of a relative phrase ("over the last year": the model's
  past_days:365 stands over the year 2025, coverage ``approximated``; the lead's Q3b ruling,
  ``model_range_stands``); where numbers are stated; an explicit comparator whose quantity word names
  one number cell is carried like a time anchor (``clauses.bind_comparators``: added when the model
  dropped it, its number winning over another the model wrote; coverage ``where: recognizer``).
- evidence (Round 20, Q3c — ``ground``): a ``contains`` / ``names`` value stands only on positive evidence
  (a proper-noun phrase, a holiday, a declared label, a value of a Library code table the filtered text cell
  declares — Q3d, note ``filter_grounded:codes`` —, a taxonomy keyword of the asked subcategories, the
  sample's rows: ``grounds``), else it is dropped (note ``filter_ungrounded:<value>``, coverage ``where:
  approximated``); on an address no resolver filled, every proper-noun phrase of the ask is carried by a
  ``names`` filter (note ``entity_added:<phrase>``, coverage ``where: recognizer``); a ranking the model left
  unordered is ordered by the ranked number cell (``clauses.rank_order``: note ``order_default``, coverage
  ``order: rules``).

Flow: recognize → ``prompt.build_messages`` → ``llm.chat_json`` (purpose "query"; the IR schema
in the validator's subset; ``check`` = the structural rules, so the one retry also fires on a
structural slip and states the rule only) → normalize → code-owned clauses → ``say``. Model
off (``call_model`` None), unavailable, or invalid after the retry → the rules floor:
``clauses.floor_answer`` (window-aware: the answer that serves the explicit stretch first, then name /
label words over declared ``words``, then the primary), time = the explicit phrase or null, where [],
notes "query_rules".

``call_model(messages) -> str`` is ``chat_json``'s transport (system + user, as measured); the
flow's prompt-string seam needs an adapter in Q3b. ``consumed`` holds the parameter-consumed
entities: strings (where values naming them are dropped) and (start, end) spans of the ask
(dropped from where AND masked for the recognizers); ``masked`` holds spans masked for the
recognizers only (Q3b: the intent's proper names — "Saturday Night Live" is no Saturday).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from ..ni_forms import llm
from .clauses import (
    bind_comparators,
    floor_answer,
    limit_for,
    rank_order,
    select_for,
    step_answer,
)
from .decl import answer_named, counted_list
from .ground import Grounds, carry_entities, ground_where, proper_phrases
from .normalize import (
    explicit_time,
    narrows,
    stated_numbers,
    structural_errors,
    time_key,
    v1_order,
    v1_where,
)
from .prompt import IR_SCHEMA, RETRY_SKELETON, build_messages, retrieve_examples
from .recognize import recognize, words
from .say import say

COVERAGE_KEYS = ("answer", "time", "where", "order", "select", "limit", "agg")
COVERAGE_VALUES = frozenset({"bound", "unbound", "approximated", "rules", "recognizer"})
NOTES = frozenset({"query_rules", "model_off", "model_unavailable", "schema_invalid", "time_explicit",
                   "where_dropped", "numbers_unstated", "order_dropped", "params_dropped", "count_list",
                   "time_range_kept", "comparator_added", "comparator_corrected", "order_default",
                   "filter_grounded:codes",    # Q3d: a Library code table grounded a filter value (one code)
                   "step_preferred"})         # G3: an explicit step word chose the same-source list answer
# Round 20 (Q3c): note codes that carry their value — "filter_ungrounded:anywhere in the world",
# "entity_added:Canucks" (``note_ok``)
VALUE_NOTES = frozenset({"filter_ungrounded", "entity_added"})
_MAX_NOTE_VALUE = 60
_COUNTED_HEADS = ("past_days:", "next_days:", "past_hours:", "next_hours:")
_CALENDAR_TOKENS = ("this_week", "next_week", "this_month")
_DAY_TOKENS = ("today", "tomorrow", "yesterday", "dow:", "date:")
_MAX_CONSUMED = 32


@dataclass
class QueryPlan:
    """``plan_query``'s closed result."""

    ir: dict                      # the final Query IR (ir.schema.json)
    model_ir: dict | None         # the model's raw valid reply; None on the rules floor
    coverage: dict                # COVERAGE_KEYS → bound | unbound | approximated | rules
    interpretation: str           # say(ir): labels and plain words, never a path
    notes: list = field(default_factory=list)       # closed codes (NOTES)
    model_meta: dict = field(default_factory=dict)  # {attempts, valid_first_try, used_fewshot}


@dataclass(frozen=True)
class Ctx:
    """Everything the normalization reads besides the IR."""

    ask: str
    kind: str | None
    answers: list
    spans: dict
    consumed_words: frozenset
    src_words: frozenset
    phrases: tuple = ()           # the ask's proper-noun phrases the address did not take (B: carried entities)
    names: tuple = ()             # the ask's proper-noun phrases, consumed words kept (A: a value's evidence)
    resolved: bool = False        # the address takes a parameter from a resolver (no carried entities)
    grounds: Grounds = field(default_factory=Grounds)  # the caller's evidence (taxonomy keywords, the sample)


def note_ok(note: object) -> bool:
    """A closed note code, or a value-carrying one ("entity_added:Canucks") with a short one-line value."""
    assert note is None or isinstance(note, (str, int, float, list, dict, bool)), "a JSON value"
    head, sep, value = str(note).partition(":")
    ok = isinstance(note, str) and (note in NOTES or (
        bool(sep) and head in VALUE_NOTES and 0 < len(value) <= _MAX_NOTE_VALUE and value.isprintable()))
    assert isinstance(ok, bool), "a verdict"
    return ok


def consumed_words(ask: str, consumed: list) -> frozenset[str]:
    """The casefolded words of the consumed strings and spans (whitespace tokens and their
    letter/digit runs)."""
    assert isinstance(ask, str) and isinstance(consumed, list), "ask + consumed required"
    assert len(consumed) <= _MAX_CONSUMED, "consumed bounded"
    texts = [ask[c[0]:c[1]] if isinstance(c, tuple) else str(c) for c in consumed]
    out = {t.casefold() for text in texts for t in text.split()}
    out |= {w for text in texts for w, _, _ in words(text.casefold())}
    return frozenset(out)


def make_ctx(ask: str, kind: str | None, answers: list[dict], spans: dict, consumed: list,
             source_name: str, grounds: Grounds | None = None, params: list | None = None) -> Ctx:
    """``params``: the source's parameter menu — one filled by a resolver scopes the rows to what it resolved."""
    assert isinstance(answers, list) and answers, "answers required"
    assert isinstance(source_name, str), "source_name must be a str"
    taken = consumed_words(ask, consumed)
    own = frozenset(w for w, _, _ in words(source_name.casefold()))
    resolved = any(isinstance(p, dict) and (p.get("fill") or {}).get("from") == "resolver" for p in (params or [])[:40])
    return Ctx(ask=ask, kind=kind, answers=answers, spans=spans, consumed_words=taken,
               src_words=frozenset(source_name.casefold().split()),
               phrases=tuple(proper_phrases(ask, spans, taken, own)),
               names=tuple(proper_phrases(ask, spans, frozenset(), own)), resolved=resolved,
               grounds=grounds or Grounds())


def _ask_model(messages: list[dict], answers: list[dict],
               call_model: Callable[[list], str] | None) -> tuple[dict | None, dict]:
    """(the valid reply or None, {attempts, valid_first_try, why})."""
    assert isinstance(messages, list) and messages, "messages required"
    assert call_model is None or callable(call_model), "call_model must be callable or None"
    if call_model is None:
        return None, {"attempts": 0, "valid_first_try": False, "why": "model_off"}
    calls: list[int] = []

    def counted(msgs: list) -> str:
        assert isinstance(msgs, list) and msgs, "messages required"
        assert len(calls) < 2, "chat_json calls at most twice (one retry)"
        calls.append(1)
        return call_model(msgs)

    try:
        obj, meta = llm.chat_json("query", messages, IR_SCHEMA, call=counted, max_tokens=600,
                                  skeleton=RETRY_SKELETON, check=lambda o: structural_errors(o, answers))
    except llm.ModelForbidden:
        raise
    except llm.ModelUnavailable as ex:
        why = "schema_invalid" if isinstance(ex, llm.SchemaInvalid) else "model_unavailable"
        return None, {"attempts": len(calls), "valid_first_try": False, "why": why}
    return obj, {"attempts": len(calls), "valid_first_try": meta.retries == 0, "why": None}


def _code_clauses(ir: dict, ctx: Ctx, notes: list[str]) -> dict:
    """Explicit comparators (``clauses.bind_comparators`` — on the list a count value counts), the evidence for
    filter values and the carried entities (``ground``), then select / limit / agg from code and a ranking's
    default order (``clauses.rank_order``); a filtered count reads the list the count value counts."""
    assert isinstance(ir, dict) and isinstance(notes, list), "ir + notes required"
    assert isinstance(ctx, Ctx), "ctx must be a Ctx"
    answer = answer_named(ctx.answers, ir["answer"])
    stepped = step_answer(ctx.ask, answer, ctx.answers)
    if stepped is not answer:
        ir["answer"], answer = stepped["name"], stepped
        notes.append("step_preferred")
    counted = counted_list(ctx.answers, answer) if ctx.kind == "count" else None
    rows = counted or answer
    ir["where"], recognized = bind_comparators(ir["where"], rows, list(ctx.spans["comparators"]))
    notes += list(dict.fromkeys(recognized))
    ir["where"], ungrounded, coded = ground_where(ir["where"], rows, phrases=list(ctx.names), spans=ctx.spans,
                                                  grounds=ctx.grounds)
    ir["where"], added = carry_entities(ir["where"], rows, [] if ctx.resolved else list(ctx.phrases))
    notes += [f"filter_ungrounded:{v}" for v in ungrounded] + [f"entity_added:{p[:_MAX_NOTE_VALUE]}" for p in added]
    notes += ["filter_grounded:codes"] if coded else []
    lst = counted if counted is not None and narrows(ir) else None
    if lst is not None:
        ir["answer"], answer = lst["name"], lst
        notes.append("count_list")
    ir["select"] = select_for(ctx.ask, ctx.kind, answer, ctx.answers, ctx.spans, ctx.consumed_words)
    limit, axis_order, ahead = limit_for(ctx.ask, ctx.kind, answer, ctx.spans)
    if limit is not None:
        ir["limit"] = limit
        ir["order"] = axis_order + [o for o in ir["order"] if o not in axis_order]
    if ahead and ir["time"] is None:
        ir["time"] = {"from": "now", "to": None}
    if not ir["order"]:
        ir["order"] = rank_order(ctx.ask, ctx.kind, answer, ctx.answers, ctx.spans, ctx.consumed_words)
        notes += ["order_default"] if ir["order"] else []
    ir["agg"] = "count" if ctx.kind == "count" else "none"
    return ir


def model_range_stands(explicit: dict | None, model_time: dict | None, spans: dict) -> bool:
    """The lead's time ruling (Round 20, Q3b): a COUNTED range the model read ("over the last year" →
    past_days:365) stands against the recognizers' CALENDAR reading of a relative phrase (the year 2025,
    September) — the line then shows the model's reading and coverage says ``approximated``. An explicit
    anchor stays the recognizers' (a phrase with a written year: "in 2027", "Thanksgiving 2027")."""
    assert isinstance(spans, dict) and "time" in spans, "recognized spans required"
    assert explicit is None or isinstance(explicit, dict), "explicit must be a dict or None"
    if not explicit or not isinstance(model_time, dict):
        return False
    counted = any(str(t or "").startswith(_COUNTED_HEADS) for t in (model_time.get("from"), model_time.get("to")))
    calendar = all(str(t or "") in _CALENDAR_TOKENS or str(t or "").startswith(("month:", "year:"))
                   for t in (explicit.get("from"), explicit.get("to")))
    written_year = any(w.isdigit() and len(w) == 4 for s in spans["time"] for w, _, _ in words(str(s["text"])))
    return counted and calendar and not written_year


def finalize(model_ir: dict, ctx: Ctx) -> tuple[dict, list[str]]:
    """A structurally valid model IR → (the final IR, notes)."""
    assert isinstance(model_ir, dict) and not structural_errors(model_ir, ctx.answers), "a valid model IR"
    assert isinstance(ctx, Ctx), "ctx must be a Ctx"
    answer = answer_named(ctx.answers, model_ir["answer"])
    notes: list[str] = []
    explicit = explicit_time(ctx.spans, ctx.kind)
    if model_range_stands(explicit, model_ir["time"], ctx.spans):
        explicit = None
        notes.append("time_range_kept")
    elif explicit is not None and time_key(explicit) != time_key(model_ir["time"]):
        notes.append("time_explicit")
    where = v1_where(model_ir["where"], answer, ctx.ask, ctx.consumed_words, ctx.src_words)
    stated = stated_numbers(where, ctx.spans, ctx.ask)
    order = v1_order(model_ir["order"], answer, ctx.kind)
    for code, before, after in (("where_dropped", model_ir["where"], where), ("numbers_unstated", where, stated),
                                ("order_dropped", model_ir["order"], order)):
        if len(after) < len(before):
            notes.append(code)
    if model_ir["params"]:
        notes.append("params_dropped")
    ir = {"answer": answer["name"], "params": {}, "select": [], "where": stated,
          "time": explicit if explicit is not None else model_ir["time"], "order": order, "limit": None,
          "agg": "none"}
    return _code_clauses(ir, ctx, notes), notes


def floor_plan(ctx: Ctx, wants: list, notes: list | None = None) -> dict:
    """The rules floor's IR (``notes`` gains the code clauses' notes — a recognized comparator)."""
    assert isinstance(ctx, Ctx), "ctx must be a Ctx"
    assert isinstance(wants, list), "wants must be a list"
    time = explicit_time(ctx.spans, ctx.kind)
    answer = floor_answer(ctx.answers, ctx.kind, ctx.ask, wants, spans=ctx.spans,
                          consumed_words=ctx.consumed_words, time=time)
    ir = {"answer": answer["name"], "params": {}, "select": [], "where": [],
          "time": time, "order": [], "limit": None, "agg": "none"}
    return _code_clauses(ir, ctx, notes if notes is not None else [])


def coverage_of(ir: dict, ctx: Ctx, floor: bool) -> dict:
    """Per clause: ``rules`` = the floor decided it; ``unbound`` = the ask states something the
    clause does not carry (a tokenless time phrase, a comparator the final where lacks) — it wins
    over ``rules``; ``approximated`` = carried coarser (a part of a day read as the day); else
    ``bound``."""
    assert isinstance(ir, dict) and isinstance(ctx, Ctx), "ir + ctx required"
    assert isinstance(floor, bool), "floor must be a bool"
    cov = {k: ("rules" if floor and k in ("answer", "time", "where", "order") else "bound") for k in COVERAGE_KEYS}
    told = [s for s in ctx.spans["time"] if s["kind"] != "duration"]
    if any(not s["token"] for s in told):
        cov["time"] = "unbound"          # a time phrase no token carries ("at noon", "since Monday")
    elif any(s["grain"] == "hour" and str(s["token"]).startswith(_DAY_TOKENS) for s in told):
        cov["time"] = "approximated"     # a part of a day read as the day ("last night", "tomorrow morning")
    carried = {float(v) for w in ir["where"] for v in (w["value"] if isinstance(w["value"], list) else [w["value"]])
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if any(float(c["value"]) not in carried for c in ctx.spans["comparators"]):
        cov["where"] = "unbound"         # "above magnitude 3" the final where does not carry
    assert set(cov.values()) <= COVERAGE_VALUES, "closed coverage values"
    return cov


def plan_query(ask: str, *, kind: str | None, wants: list, source: dict, answers: list[dict], params: list[dict],
               consumed: list, now: dt.datetime, zone: str, call_model: Callable[[list], str] | None,
               pool: list[dict], masked: list | tuple = (), grounds: Grounds | None = None) -> QueryPlan:
    """The ask → QueryPlan (module docstring). ``masked``: char ranges masked for the recognizers only
    (the intent's proper names, ``recognize.name_masks``) — unlike ``consumed`` they never drop a where.
    ``grounds``: the evidence the caller holds for filter values (``ground.Grounds``: the taxonomy keywords of
    the asked subcategories, the fetched sample); None = the ask and the answer alone."""
    assert isinstance(ask, str) and ask.strip(), "ask required"
    assert isinstance(answers, list) and answers and isinstance(source, dict), "answers + source required"
    spans = recognize(ask, now=now, zone=zone, masked=[*[c for c in consumed[:_MAX_CONSUMED] if isinstance(c, tuple)],
                                                       *list(masked)[:_MAX_CONSUMED]])
    ctx = make_ctx(ask, kind, answers, spans, list(consumed[:_MAX_CONSUMED]), str(source.get("name") or ""), grounds,
                   params)
    examples = retrieve_examples(ask, kind or "", pool) if pool else []
    messages = build_messages(ask, source=source, answers=answers, params=params, now=now, zone=zone,
                              examples=examples)
    model_ir, meta = _ask_model(messages, answers, call_model)
    if model_ir is None:
        notes = ["query_rules", meta["why"]]
        ir = floor_plan(ctx, list(wants or []), notes)
    else:
        ir, notes = finalize(model_ir, ctx)
    assert all(note_ok(n) for n in notes), "closed notes"
    coverage = coverage_of(ir, ctx, model_ir is None)
    if "time_range_kept" in notes and coverage["time"] in ("bound", "rules"):
        coverage["time"] = "approximated"  # the lead's ruling: the model's range, visibly not the calendar one
    carried = {"comparator_added", "comparator_corrected"} & set(notes) or any(n.startswith("entity_added:")
                                                                                for n in notes)
    if carried and coverage["where"] in ("bound", "rules"):
        coverage["where"] = "recognizer"   # an explicit comparator / a named entity of the ask, carried by code
    if any(n.startswith("filter_ungrounded:") for n in notes) and coverage["where"] != "unbound":
        coverage["where"] = "approximated"  # a filter the ask asked for in words no evidence grounds: dropped
    if "order_default" in notes and coverage["order"] == "bound":
        coverage["order"] = "rules"        # a ranking the model left unordered: the ranked cell, by code
    return QueryPlan(ir=ir, model_ir=model_ir, coverage=coverage,
                     interpretation=say(ir, answers), notes=notes,
                     model_meta={"attempts": meta["attempts"], "valid_first_try": meta["valid_first_try"],
                                 "used_fewshot": bool(examples)})
