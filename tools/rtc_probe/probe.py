#!/usr/bin/env python3
"""End-to-end probe of the hosted signaling node — the thing a user's phone depends on.

Plays BOTH sides through the real node: a synthetic Desktop registers (challenge/prove
with a throwaway routing key), a synthetic phone offers, the DataChannel opens, and a
hello/auth-shaped exchange completes. Runs once with the network's own path and once
FORCING the TURN relay, so a broken coturn shows up even when the direct path works.
Also checks TLS certificate expiry and a STUN binding. Exit code 0 = healthy; anything
else names the failing stage. No secrets: the node is open-registration and the probe's
desktop id is random per run.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_NODE = "rtc.securecloudgroup.com"
_HELLO_WAIT = 20.0
_CHANNEL_WAIT = 30.0


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def check_tls_days(host: str) -> int:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # the node speaks TLS 1.2+; never negotiate down
    with socket.create_connection((host, 443), timeout=10) as sock, ctx.wrap_socket(sock, server_hostname=host) as tls:
        cert = tls.getpeercert()
    not_after = ssl.cert_time_to_seconds(cert["notAfter"])
    return int((not_after - time.time()) // 86400)


def check_stun(host: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    tid = os.urandom(12)
    s.sendto(struct.pack("!HHI", 0x0001, 0, 0x2112A442) + tid, (host, 3478))
    data, _ = s.recvfrom(1024)
    mtype = struct.unpack("!H", data[:2])[0]
    assert mtype == 0x0101, f"unexpected STUN response type 0x{mtype:04x}"


async def desktop_side(url: str, desktop_id: str, key: Ed25519PrivateKey, got_offer: asyncio.Future, answer_q: asyncio.Queue) -> None:
    import websockets
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    async with websockets.connect(url, max_size=256 * 1024) as ws:
        await ws.send(json.dumps({"role": "desktop", "desktop_id": desktop_id, "pubkey": b64(pub)}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), _HELLO_WAIT))
            if msg.get("type") == "challenge":
                nonce = base64.b64decode(msg["nonce"])
                sig = key.sign(b"sb-register-v1" + nonce + desktop_id.encode())
                await ws.send(json.dumps({"type": "prove", "sig": b64(sig)}))
            elif msg.get("type") == "registered":
                break
            elif msg.get("type") == "error":
                raise RuntimeError(f"desktop registration refused: {msg.get('detail')}")
        # wait for the phone's offer, answer it
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), _CHANNEL_WAIT))
            if msg.get("type") == "offer":
                got_offer.set_result(msg)
                to, sdp = await answer_q.get()
                await ws.send(json.dumps({"type": "answer", "to": to, "sdp": sdp}))
                await asyncio.sleep(_CHANNEL_WAIT)  # keep the registration alive while the channel test runs
                return


async def run_pair(node: str, relay_only: bool) -> float:
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    import websockets

    url = f"wss://{node}/signal"
    desktop_id = "probe-" + secrets.token_hex(8)
    key = Ed25519PrivateKey.generate()
    got_offer: asyncio.Future = asyncio.get_running_loop().create_future()
    answer_q: asyncio.Queue = asyncio.Queue()
    desk_task = asyncio.ensure_future(desktop_side(url, desktop_id, key, got_offer, answer_q))

    # phone side
    ice: list = []
    async with websockets.connect(url, max_size=256 * 1024) as ws:
        await ws.send(json.dumps({"role": "phone", "desktop_id": desktop_id}))
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), 3.0))
            if first.get("type") == "ice":
                ice = first.get("iceServers") or []
            elif first.get("type") == "error":
                raise RuntimeError(f"phone refused: {first.get('detail')}")
        except asyncio.TimeoutError:
            pass
        servers = [RTCIceServer(**s) for s in ice]
        if relay_only:
            assert any("turn:" in u for s in ice for u in (s.get("urls") or [])), "no TURN server pushed — relay path cannot be tested"
        pc_phone = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        pc_desk = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        opened: asyncio.Future = asyncio.get_running_loop().create_future()
        echoed: asyncio.Future = asyncio.get_running_loop().create_future()

        @pc_desk.on("datachannel")
        def _on_dc(ch):
            @ch.on("message")
            def _m(data):
                ch.send(json.dumps({"type": "probe_ok", "echo": json.loads(data).get("nonce")}))

        ch = pc_phone.createDataChannel("sb-api")
        nonce = secrets.token_hex(8)

        @ch.on("open")
        def _open():
            opened.set_result(True)
            ch.send(json.dumps({"type": "hello", "nonce": nonce}))

        @ch.on("message")
        def _msg(data):
            m = json.loads(data)
            if m.get("type") == "probe_ok" and m.get("echo") == nonce and not echoed.done():
                echoed.set_result(True)

        t0 = time.monotonic()
        await pc_phone.setLocalDescription(await pc_phone.createOffer())
        offer_sdp = pc_phone.localDescription.sdp
        if relay_only:
            # aiortc has no iceTransportPolicy: offer ONLY relay candidates, so whatever pair
            # the two sides nominate must run through coturn.
            lines = re.split(r"\r?\n", offer_sdp)
            offer_sdp = "\r\n".join(l for l in lines if not l.startswith("a=candidate:") or " typ relay " in l)
            assert " typ relay " in offer_sdp, "no relay candidate gathered — TURN allocation failed"
        await ws.send(json.dumps({"type": "offer", "sdp": offer_sdp}))
        offer = await asyncio.wait_for(got_offer, _CHANNEL_WAIT)
        await pc_desk.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type="offer"))
        await pc_desk.setLocalDescription(await pc_desk.createAnswer())
        await answer_q.put((offer["from"], pc_desk.localDescription.sdp))
        ans = json.loads(await asyncio.wait_for(ws.recv(), _CHANNEL_WAIT))
        assert ans.get("type") == "answer", f"expected answer, got {ans}"
        await pc_phone.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type="answer"))
        await asyncio.wait_for(opened, _CHANNEL_WAIT)
        await asyncio.wait_for(echoed, _CHANNEL_WAIT)
        elapsed = time.monotonic() - t0
        await pc_phone.close()
        await pc_desk.close()
    desk_task.cancel()
    await asyncio.gather(desk_task, return_exceptions=True)  # retrieve the cancellation quietly
    return elapsed


def main() -> int:
    # coturn correctly refuses permissions for private-range peers (our own host candidates);
    # aioice reports each refusal as an error — expected noise on a relay-forced run.
    logging.getLogger("aioice").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=os.environ.get("RTC_PROBE_NODE", DEFAULT_NODE))
    ap.add_argument("--min-cert-days", type=int, default=14)
    args = ap.parse_args()
    failures: list[str] = []
    try:
        days = check_tls_days(args.node)
        print(f"tls: certificate valid for {days} more days")
        if days < args.min_cert_days:
            failures.append(f"certificate expires in {days} days")
    except Exception as exc:
        failures.append(f"tls: {exc}")
    try:
        check_stun(args.node)
        print("stun: binding ok")
    except Exception as exc:
        failures.append(f"stun: {exc}")
    for relay in (False, True):
        try:
            secs = asyncio.run(run_pair(args.node, relay_only=relay))
            print(f"pair ({'relay-forced' if relay else 'natural'}): channel open + round trip in {secs:.2f}s")
        except Exception as exc:
            failures.append(f"pair ({'relay' if relay else 'natural'}): {type(exc).__name__}: {exc}")
    if failures:
        print("PROBE FAILED:\n  - " + "\n  - ".join(failures))
        return 1
    print("PROBE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
