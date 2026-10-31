"""A source's free request limits against a card's cadence (``ni_limits``), and where they reach a card: sealed with
the build (``_built_from.limits``), said on the board (``limit``), and the host's wait beside it (``waiting``).

T, 2026-10-31: "Prefer a source that fits". "NVDA stock price every 30 minutes" is 48 requests a day; Alpha Vantage's
free key allows 25, so a card built on it would stop updating from about midday, and nothing said so. The Library
pack v1.6.0 carries each provider's documented limits; the rule and the card's words are tested here, the pick order
over the pack's real rows in ``test_ni_proof_asks``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni, ni_flow, ni_limits, ni_polite, ni_routes
from smartbrain_3000.secrets import gen_master_key

AV = [{"requests": 1, "seconds": 1}, {"requests": 25, "seconds": 86400}]
FINNHUB = [{"requests": 60, "seconds": 60}]
AV_BLOCK = {"provider": "Alpha Vantage", "keyed": True, "windows": AV}
AV_LINE = ("Alpha Vantage's free key allows 25 requests a day. Refreshing every 30 minutes needs 48, so this card "
           "will stop updating partway through each day.")
AV_URL = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=NVDA"


# ---- the field as the pack has it -------------------------------------------------------------------------------

def test_clean_takes_the_packs_windows_shortest_first() -> None:
    assert ni_limits.clean(list(reversed(AV))) == AV
    assert ni_limits.clean(FINNHUB) == FINNHUB
    four = [{"requests": n, "seconds": s} for n, s in ((1, 1), (60, 60), (1000, 3600), (10_000_000, 31 * 86400))]
    assert ni_limits.clean(four) == four, "four windows, both ends of each bound"
    copy = ni_limits.clean(FINNHUB)
    copy[0]["requests"] = 1
    assert FINNHUB[0]["requests"] == 60, "a copy, never the pack's own dicts"


@pytest.mark.parametrize("limits", [
    None, "25/day", {"requests": 25, "seconds": 86400}, [], [{"requests": 1, "seconds": s} for s in (1, 2, 3, 4, 5)],
    [{"requests": 25}], [{"requests": 25, "seconds": 86400, "per": "key"}], [{"requests": 0, "seconds": 60}],
    [{"requests": 10_000_001, "seconds": 60}], [{"requests": 1, "seconds": 0}],
    [{"requests": 1, "seconds": 31 * 86400 + 1}], [{"requests": True, "seconds": 60}],
    [{"requests": 25.0, "seconds": 86400}], [{"requests": "25", "seconds": 86400}],
    [{"requests": 25, "seconds": 86400}, {"requests": 50, "seconds": 86400}],
    [{"requests": 25, "seconds": 86400}, "1/s"],
], ids=lambda v: repr(v)[:40])
def test_a_malformed_field_is_unknown_never_a_limit(limits) -> None:
    """All or nothing: pack data is never trusted to be well formed, and one bad window makes the whole field
    unknown, which counts as covering (``fits`` is None)."""
    assert ni_limits.clean(limits) == []
    assert ni_limits.block("Alpha Vantage", True, limits) is None
    assert ni_limits.fits(ni_limits.clean(limits), 30) is None


# ---- the rule ------------------------------------------------------------------------------------------------------

def test_a_card_makes_one_request_a_refresh_so_a_window_needs_its_length_over_the_cadence() -> None:
    assert ni_limits.need(86400, 30) == 48 and ni_limits.need(86400, 60) == 24
    assert ni_limits.need(86400, 58) == 25 and ni_limits.need(86400, 57) == 26, "25 a day: every 58 minutes"
    assert ni_limits.need(60, 30) == 1 and ni_limits.need(1, 15) == 1, "a window shorter than the cadence: one"
    assert ni_limits.need(3600, 15) == 4 and ni_limits.need(3601, 15) == 5, "a partial refresh still counts"
    assert ni_limits.effective_minutes(5, 15) == 15 and ni_limits.effective_minutes(30, 15) == 30
    assert ni_limits.effective_minutes(5, 0) == 5, "an own host has no floor"


def test_fits_says_whether_every_window_covers_the_cadence() -> None:
    assert ni_limits.fits(AV, 30) is False and ni_limits.fits(AV, 58) is True
    assert ni_limits.fits(FINNHUB, 1) is True, "60 a minute covers even every minute"
    assert ni_limits.fits([], 30) is None, "no window known: unknown, which the pick counts as covering"
    assert ni_limits.overrun(AV, 30) == {"requests": 25, "seconds": 86400, "need": 48}
    assert ni_limits.overrun(AV, 58) is None


def test_the_window_overrun_most_is_the_one_the_card_names() -> None:
    hour_worst = [{"requests": 2, "seconds": 3600}, {"requests": 50, "seconds": 86400}]
    assert ni_limits.overrun(hour_worst, 15) == {"requests": 2, "seconds": 3600, "need": 4}, "4/2 beats 96/50"
    day_worst = [{"requests": 10, "seconds": 3600}, {"requests": 100, "seconds": 86400}]
    assert ni_limits.overrun(day_worst, 5) == {"requests": 100, "seconds": 86400, "need": 288}, "288/100 beats 12/10"


# ---- what a build seals ------------------------------------------------------------------------------------------

def test_block_seals_who_sets_the_limit_and_whether_it_is_per_key() -> None:
    assert ni_limits.block("Alpha Vantage", {"in": "query", "name": "apikey"}, list(reversed(AV))) == AV_BLOCK
    keyless = ni_limits.block("  Open \n Data  ", None, FINNHUB)
    assert keyless == {"provider": "Open Data", "keyed": False, "windows": FINNHUB}
    assert len(ni_limits.block("x" * 500, True, FINNHUB)["provider"]) == ni_limits.MAX_PROVIDER
    assert ni_limits.block(None, True, FINNHUB)["provider"] == ""
    assert ni_limits.valid_block(AV_BLOCK) and ni_limits.valid_block(keyless)


@pytest.mark.parametrize("value", [
    None, [], {**AV_BLOCK, "extra": 1}, {k: v for k, v in AV_BLOCK.items() if k != "keyed"},
    {**AV_BLOCK, "provider": 7}, {**AV_BLOCK, "provider": "x" * 121}, {**AV_BLOCK, "keyed": "yes"},
    {**AV_BLOCK, "windows": []}, {**AV_BLOCK, "windows": list(reversed(AV))},
    {**AV_BLOCK, "windows": [{"requests": 0, "seconds": 1}]},
], ids=lambda v: repr(v)[:40])
def test_only_exactly_what_block_seals_is_a_sealed_limit(value) -> None:
    assert ni_limits.valid_block(value) is False
    assert ni_limits.line(value, 30) is None


# ---- what the card says ------------------------------------------------------------------------------------------

def test_the_line_names_the_limit_what_the_cadence_needs_and_what_happens() -> None:
    assert ni_limits.line(AV_BLOCK, 30) == AV_LINE
    assert ni_limits.line(AV_BLOCK, 60) is None and ni_limits.line(AV_BLOCK, 58) is None, "it covers: nothing to say"
    assert ni_limits.line({"provider": "Finnhub", "keyed": True, "windows": FINNHUB}, 1) is None


@pytest.mark.parametrize(("block", "cadence", "said"), [
    ({"provider": "Open Data Portal", "keyed": False, "windows": [{"requests": 10, "seconds": 86400}]}, 60,
     "Open Data Portal allows 10 requests a day without a key. Refreshing every hour needs 24, so this card will "
     "stop updating partway through each day."),
    ({"provider": "NOAA Tides", "keyed": True, "windows": [{"requests": 1, "seconds": 3600}]}, 15,
     "NOAA Tides' free key allows 1 request an hour. Refreshing every 15 minutes needs 4, so this card will stop "
     "updating partway through each hour."),
    ({"provider": "", "keyed": True, "windows": [{"requests": 5, "seconds": 7200}]}, 15,
     "This source's free key allows 5 requests every 2 hours. Refreshing every 15 minutes needs 8, so some of its "
     "refreshes will be refused."),
    ({"provider": "Big Data", "keyed": True, "windows": [{"requests": 1000, "seconds": 31 * 86400}]}, 15,
     "Big Data's free key allows 1,000 requests a month. Refreshing every 15 minutes needs 2,976, so this card "
     "will stop updating partway through each month."),
    ({"provider": "Weekly Co", "keyed": False, "windows": [{"requests": 100, "seconds": 7 * 86400}]}, 60,
     "Weekly Co allows 100 requests a week without a key. Refreshing every hour needs 168, so this card will stop "
     "updating partway through each week."),
    ({"provider": "Odd", "keyed": False, "windows": [{"requests": 1, "seconds": 5400}]}, 30,
     "Odd allows 1 request every 90 minutes without a key. Refreshing every 30 minutes needs 3, so some of its "
     "refreshes will be refused."),
    ({"provider": "Slow", "keyed": True, "windows": [{"requests": 1, "seconds": 172800}]}, 1440,
     "Slow's free key allows 1 request every 2 days. Refreshing every day needs 2, so some of its refreshes will be "
     "refused."),
    ({"provider": "Q", "keyed": True, "windows": [{"requests": 3, "seconds": 86400}]}, 90,
     "Q's free key allows 3 requests a day. Refreshing every 90 minutes needs 16, so this card will stop updating "
     "partway through each day."),
])
def test_the_line_reads_as_a_person_would_say_it(block, cadence, said) -> None:
    assert ni_limits.line(block, cadence) == said
    assert len(said) <= 400, "the board row's bound"


# ---- sealed with the build ---------------------------------------------------------------------------------------

def _store() -> ni.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ni.NIStore(conn, gen_master_key())


def _row(**over) -> dict:
    return {"source_id": "alphavantage-quote", "provider": "Alpha Vantage", "url": AV_URL,
            "needs_key": {"in": "query", "name": "apikey"}, "limits": AV, **over}


def test_a_pick_seals_its_rows_limits_and_a_build_from_that_address_carries_them() -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "NVDA stock price every 30 minutes")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("NVDA stock price every 30 minutes", "source"))
    ni_flow.seal_library_pick(store, item_id, AV_URL, _row())
    assert ni_flow._flow_read(store, item_id)["_library_limits"] == AV_BLOCK
    built = ni_flow._built_from_of(store, item_id, "mapping", AV_URL, "Global quote")
    assert built == {"path": "mapping", "host": "www.alphavantage.co", "title": "Global quote", "limits": AV_BLOCK}
    ni._validate_built_from(built)
    other = ni_flow._built_from_of(store, item_id, "research", "https://quotes.example.org/nvda.json", "Quote")
    assert "limits" not in other, "a source research found is not the Library row: its limits are not known"
    ni_flow.seal_library_pick(store, item_id, "https://finnhub.io/api/v1/quote?symbol=NVDA",
                              _row(source_id="finnhub-quote", url="https://finnhub.io/api/v1/quote?symbol=NVDA",
                                   limits=None, provider="Finnhub"))
    assert "_library_limits" not in ni_flow._flow_read(store, item_id), "a re-pick keeps no earlier row's limits"
    record = ni_flow._flow_read(store, item_id)
    ni_flow._flow_write(store, item_id, {**record, "_library_limits": {**AV_BLOCK, "windows": [{"requests": 0}]}})
    assert "limits" not in ni_flow._built_from_of(store, item_id, "mapping",
                                                  "https://finnhub.io/api/v1/quote?symbol=NVDA"), \
        "a tampered record's limits are never sealed"


def test_the_sealed_marker_takes_limits_only_in_the_shape_block_seals() -> None:
    base = {"path": "mapping", "host": "www.alphavantage.co", "title": "Global quote"}
    ni._validate_built_from(base)
    ni._validate_built_from({**base, "limits": AV_BLOCK})
    for bad in ({**base, "limits": None}, {**base, "limits": {**AV_BLOCK, "windows": list(reversed(AV))}},
                {**base, "limits": AV_BLOCK, "other": 1}, {"path": "mapping", "host": "h", "limits": AV_BLOCK}):
        with pytest.raises(ValueError, match="limits"):
            ni._validate_built_from(bad)


# ---- on the board ------------------------------------------------------------------------------------------------

def _item(interval: int, url: str = AV_URL, limits: dict | None = AV_BLOCK, **spec) -> dict:
    built = {"path": "mapping", "host": "www.alphavantage.co", "title": "Global quote"}
    if limits is not None:
        built["limits"] = limits
    return {"id": "c1", "interval_minutes": interval,
            "spec": {"source": {"type": "http_json", "url": url}, "_built_from": built, **spec}}


def test_the_board_line_uses_the_cadence_the_card_really_runs_at() -> None:
    assert ni_routes._limit_line(_item(30)) == AV_LINE
    assert ni_routes._limit_line(_item(60)) is None, "every hour: Alpha Vantage's 25 a day cover it"
    floored = ni_routes._limit_line(_item(5))
    assert floored is not None and "Refreshing every 15 minutes needs 96" in floored, "held to the third-party floor"
    assert ni_routes._limit_line(_item(30, limits=None)) is None, "no limit known: nothing to say"
    own = _item(5, url="http://quotes.local/nvda", limits={**AV_BLOCK, "windows": [{"requests": 100,
                                                                                    "seconds": 86400}]})
    assert "Refreshing every 5 minutes needs 288" in ni_routes._limit_line(own), "an own host has no floor"


@pytest.fixture
def polite_clock(monkeypatch):
    clock = SimpleNamespace(now=5000.0)
    monkeypatch.setattr(ni_polite, "_clock", lambda: clock.now)
    return clock


def test_pause_of_is_the_hosts_own_wait_and_nothing_else(polite_clock) -> None:
    gate = ni_polite.Politeness()
    assert gate.pause_of("www.alphavantage.co") is None
    for _ in range(ni_polite.HOST_BURST + 3):  # SmartBrain's own spacing runs dry: never the host's pause
        gate._take("www.alphavantage.co")
    assert gate.pause_of("www.alphavantage.co") is None
    gate.pause("www.alphavantage.co", 3600.0, "our standard API rate limit is 25 requests per day")
    left, said = gate.pause_of("WWW.AlphaVantage.co")
    assert left == 3600.0 and said == "our standard API rate limit is 25 requests per day"
    polite_clock.now += 3599.5
    assert gate.pause_of("www.alphavantage.co")[0] == pytest.approx(0.5)
    polite_clock.now += 0.5
    assert gate.pause_of("www.alphavantage.co") is None, "over at its end"


def test_the_board_carries_the_hosts_wait_for_the_cards_source(polite_clock) -> None:
    now = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
    item = _item(30)
    assert ni_routes._waiting_of(item, now=now) is None
    ni_polite.SHARED.pause("www.alphavantage.co", 3600.0, "x" * 400)
    waiting = ni_routes._waiting_of(item, now=now)
    assert waiting == {"host": "www.alphavantage.co", "until": "2026-10-31T19:00:00+00:00", "said": "x" * 300}
    assert ni_routes._waiting_of({**item, "spec": {**item["spec"], "_shell": True}}, now=now) is None, "a shell"
    templated = _item(30, url="https://{status_host}/api/v2/summary.json")
    assert ni_routes._waiting_of(templated, now=now) is None, "a host the card fills in later"
    assert ni_routes._waiting_of({"id": "c2", "interval_minutes": 30, "spec": {}}, now=now) is None, "no source"
    polite_clock.now += 3600.0
    assert ni_routes._waiting_of(item, now=now) is None


def test_a_shell_card_says_neither_while_its_build_runs(polite_clock) -> None:
    """A card still building has no source of its own yet: its flow says what it is doing (the row's ``flow``), and
    the board row carries neither field, even while a host it is trying holds SmartBrain off."""
    store = _store()
    item_id = ni_flow.create_shell_item(store, "NVDA stock price every 30 minutes")
    ni_polite.SHARED.pause("www.alphavantage.co", 60.0, "slow down")
    row = ni_routes._board_row(store, store.get_item(item_id))
    assert "limit" not in row and "waiting" not in row
