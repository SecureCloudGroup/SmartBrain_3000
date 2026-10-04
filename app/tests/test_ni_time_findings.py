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
