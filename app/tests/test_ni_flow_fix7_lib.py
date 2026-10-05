"""fix7-lib regressions (live blind6, 2026-10-04): a named subdivision scopes a list's rows
(NHL "metropolitan division"); the stray-subject check in ``_other_subject``'s entity_params
branch reads only words the user said (USD→MXN never carries the model's "conversion"); a
Library provider named in the ask that isn't the pick's provider is stray even when the
intent's ``names`` blank missed the lowercase outlet. Deterministic, no network, no model."""

from __future__ import annotations

import pytest

from smartbrain_3000 import ni_flow

# --- defect 1: a named subdivision scopes a list's rows ----------------------------------------

_NHL_ANSWER = {
    "name": "standings", "label": "League standings", "words": ["standings"],
    "primary": True, "kind": "list", "path": "standings",
    "cells": [{"path": "teamName.default", "label": "Team", "type": "text"},
              {"path": "points", "label": "Pts", "type": "number"}],
}
_NHL_SAMPLE = {"standings": [
    {"teamName": {"default": "Pittsburgh Penguins"},
     "divisionName": "Metropolitan", "conferenceName": "Eastern", "points": 4},
    {"teamName": {"default": "Edmonton Oilers"},
     "divisionName": "Pacific", "conferenceName": "Western", "points": 5},
    {"teamName": {"default": "Minnesota Wild"},
     "divisionName": "Central", "conferenceName": "Western", "points": 4},
    {"teamName": {"default": "New York Rangers"},
     "divisionName": "Metropolitan", "conferenceName": "Eastern", "points": 3},
]}


def test_a_named_subdivision_scopes_a_list_to_rows_that_name_it() -> None:
    """"NHL standings metropolitan division" scopes by divisionName; the sealed filter rides the
    pipeline so every refresh keeps only the Metropolitan rows (defect 1 — class fix)."""
    answer = ni_flow._scope_rows_to_place(_NHL_ANSWER, _NHL_SAMPLE, "metropolitan division")
    assert answer["filter"] == {"path": "divisionName", "equals": "Metropolitan"}


def test_a_named_conference_scopes_by_a_different_row_key() -> None:
    """"NHL standings eastern conference" scopes by conferenceName — the subdivision type word
    (``conference``) picks the row key, the value word picks the row value."""
    answer = ni_flow._scope_rows_to_place(_NHL_ANSWER, _NHL_SAMPLE, "eastern conference")
    assert answer["filter"] == {"path": "conferenceName", "equals": "Eastern"}


def test_a_named_subdivision_without_matching_rows_is_honest_nothing() -> None:
    """"Southern division" names no row of the NHL sample → the Library's nothing-signal rises
    so the caller moves on to the next source rather than shipping an unscoped list."""
    with pytest.raises(ValueError, match="has nothing for southern division"):
        ni_flow._scope_rows_to_place(_NHL_ANSWER, _NHL_SAMPLE, "southern division")


def test_a_place_with_no_subdivision_word_still_falls_through() -> None:
    """The aurora Kp "Minnesota" ask has no subdivision keyword — the function returns the
    answer unchanged, and the source-level note path still ships (planetary data policy)."""
    assert ni_flow._scope_rows_to_place(_NHL_ANSWER, _NHL_SAMPLE, "Minnesota") is _NHL_ANSWER


def test_subdivision_parse_requires_both_a_type_word_and_a_value() -> None:
    """"division" alone names no scope; "metropolitan" alone names no subdivision ask. Both halves
    required so a stray word doesn't bind a filter."""
    assert ni_flow._subdivision_in("division") is None
    assert ni_flow._subdivision_in("metropolitan") is None
    assert ni_flow._subdivision_in("metropolitan division") == ("division", "metropolitan")


def test_scoping_names_place_detects_row_filter_overlap() -> None:
    """A chosen answer whose row filter's value shares a word with the ask's place → the data is
    scoped. The verify step then drops the "isn't specific to" note from the policy path."""
    answer = {"kind": "list", "filter": {"path": "divisionName", "equals": "Metropolitan"}}
    assert ni_flow._scoping_names_place([answer], "metropolitan division") is True
    assert ni_flow._scoping_names_place([answer], "Pacific division") is False
    assert ni_flow._scoping_names_place([], "metropolitan division") is False


# --- defect 3: the entity_params ``_other_subject`` reads only words the user said -------------

