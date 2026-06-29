"""Desktop pairing-by-code host — serves the PairingPayload to the installed PWA.

See :mod:`pairing_code` for the trust model. The Desktop opens a SECOND, temporary
outbound WSS to the broker under a code-derived room id, answers the app's WebRTC offer,
proves mutual knowledge of the code bound to the DTLS channel (HMAC over the channel
binding), then sends the pairing payload inside the encrypted channel. One success ends the
session; it also self-terminates on a 5-minute expiry, after too many failed attempts, or
when ``stop`` is set. aiortc/websockets are imported lazily (only when a session runs).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

from . import pairing_code, remote_config, webrtc_peer

log = logging.getLogger(__name__)

_PAIR_CHANNEL = "sb-pair"
_MAX_MSG = 256 * 1024
_MAX_CHANNEL_MSG = 64 * 1024  # the pairing payload is a couple KB; cap inbound + outbound
_NONCE_BYTES = 16
_MAX_GUESSES = 8


def _b64(b: bytes) -> str:
    assert isinstance(b, bytes), "b64 takes bytes"
    return base64.b64encode(b).decode("ascii")


def _handle_phello(pc, channel, code_key: bytes, session: dict, msg: dict) -> None:
    """Reply to the app's challenge: prove we know the code, bound to THIS channel."""
    nonce = base64.b64decode(str(msg.get("nonce") or ""))
    assert 0 < len(nonce) <= _NONCE_BYTES, "challenge nonce must be small + non-empty"
    session["binding"] = webrtc_peer.channel_binding(pc)
    session["nonce2"] = os.urandom(_NONCE_BYTES)
    mac_h = pairing_code.mac(code_key, "host", nonce, session["binding"])
    _send(channel, {"type": "phello_ok", "mac": _b64(mac_h), "nonce2": _b64(session["nonce2"])})


def _handle_pconfirm(channel, code_key: bytes, payload: dict, session: dict, state: dict, msg: dict) -> None:
    """Verify the app proved the code; on success send the payload, else count a wrong guess."""
    got = base64.b64decode(str(msg.get("mac") or ""))
    expect = pairing_code.mac(code_key, "guest", session["nonce2"], session["binding"])
    if session["nonce2"] and pairing_code.mac_equal(got, expect):
        _send(channel, {"type": "ppayload", "payload": json.dumps(payload)})
        state["ok"] = True
        state["done"].set()
        return
    state["guesses"] += 1
    _send(channel, {"type": "perror", "detail": "incorrect code"})
    if state["guesses"] >= _MAX_GUESSES:
        state["done"].set()


def _wire_pairing_channel(pc, channel, code_key: bytes, payload: dict, state: dict) -> None:
    """Attach the code-auth -> payload handshake to the pairing DataChannel."""
    assert channel is not None, "channel required"
    session: dict = {"nonce2": b"", "binding": b""}

    @channel.on("message")
    def _on_message(message) -> None:
        if state["done"].is_set():
            return
        text = message if isinstance(message, str) else bytes(message).decode("utf-8", "replace")
        if len(text) > _MAX_CHANNEL_MSG:
            return
        try:
            msg = json.loads(text)
            assert isinstance(msg, dict), "message must be a JSON object"
        except Exception:
            return
        try:
            if msg.get("type") == "phello":
                _handle_phello(pc, channel, code_key, session, msg)
            elif msg.get("type") == "pconfirm":
                _handle_pconfirm(channel, code_key, payload, session, state, msg)
        except Exception as exc:  # one bad message must not crash the host
            log.warning("pair: handler failed: %s", type(exc).__name__)


async def _answer(offer_sdp: str, ice_servers, code_key: bytes, payload: dict, state: dict):
    """Create a peer for the app's pairing offer; return ``(pc, answer_sdp)``."""
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    assert offer_sdp, "offer sdp required"
    servers = [RTCIceServer(**s) for s in (ice_servers or [])]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=servers))

    @pc.on("datachannel")
    def _on_dc(channel) -> None:
        if channel.label == _PAIR_CHANNEL:  # ignore any other channel
            _wire_pairing_channel(pc, channel, code_key, payload, state)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
    await pc.setLocalDescription(await pc.createAnswer())
    assert pc.localDescription is not None, "answer description must be set"
    return pc, webrtc_peer._single_fingerprint(pc.localDescription.sdp)


def _send(channel, obj: dict) -> None:
    try:
        channel.send(json.dumps(obj))
    except Exception as exc:  # channel closing — nothing to recover
        log.warning("pair: send failed: %s", type(exc).__name__)


async def run_pairing_host(
    *, signaling_url: str, token: str, code: str, payload: dict, stop=None, ice_servers=None, expiry_s: int = 300
) -> bool:
    """Host one pairing session for ``code``, serving ``payload``. Returns True if a device
    paired, else False (expired / too many wrong attempts / stopped / link error)."""
    import websockets  # lazy: only when a session runs

    assert signaling_url and code, "signaling url + code required"  # token empty in hosted (tokenless) mode
    assert isinstance(payload, dict), "payload must be a dict (PairingPayload)"
    room_id, code_key = pairing_code.derive(code)
    state: dict = {"done": asyncio.Event(), "ok": False, "guesses": 0}
    peers: list = []

    async def _deadline() -> None:
        await asyncio.sleep(expiry_s)
        state["done"].set()

    async def _await_stop() -> None:
        if stop is not None:
            await stop.wait()
            state["done"].set()

    async def _close_ws_on_done(ws) -> None:
        await state["done"].wait()
        try:
            await ws.close()
        except Exception:  # already closing
            pass

    timers = [asyncio.ensure_future(_deadline()), asyncio.ensure_future(_await_stop())]
    try:
        async with websockets.connect(signaling_url, max_size=_MAX_MSG) as ws:
            await ws.send(json.dumps({"role": "desktop", "desktop_id": room_id, "token": token}))
            closer = asyncio.ensure_future(_close_ws_on_done(ws))
            # Broker-pushed ephemeral ICE (STUN/TURN, fresh creds) overrides the static ice_servers
            # param — without it the pairing peer has no relay candidate and never connects.
            node_ice = None
            try:
                async for raw in ws:
                    if state["done"].is_set():
                        break
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "ice":
                        node_ice = msg.get("iceServers")
                        continue
                    if mtype != "offer":
                        continue
                    # Reorder pushed ICE by live UDP reachability (TCP TURN first when UDP is
                    # blocked) so the relay works from Docker / UDP-blocking networks.
                    ice = (remote_config.adapt_pushed_ice(node_ice)
                           if node_ice is not None else ice_servers)
                    pc, answer_sdp = await _answer(str(msg.get("sdp") or ""), ice, code_key, payload, state)
                    peers.append(pc)
                    await ws.send(json.dumps({"type": "answer", "to": msg.get("from"), "sdp": answer_sdp}))
            finally:
                closer.cancel()
    except Exception as exc:  # any disconnect — the session just ends (caller can restart)
        log.warning("pair: host link error: %s", type(exc).__name__)
    finally:
        for t in timers:
            t.cancel()
        if state["ok"]:
            await asyncio.sleep(1.0)  # let the payload flush before tearing the channel down
        for pc in peers:
            await pc.close()
    return state["ok"]
