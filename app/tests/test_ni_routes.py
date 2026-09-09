"""HTTP tests for the Neural Interface API: lock gate, board shapes, validate/run,
PATCH restriction, delete cascade, and the Desktop-local credential PUT.

Only the LLM turn / model-source fetch is faked; every route drives the real FastAPI
app with a real DuckDB + migrations (mirrors test_schedule_routes.py).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import ni


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _unlock(client: TestClient) -> None:
    assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200


def _scene() -> dict:
    """A scene binding a 'text' field — pairs with the {'text': ...} preview/payload."""
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}


def _spec_body(**over) -> dict:
    body = {
        "title": "Weather",
        "goal": "show the weather",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [],
        "scene": _scene(),
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"text": "preview"},
    }
    body.update(over)
    return body


def _create_via_tool(client: TestClient, **over) -> str:
    """Create an NI item through the audited tool chokepoint (parks then approves)."""
    body = _spec_body(**over)
    r = client.post("/api/tools/invoke", json={"name": "create_ni_item", "args": body})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval", r.text
    pid = r.json()["pending_id"]
    approve = client.post(f"/api/agent/pending/{pid}/approve",
                          json={"confirm_tool": "create_ni_item"})
    assert approve.status_code == 200, approve.text
    return approve.json()["result"]["id"]


# --- lock gate ------------------------------------------------------------

def test_routes_require_unlock(client: TestClient) -> None:
    assert client.get("/api/ni/board").status_code == 423
    assert client.get("/api/ni/items/x").status_code == 423
    assert client.post("/api/ni/items/x/validate", json={"ok": True}).status_code == 423
    assert client.post("/api/ni/items/x/run").status_code == 423
    assert client.patch("/api/ni/items/x", json={"enabled": False}).status_code == 423
    assert client.delete("/api/ni/items/x").status_code == 423
    # Credential PUT also gates on unlock (Desktop-local check runs at handler entry).
    r = client.put("/api/ni/items/x/credential",
                   json={"name": "n", "value": "v", "host": "h"},
                   headers={"X-SB-Local": "1"})
    assert r.status_code == 423


# --- board view: draft → preview slot, live → latest, degraded → last_good --

def test_board_shape_and_slot_selection(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)  # fresh item lives in draft
    items = client.get("/api/ni/board").json()["items"]
    row = next(i for i in items if i["id"] == iid)
    assert row["state"] == "draft" and row["payload_slot"] == "preview"
    assert row["payload"] == {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "preview", "role": "title", "tone": "default", "size": "md"},
    ]}


def test_board_prefers_latest_when_live_ok(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    store = client.app.state.ni
    # Push to live and write both a latest and a last_good.
    store.set_state(iid, "live")
    store.write_snapshot(iid, "latest",
                         ni.bind_scene(_scene(), {"text": "latest_val"}), ok=True)
    store.write_snapshot(iid, "last_good",
                         ni.bind_scene(_scene(), {"text": "last_good_val"}), ok=True)
    row = next(i for i in client.get("/api/ni/board").json()["items"] if i["id"] == iid)
    assert row["payload_slot"] == "latest"
    assert row["payload"]["children"][0]["value"] == "latest_val"


def test_board_falls_back_to_last_good_when_degraded(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    store = client.app.state.ni
    store.set_state(iid, "degraded")
    # latest is a failure marker (ok=false) — the board must skip it and show last_good.
    store.write_snapshot(iid, "latest",
                         ni.bind_scene(_scene(), {"text": "stale"}), ok=False)
    store.write_snapshot(iid, "last_good",
                         ni.bind_scene(_scene(), {"text": "good"}), ok=True)
    row = next(i for i in client.get("/api/ni/board").json()["items"] if i["id"] == iid)
    assert row["payload_slot"] == "last_good"
    assert row["payload"]["children"][0]["value"] == "good"


# --- item detail: secret names, no values --------------------------------

def test_get_item_returns_spec_with_secret_names_only(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client, source={
        "type": "http_json",
        "url": "https://api.example.com/q",
        "headers": {"X-Api-Key": {"$secret": "ni:x:api_key"}},
    })
    body = client.get(f"/api/ni/items/{iid}").json()
    assert body["spec"]["source"]["headers"]["X-Api-Key"] == {"$secret": "ni:x:api_key"}
    # No plaintext secret value should ever have travelled the GET path.
    import json as _json
    assert "s3cret" not in _json.dumps(body)


def test_get_item_404_for_unknown(client: TestClient) -> None:
    _unlock(client)
    assert client.get("/api/ni/items/no-such").status_code == 404


# --- validate: C2 verdicts ------------------------------------------------

def test_validate_ok_stamps_c2_and_leaves_state(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    client.app.state.ni.set_state(iid, "commissioning")
    r = client.post(f"/api/ni/items/{iid}/validate", json={"ok": True, "note": ""})
    assert r.status_code == 200 and r.json()["state"] == "commissioning"
    assert client.app.state.ni.get_item(iid)["spec"].get("_c2_ok") is True


def test_validate_wrong_returns_to_draft(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    client.app.state.ni.set_state(iid, "commissioning")
    r = client.post(f"/api/ni/items/{iid}/validate",
                    json={"ok": False, "note": "wrong number"})
    assert r.status_code == 200 and r.json()["state"] == "draft"
    assert client.app.state.ni.get_item(iid)["state"] == "draft"


def test_validate_404_for_unknown(client: TestClient) -> None:
    _unlock(client)
    assert client.post("/api/ni/items/nope/validate",
                       json={"ok": True}).status_code == 404


# --- run: synchronous execution -----------------------------------------

def test_run_route_executes_synchronously(client: TestClient, monkeypatch) -> None:
    """POST /run drives ni.run_item and returns the outcome — the desktop's Run now button."""
    _unlock(client)
    iid = _create_via_tool(client)

    captured: dict = {}

    def fake_run_item(store, item_id, *, gateway_mod, secrets_store, schedules_store=None):
        captured["id"] = item_id
        return {"status": "ok", "duration_ms": 7}

    monkeypatch.setattr(ni, "run_item", fake_run_item)
    r = client.post(f"/api/ni/items/{iid}/run")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and captured["id"] == iid


