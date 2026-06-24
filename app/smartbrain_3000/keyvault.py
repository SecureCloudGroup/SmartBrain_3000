"""Passphrase- and recovery-key-based unlock for the master key.

The master key (which encrypts all secrets — see ``secrets.py``) is wrapped and
stored locally under one or more *methods*:

* ``passphrase`` — wrapped with a key derived from the user's passphrase via
  Argon2id (passphrases are low-entropy, so a slow KDF is required).
* ``recovery``  — wrapped directly under a generated, high-entropy Recovery Key
  (256-bit), shown once in an Emergency Kit.

Either method decrypts the SAME master key. Nothing here leaves the machine:
there is no server and no escrow. A forgotten passphrase is only recoverable via
the Recovery Key, so the Emergency Kit must be kept safe and offline.
"""

from __future__ import annotations

import base64
import os

import duckdb
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import MASTER_KEY_BYTES, gen_master_key

_SALT_BYTES = 16
_NONCE_BYTES = 12

# Argon2id defaults (OWASP-style). Stored per-vault so they can be raised later.
_TIME_COST = 3
_MEMORY_COST = 64 * 1024  # 64 MiB
_PARALLELISM = 4

# Upper bounds on the per-vault KDF parameters. These are read from the (possibly
# restored or corrupted) key_wraps row and fed to a resource-bounded primitive, so
# they must be validated — an unbounded memory_cost would OOM the unlock worker.
_MAX_TIME_COST = 16
_MAX_MEMORY_COST = 1024 * 1024  # 1 GiB in KiB
_MAX_PARALLELISM = 16


def _aad(method: str) -> bytes:
    """Associated data binding a wrap to its unlock method."""
    assert method, "method required"
    assert method in ("passphrase", "recovery"), "unknown unlock method"
    return f"smartbrain_3000:master_key:{method}".encode("utf-8")


def _derive(
    passphrase: str, salt: bytes, time_cost: int, memory_cost: int, parallelism: int
) -> bytes:
    """Derive a 32-byte wrapping key from a passphrase via Argon2id."""
    assert passphrase, "passphrase must be non-empty"
    # Validate the stored/forwarded KDF parameters (NOT via assert — must hold
    # under `python -O`, and a corrupt/planted wrap must fail cleanly, not OOM).
    if len(salt) != _SALT_BYTES:
        raise ValueError("invalid key wrap: salt length")
    if not 1 <= time_cost <= _MAX_TIME_COST:
        raise ValueError("invalid key wrap: time_cost out of range")
    if not 8 <= memory_cost <= _MAX_MEMORY_COST:
        raise ValueError("invalid key wrap: memory_cost out of range")
    if not 1 <= parallelism <= _MAX_PARALLELISM:
        raise ValueError("invalid key wrap: parallelism out of range")
    key = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=MASTER_KEY_BYTES,
        type=Type.ID,
    )
    assert len(key) == MASTER_KEY_BYTES, "derived key must be 32 bytes"
    return key


def _encode_recovery(raw: bytes) -> str:
    """Render 32 random bytes as a grouped, uppercase base32 Recovery Key."""
    assert len(raw) == MASTER_KEY_BYTES, "recovery secret must be 32 bytes"
    b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
    grouped = "-".join(b32[i : i + 4] for i in range(0, len(b32), 4))
    assert grouped, "recovery display must be non-empty"
    return grouped


def _decode_recovery(display: str) -> bytes:
    """Parse a Recovery Key string (dashes/case-insensitive) back to bytes."""
    assert display, "recovery key must be non-empty"
    cleaned = display.replace("-", "").replace(" ", "").upper()
    padded = cleaned + ("=" * ((-len(cleaned)) % 8))
    raw = base64.b32decode(padded)
    assert len(raw) == MASTER_KEY_BYTES, "recovery key must decode to 32 bytes"
    return raw


