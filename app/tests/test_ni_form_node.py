"""Phase 1a-2: the `form` scene node (validator + binder) in smartbrain_3000.ni.

Covers the validator (good + bad shapes, the sealed ``design.second``), the bind
that re-reads the sealed record by path and runs the ni_forms engine on it
(deterministic hash across two binds at one fetch instant; no model call), the
drift fallback that flags `design_needs_attention`, the C2 ``presentation_id``
reseal (``swap_to_second``) and the ``_present_ok`` attestation on the spec.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smartbrain_3000 import ni
from smartbrain_3000.ni_forms.form_scene import swap_to_second
from smartbrain_3000.ni_forms.types import TEXT_SRC

_NOW = datetime(2026, 10, 6, 18, 21, tzinfo=UTC)
_SPEC = {"title": "NVDA", "goal": "price of NVDA", "interval_minutes": 15,
         "source": {"type": "http_json", "url": "https://api.example.org/x"}}


def _good_node(**over) -> dict:
    """A sealed form node mirroring what ni_forms emits for a one-number `stat`
    candidate. The engine's `stat.match` returns `params={}` for this shape; keep
    the sealed node consistent so the bind takes the sealed path."""
    base: dict = {
        "type": "form",
        "form": "stat",
        "variant": "number",
        "params": {},
        "record": {
            "kind": "measure",
            "fields": [{"name": "price", "label": "Price", "path": "price",
                        "type": "currency", "role": "measure",
                        "currency": "USD", "precision": 2}],
            "rows": None,
        },
        "spans": {"desktop": "d1x1", "phone": "p1x1"},
        "design": {"designer": "rules", "pick": "c0", "second": None},
    }
    base.update(over)
    return base


def _second() -> dict:
    return {"id": "c1", "form": "kv_grid", "variant": "fields", "params": {},
            "spans": {"desktop": "d2x1", "phone": "p2x1"}}


def _ctx() -> dict:
    return ni._form_bind_context(_SPEC, _NOW)


def test_form_type_is_scene_type() -> None:
    """The validator's dispatch must accept the new 'form' scene type."""
    assert "form" in ni._SCENE_TYPES
    ni.validate_scene(_good_node())


def test_form_validator_catches_bad_form_name() -> None:
    with pytest.raises(ValueError, match="form.form unknown"):
        ni.validate_scene(_good_node(form="not_a_form"))


def test_form_validator_catches_bad_span() -> None:
    with pytest.raises(ValueError, match="form.spans.desktop"):
        ni.validate_scene(_good_node(spans={"desktop": "zzz", "phone": "p1x1"}))


def test_form_validator_catches_bad_designer() -> None:
    bad = _good_node()
    bad["design"]["designer"] = "oracle"
    with pytest.raises(ValueError, match="designer"):
        ni.validate_scene(bad)


def test_form_validator_refuses_unknown_field_type_and_role() -> None:
    """The enums are ``ni_forms.types.FIELD_TYPES`` / ``ROLES`` — one source of truth."""
    bad = _good_node()
    bad["record"]["fields"][0]["type"] = "mystery"
    with pytest.raises(ValueError, match="type"):
        ni.validate_scene(bad)
    bad = _good_node()
    bad["record"]["fields"][0]["role"] = "headline"
    with pytest.raises(ValueError, match="role"):
        ni.validate_scene(bad)
    ok = _good_node()
    ok["record"]["fields"][0]["role"] = "score_a"   # in ROLES, was not in the old local copy
    ni.validate_scene(ok)


def test_form_validator_refuses_duplicate_field_names() -> None:
    bad = _good_node()
    bad["record"]["fields"].append(dict(bad["record"]["fields"][0]))
    with pytest.raises(ValueError, match="duplicate|exceeds"):
        ni.validate_scene(bad)


def test_form_validator_second_is_a_sealed_candidate_or_none() -> None:
    node = _good_node()
    node["design"]["second"] = _second()
    ni.validate_scene(node)
    node["design"]["second"] = "c1"
    with pytest.raises(ValueError, match="design.second"):
        ni.validate_scene(node)
    node["design"]["second"] = {**_second(), "form": "nope"}
    with pytest.raises(ValueError, match="form.form unknown"):
        ni.validate_scene(node)


