"""Client for the local Bifrost LLM gateway (OpenAI-compatible).

The app routes all model calls through Bifrost at ``SMARTBRAIN_LLM_GATEWAY_URL``.
A small capability -> "provider/model" map lets callers ask for a capability
(e.g. "fast_chat") instead of a concrete model id; an explicit model id always
wins. Provider configuration (keys, local endpoints) is managed separately.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

DEFAULT_GATEWAY_URL = "http://bifrost:8080"

# Embedding model id (provider/model) routed to Ollama via Bifrost. The exact
# Ollama tag matters: bare 'ollama/nomic-embed-text' 404s, ':v1.5' resolves.
DEFAULT_EMBED_MODEL = "ollama/nomic-embed-text:v1.5"
_MAX_EMBED_DIM = 4096  # trust-boundary cap on a response vector (mirrors kb._MAX_EMBED_DIM)
# Cap embed input so a long doc doesn't blow the embed model's context window (Ollama
# hard-errors with "input exceeds context length" rather than truncating). ~6000 chars is
# a safe margin under nomic-embed-text's 2048-token window. Long docs embed on their head.
_MAX_EMBED_CHARS = 6000

# Minimal default capability -> "provider/model" map. Made user-editable later
# (settings UI). An explicit model id in the request overrides this.
DEFAULT_ROUTES: dict[str, str] = {
    "fast_chat": "openai/gpt-4o-mini",
    "chat": "openai/gpt-4o-mini",
    "reasoning": "anthropic/claude-3-5-sonnet-latest",
}

# Shown (as a GatewayError) when a request exceeds its timeout — usually a cold local
# model still loading. Clearer than a raw httpx timeout or bifrost's generic wording.
_TIMEOUT_MESSAGE = "the model didn't respond in time — a local model may still be loading; try again in a moment"


def gateway_url() -> str:
    """Return the Bifrost base URL from the environment."""
    url = os.environ.get("SMARTBRAIN_LLM_GATEWAY_URL", DEFAULT_GATEWAY_URL)
    assert url, "gateway url must be non-empty"
    assert url.startswith("http"), "gateway url must be http(s)"
    return url


# --- Module-level pooled httpx client (B22) -------------------------------
# Main's lifespan sets this on startup and clears it on shutdown. Gateway
# functions use the pool when present and fall back to a per-call client when
# absent (e.g. unit tests that never call set_pool). The pool is owned by the
# lifespan — gateway code MUST NOT close it.
_pool: httpx.Client | None = None


def set_pool(client: httpx.Client | None) -> None:
    """Install (or clear with ``None``) the process-wide pooled gateway client."""
    assert client is None or isinstance(client, httpx.Client), "pool must be an httpx.Client or None"
    global _pool
    _pool = client


def _resolve_client(client: httpx.Client | None, timeout: float) -> tuple[httpx.Client, bool]:
    """Pick a client for a single call: explicit arg > module pool > new per-call client.

    Returns ``(client, owns_client)`` — owns_client is True only when this call
    opened the client itself (the caller must close it). Never closes the pool.
    """
    assert timeout > 0, "timeout must be positive"
    if client is not None:
        return client, False
    if _pool is not None:
        return _pool, False
    return httpx.Client(base_url=gateway_url(), timeout=timeout), True


def resolve_model(capability: str, routes: dict[str, str] | None = None) -> str | None:
    """Map a capability to a 'provider/model' id, or None if unmapped."""
    assert capability, "capability must be non-empty"
    table = routes if routes is not None else DEFAULT_ROUTES
    assert isinstance(table, dict), "routes must be a mapping"
    return table.get(capability)


_LOCAL_PROVIDER_NAMES = frozenset(("ollama", "mlx"))

# A local model server (Ollama / MLX / oMLX) serves ONE request at a time: a second overlapping
# request errors with "model is busy; cannot reload runtime settings variant until active requests
# finish". The app has several potential concurrent callers — foreground chat, the background
# auto-reindex embedding (every tick), and parallel scheduled turns — so serialize EVERY
# local-provider call process-wide, and oMLX never sees overlap. Cloud providers are exempt (they
# parallelize fine).
#
# A Semaphore (NOT a Lock/RLock): the streaming path holds this across the SSE generator, which
# Starlette drives across DIFFERENT threadpool workers (one anyio to_thread per chunk, no thread
# affinity). A Lock/RLock raises RuntimeError on a cross-thread release and would leave the lock
# wedged forever; a Semaphore's release is not owner-bound, so acquire-on-A / release-on-B is fine.
# Bounded, so an accidental double-release fails loud instead of silently allowing two callers.
# Every local call is one acquire + one release (no nesting), so re-entrancy isn't needed.
_LOCAL_SEM = threading.BoundedSemaphore(1)


def _is_local(model: str) -> bool:
    """True if ``model`` ('provider/id') is served by a local, single-request-at-a-time server."""
    return bool(model) and model.split("/", 1)[0] in _LOCAL_PROVIDER_NAMES


@contextmanager
def _serialized(model: str) -> Iterator[None]:
    """Hold the local-model semaphore for a call to a local provider; a no-op for cloud models."""
    if _is_local(model):
        _LOCAL_SEM.acquire()
        try:
            yield
        finally:
            _LOCAL_SEM.release()
    else:
        yield


def local_available() -> bool:
    """Non-blocking peek: True if no local-model call is in flight right now. Advisory only — a
    background task (auto-reindex) uses it to skip cleanly when a foreground chat holds the model,
    rather than blocking the scheduler tick behind the user's request. Racy by nature, which is
    fine: any embed it then issues still serializes through ``_serialized``, so it can never collide."""
    if _LOCAL_SEM.acquire(blocking=False):
        _LOCAL_SEM.release()
        return True
    return False


def default_chat_for(catalog: list[dict]) -> str | None:
    """Pick a sensible chat model id from a catalog when only local providers exist.

    A no-cloud-key install (only Ollama/MLX configured) should not fall back to a
    hardcoded cloud default the gateway can't serve. Returns the first local chat
    model in ``catalog`` iff every chat provider in it is local; otherwise None so
    callers keep the existing route (cloud keys ARE configured — the default works).
    """
    assert isinstance(catalog, list), "catalog must be a list"
    chat_models = [m for m in catalog if isinstance(m, dict) and m.get("chat")]
    if not chat_models:
        return None
    providers = {m.get("provider") for m in chat_models if m.get("provider")}
    if not providers or not providers.issubset(_LOCAL_PROVIDER_NAMES):
        return None  # at least one cloud chat provider is available — keep defaults
    for entry in chat_models:  # bounded by len(catalog)
        mid = entry.get("id")
        if isinstance(mid, str) and "/" in mid:
            return mid
    return None


_ROUTES_META_KEY = "model_routes"  # persisted capability->model overrides (plaintext config)


def load_routes(conn) -> dict[str, str]:
    """Return the persisted capability->model map, merged over the built-in defaults.

    Falls back to ``DEFAULT_ROUTES`` for any capability the user hasn't set, so a
    fresh install still resolves a model. Malformed stored JSON is ignored.
    """
    from . import db  # local import: db has no gateway dependency (avoids any cycle)

    assert conn is not None, "conn required to load routes"
    routes = dict(DEFAULT_ROUTES)
    raw = db.meta_get(conn, _ROUTES_META_KEY)
    if not raw:
        return routes
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return routes  # corrupt config — fall back to defaults rather than fail a chat
    if isinstance(stored, dict):
        for cap, model in stored.items():
            if isinstance(cap, str) and isinstance(model, str) and model:
                routes[cap] = model
    return routes


def save_routes(conn, routes: dict[str, str]) -> None:
    """Persist a validated capability->model map to the meta table."""
    from . import db

    assert conn is not None, "conn required to save routes"
    assert isinstance(routes, dict), "routes must be a mapping"
    clean = {c: m for c, m in routes.items() if isinstance(c, str) and isinstance(m, str) and m}
    db.meta_set(conn, _ROUTES_META_KEY, json.dumps(clean))


_CTX_META_KEY = "model_context_lengths"  # persisted 'provider/model' -> context-length-in-tokens
_DEFAULT_CONTEXT_TOKENS = 8192   # conservative fallback when a model's context length is unknown
_CHARS_PER_TOKEN = 3.5           # conservative (real English ~4): UNDER-estimate so we never overflow
_RESULT_FRACTION = 0.30          # a single tool result may occupy ~this share of the context window
_RESULT_CAP_FLOOR = 8000         # a tiny-context model must still return a usable amount of text
_RESULT_CAP_CEILING = 200000     # absolute upper bound on one tool result (bounds transcript memory)


def load_context_lengths(conn) -> dict[str, int]:
    """Return the persisted 'provider/model' -> context-length (tokens) map. Corrupt JSON -> {}."""
    from . import db

    assert conn is not None, "conn required"
    raw = db.meta_get(conn, _CTX_META_KEY)
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return {}  # corrupt config — behave as if unset (fall back to the default)
    return {m: n for m, n in stored.items() if isinstance(m, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0} if isinstance(stored, dict) else {}


def save_context_lengths(conn, lengths: dict[str, int]) -> None:
    """Persist a validated 'provider/model' -> positive-int-tokens map to the meta table."""
    from . import db

    assert conn is not None, "conn required"
    assert isinstance(lengths, dict), "lengths must be a mapping"
    clean = {m: n for m, n in lengths.items() if isinstance(m, str) and "/" in m and isinstance(n, int) and not isinstance(n, bool) and n > 0}
    db.meta_set(conn, _CTX_META_KEY, json.dumps(clean))


def resolve_context_length(conn, model: str) -> int:
    """Context length (tokens) for a model: stored override -> conservative default. No network call.

    The override is populated by local-model registration (probe captures max_model_len) or the
    Settings field; an unknown or unspecified model falls back to ``_DEFAULT_CONTEXT_TOKENS``."""
    if not model or conn is None:
        return _DEFAULT_CONTEXT_TOKENS
    override = load_context_lengths(conn).get(model)
    return override if isinstance(override, int) and override > 0 else _DEFAULT_CONTEXT_TOKENS


def result_cap_for(conn, model: str) -> int:
    """Dynamic cap (chars) on a tool result fed back to ``model`` — scaled to its context length so a
    big-context model can read/return much more than a small one, clamped to a safe [floor, ceiling]."""
    tokens = resolve_context_length(conn, model)
    cap = int(tokens * _RESULT_FRACTION * _CHARS_PER_TOKEN)
    return max(_RESULT_CAP_FLOOR, min(cap, _RESULT_CAP_CEILING))


class GatewayError(Exception):
    """An LLM gateway/provider error carrying an upstream status + message."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.tools_unsupported = False  # set when the upstream rejected the tools field


