"""v2 glue: run the researcher before the flow's honest stop and land its proposal as a held card (S4c, S5b).

``fallback`` is called by ``ni_flow`` when the auto-pick finds no buildable Library row, or every row failed.
It picks the model tier (``ni_research.policy``), runs the bounded loop with Library-only provenance and, on a
proposal, lands the card exactly like an auto-pick: ``_built_from.path`` is the closed value ``research`` (kept
apart from ``auto`` so the board and the eval can tell a researched recipe from a declared-answers build; both
are open paths, so a preview shows and nothing recurs until Keep, whatever the source). The interval is stamped
by code from ``recipe.question.cadence``. Fetches are Library rows only, unless the plan carries the open web
(cloud tier, kill switch not off): then also exact search results and plain addresses on a found site (S5b,
``ResearchCtx.origin``). No keys, no redirects either way; the local closed-world tier never has the web.
"""
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from datetime import UTC
from zoneinfo import ZoneInfo

from . import gateway, ni, ni_flow, ni_recipe
from .ni_research import loop, policy
from .ni_research.ctx import ResearchCtx, normal_url, one_line

log = logging.getLogger(__name__)

# the model seam: (model, messages, tools or None) -> an OpenAI-shaped response. None = the real gateway.
_CHAT: Callable[[str, list, list | None], dict] | None = None
_SEAM: dict = {"cloud": None, "local": None, "consented": False}
# the web-search seam: (query, limit) -> {"results": [...]}. None = the app's own service (``ni_flow``'s provider).
_SEARCH: Callable[[str, int], dict] | None = None
RESEARCH_TIMEOUT_S = 120.0
RESEARCH_MAX_TOKENS = 4096
ORIGINS_LIBRARY = frozenset({"library"})
ORIGINS_WEB = frozenset({"library", "search", "preview", "same_domain"})


def set_research_chat(chat: Callable | None, *, cloud: str | None = None, local: str | None = None,
                      consented: bool = False) -> None:
    """Install a model seam (the eval's cassettes, tests). ``cloud`` / ``local`` name the routes it stands for;
    ``consented`` is the caller's own word that the cloud route may be used (the eval operator), never a default."""
    assert isinstance(_SEAM, dict), "a seam table"
    global _CHAT
    assert chat is None or callable(chat), "chat must be callable"
    _CHAT = chat
    _SEAM.update(cloud=cloud, local=local, consented=bool(consented and chat is not None))


def set_research_search(search: Callable[[str, int], dict] | None) -> None:
    """Install a web-search seam (the eval's cassettes, tests); None goes back to the app's search service."""
    global _SEARCH
    assert search is None or callable(search), "search must be callable"
    _SEARCH = search


def _searcher() -> Callable[[str, int], dict] | None:
    """The search function a web run uses: the installed seam, else the app's search service (``None`` when the
    app wired none, which makes ``web_search`` answer a typed error)."""
    assert _SEARCH is None or callable(_SEARCH), "a callable seam"
    if _SEARCH is not None:
        return _SEARCH
    service = ni_flow._resolve_search_service()
    return (lambda query, limit: service.search(query, limit=limit)) if service is not None else None  # type: ignore[attr-defined]


def question_from_intent(request: str, intent: dict) -> dict:
    """The ask as ``recipe.question``: kind, subject, place and cadence from the understood intent. The window,
    threshold, compare and units constraints are read by the verifier from the user's words, not copied here."""
    assert isinstance(request, str) and isinstance(intent, dict), "args required"
    kind = intent.get("frame_kind")
    place = intent.get("place")
    minutes = intent.get("cadence_minutes")
    minutes = minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else 15
    cadence = next(name for name, cap in (("realtime", 30), ("hourly", 120), ("daily", 2880), ("weekly", 10**9))
                   if minutes <= cap)
    assert cadence in ni_recipe.CADENCE_MINUTES, "a closed cadence"
    return {"kind": kind if kind in ni_recipe.QUESTION_KINDS else "lookup",
            "subject": one_line(intent.get("subject") or request, 120),
            "places": [one_line(place, 80)] if isinstance(place, str) and place.strip() else [],
            "entities": [], "window": None, "threshold": None, "order": [], "limit": None,
            "compare_count": 0, "units": None, "cadence": cadence}


