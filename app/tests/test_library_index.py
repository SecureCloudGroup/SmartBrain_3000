"""NI Library page backend: the pinned pack (install/verify/read), search, and the user's sealed local
sources — plus the /api/library routes. The pack here is a REAL DuckDB file in the exact schema
``sourcetool build`` produces; only the network fetch is replaced."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, library_resolve, netguard, ni
from smartbrain_3000.secrets import gen_master_key

_POLICY_GEO = {"prefer": ["official", "primary", "aggregator", "community"], "match": "geo", "resolvers": ["place"],
               "max_km": 30, "differ_on": ["water"], "max_age": "1d", "cross_check": False, "ask_if_ambiguous": True}
_POLICY_NAME = {**_POLICY_GEO, "match": "name", "max_km": None, "differ_on": []}
_TAXONOMY = [  # category, subcategory, label, keywords, policy
    ("water", "tides", "Water & Coast › Tides", ["tide", "tides", "high tide"], _POLICY_GEO),
    ("markets", "crypto", "Markets & Money › Crypto", ["bitcoin", "crypto"], _POLICY_NAME),
    ("tech", "service_status", "Tech & Internet › Service status", ["status", "down"], _POLICY_NAME),
    ("sports", "schedules", "Sports › Schedules & fixtures", ["schedule", "next game"], _POLICY_NAME),
]


def _param(name, kind, fill):
    return {"name": name, "kind": kind, "example": None, "required": True, "fill": fill}


_TIDE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station={station}&begin_date={begin}"
_SOURCES = [  # id, name, tier, status, prior, category, terms, kinds, access extras, extra record fields
    ("coops-tide-hilo", "NOAA tide predictions", "curated", "ok", 2.0, ("water", "tides"),
     {"tide": 3.0, "tides": 3.0, "noaa": 1.5, "predictions": 3.0}, ["next_event", "schedule"],
     {"url_template": _TIDE_URL, "params": [
         _param("station", "station", {"from": "resolver", "resolver": "tide_station", "field": "key"}),
         _param("begin", "date", {"from": "clock", "format": "%Y%m%d", "offset_days": 0})]}, {}),
    ("coingecko-price", "CoinGecko coin price", "curated", "ok", 1.8, ("markets", "crypto"),
     {"bitcoin": 2.5, "price": 3.0, "coin": 3.0}, ["current_value"],
     {"url_template": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin", "params": []}, {}),
    ("dead-crypto", "Dead crypto feed", "harvested", "failed", -1.0, ("markets", "crypto"),
     {"bitcoin": 5.0, "price": 5.0}, ["current_value"], {"url_template": "https://dead.example.org/x"}, {}),
    ("statuspage-summary", "Service status", "curated", "ok", 1.5, ("tech", "service_status"),
     {"status": 3.0, "github": 2.0, "down": 2.5}, ["status", "alerts"],
     {"url_template": "https://{status_host}/api/v2/summary.json", "params": [
         _param("status_host", "service", {"from": "resolver", "resolver": "statuspage", "field": "key"})]}, {}),
    ("mlb-team-schedule", "MLB team schedule", "curated", "ok", 1.6, ("sports", "schedules"),
     {"schedule": 3.0, "mlb": 2.0, "team": 1.0}, ["schedule", "next_event"],
     {"url_template": "https://statsapi.mlb.com/api/v1/schedule?teamId={team}", "params": [
         _param("team", "team", {"from": "resolver", "resolver": "team_mlb", "field": "key"})]}, {}),
    ("keyed-quote", "Keyed quote", "curated", "ok", 1.9, ("markets", "crypto"),
     {"bitcoin": 2.0, "quote": 3.0}, ["current_value"],
     {"url_template": "https://keyed.example.org/q?apikey={key}", "params": [
         _param("key", "key", {"from": "vault_key"})]}, {}),
    ("helper-stations", "Tide station list", "curated", "ok", 1.0, ("water", "tides"),
     {"tide": 2.0, "tides": 2.0, "stations": 3.0}, ["lookup"],
     {"url_template": "https://api.tidesandcurrents.noaa.gov/mdapi/stations.json"}, {"role": "helper"}),
]
_RESOLVER_ENTRIES = [  # id, resolver, kind, key, name, lat, lon, state, attrs, rank, aliases
    ("place:1", "place", "place", "1", "Portland", 45.5, -122.6, "OR", {"pop": 650000}, 5.8,
     ["portland", "portland or", "portland oregon"]),
    ("place:2", "place", "place", "2", "Portland", 43.6, -70.2, "ME", {"pop": 68000}, 4.8,
     ["portland", "portland me", "portland maine"]),
    ("place:3", "place", "place", "3", "Melbourne", 28.1, -80.64, "FL", {"pop": 90000}, 4.9,
     ["melbourne", "melbourne fl", "melbourne florida"]),
    ("place:4", "place", "place", "4", "Denver", 39.7, -104.9, "CO", {"pop": 716000}, 5.9, ["denver", "denver co"]),
    ("place:5", "place", "place", "5", "Denver", 42.67, -92.3, "IA", {"pop": 1900}, 3.3, ["denver", "denver ia"]),
    ("place:6", "place", "place", "6", "South Lake Tahoe", 38.9, -120.0, "CA", {"pop": 21225, "nicknames": ["tahoe"]},
     3.0, ["south lake tahoe", "tahoe"]),
    ("tide_station:872", "tide_station", "station", "872", "Melbourne Causeway", 28.08, -80.60, "FL",
     {"water": "Indian River"}, 1.0, ["melbourne causeway"]),
    ("tide_station:873", "tide_station", "station", "873", "Eau Gallie", 28.16, -80.63, "FL",
     {"water": "Indian River"}, 1.0, ["eau gallie"]),
    ("tide_station:900", "tide_station", "station", "900", "Sebastian Inlet", 27.86, -80.45, "FL",
     {"water": "Atlantic"}, 1.0, ["sebastian inlet"]),
    ("statuspage:www.githubstatus.com", "statuspage", "service", "www.githubstatus.com", "GitHub", None, None, "",
     {}, 1.0, ["github"]),
    ("team_mlb:144", "team_mlb", "team", "144", "Atlanta Braves", None, None, "", {"league": "mlb"}, 1.0,
     ["atlanta braves", "braves", "atl"]),
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
    for cat, sub, label, kw, pol in _TAXONOMY:
        con.execute("INSERT INTO library_taxonomy VALUES (?,?,?,?,?,?,?)", (cat, sub, label, [], [], kw,
                                                                              json.dumps(pol)))
    for sid, name, tier, status, prior, (cat, sub), terms, kinds, access, extra in _SOURCES:
        acc = {"kind": "http_json", "auth": "none", "headers": {}, "params": [], **access}
        rec = {"id": sid, "name": name, "description": f"{name} description", "tier": tier,
               "categories": [f"{cat}/{sub}"], "kinds": kinds, "access": acc,
               "examples": [" ".join(terms)],  # a real record's examples feed its index terms
               "provider": {"id": "p", "name": "Provider", "authority": "official"}, **extra}
        con.execute("INSERT INTO library_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, name, f"{name} description", "p", "Provider", "official", tier, "US", "", "http_json",
                     acc["url_template"], "https://example.org", "none", "public_domain", "hourly", status,
                     "allow", 0, 0, prior, json.dumps(rec), kinds, extra.get("role", ""), ""))
        con.execute("INSERT INTO library_source_categories VALUES (?,?,?)", (sid, cat, sub))
        for t, w in terms.items():
            con.execute("INSERT INTO library_terms VALUES (?,?,?)", (t, sid, w))
        for p in acc.get("params", []):
            if p["fill"]["from"] == "resolver":
                con.execute("INSERT INTO library_source_resolvers VALUES (?,?)", (sid, p["fill"]["resolver"]))
    for eid, res, kind, key, name, lat, lon, st, attrs, rank, aliases in _RESOLVER_ENTRIES:
        con.execute("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, res, kind, key, name, lat, lon, st, json.dumps(attrs), rank))
        for a in aliases:
            con.execute("INSERT INTO library_resolver_aliases VALUES (?,?,?)", (a, eid, False))
    con.execute("INSERT INTO library_meta VALUES ('built_at','2026-09-28T00:00:00Z'), ('records','7'), "
                "('schema','1')")
    con.close()


@pytest.fixture()
def pack_bytes(tmp_path) -> bytes:
    p = tmp_path / "src.duckdb"
    _build_pack(p)
    return gzip.compress(p.read_bytes())


class _FakeNet:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def safe_fetch_library_pack(self, url: str, max_bytes: int) -> bytes:
        self.calls += 1
        assert url.startswith("https://") and max_bytes > 0
        return self.payload


def _index(tmp_path: Path, payload: bytes, sha: str | None = None) -> tuple[library_index.LibraryIndex, _FakeNet]:
    net = _FakeNet(payload)
    pack = {"tag": "vtest", "url": "https://example.org/library.duckdb.gz",
            "sha256": sha or hashlib.sha256(payload).hexdigest()}
    return library_index.LibraryIndex(tmp_path / "data", netguard_mod=net, pack=pack), net


# --- the pinned pack ----------------------------------------------------------------------------

def test_install_verifies_hash_then_reads(tmp_path, pack_bytes) -> None:
    idx, net = _index(tmp_path, pack_bytes)
    assert idx.status() == {"installed": False, "tag": "vtest"}
    meta = idx.install()
    assert meta["tag"] == "vtest" and net.calls == 1
    idx.install()  # idempotent: no second download
    assert net.calls == 1
    st = idx.status()
    assert st["installed"] and st["records"] == 7 and st["by_status"]["ok"] == 6


def test_install_refuses_a_hash_mismatch_and_writes_nothing(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes, sha="0" * 64)
    with pytest.raises(library_index.LibraryIndexError, match="pinned hash"):
        idx.install()
    assert not (tmp_path / "data" / "library" / "library.duckdb").exists()
    assert idx.installed() is None


def test_install_refuses_an_unpinned_build(tmp_path, pack_bytes) -> None:
    idx, net = _index(tmp_path, pack_bytes, sha="PENDING")
    with pytest.raises(library_index.LibraryIndexError, match="no Library pack pinned"):
        idx.install()
    assert net.calls == 0


def test_install_refuses_an_oversized_unpack(tmp_path, monkeypatch) -> None:
    bomb = gzip.compress(b"\0" * (3 << 20))
    monkeypatch.setattr(library_index, "MAX_PACK_BYTES", 1 << 20)
    idx, _ = _index(tmp_path, bomb)
    with pytest.raises(library_index.LibraryIndexError, match="larger than allowed"):
        idx.install()
    assert not list((tmp_path / "data" / "library").glob("*.duckdb"))


def test_a_new_release_pin_reinstalls(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    newer, net = _index(tmp_path, pack_bytes + b"", sha="1" * 64)
    assert newer.installed() is None  # the old pack is not this release's pin
    with pytest.raises(library_index.LibraryIndexError):
        newer.install()
    assert net.calls == 1


# --- reads ----------------------------------------------------------------------------------------

def test_taxonomy_counts_exclude_broken_sources(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    cats = {c["id"]: c for c in idx.taxonomy()}
    assert cats["markets"]["count"] == 2  # coin price + keyed quote; dead-crypto (failed) is not counted
    assert cats["water"]["subcategories"][0] == {"id": "tides", "label": "Tides", "count": 2,
                                                 "keywords": ["tide", "tides", "high tide"]}


def test_search_ranks_by_relevance_and_hides_failed(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    r = idx.search("bitcoin price")
    ids = [x["id"] for x in r["results"]]
    assert ids[0] == "coingecko-price" and "dead-crypto" not in ids  # the failed feed never shows by default
    assert idx.search("tides for Charleston today")["results"][0]["id"] == "coops-tide-hilo"
    assert idx.search("is github down")["results"][0]["id"] == "statuspage-summary"
    assert idx.search("bitcoin", status="failed")["results"][0]["id"] == "dead-crypto"


def test_browse_without_query_orders_by_prior_and_filters(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    ids = [x["id"] for x in idx.search()["results"]]
    # reviewed sources first, in taxonomy order: water, markets, tech, sports
    assert ids.index("coops-tide-hilo") < ids.index("coingecko-price") < ids.index("statuspage-summary") \
        < ids.index("mlb-team-schedule")
    only = idx.search(category="markets", subcategory="crypto")
    assert only["total"] == 2 and {r["categories"][0] for r in only["results"]} == {"markets/crypto"}
    page = idx.search(offset=1, limit=1)
    assert page["total"] == 6 and len(page["results"]) == 1  # failed sources never browse
    assert idx.get("coops-tide-hilo")["name"] == "NOAA tide predictions"
    assert idx.get("nope") is None


# --- local sources ------------------------------------------------------------------------------

_CATS = {"water/tides", "markets/crypto"}


def _form(**over) -> dict:
    body = {"name": "My harbor tides", "url": "https://tides.example.com/api/{station}.json",
            "description": "tides for my dock", "category": "water/tides", "access_kind": "http_json",
            "needs_key": False}
    body.update(over)
    return body


def test_validate_local_builds_a_record(monkeypatch) -> None:
    monkeypatch.setattr(netguard, "validate_public_url", lambda url: None)
    rec = library_index.validate_local(_form(needs_key=True), _CATS)
    assert rec["tier"] == "local" and rec["id"].startswith("local-")
    assert [p["name"] for p in rec["access"]["params"]] == ["station", "key"]
    assert rec["access"]["auth"] == "free_key" and rec["provider"]["name"] == "tides.example.com"


@pytest.mark.parametrize(("over", "msg"), [
    ({"url": "http://tides.example.com/x"}, "https://"),
    ({"url": "https://tides.example.com/x?api_key=abc123"}, "Don't put a key"),
    ({"url": "https://{host}/x"}, "real public host"),
    ({"category": "weather/forecast"}, "Choose a category"),
    ({"access_kind": "binary"}, "data format"),
    ({"name": "x"}, "name"),
])
def test_validate_local_refuses(monkeypatch, over, msg) -> None:
    monkeypatch.setattr(netguard, "validate_public_url", lambda url: None)
    with pytest.raises(ValueError, match=msg):
        library_index.validate_local(_form(**over), _CATS)


def test_validate_local_refuses_private_hosts() -> None:
    with pytest.raises(ValueError, match="public internet host"):
        library_index.validate_local(_form(url="https://127.0.0.1/x"), _CATS)


def test_local_sources_are_sealed_at_rest(monkeypatch) -> None:
    monkeypatch.setattr(netguard, "validate_public_url", lambda url: None)
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = ni.NIStore(conn, gen_master_key())
    local = library_index.LocalSources(store)
    rec = local.add(library_index.validate_local(_form(), _CATS))
    assert [r["id"] for r in local.list()] == [rec["id"]]
    raw = conn.execute("SELECT ciphertext FROM ni_snapshots WHERE item_id = ?",
                       [library_index.LOCAL_RESERVED_ID]).fetchone()[0]
    assert b"harbor" not in bytes(raw) and b"tides.example.com" not in bytes(raw)
    with pytest.raises(ValueError, match="already added"):
        local.add(library_index.validate_local(_form(), _CATS))
    assert local.search("harbor tides")[0]["id"] == rec["id"]
    assert local.search("bitcoin") == []
    assert local.search("", category="markets") == []
    assert local.delete(rec["id"]) and local.list() == [] and not local.delete(rec["id"])


# --- routes ---------------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch, pack_bytes):
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "lib.duckdb"))
    net = _FakeNet(pack_bytes)
    monkeypatch.setattr(netguard, "safe_fetch_library_pack", net.safe_fetch_library_pack)
    monkeypatch.setattr(netguard, "validate_public_url", lambda url: None)
    monkeypatch.setattr(library_index, "PACK", {"tag": "vtest", "url": "https://example.org/l.gz",
                                                "sha256": hashlib.sha256(pack_bytes).hexdigest()})
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as c:
        yield c


def test_routes_require_unlock(client: TestClient) -> None:
    for method, path in (("get", "/api/library/status"), ("post", "/api/library/install"),
                         ("get", "/api/library/sources"), ("get", "/api/library/local"),
                         ("delete", "/api/library/local/local-x")):
        assert getattr(client, method)(path).status_code == 423, path


def test_route_flow_install_browse_add_remove(client: TestClient) -> None:
    assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
    assert client.get("/api/library/status").json()["installed"] is False
    assert client.get("/api/library/sources").status_code == 409
    st = client.post("/api/library/install").json()
    assert st["installed"] and st["records"] == 7
    cats = client.get("/api/library/taxonomy").json()["categories"]
    assert {c["id"] for c in cats} == {"water", "markets", "tech", "sports"}
    assert "keywords" not in cats[0]["subcategories"][0]
    hits = client.get("/api/library/sources", params={"q": "bitcoin price"}).json()
    assert hits["results"][0]["id"] == "coingecko-price" and hits["local"] == []
    assert client.get("/api/library/sources/coops-tide-hilo").json()["name"] == "NOAA tide predictions"
    assert client.get("/api/library/sources/nope").status_code == 404
    bad = client.post("/api/library/local", json=_form(url="http://x.example.com/a"))
    assert bad.status_code == 400 and "https://" in bad.json()["detail"]
    added = client.post("/api/library/local", json=_form()).json()
    assert added["tier"] == "local"
    from smartbrain_3000 import library_client
    assert library_client.pending(client.app.state.ni) == 0  # a private source is never sent
    suggested = client.post("/api/library/local", json=_form(name="Suggested gauge",
                                                             url="https://harbor.example.org/other",
                                                             suggest=True))
    assert suggested.status_code == 200 and library_client.pending(client.app.state.ni) == 1
    client.delete(f"/api/library/local/{suggested.json()['id']}")
    mine = client.get("/api/library/sources", params={"q": "harbor"}).json()["local"]
    assert [r["id"] for r in mine] == [added["id"]]
    assert client.get(f"/api/library/sources/{added['id']}").json()["tier"] == "local"
    assert [r["id"] for r in client.get("/api/library/local").json()["sources"]] == [added["id"]]
    assert client.delete(f"/api/library/local/{added['id']}").json() == {"ok": True}
    assert client.delete(f"/api/library/local/{added['id']}").status_code == 404


# --- resolvers + fills + candidates (the card flow's layer 1) -------------------------------------



@pytest.fixture()
def lib(tmp_path, pack_bytes):
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    return idx


def test_resolver_by_name_ask_on_ties_pick_by_population(lib) -> None:
    with lib._conn() as con:
        r = library_resolve.Resolver(con)
        assert r.by_name("place", "weather in Portland")["status"] == "ambiguous"  # OR vs ME: ~10x, ask
        amb = r.by_name("place", "weather in Portland")
        assert [c["state"] for c in amb["candidates"]][:2] == ["OR", "ME"]  # likeliest readings first
        assert r.by_name("place", "weather in Portland Oregon")["best"]["state"] == "OR"
        assert r.by_name("place", "rain in Denver")["best"]["state"] == "CO"  # 377x bigger: what people mean
        assert r.by_name("statuspage", "is GitHub down")["best"]["key"] == "www.githubstatus.com"
        assert r.by_name("team_mlb", "next Braves game")["best"]["key"] == "144"
        assert r.by_name("team_mlb", "is it sunny")["status"] == "none"


def test_resolver_near_asks_when_close_choices_differ(lib) -> None:
    with lib._conn() as con:
        r = library_resolve.Resolver(con)
        near = r.near("tide_station", 28.1, -80.64, 30, ("water",))
        assert near["status"] == "resolved" and near["best"]["key"] == "872"  # both close ones are Indian River
        mixed = r.near("tide_station", 27.97, -80.53, 30, ("water",))  # between the lagoon and the inlet
        assert mixed["status"] == "ambiguous"
        assert {c["attrs"]["water"] for c in mixed["candidates"]} == {"Indian River", "Atlantic"}
        assert r.near("tide_station", 39.7, -104.9, 30, ("water",))["status"] == "none"  # Denver: honest


def test_candidate_urls_fill_from_words_clock_and_resolvers(lib) -> None:
    with lib._conn() as con:
        r = library_resolve.Resolver(con)
        rec = json.loads(con.execute("SELECT record FROM library_sources WHERE id='coops-tide-hilo'").fetchone()[0])
        urls, why = library_resolve.candidate_urls(rec, "tides for Melbourne FL", _POLICY_GEO, r,
                                                   now=datetime(2026, 9, 28, 9, tzinfo=UTC))
        assert not why and len(urls) == 1
        assert urls[0]["url"] == ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station=872"
                                  "&begin_date=20260928")
        assert "Melbourne Causeway" in urls[0]["label"]


def test_candidate_urls_refuse_a_helper_or_an_unnamed_thing(lib) -> None:
    with lib._conn() as con:
        r = library_resolve.Resolver(con)
        get = lambda sid: json.loads(con.execute("SELECT record FROM library_sources WHERE id=?",
                                                 [sid]).fetchone()[0])
        assert library_resolve.candidate_urls(get("helper-stations"), "tide stations", {}, r)[1].startswith(
            "a lookup helper")
        assert "doesn't name" in library_resolve.candidate_urls(get("statuspage-summary"), "is it down", {}, r)[1]


def test_a_keyed_source_is_offered_without_its_key_and_says_where_the_key_goes(lib) -> None:
    """The address never carries a key; ``needs_key`` tells the card what to ask for."""
    with lib._conn() as con:
        r = library_resolve.Resolver(con)
        rec = json.loads(con.execute("SELECT record FROM library_sources WHERE id='keyed-quote'").fetchone()[0])
        urls, why = library_resolve.candidate_urls(rec, "bitcoin quote", {}, r)
        assert why == "" and urls[0]["url"] == "https://keyed.example.org/q"
        assert urls[0]["needs_key"] == {"in": "query", "name": "apikey", "prefix": "", "docs_url": ""}
        coin = json.loads(con.execute("SELECT record FROM library_sources WHERE id='coingecko-price'").fetchone()[0])
        contact = {**coin, "access": {**coin["access"], "contact_ua": True}}
        urls, _ = library_resolve.candidate_urls(contact, "bitcoin price", {}, r)
        assert urls and urls[0]["needs_contact"] is True and "needs_key" not in urls[0]


@pytest.mark.parametrize(("access", "want"), [
    ({"url_template": "https://a.example.org/x?zip={zip}&API_KEY={key}"}, {"in": "query", "name": "API_KEY", "prefix": ""}),
    ({"url_template": "https://a.example.org/x", "headers": {"X-eBirdApiToken": "{key}"}},
     {"in": "header", "name": "X-eBirdApiToken", "prefix": ""}),
    ({"url_template": "https://a.example.org/x", "headers": {"Authorization": "Token {key}"}},
     {"in": "header", "name": "Authorization", "prefix": "Token "}),
    ({"url_template": "https://a.example.org/{key}/x"}, None),  # a key in the path would sit in logs
    ({"url_template": "https://a.example.org/x", "headers": {"X-K": "{key}-suffix"}}, None),
])
def test_key_placement(access, want) -> None:
    assert library_resolve.key_placement(access, "key") == want


def test_ambiguous_reading_expands_together_and_duplicates_are_refused() -> None:
    values = {"lat": [("45.5", "Portland (OR)"), ("43.6", "Portland (ME)")],
              "lon": [("-122.6", "Portland (OR)"), ("-70.2", "Portland (ME)")]}
    urls = library_resolve._expand("https://x.example.org/f?lat={lat}&lon={lon}", values,
                                   {"lat": "place", "lon": "place"})
    assert [u["url"] for u in urls] == ["https://x.example.org/f?lat=45.5&lon=-122.6",
                                        "https://x.example.org/f?lat=43.6&lon=-70.2"]  # never mixed readings
    assert all(u["choice"] for u in urls)


def test_candidates_for_the_card_flow(lib) -> None:
    tides, _ = lib.candidates("tides for Melbourne FL")
    assert [c["source_id"] for c in tides] == ["coops-tide-hilo"]  # the helper list is never a card
    assert tides[0]["url"].startswith("https://api.tidesandcurrents.noaa.gov/") and "station=872" in tides[0]["url"]
    status, _ = lib.candidates("is GitHub down")
    assert status[0]["url"] == "https://www.githubstatus.com/api/v2/summary.json"
    braves, _ = lib.candidates("next Braves game")
    assert braves[0]["url"] == "https://statsapi.mlb.com/api/v1/schedule?teamId=144"
    coins, skipped = lib.candidates("bitcoin price")
    # a failed source stays off the card; a keyed one shows AFTER the keyless one, saying it needs a key
    assert [c["source_id"] for c in coins] == ["coingecko-price", "keyed-quote"]
    assert coins[0]["needs_key"] is None and coins[1]["needs_key"]["in"] == "query"
    nothing, _ = lib.candidates("tides in Denver CO")
    assert nothing == []  # no station within range: the flow falls through to web search, honestly


# --- the flow: Library candidates on the pick card, and a tap is a Yes (R6) ------------------------

def test_flow_pick_pause_seals_library_candidates_and_a_tap_records_yes(tmp_path, pack_bytes, monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = ni.NIStore(conn, gen_master_key())
    item_id = ni_flow.create_shell_item(store, "tides for Melbourne FL")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("tides for Melbourne FL", "source"))
    ni_flow._pause_source_pick(store, item_id, "tides for Melbourne FL", {"kind": "external_data"},
                               call_model=lambda _p: "{}")
    rec = ni_flow._flow_read(store, item_id)
    assert rec["error"] == ni_flow.AWAITING_SOURCE_PICK and rec["_ranked_library"][0]["source_id"] == "coops-tide-hilo"
    field = ni_flow.board_flow_field(store, item_id)
    sug = field["suggestions"][0]
    assert sug["kind"] == "library" and "station=872" in sug["url"]
    assert any("Melbourne Causeway" in e for e in sug["evidence"])
    local = library_index.LocalSources(store)
    assert local.record_yes("coops-tide-hilo") == 1 and local.record_yes("coops-tide-hilo") == 2
    raw = conn.execute("SELECT ciphertext FROM ni_snapshots WHERE item_id = ? AND slot = 'votes'",
                       [library_index.LOCAL_RESERVED_ID]).fetchone()[0]
    assert b"coops" not in bytes(raw)  # the user's choices are sealed at rest


def test_flow_without_a_library_is_unchanged(monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", None)
    assert ni_flow._library_candidates("tides for Melbourne FL") == []


class _NoSearch:
    """A search service that must never be asked (the Library already answered)."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 10) -> dict:
        self.queries.append(query)
        return {"results": [{"title": "Web", "url": "https://web.example.org/", "snippet": ""}]}


