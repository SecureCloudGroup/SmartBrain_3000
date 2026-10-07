"""`map_lite`: points on the first-party land outline (basemap prim). The box is fitted to
the points with a minimum extent; point size follows sqrt(magnitude); the top points get
direct labels via the list beside/below the map (numbered markers tie them together).
W1 rejected; W2R1 rejected (too_short); W2R2 map + top item; W2R3 map + 3 rows;
W3/W4 R1 map | top item, R2 map + 3 rows, R3 map + 7 rows; W5/W6 map | list at every R.
"""
from __future__ import annotations

import math

from ..base import BaseForm, all_plans, calm, fact_rc, line_h, more_line
from ..ctx import Canvas, LayoutCtx, pbox
from ..rec import R as RView
from ..rec import is_numeric

MIN_EXT = 20.0     # degrees


def _plan(w, r):
    if w == "W1" or (w == "W2" and r == 1):
        return None
    if w in ("W5", "W6") or (w in ("W3", "W4") and r == 1):
        return "side"
    return "below"


class MapLite(BaseForm):
    name = "map_lite"
    variants = ("default",)
    slots = {"lat": {"roles": ["lat"], "types": [], "required": True, "many": False},
             "lon": {"roles": ["lon"], "types": [], "required": True, "many": False},
             "name": {"roles": ["name"], "types": [], "required": False, "many": False},
             "value": {"roles": ["value", "measure", "severity"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "locate"
    PLANS = {"default": all_plans(_plan)}
    REJECTS = {"default": {"W1R1": "too_narrow", "W1R2": "too_narrow", "W1R3": "too_narrow", "W2R1": "too_short"}}

    def match(self, rec, prof):
        R = RView(rec)
        la, lo = R.first("lat"), R.first("lon")
        if la is None or lo is None:
            return []
        # one point on a map answers only 'where': a single reading with its own measure (or several facts)
        # asked about as a reading is not a map (round 3, form fit by data signature)
        if len(rec.rows) <= 1 and "where" not in (prof.wants or []) and rec.kind in ("measure", "records") and \
                any(f.role == "measure" for f in rec.fields):
            return []
        b = {"lat": la.name, "lon": lo.name}
        nm = R.first("name")
        if nm:
            b["name"] = nm.name
        v = next((f for f in rec.fields if f.role in ("value", "measure", "severity") and is_numeric(f)), None)
        if v:
            b["value"] = v.name
        return [{"variant": "default", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["where", "now", "count", "rank"]

    def describes(self, cand, rec):
        return "Map of the points, with the largest listed beside it"

    def summary(self, cand, rec, now):
        R = RView(rec)
        b = cand.bindings
        n = len(rec.rows)
        return f"{n} place{'' if n == 1 else 's'}" + \
            ("; " + "; ".join(R.text(i, b["name"]) for i in self._order(R, b)[:3]) if b.get("name") else "")

    def _order(self, R, b):
        idx = [i for i in range(len(R.rows)) if R.num(i, b["lat"]) is not None and R.num(i, b["lon"]) is not None]
        if b.get("value"):
            idx.sort(key=lambda i: -(R.num(i, b["value"]) or 0))
        return idx

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        idx = self._order(R, b)
        if not idx:
            calm(ctx, cv, [["tpl", "none_now", {}]])
            return
        cv.d.meta["no_balance"] = True
        drop = ctx.rung.get("drop", 0)
        # a list beside / under the map only when a row has something to say (a name or a value):
        # one unnamed point takes the whole box, and nothing is counted as "more" (fix round 1a-5, E)
        listable = bool(b.get("name") or b.get("value"))
        if not listable:
            fa, fb = 0.0, 1.0
            mt, mb = y0, y1
            lx, ly0, ly1 = 0.0, y1, y1
        elif plan == "side":
            fa, fb = 0.0, 0.55
            mt, mb = y0, y1
            lx, ly0, ly1 = 0.58, y0, y1
        else:
            nlist = {1: 1, 2: 3, 3: 7}[ctx.rows] if ctx.wc not in ("W2",) else {2: 1, 3: 3}[ctx.rows]
            lh_ = 22
            list_h = min(nlist, len(idx)) * lh_ + 4
            fa, fb = 0.0, 1.0
            mt, mb = y0, y1 - list_h - 6
            lx, ly0, ly1 = 0.0, mb + 8, y1
        # fit the bbox to the points with a minimum extent, matched to the box aspect
        lats = [R.num(i, b["lat"]) for i in idx]
        lons = [R.num(i, b["lon"]) for i in idx]
        la0, la1, lo0, lo1 = min(lats), max(lats), min(lons), max(lons)
        cla, clo = (la0 + la1) / 2, (lo0 + lo1) / 2
        ext_la = max(la1 - la0, MIN_EXT) * 1.2
        ext_lo = max(lo1 - lo0, MIN_EXT) * 1.2
        bw, bh = (fb - fa) * W, mb - mt
        k = math.cos(math.radians(cla)) or 0.1
        if ext_lo * k / ext_la < bw / bh:
            ext_lo = ext_la * (bw / bh) / k
        else:
            ext_la = ext_lo * k / (bw / bh)
        bbox = [clo - ext_lo / 2, max(-90, cla - ext_la / 2), clo + ext_lo / 2, min(90, cla + ext_la / 2)]
        box = pbox(["f", fa], ["f", fb], mt, mb)
        cv.basemap(box, [round(v, 3) for v in bbox])
        vals = [R.num(i, b["value"]) for i in idx] if b.get("value") else []
        vmax = max([v for v in vals if v is not None] or [1]) or 1
        for n, i in enumerate(reversed(idx[:60])):
            la, lo = R.num(i, b["lat"]), R.num(i, b["lon"])
            fx = fa + (lo - bbox[0]) / (bbox[2] - bbox[0]) * (fb - fa)
            fy = mt + (1 - (la - bbox[1]) / (bbox[3] - bbox[1])) * (mb - mt)
            v = R.num(i, b["value"]) if b.get("value") else None
            r = 3 + 5 * math.sqrt(max(v, 0) / vmax) if v is not None else 4
            top = i in idx[:3]
            did = cv.dot(["f", fx], fy, round(r, 1), "viz-cat-2" if top else "viz-line", ring="viz-dot-ring", ring_w=1)
            if b.get("name"):
                cv.hit(did, cv.run(["raw", b["name"], i]))
        # list: largest first (value + name), the first ones match the highlighted dots
        y = ly0
        lh_ = 22
        k_ = int((ly1 - ly0) // lh_)
        if k_ < len(idx):
            k_ = int((ly1 - ly0 - line_h("meta") - 4) // lh_)
        k_ = max(0, k_ - drop)
        lw = (1 - lx) * W
        for i in idx[:k_]:
            vw = 0
            if b.get("value") and R.cell(i, b["value"]) is not None:
                vs = cv.run(fact_rc(R, b["value"], i))
                vw = cv.measure(vs, "row-strong", 14) * 1.04 + 2
                cv.text(["f", lx], y, fact_rc(R, b["value"], i), "row-strong", src="data", max_w=vw, px=14)
            if b.get("name"):
                cv.text(["f", lx] if not vw else ["f", lx + (vw + 8) / W], y, ["raw", b["name"], i], "sub",
                        src="data", max_w=max(20, lw - vw - 8), tok="text")
            y += lh_
        if listable and len(idx) > k_:
            more_line(cv, ["f", lx], y + 2, len(idx) - k_, lw)
        cv.d.state = "one" if len(idx) == 1 else ("many" if listable and len(idx) > k_ else "few")


FORM = MapLite()
