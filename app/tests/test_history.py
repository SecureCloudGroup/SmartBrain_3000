"""Tests for encrypted chat history: storage layer + HTTP API (H1)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000.history import ChatHistory
from smartbrain_3000.secrets import gen_master_key


def _hist(master_key: bytes | None = None) -> ChatHistory:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ChatHistory(conn, master_key or gen_master_key())


# --- storage layer --------------------------------------------------------

def test_create_list_get_conversation() -> None:
    h = _hist()
    cid = h.create_conversation("Trip planning")
    assert [c["title"] for c in h.list_conversations()] == ["Trip planning"]
    convo = h.get_conversation(cid)
    assert convo and convo["id"] == cid and convo["title"] == "Trip planning"
    assert h.get_conversation("missing") is None


def test_messages_roundtrip_in_order() -> None:
    h = _hist()
    cid = h.create_conversation("c")
    h.add_message(cid, "user", "hello")
    h.add_message(cid, "assistant", "hi there")
    msgs = h.get_messages(cid)
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "hello"), ("assistant", "hi there")]


def test_add_message_unknown_conversation_raises() -> None:
    with pytest.raises(ValueError):
        _hist().add_message("nope", "user", "x")


def test_add_message_rejects_bad_role() -> None:
    h = _hist()
    cid = h.create_conversation("c")
    with pytest.raises(AssertionError):
        h.add_message(cid, "robot", "x")


def test_rename_conversation() -> None:
    h = _hist()
    cid = h.create_conversation("old")
    h.rename_conversation(cid, "new")
    assert h.get_conversation(cid)["title"] == "new"


def test_delete_cascades_messages() -> None:
    h = _hist()
    cid = h.create_conversation("c")
    h.add_message(cid, "user", "hello")
    h.delete_conversation(cid)
    assert h.get_conversation(cid) is None
    assert h.get_messages(cid) == []  # messages gone, no orphans


def test_list_orders_by_recent_activity() -> None:
    h = _hist()
    first = h.create_conversation("first")
    second = h.create_conversation("second")
    h.add_message(first, "user", "bump")  # touches first.updated_at -> most recent
    assert [c["id"] for c in h.list_conversations()] == [first, second]


def test_content_encrypted_at_rest() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    h = ChatHistory(conn, gen_master_key())
    cid = h.create_conversation("Secret Title")
    h.add_message(cid, "user", "super-secret-message")
    raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM messages;").fetchall())
    raw += b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM conversations;").fetchall())
    assert b"super-secret-message" not in raw
    assert b"Secret Title" not in raw


def test_wrong_key_cannot_read() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    cid = ChatHistory(conn, gen_master_key()).create_conversation("t")
    with pytest.raises(Exception):
        ChatHistory(conn, gen_master_key()).get_conversation(cid)


def test_message_aad_domain_separated() -> None:
    # A conversation-title ciphertext must NOT authenticate as a message
    # (domain prefix differs), even reusing the same id.
    from cryptography.exceptions import InvalidTag

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    h = ChatHistory(conn, gen_master_key())
    cid = h.create_conversation("t")
    row = conn.execute("SELECT nonce, ciphertext FROM conversations WHERE id = ?;", [cid]).fetchone()
    conn.execute(
        "INSERT INTO messages (id, conversation_id, nonce, ciphertext) VALUES (?, ?, ?, ?);",
        [cid, cid, bytes(row[0]), bytes(row[1])],
    )
    with pytest.raises(InvalidTag):
        h.get_messages(cid)


# --- HTTP API -------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_history_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/conversations").status_code == 423
    assert client.post("/api/conversations", json={}).status_code == 423


def test_conversation_crud_via_api(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    cid = client.post("/api/conversations", json={"title": "Notes"}).json()["id"]
    assert client.get("/api/conversations").json()["conversations"][0]["title"] == "Notes"
    client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "hi"})
    client.post(f"/api/conversations/{cid}/messages", json={"role": "assistant", "content": "yo"})
    convo = client.get(f"/api/conversations/{cid}").json()
    assert [m["content"] for m in convo["messages"]] == ["hi", "yo"]
    assert client.patch(f"/api/conversations/{cid}", json={"title": "Renamed"}).json() == {"ok": True}
    assert client.get(f"/api/conversations/{cid}").json()["title"] == "Renamed"
    assert client.delete(f"/api/conversations/{cid}").json() == {"ok": True}
    assert client.get(f"/api/conversations/{cid}").status_code == 404


def test_add_message_to_missing_conversation_404(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.post("/api/conversations/nope/messages", json={"role": "user", "content": "x"})
    assert r.status_code == 404


def test_lock_clears_history(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/conversations", json={"title": "x"})
    client.post("/api/account/lock")
    assert client.get("/api/conversations").status_code == 423  # history dropped on lock


# --- pagination (M4) ------------------------------------------------------

def _seed_conversations(h: ChatHistory, n: int) -> list[str]:
    """Create n conversations bumping updated_at each time; return newest-first ids."""
    assert n >= 1, "n must be positive"
    ids: list[str] = []
    for i in range(n):  # bounded by caller
        cid = h.create_conversation(f"c{i:03d}")
        # Bump updated_at deterministically so newest-first order is stable.
        h.rename_conversation(cid, f"c{i:03d}")
        ids.append(cid)
    ids.reverse()  # newest-first matches list order
    return ids


def test_list_conversations_page_first_page_and_has_more() -> None:
    h = _hist()
    ids = _seed_conversations(h, 5)
    page = h.list_conversations_page(limit=2)
    assert [item["id"] for item in page["items"]] == ids[:2]
    assert page["has_more"] is True
    assert page["next_cursor"], "next_cursor must be set when has_more"


def test_list_conversations_page_cursor_no_overlap_no_gap() -> None:
    h = _hist()
    ids = _seed_conversations(h, 5)
    first = h.list_conversations_page(limit=2)
    second = h.list_conversations_page(before=first["next_cursor"], limit=2)
    third = h.list_conversations_page(before=second["next_cursor"], limit=2)
    seen = [item["id"] for item in first["items"] + second["items"] + third["items"]]
    assert seen == ids  # exact union, no overlap, no gap, newest-first preserved
    assert third["has_more"] is False
    assert third["next_cursor"] is None


def test_list_conversations_page_no_params_returns_newest_page() -> None:
    h = _hist()
    ids = _seed_conversations(h, 3)
    page = h.list_conversations_page()
    assert [item["id"] for item in page["items"]] == ids
    assert page["has_more"] is False


def test_list_conversations_page_clamps_to_max() -> None:
    from smartbrain_3000.history import _MAX_LIST_PAGE

    h = _hist()
    _seed_conversations(h, 3)
    page = h.list_conversations_page(limit=_MAX_LIST_PAGE + 10_000)
    assert len(page["items"]) <= _MAX_LIST_PAGE


def _seed_messages(h: ChatHistory, cid: str, n: int) -> list[str]:
    """Append n messages; return ids in oldest-first creation order."""
    assert n >= 1, "n must be positive"
    ids: list[str] = []
    for i in range(n):  # bounded by caller
        ids.append(h.add_message(cid, "user", f"m{i:03d}"))
    return ids


def test_get_messages_page_first_page_returns_newest_slice() -> None:
    h = _hist()
    cid = h.create_conversation("c")
    ids = _seed_messages(h, cid, 5)
    page = h.get_messages_page(cid, limit=2)
    # Items are oldest-first within the page; the page is the two NEWEST messages.
    assert [m["id"] for m in page["items"]] == ids[-2:]
    assert page["has_more"] is True
    assert page["next_cursor"], "next_cursor must be set when has_more"


def test_get_messages_page_cursor_no_overlap_no_gap() -> None:
    h = _hist()
    cid = h.create_conversation("c")
    ids = _seed_messages(h, cid, 5)
    first = h.get_messages_page(cid, limit=2)
    second = h.get_messages_page(cid, before=first["next_cursor"], limit=2)
    third = h.get_messages_page(cid, before=second["next_cursor"], limit=2)
    # Stitch oldest-first: third (oldest page) + second + first.
    combined = [m["id"] for m in third["items"] + second["items"] + first["items"]]
    assert combined == ids
    assert third["has_more"] is False
    assert third["next_cursor"] is None


def test_message_order_deterministic_under_created_at_ties() -> None:
    # Regression: messages that share a created_at tick must still come back in INSERTION order,
    # not random-UUID order. add_message stamps DEFAULT current_timestamp, which a fast clock can
    # collide (the source of the old flake); force the tie explicitly and assert both paths.
    h = _hist()
    cid = h.create_conversation("c")
    ids = _seed_messages(h, cid, 6)
    h._conn.execute(
        "UPDATE messages SET created_at = TIMESTAMP '2026-01-01 00:00:00' WHERE conversation_id = ?;", [cid]
    )
    # full list (export path via get_messages)
    assert [m["id"] for m in h.get_messages(cid)] == ids
    # paginated (UI path): stitch oldest-first pages with no gap/overlap, in insertion order
    p1 = h.get_messages_page(cid, limit=2)
    p2 = h.get_messages_page(cid, before=p1["next_cursor"], limit=2)
    p3 = h.get_messages_page(cid, before=p2["next_cursor"], limit=2)
    assert [m["id"] for m in p3["items"] + p2["items"] + p1["items"]] == ids
    assert p3["has_more"] is False and p3["next_cursor"] is None


def test_get_messages_page_rejects_malformed_cursor() -> None:
    # A stale/legacy 'created_at|<uuid>' cursor (rowid part not an int) must be rejected as a
    # ValueError, not blow up the keyset query with a DuckDB cast error.
    h = _hist()
    cid = h.create_conversation("c")
    _seed_messages(h, cid, 2)
    with pytest.raises(ValueError):
        h.get_messages_page(cid, before="2026-01-01 00:00:00|deadbeef-not-a-rowid")


def test_pagination_rejects_malformed_cursors_with_400(client: TestClient) -> None:
    # Every malformed cursor shape maps to 400, not a bare 500 — on BOTH paginated routes.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    cid = client.post("/api/conversations", json={"title": "c"}).json()["id"]
    # Malformed for both cursor kinds (no pipe / leading pipe / unparseable timestamp).
    for cur in ("nopipe", "|only-id", "not-a-timestamp|5"):
        assert client.get(f"/api/conversations/{cid}", params={"before": cur}).status_code == 400, cur
        assert client.get("/api/conversations", params={"before": cur}).status_code == 400, cur
    # A non-integer rowid is malformed for the MESSAGES cursor specifically (rowid must be int);
    # for the conversations cursor the id part is an opaque string, so it is not rejected there.
    assert client.get(f"/api/conversations/{cid}", params={"before": "2026-01-01 00:00:00|x"}).status_code == 400


def test_get_messages_page_clamps_to_max() -> None:
    from smartbrain_3000.history import _MAX_MSG_PAGE

    h = _hist()
    cid = h.create_conversation("c")
    _seed_messages(h, cid, 3)
    page = h.get_messages_page(cid, limit=_MAX_MSG_PAGE + 10_000)
    assert len(page["items"]) <= _MAX_MSG_PAGE


def test_http_conversations_pagination_via_cursor(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    ids: list[str] = []
    for i in range(5):  # bounded
        ids.append(client.post("/api/conversations", json={"title": f"c{i}"}).json()["id"])
    ids.reverse()  # newest-first
    first = client.get("/api/conversations", params={"limit": 2}).json()
    assert [c["id"] for c in first["conversations"]] == ids[:2]
    assert first["has_more"] is True
    second = client.get(
        "/api/conversations", params={"limit": 2, "before": first["next_cursor"]}
    ).json()
    assert [c["id"] for c in second["conversations"]] == ids[2:4]
    assert second["has_more"] is True
    third = client.get(
        "/api/conversations", params={"limit": 2, "before": second["next_cursor"]}
    ).json()
    assert [c["id"] for c in third["conversations"]] == ids[4:]
    assert third["has_more"] is False
    assert third["next_cursor"] is None


def test_http_conversations_no_params_returns_first_page_with_cursor_fields(
    client: TestClient,
) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/conversations", json={"title": "a"})
    body = client.get("/api/conversations").json()
    # Backward-compatible field is still present; new fields are added.
    assert "conversations" in body and isinstance(body["conversations"], list)
    assert "next_cursor" in body and "has_more" in body
    assert body["has_more"] is False


def test_http_messages_pagination_via_cursor(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    cid = client.post("/api/conversations", json={"title": "c"}).json()["id"]
    sent: list[str] = []
    for i in range(5):  # bounded
        client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": f"m{i}"})
        sent.append(f"m{i}")
    first = client.get(f"/api/conversations/{cid}", params={"limit": 2}).json()
    assert [m["content"] for m in first["messages"]] == sent[-2:]
    assert first["has_more"] is True
    second = client.get(
        f"/api/conversations/{cid}", params={"limit": 2, "before": first["next_cursor"]}
    ).json()
    assert [m["content"] for m in second["messages"]] == sent[-4:-2]
    third = client.get(
        f"/api/conversations/{cid}", params={"limit": 2, "before": second["next_cursor"]}
    ).json()
    assert [m["content"] for m in third["messages"]] == sent[:-4]
    assert third["has_more"] is False
    assert third["next_cursor"] is None
