"""``SMARTBRAIN_NI_FLOW`` is retired: v2 is the only flow. Any other value warns once and changes nothing."""

from __future__ import annotations

import logging

import pytest

from smartbrain_3000 import ni_flow


def test_the_flag_is_gone() -> None:
    assert not hasattr(ni_flow, "flow_mode") and not hasattr(ni_flow, "_is_v2")


@pytest.mark.parametrize("value", ["v1", "V1", "junk", "v3"])
def test_a_retired_value_warns(monkeypatch, caplog, value) -> None:
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", value)
    with caplog.at_level(logging.WARNING):
        ni_flow.warn_retired_flow_flag()
    assert any("SMARTBRAIN_NI_FLOW" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", [None, "v2", " V2 "])
def test_unset_or_v2_is_silent(monkeypatch, caplog, value) -> None:
    if value is None:
        monkeypatch.delenv("SMARTBRAIN_NI_FLOW", raising=False)
    else:
        monkeypatch.setenv("SMARTBRAIN_NI_FLOW", value)
    with caplog.at_level(logging.WARNING):
        ni_flow.warn_retired_flow_flag()
    assert not caplog.records


def test_v1_in_the_environment_still_runs_v2(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        assert client.get("/api/ni/items/nope/flow").status_code == 404  # the route exists
        assert client.get("/api/ni/board").status_code == 200
