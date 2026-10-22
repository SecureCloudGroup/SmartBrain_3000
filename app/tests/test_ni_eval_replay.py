"""NI eval harness (plan S0): scoring unit tests + an offline replay of a small committed cassette.

``tools/ni-eval.py`` is loaded by path (it lives outside the package; the shipped image carries
``app/`` only, so everything skips cleanly when the tool is absent). The replay tests drive the
REAL ``ni_flow.run_flow`` with the recorded model replies and fetch responses served from
``tests/ni_eval/cassettes/ci`` (built by hand from ``fixtures/ni_flow`` — see docs/internal/ni-eval.md).
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_TOOL = _REPO / "tools" / "ni-eval.py"
_EVAL = pathlib.Path(__file__).parent / "ni_eval"

pytestmark = pytest.mark.skipif(not _TOOL.exists(), reason="ni-eval tool not shipped in the app image")


def _load():
    if not _TOOL.exists():
        return None
    spec = importlib.util.spec_from_file_location("_ni_eval_tool", _TOOL)
    assert spec is not None and spec.loader is not None, "spec load must succeed"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ni_eval_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


ne = _load()


def _load_e2e():
    spec = importlib.util.spec_from_file_location("_ni_live_e2e_tool", _REPO / "tools" / "ni-live-e2e.py")
    assert spec is not None and spec.loader is not None, "spec load must succeed"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _local_route(monkeypatch):
    from smartbrain_3000 import gateway
    monkeypatch.setattr(gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})


def _gold(**over) -> dict:
    base = {"id": "T1", "kind": "count", "ask": "x", "expected_outcome": "card", "forbidden_outcomes": [],
            "acceptable_sources": None, "human_verdict": None, "baseline": {},
            "constraints": {"places": [], "window": None, "threshold": None, "compare_n": None,
                            "units": None, "entities": []}}
    cons = {**base["constraints"], **over.pop("constraints", {})}
    return {**base, **over, "constraints": cons}


def _card(text: list[str], url: str = "https://a.example/x?min=4.0", **extra) -> dict:
    return {"outcome": "live", "scene_text": text, "preview": None, "source": "",
            "tapped": [{"title": "", "label": "", "url": url, "params": {}}], **extra}


# --- scoring: outcome classes ---------------------------------------------------------------------

@pytest.mark.parametrize("outcome,cls", [("links", "links"), ("source", "links"), ("needs-key", "links"),
                                         ("unsupported", "declined"), ("crash", "error"),
                                         ("failed", "error"), ("built-no-run", "error"),
                                         ("waiting-pick", "waiting")])
def test_non_card_outcomes_map_to_their_class(outcome: str, cls: str) -> None:
    res = ne.score_ask({"outcome": outcome}, _gold())
    assert res["cls"] == cls, res
    assert res["cls"] in ne.CLASSES, "class is one of the known classes"


def test_forbidden_or_declined_expectation_makes_a_card_wrong() -> None:
    assert ne.score_ask(_card(["7"]), _gold(expected_outcome="decline"))["cls"] == "wrong"
    assert ne.score_ask(_card(["7"]), _gold(forbidden_outcomes=["card"]))["cls"] == "wrong"


# --- scoring: constraint checks -------------------------------------------------------------------

def test_threshold_missing_from_the_request_is_wrong_and_present_is_right() -> None:
    gold = _gold(constraints={"threshold": 4.0})
    assert ne.score_ask(_card(["12"], url="https://q/2.5_day.geojson"), gold)["cls"] == "wrong"
    assert ne.score_ask(_card(["12"], url="https://q/q?minmagnitude=4"), gold)["cls"] == "right"


def test_compare_needs_every_entity_named_on_the_card() -> None:
    gold = _gold(kind="compare", constraints={"compare_n": 2, "entities": ["Denver", "Salt Lake City"]})
    assert ne.score_ask(_card(["Denver 61 F"]), gold)["cls"] == "wrong"
    assert ne.score_ask(_card(["Denver 61 F", "Salt Lake City 55 F"]), gold)["cls"] == "right"


def test_places_partial_when_only_some_are_named() -> None:
    gold = _gold(constraints={"places": ["Boise", "Nampa"]})
    res = ne.score_ask(_card(["Boise 68"]), gold)
    assert res["cls"] == "partial" and "1/2" in res["reason"], res


def test_a_source_outside_a_narrow_gold_list_needs_review_not_wrong() -> None:
    res = ne.score_ask(_card(["1"]), _gold(acceptable_sources=["other.example"]))
    assert (res["cls"], res["needs_review"], res["reason"]) == ("partial", True, ne.SOURCE_REVIEW), res
    assert ne.score_ask(_card(["1"]), _gold(acceptable_sources=["a.example"]))["cls"] == "right"
    failed = ne.score_ask(_card(["12"], url="https://a.example/q"),
                          _gold(acceptable_sources=["other.example"], constraints={"threshold": 4.0}))
    assert failed["cls"] == "wrong", "a failed constraint stays wrong whatever the source"


def test_only_the_built_source_counts_in_v1_and_v2_alike() -> None:
    tried = {"title": "", "label": "", "url": "https://tried.example/x", "params": {}}
    built = {"title": "", "label": "", "url": "https://built.example/x", "params": {}}
    gold = _gold(acceptable_sources=["tried.example"])
    v1 = {**_card(["1"]), "tapped": [tried, built], "source": "library: tried.example → built.example"}
    v2 = {**_card(["1"]), "tapped": [built]}
    for run in (v1, v2):  # a url that was tapped but not built from is no evidence for the card
        res = ne.score_ask(run, gold)
        assert res["checks"]["source"] is False and res["cls"] == "partial", res
    assert ne.score_ask(v1, _gold(acceptable_sources=["built.example"]))["cls"] == "right"
    page = {**_card(["1"]), "tapped": [tried], "from": {"kind": "web page", "host": "page.example", "title": ""}}
    assert ne.score_ask(page, _gold(acceptable_sources=["page.example"]))["cls"] == "right"


def test_every_row_failed_gets_the_same_label_in_v1_and_v2() -> None:
    e2e = _load_e2e()
    link = {"kind": "link", "host": "h.example", "title": "T", "url": "https://h.example/p"}
    lib = {"kind": "library", "host": "l.example", "title": "L", "url": "https://l.example/d"}
    assert e2e.stop_label([link])[0] == "links" and e2e.stop_label([link])[1][0]["host"] == "h.example"
    assert e2e.stop_label([lib, link])[0] == "waiting-pick", "a buildable row is still untried"
    assert e2e.stop_label([])[0] == "no-source"


def test_nothing_checkable_is_partial_and_flagged_for_review() -> None:
    res = ne.score_ask(_card(["1"]), _gold())
    assert (res["cls"], res["needs_review"]) == ("partial", True), res


def test_human_verdict_carries_only_for_the_same_card() -> None:
    run = _card(["Denver 61 F"])
    gold = _gold(human_verdict="wrong", baseline={"card_sha": ne._sha(ne.card_text(run))})
    assert ne.score_ask(run, gold)["cls"] == "wrong"
    changed = ne.score_ask(_card(["Denver 61 F", "Salt Lake City"]), gold)
    assert changed["cls"] == "partial" and changed["needs_review"], changed


# --- aggregation ----------------------------------------------------------------------------------

def test_aggregate_counts_per_kind_and_overall() -> None:
    scored = [{"kind": "a", "cls": "right"}, {"kind": "a", "cls": "wrong"},
              {"kind": "b", "cls": "links"}, {"kind": "b", "cls": "declined"}]
    table = ne.aggregate(scored)
    assert table["a"]["R"] == 0.5 and table["a"]["W"] == 0.5, table["a"]
    assert table["b"]["L"] == 1.0 and table["ALL"]["n"] == 4, table
    assert "ALL" in ne.format_table(table)


def test_needs_review_is_counted_next_to_wrong_overall_and_per_kind() -> None:
    scored = [{"kind": "a", "cls": "partial", "needs_review": True}, {"kind": "a", "cls": "right", "needs_review": False},
              {"kind": "b", "cls": "partial", "needs_review": True}, {"kind": "b", "cls": "wrong"}]
    table = ne.aggregate(scored)
    assert (table["a"]["needs_review"], table["b"]["needs_review"], table["ALL"]["needs_review"]) == (1, 1, 2)
    lines = ne.format_table(table).splitlines()
    assert lines[0].index("wrong") < lines[0].index("review") < lines[0].index("links")
    all_row = next(line for line in lines if line.startswith("ALL"))
    assert all_row.split()[4:6] == ["1", "2"], "wrong, then review"


def test_waiting_is_its_own_column_not_links() -> None:
    scored = [{"kind": "a", "cls": "waiting"}, {"kind": "a", "cls": "links"}, {"kind": "a", "cls": "right"}]
    table = ne.aggregate(scored)
    assert table["ALL"]["waiting"] == 1 and table["ALL"]["links"] == 1, table["ALL"]
    assert table["ALL"]["L"] == 1 / 3, "waiting does not count toward links"
    header = ne.format_table(table).splitlines()[0]
    assert "wait" in header and header.index("decl") < header.index("wait") < header.index("err")
    assert ne.score_ask({"outcome": "source"}, _gold())["cls"] == "links", "old source stays links"


def test_flow_flag_sets_the_env_for_the_run(monkeypatch) -> None:
    import argparse

    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "x")  # records the original so teardown restores it
    monkeypatch.delenv("SMARTBRAIN_NI_FLOW")
    ne._set_flow(argparse.Namespace(flow=None))
    assert "SMARTBRAIN_NI_FLOW" not in os.environ
    ne._set_flow(argparse.Namespace(flow="v2"))
    assert os.environ["SMARTBRAIN_NI_FLOW"] == "v2"
    with pytest.raises(SystemExit):  # argparse rejects anything but v1 / v2
        ne.main(["replay", "--cassette-dir", str(_EVAL), "--flow", "v3"])


# --- cassette mechanics ---------------------------------------------------------------------------

def test_redact_url_hides_secret_query_values() -> None:
    assert ne.redact_url("https://h/p?q=1&api_key=SECRET&x=2") == "https://h/p?q=1&api_key=REDACTED&x=2"
    assert ne.redact_url("https://h/p") == "https://h/p"


def test_record_then_replay_round_trips_fetch_model_and_failures(tmp_path) -> None:
    from smartbrain_3000 import netguard
    rec = ne.Session("R1", recording=True)

    def real_json(url, **kw):
        if "dead" in url:
            raise netguard.FetchError("HTTP 404", status=404, kind="http")
        return {"url": url}

    original = netguard.safe_fetch_json
    netguard.safe_fetch_json = real_json
    try:
        with ne.patched_netguard(rec):
            assert netguard.safe_fetch_json("https://ok/x") == {"url": "https://ok/x"}
            with pytest.raises(netguard.FetchError):
                netguard.safe_fetch_json("https://dead/x")
        assert rec.model_call("p", lambda p: "reply:" + p) == "reply:p"
    finally:
        netguard.safe_fetch_json = original
    ne.save_session(tmp_path, rec)
    rep = ne.load_session(tmp_path, "R1")
    with ne.patched_netguard(rep):
        assert netguard.safe_fetch_json("https://ok/x") == {"url": "https://ok/x"}
        with pytest.raises(netguard.FetchError) as err:
            netguard.safe_fetch_json("https://dead/x")
        assert err.value.status == 404
        with pytest.raises(ne.CassetteMiss):
            netguard.safe_fetch_json("https://never/recorded")
    assert netguard.safe_fetch_json is original, "patch must be undone"
    assert rep.model_call("p") == "reply:p"
    with pytest.raises(ne.CassetteMiss):
        rep.model_call("again")


# --- gold + end-to-end replay ---------------------------------------------------------------------

def test_committed_gold_is_well_formed_and_matches_the_blind50_review() -> None:
    gold = ne.load_gold()
    ids = [g["id"] for g in gold]
    assert len(ids) == len(set(ids)) >= 170, len(ids)
    for g in gold:
        assert {"id", "ask", "kind", "constraints", "acceptable_sources", "expected_outcome",
                "forbidden_outcomes", "human_verdict"} <= set(g), g["id"]
    blind = [g for g in gold if g["set"] == "A"]
    tally = {v: sum(1 for g in blind if g["human_verdict"] == v) for v in ("right", "partial", "wrong")}
    assert tally == {"right": 13, "partial": 6, "wrong": 4}, tally  # review.md: 13 / 6 / 4 of 23 live


def test_blind50_baseline_results_rescore_to_the_human_review() -> None:
    results = json.loads((pathlib.Path(__file__).parent / "fixtures" / "ni_blind" / "2026-10-05"
                          / "results.json").read_text())
    gold = ne.load_gold()
    by_ask = {g["ask"]: g["id"] for g in gold if g["set"] == "A"}
    for r in results:
        r["id"] = by_ask[r["ask"]]
    _, table = ne.score_all(gold, results)
    total = table["ALL"]
    assert (total["right"], total["partial"], total["wrong"], total["links"]) == (13, 6, 4, 27), total


def test_replaying_the_ci_cassette_drives_the_real_flow_and_scores_it() -> None:
    gold = ne.load_gold(_EVAL / "gold" / "ci.json")
    runs = ne.replay_all(gold, _EVAL / "cassettes" / "ci")
    assert [r["id"] for r in runs] == ["CI1", "CI2", "CI3", "CI4", "CI5", "CI6", "CI7", "CI8", "CI9", "CI10"], runs
    assert all(r["cassette_drift"] == 0 for r in runs), "scripted cassette has no prompt hashes"
    scored, table = ne.score_all(gold, runs)
    got = {s["id"]: s["cls"] for s in scored}
    # CI4 ("countdown to Christmas", no date) ends on the card's question "when is it?", which the user answers: it
    # waits. It scored "declined" while every unsupported terminal read as one; since the declined card is its own
    # state, a terminal that holds a question is "waiting", and a decline the gold expects would score right.
    assert got == {"CI1": "right", "CI2": "partial", "CI3": "wrong", "CI4": "waiting", "CI5": "links",
                   "CI6": "right", "CI7": "right", "CI8": "right",
                   "CI9": "right", "CI10": "right"}, got  # CI9 (new): the gold expects a decline and the flow ends on the declined card
    assert (table["ALL"]["n"], table["ALL"]["right"], table["ALL"]["decl_ok"]) == (10, 6, 1), table["ALL"]


def test_replay_is_deterministic() -> None:
    gold = ne.load_gold(_EVAL / "gold" / "ci.json")
    first = ne.score_all(gold, ne.replay_all(gold, _EVAL / "cassettes" / "ci"))[0]
    second = ne.score_all(gold, ne.replay_all(gold, _EVAL / "cassettes" / "ci"))[0]
    assert first == second, "two replays must score identically"
    assert len(first) == 10, first


def test_missing_cassette_entry_surfaces_as_an_error_not_a_pass(tmp_path) -> None:
    gold = [g for g in ne.load_gold(_EVAL / "gold" / "ci.json") if g["id"] == "CI1"]
    sess = ne.Session("CI1", {"ask_id": "CI1", "meta": {}, "fetches": [], "model": []})
    run = ne.run_entry(gold[0], sess)
    assert run["outcome"] != "live", run
    assert ne.score_ask(run, gold[0])["cls"] in ("error", "links", "declined"), run


# --- S4c: the researcher through the cassette ---------------------------------------------------------------------

def test_a_research_ask_replays_from_a_hand_built_cassette_with_no_model() -> None:
    gold = [g for g in ne.load_gold(_EVAL / "gold" / "ci.json") if g["id"] == "CI6"]
    assert gold[0]["harness"]["mode"] == "research"
    runs = ne.replay_all(gold, _EVAL / "cassettes" / "ci")
    run = runs[0]
    assert run["outcome"] == "awaiting-yes" and run["from"]["kind"] == "library", run  # held for Keep, nothing recurs
    assert run["source"] == "https://data.example.org/quakes.json" and run["cassette_drift"] == 0
    assert run["research"] == {"calls": 4, "prompt": 29500, "completion": 240, "cache_read": 12700,
                               "cache_write": 5000, "cost_usd": 0.0}, run["research"]
    assert ne.score_all(gold, runs)[0][0]["cls"] == "right"
    assert "median" in ne.research_summary(runs) and ne.research_summary([{"id": "x"}]) == ""


def test_a_compare_ask_replays_to_a_card_that_shows_both_things() -> None:
    """S7: the researcher proposes a compare (one address per thing); the card is held for Keep with both rows."""
    gold = [g for g in ne.load_gold(_EVAL / "gold" / "ci.json") if g["id"] == "CI8"]
    run = ne.replay_all(gold, _EVAL / "cassettes" / "ci")[0]
    assert run["outcome"] == "awaiting-yes" and run["cassette_drift"] == 0, run
    assert [(r["name"], r["value"]) for r in run["preview"]["rows"]] == [("Alpha Corp", 131.5), ("Beta Corp", 150.25)]
    assert ne.score_all(gold, [run])[0][0]["cls"] == "right"


def test_research_off_and_local_change_what_the_same_cassette_can_do() -> None:
    gold = [g for g in ne.load_gold(_EVAL / "gold" / "ci.json") if g["id"] == "CI6"]
    off = ne.replay_all(gold, _EVAL / "cassettes" / "ci", research="off")[0]
    assert off["outcome"] != "awaiting-yes" and "research" not in off, off  # no researcher, no card: the honest stop
    local = ne.replay_all(gold, _EVAL / "cassettes" / "ci", research="local")[0]
    assert local["outcome"] != "awaiting-yes", "the cloud turns are never served to the closed-world local mode"
    assert os.environ.get("SMARTBRAIN_NI_RESEARCH") in (None, "off"), "the env is restored after each ask"


def test_research_turns_record_and_replay_and_count_drift(tmp_path) -> None:
    rec = ne.Session("R2", recording=True)
    seen = []

    def real(model, messages, tools):
        seen.append((model, tools))
        return {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                         "prompt_tokens_details": {"cached_read_tokens": 4}}}

    rec.research_turn("anthropic/x", [{"role": "user", "content": "a"}], [{"t": 1}], real)
    ne.save_session(tmp_path, rec)
    rep = ne.load_session(tmp_path, "R2")
    assert rep.research_turn("anthropic/x", [{"role": "user", "content": "b"}], None)["usage"]["prompt_tokens"] == 10
    assert rep.drift == 1 and seen == [("anthropic/x", [{"t": 1}])]
    assert rep.research_stats()["cache_read"] == 4
    with pytest.raises(ne.CassetteMiss):
        rep.research_turn("anthropic/x", [{"role": "user", "content": "c"}], None)


def test_the_research_flag_is_closed_and_defaults_to_the_environment() -> None:
    with pytest.raises(SystemExit):
        ne.main(["replay", "--cassette-dir", str(_EVAL), "--research", "web"])
    assert set(ne.RESEARCH_MODES) == {"cloud", "local", "off"}


# --- the declined card as its own class ---------------------------------------------------------------------

DECLINE_GOLD = {"expected_outcome": "decline", "forbidden_outcomes": ["card"]}


def test_a_decline_is_right_when_the_gold_expects_one_and_declined_otherwise() -> None:
    run = {"outcome": "declined", "decline": {"reason": "no_answering_source"}}
    res = ne.score_ask(run, _gold(**DECLINE_GOLD))
    assert (res["cls"], res["reason"]) == ("right", ne.DECLINE_RIGHT), res
    assert ne.score_ask(run, _gold(expected_outcome=None, forbidden_outcomes=["card"]))["cls"] == "right"
    for open_gold in (_gold(), _gold(expected_outcome=None)):  # a card was expected, or the gold is silent
        res = ne.score_ask(run, open_gold)
        assert res["cls"] == "declined" and res["cls"] not in ("right", "wrong"), res


def test_only_a_declined_outcome_earns_the_decline_credit() -> None:
    gold = _gold(**DECLINE_GOLD)
    assert ne.score_ask(_card(["1"]), gold)["cls"] == "wrong", "a card where a decline was expected"
    assert ne.score_ask({"outcome": "links"}, gold)["cls"] == "links", "links are not a decline"
    assert ne.score_ask({"outcome": "needs-key"}, gold)["cls"] == "links"
    assert ne.score_ask({"outcome": "asks"}, gold)["cls"] == "waiting", "a question for the user is not a decline"
    assert ne.score_ask({"outcome": "crash"}, gold)["cls"] == "error"


def test_a_research_fault_scores_error_never_right_or_declined() -> None:
    """M1: the researcher's fault is the failed card, so a gateway outage cannot earn the decline credit."""
    assert ne.flow_outcome({"state": "failed", "error": "research: the research model was unavailable"}) == "failed"
    run = {"outcome": "failed"}
    assert ne.score_ask(run, _gold(**DECLINE_GOLD))["cls"] == "error", "a fault on a decline gold is not right"
    assert ne.score_ask(run, _gold())["cls"] == "error", "a fault on a card gold is an error, not a calm decline"
    assert ne.score_ask(run, _gold(expected_outcome=None, forbidden_outcomes=["card"]))["cls"] == "error"


