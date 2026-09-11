"""Outbound MCP connector for Neural Interface (ni-format.md §22).

Drives the real ``mcp`` package end-to-end via a tiny in-tree FastMCP stdio server
spawned per test — proves the shipped code walks the actual client API (initialize
→ tools/call → shape → teardown). One narrowly scoped test uses the connector to
verify env stripping (the echo server dumps its os.environ back).

Every fixture is hermetic: fresh in-memory DuckDB, fresh master key, tmp working
directory. No network reaches the internet from any test — the stdio child is a
python subprocess this test authored.
"""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import socketserver
import sys
import textwrap
import threading
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni, ni_mcp
from smartbrain_3000.secrets import gen_master_key

_LOCAL = {"x-sb-local": "1"}


# --- fixtures ----------------------------------------------------------------

def _store() -> tuple[ni.NIStore, duckdb.DuckDBPyConnection, bytes]:
    """A hermetic NIStore over a fresh in-memory DuckDB with migrations applied."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return ni.NIStore(conn, key), conn, key


def _stdio_server_script(tmp_path: pathlib.Path, body: str) -> str:
    """Write a minimal FastMCP stdio server to ``tmp_path`` and return its path.

    Every test that drives ``ni_mcp.call_tool`` end-to-end injects a fresh script
    so the tool implementations can vary per test (echo, JSON, error, stall) —
    all through the SAME shipped client code path.
    """
    assert isinstance(body, str) and body, "server body required"
    assert isinstance(tmp_path, pathlib.Path), "tmp_path required"
    script = tmp_path / "server.py"
    header = textwrap.dedent(
        """
        from mcp.server.fastmcp import FastMCP
        server = FastMCP('test')
        """,
    ).strip() + "\n"
    footer = "\nif __name__ == '__main__':\n    server.run(transport='stdio')\n"
    script.write_text(header + textwrap.dedent(body) + footer, encoding="utf-8")
    return str(script)


def _stdio_config(tmp_path: pathlib.Path, body: str, *,
                  label: str = "test-server", enabled: bool = True) -> dict:
    """Return a validated stdio server dict pointed at a fresh FastMCP script."""
    assert isinstance(label, str), "label required"
    return {
        "id": "test-server-id",
        "label": label,
        "transport": "stdio",
        "command": sys.executable,
        "args": [_stdio_server_script(tmp_path, body)],
        "enabled": enabled,
    }


# --- ServerRegistry: CRUD + bounds ------------------------------------------

def test_registry_add_lists_and_gets() -> None:
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    row = reg.add({"label": "mine", "transport": "stdio",
                   "command": "/usr/bin/env", "args": ["mcp-server"],
                   "enabled": True})
    assert row["id"] and row["label"] == "mine" and row["transport"] == "stdio"
    servers = reg.list_servers()
    assert len(servers) == 1 and servers[0]["id"] == row["id"]
    got = reg.get(row["id"])
    assert got is not None and got["command"] == "/usr/bin/env"


def test_registry_update_replaces_config() -> None:
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    row = reg.add({"label": "a", "transport": "http",
                   "url": "http://127.0.0.1:9000/mcp", "enabled": True})
    updated = reg.update(row["id"], {"label": "b", "transport": "http",
                                     "url": "http://127.0.0.1:9001/mcp",
                                     "enabled": False})
    assert updated["id"] == row["id"] and updated["label"] == "b"
    assert updated["url"].endswith(":9001/mcp") and updated["enabled"] is False


def test_registry_update_unknown_raises_keyerror() -> None:
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    with pytest.raises(KeyError):
        reg.update("no-such-id", {"label": "x", "transport": "stdio",
                                   "command": "/bin/true", "args": [],
                                   "enabled": True})


def test_registry_delete_removes_row() -> None:
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    row = reg.add({"label": "a", "transport": "stdio",
                   "command": "/bin/true", "args": [], "enabled": True})
    reg.delete(row["id"])
    assert reg.get(row["id"]) is None
    with pytest.raises(KeyError):
        reg.delete(row["id"])


def test_registry_refuses_over_bound() -> None:
    """MAX_SERVERS caps the registry — the 11th add raises ValueError."""
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    for i in range(ni_mcp.MAX_SERVERS):  # bounded
        reg.add({"label": f"srv-{i}", "transport": "stdio",
                 "command": "/bin/true", "args": [], "enabled": True})
    with pytest.raises(ValueError, match="server limit"):
        reg.add({"label": "one-too-many", "transport": "stdio",
                 "command": "/bin/true", "args": [], "enabled": True})


def test_registry_refuses_bad_transport_shape() -> None:
    """Every field validator has its say; garbage returns 400-shape (ValueError)."""
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    # http config carrying stdio fields — rejected
    with pytest.raises(ValueError, match="http config"):
        reg.add({"label": "x", "transport": "http",
                 "url": "http://127.0.0.1/mcp", "command": "/bin/true",
                 "enabled": True})
    # Unknown transport
    with pytest.raises(ValueError, match="transport must be"):
        reg.add({"label": "x", "transport": "carrier-pigeon", "enabled": True})
    # http url with userinfo — refused (credentials belong in the server)
    with pytest.raises(ValueError, match="userinfo"):
        reg.add({"label": "x", "transport": "http",
                 "url": "http://user:pass@host/mcp", "enabled": True})
    # stdio with bad args
    with pytest.raises(ValueError, match="stdio.args"):
        reg.add({"label": "x", "transport": "stdio",
                 "command": "/bin/true", "args": "not a list",  # type: ignore[arg-type]
                 "enabled": True})


def test_registry_sealed_at_rest() -> None:
    """The raw DuckDB bytes for the reserved slot MUST NOT contain plaintext command/url.

    Sealed body (AES-GCM) is opaque bytes; a naive substring scan is enough to
    prove the plaintext never touches the row.
    """
    store, conn, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    reg.add({"label": "secret-host", "transport": "http",
             "url": "http://internal.example.com/mcp/xyz",
             "enabled": True})
    row = conn.execute(
        "SELECT nonce, ciphertext FROM ni_snapshots WHERE item_id = ? AND slot = ?;",
        [ni_mcp.SERVERS_RESERVED_ID, ni_mcp.SERVERS_SLOT],
    ).fetchone()
    assert row is not None, "sealed row must exist after add"
    raw = bytes(row[0]) + bytes(row[1])
    assert b"internal.example.com" not in raw
    assert b"http://internal.example.com/mcp/xyz" not in raw
    assert b"secret-host" not in raw


def test_registry_reserved_id_never_shows_in_item_list() -> None:
    """LibraryStore precedent: reserved snapshot rows must not surface via list_items."""
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    reg.add({"label": "a", "transport": "stdio",
             "command": "/bin/true", "args": [], "enabled": True})
    assert store.list_items() == []  # no ni_items row was created


# --- _validate_mcp_source: spec-time refusals -------------------------------

def _min_source(**over) -> dict:
    """A minimal valid mcp_tool source (used to fuzz single-field failures)."""
    base = {"type": "mcp_tool", "server_id": "srv-a",
            "tool": "query", "arguments": {"sql": "SELECT 1"}}
    base.update(over)
    return base


def test_validator_accepts_minimal_mcp_source() -> None:
    ni._validate_mcp_source(_min_source())


def test_validator_refuses_placeholder_in_arguments() -> None:
    with pytest.raises(ValueError, match="frozen literal"):
        ni._validate_mcp_source(_min_source(
            arguments={"q": "{{param:x}}"}))


def test_validator_refuses_secret_ref_in_arguments() -> None:
    with pytest.raises(ValueError, match=r"\$secret"):
        ni._validate_mcp_source(_min_source(
            arguments={"token": {"$secret": "ni:x:t"}}))


def test_validator_refuses_bad_tool_name() -> None:
    with pytest.raises(ValueError, match="A-Za-z"):
        ni._validate_mcp_source(_min_source(tool="bad name with spaces"))


def test_validator_refuses_oversized_arguments() -> None:
    big = {"blob": "x" * (ni._MAX_MCP_ARGS_BYTES + 10)}
    with pytest.raises(ValueError, match="canonical bytes"):
        ni._validate_mcp_source(_min_source(arguments=big))


def test_validator_refuses_non_json_arguments() -> None:
    with pytest.raises(ValueError, match="JSON-serializable"):
        ni._validate_mcp_source(_min_source(arguments={"x": {1, 2, 3}}))


def test_validator_refuses_unknown_keys() -> None:
    src = _min_source()
    src["extra"] = "field"
    with pytest.raises(ValueError, match="unknown keys"):
        ni._validate_mcp_source(src)


# --- end-to-end: call_tool through stdio + FastMCP --------------------------

def test_call_tool_end_to_end_json(tmp_path: pathlib.Path) -> None:
    """Drives ClientSession + stdio_client against a real FastMCP server; the joined
    text parses as JSON so the payload contains both ``data`` and ``text`` (§22)."""
    server = _stdio_config(tmp_path, """
        import json
        @server.tool()
        def echo(payload: dict) -> str:
            return json.dumps({"got": payload})
    """)
    out = ni_mcp.call_tool(server, "echo", {"payload": {"a": 1}}, timeout_s=15.0)
    assert isinstance(out["data"], dict) and out["data"]["got"] == {"a": 1}
    assert "got" in out["text"]


def test_call_tool_end_to_end_plain_text(tmp_path: pathlib.Path) -> None:
    """A tool whose joined text is NOT JSON returns {'text': raw} only (no 'data')."""
    server = _stdio_config(tmp_path, """
        @server.tool()
        def hello() -> str:
            return "hi there"
    """)
    out = ni_mcp.call_tool(server, "hello", {}, timeout_s=15.0)
    assert out == {"text": "hi there"}


def test_call_tool_maps_tool_error(tmp_path: pathlib.Path) -> None:
    """A tool that raises → CallToolResult.isError=True → mcp_tool_error."""
    server = _stdio_config(tmp_path, """
        @server.tool()
        def bad() -> str:
            raise RuntimeError("nope")
    """)
    with pytest.raises(ni_mcp.NIMcpError) as info:
        ni_mcp.call_tool(server, "bad", {}, timeout_s=15.0)
    assert info.value.kind == "mcp_tool_error"


def test_call_tool_maps_timeout(tmp_path: pathlib.Path) -> None:
    """A tool that stalls past the deadline → mcp_timeout."""
    server = _stdio_config(tmp_path, """
        import time
        @server.tool()
        def slow() -> str:
            time.sleep(10)
            return "done"
    """)
    with pytest.raises(ni_mcp.NIMcpError) as info:
        ni_mcp.call_tool(server, "slow", {}, timeout_s=1.5)
    assert info.value.kind == "mcp_timeout"


def test_call_tool_env_strips_smartbrain_and_anthropic(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """The child process must never see SMARTBRAIN_* / ANTHROPIC_* env vars."""
    monkeypatch.setenv("SMARTBRAIN_MASTER", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nope")
    server = _stdio_config(tmp_path, """
        import os, json
        @server.tool()
        def dump_env() -> str:
            leaks = [k for k in os.environ
                     if k.startswith(('SMARTBRAIN_', 'ANTHROPIC_'))]
            return json.dumps({"leaks": leaks})
    """)
    out = ni_mcp.call_tool(server, "dump_env", {}, timeout_s=15.0)
    assert out["data"] == {"leaks": []}


def test_call_tool_unknown_transport_raises() -> None:
    """A malformed server dict fails fast with mcp_unavailable."""
    with pytest.raises(ni_mcp.NIMcpError) as info:
        ni_mcp.call_tool({"transport": "carrier-pigeon"}, "x", {}, timeout_s=1.0)
    assert info.value.kind == "mcp_unavailable"


def test_call_tool_spawn_failure_maps_unavailable(tmp_path: pathlib.Path) -> None:
    """A command that doesn't exist raises OSError inside stdio_client → mcp_unavailable."""
    server = {"id": "s", "label": "no-cmd", "transport": "stdio",
              "command": "/nonexistent/mcp/command",
              "args": [], "enabled": True}
    with pytest.raises(ni_mcp.NIMcpError) as info:
        ni_mcp.call_tool(server, "any", {}, timeout_s=5.0)
    assert info.value.kind == "mcp_unavailable"


