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


def test_non_text_blocks_survive_flatten_without_marker() -> None:
    img = {"type": "image_url", "image_url": {"url": "data:x"}, "cache": True}
    out = gateway._wire_messages([{"role": "user", "content": [img]}], "openai/x")
    assert out[0]["content"] == [{"type": "image_url", "image_url": {"url": "data:x"}}]
