"""LayoutCtx, Draft and the prim builder every form draws with (CONTRACTS.md 5.1, 6.2).

Coordinates: x is an anchor (["l",px] | ["r",px] | ["c",px] | ["f",frac]) resolved
against the content box width; y is absolute from the content-box top. Text is
measured with HarfBuzz at the bucket FLOOR (min_w) and pre-broken there; plots are
fractional boxes that stretch between floor and ceiling while text never stretches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from . import fmt, prov
from . import text as TX
from . import tokens as TK
from .rec import R as RView
from .spans import Bucket, Span
from .types import Candidate, CardInput, DataRecord, Profile

TYPE = TK.TYPE
SP = TK.SPACE
VIZ = TK.VIZ


@lru_cache(maxsize=512)
def ty(role: str, px: int | None = None) -> tuple[int, int, int, bool]:
    """(px, weight, line height, tnum) for a text role; line height scales with px."""
    t = TYPE[role]
    p = px or t["px"]
    return p, t["wt"], round(t["lh"] * p / t["px"]), bool(t["tnum"])


@lru_cache(maxsize=256)
def asc_desc(px: float, wt: int) -> tuple[float, float]:
    m = TX.metrics(px, wt)
    return m["ascent"], m["descent"]


def baseline(top: float, role: str, px: int | None = None) -> float:
    p, wt, lh, _ = ty(role, px)
    a, d = asc_desc(p, wt)
    return top + (lh - (a + d)) / 2 + a


def rx(x: list, W: float) -> float:
    k, v = x[0], x[1]
    if k == "l":
        return v
    if k == "r":
        return W - v
    if k == "c":
        return W / 2 + v
    if k == "f":
        return v * W
    raise ValueError(x)


def shift(x: list, dx: float) -> list:
    """Move an anchor by dx px in reading direction (l/c: right, r: left)."""
    if x[0] in ("l", "c"):
        return [x[0], x[1] + dx]
    if x[0] == "r":
        return ["r", x[1] - dx]
    raise ValueError("cannot shift a fraction anchor by px")


class Ids:
    def __init__(self, start: int = 1):
        self.n = start

    def __call__(self) -> int:
        i = self.n
        self.n += 1
        return i


@dataclass
class LayoutCtx:
    cand: Candidate
    rec: DataRecord
    prof: Profile
    inp: CardInput
    span: Span
    bucket: Bucket
    body: dict                     # {x0, x1, y0, y1}: x as anchors, y absolute
    now: datetime
    rung: dict                     # ladder state (ladder.py)
    ids: Ids
    state: str = "ok"
    R: RView | None = None
    host: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def W(self) -> int:            # floor width (layout measures here)
        return self.bucket.min_w

    @property
    def Wmax(self) -> int:
        return self.bucket.max_w

    @property
    def phone(self) -> bool:
        return self.span.device == "phone"

    @property
    def wc(self) -> str:
        return self.span.wclass

    @property
    def rows(self) -> int:
        return self.span.rows

    @property
    def tz(self) -> str:
        return self.rec.context.card_tz


@dataclass
class Draft:
    prims: list = field(default_factory=list)
    live: list = field(default_factory=list)
    hitmap: list = field(default_factory=list)
    reading_order: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    state: str = "ok"
    prov: dict = field(default_factory=dict)       # prim id -> recipe (lint provenance)
    plan: str = ""
    meta: dict = field(default_factory=dict)       # lint hints: plot boxes, balanced, amplitude ...


@dataclass
class Box:
    """A placed element measured at the floor width."""
    x0: float
    x1: float
    top: float
    bottom: float
    id: int | None = None
    lines: int = 1

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.bottom - self.top


class Canvas:
    """Builds prims into a Draft. Every text prim carries a recipe (provenance)."""

    def __init__(self, ctx: LayoutCtx, draft: Draft | None = None):
        self.ctx = ctx
        self.d = draft or Draft()

    # ------------------------------------------------------------------ text
    def measure(self, s: str, role: str, px: int | None = None) -> float:
        p, wt, _, tn = ty(role, px)
        return TX.width(s, p, wt, tn)

    def run(self, recipe) -> str:
        return TX.clean(prov.run(recipe, self.ctx.R, self.ctx.inp, self.ctx.host))

    def fits(self, s: str, role: str, max_w: float, px: int | None = None) -> bool:
        p, wt, _, tn = ty(role, px)
        return TX.fits(s, p, wt, max_w, tn)

    def text(self, x: list, top: float, recipe, role: str, *, src: str, max_w: float,
             max_lines: int = 1, tok: str | None = None, anchor: str = "start", px: int | None = None,
             s: str | None = None, hit: str | None = None) -> Box | None:
        """Place a text prim with its first line box starting at `top`. Returns None for
        an empty string (a missing value drops its element; never a placeholder)."""
        s = self.run(recipe) if s is None else s
        if not s:
            return None
        p, wt, lh, tn = ty(role, px)
        cap = int(max_lines * max_w / (p * 0.25)) + 8     # more chars than can ever fit
        s_fit = s if len(s) <= cap else s[:cap] + " …"    # bounded shaping cost; still ellipsized
        lines, _trunc = TX.break_lines(s_fit, p, wt, max_w, max_lines, tn)
        pid = self.ctx.ids()
        dirn = TX.direction(s)
        if dirn == "rtl" and anchor == "start" and x[0] in ("l", "c"):
            # logical anchor (C-3): an rtl run starts at the slot's right edge
            x = shift(x, max_w)
        prim = {"k": "text", "id": pid, "x": list(x), "y": round(baseline(top, role, p), 2),
                "max_w": round(max_w, 2), "lines": lines, "role": role, "px": p,
                "tok": tok or TK.ROLE_TOKENS[role], "anchor": anchor, "dir": dirn, "src": src}
        self.d.prims.append(prim)
        self.d.reading_order.append(pid)
        self.d.prov[pid] = recipe
        if hit:
            self.d.hitmap.append({"prim": pid, "label": hit})
        w = max(TX.width(ln, p, wt, tn) for ln in lines)
        x0 = rx(x, self.ctx.W)
        if anchor == "middle":
            x0 -= w / 2
        elif (anchor == "end") != (dirn == "rtl"):
            x0 -= w
        return Box(x0, x0 + w, top, top + lh * len(lines), pid, len(lines))

    def time(self, x: list, top: float, t_iso: str, tfmt: str, role: str, *, tz: str = "card",
             show_zone: bool = False, anchor: str = "start", px: int | None = None,
             tok: str | None = None) -> Box:
        """An absolute time. Card-zone times are laid out at their exact width (the card
        zone is sealed); viewer-zone times reserve the widest value of the format."""
        p, wt, lh, tn = ty(role, px)
        if tz == "card":
            s = fmt.format_time(t_iso, tfmt, self.ctx.tz, False)
        else:
            s = fmt.WIDEST[tfmt]
        w = TX.width(s, p, wt, tn)
        if show_zone:
            w += TX.width(fmt.ZONE_WIDEST, p, wt, tn)
        w = w * (1 + TX.MARGIN) + 0.5
        pid = self.ctx.ids()
        self.d.prims.append({"k": "time", "id": pid, "x": list(x), "y": round(baseline(top, role, p), 2),
                             "max_w": round(w, 2), "t": t_iso, "fmt": tfmt, "tz": tz, "zone": self.ctx.tz,
                             "role": role, "px": p, "tok": tok or TK.ROLE_TOKENS[role], "anchor": anchor,
                             "show_zone": show_zone})
        self.d.reading_order.append(pid)
        x0 = rx(x, self.ctx.W)
        if anchor == "middle":
            x0 -= w / 2
        elif anchor == "end":
            x0 -= w
        return Box(x0, x0 + w, top, top + lh, pid)

    # ------------------------------------------------------------------ marks
    def _add(self, prim: dict) -> int:
        prim["id"] = self.ctx.ids()
        self.d.prims.append(prim)
        return prim["id"]

    def rect(self, x0, x1, y0, y1, tok, *, r=0, stroke=None) -> int:
        return self._add({"k": "rect", "x0": list(x0), "x1": list(x1), "y0": round(y0, 2), "y1": round(y1, 2),
                          "tok": tok, "r": r, "stroke": stroke})

    def line(self, x0, y0, x1, y1, tok, *, w=1, dash=None) -> int:
        return self._add({"k": "line", "x0": list(x0), "y0": round(y0, 2), "x1": list(x1), "y1": round(y1, 2),
                          "tok": tok, "w": w, "dash": dash})

    def tri(self, x, y, size, dirn, tok) -> int:
        return self._add({"k": "tri", "x": list(x), "y": round(y, 2), "size": size, "dir": dirn, "tok": tok})

    def dot(self, x, y, r, tok, *, ring=None, ring_w=0) -> int:
        return self._add({"k": "dot", "x": list(x), "y": round(y, 2), "r": r, "tok": tok, "ring": ring,
                          "ring_w": ring_w})

    def path(self, box: dict, pts: list, tok, *, w=2, style="solid", fill=None) -> int:
        return self._add({"k": "path", "box": box, "pts": [[round(a, 4), round(b, 4)] for a, b in pts],
                          "tok": tok, "w": w, "style": style, "fill": fill})

    def cells(self, box, cols, rows, v, *, ramp="seq", steps=5, gap=1) -> int:
        return self._add({"k": "cells", "box": box, "cols": cols, "rows": rows, "v": v, "ramp": ramp,
                          "steps": steps, "gap": gap})

    def image(self, box, ref, fit, crop=None) -> int:
        return self._add({"k": "image", "box": box, "ref": ref, "fit": fit, "crop": crop})

    def basemap(self, box, bbox) -> int:
        return self._add({"k": "basemap", "box": box, "asset": "world110m", "bbox": bbox,
                          "land": "map-land", "stroke": "map-stroke"})

    def icon(self, x, y, size, name, tok) -> int:
        return self._add({"k": "icon", "x": list(x), "y": round(y, 2), "size": size, "name": name, "tok": tok})

    # ------------------------------------------------------------------ live
    def live(self, k: str, **args):
        self.d.live.append({"k": k, "args": args})

    def hit(self, pid: int, label: str):
        self.d.hitmap.append({"prim": pid, "label": label})

    def drop(self, what: str):
        if what not in self.d.dropped:
            self.d.dropped.append(what)

    # ------------------------------------------------------------------ vertical moves
    def mark(self) -> int:
        return len(self.d.prims)

    def shift(self, mark: int, dy: float):
        """Move every prim created since `mark` down by dy (balancing a block)."""
        if not dy:
            return
        for p in self.d.prims[mark:]:
            for k in ("y", "y0", "y1"):
                if k in p and isinstance(p[k], (int, float)):
                    p[k] = round(p[k] + dy, 2)
            if "box" in p:
                p["box"] = dict(p["box"], y0=round(p["box"]["y0"] + dy, 2), y1=round(p["box"]["y1"] + dy, 2))
        ids = {p["id"] for p in self.d.prims[mark:]}
        for pl in self.d.meta.get("plots", []):
            if pl.get("id") in ids:
                pl["y0"] += dy
                pl["y1"] += dy
        for lb in self.d.live:
            b = lb["args"].get("box")
            if b is not None and any(i in ids for i in lb["args"].get("prims", [])):
                lb["args"]["box"] = dict(b, y0=round(b["y0"] + dy, 2), y1=round(b["y1"] + dy, 2))

    def balance(self, mark: int, content_top: float, content_bottom: float, y0: float, y1: float):
        """Centre a block vertically inside [y0, y1] (the 'few'/'single' designed state)."""
        free = (y1 - y0) - (content_bottom - content_top)
        if free > 0:
            self.shift(mark, round(y0 + free / 2 - content_top, 1))


def pbox(x0: list, x1: list, y0: float, y1: float) -> dict:
    return {"x0": list(x0), "x1": list(x1), "y0": round(y0, 2), "y1": round(y1, 2)}
