"""Tests for the web app-shell serving, PWA assets, headers + serve config (D)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import serving


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_root_serves_app_shell(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "SmartBrain_3000" in r.text


def test_spa_fallback_for_client_route(client: TestClient) -> None:
    r = client.get("/some/deep/client/route")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text  # the shell, not a 404
    assert r.headers.get("cache-control") == "no-cache"  # shell must stay fresh


def test_real_file_takes_precedence_over_shell(client: TestClient) -> None:
    # A real built asset must be served by the catch-all, not the SPA fallback.
    r = client.get("/icons/icon-512.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "<!DOCTYPE html>" not in r.text


def test_manifest_and_all_icons_reachable(client: TestClient) -> None:
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(r.text)
    assert data["name"] and data["start_url"] == "/" and data["icons"]
    for icon in data["icons"]:  # bounded by the manifest's icon list
        got = client.get(icon["src"])
        assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert client.get("/icons/apple-touch-icon.png").status_code == 200  # referenced by index.html


def test_service_worker_never_caches_secrets(client: TestClient) -> None:
    r = client.get("/service-worker.js")
    assert r.status_code == 200
    assert r.headers.get("Service-Worker-Allowed") == "/"
    assert r.headers.get("cache-control") == "no-cache"
    body = r.text
    # The built SW is minified and comment-free, so these startsWith() calls can
    # only come from the fetch-handler bypass — the SW must skip /api and /mcp.
    assert re.search(r"""startsWith\(\s*["']/api["']\s*\)""", body)
    assert re.search(r"""startsWith\(\s*["']/mcp["']\s*\)""", body)


def test_icon_served(client: TestClient) -> None:
    r = client.get("/icons/icon-192.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_api_not_shadowed_and_404_is_json(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json().get("detail")  # JSON 404, not the HTML shell


def test_mcp_not_shadowed_by_spa(client: TestClient) -> None:
    r = client.get("/mcp/definitely-not-a-route")
    assert r.status_code in (401, 404)  # auth wrapper may 401 first; never the shell
    assert "<!DOCTYPE html>" not in r.text


def test_html_page_csp_via_meta_and_hardening_headers(client: TestClient) -> None:
    r = client.get("/")
    # HTML defers CSP to SvelteKit's hash-based <meta> (it allow-lists the inline
    # bootstrap by sha256); a header CSP would intersect and block it.
    assert "content-security-policy" not in {k.lower() for k in r.headers}
    assert 'http-equiv="content-security-policy"' in r.text
    assert "default-src 'self'" in r.text and "script-src 'self'" in r.text
    # Hardening + framing protection still come from headers.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    # HSTS must NOT be set: it would break the http://localhost desktop flow.
    assert "strict-transport-security" not in {k.lower() for k in r.headers}


def test_api_responses_carry_security_headers(client: TestClient) -> None:
    h = client.get("/api/health").headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"


def test_safe_file_blocks_traversal_and_serves_real_files() -> None:
    web = serving.web_dir()
    assert serving.safe_file(web, "../../../../etc/passwd") is None  # traversal blocked
    assert serving.safe_file(web, "/etc/passwd") is None  # absolute path blocked
    assert serving.safe_file(web, "nope/missing.js") is None  # missing -> shell fallback
    served = serving.safe_file(web, "manifest.webmanifest")
    assert served is not None and served.name == "manifest.webmanifest"  # real file resolved


# --- serve.py config ------------------------------------------------------

def test_build_config_plain_http(monkeypatch) -> None:
    from smartbrain_3000 import serve

    for var in ("SMARTBRAIN_TLS_CERT", "SMARTBRAIN_TLS_KEY", "SMARTBRAIN_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMARTBRAIN_PORT", "33000")
    cfg = serve.build_config()
    assert cfg["host"] == "127.0.0.1" and cfg["port"] == 33000  # loopback by default
    assert "ssl_certfile" not in cfg and "ssl_keyfile" not in cfg


def test_build_config_tls(monkeypatch) -> None:
    from smartbrain_3000 import serve

    monkeypatch.setenv("SMARTBRAIN_TLS_CERT", "/certs/c.pem")
    monkeypatch.setenv("SMARTBRAIN_TLS_KEY", "/certs/k.pem")
    cfg = serve.build_config()
    assert cfg["ssl_certfile"] == "/certs/c.pem" and cfg["ssl_keyfile"] == "/certs/k.pem"


@pytest.mark.parametrize("present,absent", [("SMARTBRAIN_TLS_CERT", "SMARTBRAIN_TLS_KEY"),
                                            ("SMARTBRAIN_TLS_KEY", "SMARTBRAIN_TLS_CERT")])
def test_build_config_partial_tls_rejected(monkeypatch, present, absent) -> None:
    from smartbrain_3000 import serve

    monkeypatch.setenv(present, "/certs/x.pem")
    monkeypatch.delenv(absent, raising=False)
    with pytest.raises(RuntimeError):  # hard raise, not assert (survives python -O)
        serve.build_config()


@pytest.mark.parametrize("bad_port", ["0", "70000"])
def test_build_config_rejects_out_of_range_port(monkeypatch, bad_port) -> None:
    from smartbrain_3000 import serve

    for var in ("SMARTBRAIN_TLS_CERT", "SMARTBRAIN_TLS_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMARTBRAIN_PORT", bad_port)
    with pytest.raises(AssertionError):
        serve.build_config()
