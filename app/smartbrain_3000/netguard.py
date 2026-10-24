"""SSRF-guarded outbound fetch for the web_fetch tool (H4b).

The only place the assistant reaches the public network. Defenses:
  * scheme allowlist (http/https only); reject embedded userinfo;
  * resolve the host and reject if ANY address is private / loopback / link-local
    (covers 169.254.169.254 metadata) / reserved / multicast / unspecified,
    including IPv4-mapped IPv6 (::ffff:) and NAT64 well-known prefix (64:ff9b::/96);
  * PIN the connection to a validated IP per-request (no process-global state):
    the request URL targets the validated IP directly, the original ``Host`` header
    is preserved, and TLS SNI/cert validation use the original hostname via
    httpcore's ``sni_hostname`` extension. A DNS rebind between check and connect
    cannot reach a private address, and a concurrent gateway/Gmail call resolves
    normally (no shared mutation of ``socket.getaddrinfo``);
  * follow redirects manually, re-validating each hop, bounded;
  * connect+read timeout, streamed size cap, content-type allowlist.

web_fetch is a REVIEWED tool, so even a guard bug is gated behind explicit user
approval and audited.
"""

from __future__ import annotations

import codecs
import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx

from . import __version__, vault_format

_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 3
_MAX_BYTES = 8_000_000
_TIMEOUT = 8.0
# _TIMEOUT is a PER-CHUNK read timeout only: a host that drips one byte every <8s keeps the read
# alive until the size cap (512 MiB for a vault) — effectively forever, and on a vault fetch that
# would wedge the whole scheduler tick. An OVERALL wall-clock deadline on the capped read abandons
# such a host. It binds ONLY the vault fetchers (page/ingest byte-behavior is unchanged: they pass
# no deadline). _monotonic is a module attribute so a test can drive the deadline deterministically
# (mirrors vault_sync._monotonic) — no real sleeping.
_VAULT_FETCH_DEADLINE_SECONDS = 60
_monotonic = time.monotonic
_ALLOWED_CT = ("text/", "application/json")
# Knowledge ingestion also accepts PDFs and generic binaries (sniffed downstream),
# with a larger cap since documents are bigger than web pages.
_INGEST_CT = ("text/", "application/json", "application/pdf", "application/xml", "application/octet-stream")
_INGEST_MAX_BYTES = 25_000_000
# §24 http_image: 4 MB cap on the sealed raster body. The sniffed magic bytes
# gate the actual raster shape; content-type is attacker-controlled and ignored
# for trust (an ``image/png`` header on non-PNG bytes still refuses the run).
_IMAGE_MAX_BYTES = 4_000_000
# Public-vault transport (subscribe-by-URL): archives are zips, but tree hosts
# commonly serve them as generic binaries; manifests are JSON, but raw-file hosts
# serve them as text/plain. Prefix match (str.startswith) covers charset suffixes.
# Size caps are imported from vault_format so transport and parser agree.
_VAULT_CT = ("application/zip", "application/octet-stream", "application/x-zip-compressed")
_MANIFEST_CT = ("application/json", "text/plain", "application/octet-stream")
# Every .sbvault is a ZIP. Static hosts routinely serve the unknown `.sbvault` extension with NO
# Content-Type (Caddy) or a wrong one (GitHub raw -> text/plain), which the content-type allowlist
# alone would reject — breaking the "host it anywhere" promise for a perfectly valid vault. So the
# whole-vault fetch ALSO accepts a body whose first bytes are the ZIP magic, regardless of the header.
# This is safe: the content-type was never a security control (it's attacker-controlled), and the
# publisher signature is verified after the fetch — magic-sniffing only widens which hosts work.
_ZIP_MAGIC = b"PK\x03\x04"
# Fetch identity — operator ruling 2026-09-23: HONEST. SmartBrain says what it is
# (and where to learn more) instead of posing as a desktop browser. Evidence, from
# netguard's real request across 43 public hosts incl. .gov/.mil: the honest identity
# never lost to the former "Chrome 124 on Linux" + browser-navigation header set, and
# won on 4 (Yahoo's chart API 429→200, cdc.gov 403→200, Amazon and ESPN bot-challenge
# 202→200). Sites that refuse automated readers refused both (NYTimes, Reuters,
# timeanddate.com — whose earlier "200s the browser set" no longer holds). Only the
# content-negotiation headers ride along: browser navigation metadata (Sec-Fetch-*, a
# user-activation flag) would misdescribe a programmatic fetch. Privacy note: every
# fetched site can tell a SmartBrain user fetched it (docs/07 discloses this).
_USER_AGENT = f"SmartBrain/{__version__} (+https://smartbrain.securecloudgroup.com)"
USER_AGENT = _USER_AGENT  # public: the honest identity every guarded request carries
_FETCH_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# The JSON readers ask for JSON first (field 2026-09-29): a content-negotiating API (Django REST
# Framework — Launch Library 2, jolpica, usaspending) answers the browser Accept above with its HTML
# page, so a live JSON source read as "not_json" and was sent to the page door. The page reader keeps
# the HTML Accept. A caller's own Accept (any case) wins; two Accept headers are never sent.
JSON_ACCEPT = "application/json, text/plain;q=0.5, */*;q=0.1"


def _json_headers(headers: dict | None) -> dict:
    headers = dict(headers or {})
    if not any(str(k).lower() == "accept" for k in headers):
        headers["Accept"] = JSON_ACCEPT
    return headers


