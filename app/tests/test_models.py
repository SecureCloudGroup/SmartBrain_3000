"""Tests for model discovery (/api/models) and capability routing (/api/routes)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db, gateway

# A representative Bifrost /v1/models payload: a chat model with pricing, an
# embedding model (Gemini-style method hint), a local chat model (no hints, no
# pricing), a local embedding model (name hint), plus entries we must skip.
_MODELS = {
    "data": [
        {
            "id": "gemini/gemini-2.5-flash",
            "name": "Gemini 2.5 Flash",
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.0000003", "completion": "0.0000025"},
            "supported_methods": ["generateContent", "countTokens"],
        },
        {"id": "gemini/text-embedding-004", "name": "Embedding", "supported_methods": ["embedContent"]},
        # Image model that DOES expose generateContent — id hint must still exclude it.
        {"id": "gemini/gemini-2.5-flash-image", "name": "Flash Image", "supported_methods": ["generateContent"]},
        {"id": "ollama/llama3.2", "name": "Llama 3.2"},  # no hints, no pricing -> chat + free
        {"id": "ollama/nomic-embed-text", "name": "Nomic Embed"},  # name hint -> not chat
        {"id": "mlx/bge-m3-mlx-fp16", "name": "BGE-M3"},  # embedder whose id lacks "embed"
        {"id": "no-slash-id", "name": "skip me"},  # un-prefixed -> skipped
        "not-a-dict",  # malformed -> skipped
    ]
}


def _mock(json_body: dict, status: int = 200) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/models", "list_models must hit /v1/models"
        return httpx.Response(status, json=json_body)

    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_list_models_parses_filters_and_prices() -> None:
    out = gateway.list_models(client=_mock(_MODELS))
    by_id = {m["id"]: m for m in out}
    assert "no-slash-id" not in by_id and len(out) == 6  # un-prefixed + non-dict skipped
    flash = by_id["gemini/gemini-2.5-flash"]
    assert flash["provider"] == "gemini" and flash["chat"] is True
    assert flash["pricing"] == {"prompt": float("0.0000003"), "completion": float("0.0000025")}
    assert by_id["gemini/text-embedding-004"]["chat"] is False  # embedContent isn't a chat method
    assert by_id["gemini/text-embedding-004"]["embed"] is True  # ...but it IS an embedding model
    assert flash["embed"] is False  # a chat model is not an embedding model
    assert by_id["ollama/nomic-embed-text"]["embed"] is True  # detected by name
    assert by_id["gemini/gemini-2.5-flash-image"]["chat"] is False  # id hint beats generateContent
    assert by_id["ollama/llama3.2"]["chat"] is True
    assert by_id["ollama/llama3.2"]["pricing"] is None  # local -> free
    assert by_id["ollama/nomic-embed-text"]["chat"] is False  # name hint
    # BGE (and other embedder families) have no "embed" in the id — must still classify as
    # an embedder, not chat, or it's hidden from the Embedding route picker (the bge-m3 bug).
    assert by_id["mlx/bge-m3-mlx-fp16"]["embed"] is True
    assert by_id["mlx/bge-m3-mlx-fp16"]["chat"] is False


def test_list_models_raises_on_error_status() -> None:
    with pytest.raises(gateway.GatewayError):
        gateway.list_models(client=_mock({"error": {"message": "down"}}, status=502))


def test_list_models_nonjson_error_body_carries_status() -> None:
    # A 401 with a non-JSON body must surface the real status (not a parse error).
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"<html>unauthorized</html>", headers={"content-type": "text/html"})

    client = httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))
    with pytest.raises(gateway.GatewayError) as info:
        gateway.list_models(client=client)
    assert info.value.status_code == 401


def test_routes_roundtrip_and_merge(tmp_path) -> None:
    conn = db.open_db(tmp_path / "r.duckdb")
    db.run_migrations(conn)  # creates the meta table
    assert gateway.load_routes(conn) == gateway.DEFAULT_ROUTES  # defaults when unset
    gateway.save_routes(conn, {"chat": "gemini/gemini-2.5-flash"})
    merged = gateway.load_routes(conn)
    assert merged["chat"] == "gemini/gemini-2.5-flash"
    assert merged["reasoning"] == gateway.DEFAULT_ROUTES["reasoning"]  # untouched cap keeps default


def test_load_routes_corrupt_json_falls_back(tmp_path) -> None:
    conn = db.open_db(tmp_path / "r2.duckdb")
    db.run_migrations(conn)  # creates the meta table
    db.meta_set(conn, "model_routes", "{not valid json")
    assert gateway.load_routes(conn) == gateway.DEFAULT_ROUTES


def test_embed_model_uses_routed_embedding(tmp_path) -> None:
    conn = db.open_db(tmp_path / "e.duckdb")
    db.run_migrations(conn)
    assert gateway.embed_model(conn) == gateway.DEFAULT_EMBED_MODEL  # no route -> default
    gateway.save_routes(conn, {"embedding": "gemini/text-embedding-004"})
    assert gateway.embed_model(conn) == "gemini/text-embedding-004"  # routed embedding wins


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_models_endpoint_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/models").status_code == 423
    assert client.get("/api/routes").status_code == 423


def test_models_endpoint_returns_catalog(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "list_models", lambda: [{"id": "gemini/x", "chat": True}])
    r = client.get("/api/models")
    assert r.status_code == 200 and r.json()["models"][0]["id"] == "gemini/x"


def test_routes_put_persists_known_caps_only(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.put("/api/routes", json={"routes": {"chat": "gemini/gemini-2.5-flash", "bogus": "x"}})
    assert r.status_code == 200
    assert r.json()["routes"]["chat"] == "gemini/gemini-2.5-flash"
    assert "bogus" not in r.json()["routes"]  # unknown capability rejected
    g = client.get("/api/routes")
    assert g.json()["routes"]["chat"] == "gemini/gemini-2.5-flash" and "chat" in g.json()["labels"]


def test_routes_endpoint_exposes_and_persists_embedding(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    g = client.get("/api/routes").json()
    assert "embedding" in g["labels"] and "embedding" in g["routes"]  # effective model shown
    r = client.put("/api/routes", json={"routes": {"embedding": "gemini/text-embedding-004"}})
    assert r.status_code == 200 and r.json()["routes"]["embedding"] == "gemini/text-embedding-004"


# --- B21: sensible default chat model when only local providers exist ----

def test_default_chat_for_picks_local_when_no_cloud() -> None:
    # No-cloud-key install: catalog only has local chat models — default to one.
    catalog = [
        {"id": "ollama/llama3.2", "provider": "ollama", "chat": True},
        {"id": "ollama/nomic-embed-text", "provider": "ollama", "chat": False, "embed": True},
        {"id": "mlx/qwen2", "provider": "mlx", "chat": True},
    ]
    assert gateway.default_chat_for(catalog) == "ollama/llama3.2"


def test_default_chat_for_returns_none_when_cloud_available() -> None:
    # A cloud chat model is configured: keep the hardcoded defaults (callers do nothing).
    catalog = [
        {"id": "openai/gpt-4o-mini", "provider": "openai", "chat": True},
        {"id": "ollama/llama3.2", "provider": "ollama", "chat": True},
    ]
    assert gateway.default_chat_for(catalog) is None


def test_default_chat_for_empty_catalog_returns_none() -> None:
    # Guard against a stale/missing default model id: empty catalog -> nothing to pick.
    assert gateway.default_chat_for([]) is None
    # Catalog with only embedding/local models also yields None for chat selection.
    assert gateway.default_chat_for([{"id": "ollama/x", "provider": "ollama", "chat": False}]) is None
