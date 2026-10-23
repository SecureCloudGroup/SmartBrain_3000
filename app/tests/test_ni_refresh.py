"""S9: refresh upgrades for recipe cards. Constraint re-check on every run (``constraint_drift``), and a
drifted or dead source parking a re-research proposal the user approves.

No model and no network: the researcher is a scripted seam (``ni_research_flow.set_research_chat``) and the fetch
layer is the S4b fake. Fetch dedupe across cards on one URL shipped in S5a (``test_ni_politeness``) and is only
confirmed here.
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_ni_l2 import _unlock, client  # noqa: F401  (client is a fixture)
from test_ni_research_loop import Script, call, reply, text_reply
from test_ni_research_tools import FakeLibrary, Net, _ctx, _draft, _row

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import (
    gateway,
    netguard,
    ni,
    ni_flow,
    ni_polite,
    ni_refresh,
    ni_research_flow,
)
from smartbrain_3000.ni_research import tools
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore, gen_master_key

URL = "https://data.example.org/q.json"
ALT = "https://alt.example.org/q.json"
ASK = "earthquakes above 4"
GOOD = {"title": "Quakes", "mag": [4.5, 5.2]}
DRIFTED = {"title": "Quakes", "mag": [1.2, 5.2]}  # the feed now carries small quakes the ask excluded
ALT_GOOD = {"title": "Alt quakes", "mag": [4.9, 6.0]}
CLOUD = "anthropic/claude-test"
PATHS = {"title": "title", "mag": "mag"}


@pytest.fixture(autouse=True)
def _v2(monkeypatch):
    """The v2 flow with research on auto, a Library that offers the card's own source and an alternative."""
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v2")
    monkeypatch.setenv("SMARTBRAIN_NI_RESEARCH", "auto")
    monkeypatch.setattr(gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: FakeLibrary([_row(URL), _row(ALT)]))
    yield
    ni_research_flow.set_research_chat(None)


def _new_store() -> tuple[ni.NIStore, duckdb.DuckDBPyConnection, bytes]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return ni.NIStore(conn, key), conn, key


def _app(conn, key: bytes):
    return SimpleNamespace(state=SimpleNamespace(master_key=key, db=SimpleNamespace(cursor=conn.cursor)))


def _recipe_spec(monkeypatch, tier: str = "curated", consent: str | None = CLOUD) -> dict:
    """What the researcher lands for ``ASK`` on ``URL``: the real propose_card output, stamped like the flow."""
    Net(monkeypatch, {URL: GOOD})
    ctx = _ctx(FakeLibrary([_row(URL)]), allowed_origins=frozenset({"library"}))
    ctx.request = ASK
    assert tools.library_search(ctx, "quakes")["ok"] and tools.fetch_sample(ctx, URL)["ok"]
    got = tools.propose_card(ctx, _draft(URL, PATHS))
    assert got["ok"], got
    spec = ni_research_flow._stamp(got["proposal"]["spec"], ASK)
    spec["recipe"]["tier"] = tier
    if consent:
        spec["_model_consent"] = consent
    return spec


def _card(store, spec: dict, state: str = "live") -> str:
    iid = store.add_item(spec, {"title": "Quakes", "mag": [4.5]})
    store.set_state(iid, state)
    return iid


def _run(store, conn, key, iid) -> None:
    ni.run_item(store, iid, gateway_mod=SimpleNamespace(), secrets_store=SecretStore(conn, key),
                schedules_store=ScheduleStore(conn, key))


def _fail_run(store, conn, key, iid, kind: str) -> None:
    with pytest.raises(ni.NIError) as exc:
        _run(store, conn, key, iid)
    assert exc.value.kind == kind


def _seed(monkeypatch, tier: str = "curated", consent: str | None = CLOUD):
    store, conn, key = _new_store()
    iid = _card(store, _recipe_spec(monkeypatch, tier, consent))
    return store, conn, key, iid


def _drift(monkeypatch, store, conn, key, iid, times: int = 1) -> None:
    """A good run (the contract is captured), then the source drifts ``times`` runs in a row."""
    net = Net(monkeypatch, {URL: GOOD, ALT: ALT_GOOD})
    _run(store, conn, key, iid)
    net.bodies[URL] = DRIFTED
    for _ in range(times):
        _fail_run(store, conn, key, iid, ni.CONSTRAINT_DRIFT)