# Charset-aware decode (field 2026-09-28: AWS's official status feed is 'application/json;charset=utf-16'
# and read as not_json under a UTF-8-only decode). Order: a byte-order mark, then the declared charset,
# then UTF-8 with replacement. A declared 8-bit charset over bytes that are valid UTF-8 is a wrong header
# (valid non-ASCII UTF-8 is essentially never real Latin-1), and a declared UTF-16 with no NUL pattern
# is 8-bit text under a wrong header too.
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_UTF16 = {"utf-16": None, "utf16": None, "utf-16le": "utf-16-le", "utf-16-le": "utf-16-le",
          "utf-16be": "utf-16-be", "utf-16-be": "utf-16-be"}
_EIGHT_BIT = {"latin-1": "latin-1", "latin1": "latin-1", "iso-8859-1": "latin-1", "iso8859-1": "latin-1",
              "windows-1252": "cp1252", "cp1252": "cp1252"}


def declared_charset(content_type: str) -> str:
    """The ``charset=`` parameter of a Content-Type header, lower-cased; '' when it names none."""
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("'\"").lower()
    return ""


def _utf16_order(content: bytes) -> str | None:
    """The byte order of BOM-less UTF-16 text, from where its NULs sit (ASCII in UTF-16 has one
    NUL per character); None when the bytes show no such pattern. Needs a dense (≥25% of the head)
    one-parity NUL run — a stray NUL in a UTF-8 body is not UTF-16 (field 2026-10-04)."""
    head = content[:64]
    if not head:
        return None
    even = sum(1 for i in range(0, len(head), 2) if head[i] == 0)
    odd = sum(1 for i in range(1, len(head), 2) if head[i] == 0)
    min_nuls = max(len(head) // 4, 1)
    if even >= min_nuls and even > odd:
        return "utf-16-be"
    if odd >= min_nuls and odd > even:
        return "utf-16-le"
    return None


def _is_json_type(content_type: str) -> bool:
    """A structured-syntax JSON type (RFC 6839: application/geo+json, ld+json, vnd.api+json…) is JSON.
    api.weather.gov serves every document as application/geo+json whatever the Accept (verified
    2026-09-29), so a JSON reader that took only application/json refused the whole NWS API."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media.startswith("application/") and media.endswith("+json")


# A text read refuses a binary body by its bytes, whatever its label: the label is the server's word, and the sniff
# read admits every application/* and text/* type (portals label CSVs application/vnd.ms-excel or octet-stream).
_BINARY_MAGIC = (b"%PDF-", _ZIP_MAGIC, b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"\x1f\x8b", b"\xd0\xcf\x11\xe0",
                 b"Rar!", b"7z\xbc\xaf", b"BZh")
_BINARY_HEAD = 1024
# Control bytes text does not carry (tab, newline, form feed and carriage return are text). A file's head is dense with
# them; a text export with one stray NUL is not, so the rule is a share of the head, never a single byte.
_CONTROL_BYTES = bytes(b for b in range(32) if b not in (9, 10, 12, 13)) + b"\x7f"
_BINARY_CONTROL_SHARE = 0.10
# A compressed or encrypted body with no magic is near-random: about half its bytes are high, and they do not form
# UTF-8. Text in a declared legacy charset (Shift_JIS, GBK) is high-byte dense too, so only UTF-8 or no declared
# charset is held to it; undeclared Latin-1 text is a few accented letters, far under the share.
_HIGH_BYTES = bytes(range(128, 256))
_BINARY_HIGH_SHARE = 0.30


def looks_binary(head: bytes, content_type: str = "") -> bool:
    """A body that is a file, not text: a known file magic (pdf, zip/xlsx, png, gif, jpeg, gzip, xls, rar, 7z, bz2);
    a head whose control bytes are 10% or more of it; or a head whose high bytes are 30% or more of it and are not
    UTF-8 when no other charset is declared. UTF-16 is text (a byte-order mark, a declared UTF-16 charset, or the
    one-parity NUL run ``decode_body`` reads as UTF-16)."""
    assert isinstance(head, (bytes, bytearray)), "the body's first bytes"
    head = bytes(head[:_BINARY_HEAD])
    if head.startswith(_BINARY_MAGIC):
        return True
    declared = declared_charset(content_type)
    if not head or head.startswith((b"\xff\xfe", b"\xfe\xff")) or declared in _UTF16 or _utf16_order(head):
        return False
    if len(head) - len(head.translate(None, _CONTROL_BYTES)) >= len(head) * _BINARY_CONTROL_SHARE:
        return True
    high = len(head) - len(head.translate(None, _HIGH_BYTES))
    return high >= len(head) * _BINARY_HIGH_SHARE and declared in ("", "utf-8", "utf8") and not _utf8_head(head)


def _utf8_head(head: bytes) -> bool:
    """The head is UTF-8, allowing a character the 1 KB cut left unfinished."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def decode_body(content: bytes, content_type: str = "") -> str:
    """A fetched body as text: BOM -> declared charset -> UTF-8 (with replacement). Never raises."""
    for bom, codec in _BOMS:
        if content.startswith(bom):
            return content[len(bom):].decode(codec, "replace")
    declared = declared_charset(content_type)
    if declared in _UTF16:
        order = _UTF16.get(declared) or _utf16_order(content)
        if order:
            return content.decode(order, "replace")
    elif not declared:
        order = _utf16_order(content)  # F8: dense NUL run in one parity + strict decode confirms
        if order:
            try:
                return content.decode(order)
            except UnicodeDecodeError:
                pass
    if declared in _EIGHT_BIT:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode(_EIGHT_BIT[declared], "replace")
    return content.decode("utf-8", "replace")


# Round 20 (B3a): bot challenges. A challenge is a refusal R8 never retries and the page
# browser never "solves": the static fetch names it (``FetchError.kind == "challenge"``) so
# the NI ladder never escalates it. Header markers (closed vendor table) mark any status;
# body markers are read only from a bounded head of an HTML error response.
_CHALLENGE_HEADERS: dict[str, tuple[str, ...]] = {
    "cf-mitigated": ("challenge",),                        # Cloudflare
    "x-vercel-mitigated": ("challenge",),                  # Vercel
    "x-amzn-waf-action": ("challenge", "captcha"),         # AWS WAF (a 202 interstitial)
}
_CHALLENGE_BODY_MARKERS: tuple[bytes, ...] = (
    b"challenge-platform", b"cf-chl", b"cf_chl", b"captcha-delivery.com", b"px-captcha",
    b"_incapsula_resource", b"kpsdk", b"awswaf", b"sgcaptcha", b"sucuri_cloudproxy",
    b"verify you are human", b"checking your browser", b"checking if the site connection",
    b"just a moment", b"are you a robot", b"complete the security check",
)
_CHALLENGE_HEAD_BYTES = 16_384
_CHALLENGE_HEAD_DEADLINE_S = 5.0  # a drip error page never stalls a fetch
_MAX_MEDIA = 100


def _media_type(response: httpx.Response) -> str:
    """The response's media type, lowercase and without parameters (bounded)."""
    assert response is not None, "a response"
    raw = str(response.headers.get("content-type", "") or "")
    media = raw.split(";", 1)[0].strip().lower()[:_MAX_MEDIA]
    assert len(media) <= _MAX_MEDIA, "media type is bounded"
    return media


def _challenge_header(response: httpx.Response) -> bool:
    """A closed vendor header says this response is a bot challenge."""
    assert _CHALLENGE_HEADERS, "the vendor table is closed and non-empty"
    assert response is not None, "a response"
    for name, values in _CHALLENGE_HEADERS.items():  # bounded: 3 vendors
        value = str(response.headers.get(name, "") or "").lower()
        if value and any(v in value for v in values):
            return True
    return False


def _is_challenge(response: httpx.Response) -> bool:
    """A refusing response that is a bot check: a vendor header, or (HTML only) a vendor
    marker in the first 16 KB of the body. Never raises; an unreadable body is no marker."""
    assert response.status_code >= 400, "only refusals are judged here"
    assert _CHALLENGE_HEAD_BYTES > 0, "the body head is bounded"
    if _challenge_header(response):
        return True
    if "html" not in _media_type(response):
        return False
    head = b""
    start = _monotonic()
    try:
        for chunk in response.iter_bytes():  # bounded: the head cap and an overall deadline
            head += chunk
            if len(head) >= _CHALLENGE_HEAD_BYTES or _monotonic() - start > _CHALLENGE_HEAD_DEADLINE_S:
                break
    except (httpx.HTTPError, OSError):
        return False
    low = head[:_CHALLENGE_HEAD_BYTES].lower()
    return any(marker in low for marker in _CHALLENGE_BODY_MARKERS)


# RFC 6052 NAT64 well-known prefix — an IPv6 in this /96 wraps an IPv4 in its
# low 32 bits (e.g. 64:ff9b::7f00:1 -> 127.0.0.1, a real loopback bypass).
_NAT64_WK = ipaddress.IPv6Network("64:ff9b::/96")


class FetchError(Exception):
    """A blocked or failed guarded fetch.

    ``status`` carries the HTTP status when the failure is a clean upstream 4xx/5xx (else None).
    Callers that need to distinguish "the publisher took this down" (HTTP 410 Gone) from a
    generic dead host key on it — the guard's message text is not a stable contract.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 kind: str | None = None, content_type: str = "",
                 retry_after: str | None = None) -> None:
        assert message, "message required"
        assert status is None or (100 <= status <= 599), "status must be an HTTP code"
        super().__init__(message)
        self.status = status
        # ``kind`` is the STABLE routing token (field 2026-09-22: the NI page
        # door matched exception class names and never fired in production
        # because this guard wraps everything). "not_json" = the URL is alive
        # and serving real content that simply isn't a JSON API — the one
        # class a caller may legitimately retry through the page pipeline.
        # "challenge" (Round 20, B3a) = the site answered with a bot check: never
        # retried, never handed to the page browser (R8).
        self.kind = kind
        # the refusing response's media type (lowercase, no parameters), "" when
        # unknown — lets a caller tell an API's JSON refusal from a page's
        self.content_type = content_type[:_MAX_MEDIA]
        # the upstream's raw ``Retry-After`` value (seconds or an HTTP date), None when absent;
        # the politeness layer parses it, this class only carries it
        self.retry_after = retry_after[:64] if isinstance(retry_after, str) and retry_after else None


def key_wall(exc: BaseException) -> bool:
    """A refusal that asks for the user's own key (release P3): a 401, or an API's JSON 403. Never a bot check or
    a rate limit (those are walls a key does not open), and never a page's plain 403 (the page browser's)."""
    assert isinstance(exc, BaseException), "an exception"
    status, kind = getattr(exc, "status", None), getattr(exc, "kind", None)
    if kind in ("challenge", "rate_limited"):
        return False
    return status == 401 or (status == 403 and "json" in str(getattr(exc, "content_type", "") or ""))


def _unwrap(addr: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Return the embedded v4 for IPv4-mapped / NAT64 well-known v6, else ``addr``.

    Both wrappers can carry a private/loopback IPv4 inside a v6 that ``is_global``
    would otherwise accept — they must be revalidated as the v4 they really name.
    """
    assert isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)), "ip address required"
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr in _NAT64_WK:
        # low 32 bits of the v6 are the embedded v4 (RFC 6052 well-known prefix).
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr


