"""`progress` (provisional: few corpus instances): value / goal bars. The ratio is
derived by code, never taken from a field. Over-goal is drawn as overflow in viz-neg
with a tri and a signed "over by". Bars by rows: 1-2 / 5 / 9. Every span valid.
"""
from __future__ import annotations

from ..base import BaseForm, all_plans, calm, line_h, more_line, rows_capacity
from ..ctx import Canvas, LayoutCtx
from ..rec import R as RView
from ..rec import is_numeric


class Progress(BaseForm):
    name = "progress"
    variants = ("default",)
    slots = {"value": {"roles": ["value", "progress", "measure", "count"], "types": [], "required": True, "many": False},
             "goal": {"roles": ["goal"], "types": [], "required": True, "many": False},
             "name": {"roles": ["name"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "monitor"
    PLANS = {"default": all_plans(lambda w, r: f"prog{r}")}

    def match(self, rec, prof):
        R = RView(rec)
        g = R.first("goal")
        v = next((f for f in rec.fields if f.role in ("value", "progress", "measure", "count") and is_numeric(f)), None)
        if g is None or v is None:
            return []
        b = {"value": v.name, "goal": g.name}
        nm = R.first("name")
        if nm is not None:
            b["name"] = nm.name
        return [{"variant": "default", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["now", "count", "threshold", "height_value"]

    def describes(self, cand, rec):
        return "Progress bars of value against goal"

    def summary(self, cand, rec, now):
        R = RView(rec)
        b = cand.bindings
        return "; ".join(f"{R.text(i, b['name']) if b.get('name') else ''} {R.text(i, b['value'])} of {R.text(i, b['goal'])}"
                         for i in range(len(rec.rows))) or "No items"

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        idx = [i for i in range(len(R.rows)) if R.num(i, b["value"]) is not None and R.num(i, b["goal"])]
        if not idx:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        row_h = 50 if ctx.rows == 1 else 56
        k, hidden = rows_capacity(y1 - y0, row_h, len(idx), line_h("meta") + 4)
        k = max(1, k - ctx.rung.get("drop", 0))
        hidden = len(idx) - k
        y = y0
        for i in idx[:k]:
            v, g = R.num(i, b["value"]), R.num(i, b["goal"])
            over = v > g
            pct_rc = ["d", "ratio", [b["value"], i, b["goal"], i], {"dec": 0}]
            pw = cv.measure(cv.run(pct_rc), "row-strong", 14) * 1.04 + 2
            cv.text(["r", 0], y, pct_rc, "row-strong", src="data", max_w=pw, anchor="end", px=14,
                    tok="viz-neg" if over else "text")
            if b.get("name"):
                cv.text(["l", 0], y, ["raw", b["name"], i], "item", src="data", max_w=W - pw - 10)
            by = y + 25
            th = 6
            frac = min(v / g, 1.0)
            cv.rect(["f", 0], ["f", 1], by - th / 2, by + th / 2, "viz-track", r=th / 2)
            cv.rect(["f", 0], ["f", max(frac, 0.01)], by - th / 2, by + th / 2, "viz-line", r=th / 2)
            if over:
                # overflow: the excess is drawn in viz-neg and named with a sign (never colour alone)
                cv.rect(["f", max(0.0, 1 - min((v - g) / g, 1.0))], ["f", 1], by - th / 2, by + th / 2, "viz-neg", r=th / 2)
            cv.text(["l", 0], by + 7, ["join", " ", [["cell", b["value"], i, {}],
                                                     ["tpl", "of_goal", {"goal": ["cell", b["goal"], i, {}]}]]],
                    "meta", src="data", max_w=W)
            y += row_h
        if hidden:
            more_line(cv, ["l", 0], y, hidden, W)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if len(idx) == 1 else ("many" if hidden else "few")


FORM = Progress()
