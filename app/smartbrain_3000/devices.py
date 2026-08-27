"""Per-device credentials for remote (WebRTC) pairing — requires unlock.

Each paired phone gets a random bearer credential the Desktop will accept on the
signaling channel. We reuse the existing encrypted ``SecretStore`` (AES-256-GCM,
the same store that holds provider keys and the MCP token) rather than a new
table: a device is one JSON record stored under the key ``device:<id>``. Listing
always happens while unlocked, so there is no need for plaintext-without-decrypt.

Minting/listing/revoking mirror the MCP-token flow (mcp_routes.py). The pairing
payload returned at mint time gains the signaling URL, the Desktop's DTLS
fingerprint, and TURN material in a later phase; here it carries the device's
identity + one-time credential.

The credential is a bearer bundle with no expiry: ROTATION IS REVOKE + RE-PAIR. To make a
stale device visible, each record carries ``created_at`` and ``last_seen`` (ISO UTC, stamped
on a successful verify at most once per hour so a busy phone doesn't rewrite the encrypted
row on every reconnect).

LOCKED-STATE HINT (see webrtc_peer.py): a locked Desktop has no store, yet should tell a
PAIRED phone "locked" without telling a stranger anything. So beside the encrypted records
we keep, in the PLAINTEXT meta table, HMAC-SHA256(boot_key, device_id) for every live device
plus the random per-install ``boot_key``. Device ids are public routing ids and the digests
are keyed, so this reveals nothing about credentials — only that the Desktop can recognise
an id it minted. The meta conn is bound once at startup via :func:`bind_meta`.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import secrets as token_lib

from . import db

log = logging.getLogger(__name__)

_PREFIX = "device:"
_CREDENTIAL_BYTES = 32  # ~256-bit, matches the MCP access token
_ID_BYTES = 8           # short, public device identifier
_MAX_LABEL = 64
_LAST_SEEN_MIN_S = 3600  # stamp last_seen at most this often per device (bounded writes)

_META_KEY_DIGESTS = "devices:known_digests"  # JSON list of hex HMAC digests (plaintext)
_META_KEY_BOOT_KEY = "devices:boot_key"      # 32 random bytes, hex (plaintext, per install)

_meta_conn = None  # plaintext meta conn (ThreadLocalConn) — set by bind_meta at startup


class DeviceError(ValueError):
    """An invalid device operation (bad id/label)."""


def _key(device_id: str) -> str:
    """Secret-store key holding a device record."""
    assert device_id and ":" not in device_id, "device id must be non-empty and ':'-free"
    return f"{_PREFIX}{device_id}"


def _now() -> str:
    """Current UTC time as an ISO-8601 string (for created_at ordering)."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def bind_meta(conn) -> None:
    """Bind the plaintext meta conn used for the locked-state digest set (main.py, at boot)."""
    global _meta_conn
    _meta_conn = conn


def _boot_key() -> bytes | None:
    """The per-install HMAC key for device-id digests (created on first use; plaintext)."""
    if _meta_conn is None:
        return None
    raw = db.meta_get(_meta_conn, _META_KEY_BOOT_KEY)
    if not raw:
        raw = token_lib.token_hex(32)
        db.meta_set(_meta_conn, _META_KEY_BOOT_KEY, raw)
    return bytes.fromhex(raw)


