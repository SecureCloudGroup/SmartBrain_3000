"""§29 stage 2 (locate) works inside the ask's FRAME: a category and a question kind are established (from the
ask, else the intent's hint) or locate fails closed; relevance is category membership or the source's own whole
words; the question kind must be one the source serves; a named place is part of the frame whatever the policy;
an entity reading must be cued, allowed and agree with a named league.

The pack here is a small REAL DuckDB file in the schema ``sourcetool build`` produces, shaped like the pinned
pack's records. The labeled set (tests/fixtures/library_locate/labeled.json, drawn from the root-cause class test
sets) runs against the real pinned pack when ``SMARTBRAIN_TEST_LIBRARY_DIR`` points at an installed one."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import library_index
from smartbrain_3000.library_resolve import Resolver

_LABELED = Path(__file__).parent / "fixtures" / "library_locate" / "labeled.json"


def _pol(match, resolvers=(), prefer=("official", "primary", "aggregator", "community")):
    return {"prefer": list(prefer), "match": match, "resolvers": list(resolvers), "max_km": 30, "differ_on": []}


_SPORTS = ("team_mlb", "team_nhl", "team_espn", "sports_league")
_TAXONOMY = [  # category, subcategory, kinds, keywords, policy
    ("weather", "forecast", ["forecast"], ["forecast", "weather", "rain"], _pol("geo", ["place"])),
    ("weather", "alerts", ["alerts", "status"], ["warning", "alert", "tornado"], _pol("geo", ["place"])),
    ("hazards", "tropical_storms", ["latest_items", "alerts"], ["hurricane", "tropical storm"], _pol("none")),
    ("hazards", "space_weather", ["current_value", "forecast", "alerts"], ["geomagnetic", "aurora", "space weather"],
     _pol("none")),
    ("water", "surf_waves", ["current_value", "forecast"], ["surf", "wave height"], _pol("geo", ["place"])),
    ("sky", "iss", ["current_value", "next_event"], ["iss", "space station"], _pol("none")),
    ("sky", "near_earth", ["latest_items", "next_event"], ["asteroid"], _pol("none")),
    ("markets", "stocks", ["current_value"], ["stock", "nasdaq"], _pol("name", ["ticker"], ("primary", "official"))),
    ("markets", "indices", ["current_value"], ["nasdaq composite", "dow"], _pol("name", ["ticker"])),
    ("sports", "scores", ["current_value", "latest_items"], ["score", "game"],
     _pol("name", _SPORTS, ("primary", "official", "aggregator", "community"))),
    ("sports", "schedules", ["schedule", "next_event"], ["schedule", "next game"],
     _pol("name", _SPORTS, ("primary", "official", "aggregator", "community"))),
    ("sports", "standings", ["ranking"], ["standings"], _pol("name", _SPORTS)),
    ("travel", "airport_delays", ["status"], ["airport delay"], _pol("name", ["airport"])),
    ("travel", "transit", ["next_event", "status"], ["metro", "subway"], _pol("geo", ["place"])),
    ("travel", "transit_alerts", ["alerts", "status"], ["delay", "service alert"], _pol("geo", ["place"])),
]

_LATLON = [{"name": "lat", "fill": {"from": "resolver", "resolver": "place", "field": "lat"}},
           {"name": "lon", "fill": {"from": "resolver", "resolver": "place", "field": "lon"}}]


def _res(resolver, field="key"):
    return [{"name": "k", "fill": {"from": "resolver", "resolver": resolver, "field": field}}]


def _src(sid, name, cat, kinds, url, params=(), *, desc="", authority="official", prior=1.0, entity="",
         audience="", words=(), examples=()):
    return {"id": sid, "name": name, "cat": cat, "kinds": kinds, "url": url, "params": list(params),
            "desc": desc or name, "authority": authority, "prior": prior, "entity": entity, "audience": audience,
            "answers": [{"name": "a", "label": name, "words": list(words)}] if words else [],
            "examples": list(examples)}


_SOURCES = [
    _src("open-meteo-forecast", "Open-Meteo forecast", "weather/forecast", ["forecast", "current_value"],
         "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}", _LATLON, authority="primary",
         words=["temperature", "rain"]),
    _src("nws-alerts-point", "NWS active alerts for a location", "weather/alerts", ["alerts"],
         "https://api.weather.gov/alerts/active?point={lat},{lon}", _LATLON, words=["warnings", "watches"]),
    _src("swpc-kp-forecast", "Planetary K-index forecast", "hazards/space_weather", ["forecast"],
         "https://services.swpc.noaa.gov/kp.json", desc="Kp 5 or more is a geomagnetic storm (aurora outlook).",
         words=["geomagnetic storm", "aurora", "northern lights"]),
    _src("nhc-current-storms", "NHC active tropical cyclones", "hazards/tropical_storms", ["latest_items", "alerts"],
         "https://www.nhc.noaa.gov/CurrentStorms.json", words=["hurricanes", "tropical storms", "active storms"]),
    _src("tropical-alerts-point", "NWS tropical watches and warnings at a location", "hazards/tropical_storms",
         ["alerts"], "https://api.weather.gov/alerts/active?point={lat},{lon}&event=Hurricane%20Warning", _LATLON,
         prior=0.5, words=["hurricane warning", "hurricane", "tropical storm warning"]),
    _src("ndbc-buoy-realtime", "NDBC buoy observations", "water/surf_waves", ["current_value", "trend"],
         "https://www.ndbc.noaa.gov/data/realtime2/{lat}_{lon}.txt", _LATLON, audience="marine",
         words=["wave height", "swell"]),
    _src("wheretheiss-now", "ISS position now", "sky/iss", ["current_value", "map"],
         "https://api.wheretheiss.at/v1/satellites/25544", authority="community",
         words=["where is the iss", "position"]),
    _src("nasa-neows-feed", "Near-Earth asteroids this week", "sky/near_earth", ["latest_items", "next_event"],
         "https://api.nasa.gov/neo/rest/v1/feed", words=["asteroids", "near miss", "flyby"]),
    _src("finnhub-quote", "Finnhub stock quote", "markets/stocks", ["current_value"],
         "https://finnhub.io/api/v1/quote?symbol={k}", _res("ticker"), authority="aggregator", prior=1.5,
         words=["stock price", "quote"]),
    _src("fred-nasdaqcom", "Nasdaq Composite (daily close) (FRED)", "markets/indices", ["current_value", "trend"],
         "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM", entity="NASDAQCOM",
         examples=["nasdaq composite"]),
    _src("thesportsdb-team-next", "Team's next matchup", "sports/schedules", ["next_event", "schedule"],
         "https://www.thesportsdb.com/api/v1/json/3/eventsnext.php?id={k}", _res("team_espn", "attrs.tsdb_id"),
         authority="community", prior=2.0, entity="NFL, NBA, MLB, NHL teams", words=["next game", "who plays"]),
    _src("mlb-team-schedule", "MLB team schedule", "sports/schedules", ["schedule", "next_event"],
         "https://statsapi.mlb.com/api/v1/schedule?teamId={k}", _res("team_mlb"), authority="primary", prior=1.0,
         words=["next game", "schedule"]),
    _src("mlb-team-results", "MLB team recent results", "sports/scores", ["latest_items", "current_value"],
         "https://statsapi.mlb.com/api/v1/schedule?teamId={k}&past=1", _res("team_mlb"), authority="primary",
         words=["score", "final", "won"]),
    _src("nhl-score-now", "NHL scores today", "sports/scores", ["current_value", "latest_items"],
         "https://api-web.nhle.com/v1/score/now", authority="primary", words=["hockey scores", "scores"]),
    _src("league-last", "Latest game in a league", "sports/scores", ["latest_items", "current_value"],
         "https://www.thesportsdb.com/api/v1/json/3/eventspastleague.php?id={k}", _res("sports_league"),
         authority="community", entity="one league: NFL, NBA, MLB, NHL", words=["scores", "latest game"]),
    _src("mlb-standings", "MLB standings", "sports/standings", ["ranking"],
         "https://statsapi.mlb.com/api/v1/standings", authority="primary", words=["standings"]),
    _src("nhl-standings-now", "NHL standings", "sports/standings", ["ranking"],
         "https://api-web.nhle.com/v1/standings/now", authority="primary", prior=1.4, words=["standings"]),
    _src("soccer-table", "Soccer league table", "sports/standings", ["ranking"],
         "https://www.thesportsdb.com/api/v1/json/3/lookuptable.php?l={k}", _res("sports_league"),
         authority="community", entity="English Premier League", words=["table", "standings"]),
    _src("soccer-matches", "Soccer fixtures and results", "sports/scores", ["latest_items", "schedule"],
         "https://api.football-data.org/v4/competitions/{k}/matches", _res("soccer_competition"),
         authority="aggregator", words=["results", "fixtures"]),
    _src("bart-advisories", "BART service advisories", "travel/transit_alerts", ["alerts", "status"],
         "https://api.bart.gov/api/bsa.aspx?cmd=bsa&json=y", entity="BART, San Francisco Bay Area",
         words=["delays", "advisories"]),
    _src("cta-alerts", "CTA 'L' service alerts", "travel/transit_alerts", ["alerts", "status"],
         "https://www.transitchicago.com/api/1.0/alerts.aspx", entity="CTA 'L' trains, Chicago", prior=1.5,
         words=["delays", "alerts"]),
    _src("faa-nas-status", "FAA airport status", "travel/airport_delays", ["status", "alerts"],
         "https://nasstatus.faa.gov/api/airport-status-information?airport={k}", _res("airport"),
         words=["delays", "ground stop"]),
]

_ENTRIES = [  # id, resolver, key, name, lat, lon, state, attrs, rank, aliases
    # three small Bostons come first: the resolver's "largest of its name" pick sits past the first three ties
    ("place:b1", "place", "b1", "Boston", 36.0, -85.0, "KY", {"pop": 1200}, 3.0, ["boston", "boston ky"]),
    ("place:b2", "place", "b2", "Boston", 31.0, -83.8, "GA", {"pop": 1300}, 3.0, ["boston", "boston ga"]),
    ("place:b3", "place", "b3", "Boston", 38.0, -86.0, "IN", {"pop": 1400}, 3.0, ["boston", "boston in"]),
    ("place:b4", "place", "b4", "Boston", 42.36, -71.06, "MA", {"pop": 675000}, 3.0, ["boston", "boston ma"]),
    ("place:tulsa", "place", "tulsa", "Tulsa", 36.15, -95.99, "OK", {"pop": 413000}, 5.0, ["tulsa"]),
    ("place:kc", "place", "kc", "Kansas City", 39.1, -94.58, "MO", {"pop": 508000}, 5.0, ["kansas city"]),
    ("place:tampa", "place", "tampa", "Tampa", 27.95, -82.46, "FL", {"pop": 384000}, 5.0, ["tampa"]),
    ("place:vb", "place", "vb", "Virginia Beach", 36.85, -75.98, "VA", {"pop": 459000}, 5.0, ["virginia beach"]),
    ("place:dc", "place", "dc", "Washington", 38.9, -77.03, "DC", {"pop": 689000}, 5.0, ["washington", "dc"]),
    ("place:tornado", "place", "tornado", "Tornado", 38.3, -81.8, "WV", {"pop": 1000}, 3.0, ["tornado"]),
    ("place:ann", "place", "ann", "Annapolis", 38.98, -76.49, "MD", {"pop": 40000}, 4.0, ["annapolis"]),
    ("place:leb", "place", "leb", "Lake Erie Beach", 42.62, -79.07, "NY", {"pop": 3800}, 3.0,
     ["lake erie beach", "lake erie"]),
    # the pack's partial readings ("lake", "erie") outrank Cleveland among the resolver's first five
    ("place:lake", "place", "lake", "Lake", 32.3, -89.3, "MS", {"pop": 468}, 5.0, ["lake"]),
    ("place:erie1", "place", "erie1", "Erie", 40.05, -105.05, "CO", {"pop": 38594}, 5.0, ["erie"]),
    ("place:erie2", "place", "erie2", "Erie", 37.57, -95.24, "KS", {"pop": 1491}, 5.0, ["erie"]),
    ("place:erie3", "place", "erie3", "Erie", 41.66, -90.08, "IL", {"pop": 1035}, 5.0, ["erie"]),
    ("place:cle", "place", "cle", "Cleveland", 41.5, -81.69, "OH", {"pop": 372000}, 4.0, ["cleveland"]),
    ("place:league", "place", "league", "League City", 29.5, -95.09, "TX", {"pop": 118000}, 4.0,
     ["league city", "league"]),
    ("team_espn:bills", "team_espn", "football/nfl/2", "Buffalo Bills", None, None, "",
     {"league": "nfl", "tsdb_id": "134918"}, 1.0, ["buffalo bills", "bills"]),
    ("team_espn:chiefs", "team_espn", "football/nfl/12", "Kansas City Chiefs", None, None, "",
     {"league": "nfl", "tsdb_id": "134931"}, 1.0, ["kansas city chiefs", "chiefs"]),
    ("team_espn:wild", "team_espn", "hockey/nhl/30", "Minnesota Wild", None, None, "",
     {"league": "nhl", "tsdb_id": "134833"}, 1.0, ["minnesota wild", "wild"]),
    ("team_espn:dodgers", "team_espn", "baseball/mlb/19", "Los Angeles Dodgers", None, None, "",
     {"league": "mlb", "tsdb_id": "135272"}, 1.0, ["los angeles dodgers", "dodgers"]),
    ("team_mlb:119", "team_mlb", "119", "Los Angeles Dodgers", None, None, "", {"league": "mlb"}, 1.0,
     ["los angeles dodgers", "dodgers"]),
    ("sports_league:mlb", "sports_league", "4424", "MLB", None, None, "", {"league": "mlb", "sport": "baseball"},
     13.0, ["mlb", "major league baseball"]),
    ("sports_league:nhl", "sports_league", "4380", "NHL", None, None, "", {"league": "nhl", "sport": "hockey"},
     12.0, ["nhl", "national hockey league"]),
    ("sports_league:nfl", "sports_league", "4391", "NFL", None, None, "", {"league": "nfl", "sport": "football"},
     15.0, ["nfl", "national football league"]),
    ("sports_league:epl", "sports_league", "4328", "Premier League", None, None, "",
     {"league": "premier-league", "sport": "soccer"}, 10.0, ["premier league", "epl"]),
    ("ticker:NDAQ", "ticker", "NDAQ", "Nasdaq, Inc.", None, None, "", {"exchange": "NASDAQ"}, 1.0,
     ["nasdaq", "ndaq", "nasdaq inc"]),
    ("airport:KHQZ", "airport", "KHQZ", "Mesquite Metro Airport", 32.7, -96.5, "TX", {}, 1.0,
     ["mesquite metro", "metro"]),
    ("airport:ORD", "airport", "ORD", "Chicago O'Hare International Airport", 41.97, -87.9, "IL", {}, 1.0,
     ["ord", "o hare", "o hare airport"]),
    # the competition's "champions league" is a partial alias (a prefix of "champions league ucl"); the
    # league reading names it in full
    ("soccer_competition:CL", "soccer_competition", "CL", "UEFA Champions League", None, None, "", {}, 1.0,
     ["champions league ucl", "uefa champions league"]),
    ("sports_league:4480", "sports_league", "4480", "UEFA Champions League", None, None, "",
     {"league": "champions-league", "sport": "soccer"}, 5.0, ["champions league", "uefa champions league"]),
]


def _build_pack(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE library_sources(
        id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, provider_id VARCHAR, provider_name VARCHAR,
        authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR, access_kind VARCHAR, url_template VARCHAR,
        docs_url VARCHAR, auth VARCHAR, terms_status VARCHAR, cadence VARCHAR, validation_status VARCHAR,
        robots VARCHAR, votes_yes INTEGER, votes_no INTEGER, prior DOUBLE, record JSON, kinds VARCHAR[],
        role VARCHAR, audience VARCHAR)""")
    con.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con.execute("CREATE TABLE library_taxonomy(category VARCHAR, subcategory VARCHAR, label VARCHAR, "
                "kinds VARCHAR[], params VARCHAR[], keywords VARCHAR[], policy JSON)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    con.execute("CREATE TABLE library_resolver_entries(id VARCHAR PRIMARY KEY, resolver VARCHAR, kind VARCHAR, "
                "key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, state VARCHAR, attrs JSON, rank DOUBLE)")
    con.execute("CREATE TABLE library_resolver_aliases(alias VARCHAR, entry_id VARCHAR, partial BOOLEAN)")
    con.execute("CREATE TABLE library_source_resolvers(source_id VARCHAR, resolver VARCHAR)")
    for cat, sub, kinds, kw, pol in _TAXONOMY:
        con.execute("INSERT INTO library_taxonomy VALUES (?,?,?,?,?,?,?)",
                    (cat, sub, f"{cat.title()} › {sub}", kinds, [], kw, json.dumps(pol)))
    for s in _SOURCES:
        cat, sub = s["cat"].split("/")
        params = [{"kind": "x", "required": True, **p} for p in s["params"]]
        acc = {"kind": "http_json", "auth": "none", "headers": {}, "params": params, "url_template": s["url"]}
        rec = {"id": s["id"], "name": s["name"], "description": s["desc"], "tier": "curated",
               "categories": [s["cat"]], "kinds": s["kinds"], "access": acc, "examples": s["examples"],
               "answers": s["answers"], "coverage": {"entity": s["entity"], "geo": "US"},
               "provider": {"id": "p", "name": "Provider", "authority": s["authority"]}}
        con.execute("INSERT INTO library_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["name"], s["desc"], "p", "Provider", s["authority"], "curated", "US", s["entity"],
                     "http_json", s["url"], "https://example.org", "none", "public_domain", "hourly", "ok", "allow",
                     0, 0, s["prior"], json.dumps(rec), s["kinds"], "", s["audience"]))
        con.execute("INSERT INTO library_source_categories VALUES (?,?,?)", (s["id"], cat, sub))
        words = library_index.tokens(" ".join([s["name"], s["desc"], *s["examples"],
                                               *(w for a in s["answers"] for w in a["words"])]))
        for t in dict.fromkeys(words):
            con.execute("INSERT INTO library_terms VALUES (?,?,?)", (t, s["id"], 2.0))
        for p in params:
            con.execute("INSERT INTO library_source_resolvers VALUES (?,?)", (s["id"], p["fill"]["resolver"]))
    for eid, res, key, name, lat, lon, st, attrs, rank, aliases in _ENTRIES:
        con.execute("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, res, res, key, name, lat, lon, st, json.dumps(attrs), rank))
        for a in aliases:
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,?)", (a, eid, False))
    con.execute("INSERT INTO library_resolver_aliases VALUES ('champions league', 'soccer_competition:CL', TRUE)")
    con.execute("INSERT INTO library_meta VALUES ('built_at','2026-09-29T00:00:00Z'), ('records','20')")
    con.close()


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> library_index.LibraryIndex:
    tmp = tmp_path_factory.mktemp("locate")
    _build_pack(tmp / "src.duckdb")
    payload = gzip.compress((tmp / "src.duckdb").read_bytes())
    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(payload),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


