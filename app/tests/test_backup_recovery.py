"""Backup & recovery data-safety proofs (pre-public gate).

The existing backup/restore/recovery tests run on an EMPTY vault and only check
guards (423/403/quarantine) or compare raw key bytes — none of them produces a
real backup of a POPULATED vault, restores it, and reads the original Client
data back DECRYPTED. These tests close that gap: they prove that real user data
survives every backup/restore/recovery path, including restoring an OLD-schema
backup into the NEW code (the restore x upgrade intersection).

Restore is boot-applied (POST /api/restore stages ``<db>.restore``; the next
process startup swaps it in via apply_pending_restore, THEN runs migrations), so
the round-trip tests drive THREE app lifecycles on real on-disk DBs:
seed+backup -> stage-restore -> reboot+unlock+read-back.
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import keyvault
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.memory import MemoryStore
from smartbrain_3000.planner import Planner

_LOCAL = {"X-SB-Local": "1"}  # Desktop-local marker required by /api/restore

_PASS = "backup-recovery-pass-123"
# Distinctive sentinels so a confidentiality check can scan raw bytes for leaks.
_DOC = "SENTINEL-LEASE-BODY-7f3a the lease ends 2027-03-01"
_TASK = "SENTINEL-TASK renew passport"
_MEM = "SENTINEL-MEMORY prefers oat milk"
_SECRET_KEY = "provider:openai:api_key"
_SECRET_VAL = "sk-SENTINEL-must-survive"

# User tables whose row counts must be identical across a backup->restore (excludes
# meta, which the per-boot record_boot mutates, and always-empty audit/usage here).
_TABLES = (
    "documents", "conversations", "messages", "memories", "profile",
    "tasks", "schedules", "secrets", "key_wraps",
)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "single.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# --- shared helpers -------------------------------------------------------

def _seed_via_app(c: TestClient) -> None:
    """Seed every user table through the running app (HTTP + the unlocked stores)."""
    c.post("/api/kb", json={"title": "Lease", "content": _DOC})
    c.post("/api/tasks", json={"title": _TASK, "notes": "city hall", "due_date": None})
    c.post("/api/memories", json={"text": _MEM})
    st = c.app.state
    cid = st.history.create_conversation("first chat")
    st.history.add_message(cid, "user", "hello")
    st.history.add_message(cid, "assistant", "hi there")
    st.schedules.add_schedule("morning brief", "summarize tasks", 1440, 60, None)
    st.secret_store.put(_SECRET_KEY, _SECRET_VAL)
    st.memory.set_profile("Brainy", "Sam", "be concise")


def _export(c: TestClient, cred: dict | None = None) -> dict:
    """POST /api/export with the Desktop-local marker + a re-auth credential."""
    r = c.post("/api/export", json=(cred or {"passphrase": _PASS}), headers=_LOCAL)
    assert r.status_code == 200, r.text
    return r.json()


def _assert_seeded_data_present(c: TestClient, cred: dict | None = None) -> None:
    """Assert an unlocked app serves back every seeded value, decrypted."""
    data = _export(c, cred)
    assert any(d["content"] == _DOC for d in data["knowledge"]), "document lost"
    assert any(t["title"] == _TASK for t in data["tasks"]), "task lost"
    assert _MEM in data["memories"], "memory lost"
    assert any(
        any(m["content"] == "hello" for m in conv["messages"]) for conv in data["conversations"]
    ), "chat message lost"
    assert data["profile"]["assistant_name"] == "Brainy", "profile lost"
    assert _SECRET_KEY in c.get("/api/secrets").json()["keys"], "secret key lost"
    assert c.app.state.secret_store.get(_SECRET_KEY) == _SECRET_VAL, "secret value lost/garbled"


def _make_seeded_backup(path, monkeypatch) -> tuple[bytes, dict, dict]:
    """Build a populated vault at ``path``; return (backup_bytes, setup_kit, src_counts)."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(path))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as ca:
        kit = ca.post("/api/account/setup", json={"passphrase": _PASS}).json()
        _seed_via_app(ca)
        backup = ca.post("/api/backup", json={"passphrase": _PASS}, headers=_LOCAL)
        assert backup.status_code == 200 and backup.content
        src_counts = {
            t: ca.app.state.db.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0] for t in _TABLES
        }
    return backup.content, kit, src_counts


