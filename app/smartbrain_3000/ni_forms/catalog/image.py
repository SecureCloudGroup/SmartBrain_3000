"""`image`: one image, `cover` (<= 25% of the area cropped) or `contain` (<= 30% letterbox).
Valid spans are computed from the aspect ratio (a structural fact): a span is accepted iff
one of the fits holds at BOTH bucket ends, else rejected with `aspect`. A caption (title)
shows at R2+ or W3+; an animation shows its latest frame plus "loop · N frames".
"""
from __future__ import annotations

from ..base import BaseForm, all_plans, line_h
from ..ctx import Canvas, LayoutCtx, pbox
from ..rec import R as RView
from ..spans import Span, bucket
from ..types import Reject

HEAD = 28          # 1-line title header (18 + 10)
FOOT = 24          # footer + gap


def _plan(w, r):
    return f"img{r}"


def _caption(sclass: str) -> bool:
    return int(sclass[3]) >= 2 or sclass[:2] in ("W3", "W4", "W5", "W6")


def fit_for(aspect: float, bw: float, bh: float):
    """('cover'|'contain', crop fraction or letterbox fraction) or None."""
    box = bw / bh
    if box <= 0:
        return None
    if aspect >= box:     # image wider than box
        crop = 1 - box / aspect
        letter = 1 - box / aspect
    else:
        crop = 1 - aspect / box
        letter = 1 - aspect / box
    if crop <= 0.25:
        return "cover", crop
    if letter <= 0.30:
        return "contain", letter
    return None


class ImageForm(BaseForm):
    name = "image"
    variants = ("still", "animated")
    slots = {"title": {"roles": ["name"], "types": [], "required": False, "many": False}}
    record_form = False
    intent_of = "now"
    PLANS = {v: all_plans(_plan) for v in variants}

    def match(self, rec, prof):
        if rec.image is None:
            return []
        R = RView(rec)
        nm = R.first("name")
        b = {"title": nm.name} if nm else {}
        return [{"variant": "animated" if rec.image.frames > 1 else "still", "bindings": b, "params": {}}]

    def _box(self, span: Span, cap: bool):
        bk = bucket(span)
        h = bk.h - HEAD - FOOT - ((line_h("meta") + 6) if cap else 0)
        return bk, h

    def structural(self, cand, rec, prof, sclass, plan):
        img = rec.image
        if img is None or not img.w or not img.h:
            return Reject("aspect")
        aspect = img.w / img.h
        from ..spans import ALL_SPANS
        span = next(s for s in ALL_SPANS if s.sclass == sclass)
        for cap in ((True, False) if _caption(sclass) else (False,)):
            bk, h = self._box(span, cap)
            fits = [fit_for(aspect, w, h) for w in (bk.min_w, bk.max_w)]
            if all(f is not None for f in fits):
                return plan if cap == _caption(sclass) else plan + "_bare"
        return Reject("aspect")

    def covers(self, cand, prof, sclass):
        return ["now", "where"]

    def describes(self, cand, rec):
        return "The image itself, fitted to the card"

    def summary(self, cand, rec, now):
        R = RView(rec)
        t = R.text(0, cand.bindings["title"]) if cand.bindings.get("title") and rec.rows else "Image"
        return f"{t} ({rec.image.w}x{rec.image.h})" if rec.image else t

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        img = ctx.rec.image
        plan = self.structural(ctx.cand, ctx.rec, ctx.prof, ctx.span.sclass, plan)
        plan = plan if isinstance(plan, str) else "img"
        cv.d.plan = plan
        cap = _caption(ctx.span.sclass) and not plan.endswith("_bare") and (b.get("title") or img.frames > 1)
        cap_h = (line_h("meta") + 6) if cap else 0
        box = pbox(["l", 0], ["r", 0], y0, y1 - cap_h)
        f = fit_for(img.w / img.h, W, y1 - cap_h - y0) or ("contain", 0)
        crop = None
        if f[0] == "cover":
            a, bx = img.w / img.h, W / (y1 - cap_h - y0)
            if a > bx:
                k = bx / a
                crop = [round((1 - k) / 2, 4), 0, round(1 - (1 - k) / 2, 4), 1]
            else:
                k = a / bx
                crop = [0, round((1 - k) / 2, 4), 1, round(1 - (1 - k) / 2, 4)]
        pid = cv.image(box, img.sha256, f[0], crop)
        cv.hit(pid, "Image")
        cv.d.meta["no_balance"] = True
        if cap:
            parts = []
            if b.get("title") and R.rows:
                parts.append(["raw", b["title"], 0])
            if img.frames > 1:
                parts.append(["tpl", "loop", {"n": ["d", "count", [img.frames]]}])
            cv.text(["l", 0], y1 - line_h("meta"), ["join", " · ", parts], "meta", src="data" if b.get("title") else "code",
                    max_w=W)


FORM = ImageForm()
