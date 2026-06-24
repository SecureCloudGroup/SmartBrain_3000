"""Tests for the append-only encrypted audit log (audit.py).

Covers the AES-GCM at-rest body, the plaintext metadata columns that drive the
Activity view, and the newest-first ``list`` ordering. Mirrors the
``test_embeddings.py`` / ``test_kb.py`` pattern: a duckdb ``:memory:``
connection + ``run_migrations`` + a freshly-generated master key.
"""

from __future__ import annotations

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.secrets import gen_master_key


def _audit() -> tuple[AuditLog, duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)  # creates audit_log
    return AuditLog(conn, gen_master_key()), conn


def test_append_returns_id_and_persists_row() -> None:
    log, conn = _audit()
    aid = log.append(
        "assistant", "kb_search", "OBSERVE", "auto", True,
        args_summary="query: tea", result_summary="2 hits",
    )
    assert isinstance(aid, str) and len(aid) == 36  # uuid4 shape
    row = conn.execute("SELECT id FROM audit_log WHERE id = ?;", [aid]).fetchone()
    assert row is not None


def test_list_roundtrip_recovers_body() -> None:
    log, _ = _audit()
    log.append(
        "user", "remember_fact", "REVIEWED", "approved", True,
        args_summary="text: I like tea", result_summary="ok",
    )
    entries = log.list()
    assert len(entries) == 1
    only = entries[0]
    assert only["args_summary"] == "text: I like tea"
    assert only["result_summary"] == "ok"


def test_metadata_columns_are_plaintext() -> None:
    # The Activity view filters/sorts by these columns without decrypting; they
    # MUST be stored in the clear so SQL can read them.
    log, conn = _audit()
    log.append("assistant", "kb_search", "OBSERVE", "auto", True, args_summary="x")
    row = conn.execute(
        "SELECT actor, tool_name, tier, decision, ok FROM audit_log;"
    ).fetchone()
    assert row is not None
    assert row[0] == "assistant" and row[1] == "kb_search"
    assert row[2] == "OBSERVE" and row[3] == "auto" and bool(row[4]) is True


def test_body_encrypted_at_rest() -> None:
    # args/result/error live INSIDE the AES-GCM ciphertext — they must not be
    # readable from the raw DB row.
    log, conn = _audit()
    log.append(
        "user", "remember_fact", "REVIEWED", "approved", True,
        args_summary="SUPER-SECRET-ARGS",
        result_summary="SUPER-SECRET-RESULT",
        error="SUPER-SECRET-ERROR",
    )
    raw = bytes(conn.execute("SELECT ciphertext FROM audit_log;").fetchone()[0])
    assert b"SUPER-SECRET-ARGS" not in raw
    assert b"SUPER-SECRET-RESULT" not in raw
    assert b"SUPER-SECRET-ERROR" not in raw


def test_list_orders_newest_first() -> None:
    # ``list`` orders by ts DESC, id DESC. To make ordering deterministic (uuid4
    # ids are unordered), stamp distinct ts values directly so the test reflects
    # only the ts-DESC behavior the Activity view relies on.
    log, conn = _audit()
    aid1 = log.append("assistant", "kb_search", "OBSERVE", "auto", True, args_summary="first")
    aid2 = log.append("assistant", "kb_search", "OBSERVE", "auto", True, args_summary="second")
    aid3 = log.append("assistant", "kb_search", "OBSERVE", "auto", True, args_summary="third")
    conn.execute("UPDATE audit_log SET ts = TIMESTAMP '2026-01-01 00:00:01' WHERE id = ?;", [aid1])
    conn.execute("UPDATE audit_log SET ts = TIMESTAMP '2026-01-01 00:00:02' WHERE id = ?;", [aid2])
    conn.execute("UPDATE audit_log SET ts = TIMESTAMP '2026-01-01 00:00:03' WHERE id = ?;", [aid3])
    summaries = [e["args_summary"] for e in log.list()]
    assert summaries == ["third", "second", "first"]
    assert len(summaries) == 3  # all three rows surfaced


def test_list_limit_bounded() -> None:
    # _LIST_LIMIT = 500 (assert-guarded); a caller cannot bypass it via a huge limit.
    log, _ = _audit()
    log.append("assistant", "kb_search", "OBSERVE", "auto", True, args_summary="x")
    assert len(log.list(limit=1)) == 1
    with pytest.raises(AssertionError):
        log.list(limit=10_000)


def test_wrong_key_cannot_decrypt() -> None:
    # AAD binds the body to the row id + the master key; a different key cannot
    # recover the contents.
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    AuditLog(conn, gen_master_key()).append(
        "assistant", "kb_search", "OBSERVE", "auto", True, args_summary="secret",
    )
    other = AuditLog(conn, gen_master_key())
    with pytest.raises(Exception):
        other.list()


def test_append_rejects_unknown_decision() -> None:
    # The decision vocabulary is closed; bogus values are caught at append time.
    log, _ = _audit()
    with pytest.raises(AssertionError):
        log.append("assistant", "kb_search", "OBSERVE", "bogus", True)


def test_append_rejects_unknown_actor() -> None:
    log, _ = _audit()
    with pytest.raises(AssertionError):
        log.append("daemon", "kb_search", "OBSERVE", "auto", True)
