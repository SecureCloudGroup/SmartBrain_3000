"""`entity_list`: status records. 0 rows = the calm designed sentence + checked time;
1 row = a spotlight (badge, name, facts); n rows = severity-sorted rows (badge · name ·
metric) with healthy rows summarised ("+8 more · Good Service"). Every span is valid:
row counts change every tick, so each span has designed 0/1/few/many states.
"""
from __future__ import annotations

from .. import fmt
from ..base import (
    BaseForm,
    all_plans,
    badge,
    badge_w,
    calm,
    fact_rc,
    line_h,
    more_line,
    rows_capacity,
)
from ..ctx import SP, Canvas, LayoutCtx, shift
from ..lint import link_ok
from ..rec import R as RView
from ..rec import fact_ok, is_numeric


def _plan(w, r):
    return f"{'narrow' if w in ('W1',) else 'rows'}{r}"


class EntityList(BaseForm):
    name = "entity_list"
    variants = ("status", "matchup", "grouped")
    slots = {"name": {"roles": ["name"], "types": ["text", "category", "identifier"], "required": True, "many": False},
             "status": {"roles": ["status", "severity", "kind"], "types": [], "required": False, "many": False},
             "metrics": {"roles": ["value", "secondary", "count", "severity"], "types": [], "required": False, "many": True},
             "meta": {"roles": ["meta", "group"], "types": [], "required": False, "many": True},
             "link": {"roles": ["link"], "types": ["url"], "required": False, "many": False},
             "score_a": {"roles": ["score_a"], "types": [], "required": False, "many": False},
             "score_b": {"roles": ["score_b"], "types": [], "required": False, "many": False},
             "name_b": {"roles": ["name_b"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "monitor"
    PLANS = {v: all_plans(_plan) for v in variants}

    def match(self, rec, prof):
        if rec.kind not in ("records", "events"):
            return []
        R = RView(rec)
        nm = R.first("name")
        st = R.first("status", "severity") or next((f for f in rec.fields if f.role == "kind" and f.type == "category"), None)
        if nm is None:
            return []
        sa, sb, nb = R.first("score_a"), R.first("score_b"), R.first("name_b")
        b = {"name": nm.name}
        if sa and sb and nb:
            b.update(score_a=sa.name, score_b=sb.name, name_b=nb.name)
            if st:
                b["status"] = st.name
            return [{"variant": "matchup", "bindings": b, "params": {}}]
        if st is None:
            return []
        b["status"] = st.name
        b["metrics"] = [f.name for f in sorted(rec.fields, key=lambda g: 0 if g.role == "value" else 1)
                        if f.name not in (nm.name, st.name) and is_numeric(f)
                        and f.role in ("value", "secondary", "count", "severity") and fact_ok(f, rec)][:4]
        b["meta"] = [f.name for f in rec.fields if f.role in ("meta",) and f.type in ("text", "category")
                     and fact_ok(f, rec)][:1]
        lk = R.first("link")
        if lk:
            b["link"] = lk.name
        grouped = rec.row_meta is not None and any(m.group for m in rec.row_meta)
        return [{"variant": "grouped" if grouped else "status", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        return ["now", "count", "yesno", "where" if False else "now"]

    def describes(self, cand, rec):
        return "Status list: the worst first, a spotlight for one, a calm line when nothing is active"

    def summary(self, cand, rec, now):
        R = RView(rec)
        n = len(rec.rows)
        if n == 0:
            return "Nothing active"
        b = cand.bindings
        names = ", ".join(f"{R.text(i, b['name'])}" + (f" ({R.text(i, b['status'])})" if b.get("status") else "")
                          for i in self._order(R, b)[:5])
        return f"{n} items: {names}"

    # ------------------------------------------------------------------ helpers
    def _sev(self, R, b, i):
        st = b.get("status")
        if st is None:
            return 0
        f = R.f[st]
        v = R.cell(i, st)
        bl = (R.rec.flags.get("baseline") or {}) if hasattr(R, "rec") else {}
        if f.ordinal and bl.get("field") == st:
            return 0 if v == f.ordinal[0] else 1   # a baseline state: every exception leads, in source order
        if f.ordinal and v in f.ordinal:
            return f.ordinal.index(v)
        if is_numeric(f) and R.num(i, st) is not None:
            return R.num(i, st)
        return 0

    def _order(self, R, b):
        idx = list(range(len(R.rows)))
        st = b.get("status")
        if st and (R.f[st].ordinal or is_numeric(R.f[st])):
            m0 = (b.get("metrics") or [None])[0]
            idx.sort(key=lambda i: (-self._sev(R, b, i), -(R.num(i, m0) or 0) if m0 else 0, i))
        return idx

    def _healthy(self, R, b, i):
        st = b.get("status")
        f = R.f.get(st) if st else None
        if f is not None and len(R.rows) >= 3 and len({R.cell(k, st) for k in range(len(R.rows))}) == 1:
            return True               # round 3: a state every row shares tells no row apart: no badge on each
        return bool(f and f.ordinal and R.cell(i, st) == f.ordinal[0])

    def _facts_rc(self, R, b, i, k=4):
        from ..base import labelled_rc
        # a number with no unit is always named ('Intensity 45'), never a bare figure
        parts = [(fact_rc(R, m, i) if (R.f[m].unit or R.f[m].currency) else labelled_rc(R, m, i))
                 for m in (b.get("metrics") or [])[:k] if R.cell(i, m) is not None]
        return ["join", " · ", parts] if parts else None

    def _filtered_line(self, ctx, cv, y):
        """Rows the user's words filtered out are counted, never silently dropped."""
        part = ctx.rec.flags.get("partition") or {}
        n = part.get("dropped") or 0
        op = next((f.get("op") for f in part.get("filters") or [] if isinstance(f, dict)), None)
        tid = {"active": "filtered_inactive", "requested": "filtered_request"}.get(op, "filtered_out")
        if n and y + line_h("meta") <= ctx.body["y1"] + 0.5:
            cv.text(["l", 0], y, ["tpl", tid, {"n": ["d", "count", [n]]}], "meta", src="code",
                    max_w=ctx.W, tok="muted")

    def _meta_rc(self, R, b, i):
        parts = [["raw", m, i] for m in (b.get("meta") or []) if R.cell(i, m) is not None]
        return ["join", " · ", parts] if parts else None

    def _hit(self, cv, R, b, i, pid):
        if b.get("link") and pid:
            u = R.cell(i, b["link"])
            if u and link_ok(str(u)):
                cv.d.hitmap.append({"prim": pid, "label": "Open", "href": str(u)})

    # ------------------------------------------------------------------ layout
    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        n = len(R.rows)
        if n == 0:
            calm(ctx, cv, [["tpl", "none_now", {}]])
            return
        order = self._order(R, b)
        if ctx.cand.variant == "matchup":
            return self._matchup(ctx, cv, order)
        calm_ok = bool((ctx.rec.flags.get("baseline") or {}).get("calm"))
        if n == 1 or (calm_ok and all(self._healthy(R, b, i) for i in order) and n > 1):
            if n > 1:        # all healthy: one calm summary line + when it was checked, balanced as one unit
                st = ["raw", b["status"], order[0]]
                m = cv.mark()
                bx = cv.text(["c", 0], y0, ["tpl", "all_status", {"n": ["d", "count", [n]], "status": st}], "display",
                             src="data", max_w=W - 8, max_lines=2, anchor="middle")
                y = (bx.bottom + 4) if bx else y0
                last = y
                a = ctx.rec.context.as_of or ctx.rec.context.fetched_at
                if a and y + line_h("meta") <= y1 + 0.5:
                    from .. import text as TX
                    from ..ctx import ty
                    lab = ["tpl", "checked", {}]
                    w1 = cv.measure(cv.run(lab), "meta") + 4
                    tw = TX.width(fmt.WIDEST["h:mm a"], *ty("meta")[:2]) * (1 + TX.MARGIN) + 0.5
                    total = w1 + tw
                    cv.text(["c", -total / 2], y, lab, "meta", src="code", max_w=w1 * (1 + TX.MARGIN) + 1)
                    cv.time(["c", -total / 2 + w1], y, a, "h:mm a", "meta", tz="viewer")
                    last = y + line_h("meta")
                cv.balance(m, y0, last, y0, y1)
                cv.d.state = "many"
                cv.d.meta["calm"] = True
                return
            return self._spotlight(ctx, cv, order[0])
        return self._rows(ctx, cv, order)

    def _spotlight(self, ctx, cv, i):
        m = cv.mark()
        self._spot(ctx, cv, i)
        from ..base import center_block
        from ..lint import text_box
        right = max([text_box(p, ctx.W)[2] for p in cv.d.prims[m:] if p["k"] in ("text", "time")] or [0])
        if ctx.W >= 400 and right < 0.65 * ctx.W:
            center_block(ctx, cv, m)

    def _spot(self, ctx, cv, i):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y = ctx.body["y0"]
        x = ["l", 0]
        st = b.get("status")
        sw = 0
        if st and R.cell(i, st) is not None and not self._healthy(R, b, i):
            bw = badge_w(cv, cv.run(["raw", st, i]))
            if ctx.wc == "W1" and bw > W * 0.45:
                bb = badge(cv, x, y, ["raw", st, i], max_w=W)
                y = bb.bottom + 6
            else:
                bb = badge(cv, x, y + 3, ["raw", st, i], max_w=W * 0.45)
                sw = bb.w + 8
        nb = cv.text(shift(x, sw), y, ["raw", b["name"], i], "display", src="data", max_w=W - sw,
                     max_lines=2 if ctx.rows > 1 else 1)
        self._hit(cv, R, b, i, nb.id if nb else None)
        y = (nb.bottom if nb else y) + 4
        f = self._facts_rc(R, b, i, k=max(1, 4 - ctx.rung.get("drop", 0)))
        if f:
            fb = cv.text(["l", 0], y, f, "sub", src="data", max_w=W, max_lines=2 if ctx.wc in ("W1", "W2") else 1,
                         tok="text")
            y = fb.bottom + 4 if fb else y
        if ctx.rows > 1 or ctx.wc in ("W4", "W5", "W6"):
            meta = [["raw", m, i] for m in (b.get("meta") or []) if R.cell(i, m) is not None]
            from ..base import fit_parts
            rc = fit_parts(cv, meta, "meta", W)
            if rc:
                mb = cv.text(["l", 0], y + 2, rc, "meta", src="data", max_w=W)
                y = (mb.bottom if mb else y) + 2
        self._filtered_line(ctx, cv, y + 2)
        cv.d.state = "one"

    def _rows(self, ctx, cv, order):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        drop = ctx.rung.get("drop", 0)
        st = b.get("status")
        row_h = 30 if ctx.phone else 32
        more_h = line_h("meta") + 4
        # unhealthy first; trailing healthy rows fold into "+N more · {status}"
        bad = [i for i in order if not self._healthy(R, b, i)]
        good = [i for i in order if self._healthy(R, b, i)]
        narrow = ctx.wc == "W1"
        if st and not narrow:
            wmax = max([badge_w(cv, cv.run(["raw", st, i])) for i in bad[:6]] or [0])
            narrow = wmax > W * 0.4
        if narrow:
            row_h = 40
        show = bad + good
        # height buys detail: when every row fits with a second line, each row carries its facts
        # (metrics · meta) under the name instead of leaving the card hollow (review 5)
        detail = False
        if not narrow:
            has_facts = any(self._facts_rc(R, b, i) or self._meta_rc(R, b, i) for i in show[:8])
            if has_facts and len(show) * (row_h + 18) <= (y1 - y0) and ctx.rows >= 2:
                detail, row_h = True, row_h + 18
        k, hidden = rows_capacity(y1 - y0, row_h, len(show), more_h)
        k = max(1, k - drop)
        hidden = len(show) - k
        order = show
        bcol = 0
        if st and not narrow:
            widths = [badge_w(cv, cv.run(["raw", st, i])) for i in show[:k] if not self._healthy(R, b, i)]
            bcol = min(max(widths or [0]), W * 0.4) + 8 if widths else 0
        m0 = (b.get("metrics") or [None])[0]
        show_metric = m0 is not None and not narrow and not detail
        y = y0
        for i in show[:k]:
            healthy = self._healthy(R, b, i)
            ty_ = y + (row_h - 19) / 2 - 3
            if narrow:
                nb = cv.text(["l", 0], y, ["raw", b["name"], i], "item", src="data", max_w=W)
                if st and R.cell(i, st) is not None:
                    cv.text(["l", 0], y + 19, ["raw", st, i], "meta", src="data", max_w=W,
                            tok="muted" if healthy else "warn")
                self._hit(cv, R, b, i, nb.id if nb else None)
                y += row_h
                continue
            if st and R.cell(i, st) is not None and not healthy:
                badge(cv, ["l", 0], y + (row_h - SP["badge_h"]) / 2 - 3, ["raw", st, i], max_w=bcol - 8)
            mw = 0
            if show_metric and R.cell(i, m0) is not None:
                ms = cv.run(fact_rc(R, m0, i))
                mw = cv.measure(ms, "sub") * 1.04 + 2
                cv.text(["r", 0], ty_, fact_rc(R, m0, i), "sub", src="data", max_w=mw,
                        anchor="end", tok="muted" if healthy else "text", s=ms)
            elif healthy and st and R.cell(i, st) is not None:
                ms = cv.run(["raw", st, i])
                mw = cv.measure(ms, "sub") * 1.04 + 2
                if mw < W * 0.45:
                    cv.text(["r", 0], ty_, ["raw", st, i], "sub", src="data", max_w=mw, anchor="end", s=ms)
                else:
                    mw = 0
            if detail:
                ty_ = y + (32 - 19) / 2 - 3
            nb = cv.text(["l", bcol], ty_, ["raw", b["name"], i], "item", src="data",
                         max_w=W - bcol - (mw + 12 if mw else 0), tok="muted" if healthy else "text")
            self._hit(cv, R, b, i, nb.id if nb else None)
            if detail:
                from ..base import fit_parts
                fr = self._facts_rc(R, b, i)
                parts = (fr[2] if fr else []) + ((self._meta_rc(R, b, i) or ["", "", []])[2])
                rc = fit_parts(cv, parts, "meta", W - bcol)
                if rc:
                    cv.text(["l", bcol], ty_ + 20, rc, "meta", src="data", max_w=W - bcol, tok="muted")
            y += row_h
        if hidden > 0:
            rest = order[k:]
            if good and all(self._healthy(R, b, i) for i in rest):
                more_line(cv, ["l", 0], y + 2, hidden, W, status_rc=["raw", st, rest[0]])
            else:
                more_line(cv, ["l", 0], y + 2, hidden, W)
            cv.d.meta["hidden_items"] = hidden
            y += line_h("meta") + 4
        self._filtered_line(ctx, cv, y + 2)
        cv.d.state = "many" if hidden else "few"

    def _matchup(self, ctx, cv, order):
        _R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        row_h = 44 if ctx.rows > 1 else 40
        k, hidden = rows_capacity(y1 - y0, row_h, len(order), line_h("meta") + 4)
        k = max(1, k - ctx.rung.get("drop", 0))
        hidden = len(order) - k
        y = y0
        for i in order[:k]:
            sa, sbb = cv.run(["cell", b["score_a"], i, {}]), cv.run(["cell", b["score_b"], i, {}])
            sw = max(cv.measure(sa, "row-strong"), cv.measure(sbb, "row-strong")) * 1.04 + 2
            cv.text(["l", 0], y, ["raw", b["name"], i], "item", src="data", max_w=W - sw - 8)
            cv.text(["r", 0], y, ["cell", b["score_a"], i, {}], "row-strong", src="data", max_w=sw, anchor="end", px=14)
            cv.text(["l", 0], y + 19, ["raw", b["name_b"], i], "item", src="data", max_w=W - sw - 8)
            cv.text(["r", 0], y + 19, ["cell", b["score_b"], i, {}], "row-strong", src="data", max_w=sw, anchor="end", px=14)
            y += row_h
        if hidden > 0:
            more_line(cv, ["l", 0], y, hidden, W)
        cv.d.state = "many" if hidden else ("one" if len(order) == 1 else "few")


FORM = EntityList()
