"""Source- and build-level verify of a declared-answers build (moved verbatim from ni_flow)."""

from __future__ import annotations

from datetime import datetime

from .. import ni
from .places import (
    _national_us_misses_place,
    _place_taken,
    _resolver_landed_outside_state,
    _scoping_names_place,
)
from .subject import (
    _about_named,
    _other_subject,
    _speaks_to,
    _stray_topics,
)
from .text import (
    _ANSWER_MEASURES,
    _COMPONENT_WORDS,
    _GENERIC_SUBJECT,
    _MAX_INTENT_FIELDS,
    _MEASURE_CUES,
    _SCORE_WORDS,
    _WINDOW_WORDS,
    _answer_score,
    _answer_tokens,
    _covers_measure,
    _event_time,
    _subcategory,
    _v12,
    _window_words,
)


def _times_in(value: object, out: list) -> None:
    """Every time-transform output (it keeps its moment) inside a built value or row (bounded walk)."""
    if getattr(value, "moment", None) is not None:
        out.append(value)
    elif isinstance(value, dict):
        for v in list(value.values())[:40]:
            _times_in(v, out)
    elif isinstance(value, list):
        for v in value[:60]:
            _times_in(v, out)


def _dated_rows(a: dict) -> bool:
    """Do a list's rows carry their date: a time / date cell, or the month and the day as numbers (the
    USNO's moon phases and seasons: "Full Moon · 10 · 6")?"""
    cells = a.get("cells") or []
    if any(c["type"] in ("time", "date") for c in cells):
        return True
    said = [_answer_tokens(f"{c.get('label') or ''} {c['path']}") for c in cells if c["type"] == "number"]
    return any("month" in t for t in said) and any("day" in t for t in said)


def _frame_gap(kind: str | None, chosen: list[dict], preview: dict, now: datetime,
                window: str | None = None) -> str | None:
    """What the chosen answers can't be for this kind of question: a next event needs a time still to
    come; a result needs a score."""
    if kind in ("next_event", "schedule"):
        # an observation's own time ("as of", window now) is not an event still to come
        typed = [a for a in chosen if _event_time(a) or _dated_rows(a)]
        if not typed:
            return "it gives no time for the next event"
        moments: list = []
        _times_in(preview, moments)
        # F5 (2026-10-04): judge the FIRST SHOWN moment — a "next" card whose first row is a past
        # event is wrong even when later rows are future. The forward cut in _build_rows_answer
        # removes past rows every refresh; this is the belt-and-braces for a build without one.
        # R4-6 (2026-10-04): an explicit day/night window on a SCHEDULE ("MLB schedule today")
        # asks for the WHOLE period including past-but-today rows; the stale-first check skips.
        # R5-1 (2026-10-04): a NEXT-EVENT ask with the same window ("when is the next tide today")
        # still wants only upcoming — the stale check must fire so an 8:01 AM low at 22:30 never
        # leads the card.
        in_period = kind == "schedule" and window in ("today", "tonight")
        if moments and not in_period and ni.next_event_stale(moments[0], now):
            return "its next time has already passed"
    if kind == "result":
        words: set[str] = set()
        for a in chosen:  # bounded by _MAX_VALUE_ANSWERS
            for text in [*a["words"], a["label"], a["name"].replace("_", " "),
                         *[c.get("label", "") for c in a.get("cells") or []]]:
                words |= _answer_tokens(text)
        if not words & _SCORE_WORDS:
            return "it gives no score or result"
    return None


def _legacy_window_gap(window: str | None, chosen: list[dict], answers: list[dict]) -> str | None:
    """An older record's answers can't say their window, but a label can: "High today" is never the
    answer to "on Saturday"."""
    if not window or _v12(answers):
        return None
    for a in chosen:  # bounded by _MAX_VALUE_ANSWERS
        said = [w for w in ("today", "tonight", "tomorrow") if w in a["label"].lower()]
        if a["kind"] == "value" and said and said[0] != window:  # a day-labeled value holds that day only
            return f"it shows {a['label']}, not {_window_words(window)}"
    return None


def _lacks(expects: list[str], answers: list[dict]) -> list[str]:
    """The components a complete answer of this kind holds (taxonomy ``expects``) that none of the
    source's answers reports ("surf" → wind on a wave-only source)."""
    out = []
    for comp in expects[:10]:  # bounded by the closed component list
        if comp in _MEASURE_CUES or comp in _ANSWER_MEASURES:
            hit = any(a.get("measure") == comp or (comp in _MEASURE_CUES and _covers_measure(a, comp))
                      for a in answers)
        elif comp in ("observed_time", "start_time"):
            hit = any(a.get("type") == "time" or any(c["type"] == "time" for c in a.get("cells") or [])
                      for a in answers)
        else:
            words = _COMPONENT_WORDS.get(comp, frozenset(_answer_tokens(comp.replace("_", " "))))
            hit = any(words & _answer_tokens(" ".join([*a["words"], a["label"], a["name"].replace("_", " "),
                                                         *[c.get("label", "") for c in a.get("cells") or []]]))
                      for a in answers)
        if not hit:
            out.append(comp.replace("_", " "))
    return out