def _is_unsafe(raw: str) -> bool:
    """Return True if ``raw`` resolves to an address the guard must reject.

    Multicast / reserved / unspecified are rejected explicitly BEFORE the
    ``is_global`` allowlist (``is_global`` does not, on its own, reject every
    multicast / unspecified address in every stdlib version). IPv4-mapped and
    NAT64-wrapped v6 are unwrapped and re-checked as the v4 they encode.
    """
    assert raw, "address string required"
    addr = ipaddress.ip_address(raw)
    addr = _unwrap(addr)
    # Explicit rejects first — the allowlist below is not a substitute for these.
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    # Allowlist: only globally-routable public addresses. is_global rejects
    # private/loopback/link-local/CGNAT(100.64/10)/benchmark/TEST-NET/reserved
    # in one check (a blocklist would keep missing ranges).
    return not addr.is_global


def _validated_ip(host: str) -> str:
    """Resolve ``host`` and return one IP, raising FetchError if any is unsafe."""
    assert host, "host required"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchError(f"cannot resolve host: {exc}") from None
    ips = {info[4][0] for info in infos}
    assert ips, "no addresses resolved"
    for raw in ips:  # bounded by the resolver's answer set
        if _is_unsafe(raw):
            raise FetchError(f"blocked non-global address: {raw}")
    return next(iter(ips))


