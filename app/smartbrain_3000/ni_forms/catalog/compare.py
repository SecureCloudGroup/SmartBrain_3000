"""`compare`: 2-12 named items x one primary metric in the user's order. Columns by
width: W1 name + value; W2 + tri; W3 + delta; W4 + shared-scale bar; W5/W6 + secondary.
Rows by height (3 / 7 / 11 desktop, 3 / 6 / 10 phone). A requested item the source did
not return gets its own row ("TWTR · not returned"). Every span is valid.
"""
from __future__ import annotations

from ..base import BaseForm, all_plans, calm, line_h, more_line, rows_capacity
from ..ctx import SP, Canvas, LayoutCtx
from ..rec import R as RView
from ..rec import is_numeric


class Compare(BaseForm):
    name = "compare"
    variants = ("default",)
    slots = {"name": {"roles": ["name"], "types": [], "required": True, "many": False},
             "value": {"roles": ["value", "measure"], "types": [], "required": True, "many": False},
             "delta": {"roles": ["delta_pct", "delta"], "types": [], "required": False, "many": False},
             "secondary": {"roles": ["secondary"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "compare"
    PLANS = {"default": all_plans(lambda w, r: f"cmp{r}")}

    def match(self, rec, prof):
        if rec.kind != "records":
            return []
        R = RView(rec)
        nm = R.first("name")
        v = next((f for f in rec.fields if f.role in ("value", "measure") and is_numeric(f)), None)
        if nm is None or v is None:
            return []
        b = {"name": nm.name, "value": v.name}
        d = R.first("delta_pct", "delta")
        if d is not None:
            b["delta"] = d.name
        s = next((f for f in rec.fields if f.role == "secondary" and is_numeric(f)), None)
        if s is not None:
            b["secondary"] = s.name
        return [{"variant": "default", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["compare", "now", "rank"] + (["trend"] if cand.bindings.get("delta") else [])

    def describes(self, cand, rec):
        R = RView(rec)
        return f"Side-by-side {R.f[cand.bindings['value']].label} for each item, in your order"

    def summary(self, cand, rec, now):
        R = RView(rec)
        b = cand.bindings
        return "; ".join(f"{R.text(i, b['name'])} {R.text(i, b['value'])}" for i in range(len(rec.rows))) or "No items"

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        n = len(R.rows)
        missing = list(ctx.rec.flags.get("missing_items") or [])
        if n == 0 and not missing:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        wc = ctx.wc
        drop = ctx.rung.get("drop", 0)
        row_h = 28 if ctx.rows == 1 else 30
        total = n + len(missing)
        k, hidden = rows_capacity(y1 - y0, row_h, total, line_h("meta") + 4)
        k = max(1, k - drop)
        hidden = total - k
        vals = [R.num(i, b["value"]) for i in range(n)]
        vs = [cv.run(["cell", b["value"], i, {}]) for i in range(n)]
        vw = max([cv.measure(s, "row-strong", 14) for s in vs] or [0]) * 1.04 + 2
        d = b.get("delta")
        show_tri = d is not None and wc != "W1"
        show_d = d is not None and wc in ("W3", "W4", "W5", "W6")
        ds = [cv.run(["cell", d, i, {"sign": True}]) if d else "" for i in range(n)]
        dw = max([cv.measure(s, "delta") for s in ds] or [0]) * 1.04 + 2 if show_d else 0
        sec = b.get("secondary") if wc in ("W5", "W6") else None
        ss = [cv.run(["cell", sec, i, {}]) if sec else "" for i in range(n)]
        sw = max([cv.measure(s, "sub") for s in ss] or [0]) * 1.04 + 2 if sec else 0
        show_bar = wc in ("W4", "W5", "W6") and all(v is None or v >= 0 for v in vals)
        right = 0
        cols = {}
        if sec:
            cols["sec"] = right
            right += sw + 16
        if show_d:
            cols["d"] = right
            right += dw + (SP["tri_size"] + SP["tri_gap"]) + 12
        elif show_tri:
            cols["tri"] = right
            right += SP["tri_size"] + 10
        cols["v"] = right
        right += vw + 12
        names = [cv.run(["raw", b["name"], i]) for i in range(n)] + [str(m) for m in missing]
        nw = min(max([cv.measure(s, "item") for s in names] or [0]) * 1.04 + 2, W * 0.4 if show_bar else W - right)
        nw_full = max([cv.measure(s, "item") for s in names] or [0]) * 1.04 + 2
        stacked = (nw_full + 12 + vw > W or vw > W * 0.5) and not show_bar and not show_d
        if stacked:
            row_h = 44
            k, hidden = rows_capacity(y1 - y0, row_h, total, line_h("meta") + 4)
            k = max(1, k - drop)
            hidden = total - k
        bar_x0, bar_x1 = nw + 12, W - right
        vmax = max([v for v in vals if v is not None] or [1]) or 1
        y = y0
        for i in range(min(k, n)):
            ty_ = y + (row_h - 19) / 2 - 3
            if stacked:
                cv.text(["l", 0], y, ["raw", b["name"], i], "item", src="data", max_w=W)
                if vals[i] is not None:
                    px = next((p for p in (14, 13, 12, 11) if cv.measure(vs[i], "row-strong", p) * 1.04 + 1 <= W), 11)
                    cv.text(["l", 0], y + 19, ["cell", b["value"], i, {}], "row-strong", src="data", max_w=W, px=px,
                            s=vs[i])
                y += row_h
                continue
            cv.text(["l", 0], ty_, ["raw", b["name"], i], "item", src="data", max_w=max(nw, 24))
            if vals[i] is not None:
                cv.text(["r", cols["v"]], ty_, ["cell", b["value"], i, {}], "row-strong", src="data", max_w=vw,
                        anchor="end", px=14, s=vs[i])
            dv = R.num(i, d) if d else None
            if dv is not None and (show_d or show_tri):
                tok = "viz-pos" if dv > 0 else "viz-neg" if dv < 0 else "muted"
                if show_d:
                    cv.text(["r", cols["d"]], ty_, ["cell", d, i, {"sign": True}], "delta", src="data", max_w=dw,
                            anchor="end", tok=tok, s=ds[i])
                    tx = cols["d"] + cv.measure(ds[i], "delta") + SP["tri_gap"] + SP["tri_size"] / 2
                else:
                    tx = cols["tri"] + SP["tri_size"] / 2
                if dv != 0:
                    cv.tri(["r", tx], ty_ + 10, SP["tri_size"], "up" if dv > 0 else "down", tok)
            if sec and ss[i]:
                cv.text(["r", cols["sec"]], ty_, ["cell", sec, i, {}], "sub", src="data", max_w=sw, anchor="end", s=ss[i])
            if show_bar and vals[i] is not None and bar_x1 - bar_x0 > 40:
                f1 = vals[i] / vmax
                bx1 = bar_x0 + (bar_x1 - bar_x0) * f1
                cv.rect(["l", bar_x0], ["l", max(bx1, bar_x0 + 2)], y + row_h / 2 - 6, y + row_h / 2 + 2, "viz-line", r=2)
            y += row_h
        for m in missing[:max(0, k - n)]:
            cv.text(["l", 0], y + (row_h - 19) / 2 - 3, ["tpl", "not_returned", {"name": ["ask"]}] if False else
                    ["tpl", "not_returned", {"name": m}], "item", src="code", max_w=W, tok="muted")
            y += row_h
        if hidden > 0:
            more_line(cv, ["l", 0], y + 2, hidden, W)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if n == 1 else ("many" if hidden else "few")


FORM = Compare()
