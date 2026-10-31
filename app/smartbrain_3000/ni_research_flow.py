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

import contextlib
import copy
import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import (
    claudecli,
    gateway,
    ni,
    ni_compare,
    ni_flow,
    ni_master,
    ni_models,
    ni_recipe,
    ni_refresh,
)
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
    to on this card is never offered, nor a source that needs the key of a site the user skipped on this build
    (``no_key``: Skip this source says the user has no key for that site), everything else (resolvers) passes
    through."""

    def __init__(self, lib: object, hint: dict, declined: set, no_key: frozenset = frozenset()) -> None:
        assert isinstance(hint, dict), "a hint"
        assert isinstance(no_key, frozenset), "the skipped sites"
        self._lib, self._hint, self._declined, self._no_key = lib, hint, declined, no_key

    def candidates(self, ask: str, limit: int = 3):
        assert limit > 0, "a positive limit"
        rows, skipped = self._lib.candidates(ask, limit=limit, hint=self._hint)
        # a skipped site's keyed rows would only ask again for the key the user said they don't have: as a read
        # the run can't make, as Use my key, and as the decline's links (2026-10-31, T's first research run)
        return [r for r in rows if r.get("url") not in self._declined
                and not (r.get("needs_key")  # the run's own test of a keyed row (``tools.library_search``)
                         and (urlsplit(str(r.get("url") or "")).hostname or "").lower() in self._no_key)], skipped

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


def _declined_of(record: dict) -> frozenset[str]:
    """The addresses the user said NO to on this card (``_declined``, bounded by the flow), for set membership."""
    out: set[str] = set()
    for url in record.get("_declined") or []:  # bounded: ni_flow keeps the last _MAX_DECLINED
        try:
            if isinstance(url, str):
                out.add(normal_url(url))
        except ValueError:  # not an address (a bad port): nothing to avoid
            continue
    return frozenset(out)


def _can_land(ctx: ResearchCtx, item_id: str, request: str, proposal: dict,
              declined: frozenset[str] = frozenset()) -> str | None:
    """Why this proposal cannot become a card (the whole spec validates and the scene binds the real outputs),
    else None. A failure goes back to the model as a failed proposal. The same rule ``try_recipe`` and
    ``propose_card`` already ran on the sample (``tools.landing_reason``); here it runs on the sealed proposal, with
    the card's real id, as the backstop. A source the user said NO to on this card never lands again, whichever
    way the run found it (the Library hides it; a search or a page link can still name it)."""
    assert isinstance(request, str) and item_id, "args required"
    assert isinstance(proposal, dict), "a proposal"
    try:
        spec = _stamp(proposal["spec"], request)
        ni.validate_spec(spec)
        if normal_url(str(spec["source"].get("url") or "")) in declined:
            return "you said no to this source on this card: find a different one"
        preview = _preview_of(ctx, spec)
    except (ni.NIError, ValueError, KeyError, AssertionError) as exc:
        return f"the card cannot be shown: {one_line(exc, 150)}"
    return tools.landing_reason(ctx, spec, preview, item_id)


def _land(store: ni.NIStore, item_id: str, request: str, ctx: ResearchCtx, proposal: dict, out: loop.Outcome,
          note: str | None = None, path: str = "research") -> dict:
    """Land the proposal as a held card (preview, nothing recurs until Keep), noting how it was built (``note``: the
    caller's own words for it, as a kept source's reuse says; ``path``: the sealed ``_built_from.path``, ``kept`` for
    a source the user kept before)."""
    assert isinstance(proposal, dict), "a proposal"
    assert store is not None and item_id, "args required"
    spec = _stamp(proposal["spec"], request)
    preview = _preview_of(ctx, spec)
    url = str(spec["source"]["url"])
    title = (ctx.urls.get(normal_url(url)) or {}).get("title") or ""
    seen = f"{ctx.fetches} fetches" + (f", {ctx.searches} searches" if ctx.searches else "")
    note = note or (f"researched ({out.mode}): {out.usage['calls']} model calls, {seen}, tier {proposal['tier']}; "
                    "held for your Keep")
    ni_flow._transition(store, item_id, "assembling", source_url=url, note=note)
    ni_flow._try_journal(store, item_id, "updated", note)
    return ni_flow._finalize(store, item_id, spec, preview, note=note, born="flow", fetched_at=ctx.now,
                             built_from=ni_flow._built_from_of(store, item_id, path, url, title))


def _chat_for(store: ni.NIStore, model: str, local: bool,
              session: claudecli.TurnSession | None = None) -> Callable[[list, list | None], dict]:
    """The model seam bound to ``model``: the installed one, else the gateway's recorded path (tool use for the
    cloud loop, a plain completion for the closed-world local mode). ``session``: the run's Claude Code session
    (``_cli_session``), so each call after the first resumes it with only the new turns."""
    assert isinstance(local, bool), "local is a flag"
    assert model, "a model id"
    if _CHAT is not None:
        return lambda messages, tools_: _CHAT(model, messages, tools_)
    if local:
        return lambda messages, tools_: gateway.chat_recorded(
            store.conn, ni_models.PURPOSES["research"], messages, model, timeout=ni_flow._FLOW_MODEL_TIMEOUT_S, temperature=0.0)
    if claudecli.is_claudecode(model):
        # the Claude Code route honours neither max_tokens nor temperature (it refuses them loudly); its tool
        # offers and calls ride the text protocol, which ``loop._text_calls`` reads back
        return lambda messages, tools_: gateway.chat_with_tools_recorded(
            store.conn, ni_models.PURPOSES["research"], messages, model, tools_, timeout=RESEARCH_TIMEOUT_S,
            session=session)
    return lambda messages, tools_: gateway.chat_with_tools_recorded(
        store.conn, ni_models.PURPOSES["research"], messages, model, tools_, timeout=RESEARCH_TIMEOUT_S, max_tokens=RESEARCH_MAX_TOKENS)


@contextlib.contextmanager
def _cli_session(model: str) -> Iterator[claudecli.TurnSession | None]:
    """One Claude Code session for one research run on that route (``claudecli.TurnSession``, as a chat turn has):
    the first call sends the whole prompt and each later call resumes the session with only the new tool results,
    so the conversation so far is read from the prompt cache instead of sent and charged again. Until 2026-10-31
    each research call was a new CLI process that re-sent the whole transcript (T's first research run: 65,616
    prompt tokens in 11 calls, then its token cap). Wiped when the run ends, however it ends; None for any other
    route and under an installed seam."""
    assert isinstance(model, str), "a model id"
    session = None
    if _CHAT is None and model and claudecli.is_claudecode(model):
        try:
            session = claudecli.open_turn_session()
        except OSError as exc:  # no private folder for it: the run's calls go stateless, as before, never a failure
            log.warning("ni_research: no Claude Code session for this run: %s", type(exc).__name__)
    try:
        yield session
    finally:
        if session is not None:
            session.close()


# what a read the run could not use was, in the card's words, from the tool's error kind (``tools.fetch_sample``)
_READ_WORDS = {
    "not_data": "no data file or table",
    "fetch_failed": "could not be read",
    "robots": "its robots.txt asks readers to stay out",
    "redirect": "it redirects",
    "needs_key": "it needs a key",
    "needs_access": "it needs a key or email",
}
# a trace entry that is no read of a source: the model's repeat, an address nothing handed over, a spent budget
_NOT_A_READ = ("repeat", "provenance", "budget_exhausted", "invalid_args")


def _reads(ctx: ResearchCtx) -> dict[str, str]:
    """Each site the run read or tried to read, with what came of it in the card's words (the last word for a site
    wins), in the order first tried. From the run's own trace: hosts only, never an address."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    out: dict[str, str] = {}
    for entry in ctx.trace[: ni_recipe.MAX_TURNS]:  # bounded: the trace's own cap
        outcome = str(entry.get("outcome") or "")
        if entry.get("tool") != "fetch_sample" or outcome.startswith(_NOT_A_READ):
            continue
        host = (urlsplit(str(entry.get("arg") or "")).hostname or "").lower()[:253]
        if host:
            out[host] = ("read, but no card was built from it" if outcome == "ok"
                         else _READ_WORDS.get(outcome.split(":", 1)[0], "could not be used"))
    return out


