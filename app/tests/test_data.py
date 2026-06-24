"""Tests for passphrase change, data export, encrypted backup + restore."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import keyvault

# B8: the Desktop-local marker the real UI sends; the WebRTC bridge filters it
# out, so a bridged-in request lacks it and is refused with 403.
_LOCAL = {"X-SB-Local": "1"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# --- passphrase change ----------------------------------------------------

def test_change_passphrase_wrong_current_rejected(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.post("/api/account/passphrase", json={"current_passphrase": "WRONG", "new_passphrase": "new-passphrase-1"})
    assert r.status_code == 401


def test_change_passphrase_rotates_unlock(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.post("/api/account/passphrase", json={"current_passphrase": "correct-horse", "new_passphrase": "new-passphrase-1"}).json()["ok"]
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"passphrase": "correct-horse"}).status_code == 401  # old no longer works
    assert client.post("/api/account/unlock", json={"passphrase": "new-passphrase-1"}).json()["unlocked"]  # new works


def test_change_passphrase_keeps_recovery_key(client: TestClient) -> None:
    kit = client.post("/api/account/setup", json={"passphrase": "correct-horse"}).json()
    client.post("/api/account/passphrase", json={"current_passphrase": "correct-horse", "new_passphrase": "new-passphrase-1"})
    client.post("/api/account/lock")
    # the master key is unchanged, so the original Recovery Key still unlocks
    assert client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]}).json()["unlocked"]


def test_change_passphrase_requires_unlock(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/account/lock")
    r = client.post("/api/account/passphrase", json={"current_passphrase": "correct-horse", "new_passphrase": "new-passphrase-1"})
    assert r.status_code == 423


def test_keyvault_reset_passphrase_rewraps(tmp_path) -> None:
    conn = dbmod.open_db(tmp_path / "k.duckdb")
    dbmod.run_migrations(conn)
    master_key = keyvault.set_passphrase(conn, "old-pass-123")
    keyvault.reset_passphrase(conn, master_key, "new-pass-456")  # no current passphrase
    assert keyvault.unlock(conn, "new-pass-456") == master_key
    with pytest.raises(Exception):  # old passphrase no longer works
        keyvault.unlock(conn, "old-pass-123")


def test_reset_passphrase_after_recovery_unlock(client: TestClient) -> None:
    # The forgot-passphrase path: unlock with the Recovery Key, then set a new
    # passphrase WITHOUT the old one (the unlocked session is the authority).
    kit = client.post("/api/account/setup", json={"passphrase": "correct-horse"}).json()
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]}).json()["unlocked"]
    assert client.post("/api/account/passphrase/reset", json={"new_passphrase": "brand-new-pass"}, headers=_LOCAL).json()["ok"]
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"passphrase": "correct-horse"}).status_code == 401
    assert client.post("/api/account/unlock", json={"passphrase": "brand-new-pass"}).json()["unlocked"]


def test_reset_passphrase_requires_unlock(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/account/lock")
    assert client.post("/api/account/passphrase/reset", json={"new_passphrase": "brand-new-pass"}, headers=_LOCAL).status_code == 423


def test_reset_passphrase_refused_from_bridge(client: TestClient) -> None:
    # B8: a bridge-origin request (no X-SB-Local marker — the bridge strips it)
    # must be refused even though the session is unlocked.
    kit = client.post("/api/account/setup", json={"passphrase": "correct-horse"}).json()
    client.post("/api/account/lock")
    client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]})
    r = client.post("/api/account/passphrase/reset", json={"new_passphrase": "brand-new-pass"})
    assert r.status_code == 403


# --- export ---------------------------------------------------------------

def test_export_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/export").status_code == 423


def test_export_contains_user_data(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Note", "content": "buy oat milk"})
    client.post("/api/tasks", json={"title": "call dentist", "notes": "", "due_date": None})
    client.post("/api/memories", json={"text": "likes tea"})
    data = client.get("/api/export").json()
    assert data["schema"] == "smartbrain-export-v1"
    assert any(d["content"] == "buy oat milk" for d in data["knowledge"])
    assert any(t["title"] == "call dentist" for t in data["tasks"])
    assert "likes tea" in data["memories"]


# --- backup ---------------------------------------------------------------

def test_backup_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/backup").status_code == 423


def test_backup_returns_duckdb_file(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.get("/api/backup")
    assert r.status_code == 200
    assert r.headers["content-disposition"].endswith('smartbrain-backup.duckdb"')
    assert len(r.content) > 0


# --- restore --------------------------------------------------------------

def test_restore_rejects_non_backup(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.post("/api/restore", content=b"not a duckdb file", headers=_LOCAL).status_code == 400


def test_restore_blocked_when_locked(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/account/lock")
    assert client.post("/api/restore", content=b"anything", headers=_LOCAL).status_code == 423


def test_restore_stages_a_valid_backup(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    backup = client.get("/api/backup").content
    r = client.post("/api/restore", content=backup, headers=_LOCAL)
    assert r.status_code == 200 and r.json()["ok"]


def test_restore_refused_from_bridge(client: TestClient) -> None:
    # B8: a bridge-origin restore (no X-SB-Local marker) must be refused — even
    # for a valid backup body — so a paired remote device cannot overwrite the vault.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    backup = client.get("/api/backup").content
    r = client.post("/api/restore", content=backup)
    assert r.status_code == 403


def test_restore_streams_large_body_without_buffering(client: TestClient, monkeypatch) -> None:
    # B17: verify the restore endpoint does NOT call ``await request.body()``
    # (which buffers the whole upload into RAM). The endpoint must drain
    # ``request.stream()`` chunk-by-chunk instead.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    from starlette.requests import Request as StarletteRequest

    def _boom(self):  # noqa: ANN001 — instance method patch on Starlette's Request
        raise AssertionError("restore must stream the body, not buffer it")

    monkeypatch.setattr(StarletteRequest, "body", _boom)
    backup = client.get("/api/backup").content
    r = client.post("/api/restore", content=backup, headers=_LOCAL)
    assert r.status_code == 200 and r.json()["ok"]


# --- boot-time restore application ----------------------------------------

def _make_db(path) -> None:
    conn = dbmod.open_db(path)
    dbmod.run_migrations(conn)
    keyvault.set_passphrase(conn, "pw-for-test")  # creates key_wraps -> a real SmartBrain DB
    conn.close()


def test_apply_pending_restore_swaps_valid(tmp_path) -> None:
    main = tmp_path / "db.duckdb"
    _make_db(main)
    other = tmp_path / "other.duckdb"
    _make_db(other)
    dbmod.staged_restore_path(main).write_bytes(other.read_bytes())
    assert dbmod.apply_pending_restore(main) is True
    assert (tmp_path / "db.duckdb.pre-restore").exists()  # old DB preserved (reversible)
    assert not dbmod.staged_restore_path(main).exists()  # staged file consumed


def test_apply_pending_restore_quarantines_garbage(tmp_path) -> None:
    main = tmp_path / "db.duckdb"
    _make_db(main)
    dbmod.staged_restore_path(main).write_bytes(b"garbage, not a duckdb file")
    assert dbmod.apply_pending_restore(main) is False
    assert (tmp_path / "db.duckdb.restore.invalid").exists()  # quarantined, not applied
    assert dbmod.is_smartbrain_db(main)  # original untouched + still valid


def test_apply_pending_restore_noop_when_none(tmp_path) -> None:
    main = tmp_path / "db.duckdb"
    _make_db(main)
    assert dbmod.apply_pending_restore(main) is False


# --- review fixes ---------------------------------------------------------

def test_unlock_rejects_out_of_range_kdf_params(tmp_path) -> None:
    # A restored/corrupt key_wraps row can pin huge Argon2 params; unlock must
    # reject them (cleanly) rather than feed them to the KDF and OOM the worker.
    conn = dbmod.open_db(tmp_path / "k.duckdb")
    dbmod.run_migrations(conn)
    keyvault.set_passphrase(conn, "pw-123456")
    conn.execute("UPDATE key_wraps SET memory_cost = 99999999 WHERE method = 'passphrase';")
    with pytest.raises(ValueError):
        keyvault.unlock(conn, "pw-123456")


def test_backup_works_with_hyphenated_db_path(tmp_path, monkeypatch) -> None:
    # current_database() is the filename stem; a hyphen must not break the COPY.
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "smartbrain-prod.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        r = client.get("/api/backup")
        assert r.status_code == 200 and len(r.content) > 0


def test_backup_cleans_up_temp_file(tmp_path, monkeypatch) -> None:
    # The backup writes a temp DuckDB next to the live DB, then streams it via
    # FileResponse with a background unlink. After the response is consumed the
    # DB directory should hold ONLY the live DB (no stray sb_backup_*.duckdb).
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        r = client.get("/api/backup")
        assert r.status_code == 200 and len(r.content) > 0
    # TestClient runs the BackgroundTask synchronously after the response is read.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("sb_backup_")]
    assert leftovers == [], f"backup temp files leaked: {leftovers}"


# --- migrations on a populated DB -----------------------------------------

def test_migrations_idempotent_on_populated_db(tmp_path) -> None:
    # After migrations + real user data, re-running run_migrations() must be a
    # no-op (returns 0, every row survives). Guards against an accidental
    # destructive migration being added to the tail of the list.
    from smartbrain_3000.kb import KnowledgeBase
    from smartbrain_3000.planner import Planner
    from smartbrain_3000.history import ChatHistory
    from smartbrain_3000.secrets import gen_master_key

    conn = dbmod.open_db(tmp_path / "live.duckdb")
    assert dbmod.run_migrations(conn) > 0  # first pass migrates everything

    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    doc_id = kb.add("Note", "buy oat milk")
    kb.put_embeddings(doc_id, [[0.1, 0.2, 0.3]], "ollama/test")  # post-14 chunked shape

    Planner(conn, key).add_task("call dentist")
    ChatHistory(conn, key).create_conversation("hello")

    doc_count = conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
    embed_count = conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0]
    task_count = conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0]
    convo_count = conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0]
    assert doc_count == 1 and embed_count == 1 and task_count == 1 and convo_count == 1

    # Second migration pass: idempotent — nothing to do, nothing to lose.
    applied = dbmod.run_migrations(conn)
    assert applied == 0

    # Row counts unchanged.
    assert conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0] == doc_count
    assert conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0] == embed_count
    assert conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == task_count
    assert conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0] == convo_count

    # The decrypted document still reads back (master key + AAD intact).
    got = kb.get(doc_id)
    assert got is not None and got["content"] == "buy oat milk"


def test_embeddings_table_is_chunked_shape(tmp_path) -> None:
    # Migration 13/14 drop the per-doc table and re-create it keyed by
    # (doc_id, chunk_idx). A populated DB after migrations must expose the
    # chunked schema (chunk_idx column present, composite PK).
    conn = dbmod.open_db(tmp_path / "schema.duckdb")
    dbmod.run_migrations(conn)
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'embeddings';"
        ).fetchall()
    }
    assert "doc_id" in cols and "chunk_idx" in cols  # post-14 chunked schema
    assert "dim" in cols and "model" in cols and "nonce" in cols and "ciphertext" in cols
