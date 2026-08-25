"""One aggregate status surface: everything the user should be able to SEE the state of,
in one call — the Settings → Status page's data source (field request: "the download is
not clear to the user"; nothing about this system should be invisible again).

Cheap by construction: no live network probes here (the settings Voice/Local-models
cards own those). Reachability shown here is what the app already knows.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from . import __version__, devices, gateway, moonshine, voice

router = APIRouter()


# /api/status/overview, NOT /api/status: main.py already serves /api/status (DB
# connectivity + install identity) and the adversarial review caught this router
# silently shadowing it.
@router.get("/api/status/overview")
def app_status(request: Request) -> dict:
    """Aggregate app status. Works LOCKED too (a locked app still has a version, an
    update state, and a voice-model download worth seeing) — encrypted-store sections
    simply report locked=true until unlock."""
    state = request.app.state
    unlocked = getattr(state, "secret_store", None) is not None
    out: dict = {
        "version": __version__,
        "unlocked": unlocked,
        "voice_local": moonshine.status(),  # phase/pct/error — needs no key
    }
    if not unlocked:
        return out
    store = state.secret_store
    out["voice"] = {
        "engine": voice._current_engine(store),
        "server_configured": bool(voice.server_config(store)["url"]),
        "stt_model": voice.stt_model(store),
        "tts_model": store.get(voice.TTS_MODEL_KEY) or "",
    }
    out["local_models"] = {
        "ollama_configured": bool(store.get(gateway.OLLAMA_URL_KEY)),
        "mlx_configured": bool(store.get(gateway.MLX_URL_KEY)),
        "mlxe_configured": bool(store.get(gateway.MLXE_URL_KEY)),
    }
    conn = state.dbx
    out["knowledge"] = _knowledge_status(state, conn)
    out["schedules"] = _schedule_status(conn)
    out["feeds"] = _feed_status(state)
    out["devices"] = _device_status(store)
    return out


def _knowledge_status(state, conn) -> dict:
    try:
        docs = int(conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0])
        chunks = int(conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0])
        return {"documents": docs, "embedded_chunks": chunks}
    except Exception:
        return {"documents": 0, "embedded_chunks": 0}


def _schedule_status(conn) -> dict:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE enabled), COUNT(*) FROM schedules;").fetchone()
        return {"enabled": int(row[0] or 0), "total": int(row[1] or 0)}
    except Exception:
        return {"enabled": 0, "total": 0}


def _feed_status(state) -> dict:
    feeds = getattr(state, "feeds", None)
    if feeds is None:
        return {"count": 0}
    try:
        rows = feeds.list_feeds()
        errors = sum(1 for f in rows if str(f.get("last_status", "")).startswith("error"))
        return {"count": len(rows), "errors": errors}
    except Exception:
        return {"count": 0}


def _device_status(store) -> dict:
    # Paired devices live in the encrypted secret store (devices.py), not a table —
    # the review caught the phantom "devices" table reading as an eternal 0.
    try:
        return {"paired": len(devices.list_devices(store))}
    except Exception:
        return {"paired": 0}
