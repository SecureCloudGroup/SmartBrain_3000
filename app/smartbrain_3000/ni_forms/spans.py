"""Resizable card spans (FROZEN by CONTRACTS.md section 4). Owner: architect.

A card occupies a snapping grid span. Desktop: cols 1..4 x rows 1..3. Phone: a
"half" (1 of 2 phone columns) or "full" (both) x rows 1..3.

Every span maps to a Bucket: the content-box width RANGE (card minus 16 px
padding each side) and the fixed content-box height. Layout lints at BOTH ends
of the width range (floor = squash risk, ceiling = stretch/sparse risk).

Geometry (all CSS px):
  desktop column outer width c in [220, 256], gap 16, pad 16
      content_w(cols) = cols*c + (cols-1)*16 - 32
      -> 1: 188-224   2: 424-496   3: 660-768   4: 896-1040
  phone gap 12, pad 16; operator-fixed content widths:
      half: 128-160   full: 288-366
  row 176, content_h(rows) = rows*176 + (rows-1)*gap - 32
      desktop: 144 / 336 / 528      phone: 144 / 332 / 520
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Device = Literal["desktop", "phone"]

ROW_PX = 176
PAD = 16
GAP = {"desktop": 16, "phone": 12}
DESK_COL_OUTER = (220, 256)
PHONE_W = {1: (128, 160), 2: (288, 366)}   # phone cols: 1 = half, 2 = full

# Width classes: the design unit for content plans. Phone half and desktop 1-col
# are different classes on purpose (128 vs 188 px floors are different designs).
WCLASS = {("phone", 1): "W1", ("desktop", 1): "W2", ("phone", 2): "W3",
          ("desktop", 2): "W4", ("desktop", 3): "W5", ("desktop", 4): "W6"}
WCLASS_ORDER = ["W1", "W2", "W3", "W4", "W5", "W6"]


@dataclass(frozen=True, order=True)
class Span:
    device: Device
    cols: int      # desktop 1..4; phone 1 (half) | 2 (full)
    rows: int      # 1..3

    def __post_init__(self):
        maxc = 4 if self.device == "desktop" else 2
        if not (1 <= self.cols <= maxc and 1 <= self.rows <= 3):
            raise ValueError(f"invalid span {self}")

    @property
    def key(self) -> str:
        """Stable id used in file names, dict keys and the proof: d2x1, p1x2 ..."""
        return f"{self.device[0]}{self.cols}x{self.rows}"

    @staticmethod
    def parse(key: str) -> Span:
        dev = {"d": "desktop", "p": "phone"}[key[0]]
        c, r = key[1:].split("x")
        return Span(dev, int(c), int(r))

    @property
    def wclass(self) -> str:
        return WCLASS[(self.device, self.cols)]

    @property
    def sclass(self) -> str:
        """Span class = width class + rows, e.g. 'W4R2'. Forms declare plans per sclass."""
        return f"{self.wclass}R{self.rows}"


@dataclass(frozen=True)
class Bucket:
    span: Span
    min_w: int
    max_w: int
    h: int          # content-box height (fixed; rows never stretch)

    @property
    def widths(self) -> tuple[int, int]:
        return (self.min_w, self.max_w)


def content_h(device: Device, rows: int) -> int:
    return rows * ROW_PX + (rows - 1) * GAP[device] - 2 * PAD


def bucket(span: Span) -> Bucket:
    if span.device == "phone":
        lo, hi = PHONE_W[span.cols]
    else:
        g = GAP["desktop"]
        lo, hi = (span.cols * c + (span.cols - 1) * g - 2 * PAD for c in DESK_COL_OUTER)
    return Bucket(span, lo, hi, content_h(span.device, span.rows))


ALL_SPANS: list[Span] = [Span("desktop", c, r) for c in range(1, 5) for r in range(1, 4)] + \
                        [Span("phone", c, r) for c in (1, 2) for r in range(1, 4)]
ALL_SCLASSES: list[str] = [f"{w}R{r}" for w in WCLASS_ORDER for r in (1, 2, 3)]


def phone_default(desktop: Span, valid: set[Span]) -> Span | None:
    """Map a desktop span to the phone span shown by default. Rule (frozen):
    desktop 1 col -> phone half, 2+ cols -> phone full, same rows; if invalid try
    full same rows, then half same rows, then full rows+1, then any valid phone span
    with the fewest rows. None only if the candidate has no valid phone span."""
    pref = [Span("phone", 1 if desktop.cols == 1 else 2, desktop.rows),
            Span("phone", 2, desktop.rows), Span("phone", 1, desktop.rows)]
    if desktop.rows < 3:
        pref.append(Span("phone", 2, desktop.rows + 1))
    for s in pref:
        if s in valid:
            return s
    rest = sorted((s for s in valid if s.device == "phone"), key=lambda s: (s.rows, -s.cols))
    return rest[0] if rest else None


if __name__ == "__main__":
    for s in ALL_SPANS:
        b = bucket(s)
        print(s.key, s.sclass, b.min_w, b.max_w, b.h)
