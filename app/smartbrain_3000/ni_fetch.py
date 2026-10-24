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

from . import formats, jailrun, netguard, ni_polite, ni_table

TEXTUAL_FORMATS = frozenset({"csv", "feed", "xml", "text"})
SNIFF_HEAD_BYTES = 4096
PAGE_DEADLINE_S = 15.0   # wall-clock bound on a page body read (a drip host)

# The rate-limiter seam: called with the URL before every guarded request, may block or raise FetchError.
before_request: Callable[[str], None] | None = None


class ParseError(netguard.FetchError):
    """The body arrived but is not the sealed format (``kind`` = ``not_<fmt>``)."""


class HtmlPage(netguard.FetchError):
    """A sniffed fetch found an ordinary web page, already fetched (so a caller that reads page tables does not fetch
    it a second time); every other caller sees the same ``not_text`` refusal as before. It carries the raw ``content``
    bytes and the ``content_type`` header, so the jail decodes the page by its one rule, exactly as a refresh does;
    ``text`` (decoded by the header alone) is for sniffing only. A recorded fetch (the eval cassette) holds text and no
    bytes: ``body`` is then the text as UTF-8 and ``charset`` says so."""

    def __init__(self, text: str, content: bytes | None = None, content_type: str = "") -> None:
        assert isinstance(text, str), "the page text"
        assert content is None or isinstance(content, (bytes, bytearray)), "the page bytes"
        assert isinstance(content_type, str), "the content type"
        super().__init__("an HTML page", kind="not_text")
        self.text = text
        self.content = bytes(content) if content is not None else None
        self.content_type = content_type

    @property
    def body(self) -> bytes:
        return self.content if self.content is not None else self.text.encode("utf-8")

    @property
    def charset(self) -> str:
        """The header charset to hand the jail ('' when none); UTF-8 for re-encoded recorded text."""
        return netguard.declared_charset(self.content_type) if self.content is not None else "utf-8"


def _get(url: str, fmt: str, headers: dict | None, allow_redirects: bool, port: int | None = None) -> dict:
    """The only place this module touches the network, politeness-engaged here and nowhere else (``port``
    is the one extra port a Library row declared). ``fmt`` ``json`` returns the parsed JSON itself."""
    assert isinstance(url, str) and url, "url required"
    assert fmt in ("json", "text", "sniff") or fmt in TEXTUAL_FORMATS, "a known format"
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


def sniff_fetch(url: str, *, allow_redirects: bool = True, port: int | None = None,
                keep_page: bool = False) -> tuple[object, str]:
    """Paste-URL fetch: try JSON first; on a ``not_json`` refusal sniff the body and parse, returning
    ``(parsed, format)``. Every other FetchError rides through; an HTML page, a binary file or a body no parser
    reads re-raises the JSON refusal so the flow routes it to the page door. A fault on the second read (the
    host asked us to wait, a dropped connection, an oversize body) rides through as that fault, never as "not
    data". ``keep_page`` True raises ``HtmlPage`` (the body already fetched) instead, for a caller that reads
    page tables."""
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
    except HtmlPage:
        if keep_page:
            raise
        raise page from None
    except netguard.FetchError as exc:
        if not str(getattr(exc, "kind", None) or "").startswith("not_"):
            raise  # a fault, not the shape of the body: reported as "not data" it would read as a decline
        raise page from None


def _sniffed_textual(url: str, allow_redirects: bool = True, port: int | None = None) -> tuple[object, str]:
    """Fetch as text and parse by what it really is; HTML is refused (the page door reads pages). The ``sniff`` read
    admits any application/* or text/* label and refuses a binary body by its bytes, so a feed labelled
    application/rss+xml or a CSV labelled application/vnd.ms-excel is read, not refused."""
    assert url.startswith(("http://", "https://")), "an http(s) url"
    assert isinstance(allow_redirects, bool), "a redirect flag"
    got = _get(url, "sniff", None, allow_redirects, port)
    text = got.get("text") if isinstance(got, dict) else ""
    if not isinstance(text, str):
        raise netguard.FetchError("no text body", kind="not_json") from None
    ct = str(got.get("content_type") or "")
    head = text[:SNIFF_HEAD_BYTES].lstrip().lower()
    if "html" in ct.lower() or head.startswith(("<!doctype html", "<html")):
        raw = got.get("content")
        raise HtmlPage(text, raw if isinstance(raw, (bytes, bytearray)) else None, ct)
    sniffed = formats.sniff_format(ct, text[:SNIFF_HEAD_BYTES])
    if sniffed == "json":
        # a server that lied about its content-type but served JSON: decode the bytes already held
        try:
            return json.loads(text), "json"
        except ValueError as exc:
            raise netguard.FetchError(f"upstream JSON reparse failed: {exc}", kind="not_json") from None
    return parse_textual(text, sniffed), sniffed


def page_tables(html: bytes, url: str, *, charset: str = "") -> dict:
    """The data tables of a page body, read inside the jail (HTML never parses in-process): ``{"tables": [read
    table...], "feeds": [href...], "entities": [type...], "extract": the jail payload}``. ``extract`` is for the
    page verifier alone (it grounds a reading against the page); values are only ever read from ``tables``.
    Raises ``netguard.FetchError`` (kind ``not_table``) when the jail cannot read the page."""
    assert isinstance(html, (bytes, bytearray)), "page bytes"
    assert isinstance(url, str), "url required"
    try:
        got = jailrun.run_extractor(bytes(html), url_hint=url, declared_charset=charset)
    except jailrun.JailError:
        raise netguard.FetchError("the page could not be read", kind="not_table") from None
    entities = [str(e.get("type")) for e in (got.get("entities") or [])[:8] if isinstance(e, dict) and e.get("type")]
    return {"tables": ni_table.read_all(got.get("tables")),
            "feeds": [str(f) for f in (got.get("feeds") or [])[:ni_table.MAX_COLUMNS] if isinstance(f, str)],
            "entities": entities, "extract": got}


def fetch_page_tables(url: str, *, allow_redirects: bool = True, port: int | None = None) -> dict:
    """GET ``url`` as an HTML page (the one polite, guarded path) and read its tables: ``page_tables``' value.
    ``allow_redirects`` False refuses a redirect, as every research and keyed fetch does."""
    assert isinstance(url, str) and url, "url required"
    assert allow_redirects in (True, False), "a redirect flag"
    if before_request is not None:
        before_request(url)
    with ni_polite.engaged(port=port):
        got = netguard.safe_fetch_page(url, allow_redirects=allow_redirects, deadline_seconds=PAGE_DEADLINE_S)
        body = got.get("content") if isinstance(got, dict) else None
        if not isinstance(body, (bytes, bytearray)):
            raise netguard.FetchError("no bytes in page response", kind="not_table")
        return page_tables(bytes(body), url, charset=netguard.declared_charset(str(got.get("content_type") or "")))


def fetch_table(url: str, locator: dict, *, allow_redirects: bool = True, port: int | None = None) -> dict:
    """The sealed table of the page at ``url`` as a pipeline payload (``ni_table.payload``). A page without the
    sealed table is ``ni_table.TableGone`` (the caller reports it; it never reads a different table)."""
    assert isinstance(locator, dict), "a sealed locator"
    assert isinstance(url, str) and url, "url required"
    page = fetch_page_tables(url, allow_redirects=allow_redirects, port=port)
    _index, table = ni_table.locate(page["tables"], locator)
    return ni_table.payload(table)