def test_flow_outcome_names_a_declined_record_and_a_question() -> None:
    assert ne.flow_outcome({"state": "ready"}) == "live"
    assert ne.flow_outcome({"state": "declined"}) == "declined"
    assert ne.flow_outcome({"state": "unsupported", "_question": {"kind": "supply_date"}}) == "asks"
    assert ne.flow_outcome({"state": "unsupported"}) == "unsupported", "a v1 ending keeps its word"
    assert ne.flow_outcome({"state": "source"}) == "source"


def test_the_summary_tables_count_declined_apart_from_links_and_credit_the_expected_ones() -> None:
    scored = [{"kind": "a", "cls": "right", "reason": ne.DECLINE_RIGHT}, {"kind": "a", "cls": "right", "reason": "x"},
              {"kind": "a", "cls": "declined"}, {"kind": "a", "cls": "links"}]
    row = ne.aggregate(scored)["a"]
    assert (row["right"], row["decl_ok"], row["declined"], row["links"]) == (2, 1, 1, 1), row
    assert row["R"] == 0.5 and row["D"] == 0.25 and row["L"] == 0.5, "L stays links + declined; D is declined alone"
    lines = ne.format_table(ne.aggregate(scored)).splitlines()
    assert lines[0].split()[-1] == "dok" and lines[0].index("decl") < lines[0].index("dok")
    assert next(line for line in lines if line.startswith("ALL")).split()[-1] == "1"


