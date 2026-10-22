"""``ResearchCtx``: the budgets, the provenance set and the trace one research run carries (plan 6.1, 6.4).

Every tool spends a turn first (``spend``), may spend a fetch or a search, and appends one trace entry
(``note``). The trace is what ``recipe.research`` seals, so entries are one printable line, bounded.
"""
from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from urllib.parse import urlsplit

from .. import ni_recipe
from ..pagegraph import registrable_domain

MAX_FETCHES = 8
MAX_SEARCHES = 3
MAX_SECONDS = 120.0
TOKEN_CAP = 60_000
MAX_SAMPLES = 8
_MAX_URLS = 200
_BAD_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp")
# Hosting platforms where one registrable domain is thousands of unrelated sites: a result on one of these
# makes only its own host a "same site", never the whole platform (the eTLD+1 heuristic has no suffix list).
_SHARED_SUFFIXES = ("github.io", "gitlab.io", "blogspot.com", "herokuapp.com", "vercel.app", "netlify.app",
                    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net",
                    "amazonaws.com", "cloudfront.net", "wordpress.com", "wixsite.com", "weebly.com", "ngrok.io",
                    "repl.co", "glitch.me", "onrender.com", "fly.dev", "surge.sh")


def one_line(text: object, n: int) -> str:
    """``text`` as one printable line of at most ``n`` characters (anything unprintable becomes a space)."""
    assert n > 0, "a positive bound"
    assert text is not None, "text required"
    return " ".join("".join(c if c.isprintable() else " " for c in str(text)).split())[:n]


def error(kind: str, detail: str = "") -> dict:
    """A typed tool error: ``{"ok": False, "error": kind, "detail": one line}``."""
    assert kind and isinstance(kind, str), "an error kind"
    assert isinstance(detail, str), "detail must be text"
    return {"ok": False, "error": kind, "detail": one_line(detail, 200)}


def normal_url(url: str) -> str:
    """``url`` without its fragment, host lower-cased, for set membership."""
    assert isinstance(url, str), "url must be text"
    parts = urlsplit(url.strip())
    assert parts is not None, "urlsplit returns a result"
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{host}{port}{parts.path}" + (f"?{parts.query}" if parts.query else "")


def host_refusal(url: str) -> str | None:
    """Why ``url`` can never be fetched by research (scheme, userinfo, loopback, private, local names), or None."""
    assert len(url) < 5000, "a bounded url"
    assert isinstance(url, str), "url must be text"
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018  (raises on a bad port)
    except ValueError:
        return "not a URL"
    if parts.scheme.lower() not in ("http", "https"):
        return "only http(s) URLs"
    if not host or parts.username or parts.password:
        return "no host, or credentials in the URL"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        local = host == "localhost" or host.endswith(_BAD_HOST_SUFFIXES) or "." not in host
        return "loopback and local names are off limits" if local else None
    return None if ip.is_global else "loopback and private addresses are off limits"


def site_of(host: str) -> str:
    """The "same site" key of ``host``: its registrable domain, or the whole host on a shared hosting platform."""
    assert isinstance(host, str), "host must be text"
    h = host.lower().rstrip(".")
    assert "/" not in h, "a host, not a URL"
    if any(h == s or h.endswith("." + s) for s in _SHARED_SUFFIXES):
        return h
    return registrable_domain(h)


