"""Subject and stray-topic checks of the verify step (moved verbatim from ni_flow)."""

from __future__ import annotations

import re

from .text import (
    _DAY_NAME_RE,
    _GENERIC_SUBJECT,
    _GEO_PARAMS,
    _KNOWN_OUTLET_WORDS,
    _MAX_INTENT_NAMES,
    _NAME_STOP,
    _OUTLET_SUFFIX_WORDS,
    _WINDOW_WORDS,
    _amp,
    _answer_tokens,
    _entity_vocabulary,
    _folded_words,
    _kind_tokens,
    _names_hit,
    _own_words,
    _proper_tokens,
    _request_proper,
    _single_keywords,
    _subcategory,
    _subject_names,
)


def _stray_topics(frame: dict, source: dict, params: dict, answers: list[dict], request: str, intent: dict) -> set[str]:
    """The subject's topic words (taxonomy keywords the user said) that belong only to subcategories this
    source isn't filed under and that nothing of the source mentions. A word the Library doesn't know as
    a topic (a place, a nickname) is never stray."""
    lib = frame.get("lib")
    try:
        taxonomy = lib.taxonomy() if lib is not None else []
    except Exception:  # an older or broken Library: no topic facts
        return set()
    filed = {str(c) for c in source.get("categories") or []}
    words = f" {' '.join(_folded_words(request))} "
    # where a word belongs: every subcategory whose name or keywords use it; whether it is a topic at all: the
    # Library uses it as one on its own — a subcategory's name ("TV shows", "Apps & software"), a
    # one-word keyword, or a phrase the user said whole ("red" alone isn't "red flag warning")
    topics: dict[str, set[str]] = {}
    eligible: set[str] = set()
    for cat in taxonomy:  # bounded by the taxonomy
        for sub in cat.get("subcategories") or []:
            sid = f"{cat['id']}/{sub['id']}"
            kws = list(sub.get("keywords") or [])
            for tok in _answer_tokens(" ".join([str(cat.get("id") or ""), str(sub.get("label") or ""), *kws])):
                topics.setdefault(tok, set()).add(sid)
            eligible |= _answer_tokens(" ".join([str(sub.get("label") or ""), *(
                kw for kw in kws if len(kw.split()) == 1 or f" {' '.join(_folded_words(kw))} " in words)]))
    said = (_answer_tokens(str(intent.get("subject") or "")) & _answer_tokens(request)) \
        - _GENERIC_SUBJECT - _WINDOW_WORDS - _answer_tokens(str(intent.get("place") or ""))
    own: set[str] = _answer_tokens(" ".join([str(source.get("name") or ""), str(source.get("description") or ""),
                                             str((source.get("coverage") or {}).get("entity") or ""),
                                             *(str(x) for x in source.get("examples") or [])]))
    for a in answers:  # bounded by _MAX_ANSWERS
        own |= _answer_tokens(" ".join([*a["words"], a["label"], a["name"].replace("_", " "),
                                        *[c.get("label", "") for c in a.get("cells") or []]]))
    stray = {t for t in said - own if t in eligible and t in topics and not topics[t] & filed}
    # F7 (2026-10-04): a proper-noun subject word the user said that no part of this source takes
    # (own words, readings, params, filter) is stray even when the Library's taxonomy doesn't carry
    # it ("Ukraine" on a general US headline feed). Scoped to sources that (a) have no single-name
    # entity (a league / provider "MLB", "CTA" already covers unlisted team / nickname words — the
    # ask's teams are its subject area), and (b) take no geo parameter — a geo-filled station is
    # place-specific and the water-body of its area isn't stray. A LIST entity (local-news-metro's
    # semicolon-separated metros) still runs: the exemption only covers names the entity actually
    # takes (``own`` already holds them); a brand it doesn't take ("Tribune", "Post") is stray.
    # F7-B class fix (2026-10-04): the request's RAW casing — never the model's Title-Case subject
    # (phones auto-capitalize sentence-initial, the model Title-Cases its subject). Named-topics
    # (2026-10-04): the sentence-initial cap is a name ONLY when the intent's validated ``names``
    # include it — the local model fills that narrow closed blank, code validates every entry is a
    # whole-word substring of the ask, and the proper-noun check consumes the result (never guesses
    # by capitalization alone). Mid-sentence caps and all-caps acronyms stay proper.
    entity = str((source.get("coverage") or {}).get("entity") or "").strip()
    if not (set(params) & _GEO_PARAMS) and (not entity or ";" in entity
                                             or _entity_vocabulary(lib, entity)):
        # fix6-rows F7-C (2026-10-04): a single-entity league source still refuses a proper token
        # its entity's domain doesn't take ("NFC East" on MLB standings) — the exemption subtracts
        # team / league aliases of that league (Yankees, AL East) rather than skipping the whole
        # check. For non-sports entities the exemption stays as it was (readings / own cover them).
        proper = _request_proper(request, intent.get("names") or ())
        proper -= _answer_tokens(" ".join(str(r) for r in (source.get("readings") or [])))
        proper -= _answer_tokens(str(intent.get("place") or ""))
        for value in params.values():  # bounded by the params
            proper -= _answer_tokens(value)
        proper -= _entity_vocabulary(lib, entity)
        stray |= (proper & said) - own
    # fix7-lib (2026-10-04): a Library provider named in the ask that isn't this source's
    # provider is stray even when the intent's ``names`` blank missed it (a lowercase outlet
    # the model didn't flag). Reads the pack's own provider vocabulary — no network, no model.
    # Tokens shared with this source's own words (NPR Business holds "news") never land in
    # stray; same for a param value or the ask's named place.
    stray |= _foreign_providers_in(lib, request, source, own, params, intent)
    return stray


