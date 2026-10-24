"""The five code-run research tools (plan 6.1, 6.4). No model, no product caller yet (S4c wires the loop).

Each tool spends a turn on the ``ResearchCtx``, does its work in code and returns a typed result:
``{"ok": True, ...}`` or ``{"ok": False, "error": kind, "detail": ...}``. Fetches go through the one shared
fetch-and-parse path (``ni_fetch``, netguard underneath) with no headers, no POST and no redirects.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from urllib.parse import unquote, urlsplit

from .. import (
    ni,
    ni_brief,
    ni_compare,
    ni_fetch,
    ni_polite,
    ni_recipe,
    ni_table,
    page_verify,
    pagegraph,
)
from ..netguard import FetchError
from ..ni_verify.classes import shadow_reasons
from ..ni_verify.rows import SourceInfo, read_result
from .ctx import (
    MAX_FAILED_FETCHES,
    MAX_REPLAYS,
    ResearchCtx,
    error,
    normal_url,
    one_line,
)
from .digest import (
    MAX_SEARCH_ROWS,
    clean_search_rows,
    clean_text,
    digest,
    rank_search_rows,
    search_digest,
    table_digest,
)

MAX_QUERY = 200
MAX_SPEC_DETAIL = 480   # an invalid_spec detail names the field and lists the allowed values: it must not be cut short
MAX_DRAFT_BYTES = 32 * 1024
MAX_PREVIEW_CHARS = 4000
MAX_PREVIEW_ROWS = 10
MAX_CANDIDATES = 5
MAX_SAMPLES = 8
_STRUCTURED = frozenset({"json", "csv", "feed", "xml"})
_TABLE = ni.TABLE_FORMAT   # the sample format of a web page's data table (page reading)
_MANY_KINDS = frozenset({"latest_items", "ranking"})
_VERIFY_CLASSES = {"window": "window", "threshold": "threshold", "compare": "compare", "scope": "place"}
_ORIGIN_WHY = {"library": "a Library source", "search": "a search result", "preview": "the previewed endpoint",
               "kept": "a source you kept before"}


def _done(ctx: ResearchCtx, tool: str, arg: object, result: dict) -> dict:
    """Append the trace entry for ``result`` and return it."""
    assert tool in ni_recipe.TOOLS, "a known tool"
    assert isinstance(result, dict) and "ok" in result, "a tool result"
    ctx.note(tool, arg, "ok" if result["ok"] else f"{result['error']}: {result.get('detail', '')}")
    return result


def fetch_sample(ctx: ResearchCtx, url: str) -> dict:
    """Fetch one sample (GET, no headers, no redirects) from an allowed origin and return its fenced digest."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_fetches >= 0, "a fetch cap"
    if not isinstance(url, str) or len(url) > 2000:
        return error("invalid_args", "url must be a string")
    again = _already_fetched(ctx, url)
    if again is not None:   # an address read (or refused) once is never fetched again: the answer is the same
        return again
    out = ctx.spend("fetch_sample")
    if out is not None:
        return out
    origin, why = ctx.origin(url)
    if origin is None:
        return _done(ctx, "fetch_sample", url, error("provenance", why))
    info = ctx.urls.get(normal_url(url)) or {}
    if info.get("blocked"):
        return _done(ctx, "fetch_sample", url, error("needs_access", "this source needs a key or contact email"))
    out = ctx.spend_fetch()
    if out is not None:
        return _done(ctx, "fetch_sample", url, out)
    got = _fetch_and_digest(ctx, normal_url(url), info.get("format"), robots=origin != "library")
    if not got["ok"] and got["error"] in _FETCH_ERRORS and len(ctx.failed_fetches) < MAX_FAILED_FETCHES:
        ctx.failed_fetches[normal_url(url)] = got
    return _done(ctx, "fetch_sample", url, got)


_FETCH_ERRORS = frozenset({"not_data", "fetch_failed", "robots", "redirect"})


