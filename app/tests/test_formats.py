"""Parser + engine + flow tests for the §3 http_json ``format`` extensions.

Covers: stdlib parsers (csv / feed / xml / text); the entity-expansion refusal;
the ``_fetch_http_json`` dispatch through ``netguard.safe_fetch_text``; the
validator's closed ``format`` set; the flow's sealed Library ``_format``
carrying onto a built spec; the paste-URL content-type sniff. Every test is
network-free (netguard fetchers are monkeypatched).
"""

from __future__ import annotations

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import formats, netguard, ni, ni_flow
from smartbrain_3000.secrets import gen_master_key

# --- parsers ------------------------------------------------------------------

def test_parse_csv_sniffs_semicolons_and_caps_rows(monkeypatch) -> None:
    monkeypatch.setattr(formats, "MAX_CSV_ROWS", 2)
    out = formats.parse_csv("name;value;note\nA;1;alpha\nB;2;beta\nC;3;gamma")
    assert out["columns"] == ["name", "value", "note"]
    assert [r["name"] for r in out["rows"]] == ["A", "B"]
    assert out["rows"][0] == {"name": "A", "value": "1", "note": "alpha"}


def test_parse_csv_handles_quoted_fields_and_tabs() -> None:
    text = "a\tb\n\"hi, world\"\t\"line\nbreak\""
    out = formats.parse_csv(text)
    assert out["columns"] == ["a", "b"]
    assert out["rows"][0]["a"] == "hi, world"
    assert out["rows"][0]["b"] == "line\nbreak"


def test_parse_csv_dedupes_slugged_columns() -> None:
    out = formats.parse_csv("name,name,Value$$\n1,2,3")
    assert out["columns"] == ["name", "name_2", "Value"]


def test_parse_csv_empty_body_refused() -> None:
    with pytest.raises(formats.FormatError):
        formats.parse_csv("")


def test_parse_csv_cell_length_capped(monkeypatch) -> None:
    monkeypatch.setattr(formats, "MAX_CSV_CELL", 8)
    out = formats.parse_csv("a\nlonglongvalue")
    assert out["rows"][0]["a"] == "longlong"


def test_parse_feed_rss_and_atom() -> None:
    rss = ("<rss><channel><title>Site</title>"
           "<item><title>A</title><link>http://x/a</link><description>hi</description></item>"
           "</channel></rss>")
    out = formats.parse_feed(rss)
    assert out["title"] == "Site" and out["items"][0]["title"] == "A"
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Site</title>'
            '<entry><title>B</title><link href="http://x/b"/><summary>ok</summary></entry></feed>')
    out2 = formats.parse_feed(atom)
    assert out2["title"] == "Atom Site" and out2["items"][0]["link"] == "http://x/b"


def test_parse_feed_refuses_doctype() -> None:
    with pytest.raises(formats.FormatError, match="DOCTYPE"):
        formats.parse_feed("<!DOCTYPE rss><rss><channel><title>x</title></channel></rss>")


def test_parse_feed_refuses_entity() -> None:
    with pytest.raises(formats.FormatError, match="DOCTYPE"):
        formats.parse_feed('<!ENTITY a "b"><rss/>')


def test_parse_xml_nests_and_promotes_repeats() -> None:
    out = formats.parse_xml('<r><a x="1">t</a><b/><b>t2</b></r>')
    assert out["r"]["a"] == {"@x": "1", "_text": "t"}
    assert isinstance(out["r"]["b"], list) and len(out["r"]["b"]) == 2


def test_parse_xml_depth_cap(monkeypatch) -> None:
    monkeypatch.setattr(formats, "MAX_XML_DEPTH", 3)
    deep = "<a><a><a><a>x</a></a></a></a>"
    with pytest.raises(formats.FormatError, match="deeper than"):
        formats.parse_xml(deep)


def test_parse_xml_strips_namespaces() -> None:
    out = formats.parse_xml(
        '<r xmlns="urn:x" xmlns:y="urn:y"><y:child>hi</y:child></r>')
    assert "child" in out["r"] and out["r"]["child"]["_text"] == "hi"


def test_parse_text_capped(monkeypatch) -> None:
    monkeypatch.setattr(formats, "MAX_TEXT_CHARS", 5)
    assert formats.parse_text("abcdefghij") == {"text": "abcde"}


