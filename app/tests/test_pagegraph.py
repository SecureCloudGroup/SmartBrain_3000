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

import gzip
import json
from pathlib import Path

import pytest

from smartbrain_3000 import jail_extract, jailrun, netguard, pagegraph

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
                      "entities", "tables", "feeds", "meta", "outline",
                      "readability"}
    assert g["readability"] == {"readable": True, "kind": "ok"}
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


# F6-A (blind-5): the outline list stores "<tag>: <text>" so a page audit can
# read the heading level. A compiled page card lifts the value verbatim — the
# jail label is NOT the heading's text and must never ride onto the card.
def test_outline_selector_strips_the_jail_h_level_prefix() -> None:
    g = _mini_graph(outline=["h1: Interstate 70",
                             "h3: Traffic & Road Conditions"])
    assert pagegraph.run_selector(g, {"kind": "outline", "index": 0}) == \
        "Interstate 70"
    assert pagegraph.run_selector(g, {"kind": "outline", "index": 1}) == \
        "Traffic & Road Conditions"
    menu = pagegraph.enumerate_menu(g)
    outline_entries = [m for m in menu if m["selector"]["kind"] == "outline"]
    assert outline_entries, "expected outline menu entries"
    for entry in outline_entries:
        assert not entry["value"].startswith(("h1:", "h2:", "h3:"))


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


# ---- recorded pages (C10/C11): 50 real pages, 2026-09-29 --------------------
# Recorded with the app's own guarded fetch (honest UA); manifest.json names
# each page's URL. readability.json labels the expected kind per page and the
# widget values an official data page must NOT lose to article extraction.

_PAGES = Path(__file__).parent / "fixtures" / "pages"
_MANIFEST = json.loads((_PAGES / "manifest.json").read_text())
_LABELS = json.loads((_PAGES / "readability.json").read_text(encoding="utf-8"))
_FIRST_PARTY = json.loads((_PAGES / "first_party.json").read_text())
_RECORDED: dict[str, dict] = {}


def _recorded(name: str) -> dict:
    if name not in _RECORDED:
        raw = gzip.decompress((_PAGES / f"{name}.html.gz").read_bytes())
        url = _MANIFEST[name]["url"]
        _RECORDED[name] = pagegraph.graph_from_extract(
            url, jail_extract.extract(raw, url))
    return _RECORDED[name]


def test_recorded_set_is_broad() -> None:
    assert len(_MANIFEST) >= 30
    assert len(_LABELS["normal_articles"]) >= 10


def test_widget_values_survive_the_read() -> None:
    """W1 page fidelity: the data widgets of status / lottery / scoreboard /
    tracker pages reach the text (field: powerball.com's '$409 Million' was
    dropped by the article extractor, so the official page looked empty)."""
    missing = [(page, value) for page, values in _LABELS["widgets"].items()
               for value in values if value not in _recorded(page)["text"]]
    total = sum(len(v) for v in _LABELS["widgets"].values())
    assert total >= 30
    assert missing == [], missing


def test_article_text_comes_first_and_extra_is_bounded() -> None:
    g = _recorded("powerball_home")
    article, extra = pagegraph.split_text(g["text"])
    assert "Powerball" in article and "$409 Million" not in article
    assert "$409 Million" in extra
    assert len(extra) <= jail_extract._MAX_EXTRA_CHARS
    assert pagegraph.split_text("plain text") == ("plain text", "")


def test_readability_kinds_on_recorded_pages() -> None:
    """Unreadable detection 100% on the labeled pages; normal pages read ok."""
    wrong = {name: _recorded(name)["readability"]["kind"]
             for name, kind in _LABELS["kinds"].items()
             if _recorded(name)["readability"]["kind"] != kind}
    assert wrong == {}, wrong
    for name, kind in _LABELS["kinds"].items():
        assert _recorded(name)["readability"]["readable"] is (kind == "ok")


def test_normal_articles_read_ok() -> None:
    bad = [n for n in _LABELS["normal_articles"]
           if not _recorded(n)["readability"]["readable"]]
    assert len(bad) <= 1, bad


