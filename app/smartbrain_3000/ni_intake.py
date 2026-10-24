"""Library intake: a card the user Keeps becomes a source in their own Library, reused by the next ask that asks the
same question (release plan P1, T 2026-10-24: "the Library is the product, research is its intake").

``remember`` runs on Keep. A card built outside the SmartBrain Library (research, or a link the user pasted) is
written into the user's own sources (``library_index.LocalSources``, sealed user data that never leaves the device)
with what it answered: the question, the user's words, the draft that built it and the trusted title of its source.

``reuse`` runs before the Library auto-pick. A kept source whose question matches the new ask (the same kind and
place, the new subject's words all among the words it was kept for, and every name the new ask carries known to it:
``ni_verify.named``; a source's title counts only for names) is read once and the kept draft runs through the
researcher's own tools: the fetch obeys robots.txt and the per-site budget, is audited (``ni_kept_pick``) and does
not happen while the open web is off (``ni_research_web``), and every check runs against the NEW ask (place,
window, threshold, compare, a fixed date). Only a draft that passes lands, held for Keep like every card from
outside the Library. No model is called, and a fault in intake goes on to the Library: it never fails the ask. A source that fails to fetch three times in a row is
forgotten, and so is a source the user says No to for the question it was offered for (``forget``). Compare cards
are not kept (P1).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
from datetime import UTC
from zoneinfo import ZoneInfo

from . import library_index, ni, ni_flow, ni_recipe, ni_research_flow, ni_standing
from .ni_research import loop, tools
from .ni_research.ctx import ResearchCtx, normal_url, one_line
from .ni_verify import named
from .ni_verify.text import _WINDOW_WORDS, _answer_tokens, _folded_words

log = logging.getLogger(__name__)

KEPT_PREFIX = "local-k"     # a kept row's id (a ``local-`` id, so the Library page lists and deletes it as yours)
KEEP_PATHS = frozenset({"research", "mapping", "kept"})   # built outside the Library (the Library's rows are found
# anyway); a card rebuilt from a kept source refreshes its entry
MAX_ENTRIES = 5             # questions one kept address answers
MAX_MISSES = 3              # failed fetches in a row before a kept address is forgotten
MAX_TRIES = 2               # kept addresses read for one ask
_DRAFT_KEYS = ("version", "title", "goal", "params", "source", "pipeline", "scene", "display", "model")
_FETCH_MISSES = frozenset({"fetch_failed", "not_data", "robots", "redirect", "needs_key"})
_LOCK = threading.Lock()    # guards read-merge-write of the sources slot


def _draft_of(spec: dict) -> dict:
    """The researcher-shaped draft a kept card replays: its source, pipeline and scene, nothing it sealed."""
    assert isinstance(spec, dict), "spec required"
    return {k: copy.deepcopy(spec[k]) for k in _DRAFT_KEYS if k in spec}


def _question_of(store: ni.NIStore, item: dict) -> dict | None:
    """The question this card answers: its sealed recipe's, else (a pasted link) the one its flow understood."""
    assert isinstance(item, dict) and item.get("id"), "an item"
    question = (item["spec"].get("recipe") or {}).get("question")
    if isinstance(question, dict):
        return question
    record = ni_flow._flow_read(store, item["id"]) or {}
    intent = record.get("intent")
    if not isinstance(intent, dict):
        return None
    return ni_research_flow.question_from_intent(str(item["spec"].get("goal") or record.get("request") or ""), intent)


