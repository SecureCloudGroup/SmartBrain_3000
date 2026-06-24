"""Headless end-to-end test for the Desktop WebRTC peer (webrtc_peer.py) — Phase 3.

A real aiortc "phone" peer negotiates with the Desktop peer over manual signaling
(no broker needed — aiortc is non-trickle), opens the sb-api DataChannel,
authenticates over the encrypted channel (credential never in the offer), then
round-trips framed HTTP requests proxied to a live app via a TestClient.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import devices, identity, webrtc_peer
from smartbrain_3000.secrets import SecretStore, gen_master_key


@pytest.fixture()
def app_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        yield client


def _req(rid: str, method: str, path: str, body: bytes = b"") -> dict:
    return {"id": rid, "method": method, "path": path, "headers": {}, "body_b64": base64.b64encode(body).decode()}


async def _connect_and_auth(store, http_client, device_id, credential, timeout=20.0):
    """Open a peer + channel and send the auth frame; return (phone, pc, channel, events)."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    phone = RTCPeerConnection()
    channel = phone.createDataChannel("sb-api")
    events: dict = {"messages": [], "auth": asyncio.get_event_loop().create_future()}

    @channel.on("open")
    def _open() -> None:
        channel.send(json.dumps({"type": "auth", "device_id": device_id, "credential": credential}))

    @channel.on("message")
    def _msg(data) -> None:
        m = json.loads(data)
        events["messages"].append(m)
        if m.get("type") in ("auth_ok", "auth_error") and not events["auth"].done():
            events["auth"].set_result(m)

    await phone.setLocalDescription(await phone.createOffer())
    pc, answer_sdp = await webrtc_peer.answer_offer(phone.localDescription.sdp, store=store, http_client=http_client)
    await phone.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
    auth = await asyncio.wait_for(events["auth"], timeout=timeout)
    return phone, pc, channel, events, auth


def test_e2e_auth_then_health(app_client: TestClient) -> None:
    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    dev = devices.create_device(store, "phone")

    async def run() -> dict:
        phone, pc, channel, events, auth = await _connect_and_auth(
            store, app_client, dev["device_id"], dev["credential"]
        )
        assert auth["type"] == "auth_ok"
        reply: asyncio.Future = asyncio.get_event_loop().create_future()

        @channel.on("message")
        def _resp(data) -> None:
            m = json.loads(data)
            if "status" in m and not reply.done():
                reply.set_result(m)

        channel.send(json.dumps(_req("1", "GET", "/api/health")))
        try:
            return await asyncio.wait_for(reply, timeout=20)
        finally:
            await phone.close()
            await pc.close()

    resp = asyncio.run(run())
    assert resp["status"] == 200 and b'"status":"ok"' in base64.b64decode(resp["body_b64"])


def test_e2e_bad_credential_is_rejected(app_client: TestClient) -> None:
    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    devices.create_device(store, "phone")  # a real device exists; we present a bogus credential

    async def run() -> dict:
        phone, pc, channel, events, auth = await _connect_and_auth(store, app_client, "nope", "wrong")
        try:
            return auth
        finally:
            await phone.close()
            await pc.close()

    auth = asyncio.run(run())
    assert auth["type"] == "auth_error"


def test_e2e_revoked_device_is_cut_off(app_client: TestClient) -> None:
    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    dev = devices.create_device(store, "phone")

    async def run() -> dict:
        phone, pc, channel, events, auth = await _connect_and_auth(
            store, app_client, dev["device_id"], dev["credential"]
        )
        assert auth["type"] == "auth_ok"
        reply: asyncio.Future = asyncio.get_event_loop().create_future()

        @channel.on("message")
        def _resp(data) -> None:
            m = json.loads(data)
            if "status" in m and not reply.done():
                reply.set_result(m)

        devices.revoke_device(store, dev["device_id"])  # revoke AFTER auth, mid-session
        channel.send(json.dumps(_req("9", "GET", "/api/health")))
        try:
            return await asyncio.wait_for(reply, timeout=20)
        finally:
            await phone.close()
            await pc.close()

    assert asyncio.run(run())["status"] == 401  # request refused once the device is revoked