def _already_fetched(ctx: ResearchCtx, url: str) -> dict | None:
    """The answer for an address this run already fetched (the kept sample's digest, or the error it gave), or None
    when it was not. Spends no fetch, and no turn for the first ``MAX_REPLAYS`` repeats; a model that keeps repeating
    pays a turn for each after that. Only an address a trusted channel handed over was ever fetched, so this
    reveals nothing a fetch would not."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(url, str), "url must be text"
    key = normal_url(url)
    kept, failed = ctx.samples.get(key), ctx.failed_fetches.get(key)
    if kept is None and failed is None:
        return None
    if ctx.replays >= MAX_REPLAYS:
        out = ctx.spend("fetch_sample")
        if out is not None:
            return out
    ctx.replays += 1
    ctx.note("fetch_sample", url, "repeat (answered from memory)")   # the sealed trace shows the model's repeats
    if kept is not None and kept.get("digest"):
        return {"ok": True, "url": url, "format": kept["format"], "digest": kept["digest"], "cached": True,
                "fetches_left": ctx.max_fetches - ctx.fetches}
    if failed is not None:
        return {**failed, "cached": True, "detail": one_line(f"already fetched: {failed.get('detail')}", 200)}
    return None


def _fetch_and_digest(ctx: ResearchCtx, url: str, fmt: str | None, robots: bool = True) -> dict:
    """Fetch ``url`` (a sealed ``fmt`` from the Library, else sniffed), keep the sample, digest it. ``robots``:
    a source that is not a Library row obeys its host's robots.txt, whichever fetcher reads it. An ordinary web
    page is read for its data tables only (``_page_sample``); its prose is never read."""
    assert isinstance(url, str) and url, "url required"
    assert fmt is None or isinstance(fmt, str), "format is text"
    try:
        with ni_polite.engaged(robots=robots):
            if fmt == "html":
                return _page_sample(ctx, url, ni_fetch.fetch_page_tables(url, allow_redirects=False))
            if fmt in _STRUCTURED or fmt == "text":
                parsed, seen = ni_fetch.fetch_parsed(url, fmt, allow_redirects=False), fmt
            else:
                parsed, seen = ni_fetch.sniff_fetch(url, allow_redirects=False, keep_page=True)
    except ni_fetch.HtmlPage as page:
        try:
            # page_tables is pure (the page was fetched inside ``engaged`` above), so this read needs no polite span;
            # the raw bytes go to the jail so it decodes the page by the same rule as a refresh
            read = ni_fetch.page_tables(page.body, url, charset=page.charset)
        except FetchError:
            return error("not_data", "the page could not be read")
        return _page_sample(ctx, url, read)
    except FetchError as exc:
        text = str(exc).lower()
        if exc.kind == "robots":
            return error("robots", "the site's robots.txt asks automated readers to stay out")
        if "redirect" in text:
            return error("redirect", "the source redirected; redirects are not followed")
        if exc.kind in ("not_json", "not_text", "not_csv", "not_feed", "not_xml", "not_table"):
            return error("not_data", "not a data file or a page with a data table")
        return error("fetch_failed", clean_text(str(exc), 120))
    return _keep_sample(ctx, url, seen, parsed, digest(url, seen, parsed))


def _keep_sample(ctx: ResearchCtx, url: str, fmt: str, parsed: object, shown: str, **extra: object) -> dict:
    """Store the sample (the oldest of ``MAX_SAMPLES`` goes) and return the fetch result the model reads."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(shown, str), "the fenced digest"
    if len(ctx.samples) >= MAX_SAMPLES:
        ctx.samples.pop(next(iter(ctx.samples)))
    ctx.samples[normal_url(url)] = {"format": fmt, "parsed": parsed, "digest": shown, **extra}
    return {"ok": True, "url": url, "format": fmt, "digest": shown, "fetches_left": ctx.max_fetches - ctx.fetches}


def _page_sample(ctx: ResearchCtx, url: str, page: dict) -> dict:
    """A web page as a sample: its data tables (``ni_table``), fenced. A page with no data table is ``not_data``: the
    text of a page is never a value, and a feed it announces is only named, not fetchable unless a search returns it."""
    assert isinstance(page, dict), "a read page"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    if not page["tables"]:
        feed = "; it announces a feed, which only a search result can make fetchable" if page["feeds"] else ""
        return error("not_data", "a web page with no data table (page text is not read)" + feed)
    graph = pagegraph.graph_from_extract(url, page["extract"])
    return _keep_sample(ctx, url, _TABLE, {"tables": page["tables"]},
                        table_digest(url, page["tables"], page["feeds"], page["entities"]), graph=graph)


def web_search(ctx: ResearchCtx, query: str) -> dict:
    """Search the web (cloud tier, web allowed, at most ``ctx.max_searches`` per run) and return a fenced digest of
    titles, snippets and addresses; every kept address becomes a legal ``search`` fetch origin."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_searches >= 0, "a search cap"
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
        return error("invalid_args", f"query must be 1..{MAX_QUERY} characters")
    if "search" not in ctx.allowed_origins:  # before any spend: a Library-only run is not charged for the refusal
        return error("web_off", "this run reads SmartBrain Library sources only")
    out = ctx.spend("web_search", "search")
    if out is not None:
        return out
    if not callable(ctx.searcher):
        return _done(ctx, "web_search", query, error("search_unavailable", "no search service is set up"))
    asked = clean_text(query.strip(), MAX_QUERY)
    try:
        got = ctx.searcher(asked, MAX_SEARCH_ROWS)
    except Exception as exc:  # a failed search degrades to a typed error, never a crash
        return _done(ctx, "web_search", query, error("search_unavailable", type(exc).__name__))
    rows = clean_search_rows(got.get("results") if isinstance(got, dict) else None)
    ctx.record_search_results(rows, asked)
    return _done(ctx, "web_search", asked, {"ok": True, "results": len(rows), "digest": search_digest(asked, rank_search_rows(rows)),
                                            "searches_left": ctx.max_searches - ctx.searches})


def library_search(ctx: ResearchCtx, query: str) -> dict:
    """Library candidates for ``query`` (the highest-trust sources); their URLs become legal fetch origins."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
        return error("invalid_args", f"query must be 1..{MAX_QUERY} characters")
    out = ctx.spend("library_search")
    if out is not None:
        return out
    candidates = getattr(ctx.library, "candidates", None)
    if not callable(candidates):
        return _done(ctx, "library_search", query, error("library_unavailable", "the Library is not installed"))
    try:
        rows, _skipped = candidates(query.strip(), limit=MAX_CANDIDATES)
    except Exception as exc:  # a broken Library degrades to an error, never a crash
        return _done(ctx, "library_search", query, error("library_unavailable", type(exc).__name__))
    found = []
    for row in rows[:MAX_CANDIDATES]:  # bounded by the limit
        blocked = bool(row.get("needs_key") or row.get("needs_contact"))
        ctx.record(row["url"], "library", format=row.get("format"), scope=row.get("scope"),
                   title=one_line(row.get("title") or "", 120), blocked=blocked, query=query.strip(),
                   page=row.get("page"))
        found.append({"title": one_line(row.get("title") or "", 120), "provider": one_line(row.get("provider") or "", 80),
                      "url": row["url"], "format": row.get("format"), "scope": row.get("scope"),
                      "tier": row.get("tier"), "needs_access": blocked})
    return _done(ctx, "library_search", query, {"ok": True, "candidates": found})


