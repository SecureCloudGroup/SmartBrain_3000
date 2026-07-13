"""Server-side map-reduce summarization of arbitrarily long documents.

A single model context cannot hold a hundreds-of-pages document (~1M chars ≈ 250k tokens, over even
gemma-4's 262k window once you add the prompt + answer), so ``summarize_document`` runs a map-reduce
entirely **inside one tool call**: split the doc into large chunks, summarize each ("map"), then merge
the summaries — hierarchically if there are many ("reduce"). It is bounded three ways so it can never
run away: a chunk-count ceiling, a reduce-pass ceiling, and a wall-clock budget (a local model is slow;
on hitting the budget it stops, reduces what it covered, and returns ``truncated: true``).

Kept out of ``tools.py`` (mirroring ``ingest``/``search``) so the tool handler stays a thin wrapper.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from . import gateway

_MAX_SUMMARY_CHUNKS = 64      # hard ceiling on map chunks; chunk size scales up so a max-size doc fits
_MAX_SUMMARY_PASSES = 3       # hard ceiling on reduce levels (fan-in 10 collapses 64 summaries in 2)
_REDUCE_FANIN = 10            # summaries merged per reduce call
_WALLCLOCK_BUDGET = 1800.0    # seconds: hard ceiling on the whole job (safety net; real docs finish under)
_CHUNK_TIMEOUT = 300.0        # seconds: per gateway chat call (generous — a cold local model is slow)
_DEFAULT_CHUNK_CHARS = 24000  # ~6k tokens: fallback chunk size when the caller can't size to the model
_MIN_CHUNK_CHARS = 8000       # keep chunks large enough to be worth a model round-trip
_MAX_CHUNK_CHARS = 48000      # cap chunk size for summarization granularity (a huge chunk over-compresses)
_MAX_TITLE = 200
_MAX_FOCUS = 500


def chunk_chars_for(result_cap: int) -> int:
    """Map chunk size scaled to the model's result cap, bounded for summarization granularity."""
    return max(_MIN_CHUNK_CHARS, min(int(result_cap), _MAX_CHUNK_CHARS))


def _focus_clause(focus: str) -> str:
    return f" Focus especially on: {focus}." if focus else ""


def _map_call(model: str, title: str, focus: str, chunk: str, idx: int, total: int) -> str:
    system = (
        f"You are summarizing section {idx + 1} of {total} of a longer document titled "
        f"\"{title}\". Write a faithful, self-contained summary of THIS section's key points — facts, "
        f"names, numbers, dates, and decisions. Use only information present in the section; do not "
        f"invent or infer beyond it.{_focus_clause(focus)}"
    )
    data = gateway.chat([{"role": "system", "content": system}, {"role": "user", "content": chunk}], model, timeout=_CHUNK_TIMEOUT)
    return gateway.completion_text(data).strip()


def _reduce_call(model: str, title: str, focus: str, parts: list[str]) -> str:
    joined = "\n\n".join(f"## Section {i + 1}\n{p}" for i, p in enumerate(parts))
    system = (
        f"You are merging section summaries of a document titled \"{title}\" into ONE coherent "
        f"summary. Preserve the key facts, names, numbers, dates, and overall structure; remove "
        f"redundancy; stay faithful to the summaries and add nothing new.{_focus_clause(focus)}"
    )
    data = gateway.chat([{"role": "system", "content": system}, {"role": "user", "content": joined}], model, timeout=_CHUNK_TIMEOUT)
    return gateway.completion_text(data).strip()


def _chunk_size(total: int, chunk_chars: int) -> int:
    """Chunk size that keeps the chunk count at or under the ceiling, inflating if the doc is huge.

    For any doc within the KB's ``ingest._MAX_TEXT`` (1M chars) ceiling the inflated size stays at or
    below ``_MAX_CHUNK_CHARS`` (since even the smallest ``base`` inflates to <=1M/64 ~= 15.6k). A doc
    past ~3M chars (beyond that stored-size cap) could inflate past ``_MAX_CHUNK_CHARS``; a small-context
    model would then fail the first map call and the tool surfaces that error rather than misreporting."""
    base = max(1, chunk_chars)
    if total <= base * _MAX_SUMMARY_CHUNKS:
        return base
    return math.ceil(total / _MAX_SUMMARY_CHUNKS)  # a max-size doc collapses into exactly the ceiling


def _reduce(model: str, title: str, focus: str, summaries: list[str], deadline: float, now: Callable[[], float]) -> tuple[str, int, bool]:
    """Merge chunk summaries into one, hierarchically. Returns (summary, reduce_passes, truncated)."""
    if not summaries:
        return "", 0, False
    passes = 0
    truncated = False
    while len(summaries) > 1 and passes < _MAX_SUMMARY_PASSES:
        passes += 1
        nxt: list[str] = []
        for i in range(0, len(summaries), _REDUCE_FANIN):
            if now() >= deadline:
                nxt.extend(summaries[i:])  # out of budget mid-reduce: carry the rest forward unmerged
                truncated = True
                break
            nxt.append(_reduce_call(model, title, focus, summaries[i:i + _REDUCE_FANIN]))
        summaries = nxt
        if truncated:
            break
    if len(summaries) > 1:  # pass ceiling or budget reached with several left — one last-resort merge
        if now() < deadline:
            summaries = [_reduce_call(model, title, focus, summaries)]
        else:
            summaries, truncated = ["\n\n".join(summaries)], True
    return summaries[0], passes, truncated


def summarize_document(
    model: str,
    title: str,
    content: str,
    *,
    focus: str = "",
    chunk_chars: int = _DEFAULT_CHUNK_CHARS,
    budget: float = _WALLCLOCK_BUDGET,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Map-reduce summarize ``content`` with ``model``. Returns title/summary + coverage metadata.

    ``truncated`` is True when the wall-clock budget cut the job short — EITHER before every chunk was
    mapped (then ``chars_covered`` < ``total_chars`` and the summary reflects only the covered head) OR
    during the reduce/merge (full coverage, but the merge is coarser than a complete one). So
    ``truncated`` implies "not a clean full-quality summary"; it does NOT imply ``chars_covered`` < total.
    Raises ``gateway.GatewayError`` only if the very first chunk fails (model unreachable) — a failure
    after partial progress degrades to a truncated result rather than losing the work already done.
    """
    assert model, "model required"
    title = (title or "")[:_MAX_TITLE]
    focus = (focus or "")[:_MAX_FOCUS]
    total = len(content)
    if not content.strip():
        return {"title": title, "summary": "", "chunks": 0, "chars_covered": 0, "total_chars": total, "truncated": False, "passes": 0}

    deadline = now() + budget
    size = _chunk_size(total, chunk_chars)
    chunks = [content[i:i + size] for i in range(0, total, size)]
    summaries: list[str] = []
    covered = 0
    truncated = False
    for idx, chunk in enumerate(chunks):
        if idx > 0 and now() >= deadline:  # always summarize at least one chunk before giving up
            truncated = True
            break
        try:
            summaries.append(_map_call(model, title, focus, chunk, idx, len(chunks)))
        except gateway.GatewayError:
            if not summaries:
                raise  # first chunk failed: the model is unreachable — surface it, don't hide as empty
            truncated = True
            break
        covered += len(chunk)
    if len(summaries) < len(chunks):
        truncated = True  # didn't cover every chunk

    summary, passes, reduce_truncated = _reduce(model, title, focus, summaries, deadline, now)
    return {
        "title": title,
        "summary": summary,
        "chunks": len(summaries),
        "chars_covered": covered,
        "total_chars": total,
        "truncated": truncated or reduce_truncated,
        "passes": passes,
    }
