"""Device registry API for remote (WebRTC) pairing — requires unlock.

Mirrors the MCP-token routes (mcp_routes.py): mint a per-device credential, list
paired devices (metadata only), and revoke one. All endpoints require the app to
be unlocked (423 otherwise), since they read/write the encrypted secret store.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import devices, identity, pairing_code, pairing_host, remote_config

router = APIRouter()


def _pairing_payload(store, app, label: str) -> dict:
    """Mint a device and assemble the PairingPayload the phone stores (matches pairing.ts)."""
    rec = devices.create_device(store, label)
    return {
        "v": 1,
        "deviceId": rec["device_id"],
        "credential": rec["credential"],
        "desktopPubkey": identity.public_key_b64(store),
        "signalingUrl": remote_config.signaling_url(),
        "desktopId": remote_config.desktop_id(getattr(app.state, "boot", None)),
        "iceServers": remote_config.ice_servers(),
    }


def _cancel_pair_session(app) -> None:
    """Stop any in-progress pairing-by-code session (one active at a time)."""
    sess = getattr(app.state, "pair_session", None)
    if sess is not None:
        sess["stop"].set()
        app.state.pair_session = None


class DeviceCreate(BaseModel):
    label: str = Field(default="device", max_length=64)


def _store(request: Request):
    """Return the unlocked SecretStore, or raise 423 if locked."""
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


@router.get("/api/devices")
def list_devices(request: Request) -> dict:
    """List paired devices (metadata only — never their credentials)."""
    return {"devices": devices.list_devices(_store(request))}


@router.post("/api/devices")
def create_device(request: Request, body: DeviceCreate) -> dict:
    """Mint a new device + one-time credential, plus the Desktop's pinned public key.

    The returned payload is what the phone stores at pairing: it pins ``desktop_pubkey``
    to verify the Desktop over the channel before ever sending its credential.
    """
    store = _store(request)
    rec = devices.create_device(store, body.label)
    return {
        **rec,
        "desktop_pubkey": identity.public_key_b64(store),
        "signaling_url": remote_config.signaling_url(),
        "desktop_id": remote_config.desktop_id(getattr(request.app.state, "boot", None)),
        "ice_servers": remote_config.ice_servers(),
    }


@router.post("/api/devices/pair-code")
async def start_pair_code(request: Request, body: DeviceCreate) -> dict:
    """Start a one-time pairing-by-code session for the installed (home-screen) app.

    Mints a device, generates a 6-char code, and hosts it on the broker for 5 minutes; the
    app enters the code to fetch the pairing over an encrypted channel (see pairing_code.py).
    Requires unlock + a configured signaling broker. One session at a time.
    """
    store = _store(request)
    signaling, token = remote_config.signaling_url(), os.environ.get("SMARTBRAIN_SIGNALING_TOKEN", "")
    if not signaling or not token:
        raise HTTPException(status_code=503, detail="remote access (signaling broker) is not configured")
    payload = _pairing_payload(store, request.app, body.label)
    code = pairing_code.generate_code()
    _cancel_pair_session(request.app)
    stop = asyncio.Event()
    task = asyncio.create_task(
        pairing_host.run_pairing_host(
            signaling_url=signaling, token=token, code=code, payload=payload,
            stop=stop, ice_servers=remote_config.ice_servers(), expiry_s=300,
        )
    )
    request.app.state.pair_session = {"stop": stop, "task": task}
    return {"code": code, "expires_in": 300}


@router.delete("/api/devices/pair-code")
def cancel_pair_code(request: Request) -> dict[str, bool]:
    """Cancel an in-progress pairing-by-code session (e.g. the operator closed the dialog)."""
    _cancel_pair_session(request.app)
    return {"ok": True}


@router.get("/api/devices/pair-code")
def pair_code_status(request: Request) -> dict[str, str]:
    """Status of the in-progress pairing-by-code session, for the Desktop's live feedback:
    waiting (phone hasn't paired yet) / paired (success) / expired (timed out or stopped)."""
    sess = getattr(request.app.state, "pair_session", None)
    if sess is None:
        return {"state": "none"}
    task = sess["task"]
    if not task.done():
        return {"state": "waiting"}
    try:
        return {"state": "paired" if task.result() else "expired"}
    except Exception:  # task cancelled/errored -> treat as expired
        return {"state": "expired"}


@router.delete("/api/devices/{device_id}")
def delete_device(request: Request, device_id: str) -> dict[str, bool]:
    """Revoke a device so it can no longer connect."""
    devices.revoke_device(_store(request), device_id)
    return {"ok": True}