def _foreign_providers_in(lib: object, request: str, source: dict, own: set[str],
                            params: dict, intent: dict) -> set[str]:
    """The folded tokens of every outlet name the ask carries that doesn't match this source's
    own provider, less tokens the source already covers (``own``), the filled params, and the
    ask's named place. The outlet names come from (a) the pack's ``providers_named_in``, (b)
    intent.names whose words end in a publisher suffix ("Weather Channel"), and (c) a bounded
    well-known-outlet set the pack hasn't indexed yet ("Axios Denver"). {} when nothing matches
    or every named outlet IS this source's. Bounded by the pack's providers + intent.names cap.

    fix7-lib (2026-10-04): "reuters business news" shipped from NPR Business because the intent
    model missed the lowercase outlet; this backstop reads the Library vocabulary so an ask that
    names any pack-known outlet the pick isn't from refuses honestly.
    fix8 (blind-7, 2026-10-04): "Weather Channel 10 day for Asheville" shipped NWS; the pack
    doesn't index Weather Channel. The outlet-suffix set and well-known-outlet set together
    catch outlet names the pack hasn't indexed yet."""
    assert isinstance(source, dict) and isinstance(params, dict), "source + params required"
    getter = getattr(lib, "providers_named_in", None)
    named: set[str] = set()
    if callable(getter):
        try:
            got = getter(request)
        except Exception:  # a broken Library: fall through to intent.names
            got = None
        if got:
            named = {str(p) for p in got}
    for name in (intent.get("names") or [])[:_MAX_INTENT_NAMES]:
        if not isinstance(name, str) or not name:
            continue
        words = {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 2}
        if words & (_OUTLET_SUFFIX_WORDS | _KNOWN_OUTLET_WORDS):
            named.add(name)
    if not named:
        return set()
    own_pname = str(source.get("provider") or "").strip().lower()
    taken = _answer_tokens(str(intent.get("place") or ""))
    for value in params.values():  # bounded by the params
        taken |= _answer_tokens(value)
    out: set[str] = set()
    for provider in named:  # bounded by the pack's providers (few match any one ask)
        if provider.strip().lower() == own_pname:
            continue
        out |= _answer_tokens(_amp(str(provider))) - _NAME_STOP - own - taken
    return out


def _speaks_to(source: dict, request: str, intent: dict, params: dict, asked_tops: set[str]) -> bool:
    """Is the source known by every naming word of the ask's subject — its name, its example asks, the values
    its address took ("oil stocks report" is an example ask of the EIA's petroleum stocks)? Not by its
    description: the Kp forecast's "geomagnetic storm" doesn't make it the answer to "will it storm tonight".
    Never when the ask names nothing, and never when the intent names the asked kind itself ("weather":
    "KC storms tonight" is no misfiled hurricane ask)."""
    about = " ".join([str(intent.get("subject") or ""), *(str(w) for w in intent.get("wants") or [])])
    if asked_tops & _answer_tokens(about):
        return False
    said = (_answer_tokens(_amp(request)) & _answer_tokens(_amp(about))) - _NAME_STOP \
        - _answer_tokens(str(intent.get("place") or ""))
    known = _answer_tokens(_amp(" ".join([str(source.get("name") or ""), *(str(x) for x in source.get("examples") or []),
                                          *(str(v) for v in params.values())])))
    return bool(said) and said <= known


