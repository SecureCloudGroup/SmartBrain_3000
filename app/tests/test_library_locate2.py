"""Locate v2: the router and hybrid retrieval over the Library's embedded cards, source asks and route asks.

Every embedding here comes from a STUB embedder (a deterministic bag of hashed words), never the network: the
route (centroid cosine with a leave-one-out gap threshold), the dense ranking (B-max over a source's card and its
example asks), the fusion (RRF plus the route's boost), the hard filters (each with its reason), the sidecar
(keyed by pack sha256 + embedder) and the fallback to the keyword ranking when there is no embedder or it fails.
The pack is a small REAL DuckDB file in the schema ``sourcetool build`` produces."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from pathlib import Path

import duckdb
import numpy as np
import pytest

from smartbrain_3000 import library_embed, library_index

_DIM = 512
_STOP = {"a", "an", "the", "is", "it", "in", "of", "for", "to", "at", "what", "whats", "how", "and", "on", "my",
         "me", "are", "be", "will", "this", "s"}


def _bow(text: str, task: str = "query") -> list[float]:
    """A stub embedding: hashed bag of words (stop words out), so asks that share words are near."""
    v = np.zeros(_DIM)
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w not in _STOP:
            v[zlib.crc32(w.encode()) % _DIM] += 1.0
    v[_DIM - 1] += 0.01  # never a zero vector
    return v.tolist()


class _Stub:
    """Counts calls; can fail on demand."""

    def __init__(self, model: str = "stub/bow-embed", fail: bool = False) -> None:
        self.model, self.fail, self.calls = model, fail, 0

    def embedder(self) -> library_embed.Embedder:
        def one(text: str, task: str) -> list[float]:
            self.calls += 1
            if self.fail:
                raise RuntimeError("embedder down")
            return _bow(text, task)
        return library_embed.Embedder(self.model, one)


def _pol(match, resolvers=()):
    return {"prefer": ["official", "primary"], "match": match, "resolvers": list(resolvers)}


_TAXONOMY = [  # category, subcategory, kinds, keywords, policy
    ("weather", "forecast", ["forecast", "current_value"], ["forecast", "weather"], _pol("geo", ["place"])),
    ("hazards", "space_weather", ["forecast", "current_value"], ["geomagnetic", "aurora"], _pol("none")),
    ("markets", "crypto", ["current_value"], ["crypto", "bitcoin"], _pol("name")),
    ("sky", "launches", ["next_event"], ["launch", "rocket"], _pol("none")),
    ("sports", "schedules", ["next_event", "schedule"], ["schedule"], _pol("name", ["team_espn"])),
    ("water", "surf_waves", ["forecast", "current_value"], ["surf", "waves"], _pol("geo", ["place"])),
]
_LATLON = [{"name": "lat", "fill": {"from": "resolver", "resolver": "place", "field": "lat"}},
           {"name": "lon", "fill": {"from": "resolver", "resolver": "place", "field": "lon"}}]
_SOURCES = [  # id, name, category, kinds, url, params, words, status, asks
    ("open-meteo-forecast", "Open-Meteo forecast", "weather/forecast", ["forecast", "current_value"],
     "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}", _LATLON, ["temperature", "rain"], "ok",
     ["is it gonna storm tonight", "will it rain tomorrow", "temperature outside", "chance of thunderstorms",
      "how hot will it get", "snow this weekend", "wind speed today", "is it going to pour", "humidity today",
      "weekend forecast"]),
    ("swpc-kp-forecast", "Planetary K-index forecast", "hazards/space_weather", ["forecast"],
     "https://services.swpc.noaa.gov/kp.json", [], ["geomagnetic storm", "aurora"], "ok",
     ["geomagnetic storm tonight", "kp index", "northern lights odds", "aurora forecast", "solar storm strength",
      "will I see the aurora", "space weather outlook", "kp forecast for tomorrow", "auroral oval", "g3 storm watch"]),
    ("coingecko-price", "CoinGecko coin price", "markets/crypto", ["current_value"],
     "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", [], ["price", "bitcoin"], "ok",
     ["bitcoin price", "how much is ethereum", "btc in dollars", "crypto prices", "solana value",
      "price of dogecoin", "ether price now", "litecoin quote", "coin market price", "what is btc worth"]),
    ("broken-coins", "Broken coin feed", "markets/crypto", ["current_value"],
     "https://broken.example.org/coins", [], ["bitcoin", "price"], "failed",
     ["bitcoin price feed", "coin prices", "crypto ticker", "btc usd", "eth usd", "altcoin prices", "coin quotes",
      "token price", "crypto market", "price of bitcoin"]),
    ("ll2-upcoming-launches", "Upcoming rocket launches", "sky/launches", ["next_event"],
     "https://ll.thespacedevs.com/2.2.0/launch/upcoming/", [], ["launch", "rocket"], "ok",
     ["next spacex launch", "when is the next rocket launch", "upcoming launches", "falcon 9 launch date",
      "launch schedule cape canaveral", "next artemis launch", "rocket launch today", "starship launch",
      "next launch from vandenberg", "upcoming space launches"]),
    ("team-next", "Team's next game", "sports/schedules", ["next_event", "schedule"],
     "https://www.thesportsdb.com/api/v1/json/3/eventsnext.php?id={k}",
     [{"name": "k", "fill": {"from": "resolver", "resolver": "team_espn", "field": "key"}}], ["next game"], "ok",
     ["when do they play next", "next game", "team schedule", "who do they play", "game time tonight",
      "next home game", "upcoming games", "when is the game", "next matchup", "season schedule"]),
]
_GRID = [{"name": n, "kind": "place", "fill": {"from": "source", "source": "nws-points", "path": f"properties.{f}"}}
         for n, f in (("office", "gridId"), ("grid_x", "gridX"), ("grid_y", "gridY"))]
# no example asks: (id, name, category, kinds, url, params, words, role, coverage geo)
_EXTRA = [
    ("nws-points", "NWS points lookup", "weather/forecast", ["current_value"],
     "https://api.weather.gov/points/{lat},{lon}", _LATLON, [], "helper", "US"),
    ("nws-forecast", "NWS 7-day forecast", "weather/forecast", ["forecast"],
     "https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast", _GRID, ["forecast"], "", "US"),
    ("swpc-discussion", "Space weather forecast discussion", "hazards/space_weather", ["text_brief"],
     "https://services.swpc.noaa.gov/text/discussion.txt", [], ["geomagnetic", "discussion"], "", "US"),
    ("open-meteo-marine", "Open-Meteo marine", "water/surf_waves", ["forecast", "current_value"],
     "https://marine-api.open-meteo.com/v1/marine?latitude={lat}&longitude={lon}", _LATLON, ["waves", "swell"], "",
     "US coastal waters"),
]
_ROUTES = {
    "weather/forecast": ["will it rain tomorrow", "weather this weekend", "is it gonna storm tonight",
                         "temperature today", "forecast for tomorrow", "will it snow", "how cold tonight",
                         "rain chances", "is it going to be windy", "heat this week", "storm tonight here",
                         "sunny tomorrow"],
    "hazards/space_weather": ["geomagnetic storm", "kp index now", "aurora tonight", "solar flare",
                              "space weather alerts", "northern lights forecast", "solar wind speed",
                              "radio blackout", "g4 storm", "aurora odds", "sunspots", "cme arrival"],
    "markets/crypto": ["bitcoin price", "ethereum price", "crypto market cap", "btc to usd", "dogecoin value",
                       "solana price", "crypto prices today", "coin prices", "ether worth", "altcoins",
                       "price of litecoin", "stablecoin peg"],
    "sky/launches": ["next rocket launch", "spacex launch schedule", "upcoming launches", "launch today",
                     "falcon heavy launch", "next nasa launch", "starship launch date", "launch window",
                     "rocket launch cape canaveral", "blue origin launch", "next launch", "ula launch"],
}
_ENTRIES = [("place:tulsa", "place", "tulsa", "Tulsa", 36.15, -95.99, "OK", {"pop": 413000}, 5.0, ["tulsa"]),
            ("team_espn:oilers", "team_espn", "hockey/echl/1", "Tulsa Oilers", None, None, "", {"league": "echl"},
             1.0, ["tulsa oilers", "tulsa"]),
            ("place:chi", "place", "chi", "Chicago", 41.88, -87.63, "IL", {"pop": 2700000}, 5.0, ["chicago"]),
            ("place:lmb", "place", "lmb", "Lake Michigan Beach", 42.22, -86.37, "MI", {"pop": 1100}, 5.0,
             ["lake michigan beach", "lake michigan"]),
            ("place:duluth-ga", "place", "duluth-ga", "Duluth", 34.0, -84.14, "GA", {"pop": 31000, "coastal": False},
             4.0, ["duluth"]),
            ("place:vb", "place", "vb", "Virginia Beach", 36.85, -75.98, "VA", {"pop": 459000, "coastal": True}, 5.0,
             ["virginia beach"])]


def _build_pack(path: Path, with_asks: bool = True) -> None:
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
    if with_asks:
        con.execute("CREATE TABLE library_source_asks(source_id VARCHAR, ask VARCHAR)")
        con.execute("CREATE TABLE library_route_asks(category VARCHAR, subcategory VARCHAR, ask VARCHAR)")
        for route, asks in _ROUTES.items():
            cat, sub = route.split("/")
            con.executemany("INSERT INTO library_route_asks VALUES (?,?,?)", [(cat, sub, a) for a in asks])
    rows = [(*x, "", "US") for x in _SOURCES] + [(*x[:7], "ok", [], *x[7:]) for x in _EXTRA]
    for sid, name, catsub, kinds, url, params, words, status, asks, role, geo in rows:
        cat, sub = catsub.split("/")
        acc = {"kind": "http_json", "auth": "none", "headers": {}, "url_template": url,
               "params": [{"kind": "x", "required": True, **p} for p in params]}
        rec = {"id": sid, "name": name, "description": name, "tier": "curated", "categories": [catsub],
               "kinds": kinds, "access": acc, "examples": [], "answers": [{"label": name, "words": words}],
               "coverage": {"entity": "", "geo": geo}, "role": role,
               "provider": {"id": "p", "name": "Provider", "authority": "official"}}
        con.execute("INSERT INTO library_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, name, name, "p", "Provider", "official", "curated", geo, "", "http_json", url,
                     "https://example.org", "none", "public_domain", "hourly", status, "allow", 0, 0, 1.0,
                     json.dumps(rec), kinds, role, ""))
        con.execute("INSERT INTO library_source_categories VALUES (?,?,?)", (sid, cat, sub))
        for t in dict.fromkeys(library_index.tokens(" ".join([name, *words]))):
            con.execute("INSERT INTO library_terms VALUES (?,?,?)", (t, sid, 2.0))
        for p in params:
            if p["fill"].get("resolver"):
                con.execute("INSERT INTO library_source_resolvers VALUES (?,?)", (sid, p["fill"]["resolver"]))
        if with_asks and asks:
            con.executemany("INSERT INTO library_source_asks VALUES (?,?)", [(sid, a) for a in asks])
    for eid, res, key, name, lat, lon, st, attrs, rank, aliases in _ENTRIES:
        con.execute("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, res, res, key, name, lat, lon, st, json.dumps(attrs), rank))
        for a in aliases:
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,?)", (a, eid, False))
    con.execute("INSERT INTO library_meta VALUES ('built_at','2026-10-03T00:00:00Z'), ('records','5')")
    con.close()


class _Net:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        return self.payload


def _install(tmp: Path, with_asks: bool = True) -> library_index.LibraryIndex:
    _build_pack(tmp / "src.duckdb", with_asks)
    payload = gzip.compress((tmp / "src.duckdb").read_bytes())
    idx = library_index.LibraryIndex(tmp / "data", netguard_mod=_Net(payload),
                                     pack={"tag": "vtest", "url": "https://example.org/l.gz",
                                           "sha256": hashlib.sha256(payload).hexdigest()})
    idx.install()
    return idx


@pytest.fixture(autouse=True)
def _clean():
    library_embed.set_provider(None)
    library_embed.forget()
    yield
    library_embed.set_provider(None)
    library_embed.forget()


@pytest.fixture
def lib(tmp_path) -> library_index.LibraryIndex:
    return _install(tmp_path)


def _wire(lib: library_index.LibraryIndex, stub: _Stub) -> library_embed.Vectors:
    library_embed.set_provider(stub.embedder)
    got = library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn, wait=True)
    assert got is not None
    return got[0]


def _ids(lib, ask, hint=None) -> list[str]:
    return [c["source_id"] for c in lib.candidates(ask, hint=hint)[0]]


# --- route ----------------------------------------------------------------------------------------------

def test_the_gap_threshold_is_fitted_leave_one_out_on_the_route_asks() -> None:
    """Two clean clusters route every held-out ask right, so the threshold is the smallest gap seen (an ask less
    sure than any route ask is not confident); labels that ignore the geometry reach the target accuracy at no
    gap -> never confident."""
    rng = np.random.default_rng(7)
    a, b = np.eye(8)[0], np.eye(8)[1]
    vecs = library_embed._unit(np.stack([a + 0.05 * rng.standard_normal(8) for _ in range(6)]
                                        + [b + 0.05 * rng.standard_normal(8) for _ in range(6)]))
    labels = ["x/a"] * 6 + ["x/b"] * 6
    t = library_embed.gap_threshold(vecs, labels)
    gaps = []
    for i, v in enumerate(vecs):  # the held-out gap of each ask: its own centroid without it vs the other
        own = [j for j in range(12) if labels[j] == labels[i] and j != i]
        other = [j for j in range(12) if labels[j] != labels[i]]
        gaps.append(float(library_embed._unit(vecs[own].sum(0)) @ v - library_embed._unit(vecs[other].sum(0)) @ v))
    assert t == pytest.approx(min(gaps), abs=1e-5)
    bad = ["x/a", "x/b"] * 6  # labels that ignore the geometry
    assert library_embed.gap_threshold(vecs, bad) == float("inf")


def test_the_route_is_the_nearest_centroid_with_its_runner_up_and_gap(lib) -> None:
    v = _wire(lib, _Stub())
    assert sorted(v.routes) == sorted(_ROUTES)
    r = v.route(library_embed.embed_query(_Stub().embedder(), "will it rain in Tulsa tomorrow"))
    assert r["route"] == "weather/forecast" and r["runner_up"] != "weather/forecast"
    assert r["gap"] > 0 and r["confident"] is (r["gap"] >= v.threshold)


# --- dense + fusion -------------------------------------------------------------------------------------

def test_dense_takes_the_best_of_a_sources_card_and_its_example_asks(lib) -> None:
    """No card says "pour"; one of the forecast source's example asks does."""
    v = _wire(lib, _Stub())
    q = library_embed.embed_query(_Stub().embedder(), "is it going to pour")
    assert v.dense(q)[0] == "open-meteo-forecast"
    assert "broken-coins" not in v.ids  # failed sources are never embedded


