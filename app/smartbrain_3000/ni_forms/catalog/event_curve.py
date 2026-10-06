"""`event_curve` (domain-specific per final_design 2.4; selected only by the
`alternating_extrema` signature, never by a word): a headline with timed variants
("High 6.0 ft at 7:10 pm" -> "Low 0.8 ft at 1:19 am" when the event passes) and a
D1-proportioned curve through the PUBLISHED extrema (solid dots) with estimated
segments between them (style "interp", legend at R2+), a now marker, past events
dimmed, highs labelled above and lows below.

Params (enumerated, never set per card): window in {local_day, around_now};
label_policy in {all, extremes, next} (the ladder steps it down).
Plans: W1/W2 rejected (sibling next_event); W3/W4 R1 compact, R2 the approved D1 look,
R3 + day table; W5/W6 R1 headline | wide curve, R2 + sub line, R3 + table.
"""
from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

from .. import fmt
from ..base import BaseForm, all_plans, calm, line_h
from ..ctx import VIZ, Canvas, LayoutCtx, pbox, shift, ty
from ..events import (
    events,
    iso_at,
    next_after,
    time_field,
    today,
    value_field,
)
from ..rec import R as RView

LABEL_POLICIES = ("all", "extremes", "next")


def _plan(w, r):
    if w == "W1":
        return None
    if w == "W2":
        return {1: "next", 2: "next_days", 3: "next_days"}[r]
    if w in ("W3", "W4"):
        return {1: "compact", 2: "curve", 3: "curve_table"}[r]
    return {1: "wide", 2: "curve", 3: "curve_table"}[r]