def _about_named(frame: dict, source: dict, params: dict) -> bool:
    """Was the address filled from a subject the user named, of a kind the ask is about? The filled
    parameter's resolver must be one the policy of a category the intent's subject + wants classify as
    resolves (the Library's ``subcategory()``); with no category facts the fill stands."""
    filled = {n: r for n, r in (source.get("entity_params") or {}).items() if n in params}
    if not filled:
        return False
    meant = frame.get("about_categories") or []
    if frame.get("lib") is None or not meant:
        return True
    takes: set[str] = set()
    for sub in meant:  # bounded: three categories
        takes |= {str(r) for r in ((_subcategory(frame["lib"], sub).get("policy") or {}).get("resolvers") or [])}
    return bool(set(filled.values()) & takes)


_SUBNATIONAL_RE = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")


def _own_readings(source: dict) -> list[str]:
    """The pick row's OWN reading (R8, 2026-10-04 — revised R9 live 2026-10-04): its row's
    label alone. A reading sealed from a SIBLING row of the pick names what THAT row's
    resolver took; a league-wide source without its own label (no team filter) does not own a
    sibling team row's "Boston Red Sox (mlb)". ``_other_subject``'s sibling-readings check
    runs separately so this source still has to carry those names or honestly refuse."""
    assert isinstance(source, dict), "source must be a dict"
    own_label = str(source.get("label") or "").strip()
    return [own_label] if own_label else []


