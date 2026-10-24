"""The ``stable`` check: a research card's address may not fix a day near today (tune3 D37), or a kept card reads
that sealed address every day and shows that day forever. A day far from now is one the ask named and stays."""

from __future__ import annotations

import pytest

from smartbrain_3000.ni_research import tools
from tests.test_ni_research_tools import NOW, FakeLibrary, Net, _ctx, _draft, _row


@pytest.mark.parametrize(("url", "fixed"), [
    ("https://earthquake.usgs.gov/fdsnws/event/1/query?starttime=2026-10-14&endtime=2026-10-21", "2026-10-14"),
    ("https://api.example.org/v1/day/20261021/scores.json", "20261021"),
    ("https://api.example.org/v1/games?date=2026%2F10%2F22", "2026/10/22"),
    ("https://api.example.org/v1/since?ts=1792584000", "1792584000"),          # epoch seconds at NOW
    ("https://api.example.org/v1/since?ts=1792584000000", "1792584000000"),    # ... and milliseconds
])
def test_an_address_that_fixes_a_day_near_now_is_found(url, fixed) -> None:
    assert tools._fixed_moment(url, NOW) == fixed


@pytest.mark.parametrize("url", [
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_week.geojson",   # always current
    "https://date.nager.at/api/v3/PublicHolidays/2027/US",                          # a year the ask named
    "https://api.example.org/v1/history?date=2026-07-04",                           # a day far from now
    "https://api.example.org/v1/item/12345678901234",                               # a long id, not a moment
])
def test_an_address_that_is_always_current_or_names_a_far_day_is_not(url) -> None:
    assert tools._fixed_moment(url, NOW) is None


def test_research_cannot_propose_a_card_whose_address_fixes_today(monkeypatch) -> None:
    """tune3 D37: a query that baked the past week's dates into its address would show that week forever once kept."""
    url = "https://feeds.example.org/quakes?starttime=2026-10-14&endtime=2026-10-21"
    Net(monkeypatch, {url: {"items": [{"title": "M4.5"}]}})
    ctx = _ctx(FakeLibrary([_row(url)]))
    assert tools.library_search(ctx, "quakes")["ok"] and tools.fetch_sample(ctx, url)["ok"]
    got = tools.try_recipe(ctx, _draft(url, {"title": "items[0].title"}))
    assert got["verdict"] == "fail" and any(r.startswith("stable: the address fixes the date 2026-10-14")
                                            for r in got["reasons"])


def test_an_always_current_address_passes_the_stable_check(monkeypatch) -> None:
    url = "https://feeds.example.org/quakes/all_week.json"
    Net(monkeypatch, {url: {"items": [{"title": "M4.5"}]}})
    ctx = _ctx(FakeLibrary([_row(url)]))
    assert tools.library_search(ctx, "quakes")["ok"] and tools.fetch_sample(ctx, url)["ok"]
    got = tools.try_recipe(ctx, _draft(url, {"title": "items[0].title"}))
    assert {"check": "stable", "ok": True, "reason": ""} in got["checks"]


def test_the_refresh_recheck_never_runs_the_stable_check() -> None:
    """A sealed address cannot change, and a card kept before the check existed must keep refreshing."""
    checks = tools.checks_for(_ctx(), {"title": "M4.5"}, "https://feeds.example.org/q?day=2026-10-21", {})
    assert "stable" not in {c["check"] for c in checks}