_CLOUDFLARE = b"""<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>
<meta http-equiv="refresh" content="390"><style>body{font-family:system-ui}</style>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></head>
<body><div class="main-wrapper"><div class="main-content"><h1>www.example.com</h1>
<h2 id="challenge-running">Verify you are human by completing the action below.</h2>
<noscript><div>Enable JavaScript and cookies to continue</div></noscript>
<div id="challenge-body-text">www.example.com needs to review the security of your
connection before proceeding.</div></div></div>
<div class="footer">Ray ID: <code>8c1f2a3b4d5e6f70</code> Performance &amp; security by
Cloudflare</div></body></html>"""

_SPA_SHELL = b"""<!doctype html><html><head><title>Transit Status</title>
<script src="/static/js/main.4f2a.js"></script></head><body>
<noscript>You need to enable JavaScript to run this app.</noscript>
<div id="root"></div></body></html>"""

_SPA_LOADING = b"""<html><head><title>Line Status</title></head><body>
<h1>Line status</h1><div id="status">Loading current service status...</div>
<footer>Privacy | Terms | Contact</footer></body></html>"""


def test_readability_challenge_shell_modal_binary() -> None:
    assert _graph_of(_CLOUDFLARE)["readability"] == {"readable": False,
                                                     "kind": "challenge"}
    assert _graph_of(_SPA_SHELL)["readability"]["kind"] == "shell"
    assert _graph_of(_SPA_LOADING)["readability"]["kind"] == "shell"
    assert _recorded("flightaware_aal100")["readability"]["kind"] == "modal"
    mojibake = pagegraph.readability(_mini_graph(
        text="".join(chr(c) for c in range(0x80, 0x2c0)) * 4 + "\ufffd" * 400))
    assert mojibake == {"readable": False, "kind": "binary"}
    compressed = _graph_of(gzip.compress(RECORDED_EVENT_PAGE * 20))
    assert compressed["readability"]["kind"] == "binary"


def test_utf16_and_latin1_pages_decode() -> None:
    """Decode by BOM, then the page's declared charset, then UTF-8."""
    page = ("<html><head><title>Caf\u00e9 status</title></head><body><h1>Caf\u00e9"
            "</h1>" + "<p>The caf\u00e9 is open until 9 PM today with all services "
            "running normally for every visitor.</p>" * 3 + "</body></html>")
    g16 = _graph_of(page.encode("utf-16"))  # BOM
    assert "caf\u00e9 is open" in g16["text"] and g16["readability"]["readable"]
    latin = page.replace("<head>", '<head><meta charset="iso-8859-1">').encode("latin-1")
    assert "caf\u00e9 is open" in _graph_of(latin)["text"]


# F4 (review 2026-10-04): a page served with a Content-Type header charset but
# no BOM or <meta charset> declaration must still decode correctly \u2014 the HTTP
# header's charset is threaded into the jail child so the common Apache default
# ("Content-Type: text/html; charset=ISO-8859-1" on real latin-1 bytes) reads
# as the characters, not mojibake.
def test_header_only_charset_reaches_the_jail() -> None:
    page = ("<html><head><title>Caf\u00e9 du Monde</title></head><body>"
            + "<p>Caf\u00e9 du Monde hours: open daily at the caf\u00e9, "
              "8 a.m. to 6 p.m. for every visitor.</p>" * 4 +
            "</body></html>")
    raw = page.encode("latin-1")
    text = jail_extract.extract(raw, "https://x.test/",
                                declared_charset="ISO-8859-1")["text"]
    assert "Caf\u00e9 du Monde" in text
    # No charset given + no meta: UTF-8-with-replacement path. The latin-1
    # ``\xe9`` isn't valid UTF-8 so the e-acute becomes the replacement char.
    fallback = jail_extract.extract(raw, "https://x.test/")["text"]
    assert "Caf\ufffd" in fallback


def test_real_jail_reads_the_official_widget() -> None:
    """End to end through the subprocess jail: the recorded powerball.com page."""
    raw = gzip.decompress((_PAGES / "powerball_home.html.gz").read_bytes())
    g = _graph_of(raw, url=_MANIFEST["powerball_home"]["url"])
    assert "$409 Million" in g["text"] and g["readability"]["readable"]


