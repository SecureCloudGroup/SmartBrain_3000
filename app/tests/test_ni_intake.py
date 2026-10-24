"""P1 (release plan, T 2026-10-24): a card the user Keeps becomes a source in their own Library, and the next ask
with the same question builds from it with no model and no research. Class tests: what is kept, what a kept source
matches, the reuse landing held through every check, a dead source forgotten, a declined one skipped."""
from __future__ import annotations

import copy

import pytest
from test_ni_answers import lib  # noqa: F401  (a fixture)
from test_ni_auto_pick import _store
from test_ni_research_loop import (  # noqa: F401
    _ASK,
    _INTENT,
    URL,
    Script,
    _flow,
    _v2,  # an autouse fixture: research on, a local route
    _wrap,
    client,
    good_run,
)
from test_ni_research_tools import QUAKES, Net, _draft

from smartbrain_3000 import library_index, ni, ni_flow, ni_intake, ni_research_flow
from smartbrain_3000.library_routes import _local_row
from smartbrain_3000.netguard import FetchError

QUESTION = ni_research_flow.question_from_intent(_ASK, _INTENT)


def _kept(store) -> list[dict]:
    return [r for r in library_index.LocalSources(store).list() if r.get("kept")]


def _researched(monkeypatch, lib_, store=None):
    """A card research built (held), as the flow lands it."""
    store, item_id, out, net = _flow(monkeypatch, _wrap(lib_), store=store, seam=Script(*good_run()),
                                     cloud="anthropic/x", consented=True)
    assert out["state"] == "ready", out
    return store, item_id, net


# --- what is kept ------------------------------------------------------------------------------------------------

