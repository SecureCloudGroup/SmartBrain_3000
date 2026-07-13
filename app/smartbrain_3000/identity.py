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
# A SECOND, separate signing identity, used to sign published vaults. Deliberately not the WebRTC
# key: that one is pinned by every paired phone and must never change, while a publisher identity is
# public and may one day need rotating. Rotating one must not break the other, and compromise of a
# published identity must not let anyone impersonate this Desktop to its own phone.
VAULT_PUBLISHER_SECRET = "vault:publisher_ed25519"
_RAW = serialization.Encoding.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw
_RAW_PUB = serialization.PublicFormat.Raw


def _load_or_create(store, secret_key: str = _PRIVKEY_SECRET) -> Ed25519PrivateKey:
    """Return an Ed25519 private key from the secret store, generating + persisting it once.

    ``secret_key`` selects WHICH identity: the WebRTC one (default) or the vault publisher one."""
    assert store is not None, "unlocked secret store required"
    assert secret_key, "secret key name required"
    raw = store.get(secret_key)
    if raw is not None:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))
    key = Ed25519PrivateKey.generate()
    secret = key.private_bytes(_RAW, _RAW_PRIV, serialization.NoEncryption())
    store.put(secret_key, base64.b64encode(secret).decode("ascii"))
    assert store.get(secret_key) is not None, "identity key must persist"
    return key


def public_key_b64(store, secret_key: str = _PRIVKEY_SECRET) -> str:
    """Return a public key (base64) — the WebRTC one is pinned by the phone at pairing."""
    pub = _load_or_create(store, secret_key).public_key().public_bytes(_RAW, _RAW_PUB)
    return base64.b64encode(pub).decode("ascii")


def sign(store, data: bytes, secret_key: str = _PRIVKEY_SECRET) -> str:
    """Sign ``data`` with one of this Desktop's identities; return a base64 signature."""
    assert isinstance(data, (bytes, bytearray)), "data to sign must be bytes"
    return base64.b64encode(_load_or_create(store, secret_key).sign(bytes(data))).decode("ascii")


def verify(public_key_b64_str: str, data: bytes, signature_b64: str) -> bool:
    """Verify an Ed25519 signature against a base64 public key (mirrors the client)."""
    assert isinstance(data, (bytes, bytearray)), "signed data must be bytes"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64_str))
        pub.verify(base64.b64decode(signature_b64), bytes(data))
        return True
    except Exception:
        return False