def _pin_url(url: str, ip: str) -> str:
    """Rewrite ``url`` to target ``ip`` directly while preserving path/query.

    Combined with a preserved ``Host`` header + ``sni_hostname`` extension, this
    pins the TCP connection to the pre-validated IP without touching any global
    resolver state.
    """
    assert url and ip, "url + ip required"
    parsed = urlparse(url)
    netloc = f"[{ip}]" if ":" in ip else ip
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _read_capped(response: httpx.Response, max_bytes: int,
                 deadline_seconds: float | None = None,
                 head_check: Callable[[bytes], None] | None = None) -> bytes:
    """Read a streamed response up to ``max_bytes``; raise if it exceeds it.

    ``head_check`` (text reads only) sees the first ``_BINARY_HEAD`` bytes as soon as they arrive and may raise, so a
    binary body is abandoned after one chunk, not read to the cap.

    ``deadline_seconds`` (vault fetchers only) is an OVERALL wall-clock bound on the read: the
    per-chunk ``_TIMEOUT`` alone lets a drip host stay alive until the cap, so between chunks we
    check elapsed time against a monotonic start and abandon a host that runs past the deadline. A
    drip host must yield a chunk each iteration (every <8s), so the loop body runs and the deadline
    trips; a zero-byte hang is already caught by the read timeout, a connect hang by the connect
    timeout. Page/ingest pass no deadline, so their byte-behavior is unchanged.
    """
    chunks: list[bytes] = []
    total = 0
    start = _monotonic() if deadline_seconds is not None else 0.0
    try:
        for chunk in response.iter_bytes():  # bounded by the size cap AND the deadline (abort early)
            if deadline_seconds is not None and _monotonic() - start > deadline_seconds:
                raise FetchError("host too slow")
            total += len(chunk)
            if total > max_bytes:
                raise FetchError("response too large")
            chunks.append(chunk)
            if head_check is not None and total >= _BINARY_HEAD:
                head_check(b"".join(chunks)[:_BINARY_HEAD])
                head_check = None
    except httpx.HTTPError as exc:
        # A mid-stream failure (reset, timeout, protocol garbage) must be a clean FetchError like
        # every other refusal — never an exception that escapes into a 500 whose log line carries
        # the full request URL. Class name only: httpx messages can embed the URL itself.
        raise FetchError(f"fetch failed while reading: {exc.__class__.__name__}") from None
    body = b"".join(chunks)
    if head_check is not None:  # a body shorter than the head
        head_check(body[:_BINARY_HEAD])
    return body


def _binary_refusal(media: str, ctype: str, kind: str) -> Callable[[bytes], None]:
    """The head check of a text read: a body that ``looks_binary`` is refused with ``kind``, whatever its label."""
    assert kind, "a refusal kind"

    def check(head: bytes) -> None:
        if looks_binary(head, ctype):
            raise FetchError(f"the body is a file, not text ({media or 'unlabelled'})", kind=kind,
                             content_type=media)
    return check


def _send_pinned(client: httpx.Client, url: str, host: str, ip: str,
                 method: str = "GET", content: bytes | None = None,
                 extra_headers: dict | None = None) -> httpx.Response:
    """One pinned request: connect to ``ip``, present ``host`` in Host header + SNI.

    ``method``/``content``/``extra_headers`` widen the SAME pinned transport to the
    keyed search providers (Brave token header, Tavily JSON POST) — none of them
    touch the address validation, redirect discipline, or read caps.

    Streaming response — the caller MUST close it. httpx.Response is NOT a context
    manager (only ``client.stream()`` is), so close it via try/finally, not ``with``.
    """
    assert client is not None and url and host and ip, "client/url/host/ip required"
    assert method in ("GET", "POST"), "only GET/POST ride the guard"
    parsed = urlparse(url)
    host_header = f"{host}:{parsed.port}" if parsed.port is not None else host
    headers = {"Host": host_header, **(extra_headers or {})}
    request = client.build_request(
        method, _pin_url(url, ip), headers=headers, content=content,
        extensions={"sni_hostname": host},
    )
    return client.send(request, stream=True)


