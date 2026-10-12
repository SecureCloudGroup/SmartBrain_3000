"""Round 20 (B3a, ni-format §35): the static DATA layer — plain ``<script type="application/json">``
documents flattened to NI-grammar leaves, ARIA grids as tables, the script-evidence ``signals``
layer, the ``data_leaf`` selector, and the measurement on the recorded page fixtures. Everything
runs in-process on the jail's own parser (no network, no browser)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from smartbrain_3000 import jail_extract, jailrun, ni, page_verify, pagegraph
from smartbrain_3000.browsers import router

PAGES = Path(__file__).parent / "fixtures" / "pages"

NEXT_PAGE = (
    '<html><head><title>Forecast</title>'
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"forecast":{"temperature":72,"wind":"NW 5 mph","rain":true,'
    '"periods":[{"name":"Tonight","temperature":61},{"name":"Saturday","temperature":75}]},'
    '"station key":"KBOS","__proto__":{"polluted":1},"bad\\u0001key":1}}}</script>'
    '<script>window.__INITIAL_STATE__ = {"secret": "never parsed as data"};</script>'
    '<script src="/app.js"></script></head><body><div id="root"></div></body></html>')
ARIA_PAGE = (
    '<html><body><div role="grid" aria-label="Arrivals">'
    '<div role="row"><span role="columnheader">Station</span><span role="columnheader">Due</span></div>'
    '<div role="row"><span role="rowheader">Logan</span><span role="gridcell">4 <b>min</b></span></div>'
    '<div role="row"><span role="cell">Airport</span><span role="cell">9 min</span></div>'
    '</div><table role="table"><tr><th>h</th></tr><tr><td>native</td></tr></table>'
    '<noscript>You need to enable JavaScript to run this app.</noscript></body></html>')


def _layers(html: str) -> dict:
    layers, _body = jail_extract._page_graph_layers(html)
    return layers


# ---- the jail's new layers ------------------------------------------------------------------

def test_a_plain_json_document_becomes_grammar_leaves_and_script_code_never_does() -> None:
    layers = _layers(NEXT_PAGE)
    assert [d["id"] for d in layers["data"]] == ["__NEXT_DATA__"]  # the window.__INITIAL_STATE__ script is code
    leaves = dict(layers["data"][0]["leaves"])
    assert leaves["props.pageProps.forecast.temperature"] == 72
    assert leaves["props.pageProps.forecast.periods[1].name"] == "Saturday"
    assert leaves['props.pageProps["station key"]'] == "KBOS"
    assert leaves["props.pageProps.forecast.rain"] is True
    assert not any("proto" in path or "bad" in path for path in leaves)
    for path in leaves:  # W1-D: every emitted path parses under the NI grammar
        ni.parse_path(path)
    assert "never parsed as data" not in json.dumps(layers["data"])


def test_hostile_json_documents_lose_only_themselves() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    wide = "[" + ",".join("0" for _ in range(400_000)) + "]"
    nested_lists = "[" * 20 + "1" + "]" * 20
    page = ("<html><head><title>Hostile</title>"
            f'<script type="application/json" id="deep">{deep}</script>'
            f'<script type="application/json" id="wide">{wide}</script>'
            f'<script type="application/json" id="lists">{nested_lists}</script>'
            '<script type="application/json" id="keys">{"ok": 1, "a\\"b": 2, "": 3, "constructor": {"x": 4}}</script>'
            '<script type="application/json">not json at all</script>'
            "</head><body><p>The prose still reads.</p></body></html>")
    layers, body = jail_extract._page_graph_layers(page)
    docs = {d["id"]: dict(d["leaves"]) for d in layers["data"]}
    assert "deep" not in docs  # RecursionError → the document is dropped, nothing else
    assert len(docs["wide"]) == 25  # leading items only
    assert "lists" not in docs  # nine-plus subscripts in a row are beyond the plain grammar
    assert docs["keys"] == {"ok": 1}
    assert "The prose still reads." in body
    for leaves in docs.values():
        for path in leaves:
            ni.parse_path(path)


def test_an_aria_grid_becomes_a_table_and_a_native_table_is_not_counted_twice() -> None:
    layers = _layers(ARIA_PAGE)
    assert layers["tables"][0] == {"caption": "", "headers": ["Station", "Due"],
                                   "rows": [["Logan", "4 min"], ["Airport", "9 min"]]}
    assert len(layers["tables"]) == 2 and layers["tables"][1]["rows"] == [["native"]]
    assert layers["signals"] == {"script_count": 0, "script_bytes": 0, "noscript_js": True}


def test_the_signals_layer_counts_script_evidence() -> None:
    sig = _layers(NEXT_PAGE)["signals"]
    assert sig["script_count"] == 3 and sig["script_bytes"] > 200 and sig["noscript_js"] is False


def test_the_parent_refuses_smuggled_data_and_signals_shapes() -> None:
    good = {"text": "t", "title": "", "data": [{"id": "x", "leaves": [["a.b", 1]]}],
            "signals": {"script_count": 1, "script_bytes": 2, "noscript_js": False}}
    assert jailrun._validate_payload(json.dumps(good))["data"][0]["id"] == "x"
    for bad in ({"data": [{"id": "x", "leaves": [], "extra": 1}]},
                {"data": [{"id": "has space", "leaves": []}]},
                {"data": [{"id": "x", "leaves": [["a", {"nested": 1}]]}]},
                {"data": [{"id": "x", "leaves": [["", 1]]}]},
                {"data": [{"id": "x", "leaves": [["a", "v" * 121]]}]},
                {"data": [{"id": f"d{i}", "leaves": []} for i in range(9)]},
                {"signals": {"script_count": 1, "script_bytes": 2}},
                {"signals": {"script_count": True, "script_bytes": 2, "noscript_js": False}},
                {"signals": {"script_count": 1, "script_bytes": -2, "noscript_js": False}}):
        with pytest.raises(jailrun.JailError) as exc:
            jailrun._validate_payload(json.dumps({"text": "t", "title": "", **bad}))
        assert exc.value.reason == "bad_field_type"
    with pytest.raises(jailrun.JailError):  # NaN is no data
        jailrun._validate_payload('{"text": "t", "title": "", "data": [{"id": "x", "leaves": [["a", NaN]]}]}')


# ---- the data_leaf selector -----------------------------------------------------------------

def _graph(html: str) -> dict:
    return pagegraph.graph_from_extract("https://wx.example.org/", {"text": "", "title": "Forecast",
                                                                    **_layers(html)})


def test_data_leaf_selectors_validate_run_and_drift() -> None:
    graph = _graph(NEXT_PAGE)
    sel = {"kind": "data_leaf", "doc": "__NEXT_DATA__", "path": "props.pageProps.forecast.temperature"}
    assert pagegraph.validate_selector(sel) == sel
    assert pagegraph.run_selector(graph, sel) == "72"
    assert pagegraph.run_selector(graph, {**sel, "path": "props.pageProps.forecast.rain"}) == "true"
    with pytest.raises(pagegraph.GraphDrift):
        pagegraph.run_selector(graph, {**sel, "path": "props.pageProps.forecast.humidity"})
    for bad in ({**sel, "extra": 1}, {**sel, "doc": "has space"}, {**sel, "path": "a..b"},
                {**sel, "path": "__proto__.x"}, {**sel, "path": 'a["constructor"]'},
                {**sel, "path": "[0].a"}, {**sel, "path": "a" + "[0]" * 9},
                {**sel, "path": "a" * 201}):
        with pytest.raises(ValueError):
            pagegraph.validate_selector(bad)
    assert pagegraph.validate_selector({**sel, "path": 'a["b c"][0].d'})["path"] == 'a["b c"][0].d'


def test_the_menu_offers_want_matching_leaves_and_a_program_compiles_from_them() -> None:
    graph = _graph(NEXT_PAGE)
    menu = pagegraph.enumerate_menu(graph, ["temperature"])
    leaves = [m for m in menu if m["selector"]["kind"] == "data_leaf"]
    assert leaves[0]["selector"]["path"] == "props.pageProps.forecast.temperature"
    pick = leaves[0]["id"]
    compiled = pagegraph.compile_program(graph, {"temperature": "temperature"}, "Boston temperature",
                                         lambda _p: json.dumps({"picks": {"temperature": pick}}))
    assert compiled is not None and compiled["values"] == {"temperature": "72"}
    stage = {"op": "graph_extract", "fields": compiled["fields"]}
    assert ni._validate_graph_extract_stage(stage, 0) == {"temperature"}


def test_hydration_state_makes_a_scriptless_page_readable_and_counts_as_evidence() -> None:
    leaves = [[f"items[{i}].temperature", 60 + i] for i in range(12)]
    graph = pagegraph.graph_from_extract("https://wx.example.org/", {
        "text": "", "title": "Forecast", "data": [{"id": "__NEXT_DATA__", "leaves": leaves}],
        "signals": {"script_count": 5, "script_bytes": 9000, "noscript_js": False}})
    assert graph["readability"] == {"readable": True, "kind": "ok"}
    assert pagegraph.data_bearing(graph)
    assert router.need_signals(graph, None) == ()  # a data-bearing page needs no render
    assert page_verify.has_evidence(graph, ["temperature"], "")
    score, evidence = pagegraph.graph_fitness(graph, ["temperature"])
    assert score >= 3 and evidence and "temperature" in evidence[0]
    thin = pagegraph.graph_from_extract("https://wx.example.org/", {
        "text": "", "title": "App", "data": [{"id": "chunks", "leaves": [["items[0]", 1]]}],
        "signals": {"script_count": 5, "script_bytes": 9000, "noscript_js": False}})
    assert router.need_signals(thin, None) == ("js_shell",)  # a chunk list is plumbing, not data


# ---- measured on the recorded page fixtures -------------------------------------------------

def test_the_recorded_pages_measured() -> None:
    """The recorded corpus (50 pages the earlier rounds sent to links / read statically): the
    DATA layer adds 5 JSON documents on 4 pages (32 leaves, one of 10+ leaves — a site config),
    and no ARIA grid; every emitted path parses. Measured 2026-10-12 (B3a)."""
    docs, leaves, aria, pages_with_docs = 0, 0, 0, 0
    for path in sorted(PAGES.glob("*.html.gz")):
        html = jail_extract._decode_html(gzip.decompress(path.read_bytes()))
        layers = _layers(html)
        native = min(html.lower().count("<table"), len(layers["tables"]))
        aria += len(layers["tables"]) - native
        docs += len(layers["data"])
        pages_with_docs += bool(layers["data"])
        for doc in layers["data"]:
            leaves += len(doc["leaves"])
            for leaf_path, _value in doc["leaves"]:
                ni.parse_path(leaf_path)
    assert (docs, pages_with_docs, leaves, aria) == (5, 4, 32, 0)


def test_the_jail_signals_reproduce_the_raw_html_need_signal_on_the_recorded_pages() -> None:
    manifest = json.loads((PAGES / "manifest.json").read_text())
    shells = []
    for path in sorted(PAGES.glob("*.html.gz")):
        raw = gzip.decompress(path.read_bytes())
        name = path.name[:-len(".html.gz")]
        url = manifest.get(name, {}).get("url", "https://example.org/")
        graph = pagegraph.graph_from_extract(url, jail_extract.extract(raw, url))
        by_html = router.need_signals(graph, raw)
        assert router.need_signals(graph, None) == by_html, name
        if "js_shell" in by_html:
            shells.append(name)
    assert shells == ["health_aws_status"]
