#!/usr/bin/env python3
"""FIT model measurement (plan B3, Phase 3a).

``ni_forms.verdict.fit_verdict`` asks the local model one closed question per
declared-answers build: does the chosen data actually answer the ask? This
tool runs it for real (the actual gateway call, including the schema retry)
over the labeled set at ``app/tests/fixtures/ni_fit/`` (63 live cards from the
three blind-50 runs + >=40 synthetic negatives) and reports precision/recall
per ``answers_ask`` class against the by-eye label.

RUN NATIVELY ONLY (never in Docker — the gateway is a host process):
    S/venv/bin/python tools/ni-fit-eval.py [--endpoint http://127.0.0.1:38080]
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

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000.ni_forms import llm as ni_llm
from smartbrain_3000.ni_forms.verdict import VERDICTS, fit_verdict

_FIX = _REPO / "app" / "tests" / "fixtures" / "ni_fit"
_DEFAULT_ENDPOINT = "http://127.0.0.1:38080"
_DEFAULT_MODEL = "mlx/Qwen3.5-9B-MLX-4bit"
_TIMEOUT_S = 180
_MAX_RECORDS = 200   # bounded: the labeled set holds 126 today


def _gateway_call(endpoint: str, model: str) -> Callable[[str], str]:
    """``fit_verdict``'s ``call_model(prompt: str) -> str`` over the real local gateway."""
    assert endpoint and isinstance(endpoint, str), "endpoint required"
    assert model and isinstance(model, str), "model id required"
    url = endpoint.rstrip("/") + "/v1/chat/completions"

    def _call(prompt: str) -> str:
        assert isinstance(prompt, str) and prompt, "prompt required"
        body = json.dumps({"model": model, "temperature": 0, "max_tokens": 400,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.load(resp)
        return payload["choices"][0]["message"]["content"]

    return _call


def _records() -> list[dict]:
    """Every labeled record under ``app/tests/fixtures/ni_fit/`` (bounded, sorted)."""
    paths = sorted(_FIX.glob("*.json"))[:_MAX_RECORDS]
    return [json.loads(p.read_text()) for p in paths if p.name != "README.md"]


def _measure_one(rec: dict, call: Callable[[str], str]) -> dict:
    """One report row: the real ``fit_verdict`` call against the gateway, scored
    against the record's label."""
    assert isinstance(rec, dict), "rec required"
    row = {"id": rec["id"], "truth": rec["label"]["answers_ask"]}
    t0 = time.time()
    try:
        verdict = fit_verdict(rec["ask"], rec["frame"], rec["chosen"], rec["preview"],
                              rec["missing_menu"], call)
    except (urllib.error.URLError, ni_llm.ModelUnavailable, TimeoutError) as exc:
        row["outcome"] = "error"
        row["detail"] = f"{type(exc).__name__}: {exc}"[:200]
        return row
    row["seconds"] = round(time.time() - t0, 1)
    if verdict is None:
        row["outcome"] = "model_unavailable"
        return row
    row["outcome"] = "valid"
    row["pred"] = verdict.answers_ask
    row["correct"] = verdict.answers_ask == row["truth"]
    return row


def run(endpoint: str, model: str) -> list[dict]:
    """Measure every labeled record; one row per record."""
    call = _gateway_call(endpoint, model)
    return [_measure_one(rec, call) for rec in _records()]


def _precision_recall(rows: list[dict], cls: str) -> tuple[float, float, int]:
    """(precision, recall, support) for one ``answers_ask`` class over the valid rows."""
    valid = [r for r in rows if r["outcome"] == "valid"]
    tp = sum(1 for r in valid if r["pred"] == cls and r["truth"] == cls)
    fp = sum(1 for r in valid if r["pred"] == cls and r["truth"] != cls)
    fn = sum(1 for r in valid if r["pred"] != cls and r["truth"] == cls)
    support = tp + fn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return precision, recall, support


def _report(rows: list[dict]) -> str:
    """The printed summary: validity rate, per-class precision/recall, overall accuracy."""
    valid = [r for r in rows if r["outcome"] == "valid"]
    lines = [f"{r['id']}: {r['outcome']} truth={r['truth']} pred={r.get('pred')} "
            f"correct={r.get('correct')}" for r in rows]
    lines.append(f"\nFIT validity: {len(valid)}/{len(rows)} "
                 f"({100 * len(valid) / len(rows) if rows else 0:.0f}%)")
    for cls in sorted(VERDICTS):
        p, r, n = _precision_recall(rows, cls)
        lines.append(f"  class {cls:<7} precision={p:.2f} recall={r:.2f} support={n}")
    acc = sum(1 for r in valid if r["correct"]) / len(valid) if valid else 0.0
    lines.append(f"overall accuracy (valid rows only): {acc:.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=_DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--out", default=None, help="optional path to write the rows as JSON")
    args = parser.parse_args(argv)
    rows = run(args.endpoint, args.model)
    print(_report(rows))
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(rows, indent=1))
    return 0 if any(r["outcome"] == "valid" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