def _research_tried(ctx: ResearchCtx) -> list[dict]:
    """What the run tried, as at most two lines of the card's "What I tried" (``ni_flow.note_tried``): its web
    searches, in their own words, and each site it read with what came of it. Before 2026-10-31 a run that ended
    without a card left only its stop reason, in a note no route showed: T's first research run declined after 11
    calls and nothing said what it had searched or read."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    searches = [e for e in ctx.trace[: ni_recipe.MAX_TURNS] if e.get("tool") == "web_search"]
    done = [str(e.get("arg") or "") for e in searches if e.get("outcome") == "ok"]
    failed = len(searches) - len(done)
    out = []
    if searches:
        words = ("searched for " + ", ".join(f'"{one_line(s, 60)}"' for s in done)) if done else "searched"
        lost = f"{failed} search failed" if failed == 1 else f"{failed} searches failed" if failed else ""
        out.append({"host": "web search", "provider": "Web search",
                    "why": one_line(words + (f"; {lost}" if done and lost else f": {lost}" if lost else ""), 200)})
    reads = _reads(ctx)
    if reads:
        parts, shown = [], 0
        for host, word in reads.items():  # bounded by the trace
            part = f"{host} ({word})"
            if len(", ".join([*parts, part])) > 170:
                break
            parts.append(part)
            shown += 1
        rest = len(reads) - shown
        out.append({"host": "research", "provider": "Research",
                    "why": one_line("read " + ", ".join(parts) + (f", and {rest} more" if rest else ""), 200)})
    return out


def _log_run(plan: policy.Plan, ctx: ResearchCtx, out: loop.Outcome) -> None:
    """One line in the app log for every research run: how it ended and why, what it spent (the cache's share
    named, so a budget that binds can be told from one that only counted cached reads), and what it read, by
    host. No address and no search words: those are the card's, not the log's."""
    assert isinstance(out, loop.Outcome), "an outcome"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    u = out.usage
    reads = "; ".join(f"{host} {word}" for host, word in _reads(ctx).items()) or "none"
    log.info("ni_research: %s run on %s ended %s (%s): %d model calls, prompt %d tokens (cache read %d, write %d), "
             "completion %d; %d tool calls, %d searches, %d fetches, %ds; reads: %s", plan.mode, plan.model,
             out.status, out.stop or out.ending or "-", u["calls"], u["prompt"], u["cache_read"], u["cache_write"],
             u["completion"], ctx.turns, ctx.searches, ctx.fetches, int(ctx.elapsed()), reads)


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


def can_research(store: ni.NIStore, item_id: str, record: dict) -> bool:
    """Could a run of this card research now: the Library is there and the tier is not off (an ``ask`` plan asks
    for model consent first, which is the user's to give)?"""
    assert store is not None and item_id, "args required"
    return research_blocker(store, item_id, record) is None


def research_blocker(store: ni.NIStore, item_id: str, record: dict) -> str | None:
    """Why no run of this card could research now, in the user's words (the card's honest line names it, never
    "no source answers this" for a missing model): the Library is not installed, research is turned off, or no
    model is routed for it. None when a run could research (``can_research``)."""
    assert store is not None and item_id, "args required"
    if ni_flow._resolve_library() is None:
        return "the SmartBrain Library isn't installed"
    if plan_for(store, item_id, record).mode != "off":
        return None
    setting_ = policy.setting(store.conn)
    if setting_ == "off":
        return "research is turned off (Settings > Models)"
    side = {"cloud": "no cloud model", "local": "no local model"}.get(setting_, "no cloud or local model")
    return f"{side} is routed for research (Settings > Models)"


def fallback(store: ni.NIStore, item_id: str, request: str, intent: dict, links: list | None = None) -> dict | None:
    """Research before the honest stop, under the consent the card holds. When the Library has nothing buildable or
    every row it offered failed, research runs at once on the plan the card's own consent allows (``plan_for``:
    the consent question named it — "if no ready-made source fits, it may also research one with the same
    service"); a card with no consent yet is asked (the ``ask`` plan); research off, no model routed or no Library
    reads None, and the caller ends the ask with the reason. Until 2026-10-31 a first run ended at once as the
    ``no_ready_source`` decline and research ran only after the Keep looking tap, so an install whose Library rows
    all failed never researched. A standing order (``_looking``, release P2) runs the same way; its ``free`` run
    (a $0 re-match after a new kept source) reads None. Returns the landed (held) card, the consent question, or (a
    run that ended without a card) the declined card, as the flow record, with ``links`` riding a decline the run
    found no links for. A run that could not finish (a fault) is the failed card, retryable, never a declined one."""
    assert isinstance(intent, dict) and request, "intent and request required"
    assert store is not None and item_id, "args required"
    record = ni_flow._flow_read(store, item_id) or {}
    if record.get("_cancel"):
        return None
    looking = record.get("_looking")
    lib = ni_flow._resolve_library()
    if lib is None or (isinstance(looking, dict) and looking.get("free")):
        return None
    plan = plan_for(store, item_id, record)
    if plan.mode == "off":
        return None
    if plan.mode == "ask":
        return ni_flow._ask_model_consent(store, item_id, plan.model or "", plan.local, research=True)
    now = ni._clock().astimezone(UTC)
    if plan.mode == "cloud" and not ni_refresh.charge_phone_spend(store.conn, now):
        # a run a phone started draws its unit before research spends (a cloud build drew it already): today's is gone
        return ni_flow._fail(store, item_id, "phone_allowance", ni_refresh.PHONE_SPEND_LINE)
    web = plan.mode == "cloud" and plan.web  # the closed-world local tier never has the web
    question = question_from_intent(request, intent)
    # a compare is searched one thing at a time: the intent's one place must not steer every search
    hint = {**ni_flow._library_hint(intent), **({"place": None} if question["entities"] else {})}
    library = _Library(lib, hint, set(record.get("_declined") or []), ni_flow.skipped_sites(record))
    ctx = ResearchCtx(request=request, question=question, now=now, window=_window_of(request, intent),
                      zone=ZoneInfo(ni.user_timezone_name()), allowed_origins=ORIGINS_WEB if web else ORIGINS_LIBRARY,
                      searcher=_searcher() if web else None, library=library)
    ni_flow._transition(store, item_id, record.get("state") or "source", error=record.get("error"),
                        note=f"researching ({plan.mode}) for a Library source" if not web
                        else "researching (cloud) in the Library and on the web")
    _audit(item_id, plan)
    declined = _declined_of(record)
    accept = lambda proposal: _can_land(ctx, item_id, request, proposal, declined)
    place = "" if question["entities"] else str(intent.get("place") or "")
    with _cli_session((plan.model or "") if plan.mode == "cloud" else "") as session:
        chat = _chat_for(store, plan.model or "", plan.mode == "local", session)
        out = (loop.run_research(ctx, chat, place=place, accept=accept) if plan.mode == "cloud"
               else loop.run_closed_world(ctx, chat, accept=accept))
    _log_run(plan, ctx, out)
    if out.status == "proposed" and out.proposal is not None:
        return _land(store, item_id, request, ctx, out.proposal, out)
    # a run that ended without a card says what it searched and read, beside the Library rows the build dropped
    ni_flow.note_tried(store, item_id, _research_tried(ctx))
    if out.status == "fault":  # the run could not finish: a fault the user retries, never a verdict about the ask
        return ni_flow._fail(store, item_id, "research", one_line("; ".join(out.reasons), 160))
    ni_flow._append_note(store, item_id, one_line("researched, no card: " + "; ".join(out.reasons), 240))
    # the run ended without a card: that is the honest declined card (a closed code, up to three links the run saw),
    # not a stop on links. A compare that no source built says so; one the verifier refused says that.
    ending = out.ending or "research_declined_budget"
    if question["entities"] and ending != "research_declined_checks":
        ending = "compare_unbuildable"
    # P3: an address that asked for a key is offered as Use my key (one thing at a time: never for a compare)
    wall = ctx.key_walls[0] if ctx.key_walls and not question["entities"] else None
    # the line names what stopped the run (its time, its model budget, its steps, the model), never one for all
    line = ni_master.gave_up_line(out.stop) if ending == "research_declined_budget" else ""
    return ni_flow._end(store, item_id, ending, line, links=out.links or links, key_wall=wall)


def research_plan(store: ni.NIStore, item: dict) -> policy.Plan | None:
    """The plan a re-research of this card would run on, or None. Only the cloud tier with the consent this
    card already holds (``_model_consent``) re-researches: off, ask, local and a missing Library all read None,
    and the card simply stays failing with its own status."""
    assert store is not None and isinstance(item, dict), "store + item required"
    assert item.get("id"), "an item id"
    if ni_flow._resolve_library() is None:
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
    with _cli_session(plan.model or "") as session:
        out = loop.run_research(ctx, _chat_for(store, plan.model or "", False, session), place=place, accept=accept)
    _log_run(plan, ctx, out)
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