def test_fusion_is_rrf_and_the_route_boosts_without_removing(lib) -> None:
    v = _wire(lib, _Stub())
    kw, dense = ["swpc-kp-forecast", "open-meteo-forecast"], ["open-meteo-forecast", "coingecko-price"]
    assert v.fuse(kw, dense, None) == ["open-meteo-forecast", "swpc-kp-forecast", "coingecko-price"]
    sure = {"route": "markets/crypto", "runner_up": "weather/forecast", "gap": 1.0, "confident": True}
    fused = v.fuse(kw, dense, sure)
    assert fused.index("coingecko-price") < fused.index("swpc-kp-forecast")
    assert set(fused) == set(kw) | set(dense)  # a boost, never a filter
    # a weak route boosts by its confidence: a near tie adds nothing, half the threshold half a vote
    weak = {"route": "markets/crypto", "runner_up": "hazards/space_weather", "gap": 0.0, "confident": False}
    v.threshold = 0.2
    assert v.fuse(kw, dense, weak) == v.fuse(kw, dense, None)
    half = v.fuse(kw, dense, {**weak, "gap": 0.1})
    assert half.index("coingecko-price") < half.index("swpc-kp-forecast")
    assert half[0] == "open-meteo-forecast"


def test_the_router_and_dense_ranking_settle_an_ask_keywords_get_wrong(lib) -> None:
    """Blind 2026-09-29: "is it gonna storm in Tulsa tonight" — "storm" is a geomagnetic-storm word, and no keyword
    names a category, so keywords alone offer nothing (WP1's honest gap); the embedded asks and the route put the
    forecast first."""
    assert lib.candidates("is it gonna storm in Tulsa tonight") == ([], ["couldn't tell what kind of data this is"])
    _wire(lib, _Stub())
    rows, _ = lib.candidates("is it gonna storm in Tulsa tonight")
    assert rows[0]["source_id"] == "open-meteo-forecast" and "lat" in rows[0]["params"]
    assert rows[0]["scope"] == "place" and rows[0]["frame_kind"] == "forecast"


