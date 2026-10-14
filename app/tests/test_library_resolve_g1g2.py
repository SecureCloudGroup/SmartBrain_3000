"""Round 20 resolver G1+G2 (blind50g review, 2026-10-12): three silent-wrong classes in
``library_resolve.Resolver.by_name("place", ask)`` / ``library_index.named_place``.

G1a. A state named in the ask eclipses an out-of-state candidate: the state becomes the place so a
     state-scoped source serves and a point source refuses honestly (no new code — the existing
     ``named_place`` state-words fallback fires once G1b stops the false candidate).
G1b. A one-word place alias written lowercase ("many") counts only with a cue: the ask capitalises
     it, names its own state, or a place-cue word sits right before it ("near Many").
G1c. A basin/ocean/region word (Atlantic, Pacific, Midwest, ...) is never a point place, closed list.
G2.  Two independently-cued places (or a place and a non-containing state) joined by a comparison
     word make the ask "compound": locate offers no candidate, closed reason, honest stop.

A small synthetic pack (resolver tables only, built in-memory) stands in for the pinned pack; the
population figures approximate pack_f's own (log10(pop) facts gathered for this round). No network,
no model except where a flow-level test stubs one.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.library_resolve import Resolver
from smartbrain_3000.secrets import gen_master_key

# id, key, name, lat, lon, state, pop, aliases — the ruling's probe towns + a few ordinary big cities
_PLACES = [
    ("place:many", "many", "Many", 31.57, -93.48, "LA", 2470, ["many"]),
    ("place:normal", "normal", "Normal", 40.51, -88.99, "IL", 53700, ["normal"]),
    ("place:hope", "hope", "Hope", 33.67, -93.59, "AR", 8700, ["hope"]),
    ("place:surprise", "surprise", "Surprise", 33.63, -112.37, "AZ", 147000, ["surprise"]),
    ("place:mobile", "mobile", "Mobile", 30.69, -88.04, "AL", 187000, ["mobile"]),
    ("place:reading", "reading", "Reading", 40.34, -75.93, "PA", 95000, ["reading"]),
    ("place:why", "why", "Why", 31.96, -112.74, "AZ", 116, ["why"]),
    ("place:nice", "nice", "Nice", 39.09, -122.83, "CA", 500, ["nice"]),
    ("place:midwest-wy", "midwest-wy", "Midwest", 43.42, -106.27, "WY", 400, ["midwest"]),
    ("place:midwest-city", "midwest-city", "Midwest City", 35.46, -97.40, "OK", 58000, ["midwest city"]),
    ("place:atlantic-ia", "atlantic-ia", "Atlantic", 41.40, -94.98, "IA", 6760, ["atlantic"]),
    ("place:pacific-mo", "pacific-mo", "Pacific", 38.48, -90.75, "MO", 7000, ["pacific"]),
    ("place:pacific-wa", "pacific-wa", "Pacific", 46.50, -122.25, "WA", 7100, ["pacific"]),
    ("place:new-england-nd", "new-england-nd", "New England", 46.53, -102.87, "ND", 692, ["new england"]),
    ("place:gulf-nc", "gulf-nc", "Gulf", 35.38, -79.30, "NC", 100, ["gulf"]),
    ("place:phoenix", "phoenix", "Phoenix", 33.45, -112.07, "AZ", 1650000, ["phoenix"]),
    ("place:miami", "miami", "Miami", 25.76, -80.19, "FL", 450000, ["miami"]),
    ("place:houston", "houston", "Houston", 29.76, -95.37, "TX", 2300000, ["houston"]),
    ("place:new-orleans", "new-orleans", "New Orleans", 29.95, -90.07, "LA", 370000, ["new orleans"]),
    ("place:chicago", "chicago", "Chicago", 41.88, -87.63, "IL", 2700000, ["chicago"]),
    ("place:boston", "boston", "Boston", 42.36, -71.06, "MA", 675000, ["boston"]),
    ("place:denver", "denver", "Denver", 39.74, -104.99, "CO", 715000, ["denver"]),
    ("place:dallas", "dallas", "Dallas", 32.78, -96.80, "TX", 1300000, ["dallas"]),
    ("place:fort-worth", "fort-worth", "Fort Worth", 32.75, -97.33, "TX", 935000, ["fort worth"]),
    ("place:charleston", "charleston", "Charleston", 32.78, -79.93, "SC", 150000, ["charleston"]),
    # lead probes (2026-10-13): a prominent city is its own cue in lowercase; a mid-size one is not
    ("place:seattle", "seattle", "Seattle", 47.61, -122.33, "WA", 780995, ["seattle"]),
    ("place:galveston", "galveston", "Galveston", 29.30, -94.80, "TX", 53538, ["galveston"]),
    # G1c control: a real, longer place name that merely starts with a region word — untouched
    ("place:atlantic-city", "atlantic-city", "Atlantic City", 39.36, -74.42, "NJ", 38500, ["atlantic city"]),
]
# id, name, code — every state the places above sit in, plus Alaska (named, never a town here)
_STATES = [
    ("AK", "Alaska"), ("AZ", "Arizona"), ("IL", "Illinois"), ("AR", "Arkansas"), ("AL", "Alabama"),
    ("PA", "Pennsylvania"), ("CA", "California"), ("WY", "Wyoming"), ("OK", "Oklahoma"), ("IA", "Iowa"),
    ("MO", "Missouri"), ("WA", "Washington"), ("ND", "North Dakota"), ("NC", "North Carolina"),
    ("FL", "Florida"), ("TX", "Texas"), ("LA", "Louisiana"), ("MA", "Massachusetts"), ("CO", "Colorado"),
    ("SC", "South Carolina"),
]


def _build_resolver_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Just the two resolver tables ``Resolver.by_name``/``named_place`` read."""
    con.execute("CREATE TABLE library_resolver_entries(id VARCHAR PRIMARY KEY, resolver VARCHAR, kind VARCHAR, "
                "key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, state VARCHAR, attrs JSON, rank DOUBLE)")
    con.execute("CREATE TABLE library_resolver_aliases(alias VARCHAR, entry_id VARCHAR, partial BOOLEAN)")
    for eid, key, name, lat, lon, state, pop, aliases in _PLACES:
        rank = round(math.log10(pop), 2) if pop > 0 else 0.0   # the pack's own rank is ~log10(population)
        con.execute("INSERT INTO library_resolver_entries VALUES (?,'place','place',?,?,?,?,?,?,?)",
                    (eid, key, name, lat, lon, state, json.dumps({"pop": pop}), rank))
        for a in aliases:
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,FALSE)", (a, eid))
    for code, name in _STATES:
        eid = f"us_state:{code}"
        con.execute("INSERT INTO library_resolver_entries VALUES (?,'us_state','us_state',?,?,NULL,NULL,?,'{}',0.0)",
                    (eid, code, name, code))
        for a in (name.lower(), code.lower()):
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,FALSE)", (a, eid))


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    _build_resolver_tables(c)
    return c


