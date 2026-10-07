"""CardShell (CONTRACTS.md 6.1): header title + mandatory footer at every span.

Footer: left  = [state marker] · [dates inferred] · host · cadence   (12/400 muted)
        right = [age when stale] as-of time prim (never dropped; width reserved for the
                widest value of its format)
Drop order when the left part does not fit: cadence, then host -> registrable
domain, then host. Markers (sample, failing, dates inferred) never drop; at narrow
widths they take a second footer line instead.
"""
from __future__ import annotations

from . import fmt
from . import text as TX
from .ctx import SP, Canvas, LayoutCtx, ty

TITLE_LINES = {1: 1, 2: 2, 3: 2}


def as_of(ctx: LayoutCtx) -> str | None:
    c = ctx.rec.context
    return c.as_of or c.fetched_at


def _markers(ctx: LayoutCtx) -> list:
    """Honesty/state markers (recipe, tok) that must stay visible."""
    out = []
    if ctx.state == "error_last_good":
        out.append((["tpl", "update_failed", {}], "warn"))
    if ctx.state == "not_ready" and ctx.rec.rows:
        out.append((["tpl", "not_ready", {}], "warn"))
    if ctx.rec.flags.get("sample"):
        a = as_of(ctx)
        out.append((["tpl", "sample", {"date": ["d", "date", [a, "MMM d", ctx.tz]]}], "warn"))
    if inferred_dates(ctx.rec):
        out.append((["tpl", "dates_inferred", {}], "muted"))
    g = gap_marker(ctx)
    if g is not None and ctx.rec.rows:
        out.insert(0, (g, "warn"))       # an empty body states it itself (base.calm)
    return out


def _older_year(iso: str, ctx) -> bool:
    return str(iso)[:4] != fmt.iso(ctx.now)[:4]


def gap_marker(ctx: LayoutCtx):
    """The data stage's honest statement of what this data cannot show (rec.flags['gap'])."""
    g = ctx.rec.flags.get("gap") if isinstance(ctx.rec.flags, dict) else None
    if not g:
        return None

    def d(iso):
        if g.get("fmt") == "quarter":
            return ["d", "quarter", [iso]]
        return ["d", "date", [iso, g.get("fmt") or ("MMM d, yyyy" if _older_year(iso, ctx) else "MMM d"), ctx.tz]]
    k = g.get("kind")
    if k == "latest" and g.get("latest"):
        return ["tpl", "gap_latest", {"date": d(g["latest"])}]
    if k == "frame_empty":
        if g.get("next"):
            return ["tpl", "gap_none_next", {"frame": g["frame"], "date": d(g["next"])}]
        if g.get("latest"):
            return ["tpl", "gap_none_latest", {"frame": g["frame"], "date": d(g["latest"])}]
        return ["tpl", "gap_none", {"frame": g["frame"]}]
    if k == "none_active":
        if g.get("latest"):
            return ["tpl", "gap_none_active_latest", {"date": d(g["latest"])}]
        if g.get("next"):
            return ["tpl", "gap_none_active_next", {"date": d(g["next"])}]
    if k == "no_match" and g.get("q"):
        return ["tpl", "gap_no_match", {"q": g["q"][:28]}]
    if k == "ids_only":
        return ["tpl", "gap_ids_only", {}]
    if k == "unnamed":
        return ["tpl", "gap_unnamed", {}]
    if k == "unit_unstated" and g.get("unit"):
        return ["tpl", "gap_unit_unstated", {}]
    if k == "unit_other" and g.get("unit") and g.get("has"):
        return ["tpl", "gap_unit_other", {"unit": g["unit"], "has": g["has"]}]
    if k == "none_of" and g.get("n"):
        return ["tpl", "gap_none_of", {"n": ["d", "count", [g["n"]]]}]
    if k == "capped" and g.get("n"):
        if g.get("total"):
            return ["tpl", "gap_capped_total", {"n": ["d", "count", [g["n"]]], "total": ["d", "count", [g["total"]]]}]
        return ["tpl", "gap_capped", {"n": ["d", "count", [g["n"]]]}]
    if k == "few" and g.get("n"):
        return ["tpl", "gap_few", {"n": ["d", "count", [g["n"]]]}]
    return None


def inferred_dates(rec) -> bool:
    return rec.flags.get("date_confidence") in ("inferred", "positional") or \
        "day_groups_positional" in (rec.inferred or [])


def _short(marks):
    return [(["tpl", "sample_short", m[0][2]], m[1]) if m[0][:2] == ["tpl", "sample"] else m for m in marks]


