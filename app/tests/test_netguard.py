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
    with caplog.at_level(logging.DEBUG), pytest.raises(FetchError) as exc:
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


@pytest.mark.parametrize(
    "headers,body,ok",
    [
        (None, b"PK\x03\x04realvaultbytes", True),                          # NO Content-Type (Caddy) + zip body
        ({"content-type": "text/plain"}, b"PK\x03\x04realvaultbytes", True),  # wrong type (GitHub raw) + zip body
        ({"content-type": "text/html"}, b"<html>404</html>", False),        # wrong type + non-zip (host error page)
    ],
)
def test_vault_fetch_accepts_zip_magic_regardless_of_content_type(monkeypatch, headers, body, ok) -> None:
    # Real-world gap found live (against a Caddy host): static hosts serve the unknown `.sbvault`
    # extension with NO Content-Type, or a wrong one. A valid vault is a ZIP, so safe_fetch_vault
    # accepts it by its magic bytes regardless of the header; the publisher signature is verified
    # afterward, so this only widens which hosts work — it weakens nothing. A non-zip body under a
    # wrong type is the host's HTML/error page and is still refused.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda r: httpx.Response(200, headers=headers or {}, content=body))
    if ok:
        assert netguard.safe_fetch_vault("http://tree.test/team.sbvault") == body
    else:
        with pytest.raises(FetchError):
            netguard.safe_fetch_vault("http://tree.test/team.sbvault")


def test_zip_magic_sniff_is_vault_only_not_page_ingest(monkeypatch) -> None:
    # The magic-sniff widening is scoped to the vault fetch; page ingest never gets it, so a zip
    # body under a non-ingest Content-Type stays refused on the ingest path.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04x"))
    with pytest.raises(FetchError):
        netguard.safe_fetch_bytes("http://tree.test/thing")


def test_page_fetch_sends_an_honest_identity(monkeypatch) -> None:
    # Operator ruling 2026-09-23: SmartBrain identifies itself honestly — no
    # desktop-browser pose, no browser-navigation metadata; content negotiation
    # stays (evidence in netguard's _FETCH_HEADERS comment).
    import httpx

    from smartbrain_3000 import __version__

    _resolve_to(monkeypatch, "93.184.216.34")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")

    _serve(monkeypatch, handler)
    netguard.safe_fetch("http://example.test/page")
    ua = seen["user-agent"]
    assert ua.startswith(f"SmartBrain/{__version__} "), ua
    assert "+https://smartbrain.securecloudgroup.com" in ua
    assert "Mozilla" not in ua and "Chrome" not in ua, "no browser pose"
    assert "text/html" in seen["accept"] and seen["accept-language"]
    assert not any(k.startswith("sec-fetch-") for k in seen), "no navigation metadata"
    assert "upgrade-insecure-requests" not in seen


def test_post_json_sends_body_and_headers(monkeypatch) -> None:
    # Tavily rides a guarded JSON POST: body, content-type, and the auth header must
    # arrive; the response parses back to a dict.
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        seen["ct"] = request.headers.get("content-type")
        seen["auth"] = request.headers.get("x-api-key")
        return httpx.Response(200, headers={"content-type": "application/json"}, text='{"ok": true}')

    _serve(monkeypatch, handler)
    out = netguard.safe_post_json("http://api.test/search", {"q": "x"}, headers={"X-Api-Key": "k"})
    assert out == {"ok": True}
    assert seen["method"] == "POST" and b'"q": "x"' in seen["body"]
    assert seen["ct"] == "application/json" and seen["auth"] == "k"


def test_post_json_refuses_redirects(monkeypatch) -> None:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://api.test/other"}))
    with pytest.raises(FetchError):
        netguard.safe_post_json("http://api.test/search", {"q": "x"})


def test_fetch_json_rejects_invalid_json(monkeypatch) -> None:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "application/json"}, text="not json"))
    with pytest.raises(FetchError):
        netguard.safe_fetch_json("http://api.test/thing")


