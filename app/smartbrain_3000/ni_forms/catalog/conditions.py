"""`conditions`: multi-measure with one primary (hero), an optional condition word and
icon from a generic code lexicon, and secondaries as KV. Plans (CONTRACTS.md 5.3):
  W1: R1 hero+icon (+1 secondary)   R2 + KV rows (>=2 secondaries)  R3 strip (series)
  W2: R1 hero+icon+word+1 secondary R2 + KV 2x2 (>=2)               R3 strip | KV 2x4 (>=7)
  W3/W4: R1 hero | KV 2x2           R2 + strip | all KV (>=3)       R3 strip + KV (series)
  W5/W6: R1 hero | KV 2x3 | strip (series | >=4)                    R2/R3 series
"""
from __future__ import annotations

from ..base import (
    BaseForm,
    all_plans,
    fact_rc,
    hero,
    kv_cells,
    kv_line_rows,
    labelled_rc,
    line_h,
    part_plot,
    series_of,
)
from ..ctx import Canvas, LayoutCtx
from ..prov import lex_entry
from ..rec import R as RView
from ..rec import fact_ok, is_numeric
from ..types import Reject


def _plan(w, r):
    if w == "W1":
        return {1: "hero", 2: "stack", 3: "strip"}[r]
    if w == "W2":
        return {1: "hero", 2: "kv", 3: "strip"}[r]
    if w in ("W3", "W4"):
        return {1: "split", 2: "full", 3: "strip"}[r]
    return {1: "tri", 2: "strip", 3: "strip"}[r]


