"""`stat`: one primary fact (number, word, yes/no, threshold, countdown, count-up).

Plans (CONTRACTS.md 5.3), by width class x rows. `F` = extra facts (reference, open,
range, secondaries), `S` = the record has a series part (structure only):
  W1R1 hero(+delta%)     W1R2 + track + KV rows (F)      W1R3 spark (S)
  W2R1 hero + delta (+ track when the body has room)     W2R2 + track + KV 2x2 (F>=2|S)   W2R3 history (S)
  W3/W4 R1 split: hero+delta | track / KV / spark        R2 + track + KV 4-up + spark     R3 chart (S)
  W5/W6 R1 hero | KV | spark (S | F>=4)                   R2/R3 chart (S)
A single fact with nothing else uses the `single` plan (hero + label centred as one unit).
"""
from __future__ import annotations

from .. import fmt
from ..base import (
    BaseForm,
    all_plans,
    delta_parts,
    delta_row,
    hero,
    kv_cells,
    kv_line_rows,
    line_h,
    part_plot,
    series_of,
)
from ..ctx import VIZ, Canvas, LayoutCtx
from ..rec import R as RView
from ..rec import fact_ok, is_numeric
from ..types import Reject

REF_ROLES = ("reference", "open", "range_lo", "range_hi")


def _sub_label(cv: Canvas, ctx: LayoutCtx, y: float, rc: list, max_w: float):
    """The label under the hero. A card titled by its one field's name would say it twice
    (``title_echo``): the label is left out and the title carries the meaning."""
    if cv.run(rc).strip().casefold() == str(ctx.inp.title or "").strip().casefold():
        return None
    return cv.text(["l", 0], y, rc, "sub", src="key", max_w=max_w)


def _plan(w: str, r: int):
    if w == "W1":
        return {1: "hero", 2: "stack", 3: "spark"}[r]
    if w == "W2":
        return {1: "hero", 2: "full", 3: "history"}[r]
    if w in ("W3", "W4"):
        return {1: "split", 2: "full", 3: "chart"}[r]
    return {1: "tri", 2: "chart", 3: "chart"}[r]


def facts(R: RView, b: dict) -> list:
    """Secondary facts, each label once: a fact whose display label repeats the value's, the change's or
    an earlier fact's label is a near-duplicate reading ('Price' beside the price) and is left out."""
    out = []
    seen = {R.f[b["value"]].label.lower()} if b.get("value") in R.f else set()
    for r in ("delta", "delta_pct"):
        if b.get(r) in R.f:
            seen.add(R.f[b[r]].label.lower())
    for role in ("reference", "open", "range_hi", "range_lo"):
        f = b.get(role)
        if f and R.cell(0, f) is not None:
            seen.discard(R.f[f].label.lower())
    raw = _facts(R, b)
    for it in raw:
        lab = R.f[it[1][1]].label.lower()
        if lab in seen:
            continue
        seen.add(lab)
        out.append(it)
    return out


def _facts(R: RView, b: dict) -> list:
    out = []
    for role in ("reference", "open", "range_hi", "range_lo"):
        f = b.get(role)
        if f and R.cell(0, f) is not None:
            out.append((["label", f], ["cell", f, 0, {"compact": True}], "data"))
    for f in b.get("secondary", []) or []:
        if R.cell(0, f) is not None:
            out.append((["label", f], ["cell", f, 0, {"compact": True}], "data"))
    return out


