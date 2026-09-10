"""Bundled Neural Interface source catalog (§18) — read-only, loaded at import.

The catalog is the curated ground for "AI suggests, user picks" (creation-flow law
§9): a small set of vetted keyless/free-tier public data endpoints the drafting
agent can suggest by default, with live web research as the labeled fallback.

Contract: the file at ``data/ni_catalog.json`` ships INSIDE the wheel (see
``pyproject.toml`` package-data). It is reviewed source, so a malformed entry is
a BUILD error, not a runtime condition — this module fails loudly at import if
the shape drifts. Every ``url_template`` is validated against the real §3 URL
shape rule via ``ni._validate_http_json_url_shape`` (private helper, imported the
same way ``tools.py`` reaches ``ni._PARAM_PLACEHOLDER`` / ``ni._seed_history`` —
sibling-module discipline, since editing ni.py is out of scope here).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ni

_CATALOG_PATH = Path(__file__).parent / "data" / "ni_catalog.json"

# Structural bounds — a bundled catalog stays small; a runaway file is a build bug.
_MAX_SOURCES = 200
_REQUIRED_KEYS: frozenset[str] = frozenset({
    "id", "title", "host", "url_template", "docs_url", "auth", "category", "notes",
})
_AUTH_VALUES: frozenset[str] = frozenset({"none", "key"})
_MAX_ID = 80
_MAX_TITLE = 200
_MAX_HOST = 253       # RFC 1035 host cap
_MAX_URL = 2000       # matches ni._MAX_URL
_MAX_DOCS_URL = 2000
_MAX_CATEGORY = 40
_MAX_NOTES = 500


def _validate_entry(entry: object, index: int) -> dict:
    """Shape-check one catalog row + run the real §3 URL-shape validator on its template.

    Import-time build gate: a bad row is a compile-error-shaped assertion (the file
    is in-repo, reviewed by a human), never a caught runtime condition.
    """
    assert isinstance(entry, dict), f"sources[{index}] must be an object"
    assert _REQUIRED_KEYS <= set(entry.keys()), (
        f"sources[{index}] missing keys: {_REQUIRED_KEYS - set(entry.keys())}"
    )
    extra = set(entry.keys()) - _REQUIRED_KEYS
    assert not extra, f"sources[{index}] unknown keys: {extra}"
    caps: tuple[tuple[str, int], ...] = (
        ("id", _MAX_ID), ("title", _MAX_TITLE), ("host", _MAX_HOST),
        ("url_template", _MAX_URL), ("docs_url", _MAX_DOCS_URL),
        ("category", _MAX_CATEGORY), ("notes", _MAX_NOTES),
    )
    for key, cap in caps:  # bounded by _REQUIRED_KEYS
        value = entry[key]
        assert isinstance(value, str) and value, f"sources[{index}].{key} must be a non-empty string"
        assert len(value) <= cap, f"sources[{index}].{key} exceeds {cap} chars"
    assert entry["auth"] in _AUTH_VALUES, (
        f"sources[{index}].auth must be one of {sorted(_AUTH_VALUES)}"
    )
    # The load-bearing check: run ni.py's own §3 URL-shape validator so a catalog
    # entry can never suggest a URL a real spec would refuse.
    try:
        ni._validate_http_json_url_shape(entry["url_template"])
    except ValueError as exc:
        raise AssertionError(f"sources[{index}].url_template: {exc}") from None
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
    assert isinstance(sources, list) and sources, "catalog must have a non-empty 'sources' list"
    assert len(sources) <= _MAX_SOURCES, f"catalog exceeds {_MAX_SOURCES} entries"
    seen_ids: set[str] = set()
    validated: list[dict] = []
    for i, entry in enumerate(sources):  # bounded by _MAX_SOURCES
        row = _validate_entry(entry, i)
        assert row["id"] not in seen_ids, f"sources[{i}].id duplicate: {row['id']!r}"
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
    assert category is None or isinstance(category, str), "category must be a string or None"
    assert _SOURCES, "catalog empty — module failed to load"
    if category is None:
        return [dict(s) for s in _SOURCES]  # bounded by _MAX_SOURCES
    return [dict(s) for s in _SOURCES if s["category"] == category]
