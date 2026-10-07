"""Build a DataRecord + CardInput from the sealed pipeline's run outputs.

WHY: Phase 1a-2 feeds the pipeline's outputs into the forms engine so the same
shapes PRESENT reasoned over at build time are what the refresh binder reads.
Pure code, no model, no clock.

Two entry points, one rule ("sealed at design time; refresh re-binds by path"):

- ``from_answers`` runs ONCE, at build: the chosen Library answers (or the small
  answer-shaped list the mapping / page paths make) plus the sample outputs decide
  each field's type and role. ``value`` answers → a one-row ``measure`` record;
  ``list`` answers → ``events`` / ``records``; ``columns`` → ``series`` / ``records``.
- ``from_spec`` runs on every bind: the Fields come straight from the sealed
  ``form.record`` specs (never re-derived from a sample), only the cells are read
  from this run's outputs by each field's ``path``.

Cells: the pipeline's ``time`` / ``date`` transforms emit display text that still
knows its instant (``ni._TimeText.moment``); the record keeps the instant as ISO 8601
Z (datetime) or ``YYYY-MM-DD`` (date) and lets the forms format it in the card zone.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from . import fmt
from .types import CardInput, Context, DataRecord, Field, check_record

# ISO 4217 three-letter alpha codes — the subset we see on Library answers (declared
# ``$`` / ``USD`` / ``EUR`` / ``GBP`` lead; the rest ride in as currency). Closed set.
_CURRENCY_CODES: frozenset[str] = frozenset({
    "AED", "ARS", "AUD", "BRL", "CAD", "CHF", "CLP", "CNY", "COP", "CZK",
    "DKK", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JPY",
    "KRW", "MXN", "MYR", "NOK", "NZD", "PEN", "PHP", "PLN", "RON", "SEK",
    "SGD", "THB", "TRY", "TWD", "USD", "VND", "ZAR",
})

_UNIT_TO_CURRENCY: dict[str, str] = {"$": "USD", "€": "EUR", "£": "GBP"}

# answer label words the pack uses for a status-like text cell (ROLE pick).
_STATUS_LABEL_WORDS: frozenset[str] = frozenset({"status", "severity", "state"})

# a field the forms can lead with (the first such field becomes the ``measure``)
_LEAD_TYPES: frozenset[str] = frozenset({"number", "quantity", "currency", "percent",
                                         "category", "text", "datetime", "date", "bool"})
_NUMBER_TYPES: frozenset[str] = frozenset({"number", "quantity", "currency", "percent"})

# caps: rows mirror ni._MAX_REPEAT_MAX + a safety 50; fields = §34 _FORM_MAX_FIELDS
_ROWS_CAP = 50
_FIELD_CAP = 8
_CELL_CHARS = 120          # check_record's cell cap; the rest rides in long_text
_LONG_TEXT_CHARS = 4000
_MAX_HISTORY_POINTS = 200  # mirrors history_track_for's max_points
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_PATH_RE = re.compile(r"[^.\[\]]+|\[\d+\]")
_MAX_PATH_STEPS = 16


def history_key(name: str) -> str:
    """The §11 history track name for a measure field: ``<name>_h``.

    A track may not share a name with a pipeline output (``ni._validate_history_spec``),
    and the measure's own output IS ``name`` — so the series rides under a suffix.
    ``form_scene.history_track_for`` declares it; ``_series_part`` reads it."""
    assert isinstance(name, str) and name, "name required"
    assert len(name) <= 80, "name too long"
    return f"{name}_h"


def _unit_id(unit: str | None) -> str | None:
    """A source's unit spelling ("°F", "mph") → the lexicon UnitId ("degF") when an
    alias matches (case-insensitive); the raw text otherwise (``fmt.unit_display``
    falls back to it with a space join)."""
    assert unit is None or isinstance(unit, str), "unit must be a str or None"
    if not unit or not unit.strip():
        return None
    needle = unit.strip().lower()
    for uid, entry in fmt._units().items():  # bounded: ~60 lexicon units
        if not isinstance(entry, dict):
            continue
        aliases = [str(a).lower() for a in entry.get("aliases") or []]
        if needle == uid.lower() or needle in aliases:
            return uid
    return unit.strip()[:80]


def _number_field_type(unit: str | None) -> tuple[str, str | None, str | None]:
    """Pick a number FIELD_TYPE from the answer's unit text.

    Returns ``(type, currency, unit_id)``. ``$`` / ``USD`` / ``EUR`` / ``GBP`` / any
    ISO 4217 alpha code → ``currency``; ``%`` → ``percent``; any other unit →
    ``quantity`` with the lexicon unit id; no unit → ``number``.
    """
    assert unit is None or isinstance(unit, str), "unit must be a str or None"
    stripped = (unit or "").strip()
    assert len(stripped) <= 200, "unit text too long"
    if not stripped:
        return "number", None, None
    if stripped in _UNIT_TO_CURRENCY:
        return "currency", _UNIT_TO_CURRENCY[stripped], None
    upper = stripped.upper()
    if upper in _CURRENCY_CODES:
        return "currency", upper, None
    if stripped == "%":
        return "percent", None, None
    return "quantity", None, _unit_id(stripped)


def _decimals_of(value: object) -> int | None:
    """Published decimals when a number was observed as a string (preserves "4.5" / "4.50").

    Native floats lose their written precision at parse time (``5.1`` is bit-equal to
    ``5.0999999...``), so we only report precision when the sample carries a string.
    """
    assert value is None or isinstance(value, (int, float, bool, str)), "scalar"
    if not isinstance(value, str):
        return None
    body = value.strip()
    if not body or "." not in body:
        return None
    tail = body.rsplit(".", 1)[1]
    if not tail or tail.rstrip("0123456789") != "":
        return None
    return len(tail)


# ---- cells ---------------------------------------------------------------------

def _dig(node: object, dotted: str) -> object:
    """Follow ``a.b[0].c`` through dicts and lists; None when any step is missing.
    Mirrors ``ni_flow._dig`` (the pipeline's row keys are dotted paths)."""
    assert isinstance(dotted, str), "dotted path must be a str"
    assert len(dotted) <= 400, "dotted path too long"
    cur = node
    for part in _PATH_RE.findall(dotted)[:_MAX_PATH_STEPS]:  # bounded by the path depth
        if part.startswith("["):
            i = int(part[1:-1])
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _moment_of(value: object) -> datetime | None:
    """The instant a cell stands for: the ``moment`` the pipeline's time text carries
    (``ni._TimeText``), else an aware ISO 8601 string. Zoneless text is never guessed."""
    assert value is None or isinstance(value, (str, int, float, bool)), "scalar"
    moment = getattr(value, "moment", None)
    if isinstance(moment, datetime) and moment.tzinfo is not None:
        return moment
    if isinstance(value, str) and len(value) >= 20:
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _datetime_cell(value: object) -> str | None:
    """ISO 8601 Z for a datetime field, or None when the cell holds no instant."""
    assert value is None or isinstance(value, (str, int, float, bool)), "scalar"
    moment = _moment_of(value)
    if moment is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_cell(value: object) -> str | None:
    """``YYYY-MM-DD`` for a date field: the moment's own calendar day (the ``date``
    transform anchors it at local midnight) or a date-led string as written."""
    assert value is None or isinstance(value, (str, int, float, bool)), "scalar"
    moment = getattr(value, "moment", None)
    if isinstance(moment, datetime):
        return moment.date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            return None
    return None


def _number_cell(value: object) -> float | int | None:
    """A finite number (a numeric string counts); None otherwise."""
    assert value is None or isinstance(value, (str, int, float, bool)), "scalar"
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _cell_for(ftype: str, raw: object) -> tuple[object, str | None]:
    """``(cell, long_text)`` for one field type: text is capped at the contract's 120
    chars with the full text (≤4000) returned beside it; anything else is typed."""
    assert isinstance(ftype, str) and ftype, "ftype required"
    assert raw is None or isinstance(raw, (str, int, float, bool, dict, list)), "raw value"
    if raw is None or isinstance(raw, (dict, list)):
        return None, None
    if ftype in _NUMBER_TYPES:
        return _number_cell(raw), None
    if ftype == "datetime":
        return _datetime_cell(raw), None
    if ftype == "date":
        return _date_cell(raw), None
    if ftype == "bool":
        return (raw if isinstance(raw, bool) else None), None
    text = str(raw)
    if not text.strip():
        return None, None   # a reading the source left blank is a missing cell, not a word
    if len(text) <= _CELL_CHARS:
        return text, None
    return text[:_CELL_CHARS - 1] + "…", text[:_LONG_TEXT_CHARS]


def _rows_as_lists(rows: list, fields: list) -> tuple[list[list], dict]:
    """``rows`` from the pipeline is a list of dicts (keyed by each cell's row path).
    The engine's DataRecord carries parallel ``[[cell, cell, ...], ...]`` lists plus
    ``long_text["r,c"]`` for capped text cells."""
    assert isinstance(rows, list), "rows must be a list"
    assert isinstance(fields, list), "fields must be a list"
    out: list[list] = []
    long_text: dict = {}
    for row in rows[:_ROWS_CAP]:
        if not isinstance(row, dict):
            continue
        cells: list = []
        for c, f in enumerate(fields):  # bounded by _FIELD_CAP
            cell, full = _cell_for(f.type, _dig(row, f.path))
            cells.append(cell)
            if full is not None:
                long_text[f"{len(out)},{c}"] = full
        out.append(cells)
    return out, long_text


# ---- build-time field derivation --------------------------------------------

def _text_type(samples: list) -> str:
    """``category`` for a short closed vocabulary — ≤12 distinct strings that either repeat
    across the rows or are short tokens (≤24 chars, ≤3 words: "High", "TS", "On time") —
    else ``text`` (names, places, sentences)."""
    assert isinstance(samples, list), "samples must be a list"
    vals = [v for v in samples[:_ROWS_CAP] if isinstance(v, str) and v.strip()]
    uniq = set(vals)
    if not uniq or len(uniq) > 12:
        return "text"
    short = all(len(v) <= 24 and len(v.split()) <= 3 for v in uniq)
    return "category" if (len(uniq) < len(vals) or short) else "text"


_GEO_ROLES: dict[str, str] = {"latitude": "lat", "lat": "lat", "longitude": "lon", "lon": "lon", "lng": "lon"}
_AS_OF_NAMES: frozenset[str] = frozenset({"as_of", "updated", "updated_at", "timestamp", "observed", "time"})
_TEXT_BODY_CHARS = 36   # past a short phrase it reads as a passage (text_brief), never a hero
_MAP_ASK_WORDS: frozenset[str] = frozenset({"where", "location", "position", "coordinates", "map",
                                             "track", "tracking"})


def _geo_role(f: Field) -> str | None:
    """``lat`` / ``lon`` for a coordinate field (by name, label or path tail) — context the
    forms place on a map or in the footer, never the headline."""
    assert isinstance(f, Field), "f must be a Field"
    words = {f.name.lower(), f.label.lower().strip(), f.path.rsplit(".", 1)[-1].lower()}
    for w in words:  # bounded: 3 spellings
        if w in _GEO_ROLES:
            return _GEO_ROLES[w]
    return None


def _field_from_answer(answer: dict, name: str, path: str, samples: list) -> Field:
    """One Field from one answer / cell declaration + its sampled values. Types follow
    the declaration (``number`` / ``time`` / ``date`` / ``text`` / ``count``; ``codes``
    make a labelled text); the role is assigned afterwards by the record shape."""
    assert isinstance(answer, dict), "answer must be a dict"
    assert isinstance(name, str) and name, "name required"
    assert isinstance(samples, list), "samples must be a list"
    label = " ".join(str(answer.get("label") or name).split())[:80] or name
    atype = answer.get("type")
    unit = answer.get("unit")
    first = next((v for v in samples if v is not None), None)
    if atype == "count":
        return Field(name=name, label=label, path=path, type="number", label_src="lexicon")
    if atype == "number" and not answer.get("codes"):
        kind, currency, uid = _number_field_type(unit if isinstance(unit, str) else None)
        return Field(name=name, label=label, path=path, type=kind, unit=uid,
                     currency=currency, precision=_decimals_of(first), label_src="lexicon")
    if atype == "time":
        return Field(name=name, label=label, path=path, type="datetime", label_src="lexicon")
    if atype == "date":
        return Field(name=name, label=label, path=path, type="date", label_src="lexicon")
    return Field(name=name, label=label, path=path, type=_text_type(samples), label_src="lexicon")


def _apply_value_roles(fields: list, answers: list, row: list, ask: str) -> None:
    """Mutate ``fields`` in place for a one-row ``measure`` record (``row`` is the sample's
    cells, used only to prefer a headline that has a value):

    - a coordinate → ``lat`` / ``lon`` when the ask is about a place (where / location /
      position / coordinates / map / track: the map leads, no measure), else ``ignore`` (a
      reading beside its coordinates — "how high" — leads on its own, the coordinates stay
      context);
    - a sentence-long text → ``text_body`` (the card reads as a passage; it leads when no
      shorter field has a value);
    - the first other field a form can lead with, preferring one whose sample cell is
      filled → ``measure`` (a number, a word, a time);
    - a later time that is the reading's own stamp (answer window ``now``, or named
      as_of / updated / timestamp) → ``as_of``; everything else → ``secondary`` (a fact).
    Roles stay inside ``types.ROLES``.
    """
    assert isinstance(fields, list), "fields must be a list"
    assert isinstance(answers, list) and len(answers) == len(fields), "one answer per field"
    assert isinstance(row, list) and len(row) == len(fields), "one sample cell per field"
    assert isinstance(ask, str), "ask must be a str"
    wants_map = bool(set(re.findall(r"[a-z]+", ask.lower())) & _MAP_ASK_WORDS)
    passage = [i for i, f in enumerate(fields)
               if f.type == "text" and isinstance(row[i], str) and len(row[i]) > _TEXT_BODY_CHARS]
    leadable = [i for i, f in enumerate(fields) if f.type in _LEAD_TYPES and _geo_role(f) is None
                and i not in passage]
    filled = [i for i in leadable if row[i] is not None]
    if wants_map and any(_geo_role(f) is not None for f in fields):
        lead = None                                   # the map is the headline
    elif filled:
        lead = filled[0]
    else:
        lead = None if passage else (leadable or [None])[0]
    for i, (f, a) in enumerate(zip(fields, answers, strict=True)):  # bounded: _FIELD_CAP
        geo = _geo_role(f)
        if geo is not None:
            f.role = geo if wants_map else "ignore"
        elif i == lead:
            f.role = "measure"
        elif i in passage:
            f.role = "text_body"
        elif f.type == "datetime" and (a.get("window") == "now" or f.name in _AS_OF_NAMES):
            f.role = "as_of"
        else:
            f.role = "secondary"


def _names_rows(samples: list) -> bool:
    """A text column names its rows when its values are distinct across the sample (an
    identity), never when one word repeats down the list (a state)."""
    assert isinstance(samples, list), "samples must be a list"
    vals = [v for v in samples[:_ROWS_CAP] if isinstance(v, str) and v.strip()]
    return len(vals) <= 1 or len(set(vals)) == len(vals)


def _apply_list_roles(fields: list, cells: list, columns: list) -> None:
    """Mutate ``fields`` in place with record-shaped roles (``columns`` = each field's
    sampled values): the first text cell whose values name their rows → ``name`` (else
    the first text cell; a status/severity/state label → ``status``); other texts →
    ``kind`` when they are a closed vocabulary (category), else ``meta``; the first
    time/date → ``time`` / ``date``; the first number → ``value``, later numbers →
    ``secondary``; coordinates → ``lat`` / ``lon``."""
    assert isinstance(fields, list), "fields must be a list"
    assert isinstance(cells, list) and len(cells) == len(fields), "one cell per field"
    assert isinstance(columns, list) and len(columns) == len(fields), "one sample column per field"
    texts = [i for i, f in enumerate(fields) if f.type in ("text", "category") and _geo_role(f) is None
             and str(cells[i].get("label") or "").strip().lower() not in _STATUS_LABEL_WORDS]
    naming = [i for i in texts if _names_rows(columns[i])]
    name_at = (naming or texts or [None])[0]
    picked_time = picked_value = False
    for i, (f, c) in enumerate(zip(fields, cells, strict=True)):  # bounded: _FIELD_CAP
        label_lower = str(c.get("label") or "").strip().lower()
        if _geo_role(f) is not None:
            f.role = _geo_role(f)
        elif f.type in ("text", "category"):
            if label_lower in _STATUS_LABEL_WORDS:
                f.role = "status"
            elif i == name_at:
                f.role = "name"
            else:
                f.role = "kind" if f.type == "category" else "meta"
        elif f.type in ("datetime", "date") and not picked_time:
            f.role, picked_time = ("time" if f.type == "datetime" else "date"), True
        elif f.type in _NUMBER_TYPES and not picked_value:
            f.role, picked_value = "value", True
        elif f.type in _NUMBER_TYPES:
            f.role = "secondary"
        else:
            f.role = "meta"


def _slug(label: str) -> str:
    """Lowercase a label into a safe field name; empty string on no safe chars."""
    assert isinstance(label, str), "label must be a str"
    assert len(label) <= 400, "label too long"
    s = _SLUG_RE.sub("_", label.lower()).strip("_")
    return re.sub(r"_+", "_", s)


def _cell_names(cells: list) -> list[str]:
    """Unique slug names for a list's cells (from the label, else the row key)."""
    assert isinstance(cells, list), "cells must be a list"
    names: list[str] = []
    for i, c in enumerate(cells[:_FIELD_CAP]):
        base = _slug(str(c.get("label") or c.get("key") or c.get("path") or "")) or f"c{i}"
        if not base[0].isalpha():
            base = f"c_{base}"
        name = base if base not in names else f"{base}_{i}"
        names.append(name)
    return names


def _shape_values(chosen: list[dict], outputs: dict, ask: str) -> tuple[str, list, list, dict]:
    """Value answers → a one-row ``measure`` record. Each answer's ``name`` is the
    pipeline output it reads (the field path)."""
    assert isinstance(chosen, list) and chosen, "chosen required"
    assert isinstance(outputs, dict), "outputs required"
    answers = chosen[:_FIELD_CAP]
    fields = [_field_from_answer(a, a["name"], a["name"], [outputs.get(a["name"])]) for a in answers]
    rows, long_text = _rows_as_lists([outputs], fields)
    _apply_value_roles(fields, answers, rows[0] if rows else [None] * len(fields), ask)
    return "measure", fields, rows, long_text


def _shape_rows(answer: dict, outputs: dict, rows_name: str) -> tuple[str, list, list, dict]:
    """A list / columns answer → ``events`` (a time cell on a declared axis), ``series``
    (columns on an axis) or ``records``. Each cell's ``key`` (else its ``path``) is the
    row key the pipeline wrote; the sample column decides category vs text."""
    assert isinstance(answer, dict), "answer must be a dict"
    assert isinstance(rows_name, str) and rows_name, "rows_name required"
    cells = list(answer.get("cells") or [])[:_FIELD_CAP]
    rows_src = outputs.get(rows_name)
    rows_src = rows_src if isinstance(rows_src, list) else []
    names = _cell_names(cells)
    fields: list = []
    columns: list = []
    for c, name in zip(cells, names, strict=True):  # bounded by _FIELD_CAP
        key = str(c.get("key") or c.get("path") or name)
        samples = [_dig(r, key) for r in rows_src[:_ROWS_CAP] if isinstance(r, dict)]
        fields.append(_field_from_answer(c, name, key, samples))
        columns.append(samples)
    _apply_list_roles(fields, cells, columns)
    rows, long_text = _rows_as_lists(rows_src, fields)
    has_axis = bool(answer.get("axis"))
    has_time = any(f.type in ("datetime", "date") for f in fields)
    if answer.get("kind") == "columns":
        kind = "series" if has_axis else "records"
    else:
        kind = "events" if (has_axis and has_time) else "records"
    return kind, fields, rows, long_text


# ---- shared assembly ------------------------------------------------------------

def _source_host(url: str | None) -> str:
    """Hostname only; empty string on a malformed URL (never raises)."""
    assert url is None or isinstance(url, str), "url must be a str or None"
    if not url:
        return ""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _context_of(context: dict, outputs: dict, as_of: str | None) -> Context:
    """The record Context: the card zone is the source's own zone when the pipeline
    extracted an IANA ``zone`` output (``card_tz_src = data``), else the viewer's."""
    assert isinstance(context, dict), "context must be a dict"
    assert isinstance(outputs, dict), "outputs must be a dict"
    viewer_tz = str(context.get("viewer_tz") or "UTC")
    zone_out = outputs.get("zone")
    if isinstance(zone_out, str) and ("/" in zone_out or zone_out in ("UTC", "GMT")):
        card_tz, src = zone_out, "data"
    else:
        card_tz, src = viewer_tz, "c2_default"
    source_url = context.get("source_url")
    return Context(fetched_at=str(context.get("fetched_at") or ""), as_of=as_of,
                   source_host=_source_host(source_url if isinstance(source_url, str) else None),
                   card_tz=card_tz, card_tz_src=src)


def _input_of(context: dict, ask: str, title: str, cadence_s: int) -> CardInput:
    """The CardInput the shell + PRESENT read (title, host, cadence, the ask's words)."""
    assert isinstance(context, dict), "context must be a dict"
    assert isinstance(cadence_s, int) and cadence_s >= 0, "cadence_s must be a non-negative int"
    source_url = context.get("source_url")
    return CardInput(card_id="", ask=str(ask)[:400], title=str(title)[:200],
                     source_url=source_url if isinstance(source_url, str) else None,
                     source_kind="http_json", source_format="json", raw_path=None,
                     http_status=None, content_type=None,
                     fetched_at=str(context.get("fetched_at") or ""), cadence_s=cadence_s,
                     viewer_tz=str(context.get("viewer_tz") or "UTC"))


def _series_part(history: dict | None, measure: str | None, as_of: str) -> dict:
    """``parts["series"]`` from the item's §11 history slot: ``{<track>: [{"t", "v"}]}``
    read under ``history_key(measure)``; missing / empty = no part."""
    assert history is None or isinstance(history, dict), "history must be a dict or None"
    assert isinstance(as_of, str), "as_of must be a str"
    if not history or not measure:
        return {}
    series = history.get(history_key(measure))
    if not isinstance(series, list):
        return {}
    rows: list[list] = []
    for p in series[:_MAX_HISTORY_POINTS]:
        if not isinstance(p, dict):
            continue
        t, v = p.get("t"), p.get("v")
        iso = _datetime_cell(t) if isinstance(t, str) else None
        if iso and isinstance(v, (int, float)) and not isinstance(v, bool):
            rows.append([iso, float(v)])
    if not rows:
        return {}
    fields = [Field(name="t", label="t", path="t", type="datetime", role="time"),
              Field(name="v", label=measure, path="v", type="number", role="value")]
    ctx = Context(fetched_at=as_of, as_of=as_of, source_host="", card_tz="UTC",
                  card_tz_src="c2_default")
    return {"series": DataRecord(v=1, kind="series", fields=fields, rows=rows, context=ctx,
                                 producer="json_profile", fingerprint="series_history")}


def _as_of_of(fields: list, rows: list) -> str | None:
    """The record's own time: the ``as_of`` field's cell on the first row, or None."""
    assert isinstance(fields, list) and isinstance(rows, list), "fields + rows required"
    for i, f in enumerate(fields):  # bounded by _FIELD_CAP
        if f.role == "as_of" and rows and isinstance(rows[0][i], str):
            return rows[0][i]
    return None


def _assemble(kind: str, fields: list, rows: list, long_text: dict, outputs: dict, *,
              history: dict | None, context: dict, ask: str, title: str, cadence_s: int
              ) -> tuple[DataRecord, CardInput]:
    """Shared tail of both entry points: context, history part, fingerprint, check."""
    assert isinstance(kind, str) and kind, "kind required"
    assert isinstance(fields, list) and fields, "fields required"
    as_of = _as_of_of(fields, rows)
    ctx = _context_of(context, outputs, as_of)
    measure = next((f.name for f in fields if f.role == "measure" and f.type in _NUMBER_TYPES), None)
    record = DataRecord(v=1, kind=kind, fields=fields, rows=rows, context=ctx,
                        producer="json_profile", long_text=long_text,
                        parts=_series_part(history, measure, as_of or ctx.fetched_at),
                        fingerprint=f"{kind}:{len(fields)}:{','.join(f.name for f in fields)}:{len(rows)}")
    check_record(record)
    return record, _input_of(context, ask, title, cadence_s)


# ---- entry points ---------------------------------------------------------------

def from_answers(chosen: list[dict], outputs: dict, *, history: dict | None,
                 context: dict, ask: str, title: str, cadence_s: int,
                 rows_output_name: str | None = None) -> tuple[DataRecord, CardInput]:
    """Build-time ``(DataRecord, CardInput)`` from the answers the pipeline was built from
    and the sample outputs it produced (types + roles derived here, then sealed).

    ``context`` carries ``source_url``, ``fetched_at`` (ISO Z), ``viewer_tz`` (IANA).
    ``rows_output_name`` names the outputs key holding the list rows (``rows`` on the
    Library-answers path); None = value answers.
    """
    assert isinstance(chosen, list) and chosen, "chosen must be a non-empty list"
    assert isinstance(outputs, dict), "outputs must be a dict"
    assert isinstance(context, dict), "context must be a dict"
    first_kind = chosen[0].get("kind")
    if first_kind == "value":
        kind, fields, rows, long_text = _shape_values([a for a in chosen if a.get("kind") == "value"],
                                                      outputs, ask)
    elif first_kind in ("list", "columns"):
        kind, fields, rows, long_text = _shape_rows(chosen[0], outputs, rows_output_name or "rows")
    else:
        raise ValueError(f"from_answers: unknown answer kind {first_kind!r}")
    return _assemble(kind, fields, rows, long_text, outputs, history=history, context=context,
                     ask=ask, title=title, cadence_s=cadence_s)


def from_spec(record_spec: dict, outputs: dict, *, history: dict | None,
              context: dict) -> tuple[DataRecord, CardInput]:
    """Bind-time ``(DataRecord, CardInput)`` from the sealed ``form.record`` spec: every
    Field (name, label, path, type, role, unit, currency, scale, precision, wallclock)
    is taken as sealed; only the cells are read from ``outputs`` by path.

    ``context`` carries ``source_url``, ``fetched_at``, ``viewer_tz``, ``title``, ``ask``
    and ``cadence_s`` (the shell prints title, host and cadence, so the bind must see
    the same words the build did).
    """
    assert isinstance(record_spec, dict), "record_spec must be a dict"
    assert isinstance(outputs, dict), "outputs must be a dict"
    assert isinstance(context, dict), "context must be a dict"
    fields: list = []
    for f in record_spec["fields"][:_FIELD_CAP]:
        fields.append(Field(name=f["name"], label=f["label"], path=f["path"], type=f["type"],
                            role=f["role"], unit=f.get("unit"), currency=f.get("currency"),
                            scale=f.get("scale"), precision=f.get("precision"),
                            wallclock=bool(f.get("wallclock")), label_src="lexicon"))
    rows_name = record_spec.get("rows")
    if rows_name:
        src = outputs.get(rows_name)
        rows, long_text = _rows_as_lists(src if isinstance(src, list) else [], fields)
    else:
        rows, long_text = _rows_as_lists([outputs], fields)
    cadence = context.get("cadence_s")
    return _assemble(str(record_spec["kind"]), fields, rows, long_text, outputs,
                     history=history, context=context, ask=str(context.get("ask") or ""),
                     title=str(context.get("title") or ""),
                     cadence_s=cadence if isinstance(cadence, int) and cadence >= 0 else 0)
