"""Tests for passphrase change, data export, encrypted backup + restore."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import keyvault
from smartbrain_3000.history import ChatHistory
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.memory import MemoryStore
from smartbrain_3000.planner import Planner
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore

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
    # Desktop-local but locked -> 423 (the unlock check precedes the passphrase re-auth).
    assert client.post("/api/export", json={"passphrase": "x"}, headers=_LOCAL).status_code == 423


def test_export_contains_user_data(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Note", "content": "buy oat milk"})
    client.post("/api/tasks", json={"title": "call dentist", "notes": "", "due_date": None})
    client.post("/api/memories", json={"text": "likes tea"})
    data = client.post("/api/export", json={"passphrase": "correct-horse"}, headers=_LOCAL).json()
    assert data["schema"] == "smartbrain-export-v1"
    assert any(d["content"] == "buy oat milk" for d in data["knowledge"])
    assert any(t["title"] == "call dentist" for t in data["tasks"])
    assert "likes tea" in data["memories"]


# --- backup ---------------------------------------------------------------

def test_backup_requires_unlock(client: TestClient) -> None:
    assert client.post("/api/backup", json={"passphrase": "x"}, headers=_LOCAL).status_code == 423


def test_backup_returns_duckdb_file(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.post("/api/backup", json={"passphrase": "correct-horse"}, headers=_LOCAL)
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
    backup = client.post("/api/backup", json={"passphrase": "correct-horse"}, headers=_LOCAL).content
    r = client.post("/api/restore", content=backup, headers=_LOCAL)
    assert r.status_code == 200 and r.json()["ok"]


def test_restore_refused_from_bridge(client: TestClient) -> None:
    # B8: a bridge-origin restore (no X-SB-Local marker) must be refused — even
    # for a valid backup body — so a paired remote device cannot overwrite the vault.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    backup = client.post("/api/backup", json={"passphrase": "correct-horse"}, headers=_LOCAL).content
    r = client.post("/api/restore", content=backup)
    assert r.status_code == 403


def test_restore_streams_large_body_without_buffering(client: TestClient, monkeypatch) -> None:
    # B17: verify the restore endpoint does NOT call ``await request.body()``
    # (which buffers the whole upload into RAM). The endpoint must drain
    # ``request.stream()`` chunk-by-chunk instead.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    # Obtain the backup BEFORE patching request.body (the POST /api/backup re-auth
    # body itself is parsed via request.body()); the patch targets only the restore.
    backup = client.post("/api/backup", json={"passphrase": "correct-horse"}, headers=_LOCAL).content
    from starlette.requests import Request as StarletteRequest

    def _boom(self):  # noqa: ANN001 — instance method patch on Starlette's Request
        raise AssertionError("restore must stream the body, not buffer it")

    monkeypatch.setattr(StarletteRequest, "body", _boom)
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
    # old DB preserved (reversible) under a unique, timestamped name (never clobbered)
    assert list(tmp_path.glob("db.duckdb.pre-restore-*"))
    assert not dbmod.staged_restore_path(main).exists()  # staged file consumed


def test_apply_pending_restore_quarantines_garbage(tmp_path) -> None:
    main = tmp_path / "db.duckdb"
    _make_db(main)
    dbmod.staged_restore_path(main).write_bytes(b"garbage, not a duckdb file")
    assert dbmod.apply_pending_restore(main) is False
    assert list(tmp_path.glob("db.duckdb.restore.invalid-*"))  # quarantined, not applied
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
        r = client.post("/api/backup", json={"passphrase": "correct-horse"}, headers=_LOCAL)
        assert r.status_code == 200 and len(r.content) > 0


def test_backup_cleans_up_temp_file(tmp_path, monkeypatch) -> None:
    # The backup writes a temp DuckDB next to the live DB, then streams it via
    # FileResponse with a background unlink. After the response is consumed the
    # DB directory should hold ONLY the live DB (no stray sb_backup_*.duckdb).
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        r = client.post("/api/backup", json={"passphrase": "correct-horse"}, headers=_LOCAL)
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


# --- old-version -> newest UPGRADE proof (no Client data loss) -------------
#
# The two tests above re-run migrations from the LATEST schema. These instead
# fabricate a DB AT AN OLD migration version, seed it with the on-disk data
# shapes of that era, run the live migration chain to the newest release, and
# prove every user-data row survives and still decrypts. This is the executable
# guarantee behind "an upgrade must never lose Client data".

_UP_PASS = "upgrade-proof-pass-123"
_UP_DOC = "the lease ends 2027-03-01"
_UP_TASK = "renew passport"
_UP_MEMORY = "prefers oat milk"
_UP_SECRET_KEY = "provider:openai:api_key"
_UP_SECRET_VAL = "sk-must-survive-the-upgrade"


def _apply_through(conn, k: int) -> None:
    """Fabricate a DB AT old migration version ``k``: run the SQL of the first
    ``k`` migrations and record their ids — what an install last touched by
    release ``k`` looks like on disk, before the newer migrations exist."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp);"
    )
    for migration_id, sql in dbmod._MIGRATIONS[:k]:  # the first k migrations, in order
        conn.execute(sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?);", [migration_id])