def _extract_error(payload: dict) -> str | None:
    """Pull a human-readable message out of a Bifrost error body, if present."""
    err = payload.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("error")
    return err if isinstance(err, str) else None


_MODELS_PATH = "/v1/models"
_MAX_MODELS = 2000  # trust-boundary cap on how many catalog entries we parse
# Method names that mark a model as chat-capable (Gemini exposes these explicitly).
_CHAT_METHODS = frozenset({"generateContent", "chat", "chat.completions", "completions"})
# Name substrings marking an embedding model when no method hints are present (Ollama/oMLX
# report none). "embed" catches nomic/mxbai/snowflake-arctic/jina; the families below are
# embedders whose ids DON'T contain "embed" — notably BAAI bge (e.g. bge-m3).
_EMBED_HINTS = ("embed", "bge", "minilm", "instructor", "sentence-transformers")
# Substrings that mark a non-chat model when no method hints are present (Ollama/OpenAI).
_NON_CHAT_HINTS = (*_EMBED_HINTS, "whisper", "tts", "dall-e", "-image", "rerank", "moderation")
# Methods/ids that mark an embedding model (Gemini exposes embedContent; Ollama by name).
_EMBED_METHODS = frozenset({"embedContent", "embed"})


def _is_embed_model(model: dict) -> bool:
    """Heuristic: is this an embedding model (vs chat/image/etc.)?"""
    assert isinstance(model, dict), "model entry must be a dict"
    methods = model.get("supported_methods") or []
    if methods:
        return any(m in _EMBED_METHODS for m in methods)
    ident = (model.get("id") or "").lower()
    return any(h in ident for h in _EMBED_HINTS)


