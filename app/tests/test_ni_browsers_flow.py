"""Round 20 (B3a, ni-format §35): the page-browser ladder in the card flow — the challenge kind,
the router judging the flow's own static read, the eligibility gate, creation / Fix escalation
(js_shell, a plain 403, want_miss), sealing ``render: "browser"`` + the census sites, the draft
landing, R12 feed candidates, and one end-to-end build through the real router → runner → fake
engine → NullWall → egress child. Deterministic and offline; the real engine is never needed."""

from __future__ import annotations

import json

import duckdb
import httpx
import pytest

from smartbrain_3000 import browsers, netguard, ni_flow, ni_routes
from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000.browsers import egress, router, runner
from smartbrain_3000.secrets import gen_master_key
from tests import _browsers as hb

URL = "https://tides.example.org/creek"
INTENT = {"kind": "external_data", "subject": "creek tides", "cadence_minutes": 720,
          "wants": ["high tide time"], "threshold": None, "display_hint": "value"}
PROSE = ("<p>This page is updated through the day by the office that publishes it. Readings are "
         "posted as they come in, and the times shown are local. Check back later for the next "
         "update to these readings, or contact the office with questions about them.</p>")
TIDE_HTML = ("<html><head><title>Creek Tides</title></head><body><h1>Tide Tables</h1>"
             "<p>Tide tables for the creek. High tide at 7:12 AM, low at 1:33 PM.</p>" + PROSE
             + "<table><tr><th>Time</th><th>Height</th><th>Tide</th></tr>"
             "<tr><td>7:12 AM</td><td>5.8 ft</td><td>High</td></tr>"
             "<tr><td>1:33 PM</td><td>0.4 ft</td><td>Low</td></tr></table></body></html>").encode()
SHELL = {"text": "", "title": "Creek Tides", "entities": [], "tables": [], "feeds": [], "meta": {},
         "outline": [], "data": [],
         "signals": {"script_count": 4, "script_bytes": 6000, "noscript_js": False}}
ELIGIBLE = {"eligible": True, "engine": "obscura", "version": "0.2.4", "reason": ""}


@pytest.fixture(autouse=True)
def _local_build_model(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})


@pytest.fixture(autouse=True)
def _no_threaded_worker(monkeypatch):
    started: list[dict] = []
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda store, item_id, **kw: started.append({"id": item_id, **kw}) or True)
    return started


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _model(*, judge: bool = True):
    """Compile prompts pick the high-tide time from the menu; the judge says it serves."""
    calls: list[str] = []

    def call(prompt: str) -> str:
        calls.append(prompt)
        if '"picks"' in prompt:
            line = next((ln for ln in prompt.splitlines() if "Time where Tide=High" in ln), "")
            return json.dumps({"picks": {"high_tide_time": line.split("|")[0].strip(" -") or None}})
        if judge and "serves" in prompt:
            return json.dumps({"serves": True, "gaps": [], "wrong": []})
        return "{}"

    call.calls = calls  # type: ignore[attr-defined]
    return call


def _rendered(sites: list[str], *, html: bytes = TIDE_HTML, assets: list[str] | None = None,
              signals: tuple = ("js_shell",)) -> router.BrowseResult:
    census = [{"site": s, "tunnels": 2, "up": 10, "down": 900, "verdict": "ok"} for s in sites]
    return router.BrowseResult(ok=True, tier="obscura", status="ok", reason="", signals=signals,
                               html=html, graph=None, final_url=URL, engine="obscura", version="0.2.4",
                               identity={"mode": "mimic", "profile": 3, "user_agent": None, "consistent": True},
                               timings={"total_ms": 5}, census=census, assets=assets)


def _spy_browse(monkeypatch, result: router.BrowseResult) -> list[dict]:
    calls: list[dict] = []

    def browse(url: str, **kw: object) -> router.BrowseResult:
        calls.append({"url": url, **kw})
        return result

    monkeypatch.setattr(ni_flow._browser_router, "browse", browse)
    return calls