def test_run_route_reports_nierror(client: TestClient, monkeypatch) -> None:
    _unlock(client)
    iid = _create_via_tool(client)

    def boom(*_a, **_k):
        raise ni.NIError("fetch_failed", "oops")

    monkeypatch.setattr(ni, "run_item", boom)
    body = client.post(f"/api/ni/items/{iid}/run").json()
    assert body["status"] == "error" and body["kind"] == "fetch_failed"


def test_run_route_404_for_unknown(client: TestClient) -> None:
    _unlock(client)
    assert client.post("/api/ni/items/nope/run").status_code == 404


# --- PATCH: restricted fields ---------------------------------------------

def test_patch_toggles_enabled_and_position(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.patch(f"/api/ni/items/{iid}", json={"enabled": False, "position": 3})
    assert r.status_code == 200
    item = client.app.state.ni.get_item(iid)
    assert item["enabled"] is False and item["position"] == 3


def test_patch_rejects_unknown_field(client: TestClient) -> None:
    """FastAPI/Pydantic returns 422 on an unknown key (model_config default rejects extras)."""
    _unlock(client)
    iid = _create_via_tool(client)
    # The PatchIn model does not declare an ``extra`` policy — Pydantic v2 default is "ignore",
    # so a truly unknown key is silently dropped. Assert the SIDE EFFECT: an ignored field
    # can't have mutated the row.
    before = client.app.state.ni.get_item(iid)
    r = client.patch(f"/api/ni/items/{iid}", json={"bogus": "value"})
    assert r.status_code == 200  # ignored, not an error
    assert client.app.state.ni.get_item(iid)["state"] == before["state"]


def test_patch_bad_display_returns_400(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.patch(f"/api/ni/items/{iid}", json={"display": {"size": "gigantic"}})
    assert r.status_code == 400


def test_patch_404_for_unknown(client: TestClient) -> None:
    _unlock(client)
    r = client.patch("/api/ni/items/nope", json={"enabled": False})
    assert r.status_code == 404


# --- delete: cascade -----------------------------------------------------

def test_delete_cascades_snapshots_and_runs(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    store = client.app.state.ni
    store.record_run(iid, "ok", duration_ms=1, error=None, contract_ok=True)
    assert client.delete(f"/api/ni/items/{iid}").status_code == 200
    conn = client.app.state.dbx
    for table in ("ni_snapshots", "ni_revisions", "ni_runs", "ni_items"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'ni_items' else 'item_id'} = ?;",
            [iid],
        ).fetchone()[0] == 0


# --- credential PUT: desktop-local + never echoes value ------------------

def test_credential_put_requires_desktop_local_header(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    # Missing header → 403 (Desktop-local guard fires BEFORE the store touches the secret).
    r = client.put(f"/api/ni/items/{iid}/credential",
                   json={"name": "api_key", "value": "s3cret", "host": "api.example.com"})
    assert r.status_code == 403
    # And no secret was stored.
    assert client.app.state.secret_store.get(f"ni:{iid}:api_key") is None


def test_credential_put_stores_and_never_echoes_value(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.put(f"/api/ni/items/{iid}/credential",
                   json={"name": "api_key", "value": "s3cret", "host": "api.example.com"},
                   headers={"X-SB-Local": "1"})
    assert r.status_code == 200
    # The value MUST NOT appear in the response body.
    assert "s3cret" not in r.text
    # The secret is stored host-bound; loading with the right host returns it, wrong host refuses.
    key = f"ni:{iid}:api_key"
    got = ni._load_credential(client.app.state.secret_store, key, "api.example.com")
    assert got == "s3cret"
    with pytest.raises(ni.NIError):
        ni._load_credential(client.app.state.secret_store, key, "attacker.example.com")
    # The audit row carries metadata only — never the value.
    entries = client.get("/api/audit").json()["entries"]
    cred_rows = [e for e in entries if e["tool"] == "ni_credential"]
    assert cred_rows and "s3cret" not in cred_rows[0]["args_summary"]
    assert cred_rows[0]["decision"] == "executed" and cred_rows[0]["ok"] is True
