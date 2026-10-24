"""S5a politeness (plan D9): per-host budget, Retry-After pause, fetch dedupe, the third-party
cadence floor, the NI port policy and robots.txt for page reads. Class tests on synthetic hosts:
no named site, no network (``netguard._guarded_get_once`` is the one faked seam)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import netguard, ni, ni_polite
from smartbrain_3000.secrets import gen_master_key

JSON_BODY = b'{"text": "hello"}'


class _Wire:
    """The faked transport: counts requests per URL and answers from a script."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.answers: dict[str, object] = {}  # url -> dict | FetchError | callable

    def __call__(self, url, allowed_ct, max_bytes, *args, **kwargs):
        self.calls.append(url)
        answer = self.answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        if url.endswith("/robots.txt"):
            raise netguard.FetchError("upstream returned HTTP 404", status=404)
        return {"final_url": url, "status": 200, "content_type": "application/json", "content": JSON_BODY}

    def count(self, host: str) -> int:
        return sum(1 for u in self.calls if f"//{host}" in u and not u.endswith("/robots.txt"))


@pytest.fixture
def wire(monkeypatch):
    ni_polite.SHARED.reset()
    now = [1000.0]
    monkeypatch.setattr(ni_polite, "_clock", lambda: now[0])
    monkeypatch.setattr(ni_polite, "_wall", lambda: 1_700_000_000.0 + now[0])
    w = _Wire()
    w.now = now
    monkeypatch.setattr(netguard, "_guarded_get_once", w)
    yield w
    ni_polite.SHARED.reset()
    ni_polite.set_dedupe(False)


def _http_spec(url: str, interval: int = 15) -> dict:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title", "tone": "default", "size": "md"}]}
    return {"version": 1, "title": "Synthetic", "goal": "show text", "params": {},
            "source": {"type": "http_json", "url": url},
            "pipeline": [{"op": "extract", "paths": {"text": "text"}}], "scene": scene,
            "display": {"size": "small"}, "contract": None, "interval_minutes": interval,
            "repair_policy": {"l1": True, "l2_frontier": False}, "model": None}


def _store():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return ni.NIStore(conn, key), conn, key


def _app(conn, key):
    return SimpleNamespace(state=SimpleNamespace(master_key=key, db=SimpleNamespace(cursor=conn.cursor)))


def _fetch(url: str, **kw) -> dict:
    with ni_polite.engaged():
        return netguard.safe_fetch_json(url, **kw)


# --- the gate: 50 cards on one host ---------------------------------------------------------

def test_fifty_cards_on_one_host_make_one_request_per_interval(wire) -> None:
    store, conn, key = _store()
    url = "https://api.synthetic-host.example/v1/now"
    ids = []
    for _ in range(50):
        iid = store.add_item(_http_spec(url), {"text": "preview"})
        store.set_state(iid, "live")
        ids.append(iid)
    conn.execute("UPDATE ni_items SET last_checked = NULL;")
    for _ in range(25):  # more than enough passes (3 cards each) to serve all 50
        ni.tick(_app(conn, key))
    assert wire.count("api.synthetic-host.example") == 1, "one request served all 50 cards"
    assert all(store.get_item(i)["last_status"] != "host_busy" for i in ids)
    due = conn.execute("SELECT count(*) FROM ni_items WHERE last_checked IS NULL;").fetchone()[0]
    assert due == 0, "every card was served from the one response"


def test_cards_over_budget_stay_due_and_are_never_failed(wire) -> None:
    store, conn, key = _store()
    ids = []
    for n in range(8):  # eight DIFFERENT urls on one host: the bucket (not dedupe) bounds them
        iid = store.add_item(_http_spec(f"https://bucket-host.example/v1/{n}"), {"text": "preview"})
        store.set_state(iid, "live")
        ids.append(iid)
    conn.execute("UPDATE ni_items SET last_checked = NULL;")
    for _ in range(8):
        ni.tick(_app(conn, key))
    assert wire.count("bucket-host.example") == ni_polite.HOST_BURST, "the burst, then deferral"
    waiting = [i for i in ids if store.get_item(i)["last_checked"] is None]
    assert len(waiting) == 8 - ni_polite.HOST_BURST, "deferred cards stay due"
    assert all(store.get_item(i)["consecutive_failures"] == 0 for i in ids), "never marked failed"
    wire.now[0] += ni_polite.HOST_REFILL_S * 3
    ni.tick(_app(conn, key))
    assert wire.count("bucket-host.example") == ni_polite.HOST_BURST + 3, "the refill serves them"


