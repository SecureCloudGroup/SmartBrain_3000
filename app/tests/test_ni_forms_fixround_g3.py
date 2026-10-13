"""Fix round G3 (2026-10-12/13, live refetch of set G — fixtures/ni_blind/live_2026-10-12g):
one labeled case per card the round fixed. S1 a one-row stat never leads with a bare date/time
cell over a real measure (G5, G8 as a secondary); S2 a text product (a discussion / summary /
outlook / brief / advisory) is an honest stop with no genuine text cell (G50); S3 an asked
condition word leads a list's row with the conditions cell, never a temperature curve (G10);
S4 a moment with a clock face leads with the time itself, the live countdown riding under it
(G40); S5 an NWS forecast period's own name is derived from its start time (G49); S6 an explicit
step word prefers the same-source list answer declaring that axis step (G9).

Record/catalog cases run through ``form_scene.design`` on synthetic answers shaped exactly like
the Library's own declarations (uv_daily, nws-forecast periods, Open-Meteo's hourly columns,
Open-Meteo sunrise/sunset) — no network, no model (``call_model=None``); the two query-layer /
flow cases call the changed functions directly.
"""
from __future__ import annotations

from datetime import UTC, datetime

from smartbrain_3000.ni_forms.form_scene import design
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.record import from_answers
from smartbrain_3000.ni_forms.spans import Span
from smartbrain_3000.ni_query.clauses import step_answer

NOW = datetime(2026, 10, 13, 0, 39, tzinfo=UTC)
URL = "https://api.example.org/x"


def _hero(lo) -> str:
    p = next(p for p in lo.clir["prims"] if p["k"] in ("text", "time") and p["role"] == "hero")
    return " ".join(p["lines"]) if p["k"] == "text" else p["t"]


def _texts(lo, role: str) -> list[str]:
    return [" ".join(p["lines"]) for p in lo.clir["prims"] if p["k"] == "text" and p["role"] == role]


# ---- S1: a one-row stat leads with the asked quantity, never a bare date (G5, G8) -------------

def test_g5_uv_index_hero_is_the_asked_quantity_not_the_day() -> None:
    answer = {"kind": "columns", "label": "UV index by day (max)", "axis": {"cell": "daily.time", "step": "day"},
              "cells": [{"path": "daily.time", "key": "day", "type": "date", "label": "Day"},
                        {"path": "daily.uv_index_max", "key": "max_uv", "type": "number", "label": "Max UV"}]}
    outputs = {"rows": [{"day": "2026-10-13", "max_uv": 5.2}]}
    d = design([answer], outputs, title="UV index", ask="UV index in Phoenix this afternoon", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC", question_kind="current_value",
              wants=["UV index"], rows_output_name="rows")
    assert d.node["form"] == "stat"
    roles = {f["name"]: f["role"] for f in d.node["record"]["fields"]}
    assert roles == {"day": "secondary", "max_uv": "measure"}
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"]["desktop"]), NOW)
    assert _hero(lo) == "5.2" and _texts(lo, "sub") == ["Max UV"]


def test_g8_snow_forecast_shows_the_cut_day_as_a_secondary() -> None:
    answer = {"kind": "columns", "label": "Snow forecast", "axis": {"cell": "daily.time", "step": "day"},
              "cells": [{"path": "daily.time", "key": "day", "type": "date", "label": "Day"},
                        {"path": "daily.snowfall_sum", "key": "snowfall", "type": "number", "label": "Snowfall",
                         "unit": "in"},
                        {"path": "daily.temperature_2m_min", "key": "low", "type": "number", "label": "Low",
                         "unit": "°F"},
                        {"path": "daily.weather_code", "key": "conditions", "type": "number",
                         "label": "Conditions", "codes": "wmo_weather"}]}
    outputs = {"rows": [{"day": "2026-10-15", "snowfall": 0.0, "low": 43.2, "conditions": "Light rain"}]}
    d = design([answer], outputs, title="Snow", ask="chance of snow in Denver on Thursday", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="UTC", question_kind="forecast",
              wants=["chance of snow"], rows_output_name="rows")
    assert d.node["form"] == "conditions"
    roles = {f["name"]: f["role"] for f in d.node["record"]["fields"]}
    assert roles["day"] == "secondary" and roles["snowfall"] == "measure"
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"]["desktop"]), NOW)
    assert any("Oct 15" in t for t in _texts(lo, "sub") + _texts(lo, "row-strong"))


# ---- S2: a text product is an honest stop with no genuine text cell (G50) --------------------