def _ids(lib, ask, hint=None) -> list[str]:
    return [c["source_id"] for c in lib.candidates(ask, hint=hint)[0]]


def _hint(subject, wants, place=None, frame_kind=None) -> dict:
    return {"subject": subject, "wants": wants, "place": place, "frame_kind": frame_kind, "window": None}


# --- the frame kind: a closed vocabulary, parsed by code ---------------------------------------------

def test_frame_kinds_are_a_closed_vocabulary() -> None:
    assert library_index.FRAME_KINDS == ("current_value", "next_event", "schedule", "forecast", "result", "trend",
                                         "ranking", "latest_items", "alerts", "status", "count")


@pytest.mark.parametrize("ask, kind", [
    ("Bills score", "result"), ("did the Chiefs win", "result"), ("how did the Eagles do", "result"),
    ("Lakers final score last night", "result"), ("last SpaceX launch", "result"),
    ("Chiefs next game", "next_event"), ("when can I see the ISS from Kansas City", "next_event"),
    ("Lakers game time", "next_event"), ("what time is high tide in Savannah", "next_event"),
    ("how many days until Christmas", "next_event"), ("Grand Geyser prediction", "next_event"),
    ("Seahawks schedule", "schedule"), ("is it gonna storm in Tulsa tonight", "forecast"),
    ("will it rain tomorrow in Denver", "forecast"), ("blizzard warning Buffalo", "alerts"),
    ("how many earthquakes today", "count"), ("is GitHub down", "status"), ("BART delays?", "status"),
    ("NBA standings", "ranking"), ("bitcoin price chart", "trend"), ("where is the ISS right now", "current_value"),
    ("tech news", "latest_items"), ("ISS passes over Denver", "next_event"),
    ("hurricanes right now", None),  # "right now" is a window, not a kind
    # no cue: the kind stays open rather than guessed ("latest X" is often a current value)
    ("weather in Boise", None), ("latest macOS version", None), ("what time is it in Tokyo", None),
    ("tide table Charleston", None), ("tidal current at the Golden Gate", None), ("Lakers game tonight", None),
])
def test_frame_kind_from_text(ask, kind) -> None:
    assert library_index.frame_kind_from_text(ask) == kind


