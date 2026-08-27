"""Feed subscriptions: parser (RSS2 + Atom), store dedup, routes, and the refresh tick."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import feeds as feedsmod
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key
from smartbrain_3000.vaults import VaultStore

_LOCAL = {"X-SB-Local": "1"}

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Blog</title>
<item><title>First post</title><link>https://blog.example/1</link>
<guid>https://blog.example/1</guid><pubDate>Fri, 22 Aug 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;</description></item>
<item><title>Second post</title><link>https://blog.example/2</link>
<guid>guid-2</guid><description>More text</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>Entry one</title><id>tag:example,2026:1</id>
<link rel="alternate" href="https://atom.example/1"/>
<summary>Summary text</summary><published>2026-08-22T10:00:00Z</published></entry>
</feed>"""


# --- parser ----------------------------------------------------------------

def test_parse_rss2() -> None:
    parsed = feedsmod.parse_feed(RSS)
    assert parsed["title"] == "Test Blog"
    assert len(parsed["items"]) == 2
    first = parsed["items"][0]
    assert first["guid"] == "https://blog.example/1"
    assert first["summary"] == "Hello & welcome"  # tags stripped, entities decoded
    assert first["published"].startswith("Fri, 22 Aug")


def test_parse_atom() -> None:
    parsed = feedsmod.parse_feed(ATOM)
    assert parsed["title"] == "Atom Feed"
    item = parsed["items"][0]
    assert item["guid"] == "tag:example,2026:1"
    assert item["link"] == "https://atom.example/1"
    assert item["summary"] == "Summary text"


def test_parse_rejects_junk() -> None:
    with pytest.raises(feedsmod.FeedError):
        feedsmod.parse_feed("this is not xml at all <")
    with pytest.raises(feedsmod.FeedError):
        feedsmod.parse_feed("<html><body>a web page</body></html>")


def test_parse_caps_items() -> None:
    many = "".join(
        f"<item><title>t{i}</title><guid>g{i}</guid></item>" for i in range(200)
    )
    parsed = feedsmod.parse_feed(f"<rss><channel><title>Big</title>{many}</channel></rss>")
    assert len(parsed["items"]) == feedsmod._MAX_ITEMS_PER_FETCH


def test_parse_guid_falls_back_to_link_then_title() -> None:
    xml = ("<rss><channel><title>F</title>"
           "<item><title>only title</title></item>"
           "<item><title>x</title><link>https://l.example/a</link></item>"
           "</channel></rss>")
    items = feedsmod.parse_feed(xml)["items"]
    assert items[0]["guid"] == "only title"
    assert items[1]["guid"] == "https://l.example/a"


def test_safe_status_strips_addresses() -> None:
    # last_status is a PLAINTEXT column; netguard messages that embed the address
    # must lose it before storage.
    assert feedsmod._safe_status(Exception("cannot resolve host: gaierror(8, 'x.example')")) == "cannot resolve host"
    assert feedsmod._safe_status(Exception("blocked non-global address: 192.168.1.5")) == "blocked non-global address"
    assert feedsmod._safe_status(Exception("host too slow")) == "host too slow"


# --- store + ingest --------------------------------------------------------

def _stores() -> tuple:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return feedsmod.FeedStore(conn, key), KnowledgeBase(conn, key), VaultStore(conn, key), conn


def test_store_roundtrip_and_delete() -> None:
    store, _kb, vaults, _conn = _stores()
    vid = vaults.create("Test Blog", tags=["feed"])
    fid = store.add("https://blog.example/feed.xml", "Test Blog", vid)
    got = store.get(fid)
    assert got["url"] == "https://blog.example/feed.xml" and got["vault_id"] == vid
    assert len(store.list_feeds()) == 1
    store.delete(fid)
    assert store.get(fid) is None and store.list_feeds() == []


def test_feed_tags_stamp_every_ingested_doc() -> None:
    store, kb, vaults, _conn = _stores()
    vid = vaults.create("Test Blog", tags=["feed"])
    fid = store.add("https://blog.example/feed.xml", "Test Blog", vid, tags=["spac", " spac ", ""])
    feed = store.get(fid)
    assert feed["tags"] == ["spac", " spac ", ""]  # sealed as given; kb cleans on ingest
    assert feedsmod.ingest_new_items(store, kb, vaults, feed, feedsmod.parse_feed(RSS)) == 2
    for doc_id in vaults.document_ids(vid):
        assert kb.get(doc_id)["tags"] == ["spac"]  # trimmed + de-duped by the kb rules


def test_ingest_dedupes_by_guid() -> None:
    store, kb, vaults, conn = _stores()
    vid = vaults.create("Test Blog", tags=["feed"])
    fid = store.add("https://blog.example/feed.xml", "Test Blog", vid)
    feed = store.get(fid)
    parsed = feedsmod.parse_feed(RSS)
    assert feedsmod.ingest_new_items(store, kb, vaults, feed, parsed) == 2
    assert feedsmod.ingest_new_items(store, kb, vaults, feed, parsed) == 0  # all seen
    assert conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0] == 2
    assert len(vaults.document_ids(vid)) == 2
    doc = kb.get(vaults.document_ids(vid)[0])
    assert doc is not None and "Test Blog" in doc["title"]


# --- routes ----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "t.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        c.post("/api/account/setup", json={"passphrase": "feed-test-pass"})
        yield c


