"""Which model each Neural Interface step runs on: the one place every NI model call site asks (pure choice).

A step is a kind of model work, not a feature: ``build`` (reading the ask, planning the query, writing the
recipe), ``research`` (finding a source), ``refresh`` (a model-source card's repeated answer) and ``summary``
(the refresh-time llm stage, a brief's summary). Verification and compare are code and have no step.

Each step has a setting, ``auto`` (the default), ``cloud`` or ``local``. ``auto`` follows the step's kind: the
one-time work (build, research) runs on the cloud model when one is routed, the repeated work (refresh, summary)
runs on the local model. ``summary`` is local-only whatever is stored: a sealed ``local/auto`` card never resolves
to cloud. Choosing a step's model never grants consent: a cloud model still needs the card's own consent wherever
the call site asked for it before.

The cloud model is env ``SMARTBRAIN_NI_RESEARCH_MODEL`` or meta ``ni_research_model``, else the first
``anthropic/*`` on the ni / chat / agent routes (never a CLI route, never a local one): one cloud model for both
one-time steps, so the consent a build asks for covers its research too. The local model is the first local one on
the same routes, resolved at call time.

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
    ``cloud`` / ``local``: what each side resolves to now, so a caller can fall back (or ask) without re-reading."""

    step: str
    setting: str
    where: str | None
    model: str | None
    cloud: str | None
    local: str | None


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


def save(conn, changes: dict) -> dict[str, str]:
    """Merge ``{step: setting}`` into the saved map and return every step's setting. Raises ValueError on an unknown
    step or setting, or ``cloud`` for a local-only step (refused, never silently stored)."""
    assert conn is not None, "conn required"
    assert isinstance(changes, dict), "changes must be a mapping"
    merged = stored(conn)
    for step, value in changes.items():  # bounded by the caller's body (at most len(STEPS) known keys)
        if step not in STEPS:
            raise ValueError(f"unknown NI step {step!r}")
        if value not in SETTINGS:
            raise ValueError(f"{step}: setting must be one of {', '.join(SETTINGS)}")
        if step in LOCAL_ONLY and value == "cloud":
            raise ValueError(f"{step} runs on this computer only")
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


def cloud_model(conn, routes: dict | None = None, gw=gateway) -> str | None:
    """The one cloud model the one-time steps use: env or meta ``ni_research_model``, else the first
    ``anthropic/*`` on the ni / chat / agent routes. Never a CLI route and never a local one."""
    assert conn is not None, "conn required"
    explicit = os.environ.get(_ENV_CLOUD) or db.meta_get(conn, "ni_research_model")
    table = routes if routes is not None else gw.load_routes(conn)
    for model in (explicit, *(gw.resolve_model(c, table) for c in _CAPABILITIES)):  # bounded: 4
        if isinstance(model, str) and model.split("/", 1)[0] == "anthropic" and not gw.is_local(model):
            return model
    return None


def choose(conn, step: str, routes: dict | None = None, gw=gateway) -> Choice:
    """The model ``step`` runs on now. ``cloud`` / ``local`` settings with nothing routed on that side give
    ``where=None`` (the caller decides how to stop); ``auto`` falls back to the other side when its own is
    missing, except a local-only step, which never has a cloud side."""
    assert step in STEPS, f"unknown NI step {step!r}"
    assert conn is not None, "conn required"
    table = routes if routes is not None else gw.load_routes(conn)
    value = setting(conn, step)
    local = local_model(conn, table, gw)
    cloud = None if step in LOCAL_ONLY else cloud_model(conn, table, gw)
    if value == "auto":
        value_side = "cloud" if step in ONE_TIME else "local"
        order = (value_side, "local" if value_side == "cloud" else "cloud")
    else:
        order = (value,)
    for side in order:  # bounded to 2
        model = cloud if side == "cloud" else local
        if model:
            return Choice(step, value, side, model, cloud, local)
    return Choice(step, value, None, None, cloud, local)


def describe(conn) -> list[dict]:
    """Every step for the Settings view: its label, setting, the options it allows and the model it runs on now."""
    assert conn is not None, "conn required"
    routes = gateway.load_routes(conn)
    out = []
    for step in STEPS:  # bounded to 4
        c = choose(conn, step, routes)
        out.append({"step": step, "label": LABELS[step], "setting": c.setting, "where": c.where, "model": c.model,
                    "options": [s for s in SETTINGS if not (step in LOCAL_ONLY and s == "cloud")]})
    return out


def record(conn, step: str, model: str, response: object) -> None:
    """Best-effort: record one step's call in the cost view under its step's purpose (never fails the call)."""
    assert step in STEPS, f"unknown NI step {step!r}"
    from . import usage  # local import: usage stays free of NI at import time

    usage.record_response(conn, model, response, purpose=PURPOSES[step])
