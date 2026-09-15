"""Tests for the bundled Neural Interface RECIPE catalog (§18/§26) and its OBSERVE tool.

The catalog file is reviewed = trusted, so shape drift is a BUILD-time error: a bad
entry makes ``smartbrain_3000.ni_catalog`` fail to import. Importing the module here
IS the primary test — the assertions below cover the invariants the format contract
promises (URL shape parity with ni.py, non-empty notes / docs_url, tool registered
as OBSERVE with no egress, category filter behavior, unknown category is empty,
and — §26 — every recipe's spec_template validates + preview binds + sample runs
its pipeline).
"""

from __future__ import annotations

import pytest

from smartbrain_3000 import ni, ni_catalog, tools

_REQUIRED = {"id", "title", "host", "url_template", "docs_url", "auth",
             "category", "notes", "spec_template", "preview_payload"}
_ALLOWED_OPTIONAL = {"sample_response", "prove_params", "geocode_fills"}


def test_catalog_loads_at_import_and_is_non_empty() -> None:
    all_entries = ni_catalog.entries()
    assert all_entries, "bundled catalog must have at least one recipe"
    assert isinstance(all_entries, list) and all(isinstance(s, dict) for s in all_entries)


def test_every_entry_has_the_required_keys_and_non_empty_notes_and_docs_url() -> None:
    for src in ni_catalog.entries():
        keys = set(src.keys())
        missing = _REQUIRED - keys
        extra = keys - _REQUIRED - _ALLOWED_OPTIONAL
        assert not missing, f"{src.get('id')!r} missing keys: {sorted(missing)}"
        assert not extra, f"{src.get('id')!r} has unknown keys: {sorted(extra)}"
        for key in ("notes", "docs_url"):
            assert isinstance(src[key], str) and src[key].strip(), f"{src['id']}.{key} must be non-empty"
        assert src["auth"] in {"none", "key"}, f"{src['id']}.auth invalid"


def test_every_url_template_passes_the_real_ni_url_shape_validator() -> None:
    """Contract parity: each url_template must pass ``ni._validate_http_json_url_shape``,
    the same private helper spec validation calls at every create/update/commission."""
    for src in ni_catalog.entries():
        ni._validate_http_json_url_shape(src["url_template"])  # raises ValueError on drift


def test_every_url_template_is_https_with_literal_authority() -> None:
    """Belt-and-braces of §3: catalog entries are user-facing; we ship https-only so
    a keyless entry can never train the drafting agent to suggest an http source."""
    for src in ni_catalog.entries():
        url = src["url_template"]
        assert url.startswith("https://"), f"{src['id']}: url_template must be https:// (got {url!r})"
        # Placeholder rule: {{param:X}} may appear only after the authority ends —
        # i.e. inside the path or query, never in the scheme/host/port.
        scheme_end = url.find("://") + 3
        tail = url[scheme_end:]
        stops = [len(tail)] + [tail.find(ch) for ch in "/?#" if tail.find(ch) != -1]
        authority = url[scheme_end:scheme_end + min(stops)]
        assert "{{param:" not in authority, f"{src['id']}: placeholder inside authority {authority!r}"


def test_entries_filter_by_category_returns_only_that_category() -> None:
    for category in ni_catalog.CATEGORIES:
        rows = ni_catalog.entries(category)
        assert rows, f"category {category!r} must have at least one entry (it appears in CATEGORIES)"
        assert all(s["category"] == category for s in rows), f"filter leaked non-{category} rows"


def test_entries_unknown_category_returns_empty_list_not_error() -> None:
    assert ni_catalog.entries("no-such-category") == []


def test_entries_returns_fresh_lists_so_callers_cannot_mutate_the_module_state() -> None:
    a = ni_catalog.entries()
    a.clear()
    assert ni_catalog.entries(), "module state must survive a caller mutating a returned list"