# --- a card build waits out SmartBrain's own spacing -----------------------------------------------

def _sleeps(wire, monkeypatch) -> list:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        wire.now[0] += seconds

    monkeypatch.setattr(ni_polite, "_sleep", sleep)
    return slept


def test_a_build_waits_for_the_bucket_where_a_refresh_defers(wire, monkeypatch) -> None:
    """tune2 (2026-10-24): six weather asks lost their Library source to SmartBrain's own budget. A build someone is
    waiting on waits for the refill; a refresh still defers (the card stays due)."""
    slept = _sleeps(wire, monkeypatch)
    for n in range(ni_polite.HOST_BURST):
        _fetch(f"https://bucket-host.example/v1/{n}")
    with pytest.raises(netguard.FetchError) as busy:
        _fetch("https://bucket-host.example/v1/refresh")
    assert busy.value.kind == "host_busy" and not slept
    with ni_polite.waiting():
        assert _fetch("https://bucket-host.example/v1/build") == {"text": "hello"}
    assert len(slept) == 1 and slept[0] <= ni_polite.HOST_REFILL_S
    assert not getattr(ni_polite.SHARED._local, "wait", None), "the allowance ends with the build"


def test_a_build_waits_at_most_its_allowance(wire, monkeypatch) -> None:
    slept = _sleeps(wire, monkeypatch)
    with ni_polite.waiting(ni_polite.HOST_REFILL_S):
        for n in range(ni_polite.HOST_BURST + 1):  # the burst, then one refill's wait
            _fetch(f"https://bucket-host.example/v2/{n}")
        with pytest.raises(netguard.FetchError) as busy:
            _fetch("https://bucket-host.example/v2/more")
    assert busy.value.kind == "host_busy"
    assert sum(slept) <= ni_polite.HOST_REFILL_S


def test_a_build_never_waits_out_a_pause_the_host_asked_for(wire, monkeypatch) -> None:
    slept = _sleeps(wire, monkeypatch)
    url = "https://slow-host.example/api"
    wire.answers[url] = netguard.FetchError("upstream returned HTTP 429", status=429, retry_after="20")
    with ni_polite.waiting():
        with pytest.raises(netguard.FetchError):
            _fetch(url)
        with pytest.raises(netguard.FetchError) as second:
            _fetch("https://slow-host.example/other")
    assert second.value.kind == "host_busy" and not slept, "the host's word, not SmartBrain's spacing"


def test_run_flow_builds_inside_the_wait(monkeypatch) -> None:
    from smartbrain_3000 import ni_flow

    seen: list[float] = []
    monkeypatch.setattr(ni_flow, "_run_flow", lambda *a: seen.append(ni_polite.SHARED._local.wait) or {})
    ni_flow.run_flow(object(), "item-1")
    assert seen == [ni_polite.BUILD_WAIT_S]
    assert not getattr(ni_polite.SHARED._local, "wait", None)


def test_a_nested_wait_never_grants_more_and_is_charged_to_the_outer(wire, monkeypatch) -> None:
    slept = _sleeps(wire, monkeypatch)
    with ni_polite.waiting(40.0):
        with ni_polite.waiting(ni_polite.BUILD_WAIT_S):
            assert ni_polite.SHARED._local.wait == 40.0, "never more than the allowance left"
            for n in range(ni_polite.HOST_BURST + 1):
                _fetch(f"https://bucket-host.example/v3/{n}")
        assert ni_polite.SHARED._local.wait == 40.0 - sum(slept), "the inner wait is charged to the outer"
    assert not getattr(ni_polite.SHARED._local, "wait", None)


def test_research_time_stops_while_a_build_waits_for_a_host(wire, monkeypatch) -> None:
    """PR #544 review S1: the wait must not spend the researcher's 120 s clock (a run that would have found a second
    source must not end on its time budget because SmartBrain was spacing its own requests)."""
    from smartbrain_3000.ni_research.ctx import ResearchCtx

    slept = _sleeps(wire, monkeypatch)
    with ni_polite.waiting():   # a build: research starts inside its wait, as run_flow runs it
        ctx = ResearchCtx(request="weather", question={})
        for n in range(ni_polite.HOST_BURST + 1):
            _fetch(f"https://bucket-host.example/v4/{n}")
        assert slept and ctx.elapsed() == 0.0
        wire.now[0] += 5.0
        assert ctx.elapsed() == 5.0, "work time still counts"


