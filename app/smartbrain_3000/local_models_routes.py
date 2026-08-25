"""Local model (Ollama / MLX) management API.

Configure on-device model servers that run on the host; the app stores their
URL (and MLX's key) in the encrypted secret store and registers them in Bifrost.
All endpoints require the app to be unlocked.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import gateway, voice

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
        ollama.update(gateway.probe_ollama(gateway.localize_local_url(ollama_url)))
    else:
        probe = gateway.probe_ollama(gateway.OLLAMA_DEFAULT_URL, timeout=_DETECT_TIMEOUT)
        assert "reachable" in probe, "probe must report reachability"
        ollama["detected"], ollama["models"] = probe["reachable"], probe["models"]
    mlx = {"configured": bool(mlx_url), "reachable": False, "models": [],
           "url": mlx_url or "", "detected": False, "default_url": gateway.MLX_DEFAULT_URL}
    if mlx_url:
        mlx.update(gateway.probe_mlx(gateway.localize_local_url(mlx_url), mlx_key or ""))
    else:
        probe = gateway.probe_mlx(gateway.MLX_DEFAULT_URL, "", timeout=_DETECT_TIMEOUT)
        assert "reachable" in probe, "probe must report reachability"
        mlx["detected"], mlx["models"] = probe["reachable"], probe["models"]
    mlxe_url = store.get(gateway.MLXE_URL_KEY)
    mlxe_key = store.get(gateway.MLXE_KEY_KEY)
    mlxe = {"configured": bool(mlxe_url), "reachable": False, "models": [],
            "url": mlxe_url or "", "detected": False, "default_url": gateway.MLXE_DEFAULT_URL}
    if mlxe_url:
        mlxe.update(gateway.probe_mlx(gateway.localize_local_url(mlxe_url), mlxe_key or ""))  # same OpenAI /v1/models shape
    else:
        probe = gateway.probe_mlx(gateway.MLXE_DEFAULT_URL, "", timeout=_DETECT_TIMEOUT)
        assert "reachable" in probe, "probe must report reachability"
        mlxe["detected"], mlxe["models"] = probe["reachable"], probe["models"]
    return {"ollama": ollama, "mlx": mlx, "mlxe": mlxe}


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
        # localize at USE: the submitted/stored URL may name the other runtime's
        # host (the SPA historically sent host.docker.internal) — registering it
        # raw natively made Bifrost refuse the create ("no such host") after the
        # old registration was already deleted. The probe path always localized;
        # the register path must too.
        gateway.register_ollama(gateway.localize_local_url(body.url))
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
    voice.reset_server_skip()  # a reconfigured MLX server deserves an immediate voice try
    synced = True
    try:
        gateway.register_mlx(gateway.localize_local_url(body.url), body.api_key)  # localize at USE (see put_ollama)
    except Exception as exc:  # saved, but the gateway is unreachable — surface it
        log.warning("mlx register skipped: %s", exc)
        synced = False
    _detect_mlx_context_lengths(request, gateway.localize_local_url(body.url), body.api_key)
    return {"ok": True, "gateway_synced": synced}


@router.put("/api/local-models/mlxe")
def put_mlxe(request: Request, body: MLXConfig) -> dict[str, bool]:
    """Save the MLX embeddings server's URL + key and register it in Bifrost (live).

    A separate provider from "mlx": chat servers (oMLX) refuse decoder embedding models,
    so the embeddings model runs on its own tiny server (tools/mlx_embed_server) and only
    the embedding capability is registered for it."""
    store = _store(request)
    store.put(gateway.MLXE_URL_KEY, body.url)
    store.put(gateway.MLXE_KEY_KEY, body.api_key)
    synced = True
    try:
        gateway.register_mlxe(gateway.localize_local_url(body.url), body.api_key)  # localize at USE (see put_ollama)
    except Exception as exc:  # saved, but the gateway is unreachable — surface it
        log.warning("mlxe register skipped: %s", exc)
        synced = False
    return {"ok": True, "gateway_synced": synced}


def _detect_mlx_context_lengths(request: Request, url: str, api_key: str) -> None:
    """Persist each MLX model's server-reported max_model_len (bifrost strips it) so the dynamic
    result cap can size to it. Keyed 'mlx/<id>' to match catalog ids. A user override for a model
    already in the store WINS (never silently clobbered); best-effort — a probe failure is ignored."""
    try:
        detected = gateway.probe_mlx(url, api_key).get("context_lengths", {})
        if not detected:
            return
        conn = request.app.state.dbx
        prefixed = {f"mlx/{mid}": tokens for mid, tokens in detected.items()}
        gateway.save_context_lengths(conn, {**prefixed, **gateway.load_context_lengths(conn)})
    except Exception as exc:  # detection is best-effort; the manual override always remains available
        log.warning("mlx context-length detection skipped: %s", exc)


class VoiceConfig(BaseModel):
    """The voice (audio) server + model names. url may be empty = "use the MLX server"."""
    url: str = ""
    api_key: str = ""
    stt_model: str = ""  # empty -> voice.DEFAULT_STT_MODEL
    tts_model: str = ""  # empty -> server TTS off (browser system voices only)
    tts_voice: str = ""


@router.put("/api/local-models/voice")
def put_voice(request: Request, body: VoiceConfig) -> dict:
    """Save the voice server + models. Not registered in Bifrost — audio endpoints are
    called directly (the gateway doesn't proxy /v1/audio/*). Returns the fresh status
    so the page can show reachability without a second round-trip."""
    store = _store(request)
    for key, value in ((gateway.VOICE_URL_KEY, body.url.strip()),
                       (gateway.VOICE_KEY_KEY, body.api_key),
                       (voice.STT_MODEL_KEY, body.stt_model.strip()),
                       (voice.TTS_MODEL_KEY, body.tts_model.strip()),
                       (voice.TTS_VOICE_KEY, body.tts_voice.strip())):
        if value:
            store.put(key, value)
        else:
            store.delete(key)
    voice.reset_server_skip()  # a reconfigured server deserves an immediate try
    return {"ok": True, "status": voice.status(store, probe=True)}


@router.delete("/api/local-models/{name}")
def delete_local(request: Request, name: str) -> dict[str, bool]:
    """Remove a local provider's config and deprovision it from Bifrost."""
    store = _store(request)
    if name == "ollama":
        store.delete(gateway.OLLAMA_URL_KEY)
    elif name == "mlx":
        store.delete(gateway.MLX_URL_KEY)
        store.delete(gateway.MLX_KEY_KEY)
    elif name == "mlxe":
        store.delete(gateway.MLXE_URL_KEY)
        store.delete(gateway.MLXE_KEY_KEY)
    elif name == "voice":
        for key in (gateway.VOICE_URL_KEY, gateway.VOICE_KEY_KEY,
                    voice.STT_MODEL_KEY, voice.TTS_MODEL_KEY, voice.TTS_VOICE_KEY):
            store.delete(key)
        return {"ok": True, "gateway_synced": True}  # never in Bifrost, nothing to deprovision
    else:
        raise HTTPException(status_code=404, detail="unknown local provider")
    synced = True
    try:
        gateway.remove_provider(name)
    except Exception as exc:  # removed locally, but the gateway is unreachable — surface it
        log.warning("local deprovision skipped: %s", exc)
        synced = False
    return {"ok": True, "gateway_synced": synced}
