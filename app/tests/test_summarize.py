"""Map-reduce summarization of arbitrarily long documents (summarize.py).

The repo FORBIDS monkeypatching gateway.chat — these tests point SMARTBRAIN_LLM_GATEWAY_URL at the
real FakeGateway stand-in and assert on the exact requests the real gateway code puts on the wire
(chunk boundaries, map-then-reduce call counts, hierarchical reduce, focus threading, the wall-clock
budget). A non-local model id keeps the local serialization semaphore out of the picture.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from smartbrain_3000 import gateway, summarize

from _fakegateway import FakeGateway

_MODEL = "test/model"  # NOT mlx/ or ollama/ -> not serialized, so the map loop isn't gated in tests


@pytest.fixture()
def fake(monkeypatch) -> Iterator[FakeGateway]:
    server = FakeGateway().start()
    monkeypatch.setenv("SMARTBRAIN_LLM_GATEWAY_URL", server.url)
    gateway.set_pool(None)  # force the per-call client so it reads the env URL
    try:
        yield server
    finally:
        server.stop()


def _chats(fake: FakeGateway) -> list[dict]:
    return [r["body"] for r in fake.requests if r["path"] == "/v1/chat/completions"]


def _sys(body: dict) -> str:
    return body["messages"][0]["content"]


def _user(body: dict) -> str:
    return body["messages"][1]["content"]


def _maps(fake: FakeGateway) -> list[dict]:
    return [b for b in _chats(fake) if "summarizing section" in _sys(b)]


def _reduces(fake: FakeGateway) -> list[dict]:
    return [b for b in _chats(fake) if "merging section summaries" in _sys(b)]


class _Clock:
    """Deterministic monotonic stand-in returning a scripted sequence (last value repeats)."""

    def __init__(self, times: list[float]) -> None:
        self._times = times
        self._i = 0

    def __call__(self) -> float:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


def test_empty_content_makes_no_calls(fake: FakeGateway) -> None:
    out = summarize.summarize_document(_MODEL, "Doc", "   \n  ")
    assert out == {"title": "Doc", "summary": "", "chunks": 0, "chars_covered": 0, "total_chars": 6, "truncated": False, "passes": 0}
    assert _chats(fake) == []


def test_single_chunk_no_reduce(fake: FakeGateway) -> None:
    fake.reply_text = "SUMMARY"
    out = summarize.summarize_document(_MODEL, "Doc", "short body", chunk_chars=8000)
    assert out["chunks"] == 1 and out["passes"] == 0 and out["truncated"] is False
    assert out["chars_covered"] == out["total_chars"] == len("short body")
    assert out["summary"] == "SUMMARY"
    assert len(_maps(fake)) == 1 and len(_reduces(fake)) == 0  # one map, no reduce


def test_multi_chunk_maps_each_then_reduces_once(fake: FakeGateway) -> None:
    content = ("A" * 10) + ("B" * 10) + ("C" * 10)
    out = summarize.summarize_document(_MODEL, "Doc", content, chunk_chars=10)
    assert out["chunks"] == 3 and out["passes"] == 1 and out["truncated"] is False
    assert out["chars_covered"] == 30
    maps = _maps(fake)
    assert [_user(b) for b in maps] == ["A" * 10, "B" * 10, "C" * 10]  # exact chunk boundaries
    reduces = _reduces(fake)
    assert len(reduces) == 1 and "## Section 1" in _user(reduces[0])  # summaries merged, not raw chunks


def test_chunk_ceiling_inflates_to_cover_whole_doc(fake: FakeGateway) -> None:
    # A doc far bigger than chunk_chars * ceiling: chunk size scales up so it still fits the ceiling
    # and the WHOLE doc is covered (the operator's "hundreds of pages" case), not truncated.
    content = "x" * (100 * (summarize._MAX_SUMMARY_CHUNKS + 6))
    out = summarize.summarize_document(_MODEL, "Big", content, chunk_chars=100)
    assert out["chunks"] <= summarize._MAX_SUMMARY_CHUNKS
    assert len(_maps(fake)) == out["chunks"] <= summarize._MAX_SUMMARY_CHUNKS
    assert out["chars_covered"] == out["total_chars"] == len(content)  # full coverage via inflation
    assert out["truncated"] is False


def test_hierarchical_reduce_collapses_many_summaries(fake: FakeGateway) -> None:
    # 25 chunks -> reduce 25->3 (3 calls) -> 3->1 (1 call): 2 passes, 4 reduce calls total.
    content = "y" * (25 * 40)
    out = summarize.summarize_document(_MODEL, "Doc", content, chunk_chars=40)
    assert out["chunks"] == 25 and out["passes"] == 2 and out["truncated"] is False
    assert len(_maps(fake)) == 25
    assert len(_reduces(fake)) == 4  # ceil(25/10)=3 then ceil(3/10)=1


def test_focus_threads_into_map_and_reduce_prompts(fake: FakeGateway) -> None:
    content = ("A" * 10) + ("B" * 10)
    summarize.summarize_document(_MODEL, "Doc", content, focus="the annual budget", chunk_chars=10)
    assert all("Focus especially on: the annual budget" in _sys(b) for b in _maps(fake))
    assert all("Focus especially on: the annual budget" in _sys(b) for b in _reduces(fake))


def test_wallclock_budget_stops_and_marks_truncated(fake: FakeGateway) -> None:
    # deadline computed on the first now(); the second now() (before chunk 2) is already past it, so
    # only the first of three chunks is summarized and the result is flagged truncated with partial coverage.
    content = ("A" * 10) + ("B" * 10) + ("C" * 10)
    clock = _Clock([0.0, 101.0])
    out = summarize.summarize_document(_MODEL, "Doc", content, chunk_chars=10, budget=100.0, now=clock)
    assert out["truncated"] is True
    assert out["chunks"] == 1 and out["chars_covered"] == 10 and out["total_chars"] == 30
    assert len(_maps(fake)) == 1


def test_first_chunk_failure_surfaces_error(fake: FakeGateway) -> None:
    # Model unreachable on the very first map call -> raise (don't hide it as an empty summary).
    fake.fail_status = 502
    with pytest.raises(gateway.GatewayError):
        summarize.summarize_document(_MODEL, "Doc", "some body text", chunk_chars=8000)


def test_chunk_chars_for_scales_and_clamps() -> None:
    assert summarize.chunk_chars_for(200000) == summarize._MAX_CHUNK_CHARS
    assert summarize.chunk_chars_for(5000) == summarize._MIN_CHUNK_CHARS
    mid = summarize.chunk_chars_for(20000)
    assert mid == 20000 and summarize._MIN_CHUNK_CHARS <= mid <= summarize._MAX_CHUNK_CHARS


def test_summarize_document_tool_end_to_end(fake: FakeGateway) -> None:
    # The whole path a chat turn uses: KB doc -> tools.run(OBSERVE) -> docsum map-reduce -> gateway.
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import tools
    from smartbrain_3000.audit import AuditLog
    from smartbrain_3000.kb import KnowledgeBase
    from smartbrain_3000.secrets import gen_master_key

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    kb.add("Perennial", "P" * 60)  # small doc -> a single map chunk here (mechanics covered above)
    ctx = tools.ToolContext(kb=kb, model=_MODEL)  # model set as the agent loop would via replace()
    fake.reply_text = "MERGED SUMMARY"

    out = tools.run(ctx, AuditLog(conn, key), "summarize_document", {"query": "Perennial"}, actor="assistant")
    assert out["title"] == "Perennial" and out["summary"] == "MERGED SUMMARY"
    assert out["total_chars"] == 60 and out["chars_covered"] == 60 and out["truncated"] is False
    assert set(out) == {"title", "chunks", "chars_covered", "total_chars", "truncated", "passes", "summary"}
    assert _maps(fake)  # it actually called the model to map at least one chunk
