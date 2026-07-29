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


# --- gateway privacy enforcement (Bifrost request logging must stay off) ----

def _config_client(record: list, client_config: dict) -> httpx.Client:
    """Mock Bifrost /api/config: GET serves ``client_config``, PUT records the body."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/config":
            return httpx.Response(200, json={"client_config": dict(client_config),
                                             "is_logs_connected": True})
        body = json.loads(req.content) if req.content else None
        record.append((req.method, req.url.path, body))
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_privacy_flags_written_and_unrelated_config_preserved() -> None:
    # Bifrost's PUT /api/config overwrites client_config wholesale — the merge must
    # carry every unrelated field through, or enforcing privacy would clobber them.
    current = {"enable_logging": True, "disable_content_logging": False,
               "log_retention_days": 365, "max_request_body_size_mb": 100,
               "drop_excess_requests": False}
    record: list = []
    with _config_client(record, current) as client:
        assert gateway.ensure_gateway_privacy(client) is True
    assert len(record) == 1
    method, path, body = record[0]
    assert (method, path) == ("PUT", "/api/config")
    put_cfg = body["client_config"]
    assert put_cfg["enable_logging"] is False
    assert put_cfg["disable_content_logging"] is True
    assert put_cfg["log_retention_days"] == 1  # the API's minimum; drains history
    assert put_cfg["max_request_body_size_mb"] == 100  # unrelated settings preserved
    assert put_cfg["drop_excess_requests"] is False


def test_privacy_enforcement_is_idempotent() -> None:
    already = {"enable_logging": False, "disable_content_logging": True,
               "log_retention_days": 1, "max_request_body_size_mb": 100}
    record: list = []
    with _config_client(record, already) as client:
        assert gateway.ensure_gateway_privacy(client) is False
    assert record == []  # nothing to write — no PUT at all


@pytest.fixture()
def app_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_unlock_enforces_privacy_and_survives_gateway_outage(app_client, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(gateway, "ensure_gateway_privacy", lambda *a, **k: calls.append(1))
    resp = app_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert resp.status_code == 200
    assert calls  # enforced at unlock
    # And a gateway outage must never block an unlock (best-effort with a loud log).
    def down(*a, **k):
        raise RuntimeError("bifrost unreachable")
    monkeypatch.setattr(gateway, "ensure_gateway_privacy", down)
    app_client.post("/api/account/lock")
    resp = app_client.post("/api/account/unlock", json={"passphrase": "correct-horse"})
    assert resp.status_code == 200  # unlock still works


def test_chat_temperature_is_opt_in() -> None:
    # Every existing caller sends None -> the payload must stay byte-identical (no
    # temperature key at all); the critique's pinned value must pass through.
    bodies: list = []
    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    with httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler)) as client:
        gateway.chat([{"role": "user", "content": "x"}], "mlx/m", client=client)
        gateway.chat([{"role": "user", "content": "x"}], "mlx/m", client=client, temperature=0.2)
    assert "temperature" not in bodies[0]  # default: unchanged payload
    assert bodies[1]["temperature"] == 0.2


# --- refused creates must not destroy a working registration (2026-07-28 outage) ---
# Bifrost v1.6.4 validates the base_url hostname at CREATE time; a refusal landing
# after our own DELETE used to leave the gateway with no provider at all — chat dead,
# and every save retry re-destroying it. _replace_provider now snapshots the existing
# registration and puts it back when the new one is refused.


def test_refused_create_restores_prior_registration() -> None:
    prior = {
        "name": "anthropic",
        "network_config": {"base_url": "http://old-and-working:1"},
        "custom_provider_config": None,  # null fields must be dropped from the restore body
        "provider_status": "active",
    }
    record: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        record.append((req.method, req.url.path, body))
        if req.method == "GET":
            return httpx.Response(200, json=prior)
        if req.method == "POST" and req.url.path == "/api/providers":
            creates = [b for (m, p, b) in record if (m, p) == ("POST", "/api/providers")]
            if len(creates) == 1:  # the intended create: refused (unresolvable host)
                return httpx.Response(400, json={"error": {"message": "no such host"}})
            return httpx.Response(200, json={"ok": True})  # the restore: accepted
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))
    with client, pytest.raises(httpx.HTTPStatusError):  # caller must still SEE the refusal
        gateway.set_provider("anthropic", "sk-new", client=client)
    creates = [b for (m, p, b) in record if (m, p) == ("POST", "/api/providers")]
    assert len(creates) == 2, "refused create must be followed by a restore"
    assert creates[1] == {
        "provider": "anthropic",
        "network_config": {"base_url": "http://old-and-working:1"},
    }
    keys = [(m, p) for (m, p, b) in record if p.endswith("/keys")]
    assert keys == [("POST", "/api/providers/anthropic/keys")]  # key re-attached to the restore


def test_refused_create_without_prior_just_raises() -> None:
    record: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        record.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if req.method == "POST" and req.url.path == "/api/providers":
            return httpx.Response(400, json={"error": {"message": "no such host"}})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))
    with client, pytest.raises(httpx.HTTPStatusError):
        gateway.set_provider("anthropic", "sk-new", client=client)
    assert record.count(("POST", "/api/providers")) == 1, "nothing to restore -> no phantom create"
    assert not any(p.endswith("/keys") for (m, p) in record)