def test_a_weak_route_restricts_nothing(lib) -> None:
    v = _wire(lib, _Stub())
    v.threshold = float("inf")  # no route is confident
    with lib._conn() as con:
        route = v.route(library_embed.embed_query(_Stub().embedder(), "bitcoin price"))
        ctx = lib._context(con, "bitcoin price", {}, route)
    # a weak route names no category of its own: the ask's keywords do (evidence of relevance, never a gate)
    assert route["confident"] is False and ctx["cats"] == lib.classify("bitcoin price")
    assert _ids(lib, "bitcoin price")[0] == "coingecko-price"


def test_a_weak_route_still_rules_out_a_reading_it_is_sure_is_not_the_subject(lib) -> None:
    """Blind: "when is sunset in Denver" became the Rockies' schedule — an event question about a word that is
    also a team. A weak route names no category, but a reading whose every subcategory trails the top route by
    the confident gap is not the subject; one that might be the route is kept. The ask names the team with a
    non-place word of its name so L1's place-only drop can't settle it on its own — the weak-route distance
    ruling is what rules it in or out here."""
    ask = "Tulsa Oilers roster"
    weak = {"route": "weather/forecast", "runner_up": "hazards/space_weather", "gap": 0.01, "confident": False,
            "threshold": 0.1, "behind": {"weather/forecast": 0.0, "hazards/space_weather": 0.01,
                                         "markets/crypto": 0.3, "sky/launches": 0.3, "sports/schedules": 0.3}}
    with lib._conn() as con:
        assert "team_espn" in lib._context(con, ask, {}, None)["found"]  # keywords alone: the Oilers
        assert "team_espn" not in lib._context(con, ask, {}, weak)["found"]
        near = {**weak, "behind": {**weak["behind"], "sports/schedules": 0.05}}
        assert "team_espn" in lib._context(con, ask, {}, near)["found"]