def test_g50_text_product_gap_flags_a_bare_category_conditions_cell() -> None:
    from smartbrain_3000 import ni_flow as F

    fields = [{"name": "period", "type": "datetime", "role": "measure"},
              {"name": "temperature", "type": "quantity", "role": "secondary"},
              {"name": "conditions", "type": "category", "role": "secondary"}]
    built = {"scene": {"record": {"kind": "measure", "fields": fields}}}
    intent = {"wants": ["forecast discussion"]}
    assert F._text_product_gap(built, intent) == "discussion"

    # F49 (live, partial): the same shape, but the conditions cell came back long enough to
    # classify ``text`` — covered, never refused.
    fields_text = [{**fields[0], "role": "secondary"},
                   {**fields[1], "role": "secondary"},
                   {"name": "conditions", "type": "text", "role": "measure"}]
    built_text = {"scene": {"record": {"kind": "measure", "fields": fields_text}}}
    assert F._text_product_gap(built_text, intent) is None
    # a non-text want (summary of today's TEMPERATURE) is never gated by this check
    assert F._text_product_gap(built, {"wants": ["temperature"]}) is None


def test_only_a_discussion_is_a_text_product_never_a_summary_or_an_advisory() -> None:
    """Lead probe: a summary / outlook / brief is served by the periods' short forecasts (F #49,
    G #49 live and partial), and an advisory is an alert the alert lists serve — a one-row alert
    list binds as a measure whose event cell is a category, so neither word may refuse."""
    from smartbrain_3000 import ni_flow as F

    periods = {"scene": {"record": {"kind": "measure", "fields": [
        {"name": "period", "type": "datetime", "role": "secondary"},
        {"name": "temperature", "type": "quantity", "role": "secondary"},
        {"name": "conditions", "type": "category", "role": "measure"}]}}}
    for want in ("short weather summary", "weather outlook", "weather brief"):
        assert F._text_product_gap(periods, {"wants": [want]}) is None
    one_alert = {"scene": {"record": {"kind": "measure", "fields": [
        {"name": "event", "type": "category", "role": "measure"},
        {"name": "area", "type": "text", "role": "secondary"},
        {"name": "expires", "type": "datetime", "role": "secondary"}]}}}
    assert F._text_product_gap(one_alert, {"wants": ["heat advisory"]}) is None
    assert F._text_product_gap(periods, {"wants": ["forecast discussion"]}) == "discussion"


# ---- S3: an asked condition word leads with conditions, never a temperature curve (G10) -------

def test_g10_fog_ask_leads_with_conditions_not_a_temperature_curve() -> None:
    answer = {"kind": "columns", "label": "Tonight, hour by hour", "axis": {"cell": "hourly.time", "step": "hour"},
              "cells": [{"path": "hourly.time", "key": "time", "type": "time", "label": "Time"},
                        {"path": "hourly.weather_code", "key": "conditions", "type": "number",
                         "label": "Conditions", "codes": "wmo_weather"},
                        {"path": "hourly.temperature_2m", "key": "temperature", "type": "number",
                         "label": "Temperature", "unit": "°F"}]}
    hours = [f"2026-10-13T{h:02d}:00:00Z" for h in range(24)]
    conds = ["Fog" if h < 6 else "Overcast" for h in range(24)]
    temps = [47.9 - h * 0.1 for h in range(24)]
    outputs = {"rows": [{"time": t, "conditions": c, "temperature": v} for t, c, v in zip(hours, conds, temps)]}
    rec, _ = from_answers([answer], outputs, history=None,
                          context={"source_url": URL, "fetched_at": "2026-10-13T00:39:00Z", "viewer_tz": "UTC"},
                          ask="will it be foggy in Portland Oregon tomorrow morning", title="fog",
                          cadence_s=900, rows_output_name="rows", wants=["fog"])
    roles = {f.name: f.role for f in rec.fields}
    assert roles["conditions"] == "value" and roles["temperature"] == "secondary"
    from smartbrain_3000.ni_forms.catalog.series_line import SeriesLine
    assert SeriesLine().match(rec, None) == []           # never a temperature curve for this ask


def _hourly_answer_with_rain() -> tuple[dict, dict]:
    answer = {"kind": "columns", "label": "Tonight, hour by hour", "axis": {"cell": "hourly.time", "step": "hour"},
              "cells": [{"path": "hourly.time", "key": "time", "type": "time", "label": "Time"},
                        {"path": "hourly.weather_code", "key": "conditions", "type": "number",
                         "label": "Conditions", "codes": "wmo_weather"},
                        {"path": "hourly.temperature_2m", "key": "temperature", "type": "number",
                         "label": "Temperature", "unit": "°F"},
                        {"path": "hourly.precipitation_probability", "key": "rain_chance", "type": "number",
                         "label": "Rain chance", "unit": "%"}]}
    hours = [f"2026-10-13T{h:02d}:00:00Z" for h in range(24)]
    outputs = {"rows": [{"time": t, "conditions": "Fog" if h < 6 else "Overcast", "temperature": 47.9 - h * 0.1,
                         "rain_chance": h * 3} for h, t in enumerate(hours)]}
    return answer, outputs


