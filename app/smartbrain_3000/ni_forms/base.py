"""BaseForm and the shared, designed blocks every form composes (the approved look,
CONTRACTS.md 7.2): hero with step-down, delta row, range track, KV cells, a line/spark
plot with direct labels and ticks, list rows, the calm empty sentence.

A block measures itself at the floor width and returns its bottom y. A form's plan
for a span class is the ordered list of blocks it draws; content is dropped by plan
(and by the ladder's drop_items rung), never squashed.
"""
from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

from . import fmt
from . import text as TX
from . import tokens as TK
from .ctx import SP, VIZ, Box, Canvas, Draft, LayoutCtx, pbox, rx, shift, ty
from .rec import R as RView
from .rec import is_numeric, ts
from .spans import ALL_SCLASSES, Span
from .types import Reject

RECORD_STATES = frozenset({"empty", "one", "few", "many", "sample", "stale", "error_last_good", "not_ready"})
SINGLE_STATES = frozenset({"ok", "sample", "stale", "error_last_good", "not_ready"})


def all_plans(fn) -> dict:
    """{sclass: plan id | None} for all 18 span classes from fn(wclass, rows)."""
    return {sc: fn(sc[:2], int(sc[3])) for sc in ALL_SCLASSES}


class BaseForm:
    name = ""
    variants: tuple = ("default",)
    slots: dict = {}
    params_space: dict = {}
    sibling = None
    record_form = True
    intent_of = "now"
    PLANS: dict = {}
    REJECTS: dict = {}          # variant -> {sclass: reject code} for PLANS None entries

    @property
    def states(self):
        return RECORD_STATES if self.record_form else SINGLE_STATES

    # -- to override
    def match(self, rec, prof) -> list[dict]:
        return []

    def structural(self, cand, rec, prof, sclass: str, plan: str) -> str | Reject:
        return plan

    def plan_body(self, ctx: LayoutCtx, cv: Canvas, plan: str) -> None:
        raise NotImplementedError

    def covers(self, cand, prof, sclass: str) -> list[str]:
        return []

    def describes(self, cand, rec) -> str:
        return self.name

    def summary(self, cand, rec, now) -> str:
        return self.name

    def intent(self, variant: str, params: dict) -> str:
        return self.intent_of

    # -- shared
    def accepts(self, cand, rec, prof, span: Span):
        plan = self.PLANS[cand.variant][span.sclass]
        if plan is None:
            return Reject(self.REJECTS.get(cand.variant, {}).get(span.sclass, "too_narrow"))
        return self.structural(cand, rec, prof, span.sclass, plan)

    def layout(self, ctx: LayoutCtx) -> Draft:
        cv = Canvas(ctx, Draft())
        cv.d.meta["body"] = ctx.body
        cv.d.meta["record_form"] = self.record_form
        cv.d.meta["now"] = ctx.now
        plan = ctx.extras.get("plan") or self.PLANS[ctx.cand.variant][ctx.span.sclass]
        cv.d.plan = plan
        if ctx.state == "needs_update":
            # a designed state, never an old layout left standing: the sealed data no longer lays out
            # cleanly at this instant (e.g. its day has passed) and no fetch has replaced it yet
            a = ctx.rec.context.as_of or ctx.rec.context.fetched_at
            calm(ctx, cv, [["tpl", "needs_update", {"date": ["d", "date", [a, "MMM d, h:mm a", ctx.tz]]}]],
                 checked=False)
            cv.d.state = "needs_update"
        elif ctx.state == "not_ready" and not ctx.rec.rows:
            calm(ctx, cv, [["tpl", "not_ready", {}]])
            cv.d.state = "not_ready"
        elif ctx.rec.error and not ctx.rec.rows:
            calm(ctx, cv, [["tpl", "no_rows", {}]])
            cv.d.state = "empty"
        else:
            m = cv.mark()
            self.plan_body(ctx, cv, plan)
            if not cv.d.state or cv.d.state == "ok":
                cv.d.state = ctx.state
            if not cv.d.meta.get("no_balance"):
                auto_balance(ctx, cv, m)
        return cv.d


