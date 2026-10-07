"""Tests for the llm shim: the schema validator the proto's jsonschema used to do,
one-retry semantics, no_model() guard, transport-error wrapping.

These tests are new in the port (the proto used external jsonschema).
"""
from __future__ import annotations

import pytest

from smartbrain_3000.ni_forms import llm

_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["pick", "fits"],
    "properties": {
        "pick": {"type": "string", "enum": ["a", "b"]},
        "fits": {"type": "array", "minItems": 1, "maxItems": 3,
                 "items": {"type": "object", "additionalProperties": False,
                           "required": ["cand"],
                           "properties": {"cand": {"type": "string", "enum": ["a", "b"]}}}},
        "second": {"anyOf": [{"type": "null"}, {"type": "string", "enum": ["a", "b"]}]},
    },
}


def _once(replies):
    """One-shot transport that pops successive replies off a list."""
    assert replies, "replies list must be non-empty"
    assert isinstance(replies, list), "replies must be a list"

    def call(_messages):
        return replies.pop(0)
    return call


def test_valid_passes_without_retry():
    """A valid reply yields the parsed object and retries=0 (no retry fired)."""
    reply = '{"pick":"a","fits":[{"cand":"a"}],"second":null}'
    obj, meta = llm.chat_json("present", [], _SCHEMA, call=_once([reply]))
    assert obj["pick"] == "a", "pick should be 'a'"
    assert meta.retries == 0, "no retry should have fired"


def test_missing_required_fails_after_one_retry():
    """Missing a required key fires one retry; two bad replies raise ModelUnavailable."""
    bad = '{"pick":"a"}'
    with pytest.raises(llm.ModelUnavailable) as ei:
        llm.chat_json("present", [], _SCHEMA, call=_once([bad, bad]))
    assert "fits" in str(ei.value), "error must name the missing key"


def test_bad_enum_fails():
    """A value outside the enum raises ModelUnavailable with 'enum' in the message."""
    bad = '{"pick":"c","fits":[{"cand":"a"}],"second":null}'
    with pytest.raises(llm.ModelUnavailable) as ei:
        llm.chat_json("present", [], _SCHEMA, call=_once([bad, bad]))
    assert "enum" in str(ei.value) or "pick" in str(ei.value), "error should name enum/pick"


def test_extra_key_fails_on_additional_properties_false():
    """additionalProperties:false must reject unknown keys."""
    bad = '{"pick":"a","fits":[{"cand":"a"}],"second":null,"extra":1}'
    with pytest.raises(llm.ModelUnavailable) as ei:
        llm.chat_json("present", [], _SCHEMA, call=_once([bad, bad]))
    assert "extra" in str(ei.value), "error should mention extra key"


def test_retry_recovers_from_first_invalid():
    """First reply invalid, second valid -> returns the valid object with retries=1."""
    bad = '{"pick":"c","fits":[]}'
    good = '{"pick":"a","fits":[{"cand":"a"}],"second":null}'
    obj, meta = llm.chat_json("present", [], _SCHEMA, call=_once([bad, good]))
    assert obj["pick"] == "a", "second reply should land"
    assert meta.retries == 1, "one retry recorded"


def test_no_model_guard_blocks_calls():
    """Inside no_model(), any call raises ModelForbidden (refresh-path contract)."""
    with llm.no_model(), pytest.raises(llm.ModelForbidden):
        llm.chat_json("present", [], _SCHEMA, call=lambda _m: '{}')


def test_transport_error_wraps_as_unavailable():
    """A transport exception becomes ModelUnavailable (no bubbling)."""
    def raiser(_m):
        raise ConnectionError("gateway down")
    with pytest.raises(llm.ModelUnavailable) as ei:
        llm.chat_json("present", [], _SCHEMA, call=raiser)
    assert "ConnectionError" in str(ei.value), "wrapped error must name the cause"