@pytest.fixture(scope="module")
def res(con: duckdb.DuckDBPyConnection) -> Resolver:
    return Resolver(con)


# --- G1b: a one-word place alias needs a cue -----------------------------------------------------

@pytest.mark.parametrize("ask,expect_name", [
    # lowercase, no state, no cue — a bare common-word alias of a town under BARE_PLACE_RANK
    # (100,000 people) is never a matched candidate (the "many" bug, #36)
    ("how many quakes hit the region today", None),
    ("the city is normal sized for its state", None),
    ("we still hope for better news tomorrow", None),
    ("i was reading a book all day", None),
    ("why did the market drop today", None),
    ("that view is nice this time of year", None),
    # a prominent place is its own cue, even written lowercase and bare (lead probe 2026-10-13:
    # "seattle weather tomorrow" must never lose its place) — Surprise and Mobile are cities of
    # 100,000+ and read as places too
    ("seattle weather tomorrow", "Seattle"),
    ("is phoenix hot right now", "Phoenix"),
    ("the weather was a surprise to everyone", "Surprise"),
    ("my phone service is mobile only", "Mobile"),
    # a mid-size place written lowercase and bare still needs a cue (the intent's own place text
    # supplies one through named_place — see test_the_intents_place_text_needs_no_cue)
    ("galveston water temp", None),
    ("water temp at galveston", "Galveston"),
    # lowercase, a place-cue word right before it (the full set: in/near/at/around/for/from/to/of)
    ("quakes near many right now", "Many"),
    ("weather in normal this week", "Normal"),
    ("flood warning for reading today", "Reading"),
    ("tornado watch at mobile tonight", "Mobile"),
    ("tide chart around nice this week", "Nice"),
    ("directions to why today", "Why"),  # "to" — only this round's broader by_name gate
    ("stories of surprise this week", "Surprise"),  # "of" — ditto
    # lowercase, its own state named — admits it even with no cue word
    ("many louisiana flood watch", "Many"),
    ("normal illinois weather report", "Normal"),
    ("hope arkansas storm warning", "Hope"),
    # lowercase, a DIFFERENT state named — the wrong state never rescues it (#36's actual shape)
    ("normal weather reported across texas", None),
    ("mobile conditions improving across illinois", None),
    # capitalised — the ask's own spelling is itself enough to be a matched candidate
    ("Many is a small town", "Many"),
    ("Normal gets cold in winter", "Normal"),
    ("Hope sits near the river", "Hope"),
    ("Surprise sits in the desert", "Surprise"),
    ("Mobile is a coastal city", "Mobile"),
    ("Reading gets crowded on weekends", "Reading"),
    ("Why sits along a desert highway", "Why"),
    ("Nice had record rainfall", "Nice"),
])
def test_g1b_by_name_requires_a_cue_for_a_bare_one_word_alias(
        res: Resolver, ask: str, expect_name: str | None) -> None:
    r = res.by_name("place", ask)
    if expect_name is None:
        assert r["status"] == "none", (ask, r)
    else:
        assert r["status"] == "resolved" and r["best"]["name"] == expect_name, (ask, r)