def _other_subject(frame: dict, source: dict, params: dict, answers: list[dict], request: str, intent: dict,
                   about_named: bool) -> str | None:
    """D1: the source is about a named subject the ask doesn't name — Slack for "is zoom having problems",
    the CTA for "WMATA red line delays", the Dow for "the S&P", Android for "ollama". A source about one
    named thing serves the ask when the ask names it; else only when every naming word the user said is
    one of its own words (and it isn't bound to one region: "subway delays" isn't BART's). A source whose
    address the user's named subject filled (a team, a ticker) must be about THAT subject: its reading
    ("Miami Marlins") holds every naming word the subject said ("Inter Miami" → refused)."""
    assert isinstance(source, dict) and isinstance(params, dict), "source + params required"
    assert isinstance(request, str) and isinstance(intent, dict), "request + intent required"
    cov = source.get("coverage") or {}
    entity = " ".join(str(cov.get("entity") or "").split())
    subject = " ".join([str(intent.get("subject") or ""), *(str(w) for w in intent.get("wants") or [])])
    taken = _answer_tokens(str(intent.get("place") or ""))
    for value in params.values():  # bounded by the params
        taken |= _answer_tokens(value)
    own = _own_words(source, answers)
    # R9 (2026-10-04, yen→dollar + Red Sox): a reading sealed from a SIBLING row of the pick (an earlier
    # pick's resolver label like "Japanese yen · US dollar", "Boston Red Sox (mlb)") names a subject the
    # ask has resolved; this source must carry it (own words / params / own label) or refuse. A league-
    # wide source after a team-source re-pick never ships for the named team; a FRED euro series after a
    # Frankfurter yen re-pick never ships for yen. Its own row's reading is excluded (that is the row's
    # own filter — the entity_params branch below carries the own-label check).
    own_label_key = str(source.get("label") or "").split(" (")[0].strip().lower()
    for reading in (source.get("readings") or [])[:8]:  # bounded: _library_readings cap
        rkey = str(reading).split(" (")[0].strip()
        if not rkey or rkey.lower() == own_label_key:
            continue  # this source's own reading, not a sibling's
        rtokens = _answer_tokens(_amp(rkey)) - _NAME_STOP - taken
        if rtokens and not (rtokens <= own):
            return f"it isn't about {rkey[:60]}"
    if source.get("entity_params"):
        label = str(source.get("label") or "").split(" (")[0].strip()
        if not about_named or not label:
            return None
        # fix7-lib (2026-10-04): the stray-subject check reads the words the USER said — never
        # tokens that only live in the model's subject. The model title-cases a category phrasing
        # ("Currency Conversion" for "USD to MXN") and the title case alone pulled "conversion"
        # into named, refusing a right pick. The Rangers hockey (team-row) case still refuses the
        # Texas Rangers' row from a request "Rangers hockey score": "hockey" is the user's own
        # word and the Library's keywords for sports/results don't carry it.
        named = _answer_tokens(_amp(request)) - _NAME_STOP - taken
        initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", label)).lower()
        # R9 (2026-10-04, yen→dollar history): drop stray words that describe the source's KIND
        # (markets/fx keywords: "currency", "forex") — the model title-cases its subject
        # ("Currency Conversion") and ``_proper_tokens`` pulls those category words even when the user
        # never said them. A kind word is not a different-entity signal.
        kind = _kind_tokens(frame.get("lib"), [str(c) for c in source.get("categories") or []])
        stray = {t for t in named - own - kind
                 if t != initials and not _names_hit({t}, _answer_tokens(_amp(label)))}
        return f"it is about {label}, not {' '.join(sorted(stray))}" if stray else None
    if not entity:
        return None
    cats = [str(c) for c in source.get("categories") or []]
    kind = _kind_tokens(frame.get("lib"), cats)
    said = (_answer_tokens(_amp(request)) | _answer_tokens(_amp(subject))) - _NAME_STOP - taken
    # a name with a day word names something ("Saturday Night Live"), its words are no window or filler
    said |= {t for m in _DAY_NAME_RE.finditer(f"{request} {subject}".lower()) for t in _answer_tokens(m.group(0))}
    names = _subject_names(source, kind)
    # called by its proper name ("Slack", "CTA", not "crude oil"): a kind word its words lack ("degraded")
    # names nothing else
    by_name = _names_hit(said, _proper_tokens(f"{entity} {source.get('name') or ''}") - _NAME_STOP)
    if _names_hit(said, names) and not _other_of_kind(frame, source, said, own, names, cats, by_name):
        return None
    # R8 (2026-10-04, revised R9 live): only the source's OWN row's reading subtracts from the
    # user's naming words. Sibling-row readings (another pick's team / currency) are handled by
    # the sibling-readings refusal at the top of this function — a league-wide source after a
    # team re-pick never ships for the team even when its sibling "(mlb)" tag matches.
    for reading in _own_readings(source):  # bounded: the pick's rows
        said -= _answer_tokens(_amp(reading.split(" (")[0]))
    # fix6-rows F7-C (2026-10-04): a league-wide source lists every team of its league in its
    # rows ("Yankees standings" → the AL East table includes the Yankees' row). The entity's
    # vocabulary (team_mlb / sports_league aliases) stands for that coverage so a team the ask
    # names doesn't look like a stray subject. The sibling-readings refusal above still fires
    # when ANOTHER row of the pick was for the team (a team-source re-pick); this subtraction
    # only loosens the final name check for the league-wide pick itself.
    said -= _entity_vocabulary(frame.get("lib"), entity)
    short = entity.split(",")[0] if len(entity.split(",")[0]) <= 40 else str(source.get("name") or "").split(" (")[0]
    other = _other_of_kind(frame, source, said, own, names, cats, by_name)
    if other:
        return f"it is about {short[:60]}, not {' '.join(other)}"
    if _names_hit(said, names) or (not (said - own) and not _SUBNATIONAL_RE.match(str(cov.get("geo") or ""))):
        return None
    return f"it is about {short[:60]}"


def _other_of_kind(frame: dict, source: dict, said: set[str], own: set[str], names: set[str],
                   cats: list[str], by_name: bool) -> list[str]:
    """Another one of the source's kind the user named: another number ("2 year yield" on the 10-year), or
    — when the ask doesn't call the source by its own name — a one-word keyword of its subcategory that none
    of its own words say ("wmata" on the CTA's alerts), never one its subcategory is named for ("inflation"
    on the CPI)."""
    numbers = {t for t in names if t.isdigit()}
    other = [] if by_name else sorted(t for t in said - own if t in _single_keywords(frame.get("lib"), cats))
    return other + sorted(t for t in said if t.isdigit() and numbers and t not in numbers)
