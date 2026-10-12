"""Tier router for browser components (ni-format §35): the static tier first, a render only
when code finds a need for one and the caller allows it.

Tiers: ``static`` (the existing page door — ``pagegraph.fetch_page_graph`` over
``netguard.safe_fetch_page`` + the extractor jail) → ``obscura`` (``runner.render``) →
``obscura-stealth`` (listed, manifest shipped, NOT selectable in this step: asking for it
returns ``tier_disabled``).

Need signals, computed by code from the static result:

- ``js_shell`` — the extracted text is under 400 characters, no structured layer carries
  data (``pagegraph.data_bearing``: an entity, a table with rows, a JSON document of 10+
  leaves — B3a), and the page carries ≥ 3 ``<script>`` tags or a ``<noscript>``
  enable-JavaScript notice (from the raw HTML, or the jail's ``signals`` layer when the
  caller read the page itself);
- ``blocked`` — the static fetch got 403/429, or the page reads as a bot challenge;
- ``want_miss`` — supplied by the caller (its program found nothing it wanted).

Escalation happens only with ``allow_render`` AND a signal, and never on a refusal R8
forbids retrying: a 429 or a challenge (a challenge page, or a refusal netguard names
``challenge`` — B3a) is recorded as ``blocked`` and left alone; only a plain 403 (often a
non-browser filter) may be rendered. A caller that already read the page passes its own
outcome (``static_evidence`` → ``browse(static=…)``) and the router still judges it. Every
result carries ``{engine, version, identity, timings, census}`` (and the render's ``assets``
when asked). ``render_sealed`` is the tick's tier: a sealed card's sites, no static read,
no escalation. A small in-memory circuit breaker per engine opens after 3 consecutive
``engine_error``/``timeout`` results and stays open for 10 minutes (``engine_unhealthy``);
``busy`` and every policy outcome leave it alone.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from .. import __version__, jailrun, netguard, pagegraph
from . import install, runner
from . import manifest as mf

TIERS = ("static", "obscura", "obscura-stealth")
SIGNALS = ("js_shell", "blocked", "want_miss")
_JS_SHELL_TEXT = 400
_MIN_SCRIPTS = 3
_MAX_NOSCRIPT = 32
_NOSCRIPT_WINDOW = 2000
_BREAKER_TRIP = 3
_BREAKER_OPEN_S = 600.0
_BOTH_JOIN_S = 300.0  # a render is bounded by its own watchdog; this only guards the join
_SCRIPT_RE = re.compile(rb"<script\b", re.IGNORECASE)
_NOSCRIPT_RE = re.compile(rb"<noscript\b", re.IGNORECASE)
_JS_NOTICE_RE = re.compile(rb"(?:enable|turn on|requires?|disabled|not available)[^<]{0,80}?javascript"
                           rb"|javascript[^<]{0,80}?(?:enable|turn on|required|disabled|not available)",
                           re.IGNORECASE)
_monotonic = time.monotonic           # module attribute so a test can open/close the breaker
_static_fetch = netguard.safe_fetch_page  # the page door's guarded fetch (tests: a local server)


@dataclass(frozen=True)
class BrowseResult:
    """One tier's answer. ``tier`` names whose content this is; ``escalation`` records a
    render attempt (or refusal) that did not produce the content."""

    ok: bool
    tier: str
    status: str
    reason: str
    signals: tuple[str, ...]
    html: bytes | None
    graph: dict | None
    final_url: str | None
    engine: str
    version: str
    identity: dict
    timings: dict
    census: list[dict]
    escalation: dict | None = field(default=None)
    assets: list[str] | None = field(default=None)  # the render's ``--dump assets`` (B3a, R12)


class _Breaker:
    """Per-engine consecutive-failure breaker (in memory, process-wide)."""

    def __init__(self) -> None:
        assert _BREAKER_TRIP > 0 and _BREAKER_OPEN_S > 0, "breaker settings are positive"
        self._lock = threading.Lock()
        self._fails: dict[str, int] = {}
        self._opened: dict[str, float] = {}
        assert not self._fails and not self._opened, "a fresh breaker is closed"

    def allow(self, engine: str) -> bool:
        assert engine, "engine required"
        assert _BREAKER_OPEN_S > 0, "open period is positive"
        with self._lock:
            opened = self._opened.get(engine)
            if opened is None:
                return True
            if _monotonic() - opened >= _BREAKER_OPEN_S:
                del self._opened[engine]  # half-open: one attempt decides
                return True
            return False

    def record(self, engine: str, status: str) -> None:
        assert engine, "engine required"
        assert status, "status required"
        with self._lock:
            if status == "ok":
                self._fails[engine] = 0
            elif status in ("engine_error", "timeout"):
                self._fails[engine] = self._fails.get(engine, 0) + 1
                if self._fails[engine] >= _BREAKER_TRIP:
                    self._opened[engine] = _monotonic()

    def health(self, engine: str) -> str:
        assert engine, "engine required"
        result = "ok" if self.allow(engine) else "unhealthy"
        assert result in ("ok", "unhealthy"), "health is closed"
        return result

    def reset(self) -> None:
        assert self._lock is not None, "lock required"
        with self._lock:
            self._fails.clear()
            self._opened.clear()
        assert not self._opened, "every engine closed"


BREAKER = _Breaker()


def engine_health(name: str) -> str:
    """``ok`` | ``unhealthy`` (breaker open)."""
    assert isinstance(name, str) and name, "engine name required"
    health = BREAKER.health(name)
    assert health in ("ok", "unhealthy"), "health is closed"
    return health


def status() -> list[dict]:
    """The Status rows: ``install.status()`` plus each engine's breaker health."""
    rows = install.status()
    assert isinstance(rows, list), "rows must be a list"
    out = [{**row, "health": engine_health(row["name"])} for row in rows]
    assert all("health" in row for row in out), "every row carries its health"
    return out


