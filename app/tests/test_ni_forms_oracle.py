"""The forms oracle, over three recorded live sets:

``live_2026-10-07`` (blind-50 on main after Phase 1a, the 1a-5 regression set),
``live_2026-10-07b`` (the 1a-6 fix-round gate set) and ``live_2026-10-07c`` (the SET C
gate after 1a-6, PR #494 — never shown to a worker). Each holds one case per live card
of its run (+ the first set's new-moon case that went to links): the ask, the set's
kind and the flow's frame kind, the Library source, the chosen answers rebuilt from the
Library answers file, the preview outputs the pipeline produced, the title / fetch
instant / viewer zone the card was laid out with. Each set's ``expected_forms.json``
gives the acceptable designs per case from the plan contract (B2) and the by-eye read;
a case marked ``excluded`` (a data-layer wrong: class K, or an answer-selection / wrong-
source miss untouched by this round) is documented but not scored.

Every case is built through the real ``form_scene`` path (``design``) with the rules
floor and the flow's frame kind, exactly as the flow would with no model; a lookup /
next_event / alerts / latest_items / status / result / schedule case over a list answer
first gets its rows filtered the way the flow now filters them (Round 20, Q3b — the subject
row filter is gone): the query layer's ``where`` read for the ask — the labeled gold IR of
that live card (``fixtures/ni_query/gold.jsonl``; sets A–D are development data), else the
expectation's filter as a ``contains`` clause — sealed by ``ni_flow._where_ops`` and run by
the engine; a hazard the current sample does not report leaves the honest empty state. Asserted per case:
the pick is in the acceptable set; the required (asked) fields are in the desktop plan;
lint red == 0 at both sealed spans; the summary names the asked quantity; forbidden
texts are absent. The pass rate per set prints with the test output (``-s`` / the
failure message) — a regression set, not a tuning set: the release gate uses a fresh
blind set.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import pytest

from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow
from smartbrain_3000.ni_forms.form_scene import design
from smartbrain_3000.ni_forms.layout import layout_span
from smartbrain_3000.ni_forms.spans import Span
from smartbrain_3000.ni_query.normalize import v1_where

ROOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "ni_forms"
SETS = ("live_2026-10-07", "live_2026-10-07b", "live_2026-10-07c", "live_2026-10-08d")
_EXPECTED_COUNTS = {"live_2026-10-07": 23, "live_2026-10-07b": 21, "live_2026-10-07c": 17,
                    "live_2026-10-08d": 23}
# D2: lookup/next_event always did; alerts/latest_items/status reach the same filter now.
# fix round datalayer-r2 (class W3): result/schedule reach it too -- a named team absent from
# every row is the honest NOTHING path (not the empty_subject_filter calm-state fallback below).
_SUBJECT_FILTER_KINDS = ("lookup", "next_event", "alerts", "latest_items", "status", "result", "schedule")
_RESULTS: dict[str, dict[str, list[str]]] = {name: {} for name in SETS}
_GOLD_SET = {"live_2026-10-07": "A", "live_2026-10-07b": "B", "live_2026-10-07c": "C", "live_2026-10-08d": "D"}
_GOLD = {g["id"]: g["ir"] for g in (json.loads(line) for line in (ROOT.parent / "ni_query" / "gold.jsonl")
                                     .read_text().splitlines() if line.strip())}


def _expect(name: str) -> dict:
    assert name in SETS, f"unknown fixture set {name!r}"
    return json.loads((ROOT / name / "expected_forms.json").read_text())


def _cases(name: str) -> list[pathlib.Path]:
    """Every case file of one set, minus any case its expectation marks ``excluded``."""
    assert name in SETS, f"unknown fixture set {name!r}"
    expect = _expect(name)
    out: list[pathlib.Path] = []
    for path in sorted((ROOT / name).glob("*.json"))[:100]:   # bounded: a set holds < 30 cases
        if path.name == "expected_forms.json":
            continue
        n = str(json.loads(path.read_text())["n"])
        if expect.get(n, {}).get("excluded"):
            continue
        out.append(path)
    return out


CASES = [(name, path) for name in SETS for path in _cases(name)]


def _load(name: str, path: pathlib.Path) -> tuple[dict, dict]:
    case = json.loads(path.read_text())
    return case, _expect(name)[str(case["n"])]


def _texts(clir: dict) -> list[str]:
    return [ln for p in clir["prims"] if p["k"] == "text" for ln in p["lines"]]


def _filtered(case: dict, exp: dict) -> tuple[list[dict], dict]:
    """The rows the flow's card shows for a lookup / next_event / alerts / latest_items / status / result /
    schedule case over a list answer (Round 20, Q3b): the query layer's ``where`` (the case's gold IR, else
    the expectation's filter read as ``contains``) as the flow's sealed engine ops over the outputs; the
    answer's own declared filter rides as before. A no-op for every other case."""
    answers, outputs = case["answers_used"], case["outputs"]
    answer = answers[0]
    kind = case["frame_kind"]
    if kind not in _SUBJECT_FILTER_KINDS or answer.get("kind") != "list":
        return answers, outputs
    rows_name = case["rows_output_name"]
    gold = _GOLD.get(f"{_GOLD_SET[_set_of(case)]}{case['n']}")
    expected = exp.get("subject_filter")
    # the gold where as the flow carries it (the query layer's v1 rules: no text code list on a text cell)
    where = v1_where(list(gold["where"]), answer, case["ask"], frozenset(), frozenset()) if gold is not None else (
        [{"col": expected["cell"], "op": "contains", "value": expected["equals"]}] if expected else [])
    ops, _dropped = ni_flow._where_ops(where, answer, {c["path"]: c["path"] for c in answer["cells"]})
    flt = answer.get("filter")
    if flt:  # the answer's own declared filter (the airport's rows, the "Final" games) as the build seals it
        ops.insert(0, {"fn": "where", "field": "rows", "key": flt["path"], "op": "eq", "value": flt["equals"]})
    rows = nimod.run_pipeline([{"op": "extract", "paths": {"rows": "rows"}},
                               {"op": "transform", "apply": ops}], {"rows": outputs[rows_name]})["rows"] \
        if ops else outputs[rows_name]
    if exp.get("empty_subject_filter"):
        assert ops and rows == [], (where, rows)
    elif exp.get("no_subject_filter"):
        assert not where, where
    elif expected:
        assert rows or not outputs[rows_name], "the filter selects at least one row"
        got = {str(ni_flow._dig(r, expected["cell"])).casefold() for r in rows}
        assert all(expected["equals"].casefold() in v for v in got), (expected, got)
    return answers, {**outputs, rows_name: rows}


