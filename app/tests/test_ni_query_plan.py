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
from smartbrain_3000.ni_query.clauses import select_for, serves_time
from smartbrain_3000.ni_query.decl import answer_named
from smartbrain_3000.ni_query.normalize import (
    count_equiv,
    explicit_time,
    order_key,
    said_whole,
    stated_numbers,
    structural_errors,
    time_key,
    v1_where,
    where_key,
)
from smartbrain_3000.ni_query.plan import make_ctx, model_range_stands
from smartbrain_3000.ni_query.prompt import RETRY_SKELETON

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ni_query"
_ZONE = "America/New_York"


def _jl(name: str) -> list[dict]:
    return [json.loads(line) for line in (_FIX / name).read_text().splitlines() if line.strip()]


_GOLD = {g["id"]: g for g in _jl("gold.jsonl")}
_LIB = json.loads((_FIX / "library.json").read_text())
_SPLIT = json.loads((_FIX / "split.json").read_text())
# the few-shot pool ships with the app (the flow's query call reads it): one copy, measured and served
_POOL = [json.loads(line) for line in (pathlib.Path(__file__).parents[1] / "smartbrain_3000" / "ni_query"
                                       / "pool.jsonl").read_text().splitlines() if line.strip()]
_FEW = {r["id"]: r for r in _jl("replies_fewshot.jsonl")}
_THU_9 = dt.datetime(2026, 10, 8, 9, 0, tzinfo=ZoneInfo(_ZONE))


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
    if model_range_stands(exp, gold["time"], spans):  # the lead's Q3b time ruling, on the gold side too
        exp = None
    gw = stated_numbers(v1_where(gold["where"], ga, row["ask"], ctx.consumed_words, ctx.src_words), spans, row["ask"])
    return ((ir["answer"] == gold["answer"] or count_equiv(ir["answer"], gold, src["answers"]))
            and time_key(ir["time"]) == time_key(exp if exp is not None else gold["time"])
            and where_key(ir["where"]) == where_key(gw)
            and order_key(ir["order"], answer_named(src["answers"], ir["answer"]), row["kind"])
            == order_key(gold["order"], ga, row["kind"]))


def test_replayed_replies_end_to_end():
    """The recorded first reply, then the recorded valid object: the module's validator agrees with
    the Q2 harness's on every few-shot TEST row, and the final IRs score 52/60 (55/60 with the floor
    for the three never-valid rows) under the full contract — Round 20, Q3b: the window-aware floor reads
    E13's "next game", and the recognized comparator E37's reply dropped is bound back (was 51 / 53)."""
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
    assert (right, floor_right) == (52, 55), (right, floor_right)


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
    assert off.notes == ["query_rules", "model_off", "comparator_added"] and off.model_meta["attempts"] == 0
    assert off.ir == {"answer": "quakes", "params": {}, "select": [],
                      "where": [{"col": "properties.mag", "op": ">", "value": 2}],
                      "time": {"from": "past_days:7", "to": "today"}, "order": [], "limit": None, "agg": "count"}
    assert off.coverage["answer"] == "rules" and off.coverage["order"] == "rules"
    # Round 20, Q3b: the floor carries the explicit comparator like the explicit time (it read: where [], unbound)
    assert off.coverage["where"] == "recognizer"
    assert off.interpretation == "Earthquakes · Magnitude > 2 · past 7 days · count"

    def down(_messages):
        raise ConnectionError("gateway down")
    plan = _plan(row, down)
    assert plan.notes == ["query_rules", "model_unavailable", "comparator_added"] and plan.ir == off.ir
    assert plan.model_meta["attempts"] == 1