def _is_chat_model(model: dict) -> bool:
    """Heuristic: can this model do chat completions (vs embeddings/image/audio/tts)?"""
    assert isinstance(model, dict), "model entry must be a dict"
    ident = (model.get("id") or "").lower()
    if any(hint in ident for hint in _NON_CHAT_HINTS):
        return False  # image/tts/embed variants can expose generateContent — exclude by id
    methods = model.get("supported_methods") or []
    if methods:  # explicit capabilities (Gemini): chat models expose generateContent
        return any(m in _CHAT_METHODS for m in methods)
    return True  # no id hints + no method info (Ollama/OpenAI): assume chat-capable


def _norm_pricing(p: object) -> dict | None:
    """Normalize Bifrost per-token pricing to {prompt, completion} floats; None if free/local."""
    if not isinstance(p, dict):
        return None
    try:
        prompt = float(p.get("prompt") or 0.0)
        completion = float(p.get("completion") or 0.0)
    except (TypeError, ValueError):
        return None
    if prompt <= 0.0 and completion <= 0.0:
        return None  # local / free model
    return {"prompt": prompt, "completion": completion}


def list_models(*, client: httpx.Client | None = None, timeout: float = 6.0) -> list[dict]:
    """Discover available models from Bifrost's live catalog (OpenAI /v1/models).

    Returns one dict per model — {id, name, provider, context_length, pricing,
    chat} — derived from the gateway, so the list stays current as providers and
    their catalogs change. Raises ``GatewayError`` on a bad/unreachable gateway.

    ``timeout`` is passed per-request so it binds even on the pooled client (whose
    own timeout is sized for chat, 60s): Bifrost aggregates every registered
    provider's catalog live, so one provider URL that hangs (a stale LAN address
    times out rather than refusing) would otherwise stall this call — and every
    /api/models caller with it — for the pool's full timeout.
    """
    client, owns_client = _resolve_client(client, timeout)
    try:
        resp = client.get(_MODELS_PATH, timeout=timeout)
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
            except ValueError:
                err_body = {}
            message = _extract_error(err_body) if isinstance(err_body, dict) else None
            raise GatewayError(resp.status_code, message or "model list failed")
        try:
            data = resp.json()
        except ValueError:
            raise GatewayError(resp.status_code or 502, "non-JSON gateway response") from None
    finally:
        if owns_client:
            client.close()
    raw = data.get("data") if isinstance(data, dict) else None
    assert isinstance(raw, list), "model catalog must be a JSON array under 'data'"
    out: list[dict] = []
    for item in raw[:_MAX_MODELS]:  # bounded loop
        mid = item.get("id") if isinstance(item, dict) else None
        if not mid or "/" not in mid:
            continue  # skip malformed / un-prefixed ids
        out.append({
            "id": mid,
            "name": item.get("name") or mid,
            "provider": mid.split("/", 1)[0],
            "context_length": item.get("context_length"),
            "pricing": _norm_pricing(item.get("pricing")),
            "chat": _is_chat_model(item),
            "embed": _is_embed_model(item),
        })
    return out