# --- C1: the frame is established or locate fails closed ------------------------------------------------

def test_no_category_fails_closed_and_the_hint_can_establish_it(lib) -> None:
    """Blind 2026-09-29: "is it gonna storm in Tulsa tonight" had no category, so every gate turned off and
    space-weather sources (a "geomagnetic storm") won. No category now means no Library candidate."""
    rows, skipped = lib.candidates("is it gonna storm in Tulsa tonight")
    assert rows == [] and skipped == ["couldn't tell what kind of data this is"]
    rows, _ = lib.candidates("is it gonna storm in Tulsa tonight",
                             hint=_hint("weather", ["storm chance"], "Tulsa", "forecast"))
    assert rows and rows[0]["source_id"] == "open-meteo-forecast" and "Tulsa" in rows[0]["label"]
    assert not any(r["source_id"].startswith("swpc") for r in rows)
    assert all(r["frame_kind"] == "forecast" for r in rows)


def test_a_declared_phrase_counts_only_whole() -> None:
    """"storm" inside "geomagnetic storm" and "iss" inside "near miss" are not the ask's words."""
    own = "Kp 5 or more is a geomagnetic storm (aurora outlook)"
    assert not library_index._speaks_to({"storm"}, own, ["geomagnetic storm"], "is it gonna storm in tulsa")
    assert library_index._speaks_to({"storm"}, own, ["geomagnetic storm"], "geomagnetic storm tonight")
    assert not library_index._speaks_to({"iss"}, "asteroids near miss", ["near miss"], "iss over denver")
    assert library_index._speaks_to({"hurricane"}, "Hurricanes and tropical storms", [], "hurricane tampa")


