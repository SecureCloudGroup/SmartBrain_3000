"""Which model each Neural Interface step runs on: the one place every NI model call site asks (pure choice).

A step is a kind of model work, not a feature: ``build`` (reading the ask, planning the query, writing the
recipe), ``research`` (finding a source), ``refresh`` (a model-source card's repeated answer) and ``summary``
(the refresh-time llm stage, a brief's summary). Verification and compare are code and have no step.

Each step has a setting, ``auto`` (the default), ``cloud`` or ``local``. ``auto`` follows the step's kind: the
one-time work (build, research) runs on the cloud model when one is routed, the repeated work (refresh, summary)
runs on the local model. ``summary`` is local-only whatever is stored: a sealed ``local/auto`` card never resolves
to cloud. Choosing a step's model never grants consent: a cloud model still needs the card's own consent wherever
the call site asked for it before.

ONE RULE FOR EVERY STEP (2026-10-29, the NVDA card): ``choose`` is the whole rule, and the flow, the Settings page
and every gate read it; no step has a private fallback the others lack. The cloud model is env
``SMARTBRAIN_NI_RESEARCH_MODEL`` or meta ``ni_research_model``, else the first ``anthropic/*`` or ``claudecode/*``
on the ni / chat / agent routes (never a local one): one cloud model for both one-time steps, so the consent a
build asks for covers its research too. The Claude Code route counts as cloud exactly as it does for a build: the
same per-card consent question, the same ceilings (the #533 deferral is withdrawn: builds already ran on it with
that consent). The local model is the first local one on the same routes, resolved at call time. On ``auto``, a
step with neither side routed falls back to the older route order every step used before per-step settings
(``fallback_model``): an explicit ``ni`` route (the operator's word, whatever provider), else a local chat / agent
model, else whatever chat / agent is. A forced side (``cloud`` / ``local``) with nothing routed there is None:
the step stops and says so, and Settings refuses to store such a choice (``save``).

Settings live in meta ``ni_step_models`` (JSON ``{step: setting}``). Research also honours its older switch (env
``SMARTBRAIN_NI_RESEARCH`` wins over everything; meta ``ni_research`` when the step is unset), whose ``off`` turns
research off.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import db, gateway

STEPS = ("build", "research", "refresh", "summary")
SETTINGS = ("auto", "cloud", "local")
LOCAL_ONLY = frozenset(("summary",))
ONE_TIME = frozenset(("build", "research"))   # auto = cloud when routed; every other step's auto = local
CLOUD_PROVIDERS = frozenset(("anthropic", "claudecode"))   # the providers the cloud side of a step may run on
LABELS = {
    "build": "Building a card (reading the ask, writing the recipe)",
    "research": "Researching a source",
    "refresh": "Refreshing a model-answered card",
    "summary": "Brief summaries",
}
PURPOSES = {step: f"ni.{step}" for step in STEPS}   # the usage_log purpose each step's calls are recorded under

_META_KEY = "ni_step_models"
_ENV_CLOUD = "SMARTBRAIN_NI_RESEARCH_MODEL"
_CAPABILITIES = ("ni", "chat", "agent")


@dataclass(frozen=True)
class Choice:
    """``where``: cloud | local | None (nothing routed for the setting). ``model``: the id to call, or None.
    ``cloud`` / ``local``: what each side resolves to now, so a caller can fall back (or ask) without re-reading.
    ``fallback``: True when ``model`` came from the older route order (``auto`` with neither side routed)."""

    step: str
    setting: str
    where: str | None
    model: str | None
    cloud: str | None
    local: str | None
    fallback: bool = False


def stored(conn) -> dict:
    """The saved ``{step: setting}`` map; anything malformed or unknown is dropped."""
    assert conn is not None, "conn required"
    try:
        raw = json.loads(db.meta_get(conn, _META_KEY) or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {s: v for s, v in raw.items() if s in STEPS and v in SETTINGS}


def setting(conn, step: str) -> str:
    """The step's setting (``auto`` when unset). Local-only steps read ``local`` for a stored ``cloud``."""
    assert step in STEPS, f"unknown NI step {step!r}"
    value = stored(conn).get(step, "auto")
    if step in LOCAL_ONLY and value == "cloud":
        return "local"
    return value


