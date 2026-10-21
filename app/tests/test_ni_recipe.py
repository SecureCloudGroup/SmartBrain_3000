"""S4a: the sealed ``recipe`` block, the researcher-draft rules, export / template handling, the tick seam."""
from __future__ import annotations

import copy
import json
import sys

import pytest
from test_ni_library import _stores, _template

from smartbrain_3000 import ni, ni_flow, ni_library, ni_recipe, ni_routes


def _question(**over) -> dict:
    q = {"kind": "latest_items", "subject": "earthquakes", "places": ["California"], "entities": [],
         "window": {"from": "-1d", "to": None}, "threshold": {"op": ">=", "value": 4.5},
         "order": [{"col": "mag", "dir": "desc"}], "limit": 5, "compare_count": 0, "units": "mag",
         "cadence": "hourly"}
    q.update(over)
    return q


def _recipe(**over) -> dict:
    r = {"question": _question(),
         "sources": [{"kind": "endpoint", "ref": "https://example.org/feed.json", "host": "example.org",
                      "why": "a published JSON feed"}],
         "constraints": [{"check": "window", "value": "-1d"}, {"check": "threshold", "value": 4.5}],
         "tier": "verified_endpoint",
         "verification": {"verdict": "pass", "checks": [{"check": "window", "ok": True, "reason": ""}],
                          "verified_at": "2026-10-21T00:00:00+00:00", "fetches": 2},
         "provenance": {"author": "flow", "built_at": "2026-10-21T00:00:00+00:00"},
         "research": {"turns": [{"tool": "fetch_sample", "arg": "feed", "outcome": "ok"}],
                      "budget": {"turns": 1, "fetches": 1, "searches": 0, "seconds": 3}}}
    r.update(over)
    return r


def _spec(**over) -> dict:
    spec = copy.deepcopy(_template()["spec_template"])
    spec["source"] = {"type": "http_json", "url": "https://example.org/feed.json"}
    spec["params"] = {}
    spec.update(over)
    return spec


def test_valid_recipe_passes_and_old_spec_without_it_is_unchanged() -> None:
    assert ni.validate_spec(_spec(recipe=_recipe()))["recipe"]["tier"] == "verified_endpoint"
    old = _spec()
    assert json.dumps(ni.validate_spec(copy.deepcopy(old)), sort_keys=True) == json.dumps(old, sort_keys=True)
    assert "recipe" not in old


@pytest.mark.parametrize("bad", [
    lambda r: r.update(tier="gold"),
    lambda r: r.update(extra=1),
    lambda r: r["question"].update(kind="astrology"),
    lambda r: r["question"].update(surprise=1),
    lambda r: r["question"].update(places=["x"] * 9),
    lambda r: r["question"].update(cadence="every_second"),
    lambda r: r["question"].update(limit=101),
    lambda r: r.update(sources=[]),
    lambda r: r["sources"][0].update(why="x" * 201),
    lambda r: r["constraints"].append({"check": "vibes", "value": None}),
    lambda r: r["verification"].update(verdict="maybe"),
    lambda r: r["provenance"].update(author="model"),
    lambda r: r["research"]["turns"].append({"tool": "shell", "arg": "", "outcome": ""}),
    lambda r: r["research"]["turns"].extend([{"tool": "web_search", "arg": "a", "outcome": "b"}] * 12),
])
def test_recipe_is_closed_and_bounded(bad) -> None:
    r = _recipe()
    bad(r)
    with pytest.raises(ValueError, match="recipe"):
        ni.validate_spec(_spec(recipe=r))


def test_research_slot_is_size_capped() -> None:
    turns = [{"tool": "web_search", "arg": "a" * 200, "outcome": "b" * 200}] * ni_recipe.MAX_TURNS
    big = {"turns": turns, "budget": {"turns": 12, "fetches": 0, "searches": 3, "seconds": 9}}
    assert len(json.dumps(big)) > ni_recipe.MAX_RESEARCH_BYTES
    with pytest.raises(ValueError, match="exceeds"):
        ni.validate_spec(_spec(recipe=_recipe(research=big)))


def _researcher(**over) -> dict:
    return _spec(recipe=_recipe(provenance={"author": "researcher", "built_at": "2026-10-21T00:00:00+00:00"}),
                 **over)


