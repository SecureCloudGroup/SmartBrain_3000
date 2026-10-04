"""Engine grammar for time-scoped asks (C7), placeholder times (C15) and page-value grounding (C9).

Every sample is a recorded source response under ``tests/fixtures/ni_window`` (fetched 2026-09-29
with the app's honest User-Agent): Open-Meteo Austin 7-day (the Library's own url) and 48-hour,
Open-Meteo Honolulu, Open-Meteo Denver across the 2026 US spring-forward (ISO labels and unix
times, historical-forecast API), NWS Denver hourly (ISO with offsets), MLB Dodgers postseason
(every game startTimeTBD=true) and regular season. The clock is frozen through ``ni._clock``."""

from __future__ import annotations

import json
import pathlib
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000 import ni as nimod
from smartbrain_3000.ni import NIError

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ni_window"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _freeze(monkeypatch, moment: datetime) -> None:
    """The engine's clock reads ``moment`` (an aware datetime, in the user's zone)."""
    assert moment.tzinfo is not None
    monkeypatch.setattr(nimod, "_clock", lambda: moment)


@pytest.fixture
def user_tz(monkeypatch):
    """Set the process zone (``local_time`` / ``local_date`` show times in it); restored after."""
    def _set(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield _set
    monkeypatch.undo()
    time.tzset()


def _om_rows(sample: dict, block: str, window: str, *, zone: str | None = None) -> list[str]:
    """Open-Meteo columns → rows (zip) → window; returns the kept rows' time labels."""
    op = {"fn": "window", "field": "rows", "key": "t", "window": window}
    if zone:
        op["zone"] = zone
    stages = [
        {"op": "extract", "paths": {"t": f"{block}.time", "v": f"{block}.{_first_metric(sample, block)}",
                                    "tz": "timezone", "offset": "utc_offset_seconds"}},
        {"op": "transform", "apply": [{"fn": "zip", "field": "t", "with": ["v"], "as": "rows"}, op]},
    ]
    nimod._validate_transform_stage(stages[1], 1, {"t", "v", "tz", "offset"})
    return [r["t"] for r in nimod.run_pipeline(stages, sample)["rows"]]


def _first_metric(sample: dict, block: str) -> str:
    return next(k for k in sample[block] if k != "time")


# --- C7: the window transform over a 7-day daily list, every weekday × every window --------------

AUSTIN = _load("open_meteo_austin_7d.json")
CHICAGO = ZoneInfo("America/Chicago")
DAYS = AUSTIN["daily"]["time"]  # Tue 2026-09-29 .. Mon 2026-10-05
assert DAYS[0] == "2026-09-29" and len(DAYS) == 7

# today (index into DAYS: 0=Tue .. 6=Mon) → window → the day indexes that window keeps
_DATE_WINDOWS = ("today", "tomorrow", "weekend", "dow:sat", "dow:sun", "dow:mon", "dow:tue",
                 "dow:fri", "next_days:3", "next_days:1")
_EXPECTED_DAYS = {
    0: ([0], [1], [4, 5], [4], [5], [6], [0], [3], [0, 1, 2], [0]),
    1: ([1], [2], [4, 5], [4], [5], [6], [], [3], [1, 2, 3], [1]),
    2: ([2], [3], [4, 5], [4], [5], [6], [], [3], [2, 3, 4], [2]),
    3: ([3], [4], [4, 5], [4], [5], [6], [], [3], [3, 4, 5], [3]),
    4: ([4], [5], [4, 5], [4], [5], [6], [], [], [4, 5, 6], [4]),   # Saturday: Sat + Sun
    5: ([5], [6], [5], [], [5], [6], [], [], [5, 6], [5]),          # Sunday: Sunday only
    6: ([6], [], [], [], [], [6], [], [], [6], [6]),                # Monday: next weekend is past the data
}
_TIME_WINDOWS = ("now", "tonight", "next_hours:6")


def _evening(day: int) -> datetime:
    """20:30 in Austin on DAYS[day]."""
    y, m, d = (int(x) for x in DAYS[day].split("-"))
    return datetime(y, m, d, 20, 30, tzinfo=CHICAGO)


@pytest.mark.parametrize("today", range(7))
def test_daily_rows_keep_the_asked_days_on_every_weekday(monkeypatch, today) -> None:
    _freeze(monkeypatch, _evening(today))
    for window, days in zip(_DATE_WINDOWS, _EXPECTED_DAYS[today], strict=True):
        assert _om_rows(AUSTIN, "daily", window) == [DAYS[i] for i in days], (DAYS[today], window)


@pytest.mark.parametrize("today", range(7))
def test_daily_rows_cannot_express_a_time_of_day(monkeypatch, today) -> None:
    """A day row has no clock: now / tonight / next hours keep nothing (the flow needs an hourly axis)."""
    _freeze(monkeypatch, _evening(today))
    for window in _TIME_WINDOWS:
        assert _om_rows(AUSTIN, "daily", window) == [], window


def _hours(day: int, hours) -> list[str]:
    return [f"{DAYS[day]}T{h:02d}:00" for h in hours]


@pytest.mark.parametrize("today", range(7))
def test_hourly_rows_keep_the_asked_hours_on_every_weekday(monkeypatch, today) -> None:
    """The Library's Open-Meteo url: 168 hourly rows from 00:00 today, local labels (timezone=auto)."""
    _freeze(monkeypatch, _evening(today))
    for window, days in zip(_DATE_WINDOWS, _EXPECTED_DAYS[today], strict=True):
        want = [t for i in days for t in _hours(i, range(24))]
        assert _om_rows(AUSTIN, "hourly", window) == want, (DAYS[today], window)
    after = today + 1 if today < 6 else None
    assert _om_rows(AUSTIN, "hourly", "now") == _hours(today, [20])
    assert _om_rows(AUSTIN, "hourly", "tonight") == (
        _hours(today, range(18, 24)) + (_hours(after, range(6)) if after else []))
    assert _om_rows(AUSTIN, "hourly", "next_hours:6") == (
        _hours(today, range(20, 24)) + (_hours(after, range(2)) if after else []))


def test_tonight_before_dawn_is_the_coming_night(monkeypatch) -> None:
    """tonight = 18:00 today → 06:00 tomorrow, whatever the hour of the ask."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 2, 0, tzinfo=CHICAGO))  # Sat 02:00
    assert _om_rows(AUSTIN, "hourly", "tonight") == _hours(4, range(18, 24)) + _hours(5, range(6))
    assert _om_rows(AUSTIN, "hourly", "now") == _hours(4, [2])


def test_the_48_hour_forecast_starts_at_the_current_hour(monkeypatch) -> None:
    """forecast_hours=48: the first row is the current hour; 'now' is that row, 'today' the rest of today."""
    sample = _load("open_meteo_austin_48h.json")
    _freeze(monkeypatch, datetime(2026, 9, 29, 10, 40, tzinfo=CHICAGO))
    assert _om_rows(sample, "hourly", "now") == ["2026-09-29T10:00"]
    assert _om_rows(sample, "hourly", "today") == [f"2026-09-29T{h:02d}:00" for h in range(10, 24)]
    assert len(_om_rows(sample, "hourly", "tomorrow")) == 24
    assert _om_rows(sample, "hourly", "next_hours:3") == ["2026-09-29T10:00", "2026-09-29T11:00",
                                                           "2026-09-29T12:00"]
    assert _om_rows(sample, "daily", "weekend") == ["2026-10-03", "2026-10-04"]


def test_now_keeps_nothing_when_every_row_is_in_the_future(monkeypatch) -> None:
    sample = _load("open_meteo_austin_48h.json")
    _freeze(monkeypatch, datetime(2026, 9, 29, 8, 0, tzinfo=CHICAGO))
    assert _om_rows(sample, "hourly", "now") == []


# --- non-UTC zones: the source's zone, not the user's -------------------------------------------

def test_the_source_zone_decides_today_for_a_user_elsewhere(monkeypatch) -> None:
    """Honolulu labels (zoneless, the place's time); the user is in Denver where it's already tomorrow."""
    sample = _load("open_meteo_honolulu_48h.json")
    _freeze(monkeypatch, datetime(2026, 9, 30, 1, 0, tzinfo=ZoneInfo("America/Denver")))  # 21:00 HST 9/29
    assert _om_rows(sample, "hourly", "now", zone="tz") == ["2026-09-29T21:00"]
    assert _om_rows(sample, "hourly", "today", zone="tz") == [f"2026-09-29T{h:02d}:00" for h in range(5, 24)]
    assert _om_rows(sample, "hourly", "tonight", zone="tz") == (
        [f"2026-09-29T{h:02d}:00" for h in range(18, 24)] + [f"2026-09-30T{h:02d}:00" for h in range(6)])
    assert _om_rows(sample, "daily", "tomorrow", zone="offset") == ["2026-09-30"]
    # no zone named: a zoneless label is compared with the user's own clock (Denver: already 9/30)
    assert _om_rows(sample, "daily", "today") == ["2026-09-30"]


def test_the_zone_field_may_be_a_utc_offset_in_seconds(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 9, 29, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles")))  # 01:30 CDT
    assert _om_rows(AUSTIN, "daily", "today", zone="offset") == ["2026-09-30"]
    assert _om_rows(AUSTIN, "daily", "today", zone="tz") == ["2026-09-30"]
    assert _om_rows(AUSTIN, "hourly", "now", zone="offset") == ["2026-09-30T01:00"]
    assert _om_rows(AUSTIN, "daily", "today") == ["2026-09-29"]  # the user's clock


def _nws_rows(window: str) -> list[str]:
    sample = _load("nws_denver_hourly.json")
    stages = [{"op": "extract", "paths": {"rows": "properties.periods"}},
              {"op": "transform", "apply": [{"fn": "window", "field": "rows", "key": "startTime",
                                             "window": window}]}]
    return [r["startTime"] for r in nimod.run_pipeline(stages, sample)["rows"]]


def test_rows_with_offsets_use_the_sources_own_zone(monkeypatch) -> None:
    """NWS periods carry -06:00; the user is in Honolulu, where it is still the day before."""
    _freeze(monkeypatch, datetime(2026, 9, 30, 1, 30, tzinfo=ZoneInfo("America/Denver"))
            .astimezone(ZoneInfo("Pacific/Honolulu")))  # Tue 21:30 HST
    today = _nws_rows("today")
    assert today[0] == "2026-09-30T00:00:00-06:00" and today[-1] == "2026-09-30T23:00:00-06:00"
    assert len(today) == 24
    assert _nws_rows("now") == ["2026-09-30T01:00:00-06:00"]
    assert _nws_rows("next_hours:3") == ["2026-09-30T01:00:00-06:00", "2026-09-30T02:00:00-06:00",
                                         "2026-09-30T03:00:00-06:00"]
    assert _nws_rows("tonight") == [f"2026-09-30T{h:02d}:00:00-06:00" for h in range(18, 24)] + [
        f"2026-10-01T{h:02d}:00:00-06:00" for h in range(6)]
    weekend = _nws_rows("weekend")
    assert {t[:10] for t in weekend} == {"2026-10-03", "2026-10-04"} and len(weekend) == 48


# --- US spring-forward (2026-03-08 02:00 America/Denver) -----------------------------------------

DENVER = ZoneInfo("America/Denver")


def _dst_epochs(window: str, zone: str = "tz") -> list[int]:
    sample = _load("open_meteo_denver_dst_unixtime.json")
    stages = [{"op": "extract", "paths": {"t": "hourly.time", "v": "hourly.temperature_2m", "tz": "timezone"}},
              {"op": "transform", "apply": [{"fn": "zip", "field": "t", "with": ["v"], "as": "rows"},
                                            {"fn": "window", "field": "rows", "key": "t", "window": window,
                                             "zone": zone}]}]
    return [r["t"] for r in nimod.run_pipeline(stages, sample)["rows"]]


def _walls(epochs: list[int]) -> list[str]:
    return [datetime.fromtimestamp(e, tz=DENVER).strftime("%m-%d %H:%M") for e in epochs]


def test_spring_forward_day_has_23_hours(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 3, 8, 12, 0, tzinfo=DENVER))
    today = _walls(_dst_epochs("today"))
    assert len(today) == 23 and "03-08 02:00" not in today
    assert today[0] == "03-08 00:00" and today[-1] == "03-08 23:00"


def test_next_hours_across_spring_forward_counts_real_hours(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 3, 8, 0, 30, tzinfo=DENVER))  # 00:30 MST
    assert _walls(_dst_epochs("now")) == ["03-08 00:00"]
    assert _walls(_dst_epochs("next_hours:3")) == ["03-08 00:00", "03-08 01:00", "03-08 03:00"]


def test_tonight_across_spring_forward_is_one_hour_shorter(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 3, 7, 20, 0, tzinfo=DENVER))  # Sat 20:00 MST
    tonight = _walls(_dst_epochs("tonight"))
    assert tonight == [f"03-07 {h:02d}:00" for h in range(18, 24)] + [
        "03-08 00:00", "03-08 01:00", "03-08 03:00", "03-08 04:00", "03-08 05:00"]
    assert _walls(_dst_epochs("weekend"))[0] == "03-07 00:00"
    assert len(_dst_epochs("weekend")) == 24 + 23


def test_open_meteo_labels_use_one_fixed_offset_across_the_change(monkeypatch) -> None:
    """Open-Meteo labels every row at its one ``utc_offset_seconds`` (-6h here), so the offset —
    not the zone name — is the honest reference for its zoneless labels."""
    sample = _load("open_meteo_denver_dst.json")
    _freeze(monkeypatch, datetime(2026, 3, 8, 7, 30, tzinfo=UTC))  # 00:30 MST = 01:30 at -06:00
    assert _om_rows(sample, "hourly", "now", zone="offset") == ["2026-03-08T01:00"]
    assert _om_rows(sample, "hourly", "next_hours:3", zone="offset") == [
        "2026-03-08T01:00", "2026-03-08T02:00", "2026-03-08T03:00"]


def test_rows_with_offsets_across_spring_forward(monkeypatch) -> None:
    """ISO rows written with their own offsets (as NWS writes them) through the change: 'today' is
    decided in the offset in force now, so late Saturday never reads Sunday's rows as today."""
    sample = _load("open_meteo_denver_dst_unixtime.json")
    rows = [{"t": datetime.fromtimestamp(e, tz=DENVER).isoformat()} for e in sample["hourly"]["time"]]

    def keep(window: str) -> list[str]:
        out = nimod.run_pipeline([{"op": "transform", "apply": [
            {"fn": "window", "field": "rows", "key": "t", "window": window}]}], {"rows": rows})["rows"]
        return [r["t"] for r in out]

    _freeze(monkeypatch, datetime(2026, 3, 7, 23, 30, tzinfo=DENVER))
    today = keep("today")
    assert {t[:10] for t in today} == {"2026-03-07"} and len(today) == 24
    _freeze(monkeypatch, datetime(2026, 3, 8, 0, 30, tzinfo=DENVER))
    assert keep("next_hours:4") == ["2026-03-08T00:00:00-07:00", "2026-03-08T01:00:00-07:00",
                                    "2026-03-08T03:00:00-06:00", "2026-03-08T04:00:00-06:00"]


# --- the window transform's contract ------------------------------------------------------------

def test_rows_whose_time_cannot_be_read_are_left_out(monkeypatch) -> None:
    _freeze(monkeypatch, _evening(0))
    rows = [{"t": "2026-09-29"}, {"t": "soon"}, {"t": None}, {"x": 1}, "text", {"t": "2026-09-30"}]
    out = nimod.run_pipeline([{"op": "transform", "apply": [
        {"fn": "window", "field": "rows", "key": "t", "window": "today"}]}], {"rows": rows})
    assert out["rows"] == [{"t": "2026-09-29"}]


def test_window_needs_a_list(monkeypatch) -> None:
    _freeze(monkeypatch, _evening(0))
    with pytest.raises(NIError) as err:
        nimod.run_pipeline([{"op": "transform", "apply": [
            {"fn": "window", "field": "rows", "key": "t", "window": "today"}]}], {"rows": "2026-09-29"})
    assert err.value.kind == "transform_type"


def test_an_unknown_zone_fails_the_run_rather_than_guessing(monkeypatch) -> None:
    _freeze(monkeypatch, _evening(0))
    with pytest.raises(NIError) as err:
        nimod.run_pipeline([{"op": "transform", "apply": [
            {"fn": "window", "field": "rows", "key": "t", "window": "today", "zone": "tz"}]}],
            {"rows": [{"t": "2026-09-29"}], "tz": "Mars/Olympus_Mons"})
    assert err.value.kind == "transform_type"


def test_a_nested_row_key_is_read(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
    sample = _load("mlb_dodgers_postseason.json")
    out = nimod.run_pipeline([
        {"op": "extract", "paths": {"rows": "dates"}},
        {"op": "transform", "apply": [
            {"fn": "window", "field": "rows", "key": "games[0].officialDate", "window": "weekend"}]}], sample)
    assert [r["date"] for r in out["rows"]] == ["2026-10-03", "2026-10-04"]


_GOOD = {"fn": "window", "field": "rows", "key": "t", "window": "today"}


@pytest.mark.parametrize("window", [
    "now", "today", "tonight", "tomorrow", "weekend", "dow:mon", "dow:sun", "next_days:1",
    "next_days:16", "next_hours:1", "next_hours:168"])
def test_validator_accepts_every_window(window) -> None:
    nimod._validate_transform_op({**_GOOD, "window": window}, 0, 0, set())
    nimod._validate_transform_op({**_GOOD, "window": window, "zone": "timezone"}, 0, 0, set())


@pytest.mark.parametrize("window", [
    "weekends", "Today", "this weekend", "dow:xyz", "dow:saturday", "dow:", "next_days:0", "next_days:17",
    "next_hours:0", "next_hours:169", "next_days:-1", "next_days:3.5", "next_days", "none", "", 5, None,
    True, ["today"]])
def test_validator_refuses_unknown_windows(window) -> None:
    with pytest.raises(ValueError):
        nimod._validate_transform_op({**_GOOD, "window": window}, 0, 0, set())


@pytest.mark.parametrize("op", [
    {**_GOOD, "n": 3},                                 # unknown key
    {**_GOOD, "op": "eq"},                             # unknown key
    {k: v for k, v in _GOOD.items() if k != "key"},    # key required
    {k: v for k, v in _GOOD.items() if k != "window"},  # window required
    {**_GOOD, "key": "bad key!"},
    {**_GOOD, "zone": "not a name"},
    {**_GOOD, "zone": 5},
])
def test_validator_refuses_malformed_window_ops(op) -> None:
    with pytest.raises(ValueError):
        nimod._validate_transform_op(op, 0, 0, set())


# --- C15: a placeholder start time renders as "time TBD" -----------------------------------------

POSTSEASON = _load("mlb_dodgers_postseason.json")
REGULAR = _load("mlb_dodgers_regular.json")


def _next_game(sample: dict, *, unless: bool = True) -> str:
    op = {"fn": "time", "field": "t"}
    if unless:
        op["unless"] = "tbd"
    stages = [{"op": "extract", "paths": {"t": "dates[0].games[0].gameDate",
                                          "tbd": "dates[0].games[0].status.startTimeTBD"}},
              {"op": "transform", "apply": [op]}]
    nimod._validate_transform_stage(stages[1], 1, {"t", "tbd"})
    return nimod.run_pipeline(stages, sample)["t"]


def test_a_tbd_start_time_shows_the_date_and_time_tbd(monkeypatch, user_tz) -> None:
    user_tz("America/Los_Angeles")
    _freeze(monkeypatch, datetime(2026, 9, 29, 12, 0).astimezone())
    assert POSTSEASON["dates"][0]["games"][0]["status"]["startTimeTBD"] is True
    assert _next_game(POSTSEASON) == "Sat Oct 3 · time TBD"
    assert _next_game(POSTSEASON, unless=False) == "Sat 3:33 AM"  # the sentinel the card used to show


def test_a_set_start_time_still_shows_the_time(monkeypatch, user_tz) -> None:
    user_tz("America/Los_Angeles")
    _freeze(monkeypatch, datetime(2026, 9, 24, 12, 0).astimezone())
    assert REGULAR["dates"][0]["games"][0]["status"]["startTimeTBD"] is False
    assert _next_game(REGULAR) == "Fri 7:15 PM"


def test_tbd_rows_in_a_schedule_list(monkeypatch, user_tz) -> None:
    user_tz("America/Los_Angeles")
    _freeze(monkeypatch, datetime(2026, 9, 29, 12, 0).astimezone())
    rows = POSTSEASON["dates"] + REGULAR["dates"][:1]
    op = {"fn": "time", "field": "rows", "key": "games[0].gameDate", "unless": "games[0].status.startTimeTBD"}
    nimod._validate_transform_op(op, 0, 0, set())
    out = nimod.run_pipeline([{"op": "transform", "apply": [op]}], {"rows": rows})["rows"]
    shown = [r["games"][0]["gameDate"] for r in out]
    assert shown == ["Sat Oct 3 · time TBD", "Sun Oct 4 · time TBD", "Oct 6 · time TBD", "Oct 7 · time TBD",
                     "Oct 9 · time TBD", "Fri 7:15 PM"]
    clock = nimod.run_pipeline([{"op": "transform", "apply": [{**op, "clock": True}]}], {"rows": rows})["rows"]
    assert [r["games"][0]["gameDate"] for r in clock] == ["time TBD"] * 5 + ["7:15 PM"]


@pytest.mark.parametrize("unless", ["bad key!", 5, True, ""])
def test_validator_refuses_a_malformed_unless(unless) -> None:
    with pytest.raises(ValueError):
        nimod._validate_transform_op({"fn": "time", "field": "t", "unless": unless}, 0, 0, set())


def test_unless_is_only_a_time_key() -> None:
    with pytest.raises(ValueError):
        nimod._validate_transform_op({"fn": "date", "field": "t", "unless": "tbd"}, 0, 0, set())


# --- C9: grounding a page reading, at build time -------------------------------------------------

LAGCHECK = ("Is Slack down? Check current Slack status and outages. Users reported problems in the last "
            "24 hours. Most reported problems: Messages 46%, Login 31%, Notifications 23%.")
POWERBALL = ("POWERBALL Next Drawing Wed, Sep 30, 2026 Estimated Jackpot $409 Million Cash Value "
             "$187.4 Million Winning Numbers Mon, Sep 28, 2026")


@pytest.mark.parametrize(("values", "text", "want"), [
    ({"status": "Operational"}, LAGCHECK, {"status": False}),
    ({"jackpot": "$409 Million"}, POWERBALL, {"jackpot": True}),
    ({"cash": "$187.4 Million", "jackpot": "$409 Million"}, POWERBALL, {"cash": True, "jackpot": True}),
    ({"jackpot": "$490 Million"}, POWERBALL, {"jackpot": False}),          # a number not on the page
    ({"jackpot": "$40 Million"}, POWERBALL, {"jackpot": False}),           # numbers are exact, not substrings
    ({"jackpot": "$409 Billion"}, POWERBALL, {"jackpot": False}),          # every word counts
    ({"n": 409}, POWERBALL, {"n": True}),
    ({"n": 187.4}, POWERBALL, {"n": True}),
    ({"n": 1874}, POWERBALL, {"n": False}),
    ({"status": "No issues"}, "Slack Status: No issues. All systems go.", {"status": True}),
    ({"status": "No issues"}, "Known issues with search. We are investigating.", {"status": False}),
    ({"count": "1,409 users"}, "There are 1409 users online", {"count": True}),
    ({"line": "Red Line"}, "Delays on the RED line near Farragut North", {"line": True}),
    ({"blank": ""}, POWERBALL, {"blank": True}),
    ({"none": None}, POWERBALL, {"none": True}),
])
def test_ground_values(values, text, want) -> None:
    assert nimod.ground_values(values, text) == want


def test_word_values_must_sit_together_on_the_page() -> None:
    far = "Service health dashboard. " + ("filler words here. " * 40) + "Everything is operational."
    assert nimod.ground_values({"s": "operational service"}, far) == {"s": False}
    near = "Service is fully operational today."
    assert nimod.ground_values({"s": "operational service"}, near) == {"s": True}


def test_a_clock_or_score_is_grounded_as_a_whole() -> None:
    page = "Departs 18:20 EDT, lands 06:25 BST after 7 hours. Final: Alabama 49–18 South Carolina."
    got = nimod.ground_values({"a": "07:25", "b": "06:25", "c": "6:25", "d": "49-18", "e": "18-49",
                               "f": "18:20 EDT"}, page)
    assert got == {"a": False, "b": True, "c": True, "d": True, "e": False, "f": True}
    # a reading may drop the seconds the page shows
    assert nimod.ground_values({"t": "19:52"}, "Highest point 19:52:58 50° SW") == {"t": True}


def test_ground_values_on_an_empty_page_grounds_nothing() -> None:
    assert nimod.ground_values({"a": "Operational", "b": 5}, "") == {"a": False, "b": False}


# --- C9: a next-event time that has passed --------------------------------------------------------

def test_next_event_stale(monkeypatch) -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert nimod.next_event_stale("2026-09-28T15:00:00-07:00", now) is True        # yesterday
    assert nimod.next_event_stale("2026-09-29T18:00:00-07:00", now) is False       # later today
    assert nimod.next_event_stale((now - timedelta(minutes=10)).isoformat(), now) is False  # in grace
    assert nimod.next_event_stale((now - timedelta(minutes=20)).isoformat(), now) is True
    assert nimod.next_event_stale((now - timedelta(minutes=20)).isoformat(), now, grace_minutes=30) is False
    assert nimod.next_event_stale(int((now - timedelta(days=1)).timestamp()), now) is True
    assert nimod.next_event_stale(int((now + timedelta(hours=2)).timestamp() * 1000), now) is False
    assert nimod.next_event_stale(now - timedelta(hours=1), now) is True
    assert nimod.next_event_stale("soon", now) is False and nimod.next_event_stale(None, now) is False


def test_next_event_stale_reads_a_formatted_time_value(monkeypatch, user_tz) -> None:
    """The ``time`` transform's text keeps its moment, so a formatted value is still judged."""
    user_tz("America/Los_Angeles")
    now = datetime(2026, 9, 29, 12, 0).astimezone()
    _freeze(monkeypatch, now)
    past = nimod.run_pipeline([{"op": "transform", "apply": [{"fn": "time", "field": "t"}]}],
                              {"t": "2026-09-29T09:00:00-07:00"})["t"]
    assert past == "9:00 AM" and nimod.next_event_stale(past, now) is True
    later = nimod.run_pipeline([{"op": "transform", "apply": [{"fn": "time", "field": "t"}]}],
                               {"t": "2026-09-29T19:00:00-07:00"})["t"]
    assert later == "7:00 PM" and nimod.next_event_stale(later, now) is False
    assert nimod.next_event_stale("9:00 AM", now) is False  # plain words carry no moment
    tbd = _next_game(POSTSEASON)
    assert nimod.next_event_stale(tbd, now) is False
    assert nimod.next_event_stale(tbd, datetime(2026, 10, 3, 18, 0).astimezone()) is False  # game day
    assert nimod.next_event_stale(tbd, datetime(2026, 10, 4, 9, 0).astimezone()) is True


def _next_scene(**extra) -> dict:
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "Next eruption", "role": "label", "tone": "muted", "size": "sm"},
        {"type": "text", "value": {"$bind": "next_time"}, "role": "value", "tone": "default", "size": "lg",
         **extra}]}