def test_form_bind_runs_engine_and_returns_two_clirs() -> None:
    """A sealed form node binds through the ni_forms engine without a model call;
    the shell prints the spec's title + host (the bind context), never the data."""
    node = _good_node()
    bound = ni.bind_scene(node, {"price": 223.86}, form_ctx=_ctx())
    assert bound["type"] == "form"
    assert set(bound["clir"]) == {"desktop", "phone"}
    assert isinstance(bound["hash"], str) and bound["hash"]
    assert bound["clir"]["desktop"]["form"] == "stat"
    assert bound["clir"]["phone"]["form"] == "stat"
    assert "design_needs_attention" not in bound   # sealed candidate survived
    texts = [ln for p in bound["clir"]["desktop"]["prims"] if p["k"] == "text" for ln in p["lines"]]
    assert "NVDA" in texts and "$223.86" in texts
    assert texts.count("example.org") == 1   # host = the registrable domain, printed once by the shell
    assert all(p.get("src") in TEXT_SRC for p in bound["clir"]["desktop"]["prims"] if p["k"] == "text")


def test_form_bind_is_deterministic_at_one_fetch_instant() -> None:
    """Two binds of the same node + outputs + context produce the same hash."""
    node = _good_node()
    h1 = ni.bind_scene(node, {"price": 223.86}, form_ctx=_ctx())["hash"]
    h2 = ni.bind_scene(node, {"price": 223.86}, form_ctx=_ctx())["hash"]
    assert h1 == h2


def test_form_bind_without_a_context_reads_the_clock_and_still_binds() -> None:
    """Template previews + older callers pass no context: the bind derives one."""
    bound = ni.bind_scene(_good_node(), {"price": 223.86})
    assert bound["type"] == "form" and bound["lint"]["red"] == 0


def test_form_bind_never_rederives_the_sealed_types() -> None:
    """The seal says ``text``; one distinct value would derive ``category``. The bind
    keeps the seal (a stat ``word`` either way, but the record is the sealed one)."""
    node = _good_node(variant="word")
    node["record"]["fields"] = [{"name": "label", "label": "Label", "path": "label",
                                 "type": "text", "role": "measure"}]
    bound = ni.bind_scene(node, {"label": "On time"}, form_ctx=_ctx())
    assert bound["form"] == "stat" and "design_needs_attention" not in bound


def test_form_bind_drift_sets_needs_attention() -> None:
    """A sealed variant the engine no longer emits takes the floor + the flag."""
    node = _good_node(variant="nonexistent_but_short")
    ni.validate_scene(node)   # shape OK: variant is a 1..80-char string
    bound = ni.bind_scene(node, {"price": 223.86}, form_ctx=_ctx())
    assert bound.get("design_needs_attention") is True


def test_form_bind_measure_over_a_list_output_reads_row_zero() -> None:
    """Fix round 1a-6, class H: a measure sealed over a list output (e.g. a one-row
    day/sunrise/sunset answer) reads row 0 of that output on every bind."""
    node = _good_node()
    node["record"]["rows"] = "rows"
    bound = ni.bind_scene(node, {"rows": [{"price": 223.86}]}, form_ctx=_ctx())
    assert bound["type"] == "form" and "design_needs_attention" not in bound
    assert "$223.86" in bound["summary"]


def test_form_bind_measure_over_rows_drift_sets_needs_attention() -> None:
    """A later refresh whose rows output no longer has exactly one row is the data
    drifting past the one-row design (fix round 1a-6, class H): the floor + the
    ``design_needs_attention`` flag, never a crash (a measure caps at 1 row)."""
    node = _good_node()
    node["record"]["rows"] = "rows"
    two = ni.bind_scene(node, {"rows": [{"price": 1.0}, {"price": 2.0}]}, form_ctx=_ctx())
    assert two.get("design_needs_attention") is True
    empty = ni.bind_scene(node, {"rows": []}, form_ctx=_ctx())
    assert empty.get("design_needs_attention") is True