# --- C2: membership passes; the audience gate yields to the asked subcategory --------------------------

def test_a_member_of_the_asked_subcategory_passes_on_membership(lib) -> None:
    """Blind: "how's the surf at Virginia Beach" dropped the NDBC buoy — "surf" isn't in its own words and
    its marine audience flag dropped it anyway, though it is filed under surf & waves."""
    assert "ndbc-buoy-realtime" in _ids(lib, "how's the surf at Virginia Beach")


def test_a_word_that_names_a_sibling_tells_the_members_apart(lib) -> None:
    """Membership answers for "delays", not for "BART": the CTA's alerts are not BART's."""
    assert _ids(lib, "BART delays?") == ["bart-advisories"]


# --- C3: the frame's kind must be one the source serves ----------------------------------------------

def test_a_result_frame_never_gets_a_next_game_source(lib) -> None:
    rows, skipped = lib.candidates("Bills score")
    assert "thesportsdb-team-next" not in [r["source_id"] for r in rows]
    assert rows or skipped  # an empty result with ranked rows always says why
    assert _ids(lib, "Chiefs next game")[0] == "thesportsdb-team-next"


def test_when_can_i_see_the_iss_is_not_where_is_it(lib) -> None:
    assert "wheretheiss-now" not in _ids(lib, "when can I see the ISS from Kansas City")
    assert "nasa-neows-feed" not in _ids(lib, "when will the ISS pass over Kansas City")
    rows, _ = lib.candidates("where is the ISS right now")
    assert rows[0]["source_id"] == "wheretheiss-now" and rows[0]["scope"] == "global"


