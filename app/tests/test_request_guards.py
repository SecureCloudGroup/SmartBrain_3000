"""Tests for the request-admission middleware: HostGuard + OriginGuard.

These two guards are the app's only network-level access checks, and both are
invisible to every other test in the suite (conftest widens the host allow-list
so the rest of the suite can talk to the app at all). They are tested here
against apps built with explicit settings.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# TestClient addresses the app as this host; an Origin must name it to be same-origin.
_HOST = "testserver"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# --- OriginGuard: the cross-site drive-by defence -----------------------------


def test_bare_request_allowed(client: TestClient) -> None:
    """No Origin, no Sec-Fetch-Site: curl, the MCP server, a bridged phone."""
    assert client.get("/api/status").status_code == 200


def test_same_origin_header_allowed(client: TestClient) -> None:
    r = client.get("/api/status", headers={"origin": f"http://{_HOST}"})
    assert r.status_code == 200


def test_cross_origin_header_refused(client: TestClient) -> None:
    r = client.get("/api/status", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_origin_null_refused(client: TestClient) -> None:
    """A sandboxed iframe or a redirected request sends null — it matches nothing."""
    r = client.get("/api/status", headers={"origin": "null"})
    assert r.status_code == 403


def test_origin_matches_regardless_of_scheme(client: TestClient) -> None:
    """The LAN/TLS overlay serves the same authority over https."""
    r = client.get("/api/status", headers={"origin": f"https://{_HOST}"})
    assert r.status_code == 200


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_sec_fetch_site_allowed(client: TestClient, site: str) -> None:
    r = client.get("/api/status", headers={"sec-fetch-site": site})
    assert r.status_code == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_sec_fetch_site_refused(client: TestClient, site: str) -> None:
    r = client.get("/api/status", headers={"sec-fetch-site": site})
    assert r.status_code == 403


def test_state_changing_post_refused_cross_site(client: TestClient) -> None:
    """The finding this guard exists for: a raw-body POST needs no preflight.

    Refused *before* routing, so the 403 (not the locked-vault 423) proves the
    guard fired rather than the endpoint.
    """
    r = client.post(
        "/api/kb/upload?filename=notes.txt",
        content=b"instructions for the assistant",
        headers={"origin": "https://evil.example", "content-type": "text/plain"},
    )
    assert r.status_code == 403


def test_shell_navigation_from_another_site_allowed(client: TestClient) -> None:
    """Only /api and /mcp are guarded — following a link to the app is legitimate."""
    r = client.get("/", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200


def test_oauth_callback_navigation_allowed_cross_site(client: TestClient) -> None:
    """Google's consent page redirects the browser here — a cross-site navigation
    by design. The guard must let it through to the route, whose one-shot state
    check is the real defense. 400 (no pending flow), not 403, proves the guard
    stepped aside and the route answered.
    """
    r = client.get(
        "/api/email/oauth/callback?code=x&state=y",
        headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"},
    )
    assert r.status_code != 403


def test_oauth_callback_scripted_fetch_still_refused(client: TestClient) -> None:
    """The exemption is navigations only — a cross-site fetch/XHR stays refused."""
    r = client.get(
        "/api/email/oauth/callback?code=x&state=y",
        headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"},
    )
    assert r.status_code == 403


def test_oauth_callback_post_still_refused(client: TestClient) -> None:
    """The exemption is GET/HEAD only — a cross-site POST to the path stays refused."""
    r = client.post(
        "/api/email/oauth/callback",
        headers={"sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 403


# --- HostGuard: the anti-DNS-rebinding check ----------------------------------


def test_disallowed_host_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "host.duckdb"))
    monkeypatch.setenv("SMARTBRAIN_ALLOWED_HOSTS", "localhost,127.0.0.1")
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:  # TestClient sends Host: testserver
        assert c.get("/api/status").status_code == 400


def test_allowed_host_matches_case_insensitively(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "case.duckdb"))
    monkeypatch.setenv("SMARTBRAIN_ALLOWED_HOSTS", "TestServer")
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/status").status_code == 200
