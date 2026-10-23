"""v2 glue for compare asks (plan S7): build a compare card from a Library row whose address is filled per thing, or
decline honestly.

``try_compare`` runs first in the v2 auto-pick, for an ask that compares two to four named things
(``ni_compare.asked``). For each thing it asks the Library for the sources of THAT thing alone (the ask rewritten by
code to name just it), then looks for one source that serves every thing with one unambiguous address each and a
declared number answer for the metric the ask names. The addresses are fetched through the flow's own fetcher, the
number is read at each response's path, and the card is sealed by ``ni_compare.build``: one exact address per thing,
named by the user's words. Nothing leaves the device that a single Library card does not send: same host, one
request per thing, held for the user's Keep.

When no Library source builds it, the researcher gets the ask (it may propose a compare, ``propose_compare``); when
nothing builds, the flow ends in the declined card ("can't compare these yet" and why), never a one-thing card.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC

from . import ni, ni_compare, ni_flow, ni_query
from .ni_verify.constraints import parse_window

log = logging.getLogger(__name__)

# the readable response formats a number can be read from
_FORMATS = frozenset({"json", "csv", "feed", "xml"})
# the answer window each asked window may be served by (``None``: the ask names none)
_WINDOWS: dict[str | None, frozenset] = {
    None: frozenset({None, "now", "latest"}), "today": frozenset({None, "now", "latest", "today"}),
    "tonight": frozenset({"tonight"}), "tomorrow": frozenset({"tomorrow"})}
_MAX_REASONS = 3


def decline(store: ni.NIStore, item_id: str, why: str) -> dict:
    """The honest end of a compare ask nothing could build: the declined card ("can't compare these yet" and why)."""
    assert store is not None and item_id, "args required"
    assert isinstance(why, str), "a reason"
    text = why if why.startswith("can't compare") else f"can't compare these yet: {why}"
    return ni_flow._end(store, item_id, "compare_unbuildable", text)


def _usable(row: dict) -> bool:
    """A Library row a compare can read: declared answers, no key or contact email, no helper lookup, no clock
    parameter (each address must be exact), a readable format."""
    assert isinstance(row, dict), "row required"
    return (row.get("answers") is not False and not ni_flow._row_needs_access(row) and not row.get("lookup")
            and not row.get("clock_params") and row.get("format", "json") in _FORMATS)


def _entity_intent(intent: dict, ask: str) -> dict:
    """The intent for ONE thing's search: its own ask as the subject, no place carried over from the other thing."""
    assert isinstance(intent, dict) and ask, "args required"
    return {**intent, "subject": ask, "place": None}