_STREAM_MAX_CHUNKS = 20000  # fixed bound on SSE chunks parsed per stream (P10 #2)
_STREAM_LINE_PREFIX = "data:"
_STREAM_DONE = "[DONE]"


def _parse_sse_line(line: str) -> dict | None:
    """Return the JSON object on a ``data: ...`` SSE line, or None to skip.

    Yields ``None`` for blank lines, comments, the ``[DONE]`` sentinel, or any
    body we cannot parse — the caller decides whether that ends the stream.
    """
    assert isinstance(line, str), "sse line must be a string"
    stripped = line.strip()
    if not stripped or stripped.startswith(":"):  # blank / SSE comment
        return None
    if not stripped.startswith(_STREAM_LINE_PREFIX):
        return None  # event:/id:/retry: lines — we only consume data lines
    body = stripped[len(_STREAM_LINE_PREFIX):].strip()
    if not body or body == _STREAM_DONE:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None  # malformed JSON chunk — skip (bounded by _STREAM_MAX_CHUNKS)


def _chunk_delta(chunk: dict) -> tuple[str, list | None, str | None]:
    """Pull (text-delta, tool_calls-or-None, finish_reason) from one stream chunk."""
    assert isinstance(chunk, dict), "stream chunk must be a JSON object"
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", None, None
    first = choices[0]
    delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
    text = delta.get("content") or ""
    tool_calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else None
    finish = first.get("finish_reason") if isinstance(first.get("finish_reason"), str) else None
    return text if isinstance(text, str) else "", tool_calls, finish


def chat_stream(
    messages: list[dict],
    model: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
    tools_spec: list[dict] | None = None,
) -> Iterator[dict]:
    """Yield streaming chat deltas from Bifrost's OpenAI-compatible SSE endpoint.

    Each yielded item is ``{"delta": str, "tool_calls": list|None, "finish_reason": str|None}``.
    When ``tools_spec`` is given the model is offered the tools (tool_choice auto); a
    non-empty ``tool_calls`` field on any chunk means the model started a tool turn — the
    caller should stop streaming and fall back to the non-streaming path. Without it, the
    model can only answer in plain text and would NARRATE actions it cannot perform.
    Raises ``GatewayError`` on the upstream error envelope or a non-200 status; on a
    failure that looks like the model not supporting tools, ``tools_unsupported`` is set so
    the caller can retry without tools.
    """
    assert messages, "messages must be non-empty"
    assert model, "model must be specified"
    assert tools_spec is None or tools_spec, "tools_spec, if given, must be non-empty"
    client, owns_client = _resolve_client(client, timeout)
    payload: dict = {"model": model, "messages": messages, "stream": True}
    if tools_spec:
        payload["tools"] = tools_spec
        payload["tool_choice"] = "auto"
    try:
        # Hold the local-model lock for the whole stream so a background embed / retry can't
        # overlap this generation on a single-request-at-a-time server (no-op for cloud).
        with _serialized(model), client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                body = resp.read()
                try:
                    err = json.loads(body)
                except (ValueError, TypeError):
                    err = {}
                msg = _extract_error(err) if isinstance(err, dict) else None
                error = GatewayError(resp.status_code, msg or f"gateway error ({resp.status_code})")
                if tools_spec:
                    error.tools_unsupported = _looks_tools_unsupported(resp.status_code, msg or "")
                raise error
            count = 0
            for line in resp.iter_lines():  # bounded by _STREAM_MAX_CHUNKS below
                count += 1
                if count > _STREAM_MAX_CHUNKS:
                    raise GatewayError(502, "stream exceeded max chunks")
                chunk = _parse_sse_line(line)
                if chunk is None:
                    continue
                err_msg = _extract_error(chunk) if isinstance(chunk, dict) else None
                if err_msg:
                    raise GatewayError(502, err_msg)
                text, tool_calls, finish = _chunk_delta(chunk)
                if not text and tool_calls is None and finish is None:
                    continue
                yield {"delta": text, "tool_calls": tool_calls, "finish_reason": finish}
    except httpx.TimeoutException:  # cold local model slow to first token — surface it clearly
        raise GatewayError(504, _TIMEOUT_MESSAGE) from None
    finally:
        if owns_client:
            client.close()


