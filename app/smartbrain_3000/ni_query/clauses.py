"""CODE-OWNED CLAUSES (Round 20, Q3a — plan §E): ``select``, ``limit``, ``agg`` and the rules
floor's answer come from code, never from the model (the Q2 model over-generated exactly these:
every cell selected, params filled with places, the declared count chosen over list + count).

- select (``select_for``): the cells — or, on a value answer, the companion VALUE answers — the
  ask's quantity words name, matched through the Library's own lexicon: a cell's label and path
  words, plus the label + words of the source's MEASURED value answers whose label holds the
  cell's label ("Conditions" ⊂ "Conditions (next period)", whose words say "snow", "fog",
  "thunderstorm"). Asked words leave out stop words, words inside a recognized time phrase or
  comparator, and parameter-consumed words. A list / columns answer selects only when it
  carries ≥ 2 quantity cells (numbers, or text a measured answer describes — never a time /
  date cell or the declared filter's cell); a ranking selects nothing (its ranked cell is the
  order). A value answer's companions match words its own lexicon leaves unexplained, never
  share its measure and are no plain text label (a name, a station). [] when nothing matches.
- limit (``limit_for``): a recognized number right after top / first / last / biggest / largest
  ("top 5" → 5); else 1 when the ask says next / latest / last and the kind is next_event,
  result or schedule on a list — with the order on the axis cell ("next" ascending from now,
  "latest" / "last" descending).
- agg: "count" iff the kind is ``count``.
- floor (``floor_answer``): the serving answer whose own words best match the ask's words and
  the intent's wants, else the first declared primary that serves the question kind, else the
  first serving answer — "serving": list / columns for kinds that show rows, a value for
  current_value / status / text_brief.
"""
from __future__ import annotations

from .decl import axis_of, cells_of
from .recognize import words

_SELECT_STOP = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are", "was", "be", "by", "with",
    "from", "as", "it", "its", "this", "that", "what", "whats", "how", "when", "where", "who", "which", "my", "me",
    "i", "show", "get", "give", "tell", "will", "would", "can", "do", "does", "did", "there", "please", "s", "any",
    "current", "currently", "latest", "newest", "recent", "next", "last", "first", "top", "forecast", "expected",
    "much", "many", "map", "plot", "list", "trend", "history", "near", "around", "off", "per", "vs", "versus",
    "time", "weather"})   # "what time is …" and "weather" name no one quantity
_LIMIT_WORDS = frozenset({"top", "first", "last", "biggest", "largest"})
_ONE_KINDS = frozenset({"next_event", "result", "schedule"})
_VALUE_KINDS = frozenset({"current_value", "status", "text_brief"})
_MAX_LEX = 200


def fold(word: str) -> str:
    """A plural folded to its singular, as ni_flow folds answer words (-ies → -y, -s off)."""
    assert isinstance(word, str), "word must be a str"
    assert len(word) <= 200, "a word, not a text"
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _lex(*texts: str) -> set[str]:
    """Folded words of ``texts`` ("_" and "." separate words), ≥ 2 characters."""
    assert all(isinstance(t, str) for t in texts), "texts must be str"
    assert len(texts) <= _MAX_LEX, "texts bounded"
    out: set[str] = set()
    for text in texts:  # bounded
        out |= {fold(w) for w, _, _ in words(text.replace("_", " ").replace(".", " ")) if len(w) >= 2}
    return out


def _path_lex(path: str) -> set[str]:
    """A cell path's last segment as words: camelCase and snake_case split, letters only."""
    assert isinstance(path, str) and path, "path required"
    last = path.rsplit(".", 1)[-1]
    assert len(last) <= 200, "a path segment"
    spaced = "".join(" " + ch if ch.isupper() else ch for ch in last)
    return {w for w in _lex(spaced) if w.isalpha()}


def _hits(word: str, lexicon: set[str]) -> bool:
    assert isinstance(word, str) and word, "word required"
    assert isinstance(lexicon, set), "lexicon must be a set"
    return word in lexicon or (len(word) >= 3 and any(t.startswith(word) for t in lexicon))


def asked_words(ask: str, spans: dict, consumed_words: frozenset[str]) -> list[str]:
    """The ask's folded content words in order: no stop word, nothing inside a recognized time
    phrase or comparator, no parameter-consumed word."""
    assert isinstance(ask, str) and isinstance(spans, dict), "ask + spans required"
    assert isinstance(consumed_words, frozenset), "consumed_words must be a frozenset"
    taken = [(s["start"], s["end"]) for s in list(spans.get("time") or []) + list(spans.get("comparators") or [])]
    out = []
    for w, start, end in words(ask)[:400]:  # bounded
        if w in _SELECT_STOP or w in consumed_words or len(w) < 2 or any(a <= start and end <= b for a, b in taken):
            continue
        if fold(w) not in out:
            out.append(fold(w))
    return out


def _measured(answers: list[dict]) -> list[dict]:
    assert isinstance(answers, list), "answers must be a list"
    out = [a for a in answers if a.get("kind") == "value" and a.get("measure") and a.get("type") != "count"]
    assert len(out) <= len(answers), "a subset"
    return out