def test_call_tool_caps_joined_text(tmp_path: pathlib.Path) -> None:
    """A server that returns > 200KB text has its joined body truncated to the cap."""
    server = _stdio_config(tmp_path, """
        @server.tool()
        def big() -> str:
            return "x" * (300 * 1024)
    """)
    out = ni_mcp.call_tool(server, "big", {}, timeout_s=15.0)
    assert len(out["text"].encode("utf-8")) <= ni_mcp.MAX_RESULT_TEXT_BYTES


# --- ni._fetch_mcp wiring ---------------------------------------------------

def test_fetch_mcp_disabled_server_raises_unavailable(tmp_path: pathlib.Path) -> None:
    """§22: an entry that exists but is disabled cannot run — the classes match spec."""
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    row = reg.add({"label": "srv", "transport": "stdio",
                   "command": "/bin/true", "args": [], "enabled": False})
    source = {"type": "mcp_tool", "server_id": row["id"],
              "tool": "echo", "arguments": {}}
    with pytest.raises(ni.NIError) as info:
        ni._fetch_mcp(source, store)
    assert info.value.kind == "mcp_unavailable"


def test_fetch_mcp_unknown_server_raises_unavailable() -> None:
    store, _, _ = _store()
    source = {"type": "mcp_tool", "server_id": "no-such",
              "tool": "echo", "arguments": {}}
    with pytest.raises(ni.NIError) as info:
        ni._fetch_mcp(source, store)
    assert info.value.kind == "mcp_unavailable"


