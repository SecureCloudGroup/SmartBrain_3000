"""Tests for the chat path through the Bifrost gateway (C2a)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import gateway

_CANNED = {
    "id": "cmpl-x",
    "choices": [{"message": {"role": "assistant", "content": "hello there"}}],
}


def test_resolve_model_known() -> None:
    assert gateway.resolve_model("fast_chat") == "openai/gpt-4o-mini"


def test_resolve_model_unknown_is_none() -> None:
    assert gateway.resolve_model("does-not-exist") is None


def test_gateway_chat_forms_request_and_parses() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_CANNED)

    mock = httpx.Client(
        base_url="http://bifrost:8080", transport=httpx.MockTransport(handler)
    )
    out = gateway.chat([{"role": "user", "content": "hi"}], "openai/gpt-4o-mini", client=mock)
    assert out == _CANNED
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"] == {
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_chat_requires_unlock(client: TestClient) -> None:
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 423


def test_chat_when_unlocked_returns_completion(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat", lambda messages, model: _CANNED)
    r = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 200
    assert r.json() == _CANNED


def test_chat_unknown_capability_400(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "bogus"},
    )
    assert r.status_code == 400


def test_gateway_chat_raises_on_error_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})

    mock = httpx.Client(
        base_url="http://bifrost:8080", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(gateway.GatewayError) as info:
        gateway.chat([{"role": "user", "content": "hi"}], "openai/gpt-4o-mini", client=mock)
    assert info.value.status_code == 401
    assert info.value.message == "Incorrect API key"


def test_chat_strips_extra_fields(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(
        gateway,
        "chat",
        lambda messages, model: {
            "id": "x",
            "choices": [],
            "extra_fields": {"provider_response_headers": {"Openai-Organization": "org-x"}},
        },
    )
    r = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "x", "choices": []}  # envelope dropped


def test_chat_provider_error_surfaces_message(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def boom(messages, model):
        raise gateway.GatewayError(401, "Incorrect API key provided")

    monkeypatch.setattr(gateway, "chat", boom)
    r = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "Incorrect API key provided"


# --- chat_stream (SSE) --------------------------------------------------

def _sse(*chunks: dict) -> bytes:
    """Encode a sequence of chunk dicts as an OpenAI SSE body + [DONE] sentinel."""
    out = []
    for c in chunks:
        out.append(f"data: {json.dumps(c)}\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def _stream_mock(body: bytes, status: int = 200) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions", "stream must hit chat completions"
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})

    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_chat_stream_yields_text_deltas() -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    with _stream_mock(body) as mock:
        items = list(gateway.chat_stream([{"role": "user", "content": "hi"}], "m", client=mock))
    deltas = [i["delta"] for i in items if i["delta"]]
    assert "".join(deltas) == "Hello"
    assert items[-1]["finish_reason"] == "stop"
    assert all(i["tool_calls"] is None for i in items)  # plain text — no tool turn


def test_chat_stream_flags_tool_calls() -> None:
    tc = [{"id": "c1", "type": "function", "function": {"name": "kb_search", "arguments": "{}"}}]
    body = _sse({"choices": [{"delta": {"tool_calls": tc}, "finish_reason": None}]})
    with _stream_mock(body) as mock:
        items = list(gateway.chat_stream([{"role": "user", "content": "hi"}], "m", client=mock))
    assert items and items[0]["tool_calls"] == tc  # caller can detect a tool turn


def test_chat_stream_raises_on_error_status() -> None:
    with _stream_mock(b'{"error": {"message": "down"}}', status=502) as mock:
        with pytest.raises(gateway.GatewayError) as info:
            list(gateway.chat_stream([{"role": "user", "content": "hi"}], "m", client=mock))
    assert info.value.status_code == 502 and "down" in info.value.message


def test_chat_stream_skips_blank_and_malformed_lines() -> None:
    # Comments, blanks, non-data lines, and bad JSON are all skipped (bounded parsing).
    body = (
        b": ping\n\n"
        b"event: extra\n\n"
        b"data: not-json\n\n"
        b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    with _stream_mock(body) as mock:
        items = list(gateway.chat_stream([{"role": "user", "content": "hi"}], "m", client=mock))
    assert [i["delta"] for i in items] == ["ok"]