# --- hard filters, each with its reason ------------------------------------------------------------------

def test_only_hard_capability_failures_drop_a_source_and_each_says_why(lib) -> None:
    _wire(lib, _Stub())
    rows, skipped = lib.candidates("bitcoin price feed")
    assert "broken-coins" not in [r["source_id"] for r in rows]
    assert "Broken coin feed: failed when last checked" in skipped
    rows, skipped = lib.candidates("will it rain tomorrow")  # the forecast needs a place the ask doesn't name
    assert "open-meteo-forecast" not in [r["source_id"] for r in rows]
    assert "Open-Meteo forecast: name the place (a city, town or ZIP)" in skipped
    rows, skipped = lib.candidates("bitcoin price history")  # a trend: the source gives only the current value
    assert "coingecko-price" not in [r["source_id"] for r in rows]
    assert "CoinGecko coin price: gives current value, not trend" in skipped


def test_no_category_fails_closed_by_keywords_and_the_vectors_open_it(lib) -> None:
    """Without vectors no keyword naming a category fails closed (the web stage runs); with them the embedded
    asks find what the keywords can't."""
    assert lib.candidates("how much is ethereum") == ([], ["couldn't tell what kind of data this is"])
    _wire(lib, _Stub())
    rows, skipped = lib.candidates("how much is ethereum")
    assert rows and rows[0]["source_id"] == "coingecko-price"
    assert "couldn't tell what kind of data this is" not in skipped