# F2a (review 2026-10-04): a reader doesn't see <select>/<option>/<datalist>
# content, [hidden] elements, or display:none/visibility:hidden. aria-hidden
# hides from assistive tech, not a sighted reader, so its content IS visible
# (review3: MLB standings rows and USWDS banner icons were being dropped).
def test_jail_body_text_skips_invisible_elements() -> None:
    body_para = (b"<p>" + b" ".join([b"Service status page prose covering every region "
                                     b"and component we run, long enough for an article "
                                     b"extractor to keep as the primary text."] * 4) + b"</p>")
    html = (b"<html><head><title>t</title></head><body>"
            + body_para +
            b"<form><select><option>Operational</option>"
            b"<option>Major outage</option></select></form>"
            b"<div hidden>Hidden by attribute</div>"
            b"<div style='display:none'>Hidden by display none</div>"
            b"<div style=\"visibility: hidden\">Hidden by visibility hidden</div>"
            b"<div aria-hidden=\"true\">Visible aria-hidden paragraph</div>"
            b"<datalist><option>list item</option></datalist>"
            b"<div style='display:none'><p>Nested display-none paragraph</p></div>"
            b"</body></html>")
    text = jail_extract.extract(html, "https://example.test/")["text"]
    assert "Service status page prose" in text
    assert "Visible aria-hidden paragraph" in text
    for hidden in ("Operational", "Major outage", "Hidden by attribute",
                   "Hidden by display none", "Hidden by visibility hidden",
                   "list item", "Nested display-none paragraph"):
        assert hidden not in text, hidden


# F2b (review3 2026-10-04): the hidden-region stack must not leak on void
# elements (one <img aria-hidden> / <hr aria-hidden> / <input hidden> / an
# <img style='display:none'> hid the entire rest of a real recorded page),
# on unclosed <option>/<datalist> (an aggregator's <select><option>…<option>
# left the hidden state alive for the whole body), on inner tags whose
# siblings are still visible, or on React Server Components streaming divs
# (``<div hidden id="S:N">`` / ``id="B:N">`` carry the real page content
# that is swapped in on hydration — field: wmata lost ~21 kB).
def test_jail_body_text_void_hidden_does_not_hide_siblings() -> None:
    html = (b"<html><head><title>t</title></head><body>"
            b"<header><img src='/logo.png' alt='' aria-hidden='true'></header>"
            b"<main><h1>Powerball</h1>"
            b"<p>Winning numbers: 5 12 33 41 60 Powerball 7.</p>"
            b"<hr aria-hidden='true'>"
            b"<p>All Systems Operational on every region we run.</p>"
            b"<img style='display: none;'>"
            b"<p>Gas price today reads three dollars and nineteen cents.</p>"
            b"<form><input type='hidden' name='csrf' value='x'><input hidden>"
            b"<p>Form section is still visible after the hidden input.</p></form>"
            b"</main></body></html>")
    text = jail_extract.extract(html, "https://example.test/")["text"]
    for keeper in ("Winning numbers: 5 12 33 41 60", "All Systems Operational",
                   "Gas price today reads three dollars",
                   "Form section is still visible"):
        assert keeper in text, keeper


def test_jail_body_text_option_without_end_tag_closes_at_select() -> None:
    html = ("<form><select name='s'>"
            "<option>Major outage"
            "<option>Minor</select></form>"
            "<h2>Current status</h2>"
            "<p>All systems operational across every tracked service today.</p>")
    _, body = jail_extract._page_graph_layers(html)
    assert "All systems operational across every tracked service" in body
    assert "Major outage" not in body and "Minor" not in body


def test_jail_body_text_react_streaming_ssr_divs_are_visible() -> None:
    html = ("<div hidden id='S:0'><h1>Line status</h1>"
            "<p>Service Advisory: track work in the tunnel this weekend.</p></div>"
            "<div hidden id='B:1'><p>Boundary payload content is the page.</p></div>"
            "<div hidden>Really hidden template that should not read.</div>")
    _, body = jail_extract._page_graph_layers(html)
    assert "Service Advisory: track work in the tunnel" in body
    assert "Boundary payload content is the page" in body
    assert "Really hidden template" not in body