def extent(prims: list, W: float):
    """(top, bottom) of the ink of `prims` at width W."""
    from .lint import mark_box, text_box
    ys = []
    for p in prims:
        b = text_box(p, W)[:4] if p["k"] in ("text", "time") else mark_box(p, W)
        if b:
            ys.append((b[1], b[3]))
    if not ys:
        return None
    return min(a for a, _ in ys), max(b for _, b in ys)


def auto_balance(ctx: LayoutCtx, cv: Canvas, mark: int):
    """The 'few' designed state: a body block that leaves more than 30% of the body empty
    below it is centred vertically as one unit (never a block with a hole under it)."""
    # Review fix: a short block is no longer centred (centring hid hollow cards from lint and put heroes
    # at different heights across a row). Content is top-anchored; a span whose content cannot fill
    # the body is L-HOLLOW and is not offered.
    return


# ============================================================================ blocks
def hero(cv: Canvas, x: list, top: float, recipe, max_w: float, *, role: str = "hero", start: int = 0,
         anchor: str = "start", src: str = "data", tok: str | None = None, s: str | None = None,
         max_lines: int = 1) -> Box | None:
    """Hero/headline with step-down: the largest ladder size that fits wins, before any
    content is dropped (design standard 3). ``max_lines`` > 1 lets a WORDED hero (a matchup,
    a status sentence) wrap at the largest size whose lines all fit before it is ever cut
    (fix round 1a-5: "Green Bay Packers vs…" was a red truncated hero on the phone)."""
    s = cv.run(recipe) if s is None else s
    if not s:
        return None
    lad = TK.TYPE[role]["ladder"]
    start = min(start + cv.ctx.rung.get("hero", 0), len(lad) - 1)
    for px in lad[start:]:
        if cv.fits(s, role, max_w, px):
            return cv.text(x, top, recipe, role, src=src, max_w=max_w, px=px, anchor=anchor, tok=tok, s=s)
    if isinstance(recipe, list) and recipe and recipe[0] == "cell" and not (recipe[3] or {}).get("compact"):
        # a number too wide at the smallest step reads compact (1.01B) before anything is cut
        rc2 = ["cell", recipe[1], recipe[2], dict(recipe[3] or {}, compact=True)]
        s2 = cv.run(rc2)
        if s2 != s:
            return hero(cv, x, top, rc2, max_w, role=role, start=start, anchor=anchor, src=src, tok=tok, s=s2)
    for px in (lad[start:] if max_lines > 1 else []):
        _, wt, _, tn = ty(role, px)
        lines, trunc = TX.break_lines(s, px, wt, max_w, max_lines, tn)
        if not trunc and len(lines) <= max_lines:
            return cv.text(x, top, recipe, role, src=src, max_w=max_w, max_lines=max_lines, px=px,
                           anchor=anchor, tok=tok, s=s)
    return cv.text(x, top, recipe, role, src=src, max_w=max_w, px=lad[-1], anchor=anchor, tok=tok, s=s)


def hero_px_fit(cv: Canvas, s: str, max_w: float, role: str = "hero") -> int | None:
    lad = TK.TYPE[role]["ladder"]
    for px in lad[min(cv.ctx.rung.get("hero", 0), len(lad) - 1):]:
        if cv.fits(s, role, max_w, px):
            return px
    return None


def line_h(role: str, px: int | None = None) -> int:
    return ty(role, px)[2]


def tri_dir(v: float) -> str | None:
    return "up" if v > 0 else "down" if v < 0 else None