# --- content-type + first-bytes sniffing -------------------------------------

@pytest.mark.parametrize(("ct", "body", "expected"), [
    ("application/json", '{"ok": 1}', "json"),
    ("text/html", "<rss><channel>", "feed"),
    ("application/atom+xml", '<?xml version="1.0"?><feed>', "feed"),
    ("text/xml", '<?xml version="1.0"?><root/>', "xml"),
    ("text/csv", "a,b\n1,2", "csv"),
    ("text/plain", "hello world", "text"),
    ("application/octet-stream", "col1|col2\nA|B", "csv"),
])
def test_sniff_format_paste_url(ct, body, expected) -> None:
    assert formats.sniff_format(ct, body) == expected


# --- engine dispatch: _fetch_http_json parses by format -----------------------

def _fake_text_source(monkeypatch, fmt: str, body: str,
                      content_type: str = "text/plain") -> list:
    calls: list = []

    def fake_text(url: str, requested_fmt: str, headers=None,
                  allow_redirects: bool = True):
        calls.append({"url": url, "fmt": requested_fmt, "headers": headers,
                      "allow_redirects": allow_redirects})
        assert requested_fmt == fmt, "engine must pass the spec's format"
        return {"final_url": url, "status": 200,
                "content_type": content_type, "text": body}

    monkeypatch.setattr(netguard, "safe_fetch_text", fake_text)
    return calls


def test_fetch_http_json_dispatches_csv(monkeypatch) -> None:
    _fake_text_source(monkeypatch, "csv", "a,b\n1,2\n3,4")
    out = ni._fetch_http_json(
        {"type": "http_json", "url": "https://ex.test/x.csv",
         "headers": {}, "format": "csv"},
        item_id="i", secrets_store=None)
    assert out == {"columns": ["a", "b"],
                   "rows": [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]}


def test_fetch_http_json_dispatches_feed(monkeypatch) -> None:
    body = ("<rss><channel><title>Feed</title>"
            "<item><title>Item A</title><link>http://x/a</link></item>"
            "</channel></rss>")
    _fake_text_source(monkeypatch, "feed", body)
    out = ni._fetch_http_json(
        {"type": "http_json", "url": "https://ex.test/rss",
         "headers": {}, "format": "feed"},
        item_id="i", secrets_store=None)
    assert out["title"] == "Feed" and out["items"][0]["title"] == "Item A"


def test_fetch_http_json_dispatches_xml(monkeypatch) -> None:
    _fake_text_source(monkeypatch, "xml", "<r><a>hi</a></r>")
    out = ni._fetch_http_json(
        {"type": "http_json", "url": "https://ex.test/x.xml",
         "headers": {}, "format": "xml"},
        item_id="i", secrets_store=None)
    assert out == {"r": {"a": {"_text": "hi"}}}


def test_fetch_http_json_default_stays_json(monkeypatch) -> None:
    """No format ⇒ historical safe_fetch_json path (byte-for-byte)."""
    called: dict = {}

    def fake_json(url, headers=None, allow_redirects=True):
        called["hit"] = True
        return {"answer": 42}

    monkeypatch.setattr(netguard, "safe_fetch_json", fake_json)
    out = ni._fetch_http_json(
        {"type": "http_json", "url": "https://ex.test/", "headers": {}},
        item_id="i", secrets_store=None)
    assert out == {"answer": 42} and called == {"hit": True}


def test_fetch_http_json_csv_parse_failure_kind_not_csv(monkeypatch) -> None:
    _fake_text_source(monkeypatch, "csv", "")
    with pytest.raises(ni.NIError) as excinfo:
        ni._fetch_http_json(
            {"type": "http_json", "url": "https://ex.test/x.csv",
             "headers": {}, "format": "csv"},
            item_id="i", secrets_store=None)
    assert excinfo.value.kind == "fetch_failed"
    assert excinfo.value.detail == "not_csv"


