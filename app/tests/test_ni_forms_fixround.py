"""Fix round 1a-5 (2026-10-07, after the first live read of the forms engine): one labeled case
set per class — A frame-aware design (kind + wants into the engine, the B2 prior, next_event
eligibility, the asked quantity never dropped), B series_line (>= 3 points, forecast hero, order),
C title_echo on labels only / rows that all fit / the universal fallback, D the lookup row filter,
E map_lite counts, F diagnostics on the bound node + the stat word card, G statistic precision.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smartbrain_3000 import ni, ni_flow
from smartbrain_3000.ni_forms import asked, fmt, prov
from smartbrain_3000.ni_forms import profile as pf
from smartbrain_3000.ni_forms.enumerate import enumerate as enumerate_cands
from smartbrain_3000.ni_forms.enumerate import frame_forms, next_event_eligible
from smartbrain_3000.ni_forms.form_scene import design, form_scene
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.lint import lint
from smartbrain_3000.ni_forms.present import _gates, build_messages
from smartbrain_3000.ni_forms.rec import R as RView
from smartbrain_3000.ni_forms.record import from_answers
from smartbrain_3000.ni_forms.registry import FORMS
from smartbrain_3000.ni_forms.spans import Span, bucket
from smartbrain_3000.ni_forms.types import Candidate, CardInput, Profile, TimeProfile

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
URL = "https://api.example.org/x"
CTX = {"source_url": URL, "fetched_at": "2026-10-07T12:00:00Z", "viewer_tz": "UTC"}
SPEC = {"title": "t", "goal": "g", "interval_minutes": 15, "source": {"type": "http_json", "url": URL}}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hourly_answers(n: int = 18, first: datetime | None = None) -> tuple[list, dict]:
    """An NWS-style hourly list: time + temperature + rain chance + conditions, now mid-series."""
    first = first or (NOW - timedelta(hours=4))
    cells = [{"path": "startTime", "key": "startTime", "label": "Time", "type": "time", "unit": None},
             {"path": "temperature", "key": "temperature", "label": "Temperature", "type": "number", "unit": "°F"},
             {"path": "pop", "key": "pop", "label": "Rain chance", "type": "number", "unit": "%"},
             {"path": "shortForecast", "key": "shortForecast", "label": "Conditions", "type": "text", "unit": None}]
    answer = {"kind": "list", "name": "hours", "label": "Hourly forecast", "path": "properties.periods",
              "words": ["hourly"], "primary": True, "cells": cells, "axis": {"cell": "startTime", "step": "hour"}}
    rows = [{"startTime": _iso(first + timedelta(hours=h)), "temperature": 50 + h, "pop": (h * 7) % 40,
             "shortForecast": ["Sunny", "Mostly Sunny", "Clear"][h % 3]} for h in range(n)]
    return [answer], {"rows": rows}


def _daily_answers(values: list[tuple[str, int, float, str]]) -> tuple[list, dict]:
    """An Open-Meteo daily columns answer: day + conditions + high + rain chance (the ask names rain)."""
    cells = [{"path": "daily.time", "key": "day", "label": "Day", "type": "date", "unit": None},
             {"path": "daily.weather_code", "key": "conditions", "label": "Conditions", "type": "number",
              "codes": "wmo_weather", "unit": None},
             {"path": "daily.temperature_2m_max", "key": "high", "label": "High", "type": "number", "unit": "°F"},
             {"path": "daily.precipitation_probability_max", "key": "rain_chance", "label": "Rain chance",
              "type": "number", "unit": "%"}]
    answer = {"kind": "columns", "name": "daily_forecast", "label": "Daily forecast", "words": ["forecast"],
              "primary": False, "cells": cells, "limit": 7, "axis": {"cell": "daily.time", "step": "day"}}
    rows = [{"day": d, "conditions": c, "high": h, "rain_chance": r} for d, r, h, c in values]
    return [answer], {"rows": rows, "zone": "UTC"}


def _bears_answers() -> tuple[list, dict]:
    chosen = [{"kind": "value", "name": "next_game", "label": "Next game", "path": "events[0].strEvent",
               "type": "text", "window": "latest", "words": ["next game"], "primary": True, "unit": None},
              {"kind": "value", "name": "start", "label": "Start", "path": "events[0].strTimestamp", "type": "time",
               "utc": True, "window": "latest", "words": ["when"], "primary": True, "unit": None},
              {"kind": "value", "name": "venue", "label": "Venue", "path": "events[0].strVenue", "type": "text",
               "window": "latest", "words": ["where"], "primary": True, "unit": None}]
    outputs = {"venue": "Lambeau Field", "start": "2026-10-11T17:00:00+00:00",
               "next_game": "Green Bay Packers vs Chicago Bears"}
    return chosen, outputs


def _design(answers, outputs, ask, kind, *, title=None, rows="rows", wants=None):
    return design(answers, outputs, title=title or ask, ask=ask, now=NOW, source_url=URL, cadence_s=900,
                  rows_output_name=rows, call_model=None, viewer_tz="UTC", question_kind=kind, wants=wants)


def _layout(d, span_key: str):
    return layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(span_key), NOW)


def _red(lo) -> list[str]:
    return sorted({i.code for i in lo.lint.issues if i.sev == "red"})


def _texts(clir: dict) -> list[str]:
    return [ln for p in clir["prims"] if p["k"] == "text" for ln in p["lines"]]


def _inp(ask: str, kind: str | None = None, wants: list | None = None) -> CardInput:
    return CardInput(card_id="t", ask=ask, title=ask, source_url=URL, source_kind="http_json", source_format="json",
                     raw_path=None, http_status=200, content_type="application/json",
                     fetched_at="2026-10-07T12:00:00Z", cadence_s=900, question_kind=kind, wants=list(wants or []))


# ---------------------------------------------------------------------------- A: the frame
def test_frame_rides_into_the_sealed_node_and_back_through_the_bind() -> None:
    chosen = [{"kind": "value", "name": "temperature", "label": "Temperature", "path": "current.temperature_2m",
               "type": "number", "unit": "°F", "window": "now", "words": ["temperature"], "primary": True}]
    node = form_scene(chosen, {"temperature": 47.0}, title="temperature", ask="how warm is it in Boise right now",
                      now=NOW, source_url=URL, cadence_s=900, question_kind="current_value", wants=["temperature"])
    assert node["frame"] == {"kind": "current_value", "wants": ["temperature"]}
    ni.validate_scene(node)
    bound = ni.bind_scene(node, {"temperature": 47.0}, form_ctx=ni._form_bind_context(SPEC, NOW))
    assert bound["design"] == {"designer": "rules", "pick": node["design"]["pick"]}
    assert bound["lint"]["red"] == 0 and isinstance(bound["lint"]["codes"], list)
    assert "design_needs_attention" not in bound
    ni._enforce_form_shape(bound)
    legacy = form_scene(chosen, {"temperature": 47.0}, title="temperature", ask="how warm", now=NOW)
    assert "frame" not in legacy     # absent = legacy: nothing sealed when the words stated no frame


def test_frame_validator_is_closed() -> None:
    chosen = [{"kind": "value", "name": "p", "label": "Price", "path": "p", "type": "number", "unit": "$"}]
    node = form_scene(chosen, {"p": 1.5}, title="p", ask="price of p", now=NOW, question_kind="current_value")
    ni.validate_scene(node)
    for bad in ({"kind": "oracle", "wants": []}, {"kind": None, "wants": ["x"] * 9}, {"kind": None, "wants": ["y" * 41]},
                {"kind": "current_value"}, {"kind": "current_value", "wants": [], "extra": 1}):
        with pytest.raises(ValueError, match="frame"):
            ni.validate_scene({**node, "frame": bad})
    ni.validate_scene({**node, "frame": {"kind": None, "wants": []}})
    bad_design = {**node, "design": {**node["design"], "fallback": False}}
    with pytest.raises(ValueError, match="fallback"):
        ni.validate_scene(bad_design)


def test_hourly_forecast_is_a_series_never_the_next_period() -> None:
    answers, outputs = _hourly_answers()
    for kind in (None, "forecast"):
        d = _design(answers, outputs, "hourly temps in Minneapolis today", kind)
        assert d.node["form"] in ("series_line", "day_table", "agenda"), d.node["form"]
        assert "temperature" in _layout(d, d.node["spans"]["desktop"]).content["fields"]
        assert "Temperature" in _layout(d, d.node["spans"]["desktop"]).clir["summary"]


def test_next_event_is_eligible_only_for_its_question_or_a_next_want() -> None:
    prof = Profile(sig="", n_rows=3, fields=[], signatures=[], time=TimeProfile(None, None, None, False, 0, 0, 0),
                   wants=[], wants_coverage={}, history_points=0)
    assert not next_event_eligible(_inp("hourly temps today"), prof)
    assert next_event_eligible(_inp("when does the next launch go", "next_event"), prof)
    assert next_event_eligible(_inp("Cubs schedule this week", "schedule"), prof)
    prof.wants = ["next"]
    assert next_event_eligible(_inp("next launch"), prof)
    answers, outputs = _hourly_answers()
    rec, inp = from_answers(answers, outputs, history=None, context=CTX, ask="hourly temps today", title="t",
                            cadence_s=900, rows_output_name="rows")
    forms = {c.form for c in enumerate_cands(rec, pf.profile(rec, inp, NOW), inp, NOW)}
    assert "next_event" not in forms and forms & {"series_line", "agenda", "day_table"}


def test_the_asked_quantity_leads_the_row_and_is_never_in_the_drop_list() -> None:
    days = [("2026-10-07", 0, 85.8, 1), ("2026-10-08", 1, 86.0, 0), ("2026-10-09", 0, 87.8, 3),
            ("2026-10-10", 72, 70.5, 82), ("2026-10-11", 74, 77.5, 61), ("2026-10-12", 11, 86.1, 2),
            ("2026-10-13", 7, 88.6, 0)]
    answers, outputs = _daily_answers(days)
    d = _design(answers, outputs, "rain chances in Nashville this week", None, title="rain chances")
    roles = {f.name: f.role for f in d.rec.fields}
    assert roles["rain_chance"] == "value" and roles["high"] == "secondary"   # the asked number leads
    assert d.prof.asked == ["rain_chance"]
    assert d.node["form"] == "day_table"
    for span in (d.node["spans"]["desktop"], "d1x2", "d2x2"):
        lo = _layout(d, span)
        assert "rain_chance" in lo.content["fields"], (span, lo.content)
        assert not _red(lo), (span, _red(lo))
    assert "Rain chance" in _layout(d, d.node["spans"]["desktop"]).clir["summary"]


def test_asked_fields_match_by_word_and_by_synonym_group() -> None:
    from smartbrain_3000.ni_forms.types import Field
    fs = [Field(name="coin", label="Coin", path="name", type="text"),
          Field(name="price", label="Price", path="current_price", type="currency"),
          Field(name="market_cap", label="Market cap", path="market_cap", type="currency"),
          Field(name="pop", label="Rain chance", path="probabilityOfPrecipitation.value", type="percent")]
    assert asked.asked_fields(fs, "top 10 cryptocurrencies by market cap") == ["market_cap"]
    assert asked.asked_fields(fs, "chance of precipitation") == ["pop"]          # rain <-> precipitation
    assert asked.asked_fields(fs, "what is it trading at", ["price"]) == ["price"]  # the frame's wants count
    assert asked.asked_fields(fs, "latest headlines") == []


def test_frame_forms_follow_the_b2_table() -> None:
    answers, outputs = _hourly_answers()
    rec, inp = from_answers(answers, outputs, history=None, context=CTX, ask="hourly", title="t", cadence_s=0,
                            rows_output_name="rows")
    prof = pf.profile(rec, inp, NOW)
    assert frame_forms("forecast", rec, prof)[0] == "series_line"
    # event_curve leads the timed lists (its match() guards the alternating-extrema signature)
    assert frame_forms("next_event", rec, prof)[:2] == ["event_curve", "next_event"]
    assert frame_forms("schedule", rec, prof)[:3] == ["event_curve", "agenda", "day_table"]
    assert frame_forms(None, rec, prof) == [] and frame_forms("", rec, prof) == []
    dans, douts = _daily_answers([("2026-10-10", 0, 43.2, 0), ("2026-10-11", 0, 37.5, 61)])
    drec, dinp = from_answers(dans, douts, history=None, context=CTX, ask="snow", title="t", cadence_s=0,
                              rows_output_name="rows")
    assert frame_forms("forecast", drec, pf.profile(drec, dinp, NOW))[0] == "day_table"
    chosen, bouts = _bears_answers()
    mrec, minp = from_answers(chosen, bouts, history=None, context=CTX, ask="next Bears game", title="t", cadence_s=0)
    assert frame_forms("next_event", mrec, pf.profile(mrec, minp, NOW)) == ["next_event", "stat"]
    moon = [{"kind": "list", "name": "phases", "label": "Phases", "path": "phasedata", "words": [],
             "cells": [{"path": "phase", "key": "phase", "label": "Phase", "type": "text"},
                       {"path": "day", "key": "day", "label": "Day", "type": "number"}]}]
    rrec, rinp = from_answers(moon, {"rows": [{"phase": "New Moon", "day": 10}]}, history=None, context=CTX,
                              ask="next new moon", title="t", cadence_s=0, rows_output_name="rows")
    assert frame_forms("next_event", rrec, pf.profile(rrec, rinp, NOW))[0] == "table"   # no time axis: a lookup


def test_present_menu_shows_the_frame_and_l_ask_gates_a_pick_that_drops_an_asked_field() -> None:
    answers, outputs = _hourly_answers()
    rec, inp = from_answers(answers, outputs, history=None, context={**CTX, "question_kind": "forecast",
                                                                       "wants": ["temperature"]},
                            ask="hourly temps today", title="t", cadence_s=900, rows_output_name="rows",
                            question_kind="forecast", wants=["temperature"])
    prof = pf.profile(rec, inp, NOW)
    cands = enumerate_cands(rec, prof, inp, NOW)
    msgs, _ids, _schema = build_messages(cands, rec, prof, inp)
    assert '"kind": "forecast"' in msgs[1]["content"] and '"asked_fields": ["temperature"]' in msgs[1]["content"]
    keep = Candidate(id="c0", form="series_line", variant="single", bindings={}, params={}, describes="", covers=[],
                     intent="trend", plans={"d1x1": "p"}, rejected_spans={}, default_span="d1x1", phone_span=None,
                     floor_score=1.0, shows={"d1x1": {"fields": ["temperature", "startTime"], "rows": 18}})
    drop = Candidate(id="c1", form="agenda", variant="list", bindings={}, params={}, describes="", covers=[],
                     intent="today", plans={"d1x1": "p"}, rejected_spans={}, default_span="d1x1", phone_span=None,
                     floor_score=1.2, shows={"d1x1": {"fields": ["shortForecast", "startTime"], "rows": 18}})
    assert "L-ASK:c1" in _gates(drop, [keep, drop], prof)
    assert not _gates(keep, [keep, drop], prof)


# ---------------------------------------------------------------------------- B: series_line
def test_series_line_needs_three_points() -> None:
    two = [("2026-10-10", 0, 43.2, 0), ("2026-10-11", 0, 37.5, 61)]
    answers, outputs = _daily_answers(two)
    rec, inp = from_answers(answers, outputs, history=None, context=CTX, ask="snow forecast this weekend",
                            title="snow forecast", cadence_s=0, rows_output_name="rows")
    assert FORMS["series_line"].match(rec, pf.profile(rec, inp, NOW)) == []
    d = _design(answers, outputs, "snow forecast for Tahoe this weekend", "forecast", title="snow forecast")
    assert d.node["form"] == "day_table"
    answers3, outputs3 = _daily_answers([*two, ("2026-10-12", 3, 39.0, 1)])
    rec3, inp3 = from_answers(answers3, outputs3, history=None, context=CTX, ask="snow", title="t", cadence_s=0,
                              rows_output_name="rows")
    assert FORMS["series_line"].match(rec3, pf.profile(rec3, inp3, NOW))


def test_forecast_series_leads_with_now_without_a_delta_and_lists_forward() -> None:
    cells = [{"path": "hourly.time", "key": "time", "label": "Time", "type": "time", "unit": None},
             {"path": "hourly.wave_height", "key": "wave_height", "label": "Wave height", "type": "number", "unit": "ft"}]
    answer = {"kind": "columns", "name": "waves_hourly", "label": "Wave height by hour", "words": ["surf"],
              "primary": False, "cells": cells, "limit": 24, "axis": {"cell": "hourly.time", "step": "hour"}}
    first = NOW - timedelta(hours=5)      # the live surf card: a day of hours read at 5 am, mostly ahead
    rows = [{"time": _iso(first + timedelta(hours=h)), "wave_height": round(3.0 + 0.05 * (h // 6), 3)} for h in range(24)]
    d = _design([answer], {"rows": rows, "zone": "UTC"}, "surf forecast Huntington Beach", "forecast", title="surf forecast")
    assert d.node["form"] == "series_line"
    for span in (d.node["spans"]["desktop"], "d2x3", "d3x1"):
        lo = _layout(d, span)
        assert not any(p.get("role") == "delta" for p in lo.clir["prims"]), span
        assert not any("(0.00%)" in ln or ln.startswith("0.000") for ln in _texts(lo.clir)), (span, _texts(lo.clir))
        subs = [p["t"] for p in lo.clir["prims"] if p["k"] == "time" and p.get("role") == "sub"]
        assert subs == sorted(subs), (span, subs)
        assert all(t >= _iso(NOW - timedelta(hours=1)) for t in subs), (span, subs)
    assert _layout(d, d.node["spans"]["desktop"]).clir["summary"].startswith("Wave height: 3.0 ft now")


# ---------------------------------------------------------------------------- C: echo, hollow, fallback
def _txt(pid, y, s, role="row", src="data", px=16):
    return {"k": "text", "id": pid, "x": ["l", 0], "y": y, "max_w": 180, "lines": [s], "role": role, "px": px,
            "tok": "text", "anchor": "start", "dir": "ltr", "src": src}


def test_title_echo_compares_labels_only_never_a_data_cell() -> None:
    b = bucket(Span.parse("d1x1"))
    inp = _inp("new moon")
    base = {"v": 1, "form": "table", "variant": "plain", "cand": "c0", "plan": "p", "span": "d1x1",
            "bucket": {"min_w": b.min_w, "max_w": b.max_w, "h": b.h}, "state": "ok", "reading_order": [1, 2],
            "summary": "s", "live": [], "hitmap": []}
    data = {**base, "prims": [_txt(1, 14, "new moon", "title", "title", 14), _txt(2, 60, "New Moon", "row", "data")]}
    label = {**base, "prims": [_txt(1, 14, "new moon", "title", "title", 14), _txt(2, 60, "New Moon", "meta", "key", 12)]}
    codes_data = {i.code for i in lint(data, None, None, None, None, inp, b, meta={"body": {"y0": 30, "y1": 120}}).issues}
    codes_label = {i.code for i in lint(label, None, None, None, None, inp, b, meta={"body": {"y0": 30, "y1": 120}}).issues}
    assert "title_echo" not in codes_data and "title_echo" in codes_label


def _moon_case() -> tuple[list, dict]:
    cells = [{"path": "phase", "key": "phase", "label": "Phase", "type": "text", "unit": None},
             {"path": "month", "key": "month", "label": "Month", "type": "number", "unit": None},
             {"path": "day", "key": "day", "label": "Day", "type": "number", "unit": None},
             {"path": "time", "key": "time", "label": "Time (UT)", "type": "text", "unit": None}]
    answer = {"kind": "list", "name": "phases", "label": "Upcoming moon phases", "path": "phasedata",
              "words": ["moon phase", "new moon"], "primary": True, "cells": cells}
    rows = [{"phase": "New Moon", "month": 10, "day": 10, "time": "07:50"},
            {"phase": "First Quarter", "month": 10, "day": 18, "time": "16:13"},
            {"phase": "Full Moon", "month": 10, "day": 26, "time": "04:12"},
            {"phase": "Last Quarter", "month": 11, "day": 1, "time": "20:28"}]
    return [answer], {"rows": rows}


def test_rows_that_all_fit_a_span_are_its_designed_few_state() -> None:
    answers, outputs = _moon_case()
    d = _design(answers, outputs, "what day is the next new moon", "next_event", title="new moon")
    assert d.node["form"] in ("table", "ranked_list"), d.node["form"]
    assert not d.cand.fallback
    for span in (d.node["spans"]["desktop"], "d2x2", "d3x3"):
        lo = _layout(d, span)
        assert "L-HOLLOW" not in _red(lo) and "title_echo" not in _red(lo), (span, _red(lo))
    assert "New Moon" in _layout(d, d.node["spans"]["desktop"]).clir["summary"]


def test_universal_fallback_when_no_form_survives() -> None:
    cells = [{"path": "t", "key": "t", "label": "Time", "type": "time", "unit": None},
             {"path": "v", "key": "v", "label": "Reading", "type": "number", "unit": None}]
    answer = {"kind": "columns", "name": "s", "label": "Series", "words": [], "primary": True, "cells": cells,
              "axis": {"cell": "t", "step": "hour"}}
    outputs = {"rows": [{"t": _iso(NOW - timedelta(hours=1)), "v": 1.5}, {"t": _iso(NOW), "v": 1.7}]}
    d = _design([answer], outputs, "the reading", "current_value", title="reading")
    assert d.node["form"] == "table" and d.node["design"]["fallback"] is True and d.cand.fallback
    ni.validate_scene(d.node)
    bound = ni.bind_scene(d.node, outputs, form_ctx=ni._form_bind_context(SPEC, NOW))
    assert bound["type"] == "form" and bound["form"] == "table" and isinstance(bound["lint"]["codes"], list)
    assert "design_needs_attention" not in bound   # the fallback re-enumerates to the same plain card


# ---------------------------------------------------------------------------- D: the subject row filter
_HOLIDAYS = {"kind": "list", "name": "holidays", "label": "Public holidays", "path": "items",
             "words": ["holidays", "thanksgiving", "christmas"], "primary": True,
             "cells": [{"path": "date", "key": "date", "label": "Date", "type": "date"},
                       {"path": "name", "key": "name", "label": "Holiday", "type": "text"}],
             "axis": {"cell": "date", "step": "day"}}
_HOLIDAY_ROWS = [{"date": "2026-10-12", "name": "Columbus Day"}, {"date": "2026-11-11", "name": "Veterans Day"},
                 {"date": "2026-11-26", "name": "Thanksgiving Day"}, {"date": "2026-11-27", "name": "Day after Thanksgiving"},
                 {"date": "2026-12-25", "name": "Christmas Day"}]


def test_subject_filter_selects_exactly_the_rows_that_name_the_subject() -> None:
    scoped = ni_flow._scope_rows_to_subject(_HOLIDAYS, {"items": _HOLIDAY_ROWS}, "Thanksgiving", None)
    assert scoped["filter"] == {"path": "name", "equals": "Thanksgiving Day"}   # fewest extra words wins
    moon, rows = _moon_case()
    scoped = ni_flow._scope_rows_to_subject(moon[0], {"phasedata": rows["rows"]}, "new moon", None)
    assert scoped["filter"] == {"path": "phase", "equals": "New Moon"}
    assert ni_flow._scope_rows_to_subject({**_HOLIDAYS, "filter": {"path": "name", "equals": "x"}},
                                          {"items": _HOLIDAY_ROWS}, "Thanksgiving", None)["filter"]["equals"] == "x"


def test_subject_filter_leaves_a_list_named_by_its_own_subject_alone() -> None:
    npr = {"kind": "list", "name": "latest", "label": "Headlines", "path": "items", "words": ["headlines", "news"],
           "cells": [{"path": "title", "key": "title", "label": "Title", "type": "text"}]}
    rows = [{"title": "A story"}, {"title": "Another story"}]
    assert ni_flow._scope_rows_to_subject(npr, {"items": rows}, "NPR headlines", None) is npr
    coins = {"kind": "list", "name": "top_coins", "label": "Top coins by market cap", "path": "items",
             "words": ["top cryptocurrencies", "market cap"],
             "cells": [{"path": "name", "key": "name", "label": "Coin", "type": "text"}]}
    assert ni_flow._scope_rows_to_subject(coins, {"items": [{"name": "Bitcoin"}]}, "cryptocurrencies", None) is coins
    launches = {"kind": "list", "name": "launches", "label": "Upcoming launches", "path": "results",
                "words": ["rocket launches", "next launch"],
                "cells": [{"path": "name", "key": "name", "label": "Launch", "type": "text"}]}
    sample = {"results": [{"name": "Falcon 9 Block 5 | Starlink Group 17-10"}]}
    assert ni_flow._scope_rows_to_subject(launches, sample, "rocket launch", None) is launches
    assert ni_flow._scope_rows_to_subject(launches, sample, "Vandenberg rocket launch", "Vandenberg") is launches
    assert ni_flow._scope_rows_to_subject(launches, sample, "", None) is launches


def test_subject_filter_leaves_a_participant_named_in_several_cells_alone() -> None:
    """A team sits in the away column of a past game and the home column of the next ones: one cell
    cannot select "the Braves games", so the schedule stays whole (the verify table's Mets cases)."""
    sched = {"kind": "list", "name": "upcoming", "label": "Upcoming games", "path": "dates", "words": ["schedule"],
             "cells": [{"path": "games[0].gameDate", "key": "games[0].gameDate", "label": "Start", "type": "time"},
                       {"path": "games[0].teams.away.team.name", "key": "a", "label": "Away", "type": "text"},
                       {"path": "games[0].teams.home.team.name", "key": "h", "label": "Home", "type": "text"}]}
    rows = [{"games": [{"gameDate": "2026-09-27T19:10:00Z", "teams": {"away": {"team": {"name": "Atlanta Braves"}},
                                                                     "home": {"team": {"name": "Miami Marlins"}}}}]},
            {"games": [{"gameDate": "2026-09-29T18:00:00Z", "teams": {"away": {"team": {"name": "Philadelphia Phillies"}},
                                                                     "home": {"team": {"name": "Atlanta Braves"}}}}]}]
    assert ni_flow._scope_rows_to_subject(sched, {"dates": rows}, "Atlanta Braves", None) is sched
    twin = {**_HOLIDAYS, "cells": [*_HOLIDAYS["cells"],
                                   {"path": "localName", "key": "localName", "label": "Local name", "type": "text"}]}
    rows2 = [{**r, "localName": r["name"]} for r in _HOLIDAY_ROWS]
    scoped = ni_flow._scope_rows_to_subject(twin, {"items": rows2}, "Thanksgiving", None)
    assert scoped["filter"] == {"path": "localName", "equals": "Thanksgiving Day"}   # same rows: still one row


def test_subject_filter_refuses_honestly_when_no_row_names_the_subject() -> None:
    with pytest.raises(ValueError, match="the list has no row for Diwali"):
        ni_flow._scope_rows_to_subject(_HOLIDAYS, {"items": _HOLIDAY_ROWS}, "Diwali", None)


def test_a_filtered_lookup_row_builds_the_card_over_that_row_only() -> None:
    scoped = ni_flow._scope_rows_to_subject(_HOLIDAYS, {"items": _HOLIDAY_ROWS}, "Thanksgiving", None)
    fb = ni_flow.FormBuild(now=NOW, ask="when is Thanksgiving this year", source_url=URL, cadence_s=900,
                           frame_kind="next_event", wants=("Thanksgiving date",))
    built = ni_flow.build_from_answers([scoped], {"items": _HOLIDAY_ROWS}, "Thanksgiving", next_event=True,
                                       frame_kind="next_event", form=fb)
    rows = built["preview_payload"]["rows"]
    assert [r["name"] for r in rows] == ["Thanksgiving Day"]
    assert any(op.get("fn") == "where" and op.get("value") == "Thanksgiving Day"
               for st in built["pipeline"] for op in st.get("apply") or [])
    scene = built["scene"]
    assert scene["frame"] == {"kind": "next_event", "wants": ["Thanksgiving date"]}
    assert scene["form"] in ("next_event", "stat", "kv_grid", "day_table", "ranked_list")
    bound = ni.bind_scene(scene, built["preview_payload"], form_ctx=ni._form_bind_context(SPEC, NOW))
    assert "Thanksgiving" in bound["summary"] and bound["lint"]["red"] == 0


# ---------------------------------------------------------------------------- E: map_lite counts
def test_map_lite_one_unnamed_point_takes_the_box_and_counts_one_place() -> None:
    chosen = [{"kind": "value", "name": "latitude", "label": "Latitude", "path": "latitude", "type": "number",
               "unit": "°", "window": "now", "words": ["where"], "primary": True},
              {"kind": "value", "name": "longitude", "label": "Longitude", "path": "longitude", "type": "number",
               "unit": "°", "window": "now", "words": ["where"], "primary": True},
              {"kind": "value", "name": "altitude", "label": "Altitude", "path": "altitude", "type": "number",
               "unit": "km", "window": "now", "words": ["altitude"], "primary": True}]
    outputs = {"altitude": 426.35, "longitude": -67.086, "latitude": 29.81}
    d = _design(chosen, outputs, "where is the ISS right now on a map", "current_value", title="ISS", rows=None)
    assert d.node["form"] == "map_lite"
    for span in (d.node["spans"]["desktop"], "d2x1", "p2x1", "d2x2"):
        lo = _layout(d, span)
        assert "+1 more" not in _texts(lo.clir), span
        assert lo.clir["summary"] == "1 place"
        assert not _red(lo), (span, _red(lo))


# ---------------------------------------------------------------------------- F: diagnostics + the stat word card
def test_enforce_form_shape_accepts_legacy_and_new_bound_nodes() -> None:
    chosen = [{"kind": "value", "name": "p", "label": "Price", "path": "p", "type": "number", "unit": "$"}]
    node = form_scene(chosen, {"p": 1.5}, title="p", ask="price of p", now=NOW)
    bound = ni.bind_scene(node, {"p": 1.5}, form_ctx=ni._form_bind_context(SPEC, NOW))
    ni._enforce_form_shape(bound)
    legacy = {k: v for k, v in bound.items() if k != "design"}
    legacy["lint"] = {"red": 0, "amber": 0}
    ni._enforce_form_shape(legacy)
    with pytest.raises(ni.NIError):
        ni._enforce_form_shape({**bound, "design": {"designer": "oracle", "pick": "c0"}})
    with pytest.raises(ni.NIError):
        ni._enforce_form_shape({**bound, "lint": {"red": 0, "amber": 0, "codes": ["x"] * 13}})


def test_stat_word_lists_the_facts_that_fit_and_wraps_a_long_hero() -> None:
    chosen, outputs = _bears_answers()
    d = _design(chosen, outputs, "next Bears game", "next_event", title="Bears game", rows=None)
    assert d.node["form"] == "stat" and d.node["variant"] == "word"
    desk = _layout(d, "d2x1")
    assert {"next_game", "start", "venue"} <= set(desk.content["fields"]), desk.content
    assert not _red(desk), _red(desk)
    phone = _layout(d, "p2x1")
    assert not _red(phone), _red(phone)
    heroes = [p for p in phone.clir["prims"] if p.get("role") == "hero"]
    assert heroes and len(heroes[0]["lines"]) == 2 and not any(ln.endswith("…") for ln in heroes[0]["lines"])


# ---------------------------------------------------------------------------- G: statistic precision
def test_statistic_decimals_follow_the_column() -> None:
    from smartbrain_3000.ni_forms.types import Field
    one = Field(name="snow", label="Snowfall", path="s", type="quantity", unit="in")
    assert fmt.stat_decimals(one, [0.0, 0.0], 0.0) == 2          # never four decimals on a one-decimal column
    assert fmt.stat_decimals(one, [0.0, 1.5], 0.75) == 2
    assert fmt.stat_decimals(one, [3.084, 3.15], 3.117) == 3       # the magnitude rule inside the column's range
    price = Field(name="p", label="Price", path="p", type="currency", currency="USD", precision=2)
    assert fmt.stat_decimals(price, [83621.0, 83622.0], 83621.5) == 2   # never fewer than the field shows
    assert fmt.column_decimals(one, []) is None


def test_prov_average_without_an_explicit_dec_follows_the_column() -> None:
    from smartbrain_3000.ni_forms.canon import canonical, fingerprint, sha256
    from smartbrain_3000.ni_forms.types import Context, DataRecord, Field
    fields = [Field(name="t", label="Day", path="t", type="date", role="date"),
              Field(name="v", label="Snowfall", path="v", type="number", role="value")]
    rows = [["2026-10-10", 0.0], ["2026-10-11", 0.0]]
    rec = DataRecord(v=1, kind="series", fields=fields, rows=rows,
                     context=Context(fetched_at="2026-10-07T12:00:00Z", as_of=None, source_host="x", card_tz="UTC",
                                     card_tz_src="data"), producer="json_profile")
    rec.fingerprint, rec.data_hash = fingerprint("series", fields, 2), sha256(canonical(rows))
    assert prov.run(["d", "agg", ["v", "avg"], {"field": "v"}], RView(rec), _inp("snow"), "") == "0.00"
    assert prov.run(["d", "agg", ["v", "avg"], {"field": "v", "dec": 1}], RView(rec), _inp("snow"), "") == "0.0"


# ---------------------------------------------------------------------------- lead review additions
def test_tide_curve_outranks_next_event_for_a_next_high_tide_ask() -> None:
    """The signature-specific form keeps its place under the frame prior: tides asked as "next high
    tide" are the curve with the next-event headline, never a bare next_event (plan B2)."""
    start = NOW.replace(hour=4, minute=40)
    rows = [{"t": _iso(start + timedelta(hours=6.2 * i)), "h": [5.5, 0.8, 6.0, 1.1][i % 4],
             "k": ["High", "Low"][i % 2]} for i in range(6)]
    cells = [{"path": "t", "key": "t", "label": "Time", "type": "time", "unit": None},
             {"path": "h", "key": "h", "label": "Height", "type": "number", "unit": "ft"},
             {"path": "k", "key": "k", "label": "Tide", "type": "text", "unit": None}]
    answers = [{"kind": "list", "name": "tides", "label": "Tide predictions", "path": "p", "cells": cells,
                "axis": {"cell": "t", "step": "hour"}, "words": ["tides"], "primary": True}]
    d = _design(answers, {"rows": rows}, "next high tide at Charleston", "next_event", wants=["next high tide"])
    assert d.node["form"] == "event_curve", d.node["form"]
    assert "next_event" in {c.form for c in d.cands}      # still offered as the runner-up


def test_a_legacy_next_event_node_keeps_its_form_at_bind() -> None:
    """A node sealed before frames existed carries none; the bind must still offer its sealed form
    (eligibility withholds next_event only from NEW designs that stated another kind)."""
    answers, outputs = _hourly_answers()
    node = _design(answers, outputs, "when is the next period", "next_event").node
    assert node["form"] == "next_event", node["form"]
    node.pop("frame")
    ni.validate_scene(node)
    bound = ni.bind_scene(node, outputs, form_ctx=ni._form_bind_context(SPEC, NOW))
    assert bound["form"] == "next_event"
    assert "design_needs_attention" not in bound

