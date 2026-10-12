"""Shared helpers for the browser-component tests (§35).

Everything is local and deterministic: a fake engine (tests/fixtures/browsers/
fake_obscura.py) packed into a real tarball, a release server on 127.0.0.1 that answers
the GitHub URLs (the transport rewrites ``https://host/path`` to ``http://127.0.0.1:p/host/path``
— the redirect allowlist, the size cap and the hashing all run on the real URLs), a
TLS-free page target, and an in-process egress server whose injected resolver answers a
public-looking address that its injected dialer maps to the local target (so the real
``netguard._is_unsafe`` check still runs on every answer).
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from smartbrain_3000.browsers import install, jail_egress, router, walls
from smartbrain_3000.browsers import manifest as mf

FAKE_ENGINE = Path(__file__).resolve().parent / "fixtures" / "browsers" / "fake_obscura.py"
FAKE_VERSION = "9.9.9"
PUBLIC_IP = "93.184.216.34"  # answered by the injected resolver; the dialer maps it to loopback
TEMPLATE = "https://github.com/h4ckf0r0day/obscura/releases/download/v{version}/{asset}"
ASSET = "obscura-test.tar.gz"


def tar_bytes(members: list[tuple[str, bytes, int]], *, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    """A gzip tarball of (name, data, mode) regular files, plus any raw extra members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tf.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
    return buf.getvalue()


def engine_bytes() -> bytes:
    return FAKE_ENGINE.read_bytes()


def fake_tarball() -> bytes:
    return tar_bytes([("obscura", engine_bytes(), 0o755), ("obscura-worker", b"never run\n", 0o755)])


def manifest_doc(tarball: bytes, *, name: str = "obscura", limits: dict | None = None,
                 pool_size: int = 8, mode: str = "mimic", member: bytes | None = None) -> dict:
    """A valid manifest for the fake engine on THIS machine's platform."""
    plat = install.platform_key()
    assert plat is not None, "tests run on a supported platform"
    member = engine_bytes() if member is None else member
    stealth = name.endswith("stealth")
    pre = ["--v8-flags", "--max-old-space-size={heap_mb}", "--proxy",
           "http://127.0.0.1:{proxy_port}", "--storage-dir", "{run_dir}/storage"]
    pre += ["--stealth"] if stealth else []
    tail = ["--timeout", "{timeout_s}", "--wait-until", "load", "--dump"]
    return {
        "name": name, "version": FAKE_VERSION, "released": "2026-10-04",
        "source_url_template": TEMPLATE,
        "platforms": {plat: {"asset": ASSET, "sha256": hashlib.sha256(tarball).hexdigest(),
                             "size": len(tarball),
                             "members": {"obscura": {"sha256": hashlib.sha256(member).hexdigest(),
                                                     "size": len(member)}}}},
        "files": [{"name": "obscura", "mode": "0755", "install": True},
                  {"name": "obscura-worker", "mode": "0644", "install": False}],
        "executable": "obscura",
        "capabilities": ["render", "js", "assets"] + (["stealth"] if stealth else []),
        "argv": {"fetch": pre + ["fetch", "{url}"] + tail + ["html", "--output", "{output_path}"],
                 "assets": pre + ["fetch", "{url}"] + tail + ["assets", "--output", "{output_path}"],
                 "honest": ["--user-agent", "{user_agent}"]},
        "forbidden_flags": ["--allow-private-network", "--eval"] + ([] if stealth else ["--stealth"]),
        "limits": {"timeout_s": 4, "script_deadline_ms": 3000, "heap_mb": 128, "rss_mb": 64,
                   "rlimit_data_mb": 0, "html_cap_bytes": 200_000, **(limits or {})},
        "identity": {"mode": mode, "pool_size": pool_size}, "min_glibc": "2.17",
    }