# --- C4: the place is the resolver's decision -------------------------------------------------------

def test_named_place_starts_from_the_resolvers_decision(lib) -> None:
    """Blind: "Boston weather this weekend" found no place — the dominant Boston sat past the first three
    ties — so every weather source was dropped, silently."""
    with lib._conn() as con:
        words, named, status = library_index.named_place(Resolver(con), "Boston weather this weekend")
        assert words == {"boston"} and named and status == "resolved"
        assert library_index.named_place(Resolver(con), "bitcoin price") == (set(), False, "none")
        # a small place without a cue is the ask's place when the intent says so
        assert library_index.named_place(Resolver(con), "water temp Annapolis")[1] is False
        assert library_index.named_place(Resolver(con), "water temp Annapolis", "Annapolis, MD") == (
            {"annapolis"}, True, "resolved")
        # a place the pack can't settle is named, never dropped; "near me" names none
        assert library_index.named_place(Resolver(con), "sunrise in Reykjavik", "Reykjavik") == (
            set(), True, "unresolved")
        assert library_index.named_place(Resolver(con), "weather near me", "near me")[1] is False
    rows, _ = lib.candidates("Boston weather this weekend")
    assert rows[0]["source_id"] == "open-meteo-forecast" and rows[0]["label"] == "Boston (MA)"
    assert rows[0]["scope"] == "place"


