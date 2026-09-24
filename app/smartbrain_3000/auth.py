"""Local API credentials (R14, operator rulings 2026-09-23).

Before this, the API authenticated by PROCESS STATE: while the vault was unlocked,
any request that passed HostGuard + OriginGuard was served. Loopback is shared by
every account on the machine, so another OS user — or any local process — could
read the unlocked vault, and the "Desktop-only" marker (a constant ``X-SB-Local``
header) stopped only phone-relayed requests. Every ``/api`` request must now carry
one of three credentials, and the credential decides its AUTHORITY:

* **Browser session** — the ``sb_session`` cookie (HttpOnly, SameSite=Strict),
  minted by setup / unlock / "open in this browser" (a passphrase or Recovery Key
  proof). Sessions live for this app run and SURVIVE Lock (a phone unlock still
  walks a known desktop tab in); a restart clears them. Authority: ``desktop`` when
  minted on a loopback Host, ``remote`` otherwise (LAN-direct phones get phone
  authority) — fixed at mint, so replaying a LAN session with a loopback Host header
  gains nothing. The Host a passphrase holder's own client sends AT unlock is still
  theirs to write: this split limits a phone's browser, it is not a wall against
  someone on the LAN who knows the passphrase.
* **Local token** — ``Authorization: Bearer <token>`` for the launcher, doctor,
  scripts and CI. The launcher owns it and passes ``SMARTBRAIN_LOCAL_TOKEN``;
  without one the app writes its own ``local-api.token`` (0600) beside the DB.
  Authority: ``desktop``.
* **Relay credential** — a per-process secret the WebRTC bridge attaches (with the
  device id) AFTER the DataChannel's own device authentication. The bridge drops
  every phone-supplied header outside its allow-list, so a phone cannot forge it.
  Authority: ``remote``.

A handful of routes stay open (liveness, account status, setup, unlock, the
state-guarded OAuth callback). ``/mcp`` keeps its own bearer. NOT defended: malware
running as YOUR account that can read your files (the token file, your browser's
cookie store) — out of scope per SECURITY.md.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
from collections import OrderedDict
from pathlib import Path

from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

COOKIE_NAME = "sb_session"
COOKIE_MAX_AGE_S = 30 * 24 * 3600  # server-side validity is this app run; see SessionTable
TOKEN_ENV = "SMARTBRAIN_LOCAL_TOKEN"
TOKEN_FILE = "local-api.token"
RELAY_HEADER = "x-sb-relay"
DEVICE_HEADER = "x-sb-device"
DESKTOP = "desktop"
REMOTE = "remote"
_MIN_TOKEN_CHARS = 32
_MAX_SESSIONS = 64
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# The only /api routes served without a credential. Everything else gets 401.
OPEN_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/health"), ("HEAD", "/api/health"),
    ("GET", "/api/account/status"),
    ("POST", "/api/account/setup"),
    ("POST", "/api/account/unlock"),
    # Google's consent page redirects the browser here cross-site, so a SameSite=Strict
    # cookie never rides it; the route's own one-shot ``state`` is its defense.
    ("GET", "/api/email/oauth/callback"), ("HEAD", "/api/email/oauth/callback"),
})

# Per process, never persisted, never sent anywhere but the in-process bridge.
_RELAY_SECRET = secrets.token_hex(32)


def relay_headers(device_id: str) -> dict[str, str]:
    """Headers the WebRTC bridge attaches to a device-authenticated request."""
    assert isinstance(device_id, str), "device id must be a string"
    return {RELAY_HEADER: _RELAY_SECRET, DEVICE_HEADER: device_id[:64]}


def _same(given: str, secret: str) -> bool:
    """Constant-time compare that never raises: header values can carry any latin-1
    byte, and ``hmac.compare_digest`` refuses non-ASCII ``str`` (a 500, not a 401)."""
    return hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))


def _valid_token(value: str) -> bool:
    return isinstance(value, str) and len(value) >= _MIN_TOKEN_CHARS and value.isascii() \
        and not any(c.isspace() for c in value)


def load_local_token(db_path: Path | None) -> str:
    """Return the local token: the launcher's (env) if given, else this install's
    own file beside the DB (created 0600 on first use).

    A present-but-weak env token is REFUSED (raises): silently falling back would
    leave the launcher holding a token the app doesn't accept.
    """
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        if not _valid_token(env):
            raise ValueError(f"{TOKEN_ENV} must be >= {_MIN_TOKEN_CHARS} non-space ASCII chars")
        return env
    if db_path is None:  # no data dir (tests without a lifespan): process-local token
        return secrets.token_urlsafe(32)
    path = Path(db_path).parent / TOKEN_FILE
    try:
        existing = path.read_text(encoding="ascii").strip()
        if _valid_token(existing):
            return existing
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("local token file unreadable (%s); minting a new one", type(exc).__name__)
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(f".{TOKEN_FILE}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("ascii") + b"\n")
    finally:
        os.close(fd)
    os.replace(tmp, path)  # atomic; the 0600 mode rides the rename
    return token


class SessionTable:
    """Browser sessions for this app run: SHA-256 digests (never tokens), each with
    the authority it was minted under.

    Bounded (oldest evicted). Deliberately NOT cleared by Lock (operator ruling:
    once per browser per app run); a restart starts empty.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._digests: OrderedDict[str, str] = OrderedDict()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()

    def mint(self, authority: str) -> str:
        assert authority in (DESKTOP, REMOTE), "a session is desktop or remote"
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._digests[self._digest(token)] = authority
            while len(self._digests) > _MAX_SESSIONS:
                self._digests.popitem(last=False)
        return token

    def authority(self, token: str) -> str | None:
        """The authority ``token`` was minted under; None for an unknown token."""
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        with self._lock:
            return self._digests.get(self._digest(token))


