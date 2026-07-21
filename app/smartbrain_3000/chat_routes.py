"""Chat HTTP API: routes completions through the Bifrost gateway.

Requires the app to be unlocked. A request may name an explicit ``model``
("provider/model") or a ``capability`` that maps to one. The provider keys that
Bifrost needs are managed separately (provisioned on unlock).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import gateway, usage

router = APIRouter()


class ChatRequest(BaseModel):
    messages: list[dict] = Field(min_length=1)
    model: str | None = None
    capability: str = "chat"


def _require_unlocked(request: Request) -> None:
    """Raise 423 unless the app has been unlocked (secret store loaded)."""
    if getattr(request.app.state, "secret_store", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")


def _base_system_prompt() -> str:
    """Always-on grounding: the current time, when to reach for tools, and the rule that
    actions must go through a tool. Keeps models from inventing facts/URLs, claiming they
    can't tell the time, or — the trust-critical one — telling the user an action is done
    when no tool actually performed it."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"The current date and time is {now}. Work out other time zones yourself when "
        "asked (e.g. London is UTC in winter, UTC+1 in summer) — never say you cannot "
        "tell the time. For current external facts (news, weather, prices, or a specific "
        "web page) call the web_search or web_fetch tools instead of guessing, and never "
        "invent URLs or facts. To change anything — add, move, or complete a task, remember "
        "a fact, add to knowledge, or send an email — you MUST emit the matching tool call and "
        "wait for its result. Describing the change in words does NOT perform it. NEVER say "
        "something was added, moved, completed, sent, or saved unless a tool call returned "
        "success in THIS turn — no 'I've updated it' or 'done' without that tool result. If "
        "you're missing a detail, ask; if you cannot call the tool, say so plainly."
    )


def _with_memory(request: Request, messages: list[dict]) -> list[dict]:
    """Prepend a system message: always-on grounding (current time + tool use) plus
    the user's profile/facts when present.

    Skipped only when the caller already supplied a system message — so memory context
    grounds the chat without the client ever handling it.
    """
    if any(m.get("role") == "system" for m in messages):
        return messages
    parts = [_base_system_prompt()]
    memory = getattr(request.app.state, "memory", None)
    profile = memory.system_prompt() if memory is not None else None
    if profile:
        parts.append(profile)
    return [{"role": "system", "content": "\n\n".join(parts)}, *messages]


@router.post("/api/chat")
def chat_endpoint(request: Request, body: ChatRequest) -> dict:
    """Resolve a model (explicit or by capability) and complete via Bifrost."""
    _require_unlocked(request)
    assert body.messages, "messages must be present"
    request.app.state.last_interactive = time.monotonic()  # background model work stands aside
    routes = gateway.load_routes(request.app.state.dbx)
    model = body.model or gateway.resolve_model(body.capability, routes)
    if not model:
        raise HTTPException(
            status_code=400, detail=f"no model mapped for capability '{body.capability}'"
        )
    try:
        result = gateway.chat(_with_memory(request, body.messages), model)
    except gateway.GatewayError as exc:  # provider/gateway reported an error
        raise HTTPException(status_code=502, detail=exc.message) from None
    except Exception as exc:  # gateway unreachable
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
    usage.record_response(request.app.state.dbx, model, result)  # best-effort cost telemetry
    result.pop("extra_fields", None)  # drop Bifrost envelope (provider headers, etc.)
    return result
