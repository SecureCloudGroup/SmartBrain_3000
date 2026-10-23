"""Fix round after gate 2: the researcher converges, what passes its checks lands, and errors say what to change.

Class tests with scripted models (no model, no network, no per-ask cases):
- convergence: a run that holds a draft which passed ``try_recipe`` never ends on a budget or on a model that stops
  without proposing it; the same behaviours without a passing draft still end in an honest decline, not a fault;
- an address is fetched once per run (a sample or an error is answered from memory), identical deterministic calls
  are not paid for again;
- search results come data-like first, whatever the host;
- an ``invalid_spec`` / ``pipeline_failed`` result names the field and the allowed values or the keys that exist;
- validation and landing agree: whatever ``propose_card`` seals can land (the landing's own rule ran on the same
  sample), and a draft that could not land says so while the model can still fix it.
"""
from __future__ import annotations

import copy
import json

import pytest
from test_ni_research_loop import DRAFT, URL, Script, call, listed, reply, text_reply
from test_ni_research_tools import _ctx, _draft

from smartbrain_3000 import netguard, ni_research_flow
from smartbrain_3000.ni_research import digest, loop, schemas, tools


def tool_results(script: Script) -> list[dict]:
    """Every tool result the model was shown by its last call, in order."""
    return [json.loads(m["content"][0]["text"]) for m in script.seen[-1]["messages"] if m["role"] == "tool"]


# --- convergence: a draft that passed is never left to the budget ------------------------------------------------------

def _passed() -> list[dict]:
    return [reply(call("library_search", query="q")), reply(call("fetch_sample", url=URL)),
            reply(call("try_recipe", draft_spec=DRAFT))]


def _idle(n: int = 40) -> list[dict]:
    """A model that carries on exploring (a different call each time, so none is a repeat)."""
    return [reply(call("resolve_entity", text=f"place {i}")) for i in range(n)]


def _burst(n: int = 14) -> list[dict]:
    """A model that makes three distinct calls in every reply."""
    return [reply(*[call("resolve_entity", text=f"place {i} {j}") for j in range(3)]) for i in range(n)]


BEHAVIOURS = {
    "explores_until_the_turns_go": lambda: _idle(),
    "three_calls_per_reply": lambda: _burst(),
    "replies_without_a_tool_call": lambda: [text_reply("done")] * 6,
    "mixes_prose_and_exploring": lambda: [text_reply("hm"), *_idle(40)],
}


@pytest.mark.parametrize("after", sorted(BEHAVIOURS))
def test_a_run_holding_a_passing_draft_always_ends_proposed(monkeypatch, after) -> None:
    ctx, _ = listed(monkeypatch)
    out = loop.run_research(ctx, Script(*_passed(), *BEHAVIOURS[after]()))
    assert out.status == "proposed" and out.proposal is not None, (after, out.reasons)
    assert ctx.turns <= ctx.max_turns


@pytest.mark.parametrize("after", sorted(BEHAVIOURS))
def test_the_same_behaviour_without_a_passing_draft_is_an_honest_decline_not_a_fault(monkeypatch, after) -> None:
    ctx, _ = listed(monkeypatch)
    out = loop.run_research(ctx, Script(*_passed()[:2], *BEHAVIOURS[after]()))
    assert out.status == "declined" and out.proposal is None, (after, out.status, out.reasons)


def test_the_token_budget_does_not_strand_a_passing_draft(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch, token_cap=1000)
    big = {"prompt_tokens": 5000, "completion_tokens": 10}
    steps = [*_passed(), reply(call("resolve_entity", text="x"), used=big), *_idle()]   # the tokens go after the pass
    out = loop.run_research(ctx, Script(*steps))
    assert out.status == "proposed", out.reasons


def test_the_time_budget_does_not_strand_a_passing_draft(monkeypatch) -> None:
    now = [0.0]
    ctx, _ = listed(monkeypatch, clock=lambda: now[0])

    class Clocked(Script):
        def __call__(self, messages, tools_):
            got = super().__call__(messages, tools_)
            now[0] += 25.0   # every model call takes 25 s: the 120 s budget is gone after the fifth
            return got
    out = loop.run_research(ctx, Clocked(*_passed(), *_idle()))
    assert out.status == "proposed", out.reasons


