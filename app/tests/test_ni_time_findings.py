"""Review 2026-10-04 findings: dates, windows, time zones — the engine either reads the asked window
honestly or says so, including on day 2 and after.

Every test here is a reviewer finding ported from ``$R/review2/flow/tests`` into the app:

* F1  (test_r5): a clock-filled date in a Library URL is sealed as a ``{{param:name}}`` slot + a
  ``clock``-kind spec param — the engine fills it from the current clock on every refresh.
* F3  (test_r1 / test_r1b): the ``window`` transform carries the answer's ``utc`` flag — zoneless
  stamps declared UTC are UTC instants, read on the user's day.
* F5-time (test_r6): a next-event / schedule list on a time axis with no asked window gets a
  forward cut (``upcoming``) every run.
* F9  (test_r2): a row whose ``tbd_if`` flag is set is a DAY row on its written date — never
  windowed by the placeholder sentinel.
* F11 (test_r3): hour windows start at ``max(window start, current hour)``; ``tonight`` before 06:00
  is the current night (now..06:00), never the next evening.
* F15/F16: the engine's ``_clock`` reads the user's IANA zone (``ni.set_user_timezone``), so a
  Docker install with no TZ env still answers on the user's calendar; the tz is a real ZoneInfo
  (not a fixed offset) so day arithmetic across DST is honest."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000 import ni as nimod

CHI = ZoneInfo("America/Chicago")
LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")


def _freeze(monkeypatch, moment: datetime) -> None:
    assert moment.tzinfo is not None, "moment must be aware"
    monkeypatch.setattr(nimod, "_clock", lambda: moment)


# ---- F11: hour windows floor at the current hour --------------------------------------------

def test_f11_tonight_at_10pm_cuts_past_evening(monkeypatch) -> None:
    """A 10 PM reader of hour-stepped ``tonight`` sees from 10 PM, never 6 PM first."""
    _freeze(monkeypatch, datetime(2026, 9, 29, 22, 0, tzinfo=CHI))
    rows = [{"t": f"2026-09-29T{h:02d}:00"} for h in range(24)]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "tonight"}]}], {"rows": rows})
    kept = [r["t"] for r in out["rows"]]
    assert kept and kept[0] == "2026-09-29T22:00"
    assert "2026-09-29T18:00" not in kept


def test_f11_tonight_before_dawn_is_the_current_night(monkeypatch) -> None:
    """Read at 02:00 ``tonight`` means now..06:00 today, never the next evening (18..06+)."""
    _freeze(monkeypatch, datetime(2026, 9, 29, 2, 0, tzinfo=CHI))
    rows = [{"t": f"2026-09-29T{h:02d}:00"} for h in range(24)]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "tonight"}]}], {"rows": rows})
    assert [r["t"] for r in out["rows"]] == [f"2026-09-29T{h:02d}:00" for h in range(2, 6)]


def test_f11_today_on_day_rows_keeps_the_whole_day(monkeypatch) -> None:
    """A day-stepped list treats a timed cell as a date row — ``today`` keeps the whole day's rows
    even when some of them are already past (an MLB games-today ask)."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 21, 30, tzinfo=NY))
    rows = [{"g": "noon", "t": "2026-10-03T16:00:00Z"},
            {"g": "night", "t": "2026-10-04T01:40:00Z"}]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "today", "step": "day"}]}],
        {"rows": rows})
    assert [r["g"] for r in out["rows"]] == ["noon", "night"]


# ---- F3: window honors declared utc flag ----------------------------------------------------

def test_f3_tonight_in_la_keeps_utc_stamped_evening(monkeypatch) -> None:
    """A row written ``2026-10-04T03:00:00`` (zoneless, declared UTC) is 8:00 PM PDT on 10/3 —
    ``tonight`` in LA keeps it, not the raw wall-UTC morning."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 11, 0, tzinfo=LA))
    rows = [{"t": "2026-10-03T18:00:00"},  # 11 AM PDT — before tonight
            {"t": "2026-10-04T03:00:00"},  # 8 PM PDT — in tonight
            {"t": "2026-10-04T12:00:00"},  # 5 AM PDT next day — in tonight (near end)
            {"t": "2026-10-04T15:00:00"}]  # 8 AM PDT next day — after
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "tonight", "utc": True}]}],
        {"rows": rows})
    assert [r["t"] for r in out["rows"]] == ["2026-10-04T03:00:00", "2026-10-04T12:00:00"]


def test_f3_window_without_utc_flag_reads_zoneless_as_wall(monkeypatch) -> None:
    """A zoneless stamp without the ``utc`` flag is the source's wall clock (the pre-fix path);
    the new flag is opt-in so older ops keep working byte-for-byte."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 20, 30, tzinfo=LA))
    rows = [{"t": "2026-10-03T18:00:00"}, {"t": "2026-10-03T20:00:00"}, {"t": "2026-10-03T22:00:00"}]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "tonight"}]}],
        {"rows": rows})
    assert [r["t"] for r in out["rows"]] == ["2026-10-03T20:00:00", "2026-10-03T22:00:00"]


