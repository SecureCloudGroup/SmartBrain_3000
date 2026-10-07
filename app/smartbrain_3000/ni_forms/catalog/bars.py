"""`bars`: one metric across categories. `ranked` sorts by value, `share` shows parts of a
whole (a single 100% bar + top-2 legend at W1R1/W2R1; the tail folds into "Other"),
`cumulative` keeps source order and is never stacked. Aggregate rows are excluded.
Negatives run left from a zero baseline in the diverging tokens, with a sign. Rows by
height (3 / 7 / 11). Every span is valid.
"""
from __future__ import annotations

from ..base import BaseForm, all_plans, calm, line_h, more_line, rows_capacity
from ..ctx import Canvas, LayoutCtx
from ..rec import R as RView
from ..rec import is_numeric


class Bars(BaseForm):
    name = "bars"
    variants = ("ranked", "share", "cumulative")
    slots = {"name": {"roles": ["name", "group", "kind"], "types": [], "required": True, "many": False},
             "value": {"roles": ["value", "measure", "share", "count"], "types": [], "required": True, "many": False},
             "label": {"roles": ["share"], "types": ["percent"], "required": False, "many": False}}
    record_form = True
    intent_of = "compare"
    PLANS = {v: all_plans(lambda w, r: f"bars{r}") for v in variants}

    def match(self, rec, prof):
        if rec.kind != "records":
            return []
        R = RView(rec)
        nm = R.first("name", "group", "kind")
        v = next((f for f in rec.fields if f.role in ("value", "measure", "count") and is_numeric(f)), None)
        sh = next((f for f in rec.fields if f.role == "share" and is_numeric(f)), None)
        if nm is None or (v is None and sh is None):
            return []
        b = {"name": nm.name, "value": (v or sh).name}
        if sh is not None:
            b["label"] = sh.name
        if any(f.cumulative for f in rec.fields):
            var = "cumulative"
        elif "percent_of_whole" in (prof.signatures or []) or sh is not None:
            var = "share"
        else:
            var = "ranked"
        return [{"variant": var, "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["rank", "compare", "count", "extreme_high"]

    def describes(self, cand, rec):
        R = RView(rec)
        return f"Bars of {R.f[cand.bindings['value']].label} per item" + \
            (", as shares of the whole" if cand.variant == "share" else ", largest first" if cand.variant == "ranked" else "")

    def summary(self, cand, rec, now):
        R = RView(rec)
        b = cand.bindings
        rows = self._rows(R, cand)
        return "; ".join(f"{R.text(i, b['name'])} {R.text(i, b['value'])}" for i in rows[:6]) or "No items"

    def _rows(self, R, cand):
        b = cand.bindings
        rm = R.rec.row_meta
        idx = [i for i in range(len(R.rows)) if R.num(i, b["value"]) is not None and not (rm and rm[i].aggregate)]
        if cand.variant in ("ranked", "share"):
            idx.sort(key=lambda i: -R.num(i, b["value"]))
        return idx

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        idx = self._rows(R, ctx.cand)
        cv.d.meta["rows_shown_agg"] = idx
        if not idx:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        vf = b["value"]
        lab = b.get("label") or vf
        if ctx.cand.variant == "share" and ctx.rows == 1 and ctx.wc in ("W1", "W2") and len(idx) > 1:
            return self._stack(ctx, cv, idx, vf, lab)
        drop = ctx.rung.get("drop", 0)
        vs_all = {i: cv.run(["cell", lab, i, {}]) for i in idx[:14]}
        vw_all = max(cv.measure(s, "sub") for s in vs_all.values()) * 1.04 + 2
        nw_all = max(cv.measure(cv.run(["raw", b["name"], i]), "item") for i in idx[:14]) * 1.04 + 2
        stacked = ctx.wc == "W1" or (W - min(nw_all, W * 0.38) - vw_all - 22) < 80
        row_h = (36 if ctx.rows == 1 else 40) if stacked else (26 if ctx.rows == 1 else 30)
        fold = ctx.cand.variant == "share"
        k, hidden = rows_capacity(y1 - y0, row_h, len(idx), row_h if fold else line_h("meta") + 4)
        k = max(1, k - drop)
        hidden = len(idx) - k
        shown = idx[:k]
        vals = [R.num(i, vf) for i in shown]
        lo, hi = min(0, min(vals)), max(0, max(vals))
        span = (hi - lo) or 1
        vs = [vs_all.get(i) or cv.run(["cell", lab, i, {}]) for i in shown]
        vw = max(cv.measure(s, "sub") for s in vs) * 1.04 + 2
        nw = 0 if stacked else min(nw_all, W * 0.38)
        bx0 = nw + (12 if nw else 0)
        bx1 = W if stacked else W - vw - 10
        z = bx0 + (bx1 - bx0) * (-lo / span)
        y = y0
        for i, v, vs_ in zip(shown, vals, vs):
            if stacked:
                vpx = next((p for p in (14, 13, 12, 11) if cv.measure(vs_, "sub", p) * 1.04 + 1 <= W * 0.62), 11)
                vww = cv.measure(vs_, "sub", vpx) * 1.04 + 2
                cv.text(["r", 0], y, ["cell", lab, i, {}], "sub", src="data", max_w=vww, anchor="end", s=vs_,
                        tok="text", px=vpx)
                if W - vww - 8 >= 30:
                    cv.text(["l", 0], y, ["raw", b["name"], i], "item", src="data", max_w=W - vww - 8)
                by = y + 25
            else:
                cv.text(["l", 0], y + (row_h - 19) / 2 - 3, ["raw", b["name"], i], "item", src="data", max_w=nw)
                by = y + row_h / 2 - 4
                cv.text(["r", 0], by - 10, ["cell", lab, i, {}], "sub", src="data", max_w=vw, anchor="end", s=vs_,
                        tok="text")
            x1 = z + (bx1 - bx0) * (v / span)
            a, c = sorted((z, x1))
            tok = "viz-div-neg" if v < 0 else "viz-line"
            cv.rect(["l", a], ["l", max(c, a + 2)], by - (3 if stacked else 4), by + (3 if stacked else 4), tok, r=2)
            y += row_h
        if lo < 0 and not stacked:
            cv.line(["l", z], y0 - 2, ["l", z], y - 4, "viz-axis", w=1)
        if hidden > 0:
            if fold:
                rest = idx[k:]
                yy = y if stacked else y + (row_h - 19) / 2 - 3
                rc = ["d", "agg", [lab, "sum", rest], {"field": lab}]
                ob = cv.text(["r", 0], yy, rc, "sub", src="data", max_w=vw + 30, anchor="end")
                cv.text(["l", 0], yy, ["tpl", "other", {}], "item", src="code",
                        max_w=max(20, W - (ob.w if ob else 0) - 8) if stacked else max(nw, 40), tok="muted")
            else:
                more_line(cv, ["l", 0], y + 2, hidden, W)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if len(idx) == 1 else ("many" if hidden else "few")

    def _stack(self, ctx, cv, idx, vf, lab):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0 = ctx.body["y0"]
        tot = sum(R.num(i, vf) for i in idx if R.num(i, vf) > 0) or 1
        x = 0.0
        top = idx[:3]
        for j, i in enumerate(top):
            f = max(R.num(i, vf), 0) / tot
            cv.rect(["f", x], ["f", min(1.0, x + f)], y0 + 4, y0 + 18, f"viz-cat-{j + 1}")
            x += f
        cv.rect(["f", x], ["f", 1.0], y0 + 4, y0 + 18, "viz-cat-other")
        y = y0 + 28
        for j, i in enumerate(idx[:2]):
            cv.rect(["l", 0], ["l", 10], y + 5, y + 15, f"viz-cat-{j + 1}", r=2)
            cv.text(["l", 16], y, ["join", " ", [["raw", b["name"], i], ["cell", lab, i, {}]]], "sub", src="data",
                    max_w=W - 16, tok="text")
            y += 21
        cv.d.state = "many"


FORM = Bars()