def test_the_last_tool_call_is_kept_for_the_proposal_and_the_model_is_told(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch, max_turns=5)
    script = Script(*_passed(), reply(call("resolve_entity", text="a")), reply(call("resolve_entity", text="b")), *_idle())
    out = loop.run_research(ctx, script)
    assert out.status == "proposed" and ctx.turns <= 5
    note = [r for r in tool_results(script) if "note" in r.get("budget", {})]
    assert note and "propose_card" in note[-1]["budget"]["note"] and "tool calls left" in note[-1]["budget"]["note"]


def test_an_automatic_proposal_that_cannot_land_is_a_failed_check_and_two_end_the_run(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    out = loop.run_research(ctx, Script(*_passed(), *_idle()), accept=lambda proposal: "the scene does not bind")
    assert out.status == "declined" and "the scene does not bind" in " ".join(out.reasons)
    assert out.ending in ("research_declined_checks", "research_declined_budget"), out.ending


def test_a_model_that_cannot_be_reached_is_still_a_fault_even_holding_a_passing_draft(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    out = loop.run_research(ctx, Script(*_passed()))   # the script runs out: the route failed
    assert out.status == "fault" and out.ending == "fault" and out.proposal is None


def test_a_nudge_says_what_comes_next_from_where_the_run_stands(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    assert "library_search" in loop._nudge(ctx, []) and "tool calls left" in loop._nudge(ctx, [])
    tools.library_search(ctx, "q")
    assert "fetch_sample" in loop._nudge(ctx, [])
    tools.fetch_sample(ctx, URL)
    assert "try_recipe" in loop._nudge(ctx, []) and "propose_card" in loop._nudge(ctx, [])
    assert "invalid_spec" in loop._nudge(ctx, ["invalid_spec at display"]), "the last problem rides along"


# --- an address is fetched once -----------------------------------------------------------------------------------------

def test_an_address_is_fetched_once_and_a_repeat_costs_neither_a_fetch_nor_a_turn(monkeypatch) -> None:
    ctx, net = listed(monkeypatch)
    assert tools.library_search(ctx, "q")["ok"]
    first = tools.fetch_sample(ctx, URL)
    turns, fetches = ctx.turns, ctx.fetches
    again = tools.fetch_sample(ctx, URL)
    assert again["ok"] and again["digest"] == first["digest"] and again["cached"] is True
    assert len(net.calls) == 1 and ctx.fetches == fetches and ctx.turns == turns


@pytest.mark.parametrize(("exc", "kind"), [(netguard.FetchError("not json", kind="not_json"), "not_data"),
                                           (netguard.FetchError("boom"), "fetch_failed"),
                                           (netguard.FetchError("redirect refused"), "redirect")])
def test_an_address_that_failed_is_not_fetched_again_and_the_same_error_comes_back(monkeypatch, exc, kind) -> None:
    ctx, net = listed(monkeypatch)
    net.fail[URL] = exc
    tools.library_search(ctx, "q")
    one, two = tools.fetch_sample(ctx, URL), tools.fetch_sample(ctx, URL)
    assert one["error"] == kind and two["error"] == kind and two["cached"] is True
    assert len(net.calls) == 1 and ctx.fetches == 1


def test_a_model_that_keeps_repeating_a_fetch_pays_turns_for_it_after_a_few(monkeypatch) -> None:
    ctx, net = listed(monkeypatch)
    tools.library_search(ctx, "q")
    for _ in range(12):  # bounded
        tools.fetch_sample(ctx, URL)
    assert len(net.calls) == 1 and ctx.turns > 3, "the free repeats are capped; the run still ends on its budget"


def test_a_repeated_search_or_draft_is_answered_from_memory(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    script = Script(reply(call("library_search", query="q")), reply(call("library_search", query="q")),
                    reply(call("fetch_sample", url=URL)), reply(call("try_recipe", draft_spec=DRAFT)),
                    reply(call("try_recipe", draft_spec=DRAFT)), reply(call("propose_card", draft_spec=DRAFT)))
    out = loop.run_research(ctx, script)
    assert out.status == "proposed"
    assert ctx.turns == 4 and ctx.replays == 2, "two repeats cost no turn"
    assert len(ctx.library.asked) == 1 if hasattr(ctx.library, "asked") else True
    repeats = [r for r in tool_results(script) if "repeat" in r]
    assert len(repeats) == 2 and all(r["ok"] for r in repeats)


def test_a_search_is_not_repeated_on_the_network(monkeypatch) -> None:
    seen: list[str] = []

    def search(query: str, limit: int) -> dict:
        seen.append(query)
        return {"results": [{"title": "t", "url": "https://x.example.net/a.json", "snippet": "s"}]}
    ctx = _ctx(None, allowed_origins=frozenset({"library", "search", "preview"}), searcher=search)
    one, two, three = (schemas.run_tool(ctx, "web_search", {"query": "q json"}) for _ in range(3))
    assert one["ok"] and three["ok"] and two["ok"] and len(seen) == 1 and ctx.searches == 1


# --- search results: data-like first -------------------------------------------------------------------------------------

def _rows(*urls: str, title: str = "Result", snippet: str = "About it") -> list[dict]:
    return [{"title": title, "url": u, "snippet": snippet} for u in urls]


SHAPES = {   # path shape -> is it data-like (the host is a parameter: nothing here knows a host)
    "/data/quakes.json": True, "/export/table.csv": True, "/feed.xml": True, "/news/rss": True,
    "/v2/api/latest?format=json": True, "/download?output=csv": True, "/report/2026.html": False,
    "/about": False, "/blog/post-title": False, "/index.php": False,
}


@pytest.mark.parametrize("host", ["a.example.org", "b.example.net", "www.some-site.co.uk", "api.example.com"])
def test_search_results_come_data_like_first_whatever_the_host(host) -> None:
    urls = [f"https://{host}{p}" for p in SHAPES]
    ranked = digest.rank_search_rows(_rows(*urls))
    data = [r for r in ranked if r["looks_like"] == "data file or API"]
    page = [r for r in ranked if r["looks_like"] == "web page"]
    assert ranked == data + page, "every data-like address precedes every page"
    wanted = {f"https://{host}{p}" for p, is_data in SHAPES.items() if is_data}
    assert wanted <= {r["url"] for r in data}
    assert not {f"https://{host}{p}" for p, is_data in SHAPES.items() if not is_data} & {r["url"] for r in data}


def test_ranking_is_stable_so_the_providers_order_breaks_ties() -> None:
    urls = [f"https://s{i}.example.org/page{i}" for i in range(5)]
    assert [r["url"] for r in digest.rank_search_rows(_rows(*urls))] == urls


def test_web_search_hands_the_model_the_ranked_digest_and_keeps_every_address_legal() -> None:
    page, data = "https://news.example.org/story", "https://feeds.example.net/q.json"
    ctx = _ctx(None, allowed_origins=frozenset({"library", "search", "preview"}),
               searcher=lambda q, n: {"results": _rows(page, data)})
    got = tools.web_search(ctx, "quakes")
    assert got["digest"].index(data) < got["digest"].index(page)
    assert ctx.origin(page)[0] == "search" and ctx.origin(data)[0] == "search"


# --- errors say what to change ----------------------------------------------------------------------------------------------

def _sampled(monkeypatch):
    ctx, net = listed(monkeypatch)
    tools.library_search(ctx, "q")
    assert tools.fetch_sample(ctx, URL)["ok"]
    return ctx, net


def _mutated(**changes):
    d = copy.deepcopy(DRAFT)
    for k, v in changes.items():
        d[k] = v
    return d


@pytest.mark.parametrize(("draft", "field", "allowed"), [
    (_mutated(display={"size": "huge"}), "spec.display.size", "'wide'"),
    (_mutated(pipeline=[{"op": "extract", "paths": {"title": "metadata.title"}},
                        {"op": "transform", "apply": [{"fn": "frobnicate", "field": "title"}]}]), "apply[0].fn", "'sum'"),
], ids=["display-size", "transform-fn"])
def test_invalid_spec_names_the_field_and_lists_every_allowed_value_uncut(monkeypatch, draft, field, allowed) -> None:
    ctx, _ = _sampled(monkeypatch)
    got = tools.try_recipe(ctx, draft)
    assert got["error"] == "invalid_spec" and field in got["detail"] and allowed in got["detail"], got["detail"]
    assert not got["detail"].endswith(("'", "[")) or got["detail"].rstrip().endswith("]"), "the list is not cut mid-way"


def test_a_path_that_misses_says_where_it_stopped_and_what_keys_are_there(monkeypatch) -> None:
    ctx, _ = _sampled(monkeypatch)
    got = tools.try_recipe(ctx, _draft(URL, {"title": "metadata.nothere"}))
    assert got["error"] == "pipeline_failed" and "metadata.nothere" in got["detail"]
    assert "title" in got["detail"] and "at metadata" in got["detail"], got["detail"]


def test_a_scene_that_binds_a_name_the_pipeline_does_not_output_fails_try_recipe_naming_both(monkeypatch) -> None:
    ctx, _ = _sampled(monkeypatch)
    got = tools.try_recipe(ctx, _draft(URL, {"headline": "metadata.title"}))   # the scene binds {{title}}
    assert got["ok"] and got["verdict"] == "fail"
    assert any("binds 'title'" in r and "headline" in r for r in got["reasons"]), got["reasons"]


def test_every_failed_call_comes_back_with_the_next_step(monkeypatch) -> None:
    ctx, net = listed(monkeypatch)
    script = Script(reply(call("fetch_sample", url="https://data.example.org/nope.json")),
                    reply(call("library_search", query="q")), reply(call("fetch_sample", url=URL)),
                    reply(call("try_recipe", draft_spec=_mutated(display={"size": "huge"}))), reply(call("resolve_entity", text="x")),
                    *_idle(3))
    loop.run_research(ctx, script)
    shown = tool_results(script)
    assert shown[0]["error"] == "provenance" and "exactly as" in shown[0]["next"]
    bad = next(r for r in shown if r.get("error") == "invalid_spec")
    assert "allowed" in bad["next"] and "try_recipe" in bad["next"]
    assert all("budget" in r and "turns_left" in r["budget"] for r in shown), "the calls left ride every result"


# --- what passes its checks lands -----------------------------------------------------------------------------------------

DRAFTS = {   # draft family -> (paths, expect it to pass)
    "binds_every_output": ({"title": "metadata.title"}, True),
    "binds_a_name_nothing_outputs": ({"headline": "metadata.title"}, False),
    "outputs_more_than_bound": ({"title": "metadata.title", "n": "metadata.count"}, True),
}
KINDS = ["lookup", "latest_items", "ranking", "text_brief", "status"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("family", sorted(DRAFTS))
def test_whatever_propose_card_seals_can_land_and_what_cannot_is_refused_with_the_landing_reason(monkeypatch, family, kind) -> None:
    paths, should = DRAFTS[family]
    ctx, net = _sampled(monkeypatch)
    ctx.question["kind"] = kind
    draft = _draft(URL, paths)
    tried, proposed = tools.try_recipe(ctx, draft), tools.propose_card(ctx, draft)
    calls = len(net.calls)
    if proposed.get("ok"):
        assert tried["verdict"] == "pass", "try_recipe and propose_card agree"
        assert ni_research_flow._can_land(ctx, "item-1", ctx.request, proposed["proposal"]) is None
    else:
        assert tried["verdict"] == "fail" or not tried["ok"], "a draft the landing would refuse never passes try_recipe"
        assert proposed["error"] in ("verify_failed", "invalid_spec", "pipeline_failed")
        if family == "binds_a_name_nothing_outputs":
            assert any("binds 'title'" in r for r in proposed["reasons"]), proposed
    assert len(net.calls) == calls, "landing reads the sample it was checked on: no second fetch"


def test_propose_and_landing_give_the_same_verdict_for_every_draft_family(monkeypatch) -> None:
    ctx, _ = _sampled(monkeypatch)
    for paths, _should in DRAFTS.values():
        proposed = tools.propose_card(ctx, _draft(URL, paths))
        if proposed.get("ok"):
            assert ni_research_flow._can_land(ctx, "item-1", ctx.request, proposed["proposal"]) is None
            continue
        assert any("lands:" in r for r in proposed["reasons"]) or proposed["error"] != "verify_failed", (paths, proposed)


def test_the_loop_fixes_a_scene_that_binds_a_missing_name_before_proposing_it(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    wrong = _draft(URL, {"headline": "metadata.title"})
    script = Script(reply(call("library_search", query="q")), reply(call("fetch_sample", url=URL)),
                    reply(call("try_recipe", draft_spec=wrong)), reply(call("try_recipe", draft_spec=DRAFT)),
                    reply(call("propose_card", draft_spec=DRAFT)))
    landed: list[str | None] = []
    out = loop.run_research(ctx, script, accept=lambda p: landed.append(ni_research_flow._can_land(ctx, "i", ctx.request, p)) or landed[-1])
    assert out.status == "proposed" and landed == [None]
    first = tool_results(script)
    assert ctx.turns == 5 and first[2]["verdict"] == "fail", "the miss was caught at try_recipe, not at landing"


def test_the_libraryless_net_is_never_touched_by_a_landing_check(monkeypatch) -> None:
    ctx, net = _sampled(monkeypatch)
    got = tools.propose_card(ctx, DRAFT)
    before = len(net.calls)
    assert ni_research_flow._can_land(ctx, "item-1", ctx.request, got["proposal"]) is None
    assert len(net.calls) == before == 1


def test_a_source_the_user_said_no_to_on_this_card_never_lands_again(monkeypatch) -> None:
    """S10: the retired web stage dropped a declined page from its fresh search. Research finds pages its own way (a
    search, a page's link), so the landing is the one place that refuses a declined source, however it was found."""
    ctx, _ = _sampled(monkeypatch)
    got = tools.propose_card(ctx, DRAFT)
    declined = ni_research_flow._declined_of({"_declined": [URL + "#top", "not an address", 7]})
    why = ni_research_flow._can_land(ctx, "item-1", ctx.request, got["proposal"], declined)
    assert why and "said no" in why
    assert ni_research_flow._can_land(ctx, "item-1", ctx.request, got["proposal"], frozenset()) is None


# --- review of PR 541: the call memory keys the whole draft; a pass on the last call is proposed; a failed pass is not retried ---

def test_two_drafts_that_differ_only_after_4000_characters_are_not_the_same_call(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    assert tools.library_search(ctx, "q")["ok"] and tools.fetch_sample(ctx, URL)["ok"]
    good = _mutated(goal="show it " + "x" * 4200)
    broken = copy.deepcopy(good)
    broken["pipeline"] = [{"op": "extract", "paths": {"title": "metadata.nothere"}}]
    assert json.dumps(good, sort_keys=True)[:4000] == json.dumps(broken, sort_keys=True)[:4000], "they agree on 4,000 characters"
    first = schemas.run_tool(ctx, "try_recipe", {"draft_spec": good})
    second = schemas.run_tool(ctx, "try_recipe", {"draft_spec": broken})
    assert first["verdict"] == "pass" and "repeat" not in second
    assert second["error"] == "pipeline_failed", "the broken draft is run, not answered with the earlier pass"
    assert schemas.run_tool(ctx, "try_recipe", {"draft_spec": good}).get("repeat"), "the same draft is still a repeat"


@pytest.mark.parametrize("max_turns", [3, 4, 5, 6])
def test_a_draft_that_passes_on_the_last_tool_call_is_still_proposed(monkeypatch, max_turns) -> None:
    ctx, _ = listed(monkeypatch, max_turns=max_turns)
    pad = [reply(call("resolve_entity", text=f"place {i}")) for i in range(max_turns - 3)]
    out = loop.run_research(ctx, Script(*pad, *_passed(), *_idle()))
    assert out.status == "proposed" and out.proposal is not None, out.reasons
    assert ctx.turns == max_turns, "the pass was the last tool call"
    assert ctx.trace[-1]["arg"] == "loop draft" and ctx.trace[-1]["outcome"] == "ok", "the trace says the loop proposed"


def test_the_loops_own_proposal_costs_no_turn_but_a_models_does(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch, max_turns=3)
    schemas.run_tool(ctx, "library_search", {"query": "q"})
    schemas.run_tool(ctx, "fetch_sample", {"url": URL})
    assert schemas.run_tool(ctx, "try_recipe", {"draft_spec": DRAFT})["verdict"] == "pass"
    assert ctx.turns == 3 and tools.propose_card(ctx, DRAFT)["error"] == "budget_exhausted"
    assert loop._propose_passed(ctx, None)[0]["ok"] and ctx.turns == 3 and not ctx.own_proposal


def test_a_pass_the_model_proposed_and_landing_refused_is_not_proposed_again(monkeypatch) -> None:
    ctx, _ = listed(monkeypatch)
    seen: list[object] = []

    def refuse(proposal):
        seen.append(proposal)
        return "the scene does not bind"
    out = loop.run_research(ctx, Script(*_passed(), reply(call("propose_card", draft_spec=DRAFT)), *_idle()), accept=refuse)
    assert len(seen) == 1, "the futile second landing check is not made"
    assert out.status == "declined" and ctx.passed["tried"] is True
    assert out.ending != "research_declined_checks" or "twice" not in " ".join(out.reasons), out.reasons


def test_a_failed_automatic_proposal_after_a_nudge_joins_the_nudge_turn() -> None:
    turns = [{"role": "assistant", "content": "hm"}, {"role": "user", "content": "Reply with a tool call"}]
    loop._say(turns, "it failed")
    assert [t["role"] for t in turns] == ["assistant", "user"] and turns[-1]["content"].endswith("it failed")