def remember(store: ni.NIStore, item: dict) -> bool:
    """On Keep: write a card built outside the Library into the user's own sources (True when written). A card that
    could not be replayed as it stands (a key, a model step, a rendered page, a compare) is not kept."""
    assert store is not None, "store required"
    assert isinstance(item, dict) and isinstance(item.get("spec"), dict), "an item"
    spec = item["spec"]
    built = spec.get("_built_from") or {}
    if built.get("path") not in KEEP_PATHS:
        return False
    question = _question_of(store, item)
    draft = _draft_of(spec)
    url = str((spec.get("source") or {}).get("url") or "")
    if (question is None or question.get("entities") or not url.startswith("https://")
            or (draft.get("source") or {}).get("type") != "http_json"):
        return False
    try:
        ni_recipe.check_researcher_draft(draft)
    except ValueError:
        return False   # not replayable by the researcher's tools (a key, a model step): nothing to reuse
    entry = {"question": question, "request": one_line(str(spec.get("goal") or ""), 500), "draft": draft,
             "title": str(built.get("title") or ""), "at": ni._clock().astimezone(UTC).isoformat()[:40], "misses": 0}
    with _LOCK:
        sources = library_index.LocalSources(store)
        rows = sources.list()
        key = normal_url(url)
        row = next((r for r in rows if _row_ok(r) and r["kept"]["key"] == key), None)
        if row is None:
            row = _new_row(key, url, entry)
            rows = _room_for_one(rows) + [row]
        entries = [e for e in _entries(row) if e["question"] != question]
        row["kept"]["entries"] = [*entries, entry][-MAX_ENTRIES:]
        _write(store, rows)
    ni_standing.rematch(store)   # P2: every open standing order looks again at this new source, at no model cost
    return True


def forget(store: ni.NIStore, item: dict) -> int:
    """On No to a held card: the user's own source at its address is no answer to questions like this one, so its
    kept entries that match the card's question go (a row with none left goes too). Returns how many went."""
    assert store is not None, "store required"
    assert isinstance(item, dict) and isinstance(item.get("spec"), dict), "an item"
    url = str((item["spec"].get("source") or {}).get("url") or "")
    question = _question_of(store, item) if url else None
    if question is None:
        return 0
    request = str(item["spec"].get("goal") or "")
    key = normal_url(url)
    with _LOCK:
        rows = library_index.LocalSources(store).list()
        gone = 0
        for r in rows:  # bounded: the user's own sources
            if not _row_ok(r) or r["kept"]["key"] != key:
                continue
            left = [e for e in _entries(r) if not matches(question, request, e)]
            gone += len(r["kept"].get("entries") or []) - len(left)
            r["kept"]["entries"] = left
        if gone:
            _write(store, rows)
    return gone


def _new_row(key: str, url: str, entry: dict) -> dict:
    """A kept address as one of the user's own sources (the Add-a-source record shape, so the Library page shows it)."""
    assert key and url, "an address"
    host = (url.split("/")[2] if url.count("/") >= 2 else "")[:253]
    name = one_line(entry["title"] or entry["draft"].get("title") or host, 120)
    return {"id": KEPT_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:11], "name": name,
            "description": one_line(f"Kept for: {entry['request']}", 600),
            "provider": {"id": "local", "name": host, "url": f"https://{host}", "authority": "community"},
            "tier": "local", "categories": [], "kinds": ["lookup"], "coverage": {"geo": "local", "entity": ""},
            "access": {"kind": "http_json", "url_template": url, "params": [], "auth": "none", "headers": {},
                       "docs_url": url},
            "terms": {"status": "unverified", "note": "kept by you", "terms_url": ""},
            "freshness": {"cadence": "irregular"}, "examples": [], "notes": "",
            "origin": {"by": "keep", "at": entry["at"]}, "validation": {"status": "unvalidated"},
            "votes": {"yes": 0, "no": 0}, "kept": {"key": key, "entries": []}}


def _room_for_one(rows: list[dict]) -> list[dict]:
    """The rows with room for one more: at the cap the oldest kept row goes (never a source the user typed in)."""
    assert isinstance(rows, list), "rows"
    if len(rows) < library_index.MAX_LOCAL_SOURCES:
        return rows
    kept = [r for r in rows if r.get("kept")]
    if not kept:
        raise ValueError("your own sources are full")
    oldest = min(kept, key=lambda r: max((e["at"] for e in _entries(r)), default=""))
    return [r for r in rows if r is not oldest]