# S5a politeness hook. ``ni_polite`` installs an object here (per thread) around the NI fetch
# paths; every other caller of this module sees ``None`` and behaves exactly as before. The object
# decides (a) whether a request may go out now / reuse a recent response (``call``), (b) which
# ports are allowed (``port_ok``) and (c) whether robots.txt lets a page be read (``robots_ok``).
_POLITE = threading.local()


def active_politeness() -> object | None:
    """The politeness object installed on this thread, or None."""
    return getattr(_POLITE, "obj", None)


def set_politeness(obj: object | None) -> object | None:
    """Install ``obj`` (or None) on this thread; return the previous one so a caller restores it."""
    previous = getattr(_POLITE, "obj", None)
    _POLITE.obj = obj
    return previous


def _guarded_get(url: str, allowed_ct: tuple[str, ...], max_bytes: int,
                 deadline_seconds: float | None = None, accept_zip_magic: bool = False,
                 method: str = "GET", content: bytes | None = None,
                 extra_headers: dict | None = None,
                 allow_redirects: bool = True, binary_kind: str | None = None) -> dict:
    """The guarded request, through the thread's politeness object when one is installed."""
    polite = active_politeness()

    def go() -> dict:
        return _guarded_get_once(url, allowed_ct, max_bytes, deadline_seconds, accept_zip_magic,
                                 method, content, extra_headers, allow_redirects, binary_kind)

    if polite is None:
        return go()
    # an Accept header alone is not a credential: only other headers (keys, contact) bar response reuse
    custom = any(str(k).lower() != "accept" for k in (extra_headers or {}))
    return polite.call(url, method, custom, (allowed_ct, max_bytes, binary_kind), go)  # type: ignore[attr-defined]


def _guarded_get_once(url: str, allowed_ct: tuple[str, ...], max_bytes: int,
                      deadline_seconds: float | None = None, accept_zip_magic: bool = False,
                      method: str = "GET", content: bytes | None = None,
                      extra_headers: dict | None = None,
                      allow_redirects: bool = True, binary_kind: str | None = None) -> dict:
    """Shared SSRF-guarded request; return {final_url, status, content_type, content (bytes)}.

    All the defenses (scheme/userinfo/IP allowlist, per-request IP pin with no
    global resolver mutation, bounded redirect re-validation, timeout, size cap,
    content-type allowlist) live here so both ``safe_fetch`` (text) and
    ``safe_fetch_bytes`` (binary) reuse one copy. ``deadline_seconds`` (vault fetchers only)
    adds an overall wall-clock bound on the body read; page/ingest pass None (unchanged). ``binary_kind`` (text
    reads) refuses a body whose first bytes are a file, not text (``looks_binary``), with that kind.
    """
    assert url, "url required"
    assert allowed_ct and max_bytes > 0, "allowed content-types + positive cap required"
    current = url
    # trust_env=False ignores HTTP(S)_PROXY/ALL_PROXY env vars (an env proxy
    # would tunnel past all IP validation).
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False, trust_env=False, headers=_FETCH_HEADERS) as client:
        for _ in range(_MAX_REDIRECTS + 1):  # fixed redirect bound
            parsed = urlparse(current)
            if parsed.scheme not in _SCHEMES:
                raise FetchError("scheme not allowed")
            if parsed.username or parsed.password:
                raise FetchError("userinfo in URL not allowed")
            host = parsed.hostname
            if not host:
                raise FetchError("no host in URL")
            try:
                parsed.port  # noqa: B018 - urlparse validates the port lazily, on ACCESS: a malformed one
            except ValueError:  # (":abc", ":99999") must be a refusal like any other, not a 500
                raise FetchError("invalid port in URL") from None
            polite = active_politeness()
            if polite is not None and not polite.port_ok(parsed.port, host):  # type: ignore[attr-defined]
                raise FetchError("port not allowed", kind="port_refused")
            ip = _validated_ip(host)  # re-validated on every hop
            # httpx.Response (from send(stream=True)) is NOT a context manager — close
            # it explicitly in finally, or the body/connection leaks (and `with` raises).
            try:
                response = _send_pinned(client, current, host, ip, method=method,
                                        content=content, extra_headers=extra_headers)
            except (httpx.HTTPError, OSError) as exc:
                # Refused/timed-out/TLS-failed connections are ordinary fates for a user-supplied
                # URL: report them as FetchError (a clean 4xx upstream), never an unhandled 500
                # whose log line would carry the full URL. Class name only — httpx messages can
                # embed the URL itself, and the fragment-hygiene rule forbids that reaching a log.
                raise FetchError(f"could not connect: {exc.__class__.__name__}") from None
            try:
                if response.is_redirect and method == "POST":
                    # A redirected POST would replay the body under rewritten-method
                    # ambiguity (301/302 vs 307) — the API providers never redirect,
                    # so refuse outright rather than model the edge.
                    raise FetchError("redirect on POST not allowed")
                if response.is_redirect and not allow_redirects:
                    # NI-style callers attach auth headers (secret or literal) whose value
                    # would silently re-send to every hop. A hostile server can 302 to its
                    # own host and harvest the header — a credential-exfiltration path. When
                    # the caller opts out, refuse the very first 3xx so a header never
                    # crosses a redirect boundary.
                    raise FetchError("redirect refused")
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("redirect without location")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:  # an error page is not content
                    raise FetchError(f"upstream returned HTTP {response.status_code}",
                                     status=response.status_code,
                                     kind="challenge" if _is_challenge(response) else None,
                                     content_type=_media_type(response),
                                     retry_after=response.headers.get("retry-after"))
                if _challenge_header(response):  # a 2xx bot check (AWS WAF answers 202)
                    raise FetchError("upstream answered with a bot challenge", kind="challenge")
                ctype = response.headers.get("content-type", "")
                media = _media_type(response)  # media types are case-insensitive (RFC 2045 §5.1)
                ct_ok = media.startswith(allowed_ct) or ("application/json" in allowed_ct and _is_json_type(media))
                if not ct_ok and not accept_zip_magic:
                    raise FetchError(f"content-type not allowed: {ctype or 'unknown'}",
                                     kind="not_json", content_type=media)
                content = _read_capped(response, max_bytes, deadline_seconds,
                                       _binary_refusal(media, ctype, binary_kind) if binary_kind else None)
                # accept_zip_magic: the header didn't match, but a real .sbvault is a ZIP — accept it
                # if the bytes prove it, else it's the host's HTML/error page, so refuse clearly.
                if not ct_ok and not content.startswith(_ZIP_MAGIC):
                    raise FetchError(f"content-type not allowed: {ctype or 'unknown'}")
                # Report the original (host-bearing) URL as the final URL, not the
                # IP-rewritten one the transport actually dialed.
                return {
                    "final_url": current,
                    "status": response.status_code,
                    "content_type": ctype,
                    "content": content,
                }
            finally:
                response.close()
    raise FetchError("too many redirects")


