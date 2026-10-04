"""Review-2 fixes (2026-10-04) for library_resolve + fetch-side: text-fill contractions, host-param
validation, lookup-value hygiene."""

from __future__ import annotations

import pytest

from smartbrain_3000 import library_resolve as lr

# ---- F8: _text_fill drops contraction fragments ----------------------------------------------

def test_f8_text_fill_drops_single_char_contraction_fragments() -> None:
    """``norm("what's")`` = "what s"; the old fill passed "s" through (tvmaze?q=s). Drop single-char
    tokens — a text fill demands a word of the subject the user named."""
    with pytest.raises(lr.Unfillable):
        lr._text_fill({"from": "text"}, "what's on", own_words=frozenset({"on"}))
    # a real noun passes through unchanged
    assert lr._text_fill({"from": "text"}, "latest react version",
                        own_words=frozenset({"latest", "version"})) == "react"


def test_f8_text_fill_drops_contraction_leftovers_before_real_tokens() -> None:
    """"nbc's on tv" would land as "s nbc" / "s tv" without the drop; the real token "nbc" leads."""
    out = lr._text_fill({"from": "text"}, "nbc's on tv", own_words=frozenset({"on", "tv"}))
    assert out == "nbc", out


def test_f8_text_fill_keeps_single_char_non_contraction_tokens() -> None:
    """K-R (2026-10-04): a genuine 1-char subject word (R, Q, X) is NOT a contraction fragment and
    must not be dropped — "latest R version" asks for the R language's version, not "latest version"."""
    assert lr._text_fill({"from": "text"}, "latest R version",
                        own_words=frozenset({"version"})) == "r"


# ---- FETCH-F3: host-parameter values must be a bare host ------------------------------------

def test_fetch_f3_host_param_refuses_userinfo_port_and_ip_literals() -> None:
    """``https://{feed}`` fills raw; the resolver would land a credentials-bearing or IP host inside
    the URL. The host-param value must be a bare host, optionally a path (with that path's query)."""
    template = "https://{feed}/rss"
    bad_values = [
        "news.example.com@203.0.113.9:8443/rss?x=1#",   # userinfo + port + query + fragment
        "203.0.113.9",                                   # bare IP
        "news.example.com:8443",                         # port
        "news.example.com?x=1",                          # query on the host, no path
        "news.example.com#frag",                         # fragment
        "user@news.example.com",                         # userinfo
    ]
    for value in bad_values:
        got = lr._expand(template, {"feed": [(value, "")]}, groups={"feed": "statuspage"})
        # a refused host-param value becomes NO candidate (so the resolver falls to its sibling)
        assert got == [], (value, got)


def test_fetch_f3_a_feed_path_with_its_query_still_fills() -> None:
    """Local-news feeds are host + path + query (``/arc/outboundfeeds/rss/?outputType=xml``)."""
    value = "www.kiro7.com/arc/outboundfeeds/rss/?outputType=xml"
    got = lr._expand("https://{feed}", {"feed": [(value, "")]}, groups={"feed": "local_news"})
    assert [c["url"] for c in got] == ["https://" + value], got


def test_fetch_f3_a_plain_host_still_fills() -> None:
    """A clean host-only value is unchanged (the exception the F3 rule is defending)."""
    got = lr._expand("https://{feed}/rss", {"feed": [("news.example.com", "")]},
                     groups={"feed": "statuspage"})
    assert len(got) == 1 and got[0]["url"] == "https://news.example.com/rss"


# ---- FETCH-F5: resolve_lookup validates each helper-fetched value --------------------------

_HELPER_URL = "https://api.example.org/points/36.1,-95.9"
_TMPL_URL = "https://api.example.org/f/{office}/forecast"


def _cand(value_url: str = _TMPL_URL) -> dict:
    return {"url": value_url, "lookup": [{"url": _HELPER_URL, "path": "office", "param": "office"}]}


@pytest.mark.parametrize("value", [".", "..", "a" * 65, "has space", "with?q=1",
                                    "slash/segment", "at@host", "amp&and", "ha$h"])
def test_fetch_f5_resolve_lookup_refuses_unsafe_values(value) -> None:
    """A helper's JSON is untrusted; its parts can only form a single safe URL path/query segment.
    Dot sentinels, oversize, and anything outside ``[A-Za-z0-9,._:-]`` is a ValueError."""
    with pytest.raises(ValueError):
        lr.resolve_lookup(_cand(), lambda _u: {"office": value})


def test_fetch_f5_a_normal_value_still_fills() -> None:
    assert lr.resolve_lookup(_cand(), lambda _u: {"office": "DMX"}).endswith("/f/DMX/forecast")
