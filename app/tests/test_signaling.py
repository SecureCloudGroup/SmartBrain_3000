"""Tests for the signaling broker + the Desktop signaling loop — Phase 3b.

The broker lives at the repo root (``signaling/server.py``) so the VPS image stays
tiny (websockets only); these tests import it via the repo root and skip cleanly if
it isn't mounted. The full-loop test wires broker + run_signaling + a real aiortc
"phone" + the auth-on-channel peer, proving the whole local path end to end.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
from collections.abc import Iterator

import duckdb
import pytest
import websockets
from fastapi.testclient import TestClient

from smartbrain_3000 import devices, webrtc_signaling
from smartbrain_3000.secrets import SecretStore, gen_master_key

_SIGNALING_DIR = pathlib.Path(__file__).resolve().parents[2] / "signaling"
if _SIGNALING_DIR.is_dir():
    sys.path.insert(0, str(_SIGNALING_DIR))
# These 9 tests exercise the production WebRTC broker. In the app-only container `signaling/`
# isn't mounted, so they skip — which silently hid them from "1 skipped". A release-gate run
# sets SMARTBRAIN_REQUIRE_SIGNALING_TESTS=1 (with server.py on PYTHONPATH) so a missing broker
# FAILS LOUDLY instead of skipping. See app/tests/README for how to run them.
if os.environ.get("SMARTBRAIN_REQUIRE_SIGNALING_TESTS") and importlib.util.find_spec("server") is None:
    raise RuntimeError(
        "SMARTBRAIN_REQUIRE_SIGNALING_TESTS is set but the signaling broker (server.py) is not "
        "importable — put signaling/ on PYTHONPATH so these tests actually run."
    )
broker_mod = pytest.importorskip("server", reason="signaling/ not mounted")


async def _serve_broker(token: str = "secret", **kwargs):
    """Start the broker on an ephemeral loopback port; return (server, ws_url, broker)."""
    broker = broker_mod.Broker(token, **kwargs)
    server = await websockets.serve(broker.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}", broker


def test_broker_relays_and_authorizes() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker("secret")
        try:
            async with websockets.connect(url) as bad:  # wrong token -> rejected
                await bad.send(json.dumps({"role": "desktop", "desktop_id": "d1", "token": "nope"}))
                assert json.loads(await asyncio.wait_for(bad.recv(), 5))["type"] == "error"

            async with websockets.connect(url) as desk:
                await desk.send(json.dumps({"role": "desktop", "desktop_id": "d1", "token": "secret"}))
                assert json.loads(await asyncio.wait_for(desk.recv(), 5))["type"] == "registered"
                async with websockets.connect(url) as phone:
                    await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                    await phone.send(json.dumps({"type": "offer", "sdp": "SDP-OFFER"}))
                    relayed = json.loads(await asyncio.wait_for(desk.recv(), 5))
                    assert relayed["type"] == "offer" and relayed["sdp"] == "SDP-OFFER"
                    assert relayed["from"].startswith("phone:")
                    await desk.send(json.dumps({"type": "answer", "to": relayed["from"], "sdp": "SDP-ANSWER"}))
                    ans = json.loads(await asyncio.wait_for(phone.recv(), 5))
                    assert ans["type"] == "answer" and ans["sdp"] == "SDP-ANSWER"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_broker_reports_desktop_offline() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker("secret")
        try:
            async with websockets.connect(url) as phone:
                await phone.send(json.dumps({"role": "phone", "desktop_id": "absent"}))
                await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                msg = json.loads(await asyncio.wait_for(phone.recv(), 5))
                assert msg["type"] == "error" and "offline" in msg["detail"]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_broker_rejects_when_token_unset() -> None:
    # Fail-closed: with no configured token, no desktop can register (no open broker).
    async def run() -> None:
        broker = broker_mod.Broker("")
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as desk:
                await desk.send(json.dumps({"role": "desktop", "desktop_id": "d1", "token": "anything"}))
                assert json.loads(await asyncio.wait_for(desk.recv(), 5))["type"] == "error"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.fixture()
def app_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        yield client


def test_full_loop_phone_to_app(app_client: TestClient) -> None:
    """broker + run_signaling + aiortc phone + auth-on-channel -> GET /api/health."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    dev = devices.create_device(store, "phone")

    async def run() -> dict:
        server, url, _ = await _serve_broker("secret")
        stop = asyncio.Event()
        desk = asyncio.create_task(webrtc_signaling.run_signaling(
            signaling_url=url, desktop_id="d1", token="secret",
            get_store=lambda: store, stop=stop, http_client=app_client,  # inject the app
        ))
        phone = RTCPeerConnection()
        channel = phone.createDataChannel("sb-api")
        loop = asyncio.get_event_loop()
        authed, reply = loop.create_future(), loop.create_future()

        @channel.on("open")
        def _open() -> None:
            channel.send(json.dumps({"type": "auth", "device_id": dev["device_id"], "credential": dev["credential"]}))

        @channel.on("message")
        def _msg(data) -> None:
            m = json.loads(data)
            if m.get("type") == "auth_ok" and not authed.done():
                authed.set_result(True)
                channel.send(json.dumps({"id": "1", "method": "GET", "path": "/api/health", "headers": {}, "body_b64": ""}))
            elif "status" in m and not reply.done():
                reply.set_result(m)

        try:
            async with websockets.connect(url) as pws:
                await pws.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await phone.setLocalDescription(await phone.createOffer())
                await pws.send(json.dumps({"type": "offer", "sdp": phone.localDescription.sdp}))
                ans = json.loads(await asyncio.wait_for(pws.recv(), 20))
                assert ans["type"] == "answer"
                await phone.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type="answer"))
                await asyncio.wait_for(authed, 20)
                return await asyncio.wait_for(reply, 20)
        finally:
            stop.set()
            desk.cancel()
            await phone.close()
            server.close()
            await server.wait_closed()

    resp = asyncio.run(run())
    assert resp["status"] == 200
    import base64
    assert b'"status":"ok"' in base64.b64decode(resp["body_b64"])