def _stage_restore(path, monkeypatch, backup_bytes: bytes) -> None:
    """Boot a FRESH vault at ``path`` and stage ``backup_bytes`` for next-boot apply."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(path))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as cb:
        r = cb.post("/api/restore", content=backup_bytes, headers=_LOCAL)
        assert r.status_code == 200 and r.json()["ok"], r.text
    assert dbmod.staged_restore_path(path).exists()  # staged, awaiting next boot


def _boot(path, monkeypatch) -> TestClient:
    """Return a TestClient on ``path`` (its lifespan applies any staged restore)."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(path))
    from smartbrain_3000.main import create_app

    return TestClient(create_app())


def _make_real_db(path, passphrase: str, doc_body: str) -> None:
    """Create a valid, populated SmartBrain DB file at ``path`` (closed)."""
    conn = dbmod.open_db(path)
    dbmod.run_migrations(conn)
    mk = keyvault.set_passphrase(conn, passphrase)
    KnowledgeBase(conn, mk).add("Doc", doc_body)
    conn.close()


def _first_doc_body(conn, master_key: bytes) -> str:
    kb = KnowledgeBase(conn, master_key)
    docs = kb.list_docs()
    assert docs, "expected at least one document"
    return kb.get(docs[0]["id"])["content"]


# --- P0: full backup -> restore round-trips -------------------------------

def test_backup_restore_roundtrip_preserves_all_client_data(tmp_path, monkeypatch) -> None:
    # Seed every table on vault A, /api/backup, restore onto a FRESH B (cross-machine
    # / fresh-install path), reboot to apply, unlock with A's passphrase, read it all back.
    a, b = tmp_path / "A.duckdb", tmp_path / "B.duckdb"
    backup, _kit, src = _make_seeded_backup(a, monkeypatch)
    _stage_restore(b, monkeypatch, backup)
    with _boot(b, monkeypatch) as cb2:
        assert cb2.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200
        _assert_seeded_data_present(cb2)
        for t in _TABLES:  # no table silently dropped/added by COPY + swap + migrate
            assert cb2.app.state.db.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0] == src[t], t
    assert not dbmod.staged_restore_path(b).exists()  # staged file consumed by the boot


def test_restore_unlock_with_recovery_key_only(tmp_path, monkeypatch) -> None:
    # The "moved to a new machine, forgot the passphrase" path: restore the backup
    # onto a fresh install and unlock with ONLY the Recovery Key from the Emergency Kit.
    a, b = tmp_path / "A.duckdb", tmp_path / "B.duckdb"
    backup, kit, _src = _make_seeded_backup(a, monkeypatch)
    _stage_restore(b, monkeypatch, backup)
    with _boot(b, monkeypatch) as cb2:
        r = cb2.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]})
        assert r.status_code == 200 and r.json()["unlocked"]
        _assert_seeded_data_present(cb2)
        # The passphrase wrap also travelled and still unlocks the moved vault.
        cb2.post("/api/account/lock")
        assert cb2.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200


def test_restore_old_v12_backup_into_new_code(tmp_path, monkeypatch) -> None:
    # Restore a backup taken on an OLD schema (v12) into the CURRENT code: the boot
    # swaps it in BEFORE run_migrations, so it must be upgraded then read back.
    b, old = tmp_path / "B.duckdb", tmp_path / "old.duckdb"
    # Fabricate a real v12-schema vault (old per-doc embeddings era, pre-task-columns).
    conn = dbmod.open_db(old)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp);"
    )
    for mid, sql in dbmod._MIGRATIONS[:12]:
        conn.execute(sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?);", [mid])
    mk = keyvault.set_passphrase(conn, _PASS)
    KnowledgeBase(conn, mk).add("Lease", _DOC)
    planner = Planner(conn, mk)
    nonce, ct = planner._seal("oldtask", {"title": _TASK, "notes": "", "tags": []})  # OLD task shape
    conn.execute(
        "INSERT INTO tasks (id, nonce, ciphertext, status, due_date) VALUES (?, ?, ?, 'open', ?);",
        ["oldtask", nonce, ct, None],
    )
    MemoryStore(conn, mk).add_memory(_MEM)
    conn.close()

    with _boot(b, monkeypatch):  # create + migrate a fresh empty B
        pass
    dbmod.staged_restore_path(b).write_bytes(old.read_bytes())  # stage the v12 backup
    with _boot(b, monkeypatch) as cb2:  # boot swaps v12 in, THEN migrates it forward
        assert cb2.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200
        data = _export(cb2)
        assert any(d["content"] == _DOC for d in data["knowledge"])
        assert any(t["title"] == _TASK for t in data["tasks"])
        assert _MEM in data["memories"]
        # The restored old DB was actually upgraded to the newest schema on the way in.
        max_id = cb2.app.state.db.execute("SELECT MAX(id) FROM schema_migrations;").fetchone()[0]
        assert max_id == dbmod._MIGRATIONS[-1][0]
        # The old task gained the new defaulted columns and still decrypts.
        task = cb2.app.state.planner.get_task("oldtask")
        assert task["title"] == _TASK and task["priority"] == "medium" and task["recur"] == "none"


