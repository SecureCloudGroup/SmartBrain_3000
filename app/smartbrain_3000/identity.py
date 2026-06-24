"""Desktop identity keypair for WebRTC channel authentication (Phase 4).

The Desktop holds a long-term Ed25519 signing key — the private key is generated once
and stored encrypted in the SecretStore; the PUBLIC key is pinned by the phone at
pairing. Before the phone sends its device credential, it challenges the Desktop with a
random nonce; the Desktop signs ``nonce || channel_binding`` with this key, and the phone
verifies the signature against the pinned public key.

The ``channel_binding`` is derived from the DTLS fingerprints of the ACTUAL peer
connection (see webrtc_peer._channel_binding). A relaying MITM broker terminates DTLS on
each leg with different fingerprints, so a signature it forwards from the real Desktop is
bound to the wrong channel and fails verification on the phone — the broker cannot
impersonate the Desktop, and the credential never reaches it.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_PRIVKEY_SECRET = "webrtc:identity_ed25519"
_RAW = serialization.Encoding.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw
_RAW_PUB = serialization.PublicFormat.Raw


def _load_or_create(store) -> Ed25519PrivateKey:
    """Return the Desktop's Ed25519 private key, generating + persisting it once."""
    assert store is not None, "unlocked secret store required"
    raw = store.get(_PRIVKEY_SECRET)
    if raw is not None:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))
    key = Ed25519PrivateKey.generate()
    secret = key.private_bytes(_RAW, _RAW_PRIV, serialization.NoEncryption())
    store.put(_PRIVKEY_SECRET, base64.b64encode(secret).decode("ascii"))
    assert store.get(_PRIVKEY_SECRET) is not None, "identity key must persist"
    return key


def public_key_b64(store) -> str:
    """Return the Desktop's public key (base64) — pinned by the phone at pairing."""
    pub = _load_or_create(store).public_key().public_bytes(_RAW, _RAW_PUB)
    return base64.b64encode(pub).decode("ascii")


def sign(store, data: bytes) -> str:
    """Sign ``data`` with the Desktop's identity key; return a base64 signature."""
    assert isinstance(data, (bytes, bytearray)), "data to sign must be bytes"
    return base64.b64encode(_load_or_create(store).sign(bytes(data))).decode("ascii")


def verify(public_key_b64_str: str, data: bytes, signature_b64: str) -> bool:
    """Verify an Ed25519 signature against a base64 public key (mirrors the client)."""
    assert isinstance(data, (bytes, bytearray)), "signed data must be bytes"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64_str))
        pub.verify(base64.b64decode(signature_b64), bytes(data))
        return True
    except Exception:
        return False