async def _open_phone(url: str, desktop_id: str):
    """Connect a phone, send its hello, and wait until the broker has admitted it.

    The broker emits no message on phone admit-success, so we send a dummy offer with
    no desktop registered: the resulting ``desktop offline`` reply both confirms the
    admit AND proves the phone is in ``_phone_loop`` (per-desktop count incremented).
    """
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"role": "phone", "desktop_id": desktop_id}))
    await ws.send(json.dumps({"type": "offer", "sdp": "probe"}))
    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert msg["type"] == "error" and "offline" in msg["detail"], "expected offline ack"
    return ws


async def _is_busy_reject(ws, detail: str) -> bool:
    """True iff the broker's first message is an error with the expected detail."""
    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
    return msg.get("type") == "error" and msg.get("detail") == detail


def test_per_desktop_phone_cap_rejects_overflow() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker(
            "secret", max_phones_per_desktop=2, max_phones=64, rate_limit=999,
        )
        try:
            held = [await _open_phone(url, "d1") for _ in range(2)]
            try:
                async with websockets.connect(url) as extra:
                    await extra.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                    assert await _is_busy_reject(extra, "busy"), "third phone must be rejected"
                # A different desktop_id is still admitted (per-desktop cap is per-id, not global).
                other = await _open_phone(url, "d2")
                await other.close()
            finally:
                for ws in held:
                    await ws.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_global_phone_cap_rejects_overflow() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker(
            "secret", max_phones=2, max_phones_per_desktop=64, rate_limit=999,
        )
        try:
            # Spread across desktop_ids so per-desktop cap can't be what rejects.
            a = await _open_phone(url, "d1")
            b = await _open_phone(url, "d2")
            try:
                async with websockets.connect(url) as extra:
                    await extra.send(json.dumps({"role": "phone", "desktop_id": "d3"}))
                    assert await _is_busy_reject(extra, "busy"), "third phone must be rejected"
            finally:
                await a.close()
                await b.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_phone_rate_limit_rejects_burst() -> None:
    async def run() -> None:
        # Concurrent cap high so rate-limit fires first; window large so timestamps don't age out.
        server, url, broker = await _serve_broker(
            "secret", max_phones=64, max_phones_per_desktop=64,
            rate_limit=3, rate_window_secs=60.0,
        )
        try:
            # Connect-and-close 3 phones — concurrent count returns to 0 each time, but the
            # rate-limit bucket accumulates timestamps that don't age out within the window.
            # _open_phone waits for an "offline" ack so each admit is guaranteed processed.
            for _ in range(3):
                ws = await _open_phone(url, "d1")
                await ws.close()
            # Wait (bounded) for each disconnect's finally to release the per-desktop slot.
            for _ in range(50):
                if broker._phones_per_desktop.get("d1", 0) == 0:
                    break
                await asyncio.sleep(0.02)
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                assert await _is_busy_reject(ws, "rate_limited"), "4th rapid connect must rate-limit"
            # A different desktop_id has its own bucket and is unaffected (gets the offline ack).
            other = await _open_phone(url, "d2")
            await other.close()
            assert broker._phones_per_desktop.get("d1", 0) == 0, "concurrent count must drop"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_phone_disconnect_releases_per_desktop_slot() -> None:
    async def run() -> None:
        server, url, broker = await _serve_broker(
            "secret", max_phones=64, max_phones_per_desktop=2, rate_limit=999,
        )
        try:
            held = [await _open_phone(url, "d1") for _ in range(2)]
            # Third is rejected while both slots are held.
            async with websockets.connect(url) as extra:
                await extra.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                assert await _is_busy_reject(extra, "busy"), "slots full -> busy"
            # Free one slot; the broker must observe the disconnect before retrying.
            await held[0].close()
            for _ in range(50):  # bounded wait for the server-side finally to run
                if broker._phones_per_desktop.get("d1", 0) <= 1:
                    break
                await asyncio.sleep(0.02)
            assert broker._phones_per_desktop.get("d1", 0) <= 1, "slot must release on disconnect"
            # A fresh connection now succeeds (no error frame on admit).
            async with websockets.connect(url) as fresh:
                await fresh.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await fresh.send(json.dumps({"type": "offer", "sdp": "x"}))
                msg = json.loads(await asyncio.wait_for(fresh.recv(), 5))
                # Desktop is offline (we never registered one), so we expect "desktop offline",
                # NOT "busy"/"rate_limited" — proving admit succeeded.
                assert msg["type"] == "error" and "offline" in msg["detail"]
            await held[1].close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_broker_caps_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SIGNALING_MAX_PHONES", "7")
    monkeypatch.setenv("SIGNALING_MAX_PHONES_PER_DESKTOP", "3")
    monkeypatch.setenv("SIGNALING_PHONE_RATE_LIMIT", "5")
    monkeypatch.setenv("SIGNALING_PHONE_RATE_WINDOW_SECS", "11.5")
    broker = broker_mod._broker_from_env("secret", [])
    assert broker._max_phones == 7
    assert broker._max_phones_per_desktop == 3
    assert broker._rate_limit == 5
    assert broker._rate_window == 11.5
