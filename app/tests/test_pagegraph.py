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


# ---- P2 selector programs (compiled page cards) ----------------------------


_TIDE_GRAPH = _mini_graph(
    title="Creek Tides",
    meta={"og:description": "Daily tide predictions"},
    entities=[{"type": "Event", "name": "High Tide", "startDate": "07:12"}],
    tables=[{"caption": "", "headers": ["Time", "Height", "Tide"],
             "rows": [["7:12 AM", "5.8 ft", "High"], ["1:33 PM", "0.4 ft", "Low"]]}],
    outline=["h1: Tide Tables"])


def test_selector_grammar_is_closed() -> None:
    ok = {"kind": "table_lookup", "table": 0, "where": "Tide",
          "equals": "High", "take": "Time"}
    assert pagegraph.validate_selector(ok) is ok
    bad = [
        {"kind": "xpath", "expr": "//td"},                       # unknown kind
        {"kind": "meta", "key": "x", "extra": 1},                 # unknown key
        {"kind": "table_cell", "table": 0, "row": "0", "col": 0},  # wrong type
        {"kind": "table_cell", "table": 0, "row": True, "col": 0},  # bool ≠ int
        {"kind": "outline", "index": 999},                         # out of range
        {"kind": "entity", "etype": "", "field": "name"},          # empty str
        "not an object",
    ]
    for sel in bad:
        with pytest.raises(ValueError):
            pagegraph.validate_selector(sel)


def test_menu_values_match_program_execution() -> None:
    """Every menu entry's shown value is exactly what its selector yields —
    the model picks by the value it SEES, and the engine gets that value."""
    menu = pagegraph.enumerate_menu(_TIDE_GRAPH)
    assert menu and len(menu) <= 80
    assert {m["selector"]["kind"] for m in menu} >= {
        "entity", "table_lookup", "meta", "title", "outline"}
    for m in menu:
        pagegraph.validate_selector(m["selector"])
        assert pagegraph.run_selector(_TIDE_GRAPH, m["selector"]) == m["value"]


def test_table_lookup_survives_cosmetic_drift_and_names_real_drift() -> None:
    sel = {"kind": "table_lookup", "table": 0, "where": "Tide",
           "equals": "High", "take": "Time"}
    assert pagegraph.run_selector(_TIDE_GRAPH, sel) == "7:12 AM"
    reordered = _mini_graph(tables=[{
        "caption": "", "headers": ["Tide", "Time"],
        "rows": [["Low", "1:33 PM"], ["High", "7:12 AM"]]}])
    assert pagegraph.run_selector(reordered, sel) == "7:12 AM"  # rows+cols moved
    for broken in (_mini_graph(tables=[]),
                   _mini_graph(tables=[{"caption": "", "headers": ["When"],
                                        "rows": [["7:12"]]}]),
                   _mini_graph(tables=[{"caption": "", "headers": ["Tide", "Time"],
                                        "rows": [["Low", "1:33 PM"]]}])):
        with pytest.raises(pagegraph.GraphDrift):
            pagegraph.run_selector(broken, sel)


def test_entity_selector_is_type_addressed() -> None:
    sel = {"kind": "entity", "etype": "Event", "field": "startDate"}
    shuffled = _mini_graph(entities=[{"type": "Organization", "name": "X"},
                                     {"type": "Event", "startDate": "08:01"}])
    assert pagegraph.run_selector(shuffled, sel) == "08:01"
    with pytest.raises(pagegraph.GraphDrift):
        pagegraph.run_selector(_mini_graph(entities=[{"type": "Organization",
                                                      "name": "X"}]), sel)


