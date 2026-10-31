"""``tools/ni-capture-fixtures.py``: the capture that writes the provider bodies the proof tests replay. Its first run
(T's Mac, 2026-10-31) saved a "1 request per second" sentence as the daily series, ``captured: true``, because it
sent requests back to back and never looked at what came back. These pin the pacing, the kind check, the burst and
the key refusal, with a fake transport: no network."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-capture-fixtures.py"
pytestmark = pytest.mark.skipif(not TOOL.exists(), reason="the capture tool ships in the repository only")


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("ni_capture_fixtures_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(tool, name: str) -> dict:
    return next(e for e in tool.CATALOGUE if e["name"] == name)


def test_requests_to_one_host_are_paced_and_other_hosts_are_not(tool) -> None:
    now, slept = [0.0], []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = tool.Pacer(1.5, clock=lambda: now[0], sleep=sleep)
    pacer.wait("www.alphavantage.co")
    pacer.wait("finnhub.io")
    assert slept == [], "a first request to each host goes at once"
    now[0] += 0.4
    pacer.wait("www.alphavantage.co")
    assert slept == [pytest.approx(1.1)], "the rest of the pace"
    pacer.mark("www.alphavantage.co")
    assert len(slept) == 1, "a burst request is counted, not spaced"
    now[0] += 5.0
    pacer.wait("www.alphavantage.co")
    assert len(slept) == 1


def test_a_body_of_another_kind_is_named_and_never_passes(tool) -> None:
    quote = json.loads((pathlib.Path(__file__).parent / "fixtures" / "providers" / "www.alphavantage.co"
                        / "alphavantage-global-quote.json").read_text(encoding="utf-8"))
    limit = {"Information": "Please consider spreading out your free API requests more sparingly (1 request per "
                            "second)."}
    daily = _entry(tool, "alphavantage-time-series-daily")
    assert tool.check(daily, {"status": 200, "body": limit}) == \
        ["a limit body, expected data", "no Meta Data", "no Time Series (Daily)"]
    assert tool.check(_entry(tool, "alphavantage-global-quote"), {"status": 200, "body": quote["body"]}) == []
    assert tool.check(_entry(tool, "alphavantage-burst-limited"), {"status": 200, "body": quote["body"]}) == \
        ["a data body, expected limit"]
    assert tool.check(_entry(tool, "finnhub-quote-no-key"), {"status": 200, "body": {"error": "Please use an API key."}}) \
        == ["HTTP 200, expected 401"]
    assert tool.kind_of(limit) == "limit" and tool.kind_of({"Information": "The demo API key is for demo purposes "
                                                                           "only."}) == "message"


def test_a_burst_entry_keeps_the_first_limited_answer_and_paces_only_its_first_request(tool) -> None:
    limit = {"status": 200, "content_type": "application/json",
             "body": {"Information": "Please consider spreading out your free API requests more sparingly (1 request "
                                     "per second)."}}
    data = {"status": 200, "content_type": "application/json", "body": {"Global Quote": {"05. price": "1.0"}}}
    answers, seen = [data, limit, data], []

    def fetcher(entry, keys, pacer, *, paced):
        seen.append(paced)
        return answers.pop(0)

    got, problems = tool.capture(_entry(tool, "alphavantage-burst-limited"), {}, tool.Pacer(0.0), fetcher)
    assert got is limit and problems == [] and seen == [True, False], "stopped at the limit, the second unpaced"
    answers[:] = [data, data, data]
    seen.clear()
    got, problems = tool.capture(_entry(tool, "alphavantage-burst-limited"), {}, tool.Pacer(0.0), fetcher)
    assert problems == ["a data body, expected limit"] and seen == [True, False, False], "at most the burst, honestly"
    answers[:] = [limit]
    seen.clear()
    got, problems = tool.capture(_entry(tool, "alphavantage-global-quote"), {}, tool.Pacer(0.0), fetcher)
    assert seen == [True] and problems, "one request for any other entry, refused when it is not the body"


def test_every_documented_shape_is_the_kind_its_entry_expects_and_names_no_key(tool, tmp_path, monkeypatch,
                                                                               capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["ni-capture-fixtures", "--out", str(tmp_path), "--provisional"])
    assert tool.main() == 0
    files = sorted(tmp_path.glob("*/*.json"))
    assert len(files) == len(tool.CATALOGUE)
    for path in files:  # bounded: the catalogue
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["captured"] is False and tool.check(_entry(tool, record["name"]), record) == []
        if record["key"]:
            assert "[redacted]" in record["url"]


def test_an_unknown_name_and_a_body_carrying_the_key_are_refused(tool, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ni-capture-fixtures", "--out", str(tmp_path), "--only", "alphavantage-bad-key"])
    assert tool.main() == 2, "a name the catalogue no longer has"
    entry = _entry(tool, "finnhub-quote")
    with pytest.raises(ValueError):
        tool.write(entry, {"status": 200, "content_type": "application/json", "body": {"echo": "secret-key-123"}},
                   tmp_path, captured=True, secret="secret-key-123")
    assert not list(tmp_path.glob("*/*.json")), "nothing written"
