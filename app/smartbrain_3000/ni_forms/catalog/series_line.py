"""`series_line`: a time series. The mark is a line for a level and bars for an amount
per interval (Field.agg per_interval|sum). Variants: `single`, `multi` (<=3 series keyed
by series_id with a legend and end labels; more are counted, never guessed), `band`
(quantile columns: min/max edges + median).
Plans: W1 R1 latest + spark, R2 + delta + min/max labels, R3 + stats; W2 R1 hero+delta+spark,
R2 headline + plot with labels and ticks, R3 + stats; W3/W4 R1 hero | plot (>=45%), R2 full,
R3 full + stats; W5/W6 R1 headline | wide plot, R2 full, R3 full + stats.
`multi` rejects W1 at every row (multi_series_identity).
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime

from .. import fmt
from ..base import (
    BaseForm,
    all_plans,
    calm,
    delta_row,
    hero,
    kv_cells,
    line_h,
    line_plot,
    series_path,
    time_ticks,
)
from ..ctx import VIZ, Canvas, LayoutCtx, pbox
from ..rec import R as RView
from ..rec import is_numeric, ts


def _plan(w, r):
    if w in ("W1",):
        return {1: "spark", 2: "spark_labels", 3: "spark_stats"}[r]
    if w == "W2":
        return {1: "hero_spark", 2: "full", 3: "full_stats"}[r]
    if w in ("W3", "W4"):
        return {1: "split", 2: "full", 3: "full_stats"}[r]
    return {1: "split", 2: "full", 3: "full_stats"}[r]


def _plan_multi(w, r):
    return None if w == "W1" else _plan(w, r)


def _bins(R, cf: str, vf: str) -> bool:
    """A worded column that CLASSIFIES the reading: at least two classes, each covering its own stretch of
    values with no overlap (0-24 one word, 25-49 the next ...). Any other word beside the number is not its
    class and is not shown under it."""
    rng: dict = {}
    for i in range(len(R.rows)):
        c, v = R.cell(i, cf), R.num(i, vf)
        if c is None or v is None:
            continue
        lo, hi = rng.get(c, (v, v))
        rng[c] = (min(lo, v), max(hi, v))
    if len(rng) < 2:
        return False
    iv = sorted(rng.values())
    return all(a[1] < b[0] for a, b in itertools.pairwise(iv))


class SeriesLine(BaseForm):
    name = "series_line"
    variants = ("single", "multi", "band")
    slots = {"time": {"roles": ["time", "date"], "types": ["datetime", "date"], "required": True, "many": False},
             "value": {"roles": ["value", "measure", "secondary"], "types": [], "required": True, "many": False},
             "values": {"roles": ["secondary", "count", "value"], "types": [], "required": False, "many": True},
             "series_id": {"roles": ["series_id"], "types": [], "required": False, "many": False},
             "lo": {"roles": ["range_lo"], "types": [], "required": False, "many": False},
             "hi": {"roles": ["range_hi"], "types": [], "required": False, "many": False}}
    record_form = True
    intent_of = "trend"
    PLANS = {"single": all_plans(_plan), "multi": all_plans(_plan_multi), "band": all_plans(_plan)}
    REJECTS = {"multi": {f"W1R{r}": "multi_series_identity" for r in (1, 2, 3)}}

    def match(self, rec, prof):
        if rec.kind not in ("series", "records", "events"):
            return []
        R = RView(rec)
        tf = R.first("time", "date")
        vf = next((f for f in rec.fields if f.role in ("value", "measure") and is_numeric(f)), None)
        if tf is None:
            return []
        if vf is None and rec.kind == "series":
            # a series whose numeric columns all bound as secondaries: each column is a line
            nums = [f for f in rec.fields if is_numeric(f) and f.role in ("secondary", "count")]
            if not nums:
                return []
            vf = nums[0]
            same = [f.name for f in nums if f.unit == vf.unit][:3]
            if len(same) >= 2:
                return [{"variant": "multi", "bindings": {"time": tf.name, "value": vf.name, "values": same},
                         "params": {}}]
        if vf is None:
            return []
        b = {"time": tf.name, "value": vf.name}
        sid = R.first("series_id")
        if sid is not None:
            b["series_id"] = sid.name
            return [{"variant": "multi", "bindings": b, "params": {}}]
        lo, hi = R.first("range_lo"), R.first("range_hi")
        if lo and hi and "family_reduced_quantiles" in (rec.inferred or []):
            b.update(lo=lo.name, hi=hi.name)
            return [{"variant": "band", "bindings": b, "params": {}}]
        if "alternating_extrema" in (prof.signatures or []) and "time_series_regular" not in (prof.signatures or []):
            return []
        return [{"variant": "single", "bindings": b, "params": {}}]

    def covers(self, cand, prof, sclass):
        c = ["trend", "now", "height_value"]
        if int(sclass[3]) >= 2 or sclass[:2] not in ("W1",):
            c += ["extreme_high", "extreme_low"]
        if cand.variant == "multi":
            c.append("compare")
        return c

    def describes(self, cand, rec):
        R = RView(rec)
        what = R.f[cand.bindings["value"]].label
        return f"{what} over time as a {'bar' if self._bars(R, cand) else 'line'} chart" + \
            (" with one line per series" if cand.variant == "multi" else "")

    def summary(self, cand, rec, now):
        R = RView(rec)
        pts = self._pts(R, cand)
        if not pts:
            return "No points"
        (_t0, _v0, _), (_t1, _v1, r1) = pts[0], pts[-1]
        vf = cand.bindings["value"]
        return f"{R.f[vf].label}: latest {R.text(r1, vf)}; {len(pts)} points"

    def _bars(self, R, cand):
        return R.f[cand.bindings["value"]].agg in ("per_interval", "sum")

    def _pts(self, R, cand, sid=None):
        b = cand.bindings
        out = []
        for i in range(len(R.rows)):
            if sid is not None and R.cell(i, b["series_id"]) != sid:
                continue
            t, v = ts(R.cell(i, b["time"]), R.tz), R.num(i, b["value"])
            if t is not None and v is not None:
                out.append((t, v, i))
        out.sort()
        return out

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        cv.d.meta["no_balance"] = True
        if ctx.cand.variant == "multi":
            return self._multi(ctx, cv, plan)
        pts = self._pts(R, ctx.cand)
        if len(pts) < 2:
            if pts:
                cv.d.meta.pop("no_balance")
                hero(cv, ["c", 0], y0, ["cell", b["value"], pts[-1][2], {}], W, anchor="middle")
                cv.d.state = "one"
                return
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        vf = b["value"]
        bars = self._bars(R, ctx.cand)
        first, last = pts[0], pts[-1]
        # a series that runs past now (a forecast, a schedule of prices) leads with the reading AT now, not
        # with its far end (round 1: the headline is the current value)
        now_ts = ctx.now.timestamp()
        past = [p for p in pts if p[0] <= now_ts + 60]
        if past and len(past) < len(pts):
            last = past[-1]
        elif not past:
            last = pts[0]
        drop = ctx.rung.get("drop", 0)
        # round 3, the ask decides the headline:
        #  * 'when ... cheapest / highest': the extreme reading still ahead (or now) and its time;
        #  * a frame wholly ahead ('this weekend', 'tomorrow'): the frame's range, not its first point.
        wants = set(getattr(ctx.prof, "wants", None) or [])
        ext_rc = None
        if not bars and "times" in wants and wants & {"extreme_low", "extreme_high"}:
            low = "extreme_low" in wants and "extreme_high" not in wants
            ahead = [p for p in pts if p[0] >= now_ts - 3600] or pts
            e = min(ahead, key=lambda p: (p[1], p[0])) if low else max(ahead, key=lambda p: (p[1], -p[0]))
            last = e
            ext_rc = ["tpl", "lowest_at" if low else "highest_at", {"time": ["cell", b["time"], e[2], {}]}]
        range_rc = None
        if not bars and not past and (R.rec.flags or {}).get("window") and ext_rc is None:
            range_rc = ["tpl", "range", {"lo": ["d", "agg", [vf, "min"], {"field": vf, "unit": False}],
                                         "hi": ["d", "agg", [vf, "max"], {"field": vf}]}]
        dp = None
        few = ((R.rec.flags or {}).get("gap") or {}).get("kind") == "few"   # 2-3 readings are no trend (round 3)
        if not bars and last is not first and ext_rc is None and range_rc is None and not few:
            dv = last[1] - first[1]
            dec = max(fmt.decimals(R.f[vf], last[1]), fmt.decimals(R.f[vf], first[1]))
            dp = {"d": dv, "sign": dv,
                  "d_rc": ["d", "diff", [vf, last[2], vf, first[2]], {"field": vf, "dec": dec, "sign": True, "unit": False}]}
            if first[1]:
                dp["p"] = 100 * dv / first[1]
                dp["p_rc"] = ["d", "pct", [vf, last[2], vf, first[2]], {"dec": 2 if abs(dp["p"]) < 10 else 1, "sign": True}]
        since = ["join", " ", [["tpl", "since", {}], ["d", "date", [fmt.iso(datetime.fromtimestamp(first[0], UTC)),
                                                                    fmt.coarse_fmt(fmt.median_gap([p[0] for p in pts]),
                                                                                   "MMM d"), ctx.tz]]]]
        head_rc = ["cell", vf, last[2], {}] if not bars else ["d", "agg", [vf, "sum", [p[2] for p in pts]], {"field": vf}]
        if range_rc is not None:
            head_rc = range_rc
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        ctx.extras["row_of"] = [p[2] for p in pts]
        split = plan == "split"
        colw = W * (0.3 if ctx.wc in ("W5", "W6") else 0.42) - 12 if split else W
        role = "hero" if plan in ("spark", "spark_labels", "spark_stats", "hero_spark", "split") else "headline"
        hb = hero(cv, ["l", 0], y0, head_rc, colw, role=role)
        y = hb.bottom if hb else y0
        if ext_rc is not None and drop < 3 and y + line_h("sub") + 2 <= y1:
            eb = cv.text(["l", 0], y, ext_rc, "sub", src="data", max_w=colw, tok="text")
            if eb:
                y = eb.bottom
        # the source's own word for the reading's class on the headline row sits under it
        cls = next((f for f in R.rec.fields if f.role in ("status", "kind") and f.type == "category"
                    and _bins(R, f.name, vf)), None)
        if cls is not None and not bars and drop < 2 and R.cell(last[2], cls.name) is not None and \
                y + line_h("sub") + 2 <= y1:
            cb = cv.text(["l", 0], y, ["raw", cls.name, last[2]], "sub", src="data", max_w=colw, tok="text")
            if cb:
                y = cb.bottom
        # a number the data gives no unit for is named instead (the source's own words for the reading), never
        # left as a bare figure; a generic or coded label names nothing and is not shown (round 2)
        fv = R.f[vf]
        if not (fv.unit or fv.currency) and fv.label_src in ("lexicon", "key_ask") and \
                fv.label.strip().lower() not in ("value", "count", "number", "amount") and drop < 2 and \
                y + line_h("sub") + 2 <= y1 and (split or plan in ("spark", "spark_labels", "hero_spark", "spark_stats")
                                                  or ctx.rows >= 2):
            lb = cv.text(["l", 0], y, ["label", vf], "sub", src="key" if fv.label_src == "key_ask" else "lexicon",
                         max_w=colw, tok="muted")
            if lb:
                y = lb.bottom
        if dp is not None and plan != "spark" and drop < 3:
            db = delta_row(cv, ["l", 0], y + 1, colw, dp, ref_label=since if ctx.wc != "W1" else None,
                           want_abs=ctx.wc not in ("W1",))
            if db:
                y = db.bottom
        stats_h = 0
        if plan.endswith("_stats") and drop < 2:
            stats_h = line_h("meta") + line_h("row-strong") + 12
        if split:
            line_plot(cv, ctx, 0.45 if ctx.wc in ("W3", "W4") else 0.33, 1.0, y0, y1, xs, ys, field_name=vf,
                      bars=bars, labels="minmax" if ctx.wc in ("W5", "W6") else "none", ticks=False, min_share=False)
            return
        labels = "none" if plan in ("spark", "hero_spark") else "minmax"
        ticks = plan not in ("spark", "spark_labels", "hero_spark", "spark_stats") and not ctx.rung.get("drop_tick")
        bottom = y1 - stats_h
        out = line_plot(cv, ctx, 0.0, 1.0, y + 8, bottom, xs, ys, field_name=vf, bars=bars, labels=labels,
                        ticks=ticks, min_share=ctx.rows >= 2 and ctx.wc != "W1")
        yb = out["bottom"]
        if (stats_h or (ctx.rows >= 2 and y1 - yb > 60)) and drop < 2:
            ncol = 2 if ctx.wc in ("W1", "W2", "W3") else 3
            items = [(["tpl", "min", {}], ["d", "agg", [vf, "min"], {"field": vf}], "data"),
                     (["tpl", "max", {}], ["d", "agg", [vf, "max"], {"field": vf}], "data"),
                     (["tpl", "avg", {}], ["d", "agg", [vf, "avg"],
                                           {"field": vf, "dec": _avg_dec(R.f[vf], ys)}], "data")][:ncol]
            yb = kv_cells(cv, [["f", i / ncol] for i in range(ncol)], yb + 12, items, col_w=W / ncol - 10, rows=1)
        if ctx.rows >= 2 and y1 - yb > 40 and drop < 1:
            self._recent(ctx, cv, pts, vf, yb + 14, y1)

    def _recent(self, ctx, cv, pts, vf, top, bottom):
        """Height left after the capped plot and the stats buys the latest readings (time | value),
        newest first: the next tier of content, never a stretched line."""
        lh = line_h("sub") + 4
        step = sorted(b[0] - a[0] for a, b in itertools.pairwise(pts))
        daily = bool(step) and step[len(step) // 2] >= 20 * 3600
        tf = fmt.coarse_fmt(step[len(step) // 2] if step else None, "MMM d") if daily else "MMM d, h:mm a"
        k = int((bottom - top) // lh)
        if k < 1:
            return
        cv.d.meta["slack"] = lh
        y = top
        for t, v, i in list(reversed(pts))[:k]:
            iso = fmt.iso(datetime.fromtimestamp(t, UTC))
            cv.time(["l", 0], y, iso, tf, "sub", tz="card", tok="muted")
            cv.text(["r", 0], y, ["cell", vf, i, {}], "sub", src="data", max_w=ctx.W * 0.5, anchor="end", tok="text")
            y += lh

    def _multi(self, ctx, cv, plan):
        R, b, W = ctx.R, ctx.cand.bindings, ctx.W
        y0, y1 = ctx.body["y0"], ctx.body["y1"]
        if b.get("values"):            # wide format: one line per column
            series = []
            for f in b["values"]:
                pts = sorted((ts(R.cell(i, b["time"]), R.tz), R.num(i, f), i) for i in range(len(R.rows))
                             if ts(R.cell(i, b["time"]), R.tz) is not None and R.num(i, f) is not None)
                series.append((f, pts))
        else:
            ids = []
            for i in range(len(R.rows)):
                s = R.cell(i, b["series_id"])
                if s not in ids:
                    ids.append(s)
            series = [(s, self._pts(R, ctx.cand, s)) for s in ids]
        series = [(s, p) for s, p in series if len(p) >= 2]
        if not series:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            return
        shown = series[:3]
        vf = b["value"]
        if b.get("values"):
            vf = shown[0][0]
        allx = [p[0] for _, ps in shown for p in ps]
        ally = [p[1] for _, ps in shown for p in ps]
        t0, t1 = min(allx), max(allx)
        lo, hi = min(ally), max(ally)
        pad = (hi - lo) * 0.06 or 1
        lo, hi = lo - pad, hi + pad
        # legend row: swatch + series name + latest value (direct identity, never colour alone)
        y = y0
        leg_h = line_h("sub")
        ncol = len(shown) + (1 if len(series) > 3 else 0)
        rows_leg = 1 if ctx.wc in ("W4", "W5", "W6") else len(shown)
        for j, (s, ps) in enumerate(shown):
            last = ps[-1]
            if b.get("values"):
                rc = ["join", " ", [["label", s], ["cell", s, last[2], {}]]]
            else:
                rc = ["join", " ", [["raw", b["series_id"], last[2]], ["cell", vf, last[2], {}]]]
            if rows_leg == 1:
                x = ["f", j / ncol]
                yy = y
                mw = W / ncol - 20
            else:
                x = ["l", 0]
                yy = y + j * (leg_h + 2)
                mw = W - 18
            cv.rect(x, x if x[0] == "l" else x, yy + 5, yy + 15, f"viz-cat-{j + 1}")
            cv.d.prims[-1]["x1"] = ["l", 10] if x[0] == "l" else ["f", x[1] + 10 / ctx.W]
            cv.text(["l", 16] if x[0] == "l" else ["f", x[1] + 16 / ctx.W], yy, rc, "sub", src="data", max_w=mw,
                    tok="text")
        if len(series) > 3:
            cv.text(["f", 3 / ncol] if rows_leg == 1 else ["l", 0], y if rows_leg == 1 else y + 3 * (leg_h + 2),
                    ["tpl", "more", {"n": ["d", "count", [len(series) - 3]]}], "sub", src="code", max_w=W / ncol)
        y += (leg_h + 2) * (rows_leg + (1 if len(series) > 3 and rows_leg > 1 else 0)) + 6
        tick_h = line_h("tick") + 2 if ctx.rows >= 2 and not ctx.rung.get("drop_tick") else 0
        box = pbox(["f", 0], ["f", 1], y + 4, y1 - tick_h)
        for j, (s, ps) in enumerate(shown):
            pts = series_path([p[0] for p in ps], [p[1] for p in ps], lo, hi, t0, t1, max_pts=390 // len(shown))
            cv.path(box, pts, f"viz-cat-{j + 1}", w=VIZ["line_w"])
        cv.d.meta.setdefault("plots", []).append({"id": None, "y0": y + 4, "y1": y1 - tick_h,
                                                  "min_share": ctx.rows >= 2})
        if tick_h:
            prev = -1e9
            for t, f in time_ticks(t0, t1, ctx.tz, max(2, int(W // 72))):
                fx = (t - t0) / (t1 - t0)
                iso = fmt.iso(datetime.fromtimestamp(t, UTC))
                w = cv.measure(fmt.format_time(iso, f, ctx.tz), "tick") * 1.04 + 1
                anc = "start" if fx < 0.04 else "end" if fx > 0.96 else "middle"
                x0 = fx * W - (0 if anc == "start" else w if anc == "end" else w / 2)
                if x0 < prev + 8:
                    continue
                prev = x0 + w
                cv.time(["f", fx], y1 - line_h("tick"), iso, f, "tick", tz="card", anchor=anc)
        n = ctx.now.timestamp()
        if t0 <= n <= t1:
            fx = (n - t0) / (t1 - t0)
            lid = cv.line(["f", fx], y + 4, ["f", fx], y1 - tick_h, "viz-now-line")
            cv.live("now_marker", prims=[lid], box=box, t0=fmt.iso(datetime.fromtimestamp(t0, UTC)),
                    t1=fmt.iso(datetime.fromtimestamp(t1, UTC)))


def _avg_dec(f, ys) -> int:
    """An average is shown at the column's display precision (magnitude rule when unknown; round 3)."""
    if f.precision is not None and not getattr(f, "unrounded", False):
        return f.precision
    return fmt.decimals(f, sum(ys) / len(ys), derived=True)


FORM = SeriesLine()
