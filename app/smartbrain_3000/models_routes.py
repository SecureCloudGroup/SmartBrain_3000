"""Model discovery + capability routing API.

``/api/models`` returns the gateway's live catalog (dynamic — no hardcoded
model lists). Capability routing (which model serves chat / reasoning /
embeddings) is persisted in the ``meta`` table and read back by the chat, agent,
and scheduler paths. All endpoints require the app to be unlocked.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import gateway, usage

log = logging.getLogger(__name__)

router = APIRouter()

_LOCAL_PROVIDERS = ("ollama", "mlx")

# Capability -> human label for the routing UI. Keys mirror gateway.DEFAULT_ROUTES,
# plus "agent" — a background/scheduled-turn override with NO built-in default (so it
# stays absent from DEFAULT_ROUTES); when unset the agent path falls back to "chat".
CAPABILITY_LABELS = {
    "chat": "Chat",
    "fast_chat": "Fast chat",
    "reasoning": "Reasoning",
    "agent": "Agent tasks (schedules)",
    "embedding": "Embedding (semantic search)",
}


class RoutesBody(BaseModel):
    routes: dict[str, str]


class ContextLengthsBody(BaseModel):
    lengths: dict[str, int]  # 'provider/model' -> context-length tokens (<=0 resets to the default)


def _require_unlocked(request: Request) -> None:
    """Raise 423 unless the app has been unlocked (secret store loaded)."""
    if getattr(request.app.state, "secret_store", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")


@router.get("/api/models")
def list_models_endpoint(request: Request) -> dict:
    """Discovered model catalog from the gateway (live; reflects configured providers).

    When the gateway catalog fails (one hanging provider URL wedges Bifrost's aggregate
    /v1/models), fall back to probing the configured LOCAL servers directly and answer
    ``degraded: true`` — one dead provider must never blank the whole model list.
    """
    _require_unlocked(request)
    try:
        return {"models": gateway.list_models(), "degraded": False}
    except Exception as exc:
        detail = exc.message if isinstance(exc, gateway.GatewayError) else f"gateway unreachable: {exc}"
        models = gateway.local_fallback_models(request.app.state.secret_store)
        if not models:
            raise HTTPException(status_code=502, detail=detail) from None
        log.warning("model catalog degraded to direct local probes: %s", detail)
        return {"models": models, "degraded": True}


@router.get("/api/routes")
def get_routes(request: Request) -> dict:
    """Current capability->model routing (persisted, merged over defaults) + labels."""
    _require_unlocked(request)
    routes = gateway.load_routes(request.app.state.dbx)
    routes.setdefault("embedding", gateway.embed_model(request.app.state.dbx))  # show the effective model
    return {"routes": routes, "labels": CAPABILITY_LABELS}


@router.put("/api/routes")
def put_routes(request: Request, body: RoutesBody) -> dict:
    """Persist routing for known capabilities only; ignore unknown keys.

    Each model must be a 'provider/model' id (the shape every gateway model uses). Without
    this, a typo like 'gpt4' would persist silently and make every later /api/chat resolve
    to it and 502 — with no feedback at save time. The '/' check is gateway-independent, so
    saving still works while the gateway is briefly unreachable.
    """
    _require_unlocked(request)
    clean = {
        cap: model
        for cap, model in body.routes.items()
        if cap in CAPABILITY_LABELS and isinstance(model, str) and model
    }
    for cap, model in clean.items():
        if "/" not in model:
            raise HTTPException(status_code=400, detail=f"model for '{cap}' must be a 'provider/model' id, got {model!r}")
    gateway.save_routes(request.app.state.dbx, clean)
    return {"ok": True, "routes": gateway.load_routes(request.app.state.dbx)}


@router.get("/api/model-context-lengths")
def get_context_lengths(request: Request) -> dict:
    """Per-model context length overrides (tokens) + the fallback default used when unset.

    The dynamic tool-result cap sizes to a model's context length; MLX registration auto-detects it,
    and this lets a user set/correct it for any model (e.g. Ollama, which isn't auto-detected)."""
    _require_unlocked(request)
    return {"lengths": gateway.load_context_lengths(request.app.state.dbx), "default": gateway._DEFAULT_CONTEXT_TOKENS}


@router.put("/api/model-context-lengths")
def put_context_lengths(request: Request, body: ContextLengthsBody) -> dict:
    """Merge per-model context-length overrides into the store (a <=0 value removes an override).

    Merges (rather than replaces) so editing one model never wipes another's auto-detected length.
    Keys must be 'provider/model' ids — the shape every gateway model uses."""
    _require_unlocked(request)
    conn = request.app.state.dbx
    merged = dict(gateway.load_context_lengths(conn))
    for model, tokens in body.lengths.items():
        if "/" not in model:
            raise HTTPException(status_code=400, detail=f"context-length key must be a 'provider/model' id, got {model!r}")
        if tokens <= 0:
            merged.pop(model, None)  # reset this model to the default
        else:
            merged[model] = tokens
    gateway.save_context_lengths(conn, merged)
    return {"ok": True, "lengths": gateway.load_context_lengths(conn)}


def _pricing_map() -> dict[str, dict]:
    """Map model id -> {prompt, completion} per-token price from the live catalog."""
    try:
        return {m["id"]: m["pricing"] for m in gateway.list_models() if m.get("pricing")}
    except Exception:  # gateway unreachable — fall back to token-only (no cost)
        return {}


def _clean_dt(value: str | None) -> str | None:
    """Validate a 'YYYY-MM-DD HH:MM:SS' UTC datetime; None if absent/invalid."""
    assert value is None or isinstance(value, str), "datetime bound must be a string or None"
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # exactly the documented shape
    except ValueError:
        return None  # ignore a malformed bound rather than 500 the view
    return value


@router.get("/api/usage")
def get_usage(request: Request, since: str | None = None, until: str | None = None) -> dict:
    """Per-model token usage + computed cost in a time window (cloud priced live; local = $0)."""
    _require_unlocked(request)
    pricing = _pricing_map()
    rows = usage.summary(request.app.state.dbx, _clean_dt(since), _clean_dt(until))
    out, total = [], 0.0
    for r in rows:
        price = pricing.get(r["model"])
        cost = 0.0 if not price else (
            r["prompt_tokens"] * price["prompt"] + r["completion_tokens"] * price["completion"]
        )
        total += cost
        local = r["model"].split("/", 1)[0] in _LOCAL_PROVIDERS
        out.append({**r, "cost": cost, "local": local})
    assert total >= 0.0, "total cost must be non-negative"
    return {"usage": out, "total_cost": total}
