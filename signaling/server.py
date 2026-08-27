"""Self-hosted WebRTC signaling broker for SmartBrain_3000 (content-blind rendezvous).

This is the ONLY operator-run, public piece of the remote-access path. It relays
WebRTC offer/answer SDP between a Desktop (which holds a long-lived OUTBOUND WSS, so
the home machine needs no inbound port) and phones, keyed by ``desktop_id``. It sees
only connection-setup metadata — SDP (DTLS fingerprints + ICE candidate IPs) and
which device ids are online. It never sees the device credential (that travels inside
the encrypted DataChannel) and never any app data; the data plane is DTLS end-to-end
or relayed by TURN as ciphertext.

Run it on a small public node (e.g. behind TLS on :443). It is intentionally tiny and
dependency-light (``websockets`` + ``cryptography`` for Ed25519) so it is easy to audit.

Wire (JSON text):
  desktop hello : {"role":"desktop","desktop_id":<id>,"token":<broker_token>,"pubkey":<b64 Ed25519>}
  broker->desk  : {"type":"challenge","nonce":<b64 16 bytes>}
  desk ->broker : {"type":"prove","sig":<b64 Ed25519 sig over b"sb-register-v1"+nonce+desktop_id>}
  broker->desk  : {"type":"registered"}                    (or {"type":"error","detail":"unauthorized"})
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
    registration rate-limit; and the cryptographic guarantee that a phone reaches the RIGHT
    Desktop is the client's DTLS-fingerprint pin (set at pairing), NOT this token.
  In BOTH modes (token check first) a desktop must PROVE POSSESSION of its id: the first
  registration of a desktop_id binds it to the hello's Ed25519 pubkey (trust-on-first-use),
  every registration must sign the broker's nonce, and a later hello with a different key is
  ``unauthorized``. Bindings persist across restarts via ``SIGNALING_STATE_FILE`` (bounded,
  oldest-evicted). ``sbpair-*`` pairing rooms are signed too but never bound (they are
  short-lived and codes get re-used). ``SIGNALING_ALLOW_LEGACY=1`` admits an unsigned hello
  for ids with NO binding — a transition window for pre-proof Desktops only; leave it 0.

PER-CLIENT LIMITS: per-IP concurrent sockets + hello rate (client IP = first X-Forwarded-For
hop when the peer is in ``SIGNALING_TRUSTED_PROXIES``, else the socket peer), per-phone-socket
offer rate, per-desktop answer rate. All env-overridable; all maps bounded + swept.

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
import ipaddress
import json
import logging
import os
import secrets
import time

import websockets
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger("sb-signaling")

_MAX_MSG = 256 * 1024
# How long a fresh connection may stay silent before it must identify itself.
_HELLO_TIMEOUT_SECS = 10.0
# Defaults for the phone-side bounds (overridable via env at Broker() construction).
# These keep an unauthenticated public endpoint from being memory-flooded or used to
# starve a single desktop, while staying generous enough for normal multi-device use.
# GLOBAL backstop across every user of the node, not a per-user allowance: a hosted
# broker serves the whole install base, so this must scale with _DEFAULT_MAX_DESKTOPS
# rather than with one household. Fairness between users is enforced by
# _DEFAULT_MAX_PHONES_PER_DESKTOP + the per-desktop connect rate limit; this value
# exists only to bound total memory. (It was 64 — small enough that the 65th phone
# *worldwide* was refused.)
_DEFAULT_MAX_PHONES = 5000
_DEFAULT_MAX_PHONES_PER_DESKTOP = 8
_DEFAULT_PHONE_RATE_LIMIT = 30           # new phone connects per desktop_id...
_DEFAULT_PHONE_RATE_WINDOW_SECS = 60.0   # ...per this sliding window.
# Hard cap on rate-limit map keys (distinct desktop_ids tracked) so the prune map can
# never grow unbounded under a churn-of-ids attack.
_RATE_MAP_MAX_KEYS = 1024
# Open-mode (tokenless) desktop-registration backstops + ephemeral TURN defaults.
_DEFAULT_TURN_TTL = 3600                 # ephemeral TURN credential lifetime (seconds)
_DEFAULT_MAX_DESKTOPS = 5000             # global cap on concurrently-registered desktops
_DEFAULT_REG_RATE_LIMIT = 120            # new desktop registrations...
_DEFAULT_REG_RATE_WINDOW_SECS = 60.0     # ...per this global sliding window.
# Per-client abuse bounds (G3/G4). Per-IP limits are keyed on the CLIENT ip (see _client_ip).
_DEFAULT_MAX_CONNS_PER_IP = 32           # concurrent sockets from one client ip
_DEFAULT_IP_HELLO_LIMIT = 60             # hellos from one client ip...
_DEFAULT_IP_HELLO_WINDOW_SECS = 60.0     # ...per this sliding window.
_DEFAULT_OFFER_LIMIT = 10                # offers one phone socket may send...
_DEFAULT_OFFER_WINDOW_SECS = 60.0        # ...per this window (excess -> rate_limited + close).
_DEFAULT_ANSWER_LIMIT = 60               # answers one desktop socket may send per window...
_DEFAULT_ANSWER_WINDOW_SECS = 60.0       # ...(excess dropped; the link stays up).
_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"
# Desktop proof-of-possession (G1/G2).
_REGISTER_PREFIX = b"sb-register-v1"     # domain separation for the registration signature
_NONCE_BYTES = 16
_PAIR_PREFIX = "sbpair-"                 # pairing rooms: signed, never bound
_PUBKEY_BYTES = 32
_SIG_BYTES = 64


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


def _turn_name(ident: str) -> str:
    """Per-client TURN username suffix so coturn's ``--user-quota`` is per client, not shared.

    Phones use their random ident; desktops a short hash of the id (the id itself is a
    routing key that should not appear in coturn logs). Colons are stripped: coturn splits
    the username on the first ':' only, but keep the name unambiguous anyway.
    """
    assert ident, "ident required"
    if ident.startswith("phone:"):
        return ident[len("phone:"):].replace(":", "")
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


def _parse_networks(raw: str) -> list:
    """CIDR list -> ip_network objects; a bad entry is a config error, so fail loudly."""
    assert isinstance(raw, str), "networks must be a string"
    return [ipaddress.ip_network(n.strip(), strict=False) for n in raw.split(",") if n.strip()]


def _client_ip(ws, trusted: list) -> str:
    """The address per-IP limits key on: the first X-Forwarded-For hop when (and ONLY when)
    the socket peer is a trusted proxy — otherwise a direct client could forge the header
    and dodge every per-IP bound — else the socket peer address itself."""
    assert isinstance(trusted, list), "trusted networks must be a list"
    peer = ""
    try:
        peer = str((ws.remote_address or ("",))[0])
    except Exception:  # unix sockets / test doubles without a peer
        peer = ""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer or "?"
    if any(addr in net for net in trusted):
        headers = getattr(getattr(ws, "request", None), "headers", None)
        fwd = (headers.get("X-Forwarded-For", "") if headers is not None else "").split(",")[0].strip()
        if fwd:
            return fwd[:64]
    return peer


def _verify_registration(pubkey_b64: str, sig_b64: str, nonce: bytes, desktop_id: str) -> bool:
    """Ed25519-verify ``sig`` over ``sb-register-v1 || nonce || desktop_id``. Any malformed
    input is simply a failed proof (never an exception that could drop the handler)."""
    assert isinstance(nonce, bytes) and nonce, "nonce required"
    try:
        pub = base64.b64decode(pubkey_b64, validate=True)
        sig = base64.b64decode(sig_b64, validate=True)
        if len(pub) != _PUBKEY_BYTES or len(sig) != _SIG_BYTES:
            return False
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, _REGISTER_PREFIX + nonce + desktop_id.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class _Bindings:
    """desktop_id -> pubkey TOFU map, bounded (oldest evicted) and optionally persisted.

    Persistence is a plain JSON file written atomically (tmp + rename) so a crash mid-write
    never leaves a truncated map that would silently unbind every Desktop. A missing or
    unreadable file starts empty — the node degrades to first-come binding, never refuses.
    """

    def __init__(self, path: str, max_entries: int) -> None:
        assert max_entries > 0, "max_entries must be positive"
        self._path = path
        self._max = int(max_entries)
        self._map: dict[str, str] = {}
        if path:
            self._load()

    def get(self, desktop_id: str) -> str | None:
        return self._map.get(desktop_id)

    def bind(self, desktop_id: str, pubkey: str) -> None:
        assert desktop_id and pubkey, "desktop_id + pubkey required"
        if desktop_id in self._map:
            return
        while len(self._map) >= self._max:  # oldest first (dict preserves insertion order)
            self._map.pop(next(iter(self._map)))
        self._map[desktop_id] = pubkey
        self._save()

    def __len__(self) -> int:
        return len(self._map)

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            assert isinstance(data, dict), "bindings file must hold a JSON object"
            items = [(str(k), str(v)) for k, v in data.items() if k and v]
            self._map = dict(items[-self._max:])
            log.info("loaded %d desktop bindings", len(self._map))
        except FileNotFoundError:
            pass
        except Exception as exc:  # corrupt file: start empty rather than refuse every Desktop
            log.warning("bindings file unreadable (%s); starting empty", type(exc).__name__)

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._map, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("could not persist desktop bindings: %s", type(exc).__name__)


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
        state_file: str = "",
        allow_legacy: bool = False,
        trusted_proxies: str = _DEFAULT_TRUSTED_PROXIES,
        max_conns_per_ip: int = _DEFAULT_MAX_CONNS_PER_IP,
        ip_hello_limit: int = _DEFAULT_IP_HELLO_LIMIT,
        ip_hello_window_secs: float = _DEFAULT_IP_HELLO_WINDOW_SECS,
        offer_limit: int = _DEFAULT_OFFER_LIMIT,
        offer_window_secs: float = _DEFAULT_OFFER_WINDOW_SECS,
        answer_limit: int = _DEFAULT_ANSWER_LIMIT,
        answer_window_secs: float = _DEFAULT_ANSWER_WINDOW_SECS,
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
        assert max_conns_per_ip > 0, "max_conns_per_ip must be positive"
        assert ip_hello_limit > 0 and ip_hello_window_secs > 0, "ip hello limit/window must be positive"
        assert offer_limit > 0 and offer_window_secs > 0, "offer limit/window must be positive"
        assert answer_limit > 0 and answer_window_secs > 0, "answer limit/window must be positive"
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
        # Proof-of-possession: desktop_id -> pubkey (TOFU), bounded by the desktop cap.
        self._bindings = _Bindings(state_file, self._max_desktops)
        self._allow_legacy = bool(allow_legacy)
        # Per-IP bounds. Concurrent counts shrink to zero and are dropped on close, so the map
        # is bounded by live sockets; the hello map is swept like the phone rate map.
        self._trusted = _parse_networks(trusted_proxies)
        self._max_conns_per_ip = int(max_conns_per_ip)
        self._ip_hello_limit = int(ip_hello_limit)
        self._ip_hello_window = float(ip_hello_window_secs)
        self._ip_conns: dict[str, int] = {}
        self._ip_hellos: dict[str, list[float]] = {}
        self._offer_limit = int(offer_limit)
        self._offer_window = float(offer_window_secs)
        self._answer_limit = int(answer_limit)
        self._answer_window = float(answer_window_secs)
        # Which desktop each admitted phone belongs to: an answer is delivered only to a phone
        # in the answering desktop's own room (G5) — a desktop can never reach a stranger's phone.
        self._phone_rooms: dict[str, str] = {}
        self._dropped_answers = 0  # cross-room / over-rate answers dropped (operator signal)

    async def handle(self, ws) -> None:
        """One connection: per-IP admission, read the hello, then run the role's relay loop."""
        role = ident = None
        phone_desktop_id = ""
        ip = _client_ip(ws, self._trusted)
        if self._ip_conns.get(ip, 0) >= self._max_conns_per_ip:
            # Pre-hello on purpose: a silent socket must not count for free until the timeout.
            await _send(ws, {"type": "error", "detail": "busy"})
            return
        self._ip_conns[ip] = self._ip_conns.get(ip, 0) + 1
        try:
            # Every admission cap below runs only once a hello arrives, so a connection
            # that never sends one would hold a socket indefinitely while bypassing all
            # of them. Bound the wait.
            hello = json.loads(await asyncio.wait_for(ws.recv(), _HELLO_TIMEOUT_SECS))
            assert isinstance(hello, dict), "hello must be a JSON object"
            if not self._admit_ip_hello(ip):
                await _send(ws, {"type": "error", "detail": "rate_limited"})
                return
            role = hello.get("role")
            desktop_id = str(hello.get("desktop_id") or "")
            if not desktop_id:
                await _send(ws, {"type": "error", "detail": "missing desktop_id"})
                return
            if role == "desktop":
                reject = self._admit_desktop(hello.get("token"))
                if reject is None:
                    reject = await self._prove_desktop(ws, desktop_id, hello.get("pubkey"))
                if reject is not None:
                    await _send(ws, {"type": "error", "detail": reject})
                    return
                # First registration of an id wins for as long as it holds the socket. A
                # desktop_id is a routing key, not a secret — it travels in the pairing
                # payload and every phone hello — so anyone who learns one could otherwise
                # re-register it, silently displace the real Desktop, and receive that
                # user's phone offers. (Their pinned-key channel auth would refuse the
                # impostor, but their remote access would stay dead: the cleanup below is
                # identity-checked, so the victim's own disconnect would not even restore
                # it.) The proof above already stops a stranger; this guards against the
                # same key registering twice. A genuine reconnect after a drop still works —
                # the stale socket is gone by then.
                if desktop_id in self._desktops:
                    log.info("refusing duplicate desktop registration")
                    await _send(ws, {"type": "error", "detail": "already registered"})
                    return
                ident = desktop_id
                self._desktops[desktop_id] = ws
                await _send(ws, {"type": "registered"})
                # Ephemeral mode: hand the Desktop fresh node ICE so its peers can relay without
                # any TURN secret baked into the app (in static mode it uses its own env creds).
                if self._turn_secret:
                    await _send(ws, {"type": "ice", "iceServers": self._ice_for_client(desktop_id)})
                await self._desktop_loop(ws, desktop_id)
            elif role == "phone":
                reject = self._admit_phone(desktop_id)
                if reject is not None:
                    await _send(ws, {"type": "error", "detail": reject})
                    return
                ident = "phone:" + secrets.token_urlsafe(8)
                phone_desktop_id = desktop_id
                self._phones[ident] = ws
                self._phone_rooms[ident] = desktop_id
                # Node ICE (STUN+TURN) so the phone can relay even on cellular. Pairing-by-code
                # rooms always need it (they have no payload creds yet); in ephemeral mode EVERY
                # phone gets fresh, short-lived creds pushed here instead of static payload creds.
                if self._turn_secret or desktop_id.startswith(_PAIR_PREFIX):
                    ice = self._ice_for_client(ident)
                    if ice:
                        await _send(ws, {"type": "ice", "iceServers": ice})
                await self._phone_loop(ws, ident, desktop_id)
            else:
                await _send(ws, {"type": "error", "detail": "bad role"})
        except Exception as exc:  # malformed/abrupt close — drop the connection cleanly
            log.info("connection ended: %s", type(exc).__name__)
        finally:
            remaining = self._ip_conns.get(ip, 0) - 1
            if remaining <= 0:
                self._ip_conns.pop(ip, None)
            else:
                self._ip_conns[ip] = remaining
            if role == "desktop" and ident and self._desktops.get(ident) is ws:
                self._desktops.pop(ident, None)
            elif role == "phone" and ident:
                self._phones.pop(ident, None)
                self._phone_rooms.pop(ident, None)
                self._release_phone(phone_desktop_id)

    def _admit_ip_hello(self, ip: str) -> bool:
        """Sliding-window hello rate per client ip; the map is swept so it stays bounded."""
        assert isinstance(ip, str), "ip must be a string"
        now = time.monotonic()
        cutoff = now - self._ip_hello_window
        bucket = [t for t in self._ip_hellos.get(ip, []) if t > cutoff]
        if len(bucket) >= self._ip_hello_limit:
            return False
        bucket.append(now)
        self._ip_hellos[ip] = bucket
        if len(self._ip_hellos) > _RATE_MAP_MAX_KEYS:
            for key in list(self._ip_hellos.keys())[:_RATE_MAP_MAX_KEYS]:
                kept = [t for t in self._ip_hellos[key] if t > cutoff]
                if kept:
                    self._ip_hellos[key] = kept
                else:
                    self._ip_hellos.pop(key, None)
        return True

    async def _prove_desktop(self, ws, desktop_id: str, pubkey) -> str | None:
        """Challenge/response proof that this Desktop holds the key bound to ``desktop_id``.

        Returns ``None`` when proven (binding the id on first sight — TOFU — unless it is
        a short-lived ``sbpair-*`` room), else the error ``detail``. A hello without a
        pubkey is a legacy client: admitted only while ``allow_legacy`` is on AND the id
        has never been bound, so a pre-proof Desktop keeps working through the transition
        but can never be used to take over an id that has a key.
        """
        assert desktop_id, "desktop_id required"
        pubkey = str(pubkey or "")
        bound = None if desktop_id.startswith(_PAIR_PREFIX) else self._bindings.get(desktop_id)
        if not pubkey:
            if self._allow_legacy and bound is None:
                return None
            return "unauthorized"
        if bound is not None and not hmac.compare_digest(bound, pubkey):
            log.info("refusing desktop registration: pubkey does not match binding")
            return "unauthorized"
        nonce = secrets.token_bytes(_NONCE_BYTES)
        await _send(ws, {"type": "challenge", "nonce": base64.b64encode(nonce).decode("ascii")})
        reply = json.loads(await asyncio.wait_for(ws.recv(), _HELLO_TIMEOUT_SECS))
        assert isinstance(reply, dict), "prove must be a JSON object"
        if reply.get("type") != "prove" or not _verify_registration(
            pubkey, str(reply.get("sig") or ""), nonce, desktop_id
        ):
            return "unauthorized"
        if bound is None and not desktop_id.startswith(_PAIR_PREFIX):
            self._bindings.bind(desktop_id, pubkey)
        return None

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
        if not self._token or not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            return "unauthorized"
        return None

    def _ice_for_client(self, ident: str) -> list:
        """ICE servers to hand a client right now: freshly-minted ephemeral creds (named per
        client, so coturn quotas are per client) when a TURN secret is configured, otherwise
        the static pair-room ICE (back-compat)."""
        assert ident, "client ident required"
        if self._turn_secret and self._turn_urls:
            user, cred = mint_turn_credentials(self._turn_secret, self._turn_ttl, name=_turn_name(ident))
            return [{"urls": list(self._turn_urls), "username": user, "credential": cred}]
        return self._pair_ice

    async def _phone_loop(self, ws, phone_id: str, desktop_id: str) -> None:
        offers: list[float] = []  # this socket's recent offer timestamps (bounded by the limit)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "offer":
                continue
            now = time.monotonic()
            offers = [t for t in offers if t > now - self._offer_window]
            if len(offers) >= self._offer_limit:
                # An admitted phone re-offering in a tight loop is the cheapest way to make
                # a Desktop spin up peers; refuse and drop the socket (a real phone re-connects).
                await _send(ws, {"type": "error", "detail": "rate_limited"})
                return
            offers.append(now)
            desk = self._desktops.get(desktop_id)
            if desk is None:
                await _send(ws, {"type": "error", "detail": "desktop offline"})
                continue
            await _send(desk, {"type": "offer", "from": phone_id, "sdp": msg.get("sdp")})

    async def _desktop_loop(self, ws, desktop_id: str) -> None:
        assert desktop_id, "desktop_id required"
        answers: list[float] = []
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "answer":
                continue
            now = time.monotonic()
            answers = [t for t in answers if t > now - self._answer_window]
            if len(answers) >= self._answer_limit:
                self._dropped_answers += 1  # drop, keep the link: it is the user's own Desktop
                continue
            answers.append(now)
            to = str(msg.get("to") or "")
            phone = self._phones.get(to)
            if phone is None:
                continue
            if self._phone_rooms.get(to) != desktop_id:
                # A desktop may only answer phones that dialled ITS id — never another room's.
                self._dropped_answers += 1
                log.info("dropping cross-room answer")
                continue
            await _send(phone, {"type": "answer", "sdp": msg.get("sdp")})

    def _admit_phone(self, desktop_id: str) -> str | None:
        """Apply global cap, per-desktop cap, and rate limit to a phone hello.

        Returns ``None`` on admit (and reserves a slot); otherwise an error ``detail``
        string. Content-blind: only the desktop_id keys, never any SDP, are inspected.
        """
        assert isinstance(desktop_id, str), "desktop_id must be a string"
        assert desktop_id, "desktop_id must be non-empty"
        if len(self._phones) >= self._max_phones:
            # Node-wide saturation refuses users who did nothing wrong and looks to them
            # like remote access is simply broken, so make it loud: it is an operator
            # signal to raise SIGNALING_MAX_PHONES, never a routine rejection.
            log.warning("global phone cap reached (%d) — refusing new phones", self._max_phones)
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
        state_file=os.environ.get("SIGNALING_STATE_FILE", ""),
        allow_legacy=os.environ.get("SIGNALING_ALLOW_LEGACY", "0").strip() in ("1", "true", "True"),
        trusted_proxies=os.environ.get("SIGNALING_TRUSTED_PROXIES", _DEFAULT_TRUSTED_PROXIES),
        max_conns_per_ip=int(os.environ.get("SIGNALING_MAX_CONNS_PER_IP", _DEFAULT_MAX_CONNS_PER_IP)),
        ip_hello_limit=int(os.environ.get("SIGNALING_IP_HELLO_LIMIT", _DEFAULT_IP_HELLO_LIMIT)),
        ip_hello_window_secs=float(os.environ.get(
            "SIGNALING_IP_HELLO_WINDOW_SECS", _DEFAULT_IP_HELLO_WINDOW_SECS)),
        offer_limit=int(os.environ.get("SIGNALING_OFFER_LIMIT", _DEFAULT_OFFER_LIMIT)),
        offer_window_secs=float(os.environ.get("SIGNALING_OFFER_WINDOW_SECS", _DEFAULT_OFFER_WINDOW_SECS)),
        answer_limit=int(os.environ.get("SIGNALING_ANSWER_LIMIT", _DEFAULT_ANSWER_LIMIT)),
        answer_window_secs=float(os.environ.get("SIGNALING_ANSWER_WINDOW_SECS", _DEFAULT_ANSWER_WINDOW_SECS)),
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
    log.info("signaling broker listening on %s:%d (mode=%s, ice=%s, bindings=%d%s, legacy=%s)",
             host, port, "open" if open_mode else "token",
             "ephemeral" if os.environ.get("SIGNALING_TURN_SECRET") else "static",
             len(broker._bindings), " persisted" if os.environ.get("SIGNALING_STATE_FILE") else " in-memory",
             "ALLOWED" if broker._allow_legacy else "off")
    async with websockets.serve(broker.handle, host, port, max_size=_MAX_MSG):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