# ---- F9: tbd_if row is a day row ------------------------------------------------------------

def test_f9_a_tbd_row_is_windowed_as_a_day_row(monkeypatch) -> None:
    """An MLB postseason TBD start carries a sentinel 07:33Z — the row's TBD flag demotes it to a
    DAY row on the written date, so ``tonight`` on an off day never shows tomorrow's game."""
    _freeze(monkeypatch, datetime(2026, 10, 5, 19, 0, tzinfo=LA))  # off day
    # The TBD game is Oct 6 (next day) with sentinel 07:33Z → 00:33 PDT 10/6.
    rows = [{"gameDate": "2026-10-06T07:33:00Z", "status": {"startTimeTBD": True}}]
    op = {"fn": "window", "field": "rows", "key": "gameDate", "window": "tonight",
          "unless": "status.startTimeTBD", "step": "day"}
    out = nimod.run_pipeline([{"op": "transform", "apply": [op]}], {"rows": rows})
    assert out["rows"] == []
    # Same TBD row on its own date is kept by "today"
    _freeze(monkeypatch, datetime(2026, 10, 6, 15, 0, tzinfo=LA))
    out = nimod.run_pipeline([{"op": "transform", "apply": [{**op, "window": "today"}]}], {"rows": rows})
    assert len(out["rows"]) == 1


# ---- F5-time: next_event list on a time axis gets a forward cut every run -------------------

def test_f5_time_upcoming_cuts_past_rows(monkeypatch) -> None:
    """``upcoming`` keeps rows at or past the current hour — a next_event schedule shown with no
    asked window never leads with a past final."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 15, 0, tzinfo=NY))
    rows = [{"t": "2026-09-27T19:10:00Z"},  # Sun past
            {"t": "2026-10-03T17:05:00Z"},  # today past
            {"t": "2026-10-03T23:40:00Z"},  # tonight future
            {"t": "2026-10-05T18:30:00Z"}]  # Mon future
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "upcoming", "step": "day"}]}],
        {"rows": rows})
    assert [r["t"] for r in out["rows"]] == ["2026-10-03T23:40:00Z", "2026-10-05T18:30:00Z"]


# ---- F1: a clock-filled date is NOT frozen into the sealed URL ------------------------------

def test_f1_a_clock_fill_seals_as_a_param_slot_not_a_literal() -> None:
    """F1 — library_resolve.candidate_urls emits a URL_template with ``{{param:start}}`` and
    clock_params metadata when the record's access.params declare a clock fill."""
    from smartbrain_3000.library_resolve import candidate_urls
    record = {
        "role": "",
        "access": {
            "url_template": "https://ex.test/data?team={team}&start={start}&end={end}",
            "params": [
                {"name": "team", "fill": {"from": "default", "value": "ATL"}},
                {"name": "start", "fill": {"from": "clock", "format": "%Y-%m-%d", "offset_days": -2}},
                {"name": "end", "fill": {"from": "clock", "format": "%Y-%m-%d", "offset_days": 7}}]}}
    now = datetime(2026, 10, 3, 12, 0, tzinfo=NY)
    urls, _why = candidate_urls(record, "atlanta today", {}, _NullResolver(), now)
    assert urls, "candidate must be offered"
    row = urls[0]
    # the filled URL (shown at consent) carries the current clock's dates (-2 .. +7)
    assert "start=2026-10-01" in row["url"] and "end=2026-10-10" in row["url"]
    # the sealed URL template keeps {{param:name}} slots for every clock fill
    assert "start={{param:start}}" in row["url_template"]
    assert "end={{param:end}}" in row["url_template"]
    assert set(row["clock_params"]) == {"start", "end"}


