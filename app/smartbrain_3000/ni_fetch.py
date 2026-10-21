"""The one fetch-and-parse path (plan 6.2 / P33): URL + sealed format + access -> a walker-shaped value.

The flow's sampling fetch (``ni_flow``) and the tick's ``ni.run_item`` fetch both go through here, so a
source is fetched, size-capped, content-type checked and parsed one way. Every request goes through
``netguard`` (SSRF pin, redirect re-validation, byte cap) and every request passes ``_get``, the single
chokepoint. ``_get`` is also where S5a's politeness is engaged (``ni_polite.engaged``: per-host budget,
Retry-After pause, fetch dedupe, port policy, installed on the thread so netguard's guarded request applies
it to every hop), once for JSON, textual and sniffed fetches alike. ``before_request`` stays as a plain
URL-only seam for a caller that needs to veto a request.

This module imports neither ``ni`` nor ``ni_flow``; callers translate ``FetchError`` to their own error type.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from . import formats, netguard, ni_polite

TEXTUAL_FORMATS = frozenset({"csv", "feed", "xml", "text"})
SNIFF_HEAD_BYTES = 4096

# The rate-limiter seam: called with the URL before every guarded request, may block or raise FetchError.
before_request: Callable[[str], None] | None = None


class ParseError(netguard.FetchError):
    """The body arrived but is not the sealed format (``kind`` = ``not_<fmt>``)."""


def _get(url: str, fmt: str, headers: dict | None, allow_redirects: bool, port: int | None = None) -> dict:
    """The only place this module touches the network, politeness-engaged here and nowhere else (``port``
    is the one extra port a Library row declared). ``fmt`` ``json`` returns the parsed JSON itself."""
    assert isinstance(url, str) and url, "url required"
    assert fmt == "json" or fmt in TEXTUAL_FORMATS or fmt == "text", "a known format"
    if before_request is not None:
        before_request(url)
    with ni_polite.engaged(port=port):
        if fmt == "json":
            return netguard.safe_fetch_json(url, headers=headers, allow_redirects=allow_redirects)
        return netguard.safe_fetch_text(url, fmt, headers=headers, allow_redirects=allow_redirects)


def parse_textual(text: str, fmt: str) -> object:
    """Parse ``text`` per ``fmt``; a parse failure is a ``ParseError`` with kind ``not_<fmt>``."""
    assert isinstance(text, str) and isinstance(fmt, str), "args required"
    assert fmt in TEXTUAL_FORMATS, "fmt must be a textual format"
    try:
        if fmt == "csv":
            return formats.parse_csv(text)
        if fmt == "feed":
            return formats.parse_feed(text)
        if fmt == "xml":
            return formats.parse_xml(text)
        return formats.parse_text(text)
    except formats.HtmlBody:
        raise ParseError(f"{fmt} source returned a web page, not its data",
                         kind=netguard.format_error_kind(fmt), content_type="text/html") from None
    except formats.FormatError as exc:
        raise ParseError(f"parse failed for {fmt}: {exc}", kind=netguard.format_error_kind(fmt)) from None


def fetch_parsed(url: str, fmt: str = "json", *, headers: dict | None = None,
                 allow_redirects: bool = True, port: int | None = None) -> object:
    """Fetch ``url`` as sealed format ``fmt`` and return the parsed value.

    ``headers`` is the access the caller resolved (keys never reach this module unresolved); callers refuse
    redirects whenever they attach any. Raises ``netguard.FetchError`` (``ParseError`` for a shape mismatch).
    """
    assert isinstance(url, str) and url, "url required"
    assert fmt == "json" or fmt in TEXTUAL_FORMATS, "fmt must be json or a textual format"
    got = _get(url, fmt, headers or None, allow_redirects, port)
    if fmt == "json":
        return got
    text = got.get("text") if isinstance(got, dict) else ""
    if not isinstance(text, str):
        raise ParseError(f"no text body for {fmt}", kind=netguard.format_error_kind(fmt))
    return parse_textual(text, fmt)


def sniff_fetch(url: str, *, allow_redirects: bool = True, port: int | None = None) -> tuple[object, str]:
    """Paste-URL fetch: try JSON first; on a ``not_json`` refusal sniff the body and parse, returning
    ``(parsed, format)``. Every other FetchError rides through; an HTML page re-raises the JSON refusal so
    the flow routes it to the page door."""
    assert allow_redirects in (True, False), "a redirect flag"
    assert isinstance(url, str) and url, "url required"
    try:
        return _get(url, "json", None, allow_redirects, port), "json"
    except netguard.FetchError as exc:
        if getattr(exc, "kind", None) != "not_json":
            raise
        page = exc  # alive but not JSON: a data file, or an ordinary web page
    try:
        return _sniffed_textual(url, allow_redirects, port)
    except netguard.FetchError:
        raise page from None


def _sniffed_textual(url: str, allow_redirects: bool = True, port: int | None = None) -> tuple[object, str]:
    """Fetch as text and parse by what it really is; HTML is refused (the page door reads pages)."""
    assert url.startswith(("http://", "https://")), "an http(s) url"
    assert isinstance(allow_redirects, bool), "a redirect flag"
    got = _get(url, "text", None, allow_redirects, port)
    text = got.get("text") if isinstance(got, dict) else ""
    if not isinstance(text, str):
        raise netguard.FetchError("no text body", kind="not_json") from None
    ct = str(got.get("content_type") or "")
    head = text[:SNIFF_HEAD_BYTES].lstrip().lower()
    if "html" in ct.lower() or head.startswith(("<!doctype html", "<html")):
        raise netguard.FetchError("an HTML page", kind="not_text")
    sniffed = formats.sniff_format(ct, text[:SNIFF_HEAD_BYTES])
    if sniffed == "json":
        # a server that lied about its content-type but served JSON: decode the bytes already held
        try:
            return json.loads(text), "json"
        except ValueError as exc:
            raise netguard.FetchError(f"upstream JSON reparse failed: {exc}", kind="not_json") from None
    return parse_textual(text, sniffed), sniffed
