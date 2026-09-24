"""HTTP tests for the Neural Interface API: lock gate, board shapes, validate/run,
PATCH restriction, delete cascade, and the Desktop-local credential PUT.

Only the LLM turn / model-source fetch is faked; every route drives the real FastAPI
app with a real DuckDB + migrations (mirrors test_schedule_routes.py).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import ni, tools
from smartbrain_3000.auth import relay_headers

_PHONE = relay_headers("phone-under-test")  # R14: phone authority (the relay credential)


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
    """Create an NI item via the INTERNAL factory (NI Foreman P2: creation left
    the model registry — the composer/card routes are the user surfaces, and
    the suite fabricates items in-process)."""
    body = _spec_body(**over)
    ctx = tools.ToolContext(ni=client.app.state.ni)
    return tools.INTERNAL_NI_TOOLS["create_ni_item"](ctx, body)["id"]


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
                   json={"name": "n", "value": "v", "host": "h"})
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
        "type": "http_page",
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

    sentinel_kb = object()
    client.app.state.kb = sentinel_kb

    def fake_run_item(store, item_id, *, gateway_mod, secrets_store,
                      schedules_store=None, kb=None):
        captured["id"] = item_id
        captured["kb"] = kb  # the route must thread app.state.kb (internal.kb items)
        return {"status": "ok", "duration_ms": 7}

    monkeypatch.setattr(ni, "run_item", fake_run_item)
    r = client.post(f"/api/ni/items/{iid}/run")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and captured["id"] == iid
    assert captured["kb"] is sentinel_kb


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
    r = client.put(f"/api/ni/items/{iid}/credential", headers=_PHONE,
                   json={"name": "api_key", "value": "s3cret", "host": "api.example.com"})
    assert r.status_code == 403
    # And no secret was stored.
    assert client.app.state.secret_store.get(f"ni:{iid}:api_key") is None


def test_credential_put_stores_and_never_echoes_value(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.put(f"/api/ni/items/{iid}/credential",
                   json={"name": "api_key", "value": "s3cret", "host": "api.example.com"})
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


def test_credential_put_journals_param_changed_entry(client: TestClient) -> None:
    """§28: a successful credential PUT lands a ``param_changed`` journal entry
    naming the param's user-visible label — the card history shows
    "credential '<label>' added". Value bytes never touch the summary.
    """
    _unlock(client)
    iid = _create_via_tool(client, params={
        "api_key": {"label": "Weather Provider Key",
                    "kind": "secret", "value": "ni:self:api_key"},
    }, source={"type": "http_page", "url": "https://api.example.com/x",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
               preview_payload={"text": "preview"})
    r = client.put(f"/api/ni/items/{iid}/credential",
                   json={"name": "api_key", "value": "s3cret", "host": "api.example.com"})
    assert r.status_code == 200
    journal = client.app.state.ni.read_journal(iid)
    param_rows = [e for e in journal if e["kind"] == "param_changed"]
    assert param_rows, "credential PUT must land a param_changed journal entry"
    assert "Weather Provider Key" in param_rows[-1]["summary"]
    assert "s3cret" not in param_rows[-1]["summary"]  # value never touches the summary
    assert param_rows[-1]["origin"] == "system"


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
        source={"type": "http_page",
                "url": "https://api.example.com/q",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    ctx = tools.ToolContext(ni=client.app.state.ni)
    iid = tools.INTERNAL_NI_TOOLS["create_ni_item"](ctx, body)["id"]
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

    D6 (audit 2026-09-09): also asserts the ``repaired`` list rides the same
    carrier surface (a §14 trial can succeed on a manual /run too). Previously
    the /run path silently dropped ``result['repaired']`` so the user missed the
    "<title> repaired itself" notice when they clicked Refresh themselves.
    """
    from smartbrain_3000 import scheduler as sched

    _unlock(client)
    iid = _create_via_tool(client)
    client.app.state.ni.commission(iid)  # /run refuses draft (K6) — advance out of it

    def fake_run_item(store, item_id, *, gateway_mod, secrets_store,
                      schedules_store=None, reserve_repair=None, kb=None):
        store.set_state(item_id, "broken")  # forces broken-transition posting
        return {"status": "ok", "duration_ms": 1,
                "alerts": [{"item_id": item_id, "title": "Watch",
                            "message": "manual fired"}],
                "repaired": [{"item_id": item_id, "title": "Watch"}]}

    monkeypatch.setattr(ni, "run_item", fake_run_item)
    r = client.post(f"/api/ni/items/{iid}/run")
    assert r.status_code == 200, r.text
    store = sched.ScheduleStore(client.app.state.dbx,
                                client.app.state.master_key)
    messages = [row["message"] for row in store.recent_runs()
                if row["schedule_title"] == "Neural Interface"]
    assert "manual fired" in messages
    assert any("is broken" in m for m in messages)
    assert any("Watch repaired itself" in m for m in messages), messages


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


# --- §17: GET /api/ni/notices (launcher tray poll) --------------------------

def _notices_store(client: TestClient):
    from smartbrain_3000 import scheduler as sched

    return sched.ScheduleStore(client.app.state.dbx, client.app.state.master_key)


def test_notices_requires_desktop_authority(client: TestClient) -> None:
    """A bridged-in remote device must never pull notice bodies (phone authority: 403)."""
    _unlock(client)
    assert client.get("/api/ni/notices", headers=_PHONE).status_code == 403


def test_notices_locked_returns_423(client: TestClient) -> None:
    """A locked vault yields no notices at all — the launcher reads 423 as 'skip'."""
    r = client.get("/api/ni/notices")
    assert r.status_code == 423


def test_notices_kind_mapping_via_carrier_posts(client: TestClient) -> None:
    """Each notice type posted through post_ni_carrier_notices maps to its §17 kind:
    fired alert -> "alert" (status complete), broken transition -> "broken",
    §14 self-repair -> "repaired" — with the sealed message as the body."""
    from smartbrain_3000 import scheduler as sched

    _unlock(client)
    sched.post_ni_carrier_notices(
        _notices_store(client),
        [{"item_id": "a", "title": "Watch", "message": "CPU is hot"}],
        [{"item_id": "b", "title": "Doomed"}],
        repaired=[{"item_id": "c", "title": "Mended"}],
    )
    rows = client.get("/api/ni/notices").json()
    assert len(rows) == 3
    by_kind = {r["kind"]: r for r in rows}
    assert set(by_kind) == {"alert", "broken", "repaired"}
    assert by_kind["alert"]["body"] == "CPU is hot"
    assert "Doomed is broken" in by_kind["broken"]["body"]
    assert "Mended repaired itself" in by_kind["repaired"]["body"]
    for row in rows:
        assert set(row) == {"id", "kind", "body", "ts"}
        assert isinstance(row["id"], int) and row["id"] > 0 and row["ts"]