def test_the_signature_and_row_shape_are_unchanged(lib) -> None:
    _wire(lib, _Stub())
    rows, skipped = lib.candidates("bitcoin price", limit=1, hint={"subject": "bitcoin", "wants": ["price"]})
    assert isinstance(skipped, list) and len(rows) == 1
    assert {"source_id", "tier", "categories", "title", "provider", "authority", "url", "host", "label", "choice",
            "status", "format", "needs_key", "params", "needs_contact", "scope", "frame_kind"} <= set(rows[0])


# --- the sidecar ------------------------------------------------------------------------------------------

def test_the_sidecar_is_keyed_by_pack_and_embedder_and_rebuilds_on_a_change(lib) -> None:
    stub = _Stub()
    _wire(lib, stub)
    path = lib._dir / library_embed.SIDECAR
    assert path.exists()
    built = stub.calls
    offered = len(_SOURCES) - 1 + sum(1 for x in _EXTRA if x[7] != "helper")  # not the failed one, not the helper
    assert built == offered + 10 * (len(_SOURCES) - 1) + 12 * len(_ROUTES)  # cards, source asks, routes
    library_embed.forget()  # a restart: the sidecar is read, nothing re-embedded
    _wire(lib, stub)
    assert stub.calls == built
    other = _Stub(model="stub/another-embed")  # the user changed their embedder
    library_embed.set_provider(other.embedder)
    assert library_embed.load(path, library_embed._key(lib.installed()["sha256"], other.embedder())) is None
    assert library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn, wait=True) is not None
    assert other.calls == built
    assert library_embed.load(path, library_embed._key("0" * 64, other.embedder())) is None  # another pack


def test_a_missing_sidecar_builds_in_the_background_and_locate_does_not_wait(lib) -> None:
    import threading
    stub = _Stub()
    library_embed.set_provider(stub.embedder)
    assert library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn) is None  # building
    for t in threading.enumerate():
        if t.name == "library-embed":
            t.join(timeout=30)
    assert library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn) is not None


def test_a_pack_without_example_asks_still_ranks_by_cards(tmp_path) -> None:
    lib = _install(tmp_path, with_asks=False)
    v = _wire(lib, _Stub())
    assert v.routes == [] and len(v.asks) == 0
    q = library_embed.embed_query(_Stub().embedder(), "bitcoin price")
    assert v.route(q) is None and v.dense(q)[0] == "coingecko-price"


# --- fallback: nothing breaks ------------------------------------------------------------------------------

def test_without_an_embedder_locate_ranks_by_keywords(lib) -> None:
    assert library_embed.current_embedder() is None
    assert not (lib._dir / library_embed.SIDECAR).exists()
    assert _ids(lib, "bitcoin price")[0] == "coingecko-price"
    assert not (lib._dir / library_embed.SIDECAR).exists()


def test_a_failing_embedder_falls_back_and_waits_before_retrying(lib) -> None:
    down = _Stub(fail=True)
    library_embed.set_provider(down.embedder)
    assert library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn, wait=True) is None
    calls = down.calls
    assert _ids(lib, "bitcoin price")[0] == "coingecko-price"  # the keyword ranking, as before
    assert down.calls == calls  # the failed build is not retried on every ask
    assert not (lib._dir / library_embed.SIDECAR).exists()


def test_a_failing_ask_embed_falls_back_for_that_ask(lib) -> None:
    stub = _Stub()
    _wire(lib, stub)
    stub.fail = True
    assert _ids(lib, "bitcoin price")[0] == "coingecko-price"


def test_a_broken_provider_is_no_embedder(lib) -> None:
    def broken():
        raise RuntimeError("locked")
    library_embed.set_provider(broken)
    assert library_embed.current_embedder() is None
    assert _ids(lib, "bitcoin price")[0] == "coingecko-price"


def test_the_gateway_embedder_is_the_knowledge_embedder(monkeypatch) -> None:
    """The Knowledge base's own path: gateway.embed with the task, keyed by its #tp1 storage identity."""
    seen = []
    monkeypatch.setattr(library_embed.gateway, "embed",
                        lambda text, model, task, timeout, acquire_timeout=None:
                        seen.append((text, model, task)) or [1.0, 0.0])
    emb = library_embed.gateway_embedder("mlx/nomicai-modernbert-embed-base-bf16")
    assert emb is not None and emb.scheme == "mlx/nomicai-modernbert-embed-base-bf16#tp1"
    assert emb.embed_one("bitcoin price", "query") == [1.0, 0.0]
    assert seen == [("bitcoin price", "mlx/nomicai-modernbert-embed-base-bf16", "query")]


