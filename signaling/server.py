"""Self-hosted WebRTC signaling broker for SmartBrain_3000 (content-blind rendezvous).

This is the ONLY operator-run, public piece of the remote-access path. It relays
WebRTC offer/answer SDP between a Desktop (which holds a long-lived OUTBOUND WSS, so
the home machine needs no inbound port) and phones, keyed by ``desktop_id``. It sees
only connection-setup metadata — SDP (DTLS fingerprints + ICE candidate IPs) and
which device ids are online. It never sees the device credential (that travels inside
the encrypted DataChannel) and never any app data; the data plane is DTLS end-to-end
or relayed by TURN as ciphertext.

Run it on a small public node (e.g. behind TLS on :443). It is intentionally tiny and
dependency-light (only ``websockets``) so it is easy to audit.

Wire (JSON text):
  desktop hello : {"role":"desktop","desktop_id":<id>,"token":<broker_token>}
  phone   hello : {"role":"phone","desktop_id":<id>}
  phone  ->desk : {"type":"offer","sdp":<sdp>}            (broker adds "from":<phone_id>)
  desk  ->phone : {"type":"answer","to":<phone_id>,"sdp":<sdp>}  (phone gets {"type":"answer","sdp"})
  broker->phone : {"type":"error","detail":...}           (desktop offline, etc.)
  broker->phone : {"type":"ice","iceServers":[...]}        (pairing rooms only: node STUN+TURN)

SECURITY NOTE: ``SIGNALING_TOKEN`` is a shared desktop registration secret for this
MVP — it stops open abuse / desktop-slot squatting. The cryptographic guarantee that
a phone is talking to the RIGHT Desktop is the client's DTLS-fingerprint pin (set at
pairing), not this token. A multi-tenant deployment should issue per-desktop tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time

import websockets

log = logging.getLogger("sb-signaling")

_MAX_MSG = 256 * 1024
# Defaults for the phone-side bounds (overridable via env at Broker() construction).
# These keep an unauthenticated public endpoint from being memory-flooded or used to
# starve a single desktop, while staying generous enough for normal multi-device use.
_DEFAULT_MAX_PHONES = 64
_DEFAULT_MAX_PHONES_PER_DESKTOP = 8
_DEFAULT_PHONE_RATE_LIMIT = 30           # new phone connects per desktop_id...
_DEFAULT_PHONE_RATE_WINDOW_SECS = 60.0   # ...per this sliding window.
# Hard cap on rate-limit map keys (distinct desktop_ids tracked) so the prune map can
# never grow unbounded under a churn-of-ids attack.
_RATE_MAP_MAX_KEYS = 1024


class Broker:
    """In-memory relay: desktop_id -> desktop ws, phone_id -> phone ws."""

    def __init__(
        self,
        token: str,
        pair_ice: list | None = None,
        max_phones: int = _DEFAULT_MAX_PHONES,
        max_phones_per_desktop: int = _DEFAULT_MAX_PHONES_PER_DESKTOP,
        rate_limit: int = _DEFAULT_PHONE_RATE_LIMIT,
        rate_window_secs: float = _DEFAULT_PHONE_RATE_WINDOW_SECS,
    ) -> None:
        assert isinstance(token, str), "token must be a string"
        assert pair_ice is None or isinstance(pair_ice, list), "pair_ice must be a list"
        assert max_phones > 0, "max_phones must be positive"
        assert max_phones_per_desktop > 0, "max_phones_per_desktop must be positive"
        assert rate_limit > 0, "rate_limit must be positive"
        assert rate_window_secs > 0, "rate_window_secs must be positive"
        self._token = token
        self._pair_ice = pair_ice or []
        self._desktops: dict[str, object] = {}
        self._phones: dict[str, object] = {}
        self._max_phones = int(max_phones)
        self._max_phones_per_desktop = int(max_phones_per_desktop)
        self._rate_limit = int(rate_limit)
        self._rate_window = float(rate_window_secs)
        # Per-desktop concurrent-phone counts (incremented on admit, decremented on disconnect).
        self._phones_per_desktop: dict[str, int] = {}
        # Per-desktop monotonic timestamps of recent phone connects (pruned each admit).
        self._phone_connects: dict[str, list[float]] = {}

    async def handle(self, ws) -> None:
        """One connection: read the hello, then run the role's relay loop."""
        role = ident = None
        phone_desktop_id = ""
        try:
            hello = json.loads(await ws.recv())
            assert isinstance(hello, dict), "hello must be a JSON object"
            role = hello.get("role")
            desktop_id = str(hello.get("desktop_id") or "")
            if not desktop_id:
                await _send(ws, {"type": "error", "detail": "missing desktop_id"})
                return
            if role == "desktop":
                if not self._token or hello.get("token") != self._token:
                    await _send(ws, {"type": "error", "detail": "unauthorized"})
                    return
                ident = desktop_id
                self._desktops[desktop_id] = ws
                await _send(ws, {"type": "registered"})
                await self._desktop_loop(ws)
            elif role == "phone":
                reject = self._admit_phone(desktop_id)
                if reject is not None:
                    await _send(ws, {"type": "error", "detail": reject})
                    return
                ident = "phone:" + secrets.token_urlsafe(8)
                phone_desktop_id = desktop_id
                self._phones[ident] = ws
                # Pairing-by-code rooms get the node's ICE (STUN+TURN) so the phone can relay
                # even on cellular — it has no TURN creds of its own until it has the payload.
                if self._pair_ice and desktop_id.startswith("sbpair-"):
                    await _send(ws, {"type": "ice", "iceServers": self._pair_ice})
                await self._phone_loop(ws, ident, desktop_id)
            else:
                await _send(ws, {"type": "error", "detail": "bad role"})
        except Exception as exc:  # malformed/abrupt close — drop the connection cleanly
            log.info("connection ended: %s", type(exc).__name__)
        finally:
            if role == "desktop" and ident and self._desktops.get(ident) is ws:
                self._desktops.pop(ident, None)
            elif role == "phone" and ident:
                self._phones.pop(ident, None)
                self._release_phone(phone_desktop_id)

    async def _phone_loop(self, ws, phone_id: str, desktop_id: str) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "offer":
                continue
            desk = self._desktops.get(desktop_id)
            if desk is None:
                await _send(ws, {"type": "error", "detail": "desktop offline"})
                continue
            await _send(desk, {"type": "offer", "from": phone_id, "sdp": msg.get("sdp")})

    async def _desktop_loop(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "answer":
                continue
            phone = self._phones.get(str(msg.get("to") or ""))
            if phone is not None:
                await _send(phone, {"type": "answer", "sdp": msg.get("sdp")})

    def _admit_phone(self, desktop_id: str) -> str | None:
        """Apply global cap, per-desktop cap, and rate limit to a phone hello.

        Returns ``None`` on admit (and reserves a slot); otherwise an error ``detail``
        string. Content-blind: only the desktop_id keys, never any SDP, are inspected.
        """
        assert isinstance(desktop_id, str), "desktop_id must be a string"
        assert desktop_id, "desktop_id must be non-empty"
        if len(self._phones) >= self._max_phones:
            return "busy"
        if self._phones_per_desktop.get(desktop_id, 0) >= self._max_phones_per_desktop:
            return "busy"
        now = time.monotonic()
        bucket = self._prune_rate_bucket(desktop_id, now)
        if len(bucket) >= self._rate_limit:
            return "rate_limited"
        bucket.append(now)
        self._phone_connects[desktop_id] = bucket
        self._phones_per_desktop[desktop_id] = self._phones_per_desktop.get(desktop_id, 0) + 1
        # Keep the rate-limit map bounded even under a churn-of-ids attack.
        if len(self._phone_connects) > _RATE_MAP_MAX_KEYS:
            self._sweep_rate_map(now)
        return None

    def _release_phone(self, desktop_id: str) -> None:
        """Decrement the per-desktop concurrent count on phone disconnect."""
        assert isinstance(desktop_id, str), "desktop_id must be a string"
        if not desktop_id:
            return
        remaining = self._phones_per_desktop.get(desktop_id, 0) - 1
        assert remaining >= -1, "release without matching admit"
        if remaining <= 0:
            self._phones_per_desktop.pop(desktop_id, None)
        else:
            self._phones_per_desktop[desktop_id] = remaining

    def _prune_rate_bucket(self, desktop_id: str, now: float) -> list[float]:
        """Return ``desktop_id``'s connect-timestamp bucket with stale entries dropped."""
        assert isinstance(desktop_id, str), "desktop_id must be a string"
        assert now >= 0.0, "now must be a monotonic timestamp"
        cutoff = now - self._rate_window
        bucket = self._phone_connects.get(desktop_id, [])
        # Bound the per-bucket scan: even a worst-case attacker can only have appended
        # _rate_limit entries (anything past that was rejected), so this is O(_rate_limit).
        kept: list[float] = [t for t in bucket if t > cutoff]
        return kept

    def _sweep_rate_map(self, now: float) -> None:
        """Drop every empty/expired bucket so the rate-limit map stays bounded."""
        assert now >= 0.0, "now must be a monotonic timestamp"
        cutoff = now - self._rate_window
        # Snapshot keys first — mutating during iteration is unsafe.
        for key in list(self._phone_connects.keys())[:_RATE_MAP_MAX_KEYS]:
            kept = [t for t in self._phone_connects[key] if t > cutoff]
            if kept:
                self._phone_connects[key] = kept
            else:
                self._phone_connects.pop(key, None)


async def _send(ws, obj: dict) -> None:
    await ws.send(json.dumps(obj))


def _pair_ice_from_env() -> list:
    """ICE servers (STUN+TURN) handed to pairing-by-code clients, built from env. The TURN
    creds are the node's static, bandwidth-only, quota-bounded creds (the same ones already
    baked into every pairing QR) — they grant relay bandwidth only, never app access."""
    urls = [u.strip() for u in os.environ.get("SMARTBRAIN_PAIR_ICE_URLS", "").split(",") if u.strip()]
    if not urls:
        return []
    server: dict = {"urls": urls}
    user = os.environ.get("SMARTBRAIN_PAIR_TURN_USERNAME", "")
    cred = os.environ.get("SMARTBRAIN_PAIR_TURN_CREDENTIAL", "")
    if user and cred:
        server["username"], server["credential"] = user, cred
    return [server]


def _broker_from_env(token: str, pair_ice: list) -> Broker:
    """Build a Broker with caps + rate-limit read from env (defaults if unset)."""
    assert isinstance(token, str), "token must be a string"
    assert isinstance(pair_ice, list), "pair_ice must be a list"
    return Broker(
        token,
        pair_ice,
        max_phones=int(os.environ.get("SIGNALING_MAX_PHONES", _DEFAULT_MAX_PHONES)),
        max_phones_per_desktop=int(os.environ.get(
            "SIGNALING_MAX_PHONES_PER_DESKTOP", _DEFAULT_MAX_PHONES_PER_DESKTOP)),
        rate_limit=int(os.environ.get("SIGNALING_PHONE_RATE_LIMIT", _DEFAULT_PHONE_RATE_LIMIT)),
        rate_window_secs=float(os.environ.get(
            "SIGNALING_PHONE_RATE_WINDOW_SECS", _DEFAULT_PHONE_RATE_WINDOW_SECS)),
    )


async def main() -> None:
    """Run the broker on SIGNALING_HOST:SIGNALING_PORT (TLS terminated upstream)."""
    host = os.environ.get("SIGNALING_HOST", "0.0.0.0")
    port = int(os.environ.get("SIGNALING_PORT", "8089"))
    token = os.environ.get("SIGNALING_TOKEN", "")
    if not token:  # fail-fast: never run an open broker (Broker() also rejects desktops if empty)
        raise SystemExit("SIGNALING_TOKEN must be set to a non-empty desktop registration secret")
    broker = _broker_from_env(token, _pair_ice_from_env())
    log.info("signaling broker listening on %s:%d", host, port)
    async with websockets.serve(broker.handle, host, port, max_size=_MAX_MSG):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
