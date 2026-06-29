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
  broker->*     : {"type":"ice","iceServers":[...]}        (node STUN/TURN; with a TURN secret the
                                                            creds are EPHEMERAL and pushed to both
                                                            desktops and phones, otherwise the static
                                                            pair-room ICE goes to sbpair-* phones only)

AUTH MODES:
  * Token mode (default, self-host): ``SIGNALING_TOKEN`` is a shared desktop registration
    secret — it stops open abuse / desktop-slot squatting.
  * Open mode (``SIGNALING_OPEN=1``, hosted/public): NO shared secret — so the public app can
    register without shipping a secret. Mass-registration is bounded by a global desktop cap +
    registration rate-limit; desktop_ids are unguessable random, so targeted slot-hijack isn't a
    concern; and the cryptographic guarantee that a phone reaches the RIGHT Desktop is the client's
    DTLS-fingerprint pin (set at pairing), NOT this token.

TURN credentials:
  * Static (default): coturn ``--lt-cred-mech`` long-term creds, shared in the pairing payload.
  * Ephemeral (``SIGNALING_TURN_SECRET`` set, coturn ``--use-auth-secret``): the broker MINTS
    short-lived creds per connection and pushes them over the signaling channel, so no TURN secret
    ever ships in a client / public repo and a leaked credential expires instead of being an open relay.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
# Open-mode (tokenless) desktop-registration backstops + ephemeral TURN defaults.
_DEFAULT_TURN_TTL = 86400                # ephemeral TURN credential lifetime (seconds)
_DEFAULT_MAX_DESKTOPS = 5000             # global cap on concurrently-registered desktops
_DEFAULT_REG_RATE_LIMIT = 120            # new desktop registrations...
_DEFAULT_REG_RATE_WINDOW_SECS = 60.0     # ...per this global sliding window.


def mint_turn_credentials(secret: str, ttl: int = _DEFAULT_TURN_TTL, name: str = "sb") -> tuple[str, str]:
    """coturn ``use-auth-secret`` (TURN REST API) ephemeral credential.

    username = ``"<unix_expiry>:<name>"``; password = base64(HMAC-SHA1(secret, username)). The
    secret stays on the node; clients receive only short-lived creds, so a leaked credential
    expires instead of turning the relay into an open proxy. coturn validates this exact scheme.
    """
    assert secret, "TURN secret required to mint credentials"
    expiry = int(time.time()) + int(ttl)
    username = f"{expiry}:{name}"
    mac = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    return username, base64.b64encode(mac).decode("ascii")


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
        *,
        open_mode: bool = False,
        turn_urls: list | None = None,
        turn_secret: str = "",
        turn_ttl: int = _DEFAULT_TURN_TTL,
        max_desktops: int = _DEFAULT_MAX_DESKTOPS,
        reg_rate_limit: int = _DEFAULT_REG_RATE_LIMIT,
        reg_rate_window_secs: float = _DEFAULT_REG_RATE_WINDOW_SECS,
    ) -> None:
        assert isinstance(token, str), "token must be a string"
        assert pair_ice is None or isinstance(pair_ice, list), "pair_ice must be a list"
        assert max_phones > 0, "max_phones must be positive"
        assert max_phones_per_desktop > 0, "max_phones_per_desktop must be positive"
        assert rate_limit > 0, "rate_limit must be positive"
        assert rate_window_secs > 0, "rate_window_secs must be positive"
        assert turn_urls is None or isinstance(turn_urls, list), "turn_urls must be a list"
        assert max_desktops > 0, "max_desktops must be positive"
        assert reg_rate_limit > 0, "reg_rate_limit must be positive"
        assert reg_rate_window_secs > 0, "reg_rate_window_secs must be positive"
        self._token = token
        self._pair_ice = pair_ice or []
        self._desktops: dict[str, object] = {}
        self._phones: dict[str, object] = {}
        self._max_phones = int(max_phones)
        self._max_phones_per_desktop = int(max_phones_per_desktop)
        self._rate_limit = int(rate_limit)
        self._rate_window = float(rate_window_secs)
        # Open mode (tokenless hosted) + ephemeral TURN config.
        self._open_mode = bool(open_mode)
        self._turn_urls = list(turn_urls or [])
        self._turn_secret = str(turn_secret or "")
        self._turn_ttl = int(turn_ttl)
        self._max_desktops = int(max_desktops)
        self._reg_rate_limit = int(reg_rate_limit)
        self._reg_rate_window = float(reg_rate_window_secs)
        self._desktop_regs: list[float] = []  # global registration timestamps (open mode rate-limit)
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
                reject = self._admit_desktop(hello.get("token"))
                if reject is not None:
                    await _send(ws, {"type": "error", "detail": reject})
                    return
                ident = desktop_id
                self._desktops[desktop_id] = ws
                await _send(ws, {"type": "registered"})
                # Ephemeral mode: hand the Desktop fresh node ICE so its peers can relay without
                # any TURN secret baked into the app (in static mode it uses its own env creds).
                if self._turn_secret:
                    await _send(ws, {"type": "ice", "iceServers": self._ice_for_client()})
                await self._desktop_loop(ws)
            elif role == "phone":
                reject = self._admit_phone(desktop_id)
                if reject is not None:
                    await _send(ws, {"type": "error", "detail": reject})
                    return
                ident = "phone:" + secrets.token_urlsafe(8)
                phone_desktop_id = desktop_id
                self._phones[ident] = ws
                # Node ICE (STUN+TURN) so the phone can relay even on cellular. Pairing-by-code
                # rooms always need it (they have no payload creds yet); in ephemeral mode EVERY
                # phone gets fresh, short-lived creds pushed here instead of static payload creds.
                if self._turn_secret or desktop_id.startswith("sbpair-"):
                    ice = self._ice_for_client()
                    if ice:
                        await _send(ws, {"type": "ice", "iceServers": ice})
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

    def _admit_desktop(self, token) -> str | None:
        """Authorize a desktop registration. Returns ``None`` to admit, else an error ``detail``.

        Token mode (default): require a matching shared token (fail-closed if none configured).
        Open mode: no token; bound mass-registration with a global desktop cap + rate-limit.
        """
        if self._open_mode:
            now = time.monotonic()
            if len(self._desktops) >= self._max_desktops:
                return "busy"
            cutoff = now - self._reg_rate_window
            self._desktop_regs = [t for t in self._desktop_regs if t > cutoff][-self._reg_rate_limit:]
            if len(self._desktop_regs) >= self._reg_rate_limit:
                return "rate_limited"
            self._desktop_regs.append(now)
            return None
        if not self._token or token != self._token:
            return "unauthorized"
        return None

    def _ice_for_client(self) -> list:
        """ICE servers to hand a client right now: freshly-minted ephemeral creds when a TURN
        secret is configured, otherwise the static pair-room ICE (back-compat)."""
        if self._turn_secret and self._turn_urls:
            user, cred = mint_turn_credentials(self._turn_secret, self._turn_ttl)
            return [{"urls": list(self._turn_urls), "username": user, "credential": cred}]
        return self._pair_ice

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