class _Library:
    """The installed Library for the researcher: the intent's frame rides as the hint, a source the user said NO
    to on this card is never offered, everything else (resolvers) passes through."""

    def __init__(self, lib: object, hint: dict, declined: set) -> None:
        assert isinstance(hint, dict), "a hint"
        self._lib, self._hint, self._declined = lib, hint, declined

    def candidates(self, ask: str, limit: int = 3):
        assert limit > 0, "a positive limit"
        rows, skipped = self._lib.candidates(ask, limit=limit, hint=self._hint)
        return [r for r in rows if r.get("url") not in self._declined], skipped

    def __getattr__(self, name: str):
        assert name, "a name"
        return getattr(self._lib, name)


def _stamp(spec: dict, request: str) -> dict:
    """The researcher's spec with the code-owned fields: the goal is the user's words and the interval comes
    from the question's cadence (never the model's)."""
    assert isinstance(spec, dict), "spec required"
    out = copy.deepcopy(spec)
    out["goal"] = request[:ni._MAX_GOAL]
    out["interval_minutes"] = ni_recipe.cadence_minutes(out)
    out.setdefault("repair_policy", {"l1": True, "l2_frontier": False})
    assert out["interval_minutes"] is not None, "the question carries a cadence"
    return out


def _preview_of(ctx: ResearchCtx, spec: dict) -> dict:
    """The full pipeline outputs on the stored sample (the bounded preview the model saw is not the card's data)."""
    assert isinstance(spec, dict), "spec required"
    sample = ctx.samples.get(normal_url(str(spec["source"]["url"])))
    assert sample is not None, "propose_card ran on a stored sample"
    return ni.run_pipeline(copy.deepcopy(spec["pipeline"]), copy.deepcopy(sample["parsed"]))


def _can_land(ctx: ResearchCtx, item_id: str, request: str, proposal: dict) -> str | None:
    """Why this proposal cannot become a card (the whole spec validates and the scene binds the real outputs),
    else None. A failure goes back to the model as a failed proposal."""
    assert isinstance(request, str) and item_id, "args required"
    assert isinstance(proposal, dict), "a proposal"
    try:
        spec = _stamp(proposal["spec"], request)
        ni.validate_spec(spec)
        preview = _preview_of(ctx, spec)
        ni.bind_scene(spec["scene"], preview, history=ni._seed_history(spec),
                      image_ref=ni._preview_image_ref(spec, item_id),
                      form_ctx=ni._form_bind_context(spec, ctx.now), alternatives=True)
    except (ni.NIError, ValueError, KeyError, AssertionError) as exc:
        return f"the card cannot be shown: {one_line(exc, 150)}"
    return None


def _land(store: ni.NIStore, item_id: str, request: str, ctx: ResearchCtx, proposal: dict, out: loop.Outcome) -> dict:
    """Land the proposal as a held card (preview, nothing recurs until Keep), noting how it was built."""
    assert isinstance(proposal, dict), "a proposal"
    assert store is not None and item_id, "args required"
    spec = _stamp(proposal["spec"], request)
    preview = _preview_of(ctx, spec)
    url = str(spec["source"]["url"])
    title = (ctx.urls.get(normal_url(url)) or {}).get("title") or ""
    seen = f"{ctx.fetches} fetches" + (f", {ctx.searches} searches" if ctx.searches else "")
    note = f"researched ({out.mode}): {out.usage['calls']} model calls, {seen}, tier {proposal['tier']}; held for your Keep"
    ni_flow._transition(store, item_id, "assembling", source_url=url, note=note)
    ni_flow._try_journal(store, item_id, "updated", note)
    return ni_flow._finalize(store, item_id, spec, preview, note=note, born="flow", fetched_at=ctx.now,
                             built_from=ni_flow._built_from_of(store, item_id, "research", url, title))


def _chat_for(store: ni.NIStore, model: str, local: bool) -> Callable[[list, list | None], dict]:
    """The model seam bound to ``model``: the installed one, else the gateway's recorded path (tool use for the
    cloud loop, a plain completion for the closed-world local mode)."""
    assert isinstance(local, bool), "local is a flag"
    assert model, "a model id"
    if _CHAT is not None:
        return lambda messages, tools_: _CHAT(model, messages, tools_)
    if local:
        return lambda messages, tools_: gateway.chat_recorded(
            store.conn, "ni_research", messages, model, timeout=ni_flow._FLOW_MODEL_TIMEOUT_S, temperature=0.0)
    return lambda messages, tools_: gateway.chat_with_tools_recorded(
        store.conn, "ni_research", messages, model, tools_, timeout=RESEARCH_TIMEOUT_S, max_tokens=RESEARCH_MAX_TOKENS)