def test_fetch_http_json_textual_honors_header_redirect_discipline(monkeypatch) -> None:
    calls = _fake_text_source(monkeypatch, "csv", "a\n1")
    # A textual fetch WITH headers must ride allow_redirects=False (mirrors http_json).
    ni._fetch_http_json(
        {"type": "http_json", "url": "https://ex.test/x.csv",
         "headers": {"X-Trace": "t"}, "format": "csv"},
        item_id="i", secrets_store=None)
    assert calls[-1]["allow_redirects"] is False
    ni._fetch_http_json(
        {"type": "http_json", "url": "https://ex.test/x.csv",
         "headers": {}, "format": "csv"},
        item_id="i", secrets_store=None)
    assert calls[-1]["allow_redirects"] is True


# --- validator: format is closed, absent = json ------------------------------

def _http_json_spec(*, source: dict) -> dict:
    return {"version": 1, "title": "t", "goal": "g", "params": {},
            "source": source, "pipeline": [],
            "scene": {"type": "text", "value": "x", "role": "value",
                       "tone": "default", "size": "md"},
            "display": {"size": "small"}, "contract": None,
            "repair_policy": {"l1": True, "l2_frontier": False},
            "model": None, "interval_minutes": 60}


def test_validate_spec_accepts_known_formats() -> None:
    for fmt in ("json", "csv", "feed", "xml", "text"):
        spec = _http_json_spec(source={"type": "http_json",
                                        "url": "https://ex.test/x",
                                        "headers": {}, "format": fmt})
        assert ni.validate_spec(spec)["source"]["format"] == fmt


def test_validate_spec_defaults_format_absent() -> None:
    """No ``format`` key ⇒ spec unchanged (existing specs stay byte-identical)."""
    spec = _http_json_spec(source={"type": "http_json",
                                    "url": "https://ex.test/x", "headers": {}})
    out = ni.validate_spec(spec)
    assert "format" not in out["source"]


def test_validate_spec_refuses_unknown_format() -> None:
    spec = _http_json_spec(source={"type": "http_json",
                                    "url": "https://ex.test/x",
                                    "headers": {}, "format": "gtfs_rt"})
    with pytest.raises(ValueError, match="format must be one of"):
        ni.validate_spec(spec)


# --- flow: a sealed Library format rides onto the built spec + sampling ------

def _store():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ni.NIStore(conn, gen_master_key()), conn


def test_sample_and_map_seals_format_on_built_spec_from_library_pick() -> None:
    """A Library CSV pick stamped ``_format=csv`` on the record: the assembled
    http_json spec carries ``format="csv"`` so every future refresh parses CSV."""
    store, _ = _store()
    item_id = ni_flow.create_shell_item(store, "birth rate by state")
    intent = {"kind": "external_data", "subject": "births", "cadence_minutes": 60,
              "wants": ["state", "births"], "threshold": None,
              "display_hint": "list"}
    ni_flow._transition(store, item_id, "intent", intent=intent)
    live = ni_flow._flow_read(store, item_id) or {}
    ni_flow._flow_write(store, item_id, {**live, "_format": "csv"})
    sample = {"columns": ["state", "births"],
              "rows": [{"state": "CA", "births": "1"},
                       {"state": "NY", "births": "2"}]}

    import json

    def mapping_model(prompt: str) -> str:
        if "verifying a data card BEFORE it ships" in prompt:
            return json.dumps({"serves": True, "gaps": [], "wrong": []})
        if "Choose the best candidate path" in prompt:
            return json.dumps({"state": "rows[0].state", "births": "rows[0].births"})
        return "{}"

    result = ni_flow._sample_and_map(
        store, item_id, "birth rate by state", intent,
        "https://ex.test/data.csv", mapping_model, lambda _u: sample)
    assert result["state"] == "ready", result.get("error")
    spec = store.get_item(item_id)["spec"]
    assert spec["source"]["type"] == "http_json"
    assert spec["source"]["format"] == "csv"
    assert spec["source"]["url"] == "https://ex.test/data.csv"


# --- paste-URL sniff: JSON refusal + first bytes pick the right parser -------