def _ice_urls_from_env() -> list:
    """STUN/TURN urls the node hands clients (shared by static + ephemeral modes)."""
    raw = os.environ.get("SIGNALING_ICE_URLS") or os.environ.get("SMARTBRAIN_PAIR_ICE_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _pair_ice_from_env() -> list:
    """ICE servers (STUN+TURN) handed to pairing-by-code clients, built from env. The TURN
    creds are the node's static, bandwidth-only, quota-bounded creds (the same ones already
    baked into every pairing QR) — they grant relay bandwidth only, never app access. Empty when
    a TURN secret is configured (ephemeral creds are minted per connection instead)."""
    if os.environ.get("SIGNALING_TURN_SECRET"):
        return []
    urls = _ice_urls_from_env()
    if not urls:
        return []
    server: dict = {"urls": urls}
    user = os.environ.get("SMARTBRAIN_PAIR_TURN_USERNAME", "")
    cred = os.environ.get("SMARTBRAIN_PAIR_TURN_CREDENTIAL", "")
    if user and cred:
        server["username"], server["credential"] = user, cred
    return [server]


def _broker_from_env(token: str, pair_ice: list, open_mode: bool = False) -> Broker:
    """Build a Broker with caps + rate-limit + TURN/open-mode config read from env (defaults if unset)."""
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
        open_mode=open_mode,
        turn_urls=_ice_urls_from_env(),
        turn_secret=os.environ.get("SIGNALING_TURN_SECRET", ""),
        turn_ttl=int(os.environ.get("SIGNALING_TURN_TTL", _DEFAULT_TURN_TTL)),
        max_desktops=int(os.environ.get("SIGNALING_MAX_DESKTOPS", _DEFAULT_MAX_DESKTOPS)),
        reg_rate_limit=int(os.environ.get("SIGNALING_REG_RATE_LIMIT", _DEFAULT_REG_RATE_LIMIT)),
        reg_rate_window_secs=float(os.environ.get(
            "SIGNALING_REG_RATE_WINDOW_SECS", _DEFAULT_REG_RATE_WINDOW_SECS)),
    )


async def main() -> None:
    """Run the broker on SIGNALING_HOST:SIGNALING_PORT (TLS terminated upstream)."""
    host = os.environ.get("SIGNALING_HOST", "0.0.0.0")
    port = int(os.environ.get("SIGNALING_PORT", "8089"))
    token = os.environ.get("SIGNALING_TOKEN", "")
    open_mode = os.environ.get("SIGNALING_OPEN", "").strip() not in ("", "0", "false", "False")
    if not token and not open_mode:
        # Fail-fast: never run an open broker by accident. Set SIGNALING_TOKEN (self-host) OR
        # SIGNALING_OPEN=1 (hosted: tokenless registration, bounded by caps + ephemeral TURN).
        raise SystemExit(
            "SIGNALING_TOKEN must be set to a non-empty desktop registration secret "
            "(or set SIGNALING_OPEN=1 for hosted tokenless mode)")
    broker = _broker_from_env(token, _pair_ice_from_env(), open_mode=open_mode)
    log.info("signaling broker listening on %s:%d (mode=%s, ice=%s)",
             host, port, "open" if open_mode else "token",
             "ephemeral" if os.environ.get("SIGNALING_TURN_SECRET") else "static")
    async with websockets.serve(broker.handle, host, port, max_size=_MAX_MSG):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