class _NullResolver:
    def water_spans(self, ask):
        return []

    def by_name(self, resolver, ask, *, many=False):
        return {"status": "none", "best": None, "candidates": [], "reason": ""}

    def near(self, *_a, **_k):
        return {"status": "none", "best": None, "candidates": [], "reason": ""}

    def source_record(self, _sid):
        return None


def test_f1_clock_kind_param_substitutes_the_current_clock(monkeypatch) -> None:
    """A sealed clock-kind param reads the engine's current clock on every substitute — day 2 of a
    live card re-fetches with day-2's date, never the creation day's literal."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    spec = {"params": {"start": {"label": "start", "kind": "clock", "format": "%Y-%m-%d",
                                   "offset_days": -2}},
            "source": {"url": "https://ex.test/?d={{param:start}}"}}
    assert nimod.substitute_params(spec)["source"]["url"] == "https://ex.test/?d=2026-10-01"
    # a day later the SAME spec reads a different URL
    _freeze(monkeypatch, datetime(2026, 10, 4, 12, 0, tzinfo=NY))
    assert nimod.substitute_params(spec)["source"]["url"] == "https://ex.test/?d=2026-10-02"


def test_f1_clock_param_refuses_unknown_strftime_codes() -> None:
    """A sealed clock-kind format may only use the engine's closed strftime set — a Library pack
    that smuggles in a %Z or similar fails the store, not the engine's refresh."""
    with pytest.raises(ValueError, match="unknown codes"):
        nimod._validate_params({"d": {"label": "d", "kind": "clock",
                                        "format": "%Y-%Z", "offset_days": 0}})


def test_f1_clock_param_bounds_offset_days() -> None:
    with pytest.raises(ValueError, match="offset_days"):
        nimod._validate_params({"d": {"label": "d", "kind": "clock",
                                        "format": "%Y-%m-%d", "offset_days": 10_000}})


# ---- F15 / F16: the engine's clock knows the user's zone -------------------------------------

def test_f15_set_user_timezone_pins_the_engine_clock(monkeypatch) -> None:
    """``set_user_timezone`` pins ``_clock`` to the user's IANA zone (what the SPA reports via the
    health handshake); without it ``_clock`` falls back to ``astimezone``."""
    try:
        nimod.set_user_timezone("Asia/Tokyo")
        assert str(nimod._clock().tzinfo) == "Asia/Tokyo"
        nimod.set_user_timezone(None)
        assert str(nimod._clock().tzinfo) != "Asia/Tokyo"
    finally:
        nimod.set_user_timezone(None)


def test_f16_clock_uses_a_real_zone_not_a_fixed_offset() -> None:
    """F16 — a ZoneInfo is a REAL zone (DST-aware), not a fixed-offset timezone. Day arithmetic a
    few days ahead crossing a DST boundary stays honest."""
    try:
        nimod.set_user_timezone("America/Los_Angeles")
        now = nimod._clock()
        assert isinstance(now.tzinfo, ZoneInfo)
        # the stored class is ZoneInfo, not timezone(timedelta(...)); + a date-day doesn't drift
        plus_7 = (now + timedelta(days=7)).date()
        assert plus_7 == now.date() + timedelta(days=7)
    finally:
        nimod.set_user_timezone(None)


def test_f15_unknown_zone_clears_the_cache() -> None:
    try:
        nimod.set_user_timezone("Mars/Olympus_Mons")
        assert nimod._user_timezone is None
    finally:
        nimod.set_user_timezone(None)


# ---- R4-6: an event-list on an hour axis keeps the whole asked period --------------------------

def test_r4_6_window_op_list_kind_disables_the_hour_floor() -> None:
    """R5-4 (2026-10-04): ``_window_op`` with ``floor_hour=False`` keeps ``step="hour"`` and marks
    ``floor: false`` on the op; the engine then keeps the whole asked period while the dawn rule
    still fires (the earlier hour→period rewrite silently dropped that rule)."""
    from smartbrain_3000 import ni_flow

    op = ni_flow._window_op("t", "today", {}, [{"paths": {}}], step="hour", floor_hour=False)
    assert op["step"] == "hour"
    assert op["floor"] is False
    # the default (forecast series) still floors — no explicit flag
    op = ni_flow._window_op("t", "today", {}, [{"paths": {}}], step="hour")
    assert op["step"] == "hour"
    assert "floor" not in op


