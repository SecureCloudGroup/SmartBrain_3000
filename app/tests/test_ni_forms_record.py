"""Phase 1a-2: ``record.from_answers`` (build time, types derived once) and
``record.from_spec`` (bind time, the sealed field specs re-read by path)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000.ni_forms.record import from_answers, from_spec, history_key

_CTX = {"source_url": "https://api.example.org/v1/thing", "fetched_at": "2026-10-06T12:00:00Z",
        "viewer_tz": "America/New_York"}


class _Shown(str):
    """A stand-in for ``ni._TimeText``: the pipeline's time text that knows its instant."""

    moment: datetime


def _shown(text: str, moment: datetime) -> _Shown:
    out = _Shown(text)
    out.moment = moment
    return out


def _value(**over):
    base = {"kind": "value", "name": "price", "label": "Price", "type": "number",
            "path": "quote.price", "unit": "$"}
    base.update(over)
    return base


def _list(**over):
    base = {"kind": "list", "label": "Tides", "path": "predictions",
            "cells": [{"path": "t", "type": "time", "label": "Time"},
                      {"path": "v", "type": "number", "label": "Height", "unit": "ft"},
                      {"path": "type", "type": "text", "label": "Tide"}]}
    base.update(over)
    return base


def test_value_single_currency_measure() -> None:
    rec, inp = from_answers([_value()], {"price": 223.86}, history=None, context=_CTX,
                            ask="price of NVDA", title="NVDA", cadence_s=1800)
    assert rec.kind == "measure"
    assert len(rec.fields) == 1
    f = rec.fields[0]
    assert (f.name, f.path, f.type, f.role, f.currency) == ("price", "price", "currency", "measure", "USD")
    assert f.label_src == "lexicon"  # a declared label is human: shown as a fact, never hidden as a key
    assert rec.rows == [[223.86]]
    assert inp.ask == "price of NVDA" and inp.viewer_tz == "America/New_York"
    assert rec.context.source_host == "api.example.org" and rec.context.card_tz == "America/New_York"


def test_value_multi_measure_then_secondary_and_as_of() -> None:
    answers = [_value(name="price", label="Price", type="number", unit="$"),
               _value(name="change", label="Change", type="number", unit="$"),
               _value(name="as_of", label="As of", type="time", unit=None, window="latest")]
    outputs = {"price": 223.86, "change": -1.65,
               "as_of": _shown("2:21 PM", datetime(2026, 10, 6, 18, 21, tzinfo=UTC))}
    rec, _ = from_answers(answers, outputs, history=None, context=_CTX,
                          ask="NVDA price", title="NVDA", cadence_s=1800)
    roles = {f.name: f.role for f in rec.fields}
    assert roles == {"price": "measure", "change": "secondary", "as_of": "as_of"}
    assert rec.rows[0][2] == "2026-10-06T18:21:00Z"   # the instant, never the words
    assert rec.context.as_of == "2026-10-06T18:21:00Z"


def test_value_time_first_leads_as_a_countdown_measure() -> None:
    answers = [_value(name="t", label="Sunset", type="time", unit=None)]
    rec, _ = from_answers(answers, {"t": _shown("6:36 PM", datetime(2026, 10, 6, 22, 36, tzinfo=UTC))},
                          history=None, context=_CTX, ask="sunset today", title="t", cadence_s=0)
    f = rec.fields[0]
    assert (f.type, f.role) == ("datetime", "measure")


def test_value_percent_quantity_count_types_and_unit_ids() -> None:
    answers = [_value(name="humidity", type="number", unit="%"),
               _value(name="wind", type="number", unit="mph"),
               _value(name="n", type="count", unit=None)]
    outputs = {"humidity": 55, "wind": 13.7, "n": 7}
    rec, _ = from_answers(answers, outputs, history=None, context=_CTX,
                          ask="weather", title="Weather", cadence_s=900)
    kinds = {f.name: f.type for f in rec.fields}
    assert kinds == {"humidity": "percent", "wind": "quantity", "n": "number"}
    assert next(f.unit for f in rec.fields if f.name == "wind") == "mph"
    assert next(f.unit for f in rec.fields if f.name == "humidity") is None


