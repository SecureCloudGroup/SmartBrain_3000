"""ENUMERATE (CONTRACTS.md 5.5): every form's `match` -> complete candidates (form,
variant, bindings, params), each laid out at every span (both bucket ends, and the
designed 0/1-row states for record forms) to compute `plans` / `rejected_spans`,
`covers`, `default_span`, `phone_span`; ranked by the rules floor; <= 6 kept.

The floor is a code table of signature -> form preference plus want coverage plus a
lint-clean default. No words, no per-card settings, never shown to the model.
"""
from __future__ import annotations

import builtins
from datetime import datetime

from .layout import layout_span
from .rec import as_input, as_profile, as_record
from .registry import FORMS
from .spans import ALL_SPANS, Span, phone_default
from .types import Candidate, Reject

# signature -> forms in preference order (reviewed against the CONTRACTS.md 5.3 corpus lists)
SIG_FORMS: dict[str, list[str]] = {
    "measure_with_reference": ["stat", "kv_grid"],
    "measure_with_range": ["stat", "kv_grid"],
    "single_measure": ["stat", "kv_grid"],
    "multi_measure": ["conditions", "stat", "kv_grid"],
    "time_series_regular": ["series_line", "table", "heatmap"],
    "time_series_irregular": ["series_line", "agenda", "table"],
    "multi_series": ["series_line", "compare"],
    "series_with_band": ["series_line"],
    "matrix": ["heatmap", "series_line"],
    "alternating_extrema": ["event_curve", "day_table", "next_event"],
    "events_with_kind": ["next_event", "agenda", "day_table", "table"],
    "state_timeline": ["agenda", "table"],
    "dated_rows": ["day_table", "table", "next_event"],
    "status_records": ["entity_list", "table", "ranked_list"],
    "ranked_records": ["table", "bars", "compare", "ranked_list", "kv_grid"],
    "records_with_links": ["ranked_list", "table"],
    "progress_to_goal": ["progress", "bars", "table"],
    "percent_of_whole": ["bars", "table"],
    "hierarchical_records": ["table", "entity_list", "bars"],
    "geo_points": ["map_lite", "table", "ranked_list"],
    "text_passage": ["text_brief"],
    "image": ["image"],
    "empty": ["entity_list", "ranked_list", "table"],
}
POS_W = (1.0, 0.72, 0.52, 0.38, 0.28)
# Port manifest 2026-10-06: cap at 4 (proto was 6); PRESENT's model prompt fits ≤4 cleanly.
MAX_CANDS = 4
MAX_PER_FORM = 2


def _area(s: Span) -> tuple:
    return (s.cols * s.rows, s.rows, s.cols)


def floor_score(form: str, prof, covers_any: set, covers_default: set) -> float:
    sc = 0.0
    for si, sig in builtins.enumerate(prof.signatures or []):
        prefs = SIG_FORMS.get(sig, [])
        if form in prefs:
            sc = max(sc, POS_W[min(prefs.index(form), 4)] * (1.0 if si == 0 else 0.85 if si == 1 else 0.7))
    wants = set(prof.wants or [])
    if wants:
        sc += 0.35 * len(wants & covers_default) / len(wants) + 0.1 * len(wants & covers_any) / len(wants)
    return round(sc, 4)


def validate_spans(form, cand: Candidate, rec, prof, inp, now: datetime) -> tuple[dict, dict]:
    """plans / rejected_spans: each span laid out (both widths by lint) in the current
    state and, for record forms, the designed empty and one-row states."""
    plans, rej = {}, {}
    states = [None]
    if form.record_form and len(rec.rows) > 3:
        states += ["empty", "one", "few"]
    elif form.record_form and len(rec.rows) > 1:
        states += ["empty", "one"]
    elif form.record_form and len(rec.rows) == 1:
        states += ["empty"]
    # the footer states every card can enter on a later tick (5.4): a span that cannot show them
    # cleanly is not offered, so a refresh never lands the user's span in a red state
    states += ["sample", "stale", "error_last_good", "not_ready", "needs_update"]
    shows = {}
    for sp in ALL_SPANS:
        a = form.accepts(cand, rec, prof, sp)
        if isinstance(a, Reject):
            rej[sp.key] = a.code
            continue
        ok = True
        for st in states:
            lo = layout_span(cand, rec, prof, inp, sp, now, state_override=st)
            if st is None:
                shows[sp.key] = lo.content
            if not lo.lint.ok:
                ok = False
                break
        if ok:
            plans[sp.key] = a
        else:
            rej[sp.key] = "lint_red"
    # L-SHRINK (review 5): a larger span keeps everything a smaller span shows - every data field, at
    # least as many rows, and the plot. A span that drops content is not a designed span.
    order = sorted(plans, key=_size_key)
    for kb in order:
        for ka in order:
            if ka == kb or ka not in plans or kb not in plans or not _smaller(ka, kb):
                continue
            A, Bc = shows.get(ka) or {}, shows.get(kb) or {}
            lost = set(A.get("fields", [])) - set(Bc.get("fields", []))
            fewer = ka[0] == kb[0] and Bc.get("rows", 0) < A.get("rows", 0)     # rows: same device only
            if lost or fewer or (A.get("plot") and not Bc.get("plot")):
                plans.pop(kb)
                rej[kb] = "shrinks"
                break
    cand.shows = {k: shows[k] for k in plans if k in shows}
    return plans, rej