class Stat(BaseForm):
    name = "stat"
    variants = ("number", "word", "yesno", "threshold", "countdown", "count_up", "extrapolate")
    slots = {"value": {"roles": ["measure", "value"], "types": ["number", "quantity", "currency", "percent",
                                                                 "category", "text", "bool", "datetime", "date"],
                       "required": True, "many": False},
             "reference": {"roles": ["reference"], "types": [], "required": False, "many": False},
             "delta": {"roles": ["delta"], "types": [], "required": False, "many": False},
             "delta_pct": {"roles": ["delta_pct"], "types": [], "required": False, "many": False},
             "range_lo": {"roles": ["range_lo"], "types": [], "required": False, "many": False},
             "range_hi": {"roles": ["range_hi"], "types": [], "required": False, "many": False},
             "open": {"roles": ["open"], "types": [], "required": False, "many": False},
             "goal": {"roles": ["goal"], "types": [], "required": False, "many": False},
             "secondary": {"roles": ["secondary", "value", "count"], "types": [], "required": False, "many": True}}
    params_space = {}
    sibling = None
    record_form = False
    intent_of = "now"
    PLANS = {v: all_plans(_plan) for v in variants}

    # ------------------------------------------------------------------ match
    def match(self, rec, prof):
        if rec.kind != "measure" and not (rec.kind == "records" and any(f.role == "measure" for f in rec.fields)):
            return []                      # structure (a record the binder read as ONE reading), never the count
        R = RView(rec)
        v = R.first("measure") or next((f for f in rec.fields if f.role == "value"), None)
        b = {}
        variant = None
        if v is not None:
            if v.type == "bool":
                variant = "yesno"
            elif v.type in ("datetime", "date"):
                variant = "countdown"
            elif is_numeric(v):
                variant = "threshold" if R.first("goal") is not None else "number"
            else:
                variant = "word"
        else:
            t = R.first("time", "date")
            if t is not None and rec.kind == "measure":
                v, variant = t, "countdown"
        if v is None:
            return []
        b["value"] = v.name
        for role in ("reference", "delta", "delta_pct", "range_lo", "range_hi", "open", "goal"):
            f = R.first(role)
            if f is not None and f.name != v.name:
                b[role] = f.name
        used = set(b.values())
        b["secondary"] = [f.name for f in rec.fields if (f.name not in used and f.role in ("secondary", "count")
                          or (f.name not in used and f.role == "value" and is_numeric(f))) and fact_ok(f, rec)][:6]
        s = series_of(rec)
        if s:
            b["series"] = s[0]
        if variant == "countdown" and prof is not None and prof.time and not prof.time.future_events \
                and prof.time.past_events:
            variant = "count_up"
        return [{"variant": variant, "bindings": b, "params": {}}]

    # ------------------------------------------------------------------ structure
    def _shape(self, cand, rec):
        RView(rec)
        b = cand.bindings
        nf = sum(1 for r in REF_ROLES if b.get(r)) + len(b.get("secondary") or [])
        has_delta = bool(b.get("delta") or b.get("delta_pct") or (b.get("reference") and cand.variant in ("number", "threshold")))
        return nf, has_delta, bool(b.get("series"))

    def structural(self, cand, rec, prof, sclass, plan):
        nf, hd, S = self._shape(cand, rec)
        w, r = sclass[:2], int(sclass[3])
        single = nf == 0 and not hd and not S
        if plan in ("spark", "history", "chart") and not S:
            return Reject("needs_series")
        if single:
            if r == 1 and w in ("W1", "W2", "W3", "W4"):
                return "single"
            return Reject("needs_series")
        if plan == "stack" and nf == 0 and not S:
            return Reject("needs_series")
        if plan == "full" and nf < 2 and not S:
            return Reject("needs_series")
        if plan == "tri" and not (S or nf >= 4):
            return Reject("needs_series")
        return plan

    def covers(self, cand, prof, sclass):
        c = ["now", "height_value"]
        if cand.variant == "yesno":
            c.append("yesno")
        if cand.variant in ("threshold",):
            c.append("threshold")
        if cand.variant == "countdown":
            c += ["next", "times"]
        r = int(sclass[3])
        if cand.bindings.get("range_lo") and (r >= 2 or sclass[:2] != "W1"):
            c += ["extreme_high", "extreme_low"]
        if cand.bindings.get("series") and (r >= 2 or sclass[:2] in ("W3", "W4", "W5", "W6")):
            c.append("trend")
        return c

    def describes(self, cand, rec):
        R = RView(rec)
        v = R.f[cand.bindings["value"]]
        extra = [r for r in ("reference", "range_lo", "delta") if cand.bindings.get(r)]
        return f"One big {v.label} value" + (f" with {', '.join(extra)}" if extra else "") + \
            (" and a history line" if cand.bindings.get("series") else "")

    def summary(self, cand, rec, now):
        R = RView(rec)
        v = cand.bindings["value"]
        if not rec.rows:
            return f"{R.f[v].label}: no value"
        s = f"{R.f[v].label}: {R.text(0, v)}"
        dp = delta_parts(R, cand.bindings) if cand.variant in ("number", "threshold") else None
        if dp and "d" in dp:
            s += f", change {fmt.num(dp['d'], 2, sign=True)}"
        return s

    # ------------------------------------------------------------------ layout
    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        cand = ctx.cand
        plan = self.structural(cand, ctx.rec, ctx.prof, ctx.span.sclass, plan)
        if isinstance(plan, Reject):
            plan = "single"
        cv.d.plan = plan
        R = ctx.R
        b = cand.bindings
        body = ctx.body
        W = ctx.W
        y0, y1 = body["y0"], body["y1"]
        drop = ctx.rung.get("drop", 0)
        vrc = self.value_recipe(ctx)
        dp = delta_parts(R, b) if cand.variant in ("number", "threshold") else None
        fs = facts(R, b)
        sub_rc = ["label", b["value"]]
        ref_label = ["tpl", "vs", {"label": ["label", b["reference"]]}] if b.get("reference") else None
        ser = series_of(ctx.rec) if b.get("series") else None

        if plan == "single":
            hb = hero(cv, ["l", 0], y0, vrc, W)
            bottom = hb.bottom if hb else y0
            if cand.variant == "countdown":
                bottom = self._countdown_bind(ctx, cv, hb)
            if _sub_label(cv, ctx, bottom + 2, sub_rc, W) is None and hb is not None:
                cv.d.meta["calm"] = True   # one fact under its own title is the whole, designed card
            return

        # ------- a hero too wide for the split column takes the full width (same content, stacked)
        if plan in ("split", "tri") and not cv.fits(cv.run(vrc), "hero", (W * (0.5 if plan == "split" else 0.34)) - 8, 20):
            plan = "hero"
            cv.d.plan = plan
        # ------- left/primary column width
        if plan in ("split",):
            colw = W * 0.5 - 8
        elif plan == "tri":
            colw = W * 0.34 - 8
        else:
            colw = W
        hb = hero(cv, ["l", 0], y0, vrc, colw)
        y = hb.bottom if hb else y0
        if cand.variant == "countdown":
            y = self._countdown_bind(ctx, cv, hb)
        if dp and drop < 3:
            want_abs = ctx.wc != "W1" or ctx.rows > 1
            db = delta_row(cv, ["l", 0], y + 2, colw, dp, ref_label=ref_label if ctx.wc != "W1" else None,
                           want_abs=want_abs)
            if db:
                y = db.bottom + 2
        elif plan in ("hero", "stack", "split", "tri") and not dp:
            sb = _sub_label(cv, ctx, y + 2, sub_rc, colw)
            if sb:
                y = sb.bottom + 2

        has_range = b.get("range_lo") and b.get("range_hi") and R.num(0, b["range_lo"]) is not None \
            and R.num(0, b["range_hi"]) is not None
        if plan == "hero":
            room = y1 - y
            if has_range and room >= 30 and drop < 2:
                y = self.track(ctx, cv, 0.0, 1.0, y + 8, labels=True, refs_line=False)
            elif fs and room >= 20 and not has_range and drop < 2:
                it = fs[0]
                cv.text(["l", 0], y + 4, ["join", " ", [it[0], it[1]]], "meta", src="data", max_w=W)
            return

        if plan == "stack":        # phone half, 2 rows
            if has_range and drop < 2:
                y = self.track(ctx, cv, 0.0, 1.0, y + 10, labels=False, refs_line=False) + 4
            n = max(0, min(3, len(fs)) - max(0, drop - 2))
            if n:
                y = kv_line_rows(cv, ["l", 0], ["r", 0], y + 8, fs[:n], width=W)
            if ser and drop < 1 and y1 - y > 60:
                self.spark(ctx, cv, ser, 0.0, 1.0, y + 10, y1)
            return

        if plan == "split":
            right0 = 0.54
            top = y0 + 4
            if ser and drop < 1:
                self.spark(ctx, cv, ser, right0, 1.0, y0, min(y1, y0 + 88))
                if has_range and drop < 2 and y1 - y >= 30:
                    self.track(ctx, cv, 0.0, 0.5, y + 8, labels=True, refs_line=False)
            elif has_range and drop < 2:
                self.track(ctx, cv, right0, 1.0, top + 16, labels=True, refs_line=True)
            elif fs:
                cols = [["f", right0], ["f", right0 + (1 - right0) / 2]]
                kv_cells(cv, cols, top, fs[:4], col_w=W * (1 - right0) / 2 - 8, rows=2, row_gap=6)
            if y < y0 + 44 and dp is None and not fs:
                pass
            return

        if plan == "full":
            if has_range and drop < 3:
                y = self.track(ctx, cv, 0.0, 1.0, y + 10, labels=True, refs_line=False) + 4
            rest = [f for f in fs if not (has_range and f[1][1] in (b.get("range_lo"), b.get("range_hi")))]
            ncol = 4 if ctx.wc == "W4" else 2
            line_h("meta") + line_h("row-strong") + 8
            max_rows = 1 if ctx.wc == "W4" else 2
            nkv = min(len(rest), ncol * max_rows)
            if drop >= 2:
                nkv = min(nkv, ncol)
            if nkv:
                cols = [["f", i / ncol] for i in range(ncol)]
                y = kv_cells(cv, cols, y + 12, rest[:nkv], col_w=W / ncol - 10, rows=max_rows, row_gap=8)
            if ser and y1 - y >= 70 and drop < 1:
                self.spark(ctx, cv, ser, 0.0, 1.0, y + 12, y1, labels="minmax")
            return

        if plan in ("spark", "history", "chart"):
            if plan == "chart" and ctx.wc in ("W5", "W6") and fs:
                ncol = min(len(fs), 4)
                cols = [["f", 0.36 + i * (0.64 / ncol)] for i in range(ncol)]
                kv_cells(cv, cols, y0 + 4, fs[:ncol], col_w=W * 0.64 / ncol - 10, rows=1)
            elif plan == "chart" and fs and y1 - y > 240:
                ncol = 4 if ctx.wc == "W4" else 2
                cols = [["f", i / ncol] for i in range(ncol)]
                y = kv_cells(cv, cols, y + 10, fs[:ncol], col_w=W / ncol - 10, rows=1)
            self.spark(ctx, cv, ser, 0.0, 1.0, y + 12, y1, labels="minmax", min_share=True)
            return

        if plan == "tri":          # W5/W6 R1: hero | KV | spark
            if ser:
                kx0, kx1 = 0.36, 0.62
                self.spark(ctx, cv, ser, 0.66, 1.0, y0, y1, labels="minmax")
            else:
                kx0, kx1 = 0.36, 1.0
            ncol = 2 if ser else 3
            cols = [["f", kx0 + i * (kx1 - kx0) / ncol] for i in range(ncol)]
            kv_cells(cv, cols, y0 + 2, fs[:ncol * 2], col_w=W * (kx1 - kx0) / ncol - 10, rows=2, row_gap=6)
            return

    # ------------------------------------------------------------------ pieces
    def value_recipe(self, ctx):
        v = ctx.cand.bindings["value"]
        if ctx.cand.variant == "countdown":
            t = ctx.R.cell(0, v)
            return ["d", "countdown", [fmt.iso(ctx.now), t]] if t else ["raw", v, 0]
        return ["cell", v, 0, {}]

    def _countdown_bind(self, ctx, cv, hb):
        if hb is None:
            return ctx.body["y0"]
        t = ctx.R.cell(0, ctx.cand.bindings["value"])
        cv.live("countdown", prim=hb.id, t=t, fmt="in_dhm")
        return hb.bottom

    def track(self, ctx: LayoutCtx, cv: Canvas, fa: float, fb: float, y: float, *, labels: bool,
              refs_line: bool) -> float:
        """Range track (approved look): 4 px round track over the domain, lo..hi segment in
        viz-line, reference ticks, a ringed dot at the value, lo/hi labels under the ends."""
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        lo, hi = R.num(0, b["range_lo"]), R.num(0, b["range_hi"])
        v = R.num(0, b["value"])
        refs = [(r, R.num(0, b[r])) for r in ("open", "reference") if b.get(r) and R.num(0, b[r]) is not None]
        dom = [lo, hi] + ([v] if v is not None else []) + [x for _, x in refs]
        d0, d1 = min(dom), max(dom)
        pad = (d1 - d0) * 0.04 or 1
        d0, d1 = d0 - pad, d1 + pad

        def F(x):
            return fa + (x - d0) / (d1 - d0) * (fb - fa)
        th = VIZ["track_h"]
        cv.rect(["f", fa], ["f", fb], y - th / 2, y + th / 2, "viz-track", r=th / 2)
        cv.rect(["f", F(lo)], ["f", F(hi)], y - th / 2, y + th / 2, "viz-line", r=th / 2)
        for r, x in refs:
            cv.line(["f", F(x)], y - VIZ["ref_tick_h"] / 2, ["f", F(x)], y + VIZ["ref_tick_h"] / 2, "viz-axis",
                    w=VIZ["ref_tick_w"])
        if v is not None:
            cv.dot(["f", F(v)], y, VIZ["now_dot_r"], "viz-line", ring="viz-dot-ring", ring_w=VIZ["dot_ring"])
        bottom = y + VIZ["ref_tick_h"] / 2
        if labels:
            ly = y + 9
            lw = W * (fb - fa) / 2 - 6
            cv.text(["f", F(lo)], ly, ["cell", b["range_lo"], 0, {}], "tick", src="data", max_w=lw,
                        anchor="start" if F(lo) - fa < 0.2 else "middle")
            cv.text(["f", F(hi)], ly, ["cell", b["range_hi"], 0, {}], "tick", src="data", max_w=lw,
                        anchor="end" if fb - F(hi) < 0.2 else "middle")
            bottom = ly + line_h("tick")
        if refs_line and refs:
            parts = [["join", " ", [["label", b[r]], ["cell", b[r], 0, {}]]] for r, _ in refs]
            rc = ["join", " · ", parts]
            s = cv.run(rc)
            mw = W * (fb - fa)
            if cv.fits(s, "meta", mw):
                cv.text(["f", fa], bottom + 4, rc, "meta", src="data", max_w=mw)
                bottom += 4 + line_h("meta")
        return bottom

    def spark(self, ctx, cv, ser, fa, fb, top, bottom, labels="none", min_share=False):
        part_plot(ctx, cv, ser, fa, fb, top, bottom, labels=labels, min_share=min_share)


FORM = Stat()