def shell(ctx: LayoutCtx, cv: Canvas) -> dict:
    """Draw header + footer into `cv`. Returns the body box."""
    W, H = ctx.W, ctx.bucket.h
    # ---------------------------------------------------------------- header
    tb = cv.text(["l", 0], 0, ["title"], "title", src="title", max_w=W, max_lines=TITLE_LINES[ctx.rows])
    nlines = tb.lines if tb else 1
    y0 = nlines * ty("title")[2] + SP["header_gap"]
    # program data layer: what the data cannot answer LEADS the body (never a footer-only honesty)
    lead = ctx.rec.flags.get("lead") if isinstance(ctx.rec.flags, dict) else None
    if lead:
        lb = cv.text(["l", 0], y0, lead, "sub", src="code", max_w=W, max_lines=2 if ctx.rows > 1 else 1,
                     tok="warn")
        if lb is not None:
            y0 = lb.bottom + SP["block_gap"]
    # ---------------------------------------------------------------- footer (right: time)
    fpx, fwt, flh, ftn = ty("footer")
    a = as_of(ctx)
    right_w = 0.0
    tf = "h:mm a"
    if a:
        same_day = fmt.local_date(a, ctx.tz) == fmt.local_date(fmt.iso(ctx.now), ctx.tz)
        tf = "h:mm a" if same_day else "MMM d, h:mm a"
        right_w = TX.width(fmt.WIDEST[tf], fpx, fwt, ftn) * (1 + TX.MARGIN) + 0.5
    age_w = 0.0
    if (ctx.state in ("stale", "needs_update") or ctx.rec.flags.get("stale_as_of")) and a:
        age_w = TX.width(fmt.AGE_WIDEST, fpx, fwt, ftn) * (1 + TX.MARGIN) + 8
    gap = 12
    age_inline = not age_w or right_w + age_w + gap + 40 <= W
    avail = W - right_w - (age_w if age_inline else 0) - (gap if right_w else 0)
    # ---------------------------------------------------------------- footer (left)
    marks = _markers(ctx)
    mark_rc = [m[0] for m in marks]
    host = ctx.host
    tails = []
    # the source is named by its registrable domain at every span (review 3: one name per card, never
    # a host that changes with the span); the cadence drops first
    if host and ctx.inp.cadence_s:
        tails.append([["host_short"], ["cadence"]])
    if host:
        tails.append([["host_short"]])
    elif ctx.inp.cadence_s:
        tails.append([["cadence"]])
    tails.append([])
    chosen, two_line = None, not age_inline
    for t in (tails if age_inline else []):
        if not (mark_rc or t):
            break
        rc = ["join", " · ", mark_rc + t]
        if TX.fits(cv.run(rc), fpx, fwt, avail, ftn):
            chosen = rc
            break
    upper_x = 0.0 if age_inline else age_w
    if chosen is None and mark_rc and not TX.fits(cv.run(["join", " · ", mark_rc]), fpx, fwt, W - upper_x, ftn):
        marks = _short(marks)          # narrow cards: the short marker, never a truncated one
        mark_rc = [m[0] for m in marks]
    if chosen is None and (mark_rc or not age_inline):
        two_line = True           # markers (and a narrow card's age) take their own line above the time
        chosen = ["join", " · ", mark_rc] if mark_rc else None
        for t in tails:
            rc = ["join", " · ", mark_rc + t]
            if (mark_rc or t) and TX.fits(cv.run(rc), fpx, fwt, W - upper_x, ftn):
                chosen = rc
                break
    ftop = H - flh * (2 if two_line else 1)
    if chosen is not None:
        tok = "warn" if marks and marks[0][1] == "warn" else "muted"
        b = cv.text(["l", upper_x if two_line else 0], ftop, chosen, "footer", src="code",
                    max_w=(W - upper_x) if two_line else avail, tok=tok)
        if b is not None and ctx.state == "error_last_good":
            cv.hit(b.id, "Fix")
    if a:
        cv.time(["r", 0], H - flh, a, tf, "footer", tz="viewer", anchor="end")
        if age_w:
            if age_inline:
                b = cv.text(["r", right_w + 8], H - flh, ["d", "age", [a, fmt.iso(ctx.now)]], "footer",
                            src="code", max_w=age_w - 8, anchor="end", tok="warn")
            else:
                b = cv.text(["l", 0], ftop, ["d", "age", [a, fmt.iso(ctx.now)]], "footer",
                            src="code", max_w=age_w - 8, tok="warn")
            if b is not None:
                cv.live("age", prim=b.id, t=a)
    return {"x0": ["l", 0], "x1": ["r", 0], "y0": y0, "y1": ftop - SP["footer_gap"]}