def _write(store: ni.NIStore, rows: list[dict]) -> None:
    """Store the sources slot (rows with no kept entry left are dropped)."""
    assert isinstance(rows, list), "rows"
    keep = [r for r in rows if not r.get("kept") or _entries(r)]
    store.write_reserved_snapshot(library_index.LOCAL_RESERVED_ID, library_index.LOCAL_SLOT, {"sources": keep})


# --- reuse ---------------------------------------------------------------------------------------------------------

def _place_words(question: dict) -> set[str]:
    """The words of the places a question names, every one kept ("OR" and "ME" are states, not fillers)."""
    assert isinstance(question, dict), "a question"
    return set().union(*(_folded_words(str(p)) for p in (question.get("places") or [])[:4]))


def _asked(new: dict, request: str, names: list[str] | tuple = ()) -> list[str]:
    """The names this ask carries (``names``: the intent's), read once per ask."""
    assert isinstance(new, dict) and isinstance(request, str), "the ask"
    return named.phrases(request, list(names)[:8], [str(p) for p in (new.get("places") or [])[:4]])


def matches(new: dict, request: str, kept: dict, names: list[str] | tuple = ()) -> bool:
    """Does a kept entry answer this question? The same kind; the same place (one names the other's words, or
    neither names one); every word of the new subject (fillers, window words and the place left out) among the
    words the user kept it for (its subject and the user's words then: a source's title lists what the page has,
    not what it was kept for); and every name the new ask carries (``names``: the intent's) known to the entry by
    those words, its source's title or its address ("AP top story" is not answered by a source kept for "BBC
    headlines"). The checks then decide on the fresh sample."""
    return _fits(new, request, kept, _asked(new, request, names))


def _fits(new: dict, request: str, kept: dict, asked: list[str]) -> bool:
    """``matches`` with the ask's names already read."""
    assert isinstance(new, dict) and _entry_ok(kept), "questions"
    old = kept["question"]
    if new.get("entities") or old.get("entities") or new.get("kind") != old.get("kind"):
        return False
    here, there = _place_words(new), _place_words(old)
    if not (here <= there or there <= here) or bool(here) != bool(there):
        return False
    want = _answer_tokens(str(new.get("subject") or request)) - _WINDOW_WORDS - here
    have = _answer_tokens(" ".join([str(old.get("subject") or ""), kept["request"]]))
    if not want or not want <= have:
        return False
    known = [kept["request"], str(old.get("subject") or ""), kept["title"],
             str((kept["draft"].get("source") or {}).get("url") or "")]
    return not named.check_named(asked, "", known)


def reuse(store: ni.NIStore, item_id: str, request: str, intent: dict) -> dict | None:
    """Build this ask from a kept source when one fits and passes every check: the landed (held) card's flow record,
    or None (nothing kept fits, or the open web is off; the flow goes on to the Library and research). A kept source
    that faults is skipped (the next one may serve; intake is a convenience, never a gate)."""
    assert store is not None and item_id, "args required"
    assert isinstance(request, str) and isinstance(intent, dict), "the ask"
    record = ni_flow._flow_read(store, item_id) or {}
    question = ni_research_flow.question_from_intent(request, intent)
    if record.get("_cancel") or question["entities"] or not ni_flow._research_web(store):
        return None   # a kept address is a site on the open web: ``ni_research_web = off`` keeps to the Library
    declined = ni_research_flow._declined_of(record)
    asked = _asked(question, request, [n for n in intent.get("names") or [] if isinstance(n, str)])
    fits = [(r, e) for r in library_index.LocalSources(store).list() if _row_ok(r) and r["kept"]["key"] not in declined
            for e in _entries(r) if _fits(question, request, e, asked)]
    fits.sort(key=lambda pair: pair[1]["at"], reverse=True)   # the most recently kept first
    for n, (row, entry) in enumerate(fits[:MAX_TRIES], 1):  # bounded: a couple of reads per ask
        try:
            landed = _try(store, item_id, request, intent, question, row, entry, n)
        except Exception as exc:  # one kept source's fault never hides the next one, the Library or research
            log.warning("ni_intake: a kept source faulted: %s", type(exc).__name__)
            continue
        if landed is not None:
            return landed
    return None


