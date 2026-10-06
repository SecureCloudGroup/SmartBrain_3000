"""LAYOUT -> CLIR for one span (CONTRACTS.md 5.1, 6.2): shell + form body, lint at both
bucket ends and every timed variant, ladder until clean or exhausted.
"""
from __future__ import annotations

import copy
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from . import canon, fmt, ladder
from .ctx import Canvas, Draft, Ids, LayoutCtx
from .lint import lint
from .rec import R as RView
from .rec import as_cand, as_input, as_profile, as_record
from .shell import shell
from .spans import Span, bucket
from .types import LayoutOut, LintIssue

MAX_ATTEMPTS = 10
COUNT_STATES = ("empty", "one", "few", "many")
OVERLAYS = ("sample", "stale", "error_last_good", "not_ready", "needs_update")


def host_of(inp) -> str:
    try:
        return (urlsplit(inp.source_url or "").hostname or "") if inp.source_url else ""
    except ValueError:
        return ""


def _slice(rec, n: int):
    r = copy.copy(rec)
    r.rows = rec.rows[:n]
    if rec.row_meta is not None:
        r.row_meta = rec.row_meta[:n]
    return r


def apply_state(rec, override: str | None, record_form: bool):
    """(record view, state) for a forced state (tests + refresh)."""
    if override is None:
        return rec, "ok"
    if override in COUNT_STATES:
        if not record_form:
            return rec, "ok"
        n = len(rec.rows)
        k = {"empty": 0, "one": min(1, n), "few": min(n, 3), "many": n}[override]
        return _slice(rec, k), "ok"
    if override == "sample":
        r = copy.copy(rec)
        r.flags = dict(rec.flags, sample=True)
        return r, "ok"
    return rec, override


def count_state(n: int, cap: int | None) -> str:
    if n == 0:
        return "empty"
    if n == 1:
        return "one"
    if cap is not None and n > cap:
        return "many"
    return "few" if cap is None or n < cap else "many"


def lint_times(clir: dict, now: datetime) -> list[str]:
    """Simulated instants for clock-relative CLIRs: every timed-variant boundary +/- 1 min
    plus hourly across the variant span, capped at 48."""
    bounds = []
    for lb in clir.get("live", []):
        a = lb["args"]
        if lb["k"] == "timed_variants":
            for v in a["variants"]:
                for k in ("t_from", "t_to"):
                    if v.get(k):
                        bounds.append(fmt.parse_t(v[k]))
        elif lb["k"] in ("past_dim", "countdown") and a.get("t"):
            bounds.append(fmt.parse_t(a["t"]))
    bounds = [b for b in bounds if isinstance(b, datetime)]
    if not bounds:
        return []
    out = set()
    for b in bounds:
        out.add(b - timedelta(minutes=1))
        out.add(b + timedelta(minutes=1))
    lo, hi = min(bounds + [now]), max(bounds)
    t = lo
    while t <= hi and len(out) < 48:
        out.add(t)
        t += timedelta(hours=1)
    return sorted(fmt.iso(x) for x in out)[:48]


