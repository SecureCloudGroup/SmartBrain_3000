"""Tests for local-model (Ollama / MLX) registration + management (C3)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import gateway
from smartbrain_3000.secrets import SecretStore, gen_master_key


def _mock_client(record: list) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        record.append((req.method, req.url.path, body))
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_register_ollama_payload() -> None:
    record: list = []
    with _mock_client(record) as client:
        gateway.register_ollama("http://host.docker.internal:11434", client=client)
    assert ("DELETE", "/api/providers/ollama") in [(m, p) for (m, p, b) in record]
    create = next(b for (m, p, b) in record if m == "POST" and p == "/api/providers")
    assert create == {
        "provider": "ollama",
        "network_config": {"base_url": "http://host.docker.internal:11434"},
    }
    key = next(b for (m, p, b) in record if p == "/api/providers/ollama/keys")
    assert key["name"] == "smartbrain-ollama"
    assert key["ollama_key_config"] == {"url": "http://host.docker.internal:11434"}


def test_register_mlx_payload() -> None:
    record: list = []
    with _mock_client(record) as client:
        gateway.register_mlx("http://host.docker.internal:8888", "1234", client=client)
    create = next(b for (m, p, b) in record if m == "POST" and p == "/api/providers")
    assert create["provider"] == "mlx"
    assert create["network_config"]["base_url"] == "http://host.docker.internal:8888"
    assert create["custom_provider_config"]["base_provider_type"] == "openai"
    # list_models must be enabled, or Bifrost never enumerates MLX into /v1/models
    # and the models go missing from the chat/routing dropdowns.
    assert create["custom_provider_config"]["allowed_requests"]["list_models"] is True
    key = next(b for (m, p, b) in record if p == "/api/providers/mlx/keys")
    assert key["name"] == "smartbrain-mlx"
    assert key["value"] == "1234"


def test_register_mlx_without_key_uses_placeholder() -> None:
    # OMLX/local MLX servers may not verify a key — registration must still work.
    record: list = []
    with _mock_client(record) as client:
        gateway.register_mlx("http://host.docker.internal:8888", "", client=client)
    key = next(b for (m, p, b) in record if p == "/api/providers/mlx/keys")
    assert key["value"] == "none"  # Bifrost needs a non-empty value; MLX ignores it


def _store(entries: dict[str, str]) -> SecretStore:
    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    for k, v in entries.items():
        store.put(k, v)
    return store


def test_provision_local_from_store() -> None:
    store = _store(
        {
            gateway.OLLAMA_URL_KEY: "http://host.docker.internal:11434",
            gateway.MLX_URL_KEY: "http://host.docker.internal:8888",
            gateway.MLX_KEY_KEY: "1234",
        }
    )
    record: list = []
    with _mock_client(record) as client:
        done = gateway.provision_local_from_store(store, client=client)
    assert sorted(done) == ["mlx", "ollama"]
    key_posts = {p for (m, p, b) in record if m == "POST" and p.endswith("/keys")}
    assert key_posts == {"/api/providers/ollama/keys", "/api/providers/mlx/keys"}


def test_provision_local_registers_keyless_mlx() -> None:
    # MLX with a URL but no key must still be re-registered on boot/unlock — gating
    # on the key would drop keyless MLX/OMLX servers after every restart.
    store = _store({gateway.MLX_URL_KEY: "http://host.docker.internal:8888"})
    record: list = []
    with _mock_client(record) as client:
        done = gateway.provision_local_from_store(store, client=client)
    assert done == ["mlx"]
    key = next(b for (m, p, b) in record if p == "/api/providers/mlx/keys")
    assert key["value"] == "none"  # placeholder for keyless MLX


def test_provision_local_skips_unconfigured() -> None:
    store = _store({})
    record: list = []
    with _mock_client(record) as client:
        done = gateway.provision_local_from_store(store, client=client)
    assert done == []
    assert record == []


def test_probe_ollama_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "phi4-mini:latest"}, {"name": "qwen3:8b"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = gateway.probe_ollama("http://host.docker.internal:11434", client=client)
    assert out == {"reachable": True, "models": ["phi4-mini:latest", "qwen3:8b"]}


def test_probe_ollama_unreachable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert gateway.probe_ollama("http://x", client=client) == {"reachable": False, "models": []}


def test_probe_mlx_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("authorization") == "Bearer 1234"
        return httpx.Response(200, json={"data": [{"id": "Qwen2.5-7B-Instruct-4bit"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = gateway.probe_mlx("http://host.docker.internal:8888", "1234", client=client)
    assert out == {"reachable": True, "models": ["Qwen2.5-7B-Instruct-4bit"]}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_local_models_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/local-models").status_code == 423
    assert client.put("/api/local-models/ollama", json={"url": "http://x"}).status_code == 423


def test_put_ollama_stores_and_registers(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    seen: list = []
    monkeypatch.setattr(gateway, "register_ollama", lambda url: seen.append(url))
    r = client.put("/api/local-models/ollama", json={"url": "http://host.docker.internal:11434"})
    assert r.status_code == 200
    assert seen == ["http://host.docker.internal:11434"]


def test_put_mlx_stores_and_registers(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    seen: list = []
    monkeypatch.setattr(gateway, "register_mlx", lambda url, key: seen.append((url, key)))
    r = client.put(
        "/api/local-models/mlx",
        json={"url": "http://host.docker.internal:8888", "api_key": "1234"},
    )
    assert r.status_code == 200
    assert seen == [("http://host.docker.internal:8888", "1234")]


def test_local_status_reports_configured_and_models(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "register_ollama", lambda url: None)
    client.put("/api/local-models/ollama", json={"url": "http://host.docker.internal:11434"})
    monkeypatch.setattr(
        gateway, "probe_ollama", lambda url, **k: {"reachable": True, "models": ["phi4-mini:latest"]}
    )
    monkeypatch.setattr(gateway, "probe_mlx", lambda url, key, **k: {"reachable": False, "models": []})
    body = client.get("/api/local-models").json()
    assert body["ollama"] == {
        "configured": True,
        "reachable": True,
        "models": ["phi4-mini:latest"],
        "url": "http://host.docker.internal:11434",  # exposed so the UI can show the port
        "detected": False,  # only meaningful when unconfigured
        "default_url": gateway.OLLAMA_DEFAULT_URL,
    }
    assert body["mlx"]["configured"] is False


def test_local_status_detects_unconfigured_default(client: TestClient, monkeypatch) -> None:
    # Nothing configured, but a server answers on the default host port -> the UI offers a
    # one-tap connect (the all-local first-run path). `detected` reflects that probe.
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(
        gateway, "probe_ollama", lambda url, **k: {"reachable": True, "models": ["qwen3:8b"]}
    )
    monkeypatch.setattr(gateway, "probe_mlx", lambda url, key, **k: {"reachable": False, "models": []})
    body = client.get("/api/local-models").json()
    assert body["ollama"]["configured"] is False
    assert body["ollama"]["detected"] is True
    assert body["ollama"]["models"] == ["qwen3:8b"]
    assert body["mlx"]["detected"] is False


def test_delete_local_removes_config_and_provider(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "register_ollama", lambda url: None)
    removed: list = []
    monkeypatch.setattr(gateway, "remove_provider", lambda name: removed.append(name))
    client.put("/api/local-models/ollama", json={"url": "http://x"})
    assert client.delete("/api/local-models/ollama").status_code == 200
    assert removed == ["ollama"]
    assert client.get("/api/local-models").json()["ollama"]["configured"] is False
