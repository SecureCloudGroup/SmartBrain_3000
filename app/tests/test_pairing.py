"""Pairing-by-code: crypto determinism + the host's code-auth -> payload handshake.

The handshake is driven directly against pairing_host._answer with a real aiortc "phone"
peer (no broker needed), mirroring test_webrtc_peer.py.
"""

from __future__ import annotations

import asyncio
import base64
import json

from smartbrain_3000 import pairing_code, pairing_host


def test_derive_deterministic_and_independent() -> None:
    r1, k1 = pairing_code.derive("ABC234")
    r2, k2 = pairing_code.derive("abc 234")  # normalize: uppercases + drops the space
    assert r1 == r2 and k1 == k2, "same code (normalized) -> same room + key"
    assert r1.startswith("sbpair-") and len(k1) == 32
    r3, k3 = pairing_code.derive("ABC235")
    assert r3 != r1 and k3 != k1, "a different code -> different room AND key"


def test_mac_label_and_equality() -> None:
    _, key = pairing_code.derive("ABC234")
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

    code = "ABC234"
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

    _, key = pairing_code.derive("ABC234")  # the host's key
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
