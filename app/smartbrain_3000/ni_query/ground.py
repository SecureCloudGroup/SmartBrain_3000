"""GROUND (Round 20, Q3c — docs/internal/ni-format.md §36): the evidence a row filter's value stands on, and the
asked entities a ``where`` must carry. Both read the ask's proper-noun phrases (``proper_phrases``) — with the
closed ranking-word table of ``clauses``, the only reading of the ask's surface the query layer adds here.

A — grounded filter values (``ground_where``). A ``contains`` / ``names`` value survives only when positive
evidence grounds it (``grounded_by``):
  1. proper   — a proper-noun phrase of the ask holds it as whole words (here a phrase keeps the words a
     parameter consumed: "Washington Metro" is a name even when the pick's label took "Washington" — a value
     made only of consumed words never reaches this step, ``normalize.v1_where`` drops it);
  2. holiday  — a holiday span of the recognizers holds it;
  3. declared — it is a cell label of the answer, or a label of a number code table the answer declares
     (``wmo_weather``); codes (Q3d, Library answers spec v1.5) — a Library code table of a text cell the filter
     reads (its own column, or every text cell for "*"; ``Grounds.codes``, the caller's values — this module does
     no I/O) holds it: one of the table's values (case ignored), or a whole-word run of one (``said_whole``), the
     value's last word folded to its singular — "coastal flood advisory", "flood", "flood watches" and "warning"
     are held by the NWS event names ("Flood Watch", "Flood Warning", …); "anywhere" by none. A ONE-word value
     must be the first or the last word of a table value (the subject or the kind of a name): "weather",
     "current" and "local" sit inside "Winter Weather Advisory" / "Rip Current Statement" / "Tropical Cyclone
     Local Statement" and would narrow every alert list that said them, so they stand on no table; "fog advisory"
     (two words, inside "Dense Fog Advisory") stands. The kept filter
     carries the table's spelling when only the singular is held ("flood watches" → "flood watch"): ``contains``
     is a substring, so the plural would match no row. Note ``filter_grounded:codes``;
  4. keyword  — it is a taxonomy keyword of the asked subcategories (``Grounds.keywords``; plurals folded), or
     names a kind of one: its words end with a keyword of two words or more ("Flash Flood Watch" is a kind of
     "flood watch" — a one-word keyword is a topic, never a kind: "active wildfires" is no "wildfire");
  5. observed — a text cell of the sample's rows holds it as whole words (the cells the filter reads: its own
     column, or every text cell for "*").
The answer's ``words`` lexicon is no evidence: it says how people ask, never what rows hold. A question word the
model turned into a filter ("anywhere in the world", "active") is dropped — note ``filter_ungrounded:<value>``,
coverage ``where: approximated`` — and the interpretation line no longer claims it.

B — carried entities (``carry_entities``). On a league-wide or network-wide list — an address that takes no
parameter from a resolver (a team, place, airport or station the source fetches FOR) — every proper-noun phrase
of the ask is carried by a filter: when no ``contains`` / ``names`` value holds it, code adds {"col": "*", "op":
"names", "value": <phrase>} (note ``entity_added:<phrase>``, coverage ``where: recognizer``) — on rows with a
text cell only (a value answer, or rows that hold no text, name nothing), and never for a phrase the chosen
answer's own name or label holds ("AL East standings" names the "East" its rows are). Nothing left is the build's
honest empty ("the latest Canucks game" on a league scoreboard with no Canucks row). A resolver-filled address
already holds what the ask names, in a spelling the consumed words may not show ("SF", "NYC", "Habs"): there the
model's own filter is the only one.

Proper-noun phrases: runs of capitalized words (the first character an upper-case letter) — never the ask's first
word or one after "?" / "!", never "I", never a word a parameter consumed, a word of the source's own name, or a
word inside a recognized time phrase (time words, holidays). Capitalization is evidence only in a sentence-case
ask: when more than half of the words after the first are capitalized (title case, all caps), none is read.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .apply import cell_value, rows_of
from .clauses import fold
from .decl import cells_of
from .normalize import TEXT_OPS, said_whole
from .recognize import words

_MAX_WORDS = 400
_MAX_PHRASES = 6
_MAX_WHERE = 6                 # the IR's where bound (ir.schema.json maxItems; ni._MAX_QUERY_LIST)
_MAX_SAMPLE_ROWS = 200
_MAX_CELL_CHARS = 2000         # of a cell's text read for a whole-word match
_NOTE_CHARS = 60
_JOINERS = frozenset({" ", "'", "’", "-", ".", ". ", "&", " & "})   # what sits between one name's words
_SENTENCE_ENDS = ("?", "!")    # "." also closes abbreviations ("St. Louis"): never a sentence end here
_MAX_CODE_VALUES = 1000        # a Library code table's values (spec v1.5)
_MAX_VALUE_WORDS = 16          # of a filter value read against a code table
RULES = ("proper", "holiday", "declared", "codes", "keyword", "observed")


@dataclass(frozen=True)
class Grounds:
    """The evidence a caller holds beyond the ask and the answer (``plan_query``'s ``grounds``)."""

    keywords: frozenset = frozenset()   # casefolded taxonomy keywords of the asked subcategories
    sample: object = None               # the fetched payload the answer's rows are read from
    codes: dict = field(default_factory=dict)   # {code table: its values} the answers' text cells declare (Q3d)


def _starts_sentence(ask: str, ws: list[tuple[str, int, int]], i: int) -> bool:
    assert isinstance(ask, str) and 0 <= i < len(ws), "a word of the ask"
    assert ws[i][1] < ws[i][2], "a non-empty word"
    return i == 0 or ask[ws[i - 1][2]:ws[i][1]].strip().endswith(_SENTENCE_ENDS)


def proper_phrases(ask: str, spans: dict, consumed_words: frozenset[str], own_words: frozenset[str]) -> list[str]:
    """The ask's proper-noun phrases, in ask order (module docstring)."""
    assert isinstance(ask, str) and isinstance(spans, dict), "ask + spans required"
    assert isinstance(consumed_words, frozenset) and isinstance(own_words, frozenset), "word sets required"
    ws = words(ask)[:_MAX_WORDS]
    caps = [ask[start].isupper() for _, start, _ in ws]
    if not ws or 2 * sum(caps[1:]) > len(ws) - 1:
        return []   # title case or all caps: capitalization tells nothing
    timed = [(s["start"], s["end"]) for s in spans.get("time") or []]
    keep = [i for i, (w, start, end) in enumerate(ws)
            if caps[i] and not _starts_sentence(ask, ws, i) and ask[start:end] != "I"
            and w not in consumed_words and fold(w) not in consumed_words and w not in own_words
            and not any(a <= start and end <= b for a, b in timed)]
    out: list[str] = []
    run: list[int] = []
    for i in keep + [len(ws) + 1]:  # bounded by the ask's words; the sentinel closes the last run
        if run and i == run[-1] + 1 and ask[ws[run[-1]][2]:ws[i][1]] in _JOINERS:
            run.append(i)
            continue
        if run:
            out.append(ask[ws[run[0]][1]:ws[run[-1]][2]])
        run = [i]
    return out[:_MAX_PHRASES]


def _keyed(text: str) -> tuple[str, ...]:
    """A phrase as its folded words ("Heat Advisories" ≡ "heat advisory")."""
    assert isinstance(text, str), "text must be a str"
    assert len(text) <= 2000, "a phrase, not a text"
    return tuple(fold(w) for w, _, _ in words(text.casefold()))


def _declared(value: str, answer: dict | None) -> bool:
    """Rule 3: a cell label of the answer, or a label of a number code table it declares (``wmo_weather``)."""
    assert isinstance(value, str), "value must be a str"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    cells = cells_of(answer)
    labels = {str(c.get("label") or "").casefold() for c in cells}
    tables = {str(t) for t in [*(c.get("codes") for c in cells), (answer or {}).get("codes")] if t}
    if tables:  # lazy: the engine's code tables, read only when an answer declares one
        from ..ni import LABEL_TABLES
        labels |= {str(x).casefold() for t in tables for x in (LABEL_TABLES.get(t) or {}).values()}
    return value.casefold() in labels - {""}


def _singulars(value: str) -> list[str]:
    """``value``'s words as said, then with the last word's plural folded ("watches" → "watch", "advisories" →
    "advisory", "warnings" → "warning"): the spellings a code table's names are read under."""
    assert isinstance(value, str), "value must be a str"
    assert len(value) <= 400, "a filter value, not a text"
    ws = [w for w, _, _ in words(value.casefold())][:_MAX_VALUE_WORDS]
    if not ws:
        return []
    last = ws[-1]
    tails = [last, fold(last), last[:-2] if len(last) > 4 and last.endswith("es") else last]
    out = list(dict.fromkeys(" ".join([*ws[:-1], t]) for t in tails))
    assert 1 <= len(out) <= 3, "at most three spellings"
    return out


def _coded(value: str, col: object, answer: dict | None, codes: dict) -> str | None:
    """Rule 3, codes (module docstring): the spelling under which a Library code table of a text cell the filter
    reads holds ``value`` — the said words, else the singular ("flood watches" → "flood watch") — or None."""
    assert isinstance(value, str) and isinstance(codes, dict), "value + codes required"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    tables = {str(c["codes"]) for c in cells_of(answer)
              if c.get("type") == "text" and c.get("codes") and (col == "*" or c["path"] == col)}
    held = [str(v) for t in sorted(tables) for v in list(codes.get(t) or ())[:_MAX_CODE_VALUES]]
    if not held:
        return None
    for form in _singulars(value):  # bounded: three spellings
        if any(said_whole(form, v) and (" " in form or _edge_word(form, v)) for v in held):
            return form
    return None


def _edge_word(word: str, name: str) -> bool:
    """Is the one-word ``word`` the first or the last word of the table value ``name`` (its subject or its kind)?
    A lone middle word ("weather" in "Winter Weather Advisory") is a modifier and grounds nothing."""
    assert isinstance(word, str) and isinstance(name, str), "word + name required"
    ends = [w for w, _, _ in words(name.casefold())][:_MAX_VALUE_WORDS]
    if not ends:
        return False
    w = fold(word)
    return fold(ends[0]) == w or fold(ends[-1]) == w


def _observed(value: str, col: object, answer: dict | None, sample: object) -> bool:
    """Rule 5: a text cell the filter reads holds the value as whole words in one of the sample's rows."""
    assert isinstance(value, str), "value must be a str"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    if sample is None or answer is None or answer.get("kind") not in ("list", "columns"):
        return False
    texts = [c["path"] for c in cells_of(answer) if c.get("type") == "text"]
    paths = texts if col == "*" else [col] if col in texts else []
    try:
        rows, _ = rows_of(answer, sample if isinstance(sample, dict) else {"items": sample})
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):
        return False
    for _, row in rows[:_MAX_SAMPLE_ROWS]:  # bounded
        for path in paths:  # bounded by the answer's cells
            text = cell_value(row, str(path))
            if isinstance(text, str) and said_whole(value, text[:_MAX_CELL_CHARS]):
                return True
    return False