def test_notices_newest_first_and_ids_stable_monotonic(client: TestClient) -> None:
    """The launcher dedupes by highest-seen id, so ids must be stable across polls
    and ordered by recency (newest first = non-increasing down the list)."""
    import time as _time

    _unlock(client)
    store = _notices_store(client)
    store.record_ni_run("complete", "first")
    _time.sleep(0.002)  # distinct microsecond timestamps -> strictly ordered ids
    store.record_ni_run("complete", "second")
    rows = client.get("/api/ni/notices").json()
    assert [r["body"] for r in rows] == ["second", "first"]
    assert rows[0]["id"] > rows[1]["id"]
    again = client.get("/api/ni/notices").json()
    assert again == rows  # stable: the same rows answer with the same ids


# --- Phase 4c: §24 image serve route --------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _image_scene() -> dict:
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "image", "alt": "radar frame"},
    ]}


def _image_spec_body() -> dict:
    return _spec_body(
        source={"type": "http_image",
                "url": "https://cdn.example.com/radar.png",
                "headers": {}},
        scene=_image_scene(),
        preview_payload={"image": {"bytes_len": 0, "format": "png"}},
    )


def test_image_route_locked_returns_423(client: TestClient) -> None:
    """§24: the serving route lives behind the standard 423 locked contract."""
    assert client.get("/api/ni/items/anything/image").status_code == 423


def test_image_route_404_when_absent(client: TestClient) -> None:
    """§24: a draft item has no image slot yet; the route 404s cleanly."""
    _unlock(client)
    iid = _create_via_tool(client, **_image_spec_body())
    r = client.get(f"/api/ni/items/{iid}/image")
    assert r.status_code == 404


def test_image_route_serves_sniffed_media_type_no_store(client: TestClient) -> None:
    """§24: after sealing an image slot the route returns the SNIFFED type with
    Cache-Control: no-store — never the served header, which is untrusted."""
    _unlock(client)
    iid = _create_via_tool(client, **_image_spec_body())
    body = _PNG_MAGIC + b"\x00\x01\x02\x03pretend-pixels"
    client.app.state.ni.write_image_snapshot(iid, body, "png")
    r = client.get(f"/api/ni/items/{iid}/image")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["cache-control"] == "no-store"
    # Phase 4c audit 2026-09-11 (finding #5): nosniff is the LOAD-BEARING polyglot
    # defence — a PNG the sniffer accepted could still parse as HTML in a browser
    # that ignores our declared Content-Type. The hardening middleware supplies
    # the header globally; freezing it in this suite so a regression that
    # short-circuits the middleware for this route fails LOUD.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == body


def test_update_ni_item_source_change_deletes_image_slot(
    client: TestClient,
) -> None:
    """Phase 4c audit 2026-09-11 (finding #7): stale pixels must not survive a
    source change — the re-consent point in the update tool clears the image
    slot alongside setting the item back to commissioning. The route serves 404
    until the next successful run seals fresh bytes.

    Drives the tools chokepoint (real audited path) — approve a create_ni_item
    with http_image, seal image bytes manually, approve an update that swings
    the source elsewhere, and expect the image route to 404.
    """
    _unlock(client)
    iid = _create_via_tool(client, **_image_spec_body())
    body = _PNG_MAGIC + b"\x00\x01\x02\x03pretend-pixels"
    client.app.state.ni.write_image_snapshot(iid, body, "png")
    assert client.get(f"/api/ni/items/{iid}/image").status_code == 200
    # Approve an update that swaps the source (type changes AWAY from http_image).
    scene_text_only = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    r = client.post("/api/tools/invoke",
                    json={"name": "update_ni_item",
                          "args": {"item_id": iid,
                                    "source": {"type": "model",
                                                "instruction": "hi"},
                                    "scene": scene_text_only,
                                    "preview_payload": {"text": "preview"}}})
    # NI Foreman P2: update left the registry — invoke 404s; drive the update
    # via the internal factory (the state_reset contract is unchanged).
    assert r.status_code == 404
    ctx = tools.ToolContext(ni=client.app.state.ni)
    out = tools.INTERNAL_NI_TOOLS["update_ni_item"](ctx, {
        "item_id": iid,
        "source": {"type": "model", "instruction": "hi"},
        "scene": scene_text_only,
        "preview_payload": {"text": "preview"}})
    assert out["state_reset"] == "commissioning"
    # Image slot is gone; route now 404s until the next successful run seals bytes.
    assert client.get(f"/api/ni/items/{iid}/image").status_code == 404


def test_pending_tile_composite_titles_resolves_referenced_items(
    client: TestClient,
) -> None:
    """§25: a parked create_ni_item with an internal.ni source has referenced
    item ids resolved to titles for the tile's promoted "Combines: …" line."""
    from smartbrain_3000 import agent_routes

    _unlock(client)
    a = _create_via_tool(client, title="Weather A")
    b = _create_via_tool(client, title="Weather B")
    store = client.app.state.ni
    row = {"id": "p1", "tool": "create_ni_item",
           "args": {"source": {"type": "internal.ni",
                                "items": {"alpha": a, "beta": b}}}}
    titles = agent_routes._resolve_pending_composite_titles(row, store)
    assert set(titles) == {"Weather A", "Weather B"}


def test_pending_tile_composite_titles_none_for_non_composite() -> None:
    """§25 side channel: absent for a non-composite tool; the tile still renders."""
    from smartbrain_3000 import agent_routes

    row = {"id": "p1", "tool": "web_fetch",
           "args": {"url": "https://example.com"}}
    assert agent_routes._resolve_pending_composite_titles(row, None) is None


def test_pending_tile_composite_titles_drops_missing_reference(
    client: TestClient,
) -> None:
    """§25: a deleted / unknown reference silently drops from the list — the
    tile never fabricates a title, and the resolver keeps the ones that exist."""
    from smartbrain_3000 import agent_routes

    _unlock(client)
    live_id = _create_via_tool(client, title="Real")
    store = client.app.state.ni
    row = {"id": "p1", "tool": "update_ni_item",
           "args": {"source": {"type": "internal.ni",
                                "items": {"a": live_id, "b": "does-not-exist"}}}}
    titles = agent_routes._resolve_pending_composite_titles(row, store)
    assert titles == ["Real"]


