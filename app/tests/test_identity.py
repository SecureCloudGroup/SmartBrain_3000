"""Tests for the Desktop identity keypair (identity.py) — Phase 4 channel auth."""

from __future__ import annotations

import duckdb

from smartbrain_3000 import identity
from smartbrain_3000.secrets import SecretStore, gen_master_key


def _store() -> SecretStore:
    return SecretStore(duckdb.connect(":memory:"), gen_master_key())


def test_keypair_is_stable_and_persisted() -> None:
    store = _store()
    pub1 = identity.public_key_b64(store)
    pub2 = identity.public_key_b64(store)
    assert pub1 and pub1 == pub2  # generated once, then stable


def test_sign_then_verify_roundtrip() -> None:
    store = _store()
    pub = identity.public_key_b64(store)
    data = b"nonce-bytes||channel-binding"
    sig = identity.sign(store, data)
    assert identity.verify(pub, data, sig) is True


def test_verify_rejects_tampering() -> None:
    store = _store()
    pub = identity.public_key_b64(store)
    sig = identity.sign(store, b"original")
    assert identity.verify(pub, b"different", sig) is False          # wrong signed data (e.g. relayed binding)
    assert identity.verify(pub, b"original", "AAAA") is False         # garbage signature
    other_pub = identity.public_key_b64(_store())                     # a different Desktop's key
    assert identity.verify(other_pub, b"original", sig) is False      # signature doesn't match pinned key
