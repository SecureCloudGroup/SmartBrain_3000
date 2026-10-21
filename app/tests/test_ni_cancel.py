"""S3 BE-7: v2 Cancel. A cooperative flag: the record seals failed(cancelled) at once, the stopped worker
writes nothing more and ``_finalize`` refuses to seal, the item keeps its spec, and Retry is offered."""

from __future__ import annotations

import copy
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_ni_answers import _INTENT, OPEN_METEO, WEATHER_URL, lib  # noqa: F401  (lib is a fixture)
from test_ni_auto_pick import _fetcher, _model, _row, _store, _Wrap

from smartbrain_3000 import ni_flow, ni_master


@pytest.fixture(autouse=True)
def _v2_local(monkeypatch):
    from smartbrain_3000 import gateway as _gateway
    monkeypatch.setattr(_gateway, "DEFAULT_ROUTES", {"chat": "mlx/test-local"})
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v2")


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _running(client: TestClient, state: str = "sampling") -> str:
    store = client.app.state.ni
    item_id = ni_flow.create_shell_item(store, "river level in Boise")
    ni_flow._transition(store, item_id, state)
    return item_id


def test_cancel_stops_a_running_flow_and_offers_retry(client) -> None:
    item_id = _running(client)
    r = client.post(f"/api/ni/items/{item_id}/flow/cancel")
    assert r.status_code == 200 and r.json() == {"ok": True, "cancelled": True}
    store = client.app.state.ni
    record = ni_flow._flow_read(store, item_id)
    assert record["state"] == "failed" and record["error"].startswith("cancelled") and record["_cancel"] is True
    assert store.get_item(item_id)["spec"]["_shell"] is True  # the item's spec is unchanged
    row = next(x for x in client.get("/api/ni/board").json()["items"] if x["id"] == item_id)
    assert row["flow"]["reopen"] == ["retry"] and "retry" in row["actions"]
    assert row["flow"]["reason"] == ni_master.reason_for("failed", "cancelled: stopped by you")


def test_cancel_refuses_an_ended_flow_a_missing_item_and_v1(client, monkeypatch) -> None:
    item_id = _running(client)
    ni_flow._transition(client.app.state.ni, item_id, "failed", error="fetch: boom")
    assert client.post(f"/api/ni/items/{item_id}/flow/cancel").status_code == 409
    assert client.post("/api/ni/items/nope/flow/cancel").status_code == 404
    live = ni_flow.create_shell_item(client.app.state.ni, "another ask")
    ni_flow._transition(client.app.state.ni, live, "mapping")
    monkeypatch.setenv("SMARTBRAIN_NI_FLOW", "v1")
    assert client.post(f"/api/ni/items/{live}/flow/cancel").status_code == 404  # v1 has no such route
    assert ni_flow._flow_read(client.app.state.ni, live)["state"] == "mapping"


def test_retry_waits_while_the_cancelled_worker_is_still_stopping(client, monkeypatch) -> None:
    item_id = _running(client)
    client.post(f"/api/ni/items/{item_id}/flow/cancel")
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **k: True)
    ni_flow._INFLIGHT.add(item_id)
    try:
        r = client.post(f"/api/ni/items/{item_id}/flow/retry")
        assert r.status_code == 409 and "still stopping" in r.json()["detail"]
    finally:
        ni_flow._INFLIGHT.discard(item_id)
    assert client.post(f"/api/ni/items/{item_id}/flow/retry").status_code == 200


def test_a_worker_finishing_after_cancel_does_not_seal_the_card(lib, monkeypatch) -> None:  # noqa: F811
    store = _store()
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: lib)
    item_id = ni_flow.create_shell_item(store, "NYC temperature and conditions")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("NYC temperature and conditions", "source"))
    ni_flow.seal_library_pick(store, item_id, WEATHER_URL,
                              {"source_id": "open-meteo-forecast", "url": WEATHER_URL, "format": "json", "params": {}})

    def fetch(_url: str) -> object:
        assert ni_flow.cancel_flow(store, item_id) is True  # the user taps Cancel while the fetch is out
        return copy.deepcopy(OPEN_METEO)

    out = ni_flow._sample_and_map(store, item_id, "NYC temperature and conditions", _INTENT, WEATHER_URL, _model, fetch)
    assert out["state"] == "failed" and out["error"].startswith("cancelled")
    assert store.get_item(item_id)["spec"]["_shell"] is True  # _finalize sealed nothing
    assert "Preparing card" in str(store.read_snapshot(item_id, "preview")["payload"])  # still the placeholder
    assert ni_flow._flow_read(store, item_id)["state"] == "failed"  # no stage revived it


def test_the_auto_pick_stops_trying_rows_once_cancelled(lib, monkeypatch) -> None:  # noqa: F811
    store, fetched = _store(), []
    base = _fetcher(fetched, {"https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson": {"x": 1},
                              WEATHER_URL: OPEN_METEO})

    def fetch(url: str) -> object:
        sample = base(url)
        ni_flow.cancel_flow(store, item_id)
        return sample

    wrap = _Wrap(lib, [_row("usgs-quakes", "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"),
                       _row("open-meteo-forecast", WEATHER_URL)])
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: wrap)
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    item_id = ni_flow.create_shell_item(store, "NYC temperature and conditions")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("NYC temperature and conditions", "intent"))
    out = ni_flow._auto_pick(store, item_id, "NYC temperature and conditions", _INTENT, _model, fetch)
    assert len(fetched) == 1 and out["state"] == "failed" and out["error"].startswith("cancelled")
    assert store.get_item(item_id)["spec"]["_shell"] is True


def test_the_decision_table_names_cancelled() -> None:
    move = ni_master.choose_move(state="failed", error_class="cancelled", shell=True)
    assert move == {"move": "park", "question": None, "reopen": ["retry"]}
    assert ni_master.reason_for("failed", "cancelled: stopped by you") == "You stopped this build."
