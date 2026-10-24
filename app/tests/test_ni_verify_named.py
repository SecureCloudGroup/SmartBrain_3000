"""Class 5 (named): a card must come from the source the ask names (tune2, 2026-10-24: "AP top story" landed from
a BBC feed and the scorer counted it right). The rule reads names from how the ask is written and looks for them
in what the source served or how a trusted channel named it; there is no list of outlets."""

from __future__ import annotations

import json
import time

import pytest

from smartbrain_3000.ni_research import tools
from smartbrain_3000.ni_verify import named
from tests.test_ni_research_tools import FakeLibrary, Net, _ctx, _draft, _row

BBC = {"title": "BBC News - Home", "items": [
    {"title": "Ambulance Paramedics strike over pay", "link": "https://www.bbc.co.uk/news/1"},
    {"title": "Andrew Price speaks after the vote", "link": "https://www.bbc.co.uk/news/2"}]}
AP = {"title": "AP Top News", "items": [{"title": "Storm makes landfall", "link": "https://apnews.com/article/1"}]}


@pytest.mark.parametrize(("ask", "names", "owned", "want"), [
    ("AP top story", [], [], [("ap",)]),                                      # an acronym, even sentence-initial
    ("latest posts on Hacker News", [], [], [("hacker", "new")]),             # a mid-sentence name, generic word kept
    ("Dolphins score", [], [], []),                                           # a sentence-initial capital alone is no name
    ("Dolphins score", ["Dolphins"], [], [("dolphin",)]),                     # ... unless the intent named it
    ("weather in Salt Lake City", [], ["Salt Lake City"], []),                # the place class owns the place
    ("US gas price trend", [], ["United States"], []),                        # ... and its initials
    ("top 10 coins by price", [], [], []),                                    # a number is the threshold's, not a name
    ("latest GOES image", [], [], [("goes",)]),                               # an acronym is no plural
    ("latest News", [], [], []),                                              # a generic word alone is no name
    ("compare Nvidia and AMD", [], ["Nvidia", "AMD"], []),                    # the compare class owns compared things
])
def test_the_names_an_ask_carries(ask, names, owned, want) -> None:
    assert named.phrases(ask, names, owned) == want


@pytest.mark.parametrize(("ask", "names", "served", "identity"), [
    ("AP top story", [], AP, ["https://feeds.apnews.com/rss/apf-topnews", "AP Top News"]),          # its own word
    ("latest NPR headlines", [], {}, ["https://feeds.npr.org/1001/rss.xml", ""]),                     # a host label
    ("NHL games tonight", [], {"games": []}, ["https://api-web.nhle.com/v1/score/now", ""]),          # a label it begins
    ("is Cloudflare having issues", [], {}, ["https://www.cloudflarestatus.com/api/v2/summary.json", ""]),
    ("number of active NWS alerts", [], {}, ["https://api.weather.gov/alerts", "National Weather Service"]),
    ("today's NWS discussion", [], {"text": "AREA FORECAST DISCUSSION National Weather Service Seattle WA"},
     ["https://api.weather.gov/products/1", ""]),                                                    # spelt out, served
    ("latest posts on Hacker News", [], {"hits": []}, ["https://hn.algolia.com/api/v1/search", ""]),  # initials "hn"
    ("who won the Dolphins game", [], {"name": "Miami Dolphins at Buffalo Bills"},
     ["https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard", ""]),                 # served data
    ("open issues for kubernetes", ["kubernetes"], {"open_issues_count": 1883},
     ["https://api.github.com/repos/kubernetes/kubernetes", ""]),                                    # the address path
])
def test_a_source_that_carries_the_name_passes(ask, names, served, identity) -> None:
    assert named.check_named(named.phrases(ask, names), json.dumps(served), identity) == []


