"""The tier router (§35): the static tier through the real page door (``pagegraph`` +
the extractor jail) fed by a local HTTP server, need signals computed by code, escalation
to the fake engine only with ``allow_render`` and a signal R8 permits, the stealth tier
refused, the circuit breaker, and ``browse_both`` running the two tiers at once."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from smartbrain_3000 import netguard
from smartbrain_3000.browsers import egress, router, runner
from tests import _browsers as hb

ARTICLE = ("<html><head><title>Tides</title></head><body><article><h1>Charleston tides</h1>"
           + "<p>High tide is at 4:12 pm and the next low tide is at 10:31 pm tonight. "
             "The water will reach five point eight feet above the mean lower low mark.</p>" * 8
           + "</article></body></html>").encode()
SHELL = (b"<html><head><title>App</title><script src='/a.js'></script><script src='/b.js'>"
         b"</script><script>window.__boot=1</script></head><body><div id='root'></div></body></html>")
NOSCRIPT = (b"<html><head><title>App</title></head><body><noscript>You need to enable JavaScript "
            b"to run this app.</noscript><div id='root'></div></body></html>")
CHALLENGE = (b"<html><head><title>Just a moment...</title><script>1</script><script>2</script>"
             b"<script>3</script></head><body>Checking your browser before accessing the site."
             b"</body></html>")
RENDERED = b"<html><body><p>rendered by the engine</p></body></html>"
NAMES = {"fixture.test": [hb.PUBLIC_IP]}
POLICY = {"mode": "own"}
IDENTITY = {"mode": "honest"}


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    hb.install_fake(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "_RUN_PARENT", str(runs))
    target = hb.LocalServer({p: (200, {}, RENDERED) for p in
                             ("/article", "/shell", "/noscript", "/challenge", "/forbidden", "/limited")})
    monkeypatch.setattr(egress, "start", hb.egress_starter(NAMES, target.port, []))
    site = hb.LocalServer({"/article": (200, {"Content-Type": "text/html"}, ARTICLE),
                           "/shell": (200, {"Content-Type": "text/html"}, SHELL),
                           "/noscript": (200, {"Content-Type": "text/html"}, NOSCRIPT),
                           "/challenge": (200, {"Content-Type": "text/html"}, CHALLENGE),
                           "/forbidden": (403, {}, b"no"), "/limited": (429, {}, b"slow")})
    static_calls: list[str] = []
    render_calls: list[str] = []
    real_render = runner.render

    def static_fetch(url: str, **kwargs: object) -> dict:
        """The page door's contract (netguard.safe_fetch_page) over a real local socket."""
        static_calls.append(url)
        resp = httpx.get(f"http://127.0.0.1:{site.port}{urlsplit(url).path}", timeout=10)
        if resp.status_code >= 400:
            raise netguard.FetchError(f"upstream returned HTTP {resp.status_code}",
                                      status=resp.status_code)
        return {"final_url": url, "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""), "content": resp.content}

    def spy_render(url: str, **kwargs: object) -> runner.RenderResult:
        render_calls.append(url)
        return real_render(url, **kwargs)

    monkeypatch.setattr(router, "_static_fetch", static_fetch)
    monkeypatch.setattr(runner, "render", spy_render)
    yield SimpleNamespace(static_calls=static_calls, render_calls=render_calls,
                          real_render=real_render)
    site.close()
    target.close()


def _browse(path: str, **kw: object) -> router.BrowseResult:
    return router.browse(f"https://fixture.test{path}", site_policy=POLICY, identity=IDENTITY, **kw)


def _canned(status: str):
    def render(url: str, **kwargs: object) -> runner.RenderResult:
        return runner.RenderResult(ok=status == "ok", status=status,
                                   reason="" if status == "ok" else "canned", html=b"x" if status == "ok" else None,
                                   final_url=None, assets=None, engine="obscura", version=hb.FAKE_VERSION,
                                   identity={"mode": "honest", "profile": None, "user_agent": "ua",
                                             "consistent": False},
                                   timings={"egress_ms": 0, "render_ms": 0, "total_ms": 0}, census=[])
    return render


def test_a_readable_page_needs_no_render(rig) -> None:
    got = _browse("/article", allow_render=True)
    assert (got.tier, got.ok, got.signals, got.escalation) == ("static", True, (), None)
    assert "High tide is at 4:12 pm" in got.graph["text"] and got.html == ARTICLE
    assert (got.engine, got.identity["user_agent"]) == ("static", netguard.USER_AGENT)
    assert {"static_ms", "total_ms"} <= set(got.timings)
    assert rig.render_calls == []


def test_a_js_shell_renders_only_when_allowed(rig) -> None:
    got = _browse("/shell")
    assert (got.tier, got.signals) == ("static", ("js_shell",)) and rig.render_calls == []
    got = _browse("/shell", allow_render=True)
    assert (got.tier, got.status, got.html) == ("obscura", "ok", RENDERED)
    assert got.signals == ("js_shell",) and got.engine == "obscura"
    assert got.version == hb.FAKE_VERSION and set(got.timings) == {"egress_ms", "render_ms", "total_ms"}
    assert {r["site"] for r in got.census} == {"fixture.test"}


def test_a_noscript_notice_is_a_shell(rig) -> None:
    assert _browse("/noscript").signals == ("js_shell",)


def test_a_plain_403_may_render_but_429_and_challenges_never_do(rig) -> None:
    got = _browse("/forbidden", allow_render=True)
    assert got.tier == "obscura" and rig.render_calls == ["https://fixture.test/forbidden"]
    got = _browse("/limited", allow_render=True)
    assert (got.tier, got.status, got.reason, got.signals) == ("static", "blocked", "http_429", ("blocked",))
    got = _browse("/challenge", allow_render=True)
    assert got.tier == "static" and "blocked" in got.signals  # a shell too, but refusals win
    assert rig.render_calls == ["https://fixture.test/forbidden"]


def test_a_callers_want_miss_skips_the_static_read(rig) -> None:
    got = _browse("/article", need_signal_from_static="want_miss", allow_render=True)
    assert got.tier == "obscura" and got.ok and got.signals == ("want_miss",)
    assert rig.static_calls == []


def test_the_stealth_tier_is_listed_but_not_selectable(rig) -> None:
    got = _browse("/shell", allow_render=True, engine="obscura-stealth")
    assert got.tier == "static"
    assert got.escalation == {"tier": "obscura-stealth", "status": "tier_disabled",
                              "reason": "tier_disabled"}
    got = _browse("/x", need_signal_from_static="js_shell", allow_render=True, engine="obscura-stealth")
    assert (got.tier, got.status) == ("none", "no_content")
    assert got.escalation["status"] == "tier_disabled" and rig.render_calls == []
    with pytest.raises(ValueError):
        _browse("/x", engine="chromium")


def test_a_failed_render_keeps_the_static_result(rig, monkeypatch) -> None:
    monkeypatch.setattr(runner, "render", _canned("unavailable"))
    got = _browse("/shell", allow_render=True)
    assert (got.tier, got.ok, got.graph is not None) == ("static", True, True)
    assert got.escalation == {"tier": "obscura", "status": "unavailable", "reason": "canned"}


def test_the_breaker_opens_after_three_engine_failures(rig, monkeypatch) -> None:
    calls: list[str] = []
    failing = _canned("engine_error")
    monkeypatch.setattr(runner, "render", lambda url, **kw: (calls.append(url), failing(url, **kw))[1])
    clock = [1000.0]
    monkeypatch.setattr(router, "_monotonic", lambda: clock[0])
    for _ in range(3):
        _browse("/x", need_signal_from_static="want_miss", allow_render=True)
    assert router.engine_health("obscura") == "unhealthy"
    got = _browse("/x", need_signal_from_static="want_miss", allow_render=True)
    assert got.escalation["status"] == "engine_unhealthy" and len(calls) == 3
    assert [r["health"] for r in router.status() if r["name"] == "obscura"] == ["unhealthy"]
    clock[0] += 601  # half-open after 10 minutes: one attempt decides
    monkeypatch.setattr(runner, "render", _canned("ok"))
    assert _browse("/x", need_signal_from_static="want_miss", allow_render=True).ok
    assert router.engine_health("obscura") == "ok"


@pytest.mark.parametrize("status", ["busy", "unavailable", "refused", "blocked", "offsite",
                                    "confinement", "oversize"])
def test_capacity_and_policy_outcomes_never_trip_the_breaker(rig, monkeypatch, status: str) -> None:
    monkeypatch.setattr(runner, "render", _canned(status))
    for _ in range(5):
        _browse("/x", need_signal_from_static="want_miss", allow_render=True)
    assert router.engine_health("obscura") == "ok"


def test_browse_both_runs_the_tiers_at_the_same_time(rig, monkeypatch) -> None:
    static_in, render_in = threading.Event(), threading.Event()
    real_static = router._static_fetch

    def static_fetch(url: str, **kw: object) -> dict:
        static_in.set()
        assert render_in.wait(10), "the render never ran alongside the static read"
        return real_static(url, **kw)

    def render(url: str, **kw: object) -> runner.RenderResult:
        render_in.set()
        assert static_in.wait(10), "the static read never ran alongside the render"
        return rig.real_render(url, **kw)

    monkeypatch.setattr(router, "_static_fetch", static_fetch)
    monkeypatch.setattr(runner, "render", render)
    static, rendered = router.browse_both("https://fixture.test/shell", allow_render=True,
                                          site_policy=POLICY, identity=IDENTITY)
    assert (static.tier, static.signals) == ("static", ("js_shell",))
    assert (rendered.tier, rendered.status, rendered.html) == ("obscura", "ok", RENDERED)
    for result in (static, rendered):
        assert result.engine and result.version and result.identity and result.timings
    only_static, none = router.browse_both("https://fixture.test/article", site_policy=POLICY,
                                           identity=IDENTITY)
    assert only_static.tier == "static" and none is None


def test_need_signals_are_pure_code() -> None:
    long_text = {"text": "word " * 200, "readability": {"kind": "ok"}}
    short_text = {"text": "hi", "readability": {"kind": "shell"}}
    scripts = b"<script></script>" * 5
    assert router.need_signals(long_text, scripts) == ()
    assert router.need_signals(short_text, b"<script></script>" * 2) == ()
    assert router.need_signals(short_text, scripts) == ("js_shell",)
    assert router.need_signals(short_text, b"<noscript>Please turn on JavaScript</noscript>") == ("js_shell",)
    assert router.need_signals(short_text, b"<noscript><img src=x></noscript>") == ()
    challenge = {"text": "Just a moment", "readability": {"kind": "challenge"}}
    assert router.need_signals(challenge, scripts) == ("js_shell", "blocked")