def chat(
    messages: list[dict],
    model: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> dict:
    """Send a chat completion through Bifrost; return the parsed JSON response.

    Raises ``GatewayError`` (with the upstream status + provider message) when
    the gateway/provider reports an error, instead of leaking httpx internals.
    """
    assert messages, "messages must be non-empty"
    assert model, "model must be specified"
    client, owns_client = _resolve_client(client, timeout)
    try:
        with _serialized(model):  # serialize local-provider calls (no-op for cloud)
            resp = client.post(
                "/v1/chat/completions", json={"model": model, "messages": messages}, timeout=timeout
            )
        try:
            data = resp.json()
        except ValueError:
            raise GatewayError(resp.status_code or 502, "non-JSON gateway response") from None
        message = _extract_error(data) if isinstance(data, dict) else None
        if resp.status_code >= 400 or message:
            status = resp.status_code if resp.status_code >= 400 else 502
            raise GatewayError(status, message or f"gateway error ({resp.status_code})")
    except httpx.TimeoutException:
        raise GatewayError(504, _TIMEOUT_MESSAGE) from None
    finally:
        if owns_client:
            client.close()
    assert isinstance(data, dict), "gateway response must be a JSON object"
    return data


def completion_text(data: dict) -> str:
    """Extract the assistant message text from a ``chat`` response (choices[0].message.content).

    Returns "" for a malformed/empty completion rather than raising, so a summarization loop can
    degrade a bad chunk gracefully. Shared by callers that need the plain text (not the raw JSON)."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list):
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


def _looks_tools_unsupported(status: int, message: str) -> bool:
    """Heuristic: did the upstream reject the tools field (vs a real error)?"""
    lowered = message.lower()
    return status in (400, 404, 422, 501) and (
        "tool" in lowered or "function" in lowered or "does not support" in lowered
    )


def chat_with_tools(
    messages: list[dict],
    model: str,
    tools_spec: list[dict],
    *,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> dict:
    """Chat completion with OpenAI tool-calling enabled; return parsed JSON.

    Additive sibling of ``chat`` (which never sends a tools field). On an
    upstream error, raises GatewayError with ``tools_unsupported`` set when the
    failure looks like the model/provider not supporting tools — so the agent
    loop can degrade to a plain completion instead of failing the turn.
    """
    assert messages, "messages must be non-empty"
    assert model, "model must be specified"
    assert tools_spec, "tools spec must be non-empty"
    client, owns_client = _resolve_client(client, timeout)
    try:
        # Pass timeout per-request: the pooled client (always installed in prod) has a
        # fixed default, so only a per-call override lets the agent/scheduled path wait
        # out a cold local-model load. Serialize local-provider calls (no-op for cloud).
        with _serialized(model):
            resp = client.post(
                "/v1/chat/completions",
                json={"model": model, "messages": messages, "tools": tools_spec, "tool_choice": "auto"},
                timeout=timeout,
            )
        try:
            data = resp.json()
        except ValueError:
            raise GatewayError(resp.status_code or 502, "non-JSON gateway response") from None
        message = _extract_error(data) if isinstance(data, dict) else None
        if resp.status_code >= 400 or message:
            status = resp.status_code if resp.status_code >= 400 else 502
            error = GatewayError(status, message or f"gateway error ({resp.status_code})")
            error.tools_unsupported = _looks_tools_unsupported(status, message or "")
            raise error
    except httpx.TimeoutException:
        raise GatewayError(504, _TIMEOUT_MESSAGE) from None
    finally:
        if owns_client:
            client.close()
    assert isinstance(data, dict), "gateway response must be a JSON object"
    return data


def embed_model(conn=None) -> str:
    """Return the embedding model id (provider/model).

    Precedence: the user's routed "embedding" model (when a conn is given and one
    is set) > the SMARTBRAIN_EMBED_MODEL env var > the built-in default. Changing
    the routed model makes existing embeddings stale until a reindex.
    """
    routed = load_routes(conn).get("embedding") if conn is not None else None
    model = routed or os.environ.get("SMARTBRAIN_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    assert model, "embed model must be non-empty"
    assert "/" in model, "embed model must be 'provider/model'"
    return model


def embed(
    input_text: str,
    model: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> list[float]:
    """Embed text through Bifrost's /v1/embeddings; return the float vector.

    Raises ``GatewayError`` on the upstream error envelope, a non-JSON body, or
    a malformed 200 (empty data, missing/empty embedding, non-finite element).
    """
    assert input_text, "input text must be non-empty"
    assert model, "model must be specified"
    input_text = input_text[:_MAX_EMBED_CHARS]  # fit the embed context; long docs embed on their head
    client, owns_client = _resolve_client(client, timeout)
    try:
        # Per-request timeout: a cold local embed model can take ~50s to load on its first call,
        # far past a pooled/short client default — only the per-call value lets a backfill wait it
        # out. Serialize local-provider calls so a background embed can't overlap a foreground chat.
        with _serialized(model):
            resp = client.post("/v1/embeddings", json={"model": model, "input": input_text}, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            raise GatewayError(resp.status_code or 502, "non-JSON gateway response") from None
        message = _extract_error(data) if isinstance(data, dict) else None
        if resp.status_code >= 400 or message:
            status = resp.status_code if resp.status_code >= 400 else 502
            raise GatewayError(status, message or f"gateway error ({resp.status_code})")
    except httpx.TimeoutException:
        raise GatewayError(504, _TIMEOUT_MESSAGE) from None
    finally:
        if owns_client:
            client.close()
    return _vector_from(data)


def _vector_from(data: object) -> list[float]:
    """Validate a gateway embeddings response and return the vector."""
    assert data is not None, "gateway response required"
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise GatewayError(502, "embeddings response missing data")
    vector = items[0].get("embedding")
    if not isinstance(vector, list) or not vector:
        raise GatewayError(502, "embeddings response missing embedding")
    if len(vector) > _MAX_EMBED_DIM:
        raise GatewayError(502, "embeddings response too long")
    # bool is a subclass of int — reject it explicitly so True/False can't pose as a value.
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
        raise GatewayError(502, "embeddings response has non-numeric/non-finite values")
    result = [float(x) for x in vector]
    assert result and all(math.isfinite(x) for x in result), "vector must be non-empty + finite"
    return result


# --- Provider provisioning -------------------------------------------------
# Logical cloud provider -> Bifrost provider name. "google" means Google AI
# Studio (a Gemini API key), which Bifrost calls "gemini".
CLOUD_PROVIDERS: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
}


def _secret_name(logical: str) -> str:
    """Secret-store key name holding a provider's API key."""
    assert logical, "provider name required"
    return f"provider:{logical}:api_key"


