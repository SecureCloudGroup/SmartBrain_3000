"""S3 BE-8: the per-item flow read for the 3 s poll. One item, the board row's own flow fields, and strictly
read-only (the board's bookkeeping writes are not copied)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import ni_flow


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _board_row(client: TestClient, item_id: str) -> dict:
    return next(r for r in client.get("/api/ni/board").json()["items"] if r["id"] == item_id)


def test_the_poll_shape_equals_the_board_rows_flow_fields(client) -> None:
    store = client.app.state.ni
    item_id = ni_flow.create_shell_item(store, "river level in Boise")
    ni_flow._transition(store, item_id, "sampling", _try={"n": 2, "of": 3, "host": "api.example.org"})
    r = client.get(f"/api/ni/items/{item_id}/flow")
    assert r.status_code == 200
    body, row = r.json(), _board_row(client, item_id)
    assert set(body) == {"id", "state", "flow", "awaiting_yes", "actions"}
    assert body["flow"] == row["flow"] and body["awaiting_yes"] == row["awaiting_yes"]
    assert body["actions"] == row["actions"] == ["cancel", "delete"] and body["state"] == row["state"]
    assert body["flow"]["progress"]["try"] == {"n": 2, "of": 3, "host": "api.example.org"}


def test_a_card_without_a_flow_declares_its_item_actions(client) -> None:
    from test_ni_yes_gate import _fetching_preview, _fetching_scene_spec
    store = client.app.state.ni
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "live")
    body = client.get(f"/api/ni/items/{iid}/flow").json()
    assert body["flow"] is None and body["actions"] == _board_row(client, iid)["actions"]


def test_unknown_item_is_404(client) -> None:
    assert client.get("/api/ni/items/nope/flow").status_code == 404


def test_the_poll_never_mutates_what_the_board_read_would(client) -> None:
    from test_ni_yes_gate import _fetching_preview, _fetching_scene_spec
    store = client.app.state.ni
    # a retired confirm pause: the board read re-picks it; the poll must leave it alone
    retired = ni_flow.create_shell_item(store, "old ask")
    store.write_snapshot(retired, "flow", {"state": "confirm_source", "request": "old ask", "updated_at": "x",
                                           "notes": []}, ok=True)
    before = store.read_snapshot(retired, "flow")
    assert client.get(f"/api/ni/items/{retired}/flow").json()["flow"]["state"] == "source"
    assert store.read_snapshot(retired, "flow") == before
    # a failed flow hiding behind a working card: the board read deletes the slot; the poll keeps it
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "live")
    store.write_snapshot(iid, "latest", {"value": 1}, ok=True)
    ni_flow._transition(store, iid, "failed", error="fetch: boom")
    assert client.get(f"/api/ni/items/{iid}/flow").json()["flow"] is None
    assert ni_flow._flow_read(store, iid) is not None, "the poll left the slot in place"
    assert _board_row(client, iid)["flow"] is None and ni_flow._flow_read(store, iid) is None  # the board's own clear
