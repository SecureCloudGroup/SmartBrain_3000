"""Declared answers as the query layer reads them (Round 20, Q3a).

One answer comes in either shape — the Library's (``row`` / ``columns`` cells) or
``ni_flow._clean_answer``'s (cells under ``cells``); every other key is the same
(name, label, words, primary, kind, path, type, unit, codes, window, measure, filter,
axis, limit, newest_first, may_be_empty). These helpers read both and never mutate.
"""
from __future__ import annotations

ANSWER_KINDS = ("value", "list", "columns")
_MAX_ANSWERS = 20
_MAX_CELLS = 8


def cells_of(answer: dict | None) -> list[dict]:
    """The answer's cells (list / columns); [] for a value answer or None."""
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    if answer is None:
        return []
    cells = answer.get("cells") or answer.get("row") or answer.get("columns") or []
    assert isinstance(cells, list), "cells must be a list"
    return [c for c in cells[:_MAX_CELLS] if isinstance(c, dict) and isinstance(c.get("path"), str)]


def answer_named(answers: list[dict], name: object) -> dict | None:
    """The declared answer called ``name``, or None."""
    assert isinstance(answers, list), "answers must be a list"
    assert len(answers) <= _MAX_ANSWERS * 4, "answers bounded"
    return next((a for a in answers if isinstance(a, dict) and a.get("name") == name), None)


def axis_of(answer: dict | None) -> str | None:
    """The answer's declared axis cell path (the rows' own time index), or None."""
    assert answer is None or isinstance(answer, dict), "answer must be a dict or None"
    axis = (answer or {}).get("axis")
    assert axis is None or isinstance(axis, dict), "axis must be a dict"
    return axis.get("cell") if axis else None


def cell_types(answer: dict | None) -> dict[str, str]:
    """{cell path: cell type} of the answer's cells."""
    cells = cells_of(answer)
    assert len(cells) <= _MAX_CELLS, "cells bounded"
    out = {c["path"]: str(c.get("type") or "") for c in cells}
    assert len(out) <= len(cells), "one type per path"
    return out


def time_cell(answer: dict | None) -> dict | None:
    """The cell a time cut reads: the axis cell, else the first time / date cell."""
    cells = cells_of(answer)
    assert isinstance(cells, list), "cells must be a list"
    assert len(cells) <= _MAX_CELLS, "cells bounded"
    axis = axis_of(answer)
    hit = next((c for c in cells if axis and c["path"] == axis), None)
    return hit or next((c for c in cells if c.get("type") in ("time", "date")), None)


def counted_list(answers: list[dict], count_answer: dict) -> dict | None:
    """The list a declared count value counts (same ``path``), or None."""
    assert isinstance(answers, list), "answers must be a list"
    assert isinstance(count_answer, dict), "count_answer must be a dict"
    if count_answer.get("kind") != "value" or count_answer.get("type") != "count":
        return None
    return next((a for a in answers if a.get("kind") != "value" and a.get("path") == count_answer.get("path")), None)