def test_notices_limit_clamped_and_defaulted(client: TestClient) -> None:
    _unlock(client)
    store = _notices_store(client)
    for i in range(25):  # bounded: just past the ≤20 clamp
        store.record_ni_run("complete", f"m{i}")
    assert len(client.get("/api/ni/notices").json()) == 10  # default
    assert len(client.get("/api/ni/notices?limit=50").json()) == 20  # clamp
    assert len(client.get("/api/ni/notices?limit=0").json()) == 1  # floor
    assert len(client.get("/api/ni/notices?limit=5").json()) == 5


# --- Integrated audit (2026-09-12): route-side regressions ----------------

def test_L7_manual_run_marks_checked_so_tick_skips_the_item(client: TestClient) -> None:
    """L7: /api/ni/items/{id}/run must ``mark_checked`` BEFORE running so a
    concurrent tick's ``due_items`` gate no longer includes the item — closes
    the double-run window that ``clear_last_checked`` created. Assertion: the
    item's ``last_status`` = 'manual' immediately after /run begins (we don't
    complete the run; the store side-effect proves the ordering).
    """
    _unlock(client)
    iid = _create_via_tool(client)
    client.app.state.ni.commission(iid)

    def spy_run_item(store, item_id, **_kw):
        # Prove the manual mark landed BEFORE run_item was called.
        item = store.get_item(item_id)
        assert item["last_status"] == "manual", (
            "L7: /run must mark_checked('manual') BEFORE running"
        )
        assert item["last_checked"] is not None, (
            "L7: last_checked must be set (not NULL) at run entry"
        )
        return {"status": "ok", "duration_ms": 1}

    import smartbrain_3000.ni as ni_mod
    orig = ni_mod.run_item
    ni_mod.run_item = spy_run_item
    try:
        r = client.post(f"/api/ni/items/{iid}/run")
        assert r.status_code == 200, r.text
    finally:
        ni_mod.run_item = orig
    # ni.due_items would NOT surface an item whose last_checked is fresh — invariant.
    due = client.app.state.ni.due_items()
    assert iid not in [i["id"] for i in due], (
        "L7: an item with fresh last_checked must not appear in due_items"
    )