def test_a_builds_wait_never_leaks_into_later_clock_readings(wire, monkeypatch) -> None:
    """PR #544 re-check B1: the time slept is counted per build, never as a running total, so a later research run on
    the same thread (another build, a test after the politeness tests) never reads a clock thrown off by it."""
    from smartbrain_3000.ni_research.ctx import ResearchCtx

    slept = _sleeps(wire, monkeypatch)
    for build in range(3):
        with ni_polite.waiting():
            for n in range(ni_polite.HOST_BURST + 1):
                _fetch(f"https://bucket-host-{build}.example/v5/{n}")
    assert slept and ni_polite.work_clock() == wire.now[0], "no build's wait is still subtracted after it ends"
    ctx = ResearchCtx(request="weather", question={})
    wire.now[0] += 2.0
    assert ctx.elapsed() == 2.0


# --- Retry-After ---------------------------------------------------------------------------

def test_retry_after_seconds_pauses_the_host_then_releases_it(wire) -> None:
    url = "https://slow-host.example/api"
    wire.answers[url] = netguard.FetchError("upstream returned HTTP 429", status=429, retry_after="120")
    with pytest.raises(netguard.FetchError) as first:
        _fetch(url)
    assert first.value.kind == "host_busy" and first.value.status == 429
    before = len(wire.calls)
    with pytest.raises(netguard.FetchError) as second:
        _fetch("https://slow-host.example/other")
    assert second.value.kind == "host_busy" and len(wire.calls) == before, "no request while paused"
    wire.now[0] += 121
    wire.answers.pop(url)
    assert _fetch(url) == {"text": "hello"}, "released after the pause"


def test_retry_after_parses_seconds_and_http_dates_and_caps_the_pause(wire) -> None:
    assert ni_polite.parse_retry_after("90") == 90.0
    later = datetime.fromtimestamp(ni_polite._wall() + 600, UTC)
    assert 590 <= ni_polite.parse_retry_after(format_datetime(later, usegmt=True)) <= 600
    assert ni_polite.parse_retry_after("999999") == ni_polite.RETRY_AFTER_CAP_S
    past = format_datetime(datetime.fromtimestamp(ni_polite._wall(), UTC) - timedelta(hours=1), usegmt=True)
    assert ni_polite.parse_retry_after(past) == ni_polite.RETRY_AFTER_MIN_S
    assert ni_polite.parse_retry_after("soon") is None and ni_polite.parse_retry_after(None) is None


def test_a_503_without_retry_after_fails_once_but_still_pauses_the_host(wire) -> None:
    url = "https://flaky-host.example/api"
    wire.answers[url] = netguard.FetchError("upstream returned HTTP 503", status=503)
    with pytest.raises(netguard.FetchError) as first:
        _fetch(url)
    assert first.value.kind is None and first.value.status == 503, "an honest failure for this card"
    with pytest.raises(netguard.FetchError) as second:
        _fetch("https://flaky-host.example/next")
    assert second.value.kind == "host_busy", "the host rests for the default pause"
    assert ni.NIError("host_busy").kind in ni.CAPACITY_KINDS


# --- the cadence floor ---------------------------------------------------------------------

def test_floor_applies_to_third_party_hosts_and_exempts_own_hosts() -> None:
    third = _http_spec("https://api.third-party.example/x", interval=1)
    assert ni_polite.cadence_floor_minutes(third) == 15
    for own in ("http://localhost/x", "http://192.168.1.20/x", "http://printer.local/x",
                "http://nas/x", "https://box.home.arpa/x"):
        assert ni_polite.cadence_floor_minutes(_http_spec(own)) == 0, own
    assert ni_polite.cadence_floor_minutes({"source": {"type": "model"}}) == 0
    assert ni_polite.cadence_floor_minutes(_http_spec("https://{host}/x")) == 15