_KEY_ATTACH_ATTEMPTS = 4


def _run(client: httpx.Client | None, fn) -> None:
    """Run ``fn(client)``, managing a fresh client when none is supplied."""
    client, owns_client = _resolve_client(client, 15.0)
    try:
        fn(client)
    finally:
        if owns_client:
            client.close()


def _attach_key(client: httpx.Client, bifrost_name: str, key_payload: dict) -> None:
    """POST a fully-formed key payload to a provider, retrying transient 5xx.

    Bifrost's key sub-resource intermittently 500s ("failed to create provider
    key in store") under rapid writes; a short bounded retry clears it. Real 4xx
    are not retried. Key ``name`` must be globally unique across providers.
    """
    assert bifrost_name, "provider name required"
    assert key_payload.get("value"), "key payload must include a value"
    last_status = 0
    for attempt in range(_KEY_ATTACH_ATTEMPTS):  # fixed bound
        resp = client.post(f"/api/providers/{bifrost_name}/keys", json=key_payload)
        if resp.status_code < 500:
            resp.raise_for_status()
            return
        last_status = resp.status_code
        time.sleep(0.5 * (attempt + 1))
    msg = f"key attach to '{bifrost_name}' failed after {_KEY_ATTACH_ATTEMPTS} tries"
    raise GatewayError(last_status, msg)


def _replace_provider(
    client: httpx.Client, bifrost_name: str, create_body: dict, key_payload: dict
) -> None:
    """Delete (if present), recreate a provider, then attach its key.

    Providers are create-only in Bifrost and inline keys in the create body are
    ignored, so delete-then-create gives a clean slate ending with one key.
    """
    assert bifrost_name, "provider name required"
    assert create_body.get("provider"), "create_body must name the provider"
    client.delete(f"/api/providers/{bifrost_name}")  # drop stale provider + keys
    created = client.post("/api/providers", json=create_body)
    if created.status_code not in (200, 201, 409):
        created.raise_for_status()
    _attach_key(client, bifrost_name, key_payload)


def set_provider(bifrost_name: str, api_key: str, *, client: httpx.Client | None = None) -> None:
    """(Re)configure a cloud provider in Bifrost with a single API key."""
    assert bifrost_name, "bifrost provider name required"
    assert api_key, "api key required"
    create_body = {"provider": bifrost_name}
    key_payload = {
        "name": f"smartbrain-{bifrost_name}",
        "value": api_key,
        "models": ["*"],
        "weight": 1.0,
    }
    _run(client, lambda c: _replace_provider(c, bifrost_name, create_body, key_payload))


def provision_from_store(store, *, client: httpx.Client | None = None) -> list[str]:
    """Materialize stored cloud keys into Bifrost; return Bifrost names set."""
    assert store is not None, "secret store required"
    client, owns_client = _resolve_client(client, 15.0)
    provisioned: list[str] = []
    try:
        for logical, bifrost_name in CLOUD_PROVIDERS.items():  # fixed, bounded
            api_key = store.get(_secret_name(logical))
            if not api_key:
                continue
            set_provider(bifrost_name, api_key, client=client)
            provisioned.append(bifrost_name)
    finally:
        if owns_client:
            client.close()
    assert isinstance(provisioned, list), "must return a list"
    return provisioned