def test_r4_6_period_step_keeps_past_hour_rows_on_today(monkeypatch) -> None:
    """At 14:10 the ``period`` branch of ``_window_test`` keeps the 8:01 AM row — a tide list
    reading "tide times today" shows the morning low alongside the afternoon highs."""
    _freeze(monkeypatch, datetime(2026, 10, 4, 14, 10, tzinfo=NY))
    rows = [{"t": "2026-10-04T08:01:00"}, {"t": "2026-10-04T14:05:00"},
            {"t": "2026-10-04T20:20:00"}]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "today", "step": "period"}]}],
        {"rows": rows})
    assert [r["t"] for r in out["rows"]] == [
        "2026-10-04T08:01:00", "2026-10-04T14:05:00", "2026-10-04T20:20:00"]


def test_r4_6_frame_gap_skips_stale_first_on_today(monkeypatch) -> None:
    """A ``schedule`` ask with ``window="today"`` keeps past-today events — the stale-first check
    (which refuses a next_event whose first row has passed) must not fire on an explicit day
    window, so a 10 PM "MLB schedule today" still ships the Final games from earlier."""
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.ni import _TimeText

    now = datetime(2026, 9, 29, 22, 30, tzinfo=NY)
    _freeze(monkeypatch, now)
    chosen = [{"kind": "list", "name": "schedule", "label": "games", "cells": [{"type": "time"}],
                "axis": {"cell": "gameDate", "step": "hour"}}]
    # a stale moment inside a time-transform output — the same shape the engine builds every refresh
    stale_moment = datetime(2026, 9, 29, 17, 5, tzinfo=NY)
    stale = _TimeText("1:05 PM")
    stale.moment = stale_moment
    preview = {"rows": [{"t": stale}]}
    # with window=today: no gap (past-but-today is intentional)
    assert ni_flow._frame_gap("schedule", chosen, preview, now, window="today") is None
    # without window (ambient "next event"): the stale-first check still fires
    assert ni_flow._frame_gap("schedule", chosen, preview, now) == "its next time has already passed"


# ---- R4-9: slot-by-position swap even when day == month ----------------------------------------

def test_r4_9_derive_clock_template_binds_by_position_on_mm_eq_dd() -> None:
    """A URL like ``.../onthisday/events/10/10`` has two params that render the SAME value — the
    position-aligned swap binds each placeholder to its own slot instead of a value replace that
    collapses mm and dd into one."""
    from smartbrain_3000 import ni_flow

    class _Lib:
        def get(self, _sid):
            return {"access": {
                "url_template": "https://ex.test/onthisday/events/{m}/{d}",
                "params": [
                    {"name": "m", "fill": {"from": "clock", "format": "%m", "offset_days": 0}},
                    {"name": "d", "fill": {"from": "clock", "format": "%d", "offset_days": 0}}]},
                "kinds": []}

    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Lib())
        _freeze(monkeypatch, datetime(2026, 10, 10, 9, 0, tzinfo=NY))
        template, meta = ni_flow._derive_clock_template(
            "demo", "https://ex.test/onthisday/events/10/10",
            {"m": "10", "d": "10"})
        assert template == "https://ex.test/onthisday/events/{{param:m}}/{{param:d}}"
        assert set(meta) == {"m", "d"}
    finally:
        monkeypatch.undo()


# ---- R4-4: a Docker card rendered the literal in UTC — the upgrade tries multiple zones --------

