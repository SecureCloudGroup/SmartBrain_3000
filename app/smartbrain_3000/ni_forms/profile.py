"""PROFILE: a DataRecord (roles applied) -> Profile, code only, <= 5 ms.

Consolidated port of the proto's pipeline/data/profile.py plus pipeline/data/wants.py,
with the two helpers from lexicon (humanise) and timeparse (zone, parse_iso_z) inlined
so this module stays self-contained inside ni_forms.

Signatures come from roles, types, units and structure only - never from words. "Today",
day counts and future/past are computed in the CARD zone (context.card_tz), never in the
viewer's zone. NOTE (ported quirk): the ranked_list "long titles" branch (search for
`ask_st`) matches stems of the ask's words against field path tails - this is the one
place profile looks at the ask text to prefer a titled list over a table/bars.
"""
from __future__ import annotations

import html
import itertools
import json
import re
import statistics
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from . import asked as asked_mod
from . import canon
from .types import CardInput, DataRecord, FieldProfile, Profile, TimeProfile

_ASSETS = Path(__file__).with_name("assets")

# ----------------------------------------------------------------------------- helpers
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def humanise(key: str) -> str:
    """'regularMarketPrice' -> 'regular market price'; snake_case / kebab-case too."""
    assert key is not None, "humanise key is None"
    assert isinstance(key, (str, int, float)), "humanise key not stringable"
    k = html.unescape(str(key))
    k = _CAMEL.sub(" ", k)
    k = re.sub(r"[_\-.:/@$#\s]+", " ", k)
    return " ".join(k.lower().split())


def zone(name):
    """IANA name -> ZoneInfo, or None for junk input (mirrors proto timeparse.zone)."""
    assert name is None or isinstance(name, str), "zone name must be str or None"
    if not name or (not isinstance(name, str)) or ("/" not in name and name not in ("UTC", "GMT")):
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def parse_iso_z(s: str) -> datetime:
    """ISO 8601 Z string -> aware UTC datetime (mirrors proto timeparse.parse_iso_z)."""
    assert isinstance(s, str), "parse_iso_z takes a str"
    assert s, "parse_iso_z empty"
    return datetime.fromisoformat(s)


# ----------------------------------------------------------------------------- wants
@lru_cache(maxsize=1)
def _wants_lex() -> dict:
    """Load the generic want vocabulary from assets. Stale (15/21 covered) per the
    port manifest; WANTS_ALL in present.py is the authoritative list used by schemas."""
    p = _ASSETS / "lexicon" / "wants.json"
    assert p.exists(), f"wants lexicon missing at {p}"
    return json.loads(p.read_text(encoding="utf-8"))["wants"]


def _is_spaceless(p: str) -> bool:
    return any(ord(ch) > 0x2E80 for ch in p) or any(0x0600 <= ord(ch) <= 0x06FF for ch in p)


def extract_wants(ask: str) -> list[str]:
    """Ask -> ordered list of want ids (first appearance wins)."""
    assert ask is None or isinstance(ask, str), "ask must be str or None"
    assert True, "no invariant"
    a = " " + (ask or "").lower().replace("’", "'") + " "
    hits: list = []
    for wid, phrases in _wants_lex().items():
        pos: int | None = None
        for ph in phrases:
            p = ph.lower()
            if _is_spaceless(p):
                i = a.find(p)
            else:
                m = re.search(r"(?<![\w])" + re.escape(p) + r"(?![\w])", a)
                i = m.start() if m else -1
            if i >= 0 and (pos is None or i < pos):
                pos = i
        if pos is not None:
            hits.append((pos, wid))
    hits.sort()
    return [w for _, w in hits]

NUM = ("number", "quantity", "currency", "percent", "duration")
DISPLAY_NUM_ROLES = ("measure", "value", "secondary", "reference", "range_lo", "range_hi", "open", "delta",
                     "delta_pct", "goal", "progress", "share", "count", "score_a", "score_b")