def test_floor_clamps_at_run_time_without_rewriting_the_row() -> None:
    store, conn, _key = _store()
    iid = store.add_item(_http_spec("https://api.third-party.example/x", interval=1), {"text": "p"})
    store.set_state(iid, "live")
    conn.execute("UPDATE ni_items SET interval_minutes = 1;")  # a card stored below the floor
    now = datetime.now(UTC)
    conn.execute("UPDATE ni_items SET last_checked = ?;", [now - timedelta(minutes=5)])
    item = store.get_item(iid)
    assert item["interval_minutes"] == 1, "the stored row is not rewritten"
    assert not ni._is_due(item, now), "5 minutes is inside the 15 minute floor"
    assert ni._is_due(item, now + timedelta(minutes=11))
    own = store.add_item(_http_spec("http://localhost/x", interval=1), {"text": "p"})
    assert store.get_item(own)["interval_minutes"] == 1, "an own host keeps its cadence"
    assert store.get_item(iid)["interval_minutes"] == 1


def test_new_and_edited_specs_are_clamped_to_the_floor() -> None:
    store, _conn, _key = _store()
    iid = store.add_item(_http_spec("https://api.third-party.example/x", interval=2), {"text": "p"})
    assert store.get_item(iid)["interval_minutes"] == ni_polite.CADENCE_FLOOR_MINUTES


# --- ports -----------------------------------------------------------------------------------

def test_ni_fetches_refuse_odd_ports_unless_the_library_row_declared_one(wire) -> None:
    pol = ni_polite.SHARED
    assert pol.port_ok(None) and pol.port_ok(80) and pol.port_ok(443)
    assert not pol.port_ok(8080) and not pol.port_ok(22)
    with ni_polite.engaged(port=8080):
        assert pol.port_ok(8080) and not pol.port_ok(9090)
    assert not pol.port_ok(8080), "the declaration does not outlive its fetch"
    assert netguard.active_politeness() is None, "netguard is untouched outside an NI fetch"


def test_own_hosts_and_the_sealed_address_keep_their_port(monkeypatch) -> None:
    pol = ni_polite.SHARED
    monkeypatch.setenv("SB_NI_OWN_HOSTS", "home.example")
    assert pol.port_ok(8123, "nas.home.example") and pol.port_ok(8123, "192.168.1.5")
    assert not pol.port_ok(8123, "api.third-party.example")
    assert ni_polite.declared_port({"url": "http://api.third-party.example:8123/x"}) == 8123
    assert ni_polite.declared_port({"url": "https://a.example/x", "port": 8443}) == 8443
    assert ni_polite.declared_port({"url": "https://a.example/x"}) is None
    assert ni_polite.declared_port({"url": "https://a.example:99999/x"}) is None


def test_a_kept_card_on_an_odd_port_is_not_refused_for_its_port(monkeypatch) -> None:
    ni_polite.SHARED.reset()
    monkeypatch.setattr(netguard, "_validated_ip", lambda host: "93.184.216.34")

    def no_dial(*a, **k):
        raise OSError("no network in tests")

    monkeypatch.setattr(netguard, "_send_pinned", no_dial)
    with pytest.raises(ni.NIError) as got:
        ni._fetch_http_json({"type": "http_json", "url": "https://api.third-party.example:8443/x"}, "item", None)
    assert "port" not in str(got.value), "the port the user kept is part of the card"
    with ni_polite.engaged(), pytest.raises(netguard.FetchError) as other:
        netguard._guarded_get_once("https://api.third-party.example:8443/x", ("text/",), 1000)
    assert other.value.kind == "port_refused", "an undeclared odd port is still refused"


def test_a_url_netguard_refuses_reports_netguard_s_reason(wire) -> None:
    with ni_polite.engaged(), pytest.raises(netguard.FetchError) as bad:
        ni_polite.admit_page("http:///x")
    assert "no host" in str(bad.value)
    assert ni_polite.SHARED.robots_ok("https://user:pw@a.example/x"), "userinfo is netguard's refusal, not robots"
    assert not any(u.endswith("/robots.txt") for u in wire.calls)


def test_port_policy_runs_inside_the_real_guard(monkeypatch) -> None:
    ni_polite.SHARED.reset()
    monkeypatch.setattr(netguard, "_validated_ip", lambda host: "93.184.216.34")
    with ni_polite.engaged(), pytest.raises(netguard.FetchError) as bad:
        netguard._guarded_get_once("https://p.example:8443/x", ("text/",), 1000)
    assert bad.value.kind == "port_refused"
    def no_dial(*a, **k):
        raise OSError("no network in tests")

    monkeypatch.setattr(netguard, "_send_pinned", no_dial)  # outside NI the port is not the refusal
    with pytest.raises(netguard.FetchError) as other:
        netguard._guarded_get_once("https://p.example:8443/x", ("text/",), 1000)
    assert other.value.kind != "port_refused"