@pytest.mark.parametrize(("time", "grades"), [
    ({"from": "dow:sat", "to": "dow:sat"}, {"daily_forecast": 2, "tonight": 1, "high_today": 0}),
    ({"from": "weekend", "to": "weekend"}, {"snow_forecast": 2, "tonight": 1, "temperature": 0}),
    ({"from": "tonight", "to": "tonight"}, {"tonight": 2, "daily_forecast": 1, "temperature": 0}),
    ({"from": "tomorrow", "to": "tomorrow"}, {"rain_tomorrow": 2, "daily_forecast": 2, "rain_today": 0}),
    ({"from": "now", "to": "now"}, {"temperature": 2, "high_today": 0, "tonight": 0}),
    ({"from": "now", "to": "next_hours:6"}, {"tonight": 2, "daily_forecast": 0}),
    ({"from": "past_days:7", "to": "today"}, {"daily_forecast": 0, "tonight": 0}),   # behind us: no preference
    (None, {"daily_forecast": 0, "temperature": 0}),
])
def test_serves_time_grades_the_declared_window_and_axis(time, grades):
    """Round 20, Q3b — the floor's first key, the removed select_answers window rule: a value declared for
    the stretch or rows whose axis cuts it exactly (2), approximately (1: day rows for tonight, hour rows
    for a weekday), or not (0)."""
    answers = {a["name"]: a for a in _LIB["open-meteo-forecast"]["answers"]}
    assert {name: serves_time(answers[name], time) for name in grades} == grades


@pytest.mark.parametrize(("rid", "answer"), [
    ("C7", "daily_forecast"),      # "on Friday": the day rows, never tonight's hours (was: tonight)
    ("E7", "daily_forecast"),      # "on Sunday"
    ("B9", "snow_forecast"),       # the weekend's day rows; "snow" names it (its own name, not just words)
    ("D12", "next_low"),           # "the next low tide": the answer whose own label says it (was: tides)
    ("E12", "next_high"),
    ("E2", "wind"),                # "right now": a value declared now that the ask names
    ("A17", "results"),            # "last night": a stretch behind us prefers no axis — its own name decides
])
def test_the_rules_floor_is_window_aware_then_prefers_its_own_name(rid, answer):
    """Round 20, Q3b: with the model off, the floor ranks (serves the explicit stretch, name / label words,
    declared words, primary, declared order) — measured on the gold set: floor alone 39 → 46 of 60 few-shot
    TEST rows right, 52 → 60 of 82."""
    plan = _plan(_GOLD[rid], None)
    assert plan.notes == ["query_rules", "model_off"] and plan.ir["answer"] == answer, plan.ir


@pytest.mark.parametrize(("value", "ask", "said"), [
    ("quakes", "how many earthquakes above 4.0 today", False),           # inside "earthquakes" (live A33)
    ("OR", "how many active wildfires are burning in Oregon", False),    # inside "Oregon" (live D36)
    ("delay", "number of flights delayed at JFK right now", False),      # "delayed" is no plural (C38)
    ("ISS", "plot where the space station is over the Pacific", False),  # "is" + s is no "ISS"
    ("Tornado Watch", "any tornado watches in Kansas", True),            # the ask's last word adds -es
    ("Winter Storm Warning", "winter storm warnings in Colorado", True),
    ("Sharks", "result of the latest Sharks game", True),
    ("Chicago O'Hare", "delays at Chicago O'Hare right now", True),
])
def test_a_filter_value_is_a_run_of_whole_ask_words(value, ask, said):
    """Round 20, Q3b — the v1 where rule: a contains / names value must be whole words of the ask (the
    ask's last word may add a plural s / es); part of a word never filters ("mentions quakes" counted 0)."""
    assert said_whole(value, ask) is said
    answer = {"name": "rows", "kind": "list", "path": "items", "row": [{"path": "name", "type": "text"}]}
    kept = v1_where([{"col": "*", "op": "names", "value": value}], answer, ask, frozenset(), frozenset())
    assert bool(kept) is said


def test_no_model_guard_passes_through():
    call, _ = _scripted(["{}"])
    with llm.no_model(), pytest.raises(llm.ModelForbidden):
        _plan(_GOLD["C1"], call)


@pytest.mark.parametrize("rid", ["A33", "E22", "E37", "D22"])
def test_a_dropped_comparator_is_bound_back_by_code(rid):
    """Round 20, Q3b: an explicit comparator is an anchor like an explicit time. The gold reply with its
    comparator stripped binds it back from the recognizer (coverage ``recognizer``, note
    ``comparator_added``); the reply that carries it stays ``bound`` with no note. (This test read: the
    dropped comparator leaves the where empty and coverage ``unbound``.)"""
    row = _GOLD[rid]
    stripped = _plan(row, _scripted([json.dumps({**row["ir"], "where": []})])[0])
    assert stripped.ir["where"] == row["ir"]["where"], stripped.ir["where"]
    assert stripped.coverage["where"] == "recognizer" and "comparator_added" in stripped.notes
    kept = _plan(row, _scripted([json.dumps(row["ir"])])[0])
    assert kept.ir["where"] == row["ir"]["where"] and kept.coverage["where"] == "bound"
    assert "comparator_added" not in kept.notes and "comparator_corrected" not in kept.notes