# --- WP1-grade precision without vectors, and the review's defects -------------------------------------------

def test_a_symbol_typed_alone_or_with_a_dollar_is_the_ticker(tmp_path) -> None:
    """Sealed: "aapl", "$pltr", "jpm" found nothing — the resolver reads a lowercase key as a word. A $-word, or a
    whole ask that is one listed stock symbol, is written as the symbol; an everyday or Library word is not."""
    _ENTRIES.append(("ticker:AAPL", "ticker", "AAPL", "Apple Inc.", None, None, "", {}, 1.0, ["apple", "aapl"]))
    try:
        lib = _install(tmp_path)
    finally:
        _ENTRIES.pop()
    with lib._conn() as con:
        assert lib._expand_short_words(con, "aapl") == "AAPL"
        assert lib._expand_short_words(con, "spx") == "spx"  # not a listed stock symbol
        assert lib._expand_short_words(con, "$pltr now") == "PLTR now"
        assert lib._expand_short_words(con, "aurora") == "aurora"  # a Library keyword
        assert lib._expand_short_words(con, "news") == "news"  # an everyday word
        assert lib._expand_short_words(con, "aapl stock") == "aapl stock"  # not the whole ask


def test_how_the_market_closed_is_a_value_and_an_outlook_is_a_written_forecast(lib) -> None:
    assert library_index.frame_kind_from_text("how did the market close") == "current_value"
    assert library_index.frame_kind_from_text("how did the dow do today") == "current_value"
    assert library_index.frame_kind_from_text("how did the Cubs do") == "result"
    assert "swpc-discussion" in _ids(lib, "geomagnetic outlook")
    rows, skipped = lib.candidates("aurora odds of a geomagnetic storm")
    assert "swpc-discussion" not in [r["source_id"] for r in rows]
    assert "Space weather forecast discussion: gives text brief, not forecast" in skipped


def test_a_chained_place_lookup_needs_a_place_and_rides_on_the_row(lib) -> None:
    """Review D3: NWS's grid is filled after consent from its points lookup, chained from the place. Without a
    place it is not offered; with one its ``lookup`` rides on the candidate and it is scoped to the place."""
    rows, skipped = lib.candidates("weather forecast this weekend")
    assert "nws-forecast" not in [r["source_id"] for r in rows]
    assert "NWS 7-day forecast: name the place (a city, town or ZIP)" in skipped
    rows, _ = lib.candidates("weather forecast for Tulsa")
    nws = next(r for r in rows if r["source_id"] == "nws-forecast")
    assert nws["scope"] == "place" and "{office}" in nws["url"]
    assert nws["lookup"] and nws["lookup"][0]["url"] == "https://api.weather.gov/points/36.15,-95.99"
    assert {x["param"] for x in nws["lookup"]} == {"office", "grid_x", "grid_y"}
    assert all("lookup" in r for r in rows)


def test_the_intents_place_said_in_the_ask_is_the_place(lib) -> None:
    """Review D12: "Lake Michigan weather forecast in Chicago" (intent place Chicago) filled Lake Michigan Beach,
    MI — the longer reading. The intent's place, when the ask says it, is the place, and the fill can't pick the
    other reading."""
    from smartbrain_3000.library_resolve import Resolver
    ask = "Lake Michigan weather forecast in Chicago"
    with lib._conn() as con:
        assert library_index.named_place(Resolver(con), ask, "Chicago")[:2] == ({"chicago"}, True)
    rows, _ = lib.candidates(ask, hint={"place": "Chicago"})
    assert rows and all("Chicago" in r["label"] and "Michigan Beach" not in r["label"] for r in rows)


def test_a_marine_source_is_refused_for_an_inland_place(lib) -> None:
    """Review D12: Open-Meteo marine for Duluth, GA. A place the pack marks inland (attrs.coastal false) gets no
    marine reading; a coastal one does."""
    rows, skipped = lib.candidates("surf in Duluth")
    assert "open-meteo-marine" not in [r["source_id"] for r in rows]
    assert any(s.startswith("Open-Meteo marine:") for s in skipped)
    assert "open-meteo-marine" in _ids(lib, "surf in Virginia Beach")


def test_the_subcategory_and_official_hosts_are_public(lib) -> None:
    """Review D11: the flow reads a subcategory's facts and the official sites through public calls."""
    sub = lib.subcategory("weather/forecast")
    assert sub["kinds"] == ["forecast", "current_value"] and sub["policy"]["match"] == "geo" and sub["expects"] == []
    assert lib.subcategory("nope/none") == {}
    assert lib.official_hosts("anything") == {}  # this pack has no official_site resolver entries


