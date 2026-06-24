"""Tests for the SSRF guard (H4b). Network-free: socket resolution is stubbed."""

from __future__ import annotations

import socket

import pytest

from smartbrain_3000 import netguard
from smartbrain_3000.netguard import FetchError


def _resolve_to(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda node, *a, **k: [(2, 1, 6, "", (ip, 0))])


@pytest.mark.parametrize(
    "ip",
    [
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
    ],
)
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


def test_safe_fetch_rejects_userinfo(monkeypatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    with pytest.raises(FetchError):
        netguard.safe_fetch("http://user:pass@example.test/")


def test_safe_fetch_blocks_private_target(monkeypatch) -> None:
    # Host resolves to loopback -> blocked before any connection is attempted.
    _resolve_to(monkeypatch, "127.0.0.1")
    with pytest.raises(FetchError):
        netguard.safe_fetch("http://internal.evil/")
