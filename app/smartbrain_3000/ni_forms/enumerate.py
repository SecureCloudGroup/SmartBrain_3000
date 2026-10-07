"""ENUMERATE (CONTRACTS.md 5.5): every form's `match` -> complete candidates (form,
variant, bindings, params), each laid out at every span (both bucket ends, and the
designed 0/1-row states for record forms) to compute `plans` / `rejected_spans`,
`covers`, `default_span`, `phone_span`; ranked by the rules floor; <= 6 kept.

The floor is a code table of signature -> form preference plus want coverage plus a
lint-clean default, then the FRAME prior (fix round 1a-5, plan contract B2: the ask's
question kind -> forms, `KIND_FORMS`) and the asked-field rule (a span that drops the
field the ask names ranks below one that keeps it). `next_event` is offered only for a
next_event / schedule question or a `next` want. When nothing survives, `fallback`
returns the honest plain card (table / kv_grid) so a record never leaves without a form.
No words, no per-card settings, never shown to the model.
"""
from __future__ import annotations

import builtins
from datetime import datetime

from .layout import layout_span
from .rec import as_input, as_profile, as_record, is_displayable
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
# B2 question kind -> forms in preference order (fix round 1a-5). `measure` = a one-row value record;
# rows records pick by their axis: `hour` (sub-daily grain), `day` (a dated axis), `timed` (any axis)
# and `rows` (no axis / the rest). The prior outranks the signature table (FRAME_W >= its max).
KIND_FORMS: dict[str, dict[str, list[str]]] = {
    "current_value": {"measure": ["stat", "conditions", "kv_grid"], "rows": ["series_line", "table"]},
    "forecast": {"measure": ["conditions", "stat"], "hour": ["series_line", "conditions", "day_table", "agenda"],
                 "day": ["day_table", "series_line"], "rows": ["day_table", "table"]},
    # event_curve leads the timed lists: its match() only accepts the alternating-extrema signature
    # (tides), where the curve with the next-event headline is the designed card (plan B2)
    "next_event": {"measure": ["next_event", "stat"], "timed": ["event_curve", "next_event", "agenda", "day_table"],
                   "rows": ["table", "ranked_list", "kv_grid"]},
    "schedule": {"measure": ["next_event", "stat"], "timed": ["event_curve", "agenda", "day_table", "next_event"],
                 "rows": ["table", "ranked_list"]},
    "result": {"measure": ["stat"], "rows": ["entity_list", "table", "ranked_list"]},
    "latest_items": {"measure": ["stat"], "rows": ["ranked_list", "table"]},
    "ranking": {"measure": ["stat"], "rows": ["table", "ranked_list", "bars"]},
    "trend": {"measure": ["stat"], "rows": ["series_line", "table"]},
    "status": {"measure": ["stat", "conditions"], "rows": ["entity_list", "table"]},
    "alerts": {"measure": ["stat"], "rows": ["entity_list", "table"]},
    "count": {"measure": ["stat"], "rows": ["entity_list", "table"]},
    "lookup": {"measure": ["kv_grid", "stat"], "rows": ["table", "ranked_list", "kv_grid"]},
    "compare": {"measure": ["kv_grid", "compare"], "rows": ["table", "compare", "bars"]},
    "map": {"measure": ["map_lite"], "rows": ["map_lite", "table"]},
    "image": {"measure": ["image"], "rows": ["image"]},
    "text_brief": {"measure": ["text_brief"], "rows": ["text_brief"]},
}
FRAME_W = 1.0            # weight of the frame prior (the signature prior's maximum)
ASKED_PENALTY = 0.6      # a default span that drops a field the ask names loses this much
_SUB_DAILY = ("second", "minute", "quarter_hour", "hour")


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


def frame_forms(kind: str | None, rec, prof) -> list[str]:
    """The B2 forms for the ask's question kind and this record's shape ([] = no prior: the words
    stated no kind). A rows record on a sub-daily axis is an `hour` series, on a dated axis a
    `day` series; a next_event / schedule question over rows with no time axis is a lookup."""
    assert kind is None or isinstance(kind, str), "kind must be a str or None"
    assert prof is not None, "prof required"
    entry = KIND_FORMS.get(kind or "")
    if not entry:
        return []
    if rec is not None and rec.kind == "measure":
        return entry["measure"]
    tp = prof.time
    timed = tp is not None and tp.field is not None
    if kind == "forecast" and timed:
        return entry["hour"] if tp.grain in _SUB_DAILY else entry["day"]
    if kind in ("next_event", "schedule") and timed:
        return entry["timed"]
    return entry["rows"]


