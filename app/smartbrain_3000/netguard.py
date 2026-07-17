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

import ipaddress
import socket
import time
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx

from . import vault_format

_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 3
_MAX_BYTES = 2_000_000
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
# Public-vault transport (subscribe-by-URL): archives are zips, but tree hosts
# commonly serve them as generic binaries; manifests are JSON, but raw-file hosts
# serve them as text/plain. Prefix match (str.startswith) covers charset suffixes.
# Size caps are imported from vault_format so transport and parser agree.
_VAULT_CT = ("application/zip", "application/octet-stream", "application/x-zip-compressed")
_MANIFEST_CT = ("application/json", "text/plain", "application/octet-stream")
# A browser-like UA: many public doc/PDF hosts (e.g. .mil/.gov) 403 a default
# client UA. We fetch only what the user explicitly asked for, on their behalf.
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# RFC 6052 NAT64 well-known prefix — an IPv6 in this /96 wraps an IPv4 in its
# low 32 bits (e.g. 64:ff9b::7f00:1 -> 127.0.0.1, a real loopback bypass).
_NAT64_WK = ipaddress.IPv6Network("64:ff9b::/96")


class FetchError(Exception):
    """A blocked or failed guarded fetch."""


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
                 deadline_seconds: float | None = None) -> bytes:
    """Read a streamed response up to ``max_bytes``; raise if it exceeds it.

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
    except httpx.HTTPError as exc:
        # A mid-stream failure (reset, timeout, protocol garbage) must be a clean FetchError like
        # every other refusal — never an exception that escapes into a 500 whose log line carries
        # the full request URL. Class name only: httpx messages can embed the URL itself.
        raise FetchError(f"fetch failed while reading: {exc.__class__.__name__}") from None
    return b"".join(chunks)


def _send_pinned(client: httpx.Client, url: str, host: str, ip: str) -> httpx.Response:
    """One pinned GET: connect to ``ip``, present ``host`` in Host header + SNI.

    Streaming response — the caller MUST close it. httpx.Response is NOT a context
    manager (only ``client.stream()`` is), so close it via try/finally, not ``with``.
    """
    assert client is not None and url and host and ip, "client/url/host/ip required"
    parsed = urlparse(url)
    host_header = f"{host}:{parsed.port}" if parsed.port is not None else host
    request = client.build_request(
        "GET", _pin_url(url, ip), headers={"Host": host_header},
        extensions={"sni_hostname": host},
    )
    return client.send(request, stream=True)


def _guarded_get(url: str, allowed_ct: tuple[str, ...], max_bytes: int,
                 deadline_seconds: float | None = None) -> dict:
    """Shared SSRF-guarded GET; return {final_url, status, content_type, content (bytes)}.

    All the defenses (scheme/userinfo/IP allowlist, per-request IP pin with no
    global resolver mutation, bounded redirect re-validation, timeout, size cap,
    content-type allowlist) live here so both ``safe_fetch`` (text) and
    ``safe_fetch_bytes`` (binary) reuse one copy. ``deadline_seconds`` (vault fetchers only)
    adds an overall wall-clock bound on the body read; page/ingest pass None (unchanged).
    """
    assert url, "url required"
    assert allowed_ct and max_bytes > 0, "allowed content-types + positive cap required"
    current = url
    # trust_env=False ignores HTTP(S)_PROXY/ALL_PROXY env vars (an env proxy
    # would tunnel past all IP validation).
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False, trust_env=False, headers=headers) as client:
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
                parsed.port  # urlparse validates the port lazily, on ACCESS: a malformed one
            except ValueError:  # (":abc", ":99999") must be a refusal like any other, not a 500
                raise FetchError("invalid port in URL") from None
            ip = _validated_ip(host)  # re-validated on every hop
            # httpx.Response (from send(stream=True)) is NOT a context manager — close
            # it explicitly in finally, or the body/connection leaks (and `with` raises).
            try:
                response = _send_pinned(client, current, host, ip)
            except (httpx.HTTPError, OSError) as exc:
                # Refused/timed-out/TLS-failed connections are ordinary fates for a user-supplied
                # URL: report them as FetchError (a clean 4xx upstream), never an unhandled 500
                # whose log line would carry the full URL. Class name only — httpx messages can
                # embed the URL itself, and the fragment-hygiene rule forbids that reaching a log.
                raise FetchError(f"could not connect: {exc.__class__.__name__}") from None
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("redirect without location")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:  # an error page is not content
                    raise FetchError(f"upstream returned HTTP {response.status_code}")
                ctype = response.headers.get("content-type", "")
                if not ctype.startswith(allowed_ct):
                    raise FetchError(f"content-type not allowed: {ctype or 'unknown'}")
                content = _read_capped(response, max_bytes, deadline_seconds)
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


def safe_fetch(url: str) -> dict:
    """Fetch ``url`` behind the SSRF guard; return {final_url, status, text}."""
    got = _guarded_get(url, _ALLOWED_CT, _MAX_BYTES)
    return {"final_url": got["final_url"], "status": got["status"], "text": got["content"].decode("utf-8", "replace")}


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
                        _VAULT_FETCH_DEADLINE_SECONDS)["content"]


def safe_fetch_vault_manifest(url: str) -> bytes:
    """Fetch a public-vault manifest (update check) behind the SSRF guard.

    Manifests are small JSON documents — often served as ``text/plain`` by
    raw-file hosts — stream-capped at ``vault_format.MAX_MANIFEST_BYTES``, with an
    overall deadline that abandons a drip host.
    """
    return _guarded_get(_strip_fragment(url), _MANIFEST_CT, vault_format.MAX_MANIFEST_BYTES,
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