def _set_of(case: dict) -> str:
    """The fixture set a case file belongs to (its id carries no set name)."""
    return next(name for name in SETS if (ROOT / name / f"{case['id']}.json").exists())


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


@pytest.mark.parametrize("name,path", CASES, ids=[f"{n}-{p.stem}" for n, p in CASES])
def test_oracle_case(name: str, path: pathlib.Path) -> None:
    case, exp = _load(name, path)
    fails = _check(case, exp)
    _RESULTS[name][case["id"]] = fails
    assert not fails, f"{name} {case['id']}: " + "; ".join(fails)


# R19 Phase 1b: "wrong-month 0" -- a day-ish card-zone time prim is painted straight from
# its own instant (`t`) in the card zone at paint time (`ni_forms.paint.live.time_prim_text`
# -- the CLIR never stores pre-rendered text for a `time` prim). The off-by-one risk class
# `ni_forms.clock` and the card-tz midnight property tests guard against is the prim's `t`
# resolving, in the card zone, to a calendar day OUTSIDE the record's own row-date span: a
# day_table walks every day between the earliest and latest row (day_rows / rows alike fill
# the gap days, e.g. a game-free day between two scheduled games) but never a day before or
# after the data it has -- a mismatch here can only mean the wrong zone was used somewhere.
_DAY_FMTS = frozenset({"EEE", "EEE d", "MMM d", "EEE MMM d"})