def _verify_source(frame: dict, source: dict, params: dict, answers: list[dict], request: str,
                   intent: dict) -> tuple[list[str], list[str]]:
    """Source-level verify (R4, 2026-10-04): the checks that need the SOURCE and the ASK but no
    build output. Returns (reasons, notes). Runs before the declared-answers build AND before the
    model mapping path, so a wrong-source pick never ships a confidently wrong model-mapped card.

    Refused when: (a) the source is another kind of data than the ask's category; (a2) a different
    named subject; (b) the ask names a place the address never took; (f) a stray topic word."""
    reasons: list[str] = []
    notes: list[str] = []
    cats = [str(c) for c in source.get("categories") or []]
    asked = frame["categories"]
    about_named = _about_named(frame, source, params)
    tops = {c.split("/")[0] for c in cats}
    said, meant = frame.get("ask_categories") or [], frame.get("about_categories") or []
    asked_tops = {c.split("/")[0] for c in said + meant}
    if said and meant and cats and not about_named and not asked_tops & tops \
            and not _speaks_to(source, request, intent, params, asked_tops):
        reasons.append(f"it is {cats[0].split('/')[0]} data, not {asked[0].split('/')[0]}")
    other = _other_subject(frame, source, params, answers, request, intent, about_named)
    if other:
        reasons.append(other)
    sub = next((c for c in asked if c in cats), None) \
        or next((c for c in asked if c.split("/")[0] in {x.split("/")[0] for x in cats}), None) \
        or (asked[0] if asked else "")
    info = _subcategory(frame["lib"], sub) if frame["lib"] is not None and sub else {}
    place = frame["place"]
    if place and not _place_taken(place, source, params):
        # fix12-lib (2026-10-05): a NAMED US-state (or city the resolver lands in a US state) on a
        # national-US series ("Unemployment rate (FRED)", coverage.geo="US") is a place the address
        # never took — the national cut shown as the asked place's reading is a confidently wrong
        # card. Refuse even when the subcategory's policy says the topic is place-free, so the
        # next source gets a shot. A truly place-free subject (aurora Kp over a region) still rides
        # through as a note because its source's coverage isn't US-national.
        policy_match = (info.get("policy") or {}).get("match")
        if policy_match in ("none", "name") and not _national_us_misses_place(source, place, request):
            notes.append(f"{source.get('provider') or 'this source'} isn't specific to {place}")
        else:
            reasons.append(f"it isn't for {place}")
    elif place and _resolver_landed_outside_state(source, place, request):
        # fix12-lib (2026-10-05): a US-state ask whose ``place`` resolver landed on a same-named
        # city of A DIFFERENT state ("earthquakes in california" → California, PA) is the wrong
        # read; the resolver's own ``_label`` suffixes "(STATE_CODE)" so a mismatch tells us to
        # refuse. The state-bounded sibling source ("USGS earthquakes in a state", us_state
        # resolver) gets a shot; "earthquakes near Pinnacles CA" keeps the near-a-place source
        # because its label still says "(CA)".
        reasons.append(f"it isn't for {place}")
    stray = sorted(_stray_topics(frame, source, params, answers, request, intent))
    if stray and not about_named:
        reasons.append("it isn't about " + ", ".join(stray))
    return reasons, notes


# a text product (the written forecast discussion) is answered only by a genuine ``text``-typed
# cell — a short category word ("Mostly Clear") is a label, never a written product (G3, set G
# #50). Only "discussion" names such a product: a summary / outlook / brief IS served by the
# periods' short forecasts (sets F #49 and G #49, live and partial), and an "advisory" is an
# alert product served by the alert lists (a one-row alert list binds as a measure and would be
# refused here).
_TEXT_PRODUCT_WANTS = frozenset({"discussion"})


def _text_product_gap(built: dict, intent: dict) -> str | None:
    """None, or the text-product word every want the user said names, when the one-row build
    holds no ``measure`` field of type ``text`` for it — the honest answer is no card, not a
    numeric stat wearing the asked word (``_verify_frame``)."""
    assert isinstance(built, dict) and isinstance(intent, dict), "built + intent required"
    wants = [str(w) for w in intent.get("wants") or []][:_MAX_INTENT_FIELDS]
    if not wants:
        return None
    named = [next(iter(_answer_tokens(w) & _TEXT_PRODUCT_WANTS), None) for w in wants]
    if not all(named):
        return None
    record = built["scene"].get("record") or {}
    assert isinstance(record, dict), "the sealed scene carries a record dict"
    if record.get("kind") != "measure":
        return None
    if any(f.get("role") == "measure" and f.get("type") == "text" for f in record.get("fields") or []):
        return None
    return named[0]