# --- P0: recovery-key + passphrase re-wrap read real data -----------------

def test_recovery_key_unlock_reads_all_client_data(client: TestClient) -> None:
    kit = client.post("/api/account/setup", json={"passphrase": _PASS}).json()
    _seed_via_app(client)
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]}).json()["unlocked"]
    _assert_seeded_data_present(client)


def test_recovery_key_survives_passphrase_reset(client: TestClient) -> None:
    # Forgot-passphrase flow must NOT invalidate the Recovery Key, and data must survive.
    kit = client.post("/api/account/setup", json={"passphrase": _PASS}).json()
    _seed_via_app(client)
    client.post("/api/account/lock")
    client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]})
    assert client.post(
        "/api/account/passphrase/reset", json={"new_passphrase": "brand-new-pass-xyz"}, headers=_LOCAL
    ).json()["ok"]
    client.post("/api/account/lock")
    # The ORIGINAL Recovery Key still unlocks the re-wrapped vault, and data is intact.
    assert client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]}).json()["unlocked"]
    assert any(  # re-auth export with the Recovery Key (the old passphrase no longer exists)
        d["content"] == _DOC for d in _export(client, {"recovery_key": kit["recovery_key"]})["knowledge"]
    )
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"passphrase": "brand-new-pass-xyz"}).json()["unlocked"]
    _assert_seeded_data_present(client, {"passphrase": "brand-new-pass-xyz"})


def test_data_survives_passphrase_change_and_reset(client: TestClient) -> None:
    # A re-wrap that stored wrong key material would pass the 32-byte length assert but
    # fail to decrypt pre-existing data — so read the data back after each rotation.
    kit = client.post("/api/account/setup", json={"passphrase": _PASS}).json()
    _seed_via_app(client)
    assert client.post(
        "/api/account/passphrase", json={"current_passphrase": _PASS, "new_passphrase": "changed-pass-1"}
    ).json()["ok"]
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"passphrase": "changed-pass-1"}).json()["unlocked"]
    assert any(d["content"] == _DOC for d in _export(client, {"passphrase": "changed-pass-1"})["knowledge"])
    client.post("/api/account/lock")
    client.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]})
    assert client.post(
        "/api/account/passphrase/reset", json={"new_passphrase": "reset-pass-2"}, headers=_LOCAL
    ).json()["ok"]
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"passphrase": "reset-pass-2"}).json()["unlocked"]
    assert any(d["content"] == _DOC for d in _export(client, {"passphrase": "reset-pass-2"})["knowledge"])


def test_corrupt_passphrase_wrap_does_not_strand_recovery(tmp_path) -> None:
    # A corrupt/planted passphrase KDF row must fail cleanly (no OOM) WITHOUT locking a
    # user out of a vault that still has a valid Recovery Key — their data stays reachable.
    conn = dbmod.open_db(tmp_path / "v.duckdb")
    dbmod.run_migrations(conn)
    mk = keyvault.set_passphrase(conn, _PASS)
    recovery = keyvault.add_recovery_key(conn, mk)
    doc_id = KnowledgeBase(conn, mk).add("Doc", _DOC)
    conn.execute("UPDATE key_wraps SET memory_cost = 99999999 WHERE method = 'passphrase';")

    with pytest.raises(ValueError):  # rejected cleanly, not a MemoryError/hang
        keyvault.unlock(conn, _PASS)
    mk2 = keyvault.unlock_with_recovery(conn, recovery)
    assert mk2 == mk
    assert KnowledgeBase(conn, mk2).get(doc_id)["content"] == _DOC  # data still reachable