def delta_parts(R: RView, b: dict) -> dict | None:
    """Delta and percent recipes from bound fields: a delta/delta_pct field when the
    data has one, else derived by code from value and reference."""
    v, ref = b.get("value"), b.get("reference")
    d, dp = b.get("delta"), b.get("delta_pct")
    out = {}
    if d is not None and R.num(0, d) is not None:
        out["d"] = R.num(0, d)
        out["d_rc"] = ["cell", d, 0, {"sign": True, "unit": False}]
    elif v is not None and ref is not None and R.num(0, v) is not None and R.num(0, ref) is not None:
        fv, fr = R.f[v], R.f[ref]
        dec = max(fmt.decimals(fv, R.num(0, v)), fmt.decimals(fr, R.num(0, ref)))
        out["d"] = R.num(0, v) - R.num(0, ref)
        out["d_rc"] = ["d", "diff", [v, 0, ref, 0], {"field": v, "dec": dec, "sign": True, "unit": False}]
    if dp is not None and R.num(0, dp) is not None:
        out["p"] = R.num(0, dp)
        out["p_rc"] = ["cell", dp, 0, {"sign": True}]
        if R.f[dp].type != "percent" and not R.f[dp].unit:     # a delta_pct role is a percent by definition
            out["p_rc"] = ["join", "", [out["p_rc"], ["lit", "%"]]]
    elif v is not None and ref is not None and R.num(0, ref):
        pv = R.num(0, v)
        if pv is not None:
            out["p"] = 100 * (pv - R.num(0, ref)) / R.num(0, ref)
            out["p_rc"] = ["d", "pct", [v, 0, ref, 0], {"dec": 2, "sign": True}]
    if "d" not in out and "p" not in out:
        return None
    out["sign"] = out.get("d", out.get("p", 0))
    return out


def delta_row(cv: Canvas, x: list, top: float, max_w: float, dp: dict, *, ref_label=None,
              want_abs=True, want_pct=True, role="delta") -> Box | None:
    """tri + signed delta (+ pct in parentheses) in viz-pos/neg, then the muted reference
    label ('vs Prev close'). Parts drop right-to-left until the row fits."""
    sgn = dp["sign"]
    tok = "viz-pos" if sgn > 0 else "viz-neg" if sgn < 0 else "muted"
    d = tri_dir(sgn)
    parts = []
    if want_abs and "d_rc" in dp:
        parts.append(dp["d_rc"])
    if want_pct and "p_rc" in dp:
        parts.append(["join", "", [["lit", "("], dp["p_rc"], ["lit", ")"]]] if parts else dp["p_rc"])
    if not parts:
        return None
    tri_w = SP["tri_size"] + SP["tri_gap"] if d else 0
    options = []
    if ref_label is not None:
        options.append((parts, True))
    options.append((parts, False))
    if len(parts) == 2:
        options.append(([parts[1] if not want_abs else parts[0]], False))
        options.append(([dp["p_rc"]] if "p_rc" in dp else [parts[0]], False))
    for ps, with_ref in options:
        rc = ["join", " ", ps]
        s = cv.run(rc)
        w = cv.measure(s, role)
        ref_s = cv.run(ref_label) if with_ref else ""
        ref_w = cv.measure(ref_s, "sub") + 6 if ref_s else 0
        if (tri_w + w + ref_w) * (1 + TX.MARGIN) <= max_w:
            px, wt, lh, _ = ty(role)
            if d:
                cy = top + lh / 2 + 0.5
                cv.tri(shift(x, SP["tri_size"] / 2), cy, SP["tri_size"], d, tok)
            b = cv.text(shift(x, tri_w), top, rc, role, src="data", max_w=w * (1 + TX.MARGIN) + 1, tok=tok, s=s)
            if ref_s:
                cv.text(shift(x, tri_w + w + 6), top, ref_label, "sub", src="key",
                        max_w=ref_w * (1 + TX.MARGIN) + 1, s=ref_s)
            return Box(rx(x, cv.ctx.W), rx(x, cv.ctx.W) + tri_w + w + ref_w, top, top + lh, b.id if b else None)
    return None