def test_the_intents_place_text_needs_no_cue(res: Resolver) -> None:
    """Lead probe 2026-10-13: the flow resolves the intent's own place text through named_place; that
    text is the cue (``by_name(..., cued=True)``), so a lowercase mid-size place the model copied as
    written ("galveston") still resolves — the bare ask alone does not."""
    words, has_place, status = library_index.named_place(res, "galveston water temp", None)
    assert (words, has_place, status) == (set(), False, "none")
    words, has_place, status = library_index.named_place(res, "galveston water temp", "galveston")
    assert words == {"galveston"} and has_place and status == "resolved"
    # a prominent place named by the intent in another state's ask is still not the place (G1a)
    r = res.by_name("place", "mobile conditions improving across illinois")
    assert r["status"] == "none"


def test_g1b_an_all_caps_short_code_counts_as_capitalised(res: Resolver) -> None:
    """Live blind50g: "any delays on the NYC subway" first regressed under this round's own fix — a
    Title-case regex alone missed "NYC" (all capitals), dropping the place and letting an unrelated
    city's transit source through. The existing two-letter rule's own ``upper`` signal (ALL-CAPS,
    1-6 chars) must count as "capitalised" for a longer one-word alias too."""
    r = res.by_name("place", "storms moving over MOBILE tonight")
    assert r["status"] == "resolved" and r["best"]["name"] == "Mobile"


