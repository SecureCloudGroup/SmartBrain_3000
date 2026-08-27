"""Pairing-by-code: crypto determinism + the host's code-auth -> payload handshake.

The handshake is driven directly against pairing_host._answer with a real aiortc "phone"
peer (no broker needed), mirroring test_webrtc_peer.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib

import pytest

from smartbrain_3000 import pairing_code, pairing_host


def test_derive_deterministic_and_independent() -> None:
    r1, k1 = pairing_code.derive("ABCDEFGH")
    r2, k2 = pairing_code.derive("abcd efgh")  # normalize: uppercases + drops the space
    assert r1 == r2 and k1 == k2, "same code (normalized) -> same room + key"
    assert pairing_code.derive("ABCD-EFGH") == (r1, k1), "the display dash is accepted"
    assert r1.startswith("sbpair-") and len(k1) == 32
    r3, k3 = pairing_code.derive("ABCDEFGJ")
    assert r3 != r1 and k3 != k1, "a different code -> different room AND key"


def test_vectors_match_paircode_ts() -> None:
    """The reference vectors pinned in web/src/lib/remote/paircode.test.ts (same code)."""
    room, key = pairing_code.derive("ABCD-EFGH")
    assert room == "sbpair-566b249c880595cb2fa34a3f97a2a30c"
    assert pairing_code.mac(key, "host", bytes(16), bytes(32)).hex() == (
        "52d3ce8ee6f6a33154c1e41e20ff76866fe89315eef61f2c1687fd003a0bb4cb"
    )


def test_code_is_8_chars_and_shorter_is_refused() -> None:
    code = pairing_code.generate_code()
    assert len(code) == 8 and all(c in pairing_code._ALPHABET for c in code)
    for bad in ("ABC234", "ABCDEFGHJ", ""):
        try:
            pairing_code.derive(bad)
        except AssertionError:
            continue
        raise AssertionError(f"{bad!r} must be refused")


def test_mac_label_and_equality() -> None:
    _, key = pairing_code.derive("ABCDEFGH")
    nonce, binding = b"n" * 16, b"b" * 32
    m = pairing_code.mac(key, "host", nonce, binding)
    assert pairing_code.mac_equal(m, pairing_code.mac(key, "host", nonce, binding))
    assert not pairing_code.mac_equal(m, pairing_code.mac(key, "guest", nonce, binding)), "label is bound"


def _payload() -> dict:
    return {
        "v": 1, "deviceId": "dev", "credential": "cred", "desktopPubkey": "pk",
        "signalingUrl": "wss://x/signal", "desktopId": "desk", "iceServers": [],
    }


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def test_e2e_correct_code_receives_payload() -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    code = "ABCDEFGH"
    _, key = pairing_code.derive(code)
    payload = _payload()

    async def run() -> dict:
        phone = RTCPeerConnection()
        channel = phone.createDataChannel("sb-pair")
        loop = asyncio.get_event_loop()
        got = loop.create_future()
        nonce = b"sixteen-byte-non"
        state = {"done": asyncio.Event(), "ok": False, "guesses": 0}

        @channel.on("open")
        def _open() -> None:
            channel.send(json.dumps({"type": "phello", "nonce": _b64(nonce)}))

        @channel.on("message")
        def _msg(data) -> None:
            m = json.loads(data)
            if m.get("type") == "phello_ok":
                binding = pairing_host.webrtc_peer.channel_binding(phone)  # phone computes the same binding
                assert pairing_code.mac_equal(base64.b64decode(m["mac"]), pairing_code.mac(key, "host", nonce, binding))
                mac_g = pairing_code.mac(key, "guest", base64.b64decode(m["nonce2"]), binding)
                channel.send(json.dumps({"type": "pconfirm", "mac": _b64(mac_g)}))
            elif m.get("type") == "ppayload" and not got.done():
                got.set_result(json.loads(m["payload"]))

        await phone.setLocalDescription(await phone.createOffer())
        pc, answer = await pairing_host._answer(phone.localDescription.sdp, None, key, payload, state)
        await phone.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        try:
            return await asyncio.wait_for(got, timeout=20)
        finally:
            await phone.close()
            await pc.close()

    assert asyncio.run(run()) == payload


def test_e2e_wrong_code_is_rejected() -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    _, key = pairing_code.derive("ABCDEFGH")  # the host's key
    state = {"done": asyncio.Event(), "ok": False, "guesses": 0}

    async def run() -> str:
        phone = RTCPeerConnection()
        channel = phone.createDataChannel("sb-pair")
        loop = asyncio.get_event_loop()
        result = loop.create_future()

        @channel.on("open")
        def _open() -> None:
            channel.send(json.dumps({"type": "phello", "nonce": _b64(b"n" * 16)}))

        @channel.on("message")
        def _msg(data) -> None:
            m = json.loads(data)
            if m.get("type") == "phello_ok":  # present a bogus proof (wrong code)
                channel.send(json.dumps({"type": "pconfirm", "mac": _b64(b"\x00" * 32)}))
            elif m.get("type") in ("perror", "ppayload") and not result.done():
                result.set_result(m.get("type"))

        await phone.setLocalDescription(await phone.createOffer())
        pc, answer = await pairing_host._answer(phone.localDescription.sdp, None, key, _payload(), state)
        await phone.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        try:
            return await asyncio.wait_for(result, timeout=20)
        finally:
            await phone.close()
            await pc.close()

    assert asyncio.run(run()) == "perror"
    assert state["ok"] is False and state["guesses"] == 1


def test_pairing_host_bounds_peers_and_signs_registration(tmp_path) -> None:
    """Against a real broker: the host signs its sbpair-* registration with the routing key,
    answers at most _MAX_PEERS live offers, and drops the 9th (bounded like run_signaling)."""
    import sys

    import duckdb
    import websockets

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "signaling"))
    broker_mod = pytest.importorskip("server", reason="signaling/ not mounted")
    from smartbrain_3000 import db, routing_key

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    db.meta_set(conn, "desktop_routing_key", routing_key.generate_private_key_b64())

    class _FakePc:
        connectionState = "connecting"

        def on(self, _event):
            return lambda fn: fn

        async def close(self) -> None:
            self.connectionState = "closed"

    async def fake_answer(offer_sdp, ice, code_key, payload, state):
        return _FakePc(), "ANSWER:" + offer_sdp

    async def run() -> int:
        broker = broker_mod.Broker("", open_mode=True, max_phones_per_desktop=64, offer_limit=64)
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        code = pairing_code.generate_code()
        room_id, _ = pairing_code.derive(code)
        stop = asyncio.Event()
        orig = pairing_host._answer
        pairing_host._answer = fake_answer
        host = asyncio.create_task(pairing_host.run_pairing_host(
            signaling_url=url, token="", code=code, payload=_payload(), stop=stop, conn=conn))
        try:
            for _ in range(100):  # wait for the (signed) registration to land
                if room_id in broker._desktops:
                    break
                await asyncio.sleep(0.02)
            assert room_id in broker._desktops, "pairing room must register via the proof"
            answered = 0
            phones = []
            for i in range(pairing_host._MAX_PEERS + 1):
                ws = await websockets.connect(url)
                phones.append(ws)
                await ws.send(json.dumps({"role": "phone", "desktop_id": room_id}))
                await ws.send(json.dumps({"type": "offer", "sdp": f"o{i}"}))
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    answered += msg.get("type") == "answer"
                except TimeoutError:
                    pass  # dropped by the cap
            for ws in phones:
                await ws.close()
            return answered
        finally:
            pairing_host._answer = orig
            stop.set()
            host.cancel()
            server.close()
            await server.wait_closed()

    assert asyncio.run(run()) == pairing_host._MAX_PEERS
