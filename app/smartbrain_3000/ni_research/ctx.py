"""``ResearchCtx``: the budgets, the provenance set and the trace one research run carries (plan 6.1, 6.4).

Every tool spends a turn first (``spend``), may spend a fetch or a search, and appends one trace entry
(``note``). The trace is what ``recipe.research`` seals, so entries are one printable line, bounded.
"""
from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from urllib.parse import urlsplit

from .. import ni_polite, ni_recipe

MAX_FETCHES = 8
MAX_SEARCHES = 3
MAX_SECONDS = 120.0
TOKEN_CAP = 60_000
MAX_SAMPLES = 8
_MAX_URLS = 200
_MAX_QUERIES = 4
_BAD_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp")
ORIGINS = ("library", "search", "preview")
PROPOSE_TOOLS = frozenset({"propose_card", "propose_compare"})   # the tools that end a run with a proposal
MAX_REPLAYS = 3         # repeats of an identical call answered from memory without a turn
MAX_MEMO = 32           # remembered call results
MAX_FAILED_FETCHES = 16


def one_line(text: object, n: int) -> str:
    """``text`` as one printable line of at most ``n`` characters (anything unprintable becomes a space)."""
    assert n > 0, "a positive bound"
    assert text is not None, "text required"
    return " ".join("".join(c if c.isprintable() else " " for c in str(text)).split())[:n]


def error(kind: str, detail: str = "", limit: int = 200) -> dict:
    """A typed tool error: ``{"ok": False, "error": kind, "detail": one line}``. ``limit`` widens the detail for the
    errors that tell the model exactly what to change (a field and its allowed values)."""
    assert kind and isinstance(kind, str), "an error kind"
    assert isinstance(detail, str) and 0 < limit <= 600, "detail must be text, bounded"
    return {"ok": False, "error": kind, "detail": one_line(detail, limit)}


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


@dataclass
class ResearchCtx:
    """One research run: the ask, the library, the clock, the budgets, what may be fetched and what happened."""

    request: str
    question: dict
    library: object | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    zone: tzinfo = UTC
    preview_url: str | None = None
    window: str | None = None   # the ask's forward window token (``now``, ``today``...), for the page-reading gate
    max_turns: int = ni_recipe.MAX_TURNS
    max_fetches: int = MAX_FETCHES
    max_searches: int = MAX_SEARCHES
    max_seconds: float = MAX_SECONDS
    token_cap: int = TOKEN_CAP
    clock: Callable[[], float] = ni_polite.work_clock   # stops while a build waits for a host's budget
    allowed_origins: frozenset = frozenset(ORIGINS)  # {"library"}: no web
    resolver: Callable[[str, str], dict] | None = None
    searcher: Callable[[str, int], dict] | None = None   # (query, limit) -> {"results": [{title,url,snippet}]}
    turns: int = 0
    fetches: int = 0
    searches: int = 0
    tokens: int = 0
    urls: dict = field(default_factory=dict)      # normal url -> {kind, format, scope, title, blocked}
    samples: dict = field(default_factory=dict)    # normal url -> {format, parsed}
    entities: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    started: float = 0.0
    passed: dict | None = None                      # the last draft try_recipe passed: {"draft", "tried"}; propose it before the budget goes
    failed_fetches: dict = field(default_factory=dict)   # normal url -> the error a fetch of it gave (never fetched twice)
    memo: dict = field(default_factory=dict)        # (tool, args) -> the result of an identical earlier call
    replays: int = 0
    own_proposal: bool = False                      # True only while the loop itself proposes the held pass (no model call: no turn)

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
        if tool in PROPOSE_TOOLS and self.own_proposal:   # the loop's own proposal of a held pass runs no model call: it costs no turn
            return None
        ready = self.passed is not None and not self.passed.get("tried")   # a draft that passed is waiting to be proposed
        if ready and tool in PROPOSE_TOOLS:   # proposing runs no model, fetch or search: it may use the last of the time and tokens
            if self.turns >= self.max_turns:
                return error("budget_exhausted", "turns budget is used up")
            self.turns += 1
            return None
        if ready and self.max_turns - self.turns <= 1:   # the last tool call is kept for the proposal
            return error("budget_exhausted", "the last tool call is kept for propose_card with the draft that passed")
        for name, used, cap in (("seconds", self.elapsed(), self.max_seconds), ("tokens", self.tokens, self.token_cap),
                                ("turns", self.turns, self.max_turns)):  # bounded: three budgets
            if used >= cap:
                return error("budget_exhausted", f"{name} budget is used up")
        if kind == "search" and self.searches >= self.max_searches:
            return error("budget_exhausted", f"searches budget is used up ({self.max_searches})")
        self.turns += 1
        self.searches += kind == "search"
        return None

    def mark_tried(self, draft: object) -> None:
        """A proposal of the held pass failed: it is not waiting any more (proposing it again could only fail again)."""
        assert isinstance(self.trace, list), "a trace"
        if self.passed is not None and draft == self.passed["draft"]:
            self.passed["tried"] = True

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
        """Remember a result URL, exactly, as a fetch origin: ``library`` | ``search`` | ``preview``. A search
        result counts only over https: an address from the open web is never read in the clear. ``query`` (S7):
        the search that handed it over, kept (a few, bounded) so a compare can tell which thing an address is for."""
        assert isinstance(url, str), "url must be text"
        assert kind in ORIGINS, "a known origin"
        if host_refusal(url) is not None or len(self.urls) >= _MAX_URLS:
            return
        if kind == "search" and urlsplit(url.strip()).scheme.lower() != "https":
            return
        query = info.pop("query", None)
        entry = self.urls.setdefault(normal_url(url), {"kind": kind, **info})
        if isinstance(query, str) and query.strip():
            seen = entry.setdefault("queries", [])
            if len(seen) < _MAX_QUERIES and one_line(query, 200) not in seen:
                seen.append(one_line(query, 200))

    def record_search_results(self, rows: list, query: str = "") -> None:
        """Web search results the loop got (S5b): each ``{url, title}`` becomes a legal fetch origin."""
        assert len(rows) < 1000, "a bounded result list"
        assert isinstance(rows, list), "rows must be a list"
        for r in rows[:10]:  # bounded: a result page
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                self.record(r["url"], "search", title=one_line(r.get("title") or "", 120), query=query)

    def origin(self, url: str) -> tuple[str | None, str]:
        """``(origin, reason)``: how ``url`` may be fetched (``library`` | ``search`` | ``preview``) or ``(None, why
        not)``. Only an address a trusted channel handed over, exactly: the model never composes one, so no path,
        query or host of its own choosing can carry data out."""
        assert isinstance(self.urls, dict), "a url table"
        assert isinstance(url, str), "url must be text"
        bad = host_refusal(url)
        if bad:
            return None, bad
        key = normal_url(url)
        found = self._origin_of(key)
        if found is None:
            return None, "not exactly a Library row, a search result or the previewed endpoint"
        if found not in self.allowed_origins:
            return None, "this run reads SmartBrain Library sources only"
        return found, ""

    def _origin_of(self, key: str) -> str | None:
        """How the normalised ``key`` was found, ignoring which origins this run allows."""
        assert isinstance(key, str), "key must be text"
        assert isinstance(self.urls, dict), "a url table"
        if key in self.urls:
            return self.urls[key]["kind"]
        if self.preview_url and key == normal_url(self.preview_url):
            return "preview"
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