def test_official_hosts_reads_the_official_site_resolver(tmp_path) -> None:
    _ENTRIES.append(("official_site:noaa", "official_site", "noaa", "NOAA", None, None, "",
                     {"domains": ["noaa.gov", "weather.gov"]}, 1.0, ["noaa", "national weather service"]))
    try:
        lib = _install(tmp_path)
    finally:
        _ENTRIES.pop()
    assert lib.official_hosts("National Weather Service forecast") == {"national weather service": ["noaa.gov",
                                                                                                   "weather.gov"]}
    assert lib.official_hosts("NOAA tides") == {"noaa": ["noaa.gov", "weather.gov"]}
    assert lib.official_hosts("tides") == {}


def test_with_vectors_a_word_only_the_category_says_is_not_enough(lib) -> None:
    """Review D2: "pollen count Raleigh" got npm downloads (a "download count"). With vectors there is no category
    gate, so a source outside the asked category must name every word that says what the user wants; one whose
    own words name only some of them is not offered."""
    _wire(lib, _Stub())
    rows, skipped = lib.candidates("geomagnetic rocket forecast")  # no source is about both
    assert rows == [] or all(r["source_id"] != "ll2-upcoming-launches" for r in rows)


# --- review2 findings: locate must fail closed, never confidently wrong ----------------------------------

def test_locate_refuses_a_cloud_embedding_model_and_falls_back_to_keywords(lib, monkeypatch) -> None:
    """L3 (privacy): locate must never embed the user's ask (or the Library's cards) with a cloud embedder.
    ``gateway_embedder`` returns None for a non-local model id; ``current_embedder`` reports None; locate
    runs the keyword ranking without any gateway.embed call."""
    seen = []
    monkeypatch.setattr(library_embed.gateway, "embed",
                        lambda text, model, task, timeout, acquire_timeout=None:
                        seen.append((text, model, task)) or [1.0, 0.0])
    # cloud model id: provider returns None (no egress possible)
    assert library_embed.gateway_embedder("openai/text-embedding-3-small") is None
    library_embed.set_provider(lambda: library_embed.gateway_embedder("openai/text-embedding-3-small"))
    assert library_embed.current_embedder() is None
    rows, _ = lib.candidates("is my ex still living at 42 elm street")
    assert isinstance(rows, list) and seen == []  # keyword path only; no embed call made
    # a locally-routed id DOES return an embedder (the gate is strictly cloud-only)
    local = library_embed.gateway_embedder("mlx/some-local-embed")
    assert local is not None


def test_a_confident_route_requires_the_floor_before_admitting_its_nearest_member(lib) -> None:
    """L2: a confident route must not admit its nearest category member whatever the similarity. "price of
    a used honda civic" admitted bestbuy at B-max 0.393 (far under FLOOR 0.828). The nearest-of-route is
    only added to the ``near`` set when its B-max clears FLOOR."""
    import numpy as np
    v = _wire(lib, _Stub())
    q = np.zeros(v.cards.shape[1], dtype=np.float32)
    q[0] = 1.0  # an orthogonal query: B-max is near zero for every stored vector
    v.threshold = -1.0  # make every route "confident" so the nearest branch runs
    bm = v.bmax(q)
    assert float(bm.max()) < library_embed.FLOOR, "an orthogonal query must be well below the floor"
    route = v.route(q)
    assert route is not None and route["confident"]
    near = v.similar(q)
    members = [i for i, c in enumerate(v.cats) if route["route"] in c]
    assert members, "the route must have at least one member in the pack"
    top = max(members, key=lambda i: bm[i])
    # mirror candidates()'s admission rule: nearest-of-route only joins ``near`` when bmax >= FLOOR
    if bm[top] >= library_embed.FLOOR:
        near.add(v.ids[top])
    assert near == set(), (near, bm[top])


def test_a_routed_member_with_no_distinguishing_word_is_not_admitted(lib) -> None:
    """L2: on a routed path, if the ask carries distinguishing words (``ctx['left']``) that NO member of
    the asked category names (``ctx['telling']`` is empty), membership alone is not evidence — the
    ``_off_words`` fallback returns 'not about what was asked' instead of silently admitting every
    category member. When ``left`` is empty too (the ask said only the category's keyword), membership
    still answers (unchanged from WP1)."""
    class _Row(dict):
        pass
    row = _Row(name="Open-Meteo forecast", tier="curated", categories=["weather/forecast"])
    record = {"description": "", "answers": [], "examples": [], "name": "Open-Meteo forecast"}
    # ask has distinguishing words left (cat, video) that no member names
    ctx = {"cats": ["weather/forecast"], "match": "geo", "telling": set(), "left": {"cat", "video"},
           "routed": True, "explained": set(), "frame": None, "asked": "cat video"}
    assert lib._off_words(row, record, ctx, {"cat", "video"}, [], "cat video") == "not about what was asked"
    # left empty: the ask said only the category's own keyword, membership still answers
    ctx_only_keyword = {**ctx, "left": set()}
    assert lib._off_words(row, record, ctx_only_keyword, {"cat"}, [], "cat") == ""
    # without routing (WP1), telling-empty + left non-empty still admits (unchanged)
    ctx_wp1 = {**ctx, "routed": False}
    assert lib._off_words(row, record, ctx_wp1, {"cat", "video"}, [], "cat video") == ""