def _resolve(ctx: ResearchCtx, resolver: str, text: str) -> dict:
    """One resolver's answer for ``text`` (the injected one, else the Library's tables)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert resolver and isinstance(text, str), "args required"
    if ctx.resolver is not None:
        return ctx.resolver(resolver, text)
    from ..library_resolve import Resolver  # local: keeps duckdb off the import edge
    with ctx.library._conn() as con:  # type: ignore[union-attr]
        return Resolver(con).by_name(resolver, text)


def resolve_entity(ctx: ResearchCtx, text: str) -> dict:
    """Resolve a named place or entity through the Library's resolver tables (no network)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    if not isinstance(text, str) or not text.strip() or len(text) > 120:
        return error("invalid_args", "text must be 1..120 characters")
    out = ctx.spend("resolve_entity")
    if out is not None:
        return out
    from ..library_index import GEO_RESOLVERS
    try:
        for name in GEO_RESOLVERS:  # bounded: the geo resolvers
            got = _resolve(ctx, name, text.strip()) or {}
            if got.get("status") in ("resolved", "ambiguous"):
                cands = [{k: c.get(k) for k in ("name", "state", "lat", "lon", "kind")}
                         for c in (got.get("candidates") or [])[:MAX_CANDIDATES]]
                ctx.entities.append(one_line(f"{name}:{text.strip()}", 120))
                return _done(ctx, "resolve_entity", text, {"ok": True, "status": got["status"], "resolver": name,
                                                             "candidates": cands})
    except Exception as exc:  # no Library or an older pack
        return _done(ctx, "resolve_entity", text, error("library_unavailable", type(exc).__name__))
    return _done(ctx, "resolve_entity", text, {"ok": True, "status": "none", "resolver": None, "candidates": []})


def _bounded(node: object, depth: int = 0) -> object:
    """A preview copy: short clean strings, at most ``MAX_PREVIEW_ROWS`` list items, shallow."""
    assert depth >= 0, "a depth"
    assert MAX_PREVIEW_ROWS > 0, "a row cap"
    if isinstance(node, str):
        return clean_text(node, 120)
    if isinstance(node, dict) and depth < 4:
        return {clean_text(str(k), 60): _bounded(v, depth + 1) for k, v in list(node.items())[:40]}
    if isinstance(node, list) and depth < 4:
        return [_bounded(v, depth + 1) for v in node[:MAX_PREVIEW_ROWS]]
    return node if isinstance(node, (bool, int, float)) or node is None else clean_text(str(node), 60)


def _checks(ctx: ResearchCtx, outputs: dict, url: str, info: dict) -> list[dict]:
    """The ni_verify classes (window, threshold, compare, scope) plus an honest-empty check, as recipe checks."""
    assert isinstance(info, dict), "source info"
    assert isinstance(outputs, dict), "outputs must be a dict"
    texts = [str(v) for v in outputs.values()] + [ctx.request, str(ctx.question.get("subject") or "")]
    result = read_result(outputs, texts, ctx.zone)
    source = SourceInfo(name=pagegraph.registrable_domain(urlsplit(url).hostname or ""), url=url,
                        scope=info.get("scope"))
    found = shadow_reasons(ctx.request, result, source, ctx.now, ctx.zone)
    # S7: the compare check reads the card's own rows, never the ask's words (the ask always names every thing):
    # each asked thing must appear in a row with a value of its own
    wanted = list(ctx.question.get("entities") or []) or list(ni_compare.asked(ctx.request))
    found["compare"] = ni_compare.reasons(wanted, outputs)
    checks = [{"check": check, "ok": not found.get(cls), "reason": one_line("; ".join(found.get(cls) or []), 120)}
              for cls, check in _VERIFY_CLASSES.items()]
    empty = not any(v not in (None, "", [], {}) for v in outputs.values())
    return [*checks, {"check": "empty", "ok": not empty, "reason": "every output is empty" if empty else ""}]