def test_a_longer_name_does_not_hide_the_place(lib) -> None:
    """Labeled: "Lake Erie water temp Cleveland" named no place — the resolver's longest reading, the small
    Lake Erie Beach, hid Cleveland — so every place source asked to name the place."""
    with lib._conn() as con:
        words, named, _ = library_index.named_place(Resolver(con), "Lake Erie wave height Cleveland")
    assert words == {"cleveland"} and named


def test_a_place_inside_a_named_subjects_name_is_that_subject(lib) -> None:
    """"premier league standings" is the Premier League, not League City, TX (a place said as part of a longer
    named subject)."""
    rows, _ = lib.candidates("premier league standings")
    assert "soccer-table" in [c["source_id"] for c in rows]
    assert {c["scope"] for c in rows} == {"global"}


def test_locate_and_fill_agree_on_the_place(lib) -> None:
    """Live: "tornado reports today" filled nws-alerts-point with Tornado, WV — a place the ask never named.
    A source that needs a place is offered only when locate finds one named."""
    rows, skipped = lib.candidates("tornado warning today")
    assert rows == [] and any("name the place" in s for s in skipped)
    assert _ids(lib, "tornado warning in Tulsa") == ["nws-alerts-point"]


# --- C5: a named place is part of the frame whatever the policy -------------------------------------

