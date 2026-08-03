"""FastAPI application entrypoint for SmartBrain_3000.

On startup it opens the local embedded DuckDB, runs migrations, and records this
boot; the app starts **locked** (no master key in memory until setup/unlock).
Then it serves health/status plus the account + secrets API, the Knowledge base,
and a loopback MCP server exposing the Knowledge read-only to external tools.
Everything runs locally; this app makes no outbound network calls of its own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import zoneinfo
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import PlainTextResponse

from . import __version__, db, gateway, mcp_server, runtime, scheduler, serving
from .account import router as account_router
from .chat_routes import router as chat_router
from .local_models_routes import router as local_models_router
from .models_routes import router as models_router
from .web_routes import router as web_router
from .kb_routes import router as kb_router
from .history_routes import router as history_router
from .memory_routes import router as memory_router
from .planner_routes import router as planner_router
from .agent_routes import router as agent_router
from .metrics_routes import router as metrics_router
from .schedule_routes import router as schedule_router
from .selfimprove_routes import router as selfimprove_router
from .vault_routes import router as vault_router
from .email_routes import router as email_router
from .data_routes import router as data_router
from .mcp_routes import router as mcp_router
from .devices_routes import router as devices_router

log = logging.getLogger(__name__)

_TICK_SECONDS = 30  # how often the background runner checks for due schedules
# Loopback-only by default (D-15). Validating the Host header blocks DNS-rebinding:
# a remote page cannot drive the local API by rebinding a hostname to 127.0.0.1.
_DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1"


def _allowed_hosts() -> list[str]:
    """Host allow-list for the Host header (loopback by default; env-overridable)."""
    raw = os.environ.get("SMARTBRAIN_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS)
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    assert hosts, "at least one allowed host required"
    return hosts


class HostGuard:
    """Reject requests whose Host isn't allow-listed — case-insensitively.

    Like Starlette's TrustedHostMiddleware (anti DNS-rebinding) but the match is
    case-insensitive, because hostnames are: a phone that lowercases
    ``<Name>.local`` must still match a configured ``<Name>.local``. ``*`` in the
    list disables the check (allow any host).
    """

    def __init__(self, app, allowed: list[str]) -> None:
        assert allowed, "at least one allowed host required"
        self._app = app
        self._any = "*" in allowed
        self._allowed = frozenset(h.lower() for h in allowed)

    async def __call__(self, scope, receive, send) -> None:
        assert "type" in scope, "ASGI scope must have a type"
        if scope["type"] not in ("http", "websocket") or self._any:
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1").split(":")[0].lower()
        if host in self._allowed:
            await self._app(scope, receive, send)
            return
        await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)


class OriginGuard:
    """Reject API calls that a *different* site drove the browser into making.

    The API authenticates by process state ("is the vault unlocked"), not by a
    per-request credential, so any page the owner visits while unlocked could
    otherwise POST to the local origin and have it act with full authority — no
    preflight required for a form-style or text/plain body. Host validation does
    not stop this: the attacker uses the real hostname.

    Two independent signals, either of which is conclusive when present:

    * ``Sec-Fetch-Site`` — the browser states the relationship itself. Only
      ``same-origin`` (our SPA) and ``none`` (the user typed/bookmarked it) may
      reach the API; ``cross-site`` and ``same-site`` are refused.
    * ``Origin`` — must name exactly the host:port the request was addressed to.
      A sandboxed or redirected context sends ``null``, which matches nothing.

    Comparing Origin against the *request's own* Host is only sound because
    HostGuard runs too: under DNS rebinding both headers carry the attacker's
    hostname and so agree here — it is the Host allow-list that refuses that.
    The two guards are complementary; neither replaces the other.

    Clients that are not browsers — the MCP server, the launcher handshake, curl
    — send neither header and are unaffected. Requests relayed in over WebRTC
    also arrive bare: ``webrtc_bridge.parse_request`` forwards only a three-header
    allow-list, so a phone cannot forge either signal.

    Scoped to ``/api`` and ``/mcp``: navigating to the app shell from a link on
    another page is legitimate and stays allowed (framing is already refused by
    ``X-Frame-Options`` / ``frame-ancestors``).
    """

    _GUARDED = ("/api", "/mcp")
    _ALLOWED_SITES = frozenset({"same-origin", "none"})

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        assert "type" in scope, "ASGI scope must have a type"
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith(self._GUARDED):
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        site = headers.get(b"sec-fetch-site", b"").decode("latin-1").strip().lower()
        if site and site not in self._ALLOWED_SITES:
            await self._refuse(scope, receive, send)
            return
        origin = headers.get(b"origin", b"").decode("latin-1").strip()
        if origin and not _origin_matches(origin, headers.get(b"host", b"").decode("latin-1")):
            await self._refuse(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _refuse(self, scope, receive, send) -> None:
        await PlainTextResponse("Cross-origin request refused", status_code=403)(scope, receive, send)


def _origin_matches(origin: str, host_header: str) -> bool:
    """True when ``origin`` names the same host:port the request was addressed to.

    Compares authorities, so scheme differences (http vs https) do not matter —
    the LAN/TLS overlay serves the same authority over https. ``null`` and any
    unparseable value match nothing.
    """
    assert isinstance(origin, str), "origin must be a string"
    assert isinstance(host_header, str), "host header must be a string"
    if not host_header:
        return False
    authority = urlsplit(origin).netloc
    return bool(authority) and authority.lower() == host_header.strip().lower()


def _mcp_token(application: FastAPI) -> str | None:
    """Return the configured MCP access token, or None while locked/unset."""
    store = getattr(application.state, "secret_store", None)
    return store.get(mcp_server.MCP_TOKEN_KEY) if store is not None else None


async def _scheduler_loop(application: FastAPI) -> None:
    """Background runner: every tick, fire due schedules (no-op while locked).

    Cooperative daemon — it ends when ``scheduler_stop`` is set on shutdown.
    The idle wait races against the stop event so shutdown is responsive; a tick
    in flight is allowed to finish before the loop exits (so the worker thread is
    never running against a closing DB connection). Each tick runs on a worker
    thread so the event loop is never blocked, and fires at most _MAX_PER_TICK.
    """
    assert application is not None, "application required"
    assert _TICK_SECONDS > 0, "tick interval must be positive"
    stop = application.state.scheduler_stop
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass  # idle interval elapsed — time to tick
        if stop.is_set():
            break
        try:
            await asyncio.to_thread(scheduler.tick, application)
        except Exception as exc:  # a bad tick must never kill the runner
            log.warning("scheduler tick failed: %s", exc)


async def _webrtc_loop(application: FastAPI) -> None:
    """Remote-access link: hold an outbound WSS to the signaling broker and answer phone offers.

    Dials the broker only AFTER the user opts in by pairing a device (``webrtc_active``), so a
    fresh, never-paired install makes no outbound connection. Lazy-imports webrtc_signaling (and
    thus aiortc/websockets) so those deps load only once remote access is actually used.
    """
    assert application is not None, "application required"
    from . import remote_config  # light (no aiortc)
    url = remote_config.signaling_url()
    if not url:
        log.warning("SMARTBRAIN_SIGNALING_URL is empty; remote access off")
        return
    if not await _await_webrtc_active(application):
        return  # shutting down before the user ever paired
    from . import webrtc_signaling  # lazy: aiortc/websockets only once remote is used
    await webrtc_signaling.run_signaling(
        signaling_url=url,
        desktop_id=remote_config.desktop_id(application.state.boot),
        token=os.environ.get("SMARTBRAIN_SIGNALING_TOKEN", ""),
        get_store=lambda: getattr(application.state, "secret_store", None),
        # Pass the function (not its result) so each offer re-picks UDP vs TCP TURN by live
        # network state — fast (UDP) when possible, resilient (TCP) when UDP is blocked.
        ice_servers=remote_config.ice_servers_adaptive,
        stop=application.state.webrtc_stop,
    )


async def _await_webrtc_active(application: FastAPI) -> bool:
    """Block until remote access is activated (the user paired a device) or shutdown fires.

    Returns True if activated, False if shutting down. Keeps a fresh, never-paired install from
    ever dialing the broker — the connection is the user's opt-in (pairing), not a default.
    """
    active = application.state.webrtc_active
    if active.is_set():
        return True
    waiter = asyncio.ensure_future(active.wait())
    stopper = asyncio.ensure_future(application.state.webrtc_stop.wait())
    try:
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        stopper.cancel()
    return active.is_set()


_GW_POOL_TIMEOUT = 60.0  # default per-request timeout for the pooled gateway client (B22)


def _init_app_state(application: FastAPI, conn) -> None:
    """Populate ``application.state`` with the locked-startup defaults."""
    assert application is not None, "application required"
    assert conn is not None, "open db connection required"
    application.state.db = conn  # raw root: startup migrations, scheduler cursor, shutdown
    application.state.dbx = db.ThreadLocalConn(conn)  # per-thread cursors for request handlers
    application.state.boot = db.record_boot(conn)
    application.state.master_key = None       # set only after setup/unlock
    application.state.secret_store = None
    application.state.kb = None
    application.state.history = None
    application.state.memory = None
    application.state.planner = None
    application.state.audit = None
    application.state.approvals = None
    application.state.session_id = None
    application.state.schedules = None
    application.state.email = None              # GmailClient once connected
    application.state.email_oauth_pending = None  # in-flight OAuth handshake
    application.state.scheduler_stop = asyncio.Event()  # cooperative shutdown signal
    application.state.webrtc_stop = asyncio.Event()     # cooperative shutdown for remote access
    assert "boot_count" in application.state.boot, "boot state must include boot_count"


async def _drain_startup_tasks(tasks: tuple) -> None:
    """Signal-stop + await each background task with a fixed upper bound."""
    assert isinstance(tasks, tuple), "tasks must be a tuple"
    for task in tasks:  # fixed, bounded by the caller's tuple length
        if task is None:
            continue
        try:
            await asyncio.wait_for(task, timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass  # hung past the gateway timeout — proceed to close
        except Exception as exc:  # a crashed background task must not leak (B12)
            log.warning("task crashed: %s", exc)


def _make_lifespan(mcp):
    """Build the FastAPI lifespan context manager bound to ``mcp``."""
    assert mcp is not None, "mcp server required"

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Open the local DuckDB, migrate, record this boot; start locked."""
        db_path = db.resolve_db_path()
        if db.apply_pending_restore(db_path):  # swap in an uploaded backup before opening
            log.warning("applied a staged database restore at startup")
        conn = db.open_db(db_path)
        applied = db.run_migrations(conn)
        assert applied >= 0, "migration count must be non-negative"
        _init_app_state(application, conn)
        # Single long-lived pooled httpx client for gateway calls (B22). Stored on
        # the gateway module so per-call functions reuse it without each route
        # having to pass ``client=``; tests that don't set the pool keep using a
        # per-call client. The lifespan owns it — gateway code never closes it.
        application.state.gw_client = httpx.Client(
            base_url=gateway.gateway_url(), timeout=_GW_POOL_TIMEOUT
        )
        gateway.set_pool(application.state.gw_client)
        try:  # privacy control: Bifrost request logging must stay off (see gateway.py).
            # Runs even before unlock (compose guarantees bifrost is healthy first);
            # best-effort here — every unlock re-enforces it.
            gateway.ensure_gateway_privacy(application.state.gw_client)
        except Exception as exc:
            log.warning("gateway privacy enforcement skipped at startup: %s", exc)
        # Remote access dials out only once the user opts in by pairing a device (keeps
        # SECURITY.md's "off by default" true). SMARTBRAIN_WEBRTC_ENABLED overrides: "1" = always
        # on (activate now), "0" = fully disabled (no task); unset = lazy (waits for a pairing).
        application.state.webrtc_active = asyncio.Event()
        _webrtc_mode = os.environ.get("SMARTBRAIN_WEBRTC_ENABLED", "")
        if _webrtc_mode == "1":
            application.state.webrtc_active.set()
        async with mcp.session_manager.run():  # drive the MCP transport for this app
            runner = asyncio.create_task(_scheduler_loop(application))  # background scheduler
            webrtc = asyncio.create_task(_webrtc_loop(application)) if _webrtc_mode != "0" else None
            try:
                yield
            finally:
                application.state.scheduler_stop.set()  # let an in-flight tick drain
                application.state.webrtc_stop.set()
                pair = getattr(application.state, "pair_session", None)
                if pair is not None:  # end an in-flight pairing-by-code session cooperatively
                    pair["stop"].set()
                    application.state.pair_session = None
                await _drain_startup_tasks((runner, webrtc, pair["task"] if pair else None))
                gateway.set_pool(None)  # clear before closing so gateway funcs never see a closed pool
                application.state.gw_client.close()
                conn.close()

    return lifespan