def test_form_bind_fails_when_a_sealed_row_field_is_gone_from_every_row() -> None:
    """A sparse row shows a blank cell; a cell gone from EVERY row is drift — the run
    fails (``extract_miss``) so last_good keeps rendering and repair fires, exactly as
    the old repeat template did."""
    node = _good_node(form="table", variant="plain", spans={"desktop": "d1x1", "phone": "p2x1"})
    node["record"] = {"kind": "records", "rows": "rows", "fields": [
        {"name": "a", "label": "A", "path": "a", "type": "text", "role": "name"},
        {"name": "b", "label": "B", "path": "b", "type": "text", "role": "meta"},
        {"name": "n", "label": "N", "path": "n", "type": "number", "role": "value"}]}
    ni.bind_scene(node, {"rows": [{"a": "first", "b": "x", "n": 1}, {"a": "second", "n": 2}]},
                  form_ctx=_ctx())
    with pytest.raises(ni.NIError) as err:
        ni.bind_scene(node, {"rows": [{"a": "first", "n": 1}, {"a": "second", "n": 2}]}, form_ctx=_ctx())
    assert err.value.kind == "extract_miss"


def test_form_bind_fails_when_the_measure_has_no_value() -> None:
    """A measure record whose headline cell is empty is a failed run (``extract_miss`` —
    last_good keeps rendering, repair fires), never a live card reading "Price: ";
    a secondary left blank stays a sparse fact."""
    node = _good_node(form="conditions", variant="default")
    node["record"]["fields"] = [
        {"name": "price", "label": "Price", "path": "price", "type": "currency", "role": "measure",
         "currency": "USD"},
        {"name": "change", "label": "Change", "path": "change", "type": "currency", "role": "secondary",
         "currency": "USD"}]
    with pytest.raises(ni.NIError) as err:
        ni.bind_scene(node, {"price": None, "change": -1.65}, form_ctx=_ctx())
    assert err.value.kind == "extract_miss"
    with pytest.raises(ni.NIError) as err:
        ni.bind_scene(node, {"price": "n/a", "change": -1.65}, form_ctx=_ctx())
    assert err.value.kind == "extract_miss"
    bound = ni.bind_scene(node, {"price": 223.86, "change": None}, form_ctx=_ctx())
    assert bound["type"] == "form" and "$223.86" in bound["summary"]


def test_form_bind_fails_when_the_rows_output_is_not_a_list() -> None:
    """The repeat bind's contract, kept: a rows output that is not a list is ``bind_type``,
    never the designed empty state."""
    node = _good_node(form="table", variant="plain", spans={"desktop": "d1x1", "phone": "p2x1"})
    node["record"] = {"kind": "records", "rows": "rows", "fields": [
        {"name": "a", "label": "A", "path": "a", "type": "text", "role": "name"},
        {"name": "b", "label": "B", "path": "b", "type": "text", "role": "meta"},
        {"name": "n", "label": "N", "path": "n", "type": "number", "role": "value"}]}
    for bad in ({"rows": "oops"}, {"rows": None}, {}):
        with pytest.raises(ni.NIError) as err:
            ni.bind_scene(node, bad, form_ctx=_ctx())
        assert err.value.kind == "bind_type" and "must resolve to a list" in str(err.value.detail)


def test_form_is_never_a_repeat_template() -> None:
    """A form is a leaf (§34): five form templates would be five whole-data engine runs."""
    repeat = {"type": "repeat", "items": {"$bind": "rows"}, "max": 5, "template": _good_node()}
    with pytest.raises(ValueError, match="may not be a form"):
        ni.validate_scene({"type": "stack", "dir": "v", "gap": "sm", "children": [repeat]})


def test_json_instants_refuses_a_preview_too_large_to_snapshot() -> None:
    """The preview walk is bounded; past the bound it raises rather than writing holes."""
    assert ni.json_instants({"rows": [{"n": i} for i in range(1_000)]})["rows"][999] == {"n": 999}
    with pytest.raises(ni.NIError) as err:
        ni.json_instants({"rows": list(range(ni._MAX_PREVIEW_NODES + 10))})
    assert err.value.kind == "bind_type" and "too large" in str(err.value.detail)


def test_form_bind_refuses_model_call() -> None:
    """ni_forms.llm.no_model() wraps bind — any attempted model call raises."""
    from smartbrain_3000.ni_forms import llm

    with llm.no_model():
        try:
            llm.chat_json("present", [], {}, call=lambda _m: "{}")
        except llm.ModelForbidden:
            return
    pytest.fail("ModelForbidden must raise inside no_model()")


