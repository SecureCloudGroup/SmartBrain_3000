"""v2 glue: run the researcher before the flow's honest stop and land its proposal as a held card (S4c, S5b).

``fallback`` is called by ``ni_flow`` when the auto-pick finds no buildable Library row, or every row failed.
It picks the model tier (``ni_research.policy``), runs the bounded loop with Library-only provenance and, on a
proposal, lands the card exactly like an auto-pick: ``_built_from.path`` is the closed value ``research`` (kept
apart from ``auto`` so the board and the eval can tell a researched recipe from a declared-answers build; both
are open paths, so a preview shows and nothing recurs until Keep, whatever the source). The interval is stamped
by code from ``recipe.question.cadence``. Fetches are Library rows only, unless the plan carries the open web
(cloud tier, kill switch not off, consent given under the web wording): then also exact search results (S5b,
``ResearchCtx.origin``). No keys, no redirects either way; the local closed-world tier never has the web.

``reresearch`` (S9) runs the same loop again for a kept card whose source drifted or died, Library only, and PARKS
the result as a proposal on the card (``_l2_proposal`` with a whole replacement ``spec``); it is never applied by code.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import gateway, ni, ni_compare, ni_flow, ni_recipe, ni_refresh
from .ni_research import loop, policy, tools
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
ORIGINS_WEB = frozenset({"library", "search", "preview"})


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
    things = ni_compare.asked(request)[: ni_compare.MAX_ENTITIES]   # S7: the things a compare ask names, as written
    return {"kind": "compare" if things else kind if kind in ni_recipe.QUESTION_KINDS else "lookup",
            "subject": one_line(intent.get("subject") or request, 120),
            "places": [one_line(place, 80)] if not things and isinstance(place, str) and place.strip() else [],
            "entities": [one_line(t, 80) for t in things], "window": None, "threshold": None, "order": [],
            "limit": None, "compare_count": len(things), "units": None, "cadence": cadence}


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


def _window_of(request: str, intent: dict) -> str | None:
    """The forward window the ask states (``now``, ``today``...), for the page-reading gate: a right-now ask needs a
    page that shows it is fresh. Read by the same recognizers the page door uses; None when the ask names none."""
    assert isinstance(request, str) and isinstance(intent, dict), "args required"
    names = [n for n in intent.get("names") or [] if isinstance(n, str)]
    return ni_flow._ask_window(request, intent.get("frame_kind"), names)


def _stamp(spec: dict, request: str) -> dict:
    """The researcher's spec with the code-owned fields: the goal is the user's words and the interval comes
    from the question's cadence (never the model's). One rule, shared with the check the researcher runs."""
    assert isinstance(spec, dict), "spec required"
    return tools.stamp(spec, request)


def _preview_of(ctx: ResearchCtx, spec: dict) -> dict:
    """The full pipeline outputs on the stored sample (the bounded preview the model saw is not the card's data)."""
    assert isinstance(spec, dict), "spec required"
    if ni_compare.is_compare(spec):   # S7: one stored sample per compared thing
        things = ni_compare.sealed(spec)
        values = [ni_compare.number_at(ctx.samples[normal_url(t["url"])]["parsed"], t["path"]) for t in things]
        assert all(v is not None for v in values), "propose_compare ran on stored samples"
        return ni.run_pipeline(copy.deepcopy(spec["pipeline"]), ni_compare.payload_of(things, values))
    sample = ctx.samples.get(normal_url(str(spec["source"]["url"])))
    assert sample is not None, "propose_card ran on a stored sample"
    return ni.run_pipeline(copy.deepcopy(spec["pipeline"]), copy.deepcopy(tools.spec_input(sample, spec["source"])))


def _can_land(ctx: ResearchCtx, item_id: str, request: str, proposal: dict) -> str | None:
    """Why this proposal cannot become a card (the whole spec validates and the scene binds the real outputs),
    else None. A failure goes back to the model as a failed proposal. The same rule ``try_recipe`` and
    ``propose_card`` already ran on the sample (``tools.landing_reason``); here it runs on the sealed proposal, with
    the card's real id, as the backstop."""
    assert isinstance(request, str) and item_id, "args required"
    assert isinstance(proposal, dict), "a proposal"
    try:
        spec = _stamp(proposal["spec"], request)
        ni.validate_spec(spec)
        preview = _preview_of(ctx, spec)
    except (ni.NIError, ValueError, KeyError, AssertionError) as exc:
        return f"the card cannot be shown: {one_line(exc, 150)}"
    return tools.landing_reason(ctx, spec, preview, item_id)


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


def plan_for(store: ni.NIStore, item_id: str, record: dict, web: bool | None = None) -> policy.Plan:
    """The research tier for this card: the setting, the routes, and the consent the card itself holds. ``web``
    False plans a Library-only run whatever the switch says (a Library consent then suffices); None reads the switch."""
    assert isinstance(record, dict), "a record"
    assert store is not None and item_id, "args required"
    cloud = _SEAM["cloud"] if _CHAT is not None else policy.cloud_route(store.conn)
    local = _SEAM["local"] if _CHAT is not None else ni_flow._local_flow_model(store)
    seam = bool(_SEAM["consented"] and cloud)
    consent = cloud if seam else ni_flow._model_consent_of(store, item_id, record)
    consent_web = seam or ni_flow._model_consent_web_of(store, item_id, record)
    return policy.choose(policy.setting(store.conn), cloud, local, consent, bool(record.get("_use_local")),
                         web=policy.web_enabled(store.conn) if web is None else bool(web), consent_web=consent_web)


def fallback(store: ni.NIStore, item_id: str, request: str, intent: dict) -> dict | None:
    """Research before the honest stop. The landed (held) card, the consent question, or (a run that ended without a
    card) the declined card, as the flow record; None when research is off, unavailable, or the card was cancelled
    (the caller then ends the ask itself). A run that could not finish (a fault) is the failed card, retryable,
    never a declined one."""
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
    question = question_from_intent(request, intent)
    # a compare is searched one thing at a time: the intent's one place must not steer every search
    hint = {**ni_flow._library_hint(intent), **({"place": None} if question["entities"] else {})}
    ctx = ResearchCtx(request=request, question=question, now=now, window=_window_of(request, intent),
                      zone=ZoneInfo(ni.user_timezone_name()), allowed_origins=ORIGINS_WEB if web else ORIGINS_LIBRARY,
                      searcher=_searcher() if web else None, library=_Library(lib, hint, set(record.get("_declined") or [])))
    ni_flow._transition(store, item_id, record.get("state") or "source", error=record.get("error"),
                        note=f"researching ({plan.mode}) for a Library source" if not web
                        else "researching (cloud) in the Library and on the web")
    _audit(item_id, plan)
    chat = _chat_for(store, plan.model or "", plan.mode == "local")
    accept = lambda proposal: _can_land(ctx, item_id, request, proposal)
    place = "" if question["entities"] else str(intent.get("place") or "")
    out = (loop.run_research(ctx, chat, place=place, accept=accept) if plan.mode == "cloud"
           else loop.run_closed_world(ctx, chat, accept=accept))
    if out.status == "proposed" and out.proposal is not None:
        return _land(store, item_id, request, ctx, out.proposal, out)
    if out.status == "fault":  # the run could not finish: a fault the user retries, never a verdict about the ask
        return ni_flow._fail(store, item_id, "research", one_line("; ".join(out.reasons), 160))
    ni_flow._append_note(store, item_id, one_line("researched, no card: " + "; ".join(out.reasons), 240))
    # the run ended without a card: that is the honest declined card (a closed code, up to three links the run saw),
    # not a stop on links. A compare that no source built says so; one the verifier refused says that.
    ending = out.ending or "research_declined_budget"
    if question["entities"] and ending != "research_declined_checks":
        ending = "compare_unbuildable"
    return ni_flow._end(store, item_id, ending, "", links=out.links)


def research_plan(store: ni.NIStore, item: dict) -> policy.Plan | None:
    """The plan a re-research of this card would run on, or None. Only the cloud tier with the consent this
    card already holds (``_model_consent``) re-researches: off, ask, local and a missing Library all read None,
    and the card simply stays failing with its own status."""
    assert store is not None and isinstance(item, dict), "store + item required"
    assert item.get("id"), "an item id"
    if not ni_flow._is_v2(store) or ni_flow._resolve_library() is None:
        return None
    plan = plan_for(store, item["id"], {}, web=False)  # re-research reads the Library only: any consent covers it
    return plan if plan.mode == "cloud" else None


def _sources_of(spec: dict) -> set:
    """Every address this card read or recorded, raw and normalised: a replacement must be a different source."""
    assert isinstance(spec, dict), "spec required"
    assert normal_url("https://a.example/x#y") == "https://a.example/x", "the normaliser drops fragments"
    urls = [str((spec.get("source") or {}).get("url") or "")]
    urls += [str(s.get("ref") or "") for s in (spec.get("recipe") or {}).get("sources") or []]
    return {u for url in urls if url for u in (url, normal_url(url))}


def _park(store: ni.NIStore, item: dict, ctx: ResearchCtx, proposal: dict, reason: str, plan: policy.Plan) -> bool:
    """Seal the researcher's card as the parked proposal on ``item`` (the card's own consent, birth marker and
    repair policy ride along). False when it cannot be parked: invalid, the same source, or the card moved."""
    assert reason in ni.RESEARCH_REASONS, "a closed reason"
    assert isinstance(proposal, dict), "a proposal"
    spec = item["spec"]
    new = _stamp(proposal["spec"], str(spec.get("goal") or ""))
    url = str(new["source"]["url"])
    if normal_url(url) in _sources_of(spec):
        return False
    new["repair_policy"] = spec.get("repair_policy") or new["repair_policy"]
    for key in ("_model_consent", "_model_consent_web", "_born"):  # bounded: what the card keeps across a new source
        if spec.get(key) is not None:
            new[key] = spec[key]
    new["_built_from"] = ni_flow._built_from_of(store, item["id"], "research", url,
                                                (ctx.urls.get(normal_url(url)) or {}).get("title") or "")
    parked = {"spec": new, "origin": "research", "reason": reason, "created_at": datetime.now(UTC).isoformat(),
              "model": plan.model or "research"}
    try:
        ni._validate_l2_proposal(parked)
    except ValueError:
        return False
    return store.set_l2_proposal(item["id"], parked, expected_rev=int(item["spec_rev"]),
                                 expected_first_failure_at=item.get("first_failure_at"),
                                 states=("degraded", "failing"))


def reresearch(store: ni.NIStore, item: dict, reason: str) -> dict | None:
    """Re-run the researcher for this kept card's recorded question and park what it proposes. The carrier
    notice on a parked proposal; None when research is off or unconsented (nothing recorded, nothing sent) or
    when it declined or could not park (recorded as an attempt, journaled, the card stays failing). The source
    the card has now is never offered back."""
    assert store is not None and isinstance(item, dict), "store + item required"
    assert reason in ni.RESEARCH_REASONS, "a closed reason"
    plan = research_plan(store, item)
    spec, request = item["spec"], str(item["spec"].get("goal") or "")
    if plan is None or not request or not isinstance(spec.get("recipe"), dict):
        return None
    question = copy.deepcopy(spec["recipe"]["question"])
    place = question["places"][0] if question.get("places") else ""
    started = time.monotonic()
    now = datetime.now(UTC)
    if not ni_refresh.take_budget(store.conn, now):  # the process-wide daily ceiling: nothing is sent past it
        return None
    first = item.get("first_failure_at")
    # the durable bound (M1): stamped on the sealed spec BEFORE the model call, so a crash still counts and the
    # pruned ni_runs ring is not what holds it
    store.set_research_attempt(item["id"], ni_refresh.next_marker(spec.get("_research_last_attempt"), first, now, "started"))
    store.record_run(item["id"], ni_refresh.RUN_STARTED, duration_ms=0, error=reason, contract_ok=None)
    hint = {"subject": question.get("subject"), "wants": [], "place": place or None, "frame_kind": question.get("kind")}
    ctx = ResearchCtx(request=request, question=question, now=ni._clock().astimezone(UTC),
                      zone=ZoneInfo(ni.user_timezone_name()), allowed_origins=frozenset({"library"}),
                      library=_Library(ni_flow._resolve_library(), ni_flow._library_hint(hint), _sources_of(spec)))
    _audit(item["id"], plan)
    accept = lambda proposal: _can_land(ctx, item["id"], request, proposal)
    out = loop.run_research(ctx, _chat_for(store, plan.model or "", False), place=place, accept=accept)
    if out.status == "fault":  # not a decline: no streak, no longer wait; the card keeps failing and tries again in a day
        ni_flow._try_journal(store, item["id"], "updated",
                             one_line("re-research could not finish: " + "; ".join(out.reasons), 240))
        store.record_run(item["id"], ni_refresh.RUN_FAULT, duration_ms=int((time.monotonic() - started) * 1000),
                         error="fault", contract_ok=None)
        return None
    parked = out.status == "proposed" and out.proposal is not None and _park(store, item, ctx, out.proposal, reason, plan)
    note = ("re-researched: a replacement source is parked for your approval" if parked
            else one_line("re-researched, no safe replacement: " + "; ".join(out.reasons or ["could not be parked"]), 240))
    ni_flow._try_journal(store, item["id"], "updated", note)
    fresh = store.get_item(item["id"])
    store.set_research_attempt(item["id"], ni_refresh.next_marker(
        ((fresh or item)["spec"]).get("_research_last_attempt"), first, datetime.now(UTC),
        "proposed" if parked else "declined"))
    store.record_run(item["id"], ni_refresh.RUN_PROPOSED if parked else ni_refresh.RUN_DECLINED,
                     duration_ms=int((time.monotonic() - started) * 1000), error=None if parked else "declined",
                     contract_ok=None)
    return {"item_id": item["id"], "title": str(spec.get("title") or "")} if parked else None
