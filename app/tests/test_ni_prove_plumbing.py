"""Unit test for tools/ni-library/prove.py's request-assembly plumbing.

The prove.py script is a network operator tool (§26): it fetches every recipe's
endpoint LIVE and runs the pipeline. This test exercises the URL / headers /
pipeline / bind glue via a STUBBED fetch so CI never touches the real network
but a regression in the plumbing (percent-encoding, {{param:}} substitution,
header build, secret-env skip) still fails here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_PROVE_PATH = (pathlib.Path(__file__).resolve().parents[2]
               / "tools" / "ni-library" / "prove.py")

# The shipped app image deliberately omits the repo's tools/ tree (operator/CI
# tooling, not app code) — mirror the tooling-tests gate in test_ni_library.py so
# the image-run suite SKIPS cleanly instead of failing on a missing path. When
# the repo root is mounted (the house command), the tests still run.
pytestmark = pytest.mark.skipif(
    not _PROVE_PATH.exists(), reason="prove.py tooling not shipped in the app image")


def _load_prove():
    """Import tools/ni-library/prove.py by path (the file isn't part of any package)."""
    assert _PROVE_PATH.exists(), f"prove.py missing: {_PROVE_PATH}"
    spec = importlib.util.spec_from_file_location("_ni_prove", _PROVE_PATH)
    assert spec is not None and spec.loader is not None, "spec load must succeed"
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ni_prove"] = module
    spec.loader.exec_module(module)
    return module


def test_prove_keyless_recipe_url_substitution_and_pipeline_bind_succeed() -> None:
    """A keyless recipe with a stubbed fetch returning the recipe's own
    sample_response must PASS end-to-end: URL fills, pipeline runs, scene binds.
    """
    prove = _load_prove()
    calls: list[tuple[str, dict]] = []

    def _fake_fetch(url: str, headers: dict) -> object:
        calls.append((url, headers))
        # A sample known to bind for weather-open-meteo.
        return {"current": {"temperature_2m": 21.0, "wind_speed_10m": 4.5}}

    from smartbrain_3000 import ni_catalog
    recipe = ni_catalog.get_recipe("weather-open-meteo")
    assert recipe is not None
    result = prove._prove_one(recipe, fetch=_fake_fetch)
    assert result == "ok", result
    assert len(calls) == 1
    url, headers = calls[0]
    # prove_params default 37.77,-122.42 → percent-encoded via urllib.parse.quote.
    assert "latitude=37.77" in url and "longitude=-122.42" in url
    assert headers == {}


def test_prove_keyed_recipe_without_env_credential_is_skipped() -> None:
    """A keyed recipe with no SB_NI_PROVE_<...>_API_KEY in env returns a SKIP
    string (not a failure). Bounded env: the fixture ensures nothing leaks in.
    """
    prove = _load_prove()
    from smartbrain_3000 import ni_catalog
    recipe = ni_catalog.get_recipe("stock-quote-finnhub")
    assert recipe is not None
    result = prove._prove_one(recipe, fetch=lambda url, headers: {})
    assert result.startswith("skipped"), result


def test_prove_keyed_recipe_with_env_credential_builds_header_and_runs(
        monkeypatch) -> None:
    """When the credential env var IS present, the header rides + the pipeline runs."""
    prove = _load_prove()
    monkeypatch.setenv("SB_NI_PROVE_STOCK_QUOTE_FINNHUB_API_KEY", "sk-test")
    seen_headers: dict = {}

    def _fake_fetch(url: str, headers: dict) -> object:
        seen_headers.update(headers)
        return {"c": 210.5, "h": 212.0, "l": 209.0, "o": 211.0, "pc": 209.5}

    from smartbrain_3000 import ni_catalog
    recipe = ni_catalog.get_recipe("stock-quote-finnhub")
    assert recipe is not None
    # Provide symbol via a stubbed prove_params (recipe's prove_params already has AAPL).
    result = prove._prove_one(recipe, fetch=_fake_fetch)
    assert result == "ok", result
    assert seen_headers.get("X-Finnhub-Token") == "sk-test"


def test_prove_reports_pipeline_failure_when_stub_shape_wrong() -> None:
    """A response the recipe's pipeline cannot extract from should surface a
    per-recipe failure string, not raise."""
    prove = _load_prove()
    from smartbrain_3000 import ni_catalog
    recipe = ni_catalog.get_recipe("crypto-price-btc-usd")
    assert recipe is not None
    result = prove._prove_one(recipe,
                              fetch=lambda url, headers: {"unexpected": "shape"})
    assert result.startswith("pipeline failed"), result