def kv_cells(cv: Canvas, cols: list, top: float, items: list, *, col_w: float, rows: int,
             row_gap: float = 10, value_role: str = "row-strong") -> float:
    """KV grid: label (meta, muted) above value (16/600, stepping down to 13 as one size
    for the whole grid before anything is truncated). `cols` = x anchors; row-major."""
    from .prov import uses_data
    base = TK.TYPE[value_role]["px"]
    # a value that cannot fit even at 13 px drops out of the grid (content drops; never squashed)
    items = [it for it in items if cv.fits(cv.run(it[1]), value_role, col_w, 13)]
    n = min(len(items), len(cols) * rows)
    vals = [cv.run(items[i][1]) for i in range(n)]
    vpx = next((p for p in range(base, 12, -1) if all(cv.fits(v, value_role, col_w, p) for v in vals)), 13)
    lab_h, val_h = line_h("meta"), line_h(value_role, vpx)
    cell_h = lab_h + val_h
    bottom = top
    for i in range(n):
        r, c = divmod(i, len(cols))
        y = top + r * (cell_h + row_gap)
        lab_rc, val_rc, src = items[i]
        cv.text(cols[c], y, lab_rc, "meta", src="data" if uses_data(lab_rc) else ("key" if lab_rc[0] == "label" else "code"),
                max_w=col_w)
        cv.text(cols[c], y + lab_h, val_rc, value_role, src=src, max_w=col_w, px=vpx, s=vals[i])
        bottom = y + cell_h
    return bottom


def kv_line_rows(cv: Canvas, x0: list, x1: list, top: float, items: list, *, width: float,
                 role_l: str = "sub", role_v: str = "row-strong", gap: float = 6) -> float:
    """Label left, value right: narrow-width KV (phone half, desktop 1-col R2)."""
    y = top
    lh = max(line_h(role_l), line_h(role_v, 14))
    for lab_rc, val_rc, src in items:
        vs = cv.run(val_rc)
        vw = cv.measure(vs, role_v, 14) * (1 + TX.MARGIN) + 1
        from .prov import uses_data
        cv.text(x0, y, lab_rc, role_l, src="data" if uses_data(lab_rc) else ("key" if lab_rc[0] == "label" else "code"),
                max_w=max(width - vw - 8, 24))
        cv.text(x1, y, val_rc, role_v, src=src, max_w=vw, anchor="end", px=14, s=vs)
        y += lh + gap
    return y - gap


def calm(ctx: LayoutCtx, cv: Canvas, lines: list, *, checked: bool = True):
    """The designed calm state: a centred sentence + 'Checked {time}' as one unit."""
    b = ctx.body
    W = ctx.W
    y = 0.0
    hs = []
    for rc in lines:
        hs.append(line_h("body") * 2)
    m = cv.mark()
    y = b["y0"]
    last = y
    from .shell import gap_marker
    g = gap_marker(ctx)
    if g is not None and not ctx.rec.rows:
        lines = [g]                  # the honest reason beats a generic 'nothing here' (round 1)
    for rc in lines:
        bx = cv.text(["c", 0], y, rc, "body", src="code", max_w=W - 8, max_lines=2 if ctx.rows > 1 else 1,
                     anchor="middle")
        if bx:
            y = bx.bottom + 2
            last = bx.bottom
    a = ctx.rec.context.fetched_at
    if checked and a:
        lab = ["tpl", "checked", {}]
        s = cv.run(lab)
        w1 = cv.measure(s, "meta") + 4
        tw = TX.width(fmt.WIDEST["h:mm a"], *ty("meta")[:2]) * (1 + TX.MARGIN) + 0.5
        total = w1 + tw
        cv.text(["c", -total / 2], y, lab, "meta", src="code", max_w=w1 * (1 + TX.MARGIN) + 1)
        cv.time(["c", -total / 2 + w1], y, a, "h:mm a", "meta", tz="viewer")
        last = y + line_h("meta")
    cv.balance(m, b["y0"], last, b["y0"], b["y1"])
    cv.d.state = "empty"
    cv.d.meta["calm"] = True


def fit_parts(cv: Canvas, parts: list, role: str, max_w: float, sep: str = " · "):
    """Join recipe parts, dropping from the end until the line fits (content drops, never cut)."""
    parts = [p for p in parts if p]
    while parts and not cv.fits(cv.run(["join", sep, parts]), role, max_w):
        parts = parts[:-1]
    return ["join", sep, parts] if parts else None