def frame_bonus(form: str, forms: list[str]) -> float:
    """FRAME_W scaled by the form's place in the kind's list; 0 when the frame names it nowhere."""
    assert isinstance(form, str), "form must be a str"
    assert isinstance(forms, list), "forms must be a list"
    if form not in forms:
        return 0.0
    return FRAME_W * POS_W[min(forms.index(form), len(POS_W) - 1)]


def next_event_eligible(inp, prof) -> bool:
    """`next_event` answers a next_event / schedule question or a `next` want — never an hourly
    forecast or a lookup list that happens to hold a future time (live 2026-10-07). A sealed form
    is never withheld at bind: ``enumerate(keep=...)`` names it."""
    assert prof is not None, "prof required"
    kind = getattr(inp, "question_kind", None) if inp is not None else None
    return kind in ("next_event", "schedule") or "next" in (prof.wants or [])


def dropped_asked(cand: Candidate, span_key: str, asked: set) -> set:
    """The asked fields a candidate's plan at `span_key` does not show."""
    assert isinstance(cand, Candidate), "cand must be a Candidate"
    assert isinstance(asked, set), "asked must be a set"
    if not asked:
        return set()
    return asked - set((cand.shows.get(span_key) or {}).get("fields", []))


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
    """The smallest desktop span that keeps every field the ask names (fix round 1a-5: the asked
    quantity is never in the drop list), covers the reachable wants AND meets the density floor: an
    untruncated title, and for a record form at least min(3, rows) data rows (review 3/4: the 1x1 HN
    card showed 2 of 30 headlines)."""
    desk = [Span.parse(k) for k in cand.plans if k.startswith("d")]
    if not desk:
        return None, set(), set()
    rec_roles = rec_roles or {}
    cov_by = {s.key: set(form.covers(cand, prof, s.sclass)) for s in desk}
    wants = set(prof.wants or [])
    reach = set().union(*cov_by.values()) & wants
    asked = set(getattr(prof, "asked", None) or [])
    # an asked field no span of this candidate shows is not held against any span (the floor's
    # penalty and PRESENT's L-ASK gate judge the candidate as a whole)
    asked &= set().union(*(set((cand.shows.get(s.key) or {}).get("fields", [])) for s in desk))

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
    for keep_asked in (True, False):
        for need_dense in (True, False):
            for s in sorted(desk, key=_area):
                if reach <= cov_by[s.key] and (dense(s) or not need_dense) and \
                        (not dropped_asked(cand, s.key, asked) or not keep_asked):
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