def test_a_kept_research_card_becomes_one_of_your_sources(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _ = _researched(monkeypatch, lib)
    assert ni_intake.remember(store, store.get_item(item_id))
    rows = _kept(store)
    assert len(rows) == 1 and rows[0]["id"].startswith("local-") and rows[0]["access"]["url_template"] == URL
    entry = rows[0]["kept"]["entries"][0]
    assert entry["question"] == store.get_item(item_id)["spec"]["recipe"]["question"] and entry["request"] == _ASK
    assert set(entry["draft"]) <= set(ni_intake._DRAFT_KEYS) and "recipe" not in entry["draft"]
    assert _local_row(rows[0])["url_template"] == URL, "the Library page lists it with your own sources"


def test_keeping_the_same_address_again_adds_a_question_and_never_a_second_row(lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _ = _researched(monkeypatch, lib)
    item = store.get_item(item_id)
    assert ni_intake.remember(store, item) and ni_intake.remember(store, item)
    other = copy.deepcopy(item)
    other["spec"]["recipe"]["question"] = {**QUESTION, "subject": "seismic events"}
    assert ni_intake.remember(store, other)
    rows = _kept(store)
    assert len(rows) == 1 and len(rows[0]["kept"]["entries"]) == 2


@pytest.mark.parametrize("change", [
    {"_built_from": {"path": "auto", "host": "data.example.org", "title": ""}},      # a Library row: found anyway
    {"pipeline": [{"op": "llm", "prompt": "summarize"}]},                              # a model step: not replayable
    {"source": {"type": "http_page", "url": URL}},                                     # a rendered page
])
def test_a_card_that_cannot_be_replayed_as_it_stands_is_not_kept(lib, monkeypatch, change) -> None:  # noqa: F811
    store, item_id, _ = _researched(monkeypatch, lib)
    item = store.get_item(item_id)
    item["spec"].update(change)
    assert not ni_intake.remember(store, item) and _kept(store) == []


def test_keep_on_the_board_writes_the_source(client, lib, monkeypatch) -> None:  # noqa: F811
    store, item_id, _ = _researched(monkeypatch, lib, store=client.app.state.ni)
    assert client.post(f"/api/ni/items/{item_id}/validate", json={"ok": True}).status_code == 200
    assert [r["access"]["url_template"] for r in _kept(store)] == [URL]
    assert any(r["url_template"] == URL for r in client.get("/api/library/local").json()["sources"])


# --- what a kept source matches ------------------------------------------------------------------------------------

def _entry(question: dict, request: str = _ASK, title: str = "Earthquakes, past hour") -> dict:
    return {"question": question, "request": request, "title": title, "draft": {}, "at": "2026-10-24", "misses": 0}


@pytest.mark.parametrize(("new", "fits"), [
    ({}, True),                                                     # the same question
    ({"subject": "latest earthquakes"}, True),                     # window and filler words aside
    ({"subject": "earthquake magnitudes"}, False),                 # a word it was not kept for
    ({"kind": "ranking"}, False),                                   # another kind of answer
    ({"places": ["Alaska"]}, False),                                # a place the kept one did not have
    ({"entities": ["Alaska", "Chile"], "kind": "compare"}, False),  # a compare is never reused (P1)
])
def test_a_kept_source_fits_only_the_question_it_answers(new, fits) -> None:
    assert ni_intake.matches({**QUESTION, **new}, _ASK, _entry(QUESTION)) is fits


@pytest.mark.parametrize(("request_then", "ask", "fits"), [
    ("BBC earthquakes", "AP earthquakes", False),         # a name the kept source is not known by
    ("USGS earthquakes", "latest USGS earthquakes", True),  # the same name
    ("earthquakes", "earthquakes", True),                   # no name asked
])
def test_every_name_the_new_ask_carries_must_be_known_to_the_kept_source(request_then, ask, fits) -> None:
    question = {**QUESTION, "subject": "earthquakes"}
    assert ni_intake.matches(question, ask, _entry(question, request=request_then, title="")) is fits


@pytest.mark.parametrize(("kept", "asked", "fits"), [
    (["Raleigh, NC"], ["Raleigh"], True),     # one names the other's words
    (["Portland, OR"], ["Portland, ME"], False),
    ([], ["Raleigh"], False),
])
def test_places_must_agree(kept, asked, fits) -> None:
    assert ni_intake.matches({**QUESTION, "places": asked}, _ASK, _entry({**QUESTION, "places": kept})) is fits


# --- reuse -----------------------------------------------------------------------------------------------------------

def test_the_next_ask_builds_from_the_kept_source_with_no_model_and_no_research(lib, monkeypatch) -> None:  # noqa: F811
    store, first, _ = _researched(monkeypatch, lib)
    assert ni_intake.remember(store, store.get_item(first))
    seam = Script()   # any model call would find no scripted reply
    store, item_id, out, net = _flow(monkeypatch, _wrap(lib), store=store, seam=seam, cloud="anthropic/x",
                                     consented=True)
    assert out["state"] == "ready" and seam.seen == [] and [c["url"] for c in net.calls] == [URL]
    item = store.get_item(item_id)
    source = item["spec"]["recipe"]["sources"][0]
    assert ni.awaits_yes(item), "a reused source is still held for your Keep"
    assert source["why"] == "a source you kept before" and source["kind"] == "endpoint"
    assert any("a source you kept before" in n for n in ni_flow._flow_read(store, item_id)["notes"])


def test_a_kept_source_that_fails_the_new_asks_checks_never_lands(lib, monkeypatch) -> None:  # noqa: F811
    store, first, _ = _researched(monkeypatch, lib)
    assert ni_intake.remember(store, store.get_item(first))
    item_id = ni_flow.create_shell_item(store, _ASK)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(_ASK, "intent"))
    Net(monkeypatch, {URL: {"metadata": {"title": ""}, "features": []}})   # the source now serves nothing
    assert ni_intake.reuse(store, item_id, _ASK, dict(_INTENT)) is None
    assert _kept(store)[0]["kept"]["entries"][0]["misses"] == 0, "a source that answered is not a dead one"


def test_a_kept_source_that_stops_answering_is_forgotten(lib, monkeypatch) -> None:  # noqa: F811
    store, first, _ = _researched(monkeypatch, lib)
    assert ni_intake.remember(store, store.get_item(first))
    Net(monkeypatch, {}, fail={URL: FetchError("connection refused", kind="network")})   # the source is gone
    for n in range(ni_intake.MAX_MISSES):
        item_id = ni_flow.create_shell_item(store, f"{_ASK} ({n})")   # a card name is unique
        ni_flow._flow_write(store, item_id, ni_flow._make_record(_ASK, "intent"))
        assert ni_intake.reuse(store, item_id, _ASK, dict(_INTENT)) is None
        assert len(_kept(store)) == (1 if n + 1 < ni_intake.MAX_MISSES else 0)


def test_a_source_you_said_no_to_on_this_card_is_never_reused_on_it(lib, monkeypatch) -> None:  # noqa: F811
    store, first, _ = _researched(monkeypatch, lib)
    assert ni_intake.remember(store, store.get_item(first))
    net = Net(monkeypatch, {URL: QUAKES})
    item_id = ni_flow.create_shell_item(store, _ASK)
    ni_flow._flow_write(store, item_id, {**ni_flow._make_record(_ASK, "intent"), "_declined": [URL]})
    assert ni_intake.reuse(store, item_id, _ASK, dict(_INTENT)) is None and net.calls == []


def test_a_kept_source_is_not_reused_for_an_ask_naming_another_publisher(lib, monkeypatch) -> None:  # noqa: F811
    """A source kept for one publisher is not reused for an ask that names another, whatever the subject words."""
    store = _store()
    row = ni_intake._new_row(ni_research_flow.normal_url(URL), URL, {
        **_entry(QUESTION, request="BBC earthquakes", title="BBC quakes"),
        "draft": _draft(URL, {"title": "metadata.title"})})
    row["kept"]["entries"] = [{**_entry(QUESTION, request="BBC earthquakes", title="BBC quakes"),
                               "draft": _draft(URL, {"title": "metadata.title"})}]
    ni_intake._write(store, [row])
    Net(monkeypatch, {URL: QUAKES})
    ask = "AP earthquakes"
    item_id = ni_flow.create_shell_item(store, ask)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(ask, "intent"))
    assert ni_intake.reuse(store, item_id, ask, {**_INTENT, "subject": "earthquakes"}) is None