def layout_span(cand, rec, prof, inp, span: Span, now: datetime, *, state_override: str | None = None,
                roles=None) -> LayoutOut:
    from .registry import FORMS
    t0 = time.perf_counter()
    cand, rec, prof, inp = as_cand(cand), as_record(rec), as_profile(prof), as_input(inp)
    form = FORMS[cand.form]
    b = bucket(span)
    view, state = apply_state(rec, state_override, form.record_form)
    host = host_of(inp)
    rung = ladder.start()
    ms_lint = 0.0
    lt = None
    clir = None
    draft = None
    for _ in range(MAX_ATTEMPTS):
        ids = Ids()
        ctx = LayoutCtx(cand=cand, rec=view, prof=prof, inp=inp, span=span, bucket=b, body={}, now=now,
                        rung=rung, ids=ids, state=state, R=RView(view), host=host)
        scv = Canvas(ctx, Draft())
        ctx.body = shell(ctx, scv)
        draft = form.layout(ctx)
        prims = scv.d.prims + draft.prims
        ro = [scv.d.reading_order[0]] if scv.d.reading_order else []
        ro += draft.reading_order + scv.d.reading_order[1:]
        clir = {"v": 1, "form": cand.form, "variant": cand.variant, "cand": cand.id, "plan": draft.plan,
                "span": span.key, "bucket": {"min_w": b.min_w, "max_w": b.max_w, "h": b.h},
                "state": state if state in OVERLAYS else (draft.state if draft.state not in ("ok", "") else state),
                "prims": prims, "reading_order": ro,
                "summary": form.summary(cand, view, now) or cand.form,
                "live": scv.d.live + draft.live, "hitmap": scv.d.hitmap + draft.hitmap,
                "dropped": list(draft.dropped), "ladder": list(rung["applied"])}
        _now_gaps(clir, b)
        pmap = dict(scv.d.prov)
        pmap.update(draft.prov)
        meta = dict(draft.meta)
        meta["body"] = ctx.body
        meta["forced"] = state_override
        tl = time.perf_counter()
        lt = lint(clir, view, roles, prof, cand, inp, b, times=lint_times(clir, now), prov_map=pmap, meta=meta,
                  host=host)
        ms_lint += (time.perf_counter() - tl) * 1000
        if lt.ok:
            break
        if not ladder.step(rung, lt.issues, {p["id"]: p for p in prims}):
            break
    lt.ms = ms_lint
    total = (time.perf_counter() - t0) * 1000
    blob = canon.canonical(clir, float_dp=2)
    if len(blob) > 16 * 1024:   # CONTRACTS §6.2 budget: AMBER only, never red yet (port manifest)
        lt.issues.append(LintIssue(code="clir_budget", sev="amber",
                                   detail=f"{len(blob)} bytes > 16384"))
    lo = LayoutOut(clir=clir, hash=canon.sha256(blob), lint=lt, ms_layout=total - ms_lint, ms_lint=ms_lint)
    lo.content = content_of(clir, pmap)
    return lo


def content_of(clir: dict, pmap: dict) -> dict:
    """What a layout SHOWS (for the resize checks): the data fields in body text, the distinct data rows,
    whether it plots, and whether the title was cut."""
    from .lint import _fields_in
    fields, rows = set(), set()
    plot = False
    title_cut = False
    for p in clir["prims"]:
        if p["k"] == "path":
            plot = True
        if p["k"] != "text":
            continue
        if p["role"] == "title":
            title_cut = any(ln.endswith("…") for ln in p["lines"])
            continue
        if p["role"] == "footer":
            continue
        rc = pmap.get(p["id"])
        pre = ""
        while rc and rc[0] == "part":
            pre += f"{rc[1]}:"
            rc = rc[2]
        for fn in _fields_in(rc):
            fields.add(pre + fn)
        for r in _rows_in(rc):
            if not pre:
                rows.add(r)
    return {"fields": sorted(fields), "rows": len(rows), "plot": plot, "title_cut": title_cut}


def _rows_in(rc, out=None):
    out = set() if out is None else out
    if not isinstance(rc, list) or not rc:
        return out
    if rc[0] == "cell" or rc[0] == "raw":
        out.add(rc[2])
    elif rc[0] == "tpl":
        for v in rc[2].values():
            _rows_in(v, out)
    elif rc[0] == "join":
        for q in rc[2]:
            _rows_in(q, out)
    elif rc[0] in ("abs", "lower"):
        _rows_in(rc[1], out)
    return out


def _now_gaps(clir: dict, b) -> None:
    """A now-marker line sweeps its box as time passes: every label its sweep can cross is listed in
    args.gaps and painters break the line around those labels (it never draws through text)."""
    from .lint import resolve_x, text_box
    by = {p["id"]: p for p in clir["prims"]}
    for lb in clir.get("live", []):
        if lb["k"] != "now_marker":
            continue
        a = lb["args"]
        lines = [by[i] for i in a.get("prims", []) if i in by and by[i]["k"] == "line"]
        if not lines:
            continue
        ly0 = min(min(l["y0"], l["y1"]) for l in lines)
        ly1 = max(max(l["y0"], l["y1"]) for l in lines)
        gaps = []
        for p in clir["prims"]:
            if p["k"] not in ("text", "time") or p["role"] in ("title", "footer"):
                continue
            for W in (b.min_w, b.max_w):
                x0, y0_, x1, y1_ = text_box(p, W)[:4]
                sx0, sx1 = resolve_x(a["box"]["x0"], 0, W), resolve_x(a["box"]["x1"], 0, W)
                if x1 > sx0 - 2 and x0 < sx1 + 2 and y1_ > ly0 and y0_ < ly1:
                    gaps.append(p["id"])
                    break
        a["gaps"] = gaps


def layout_all(cand, rec, prof, inp, now) -> dict[str, LayoutOut]:
    cand = as_cand(cand)
    out = {}
    for key in cand.plans:
        out[key] = layout_span(cand, rec, prof, inp, Span.parse(key), now)
    return out