class Conditions(BaseForm):
    name = "conditions"
    variants = ("default",)
    slots = {"value": {"roles": ["measure"], "types": ["quantity", "number", "percent", "currency"], "required": True, "many": False},
             "code": {"roles": ["kind", "status"], "types": ["category", "number"], "required": False, "many": False},
             "secondary": {"roles": ["secondary", "value", "range_hi", "range_lo", "count"], "types": [], "required": True, "many": True}}
    record_form = False
    intent_of = "now"
    PLANS = {"default": all_plans(_plan)}

    def match(self, rec, prof):
        if rec.kind != "measure":
            return []
        R = RView(rec)
        v = R.first("measure")
        if v is None or not is_numeric(v):
            return []
        if R.first("reference", "delta", "delta_pct") is not None:
            return []            # a value with a change against a reference is a stat story
        code = next((f for f in rec.fields if f.unit == "wmo"), None)
        sec = [f.name for f in rec.fields if f.name != v.name and (code is None or f.name != code.name)
               and fact_ok(f, rec) and f.role not in ("as_of", "time", "date", "name", "unknown")
               and (is_numeric(f) or f.type in ("category", "text"))]
        seen, uniq = {v.label.lower()}, []
        for n_ in sec:                      # each label once: a repeated label is a near-duplicate reading
            lab = R.f[n_].label.lower()
            if lab not in seen:
                seen.add(lab)
                uniq.append(n_)
        sec = uniq
        if not sec:
            return []
        b = {"value": v.name, "secondary": sec[:8]}
        if code is not None:
            b["code"] = code.name
        s = series_of(rec)
        if s:
            b["series"] = s[0]
        return [{"variant": "default", "bindings": b, "params": {}}]

    def structural(self, cand, rec, prof, sclass, plan):
        n = len(cand.bindings.get("secondary") or [])
        S = bool(cand.bindings.get("series"))
        w, _r = sclass[:2], int(sclass[3])
        if plan == "strip":
            if S:
                return plan
            if w == "W2" and n >= 7:
                return "kv8"
            return Reject("needs_series")
        if plan in ("split",) and n < 2 and not S:
            return "single"
        if plan in ("stack", "kv") and n < 2 and not S:
            return Reject("needs_series")
        if plan == "full" and n < 3 and not S:
            return Reject("needs_series")
        if plan == "tri" and not (S or n >= 4):
            return Reject("needs_series")
        return plan

    def covers(self, cand, prof, sclass):
        c = ["now", "height_value"]
        if cand.bindings.get("series") and (int(sclass[3]) >= 2 or sclass[:2] in ("W5", "W6")):
            c.append("trend")
        return c

    def describes(self, cand, rec):
        R = RView(rec)
        return f"{R.f[cand.bindings['value']].label} large, with {len(cand.bindings['secondary'])} more readings"

    def summary(self, cand, rec, now):
        R = RView(rec)
        if not rec.rows:
            return "No reading"
        parts = [f"{R.f[f].label} {R.text(0, f)}" for f in [cand.bindings["value"]] + cand.bindings["secondary"]]
        return ", ".join(p for p in parts if p.strip())

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        plan = self.structural(ctx.cand, ctx.rec, ctx.prof, ctx.span.sclass, plan)
        if isinstance(plan, Reject):
            plan = "hero"
        cv.d.plan = plan
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        drop = ctx.rung.get("drop", 0)
        secs = [f for f in b["secondary"] if R.cell(0, f) is not None]
        items = [(["label", f], fact_rc(R, f), "data") for f in secs]
        code = b.get("code")
        ent = lex_entry("wmo", R.cell(0, code)) if code else None
        icon = ent.get("icon") if ent else None
        word_rc = ["lex", "wmo", code, 0] if ent and ent.get("word") else None
        ser = series_of(ctx.rec) if b.get("series") else None

        if plan == "single":
            m = cv.mark()
            hb = hero(cv, ["c", 0], y0, ["cell", b["value"], 0, {}], W, anchor="middle")
            sub = [x for x in ([word_rc] if word_rc else []) + [labelled_rc(R, f) for f in secs[:2]]]
            sb = cv.text(["c", 0], (hb.bottom if hb else y0) + 1, ["join", " · ", sub], "sub", src="data",
                         max_w=W, anchor="middle")
            cv.balance(m, y0, sb.bottom if sb else (hb.bottom if hb else y0), y0, y1)
            return
        colw = W if plan not in ("split", "tri") else (W * 0.46 if plan == "split" else W * 0.3)
        icon_sz = 28 if icon else 0
        hb = hero(cv, ["l", 0], y0, ["cell", b["value"], 0, {}], colw - (icon_sz + 8 if icon else 0))
        y = hb.bottom if hb else y0
        if icon and hb:
            cv.icon(["l", hb.x1 + 10 + icon_sz / 2], hb.top + (hb.bottom - hb.top) / 2, icon_sz, icon, "muted")
        # sub line: condition word and/or the first secondary
        sub = []
        if word_rc and plan != "hero" or (word_rc and ctx.wc != "W1"):
            sub.append(word_rc)
        if not word_rc:
            sub.append(["label", b["value"]])      # a bare number is always named
        first_in_sub = plan in ("hero",) or (plan in ("split", "tri") and not word_rc)
        if first_in_sub and secs:
            sub.append(labelled_rc(R, secs[0]))
        if sub and len(sub) == 2 and not cv.fits(cv.run(["join", " · ", sub]), "sub", colw) and \
                all(cv.fits(cv.run(x), "sub", colw) for x in sub) and y1 - y >= 2 * line_h("sub") + 2:
            # both parts fit on their own lines: stack them rather than drop the reading
            for x in sub:
                sb = cv.text(["l", 0], y + 1, x, "sub", src="data", max_w=colw)
                if sb:
                    y = sb.bottom
            sub = []
            first_in_sub = first_in_sub and bool(secs)
        if sub:
            opts = [sub] + [[x] for x in sub]          # drop parts until the line fits (never squash)
            pick = next((o for o in opts if cv.fits(cv.run(["join", " · ", o]), "sub", colw)), None)
            if pick is not None:
                if first_in_sub and secs and labelled_rc(R, secs[0]) not in pick:
                    first_in_sub = False
                sb = cv.text(["l", 0], y + 1, ["join", " · ", pick], "sub", src="data", max_w=colw)
                if sb:
                    y = sb.bottom
            else:
                first_in_sub = False
        rest = items[1:] if first_in_sub else items
        if plan == "hero":
            return
        if plan == "stack":
            n = max(0, min(len(rest), 3 if y1 - y < 120 else 6) - drop)
            y = kv_line_rows(cv, ["l", 0], ["r", 0], y + 10, rest[:n], width=W)
            if ser and y1 - y > 70 and drop < 2:
                part_plot(ctx, cv, ser, 0.0, 1.0, y + 10, y1)
            return
        if plan in ("kv", "kv8"):
            rows = 2 if plan == "kv" else 4
            n = max(0, min(len(rest), 2 * rows) - drop)
            y = kv_cells(cv, [["f", 0], ["f", 0.5]], y + 12, rest[:n], col_w=W / 2 - 8, rows=rows, row_gap=8)
            if ser and y1 - y > 80 and drop < 2:
                part_plot(ctx, cv, ser, 0.0, 1.0, y + 12, y1, labels="minmax")
            return
        if plan == "split":
            r0 = 0.52
            n = max(0, min(len(rest), 4) - drop)
            kv_cells(cv, [["f", r0], ["f", r0 + (1 - r0) / 2]], y0 + 2, rest[:n], col_w=W * (1 - r0) / 2 - 8,
                     rows=2, row_gap=6)
            return
        if plan == "full":
            ncol = 4 if ctx.wc == "W4" else 2
            cell_h = line_h("meta") + line_h("row-strong") + 8
            room = y1 - y - 12
            if ser and drop < 2:
                room -= 120
            rows = max(1, min(int(room // cell_h), -(-len(rest) // ncol)))
            n = max(0, min(len(rest), ncol * rows) - drop)
            y = kv_cells(cv, [["f", i / ncol] for i in range(ncol)], y + 12, rest[:n], col_w=W / ncol - 10,
                         rows=rows, row_gap=8)
            if ser and drop < 2:
                part_plot(ctx, cv, ser, 0.0, 1.0, y + 12, y1, labels="minmax")
            return
        if plan == "tri":
            kx1 = 0.64 if ser else 1.0
            ncol = 2 if ser else 3
            n = max(0, min(len(rest), ncol * 2) - drop)
            kv_cells(cv, [["f", 0.32 + i * (kx1 - 0.32) / ncol] for i in range(ncol)], y0 + 2, rest[:n],
                     col_w=W * (kx1 - 0.32) / ncol - 10, rows=2, row_gap=6)
            if ser:
                part_plot(ctx, cv, ser, 0.68, 1.0, y0, y1, labels="minmax")
            return
        if plan == "strip":
            ncol = {"W1": 1, "W2": 2, "W3": 2, "W4": 4}.get(ctx.wc, 4)
            n = max(0, min(len(rest), ncol * (2 if ctx.rows == 3 else 1)) - drop)
            if n:
                if ncol == 1:
                    y = kv_line_rows(cv, ["l", 0], ["r", 0], y + 10, rest[:n], width=W)
                else:
                    y = kv_cells(cv, [["f", i / ncol] for i in range(ncol)], y + 12, rest[:n], col_w=W / ncol - 10,
                                 rows=2, row_gap=8)
            part_plot(ctx, cv, ser, 0.0, 1.0, y + 12, y1, labels="minmax", min_share=ctx.rows >= 3)
            return


FORM = Conditions()