def test_a_named_place_leads_with_a_place_scoped_source_and_tags_the_rest_global(lib) -> None:
    rows, _ = lib.candidates("any hurricanes headed for Tampa?")
    assert rows[0]["source_id"] == "tropical-alerts-point" and rows[0]["scope"] == "place"
    storms = [r for r in rows if r["source_id"] == "nhc-current-storms"]
    assert storms and storms[0]["scope"] == "global"


# --- C6: entities are cued, allowed by the frame and agree with the named league -------------------------

def test_an_index_is_not_the_company_that_shares_its_name(lib) -> None:
    rows = _ids(lib, "how's the Nasdaq doing today")
    assert rows and rows[0] == "fred-nasdaqcom" and "finnhub-quote" not in rows
    assert _ids(lib, "NDAQ") == ["finnhub-quote"]
    assert "finnhub-quote" in _ids(lib, "Nasdaq Inc stock")


def test_a_team_must_agree_with_the_named_league(lib) -> None:
    """Blind: "MLB wild card standings" read "wild" as the Minnesota Wild and the entity-only gate left
    nothing. The league is the subject; a source about another league is not a candidate."""
    rows = _ids(lib, "MLB wild card standings")
    assert rows and rows[0] == "mlb-standings" and "nhl-standings-now" not in rows
    wild = _ids(lib, "Minnesota Wild score")
    assert "nhl-score-now" in wild and not any(r.startswith("mlb") for r in wild)


def test_an_entity_the_category_does_not_take_is_not_the_subject(lib) -> None:
    """Blind: "DC metro red line delays" became an airport reading ("Metro" airport, cued by "delays")."""
    assert "faa-nas-status" not in _ids(lib, "DC metro red line delays")
    assert _ids(lib, "delays at ORD") == ["faa-nas-status"]  # a typed code is the entity
    # so is a name that says its kind (corpus: "delays at Newark airport" got nothing)
    assert _ids(lib, "delays at O'Hare airport") == ["faa-nas-status"]


def test_a_reading_said_by_part_of_its_name_joins_the_one_named_in_full(lib) -> None:
    """Corpus: "Champions League results" lost football-data-matches — the competition's own "champions
    league" alias is a partial one, so only the league reading was kept."""
    assert _ids(lib, "Champions League scores") == ["soccer-matches"]


def test_an_ing_word_says_its_keyword(lib) -> None:
    """Corpus: "Is it raining in Seattle right now?" couldn't tell what kind of data it was."""
    assert _ids(lib, "is it raining in Tulsa")[0] == "open-meteo-forecast"


# --- C15: among sources for the same subject, the policy's authority order leads -----------------------

def test_the_primary_source_leads_for_the_same_subject(lib) -> None:
    rows = _ids(lib, "next Dodgers game")
    assert rows[:2] == ["mlb-team-schedule", "thesportsdb-team-next"]


