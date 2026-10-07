"""The forms oracle over the recorded live set (blind-50 on main after Phase 1a, 2026-10-07).

``tests/fixtures/ni_forms/live_2026-10-07/`` holds one case per live card of that run (+ the
new-moon case that went to links): the ask, the set's kind and the flow's frame kind, the
Library source, the chosen answers rebuilt from the Library answers file, the preview outputs
the pipeline produced, the title / fetch instant / viewer zone the card was laid out with.
``expected_forms.json`` gives the acceptable designs per case from the plan contract (B2).

Every case is built through the real ``form_scene`` path (``design``) with the rules floor and
the flow's frame kind, exactly as the flow would with no model; the lookup / next-event cases
first pass the flow's subject row filter (``ni_flow._scope_rows_to_subject``). Asserted per case:
the pick is in the acceptable set; the required (asked) fields are in the desktop plan; lint red
== 0 at both sealed spans; the summary names the asked quantity; forbidden texts are absent.
The pass rate prints with the test output (``-s`` / the failure message) — a regression set, not
a tuning set: the release gate uses a fresh blind set.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import pytest

from smartbrain_3000 import ni_flow
from smartbrain_3000.ni_forms.form_scene import design
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.spans import Span

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "ni_forms" / "live_2026-10-07"
EXPECT = json.loads((FIX / "expected_forms.json").read_text())
CASES = sorted(p for p in FIX.glob("*.json") if p.name != "expected_forms.json")
_RESULTS: dict[str, list[str]] = {}


def _load(path: pathlib.Path) -> tuple[dict, dict]:
    case = json.loads(path.read_text())
    return case, EXPECT[str(case["n"])]


def _texts(clir: dict) -> list[str]:
    return [ln for p in clir["prims"] if p["k"] == "text" for ln in p["lines"]]


def _filtered(case: dict, exp: dict) -> tuple[list[dict], dict]:
    """The flow's subject row filter (class D) applied the way ``_try_answers_build`` applies it:
    only a lookup / next_event ask over a list answer; the sealed ``filter`` selects the rows."""
    answers, outputs = case["answers_used"], case["outputs"]
    answer = answers[0]
    if case["frame_kind"] not in ("lookup", "next_event") or answer.get("kind") != "list":
        return answers, outputs
    sample = {answer["path"]: outputs.get(case["rows_output_name"]) or []}
    scoped = ni_flow._scope_rows_to_subject(answer, sample, case["subject"] or "", None)
    if exp.get("no_subject_filter") or not exp.get("subject_filter"):
        assert "filter" not in scoped, scoped.get("filter")
        return answers, outputs
    flt = scoped["filter"]
    assert flt == {"path": exp["subject_filter"]["cell"], "equals": exp["subject_filter"]["equals"]}, flt
    rows = [r for r in outputs[case["rows_output_name"]] if ni_flow._dig(r, flt["path"]) == flt["equals"]]
    assert rows, "the subject filter selects at least one row"
    return [scoped], {**outputs, case["rows_output_name"]: rows}


def _check(case: dict, exp: dict) -> list[str]:
    """Every oracle check for one case; the list of failures (empty = pass)."""
    assert isinstance(case, dict) and isinstance(exp, dict), "case + expectation required"
    answers, outputs = _filtered(case, exp)
    now = datetime.fromisoformat(case["fetched_at"])
    d = design(answers, outputs, title=case["title"], ask=case["ask"], now=now, source_url=case["source_url"],
               cadence_s=900, rows_output_name=case["rows_output_name"], call_model=None,
               viewer_tz=case["viewer_tz"], question_kind=case["frame_kind"], wants=[])
    node = d.node
    fails: list[str] = []
    if node["form"] not in exp["forms"]:
        fails.append(f"pick {node['form']} not in {exp['forms']}")
    los = {side: layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(node["spans"][side]), now)
           for side in ("desktop", "phone")}
    shown = set(los["desktop"].content["fields"])
    missing = [f for f in exp["required"] if f not in shown]
    if missing:
        fails.append(f"desktop plan drops {missing} (shows {sorted(shown)})")
    for side, lo in los.items():
        red = sorted({i.code for i in lo.lint.issues if i.sev == "red"})
        if red:
            fails.append(f"{side} {node['spans'][side]} red {red}")
    summary = los["desktop"].clir["summary"]
    for w in exp["summary_words"]:
        if w.lower() not in summary.lower():
            fails.append(f"summary lacks {w!r}: {summary[:80]!r}")
    texts = " | ".join(_texts(los["desktop"].clir) + _texts(los["phone"].clir) + [summary])
    for t in exp.get("forbidden_texts") or []:
        if t in texts:
            fails.append(f"shows {t!r}")
    if node["design"]["designer"] != "rules":
        fails.append("the rules floor designed this case (no model)")
    if (node.get("frame") or {}).get("kind") != case["frame_kind"]:
        fails.append(f"frame not sealed: {node.get('frame')}")
    return fails


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_oracle_case(path: pathlib.Path) -> None:
    case, exp = _load(path)
    fails = _check(case, exp)
    _RESULTS[case["id"]] = fails
    assert not fails, f"{case['id']}: " + "; ".join(fails)


def test_oracle_pass_rate_is_reported_and_complete() -> None:
    """Runs last (pytest keeps file order): prints the per-case verdicts and the rate, and holds
    the whole set to 100 % — every recorded live case has an acceptable design."""
    assert len(CASES) == 23, f"the 2026-10-07 live set holds 22 live cards + the new-moon case, got {len(CASES)}"
    lines = [f"[{'PASS' if not f else 'FAIL'}] {cid}" + (": " + "; ".join(f) if f else "")
             for cid, f in sorted(_RESULTS.items())]
    passed = sum(1 for f in _RESULTS.values() if not f)
    report = "\n".join(lines) + f"\nORACLE live_2026-10-07: {passed}/{len(_RESULTS)} pass"
    print("\n" + report)
    assert len(_RESULTS) == len(CASES), "every case ran"
    assert passed == len(CASES), report
