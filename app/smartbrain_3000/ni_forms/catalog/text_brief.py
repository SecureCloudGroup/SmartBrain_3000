"""`text_brief`: a passage - the body clamped by span, an attribution or title line, and a
"Read more" link when a link role exists (https only). RTL runs are isolated and start at
the right edge. W1R1 rejected (too_short); every other span valid.
"""
from __future__ import annotations

from ..base import BaseForm, all_plans, calm, line_h
from ..ctx import Canvas, LayoutCtx
from ..lint import link_ok
from ..rec import R as RView


def _plan(w, r):
    return None if (w == "W1" and r == 1) else f"text{r}"


class TextBrief(BaseForm):
    name = "text_brief"
    variants = ("default",)
    slots = {"body": {"roles": ["text_body"], "types": ["text"], "required": True, "many": False},
             "name": {"roles": ["name"], "types": [], "required": False, "many": False},
             "link": {"roles": ["link"], "types": [], "required": False, "many": False}}
    record_form = False
    intent_of = "read"
    PLANS = {"default": all_plans(_plan)}
    REJECTS = {"default": {"W1R1": "too_short"}}

    def match(self, rec, prof):
        R = RView(rec)
        body = R.first("text_body")
        if body is None:
            return []
        b = {"body": body.name}
        nm = R.first("name")
        if nm is not None:
            b["name"] = nm.name
        lk = R.first("link")
        if lk is not None:
            b["link"] = lk.name
        return [{"variant": "default", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["read", "now"]

    def describes(self, cand, rec):
        return "The passage itself, clamped to the card, with a link to read more"

    def summary(self, cand, rec, now):
        R = RView(rec)
        return R.text(0, cand.bindings["body"])[:280] if rec.rows else "No text"

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        if not R.rows or R.cell(0, b["body"]) in (None, ""):
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        drop = ctx.rung.get("drop", 0)
        body_rc = ["raw", b["body"], 0]
        name = b.get("name")
        link = b.get("link") if b.get("link") and link_ok(str(R.cell(0, b["link"]) or "")) else None
        lh = line_h("body")
        # a long name is a title (top); a short one is an attribution (below the passage)
        name_s = cv.run(["raw", name, 0]) if name else ""
        if name_s and name_s.casefold() == cv.run(["title"]).casefold():
            name_s = ""            # never repeat the card title in the body
        title_top = bool(link) and len(name_s) > 0
        y = y0
        if title_top and name_s:
            tb = cv.text(["l", 0], y, ["raw", name, 0], "name", src="data", max_w=W, max_lines=1 if ctx.rows == 1 else 2)
            y = tb.bottom + 6
        tail_h = 0
        if name_s and not title_top:
            tail_h += line_h("meta") + 6
        if link:
            tail_h += line_h("meta") + 6
        n_lines = max(1, int((y1 - y - tail_h) // lh) - drop)
        bb = cv.text(["l", 0], y, body_rc, "body", src="data", max_w=W, max_lines=n_lines)
        y = (bb.bottom if bb else y) + 6
        if name_s and not title_top:
            cv.text(["l", 0], y, ["join", "", [["lit", "— "], ["raw", name, 0]]], "meta", src="data", max_w=W)
            y += line_h("meta") + 6
        if link:
            lb = cv.text(["l", 0], y, ["tpl", "read_more", {}], "meta", src="code", max_w=W, tok="accent")
            cv.d.hitmap.append({"prim": lb.id, "label": "Read more", "href": str(R.cell(0, link))})


FORM = TextBrief()
