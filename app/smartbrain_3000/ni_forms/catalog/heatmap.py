"""`heatmap` (provisional: 3 corpus instances): a day x time-of-day matrix on the
sequential ramp with QUANTILE buckets (a spike never flattens the scale). The headline
is code-picked from the ask's wants: extreme_low -> the minimum, extreme_high -> the
maximum, else the latest value. W1 and W2R1 are rejected (cell_too_small); other spans
need cells >= 6x6 px at the floor, else cell_too_small.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import fmt
from ..base import BaseForm, all_plans, calm, line_h
from ..ctx import Canvas, LayoutCtx, pbox
from ..rec import R as RView
from ..rec import is_numeric, ts
from ..spans import ALL_SPANS, bucket
from ..types import Reject


def _plan(w, r):
    if w == "W1" or (w == "W2" and r == 1):
        return None
    return f"heat{r}"


class Heatmap(BaseForm):
    name = "heatmap"
    variants = ("day_hour", "calendar")
    slots = {"time": {"roles": ["time"], "types": ["datetime"], "required": True, "many": False},
             "value": {"roles": ["value", "measure", "count"], "types": [], "required": True, "many": False}}
    record_form = True
    intent_of = "plan"
    PLANS = {"day_hour": all_plans(_plan), "calendar": all_plans(_plan)}
    REJECTS = {v: {"W1R1": "cell_too_small", "W1R2": "cell_too_small", "W1R3": "cell_too_small",
                   "W2R1": "cell_too_small"} for v in ("day_hour", "calendar")}

    def match(self, rec, prof):
        if "matrix" not in (prof.signatures or []):
            return []
        R = RView(rec)
        tf = R.first("time", "date")
        vf = next((f for f in rec.fields if f.role in ("value", "measure", "count", "status")
                   and (is_numeric(f) or f.type == "bool")), None)
        if tf is None or vf is None:
            return []
        daily = tf.type == "date" or (prof.time and prof.time.grain in ("day", "week"))
        return [{"variant": "calendar" if daily else "day_hour",
                 "bindings": {"time": tf.name, "value": vf.name}, "params": {}}]

    def _val(self, R, i, f):
        v = R.cell(i, f)
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        return R.num(i, f)

    def _grid(self, R, b, calendar=False):
        """{(row key, col): [(v, row)]}: day x hour, or week x weekday for daily data."""
        z = fmt.zone(R.tz)
        cells = {}
        for i in range(len(R.rows)):
            t = fmt.parse_t(R.cell(i, b["time"]))
            v = self._val(R, i, b["value"])
            if t is None or v is None:
                continue
            d = t.astimezone(z).date() if isinstance(t, datetime) else t
            if calendar:
                wk = d - timedelta(days=d.weekday())
                cells.setdefault((wk, d.weekday()), []).append((v, i))
            elif isinstance(t, datetime):
                cells.setdefault((d, t.astimezone(z).hour), []).append((v, i))
        days = sorted({k for k, _ in cells})
        return days, cells

    def structural(self, cand, rec, prof, sclass, plan):
        days = max(1, (prof.time.days if prof and prof.time else 7) or 7)
        cols = 24
        if cand.variant == "calendar":
            days, cols = max(1, -(-days // 7)), 7
        span = next(s for s in ALL_SPANS if s.sclass == sclass)
        bk = bucket(span)
        grid_h = bk.h - 28 - 24 - 60 - 16
        grid_w = bk.min_w - 48
        if grid_h / days < 6 or grid_w / cols < 6:
            return Reject("cell_too_small")
        return plan

    def covers(self, cand, prof, sclass):
        return ["extreme_low", "extreme_high", "times", "each_day", "trend"]

    def describes(self, cand, rec):
        return "A day by hour grid shaded from low to high, with the best hour named"

    def summary(self, cand, rec, now):
        RView(rec)
        return f"{len(rec.rows)} hourly values in a day by hour grid"

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        cal = ctx.cand.variant == "calendar"
        ncol = 7 if cal else 24
        days, cells = self._grid(R, b, cal)
        if not days:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        cv.d.meta["no_balance"] = True
        vf = b["value"]
        allv = sorted(v for vs in cells.values() for v, _ in vs)
        wants = ctx.prof.wants or []
        pick = None
        if "extreme_low" in wants:
            pick = min((v, i) for vs in cells.values() for v, i in vs)
            word = ["tpl", "low", {}]
        elif "extreme_high" in wants:
            pick = max((v, i) for vs in cells.values() for v, i in vs)
            word = ["tpl", "high", {}]
        if pick is None:
            last = max(((ts(R.cell(i, b["time"]), R.tz), i) for vs in cells.values() for _, i in vs))
            pick = (self._val(R, last[1], vf), last[1])
            word = ["tpl", "latest", {}]
        # headline: "{Low|High|Latest} {value} at" + card-zone time
        rc = ["tpl", "kind_value_at", {"kind": word, "value": ["cell", vf, pick[1], {}]}]
        s = cv.run(rc)
        tfmt = "MMM d" if cal else "MMM d, h:mm a"
        tstr = fmt.format_time(R.cell(pick[1], b["time"]), tfmt, ctx.tz)
        px = next((p for p in (25, 22, 20, 18) if cv.measure(s + " " + tstr, "headline", p) * 1.04 <= W), None)
        two = px is None
        if two:
            px = next((p for p in (22, 20, 18) if max(cv.measure(s, "headline", p), cv.measure(tstr, "headline", p)) * 1.04 <= W), 18)
        hb = cv.text(["l", 0], y0, rc, "headline", src="data", max_w=W, px=px, s=s)
        tx = ["l", 0] if two else ["l", cv.measure(s + " ", "headline", px)]
        ty_ = hb.bottom if two else y0
        tm = cv.time(tx, ty_, R.cell(pick[1], b["time"]), tfmt, "headline", tz="card", px=px)
        cv.d.meta["time_src"] = {tm.id: b["time"]}
        top = tm.bottom + 10
        # quantile buckets (5 steps)
        qs = [allv[min(len(allv) - 1, int(len(allv) * q))] for q in (0.2, 0.4, 0.6, 0.8)]

        def step(v):
            return sum(v > q for q in qs)
        lab_w = cv.measure("May 28" if cal else "Wed 28", "tick") * 1.04 + 6
        tick_h = line_h("tick") + 2
        leg_h = line_h("tick") + 6
        gy0, gy1 = top, y1 - tick_h - leg_h
        if cal:      # square-ish calendar cells, never stretched past 3:1
            gy1 = min(gy1, gy0 + len(days) * max(8.0, min(28.0, (W - lab_w) / 7 / 1.4)))
        box = pbox(["l", lab_w], ["r", 0], gy0, gy1)
        vv = []
        for d in days:
            for h in range(ncol):
                c = cells.get((d, h))
                vv.append(step(sum(v for v, _ in c) / len(c)) if c else -1)
        cv.cells(box, ncol, len(days), vv, ramp="seq", steps=5, gap=1)
        cv.d.meta.setdefault("plots", []).append({"id": None, "y0": gy0, "y1": gy1, "min_share": True})
        rh = (gy1 - gy0) / len(days)
        for j, d in enumerate(days):
            if rh < line_h("tick") and j % max(1, int(line_h("tick") // rh) + 1):
                continue
            cv.time(["l", 0], gy0 + j * rh + (rh - line_h("tick")) / 2, d.isoformat(), "MMM d" if cal else "EEE d",
                    "tick", tz="card")
        gw = W - lab_w
        if cal:          # weekday initials under the columns (real dates: verified weekdays)
            for h in range(7):
                dd = days[0] + timedelta(days=h)
                cv.time(["f", (lab_w + gw * (h + 0.5) / 7) / W], gy1 + 2, dd.isoformat(), "EEE", "tick", tz="card",
                        anchor="middle")
        else:
            for h in (0, 6, 12, 18):
                t = fmt.iso(fmt.day_start(days[0], ctx.tz) + timedelta(hours=h))
                cv.time(["f", (lab_w + gw * (h + 0.5) / 24) / W], gy1 + 2, t, "ha_short", "tick", tz="card",
                        anchor="middle")
        # legend: the ramp named at both ends (low/high values), never colour alone
        ly = gy1 + tick_h + 4
        lo_rc = ["d", "agg", [vf, "min"], {"field": vf}]
        hi_rc = ["d", "agg", [vf, "max"], {"field": vf}]
        lb = cv.text(["l", lab_w], ly, lo_rc, "tick", src="data", max_w=gw / 2 - 4)
        hbx = cv.text(["r", 0], ly, hi_rc, "tick", src="data", max_w=gw / 2 - 4, anchor="end")
        free = gw - (lb.w if lb else 0) - (hbx.w if hbx else 0) - 16
        if free >= 64:
            x = lab_w + (lb.w if lb else 0) + 8
            sw = min(12, (free - 8) / 5)
            for k in range(5):
                cv.rect(["l", x + k * sw], ["l", x + k * sw + sw - 2], ly + 3, ly + 11, f"viz-seq-{k + 1}")
        cv.d.state = "many"


FORM = Heatmap()