def _words(text: str) -> set[str]:
    return {ni_query.clauses.fold(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def metric_for(request: str, entities: tuple[str, ...], answers: list[dict]) -> tuple[dict | None, str]:
    """``(answer, why-not)``: the one number answer a source declares that the ask is for. Only a plain number read now
    (or in the asked window); a measure the ask names that the source does not report is its nothing, never another
    quantity under the asked one's name. With no measure named, the answer sharing most words with the ask wins, else
    the source's first primary one."""
    assert isinstance(request, str) and isinstance(answers, list), "args required"
    window = parse_window(request)
    ok = _WINDOWS.get(window)
    if ok is None:
        return None, f"a compare reads the current value, not {window.replace('_', ' ') if window else 'that time'}"
    plain = [a for a in answers if a["kind"] == "value" and a["type"] == "number" and not a.get("codes")
             and a.get("window") in ok]
    if not plain:
        return None, "it declares no number to compare"
    said = frozenset(w for e in entities for w in _words(e))
    named, unreported = ni_flow._unbound_measures(request, answers, said)
    if named:
        for a in plain:  # bounded: a source's few answers
            if any(ni_flow._reports_measure(a, m) for m in named):
                return a, ""
        return None, "it doesn't report " + " or ".join(m.replace("_", " ") for m in unreported or named)
    mine = _words(request) - said
    best = max(plain, key=lambda a: (len(mine & _words(" ".join([a["label"], a["name"], *a["words"]]))), a["primary"]))
    return best, ""


def _read(row: dict, answer: dict, sample: object) -> tuple[float | int | None, str | None, str]:
    """``(number, unit, path)`` of one thing: the answer filled with this row's parameters, read from its sample."""
    assert isinstance(row, dict) and isinstance(answer, dict), "args required"
    filled = ni_flow._fill_answer(answer, row.get("params") or {})
    payload = sample if isinstance(sample, dict) else {"items": sample}
    if not ni_flow._answer_present(filled, payload):
        return None, None, filled["path"]
    unit = ni_flow._answer_unit(filled, payload) or None
    return ni_compare.number_at(payload, filled["path"]), unit, filled["path"]


def _prepare(request, intent, names_, picks, do_fetch, now) -> tuple[dict | None, str]:
    """Everything that can refuse one source, before anything is written: its metric, a reading per thing (each
    distinct address fetched once), and the sealed spec. ``({spec, built, answer, note}, "")`` or ``(None, why)``."""
    assert len(picks) == len(names_), "a row per thing"
    assert callable(do_fetch), "a fetcher"
    answer, why = metric_for(request, names_, ni_flow._library_answers(picks[0]["source_id"]))
    if answer is None:
        return None, why
    entities: list[dict] = []
    values: list = []
    units: set = set()
    samples: dict[str, object] = {}   # an address that serves several things (the thing in the PATH) is read once
    for name, row in zip(names_, picks, strict=True):  # bounded: at most MAX_ENTITIES
        try:
            if row["url"] not in samples:
                samples[row["url"]] = do_fetch(row["url"])
            number, unit, path = _read(row, answer, samples[row["url"]])
        except Exception as exc:  # a thing that can't be read stops this source; the next one may serve it
            return None, f"{name} couldn't be read ({type(exc).__name__})"
        if number is None:
            return None, f"it has no {answer['label'].lower()} for {name}"
        if any((e["url"], e["path"]) == (row["url"], path) for e in entities):
            return None, f"one address and path serves more than one of them, so they can't be told apart ({name})"
        entities.append({"name": name, "url": row["url"], "path": path})
        values.append(number)
        units.add(unit)
    if len(units) != 1:
        return None, "the readings are in different units"
    cadence = int(intent.get("cadence_minutes") or ni_flow._DEFAULT_CADENCE)
    title = ni_compare.title_of(list(names_), answer["label"])
    built = ni_compare.build(entities, values, label=answer["label"], unit=next(iter(units)), title=title, ask=request,
                             now=now, cadence_s=cadence * 60, viewer_tz=ni.user_timezone_name())
    if built is None:
        return None, "the readings can't be laid out side by side"
    fmt = picks[0].get("format", "json")
    source = {"type": "http_json", "url": entities[0]["url"], **({"format": fmt} if fmt != "json" else {}),
              "compare": {"entities": entities}}
    spec = ni_flow.build_final_spec(request, intent, source, cadence, built["pipeline"], built["scene"])
    spec["title"] = title
    ni.validate_spec(spec)
    note = (f"built from the Library: {answer['label']} for {', '.join(names_)}, one reading each on "
            f"{picks[0].get('host') or 'the source'}")
    return {"spec": spec, "built": built, "note": note}, ""


def _try_source(store, item_id, request, intent, names_, picks, do_fetch, now) -> tuple[dict | None, str]:
    """Build the card from one source's per-thing rows (``picks``), or ``(None, why)``. The error boundary is one
    source's attempt: whatever refuses or breaks it (the metric, a long token in the ask, the seal) ends THIS source
    as a reason, logged, and the next source runs. The landing below it is outside: a write failure is not a source's
    nothing."""
    ready = None
    try:
        ready, why = _prepare(request, intent, names_, picks, do_fetch, now)
    except (ValueError, TypeError, KeyError, AssertionError, ni.NIError) as exc:
        log.warning("compare: source %s declined by %s: %s", picks[0].get("source_id"), type(exc).__name__, exc)
        return None, f"it can't be sealed as a comparison ({type(exc).__name__})"
    if ready is None:
        return None, why
    spec, built, note = ready["spec"], ready["built"], ready["note"]
    url = spec["source"]["url"]
    ni_flow._transition(store, item_id, "assembling", source_url=url, note=note)
    ni_flow._try_journal(store, item_id, "updated", note)
    landed = ni_flow._finalize(store, item_id, spec, built["preview_payload"], note=note, born="flow", fetched_at=now,
                               built_from=ni_flow._built_from_of(store, item_id, "auto", url,
                                                                  str(picks[0].get("title") or "")))
    return landed, ""


def _picks(per: list[list[dict]]) -> list[tuple[str, list[dict]]]:
    """The sources every thing has exactly one usable row of, in the first thing's order: ``[(source_id, [row per
    thing])]``. A thing with several rows of one source is ambiguous there (several readings of its name)."""
    assert per, "rows per thing required"
    order = [r["source_id"] for r in per[0]]
    out: list[tuple[str, list[dict]]] = []
    for sid in dict.fromkeys(order):  # bounded: the first thing's few rows
        rows = [[r for r in rs if r["source_id"] == sid] for rs in per]
        if all(len(x) == 1 for x in rows):
            out.append((sid, [x[0] for x in rows]))
    return out


def library_build(store: ni.NIStore, item_id: str, request: str, intent: dict, names_: tuple[str, ...],
                  do_fetch: Callable[[str], object]) -> tuple[dict | None, list[str]]:
    """Try the Library: ``(the landed card record, [])`` or ``(None, why-not reasons)``."""
    assert len(names_) >= ni_compare.MIN_ENTITIES, "things to compare"
    assert store is not None and item_id, "args required"
    per: list[list[dict]] = []
    why: list[str] = []
    for i, name in enumerate(names_):  # bounded: at most MAX_ENTITIES
        ask = ni_compare.entity_ask(request, names_, i)
        rows = ni_flow._without_declined(store, item_id, ni_flow._library_candidates(
            ask, _entity_intent(intent, ask), limit=ni_flow._AUTO_ROWS))
        per.append([r for r in rows if _usable(r)])
        if not per[-1]:
            why.append(f"the Library has no source it can read for {name}")
    if not all(per):
        return None, why[:_MAX_REASONS]
    now = ni._clock().astimezone(UTC)
    for sid, picks in _picks(per):  # bounded: a few sources
        landed, reason = _try_source(store, item_id, request, intent, names_, picks, do_fetch, now)
        if landed is not None:
            return landed, []
        why.append(f"{picks[0].get('title') or sid}: {reason}")
    return None, (why or ["no one Library source serves every one of them"])[:_MAX_REASONS]


def try_compare(store: ni.NIStore, item_id: str, request: str, intent: dict,
                do_fetch: Callable[[str], object]) -> dict | None:
    """The v2 answer to a compare ask: the landed card, the researcher's, or the honest decline. None when the ask
    does not compare named things (the caller then runs the ordinary auto-pick)."""
    assert store is not None and item_id, "args required"
    assert isinstance(intent, dict) and request, "intent and request required"
    names_ = ni_compare.asked(request)
    if not names_:
        return None
    if len(names_) > ni_compare.MAX_ENTITIES:
        return decline(store, item_id, f"that is {len(names_)} things and a card compares up to {ni_compare.MAX_ENTITIES}")
    landed, why = library_build(store, item_id, request, intent, names_, do_fetch)
    if landed is not None:
        return landed
    from . import (
        ni_research_flow,  # local: the researcher imports this module's siblings
    )
    found = ni_research_flow.fallback(store, item_id, request, intent)
    if found is not None:
        return found
    return decline(store, item_id, "; ".join(why) or "no source reads them side by side")