_WORDER = {("phone", 1): 0, ("desktop", 1): 1, ("phone", 2): 2, ("desktop", 2): 3, ("desktop", 3): 4,
           ("desktop", 4): 5}


def _size_key(k: str):
    s = Span.parse(k)
    return (_WORDER[(s.device, s.cols)], s.rows)


def _smaller(ka: str, kb: str) -> bool:
    a, b = _size_key(ka), _size_key(kb)
    return a[0] <= b[0] and a[1] <= b[1]


def pick_default(form, cand: Candidate, prof, n_rows: int = 0, rec_roles: dict | None = None
                 ) -> tuple[str | None, set, set]:
    """The smallest desktop span that covers the reachable wants AND meets the density floor: an
    untruncated title, and for a record form at least min(3, rows) data rows (review 3/4: the 1x1 HN
    card showed 2 of 30 headlines)."""
    desk = [Span.parse(k) for k in cand.plans if k.startswith("d")]
    if not desk:
        return None, set(), set()
    rec_roles = rec_roles or {}
    cov_by = {s.key: set(form.covers(cand, prof, s.sclass)) for s in desk}
    wants = set(prof.wants or [])
    reach = set().union(*cov_by.values()) & wants

    b = cand.bindings or {}
    lead = b.get("value") or next((m for m in (b.get("metrics") or []) if m in rec_roles and rec_roles[m] == "value"),
                                  None)
    lead_somewhere = bool(lead) and any(lead in (sh or {}).get("fields", []) for sh in cand.shows.values())

    def dense(s):
        sh = cand.shows.get(s.key) or {}
        if sh.get("title_cut"):
            return False
        if form.record_form and n_rows >= 2 and sh.get("rows", 0) and sh["rows"] < min(3, n_rows):
            return False
        if lead_somewhere and lead not in sh.get("fields", []):
            return False              # the default span shows the reading the card leads with (round 1)
        return True
    for need_dense in (True, False):
        for s in sorted(desk, key=_area):
            if reach <= cov_by[s.key] and (dense(s) or not need_dense):
                return s.key, reach, cov_by[s.key]
    s = min(desk, key=_area)
    return s.key, reach, cov_by[s.key]


def phone_pick(desk_key: str, valid: set, cand: Candidate):
    """Phone default: full width at the desktop span's rows (review 4); the half column only when the
    user asks for it, or when no full-width span is valid. A cut title steps to the next valid span."""
    d = Span.parse(desk_key)
    fulls = [Span("phone", 2, r) for r in range(d.rows, 4)] + [Span("phone", 2, r) for r in range(d.rows - 1, 0, -1)]
    for s in fulls:
        if s in valid and not (cand.shows.get(s.key) or {}).get("title_cut"):
            return s
    return phone_default(d, valid)


def enumerate(rec, prof, inp, now: datetime) -> list[Candidate]:
    rec, prof, inp = as_record(rec), as_profile(prof), as_input(inp)
    raw = []
    for name in sorted(FORMS):
        form = FORMS[name]
        try:
            ms = form.match(rec, prof)
        except Exception:          # a form that cannot read this record simply does not match
            ms = []
        for m in ms:
            raw.append((name, m))
    cands: list[Candidate] = []
    for name, m in raw:
        form = FORMS[name]
        c = Candidate(id="c?", form=name, variant=m["variant"], bindings=m["bindings"], params=m["params"],
                      describes="", covers=[], intent=form.intent(m["variant"], m["params"]), plans={},
                      rejected_spans={}, default_span="", phone_span=None, floor_score=0.0)
        c.plans, c.rejected_spans = validate_spans(form, c, rec, prof, inp, now)
        if not any(k.startswith("d") for k in c.plans):
            continue
        c.default_span, reach, cov_def = pick_default(form, c, prof, len(rec.rows),
                                                      {f.name: f.role for f in rec.fields})
        c.covers = sorted(cov_def)
        valid = {Span.parse(k) for k in c.plans}
        ps = phone_pick(c.default_span, valid, c)
        c.phone_span = ps.key if ps else None
        c.describes = form.describes(c, rec)
        cov_any = set()
        for k in c.plans:
            cov_any |= set(form.covers(c, prof, Span.parse(k).sclass))
        c.floor_score = floor_score(name, prof, cov_any, cov_def)
        cands.append(c)
    ranked = floor_rank(cands, prof)
    out, per = [], {}
    for c in ranked:
        if per.get(c.form, 0) >= MAX_PER_FORM:
            continue
        per[c.form] = per.get(c.form, 0) + 1
        out.append(c)
        if len(out) >= MAX_CANDS:
            break
    for i, c in builtins.enumerate(out):
        c.id = f"c{i}"
    return out


def floor_rank(cands: list[Candidate], prof) -> list[Candidate]:
    """Signatures + want coverage only (stable: ties keep match order)."""
    return sorted(cands, key=lambda c: -c.floor_score)