def _table_exists(conn, name: str) -> bool:
    """True if a table named ``name`` exists in the DB."""
    return (
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?;", [name]
        ).fetchone()
        is not None
    )


def _seed_v12(conn, master_key: bytes) -> dict:
    """Seed every user table using the MIGRATION-12-ERA on-disk shapes: OLD per-doc
    embeddings (doc_id PK) and OLD tasks (no priority/due_time/recur), plus the
    stores whose schema is unchanged. Returns the seeded ids."""
    kb = KnowledgeBase(conn, master_key)
    doc_id = kb.add("Lease", _UP_DOC)
    # OLD per-doc embedding — derived search data that migration 13 drops.
    conn.execute(
        "INSERT INTO embeddings (doc_id, nonce, ciphertext, dim, model) VALUES (?, ?, ?, ?, ?);",
        [doc_id, b"\x00" * 12, b"old-derived-vector", 3, "old/model"],
    )
    # OLD task row: only the migration-8 columns exist yet, so seal the body and raw-insert.
    planner = Planner(conn, master_key)
    tid = "task-from-v12"
    nonce, ct = planner._seal(tid, {"title": _UP_TASK, "notes": "city hall", "tags": ["personal"]})
    conn.execute(
        "INSERT INTO tasks (id, nonce, ciphertext, status, due_date) VALUES (?, ?, ?, 'open', ?);",
        [tid, nonce, ct, "2026-02-01"],
    )
    # OLD conversation rows: no deleted_at column yet (migration 25 adds it), so seal
    # with the store but raw-insert the v12-era shapes (same pattern as the OLD task).
    hist = ChatHistory(conn, master_key)
    cid = str(uuid.uuid4())
    nonce, ct = hist._seal(b"conversation:", cid, {"title": "first chat"})
    conn.execute(
        "INSERT INTO conversations (id, nonce, ciphertext) VALUES (?, ?, ?);", [cid, nonce, ct]
    )
    for role, content in (("user", "hello"), ("assistant", "hi there")):
        mid = str(uuid.uuid4())
        nonce, ct = hist._seal(b"message:", mid, {"role": role, "content": content})
        conn.execute(
            "INSERT INTO messages (id, conversation_id, nonce, ciphertext) VALUES (?, ?, ?, ?);",
            [mid, cid, nonce, ct],
        )
    mem = MemoryStore(conn, master_key)
    mem.add_memory(_UP_MEMORY)
    mem.set_profile("Brainy", "Sam", "be concise")
    ScheduleStore(conn, master_key).add_schedule("morning brief", "summarize tasks", 1440, 60, None)
    SecretStore(conn, master_key).put(_UP_SECRET_KEY, _UP_SECRET_VAL)  # provider/Gmail creds live here
    # Append-only metadata tables: minimal raw rows just to prove their counts survive.
    conn.execute(
        "INSERT INTO audit_log (id, actor, tool_name, tier, decision, ok, nonce, ciphertext) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
        ["audit-1", "assistant", "kb_search", "observe", "auto", True, b"\x00" * 12, b"ct"],
    )
    conn.execute(
        "INSERT INTO pending_actions (id, tool_name, tier, status, nonce, ciphertext) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        ["pend-1", "send_email", "irreversible", "pending", b"\x00" * 12, b"ct"],
    )
    conn.execute(
        "INSERT INTO usage_log (id, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?);",
        ["usage-1", "openai/gpt-4o", 100, 50],
    )
    return {"doc_id": doc_id, "task_id": tid, "conversation_id": cid}


def test_upgrade_from_v12_preserves_all_client_data(tmp_path) -> None:
    # The riskiest boundary: v12 is BEFORE the destructive embeddings migration (13),
    # before the task-column additions (15-17), and before schedule_runs (18).
    conn = dbmod.open_db(tmp_path / "v12.duckdb")
    _apply_through(conn, 12)
    master_key = keyvault.set_passphrase(conn, _UP_PASS)
    ids = _seed_v12(conn, master_key)

    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
        for t in (
            "documents", "conversations", "messages", "memories", "profile",
            "tasks", "schedules", "audit_log", "pending_actions", "usage_log",
        )
    }

    applied = dbmod.run_migrations(conn)
    assert applied == 13  # exactly ids 13..25

    # Every user-data row count is unchanged across the upgrade.
    for t, n in counts.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0] == n, t

    # Encrypted content still decrypts (master key + AAD survived the migration chain).
    assert KnowledgeBase(conn, master_key).get(ids["doc_id"])["content"] == _UP_DOC
    assert SecretStore(conn, master_key).get(_UP_SECRET_KEY) == _UP_SECRET_VAL
    assert keyvault.unlock(conn, _UP_PASS) == master_key  # the passphrase still unlocks

    # The OLD task gained the new columns (defaulted) and its body still decrypts.
    task = Planner(conn, master_key).get_task(ids["task_id"])
    assert task is not None
    assert task["title"] == _UP_TASK
    assert task["priority"] == "medium" and task["due_time"] is None and task["recur"] == "none"

    # Derived per-doc embeddings were dropped (migration 13); the table is now the
    # chunked shape and empty — the documents remain so reindex can rebuild them.
    assert conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0] == 0
    ecols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'embeddings';"
        ).fetchall()
    }
    assert "chunk_idx" in ecols  # post-14 chunked schema

    # New table present; re-running the chain is a no-op (idempotent).
    assert _table_exists(conn, "schedule_runs")
    assert dbmod.run_migrations(conn) == 0
    conn.close()