def _shell_card(monkeypatch, store: nimod.NIStore, page: dict | None = None) -> str:
    monkeypatch.setattr(nimod, "_fetch_http_page", lambda source, item_id, secrets, **kw: dict(page or SHELL))
    item_id = ni_flow.create_shell_item(store, "creek tide times")
    ni_flow._transition(store, item_id, "intent", intent=INTENT)
    return item_id


def _notes(store: nimod.NIStore, item_id: str) -> str:
    return " ".join((ni_flow._flow_read(store, item_id) or {}).get("notes") or [])


# ---- netguard names a challenge; the router never renders one --------------------------------

def _serve(monkeypatch, response: httpx.Response) -> None:
    monkeypatch.setattr("socket.getaddrinfo", lambda node, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda req: response), **kw))


@pytest.mark.parametrize(("status", "headers", "body", "kind", "media"), [
    (403, {"content-type": "text/html", "cf-mitigated": "challenge"}, b"<html>x</html>", "challenge", "text/html"),
    (403, {"content-type": "text/html"}, b"<html><script src='/cdn-cgi/challenge-platform/x'></script>",
     "challenge", "text/html"),
    (403, {"content-type": "text/html; charset=utf-8"}, b"<html><p>Forbidden</p></html>", None, "text/html"),
    (403, {"content-type": "application/json"}, b'{"error": "forbidden"}', None, "application/json"),
    (429, {"content-type": "text/html"}, b"slow down", None, "text/html"),
])
def test_netguard_names_a_challenge_and_keeps_the_refusals_media_type(monkeypatch, status, headers, body,
                                                                      kind, media) -> None:
    _serve(monkeypatch, httpx.Response(status, headers=headers, content=body))
    with pytest.raises(netguard.FetchError) as exc:
        netguard.safe_fetch_page("https://example.test/page")
    assert (exc.value.status, exc.value.kind, exc.value.content_type) == (status, kind, media)


def test_a_drip_error_page_cannot_stall_the_challenge_read(monkeypatch) -> None:
    clock = iter(range(10_000))
    monkeypatch.setattr(netguard, "_monotonic", lambda: float(next(clock)))  # one second per look

    def drip():
        for _ in range(50):
            yield b"x"
        yield b"just a moment"  # past the 5 s budget: never read

    _serve(monkeypatch, httpx.Response(403, headers={"content-type": "text/html"}, content=drip()))
    with pytest.raises(netguard.FetchError) as exc:
        netguard.safe_fetch_page("https://example.test/page")
    assert (exc.value.status, exc.value.kind) == (403, None)


def test_a_2xx_bot_check_is_a_challenge_never_content(monkeypatch) -> None:
    _serve(monkeypatch, httpx.Response(202, headers={"content-type": "text/html",
                                                     "x-amzn-waf-action": "challenge"}, content=b"<html/>"))
    with pytest.raises(netguard.FetchError) as exc:
        netguard.safe_fetch_page("https://example.test/page")
    assert exc.value.kind == "challenge" and exc.value.status is None


@pytest.mark.parametrize(("failure", "renders"), [
    (netguard.FetchError("HTTP 403", status=403), True),
    (netguard.FetchError("HTTP 403", status=403, kind="challenge"), False),
    (netguard.FetchError("HTTP 429", status=429), False),
])
def test_the_router_judges_a_callers_static_read(monkeypatch, failure, renders) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runner, "render", lambda url, **kw: calls.append(url) or runner.RenderResult(
        ok=True, status="ok", reason="", html=b"<p>x</p>", final_url=url, assets=None, engine="obscura",
        version="0.2.4", identity={}, timings={"total_ms": 1}, census=[]))
    static = router.static_evidence(URL, failure=failure)
    got = router.browse(URL, static=static, allow_render=True, site_policy={"mode": "open"},
                        identity={"card": "c1"})
    assert (got.tier == "obscura") is renders and bool(calls) is renders
    assert static.signals == ("blocked",)


# ---- the eligibility gate ---------------------------------------------------------------------