def test_scene_next_marks_a_passed_time_as_no_current_prediction(monkeypatch, user_tz) -> None:
    user_tz("America/Denver")
    now = datetime(2026, 9, 29, 12, 0).astimezone()
    _freeze(monkeypatch, now)
    scene = _next_scene(next=True)
    nimod._validate_scene_node(scene, 1, nimod._NodeCounter())
    stages = [{"op": "transform", "apply": [{"fn": "time", "field": "next_time"}]}]
    past = nimod.run_pipeline(stages, {"next_time": "2026-09-29T09:52:00-06:00"})
    bound = nimod.bind_scene(scene, past)
    assert bound["children"][1]["value"] == "no current prediction"
    assert "next" not in bound["children"][1]
    nimod._enforce_bind_types(scene, bound)
    future = nimod.run_pipeline(stages, {"next_time": "2026-09-29T15:52:00-06:00"})
    assert nimod.bind_scene(scene, future)["children"][1]["value"] == "3:52 PM"
    # without the mark a passed time still shows as written
    assert nimod.bind_scene(_next_scene(), past)["children"][1]["value"] == "9:52 AM"


@pytest.mark.parametrize("extra", [{"next": "yes"}, {"next": 1}])
def test_scene_next_is_a_boolean(extra) -> None:
    with pytest.raises(ValueError):
        nimod._validate_scene_node(_next_scene(**extra), 1, nimod._NodeCounter())


