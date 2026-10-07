"""LINT (CONTRACTS.md 6.4): closed codes, red blocks the layout, amber is reported.

Geometry is resolved at BOTH ends of the bucket width (floor catches squashing,
ceiling catches stretching/sparseness) and in every timed-variant "world" (each
variant of a timed_variants slot is checked with the others hidden).

`prov` (prim id -> recipe) and `meta` (form hints) come from the Draft when lint is
called by layout_span; an external caller without them gets the provenance check
as a red `provenance` for every text prim that cannot be reconstructed.
"""
from __future__ import annotations

import itertools
import time
import unicodedata
from functools import lru_cache
from urllib.parse import urlsplit

from . import fmt, prov
from . import text as TX
from . import tokens as TK
from .ctx import asc_desc, ty
from .rec import R as RView
from .spans import Bucket
from .types import LintIssue, LintResult

RED, AMBER = "red", "amber"
VALUE_ROLES = {"hero", "headline", "display", "row", "row-strong", "sub", "delta", "label", "badge"}
VAR_TEXT_ROLES = {"item", "body", "name", "row", "sub", "meta", "index"}
_STRIP = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF} | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def resolve_x(x: list, left: float, width: float) -> float:
    """Anchor -> absolute px (shared with painters)."""
    k = x[0]
    if k == "l":
        return left + x[1]
    if k == "r":
        return left + width - x[1]
    if k == "c":
        return left + width / 2 + x[1]
    if k == "f":
        return left + x[1] * width
    if k == "m":
        a, b = resolve_x(x[1], left, width), resolve_x(x[2], left, width)
        return a + (b - a) * x[3]
    raise ValueError(f"unknown x anchor {x!r}")