def test_S5_commission_refuses_when_template_placeholder_has_no_credential(
    client: TestClient,
) -> None:
    """S5: an installed template with ``ni:self:api_key`` placeholder in the
    secret param must refuse commission (409) until the credential is PUT."""
    _unlock(client)
    # Fabricate an item whose secret param carries a ``ni:...`` placeholder as
    # if the install path had just landed. The real install rewrites ni:self →
    # ni:<id>; simulate both shapes to prove the guard.
    body = _spec_body(
        params={"api_key": {"label": "Key", "kind": "secret",
                             "value": "ni:self:api_key"}},
        source={"type": "http_page",
                 "url": "https://api.example.com/q",
                 "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    ctx = tools.ToolContext(ni=client.app.state.ni)
    iid = tools.INTERNAL_NI_TOOLS["create_ni_item"](ctx, body)["id"]
    # No credential entered yet — placeholder must be treated as unfilled.
    r2 = client.post(f"/api/ni/items/{iid}/commission")
    assert r2.status_code == 409 and "secret" in r2.json()["detail"], r2.text
    # After the credential PUT lands, commission succeeds.
    put = client.put(
        f"/api/ni/items/{iid}/credential",
        json={"name": "api_key", "value": "s3cret", "host": "api.example.com"},
    )
    assert put.status_code == 200, put.text
    # The item's spec param.value still carries the placeholder (only credential value
    # went into the SecretStore); with the SecretStore now holding it, commission accepts.
    # NB: create_ni_item rewrote ni:self → ni:<iid>, so verify the resolved key exists.
    resolved_key = f"ni:{iid}:api_key"
    stored = client.app.state.secret_store.get(resolved_key)
    # put_credential seals a JSON envelope {"value","host"}; decode to prove
    # the raw value made it under the expected item-scoped key.
    assert stored is not None, (
        f"S5 setup: credential must land under {resolved_key!r}"
    )
    import json as _json
    assert _json.loads(stored)["value"] == "s3cret", (
        f"S5 setup: credential value under {resolved_key!r} must be 's3cret'"
    )
    # Manually stamp the spec.params.value back to a ni:<iid>:api_key so the S5
    # guard has something to look up — mirrors what real install rewrites.
    store = client.app.state.ni
    current = store.get_item(iid)["spec"]
    current["params"]["api_key"]["value"] = resolved_key
    store.conn.execute(
        "UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
        [*store._seal_item(iid, current), iid],
    )
    r3 = client.post(f"/api/ni/items/{iid}/commission")
    assert r3.status_code == 200, r3.text


def test_S6_commission_writes_audit_row_with_item_id_only(
    client: TestClient,
) -> None:
    """S6: a successful commission writes an audit row (metadata: item_id
    only — no titles / content)."""
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.post(f"/api/ni/items/{iid}/commission")
    assert r.status_code == 200, r.text
    entries = client.get("/api/audit").json()["entries"]
    rows = [e for e in entries if e["tool"] == "ni_commission"]
    assert rows, "S6: commission must write an audit row"
    assert iid in rows[0]["args_summary"]
    # No sealed titles / content ride the audit body.
    import json as _json
    body = _json.dumps(rows[0])
    assert "Weather" not in body, "audit body must not carry the item's title"


# --- needs_params wave (2026-09-14) ------------------------------------------

def _keyless_param_item(client: TestClient, value: str = "") -> str:
    """A draft item whose http_page URL references a non-secret param."""
    return _create_via_tool(
        client,
        title=f"Param card {value or 'empty'}",
        params={"city": {"label": "City name", "kind": "string", "value": value}},
        source={"type": "http_page",
                "url": "https://api.example.com/w?city={{param:city}}",
                "headers": {}},
    )


def test_board_row_carries_needs_params_for_unfilled_slot(client: TestClient) -> None:
    """D2: an unfilled referenced non-secret param rides the board row so the
    card can render the fill affordance (mirrors needs_credentials)."""
    _unlock(client)
    iid = _keyless_param_item(client)
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["needs_params"] == [{"name": "city", "label": "City name"}]
    filled = _keyless_param_item(client, value="Kansas City")
    board = client.get("/api/ni/board").json()
    row2 = next(i for i in board["items"] if i["id"] == filled)
    assert row2["needs_params"] == []


def test_commission_refuses_unfilled_nonsecret_param(client: TestClient) -> None:
    """D2: Activate on a card whose referenced param is empty 409s with the
    param named — the $0.00-Finnhub class never reaches the engine."""
    _unlock(client)
    iid = _keyless_param_item(client)
    r = client.post(f"/api/ni/items/{iid}/commission")
    assert r.status_code == 409
    assert "city" in r.json()["detail"] and "City name" in r.json()["detail"]


def test_param_put_fills_value_and_unblocks_commission(client: TestClient) -> None:
    """D2: the param PUT collects the value (desktop-local), journals it, and
    commission then succeeds."""
    _unlock(client)
    iid = _keyless_param_item(client)
    r = client.put(f"/api/ni/items/{iid}/param",
                   json={"name": "city", "value": "Kansas City"})
    assert r.status_code == 200 and r.json()["needs_params"] == []
    item = client.app.state.ni.get_item(iid)
    assert item["spec"]["params"]["city"]["value"] == "Kansas City"
    journal = client.app.state.ni.read_journal(iid)
    assert any(e["kind"] == "param_changed" and "City name" in e["summary"]
               for e in journal)
    assert client.post(f"/api/ni/items/{iid}/commission").status_code == 200


def test_param_put_refuses_secret_params_and_unknown_names(client: TestClient) -> None:
    """D2: secrets belong to the credential PUT (409); unknown names 404."""
    _unlock(client)
    iid = _create_via_tool(
        client,
        title="Keyed param card",
        params={"api_key": {"label": "Key", "kind": "secret",
                             "value": "ni:self:api_key"}},
        source={"type": "http_page", "url": "https://api.example.com/q",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    r = client.put(f"/api/ni/items/{iid}/param",
                   json={"name": "api_key", "value": "sk-x"})
    assert r.status_code == 409
    r2 = client.put(f"/api/ni/items/{iid}/param",
                    json={"name": "nope", "value": "x"})
    assert r2.status_code == 404


# --- C2-feedback wave (2026-09-15) --------------------------------------------

def _commissioned_model_item(client: TestClient) -> str:
    """A commissioning item with a model source (no egress) + a latest snapshot."""
    iid = _create_via_tool(client, title="C2 target", draft=True)
    store = client.app.state.ni
    store.commission(iid)
    return iid


def test_F1_looks_right_kicks_the_c3_run_immediately(client: TestClient,
                                                      monkeypatch) -> None:
    """F1: the ok=true verdict runs the item synchronously (the field run left
    a 30-minute dead-button window); the response reports the post-run state."""
    _unlock(client)
    iid = _commissioned_model_item(client)
    fired: dict = {}

    def _fake_run(store, item_id, **kwargs):
        fired["id"] = item_id
        store.record_run(item_id, "ok", duration_ms=1, error=None, contract_ok=None)
        return {"alerts": [], "repaired": []}

    from smartbrain_3000 import ni as nimod2
    monkeypatch.setattr(nimod2, "run_item", _fake_run)
    r = client.post(f"/api/ni/items/{iid}/validate", json={"ok": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert fired.get("id") == iid, "the verdict must kick the C3 run"
    assert body["run"] == "ok"
    # The verdict is stamped regardless of what the run did.
    assert client.app.state.ni.get_item(iid)["spec"].get("_c2_ok") is True


def test_F1_run_failure_never_masks_the_recorded_verdict(client: TestClient,
                                                          monkeypatch) -> None:
    """A C3 kick failure reports run=error but the validate still succeeds."""
    _unlock(client)
    iid = _commissioned_model_item(client)

    def _boom(store, item_id, **kwargs):
        raise ni.NIError("fetch_failed")

    from smartbrain_3000 import ni as nimod2
    monkeypatch.setattr(nimod2, "run_item", _boom)
    r = client.post(f"/api/ni/items/{iid}/validate", json={"ok": True})
    assert r.status_code == 200, r.text
    assert r.json()["run"] == "error"
    assert client.app.state.ni.get_item(iid)["spec"].get("_c2_ok") is True


def test_F1_wrong_verdict_never_runs(client: TestClient, monkeypatch) -> None:
    """ok=false rewinds to draft and must NOT kick a run."""
    _unlock(client)
    iid = _commissioned_model_item(client)
    fired: dict = {}

    def _fake_run(store, item_id, **kwargs):
        fired["id"] = item_id
        return {}

    from smartbrain_3000 import ni as nimod2
    monkeypatch.setattr(nimod2, "run_item", _fake_run)
    r = client.post(f"/api/ni/items/{iid}/validate",
                    json={"ok": False, "note": "wrong number"})
    assert r.status_code == 200
    assert "id" not in fired, "a rejected first run must not refresh"
    assert client.app.state.ni.get_item(iid)["state"] == "draft"


def test_F2_board_row_exposes_c2_ok(client: TestClient, monkeypatch) -> None:
    """F2: the sealed _c2_ok attestation rides the board row so the card stops
    re-asking after the user answered."""
    _unlock(client)
    iid = _commissioned_model_item(client)
    row = next(i for i in client.get("/api/ni/board").json()["items"]
               if i["id"] == iid)
    assert row["c2_ok"] is False
    from smartbrain_3000 import ni as nimod2
    monkeypatch.setattr(nimod2, "run_item",
                        lambda store, item_id, **kw: {"alerts": [], "repaired": []})
    assert client.post(f"/api/ni/items/{iid}/validate",
                       json={"ok": True}).status_code == 200
    row2 = next(i for i in client.get("/api/ni/board").json()["items"]
                if i["id"] == iid)
    assert row2["c2_ok"] is True


def test_W2_commission_refuses_unfinalized_flow_shell(client: TestClient) -> None:
    """W2 (field 2026-09-15): a user Activated a failed flow's shell; the
    placeholder model source ran and reported 'ok' on a card rendering
    'Preparing card…' forever. The commission door now refuses shells and the
    board row carries the flag so the card hides Activate."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    store = client.app.state.ni
    iid = ni_flow.create_shell_item(store, "a flow that will never finish")
    row = next(i for i in client.get("/api/ni/board").json()["items"]
               if i["id"] == iid)
    assert row["shell"] is True
    r = client.post(f"/api/ni/items/{iid}/commission")
    assert r.status_code == 409 and "never finished" in r.json()["detail"]
    # A finalized card (spec replaced) commissions normally — proven across
    # the existing flow suites; here we only pin the refusal.


# --- card-consent wave (2026-09-15) --------------------------------------------

def _paused_recipe_flow(client: TestClient, recipe_id: str = "crypto-price-btc-usd",
                         request_text: str = "bitcoin price please",
                         intent: dict | None = None) -> str:
    """A shell item paused at confirm_source with the recipe's sealed disclosure."""
    from smartbrain_3000 import ni_catalog, ni_flow
    store = client.app.state.ni
    recipe = ni_catalog.get_recipe(recipe_id)
    assert recipe is not None
    item_id = ni_flow.create_shell_item(store, request_text)
    ni_flow._pause_for_recipe_confirm(
        store, item_id,
        intent or {"subject": "Bitcoin", "cadence_minutes": 15, "place": None,
                   "wants": ["price"]},
        recipe)
    return item_id


def test_card_consent_board_carries_the_sealed_disclosure(client: TestClient) -> None:
    """The tile renders the consent from the SEALED record: exact URL, recipe
    title, and any not-covered wants — no model relay involved."""
    _unlock(client)
    iid = _paused_recipe_flow(
        client, "stock-quote-finnhub", "NVDA price and volume every 22 minutes",
        {"subject": "NVDA", "cadence_minutes": 22, "place": None,
         "wants": ["price", "volume"]})
    row = next(i for i in client.get("/api/ni/board").json()["items"]
               if i["id"] == iid)
    flow = row["flow"]
    assert flow["state"] == "confirm_source"
    assert flow["source_url"].startswith("https://finnhub.io/api/v1/quote")
    assert flow["recipe_title"], "recipe title must ride for the card copy"
    assert flow["not_covered"] == ["volume"]


def test_card_consent_approve_runs_the_flow_synchronously(client: TestClient) -> None:
    """[Approve source] executes the continuation from the sealed record —
    keyless recipe settles ready + commissioning in the same request."""
    _unlock(client)
    iid = _paused_recipe_flow(client)
    r = client.post(f"/api/ni/items/{iid}/flow/confirm-source")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"
    item = client.app.state.ni.get_item(iid)
    assert item["state"] == "commissioning"
    assert "_shell" not in item["spec"]
    journal = client.app.state.ni.read_journal(iid)
    assert any(e["kind"] == "source_changed" and "approved the source" in e["summary"]
               for e in journal)


def test_card_consent_decline_reenters_the_source_pick(client: TestClient) -> None:
    """G1: [Not this source] is a fork, not a death — the flow re-enters the
    ``source`` pick pause (suggestions + paste-URL render from that state),
    never fetches, and the shell stays refusing commission."""
    _unlock(client)
    iid = _paused_recipe_flow(client)
    r = client.post(f"/api/ni/items/{iid}/flow/decline-source")
    assert r.status_code == 200 and r.json()["state"] == "source"
    from smartbrain_3000 import ni_flow
    record = ni_flow._flow_read(client.app.state.ni, iid)
    assert record["state"] == "source"
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    assert row["flow"]["state"] == "source" and "suggestions" in row["flow"]
    item = client.app.state.ni.get_item(iid)
    assert item["state"] == "draft" and item["spec"].get("_shell") is True
    assert client.post(f"/api/ni/items/{iid}/commission").status_code == 409


def test_card_consent_routes_409_without_a_pending_confirm(client: TestClient) -> None:
    """No pause ⇒ 409 for both routes (idempotence: a raced second tap too)."""
    _unlock(client)
    iid = _create_via_tool(client)
    for path in ("flow/confirm-source", "flow/decline-source"):
        r = client.post(f"/api/ni/items/{iid}/{path}")
        assert r.status_code == 409, (path, r.text)
    # Approve once, then the second tap 409s honestly.
    iid2 = _paused_recipe_flow(client, request_text="second bitcoin card",
                                intent={"subject": "BTC2", "cadence_minutes": 15,
                                        "place": None, "wants": ["price"]})
    assert client.post(f"/api/ni/items/{iid2}/flow/confirm-source").status_code == 200
    assert client.post(f"/api/ni/items/{iid2}/flow/confirm-source").status_code == 409


# --- NI Foreman P1: composer intake + retry (2026-09-16) ----------------------

def test_intake_creates_shell_and_starts_worker(client: TestClient,
                                                 monkeypatch) -> None:
    """The composer's sentence goes straight to the flow: shell card first
    (instant acknowledgment), worker spawned, audited as a user action."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda store, iid, **kw: fired.update(id=iid, **kw) or True)
    r = client.post("/api/ni/intake",
                    json={"request": "NVDA stock price every 28 minutes"})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    assert fired["id"] == iid and fired.get("source_url") is None
    item = client.app.state.ni.get_item(iid)
    assert item is not None and item["state"] == "draft"
    assert item["spec"].get("_shell") is True
    entries = client.get("/api/audit").json()["entries"]
    assert any(e["tool"] == "ni_intake" for e in entries)


def test_intake_duplicate_title_409s_naming_the_card(client: TestClient,
                                                      monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **k: True)
    body = {"request": "the exact same card"}
    assert client.post("/api/ni/intake", json=body).status_code == 200
    r = client.post("/api/ni/intake", json=body)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    # allow_duplicate opts in, mirroring the flow tools.
    assert client.post("/api/ni/intake",
                       json={**body, "allow_duplicate": True}).status_code == 200


def test_intake_validates_source_url_shape(client: TestClient) -> None:
    _unlock(client)
    r = client.post("/api/ni/intake",
                    json={"request": "watch this", "source_url": "ftp://nope"})
    assert r.status_code == 400 and "source_url" in r.json()["detail"]


def test_retry_reruns_a_failed_shell_flow(client: TestClient, monkeypatch) -> None:
    """Retry re-runs the SAME sealed request; a fetch-class failure drops the
    URL (fresh source resolution), a running/healthy flow refuses."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    store = client.app.state.ni
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, iid, **kw: fired.update(id=iid, **kw) or True)
    iid = client.post("/api/ni/intake", json={"request": "retry me please"}).json()["id"]
    # Running flow (state intent) → 409.
    assert client.post(f"/api/ni/items/{iid}/flow/retry").status_code == 409
    # Terminal fetch failure → retry WITHOUT the url.
    ni_flow._fail(store, iid, "fetch", "sample fetch failed: FetchError")
    record = ni_flow._flow_read(store, iid)
    record["source_url"] = "https://query1.finance.yahoo.com/bad"
    ni_flow._flow_write(store, iid, record)
    fired.clear()
    r = client.post(f"/api/ni/items/{iid}/flow/retry")
    assert r.status_code == 200, r.text
    assert fired["id"] == iid and fired.get("source_url") is None
    # Terminal non-fetch failure keeps the user's URL.
    ni_flow._fail(store, iid, "mapping", "mapping stage failed after retry")
    record = ni_flow._flow_read(store, iid)
    record["source_url"] = "https://api.example.com/mine"
    ni_flow._flow_write(store, iid, record)
    fired.clear()
    assert client.post(f"/api/ni/items/{iid}/flow/retry").status_code == 200
    assert fired.get("source_url") == "https://api.example.com/mine"


def test_retry_refuses_finished_cards(client: TestClient) -> None:
    """A finalized (non-shell) card has nothing to retry — remap/fix owns it."""
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.post(f"/api/ni/items/{iid}/flow/retry")
    assert r.status_code == 409 and "unfinished" in r.json()["detail"]


# --- P3 card affordances: pick-source / pick-recipe / fix / rename+cadence --

def _seed_source_pause(client: TestClient, monkeypatch, request_text: str) -> str:
    """Intake a shell (worker stubbed) and force its flow to the ``source``
    pick pause — the state the P3 card affordances operate on."""
    from smartbrain_3000 import ni_flow
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, iid, **kw: True)
    iid = client.post("/api/ni/intake", json={"request": request_text}).json()["id"]
    store = client.app.state.ni
    record = ni_flow._flow_read(store, iid)
    record["state"] = "source"
    record["error"] = ni_flow.AWAITING_SOURCE_PICK
    ni_flow._flow_write(store, iid, record)
    return iid


def test_pick_source_resumes_pause_with_user_pasted_url(
        client: TestClient, monkeypatch) -> None:
    """P3: the card's paste-a-URL form — the user's paste IS the consent; the
    worker resumes sampling against exactly that URL."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    iid = _seed_source_pause(client, monkeypatch, "show my metric")
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, i, **kw: fired.update(id=i, **kw) or True)
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source",
                    json={"url": "https://api.example.com/metric.json"})
    assert r.status_code == 200 and r.json()["started"] is True, r.text
    assert fired["id"] == iid
    assert fired["source_url"] == "https://api.example.com/metric.json"


def test_pick_source_rejects_bad_url_shape_and_wrong_state(
        client: TestClient, monkeypatch) -> None:
    """P3: a non-https / non-URL paste bounces 400 with the validator text;
    any flow state other than ``source`` refuses 409 (nothing to pick)."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    iid = _seed_source_pause(client, monkeypatch, "show my metric")
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source",
                    json={"url": "ftp://example.com/x"})
    assert r.status_code == 400 and r.json()["detail"].startswith("url:"), r.text
    store = client.app.state.ni
    record = ni_flow._flow_read(store, iid)
    record["state"] = "sampling"
    ni_flow._flow_write(store, iid, record)
    r2 = client.post(f"/api/ni/items/{iid}/flow/pick-source",
                     json={"url": "https://api.example.com/metric.json"})
    assert r2.status_code == 409 and "asking for a source" in r2.json()["detail"]


def test_pick_recipe_routes_into_confirm_source_pause(
        client: TestClient, monkeypatch) -> None:
    """P3: tapping a vetted suggestion never fetches — it lands the standard
    Approve-source consent pause carrying the recipe's exact URL."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    iid = _seed_source_pause(client, monkeypatch, "usd to eur rate")
    # Unknown recipe id → 404, pause untouched (probe BEFORE the real pick —
    # a successful pick consumes the ``source`` pause).
    r0 = client.post(f"/api/ni/items/{iid}/flow/pick-recipe",
                     json={"recipe_id": "no-such-recipe"})
    assert r0.status_code == 404
    r = client.post(f"/api/ni/items/{iid}/flow/pick-recipe",
                    json={"recipe_id": "fx-usd-eur"})
    assert r.status_code == 200 and r.json()["state"] == "confirm_source", r.text
    record = ni_flow._flow_read(client.app.state.ni, iid)
    assert record["state"] == "confirm_source"
    from urllib.parse import urlparse
    assert urlparse(str(record.get("source_url") or "")).hostname == "api.frankfurter.dev"


def test_board_source_pause_exposes_deterministic_suggestions(
        client: TestClient, monkeypatch) -> None:
    """P3: while paused at ``source`` the board row carries the scorer's
    vetted suggestions (id/title/host/url) so the card renders them from code
    alone — no chat model relay."""
    _unlock(client)
    iid = _seed_source_pause(client, monkeypatch, "bitcoin price in usd")
    rows = client.get("/api/ni/board").json()["items"]
    row = next(x for x in rows if x["id"] == iid)
    suggestions = row["flow"]["suggestions"]
    assert suggestions, "the pick pause must surface vetted suggestions"
    assert {"recipe_id", "title", "host", "url"} <= set(suggestions[0])
    assert any(s["recipe_id"] == "crypto-price-btc-usd" for s in suggestions)


def test_fix_route_starts_remap_against_own_frozen_source(
        client: TestClient, monkeypatch) -> None:
    """P3: the card's Fix re-enters the flow at sampling with a ``_remap``
    record bound to the item's OWN frozen URL — never a new host."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    # One-door law: the create factory refuses http_json (flow-born only), so
    # flip the finalized spec at store level — exactly what a flow finalize
    # writes.
    iid = _create_via_tool(client)
    store = client.app.state.ni
    spec = dict(store.get_item(iid)["spec"])
    spec["source"] = {"type": "http_json", "url": "https://api.example.com/q"}
    store.update_spec(iid, spec, origin="user")
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, i, **kw: fired.update(id=i, **kw) or True)
    r = client.post(f"/api/ni/items/{iid}/flow/fix")
    assert r.status_code == 200 and r.json()["started"] is True, r.text
    record = ni_flow._flow_read(store, iid)
    assert record["_remap"] is True and record["state"] == "sampling"
    assert fired["source_url"] == "https://api.example.com/q"


def test_fix_route_409_surfaces_begin_remap_guidance(
        client: TestClient, monkeypatch) -> None:
    """P3: begin_remap's refusals (a sourceless shell here) surface as 409
    with the user-facing guidance, not a 500. (P2: page cards are fixable
    too, so the guidance names both API and web-page sources.)"""
    _unlock(client)
    iid = _seed_source_pause(client, monkeypatch, "fix a shell")
    r = client.post(f"/api/ni/items/{iid}/flow/fix")
    assert r.status_code == 409, r.text
    assert "web-page sources" in r.json()["detail"]


def test_patch_title_renames_card_and_journals(client: TestClient) -> None:
    """P3 Edit modal: PATCH title rewrites the sealed spec title (attestations
    preserved) and journals the rename."""
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.patch(f"/api/ni/items/{iid}", json={"title": "My Renamed Card"})
    assert r.status_code == 200, r.text
    item = client.app.state.ni.get_item(iid)
    assert item["spec"]["title"] == "My Renamed Card"
    journal = client.app.state.ni.read_journal(iid)
    assert any("renamed to" in e["summary"] for e in journal)
    # Pydantic bounds: an over-long title never reaches the store.
    assert client.patch(f"/api/ni/items/{iid}",
                        json={"title": "x" * 301}).status_code == 422


def test_patch_interval_updates_cadence_and_journals(client: TestClient) -> None:
    """P3 Edit modal: PATCH interval_minutes updates the sealed cadence
    (operational field — attestations preserved) and journals it; out-of-range
    values bounce at the schema."""
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.patch(f"/api/ni/items/{iid}", json={"interval_minutes": 21})
    assert r.status_code == 200, r.text
    item = client.app.state.ni.get_item(iid)
    assert item["spec"]["interval_minutes"] == 21
    assert item["interval_minutes"] == 21, "plaintext cadence column follows the spec"
    journal = client.app.state.ni.read_journal(iid)
    assert any("every 21m" in e["summary"] for e in journal)
    assert client.patch(f"/api/ni/items/{iid}",
                        json={"interval_minutes": 0}).status_code == 422
    assert client.patch(f"/api/ni/items/{iid}",
                        json={"interval_minutes": 10081}).status_code == 422


# --- G1: answer / reopen / findings routes ---------------------------------

def test_answer_route_resumes_supply_date_terminal(client: TestClient, monkeypatch) -> None:
    """G1: an answerable unsupported (computed ask, no date) exposes the
    supply_date question on the board; the user's typed date resumes the flow
    with ``_supplied`` stamped (their answer is the truth, never a model's)."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, iid, **kw: True)
    iid = client.post("/api/ni/intake", json={"request": "countdown to the vote"}).json()["id"]
    store = client.app.state.ni
    ni_flow._terminate_unsupported(
        store, iid,
        "computed-only requires an explicit YYYY-MM-DD date in the request",
        question={"kind": "supply_date",
                   "prompt": "When is it? Add the date as YYYY-MM-DD."})
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    assert row["flow"]["question"]["kind"] == "supply_date"
    assert row["flow"]["reason"]
    # Malformed date bounces 400 with actionable text; the question stays.
    bad = client.post(f"/api/ni/items/{iid}/flow/answer",
                      json={"kind": "supply_date", "value": "November 3rd"})
    assert bad.status_code == 400 and "YYYY-MM-DD" in bad.json()["detail"]
    good = client.post(f"/api/ni/items/{iid}/flow/answer",
                       json={"kind": "supply_date", "value": "2026-11-03"})
    assert good.status_code == 200 and good.json()["started"] is True
    record = ni_flow._flow_read(store, iid)
    assert record["_supplied"]["date"] == "2026-11-03"
    assert record["state"] == "intent"
    # The question is consumed — answering again 409s.
    assert client.post(f"/api/ni/items/{iid}/flow/answer",
                       json={"kind": "supply_date", "value": "2026-01-01"}).status_code == 409


def test_answer_route_409_when_no_question_pending(client: TestClient) -> None:
    _unlock(client)
    iid = _create_via_tool(client)
    r = client.post(f"/api/ni/items/{iid}/flow/answer",
                    json={"kind": "supply_date", "value": "2026-01-01"})
    assert r.status_code == 409 and "not asking" in r.json()["detail"]


def test_reopen_route_reenters_pick_for_failed_shells_only(
        client: TestClient, monkeypatch) -> None:
    """G1: reopen = the 'pick a different source' way out. Shell + terminal
    only; finalized cards 409 toward Fix."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda s, iid, **kw: True)
    iid = client.post("/api/ni/intake", json={"request": "a source that died"}).json()["id"]
    store = client.app.state.ni
    # Non-terminal → 409.
    assert client.post(f"/api/ni/items/{iid}/flow/reopen").status_code == 409
    ni_flow._fail(store, iid, "fetch", "sample fetch failed: HTTPError")
    r = client.post(f"/api/ni/items/{iid}/flow/reopen")
    assert r.status_code == 200 and r.json()["state"] == "source"
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    assert row["flow"]["state"] == "source" and "suggestions" in row["flow"]
    # Finalized card → 409 (Fix owns re-derivation).
    iid2 = _create_via_tool(client)
    assert client.post(f"/api/ni/items/{iid2}/flow/reopen").status_code == 409


def test_findings_routes_list_and_resolve(client: TestClient) -> None:
    from smartbrain_3000 import ni_watch
    _unlock(client)
    conn = client.app.state.ni.conn
    fid = ni_watch.file_finding(conn, "create", "warn", "a route-level finding")
    finds = client.get("/api/ni/findings").json()["findings"]
    assert any(f["id"] == fid for f in finds)
    assert client.post(f"/api/ni/findings/{fid}/resolve").status_code == 200
    assert client.post(f"/api/ni/findings/{fid}/resolve").status_code == 404
    finds2 = client.get("/api/ni/findings").json()["findings"]
    assert not any(f["id"] == fid for f in finds2)


# --- G4a: the Refine route + c2-wrong auto-refine ---------------------------

def _refinable_card(client: TestClient) -> str:
    """A finalized http_json card (post-shell) the refine paths accept."""
    iid = _create_via_tool(client)
    store = client.app.state.ni
    spec = dict(store.get_item(iid)["spec"])
    spec["goal"] = "track the weather"
    spec["source"] = {"type": "http_json",
                       "url": "https://api.example.com/weather.json"}
    spec["pipeline"] = [{"op": "extract", "paths": {"temperature": "current.temp"}}]
    store.update_spec(iid, spec, origin="user")
    return iid


def test_refine_route_rebuilds_from_note(client: TestClient, monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    _unlock(client)
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, iid, **kw: fired.update(id=iid, **kw) or True)
    iid = _refinable_card(client)
    r = client.post(f"/api/ni/items/{iid}/refine",
                    json={"note": "should be in Fahrenheit degrees."})
    assert r.status_code == 200 and r.json()["kind"] == "rebuild", r.text
    record = ni_flow._flow_read(client.app.state.ni, iid)
    assert record["_refine_note"] == "should be in Fahrenheit degrees."
    assert fired["id"] == iid


def test_refine_route_cadence_and_guidance(client: TestClient, monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    iid = _refinable_card(client)
    r = client.post(f"/api/ni/items/{iid}/refine",
                    json={"note": "update every 45 minutes"})
    assert r.status_code == 200 and r.json()["kind"] == "cadence"
    assert client.app.state.ni.get_item(iid)["spec"]["interval_minutes"] == 45
    # A model-source card refuses with guidance, not a 500.
    iid2 = _create_via_tool(client, title="Haiku card")
    r2 = client.post(f"/api/ni/items/{iid2}/refine",
                     json={"note": "make it different"})
    assert r2.status_code == 409 and "composer" in r2.json()["detail"]


def test_validate_wrong_with_note_drives_the_rebuild(client: TestClient,
                                                      monkeypatch) -> None:
    """The modal's promise, finally true: Something's-wrong + a note on a
    rebuildable card re-enters sampling with the note sealed."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    iid = _refinable_card(client)
    store = client.app.state.ni
    store.commission(iid)
    r = client.post(f"/api/ni/items/{iid}/validate",
                    json={"ok": False, "note": "should be in Fahrenheit degrees."})
    assert r.status_code == 200 and r.json()["refine"] == "rebuild", r.text
    record = ni_flow._flow_read(store, iid)
    assert record["_refine_note"] == "should be in Fahrenheit degrees."


# --- Claims audit 2026-09-21: the mocked-mask class, unmasked ---------------

def test_retry_reseeds_the_record_so_the_worker_can_actually_run(
        client: TestClient, monkeypatch) -> None:
    """THE audit finding: retry cleared the flow slot then spawned — run_flow
    crashed 'no flow record' on every tap, forever, and the mocked worker in
    the old test was the mask. Now: the route RE-SEEDS the record, and this
    test runs the REAL run_flow continuation synchronously to prove the
    worker path completes instead of crashing."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    iid = client.post("/api/ni/intake", json={"request": "retry me for real"}).json()["id"]
    store = client.app.state.ni
    ni_flow._fail(store, iid, "worker", "crash: ValueError")
    r = client.post(f"/api/ni/items/{iid}/flow/retry")
    assert r.status_code == 200, r.text
    record = ni_flow._flow_read(store, iid)
    assert record is not None, "retry must RE-SEED the record, never clear it"
    assert record["state"] == "intent"
    assert record["request"] == "retry me for real", "sealed request preserved"
    # The REAL worker body now runs against the reseeded record — the old
    # code path raised ValueError('no flow record') right here.
    import json as _json
    intent_reply = _json.dumps({
        "kind": "external_data", "subject": "retry", "cadence_minutes": 15,
        "wants": ["value"], "threshold": None, "display_hint": "value"})
    result = ni_flow.run_flow(store, iid,
                               gateway_call=lambda m, p: intent_reply,
                               fetcher=lambda url: {}, catalog=[])
    assert result["state"] == "source", (
        "the retried flow must proceed (here: to the pick pause), not crash")


def test_retry_never_promotes_an_unapproved_confirm_url(
        client: TestClient, monkeypatch) -> None:
    """Audit consent guard: a record that died at confirm_source carries a
    recipe URL the user NEVER approved — retry must drop it."""
    from smartbrain_3000 import ni_catalog, ni_flow
    _unlock(client)
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, i, **kw: fired.update(id=i, **kw) or True)
    iid = client.post("/api/ni/intake", json={"request": "unapproved url guard"}).json()["id"]
    store = client.app.state.ni
    ni_flow._pause_for_recipe_confirm(store, iid, {},
                                       ni_catalog.get_recipe("crypto-price-btc-usd"))
    record = ni_flow._flow_read(store, iid)
    record["state"] = "failed"
    record["error"] = "stale: flow record stranded"
    ni_flow._flow_write(store, iid, record)
    r = client.post(f"/api/ni/items/{iid}/flow/retry")
    assert r.status_code == 200, r.text
    assert fired.get("source_url") is None, (
        "a confirm-pause URL was never consented — retry must not fetch it")


def test_sweep_never_kills_user_gated_pauses() -> None:
    """Audit: the 1h sweep executed live consent pauses ('creation stalled'
    on a card that was just waiting for the user). Every user-gated pause is
    exempt now.

    STANDALONE store on purpose: the TestClient app runs a live scheduler
    thread whose tick races this test's flow-record writes on the shared
    DuckDB connection (TransactionException: Conflict on update — flaked in
    the shipped-image suite). No app, no thread, no race.
    """
    from datetime import UTC, datetime, timedelta

    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni as nimod
    from smartbrain_3000 import ni_flow
    from smartbrain_3000.secrets import gen_master_key
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = nimod.NIStore(conn, gen_master_key())
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
    for state, marker in (("source", ni_flow.AWAITING_SOURCE_PICK),
                           ("confirm_source", "awaiting_confirm")):
        iid = ni_flow.create_shell_item(store, f"pause guard {state}")
        record = ni_flow._flow_read(store, iid)
        record["state"] = state
        record["error"] = marker
        record["updated_at"] = old
        ni_flow._flow_write(store, iid, record)
    swept = ni_flow.sweep_stranded_flows(store)
    assert swept == 0, "user-gated pauses must survive the sweep"


def test_pick_routes_refuse_the_inflight_locating_window(
        client: TestClient, monkeypatch) -> None:
    """Audit: a bare state=source (mid-rank progress window) rendered the
    full pick card and accepted taps that corrupted the live flow. The pick
    routes now require the real pause marker; the board withholds
    suggestions in the window."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    iid = client.post("/api/ni/intake", json={"request": "mid rank window"}).json()["id"]
    store = client.app.state.ni
    record = ni_flow._flow_read(store, iid)
    record["state"] = "source"  # NO awaiting_pick marker: the in-flight window
    ni_flow._flow_write(store, iid, record)
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source",
                    json={"url": "https://api.example.com/x.json"})
    assert r.status_code == 409 and "asking for a source" in r.json()["detail"]
    r2 = client.post(f"/api/ni/items/{iid}/flow/pick-recipe",
                     json={"recipe_id": "crypto-price-btc-usd"})
    assert r2.status_code == 409
    row = next(x for x in client.get("/api/ni/board").json()["items"]
               if x["id"] == iid)
    assert "suggestions" not in row["flow"], (
        "the locating window must not dress up as the pick card")


def test_refine_refuses_while_a_flow_is_running(client: TestClient,
                                                 monkeypatch) -> None:
    """Audit: refine during an active build silently lost the note."""
    from smartbrain_3000 import ni_flow
    _unlock(client)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)
    iid = _refinable_card(client)
    store = client.app.state.ni
    ni_flow._flow_write(store, iid,
                         ni_flow._make_record("busy build", "sampling"))
    r = client.post(f"/api/ni/items/{iid}/refine",
                    json={"note": "should be in Fahrenheit degrees."})
    assert r.status_code == 409 and "busy building" in r.json()["detail"]