# --- the fetch contract (C12): JSON asks for JSON; a body decodes by what it says it is --------------

_JSON_FIRST = "application/json, text/plain;q=0.5, */*;q=0.1"


def _accept_seen(monkeypatch, body: str = '{"ok": true}', ctype: str = "application/json") -> dict:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get_list("accept")
        return httpx.Response(200, headers={"content-type": ctype}, text=body)

    _serve(monkeypatch, handler)
    return seen


def test_fetch_json_asks_for_json_not_a_web_page(monkeypatch) -> None:
    # A content-negotiating API (Django REST Framework: Launch Library 2, jolpica, usaspending) serves
    # its HTML page to a browser Accept. The JSON reader must ask for JSON first.
    seen = _accept_seen(monkeypatch)
    assert netguard.safe_fetch_json("http://api.test/launches") == {"ok": True}
    assert seen["accept"] == [_JSON_FIRST]


def test_fetch_json_keeps_a_callers_own_accept_and_never_sends_two(monkeypatch) -> None:
    seen = _accept_seen(monkeypatch)
    netguard.safe_fetch_json("http://api.test/x", headers={"accept": "application/geo+json"})
    assert seen["accept"] == ["application/geo+json"]
    netguard.safe_fetch_json("http://api.test/x", headers={"X-Api-Key": "k"})
    assert seen["accept"] == [_JSON_FIRST]


def test_post_json_asks_for_json(monkeypatch) -> None:
    seen = _accept_seen(monkeypatch)
    netguard.safe_post_json("http://api.test/search", {"q": "x"})
    assert seen["accept"] == [_JSON_FIRST]


def test_page_fetch_keeps_the_html_accept(monkeypatch) -> None:
    seen = _accept_seen(monkeypatch, "<html>ok</html>", "text/html")
    netguard.safe_fetch_page("http://example.test/page")
    assert len(seen["accept"]) == 1 and seen["accept"][0].startswith("text/html")


_TEXT = '{"name": "Café Zoë – 東京", "n": 1}'


@pytest.mark.parametrize(("content", "ctype"), [
    (_TEXT.encode("utf-8"), "application/json"),
    (_TEXT.encode("utf-8"), "application/json; charset=utf-8"),
    (b"\xef\xbb\xbf" + _TEXT.encode("utf-8"), "application/json"),                 # UTF-8 BOM
    (b"\xff\xfe" + _TEXT.encode("utf-16-le"), "application/json"),                 # UTF-16 LE BOM, no charset
    (b"\xfe\xff" + _TEXT.encode("utf-16-be"), "application/json"),                 # UTF-16 BE BOM, no charset
    (b"\xff\xfe" + _TEXT.encode("utf-16-le"), "application/json; charset=utf-16"),
    (_TEXT.encode("utf-16-le"), "application/json;charset=utf-16"),                # no BOM, generic utf-16
    (_TEXT.encode("utf-16-be"), "application/json;charset=utf-16"),
    (_TEXT.encode("utf-16-le"), "application/json; charset=UTF-16LE"),
    (_TEXT.encode("utf-16-be"), 'application/json; charset="utf-16be"'),
    (_TEXT.encode("utf-16-le"), "application/json"),                               # undeclared, no BOM
    (_TEXT.encode("utf-8"), "application/json; charset=iso-8859-1"),               # UTF-8 under a wrong header
    (_TEXT.encode("utf-8"), "application/json; charset=utf-16"),                   # UTF-8 under a wrong header
], ids=["utf8", "utf8-declared", "utf8-bom", "utf16le-bom", "utf16be-bom", "utf16-bom-declared",
        "utf16le-nobom", "utf16be-nobom", "utf16le-declared", "utf16be-declared-quoted", "utf16le-undeclared",
        "utf8-says-latin1", "utf8-says-utf16"])
def test_decode_body_reads_what_the_bytes_are(content, ctype) -> None:
    assert netguard.decode_body(content, ctype) == _TEXT