def test_scene_next_needs_a_bound_value() -> None:
    scene = {"type": "text", "value": "9:52 AM", "role": "value", "tone": "default", "size": "lg",
             "next": True}
    with pytest.raises(ValueError):
        nimod._validate_scene_node(scene, 1, nimod._NodeCounter())


# --- D4: a UTC-stamped row is judged on the user's calendar (review 2026-10-03) ----------------------------
# MLB / NHL send every start in UTC ("Z"). With no zone in the spec, a "Z" says nothing about where the games
# are: "today" is the user's today. On the UTC calendar, at 21:30 in New York "today" showed tomorrow's 1:05 PM
# game and dropped today's, "tonight" was empty while a 9:40 PM game was on, and at 14:00 "tomorrow" took in
# tonight's 9:40 PM game.

NY = ZoneInfo("America/New_York")
_GAMES = [  # Saturday 2026-10-03 (New York): 1:05 PM, 7:10 PM, 9:40 PM; Sunday: 1:05 PM, 8:20 PM
    {"g": "sat-1305", "gameDate": "2026-10-03T17:05:00Z"},
    {"g": "sat-1910", "gameDate": "2026-10-03T23:10:00Z"},
    {"g": "sat-2140", "gameDate": "2026-10-04T01:40:00Z"},
    {"g": "sun-1305", "gameDate": "2026-10-04T17:05:00+00:00"},
    {"g": "sun-2020", "gameDate": "2026-10-05T00:20:00Z"},
]


