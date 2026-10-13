"""Round 20, Q3b — the engine grammar the query layer's clauses seal into (docs/internal/ni-format.md §36):
``where`` gains ``contains`` / ``in`` / ``or_keys``, ``window`` gains the SPAN form (``to`` / ``back`` and the
backward / calendar tokens), ``sort_by`` gains ``missing: "last"``, and the spec gains ``query`` +
``interpretation``. Every op is run through the engine's own validator and pipeline; the clock is frozen
through ``ni._clock``."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000 import ni as nimod
from smartbrain_3000.ni_query import normalize as q_normalize
from smartbrain_3000.ni_query import plan as q_plan
from smartbrain_3000.ni_query import prompt as q_prompt

NY = ZoneInfo("America/New_York")


def _run(rows: list, *ops: dict) -> list:
    """``rows`` through the given transform ops (validated as the engine validates a spec)."""
    stages = [{"op": "extract", "paths": {"rows": "items"}}, {"op": "transform", "apply": list(ops)}]
    nimod._validate_transform_stage(stages[1], 1, {"rows"})
    return nimod.run_pipeline(stages, {"items": rows})["rows"]


def _bad(*ops: dict) -> None:
    with pytest.raises(ValueError):
        nimod._validate_transform_stage({"op": "transform", "apply": list(ops)}, 1, {"rows"})


# --- where: contains / in / or_keys ---------------------------------------------------------------------

GAMES = [{"home": {"name": "San Jose Sharks"}, "away": {"name": "Boston Bruins"}, "state": "FINAL", "n": 3},
         {"home": {"name": "Dallas Stars"}, "away": {"name": "San Jose Sharks"}, "state": "LIVE", "n": "4"},
         {"home": {"name": "Buffalo Sabres"}, "away": {"name": "Utah Mammoth"}, "state": "FUT", "n": None}]


def test_where_contains_is_a_case_folded_substring_and_or_keys_take_any_cell() -> None:
    home = {"fn": "where", "field": "rows", "key": "home.name", "op": "contains", "value": "sharks"}
    assert [r["n"] for r in _run(GAMES, home)] == [3]
    both = {**home, "or_keys": ["away.name"]}
    assert [r["n"] for r in _run(GAMES, both)] == [3, "4"]          # home OR away: the team's own games
    assert _run(GAMES, {**both, "value": "Canucks"}) == []          # a team in no row: nothing, never the list
    _bad({**home, "value": ""})
    _bad({**home, "value": 4})
    _bad({**home, "op": "eq", "or_keys": ["away.name"]})            # or_keys rides with contains only
    _bad({**home, "or_keys": ["a"] * 8})
    _bad({**home, "or_keys": ["bad key!"]})


def test_where_in_compares_each_value_as_eq() -> None:
    op = {"fn": "where", "field": "rows", "key": "state", "op": "in", "value": ["FINAL", "OFF"]}
    assert [r["state"] for r in _run(GAMES, op)] == ["FINAL"]
    nums = [{"c": 95}, {"c": 61}, {"c": 99.0}, {"c": "95"}]
    assert _run(nums, {"fn": "where", "field": "rows", "key": "c", "op": "in", "value": [95, 96, 99]}) == \
        [{"c": 95}, {"c": 99.0}]                                    # eq rules: "95" (text) is not 95
    _bad({**op, "value": "FINAL"})
    _bad({**op, "value": []})
    _bad({**op, "value": list(range(13))})
    _bad({**op, "value": [["FINAL"]]})


# --- window: the span form ------------------------------------------------------------------------------

QUAKES = [{"t": "2026-10-08T23:50:00-04:00"}, {"t": "2026-10-08T03:00:00-04:00"}, {"t": "2026-10-06T12:00:00-04:00"},
          {"t": "2026-10-03T12:00:00-04:00"}, {"t": "2026-09-30T12:00:00-04:00"}, {"t": "2025-12-31T12:00:00-05:00"}]
DAYS = [{"d": "2026-10-04"}, {"d": "2026-10-05"}, {"d": "2026-10-08"}, {"d": "2026-10-09"}, {"d": "2026-10-12"}]


def _span(monkeypatch, rows: list, key: str, **kw) -> list:
    monkeypatch.setattr(nimod, "_clock", lambda: datetime(2026, 10, 9, 10, 30, tzinfo=NY))   # a Friday
    op = {"fn": "window", "field": "rows", "key": key, **kw}
    return [r[key] for r in _run(rows, op)]


def test_backward_and_calendar_spans_cut_dated_past_rows(monkeypatch) -> None:
    assert _span(monkeypatch, QUAKES, "t", window="past_days:7")[-1] == "2026-10-03T12:00:00-04:00"
    assert len(_span(monkeypatch, QUAKES, "t", window="past_days:7")) == 4      # Oct 2 00:00 → now
    assert _span(monkeypatch, QUAKES, "t", window="yesterday") == [QUAKES[0]["t"], QUAKES[1]["t"]]
    assert _span(monkeypatch, QUAKES, "t", window="past_hours:12") == [QUAKES[0]["t"]]
    assert len(_span(monkeypatch, QUAKES, "t", window="this_week")) == 3           # Mon Oct 5 .. now
    assert len(_span(monkeypatch, QUAKES, "t", window="this_month")) == 4
    assert _span(monkeypatch, QUAKES, "t", window="year:2025") == [QUAKES[5]["t"]]
    assert _span(monkeypatch, QUAKES, "t", window="month:2026-09") == [QUAKES[4]["t"]]
    assert _span(monkeypatch, QUAKES, "t", window="date:2026-10-06") == [QUAKES[2]["t"]]
    # a weekday on dated past rows is the latest one (back): Tuesday Oct 6, never next Tuesday
    assert _span(monkeypatch, QUAKES, "t", window="dow:tue", back=True) == [QUAKES[2]["t"]]
    assert _span(monkeypatch, QUAKES, "t", window="dow:tue") == []
    assert _span(monkeypatch, QUAKES, "t", window="weekend", back=True) == [QUAKES[3]["t"]]   # Sat Oct 3


def test_a_span_with_an_end_token_and_day_rows(monkeypatch) -> None:
    # today .. the end of this ISO week (a schedule's "this week"): Fri Oct 9 .. Sun Oct 11
    assert _span(monkeypatch, DAYS, "d", window="today", to="this_week") == ["2026-10-09"]
    assert _span(monkeypatch, DAYS, "d", window="past_days:7", to="today") == ["2026-10-04", "2026-10-05",
                                                                                   "2026-10-08", "2026-10-09"]
    assert _span(monkeypatch, DAYS, "d", window="now", to="open") == ["2026-10-09", "2026-10-12"]
    assert _span(monkeypatch, DAYS, "d", window="open", to="yesterday") == ["2026-10-04", "2026-10-05",
                                                                             "2026-10-08"]


def test_the_span_form_is_closed() -> None:
    base = {"fn": "window", "field": "rows", "key": "t"}
    for bad in ({"window": "past_days:1000"}, {"window": "next_days:17", "to": "today"}, {"window": "date:2026-02-30"},
                {"window": "month:2026-13"}, {"window": "today", "to": "upcoming"}, {"window": "today", "back": "yes"},
                {"window": "past_days:7", "floor": False}, {"window": "fortnight"}):
        _bad({**base, **bad})
    nimod._validate_transform_stage({"op": "transform", "apply": [{**base, "window": "today"}]}, 1, {"rows"})


# --- sort_by: missing last ------------------------------------------------------------------------------

def test_sort_by_missing_last_reads_dotted_keys_and_numbers_written_as_text() -> None:
    fires = [{"a": {"size": "1200.5"}}, {"a": {"size": None}}, {"a": {"size": 98000}}, {"a": {}}, {"a": {"size": 7}}]
    op = {"fn": "sort_by", "field": "rows", "key": "a.size", "dir": "desc", "missing": "last"}
    assert [r["a"].get("size") for r in _run(fires, op)] == [98000, "1200.5", 7, None, None]
    assert [r["a"].get("size") for r in _run(fires, {**op, "dir": "asc"})] == [7, "1200.5", 98000, None, None]
    _bad({**op, "missing": "first"})
    _bad({"fn": "sort_by", "field": "rows", "dir": "asc", "missing": "last"})
    _bad({"fn": "sort_by", "field": "rows", "key": "a.size", "dir": "asc"})     # a dotted key needs missing last


# --- spec.query / spec.interpretation ---------------------------------------------------------------------

def _spec(**extra) -> dict:
    return {"version": 1, "title": "t", "goal": "g", "params": {},
            "source": {"type": "http_json", "url": "https://example.org/x"},
            "pipeline": [{"op": "extract", "paths": {"text": "v"}}],
            "scene": {"type": "stack", "dir": "v", "gap": "sm", "children": [
                {"type": "text", "value": "{{text}}", "role": "title", "tone": "default", "size": "md"}]},
            "display": {"size": "small"}, "interval_minutes": 15, **extra}


QUERY = {"ir": {"answer": "quakes", "params": {}, "select": [], "where": [{"col": "properties.mag", "op": ">", "value": 4}],
                "time": {"from": "past_days:7", "to": "today"}, "order": [], "limit": None, "agg": "count"},
         "coverage": {k: "bound" for k in q_plan.COVERAGE_KEYS}, "notes": ["time_explicit"],
         "model": {"attempts": 1, "used_fewshot": True}}


def test_the_sealed_query_and_interpretation_are_closed() -> None:
    nimod.validate_spec(_spec(query=QUERY, interpretation="Earthquakes · Magnitude > 4 · past 7 days · count"))
    for bad in ({**QUERY, "extra": 1}, {**QUERY, "notes": ["made_up"]},
                {**QUERY, "coverage": {**QUERY["coverage"], "time": "maybe"}},
                {**QUERY, "model": {"attempts": 3, "used_fewshot": True}},
                {**QUERY, "ir": {**QUERY["ir"], "where": [{"col": "x", "op": "~", "value": 1}]}},
                {**QUERY, "ir": {**QUERY["ir"], "limit": 0}}, {**QUERY, "ir": {**QUERY["ir"], "agg": "sum"}}):
        with pytest.raises(ValueError):
            nimod.validate_spec(_spec(query=bad))
    for bad_line in ("", "two\nlines", "x" * 301, 5):
        with pytest.raises(ValueError):
            nimod.validate_spec(_spec(interpretation=bad_line))


def test_a_value_note_carries_its_value_and_nothing_else_passes() -> None:
    """Round 20 (Q3c): "filter_ungrounded:<value>" / "entity_added:<phrase>" carry a short one-line value; any
    other code stays closed."""
    for notes in (["filter_ungrounded:anywhere in the world"], ["entity_added:Canucks", "order_default"]):
        nimod.validate_spec(_spec(query={**QUERY, "notes": notes}))
        assert all(q_plan.note_ok(n) for n in notes)
    for bad in ("entity_added:", "entity_added", "made_up:x", "filter_ungrounded:" + "x" * 61, "entity_added:a\nb"):
        assert not nimod._query_note_ok(bad) and not q_plan.note_ok(bad), bad
        with pytest.raises(ValueError):
            nimod.validate_spec(_spec(query={**QUERY, "notes": [bad]}))


def test_the_engines_closed_query_sets_mirror_the_query_layer() -> None:
    assert nimod._QUERY_COVERAGE_KEYS == frozenset(q_plan.COVERAGE_KEYS)
    assert nimod._QUERY_COVERAGE_VALUES == q_plan.COVERAGE_VALUES
    assert q_plan.NOTES <= nimod._QUERY_NOTES
    assert q_plan.VALUE_NOTES == nimod._QUERY_VALUE_NOTES
    assert nimod._QUERY_OPS == frozenset(q_prompt.OPS)
    assert nimod._QUERY_IR_KEYS == frozenset(q_prompt.IR_KEYS)
    assert q_normalize.NUM_OPS | q_normalize.TEXT_OPS == nimod._QUERY_OPS