def test_every_candidate_carries_scope_and_frame_kind(lib) -> None:
    rows, _ = lib.candidates("Chiefs next game")
    assert all(r["scope"] in ("place", "global") and r["frame_kind"] == "next_event" for r in rows)


# --- the labeled set, against the real pinned pack ---------------------------------------------------

_PACK_DIR = os.environ.get("SMARTBRAIN_TEST_LIBRARY_DIR", "")


def _match(sid: str, pats: list[str]) -> bool:
    return any(sid == p or (p.endswith("*") and sid.startswith(p[:-1])) for p in pats)


def _grade(idx: library_index.LibraryIndex, row: dict) -> tuple[bool | None, bool | None]:
    """(forbidden admission?, admissible?) for one labeled row; None where the row doesn't label it."""
    rows, _ = idx.candidates(row["ask"], hint=row.get("hint"))
    ids = [r["source_id"] for r in rows]
    bad = None
    if "forbid" in row or "tops" in row:
        bad = any(_match(i, row.get("forbid", [])) for i in ids) or any(
            row.get("tops") and not {c.split("/")[0] for c in r["categories"]} & set(row["tops"]) for r in rows)
    ok = None
    if "admit" in row:
        ok = any(i in row["admit"] for i in ids)
    if "first" in row:
        ok = ok is not False and bool(ids) and ids[0] in row["first"]
    if row.get("label_has") and rows:
        ok = ok is not False and row["label_has"] in (rows[0]["label"] or "")
    if row.get("scope"):
        ok = ok is not False and bool(rows) and rows[0]["scope"] == row["scope"]
    if row.get("scope_all"):
        ok = ok is not False and all(r["scope"] == row["scope_all"] for r in rows)
    return bad, ok


@pytest.mark.skipif(not (_PACK_DIR and (Path(_PACK_DIR) / "library" / "library.duckdb").exists()),
                    reason="set SMARTBRAIN_TEST_LIBRARY_DIR to an installed pinned pack")
def test_the_labeled_locate_set_on_the_pinned_pack() -> None:
    idx = library_index.LibraryIndex(Path(_PACK_DIR))
    rows = json.loads(_LABELED.read_text())["rows"]
    forbidden, admitted, labeled, frames, framed, controls = [], 0, 0, [], 0, []
    for row in rows:
        failed = False
        if "frame" in row:
            framed += 1
            if library_index.frame_kind_from_text(row["ask"]) != row["frame"]:
                frames.append(row["ask"])
                failed = True
        if "place" in row:  # a place named where none is, is a forbidden admission; a missed one, a recall miss
            with idx._conn() as con:
                words = library_index.named_place(Resolver(con), row["ask"])[0]
            if not row["place"] and words:
                forbidden.append(row["ask"])
            elif row["place"]:
                labeled += 1
                admitted += set(row["place"]) <= words
            failed = failed or bool(words) != bool(row["place"]) or not set(row["place"]) <= words
        if any(k in row for k in ("admit", "first", "forbid", "tops", "scope", "scope_all", "label_has")):
            bad, ok = _grade(idx, row)
            if bad:
                forbidden.append(row["ask"])
            if ok is not None:
                labeled += 1
                admitted += ok
            failed = failed or bool(bad) or ok is False
        if row.get("control") and failed:
            controls.append(row["ask"])
    assert forbidden == [], forbidden
    assert admitted >= 0.95 * labeled, (admitted, labeled)
    assert controls == [], controls
    assert len(frames) <= 0.05 * framed, frames


def test_the_frame_holds_with_the_librarys_vectors(lib) -> None:
    """Locate v2: with an embedder wired (a stub here) but a pack without example asks there is no route, so the
    keyword frame decides exactly as without vectors: the named subject first, no space weather for a storm
    question with no category, a place-needing source only for a named place."""
    from test_library_locate2 import _Stub

    from smartbrain_3000 import library_embed

    library_embed.forget()
    library_embed.set_provider(_Stub().embedder)
    try:
        assert library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn, wait=True) is not None
        assert _ids(lib, "BART delays?")[0] == "bart-advisories"
        assert lib.candidates("is it gonna storm in Tulsa tonight")[0] == []
        rows, skipped = lib.candidates("tornado warning today")
        assert rows == [] and any("name the place" in s for s in skipped)
        assert _ids(lib, "Chiefs next game")[0] == "thesportsdb-team-next"
    finally:
        library_embed.set_provider(None)
        library_embed.forget()
