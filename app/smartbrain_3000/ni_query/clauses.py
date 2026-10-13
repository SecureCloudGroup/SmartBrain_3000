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
- limit (``limit_for``): a recognized number right before or right after a word of the closed ranking
  table (``RANK_WORDS``: top, biggest, largest, smallest, highest, lowest, most, least, best, worst,
  first, last — "top 5", "ten biggest", "biggest ten" → the number; Round 20, Q3c: either order); else 1
  when the ask says next / latest / last and the kind is next_event, result or schedule on a list — with
  the order on the axis cell ("next" ascending from now, "latest" / "last" descending).
- ranking order (``rank_order``, Round 20, Q3c): a ``ranking`` ask the model left without an order is
  ordered by its ranked number cell — the number cell the ask's quantity words name (the select lexicon:
  "by trading volume" → the 24h volume cell), else the one the answer's own label names ("Top coins by
  market cap"), else the answer's first number cell — descending, ascending when the ask's ranking word
  is smallest / lowest / least.
- step preference (``step_for``, G3): an explicit step word ("hourly", "by hour", "daily", "by
  day") prefers the same-source list answer declaring that axis step over one that does not — a
  single current-hour reading is not an "hourly forecast".
- agg: "count" iff the kind is ``count``.
- comparators (``bind_comparators``, Round 20 Q3b): an explicit comparator the recognizers read is an
  anchor like an explicit time. Its quantity word (between the phrase and the number, else before
  the phrase) names ONE number cell of the chosen answer — by the cell's label or path tail, else by
  the answer's declared ``words`` when the answer has one number cell — and the IR carries it: added
  when the model's where has no comparator on that cell (note ``comparator_added``), its number
  replacing a different one the model wrote (``comparator_corrected``). A word naming no number cell
  or several adds nothing (coverage ``where: unbound``, as before). The unit never picks the cell
  ("4 in Alaska" is no inch).
- floor (``floor_answer``): among the answers that serve the question kind (list / columns for
  kinds that show rows, a value for current_value / status / text_brief), ranked by (Round 20,
  Q3b): (1) an explicit asked stretch the answer's declared window or axis step serves
  (``serves_time``: now → a value declared now / latest; today / tonight / tomorrow → a value declared
  for it or rows on a time axis, day rows only approximately for tonight; next hours → an hour /
  period axis; any other stretch — a weekday, a date, a weekend, N days, a month — a day / period
  axis, hour rows approximately; a stretch behind us — yesterday, past N days — prefers none, as the
  removed window rule read it); (2) ask words (time phrase, comparator, stop and consumed words out)
  and wants that are whole words of the answer's own name or label; (3) those that are its declared
  ``words``; (4) the declared primary; (5) the declared order.
"""
from __future__ import annotations

import itertools

from .decl import axis_of, axis_step_of, cells_of
from .recognize import words

_SELECT_STOP = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are", "was", "be", "by", "with",
    "from", "as", "it", "its", "this", "that", "what", "whats", "how", "when", "where", "who", "which", "my", "me",
    "i", "show", "get", "give", "tell", "will", "would", "can", "do", "does", "did", "there", "please", "s", "any",
    "current", "currently", "latest", "newest", "recent", "next", "last", "first", "top", "forecast", "expected",
    "much", "many", "map", "plot", "list", "trend", "history", "near", "around", "off", "per", "vs", "versus",
    "time", "weather"})   # "what time is …" and "weather" name no one quantity
# the closed ranking-word table (Round 20, Q3c): a word that ranks rows → the direction it ranks them
RANK_WORDS: dict[str, str] = {"top": "desc", "biggest": "desc", "largest": "desc", "smallest": "asc",
                              "highest": "desc", "lowest": "asc", "most": "desc", "least": "asc", "best": "desc",
                              "worst": "desc", "first": "desc", "last": "desc"}
_ONE_KINDS = frozenset({"next_event", "result", "schedule"})
_ONE_DAY = frozenset({"today", "tonight", "tomorrow"})
_NUM_CMP = frozenset({"<", "<=", ">", ">=", "=", "!="})
_MAX_COMPARATORS = 6
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
        before = [w for w, _, end in ws if end <= n["start"]][-1:]
        after = [w for w, start, _ in ws if start >= n["end"]][:1]
        if set(before + after) & RANK_WORDS.keys() and isinstance(n["value"], int) and 1 <= n["value"] <= 100:
            return n["value"], [], False
    said = {w for w, _, _ in ws}
    if kind not in _ONE_KINDS or answer.get("kind") == "value" or not said & {"next", "latest", "last"}:
        return None, [], False
    axis, ahead = axis_of(answer), "next" in said
    return 1, ([{"col": axis, "dir": "asc" if ahead else "desc"}] if axis else []), ahead


# an explicit step word names the row grain the ask wants (closed, G3): "hourly" / "daily", or
# "by" immediately before "hour" / "day"
_STEP_WORDS: dict[str, str] = {"hourly": "hour", "daily": "day"}


def step_for(ask: str) -> str | None:
    """The axis step ("hour" / "day") an explicit ask word names ("hourly", "by hour", "daily",
    "by day"), or None."""
    assert isinstance(ask, str), "ask must be a str"
    ws = words(ask)[:400]
    said = {w for w, _, _ in ws}
    by_adjacent = {b for (a, _, a_end), (b, b_start, _) in itertools.pairwise(ws) if a == "by" and b_start - a_end <= 1}
    step = next((s for w, s in _STEP_WORDS.items() if w in said or s in by_adjacent), None)
    assert step is None or step in _STEP_WORDS.values(), "a closed axis step"
    return step


def step_answer(ask: str, answer: dict, answers: list[dict]) -> dict:
    """``answer``, or the same-source list answer whose declared axis step matches an explicit
    step word of the ask when ``answer`` does not already serve it (module docstring: step
    preference) — a single current-hour reading is not an "hourly forecast"."""
    assert isinstance(ask, str) and isinstance(answer, dict), "ask + answer required"
    assert isinstance(answers, list), "answers must be a list"
    step = step_for(ask)
    if step is None or axis_step_of(answer) == step:
        return answer
    sibling = next((a for a in answers if a is not answer and a.get("kind") == "list"
                     and axis_step_of(a) == step), None)
    return sibling if sibling is not None else answer


def rank_order(ask: str, kind: str | None, answer: dict | None, answers: list[dict], spans: dict,
               consumed_words: frozenset[str]) -> list[dict]:
    """The ranking ask's default order (module docstring): [{col, dir}] on the ranked number cell, or []."""
    assert isinstance(ask, str) and isinstance(answers, list), "ask + answers required"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    numbers = [c["path"] for c in cells_of(answer) if c.get("type") == "number"]
    if kind != "ranking" or not numbers:
        return []
    lexicons = [(path, lex) for path, lex in _cell_lexicons(answer or {}, answers) if path in numbers]
    asked = asked_words(ask, spans, consumed_words)
    named = next((path for w in asked for path, lex in lexicons if _hits(w, lex)), None)
    own = _lex(str((answer or {}).get("label") or ""))
    labels = [(c["path"], _lex(str(c.get("label") or ""))) for c in cells_of(answer) if c["path"] in numbers]
    labelled = next((path for path, lex in labels if lex and lex <= own), None)
    low = any(RANK_WORDS.get(w) == "asc" for w, _, _ in words(ask)[:400])   # "top 5 lowest" ranks up
    return [{"col": named or labelled or numbers[0], "dir": "asc" if low else "desc"}]


def comparator_cell(comparator: dict, answer: dict | None) -> str | None:
    """The one number cell an explicit comparator names (module docstring), or None."""
    assert isinstance(comparator, dict), "a recognized comparator"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    numbers = [c for c in cells_of(answer) if c.get("type") == "number"]
    declared = _lex(*[str(w) for w in ((answer or {}).get("words") or [])[:20]])
    for word in list(comparator.get("quantity") or [])[:2]:  # bounded: nearest first
        w = fold(str(word))
        hits = [c["path"] for c in numbers if w in _lex(str(c.get("label") or "")) | _path_lex(c["path"])]
        if not hits and w in declared:
            hits = [c["path"] for c in numbers]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def bind_comparators(where: list[dict], answer: dict | None, comparators: list[dict]) -> tuple[list[dict], list[str]]:
    """(the where with every explicit comparator bound to its cell, notes) — module docstring. A model clause
    on the cell with the recognized number stands as written; a recognized comparator left over corrects a
    model clause on its cell with another number, else it is added."""
    assert isinstance(where, list) and isinstance(comparators, list), "where + comparators required"
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    out, notes, held = [dict(w) for w in where], [], set()
    named = [(c, comparator_cell(c, answer)) for c in comparators[:_MAX_COMPARATORS]]  # bounded
    left = []
    for c, col in named:  # first: the model's clauses that already carry a recognized comparator
        same = next((i for i, w in enumerate(out) if i not in held and w.get("col") == col
                     and w.get("op") in _NUM_CMP and _same_number(w.get("value"), c["value"])), None)
        if col is not None and same is None:
            left.append((c, col))
        elif same is not None:
            held.add(same)
    for c, col in left:  # then: correct a clause on the cell with another number, else add one
        clause = {"col": col, "op": c["op"], "value": c["value"]}
        mine = [i for i, w in enumerate(out) if i not in held and w.get("col") == col and w.get("op") in _NUM_CMP]
        if mine:
            out[mine[0]] = clause
            notes.append("comparator_corrected")
        else:
            out.append(clause)
            notes.append("comparator_added")
        held.add(mine[0] if mine else len(out) - 1)
    return out, notes


def _same_number(a: object, b: object) -> bool:
    assert not isinstance(a, bytes) and not isinstance(b, bytes), "decoded values only"
    assert b is not None, "a recognized number"
    try:
        return float(a) == float(b)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def serves(answer: dict, kind: str | None) -> bool:
    """Does the answer's shape serve the question kind (a value vs rows)?"""
    assert isinstance(answer, dict), "answer must be a dict"
    assert kind is None or isinstance(kind, str), "kind must be a str"
    if kind in _VALUE_KINDS:
        return answer.get("kind") == "value"
    return kind is None or answer.get("kind") != "value"


def serves_time(answer: dict, time: dict | None) -> int:
    """How well the answer's declared window or axis step serves the explicit asked stretch (module
    docstring, the floor's first key — the removed ``select_answers`` window rule): 2 = cut exactly,
    1 = approximately (day rows for tonight, hour rows for a day-grain stretch), 0 = not, or no stretch."""
    assert isinstance(answer, dict), "answer must be a dict"
    assert time is None or isinstance(time, dict), "time must be a dict or None"
    lo, hi = (time or {}).get("from"), (time or {}).get("to")
    if not time or any(t == "yesterday" or str(t).startswith("past_") for t in (lo, hi)):
        return 0  # no stretch, or one behind us (the removed rule had no window for those: "last night")
    axis = answer.get("axis")
    step = axis.get("step") if isinstance(axis, dict) else None
    if lo == "now" and hi in ("now", None):
        return 2 if answer.get("kind") == "value" and answer.get("window") in ("now", "latest") else 0
    if lo == hi and lo in _ONE_DAY:
        exact = answer.get("window") == lo or step in ("hour", "period") or (step == "day" and lo != "tonight")
        return 2 if exact else (1 if step == "day" else 0)
    if any(str(t).startswith("next_hours:") for t in (lo, hi)):
        return 2 if step in ("hour", "period") else 0
    return 2 if step in ("day", "period") else (1 if step == "hour" else 0)


def floor_answer(answers: list[dict], kind: str | None, ask: str, wants: list, *, spans: dict | None = None,
                 consumed_words: frozenset[str] = frozenset(), time: dict | None = None) -> dict:
    """The rules floor's answer (module docstring): the best key of (serves the stretch, name / label
    hits, ``words`` hits, primary, declared order) among the answers that serve the question kind."""
    assert isinstance(answers, list) and answers, "answers required"
    assert isinstance(wants, list), "wants must be a list"
    serving = [a for a in answers if serves(a, kind)] or list(answers)
    said = set(asked_words(ask, spans or {}, consumed_words))
    said |= {fold(w) for w, _, _ in words(" ".join(str(w) for w in wants[:4]))[:40] if w not in _SELECT_STOP}
    scored = []
    for i, a in enumerate(serving[:_MAX_LEX]):  # bounded by the declared answers
        own = _lex(str(a.get("name") or ""), str(a.get("label") or ""))
        told = _lex(*[str(w) for w in (a.get("words") or [])[:20]])
        scored.append((serves_time(a, time), len(said & own), len(said & told), bool(a.get("primary")), -i))
    best = serving[-max(scored)[-1]]
    assert best in serving, "the floor picks a serving answer"
    return best
