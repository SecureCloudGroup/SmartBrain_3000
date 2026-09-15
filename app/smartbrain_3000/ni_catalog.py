"""Bundled Neural Interface source catalog (§18) — read-only, loaded at import.

The catalog is the curated ground for "AI suggests, user picks" (creation-flow law
§9): a small set of vetted keyless/free-tier public data endpoints the drafting
agent can suggest by default, with live web research as the labeled fallback.

§26 (2026-09-13): every entry is now a RECIPE — a full §19 template shape
(``spec_template`` + ``preview_payload``) plus optional ``sample_response`` (a
trimmed real API response the recipe's pipeline was written against) and
``prove_params`` (defaults for the live ``tools/ni-library/prove.py`` runner).
This module runs the SAME validators the app uses in production so a drift is a
BUILD-time error: ``ni.validate_spec(allow_empty_params=True)`` on the spec
template, ``ni.bind_scene`` on the preview against the scene, and — when a
``sample_response`` is supplied — the deterministic run of the recipe's own
``pipeline`` against that sample, followed by another bind, so the sample →
pipeline → bind path is proven for every recipe carrying a sample.

Contract: the file at ``data/ni_catalog.json`` ships INSIDE the wheel (see
``pyproject.toml`` package-data). It is reviewed source, so a malformed entry is
a BUILD error, not a runtime condition — this module fails loudly at import if
the shape drifts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import ni

_CATALOG_PATH = Path(__file__).parent / "data" / "ni_catalog.json"

# Structural bounds — a bundled catalog stays small; a runaway file is a build bug.
_MAX_SOURCES = 200
_REQUIRED_KEYS: frozenset[str] = frozenset({
    "id", "title", "host", "url_template", "docs_url", "auth", "category", "notes",
    # §26 recipe fields — required so an entry is always installable
    "spec_template", "preview_payload",
})
# §26 optional fields — validators skip when absent, but reject unknown keys.
# ``geocode_fills`` (geocode-consent, 2026-09-15, operator-approved): maps a
# geocode RESULT field (latitude/longitude only) to a declared non-secret param
# name, so the flow can fill coordinates from a place the user named — behind
# the confirm_source consent card, which discloses the lookup host verbatim.
_OPTIONAL_KEYS: frozenset[str] = frozenset({"sample_response", "prove_params",
                                             "geocode_fills"})
_GEOCODE_RESULT_FIELDS: frozenset[str] = frozenset({"latitude", "longitude"})
_ALLOWED_KEYS: frozenset[str] = _REQUIRED_KEYS | _OPTIONAL_KEYS
_AUTH_VALUES: frozenset[str] = frozenset({"none", "key"})
_MAX_ID = 80
_MAX_TITLE = 200
_MAX_HOST = 253       # RFC 1035 host cap
_MAX_URL = 2000       # matches ni._MAX_URL
_MAX_DOCS_URL = 2000
_MAX_CATEGORY = 40
_MAX_NOTES = 500


def _validate_str_fields(entry: dict, index: int) -> None:
    """Bounded string checks for the operator-facing metadata columns."""
    assert isinstance(entry, dict), "entry must be a dict"
    caps: tuple[tuple[str, int], ...] = (
        ("id", _MAX_ID), ("title", _MAX_TITLE), ("host", _MAX_HOST),
        ("url_template", _MAX_URL), ("docs_url", _MAX_DOCS_URL),
        ("category", _MAX_CATEGORY), ("notes", _MAX_NOTES),
    )
    for key, cap in caps:  # bounded by caps
        value = entry[key]
        assert isinstance(value, str) and value, (
            f"sources[{index}].{key} must be a non-empty string"
        )
        assert len(value) <= cap, f"sources[{index}].{key} exceeds {cap} chars"


def _validate_recipe_spec(entry: dict, index: int) -> dict:
    """Run the real §2 spec validator on the recipe's ``spec_template`` (empty-params mode)
    and bind the ``preview_payload`` against its scene — the same two gates ``parse_pack``
    runs on a library template (§19).
    """
    spec_template = entry["spec_template"]
    assert isinstance(spec_template, dict), (
        f"sources[{index}].spec_template must be an object"
    )
    try:
        validated = ni.validate_spec(spec_template, allow_empty_params=True)
    except ValueError as exc:
        raise AssertionError(
            f"sources[{index}].spec_template invalid: {exc}"
        ) from None
    preview = entry["preview_payload"]
    assert isinstance(preview, dict), (
        f"sources[{index}].preview_payload must be an object"
    )
    image_ref = ni._preview_image_ref(validated, entry["id"])
    try:
        ni.bind_scene(validated["scene"], preview,
                      history=ni._seed_history(validated),
                      image_ref=image_ref)
    except (ni.NIError, ValueError) as exc:
        raise AssertionError(
            f"sources[{index}].preview_payload does not bind: {exc}"
        ) from None
    return validated


def _validate_recipe_sample(entry: dict, index: int, validated_spec: dict) -> None:
    """§26 determinism proof: when ``sample_response`` is present, run the recipe's
    pipeline against it and re-bind — the recipe must survive its own real-payload
    shape end-to-end at import.

    A malformed sample makes the catalog fail to load, exactly like a bad spec
    would. Kept OUT of the recipe surface when a real response shape is genuinely
    hard to nail down without a live probe.
    """
    sample = entry.get("sample_response")
    if sample is None:
        return
    assert isinstance(sample, (dict, list)), (
        f"sources[{index}].sample_response must be a JSON object or list"
    )
    stages = validated_spec.get("pipeline") or []
    try:
        outputs = ni.run_pipeline(stages, sample)
    except ni.NIError as exc:
        raise AssertionError(
            f"sources[{index}].sample_response failed the pipeline "
            f"({exc.kind}: {exc.detail})"
        ) from None
    image_ref = ni._preview_image_ref(validated_spec, entry["id"])
    try:
        ni.bind_scene(validated_spec["scene"], outputs,
                      history=ni._seed_history(validated_spec),
                      image_ref=image_ref)
    except (ni.NIError, ValueError) as exc:
        raise AssertionError(
            f"sources[{index}].sample_response binds the pipeline output "
            f"but fails the scene bind: {exc}"
        ) from None


def _validate_prove_params(entry: dict, index: int, validated_spec: dict) -> None:
    """Shape gate for the optional ``prove_params`` block used by tools/ni-library/prove.py.

    Each key must name a declared param on the spec (unknown = build error), and
    values must be string/number literals — the same shape ``NIStore.add_item``
    would accept via the install path.
    """
    prove = entry.get("prove_params")
    if prove is None:
        return
    assert isinstance(prove, dict), (
        f"sources[{index}].prove_params must be an object"
    )
    declared = (validated_spec.get("params") or {})
    for name, value in prove.items():  # bounded by ni._MAX_PARAMS via validated_spec
        assert name in declared, (
            f"sources[{index}].prove_params.{name!r} is not a declared spec param"
        )
        assert (isinstance(value, (str, int, float))
                and not isinstance(value, bool)), (
            f"sources[{index}].prove_params.{name} must be a string or number"
        )


def _validate_entry(entry: object, index: int) -> dict:
    """Shape-check one catalog recipe row end-to-end (§26).

    Import-time build gate: a bad row is a compile-error-shaped assertion (the file
    is in-repo, reviewed by a human), never a caught runtime condition. Runs the
    same validators the library-pack loader uses at install so a recipe shipped
    here can never claim to be installable but fail parse_pack at subscriber-side.
    """
    assert isinstance(entry, dict), f"sources[{index}] must be an object"
    missing = _REQUIRED_KEYS - set(entry.keys())
    assert not missing, f"sources[{index}] missing keys: {sorted(missing)}"
    extra = set(entry.keys()) - _ALLOWED_KEYS
    assert not extra, f"sources[{index}] unknown keys: {sorted(extra)}"
    _validate_str_fields(entry, index)
    assert entry["auth"] in _AUTH_VALUES, (
        f"sources[{index}].auth must be one of {sorted(_AUTH_VALUES)}"
    )
    # The load-bearing checks: real §3 URL shape on ``url_template`` (kept for the
    # list_ni_catalog display + parity with prior behavior), then the recipe's
    # spec_template through the FULL §2 validator, then the preview bind.
    try:
        ni._validate_http_json_url_shape(entry["url_template"])
    except ValueError as exc:
        raise AssertionError(f"sources[{index}].url_template: {exc}") from None
    validated_spec = _validate_recipe_spec(entry, index)
    # geocode_fills (2026-09-15): closed map {geocode result field -> param name};
    # every target must be a declared NON-secret param of the spec_template.
    if "geocode_fills" in entry:
        fills = entry["geocode_fills"]
        assert isinstance(fills, dict) and fills, (
            f"sources[{index}].geocode_fills must be a non-empty object")
        declared = validated_spec.get("params") or {}
        for field, param_name in fills.items():
            assert field in _GEOCODE_RESULT_FIELDS, (
                f"sources[{index}].geocode_fills key {field!r} not a geocode field")
            decl = declared.get(param_name)
            assert isinstance(decl, dict) and decl.get("kind") != "secret", (
                f"sources[{index}].geocode_fills target {param_name!r} must be a "
                "declared non-secret param")
    # A recipe's ``url_template`` MUST match its ``spec_template.source.url`` so a
    # reader of the operator-facing display sees exactly the URL the deterministic
    # build path would fetch (drift here would silently mis-describe the recipe).
    spec_url = (validated_spec.get("source") or {}).get("url")
    assert isinstance(spec_url, str) and spec_url == entry["url_template"], (
        f"sources[{index}].url_template must equal spec_template.source.url"
    )
    _validate_recipe_sample(entry, index, validated_spec)
    _validate_prove_params(entry, index, validated_spec)
    return entry


def _load() -> tuple[dict, ...]:
    """Read + validate the bundled catalog file once at import — fail loudly on drift."""
    assert _CATALOG_PATH.exists(), f"catalog file missing: {_CATALOG_PATH}"
    raw = _CATALOG_PATH.read_text(encoding="utf-8")
    assert raw, "catalog file is empty"
    data = json.loads(raw)
    assert isinstance(data, dict), "catalog root must be a JSON object"
    assert data.get("version") == 1, "catalog version must be 1"
    sources = data.get("sources")
    assert isinstance(sources, list) and sources, (
        "catalog must have a non-empty 'sources' list"
    )
    assert len(sources) <= _MAX_SOURCES, f"catalog exceeds {_MAX_SOURCES} entries"
    seen_ids: set[str] = set()
    validated: list[dict] = []
    for i, entry in enumerate(sources):  # bounded by _MAX_SOURCES
        row = _validate_entry(entry, i)
        assert row["id"] not in seen_ids, (
            f"sources[{i}].id duplicate: {row['id']!r}"
        )
        seen_ids.add(row["id"])
        validated.append(row)
    return tuple(validated)


_SOURCES: tuple[dict, ...] = _load()
CATEGORIES: frozenset[str] = frozenset(s["category"] for s in _SOURCES)


def entries(category: str | None = None) -> list[dict]:
    """Return the catalog entries (a fresh list of dicts), optionally filtered by category.

    An unknown ``category`` returns an empty list — never an error — so the tool
    surface can pass through whatever the model asked for without needing to
    pre-check membership.
    """
    assert category is None or isinstance(category, str), (
        "category must be a string or None"
    )
    assert _SOURCES, "catalog empty — module failed to load"
    if category is None:
        return [_deep_copy(s) for s in _SOURCES]  # bounded by _MAX_SOURCES
    return [_deep_copy(s) for s in _SOURCES if s["category"] == category]


def get_recipe(recipe_id: str) -> dict | None:
    """Look up one recipe by id (§26). Returns a fresh deep copy or ``None``.

    Used by the ``create_ni_item_from_recipe`` tool: the caller mutates the
    returned dict to fill param slots, so the module's own state must never move.
    """
    assert isinstance(recipe_id, str), "recipe_id must be a string"
    assert _SOURCES, "catalog empty — module failed to load"
    if not recipe_id:
        return None
    for src in _SOURCES:  # bounded by _MAX_SOURCES
        if src["id"] == recipe_id:
            return _deep_copy(src)
    return None


def _deep_copy(value: Any) -> Any:
    """Deep-copy a JSON-shaped value via a round-trip (protects module state)."""
    assert value is not None, "value must not be None"
    return json.loads(json.dumps(value))