@pytest.mark.parametrize("ask,expect", [
    # a small town (pop < 100k) stays unnamed without a cue even when the ask capitalises it — the
    # SAME precedent as the pack's own "Melbourne"/"Annapolis" (test_of_is_not_a_place_cue,
    # test_named_place_starts_from_the_resolvers_decision): a candidate match alone isn't a NAMED
    # place; relevance still wants a cue, a state, size or a full multi-word name
    ("Many is a small town", set()),
    ("Hope sits near the river", set()),
    ("Why sits along a desert highway", set()),
    # a BIG same-named city (pop >= 100k) is unaffected either way — already-working, not this fix
    ("Surprise sits in the desert", {"surprise"}),
    ("Mobile is a coastal city", {"mobile"}),
    # a cue in named_place's own narrower set admits it for relevance too
    ("quakes near many right now", {"many"}),
    ("weather in normal this week", {"normal"}),
    # its own state admits it (the union includes the state's words, as it already does for any place)
    ("many louisiana flood watch", {"many", "louisiana"}),
])
def test_g1b_named_place_relevance_matches_the_pinned_precedent(res: Resolver, ask: str, expect: set[str]) -> None:
    words, named, _status = library_index.named_place(res, ask)
    assert words == expect, (ask, words)
    assert named == bool(expect), (ask, named)


def test_g1b_a_wrong_named_state_does_not_rescue_a_cueless_alias(res: Resolver) -> None:
    """"how many quakes ... hit Alaska" (#36): "many" (Louisiana) still has no cue, and Alaska is a
    DIFFERENT state — the state-named branch must check the CANDIDATE's own state, not just that
    some state was named anywhere."""
    ask = "how many quakes above magnitude 4 hit Alaska in the last 24 hours"
    words, named, status = library_index.named_place(res, ask)
    assert words == {"alaska"} and named and status == "resolved"  # G1a: the state is the place


def test_g1b_midwest_city_stays_resolvable(res: Resolver) -> None:
    """A region word ("midwest") is vetoed outright (G1c); its own longer, different alias
    ("midwest city") is untouched and still resolves."""
    words, named, _status = library_index.named_place(res, "weather in Midwest City this weekend")
    assert words == {"midwest", "city"} and named


# --- G1c: a region/basin word is never a point place ----------------------------------------------

@pytest.mark.parametrize("ask", [
    "number of active hurricanes in the Atlantic right now",  # capitalised — still never a point (#37)
    "any storms forming in the pacific this week",
    "oil rigs in the gulf of mexico today",
    "water temperature in the caribbean this week",
    "sea ice extent in the arctic this month",
    "shipping lanes in the indian ocean today",
    "ice cover on the great lakes today",
    "storm surge along the gulf coast tonight",
    "flights delayed along the east coast today",
    "wildfire smoke over the west coast today",
    "snowfall across the midwest this week",
    "storms moving through the northeast tonight",
    "heat advisory for the southeast today",
    "dust storm warning for the southwest today",
    "rain moving into the pacific northwest today",
    "nor'easter forming over new england tonight",
])
def test_g1c_region_words_are_never_a_point_place(res: Resolver, ask: str) -> None:
    words, named, status = library_index.named_place(res, ask)
    assert words == set() and named is False and status == "none", (ask, words, status)


def test_g1c_atlantic_city_is_a_different_longer_alias(res: Resolver) -> None:
    """The veto matches only the bare region word's exact alias text; a real, longer place name
    ("Atlantic City") is a different alias and resolves normally."""
    r = res.by_name("place", "a trip to Atlantic City this weekend")
    assert r["status"] == "resolved" and r["best"]["name"] == "Atlantic City"


# --- G2: a compound ask seals no single-place card -------------------------------------------------

@pytest.mark.parametrize("ask", [
    "is it warmer in Phoenix or Miami right now",
    "humidity in Houston compared with New Orleans at the moment",
    "wind speed in Chicago versus Denver today",
    "rainfall in Dallas compared to Boston this month",
    "snowfall in Denver against Charleston this winter",
    "temperature in Miami than in Phoenix right now",
])
def test_g2_a_compound_ask_resolves_no_candidate_place(res: Resolver, ask: str) -> None:
    words, named, status = library_index.named_place(res, ask)
    assert words == set() and named is False and status == "compound", (ask, words, status)


