"""Memory + identity HTTP API (requires unlock).

Facts the assistant should remember, plus a profile (assistant name / user name
/ custom instructions). Both are encrypted at rest; the chat route composes them
into a system message server-side.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .memory import MemoryStore

router = APIRouter()


class MemoryIn(BaseModel):
    text: str = Field(min_length=1)


class ProfileIn(BaseModel):
    assistant_name: str = Field(default="", max_length=120)
    user_name: str = Field(default="", max_length=120)
    instructions: str = Field(default="", max_length=4000)


def _mem(request: Request) -> MemoryStore:
    """Return the unlocked MemoryStore, or raise 423 if locked."""
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return memory


@router.get("/api/memories")
def list_memories(request: Request) -> dict:
    """List remembered facts."""
    return {"memories": _mem(request).list_memories()}


@router.post("/api/memories")
def add_memory(request: Request, body: MemoryIn) -> dict[str, str]:
    """Remember a new fact; return its id."""
    return {"id": _mem(request).add_memory(body.text.strip())}


@router.delete("/api/memories/{mid}")
def delete_memory(request: Request, mid: str) -> dict[str, bool]:
    """Forget a fact."""
    _mem(request).delete_memory(mid)
    return {"ok": True}


@router.get("/api/profile")
def get_profile(request: Request) -> dict:
    """Return the identity profile (assistant/user names + instructions)."""
    return _mem(request).get_profile()


@router.put("/api/profile")
def set_profile(request: Request, body: ProfileIn) -> dict[str, bool]:
    """Update the identity profile."""
    _mem(request).set_profile(
        body.assistant_name.strip(), body.user_name.strip(), body.instructions.strip()
    )
    return {"ok": True}