def _digest(device_id: str, key: bytes) -> str:
    return hmac.new(key, device_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _known_digests() -> set[str]:
    if _meta_conn is None:
        return set()
    raw = db.meta_get(_meta_conn, _META_KEY_DIGESTS)
    try:
        return set(json.loads(raw)) if raw else set()
    except ValueError:
        return set()


def _update_known(device_id: str, add: bool) -> None:
    """Add/remove a device id's digest in the plaintext set (no-op when meta isn't bound)."""
    key = _boot_key()
    if key is None:
        return
    digests = _known_digests()
    (digests.add if add else digests.discard)(_digest(device_id, key))
    db.meta_set(_meta_conn, _META_KEY_DIGESTS, json.dumps(sorted(digests)))


def is_known_device_id(device_id: str) -> bool:
    """True if ``device_id`` was minted here and not revoked — works while LOCKED (no store).

    Reads only the plaintext digest set, so the answer says nothing about credentials; it
    gates whether a peer is told "locked" or just dropped (webrtc_peer.py).
    """
    if not device_id or ":" in device_id:
        return False
    key = _boot_key()
    if key is None:
        return False
    return _digest(device_id, key) in _known_digests()


def create_device(store, label: str = "device") -> dict:
    """Mint a new device credential; return its record INCLUDING the one-time credential.

    The returned ``credential`` is shown once (it's the device's bearer secret);
    later reads via :func:`list_devices` never include it.
    """
    assert store is not None, "unlocked secret store required"
    clean = (str(label) or "device").strip()[:_MAX_LABEL] or "device"
    device_id = token_lib.token_urlsafe(_ID_BYTES)
    record = {
        "device_id": device_id,
        "label": clean,
        "created_at": _now(),
        "last_seen": None,
        "credential": token_lib.token_urlsafe(_CREDENTIAL_BYTES),
    }
    store.put(_key(device_id), json.dumps(record))
    assert store.get(_key(device_id)) is not None, "device record must persist"
    _update_known(device_id, add=True)
    return record


def list_devices(store) -> list[dict]:
    """Return public device metadata (device_id, label, created_at, last_seen) — never credentials."""
    assert store is not None, "unlocked secret store required"
    out: list[dict] = []
    for key in store.list_keys():  # bounded by the number of stored secrets
        if not key.startswith(_PREFIX):
            continue
        rec = json.loads(store.get(key))
        out.append({
            "device_id": rec["device_id"], "label": rec["label"],
            "created_at": rec["created_at"], "last_seen": rec.get("last_seen"),
        })
    return sorted(out, key=lambda d: d["created_at"])


def revoke_device(store, device_id: str) -> None:
    """Delete a device's credential so it can no longer pair/connect (idempotent).

    This is also the rotation path: there is no credential refresh — revoke, then pair again.
    """
    assert store is not None, "unlocked secret store required"
    assert device_id, "device id required"
    store.delete(_key(device_id))
    if ":" not in device_id:
        _update_known(device_id, add=False)


def device_exists(store, device_id: str) -> bool:
    """True if ``device_id`` is still a registered (non-revoked) device.

    Used to enforce revocation on every request of a live connection: revoking a
    device deletes its record, so an in-flight session is cut off immediately.
    """
    assert store is not None, "unlocked secret store required"
    if not device_id or ":" in device_id:
        return False
    return store.get(_key(device_id)) is not None


def _stamp_last_seen(store, device_id: str, rec: dict, now: datetime.datetime) -> None:
    """Write ``last_seen`` if the stored stamp is older than an hour (or absent)."""
    prev = rec.get("last_seen")
    if prev:
        try:
            if (now - datetime.datetime.fromisoformat(prev)).total_seconds() < _LAST_SEEN_MIN_S:
                return
        except ValueError:
            pass  # unparsable stamp -> overwrite
    rec["last_seen"] = now.isoformat()
    store.put(_key(device_id), json.dumps(rec))


def verify_device(store, device_id: str, credential: str, now: datetime.datetime | None = None) -> bool:
    """Constant-time check that ``(device_id, credential)`` is a live device.

    A success stamps ``last_seen`` (at most hourly per device) so Settings can show stale
    devices worth revoking. ``now`` is injectable for tests.
    """
    assert store is not None, "unlocked secret store required"
    if not device_id or ":" in device_id or not credential:
        return False
    raw = store.get(_key(device_id))
    if raw is None:
        return False
    rec = json.loads(raw)
    if not token_lib.compare_digest(rec.get("credential", ""), credential):
        return False
    try:
        _stamp_last_seen(store, device_id, rec, now or datetime.datetime.now(datetime.UTC))
    except Exception as exc:  # bookkeeping must never fail an otherwise valid auth
        log.warning("devices: last_seen stamp failed: %s", type(exc).__name__)
    return True