def _flow_store(tmp_path, pack_bytes, monkeypatch):
    from smartbrain_3000 import ni_flow
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: idx)
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ni.NIStore(conn, gen_master_key())


def test_the_library_answers_first_and_web_search_never_runs(tmp_path, pack_bytes, monkeypatch) -> None:
    """Layer 1 is the Library: when it has a source, no web search is made and no web rows seal."""
    from smartbrain_3000 import ni_flow
    store = _flow_store(tmp_path, pack_bytes, monkeypatch)
    svc = _NoSearch()
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", lambda: svc)
    item_id = ni_flow.create_shell_item(store, "tides for Melbourne FL")
    ni_flow._pause_source_pick(store, item_id, "tides for Melbourne FL", {"kind": "external_data"},
                               call_model=lambda _p: "{}")
    rec = ni_flow._flow_read(store, item_id)
    assert rec["_ranked_library"] and not rec.get("_ranked_search")
    assert svc.queries == []


def test_a_declined_or_refined_source_repicks_from_the_library(tmp_path, pack_bytes, monkeypatch) -> None:
    """Re-entering the pick (a note asking for another source) offers the Library's rows again."""
    from smartbrain_3000 import ni_flow
    store = _flow_store(tmp_path, pack_bytes, monkeypatch)
    item_id = ni_flow.create_shell_item(store, "tides for Melbourne FL")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("tides for Melbourne FL", "failed"))
    ni_flow.reenter_source_pick(store, item_id, "pick again")
    field = ni_flow.board_flow_field(store, item_id)
    assert field["state"] == "source" and field["suggestions"][0]["kind"] == "library"


