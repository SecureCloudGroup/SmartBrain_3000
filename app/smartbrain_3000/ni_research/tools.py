"""The five code-run research tools (plan 6.1, 6.4). No model, no product caller yet (S4c wires the loop).

Each tool spends a turn on the ``ResearchCtx``, does its work in code and returns a typed result:
``{"ok": True, ...}`` or ``{"ok": False, "error": kind, "detail": ...}``. Fetches go through the one shared
fetch-and-parse path (``ni_fetch``, netguard underneath) with no headers, no POST and no redirects.
"""
from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

from .. import ni, ni_brief, ni_compare, ni_fetch, ni_polite, ni_recipe, pagegraph
from ..netguard import FetchError
from ..ni_verify.classes import shadow_reasons
from ..ni_verify.rows import SourceInfo, read_result
from .ctx import ResearchCtx, error, normal_url, one_line
from .digest import (
    MAX_SEARCH_ROWS,
    clean_search_rows,
    clean_text,
    digest,
    search_digest,
)

MAX_QUERY = 200
MAX_DRAFT_BYTES = 32 * 1024
MAX_PREVIEW_CHARS = 4000
MAX_PREVIEW_ROWS = 10
MAX_CANDIDATES = 5
MAX_SAMPLES = 8
_STRUCTURED = frozenset({"json", "csv", "feed", "xml"})
_VERIFY_CLASSES = {"window": "window", "threshold": "threshold", "compare": "compare", "scope": "place"}
_ORIGIN_WHY = {"library": "a Library source", "search": "a search result", "preview": "the previewed endpoint"}


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
    return _done(ctx, "fetch_sample", url, _fetch_and_digest(ctx, normal_url(url), info.get("format"),
                                                              robots=origin != "library"))