def test_sniffed_fetch_falls_through_to_feed_on_json_refusal(monkeypatch) -> None:
    """A pasted URL that serves RSS: the JSON path refuses, the sniff picks feed."""
    body = ("<rss><channel><title>PastedFeed</title>"
            "<item><title>Hello</title><link>http://x/a</link></item>"
            "</channel></rss>")

    def refuse_json(url, headers=None, allow_redirects=True):
        raise netguard.FetchError("upstream returned invalid JSON", kind="not_json")

    def fake_text(url, fmt, headers=None, allow_redirects=True):
        return {"final_url": url, "status": 200,
                "content_type": "application/rss+xml", "text": body}

    monkeypatch.setattr(netguard, "safe_fetch_json", refuse_json)
    monkeypatch.setattr(netguard, "safe_fetch_text", fake_text)
    out = ni_flow._sniffed_fetch("https://ex.test/rss")
    assert out["title"] == "PastedFeed"


def test_sniffed_fetch_reraises_security_refusal(monkeypatch) -> None:
    """Non-format errors (SSRF refusal, timeout) ride through unchanged."""
    def refuse(url, headers=None, allow_redirects=True):
        raise netguard.FetchError("blocked non-global address: 10.0.0.1")

    monkeypatch.setattr(netguard, "safe_fetch_json", refuse)
    with pytest.raises(netguard.FetchError, match="blocked"):
        ni_flow._sniffed_fetch("https://ex.test/x")


def test_an_entity_declaration_padded_past_the_head_is_still_refused() -> None:
    from smartbrain_3000 import formats
    body = "<?xml version='1.0'?>" + " " * 10_000 + \
        "<!DOCTYPE r [<!ENTITY a 'aaaa'>]><r>&a;&a;</r>"
    for parse in (formats.parse_xml, formats.parse_feed):
        with pytest.raises(formats.FormatError):
            parse(body)


# --- preambles and leading whitespace (field 2026-09-29) ------------------------------------------

GISS = ("Land-Ocean: Global Means\n"
        "Year,Jan,Feb,Mar,J-D\n"
        "1880,-.18,-.24,-.09,-.17\n"
        "1881,-.19,-.14,.03,-.09\n"
        "1882,.16,.14,.04,-.11\n")
CO2 = ("# --------------------------------------------------------------------\n"
       "# USE OF NOAA GML DATA\n"
       "#\n"
       "year,month,decimal date,average,deseasonalized\n"
       "1958,3,1958.2027,315.71,314.44\n"
       "1958,4,1958.2877,317.45,315.16\n")


def test_a_csv_title_line_before_the_header_is_skipped() -> None:
    out = formats.parse_csv(GISS)
    assert out["columns"] == ["Year", "Jan", "Feb", "Mar", "J-D"]
    assert out["rows"][0]["Year"] == "1880" and out["rows"][0]["J-D"] == "-.17"


def test_csv_comment_lines_before_the_header_are_skipped() -> None:
    out = formats.parse_csv(CO2)
    assert out["columns"] == ["year", "month", "decimal_date", "average", "deseasonalized"]
    assert [r["average"] for r in out["rows"]] == ["315.71", "317.45"]


def test_a_preamble_keeps_the_newest_first_series_rule() -> None:
    text = "Monthly series, from the source\n\ndate,value,note\n2026-07-01,4.1,a\n2026-08-01,4.2,b\n" \
           "2026-09-01,4.3,c\n"
    out = formats.parse_csv(text)
    assert out["columns"] == ["date", "value", "note"] and out["rows"][0]["date"] == "2026-09-01"


def test_a_plain_or_ragged_csv_is_unchanged() -> None:
    assert formats._strip_csv_preamble("a,b\n1,2\n") == "a,b\n1,2\n"
    ragged = "a,b,c\n1,2\n3,4\n5,6\n"  # data rows shorter than the header: the header stays
    assert formats._strip_csv_preamble(ragged) == ragged
    assert formats.parse_csv("value\n1\n2\n")["columns"] == ["value"]  # one column


FEED = ("<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>"
        "<item><title>A</title><link>https://ex.test/a</link>"
        "<pubDate>Tue, 29 Sep 2026 01:00:00 GMT</pubDate></item></channel></rss>")


def test_blank_lines_or_a_bom_before_the_xml_declaration_parse() -> None:
    for lead in ("\n\n  ", "﻿", "﻿\r\n"):
        assert formats.parse_feed(lead + FEED)["items"][0]["title"] == "A"
        assert "rss" in formats.parse_xml(lead + FEED)


@pytest.mark.parametrize("value", ["Tue, 29 Sep 2026 01:00:00 GMT", "Tue, 29 Sep 2026 01:00:00 +0000",
                                   "29 Sep 2026 01:00 -0400", "Tue, 29 Sep 2026 01:00:00 -0000"])
