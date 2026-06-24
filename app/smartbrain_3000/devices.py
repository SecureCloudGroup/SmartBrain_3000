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
"""

from __future__ import annotations

import datetime
import json
import secrets as token_lib

_PREFIX = "device:"
_CREDENTIAL_BYTES = 32  # ~256-bit, matches the MCP access token
_ID_BYTES = 8           # short, public device identifier
_MAX_LABEL = 64


class DeviceError(ValueError):
    """An invalid device operation (bad id/label)."""


def _key(device_id: str) -> str:
    """Secret-store key holding a device record."""
    assert device_id and ":" not in device_id, "device id must be non-empty and ':'-free"
    return f"{_PREFIX}{device_id}"


def _now() -> str:
    """Current UTC time as an ISO-8601 string (for created_at ordering)."""
    return datetime.datetime.now(datetime.UTC).isoformat()


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
        "credential": token_lib.token_urlsafe(_CREDENTIAL_BYTES),
    }
    store.put(_key(device_id), json.dumps(record))
    assert store.get(_key(device_id)) is not None, "device record must persist"
    return record


def list_devices(store) -> list[dict]:
    """Return public device metadata (device_id, label, created_at) — never credentials."""
    assert store is not None, "unlocked secret store required"
    out: list[dict] = []
    for key in store.list_keys():  # bounded by the number of stored secrets
        if not key.startswith(_PREFIX):
            continue
        rec = json.loads(store.get(key))
        out.append({"device_id": rec["device_id"], "label": rec["label"], "created_at": rec["created_at"]})
    return sorted(out, key=lambda d: d["created_at"])


def revoke_device(store, device_id: str) -> None:
    """Delete a device's credential so it can no longer pair/connect (idempotent)."""
    assert store is not None, "unlocked secret store required"
    assert device_id, "device id required"
    store.delete(_key(device_id))


def device_exists(store, device_id: str) -> bool:
    """True if ``device_id`` is still a registered (non-revoked) device.

    Used to enforce revocation on every request of a live connection: revoking a
    device deletes its record, so an in-flight session is cut off immediately.
    """
    assert store is not None, "unlocked secret store required"
    if not device_id or ":" in device_id:
        return False
    return store.get(_key(device_id)) is not None


def verify_device(store, device_id: str, credential: str) -> bool:
    """Constant-time check that ``(device_id, credential)`` is a live device."""
    assert store is not None, "unlocked secret store required"
    if not device_id or ":" in device_id or not credential:
        return False
    raw = store.get(_key(device_id))
    if raw is None:
        return False
    stored = json.loads(raw).get("credential", "")
    return token_lib.compare_digest(stored, credential)
