"""Outbound MCP connector for Neural Interface (ni-format.md §22).

SmartBrain's MCP posture has always been INBOUND-only. §22 opens the one carefully
scoped outbound path the operator approved: fetch a card's payload from a server the
user explicitly configured, letting databases and the broader MCP ecosystem become
NI sources while credentials stay in the user's OWN server process (SmartBrain never
holds the DB password). This module owns two objects:

* :class:`ServerRegistry` — CRUD over a single sealed slot under the reserved
  ``__mcp_servers__`` snapshot id (LibraryStore precedent). The list of user-
  configured servers is desktop-local-only to edit; nothing agent-authored can
  create or mutate a server (a server config carries execution/connection
  authority, so the "explicit UI act" law from feeds applies).
* :func:`call_tool` — one ``tools/call`` per engine run via the installed ``mcp``
  package's async client. The engine is sync (threads), so we drive the async
  exchange inside :func:`asyncio.run` in a helper thread-safe way: one event loop
  per fetch (deterministic, no shared loop state; the app's main loop is
  uvicorn's — engine ticks never touch it).

Errors map to §22's host-free classes: ``mcp_unavailable`` (spawn/connect/init
failure), ``mcp_tool_error`` (tool returned isError or protocol error),
``mcp_timeout`` (wall-clock deadline).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from . import ni

log = logging.getLogger("smartbrain.ni.mcp")

# --- bounds (§22 — every one verifiable, every one refused when exceeded) --

# One sealed slot under the reserved snapshot id (LibraryStore precedent). No matching
# ``ni_items`` row exists, so board/list queries never surface the registry.
SERVERS_RESERVED_ID = "__mcp_servers__"
SERVERS_SLOT = "servers"

MAX_SERVERS = 10                        # §22: bounded user server count
MAX_LABEL = 100                         # display label upper bound
MAX_COMMAND = 500                       # stdio.command path/name upper bound
MAX_ARGS = 20                           # stdio.args list length upper bound
MAX_ARG = 200                           # per-arg upper bound
MAX_URL = 2000                          # http url upper bound (matches ni._MAX_URL)
_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http"})

# §22 execution bounds — wall-clock cap on ONE call, and the joined-text result cap.
DEFAULT_CALL_TIMEOUT_S = 20.0
MAX_RESULT_TEXT_BYTES = 200 * 1024      # §22: joined text content capped at 200 KB
# Bounded concurrency for live outbound MCP calls. A wedged call thread continues to
# hold its slot (the release lives in the worker's finally) — that IS the point: the
# cap bounds LIVE threads so a pathological server can't grow the process's thread
# count without bound, wedged ones DO count against the cap.
_MAX_CONCURRENT_CALLS = 4
_LIVE_CALLS = threading.Semaphore(_MAX_CONCURRENT_CALLS)
# httpx.AsyncClient timeout for the http transport. Owned locally (not imported from
# mcp.shared._httpx_utils, which is a private path) — matches the MCP defaults so the
# SSE read leg has room, while stated verbatim here.
_HTTP_CONNECT_TIMEOUT_S = 30.0
_HTTP_READ_TIMEOUT_S = 300.0

# Env keys the child process must NEVER inherit — the credential firewall.
_FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = ("SMARTBRAIN_", "ANTHROPIC_")
# Minimal safe env passthrough (mirrors mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS but
# built explicitly so the invariant asserts locally without depending on package
# internals). Extended with LANG/LC_ALL so the child's stdout stays UTF-8-decodable.
# Drift guard: a test in test_ni_mcp.py asserts every key in the installed package's
# DEFAULT_INHERITED_ENV_VARS lives in this set — so a package bump that widens the
# safe list is caught here instead of silently forbidding what the package expects.
_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP",
    "USERNAME", "USERPROFILE",
    "HOME", "LOGNAME", "SHELL", "TERM", "USER",
    "LANG", "LC_ALL",
})

# Arguments in the sealed spec are frozen (§22: ``tool`` + ``arguments`` are FROZEN
# literal JSON, no ``{{param:}}``, no ``$secret``). Enforced by ``ni._validate_mcp_source``
# — this module trusts the spec is validated before it lands here.
MAX_ARGUMENTS_BYTES = 8 * 1024


class NIMcpError(Exception):
    """A host-free failure class for the caller (``ni._fetch_mcp``) to map to NIError.

    ``kind`` is one of ``mcp_unavailable`` / ``mcp_tool_error`` / ``mcp_timeout`` per
    §22. ``detail`` is a short host-free class string (never the URL, host, or bytes).
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        assert kind in {"mcp_unavailable", "mcp_tool_error", "mcp_timeout"}, "bad kind"
        assert isinstance(detail, str), "detail must be a string"
        super().__init__(kind if not detail else f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


# --- server config validation (used by ServerRegistry.add/update) ------------

def _validate_common(config: dict) -> None:
    """Shape-check the fields that BOTH transports carry — label, transport, enabled.

    The id + transport-specific fields are validated by the caller (add/update runs
    ``_validate_stdio`` or ``_validate_http`` after this).
    """
    assert isinstance(config, dict), "config must be a dict"
    label = config.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError("label must be a non-empty string")
    if len(label) > MAX_LABEL:
        raise ValueError(f"label exceeds {MAX_LABEL} chars")
    transport = config.get("transport")
    if transport not in _TRANSPORTS:
        raise ValueError(f"transport must be one of {sorted(_TRANSPORTS)}")
    enabled = config.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")  # noqa: TRY004 — one exception class per validator (parity with ni.py)


def _validate_stdio(config: dict) -> None:
    """stdio requires ``command`` (str) + ``args`` (list[str]); refuse extras."""
    assert isinstance(config, dict), "config must be a dict"
    command = config.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError("stdio.command must be a non-empty string")
    if len(command) > MAX_COMMAND:
        raise ValueError(f"stdio.command exceeds {MAX_COMMAND} chars")
    args = config.get("args")
    if not isinstance(args, list):
        raise ValueError("stdio.args must be a list")  # noqa: TRY004 — one exception class per validator
    if len(args) > MAX_ARGS:
        raise ValueError(f"stdio.args exceeds {MAX_ARGS} entries")
    for i, arg in enumerate(args):  # bounded by MAX_ARGS
        if not isinstance(arg, str):
            raise ValueError(f"stdio.args[{i}] must be a string")  # noqa: TRY004
        if len(arg) > MAX_ARG:
            raise ValueError(f"stdio.args[{i}] exceeds {MAX_ARG} chars")


def _validate_http(config: dict) -> None:
    """http requires ``url`` starting with http(s)://, no userinfo (auth stays server-side)."""
    assert isinstance(config, dict), "config must be a dict"
    url = config.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("http.url must be a non-empty string")
    if len(url) > MAX_URL:
        raise ValueError(f"http.url exceeds {MAX_URL} chars")
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        raise ValueError("http.url must start with http:// or https://")
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("http.url must include a host")
    # userinfo (user:pass@host) would smuggle auth into the sealed spec — refuse it;
    # the user's OWN server owns credentials per §22.
    if parsed.username or parsed.password:
        raise ValueError("http.url must not carry userinfo (credentials belong in the server)")


def _validate_new_config(config: dict) -> dict:
    """Validate an add/update body; return a canonical dict fit to seal.

    Refuses unknown keys and enforces the transport-specific shape. Callers pass
    the *desired* config; the server id is minted by the registry (not by the caller)
    so an add cannot squat on a chosen id.
    """
    assert isinstance(config, dict), "config must be a dict"
    allowed = {"label", "transport", "enabled", "command", "args", "url"}
    extra = set(config.keys()) - allowed
    if extra:
        raise ValueError(f"unknown keys in server config: {sorted(extra)}")
    _validate_common(config)
    transport = config["transport"]
    if transport == "stdio":
        _validate_stdio(config)
        if "url" in config:
            raise ValueError("stdio config must not carry a url field")
        return {"label": config["label"], "transport": "stdio",
                "command": config["command"],
                "args": list(config["args"]),  # freeze order + shape
                "enabled": bool(config["enabled"])}
    # http (the only other transport per _TRANSPORTS)
    _validate_http(config)
    if "command" in config or "args" in config:
        raise ValueError("http config must not carry command/args fields")
    return {"label": config["label"], "transport": "http",
            "url": config["url"],
            "enabled": bool(config["enabled"])}


# --- ServerRegistry ---------------------------------------------------------

class ServerRegistry:
    """The single MCP server registry (§22). Sealed under one reserved snapshot slot.

    Rows are held in a plain list body ``{"servers": [{...}]}``. A URL / command leak
    reveals the user's target infrastructure, so both live sealed at rest (feed +
    library subscription precedent). Every list return copies to prevent callers
    from mutating the stored list in place.
    """

    def __init__(self, ni_store: ni.NIStore) -> None:
        assert ni_store is not None, "NIStore required"
        self._ni = ni_store

    def _read(self) -> list[dict]:
        """Decrypt the sealed slot and return the (fresh) list of server configs."""
        row = self._ni.read_reserved_snapshot(SERVERS_RESERVED_ID, SERVERS_SLOT)
        if row is None:
            return []
        body = row.get("payload") or {}
        servers = body.get("servers") if isinstance(body, dict) else None
        if not isinstance(servers, list):
            return []
        return [dict(s) for s in servers if isinstance(s, dict)]  # bounded by MAX_SERVERS

    def _write(self, servers: list[dict]) -> None:
        """Seal the list under the reserved slot; refuse when the bound is exceeded."""
        assert isinstance(servers, list), "servers must be a list"
        if len(servers) > MAX_SERVERS:
            raise ValueError(f"server registry exceeds {MAX_SERVERS} entries")
        self._ni.write_reserved_snapshot(
            SERVERS_RESERVED_ID, SERVERS_SLOT, {"servers": servers},
        )

    def list_servers(self) -> list[dict]:
        """A fresh copy of every stored server config."""
        return self._read()

    def get(self, server_id: str) -> dict | None:
        """One server by id, or None. Returned dict is a copy — safe to mutate."""
        assert isinstance(server_id, str) and server_id, "server id required"
        for row in self._read():  # bounded by MAX_SERVERS
            if row.get("id") == server_id:
                return dict(row)
        return None

    def add(self, config: dict) -> dict:
        """Mint a fresh id, seal the entry, return the persisted row."""
        assert isinstance(config, dict), "config must be a dict"
        validated = _validate_new_config(config)
        current = self._read()
        if len(current) >= MAX_SERVERS:
            raise ValueError(f"server limit reached ({MAX_SERVERS})")
        server_id = str(uuid.uuid4())
        row = {"id": server_id, **validated}
        current.append(row)
        self._write(current)
        return dict(row)

    def update(self, server_id: str, config: dict) -> dict:
        """Replace one server's config (id fixed). Returns the persisted row."""
        assert isinstance(server_id, str) and server_id, "server id required"
        validated = _validate_new_config(config)
        current = self._read()
        for i, row in enumerate(current):  # bounded by MAX_SERVERS
            if row.get("id") == server_id:
                new_row = {"id": server_id, **validated}
                current[i] = new_row
                self._write(current)
                return dict(new_row)
        raise KeyError(server_id)

    def delete(self, server_id: str) -> None:
        """Drop one server. Raises KeyError when absent (caller maps to 404)."""
        assert isinstance(server_id, str) and server_id, "server id required"
        current = self._read()
        remaining = [row for row in current if row.get("id") != server_id]
        if len(remaining) == len(current):
            raise KeyError(server_id)
        self._write(remaining)


# --- call_tool: sync facade over the mcp async client -----------------------

def _stripped_env() -> dict[str, str]:
    """The env dict passed to a stdio child.

    Two invariants:
      1. NEVER contains a SMARTBRAIN_* / ANTHROPIC_* key (the credential firewall).
      2. Only carries a hand-picked, small set of vars the child likely needs to
         run (PATH for the launcher, HOME/USER for tools that read them,
         locale/system dirs on Windows). Nothing else rides through.
    """
    forbidden = _FORBIDDEN_ENV_PREFIXES
    out: dict[str, str] = {}
    for k, v in os.environ.items():  # bounded by process env size
        if any(k.startswith(pfx) for pfx in forbidden):
            continue
        if k in _SAFE_ENV_KEYS and isinstance(v, str):
            out[k] = v
    for k in out:
        assert not k.startswith(forbidden), f"stripped env leaked forbidden key: {k}"
    return out


@asynccontextmanager
async def _client_streams(server: dict, cwd: str):
    """Open the transport-specific stream pair for ``server`` and yield (read, write).

    stdio: spawn the user's command in a fresh process group (start_new_session=True
    inside mcp.client.stdio._create_platform_compatible_process) with a stripped env
    and a private cwd; the mcp package's context manager owns teardown (close stdin
    → wait up to 2s → SIGTERM/SIGKILL escalation via the process group). §22 credential
    firewall lives HERE (env dict).

    http: connect to the user's URL. §22 verbatim rationale — this path deliberately
    does NOT ride netguard: the entire point of the outbound MCP feature is the user's
    OWN loopback/LAN server, which netguard categorically (and rightly) blocks for
    anonymous fetches. The consent-scoped exception lives HERE ONLY: the address was
    typed by the user in a desktop-local act, is frozen thereafter, and nothing
    model-authored can ever reach this code path with a different address. Redirects
    are REFUSED by owning our own ``httpx.AsyncClient(follow_redirects=False)`` and
    passing it in — the mcp package's default client hardcodes ``follow_redirects=True``
    (mcp/shared/_httpx_utils.py:create_mcp_http_client), so a 3xx would silently rewrite
    the host away from the address the user consented to. With our client owned, a 3xx
    now surfaces as a plain transport error → ``mcp_unavailable``.
    """
    assert isinstance(server, dict), "server must be a dict"
    assert isinstance(cwd, str) and cwd, "cwd required"
    transport = server.get("transport")
    if transport == "stdio":
        params = StdioServerParameters(
            command=server["command"],
            args=list(server.get("args") or []),
            env=_stripped_env(),
            cwd=cwd,
        )
        async with stdio_client(params) as (read, write):
            yield read, write
        return
    if transport == "http":
        # streamable_http_client is the current non-deprecated context manager. When
        # ``http_client=`` is provided the package leaves its lifecycle to us — we own
        # the ``async with`` so the client's connections are always closed on exit.
        timeout = httpx.Timeout(_HTTP_CONNECT_TIMEOUT_S, read=_HTTP_READ_TIMEOUT_S)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client, \
                streamable_http_client(server["url"], http_client=client) as (read, write, _sid):
            yield read, write
        return
    raise NIMcpError("mcp_unavailable", "unknown transport")


def _shape_result(content_items: list, cap: int) -> dict:
    """Join the CallToolResult.content text items, cap, JSON-sniff.

    Text items only — image/audio/tool-use/resource references are IGNORED (§22 v1:
    the payload contract is a JSON dict/list or a text blob; no binary in the sealed
    snapshot). Each item is capped at ``cap`` bytes standalone (F3 audit: a pathological
    many-item result could otherwise grow the join unbounded); the running joined size
    is tracked as we iterate and the walk stops the moment ``cap`` is reached, so no
    intermediate string ever exceeds the ceiling.
    """
    assert isinstance(content_items, list), "content_items must be a list"
    assert isinstance(cap, int) and cap > 0, "cap must be positive"
    parts: list[str] = []
    running = 0
    for item in content_items:  # bounded by the mcp result shape
        if getattr(item, "type", None) != "text":
            continue
        text = getattr(item, "text", "")
        if not isinstance(text, str) or not text:
            continue
        text = _cap_utf8(text, cap)  # per-item cap: kills large-item pathology
        sep_len = 1 if parts else 0
        remaining = cap - running - sep_len
        if remaining <= 0:
            break
        chunk_bytes = text.encode("utf-8")
        if len(chunk_bytes) > remaining:
            text = _cap_utf8(text, remaining)
            chunk_bytes = text.encode("utf-8")
        parts.append(text)
        running += sep_len + len(chunk_bytes)
        if running >= cap:
            break
    joined = "\n".join(parts)
    try:
        parsed = json.loads(joined)
    except (ValueError, TypeError):
        return {"text": joined}
    if isinstance(parsed, (dict, list)):
        return {"data": parsed, "text": joined}
    return {"text": joined}


def _cap_utf8(text: str, cap: int) -> str:
    """Return ``text`` truncated at ``cap`` UTF-8 bytes on a valid codepoint boundary."""
    assert isinstance(text, str), "text must be a string"
    assert isinstance(cap, int) and cap > 0, "cap must be positive"
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    return encoded[:cap].decode("utf-8", errors="ignore")


async def _drive_exchange(server: dict, tool: str, arguments: dict, cwd: str,
                          cap: int) -> dict:
    """Open the transport, initialize the session, call the tool, shape the result.

    Runs inside ``asyncio.run`` from :func:`call_tool` — one event loop per fetch.
    Every failure class maps to :class:`NIMcpError` here so the sync caller sees a
    single exception type (never a raw MCP protocol / transport error).
    """
    assert isinstance(server, dict), "server must be a dict"
    assert isinstance(tool, str) and tool, "tool required"
    try:
        async with _client_streams(server, cwd) as (read, write), \
                ClientSession(read, write) as session:
            try:
                await session.initialize()
            except Exception as exc:  # protocol init failure
                raise NIMcpError("mcp_unavailable",
                                 exc.__class__.__name__) from None
            try:
                result = await session.call_tool(tool, arguments)
            except Exception as exc:  # tool call transport failure
                raise NIMcpError("mcp_tool_error",
                                 exc.__class__.__name__) from None
            if getattr(result, "isError", False):
                raise NIMcpError("mcp_tool_error", "server reported isError")
            return _shape_result(list(result.content or []), cap)
    except BaseExceptionGroup as group:  # anyio task-group teardown wraps ours
        buried = _extract_nimcperror(group)
        raise buried from None
    except NIMcpError:
        raise
    except Exception as exc:  # spawn / connect failure lives here
        raise NIMcpError("mcp_unavailable", exc.__class__.__name__) from None


def _extract_nimcperror(group: BaseExceptionGroup) -> NIMcpError:
    """Find the NIMcpError inside an anyio task-group ExceptionGroup, or synthesize
    ``mcp_unavailable`` when the group carries only lower-level transport errors.

    anyio wraps every exception raised inside a running task group into a
    ``BaseExceptionGroup`` at ``__aexit__`` time (Python 3.11+). Our own
    ``NIMcpError`` is the one we care about — the surrounding wrappers are
    transport / cleanup errors we've already classified elsewhere.
    """
    assert isinstance(group, BaseExceptionGroup), "group must be an ExceptionGroup"
    matched, _rest = group.split(NIMcpError)
    if matched is not None:
        for exc in _flatten(matched):  # bounded by the group's tree size
            if isinstance(exc, NIMcpError):
                return exc
    return NIMcpError("mcp_unavailable", group.__class__.__name__)


def _flatten(group: BaseExceptionGroup) -> list[BaseException]:
    """Return a flat list of leaf exceptions inside ``group`` (never nested groups)."""
    assert isinstance(group, BaseExceptionGroup), "group must be an ExceptionGroup"
    out: list[BaseException] = []
    # Bounded traversal: task groups are shallow (one level per nested ``async with``);
    # the loop cap defends against a pathological structure without adding recursion.
    stack: list[BaseException] = [group]
    for _ in range(64):
        if not stack:
            break
        current = stack.pop()
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        else:
            out.append(current)
    return out


def _run_in_thread(coro_fn, timeout_s: float) -> dict:
    """Run ``coro_fn()`` on a fresh event loop in a helper thread; timeout aware.

    The app's main asyncio loop is uvicorn's — engine ticks live in scheduler threads,
    so they must NEVER touch it. Running a fresh ``asyncio.run`` on a worker thread is
    deterministic and lets the wall-clock watchdog fire without loop cross-talk.
    A wrapper around ``asyncio.wait_for`` bounds the whole exchange (init + call +
    teardown) at ``timeout_s`` seconds.

    Concurrency cap (F5 audit): acquires one slot on ``_LIVE_CALLS`` before spawning
    the worker; refuses with ``mcp_unavailable`` when the cap is exhausted. The slot
    is released inside the worker's ``finally`` — so a wedged thread that outlives
    ``thread.join`` continues to hold its slot until it eventually dies. That IS the
    point: the cap bounds LIVE threads, and wedged ones count against the cap.
    """
    assert callable(coro_fn), "coro_fn must be callable"
    assert timeout_s > 0, "timeout must be positive"
    if not _LIVE_CALLS.acquire(blocking=False):
        raise NIMcpError("mcp_unavailable", "connector busy")
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(
                asyncio.wait_for(coro_fn(), timeout=timeout_s),
            )
        except TimeoutError:
            box["error"] = NIMcpError("mcp_timeout")
        except NIMcpError as exc:
            box["error"] = exc
        except Exception as exc:  # never let a raw exception cross the thread boundary
            box["error"] = NIMcpError("mcp_unavailable", exc.__class__.__name__)
        finally:
            _LIVE_CALLS.release()

    thread = threading.Thread(target=_target, name="ni-mcp-call", daemon=True)
    thread.start()
    # Bound the wait a few seconds beyond the deadline so a hung teardown still returns
    # (the child process group has already been signalled by the mcp package's exit
    # path — this bound protects the caller from a completely wedged thread).
    thread.join(timeout=timeout_s + 5.0)
    if thread.is_alive():
        # The exchange is still running past its wall-clock ceiling — the stdio_client
        # / streamable_http_client teardown will eventually reap the child (killpg on
        # posix, Job Object on Windows), but we surface a timeout to the caller now
        # rather than block the tick.
        raise NIMcpError("mcp_timeout", "thread join exceeded deadline")
    if "error" in box:
        raise box["error"]
    result = box.get("result")
    assert isinstance(result, dict), "coro must yield a dict payload"
    return result


def call_tool(server: dict, tool: str, arguments: dict, *,
              timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> dict:
    """Run one MCP ``tools/call`` against ``server`` and return the shaped payload.

    Contract:
      * server is a validated registry row (id + label + transport + transport-
        specific fields + enabled);
      * ``tool`` is a slug (validated by the caller — ``ni._validate_mcp_source``);
      * ``arguments`` is a JSON-serializable dict already frozen in the sealed spec
        (no ``{{param:}}``, no ``$secret`` — enforced by the source validator);
      * ``timeout_s`` bounds the whole exchange (init + call + teardown); on expiry
        the child process group is reaped by the mcp package's own teardown path.

    Returns ``{"text": raw}`` when the joined text does not parse as JSON, else
    ``{"data": parsed, "text": raw}`` for dict/list JSON. Every failure raises
    :class:`NIMcpError` with one of the three host-free classes.
    """
    assert isinstance(server, dict), "server must be a dict"
    assert isinstance(tool, str) and tool, "tool required"
    assert isinstance(arguments, dict), "arguments must be a dict"
    assert isinstance(timeout_s, (int, float)) and timeout_s > 0, "positive timeout required"
    transport = server.get("transport")
    if transport not in _TRANSPORTS:
        raise NIMcpError("mcp_unavailable", "bad transport")
    # Private cwd for the stdio child — never the shared tmp where another local user
    # could pre-place .mcp.json for the child to trip on (claudecli._private_cwd
    # precedent). Removed on exit whether the call succeeded or timed out.
    cwd = tempfile.mkdtemp(prefix="smartbrain-ni-mcp-")
    try:
        try:
            os.chmod(cwd, 0o700)
        except OSError:
            pass  # best-effort — mkdtemp already yields 0o700 on posix
        return _run_in_thread(
            lambda: _drive_exchange(server, tool, dict(arguments), cwd,
                                    MAX_RESULT_TEXT_BYTES),
            timeout_s,
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "MAX_ARG",
    "MAX_ARGS",
    "MAX_ARGUMENTS_BYTES",
    "MAX_COMMAND",
    "MAX_LABEL",
    "MAX_RESULT_TEXT_BYTES",
    "MAX_SERVERS",
    "MAX_URL",
    "SERVERS_RESERVED_ID",
    "SERVERS_SLOT",
    "NIMcpError",
    "ServerRegistry",
    "call_tool",
]

# --- Notes on the installed mcp client API (verified against mcp 1.28.0) ---
#
# * ClientSession(read_stream, write_stream) is an async context manager
#   (``async with`` — mcp/shared/session.py:221). initialize() is async and
#   sends the InitializeRequest; call_tool(name, arguments) returns a
#   CallToolResult (mcp/client/session.py:160, 386). CallToolResult has fields
#   ``content: list[ContentBlock]`` + ``isError: bool`` (mcp/types.py:1363).
#   Text items expose ``.type == "text"`` and ``.text: str`` (mcp/types.py:1026).
# * stdio_client(StdioServerParameters(...)) is an asynccontextmanager that
#   yields (read_stream, write_stream) (mcp/client/stdio/__init__.py:99).
#   Passing ``env=`` merges over ``mcp.client.stdio.get_default_environment()``
#   (the intersection of DEFAULT_INHERITED_ENV_VARS with os.environ); we build a
#   stripped dict from a hand-picked subset instead so no SMARTBRAIN_* /
#   ANTHROPIC_* ever rides through. Cwd override is supported. Teardown IS
#   guaranteed by the package: close
#   stdin → wait up to PROCESS_TERMINATION_TIMEOUT (2s) → posix killpg via
#   ``terminate_posix_process_tree`` (children spawned with
#   ``start_new_session=True`` so the process group is atomic). We do NOT need
#   a killpg fallback — the package is already correct.
# * streamable_http_client(url) is the current non-deprecated http transport
#   (mcp/client/streamable_http.py:600). The old streamablehttp_client symbol
#   is deprecated; we use the new one. httpx.AsyncClient lifecycle is owned by
#   the context manager, so a bounded ``asyncio.wait_for`` around the whole
#   exchange also bounds the http connect.