def test_ids_are_unique_across_the_catalog() -> None:
    ids = [s["id"] for s in ni_catalog.entries()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


# --- §26 recipe determinism gates ------------------------------------------

def test_every_recipe_spec_template_validates_with_empty_param_values() -> None:
    """§26/§19: the recipe's spec_template must pass ni.validate_spec in template
    mode (empty param values allowed). Import-time build gate already runs this;
    this test names it in the suite so a regression in the validator surfaces here."""
    for src in ni_catalog.entries():
        ni.validate_spec(src["spec_template"], allow_empty_params=True)


def test_every_recipe_preview_payload_binds_against_its_scene() -> None:
    """§19 gate: preview_payload binds against the recipe's scene using seeded
    history + a preview image_ref — matches the parse_pack contract."""
    for src in ni_catalog.entries():
        spec = src["spec_template"]
        ni.bind_scene(spec["scene"], src["preview_payload"],
                      history=ni._seed_history(spec),
                      image_ref=ni._preview_image_ref(spec, src["id"]))


def test_every_recipe_url_template_matches_spec_template_source_url() -> None:
    """A recipe's operator-facing url_template must equal its spec_template.source.url
    so the deterministic build path fetches exactly what the display promises."""
    for src in ni_catalog.entries():
        spec_url = (src["spec_template"].get("source") or {}).get("url")
        assert spec_url == src["url_template"], (
            f"{src['id']}: url_template != spec_template.source.url"
        )


def test_every_recipe_with_a_sample_response_survives_pipeline_and_bind() -> None:
    """§26 determinism proof: sample_response → run_pipeline → bind_scene end-to-end.
    A recipe that ships a sample must survive its own real-payload shape."""
    for src in ni_catalog.entries():
        sample = src.get("sample_response")
        if sample is None:
            continue
        spec = src["spec_template"]
        outputs = ni.run_pipeline(spec.get("pipeline") or [], sample)
        ni.bind_scene(spec["scene"], outputs,
                      history=ni._seed_history(spec),
                      image_ref=ni._preview_image_ref(spec, src["id"]))


def test_get_recipe_returns_a_fresh_deep_copy() -> None:
    """get_recipe copies so a mutating caller (from_recipe fills param slots)
    cannot corrupt module state."""
    r = ni_catalog.get_recipe("weather-open-meteo")
    assert r is not None
    r["spec_template"]["params"]["latitude"]["value"] = "MUTATED"
    fresh = ni_catalog.get_recipe("weather-open-meteo")
    assert fresh is not None
    assert fresh["spec_template"]["params"]["latitude"]["value"] == ""


def test_get_recipe_unknown_id_returns_none() -> None:
    assert ni_catalog.get_recipe("no-such-recipe") is None
    assert ni_catalog.get_recipe("") is None


# --- OBSERVE tool -------------------------------------------------------------

def test_list_ni_catalog_tool_is_registered_observe_no_egress_and_allowlisted() -> None:
    tool = tools.get_tool("list_ni_catalog")
    assert tool is not None, "list_ni_catalog must be registered"
    assert tool.tier is tools.Tier.OBSERVE
    assert tool.egress is False
    assert "list_ni_catalog" in tools._OBSERVE_READONLY
    # Closed schema: the registry's build-time gate already asserts this, but be explicit.
    schema = tool.params_schema
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["properties"].keys()) == {"category"}


def test_list_ni_catalog_tool_returns_all_entries_without_ctx() -> None:
    """The catalog is static data; the handler ignores ToolContext (no store needed)."""
    tool = tools.get_tool("list_ni_catalog")
    out = tool.handler(tools.ToolContext(), tools.validate_args(tool, {}))
    assert out == {"sources": ni_catalog.entries()}


def test_list_ni_catalog_tool_filters_by_category() -> None:
    tool = tools.get_tool("list_ni_catalog")
    # Pick any category that exists in the shipped catalog — driven by the data itself.
    category = next(iter(ni_catalog.CATEGORIES))
    out = tool.handler(tools.ToolContext(), tools.validate_args(tool, {"category": category}))
    assert out["sources"], f"expected at least one entry for {category!r}"
    assert all(s["category"] == category for s in out["sources"])


def test_list_ni_catalog_tool_unknown_category_is_empty_not_error() -> None:
    tool = tools.get_tool("list_ni_catalog")
    out = tool.handler(tools.ToolContext(), tools.validate_args(tool, {"category": "does-not-exist"}))
    assert out == {"sources": []}


def test_list_ni_catalog_tool_rejects_unknown_arg() -> None:
    """The closed schema refuses stray keys at the gate, so the model can't smuggle args."""
    tool = tools.get_tool("list_ni_catalog")
    with pytest.raises(ValueError):
        tools.validate_args(tool, {"bogus": "x"})