def test_jail_body_text_hidden_ancestor_pops_on_outer_end_tag() -> None:
    # <span> never closes before </div> — the hidden state must still exit
    # when the outer <div hidden> closes, so sibling body text reads.
    html = ("<div hidden><span>should stay hidden across sibling tags "
            "and never leak</div>"
            "<p>Visible sibling after the hidden ancestor region closes.</p>")
    _, body = jail_extract._page_graph_layers(html)
    assert "Visible sibling after the hidden ancestor" in body
    assert "should stay hidden" not in body


# F2d (review4 2026-10-04): malformed hidden markup must not swallow the
# body. The HTML spec's implied end tags (a sibling <li>/<p>/<dt>/<dd>/<tr>/
# <td>/<th>/<option> start closes the open peer; a block-level start closes
# an open <p>) and ancestor-pop on any end tag keep a hidden region bounded
# to its own sub-tree, so the sibling that follows reads again.
@pytest.mark.parametrize(("html", "visible", "hidden"), [
    ("<ul><li hidden>a<li>VIS_B<li>VIS_C</ul><p>REST</p>",
     ("VIS_B", "VIS_C", "REST"), ("a",)),
    ("<ul><li hidden>a<li hidden>b<li>VIS_C</ul><p>REST</p>",
     ("VIS_C", "REST"), ("a", "b")),
    ("<p style='display:none'>x<p>VIS_P</p><div>REST</div>",
     ("VIS_P", "REST"), ("x",)),
    ("<p hidden>x<div>VIS_DIV</div><section>REST</section>",
     ("VIS_DIV", "REST"), ("x",)),
    ("<table><tr hidden><td>x<tr><td>VIS_Y</table><p>REST</p>",
     ("VIS_Y", "REST"), ("x",)),
    ("<table><tr><td hidden>x<td>VIS_Y</tr></table><p>REST</p>",
     ("VIS_Y", "REST"), ("x",)),
    ("<dl><dt hidden>a<dd>VIS_DD<dt>VIS_DT</dl><p>REST</p>",
     ("VIS_DD", "VIS_DT", "REST"), ("a",)),
    ("<div><span hidden>x</div><p>REST</p>",
     ("REST",), ("x",)),
    ("<p hidden>x<ul><li>VIS_LI</ul><p>REST</p>",
     ("VIS_LI", "REST"), ("x",)),
    ("<div hidden><div>inner</div>LEAK</div><p>REST</p>",
     ("REST",), ("inner", "LEAK")),
    # F2e (review5 2026-10-04): a start tag that is NOT in the HTML spec's
    # "close a p element" list — <br>, <label>, <button>, <td>, <th>, <tr>,
    # <caption> — must not close an open <p hidden>. Before the fix, every
    # tag in _BLOCK_TAGS closed <p>, which leaked the rest of the paragraph.
    ("<p hidden>S1<br>S2</p><p>REST</p>",
     ("REST",), ("S1", "S2")),
    ("<p hidden>S1 <label>L</label> S2</p>SHOWN",
     ("SHOWN",), ("S1", "L", "S2")),
    ("<p hidden>S1 <button>B</button> S2</p>SHOWN",
     ("SHOWN",), ("S1", "B", "S2")),
])
def test_jail_body_text_implicit_close_before_hidden_sibling(
        html, visible, hidden) -> None:
    _, body = jail_extract._page_graph_layers(html)
    assert isinstance(body, str), "body must be a string"
    for keeper in visible:
        assert keeper in body, (html, keeper)
    for miss in hidden:
        assert miss not in body, (html, miss)