def gen_recovery_key() -> tuple[str, bytes]:
    """Generate a fresh Recovery Key; return (display string, raw 32 bytes)."""
    raw = os.urandom(MASTER_KEY_BYTES)
    display = _encode_recovery(raw)
    assert len(raw) == MASTER_KEY_BYTES, "recovery secret must be 32 bytes"
    assert _decode_recovery(display) == raw, "encode/decode must round-trip"
    return display, raw


def _ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the key_wraps table if absent; verify it exists."""
    assert conn is not None, "connection must be open"
    conn.execute(
        "CREATE TABLE IF NOT EXISTS key_wraps ("
        "method TEXT PRIMARY KEY, kdf TEXT NOT NULL, salt BLOB, "
        "time_cost INTEGER, memory_cost INTEGER, parallelism INTEGER, "
        "nonce BLOB NOT NULL, wrapped BLOB NOT NULL);"
    )
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'key_wraps';"
    ).fetchone()
    assert exists is not None, "key_wraps table must exist after creation"


def _has(conn: duckdb.DuckDBPyConnection, method: str) -> bool:
    """Return True if a wrap for ``method`` exists."""
    assert conn is not None, "connection must be open"
    _ensure_table(conn)
    row = conn.execute(
        "SELECT 1 FROM key_wraps WHERE method = ?;", [method]
    ).fetchone()
    assert row is None or len(row) == 1, "unexpected key_wraps row shape"
    return row is not None


def is_initialized(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True if a passphrase-wrapped master key exists."""
    assert conn is not None, "connection must be open"
    return _has(conn, "passphrase")


