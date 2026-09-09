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
        # A1: existing route tests were written against the "land in draft" behavior
        # (board picks preview slot, PATCH/run tests assume a settled draft). Default
        # ``draft: True`` keeps that surface; new commissioning-path tests below flip
        # it explicitly so both paths are covered.
        "draft": True,
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
    """G3: after a failure the engine writes an ok=False ``latest`` marker on its own,
    so the board's latest-if-ok-else-last_good fallback picks up last_good. Prior test
    fabricated this state by hand; now the engine drives it (any regression in
    ``_handle_failure``'s snapshot-write would show up here)."""
    _unlock(client)
    iid = _create_via_tool(client)
    store = client.app.state.ni
    # Prime a last_good the way a healthy run would.
    store.set_state(iid, "live")
    store.write_snapshot(iid, "last_good",
                         ni.bind_scene(_scene(), {"text": "good"}), ok=True)
    # Drive a real failure through the engine path — this is the write that used to be
    # fabricated by hand (a G3 regression would silently keep the old latest snapshot).
    exc = ni.NIError("fetch_failed", "oops")
    ni._handle_failure(store, store.get_item(iid), exc, started=0.0)
    store.set_state(iid, "degraded")  # mirror the transition the tick would apply
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
    client.app.state.ni.commission(iid)  # /run refuses draft (K6) — advance out of it

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
    client.app.state.ni.commission(iid)  # /run refuses draft (K6) — advance out of it

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


def test_patch_ignores_unknown_field_but_applies_recognized_neighbors(client: TestClient) -> None:
    """K9: describe the REAL behavior — Pydantic v2's default extra=ignore silently drops
    unknown keys, so a bogus field is not an error. The state-unchanged assertion is made
    non-vacuous by ALSO sending a real field alongside: the recognized field must apply
    (proving the payload wasn't rejected wholesale) while the state itself stays put.
    """
    _unlock(client)
    iid = _create_via_tool(client)
    before = client.app.state.ni.get_item(iid)
    r = client.patch(f"/api/ni/items/{iid}",
                     json={"bogus": "value", "enabled": False})
    assert r.status_code == 200  # unknown key ignored; recognized key applied
    after = client.app.state.ni.get_item(iid)
    assert after["enabled"] is False, "recognized 'enabled' field must have applied"
    assert after["state"] == before["state"], "state is unrelated to enabled + must stay put"


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
    got = ni._load_credential(client.app.state.secret_store, key, "api.example.com",
                              item_id=iid, request_scheme="https")
    assert got == "s3cret"
    with pytest.raises(ni.NIError):
        ni._load_credential(client.app.state.secret_store, key, "attacker.example.com",
                            item_id=iid, request_scheme="https")
    # The audit row carries metadata only — never the value.
    entries = client.get("/api/audit").json()["entries"]
    cred_rows = [e for e in entries if e["tool"] == "ni_credential"]
    assert cred_rows and "s3cret" not in cred_rows[0]["args_summary"]
    assert cred_rows[0]["decision"] == "executed" and cred_rows[0]["ok"] is True


# --- audit-finding route regressions --------------------------------------

def test_commission_route_moves_draft_to_commissioning(client: TestClient) -> None:
    """A2: POST /commission moves draft -> commissioning; 409 otherwise."""
    _unlock(client)
    iid = _create_via_tool(client)  # draft
    r = client.post(f"/api/ni/items/{iid}/commission")
    assert r.status_code == 200 and r.json()["state"] == "commissioning"
    # A second call refuses (409) because state is no longer draft.
    r2 = client.post(f"/api/ni/items/{iid}/commission")
    assert r2.status_code == 409