def _static_identity() -> dict:
    ua = netguard.USER_AGENT
    assert ua and ua.startswith("SmartBrain/"), "the static tier is honest"
    assert ua.isascii(), "the UA is ASCII"
    return {"mode": "honest", "profile": None, "user_agent": ua, "consistent": True}


def _static(url: str) -> tuple[BrowseResult, tuple[str, ...]]:
    """Tier 1 through the page door; returns the result and the need signals it shows."""
    assert isinstance(url, str) and url, "url required"
    started = _monotonic()
    captured: dict = {}

    def fetcher(target: str, **kwargs: object) -> dict:
        assert target == url, "the page door fetches exactly the requested URL"
        assert set(kwargs) <= {"deadline_seconds"}, "headerless by design"
        got = _static_fetch(target, **kwargs)
        captured.update(got if isinstance(got, dict) else {})
        return got

    graph, failure = None, None
    try:
        graph = pagegraph.fetch_page_graph(url, fetcher=fetcher)
    except netguard.FetchError as exc:
        failure = exc
    except jailrun.JailError as exc:
        failure = exc
    html = captured.get("content") if isinstance(captured.get("content"), bytes) else None
    result = static_evidence(url, graph=graph, html=html, failure=failure,
                             final_url=captured.get("final_url"),
                             static_ms=int((_monotonic() - started) * 1000))
    assert result.tier == "static", "tier 1 answers as static"
    return result, result.signals


def static_evidence(url: str, *, graph: dict | None = None, html: bytes | None = None,
                    failure: Exception | None = None, final_url: str | None = None,
                    static_ms: int = 0) -> BrowseResult:
    """A static read's outcome as the tier-1 ``BrowseResult``: the router's own read uses
    it, and so does a caller that read the page itself (the NI page door) — the facts go in,
    the signals come out of code here. ``failure`` is the read's ``FetchError`` /
    ``JailError``: 403/429 are ``blocked`` (reason ``http_<status>``), a refusal netguard
    names ``challenge`` is ``blocked`` with reason ``challenge`` and never renders."""
    assert isinstance(url, str) and url, "url required"
    assert graph is None or failure is None, "a read either produced a graph or failed"
    status, reason, signals = "ok", "", ()
    if isinstance(failure, netguard.FetchError):
        status, reason = "fetch_failed", f"http_{failure.status}" if failure.status else (failure.kind or "fetch")
        if failure.kind == "challenge":
            status, reason, signals = "blocked", "challenge", ("blocked",)
        elif failure.status in (403, 429):
            status, signals = "blocked", ("blocked",)
    elif isinstance(failure, jailrun.JailError):
        status, reason = "extract_failed", failure.reason
    elif graph is None:
        status, reason = "fetch_failed", "fetch"
    if graph is not None:
        signals = need_signals(graph, html)
    return BrowseResult(
        ok=status == "ok", tier="static", status=status, reason=reason, signals=signals,
        html=html if status == "ok" else None, graph=graph, final_url=final_url,
        engine="static", version=__version__, identity=_static_identity(),
        timings={"static_ms": static_ms, "total_ms": static_ms}, census=[])