# ----------------------------------------------------------------------------- colour
def _lum(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


@lru_cache(maxsize=256)
def contrast(tok: str, theme: str, bg: str = "panel") -> float:
    br, bgc, bb, _ = TK.rgba(theme, bg)
    r, g, b, a = TK.rgba(theme, tok)
    comp = (r * a + br * (1 - a), g * a + bgc * (1 - a), b * a + bb * (1 - a))
    l1, l2 = _lum(comp), _lum((br, bgc, bb))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ----------------------------------------------------------------------------- geometry
def text_box(p: dict, W: float) -> tuple[float, float, float, float, list]:
    """(x0, y0, x1, y1, line widths) of a text/time prim at content width W."""
    px = p["px"]
    role = p["role"]
    _, wt, lh, tn = ty(role, px)
    a, d = asc_desc(px, wt)
    if p["k"] == "text":
        ws = [TX.width(ln, px, wt, tn) for ln in p["lines"]]
        n = len(p["lines"])
    else:
        ws = [p["max_w"]]
        n = 1
    w = max(ws) if ws else 0
    x = resolve_x(p["x"], 0, W)
    anc = p.get("anchor", "start")
    rtl = p.get("dir") == "rtl"
    if anc == "middle":
        x0 = x - w / 2
    elif (anc == "end") != rtl:
        x0 = x - w
    else:
        x0 = x
    y = p["y"]
    return x0, y - a, x0 + w, y + (n - 1) * lh + d, ws


def mark_box(p: dict, W: float):
    k = p["k"]
    if k in ("dot",):
        r = p["r"] + (p.get("ring_w") or 0)
        x = resolve_x(p["x"], 0, W)
        return x - r, p["y"] - r, x + r, p["y"] + r
    if k in ("tri", "icon"):
        s = p["size"] / 2
        x = resolve_x(p["x"], 0, W)
        return x - s, p["y"] - s, x + s, p["y"] + s
    if k in ("rect",):
        return resolve_x(p["x0"], 0, W), p["y0"], resolve_x(p["x1"], 0, W), p["y1"]
    if k == "line":
        xa, xb = resolve_x(p["x0"], 0, W), resolve_x(p["x1"], 0, W)
        return min(xa, xb), min(p["y0"], p["y1"]), max(xa, xb), max(p["y0"], p["y1"])
    if k in ("path", "cells", "image", "basemap"):
        b = p["box"]
        return resolve_x(b["x0"], 0, W), b["y0"], resolve_x(b["x1"], 0, W), b["y1"]
    return None


def _inter(a, b, pad=0.5) -> bool:
    return a[0] < b[2] - pad and b[0] < a[2] - pad and a[1] < b[3] - pad and b[1] < a[3] - pad


def _path_pts(p: dict, W: float):
    b = p["box"]
    x0, x1 = resolve_x(b["x0"], 0, W), resolve_x(b["x1"], 0, W)
    return [(x0 + fx * (x1 - x0), b["y0"] + fy * (b["y1"] - b["y0"])) for fx, fy in p["pts"]]


def _worlds(clir: dict) -> list[tuple[set, str | None]]:
    """[(hidden prim ids, variant label)] - one world per timed variant."""
    tv = [lb for lb in clir.get("live", []) if lb["k"] == "timed_variants"]
    if not tv:
        return [(set(), None)]
    base_hidden = set()
    for lb in tv:
        for v in lb["args"]["variants"][1:]:
            base_hidden |= set(v["prims"])
    worlds = []
    for bi, lb in enumerate(tv):
        vs = lb["args"]["variants"]
        for vi, v in enumerate(vs):
            hid = set(base_hidden)
            for vj, v2 in enumerate(vs):
                if vj != vi:
                    hid |= set(v2["prims"])
            hid -= set(v["prims"])
            worlds.append((hid, v.get("t_from")))
        if bi == 0 and len(worlds) > 24:
            break
    return worlds or [(set(), None)]


# ----------------------------------------------------------------------------- main
def lint(clir: dict, rec, roles, prof, cand, inp, bucket: Bucket, *, times: list | None = None,
         prov_map: dict | None = None, meta: dict | None = None, host: str = "") -> LintResult:
    t0 = time.perf_counter()
    meta = meta or {}
    issues: list[LintIssue] = []
    seen = set()

    def add(code, sev, prim=None, width=None, at=None, detail=""):
        key = (code, prim, width, at)
        if key in seen:
            return
        seen.add(key)
        issues.append(LintIssue(code, sev, prim, width, at, detail[:160]))

    prims = clir["prims"]
    by = {p["id"]: p for p in prims}
    texts = [p for p in prims if p["k"] in ("text", "time")]
    H = bucket.h
    phone = bucket.span.device == "phone"
    title_ids = {p["id"] for p in texts if p["role"] == "title"}
    footer_ids = {p["id"] for p in texts if p["role"] == "footer"}
    body = meta.get("body") or {"y0": 0, "y1": H}
    worlds = _worlds(clir)

    # ---------------------------------------------------------------- per-prim checks (width-free)
    for p in texts:
        if p["px"] < 11:
            add("tiny_text", RED, p["id"], detail=str(p["px"]))
        if p["k"] == "text":
            px = p["px"]
            _, wt, _, tn = ty(p["role"], px)
            for ln in p["lines"]:
                if TX.width(ln, px, wt, tn) * (1 + TX.MARGIN) > p["max_w"] + 0.6:
                    add("overflow", RED, p["id"], detail=ln[:40])
                if any(ord(ch) in _STRIP or (unicodedata.category(ch) == "Cc") for ch in ln):
                    add("bidi_clean", RED, p["id"])
                cov = TX.coverage(ln, wt)
                if cov["missing"]:
                    # Hybrid Phase 1a-2: the browser paints this text, so an unknown
                    # glyph is a warning, not a block (the measurer already uses a
                    # tofu fallback advance; a future server-side PNG painter can
                    # re-raise this to red). See docs/internal/ni-format.md §34.
                    add("glyph_missing", AMBER, p["id"], detail="".join(cov["missing"])[:10])
                elif cov["fallback"]:
                    add("glyph_fallback", AMBER, p["id"], detail=",".join(cov["fallback"]))
            trunc = any(ln.endswith("…") for ln in p["lines"])
            if trunc:
                role = p["role"]
                if role == "hero":
                    add("truncated_hero", RED, p["id"])
                elif role == "headline":
                    add("truncated_headline", RED, p["id"])
                elif role == "title":
                    add("truncated_title", AMBER, p["id"])
                elif p.get("src") == "data" and _is_value(prov_map, p["id"]):
                    add("truncated_value", RED, p["id"])
        else:
            need = TX.width(fmt.WIDEST[p["fmt"]] if p["tz"] == "viewer" else
                            fmt.format_time(p["t"], p["fmt"], p["zone"]), p["px"], ty(p["role"], p["px"])[1],
                            ty(p["role"], p["px"])[3])
            if p["max_w"] + 0.6 < need:
                add("truncated_asof" if p["role"] == "footer" else "overflow", RED, p["id"])
        tok = p.get("tok")
        for theme in TK.THEMES:
            c = contrast(tok, theme)
            large = p["px"] >= 24 or (p["px"] >= 19 and ty(p["role"], p["px"])[1] >= 600)
            if c < (3.0 if large else 4.5):
                add("contrast", RED, p["id"], detail=f"{tok} {theme} {c:.2f}")
                break

    # ---------------------------------------------------------------- geometry per width x world
    widths = [bucket.min_w, bucket.max_w]
    for W in widths:
        tb_all = {p["id"]: text_box(p, W)[:4] for p in texts}
        mb_all = {p["id"]: mark_box(p, W) for p in prims if p["k"] not in ("text", "time")}
        for hidden, wlabel in worlds:
            vis = [p for p in prims if p["id"] not in hidden]
            tb = {p["id"]: tb_all[p["id"]] for p in vis if p["k"] in ("text", "time")}
            mb = {p["id"]: mb_all[p["id"]] for p in vis if p["k"] not in ("text", "time")}
            # out of box
            for pid, b in list(tb.items()) + [(k, v) for k, v in mb.items() if v]:
                bleed = 8.6 if by[pid]["k"] == "rect" and by[pid]["tok"] == "row-hi" else 0.6
                if b[0] < -bleed or b[2] > W + bleed or b[1] < -0.6 or b[3] > H + 0.6:
                    add("out_of_box", RED, pid, W, wlabel, f"{b[0]:.0f},{b[1]:.0f},{b[2]:.0f},{b[3]:.0f} in {W}x{H}")
            # text-text overlap (sweep by top)
            items = sorted(tb.items(), key=lambda kv: kv[1][1])
            for i, (ia, a) in enumerate(items):
                for ib, b in items[i + 1:]:
                    if b[1] >= a[3] - 0.5:
                        break
                    if _inter(a, b):
                        add("overlap", RED, ia, W, wlabel, f"text {ia}~{ib}")
            # mark-label overlap: dots, tris, icons, and path polylines under labels
            marks = [(k, v) for k, v in mb.items() if v and by[k]["k"] in ("dot", "tri", "icon")]
            for ia, a in tb.items():
                if ia in title_ids or ia in footer_ids:
                    continue
                for ib, b in marks:
                    if _inter(a, b, pad=1.0):
                        add("overlap", RED, ia, W, wlabel, f"mark {ib}")
            # lines: a static line never crosses a label; a now-marker line (it sweeps its box over time)
            # must list every label inside its sweep in `gaps` so painters break the line around it
            now_lines = {}
            for lb in clir.get("live", []):
                if lb["k"] == "now_marker":
                    for i in lb["args"].get("prims", []):
                        now_lines[i] = lb["args"]
            for p in vis:
                if p["k"] != "line":
                    continue
                lb_ = mb.get(p["id"])
                if not lb_:
                    continue
                if p["id"] in now_lines:
                    a_ = now_lines[p["id"]]
                    bx = a_["box"]
                    sweep = (resolve_x(bx["x0"], 0, W) - 1, min(p["y0"], p["y1"]), resolve_x(bx["x1"], 0, W) + 1,
                             max(p["y0"], p["y1"]))
                    gaps = set(a_.get("gaps") or [])
                    for ia, a in tb.items():
                        if ia in title_ids or ia in footer_ids or ia in gaps:
                            continue
                        if _inter(a, sweep, pad=1.0):
                            add("overlap", RED, ia, W, wlabel, f"now line {p['id']} sweeps label")
                    continue
                for ia, a in tb.items():
                    if ia in title_ids or ia in footer_ids:
                        continue
                    if _inter(a, (lb_[0] - 0.5, lb_[1], lb_[2] + 0.5, lb_[3]), pad=1.0):
                        add("overlap", RED, ia, W, wlabel, f"line {p['id']}")
            for p in vis:
                if p["k"] != "path" or not p.get("fill") or len(p["pts"]) < 3:
                    continue
                bb = p["box"]
                for ia, a in tb.items():
                    if ia in title_ids or ia in footer_ids:
                        continue
                    bx0, bx1 = resolve_x(bb["x0"], 0, W), resolve_x(bb["x1"], 0, W)
                    if a[2] > bx0 and a[0] < bx1 and a[1] + 1 < bb["y1"] < a[3] - 1:
                        add("overlap", RED, ia, W, wlabel, f"label straddles the fill baseline of {p['id']}")
            for p in vis:
                if p["k"] != "path":
                    continue
                pts = _path_pts(p, W)
                for ia, a in tb.items():
                    if ia in title_ids or ia in footer_ids or a[3] < p["box"]["y0"] or a[1] > p["box"]["y1"]:
                        continue
                    if any(a[0] + 1 < x < a[2] - 1 and a[1] + 1 < y < a[3] - 1 for x, y in pts):
                        add("overlap", RED, ia, W, wlabel, f"path {p['id']}")
            # sparseness (body only)
            _sparse(add, vis, tb, mb, body, W, wlabel, meta, title_ids | footer_ids)
        # stretch (amber): same-baseline neighbours drift apart at the ceiling
    if len(widths) == 2:
        _stretch(add, prims, widths, title_ids | footer_ids)

    # ---------------------------------------------------------------- body content (L-BODY-EMPTY)
    body_ids = [p["id"] for p in prims if p["id"] not in title_ids and p["id"] not in footer_ids
                and not (p["k"] == "rect" and p.get("tok") == "row-hi")]
    if not body_ids:
        add("L-BODY-EMPTY", RED, detail="no content between the header and the footer")
    _value_sanity(add, texts, prov_map, rec, inp, title_ids, footer_ids)

    # ---------------------------------------------------------------- plot proportions
    for pl in meta.get("plots", []):
        bh = body["y1"] - body["y0"]
        need = TK.VIZ["plot_min_share"] * bh
        if pl.get("cap"):
            need = min(need, pl["cap"] * 0.5)
        if pl.get("min_share") and bh > 0 and (pl["y1"] - pl["y0"]) < need - 1e-6:
            add("plot_min_share", RED, pl.get("id"), detail=f"{(pl['y1'] - pl['y0']) / bh:.2f}")
        if pl.get("amplitude") is not None:
            need = TK.VIZ["amplitude_min"]["phone" if phone else "desktop"]
            if pl["amplitude"] + 1e-6 < need:
                add("amplitude_min", RED, pl.get("id"), detail=f"{pl['amplitude']:.0f} < {need}")

    # ---------------------------------------------------------------- semantic / honesty / safety
    R = RView(rec) if rec is not None else None
    title = fmt.one_line(inp.title).casefold() if inp is not None else ""
    for p in texts:
        if p["k"] != "text" or p["id"] in title_ids:
            continue
        s = " ".join(p["lines"])
        if title and s.casefold() == title:
            add("title_echo", RED, p["id"])
    if prov_map is not None and R is not None:
        _provenance(add, texts, prov_map, R, inp, host, rec)
    else:
        for p in texts:
            if p["k"] == "text" and p["id"] not in title_ids:
                add("provenance", RED, p["id"], detail="no recipe")
    recipes = list((prov_map or {}).values())
    rs = repr(recipes)
    if rec is not None:
        from .shell import inferred_dates
        if rec.flags.get("sample") and "'sample'" not in rs and "'sample_short'" not in rs:
            add("sample_marked", RED)
        if inferred_dates(rec):
            if "'dates_inferred'" not in rs:
                add("inferred_marked", RED)
            for p in texts:
                if p["k"] == "time" and "EEE" in p["fmt"]:
                    add("unverified_weekday", RED, p["id"])
            if "'EEE" in rs:
                add("unverified_weekday", RED)
        n = len(rec.rows)
        if n == 0 and not rec.error and meta.get("record_form") and clir.get("state") not in ("empty", "not_ready", "needs_update") \
                and not meta.get("calm"):
            add("L-EMPTY", RED)
        if rec.flags.get("lost_columns"):
            add("L-LOSS", AMBER)
        for pid, fname in (meta.get("time_src") or {}).items():
            f = R.f.get(fname) if R else None
            if f is not None and f.wallclock and by.get(pid, {}).get("tz") != "card":
                add("L-TIME", RED, pid)
        for i in meta.get("rows_shown_agg", []):
            if rec.row_meta and i < len(rec.row_meta) and rec.row_meta[i].aggregate:
                add("L-AGG", RED, detail=f"row {i}")
    if meta.get("today") and meta.get("today") != fmt.local_date(fmt.iso(meta["now"]), rec.context.card_tz).isoformat():
        add("stale_day", RED)
    ext = meta.get("extrema") or []
    for (k1, y1_), (k2, y2_) in itertools.pairwise(ext):
        if k1 != k2 and ((k1 == "hi" and y1_ > y2_ + 0.5) or (k1 == "lo" and y1_ < y2_ - 0.5)):
            add("extrema_inverted", RED)
    for lb in clir.get("live", []):
        if lb["k"] != "timed_variants":
            continue
        vs = lb["args"]["variants"]
        for va, vb in itertools.pairwise(vs):
            if va.get("t_to") != vb.get("t_from"):
                add("headline_recompute", RED, detail="variant windows not contiguous")
    for (tf, tt, te) in meta.get("headline_events", []):
        if tt is not None and te != tt:
            add("headline_recompute", RED, detail=f"{te} != {tt}")
    if "'interp_legend'" not in rs and bucket.span.rows >= 2:
        if any(p["k"] == "path" and p.get("style") == "interp" for p in prims):
            add("interp_marked", RED)
    if cand is not None and rec is not None:
        names = {f.name for f in rec.fields}
        try:
            from .registry import FORMS as _F
            slots = set(_F[cand.form].slots)
        except Exception:
            slots = set(cand.bindings or {})
        for slot, fname in (cand.bindings or {}).items():
            if slot not in slots:
                continue
            for fn in (fname if isinstance(fname, list) else [fname]):
                if fn is not None and fn not in names:
                    add("L-ROLE", RED, detail=f"{slot}:{fn}")
    qs = []
    if roles is not None:
        qs = roles.get("questions") if isinstance(roles, dict) else getattr(roles, "questions", [])
    if (rec is not None and rec.flags.get("open_filter")) or \
            any((q.get("id") if isinstance(q, dict) else q.id).startswith("q_param") for q in qs or []):
        add("L-FILTER", RED, detail="the ask names rows the card does not filter yet (open C2 question)")
    if prof is not None and cand is not None:
        try:
            from .registry import FORMS
            cov = set(FORMS[cand.form].covers(cand, prof, bucket.span.sclass))
            for w in prof.wants or []:
                if w not in cov:
                    add("L-ASK", AMBER, detail=w)
        except Exception:
            pass
    for u in meta.get("unit_missing", []):
        add("L-UNIT", RED, detail=u)
    # links
    for h in clir.get("hitmap", []):
        href = h.get("href")
        if href is not None and not link_ok(href):
            add("link_policy", RED, h.get("prim"), detail=href[:60])
    # a11y
    if not clir.get("summary"):
        add("summary", RED)
    ro = set(clir.get("reading_order", []))
    if not {p["id"] for p in texts} <= ro:
        add("reading_order", RED)
    _hero_dominant(add, texts, title_ids, footer_ids)
    _colour_only(add, prims, texts)

    red = [i for i in issues if i.sev == RED]
    return LintResult(ok=not red, issues=issues, widths=widths, times=list(times or []),
                      ms=(time.perf_counter() - t0) * 1000)


def _is_value(prov_map, pid) -> bool:
    """A formatted number or derived value (not free text that happens to hold digits)."""
    if prov_map is None:
        return True
    rc = prov_map.get(pid)
    while rc and rc[0] == "part":
        rc = rc[2]
    if not rc:
        return False
    if rc[0] in ("cell", "d"):
        return True
    if rc[0] == "join":
        return any(_is_value({0: x}, 0) for x in rc[2])
    return False


def link_ok(href: str) -> bool:
    try:
        u = urlsplit(href)
    except ValueError:
        return False
    return u.scheme == "https" and not u.username and not u.password and u.port in (None, 443) and bool(u.hostname)


def _sparse(add, vis, tb, mb, body, W, wlabel, meta, skip):
    y0, y1 = body["y0"], body["y1"]
    bh = y1 - y0
    if bh <= 0:
        return
    iv = []
    hx = []
    for p in vis:
        pid = p["id"]
        if pid in skip:
            continue
        if pid in tb:
            b = tb[pid]
            if p["k"] == "text" and p["role"] in VAR_TEXT_ROLES and p["x"][0] != "c":
                x = resolve_x(p["x"], 0, W)
                anc = p.get("anchor", "start")
                if anc == "start" and p.get("dir") != "rtl":
                    hx.append((b[0], max(b[2], x + p["max_w"])))
                elif anc == "end" or p.get("dir") == "rtl":
                    hx.append((min(b[0], x - p["max_w"]), b[2]))
                else:
                    hx.append((b[0], b[2]))
            else:
                hx.append((b[0], b[2]))
        else:
            b = mb.get(pid)
            if not b or p["k"] == "rect" and p["tok"] in ("row-hi",):
                continue
            hx.append((b[0], b[2]))
        if b[3] > y0 - 0.5 and b[1] < y1 + 0.5:
            iv.append((max(b[1], y0), min(b[3], y1)))
    if not iv:
        return
    iv.sort()
    merged = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    top_gap = merged[0][0] - y0
    bot_gap = y1 - merged[-1][1]
    inner = max([b[0] - a[1] for a, b in itertools.pairwise(merged)] or [0])
    # L-HOLLOW: any empty band (top, bottom or between blocks) over 30% of the body (48 px floor for the
    # 1-row body), whether or not the gaps are balanced. The designed calm sentence is exempt; a forced
    # count state (a list that shrank on refresh) is reported amber.
    lim = max(0.30 * bh, 60.0)
    # a list stops when its next item does not fit: the bottom band may hold up to that item's height
    bot_ok = max(lim, float(meta.get("slack") or 0) + 12)
    band = max(top_gap, inner, bot_gap if bot_gap > bot_ok else 0)
    if band > lim and not meta.get("calm"):
        sev = AMBER if meta.get("forced") in ("empty", "one", "few") else RED
        add("L-HOLLOW", sev, width=W, at=wlabel,
            detail=f"empty band {band:.0f} of {bh:.0f} (top {top_gap:.0f} inner {inner:.0f} bottom {bot_gap:.0f})")
    # horizontal: content must not leave an unbalanced empty side > 35% of the width
    if hx and W >= 400:
        lo = min(a for a, _ in hx)
        hi = max(b for _, b in hx)
        left, right = lo, W - hi
        if max(left, right) > 0.35 * W and abs(left - right) > 24:
            add("too_sparse", RED, width=W, at=wlabel, detail=f"x {lo:.0f}..{hi:.0f} of {W}")


def _stretch(add, prims, widths, skip):
    rows: dict = {}
    for p in prims:
        if p["k"] in ("text", "time") and p["id"] not in skip:
            rows.setdefault(round(p["y"]), []).append(p)
    for ps in rows.values():
        if len(ps) < 2:
            continue
        ga, gb = [], []
        for W, acc in ((widths[0], ga), (widths[1], gb)):
            bx = sorted(text_box(p, W)[:4] for p in ps)
            acc.extend(b[0] - a[2] for a, b in itertools.pairwise(bx))
        for a, b in zip(ga, gb):
            if b > 48 and b > 2 * max(a, 1):
                add("stretch", AMBER, ps[0]["id"], detail=f"{a:.0f}->{b:.0f}")
                break


def _provenance(add, texts, prov_map, R, inp, host, rec):
    for p in texts:
        if p["k"] != "text":
            continue
        rc = prov_map.get(p["id"])
        if rc is None:
            add("provenance", RED, p["id"], detail="no recipe")
            continue
        try:
            want = TX.clean(prov.run(rc, R, inp, host))
        except Exception as ex:   # a recipe that no longer runs is not reconstructible
            add("provenance", RED, p["id"], detail=f"{type(ex).__name__}")
            continue
        got = " ".join(p["lines"])
        if got.replace(" ", "") != fmt.one_line(want).replace(" ", ""):
            g = got.rstrip("…").rstrip()
            if not (got.endswith("…") and fmt.one_line(want).replace(" ", "").startswith(g.replace(" ", ""))):
                add("provenance", RED, p["id"], detail=f"{got[:30]!r} != {want[:30]!r}")
        if prov.uses_data(rc) and p.get("src") not in ("data",):
            add("provenance", RED, p["id"], detail="data read under non-data src")
        for fname, row, opts in prov.cells_used(rc):
            f = R.f.get(fname)
            if f is None:
                add("L-ROLE", RED, p["id"], detail=fname)
                continue
            if opts.get("dec") is not None and not opts.get("derived") and not opts.get("compact") \
                    and f.precision is not None:
                pub = f.precision - (2 if f.type == "percent" and f.scale == "0..1" else 0)
                if opts["dec"] < pub:
                    add("L-PREC", RED, p["id"], detail=f"{fname} {opts['dec']}<{pub}")


def _hero_dominant(add, texts, title_ids, footer_ids):
    body = [p for p in texts if p["id"] not in title_ids and p["id"] not in footer_ids]
    if not body:
        return
    top = max(p["px"] for p in body)
    heroes = [p for p in body if p["role"] in ("hero", "headline", "display")]
    if heroes and max(p["px"] for p in heroes) < top:
        add("hero_dominant", RED, detail="primary is not the largest text")
    title_px = max([p["px"] for p in texts if p["id"] in title_ids] or [0])
    if heroes and title_px > max(p["px"] for p in heroes):
        add("hero_dominant", RED, detail="title louder than the primary")


def _colour_only(add, prims, texts):
    tris = [p for p in prims if p["k"] == "tri"]
    for p in texts:
        if p.get("tok") not in ("viz-pos", "viz-neg"):
            continue
        s = " ".join(p.get("lines", [""])) if p["k"] == "text" else ""
        signed = s[:1] in ("+", fmt.MINUS, "-")
        near = any(abs(t["y"] - (p["y"] - p["px"] * 0.35)) < p["px"] and t["tok"] == p["tok"] for t in tris)
        if not (signed and near):
            add("colour_only", RED, p["id"])


_BOOLS = {"true", "false"}
_CODE_TOK = __import__("re").compile(r"^[A-Z0-9/+\-]{3,}$")
PRIMARY_ROLES = {"hero", "headline", "display"}


def _fields_in(rc, out=None):
    out = [] if out is None else out
    if not isinstance(rc, list) or not rc:
        return out
    k = rc[0]
    if k in ("cell", "raw"):
        out.append(rc[1])
    elif k == "tpl":
        for v in rc[2].values():
            _fields_in(v, out)
    elif k == "join":
        for p in rc[2]:
            _fields_in(p, out)
    elif k in ("abs", "lower"):
        _fields_in(rc[1], out)
    elif k == "d" and rc[1] in ("diff", "pct", "ratio", "compass", "agg"):
        out.append(rc[2][0])
    return out


def _value_sanity(add, texts, prov_map, rec, inp, title_ids, footer_ids):
    """L-VALUE-SANITY (red): a raw boolean shown as a word, a raw code string as the primary, or a
    coordinate / zone / year / storage size as the primary fact (unless the ask names it)."""
    if rec is None:
        return
    from .rec import is_context
    ask = (getattr(inp, "ask", "") or "").lower()
    by = {f.name: f for f in rec.fields}
    for p in texts:
        if p["k"] != "text" or p["id"] in title_ids or p["id"] in footer_ids or p.get("src") != "data":
            continue
        s = " ".join(p["lines"]).strip()
        rc = (prov_map or {}).get(p["id"])
        if rc and rc[0] == "part":
            continue
        if s.lower() in _BOOLS:
            add("L-VALUE-SANITY", RED, p["id"], detail=f"raw boolean {s!r}")
            continue
        if p["role"] not in PRIMARY_ROLES:
            continue
        toks = s.split()
        codes = [t for t in toks if _CODE_TOK.match(t) and any(ch.isdigit() for ch in t)]
        if len(toks) >= 3 and len(codes) >= 3 and len(codes) >= 0.5 * len(toks):
            add("L-VALUE-SANITY", RED, p["id"], detail="a raw code string as the primary")
            continue
        for fn in _fields_in(rc):
            f = by.get(fn)
            if f is None:
                continue
            lab = (f.label or "").lower()
            if lab and lab in ask:
                continue
            i = rec.fields.index(f)
            vals = [r[i] for r in rec.rows[:5] if r[i] is not None]
            yearish = vals and all(isinstance(v, int) and 1900 <= v <= 2100 for v in vals) and \
                any(w in lab for w in ("year", "season"))
            tail = f.path.split(".")[-1].replace("[]", "").replace("_", " ").lower().split()
            sizeish = "size" in tail and not f.unit
            if is_context(f, rec) or yearish or sizeish:
                add("L-VALUE-SANITY", RED, p["id"], detail=f"{fn} ({lab}) is not a primary fact")
                break
