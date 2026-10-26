"""Route authority classes for a paired phone (2026-10-26).

A phone (relay credential) may do what carries no key and seals no consent: ask for a card
and drive a card it already has. Everything else stays Desktop-local. The frozen set below
makes a new Desktop-local route (or a quiet removal of one) a deliberate edit, reviewed.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000.auth import relay_headers

_PHONE = relay_headers("phone-under-test")

# Every route that refuses a phone. Keys (credentials), consent seals, trust, backups,
# pairing, tokens and updates. Adding to or shrinking this set needs T's decision.
DESKTOP_ONLY = frozenset({
    "DELETE /api/devices/pair-code",
    "DELETE /api/devices/{device_id}",
    "DELETE /api/feeds/{feed_id}",
    "DELETE /api/mcp/token",
    "DELETE /api/ni/library",
    "DELETE /api/ni/mcp-servers/{server_id}",
    "GET /api/mcp/token",
    "GET /api/ni/items/{item_id}/export-template",
    "GET /api/ni/notices",
    "POST /api/account/passphrase/reset",
    "POST /api/backup",
    "POST /api/devices",
    "POST /api/devices/pair-code",
    "POST /api/export",
    "POST /api/feeds",
    "POST /api/local-models/claudecode/update",
    "POST /api/mcp/token",
    "POST /api/ni/items/{item_id}/flow/access",
    "POST /api/ni/items/{item_id}/flow/answer",
    "POST /api/ni/items/{item_id}/flow/pick-source",
    "POST /api/ni/items/{item_id}/flow/use-key",
    "POST /api/ni/items/{item_id}/switch-feed",
    "POST /api/ni/library/connect",
    "POST /api/ni/library/trust-key",
    "POST /api/ni/mcp-servers",
    "POST /api/restore",
    "POST /api/update/install",
    "POST /api/vaults/{vault_id}/key",
    "POST /api/vaults/{vault_id}/trust-publisher",
    "POST /api/vaults/{vault_id}/verify-hosted",
    "PUT /api/ni/items/{item_id}/credential",
    "PUT /api/ni/mcp-servers/{server_id}",
})

PHONE_OK_NI = [
    ("POST", "/api/ni/items/nope/flow/retry"),
    ("POST", "/api/ni/items/nope/flow/cancel"),
    ("POST", "/api/ni/items/nope/flow/fix"),
    ("POST", "/api/ni/items/nope/flow/reopen"),
    ("POST", "/api/ni/items/nope/flow/stop-looking"),
    ("POST", "/api/ni/items/nope/refine"),
    ("PUT", "/api/ni/items/nope/param"),
    ("POST", "/api/ni/items/nope/brief-summary"),
    ("POST", "/api/ni/findings/nope/resolve"),
    ("GET", "/api/ni/findings"),
    ("GET", "/api/ni/measure"),
    ("POST", "/api/ni/items/nope/activate"),            # fixed-choice consent taps (T, 10:46Z)
    ("POST", "/api/ni/items/nope/flow/keep-looking"),
    ("POST", "/api/ni/items/nope/repair-policy"),
]
_BODIES = {"repair-policy": {"l1": True}, "refine": {"note": "every hour"}, "param": {"name": "x", "value": "y"},
           "brief-summary": {"enabled": True}}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "phone.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _walk(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)  # FastAPI wraps each include_router
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield route


def test_the_desktop_only_set_is_frozen(client: TestClient) -> None:
    found = set()
    for route in _walk(client.app.routes):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(route, "methods", None):
            continue
        if "_require_desktop_local" in inspect.getsource(endpoint):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found.add(f"{method} {route.path}")
    assert found == DESKTOP_ONLY


@pytest.mark.parametrize(("method", "path"), PHONE_OK_NI)
def test_a_phone_can_drive_its_cards(client: TestClient, method: str, path: str) -> None:
    body = _BODIES.get(path.rsplit("/", 1)[-1])
    r = client.request(method, path, headers=_PHONE, json=body) if body else client.request(method, path, headers=_PHONE)
    assert r.status_code != 403, r.text


def test_a_phone_can_ask_for_a_card_but_not_with_a_link(client: TestClient, monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda store, iid, **kw: fired.update(id=iid, **kw) or True)
    r = client.post("/api/ni/intake", headers=_PHONE, json={"request": "Oregon wildfires today"})
    assert r.status_code == 200, r.text
    assert fired["id"] == r.json()["id"] and fired.get("source_url") is None
    entries = client.get("/api/audit").json()["entries"]
    assert any(e["tool"] == "ni_intake" and "paired phone" in str(e) for e in entries)
    link = client.post("/api/ni/intake", headers=_PHONE,
                       json={"request": "Oregon wildfires", "source_url": "https://example.com/a.json"})
    assert link.status_code == 403
    assert "Mac" in link.json()["detail"]


def test_a_phone_may_only_answer_the_model_consent_question(client: TestClient) -> None:
    typed = client.post("/api/ni/items/nope/flow/answer", headers=_PHONE,
                        json={"kind": "supply_date", "value": "2026-01-01"})
    assert typed.status_code == 403
    tap = client.post("/api/ni/items/nope/flow/answer", headers=_PHONE,
                      json={"kind": "model_consent", "value": "allow"})
    assert tap.status_code != 403
