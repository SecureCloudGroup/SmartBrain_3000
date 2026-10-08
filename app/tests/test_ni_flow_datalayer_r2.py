"""Round 19, data-layer fix round 2 (datalayer-r2): one labeled test per class, over the
functions ni_flow.py / ni_forms/record.py gained or changed. Each test names its class in
its own docstring; the plan's rule freeze applies — these are the labeled-set deltas for
the classes this round measured and shipped.
"""
from __future__ import annotations

from smartbrain_3000 import ni_flow
from smartbrain_3000.ni_forms import record as ni_record
from smartbrain_3000.ni_forms.types import Field


def _answer(**kw) -> dict:
    base = {"name": "a", "label": "A", "words": [], "primary": False, "kind": "value"}
    base.update(kw)
    return base


# ---- class W1: the card never claims a quantity it does not show -------------------------

_TONIGHT = {"kind": "columns", "name": "tonight", "label": "Tonight, hour by hour", "primary": True,
            "words": ["tonight", "overnight", "snow tonight", "rain tonight", "hour by hour"],
            "cells": [{"path": "hourly.time", "key": "time", "label": "Time", "type": "time"},
                      {"path": "hourly.temperature_2m", "key": "temperature", "label": "Temperature", "type": "number"}],
            "axis": {"cell": "time", "step": "hour"}}
_SNOW = {"kind": "columns", "name": "snow_forecast", "label": "Snow forecast", "primary": False,
         "words": ["snow", "snowfall", "will it snow", "how much snow"],
         "cells": [{"path": "daily.time", "key": "day", "label": "Day", "type": "date"},
                   {"path": "daily.snowfall_sum", "key": "snowfall", "label": "Snowfall", "type": "number"}],
         "axis": {"cell": "day", "step": "day"}}


def test_w1_a_tied_list_whose_name_says_the_ask_word_wins(monkeypatch) -> None:
    """W1 (lead, set D #7): "any snow expected in Duluth midweek" scores the one word "snow" on both
    the Tonight hourly answer (its words say "snow tonight") and the Snow forecast; the declared
    order shipped the hourly temperature under the title "snow". Among tied list / columns answers
    the one whose own name or label says an ask word wins; a tie with no such name keeps the order."""
    monkeypatch.setattr(ni_flow, "_v12", lambda answers: False)
    pick = ni_flow.select_answers([_TONIGHT, _SNOW], "any snow expected in Duluth midweek", [])
    assert [a["name"] for a in pick] == ["snow_forecast"]
    pick = ni_flow.select_answers([_TONIGHT, _SNOW], "will it snow this weekend", [])
    assert [a["name"] for a in pick] == ["snow_forecast"]
    pick = ni_flow.select_answers([_TONIGHT, _SNOW], "tonight's forecast hour by hour", [])
    assert [a["name"] for a in pick] == ["tonight"]
    # a word neither name says: the declared order, unchanged
    tied = [dict(_TONIGHT, words=["frost"]), dict(_SNOW, words=["frost"])]
    pick = ni_flow.select_answers(tied, "frost warning", [])
    assert [a["name"] for a in pick] == ["tonight"]


def test_five_columns_are_accepted_and_six_refused() -> None:
    """datalayer-r2: the per-answer cell cap is 5 (Open-Meteo's hourly tonight answer carries time,
    conditions, temperature, rain chance and snowfall); a sixth is still refused. A columns answer
    declares its cells under ``columns`` (a list answer under ``row``)."""
    time_col = {"path": "hourly.time", "label": "Time", "type": "time"}
    col = lambda i: {"path": f"hourly.c{i}", "label": f"C{i}", "type": "number"}
    base = {"kind": "columns", "name": "tonight", "label": "Tonight, hour by hour", "words": ["tonight"], "primary": True,
            "limit": 24, "axis": {"cell": "hourly.time", "step": "hour"}}
    assert ni_flow._clean_answer(dict(base, columns=[time_col, *(col(i) for i in range(4))])) is not None   # 5
    assert ni_flow._clean_answer(dict(base, columns=[time_col, col(0)])) is not None                       # 2
    assert ni_flow._clean_answer(dict(base, columns=[time_col, *(col(i) for i in range(5))])) is None       # 6


def test_w3_a_named_team_absent_from_every_row_is_the_honest_nothing() -> None:
    """class W3: "result" kind now reaches _scope_rows_to_subject; a team named in neither
    home nor away cell over any row raises the flow's nothing-path ValueError."""
    games = _answer(name="games", label="Games", kind="list", path="games",
                    cells=[{"path": "away", "label": "Away", "type": "text"},
                           {"path": "home", "label": "Home", "type": "text"}])
    sample = {"games": [{"away": "Bruins", "home": "Mammoth"}, {"away": "Stars", "home": "Sabres"}]}
    try:
        ni_flow._scope_rows_to_subject(games, sample, "Sharks", None)
    except ValueError as exc:
        assert "no row for Sharks" in str(exc)
    else:
        raise AssertionError("expected the honest nothing path")