def test_value_text_is_a_category_measure() -> None:
    answers = [_value(name="label", type="text", unit=None)]
    rec, _ = from_answers(answers, {"label": "On time"}, history=None, context=_CTX,
                          ask="status", title="X", cadence_s=0)
    assert (rec.fields[0].type, rec.fields[0].role) == ("category", "measure")


def test_coded_number_answers_are_text() -> None:
    """A ``codes`` answer's output is the label the pipeline looked up, never the code."""
    answers = [_value(name="conditions", type="number", unit=None, codes="wmo_weather")]
    rec, _ = from_answers(answers, {"conditions": "Partly cloudy"}, history=None, context=_CTX,
                          ask="sky", title="X", cadence_s=0)
    assert rec.fields[0].type == "category" and rec.rows == [["Partly cloudy"]]


def test_list_dated_events_read_rows_by_the_cell_path() -> None:
    answer = _list(axis={"cell": "t", "step": "hour"},
                   cells=[{"path": "t", "type": "time", "label": "Time"},
                          {"path": "h", "type": "number", "label": "Height", "unit": "ft"},
                          {"path": "k", "type": "text", "label": "Tide"}])
    outputs = {"rows": [
        {"t": _shown("1:40 AM", datetime(2026, 10, 6, 5, 40, tzinfo=UTC)), "h": 5.5, "k": "High"},
        {"t": _shown("7:48 AM", datetime(2026, 10, 6, 11, 48, tzinfo=UTC)), "h": 0.8, "k": "Low"},
        {"t": _shown("2:10 PM", datetime(2026, 10, 6, 18, 10, tzinfo=UTC)), "h": 6.0, "k": "High"},
    ]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="tides", title="Tides", cadence_s=86400, rows_output_name="rows")
    assert rec.kind == "events"
    assert [f.path for f in rec.fields] == ["t", "h", "k"]           # the row keys the pipeline wrote
    assert [f.name for f in rec.fields] == ["time", "height", "tide"]  # slugs of the declared labels
    roles = {f.name: f.role for f in rec.fields}
    assert roles == {"time": "time", "height": "value", "tide": "name"}   # the only text names the rows
    assert rec.rows[0] == ["2026-10-06T05:40:00Z", 5.5, "High"]


def test_list_cell_key_overrides_the_path_for_zipped_columns() -> None:
    """Columns answers are zipped under slug keys; the ``key`` names the row key."""
    answer = {"kind": "columns", "label": "Hourly", "axis": {"cell": "hourly.time", "step": "hour"},
              "cells": [{"path": "hourly.time", "key": "time", "type": "time", "label": "Time"},
                        {"path": "hourly.temperature_2m", "key": "temp", "type": "number",
                         "label": "Temp", "unit": "°F"}]}
    outputs = {"rows": [{"time": _shown("1 AM", datetime(2026, 10, 6, 5, 0, tzinfo=UTC)), "temp": 65.5},
                        {"time": _shown("2 AM", datetime(2026, 10, 6, 6, 0, tzinfo=UTC)), "temp": 65.2}]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="temp", title="Temp", cadence_s=900, rows_output_name="rows")
    assert rec.kind == "series"
    assert [f.path for f in rec.fields] == ["time", "temp"]
    assert next(f.unit for f in rec.fields if f.name == "temp") == "degF"


def test_list_name_is_the_column_that_tells_rows_apart() -> None:
    """A repeating word (a state) never names the rows when another text column does."""
    answer = _list(cells=[{"path": "level", "type": "text", "label": "Match level"},
                           {"path": "title", "type": "text", "label": "Title"}])
    outputs = {"rows": [{"level": "none", "title": "Make your first edit"},
                        {"level": "none", "title": "Show HN: a tiny thing"},
                        {"level": "none", "title": "Why the sky is blue"}]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="top stories", title="HN", cadence_s=900, rows_output_name="rows")
    roles = {f.name: f.role for f in rec.fields}
    assert roles == {"match_level": "kind", "title": "name"}