def test_feed_routes_full_lifecycle(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(feedsmod.netguard, "validate_public_url", lambda url: None)
    monkeypatch.setattr(feedsmod, "fetch_and_parse", lambda url: feedsmod.parse_feed(RSS))
    import smartbrain_3000.feed_routes as fr
    monkeypatch.setattr(fr.netguard, "validate_public_url", lambda url: None)
    monkeypatch.setattr(fr.feedsmod, "fetch_and_parse", lambda url: feedsmod.parse_feed(RSS))

    r = client.post("/api/feeds", json={"url": "https://blog.example/feed.xml", "tags": ["spac"]},
                    headers=_LOCAL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Test Blog" and body["items"] == 2

    # The feed's tag rode into both ingested documents.
    docs = client.get("/api/kb").json()["documents"]
    assert len(docs) == 2 and all(d["tags"] == ["spac"] for d in docs)

    listed = client.get("/api/feeds").json()["feeds"]
    assert len(listed) == 1 and listed[0]["last_status"] == "ok: 2 new"

    # Refresh with the same items: dedup makes it a clean no-op.
    r = client.post(f"/api/feeds/{body['id']}/refresh")
    assert r.status_code == 200 and r.json()["items"] == 0

    # Delete keeping docs (default): grouping gone, documents stay.
    r = client.delete(f"/api/feeds/{body['id']}", headers=_LOCAL)
    assert r.status_code == 200 and r.json()["docs_removed"] == 0
    assert client.get("/api/feeds").json()["feeds"] == []
    docs = client.get("/api/kb").json()
    assert len(docs["documents"]) == 2


def test_feed_delete_can_remove_docs(client: TestClient, monkeypatch) -> None:
    import smartbrain_3000.feed_routes as fr
    monkeypatch.setattr(fr.netguard, "validate_public_url", lambda url: None)
    monkeypatch.setattr(fr.feedsmod, "fetch_and_parse", lambda url: feedsmod.parse_feed(RSS))
    body = client.post("/api/feeds", json={"url": "https://blog.example/feed.xml"}, headers=_LOCAL).json()
    r = client.delete(f"/api/feeds/{body['id']}?remove_docs=1", headers=_LOCAL)
    assert r.status_code == 200 and r.json()["docs_removed"] == 2
    assert client.get("/api/kb").json()["documents"] == []


def test_feed_add_rejects_unfetchable(client: TestClient, monkeypatch) -> None:
    import smartbrain_3000.feed_routes as fr
    monkeypatch.setattr(fr.netguard, "validate_public_url", lambda url: None)
    def boom(url):
        raise feedsmod.FeedError("not an RSS or Atom feed")
    monkeypatch.setattr(fr.feedsmod, "fetch_and_parse", boom)
    r = client.post("/api/feeds", json={"url": "https://blog.example/nope"}, headers=_LOCAL)
    assert r.status_code == 400
    assert client.get("/api/feeds").json()["feeds"] == []  # nothing half-created


def test_feed_routes_locked(client: TestClient) -> None:
    client.post("/api/account/lock")
    assert client.get("/api/feeds").status_code == 423


# --- tick ------------------------------------------------------------------

def test_tick_refreshes_due_and_isolates_failures(client: TestClient, monkeypatch) -> None:
    import smartbrain_3000.feed_routes as fr
    monkeypatch.setattr(fr.netguard, "validate_public_url", lambda url: None)
    monkeypatch.setattr(fr.feedsmod, "fetch_and_parse", lambda url: feedsmod.parse_feed(RSS))
    good = client.post("/api/feeds", json={"url": "https://good.example/f.xml"}, headers=_LOCAL).json()
    monkeypatch.setattr(fr.feedsmod, "fetch_and_parse", lambda url: feedsmod.parse_feed(ATOM))
    bad = client.post("/api/feeds", json={"url": "https://bad.example/f.xml"}, headers=_LOCAL).json()

    # Age both so they're due, then tick with a fetch that fails for one host only.
    app = client.app
    app.state.db.execute("UPDATE feeds SET last_checked = now() - to_seconds(999999);")
    def fetch(url):
        if "bad.example" in url:
            raise feedsmod.FeedError("gone")
        return feedsmod.parse_feed(RSS)
    monkeypatch.setattr(feedsmod, "fetch_and_parse", fetch)
    feedsmod.tick(app)

    feeds_now = {f["id"]: f for f in client.get("/api/feeds").json()["feeds"]}
    assert feeds_now[good["id"]]["last_status"].startswith("ok")
    assert feeds_now[bad["id"]]["last_status"].startswith("error")


def test_feed_items_are_feed_origin_so_the_model_sees_them_as_outside_words() -> None:
    from smartbrain_3000 import vaults as vaults_mod
    store, kb, vaults, _conn = _stores()
    vid = vaults.create("Test Blog", tags=["feed"])
    feed = store.get(store.add("https://blog.example/feed.xml", "Test Blog", vid))
    assert feedsmod.ingest_new_items(store, kb, vaults, feed, feedsmod.parse_feed(RSS)) == 2
    for doc_id in vaults.document_ids(vid):
        assert vaults.origin_of(vid, doc_id) == vaults_mod.FEED
        assert vaults.import_provenance(doc_id)["origin"] == vaults_mod.FEED
    # vault-owned copies: removable alongside the vault, but never rename/delete-blocked
    assert set(vaults.import_origin_doc_ids(vid)) == set(vaults.document_ids(vid))
    from fastapi import HTTPException
    from smartbrain_3000 import kb_routes

    class _Req:
        class app:
            class state:
                pass
    _Req.app.state.vaults = vaults
    for doc_id in vaults.document_ids(vid):
        kb_routes._refuse_if_vault_owned(_Req, doc_id)  # no 409 for a feed item

    class _Imported:
        def import_provenance(self, doc_id):
            return {"origin": "import", "name": "Other", "vault_id": "v"}
    _Req.app.state.vaults = _Imported()
    try:
        kb_routes._refuse_if_vault_owned(_Req, "x")
        raise AssertionError("import-origin copies must still be refused")
    except HTTPException as exc:
        assert exc.status_code == 409
