"""Tests for materializing provider keys into Bifrost (C2b)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import gateway
from smartbrain_3000.secrets import SecretStore, gen_master_key


def _store_with(entries: dict[str, str]) -> SecretStore:
    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    for key, value in entries.items():
        store.put(key, value)
    return store


def _mock_client(record: list) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        record.append((req.method, req.url.path, body))
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_provision_only_present_keys() -> None:
    store = _store_with(
        {"provider:openai:api_key": "sk-oai", "provider:google:api_key": "g-key"}
    )
    record: list = []
    with _mock_client(record) as client:
        provisioned = gateway.provision_from_store(store, client=client)
    assert sorted(provisioned) == ["gemini", "openai"]  # google -> gemini; anthropic absent
    key_posts = {
        (p, b["value"]) for (m, p, b) in record if m == "POST" and p.endswith("/keys")
    }
    assert ("/api/providers/openai/keys", "sk-oai") in key_posts
    assert ("/api/providers/gemini/keys", "g-key") in key_posts
    assert all("anthropic" not in p for (m, p, b) in record)  # no key -> not touched


def test_set_provider_payload() -> None:
    record: list = []
    with _mock_client(record) as client:
        gateway.set_provider("anthropic", "sk-ant", client=client)
    methods_paths = [(m, p) for (m, p, b) in record]
    assert ("DELETE", "/api/providers/anthropic") in methods_paths  # clean replace
    assert ("POST", "/api/providers") in methods_paths  # recreate provider
    key_posts = [b for (m, p, b) in record if m == "POST" and p == "/api/providers/anthropic/keys"]
    assert len(key_posts) == 1  # key attached via sub-resource
    assert key_posts[0]["value"] == "sk-ant"
    assert key_posts[0]["models"] == ["*"]
    assert key_posts[0]["name"] == "smartbrain-anthropic"  # unique per provider


def test_deprovision_deletes_all_managed() -> None:
    record: list = []
    with _mock_client(record) as client:
        gateway.deprovision(client=client)
    deleted = {p for (m, p, b) in record if m == "DELETE"}
    assert deleted == {
        "/api/providers/openai",
        "/api/providers/anthropic",
        "/api/providers/gemini",
    }


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_unlock_triggers_provision_lock_triggers_deprovision(client: TestClient, monkeypatch) -> None:
    calls = {"provision": 0, "deprovision": 0}
    monkeypatch.setattr(
        gateway, "provision_from_store", lambda store: calls.__setitem__("provision", calls["provision"] + 1)
    )
    monkeypatch.setattr(
        gateway, "deprovision", lambda: calls.__setitem__("deprovision", calls["deprovision"] + 1)
    )
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert calls["provision"] == 1  # setup unlocks -> provisions
    client.post("/api/account/lock")
    assert calls["deprovision"] == 1


def test_put_provider_key_syncs_to_bifrost(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    seen: list = []
    monkeypatch.setattr(gateway, "set_provider", lambda name, key: seen.append((name, key)))
    r = client.put("/api/secrets/provider:openai:api_key", json={"value": "sk-live"})
    assert r.status_code == 200
    assert seen == [("openai", "sk-live")]


def test_put_non_provider_key_no_sync(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    seen: list = []
    monkeypatch.setattr(gateway, "set_provider", lambda name, key: seen.append((name, key)))
    client.put("/api/secrets/some-other-key", json={"value": "v"})
    assert seen == []


def test_delete_provider_key_removes_from_bifrost(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    removed: list = []
    monkeypatch.setattr(gateway, "set_provider", lambda name, key: None)
    monkeypatch.setattr(gateway, "remove_provider", lambda name: removed.append(name))
    client.put("/api/secrets/provider:anthropic:api_key", json={"value": "sk-ant"})
    client.delete("/api/secrets/provider:anthropic:api_key")
    assert removed == ["anthropic"]


def test_set_provider_retries_transient_500(monkeypatch) -> None:
    monkeypatch.setattr(gateway.time, "sleep", lambda _s: None)  # no real backoff
    counter = {"keys": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/keys"):
            counter["keys"] += 1
            if counter["keys"] == 1:  # first attempt: transient store error
                return httpx.Response(500, json={"error": {"message": "store error"}})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))
    gateway.set_provider("openai", "sk-x", client=client)  # must NOT raise
    client.close()
    assert counter["keys"] == 2  # 500 then retried -> 200


def test_set_provider_raises_after_persistent_500(monkeypatch) -> None:
    monkeypatch.setattr(gateway.time, "sleep", lambda _s: None)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/keys"):
            return httpx.Response(500, json={"error": {"message": "store error"}})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))
    with pytest.raises(gateway.GatewayError):
        gateway.set_provider("openai", "sk-x", client=client)
    client.close()
