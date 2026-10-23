"""S2 shadow wiring: the four constraint classes run at the flow's verify call site, record their reasons on
the flow record (``_verify_shadow``) and never block a build. Replays the recorded Library samples of
test_ni_flow_frame_verify (frozen clock, no network, no model)."""

from __future__ import annotations

import pytest
import test_ni_flow_frame_verify as fv

from smartbrain_3000 import ni_flow


def _record(store, item_id: str) -> dict:
    rec = ni_flow._flow_read(store, item_id)
    assert rec is not None, "the flow record is sealed"
    return rec


def test_a_clean_build_records_an_empty_shadow(monkeypatch) -> None:
    store = fv._store()
    ask = "is it gonna storm in Tulsa tonight"
    out = fv._run(store, monkeypatch, "open_meteo_charleston", ask,
                  fv._intent(ask, "weather", ["storm chance"], "Tulsa"))
    assert out["state"] == "ready", out["notes"]
    assert _record(store, out["_item"]).get("_verify_shadow") == {}


def test_a_compare_the_build_cannot_answer_is_refused_at_build(monkeypatch) -> None:
    """S7 turned the S6 shadow record into a build gate: a one-city card for a two-city ask is never shipped."""
    store = fv._store()
    ask = "temperature in Charleston vs Tulsa"
    out = fv._run(store, monkeypatch, "open_meteo_charleston", ask,
                  fv._intent(ask, "weather", ["temperature"], "Charleston"))
    assert out["state"] == "unsupported", out["notes"]
    assert any("can't compare these yet" in n and "Tulsa" in n for n in out["notes"]), out["notes"]


def test_the_shadow_never_raises_and_says_what_failed(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("bad")
    monkeypatch.setattr("smartbrain_3000.ni_verify.frame.shadow_reasons", boom)
    store = fv._store()
    ask = "is it gonna storm in Tulsa tonight"
    out = fv._run(store, monkeypatch, "open_meteo_charleston", ask,
                  fv._intent(ask, "weather", ["storm chance"], "Tulsa"))
    assert out["state"] == "ready", out["notes"]
    assert _record(store, out["_item"]).get("_verify_shadow") == {"error": ["RuntimeError"]}


@pytest.mark.parametrize("ask", ["storm chance tonight in Tulsa", "tonight, will it storm in Tulsa"])
def test_a_window_the_hours_hold_is_not_objected_to(monkeypatch, ask) -> None:
    store = fv._store()
    out = fv._run(store, monkeypatch, "open_meteo_charleston", ask, fv._intent(ask, "weather", ["storm chance"], "Tulsa"))
    assert out["state"] == "ready", out["notes"]
    assert "window" not in _record(store, out["_item"]).get("_verify_shadow", {})
