"""FIT (plan B3, Phase 3a): ``ni_forms.verdict.fit_verdict`` — the closed per-call menu,
the literal skeleton, and the evidence-grounding rule (an evidence value that does not
substring-match the fenced preview rows is stripped, never trusted at face value).
"""
from __future__ import annotations

import json

import pytest

from smartbrain_3000.ni_forms import llm
from smartbrain_3000.ni_forms.verdict import fit_verdict

_ASK = "red flag warnings in California"
_FRAME = {"kind": "alerts", "window": None, "place": "California"}
_CHOSEN = [{"name": "alerts", "label": "Active weather alerts", "kind": "list"}]
_PREVIEW = {"rows": [{"event": "Extreme Heat Warning", "severity": "Severe"},
                    {"event": "Coastal Flood Advisory", "severity": "Minor"}]}
_MENU = ["red", "flag", "warning"]


def _once(replies: list):
    """One-shot transport that pops successive replies off a list."""
    assert isinstance(replies, list) and replies, "replies must be a non-empty list"

    def call(_prompt):
        return replies.pop(0)
    return call


def test_no_call_model_returns_none_without_calling():
    calls = []
    assert fit_verdict(_ASK, _FRAME, _CHOSEN, _PREVIEW, _MENU, None) is None
    assert calls == []


def test_valid_reply_returns_a_verdict_and_keeps_grounded_evidence():
    reply = json.dumps({"answers_ask": "no", "missing": ["flag"], "wrong": ["alerts"],
                        "evidence": [{"answer": "alerts", "value": "Extreme Heat Warning"}]})
    v = fit_verdict(_ASK, _FRAME, _CHOSEN, _PREVIEW, _MENU, _once([reply]))
    assert v is not None
    assert v.answers_ask == "no"
    assert v.missing == ["flag"]
    assert v.wrong == ["alerts"]
    assert v.evidence == [{"answer": "alerts", "value": "Extreme Heat Warning"}]


def test_ungrounded_evidence_is_stripped_not_trusted():
    """An evidence value the model invented (never in the fenced preview) is dropped —
    the `_judge_build` closed-world rule, so nothing unquoted reaches the sealed verdict."""
    reply = json.dumps({"answers_ask": "no", "missing": [], "wrong": ["alerts"],
                        "evidence": [{"answer": "alerts", "value": "Red Flag Warning"}]})
    v = fit_verdict(_ASK, _FRAME, _CHOSEN, _PREVIEW, _MENU, _once([reply]))
    assert v is not None
    assert v.evidence == []   # "Red Flag Warning" never appears in _PREVIEW's rows


def test_schema_invalid_twice_returns_none_not_raise():
    bad = '{"analysis": "the data looks wrong"}'
    v = fit_verdict(_ASK, _FRAME, _CHOSEN, _PREVIEW, _MENU, _once([bad, bad]))
    assert v is None


def test_model_forbidden_propagates_inside_no_model():
    with llm.no_model(), pytest.raises(llm.ModelForbidden):
        fit_verdict(_ASK, _FRAME, _CHOSEN, _PREVIEW, _MENU, lambda _p: "{}")


def test_menu_and_answer_names_are_closed_enums():
    """A 'missing' id outside this call's menu is schema-invalid — the retry gets the
    same literal skeleton, and a second miss returns None (never an invented id)."""
    outside = json.dumps({"answers_ask": "no", "missing": ["not_on_the_menu"], "wrong": [],
                          "evidence": []})
    good = json.dumps({"answers_ask": "no", "missing": ["flag"], "wrong": [], "evidence": []})
    v = fit_verdict(_ASK, _FRAME, _CHOSEN, _PREVIEW, _MENU, _once([outside, good]))
    assert v is not None and v.missing == ["flag"]


def test_value_answer_preview_is_one_row():
    """A value-answer build's preview (no 'rows' key) still reads as exactly one fenced
    row — fit_verdict never requires a list-shaped build."""
    chosen = [{"name": "temperature", "label": "Temperature", "kind": "value", "unit": "°F"}]
    preview = {"temperature": 68, "conditions": "Clear"}
    reply = json.dumps({"answers_ask": "yes", "missing": [], "wrong": [], "evidence": []})
    v = fit_verdict("how cold is it", {"kind": "current_value"}, chosen, preview, [], _once([reply]))
    assert v is not None and v.answers_ask == "yes"