def write_engines(directory: Path, docs: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (directory / f"{doc['name']}.json").write_text(json.dumps(doc))
    return directory


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        server.requests.append(self.path)
        status, headers, body = server.routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


class LocalServer:
    """A ThreadingHTTPServer on 127.0.0.1 serving fixed ``routes`` {path: (status, headers, body)}."""

    def __init__(self, routes: dict | None = None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.routes = dict(routes or {})
        self.httpd.requests = []
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def routes(self) -> dict:
        return self.httpd.routes

    @property
    def requests(self) -> list[str]:
        return self.httpd.requests

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _Stream:
    def __init__(self, resp: httpx.Response, client: httpx.Client) -> None:
        self._resp, self._client = resp, client
        self.status_code, self.headers = resp.status_code, resp.headers

    def iter_bytes(self, chunk_size: int):
        return self._resp.iter_bytes(chunk_size)

    def close(self) -> None:
        self._resp.close()
        self._client.close()


def transport_for(server: LocalServer):
    """``https://host/path`` → ``http://127.0.0.1:port/host/path`` (no redirect following)."""

    def open_url(url: str) -> _Stream:
        parts = urlsplit(url)
        client = httpx.Client(follow_redirects=False, trust_env=False, timeout=10.0)
        local = f"http://127.0.0.1:{server.port}/{parts.hostname}{parts.path}"
        return _Stream(client.send(client.build_request("GET", local), stream=True), client)

    return open_url


def release_routes(tarball: bytes, *, redirect_to: str = "objects.githubusercontent.com") -> dict:
    """The pinned GitHub URL 302s to a CDN host that serves the tarball."""
    pinned = f"/github.com/h4ckf0r0day/obscura/releases/download/v{FAKE_VERSION}/{ASSET}"
    cdn = f"/{redirect_to}/release-asset/{ASSET}"
    return {pinned: (302, {"Location": f"https://{redirect_to}/release-asset/{ASSET}"}, b""),
            cdn: (200, {"Content-Type": "application/octet-stream"}, tarball)}


def use_test_engines(monkeypatch, tmp_path: Path, docs: list[dict]) -> Path:
    """Point the component at a private data dir + test manifests, enabled, NullWall on,
    with the process-wide install state and the router's breaker reset."""
    with install._state_lock:
        install._state.clear()
        install._inflight.clear()
    router.BREAKER.reset()
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "data" / "sb.duckdb"))
    monkeypatch.delenv(install.DISABLE_ENV, raising=False)
    monkeypatch.setenv(walls.NULLWALL_ENV, "1")
    monkeypatch.setattr(mf, "ENGINES_DIR", write_engines(tmp_path / "engines", docs))
    return tmp_path / "data" / "browsers"


def install_fake(monkeypatch, tmp_path: Path, **doc_kw: object) -> mf.Manifest:
    """Install the fake engine through the real installer from a local release server."""
    tarball = fake_tarball()
    doc = manifest_doc(tarball, **doc_kw)
    use_test_engines(monkeypatch, tmp_path, [doc])
    server = LocalServer(release_routes(tarball))
    try:
        m = mf.load(doc["name"])
        result = install.install(m, install.platform_key(), transport=transport_for(server))
    finally:
        server.close()
    assert result["ok"], result
    return m


class PageTarget:
    """A TLS-free page server; requests arrive through the egress tunnel."""

    def __init__(self, html: bytes) -> None:
        self.server = LocalServer({"/": (200, {"Content-Type": "text/html"}, html),
                                   "/page": (200, {"Content-Type": "text/html"}, html)})
        self.port = self.server.port

    def close(self) -> None:
        self.server.close()


def resolver_for(names: dict[str, list[str]]):
    def resolve(host: str, port: int) -> list[str]:
        if host not in names:
            raise OSError("no such host")
        return list(names[host])
    return resolve


def dialer_to(port: int):
    """Every validated public address dials the local target instead."""

    def dial(ip: str, _port: int, timeout: float) -> socket.socket:
        if ip != PUBLIC_IP:
            raise OSError("unreachable in tests")
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)

    return dial


class InProcessEgress:
    """The real ``EgressServer`` in this process, shaped like ``egress.Egress``."""

    def __init__(self, config: dict, names: dict[str, list[str]], target_port: int) -> None:
        self.server = jail_egress.EgressServer(config, resolver=resolver_for(names),
                                               dialer=dialer_to(target_port))
        self.endpoint = self.server.listen()
        threading.Thread(target=self.server.serve, daemon=True).start()

    def stop(self) -> tuple[list[dict], int]:
        self.server.close()
        got = self.server.census()
        return got["census"], got["dropped"]


def egress_starter(names: dict[str, list[str]], target_port: int, started: list):
    """A drop-in for ``egress.start`` that builds an ``InProcessEgress``."""

    def start(run_dir: str, *, kind: str, policy: str, sites: list[str], main_host: str,
              main_port: int, max_life_s: float, caps: dict | None = None) -> InProcessEgress:
        config = {"listen": kind, "socket_path": None, "policy": policy, "sites": list(sites),
                  "main_host": main_host, "main_port": main_port, "max_life_s": max_life_s,
                  "caps": dict(caps or {})}
        handle = InProcessEgress(config, names, target_port)
        started.append(handle)
        return handle

    return start
