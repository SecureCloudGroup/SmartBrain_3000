"""Tests for the local encrypted secret store (B1)."""

from __future__ import annotations

import duckdb
import pytest

from smartbrain_3000.secrets import MASTER_KEY_BYTES, SecretStore, gen_master_key


def _store() -> tuple[duckdb.DuckDBPyConnection, SecretStore]:
    conn = duckdb.connect(":memory:")
    return conn, SecretStore(conn, gen_master_key())


def test_gen_master_key_length() -> None:
    assert len(gen_master_key()) == MASTER_KEY_BYTES


def test_round_trip() -> None:
    _, store = _store()
    store.put("provider:openai:api_key", "sk-secret-123")
    assert store.get("provider:openai:api_key") == "sk-secret-123"


def test_absent_returns_none() -> None:
    _, store = _store()
    assert store.get("missing") is None


def test_update_overwrites() -> None:
    _, store = _store()
    store.put("k", "v1")
    store.put("k", "v2")
    assert store.get("k") == "v2"


def test_delete() -> None:
    _, store = _store()
    store.put("k", "v")
    store.delete("k")
    assert store.get("k") is None


def test_list_keys_sorted() -> None:
    _, store = _store()
    store.put("b", "1")
    store.put("a", "2")
    assert store.list_keys() == ["a", "b"]


def test_ciphertext_at_rest_is_not_plaintext() -> None:
    conn, store = _store()
    store.put("k", "super-secret-value")
    row = conn.execute("SELECT ciphertext FROM secrets WHERE key = 'k';").fetchone()
    raw = bytes(row[0])
    assert b"super-secret-value" not in raw  # stored encrypted, not in the clear
    assert len(raw) > 0


def test_wrong_master_key_cannot_decrypt() -> None:
    conn, store = _store()
    store.put("k", "v")
    other = SecretStore(conn, gen_master_key())  # different key, same table
    with pytest.raises(Exception):
        other.get("k")


def test_aad_binding_prevents_row_swap() -> None:
    conn, store = _store()
    store.put("key_a", "value_a")
    row = conn.execute(
        "SELECT nonce, ciphertext FROM secrets WHERE key = 'key_a';"
    ).fetchone()
    # Copy key_a's encrypted bytes under a different key name.
    conn.execute(
        "INSERT INTO secrets (key, nonce, ciphertext) VALUES (?, ?, ?);",
        ["key_b", row[0], row[1]],
    )
    # AAD for "key_b" != AAD used at encryption ("key_a") -> auth must fail.
    with pytest.raises(Exception):
        store.get("key_b")
