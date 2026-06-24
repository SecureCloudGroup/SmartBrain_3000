"""Tests for Recovery Key + Emergency Kit (B3)."""

from __future__ import annotations

import os
import tempfile

import duckdb
import pytest

from smartbrain_3000 import keyvault
from smartbrain_3000.secrets import MASTER_KEY_BYTES, SecretStore


def _init(passphrase: str = "pw") -> tuple[duckdb.DuckDBPyConnection, bytes, str]:
    conn = duckdb.connect(":memory:")
    master_key = keyvault.set_passphrase(conn, passphrase)
    recovery_key = keyvault.add_recovery_key(conn, master_key)
    return conn, master_key, recovery_key


def test_add_recovery_requires_init() -> None:
    conn = duckdb.connect(":memory:")
    with pytest.raises(AssertionError):
        keyvault.add_recovery_key(conn, b"\x00" * MASTER_KEY_BYTES)


def test_has_recovery_toggles() -> None:
    conn = duckdb.connect(":memory:")
    master_key = keyvault.set_passphrase(conn, "pw")
    assert keyvault.has_recovery(conn) is False
    keyvault.add_recovery_key(conn, master_key)
    assert keyvault.has_recovery(conn) is True


def test_recovery_round_trip() -> None:
    conn, master_key, recovery_key = _init()
    assert keyvault.unlock_with_recovery(conn, recovery_key) == master_key


def test_both_paths_yield_same_key() -> None:
    conn, master_key, recovery_key = _init("the-pass")
    assert keyvault.unlock(conn, "the-pass") == master_key
    assert keyvault.unlock_with_recovery(conn, recovery_key) == master_key


def test_recovery_dashes_and_case_optional() -> None:
    conn, master_key, recovery_key = _init()
    assert keyvault.unlock_with_recovery(conn, recovery_key.replace("-", "")) == master_key
    assert keyvault.unlock_with_recovery(conn, recovery_key.lower()) == master_key


def test_wrong_recovery_key_fails() -> None:
    conn, _, _ = _init()
    other_display, _ = keyvault.gen_recovery_key()  # a different, valid-format key
    with pytest.raises(Exception):
        keyvault.unlock_with_recovery(conn, other_display)


def test_recovery_key_format() -> None:
    display, raw = keyvault.gen_recovery_key()
    assert len(raw) == MASTER_KEY_BYTES
    assert "-" in display
    assert display.replace("-", "").isalnum()


def test_emergency_kit_contains_key_and_product() -> None:
    _, _, recovery_key = _init()
    kit = keyvault.emergency_kit_text(recovery_key)
    assert recovery_key in kit
    assert "SmartBrain_3000" in kit
    assert "Recovery Key" in kit


def test_end_to_end_recover_persisted() -> None:
    # Set passphrase + recovery, store a secret, persist, reopen, recover via key.
    path = os.path.join(tempfile.mkdtemp(), "vault.duckdb")
    conn = duckdb.connect(path)
    master_key = keyvault.set_passphrase(conn, "forgotten-later")
    recovery_key = keyvault.add_recovery_key(conn, master_key)
    SecretStore(conn, master_key).put("provider:anthropic:api_key", "sk-ant-xyz")
    conn.close()

    conn2 = duckdb.connect(path)  # simulates a restart; passphrase "lost"
    master_key2 = keyvault.unlock_with_recovery(conn2, recovery_key)
    assert SecretStore(conn2, master_key2).get("provider:anthropic:api_key") == "sk-ant-xyz"
