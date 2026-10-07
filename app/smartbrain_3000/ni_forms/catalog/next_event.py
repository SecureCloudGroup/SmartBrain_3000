"""`next_event`: the next future event - its kind or name, its time (a time prim at hero
size; card zone for wall-clock data), a live countdown, and at R2 the following one or
two events. The switch to the following event is a clock boundary (relayout, no fetch).
W1-W4 R1/R2 valid; R3 and W5/W6 rejected (sibling_better: agenda / event_curve).
"""
from __future__ import annotations

from .. import fmt
from ..base import BaseForm, all_plans, calm, line_h
from ..ctx import Canvas, LayoutCtx
from ..events import events, kind_rc, next_after, time_field, today, value_field
from ..rec import R as RView
from ..rec import fact_ok
from ..shell import inferred_dates


def _plan(w, r):
    if w in ("W5", "W6") or r == 3:
        return None
    return f"next{r}"


class NextEvent(BaseForm):
    name = "next_event"
    variants = ("default",)
    slots = {"time": {"roles": ["time", "date"], "types": ["datetime", "date"], "required": True, "many": False},
             "name": {"roles": ["name", "kind"], "types": [], "required": False, "many": False},
             "value": {"roles": ["value", "measure"], "types": [], "required": False, "many": False},
             "meta": {"roles": ["meta"], "types": [], "required": False, "many": True}}
    record_form = True
    intent_of = "now"
    PLANS = {"default": all_plans(_plan)}
    REJECTS = {"default": {f"{w}R{r}": "sibling_better" for w in ("W1", "W2", "W3", "W4", "W5", "W6")
                           for r in (1, 2, 3) if _plan(w, r) is None}}

    def match(self, rec, prof):
        if rec.kind not in ("events", "records"):
            return []
        R = RView(rec)
        tf = time_field(R)
        if tf is None or (prof.time is not None and prof.time.future_events == 0 and prof.n_rows > 0):
            return []
        b = {"time": tf.name}
        vf = value_field(R)
        nm = R.first("name") if "alternating_extrema" not in (prof.signatures or []) else None
        if nm is not None:
            b["name"] = nm.name
        if vf is not None:
            b["value"] = vf.name
        b["meta"] = [f.name for f in rec.fields if f.role == "meta" and fact_ok(f, rec)
                     and f.type in ("text", "category")][:1]
        return [{"variant": "default", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["next", "now", "times", "height_value"]

    def describes(self, cand, rec):
        return "The next upcoming event with its time and a live countdown"

    def summary(self, cand, rec, now):
        R = RView(rec)
        evs = events(R, cand.bindings["time"], cand.bindings.get("value"))
        k = next_after(evs, now)
        if k is None:
            return "Nothing upcoming"
        e = evs[k]
        when = fmt.format_time(e.iso, 'MMM d, h:mm a', rec.context.card_tz) if not e.all_day else e.iso
        name = R.text(e.row, cand.bindings["name"]) if cand.bindings.get("name") else ""
        return f"Next: {name} · {when}" if name else f"Next: {when}"

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        tf = R.f[b["time"]]
        evs = events(R, b["time"], b.get("value"))
        d0 = today(ctx.now, ctx.tz)
        if tf.type == "date":
            evs = [e for e in evs if e.day >= d0]
            k = 0 if evs else None
        else:
            k = next_after(evs, ctx.now)
        if k is None:
            calm(ctx, cv, [["tpl", "no_upcoming", {}]])
            return
        e = evs[k]
        tz = "card" if (tf.wallclock or tf.type == "date") else "viewer"
        cv.d.meta["time_src"] = {}
        y = y0
        # label: "Next high" (structural extremum), else the event's name
        krc = kind_rc(e, R, None)
        if krc is not None:
            lb = cv.text(["l", 0], y, ["tpl", "next_kind", {"kind": ["lower", krc]}], "sub", src="code", max_w=W)
        elif b.get("name"):
            lb = cv.text(["l", 0], y, ["raw", b["name"], e.row], "item", src="data", max_w=W,
                         max_lines=2 if ctx.rows > 1 else 1)
        else:
            lb = None
        y = lb.bottom + 2 if lb else y
        same_day = e.day == d0
        if e.all_day or not same_day:
            f = "EEE MMM d" if not inferred_dates(ctx.rec) else "MMM d"
        else:
            f = "h:mm a"
        ttz = "card" if e.all_day else tz
        pick = None
        for f2 in ([f, "MMM d"] if f != "h:mm a" else [f]):
            room = y1 - y - line_h("sub") - 2          # the hero must leave room for its value line
            for px in (31, 28, 24, 20):
                sample = fmt.format_time(e.iso, f2, ctx.tz) if ttz == "card" else fmt.WIDEST[f2]
                if cv.measure(sample, "hero", px) * 1.04 + 1 <= W and line_h("hero", px) <= room:
                    pick = (f2, px)
                    break
            if pick:
                break
        f, px = pick or (f, 20)
        tb = cv.time(["l", 0], y, e.iso, f, "hero", tz=ttz, px=px)
        cv.d.meta["time_src"][tb.id] = b["time"]
        y = tb.bottom
        lh = line_h("sub")
        if not same_day and not e.all_day and y + lh <= y1:     # the hero shows the day; the time goes under it
            t1 = cv.time(["l", 0], y + 2, e.iso, "h:mm a", "sub", tz=tz, tok="text")
            cv.d.meta["time_src"][t1.id] = b["time"]
            y = t1.bottom
        val = ["cell", b["value"], e.row, {}] if b.get("value") and R.cell(e.row, b["value"]) is not None else None
        cdw = cv.measure(fmt.COUNTDOWN_WIDEST, "sub") * 1.04 + 2
        cd_rc = ["d", "countdown", [fmt.iso(ctx.now), e.iso]]
        vw = cv.measure(cv.run(val) + " ·", "sub") * 1.04 + 2 if val else 0
        one_line = vw + 4 + cdw <= W
        cd_ok = not e.all_day and (y + 2 + lh * (1 if one_line or not val else 2) <= y1)
        if val:
            vrc = ["join", "", [val, ["lit", " ·"]]] if cd_ok and one_line else val
            pb = cv.text(["l", 0], y + 2, vrc, "sub", src="data", max_w=W, tok="text")
        if cd_ok:
            if val and one_line:
                x, yy = ["l", pb.x1 - pb.x0 + 4], y + 2
            else:
                x, yy = ["l", 0], (pb.bottom + 2 if val else y + 2)
            cd = cv.text(x, yy, cd_rc, "sub", src="code", max_w=cdw)
            if cd:
                cv.live("countdown", prim=cd.id, t=e.iso, fmt="in_dhm")
                y = cd.bottom
        elif val:
            y = pb.bottom
        if ctx.rows >= 2:
            y += 12
            for e2 in evs[k + 1:k + 4]:
                if y + line_h("sub") > y1:
                    break
                if tz == "card" and not e2.all_day:
                    parts2 = ([["d", "relday", [e2.day.isoformat(), d0.isoformat()]]] if e2.day != d0 else []) + \
                        [["d", "date", [e2.iso, "h:mm a", ctx.tz]]]
                    t2 = cv.text(["l", 0], y, ["join", " ", parts2], "sub", src="code", max_w=W * 0.55)
                else:
                    tf2 = "h:mm a" if e2.day == d0 and not e2.all_day else ("MMM d" if inferred_dates(ctx.rec) or e2.all_day
                                                                            else "MMM d, h:mm a")
                    t2 = cv.time(["l", 0], y, e2.iso, tf2, "sub", tz="card" if e2.all_day else tz, tok="muted")
                    cv.d.meta["time_src"][t2.id] = b["time"]
                rest = []
                k2 = kind_rc(e2, R, None)
                if k2 is not None:
                    rest.append(k2)
                elif b.get("name"):
                    rest.append(["raw", b["name"], e2.row])
                if b.get("value") and R.cell(e2.row, b["value"]) is not None:
                    rest.append(["cell", b["value"], e2.row, {}])
                if rest:
                    rs = cv.run(["join", " · ", rest])
                    if cv.fits(rs, "sub", W - t2.x1 - 8):
                        cv.text(["l", t2.x1 + 8], y, ["join", " · ", rest], "sub", src="data",
                                max_w=W - t2.x1 - 8, tok="text", s=rs)
                    elif y + 2 * line_h("sub") <= y1:      # narrow: under its time
                        y += line_h("sub")
                        cv.text(["l", 0], y, ["join", " · ", rest], "sub", src="data", max_w=W, tok="text", s=rs)
                y += line_h("sub") + 4
        cv.d.state = "one" if len(evs) == 1 else "few"


FORM = NextEvent()