def test_e2e_allowlist_blocks_non_api(app_client: TestClient) -> None:
    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    dev = devices.create_device(store, "phone")

    async def run() -> dict:
        phone, pc, channel, events, auth = await _connect_and_auth(
            store, app_client, dev["device_id"], dev["credential"]
        )
        assert auth["type"] == "auth_ok"
        reply: asyncio.Future = asyncio.get_event_loop().create_future()

        @channel.on("message")
        def _resp(data) -> None:
            m = json.loads(data)
            if "status" in m and not reply.done():
                reply.set_result(m)

        channel.send(json.dumps(_req("2", "GET", "/mcp/x")))  # outside /api -> bridge refuses
        try:
            return await asyncio.wait_for(reply, timeout=20)
        finally:
            await phone.close()
            await pc.close()

    assert asyncio.run(run())["status"] == 400


def test_e2e_channel_auth_then_request(app_client: TestClient) -> None:
    """Phone challenges the Desktop, verifies the signature is bound to THIS channel
    against the pinned pubkey, THEN authenticates and makes a request."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    dev = devices.create_device(store, "phone")
    pinned_pubkey = identity.public_key_b64(store)  # what the phone would pin at pairing
    nonce = base64.b64encode(b"sixteen-byte-non").decode()

    async def run() -> dict:
        phone = RTCPeerConnection()
        channel = phone.createDataChannel("sb-api")
        loop = asyncio.get_event_loop()
        hello_ok, reply = loop.create_future(), loop.create_future()

        @channel.on("open")
        def _open() -> None:
            channel.send(json.dumps({"type": "hello", "nonce": nonce}))

        @channel.on("message")
        def _msg(data) -> None:
            m = json.loads(data)
            if m.get("type") == "hello_ok" and not hello_ok.done():
                hello_ok.set_result(m)
            elif m.get("type") == "auth_ok":
                channel.send(json.dumps(_req("1", "GET", "/api/health")))
            elif "status" in m and not reply.done():
                reply.set_result(m)

        await phone.setLocalDescription(await phone.createOffer())
        pc, answer = await webrtc_peer.answer_offer(phone.localDescription.sdp, store=store, http_client=app_client)
        await phone.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        try:
            ho = await asyncio.wait_for(hello_ok, timeout=20)
            binding = webrtc_peer.channel_binding(phone)  # phone computes the same binding (direct conn)
            assert ho["pubkey"] == pinned_pubkey, "Desktop pubkey must match the pinned key"
            assert identity.verify(ho["pubkey"], base64.b64decode(nonce) + binding, ho["signature"])
            # relay-resistance: the same signature must NOT verify for a different binding
            assert not identity.verify(ho["pubkey"], base64.b64decode(nonce) + b"other-binding", ho["signature"])
            channel.send(json.dumps(
                {"type": "auth", "device_id": dev["device_id"], "credential": dev["credential"]}))
            return await asyncio.wait_for(reply, timeout=20)
        finally:
            await phone.close()
            await pc.close()

    assert asyncio.run(run())["status"] == 200


def test_encode_response_is_binary_safe() -> None:
    out = webrtc_peer._encode_response({"id": "9", "status": 201, "headers": {}, "body": b"\x00\xff hi"})
    parsed = json.loads(out)
    assert parsed["status"] == 201 and base64.b64decode(parsed["body_b64"]) == b"\x00\xff hi"


def test_single_fingerprint_keeps_only_the_first() -> None:
    # aiortc advertises the cert with several hash algos; a browser keeps only one (and
    # may reorder), so the two peers bound DIFFERENT fingerprints of the same cert and
    # channel-auth failed ("untrusted"). Advertising one fingerprint makes both agree.
    sdp = (
        "v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        "a=fingerprint:sha-256 AA:BB:CC\r\na=fingerprint:sha-512 DD:EE:FF\r\na=setup:active\r\n"
    )
    out = webrtc_peer._single_fingerprint(sdp)
    assert [ln for ln in out.splitlines() if ln.startswith("a=fingerprint:")] == ["a=fingerprint:sha-256 AA:BB:CC"]
    assert "a=setup:active" in out and out.startswith("v=0")  # all other lines preserved