def test_the_recognized_number_wins_over_the_models():
    """A model comparator on the cell with another number takes the recognized one (note it); a number the ask
    never states is dropped first and the recognized comparator added."""
    row = _GOLD["D22"]   # newest earthquakes above magnitude 3 in Nevada
    src = _LIB[row["source_id"]]
    ask = "the 5 newest earthquakes above magnitude 3 in Nevada"
    reply = json.dumps({**row["ir"], "where": [{"col": "properties.mag", "op": ">", "value": 5}]})
    plan = plan_query(ask, kind=row["kind"], wants=[], source={"id": row["source_id"], "name": src["name"]},
                      answers=src["answers"], params=src["params"], consumed=_consumed(ask), now=_now(row),
                      zone=_ZONE, call_model=_scripted([reply])[0], pool=_POOL)
    assert plan.ir["where"] == [{"col": "properties.mag", "op": ">", "value": 3}]
    assert "comparator_corrected" in plan.notes and plan.coverage["where"] == "recognizer"
    unstated = _plan(row, _scripted([json.dumps({**row["ir"], "where": [
        {"col": "properties.mag", "op": ">", "value": 7}]})])[0])
    assert unstated.ir["where"] == row["ir"]["where"]
    assert {"numbers_unstated", "comparator_added"} <= set(unstated.notes)


def test_a_comparator_binds_by_its_quantity_word_never_by_its_unit():
    """"magnitude 4 in Alaska" is magnitude > 4 — the unit never picks a cell, so it never becomes an inches
    filter; a quantity word naming two number cells, or none, adds nothing (coverage ``unbound``)."""
    from smartbrain_3000.ni_query.clauses import bind_comparators
    quakes = answer_named(_LIB["usgs-quakes-state"]["answers"], "quakes")
    for ask in ("recent quakes stronger than magnitude 4 in Alaska", "earthquakes above 4 in alaska"):
        comparators = recognize(ask, now=_THU_9, zone=_ZONE)["comparators"]
        assert bind_comparators([], quakes, comparators) == (
            [{"col": "properties.mag", "op": ">", "value": 4}], ["comparator_added"]), ask
    snow = {"name": "snow", "kind": "list", "path": "days", "words": ["snow", "snowfall"],
            "row": [{"path": "date", "type": "date", "label": "Day"},
                    {"path": "snowfall", "type": "number", "label": "Snowfall", "unit": "in"}]}
    comparators = recognize("recent quakes stronger than magnitude 4 in Alaska", now=_THU_9, zone=_ZONE)["comparators"]
    assert bind_comparators([], snow, comparators) == ([], []), "no inches filter from '4 in Alaska'"
    wind = {"name": "wind", "kind": "list", "path": "hours", "words": ["wind"],
            "row": [{"path": "wind_speed", "type": "number", "label": "Wind speed"},
                    {"path": "wind_gust", "type": "number", "label": "Wind gust"}]}
    comparators = recognize("hours with wind above 30 mph", now=_THU_9, zone=_ZONE)["comparators"]
    assert comparators and bind_comparators([], wind, comparators) == ([], []), "two cells named: nothing added"
    gusts = recognize("hours with gusts above 30 mph", now=_THU_9, zone=_ZONE)["comparators"]
    assert bind_comparators([], wind, gusts)[0] == [{"col": "wind_gust", "op": ">", "value": 30}]


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


def test_a_weekday_range_reads_monday_to_friday():
    """Round 20, Q3b fix round: a span between two weekdays reads "Monday to Friday" (it read "on Monday to on
    Friday"); a single weekday keeps "on Friday", other ranges keep their words."""
    from smartbrain_3000.ni_query.say import time_words
    assert time_words({"from": "dow:mon", "to": "dow:fri"}) == "Monday to Friday"
    assert time_words({"from": "dow:fri", "to": "dow:fri"}) == "on Friday"
    assert time_words({"from": "today", "to": "next_days:7"}) == "next 7 days"


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