def test_both_unlock_paths_decrypt_rich_stores_after_restart(tmp_path) -> None:
    # Persist via passphrase+recovery, reopen the FILE (simulated restart), and prove
    # BOTH unlock methods decrypt the rich stores (not just compare raw key bytes).
    path = tmp_path / "v.duckdb"
    conn = dbmod.open_db(path)
    dbmod.run_migrations(conn)
    mk = keyvault.set_passphrase(conn, _PASS)
    recovery = keyvault.add_recovery_key(conn, mk)
    doc_id = KnowledgeBase(conn, mk).add("Doc", _DOC)
    MemoryStore(conn, mk).add_memory(_MEM)
    tid = Planner(conn, mk).add_task(_TASK)
    conn.close()

    c2 = dbmod.open_db(path)
    dbmod.run_migrations(c2)
    mk_r = keyvault.unlock_with_recovery(c2, recovery)
    assert mk_r == mk
    assert KnowledgeBase(c2, mk_r).get(doc_id)["content"] == _DOC
    assert any(m["text"] == _MEM for m in MemoryStore(c2, mk_r).list_memories())
    assert Planner(c2, mk_r).get_task(tid)["title"] == _TASK
    c2.close()

    c3 = dbmod.open_db(path)
    dbmod.run_migrations(c3)
    mk_p = keyvault.unlock(c3, _PASS)
    assert mk_p == mk and KnowledgeBase(c3, mk_p).get(doc_id)["content"] == _DOC
    c3.close()


# --- P0/P1: confidentiality, crash-safety, reversibility, corruption ------

def test_backup_file_leaks_no_plaintext_client_data(client: TestClient, tmp_path) -> None:
    client.post("/api/account/setup", json={"passphrase": _PASS})
    client.post("/api/kb", json={"title": "Lease", "content": _DOC})
    client.post("/api/memories", json={"text": _MEM})
    client.app.state.secret_store.put(_SECRET_KEY, _SECRET_VAL)
    backup = client.post("/api/backup", json={"passphrase": _PASS}, headers=_LOCAL).content
    f = tmp_path / "leak.duckdb"
    f.write_bytes(backup)

    raw = f.read_bytes()
    assert b"SENTINEL-LEASE-BODY-7f3a" not in raw, "document body leaked in backup at rest"
    assert _SECRET_VAL.encode() not in raw, "secret value leaked in backup at rest"
    assert b"SENTINEL-MEMORY" not in raw, "memory text leaked in backup at rest"

    probe = duckdb.connect(str(f), read_only=True)
    try:
        assert probe.execute(
            "SELECT COUNT(*) FROM key_wraps WHERE method = 'passphrase';"
        ).fetchone()[0] == 1  # self-contained: restorable with the same passphrase
        rows = probe.execute("SELECT nonce, ciphertext FROM secrets;").fetchall()
        assert rows and all(_SECRET_VAL.encode() not in bytes(r[1]) for r in rows)
    finally:
        probe.close()


def test_apply_pending_restore_self_heals_after_crash_mid_swap(tmp_path) -> None:
    # Simulate a crash AFTER the live DB was moved to *.pre-restore but BEFORE the staged
    # file was swapped in. The next boot must complete the swap to a bootable, unlockable,
    # NON-empty DB and must NOT clobber the preserved original.
    main = tmp_path / "db.duckdb"
    _make_real_db(main, "pass-orig", "ORIGINAL-DATA")
    staged = dbmod.staged_restore_path(main)
    _make_real_db(staged, "pass-new", "NEW-DATA")

    pre = main.parent / (main.name + ".pre-restore")
    main.replace(pre)  # crash point: step 1 done (live -> .pre-restore), step 3 not yet
    assert not main.exists() and pre.exists() and staged.exists()

    assert dbmod.apply_pending_restore(main) is True  # next boot completes the swap
    assert main.exists() and dbmod.is_smartbrain_db(main) and not staged.exists()

    conn = dbmod.open_db(main)
    dbmod.run_migrations(conn)
    assert _first_doc_body(conn, keyvault.unlock(conn, "pass-new")) == "NEW-DATA"  # live = restore
    conn.close()
    # The displaced original was preserved and is still recoverable from *.pre-restore.
    pconn = dbmod.open_db(pre)
    dbmod.run_migrations(pconn)
    assert _first_doc_body(pconn, keyvault.unlock(pconn, "pass-orig")) == "ORIGINAL-DATA"
    pconn.close()


def test_pre_restore_holds_recoverable_original(tmp_path) -> None:
    # After a restore swap, the displaced original at *.pre-restore must be genuinely
    # recoverable (real decryptable data), not just a file that exists.
    main = tmp_path / "db.duckdb"
    _make_real_db(main, "pass-orig", "ORIGINAL-DATA")
    staged = dbmod.staged_restore_path(main)
    _make_real_db(staged, "pass-new", "NEW-DATA")

    assert dbmod.apply_pending_restore(main) is True
    pre_copies = list(main.parent.glob(main.name + ".pre-restore-*"))  # unique timestamped name
    assert len(pre_copies) == 1
    main.unlink()
    pre_copies[0].replace(main)  # operator rolls back to the pre-restore copy

    conn = dbmod.open_db(main)
    dbmod.run_migrations(conn)
    assert _first_doc_body(conn, keyvault.unlock(conn, "pass-orig")) == "ORIGINAL-DATA"
    conn.close()


