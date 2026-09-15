"""Unit tests for tools/ni-flow-eval.py's pure plumbing.

The eval tool is an operator gate (see its docstring): --live / --phrasings hit
bifrost + real APIs, --chaos + --recorded run against fixtures. Those modes
belong in operator-run smokes, not CI. What CI CAN verify without network or
model is the plumbing:

- case-table shape (every entry has the fields the modes rely on),
- paraphrase-table completeness (>=5 per case),
- chaos mutators actually mutate (rename/truncate/empty),
- gate arithmetic (live / phrasings / chaos).

Mirrors test_ni_prove_plumbing's skipif gate — the shipped app image omits the
repo's tools/ tree, so a missing script SKIPS this file cleanly instead of
failing on ImportError.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_EVAL_PATH = (pathlib.Path(__file__).resolve().parents[2]
              / "tools" / "ni-flow-eval.py")

pytestmark = pytest.mark.skipif(
    not _EVAL_PATH.exists(),
    reason="ni-flow-eval.py tooling not shipped in the app image")


def _load_eval():
    """Import tools/ni-flow-eval.py by path (the file isn't part of any package)."""
    assert _EVAL_PATH.exists(), f"ni-flow-eval.py missing: {_EVAL_PATH}"
    spec = importlib.util.spec_from_file_location("_ni_flow_eval", _EVAL_PATH)
    assert spec is not None and spec.loader is not None, "spec load must succeed"
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ni_flow_eval"] = module
    spec.loader.exec_module(module)
    return module


def test_case_table_has_every_field_each_mode_relies_on() -> None:
    """The registry is the eval's spine — every entry must have the keys
    both live/recorded/chaos/engine runners read (silent KeyErrors would
    surface as 'FAIL(<klass>)' rows and hide broken plumbing).
    """
    ev = _load_eval()
    assert len(ev.CASES) >= 10, "registry must include at least the graduated 10 cases"
    seen_ids: set[str] = set()
    required = {"id", "request", "fields", "klass", "expected", "intent"}
    for case in ev.CASES:
        missing = required - set(case)
        assert not missing, f"case {case.get('id')} missing keys: {missing}"
        assert case["klass"] in {"value", "list", "refuse", "image"}, \
            f"unknown klass in {case['id']}: {case['klass']}"
        assert case["id"] not in seen_ids, f"duplicate id: {case['id']}"
        seen_ids.add(case["id"])
        engine_state = case["expected"].get("engine_state")
        allowed = ev._ENGINE_STATES | {"skipped"}
        assert engine_state in allowed, \
            f"case {case['id']}: expected.engine_state {engine_state!r} not in {sorted(allowed)}"
    # Quakes case must carry a filter_threshold for the where-op branch.
    quakes = next(c for c in ev.CASES if c["id"] == "quakes-m5")
    assert isinstance(quakes.get("filter_threshold"), (int, float))


def test_every_phrasings_row_has_at_least_five_paraphrases() -> None:
    """Phrasings mode drives 5 rephrasings per case with a phrasings block;
    anything less breaks the intent-stability matrix and the >=90% gate math."""
    ev = _load_eval()
    for cid, rephrasings in ev.PARAPHRASES.items():
        assert isinstance(rephrasings, list), f"no paraphrase list for {cid}"
        assert len(rephrasings) >= 5, \
            f"case {cid} has {len(rephrasings)} paraphrases (need >=5)"
        assert len(set(rephrasings)) == len(rephrasings), \
            f"paraphrase duplicates in {cid}"


def test_registry_load_rejects_unknown_keys() -> None:
    """load_registry must refuse a row with a top-level key outside the closed set."""
    ev = _load_eval()
    import json as _json
    import pathlib as _pathlib
    tmp = _pathlib.Path("/tmp/_ni_bad_registry.json")
    row = {"id": "x", "request": "y", "fields": {}, "klass": "value",
           "expected": {"engine_state": "ready"}, "intent": {},
           "unexpected_key": True}
    tmp.write_text(_json.dumps([row]))
    with pytest.raises(RuntimeError) as exc:
        ev.load_registry(tmp)
    assert "unknown keys" in str(exc.value)


def test_registry_load_rejects_unknown_engine_state() -> None:
    """expected.engine_state must be in the closed vocabulary."""
    ev = _load_eval()
    import json as _json
    import pathlib as _pathlib
    tmp = _pathlib.Path("/tmp/_ni_bad_registry_state.json")
    row = {"id": "x", "request": "y", "fields": {}, "klass": "value",
           "expected": {"engine_state": "made_up"}, "intent": {}}
    tmp.write_text(_json.dumps([row]))
    with pytest.raises(RuntimeError) as exc:
        ev.load_registry(tmp)
    assert "engine_state" in str(exc.value)


def test_record_arg_validation_refuses_wholesale_without_only_or_flag() -> None:
    """--record without --only AND without --record-all must refuse (safety)."""
    ev = _load_eval()
    rc = ev._run_record(set(), record_all=False)
    assert rc == 2, "wholesale --record must refuse (returns 2)"


def test_chaos_rename_actually_renames_a_leaf_key() -> None:
    """chaos_rename_field must mutate the sample so the pre-drift path resolves
    to a MISS. Same-object return would mean the drill has no signal."""
    ev = _load_eval()
    original = {"chart": {"result": [{"meta": {"regularMarketPrice": 100.5}}]}}
    mutated = ev.chaos_rename_field(original,
                                    "chart.result[0].meta.regularMarketPrice")
    # deepcopy guarantee: the original object is untouched.
    assert original["chart"]["result"][0]["meta"]["regularMarketPrice"] == 100.5
    # The mutated tree renamed the leaf, so the original path no longer exists.
    # (The eval's rename is bounded to plain dict tails so a [] index step is
    # tolerated but doesn't descend deeper — the visible mutation is still
    # detectable via the top-level shape.)
    dumped = json.dumps(mutated)
    assert "regularMarketPrice_renamed" in dumped or "chart_renamed" in dumped, \
        f"no rename visible in mutated tree: {dumped[:200]}"


def test_chaos_truncate_produces_invalid_json_bytes() -> None:
    """chaos_truncate must return a strictly shorter prefix whose parse fails."""
    ev = _load_eval()
    raw = json.dumps({"deep": {"nested": {"value": 42, "extra": "trailing"}}}).encode()
    cut = ev.chaos_truncate(raw)
    assert isinstance(cut, bytes), "truncate must return bytes"
    assert 0 < len(cut) < len(raw), "truncate must shorten but not empty"
    with pytest.raises(json.JSONDecodeError):
        json.loads(cut)


def test_chaos_empty_returns_an_empty_object() -> None:
    """chaos_empty is the derive-starvation drill — an empty object is
    exactly what makes _derive_ni_paths return zero candidates."""
    ev = _load_eval()
    result = ev.chaos_empty()
    assert result == {}, "empty drill must return {}"
    assert isinstance(result, dict), "empty drill must be a dict for the walker"


def test_live_gate_requires_all_cases_passing_both_reps_with_stable_maps() -> None:
    """live_gate_pass is the release-tag gate; make sure it rejects the
    common near-misses (missing a rep, one FAIL, or unstable mapping)."""
    ev = _load_eval()
    good: list[dict] = []
    for case in ev.CASES:
        for rep in (1, 2):
            good.append({"id": case["id"], "rep": rep, "status": "PASS",
                         "mapping": {"k": f"{case['id']}.stable"}})
    assert ev.live_gate_pass(good), "the fully-green matrix must pass the gate"
    # missing rep
    missing = [e for e in good if not (e["id"] == "aapl-5min" and e["rep"] == 2)]
    assert not ev.live_gate_pass(missing), "missing rep must FAIL the gate"
    # one FAIL
    failed = [dict(e) for e in good]
    failed[0] = {**failed[0], "status": "FAIL(pipeline)"}
    assert not ev.live_gate_pass(failed), "any FAIL must FAIL the gate"
    # unstable mapping between reps
    unstable = [dict(e) for e in good]
    unstable[1] = {**unstable[1], "mapping": {"k": "drift.happened"}}
    assert not ev.live_gate_pass(unstable), "unstable mappings must FAIL the gate"


def test_phrasing_gate_uses_the_ninety_percent_threshold() -> None:
    """>=90% cases with kind AND cadence agreement pass; a single dissenting
    case at N=10 leaves 9/10 = 0.9 which is the threshold boundary (pass)."""
    ev = _load_eval()
    agree = {"kind_agree": True, "cadence_agree": True}
    disagree = {"kind_agree": False, "cadence_agree": True}
    ten_all_agree = [{"id": f"c{i}", "agreement": agree} for i in range(10)]
    assert ev.phrasing_gate_pass(ten_all_agree)
    one_dissent = [{"id": f"c{i}", "agreement": agree} for i in range(9)] + \
                  [{"id": "c9", "agreement": disagree}]
    assert ev.phrasing_gate_pass(one_dissent), "9/10 == 0.9 must PASS at 90%"
    two_dissent = [{"id": f"c{i}", "agreement": agree} for i in range(8)] + \
                  [{"id": "c8", "agreement": disagree},
                   {"id": "c9", "agreement": disagree}]
    assert not ev.phrasing_gate_pass(two_dissent), "8/10 must FAIL at 90%"


def test_chaos_gate_rejects_a_retry_storm_or_any_non_pass() -> None:
    """chaos_gate_pass demands every drill PASSes AND stays under the model-
    call cap; any single FAIL row or an over-cap PASS must trip the gate."""
    ev = _load_eval()
    clean = [{"status": "PASS(clean_fail)", "model_calls": 2} for _ in range(3)]
    assert ev.chaos_gate_pass(clean)
    with_fail = clean + [{"status": "FAIL(retry_storm calls=99)", "model_calls": 99}]
    assert not ev.chaos_gate_pass(with_fail)
    over_cap = [{"status": "PASS(clean_fail)",
                 "model_calls": ev._CHAOS_MODEL_CAP + 1}]
    assert not ev.chaos_gate_pass(over_cap)


def test_engine_mode_is_an_exclusive_mode_flag() -> None:
    """--engine joins the mutually-exclusive mode set alongside --recorded /
    --phrasings / --chaos / --record.
    """
    ev = _load_eval()
    ns = ev._parse_args(["--engine"])
    assert ns.engine is True
    # Every mode flag is False by default; --engine is the sole toggle here.
    for flag in ("recorded", "phrasings", "chaos", "record", "record_all"):
        assert getattr(ns, flag) is False, f"{flag} should default to False"


def test_engine_gate_grades_each_row_against_registry_expected_state() -> None:
    """engine_gate_pass grades every case against its registry expected_state.

    Per ni-cases.md §B: BY-DESIGN ``failed``/``source`` rows PASS on state
    match alone (their honest outcome IS the feature). ``ready`` /
    ``awaiting_params`` / ``awaiting_credential`` rows additionally require
    the C2 frozen-URL invariant. ``refuse``-class rows accept either
    ``unsupported`` OR ``ready`` (computed source may or may not be landed).
    """
    ev = _load_eval()
    # A fully-green matrix: each row hits its registry expected_state; image is skipped.
    good: list[dict] = []
    for case in ev.CASES:
        klass = case["klass"]
        expected = case["expected"]["engine_state"]
        if klass == "image":
            good.append({"id": case["id"], "klass": klass,
                         "flow_state": "skipped",
                         "expected_state": expected, "frozen_url_ok": False})
            continue
        if klass == "refuse":
            good.append({"id": case["id"], "klass": klass,
                         "flow_state": "unsupported",
                         "expected_state": expected, "frozen_url_ok": False})
            continue
        settled = expected in ev._ENGINE_SETTLED
        good.append({"id": case["id"], "klass": klass,
                     "flow_state": expected,
                     "expected_state": expected,
                     "frozen_url_ok": settled})
    assert ev.engine_gate_pass(good), "fully-green matrix must pass the engine gate"
    # A single settled case that failed trips the gate.
    bad = [dict(r) for r in good]
    for r in bad:
        if r.get("expected_state") == "ready":
            r["flow_state"] = "failed"
            break
    assert not ev.engine_gate_pass(bad)
    # A settled case with frozen_url_ok=False fails (URL swap defect).
    unfrozen = [dict(r) for r in good]
    for r in unfrozen:
        if r.get("expected_state") in ev._ENGINE_SETTLED:
            r["frozen_url_ok"] = False
            break
    assert not ev.engine_gate_pass(unfrozen)
    # A BY-DESIGN failed row PASSES on state match — no URL invariant.
    failed_row_registry = [c for c in ev.CASES
                           if c["expected"]["engine_state"] == "failed"]
    if failed_row_registry:
        one = failed_row_registry[0]
        by_design = [{"id": one["id"], "klass": one["klass"],
                      "flow_state": "failed",
                      "expected_state": "failed",
                      "frozen_url_ok": False}]
        # Also include a settled row so `expected_ids.issubset(seen)` doesn't trip.
        # (engine_gate_pass wants every non-image case present; here we only
        # check the failed-row semantics with a helper.)
        assert ev._grade_engine_row(by_design[0], one["klass"]) is True


def test_check_ni_flow_module_reports_present_on_this_branch() -> None:
    """M2 (audit 2026-09-13): the docstring's claim about ``ni_flow`` was
    inconsistent — the module lives on this branch, so the presence check
    must report ``present`` when the eval is imported.
    """
    ev = _load_eval()
    assert ev._check_ni_flow_module() == "present", \
        "smartbrain_3000.ni_flow must be importable on this branch"
