"""Keyless web search via DuckDuckGo, behind the SSRF guard.

The assistant has ``web_fetch`` to read a known URL, but answering "search the web
for X" requires discovering pages. This returns result {title, url, snippet} triples
parsed from DuckDuckGo's no-JS HTML, fetched through the same SSRF-guarded path as
every other egress. No API key, no third-party SaaS.

DuckDuckGo throttles scrapers intermittently (HTTP 403), so we try the Lite mirror
first and fall back to the classic HTML endpoint; the parser handles both markups
(``result-link``/``result__a`` titles, ``result-snippet``/``result__snippet`` snippets).
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from . import netguard

# Tried in order; the first that returns results wins (resilience against an intermittent 403).
_ENDPOINTS = (
    "https://lite.duckduckgo.com/lite/?q={q}",
    "https://html.duckduckgo.com/html/?q={q}",
)
_MAX_RESULTS = 10


class SearchError(Exception):
    """A failed web search (upstream blocked/unavailable)."""


def _decode_url(href: str) -> str:
    """Resolve a DuckDuckGo redirect href (//duckduckgo.com/l/?uddg=...) to the target."""
    if not href:
        return ""
    parsed = urlparse(href if href.startswith("http") else "https:" + href)
    target = parse_qs(parsed.query).get("uddg")
    return unquote(target[0]) if target else href


class _ResultParser(HTMLParser):
    """Collect result titles (a.result-link / a.result__a) + snippets (.result-snippet /
    .result__snippet). Snippets may be on a non-anchor tag (Lite uses a ``td``), so the
    parser tracks which tag opened the current capture and closes on the matching end tag."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self._mode: str | None = None  # "title" | "snippet" | None
        self._tag = ""  # the tag that opened the current capture
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "a" and ("result-link" in cls or "result__a" in cls):
            self._mode, self._tag, self._href, self._buf = "title", tag, a.get("href", "") or "", []
        elif "result-snippet" in cls or "result__snippet" in cls:
            self._mode, self._tag, self._buf = "snippet", tag, []

    def handle_data(self, data: str) -> None:
        if self._mode is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._mode is None or tag != self._tag:
            return
        text = "".join(self._buf).strip()
        if self._mode == "title":
            self.results.append({"title": text, "url": _decode_url(self._href), "snippet": ""})
        elif self._mode == "snippet" and self.results:
            self.results[-1]["snippet"] = text
        self._mode, self._tag = None, ""


def parse_results(html: str, limit: int) -> list[dict]:
    """Extract up to ``limit`` {title, url, snippet} results from DuckDuckGo HTML."""
    assert isinstance(html, str), "html must be str"
    assert 1 <= limit <= _MAX_RESULTS, "limit out of range"
    parser = _ResultParser()
    parser.feed(html)
    return [r for r in parser.results if r["title"] and r["url"].startswith("http")][:limit]


def web_search(query: str, limit: int = 5) -> list[dict]:
    """Search the web (DuckDuckGo) and return result {title, url, snippet} triples."""
    assert query and query.strip(), "query required"
    limit = min(max(int(limit), 1), _MAX_RESULTS)
    q = quote_plus(query.strip())
    last_err: Exception | None = None
    for endpoint in _ENDPOINTS:
        try:
            page = netguard.safe_fetch(endpoint.format(q=q))["text"]
        except netguard.FetchError as exc:  # blocked/unavailable -> try the next endpoint
            last_err = exc
            continue
        results = parse_results(page, limit)
        if results:
            return results
    if last_err is not None:
        raise SearchError(f"search unavailable: {last_err}") from None
    return []  # endpoints reachable but no parseable results