@pytest.mark.parametrize("ask", [
    "Lake Michigan water temperature in Chicago",  # no comparison word at all
    "Dodgers vs Giants score",  # neither side is a place
    "Bitcoin price today versus a week ago",  # one subject, two times
    "weather in Dallas-Fort Worth",  # one place name
    "flights from Boston to Denver",  # a route, not a comparison
    "Charleston harbor tides",  # one place
])
def test_g2_controls_stay_single_subject(res: Resolver, ask: str) -> None:
    _words, _named, status = library_index.named_place(res, ask)
    assert status != "compound", (ask, status)


def test_g2_candidates_returns_a_closed_reason_for_a_compound_ask(lib: library_index.LibraryIndex) -> None:
    rows, skipped = lib.candidates("is it warmer in Phoenix or Miami right now")
    assert rows == [] and skipped == ["this compares two places; one source answers one place"]


def test_g2_a_non_compound_ask_still_builds(lib: library_index.LibraryIndex) -> None:
    rows, _skipped = lib.candidates("weather in Phoenix right now")
    assert rows and rows[0]["source_id"] == "open-meteo-forecast"


# --- G2: the intent validator takes a compound place as a list, never a stage failure -------------

def test_intent_validator_accepts_a_short_place_list() -> None:
    reply = {"kind": "external_data", "cadence_minutes": 15, "wants": ["temperature"],
             "place": ["Phoenix", "Miami"]}
    out = ni_flow._validate_intent(reply)
    assert out["place"] == "Phoenix / Miami"


def test_intent_validator_still_refuses_a_too_long_place_list() -> None:
    reply = {"kind": "external_data", "cadence_minutes": 15, "wants": ["temperature"],
             "place": ["Phoenix", "Miami", "Houston", "Denver"]}
    with pytest.raises(ValueError, match="intent.place must be a string or null"):
        ni_flow._validate_intent(reply)


def test_intent_validator_still_refuses_a_non_string_non_list_place() -> None:
    reply = {"kind": "external_data", "cadence_minutes": 15, "wants": ["temperature"], "place": 42}
    with pytest.raises(ValueError, match="intent.place must be a string or null"):
        ni_flow._validate_intent(reply)


# --- the pinned-pack-shaped fixture (candidates()/run_flow level) ----------------------------------

_TAXONOMY = [("weather", "current_value", ["current_value"], ["temperature", "weather"],
              {"prefer": ["official"], "match": "geo", "resolvers": ["place"], "max_km": 30, "differ_on": []})]
_LATLON = [{"name": "lat", "fill": {"from": "resolver", "resolver": "place", "field": "lat"}},
           {"name": "lon", "fill": {"from": "resolver", "resolver": "place", "field": "lon"}}]
_SOURCE = {"id": "open-meteo-forecast", "name": "Open-Meteo forecast", "cat": "weather/current_value",
           "kinds": ["current_value"], "url": "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}",
           "params": _LATLON, "desc": "Open-Meteo forecast", "authority": "primary", "prior": 1.0, "entity": "",
           "audience": "", "answers": [{"name": "a", "label": "Temperature", "words": ["temperature"]}],
           "examples": ["weather in a city"]}