_ADDRESS_DAY_RE = re.compile(r"(?<!\d)(20\d\d)[-/.]?(0[1-9]|1[0-2])[-/.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_ADDRESS_EPOCH_RE = re.compile(r"(?<!\d)(1[6-9]\d{8})(\d{3})?(?!\d)")
_FIXED_NEAR = timedelta(days=7)   # a moment this close to now in an address is "today" written down, not an asked day


def _fixed_moment(url: str, now) -> str | None:
    """The date or timestamp within a week of ``now`` that ``url`` writes into its path or query, else None. A card
    reads the same sealed address every day, so an address that fixes "today" (or "the past week", from-to) shows that
    day forever; a day far from now is a day the ask named and may stay."""
    assert isinstance(url, str), "an address"
    assert now.tzinfo is not None, "now must be aware"
    parts = urlsplit(url)
    text = unquote(f"{parts.path}?{parts.query}")[:2000]
    today = now.date()
    for m in _ADDRESS_DAY_RE.finditer(text):  # bounded by the capped address
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if abs(day - today) <= _FIXED_NEAR:
            return m.group(0)
    for m in _ADDRESS_EPOCH_RE.finditer(text):  # epoch seconds (or milliseconds)
        if abs(int(m.group(1)) - now.timestamp()) <= _FIXED_NEAR.total_seconds():
            return m.group(0)
    return None


def _stable(urls: list[str], now) -> dict:
    """The ``stable`` recipe check for a build: no source address fixes a day near now. Build only; the refresh
    re-check (``checks_for``) never runs it, as a sealed address cannot change."""
    assert isinstance(urls, list) and urls, "the build's source addresses"
    fixed = next((f for f in (_fixed_moment(u, now) for u in urls[:8]) if f), None)
    return {"check": "stable", "ok": fixed is None, "reason": "" if fixed is None else one_line(
        f"the address fixes the date {fixed}, so a kept card would show that day forever: use an address that is "
        "always current", 160)}


def checks_for(ctx: ResearchCtx, outputs: dict, url: str, info: dict) -> list[dict]:
    """The recipe checks for ``outputs`` (the verifier ``try_recipe`` and ``propose_card`` run), for the refresh
    re-check (S9): the same classes on a fresh payload, so a constraint cannot pass at Keep by a different rule."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(outputs, dict), "outputs must be a dict"
    return _checks(ctx, outputs, url, info)


def sample_payload(parsed: object) -> object:
    """A sample as the engine reads it at refresh: a bare list is wrapped as ``{"items": [...]}``."""
    return ni_brief.normalize(parsed) or parsed


def spec_input(sample: dict, source: dict) -> object:
    """What the pipeline reads from the stored ``sample`` for this sealed ``source``: the sample as the engine reads it,
    or, for a page, the one table the source names (found by its header names exactly as a refresh finds it, so a
    table the page cannot single out is ``ni_table.TableGone`` here too)."""
    assert isinstance(sample, dict) and isinstance(source, dict), "a sample and a source"
    if sample["format"] != _TABLE:
        return sample_payload(sample["parsed"])
    _index, table = ni_table.locate(sample["parsed"]["tables"], source["table"])
    return ni_table.payload(table)


def _seal_table(ctx: ResearchCtx, draft: dict) -> tuple[dict | None, dict]:
    """A draft that reads a page table names it ``source.table = {"index": N}`` (N from the fetch digest); code seals the
    table by its header names (``ni_table.locator``) so the card finds the same table again, whatever its position."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(draft, dict), "a draft"
    source = draft["source"]
    sample = ctx.samples.get(normal_url(str(source.get("url") or "")))
    if sample is None or sample["format"] != _TABLE:
        return None, error("no_sample", "fetch_sample the page URL first")
    tables, want = sample["parsed"]["tables"], source.get("table")
    index = want.get("index") if isinstance(want, dict) and set(want) == {"index"} else None
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(tables):
        return None, error("invalid_spec", 'source.table must be {"index": N}, a table number from the fetch digest')
    out = copy.deepcopy(draft)
    out["source"]["table"] = ni_table.locator(tables[index])
    return out, {}


def _page_check(ctx: ResearchCtx, sample: dict, outputs: dict) -> dict:
    """The page-reading gate (``page_verify``, the compiled tier: every value is lifted verbatim from the page's
    tables and must be on the page) as a recipe check. A checker that fails closed: it cannot pass what it cannot read."""
    assert isinstance(ctx, ResearchCtx) and isinstance(sample, dict), "a ctx and a sample"
    assert isinstance(outputs, dict), "outputs must be a dict"
    kind = ctx.question.get("kind")
    try:
        reasons = page_verify.verify_page_reading(
            sample["graph"], outputs, frame_kind=kind, wants=[], subject=str(ctx.question.get("subject") or ""),
            now=ctx.now, many=kind in _MANY_KINDS, tier="compiled", window=ctx.window)
    except Exception as exc:  # any failure of the checker is a failed check, never a pass
        reasons = [f"the page reading could not be checked ({type(exc).__name__})"]
    return {"check": "subject", "ok": not reasons, "reason": one_line("page reading: " + "; ".join(reasons), 120) if reasons else ""}


def brief_spec(ctx: ResearchCtx, spec: dict, parsed: object) -> dict:
    """S8: for a headline question (``text_brief`` / ``latest_items``) over a sample that holds a list of titled,
    linked or dated items, the card is laid out by code (``ni_brief``): the model's source stays, its pipeline and
    scene are replaced, so the headline text is extracted and never written by a model. Any other question or
    sample keeps the draft as written."""
    assert isinstance(ctx, ResearchCtx) and isinstance(spec, dict), "a ctx and a spec"
    kind = ctx.question.get("kind")
    if not ni_brief.is_brief_question(kind):
        return spec
    cadence = ni_recipe.CADENCE_MINUTES.get(ctx.question.get("cadence") or "hourly", 60)
    built = ni_brief.build(parsed, title=str(spec.get("title") or ctx.question.get("subject") or "Headlines"),
                           ask=ctx.request, now=ctx.now, source_url=str(spec["source"]["url"]),
                           cadence_s=cadence * 60, viewer_tz=str(getattr(ctx.zone, "key", None) or "UTC"),
                           question_kind=kind, wants=[])
    if built is None:
        return spec
    from ..ni_forms.form_scene import display_size_for_span
    return {**spec, "pipeline": built["pipeline"], "scene": built["scene"], "brief": built["brief"],
            "display": {"size": display_size_for_span(str(built["scene"]["spans"]["desktop"]))}}


def _run(ctx: ResearchCtx, draft: object) -> dict:
    """Validate ``draft`` as a researcher draft and run its pipeline on the stored sample (no network)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    if not isinstance(draft, dict) or len(json.dumps(draft, default=str)) > MAX_DRAFT_BYTES:
        return error("invalid_spec", f"draft_spec must be an object under {MAX_DRAFT_BYTES} bytes")
    if isinstance(draft.get("source"), dict) and draft["source"].get("format") == _TABLE:
        draft, err = _seal_table(ctx, draft)
        if draft is None:
            return err
    try:
        spec = ni.validate_spec(copy.deepcopy(draft), researcher=True)
    except (ValueError, TypeError, AssertionError) as exc:
        return error("invalid_spec", str(exc), limit=MAX_SPEC_DETAIL)
    source = spec.get("source") or {}
    if source.get("type") != "http_json" or source.get("headers") or source.get("secret_query"):
        return error("invalid_spec", "a researcher draft reads one plain http_json source")
    key = normal_url(str(source.get("url") or ""))
    sample = ctx.samples.get(key)
    if sample is None:
        return error("no_sample", "fetch_sample the source URL first")
    if (source.get("format") or "json") != sample["format"]:
        return error("invalid_spec", f"source.format must be {sample['format']} like the sample")
    try:
        given = spec_input(sample, source)
    except ni_table.TableGone as exc:
        return error(exc.kind, exc.detail)
    spec = brief_spec(ctx, spec, given)
    try:
        outputs = ni.run_pipeline(copy.deepcopy(spec["pipeline"]), copy.deepcopy(given))
    except ni.NIError as exc:
        return error("pipeline_failed", f"{exc.kind}: {exc.detail}" + _miss_hint(exc, spec["pipeline"], given),
                     limit=MAX_SPEC_DETAIL)
    info = ctx.urls.get(key) or {}
    checks = [*_checks(ctx, outputs, source["url"], info), _stable([source["url"]], ctx.now)]
    if sample["format"] == _TABLE:
        checks.append(_page_check(ctx, sample, outputs))
    preview = _bounded(outputs)
    if len(json.dumps(preview, default=str)) > MAX_PREVIEW_CHARS:
        preview = _bounded_small(outputs)
    return {"ok": True, "spec": spec, "key": key, "format": sample["format"], "checks": checks, "preview": preview,
            "outputs": outputs, "lands": landing_reason(ctx, spec, outputs, validate=False)}


def cadence_stamp(question: object) -> int | None:
    """The refresh interval the code stamps from a question's cadence (None when it names none)."""
    assert question is None or isinstance(question, (dict, str)), "a question"
    cadence = question.get("cadence") if isinstance(question, dict) else None
    return ni_recipe.CADENCE_MINUTES.get(cadence) if isinstance(cadence, str) else None


def stamp(spec: dict, request: str, minutes: int | None = None) -> dict:
    """The researcher's spec with the code-owned fields, as it lands: the goal is the user's words, the interval
    comes from the question's cadence (never the model's). ``minutes`` None reads the spec's own recipe."""
    assert isinstance(spec, dict), "spec required"
    assert isinstance(request, str), "the user's words"
    out = copy.deepcopy(spec)
    out["goal"] = request[:ni._MAX_GOAL]
    out["interval_minutes"] = minutes if minutes is not None else ni_recipe.cadence_minutes(out)
    out.setdefault("repair_policy", {"l1": True, "l2_frontier": False})
    assert out["interval_minutes"] is not None, "the question carries a cadence"
    return out


def landing_reason(ctx: ResearchCtx, spec: dict, outputs: dict, item_id: str = "preview", validate: bool = True
                   ) -> str | None:
    """Why this draft could not become a card, else None: the spec as it lands (stamped, whole) validates and its scene
    binds the outputs of the stored sample. The one rule the landing uses (``ni_research_flow._can_land`` runs it), so
    a draft cannot pass try_recipe or propose_card and then fail to land: the model hears it while it can still fix it.
    ``validate`` False binds only (a draft, before it is sealed with its recipe)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(spec, dict) and isinstance(outputs, dict), "a spec and its outputs"
    try:
        landed = stamp(spec, ctx.request, cadence_stamp(ctx.question))
        if validate:   # a draft has no recipe yet, and the whole-spec rules are for the sealed proposal
            ni.validate_spec(landed)
        ni.bind_scene(landed["scene"], outputs, history=ni._seed_history(landed),
                      image_ref=ni._preview_image_ref(landed, item_id),
                      form_ctx=ni._form_bind_context(landed, ctx.now), alternatives=True)
    except (ni.NIError, ValueError, KeyError, AssertionError) as exc:
        return f"the card cannot be shown: {bind_hint(exc, outputs)}"
    return ni_compare.refusal(landed, outputs)


def bind_hint(exc: Exception, outputs: dict) -> str:
    """A landing failure as the model should read it: a scene that binds a name the pipeline does not output says so
    and lists the outputs it has (the usual cause of an ``extract_miss`` at bind time)."""
    assert isinstance(exc, Exception), "an error"
    assert isinstance(outputs, dict), "outputs"
    text = one_line(exc, 150)
    detail = str(getattr(exc, "detail", ""))
    if getattr(exc, "kind", "") == "extract_miss" and detail.startswith("key '") and "'" in detail[5:]:
        name = detail[5:detail.index("'", 5)]
        if name not in outputs:
            have = ", ".join(one_line(k, 30) for k in list(outputs)[:12]) or "none"
            return one_line(f"the scene binds '{name}' but the pipeline outputs only: {have}. Bind only those names", 220)
    return text


def _miss_hint(exc: ni.NIError, pipeline: list, given: object) -> str:
    """For an ``extract_miss``: which path of the first extract stage stops where, and the keys there. Bounded."""
    assert isinstance(pipeline, list), "a pipeline"
    if exc.kind != "extract_miss" or not pipeline or not isinstance(pipeline[0], dict):
        return ""
    paths = pipeline[0].get("paths")
    for name, path in list((paths if isinstance(paths, dict) else {}).items())[:40]:  # bounded: one stage's paths
        try:
            steps = ni.parse_path(path)
        except (ni.NIError, ValueError, TypeError):
            continue
        cur = given
        for i, step in enumerate(steps[:40]):  # bounded: a path's steps
            try:
                nxt = ni._resolve_path(cur, [step])
            except ni.NIError:
                at = ".".join(str(st[1]) for st in steps[:i] if st[0] == "key") or "the top"
                keys = ", ".join(clean_text(str(k), 30) for k in list(cur)[:12]) if isinstance(cur, dict) else \
                    (f"a list of {len(cur)}" if isinstance(cur, list) else "no keys")
                return one_line(f" | path {path!r} for {name!r} fails at step {i + 1}; at {at} there is: {keys}", 260)
            cur = nxt
    return ""


def _bounded_small(outputs: dict) -> dict:
    """A preview that is still too long: the first few keys only, so it fits the cap."""
    assert MAX_PREVIEW_CHARS > 0, "a preview cap"
    assert isinstance(outputs, dict), "outputs must be a dict"
    out: dict = {}
    for k, v in list(outputs.items())[:12]:  # bounded
        out[clean_text(str(k), 60)] = _bounded(v, 3)
        if len(json.dumps(out, default=str)) > MAX_PREVIEW_CHARS:
            out.pop(clean_text(str(k), 60))
            break
    return out


def try_recipe(ctx: ResearchCtx, draft_spec: object) -> dict:
    """Run a draft on the last sample (no network): the card preview rows and the ni_verify reasons, or a typed error."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    out = ctx.spend("try_recipe")
    if out is not None:
        return out
    got = _run(ctx, draft_spec)
    if not got["ok"]:
        return _done(ctx, "try_recipe", "draft", got)
    reasons = [f"{c['check']}: {c['reason']}" for c in got["checks"] if not c["ok"]]
    if got["lands"]:
        reasons.append(f"lands: {got['lands']}")
    if not reasons and isinstance(draft_spec, dict):   # a pass is kept: the loop proposes it before the budget is gone
        ctx.passed = {"draft": copy.deepcopy(draft_spec), "tried": False}
    return _done(ctx, "try_recipe", "draft", {"ok": True, "preview": got["preview"], "checks": got["checks"],
                                              "reasons": reasons, "verdict": "fail" if reasons else "pass"})


def _constraints(question: dict) -> list[dict]:
    """The question's constraints as recipe checks, stamped by code from the ask (never from the draft)."""
    assert ni_recipe.MAX_CONSTRAINTS > 0, "a constraint cap"
    assert isinstance(question, dict), "question must be a dict"
    window, threshold = question.get("window"), question.get("threshold")
    wanted = [("subject", question.get("subject")), ("numbers", None), ("freshness", None), ("empty", None)]
    if window:
        wanted.append(("window", window.get("from") or window.get("to")))
    if threshold:
        wanted.append(("threshold", threshold.get("value")))
    if question.get("places"):
        wanted.append(("place", question["places"][0]))
    if question.get("compare_count"):
        wanted.append(("compare", question["compare_count"]))
    if question.get("units"):
        wanted.append(("units", question["units"]))
    return [{"check": c, "value": v} for c, v in wanted][: ni_recipe.MAX_CONSTRAINTS]


def _tier(origin: str, fmt: str) -> str:
    """The quality tier, derived by code from how the source was found and what it is."""
    assert origin in _ORIGIN_WHY, "a known origin"
    assert isinstance(fmt, str), "a format"
    if fmt == _TABLE:   # a page's table is a reading of a page, whoever found the page
        return "page_reading"
    if origin == "library":
        return "curated"
    return "verified_endpoint" if fmt in _STRUCTURED else "page_reading"


def propose_card(ctx: ResearchCtx, draft_spec: object) -> dict:
    """The terminal tool: re-run the draft and, when every check passes, seal the proposal (recipe, tier, trace).
    A proposal of the held pass that fails leaves it no longer waiting (it cannot land as it is)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    out = ctx.spend("propose_card")
    if out is not None:
        return out
    got = _propose(ctx, draft_spec)
    if not got["ok"]:
        ctx.mark_tried(draft_spec)
    return got


def _propose(ctx: ResearchCtx, draft_spec: object) -> dict:
    """``propose_card`` after the budget: run, seal, land."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    got = _run(ctx, draft_spec)
    if got["ok"] and (any(not c["ok"] for c in got["checks"]) or got["lands"]):
        reasons = [f"{c['check']}: {c['reason']}" for c in got["checks"] if not c["ok"]]
        reasons += [f"lands: {got['lands']}"] if got["lands"] else []
        got = error("verify_failed", "; ".join(reasons))
        got["reasons"] = reasons
    if not got["ok"]:
        return _done(ctx, "propose_card", "draft", got)
    spec, key = got["spec"], got["key"]
    origin, why = ctx.origin(key)
    if origin is None:
        return _done(ctx, "propose_card", "draft", error("provenance", why))
    url = spec["source"]["url"]
    ctx.note("propose_card", "loop draft" if ctx.own_proposal else "draft", "ok")   # an audit can tell who proposed
    scope = (ctx.urls.get(key) or {}).get("scope")
    source = {"kind": "page" if got["format"] == _TABLE
              else {"library": "library", "search": "search"}.get(origin or "", "endpoint"),
              "ref": url[:300], "host": (key.split("/")[2])[:253], "why": _ORIGIN_WHY[origin]}
    if isinstance(scope, str) and scope.strip():
        source["scope"] = scope.strip()[:20]  # what the place check had; the refresh re-check reads it back
    recipe = {"question": ctx.question,
              "sources": [source],
              "constraints": _constraints(ctx.question), "tier": _tier(origin, got["format"]),
              "verification": {"verdict": "pass", "checks": got["checks"], "verified_at": ctx.now.isoformat()[:40],
                               "fetches": min(ctx.fetches, 8)},
              "provenance": {"author": "researcher", "built_at": ctx.now.isoformat()[:40]},
              "research": ctx.sealed_trace()}
    try:
        sealed = ni.validate_spec({**spec, "recipe": recipe}, researcher=True)
    except (ValueError, TypeError) as exc:
        return error("invalid_spec", str(exc), limit=MAX_SPEC_DETAIL)
    lands = landing_reason(ctx, sealed, got["outputs"])   # the landing's own rule, on the sealed proposal
    if lands:
        return _done(ctx, "propose_card", "draft", {**error("verify_failed", f"lands: {lands}"),
                                                    "reasons": [f"lands: {lands}"]})
    return {"ok": True, "proposal": {"spec": sealed, "tier": recipe["tier"], "preview": got["preview"]}}


# ---- S7: compare cards ------------------------------------------------------------------------------------

def _compare_things(ctx: ResearchCtx, compare: object) -> tuple[list[dict] | None, dict]:
    """The proposed things as ``[{name, url, path}]`` with each name taken from the QUESTION's own spelling (the model
    only says which asked thing an address is for), or ``(None, a typed error)``. Every thing the question asked must
    be proposed exactly once; every address must be one a Library row or a search handed over, fetched here, and found
    by a search that named that thing and no other asked thing."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    wanted = [str(e) for e in ctx.question.get("entities") or []]
    if len(wanted) < ni_compare.MIN_ENTITIES:
        return None, error("not_a_compare", "the question does not compare named things; use propose_card")
    rows = compare.get("things") if isinstance(compare, dict) else None
    if not isinstance(rows, list) or len(rows) != len(wanted) or not all(
            isinstance(r, dict) and all(isinstance(r.get(k), str) for k in ("name", "url", "path")) for r in rows):
        return None, error("invalid_args", f"things must list each of: {', '.join(wanted)} (name, url, path)")
    things: list[dict] = []
    for row in rows:  # bounded by ni_compare.MAX_ENTITIES
        hit = ni_compare.named_in(wanted, row["name"])
        if len(hit) != 1 or any(t["name"] == hit[0] for t in things):
            return None, error("invalid_args", f"name {one_line(row['name'], 60)!r} is not exactly one asked thing, once")
        err = _compare_address(ctx, hit[0], wanted, row["url"])
        if err is not None:
            return None, err
        things.append({"name": hit[0], "url": normal_url(row["url"]), "path": row["path"].strip()})
    return things, {}


def _compare_address(ctx: ResearchCtx, name: str, wanted: list[str], url: str) -> dict | None:
    """A typed error when ``url`` may not stand for ``name`` in a compare (provenance, sample, search), else None."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert name in wanted, "an asked thing"
    origin, why = ctx.origin(url)
    if origin is None:
        return error("provenance", why)
    info = ctx.urls.get(normal_url(url)) or {}
    if info.get("blocked"):
        return error("needs_access", "this source needs a key or contact email")
    if normal_url(url) not in ctx.samples:
        return error("no_sample", f"fetch_sample the address for {name} first")
    queries = [str(q) for q in info.get("queries") or []]   # bounded by ctx._MAX_QUERIES
    for query in queries:
        named = ni_compare.named_in(wanted, query)
        if named == [name]:
            return None
        # a search that named several asked things may hand over one response that serves them all (a table of
        # rates, one page of forecasts): the address then stands for each thing only when its sample names that thing
        if len(named) > 1 and name in named and ni_compare.mentions(ctx.samples[normal_url(url)]["parsed"], name,
                                                                             [w for w in wanted if w != name]):
            return None
    seen = "; ".join(one_line(q, 60) for q in queries[:3]) or "none"
    return error("wrong_thing", f"the address for {name} must come from a search whose query names {name} and no "
                 f"other asked thing; this address was found by: {seen}. Run web_search or library_search for "
                 f"{name} alone and use one of its results", limit=MAX_SPEC_DETAIL)


def _compare_values(ctx: ResearchCtx, things: list[dict]) -> tuple[list | None, str, dict]:
    """``(numbers, format, error)``: the number at each thing's path in its stored sample; one format throughout."""
    assert things, "things required"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    fmts = {ctx.samples[t["url"]]["format"] for t in things}
    if _TABLE in fmts:
        return None, "", error("invalid_spec", "a page table is built with propose_card, not propose_compare")
    if len(fmts) != 1:
        return None, "", error("invalid_spec", "every thing's address must answer in the same format")
    values = []
    for t in things:  # bounded
        got = ni_compare.number_at(ctx.samples[t["url"]]["parsed"], t["path"])
        if got is None:
            return None, "", error("extract_miss", f"no number at {one_line(t['path'], 80)!r} in the sample for {t['name']}")
        values.append(got)
    return values, next(iter(fmts)), {}


def _compare_spec(ctx: ResearchCtx, things: list[dict], values: list, fmt: str, label: str, unit: str | None
                  ) -> tuple[dict | None, dict]:
    """The sealed compare draft and its outputs (``{spec, outputs}``), or ``(None, a typed error)``."""
    assert len(things) == len(values), "a number per thing"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    cadence = ni_recipe.CADENCE_MINUTES.get(ctx.question.get("cadence") or "hourly", 60)
    title = ni_compare.title_of([t["name"] for t in things], label)
    built = ni_compare.build(things, values, label=label, unit=unit, title=title, ask=ctx.request, now=ctx.now,
                             cadence_s=cadence * 60, viewer_tz=str(getattr(ctx.zone, "key", None) or "UTC"))
    if built is None:
        return None, error("not_displayable", "these readings can't be laid out side by side")
    from ..ni_forms.form_scene import display_size_for_span
    draft = {"version": 1, "title": title, "goal": ctx.request[:ni._MAX_GOAL], "params": {},
             "source": {"type": "http_json", "url": things[0]["url"], **({"format": fmt} if fmt != "json" else {}),
                        "compare": {"entities": things}},
             "pipeline": built["pipeline"], "scene": built["scene"],
             "display": {"size": display_size_for_span(str(built["scene"]["spans"]["desktop"]))}, "model": None}
    try:
        spec = ni.validate_spec(copy.deepcopy(draft), researcher=True)
    except (ValueError, TypeError, AssertionError) as exc:
        return None, error("invalid_spec", str(exc))
    return {"spec": spec, "outputs": built["preview_payload"]}, {}


def propose_compare(ctx: ResearchCtx, compare: object) -> dict:
    """The terminal tool for an ask that compares two to four named things: one address per thing, one path to a
    number in each, the same metric. The program checks every address's provenance and its search, reads the numbers
    from the samples already fetched, names each row from the question (never from the model) and seals the card
    with the same checks and recipe as ``propose_card``. A thing missing, a number missing or a check failing is a
    typed error, never a card with fewer things."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    out = ctx.spend("propose_compare")
    if out is not None:
        return out
    things, err = _compare_things(ctx, compare)
    if things is None:
        return _done(ctx, "propose_compare", "compare", err)
    values, fmt, err = _compare_values(ctx, things)
    if values is None:
        return _done(ctx, "propose_compare", "compare", err)
    label = ni_compare.measure_label(ctx.request, [t["name"] for t in things])  # the question's words, never the model's
    unit = ni_compare.clean_unit(compare.get("unit") if isinstance(compare, dict) else None)  # type: ignore[union-attr]
    built, err = _compare_spec(ctx, things, values, fmt, label, unit)
    if built is None:
        return _done(ctx, "propose_compare", "compare", err)
    checks = [*_checks(ctx, built["outputs"], things[0]["url"], ctx.urls.get(things[0]["url"]) or {}),
              _stable([t["url"] for t in things], ctx.now)]
    if any(not c["ok"] for c in checks):
        reasons = [f"{c['check']}: {c['reason']}" for c in checks if not c["ok"]]
        return _done(ctx, "propose_compare", "compare", {**error("verify_failed", "; ".join(reasons)), "reasons": reasons})
    return _seal_compare(ctx, built, things, fmt, checks)


def _seal_compare(ctx: ResearchCtx, built: dict, things: list[dict], fmt: str, checks: list[dict]) -> dict:
    """The proposal for a compare that passed its checks: recipe (one source per thing), tier and trace."""
    assert things and checks, "things and checks required"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    origins = [ctx.origin(t["url"])[0] or "preview" for t in things]
    kinds = [{"library": "library", "search": "search"}.get(o, "endpoint") for o in origins]
    tier = "curated" if all(o == "library" for o in origins) else _tier("search", fmt)
    ctx.note("propose_compare", "compare", "ok")
    recipe = {"question": ctx.question,
              "sources": [{"kind": k, "ref": t["url"][:300], "host": (t["url"].split("/")[2])[:253],
                           "why": f"{_ORIGIN_WHY[o]} for {t['name']}"[:200]}
                          for k, o, t in zip(kinds, origins, things, strict=True)],
              "constraints": _constraints(ctx.question), "tier": tier,
              "verification": {"verdict": "pass", "checks": checks, "verified_at": ctx.now.isoformat()[:40],
                               "fetches": min(ctx.fetches, 8)},
              "provenance": {"author": "researcher", "built_at": ctx.now.isoformat()[:40]},
              "research": ctx.sealed_trace()}
    try:
        sealed = ni.validate_spec({**built["spec"], "recipe": recipe}, researcher=True)
    except (ValueError, TypeError) as exc:
        return error("invalid_spec", str(exc))
    return {"ok": True, "proposal": {"spec": sealed, "tier": tier, "preview": _bounded(built["outputs"])}}