def _games(window: str, zone: str | None = None) -> list[str]:
    op = {"fn": "window", "field": "rows", "key": "gameDate", "window": window}
    paths = {"rows": "games"}
    if zone:
        op["zone"], paths["zone"] = "zone", "zone"
    sample = {"games": _GAMES, "zone": zone}
    return [r["g"] for r in nimod.run_pipeline([{"op": "extract", "paths": paths},
                                                {"op": "transform", "apply": [op]}], sample)["rows"]]


@pytest.mark.parametrize("clock, window, want", [
    # the review's cases
    ((21, 30), "today", ["sat-1305", "sat-1910", "sat-2140"]),
    ((21, 30), "tonight", ["sat-1910", "sat-2140"]),
    ((14, 0), "tomorrow", ["sun-1305", "sun-2020"]),
    # more of the class
    ((14, 0), "today", ["sat-1305", "sat-1910", "sat-2140"]),
    ((14, 0), "tonight", ["sat-1910", "sat-2140"]),
    ((21, 30), "tomorrow", ["sun-1305", "sun-2020"]),
    ((21, 30), "dow:sun", ["sun-1305", "sun-2020"]),
    ((9, 0), "weekend", ["sat-1305", "sat-1910", "sat-2140", "sun-1305", "sun-2020"]),
    ((23, 59), "next_days:1", ["sat-1305", "sat-1910", "sat-2140"]),
])
def test_utc_stamped_rows_are_judged_on_the_users_calendar(monkeypatch, clock, window, want) -> None:
    _freeze(monkeypatch, datetime(2026, 10, 3, *clock, tzinfo=NY))
    assert _games(window) == want


def test_utc_rows_follow_the_user_in_another_zone(monkeypatch) -> None:
    """In Los Angeles at 19:00 Saturday, the 9:40 PM ET game (6:40 PM PT) is today's, and "tonight" (from
    18:00 PT) holds it — never the UTC day."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 19, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
    assert _games("today") == ["sat-1305", "sat-1910", "sat-2140"]
    assert _games("tonight") == ["sat-2140"]


def test_a_named_zone_still_decides_for_utc_rows(monkeypatch) -> None:
    """The spec names the source's zone: its calendar, not the user's (unchanged)."""
    _freeze(monkeypatch, datetime(2026, 10, 3, 21, 30, tzinfo=NY))
    assert _games("today", zone="Asia/Tokyo") == ["sat-1305", "sat-1910", "sat-2140"]  # Sunday in Tokyo
    assert _games("tomorrow", zone="Asia/Tokyo") == ["sun-1305", "sun-2020"]


def test_next_hours_on_utc_rows_counts_real_hours(monkeypatch) -> None:
    _freeze(monkeypatch, datetime(2026, 10, 3, 18, 30, tzinfo=NY))
    assert _games("next_hours:4") == ["sat-1910", "sat-2140"]