def test_upgrade_from_v2_preserves_documents(tmp_path) -> None:
    # The oldest realistic install: only meta + documents existed (the whole chain runs).
    conn = dbmod.open_db(tmp_path / "v2.duckdb")
    _apply_through(conn, 2)
    master_key = keyvault.set_passphrase(conn, _UP_PASS)
    doc_id = KnowledgeBase(conn, master_key).add("Ancient", "note from the v2 era")

    assert dbmod.run_migrations(conn) == 23  # ids 3..25
    assert KnowledgeBase(conn, master_key).get(doc_id)["content"] == "note from the v2 era"
    assert conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0] == 0  # fresh, empty
    assert _table_exists(conn, "schedule_runs")
    assert dbmod.run_migrations(conn) == 0
    conn.close()


def test_upgrade_from_v17_preserves_embeddings_and_tasks(tmp_path) -> None:
    # An install already PAST the destructive migration: its chunked embeddings and
    # richer tasks must NOT be re-dropped by the final (additive) migration.
    conn = dbmod.open_db(tmp_path / "v17.duckdb")
    _apply_through(conn, 17)
    master_key = keyvault.set_passphrase(conn, _UP_PASS)
    kb = KnowledgeBase(conn, master_key)
    doc_id = kb.add("Recent", "note from the v17 era")
    kb.put_embeddings(doc_id, [[0.1, 0.2, 0.3]], "ollama/test")  # chunked vector
    planner = Planner(conn, master_key)
    tid = planner.add_task("file taxes", priority="high", due_time="09:00", recur="weekly")

    assert dbmod.run_migrations(conn) == 8  # ids 18..25
    # Chunked embedding preserved (18-21 only add schedule_runs, its seen column, and vaults).
    assert conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0] == 1
    t = planner.get_task(tid)
    assert t["title"] == "file taxes" and t["priority"] == "high"
    assert t["due_time"] == "09:00" and t["recur"] == "weekly"
    assert kb.get(doc_id)["content"] == "note from the v17 era"
    assert _table_exists(conn, "schedule_runs")
    assert dbmod.run_migrations(conn) == 0
    conn.close()


def test_migration_19_marks_preexisting_runs_seen(tmp_path) -> None:
    # Upgrade safety: run history recorded BEFORE the "seen" column existed must become seen=true
    # (the ALTER's DEFAULT), so the new Scheduled-updates badge doesn't light up with the user's
    # entire back-catalog the moment they upgrade.
    conn = dbmod.open_db(tmp_path / "pre19.duckdb")
    _apply_through(conn, 18)  # schedule_runs exists, but WITHOUT the seen column
    master_key = keyvault.set_passphrase(conn, _UP_PASS)
    store = ScheduleStore(conn, master_key)
    sid = store.add_schedule("old", "p", 0, 0, None)
    conn.execute(  # a run recorded the pre-19 way (no seen column in the INSERT)
        "INSERT INTO schedule_runs (id, schedule_id, status, nonce, ciphertext) VALUES (?, ?, ?, ?, ?);",
        ["run-old", sid, "complete", b"\x00" * 12, b"x"],
    )
    assert dbmod.run_migrations(conn) == 7  # 19 seen; 20-23 vaults; 24 doc_summaries; 25 chat trash
    assert store.unseen_count() == 0  # the back-catalog is seen -> badge stays quiet on upgrade
    conn.close()


def test_app_boots_and_serves_data_after_v12_upgrade(tmp_path, monkeypatch) -> None:
    # End-to-end: the real app boots on a freshly-upgraded v12 DB, unlocks with the
    # original passphrase, and serves the seeded data back through the HTTP API.
    path = tmp_path / "boot.duckdb"
    conn = dbmod.open_db(path)
    _apply_through(conn, 12)
    master_key = keyvault.set_passphrase(conn, _UP_PASS)
    _seed_v12(conn, master_key)
    assert dbmod.run_migrations(conn) == 13  # ids 13..25 (vaults + doc_summaries + chat trash)
    conn.close()  # release the file before the app opens it

    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(path))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as boot_client:
        assert boot_client.get("/api/health").json()["status"] == "ok"
        assert boot_client.post("/api/account/unlock", json={"passphrase": _UP_PASS}).status_code == 200
        data = boot_client.post("/api/export", json={"passphrase": _UP_PASS}, headers=_LOCAL).json()
        assert any(d["content"] == _UP_DOC for d in data["knowledge"])
        assert any(t["title"] == _UP_TASK for t in data["tasks"])
        assert _UP_MEMORY in data["memories"]
        assert _UP_SECRET_KEY in boot_client.get("/api/secrets").json()["keys"]
