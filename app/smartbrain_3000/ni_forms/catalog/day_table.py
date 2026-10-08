"""`day_table`: rows keyed by card-zone date. Today is highlighted, past events dim,
the table rolls over at card-zone midnight (a clock boundary). Weekday labels only
when dates are verified; inferred dates read Today / Tomorrow / +2 days.
Variants: `events` (time-stamped events grouped by day: tri · time · value cells) and
`rows` (one dated row per day: name + range + secondary).
W1R1 rejected (too_short); days by rows: R1 2-3, R2 5-7, R3 10-14 (from measured heights).
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import fmt
from ..base import BaseForm, all_plans, calm, fact_rc, line_h
from ..ctx import SP, Canvas, LayoutCtx
from ..events import events, time_field, today, value_field
from ..rec import R as RView
from ..shell import inferred_dates


def _plan(w, r):
    if w == "W1" and r == 1:
        return None
    return f"days{r}"


def day_label_rc(ctx, d: date, d0: date):
    """(recipe or None, time iso or None): Today/Tomorrow, else a verified weekday time
    prim, else '+n days' when dates are inferred."""
    n = (d - d0).days
    if n in (0, 1, -1) or inferred_dates(ctx.rec):
        return ["d", "relday", [d.isoformat(), d0.isoformat()]], None
    return None, d.isoformat()


def day_rows(ctx: LayoutCtx, cv: Canvas, evs: list, top: float, bottom: float, *, days: int | None = None,
             compact: bool = False, name_f: str | None = None, start=None) -> float:
    """Events grouped by card-zone day: label | cells (tri, time, value). Returns bottom."""
    _R, W = ctx.R, ctx.W
    tz = ctx.tz
    vf = ctx.cand.bindings.get("value")
    d0 = today(ctx.now, tz)
    by = {}
    for e in evs:
        by.setdefault(e.day, []).append(e)
    last_day = max(by) if by else d0
    lab_w = max(cv.measure(s, "sub") for s in ("Yesterday", "Tomorrow", "+13 days", "Wed 28")) * 1.04 + 10
    t_w = cv.measure("12:59 pm", "sub") * 1.04 + 1
    cells = []
    for e in evs:
        s = cv.run(["cell", vf, e.row, {}]) if vf else (cv.run(["raw", name_f, e.row]) if name_f else "")
        cells.append(cv.measure(s, "sub") * 1.04 + 1)
    val_w = min(max(cells or [0]), W * 0.5)
    tri_w = SP["tri_size"] + 4 if any(e.ext for e in evs) else 0
    cell_w = tri_w + t_w + 6 + val_w
    crole = "sub"
    if cell_w > W:                          # narrowest cards: cells step down to 12 px before anything drops
        crole = "meta"
        t_w = cv.measure("12:59 pm", "meta") * 1.04 + 1
        val_w = val_w * 12 / 14 + 1
        cell_w = tri_w + t_w + 4 + val_w
    stack = W - lab_w < cell_w + 4          # narrow: the day label takes its own line
    x_cells = 0 if stack else lab_w
    avail = W - x_cells
    per_day = max([len(v) for v in by.values()] or [1])
    k = max(1, min(per_day, int((avail + 14) // (cell_w + 14))))
    # highs and lows: one column per kind (review 8), in the order the first event sets; a day with two
    # highs takes two lines in the highs column, never a high in the lows column
    kinds = []
    for e in evs:
        if e.ext and e.ext not in kinds:
            kinds.append(e.ext)
    by_kind = len(kinds) == 2 and all(e.ext for e in evs)
    if by_kind:
        if k >= 2:
            k = 2
            per_day = max([max(sum(1 for e in v if e.ext == kd) for kd in kinds) for v in by.values()] or [1])
        else:
            by_kind = False
    pitch = avail / k                       # columns spread over the width (fractions: no dead right side)
    lh = line_h("sub")
    gap = 8 if compact else 10
    y = top
    cv.d.meta["today"] = d0.isoformat()
    d = start or d0
    shown = 0
    maxd = days or 60
    while shown < maxd:
        evd = by.get(d, [])
        if d > last_day:
            rc = ["tpl", "not_published", {"day": ["d", "date", [d.isoformat(), "MMM d", tz]]}]
            if y + lh > bottom:
                break
            cv.text(["l", 0], y, rc, "meta", src="code", max_w=W)
            y += lh + gap
            break
        if by_kind:
            lines = max(1, max(sum(1 for e in evd if e.ext == kd) for kd in kinds))
        else:
            lines = max(1, -(-len(evd) // k))
        h = lines * lh + (lh if stack else 0)
        if y + h > bottom + 0.5:
            cv.d.meta["slack"] = max(cv.d.meta.get("slack", 0), h + gap)
            break
        if d == d0:
            cv.rect(["l", -6], ["r", -6], y - 4, y + h + 3, "row-hi", r=6)
        lrc, tiso = day_label_rc(ctx, d, d0)
        tok = "text" if d == d0 else "muted"
        if lrc is not None:
            cv.text(["l", 0], y, lrc, "sub", src="code", max_w=(W if stack else lab_w - 8), tok=tok)
        else:
            cv.time(["l", 0], y, tiso, "EEE d", "sub", tz="card", tok=tok)
        yc = y + (lh if stack else 0)
        seen_k: dict = {}
        for j, e in enumerate(evd):
            if by_kind:
                c_ = kinds.index(e.ext)
                r_ = seen_k.get(e.ext, 0)
                seen_k[e.ext] = r_ + 1
            else:
                r_, c_ = divmod(j, k)
            x = x_cells + c_ * pitch
            yy = yc + r_ * lh
            ids = []
            if e.ext:
                ids.append(cv.tri(["f", (x + SP["tri_size"] / 2) / W], yy + lh / 2, SP["tri_size"] - 1,
                                  "up" if e.ext == "hi" else "down", "muted"))
            if not e.all_day:
                tm = cv.time(["f", (x + tri_w) / W], yy, e.iso, "h:mm a", crole, tz="card", tok="text")
                ids.append(tm.id)
                cv.d.meta.setdefault("time_src", {})[tm.id] = ctx.cand.bindings.get("time")
            src = ["cell", vf, e.row, {}] if vf else (["raw", name_f, e.row] if name_f else None)
            if src:
                vx = x + tri_w + (t_w + (6 if crole == "sub" else 4) if not e.all_day else 0)
                tb = cv.text(["f", vx / W], yy, src, crole, src="data",
                             max_w=min(val_w, pitch - (vx - x) - 4) if k > 1 else W - vx, tok="text")
                if tb:
                    ids.append(tb.id)
            cv.live("past_dim", prims=ids, t=e.iso if not e.all_day else fmt.iso(
                fmt.day_start(e.day + timedelta(days=1), tz)))
        y += h + gap
        shown += 1
        d = d + timedelta(days=1)
    return y


class DayTable(BaseForm):
    name = "day_table"
    variants = ("events", "rows")
    slots = {"time": {"roles": ["time", "date"], "types": ["datetime", "date"], "required": True, "many": False},
             "value": {"roles": ["value", "measure"], "types": [], "required": False, "many": False},
             "name": {"roles": ["name", "kind"], "types": [], "required": False, "many": False},
             "hi": {"roles": ["range_hi"], "types": [], "required": False, "many": False},
             "lo": {"roles": ["range_lo"], "types": [], "required": False, "many": False},
             "secondary": {"roles": ["secondary"], "types": [], "required": False, "many": True}}
    sibling = None
    record_form = True
    intent_of = "plan"
    PLANS = {v: all_plans(_plan) for v in variants}
    REJECTS = {v: {"W1R1": "too_short"} for v in variants}

    def match(self, rec, prof):
        if rec.kind not in ("records", "events", "series"):
            return []
        R = RView(rec)
        tf = time_field(R)
        if tf is None:
            return []
        out = []
        vf = value_field(R)
        nm = R.first("name", "kind")
        dense = prof.time is not None and prof.time.grain in ("second", "minute", "quarter_hour", "hour") and \
            "alternating_extrema" not in (prof.signatures or [])
        if not dense and (tf.type == "datetime" and (vf or nm) and "dated_rows" not in (prof.signatures or []) or
                          (tf.type == "datetime" and "alternating_extrema" in (prof.signatures or []))):
            b = {"time": tf.name}
            if vf:
                b["value"] = vf.name
            elif nm:
                b["name"] = nm.name
            out.append({"variant": "events", "bindings": b, "params": {}})
        if tf.type == "date" or "dated_rows" in (prof.signatures or []) and tf.type != "datetime":
            b = {"time": tf.name}
            if nm:
                b["name"] = nm.name
            hi, lo = R.first("range_hi"), R.first("range_lo")
            if hi and lo:
                b["hi"], b["lo"] = hi.name, lo.name
            if vf and not (hi and lo):
                b["value"] = vf.name
            used = {tf.name, b.get("name"), b.get("hi"), b.get("lo"), b.get("value")}
            b["secondary"] = [f.name for f in rec.fields if f.name not in used
                              and f.role not in ("link", "link_secondary", "ignore", "image_url", "as_of", "unknown")
                              and f.type not in ("url", "lat", "lon")][:4]
            out.append({"variant": "rows", "bindings": b, "params": {}})
        return out

    def covers(self, cand, prof, sclass):
        return ["each_day", "times", "height_value", "extreme_high", "extreme_low", "next"]

    def describes(self, cand, rec):
        return "One row per day starting today, today highlighted"

    def summary(self, cand, rec, now):
        R = RView(rec)
        b = cand.bindings
        lead = b.get("value") or b.get("hi") or b.get("name")
        if not rec.rows or not lead:
            return f"{len(rec.rows)} entries by day"
        vals = "; ".join(R.text(i, lead) for i in range(min(5, len(rec.rows))))
        return f"{len(rec.rows)} entries by day · {R.f[lead].label}: {vals}"

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b = ctx.R, ctx.cand.bindings
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        allev = events(R, b["time"], b.get("value"))
        d0 = today(ctx.now, ctx.tz)
        drop = ctx.rung.get("drop", 0)
        days = max(1, {1: 3, 2: 7, 3: 14}[ctx.rows] - drop)
        evs = [e for e in allev if e.day >= d0]
        is_history = False
        if not evs and allev:
            # history only: the most recent days, oldest first, ending at the last published day
            recent = sorted({e.day for e in allev})[-days:]
            evs = [e for e in allev if e.day >= recent[0]]
            is_history = True
        if not evs:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        # fix round 1a-7 (class P2): a window cut ("this weekend") can already leave evs
        # starting after d0 — walk day_rows from the first day that HAS data, never
        # synthesizing the empty days between today and the window the ask named.
        start = min(e.day for e in evs)
        if ctx.cand.variant == "events":
            day_rows(ctx, cv, evs, y0 + 4, y1, days=days, name_f=b.get("name"), start=start)
        else:
            self.rows(ctx, cv, evs, y0 + 4, y1, days, history=is_history)
        cv.d.state = "many"

    def rows(self, ctx, cv, evs, top, bottom, days, history=False):
        """One row per date: day label | name | value columns (right-aligned, headed by their
        labels so a column of times or numbers is never anonymous)."""
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        d0 = today(ctx.now, ctx.tz)
        narrow = ctx.wc == "W1"
        shown = evs[:days]
        # monthly / yearly rows read 'Aug 2026' / '2026', never a weekday (round 1: labels as coarse as the data)
        dfmt = fmt.coarse_fmt(fmt.median_gap([e.day.isoformat() for e in evs]), "EEE d")
        lab_w = max(cv.measure(s, "sub") for s in ("Tomorrow", "+13 days", fmt.WIDEST[dfmt])) * 1.04 + 10
        if narrow:
            lab_w = max(cv.measure("Tomorrow", "meta"), cv.measure(fmt.WIDEST[dfmt], "meta")) * 1.04 + 4
        # value columns in reading order: range, value, then the other fields
        cols = []
        if b.get("hi"):
            cols.append(("rng", None, max(cv.measure(cv.run(self._rng(R, b, e.row)), "sub") for e in shown) * 1.04 + 2,
                         ["join", " / ", [["label", b["hi"]], ["label", b["lo"]]]]))
        elif b.get("value"):
            cols.append(("val", b["value"], max(cv.measure(cv.run(["cell", b["value"], e.row, {}]), "sub")
                                                for e in shown) * 1.04 + 2, ["label", b["value"]]))
        for f in (b.get("secondary") or []):
            if R.f[f].type == "datetime":
                fw = max(cv.measure(fmt.format_time(R.cell(e.row, f), "h:mm a", ctx.tz), "sub")
                         for e in shown if R.cell(e.row, f)) * 1.04 + 2 if any(R.cell(e.row, f) for e in shown) else 0
            else:
                fw = max([cv.measure(cv.run(fact_rc(R, f, e.row)), "sub") for e in shown] or [0]) * 1.04 + 2
            if fw > 2:
                cols.append(("sec", f, fw, ["label", f]))
        gap = 14
        name_min = 90 if b.get("name") else 0
        if cols and not narrow and cols[0][2] > W - lab_w - 8:
            narrow = True                 # the first value does not fit beside the day: stack them
            lab_w = cv.measure("Tomorrow", "meta") * 1.04 + 4
        room = W - (0 if narrow else lab_w) - name_min
        keep, used = [], 0.0
        for c in cols:
            hw = cv.measure(cv.run(c[3]), "meta") * 1.04 + 2
            w = max(c[2], hw) if hw <= c[2] * 1.5 else c[2]          # a long header ellipsizes, never widens
            lim = room if keep else room + name_min          # the first value column outranks the name
            if used + w + (gap if keep else 0) > lim:
                break
            keep.append((c, w))
            used += w + (gap if len(keep) > 1 else 0)
        # right offsets, last column flush right
        offs, r = [], 0.0
        for c, w in reversed(keep):
            offs.append(r)
            r += w + gap
        offs = list(reversed(offs))
        lh = line_h("sub")
        row_h = lh + (12 if ctx.rows > 1 else 6) + (line_h("meta") if narrow else 0)
        y = top
        if keep and keep[0][0][0] == "rng":
            keep = [(keep[0][0][:3] + (None,), keep[0][1])] + keep[1:]
        if keep and not (len(keep) == 1 and keep[0][0][0] == "rng"):
            for (c, w), off in zip(keep, offs):
                if c[3] is not None:
                    cv.text(["r", off], y, c[3], "meta", src="key", max_w=w, anchor="end")
            y += line_h("meta") + 4
        for e in shown:
            if y + row_h - (12 if ctx.rows > 1 else 6) > bottom + 0.5:
                break
            if e.day == d0:
                cv.rect(["l", -6], ["r", -6], y - 4, y + row_h - (12 if ctx.rows > 1 else 6) + 3, "row-hi", r=6)
            lrc, tiso = day_label_rc(ctx, e.day, d0) if dfmt == "EEE d" else (None, e.day.isoformat())
            tok = "text" if e.day == d0 or history else "muted"
            role = "meta" if narrow else "sub"
            ids = []
            if lrc is not None:
                ids.append(cv.text(["l", 0], y, lrc, role, src="code", max_w=lab_w - 4, tok=tok).id)
            else:
                ids.append(cv.time(["l", 0], y, tiso, dfmt, role, tz="card", tok=tok).id)
            if narrow:                       # label line, then the values under it
                y += line_h("meta")
            for (c, w), off in zip(keep, offs):
                kind, f = c[0], c[1]
                if kind == "rng":
                    tb = cv.text(["r", off], y, self._rng(R, b, e.row), "sub", src="data", max_w=w, anchor="end", tok="text")
                elif R.cell(e.row, f) is None:
                    continue
                elif R.f[f].type == "datetime":
                    tb = cv.time(["r", off], y, R.cell(e.row, f), "h:mm a", "sub", tz="card", anchor="end", tok="text")
                else:
                    tb = cv.text(["r", off], y, fact_rc(R, f, e.row) if kind == "sec" else ["cell", f, e.row, {}],
                                 "sub", src="data", max_w=w, anchor="end", tok="text")
                if tb:
                    ids.append(tb.id)
            if b.get("name") and R.cell(e.row, b["name"]) is not None:
                nx = 0 if narrow else lab_w
                nw = W - nx - (used + gap if keep else 0)
                if nw > 40:
                    tb = cv.text(["l", nx], y, ["raw", b["name"], e.row], "sub", src="data", max_w=nw, tok="text")
                    ids.append(tb.id)
            if not history:
                cv.live("past_dim", prims=ids, t=fmt.iso(fmt.day_start(e.day + timedelta(days=1), ctx.tz)))
            y += row_h - (line_h("meta") if narrow else 0)
        cv.d.meta["today"] = d0.isoformat()
        return y

    @staticmethod
    def _rng(R, b, i):
        return ["join", " / ", [["cell", b["hi"], i, {}], ["cell", b["lo"], i, {}]]]


FORM = DayTable()