def test_available_is_disabled_by_the_opt_out_and_prepare_never_starts() -> None:
    assert browsers.available() == {"eligible": False, "engine": "obscura", "version": "",
                                    "reason": "disabled"}
    assert browsers.prepare() is False
    assert ni_flow.tap_notice() == {"render_notice": False, "site_cap": 12}


def test_available_is_eligible_only_after_the_self_test_under_the_wall(tmp_path, monkeypatch) -> None:
    hb.install_fake(monkeypatch, tmp_path)
    got = browsers.available()
    assert got == {"eligible": True, "engine": "obscura", "version": hb.FAKE_VERSION, "reason": ""}
    assert browsers.progress() == {"phase": "ready", "pct": 100}
    assert ni_flow.tap_notice()["render_notice"] is True
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": {**got, "eligible": False,
                                                                         "reason": "sandbox"})
    assert ni_flow.tap_notice()["render_notice"] is False


# ---- the ladder at creation ------------------------------------------------------------------

def test_a_js_shell_renders_once_at_creation_and_seals_its_own_site(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    calls = _spy_browse(monkeypatch, _rendered(["example.org"]))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "ready", out
    item = store.get_item(item_id)
    assert item["spec"]["source"] == {"type": "http_page", "url": URL, "render": "browser",
                                      "sites": ["example.org"]}
    assert item["state"] == "commissioning" and nimod.awaits_yes(item)  # first-party: under the tap
    assert [st["op"] for st in item["spec"]["pipeline"]] == ["graph_extract"]
    assert len(calls) == 1 and calls[0]["static"].signals == ("js_shell",)
    assert calls[0]["site_policy"] == {"mode": "open"} and calls[0]["want_assets"] is True
    assert calls[0]["identity"] == {"card": item_id} and calls[0]["allow_render"] is True
    assert "page browser" in _notes(store, item_id)
    fields = ni_routes._render_fields(item)
    assert fields == {"rendered": True, "render": {"sites": ["example.org"], "unavailable": False},
                      "draft": None, "preview": None}


def test_a_page_that_loads_other_sites_lands_as_a_draft_listing_them(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    assets = ["https://api.tidecdn.net/v1/tides.json?station=1", "https://example.org/app.js",
              "https://tracker.example.com/collect?fmt=json", "https://example.org/api/v2/levels"]
    _spy_browse(monkeypatch, _rendered(["example.org", "tidecdn.net", "zz-fonts.com"], assets=assets))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "ready", out
    item = store.get_item(item_id)
    assert item["state"] == "draft" and nimod.render_needs_consent(item["spec"])
    assert item["spec"]["source"]["sites"] == ["example.org", "tidecdn.net", "zz-fonts.com"]
    assert item["spec"]["_feed_candidates"] == [
        {"url": "https://api.tidecdn.net/v1/tides.json?station=1", "site": "tidecdn.net"},
        {"url": "https://example.org/api/v2/levels", "site": "example.org"}]
    assert "waiting for your OK to also load: tidecdn.net, zz-fonts.com" in _notes(store, item_id)
    fields = ni_routes._render_fields(item)
    assert fields["draft"] == {"reason": "render_sites", "sites": ["example.org", "tidecdn.net", "zz-fonts.com"]}
    assert fields["preview"]["feed_candidates"][0]["url"] == "https://api.tidecdn.net/v1/tides.json?station=1"


def test_no_render_while_the_browser_is_ineligible_links_as_today(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    monkeypatch.setattr(ni_flow._browser_router, "browse",
                        lambda *a, **kw: pytest.fail("rendered while ineligible"))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "failed"  # a pasted shell page with nothing readable: the honest stop
    assert "render_skipped:disabled" in _notes(store, item_id)
    assert store.get_item(item_id)["spec"].get("_shell") is True


def test_a_static_challenge_page_is_never_rendered_and_the_note_says_why(monkeypatch) -> None:
    store = _store()
    page = {**SHELL, "title": "Just a moment...",
            "text": "Just a moment... Checking your browser before accessing the site."}
    item_id = _shell_card(monkeypatch, store, page=page)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    monkeypatch.setattr(runner, "render", lambda *a, **kw: pytest.fail("rendered a challenge"))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "failed" and "render_skipped:challenge" in _notes(store, item_id)


def test_more_than_twelve_sites_is_an_honest_stop(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    _spy_browse(monkeypatch, _rendered(["example.org"] + [f"site{i}.com" for i in range(12)]))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "failed" and "render_failed:too_many_sites" in _notes(store, item_id)


def test_a_preparable_engine_is_prepared_and_waited_for_with_progress(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    answers = [{**ELIGIBLE, "eligible": False, "reason": "absent"},
               {**ELIGIBLE, "eligible": False, "reason": "preparing"}, dict(ELIGIBLE)]
    seen: list[dict | None] = []
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": answers.pop(0) if len(answers) > 1
                        else answers[0])
    monkeypatch.setattr(browsers, "prepare", lambda engine="obscura": True)
    monkeypatch.setattr(browsers, "progress", lambda engine="obscura": {"phase": "downloading", "pct": 43})
    monkeypatch.setattr(ni_flow, "_sleep", lambda s: seen.append(
        ni_flow.board_flow_field(store, item_id).get("browser")))
    _spy_browse(monkeypatch, _rendered(["example.org"]))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "ready", out
    assert seen[0] == {"phase": "downloading", "pct": 43}  # "Preparing the page browser (43%)…"
    assert "_browser" not in {k for k, v in ni_flow._flow_read(store, item_id).items() if v}


# ---- refusals at the sampling door: a plain 403 renders once, a 429 / challenge never --------

@pytest.mark.parametrize(("failure", "door"), [
    (netguard.FetchError("HTTP 403", status=403, content_type="text/html"), True),
    (netguard.FetchError("HTTP 403", status=403, content_type="application/json"), False),
    (netguard.FetchError("HTTP 403", status=403, kind="challenge", content_type="text/html"), False),
    (netguard.FetchError("HTTP 429", status=429, content_type="text/html"), False),
])
def test_a_pasted_links_refusal_reaches_the_page_browser_only_when_plain(monkeypatch, failure, door) -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "creek tide times")
    ni_flow._transition(store, item_id, "intent", intent=INTENT)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    calls = _spy_browse(monkeypatch, _rendered(["example.org"], signals=("blocked",)))
    monkeypatch.setattr(nimod, "_fetch_http_page",
                        lambda *a, **kw: pytest.fail("a second static fetch of a refused page"))

    def refuse(_url: str) -> object:
        raise failure

    out = ni_flow._sample_and_map(store, item_id, "creek tide times", INTENT, URL, _model(), refuse)
    assert (out["state"] == "ready") is door and len(calls) == int(door)
    if door:
        assert calls[0]["static"].reason == "http_403" and calls[0]["static"].graph is None
        assert store.get_item(item_id)["spec"]["source"]["render"] == "browser"


# ---- want_miss and the one-visit rule ----------------------------------------------------------

# a readable page that names the ask but carries no structure, and 40 KB of script: want_miss
UNFIT = {**SHELL, "text": "The creek tide tables load below once the page finishes. " * 10,
         "signals": {"script_count": 9, "script_bytes": 40_000, "noscript_js": False}}


def test_want_miss_renders_once_and_is_adopted_only_when_it_compiles(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store, page=UNFIT)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    calls = _spy_browse(monkeypatch, _rendered(["example.org"], signals=("want_miss",)))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert out["state"] == "ready", out
    assert len(calls) == 1 and calls[0]["need_signal_from_static"] == "want_miss"
    assert calls[0]["static"] is None
    assert store.get_item(item_id)["spec"]["source"]["render"] == "browser"


def test_a_want_miss_render_without_structure_is_not_adopted(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store, page=UNFIT)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    bare = b"<html><head><title>Marina</title></head><body>" + PROSE.encode() + b"</body></html>"
    _spy_browse(monkeypatch, _rendered(["example.org"], html=bare, signals=("want_miss",)))
    ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert "render_failed:no_structure" in _notes(store, item_id)
    assert store.get_item(item_id)["spec"]["source"].get("render") is None


def test_a_failed_shell_render_spends_the_one_visit(monkeypatch) -> None:
    store = _store()
    # readable (≥ 200 chars) yet a shell (< 400 visible): the shell render fails, then want_miss holds
    item_id = _shell_card(monkeypatch, store,
                          page={**UNFIT, "text": "The creek tide tables for today are listed for local anglers. " * 5})
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    failed = router.BrowseResult(**{**_rendered([]).__dict__, "ok": False, "tier": "static",
                                    "status": "ok", "html": None,
                                    "escalation": {"tier": "obscura", "status": "timeout", "reason": "deadline"}})
    calls = _spy_browse(monkeypatch, failed)
    ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert len(calls) == 1 and "render_failed:timeout" in _notes(store, item_id)


# ---- Fix escalation ---------------------------------------------------------------------------

def test_a_failing_static_page_cards_fix_renders_once_and_keeps_the_sites_consent(monkeypatch) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    _spy_browse(monkeypatch, _rendered(["example.org", "tidecdn.net"]))
    ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert store.get_item(item_id)["state"] == "draft"
    store.record_render_consent(item_id)
    store.set_state(item_id, "live")
    calls = _spy_browse(monkeypatch, _rendered(["example.org", "tidecdn.net"]))
    out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model(), remap=True)
    assert out["state"] == "ready" and len(calls) == 1 and calls[0]["want_assets"] is False
    item = store.get_item(item_id)
    assert item["spec"]["_render_ok"] is True and item["state"] == "commissioning"  # same sites: kept
    wider = _spy_browse(monkeypatch, _rendered(["example.org", "tidecdn.net", "example.net"]))
    ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model(), remap=True)
    item = store.get_item(item_id)
    assert len(wider) == 1 and item["state"] == "draft" and "_render_ok" not in item["spec"]


# ---- R12: feed candidates and the switch-feed door ---------------------------------------------

def test_feed_candidates_are_json_looking_https_urls_on_the_sealed_sites_only() -> None:
    sites = ["example.org", "tidecdn.net"]
    assets = ["https://example.org/data/levels.json", "https://example.org/api/v1/x",
              "https://api.tidecdn.net/q", "https://tidecdn.net/feed?format=json",
              "https://example.org/app.js", "http://example.org/x.json",
              "https://evil.example.com/x.json", "https://user:pw@example.org/x.json",
              "https://example.org/data/levels.json", "https://example.org/apiary",
              "https://example.org/z.json", "https://example.org/y.json"]
    got = ni_flow._feed_candidates_of(assets, sites)
    assert [c["url"] for c in got] == ["https://example.org/data/levels.json", "https://example.org/api/v1/x",
                                       "https://api.tidecdn.net/q", "https://tidecdn.net/feed?format=json",
                                       "https://example.org/z.json"]
    assert {c["site"] for c in got} == {"example.org", "tidecdn.net"}


def test_switch_feed_takes_only_a_sealed_candidate_and_re_enters_at_the_tap(monkeypatch,
                                                                            _no_threaded_worker) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(ELIGIBLE))
    _spy_browse(monkeypatch, _rendered(["example.org"], assets=["https://example.org/api/tides"]))
    ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    item = store.get_item(item_id)
    with pytest.raises(ValueError):
        ni_flow.begin_switch_feed(store, item, "https://example.org/api/other")
    assert ni_flow.begin_switch_feed(store, item, "https://example.org/api/tides") is True
    record = ni_flow._flow_read(store, item_id)
    assert (record["state"], record["source_url"], record["_pasted"]) == (
        "intent", "https://example.org/api/tides", "https://example.org/api/tides")
    assert _no_threaded_worker[-1] == {"id": item_id, "source_url": "https://example.org/api/tides"}


