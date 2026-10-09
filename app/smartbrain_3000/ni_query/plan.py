"""PLAN (Round 20, Q3a): an information ask → a closed Query IR over ONE declared Library answer
(``plan_query``), with a coverage record per clause, a code-rendered interpretation line and
closed notes. Not wired into the flow in this step: Q3b replaces ``ni_flow.select_answers``, the
window regex table and the subject row filter with it, one per measured delta.

Ownership (plan §E):
- model-owned — one local call (few-shot k = 3 from ``pool``, the literal skeleton): answer,
  time, where, order; then checked (``normalize.structural_errors``) and cut down (the v1
  rules + the recognizer validator, ``normalize``);
- code-owned (``clauses``): select, limit, agg; params = {} in this step;
- recognizers (``recognize``): an explicit time phrase decides time; where numbers are stated.

Flow: recognize → ``prompt.build_messages`` → ``llm.chat_json`` (purpose "query"; the IR schema
in the validator's subset; ``check`` = the structural rules, so the one retry also fires on a
structural slip and states the rule only) → normalize → code-owned clauses → ``say``. Model
off (``call_model`` None), unavailable, or invalid after the retry → the rules floor:
``clauses.floor_answer``, time = the explicit phrase or null, where [], notes "query_rules".

``call_model(messages) -> str`` is ``chat_json``'s transport (system + user, as measured); the
flow's prompt-string seam needs an adapter in Q3b. ``consumed`` holds the parameter-consumed
entities: strings (where values naming them are dropped) and (start, end) spans of the ask
(dropped from where AND masked for the recognizers).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from ..ni_forms import llm
from .clauses import floor_answer, limit_for, select_for
from .decl import answer_named, counted_list
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
COVERAGE_VALUES = frozenset({"bound", "unbound", "approximated", "rules"})
NOTES = frozenset({"query_rules", "model_off", "model_unavailable", "schema_invalid", "time_explicit",
                   "where_dropped", "numbers_unstated", "order_dropped", "params_dropped", "count_list"})
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
             source_name: str) -> Ctx:
    assert isinstance(answers, list) and answers, "answers required"
    assert isinstance(source_name, str), "source_name must be a str"
    return Ctx(ask=ask, kind=kind, answers=answers, spans=spans, consumed_words=consumed_words(ask, consumed),
               src_words=frozenset(source_name.casefold().split()))


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
    """select / limit / agg from code; a filtered count reads the list the count value counts."""
    assert isinstance(ir, dict) and isinstance(notes, list), "ir + notes required"
    assert isinstance(ctx, Ctx), "ctx must be a Ctx"
    answer = answer_named(ctx.answers, ir["answer"])
    lst = counted_list(ctx.answers, answer) if ctx.kind == "count" and narrows(ir) else None
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
    ir["agg"] = "count" if ctx.kind == "count" else "none"
    return ir


def finalize(model_ir: dict, ctx: Ctx) -> tuple[dict, list[str]]:
    """A structurally valid model IR → (the final IR, notes)."""
    assert isinstance(model_ir, dict) and not structural_errors(model_ir, ctx.answers), "a valid model IR"
    assert isinstance(ctx, Ctx), "ctx must be a Ctx"
    answer = answer_named(ctx.answers, model_ir["answer"])
    notes: list[str] = []
    explicit = explicit_time(ctx.spans, ctx.kind)
    if explicit is not None and time_key(explicit) != time_key(model_ir["time"]):
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


def floor_plan(ctx: Ctx, wants: list) -> dict:
    """The rules floor's IR."""
    assert isinstance(ctx, Ctx), "ctx must be a Ctx"
    assert isinstance(wants, list), "wants must be a list"
    answer = floor_answer(ctx.answers, ctx.kind, ctx.ask, wants)
    ir = {"answer": answer["name"], "params": {}, "select": [], "where": [],
          "time": explicit_time(ctx.spans, ctx.kind), "order": [], "limit": None, "agg": "none"}
    return _code_clauses(ir, ctx, [])


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
               pool: list[dict]) -> QueryPlan:
    """The ask → QueryPlan (module docstring)."""
    assert isinstance(ask, str) and ask.strip(), "ask required"
    assert isinstance(answers, list) and answers and isinstance(source, dict), "answers + source required"
    masked = [c for c in consumed[:_MAX_CONSUMED] if isinstance(c, tuple)]
    spans = recognize(ask, now=now, zone=zone, masked=masked)
    ctx = make_ctx(ask, kind, answers, spans, list(consumed[:_MAX_CONSUMED]), str(source.get("name") or ""))
    examples = retrieve_examples(ask, kind or "", pool) if pool else []
    messages = build_messages(ask, source=source, answers=answers, params=params, now=now, zone=zone,
                              examples=examples)
    model_ir, meta = _ask_model(messages, answers, call_model)
    if model_ir is None:
        ir, notes = floor_plan(ctx, list(wants or [])), ["query_rules", meta["why"]]
    else:
        ir, notes = finalize(model_ir, ctx)
    assert set(notes) <= NOTES, "closed notes"
    return QueryPlan(ir=ir, model_ir=model_ir, coverage=coverage_of(ir, ctx, model_ir is None),
                     interpretation=say(ir, answers), notes=notes,
                     model_meta={"attempts": meta["attempts"], "valid_first_try": meta["valid_first_try"],
                                 "used_fewshot": bool(examples)})
