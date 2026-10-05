"""fix14-bbox (live 2026-10-05): never a confidently wrong state earthquake card.

Two class fixes, each deterministic (no network, no model):

* Defect 1 — a US state's bounding box covers neighbors; the USGS state feed on
  "earthquakes in California" shipped rows "22 km NNE of Yerington, Nevada" because
  western Nevada sits inside California's box. A bbox-filled place-scoped source now
  rides a sealed ``state_scope`` filter — rows naming ONLY other US states drop; rows
  naming the asked state (or no state) ride on; border rows that also name the asked
  state ("Nevada-California border region") ride on. Empty rows show the honest
  may-be-empty line ("No Earthquakes right now").
* Defect 2 — "earthquakes in California today" shipped Sat/Sun rows because the USGS
  answer declares no axis for its ``properties.time`` cell (USGS writes epoch ms).
  A list that carries exactly one time / date cell but no declared axis now rides an
  inferred ``hour`` axis when the ask carries a window, so a "today" cut narrows to
  today's rows in the user's zone. A no-window ask ("earthquakes in Alaska", "latest
  earthquakes") stays unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from smartbrain_3000 import ni, ni_flow

# --- defect 1: bbox state-scope row filter -------------------------------------------------

_USGS_STATE_ANSWER = {
    "name": "quakes", "label": "Earthquakes", "words": ["earthquakes", "quakes"],
    "primary": True, "kind": "list", "path": "features",
    "cells": [
        {"path": "properties.mag", "label": "Magnitude", "type": "number"},
        {"path": "properties.place", "label": "Place", "type": "text"},
        {"path": "properties.time", "label": "Time", "type": "time"},
    ],
    "may_be_empty": True,
}
_USGS_STATE_SAMPLE = {"features": [
    {"properties": {"mag": 2.82, "place": "22 km NNE of Yerington, Nevada",
                      "time": 1759878900000}},
    {"properties": {"mag": 2.65, "place": "58 km NNW of Rachel, Nevada",
                      "time": 1759714680000}},
    {"properties": {"mag": 3.1, "place": "6 km W of Lee Vining, California",
                      "time": 1759791600000}},
    {"properties": {"mag": 3.4, "place": "offshore Northern California",
                      "time": 1759856400000}},
    {"properties": {"mag": 2.9, "place": "Nevada-California border region",
                      "time": 1759795200000}},
]}


def test_state_scope_filter_drops_rows_naming_only_other_states() -> None:
    """"earthquakes in California": rows named ", Nevada" drop, rows named ", California" or
    "Northern California" ride on, border rows that name both ride on."""
    scoped = ni_flow._scope_rows_to_state_bbox(_USGS_STATE_ANSWER, "California",
                                                 "earthquakes in california")
    assert scoped["state_scope"] == {"path": "properties.place", "code": "CA"}
    built = ni_flow.build_from_answers([scoped], _USGS_STATE_SAMPLE, "California earthquakes")
    places = [row["properties"]["place"] for row in built["preview_payload"]["rows"]]
    assert places == [
        "6 km W of Lee Vining, California",
        "offshore Northern California",
        "Nevada-California border region",
    ]


def test_state_scope_filter_rides_the_sealed_pipeline() -> None:
    """The pipeline carries a ``where`` op with ``state_scope`` so every refresh keeps the
    row test honest — the engine re-reads the response on every tick."""
    scoped = ni_flow._scope_rows_to_state_bbox(_USGS_STATE_ANSWER, "California",
                                                 "earthquakes in california")
    built = ni_flow.build_from_answers([scoped], _USGS_STATE_SAMPLE, "California")
    where = next(op for st in built["pipeline"] if st.get("op") == "transform"
                   for op in st["apply"] if op["fn"] == "where")
    assert where == {"fn": "where", "field": "rows", "key": "properties.place",
                       "op": "state_scope", "value": "CA"}


def test_state_scope_filter_empties_an_asked_state_with_nothing_today() -> None:
    """A California ask against a sample that holds only Nevada rows → rows empty, the
    ``may_be_empty`` scene child says "No Earthquakes right now" (never a Nevada card)."""
    nevada_only = {"features": [
        {"properties": {"mag": 2.82, "place": "22 km NNE of Yerington, Nevada",
                          "time": 1759878900000}},
        {"properties": {"mag": 2.65, "place": "58 km NNW of Rachel, Nevada",
                          "time": 1759714680000}},
    ]}
    scoped = ni_flow._scope_rows_to_state_bbox(_USGS_STATE_ANSWER, "California",
                                                 "earthquakes in california")
    built = ni_flow.build_from_answers([scoped], nevada_only, "California")
    assert built["preview_payload"]["rows"] == []


def test_state_scope_filter_skipped_without_a_place_cell() -> None:
    """A list that doesn't declare a place cell is left alone — the row filter needs a text
    column to read."""
    plain = {"name": "x", "label": "X", "words": [], "primary": True, "kind": "list",
             "path": "features", "cells": [{"path": "properties.mag", "label": "Magnitude",
                                              "type": "number"}]}
    assert ni_flow._scope_rows_to_state_bbox(plain, "California", "x") is plain


def test_rows_need_state_scope_fires_only_for_bbox_place_scope() -> None:
    """A bbox-filled place-scoped source → True. A standard geo-param (``lat``/``lon``) place
    scope → False (the data already names one point). A global source → False."""
    bbox_params = {"min_lat": "32.5", "max_lat": "42.0", "min_lon": "-124.5",
                     "max_lon": "-114.1", "min_mag": "2.5", "start": "2026-09-05"}
    assert ni_flow._rows_need_state_scope({"_library_scope": "place"}, bbox_params) is True
    assert ni_flow._rows_need_state_scope({"_library_scope": "global"}, bbox_params) is False
    near_params = {"lat": "37.77", "lon": "-122.42", "radius_km": "200"}
    assert ni_flow._rows_need_state_scope({"_library_scope": "place"}, near_params) is False


# --- defect 2: axis inference for an event list on a window --------------------------------

def test_today_window_cuts_an_event_list_without_a_declared_axis(monkeypatch) -> None:
    """"earthquakes in California today": the USGS answer declares no axis, but the single
    time cell rides as an inferred hour axis so a ``today`` cut keeps today's rows."""
    la = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 10, 5, 10, 0, tzinfo=la)
    monkeypatch.setattr(ni, "_clock", lambda: now)
    today_ms = int(datetime(2026, 10, 5, 9, 55, tzinfo=la).astimezone(UTC).timestamp() * 1000)
    past_ms = int(datetime(2026, 10, 3, 12, 47, tzinfo=la).astimezone(UTC).timestamp() * 1000)
    sample = {"features": [
        {"properties": {"mag": 2.5, "place": "6 km W of Lee Vining, California",
                          "time": today_ms}},
        {"properties": {"mag": 2.3, "place": "offshore Northern California",
                          "time": past_ms}},
    ]}
    scoped = ni_flow._scope_rows_to_state_bbox(_USGS_STATE_ANSWER, "California", "earthquakes")
    built = ni_flow.build_from_answers([scoped], sample, "California", window="today")
    places = [row["properties"]["place"] for row in built["preview_payload"]["rows"]]
    assert places == ["6 km W of Lee Vining, California"]