@pytest.mark.parametrize(("ask", "names", "served", "identity"), [
    ("AP top story", [], BBC, ["https://feeds.bbci.co.uk/news/rss.xml", "BBC News - Home"]),
    ("next F1 race", [], {"events": [{"strEvent": "Chiefs at Raiders"}]},
     ["https://www.thesportsdb.com/api/v1/json/123/eventsnextleague.php?id=4391", "TheSportsDB NFL"]),
    ("Weather Channel 10 day forecast", ["Weather Channel"], {"periods": []},
     ["https://api.weather.gov/gridpoints/GSP/1,2/forecast", "NWS forecast"]),
    ("reuters business news", ["reuters"], {"items": [{"title": "Markets rally"}]},
     ["https://feeds.npr.org/1006/rss.xml", "NPR Business"]),
])
def test_a_source_from_someone_else_is_refused(ask, names, served, identity) -> None:
    reasons = named.check_named(named.phrases(ask, names), json.dumps(served), identity)
    assert len(reasons) == 1 and "the ask names" in reasons[0]


def test_initials_by_chance_in_a_long_served_text_never_carry_a_name() -> None:
    """"Ambulance Paramedics" spells "ap" by chance; only a name of three or more capitals spells an acronym out of
    served text, and prefixes are read from the source's own name, never from what it served."""
    served = json.dumps(BBC) + " apple appeal apparatus " * 200
    assert named.check_named([("ap",)], served, ["https://feeds.bbci.co.uk/news/rss.xml"])
    assert named.check_named([("goes",)], "goethe goes going", ["https://example.org/x"])


def test_a_huge_served_text_is_read_in_bounded_time() -> None:
    served = ("Alpha Beta Gamma Delta " * 40000) + " ap"
    started = time.perf_counter()
    named.check_named([("zz",), ("qq",)], served, ["https://example.org/feed"])
    assert time.perf_counter() - started < 2.0


def _feed_ctx(monkeypatch, url: str, body: dict, title: str):
    net = Net(monkeypatch, {url: body})
    ctx = _ctx(FakeLibrary([_row(url, title=title, provider="")]))
    ctx.request = "AP top story"
    assert tools.library_search(ctx, "top story")["ok"]
    assert tools.fetch_sample(ctx, url)["ok"]
    return ctx, net


def test_research_cannot_propose_a_card_from_another_publisher(monkeypatch) -> None:
    url = "https://feeds.bbci.co.uk/news/top.json"
    ctx, _ = _feed_ctx(monkeypatch, url, BBC, "BBC News - Home")
    draft = _draft(url, {"title": "items[0].title"})
    got = tools.try_recipe(ctx, draft)
    assert got["verdict"] == "fail" and any(r.startswith("subject: the ask names 'ap'") for r in got["reasons"])
    refused = tools.propose_card(ctx, draft)
    assert refused["error"] == "verify_failed" and refused["reasons"] == got["reasons"]


def test_the_draft_cannot_name_the_publisher_itself(monkeypatch) -> None:
    """The card's title and outputs are the model's words: a draft titled "AP top story" over a BBC feed is still
    the BBC's card."""
    url = "https://feeds.bbci.co.uk/news/top.json"
    ctx, _ = _feed_ctx(monkeypatch, url, BBC, "BBC News - Home")
    draft = _draft(url, {"AP": "items[0].title"}, title="AP top story")
    assert tools.try_recipe(ctx, draft)["verdict"] == "fail"


def test_research_proposes_the_named_publishers_card(monkeypatch) -> None:
    url = "https://feeds.apnews.com/top.json"
    ctx, _ = _feed_ctx(monkeypatch, url, AP, "AP Top News")
    draft = _draft(url, {"title": "items[0].title"})
    assert tools.try_recipe(ctx, draft)["verdict"] == "pass"
    assert tools.propose_card(ctx, draft)["ok"]


def test_a_refresh_never_judges_the_publisher_again() -> None:
    """The address is sealed at Keep: a fresh payload cannot change who publishes it, so the refresh re-check runs
    the same classes as before and never the named one."""
    ctx = _ctx()
    ctx.request = "AP top story"
    checks = tools.checks_for(ctx, {"title": "Storm"}, "https://feeds.bbci.co.uk/news/top.json", {})
    assert "subject" not in {c["check"] for c in checks}
