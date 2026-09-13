#!/usr/bin/env python3
"""Live end-to-end prove for every Neural Interface recipe (§26).

Operator tool — NOT an app path. Runs OUTSIDE netguard on purpose so a real
public API can be fetched with plain ``httpx``: the recipes ship deterministic
extract paths and every one already round-trips its ``sample_response`` at
import (see ``ni_catalog._validate_recipe_sample``), so this script's job is to
prove the CURRENT REAL PAYLOAD still matches — the same guard the future
registry CI will run nightly.

Usage:
    python tools/ni-library/prove.py
    python tools/ni-library/prove.py --only weather-open-meteo,fx-usd-eur

Exit code is 0 when every enabled recipe passes, 1 otherwise. A recipe whose
``auth`` is ``"key"`` is SKIPPED unless the corresponding environment variable
is present (``SB_NI_PROVE_<RECIPE_ID_UPPER>_API_KEY`` — the credential rides
the same ``X-Finnhub-Token`` / equivalent header the recipe declares).

Each recipe is validated by ``ni_catalog`` at import; this script uses the
recipe's ``prove_params`` block for defaults so a keyless recipe requires no
arguments. The unit-test companion (``test_ni_prove_plumbing.py``) covers the
recipe->request assembly with a stubbed fetch so the plumbing is exercised in
CI without any network dependency.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from typing import Any

# The app package must be importable — add repo/app to sys.path.
_REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000 import ni, ni_catalog

_PARAM_RE = re.compile(r"\{\{param:([A-Za-z_][A-Za-z0-9_-]*)\}\}")
_TIMEOUT_S = 10.0


def _resolve_url(recipe: dict, values: dict) -> str:
    """Substitute {{param:X}} in the recipe's ``url_template`` from ``values``.

    Uses the same percent-encoding rule the engine does (``urllib.parse.quote``,
    ``safe=""``) so the probed URL matches the byte shape the engine would send.
    Missing params raise so the operator sees which recipe is under-specified.
    """
    from urllib.parse import quote

    assert isinstance(recipe, dict) and isinstance(values, dict), "recipe + values"
    url = recipe["url_template"]

    def _one(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            raise ValueError(f"recipe {recipe['id']!r} missing prove param {name!r}")
        return quote(str(values[name]), safe="")

    return _PARAM_RE.sub(_one, url)


def _resolve_headers(recipe: dict, secret_values: dict) -> dict:
    """Build a headers dict from the recipe's ``spec_template.source.headers``.

    ``$secret`` refs are replaced with the value from ``secret_values[<name>]``
    (looked up via the ni:self:<name> tail); headers with no matching secret in
    the environment are DROPPED so a keyless recipe passes through cleanly.
    """
    assert isinstance(recipe, dict) and isinstance(secret_values, dict), "args"
    src = recipe["spec_template"].get("source") or {}
    headers = src.get("headers") or {}
    out: dict[str, str] = {}
    for name, value in headers.items():  # bounded by ni._MAX_HEADERS
        if isinstance(value, dict) and "$secret" in value:
            ref = str(value["$secret"])
            key = ref.split(":")[-1]
            value_str = secret_values.get(key)
            if value_str:
                out[name] = str(value_str)
        elif isinstance(value, str):
            out[name] = value
    return out


def _secret_env(recipe_id: str, name: str) -> str | None:
    """Return the environment variable for one recipe's secret param, or None."""
    assert recipe_id and name, "recipe + name required"
    slug = recipe_id.upper().replace("-", "_")
    return os.environ.get(f"SB_NI_PROVE_{slug}_{name.upper()}")


def _collect_secrets(recipe: dict) -> tuple[dict[str, str], list[str]]:
    """Gather secret values from the environment; return (values, missing_names)."""
    assert isinstance(recipe, dict), "recipe required"
    params = recipe["spec_template"].get("params") or {}
    values: dict[str, str] = {}
    missing: list[str] = []
    for name, decl in params.items():  # bounded by ni._MAX_PARAMS
        if not (isinstance(decl, dict) and decl.get("kind") == "secret"):
            continue
        raw = _secret_env(recipe["id"], name)
        if raw:
            values[name] = raw
        else:
            missing.append(name)
    return values, missing


def _prove_one(recipe: dict, *, fetch=None) -> str:
    """Run the recipe's pipeline against a LIVE response; return ``ok`` or a message.

    ``fetch`` (unit-test seam): a callable ``(url, headers) -> dict`` that returns
    the parsed JSON. Real runs default to the ``httpx``-based fetch below; tests
    inject a fake so the plumbing (URL resolve, headers, pipeline, bind) can be
    exercised without network.
    """
    assert isinstance(recipe, dict), "recipe required"
    secret_values, missing = _collect_secrets(recipe)
    if missing:
        return f"skipped (no env credential for: {', '.join(missing)})"
    prove_params: dict[str, Any] = dict(recipe.get("prove_params") or {})
    try:
        url = _resolve_url(recipe, prove_params)
    except ValueError as exc:
        return f"prove-params error: {exc}"
    headers = _resolve_headers(recipe, secret_values)
    fetcher = fetch if fetch is not None else _http_fetch
    try:
        payload = fetcher(url, headers)
    except Exception as exc:  # any fetch/parse issue is a per-recipe failure
        return f"fetch failed: {exc.__class__.__name__}: {exc}"
    stages = recipe["spec_template"].get("pipeline") or []
    try:
        outputs = ni.run_pipeline(stages, payload)
    except ni.NIError as exc:
        return f"pipeline failed: {exc.kind}: {exc.detail}"
    scene = recipe["spec_template"].get("scene") or {}
    try:
        ni.bind_scene(scene, outputs,
                      history=ni._seed_history(recipe["spec_template"]),
                      image_ref=ni._preview_image_ref(recipe["spec_template"],
                                                       recipe["id"]))
    except (ni.NIError, ValueError) as exc:
        return f"bind failed: {exc}"
    return "ok"


def _http_fetch(url: str, headers: dict) -> object:
    """Plain httpx.get with a bounded timeout — no netguard (operator tool).

    Imported inside the function so the unit test doesn't need httpx installed
    to exercise the plumbing via the ``fetch`` seam.
    """
    assert isinstance(url, str) and url, "url required"
    assert isinstance(headers, dict), "headers must be a dict"
    import httpx

    with httpx.Client(timeout=_TIMEOUT_S) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI arg parser: an optional --only comma-list to prove a subset."""
    parser = argparse.ArgumentParser(
        description="Live-prove every Neural Interface recipe end-to-end.",
    )
    parser.add_argument(
        "--only", type=str, default="",
        help="Comma-separated recipe ids to run (default: every recipe)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Prove every enabled recipe; return 0 on all-pass, 1 otherwise."""
    args = _parse_args(argv)
    allowlist = {s.strip() for s in args.only.split(",") if s.strip()}
    recipes = ni_catalog.entries()
    if allowlist:
        recipes = [r for r in recipes if r["id"] in allowlist]
    failed = 0
    for recipe in recipes:  # bounded by _MAX_SOURCES
        result = _prove_one(recipe)
        status = "PASS" if result == "ok" else (
            "SKIP" if result.startswith("skipped") else "FAIL"
        )
        print(f"{status} {recipe['id']}: {result}")
        if status == "FAIL":
            failed += 1
    print(f"\n{len(recipes) - failed} passed/skipped, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
