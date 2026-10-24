"""P5 (release plan, T 2026-10-24): the bundled seed. Sources SmartBrain built on its own runs and checked by hand
ship with the app in the kept-row shape, so a new user's ask builds from one in seconds, with no model and no
research, by the same match and the same checks as a source the user kept. The user's own rows come first; Keep
makes a seed source one of theirs; No, or three failed reads, turns that seed entry off for this user only."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import pathlib

import pytest
from test_ni_answers import lib  # noqa: F401  (a fixture)
from test_ni_auto_pick import _store
from test_ni_intake import _ask, _kept, _researched
from test_ni_research_loop import (  # noqa: F401
    _ASK,
    _INTENT,
    URL,
    Script,
    _flow,
    _v2,  # an autouse fixture: research on, a local route
    _wrap,
    call,
    good_run,
    reply,
)
from test_ni_research_tools import QUAKES, Net

from smartbrain_3000 import (
    ni,
    ni_flow,
    ni_intake,
    ni_measure,
    ni_research_flow,
    ni_routes,
)
from smartbrain_3000.netguard import FetchError


@pytest.fixture
def seeded(lib, monkeypatch, tmp_path):  # noqa: F811
    """A seed file holding the source research built for the test ask, checked and exported on another store."""
    store, item_id, _ = _researched(monkeypatch, lib)
    row = ni_intake.seed_row(store, [store.get_item(item_id)])
    assert row is not None and row["id"].startswith(ni_intake.SEED_PREFIX) and row["origin"]["by"] == "seed"
    path = tmp_path / "ni_seed.json"
    path.write_text(json.dumps({"version": ni_intake.SEED_VERSION, "made": "2026-10-24", "rows": [row]}))
    monkeypatch.setattr(ni_intake, "SEED_FILE", path)
    ni_intake._seed_at.cache_clear()
    yield row
    ni_intake._seed_at.cache_clear()


def test_a_new_users_ask_builds_from_the_seed_with_no_model_and_no_research(lib, seeded, monkeypatch) -> None:  # noqa: F811
    seam = Script()   # any model call would find no scripted reply
    store, item_id, out, net = _flow(monkeypatch, _wrap(lib), store=_store(), seam=seam, cloud="anthropic/x",
                                     consented=True)
    assert out["state"] == "ready" and seam.seen == [] and [c["url"] for c in net.calls] == [URL]
    item = store.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == "seed" and ni.awaits_yes(item), "held for Keep like any other"
    assert item["spec"]["recipe"]["sources"][0]["why"] == "a source SmartBrain checked"
    assert any("a source SmartBrain checked" in n for n in ni_flow._flow_read(store, item_id)["notes"])
    ni_measure.record(store, item, kept=True)
    assert ni_measure.view(store)["by_source"]["library"]["kept"] == 1, "the seed counts with the Library"


def test_a_seed_card_from_a_page_table_says_it_reads_a_web_page(lib, monkeypatch, tmp_path) -> None:  # noqa: F811
    """#551 review F3: a seed card is labelled by what it reads, as a kept card is: a page's table is a web page
    (ruling 2026-10-22), never a Library row."""
    from test_ni_page_tables import CLOUD, EQ_PAGE, EQ_URL, table_draft
    from test_ni_research_web import _web_flow, rows
    draft = table_draft(paths={"top": "rows[0].place"})
    draft["source"]["url"] = EQ_URL
    script = Script(reply(call("library_search", query="earthquakes")), reply(call("web_search", query="quakes table")),
                    reply(call("fetch_sample", url=EQ_URL)), reply(call("try_recipe", draft_spec=draft)),
                    reply(call("propose_card", draft_spec=draft)))
    built, item_id, out, _, _ = _web_flow(monkeypatch, _wrap(lib), script, rows(EQ_URL), {EQ_URL: EQ_PAGE}, **CLOUD)
    assert out["state"] == "ready", out
    path = tmp_path / "seed-page.json"
    path.write_text(json.dumps({"version": ni_intake.SEED_VERSION, "made": "2026-10-24",
                                "rows": [ni_intake.seed_row(built, [built.get_item(item_id)])]}))
    monkeypatch.setattr(ni_intake, "SEED_FILE", path)
    store = _store()
    Net(monkeypatch, {EQ_URL: EQ_PAGE})
    again = _ask(store)
    assert ni_intake.reuse(store, again, _ASK, dict(_INTENT))["state"] == "ready"
    item = store.get_item(again)
    assert item["spec"]["_built_from"]["path"] == "seed" and item["spec"]["recipe"]["tier"] == "page_reading"
    assert ni_routes._awaiting_yes_of(item)["from"] == "page"
    assert ni_intake.remember(store, item)   # kept, it is the user's own page source, and reads its table the same way
    Net(monkeypatch, {EQ_URL: EQ_PAGE})
    mine = _ask(store)
    assert ni_intake.reuse(store, mine, _ASK, dict(_INTENT))["state"] == "ready"
    assert store.get_item(mine)["spec"]["_built_from"]["path"] == "kept"
    assert ni_routes._awaiting_yes_of(store.get_item(mine))["from"] == "page"


def test_keeping_a_seed_card_makes_it_your_own_and_your_own_comes_first(lib, seeded, monkeypatch) -> None:  # noqa: F811
    store, item_id, out, _ = _flow(monkeypatch, _wrap(lib), store=_store(), seam=Script(), cloud="anthropic/x",
                                   consented=True)
    assert out["state"] == "ready" and ni_intake.remember(store, store.get_item(item_id))
    assert [r["kept"]["key"] for r in _kept(store)] == [seeded["kept"]["key"]], "now one of the user's own"
    Net(monkeypatch, {URL: QUAKES})
    again = _ask(store)
    assert ni_intake.reuse(store, again, _ASK, dict(_INTENT))["state"] == "ready"
    assert store.get_item(again)["spec"]["_built_from"]["path"] == "kept", "read as the user's own, not the seed's"


def test_no_on_a_seed_card_turns_that_entry_off_for_this_user_only(lib, seeded, monkeypatch) -> None:  # noqa: F811
    store, item_id, out, _ = _flow(monkeypatch, _wrap(lib), store=_store(), seam=Script(), cloud="anthropic/x",
                                   consented=True)
    assert out["state"] == "ready"
    assert ni_intake.forget(store, store.get_item(item_id)) == 1
    Net(monkeypatch, {URL: QUAKES})
    assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT)) is None, "not offered for this question again"
    other = _store()   # another user still has it, and the bundled file is unchanged
    Net(monkeypatch, {URL: QUAKES})
    assert ni_intake.reuse(other, _ask(other), _ASK, dict(_INTENT))["state"] == "ready"
    assert len(ni_intake.seed_rows()) == 1


def test_a_seed_source_that_fails_three_reads_in_a_row_is_turned_off(seeded, monkeypatch) -> None:
    store = _store()
    net = Net(monkeypatch, {})
    net.fail[URL] = FetchError("gone", status=404)
    for _ in range(ni_intake.MAX_MISSES):
        assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT)) is None
    assert len(net.calls) == ni_intake.MAX_MISSES
    assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT)) is None
    assert len(net.calls) == ni_intake.MAX_MISSES, "a dead seed source stops costing every ask a fetch"


def test_an_answer_resets_a_seed_sources_failed_reads(seeded, monkeypatch) -> None:
    store = _store()
    net = Net(monkeypatch, {})
    net.fail[URL] = FetchError("down", status=503)
    ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT))
    assert list(ni_intake._seed_state(store)["misses"].values()) == [1]
    Net(monkeypatch, {URL: QUAKES})
    assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT))["state"] == "ready"
    assert ni_intake._seed_state(store) == {"off": [], "misses": {}}


def _damage(row: dict, damage: str) -> None:
    """One way a shipped row can be wrong; every one of them is skipped, never read."""
    entry = row["kept"]["entries"][0]
    source = entry["draft"]["source"]
    if damage == "id":
        row["id"] = "local-k123"                                   # not a seed row
    elif damage == "draft":
        entry["draft"]["pipeline"] = [{"op": "llm", "prompt": "x"}]   # not replayable
    elif damage == "http":   # the address it reads is in the clear, though its draft says https
        row["access"]["url_template"] = URL.replace("https://", "http://")
    elif damage == "key":    # keyed as another address: it would dodge the user's declines and off list
        row["kept"]["key"] = "https://elsewhere.example.org/q.json"
    elif damage == "provider":
        row["provider"]["name"] = "elsewhere.example.org"
    elif damage == "draft-url":   # the entry replays another address than the row reads
        source["url"] = "https://elsewhere.example.org/q.json"
    elif damage == "pipeline-text":   # propose_card would refuse it after the fetch, so it is never read
        entry["draft"]["pipeline"] = "rows[0]"
    elif damage == "source-text":
        entry["draft"]["source"] = "https://data.example.org/q.json"
    elif damage == "headers":
        source["headers"] = {"X-Api-Key": "abc"}
    elif damage == "secret-query":
        source["secret_query"] = {"key": "api_key"}
    elif damage == "title":
        entry["title"] = "t" * (ni_intake.MAX_TITLE + 1)
    elif damage == "request":
        entry["request"] = "r" * (ni_intake.MAX_REQUEST + 1)


_ROW_DAMAGE = ["id", "draft", "pipeline-text", "http", "key", "provider", "draft-url", "source-text", "headers",
               "secret-query", "title", "request"]


@pytest.mark.parametrize("damage", ["file", "version", *_ROW_DAMAGE])
def test_a_seed_this_version_cannot_read_is_skipped(seeded, monkeypatch, tmp_path, damage) -> None:
    row = copy.deepcopy(seeded)
    body = {"version": ni_intake.SEED_VERSION, "rows": [row]}
    if damage == "version":
        body["version"] = ni_intake.SEED_VERSION + 1
    elif damage != "file":
        _damage(row, damage)
    path = tmp_path / f"seed-{damage}.json"
    path.write_text("{not json" if damage == "file" else json.dumps(body))
    monkeypatch.setattr(ni_intake, "SEED_FILE", path)
    assert ni_intake.seed_rows() == []
    store = _store()
    Net(monkeypatch, {URL: QUAKES})
    assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT)) is None


@pytest.mark.parametrize("damage", ["source-text", "draft-url", "key"])
def test_one_damaged_seed_row_never_costs_the_rest(seeded, monkeypatch, tmp_path, damage) -> None:
    bad = copy.deepcopy(seeded)
    bad["id"] = ni_intake.SEED_PREFIX + "bad"
    _damage(bad, damage)
    path = tmp_path / "seed-mixed.json"
    path.write_text(json.dumps({"version": ni_intake.SEED_VERSION, "rows": [bad, seeded]}))
    monkeypatch.setattr(ni_intake, "SEED_FILE", path)
    assert [r["id"] for r in ni_intake.seed_rows()] == [seeded["id"]]
    store = _store()
    Net(monkeypatch, {URL: QUAKES})
    assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT))["state"] == "ready"


def test_a_kept_address_is_never_read_in_the_clear(monkeypatch) -> None:
    """The research context's own rule: an address from the open web (search, kept, seed) counts only over https."""
    from smartbrain_3000.ni_research.ctx import WEB_ORIGINS, ResearchCtx
    ctx = ResearchCtx(request=_ASK, question={})
    for origin in WEB_ORIGINS:
        ctx.record(f"http://{origin}.example.org/q.json", origin)
        ctx.record(f"https://{origin}.example.org/q.json", origin)
    assert sorted(ctx.urls) == sorted(f"https://{o}.example.org/q.json" for o in WEB_ORIGINS)


