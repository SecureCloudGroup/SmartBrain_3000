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

from collections.abc import Callable
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


# ---------------------------------------------------------------------------
# Provider registry (A2): SearXNG (self-hosted), Brave and Tavily (BYO key),
# with the keyless DuckDuckGo scrape above as the always-available fallback.
# Configuration lives in the `meta` KV (engine choice, SearXNG URL); API keys
# live in the encrypted secret store under `websearch:<name>:api_key` and are
# injected HERE by the route layer — a SearchService encapsulates them exactly
# like ctx.email encapsulates the Gmail token, so no tool handler ever sees a
# raw key (the ToolContext credential-firewall posture).
# ---------------------------------------------------------------------------

ENGINES = ("auto", "searxng", "brave", "tavily", "ddg")  # "auto" = first configured, ddg last
META_ENGINE = "websearch:engine"
META_SEARXNG_URL = "websearch:searxng_url"
SECRET_BRAVE = "websearch:brave:api_key"
SECRET_TAVILY = "websearch:tavily:api_key"
_PROVIDER_TIMEOUT_NOTE = "each provider call rides netguard's fetch timeout"


def _parse_searxng(data: dict, limit: int) -> list[dict]:
    out = []
    for r in (data.get("results") or [])[:limit]:
        if r.get("url") and r.get("title"):
            out.append({"title": r["title"], "url": r["url"], "snippet": r.get("content") or ""})
    return out


def _parse_brave(data: dict, limit: int) -> list[dict]:
    out = []
    for r in ((data.get("web") or {}).get("results") or [])[:limit]:
        if r.get("url") and r.get("title"):
            out.append({"title": r["title"], "url": r["url"], "snippet": r.get("description") or ""})
    return out


def _parse_tavily(data: dict, limit: int) -> list[dict]:
    out = []
    for r in (data.get("results") or [])[:limit]:
        if r.get("url") and r.get("title"):
            out.append({"title": r["title"], "url": r["url"], "snippet": r.get("content") or ""})
    return out


class SearchService:
    """Web search across the configured providers, falling back left-to-right.

    ``engine`` pins one provider ("auto" tries every configured one in registry
    order); DuckDuckGo always anchors the chain so search never depends on a key.
    Every provider call goes through netguard (the one egress path) — Brave rides
    a guarded GET with its token header, Tavily a guarded JSON POST.
    """

    def __init__(self, engine: str = "auto", searxng_url: str = "",
                 brave_key: str = "", tavily_key: str = "") -> None:
        assert engine in ENGINES, f"unknown engine {engine!r}"
        self.engine = engine
        self.searxng_url = (searxng_url or "").rstrip("/")
        self._brave_key = brave_key or ""
        self._tavily_key = tavily_key or ""

    def configured(self) -> list[str]:
        """Provider names available to the chain, in fallback order (ddg always last)."""
        out = []
        if self.searxng_url:
            out.append("searxng")
        if self._brave_key:
            out.append("brave")
        if self._tavily_key:
            out.append("tavily")
        out.append("ddg")
        return out

    def _chain(self) -> list[str]:
        chain = self.configured()
        if self.engine != "auto" and self.engine in chain:
            chain.remove(self.engine)
            chain.insert(0, self.engine)
        return chain

    def _searxng(self, query: str, limit: int) -> list[dict]:
        url = f"{self.searxng_url}/search?q={quote_plus(query)}&format=json"
        return _parse_searxng(netguard.safe_fetch_json(url), limit)

    def _brave(self, query: str, limit: int) -> list[dict]:
        url = f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count={limit}"
        data = netguard.safe_fetch_json(url, headers={"X-Subscription-Token": self._brave_key,
                                                      "Accept": "application/json"})
        return _parse_brave(data, limit)

    def _tavily(self, query: str, limit: int) -> list[dict]:
        data = netguard.safe_post_json("https://api.tavily.com/search",
                                       {"api_key": self._tavily_key, "query": query,
                                        "max_results": limit})
        return _parse_tavily(data, limit)

    def search(self, query: str, limit: int = 5) -> dict:
        """Return {"results": [...], "engine": <which provider answered>}.

        A provider that errors or returns nothing yields to the next; the LAST
        error is surfaced only if the whole chain came up empty-handed.
        """
        assert query and query.strip(), "query required"
        limit = min(max(int(limit), 1), _MAX_RESULTS)
        last_err: Exception | None = None
        for name in self._chain():  # bounded by the registry size
            try:
                if name == "searxng":
                    results = self._searxng(query, limit)
                elif name == "brave":
                    results = self._brave(query, limit)
                elif name == "tavily":
                    results = self._tavily(query, limit)
                else:
                    results = web_search(query, limit)
            except Exception as exc:  # one provider down must never kill the chain
                last_err = exc
                continue
            if results:
                return {"results": results, "engine": name}
        if last_err is not None:
            raise SearchError(f"search unavailable: {last_err}") from None
        return {"results": [], "engine": self._chain()[-1]}


def service_from(conn, secret_get: Callable[[str], str | None]) -> SearchService:
    """Build the configured SearchService for a turn (route layer only).

    ``secret_get`` is the ONLY touchpoint with the secret store — called here,
    at construction, so the keys live inside the service and never transit a
    tool handler or the ToolContext dataclass repr.
    """
    from . import db  # local import: search must stay importable without duckdb loaded

    engine = (db.meta_get(conn, META_ENGINE) or "auto") if conn is not None else "auto"
    if engine not in ENGINES:
        engine = "auto"
    searxng = (db.meta_get(conn, META_SEARXNG_URL) or "") if conn is not None else ""
    return SearchService(
        engine=engine,
        searxng_url=searxng,
        brave_key=secret_get(SECRET_BRAVE) or "",
        tavily_key=secret_get(SECRET_TAVILY) or "",
    )
