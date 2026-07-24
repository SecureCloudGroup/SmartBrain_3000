"""Desktop WebRTC peer — Phase 3/4.

A paired phone (browser PWA, no app install) opens a WebRTC connection to this
Desktop through a self-hosted signaling broker; the DTLS-encrypted DataChannel
carries framed HTTP requests that :func:`webrtc_bridge.handle_frame` proxies to
the app's own loopback. The data plane is end-to-end encrypted between phone and
Desktop, so a relay (e.g. TURN) only ever carries ciphertext.

aiortc gathers ICE candidates into the SDP (non-trickle), so signaling is a single
offer -> answer exchange. aiortc is imported lazily (only when a peer is actually
created), so app startup and the test base are unaffected while remote access is off.

Handshake over the channel (in order):
  1. CHANNEL AUTH (defeats a MITM broker): the phone sends {"type":"hello","nonce"};
     the Desktop replies {"type":"hello_ok","pubkey","signature"} where signature =
     Ed25519(nonce || channel_binding). channel_binding is derived from the DTLS
     fingerprints of THIS peer connection, so a relaying broker (different DTLS legs)
     cannot forward a valid signature. The phone verifies against the pubkey it pinned
     at pairing BEFORE sending its credential.
  2. DEVICE AUTH: {"type":"auth","device_id","credential"} -> {"type":"auth_ok"} /
     {"type":"auth_error"}. The credential is sent only inside DTLS, never to the broker.
  3. REQUESTS: {"id","method","path","headers","body_b64"} -> {"id","status","headers",
     "body_b64"} (bodies base64 so binary uploads/backups are safe). The device is
     re-checked every request, so a revocation cuts the session off immediately.
     A request carrying {"chunks": true} may get its response as ORDERED part-frames
     {"id","seq","more","body_b64"} (first part also has status+headers) when one
     message can't hold it; without the flag an oversized response stays a 413.
  4. KEEPALIVE (authed only): {"type":"ping","t"} -> {"type":"pong","t"}. The phone pings
     on a timer so idle NAT/consent mappings stay warm and a dead path is noticed without
     user traffic. Before auth, a ping is just an invalid auth message (rejected, closed).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging

from . import devices, identity, webrtc_bridge

log = logging.getLogger(__name__)

_API_CHANNEL = "sb-api"
_MAX_MESSAGE_BYTES = 256 * 1024  # cap on a single DataChannel message, BOTH directions
_MAX_INFLIGHT = 16               # bound concurrent in-flight requests per channel
_MAX_NONCE_BYTES = 64            # a peer's hello nonce is small; never sign arbitrary-length data
# A response too big for one message is split into ordered part-frames when the request
# advertised {"chunks": true} (phones do since the audit feed outgrew one message). The
# ceiling bounds what one request may stream (memory on both ends); past it, 413 as ever.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CHUNK_B64_CHARS = 192 * 1024  # per-frame payload; envelope stays well under _MAX_MESSAGE_BYTES

# Strong refs to in-flight ``ensure_future`` tasks. CPython may GC a task whose
# only reference is the event loop's weakref set (issue 91887), silently
# cancelling mid-flight work. Tasks add themselves here on creation and remove
# themselves via ``add_done_callback`` on completion.
_pending_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> asyncio.Task:
    """Hold a strong ref to ``task`` until it completes (defeats GC of bg tasks)."""
    assert task is not None, "task required"
    assert isinstance(task, asyncio.Task), "asyncio.Task required"
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task


def _encode_response(resp: dict) -> str:
    """Serialize a handle_frame response frame into a DataChannel message."""
    assert isinstance(resp, dict) and "id" in resp, "response frame needs an id"
    return json.dumps({
        "id": resp["id"],
        "status": int(resp["status"]),
        "headers": resp.get("headers") or {},
        "body_b64": base64.b64encode(resp.get("body") or b"").decode("ascii"),
    })


def _error_response(rid: str, status: int, detail: str) -> str:
    """A JSON error response message (for 413/429/401 surfaced like any API error)."""
    body = json.dumps({"detail": detail}).encode("utf-8")
    return _encode_response({"id": rid or "?", "status": status, "headers": {}, "body": body})


def _encode_response_parts(resp: dict) -> list[str]:
    """Split one response into ordered part-frames a chunk-aware phone reassembles.

    Every part carries ``{id, seq, more, body_b64}``; the FIRST also carries ``status`` +
    ``headers``. The DataChannel is reliable + ordered, so ``seq`` is a cross-check, not a
    reordering mechanism. Kept beside ``_encode_response`` so the two wire shapes evolve
    together (mirrored in web/src/lib/remote/protocol.ts).
    """
    assert isinstance(resp, dict) and "id" in resp, "response frame needs an id"
    body_b64 = base64.b64encode(resp.get("body") or b"").decode("ascii")
    pieces = [body_b64[i : i + _CHUNK_B64_CHARS] for i in range(0, len(body_b64), _CHUNK_B64_CHARS)] or [""]
    out: list[str] = []
    for seq, piece in enumerate(pieces):  # bounded: len(body) <= _MAX_RESPONSE_BYTES
        frame: dict = {"id": resp["id"], "seq": seq, "more": seq < len(pieces) - 1, "body_b64": piece}
        if seq == 0:
            frame["status"] = int(resp["status"])
            frame["headers"] = resp.get("headers") or {}
        out.append(json.dumps(frame))
    return out


def _sdp_fingerprint(sdp: str) -> str:
    """Extract the DTLS ``a=fingerprint`` value (algo + hex), lower-cased, from an SDP.

    Lower-casing keeps the binding byte-identical across the Python Desktop and the
    JS phone, which may format the SDP fingerprint with different casing.
    """
    for line in (sdp or "").splitlines():
        if line.startswith("a=fingerprint:"):
            return line.split(":", 1)[1].strip().lower()
    return ""


def _single_fingerprint(sdp: str) -> str:
    """Drop all but the first ``a=fingerprint`` line from an SDP.

    aiortc advertises the DTLS cert with several hash algorithms (sha-256 AND
    sha-512). Browsers keep only one and may reorder them, so the two peers can
    hash DIFFERENT fingerprints of the SAME cert and the channel binding
    mismatches (the phone reports "untrusted"). Sending a single fingerprint
    makes every peer agree on the one value the Desktop binds to. DTLS is
    unaffected: the cert is still verified against the kept fingerprint.
    """
    out: list[str] = []
    kept = False
    for line in (sdp or "").splitlines(keepends=True):
        if line.startswith("a=fingerprint:"):
            if kept:
                continue
            kept = True
        out.append(line)
    return "".join(out)


def channel_binding(pc) -> bytes:
    """Bind to THIS DTLS session: sha256 of the connection's two fingerprints (sorted).

    Both peers compute the same value for a direct connection; a relaying MITM, whose
    two DTLS legs carry different fingerprints, computes a different value — which is
    what makes the signed handshake relay-resistant.
    """
    local = _sdp_fingerprint(pc.localDescription.sdp if pc.localDescription else "")
    remote = _sdp_fingerprint(pc.remoteDescription.sdp if pc.remoteDescription else "")
    assert local and remote, "channel binding needs both DTLS fingerprints"
    return hashlib.sha256("|".join(sorted([local, remote])).encode("ascii")).digest()


def _safe_send(channel, text: str) -> None:
    """Send on a channel, swallowing errors if it is closing (never crash the task)."""
    try:
        channel.send(text)
    except Exception as exc:  # channel closed/closing — nothing to recover
        log.warning("webrtc: send failed: %s", type(exc).__name__)


def _handle_channel_auth(channel, msg: dict, store, session: dict) -> None:
    """Channel-auth (hello) + device-auth path; reached only before ``session['authed']``."""
    assert isinstance(msg, dict), "message dict required"
    assert isinstance(session, dict) and "pc" in session, "session must carry the pc"
    mtype = msg.get("type")
    if mtype == "hello":  # prove Desktop identity, bound to this DTLS channel
        try:
            nonce = base64.b64decode(str(msg.get("nonce") or ""))
            if not nonce or len(nonce) > _MAX_NONCE_BYTES:
                return  # reject empty/oversized challenge — never sign arbitrary-length data
            sig = identity.sign(store, nonce + channel_binding(session["pc"]))
            _safe_send(channel, json.dumps(
                {"type": "hello_ok", "pubkey": identity.public_key_b64(store), "signature": sig}))
        except Exception as exc:
            log.warning("webrtc: hello failed: %s", type(exc).__name__)
        return
    device_id = str(msg.get("device_id") or "")
    if mtype == "auth" and devices.verify_device(store, device_id, str(msg.get("credential") or "")):
        session["authed"], session["device_id"] = True, device_id
        _safe_send(channel, json.dumps({"type": "auth_ok"}))
    else:  # reject + close the channel (credential is a 256-bit token, so brute force is moot)
        _safe_send(channel, json.dumps({"type": "auth_error"}))
        channel.close()


async def _handle_request(channel, msg: dict, http_client, store, session: dict) -> None:
    """Authed request path: revocation check + bounded in-flight + bridge dispatch."""
    assert isinstance(msg, dict), "message dict required"
    assert session.get("authed"), "_handle_request requires an authed session"
    rid = str(msg.get("id") or "?")
    if not devices.device_exists(store, session["device_id"]):  # revoked mid-session -> refuse
        _safe_send(channel, _error_response(rid, 401, "device revoked"))
        return
    if session["inflight"] >= _MAX_INFLIGHT:  # backpressure: bound concurrent requests
        _safe_send(channel, _error_response(rid, 429, "too many in-flight requests"))
        return

    session["inflight"] += 1
    try:
        frame = {
            "id": msg.get("id"),
            "method": msg.get("method"),
            "path": msg.get("path"),
            "headers": msg.get("headers") or {},
            "body": base64.b64decode(msg.get("body_b64") or ""),
        }
        resp = await asyncio.to_thread(webrtc_bridge.handle_frame, frame, http_client)
        out = _encode_response(resp)
        if len(out) > _MAX_MESSAGE_BYTES:
            body_len = len(resp.get("body") or b"")
            if msg.get("chunks") is True and body_len <= _MAX_RESPONSE_BYTES:
                # Chunk-aware phone: stream ordered parts instead of refusing. An old
                # phone never sets the flag and keeps getting the 413 below.
                for part in _encode_response_parts(resp):
                    _safe_send(channel, part)
                return
            out = _error_response(str(resp.get("id") or "?"), 413, "response exceeds channel limit")
        _safe_send(channel, out)
    except Exception as exc:  # a single bad request must never tear down the channel
        log.warning("webrtc: serve failed: %s", type(exc).__name__)
    finally:
        session["inflight"] -= 1


async def _serve_message(channel, raw_text: str, http_client, store, session: dict) -> None:
    """Process one DataChannel message: route to channel-auth or request path."""
    try:
        msg = json.loads(raw_text)
        assert isinstance(msg, dict), "message must be a JSON object"
    except Exception as exc:  # undecodable -> ignore
        log.warning("webrtc: undecodable message: %s", type(exc).__name__)
        return
    if not session["authed"]:
        _handle_channel_auth(channel, msg, store, session)
        return
    if msg.get("type") == "ping":  # keepalive (authed only): echo so the phone knows the
        _safe_send(channel, json.dumps({"type": "pong", "t": msg.get("t")}))  # path is alive
        return
    await _handle_request(channel, msg, http_client, store, session)


def _wire_channel(pc, channel, http_client, store) -> None:
    """Attach the auth-gated, bounded request->response bridge to an open DataChannel."""
    assert channel is not None, "channel required"
    session = {"authed": False, "device_id": None, "inflight": 0, "pc": pc}

    @channel.on("message")
    def _on_message(message) -> None:  # aiortc invokes this sync; serve on the loop
        text = message if isinstance(message, str) else bytes(message).decode("utf-8", "replace")
        if len(text) > _MAX_MESSAGE_BYTES:  # bound INBOUND too (a peer can't exhaust memory)
            log.warning("webrtc: oversized inbound message dropped (%d bytes)", len(text))
            return
        # Hold a strong ref until the task completes — CPython otherwise can GC
        # a fire-and-forget task while it is still running (issue 91887).
        _track(asyncio.ensure_future(_serve_message(channel, text, http_client, store, session)))


async def answer_offer(offer_sdp: str, *, store, http_client, ice_servers=None):
    """Create a peer connection for an incoming offer; return ``(pc, answer_sdp)``.

    The offer carries NO credential — the device authenticates over the encrypted
    channel (see module docstring). The caller MUST keep ``pc`` referenced for the
    connection's lifetime.
    """
    from aiortc import (  # lazy: only when a peer is actually created
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )

    assert offer_sdp, "offer sdp required"
    assert store is not None, "device store required for channel auth"
    servers = [RTCIceServer(**s) for s in (ice_servers or [])]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=servers))

    @pc.on("datachannel")
    def _on_datachannel(channel) -> None:
        if channel.label == _API_CHANNEL:  # ignore any other channel a peer opens
            _wire_channel(pc, channel, http_client, store)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
    await pc.setLocalDescription(await pc.createAnswer())
    assert pc.localDescription is not None, "answer description must be set"
    # Advertise ONE DTLS fingerprint so the phone and Desktop bind to the same value.
    # channel_binding() reads pc.localDescription, whose first fingerprint is the one
    # kept here, so both sides agree even though browsers normalize the answer.
    return pc, _single_fingerprint(pc.localDescription.sdp)