def test_no_to_a_card_from_a_kept_source_forgets_it_for_that_question(client, lib, monkeypatch) -> None:  # noqa: F811
    """A source you said No to is not offered for that question again: its matching entries go, others stay."""
    store, first, _ = _researched(monkeypatch, lib, store=client.app.state.ni)
    assert ni_intake.remember(store, store.get_item(first))
    other = {**_entry({**QUESTION, "kind": "ranking"}), "draft": _draft(URL, {"title": "metadata.title"})}
    rows = library_index.LocalSources(store).list()
    rows[0]["kept"]["entries"].append(other)
    ni_intake._write(store, rows)
    Net(monkeypatch, {URL: QUAKES})
    _, item_id, out, _ = _flow(monkeypatch, _wrap(lib), store=store, seam=Script(), cloud="anthropic/x",
                               consented=True)
    assert out["state"] == "ready"
    assert client.post(f"/api/ni/items/{item_id}/validate", json={"ok": False}).status_code == 200
    assert [e["question"]["kind"] for e in _kept(store)[0]["kept"]["entries"]] == ["ranking"]


def test_forget_drops_a_row_with_nothing_left() -> None:
    store = _store()
    entry = {**_entry(QUESTION), "draft": _draft(URL, {"title": "metadata.title"})}
    row = ni_intake._new_row(ni_research_flow.normal_url(URL), URL, entry)
    row["kept"]["entries"] = [entry]
    ni_intake._write(store, [row])
    item = {"id": "x", "spec": {"goal": _ASK, "source": {"url": URL}, "recipe": {"question": QUESTION}}}
    assert ni_intake.forget(store, item) == 1 and _kept(store) == []
