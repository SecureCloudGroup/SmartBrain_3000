"""Tests for the per-device registry (devices.py + /api/devices) — Phase 2."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import devices
from smartbrain_3000.secrets import SecretStore, gen_master_key


def _store() -> SecretStore:
    return SecretStore(duckdb.connect(":memory:"), gen_master_key())


def test_create_and_verify() -> None:
    store = _store()
    rec = devices.create_device(store, "My Phone")
    assert rec["label"] == "My Phone" and rec["device_id"] and rec["credential"]
    assert "created_at" in rec
    assert devices.verify_device(store, rec["device_id"], rec["credential"]) is True
    assert devices.verify_device(store, rec["device_id"], "wrong") is False
    assert devices.verify_device(store, "no-such-device", rec["credential"]) is False


def test_device_exists_tracks_revocation() -> None:
    store = _store()
    rec = devices.create_device(store, "phone")
    assert devices.device_exists(store, rec["device_id"]) is True
    assert devices.device_exists(store, "no-such-id") is False
    devices.revoke_device(store, rec["device_id"])
    assert devices.device_exists(store, rec["device_id"]) is False


def test_list_excludes_credentials() -> None:
    store = _store()
    devices.create_device(store, "Phone A")
    devices.create_device(store, "Phone B")
    listed = devices.list_devices(store)
    assert len(listed) == 2
    assert all("credential" not in d for d in listed)
    assert {d["label"] for d in listed} == {"Phone A", "Phone B"}


def test_list_ignores_other_secrets() -> None:
    store = _store()
    store.put("provider:openai:api_key", "sk-not-a-device")
    store.put("mcp:access_token", "tok")
    devices.create_device(store, "Phone")
    listed = devices.list_devices(store)
    assert len(listed) == 1 and listed[0]["label"] == "Phone"


def test_revoke_removes_device() -> None:
    store = _store()
    rec = devices.create_device(store, "Phone")
    devices.revoke_device(store, rec["device_id"])
    assert devices.verify_device(store, rec["device_id"], rec["credential"]) is False
    assert devices.list_devices(store) == []
    devices.revoke_device(store, rec["device_id"])  # idempotent — no error on re-revoke


def test_label_is_bounded() -> None:
    store = _store()
    rec = devices.create_device(store, "x" * 200)
    assert len(rec["label"]) == 64


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_devices_endpoints_require_unlock(client: TestClient) -> None:
    assert client.get("/api/devices").status_code == 423
    assert client.post("/api/devices", json={"label": "Phone"}).status_code == 423
    assert client.delete("/api/devices/abc").status_code == 423


def test_create_list_revoke_flow(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    created = client.post("/api/devices", json={"label": "My Phone"}).json()
    assert created["device_id"] and created["credential"] and created["label"] == "My Phone"
    assert created["desktop_pubkey"]  # pinned by the phone to verify the Desktop over the channel

    listed = client.get("/api/devices").json()["devices"]
    assert len(listed) == 1
    assert listed[0]["device_id"] == created["device_id"]
    assert "credential" not in listed[0]  # never leak the secret on read

    assert client.delete(f"/api/devices/{created['device_id']}").status_code == 200
    assert client.get("/api/devices").json()["devices"] == []