def test_fetch_mcp_maps_nimcperror_kinds(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The wire from ni_mcp.NIMcpError → ni.NIError preserves the host-free class."""
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    row = reg.add({"label": "srv", "transport": "stdio",
                   "command": "/bin/true", "args": [], "enabled": True})

    def _raise_timeout(_srv, _tool, _args, timeout_s=None):
        raise ni_mcp.NIMcpError("mcp_timeout", "boom")

    from smartbrain_3000 import ni_mcp as _m
    monkeypatch.setattr(_m, "call_tool", _raise_timeout)
    source = {"type": "mcp_tool", "server_id": row["id"],
              "tool": "echo", "arguments": {}}
    with pytest.raises(ni.NIError) as info:
        ni._fetch_mcp(source, store)
    assert info.value.kind == "mcp_timeout"


# --- routes: /api/ni/mcp-servers CRUD + delete-in-use -----------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "mcp.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as tc:
        assert tc.post("/api/account/setup",
                       json={"passphrase": "correct-horse"}).status_code == 200
        yield tc


def test_routes_require_unlock(tmp_path, monkeypatch) -> None:
    """Every read/write path returns 423 while locked."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "locked.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as tc:
        assert tc.get("/api/ni/mcp-servers").status_code == 423
        r = tc.post("/api/ni/mcp-servers", json={"label": "x", "transport": "stdio",
                                                  "command": "/bin/true"},
                    headers=_LOCAL)
        assert r.status_code == 423


def test_routes_desktop_local_on_writes(client: TestClient) -> None:
    """POST/PUT/DELETE all require the desktop-local header (X-SB-Local)."""
    # POST without the header → 403
    r = client.post("/api/ni/mcp-servers",
                    json={"label": "x", "transport": "stdio", "command": "/bin/true"})
    assert r.status_code == 403
    # POST with the header succeeds
    r = client.post("/api/ni/mcp-servers", headers=_LOCAL,
                    json={"label": "x", "transport": "stdio",
                          "command": "/bin/true", "args": []})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    # PUT without the header → 403
    assert client.put(f"/api/ni/mcp-servers/{sid}",
                      json={"label": "y", "transport": "stdio",
                            "command": "/bin/true"}).status_code == 403
    # DELETE without the header → 403
    assert client.delete(f"/api/ni/mcp-servers/{sid}").status_code == 403


def test_routes_crud_roundtrip(client: TestClient) -> None:
    """POST → list → PUT → list → DELETE walks the whole surface."""
    r = client.post("/api/ni/mcp-servers", headers=_LOCAL,
                    json={"label": "a", "transport": "stdio",
                          "command": "/bin/true", "args": []})
    assert r.status_code == 200
    sid = r.json()["id"]
    listing = client.get("/api/ni/mcp-servers").json()
    assert len(listing["servers"]) == 1 and listing["servers"][0]["label"] == "a"
    r = client.put(f"/api/ni/mcp-servers/{sid}", headers=_LOCAL,
                   json={"label": "b", "transport": "stdio",
                         "command": "/bin/false", "args": ["--flag"],
                         "enabled": False})
    assert r.status_code == 200 and r.json()["label"] == "b"
    listing = client.get("/api/ni/mcp-servers").json()
    assert listing["servers"][0]["command"] == "/bin/false"
    assert listing["servers"][0]["enabled"] is False
    r = client.delete(f"/api/ni/mcp-servers/{sid}", headers=_LOCAL)
    assert r.status_code == 200
    assert client.get("/api/ni/mcp-servers").json()["servers"] == []


def test_delete_in_use_returns_409(client: TestClient) -> None:
    """An item referencing this server pins its registry entry — DELETE returns 409."""
    r = client.post("/api/ni/mcp-servers", headers=_LOCAL,
                    json={"label": "a", "transport": "stdio",
                          "command": "/bin/true", "args": []})
    sid = r.json()["id"]
    # Add an item pointing at this server (bypass tool chokepoint — store.add_item
    # only needs a valid spec; the sealed reference is what the scan sees).
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    spec = {"version": 1, "title": "t", "goal": "g", "params": {},
            "source": {"type": "mcp_tool", "server_id": sid,
                       "tool": "echo", "arguments": {}},
            "pipeline": [], "scene": scene, "display": {"size": "small"},
            "repair_policy": {"l1": True, "l2_frontier": False},
            "model": None}
    client.app.state.ni.add_item(spec, {"text": "preview"}, origin="user")
    r = client.delete(f"/api/ni/mcp-servers/{sid}", headers=_LOCAL)
    assert r.status_code == 409
    assert "reference" in r.json()["detail"]


def test_delete_unknown_returns_404(client: TestClient) -> None:
    r = client.delete("/api/ni/mcp-servers/no-such-id", headers=_LOCAL)
    assert r.status_code == 404


def test_add_bad_config_returns_400(client: TestClient) -> None:
    """Registry ValueError classes surface as 400 through the route wrapper."""
    r = client.post("/api/ni/mcp-servers", headers=_LOCAL,
                    json={"label": "x", "transport": "http",
                          "url": "not-a-url"})
    assert r.status_code == 400


def test_audit_row_metadata_only(client: TestClient) -> None:
    """§22: the audit trail may name label + transport, NEVER command / url."""
    r = client.post("/api/ni/mcp-servers", headers=_LOCAL,
                    json={"label": "internal", "transport": "http",
                          "url": "http://10.0.0.5:9000/mcp/secret-endpoint"})
    assert r.status_code == 200, r.text
    entries = client.get("/api/audit").json()["entries"]
    row = next(e for e in entries if e["tool"] == "ni_mcp_server_add")
    combined = json.dumps(row)
    assert "10.0.0.5" not in combined and "secret-endpoint" not in combined


def test_get_item_shows_mcp_provenance() -> None:
    """The tools provenance helper labels an mcp_tool source with the tool name."""
    from smartbrain_3000 import tools as tools_mod
    got = tools_mod._ni_source_provenance(
        {"type": "mcp_tool", "server_id": "s", "tool": "list-orders", "arguments": {}})
    assert "MCP" in got and "list-orders" in got


# --- teardown discipline: no orphan child dirs after a call -----------------

def test_call_tool_removes_private_cwd(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The per-fetch cwd (mkdtemp) MUST be rmtree-d on every exit path."""
    captured: list[str] = []
    real_mkdtemp = ni_mcp.tempfile.mkdtemp

    def _spy(**kwargs):
        got = real_mkdtemp(**kwargs)
        captured.append(got)
        return got

    monkeypatch.setattr(ni_mcp.tempfile, "mkdtemp", _spy)
    server = _stdio_config(tmp_path, """
        @server.tool()
        def ok() -> str:
            return "ok"
    """)
    ni_mcp.call_tool(server, "ok", {}, timeout_s=15.0)
    assert captured, "mkdtemp must have been called"
    for path in captured:
        assert not os.path.isdir(path), f"cwd not cleaned: {path}"


# --- HIGH F1: http transport refuses redirects ------------------------------

class _RedirectAndCanaryHandler(http.server.BaseHTTPRequestHandler):
    """Test-only handler: any request to ``/mcp`` returns 302 pointing at ``/canary``.

    A canary hit records the request into the server-scoped ``hits`` list — the test
    asserts the list stays empty, proving the mcp client never followed the 3xx.
    """

    def log_message(self, format: str, *args: object) -> None:
        # Silence stdlib http.server's stderr logging inside the test process.
        return

    def _handle(self) -> None:
        assert isinstance(self.path, str), "path must be a string"
        if self.path.startswith("/mcp"):
            self.send_response(302)
            self.send_header("Location", "/canary")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Anything else is a canary hit — record it and answer 200 so the test
        # would notice (but the test asserts we never get here).
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        # Drain any request body (streamable-http POSTs a JSON envelope) so the
        # client's send-side doesn't wedge on unread bytes when we redirect.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()


class _RedirectServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server that also carries a ``hits`` list for canary detection."""

    daemon_threads = True
    hits: list[str]


def _redirect_server() -> tuple[_RedirectServer, str]:
    """Bind a redirect+canary server on 127.0.0.1:<ephemeral>; return (server, url)."""
    server = _RedirectServer(("127.0.0.1", 0), _RedirectAndCanaryHandler)
    server.hits = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/mcp"


def test_http_transport_refuses_redirect() -> None:
    """F1: a 302 from the configured MCP url MUST NOT re-issue against the canary path.

    The mcp package's default http client hardcodes ``follow_redirects=True`` in
    ``create_mcp_http_client`` (mcp/shared/_httpx_utils.py). ni_mcp constructs its
    own ``httpx.AsyncClient(follow_redirects=False, ...)`` and passes it as
    ``http_client=`` to ``streamable_http_client`` so a 3xx surfaces as a plain
    transport error → ``mcp_unavailable`` before any request re-issues.
    """
    server, url = _redirect_server()
    try:
        config = {"id": "s", "label": "redir", "transport": "http",
                  "url": url, "enabled": True}
        with pytest.raises(ni_mcp.NIMcpError) as info:
            ni_mcp.call_tool(config, "any", {}, timeout_s=5.0)
        assert info.value.kind == "mcp_unavailable"
        assert server.hits == [], f"canary should never be hit; got {server.hits!r}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_transport_passes_no_follow_redirects_client(monkeypatch) -> None:
    """F1 belt: the client we hand to ``streamable_http_client`` MUST carry
    ``follow_redirects=False`` — regardless of any package-side default flip.
    """
    captured: dict[str, object] = {}

    class _Recorded:
        """Stand-in async-context yielding (read, write, session_id) tuple shape."""

        def __init__(self, url: str, *, http_client, terminate_on_close: bool = True) -> None:
            assert isinstance(url, str) and url, "url required"
            captured["url"] = url
            captured["client"] = http_client
            captured["follow_redirects"] = http_client.follow_redirects
            self._client = http_client

        async def __aenter__(self):
            raise ni_mcp.NIMcpError("mcp_unavailable", "captured")

        async def __aexit__(self, *_exc) -> None:
            return None

    monkeypatch.setattr(ni_mcp, "streamable_http_client", _Recorded)
    config = {"id": "s", "label": "x", "transport": "http",
              "url": "http://127.0.0.1:1/mcp", "enabled": True}
    with pytest.raises(ni_mcp.NIMcpError):
        ni_mcp.call_tool(config, "any", {}, timeout_s=2.0)
    assert captured["follow_redirects"] is False, captured
    assert captured["url"] == "http://127.0.0.1:1/mcp"


# --- HIGH F2: server_id grammar ---------------------------------------------

def test_validator_refuses_placeholder_in_server_id() -> None:
    """F2: {{param:...}} inside server_id is refused by the slug grammar."""
    with pytest.raises(ValueError, match="server_id"):
        ni._validate_mcp_source(_min_source(server_id="{{param:s}}"))


def test_validator_refuses_whitespace_in_server_id() -> None:
    """F2: whitespace is outside the slug charset."""
    with pytest.raises(ValueError, match="server_id"):
        ni._validate_mcp_source(_min_source(server_id="x y"))


def test_validator_refuses_oversized_server_id() -> None:
    """F2: 41 chars exceeds the 40-char slug ceiling."""
    with pytest.raises(ValueError):
        ni._validate_mcp_source(_min_source(server_id="a" * 41))


def test_validator_refuses_tool_name_with_trailing_newline() -> None:
    """F6: ``$`` matches before ``\\n`` in Python re; ``\\Z`` is the correct anchor."""
    with pytest.raises(ValueError, match="A-Za-z"):
        ni._validate_mcp_source(_min_source(tool="query\n"))


def test_mcp_param_only_change_not_source_change() -> None:
    """F2 belt: a param-value edit on an mcp-source item is NOT a source change (the
    server_id is a fixed slot by grammar, so no placeholder can shift the effective
    server), and ``substitute_params`` leaves ``server_id`` byte-identical.
    """
    from smartbrain_3000 import tools as tools_mod

    base = {
        "version": 1, "title": "t", "goal": "g",
        "params": {"q": {"label": "Q", "kind": "string", "value": "one"}},
        "source": {"type": "mcp_tool", "server_id": "srv-a",
                   "tool": "query", "arguments": {"sql": "SELECT 1"}},
        "pipeline": [], "scene": {"type": "divider"},
        "display": {"size": "small"},
        "repair_policy": {"l1": True, "l2_frontier": False},
        "model": None,
    }
    updated = json.loads(json.dumps(base))
    updated["params"]["q"]["value"] = "two"
    assert tools_mod._ni_source_effectively_changed(base, updated) is False
    filled = ni.substitute_params(updated)
    assert filled["source"]["server_id"] == "srv-a"


# --- MED F3: shape_result progressive cap -----------------------------------

class _TxtItem:
    """Duck-typed CallToolResult.content text item for _shape_result."""

    __slots__ = ("text", "type")

    def __init__(self, text: str) -> None:
        assert isinstance(text, str), "text must be a string"
        self.type = "text"
        self.text = text


def test_shape_result_caps_many_items_progressively() -> None:
    """F3: many text items each just under the cap MUST stop the walk at ``cap`` bytes."""
    cap = ni_mcp.MAX_RESULT_TEXT_BYTES
    # 100 items × 50 KB each = 5 MB unbounded; the walk must stop at ``cap``.
    items = [_TxtItem("x" * (50 * 1024)) for _ in range(100)]
    out = ni_mcp._shape_result(items, cap)
    assert "text" in out
    assert len(out["text"].encode("utf-8")) <= cap


def test_shape_result_caps_single_giant_item() -> None:
    """F3: a single 5 MB text item is capped at ``cap`` bytes standalone before join."""
    cap = ni_mcp.MAX_RESULT_TEXT_BYTES
    items = [_TxtItem("y" * (5 * 1024 * 1024))]
    out = ni_mcp._shape_result(items, cap)
    assert len(out["text"].encode("utf-8")) <= cap


# --- LOW F4: env drift guard ------------------------------------------------

def test_safe_env_keys_covers_mcp_default_inherited() -> None:
    """F4 drift guard: every key the installed mcp package considers safe to inherit
    for a stdio child is present in ``_SAFE_ENV_KEYS``. A package bump that widens
    the safe list is caught here instead of silently forbidding what the package
    expects to see.
    """
    import mcp.client.stdio as _stdio_mod
    default = set(_stdio_mod.DEFAULT_INHERITED_ENV_VARS)
    assert default <= ni_mcp._SAFE_ENV_KEYS, (
        f"drift: DEFAULT_INHERITED_ENV_VARS added keys not in _SAFE_ENV_KEYS: "
        f"{sorted(default - ni_mcp._SAFE_ENV_KEYS)!r}"
    )


# --- LOW F5: bounded live-call cap ------------------------------------------

def test_call_tool_refuses_when_semaphore_exhausted(monkeypatch) -> None:
    """F5: a Semaphore(0) monkeypatch simulates "all slots held by live threads" —
    the next call MUST refuse with ``mcp_unavailable`` (detail: connector busy).
    """
    monkeypatch.setattr(ni_mcp, "_LIVE_CALLS", threading.Semaphore(0))
    config = {"id": "s", "label": "x", "transport": "stdio",
              "command": "/bin/true", "args": [], "enabled": True}
    with pytest.raises(ni_mcp.NIMcpError) as info:
        ni_mcp.call_tool(config, "any", {}, timeout_s=1.0)
    assert info.value.kind == "mcp_unavailable"
    assert "busy" in info.value.detail


# --- MED F7: consent-card mcp_label -----------------------------------------

def test_pending_tile_mcp_label_none_when_absent() -> None:
    """F7: _pending_tile omits the label (as ``None``) for a non-mcp parked tool."""
    from smartbrain_3000 import agent_routes
    row = {"id": "p1", "tool": "web_fetch", "tier": "reviewed",
           "created_at": "t", "args": {"url": "https://example.com"}}
    tile = agent_routes._pending_tile(row)
    assert tile["mcp_label"] is None


def test_pending_tile_mcp_label_included_when_passed() -> None:
    """F7: _pending_tile threads the resolved label into the tile shape."""
    from smartbrain_3000 import agent_routes
    row = {"id": "p1", "tool": "create_ni_item", "tier": "reviewed",
           "created_at": "t",
           "args": {"source": {"type": "mcp_tool", "server_id": "srv-a",
                               "tool": "query", "arguments": {}}}}
    tile = agent_routes._pending_tile(row, mcp_label="My Database")
    assert tile["mcp_label"] == "My Database"


def test_resolve_pending_mcp_label_resolves_from_registry() -> None:
    """F7: the resolver reads the label out of the sealed registry (not the args)."""
    from smartbrain_3000 import agent_routes
    store, _, _ = _store()
    reg = ni_mcp.ServerRegistry(store)
    entry = reg.add({"label": "Warehouse", "transport": "stdio",
                     "command": "/bin/true", "args": [], "enabled": True})
    row = {"id": "p1", "tool": "create_ni_item",
           "args": {"source": {"type": "mcp_tool", "server_id": entry["id"],
                               "tool": "query", "arguments": {}}}}
    assert agent_routes._resolve_pending_mcp_label(row, store) == "Warehouse"


def test_resolve_pending_mcp_label_none_for_non_mcp() -> None:
    """F7: a non-mcp tool never yields a label; the resolver returns None cleanly."""
    from smartbrain_3000 import agent_routes
    store, _, _ = _store()
    row = {"id": "p1", "tool": "web_fetch",
           "args": {"url": "https://example.com"}}
    assert agent_routes._resolve_pending_mcp_label(row, store) is None


def test_resolve_pending_mcp_label_none_for_missing_server() -> None:
    """F7: an unknown server_id returns None (the tile still renders; the args are
    read verbatim by the executor — the label is a display-side convenience only).
    """
    from smartbrain_3000 import agent_routes
    store, _, _ = _store()
    row = {"id": "p1", "tool": "update_ni_item",
           "args": {"source": {"type": "mcp_tool", "server_id": "no-such",
                               "tool": "query", "arguments": {}}}}
    assert agent_routes._resolve_pending_mcp_label(row, store) is None


def test_call_tool_releases_semaphore_on_success(tmp_path: pathlib.Path) -> None:
    """F5: a successful call releases the slot — the semaphore returns to full."""
    server = _stdio_config(tmp_path, """
        @server.tool()
        def ok() -> str:
            return "ok"
    """)
    ni_mcp.call_tool(server, "ok", {}, timeout_s=15.0)
    # Acquire every slot and confirm the semaphore was replenished on the way out.
    got = [ni_mcp._LIVE_CALLS.acquire(blocking=False)
           for _ in range(ni_mcp._MAX_CONCURRENT_CALLS)]  # bounded
    try:
        assert all(got), "semaphore was not fully released after a successful call"
        # And the (N+1)th non-blocking acquire fails, proving the bound is tight.
        assert ni_mcp._LIVE_CALLS.acquire(blocking=False) is False
    finally:
        for ok in got:  # bounded
            if ok:
                ni_mcp._LIVE_CALLS.release()