def test_w3_a_named_team_present_seals_a_filter_to_its_row() -> None:
    """class W3: the same ask, with the team present, seals a filter instead of refusing."""
    games = _answer(name="games", label="Games", kind="list", path="games",
                    cells=[{"path": "away", "label": "Away", "type": "text"},
                           {"path": "home", "label": "Home", "type": "text"}])
    sample = {"games": [{"away": "Bruins", "home": "Mammoth"}, {"away": "Sharks", "home": "Blues"}]}
    scoped = ni_flow._scope_rows_to_subject(games, sample, "Sharks", None)
    assert scoped["filter"] == {"path": "away", "equals": "Sharks"}


def test_w3_a_team_on_both_sides_over_different_rows_keeps_the_whole_list() -> None:
    """class W3 regression guard: a team appearing as home in one row and away in another
    (a team-scoped source's own recent-results list) is the pre-existing participant case --
    one cell cannot select "its" rows, so the list stays whole, unchanged, no exception."""
    results = _answer(name="results", label="Recent results", kind="list", path="dates",
                      cells=[{"path": "away", "label": "Away", "type": "text"},
                             {"path": "home", "label": "Home", "type": "text"}])
    sample = {"dates": [{"away": "Yankees", "home": "Red Sox"}, {"away": "Orioles", "home": "Yankees"}]}
    scoped = ni_flow._scope_rows_to_subject(results, sample, "Yankees", None)
    assert scoped is results and scoped.get("filter") is None


# ---- class W4: a count is the row count after the window cut, never a stale one-row reading --

def test_w4_count_frame_kind_always_cuts_the_window() -> None:
    """class W4a: _cuts(frame_kind="count") is always True, even over rows that lie entirely
    in the past (the "recent observations" leniency _cuts otherwise applies) -- a stale row
    outside the asked stretch must not silently survive into a tally."""
    assert ni_flow._cuts("next_days:7", [], [], {}, "t", frame_kind="count") is True


def test_w4_count_kind_skips_the_one_row_measure_demotion() -> None:
    """class W4b: _shape_rows's one-row -> "measure" demotion (fix round 1a-6, class H) must
    not fire for a count ask -- one surviving row is still the tally's one entity, not a
    stand-in reading."""
    outputs = {"rows": [{"mag": 2.7, "place": "Louisiana"}]}
    answer = {"cells": [{"path": "mag", "label": "Magnitude", "type": "number", "key": "mag"},
                        {"path": "place", "label": "Place", "type": "text", "key": "place"}],
              "kind": "list"}
    kind, fields, rows, _long = ni_record._shape_rows(answer, outputs, "rows", ask="earthquakes",
                                                      question_kind="count")
    assert kind != "measure"
    assert len(rows) == 1


def test_w4_count_kind_one_row_demotion_still_fires_for_other_kinds() -> None:
    """class W4b regression guard: the SAME one-row answer, asked without a count frame
    (e.g. a lookup), keeps the fix round 1a-6 demotion -- this round narrows the rule, it
    does not remove it."""
    outputs = {"rows": [{"mag": 2.7, "place": "Louisiana"}]}
    answer = {"cells": [{"path": "mag", "label": "Magnitude", "type": "number", "key": "mag"},
                        {"path": "place", "label": "Place", "type": "text", "key": "place"}],
              "kind": "list"}
    kind, fields, rows, _long = ni_record._shape_rows(answer, outputs, "rows", ask="earthquake",
                                                      question_kind="lookup")
    assert kind == "measure"
    assert isinstance(fields[0], Field)


# ---- partial #27: a trend ask prefers its declared history list --------------------------

def test_trend_prefers_a_declared_history_list_over_a_tied_latest_value() -> None:
    """partial #27: "unemployment rate over the last two years" (frame_kind="trend") must
    choose the declared "recent" history list, not "latest" -- even though "latest" ties or
    beats "recent" on raw word score (the phrase "unemployment rate" only scores against
    "latest")."""
    latest = _answer(name="latest", label="Unemployment rate", kind="value",
                     words=["unemployment rate", "latest", "current", "now", "today"])
    recent = _answer(name="recent", label="Recent readings", kind="list", path="rows",
                     words=["recent", "history", "trend", "past", "over time", "chart"],
                     cells=[{"path": "date", "label": "Date", "type": "date"},
                            {"path": "v", "label": "Unemployment rate", "type": "number"}],
                     axis={"cell": "date", "step": "day"})
    chosen = ni_flow.select_answers([latest, recent], "unemployment rate over the last two years",
                                    [], None, "trend")
    assert [a["name"] for a in chosen] == ["recent"]


def test_trend_with_no_declared_history_still_falls_back_to_a_value() -> None:
    """partial #27 regression guard: a trend ask over a source with NO history list (no
    axis-bearing list/columns answer at all) still returns the value answer -- the new rule
    only wins when a real history candidate scores on the ask's own words."""
    latest = _answer(name="latest", label="Unemployment rate", kind="value",
                     words=["unemployment rate", "latest", "current", "now", "today"], primary=True)
    chosen = ni_flow.select_answers([latest], "unemployment rate over the last two years",
                                    [], None, "trend")
    assert [a["name"] for a in chosen] == ["latest"]