class EventCurve(BaseForm):
    name = "event_curve"
    variants = ("default",)
    slots = {"time": {"roles": ["time"], "types": ["datetime"], "required": True, "many": False},
             "value": {"roles": ["value", "measure"], "types": ["quantity", "number"], "required": True, "many": False}}
    params_space = {"window": ["local_day", "around_now"], "label_policy": list(LABEL_POLICIES)}
    sibling = "day_table"
    record_form = True
    intent_of = "now"
    PLANS = {"default": all_plans(_plan)}
    REJECTS = {"default": {f"W1R{r}": "too_narrow" for r in (1, 2, 3)}}

    def intent(self, variant, params):
        return "today" if params.get("window") == "local_day" else "now"

    def match(self, rec, prof):
        if "alternating_extrema" not in (prof.signatures or []):
            return []
        R = RView(rec)
        tf, vf = time_field(R), value_field(R)
        if tf is None or vf is None or tf.type != "datetime":
            return []
        b = {"time": tf.name, "value": vf.name}
        return [{"variant": "default", "bindings": b, "params": {"window": w, "label_policy": "all"}}
                for w in ("around_now", "local_day")]

    def covers(self, cand, prof, sclass):
        if sclass in ("W1R1", "W2R1"):        # next: the next event + countdown
            return ["now", "next", "height_value", "times"]
        if sclass[:2] in ("W1", "W2"):         # next + the published times by day
            return ["now", "next", "height_value", "times", "each_day", "extreme_high", "extreme_low"]
        if sclass in ("W3R1", "W4R1"):        # compact: headline + unlabelled curve
            return ["now", "next", "height_value", "trend"]
        c = ["now", "next", "times", "height_value", "extreme_high", "extreme_low", "trend"]
        if int(sclass[3]) == 3:
            c.append("each_day")
        return c

    def describes(self, cand, rec):
        w = "today (midnight to midnight)" if cand.params.get("window") == "local_day" else "the next 30 hours"
        return f"Headline with the next high or low, and a curve through the published highs and lows for {w}"

    def summary(self, cand, rec, now):
        R = RView(rec)
        evs = events(R, cand.bindings["time"], cand.bindings["value"])
        k = next_after(evs, now)
        if k is None:
            return "No upcoming events in the data"
        e = evs[k]
        word = "High" if e.ext == "hi" else "Low" if e.ext == "lo" else "Next"
        return f"Next: {word} {R.text(e.row, cand.bindings['value'])} at " \
               f"{fmt.format_time(e.iso, 'h:mm a', rec.context.card_tz)}; {len(evs)} published events"

    # ------------------------------------------------------------------ layout
    def window(self, ctx, evs):
        tz = ctx.tz
        if ctx.cand.params.get("window") == "local_day" and ctx.span.wclass not in ("W5", "W6"):
            d = today(ctx.now, tz)
            a = fmt.day_start(d, tz).timestamp()
            return a, fmt.day_start(d + timedelta(days=1), tz).timestamp()
        n = ctx.now.timestamp()
        return n - 6 * 3600, n + 30 * 3600

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        evs = events(R, b["time"], b["value"])
        if not evs:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        cv.d.meta["time_src"] = {}
        w0, w1 = self.window(ctx, evs)
        nxt = next_after(evs, ctx.now)
        ctx.extras["horizon"] = w1
        inw = [e for e in evs if w0 <= e.t <= w1]
        if len(evs) < 3 or len(inw) < 2:
            # designed sparse state: too few published points for an honest curve
            if nxt is None:
                calm(ctx, cv, [["tpl", "no_upcoming", {}]])
                return
            from .day_table import day_rows
            y = self.headline(ctx, cv, evs, nxt, ["l", 0], y0, W, hmax=20 if ctx.rows == 1 else None,
                              sub=ctx.rows > 1)
            day_rows(ctx, cv, [e for e in evs if e.day >= today(ctx.now, ctx.tz)], y + 10, y1,
                     days=1 if ctx.rows == 1 else 3, compact=True)
            cv.d.state = "few"
            return
        cv.d.meta["no_balance"] = True
        if plan in ("next", "next_days"):
            from .day_table import day_rows
            if nxt is None:
                calm(ctx, cv, [["tpl", "no_upcoming", {}]])
                return
            y = self.headline(ctx, cv, evs, nxt, ["l", 0], y0, W, hmax=20 if ctx.rows == 1 else 22,
                              sub=True, two_line=True)
            if plan == "next_days":
                day_rows(ctx, cv, [e for e in evs if e.day >= today(ctx.now, ctx.tz)], y + 12, y1,
                         days=None, compact=True)
            cv.d.state = "many"
            return
        wide = plan == "wide"
        head_w = W * 0.34 - 12 if wide else W
        # ------------------------------------------------ headline (timed variants)
        y = y0
        hmax = (20 if y1 - y0 >= 92 else 18) if plan == "compact" else None
        hb = self.headline(ctx, cv, evs, nxt, ["l", 0], y, head_w, hmax=hmax, sub=plan != "compact",
                           two_line=wide)
        y = hb
        # ------------------------------------------------ table (R3)
        bottom = y1
        legend = plan != "compact" and ctx.rows >= 2
        if legend:
            cv.text(["l", 0], bottom - line_h("meta"), ["tpl", "interp_legend", {}], "meta", src="code", max_w=W)
            bottom -= line_h("meta") + 6
        # ------------------------------------------------ curve (aspect-capped), then the table fills
        fa = 0.36 if wide else 0.0
        top = y0 if wide else y + (4 if plan == "compact" else 8)
        from ..base import plot_cap
        cbot = bottom
        if plan in ("curve", "curve_table"):
            lab_room = 2 * (line_h("label") + VIZ["label_offset"]) + line_h("tick") + 2
            cbot = min(bottom, top + plot_cap(ctx, 1.0 - fa) + lab_room)
            if plan == "curve_table":          # R3 buys the table: the curve keeps at most ~55% of the room
                cbot = min(cbot, top + max(0.55 * (bottom - top), 150))
            elif plan == "curve" and ctx.rows == 2:   # R2: curve plus today's published times (never fewer
                cbot = min(cbot, top + max(0.62 * (bottom - top), 120))   # events than the narrower card)
        self.curve(ctx, cv, evs, nxt, w0, w1, fa, 1.0, top, cbot,
                   labels=plan != "compact", ticks=plan not in ("compact", "wide"))
        if plan in ("curve", "curve_table") and bottom - cbot > line_h("sub") + 16:
            from .day_table import day_rows
            for pl in cv.d.meta.get("plots", []):
                pl["min_share"] = False          # the curve shares the body with the table by design
            day_rows(ctx, cv, [e for e in evs if e.day >= today(ctx.now, ctx.tz)], cbot + 14, bottom,
                     days=None, compact=True)
        cv.d.state = "many"

    def headline(self, ctx, cv, evs, nxt, x, top, max_w, *, hmax=None, sub=True, two_line=False) -> float:
        _R, b = ctx.R, ctx.cand.bindings
        tz = ctx.tz
        vf = b["value"]
        show_zone = True
        if nxt is None:
            tb = cv.text(x, top, ["tpl", "no_upcoming", {}], "sub", src="code", max_w=max_w)
            return tb.bottom if tb else top
        # variants: from the next event up to the end of the data (cap 8)
        horizon = ctx.extras.get("horizon")
        ks = [k for k in range(nxt, min(len(evs), nxt + 6))
              if horizon is None or k == nxt or evs[k - 1].t < horizon]
        heads = []
        for k in ks:
            e = evs[k]
            word = ["tpl", "high", {}] if e.ext == "hi" else ["tpl", "low", {}] if e.ext == "lo" else ["tpl", "next", {}]
            heads.append((k, ["tpl", "kind_value_at", {"kind": word, "value": ["cell", vf, e.row, {}]}]))
        # one headline size for every variant (no jump when the variant switches)
        lad = [p for p in TX_LADDER("headline") if hmax is None or p <= hmax]
        lad = lad[min(ctx.rung.get("hero", 0), len(lad) - 1):]
        cv.measure(fmt.ZONE_WIDEST, "headline", lad[-1])
        px = lad[-1]
        for p in lad:
            ok = True
            for k, rc in heads:
                s = cv.run(rc)
                tstr = fmt.format_time(evs[k].iso, "h:mm a", tz)
                w1_ = cv.measure(s, "headline", p) + cv.measure(" " + tstr, "headline", p) + \
                    cv.measure(fmt.ZONE_WIDEST, "headline", p)
                if two_line:
                    w1_ = max(cv.measure(s, "headline", p), cv.measure(tstr + fmt.ZONE_WIDEST, "headline", p))
                if w1_ * 1.04 > max_w:
                    ok = False
                    break
            if ok:
                px = p
                break
        lh = ty("headline", px)[2]
        variants = []
        bottom = top
        for j, (k, rc) in enumerate(heads):
            e = evs[k]
            ids = []
            s = cv.run(rc)
            tb = cv.text(x, top, rc, "headline", src="data", max_w=max_w, px=px, s=s)
            ids.append(tb.id)
            if two_line:
                tm = cv.time(x, top + lh, e.iso, "h:mm a", "headline", tz="card", show_zone=show_zone, px=px)
            else:
                sp = cv.measure(s + " ", "headline", px)
                tm = cv.time(shift(x, sp), top, e.iso, "h:mm a", "headline", tz="card", show_zone=show_zone, px=px)
            ids.append(tm.id)
            cv.d.meta["time_src"][tm.id] = b["time"]
            yb = tm.bottom
            if sub:
                word = ["tpl", "rising", {}] if e.ext == "hi" else ["tpl", "falling", {}] if e.ext == "lo" else None
                sx = x
                if word is not None:
                    sw = cv.text(x, yb + 2, ["join", "", [word, ["lit", " ·"]]], "sub", src="code",
                                 max_w=max_w)
                    ids.append(sw.id)
                    sx = shift(x, sw.x1 - sw.x0 + 4)
                t_from = ctx.now if j == 0 else datetime.fromtimestamp(evs[k - 1].t, UTC)
                cd = cv.text(sx, yb + 2, ["d", "countdown", [fmt.iso(t_from), e.iso]], "sub", src="code",
                             max_w=cv.measure(fmt.COUNTDOWN_WIDEST, "sub") * 1.04 + 2)
                ids.append(cd.id)
                cv.live("countdown", prim=cd.id, t=e.iso, fmt="in_hm")
                yb = cd.bottom
            bottom = max(bottom, yb)
            variants.append({"t_from": None if j == 0 else evs[k - 1].iso, "t_to": e.iso if j < len(heads) - 1 else None,
                             "prims": ids})
            cv.d.meta.setdefault("headline_events", []).append(
                (variants[-1]["t_from"], variants[-1]["t_to"], e.iso if j < len(heads) - 1 else None))
        # the slot: first variant visible now; later ones hidden until their window
        if len(variants) > 1:
            cv.live("timed_variants", slot="headline", variants=variants)
        return bottom

    def curve(self, ctx, cv, evs, nxt, w0, w1, fa, fb, top, bottom, *, labels=True, ticks=True):
        _R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        tz = ctx.tz
        vf = b["value"]
        inset = (VIZ["dot_r"] + VIZ["dot_ring"] + 1) / W       # dots at the window edge stay inside the card
        fa, fb = fa + inset, fb - inset
        pol = LABEL_POLICIES[min(ctx.rung.get("label_policy", 0) + LABEL_POLICIES.index(
            ctx.cand.params.get("label_policy", "all")), 2)]
        lab_h = line_h("label")
        tick_h = line_h("tick") + 2 if ticks and not ctx.rung.get("drop_tick") else 0
        room = (lab_h + VIZ["label_offset"]) if labels else VIZ["dot_r"] + VIZ["dot_ring"]
        cy0, cy1 = top + room, bottom - tick_h - room
        # events that shape the visible curve: in window plus one neighbour each side
        inw = [k for k, e in enumerate(evs) if w0 <= e.t <= w1]
        lo_k = max(0, (inw[0] if inw else next((k for k, e in enumerate(evs) if e.t > w0), len(evs) - 1)) - 1)
        hi_k = min(len(evs) - 1, (inw[-1] if inw else lo_k) + 1)
        seg = evs[lo_k:hi_k + 1]
        vals = [e.value for e in seg if e.value is not None]
        if len(seg) < 2 or not vals:
            return
        vmin, vmax = min(vals), max(vals)
        if vmax == vmin:
            vmax += 1

        def FX(t):
            return fa + (t - w0) / (w1 - w0) * (fb - fa)

        def PY(v):          # y px
            return cy0 + (1 - (v - vmin) / (vmax - vmin)) * (cy1 - cy0)
        box = pbox(["f", fa], ["f", fb], cy0, cy1)
        # cosine interpolation between published points (honest: style interp)
        pts = []
        per = max(6, min(40, int(360 / max(1, len(seg) - 1))))
        for a, c in itertools.pairwise(seg):
            for s in range(per):
                u = s / per
                t = a.t + (c.t - a.t) * u
                v = a.value + (c.value - a.value) * (1 - math.cos(math.pi * u)) / 2
                if w0 <= t <= w1:
                    pts.append(((t - w0) / (w1 - w0), 1 - (v - vmin) / (vmax - vmin)))
        last = seg[-1]
        if w0 <= last.t <= w1:
            pts.append(((last.t - w0) / (w1 - w0), 1 - (last.value - vmin) / (vmax - vmin)))
        # clip edges exactly at the window
        if pts and pts[0][0] > 0.002:
            v = self._interp(seg, w0)
            if v is not None:
                pts.insert(0, (0.0, 1 - (v - vmin) / (vmax - vmin)))
        if pts and pts[-1][0] < 0.998:
            v = self._interp(seg, w1)
            if v is not None:
                pts.append((1.0, 1 - (v - vmin) / (vmax - vmin)))
        pts = pts[:398]
        if len(pts) < 2:
            return
        cv.path(box, pts + [(pts[-1][0], 1.0), (pts[0][0], 1.0)], "viz-fill", w=0, fill="viz-fill")
        pid = cv.path(box, pts, "viz-line", w=VIZ["line_w"], style="interp")
        cv.d.meta.setdefault("plots", []).append({"id": pid, "y0": top, "y1": bottom, "min_share": ctx.rows >= 2,
                                                  "amplitude": PY(vmin) - PY(vmax)})
        # dots, labels, past dim
        vis = [evs[k] for k in inw]
        lab_set = set()
        if labels:
            if pol == "all":
                lab_set = {e.row for e in vis}
            elif pol == "extremes":
                his = [e for e in vis if e.ext == "hi"]
                los = [e for e in vis if e.ext == "lo"]
                if his:
                    lab_set.add(max(his, key=lambda e: e.value).row)
                if los:
                    lab_set.add(min(los, key=lambda e: e.value).row)
                if nxt is not None:
                    lab_set.add(evs[nxt].row)
            else:
                if nxt is not None:
                    lab_set.add(evs[nxt].row)
        if labels and pol == "all" and not ctx.rung.get("label_policy"):
            lw = sum(cv.measure(cv.run(["join", " · ", [["d", "date", [e.iso, "h:mm a", tz]],
                                                         ["cell", vf, e.row, {}]]]), "label") + 10 for e in vis)
            if lw > (fb - fa) * W * 1.6:          # highs and lows share the width above/below the curve
                lab_set = {e.row for e in vis if e.row in lab_set and
                           (e.ext == "hi" and e.value == max(x.value for x in vis if x.ext == "hi") or
                            e.ext == "lo" and e.value == min(x.value for x in vis if x.ext == "lo") or
                            (nxt is not None and e.row == evs[nxt].row))}
        ext_meta = []
        for e in vis:
            fx = FX(e.t)
            ydot = PY(e.value)
            did = cv.dot(["f", fx], ydot, VIZ["dot_r"], "viz-line", ring="viz-dot-ring", ring_w=VIZ["dot_ring"])
            dim = [did]
            ext_meta.append(("hi" if e.ext == "hi" else "lo", ydot))
            if e.row in lab_set:
                rc = ["join", " · ", [["d", "date", [e.iso, "h:mm a", tz]], ["cell", vf, e.row, {}]]]
                s = cv.run(rc)
                w = cv.measure(s, "label") * 1.04 + 1
                px_ = fx * W
                anc = "middle"
                if px_ - w / 2 < fa * W:
                    anc = "start"
                    fa if fx - fa < 0.02 else fx - 0.0
                    x = ["f", max(fa, fx - 6 / W)] if px_ - fa * W < 8 else ["f", fx]
                    x = ["f", fa] if px_ - w / 2 < fa * W else x
                elif px_ + w / 2 > fb * W:
                    anc = "end"
                    x = ["f", fb]
                else:
                    x = ["f", fx]
                above = e.ext == "hi"
                if ctx.rung.get("flip") and ydot - lab_h - VIZ["label_offset"] < top:
                    above = False
                ly = ydot - VIZ["label_offset"] - lab_h if above else ydot + VIZ["label_offset"]
                if not above and ly < cy1 + 2 < ly + lab_h:
                    ly = cy1 + 3                   # a low label sits clear of the baseline, never across it
                tb = cv.text(x, ly, rc, "label", src="data", max_w=w, anchor=anc)
                if tb:
                    dim.append(tb.id)
            cv.live("past_dim", prims=dim, t=e.iso)
            cv.hit(did, cv.run(["join", " · ", [["d", "date", [e.iso, "h:mm a", tz]], ["cell", vf, e.row, {}]]]))
        cv.d.meta["extrema"] = ext_meta
        # ticks: every 6 h on card-zone clock hours (no weekdays: dates may be inferred)
        if tick_h:
            z = fmt.zone(tz)
            t = datetime.fromtimestamp(w0, UTC).astimezone(z).replace(minute=0, second=0, microsecond=0)
            t = t.replace(hour=(t.hour // 6) * 6)
            prev = -1e9
            while t.timestamp() <= w1 + 1:
                if t.timestamp() >= w0 - 1:
                    fx = FX(t.timestamp())
                    s = fmt.format_time(fmt.iso(t), "ha_short", tz)
                    w = cv.measure(s, "tick") * 1.04 + 1
                    anc = "start" if fx - fa < 0.03 else "end" if fb - fx < 0.03 else "middle"
                    x0 = fx * W - (0 if anc == "start" else w if anc == "end" else w / 2)
                    if x0 > prev + 10:
                        cv.time(["f", fx], bottom - line_h("tick"), fmt.iso(t), "ha_short", "tick", tz="card", anchor=anc)
                        prev = x0 + w
                t = (t + timedelta(hours=6)).astimezone(z)
                t = t.replace(hour=(t.hour // 6) * 6, minute=0)
        # now marker (line + dot on the curve), moved live by the painter
        n = ctx.now.timestamp()
        if w0 <= n <= w1:
            fx = FX(n)
            v = self._interp(seg, n)
            lid = cv.line(["f", fx], top + 2, ["f", fx], cy1 + 2, "viz-now-line", w=VIZ["now_w"])
            ids = [lid]
            if v is not None:
                ids.append(cv.dot(["f", fx], PY(v), VIZ["now_dot_r"], "viz-now", ring="viz-dot-ring",
                                  ring_w=VIZ["now_dot_ring"]))
            cv.live("now_marker", prims=ids, box=box, t0=iso_at(w0), t1=iso_at(w1), path=pid)

    @staticmethod
    def _interp(seg, t):
        for a, c in itertools.pairwise(seg):
            if a.t <= t <= c.t:
                u = (t - a.t) / (c.t - a.t) if c.t > a.t else 0
                return a.value + (c.value - a.value) * (1 - math.cos(math.pi * u)) / 2
        return None


def TX_LADDER(role):
    from .. import tokens as TK
    return TK.TYPE[role]["ladder"]


FORM = EventCurve()