def test_r4_4_upgrade_tries_server_zone_then_user_zone(monkeypatch) -> None:
    """A card created at 02:00 UTC = 7 PM PDT the previous day: the user-zone render returns
    yesterday's date, but the server (Docker) rendered today's. The upgrade tries UTC first and
    accepts whichever reproduces the stored literal byte-for-byte."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.secrets import gen_master_key

    class _Lib:
        def get(self, _sid):
            return {"access": {
                "url_template": "https://ex.test/schedule?date={date}",
                "params": [{"name": "date", "label": "date",
                              "fill": {"from": "clock", "format": "%Y-%m-%d",
                                        "offset_days": 0}}]}, "kinds": []}

    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Lib())
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    _freeze(monkeypatch, datetime(2026, 10, 4, 19, 0, tzinfo=LA))
    url = "https://ex.test/schedule?date=2026-10-05"  # Docker (UTC) rendered 10/5 at 02:00Z
    spec = {"version": 1, "title": "t", "goal": "g", "params": {},
             "source": {"type": "http_json", "url": url, "headers": {}},
             "pipeline": [], "scene": {"type": "stack", "dir": "v", "gap": "sm",
                                         "children": [{"type": "text", "value": "x",
                                                        "role": "title", "tone": "default",
                                                        "size": "md"}]},
             "display": {"size": "small"}, "contract": None,
             "repair_policy": {"l1": True, "l2_frontier": False}, "model": None}
    item_id = store.add_item(spec, {})
    ni_flow._flow_write(store, item_id, {"state": "ready", "request": "r",
                                          "updated_at": "2026-10-03T12:00:00Z",
                                          "notes": [], "_library_source": "demo"})
    from datetime import UTC
    item = dict(store.get_item(item_id))
    item["created_at"] = datetime(2026, 10, 5, 2, 0, tzinfo=UTC).isoformat()
    assert ni_flow.upgrade_pre_f1_literal_dates(store, item) is not None
    assert "{{param:date}}" in store.get_item(item_id)["spec"]["source"]["url"]


# ---- R4-5: the upgrade reslots the pipeline too ------------------------------------------------

def test_r4_5_upgrade_reslots_a_date_keyed_pipeline(monkeypatch) -> None:
    """A NEOWS-shape pipeline (``near_earth_objects["2026-10-01"]``) must have its creation-day
    literal swapped for the clock slot alongside the URL — otherwise day 2 extract-misses."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.secrets import gen_master_key

    class _Lib:
        def get(self, _sid):
            return {"access": {
                "url_template": "https://ex.test/feed?start_date={date}",
                "params": [{"name": "date", "label": "date",
                              "fill": {"from": "clock", "format": "%Y-%m-%d",
                                        "offset_days": 0}}]}, "kinds": []}

    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Lib())
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    _freeze(monkeypatch, datetime(2026, 10, 1, 15, 0, tzinfo=NY))
    url = "https://ex.test/feed?start_date=2026-10-01"
    pipeline = [{"op": "extract",
                   "paths": {"rows": 'near_earth_objects["2026-10-01"]'}}]
    spec = {"version": 1, "title": "t", "goal": "g", "params": {},
             "source": {"type": "http_json", "url": url, "headers": {}},
             "pipeline": pipeline,
             "scene": {"type": "stack", "dir": "v", "gap": "sm",
                         "children": [{"type": "text", "value": "x",
                                         "role": "title", "tone": "default", "size": "md"}]},
             "display": {"size": "small"}, "contract": None,
             "repair_policy": {"l1": True, "l2_frontier": False}, "model": None}
    item_id = store.add_item(spec, {})
    ni_flow._flow_write(store, item_id, {"state": "ready", "request": "r",
                                          "updated_at": "2026-10-01T12:00:00Z",
                                          "notes": [], "_library_source": "demo"})
    from datetime import UTC
    item = dict(store.get_item(item_id))
    item["created_at"] = datetime(2026, 10, 1, 19, 0, tzinfo=UTC).isoformat()
    assert ni_flow.upgrade_pre_f1_literal_dates(store, item) is not None
    sealed_pipe = store.get_item(item_id)["spec"]["pipeline"]
    assert sealed_pipe[0]["paths"]["rows"] == 'near_earth_objects["{{param:date}}"]'


# ---- R4-2: a build crossing midnight never crashes the C2 verify -------------------------------

def test_r4_2_handoff_verify_uses_fetch_now_not_live_clock(monkeypatch) -> None:
    """A build whose fetch runs at 23:59 and whose seal runs at 00:01 must still pass the C2
    verify — ``_handoff``'s ``fetch_now`` kwarg pins the substitute to the sampler's moment."""
    from smartbrain_3000 import ni_flow

    spec = {"params": {"d": {"label": "d", "kind": "clock", "format": "%Y-%m-%d",
                              "offset_days": 0}},
             "source": {"url": "https://ex.test/?d={{param:d}}"}}
    fetch_now = datetime(2026, 9, 28, 23, 59, 30, tzinfo=NY)
    # even if the live clock has advanced past midnight, the frozen fetch_now still reproduces
    # the fetch URL byte-for-byte
    _freeze(monkeypatch, datetime(2026, 9, 29, 0, 1, 0, tzinfo=NY))
    assert ni_flow._f1_render_url_at(spec, fetch_now) == "https://ex.test/?d=2026-09-28"