def test_form_bind_type_enforcement_accepts_bound_form() -> None:
    """_enforce_bind_types mirrors the validator for the BOUND shape."""
    node = _good_node()
    bound = ni.bind_scene(node, {"price": 223.86}, form_ctx=_ctx())
    ni._enforce_bind_types(node, bound)   # must not raise


# --- §34 Phase 1b: the bound node's `clock` key + `_form_bind_context`'s `now` --------

def test_form_bind_carries_a_clock_key_from_the_stale_threshold() -> None:
    """`_good_node()` has no time field at all, so the stale threshold (as_of + 2x
    cadence) is the only candidate `next_boundary` can find; `_SPEC`'s 15-minute
    interval makes that fetched_at + 30 min."""
    bound = ni.bind_scene(_good_node(), {"price": 223.86}, form_ctx=_ctx())
    assert bound["clock"] == {"next": "2026-10-06T18:51:00Z"}   # _NOW + 30 min


def test_form_bind_context_now_defaults_to_fetched_at() -> None:
    ctx = ni._form_bind_context(_SPEC, _NOW)
    assert ctx["now"] == ctx["fetched_at"]


def test_form_bind_context_now_drives_layout_while_fetched_at_stays_fixed() -> None:
    """A clock pass passes a LATER `now` with the run's original `fetched_at` held
    fixed: the stale threshold is computed from `fetched_at` (unmoved), so once `now`
    itself is past that threshold, nothing is left in the future -- `clock.next` is
    None, never re-based on the later `now`."""
    ctx_same = ni._form_bind_context(_SPEC, _NOW)
    at_fetch = ni.bind_scene(_good_node(), {"price": 223.86}, form_ctx=ctx_same)
    assert at_fetch["clock"]["next"] is not None

    ctx_later = ni._form_bind_context(_SPEC, _NOW, _NOW + timedelta(hours=1))
    assert ctx_later["fetched_at"] == ctx_same["fetched_at"]   # unchanged
    assert ctx_later["now"] != ctx_later["fetched_at"]
    later = ni.bind_scene(_good_node(), {"price": 223.86}, form_ctx=ctx_later)
    assert later["clock"]["next"] is None   # the 30-min stale threshold already passed


def test_enforce_form_shape_accepts_absent_clock() -> None:
    """Version-skew: a pre-1b bound payload has no `clock` key at all."""
    bound = ni.bind_scene(_good_node(), {"price": 223.86}, form_ctx=_ctx())
    del bound["clock"]
    ni._enforce_form_shape(bound)   # must not raise


def test_enforce_form_shape_accepts_a_null_next() -> None:
    bound = ni.bind_scene(_good_node(), {"price": 223.86}, form_ctx=_ctx())
    bound["clock"] = {"next": None}
    ni._enforce_form_shape(bound)   # must not raise


def test_enforce_form_shape_rejects_a_malformed_clock() -> None:
    bound = ni.bind_scene(_good_node(), {"price": 223.86}, form_ctx=_ctx())
    for bad in ({"next": "not-a-date"}, {"next": 123}, {"foo": None}, "soon"):
        with pytest.raises(ni.NIError, match="clock"):
            ni._enforce_form_shape({**bound, "clock": bad})


def test_swap_to_second_reseals_the_runner_up_over_the_same_record() -> None:
    node = _good_node()
    node["design"]["second"] = _second()
    swapped = swap_to_second(node)
    ni.validate_scene(swapped)
    assert (swapped["form"], swapped["variant"], swapped["spans"]["desktop"]) == ("kv_grid", "fields", "d2x1")
    assert swapped["design"]["pick"] == "c1"
    assert swapped["design"]["second"]["form"] == "stat"     # the former pick stays on offer
    assert swapped["record"] == node["record"]               # same record, nothing re-derived
    with pytest.raises(ValueError, match="no second"):
        swap_to_second(_good_node())


def test_present_ok_is_a_closed_attestation_on_the_spec() -> None:
    spec = {"version": 1, "title": "t", "goal": "g", "params": {},
            "source": {"type": "http_json", "url": "https://api.example.org/x"},
            "pipeline": [{"op": "extract", "paths": {"price": "p"}}],
            "scene": _good_node(), "display": {"size": "small"}, "interval_minutes": 15}
    ni.validate_spec({**spec, "_present_ok": "second"})
    with pytest.raises(ValueError, match="_present_ok"):
        ni.validate_spec({**spec, "_present_ok": "third"})
