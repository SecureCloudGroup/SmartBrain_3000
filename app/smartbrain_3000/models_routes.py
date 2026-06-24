"""Model discovery + capability routing API.

``/api/models`` returns the gateway's live catalog (dynamic — no hardcoded
model lists). Capability routing (which model serves chat / reasoning /
embeddings) is persisted in the ``meta`` table and read back by the chat, agent,
and scheduler paths. All endpoints require the app to be unlocked.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import gateway, usage

router = APIRouter()

_LOCAL_PROVIDERS = ("ollama", "mlx")

# Capability -> human label for the routing UI. Keys mirror gateway.DEFAULT_ROUTES.
CAPABILITY_LABELS = {
    "chat": "Chat",
    "fast_chat": "Fast chat",
    "reasoning": "Reasoning",
    "embedding": "Embedding (semantic search)",
}


class RoutesBody(BaseModel):
    routes: dict[str, str]


def _require_unlocked(request: Request) -> None:
    """Raise 423 unless the app has been unlocked (secret store loaded)."""
    if getattr(request.app.state, "secret_store", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")


@router.get("/api/models")
def list_models_endpoint(request: Request) -> dict:
    """Discovered model catalog from the gateway (live; reflects configured providers)."""
    _require_unlocked(request)
    try:
        models = gateway.list_models()
    except gateway.GatewayError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from None
    except Exception as exc:  # gateway unreachable
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
    return {"models": models}


@router.get("/api/routes")
def get_routes(request: Request) -> dict:
    """Current capability->model routing (persisted, merged over defaults) + labels."""
    _require_unlocked(request)
    routes = gateway.load_routes(request.app.state.dbx)
    routes.setdefault("embedding", gateway.embed_model(request.app.state.dbx))  # show the effective model
    return {"routes": routes, "labels": CAPABILITY_LABELS}


@router.put("/api/routes")
def put_routes(request: Request, body: RoutesBody) -> dict:
    """Persist routing for known capabilities only; ignore unknown keys."""
    _require_unlocked(request)
    clean = {
        cap: model
        for cap, model in body.routes.items()
        if cap in CAPABILITY_LABELS and isinstance(model, str) and model
    }
    gateway.save_routes(request.app.state.dbx, clean)
    return {"ok": True, "routes": gateway.load_routes(request.app.state.dbx)}


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