def test_a_declined_flow_replays_to_its_closed_reason_and_scores_right(tmp_path) -> None:
    gold = [g for g in ne.load_gold(_EVAL / "gold" / "ci.json") if g["id"] == "CI9"]
    assert gold[0]["expected_outcome"] == "decline" and gold[0]["forbidden_outcomes"] == ["card"]
    run = ne.replay_all(gold, _EVAL / "cassettes" / "ci")[0]
    assert run["outcome"] == "declined" and run["decline"]["reason"] == "no_answering_source", run
    assert run["decline"]["headline"] == "I couldn't build a reliable card for this" and len(run["decline"]["links"]) <= 3
    assert ne.score_ask(run, gold[0])["cls"] == "right"
    card_gold = {**gold[0], "expected_outcome": "card", "forbidden_outcomes": []}
    assert ne.score_ask(run, card_gold)["cls"] == "declined", "the same decline, were a card expected"


def test_the_live_harness_reports_a_declined_record_as_declined_with_its_links() -> None:
    e2e = _load_e2e()
    rec = {"state": "declined", "notes": ["n"], "_decline_card": {
        "reason": "research_gave_up", "line": "I ran out of steps.",
        "links": [{"title": "Docs", "url": "https://docs.example.org/x"}, {"title": "no", "url": "http://insecure.example/x"}]}}
    out = e2e._v2_stop(None, "id", rec, {"outcome": "", "links": [], "notes": []})
    assert out["outcome"] == "declined" and out["detail"].startswith("research_gave_up: ")
    assert [x["host"] for x in out["links"]] == ["docs.example.org"], "an insecure link is never listed"
    assert ne.outcome_class(out) == "declined"
