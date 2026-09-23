"""Page-graph platform tests (round 10 P1) + the W1 extraction-fidelity seed.

W1 (the acceptance framework's CI leg): recorded pages — miniature but
REAL-shaped documents in the SWDE genre (JSON-LD entities, data tables, feed
autodiscovery, OpenGraph meta, heading outlines) — each with hand-verified
ground truth. The suite grows a row per page-template class; regressions
here mean the platform stopped understanding a whole class of pages, not one
site. Everything runs the REAL subprocess jail (no parser stubs): the child
process parses hostile HTML, the parent validates the closed payload.
"""

from __future__ import annotations

import json

import pytest

from smartbrain_3000 import jailrun, netguard, pagegraph

# ---- W1 recorded pages (ground truth beside each) -------------------------

RECORDED_EVENT_PAGE = b"""<html><head><title>Riverside Regatta 2026</title>
<meta property="og:description" content="Annual riverside sailing regatta">
<link rel="alternate" type="application/rss+xml" href="/events.xml">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event","name":"Riverside Regatta",
 "startDate":"2026-10-03T09:00","location":{"@type":"Place","name":"River Park"}}
</script></head>
<body><h1>Riverside Regatta</h1><h2>Schedule</h2>
<table><caption>Race schedule</caption>
<tr><th>Race</th><th>Start</th></tr>
<tr><td>Junior heats</td><td>9:00 AM</td></tr>
<tr><td>Open final</td><td>2:30 PM</td></tr></table>
<p>The annual regatta returns with two full race days of sailing on the
river, food stalls along the bank, and free entry for spectators.</p>
</body></html>"""

RECORDED_GRAPH_PAGE = b"""<html><head><title>Museum of Tides</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Museum","name":"Museum of Tides","telephone":"+1-555-0100"},
 {"@type":"Article","headline":"Spring tide season opens","datePublished":"2026-03-01"}]}
</script></head>
<body><h1>Museum of Tides</h1>
<p>Exhibits about coastal tides and the moon, open daily with guided tours
every morning and afternoon for visitors of all ages.</p></body></html>"""

RECORDED_PLAIN_ARTICLE = b"""<html><head><title>Plain Article</title></head>
<body><h1>A plain article</h1>
<p>This page ships no structured data at all - no JSON-LD, no tables, no
feeds - only prose. The graph layers must come back EMPTY, never invented,
while text extraction still works on this real paragraph of content.</p>
</body></html>"""


def _graph_of(html: bytes, url: str = "https://example.org/page") -> dict:
    return pagegraph.fetch_page_graph(
        url, fetcher=lambda u, deadline_seconds: {"content": html})


# ---- W1 rows ---------------------------------------------------------------


def test_w1_event_page_full_ground_truth() -> None:
    """Entities, table, feed, meta and outline from one recorded event page."""
    g = _graph_of(RECORDED_EVENT_PAGE)
    # trafilatura metadata prefers the h1 over <title> — recorded as-is.
    assert g["title"] == "Riverside Regatta"
    assert g["render_mode"] == "static"
    ent = g["entities"][0]
    assert ent["type"] == "Event" and ent["name"] == "Riverside Regatta"
    assert ent["startDate"] == "2026-10-03T09:00"
    table = g["tables"][0]
    assert table["headers"] == ["Race", "Start"]
    assert table["rows"] == [["Junior heats", "9:00 AM"], ["Open final", "2:30 PM"]]
    assert g["feeds"] == ["/events.xml"]
    assert g["meta"].get("og:description") == "Annual riverside sailing regatta"
    assert g["outline"][:2] == ["h1: Riverside Regatta", "h2: Schedule"]
    assert "regatta returns" in g["text"]


def test_w1_jsonld_graph_array_yields_both_entities() -> None:
    """@graph wrappers (very common on real sites) unwrap to their members."""
    g = _graph_of(RECORDED_GRAPH_PAGE)
    types = {e["type"] for e in g["entities"]}
    assert types == {"Museum", "Article"}
    by_type = {e["type"]: e for e in g["entities"]}
    assert by_type["Museum"]["telephone"] == "+1-555-0100"
    assert by_type["Article"]["headline"] == "Spring tide season opens"


def test_w1_plain_page_layers_empty_never_invented() -> None:
    g = _graph_of(RECORDED_PLAIN_ARTICLE)
    assert g["entities"] == [] and g["tables"] == [] and g["feeds"] == []
    assert g["outline"] == ["h1: A plain article"]
    assert "no structured data" in g["text"]


