"""Integration tests: the REAL gateway code against a REAL local Bifrost stand-in.

No gateway function is monkeypatched here — real httpx, real SSE parsing, real
payload construction over a real socket. These would have caught the streaming
"no tools field" regression (test_stream_offers_tools asserts the actual bytes we
send). Contrast with the unit tests that lambda-replace gateway.* and so never
exercise the wire path.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from smartbrain_3000 import gateway

from _fakegateway import FakeGateway


@pytest.fixture()
def fake(monkeypatch) -> Iterator[FakeGateway]:
    server = FakeGateway().start()
    monkeypatch.setenv("SMARTBRAIN_LLM_GATEWAY_URL", server.url)
    gateway.set_pool(None)  # force the per-call client so it reads the env URL
    try:
        yield server
    finally:
        server.stop()


def test_chat_roundtrips_real_http(fake: FakeGateway) -> None:
    fake.reply_text = "hello from the model"
    out = gateway.chat([{"role": "user", "content": "hi"}], "gemini/gemini-2.5-flash")
    assert out["choices"][0]["message"]["content"] == "hello from the model"
    sent = fake.last("/v1/chat/completions")
    assert sent["model"] == "gemini/gemini-2.5-flash"
    assert "tools" not in sent  # plain chat must NOT offer tools


def test_chat_stream_offers_tools_on_the_wire(fake: FakeGateway) -> None:
    # THE regression guard for the production bug: the streamed request MUST carry the
    # tools field, or the model can't call a tool and narrates un-performed actions.
    spec = [{"type": "function", "function": {"name": "add_task", "parameters": {}}}]
    chunks = list(gateway.chat_stream([{"role": "user", "content": "add a task"}], "m", tools_spec=spec))
    assert "".join(c["delta"] for c in chunks) == fake.reply_text
    sent = fake.last("/v1/chat/completions")
    assert sent["stream"] is True
    assert sent["tools"] == spec and sent["tool_choice"] == "auto"


def test_chat_stream_surfaces_tool_calls(fake: FakeGateway) -> None:
    fake.reply_tool_calls = [{"index": 0, "id": "c1", "type": "function",
                              "function": {"name": "add_task", "arguments": "{}"}}]
    chunks = list(gateway.chat_stream([{"role": "user", "content": "x"}], "m", tools_spec=[{"type": "function", "function": {"name": "add_task"}}]))
    assert any(c["tool_calls"] for c in chunks)


def test_chat_with_tools_sends_tools(fake: FakeGateway) -> None:
    spec = [{"type": "function", "function": {"name": "kb_search", "parameters": {}}}]
    gateway.chat_with_tools([{"role": "user", "content": "x"}], "m", spec)
    sent = fake.last("/v1/chat/completions")
    assert sent["tools"] == spec and sent["tool_choice"] == "auto"


def test_embed_roundtrips_real_http(fake: FakeGateway) -> None:
    fake.embedding = [0.5, 0.25, 0.125, 0.0625]
    vec = gateway.embed("some text", "ollama/nomic-embed-text:v1.5")
    assert vec == [0.5, 0.25, 0.125, 0.0625]
    assert fake.last("/v1/embeddings")["model"] == "ollama/nomic-embed-text:v1.5"


def test_list_models_roundtrips_real_http(fake: FakeGateway) -> None:
    fake.models = [{"id": "openai/gpt-4o", "object": "model"}, {"id": "gemini/gemini-2.5-flash", "object": "model"}]
    ids = [m["id"] for m in gateway.list_models()]
    assert "openai/gpt-4o" in ids and "gemini/gemini-2.5-flash" in ids


def test_chat_stream_tools_unsupported_flag(fake: FakeGateway) -> None:
    # A 4xx that looks like tools-rejection must set tools_unsupported so callers retry.
    fake.fail_status = 400
    fake.fail_body = {"error": {"message": "model does not support tools"}}
    spec = [{"type": "function", "function": {"name": "add_task"}}]
    with pytest.raises(gateway.GatewayError) as ei:
        list(gateway.chat_stream([{"role": "user", "content": "x"}], "m", tools_spec=spec))
    assert getattr(ei.value, "tools_unsupported", False) is True
