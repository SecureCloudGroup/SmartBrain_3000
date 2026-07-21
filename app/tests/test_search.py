"""Tests for keyless web search (search.py) + the web_search tool."""

from __future__ import annotations

import pytest

from smartbrain_3000 import netguard, search, tools

_DDG_HTML = """<html><body>
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopenai.com%2Fabout&rut=x">About OpenAI</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopenai.com%2Fabout">OpenAI is an AI research company; its CTO leads engineering.</a>
</div>
<div class="result results_links">
  <h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FOpenAI">OpenAI - Wikipedia</a></h2>
  <a class="result__snippet">OpenAI is an American artificial intelligence organization.</a>
</div>
</body></html>"""


def test_parse_results_extracts_title_url_snippet() -> None:
    results = search.parse_results(_DDG_HTML, 5)
    assert len(results) == 2
    assert results[0]["title"] == "About OpenAI"
    assert results[0]["url"] == "https://openai.com/about"  # uddg redirect decoded
    assert "CTO" in results[0]["snippet"]
    assert results[1]["url"] == "https://en.wikipedia.org/wiki/OpenAI"


def test_parse_results_respects_limit() -> None:
    assert len(search.parse_results(_DDG_HTML, 1)) == 1


def test_web_search_uses_guarded_fetch(monkeypatch) -> None:
    seen = {}

    def fake(url):
        seen["url"] = url
        return {"final_url": url, "status": 200, "text": _DDG_HTML}

    monkeypatch.setattr(netguard, "safe_fetch", fake)
    results = search.web_search("OpenAI CTO", limit=3)
    assert "duckduckgo.com" in seen["url"] and "OpenAI" in seen["url"].replace("+", " ")
    assert len(results) == 2 and results[0]["url"].startswith("http")


def test_web_search_maps_fetch_error(monkeypatch) -> None:
    def boom(url):
        raise netguard.FetchError("blocked")

    monkeypatch.setattr(netguard, "safe_fetch", boom)
    with pytest.raises(search.SearchError):
        search.web_search("anything")


def test_web_search_tool_is_reviewed_egress() -> None:
    tool = tools.get_tool("web_search")
    assert tool is not None
    assert tool.tier is tools.Tier.REVIEWED and tool.egress is True


# --- provider registry (A2) -------------------------------------------------

def _svc(**kw) -> search.SearchService:
    return search.SearchService(**kw)


def test_configured_order_anchors_ddg_last() -> None:
    s = _svc(searxng_url="https://sx.example", brave_key="b", tavily_key="t")
    assert s.configured() == ["searxng", "brave", "tavily", "ddg"]
    assert _svc().configured() == ["ddg"], "keyless install still searches"


def test_engine_pin_moves_provider_first() -> None:
    s = _svc(engine="tavily", searxng_url="https://sx.example", tavily_key="t")
    assert s._chain()[0] == "tavily"
    # A pinned-but-unconfigured engine can't be honored — the chain just proceeds.
    assert _svc(engine="brave")._chain() == ["ddg"]


def test_provider_error_falls_back_down_the_chain(monkeypatch) -> None:
    s = _svc(searxng_url="https://sx.example", brave_key="b")

    def sx_fail(url, headers=None):
        raise netguard.FetchError("searxng down")

    monkeypatch.setattr(netguard, "safe_fetch_json", sx_fail)
    calls = {}

    def ddg_ok(query, limit):
        calls["ddg"] = query
        return [{"title": "T", "url": "https://x.example/a", "snippet": ""}]

    monkeypatch.setattr(search, "web_search", ddg_ok)
    # brave also rides safe_fetch_json (patched to fail) -> lands on ddg
    out = s.search("hello", 3)
    assert out["engine"] == "ddg" and calls["ddg"] == "hello"


def test_provider_parsers_shape_results() -> None:
    sx = search._parse_searxng({"results": [{"title": "A", "url": "https://a", "content": "s"}]}, 5)
    br = search._parse_brave({"web": {"results": [{"title": "B", "url": "https://b", "description": "d"}]}}, 5)
    tv = search._parse_tavily({"results": [{"title": "C", "url": "https://c", "content": "x"}]}, 5)
    for out, title in ((sx, "A"), (br, "B"), (tv, "C")):
        assert out[0]["title"] == title and set(out[0]) == {"title", "url", "snippet"}


def test_service_from_reads_meta_and_secrets() -> None:
    import duckdb

    from smartbrain_3000 import db as dbmod

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    dbmod.meta_set(conn, search.META_ENGINE, "brave")
    dbmod.meta_set(conn, search.META_SEARXNG_URL, "https://sx.example")
    secrets = {search.SECRET_BRAVE: "bk"}
    s = search.service_from(conn, secrets.get)
    assert s.engine == "brave" and s.searxng_url == "https://sx.example"
    assert s._chain()[0] == "brave"


def test_service_from_tolerates_bad_engine() -> None:
    import duckdb

    from smartbrain_3000 import db as dbmod

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    dbmod.meta_set(conn, search.META_ENGINE, "bogus")
    assert search.service_from(conn, lambda k: None).engine == "auto"
