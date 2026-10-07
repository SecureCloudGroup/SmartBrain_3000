"""`ranked_list`: text-primary records with an index, a title (1-2 measured lines) and a
meta line (host · counts), links in the hitmap (https only). Rows per span come from
measured heights at the bucket floor; overflow ends with "+N more". W5/W6 use two
columns. 0 rows = calm sentence.
"""
from __future__ import annotations

import itertools

from .. import text as TX
from ..base import BaseForm, all_plans, calm, line_h, more_line
from ..ctx import SP, Canvas, LayoutCtx
from ..lint import link_ok
from ..rec import R as RView
from ..rec import fact_ok, is_numeric


def _plan(w, r):
    return f"list{r}" + ("_2col" if w in ("W5", "W6") else "")


class RankedList(BaseForm):
    name = "ranked_list"
    variants = ("default",)
    slots = {"name": {"roles": ["name", "text_body"], "types": ["text"], "required": True, "many": False},
             "link": {"roles": ["link"], "types": ["url"], "required": False, "many": False},
             "time": {"roles": ["time"], "types": ["datetime", "date"], "required": False, "many": False},
             "meta": {"roles": ["count", "secondary", "meta", "value"], "types": [], "required": False, "many": True}}
    record_form = True
    intent_of = "read"
    PLANS = {"default": all_plans(_plan)}

    def match(self, rec, prof):
        if rec.kind not in ("records", "events"):
            return []
        R = RView(rec)
        nm = next((f for f in rec.fields if f.role == "name" and (f.type == "text" or (
            f.type == "category" and len({str(r[rec.fields.index(f)]) for r in rec.rows}) == len(rec.rows)))), None)
        if nm is None:
            return []
        b = {"name": nm.name}
        lk = R.first("link")
        if lk:
            b["link"] = lk.name
        def varies(f):
            i = rec.fields.index(f)
            return len(rec.rows) < 3 or len({r[i] for r in rec.rows if r[i] is not None}) > 1
        # a fact every row repeats says nothing about any one item (it is the list's context)
        ms = [f for f in rec.fields if f.name != nm.name and f.role in ("count", "secondary", "value", "meta")
              and (is_numeric(f) or f.type in ("text", "category")) and fact_ok(f, rec) and varies(f)]
        b["meta"] = [f.name for f in sorted(ms, key=lambda f: 0 if is_numeric(f) else 1)][:3]   # counts first
        num0 = next((rec.fields.index(f) for f in ms if is_numeric(f) and f.name in b["meta"]), None)
        if num0 is not None and len(rec.rows) >= 3:
            xs = [r[num0] for r in rec.rows if isinstance(r[num0], (int, float)) and not isinstance(r[num0], bool)]
            if len(xs) >= 3 and (all(a >= c for a, c in itertools.pairwise(xs)) or all(a <= c for a, c in itertools.pairwise(xs))):
                b["lead"] = "num"          # the list is ranked by that number: it leads each meta line
        tf = next((f for f in rec.fields if f.role == "time" and f.type in ("datetime", "date")), None)
        if tf is not None:
            b["time"] = tf.name            # when each item happened: a list of dated items shows its dates
            if (rec.flags.get("order") or {}).get("by") == tf.name:
                b["lead"] = "time"         # a list read newest first leads each meta line with the date
        return [{"variant": "default", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["read", "rank", "now"]

    def describes(self, cand, rec):
        return "Numbered list of titles with a short meta line and links"

    def summary(self, cand, rec, now):
        R = RView(rec)
        if not rec.rows:
            return "No items"
        return f"{len(rec.rows)} items. " + "; ".join(R.text(i, cand.bindings["name"]) for i in range(min(5, len(rec.rows))))

    def _meta_rc(self, R, b, i, W, cv=None):
        """host · counts · text meta; parts drop from the end until the line fits (never cut)."""
        parts, texts, nums = [], [], []
        for m in b.get("meta") or []:
            if R.cell(i, m) is None:
                continue
            f = R.f[m]
            if is_numeric(f):
                nums.append(["join", " ", [["cell", m, i, {"compact": True}], ["lower", ["label", m]]]])
            elif str(R.cell(i, m)).strip() != str(R.cell(i, b["name"]) or "").strip():
                texts.append(["raw", m, i])           # never the title again
        tm = [["cell", b["time"], i, {"short": True}]] if b.get("time") and R.cell(i, b["time"]) else []
        host = [["d", "host", [b["link"], i]]] if b.get("link") and R.cell(i, b["link"]) else []
        # the number a list is ranked by leads its meta line; otherwise when (and where) the item is from
        if b.get("lead") == "num":
            parts += nums + tm + host
        else:
            parts += tm + host + nums
        parts += texts
        if not parts:
            return None
        if cv is not None:
            while len(parts) > 1 and not cv.fits(cv.run(["join", " · ", parts]), "meta", W):
                parts = parts[:-1]
            if not cv.fits(cv.run(["join", " · ", parts]), "meta", W):
                return None
        return ["join", " · ", parts]

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        n = len(R.rows)
        if n == 0:
            calm(ctx, cv, [["tpl", "no_items", {}]])
            return
        drop = ctx.rung.get("drop", 0)
        if n == 1:                  # one item: it is the story (display size, up to 3 lines, meta)
            tb = cv.text(["l", 0], y0, ["raw", b["name"], 0], "display", src="data", max_w=W,
                         max_lines=min(3, 1 + ctx.rows))
            if b.get("link") and tb:
                u = R.cell(0, b["link"])
                if u and link_ok(str(u)):
                    cv.d.hitmap.append({"prim": tb.id, "label": "Open", "href": str(u)})
            mr = self._meta_rc(R, b, 0, W, cv)
            if mr and tb:
                cv.text(["l", 0], tb.bottom + 4, mr, "meta", src="data", max_w=W)
            cv.d.state = "one"
            return
        cols = 2 if plan.endswith("_2col") and n > {1: 3, 2: 6, 3: 10}[ctx.rows] else 1
        gap_c = 24
        cw = (W - gap_c * (cols - 1)) / cols
        idx_w = SP["list_index_w"] + (8 if n >= 10 else 0)
        tw = cw - idx_w
        title_lines = 1 if (cw >= 400 or ctx.rows == 1) else 2
        with_meta = ctx.rows >= 2 and drop < 2
        gap = SP["row_gap_phone"] if ctx.phone else SP["row_gap_desktop"]
        more_h = line_h("meta") + 4
        _, _wt, lh, _tn = (14, 500, 19, False)
        # measure each row at the floor, pack greedily per column
        heights = []
        for i in range(n):
            s = cv.run(["raw", b["name"], i])
            lines, _ = TX.break_lines(s, 14, 500, tw, title_lines)
            h = len(lines) * lh + (line_h("meta") + 2 if with_meta and self._meta_rc(R, b, i, W) else 0)
            heights.append(h)
        y1 - y0
        placed = []          # (row, col, top)
        col, y = 0, y0
        i = 0
        while i < n:
            h = heights[i]
            last_col = col == cols - 1
            need = h + (more_h if (last_col and i < n - 1) else 0)
            if y + need > y1 + 0.5:
                if not last_col:
                    col += 1
                    y = y0
                    continue
                cv.d.meta["slack"] = need + gap
                break
            placed.append((i, col, y))
            y += h + gap
            i += 1
        k = max(1, len(placed) - max(0, drop - 1))
        placed = placed[:k]
        hidden = n - k
        for i, c, top in placed:
            x = c * (cw + gap_c)
            cv.text(["l", x], top, ["d", "count", [i + 1]], "index", src="code", max_w=idx_w, tok="muted")
            tb = cv.text(["l", x + idx_w], top, ["raw", b["name"], i], "item", src="data", max_w=tw,
                         max_lines=title_lines)
            if b.get("link") and tb:
                u = R.cell(i, b["link"])
                if u and link_ok(str(u)):
                    cv.d.hitmap.append({"prim": tb.id, "label": "Open", "href": str(u)})
            if with_meta:
                mr = self._meta_rc(R, b, i, tw, cv)
                if mr and tb:
                    cv.text(["l", x + idx_w], tb.bottom + 2, mr, "meta", src="data", max_w=tw)
        if hidden:
            lastc = placed[-1][1]
            ybot = max(t + heights[i] for i, c, t in placed if c == lastc)
            more_line(cv, ["l", lastc * (cw + gap_c) + idx_w], ybot + 4, hidden, tw)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if n == 1 else ("many" if hidden else "few")


FORM = RankedList()