def test_frankfurter_usd_mxn_doesnt_pick_up_the_models_conversion_subject() -> None:
    """USD→MXN with Frankfurter: the model title-cases its subject ("Currency Conversion"),
    but the user never typed "conversion" — the entity_params stray check reads request words
    only, so the ship goes through on the right pick (defect 3)."""
    frame = {"kind": None, "window": None, "place": None, "categories": [],
             "ask_categories": [], "about_categories": [], "lib": None}
    source = {
        "categories": ["markets/fx"],
        "name": "Frankfurter exchange rates",
        "description": "Latest ECB reference exchange rates from a base currency.",
        "examples": ["dollar to euro", "USD EUR exchange rate", "how many yen per dollar"],
        "coverage": {"entity": ""},
        "entity_params": {"base": "currency", "quote": "currency"},
        "readings": [], "label": "US dollar · Mexican peso",
        "provider": "Frankfurter",
    }
    intent = {"subject": "Currency Conversion", "wants": ["rate"], "names": []}
    out = ni_flow._other_subject(frame, source, {"base": "USD", "quote": "MXN"}, [],
                                  "USD to MXN", intent, about_named=True)
    assert out is None, out


def test_rangers_hockey_still_refuses_the_texas_rangers_row() -> None:
    """Request "Rangers hockey score" names "hockey", which the Library's sports/results keywords
    don't carry; the entity_params stray check keeps that word (not in own, not in kind, no name
    hit against the "Texas Rangers" label) and refuses. The team-row remains honest."""
    frame = {"kind": None, "window": None, "place": None, "categories": [],
             "ask_categories": [], "about_categories": [], "lib": None}
    source = {
        "categories": ["sports/results"],
        "name": "MLB team recent results",
        "description": "One MLB team's games over the past week, with the final score of each.",
        "examples": ["Yankees score", "did the Yankees win", "Mets final score"],
        "coverage": {"entity": ""},
        "entity_params": {"team": "team_mlb"},
        "readings": [], "label": "Texas Rangers (mlb)",
        "provider": "MLB",
    }
    intent = {"subject": "New York Rangers", "wants": ["score"], "names": []}
    out = ni_flow._other_subject(frame, source, {"team": "140"}, [],
                                  "what was the Rangers hockey score", intent, about_named=True)
    assert out is not None
    assert "hockey" in out, out


# --- defect 2: a Library provider named in the ask that isn't this source's provider ----------

class _ProviderLib:
    """Minimal Library stand-in for ``_stray_topics``' provider backstop."""

    def __init__(self, providers: list[str]) -> None:
        self._providers = list(providers)

    def taxonomy(self) -> list[dict]:
        return [{"id": "news", "subcategories": [
            {"id": "topic_news", "label": "Topic news",
             "keywords": ["news", "business", "headlines"]}]}]

    def classify(self, text: str, limit: int = 3) -> list[str]:
        return []

    def subcategory(self, _sub: str) -> dict:
        return {}

    def entity_vocabulary(self, _entity: str) -> set[str]:
        return set()

    def providers_named_in(self, request: str) -> set[str]:
        import re
        low = " " + " ".join(re.findall(r"[A-Za-z0-9]+", request.lower())) + " "
        return {p for p in self._providers
                if " " + p.lower() + " " in low}


def test_a_library_provider_named_in_the_ask_backstops_a_mismatched_pick() -> None:
    """"fox news business" names "Fox News" (a Library provider); the pick is NPR Business
    (provider NPR). The deterministic backstop in ``_stray_topics`` reads the Library vocabulary
    so the ask refuses even when the intent's ``names`` blank missed the lowercase outlet."""
    frame = {"kind": None, "window": None, "place": None, "categories": ["news/topic_news"],
             "ask_categories": ["news/topic_news"], "about_categories": ["news/topic_news"],
             "lib": _ProviderLib(["Fox News", "NPR", "ABC News"])}
    source = {"categories": ["news/topic_news"], "name": "NPR Business",
              "description": "NPR Business RSS feed.",
              "examples": ["business news"],
              "coverage": {"entity": ""}, "entity_params": {},
              "readings": [], "label": "", "provider": "NPR"}
    stray = ni_flow._stray_topics(frame, source, {}, [], "fox news business",
                                   {"subject": "business news", "wants": ["headlines"], "names": []})
    assert "fox" in stray, stray


# fix8 (blind-7, 2026-10-04): "Weather Channel 10 day for Asheville" shipped NWS
# because the pack doesn't index Weather Channel. The suffix / known-outlet set
# backstops the pack vocabulary — the stray-check refuses when the picked source
# isn't from the named outlet, even for outlets the pack hasn't indexed.
def test_outlet_suffix_name_backstops_pack_vocabulary() -> None:
    """"Weather Channel 10 day for Asheville" vs an NWS pick: 'channel' is an
    outlet-suffix word, so the backstop refuses NWS as a Weather Channel pick."""
    source = {"categories": ["weather"], "name": "NWS 7-day forecast",
              "description": "National Weather Service.", "examples": [],
              "coverage": {}, "entity_params": {},
              "readings": [], "label": "", "provider": "National Weather Service"}
    stray = ni_flow._foreign_providers_in(
        _ProviderLib([]), "Weather Channel 10 day for Asheville", source, set(),
        {}, {"names": ["Weather Channel"], "subject": "Asheville weather", "place": "Asheville"})
    assert "channel" in stray, stray