def validate_public_url(url: str) -> None:
    """Run the SSRF guard's pre-fetch URL checks against ``url`` without fetching. Raises
    ``FetchError`` on the same shapes ``_guarded_get`` would refuse before it opens a socket:
    unknown scheme, embedded userinfo, missing/invalid host, invalid port, or an address that
    resolves to a non-global (localhost/LAN/reserved) IP. This exists so a caller storing a URL
    (e.g. a vault's ``hosted_url``) can reuse the guard's rules for validation-only, without a
    fetch attempt at PATCH time; the fetch itself remains the authoritative refusal at use time.

    A DNS-unresolvable host at validation time (transient outage, offline test env) is treated
    as SHAPE-OK — the caller isn't attempting the fetch here, and the eventual verify/fetch is
    where an unreachable host becomes an honest failure. Localhost/LAN literals and hostnames
    that DO resolve to non-global addresses are still rejected: that's the load-bearing rule.
    """
    assert isinstance(url, str) and url, "url required"
    parsed = urlparse(_strip_fragment(url))
    if parsed.scheme not in _SCHEMES:
        raise FetchError("scheme not allowed")
    if parsed.username or parsed.password:
        raise FetchError("userinfo in URL not allowed")
    host = parsed.hostname
    if not host:
        raise FetchError("no host in URL")
    try:
        parsed.port  # noqa: B018 - urlparse validates port lazily, on ACCESS — see _guarded_get
    except ValueError:
        raise FetchError("invalid port in URL") from None
    try:
        _validated_ip(host)  # DNS + is_global allowlist; raises FetchError on non-global
    except FetchError as exc:
        if str(exc).startswith("cannot resolve host"):
            return  # shape OK; the fetch is the honest failure point for a dead/typo host
        raise


def safe_fetch(url: str) -> dict:
    """Fetch ``url`` behind the SSRF guard; return {final_url, status, text}."""
    got = _guarded_get(url, _ALLOWED_CT, _MAX_BYTES)
    return {"final_url": got["final_url"], "status": got["status"],
            "text": decode_body(got["content"], got["content_type"]), "content": got["content"]}


def safe_fetch_json(url: str, headers: dict | None = None,
                    allow_redirects: bool = True) -> dict:
    """Guarded GET returning parsed JSON (search APIs: SearXNG, Brave).

    Same guard as ``safe_fetch``; ``headers`` carries a provider auth header — it
    never influences address validation, redirects, or caps. Bad JSON is a clean
    FetchError, never a decode traceback. ``allow_redirects=False`` refuses any
    3xx so an auth-carrying request never re-sends its headers to a rewritten host
    (credential-exfiltration path — NI's ``_fetch_http_json`` opts out when it
    attaches ANY header, secret or literal).
    """
    import json

    got = _guarded_get(url, ("application/json", "text/"), _MAX_BYTES,
                       extra_headers=_json_headers(headers), allow_redirects=allow_redirects)
    text = decode_body(got["content"], got["content_type"])
    try:
        return json.loads(text)
    except ValueError:
        from .formats import looks_like_html  # lazy: formats is a pure-text leaf
        if looks_like_html(text):  # the body decides, not the header: a maintenance page served as JSON
            raise FetchError("upstream returned a web page, not JSON", kind="not_json",
                             content_type="text/html") from None
        raise FetchError("upstream returned invalid JSON", kind="not_json") from None


def safe_post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    """Guarded JSON POST returning parsed JSON (search APIs: Tavily).

    Identical validation/pinning/caps as every other guarded call; redirects on
    POST are refused outright (no method-rewrite ambiguity).
    """
    import json

    body = json.dumps(payload).encode("utf-8")
    got = _guarded_get(url, ("application/json", "text/"), _MAX_BYTES, method="POST",
                       content=body, extra_headers=_json_headers({"Content-Type": "application/json",
                                                                  **(headers or {})}))
    try:
        return json.loads(decode_body(got["content"], got["content_type"]))
    except ValueError:
        raise FetchError("upstream returned invalid JSON", kind="not_json") from None


