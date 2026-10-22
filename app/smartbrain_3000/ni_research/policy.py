"""Which model runs the research (plan 6.3, decision D1, tiered). Pure choice; the flow acts on it.

(a) cloud: an Anthropic route is configured AND this card has research consent.
(b) the Claude Code CLI route is DEFERRED. TODO(T): check the CLI terms for automated research before adding a
    tier here; today ``claudecode/*`` is never chosen for research (``cloud_route`` skips it).
(c) closed-world local: code ranks Library rows, the local model only picks one and fills the recipe.

Open web (S5b): the cloud tier may also search the web and read public pages, unless the kill switch (env
``SMARTBRAIN_NI_RESEARCH_WEB`` or meta ``ni_research_web`` set to ``off``) limits research to the Library. The
closed-world local tier never has the web, whatever the switch says.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .. import db, gateway

SETTINGS = ("auto", "cloud", "local", "off")
MODES = ("cloud", "local", "ask", "off")
_ENV_SETTING = "SMARTBRAIN_NI_RESEARCH"
_ENV_MODEL = "SMARTBRAIN_NI_RESEARCH_MODEL"
_ENV_WEB = "SMARTBRAIN_NI_RESEARCH_WEB"


@dataclass(frozen=True)
class Plan:
    """``mode``: cloud | local | ask (needs the card's consent first) | off. ``model``: the cloud id for cloud/ask,
    the local id for local. ``web``: the cloud tier may search and read the open web (never true for local/off)."""

    mode: str
    model: str | None = None
    local: str | None = None
    web: bool = False


def setting(conn) -> str:
    """The research setting: env ``SMARTBRAIN_NI_RESEARCH``, then meta ``ni_research``; default ``auto``."""
    assert SETTINGS[0] == "auto", "auto is the default"
    assert conn is not None, "conn required"
    raw = os.environ.get(_ENV_SETTING)
    if raw is None or not raw.strip():
        raw = db.meta_get(conn, "ni_research")
    value = str(raw or "").strip().lower()
    return value if value in SETTINGS else "auto"


def web_enabled(conn) -> bool:
    """The open-web kill switch: env ``SMARTBRAIN_NI_RESEARCH_WEB``, then meta ``ni_research_web``. Only ``off``
    (also ``0``, ``false``, ``no``) limits research to the Library; anything else, or nothing, leaves the web on."""
    assert _ENV_WEB, "the env name"
    assert conn is not None, "conn required"
    raw = os.environ.get(_ENV_WEB)
    if raw is None or not raw.strip():
        raw = db.meta_get(conn, "ni_research_web")
    return str(raw or "").strip().lower() not in ("off", "0", "false", "no")


def cloud_route(conn) -> str | None:
    """The Anthropic route research would use: env or meta ``ni_research_model``, else the first ``anthropic/*``
    on the ni / chat / agent routes. Never a CLI route and never a local one."""
    assert _ENV_MODEL, "the env name"
    assert conn is not None, "conn required"
    explicit = os.environ.get(_ENV_MODEL) or db.meta_get(conn, "ni_research_model")
    routes = gateway.load_routes(conn)
    for model in (explicit, *(gateway.resolve_model(c, routes) for c in ("ni", "chat", "agent"))):  # bounded: 4
        if isinstance(model, str) and model.split("/", 1)[0] == "anthropic":
            return model
    return None


def choose(setting_: str, cloud: str | None, local: str | None, consent: str | None, use_local: bool,
           web: bool = True) -> Plan:
    """The plan from the setting, the routes, this card's consent (the model id it allowed) and its local choice.
    ``web`` is the kill switch's answer; it rides only on the cloud and ask plans."""
    assert setting_ in SETTINGS, "a known setting"
    assert consent is None or isinstance(consent, str), "consent is a model id or None"
    if setting_ == "off" or (setting_ == "local" and not local) or (setting_ == "cloud" and not cloud):
        return Plan("off")
    if setting_ == "local" or not cloud:
        return Plan("local", local, local) if local else Plan("off")
    if use_local and local:  # the card's own local choice wins, as in ``run_flow``
        return Plan("local", local, local)
    if consent == cloud:
        return Plan("cloud", cloud, local, web=bool(web))
    return Plan("ask", cloud, local, web=bool(web))