@pytest.mark.parametrize("charset", ["latin-1", "iso-8859-1", "ISO-8859-1", "windows-1252"])
def test_decode_body_honours_an_eight_bit_charset(charset) -> None:
    text = '{"city": "Zürich", "note": "déjà vu"}'
    assert netguard.decode_body(text.encode("latin-1"), f"text/plain; charset={charset}") == text


def test_decode_body_windows_1252_punctuation_and_the_utf8_fallback() -> None:
    assert netguard.decode_body("“quoted” — €5".encode("cp1252"), "text/html; charset=windows-1252") \
        == "“quoted” — €5"
    assert netguard.decode_body(b"ok \xff", "text/plain") == "ok �"  # undeclared junk: utf-8, replaced
    assert netguard.decode_body(b"", "application/json") == ""


# The AWS Health Dashboard's official feed (health.aws.amazon.com/public/currentevents), shape as
# served 2026-09-28: 'application/json;charset=utf-16', a UTF-16 LE body with a BOM.
_AWS_CURRENTEVENTS = [
    {"date": "1727500000", "region_name": "Middle East (UAE)", "status": "1", "service": "ec2-me-central-1",
     "service_name": "Amazon Elastic Compute Cloud", "summary": "[RESOLVED] Increased API Error Rates",
     "event_log": [{"summary": "Increased API error rates", "message": "We are investigating…",
                    "status": 1, "timestamp": 1727500000}]},
    {"date": "1727400000", "region_name": "Middle East (Bahrain)", "status": "0", "service": "lambda-me-south-1",
     "service_name": "AWS Lambda", "summary": "Informational message", "event_log": []},
]


@pytest.mark.parametrize("bom", [b"\xff\xfe", b""], ids=["bom", "no-bom"])
def test_fetch_json_parses_a_utf16_feed(monkeypatch, bom) -> None:
    import json

    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    body = bom + json.dumps(_AWS_CURRENTEVENTS, ensure_ascii=False).encode("utf-16-le")
    _serve(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "application/json;charset=utf-16"}, content=body))
    out = netguard.safe_fetch_json("https://health.aws.test/public/currentevents")
    assert out == _AWS_CURRENTEVENTS
    assert out[0]["service"] == "ec2-me-central-1"


def test_text_and_feed_readers_decode_by_charset(monkeypatch) -> None:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-16"},
                                  content="\ufeff<p>Zoë</p>".encode("utf-16-le"))
        return httpx.Response(200, headers={"content-type": "application/rss+xml; charset=iso-8859-1"},
                              content="<rss><title>Zürich</title></rss>".encode("latin-1"))

    _serve(monkeypatch, handler)
    assert "Zürich" in netguard.safe_fetch_feed("http://feeds.test/rss")["text"]
    assert "Zürich" in netguard.safe_fetch_text("http://feeds.test/rss", "xml")["text"]
    assert "Zoë" in netguard.safe_fetch("http://example.test/page")["text"]


@pytest.mark.parametrize("ctype", ["application/geo+json", "application/ld+json; charset=utf-8",
                                   "application/vnd.api+json", "application/problem+json"])
def test_fetch_json_accepts_structured_json_types(monkeypatch, ctype) -> None:
    # api.weather.gov serves every JSON document as application/geo+json whatever the Accept says
    # (verified 2026-09-29): a +json type (RFC 6839) is JSON, never "not_json".
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": ctype},
                                                 text='{"properties": {"gridId": "TSA"}}'))
    assert netguard.safe_fetch_json("https://api.weather.test/points/36.1279,-95.9023") == \
        {"properties": {"gridId": "TSA"}}


def test_fetch_json_still_refuses_a_non_json_media_type(monkeypatch) -> None:
    import httpx

    _resolve_to(monkeypatch, "93.184.216.34")
    _serve(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"))
    with pytest.raises(FetchError) as err:
        netguard.safe_fetch_json("http://api.test/doc")
    assert err.value.kind == "not_json"
