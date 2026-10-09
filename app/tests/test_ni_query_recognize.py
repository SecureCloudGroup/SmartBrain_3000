"""Round 20 Q3a: the query layer's recognizers (``ni_query.recognize``) — table-driven over the
109 gold asks, plus the Q0 bake-off's measured failure probes.

Every gold ask with an explicit time phrase must yield the gold time through
``normalize.explicit_time`` (the recognizer's token read by the question kind's direction);
the exceptions are listed with their reason, never skipped silently. Asks without one must
yield no token at all (the model's token then stands).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from zoneinfo import ZoneInfo

import pytest

from smartbrain_3000.ni_query import recognize
from smartbrain_3000.ni_query.normalize import explicit_time
from smartbrain_3000.ni_query.recognize import words

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ni_query"
_ZONE = "America/New_York"
_THU = dt.datetime(2026, 10, 8, 9, 0, tzinfo=ZoneInfo(_ZONE))
# label variance and out-of-scope rows: the recognizer reads the phrase one way, the gold label another
EXCEPTIONS = {
    "B26": "'over the last year': both libraries read calendar 2025 (year:2025); gold labels it rolling past_days:365",
    "E27": "'over the last year': calendar 2025 vs the gold's rolling past_days:365 (same class as B26)",
    "C27": "'over the past month': both libraries read calendar September (month:2026-09); gold rolling past_days:30",
    "D45": "noted row (source_wrong: people-in-space has no position) — gold leaves time null despite 'right now'",
    "E36": "noted row (source_wrong: a point alert feed) — gold leaves time null despite 'right now'",
    "E44": "noted row (compare: 'now versus a year ago' is two points; out of IR scope)",
}
# asks with no explicit time phrase: no token, the model's reading stands
NO_PHRASE = frozenset({
    "A3", "A6", "A8", "A10", "A18", "A22", "A25", "A29", "A30", "A34", "A49", "B2", "B22", "B29", "B32", "B44", "B45",
    "C6", "C9", "C21", "C31", "C34", "C45", "D3", "D7", "D12", "D13", "D14", "D18", "D21", "D22", "D24", "D25", "D33",
    "D36", "E3", "E12", "E13", "E22", "E26", "E31", "E33", "E38"})


def _gold() -> list[dict]:
    rows = [json.loads(line) for line in (_FIX / "gold.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == 109, "the gold set holds 109 rows"
    return rows


def _now(row: dict) -> dt.datetime:
    split = json.loads((_FIX / "split.json").read_text())
    day = dt.date.fromisoformat(split["ref_dates"][row["set"]])
    return dt.datetime.combine(day, dt.time(9, 0), tzinfo=ZoneInfo(split["zone"]))


@pytest.mark.parametrize("row", _gold(), ids=lambda r: r["id"])
def test_gold_time_phrases(row):
    """An explicit phrase yields the gold time (or a listed exception); none yields no token."""
    got = explicit_time(recognize(row["ask"], now=_now(row), zone=_ZONE), row["kind"])
    if row["id"] in NO_PHRASE:
        assert got is None, f"{row['id']}: no explicit phrase, yet the recognizers produced {got}"
    elif row["id"] in EXCEPTIONS:
        assert got is not None and got != row["ir"]["time"], f"{row['id']} is a listed exception: {got}"
    else:
        assert got == row["ir"]["time"], f"{row['id']} {row['ask']!r}: {got} != gold {row['ir']['time']}"


def test_exception_list_is_exact():
    """Exactly 60 explicit rows match; the exception list names every other explicit row."""
    rows = _gold()
    explicit = [r for r in rows if r["id"] not in NO_PHRASE]
    assert len(explicit) == 66 and len(NO_PHRASE) == 43, "66 explicit rows, 43 without a phrase"
    assert set(EXCEPTIONS) <= {r["id"] for r in explicit}, "exceptions are explicit rows"


def _texts(spans: dict, key: str = "time") -> list[str]:
    return [s["text"] for s in spans[key]]


def test_word_boundaries_kill_substring_hits():
    """'now' in "snow", 'cent' in "recent", 'Sa' in "Salt Lake" never surface."""
    snow = recognize("snow forecast for Tahoe this weekend", now=_THU, zone=_ZONE)
    assert _texts(snow) == ["this weekend"], "only the weekend phrase"
    recent = recognize("recent earthquakes in Alaska", now=_THU, zone=_ZONE)
    assert recent["numbers"] == [] and recent["time"] == [], "no 'cent' number, no time"
    salt = recognize("weather in Salt Lake City", now=_THU, zone=_ZONE)
    assert salt["time"] == [], "no 'Sa' (Saturday)"


def test_capital_after_unit_drops_the_unit():
    """'magnitude 4 in Alaska' is the number 4, not four inches; the comparator reads it."""
    spans = recognize("recent quakes stronger than magnitude 4 in Alaska", now=_THU, zone=_ZONE)
    assert [(n["value"], n["unit"]) for n in spans["numbers"]] == [(4, None)], spans["numbers"]
    assert [(c["op"], c["value"], c["text"]) for c in spans["comparators"]] == [(">", 4, "stronger than magnitude 4")]
    inches = recognize("at least 3 inches of snow", now=_THU, zone=_ZONE)
    assert [(c["op"], c["value"], c["unit"]) for c in inches["comparators"]] == [(">=", 3, "Inch")]


def test_holiday_with_the_asked_year():
    """'Thanksgiving in 2027' → the 2027 holiday (ms alone says 2026) and the year phrase."""
    spans = recognize("when is Thanksgiving in 2027", now=_THU, zone=_ZONE)
    kinds = {s["kind"]: s for s in spans["time"]}
    assert kinds["holiday"]["from"].startswith("2027-11-25") and kinds["holiday"]["lib"] == "puckling"
    assert kinds["range"]["token"] == "year:2027", kinds
    assert explicit_time(spans, "lookup") == {"from": "year:2027", "to": "year:2027"}


def test_seasons_and_places_are_no_holidays():
    """puckling labels seasons and 'Corpus Christi' holidays; they are dropped whole."""
    for ask in ("fall foliage forecast", "winter storm warnings in Colorado", "Corpus Christi weather"):
        assert recognize(ask, now=_THU, zone=_ZONE)["time"] == [], ask


def test_past_months_run_backward():
    """'past 12 months' is the last 365 days, ending now — never a year ahead."""
    spans = recognize("consumer price index over the past 12 months", now=_THU, zone=_ZONE)
    (span,) = spans["time"]
    assert span["token"] == "past_days:365" and span["from"] < span["to"] <= _THU.isoformat()
    assert explicit_time(spans, "trend") == {"from": "past_days:365", "to": "today"}
    hours = recognize("quakes in the last 24 hours", now=_THU, zone=_ZONE)
    assert [s["token"] for s in hours["time"]] == ["past_hours:24"], hours["time"]


def test_this_weekend_is_a_range():
    spans = recognize("rain this weekend", now=_THU, zone=_ZONE)
    (span,) = spans["time"]
    assert (span["kind"], span["token"]) == ("range", "weekend"), span
    assert span["from"].startswith("2026-10-10") and span["to"].startswith("2026-10-12")


def test_cadence_is_not_a_time_token():
    """'every 30 minutes' is a duration: no token, no override, its number dropped."""
    spans = recognize("temperature in Boston every 30 minutes", now=_THU, zone=_ZONE)
    assert [s["kind"] for s in spans["time"]] == ["duration"] and spans["time"][0]["token"] is None
    assert explicit_time(spans, "current_value") is None and spans["numbers"] == []


def test_this_week_reads_by_direction():
    spans = recognize("games this week", now=_THU, zone=_ZONE)
    assert explicit_time(spans, "forecast") == {"from": "today", "to": "next_days:7"}
    assert explicit_time(spans, "schedule") == {"from": "today", "to": "this_week"}
    assert explicit_time(spans, "count") == {"from": "past_days:7", "to": "today"}


def test_agreement_drops_one_library_spans():
    """puckling-only 'sun' (Sunday) and ms-only 'the first quarter' (Q1) are not time."""
    sun = recognize("how strong is the sun's UV in Honolulu at noon", now=_THU, zone=_ZONE)
    assert [s["text"] for s in sun["time"]] == ["noon"] and sun["time"][0]["token"] is None
    moon = recognize("when does the first quarter moon fall this month", now=_THU, zone=_ZONE)
    assert [s["token"] for s in moon["time"]] == ["this_month"], moon["time"]


def test_absorbed_preposition_is_cut():
    """puckling reads 'Boston tomorrow' as 'on tomorrow'; the span is cut to the word."""
    spans = recognize("what time is sunrise in Boston tomorrow", now=_THU, zone=_ZONE)
    assert [(s["text"], s["token"]) for s in spans["time"]] == [("tomorrow", "tomorrow")]


def test_masked_ranges_drop_spans():
    ask = "aqi in 98101 right now"
    bare = recognize(ask, now=_THU, zone=_ZONE)
    assert [n["value"] for n in bare["numbers"]] == [98101], "the zip reads as a number unmasked"
    masked = recognize(ask, now=_THU, zone=_ZONE, masked=[(7, 12)])
    assert masked["numbers"] == [] and [s["token"] for s in masked["time"]] == ["now"]


def test_comparator_table():
    cases = {"how many earthquakes above 4.0 today": (">", 4), "earthquakes within 50 miles of reno": ("<=", 50),
             "fewer than ten games": ("<", 10), "coins at most 2 dollars": ("<=", 2)}
    for ask, want in cases.items():
        got = [(c["op"], c["value"]) for c in recognize(ask, now=_THU, zone=_ZONE)["comparators"]]
        assert got == [want], f"{ask}: {got}"
    far = recognize("rain over the next three days", now=_THU, zone=_ZONE)
    assert far["comparators"] == [], "a number inside a time phrase is no comparator"


def test_words_tokenizer():
    assert [w for w, _, _ in words("O'Hare's 4.5+ temps")] == ["o", "hare", "s", "4", "5", "temps"]
    assert words("") == [], "empty text has no words"