def test_source_port_is_a_closed_optional_field() -> None:
    spec = _http_spec("https://api.third-party.example:8443/x")
    spec["source"]["port"] = 8443
    assert ni.validate_spec(spec)["source"]["port"] == 8443
    spec["source"]["port"] = "8443"
    with pytest.raises(ValueError):
        ni.validate_spec(spec)


# --- robots.txt ------------------------------------------------------------------------------

def _page(url: str) -> dict:
    with ni_polite.engaged():
        return netguard.safe_fetch_page(url)


def test_robots_disallow_stops_a_page_read_but_not_a_json_api(wire) -> None:
    wire.answers["https://robots-host.example/robots.txt"] = {
        "final_url": "x", "status": 200, "content_type": "text/plain",
        "content": b"User-agent: *\nDisallow: /private/\n"}
    wire.answers["https://robots-host.example/private/page"] = {
        "final_url": "x", "status": 200, "content_type": "text/html", "content": b"<html></html>"}
    with pytest.raises(netguard.FetchError) as no:
        _page("https://robots-host.example/private/page")
    assert no.value.kind == "robots"
    assert _page("https://robots-host.example/public/page")["status"] == 200
    assert _fetch("https://robots-host.example/private/data") == {"text": "hello"}, "JSON APIs skip robots"
    robots_calls = [u for u in wire.calls if u.endswith("/robots.txt")]
    _page("https://robots-host.example/public/other")
    assert [u for u in wire.calls if u.endswith("/robots.txt")] == robots_calls, "robots.txt is cached"


def _robots_host(wire) -> None:
    wire.answers["https://robots-host.example/robots.txt"] = {
        "final_url": "x", "status": 200, "content_type": "text/plain",
        "content": b"User-agent: *\nDisallow: /private/\n"}


def test_a_source_from_outside_the_library_obeys_robots_whichever_fetcher_reads_it(wire) -> None:
    """M2 (review #535): robots.txt is keyed on where the source came from, not on which fetcher ran."""
    _robots_host(wire)
    with ni_polite.engaged(robots=True):
        for read in (lambda u: netguard.safe_fetch_json(u), lambda u: netguard.safe_fetch_text(u, "csv"),
                     lambda u: netguard.safe_fetch_page(u)):
            with pytest.raises(netguard.FetchError) as no:
                read("https://robots-host.example/private/data")
            assert no.value.kind == "robots"
        assert netguard.safe_fetch_json("https://robots-host.example/public/data") == {"text": "hello"}
        with ni_polite.engaged(), pytest.raises(netguard.FetchError):  # an inner engage keeps the enclosing answer
            netguard.safe_fetch_json("https://robots-host.example/private/data")
        wire.answers["http://10.0.0.5/robots.txt"] = {"final_url": "x", "status": 200, "content_type": "text/plain",
                                                      "content": b"User-agent: *\nDisallow: /\n"}
        assert netguard.safe_fetch_json("http://10.0.0.5/private/x") == {"text": "hello"}, "own hosts are exempt"
    assert _fetch("https://robots-host.example/private/data") == {"text": "hello"}, "the flag ends with its block"
    assert not any("//robots-host.example/private" in u for u in wire.calls[:3]), "the refused reads never left"