def grounded_by(value: str, col: object, answer: dict | None, *, phrases: list[str], spans: dict,
                grounds: Grounds) -> str | None:
    """The first rule (``RULES``) that grounds ``value`` as a filter on ``col``, or None (module docstring)."""
    assert isinstance(value, str) and value.strip(), "a filter value"
    assert isinstance(grounds, Grounds) and isinstance(phrases, list), "grounds + phrases required"
    v = " ".join(value.split())[:400]
    if any(said_whole(v, p) for p in phrases):
        return "proper"
    if any(said_whole(v, str(s["text"])) for s in spans.get("time") or [] if s.get("kind") == "holiday"):
        return "holiday"
    if _declared(v, answer):
        return "declared"
    if _coded(v, col, answer, grounds.codes) is not None:
        return "codes"
    key = _keyed(v)
    kinds = [_keyed(str(k)) for k in list(grounds.keywords)[:2000]]
    if key and any(k == key or (len(k) >= 2 and key[-len(k):] == k) for k in kinds):
        return "keyword"
    return "observed" if _observed(v, col, answer, grounds.sample) else None


def ground_where(where: list[dict], answer: dict | None, *, phrases: list[str], spans: dict,
                 grounds: Grounds) -> tuple[list[dict], list[str], bool]:
    """A (module docstring): (the filters that stand — a plural a code table holds only in the singular carried in
    the table's spelling —, the ungrounded values dropped — note-sized —, whether a code table grounded one)."""
    assert isinstance(where, list) and isinstance(spans, dict), "where + spans required"
    assert isinstance(grounds, Grounds), "grounds required"
    kept: list[dict] = []
    dropped: list[str] = []
    coded = False
    for w in where[:_MAX_WHERE * 2]:  # bounded
        value = w.get("value")
        if w.get("op") not in TEXT_OPS or not isinstance(value, str) or not value.strip():
            kept.append(w)
            continue
        rule = grounded_by(value, w.get("col"), answer, phrases=phrases, spans=spans, grounds=grounds)
        if rule is None:
            dropped.append(" ".join(value.split())[:_NOTE_CHARS])
            continue
        coded = coded or rule == "codes"
        v = " ".join(value.split())[:400]
        form = _coded(v, w.get("col"), answer, grounds.codes)
        said = " ".join(x for x, _, _ in words(v.casefold()))
        kept.append({**w, "value": form} if form is not None and form != said else w)
    return kept, dropped, coded


def carry_entities(where: list[dict], answer: dict | None, phrases: list[str]) -> tuple[list[dict], list[str]]:
    """B (module docstring): (the where carrying every phrase, the phrases code added). The caller passes no
    phrase for an address a resolver filled."""
    assert isinstance(where, list) and isinstance(phrases, list), "where + phrases required"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    if not any(c.get("type") == "text" for c in cells_of(answer)):
        return list(where), []
    carried = [str(w["value"]).casefold().strip() for w in where
               if w.get("op") in TEXT_OPS and isinstance(w.get("value"), str)]
    own = set(_keyed(f"{(answer or {}).get('name') or ''} {(answer or {}).get('label') or ''}".replace("_", " ")))
    out, added = list(where), []
    for phrase in phrases[:_MAX_PHRASES]:  # bounded
        said = phrase.casefold()
        if len(out) >= _MAX_WHERE or any(v and (v in said or said in v) for v in carried) \
                or set(_keyed(phrase)) <= own:
            continue
        out.append({"col": "*", "op": "names", "value": phrase})
        added.append(phrase)
    return out, added