def _row_ok(row: object) -> bool:
    """A kept row this version can read: a row is user data that outlives upgrades, so a malformed one is skipped."""
    return (isinstance(row, dict) and isinstance(row.get("kept"), dict) and isinstance(row["kept"].get("key"), str)
            and isinstance(row.get("access"), dict) and isinstance(row["access"].get("url_template"), str)
            and isinstance(row.get("provider"), dict))


def _entry_ok(entry: object) -> bool:
    """A kept entry this version can read (the shape ``remember`` writes)."""
    return (isinstance(entry, dict) and isinstance(entry.get("question"), dict)
            and all(isinstance(entry.get(k), str) for k in ("request", "title", "at"))
            and isinstance(entry.get("draft"), dict))


def _entries(row: dict) -> list[dict]:
    """A kept row's readable entries (none when it is not a kept row)."""
    entries = row["kept"].get("entries") if isinstance(row.get("kept"), dict) else None
    return [e for e in entries[:MAX_ENTRIES] if _entry_ok(e)] if isinstance(entries, list) else []


def _try(store: ni.NIStore, item_id: str, request: str, intent: dict, question: dict, row: dict, entry: dict,
         n: int) -> dict | None:
    """One kept source for this ask (the ``n``-th tried): read it, replay its draft through the researcher's tools,
    land it or None. The read is one the user did not tap for on this card, so it is audited like the auto-pick's."""
    url = row["access"]["url_template"]
    host = str(row["provider"].get("name") or "")[:120]
    ctx = ResearchCtx(request=request, question=question, now=ni._clock().astimezone(UTC),
                      window=ni_research_flow._window_of(request, intent), zone=ZoneInfo(ni.user_timezone_name()),
                      allowed_origins=frozenset({"kept"}), library=None, searcher=None)
    fmt = (entry["draft"].get("source") or {}).get("format")
    ctx.record(url, "kept", title=entry["title"], **({"format": fmt} if isinstance(fmt, str) else {}))
    ni_flow._audit_auto_pick(item_id, host, {"n": n}, tool="ni_kept_pick")
    got = tools.fetch_sample(ctx, url)
    _count(store, row["kept"]["key"], entry, missed=not got["ok"] and got.get("error") in _FETCH_MISSES)
    if not got["ok"]:
        return None
    proposed = tools.propose_card(ctx, {**copy.deepcopy(entry["draft"]), "goal": request})
    if not proposed.get("ok"):
        log.info("ni_intake: kept source did not fit: %s", one_line("; ".join(proposed.get("reasons") or []), 200))
        return None
    proposal = proposed["proposal"]
    if ni_research_flow._can_land(ctx, item_id, request, proposal, ni_research_flow._declined_of(
            ni_flow._flow_read(store, item_id) or {})) is not None:
        return None
    note = f"built from a source you kept before ({host}); held for your Keep"
    return ni_research_flow._land(store, item_id, request, ctx, proposal, loop.Outcome("proposed", "kept"), note=note,
                                  path="kept")


def _count(store: ni.NIStore, key: str, entry: dict, *, missed: bool) -> None:
    """A read of a kept address: a failed fetch counts against it, any answer resets it; at ``MAX_MISSES`` the
    entry is forgotten (a dead source stops costing every ask a fetch)."""
    assert isinstance(key, str), "a key"
    if not missed and not entry.get("misses"):
        return
    with _LOCK:
        rows = library_index.LocalSources(store).list()
        for r in rows:  # bounded: the user's own sources
            if not _row_ok(r) or r["kept"]["key"] != key:
                continue
            entries = _entries(r)
            for e in entries:  # bounded: MAX_ENTRIES
                if e["question"] == entry["question"]:
                    e["misses"] = int(e.get("misses") or 0) + 1 if missed else 0
            r["kept"]["entries"] = [e for e in entries if int(e.get("misses") or 0) < MAX_MISSES]
        _write(store, rows)