# ---- one build end to end: router → runner → fake engine → NullWall → egress child -------------

def test_end_to_end_a_shell_page_renders_through_the_fake_engine(tmp_path, monkeypatch) -> None:
    hb.install_fake(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(runner, "_RUN_PARENT", str(runs))
    target = hb.LocalServer({"/creek": (200, {"Content-Type": "text/html"}, TIDE_HTML)})
    names = {"tides.fixture.test": [hb.PUBLIC_IP], "cdn.fixturecdn.test": [hb.PUBLIC_IP]}
    monkeypatch.setattr(egress, "start", hb.egress_starter(names, target.port, []))
    (runs / "fake_mode.json").write_text(json.dumps({
        "mode": "offsite", "offsite_host": "cdn.fixturecdn.test",
        "assets": ["https://cdn.fixturecdn.test/v1/tides.json", "https://tides.fixture.test/app.js"]}))
    store = _store()
    item_id = _shell_card(monkeypatch, store)
    url = "https://tides.fixture.test/creek"
    try:
        out = ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, url, _model())
    finally:
        target.close()
    assert out["state"] == "ready", (out, _notes(store, item_id))
    item = store.get_item(item_id)
    assert item["spec"]["source"] == {"type": "http_page", "url": url, "render": "browser",
                                      "sites": ["fixture.test", "fixturecdn.test"]}
    assert item["state"] == "draft"  # the page loaded another site: Activate is the consent
    assert item["spec"]["_feed_candidates"] == [{"url": "https://cdn.fixturecdn.test/v1/tides.json",
                                                 "site": "fixturecdn.test"}]
    seen = [json.loads(line) for line in (runs / "fake_seen.jsonl").read_text().splitlines()]
    assert len(seen) == 2 and "assets" in seen[1]["argv"]  # the html run, then the creation-only assets run