def test_a_retired_catalog_confirm_pause_relands_as_a_library_pick(tmp_path, pack_bytes, monkeypatch) -> None:
    """A card left waiting at the retired built-in catalog's confirm step after an upgrade
    becomes a normal pick card with the Library's sources — never a dead state."""
    from smartbrain_3000 import ni_flow
    store = _flow_store(tmp_path, pack_bytes, monkeypatch)
    item_id = ni_flow.create_shell_item(store, "tides for Melbourne FL")
    record = ni_flow._make_record("tides for Melbourne FL", "source")
    record.update(state="confirm_source", error="awaiting_confirm", _recipe_id="tides-x",
                  source_url="https://example.org/old")
    store.write_snapshot(item_id, "flow", record, ok=True)  # as a pre-Library build wrote it
    field = ni_flow.board_flow_field(store, item_id)
    assert field["state"] == "source" and field["error"] == ni_flow.AWAITING_SOURCE_PICK
    assert field["suggestions"][0]["kind"] == "library"
    assert ni_flow._flow_read(store, item_id)["state"] == "source"


def test_classify_the_longest_keyword_wins(tmp_path) -> None:
    """Live 2026-09-29: with "temp" a weather word, "water temp Charleston" tied water with weather and
    got the forecast. A keyword inside a longer matched keyword doesn't count on its own."""
    idx = library_index.LibraryIndex(tmp_path)
    idx._taxonomy_cache = [
        {"id": "weather", "subcategories": [{"id": "forecast", "keywords": ["temp", "temperature"]}]},
        {"id": "water", "subcategories": [{"id": "water_temp", "keywords": ["water temp", "ocean temperature"]}]},
    ]
    assert idx.classify("water temp Charleston") == ["water/water_temp"]
    assert idx.classify("ocean temperature San Diego") == ["water/water_temp"]
    assert idx.classify("temp in Denver") == ["weather/forecast"]


