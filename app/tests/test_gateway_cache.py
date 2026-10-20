"""Prompt-cache block handling in the gateway (anthropic/* routes only)."""

from __future__ import annotations

import json

import httpx

from smartbrain_3000 import gateway

_OK = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
_TOOL = {"type": "function", "function": {"name": "t", "parameters": {"z": 1, "a": {"y": 2, "b": 3}}}}


def _client(bodies: list[bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=_OK)
    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def _block_msgs() -> list[dict]:
    return [
        {"role": "system", "content": [
            {"type": "text", "text": "static rules. ", "cache": True},
            {"type": "text", "text": "more."},
        ]},
        {"role": "user", "content": "hello"},
    ]


def test_anthropic_gets_cache_control_on_marked_block() -> None:
    bodies: list[bytes] = []
    gateway.chat(_block_msgs(), "anthropic/claude-sonnet-4-5", client=_client(bodies))
    sent = json.loads(bodies[0])["messages"]
    blocks = sent[0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]
    assert all("cache" not in b for b in blocks)  # our marker never reaches the wire
    assert sent[1] == {"role": "user", "content": "hello"}


def test_non_anthropic_payload_is_byte_identical_to_plain_strings() -> None:
    plain = [{"role": "system", "content": "static rules. more."}, {"role": "user", "content": "hello"}]
    a: list[bytes] = []
    b: list[bytes] = []
    gateway.chat(_block_msgs(), "openai/gpt-4o-mini", client=_client(a))
    gateway.chat(plain, "openai/gpt-4o-mini", client=_client(b))
    assert a == b  # byte-for-byte
    assert b"cache_control" not in a[0]
    c: list[bytes] = []
    gateway.chat_with_tools(plain, "ollama/x", [_TOOL], client=_client(c))
    assert b"cache_control" not in c[0] and json.loads(c[0])["tools"] == [_TOOL]


def test_string_conversation_untouched_on_every_route() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    assert gateway._wire_messages(msgs, "anthropic/x")[0] is msgs[0]
    assert gateway._wire_messages(msgs, "openai/x")[0] is msgs[0]


def test_at_most_four_breakpoints_keeps_the_latest() -> None:
    msgs = [{"role": "user", "content": [{"type": "text", "text": str(i), "cache": True}]} for i in range(6)]
    wire = gateway._wire_messages(msgs, "anthropic/x")
    flags = ["cache_control" in m["content"][0] for m in wire]
    assert flags == [False, False, True, True, True, True]


def test_tool_result_breakpoint_and_sorted_tools_on_anthropic() -> None:
    bodies: list[bytes] = []
    msgs = [{"role": "user", "content": "q"},
            {"role": "tool", "tool_call_id": "1", "content": [{"type": "text", "text": "r", "cache": True}]}]
    gateway.chat_with_tools(msgs, "anthropic/claude-haiku-4-5", [_TOOL], client=_client(bodies))
    body = json.loads(bodies[0])
    assert body["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    params = body["tools"][0]["function"]["parameters"]
    assert list(params) == ["a", "z"] and list(params["a"]) == ["b", "y"]  # keys sorted
    again: list[bytes] = []
    gateway.chat_with_tools(msgs, "anthropic/claude-haiku-4-5", [json.loads(json.dumps(_TOOL))], client=_client(again))
    assert again == bodies  # deterministic prefix


def test_max_tokens_passthrough_and_default_unchanged() -> None:
    msgs = [{"role": "user", "content": "q"}]
    a: list[bytes] = []
    b: list[bytes] = []
    gateway.chat(msgs, "openai/x", client=_client(a))
    gateway.chat(msgs, "openai/x", client=_client(b), max_tokens=64)
    assert "max_tokens" not in json.loads(a[0])  # default payload unchanged
    assert json.loads(b[0])["max_tokens"] == 64
    c: list[bytes] = []
    gateway.chat_with_tools(msgs, "openai/x", [_TOOL], client=_client(c), max_tokens=32)
    assert json.loads(c[0])["max_tokens"] == 32
    d: list[bytes] = []
    gateway.chat_with_tools(msgs, "openai/x", [_TOOL], client=_client(d))
    assert "max_tokens" not in json.loads(d[0])


def _flaky(script: list, calls: list[int], sleeps: list[float], monkeypatch) -> httpx.Client:
    """Client whose Nth request replays script[N] (an int status, a Response, or an exception)."""
    monkeypatch.setattr(gateway, "_sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        item = script[min(len(calls), len(script) - 1)]
        calls.append(1)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(item, json=_OK if item == 200 else {"error": {"message": "boom"}})
    return httpx.Client(base_url="http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_retry_5xx_then_success_with_backoff(monkeypatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    client = _flaky([503, 500, 200], calls, sleeps, monkeypatch)
    assert gateway.completion_text(gateway.chat([{"role": "user", "content": "q"}], "openai/x", client=client)) == "hi"
    assert len(calls) == 3 and sleeps == [0.5, 1.0]


def test_retry_honors_retry_after_and_stops_at_two_retries(monkeypatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    limited = httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"message": "slow down"}})
    client = _flaky([limited], calls, sleeps, monkeypatch)
    try:
        gateway.chat([{"role": "user", "content": "q"}], "openai/x", client=client)
        raise AssertionError("expected GatewayError")
    except gateway.GatewayError as exc:
        assert exc.status_code == 429 and exc.message == "slow down"
    assert len(calls) == 3 and sleeps == [3.0, 3.0]  # 1 try + 2 retries, Retry-After honored


def test_4xx_is_never_retried(monkeypatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    client = _flaky([400], calls, sleeps, monkeypatch)
    try:
        gateway.chat_with_tools([{"role": "user", "content": "q"}], "openai/x", [_TOOL], client=client)
        raise AssertionError("expected GatewayError")
    except gateway.GatewayError as exc:
        assert exc.status_code == 400
    assert len(calls) == 1 and sleeps == []


def test_connection_error_retried_but_timeout_is_not(monkeypatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    client = _flaky([httpx.ConnectError("down"), 200], calls, sleeps, monkeypatch)
    assert gateway.chat([{"role": "user", "content": "q"}], "openai/x", client=client)["choices"]
    assert len(calls) == 2 and len(sleeps) == 1
    calls.clear()
    sleeps.clear()
    client = _flaky([httpx.ReadTimeout("slow")], calls, sleeps, monkeypatch)
    try:
        gateway.chat([{"role": "user", "content": "q"}], "openai/x", client=client)
        raise AssertionError("expected GatewayError")
    except gateway.GatewayError as exc:
        assert exc.status_code == 504
    assert len(calls) == 1 and sleeps == []


def test_local_semaphore_released_after_retries(monkeypatch) -> None:
    calls: list[int] = []
    client = _flaky([502, 200], calls, [], monkeypatch)
    gateway.chat([{"role": "user", "content": "q"}], "ollama/x", client=client)
    assert len(calls) == 2
    assert gateway.local_available()  # serializer free again (no leaked acquire)


def test_non_text_blocks_survive_flatten_without_marker() -> None:
    img = {"type": "image_url", "image_url": {"url": "data:x"}, "cache": True}
    out = gateway._wire_messages([{"role": "user", "content": [img]}], "openai/x")
    assert out[0]["content"] == [{"type": "image_url", "image_url": {"url": "data:x"}}]
