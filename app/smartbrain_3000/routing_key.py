"""Desktop routing key — proof-of-possession of ``desktop_routing_id`` toward the broker.

The signaling broker binds a desktop_id to the FIRST Ed25519 public key that registers it
(trust-on-first-use) and challenges every later registration to sign a nonce with it, so
a stranger who learns the id (it travels in the pairing payload and every phone hello)
cannot displace the real Desktop. The private key lives plaintext in the ``meta`` table
(see :func:`db.record_boot`): it must be usable while the vault is LOCKED, and it guards
only routing continuity — never data, which stays behind the device credential and the
pinned DTLS key.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_META_KEY = "desktop_routing_key"
_PREFIX = b"sb-register-v1"  # domain separation: this key signs nothing else


def generate_private_key_b64() -> str:
    """A fresh Ed25519 private key as base64 of its 32 raw bytes (the stored form)."""
    return base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode("ascii")


def _private_key(conn) -> Ed25519PrivateKey:
    from . import db

    raw = db.meta_get(conn, _META_KEY)
    assert raw, "desktop_routing_key must exist (record_boot generates it)"
    key = base64.b64decode(raw)
    assert len(key) == 32, "routing key must be 32 raw Ed25519 bytes"
    return Ed25519PrivateKey.from_private_bytes(key)


def public_key_b64(conn) -> str:
    """This Desktop's routing public key (base64 raw 32 bytes) — sent in the desktop hello."""
    assert conn is not None, "db connection required"
    return base64.b64encode(_private_key(conn).public_key().public_bytes_raw()).decode("ascii")


def sign(conn, data: bytes) -> str:
    """Base64 Ed25519 signature over ``data`` with the routing key."""
    assert conn is not None, "db connection required"
    assert isinstance(data, bytes) and data, "data to sign must be non-empty bytes"
    return base64.b64encode(_private_key(conn).sign(data)).decode("ascii")


def registration_message(nonce: bytes, desktop_id: str) -> bytes:
    """The exact bytes both sides sign/verify for a registration challenge."""
    assert isinstance(nonce, bytes) and nonce, "nonce required"
    assert desktop_id, "desktop_id required"
    return _PREFIX + nonce + desktop_id.encode("utf-8")
