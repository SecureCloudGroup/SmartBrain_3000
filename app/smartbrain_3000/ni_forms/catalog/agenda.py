"""`agenda`: a time-ordered list with a now line, past events dimmed, a live countdown on
the next event, and full datetimes (an event after midnight belongs to the next day and
says so). Variants: `list`, `timeline` (a state strip for events with an end time).
Rows by height (2 / 6 / 10); W1 time + name; W3+ adds the meta column. Every span valid.
"""
from __future__ import annotations

from .. import fmt
from ..base import BaseForm, all_plans, calm, line_h, more_line
from ..ctx import Canvas, LayoutCtx
from ..events import events, next_after, time_field, today
from ..lint import link_ok
from ..rec import R as RView
from ..rec import fact_ok


class Agenda(BaseForm):
    name = "agenda"
    variants = ("list", "timeline")
    slots = {"time": {"roles": ["time", "date"], "types": ["datetime", "date"], "required": True, "many": False},
             "end": {"roles": ["time_end"], "types": [], "required": False, "many": False},
             "name": {"roles": ["name", "kind", "status"], "types": [], "required": True, "many": False},
             "meta": {"roles": ["meta"], "types": [], "required": False, "many": True},
             "link": {"roles": ["link"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "today"
    PLANS = {v: all_plans(lambda w, r: f"agenda{r}") for v in variants}

    def match(self, rec, prof):
        if rec.kind not in ("events", "records"):
            return []
        R = RView(rec)
        tf = time_field(R)
        nm = R.first("name", "kind", "status")
        if tf is None or nm is None or tf.type != "datetime":
            return []
        b = {"time": tf.name, "name": nm.name, "meta": [f.name for f in rec.fields if f.role == "meta" and fact_ok(f, rec)
                                                        and f.type in ("text", "category")][:1]}
        end = R.first("time_end")
        if end is not None:
            b["end"] = end.name
        lk = R.first("link")
        if lk is not None:
            b["link"] = lk.name
        out = [{"variant": "list", "bindings": b, "params": {}}]
        if end is not None and "state_timeline" in (prof.signatures or []):
            out.append({"variant": "timeline", "bindings": dict(b), "params": {}})
        return out

    def covers(self, cand, prof, sclass):
        return ["now", "next", "times", "count"]

    def describes(self, cand, rec):
        return "Today's list in time order with a now line and the next item counted down"

    def summary(self, cand, rec, now):
        R = RView(rec)
        evs = events(R, cand.bindings["time"])
        k = next_after(evs, now)
        return f"{len(evs)} items" + (f"; next {R.text(evs[k].row, cand.bindings['name'])}" if k is not None else "")

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        evs = events(R, b["time"])
        if not evs:
            calm(ctx, cv, [["tpl", "no_items", {}]])
            return
        if ctx.cand.variant == "timeline":
            return self._timeline(ctx, cv, evs)
        tf = R.f[b["time"]]
        tz = "card" if tf.wallclock else "viewer"
        cv.d.meta["time_src"] = {}
        nxt = next_after(evs, ctx.now)
        start = max(0, (nxt if nxt is not None else len(evs)) - 1)
        rows = evs[start:]
        d0 = today(ctx.now, ctx.tz)
        with_meta = ctx.wc in ("W3", "W4", "W5", "W6") and ctx.rows >= 2 and b.get("meta")
        row_h = (19 + (16 if with_meta else 0) + (10 if ctx.rows > 1 else 7))
        more_h = line_h("meta") + 4
        avail = y1 - y0
        k = int(avail // row_h)
        if k < len(rows):
            k = int((avail - more_h) // row_h)
        k = max(1, k - ctx.rung.get("drop", 0))
        hidden = len(rows) - k
        multi_day = any(e.day != d0 for e in rows[:k])
        tfmt = "h:mm a"
        t_w = cv.measure(fmt.WIDEST[tfmt], "sub") * 1.04 + 1
        day_w = cv.measure("Tomorrow ", "sub") * 1.04 if multi_day and ctx.wc not in ("W1",) else 0
        cd_w = cv.measure(fmt.COUNTDOWN_WIDEST, "meta") * 1.04 + 2 if ctx.wc not in ("W1", "W2") else 0
        name_x = day_w + t_w + 10
        if ctx.wc == "W1":
            name_x = 0
        y = y0
        for j, e in enumerate(rows[:k]):
            ids = []
            ty_ = y
            if ctx.wc == "W1":       # stacked: time over name
                tm = cv.time(["l", 0], ty_, e.iso, tfmt, "meta", tz=tz, tok="muted")
                cv.d.meta["time_src"][tm.id] = b["time"]
                ids.append(tm.id)
                nb = cv.text(["l", 0], ty_ + 16, ["raw", b["name"], e.row], "item", src="data", max_w=W)
                ids.append(nb.id if nb else None)
                y += 16 + row_h
            else:
                if day_w and e.day != d0:
                    ids.append(cv.text(["l", 0], ty_, ["d", "relday", [e.day.isoformat(), d0.isoformat()]], "sub",
                                       src="code", max_w=day_w).id)
                tm = cv.time(["l", day_w], ty_, e.iso, tfmt, "sub", tz=tz, tok="text")
                cv.d.meta["time_src"][tm.id] = b["time"]
                ids.append(tm.id)
                is_next = nxt is not None and e is evs[nxt]
                cw = cd_w if is_next else 0
                nb = cv.text(["l", name_x], ty_, ["raw", b["name"], e.row], "item", src="data",
                             max_w=W - name_x - (cw + 8 if cw else 0))
                if nb:
                    ids.append(nb.id)
                    if b.get("link") and R.cell(e.row, b["link"]) and link_ok(str(R.cell(e.row, b["link"]))):
                        cv.d.hitmap.append({"prim": nb.id, "label": "Open", "href": str(R.cell(e.row, b["link"]))})
                if is_next and cw:
                    cd = cv.text(["r", 0], ty_ + 2, ["d", "countdown", [fmt.iso(ctx.now), e.iso]], "meta", src="code",
                                 max_w=cw, anchor="end", tok="accent")
                    cv.live("countdown", prim=cd.id, t=e.iso, fmt="in_hm")
                if with_meta:
                    mm = [["raw", m, e.row] for m in b["meta"] if R.cell(e.row, m) is not None]
                    if mm:
                        ids.append(cv.text(["l", name_x], ty_ + 19, ["join", " · ", mm], "meta", src="data",
                                           max_w=W - name_x).id)
                y += row_h
            cv.live("past_dim", prims=[i for i in ids if i], t=e.iso)
            if nxt is not None and j + start == nxt - 1:
                # the now line sits between the last past event and the next one
                cv.line(["l", 0], y - 5, ["r", 0], y - 5, "accent", w=1)
        if hidden > 0:
            more_line(cv, ["l", name_x], y + 2, hidden, W - name_x)
            cv.d.meta["hidden_items"] = hidden
        cv.d.state = "one" if len(evs) == 1 else ("many" if hidden else "few")

    def _timeline(self, ctx, cv, evs):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        t0 = min(e.t for e in evs)
        ends = [fmt.parse_t(R.cell(e.row, b["end"])) for e in evs]
        t1 = max([x.timestamp() for x in ends if x is not None] + [e.t for e in evs])
        if t1 <= t0:
            t1 = t0 + 3600
        kinds = []
        for e in evs:
            v = R.cell(e.row, b["name"])
            if v not in kinds:
                kinds.append(v)
        top = y0 + 4
        for e, en in zip(evs, ends):
            if en is None:
                continue
            fa, fb = (e.t - t0) / (t1 - t0), (en.timestamp() - t0) / (t1 - t0)
            j = min(kinds.index(R.cell(e.row, b["name"])), 5)
            cv.rect(["f", fa], ["f", max(fb, fa + 0.003)], top, top + 22, f"viz-cat-{j + 1}")
        y = top + 32
        # legend: every state named (never colour alone)
        for j, kd in enumerate(kinds[:6]):
            if y + 19 > y1:
                break
            row = next(e.row for e in evs if R.cell(e.row, b["name"]) == kd)
            cv.rect(["l", 0], ["l", 10], y + 5, y + 15, f"viz-cat-{j + 1}", r=2)
            cv.text(["l", 16], y, ["raw", b["name"], row], "sub", src="data", max_w=W - 16, tok="text")
            y += 21
        cv.d.state = "many"


FORM = Agenda()