def test_r4_2_realign_url_rebuilds_from_template_at_now(monkeypatch) -> None:
    """A resumed build whose sealed ``_library_url`` is stale (yesterday's) is rebuilt from the
    template at the current clock before the fetch — the handoff verify at the same moment
    reproduces that rebuilt URL."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.secrets import gen_master_key

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    item_id = ni_flow.create_shell_item(store, "r")
    live = {"state": "sampling", "request": "r", "updated_at": "", "notes": [],
             "_library_source": "demo",
             "_library_url_template": "https://ex.test/?d={{param:d}}",
             "_library_clock_params": {"d": {"format": "%Y-%m-%d", "offset_days": 0,
                                               "label": "d"}}}
    ni_flow._flow_write(store, item_id, live)
    _freeze(monkeypatch, datetime(2026, 10, 10, 9, 0, tzinfo=NY))
    rebuilt = ni_flow._realign_url_to_now(store, item_id, live, "https://ex.test/?d=2026-10-09",
                                            nimod._clock())
    assert rebuilt == "https://ex.test/?d=2026-10-10"
    assert ni_flow._flow_read(store, item_id)["_library_url"] == rebuilt


# ---- R4-11: the mapping path's pipeline reslots clock-filled literals too ----------------------

def test_r4_11_reslot_runs_on_sealed_params_with_clock_kind(monkeypatch) -> None:
    """A pipeline whose extract-path or where-value holds a clock-filled literal is reslotted into
    the ``{{param:name}}`` slot form whenever the sealed spec params include a clock-kind entry
    — the shared ``_reslot_clock_params_in_pipeline`` the answers path already uses."""
    from smartbrain_3000 import ni_flow

    _freeze(monkeypatch, datetime(2026, 10, 5, 12, 0, tzinfo=NY))
    stages = [{"op": "extract",
                 "paths": {"rows": 'near_earth_objects["2026-10-05"]'}}]
    out = ni_flow._reslot_clock_params_in_pipeline(stages, {"d": "2026-10-05"},
                                                     frozenset({"d"}))
    assert out[0]["paths"]["rows"] == 'near_earth_objects["{{param:d}}"]'


# ---- R5-1: next_event + today keeps the forward floor + stale check -----------------------------

def test_r5_1_frame_gap_fires_stale_on_next_event_today(monkeypatch) -> None:
    """A next_event ask with window=today still refuses when the first shown moment is in the past
    — only a schedule / result list keeps past-today rows."""
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.ni import _TimeText

    now = datetime(2026, 10, 4, 22, 30, tzinfo=NY)
    _freeze(monkeypatch, now)
    chosen = [{"kind": "list", "name": "tides", "label": "tides",
                "cells": [{"type": "time"}], "axis": {"cell": "t", "step": "hour"}}]
    stale = _TimeText("8:01 AM")
    stale.moment = datetime(2026, 10, 4, 8, 1, tzinfo=NY)
    preview = {"rows": [{"t": stale}]}
    # next_event + today: stale check fires
    assert ni_flow._frame_gap("next_event", chosen, preview, now, window="today") \
        == "its next time has already passed"
    # schedule + today: the whole period, no gap
    assert ni_flow._frame_gap("schedule", chosen, preview, now, window="today") is None


def test_r5_1_build_rows_floor_hour_true_on_next_event_list(monkeypatch) -> None:
    """``_build_rows_answer`` floors a next_event LIST on today / tonight (R5-1): a 22:30 "next
    tide today" cuts the 8:01 AM row even though the answer is a list — a schedule list keeps it."""
    from smartbrain_3000 import ni_flow

    _freeze(monkeypatch, datetime(2026, 10, 4, 15, 0, tzinfo=NY))
    answer = {"kind": "list", "name": "tides", "label": "tides", "path": "predictions",
              "cells": [{"type": "time", "path": "t", "label": "time"},
                         {"type": "text", "path": "type", "label": "type"}],
              "axis": {"cell": "t", "step": "hour"}}
    payload = {"predictions": [
        {"t": "2026-10-04T08:01:00", "type": "L"},
        {"t": "2026-10-04T14:05:00", "type": "H"},
        {"t": "2026-10-04T20:20:00", "type": "L"}]}
    # schedule: all three rows kept (floor_hour=False, step=hour, no floor flag)
    sched = ni_flow._build_rows_answer(answer, payload, "tides", window="today",
                                        next_event=True, frame_kind="schedule")
    sched_times = [str(r["t"]) for r in sched["preview_payload"]["rows"]]
    assert any("8:01 AM" in t for t in sched_times), sched_times
    assert len(sched_times) == 3
    # next_event at 15:00: the 8:01 AM row drops (forward floor on today), 20:20 PM kept
    nxt = ni_flow._build_rows_answer(answer, payload, "tides", window="today",
                                      next_event=True, frame_kind="next_event")
    nxt_times = [str(r["t"]) for r in nxt["preview_payload"]["rows"]]
    assert not any("8:01 AM" in t for t in nxt_times), nxt_times