def settings(conn) -> dict[str, str]:
    """Every step's setting."""
    assert conn is not None, "conn required"
    return {step: setting(conn, step) for step in STEPS}


def save(conn, changes: dict, routes: dict | None = None, gw=gateway) -> dict[str, str]:
    """Merge ``{step: setting}`` into the saved map and return every step's setting. Raises ValueError on an unknown
    step or setting, ``cloud`` for a local-only step, or a side nothing is routed on (#561 review note: Settings
    refuses a choice routing cannot serve, so a step is never silently left without a model); nothing is stored
    on a refusal."""
    assert conn is not None, "conn required"
    assert isinstance(changes, dict), "changes must be a mapping"
    merged = stored(conn)
    table = None
    for step, value in changes.items():  # bounded by the caller's body (at most len(STEPS) known keys)
        if step not in STEPS:
            raise ValueError(f"unknown NI step {step!r}")
        if value not in SETTINGS:
            raise ValueError(f"{step}: setting must be one of {', '.join(SETTINGS)}")
        if step in LOCAL_ONLY and value == "cloud":
            raise ValueError(f"{step} runs on this computer only")
        if value != "auto":
            if table is None:
                table = routes if routes is not None else gw.load_routes(conn)
            if value == "cloud" and not cloud_model(conn, table, gw):
                raise ValueError(f"{step}: no cloud model is routed; route ni, chat or agent to an Anthropic or "
                                 "Claude Code model first (Settings > Models)")
            if value == "local" and not local_model(conn, table, gw):
                raise ValueError(f"{step}: no local model is routed; route ni, chat or agent to a model on this "
                                 "computer first (Settings > Models)")
        if value == "auto":
            merged.pop(step, None)  # auto is "unset": an older switch (research's ``ni_research``) keeps its say
        else:
            merged[step] = value
    db.meta_set(conn, _META_KEY, json.dumps({s: merged[s] for s in STEPS if s in merged}, sort_keys=True))
    return settings(conn)


def local_model(conn, routes: dict | None = None, gw=gateway) -> str | None:
    """The first LOCAL model on the ni / chat / agent routes, or None (a cloud id is never returned). ``gw`` is the
    gateway module, or a caller's injected stand-in for it (``load_routes`` / ``resolve_model`` / ``is_local``)."""
    assert conn is not None or routes is not None, "conn or routes required"
    table = routes if routes is not None else gw.load_routes(conn)
    for capability in _CAPABILITIES:  # bounded to 3
        model = gw.resolve_model(capability, table)
        if model and gw.is_local(model):
            return model
    return None


def is_cloud(model: object, gw=gateway) -> bool:
    """Is ``model`` one the cloud side of a step may run on: an ``anthropic/*`` or ``claudecode/*`` id that is not
    local? (The Claude Code CLI is served by the user's own login: a cloud service, never local.)"""
    return isinstance(model, str) and model.split("/", 1)[0] in CLOUD_PROVIDERS and not gw.is_local(model)


def cloud_model(conn, routes: dict | None = None, gw=gateway) -> str | None:
    """The one cloud model the cloud side of every step uses: env or meta ``ni_research_model``, else the first
    ``anthropic/*`` or ``claudecode/*`` on the ni / chat / agent routes. Never a local one, and never another
    provider (those reach a step only through ``fallback_model``)."""
    assert conn is not None, "conn required"
    explicit = os.environ.get(_ENV_CLOUD) or db.meta_get(conn, "ni_research_model")
    table = routes if routes is not None else gw.load_routes(conn)
    for model in (explicit, *(gw.resolve_model(c, table) for c in _CAPABILITIES)):  # bounded: 4
        if is_cloud(model, gw):
            return model
    return None