def _kill(monkeypatch, store, conn, key, iid, times: int = 3) -> None:
    net = Net(monkeypatch, {URL: GOOD, ALT: ALT_GOOD})
    _run(store, conn, key, iid)
    net.fail[URL] = netguard.FetchError("gone")
    for _ in range(times):
        _fail_run(store, conn, key, iid, "fetch_failed")


def _researcher(*, alt_body=ALT_GOOD, text_only: bool = False, consented: bool = True) -> Script:
    """The scripted researcher: finds the alternative Library source and proposes a card for it."""
    draft = _draft(ALT, {"title": "title", "mag": "mag"})
    script = (Script(*[text_reply("still looking")] * 3) if text_only else
              Script(reply(call("library_search", query="quakes")), reply(call("fetch_sample", url=ALT)),
                     reply(call("try_recipe", draft_spec=draft)), reply(call("propose_card", draft_spec=draft))))
    ni_research_flow.set_research_chat(lambda model, messages, tools_: script(messages, tools_), cloud=CLOUD,
                                       consented=consented)
    return script


def _work(store, conn, key, iid) -> bool:
    """What the scheduler does after a tick: mark the candidate, hand it to the L2 worker (run inline)."""
    cands: list[str] = []
    ni._collect_l2_candidate(store, iid, cands)
    if cands:
        ni._run_l2_worker(_app(conn, key), cands)
    return bool(cands)


def _proposal(store, iid) -> dict | None:
    return store.get_item(iid)["spec"].get("_l2_proposal")


def _age_marker(store, iid, hours: float) -> None:
    """Move the sealed attempt marker ``hours`` into the past (the clock for the day wait and the backoff)."""
    marker = dict(store.get_item(iid)["spec"]["_research_last_attempt"])
    marker["at"] = (datetime.fromisoformat(marker["at"]) - timedelta(hours=hours)).isoformat()
    assert store.set_research_attempt(iid, marker)


def _flood_runs(store, iid, n: int = 60) -> None:
    """Enough telemetry rows to evict the whole 50-row ring."""
    for _ in range(n):
        store.record_run(iid, "ok", duration_ms=1, error=None, contract_ok=None)
    assert ni_refresh.RUN_STARTED not in _statuses(store, iid)


def _statuses(store, iid) -> list[str]:
    return [r["status"] for r in store.list_runs(iid)]


# --- 1. the constraint re-check on every run -----------------------------------------------------------------

def test_a_source_that_drifts_scope_fails_the_run_loudly_and_last_good_keeps_showing(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch)
    net = Net(monkeypatch, {URL: GOOD, ALT: ALT_GOOD})
    _run(store, conn, key, iid)
    assert store.get_item(iid)["state"] == "live"
    good = store.read_snapshot(iid, "last_good")
    net.bodies[URL] = DRIFTED
    _fail_run(store, conn, key, iid, ni.CONSTRAINT_DRIFT)
    item = store.get_item(iid)
    assert item["last_status"] == "constraint_drift" and item["state"] == "degraded"
    assert item["consecutive_failures"] == 1
    assert store.read_snapshot(iid, "last_good") == good, "never replaced by the drifted reading"
    assert store.read_snapshot(iid, "latest")["ok"] is False
    assert store.list_runs(iid)[0]["status"] == "error" and store.list_runs(iid)[0]["error"] == "constraint_drift"
    assert ni.CONSTRAINT_DRIFT not in ni.CAPACITY_KINDS, "a drift is a failure, not capacity"
    assert ni.CONSTRAINT_DRIFT not in ni._L1_SPEC_SHAPE_CLASSES, "a local repair cannot move a source back"
    net.bodies[URL] = GOOD
    _run(store, conn, key, iid)
    assert store.get_item(iid)["state"] == "live", "a source that returns to scope recovers by itself"