def test_the_board_offers_the_paste_its_render_notice_at_the_pick_pause(monkeypatch) -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "creek tide times")
    ni_flow._transition(store, item_id, "source", error=ni_flow.AWAITING_SOURCE_PICK)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": {**ELIGIBLE, "eligible": False,
                                                                         "reason": "absent"})
    assert ni_flow.board_flow_field(store, item_id)["tap"] == {"render_notice": True, "site_cap": 12}


# ---- the launcher's cue: the ladder marks a render WANTED even when it can't render here -----

@pytest.fixture()
def fresh_wanted():
    browsers._WANTED.clear()
    yield
    browsers._WANTED.clear()


NO_SIDECAR = {**ELIGIBLE, "eligible": False, "reason": "docker_no_sidecar"}
READABLE = {**SHELL, "text": "The creek tide tables are posted here twice a day by the harbor office. " * 8,
            "signals": {"script_count": 1, "script_bytes": 300, "noscript_js": False}}
CHALLENGE = {**SHELL, "title": "Just a moment...",
             "text": "Just a moment... Checking your browser before accessing the site."}


@pytest.mark.parametrize(("page", "wanted"), [(SHELL, True), (UNFIT, True), (READABLE, False),
                                              (CHALLENGE, False)])
def test_the_ladder_marks_a_render_wanted_only_when_it_would_render(monkeypatch, fresh_wanted, page,
                                                                    wanted) -> None:
    store = _store()
    item_id = _shell_card(monkeypatch, store, page=page)
    monkeypatch.setattr(browsers, "available", lambda engine="obscura": dict(NO_SIDECAR))
    monkeypatch.setattr(ni_flow._browser_router, "browse", lambda *a, **kw: pytest.fail("rendered"))
    ni_flow._build_page_card(store, item_id, "creek tide times", INTENT, URL, _model())
    assert browsers.renderer_status()["wanted"] is wanted
    assert ("render_skipped:docker_no_sidecar" in _notes(store, item_id)) is wanted


def test_a_pasted_plain_403_marks_a_render_wanted(monkeypatch, fresh_wanted) -> None:
    store = _store()
    item_id = ni_flow.create_shell_item(store, "creek tide times")
    ni_flow._transition(store, item_id, "intent", intent=INTENT)

    def refuse(_url: str) -> object:
        raise netguard.FetchError("HTTP 403", status=403, content_type="text/html")

    out = ni_flow._sample_and_map(store, item_id, "creek tide times", INTENT, URL, _model(), refuse)
    assert out["state"] == "failed" and browsers.renderer_status()["wanted"] is True
    assert "render_skipped:disabled" in _notes(store, item_id)  # the suite's opt-out