def _verify_frame(frame: dict, source: dict, params: dict, built: dict, request: str, intent: dict,
                  now: datetime) -> tuple[list[str], list[str]]:
    """Does this declared-answers build answer the ask's frame? Returns (reasons it doesn't — any one
    refuses the build —, honest notes for a card that does). Deterministic; no model.

    Source-level checks (``_verify_source``: a / a2 / b / f) run first; build-level checks follow:
    (c) chosen answers can't be this kind of question (a next event without a time still to come,
    a result without a score); (d) an older record's answer is labeled for another day than the
    asked window; (e) nothing chosen speaks to the words the user asked about, and some asked want
    is unanswered — refused also when every want the user said is unanswered."""
    chosen, answers = built["chosen"], built["answers"]
    reasons, notes = _verify_source(frame, source, params, answers, request, intent)
    # fix7-lib (2026-10-04): a chosen answer with a row filter that names the ask's named value
    # (CBP "San Ysidro", NHL "Metropolitan") scopes the data to it; the "isn't specific to" note
    # the source-level place check would ship from the subcategory policy is no longer honest,
    # and a subdivision ask whose rows the source doesn't carry has already refused upstream.
    if frame["place"] and _scoping_names_place(chosen, frame["place"]):
        tag_note = f"isn't specific to {frame['place']}"
        tag_reason = f"it isn't for {frame['place']}"
        notes = [n for n in notes if tag_note not in n]
        reasons = [r for r in reasons if r != tag_reason]
    asked = frame["categories"]
    cats = [str(c) for c in source.get("categories") or []]
    sub = next((c for c in asked if c in cats), None) \
        or next((c for c in asked if c.split("/")[0] in {x.split("/")[0] for x in cats}), None) \
        or (asked[0] if asked else "")
    info = _subcategory(frame["lib"], sub) if frame["lib"] is not None and sub else {}
    gap = _frame_gap(frame["kind"], chosen, built["preview_payload"], now,
                      window=frame.get("window"))         or _legacy_window_gap(frame["window"], chosen, answers)
    if gap:
        reasons.append(gap)
    text_gap = _text_product_gap(built, intent)
    if text_gap:
        reasons.append(f"no source reports a forecast {text_gap} yet")
    about: set[str] = set()
    for text in [str(intent.get("subject") or ""), *(str(w) for w in intent.get("wants") or [])]:
        about |= _answer_tokens(text.replace("_", " "))
    distinct = (_answer_tokens(request) & about) - _WINDOW_WORDS - _GENERIC_SUBJECT \
        - _answer_tokens(str((source.get("coverage") or {}).get("entity") or ""))
    for value in params.values():  # bounded by the params
        distinct -= _answer_tokens(value)
    # F12 (2026-10-04): refuse when EVERY want the user said is unanswered — the existing distinct
    # check passed when one shared subject word scored even with all quantities missing ("gas
    # inventories this week" scored on "gas" alone and shipped a retail price source).
    # ISS "coordinates" stays accepted because _WANT_SYNONYMS covers it via lat / lon / position.
    said_wants = _said_wants(request, intent.get("wants") or [], list(params.values()))
    all_unanswered = bool(said_wants) and len(built["unanswered"]) >= len(said_wants)
    if built["unanswered"] and (all_unanswered or (
            distinct and not any(_answer_score(a, distinct) > 0 for a in chosen))):
        reasons.append("it doesn't report " + ", ".join(built["unanswered"]))
    if sub in cats:  # a complete answer of the kind it is filed for: name what the source can't report
        lacks = [x for x in _lacks(list(info.get("expects") or []), answers) if x not in built["unanswered"]]
        built["unanswered"] = list(built["unanswered"]) + lacks
    return reasons, notes


def _said_wants(request: str, wants: list, filled: list[str]) -> list[str]:
    """The wants the user actually said in the request (a want whose tokens overlap the request
    minus filled values). Used by _verify_frame's F12 branch."""
    ask = _answer_tokens(request or "")
    for value in filled:  # bounded by params + place
        ask -= _answer_tokens(value)
    out: list[str] = []
    for want in (wants or [])[:_MAX_INTENT_FIELDS]:
        if _answer_tokens(str(want).replace("_", " ")) & ask:
            out.append(str(want))
    return out
