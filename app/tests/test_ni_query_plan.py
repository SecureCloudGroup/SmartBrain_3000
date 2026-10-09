"""Round 20 Q3a: ``plan_query`` end to end with a stubbed model — the recorded Q2 replies replayed
through the module's own call path — plus the rule-only retry, the rules floor, the code-owned
``select`` on the gold set's quantity asks, and the interpretation line (``say``)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000.ni_forms import llm
from smartbrain_3000.ni_query import plan_query, recognize, say
from smartbrain_3000.ni_query.clauses import select_for
from smartbrain_3000.ni_query.decl import answer_named
from smartbrain_3000.ni_query.normalize import (
    count_equiv,
    explicit_time,
    order_key,
    stated_numbers,
    structural_errors,
    time_key,
    v1_where,
    where_key,
)
from smartbrain_3000.ni_query.plan import make_ctx
from smartbrain_3000.ni_query.prompt import RETRY_SKELETON

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ni_query"
_ZONE = "America/New_York"


def _jl(name: str) -> list[dict]:
    return [json.loads(line) for line in (_FIX / name).read_text().splitlines() if line.strip()]


_GOLD = {g["id"]: g for g in _jl("gold.jsonl")}
_LIB = json.loads((_FIX / "library.json").read_text())
_SPLIT = json.loads((_FIX / "split.json").read_text())
_POOL = _jl("pool.jsonl")
_FEW = {r["id"]: r for r in _jl("replies_fewshot.jsonl")}


def _now(row: dict) -> dt.datetime:
    day = dt.date.fromisoformat(_SPLIT["ref_dates"][row["set"]])
    return dt.datetime.combine(day, dt.time(9, 0), tzinfo=ZoneInfo(_ZONE))


def _consumed(ask: str) -> list[str]:
    """The re-score's proxy for parameter-consumed entities: the ask's title-case tokens."""
    return [t for t in ask.split() if t[:1].isupper() and t[1:] == t[1:].lower()]


def _plan(row: dict, call_model, pool: list | None = None):
    src = _LIB[row["source_id"]]
    return plan_query(row["ask"], kind=row["kind"], wants=[], source={"id": row["source_id"], "name": src["name"]},
                      answers=src["answers"], params=src["params"], consumed=_consumed(row["ask"]), now=_now(row),
                      zone=_ZONE, call_model=call_model, pool=_POOL if pool is None else pool)


def _scripted(replies: list[str]):
    """A transport that answers from a script (the last reply repeats) and records every call."""
    calls: list[list] = []

    def call(messages: list) -> str:
        calls.append(messages)
        return replies[min(len(calls), len(replies)) - 1]
    return call, calls


def _full_right(row: dict, ir: dict) -> bool:
    """The full contract's four comparisons (the eval tool's ``full_score``), module functions only."""
    src = _LIB[row["source_id"]]
    spans = recognize(row["ask"], now=_now(row), zone=_ZONE)
    ctx = make_ctx(row["ask"], row["kind"], src["answers"], spans, _consumed(row["ask"]), src["name"])
    gold, ga = row["ir"], answer_named(src["answers"], row["ir"]["answer"])
    exp = explicit_time(spans, row["kind"])
    gw = stated_numbers(v1_where(gold["where"], ga, row["ask"], ctx.consumed_words, ctx.src_words), spans, row["ask"])
    return ((ir["answer"] == gold["answer"] or count_equiv(ir["answer"], gold, src["answers"]))
            and time_key(ir["time"]) == time_key(exp if exp is not None else gold["time"])
            and where_key(ir["where"]) == where_key(gw)
            and order_key(ir["order"], answer_named(src["answers"], ir["answer"]), row["kind"])
            == order_key(gold["order"], ga, row["kind"]))


def test_replayed_replies_end_to_end():
    """The recorded first reply, then the recorded valid object: the module's validator agrees with
    the Q2 harness's on every few-shot TEST row, and the final IRs score 51/60 (53/60 with the floor
    for the three never-valid rows) under the full contract."""
    right = floor_right = 0
    test = [_GOLD[i] for i in _SPLIT["test"]]
    for row in test:
        rec = _FEW[row["id"]]
        second = json.dumps(rec["parsed"]) if rec["parsed"] else rec["reply_raw"]
        call, calls = _scripted([rec["reply_raw"], second])
        plan = _plan(row, call)
        assert plan.model_ir == rec["parsed"], f"{row['id']}: the validators disagree"
        assert plan.model_meta["used_fewshot"] and plan.model_meta["attempts"] == len(calls) <= 2
        assert set(plan.coverage) == {"answer", "time", "where", "order", "select", "limit", "agg"}
        assert not structural_errors(plan.ir, _LIB[row["source_id"]]["answers"]), plan.ir
        if not row["note"]:
            right += plan.model_ir is not None and _full_right(row, plan.ir)
            floor_right += _full_right(row, plan.ir)
    assert (right, floor_right) == (51, 53), (right, floor_right)


def test_retry_states_the_rule_only():
    """A slip on the first try (select naming the answer itself) gets ONE retry whose message names
    no answer and no cell path — the Q2 retry listed companions and the model took one."""
    row = _GOLD["C1"]
    bad = ('{"answer": "temperature", "params": {}, "select": ["temperature"], "where": [], '
           '"time": {"from": "now", "to": "now"}, "order": [], "limit": null, "agg": "none"}')
    good = bad.replace('["temperature"]', "[]")
    call, calls = _scripted([bad, good])
    plan = _plan(row, call)
    assert len(calls) == 2 and plan.model_meta == {"attempts": 2, "valid_first_try": False, "used_fewshot": True}
    retry = calls[1][-1]["content"]
    assert retry == ("Invalid: select on a value answer names only OTHER value answers, else []. "
                     "Reply with ONLY this JSON shape:\n" + RETRY_SKELETON)
    src = _LIB[row["source_id"]]["answers"]
    ids = {a["name"] for a in src} | {c["path"] for a in src for c in (a.get("row") or a.get("columns") or [])}
    tokens = set(retry.replace('"', " ").replace(",", " ").replace(".", " ").split()) | set(retry.split())
    assert not ids & tokens, f"the retry leaks ids: {ids & tokens}"


@pytest.mark.parametrize("reply, rule", [
    ('{"answer": "weather", "params": {}, "select": [], "where": [], "time": null, "order": [], "limit": null, '
     '"agg": "none"}', "answer is one of the declared answer names, copied exactly"),
    ('{"answer": "temperature", "params": {}, "select": [], "where": [{"col": "lat", "op": "names", "value": "Boise"}], '
     '"time": null, "order": [], "limit": null, "agg": "none"}',
     "where col is a cell path of the chosen answer, or * with op names (a value answer has no cells)"),
    ('{"answer": "temperature", "params": {}, "select": [], "where": [], "time": {"from": "Friday", "to": null}, '
     '"order": [], "limit": null, "agg": "none"}', "time from and to are tokens from the TOKENS line, or null"),
])
def test_structural_rules_reach_the_retry(reply, rule):
    call, calls = _scripted([reply])
    plan = _plan(_GOLD["C1"], call)
    assert plan.model_ir is None and plan.notes == ["query_rules", "schema_invalid"]
    assert calls[1][-1]["content"].startswith(f"Invalid: {rule}. Reply with ONLY this JSON shape:")


def test_rules_floor_when_the_model_is_off_or_down():
    row = _GOLD["E37"]   # how many earthquakes above magnitude 2 hit Oklahoma this week
    off = _plan(row, None)
    assert off.notes == ["query_rules", "model_off"] and off.model_meta["attempts"] == 0
    assert off.ir == {"answer": "quakes", "params": {}, "select": [], "where": [],
                      "time": {"from": "past_days:7", "to": "today"}, "order": [], "limit": None, "agg": "count"}
    assert off.coverage["answer"] == "rules" and off.coverage["order"] == "rules"
    assert off.coverage["where"] == "unbound", "the floor's where carries no comparator: flagged, not hidden"
    assert off.interpretation == "Earthquakes · past 7 days · count"

    def down(_messages):
        raise ConnectionError("gateway down")
    plan = _plan(row, down)
    assert plan.notes == ["query_rules", "model_unavailable"] and plan.ir == off.ir
    assert plan.model_meta["attempts"] == 1


def test_no_model_guard_passes_through():
    call, _ = _scripted(["{}"])
    with llm.no_model(), pytest.raises(llm.ModelForbidden):
        _plan(_GOLD["C1"], call)


def test_unstated_comparator_is_flagged_unbound():
    """The model dropped 'above magnitude 2': the final where is empty and coverage says so."""
    row = _GOLD["E37"]
    reply = json.dumps({**row["ir"], "where": []})
    plan = _plan(row, _scripted([reply])[0])
    assert plan.coverage["where"] == "unbound" and plan.coverage["time"] == "bound"
    kept = _plan(row, _scripted([json.dumps(row["ir"])])[0])
    assert kept.coverage["where"] == "bound" and kept.interpretation == "Earthquakes · Magnitude > 2 · past 7 days · count"


def test_unstated_numbers_are_dropped():
    """A numeric filter the ask never states ("IncidentSize > 0") is cut; a stated one stays."""
    row = _GOLD["E26"]   # largest wildfires burning in California by acres
    reply = json.dumps({**row["ir"], "where": [{"col": "attributes.IncidentSize", "op": ">", "value": 0}]})
    plan = _plan(row, _scripted([reply])[0])
    assert plan.ir["where"] == [] and "numbers_unstated" in plan.notes


# the gold set's quantity asks: select from the Library's own lexicon (A5's gold also selects the rain
# chance cell — "snow forecast" names conditions only, by the lexicon)
SELECT_CASES = {
    "A4": ["daily.precipitation_probability_max"], "A5": ["shortForecast"], "A6": [], "A7": ["temperature"],
    "B7": ["daily.temperature_2m_max"], "B9": ["daily.snowfall_sum"], "C7": ["daily.precipitation_probability_max"],
    "C11": ["shortForecast"], "D7": ["daily.snowfall_sum"], "D8": ["temperature"],
    "D10": ["hourly.precipitation_probability", "hourly.weather_code"], "E7": ["daily.precipitation_probability_max"],
    "E8": [], "E10": ["hourly.weather_code"], "E13": ["start"], "B6": [], "A22": [], "D22": []}


@pytest.mark.parametrize("rid", sorted(SELECT_CASES))
def test_lexicon_select(rid):
    row = _GOLD[rid]
    src = _LIB[row["source_id"]]
    spans = recognize(row["ask"], now=_now(row), zone=_ZONE)
    ctx = make_ctx(row["ask"], row["kind"], src["answers"], spans, _consumed(row["ask"]), src["name"])
    answer = answer_named(src["answers"], row["ir"]["answer"])
    got = select_for(row["ask"], row["kind"], answer, src["answers"], spans, ctx.consumed_words)
    assert got == SELECT_CASES[rid], f"{rid} {row['ask']!r}: {got}"


SAY_CASES = {
    "E37": "Earthquakes · Magnitude > 2 · past 7 days · count",
    "A22": "Top coins by market cap · top 10 by Market cap",
    "A4": "Daily forecast (Rain chance) · next 7 days",
    "E39": "Public holidays · Holiday contains Thanksgiving · in 2027 · the first one",
    "A8": "Upcoming launches · from now · the next one",
    "C7": "Daily forecast (Rain chance) · on Friday",
    "A31": "Active weather alerts · Alert contains Tornado Watch · now",
    "A17": "Recent results · yesterday · the latest one",
    "B14": "Next games · this week",
    "D22": "Earthquakes · Magnitude > 3 · newest first",
    "B26": "Recent readings · past 365 days",
    "E13": "Next game (Start)",
}


@pytest.mark.parametrize("rid", sorted(SAY_CASES))
def test_say_golden(rid):
    row = _GOLD[rid]
    assert say(row["ir"], _LIB[row["source_id"]]["answers"]) == SAY_CASES[rid]


def test_every_gold_ir_and_recorded_reply_is_structurally_valid():
    for row in _GOLD.values():
        answers = _LIB[row["source_id"]]["answers"]
        assert structural_errors(row["ir"], answers) == [], row["id"]
        parsed = _FEW.get(row["id"], {}).get("parsed")
        assert parsed is None or structural_errors(parsed, answers) == [], row["id"]


def test_chat_json_check_hook():
    """``llm.chat_json(check=...)``: a rule the schema subset cannot state fires the one retry with
    its own message; a second slip raises SchemaInvalid; without ``check`` nothing changes."""
    def check(obj: dict) -> list[str]:
        return [] if obj.get("a") == 2 else ["a must be 2"]

    call, calls = _scripted(['{"a": 1}', '{"a": 2}'])
    obj, meta = llm.chat_json("query", [], {"type": "object"}, call=call, skeleton="{}", check=check)
    assert obj == {"a": 2} and meta.retries == 1
    assert calls[1][-1]["content"] == "Invalid: a must be 2. Reply with ONLY this JSON shape:\n{}"
    with pytest.raises(llm.SchemaInvalid):
        llm.chat_json("query", [], {"type": "object"}, call=_scripted(['{"a": 1}'])[0], check=check)
    assert llm.chat_json("query", [], {"type": "object"}, call=_scripted(['{"a": 1}'])[0])[0] == {"a": 1}


def _v1_right(row: dict, reply: dict | None) -> bool:
    """rescore_v1.py's four comparisons on a raw reply (the eval tool's ``v1_score``, module only)."""
    if not isinstance(reply, dict):
        return False
    src = _LIB[row["source_id"]]
    spans = recognize(row["ask"], now=_now(row), zone=_ZONE)
    ctx = make_ctx(row["ask"], row["kind"], src["answers"], spans, _consumed(row["ask"]), src["name"])
    gold, ga = row["ir"], answer_named(src["answers"], row["ir"]["answer"])
    exp = explicit_time(spans, row["kind"])
    ww = [v1_where(w, ga, row["ask"], ctx.consumed_words, ctx.src_words) for w in (reply["where"], gold["where"])]
    return ((reply["answer"] == gold["answer"] or count_equiv(reply["answer"], gold, src["answers"]))
            and time_key(exp if exp is not None else reply["time"]) == time_key(exp if exp is not None else gold["time"])
            and where_key(ww[0]) == where_key(ww[1])
            and order_key(reply["order"], ga, row["kind"]) == order_key(gold["order"], ga, row["kind"]))


def test_v1_contract_reproduces_the_rescore():
    """The lead's re-score of the recorded replies, through the module's normalization: 50/60
    few-shot TEST and 54/82 zero-shot ALL (clean rows) — independent of the tools/ tree."""
    zero = {r["id"]: r for r in _jl("replies_zero.jsonl")}
    few_rows = [_GOLD[i] for i in _SPLIT["test"] if not _GOLD[i]["note"]]
    zero_rows = [g for g in _GOLD.values() if not g["note"]]
    few_right = sum(_v1_right(g, _FEW[g["id"]]["parsed"]) for g in few_rows)
    zero_right = sum(_v1_right(g, zero[g["id"]]["parsed"]) for g in zero_rows)
    assert (few_right, len(few_rows), zero_right, len(zero_rows)) == (50, 60, 54, 82)