def deprovision(*, client: httpx.Client | None = None) -> None:
    """Remove all SmartBrain-managed providers from Bifrost (best-effort)."""
    client, owns_client = _resolve_client(client, 15.0)
    try:
        for bifrost_name in CLOUD_PROVIDERS.values():  # fixed, bounded
            try:
                client.delete(f"/api/providers/{bifrost_name}")
            except Exception:
                pass  # best-effort; absence is fine
    finally:
        if owns_client:
            client.close()


def provider_for_secret_key(secret_key: str) -> str | None:
    """If ``secret_key`` names a managed provider key, return its Bifrost name."""
    assert secret_key, "secret key required"
    for logical, bifrost_name in CLOUD_PROVIDERS.items():  # fixed, bounded
        if secret_key == _secret_name(logical):
            return bifrost_name
    return None


def remove_provider(bifrost_name: str, *, client: httpx.Client | None = None) -> None:
    """Remove a single provider from Bifrost (best-effort, idempotent)."""
    assert bifrost_name, "bifrost provider name required"
    client, owns_client = _resolve_client(client, 15.0)
    try:
        client.delete(f"/api/providers/{bifrost_name}")
    finally:
        if owns_client:
            client.close()


# --- Local model providers (Ollama / MLX, on the host) --------------------
# Local servers run on the host and must bind 0.0.0.0; the gateway reaches them
# at host.docker.internal. Config lives in the secret store under these keys.
LOCAL_PROVIDERS = ("ollama", "mlx")
# Bifrost's per-provider request timeout defaults to 30s — too short for a COLD local
# model load (an agent/scheduled turn can be the first request after boot, and loading a
# large MLX/Ollama model into memory takes well over 30s). Give local providers a generous
# budget so a cold load completes instead of 504-ing the turn. Cloud providers keep the
# default. The app-side agent timeout (scheduler._AGENT_TURN_TIMEOUT) is matched to this.
_LOCAL_PROVIDER_TIMEOUT_SECS = 300
OLLAMA_URL_KEY = "local:ollama:url"
MLX_URL_KEY = "local:mlx:url"
MLX_KEY_KEY = "local:mlx:api_key"
# Default host URLs for auto-detecting a server when nothing is configured yet — the
# common "I installed Ollama and SmartBrain, now connect them" path. The gateway runs
# in-container and reaches host services via host.docker.internal.
OLLAMA_DEFAULT_URL = "http://host.docker.internal:11434"
MLX_DEFAULT_URL = "http://host.docker.internal:8888"


def register_ollama(url: str, *, client: httpx.Client | None = None) -> None:
    """Register Ollama as a NATIVE Bifrost provider reachable at ``url``.

    Ollama is a built-in provider type (no custom_provider_config), and its key
    must carry ``ollama_key_config.url``.
    """
    assert url, "ollama url required"
    create_body = {
        "provider": "ollama",
        # allow_private_network: bifrost >= v1.6.4 SSRF-guards provider URLs and refuses private IPs
        # by default — but host.docker.internal IS a private IP, and reaching host-local model
        # servers is this provider's entire job. Scoped per-provider; link-local stays blocked.
        "network_config": {"base_url": url, "default_request_timeout_in_seconds": _LOCAL_PROVIDER_TIMEOUT_SECS,
                           "allow_private_network": True},
    }
    key_payload = {
        "name": "smartbrain-ollama",
        "value": "ollama",
        "models": ["*"],
        "weight": 1.0,
        "ollama_key_config": {"url": url},
    }
    _run(client, lambda c: _replace_provider(c, "ollama", create_body, key_payload))


def register_mlx(url: str, api_key: str = "", *, client: httpx.Client | None = None) -> None:
    """Register an MLX (OpenAI-compatible) server as a CUSTOM Bifrost provider.

    ``api_key`` is optional — many local MLX/OMLX servers don't verify one. Bifrost
    still wants a non-empty key value, so a placeholder is used when none is given
    (mirrors the Ollama registration).
    """
    assert url, "mlx url required"
    assert isinstance(api_key, str), "mlx api key must be a string"
    create_body = {
        "provider": "mlx",
        # allow_private_network: bifrost >= v1.6.4 SSRF-guards provider URLs and refuses private IPs
        # by default — but host.docker.internal IS a private IP, and reaching host-local model
        # servers is this provider's entire job. Scoped per-provider; link-local stays blocked.
        "network_config": {"base_url": url, "default_request_timeout_in_seconds": _LOCAL_PROVIDER_TIMEOUT_SECS,
                           "allow_private_network": True},
        "custom_provider_config": {
            "base_provider_type": "openai",
            # list_models lets Bifrost enumerate the server's /v1/models into its own
            # catalog — without it MLX chats fine but never shows up in the model
            # dropdowns (list_models defaults to False for custom providers).
            # embedding lets an MLX embedding model (e.g. nomic-embed-text-v1.5) serve
            # Knowledge semantic search, so the whole stack can run MLX-only (no Ollama).
            "allowed_requests": {
                "chat_completion": True,
                "chat_completion_stream": True,
                "list_models": True,
                "embedding": True,
            },
        },
    }
    key_payload = {"name": "smartbrain-mlx", "value": api_key or "none", "models": ["*"], "weight": 1.0}
    _run(client, lambda c: _replace_provider(c, "mlx", create_body, key_payload))


