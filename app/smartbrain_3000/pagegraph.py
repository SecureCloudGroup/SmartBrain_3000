"""Page graph — turn any web page into structured, USABLE data (platform P1).

The reusable page-understanding component (design round 10). One guarded
fetch + one jailed parse yields a deterministic PageGraph:

    {url, fetched_at, render_mode, text, title,
     entities[],   # schema.org JSON-LD, flattened scalar fields
     tables[],     # {caption, headers[], rows[][]} typed grids
     feeds[],      # RSS/Atom autodiscovery hrefs
     meta{},       # OpenGraph/description pairs
     outline[]}    # h1-h3 headings

Consumers: the NI flow's search-evaluate step (rank candidates by what their
pages actually CONTAIN, with evidence previews), the P2 compiler (selector
programs over graph paths), chat research tools, future ingestion. All layers
are parsed INSIDE the subprocess jail (hostile HTML never parses in-process);
this module only fetches under netguard and validates the jail's bounded
output. ``render_mode`` is "static" until the P3 browser ladder lands.

``graph_fitness`` is the deterministic scorer the evaluate step uses: how
well a page's STRUCTURED content serves a set of wants, with grounded
evidence strings (label: value pairs lifted verbatim from the page's own
entities/tables — never model-authored).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from . import jailrun, netguard

_FETCH_DEADLINE_S = 15.0
_MAX_WANT_TOKENS = 24
_MAX_EVIDENCE = 2
_MAX_EVIDENCE_CHARS = 90

# Tokens too generic to indicate topical fit on their own (mirrors the NI
# coverage-generic posture; kept local so pagegraph stays NI-independent).
_GENERIC_TOKENS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "show", "me", "my", "a", "an", "of", "in",
    "on", "to", "any", "all", "data", "info", "information", "current",
    "latest", "today", "daily", "every", "update", "updates", "live", "now",
    "price", "prices", "value", "values", "time", "times",
})

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def fetch_page_graph(url: str, *, fetcher=None) -> dict:
    """Fetch ``url`` under netguard and return its PageGraph.

    Headerless by design — this is the SEARCH-side reader (the round-10
    consent ruling: searching includes reading results); credentialed page
    fetches stay with the engine's ``ni._fetch_http_page``. Raises
    ``netguard.FetchError`` on fetch refusals and ``jailrun.JailError`` on
    extraction failures — callers degrade per their own contract.
    """
    assert isinstance(url, str) and url, "url required"
    do_fetch = fetcher if fetcher is not None else netguard.safe_fetch_page
    got = do_fetch(url, deadline_seconds=_FETCH_DEADLINE_S)
    body = got.get("content") if isinstance(got, dict) else None
    if not isinstance(body, (bytes, bytearray)):
        raise netguard.FetchError("no bytes in page response")
    extracted = jailrun.run_extractor(bytes(body), url_hint=url)
    return {
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "render_mode": "static",
        "text": str(extracted.get("text") or ""),
        "title": str(extracted.get("title") or ""),
        "entities": list(extracted.get("entities") or []),
        "tables": list(extracted.get("tables") or []),
        "feeds": list(extracted.get("feeds") or []),
        "meta": dict(extracted.get("meta") or {}),
        "outline": list(extracted.get("outline") or []),
    }


def _want_tokens(wants: list[str]) -> list[str]:
    """Distinctive lowercase tokens from the wants (generic words dropped)."""
    assert isinstance(wants, list), "wants must be a list"
    seen: list[str] = []
    for want in wants[:8]:
        if not isinstance(want, str):
            continue
        for tok in _TOKEN_RE.findall(want.lower()):
            if tok not in _GENERIC_TOKENS and tok not in seen:
                seen.append(tok)
            if len(seen) >= _MAX_WANT_TOKENS:
                return seen
    return seen


def _evidence_line(label: str, value: str) -> str:
    label = " ".join(str(label).split())[:40]
    value = " ".join(str(value).split())[:_MAX_EVIDENCE_CHARS]
    return f"{label}: {value}" if label else value


def graph_fitness(graph: dict, wants: list[str]) -> tuple[int, list[str]]:
    """Score how well a PageGraph SERVES the wants; return (score, evidence).

    Deterministic, structure-weighted: a want token matching an entity field
    or a table cell/header (data the page itself structured) scores far above
    a plain-text mention. Evidence strings are lifted VERBATIM from matched
    structured nodes — grounded by construction, shown on the pick card
    before any tap. Score 0 means the page shows no sign of serving the ask.
    """
    assert isinstance(graph, dict), "graph required"
    tokens = _want_tokens(wants)
    if not tokens:
        return 0, []
    score = 0
    evidence: list[str] = []

    def _hit(text: str) -> bool:
        low = str(text).lower()
        return any(tok in low for tok in tokens)

    for ent in graph.get("entities") or []:  # bounded by the jail's caps
        if not isinstance(ent, dict):
            continue
        matched = [(k, v) for k, v in ent.items()
                   if k != "type" and (_hit(k) or _hit(v))]
        if matched:
            score += 5 + 2 * min(len(matched), 3)
            if len(evidence) < _MAX_EVIDENCE:
                evidence.append(_evidence_line(*matched[0]))
    for table in graph.get("tables") or []:
        if not isinstance(table, dict):
            continue
        headers = [str(h) for h in (table.get("headers") or [])]
        header_hit = any(_hit(h) for h in headers)
        row_hit_value = ""
        for row in (table.get("rows") or [])[:10]:
            for i, cell in enumerate(row):
                if _hit(cell):
                    label = headers[i] if i < len(headers) else "row"
                    row_hit_value = _evidence_line(label, cell)
                    break
            if row_hit_value:
                break
        if header_hit or row_hit_value:
            score += 6 if header_hit else 3
            if row_hit_value and len(evidence) < _MAX_EVIDENCE:
                evidence.append(row_hit_value)
            elif header_hit and not row_hit_value and (table.get("rows") or []):
                # A matching column with data: first data cell as evidence.
                col = next((i for i, h in enumerate(headers) if _hit(h)), 0)
                first = (table["rows"][0] or [""])
                cell = first[col] if col < len(first) else ""
                if cell and len(evidence) < _MAX_EVIDENCE:
                    evidence.append(_evidence_line(headers[col], cell))
    if _hit(graph.get("title") or ""):
        score += 3
    for value in (graph.get("meta") or {}).values():
        if _hit(value):
            score += 2
            break
    for line in graph.get("outline") or []:
        if _hit(line):
            score += 2
            break
    if score == 0 and _hit(graph.get("text") or ""):
        score = 1  # text-only mention: last-resort signal, never strong
    return score, evidence[:_MAX_EVIDENCE]