def test_menu_reaches_deep_rows_by_want_and_never_emits_invalid() -> None:
    """Live-probe findings (2026-09-23): (1) a big table starved the menu —
    a want about row 150 was unreachable; (2) an unnamed column produced a
    selector the grammar rejects. Want-matching row keys now come first,
    each table's share is capped, and every entry validates."""
    rows = [[str(i), f"Country{i}", f"{i * 1000}"] for i in range(200)]
    rows[150] = ["150", "Iceland", "383,726"]
    big = _mini_graph(
        title="Populations", meta={"og:description": "By country"},
        tables=[{"caption": "", "headers": ["", "Location", "Population"],
                 "rows": rows}])
    menu = pagegraph.enumerate_menu(big, ["Iceland population"])
    for m in menu:
        pagegraph.validate_selector(m["selector"])  # nothing invalid emitted
        assert m["selector"].get("where") != ""
    hit = [m for m in menu if m["value"] == "383,726"]
    assert hit and hit[0]["selector"]["equals"] == "Iceland"
    kinds = {m["selector"]["kind"] for m in menu}
    assert {"meta", "title"} <= kinds  # the table did not starve the rest


def test_w1_long_table_rows_reach_the_graph() -> None:
    """List pages run long: row 150 of a real table must be addressable
    (the old 40-row cap made it invisible to the whole platform)."""
    rows = "".join(f"<tr><td>{i}</td><td>Place{i}</td><td>{i * 7}</td></tr>"
                   for i in range(300))
    page = (b"<html><head><title>Long</title></head><body><h1>Long</h1>"
            b"<table><tr><th>Rank</th><th>Name</th><th>Score</th></tr>"
            + rows.encode() + b"</table><p>Prose long enough to be kept by "
            b"the extractor as the main text of this list page.</p></body></html>")
    g = _graph_of(page)
    names = [r[1] for r in g["tables"][0]["rows"]]
    assert "Place150" in names and len(names) >= 300


def test_w1_graph_overflow_trims_tables_never_the_text() -> None:
    """A table big enough to blow the 1 MB jail pipe is trimmed in the child
    — the text path survives; before, the overflow failed the whole read."""
    cell = "x" * 110
    rows = "".join("<tr>" + f"<td>{cell}</td>" * 12 + "</tr>" for _ in range(150))
    tables = (b"<table><tr>" + b"".join(f"<th>h{c}</th>".encode() for c in range(12))
              + b"</tr>" + rows.encode() + b"</table>") * 8
    page = (b"<html><head><title>Huge</title></head><body><h1>Huge</h1>"
            + tables + b"<p>The real prose of this enormous page must still "
            b"come through the jail intact after the trim.</p></body></html>")
    assert len(page) < 2 * 1024 * 1024  # under the parent's input cap
    g = _graph_of(page)
    assert g["title"] and isinstance(g["tables"], list)
    total_rows = sum(len(t["rows"]) for t in g["tables"])
    assert total_rows < 8 * 150  # trimmed, not fatal


def test_many_tables_never_starve_meta_and_title() -> None:
    """Review nit (2026-09-23): per-table caps alone let 4+ tables fill the
    whole menu; meta/title are now emitted before tables."""
    tables = [{"caption": "", "headers": ["K", "V"],
               "rows": [[f"k{t}{r}", f"v{t}{r}"] for r in range(10)]}
              for t in range(8)]
    g = _mini_graph(title="T", meta={"og:description": "D"}, tables=tables)
    kinds = {m["selector"]["kind"] for m in pagegraph.enumerate_menu(g)}
    assert {"meta", "title"} <= kinds


def test_value_kind_is_coarse_and_deterministic() -> None:
    kind = pagegraph.value_kind
    for v in ("7:12 AM", "13:05", "7:12 pm", "07:12:30"):
        assert kind(v) == "time", v
    for v in ("2026-10-03", "2026-10-03T09:00", "10/03/2026", "Oct 3, 2026", "3 October 2026"):
        assert kind(v) == "date", v
    for v in ("5.8 ft", "829.8 m", "1,429,404,000", "$1,234.56", "40 kt", "-3.2 °C", "12%", "18.4"):
        assert kind(v) == "numeric", v
    for v in ("Burj Khalifa", "High", "Tropical Storm Fay (40 kt, moving SSW)", "open"):
        assert kind(v) == "text", v
    assert kind("") == kind(None) == kind("   ") == "empty"