def safe_fetch_page(url: str, headers: dict | None = None,
                    allow_redirects: bool = True,
                    deadline_seconds: float | None = None) -> dict:
    """Guarded GET for the ``http_page`` NI source — HTML bytes for the subprocess jail.

    Same SSRF guard as every other fetcher, with the html-friendly content-type set
    (``text/html``, ``application/xhtml+xml``, plus the generic ``text/``) so pages
    served with a plain ``text/plain`` still land. Returns ``{final_url, status,
    content_type, content (bytes)}`` — the caller feeds ``content`` into
    ``jailrun.run_extractor`` (no in-process HTML parse, per §15 + §16).
    ``allow_redirects=False`` is threaded through when the NI caller attaches any
    header (mirrors ``safe_fetch_json`` credential-exfiltration guard).
    """
    polite = active_politeness()
    if polite is not None and not polite.robots_ok(url):  # type: ignore[attr-defined]
        raise FetchError("robots.txt asks automated readers to stay out", kind="robots")
    got = _guarded_get(
        url, ("text/html", "application/xhtml+xml", "text/"),
        _MAX_BYTES, extra_headers=headers, allow_redirects=allow_redirects,
        deadline_seconds=deadline_seconds,
    )
    return {"final_url": got["final_url"], "status": got["status"],
            "content_type": got["content_type"], "content": got["content"]}


def safe_fetch_image(url: str, headers: dict | None = None,
                     allow_redirects: bool = True,
                     deadline_seconds: float | None = None) -> dict:
    """Guarded GET for the ``http_image`` NI source (§24) — raster bytes for sealing.

    Same SSRF guard as every other fetcher, with the image content-type set
    (``image/*``, plus ``application/octet-stream`` which raw-file hosts commonly
    serve unknown image extensions as). 4 MB cap (§24), overall wall-clock deadline
    against drip hosts (mirrors ``safe_fetch_page``). Returns ``{final_url, status,
    content_type, content (bytes)}`` — the caller sniffs the magic bytes and refuses
    anything but the §24 allowlist (PNG / JPEG / GIF / WebP), because the served
    header is attacker-controlled and the sniffed type is what gets stored.
    ``allow_redirects=False`` is threaded through when the NI caller attaches any
    header (mirrors ``safe_fetch_json`` / ``safe_fetch_page``).
    """
    got = _guarded_get(
        url, ("image/", "application/octet-stream"),
        _IMAGE_MAX_BYTES, extra_headers=headers, allow_redirects=allow_redirects,
        deadline_seconds=deadline_seconds,
    )
    return {"final_url": got["final_url"], "status": got["status"],
            "content_type": got["content_type"], "content": got["content"]}


def safe_fetch_feed(url: str) -> dict:
    """Guarded GET for RSS/Atom documents — same SSRF guard, XML-friendly content types.

    Feeds ship as application/rss+xml, application/atom+xml, text/xml, application/xml,
    and occasionally text/html from sloppy servers; the parser rejects non-feeds, so the
    content-type gate here is about class (textual), not format.
    """
    got = _guarded_get(url, ("application/", "text/"), _MAX_BYTES)
    return {"final_url": got["final_url"], "status": got["status"],
            "text": decode_body(got["content"], got["content_type"])}


# --- textual NI sources (§3 http_json ``format``: csv / feed / xml / text) ------
#
# Same SSRF machinery + streamed cap as safe_fetch_json; only the content-type
# allowlist changes, matched to what the format's real hosts actually serve.
# The redirect discipline is threaded through by the caller (NI's engine opts
# out of redirects whenever ANY header is attached — verbatim mirror of
# safe_fetch_json's credential-exfiltration guard).
_CSV_CT = ("text/csv", "application/csv", "text/plain",
           "application/octet-stream", "text/")
_FEED_CT = ("application/", "text/")
_XML_CT = ("application/", "text/")
_TEXT_CT = ("text/",)
# ``sniff``: an address whose format nobody sealed (a pasted link, a research fetch), read to find out what it is.
# Like the feed and xml reads it admits every application/* and text/* label, and the bytes decide: a binary body is
# refused by ``looks_binary`` on every textual read, and the sniffer and parsers refuse what is not their format.
_SNIFF_CT = ("application/", "text/")
_FORMAT_CT: dict[str, tuple[str, ...]] = {
    "csv": _CSV_CT, "feed": _FEED_CT, "xml": _XML_CT, "text": _TEXT_CT, "sniff": _SNIFF_CT,
}
_FORMAT_KIND: dict[str, str] = {
    "csv": "not_csv", "feed": "not_feed", "xml": "not_xml", "text": "not_text", "sniff": "not_text",
}