def test_a_kept_card_from_the_web_refreshes_under_robots_and_a_library_card_does_not(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(ni, "_dispatch_source", lambda *a: seen.append(getattr(ni_polite.SHARED._local, "robots", False)))
    web = {"recipe": {"sources": [{"kind": "search", "ref": "https://x.example/a.json", "host": "x.example", "why": "w"}]}}
    lib = {"recipe": {"sources": [{"kind": "library", "ref": "https://x.example/a.json", "host": "x.example", "why": "w"}]}}
    for spec in (web, lib, {}):
        ni._fetch_source(spec, "i1", None, None, None, object(), None)
    assert seen == [True, False, False]
    assert ni_polite.robots_for(web) and not ni_polite.robots_for(lib) and not ni_polite.robots_for({"recipe": 3})


def test_research_reads_a_search_result_only_where_robots_allows_and_a_library_row_as_before(wire) -> None:
    from test_ni_research_tools import _ctx

    from smartbrain_3000 import ni_research_flow
    from smartbrain_3000.ni_research import tools
    _robots_host(wire)
    blocked, open_ = "https://robots-host.example/private/q.json", "https://robots-host.example/public/q.json"
    ctx = _ctx(None, allowed_origins=ni_research_flow.ORIGINS_WEB)
    ctx.record(blocked, "search")
    ctx.record(open_, "search")
    assert tools.fetch_sample(ctx, blocked)["error"] == "robots"
    assert tools.fetch_sample(ctx, open_)["ok"]
    lib = _ctx(None, allowed_origins=ni_research_flow.ORIGINS_LIBRARY)
    lib.record(blocked, "library")
    assert tools.fetch_sample(lib, blocked)["ok"], "a Library row is a vetted API, read as before"
    assert blocked not in wire.calls[: wire.calls.index(open_)], "the refused read never reached the host"


def test_robots_missing_allows_and_unavailable_disallows_for_a_while(wire) -> None:
    assert _page("https://no-robots.example/a")["status"] == 200, "a 404 robots.txt means no rules"
    wire.answers["https://down-robots.example/robots.txt"] = netguard.FetchError(
        "upstream returned HTTP 500", status=500)
    with pytest.raises(netguard.FetchError) as no:
        _page("https://down-robots.example/a")
    assert no.value.kind == "robots"
    wire.now[0] += ni_polite.ROBOTS_FAIL_TTL_S + 1
    wire.answers.pop("https://down-robots.example/robots.txt")
    assert _page("https://down-robots.example/a")["status"] == 200


def test_engine_maps_politeness_refusals_to_honest_statuses() -> None:
    busy = ni._fetch_error(netguard.FetchError("x", kind="host_busy"))
    assert busy.kind == "host_busy" and busy.kind in ni.CAPACITY_KINDS
    robots = ni._fetch_error(netguard.FetchError("x", kind="robots"))
    assert robots.kind == "fetch_failed" and "robots" in str(robots)
    assert ni._fetch_error(netguard.FetchError("x")).kind == "fetch_failed"


def test_netguard_is_untouched_by_default(wire) -> None:
    """Outside an NI fetch (search, vault, feeds) there is no budget, cache or port rule."""
    assert netguard.active_politeness() is None
    for _ in range(ni_polite.HOST_BURST + 3):
        assert netguard.safe_fetch_json("https://plain-host.example:9000/x") == {"text": "hello"}
    assert wire.count("plain-host.example:9000") == ni_polite.HOST_BURST + 3


@pytest.mark.parametrize("path", ["json", "csv", "sniff", "research"])
def test_every_ni_fetch_path_is_engaged_once_at_the_shared_chokepoint(wire, monkeypatch, path) -> None:
    """json, textual, sniffed and research fetches all run politeness-engaged (the thread-local object is
    installed inside ``ni_fetch._get``, once per request) and the engagement does not leak afterwards."""
    from smartbrain_3000 import ni_fetch
    from smartbrain_3000.ni_research import tools as research_tools
    from smartbrain_3000.ni_research.ctx import ResearchCtx

    url = "https://shared.example.org/data"
    csv = {"final_url": url, "status": 200, "content_type": "text/csv", "content": b"a,b\n1,2\n", "text": "a,b\n1,2\n"}
    seen: list[object] = []
    real = netguard.active_politeness
    monkeypatch.setattr(netguard, "active_politeness", lambda: seen.append(real()) or real())
    if path in ("csv", "research"):
        wire.answers[url] = csv
    if path == "json":
        ni_fetch.fetch_parsed(url, "json")
    elif path == "csv":
        ni_fetch.fetch_parsed(url, "csv")
    elif path == "sniff":
        ni_fetch.sniff_fetch(url)
    else:
        ctx = ResearchCtx(request="synthetic", question={}, library=None,
                          now=datetime(2026, 10, 21, tzinfo=UTC))
        research_tools._fetch_and_digest(ctx, url, "csv")
    assert [u for u in wire.calls if not u.endswith("/robots.txt")] == [url], "one request, through the shared path"
    assert seen and all(p is ni_polite.SHARED for p in seen), "the request ran politeness-engaged"
    monkeypatch.undo()
    assert netguard.active_politeness() is None, "engagement does not leak past the fetch"
