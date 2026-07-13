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
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from starlette.responses import PlainTextResponse

from . import __version__, db, gateway, mcp_server, scheduler, serving
from .account import router as account_router
from .chat_routes import router as chat_router
from .local_models_routes import router as local_models_router
from .models_routes import router as models_router
from .kb_routes import router as kb_router
from .history_routes import router as history_router
from .memory_routes import router as memory_router
from .planner_routes import router as planner_router
from .agent_routes import router as agent_router
from .schedule_routes import router as schedule_router
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
    serving.add_security_headers(application)  # tight CSP + hardening on every response
    for router in (
        account_router, chat_router, local_models_router, models_router, kb_router,
        history_router, memory_router, planner_router, agent_router, schedule_router,
        email_router, data_router, mcp_router, devices_router, vault_router,
    ):  # fixed, bounded
        application.include_router(router)


def create_app() -> FastAPI:
    """Build a fully-wired SmartBrain app (and its own MCP server instance)."""
    mcp = mcp_server.build_server(lambda: getattr(app.state, "kb", None))
    app = FastAPI(title="SmartBrain_3000", version=__version__, lifespan=_make_lifespan(mcp))
    _install_routes(app)
    # Read-only Knowledge for external tools; auth-gated by the MCP access token.
    app.mount("/mcp", mcp_server.auth_wrapped_app(mcp, lambda: _mcp_token(app)))

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """Liveness probe: report a fixed status plus the running version."""
        assert __version__, "version string must be non-empty"
        payload = {"status": "ok", "version": __version__}
        assert payload["status"] == "ok", "health payload must report ok"
        return payload

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