def _fetch_and_digest(ctx: ResearchCtx, url: str, fmt: str | None, robots: bool = True) -> dict:
    """Fetch ``url`` (a sealed ``fmt`` from the Library, else sniffed), keep the sample, digest it. ``robots``:
    a source that is not a Library row obeys its host's robots.txt, whichever fetcher reads it."""
    assert isinstance(url, str) and url, "url required"
    assert fmt is None or isinstance(fmt, str), "format is text"
    if fmt == "html":
        return error("not_data", "an HTML page; research reads JSON, CSV, feeds and XML")
    try:
        with ni_polite.engaged(robots=robots):
            if fmt in _STRUCTURED or fmt == "text":
                parsed, seen = ni_fetch.fetch_parsed(url, fmt, allow_redirects=False), fmt
            else:
                parsed, seen = ni_fetch.sniff_fetch(url, allow_redirects=False)
    except FetchError as exc:
        text = str(exc).lower()
        if exc.kind == "robots":
            return error("robots", "the site's robots.txt asks automated readers to stay out")
        if "redirect" in text:
            return error("redirect", "the source redirected; redirects are not followed")
        if exc.kind in ("not_json", "not_text", "not_csv", "not_feed", "not_xml"):
            return error("not_data", "not a data file (a web page?)")
        return error("fetch_failed", clean_text(str(exc), 120))
    if len(ctx.samples) >= MAX_SAMPLES:
        ctx.samples.pop(next(iter(ctx.samples)))
    ctx.samples[normal_url(url)] = {"format": seen, "parsed": parsed}
    return {"ok": True, "url": url, "format": seen, "digest": digest(url, seen, parsed),
            "fetches_left": ctx.max_fetches - ctx.fetches}


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
    return _done(ctx, "web_search", asked, {"ok": True, "results": len(rows), "digest": search_digest(asked, rows),
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
                   title=one_line(row.get("title") or "", 120), blocked=blocked, query=query.strip())
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


def checks_for(ctx: ResearchCtx, outputs: dict, url: str, info: dict) -> list[dict]:
    """The recipe checks for ``outputs`` (the verifier ``try_recipe`` and ``propose_card`` run), for the refresh
    re-check (S9): the same classes on a fresh payload, so a constraint cannot pass at Keep by a different rule."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert isinstance(outputs, dict), "outputs must be a dict"
    return _checks(ctx, outputs, url, info)


def sample_payload(parsed: object) -> object:
    """A sample as the engine reads it at refresh: a bare list is wrapped as ``{"items": [...]}``."""
    return ni_brief.normalize(parsed) or parsed


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
    try:
        spec = ni.validate_spec(copy.deepcopy(draft), researcher=True)
    except (ValueError, TypeError, AssertionError) as exc:
        return error("invalid_spec", str(exc))
    source = spec.get("source") or {}
    if source.get("type") != "http_json" or source.get("headers") or source.get("secret_query"):
        return error("invalid_spec", "a researcher draft reads one plain http_json source")
    key = normal_url(str(source.get("url") or ""))
    sample = ctx.samples.get(key)
    if sample is None:
        return error("no_sample", "fetch_sample the source URL first")
    if (source.get("format") or "json") != sample["format"]:
        return error("invalid_spec", f"source.format must be {sample['format']} like the sample")
    spec = brief_spec(ctx, spec, sample["parsed"])
    try:
        outputs = ni.run_pipeline(copy.deepcopy(spec["pipeline"]), copy.deepcopy(sample_payload(sample["parsed"])))
    except ni.NIError as exc:
        return error("pipeline_failed", f"{exc.kind}: {exc.detail}")
    info = ctx.urls.get(key) or {}
    checks = _checks(ctx, outputs, source["url"], info)
    preview = _bounded(outputs)
    if len(json.dumps(preview, default=str)) > MAX_PREVIEW_CHARS:
        preview = _bounded_small(outputs)
    return {"ok": True, "spec": spec, "key": key, "format": sample["format"], "checks": checks, "preview": preview}


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
    if origin == "library":
        return "curated"
    return "verified_endpoint" if fmt in _STRUCTURED else "page_reading"


def propose_card(ctx: ResearchCtx, draft_spec: object) -> dict:
    """The terminal tool: re-run the draft and, when every check passes, seal the proposal (recipe, tier, trace)."""
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    assert ctx.max_turns > 0, "a turn budget"
    out = ctx.spend("propose_card")
    if out is not None:
        return out
    got = _run(ctx, draft_spec)
    if got["ok"] and any(not c["ok"] for c in got["checks"]):
        reasons = [f"{c['check']}: {c['reason']}" for c in got["checks"] if not c["ok"]]
        got = error("verify_failed", "; ".join(reasons))
        got["reasons"] = reasons
    if not got["ok"]:
        return _done(ctx, "propose_card", "draft", got)
    spec, key = got["spec"], got["key"]
    origin, why = ctx.origin(key)
    if origin is None:
        return _done(ctx, "propose_card", "draft", error("provenance", why))
    url = spec["source"]["url"]
    ctx.note("propose_card", "draft", "ok")
    scope = (ctx.urls.get(key) or {}).get("scope")
    source = {"kind": {"library": "library", "search": "search"}.get(origin or "", "endpoint"),
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
        return error("invalid_spec", str(exc))
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
        hit = [w for w in wanted if ni_compare.names(w, row["name"])]
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
    others = [w for w in wanted if w != name]
    for query in info.get("queries") or []:  # bounded by ctx._MAX_QUERIES
        if ni_compare.names(name, query) and not any(ni_compare.names(o, query) for o in others):
            return None
    return error("wrong_thing", f"the address for {name} must come from a search for {name} alone")


def _compare_values(ctx: ResearchCtx, things: list[dict]) -> tuple[list | None, str, dict]:
    """``(numbers, format, error)``: the number at each thing's path in its stored sample; one format throughout."""
    assert things, "things required"
    assert isinstance(ctx, ResearchCtx), "a research ctx"
    fmts = {ctx.samples[t["url"]]["format"] for t in things}
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
    checks = _checks(ctx, built["outputs"], things[0]["url"], ctx.urls.get(things[0]["url"]) or {})
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
