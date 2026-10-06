"""`table`: multi-column ranked records. Columns are ordered by role priority and dropped
by width (W2 2, W3 3, W4 4-5, W5 6-7, W6 <=8); W1 is rejected (too_narrow; the siblings
are bars / ranked_list). Rows come from the height; overflow ends in "+N more".
Variants: `plain`, `grouped` (row_meta groups get a group line).
"""
from __future__ import annotations

import itertools

from ..base import BaseForm, all_plans, calm, fact_rc, line_h, more_line, rows_capacity
from ..ctx import Canvas, LayoutCtx
from ..rec import R as RView
from ..rec import is_displayable, is_numeric

PRIORITY = ["rank", "name", "value", "measure", "delta_pct", "delta", "status", "severity", "kind", "count",
            "secondary", "share", "time", "date", "meta", "group"]
MAXCOLS = {"W2": 2, "W3": 3, "W4": 5, "W5": 7, "W6": 8}


def _plan(w, r):
    return None if w == "W1" else f"table{r}"


class Table(BaseForm):
    name = "table"
    variants = ("plain", "grouped")
    slots = {"columns": {"roles": PRIORITY, "types": [], "required": True, "many": True},
             "name": {"roles": ["name", "meta"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "compare"
    PLANS = {v: all_plans(_plan) for v in variants}
    REJECTS = {v: {f"W1R{r}": "too_narrow" for r in (1, 2, 3)} for v in variants}

    def match(self, rec, prof):
        if rec.kind not in ("records", "events"):
            return []
        cols = [f for f in rec.fields if is_displayable(f) and f.role in PRIORITY and f.type not in ("lat", "lon")]
        if not any(f.role == "name" for f in cols):
            # structural fallback: the first text column names the rows
            first_text = next((f for f in cols if f.type in ("text", "category")), None)
            if first_text is None:
                return []
            cols.remove(first_text)
            cols.insert(0, first_text)
            self_name = first_text.name
        else:
            self_name = next(f.name for f in cols if f.role == "name")
        if len(cols) < 3:
            return []
        cols.sort(key=lambda f: PRIORITY.index("name") if f.name == self_name else PRIORITY.index(f.role))
        grouped = rec.row_meta is not None and any(m.group for m in rec.row_meta)
        return [{"variant": "grouped" if grouped else "plain",
                 "bindings": {"columns": [f.name for f in cols][:10], "name": self_name}, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["rank", "compare", "count", "now"]

    def describes(self, cand, rec):
        R = RView(rec)
        return "Table with columns " + ", ".join(R.f[c].label for c in cand.bindings["columns"][:5])

    def summary(self, cand, rec, now):
        R = RView(rec)
        c = cand.bindings["columns"]
        return f"{len(rec.rows)} rows: " + "; ".join(" ".join(R.text(i, f) for f in c[:3]) for i in range(min(5, len(rec.rows))))

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, W = ctx.R, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        n = len(R.rows)
        if n == 0:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        drop = ctx.rung.get("drop", 0)
        allc = list(ctx.cand.bindings["columns"])
        budget = max(2, MAXCOLS[ctx.wc] - drop)
        # the reading outranks a rank column when both cannot fit: the rows' order already shows the rank
        # (round 3: a narrow table keeps the number that answers)
        # only when the rows are NOT in that rank's order (re-sorted by the reading): then the rank misleads
        rk = next((c for c in allc[:budget] if R.f[c].role == "rank"), None)
        if rk is not None and len(allc) > budget and ((R.rec.flags or {}).get("order") or {}).get("changed") and \
                any(R.f[c].role in ("value", "measure") for c in allc[budget:]):
            xs = [R.num(i, rk) for i in range(n) if R.num(i, rk) is not None]
            if len(xs) >= 3 and not (all(a <= b for a, b in itertools.pairwise(xs)) or all(a >= b for a, b in itertools.pairwise(xs))):
                allc = [c for c in allc if c != rk]
        cols = allc[:budget]
        row_h = 24 if ctx.rows == 1 else 26
        head_h = line_h("meta") + 6
        k, hidden = rows_capacity(y1 - y0 - head_h, row_h, n, line_h("meta") + 4)
        k = max(1, k)
        hidden = n - k
        rows = list(range(n))[:k]
        gap = 14
        # measure every non-name column at the floor; the name column takes the rest
        def width(c):
            vw = max([cv.measure(cv.run(fact_rc(R, c, i)), "sub") for i in rows] or [0])
            hw = cv.measure(R.f[c].label, "meta")
            # a long header ellipsizes over its values, never pushes the column (and the reading) out
            return max(vw, min(hw, vw * 1.3 + 12)) * 1.04 + 2
        name = ctx.cand.bindings.get("name") or next((c for c in cols if R.f[c].role == "name"), cols[0])
        widths = {c: width(c) for c in cols if c != name}
        if name not in cols:
            cols.insert(0, name)
        while cols and sum(widths.get(c, 0) + gap for c in cols if c != name) > W - 80:
            victim = [c for c in cols if c != name][-1]
            cols.remove(victim)
            widths.pop(victim, None)
        # layout: columns before the name hug the left (rank), after it hug the right
        before = []
        for c in cols:
            if c == name:
                break
            before.append(c)
        after = [c for c in cols if c not in before and c != name]
        x = 0.0
        pos = {}
        for c in before:
            pos[c] = ("l", x, widths[c])
            x += widths[c] + gap
        name_x = x
        r = 0.0
        for c in reversed(after):
            pos[c] = ("r", r, widths[c])
            r += widths[c] + gap
        name_w = max(40, W - name_x - r)
        # header
        for c, (k_, off, w) in pos.items():
            is_numeric(R.f[c])
            if k_ == "l":
                cv.text(["l", off], y0, ["label", c], "meta", src="key", max_w=w)
            else:
                cv.text(["r", off], y0, ["label", c], "meta", src="key", max_w=w, anchor="end")
        cv.text(["l", name_x], y0, ["label", name], "meta", src="key", max_w=name_w)
        cv.line(["l", 0], y0 + head_h - 3, ["r", 0], y0 + head_h - 3, "border", w=1)
        y = y0 + head_h
        for i in rows:
            ty_ = y + (row_h - 19) / 2
            for c, (k_, off, w) in pos.items():
                if R.cell(i, c) is None:
                    continue
                anchor = "start" if k_ == "l" else "end"
                cv.text([k_, off], ty_, fact_rc(R, c, i), "sub", src="data", max_w=w, anchor=anchor,
                        tok="muted" if R.f[c].role in ("rank",) else "text")
            cv.text(["l", name_x], ty_, ["raw", name, i], "item", src="data", max_w=name_w)
            y += row_h
        if hidden:
            more_line(cv, ["l", name_x], y + 2, hidden, name_w)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if n == 1 else ("many" if hidden else "few")


FORM = Table()