def _cell_lexicons(answer: dict, answers: list[dict]) -> list[tuple[str, set[str]]]:
    """(path, lexicon) for the answer's QUANTITY cells (module docstring)."""
    assert isinstance(answer, dict), "answer must be a dict"
    assert isinstance(answers, list), "answers must be a list"
    fixed = (answer.get("filter") or {}).get("path")
    out = []
    for cell in cells_of(answer):  # bounded by the cells
        if cell.get("type") in ("time", "date") or cell["path"] == fixed:
            continue
        label = _lex(str(cell.get("label") or ""))
        lexicon = label | _path_lex(cell["path"])
        linked = [a for a in _measured(answers) if label and label <= _lex(str(a.get("label") or ""))]
        for a in linked:  # bounded by the answers
            lexicon |= _lex(str(a.get("label") or ""), *[str(w) for w in (a.get("words") or [])[:20]])
        if cell.get("type") == "number" or linked:
            out.append((cell["path"], lexicon))
    return out


def _value_companions(answer: dict, answers: list[dict], asked: list[str]) -> list[str]:
    """Other value answers named by words the chosen answer's own lexicon leaves unexplained."""
    assert isinstance(answer, dict) and isinstance(asked, list), "answer + asked required"
    assert answer.get("kind") == "value", "a value answer"
    own = _lex(str(answer.get("name") or ""), str(answer.get("label") or ""),
               *[str(w) for w in (answer.get("words") or [])[:20]])
    left = [w for w in asked if not _hits(w, own)]
    out = []
    for w in left:  # bounded by the ask
        for other in answers:  # bounded by the answers
            if other is answer or other.get("kind") != "value" or other.get("type") == "count" \
                    or (other.get("type") == "text" and not other.get("measure")) \
                    or (other.get("measure") and other.get("measure") == answer.get("measure")):
                continue
            lexicon = _lex(str(other.get("name") or ""), str(other.get("label") or ""),
                           *[str(x) for x in (other.get("words") or [])[:20]])
            if _hits(w, lexicon) and other["name"] not in out:
                out.append(other["name"])
    return out[:6]


def select_for(ask: str, kind: str | None, answer: dict, answers: list[dict], spans: dict,
               consumed_words: frozenset[str]) -> list[str]:
    """The code-owned ``select`` (module docstring)."""
    assert isinstance(answer, dict) and isinstance(answers, list), "answer + answers required"
    assert kind is None or isinstance(kind, str), "kind must be a str"
    asked = asked_words(ask, spans, consumed_words)
    if answer.get("kind") == "value":
        return _value_companions(answer, answers, asked)
    cells = _cell_lexicons(answer, answers)
    if kind == "ranking" or len(cells) < 2:
        return []
    out = []
    for w in asked:  # bounded: the ask's words, in asked order
        out += [path for path, lexicon in cells if _hits(w, lexicon) and path not in out]
    return out[:6]


def limit_for(ask: str, kind: str | None, answer: dict, spans: dict) -> tuple[int | None, list[dict], bool]:
    """(limit, the axis order that goes with it, "next" = rows from now on)."""
    assert isinstance(ask, str) and isinstance(answer, dict), "ask + answer required"
    assert isinstance(spans, dict), "spans required"
    ws = words(ask)[:400]
    for n in spans.get("numbers") or []:  # bounded by the recognizer
        before = [w for w, _, end in ws if end <= n["start"]]
        if before and before[-1] in _LIMIT_WORDS and isinstance(n["value"], int) and 1 <= n["value"] <= 100:
            return n["value"], [], False
    said = {w for w, _, _ in ws}
    if kind not in _ONE_KINDS or answer.get("kind") == "value" or not said & {"next", "latest", "last"}:
        return None, [], False
    axis, ahead = axis_of(answer), "next" in said
    return 1, ([{"col": axis, "dir": "asc" if ahead else "desc"}] if axis else []), ahead


def serves(answer: dict, kind: str | None) -> bool:
    """Does the answer's shape serve the question kind (a value vs rows)?"""
    assert isinstance(answer, dict), "answer must be a dict"
    assert kind is None or isinstance(kind, str), "kind must be a str"
    if kind in _VALUE_KINDS:
        return answer.get("kind") == "value"
    return kind is None or answer.get("kind") != "value"


def floor_answer(answers: list[dict], kind: str | None, ask: str, wants: list) -> dict:
    """The rules floor's answer (module docstring)."""
    assert isinstance(answers, list) and answers, "answers required"
    assert isinstance(wants, list), "wants must be a list"
    serving = [a for a in answers if serves(a, kind)] or list(answers)
    said = {fold(w) for w, _, _ in words(" ".join([ask, *[str(w) for w in wants[:4]]]))[:400]
            if w not in _SELECT_STOP}
    scored = [(len(said & _lex(str(a.get("name") or ""), str(a.get("label") or ""),
                                *[str(w) for w in (a.get("words") or [])[:20]])), i, a) for i, a in enumerate(serving)]
    best = max(scored, key=lambda s: (s[0], -s[1]))
    if best[0] > 0:
        return best[2]
    return next((a for a in serving if a.get("primary")), serving[0])