def test_data_where_none_was_expected_and_nothing_where_data_was_both_drift(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch)
    net = Net(monkeypatch, {URL: GOOD, ALT: ALT_GOOD})
    _run(store, conn, key, iid)
    net.bodies[URL] = {"title": "", "mag": []}
    _fail_run(store, conn, key, iid, ni.CONSTRAINT_DRIFT)
    assert store.get_item(iid)["last_status"] == "constraint_drift"
    reasons = ni_refresh.drift_reasons(store.get_item(iid)["spec"], {"title": "", "mag": []}, datetime.now(UTC))
    assert [r.split(":")[0] for r in reasons] == ["empty"]


def test_only_constraints_that_passed_at_keep_are_rechecked(monkeypatch) -> None:
    spec = _recipe_spec(monkeypatch)
    out = {"title": "Quakes", "mag": [1.2]}
    assert [r.split(":")[0] for r in ni_refresh.drift_reasons(spec, out, datetime.now(UTC))] == ["threshold"]
    kept_failing = copy.deepcopy(spec)
    for check in kept_failing["recipe"]["verification"]["checks"]:
        check["ok"] = check["check"] != "threshold"
    kept_failing["recipe"]["verification"]["verdict"] = "partial"
    assert ni_refresh.drift_reasons(kept_failing, out, datetime.now(UTC)) == [], "it did not pass at Keep"
    assert ni_refresh.RECHECKED == {"place", "window", "threshold", "compare", "empty"}


