"""Tests for the encrypted Knowledge base + its HTTP API (KB1)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key


def _kb() -> KnowledgeBase:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)  # creates the documents table
    return KnowledgeBase(conn, gen_master_key())


def test_add_get_roundtrip() -> None:
    kb = _kb()
    doc_id = kb.add("My Title", "the body text")
    doc = kb.get(doc_id)
    assert doc["id"] == doc_id
    assert doc["title"] == "My Title"
    assert doc["content"] == "the body text"


def test_get_absent_returns_none() -> None:
    assert _kb().get("nope") is None


def test_list_returns_titles_not_bodies() -> None:
    kb = _kb()
    kb.add("A", "alpha")
    kb.add("B", "beta")
    docs = kb.list_docs()
    assert {d["title"] for d in docs} == {"A", "B"}
    assert all("content" not in d for d in docs)


def test_delete() -> None:
    kb = _kb()
    doc_id = kb.add("t", "c")
    kb.delete(doc_id)
    assert kb.get(doc_id) is None


def test_search_matches_and_snippet() -> None:
    kb = _kb()
    kb.add("Cooking", "pasta with tomato sauce and basil")
    kb.add("Travel", "flights to rome and venice")
    results = kb.search("tomato")
    assert len(results) == 1
    assert results[0]["title"] == "Cooking"
    assert "tomato" in results[0]["snippet"].lower()


def test_search_ranks_by_frequency() -> None:
    kb = _kb()
    kb.add("One", "rome rome rome")
    kb.add("Two", "rome once")
    assert [r["title"] for r in kb.search("rome")] == ["One", "Two"]


def test_content_encrypted_at_rest() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    KnowledgeBase(conn, gen_master_key()).add("Secret", "super-secret-knowledge")
    raw = bytes(conn.execute("SELECT ciphertext FROM documents;").fetchone()[0])
    assert b"super-secret-knowledge" not in raw  # body encrypted
    assert b"Secret" not in raw  # title encrypted too


def test_wrong_key_cannot_read() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    doc_id = KnowledgeBase(conn, gen_master_key()).add("t", "c")
    other = KnowledgeBase(conn, gen_master_key())
    with pytest.raises(Exception):
        other.get(doc_id)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_kb_requires_unlock(client: TestClient) -> None:
    assert client.post("/api/kb", json={"title": "t", "content": "c"}).status_code == 423
    assert client.get("/api/kb").status_code == 423
    assert client.get("/api/kb/search", params={"q": "x"}).status_code == 423


def test_kb_crud_and_search_via_api(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    doc_id = client.post(
        "/api/kb", json={"title": "Notes", "content": "buy milk and eggs"}
    ).json()["id"]
    assert client.get("/api/kb").json()["documents"][0]["title"] == "Notes"
    assert client.get(f"/api/kb/{doc_id}").json()["content"] == "buy milk and eggs"
    results = client.get("/api/kb/search", params={"q": "milk"}).json()["results"]
    assert results and results[0]["id"] == doc_id
    assert client.delete(f"/api/kb/{doc_id}").json() == {"ok": True}
    assert client.get(f"/api/kb/{doc_id}").status_code == 404


def test_rename_doc_changes_title_keeps_content(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    doc_id = client.post("/api/kb", json={"title": "1706.03762", "content": "Attention is all you need"}).json()["id"]
    assert client.patch(f"/api/kb/{doc_id}", json={"title": "Transformers paper"}).json() == {"ok": True}
    got = client.get(f"/api/kb/{doc_id}").json()
    assert got["title"] == "Transformers paper" and got["content"] == "Attention is all you need"


def test_rename_missing_doc_404(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.patch("/api/kb/nope", json={"title": "x"}).status_code == 404


def test_kb_search_route_not_shadowed_by_id(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.get("/api/kb/search", params={"q": "anything"})
    assert r.status_code == 200 and "results" in r.json()


def test_explicit_lexical_mode_never_calls_the_gateway(client: TestClient, monkeypatch) -> None:
    # The default is now hybrid (better results), but a pure-keyword search must stay available and
    # gateway-free: it is the fast path, and it works with no embed model configured at all.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Notes", "content": "buy milk"})

    def boom(*a, **k):
        raise AssertionError("gateway must not be called in lexical mode")

    monkeypatch.setattr(gateway, "embed", boom)
    r = client.get("/api/kb/search", params={"q": "milk", "mode": "lexical"})
    assert r.status_code == 200
    assert r.json()["results"][0]["title"] == "Notes"
    assert r.json()["degraded"] is False


def test_default_search_is_hybrid_and_still_works_with_no_gateway(client: TestClient, monkeypatch) -> None:
    # Hybrid is the default because keyword and vector search miss in opposite directions. It must
    # degrade to keyword-only (and SAY so) when no embed model is reachable — never just fail.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Notes", "content": "buy milk"})
    monkeypatch.setattr(gateway, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    r = client.get("/api/kb/search", params={"q": "milk"})  # no mode -> hybrid
    assert r.status_code == 200
    assert r.json()["results"][0]["title"] == "Notes"
    assert r.json()["degraded"] is True


def test_search_semantic_falls_back_when_gateway_unreachable(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Notes", "content": "buy milk"})

    def boom(input_text, model, **k):
        raise gateway.GatewayError(502, "ollama down")

    monkeypatch.setattr(gateway, "embed", boom)
    r = client.get("/api/kb/search", params={"q": "milk", "mode": "semantic"})
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["results"][0]["title"] == "Notes"  # lexical fallback still works


def test_search_semantic_empty_index_not_degraded(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Notes", "content": "buy milk"})  # no embedding yet
    monkeypatch.setattr(gateway, "embed", lambda input_text, model, **k: [1.0, 0.0, 0.0])
    r = client.get("/api/kb/search", params={"q": "milk", "mode": "semantic"})
    assert r.status_code == 200
    assert r.json() == {"results": [], "degraded": False}


def test_search_invalid_mode_400(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.get("/api/kb/search", params={"q": "x", "mode": "bogus"}).status_code == 400


def test_reindex_backfills_and_enables_semantic(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Notes", "content": "buy milk"})
    monkeypatch.setattr(gateway, "embed", lambda input_text, model, **k: [1.0, 0.0, 0.0])
    assert client.post("/api/kb/reindex").json() == {"embedded": 1, "skipped": 0, "failed": 0, "error": "", "pending": 0}
    r = client.get("/api/kb/search", params={"q": "anything", "mode": "semantic"})
    assert r.json()["degraded"] is False
    assert r.json()["results"][0]["title"] == "Notes"  # now semantically findable


def test_reindex_best_effort_continues_on_failure(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Good", "content": "ok"})
    client.post("/api/kb", json={"title": "Bad", "content": "boom"})

    def maybe(input_text, model, **k):
        if "boom" in input_text:  # embedded text is now "title\ncontent"
            raise gateway.GatewayError(502, "nope")
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(gateway, "embed", maybe)
    out = client.post("/api/kb/reindex").json()
    # `pending` is the document whose embed failed: it is still un-indexed, and reindex must say so
    # rather than report a clean finish.
    assert out == {"embedded": 1, "skipped": 0, "failed": 1, "error": "nope", "pending": 1}


def test_reindex_idempotent(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "N", "content": "c"})
    monkeypatch.setattr(gateway, "embed", lambda input_text, model, **k: [1.0, 0.0, 0.0])
    assert client.post("/api/kb/reindex").json() == {"embedded": 1, "skipped": 0, "failed": 0, "error": "", "pending": 0}
    assert client.post("/api/kb/reindex").json() == {"embedded": 0, "skipped": 0, "failed": 0, "error": "", "pending": 0}


def test_reindex_embeds_title(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Bifrost", "content": "see notes"})
    seen: list[str] = []
    monkeypatch.setattr(gateway, "embed", lambda input_text, model, **k: seen.append(input_text) or [1.0, 0.0])
    client.post("/api/kb/reindex")
    assert seen and "Bifrost" in seen[0]  # the title is part of the embedded text


def test_semantic_search_ranks_by_meaning_not_constant(client: TestClient, monkeypatch) -> None:
    # A REAL ranking test: a deterministic bag-of-words embedder (different texts -> DIFFERENT
    # vectors) must surface the content-relevant doc first. Constant-vector tests would pass
    # even if cosine ranking were completely broken — this one would not.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Errand", "content": "buy milk at the store"})
    client.post("/api/kb", json={"title": "Work", "content": "fix the python bug in the code"})
    vocab = ("milk", "store", "groceries", "python", "bug", "code")

    def bow(input_text: str, model: str, **_k) -> list[float]:
        text = input_text.lower()
        return [float(text.count(w)) for w in vocab]

    monkeypatch.setattr(gateway, "embed", bow)
    assert client.post("/api/kb/reindex").json()["embedded"] == 2
    milk = client.get("/api/kb/search", params={"q": "milk", "mode": "semantic"}).json()
    assert milk["degraded"] is False and milk["results"][0]["title"] == "Errand"
    code = client.get("/api/kb/search", params={"q": "python bug", "mode": "semantic"}).json()
    assert code["results"][0]["title"] == "Work"  # a different query yields a different top hit


# --- /api/kb/ingest-url + /api/kb/reindex route error paths ---------------

def test_ingest_url_unreachable_returns_400_with_reason(client: TestClient, monkeypatch) -> None:
    # Contract: a user-supplied URL that can't be fetched (SSRF-blocked, unreachable,
    # timeout) or yields no text is a client-fixable 400 — and the reason is surfaced
    # in `detail` so the UI shows why (describeError passes 4xx detail through).
    from smartbrain_3000 import netguard

    client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def unreachable(url):
        raise netguard.FetchError("upstream unreachable: timed out")

    monkeypatch.setattr(netguard, "safe_fetch_bytes", unreachable)
    r = client.post("/api/kb/ingest-url", json={"url": "https://example.com/unreachable"})
    assert r.status_code == 400
    assert "unreachable" in r.json().get("detail", "").lower()


def test_reindex_route_all_failed_returns_200(client: TestClient, monkeypatch) -> None:
    # If the gateway/embed is completely down, reindex must STILL return 200 with
    # failed>0 (best-effort), not bubble a 500. Doc-level failure already covered;
    # this asserts the route surface when every doc fails.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "A", "content": "a"})
    client.post("/api/kb", json={"title": "B", "content": "b"})

    def down(input_text, model, **k):
        raise gateway.GatewayError(502, "ollama down")

    monkeypatch.setattr(gateway, "embed", down)
    r = client.post("/api/kb/reindex")
    assert r.status_code == 200  # NOT a 500
    body = r.json()
    assert body["failed"] >= 2 and body["embedded"] == 0
    assert "ollama down" in body["error"]


# --- manual tags (lexical-only) -----------------------------------------------------------------

def test_set_tags_roundtrip_cleaned_and_bounded() -> None:
    kb = _kb()
    doc_id = kb.add("Lease", "the rental agreement")
    assert kb.set_tags(doc_id, [" property ", "property", "", "2024"]) is True
    assert kb.get(doc_id)["tags"] == ["property", "2024"]  # trimmed, de-duped, blanks dropped
    (row,) = [d for d in kb.list_docs() if d["id"] == doc_id]
    assert row["tags"] == ["property", "2024"]  # the list surfaces tags without content
    assert kb.set_tags(doc_id, [f"t{i}" for i in range(30)]) is True
    assert len(kb.get(doc_id)["tags"]) == 20  # bounded (mirrors planner)
    assert kb.set_tags("nope", ["x"]) is False


def test_rename_preserves_tags() -> None:
    # THE data-loss trap: rename re-seals the whole body, so tags must ride through.
    kb = _kb()
    doc_id = kb.add("Cryptic-scan-0042", "the deed to the house")
    kb.set_tags(doc_id, ["property"])
    assert kb.rename(doc_id, "House deed") is True
    doc = kb.get(doc_id)
    assert doc["title"] == "House deed" and doc["tags"] == ["property"]


def test_replace_drops_tags() -> None:
    # replace() is the vault-update primitive; vault-owned copies can't be tagged, so a
    # replace starting fresh is intentional (pinned so a future edit can't flip it silently).
    kb = _kb()
    doc_id = kb.add("Guide", "v1 text")
    kb.set_tags(doc_id, ["keep-me"])
    assert kb.replace(doc_id, "Guide", "v2 text") is True
    assert kb.get(doc_id)["tags"] == []


def test_lexical_search_finds_tag_instantly() -> None:
    kb = _kb()
    doc_id = kb.add("Scan 0042", "an agreement between the parties")
    kb.add("Recipes", "pasta with tomato sauce")
    assert kb.search("taxes") == []  # index is built now; no tag yet
    kb.set_tags(doc_id, ["taxes"])
    results = kb.search("taxes")  # in-memory index re-tokenized instantly — no reindex step
    assert [r["id"] for r in results] == [doc_id]


def test_legacy_body_without_tags_opens_and_indexes() -> None:
    # A document sealed before tags existed has no "tags" key; it must open as [] everywhere.
    import json as jsonmod
    import os as osmod
    import uuid as uuidmod

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    doc_id = str(uuidmod.uuid4())
    nonce = osmod.urandom(12)
    body = {"title": "Old doc", "content": "sealed before tags existed", "meta": {}}
    ciphertext = AESGCM(key).encrypt(nonce, jsonmod.dumps(body).encode(), doc_id.encode())
    conn.execute("INSERT INTO documents (id, nonce, ciphertext) VALUES (?, ?, ?);",
                 [doc_id, nonce, ciphertext])
    assert kb.get(doc_id)["tags"] == []
    (item,) = kb.iter_documents(limit=10)
    assert item == (doc_id, "Old doc", "sealed before tags existed", [])
    assert [r["id"] for r in kb.search("sealed")] == [doc_id]


def test_patch_tags_via_api(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    doc_id = client.post("/api/kb", json={"title": "Notes", "content": "x"}).json()["id"]
    # tags only: title untouched
    assert client.patch(f"/api/kb/{doc_id}", json={"tags": ["a", "b"]}).json() == {"ok": True}
    got = client.get(f"/api/kb/{doc_id}").json()
    assert got["title"] == "Notes" and got["tags"] == ["a", "b"]
    assert client.get("/api/kb").json()["documents"][0]["tags"] == ["a", "b"]
    # title only: tags untouched
    client.patch(f"/api/kb/{doc_id}", json={"title": "Renamed"})
    assert client.get(f"/api/kb/{doc_id}").json()["tags"] == ["a", "b"]
    # clear with []
    client.patch(f"/api/kb/{doc_id}", json={"tags": []})
    assert client.get(f"/api/kb/{doc_id}").json()["tags"] == []
    # neither field -> 422; blank title -> 422; unknown doc -> 404
    assert client.patch(f"/api/kb/{doc_id}", json={}).status_code == 422
    assert client.patch(f"/api/kb/{doc_id}", json={"title": "   "}).status_code == 422
    assert client.patch("/api/kb/nope", json={"tags": ["x"]}).status_code == 404