def _audit(item_id: str, plan: policy.Plan) -> bool:
    """Record one research run (``ni_research``: item, tier, model) in the audit log. Best effort, like the
    auto-pick record: a locked or unwired log never stops the build. True when a row was written."""
    assert isinstance(plan, policy.Plan), "a plan"
    assert item_id and plan.mode in ("cloud", "local"), "a researching plan"
    assert plan.mode == "cloud" or not plan.web, "only the cloud tier has the web"
    try:
        log_ = ni_flow._AUDIT_PROVIDER() if ni_flow._AUDIT_PROVIDER is not None else None
        if log_ is None:
            return False
        log_.append("assistant", "ni_research", "reviewed", "auto", True,
                    args_summary=json.dumps({"item_id": item_id, "mode": plan.mode, "model": plan.model,
                                                 "web": plan.web}))
        return True
    except Exception as exc:  # the audit row is a record, never a gate
        log.warning("ni_research: audit failed: %s", type(exc).__name__)
        return False


def plan_for(store: ni.NIStore, item_id: str, record: dict) -> policy.Plan:
    """The research tier for this card: the setting, the routes, and the consent the card itself holds."""
    assert isinstance(record, dict), "a record"
    assert store is not None and item_id, "args required"
    cloud = _SEAM["cloud"] if _CHAT is not None else policy.cloud_route(store.conn)
    local = _SEAM["local"] if _CHAT is not None else ni_flow._local_flow_model(store)
    consent = cloud if _SEAM["consented"] and cloud else ni_flow._model_consent_of(store, item_id, record)
    return policy.choose(policy.setting(store.conn), cloud, local, consent, bool(record.get("_use_local")),
                         web=policy.web_enabled(store.conn))


def fallback(store: ni.NIStore, item_id: str, request: str, intent: dict) -> dict | None:
    """Research before the honest stop. The landed (held) card, or the consent question, as the flow record;
    None when research is off, unavailable or declined (the caller then makes the honest stop, with the reasons
    appended to its note)."""
    assert isinstance(intent, dict) and request, "intent and request required"
    assert store is not None and item_id, "args required"
    record = ni_flow._flow_read(store, item_id) or {}
    lib = ni_flow._resolve_library()
    if not ni_flow._is_v2(store) or record.get("_cancel") or lib is None:
        return None
    plan = plan_for(store, item_id, record)
    if plan.mode == "off":
        return None
    if plan.mode == "ask":
        return ni_flow._ask_model_consent(store, item_id, plan.model or "", plan.local, research=True)
    now = ni._clock().astimezone(UTC)
    web = plan.mode == "cloud" and plan.web  # the closed-world local tier never has the web
    ctx = ResearchCtx(request=request, question=question_from_intent(request, intent), now=now,
                      zone=ZoneInfo(ni.user_timezone_name()), allowed_origins=ORIGINS_WEB if web else ORIGINS_LIBRARY,
                      searcher=_searcher() if web else None,
                      library=_Library(lib, ni_flow._library_hint(intent), set(record.get("_declined") or [])))
    ni_flow._transition(store, item_id, record.get("state") or "source", error=record.get("error"),
                        note=f"researching ({plan.mode}) for a Library source" if not web
                        else "researching (cloud) in the Library and on the web")
    _audit(item_id, plan)
    chat = _chat_for(store, plan.model or "", plan.mode == "local")
    accept = lambda proposal: _can_land(ctx, item_id, request, proposal)
    place = str(intent.get("place") or "")
    out = (loop.run_research(ctx, chat, place=place, accept=accept) if plan.mode == "cloud"
           else loop.run_closed_world(ctx, chat, accept=accept))
    if out.status == "proposed" and out.proposal is not None:
        return _land(store, item_id, request, ctx, out.proposal, out)
    ni_flow._append_note(store, item_id, one_line("researched, no card: " + "; ".join(out.reasons), 240))
    return None