def test_rss_dates_are_times(value) -> None:
    shown = ni.local_time(value)
    assert shown.endswith(("AM", "PM")) and "2026" not in shown
    assert ni_flow._is_timestamp(value, "published")


def test_a_feed_card_shows_published_as_local_time() -> None:
    built = ni_flow.assemble_from_mapping({"title": "items[0].title"}, {"title": "string"}, "list",
                                          formats.parse_feed(FEED))
    assert {"fn": "time", "field": "rows", "key": "published"} in built["pipeline"][-1]["apply"]
    assert built["preview_payload"]["rows"][0]["published"].endswith(("AM", "PM"))


def test_not_an_rss_date() -> None:
    with pytest.raises(ni.NIError):
        ni.local_time("Tue, 31 Feb 2026 01:00:00 GMT")
    assert not ni_flow._is_timestamp("sometime next week", "published")


# --- Phase 0 (2026-10-05): a pasted non-JSON link keeps its sniffed format ---------------------------

def _refuse_json(url, headers=None, allow_redirects=True):
    raise netguard.FetchError("upstream returned invalid JSON", kind="not_json")


def test_sniffed_fetch_with_format_names_the_sniffed_format(monkeypatch) -> None:
    body = "<?xml version='1.0'?><rss><channel><title>PastedFeed</title></channel></rss>"

    def fake_text(url, fmt, headers=None, allow_redirects=True):
        return {"final_url": url, "status": 200, "content_type": "application/rss+xml", "text": body}

    monkeypatch.setattr(netguard, "safe_fetch_json", _refuse_json)
    monkeypatch.setattr(netguard, "safe_fetch_text", fake_text)
    out, fmt = ni_flow._sniffed_fetch_with_format("https://ex.test/rss")
    assert fmt == "feed" and out["title"] == "PastedFeed"
    assert ni_flow._sniffed_fetch("https://ex.test/rss")["title"] == "PastedFeed"  # the old shape still works


def test_seal_sniffed_format_stamps_the_record_and_never_for_json() -> None:
    store, _ = _store()
    item_id = ni_flow.create_shell_item(store, "pasted csv")
    ni_flow._seal_sniffed_format(store, item_id, "json")
    assert "_format" not in (ni_flow._flow_read(store, item_id) or {})
    ni_flow._seal_sniffed_format(store, item_id, "csv")
    assert (ni_flow._flow_read(store, item_id) or {})["_format"] == "csv"


def test_pasted_csv_link_seals_its_format_on_the_built_spec(monkeypatch) -> None:
    """The product path for a pasted CSV link: the default fetcher sniffs csv, seals ``_format``, and the
    built spec carries ``source.format == "csv"`` so the engine's first refresh parses CSV, not JSON."""
    import json
    body = "state,births\nCA,1\nNY,2\n"

    def fake_text(url, fmt, headers=None, allow_redirects=True):
        return {"final_url": url, "status": 200, "content_type": "text/csv", "text": body}

    monkeypatch.setattr(netguard, "safe_fetch_json", _refuse_json)
    monkeypatch.setattr(netguard, "safe_fetch_text", fake_text)
    store, _ = _store()
    request = "birth rate by state"
    item_id = ni_flow.create_shell_item(store, request)

    def model(_model: str, prompt: str) -> str:
        if "verifying a data card BEFORE it ships" in prompt:
            return json.dumps({"serves": True, "gaps": [], "wrong": []})
        if "Choose the best candidate path" in prompt:
            return json.dumps({"state": "rows[0].state", "births": "rows[0].births"})
        return json.dumps({"kind": "external_data", "subject": "births", "cadence_minutes": 60,
                           "wants": ["state", "births"], "threshold": None, "display_hint": "list"})

    result = ni_flow.run_flow(store, item_id, gateway_call=model, ni_route_model="local/test",
                              source_url="https://ex.test/data.csv")
    assert result["state"] == "ready", result.get("error")
    item = store.get_item(item_id)
    assert item is not None and item["spec"]["source"]["format"] == "csv"
    assert (ni_flow._flow_read(store, item_id) or {}).get("_format") == "csv"
