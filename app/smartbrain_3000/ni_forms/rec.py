"""Record access helpers shared by every form (roles, typed columns, times).

Forms read roles, types, units, signatures and wants only; never words.
"""
from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import UTC, date, datetime

from . import fmt
from .types import (
    Candidate,
    CardInput,
    Context,
    DataRecord,
    Field,
    FieldProfile,
    ImageRef,
    Profile,
    RowMeta,
    TimeProfile,
)

NUMERIC_TYPES = {"number", "quantity", "currency", "percent", "duration"}
TIME_TYPES = {"time", "date", "datetime"}


def _dc(cls, d):
    if d is None or isinstance(d, cls):
        return d
    names = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


def as_record(d) -> DataRecord:
    """dict (JSON) -> DataRecord with Field/Context/RowMeta/ImageRef objects. Idempotent."""
    if isinstance(d, DataRecord):
        r = d
    else:
        r = _dc(DataRecord, dict(d))
    r.fields = [_dc(Field, f) for f in r.fields]
    r.context = _dc(Context, r.context)
    if r.row_meta is not None:
        r.row_meta = [_dc(RowMeta, m) for m in r.row_meta]
    r.image = _dc(ImageRef, r.image)
    r.parts = {k: as_record(v) for k, v in (r.parts or {}).items()}
    return r


def as_profile(d) -> Profile:
    if isinstance(d, Profile):
        p = d
    else:
        p = _dc(Profile, dict(d))
    p.fields = [_dc(FieldProfile, f) for f in p.fields]
    p.time = _dc(TimeProfile, p.time)
    return p


def as_input(d) -> CardInput:
    return d if isinstance(d, CardInput) else _dc(CardInput, dict(d))


def as_cand(d) -> Candidate:
    return d if isinstance(d, Candidate) else _dc(Candidate, dict(d))


class R:
    """A read-only view of a record for layout: field lookup by name/role, typed columns."""

    def __init__(self, rec: DataRecord):
        self.rec = rec
        self.f = {f.name: f for f in rec.fields}
        self.idx = {f.name: i for i, f in enumerate(rec.fields)}
        self.rows = rec.rows
        self.tz = rec.context.card_tz

    # -- fields
    def by_role(self, *roles) -> list[Field]:
        return [f for f in self.rec.fields if f.role in roles]

    def first(self, *roles) -> Field | None:
        for r in roles:
            for f in self.rec.fields:
                if f.role == r:
                    return f
        return None

    def cell(self, row: int, name: str | None):
        if name is None or name not in self.idx or row >= len(self.rows) or row < 0:
            return None
        return self.rows[row][self.idx[name]]

    def col(self, name: str) -> list:
        i = self.idx[name]
        return [r[i] for r in self.rows]

    def num(self, row: int, name: str | None) -> float | None:
        v = self.cell(row, name)
        return float(v) if fmt.is_num(v) else None

    def time(self, row: int, name: str | None) -> datetime | date | None:
        return fmt.parse_t(self.cell(row, name))

    def text(self, row: int, name: str | None, *, derived=False) -> str:
        v = self.cell(row, name)
        if v is None or name is None:
            return ""
        return fmt.value_text(v, self.f[name], derived=derived)


def is_numeric(f: Field) -> bool:
    return f.type in NUMERIC_TYPES


def is_displayable(f: Field) -> bool:
    return f.role not in ("ignore", "link", "link_secondary", "image_url", "series_id") and \
        f.type not in ("url",)


_IANA = __import__("re").compile(r"^[A-Z][A-Za-z_]+/[A-Za-z_]+(/[A-Za-z_]+)?$")
_ELEV = frozenset({"elevation", "altitude", "alt", "elev", "height above sea level"})


def is_context(f: Field, rec: DataRecord) -> bool:
    """Where/which-zone metadata of the reading (coordinates, the zone name, the station's elevation
    beside its coordinates): context for the footer/summary, never a displayed fact."""
    if f.role in ("lat", "lon", "as_of", "ignore"):
        return True
    i = rec.fields.index(f) if f in rec.fields else None
    vals = [r[i] for r in rec.rows[:20] if i is not None and r[i] is not None]
    if f.type in ("category", "text") and vals and all(isinstance(v, str) and _IANA.match(v) for v in vals):
        return True
    key = " ".join(str(f.label or f.name).lower().replace("_", " ").split())
    tail = f.path.split(".")[-1].replace("[]", "").lower()
    if any(w in (key.split() + [tail]) for w in ("latitude", "longitude", "lat", "lon", "lng", "coordinates")):
        return True
    has_ll = any(g.role in ("lat", "lon") for g in rec.fields)
    return bool(has_ll and rec.kind == "measure" and any(w in key.split() or key == w for w in _ELEV))


def fact_ok(f: Field, rec: DataRecord) -> bool:
    """A field may be shown as a SECONDARY fact: displayable, not context, and named by the display
    vocabulary or the user (a raw source key the vocabulary does not know is never shown as a fact)."""
    if not (is_displayable(f) and not is_context(f, rec) and (f.label_src or "lexicon") != "key"):
        return False
    return _informative(f, rec)


_WEBLIKE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$|^(?:https?:)?//|^(?:[a-z0-9-]+\.)+[a-z]{2,}$", 2)
_NUMTXT = __import__("re").compile(r"^v?[\d.,:/+\- ]+$")


def _informative(f: Field, rec: DataRecord) -> bool:
    """Round 3: a secondary fact must tell the reader something about the row it sits on: never an identifier,
    never a number that is zero on most rows (a default the source fills in), never an e-mail address or a
    host name, and never a bare string of digits (a version or code that means nothing without its key)."""
    if f.type == "identifier":
        return False
    try:
        i = rec.fields.index(f)
    except ValueError:
        return True
    vals = [r[i] for r in rec.rows if r[i] is not None]
    if not vals:
        return True
    if f.type in NUMERIC_TYPES and len(vals) >= 3 and f.role not in ("value", "measure", "severity", "rank"):
        zeros = sum(1 for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0)
        if zeros >= 0.8 * len(vals):
            return False
    if f.type in ("text", "category") and f.role not in ("name", "status", "severity", "kind"):
        sv = [str(v).strip() for v in vals]
        if sum(1 for v in sv if _WEBLIKE.match(v)) >= 0.5 * len(sv):
            return False
        if sum(1 for v in sv if _NUMTXT.match(v)) >= 0.8 * len(sv):
            return False
    return True


def ts(v, tz: str | None = None) -> float | None:
    """Epoch seconds of a datetime/date cell for ordering and plotting. A calendar date is its midnight in
    the card zone `tz` (so it prints as the same date there), UTC when no zone is given - never the host's."""
    t = fmt.parse_t(v)
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.timestamp()
    z = fmt.zone(tz) if tz else None
    return datetime(t.year, t.month, t.day, tzinfo=z or UTC).timestamp()