def enumerate(rec, prof, inp, now: datetime, keep: frozenset = frozenset()) -> list[Candidate]:
    rec, prof, inp = as_record(rec), as_profile(prof), as_input(inp)
    raw = []
    for name in sorted(FORMS):
        form = FORMS[name]
        if name == "next_event" and name not in keep and not next_event_eligible(inp, prof):
            continue   # ``keep``: the bind always offers the SEALED form (a legacy node has no frame)
        try:
            ms = form.match(rec, prof)
        except Exception:          # a form that cannot read this record simply does not match
            ms = []
        for m in ms:
            raw.append((name, m))
    cands: list[Candidate] = []
    for name, m in raw:
        form = FORMS[name]
        c = Candidate(id=f"m{len(cands)}", form=name, variant=m["variant"], bindings=m["bindings"], params=m["params"],
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
    ranked = floor_rank(cands, prof, inp=inp, rec=rec)
    out, per = [], {}
    for c in ranked:
        if per.get(c.form, 0) >= MAX_PER_FORM:
            continue
        per[c.form] = per.get(c.form, 0) + 1
        out.append(c)
        if len(out) >= MAX_CANDS:
            break
    if not out:
        out = [fallback(rec, prof, inp, now)]
    for i, c in builtins.enumerate(out):
        c.id = f"c{i}"
    return out


def floor_rank(cands: list[Candidate], prof, inp=None, rec=None) -> list[Candidate]:
    """The floor order (stable: ties keep match order), in tiers: (1) candidates whose default span
    keeps every field the ask names, before any that drops one (the asked quantity is never in the
    drop list); (2) the frame prior — the B2 forms for `inp.question_kind` in their order, before
    forms the frame names nowhere; (3) signatures + want coverage (each candidate's `floor_score` on
    entry). The numeric `floor_score` is finalized too (frame bonus, asked penalty) for the record
    and PRESENT's second-option threshold — call once per enumeration."""
    assert isinstance(cands, list), "cands must be a list"
    assert prof is not None, "prof required"
    forms = frame_forms(getattr(inp, "question_kind", None) if inp is not None else None, rec, prof)
    asked = set(getattr(prof, "asked", None) or [])
    lost = {c.id: bool(c.default_span and dropped_asked(c, c.default_span, asked)) for c in cands}
    for c in cands:
        c.floor_score = round(c.floor_score + frame_bonus(c.form, forms) - (ASKED_PENALTY if lost[c.id] else 0.0), 4)
    rank = {c.id: forms.index(c.form) if c.form in forms else len(forms) for c in cands}
    return sorted(cands, key=lambda c: (lost[c.id], rank[c.id], -c.floor_score))


def _fallback_match(form_name: str, rec, prof) -> dict:
    """The form's own match when it has one, else a binding over every displayable column
    (a table with two columns, a measure with one field): the plain card always has one."""
    assert form_name in ("table", "kv_grid"), "fallback forms are table / kv_grid"
    assert rec is not None, "rec required"
    ms = FORMS[form_name].match(rec, prof)
    if ms:
        return ms[0]
    cols = [f.name for f in rec.fields if is_displayable(f) and f.type not in ("lat", "lon")] or \
        [f.name for f in rec.fields]
    if form_name == "kv_grid":
        return {"variant": "fields", "bindings": {"fields": cols[:24]}, "params": {}}
    name = next((f.name for f in rec.fields if f.name in cols and f.type in ("text", "category")), cols[0])
    return {"variant": "plain", "bindings": {"columns": cols[:10], "name": name}, "params": {}}


def fallback(rec, prof, inp, now: datetime) -> Candidate:
    """The universal fallback (fix round 1a-5, class C): when no candidate survives, a `table`
    (records / events / series) or a `kv_grid` (measure) over the record, at its smallest desktop
    span whose lint is clean — else the smallest span the form accepts, its lint recorded by the
    bind (`lint.codes`) rather than enforced. The card is honest and plain, never missing."""
    assert rec is not None and prof is not None, "rec + prof required"
    assert isinstance(now, datetime), "now must be a datetime"
    form_name = "kv_grid" if rec.kind == "measure" else "table"
    form = FORMS[form_name]
    m = _fallback_match(form_name, rec, prof)
    c = Candidate(id="c0", form=form_name, variant=m["variant"], bindings=m["bindings"], params=m["params"],
                  describes="", covers=[], intent=form.intent(m["variant"], m["params"]), plans={},
                  rejected_spans={}, default_span="", phone_span=None, floor_score=0.0, fallback=True)
    clean: dict[str, bool] = {}
    for sp in ALL_SPANS:
        a = form.accepts(c, rec, prof, sp)
        if isinstance(a, Reject):
            c.rejected_spans[sp.key] = a.code
            continue
        lo = layout_span(c, rec, prof, inp, sp, now)
        c.plans[sp.key] = a
        c.shows[sp.key] = lo.content
        clean[sp.key] = lo.lint.ok
    desk = [k for k in c.plans if k.startswith("d")]
    assert desk, "table / kv_grid accept a desktop span at every shape"
    c.default_span = min([k for k in desk if clean[k]] or desk, key=lambda k: _area(Span.parse(k)))
    ps = phone_pick(c.default_span, {Span.parse(k) for k in c.plans}, c)
    c.phone_span = ps.key if ps else None
    c.describes = form.describes(c, rec)
    c.covers = sorted(form.covers(c, prof, Span.parse(c.default_span).sclass))
    return c