def _date_of(t_iso: str, zone) -> object:
    """A prim's `t` -> its own calendar day. A bare ``YYYY-MM-DD`` (len 10) IS a floating
    calendar day already -- never zone-shifted (``datetime.fromisoformat`` would parse it
    naive and ``.astimezone`` would then assume the HOST's local zone, exactly the off-by-
    one bug this check exists to catch; ``fmt.parse_t`` / ``ni_forms.clock`` avoid it the
    same way). A full instant is converted to its calendar day in the card zone."""
    from datetime import datetime as _dt

    if len(t_iso) == 10:
        from datetime import date as _date

        return _date.fromisoformat(t_iso)
    return _dt.fromisoformat(t_iso).astimezone(zone).date()


def _row_date_span_in_card_tz(rec, card_tz: str) -> tuple:
    """(earliest, latest) calendar day across every date/datetime cell of `rec` and its
    parts, in the card zone -- or None when there is no such field anywhere."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(card_tz)
    days: list = []
    for part in (rec, *rec.parts.values()):
        idx = [i for i, f in enumerate(part.fields) if f.type in ("datetime", "date")]
        if not idx:
            continue
        for row in part.rows:
            for i in idx:
                v = row[i]
                if not isinstance(v, str):
                    continue
                try:
                    days.append(_date_of(v, zone))
                except ValueError:
                    continue
    return (min(days), max(days)) if days else None


def _check_no_date_off_by_one(case: dict, exp: dict) -> list[str]:
    from zoneinfo import ZoneInfo

    answers, outputs = _filtered(case, exp)
    now = datetime.fromisoformat(case["fetched_at"])
    d = design(answers, outputs, title=case["title"], ask=case["ask"], now=now, source_url=case["source_url"],
              cadence_s=900, rows_output_name=case["rows_output_name"], call_model=None,
              viewer_tz=case["viewer_tz"], question_kind=case["frame_kind"], wants=[])
    card_tz = d.rec.context.card_tz
    span = _row_date_span_in_card_tz(d.rec, card_tz)
    if span is None:
        return []   # no date/datetime field anywhere -- nothing this check can say
    lo, hi = span
    zone = ZoneInfo(card_tz)
    fails: list[str] = []
    for side in ("desktop", "phone"):
        clir = layout_span(d.cand, d.rec, d.prof, d.inp, Span.parse(d.node["spans"][side]), now).clir
        for p in clir["prims"]:
            if p.get("k") != "time" or p.get("tz") != "card" or p.get("fmt") not in _DAY_FMTS:
                continue
            shown = _date_of(p["t"], zone)
            if not (lo <= shown <= hi):
                fails.append(f"{side} prim {p['id']} ({p['fmt']}): card_tz {card_tz} day {shown} "
                             f"is outside the record's own row-date span [{lo}, {hi}]")
    return fails


@pytest.mark.parametrize("name,path", CASES, ids=[f"{n}-{p.stem}" for n, p in CASES])
def test_oracle_case_no_date_off_by_one(name: str, path: pathlib.Path) -> None:
    case, exp = _load(name, path)
    fails = _check_no_date_off_by_one(case, exp)
    assert not fails, f"{name} {case['id']}: " + "; ".join(fails)


def test_oracle_pass_rate_is_reported_and_complete() -> None:
    """Runs last (pytest keeps file order): prints the per-set, per-case verdicts and
    rate, and holds EVERY fixture set to 100% — every scored case has an acceptable
    design. ``live_2026-10-07`` is the 1a-5 regression set; ``live_2026-10-07b`` is the
    fresh 1a-6 gate set."""
    reports: list[str] = []
    bad_sets: list[str] = []
    for name in SETS:
        results = _RESULTS[name]
        lines = [f"[{'PASS' if not f else 'FAIL'}] {cid}" + (": " + "; ".join(f) if f else "")
                 for cid, f in sorted(results.items())]
        passed = sum(1 for f in results.values() if not f)
        reports.append(f"{name}:\n" + "\n".join(lines) + f"\nORACLE {name}: {passed}/{len(results)} pass")
        if len(results) != _EXPECTED_COUNTS[name] or passed != len(results):
            bad_sets.append(name)
    report = "\n\n".join(reports)
    print("\n" + report)
    assert not bad_sets, report