# ---- R5-4: floor flag preserves dawn rule --------------------------------------------------------

def test_r5_4_floor_false_tonight_before_dawn_keeps_dawn_rule(monkeypatch) -> None:
    """A list window ``tonight`` at 01:30 with ``floor: false`` STILL fires the dawn rule
    (now..06:00) — the earlier hour→period rewrite silently dropped it."""
    _freeze(monkeypatch, datetime(2026, 10, 5, 1, 30, tzinfo=NY))
    rows = [{"t": f"2026-10-05T{h:02d}:00:00"} for h in range(24)]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "tonight", "floor": False}]}],
        {"rows": rows})
    assert [r["t"] for r in out["rows"]] == [f"2026-10-05T{h:02d}:00:00" for h in range(1, 6)]


def test_r5_4_floor_false_today_keeps_past_today_rows(monkeypatch) -> None:
    """``today`` with ``floor: false`` on hour rows keeps the whole day (an event / schedule list
    at 22:30 still shows the 2 PM Final game)."""
    _freeze(monkeypatch, datetime(2026, 10, 4, 22, 30, tzinfo=NY))
    rows = [{"t": "2026-10-04T08:01:00"}, {"t": "2026-10-04T14:05:00"},
            {"t": "2026-10-04T20:20:00"}]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "today", "floor": False}]}],
        {"rows": rows})
    assert [r["t"] for r in out["rows"]] == [
        "2026-10-04T08:01:00", "2026-10-04T14:05:00", "2026-10-04T20:20:00"]


# ---- R5-2: midnight realign binds access + repick -----------------------------------------------

def test_r5_2_realign_rebuilds_access_url(monkeypatch) -> None:
    """A sealed ``_access`` tied to the pre-realign URL is rewritten to the realigned fetch URL —
    the handoff's access check and the fetcher both match the new URL."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.secrets import gen_master_key

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    iid = ni_flow.create_shell_item(store, "r")
    pre_url = "https://ex.test/?d=2026-10-09"
    live = {"state": "sampling", "request": "r", "updated_at": "", "notes": [],
             "_library_source": "demo",
             "_library_url": pre_url,
             "_library_url_template": "https://ex.test/?d={{param:d}}",
             "_library_clock_params": {"d": {"format": "%Y-%m-%d", "offset_days": 0,
                                               "label": "d"}},
             "_access": {"url": pre_url, "host": "ex.test", "provider": "p",
                          "key": {"in": "query", "name": "api_key", "prefix": "",
                                   "docs_url": ""}, "contact": False}}
    ni_flow._flow_write(store, iid, live)
    _freeze(monkeypatch, datetime(2026, 10, 10, 9, 0, tzinfo=NY))
    rebuilt = ni_flow._realign_url_to_now(store, iid, live, pre_url, nimod._clock())
    assert rebuilt == "https://ex.test/?d=2026-10-10"
    assert ni_flow._flow_read(store, iid)["_access"]["url"] == rebuilt


def test_r5_2_repick_without_matches_by_source_id(monkeypatch) -> None:
    """After a URL realign the ranked row's literal URL and ``_library_url`` differ by day —
    ``_repick_without`` must bind by sealed ``_library_source`` so a 403 after midnight still
    hands off to the next source."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.secrets import gen_master_key

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    iid = ni_flow.create_shell_item(store, "r")
    pre_url, post_url = "https://ex.test/?d=2026-10-09", "https://ex.test/?d=2026-10-10"
    ni_flow._flow_write(store, iid, {
        "state": "sampling", "request": "r", "updated_at": "", "notes": [],
        "_library_source": "demo", "_library_url": post_url,
        "_ranked_library": [
            {"source_id": "demo", "url": pre_url, "label": "a", "provider": "p",
             "host": "ex.test"},
            {"source_id": "other", "url": "https://y.test/", "label": "b",
             "provider": "q", "host": "y.test"}]})
    moved = ni_flow._repick_without(store, iid, post_url, why="t")
    assert moved is not None
    rest = (ni_flow._flow_read(store, iid) or {}).get("_ranked_library") or []
    assert [r["source_id"] for r in rest] == ["other"]


