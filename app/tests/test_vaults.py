"""Vaults: a named, selectable subset of the knowledge base, and searching within one.

This is the collection primitive the shareable ``.sbvault`` artifact will be built on. The two
properties that matter most here are the ones a user would be furious to get wrong:

- Deleting a vault must NOT delete its documents. It removes a grouping, not your files.
- Searching an EMPTY vault must return nothing — not silently fall back to the whole library.
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key
from smartbrain_3000.vaults import IMPORTED, VaultStore


def _stores() -> tuple[KnowledgeBase, VaultStore]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return KnowledgeBase(conn, key), VaultStore(conn, key)


# --- the store ---------------------------------------------------------------------------------

def test_create_list_and_rename() -> None:
    _, vs = _stores()
    vid = vs.create("Property", "leases and deeds")
    got = vs.get(vid)
    assert got["name"] == "Property" and got["description"] == "leases and deeds"
    assert got["kind"] == "local" and got["version"] == 1 and got["doc_count"] == 0

    vs.update(vid, "Real estate", "renamed")
    assert vs.get(vid)["name"] == "Real estate"
    assert [v["id"] for v in vs.list_vaults()] == [vid]


def test_vault_name_is_encrypted_at_rest() -> None:
    # What you called a collection ("Divorce", "Cancer treatment") reveals as much as its contents.
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    VaultStore(conn, gen_master_key()).create("Divorce", "very private")
    raw = bytes(conn.execute("SELECT ciphertext FROM vaults;").fetchone()[0])
    assert b"Divorce" not in raw and b"very private" not in raw


def test_a_document_can_belong_to_several_vaults() -> None:
    # A lease belongs in both "Property" and "2026 taxes" — membership can't be a column on the doc.
    kb, vs = _stores()
    doc = kb.add("Lease", "the lease renews in March")
    a, b = vs.create("Property"), vs.create("2026 taxes")
    vs.add_documents(a, [doc])
    vs.add_documents(b, [doc])
    assert set(vs.vaults_for_document(doc)) == {a, b}


def test_adding_the_same_document_twice_is_a_noop() -> None:
    kb, vs = _stores()
    doc = kb.add("Doc", "body")
    vid = vs.create("V")
    assert vs.add_documents(vid, [doc]) == 1
    assert vs.add_documents(vid, [doc]) == 0, "idempotent — not an error"
    assert vs.count_documents(vid) == 1


def test_deleting_a_vault_does_not_delete_its_documents() -> None:
    # The one that would make a user furious.
    kb, vs = _stores()
    doc = kb.add("Precious", "irreplaceable content")
    vid = vs.create("Temp")
    vs.add_documents(vid, [doc])
    vs.delete(vid)
    assert vs.get(vid) is None
    assert kb.get(doc) is not None, "removing a grouping must not shred the files in it"


def test_removing_a_document_from_a_vault_does_not_delete_it() -> None:
    kb, vs = _stores()
    doc = kb.add("Doc", "body")
    vid = vs.create("V")
    vs.add_documents(vid, [doc])
    vs.remove_documents(vid, [doc])
    assert vs.count_documents(vid) == 0
    assert kb.get(doc) is not None


def test_forget_document_clears_ghost_membership() -> None:
    kb, vs = _stores()
    doc = kb.add("Doc", "body")
    vid = vs.create("V")
    vs.add_documents(vid, [doc])
    kb.delete(doc)
    vs.forget_document(doc)
    assert vs.count_documents(vid) == 0, "a vault must not point at a document that no longer exists"


def test_imported_vaults_are_marked_and_keep_their_source() -> None:
    _, vs = _stores()
    source = {"url": "https://example.com/expert.sbvault", "publisher_pubkey": "abc123"}
    vid = vs.create("Expert pack", "from a friend", kind=IMPORTED, source=source)
    got = vs.get(vid)
    assert got["kind"] == IMPORTED and got["source"] == source

    vs.update(vid, "Renamed pack")
    assert vs.get(vid)["source"] == source, "a rename must not lose where an import came from"


def test_bump_version_is_monotonic() -> None:
    _, vs = _stores()
    vid = vs.create("V")
    assert vs.bump_version(vid) == 2
    assert vs.bump_version(vid) == 3


# --- scoped search -----------------------------------------------------------------------------

def test_search_scoped_to_a_vault_ignores_documents_outside_it() -> None:
    kb, vs = _stores()
    inside = kb.add("In", "the QUOKKA report")
    kb.add("Out", "the QUOKKA memo")  # same term, not in the vault
    vid = vs.create("V")
    vs.add_documents(vid, [inside])

    assert len(kb.search("quokka")) == 2  # unscoped: both
    scoped = kb.search("quokka", scope=set(vs.document_ids(vid)))
    assert [h["id"] for h in scoped] == [inside]


def test_scoped_hybrid_search_also_restricts_the_semantic_half() -> None:
    kb, vs = _stores()
    inside = kb.add("In", "alpha")
    outside = kb.add("Out", "alpha")
    kb.put_embedding(inside, [1.0, 0.0], "m")
    kb.put_embedding(outside, [1.0, 0.0], "m")  # equally similar
    vid = vs.create("V")
    vs.add_documents(vid, [inside])

    scope = set(vs.document_ids(vid))
    assert [h["id"] for h in kb.semantic_search([1.0, 0.0], "m", scope=scope)] == [inside]
    assert [h["id"] for h in kb.hybrid_search("alpha", [1.0, 0.0], "m", scope=scope)] == [inside]


def test_searching_an_empty_vault_finds_nothing_rather_than_everything() -> None:
    # The dangerous default: an empty scope must NOT be treated as "no scope".
    kb, vs = _stores()
    kb.add("Doc", "the quokka report")
    vid = vs.create("Empty")
    assert kb.search("quokka", scope=set(vs.document_ids(vid))) == []


# --- the API -----------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "v.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        c.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield c


def test_vault_api_requires_unlock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "locked.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/vaults").status_code == 423
        assert c.post("/api/vaults", json={"name": "V"}).status_code == 423


def test_vault_crud_and_membership_via_api(client: TestClient) -> None:
    doc = client.post("/api/kb", json={"title": "Lease", "content": "renews in March"}).json()["id"]
    vid = client.post("/api/vaults", json={"name": "Property", "description": "deeds"}).json()["id"]

    assert client.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [doc]}).json() == {
        "added": 1, "doc_count": 1,
    }
    got = client.get(f"/api/vaults/{vid}").json()
    assert got["name"] == "Property" and got["doc_ids"] == [doc]

    client.patch(f"/api/vaults/{vid}", json={"name": "Real estate"})
    assert client.get("/api/vaults").json()["vaults"][0]["name"] == "Real estate"

    client.delete(f"/api/vaults/{vid}/documents/{doc}")
    assert client.get(f"/api/vaults/{vid}").json()["doc_count"] == 0
    assert client.get(f"/api/kb/{doc}").status_code == 200, "the document itself survives"


def test_search_can_be_scoped_to_a_vault_via_the_api(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(gateway, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    inside = client.post("/api/kb", json={"title": "In", "content": "the quokka report"}).json()["id"]
    client.post("/api/kb", json={"title": "Out", "content": "the quokka memo"})
    vid = client.post("/api/vaults", json={"name": "V"}).json()["id"]
    client.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [inside]})

    everything = client.get("/api/kb/search", params={"q": "quokka"}).json()["results"]
    assert len(everything) == 2
    scoped = client.get("/api/kb/search", params={"q": "quokka", "vault": vid}).json()["results"]
    assert [h["id"] for h in scoped] == [inside]


def test_scoping_to_an_unknown_vault_is_a_404_not_a_silent_full_search(client: TestClient) -> None:
    client.post("/api/kb", json={"title": "Doc", "content": "quokka"})
    r = client.get("/api/kb/search", params={"q": "quokka", "vault": "no-such-vault"})
    assert r.status_code == 404


def test_deleting_a_document_removes_it_from_its_vaults(client: TestClient) -> None:
    doc = client.post("/api/kb", json={"title": "Doc", "content": "body"}).json()["id"]
    vid = client.post("/api/vaults", json={"name": "V"}).json()["id"]
    client.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [doc]})
    client.delete(f"/api/kb/{doc}")
    assert client.get(f"/api/vaults/{vid}").json()["doc_count"] == 0, "no ghost members"