def test_a_source_is_about_its_own_words_not_its_categorys(lib) -> None:
    """Live 2026-09-29: "gold price per ounce" got WTI crude oil — "gold" is a commodities keyword, and
    the relevance check counted the category's vocabulary as the source's own. Only the source's own
    words (name, description, examples, declared answers) say what it is about."""
    rows, _ = lib.candidates("crypto fear price")  # "crypto" is the category's word, never CoinGecko's own
    assert "coingecko-price" not in [c["source_id"] for c in rows]
    rows, _ = lib.candidates("bitcoin price")
    assert rows and rows[0]["source_id"] == "coingecko-price"


def test_a_reviewed_area_name_is_a_place_without_a_cue(lib) -> None:
    """Blind 2026-09-29: "how much snow is Tahoe getting this week" had no place — South Lake Tahoe is
    small, and "Tahoe" came without "in". A reviewed nickname is a place on its own; a plain small-town
    name still needs its cue."""
    from smartbrain_3000.library_resolve import Resolver
    with lib._conn() as con:
        res = Resolver(con)
        assert lib._place_words(res, "Tahoe weather")[0] == {"tahoe"}
        assert lib._place_words(res, "Melbourne weather")[0] == set()


def test_of_is_not_a_place_cue(lib) -> None:
    """Live 2026-09-29: "price of silver" read "of silver" as Silver City. A small place is the ask's
    place only after in / at / near / for / around."""
    from smartbrain_3000.library_resolve import Resolver
    with lib._conn() as con:
        res = Resolver(con)
        assert lib._place_words(res, "value of Melbourne")[0] == set()
        assert "melbourne" in lib._place_words(res, "tides in Melbourne")[0]


def test_a_formatted_resolver_fill(lib) -> None:
    """Coinbase takes "BTC-USD": the Library's fill is the entry's symbol through "{UPPER}-USD". The app
    only knew a bare "{UPPER}", so Coinbase was never offered (live 2026-09-29)."""
    rec = {"id": "pair-src", "name": "Pair", "tier": "curated", "categories": ["markets/crypto"],
           "access": {"kind": "http_json", "url_template": "https://x.example.org/p/{pair}", "params": [
               {"name": "pair", "kind": "place", "required": True,
                "fill": {"from": "resolver", "resolver": "place", "field": "state", "format": "{UPPER}-USD"}}]}}
    with lib._conn() as con:
        r = library_resolve.Resolver(con)
        urls, why = library_resolve.candidate_urls(rec, "Denver CO", {}, r)
        assert not why and urls[0]["url"] == "https://x.example.org/p/CO-USD"
        rec["access"]["params"][0]["fill"]["format"] = "{LOWER}"  # still refused, honestly
        assert library_resolve.candidate_urls(rec, "Denver CO", {}, r)[1].endswith("isn't supported yet")