def need_signals(graph: dict, html: bytes | None) -> tuple[str, ...]:
    """``js_shell`` / ``blocked`` from a static page graph and its raw HTML — or, when the
    caller has no raw HTML, the jail's ``signals`` layer (pure code)."""
    assert isinstance(graph, dict), "graph required"
    out: list[str] = []
    article, extra = pagegraph.split_text(str(graph.get("text") or ""))
    visible = len(article.strip()) + len(extra.strip())
    scripts, notice = _script_evidence(graph, html)
    if visible < _JS_SHELL_TEXT and not pagegraph.data_bearing(graph) and (
            scripts >= _MIN_SCRIPTS or notice):
        out.append("js_shell")
    if (graph.get("readability") or {}).get("kind") == "challenge":
        out.append("blocked")
    assert set(out) <= set(SIGNALS), "signals are closed"
    return tuple(out)


def _script_evidence(graph: dict, html: bytes | None) -> tuple[int, bool]:
    """(``<script>`` count, a noscript enable-JavaScript notice): from the raw HTML when the
    router read it, else from the extractor jail's closed ``signals`` layer (B3a)."""
    assert isinstance(graph, dict), "graph required"
    if html is not None:
        return len(_SCRIPT_RE.findall(html)), _noscript_notice(html)
    sig = graph.get("signals") if isinstance(graph.get("signals"), dict) else {}
    count = sig.get("script_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
    assert count >= 0, "a count"
    return count, sig.get("noscript_js") is True


def _noscript_notice(html: bytes) -> bool:
    """A ``<noscript>`` block (one of the first 32) telling the reader to enable JS."""
    assert isinstance(html, bytes), "html must be bytes"
    assert _MAX_NOSCRIPT > 0 and _NOSCRIPT_WINDOW > 0, "scan bounds are positive"
    for count, match in enumerate(_NOSCRIPT_RE.finditer(html)):  # bounded below
        if count >= _MAX_NOSCRIPT:
            break
        window = html[match.end(): match.end() + _NOSCRIPT_WINDOW]
        if _JS_NOTICE_RE.search(window.split(b"</noscript", 1)[0]):
            return True
    return False


def _escalates(signals: tuple[str, ...], static: BrowseResult | None) -> bool:
    """Which signals may render: a shell, a caller's miss, or a PLAIN 403 — never a 429 or
    a challenge (R8: never retry a refusal, never solve a challenge)."""
    assert isinstance(signals, tuple), "signals must be a tuple"
    assert static is None or static.tier == "static", "only a static result is judged here"
    if "blocked" in signals:  # a refusal outranks every other signal
        return static is not None and static.reason == "http_403" and static.graph is None
    return "js_shell" in signals or "want_miss" in signals


def would_render(*, static: BrowseResult | None = None, signal: str | None = None) -> bool:
    """B3a: would ``browse(..., allow_render=True)`` escalate this static read (or a caller's
    signal) to a render? The same rule ``browse`` applies — the flow marks a render WANTED
    with it before it knows whether this machine can render."""
    assert static is None or static.tier == "static", "only a static result is judged here"
    assert signal is None or signal in SIGNALS, "closed signals"
    signals = static.signals if static is not None else ((signal,) if signal else ())
    return _escalates(signals, static)


def browse(url: str, *, need_signal_from_static: str | None = None, allow_render: bool = False,
           site_policy: dict, identity: dict, engine: str = "obscura",
           timezone: str = "UTC", static: BrowseResult | None = None,
           want_assets: bool = False) -> BrowseResult:
    """Static tier, then (only with ``allow_render`` and an escalating signal) a render.
    ``need_signal_from_static`` lets a caller that already read the page statically pass
    its signal (``want_miss`` included) and skip tier 1; ``static`` lets it pass its whole
    read (``static_evidence``) so a plain 403 is judged here without a second fetch.
    ``want_assets`` (creation only) adds the render's ``--dump assets`` run."""
    assert need_signal_from_static is None or need_signal_from_static in SIGNALS, "closed signals"
    assert isinstance(url, str) and url, "url required"
    assert static is None or (static.tier == "static" and need_signal_from_static is None), \
        "a static read or a signal, never both"
    if engine not in TIERS[1:]:
        raise ValueError("unknown render tier")
    if static is not None:
        signals = static.signals
    elif need_signal_from_static is None:
        static, signals = _static(url)
    else:
        signals = (need_signal_from_static,)
    if not allow_render or not _escalates(signals, static):
        return static if static is not None else _no_content(url, signals, None)
    attempt = _render_tier(url, engine, site_policy, identity, timezone, signals,
                           want_assets=bool(want_assets))
    if attempt.ok:
        return attempt
    escalation = {"tier": engine, "status": attempt.status, "reason": attempt.reason}
    base = static if static is not None else _no_content(url, signals, None)
    return BrowseResult(**{**base.__dict__, "escalation": escalation})


def render_sealed(url: str, *, sites: list[str], identity: dict, timezone: str = "UTC",
                  engine: str = "obscura") -> BrowseResult:
    """The tick's tier (B3a): render a sealed card's URL under its sealed sites — no static
    read, no escalation, no assets. Busy, unavailable and every refusal are statuses."""
    assert isinstance(url, str) and url, "url required"
    assert isinstance(sites, list) and sites, "a sealed card names its sites"
    if engine not in TIERS[1:]:
        raise ValueError("unknown render tier")
    got = _render_tier(url, engine, {"mode": "sealed", "sites": list(sites)}, identity,
                       timezone, (), want_assets=False)
    assert got.tier == engine, "the render answers as its tier"
    return got


def _no_content(url: str, signals: tuple[str, ...], escalation: dict | None) -> BrowseResult:
    """The caller skipped tier 1 and no render produced content."""
    assert isinstance(url, str), "url required"
    assert isinstance(signals, tuple), "signals required"
    return BrowseResult(ok=False, tier="none", status="no_content", reason="not_rendered",
                        signals=signals, html=None, graph=None, final_url=None, engine="none",
                        version="", identity=_static_identity(),
                        timings={"total_ms": 0}, census=[], escalation=escalation)


def _render_tier(url: str, engine: str, site_policy: dict, identity: dict, timezone: str,
                 signals: tuple[str, ...], want_assets: bool = False) -> BrowseResult:
    """Tier 2/3 through the runner, guarded by the tier switch and the breaker."""
    assert engine in TIERS[1:], "render tiers only"
    assert isinstance(signals, tuple), "signals required"
    if engine not in mf.SELECTABLE:
        return _render_refusal(engine, "tier_disabled", "tier_disabled", signals)
    if not BREAKER.allow(engine):
        return _render_refusal(engine, "engine_unhealthy", "breaker_open", signals)
    extra = {"want_assets": True} if want_assets else {}
    got = runner.render(url, engine=engine, site_policy=site_policy, identity=identity,
                        timezone=timezone, **extra)
    BREAKER.record(engine, got.status)
    return BrowseResult(ok=got.ok, tier=engine, status=got.status, reason=got.reason,
                        signals=signals, html=got.html, graph=None, final_url=got.final_url,
                        engine=got.engine, version=got.version, identity=dict(got.identity),
                        timings=dict(got.timings), census=list(got.census),
                        assets=list(got.assets) if got.assets is not None else None)


def _render_refusal(engine: str, status: str, reason: str, signals: tuple[str, ...]) -> BrowseResult:
    assert status in ("tier_disabled", "engine_unhealthy"), "router refusals are closed"
    assert reason, "reason required"
    return BrowseResult(ok=False, tier=engine, status=status, reason=reason, signals=signals,
                        html=None, graph=None, final_url=None, engine=engine, version="",
                        identity={"mode": "", "profile": None, "user_agent": None,
                                  "consistent": False},
                        timings={"egress_ms": 0, "render_ms": 0, "total_ms": 0}, census=[])


def browse_both(url: str, *, allow_render: bool = False, site_policy: dict, identity: dict,
                engine: str = "obscura",
                timezone: str = "UTC") -> tuple[BrowseResult, BrowseResult | None]:
    """Tier 1 and tier 2 at the same time (discovery); both results go back for the caller
    to compare. Without ``allow_render`` only the static tier runs."""
    assert isinstance(url, str) and url, "url required"
    if engine not in TIERS[1:]:
        raise ValueError("unknown render tier")
    if not allow_render:
        return _static(url)[0], None
    box: dict[str, BrowseResult] = {}

    def run_render() -> None:
        assert not box, "one render per call"
        box["render"] = _render_tier(url, engine, site_policy, identity, timezone, ())
        assert box["render"].tier == engine, "the render answers as its tier"

    worker = threading.Thread(target=run_render, name="browse-render", daemon=True)
    worker.start()
    static = _static(url)[0]
    worker.join(_BOTH_JOIN_S)
    rendered = box.get("render")
    if rendered is None:  # never expected: the runner's watchdog ends every render
        rendered = BrowseResult(ok=False, tier=engine, status="timeout", reason="join",
                                signals=(), html=None, graph=None, final_url=None,
                                engine=engine, version="",
                                identity={"mode": "", "profile": None, "user_agent": None,
                                          "consistent": False},
                                timings={"total_ms": int(_BOTH_JOIN_S * 1000)}, census=[])
    assert static.tier == "static", "the first result is the static tier"
    return static, rendered
