"""Parametrized regression over the NI case registry.

Every row in ``app/tests/fixtures/ni_flow/cases.json`` is driven through the
real ``smartbrain_3000.ni_flow.run_flow`` here, using a scripted gateway and a
fixture-backed (or scripted-raising) fetcher. The final ``flow_state`` must
equal each row's ``expected.engine_state`` — so any new case added to the
registry becomes a PR-time regression automatically, hermetic and offline.

The eval tool at ``tools/ni-flow-eval.py`` is imported for its
``_pick_ground_paths`` helper (loaded by path — the file lives outside the
package). The house image ships the tool alongside the app, so this suite is
skipped cleanly when the tool is absent (parity with ``test_ni_flow_eval_plumbing``).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from collections.abc import Callable

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow
from smartbrain_3000.secrets import gen_master_key

_REPO = pathlib.Path(__file__).resolve().parents[2]
_EVAL_PATH = _REPO / "tools" / "ni-flow-eval.py"
_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ni_flow"
_REGISTRY_PATH = _FIXTURES / "cases.json"

pytestmark = pytest.mark.skipif(
    not _EVAL_PATH.exists() or not _REGISTRY_PATH.exists(),
    reason="ni-flow-eval tooling or registry not shipped in the app image")


def _load_eval_module():
    """Import tools/ni-flow-eval.py by path (the file isn't part of any package).

    Returns None when tools/ is absent — the SHIPPED docker image carries
    app/ only, and this module must still COLLECT there (the pytestmark
    skipif then skips every test; module-level code cannot rely on it).
    """
    if not _EVAL_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("_ni_flow_eval", _EVAL_PATH)
    assert spec is not None and spec.loader is not None, "spec load must succeed"
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ni_flow_eval"] = module
    spec.loader.exec_module(module)
    return module


_EVAL = _load_eval_module()


def _load_registry() -> list[dict]:
    """Read the registry JSON. Bounded shape; ``load_registry`` already validated."""
    assert _REGISTRY_PATH.exists(), f"registry missing: {_REGISTRY_PATH}"
    with _REGISTRY_PATH.open(encoding="utf-8") as fp:
        cases = json.load(fp)
    assert isinstance(cases, list) and cases, "registry must be a non-empty list"
    return cases


def _testable(case: dict) -> bool:
    """Rows to parametrize: fixture-backed OR url-null/failed-class.

    Image rows exit — the engine's image path is exercised by ``prove.py`` on
    shipped recipes, not by ``run_flow`` in this suite.
    """
    assert isinstance(case, dict), "case required"
    if case["klass"] == "image":
        return False
    expected_state = str((case["expected"] or {}).get("engine_state") or "")
    if expected_state in ("failed", "source"):
        return True
    if case.get("fixture") is not None:
        return True
    return case.get("url") is None  # url-null and not failed/source is still ok


# Collection-safe in the shipped image (no tools/, possibly no registry): an
# empty parametrize list collects zero tests and the pytestmark skip covers
# the rest — module-level code must never assert paths that only exist in a
# repo checkout.
_CASES: list[dict] = (
    [c for c in _load_registry() if _testable(c)]
    if (_EVAL is not None and _REGISTRY_PATH.exists()) else []
)


def _store() -> tuple[nimod.NIStore, duckdb.DuckDBPyConnection]:
    """Fresh in-memory NIStore for one test."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key()), conn


def _scripted_gateway(replies: list[str]) -> Callable[[str, str], str]:
    """Build a ``(model, prompt) -> reply`` callable that pops from ``replies``."""
    assert isinstance(replies, list), "replies list required"
    remaining = list(replies)

    def _call(model: str, prompt: str) -> str:
        assert isinstance(model, str) and isinstance(prompt, str), "args required"
        assert remaining, "scripted gateway exhausted its replies"
        return remaining.pop(0)

    return _call


def _lan_or_dead_fetcher(kind: str) -> Callable[[str], object]:
    """Scripted fetcher for LAN / dead-endpoint rows.

    Runs OFFLINE by design: netguard's runtime refusal + a 404 both surface
    the flow's ``failed(fetch)`` class the same way (a bare exception raised
    from the fetcher). Verifying the exact netguard branch is a separate
    suite's job — here we assert the flow degrades honestly on ANY fetch
    exception, which is the safety-edge contract in ni-cases.md §B.
    """
    assert kind in ("lan", "dead"), "unknown kind"

    def _raise(url: str) -> object:
        assert isinstance(url, str), "url required"
        if kind == "lan":
            raise PermissionError(
                f"netguard-refused (simulated): {url} is a private/reserved host")
        raise ConnectionError(f"HTTP 404 (simulated): {url} does not exist")

    return _raise


def _xml_fetcher(case: dict) -> Callable[[str], object]:
    """Fetcher for the xml-not-json row: read the raw fixture, then json.loads it.

    The fixture ships as raw XML bytes (recorded live from BBC's RSS endpoint).
    ``json.loads`` on those bytes raises ``JSONDecodeError`` — exactly the
    class ``_sample_and_map`` catches and stamps as ``failed(fetch)``.
    """
    assert isinstance(case, dict), "case required"
    fixture_name = case.get("fixture")
    assert isinstance(fixture_name, str) and fixture_name, "fixture required"
    fixture_path = _FIXTURES / fixture_name

    def _fetch(url: str) -> object:
        assert isinstance(url, str), "url required"
        raw = fixture_path.read_bytes()
        return json.loads(raw)  # this raise IS the failed-fetch signal

    return _fetch


def _happy_fetcher(case: dict) -> Callable[[str], object]:
    """Fetcher for a happy row: returns the JSON fixture verbatim on every call."""
    assert isinstance(case, dict), "case required"
    fixture_name = case.get("fixture")
    assert isinstance(fixture_name, str), "happy row must have a fixture"
    with (_FIXTURES / fixture_name).open(encoding="utf-8") as fp:
        payload = json.load(fp)

    def _fetch(url: str) -> object:
        assert isinstance(url, str), "url required"
        return payload

    return _fetch


def _mapping_reply_for(case: dict) -> str:
    """Derive the mapping reply from the fixture via the eval's ground picker."""
    assert isinstance(case, dict), "case required"
    fixture_name = case.get("fixture")
    assert isinstance(fixture_name, str), "mapping stage needs a JSON fixture"
    with (_FIXTURES / fixture_name).open(encoding="utf-8") as fp:
        sample = json.load(fp)
    cands = ni_flow.derive_paths(sample)
    assert cands, f"no candidates derived from {fixture_name}"
    inferred = ni_flow.infer_fields(case["intent"])
    reconciled = ni_flow.reconcile_field_types(inferred, cands)
    mapping = _EVAL._pick_ground_paths(cands, reconciled)
    return json.dumps(mapping)


def _pytest_source_url(case: dict) -> str | None:
    """Which URL to pass as ``source_url`` on this row's ``run_flow`` call."""
    assert isinstance(case, dict), "case required"
    override = case.get("pytest_source_url")
    if isinstance(override, str) and override:
        return override
    url = case.get("url")
    return url if isinstance(url, str) else None


def _drive_flow(case: dict) -> dict:
    """Set up a fresh store + a per-row fetcher / gateway, then run the flow."""
    assert isinstance(case, dict), "case required"
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, case["request"])
    intent_reply = json.dumps(case["intent"])
    expected_state = str((case["expected"] or {}).get("engine_state") or "")

    if case["id"] == "lan-refused":
        fetcher = _lan_or_dead_fetcher("lan")
        gateway = _scripted_gateway([intent_reply])
    elif case["id"] == "dead-endpoint":
        fetcher = _lan_or_dead_fetcher("dead")
        gateway = _scripted_gateway([intent_reply])
    elif case["id"] == "xml-not-json":
        fetcher = _xml_fetcher(case)
        gateway = _scripted_gateway([intent_reply])
    elif expected_state == "source":
        # No fetch happens (pause at AWAITING_SOURCE_PICK). One reply is enough.
        fetcher = lambda url: {}  # unused
        gateway = _scripted_gateway([intent_reply])
    elif case["klass"] == "refuse":
        # Computed branch: intent classifies computed_only, then _handle_computed
        # returns unsupported without a mapping stage.
        fetcher = lambda url: {}
        gateway = _scripted_gateway([intent_reply])
    else:
        fetcher = _happy_fetcher(case)
        gateway = _scripted_gateway([intent_reply, _mapping_reply_for(case)])

    result = ni_flow.run_flow(
        store, item_id,
        gateway_call=gateway, fetcher=fetcher, catalog=[],
        source_url=_pytest_source_url(case),
    )
    return result


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_registry_row_drives_run_flow_to_expected_state(case: dict) -> None:
    """Every registry row's flow terminates in its declared ``expected_state``."""
    assert isinstance(case, dict) and case.get("id"), "case required"
    expected_state = str((case["expected"] or {}).get("engine_state") or "")
    assert expected_state, f"case {case['id']} missing expected.engine_state"
    result = _drive_flow(case)
    actual = str(result.get("state") or "")
    assert actual == expected_state, (
        f"case {case['id']}: expected {expected_state!r}, got {actual!r}; "
        f"error={result.get('error')!r}")