@pytest.mark.parametrize(("over", "word"), [
    ({"pipeline": [{"op": "llm", "instruction": "x", "output": {"a": "string"}}]}, "llm"),
    ({"source": {"type": "model", "instruction": "hi"}}, "model"),
    ({"source": {"type": "mcp_tool", "server_id": "s", "tool": "t", "arguments": {}}}, "mcp_tool"),
    ({"source": {"type": "http_json", "url": "https://example.org/x",
                 "headers": {"X-K": {"$secret": "ni:self:k"}}}}, "secret"),
    ({"params": {"k": {"label": "K", "kind": "secret", "value": "ni:self:k"}}}, "secret"),
    ({"interval_minutes": 5}, "interval"),
])
def test_researcher_draft_refuses_the_dangerous_shapes(over, word) -> None:
    with pytest.raises(ValueError, match=word):
        ni.validate_spec(_researcher(**over), allow_empty_params=True)


def test_researcher_flag_applies_without_a_marker_and_marker_without_flag() -> None:
    llm = {"pipeline": [{"op": "llm", "instruction": "x", "output": {"a": "string"}}]}
    with pytest.raises(ValueError, match="llm"):
        ni.validate_spec(_spec(**llm), researcher=True)
    assert ni.validate_spec(_researcher())["recipe"]["provenance"]["author"] == "researcher"
    # a human / flow-authored spec keeps these shapes
    assert ni.validate_spec(_spec(interval_minutes=60, recipe=_recipe()))["interval_minutes"] == 60


def test_export_strips_the_whole_recipe_including_the_research_trace() -> None:
    secrets, store, _, _ = _stores()
    iid = store.add_item(_spec(recipe=_recipe()), {"title": "t"})
    exported = ni_routes._sanitize_spec_for_export(store.get_item(iid), secrets)
    assert "recipe" in ni_routes._EXPORT_STRIP_KEYS
    assert "recipe" not in exported and "research" not in json.dumps(exported)


def test_template_with_a_recipe_or_research_trace_is_refused() -> None:
    for carried in (_recipe(), {"research": _recipe()["research"]}):
        t = _template()
        t["spec_template"]["recipe"] = carried
        with pytest.raises(ni_library.LibraryError, match="system-owned keys"):
            ni_library._validate_one_template(t, 0, set())
    assert "recipe" in ni_library._TEMPLATE_STRIP_KEYS
    installed = ni_library.build_installed_spec(_template(), {"zip": "90210"})
    assert "recipe" not in installed


def test_old_specs_load_validate_store_and_export_byte_identically() -> None:
    secrets, store, _, _ = _stores()
    corpus = [_template()["spec_template"], _spec(), _spec(interval_minutes=15),
              _spec(pipeline=[{"op": "llm", "instruction": "x", "output": {"a": "string"}}])]
    for old in corpus:
        before = json.dumps(old, sort_keys=True)
        checked = ni.validate_spec(copy.deepcopy(old), allow_empty_params=True)
        assert json.dumps(checked, sort_keys=True) == before
        iid = store.add_item(copy.deepcopy(old), {"title": "t"}) if old["params"] == {} else None
        if iid:
            stored = store.get_item(iid)["spec"]
            assert "recipe" not in stored
            assert all(json.dumps(stored.get(k), sort_keys=True) == json.dumps(old.get(k), sort_keys=True)
                       for k in old)
            exported = ni_routes._sanitize_spec_for_export(store.get_item(iid), secrets)
            assert json.dumps(exported, sort_keys=True) == json.dumps(
                {k: v for k, v in old.items() if k not in ni_routes._EXPORT_STRIP_KEYS}, sort_keys=True)


def test_ni_does_not_import_ni_flow_at_tick_time() -> None:
    import inspect
    assert "ni_flow" not in inspect.getsource(ni.run_item)
    assert ni._TICK_UPGRADE is ni_flow.upgrade_pre_f1_literal_dates
    ni.set_tick_upgrade(None)
    try:
        assert ni._TICK_UPGRADE is None
    finally:
        ni.set_tick_upgrade(ni_flow.upgrade_pre_f1_literal_dates)
    assert "smartbrain_3000.ni_flow" in sys.modules