def test_list_records_without_axis_or_time() -> None:
    answer = _list(cells=[{"path": "name", "type": "text", "label": "Name"},
                           {"path": "pts", "type": "number", "label": "Points"},
                           {"path": "st", "type": "text", "label": "Status"},
                           {"path": "note", "type": "text", "label": "Note"}])
    outputs = {"rows": [
        {"name": "Fay", "pts": 40, "st": "TS", "note": "a"},
        {"name": "Odalys", "pts": 45, "st": "TS", "note": "b"},
        {"name": "Polo", "pts": 75, "st": "HU", "note": "c"},
    ]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="storms", title="Storms", cadence_s=900, rows_output_name="rows")
    assert rec.kind == "records"
    roles = {f.name: f.role for f in rec.fields}
    assert roles == {"name": "name", "points": "value", "status": "status", "note": "kind"}


def test_single_row_list_becomes_a_measure_leading_with_the_asked_field() -> None:
    """Fix round 1a-6, class H: a day/sunrise/sunset columns answer cut to tomorrow's one
    row reads like a value answer (measure) — the asked field leads, never the day."""
    answer = {"kind": "columns", "label": "Sunrise and sunset by day",
              "cells": [{"path": "daily.time", "key": "day", "type": "date", "label": "Day"},
                        {"path": "daily.sunrise", "key": "sunrise", "type": "time", "label": "Sunrise"},
                        {"path": "daily.sunset", "key": "sunset", "type": "time", "label": "Sunset"}],
              "axis": {"cell": "daily.time", "step": "day"}}
    outputs = {"rows": [{"day": _shown("Thu Oct 8", datetime(2026, 10, 8, tzinfo=UTC)),
                        "sunrise": _shown("6:49 am", datetime(2026, 10, 8, 10, 49, tzinfo=UTC)),
                        "sunset": _shown("6:13 pm", datetime(2026, 10, 8, 22, 13, tzinfo=UTC))}]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="what time is sunrise in Boston tomorrow", title="sunrise",
                          cadence_s=900, rows_output_name="rows")
    assert rec.kind == "measure" and len(rec.rows) == 1
    roles = {f.name: f.role for f in rec.fields}
    assert roles == {"day": "secondary", "sunrise": "measure", "sunset": "secondary"}


def test_single_row_list_with_no_asked_field_leads_with_the_first_displayable() -> None:
    """No field matches the ask's words: the first displayable field leads (unchanged
    from the value-answer rule)."""
    answer = _list(cells=[{"path": "a", "type": "text", "label": "Alpha"},
                           {"path": "b", "type": "number", "label": "Beta"}])
    rec, _ = from_answers([answer], {"rows": [{"a": "x", "b": 5}]}, history=None, context=_CTX,
                          ask="status", title="t", cadence_s=0, rows_output_name="rows")
    assert rec.kind == "measure"
    assert next(f.role for f in rec.fields if f.name == "alpha") == "measure"


def test_single_row_list_never_treats_a_long_cell_as_a_passage() -> None:
    """Fix round 1a-6, class H: a matchup name over 36 chars is row data, never a passage
    ('Washington Capitals vs Pittsburgh Penguins' misfired as text_brief) — the asked
    field (the ask said 'game') still leads."""
    answer = _list(cells=[{"path": "g", "type": "text", "label": "Game"},
                          {"path": "v", "type": "text", "label": "Venue"}])
    outputs = {"rows": [{"g": "Washington Capitals vs Pittsburgh Penguins", "v": "Capital One Arena"}]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="NHL games tonight", title="t", cadence_s=0, rows_output_name="rows")
    assert rec.kind == "measure"
    roles = {f.name: f.role for f in rec.fields}
    assert roles == {"game": "measure", "venue": "secondary"}


