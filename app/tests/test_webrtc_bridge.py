"""Tests for the WebRTC loopback bridge (webrtc_bridge.py) — Phase 1.

The bridge turns a remote device's request frame into a call against the app's own
loopback and frames the response back. These tests drive it with a Starlette
TestClient (which runs lifespan + the real routes), exactly the interface the
aiortc handler will use in production with an httpx.Client.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import webrtc_bridge


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_proxy_health_roundtrips(client: TestClient) -> None:
    out = webrtc_bridge.handle_frame({"id": "r1", "method": "GET", "path": "/api/health"}, client)
    assert out["id"] == "r1" and out["status"] == 200
    assert b'"status":"ok"' in out["body"]
    assert out["headers"].get("content-type", "").startswith("application/json")


def test_proxy_passes_through_locked_status(client: TestClient) -> None:
    # /api/mcp is unlock-gated -> 423 while locked. The bridge must relay the status faithfully.
    out = webrtc_bridge.handle_frame({"id": "r2", "method": "GET", "path": "/api/mcp"}, client)
    assert out["status"] == 423


@pytest.mark.parametrize("path", ["/mcp/anything", "/", "/index.html", "/apifoo", "/api"])
def test_rejects_paths_outside_api(client: TestClient, path: str) -> None:
    out = webrtc_bridge.handle_frame({"id": "r3", "method": "GET", "path": path}, client)
    assert out["status"] == 400 and b"path must be a local" in out["body"]


@pytest.mark.parametrize("path", [
    "/api/../mcp", "/api/../", "/api/foo/../../mcp", "/api/../index.html", "/api/../openapi.json",
])
def test_rejects_dot_segment_traversal(client: TestClient, path: str) -> None:
    # The HTTP client normalizes ".." against the loopback base, so a leading "/api/"
    # is not enough — these would escape to /mcp, the SPA, or docs. Must be rejected.
    out = webrtc_bridge.handle_frame({"id": "t", "method": "GET", "path": path}, client)
    assert out["status"] == 400 and b"illegal path" in out["body"]


@pytest.mark.parametrize("path", ["/api/x\r\nHost: evil", "/api/..\\mcp", "/api/x\x00y"])
def test_rejects_control_and_backslash_paths(client: TestClient, path: str) -> None:
    out = webrtc_bridge.handle_frame({"id": "t", "method": "GET", "path": path}, client)
    assert out["status"] == 400


def test_rejects_absolute_url_smuggling(client: TestClient) -> None:
    out = webrtc_bridge.handle_frame(
        {"id": "r4", "method": "GET", "path": "http://evil.example/api/health"}, client
    )
    assert out["status"] == 400


def test_rejects_disallowed_method(client: TestClient) -> None:
    out = webrtc_bridge.handle_frame({"id": "r5", "method": "CONNECT", "path": "/api/health"}, client)
    assert out["status"] == 400 and b"method not allowed" in out["body"]


def test_rejects_oversized_body(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(webrtc_bridge, "_MAX_BODY_BYTES", 4)
    out = webrtc_bridge.handle_frame(
        {"id": "r6", "method": "POST", "path": "/api/kb", "body": b"way too long"}, client
    )
    assert out["status"] == 400 and b"size cap" in out["body"]


def test_missing_id_is_unrecoverable(client: TestClient) -> None:
    with pytest.raises(AssertionError):
        webrtc_bridge.handle_frame({"method": "GET", "path": "/api/health"}, client)


def test_parse_request_strips_unsafe_headers() -> None:
    req = webrtc_bridge.parse_request(
        {
            "method": "post",
            "path": "/api/chat",
            "headers": {"Host": "evil", "Content-Type": "application/json", "X-Secret": "leak"},
            "body": b"{}",
        }
    )
    assert req["method"] == "POST"
    assert req["headers"] == {"Content-Type": "application/json"}  # Host + X-Secret dropped


def test_upstream_failure_becomes_error_frame() -> None:
    class _Boom:
        def request(self, *a, **k):  # noqa: ANN002, ANN003 - test stub
            raise RuntimeError("loopback down")

    out = webrtc_bridge.handle_frame({"id": "r7", "method": "GET", "path": "/api/health"}, _Boom())
    assert out["status"] == 502 and b"upstream error" in out["body"]
