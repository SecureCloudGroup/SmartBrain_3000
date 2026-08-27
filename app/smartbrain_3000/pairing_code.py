"""Pairing-by-code crypto for the home-screen PWA path.

The installed (Add to Home Screen) PWA gets storage that iOS isolates from Safari, so it
cannot inherit a Safari pairing. Instead the Desktop shows a short one-time CODE; the app
derives a broker rendezvous room + a shared key from it, connects to the Desktop over
WebRTC (relayed by the broker), authenticates the channel with the code (HMAC over the
DTLS channel binding — the same anti-MITM technique as the normal channel-auth), and the
Desktop hands the pairing payload over inside the DTLS-encrypted channel.

TRUST MODEL: an HONEST broker (and any network observer, behind TLS) learns nothing — the
payload is DTLS end-to-end and the channel binding rejects a passive relay. A deliberately
MALICIOUS broker sees the room id, which is PBKDF2(code) under a FIXED salt, so it can grind
the code OFFLINE and then answer the offer itself (MITM the pairing). The arithmetic:

  8 characters over a 31-symbol alphabet = 31^8 ≈ 2^39.6 codes
  x 300,000 PBKDF2-SHA256 iterations (≈ 2^18.2)   => ≈ 2^58 SHA-256 evaluations per grind

That is a COST, not an impossibility (a GPU farm finishes it; a laptop does not within the
5-minute code window). The 8-char code buys ~2^10 over the earlier 6-char one; a self-hosted
broker removes the attacker entirely, and QR pairing (in Safari) stays fully out-of-band.

The derivation MUST stay byte-identical to web/src/lib/remote/paircode.ts.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# Unambiguous uppercase alphabet (no O/0/I/1/L) — 31 symbols, ~4.95 bits each => 8 chars ~39.6 bits.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
_SALT = b"sb-pair-v1"
_ITERS = 300_000
_KEY_LEN = 32  # HMAC key length; room id takes a further 16 bytes


def generate_code() -> str:
    """A fresh 8-char pairing code from the unambiguous alphabet (shown as ABCD-EFGH)."""
    code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    assert len(code) == _CODE_LEN, "generated code must be 8 chars"
    assert all(c in _ALPHABET for c in code), "code must use the alphabet"
    return code


def normalize(code: str) -> str:
    """Normalize user input before deriving: uppercase, drop spaces/dashes/anything outside
    the alphabet — so "ABCD-EFGH", "abcd efgh" and "ABCDEFGH" are the same code."""
    assert isinstance(code, str), "code must be a string"
    norm = "".join(c for c in code.upper() if c in _ALPHABET)
    assert isinstance(norm, str), "normalize returns a string"
    return norm


def derive(code: str) -> tuple[str, bytes]:
    """Derive ``(room_id, code_key)`` from the code: room_id routes via the broker, code_key
    authenticates the channel. PBKDF2 over 48 bytes, split 16 (room) + 32 (key)."""
    norm = normalize(code)
    assert len(norm) == _CODE_LEN, "code must be 8 characters from the alphabet"
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
