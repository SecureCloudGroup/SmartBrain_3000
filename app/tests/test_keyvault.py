"""Tests for passphrase-based master-key unlock (B2)."""

from __future__ import annotations

import os
import tempfile

import duckdb
import pytest

from smartbrain_3000 import keyvault
from smartbrain_3000.secrets import MASTER_KEY_BYTES, SecretStore


def _conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def test_not_initialized_initially() -> None:
    assert keyvault.is_initialized(_conn()) is False


def test_set_then_initialized() -> None:
    conn = _conn()
    keyvault.set_passphrase(conn, "correct horse battery staple")
    assert keyvault.is_initialized(conn) is True


def test_set_returns_32_byte_key() -> None:
    assert len(keyvault.set_passphrase(_conn(), "pw")) == MASTER_KEY_BYTES


def test_unlock_correct_passphrase_returns_same_key() -> None:
    conn = _conn()
    mk1 = keyvault.set_passphrase(conn, "s3cret-pass")
    mk2 = keyvault.unlock(conn, "s3cret-pass")
    assert mk1 == mk2


def test_unlock_wrong_passphrase_fails() -> None:
    conn = _conn()
    keyvault.set_passphrase(conn, "right-pass")
    with pytest.raises(Exception):
        keyvault.unlock(conn, "wrong-pass")


def test_double_set_raises() -> None:
    conn = _conn()
    keyvault.set_passphrase(conn, "pw")
    with pytest.raises(AssertionError):
        keyvault.set_passphrase(conn, "pw2")


def test_unlock_before_set_raises() -> None:
    with pytest.raises(AssertionError):
        keyvault.unlock(_conn(), "pw")


def test_end_to_end_persisted_with_secret_store() -> None:
    # Set passphrase, store a secret, persist to disk, reopen, unlock, read back.
    path = os.path.join(tempfile.mkdtemp(), "vault.duckdb")
    conn = duckdb.connect(path)
    master_key = keyvault.set_passphrase(conn, "pp")
    SecretStore(conn, master_key).put("provider:openai:api_key", "sk-xyz")
    conn.close()

    conn2 = duckdb.connect(path)  # simulates a restart
    master_key2 = keyvault.unlock(conn2, "pp")
    assert master_key2 == master_key
    assert SecretStore(conn2, master_key2).get("provider:openai:api_key") == "sk-xyz"