# F2f (review5 2026-10-04): once the open-element stack hits its cap, an
# un-pushable hidden-maker (script/style/[hidden]/display:none) must still
# hide its content — a counter per tag name tracks the overflowed depth and
# the matching end tag releases it. Before the fix, 300 unclosed <span>
# pushed the cap, then <script>/<style>/[hidden] silently became "visible"
# and leaked SECRET_JS, CSS source, and hidden-template text.
def test_jail_body_text_overflowed_stack_still_hides_script_and_style() -> None:
    many_spans = "<span>a" * 300  # exceeds _MAX_OPEN_STACK (256)
    html = (
        "<body>" + many_spans
        + "<script>var SECRET_JS=1</script>"
        + "<style>.secret_css{color:red}</style>"
        + "<div hidden>HIDDEN_DIV_TEMPLATE</div>"
        + "<div style='display:none'>HIDDEN_STYLE_TEMPLATE</div>"
        + "<p>END_VISIBLE</p>"
    )
    _, body = jail_extract._page_graph_layers(html)
    assert "END_VISIBLE" in body
    for leak in ("SECRET_JS", ".secret_css", "HIDDEN_DIV_TEMPLATE",
                 "HIDDEN_STYLE_TEMPLATE"):
        assert leak not in body, leak


# F2c (review3 2026-10-04): the recorded pages must keep the real body text
# that the hidden-stack leak was dropping. The signals asserted below each
# live inside the base extractor output but fell out of HEAD once hidden
# attributes started pushing to the stack.
@pytest.mark.parametrize(("page", "needle"), [
    ("githubstatus", "Incident with Pull Requests"),
    ("art_cdc_flu", "A .gov website belongs to an official government organization"),
    ("isitdown_slack", "Confirmed outages"),
    ("wmata_red_status", "Sign up for Metro service alerts"),
    ("mlb_standings_wc", "Los Angeles Angels"),
])
def test_recorded_pages_keep_hidden_sibling_body_text(page, needle) -> None:
    assert needle in _recorded(page)["text"], (page, needle)


def test_challenge_page_never_reads_as_its_script() -> None:
    """A JS proof-of-work page reads as nothing, never as its code (field:
    Reddit's challenge was read as the page and its title shipped)."""
    g = _recorded("reddit_worldnews_new")
    assert "addEventListener" not in g["text"] and "{" not in g["text"]
    assert g["readability"]["readable"] is False


# ---- fitness: identity metadata is never evidence (C11) --------------------


def test_identity_only_jsonld_scores_zero() -> None:
    g = _mini_graph(entities=[
        {"type": "WebPage", "name": "Is Slack down? Slack status",
         "url": "https://agg.example/slack", "isPartOf.@id": "https://agg.example/#site"},
        {"type": "WebPageElement", "about.name": "Slack", "url": "https://agg.example/x"},
        {"type": "Organization", "name": "Slack status checker"},
        {"type": "NewsArticle", "headline": "Slack status today"}])
    assert pagegraph.graph_fitness(g, ["slack status"]) == (0, [])


def test_value_fields_still_count() -> None:
    g = _mini_graph(entities=[{"type": "Event", "name": "High Tide",
                               "url": "https://x/tide", "startDate": "07:12"}])
    score, evidence = pagegraph.graph_fitness(g, ["tide times"])
    assert score > 0 and evidence == ["name: High Tide"]


@pytest.mark.parametrize("page", ["isitdown_slack", "lagcheck_slack",
                                  "dcmetromap_alerts", "powerball_checker",
                                  "lotteryusa_powerball"])
def test_aggregator_identity_evidence_is_gone(page) -> None:
    """The recorded aggregators whose rank came from their own JSON-LD name,
    url, about and isPartOf (field: isitdown 31, dcmetromap 40)."""
    wants = ["status", "slack", "delays", "dc metro red line", "jackpot", "powerball"]
    _, evidence = pagegraph.graph_fitness(_recorded(page), wants)
    for line in evidence:
        field = line.split(":", 1)[0]
        assert field.split(".")[0] not in pagegraph.IDENTITY_FIELDS, (page, line)


# ---- first party + refreshability (C11) -------------------------------------


def test_first_party_labeled_rows() -> None:
    official = _FIRST_PARTY["official_hosts"]
    wrong = [r for r in _FIRST_PARTY["rows"]
             if pagegraph.first_party(r["host"], r["subject"], official) is not r["expect"]]
    assert wrong == []
    assert len(_FIRST_PARTY["rows"]) >= 40