def more_line(cv: Canvas, x: list, top: float, n: int, max_w: float, *, status_rc=None, role="meta") -> Box | None:
    if n <= 0:
        return None
    if status_rc is not None:
        rc = ["tpl", "more_status", {"n": ["d", "count", [n]], "status": status_rc}]
        return cv.text(x, top, rc, role, src="data", max_w=max_w)
    return cv.text(x, top, ["tpl", "more", {"n": ["d", "count", [n]]}], role, src="code", max_w=max_w)


# ============================================================================ plots
def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        out.append(round(t, 10))
        t += step
    return out


def _daily(xs) -> bool:
    """A series of one reading per day or coarser (daily highs are not an intraday line)."""
    s = sorted(xs)
    steps = sorted(b - a for a, b in itertools.pairwise(s) if b > a)
    return bool(steps) and steps[len(steps) // 2] >= 20 * 3600


def time_ticks(t0: float, t1: float, tz: str, max_n: int, min_step: float = 0) -> list[tuple[float, str]]:
    """(epoch, fmt) ticks on local boundaries for a time axis; at most max_n."""
    span = t1 - t0
    if span <= 0 or max_n < 2:
        return []
    z = fmt.zone(tz)
    cands = [(3 * 3600, "ha_short"), (6 * 3600, "ha_short"), (12 * 3600, "ha_short"), (86400, "EEE d"),
             (2 * 86400, "MMM d"), (7 * 86400, "MMM d"), (14 * 86400, "MMM d"), (30.44 * 86400, "MMM d"),
             (91.3 * 86400, "MMM d"), (365.25 * 86400, "yyyy"), (5 * 365.25 * 86400, "yyyy")]
    cands = [c for c in cands if c[0] >= min_step]
    for step, f in cands:
        if span / step <= max_n:
            break
    out = []
    loc0 = datetime.fromtimestamp(t0, UTC).astimezone(z)
    if step < 86400:
        h = int(step // 3600)
        cur = loc0.replace(minute=0, second=0, microsecond=0)
        cur = cur.replace(hour=(cur.hour // h) * h)
        while cur.timestamp() <= t1:
            if cur.timestamp() >= t0:
                out.append((cur.timestamp(), f))
            cur = (cur + timedelta(hours=h)).astimezone(z)
            cur = cur.replace(hour=(cur.hour // h) * h, minute=0)
    elif step < 28 * 86400:
        d = max(1, round(step / 86400))
        cur = loc0.replace(hour=0, minute=0, second=0, microsecond=0)
        while True:
            t = datetime(cur.year, cur.month, cur.day, tzinfo=z).timestamp()
            if t > t1:
                break
            if t >= t0:
                out.append((t, f))
            cur = cur + timedelta(days=d)
    else:
        months = max(1, round(step / (30.44 * 86400)))
        y, m = loc0.year, loc0.month
        while True:
            t = datetime(y, m, 1, tzinfo=z).timestamp()
            if t > t1:
                break
            if t >= t0 and (m - 1) % months == 0:
                out.append((t, "yyyy" if months >= 12 else "MMM d" if months < 3 else "MMM d"))
            m += 1
            if m > 12:
                y, m = y + 1, 1
    return out[:max_n + 1]


def series_path(xs: list, ys: list, lo: float, hi: float, t0: float, t1: float, max_pts: int = 360) -> list:
    """Fractional points of a polyline in its box (fy 0 = top). Downsampled by bucket
    min/max so extremes survive."""
    pts = [((x - t0) / (t1 - t0) if t1 > t0 else 0.5, 1 - ((y - lo) / (hi - lo) if hi > lo else 0.5))
           for x, y in zip(xs, ys)]
    if len(pts) > max_pts:
        step = len(pts) / (max_pts / 2)
        out = []
        i = 0.0
        while int(i) < len(pts):
            seg = pts[int(i):int(i + step)] or [pts[int(i)]]
            a = min(seg, key=lambda p: p[1])
            b = max(seg, key=lambda p: p[1])
            out.extend(sorted({a, b}, key=lambda p: p[0]))
            i += step
        pts = out[:max_pts]
    return pts


def line_plot(cv: Canvas, ctx: LayoutCtx, fx0: float, fx1: float, top: float, bottom: float,
              xs: list, ys: list, *, field_name: str, bars: bool = False, labels: str = "minmax",
              ticks: bool = True, now_marker: bool = True, fill: bool = True, tok: str = "viz-line",
              min_share: bool = False, zero_base: bool = False) -> dict:
    """A time plot in the fractional box [fx0, fx1] x [top, bottom]: line (+wash) or bars,
    direct min/max/last labels (11/500) placed outside the marks, time ticks (11/400),
    a now marker when now is inside the window. Returns meta for the caller."""
    W = ctx.W
    cap = plot_cap(ctx, fx1 - fx0)
    if bottom - top > cap:
        bottom = top + cap           # aspect cap: extra height is never spent stretching a line (review 5)
    lab_h = line_h("label")
    tick_h = line_h("tick") if ticks and not ctx.rung.get("drop_tick") else 0
    lab_room = lab_h + 4 if labels != "none" else 4
    y0 = top + lab_room
    y1 = bottom - tick_h - (lab_room if labels in ("minmax",) else 4)
    if y1 - y0 < 16:
        labels = "none"
        y0, y1 = top + 4, bottom - tick_h - 4
    t0, t1 = min(xs), max(xs)
    if t1 == t0:
        t1 = t0 + 1
    lo, hi = min(ys), max(ys)
    if zero_base or bars:
        lo, hi = min(lo, 0), max(hi, 0)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.04
    lo_p, hi_p = (lo if (zero_base or bars) and lo == 0 else lo - pad), hi + pad
    box = pbox(["f", fx0], ["f", fx1], y0, y1)
    pid = None
    if bars:
        n = len(xs)
        bw = (fx1 - fx0) / max(n, 1)
        zero_fy = 1 - (0 - lo_p) / (hi_p - lo_p)
        for x, y in zip(xs, ys):
            f = fx0 + (x - t0) / (t1 - t0) * (fx1 - fx0 - bw) if n > 1 else fx0
            fy = 1 - (y - lo_p) / (hi_p - lo_p)
            ya, yb = sorted((y0 + fy * (y1 - y0), y0 + zero_fy * (y1 - y0)))
            cv.rect(["f", f + bw * 0.12], ["f", f + bw * 0.88], ya, max(yb, ya + 1),
                    "viz-neg" if y < 0 else tok, r=1)
    else:
        pts = series_path(xs, ys, lo_p, hi_p, t0, t1)
        if fill and len(pts) > 1:
            cv.path(box, pts + [[pts[-1][0], 1.0], [pts[0][0], 1.0]], "viz-fill", w=0, fill="viz-fill")
        pid = cv.path(box, pts, tok, w=VIZ["line_w"])
    meta = {"id": pid, "y0": y0, "y1": y1, "min_share": min_share, "cap": cap}
    cv.d.meta.setdefault("plots", []).append(meta)

    def X(t):
        return fx0 + (t - t0) / (t1 - t0) * (fx1 - fx0)

    def Y(v):
        return y0 + (1 - (v - lo_p) / (hi_p - lo_p)) * (y1 - y0)
    # direct labels: max above, min below, keep inside the box horizontally
    if labels != "none" and len(ys) > 1:
        imax = max(range(len(ys)), key=lambda i: ys[i])
        imin = min(range(len(ys)), key=lambda i: ys[i])
        (fx1 - fx0) * W
        for i, above in ((imax, True), (imin, False)):
            rc = ["cell", field_name, ctx.extras["row_of"][i], {}]
            s = cv.run(rc)
            w = cv.measure(s, "label") * (1 + TX.MARGIN) + 1
            fx = X(xs[i])
            px_ = fx * W
            anchor = "middle"
            if px_ - w / 2 < fx0 * W:
                anchor = "start"
                fx = fx0
            elif px_ + w / 2 > fx1 * W:
                anchor = "end"
                fx = fx1
            yv = Y(ys[i])
            ty_ = yv - VIZ["label_offset"] - lab_h if above else max(yv + VIZ["label_offset"] - 2, y1 + 2)
            ty_ = max(top, min(ty_, bottom - tick_h - lab_h))
            cv.text(["f", fx], ty_, rc, "label", src="data", max_w=w, anchor=anchor)
            if i == imax and imax == imin:
                break
    # ticks
    if tick_h:
        tks = time_ticks(t0, t1, ctx.tz, max(2, int((fx1 - fx0) * W // 64)), min_step=86400 if _daily(xs) else 0)
        prev_x1 = -1e9
        for t, f in tks:
            iso = fmt.iso(datetime.fromtimestamp(t, UTC))
            s = fmt.format_time(iso, f, ctx.tz)
            w = cv.measure(s, "tick") * (1 + TX.MARGIN) + 1
            fx = X(t)
            px_ = fx * W
            anc = "middle"
            if px_ - w / 2 < fx0 * W:
                anc, x0 = "start", px_
            elif px_ + w / 2 > fx1 * W:
                anc, x0 = "end", px_ - w
            else:
                x0 = px_ - w / 2
            if x0 < prev_x1 + 8:
                continue
            prev_x1 = x0 + w
            cv.time(["f", fx], bottom - tick_h, iso, f, "tick", tz="card", anchor=anc)
    # now marker
    nowt = ctx.now.timestamp()
    daily = _daily(xs)
    if now_marker and t0 <= nowt <= t1 and not bars and not daily:
        fx = X(nowt)
        lid = cv.line(["f", fx], y0 - 2, ["f", fx], y1, "viz-now-line", w=VIZ["now_w"])
        cv.live("now_marker", prims=[lid], box=box, t0=fmt.iso(datetime.fromtimestamp(t0, UTC)),
                t1=fmt.iso(datetime.fromtimestamp(t1, UTC)), path=pid)
    return {"X": X, "Y": Y, "box": box, "path": pid, "y0": y0, "y1": y1, "t0": t0, "t1": t1, "bottom": bottom}


def plot_cap(ctx, frac_w: float) -> float:
    """The tallest a plot may be: half its own width (never less than 96 px). Height beyond it goes to the
    next tier of content (facts, a table), or the span is not offered (L-HOLLOW)."""
    return max(96.0, 0.5 * frac_w * ctx.W)


def series_of(rec):
    """(part name, time field, value field) of the first series part, else None. Of several numeric
    columns the one the current reading continues is the history (its last point is nearest the
    reading) - structure, not words: a quote's 'close', not its 'open'."""
    R0 = RView(rec)
    m = next((f for f in rec.fields if f.role in ("measure", "value")), None)
    cur = R0.num(0, m.name) if (m is not None and rec.rows) else None
    for name, p in (rec.parts or {}).items():
        if p.kind != "series" or not p.rows:
            continue
        tf = next((f for f in p.fields if f.role == "time" or f.type == "datetime"), None)
        vs = [f for f in p.fields if is_numeric(f) and f.role in ("measure", "value", "unknown")]
        if not tf or not vs:
            continue
        vf = vs[0]
        if cur is not None and len(vs) > 1:
            PR = RView(p)

            def last(f):
                for i in range(len(p.rows) - 1, -1, -1):
                    v = PR.num(i, f.name)
                    if v is not None:
                        return v
                return None
            scored = [(abs(last(f) - cur), k, f) for k, f in enumerate(vs) if last(f) is not None]
            if scored:
                vf = min(scored)[2]
        return name, tf.name, vf.name
    return None


def part_plot(ctx, cv, ser, fa, fb, top, bottom, *, labels="none", min_share=False, ticks=None):
    """A line plot of a record part (series), recipes tagged ['part', name, rc]."""
    part = ctx.rec.parts[ser[0]]
    PR = RView(part)
    xs, ys, rows = [], [], []
    for i in range(len(part.rows)):
        t, v = ts(PR.cell(i, ser[1]), PR.tz), PR.num(i, ser[2])
        if t is not None and v is not None:
            xs.append(t)
            ys.append(v)
            rows.append(i)
    if len(xs) < 2:
        return None
    saved = ctx.R
    ctx.R = PR
    before = set(cv.d.prov)
    if labels != "none" and bottom - top > 110:
        cb = cv.text(["f", fa], top, ["label", ser[2]], "meta", src="key", max_w=(fb - fa) * ctx.W)
        if cb:
            top = cb.bottom + 2
    ctx.extras["row_of"] = rows
    try:
        out = line_plot(cv, ctx, fa, fb, top, bottom, xs, ys, field_name=ser[2], labels=labels,
                        ticks=(labels != "none" and bottom - top > 80) if ticks is None else ticks,
                        min_share=min_share)
    finally:
        ctx.R = saved
    for pid in set(cv.d.prov) - before:
        cv.d.prov[pid] = ["part", ser[0], cv.d.prov[pid]]
    return out


def badge(cv: Canvas, x: list, top: float, recipe, *, tok="badge-line", max_w: float = 120,
          s: str | None = None) -> Box | None:
    """Outline pill (approved look): 12/600 text, 20 px tall, radius 10."""
    s = cv.run(recipe) if s is None else s
    if not s:
        return None
    px, wt, lh, tn = ty("badge")
    tw = min(TX.width(s, px, wt, tn) * (1 + TX.MARGIN) + 1, max_w - 2 * SP["badge_pad_x"])
    w = tw + 2 * SP["badge_pad_x"]
    h = SP["badge_h"]
    cv.rect(x, shift(x, w), top, top + h, "panel", r=SP["badge_r"], stroke=tok)
    text_tok = "warn" if tok == "badge-line" else tok
    b = cv.text(shift(x, SP["badge_pad_x"]), top + (h - lh) / 2, recipe, "badge", src="data", max_w=tw,
                tok=text_tok, s=s)
    x0 = rx(x, cv.ctx.W)
    return Box(x0, x0 + w, top, top + h, b.id if b else None)


def badge_w(cv: Canvas, s: str) -> float:
    px, wt, lh, tn = ty("badge")
    return TX.width(s, px, wt, tn) * (1 + TX.MARGIN) + 1 + 2 * SP["badge_pad_x"]


def fact_rc(R: RView, fname: str, row: int = 0):
    """Recipe for one fact value: directions in degrees read as compass points."""
    f = R.f[fname]
    if f.unit == "deg" and R.num(row, fname) is not None:
        return ["d", "compass", [fname, row]]
    if f.unit in ("wmo",):
        return ["lex", "wmo", fname, row]
    return ["cell", fname, row, {"compact": True}]


def labelled_rc(R: RView, fname: str, row: int = 0):
    """'Wind 13.7 mph' as one recipe (label + value)."""
    return ["join", " ", [["label", fname], fact_rc(R, fname, row)]]


def rows_capacity(avail: float, row_h: float, n: int, more_h: float) -> tuple[int, int]:
    """(rows shown, hidden) for a list: reserve a '+N more' line only when needed."""
    cap = int(max(0, avail) // row_h)
    if n <= cap:
        return n, 0
    cap = int(max(0, avail - more_h) // row_h)
    return cap, n - cap


def center_block(ctx: LayoutCtx, cv: Canvas, mark: int):
    """Turn a left-anchored block into a horizontally centred unit (the balanced single
    unit): every l-anchor becomes a c-anchor at the same floor position."""
    from .lint import mark_box, text_box
    xs = []
    for p in cv.d.prims[mark:]:
        b = text_box(p, ctx.W)[:4] if p["k"] in ("text", "time") else mark_box(p, ctx.W)
        if b:
            xs.append((b[0], b[2]))
    if not xs:
        return
    x0, x1 = min(a for a, _ in xs), max(b for _, b in xs)
    off = (ctx.W - (x1 - x0)) / 2 - x0
    half = ctx.W / 2

    def conv(x):
        if x[0] == "l":
            return ["c", round(x[1] + off - half, 2)]
        return x
    for p in cv.d.prims[mark:]:
        for k in ("x", "x0", "x1"):
            if k in p and isinstance(p[k], list):
                p[k] = conv(p[k])
