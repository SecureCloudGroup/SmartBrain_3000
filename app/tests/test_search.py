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
