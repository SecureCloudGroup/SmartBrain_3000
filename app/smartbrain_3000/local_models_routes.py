"""Local model (Ollama / MLX) management API.

Configure on-device model servers that run on the host; the app stores their
URL (and MLX's key) in the encrypted secret store and registers them in Bifrost.
All endpoints require the app to be unlocked.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import gateway

router = APIRouter()
log = logging.getLogger(__name__)

_DETECT_TIMEOUT = 1.5  # short probe of the default port when nothing is configured yet


class OllamaConfig(BaseModel):
    url: str = Field(min_length=1)


class MLXConfig(BaseModel):
    url: str = Field(min_length=1)
    api_key: str = ""  # optional: many local MLX/OMLX servers don't verify a key


def _store(request: Request):
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


@router.get("/api/local-models")
def local_status(request: Request) -> dict:
    """Report configured/reachable state + available models for each local provider."""
    store = _store(request)
    ollama_url = store.get(gateway.OLLAMA_URL_KEY)
    mlx_url = store.get(gateway.MLX_URL_KEY)
    mlx_key = store.get(gateway.MLX_KEY_KEY)
    # url is exposed (not secret — it's host.docker.internal:<port>) so the UI can show
    # the configured port; the MLX api_key is never returned. When a provider is NOT yet
    # configured we probe its default host port so the UI can offer a one-tap "we found
    # Ollama running — connect it" (the all-local first-run path), reported as `detected`.
    ollama = {"configured": bool(ollama_url), "reachable": False, "models": [],
              "url": ollama_url or "", "detected": False, "default_url": gateway.OLLAMA_DEFAULT_URL}
    if ollama_url:
        ollama.update(gateway.probe_ollama(ollama_url))
    else:
        probe = gateway.probe_ollama(gateway.OLLAMA_DEFAULT_URL, timeout=_DETECT_TIMEOUT)
        assert "reachable" in probe, "probe must report reachability"
        ollama["detected"], ollama["models"] = probe["reachable"], probe["models"]
    mlx = {"configured": bool(mlx_url), "reachable": False, "models": [],
           "url": mlx_url or "", "detected": False, "default_url": gateway.MLX_DEFAULT_URL}
    if mlx_url:
        mlx.update(gateway.probe_mlx(mlx_url, mlx_key or ""))
    else:
        probe = gateway.probe_mlx(gateway.MLX_DEFAULT_URL, "", timeout=_DETECT_TIMEOUT)
        assert "reachable" in probe, "probe must report reachability"
        mlx["detected"], mlx["models"] = probe["reachable"], probe["models"]
    return {"ollama": ollama, "mlx": mlx}


@router.put("/api/local-models/ollama")
def put_ollama(request: Request, body: OllamaConfig) -> dict[str, bool]:
    """Save Ollama's URL and register it in Bifrost (live).

    ``gateway_synced`` tells the UI whether the gateway registration actually
    succeeded — saving the URL but failing to register it must NOT be reported as
    plain success (mirrors the cloud-provider path in account.put_secret).
    """
    _store(request).put(gateway.OLLAMA_URL_KEY, body.url)
    synced = True
    try:
        gateway.register_ollama(body.url)
    except Exception as exc:  # saved, but the gateway is unreachable — surface it
        log.warning("ollama register skipped: %s", exc)
        synced = False
    return {"ok": True, "gateway_synced": synced}


@router.put("/api/local-models/mlx")
def put_mlx(request: Request, body: MLXConfig) -> dict[str, bool]:
    """Save MLX's URL + key and register it in Bifrost (live). See put_ollama for gateway_synced."""
    store = _store(request)
    store.put(gateway.MLX_URL_KEY, body.url)
    store.put(gateway.MLX_KEY_KEY, body.api_key)
    synced = True
    try:
        gateway.register_mlx(body.url, body.api_key)
    except Exception as exc:  # saved, but the gateway is unreachable — surface it
        log.warning("mlx register skipped: %s", exc)
        synced = False
    return {"ok": True, "gateway_synced": synced}


@router.delete("/api/local-models/{name}")
def delete_local(request: Request, name: str) -> dict[str, bool]:
    """Remove a local provider's config and deprovision it from Bifrost."""
    store = _store(request)
    if name == "ollama":
        store.delete(gateway.OLLAMA_URL_KEY)
    elif name == "mlx":
        store.delete(gateway.MLX_URL_KEY)
        store.delete(gateway.MLX_KEY_KEY)
    else:
        raise HTTPException(status_code=404, detail="unknown local provider")
    synced = True
    try:
        gateway.remove_provider(name)
    except Exception as exc:  # removed locally, but the gateway is unreachable — surface it
        log.warning("local deprovision skipped: %s", exc)
        synced = False
    return {"ok": True, "gateway_synced": synced}