@dataclass
class ResearchCtx:
    """One research run: the ask, the library, the clock, the budgets, what may be fetched and what happened."""

    request: str
    question: dict
    library: object | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    zone: tzinfo = UTC
    preview_url: str | None = None
    max_turns: int = ni_recipe.MAX_TURNS
    max_fetches: int = MAX_FETCHES
    max_searches: int = MAX_SEARCHES
    max_seconds: float = MAX_SECONDS
    token_cap: int = TOKEN_CAP
    clock: Callable[[], float] = time.monotonic
    allowed_origins: frozenset = frozenset({"library", "search", "preview", "same_domain"})  # {"library"}: no web
    resolver: Callable[[str, str], dict] | None = None
    searcher: Callable[[str, int], dict] | None = None   # (query, limit) -> {"results": [{title,url,snippet}]}
    turns: int = 0
    fetches: int = 0
    searches: int = 0
    tokens: int = 0
    urls: dict = field(default_factory=dict)      # normal url -> {kind, format, scope, title, blocked}
    domains: set = field(default_factory=set)      # registrable domains of recorded results
    samples: dict = field(default_factory=dict)    # normal url -> {format, parsed}
    entities: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    started: float = 0.0

    def __post_init__(self) -> None:
        assert self.max_turns > 0, "a turn budget"
        assert isinstance(self.request, str) and isinstance(self.question, dict), "the ask and its question"
        self.started = self.clock()

    def elapsed(self) -> float:
        assert self.started >= 0, "a start time"
        assert callable(self.clock), "a clock"
        return self.clock() - self.started

    def spend(self, tool: str, kind: str | None = None) -> dict | None:
        """Spend one turn (and a ``search`` when named); a typed budget error when any budget is out."""
        assert tool in ni_recipe.TOOLS, "a known tool"
        assert kind in (None, "search"), "a known budget"
        for name, used, cap in (("seconds", self.elapsed(), self.max_seconds), ("tokens", self.tokens, self.token_cap),
                                ("turns", self.turns, self.max_turns)):  # bounded: three budgets
            if used >= cap:
                return error("budget_exhausted", f"{name} budget is used up")
        if kind == "search" and self.searches >= self.max_searches:
            return error("budget_exhausted", f"searches budget is used up ({self.max_searches})")
        self.turns += 1
        self.searches += kind == "search"
        return None

    def spend_fetch(self) -> dict | None:
        """Count one network fetch (the turn was spent on entry); a typed error when the 8 are used."""
        assert self.fetches >= 0, "a fetch count"
        assert self.max_fetches >= 0, "a fetch cap"
        if self.fetches >= self.max_fetches:
            return error("budget_exhausted", f"fetches budget is used up ({self.max_fetches})")
        self.fetches += 1
        return None

    def add_tokens(self, n: int) -> None:
        """The loop reports the model tokens it used so the token cap can end the run."""
        assert n < 10**9, "a sane token count"
        assert isinstance(n, int) and n >= 0, "a token count"
        self.tokens += n

    def note(self, tool: str, arg: object, outcome: object) -> None:
        """Append one trace entry (tool, bounded argument, bounded outcome)."""
        assert isinstance(self.trace, list), "a trace"
        assert tool in ni_recipe.TOOLS, "a known tool"
        if len(self.trace) < ni_recipe.MAX_TURNS:
            self.trace.append({"tool": tool, "arg": one_line(arg, 200), "outcome": one_line(outcome, 200)})

    def record(self, url: str, kind: str, **info: object) -> None:
        """Remember a result URL (and its registrable domain) as a fetch origin: ``library`` | ``search`` | ``preview``."""
        assert isinstance(url, str), "url must be text"
        assert kind in ("library", "search", "preview"), "a known origin"
        if host_refusal(url) is not None or len(self.urls) >= _MAX_URLS:
            return
        key = normal_url(url)
        self.urls.setdefault(key, {"kind": kind, **info})
        self.domains.add(site_of(urlsplit(key).hostname or ""))

    def record_search_results(self, rows: list) -> None:
        """Web search results the loop got (S5b): each ``{url, title}`` becomes a legal fetch origin."""
        assert len(rows) < 1000, "a bounded result list"
        assert isinstance(rows, list), "rows must be a list"
        for r in rows[:10]:  # bounded: a result page
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                self.record(r["url"], "search", title=one_line(r.get("title") or "", 120))

    def origin(self, url: str) -> tuple[str | None, str]:
        """``(origin, reason)``: how ``url`` may be fetched (``library`` | ``search`` | ``preview`` | ``same_domain``)
        or ``(None, why not)``. Only recorded results, the previewed endpoint and their registrable domains."""
        assert isinstance(self.domains, set), "a domain set"
        assert isinstance(url, str), "url must be text"
        bad = host_refusal(url)
        if bad:
            return None, bad
        key = normal_url(url)
        found = self._origin_of(key)
        if found is None:
            return None, "not a Library row, a search result, the previewed endpoint or a site already found"
        if found not in self.allowed_origins:
            return None, "this run reads SmartBrain Library sources only"
        if found == "same_domain":  # a composed address: no query to carry data out, no fragment
            parts = urlsplit(url.strip())
            if parts.query or parts.fragment or "?" in url or "#" in url:
                return None, "another address on a found site must be plain: no query string and no fragment"
        return found, ""

    def _origin_of(self, key: str) -> str | None:
        """How the normalised ``key`` was found, ignoring which origins this run allows."""
        assert isinstance(key, str), "key must be text"
        assert isinstance(self.urls, dict), "a url table"
        if key in self.urls:
            return self.urls[key]["kind"]
        if self.preview_url and key == normal_url(self.preview_url):
            return "preview"
        if site_of(urlsplit(key).hostname or "") in self.domains:
            return "same_domain"
        return None

    def budget_used(self) -> dict:
        """The budget block ``recipe.research`` seals."""
        assert self.turns >= 0, "a turn count"
        assert self.searches >= 0, "a search count"
        return {"turns": self.turns, "fetches": self.fetches, "searches": self.searches, "seconds": int(self.elapsed())}

    def sealed_trace(self) -> dict:
        """``recipe.research``: the trace squeezed under the ni_recipe byte cap (shorter lines, then fewer)."""
        assert isinstance(self.trace, list), "a trace"
        assert ni_recipe.MAX_RESEARCH_BYTES > 0, "a trace cap"
        turns = [dict(t) for t in self.trace[: ni_recipe.MAX_TURNS]]
        limit = 200
        for _ in range(12):  # bounded: halve the lines, then drop the oldest
            body = {"turns": turns, "budget": self.budget_used()}
            if len(json.dumps(body, sort_keys=True)) <= ni_recipe.MAX_RESEARCH_BYTES:
                return body
            limit = max(limit // 2, 20)
            turns = [{**t, "arg": t["arg"][:limit], "outcome": t["outcome"][:limit]} for t in turns]
            turns = turns[1:] if limit == 20 and len(turns) > 1 else turns
        return {"turns": turns[-1:], "budget": self.budget_used()}