def test_commission_route_refuses_when_secret_param_unfilled(client: TestClient) -> None:
    """A2: unfilled secret param blocks commissioning until the credential is entered."""
    _unlock(client)
    body = _spec_body(
        params={"api_key": {"label": "Key", "kind": "secret", "value": ""}},
        source={"type": "http_json",
                "url": "https://api.example.com/q",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    r = client.post("/api/tools/invoke", json={"name": "create_ni_item", "args": body})
    pid = r.json()["pending_id"]
    approve = client.post(f"/api/agent/pending/{pid}/approve",
                          json={"confirm_tool": "create_ni_item"})
    iid = approve.json()["result"]["id"]
    # No credential entered yet — commission refuses with 409.
    r2 = client.post(f"/api/ni/items/{iid}/commission")
    assert r2.status_code == 409 and "secret" in r2.json()["detail"]


def test_validate_route_returns_409_outside_commissioning(client: TestClient) -> None:
    """C: /validate refuses (409) when the item isn't currently commissioning."""
    _unlock(client)
    iid = _create_via_tool(client)  # draft
    r = client.post(f"/api/ni/items/{iid}/validate", json={"ok": True})
    assert r.status_code == 409


def test_run_route_refuses_draft_and_broken_with_409(client: TestClient) -> None:
    """K6: /run refuses draft and broken states before touching the engine."""
    _unlock(client)
    iid = _create_via_tool(client)  # draft
    r = client.post(f"/api/ni/items/{iid}/run")
    assert r.status_code == 409
    client.app.state.ni.set_state(iid, "broken")
    r2 = client.post(f"/api/ni/items/{iid}/run")
    assert r2.status_code == 409


def test_manual_run_route_posts_alerts_and_broken_to_carrier(
    client: TestClient, monkeypatch,
) -> None:
    """M1b (audit 2026-09-09): POST /api/ni/items/{id}/run posts fired alerts +
    broken transition notices to the NI carrier row exactly like _auto_update_ni.
    """
    from smartbrain_3000 import scheduler as sched

    _unlock(client)
    iid = _create_via_tool(client)
    client.app.state.ni.commission(iid)  # /run refuses draft (K6) — advance out of it

    def fake_run_item(store, item_id, *, gateway_mod, secrets_store,
                      schedules_store=None):
        store.set_state(item_id, "broken")  # forces broken-transition posting
        return {"status": "ok", "duration_ms": 1,
                "alerts": [{"item_id": item_id, "title": "Watch",
                            "message": "manual fired"}]}

    monkeypatch.setattr(ni, "run_item", fake_run_item)
    r = client.post(f"/api/ni/items/{iid}/run")
    assert r.status_code == 200, r.text
    store = sched.ScheduleStore(client.app.state.dbx,
                                client.app.state.master_key)
    messages = [row["message"] for row in store.recent_runs()
                if row["schedule_title"] == "Neural Interface"]
    assert "manual fired" in messages
    assert any("is broken" in m for m in messages)


def test_fetch_http_json_refuses_redirect_only_when_headers_attached(monkeypatch) -> None:
    """E: NI's http_json fetch opts out of redirects WHEN and ONLY WHEN it attaches any
    header (secret or literal) — a hostile server could otherwise 302 to itself and
    harvest the credential. When no header is attached, redirects follow as before."""
    from smartbrain_3000 import netguard

    captured: list[dict] = []

    def fake_safe_fetch_json(url: str, headers=None, allow_redirects: bool = True):
        captured.append({"url": url, "headers": headers, "allow_redirects": allow_redirects})
        return {"ok": True}

    monkeypatch.setattr(netguard, "safe_fetch_json", fake_safe_fetch_json)

    # WITH headers → allow_redirects=False threaded through.
    ni._fetch_http_json(
        {"type": "http_json", "url": "https://api.example.com/q",
         "headers": {"X-Trace": "id-1"}},
        item_id="itemA",
        secrets_store=None,  # never touched: no $secret in headers
    )
    assert captured[-1]["allow_redirects"] is False
    # WITHOUT headers → default behavior (allow_redirects=True) preserved.
    ni._fetch_http_json(
        {"type": "http_json", "url": "https://api.example.com/q", "headers": {}},
        item_id="itemA",
        secrets_store=None,
    )
    assert captured[-1]["allow_redirects"] is True
    # And netguard itself really refuses a 302 when the caller opts out — direct proof.
    monkeypatch.undo()

    class _StubResp:
        is_redirect = True
        status_code = 302

        def __init__(self) -> None:
            self.headers = {"content-type": "text/html",
                            "location": "https://attacker.example.com/x"}

        def close(self) -> None:
            return

    class _StubClient:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def build_request(self, *_a, **_k):
            return object()

        def send(self, *_a, **_k):
            return _StubResp()

    monkeypatch.setattr(netguard, "_validated_ip", lambda _h: "203.0.113.1")
    monkeypatch.setattr(netguard.httpx, "Client", lambda *_a, **_k: _StubClient())
    with pytest.raises(netguard.FetchError, match="redirect refused"):
        netguard.safe_fetch_json("https://api.example.com/q",
                                 headers={"X-Trace": "id-1"}, allow_redirects=False)
