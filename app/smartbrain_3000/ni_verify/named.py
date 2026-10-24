"""Named: do the names an ask carries show up in how a source is known? Used by Library intake (``ni_intake``) to
decide whether a source the user kept for one ask also answers a new one: "AP top story" is not answered by a source
kept for "BBC headlines", however well its draft binds.

The ask's names are its proper words (``_request_proper``: a mid-sentence capital, an all-caps acronym, a
sentence-initial word the intent named), grouped into phrases as written, less the words another check owns (the
place, the things compared, a window or a number). Each phrase must show up in what the source served or in how it is
known (its address, its title, the words it was kept for): one of its words, or the phrase as initials ("HN" for
Hacker News), or an acronym spelt out ("National Weather Service" for NWS). Pure: nothing here fetches or asks a
model. It is not a research verifier: as one it refused far more right cards than wrong ones (census ni-named1,
tuning set, 2026-10-24), while for reuse a miss only means the ask is built another way.
"""

from __future__ import annotations

import re

from .text import _NAME_STOP, _amp, _answer_tokens, _folded_words, _request_proper

_MAX_SERVED_CHARS = 200_000   # what the source served is read this far (a bounded, linear scan)
_MAX_INITIALS = 6             # an acronym spells out at most this many words
_MIN_PREFIX = 3               # a source word that begins with a name of this length carries it ("nhl" in "nhle")
_MAX_PHRASES = 8
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_INITIAL_STOP = frozenset({"of", "and", "the", "for", "on", "in", "at", "to", "a", "an", "s"})


def phrases(request: str, intent_names: list[str] | tuple = (), owned: list[str] | tuple = ()
            ) -> list[tuple[str, ...]]:
    """The ask's names as phrases of folded words, in the order written: runs of consecutive proper words, less
    numbers and the words of what another class owns (``owned``: the place, the things compared), and less a word
    that spells an owned name's initials ("US" for "United States"). A run of generic words alone ("News") is no
    name; inside a name it stays, so "Hacker News" still spells "hn"."""
    assert isinstance(request, str), "request must be a string"
    assert isinstance(owned, (list, tuple)), "owned must be a list"
    proper = _request_proper(request, list(intent_names)[:8])
    taken: set[str] = set()
    for text in owned[:8]:  # bounded: a place and a few compared things
        seq = _words(str(text), 200)
        taken |= set(seq) | _answer_tokens(str(text)) | ({"".join(w[0] for w in seq)} if len(seq) >= 2 else set())
    out: list[tuple[str, ...]] = []
    run: list[str] = []
    for word in [*_WORD_RE.findall(_amp(request))[:200], ""]:  # bounded: an ask's words; "" ends the last run
        tokens = _answer_tokens(word)
        if word and tokens and tokens <= proper and not tokens & taken and not word.isdigit():
            run.append(_fold(word))
            continue
        if run and not set(run) <= _NAME_STOP:
            out.append(tuple(run))
        run = []
    return out[:_MAX_PHRASES]


def _fold(word: str) -> str:
    """One word as names are compared: lower-cased, a plural folded ("Dolphins" is "dolphin"), except an acronym,
    which is no plural ("GOES" stays "goes", so the verb "goes" never carries it)."""
    assert isinstance(word, str), "a word"
    if len(word) >= 2 and word.isupper():
        return word.lower()
    folded = _folded_words(word)
    return folded[0] if folded else ""


def _words(text: str, budget: int) -> list[str]:
    """The folded words of the first ``budget`` characters of ``text``, in order."""
    assert budget >= 0, "a budget"
    return [w for w in (_fold(x) for x in _WORD_RE.findall(str(text or "")[:budget])) if w]


def _initials(seq: list[str], least: int = 2) -> set[str]:
    """The initials of every run of ``least``..``_MAX_INITIALS`` consecutive words, fillers skipped ("Food and Drug
    Administration" spells "fda", "U.S. Geological Survey" "usgs")."""
    assert isinstance(seq, list), "words must be a list"
    heads = [w[0].lower() for w in seq if w.lower() not in _INITIAL_STOP]
    return {"".join(heads[i:i + n]) for i in range(len(heads)) for n in range(least, _MAX_INITIALS + 1)
            if i + n <= len(heads)}


def _capital_initials(text: str) -> set[str]:
    """Initials of three or more consecutive capitalised words of ``text`` ("National Weather Service"): a served
    text spells out an acronym only in a proper name. Fillers between them are skipped ("Food and Drug ...")."""
    assert isinstance(text, str), "text must be a string"
    out: set[str] = set()
    run: list[str] = []
    for word in [*_WORD_RE.findall(text[:_MAX_SERVED_CHARS]), ""]:  # bounded: the capped text; "" ends the last run
        if word and (word[0].isupper() or (run and word.lower() in _INITIAL_STOP)):
            run.append(word)
            continue
        out |= _initials(run, 3)
        run = []
    return out


def _carried(phrase: tuple[str, ...], served: set[str], named: set[str], initials: set[str]) -> bool:
    """Does the source carry this phrase: one of its own words (not a generic one) served or in its name, or a word
    of its name that begins with it ("nhl" in "nhle"); the phrase's initials as a word ("hn"); or the one-word
    phrase spelt out ("nws" by "National Weather Service")? Prefixes are read from the source's name only: a long
    served text holds every short prefix by chance."""
    assert phrase, "a phrase"
    for w in phrase:  # bounded: a phrase's words
        if w in _NAME_STOP:
            continue
        if w in served or w in named or (len(w) >= _MIN_PREFIX and any(x.startswith(w) for x in named)):
            return True
    if len(phrase) >= 2 and "".join(w[0] for w in phrase) in served | named:
        return True
    return len(phrase) == 1 and len(phrase[0]) >= 2 and phrase[0] in initials


def check_named(names: list[tuple[str, ...]], served: str, identity: list[str]) -> list[str]:
    """The reasons the source does not carry the ask's names ([] = it does, or the ask names nothing). ``served`` is
    what the source returned (the sample as text); ``identity`` how a trusted channel named it (address, titles)."""
    assert isinstance(served, str), "served must be text"
    assert isinstance(identity, list), "identity must be a list"
    if not names:
        return []
    named_seq = [w for text in identity[:8] for w in _words(text, 2000)]  # bounded: a few short names
    served_words = set(_words(served, _MAX_SERVED_CHARS))
    named, initials = set(named_seq), _initials(named_seq) | _capital_initials(served)
    missing = [" ".join(p) for p in names if not _carried(p, served_words, named, initials)]
    return [f"the ask names {m!r}, and nothing this source served or is called says it is from or about it"
            for m in missing]
