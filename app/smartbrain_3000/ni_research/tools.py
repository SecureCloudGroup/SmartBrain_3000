"""The five code-run research tools (plan 6.1, 6.4). No model, no product caller yet (S4c wires the loop).

Each tool spends a turn on the ``ResearchCtx``, does its work in code and returns a typed result:
``{"ok": True, ...}`` or ``{"ok": False, "error": kind, "detail": ...}``. Fetches go through the one shared
fetch-and-parse path (``ni_fetch``, netguard underneath) with no headers, no POST and no redirects.
"""
from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

from .. import ni, ni_fetch, ni_recipe, pagegraph
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
_ORIGIN_WHY = {"library": "a Library source", "search": "a search result", "preview": "the previewed endpoint",
               "same_domain": "the same site as an earlier result"}


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
    return _done(ctx, "fetch_sample", url, _fetch_and_digest(ctx, normal_url(url), info.get("format")))


def _fetch_and_digest(ctx: ResearchCtx, url: str, fmt: str | None) -> dict:
    """Fetch ``url`` (a sealed ``fmt`` from the Library, else sniffed), keep the sample, digest it."""
    assert isinstance(url, str) and url, "url required"
    assert fmt is None or isinstance(fmt, str), "format is text"
    if fmt == "html":
        return error("not_data", "an HTML page; research reads JSON, CSV, feeds and XML")
    try:
        if fmt in _STRUCTURED or fmt == "text":
            parsed, seen = ni_fetch.fetch_parsed(url, fmt, allow_redirects=False), fmt
        else:
            parsed, seen = ni_fetch.sniff_fetch(url, allow_redirects=False)
    except FetchError as exc:
        text = str(exc).lower()
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
    out = ctx.spend("web_search", "search")
    if out is not None:
        return out
    if "search" not in ctx.allowed_origins:
        return _done(ctx, "web_search", query, error("web_off", "this run reads SmartBrain Library sources only"))
    if not callable(ctx.searcher):
        return _done(ctx, "web_search", query, error("search_unavailable", "no search service is set up"))
    asked = clean_text(query.strip(), MAX_QUERY)
    try:
        got = ctx.searcher(asked, MAX_SEARCH_ROWS)
    except Exception as exc:  # a failed search degrades to a typed error, never a crash
        return _done(ctx, "web_search", query, error("search_unavailable", type(exc).__name__))
    rows = clean_search_rows(got.get("results") if isinstance(got, dict) else None)
    ctx.record_search_results(rows)
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
                   title=one_line(row.get("title") or "", 120), blocked=blocked)
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
    checks = [{"check": check, "ok": not found.get(cls), "reason": one_line("; ".join(found.get(cls) or []), 120)}
              for cls, check in _VERIFY_CLASSES.items()]
    empty = not any(v not in (None, "", [], {}) for v in outputs.values())
    return [*checks, {"check": "empty", "ok": not empty, "reason": "every output is empty" if empty else ""}]


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
    try:
        outputs = ni.run_pipeline(copy.deepcopy(spec["pipeline"]), copy.deepcopy(sample["parsed"]))
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
    origin, _ = ctx.origin(key)
    url = spec["source"]["url"]
    ctx.note("propose_card", "draft", "ok")
    recipe = {"question": ctx.question,
              "sources": [{"kind": {"library": "library", "search": "search"}.get(origin or "", "endpoint"),
                           "ref": url[:300], "host": (key.split("/")[2])[:253], "why": _ORIGIN_WHY[origin or "same_domain"]}],
              "constraints": _constraints(ctx.question), "tier": _tier(origin or "same_domain", got["format"]),
              "verification": {"verdict": "pass", "checks": got["checks"], "verified_at": ctx.now.isoformat()[:40],
                               "fetches": min(ctx.fetches, 8)},
              "provenance": {"author": "researcher", "built_at": ctx.now.isoformat()[:40]},
              "research": ctx.sealed_trace()}
    try:
        sealed = ni.validate_spec({**spec, "recipe": recipe}, researcher=True)
    except (ValueError, TypeError) as exc:
        return error("invalid_spec", str(exc))
    return {"ok": True, "proposal": {"spec": sealed, "tier": recipe["tier"], "preview": got["preview"]}}