def _state_value(app, name: str, factory):
    """Lazily create an app.state member (tests may skip the lifespan)."""
    value = getattr(app.state, name, None)
    if value is None:
        value = factory()
        setattr(app.state, name, value)
    return value


def sessions(app) -> SessionTable:
    return _state_value(app, "sessions", SessionTable)


def local_token(app) -> str:
    return _state_value(app, "local_token", lambda: load_local_token(None))


def _cookie_value(raw: str) -> str:
    for part in raw.split(";"):  # bounded by the header size limit
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value.strip()
    return ""


def authority_of(scope: dict, app) -> str | None:
    """Resolve a request's credential to ``desktop`` / ``remote`` / None."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    relay = headers.get(RELAY_HEADER)
    if relay is not None:  # a relay header is authoritative: valid, or nothing
        return REMOTE if _same(relay, _RELAY_SECRET) else None
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return DESKTOP if _same(auth[7:].strip(), local_token(app)) else None
    cookie = _cookie_value(headers.get("cookie", ""))
    minted = sessions(app).authority(cookie) if cookie else None
    if minted is None:
        return None
    # Fixed at mint; a later request can only NARROW it (the Host header is the
    # client's to write), never widen a LAN session to desktop.
    return DESKTOP if minted == DESKTOP and host_authority(headers.get("host", "")) == DESKTOP else REMOTE


def host_authority(host_header: str) -> str:
    """The authority a browser session opened on this Host gets: desktop on
    loopback, phone authority otherwise (the web app loaded from a LAN address)."""
    return DESKTOP if _host_name(host_header) in _LOOPBACK_HOSTS else REMOTE


def _host_name(host_header: str) -> str:
    """Host header without its port, lowercased (``[::1]:33000`` → ``[::1]``)."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class SessionGuard:
    """ASGI middleware: every ``/api`` request needs a credential (or an open route);
    without one it is refused — 423 while the vault is locked, 401 otherwise.

    Records the resolved authority in ``scope["state"]["sb_authority"]`` (read via
    ``request.state.sb_authority``) for the Desktop-only checks downstream.
    Installed INSIDE HostGuard/OriginGuard, so a bad Host or cross-site request is
    refused before any credential is looked at.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not (path == "/api" or path.startswith("/api/")):
            await self._app(scope, receive, send)
            return
        authority = authority_of(scope, scope["app"])
        scope.setdefault("state", {})["sb_authority"] = authority
        method = scope.get("method", "GET").upper()
        if authority is None and (method, path) not in OPEN_ROUTES:
            # LOCKED: the ordinary 423 (lock state is public anyway — /api/account/status),
            # so a tab left open across a restart or an update, this release's page or the
            # previous one's, takes its existing "locked -> /unlock" path. Unlocked: 401
            # no_session, which the page reads as "open SmartBrain in this browser".
            if getattr(scope["app"].state, "secret_store", None) is None:
                response = JSONResponse({"detail": "locked: unlock first"}, status_code=423)
            else:
                response = JSONResponse({"detail": "session required", "code": "no_session"},
                                        status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)