def test_w1_hostile_page_degrades_to_empty_layers_not_a_crash() -> None:
    """Malformed markup and poisoned JSON-LD must never break extraction."""
    hostile = (b"<html><head><title>Broken</title>"
               b'<script type="application/ld+json">{not json at all]</script>'
               b"</head><body><h1>Still<table><tr><td>works"
               b"<p>Unclosed tags everywhere, and a paragraph of prose long "
               b"enough that the text extractor keeps it around anyway.</p>")
    g = _graph_of(hostile)
    assert g["entities"] == []  # poisoned blob dropped, not fatal
    assert isinstance(g["tables"], list) and isinstance(g["outline"], list)
    assert isinstance(g["title"], str) and g["title"]


def test_w1_jsonld_recursion_bomb_cannot_kill_extraction() -> None:
    """Adversarial review 2026-09-22 (verified live pre-fix): ~13 KB of
    nested empty ``@graph`` shells blew Python's recursion limit INSIDE the
    jail child and killed text/title with it — a regression for every
    consented page fetch, not just the graph. The walk is iterative and
    budgeted now: hollow shells spend a counter, real entities still
    extract, and prose extraction never dies."""
    bomb = b'{"@graph":[' * 1200 + b'{}' + b']}' * 1200
    page = (b"<html><head><title>Bombed</title>"
            b'<script type="application/ld+json">' + bomb + b"</script>"
            b'<script type="application/ld+json">'
            b'{"@type":"Event","name":"Survivor"}</script></head>'
            b"<body><h1>Bombed</h1><p>A real paragraph of prose that must "
            b"still extract cleanly after the hostile blob is absorbed by "
            b"the bounded walker.</p></body></html>")
    g = _graph_of(page)
    assert "still extract cleanly" in g["text"]  # the text path SURVIVED
    assert {"type": "Event", "name": "Survivor"} in g["entities"]


# ---- platform contract ------------------------------------------------------


def test_fetch_page_graph_contract_shape() -> None:
    g = _graph_of(RECORDED_EVENT_PAGE)
    assert set(g) == {"url", "fetched_at", "render_mode", "text", "title",
                      "entities", "tables", "feeds", "meta", "outline"}
    assert g["url"] == "https://example.org/page"


def test_fetch_page_graph_no_bytes_raises_fetcherror() -> None:
    with pytest.raises(netguard.FetchError):
        pagegraph.fetch_page_graph(
            "https://example.org/x",
            fetcher=lambda u, deadline_seconds: {"content": None})


def test_jail_validator_rejects_smuggled_graph_keys() -> None:
    """A compromised child cannot widen the payload: unknown keys and wrong
    types both fail closed."""
    with pytest.raises(jailrun.JailError):
        jailrun._validate_payload(json.dumps(
            {"text": "t", "title": "x", "entities": [], "smuggled": 1}))
    with pytest.raises(jailrun.JailError):
        jailrun._validate_payload(json.dumps(
            {"text": "t", "title": "x", "meta": ["not", "a", "dict"]}))
    out = jailrun._validate_payload(json.dumps(
        {"text": "t", "title": "x", "entities": [], "meta": {}}))
    assert out["entities"] == []


# ---- fitness scorer ---------------------------------------------------------


def _mini_graph(**layers) -> dict:
    base = {"url": "https://x.example.org/", "fetched_at": "now",
            "render_mode": "static", "text": "", "title": "",
            "entities": [], "tables": [], "feeds": [], "meta": {},
            "outline": []}
    base.update(layers)
    return base


def test_graph_fitness_structure_outranks_prose_mentions() -> None:
    structured = _mini_graph(entities=[{"type": "Event", "name": "High Tide"}])
    prose_only = _mini_graph(text="somewhere in prose a tide is mentioned")
    s1, ev1 = pagegraph.graph_fitness(structured, ["tide times"])
    s2, ev2 = pagegraph.graph_fitness(prose_only, ["tide times"])
    assert s1 > s2 > 0
    assert ev1 == ["name: High Tide"] and ev2 == []


def test_graph_fitness_table_evidence_is_verbatim_cell_content() -> None:
    g = _mini_graph(tables=[{"caption": "", "headers": ["Time", "Tide"],
                             "rows": [["7:12 AM", "High"], ["1:33 PM", "Low"]]}])
    score, evidence = pagegraph.graph_fitness(g, ["tide levels"])
    assert score > 0
    assert evidence == ["Tide: High"]  # header hit → first data cell, verbatim


def test_graph_fitness_generic_wants_never_score() -> None:
    """All-generic wants ("current data", "latest info") must not make every
    page look fit — no tokens, no score, by construction."""
    g = _mini_graph(text="current data latest info update",
                    title="Current data updates")
    assert pagegraph.graph_fitness(g, ["current data", "latest info"]) == (0, [])


def test_graph_fitness_off_topic_page_scores_zero() -> None:
    g = _mini_graph(entities=[{"type": "Product", "name": "Blue Kayak"}],
                    text="paddling gear for sale")
    assert pagegraph.graph_fitness(g, ["tide times"])[0] == 0