def test_a_no_holds_when_a_later_seed_words_its_question_otherwise(lib, seeded, monkeypatch, tmp_path) -> None:  # noqa: F811
    store, item_id, out, _ = _flow(monkeypatch, _wrap(lib), store=_store(), seam=Script(), cloud="anthropic/x",
                                   consented=True)
    assert out["state"] == "ready" and ni_intake.forget(store, store.get_item(item_id)) == 1
    later = copy.deepcopy(seeded)   # the next release's seed: the same address, its question worded otherwise
    later["kept"]["entries"][0]["question"] = {**later["kept"]["entries"][0]["question"], "cadence": "daily"}
    path = tmp_path / "seed-later.json"
    path.write_text(json.dumps({"version": ni_intake.SEED_VERSION, "rows": [later]}))
    monkeypatch.setattr(ni_intake, "SEED_FILE", path)
    Net(monkeypatch, {URL: QUAKES})
    assert ni_intake.reuse(store, _ask(store), _ASK, dict(_INTENT)) is None, "the user's No still holds"


def test_a_seed_row_is_one_address_and_only_what_can_be_replayed(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _ = _researched(monkeypatch, lib)
    item = store.get_item(item_id)
    other = copy.deepcopy(item)
    other["spec"]["source"]["url"] = "https://other.example.org/quakes.json"
    assert ni_intake.seed_row(store, [item, other]) is None, "one row is one address"
    library = copy.deepcopy(item)
    library["spec"]["_built_from"]["path"] = "auto"
    assert ni_intake.seed_row(store, [library]) is None, "a Library card is found by the Library anyway"
    row = ni_intake.seed_row(store, [item, copy.deepcopy(item)])
    assert row is not None and len(row["kept"]["entries"]) == 2 and "host" not in json.dumps(row["kept"]["key"])


def test_the_bundled_seed_reads_whole() -> None:
    """Every row the app ships is one this version reads and the researcher's tools replay."""
    raw = json.loads(ni_intake.SEED_FILE.read_text("utf-8"))
    ni_intake._seed_at.cache_clear()
    assert raw["version"] == ni_intake.SEED_VERSION
    assert len(ni_intake.seed_rows()) == len(raw["rows"])
    assert all(len(r["kept"]["entries"]) == len(ni_intake._entries(r)) for r in ni_intake.seed_rows())


# --- the seeding tool (tools/ni-seed.py; the docker image ships app/ only) ----------------------------------------

_TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-seed.py"
needs_tool = pytest.mark.skipif(not _TOOL.is_file(), reason="tools/ is not in this image")


def _tool():
    spec = importlib.util.spec_from_file_location("ni_seed_tool", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_tool
def test_export_ships_exactly_the_candidates_checked_right(seeded, tmp_path) -> None:
    tool = _tool()
    second = copy.deepcopy(seeded)   # the same address kept for another question
    second["kept"]["entries"][0]["question"] = {**second["kept"]["entries"][0]["question"], "subject": "seismic events"}
    wrong = copy.deepcopy(seeded)
    wrong["kept"]["key"] = wrong["access"]["url_template"] = "https://wrong.example.org/q.json"
    out = tmp_path / "run"
    out.mkdir()
    (out / "candidates.json").write_text(json.dumps([{"id": "S1", "row": seeded}, {"id": "S2", "row": second},
                                                     {"id": "S3", "row": wrong}]))
    seed_file = tmp_path / "shipped.json"
    args = argparse.Namespace(out=out, seed_file=seed_file)
    (out / "verdicts.json").write_text(json.dumps({"S1": "right", "S2": "right", "S3": ""}))
    assert tool.cmd_export(args) == 2 and not seed_file.exists(), "every candidate is checked first"
    (out / "verdicts.json").write_text(json.dumps({"S1": "right", "S2": "right", "S3": "wrong"}))
    assert tool.cmd_export(args) == 0
    body = json.loads(seed_file.read_text())
    assert body["version"] == ni_intake.SEED_VERSION and [r["kept"]["key"] for r in body["rows"]] == [
        seeded["kept"]["key"]], "only what was checked right ships"
    assert len(body["rows"][0]["kept"]["entries"]) == 2, "one row per address, its questions merged"


@needs_tool
def test_the_seed_asks_are_a_plain_list() -> None:
    asks = _tool().load_asks(_TOOL.with_name("ni-seed-asks.txt"))
    assert len(asks) >= 40 and len({a.lower() for a in asks}) == len(asks)


@needs_tool
def test_a_seed_row_counts_only_when_it_builds_the_ask_again(lib, seeded, monkeypatch, tmp_path) -> None:  # noqa: F811
    """#551: a row that loads is not enough. ``serves`` runs the real flow for a new user and wants the card built
    from the seed; ``check`` runs it for every shipped entry, and ``run`` for every candidate."""
    tool = _tool()

    def llm(prompt: str, _n: int) -> str:
        return json.dumps({**_INTENT, "wants": ["magnitude"]}) if prompt.startswith("Classify") else (
            '{"serves": true, "gaps": [], "wrong": []}')
    Net(monkeypatch, {URL: QUAKES})
    assert tool.serves([seeded], _ASK, _wrap(lib), llm, "mlx/test-local", tmp_path)
    Net(monkeypatch, {URL: {"other": [{"x": 1}]}})   # the address answers, but not with what the card reads
    assert not tool.serves([seeded], _ASK, _wrap(lib), llm, "mlx/test-local", tmp_path)
    assert ni_intake.seed_rows() == [seeded], "the shipped seed is back in place"


def test_a_run_turns_a_card_research_built_into_a_candidate(lib, seeded, monkeypatch, tmp_path) -> None:  # noqa: F811
    """``run_one`` on a fresh store, as a new user: the intent read, Keep looking tapped, research lands the card,
    and the row its Keep would write is the candidate once it builds the ask again for another new user. A run reads
    no bundled seed (here one already answers the ask), so a run after an export researches again instead of
    confirming the last export."""
    tool = _tool()
    Net(monkeypatch, {URL: QUAKES})
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    ni_research_flow.set_research_chat(lambda model, messages, tools: seam(messages, tools or None),
                                       cloud="anthropic/x", consented=True)
    seam = Script(*good_run())
    seen: list[str] = []

    def llm(prompt: str, _n: int) -> str:
        seen.append(prompt[:120])
        return json.dumps({**_INTENT, "wants": ["magnitude"]}) if prompt.startswith("Classify") else '{"serves": true, "gaps": [], "wrong": []}'
    run = tool.run_one(_ASK, _wrap(lib), llm, "mlx/test-local", tmp_path)
    ni_research_flow.set_research_chat(None)
    assert run["outcome"] == "candidate", run.get("detail")
    assert run["path"] == "research" and run["url"] == URL and run["row"]["id"].startswith(ni_intake.SEED_PREFIX)
    assert ni_intake.seed_rows() == [seeded], "the shipped seed is back in place after the run"