def test_truncated_backup_rejected_live_db_intact(client: TestClient) -> None:
    # A torn upload (DuckDB header may survive, catalog does not) must be rejected and
    # must never displace the live vault.
    client.post("/api/account/setup", json={"passphrase": _PASS})
    client.post("/api/kb", json={"title": "Lease", "content": _DOC})
    backup = client.post("/api/backup", json={"passphrase": _PASS}, headers=_LOCAL).content
    r = client.post("/api/restore", content=backup[:512], headers=_LOCAL)
    assert r.status_code == 400  # not a valid SmartBrain backup
    # Live vault still serves its data; nothing was staged.
    assert any(d["content"] == _DOC for d in _export(client)["knowledge"])


# --- forward-compat guard: never open/restore a NEWER-schema database ------

def _make_future_schema_db(path) -> None:
    """A valid SmartBrain DB that records a migration id beyond this build's newest."""
    conn = dbmod.open_db(path)
    dbmod.run_migrations(conn)
    keyvault.set_passphrase(conn, _PASS)  # key_wraps -> a real SmartBrain DB
    conn.execute("INSERT INTO schema_migrations (id) VALUES (?);", [dbmod.NEWEST_MIGRATION + 100])
    conn.close()


def test_future_schema_db_refused_at_boot(tmp_path) -> None:
    # An upgrade must never break a working app: if a DB was written by a NEWER
    # version, this (older) code must REFUSE to open it rather than corrupt it.
    path = tmp_path / "future.duckdb"
    _make_future_schema_db(path)
    conn = dbmod.open_db(path)
    with pytest.raises(RuntimeError, match="newer than this app"):
        dbmod.run_migrations(conn)
    conn.close()


def test_restore_rejects_future_schema_backup(client: TestClient, tmp_path) -> None:
    # Restoring a backup from a newer app version is refused at upload (clear 400),
    # so it is never staged and the live vault is untouched.
    client.post("/api/account/setup", json={"passphrase": _PASS})
    client.post("/api/kb", json={"title": "Lease", "content": _DOC})
    future = tmp_path / "future.duckdb"
    _make_future_schema_db(future)
    r = client.post("/api/restore", content=future.read_bytes(), headers=_LOCAL)
    assert r.status_code == 400 and "newer version" in r.json()["detail"]
    assert not dbmod.staged_restore_path(dbmod.resolve_db_path()).exists()  # never staged
    assert any(d["content"] == _DOC for d in _export(client)["knowledge"])  # live intact


# --- egress hardening: bridge guard + passphrase re-auth on backup/export ---

def test_backup_and_export_refused_from_bridge(client: TestClient) -> None:
    # A bridged-in paired remote device (no X-SB-Local marker) must never be able to
    # pull the whole vault file or the decrypted plaintext, even with a valid passphrase.
    client.post("/api/account/setup", json={"passphrase": _PASS})
    assert client.post("/api/backup", json={"passphrase": _PASS}).status_code == 403
    assert client.post("/api/export", json={"passphrase": _PASS}).status_code == 403


def test_backup_and_export_require_correct_passphrase(client: TestClient) -> None:
    # Desktop-local + unlocked is not enough: the passphrase must be re-entered.
    client.post("/api/account/setup", json={"passphrase": _PASS})
    assert client.post("/api/backup", json={"passphrase": "wrong-pass"}, headers=_LOCAL).status_code == 401
    assert client.post("/api/export", json={"passphrase": "wrong-pass"}, headers=_LOCAL).status_code == 401
    assert client.post("/api/export", json={}, headers=_LOCAL).status_code == 400  # passphrase required
    # The correct passphrase still works.
    assert client.post("/api/backup", json={"passphrase": _PASS}, headers=_LOCAL).status_code == 200


def test_backup_and_export_accept_recovery_key(client: TestClient) -> None:
    # A user who unlocked via the Recovery Key (forgot passphrase) can still re-auth egress.
    kit = client.post("/api/account/setup", json={"passphrase": _PASS}).json()
    assert client.post(
        "/api/backup", json={"recovery_key": kit["recovery_key"]}, headers=_LOCAL
    ).status_code == 200
    assert client.post(
        "/api/export", json={"recovery_key": kit["recovery_key"]}, headers=_LOCAL
    ).status_code == 200