def test_no_window_keeps_the_recent_list_unchanged() -> None:
    """"earthquakes in California" (no window, no "today"): the list rides unchanged —
    axis inference only fires when a window is asked."""
    scoped = ni_flow._scope_rows_to_state_bbox(_USGS_STATE_ANSWER, "California", "earthquakes")
    built = ni_flow.build_from_answers([scoped], _USGS_STATE_SAMPLE, "California")
    assert any(op.get("fn") == "window" for st in built["pipeline"] if st.get("op") == "transform"
                 for op in st["apply"]) is False


def test_inferred_axis_skips_a_list_with_more_than_one_time_cell() -> None:
    """A list with two time cells is ambiguous — the pack declares which indexes the rows
    (axis). The helper returns None so no cut is applied."""
    cells = [{"path": "t1", "label": "Start", "type": "time"},
             {"path": "t2", "label": "End", "type": "time"}]
    assert ni_flow._inferred_axis(cells, "today") is None


def test_inferred_axis_skips_when_no_window_is_asked() -> None:
    """No window means nothing to cut — the inferred axis stays off so a "latest" ask
    keeps the recent list."""
    cells = [{"path": "properties.time", "label": "Time", "type": "time"}]
    assert ni_flow._inferred_axis(cells, None) is None


# --- engine: state_scope op semantics ------------------------------------------------------

def test_state_scope_engine_keeps_asked_state_and_stateless_rows() -> None:
    """The engine's row test: text that names the asked state (full name or ", CODE") OR
    names no US state at all rides on; text that names only other US states drops."""
    assert ni._state_scope_keeps("6 km W of Lee Vining, California", "CA") is True
    assert ni._state_scope_keeps("offshore Northern California", "CA") is True
    assert ni._state_scope_keeps("Nevada-California border region", "CA") is True
    assert ni._state_scope_keeps("22 km NNE of Yerington, Nevada", "CA") is False
    assert ni._state_scope_keeps("58 km NNW of Rachel, NV", "CA") is False
    assert ni._state_scope_keeps("16 km from Mazatlan, Mexico", "CA") is True
