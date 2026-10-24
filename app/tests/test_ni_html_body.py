"""A web page where the source sealed data (csv / feed / xml / json) is a fetch failure, for any format.

A site maintenance page served as text/html with 200 (or HTML under a data content-type) used to parse as
a one-column table or read as "has nothing for this right now". The body decides: every textual parser
refuses a body that opens as a page, and a picked row that answers with one moves on with an honest why.
"""

from __future__ import annotations

import pytest
from test_ni_auto_pick import (  # noqa: F401  (_v2_local is the autouse v2 fixture)
    _SYN_FEED,
    _SYN_URL,
    _row,
    _run_flow_default_fetch,
    _store,
    _syn_model,
    _SynLib,
    _v2_local,
)

from smartbrain_3000 import formats, netguard, ni_flow

_PAGES = [
    "<!DOCTYPE html><html><head><title>Down for maintenance</title></head><body>Back soon</body></html>",
    "  \n<html lang='en'><body><h1>Scheduled maintenance</h1></body></html>",
    "﻿<?xml version='1.0'?><!-- cached --><!doctype HTML><html><body>try later</body></html>",
    "<head><title>503</title></head><body>unavailable</body>",
]
_DATA = {
    "csv": "name,count\nalpha,3\nbeta,5\n",
    "feed": "<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
            "<item><title>a</title><link>https://data.synthetic.example/a</link></item></channel></rss>",
    "xml": "<?xml version='1.0'?><root><row><name>alpha</name></row></root>",
}
_PARSERS = {"csv": formats.parse_csv, "feed": formats.parse_feed, "xml": formats.parse_xml}


@pytest.mark.parametrize("page", _PAGES)
@pytest.mark.parametrize("fmt", sorted(_PARSERS))
def test_every_textual_parser_refuses_a_web_page_body(fmt, page) -> None:
    with pytest.raises(formats.HtmlBody, match="web page"):
        _PARSERS[fmt](page)
    assert formats.looks_like_html(page)


@pytest.mark.parametrize("fmt", sorted(_PARSERS))
def test_real_data_that_quotes_markup_is_not_a_web_page(fmt) -> None:
    quoted = _DATA[fmt].replace("alpha", "<html>") if fmt == "csv" else _DATA[fmt]
    assert not formats.looks_like_html(quoted)
    assert _PARSERS[fmt](quoted)


@pytest.mark.parametrize("fmt", sorted(_PARSERS))
def test_the_flow_parse_path_turns_a_page_into_a_fetch_failure(fmt) -> None:
    with pytest.raises(netguard.FetchError) as caught:
        ni_flow._textual_parse(_PAGES[0], fmt)
    assert ni_flow._is_web_page(caught.value) and caught.value.kind == netguard.format_error_kind(fmt)


@pytest.mark.parametrize("ctype", ["application/json", "text/html"])
def test_json_with_a_web_page_body_is_a_web_page_failure(monkeypatch, ctype) -> None:
    got = {"final_url": "https://x.example/", "status": 200, "content_type": ctype,
           "content": _PAGES[0].encode()}
    monkeypatch.setattr(netguard, "_guarded_get", lambda *a, **k: got)
    with pytest.raises(netguard.FetchError) as caught:
        netguard.safe_fetch_json("https://x.example/")
    assert caught.value.kind == "not_json" and ni_flow._is_web_page(caught.value)


def _spy_move_on(monkeypatch) -> list[str]:
    """Record the reason every move-on to the next source is given."""
    whys: list[str] = []
    real = ni_flow._move_on

    def spy(store, item_id, url, why, *a, **k):
        whys.append(why)
        return real(store, item_id, url, why, *a, **k)

    monkeypatch.setattr(ni_flow, "_move_on", spy)
    return whys


@pytest.mark.parametrize("fmt,url", [("csv", _SYN_URL), ("feed", _SYN_FEED), ("xml", "https://data.synthetic.example/d.xml")])
def test_a_picked_row_that_answers_with_a_page_moves_on_saying_so(monkeypatch, fmt, url) -> None:
    whys = _spy_move_on(monkeypatch)
    _store_, _item, out, _seen = _run_flow_default_fetch(monkeypatch, fmt, url, _PAGES[0])
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "no_ready_source"  # nothing was built
    assert whys == ["returned a web page, not its data"]  # a fetch failure, not "has nothing for this right now"


def test_a_picked_json_row_that_answers_with_a_page_moves_on_saying_so(monkeypatch) -> None:
    store, whys = _store(), _spy_move_on(monkeypatch)

    def page(u: str, *a, **k):
        raise netguard.FetchError("upstream returned a web page, not JSON", kind="not_json",
                                  content_type="text/html")

    monkeypatch.setattr(ni_flow._netguard_mod, "safe_fetch_json", page)
    monkeypatch.setattr(ni_flow._netguard_mod, "safe_fetch_text",
                        lambda u, f="text", *a, **k: {"text": _PAGES[0], "content_type": "text/html"})
    url = "https://data.synthetic.example/series.json"
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _SynLib(None, [_row("syn", url, format="json")]))
    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", None)
    item_id = ni_flow.create_shell_item(store, "synthetic tally")
    ni_flow._flow_write(store, item_id, ni_flow._make_record("synthetic tally", "intent"))
    out = ni_flow.run_flow(store, item_id, gateway_call=lambda m, p: _syn_model(p), ni_route_model="mlx/test-local")
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "no_ready_source"
    assert whys == ["returned a web page, not its data"]


def test_many_leading_comments_sniff_in_linear_time() -> None:
    """A body of many leading comments and no page opener is data, decided fast; the same
    comments ahead of ``<html>`` are a page. No pattern may backtrack across comments."""
    import time

    from smartbrain_3000 import formats

    banner = "<!-- x -->\n" * 400
    started = time.monotonic()
    assert formats.looks_like_html(banner + "<rss/>") is False
    assert time.monotonic() - started < 0.05
    assert formats.looks_like_html("<!-- x -->\n" * 300 + "<html><body>maintenance</body></html>") is True
    assert formats.looks_like_html("<!-- never closed <html>") is False
    assert formats.looks_like_html('<?xml version="1.0"?>\n<!-- a --><!DOCTYPE html><html>') is True