def fallback_model(routes: dict, gw=gateway) -> str | None:
    """The older route order a step on ``auto`` falls back to when neither side is routed (C1, audit 2026-09-13;
    P0, 2026-09-16): an EXPLICIT ``ni`` route, the operator's word even when it is a cloud model of any provider;
    else a LOCAL ``chat`` / ``agent`` model (§29 local-preferred); else whatever ``chat`` / ``agent`` is."""
    assert isinstance(routes, dict), "routes required"
    ni_route = gw.resolve_model("ni", routes)
    if ni_route:
        return ni_route
    chat, agent = gw.resolve_model("chat", routes), gw.resolve_model("agent", routes)
    for candidate in (chat, agent):  # bounded to 2 (P10 #2)
        if candidate and gw.is_local(candidate):
            return candidate
    return chat or agent


def choose(conn, step: str, routes: dict | None = None, gw=gateway) -> Choice:
    """The model ``step`` runs on now: THE rule (the flow, the Settings page and the gates all read this).
    ``cloud`` / ``local`` settings with nothing routed on that side give ``where=None`` (the caller decides how to
    stop); ``auto`` falls back to the other side when its own is missing, then to ``fallback_model`` (a one-time
    step tries the older order right after the cloud model, so an explicit ``ni`` route keeps its say; a repeated
    step tries it after both sides); a local-only step never has a cloud side and only takes a local fallback."""
    assert step in STEPS, f"unknown NI step {step!r}"
    assert conn is not None, "conn required"
    table = routes if routes is not None else gw.load_routes(conn)
    value = setting(conn, step)
    local = local_model(conn, table, gw)
    cloud = None if step in LOCAL_ONLY else cloud_model(conn, table, gw)
    if value == "auto":
        order = ("cloud", "fallback", "local") if step in ONE_TIME else ("local", "cloud", "fallback")
    else:
        order = (value,)
    for side in order:  # bounded to 3
        if side == "fallback":
            model = fallback_model(table, gw)
            if model and (step not in LOCAL_ONLY or gw.is_local(model)):
                # ``fallback`` marks a model only the older order reaches (another provider): the step's own
                # local model found by that order is simply the local side
                return Choice(step, value, "local" if gw.is_local(model) else "cloud", model, cloud, local,
                              model not in (cloud, local))
            continue
        model = cloud if side == "cloud" else local
        if model:
            return Choice(step, value, side, model, cloud, local)
    return Choice(step, value, None, None, cloud, local)


def describe(conn) -> list[dict]:
    """Every step for the Settings view: its label, setting, the options it allows and the model it runs on now,
    from the same ``choose`` the flow runs on (so "no model" is shown only when the step would really stop)."""
    assert conn is not None, "conn required"
    from .ni_research import policy  # local: policy imports this module

    routes = gateway.load_routes(conn)
    research_off = policy.setting(conn) == "off"   # env SMARTBRAIN_NI_RESEARCH or meta ni_research
    out = []
    for step in STEPS:  # bounded to 4
        c = choose(conn, step, routes)
        off = step == "research" and research_off
        out.append({"step": step, "label": LABELS[step], "setting": c.setting, "where": None if off else c.where,
                    "model": None if off else c.model, "off": off, "fallback": bool(c.fallback and not off),
                    "options": [s for s in SETTINGS if not (step in LOCAL_ONLY and s == "cloud")]})
    return out


def record(conn, step: str, model: str, response: object) -> None:
    """Best-effort: record one step's call in the cost view under its step's purpose (never fails the call)."""
    assert step in STEPS, f"unknown NI step {step!r}"
    from . import usage  # local import: usage stays free of NI at import time

    usage.record_response(conn, model, response, purpose=PURPOSES[step])