def test_first_party_fallback_is_the_subject_in_the_host() -> None:
    wrong = [r for r in _FIRST_PARTY["fallback_rows"]
             if pagegraph.first_party(r["host"], r["subject"], {}) is not r["expect"]]
    assert wrong == []


# D10 (review 2026-10-03): with no official listing, only a NAMED entity of the
# subject (a company / organization / product / service, written as a proper
# name in the subject or the ask) can be a host's own name; topic words never.
@pytest.mark.parametrize(("host", "subject", "ask", "expect"), [
    ("bitcoin.org", "bitcoin price", "", False),            # review inputs
    ("tides.net", "Charleston tides", "", False),
    ("stock.com", "Tesla stock", "", False),
    ("mortgage-rates.com", "mortgage rates", "", False),
    ("bitcoin.org", "Bitcoin price", "What is the Bitcoin price", False),  # a currency topic
    ("www.mortgagerates.com", "Mortgage Rates", "", False),
    ("eggs.com", "eggs", "price of eggs", False),           # lowercase: not a name
    ("gold.org", "Gold price", "", False),
    ("weather.com", "Boston weather", "", False),
    ("hurricanes.com", "Hurricanes", "", False),
    ("charleston.tides.net", "Charleston tides", "", False),  # a subdomain is the site's, not the subject's
    ("www.news.com", "Tesla news", "", False),
    ("www.tesla.com", "Tesla stock", "", True),
    ("www.coinbase.com", "Coinbase stock", "", True),
    ("slack.com", "slack", "is Slack down?", True),         # capitalized in the ask
    ("status.zoom.us", "zoom", "Is Zoom down", True),
    ("status.zoom.us", "zoom", "is zoom down", False),       # no proper name anywhere
    ("status.zoom.us", "zoom", "IS ZOOM DOWN", False),       # shouting is not a name
    ("slack-status.com", "Slack", "is slack down", True),    # the model wrote the name
    ("www.powerball.com", "Powerball jackpot", "", True),
    # F1 (review 2026-10-04): the brand must be the host label's FIRST part with every other
    # part a status-ish _OWN_SUFFIXES word; junk parts ("giveaway", "mirror", "forecast") never
    # count. And a shared-hosting suffix (github.io, herokuapp.com, netlify.app, vercel.app,
    # pages.dev, web.app) is never first-party in the no-listing fallback — the subdomain is
    # whoever rented it, not the subject.
    ("free-coinbase-giveaway.com", "Coinbase", "", False),
    ("tesla-stock-forecast.com", "Tesla stock", "", False),
    ("github-status-mirror.xyz", "GitHub", "", False),
    ("slackstatus.herokuapp.com", "Slack", "is slack down", False),
    ("wmata.netlify.app", "WMATA", "", False),
    ("foo.github.io", "GitHub", "", False),
    ("something.vercel.app", "GitHub", "", False),
    # F3 (review3 2026-10-04): a hyphenated brand name must match its hyphenated
    # host label as a prefix of the parts ("T-Mobile" → t-mobile.com), while
    # junk / aggregator parts after the brand still disqualify the host.
    ("t-mobile.com", "T-Mobile", "is T-Mobile down", True),
    ("www.t-mobile.com", "T-Mobile status", "T-Mobile outage", True),
    ("coca-cola.com", "Coca-Cola", "Coca-Cola stock", True),
    ("www.mercedes-benz.com", "Mercedes-Benz", "Mercedes-Benz recalls", True),
    ("www.harley-davidson.com", "Harley-Davidson", "Harley-Davidson news", True),
    ("www.rolls-royce.com", "Rolls-Royce", "Rolls-Royce news", True),
    ("www.chick-fil-a.com", "Chick-fil-A", "is Chick-fil-A open", True),
    ("www.7-eleven.com", "7-Eleven", "7-Eleven hours", True),
    ("www.usa-mobile.com", "T-Mobile", "is T-Mobile down", False),  # brand not the prefix
    # R4-8 (2026-10-04): the hyphen-brand match must require the hyphenated word to live in the
    # SUBJECT (the same ``_name_tokens`` rule for non-hyphen names). An ask-only hyphen is never
    # a brand of the subject: "Verizon vs T-Mobile outage" with subject "Verizon outage" ships
    # t-mobile.com for Verizon; "Real-time NVDA price" / "COVID-19 cases in Ohio" / "is X-Men on
    # Disney+" / "Hong-Kong weather" / "New-York news" / "Los-Angeles news" all do similar.
    ("t-mobile.com", "Verizon outage", "Verizon vs T-Mobile outage", False),
    ("real-time.com", "NVDA price", "Real-time NVDA price", False),
    ("x-men.com", "Disney+ schedule", "is X-Men on Disney+", False),
    ("hong-kong.com", "Hong Kong weather", "Hong-Kong weather", False),
    ("new-york.com", "New York news", "New-York news", False),
    ("los-angeles-times.com", "LA Times", "Los-Angeles news", False),
])
def test_first_party_fallback_needs_a_named_entity(host, subject, ask, expect) -> None:
    assert pagegraph.first_party(host, subject, {}, ask=ask) is expect


