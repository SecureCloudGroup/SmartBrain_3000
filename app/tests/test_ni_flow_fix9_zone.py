"""fix9-zone (2026-10-04): a source that names its zone at the top (Open-Meteo ``timezone`` /
``utc_offset_seconds``) rides it onto the ``time`` transform so a zoneless cell is anchored in the
source's zone — a "sunset 18:36" declared in Denver reads at 20:13 EDT as 20:36 EDT, not a past
18:36 EDT. The stale-first check in ``_frame_gap`` then ships the right card.

Live defect: "when is sunset in Denver" at 20:13 EDT (= 18:13 MDT) silently dropped the
``open-meteo-sun`` row because the moment was read in the user's zone, past 20:13 EDT by 2 hours.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow

DENVER = ZoneInfo("America/Denver")
NY = ZoneInfo("America/New_York")

_FIXTURE = Path.home() / "SmartBrain-eval" / "gate" / "fixtures" / "openmeteo_sun_denver.json"


def _freeze(monkeypatch, moment: datetime) -> None:
    assert moment.tzinfo is not None, "moment must be aware"
    monkeypatch.setattr(nimod, "_clock", lambda: moment)


def _sun_payload() -> dict:
    assert _FIXTURE.exists(), f"missing fixture at {_FIXTURE}"
    return json.loads(_FIXTURE.read_text())


# ---- _time_moment honors a declared zone ----------------------------------------------------

def test_time_moment_zoneless_anchors_in_declared_zone(monkeypatch) -> None:
    """A zoneless ISO value anchors in the ``zone`` argument when given; falls back to user zone."""
    from datetime import UTC
    _freeze(monkeypatch, datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    moment = nimod._time_moment("2026-10-04T18:36", zone=DENVER)
    assert moment.utcoffset() == DENVER.utcoffset(moment)
    assert moment.hour == 18 and moment.minute == 36  # Denver wall clock, as written
    # the user-zone fallback (no zone argument) attaches NY, which is wrong for a Denver source
    moment_user = nimod._time_moment("2026-10-04T18:36")
    assert moment_user.utcoffset() == NY.utcoffset(moment_user)
    # a UTC flag still wins (zone ignored); the UTC instant is 18:36 Z
    moment_utc = nimod._time_moment("2026-10-04T18:36", naive_utc=True, zone=DENVER)
    assert moment_utc.astimezone(UTC).hour == 18
    assert moment_utc.astimezone(UTC).minute == 36


# ---- _txf_time reads the zone from the op's named top-level output --------------------------

def test_txf_time_reads_declared_zone_from_payload(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    out = nimod.run_pipeline(
        [{"op": "extract", "paths": {"sunset": "daily.sunset[0]", "zone": "timezone"}},
         {"op": "transform", "apply": [{"fn": "time", "field": "sunset", "zone": "zone"}]}],
        _sun_payload(),
    )
    assert isinstance(out["sunset"], str)
    assert "6:36 PM" in out["sunset"]
    moment = out["sunset"].moment
    # 18:36 Denver (MDT, -06:00) == 20:36 EDT
    assert moment.hour == 18 and moment.minute == 36
    assert moment.tzinfo is not None and moment.utcoffset().total_seconds() == -6 * 3600


# ---- _build_value_answers rides the source's zone onto the time op --------------------------

def test_value_answers_sunset_ships_at_2013_edt(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    answer = {"name": "sunset_today", "label": "Sunset today",
              "kind": "value", "primary": True, "type": "time",
              "path": "daily.sunset[0]", "words": ["sunset"], "window": "today",
              "measure": "sunset"}
    built = ni_flow.build_from_answers([answer], _sun_payload(), "Sunset today",
                                        next_event=True, frame_kind="next_event")
    # the moment is anchored in Denver, so the stale-first check holds
    frame_gap = ni_flow._frame_gap("next_event", [answer], built["preview_payload"],
                                    datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    assert frame_gap is None, f"gap at 20:13 EDT must be None, got {frame_gap!r}"
    # the shown text is clock-only ("6:36 PM" today)
    assert "6:36 PM" in built["preview_payload"]["sunset_today"]


def test_value_answers_sunset_refuses_well_past_dusk(monkeypatch) -> None:
    """Well past dusk (19:00 MDT, 24 minutes past the 18:36 sunset) the stale-first check
    (grace = 15 minutes) refuses the card as the existing C9 rule intends."""
    _freeze(monkeypatch, datetime(2026, 10, 4, 19, 0, tzinfo=DENVER))
    answer = {"name": "sunset_today", "label": "Sunset today",
              "kind": "value", "primary": True, "type": "time",
              "path": "daily.sunset[0]", "words": ["sunset"], "window": "today",
              "measure": "sunset"}
    built = ni_flow.build_from_answers([answer], _sun_payload(), "Sunset today",
                                        next_event=True, frame_kind="next_event")
    frame_gap = ni_flow._frame_gap("next_event", [answer], built["preview_payload"],
                                    datetime(2026, 10, 4, 19, 0, tzinfo=DENVER))
    assert frame_gap == "its next time has already passed", f"unexpected: {frame_gap!r}"


# ---- siblings are unchanged -----------------------------------------------------------------

def test_source_without_declared_zone_is_user_zone(monkeypatch) -> None:
    """A tide / NWS source that doesn't declare ``timezone`` / ``utc_offset_seconds`` keeps the
    pre-fix behavior: a zoneless row is read on the user's wall clock."""
    _freeze(monkeypatch, datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    answer = {"name": "next_tide", "label": "Next tide",
              "kind": "value", "primary": True, "type": "time",
              "path": "predictions[0].t", "words": ["tide"],
              "measure": "tide_time"}
    payload = {"predictions": [{"t": "2026-10-04 21:30", "v": "1.2", "type": "H"}]}
    built = ni_flow.build_from_answers([answer], payload, "Next tide",
                                        next_event=True, frame_kind="next_event")
    assert ni_flow._frame_gap("next_event", [answer], built["preview_payload"],
                               datetime(2026, 10, 4, 20, 13, tzinfo=NY)) is None


def test_utc_declared_times_still_utc(monkeypatch) -> None:
    """A source whose zoneless times are UTC (TheSportsDB ``strTimestamp``) keeps the UTC path —
    ``zone`` can't override an explicit ``utc: true``."""
    from datetime import UTC
    _freeze(monkeypatch, datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    out = nimod.run_pipeline(
        [{"op": "extract", "paths": {"start": "strTimestamp", "zone": "timezone"}},
         {"op": "transform", "apply": [
             {"fn": "time", "field": "start", "utc": True, "zone": "zone"}]}],
        {"strTimestamp": "2026-10-05T00:30:00", "timezone": "America/Denver"},
    )
    # 00:30 UTC: utc wins over zone (the moment's UTC instant is 00:30 Z)
    assert isinstance(out["start"], str)
    moment = out["start"].moment
    assert moment.astimezone(UTC).hour == 0
    assert moment.astimezone(UTC).minute == 30


def test_zone_validator_accepts_time_zone_field() -> None:
    """``zone`` on a ``time`` op must be accepted; a non-string zone is refused."""
    outputs: set = set()
    nimod._validate_transform_op(
        {"fn": "time", "field": "x", "zone": "zone"}, 0, 0, outputs)
    try:
        nimod._validate_transform_op(
            {"fn": "time", "field": "x", "zone": 42}, 0, 0, outputs)
    except ValueError as exc:
        assert "zone" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for malformed zone")


def test_rows_cell_time_rides_declared_zone(monkeypatch) -> None:
    """A columns / list answer gets the same zone treatment — the cell time op carries
    ``zone: 'zone'`` and the first extract stage pulls the top-level ``timezone``."""
    _freeze(monkeypatch, datetime(2026, 10, 4, 20, 13, tzinfo=NY))
    answer = {"name": "sun_week", "label": "Sun by day",
              "kind": "columns", "primary": False,
              "cells": [{"path": "daily.time", "label": "Day", "type": "date"},
                        {"path": "daily.sunset", "label": "Sunset", "type": "time"}]}
    built = ni_flow.build_from_answers([answer], _sun_payload(), "Sun by day")
    first_stage = built["pipeline"][0]
    assert first_stage["paths"].get("zone") in ("timezone", "utc_offset_seconds")
    ops = built["pipeline"][1]["apply"]
    time_ops = [o for o in ops if o.get("fn") == "time"]
    assert time_ops and all(o.get("zone") == "zone" for o in time_ops)


def test_pause_with_web_names_why_the_library_row_dropped() -> None:
    """A silent-drop ("the Library has no source for this") used to bury the reason the previously
    tapped Library row failed. ``_pause_with_web`` now reads ``drop_why`` and prefixes the pause
    note with it so every move-on says why."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000.secrets import gen_master_key

    class _Service:
        def search(self, _request):
            return [{"title": "A page", "host": "example.org",
                      "url": "https://example.org/a", "snippet": "x"}]

    class _NoopEval:
        def __call__(self, rows, _intent, _request):
            return [{**r, "fitness": 1, "evidence": ["x"]} for r in rows]

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    item_id = ni_flow.create_shell_item(store, "when is sunset in Denver")
    ni_flow._flow_write(store, item_id,
                        ni_flow._make_record("when is sunset in Denver", "source"))
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ni_flow, "_resolve_search_service", lambda: _Service())
        mp.setattr(ni_flow, "_s2_search_candidates", lambda _s, _r, _i: [
            {"title": "A page", "host": "example.org", "url": "https://example.org/a",
             "snippet": "x", "fitness": 1}])
        mp.setattr(ni_flow, "_s2_evaluate", _NoopEval())
        mp.setattr(ni_flow, "rank_web_rows", lambda _web, _r, _i, _cm: [0])
        pause = ni_flow._pause_with_web(store, item_id, "when is sunset in Denver",
                                         {}, lambda _p: "", drop_why="its next time has already passed")
    finally:
        mp.undo()
    assert pause is not None
    note = (pause.get("notes") or [""])[-1]
    assert "its next time has already passed" in note, f"missing why in: {note!r}"


def test_a_want_that_names_the_sources_own_category_is_answered() -> None:
    """'weather this weekend in Austin' on the NWS 7-day forecast was refused "it doesn't report
    weather" (live holdout 2026-10-04): the want named the source's own category. The words of its
    category / subcategory LABELS ("Weather & Air" / "Forecast") answer such a want; their keyword
    lists don't (a forecast must not pass for "tornado warnings")."""
    answers = [{"name": "periods", "label": "Forecast periods", "words": ["forecast", "rain chance"],
                "kind": "list", "primary": True, "cells": []}]
    kind = {"weather", "air", "forecast"}
    assert ni_flow._unanswered_wants(answers, "weather this weekend in Austin", ["weather"],
                                     ["Austin"], kind=kind) == []
    assert ni_flow._unanswered_wants(answers, "tornado warnings in Austin", ["tornado warnings"],
                                     ["Austin"], kind=kind) == ["tornado warnings"]
    assert ni_flow._unanswered_wants(answers, "weather this weekend in Austin", ["weather"],
                                     ["Austin"]) == ["weather"]  # without the kind: unchanged