def safe_fetch_text(url: str, fmt: str, headers: dict | None = None,
                    allow_redirects: bool = True) -> dict:
    """Guarded GET returning ``{final_url, status, content_type, text, content}`` for a
    non-JSON textual NI source (``format`` ∈ {csv, feed, xml, text}), or an unsealed address read to sniff
    its format (``sniff``).

    Same SSRF guard + streamed byte cap as ``safe_fetch_json``; the content-type
    allowlist is scoped per-format (see ``_FORMAT_CT``). The caller (NI's
    ``_fetch_http_json`` dispatch) is responsible for the redirect discipline —
    ``allow_redirects=False`` whenever ANY header is attached, mirroring the
    credential-exfiltration guard on the JSON path. Bad-shape parse failures
    downstream translate to ``FetchError(kind="not_<fmt>")``, keyed off
    ``_FORMAT_KIND`` so callers can route "the URL is alive but wrong shape"
    without matching on message text.
    """
    assert isinstance(fmt, str) and fmt in _FORMAT_CT, "fmt must be a supported textual format"
    assert isinstance(url, str) and url, "url required"
    got = _guarded_get(url, _FORMAT_CT[fmt], _MAX_BYTES,
                       extra_headers=headers, allow_redirects=allow_redirects, binary_kind=_FORMAT_KIND[fmt])
    return {"final_url": got["final_url"], "status": got["status"],
            "content_type": got["content_type"],
            "text": decode_body(got["content"], got["content_type"])}


def format_error_kind(fmt: str) -> str:
    """Return the ``FetchError.kind`` a parse failure of ``fmt`` should carry."""
    assert isinstance(fmt, str) and fmt in _FORMAT_KIND, "fmt must be a supported textual format"
    return _FORMAT_KIND[fmt]


def safe_fetch_bytes(url: str) -> dict:
    """Fetch ``url`` behind the SSRF guard, returning raw bytes (for ingestion).

    Same protections as ``safe_fetch`` but a wider content-type allowlist + larger
    cap so PDFs/documents can be ingested. Returns {final_url, status,
    content_type, content (bytes)}; the caller sniffs/extracts the bytes.
    """
    return _guarded_get(url, _INGEST_CT, _INGEST_MAX_BYTES)


def _strip_fragment(url: str) -> str:
    """Drop any ``#fragment`` before the URL reaches resolution, fetch, or a log.

    A sealed-share URL carries its key material in the fragment (``#k=<key>``);
    stripping here — before anything else sees the URL — guarantees the key can
    never reach the remote host, an error message, or a log line.
    """
    assert url, "url required"
    return urldefrag(url).url


def safe_fetch_vault(url: str) -> bytes:
    """Fetch a public-vault archive behind the SSRF guard; return raw bytes.

    Same defenses as ``safe_fetch``, with vault-shaped bounds: zip-ish content
    types only, capped at ``vault_format.MAX_VAULT_BYTES``. The cap is enforced
    on the byte stream as it arrives (``_read_capped``) — a lying Content-Length
    header cannot bypass it — and an overall deadline abandons a drip host.
    """
    return _guarded_get(_strip_fragment(url), _VAULT_CT, vault_format.MAX_VAULT_BYTES,
                        _VAULT_FETCH_DEADLINE_SECONDS, accept_zip_magic=True)["content"]


def safe_fetch_vault_manifest(url: str) -> bytes:
    """Fetch a public-vault manifest (update check) behind the SSRF guard.

    Manifests are small JSON documents — often served as ``text/plain`` by
    raw-file hosts — stream-capped at ``vault_format.MAX_MANIFEST_BYTES``, with an
    overall deadline that abandons a drip host.
    """
    return _guarded_get(_strip_fragment(url), _MANIFEST_CT, vault_format.MAX_MANIFEST_BYTES,
                        _VAULT_FETCH_DEADLINE_SECONDS)["content"]


def safe_fetch_ni_pack(url: str, max_bytes: int) -> bytes:
    """Fetch a Neural Interface template pack (§19) behind the SSRF guard — RAW bytes.

    Packs are canonical JSON: signature verification must run against the EXACT bytes
    the host served (a re-decode / re-encode would break canonical form). Same SSRF
    machinery as vault manifests + the streamed byte cap; content-type set accepts JSON
    plus the two textual/raw fallbacks raw-file hosts commonly serve.
    """
    assert 0 < max_bytes, "cap must be positive"
    return _guarded_get(_strip_fragment(url), _MANIFEST_CT, max_bytes,
                        _VAULT_FETCH_DEADLINE_SECONDS)["content"]


_LIBRARY_PACK_CT = ("application/octet-stream", "application/gzip", "application/x-gzip")


def safe_fetch_library_pack(url: str, max_bytes: int) -> bytes:
    """Fetch the SmartBrain Library pack (a gzip of a DuckDB file) behind the SSRF guard — RAW bytes.

    The caller verifies the bytes against the sha256 this app release pins (ruling R12), so the
    content type is not a trust signal; GitHub release assets redirect to a CDN host, which the
    guard re-validates on every hop. Streamed byte cap + the vault fetchers' wall-clock deadline.
    """
    assert 0 < max_bytes, "cap must be positive"
    return _guarded_get(_strip_fragment(url), _LIBRARY_PACK_CT, max_bytes,
                        _VAULT_FETCH_DEADLINE_SECONDS)["content"]


def safe_fetch_vault_object(url: str, max_bytes: int) -> bytes:
    """Fetch one tree-hosted vault entry (index.bin / objects/*.bin) behind the SSRF guard.

    The tree delta path fetches only the objects an update actually changed. The byte cap comes
    from the caller because it differs by entry kind (index vs document vs vectors) — always one
    of ``vault_format``'s explicit bounds, so transport and parser agree. Content types match the
    manifest fetch: raw-file hosts serve ``.bin`` as octet-stream or text/plain.
    """
    assert 0 < max_bytes <= vault_format.MAX_VAULT_BYTES, "cap must be a vault_format bound"
    return _guarded_get(_strip_fragment(url), _MANIFEST_CT, max_bytes,
                        _VAULT_FETCH_DEADLINE_SECONDS)["content"]