def _install_routes(application: FastAPI) -> None:
    """Mount middleware + every API router on ``application`` (registration order matters)."""
    assert application is not None, "application required"
    application.add_middleware(HostGuard, allowed=_allowed_hosts())  # anti DNS-rebinding (case-insensitive)
    application.add_middleware(OriginGuard)  # anti cross-site drive-by against the local API
    serving.add_security_headers(application)  # tight CSP + hardening on every response
    for router in (
        account_router, chat_router, local_models_router, models_router, kb_router,
        history_router, memory_router, planner_router, agent_router, schedule_router,
        metrics_router, selfimprove_router, email_router, data_router, mcp_router, devices_router,
        vault_router, web_router,
    ):  # fixed, bounded
        application.include_router(router)


def create_app() -> FastAPI:
    """Build a fully-wired SmartBrain app (and its own MCP server instance)."""
    mcp = mcp_server.build_server(
        lambda: getattr(app.state, "kb", None),
        lambda: getattr(app.state, "vaults", None),  # so MCP KB tools tag imported-vault content
    )
    app = FastAPI(title="SmartBrain_3000", version=__version__, lifespan=_make_lifespan(mcp))
    _install_routes(app)
    # Read-only Knowledge for external tools; auth-gated by the MCP access token.
    app.mount("/mcp", mcp_server.auth_wrapped_app(mcp, lambda: _mcp_token(app)))

    @app.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        """Liveness probe: status + version, plus the legacy-launcher nudge flag.

        Modern (self-updating) launchers stamp every probe with an
        X-SmartBrain-Launcher header; recording that it was EVER seen is what lets
        the UI show pre-self-update users a one-time "update the desktop app"
        banner — and never show it again once a modern launcher is talking to us.
        Both the record and the read are best-effort: health must never fail over
        bookkeeping.
        """
        assert __version__, "version string must be non-empty"
        payload: dict[str, object] = {"status": "ok", "version": __version__}
        try:
            handshake = request.headers.get("x-smartbrain-launcher", "")
            conn = request.app.state.dbx
            if handshake and handshake[:32] != db.meta_get(conn, "launcher:version"):
                db.meta_set(conn, "launcher:version", handshake[:32])  # write only on change
            payload["launcher_update_needed"] = bool(
                runtime.in_container() and not db.meta_get(conn, "launcher:version")
            )
        except Exception:  # health stays a liveness probe first
            payload["launcher_update_needed"] = False
        try:
            # The SPA reports its IANA timezone the same way — it's what lets the
            # chat time note speak the user's local time instead of bare UTC.
            tz = request.headers.get("x-smartbrain-timezone", "")
            if tz and len(tz) <= 64:
                conn = request.app.state.dbx
                if tz != db.meta_get(conn, "user:timezone"):
                    zoneinfo.ZoneInfo(tz)  # validates; garbage raises -> not stored
                    db.meta_set(conn, "user:timezone", tz)
        except Exception:
            pass
        # The launcher's half of the update handshake. It rides this probe (~every 30s) so
        # the page can offer an install where the owner is actually looking, instead of only
        # in a menu behind a tray icon. Both facts live in PROCESS memory, never the
        # database: a restart is exactly what an install does, and clearing the request by
        # construction is what stops one restart from asking for another.
        staged = request.headers.get("x-smartbrain-update", "")[:32]
        if staged:
            request.app.state.update_ready = staged
        elif getattr(request.app.state, "update_ready", ""):
            request.app.state.update_ready = ""  # the launcher withdrew it (installed elsewhere)
        ready = getattr(request.app.state, "update_ready", "")
        if ready and ready != __version__:
            payload["update_ready"] = ready
            requested = getattr(request.app.state, "update_requested", "")
            if requested == ready:
                payload["update_requested"] = requested
        assert payload["status"] == "ok", "health payload must report ok"
        return payload

    @app.post("/api/update/install")
    def request_install(request: Request) -> dict[str, object]:
        """Ask the desktop launcher to install the update it has staged.

        Desktop-local ONLY. The remote bridge forwards anything under /api, so without this
        guard a paired phone — or any web page the owner happened to visit — could restart
        the machine's stack. A phone is shown the update exists; installing it stays a
        decision made at the desk.

        The request is a single string in process memory that the launcher claims on its
        next handshake (within ~30s). It cannot outlive a restart, which is precisely what
        an install is.
        """
        if request.headers.get("x-sb-local") != "1":
            raise HTTPException(status_code=403, detail="this endpoint is Desktop-local only")
        ready = getattr(request.app.state, "update_ready", "")
        if not ready or ready == __version__:
            raise HTTPException(status_code=409, detail="no update is staged to install")
        request.app.state.update_requested = ready
        return {"ok": True, "version": ready}

    @app.get("/api/status")
    def status(request: Request) -> dict[str, object]:
        """Report DB connectivity and this install's persisted identity."""
        boot = request.app.state.boot
        assert isinstance(boot, dict), "boot state must be a dict"
        assert "install_id" in boot, "boot state must include install_id"
        # Omit desktop_routing_id: it's the WebRTC broker routing key and must not leak here.
        public = {k: v for k, v in boot.items() if k != "desktop_routing_id"}
        return {"db": "ok", "version": __version__, **public}

    serving.mount_web(app)  # static shell + PWA + SPA fallback — registered LAST
    return app


app = create_app()