def _build_pack(path: Path) -> None:
    con_ = duckdb.connect(str(path))
    con_.execute("""CREATE TABLE library_sources(
        id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, provider_id VARCHAR, provider_name VARCHAR,
        authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR, access_kind VARCHAR, url_template VARCHAR,
        docs_url VARCHAR, auth VARCHAR, terms_status VARCHAR, cadence VARCHAR, validation_status VARCHAR,
        robots VARCHAR, votes_yes INTEGER, votes_no INTEGER, prior DOUBLE, record JSON, kinds VARCHAR[],
        role VARCHAR, audience VARCHAR)""")
    con_.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con_.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con_.execute("CREATE TABLE library_taxonomy(category VARCHAR, subcategory VARCHAR, label VARCHAR, "
                 "kinds VARCHAR[], params VARCHAR[], keywords VARCHAR[], policy JSON)")
    con_.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    con_.execute("CREATE TABLE library_source_resolvers(source_id VARCHAR, resolver VARCHAR)")
    _build_resolver_tables(con_)  # the resolver_entries/aliases tables, shared with the unit-test pack
    for cat, sub, kinds, kw, pol in _TAXONOMY:
        con_.execute("INSERT INTO library_taxonomy VALUES (?,?,?,?,?,?,?)",
                     (cat, sub, f"{cat.title()} › {sub}", kinds, [], kw, json.dumps(pol)))
    s = _SOURCE
    cat, sub = s["cat"].split("/")
    params = [{"kind": "x", "required": True, **p} for p in s["params"]]
    acc = {"kind": "http_json", "auth": "none", "headers": {}, "params": params, "url_template": s["url"]}
    rec = {"id": s["id"], "name": s["name"], "description": s["desc"], "tier": "curated", "categories": [s["cat"]],
           "kinds": s["kinds"], "access": acc, "examples": s["examples"], "answers": s["answers"],
           "coverage": {"entity": s["entity"], "geo": "US"},
           "provider": {"id": "p", "name": "Provider", "authority": s["authority"]}}
    con_.execute("INSERT INTO library_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (s["id"], s["name"], s["desc"], "p", "Provider", s["authority"], "curated", "US", s["entity"],
                  "http_json", s["url"], "https://example.org", "none", "public_domain", "hourly", "ok", "allow",
                  0, 0, s["prior"], json.dumps(rec), s["kinds"], "", s["audience"]))
    con_.execute("INSERT INTO library_source_categories VALUES (?,?,?)", (s["id"], cat, sub))
    words = library_index.tokens(" ".join([s["name"], s["desc"], *s["examples"],
                                           *(w for a in s["answers"] for w in a["words"])]))
    for t in dict.fromkeys(words):
        con_.execute("INSERT INTO library_terms VALUES (?,?,?)", (t, s["id"], 2.0))
    for p in params:
        con_.execute("INSERT INTO library_source_resolvers VALUES (?,?)", (s["id"], p["fill"]["resolver"]))
    con_.execute("INSERT INTO library_meta VALUES ('built_at','2026-10-12T00:00:00Z'), ('records','1')")
    con_.close()


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> library_index.LibraryIndex:
    tmp = tmp_path_factory.mktemp("g1g2")
    _build_pack(tmp / "src.duckdb")
    payload = gzip.compress((tmp / "src.duckdb").read_bytes())
    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(payload),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


# --- the flow-level honest stop (through run_flow, a stubbed model) --------------------------------

def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def test_a_compound_ask_through_run_flow_reaches_the_honest_stop(lib, monkeypatch) -> None:
    """"is it warmer in Phoenix or Miami" never seals Phoenix's card alone: run_flow (intent, a
    stubbed model; locate, the real G2 rule) lands the SAME honest stop as "no source answers yet"."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    store = _store()
    ask = "is it warmer in Phoenix or Miami right now"
    item_id = ni_flow.create_shell_item(store, ask)
    intent_json = json.dumps({"kind": "external_data", "subject": "temperature", "cadence_minutes": 15,
                              "wants": ["temperature"], "place": None})
    out = ni_flow.run_flow(store, item_id, gateway_call=lambda _m, _p: intent_json, ni_route_model="m")
    assert out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert "no SmartBrain Library source answers this yet" in out["notes"][-1]


def test_a_place_list_intent_reaches_the_same_stop_not_a_stage_failure(lib, monkeypatch) -> None:
    """A model that answers ``place`` as a list (set G live: Phoenix/Miami) must not fail the intent
    stage after its retry — it reaches the ordinary source-pick pause, exactly like a clean intent."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    store = _store()
    ask = "is it warmer in Phoenix or Miami right now"
    item_id = ni_flow.create_shell_item(store, ask)
    intent_json = json.dumps({"kind": "external_data", "subject": "temperature", "cadence_minutes": 15,
                              "wants": ["temperature"], "place": ["Phoenix", "Miami"]})
    out = ni_flow.run_flow(store, item_id, gateway_call=lambda _m, _p: intent_json, ni_route_model="m")
    assert out["state"] == "source" and out["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert out["state"] != "failed"
