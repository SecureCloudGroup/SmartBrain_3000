"""Tests for the SSRF guard (H4b). Network-free: socket resolution is stubbed."""

from __future__ import annotations

import logging
import socket

import pytest

from smartbrain_3000 import netguard, vault_format
from smartbrain_3000.netguard import FetchError


def _resolve_to(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda node, *a, **k: [(2, 1, 6, "", (ip, 0))])


# Shared refusal matrix: every address the guard must reject, reused by the
# resolver test and the vault-fetch transport tests below.
_BLOCKED_IPS = [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
    "::1", "0.0.0.0", "::ffff:10.0.0.1",
    "100.64.0.1",  # CGNAT — not is_private, but not is_global
    "198.18.0.1",  # benchmark range
    "192.0.2.1",   # TEST-NET-1
    "240.0.0.1",   # reserved
    # B1 explicit rejects (multicast / reserved / unspecified BEFORE is_global):
    "239.255.255.250",  # IPv4 multicast (SSDP) — older is_global accepts as "non-private"
    "224.0.0.1",        # IPv4 multicast base
    "ff02::1",          # IPv6 link-local multicast
    # B1 NAT64 well-known prefix (RFC 6052): the low 32 bits embed a v4 the
    # guard must re-validate. The wrapped v4s below are loopback / private
    # / link-local / multicast — all loopback-bypass attempts via NAT64.
    "64:ff9b::7f00:1",   # wraps 127.0.0.1
    "64:ff9b::a00:1",    # wraps 10.0.0.1
    "64:ff9b::a9fe:a9fe",  # wraps 169.254.169.254 (cloud metadata)
    "64:ff9b::efff:fffa",  # wraps 239.255.255.250 (IPv4 multicast)
]


@pytest.mark.parametrize("ip", _BLOCKED_IPS)
def test_validated_ip_blocks_non_global(monkeypatch, ip) -> None:
    _resolve_to(monkeypatch, ip)
    with pytest.raises(FetchError):
        netguard._validated_ip("evil.test")


def test_validated_ip_accepts_public(monkeypatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    assert netguard._validated_ip("example.test") == "93.184.216.34"


def test_safe_fetch_reads_success_body(monkeypatch) -> None:
    # The SSRF *validation* tests all raise FetchError BEFORE any send; this exercises the
    # SUCCESS path (stream read + close). httpx.Response is not a context manager, so the
    # old `with _send_pinned(...) as response:` raised here — breaking web_search/web_fetch/
    # ingest_url in production while the mocked tests stayed green. Regression guard.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<html>hello world</html>")
    )
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    out = netguard.safe_fetch("http://example.test/page")
    assert out["status"] == 200
    assert "hello world" in out["text"]
    assert out["final_url"] == "http://example.test/page"


def test_validated_ip_rejects_if_any_record_is_private(monkeypatch) -> None:
    # A multi-A response with one public + one private address must be rejected.
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda node, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("10.0.0.1", 0))],
    )
    with pytest.raises(FetchError):
        netguard._validated_ip("evil.test")


def test_guarded_fetch_does_not_mutate_global_resolver(monkeypatch) -> None:
    """B1: the SSRF pin must NEVER reassign socket.getaddrinfo (a concurrent
    gateway/Gmail call during a web_fetch must resolve normally)."""
    _resolve_to(monkeypatch, "127.0.0.1")  # forces FetchError (loopback) before any connect
    original = socket.getaddrinfo
    with pytest.raises(FetchError):
        netguard.safe_fetch("http://internal.evil/")
    assert socket.getaddrinfo is original  # no global state was touched, even on failure


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://h/x", "data:text/plain,hi", "gopher://h/"])
def test_safe_fetch_rejects_bad_scheme(url) -> None:
    with pytest.raises(FetchError):
        netguard.safe_fetch(url)


@pytest.mark.parametrize("url", ["http://example.test:99999/x", "http://example.test:abc/x"])
def test_safe_fetch_rejects_malformed_port(url) -> None:
    # urlparse validates the port lazily, on attribute access: a malformed one must be a clean
    # FetchError, not a ValueError escaping into a 500. Checked before DNS, so no resolver stub.
    with pytest.raises(FetchError, match="port"):
        netguard.safe_fetch(url)


def test_safe_fetch_rejects_userinfo(monkeypatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    with pytest.raises(FetchError):
        netguard.safe_fetch("http://user:pass@example.test/")


def test_safe_fetch_blocks_private_target(monkeypatch) -> None:
    # Host resolves to loopback -> blocked before any connection is attempted.
    _resolve_to(monkeypatch, "127.0.0.1")
    with pytest.raises(FetchError):
        netguard.safe_fetch("http://internal.evil/")


# --- vault-fetch transport (public vaults / subscribe-by-URL) --------------------------------

_VAULT_FETCHERS = [netguard.safe_fetch_vault, netguard.safe_fetch_vault_manifest]


def _serve(monkeypatch, handler) -> None:
    """Route netguard's httpx.Client through a MockTransport (no sockets)."""
    import httpx

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("fetch", _VAULT_FETCHERS)
@pytest.mark.parametrize("ip", _BLOCKED_IPS)
def test_vault_fetch_blocks_non_global(monkeypatch, fetch, ip) -> None:
    # Both vault helpers must sit behind the exact same refusal matrix as page fetch.
    _resolve_to(monkeypatch, ip)
    with pytest.raises(FetchError):
        fetch("https://evil.test/team.sbvault")


@pytest.mark.parametrize("fetch", _VAULT_FETCHERS)
def test_vault_fetch_fragment_never_reaches_resolver_or_logs(monkeypatch, caplog, fetch) -> None:
    # A sealed-share URL carries its key in the fragment (#k=<key>). Even on a
    # refused fetch, the key must not reach the resolver, the error, or any log.
    seen = {}

    def spy_gai(node, *args, **kwargs):
        seen["node"] = node
        return [(2, 1, 6, "", ("127.0.0.1", 0))]  # loopback -> refused before any connect

    monkeypatch.setattr(socket, "getaddrinfo", spy_gai)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FetchError) as exc:
            fetch("https://tree.test/team.sbvault#k=FAKEKEY_ABC123")
    assert seen["node"] == "tree.test"
    assert "FAKEKEY_ABC123" not in str(exc.value)
    assert "FAKEKEY_ABC123" not in caplog.text