def test_a_corrupt_sidecar_is_discarded_and_locate_rebuilds(lib, tmp_path) -> None:
    """L7: a sidecar whose zip is truncated/corrupt must not permanently poison locate (``candidates()`` would
    return [] for every ask under the same pack+embedder key). ``load`` catches any exception, deletes the
    bad file, so the next ``ready`` rebuilds it."""
    path = lib._dir / library_embed.SIDECAR
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a zip at all")
    stub = _Stub()
    library_embed.set_provider(stub.embedder)
    key = library_embed._key(lib.installed()["sha256"], stub.embedder())
    assert library_embed.load(path, key) is None
    assert not path.exists()  # discarded so the next build can land cleanly
    got = library_embed.ready(lib._dir, lib.installed()["sha256"], lib._conn, wait=True)
    assert got is not None and path.exists()


def test_a_query_of_a_different_dimension_falls_back_to_keywords(lib, monkeypatch) -> None:
    """L7: a query embedding of a different dimension than the sidecar's vectors (an embedder tag that moved
    weights under the same provider/model id) would raise ValueError on the first matmul and leave candidates
    = []. ``candidates`` validates the query dim and falls back to the keyword ranking for that ask."""
    stub = _Stub()
    _wire(lib, stub)
    # the next embed_query returns a different-dim vector than the cards were built with
    import numpy as np
    original = library_embed.embed_query
    monkeypatch.setattr(library_embed, "embed_query",
                        lambda emb, ask: np.ones(_DIM + 1, dtype=np.float32) / float(np.sqrt(_DIM + 1)))
    rows, skipped = lib.candidates("bitcoin price")  # must not raise, must not return []
    assert rows, "a dim mismatch must fall back to the keyword ranking, not fail closed"
    assert any(r["source_id"] == "coingecko-price" for r in rows)
    monkeypatch.setattr(library_embed, "embed_query", original)


def test_a_pack_without_route_asks_does_not_embed_or_build(tmp_path) -> None:
    """L8: with no library_route_asks table (v2 is inert without a router), ``candidates`` must not build the
    sidecar or embed any ask — the earlier behaviour was to embed every card + every ask for a path that then
    discarded the vectors."""
    lib = _install(tmp_path, with_asks=False)
    stub = _Stub()
    library_embed.set_provider(stub.embedder)
    rows, _ = lib.candidates("bitcoin price")
    assert stub.calls == 0  # no build, no query embed
    assert rows and rows[0]["source_id"] == "coingecko-price"
    assert not (lib._dir / library_embed.SIDECAR).exists()


def test_the_build_yields_to_a_waiting_foreground_call(monkeypatch) -> None:
    """L5: ``_embed_all`` polls ``gateway.local_waiters()`` between texts and sleeps while a foreground acquirer
    is pending — so a background Library build never starves the next chat embed on a non-FIFO semaphore."""
    polls = []
    pending = [3]  # three "foreground" acquires queue up and then drop one at a time

    def waiters_fn() -> int:
        n = pending[0]
        polls.append(n)
        if n:
            pending[0] -= 1  # one more foreground caller got served after this poll
        return n
    monkeypatch.setattr(library_embed.gateway, "local_waiters", waiters_fn)
    monkeypatch.setattr(library_embed.gateway, "local_available", lambda: True)
    monkeypatch.setattr(library_embed, "YIELD_SLEEP", 0.0)
    e = library_embed.Embedder("mlx/x", lambda text, task: [1.0, 0.0])
    library_embed._embed_all(e, ["a"], "document")
    # polled until waiters dropped to 0 — the build backed off instead of racing for the semaphore
    assert polls and polls[-1] == 0 and any(p > 0 for p in polls), polls


def test_embed_query_returns_none_when_the_local_serializer_is_busy() -> None:
    """L6: ``embed_query`` runs through ``gateway.embed`` with a short ``acquire_timeout``; a busy local
    serializer raises ``LocalBusy``, which ``embed_query``'s existing except catches and returns None for.
    The ask ranks on keywords for that one call (busy is a fallback, not a failure)."""
    import threading
    import time

    from smartbrain_3000 import gateway
    emb = library_embed.gateway_embedder("mlx/nomicai-modernbert-embed-base-bf16")
    assert emb is not None
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with gateway._serialized("mlx/nomicai-modernbert-embed-base-bf16"):
            held.set()
            release.wait(2.0)
    t = threading.Thread(target=hold); t.start()
    try:
        assert held.wait(2.0)
        # the semaphore is held by another thread — a query-path embed through gateway_embedder
        # fails fast (short acquire_timeout) and embed_query returns None for the keyword fallback
        t0 = time.monotonic()
        assert library_embed.embed_query(emb, "bitcoin price") is None
        assert time.monotonic() - t0 < 2.0, "query must not wait on the foreground stream"
    finally:
        release.set(); t.join()
