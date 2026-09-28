"""NI Library page backend: the pinned pack (install/verify/read), search, and the user's sealed local
sources — plus the /api/library routes. The pack here is a REAL DuckDB file in the exact schema
``sourcetool build`` produces; only the network fetch is replaced."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_index, netguard, ni
from smartbrain_3000.secrets import gen_master_key

_TAXONOMY = [
    ("water", "tides", "Water & Coast › Tides", ["tide", "tides", "high tide"]),
    ("markets", "crypto", "Markets & Money › Crypto", ["bitcoin", "crypto"]),
    ("tech", "service_status", "Tech & Internet › Service status", ["status", "down"]),
]
_SOURCES = [  # id, name, tier, status, prior, category, terms
    ("coops-tide-hilo", "NOAA tide predictions", "curated", "ok", 2.0, ("water", "tides"),
     {"tide": 3.0, "tides": 3.0, "noaa": 1.5, "predictions": 3.0}),
    ("coingecko-price", "CoinGecko coin price", "curated", "ok", 1.8, ("markets", "crypto"),
     {"bitcoin": 2.5, "price": 3.0, "coin": 3.0}),
    ("dead-crypto", "Dead crypto feed", "harvested", "failed", -1.0, ("markets", "crypto"),
     {"bitcoin": 5.0, "price": 5.0}),
    ("statuspage-summary", "Service status", "curated", "ok", 1.5, ("tech", "service_status"),
     {"status": 3.0, "github": 2.0, "down": 2.5}),
]


def _build_pack(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE library_sources(
        id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, provider_id VARCHAR, provider_name VARCHAR,
        authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR, access_kind VARCHAR, url_template VARCHAR,
        docs_url VARCHAR, auth VARCHAR, terms_status VARCHAR, cadence VARCHAR, validation_status VARCHAR,
        robots VARCHAR, votes_yes INTEGER, votes_no INTEGER, prior DOUBLE, record JSON)""")
    con.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con.execute("CREATE TABLE library_taxonomy(category VARCHAR, subcategory VARCHAR, label VARCHAR, "
                "kinds VARCHAR[], params VARCHAR[], keywords VARCHAR[])")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    for cat, sub, label, kw in _TAXONOMY:
        con.execute("INSERT INTO library_taxonomy VALUES (?,?,?,?,?,?)", (cat, sub, label, [], [], kw))
    for sid, name, tier, status, prior, (cat, sub), terms in _SOURCES:
        rec = {"id": sid, "name": name, "tier": tier, "categories": [f"{cat}/{sub}"]}
        con.execute("INSERT INTO library_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, name, f"{name} description", "p", "Provider", "official", tier, "US", "", "http_json",
                     "https://example.org/x", "https://example.org", "none", "public_domain", "hourly", status,
                     "allow", 0, 0, prior, json.dumps(rec)))
        con.execute("INSERT INTO library_source_categories VALUES (?,?,?)", (sid, cat, sub))
        for t, w in terms.items():
            con.execute("INSERT INTO library_terms VALUES (?,?,?)", (t, sid, w))
    con.execute("INSERT INTO library_meta VALUES ('built_at','2026-09-28T00:00:00Z'), ('records','4'), "
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
    assert st["installed"] and st["records"] == 4 and st["by_status"]["ok"] == 3


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
    assert cats["markets"]["count"] == 1  # dead-crypto (failed) is not counted
    assert cats["water"]["subcategories"][0] == {"id": "tides", "label": "Tides", "count": 1,
                                                 "keywords": ["tide", "tides", "high tide"]}


def test_search_ranks_by_relevance_and_hides_failed(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    r = idx.search("bitcoin price")
    assert [x["id"] for x in r["results"]] == ["coingecko-price"]  # the failed feed never shows by default
    assert idx.search("tides for Charleston today")["results"][0]["id"] == "coops-tide-hilo"
    assert idx.search("is github down")["results"][0]["id"] == "statuspage-summary"
    assert idx.search("bitcoin", status="failed")["results"][0]["id"] == "dead-crypto"


def test_browse_without_query_orders_by_prior_and_filters(tmp_path, pack_bytes) -> None:
    idx, _ = _index(tmp_path, pack_bytes)
    idx.install()
    assert [x["id"] for x in idx.search()["results"]] == ["coops-tide-hilo", "coingecko-price",
                                                          "statuspage-summary"]
    only = idx.search(category="markets", subcategory="crypto")
    assert only["total"] == 1 and only["results"][0]["categories"] == ["markets/crypto"]
    page = idx.search(offset=1, limit=1)
    assert page["total"] == 3 and [x["id"] for x in page["results"]] == ["coingecko-price"]
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
    assert st["installed"] and st["records"] == 4
    cats = client.get("/api/library/taxonomy").json()["categories"]
    assert {c["id"] for c in cats} == {"water", "markets", "tech"}
    assert "keywords" not in cats[0]["subcategories"][0]
    hits = client.get("/api/library/sources", params={"q": "bitcoin price"}).json()
    assert [r["id"] for r in hits["results"]] == ["coingecko-price"] and hits["local"] == []
    assert client.get("/api/library/sources/coops-tide-hilo").json()["name"] == "NOAA tide predictions"
    assert client.get("/api/library/sources/nope").status_code == 404
    bad = client.post("/api/library/local", json=_form(url="http://x.example.com/a"))
    assert bad.status_code == 400 and "https://" in bad.json()["detail"]
    added = client.post("/api/library/local", json=_form()).json()
    assert added["tier"] == "local"
    mine = client.get("/api/library/sources", params={"q": "harbor"}).json()["local"]
    assert [r["id"] for r in mine] == [added["id"]]
    assert client.get(f"/api/library/sources/{added['id']}").json()["tier"] == "local"
    assert [r["id"] for r in client.get("/api/library/local").json()["sources"]] == [added["id"]]
    assert client.delete(f"/api/library/local/{added['id']}").json() == {"ok": True}
    assert client.delete(f"/api/library/local/{added['id']}").status_code == 404