def provision_local_from_store(store, *, client: httpx.Client | None = None) -> list[str]:
    """Register any configured local providers (Ollama/MLX) from the store."""
    assert store is not None, "secret store required"
    done: list[str] = []

    def _do(c: httpx.Client) -> None:
        ollama_url = store.get(OLLAMA_URL_KEY)
        if ollama_url:
            register_ollama(ollama_url, client=c)
            done.append("ollama")
        mlx_url = store.get(MLX_URL_KEY)
        if mlx_url:  # key is optional (keyless MLX/OMLX) — gate on the URL, like Ollama
            register_mlx(mlx_url, store.get(MLX_KEY_KEY) or "", client=c)
            done.append("mlx")

    _run(client, _do)
    assert isinstance(done, list), "must return a list"
    return done


def deprovision_local(*, client: httpx.Client | None = None) -> None:
    """Remove local providers from Bifrost (best-effort)."""
    def _do(c: httpx.Client) -> None:
        for name in LOCAL_PROVIDERS:  # fixed, bounded
            try:
                c.delete(f"/api/providers/{name}")
            except Exception:
                pass  # best-effort; absence is fine

    _run(client, _do)


def local_fallback_models(store) -> list[dict]:
    """Catalog fallback built by probing configured LOCAL providers directly.

    When Bifrost's aggregated catalog is unavailable (one registered provider's
    URL hanging wedges the whole /v1/models — the very failure that motivated
    this), the local servers themselves are usually fine. Probing them directly
    (each probe carries its own short timeout) degrades the catalog to "local
    models only" instead of an empty list: chat keeps working, and cloud models
    reappear once the gateway catalog recovers. Entries mirror list_models' shape.
    """
    out: list[dict] = []
    ollama_url = store.get(OLLAMA_URL_KEY)
    if ollama_url:
        for name in probe_ollama(ollama_url)["models"][:_MAX_MODELS]:
            mid = f"ollama/{name}"
            out.append({
                "id": mid, "name": mid, "provider": "ollama", "context_length": None,
                "pricing": None, "chat": _is_chat_model({"id": mid}), "embed": _is_embed_model({"id": mid}),
            })
    mlx_url = store.get(MLX_URL_KEY)
    if mlx_url:
        probe = probe_mlx(mlx_url, store.get(MLX_KEY_KEY) or "")
        for name in probe["models"][:_MAX_MODELS]:
            mid = f"mlx/{name}"
            out.append({
                "id": mid, "name": mid, "provider": "mlx",
                "context_length": probe["context_lengths"].get(name),
                "pricing": None, "chat": _is_chat_model({"id": mid}), "embed": _is_embed_model({"id": mid}),
            })
    return out


def probe_ollama(url: str, *, client: httpx.Client | None = None, timeout: float = 4.0) -> dict:
    """Return ``{reachable, models}`` for an Ollama server (best-effort).

    ``timeout`` is short for unconfigured auto-detection (a quick localhost check that
    must not hang the status call when nothing is listening) and longer once configured.
    """
    assert url, "url required"
    assert timeout > 0, "timeout must be positive"
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.get(f"{url.rstrip('/')}/api/tags")
        resp.raise_for_status()
        names = [m.get("name") for m in resp.json().get("models", []) if m.get("name")]
        return {"reachable": True, "models": names}
    except Exception:
        return {"reachable": False, "models": []}
    finally:
        if owns_client:
            client.close()


def probe_mlx(url: str, api_key: str, *, client: httpx.Client | None = None, timeout: float = 4.0) -> dict:
    """Return ``{reachable, models, context_lengths}`` for an MLX OpenAI server (best-effort).

    The MLX server's /v1/models reports ``max_model_len`` per model — the context length bifrost
    strips from its own catalog — so ``context_lengths`` maps each model id to it (when present).
    ``timeout`` is short for unconfigured auto-detection (see ``probe_ollama``).
    """
    assert url, "url required"
    assert timeout > 0, "timeout must be positive"
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = client.get(f"{url.rstrip('/')}/v1/models", headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        ids = [m.get("id") for m in data if m.get("id")]
        ctx = {m["id"]: int(m["max_model_len"]) for m in data
               if m.get("id") and isinstance(m.get("max_model_len"), int) and m["max_model_len"] > 0}
        return {"reachable": True, "models": ids, "context_lengths": ctx}
    except Exception:
        return {"reachable": False, "models": [], "context_lengths": {}}
    finally:
        if owns_client:
            client.close()