def _roles_for(ask: str, wants: list[str]) -> dict[str, str]:
    answer, outputs = _hourly_answer_with_rain()
    rec, _ = from_answers([answer], outputs, history=None,
                          context={"source_url": URL, "fetched_at": "2026-10-13T00:39:00Z", "viewer_tz": "UTC"},
                          ask=ask, title="hourly", cadence_s=900, rows_output_name="rows", wants=wants)
    return {f.name: f.role for f in rec.fields}


def test_a_named_number_cell_outranks_a_condition_word() -> None:
    """Lead probe: when the ask names a number cell ("rain chance") a condition word beside it
    ("foggy") never takes the lead away from that number."""
    roles = _roles_for("rain chance tomorrow morning and will it be foggy", ["rain chance", "fog"])
    assert roles["rain_chance"] == "value" and roles["conditions"] != "value"


def test_quantity_words_never_lead_with_the_conditions_cell() -> None:
    """Lead probe: "windy", "humid", "rainy", "snowy" ask for a quantity a number cell carries; the
    conditions cell never leads on them (a number leads, as before this round)."""
    for ask in ("how windy will it be tomorrow", "how humid is it tonight hour by hour",
                "is it going to be rainy tomorrow", "will it be snowy tonight"):
        roles = _roles_for(ask, [])
        assert roles["conditions"] != "value", ask
        assert any(roles[n] == "value" for n in ("temperature", "rain_chance")), ask


# ---- S4: a moment with a clock face leads with the time, countdown riding under it (G40) ------

def test_g40_sunset_hero_is_the_clock_time_with_the_countdown_under_it() -> None:
    answer = {"kind": "value", "name": "sunset_tomorrow", "label": "Sunset tomorrow", "type": "time",
             "path": "sunset"}
    outputs = {"sunset_tomorrow": "2026-10-14T00:23:00Z"}
    d = design([answer], outputs, title="Sunset", ask="what time does the sun set in Denver tomorrow", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="America/Denver",
              question_kind="next_event", wants=["sunset_time"])
    assert d.node["form"] == "stat"
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"]["desktop"]), NOW)
    assert _hero(lo) == "6:23 pm"
    subs = _texts(lo, "sub")
    assert any("23 h 44 m" in s for s in subs) and "Sunset tomorrow" in subs
    assert lo.clir["live"] and lo.clir["live"][0]["k"] == "countdown"


# ---- S5: a forecast period's own name is derived from its start time (G49) -------------------

def test_g49_period_rows_show_their_derived_day_night_name() -> None:
    answer = {"kind": "list", "label": "Today", "path": "properties.periods",
             "axis": {"cell": "startTime", "step": "period"},
             "cells": [{"path": "startTime", "type": "time", "label": "Period"},
                       {"path": "temperature", "type": "number", "label": "Temperature", "unit": "°F"},
                       {"path": "shortForecast", "type": "text", "label": "Conditions"},
                       {"path": "probabilityOfPrecipitation.value", "type": "number", "label": "Rain chance",
                        "unit": "%"}]}
    outputs = {"rows": [
        {"startTime": "2026-10-13T19:00:00-07:00", "temperature": 61, "shortForecast": "Partly Sunny",
         "probabilityOfPrecipitation": {"value": 2}},
        {"startTime": "2026-10-14T06:00:00-07:00", "temperature": 52, "shortForecast": "Mostly Cloudy",
         "probabilityOfPrecipitation": {"value": 5}}]}
    d = design([answer], outputs, title="Summary", ask="short weather summary for Seattle tomorrow", now=NOW,
              source_url=URL, cadence_s=900, call_model=None, viewer_tz="America/Los_Angeles",
              question_kind="forecast", wants=["summary"], rows_output_name="rows")
    assert d.node["form"] == "ranked_list"
    # a 2-row span (phone's own pick on the live card) leaves room for each item's meta line
    lo = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse("p2x2"), NOW)
    metas = " | ".join(_texts(lo, "meta"))
    assert "Tuesday Night" in metas and "Wednesday" in metas


# ---- S6: an explicit step word prefers the list answer with that axis step (G9) ----------------

def test_g9_hourly_ask_prefers_the_hour_axis_list_over_one_hour_value() -> None:
    rain_this_hour = {"name": "rain_this_hour", "kind": "value", "measure": "precip_chance"}
    hours = {"name": "hours", "kind": "list", "axis": {"cell": "startTime", "step": "hour"}}
    answers = [rain_this_hour, hours]
    assert step_answer("hourly rain forecast for Orlando today", rain_this_hour, answers) is hours
    # no step word named: the model/floor's own pick stands
    assert step_answer("rain chance this hour in Orlando", rain_this_hour, answers) is rain_this_hour
