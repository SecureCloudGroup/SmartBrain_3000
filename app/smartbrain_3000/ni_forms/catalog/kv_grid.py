"""`kv_grid`: peers without a primary (rates, fees, ratings, world clocks, nutrition).
Cells are label (12 muted) over value (16/600). Capacity: cols W1 1, W2/W3 2, W4 3,
W5 4, W6 6; rows 2 / 5 / 8 by span rows; overflow ends in a "+N more" cell; few
cells spread across the width and centre vertically. Every span is valid.
"""
from __future__ import annotations

from ..base import BaseForm, all_plans, calm, fact_rc, kv_cells, line_h
from ..ctx import Canvas, LayoutCtx
from ..rec import R as RView
from ..rec import is_context, is_displayable, is_numeric

COLS = {"W1": 1, "W2": 2, "W3": 2, "W4": 3, "W5": 4, "W6": 6}
ROWS = {1: 2, 2: 5, 3: 8}


class KvGrid(BaseForm):
    name = "kv_grid"
    variants = ("fields", "records")
    slots = {"fields": {"roles": ["measure", "value", "secondary", "count", "reference", "range_lo", "range_hi"],
                        "types": [], "required": False, "many": True},
             "name": {"roles": ["name"], "types": [], "required": False, "many": False},
             "value": {"roles": ["value", "measure"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "now"
    PLANS = {v: all_plans(lambda w, r: f"grid{r}") for v in variants}

    def match(self, rec, prof):
        R = RView(rec)
        out = []
        if rec.kind == "measure":
            fs = [f.name for f in rec.fields if is_displayable(f) and not is_context(f, rec)
                  and f.role not in ("as_of", "unknown", "ignore")
                  and (is_numeric(f) or f.type in ("text", "category", "datetime", "date", "bool"))]
            # the reading first, then the other readings, then states, then names and details (round 3:
            # the grid leads with what answers, never with a record's descriptive attributes)
            rank = {"measure": 0, "value": 0, "count": 1, "secondary": 1, "reference": 1, "range_lo": 1,
                    "range_hi": 1, "name": 2, "status": 3, "severity": 3, "kind": 4}
            fs.sort(key=lambda nm: rank.get(R.f[nm].role, 5))
            if len(fs) >= 2:
                out.append({"variant": "fields", "bindings": {"fields": fs[:24]}, "params": {}})
        if rec.kind in ("records",):
            nm, v = R.first("name"), next((f for f in rec.fields if f.role in ("value", "measure")), None)
            if nm and v and len(rec.fields) <= 4:
                out.append({"variant": "records", "bindings": {"name": nm.name, "value": v.name}, "params": {}})
        return out

    def covers(self, cand, prof, sclass):
        return ["now", "compare", "height_value"]

    def describes(self, cand, rec):
        return "A grid of labelled values, all equal weight"

    def summary(self, cand, rec, now):
        R = RView(rec)
        return "; ".join(f"{lab}: {val}" for lab, val in self._pairs_text(R, cand)[:8]) or "No values"

    def _items(self, R, cand):
        b = cand.bindings
        if cand.variant == "fields":
            return [(["label", f], fact_rc(R, f, 0), "data") for f in b["fields"] if R.cell(0, f) is not None]
        return [(["raw", b["name"], i], ["cell", b["value"], i, {}], "data") for i in range(len(R.rows))
                if R.cell(i, b["value"]) is not None]

    def _pairs_text(self, R, cand):
        b = cand.bindings
        if cand.variant == "fields":
            return [(R.f[f].label, R.text(0, f)) for f in b["fields"] if R.rows]
        return [(R.text(i, b["name"]), R.text(i, b["value"])) for i in range(len(R.rows))]

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, W = ctx.R, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        items = self._items(R, ctx.cand)
        if not items:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        cols = COLS[ctx.wc]
        cell_h = line_h("meta") + line_h("row-strong")
        gap = 8 if ctx.rows == 1 else 12
        rows = max(1, min(ROWS[ctx.rows], int((y1 - y0 + gap) // (cell_h + gap))) - ctx.rung.get("drop", 0))
        cap = cols * rows
        n = len(items)
        shown = items if n <= cap else items[:cap - 1]
        hidden = n - len(shown)
        ncol = min(cols, len(shown) + (1 if hidden else 0))
        colx = [["f", i / ncol] for i in range(ncol)]
        kv_cells(cv, colx, y0, shown, col_w=W / ncol - 12, rows=rows, row_gap=gap)
        if hidden:
            r_, c_ = divmod(len(shown), ncol)
            cv.text(colx[c_], y0 + r_ * (cell_h + gap) + line_h("meta"),
                    ["tpl", "more", {"n": ["d", "count", [hidden]]}], "row", src="code", max_w=W / ncol - 12,
                    tok="muted", px=14)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if n == 1 else ("many" if hidden else "few")


FORM = KvGrid()