def test_from_spec_measure_over_rows_reads_only_row_zero() -> None:
    """``record.rows`` names the pipeline output; ``from_spec`` reads row 0 of it, never
    every row (a measure caps at 1 row)."""
    spec = {"kind": "measure", "rows": "rows",
            "fields": [{"name": "sunrise", "label": "Sunrise", "path": "sunrise", "type": "datetime",
                        "role": "measure"}]}
    one = from_spec(spec, {"rows": [{"sunrise": "2026-10-08T10:49:00Z"}]}, history=None, context=_SPEC_CTX)[0]
    assert one.rows == [["2026-10-08T10:49:00Z"]]
    two = from_spec(spec, {"rows": [{"sunrise": "a"}, {"sunrise": "b"}]}, history=None, context=_SPEC_CTX)[0]
    assert len(two.rows) == 1   # never 2 — check_record caps a measure at 1 row
    empty = from_spec(spec, {"rows": []}, history=None, context=_SPEC_CTX)[0]
    assert empty.rows == []


def test_columns_records_without_axis() -> None:
    answer = {"kind": "columns", "label": "Rates",
              "cells": [{"path": "cur", "type": "text", "label": "Currency"},
                        {"path": "rate", "type": "number", "label": "Rate"}]}
    outputs = {"rows": [{"cur": "EUR", "rate": 0.9}, {"cur": "GBP", "rate": 0.75}]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="rates", title="Rates", cadence_s=86400, rows_output_name="rows")
    assert rec.kind == "records"


def test_date_cells_keep_the_calendar_day_from_the_moment() -> None:
    answer = _list(axis={"cell": "d", "step": "day"},
                   cells=[{"path": "d", "type": "date", "label": "Day"},
                          {"path": "hi", "type": "number", "label": "High", "unit": "°F"}])
    ny = ZoneInfo("America/New_York")
    outputs = {"rows": [{"d": _shown("Tue Oct 6", datetime(2026, 10, 6, tzinfo=ny)), "hi": 78.1},
                        {"d": _shown("Wed Oct 7", datetime(2026, 10, 7, tzinfo=ny)), "hi": 74.0}]}
    rec, _ = from_answers([answer], outputs, history=None, context=_CTX,
                          ask="forecast", title="Forecast", cadence_s=3600, rows_output_name="rows")
    assert rec.fields[0].type == "date" and rec.fields[0].role == "date"
    assert [r[0] for r in rec.rows] == ["2026-10-06", "2026-10-07"]


def test_zoneless_time_text_is_never_guessed() -> None:
    """A bare string with no instant attached and no zone is not a datetime cell."""
    answers = [_value(name="t", type="time", unit=None)]
    rec, _ = from_answers(answers, {"t": "2026-10-06T18:21:00"}, history=None,
                          context=_CTX, ask="when", title="t", cadence_s=0)
    assert rec.fields[0].type == "datetime" and rec.rows == [[None]]


def test_history_lifts_series_part_under_the_track_key() -> None:
    history = {history_key("price"): [{"t": "2026-10-05T18:00:00Z", "v": 220.0},
                                      {"t": "2026-10-06T18:00:00Z", "v": 223.86}]}
    rec, _ = from_answers([_value()], {"price": 223.86}, history=history, context=_CTX,
                          ask="price", title="NVDA", cadence_s=1800)
    part = rec.parts["series"]
    assert part.kind == "series" and len(part.rows) == 2 and part.rows[0][1] == 220.0
    assert history_key("price") == "price_h"   # never the output's own name (§11 collision rule)


def test_source_zone_output_sets_the_card_zone() -> None:
    rec, _ = from_answers([_value()], {"price": 5, "zone": "America/Denver"}, history=None,
                          context=_CTX, ask="a", title="t", cadence_s=0)
    assert (rec.context.card_tz, rec.context.card_tz_src) == ("America/Denver", "data")