def test_outlet_suffix_on_its_own_source_is_not_stray() -> None:
    """An ask for Weather Channel against a TWC pick does not refuse itself."""
    source = {"categories": ["weather"], "name": "TWC forecast",
              "description": "Weather Channel.", "examples": [],
              "coverage": {}, "entity_params": {},
              "readings": [], "label": "", "provider": "Weather Channel"}
    stray = ni_flow._foreign_providers_in(
        _ProviderLib([]), "Weather Channel 10 day for Asheville", source, set(),
        {}, {"names": ["Weather Channel"], "subject": "Weather Channel", "place": "Asheville"})
    assert "channel" not in stray, stray


def test_the_pick_s_own_provider_is_not_stray() -> None:
    """An NPR Business pick for an ask that says "NPR" does not refuse itself — the backstop
    only reports providers that AREN'T this source's provider."""
    frame = {"kind": None, "window": None, "place": None, "categories": ["news/topic_news"],
             "ask_categories": ["news/topic_news"], "about_categories": ["news/topic_news"],
             "lib": _ProviderLib(["Fox News", "NPR", "ABC News"])}
    source = {"categories": ["news/topic_news"], "name": "NPR Business",
              "description": "NPR Business RSS feed.", "examples": ["business news"],
              "coverage": {"entity": ""}, "entity_params": {},
              "readings": [], "label": "", "provider": "NPR"}
    stray = ni_flow._stray_topics(frame, source, {}, [], "NPR business headlines",
                                   {"subject": "NPR business", "wants": ["headlines"], "names": []})
    assert "npr" not in stray, stray


def test_a_provider_phrase_is_its_whole_name_and_never_only_generic_words() -> None:
    """A provider counts as named only when its WHOLE name is in the ask, and a name made only of
    generic words never counts: "HG Weather" / "US Weather" / "News" / "PC World" matched every
    weather or news ask and refused right cards as "isn't about hg / pc" (live 2026-10-04)."""
    from smartbrain_3000 import library_index as li
    phrase = li.LibraryIndex._provider_phrase
    assert phrase("HG Weather") == " hg weather "  # every part, short ones included
    assert phrase("US Weather") is None and phrase("News") is None  # only generic words
    assert phrase("PC World") == " pc world "  # "bbc world news" doesn't hold " pc world "
    assert phrase("Fox News") == " fox news "


# fix8 (blind-7, 2026-10-04): a page card for an ask that names an outlet must
# come from that outlet's own site. The named-outlet set includes pack providers
# (``providers_named_in``) + an outlet-suffix set ("Weather Channel") + a bounded
# well-known-outlet set ("Axios Denver"). The first-party check accepts the
# outlet's own host and refuses a third-party page that merely mentions it.
def test_named_outlets_from_pack_providers_outlet_suffix_and_known_names(monkeypatch) -> None:
    monkeypatch.setattr(ni_flow, "_resolve_library",
                         lambda: _ProviderLib(["Fox News", "BBC"]))
    # pack provider named in the ask
    assert "Fox News" in ni_flow._named_outlets(
        "Fox News headlines",
        {"names": [], "subject": "Fox News"})
    # outlet-suffix word on intent.names ("Weather Channel" → "channel")
    assert "Weather Channel" in ni_flow._named_outlets(
        "Weather Channel 10 day for Asheville",
        {"names": ["Weather Channel"], "subject": "Asheville weather"})
    # bounded known-outlet set (pack doesn't know Axios yet)
    assert "Axios Denver" in ni_flow._named_outlets(
        "Axios Denver latest",
        {"names": ["Axios Denver"], "subject": "Axios Denver"})
    # a plain subject like NYC weather is NOT an outlet
    assert ni_flow._named_outlets(
        "NYC weather",
        {"names": ["NYC"], "subject": "NYC weather"}) == []


def test_page_wrong_outlet_refuses_third_party_host_and_accepts_first_party(monkeypatch) -> None:
    """A rockymountainvoice.com page for "Axios Denver" is wrong; axios.com for
    the same ask passes first_party and ships."""
    monkeypatch.setattr(ni_flow, "_resolve_library", lambda: _ProviderLib([]))
    intent = {"names": ["Axios Denver"], "subject": "Axios Denver"}
    assert ni_flow._page_wrong_outlet(
        "https://rockymountainvoice.com/category/axios-denver/",
        "Axios Denver latest", intent) == "Axios Denver"
    assert ni_flow._page_wrong_outlet(
        "https://www.axios.com/local/denver", "Axios Denver latest", intent) == ""
    # no outlet named → empty (the check doesn't fire for ordinary asks)
    assert ni_flow._page_wrong_outlet(
        "https://news.example.com/", "something else", {"names": [], "subject": "x"}) == ""
