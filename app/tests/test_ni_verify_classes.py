"""The four verify constraint classes (plan S2): time window, numeric threshold, compare count, source scope.

Pure functions over (the ask's constraints, the built result, the source): each class is tested over several
phrasings and edges, never over one named question. Time zone edges use a fixed clock."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000.ni_verify.classes import (
    check_compare,
    check_scope,
    check_threshold,
    check_window,
    shadow_reasons,
    source_floor,
    window_span,
)
from smartbrain_3000.ni_verify.constraints import (
    parse_area,
    parse_ask,
    parse_entities,
    parse_threshold,
    parse_window,
)
from smartbrain_3000.ni_verify.rows import (
    Result,
    SourceInfo,
    iso_span,
    read_result,
    scan,
)

CHI = ZoneInfo("America/Chicago")
TOKYO = ZoneInfo("Asia/Tokyo")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _span(text: str) -> tuple[datetime, datetime]:
    at = _utc(text)
    return at, at


def _result(*moments: str, text: str = "x") -> Result:
    return Result(spans=tuple(_span(m) for m in moments), text=text)


# ---- parse -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("NHL games tonight", "tonight"), ("what's on tonight", "tonight"), ("tonight's lineup", "tonight"),
    ("movies this evening", "tonight"), ("games today", "today"), ("today's schedule", "today"),
    ("hourly temps in Minneapolis today", "today"), ("rain tomorrow", "tomorrow"),
    ("snow forecast for Tahoe this weekend", "weekend"), ("last night's score", "last_night"),
    ("yesterday's results", "yesterday"),
    ("NHL games", None), ("Saturday Night Live", None), ("games today and tomorrow", None),
    ("next weekend forecast", None),
])
def test_parse_window(text, want):
    assert parse_window(text) == want


@pytest.mark.parametrize("text,want", [
    ("how many earthquakes above 4.0 today", (">", 4.0)), ("quakes over 5", (">", 5.0)),
    ("at least 3 inches of snow", (">=", 3.0)), ("M4.5+ earthquakes", (">=", 4.5)),
    ("temps below 20", ("<", 20.0)), ("under 10 dollars", ("<", 10.0)), ("no more than 2", ("<=", 2.0)),
    ("earthquakes magnitude over 5.5", (">", 5.5)),
    ("top 10 cryptocurrencies", None), ("over the last 24 hours", None), ("under 5 minutes", None),
    ("how warm is it in Boise", None),
])
def test_parse_threshold(text, want):
    assert parse_threshold(text) == want


@pytest.mark.parametrize("text,want", [
    ("weather in Denver vs Salt Lake City", ("denver", "salt lake city")),
    ("Chiefs versus Bills", ("chiefs", "bills")), ("compare Ethereum and Solana prices", ("ethereum", "solana")),
    ("bitcoin price vs ethereum price", ("bitcoin", "ethereum")),
    ("difference between Boston and Seattle weather", ("boston", "seattle")),
    ("Lakers vs. Celtics tonight", ("lakers", "celtics")),
    ("compare Paris, London and Rome", ("paris", "london", "rome")),
    ("weather in Denver", ()), ("rock and roll history", ()), ("top 10 cryptocurrencies by market cap", ()),
    ("snow or rain tomorrow", ()),
])
def test_parse_entities(text, want):
    assert parse_entities(text) == want


@pytest.mark.parametrize("text,want", [
    ("how many active hurricanes in the Pacific", "basin"), ("storms in the Atlantic", "basin"),
    ("outages nationwide", "national"), ("how many flights delayed across the US", "national"),
    ("worldwide cases", "world"), ("snow in the Midwest", "region"),
    ("weather in Pacific Time", None), ("flood warnings in Houston", None), ("Pacific Beach surf", None),
])
def test_parse_area(text, want):
    assert parse_area(text) == want


def test_parse_ask_collects_every_class():
    ask = parse_ask("how many earthquakes above 4.0 in the Pacific tonight")
    assert (ask.window, ask.threshold, ask.area, ask.counting) == ("tonight", (">", 4.0), "basin", True)
    assert parse_ask("weather in Denver vs Salt Lake City").entities == ("denver", "salt lake city")


# ---- class 1: time window --------------------------------------------------------------------------

NOW_EVENING = _utc("2026-10-06T02:45")    # Mon Oct 5, 21:45 in Chicago (still Oct 6 in UTC)
NOW_AFTER_MIDNIGHT = _utc("2026-10-06T06:30")  # Tue Oct 6, 01:30 in Chicago


def test_window_span_tonight_before_and_after_midnight():
    start, end = window_span("tonight", NOW_EVENING, CHI)
    assert (start, end) == (datetime(2026, 10, 5, 17, tzinfo=CHI), datetime(2026, 10, 6, 6, tzinfo=CHI))
    # 01:30 local: still the night that began on the 5th
    assert window_span("tonight", NOW_AFTER_MIDNIGHT, CHI) == (start, end)
    # 07:00 local: the next night starts that evening
    assert window_span("tonight", _utc("2026-10-06T12:00"), CHI)[0] == datetime(2026, 10, 6, 17, tzinfo=CHI)


def test_window_span_today_follows_the_user_zone_not_utc():
    # 21:45 CDT on the 5th is already the 6th in UTC: "today" is still the 5th for the user
    assert window_span("today", NOW_EVENING, CHI)[0] == datetime(2026, 10, 5, 0, tzinfo=CHI)
    assert window_span("today", NOW_EVENING, UTC)[0] == datetime(2026, 10, 6, 0, tzinfo=UTC)
    # Tokyo is a day ahead of Chicago at the same instant
    assert window_span("today", NOW_EVENING, TOKYO)[0].day == 6


def test_window_span_tomorrow_yesterday_last_night_weekend():
    assert window_span("tomorrow", NOW_EVENING, CHI)[0] == datetime(2026, 10, 6, 0, tzinfo=CHI)
    assert window_span("yesterday", NOW_EVENING, CHI)[0] == datetime(2026, 10, 4, 0, tzinfo=CHI)
    assert window_span("last_night", NOW_EVENING, CHI)[0] == datetime(2026, 10, 4, 17, tzinfo=CHI)
    mon = window_span("weekend", NOW_EVENING, CHI)       # Monday -> the coming Fri 18:00 .. Mon 00:00
    assert mon == (datetime(2026, 10, 9, 18, tzinfo=CHI), datetime(2026, 10, 12, 0, tzinfo=CHI))
    sunday = window_span("weekend", _utc("2026-10-11T18:00"), CHI)   # a Sunday asks about the weekend underway
    assert sunday == mon


@pytest.mark.parametrize("ask_text,moments,flagged", [
    ("NHL games tonight", ["2026-10-06T23:00"], True),                  # tomorrow evening Chicago
    ("games tonight", ["2026-10-06T23:00", "2026-10-07T00:30"], True),
    ("what's on tonight", ["2026-10-06T01:00"], False),                  # 20:00 Chicago
    ("what's on tonight", ["2026-10-06T01:00", "2026-10-07T23:00"], False),  # one in the window is enough
    ("tonight's games", ["2026-10-06T12:00"], True),                     # 07:00 Chicago on the 6th is after the night
    ("games today", ["2026-10-06T23:00"], True),                         # the 6th, but today is the 5th for the user
    ("games today", ["2026-10-06T01:00"], False),                        # 20:00 Chicago on the 5th
    ("games tomorrow", ["2026-10-06T23:00"], False),
    ("what's on", ["2026-10-06T23:00"], False),                          # no window asked
])
def test_check_window_evening(ask_text, moments, flagged):
    got = check_window(parse_ask(ask_text), _result(*moments), NOW_EVENING, CHI)
    assert bool(got) is flagged, got


def test_check_window_after_midnight_edge():
    # 01:30 local on the 6th: "tonight" is the night of the 5th, "today" is the 6th
    late = _result("2026-10-06T04:00")   # 23:00 Chicago on the 5th
    assert check_window(parse_ask("games tonight"), late, NOW_AFTER_MIDNIGHT, CHI) == []
    assert check_window(parse_ask("games today"), late, NOW_AFTER_MIDNIGHT, CHI) != []
    # the same instant read in a UTC clock would call it today: the zone is what decides
    assert check_window(parse_ask("games today"), late, NOW_AFTER_MIDNIGHT, UTC) == []


def test_check_window_weekend_dates_and_silence():
    ask = parse_ask("snow forecast this weekend")
    days = Result(spans=tuple(iso_span(d, CHI) for d in ("2026-10-10", "2026-10-11")), text="x")
    weekdays = Result(spans=tuple(iso_span(d, CHI) for d in ("2026-10-06", "2026-10-07")), text="x")
    assert check_window(ask, days, NOW_EVENING, CHI) == []
    assert check_window(ask, weekdays, NOW_EVENING, CHI) != []
    assert check_window(ask, Result(text="no times"), NOW_EVENING, CHI) == []   # nothing to judge


# ---- class 2: numeric threshold ---------------------------------------------------------------------

QUAKES = Result(fields={"mag": (2.6, 4.3, 4.4, 5.1)}, text="quakes")
SOURCE_25 = SourceInfo(name="USGS M2.5+ earthquakes, past day",
                       url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson")
SOURCE_45 = SourceInfo(name="USGS M4.5+ earthquakes, past day",
                       url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")


@pytest.mark.parametrize("ask_text,fields,flagged", [
    ("how many earthquakes above 4.0 today", {"mag": (2.6, 4.3, 4.4)}, True),
    ("earthquakes over 4", {"mag": (4.3, 4.4)}, False),
    ("quakes at least 4.3", {"mag": (4.3, 4.4)}, False),
    ("quakes at least 4.4", {"mag": (4.3, 4.4)}, True),
    ("earthquakes magnitude below 3", {"mag": (2.6, 2.9)}, False),
    ("earthquakes below 3", {"mag": (2.6, 3.5)}, True),
    ("days with temperature above 80", {"temperature_2m": (70.0, 85.0)}, True),
    ("days with temperature above 80", {"temperature_2m": (81.0, 85.0), "humidity": (10.0,)}, False),
    ("earthquakes above 4.0", {"depth": (2.0, 3.0)}, False),    # no column the bound is about: silent
])
def test_check_threshold_columns(ask_text, fields, flagged):
    got = check_threshold(parse_ask(ask_text), Result(fields=fields, text="x"), SourceInfo())
    assert bool(got) is flagged, got


def test_check_threshold_floor_of_the_feed_when_no_column():
    bare = Result(text="12 earthquakes")
    ask = parse_ask("how many earthquakes above 4.0")
    assert check_threshold(ask, bare, SOURCE_25) != []         # a 2.5+ feed cannot have applied "above 4.0"
    assert check_threshold(ask, bare, SOURCE_45) == []
    assert check_threshold(ask, bare, SourceInfo()) == []      # unknown floor: silent
    assert check_threshold(parse_ask("how many earthquakes below 4.0"), bare, SOURCE_25) == []
    assert source_floor(SOURCE_25) == 2.5 and source_floor(SourceInfo(name="Quakes")) is None


def test_check_threshold_no_bound_is_silent():
    assert check_threshold(parse_ask("earthquakes today"), QUAKES, SOURCE_25) == []


# ---- class 3: compare count -------------------------------------------------------------------------

@pytest.mark.parametrize("ask_text,card,flagged", [
    ("weather in Denver vs Salt Lake City", "temperature 65 denver (co)", True),
    ("weather in Denver vs Salt Lake City", "denver 65 salt lake city 71", False),
    ("Chiefs versus Bills", "kansas city chiefs 24 buffalo bills 20", False),
    ("Chiefs versus Bills", "kansas city chiefs 24", True),
    ("compare Ethereum and Solana prices", "ethereum 2400", True),
    ("compare Ethereum and Solana prices", "Ethereum 2400 Solana 150".lower(), False),
    ("compare Paris, London and Rome", "paris london", True),
    ("weather in Denver", "denver", False),                       # not a comparison
    ("compare Ethereum and Solana prices", "", False),            # nothing shown, nothing to judge
])
def test_check_compare(ask_text, card, flagged):
    got = check_compare(parse_ask(ask_text), Result(text=card))
    assert bool(got) is flagged, got


def test_check_compare_names_whole_words_only():
    # "bills" must not match inside "billsville"
    assert check_compare(parse_ask("Chiefs vs Bills"), Result(text="chiefs billsville")) != []


# ---- class 4: source scope --------------------------------------------------------------------------

POINT = SourceInfo(name="NWS alerts for a location", scope="place", params=frozenset({"lat", "lon"}))
GLOBAL = SourceInfo(name="NHC storms", scope="global")


@pytest.mark.parametrize("ask_text,source,flagged", [
    ("how many active hurricanes in the Pacific", POINT, True),
    ("number of storms in the Atlantic", POINT, True),
    ("how many outages nationwide", POINT, True),
    ("total cases worldwide", SourceInfo(scope="global", params=frozenset({"zip"})), True),
    ("how many outages across the US", SourceInfo(url="https://x.test/alerts?point=1,2"), True),
    ("how many active hurricanes in the Pacific", GLOBAL, False),
    ("how many flood warnings in Houston", POINT, False),            # a place, not a wide area
    ("active hurricanes in the Pacific", POINT, False),              # not a count
    ("how many earthquakes today", POINT, False),
])
def test_check_scope(ask_text, source, flagged):
    got = check_scope(parse_ask(ask_text), source)
    assert bool(got) is flagged, got


# ---- reading a built result --------------------------------------------------------------------------

def test_scan_reads_stamps_epochs_pairs_and_columns():
    preview = {"rows": [
        {"dateEvent": "2026-10-06", "strTime": "23:00:00", "dateEventLocal": "2026-10-06", "strTimeLocal": "19:00:00"},
        {"properties": {"mag": 4.3, "time": 1_791_252_129_190}},
        {"startTime": "2026-10-05T22:00:00-05:00", "temperature": 60},
    ], "time": ["2026-10-05T00:00", "2026-10-05"]}
    spans, cols = scan(preview, CHI)
    stamps = sorted(s[0].astimezone(UTC).isoformat() for s in spans)
    assert "2026-10-06T23:00:00+00:00" in stamps               # date + clock pair, UTC
    assert "2026-10-06T03:00:00+00:00" in stamps               # offset stamp
    assert cols["mag"] == [4.3] and cols["temperature"] == [60.0]
    assert any(s[0].astimezone(UTC).year == 2026 and s[0].astimezone(UTC).day == 6 for s in spans)  # epoch ms


def test_scan_a_date_and_clock_row_makes_one_moment_not_a_whole_day():
    row = {"dateEvent": "2026-10-06", "dateEventLocal": "2026-10-06", "strTime": "23:00:00"}
    spans, _cols = scan({"rows": [row]}, CHI)
    assert len(spans) == 1 and spans[0][0] == spans[0][1]
    spans, _cols = scan({"rows": [{"day": "2026-10-06"}]}, CHI)   # a bare date spans its whole day
    assert len(spans) == 1 and spans[0][0] != spans[0][1]


def test_read_result_joins_texts_and_keeps_extra_spans():
    extra = _span("2026-10-06T01:00")
    got = read_result({"a": 1}, ["Denver (CO)", "Temp"], CHI, [extra])
    assert got.text == "denver (co) temp" and extra in got.spans and got.fields == {"a": (1.0,)}


def test_iso_span_zone_and_garbage():
    day = iso_span("2026-10-05", CHI)
    assert day[1] - day[0] == (datetime(2026, 10, 6, tzinfo=CHI) - datetime(2026, 10, 5, tzinfo=CHI))
    naive = iso_span("2026-10-05T22:00", CHI)
    assert naive[0].utcoffset() is not None and naive[0].astimezone(UTC).hour == 3
    assert iso_span("not a date", CHI) is None and iso_span("2026-13-45", CHI) is None


def test_shadow_reasons_groups_by_class_and_is_empty_for_a_good_card():
    bad = shadow_reasons("how many earthquakes above 4.0 today", QUAKES, SOURCE_25, NOW_EVENING, CHI)
    assert set(bad) == {"threshold"}
    ok = shadow_reasons("how many earthquakes above 4.0 today", Result(fields={"mag": (4.3,)}, text="x"),
                        SOURCE_45, NOW_EVENING, CHI)
    assert ok == {}
    several = shadow_reasons("how many hurricanes in the Pacific vs Atlantic tonight", _result("2026-10-08T00:00"),
                             POINT, NOW_EVENING, CHI)
    assert {"window", "scope"} <= set(several)
