"""Tests for the account + secrets HTTP API (B1-B3 wired into the app)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# B8: header the real Desktop UI sends; the WebRTC bridge filters it out.
_LOCAL = {"X-SB-Local": "1"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_status_fresh(client: TestClient) -> None:
    r = client.get("/api/account/status")
    assert r.status_code == 200
    assert r.json() == {"initialized": False, "unlocked": False, "has_recovery": False}


def test_setup_returns_kit_and_unlocks(client: TestClient) -> None:
    r = client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert r.status_code == 200
    body = r.json()
    assert body["recovery_key"]
    assert "Emergency Kit" in body["emergency_kit"]
    assert client.get("/api/account/status").json() == {
        "initialized": True, "unlocked": True, "has_recovery": True
    }


def test_setup_rejects_short_passphrase(client: TestClient) -> None:
    r = client.post("/api/account/setup", json={"passphrase": "short"})
    assert r.status_code == 422  # pydantic min_length


def test_double_setup_conflicts(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.post("/api/account/setup", json={"passphrase": "another-one"})
    assert r.status_code == 409


def test_secret_requires_unlock(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/account/lock")
    r = client.put("/api/secrets/provider:openai:api_key", json={"value": "sk-1"})
    assert r.status_code == 423


def test_secret_roundtrip_when_unlocked(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.put(
        "/api/secrets/provider:openai:api_key", json={"value": "sk-1"}
    ).status_code == 200
    assert "provider:openai:api_key" in client.get("/api/secrets").json()["keys"]


def test_unlock_with_passphrase(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/account/lock")
    r = client.post("/api/account/unlock", json={"passphrase": "correct-horse"})
    assert r.status_code == 200 and r.json() == {"unlocked": True}


def test_unlock_wrong_passphrase_401(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/account/lock")
    r = client.post("/api/account/unlock", json={"passphrase": "wrong-pass"})
    assert r.status_code == 401


def test_unlock_with_recovery_key(client: TestClient) -> None:
    setup = client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    recovery_key = setup.json()["recovery_key"]
    client.post("/api/account/lock")
    r = client.post("/api/account/unlock", json={"recovery_key": recovery_key})
    assert r.status_code == 200
    assert client.put("/api/secrets/k", json={"value": "v"}).status_code == 200


def test_secret_values_never_returned(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.put("/api/secrets/provider:openai:api_key", json={"value": "super-secret"})
    # Names only, and filtered to the provider-key namespace (see test below).
    assert client.get("/api/secrets").json() == {"keys": ["provider:openai:api_key"]}


def test_put_provider_secret_reports_gateway_synced_true(client: TestClient, monkeypatch) -> None:
    # B6: a successful Bifrost sync must surface gateway_synced=true to the UI.
    from smartbrain_3000 import gateway

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(gateway, "set_provider", lambda name, key: calls.append((name, key)))
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.put("/api/secrets/provider:openai:api_key", json={"value": "sk-good"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "gateway_synced": True}
    assert calls and calls[0][1] == "sk-good"


def test_put_provider_secret_reports_gateway_synced_false_on_failure(
    client: TestClient, monkeypatch
) -> None:
    # B6: when the Bifrost sync raises, the key is still stored (so a transient
    # gateway hiccup doesn't lose it) but gateway_synced is false so the UI
    # doesn't claim the provider is configured when it isn't.
    from smartbrain_3000 import gateway

    def _explode(name: str, value: str) -> None:
        raise RuntimeError("bifrost down")

    monkeypatch.setattr(gateway, "set_provider", _explode)
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.put("/api/secrets/provider:openai:api_key", json={"value": "sk-1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "gateway_synced": False}
    # Secret was still stored — visible in the provider-key listing.
    assert "provider:openai:api_key" in client.get("/api/secrets").json()["keys"]


def test_put_non_provider_secret_reports_gateway_synced_false(client: TestClient) -> None:
    # B6: storing a non-provider secret (e.g. an MCP access token) does not
    # touch Bifrost, so gateway_synced is false — no sync was attempted.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.put("/api/secrets/mcp:access_token", json={"value": "tok-1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "gateway_synced": False}


def test_list_secrets_filters_to_provider_keys(client: TestClient) -> None:
    # /api/secrets must NOT enumerate paired devices, email state, or the MCP
    # token to an unlocked session — only the Providers-page namespace.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.put("/api/secrets/provider:openai:api_key", json={"value": "sk-1"})
    client.put("/api/secrets/device:abc", json={"value": "device-record"})
    client.put("/api/secrets/email/gmail/refresh_token", json={"value": "rt-1"})
    client.put("/api/secrets/mcp:access_token", json={"value": "tok-1"})
    keys = client.get("/api/secrets").json()["keys"]
    assert "provider:openai:api_key" in keys
    assert not any(k.startswith("device:") for k in keys)
    assert not any("refresh_token" in k for k in keys)
    assert not any(k.startswith("mcp:") for k in keys)