def test_safe_fetch_vault_success_strips_fragment_from_request(monkeypatch, caplog) -> None:
    # Happy path: bytes come back, and the fragment never crosses the wire or a log.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04vaultbytes")

    _serve(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        out = netguard.safe_fetch_vault("http://tree.test/team.sbvault#k=FAKEKEY_ABC123")
    assert out == b"PK\x03\x04vaultbytes"
    assert "FAKEKEY_ABC123" not in seen["url"] and "#" not in seen["url"]
    assert "FAKEKEY_ABC123" not in caplog.text


@pytest.mark.parametrize(
    "fetch,ctype,ok",
    [
        (netguard.safe_fetch_vault, "application/zip", True),
        (netguard.safe_fetch_vault, "application/x-zip-compressed", True),
        (netguard.safe_fetch_vault, "application/octet-stream", True),
        (netguard.safe_fetch_vault, "text/html; charset=utf-8", False),
        (netguard.safe_fetch_vault, "application/json", False),
        (netguard.safe_fetch_vault_manifest, "application/json; charset=utf-8", True),
        (netguard.safe_fetch_vault_manifest, "text/plain", True),
        (netguard.safe_fetch_vault_manifest, "application/octet-stream", True),
        (netguard.safe_fetch_vault_manifest, "text/html; charset=utf-8", False),
        (netguard.safe_fetch_vault_manifest, "application/zip", False),
    ],
)
def test_vault_fetch_content_types(monkeypatch, fetch, ctype, ok) -> None:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": ctype}, content=b"x"))
    if ok:
        assert fetch("http://tree.test/f") == b"x"
    else:
        with pytest.raises(FetchError):
            fetch("http://tree.test/f")


def test_vault_manifest_cap_is_stream_bounded_not_header_trusted(monkeypatch) -> None:
    # Content-Length claims 10 bytes; the body is one byte past the cap. The
    # reader must count streamed bytes, never trust the header.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    body = b"j" * (vault_format.MAX_MANIFEST_BYTES + 1)
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json", "content-length": "10"}, content=body
        ),
    )
    with pytest.raises(FetchError):
        netguard.safe_fetch_vault_manifest("http://tree.test/manifest.json")


def test_vault_manifest_at_cap_accepted(monkeypatch) -> None:
    # Exactly at the cap is fine; the bound is > cap, not >= cap.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    body = b"j" * vault_format.MAX_MANIFEST_BYTES
    _serve(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "application/json"}, content=body))
    assert netguard.safe_fetch_vault_manifest("http://tree.test/manifest.json") == body


def test_safe_fetch_vault_cap_comes_from_vault_format(monkeypatch) -> None:
    # Prove the vault helper's bound IS vault_format.MAX_VAULT_BYTES without
    # allocating 512 MiB: shrink the constant, then cross it by one byte.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(netguard.vault_format, "MAX_VAULT_BYTES", 64)
    _serve(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "application/zip"}, content=b"z" * 65))
    with pytest.raises(FetchError):
        netguard.safe_fetch_vault("http://tree.test/team.sbvault")


# --- overall wall-clock deadline: abandon a slow-drip host --------------------------------------
# The per-chunk read timeout (_TIMEOUT) alone lets a host drip one chunk every <8s and keep the read
# alive under the byte cap until 512 MiB — effectively forever. On a VAULT fetch that would run
# synchronously inside the scheduler tick and wedge the whole scheduler. An overall deadline abandons
# it. The clock is injected (netguard._monotonic) so the deadline trips deterministically, no sleep.


@pytest.mark.parametrize(
    "fetch,ctype",
    [
        (netguard.safe_fetch_vault, "application/zip"),
        (netguard.safe_fetch_vault_manifest, "application/json"),
    ],
)
def test_vault_fetch_abandons_a_drip_host_at_the_deadline(monkeypatch, fetch, ctype) -> None:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": ctype}, content=b"x" * 64))
    calls = {"n": 0}

    def clock() -> float:  # first call is the read's start; every later call is past the deadline
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else float(netguard._VAULT_FETCH_DEADLINE_SECONDS) + 1.0

    monkeypatch.setattr(netguard, "_monotonic", clock)
    with pytest.raises(FetchError) as exc:
        fetch("http://tree.test/f")
    assert "too slow" in str(exc.value), "the deadline yields a clean, class-name-only FetchError"


def test_page_fetch_has_no_deadline_so_ingest_byte_behavior_is_unchanged(monkeypatch) -> None:
    # The deadline binds ONLY the vault fetchers. A page fetch passes no deadline, so even a clock
    # jumped far past any deadline never abandons it — the whole body still returns byte-for-byte.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    body = b"<html>" + b"z" * 4096 + b"</html>"
    _serve(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body))
    monkeypatch.setattr(netguard, "_monotonic", lambda: 10_000.0)  # would trip any deadline, if one applied
    out = netguard.safe_fetch("http://example.test/page")
    assert out["text"] == body.decode("utf-8"), "page/ingest read is unbounded by the vault deadline"
