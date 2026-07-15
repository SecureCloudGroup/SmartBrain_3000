"""Chat history HTTP API (requires unlock).

Conversations + messages are encrypted at rest; these endpoints create / list /
read / rename / delete them. Chat completion stays in ``/api/chat`` — the SPA
orchestrates: persist the user turn, call the gateway, persist the reply.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .history import ChatHistory

router = APIRouter()


class NewConversation(BaseModel):
    title: str | None = None


class RenameIn(BaseModel):
    title: str = Field(min_length=1)


class MessageIn(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = Field(min_length=1)
    # Citations from the agent turn's tool results; validated/bounded in ChatHistory.
    sources: list[dict] | None = None


def _hist(request: Request) -> ChatHistory:
    """Return the unlocked ChatHistory, or raise 423 if locked."""
    history = getattr(request.app.state, "history", None)
    if history is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return history


@router.get("/api/conversations")
def list_conversations(
    request: Request,
    before: str | None = Query(default=None, description="keyset cursor from a prior page"),
    limit: int | None = Query(default=None, ge=1, description="page size (clamped to a hard max)"),
) -> dict:
    """List one (newest-first) page of conversations; cursor + has_more page older."""
    assert request is not None, "request required"
    try:
        page = _hist(request).list_conversations_page(before=before, limit=limit)
    except ValueError:  # malformed/legacy pagination cursor -> 400, not a bare 500
        raise HTTPException(status_code=400, detail="invalid pagination cursor") from None
    assert isinstance(page, dict), "paginated result must be a dict"
    return {
        "conversations": page["items"],
        "next_cursor": page["next_cursor"],
        "has_more": page["has_more"],
    }


@router.post("/api/conversations")
def create_conversation(request: Request, body: NewConversation) -> dict[str, str]:
    """Create a conversation; return its id."""
    title = (body.title or "").strip() or "New conversation"
    return {"id": _hist(request).create_conversation(title)}


@router.get("/api/conversations/{cid}")
def get_conversation(
    request: Request,
    cid: str,
    before: str | None = Query(default=None, description="keyset cursor from a prior page"),
    limit: int | None = Query(default=None, ge=1, description="page size (clamped to a hard max)"),
) -> dict:
    """Return a conversation with one (newest-first) page of its messages."""
    assert cid, "conversation id required"
    history = _hist(request)
    convo = history.get_conversation(cid)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    try:
        page = history.get_messages_page(cid, before=before, limit=limit)
    except ValueError:  # malformed/legacy pagination cursor -> 400, not a bare 500
        raise HTTPException(status_code=400, detail="invalid pagination cursor") from None
    assert isinstance(page, dict), "paginated result must be a dict"
    return {
        **convo,
        "messages": page["items"],
        "next_cursor": page["next_cursor"],
        "has_more": page["has_more"],
    }


@router.patch("/api/conversations/{cid}")
def rename_conversation(request: Request, cid: str, body: RenameIn) -> dict[str, bool]:
    """Rename a conversation."""
    history = _hist(request)
    if history.get_conversation(cid) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    history.rename_conversation(cid, body.title.strip())
    return {"ok": True}


@router.delete("/api/conversations/{cid}")
def delete_conversation(request: Request, cid: str) -> dict[str, bool]:
    """Delete a conversation and its messages."""
    _hist(request).delete_conversation(cid)
    return {"ok": True}


@router.post("/api/conversations/{cid}/messages")
def add_message(request: Request, cid: str, body: MessageIn) -> dict[str, str]:
    """Append a message to a conversation."""
    history = _hist(request)
    try:
        return {"id": history.add_message(cid, body.role, body.content, sources=body.sources)}
    except ValueError:
        raise HTTPException(status_code=404, detail="conversation not found") from None
