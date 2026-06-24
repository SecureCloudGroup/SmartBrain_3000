"""Pairing-by-code crypto for the home-screen PWA path.

The installed (Add to Home Screen) PWA gets storage that iOS isolates from Safari, so it
cannot inherit a Safari pairing. Instead the Desktop shows a short one-time CODE; the app
derives a broker rendezvous room + a shared key from it, connects to the Desktop over
WebRTC (relayed by the broker), authenticates the channel with the code (HMAC over the
DTLS channel binding — the same anti-MITM technique as the normal channel-auth), and the
Desktop hands the pairing payload over inside the DTLS-encrypted channel.

TRUST MODEL: with a 6-char code, an HONEST broker (and any network observer, behind TLS)
learns nothing — the payload is DTLS end-to-end and the channel binding rejects a passive
relay. A deliberately MALICIOUS broker could brute-force the short code offline (PBKDF2
raises the cost but does not make it infeasible) and MITM the pairing. Run code-pairing on
a broker you control; QR pairing (in Safari) stays fully out-of-band for an untrusted broker.

The derivation MUST stay byte-identical to web/src/lib/remote/paircode.ts.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# Unambiguous uppercase alphabet (no O/0/I/1/L) — 32 symbols, ~5 bits each => 6 chars ~30 bits.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 6
_SALT = b"sb-pair-v1"
_ITERS = 300_000
_KEY_LEN = 32  # HMAC key length; room id takes a further 16 bytes


def generate_code() -> str:
    """A fresh 6-char pairing code from the unambiguous alphabet."""
    code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    assert len(code) == _CODE_LEN, "generated code must be 6 chars"
    assert all(c in _ALPHABET for c in code), "code must use the alphabet"
    return code


def normalize(code: str) -> str:
    """Normalize user input (uppercase, drop spaces/dashes/ambiguous) before deriving."""
    assert isinstance(code, str), "code must be a string"
    norm = "".join(c for c in code.upper() if c in _ALPHABET)
    assert isinstance(norm, str), "normalize returns a string"
    return norm


def derive(code: str) -> tuple[str, bytes]:
    """Derive ``(room_id, code_key)`` from the code: room_id routes via the broker, code_key
    authenticates the channel. PBKDF2 over 48 bytes, split 16 (room) + 32 (key)."""
    norm = normalize(code)
    assert len(norm) == _CODE_LEN, "code must be 6 characters from the alphabet"
    dk = hashlib.pbkdf2_hmac("sha256", norm.encode("ascii"), _SALT, _ITERS, dklen=16 + _KEY_LEN)
    assert len(dk) == 16 + _KEY_LEN, "derived key block has the expected length"
    return "sbpair-" + dk[:16].hex(), dk[16:]


def mac(code_key: bytes, label: str, nonce: bytes, binding: bytes) -> bytes:
    """HMAC-SHA256(code_key, label || nonce || binding) — proves code knowledge bound to THIS
    DTLS channel. ``label`` ('host'/'guest') separates the two directions so neither replays."""
    assert label in ("host", "guest"), "label must be host or guest"
    assert isinstance(nonce, bytes) and isinstance(binding, bytes), "nonce + binding must be bytes"
    return hmac.new(code_key, label.encode("ascii") + nonce + binding, hashlib.sha256).digest()


def mac_equal(a: bytes, b: bytes) -> bool:
    """Constant-time MAC comparison."""
    assert isinstance(a, bytes) and isinstance(b, bytes), "compare bytes only"
    return hmac.compare_digest(a, b)