# ---- R5-5: _f1_templatize_literal walks the record by position ----------------------------------

def test_r5_5_templatize_by_position_slots_all_clock_occurrences(monkeypatch) -> None:
    """A pre-F1 URL whose record repeats a clock placeholder gets EVERY occurrence slotted — the
    count-1 value replace left the second one frozen."""
    from smartbrain_3000 import ni_flow

    rec = {"access": {
        "url_template": "https://ex.test/{year}/x?year={year}",
        "params": [{"name": "year", "label": "year",
                     "fill": {"from": "clock", "format": "%Y", "offset_days": 0}}]}}
    _freeze(monkeypatch, datetime(2026, 10, 4, 15, 0, tzinfo=NY))
    tried = ni_flow._f1_templatize_literal(
        "https://ex.test/2026/x?year=2026",
        {"year": {"format": "%Y", "offset_days": 0, "label": "year"}},
        datetime(2026, 10, 4, 15, 0, tzinfo=NY), lib_record=rec)
    assert tried == "https://ex.test/{{param:year}}/x?year={{param:year}}"


def test_r5_5_templatize_rejects_partial_on_day_eq_month(monkeypatch) -> None:
    """A date whose day equals the month (10/10) binds each placeholder to its OWN slot — the
    position walk never collapses mm and dd onto one match."""
    from smartbrain_3000 import ni_flow

    rec = {"access": {
        "url_template": "https://ex.test/{mm}/{dd}",
        "params": [{"name": "mm", "label": "mm",
                     "fill": {"from": "clock", "format": "%m", "offset_days": 0}},
                    {"name": "dd", "label": "dd",
                     "fill": {"from": "clock", "format": "%d", "offset_days": 0}}]}}
    _freeze(monkeypatch, datetime(2026, 10, 10, 9, 0, tzinfo=NY))
    tried = ni_flow._f1_templatize_literal(
        "https://ex.test/10/10",
        {"mm": {"format": "%m", "offset_days": 0, "label": "mm"},
         "dd": {"format": "%d", "offset_days": 0, "label": "dd"}},
        datetime(2026, 10, 10, 9, 0, tzinfo=NY), lib_record=rec)
    assert tried == "https://ex.test/{{param:mm}}/{{param:dd}}"


# ---- R5-8: keyed clock sources drop vault_key segments before alignment -------------------------

def test_r5_8_derive_clock_template_strips_vault_key_segments(monkeypatch) -> None:
    """A keyed clock source (neows, finnhub, FEC) ships its URL with the ``{key}`` segment stripped;
    the position-aligned derive must drop that segment from the Library template too, or the walk
    fails on the trailing ``&token={key}`` the URL legitimately lacks."""
    from smartbrain_3000 import ni_flow

    class _Lib:
        def get(self, _sid):
            return {"access": {
                "url_template": "https://ex.test/feed?start_date={date}&api_key={key}",
                "params": [{"name": "date", "label": "date",
                              "fill": {"from": "clock", "format": "%Y-%m-%d",
                                        "offset_days": 0}},
                            {"name": "key", "fill": {"from": "vault_key"}}]}}

    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Lib())
    _freeze(monkeypatch, datetime(2026, 10, 4, 15, 0, tzinfo=NY))
    template, meta = ni_flow._derive_clock_template(
        "demo", "https://ex.test/feed?start_date=2026-10-04", {"date": "2026-10-04"})
    assert template == "https://ex.test/feed?start_date={{param:date}}"
    assert list(meta) == ["date"]