def _field_profiles(rec: DataRecord) -> list:
    out = []
    for i, f in enumerate(rec.fields):
        col = [r[i] for r in rec.rows]
        nn = [c for c in col if c is not None]
        nums = [c for c in nn if isinstance(c, (int, float)) and not isinstance(c, bool)]
        lens = sorted(len(str(c)) for c in nn)
        srt = None
        if len(nn) >= 3:
            try:
                if all(a <= b for a, b in itertools.pairwise(nn)) and nn[0] != nn[-1]:
                    srt = "asc"
                elif all(a >= b for a, b in itertools.pairwise(nn)) and nn[0] != nn[-1]:
                    srt = "desc"
            except TypeError:
                srt = None
        out.append(FieldProfile(name=f.name, type=f.type, unit=f.unit, role=f.role,
                                distinct=len({str(c) for c in nn}), nulls=len(col) - len(nn),
                                min=float(min(nums)) if nums and f.type in NUM else None,
                                max=float(max(nums)) if nums and f.type in NUM else None,
                                sorted=srt, max_len=lens[-1] if lens else 0,
                                p50_len=lens[len(lens) // 2] if lens else 0))
    return out


def _time_field(rec: DataRecord) -> int | None:
    for role in ("time", "date"):
        for i, f in enumerate(rec.fields):
            if f.role == role and f.type in ("datetime", "date", "time"):
                return i
    for i, f in enumerate(rec.fields):
        if f.type in ("datetime", "date") and f.role == "unknown":
            return i
    return None


def _grain(secs: list) -> str | None:
    if not secs:
        return None
    d = sorted(b - a for a, b in itertools.pairwise(secs) if b != a)
    if not d:
        return None
    med = statistics.median(d)
    spread = (max(d) - min(d)) / med if med else 0
    table = [(1, "second"), (60, "minute"), (900, "quarter_hour"), (3600, "hour"), (86400, "day"),
             (7 * 86400, "week"), (30.4 * 86400, "month"), (365.25 * 86400, "year")]
    best = min(table, key=lambda t: abs(t[0] - med) / t[0])
    if abs(best[0] - med) / best[0] > 0.25 or spread > 0.6 and best[1] not in ("month", "year"):
        return "irregular"
    return best[1]


def time_profile(rec: DataRecord, now: datetime) -> TimeProfile:
    ti = _time_field(rec)
    if ti is None:
        return TimeProfile(field=None, grain=None, span_h=None, covers_today=False, future_events=0,
                           past_events=0, days=0)
    f = rec.fields[ti]
    z = zone(rec.context.card_tz) or UTC
    secs, days, fut, past = [], set(), 0, 0
    nowz = now if now.tzinfo else now.replace(tzinfo=UTC)
    for r in rec.rows:
        v = r[ti]
        if v is None:
            continue
        if f.type == "date":
            try:
                d = datetime.fromisoformat(v).date()
            except ValueError:
                continue
            days.add(d)
            secs.append(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
            if d > nowz.astimezone(z).date():
                fut += 1
            elif d < nowz.astimezone(z).date():
                past += 1
            continue
        if f.type == "time":
            continue
        t = parse_iso_z(v)
        secs.append(t.timestamp())
        days.add(t.astimezone(z).date())
        if t > nowz:
            fut += 1
        else:
            past += 1
    s = sorted(secs)
    return TimeProfile(field=f.name, grain=_grain(s), span_h=round((s[-1] - s[0]) / 3600, 3) if len(s) >= 2 else 0.0,
                       covers_today=nowz.astimezone(z).date() in days, future_events=fut, past_events=past,
                       days=len(days))


def signatures(rec: DataRecord, tp: TimeProfile, wants: list | None = None, ask: str | None = None) -> list:
    F = rec.fields
    roles = {f.role for f in F}
    n = len(rec.rows)
    num_disp = [f for f in F if f.type in NUM and f.role in DISPLAY_NUM_ROLES]
    names = [f for f in F if f.role in ("name", "kind")]
    out: list = []
    if rec.error:
        return out
    if rec.kind == "image" or rec.image is not None:
        out.append("image")
    if n == 0:
        out.append("empty")
        return out
    if rec.kind == "text" or ("text_body" in roles and n == 1):
        out.append("text_passage")
    if "goal" in roles or "progress" in roles:
        out.append("progress_to_goal")
    if n == 1:
        if "measure" in roles and "reference" in roles:
            out.append("measure_with_reference")
        if "measure" in roles and ("range_lo" in roles or "range_hi" in roles):
            out.append("measure_with_range")
        meas = [f for f in num_disp if f.role in ("measure", "secondary", "value")]
        if len(meas) >= 2 or (len(meas) == 1 and any(f.unit == "wmo" or f.role == "status" for f in F) and rec.kind == "measure"):
            out.append("multi_measure")
        if len(meas) == 1 and "measure_with_reference" not in out:
            out.append("single_measure")
        elif not meas and any(f.role == "measure" and f.type not in NUM for f in F):
            out.append("single_measure")          # one reading that is a word, a state or a yes/no
        if "time" in roles and (names or "measure" not in roles) and tp.future_events:
            out.append("events_with_kind")          # one upcoming event (a countdown target)
        if "status" in roles and not meas:
            out.append("status_records")
        if "lat" in roles and "lon" in roles:
            out.append("geo_points")
        if not out:
            out.append("multi_measure")         # one record of named facts with no single reading
        return out
    # multi-row readings
    if "family_reduced_quantiles" in rec.inferred:
        out.append("series_with_band")
    sid = next((i for i, f in enumerate(F) if f.role == "series_id"), None)
    if sid is not None:
        ids = {r[sid] for r in rec.rows}
        if 2 <= len(ids) <= 8:
            out.append("multi_series")
    ti = _time_field(rec)
    vals = [i for i, f in enumerate(F) if f.type in NUM and f.role in DISPLAY_NUM_ROLES]
    # rows that are distinct ITEMS (each has its own name) are events or records, never repeated readings
    # of one quantity: their numbers are attributes, not a line over time (round 1: events vs series)
    items = _distinct_items(rec)
    if ti is not None and vals and F[ti].type in ("datetime", "date") and not items:
        if sid is None and rec.kind in ("events", "series") and _alternates(rec, ti, vals):
            out.append("alternating_extrema")
        sub_daily = tp.grain in ("second", "minute", "quarter_hour", "hour") and (tp.span_h or 0) >= 72
        v0 = [r[vals[0]] for r in rec.rows if isinstance(r[vals[0]], (int, float))]
        if (sub_daily and tp.days >= 3) or (tp.grain == "day" and tp.days >= 28 and
                                            v0 and all(float(x).is_integer() and x >= 0 for x in v0)):
            out.append("matrix")
    if ti is not None and F[ti].type in ("datetime", "date"):
        bools = [i for i, f in enumerate(F) if f.type == "bool" and f.role not in ("ignore",)]
        if tp.grain == "day" and tp.days >= 28 and bools and "matrix" not in out:
            out.append("matrix")
    if rec.kind == "events" or (ti is not None and names and not vals):
        st = next((i for i, f in enumerate(F) if f.role in ("status", "kind") and f.type == "category"), None)
        nm = next((i for i, f in enumerate(F) if f.role == "name"), None)
        if st is not None and nm is not None:
            # the same entity passes through several states over time
            by: dict = {}
            for r in rec.rows:
                by.setdefault(r[nm], set()).add(r[st])
            multi = sum(len([r for r in rec.rows if r[nm] == k]) for k, v in by.items() if len(v) >= 2)
            if multi >= 0.3 * len(rec.rows) and len(by) < len(rec.rows):
                out.append("state_timeline")
        out.append("events_with_kind")
    if (ti is not None and vals and rec.kind in ("series", "events") or (ti is not None and vals and not names)) \
            and not items:
        out.append("time_series_regular" if tp.grain not in (None, "irregular") else "time_series_irregular")
    if ti is not None and F[ti].type == "date" and tp.days >= 2:
        out.append("dated_rows")
    if "share" in roles:
        out.append("percent_of_whole")
    elif wants and "share" in wants and names and len(vals) == 1 and n <= 40 and ti is None and \
            all(isinstance(r[vals[0]], (int, float)) and r[vals[0]] >= 0 for r in rec.rows if r[vals[0]] is not None):
        out.append("percent_of_whole")          # the ask asks for a breakdown of an additive, non-negative value
    if rec.row_meta and any(m.group or m.aggregate for m in rec.row_meta if m):
        out.append("hierarchical_records")
    if any(f.role == "name" for f in F) and (any(f.role in ("status", "severity") for f in F) or
                                             (ti is None and any(f.role == "kind" and f.type == "category" for f in F))):
        out.append("status_records")
    if "rank" in roles or (any(f.role == "name" for f in F) and vals and ti is None):
        out.append("ranked_records")
    if "link" in roles and any(f.role == "name" and (f.type == "text" or (
            f.type == "category" and len({r[i] for r in rec.rows}) == n)) for i, f in enumerate(F)):
        out.append("records_with_links")
    if n <= 12 and names and 1 <= len(vals) <= 3 and ti is None:
        out.append("multi_measure")
    if "lat" in roles and "lon" in roles:
        out.append("geo_points")
    if not out:
        out.append("ranked_records" if vals else "records_with_links" if "link" in roles else "status_records"
                   if names else "records_with_links")
    # items named by long titles (headlines, bill titles, product names) read as a titled list: a table or a
    # day grid would cut the very words that identify each item (round 2, form fit by data signature)
    nm = next((i for i, f in enumerate(F) if f.role == "name" and f.type == "text"), None)
    sched = bool(set(wants or []) & {"next", "times", "each_day", "countdown"}) or bool(tp.future_events)
    # ... unless the rows are matchups or shares, or their reading could not be shown in a list's meta line
    # rows whose reading the ask is about (a superlative, a comparison, a count, or the reading's own key in
    # the ask's words) keep a table / bars; otherwise the reading is a detail of each titled item
    ask_st = {w[:5] for w in re.findall(r"[a-z]{4,}", (ask or "").lower())}
    val_asked = any(f.type in NUM and f.role in ("value", "measure") and
                    ({t[:5] for t in humanise(f.path.rsplit(".", 1)[-1]).split() if len(t) >= 4} & ask_st)
                    for f in F) or bool(set(wants or []) & {"rank", "extreme_high", "extreme_low", "compare", "count"})
    hidden_val = val_asked and any(f.type in NUM and f.role in ("value", "measure") for f in F)
    paired = bool(roles & {"score_a", "score_b", "share"}) or "percent_of_whole" in out
    if nm is not None and n >= 2 and not sched and not paired and not hidden_val and \
            "records_with_links" != (out[0] if out else ""):
        vs = [str(r[nm]) for r in rec.rows if r[nm] is not None]
        if vs and sum(len(v) for v in vs) / len(vs) >= 24 and len(set(vs)) >= 0.8 * len(vs):
            if "records_with_links" in out:
                out.remove("records_with_links")
            out.insert(0, "records_with_links")
    return out


def _distinct_items(rec: DataRecord) -> bool:
    """A name column that tells at least half the rows apart (titles, addresses, vessel names)."""
    n = len(rec.rows)
    if n < 3:
        return False
    for i, f in enumerate(rec.fields):
        if f.role == "name" and f.type in ("text", "category", "identifier"):
            vals = [r[i] for r in rec.rows if r[i] is not None]
            if vals and len(set(vals)) >= 0.5 * n and rec.kind == "events":
                return True
    return False


def _alternates(rec, ti, vals) -> bool:
    """Strict alternation of local maxima and minima in time order: >= 8 points, or >= 4 points with a
    two-valued category that alternates in lockstep (random data alternates by chance too often below)."""
    order = sorted(range(len(rec.rows)), key=lambda k: str(rec.rows[k][ti] or ""))
    cats = [i for i, f in enumerate(rec.fields) if f.type == "category" and
            len({rec.rows[k][i] for k in order}) == 2]
    for i in vals[:2]:
        xs = [rec.rows[k][i] for k in order]
        if len(xs) < 4 or any(not isinstance(x, (int, float)) for x in xs):
            continue
        s = [1 if b > a else -1 if b < a else 0 for a, b in itertools.pairwise(xs)]
        if 0 in s or not all(p != q for p, q in itertools.pairwise(s)):
            continue
        if len(xs) >= 8:
            return True
        for c in cats:
            cs = [rec.rows[k][c] for k in order]
            if all(a != b for a, b in itertools.pairwise(cs)):
                return True
    return False


def wants_coverage(rec: DataRecord, wants: list, tp: TimeProfile) -> dict:
    F = rec.fields
    by = {}
    for f in F:
        by.setdefault(f.role, f.name)
    num = next((f.name for f in F if f.type in NUM and f.role in ("measure", "value")), None)
    name = by.get("name")
    out = {}
    for w in wants:
        if w in ("extreme_high", "extreme_low"):
            out[w] = (by.get("range_hi") if w == "extreme_high" else by.get("range_lo")) or (num if len(rec.rows) > 1 else None)
        elif w == "times":
            out[w] = tp.field
        elif w == "each_day":
            out[w] = tp.field if tp.days >= 2 else None
        elif w == "now":
            out[w] = num or tp.field or name
        elif w == "next":
            out[w] = tp.field if tp.future_events else None
        elif w == "trend":
            out[w] = num if tp.field and len(rec.rows) >= 3 else None
        elif w == "compare":
            out[w] = num if len(rec.rows) >= 2 and (name or by.get("series_id")) else None
        elif w == "rank":
            out[w] = by.get("rank") or (num if name and len(rec.rows) >= 2 else None)
        elif w == "count":
            out[w] = name or num
        elif w == "yesno":
            out[w] = by.get("status") or num
        elif w == "threshold":
            out[w] = num
        elif w == "where":
            out[w] = by.get("lat") or by.get("venue") or next((f.name for f in F if f.role == "meta" and f.type in ("text", "category")), None)
        elif w == "read":
            out[w] = by.get("text_body")
        elif w == "height_value":
            out[w] = num
        elif w == "progress":
            out[w] = by.get("goal") or by.get("progress")
        elif w == "status":
            out[w] = by.get("status") or by.get("severity")
        elif w == "list":
            out[w] = name
        elif w == "share":
            out[w] = by.get("share") or (num if name else None)
        elif w == "countdown":
            out[w] = tp.field if tp.future_events or tp.field else by.get("as_of")
        else:
            out[w] = None
    return out


def profile(rec: DataRecord, inp: CardInput, now: datetime) -> Profile:
    tp = time_profile(rec, now)
    wants = extract_wants(inp.ask)
    sigs = signatures(rec, tp, wants, inp.ask)
    # a part adds its reading only when it is the history of a series or the current summary
    PART_SIGS = ("time_series_regular", "time_series_irregular", "multi_series", "series_with_band",
                 "single_measure", "multi_measure", "measure_with_reference", "measure_with_range")
    names = {str(r[i]) for i, f in enumerate(rec.fields) if f.role == "name" for r in rec.rows if r[i] is not None}
    for p in rec.parts.values():
        if p.kind not in ("series", "measure"):
            continue
        sid = next((i for i, f in enumerate(p.fields) if f.role == "series_id"), None)
        if sid is not None and names and {str(r[sid]) for r in p.rows if r[sid] is not None} <= names:
            continue                     # the part is the inner list of the record's own items: a detail
        for s in signatures(p, time_profile(p, now), wants):
            if s in PART_SIGS and s not in sigs:
                sigs.append(s)
    prof = Profile(sig="", n_rows=len(rec.rows), fields=_field_profiles(rec), signatures=sigs, time=tp,
                   wants=wants, wants_coverage=wants_coverage(rec, wants, tp), history_points=0,
                   asked=asked_mod.asked_fields(rec.fields, inp.ask or "", list(getattr(inp, "wants", None) or [])))
    body = {"sigs": sigs, "types": [(f.type, f.role, f.unit) for f in rec.fields], "grain": tp.grain,
            "wants": wants}
    prof.sig = canon.sha256(canon.canonical(body))
    return prof
