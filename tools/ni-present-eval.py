#!/usr/bin/env python3
"""PRESENT model-pick measurement (fix round 1a-6, class M).

PRESENT (``ni_forms.present``) asks the local 9B to choose among the rules floor's
candidates. The first live read (2026-10-07) showed it never validating: free-form
replies (``{"analysis": …}``, ``{"option_0": …}``) of the right shape as JSON but the
wrong schema, every time — the prompt described the schema in words; the intent
stage's prompt (which DOES validate) shows a literal JSON shape instead. This tool
measures PRESENT for real (the actual ``present.present()``, including the schema
retry) over every multi-candidate case recorded under
``app/tests/fixtures/ni_forms/live_2026-10-07*/``, reports validity, the pick per
case, agreement with the rules floor, agreement with the oracle's acceptable set
(``expected_forms.json``), and the hard-gate outcomes.

RUN NATIVELY ONLY (never in Docker — the gateway is a host process):
    S/venv/bin/python tools/ni-present-eval.py [--sets live_2026-10-07 live_2026-10-07b]
                                               [--endpoint http://127.0.0.1:38080]
                                               [--model mlx/Qwen3.5-9B-MLX-4bit]
No network beyond the local gateway; no write beyond an optional ``--out`` report.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000.ni_forms import llm as ni_llm
from smartbrain_3000.ni_forms import present as ni_present
from smartbrain_3000.ni_forms.form_scene import design

_FIX_ROOT = _REPO / "app" / "tests" / "fixtures" / "ni_forms"
_DEFAULT_SETS = ("live_2026-10-07", "live_2026-10-07b")
_DEFAULT_ENDPOINT = "http://127.0.0.1:38080"
_DEFAULT_MODEL = "mlx/Qwen3.5-9B-MLX-4bit"
_TIMEOUT_S = 180


def _gateway_call(endpoint: str, model: str) -> Callable[[list], str]:
    """PRESENT's ``call(messages) -> str`` over the real local-model gateway."""
    assert endpoint and isinstance(endpoint, str), "endpoint required"
    assert model and isinstance(model, str), "model id required"
    url = endpoint.rstrip("/") + "/v1/chat/completions"

    def _call(messages: list) -> str:
        assert isinstance(messages, list) and messages, "messages required"
        body = json.dumps({"model": model, "temperature": 0, "max_tokens": 1500,
                           "messages": messages}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.load(resp)
        return payload["choices"][0]["message"]["content"]

    return _call


def _cases(fixture_set: str) -> list[tuple[dict, dict]]:
    """``[(case, expected)]`` for every non-excluded case of one fixture set."""
    assert isinstance(fixture_set, str) and fixture_set, "fixture_set required"
    fix = _FIX_ROOT / fixture_set
    expect = json.loads((fix / "expected_forms.json").read_text())
    assert isinstance(expect, dict), "expected_forms.json must be a dict"
    out: list[tuple[dict, dict]] = []
    for path in sorted(fix.glob("*.json"))[:100]:   # bounded: a fixture set holds < 30 cases
        if path.name == "expected_forms.json":
            continue
        case = json.loads(path.read_text())
        exp = expect.get(str(case["n"])) or {}
        if exp.get("excluded"):
            continue
        out.append((case, exp))
    return out


def _designed(case: dict):
    """The rules-floor design (no model) for one recorded case: its candidate menu."""
    assert isinstance(case, dict), "case required"
    now = datetime.fromisoformat(case["fetched_at"])
    return design(case["answers_used"], case["outputs"], title=case["title"], ask=case["ask"], now=now,
                 source_url=case.get("source_url"), cadence_s=900,
                 rows_output_name=case.get("rows_output_name"), call_model=None,
                 viewer_tz=case.get("viewer_tz", "UTC"), question_kind=case.get("frame_kind"),
                 wants=case.get("wants") or [])


def _measure_one(fixture_set: str, case: dict, exp: dict, call: Callable[[list], str]) -> dict:
    """One row of the report: the real ``present()`` call against the gateway."""
    assert isinstance(case, dict) and isinstance(exp, dict), "case + expected required"
    d = _designed(case)
    row = {"set": fixture_set, "id": case["id"], "candidates": len(d.cands)}
    if len(d.cands) < 2:
        row["outcome"] = "single_candidate"
        return row
    t0 = time.time()
    res = ni_present.present(d.cands, d.rec, d.prof, d.inp, call=call)
    row["seconds"] = round(time.time() - t0, 1)
    row["designer"], row["pick"], row["gates"] = res.designer, res.used, list(res.gates)
    row["floor_pick"] = d.cands[0].id
    row["agrees_with_floor"] = res.used == d.cands[0].id
    if exp.get("forms"):
        picked_form = next(c.form for c in d.cands if c.id == res.used)
        row["in_oracle_set"] = picked_form in exp["forms"]
    row["outcome"] = "valid" if res.designer == "model" else "rules_fallback"
    return row


def run(sets: list[str], endpoint: str, model: str) -> list[dict]:
    """Measure every multi-candidate case of ``sets``; one row per case."""
    assert isinstance(sets, list) and sets, "sets required"
    call = _gateway_call(endpoint, model)
    rows: list[dict] = []
    for fixture_set in sets[:10]:            # bounded: a handful of fixture sets, ever
        for case, exp in _cases(fixture_set):
            try:
                rows.append(_measure_one(fixture_set, case, exp, call))
            except (urllib.error.URLError, ni_llm.ModelUnavailable, TimeoutError) as exc:
                rows.append({"set": fixture_set, "id": case["id"], "outcome": "error",
                            "detail": f"{type(exc).__name__}: {exc}"[:200]})
    return rows


def _report(rows: list[dict]) -> str:
    """The printed summary: validity %, per-case picks, agreement, gates."""
    assert isinstance(rows, list), "rows required"
    scored = [r for r in rows if r["outcome"] in ("valid", "rules_fallback")]
    valid = [r for r in scored if r["outcome"] == "valid"]
    lines = [f"{r['set']} {r['id']}: {r['outcome']} pick={r.get('pick')} floor={r.get('floor_pick')} "
            f"agrees_with_floor={r.get('agrees_with_floor')} in_oracle_set={r.get('in_oracle_set')} "
            f"gates={r.get('gates')}" for r in rows]
    pct = 100 * len(valid) / len(scored) if scored else 0.0
    lines.append(f"\nPRESENT validity: {len(valid)}/{len(scored)} ({pct:.0f}%) over "
                 f"{len(rows)} cases ({len(rows) - len(scored)} single-candidate / error)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sets", nargs="+", default=list(_DEFAULT_SETS))
    parser.add_argument("--endpoint", default=_DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--out", default=None, help="optional path to write the rows as JSON")
    args = parser.parse_args(argv)
    rows = run(args.sets, args.endpoint, args.model)
    print(_report(rows))
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(rows, indent=1))
    scored = [r for r in rows if r["outcome"] in ("valid", "rules_fallback")]
    return 0 if scored else 1


if __name__ == "__main__":
    sys.exit(main())