def has_recovery(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True if a Recovery Key has been configured."""
    assert conn is not None, "connection must be open"
    return _has(conn, "recovery")


def set_passphrase(conn: duckdb.DuckDBPyConnection, passphrase: str) -> bytes:
    """First run: create + wrap a new master key under ``passphrase``."""
    assert passphrase, "passphrase must be non-empty"
    assert not is_initialized(conn), "keyvault already initialized"
    master_key = gen_master_key()
    salt = os.urandom(_SALT_BYTES)
    wrapping = _derive(passphrase, salt, _TIME_COST, _MEMORY_COST, _PARALLELISM)
    nonce = os.urandom(_NONCE_BYTES)
    wrapped = AESGCM(wrapping).encrypt(nonce, master_key, _aad("passphrase"))
    conn.execute(
        "INSERT INTO key_wraps (method, kdf, salt, time_cost, memory_cost, "
        "parallelism, nonce, wrapped) VALUES "
        "('passphrase', 'argon2id', ?, ?, ?, ?, ?, ?);",
        [salt, _TIME_COST, _MEMORY_COST, _PARALLELISM, nonce, wrapped],
    )
    return master_key


def unlock(conn: duckdb.DuckDBPyConnection, passphrase: str) -> bytes:
    """Return the master key by decrypting it with ``passphrase``."""
    assert passphrase, "passphrase must be non-empty"
    assert is_initialized(conn), "keyvault is not initialized"
    row = conn.execute(
        "SELECT salt, time_cost, memory_cost, parallelism, nonce, wrapped "
        "FROM key_wraps WHERE method = 'passphrase';"
    ).fetchone()
    assert row is not None and len(row) == 6, "passphrase wrap malformed"
    salt = bytes(row[0])
    nonce, wrapped = bytes(row[4]), bytes(row[5])
    wrapping = _derive(passphrase, salt, int(row[1]), int(row[2]), int(row[3]))
    master_key = AESGCM(wrapping).decrypt(nonce, wrapped, _aad("passphrase"))
    assert len(master_key) == MASTER_KEY_BYTES, "unlocked key must be 32 bytes"
    return master_key


def reset_passphrase(conn: duckdb.DuckDBPyConnection, master_key: bytes, new: str) -> None:
    """Re-wrap an already-unlocked master key under a new passphrase.

    Unlike ``change_passphrase`` this does NOT require the current passphrase —
    being unlocked (e.g. via the Recovery Key) is the authorization. The master
    key is unchanged, so all encrypted data and the Recovery Key stay valid; only
    the passphrase wrap is replaced.
    """
    assert new, "new passphrase required"
    assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
    assert is_initialized(conn), "keyvault is not initialized"
    salt = os.urandom(_SALT_BYTES)
    wrapping = _derive(new, salt, _TIME_COST, _MEMORY_COST, _PARALLELISM)
    nonce = os.urandom(_NONCE_BYTES)
    wrapped = AESGCM(wrapping).encrypt(nonce, master_key, _aad("passphrase"))
    conn.execute(
        "UPDATE key_wraps SET kdf = 'argon2id', salt = ?, time_cost = ?, "
        "memory_cost = ?, parallelism = ?, nonce = ?, wrapped = ? WHERE method = 'passphrase';",
        [salt, _TIME_COST, _MEMORY_COST, _PARALLELISM, nonce, wrapped],
    )
    assert is_initialized(conn), "passphrase wrap must remain after re-wrap"


def change_passphrase(conn: duckdb.DuckDBPyConnection, current: str, new: str) -> None:
    """Re-wrap the master key under a new passphrase, verifying the current one first."""
    assert current and new, "current + new passphrase required"
    assert is_initialized(conn), "keyvault is not initialized"
    master_key = unlock(conn, current)  # raises on a wrong current passphrase
    reset_passphrase(conn, master_key, new)


def add_recovery_key(conn: duckdb.DuckDBPyConnection, master_key: bytes) -> str:
    """Generate a Recovery Key wrapping ``master_key``; return its display string."""
    assert is_initialized(conn), "set a passphrase before adding a recovery key"
    assert len(master_key) == MASTER_KEY_BYTES, "master key must be 32 bytes"
    display, raw = gen_recovery_key()
    nonce = os.urandom(_NONCE_BYTES)
    wrapped = AESGCM(raw).encrypt(nonce, master_key, _aad("recovery"))
    conn.execute(
        "INSERT INTO key_wraps (method, kdf, salt, time_cost, memory_cost, "
        "parallelism, nonce, wrapped) VALUES "
        "('recovery', 'direct', NULL, NULL, NULL, NULL, ?, ?) "
        "ON CONFLICT (method) DO UPDATE SET kdf = excluded.kdf, "
        "nonce = excluded.nonce, wrapped = excluded.wrapped;",
        [nonce, wrapped],
    )
    return display


def unlock_with_recovery(conn: duckdb.DuckDBPyConnection, recovery_key: str) -> bytes:
    """Return the master key by decrypting it with the Recovery Key."""
    assert recovery_key, "recovery key must be non-empty"
    row = conn.execute(
        "SELECT nonce, wrapped FROM key_wraps WHERE method = 'recovery';"
    ).fetchone()
    assert row is not None, "no recovery key configured"
    raw = _decode_recovery(recovery_key)
    nonce, wrapped = bytes(row[0]), bytes(row[1])
    master_key = AESGCM(raw).decrypt(nonce, wrapped, _aad("recovery"))
    assert len(master_key) == MASTER_KEY_BYTES, "unlocked key must be 32 bytes"
    return master_key


def emergency_kit_text(
    recovery_key: str,
    product: str = "SmartBrain_3000",
    licensor: str = "The Frels Holdings LLC",
) -> str:
    """Render the printable Emergency Kit content for ``recovery_key``."""
    assert recovery_key, "recovery key required"
    assert product, "product name required"
    lines = [
        f"{product} — Emergency Kit",
        "",
        "Keep this in a safe, OFFLINE place (print it, or store it in a password",
        "manager). Anyone with this Recovery Key can unlock your data; and",
        "without it, a forgotten passphrase CANNOT be recovered — there is no",
        "server and no reset.",
        "",
        "Recovery Key:",
        f"    {recovery_key}",
        "",
        'To recover: open the app, choose "Unlock with Recovery Key", and enter',
        "the key exactly as shown (dashes and letter case do not matter).",
        "",
        f"(c) 2026 {licensor}",
    ]
    text = "\n".join(lines)
    assert recovery_key in text, "kit must contain the recovery key"
    return text