def test_source_host_empty_when_url_absent() -> None:
    rec, inp = from_answers([_value()], {"price": 5}, history=None, context={
        "fetched_at": "2026-10-06T12:00:00Z"}, ask="a", title="t", cadence_s=0)
    assert rec.context.source_host == "" and inp.source_url is None
    assert rec.context.card_tz == "UTC"


def test_long_text_cells_are_capped_with_the_full_text_kept() -> None:
    long = "x" * 300
    rec, _ = from_answers([_value(name="reading", type="text", unit=None)], {"reading": long},
                          history=None, context=_CTX, ask="a", title="t", cadence_s=0)
    assert len(rec.rows[0][0]) <= 120 and rec.long_text["0,0"] == long


def test_unknown_answer_kind_raises() -> None:
    with pytest.raises(ValueError):
        from_answers([{"kind": "nope", "name": "x", "type": "number"}], {}, history=None,
                     context=_CTX, ask="a", title="t", cadence_s=0)


# ---- from_spec: the sealed specs are the truth ----------------------------------

_SPEC_CTX = {**_CTX, "title": "NVDA", "ask": "price of NVDA", "cadence_s": 900}


def test_from_spec_keeps_a_sealed_text_type_the_sample_would_call_category() -> None:
    """One distinct value would derive ``category``; the seal says ``text`` and wins."""
    spec = {"kind": "measure", "rows": None,
            "fields": [{"name": "label", "label": "Label", "path": "label", "type": "text",
                        "role": "measure"}]}
    rec, inp = from_spec(spec, {"label": "On time"}, history=None, context=_SPEC_CTX)
    assert rec.fields[0].type == "text" and rec.rows == [["On time"]]
    assert inp.title == "NVDA" and inp.cadence_s == 900 and inp.ask == "price of NVDA"


def test_from_spec_keeps_a_sealed_category_over_many_distinct_values() -> None:
    spec = {"kind": "records", "rows": "rows",
            "fields": [{"name": "name", "label": "Name", "path": "name", "type": "category",
                        "role": "name"},
                       {"name": "n", "label": "N", "path": "n", "type": "number", "role": "value"}]}
    rows = [{"name": f"item {i}", "n": i} for i in range(30)]
    rec, _ = from_spec(spec, {"rows": rows}, history=None, context=_SPEC_CTX)
    assert rec.fields[0].type == "category" and len(rec.rows) == 30


def test_from_spec_reads_rows_by_path_and_leaves_sparse_cells_empty() -> None:
    spec = {"kind": "records", "rows": "rows",
            "fields": [{"name": "team", "label": "Team", "path": "team", "type": "text", "role": "name"},
                       {"name": "runs", "label": "Runs", "path": "score.runs", "type": "number",
                        "role": "value"}]}
    rows = [{"team": "Yankees"}, {"team": "Orioles", "score": {"runs": 10}}]
    rec, _ = from_spec(spec, {"rows": rows}, history=None, context=_SPEC_CTX)
    assert rec.rows == [["Yankees", None], ["Orioles", 10]]


def test_from_spec_value_record_reads_outputs_by_field_path() -> None:
    spec = {"kind": "measure", "rows": None,
            "fields": [{"name": "price", "label": "Price", "path": "price", "type": "currency",
                        "role": "measure", "currency": "USD", "precision": 2},
                       {"name": "as_of", "label": "As of", "path": "as_of", "type": "datetime",
                        "role": "as_of"}]}
    outputs = {"price": "224.10", "as_of": _shown("2:21 PM", datetime(2026, 10, 6, 18, 21, tzinfo=UTC))}
    rec, _ = from_spec(spec, outputs, history=None, context=_SPEC_CTX)
    assert rec.rows == [[224.1, "2026-10-06T18:21:00Z"]]
    assert rec.fields[0].precision == 2 and rec.context.as_of == "2026-10-06T18:21:00Z"