def test_a_spec_without_a_recipe_never_touches_the_recheck(monkeypatch) -> None:
    store, conn, key = _new_store()
    spec = _recipe_spec(monkeypatch)
    del spec["recipe"]
    iid = _card(store, spec)
    monkeypatch.setattr(ni_refresh, "drift_reasons", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    net = Net(monkeypatch, {URL: DRIFTED})
    _run(store, conn, key, iid)  # the drifted reading is simply the card's data, exactly as before S9
    assert store.get_item(iid)["state"] == "live" and store.get_item(iid)["last_status"] == "ok"
    assert ni._constraint_drift(spec, {"mag": [1]}, datetime.now(UTC)) == ""
    net.fail[URL] = netguard.FetchError("gone")
    cands: list[str] = []
    ni._collect_l2_candidate(store, iid, cands)
    assert cands == [] and _proposal(store, iid) is None


def test_two_recipe_cards_on_one_url_share_one_request_in_a_tick(monkeypatch) -> None:
    """Fetch dedupe shipped in S5a (``ni_polite``); confirmed, not rebuilt, with the re-check riding on it."""
    ni_polite.SHARED.reset()
    seen: list[str] = []

    def wire(url, allowed_ct, max_bytes, *args, **kwargs):
        seen.append(url)
        if url.endswith("/robots.txt"):
            raise netguard.FetchError("upstream returned HTTP 404", status=404)
        return {"final_url": url, "status": 200, "content_type": "application/json",
                "content": json.dumps(GOOD).encode()}

    real_json, real_text = netguard.safe_fetch_json, netguard.safe_fetch_text
    store, conn, key = _new_store()
    ids = [_card(store, _recipe_spec(monkeypatch)) for _ in range(2)]  # builds with the S4b fake net
    monkeypatch.setattr(netguard, "safe_fetch_json", real_json)  # then the real chokepoint over a fake wire
    monkeypatch.setattr(netguard, "safe_fetch_text", real_text)
    monkeypatch.setattr(netguard, "_guarded_get_once", wire)
    conn.execute("UPDATE ni_items SET last_checked = NULL;")
    try:
        assert ni.tick(_app(conn, key))["checked"] == 2
    finally:
        ni_polite.SHARED.reset()
        ni_polite.set_dedupe(False)
    assert [u for u in seen if not u.endswith("/robots.txt")] == [URL]
    assert all(store.get_item(i)["last_status"] == "ok" for i in ids)


# --- 2. a drifted or dead source parks a proposal ------------------------------------------------------------

def test_a_drifting_source_parks_a_proposal_that_is_never_applied(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    before = store.get_item(iid)
    script = _researcher()
    assert _work(store, conn, key, iid)
    prop = _proposal(store, iid)
    assert prop["origin"] == "research" and prop["reason"] == "constraint_drift" and "stages" not in prop
    assert prop["spec"]["source"]["url"] == ALT and prop["spec"]["recipe"]["provenance"]["author"] == "researcher"
    assert prop["spec"]["_model_consent"] == CLOUD and prop["spec"]["_built_from"]["path"] == "research"
    assert prop["model"] == CLOUD
    offered = [m for s in script.seen for m in s["messages"] if m["role"] == "tool"]
    assert URL not in str(offered), "the source the card has now is never offered back"
    after = store.get_item(iid)
    assert after["spec"]["source"]["url"] == URL and after["spec_rev"] == before["spec_rev"], "not applied"
    assert after["state"] == "degraded" and after["last_status"] == "constraint_drift"
    assert _statuses(store, iid)[:2] == [ni_refresh.RUN_PROPOSED, ni_refresh.RUN_STARTED]


def test_a_dead_source_parks_a_proposal_once_the_backoff_ladder_has_run(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="curated")
    _kill(monkeypatch, store, conn, key, iid, times=2)
    assert store.get_item(iid)["state"] == "degraded"
    _researcher()
    assert not _work(store, conn, key, iid), "curated waits for the ladder (failing), not the first failures"
    _fail_run(store, conn, key, iid, "fetch_failed")
    assert store.get_item(iid)["state"] == "failing"
    assert _work(store, conn, key, iid)
    prop = _proposal(store, iid)
    assert prop["reason"] == "source_dead" and prop["spec"]["source"]["url"] == ALT
    assert store.get_item(iid)["spec"]["source"]["url"] == URL


@pytest.mark.parametrize(("tier", "reason", "state", "met"), [
    ("curated", "constraint_drift", "degraded", False), ("curated", "constraint_drift", "failing", True),
    ("curated", "source_dead", "degraded", False), ("curated", "source_dead", "failing", True),
    ("verified_endpoint", "constraint_drift", "degraded", True), ("verified_endpoint", "source_dead", "degraded", False),
    ("verified_endpoint", "source_dead", "failing", True), ("page_reading", "constraint_drift", "degraded", True),
    ("page_reading", "source_dead", "degraded", False), ("page_reading", "source_dead", "failing", True),
    ("page_reading", "constraint_drift", "live", False), ("page_reading", "constraint_drift", "broken", False)])
def test_the_tier_table_sets_how_readily_a_failure_asks_for_research(tier, reason, state, met) -> None:
    status = {"constraint_drift": ni.CONSTRAINT_DRIFT, "source_dead": "fetch_failed"}[reason]
    item = {"last_status": status, "state": state, "first_failure_at": datetime.now(UTC)}
    assert ni_refresh.reason_of(item) == reason
    assert ni_refresh.trigger_met(item, {"recipe": {"tier": tier}}) is met
    assert ni_refresh.trigger_met(item, {"recipe": {"tier": tier}, "_l1_trial": {"rev_before": 1}}) is False
    assert set(ni_refresh.TRIGGER) == ni_recipe_tiers()
    assert ni_refresh.reason_of({"last_status": "extract_miss"}) is None


def ni_recipe_tiers() -> set:
    from smartbrain_3000 import ni_recipe
    return set(ni_recipe.QUALITY_TIERS)


# --- 3. consent, off, rate limit -----------------------------------------------------------------------------

def test_research_off_or_unconsented_leaves_the_card_failing_with_nothing_sent(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint", consent=None)
    _drift(monkeypatch, store, conn, key, iid)
    script = _researcher(consented=False)  # a cloud route exists, but this card never allowed it
    assert not _work(store, conn, key, iid) and script.seen == []
    assert _proposal(store, iid) is None and ni_refresh.RUN_STARTED not in _statuses(store, iid)
    monkeypatch.setenv("SMARTBRAIN_NI_RESEARCH", "off")
    store2, conn2, key2, iid2 = _seed(monkeypatch, tier="verified_endpoint", consent=CLOUD)
    _drift(monkeypatch, store2, conn2, key2, iid2)
    script2 = _researcher()
    assert not _work(store2, conn2, key2, iid2) and script2.seen == []
    item = store2.get_item(iid2)
    assert _proposal(store2, iid2) is None and item["last_status"] == "constraint_drift" and item["state"] == "degraded"


def test_a_library_scope_consent_still_re_researches_with_the_web_switch_on(monkeypatch) -> None:
    """Re-research reads the Library only, so the card's own consent covers it whatever its scope (S5b)."""
    monkeypatch.delenv("SMARTBRAIN_NI_RESEARCH_WEB", raising=False)
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint", consent=CLOUD)  # no _model_consent_web
    _drift(monkeypatch, store, conn, key, iid)
    script = _researcher(consented=False)  # the card's sealed consent is the only consent
    assert _work(store, conn, key, iid) and script.seen, "the researcher ran"
    assert all("web_search" not in {t["function"]["name"] for t in s["tools"]} for s in script.seen if s["tools"])
    assert _proposal(store, iid)["spec"]["source"]["url"] == ALT


def test_the_v1_flow_never_re_researches(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    script = _researcher()
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    assert not _work(store, conn, key, iid) and script.seen == []


def test_a_decline_is_honest_and_the_day_limit_holds(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    script = _researcher(text_only=True)  # the model never proposes
    assert _work(store, conn, key, iid)
    assert _proposal(store, iid) is None
    assert _statuses(store, iid)[:2] == [ni_refresh.RUN_DECLINED, ni_refresh.RUN_STARTED]
    seen = len(script.seen)
    assert seen >= 1
    assert not _work(store, conn, key, iid), "at most one re-research per card per day"
    assert len(script.seen) == seen
    _age_marker(store, iid, 25)
    _researcher()
    assert _work(store, conn, key, iid), "a day later it may try again"
    assert _proposal(store, iid)["spec"]["source"]["url"] == ALT


def test_a_crash_mid_run_still_burns_the_attempt(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    ni_research_flow.set_research_chat(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")), cloud=CLOUD, consented=True)
    assert _work(store, conn, key, iid)
    assert ni_refresh.RUN_STARTED in _statuses(store, iid) and _proposal(store, iid) is None
    assert ni_refresh.RUN_FAULT in _statuses(store, iid) and ni_refresh.RUN_DECLINED not in _statuses(store, iid), \
        "a fault is not recorded as a decline"
    assert store.get_item(iid)["spec"]["_research_last_attempt"]["declines"] == 0, "and it starts no back-off streak"
    assert not _work(store, conn, key, iid)


def test_one_proposal_per_streak_even_after_it_is_dismissed(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    _researcher()
    assert _work(store, conn, key, iid)
    store.clear_l2_proposal(iid)  # Dismiss
    _age_marker(store, iid, 48)
    _researcher()
    assert not _work(store, conn, key, iid), "the user said no to this streak"
    _fail_run(store, conn, key, iid, ni.CONSTRAINT_DRIFT)
    assert not _work(store, conn, key, iid)
    # the streak resets on a clean run; a later drift is a new streak
    Net(monkeypatch, {URL: GOOD})
    _run(store, conn, key, iid)
    assert store.get_item(iid)["state"] == "live"


def test_a_pending_proposal_blocks_another_and_a_stage_repair_is_not_offered_for_drift(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    item = store.get_item(iid)
    item["state"] = "failing"
    spec = dict(item["spec"], repair_policy={"l1": False, "l2_frontier": True})
    assert ni._l2_eligible(item, spec) is False, "a stage patch cannot fix scope drift"
    _researcher()
    assert _work(store, conn, key, iid)
    assert not _work(store, conn, key, iid)


# --- 4. approval is the only way a proposal changes the card --------------------------------------------------

def test_the_tick_alone_never_applies_a_proposal(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    _researcher()
    assert _work(store, conn, key, iid)
    rev = store.get_item(iid)["spec_rev"]
    conn.execute("UPDATE ni_items SET last_checked = NULL;")
    ni.tick(_app(conn, key))
    after = store.get_item(iid)
    assert after["spec"]["source"]["url"] == URL and after["spec_rev"] == rev
    assert isinstance(after["spec"].get("_l2_proposal"), dict) and after["spec"].get("_l1_trial") is None


def test_the_proposal_shape_is_closed(monkeypatch) -> None:
    spec = _recipe_spec(monkeypatch)
    ok = {"spec": spec, "origin": "research", "reason": "source_dead", "created_at": datetime.now(UTC).isoformat(),
          "model": CLOUD}
    ni._validate_l2_proposal(ok)
    for bad in ({**ok, "stages": {"extract": {"a": "b"}}}, {**ok, "reason": "because"}, {**ok, "origin": "user"},
                {**ok, "extra": 1}, {k: v for k, v in ok.items() if k != "model"},
                {**ok, "spec": {**spec, "_l2_proposal": ok}}):
        with pytest.raises(ValueError, match="_l2_proposal"):
            ni._validate_l2_proposal(bad)
    ni._validate_l2_proposal({"stages": {"extract": {"a": "b"}}, "created_at": ok["created_at"], "model": "m"})


def test_apply_swaps_the_card_under_the_repair_trial_and_the_old_contract_is_not_carried(client: TestClient, monkeypatch) -> None:  # noqa: F811
    _unlock(client)
    store = client.app.state.ni
    spec = _recipe_spec(monkeypatch, tier="verified_endpoint")
    iid = _card(store, spec)
    current = store.get_item(iid)["spec"]
    current["_c2_ok"], current["contract"] = True, {"shape": {"title": "string"}}
    store.conn.execute("UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;", [*store._seal_item(iid, current), iid])
    new = ni_research_flow._stamp(copy.deepcopy(spec), ASK)
    new["source"] = {"type": "http_json", "url": ALT}
    proposal = {"spec": new, "origin": "research", "reason": "source_dead",
                "created_at": datetime.now(UTC).isoformat(), "model": CLOUD}
    store.conn.execute("UPDATE ni_items SET state='degraded', consecutive_failures = 1, first_failure_at = now() WHERE id = ?;", [iid])
    assert store.set_l2_proposal(iid, proposal, expected_rev=store.get_item(iid)["spec_rev"], states=("degraded", "failing"))
    assert client.get("/api/ni/board").json()["items"][0]["l2_proposal"] is True
    rev = store.get_item(iid)["spec_rev"]
    r = client.post(f"/api/ni/items/{iid}/l2-proposal/apply")
    assert r.status_code == 200, r.text
    after = store.get_item(iid)
    assert after["spec"]["source"]["url"] == ALT and after["spec_rev"] == rev + 1
    assert after["spec"]["contract"] is None and "_c2_ok" not in after["spec"] and "_l2_proposal" not in after["spec"]
    assert after["spec"]["_l1_trial"]["origin"] == "repair_l2", "the same trial and auto-revert as any repair"
    Net(monkeypatch, {ALT: ALT_GOOD, URL: GOOD})
    ni.run_item(store, iid, gateway_mod=SimpleNamespace(), secrets_store=SecretStore(store.conn, client.app.state.master_key),
                schedules_store=None)
    assert store.get_item(iid)["state"] == "live"


# --- 5. the honest status vocabulary --------------------------------------------------------------------------

def test_constraint_drift_is_a_status_beside_the_other_failures() -> None:
    assert ni.CONSTRAINT_DRIFT == "constraint_drift" and ni.CONSTRAINT_DRIFT not in ni.CAPACITY_KINDS
    assert ni.RESEARCH_REASONS == {"constraint_drift", "source_dead"}
    assert {ni_refresh.RUN_STARTED, ni_refresh.RUN_PROPOSED, ni_refresh.RUN_DECLINED} <= {
        "repair_research_started", "repair_research_proposed", "repair_research_declined"}
    now = datetime.now(UTC)
    assert ni_refresh.may_research(None, None, now) is True
    old = {"at": (now - timedelta(hours=23)).isoformat(), "streak": None, "outcome": "started", "declines": 0}
    assert ni_refresh.may_research(old, None, now) is False
    assert ni_refresh.may_research({**old, "at": (now - timedelta(hours=25)).isoformat()}, None, now) is True


# --- 6. review fixes: the durable marker, the daily budget, the decline backoff ---------------------------------

def test_the_attempt_bounds_survive_the_telemetry_ring_being_pruned(monkeypatch) -> None:
    """M1: 60 later run rows evict the started row from ni_runs; the sealed marker still refuses a second run."""
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    script = _researcher(text_only=True)
    assert _work(store, conn, key, iid)
    seen = len(script.seen)
    _flood_runs(store, iid)
    assert not _work(store, conn, key, iid) and len(script.seen) == seen, "still inside the day"
    assert store.get_item(iid)["spec"]["_research_last_attempt"]["outcome"] == "declined"


def test_a_dismissed_proposal_is_not_asked_again_after_the_ring_is_pruned(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    _researcher()
    assert _work(store, conn, key, iid)
    assert store.get_item(iid)["spec"]["_research_last_attempt"]["outcome"] == "proposed"
    store.clear_l2_proposal(iid)  # Dismiss
    _flood_runs(store, iid)
    _age_marker(store, iid, 72)
    script = _researcher()
    assert not _work(store, conn, key, iid) and script.seen == [], "the user said no to this streak"


def test_the_marker_is_carried_by_revert_apply_and_update_and_never_exported(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    _researcher()
    assert _work(store, conn, key, iid)
    marker = store.get_item(iid)["spec"]["_research_last_attempt"]
    proposal = _proposal(store, iid)
    store.clear_l2_proposal(iid)
    rev = store.get_item(iid)["spec_rev"]
    assert store.apply_repair(iid, proposal["spec"], origin="repair_l2", expected_rev=rev, replace_spec=True)
    assert store.get_item(iid)["spec"]["_research_last_attempt"] == marker, "apply_repair"
    ni._revert_l1_trial_if_active(store, iid)
    reverted = store.get_item(iid)["spec"]
    assert reverted["source"]["url"] == URL and reverted["_research_last_attempt"] == marker, "revert"
    edited = dict(reverted)
    edited.pop("_research_last_attempt")
    store.update_spec(iid, edited)
    assert store.get_item(iid)["spec"]["_research_last_attempt"] == marker, "update_spec"
    from smartbrain_3000 import ni_library, ni_routes
    assert "_research_last_attempt" in ni_routes._EXPORT_STRIP_KEYS
    assert "_research_last_attempt" in ni_library._TEMPLATE_STRIP_KEYS
    template = {"spec_template": {**reverted}}
    with pytest.raises(ni_library.LibraryError, match="_research_last_attempt"):
        ni_library._validate_template_spec_and_preview(template, "t")


def test_the_marker_shape_is_closed(monkeypatch) -> None:
    spec = _recipe_spec(monkeypatch)
    good = {"at": datetime.now(UTC).isoformat(), "streak": None, "outcome": "started", "declines": 0}
    ni.validate_spec({**spec, "_research_last_attempt": good})
    for bad in ({**good, "extra": 1}, {**good, "outcome": "x"}, {**good, "at": "yesterday"}, {**good, "declines": -1},
                {**good, "declines": True}, "now"):
        with pytest.raises(ValueError, match="_research_last_attempt"):
            ni.validate_spec({**spec, "_research_last_attempt": bad})


def test_the_daily_budget_caps_background_re_research_across_cards(monkeypatch) -> None:
    """M2: a durable per-UTC-day count over all cards, checked before anything is sent; 0 turns it off."""
    monkeypatch.setenv("SMARTBRAIN_NI_RERESEARCH_DAILY", "2")
    store, conn, key = _new_store()
    ids = []
    for _ in range(3):
        iid = _card(store, _recipe_spec(monkeypatch, tier="verified_endpoint"))
        ids.append(iid)
        _drift(monkeypatch, store, conn, key, iid)
    script = _researcher(text_only=True)
    assert [_work(store, conn, key, iid) for iid in ids] == [True, True, False]
    seen = len(script.seen)
    assert not _work(store, conn, key, ids[2]) and len(script.seen) == seen, "nothing sent past the ceiling"
    # the count is durable and per UTC day: it resets when the day turns
    conn.execute("UPDATE meta SET value = '2000-01-01:2' WHERE key = ?;", [ni_refresh.META_COUNT])
    assert _work(store, conn, key, ids[2])
    monkeypatch.setenv("SMARTBRAIN_NI_RERESEARCH_DAILY", "0")
    assert ni_refresh.budget_left(conn, datetime.now(UTC)) is False
    monkeypatch.delenv("SMARTBRAIN_NI_RERESEARCH_DAILY")
    dbmod.meta_set(conn, ni_refresh.META_DAILY, "junk")
    assert ni_refresh.daily_limit(conn) == ni_refresh.DEFAULT_DAILY == 10
    dbmod.meta_set(conn, ni_refresh.META_DAILY, "3")
    assert ni_refresh.daily_limit(conn) == 3


def test_a_decline_backs_off_one_two_four_then_seven_days_within_a_streak(monkeypatch) -> None:
    store, conn, key, iid = _seed(monkeypatch, tier="verified_endpoint")
    _drift(monkeypatch, store, conn, key, iid)
    monkeypatch.setenv("SMARTBRAIN_NI_RERESEARCH_DAILY", "100")
    assert [ni_refresh.decline_wait(n).days for n in (1, 2, 3, 4, 5, 9)] == [1, 2, 4, 7, 7, 7]
    for n, wait_days in ((1, 1), (2, 2), (3, 4), (4, 7)):
        script = _researcher(text_only=True)
        assert _work(store, conn, key, iid), f"attempt {n}"
        assert store.get_item(iid)["spec"]["_research_last_attempt"]["declines"] == n
        seen = len(script.seen)
        _age_marker(store, iid, wait_days * 24 - 1)
        assert not _work(store, conn, key, iid) and len(script.seen) == seen, f"still waiting after decline {n}"
        _age_marker(store, iid, 2)
    seen_before = len(_researcher(text_only=True).seen)
    assert _work(store, conn, key, iid), "after the cap's wait it may try again"
    assert seen_before == 0


def test_a_new_streak_resets_the_decline_count_but_not_the_day(monkeypatch) -> None:
    now = datetime.now(UTC)
    first = now - timedelta(hours=1)
    marker = ni_refresh.next_marker(None, first, now - timedelta(days=3), "started")
    marker = ni_refresh.next_marker(marker, first, now - timedelta(days=3), "declined")
    assert marker["declines"] == 1
    later = ni_refresh.next_marker(marker, now, now, "started")
    assert later["declines"] == 0 and later["streak"] != marker["streak"]
    assert ni_refresh.may_research(later, now, now) is False, "a new streak still waits out the day"
    assert ni_refresh.may_research(later, now, now + timedelta(hours=25)) is True


def test_a_replacement_re_check_uses_the_scope_recorded_at_keep(monkeypatch) -> None:
    """L2: the Library scope the place check had at Keep rides on the recipe's source and is passed back."""
    seen: list[dict] = []
    real = tools.checks_for

    def spy(ctx, outputs, url, info):
        seen.append(info)
        return real(ctx, outputs, url, info)

    monkeypatch.setattr(tools, "checks_for", spy)
    spec = _recipe_spec(monkeypatch)
    assert spec["recipe"]["sources"][0].get("scope") == "global", "recorded at Keep for a new card"
    ni_refresh.drift_reasons(spec, GOOD, datetime.now(UTC))
    assert seen[-1] == {"scope": "global"}
    old = copy.deepcopy(spec)
    old["recipe"]["sources"][0].pop("scope")
    ni.validate_spec(old)
    ni_refresh.drift_reasons(old, GOOD, datetime.now(UTC))
    assert seen[-1] == {}, "a card kept before the scope was recorded reads as before"
    bad = copy.deepcopy(spec)
    bad["recipe"]["sources"][0]["scope"] = "x" * 21
    with pytest.raises(ValueError, match="scope"):
        ni.validate_spec(bad)


def test_a_stated_constraint_with_no_passing_record_is_not_rechecked(monkeypatch) -> None:
    """L4: only checks with a passing verification record re-run."""
    spec = _recipe_spec(monkeypatch)
    spec["recipe"]["constraints"].append({"check": "compare", "value": None})
    spec["recipe"]["verification"]["checks"] = [c for c in spec["recipe"]["verification"]["checks"]
                                                if c["check"] != "compare"]
    assert "compare" not in ni_refresh._rechecked(spec["recipe"])
    assert ni_refresh._rechecked(spec["recipe"]) <= {c["check"] for c in spec["recipe"]["verification"]["checks"]
                                                     if c["ok"]}