def test_official_listing_still_decides_when_present() -> None:
    official = _FIRST_PARTY["official_hosts"]
    assert pagegraph.first_party("www.nps.gov", "old faithful", official, ask="old faithful") is True
    assert pagegraph.first_party("bitcoin.org", "bitcoin price", official) is False


def test_first_party_without_evidence_does_not_lead() -> None:
    """The _s2_evaluate order: authority leads only with evidence of serving
    the ask; a zero-evidence first-party row sorts by fitness like the rest."""
    rows = [
        {"host": "aggregator.example", "first": False, "fitness": 3, "evidence": ["Jackpot: $409M"]},
        {"host": "bitcoin.org", "first": True, "fitness": 0, "evidence": []},
        {"host": "www.powerball.com", "first": True, "fitness": 2, "evidence": ["Jackpot: $409M"]},
        {"host": "unfetched.example", "first": True},
    ]
    keyed = sorted(
        ((not pagegraph.authority_leads(r["first"], r.get("evidence")),
          -(r["fitness"] if r.get("fitness") is not None else -1.0), i, r)
         for i, r in enumerate(rows)), key=lambda t: t[:3])
    assert [t[-1]["host"] for t in keyed] == [
        "www.powerball.com", "aggregator.example", "bitcoin.org", "unfetched.example"]
    assert pagegraph.authority_leads(True, ["a: b"]) is True
    assert pagegraph.authority_leads(True, []) is False
    assert pagegraph.authority_leads(True, None) is False
    assert pagegraph.authority_leads(False, ["a: b"]) is False


def test_registrable_domain() -> None:
    assert pagegraph.registrable_domain("health.aws.amazon.com") == "amazon.com"
    assert pagegraph.registrable_domain("www.bbc.co.uk") == "bbc.co.uk"
    assert pagegraph.registrable_domain("tfl.gov.uk") == "tfl.gov.uk"
    assert pagegraph.registrable_domain("azure.status.microsoft") == "status.microsoft"
    assert pagegraph.registrable_domain("WWW.WMATA.COM.") == "wmata.com"


@pytest.mark.parametrize(("page", "url", "low"), [
    ("cbsnews_noreaster", None, True),                  # NewsArticle
    ("art_usatoday_powerball", None, True),             # dated story URL
    ("powerball_home", None, False),
    ("wmata_red_status", None, False),
    ("slack_status", None, False),
    ("espn_cfb_scoreboard", None, False),
    ("mlb_standings_wc", None, False),
    ("slack_status", "https://www.tuscaloosanews.com/story/sports/college/"
     "football/2026/09/26/alabama-football-vs-south-carolina-score-analysis/1/", True),
    ("slack_status", "https://example.com/recap/alabama-vs-south-carolina-final", True),
])
def test_refreshability(page, url, low) -> None:
    g = _recorded(page)
    score = pagegraph.refreshability(g, url or g["url"])
    assert 0.0 <= score <= 1.0
    assert (score < 0.5) is low, (page, score)
