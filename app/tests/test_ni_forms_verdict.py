"""FIT (plan B3, Phase 3a): ``ni_forms.verdict.fit_verdict`` — the closed per-call menu,
the literal skeleton, and the evidence-grounding rule (an evidence value that does not
substring-match the fenced preview rows is stripped, never trusted at face value).
``visible_fit`` (operator ruling 2026-10-12): the sealed verdict as the card's closed advisory
marker — its verdicts, its suppression over real builds and binds, its closed labels.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from smartbrain_3000 import ni, ni_flow
from smartbrain_3000.ni_forms import llm
from smartbrain_3000.ni_forms.verdict import (
    MAX_VISIBLE_MISSING,
    fit_verdict,
    visible_fit,
)

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


# --- the visible advisory: visible_fit ------------------------------------------------------------------

_ALERTS = {"name": "alerts", "label": "Active weather alerts", "kind": "list", "primary": True,
           "words": ["alerts", "warnings"], "path": "features", "may_be_empty": True,
           "row": [{"path": "properties.event", "label": "Event", "type": "text"},
                   {"path": "properties.areaDesc", "label": "Area", "type": "text"}]}
_QUAKES = {"name": "quakes", "label": "Earthquakes", "kind": "list", "primary": True,
           "words": ["earthquakes", "quakes"], "path": "features",
           "row": [{"path": "properties.place", "label": "Place", "type": "text"},
                   {"path": "properties.mag", "label": "Magnitude", "type": "number"}]}
_ONE_ALERT = {"features": [{"properties": {"event": "Tornado Warning", "areaDesc": "Sedgwick, KS"}}]}
_TWO_QUAKES = {"features": [{"properties": {"place": "194 km SE of Attu Station, Alaska", "mag": 4.6}},
                            {"properties": {"place": "Rat Islands, Aleutian Islands", "mag": 4.2}}]}


def _card(answer: dict, sample: dict, *, refresh: dict | None = None, agg: str = "none",
          verdict: str | None = "no", missing: list | None = None) -> tuple[dict, dict]:
    """A real declared card as (sealed spec, latest bind): the flow's own builder over ``sample``,
    the engine's pipeline + bind over ``refresh`` (the next fetch) when given, else the build's
    own preview."""
    built = ni_flow.build_from_answers([ni_flow._clean_answer(answer)], sample, "card", agg=agg)
    spec = {"title": "card", "goal": "card", "interval_minutes": 15, "pipeline": built["pipeline"],
            "scene": built["scene"], "source": {"type": "http_json", "url": "https://example.org/x"}}
    if verdict is not None:
        spec["_fit"] = {"answers_ask": verdict, "missing": list(missing or []), "wrong": [], "evidence": []}
    outputs = built["preview_payload"] if refresh is None else ni.run_pipeline(built["pipeline"], refresh)
    latest = ni.bind_scene(spec["scene"], outputs, form_ctx=ni._form_bind_context(spec, datetime.now(UTC)))
    return spec, latest


@pytest.mark.parametrize(("verdict", "shown"), [("no", True), ("partly", True), ("yes", False), (None, False)])
def test_visible_fit_shows_no_and_partly_only(verdict, shown) -> None:
    spec, latest = _card(_QUAKES, _TWO_QUAKES, verdict=verdict, missing=["magnitude"])
    assert visible_fit(spec, latest) == ({"verdict": verdict, "missing": ["Magnitude"]} if shown else None)


def test_visible_fit_is_moot_before_a_reading_is_bound() -> None:
    spec, latest = _card(_QUAKES, _TWO_QUAKES)
    assert visible_fit(spec, latest) == {"verdict": "no", "missing": []}
    assert visible_fit(spec, None) is None and visible_fit(spec, {}) is None   # no reading yet
    assert visible_fit({**spec, "_shell": True}, latest) is None               # the flow's placeholder


def test_an_empty_may_be_empty_list_is_the_answer_not_a_doubt() -> None:
    """Set G #33/#34: "tornado warnings in Kansas" with none active — the model called the empty list
    "no answer"; the designed empty state IS the answer, so no marker. The same card with a warning
    active shows the verdict."""
    spec, latest = _card(_ALERTS, {"features": []})
    assert latest["clir"]["desktop"]["state"] == "empty"
    assert visible_fit(spec, latest) is None
    spec, latest = _card(_ALERTS, _ONE_ALERT)
    assert latest["clir"]["desktop"]["state"] != "empty"
    assert visible_fit(spec, latest) == {"verdict": "no", "missing": []}


def test_a_count_card_with_zero_rows_is_the_answer() -> None:
    """A count ask ("how many quakes above magnitude 4 …") whose refresh holds no rows: 0 IS the
    tally, so no marker — the build sealed the engine's count op over its rows."""
    spec, latest = _card(_QUAKES, _TWO_QUAKES, refresh={"features": []}, agg="count")
    assert any(op.get("fn") == "count" for st in spec["pipeline"] for op in st.get("apply") or [])
    assert latest["clir"]["desktop"]["state"] == "empty"
    assert visible_fit(spec, latest) is None


def test_an_empty_list_not_designed_to_be_empty_keeps_the_marker() -> None:
    spec, latest = _card(_QUAKES, _TWO_QUAKES, refresh={"features": []})
    assert not any(op.get("fn") == "count" for st in spec["pipeline"] for op in st.get("apply") or [])
    assert latest["clir"]["desktop"]["state"] == "empty"
    assert visible_fit(spec, latest) == {"verdict": "no", "missing": []}


def test_missing_labels_are_closed_lexicon_or_plain_and_at_most_three() -> None:
    """Every label is the label lexicon's text for the id or the id itself when it is a plain
    lowercase word / phrase (<= 24 chars); anything else is dropped; duplicates fold; <= 3."""
    spec, latest = _card(_QUAKES, _TWO_QUAKES, missing=[
        "next_low_tide", "Ground Delays!", "x" * 25, "precip_prob", "chance of rain", "fog"])
    assert visible_fit(spec, latest)["missing"] == ["Chance of rain", "fog"]
    spec, latest = _card(_QUAKES, _TWO_QUAKES, missing=["wave_height", "UV index", "chance of snow", "wind"])
    out = visible_fit(spec, latest)
    assert out["missing"] == ["Waves", "UV index", "chance of snow"]
    assert len(out["missing"]) == MAX_VISIBLE_MISSING and set(out) == {"verdict", "missing"}

