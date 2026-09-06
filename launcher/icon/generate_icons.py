#!/usr/bin/env python3
"""Generate the launcher's tray icons — a brain silhouette with "SB" knocked out.

The glyph is a solid side-view brain with the letters "SB" cut out of its center
(transparent), so the letters read as the bar/panel color behind the icon: black
brain + white letters on a light macOS menu bar, auto-inverted in dark mode.

Outputs (committed next to this script; the launcher embeds them via //go:embed):
  icon_mac.png   — 44x44 menu-bar glyph: brain in BLACK on transparent, used as a macOS
                   *template* icon so the menu bar tints it for light/dark automatically.
  icon_win.ico   — 32x32 brain in mid-blue for the Windows system tray (no template tinting
                   there — pure black would vanish on a dark taskbar).
  icon_linux.png — 44x44 brain in the same mid-blue for the Linux tray (SNI carries raw PNG).

  icon_app.png is NOT generated here: it is the Finder/Dock mark derived from the brand
  asset by tools/brand/make_icons.py (run that from the repo root). CI turns it into .icns.

Needs Pillow + a bold sans TTF (tries DejaVu Sans Bold / Arial Bold / Helvetica). Run from this
directory:  python3 generate_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]
_SS = 4  # supersample, then average down for smooth edges

# The brain silhouette as a union of discs + one ellipse (unit-box coordinates,
# x right / y down): a lobed crown, a fuller back, a small cerebellum tuck and a
# stem at the lower rear. Solid fill — cortical detail would be mush at 22 px.
_BRAIN_CORE = (0.14, 0.18, 0.86, 0.70)  # main mass ellipse (l, t, r, b)
_BRAIN_BUMPS = (  # (cx, cy, r) discs unioned onto the core
    (0.26, 0.28, 0.13),  # frontal lobe crown
    (0.42, 0.21, 0.14),
    (0.60, 0.21, 0.14),
    (0.76, 0.30, 0.12),  # rear crown
    (0.18, 0.42, 0.11),  # brow / front
    (0.84, 0.44, 0.10),  # occipital back
    (0.78, 0.60, 0.10),  # cerebellum
    (0.24, 0.58, 0.10),  # temporal front-bottom
)
_BRAIN_STEM = (0.60, 0.64, 0.72, 0.84)  # ellipse for the stem, lower rear


def _font_path() -> str:
    for p in _FONTS:
        if Path(p).exists():
            return p
    raise SystemExit("no bold TTF found — add one to _FONTS for your system")


def _fit(font_path: str, box: int, text: str, frac: float) -> ImageFont.FreeTypeFont:
    """Largest font size whose text fits within frac*box in both dimensions."""
    s = 8
    while True:
        f = ImageFont.truetype(font_path, s)
        b = ImageDraw.Draw(Image.new("RGBA", (4, 4))).textbbox((0, 0), text, font=f)
        if (b[2] - b[0]) > box * frac or (b[3] - b[1]) > box * frac:
            return ImageFont.truetype(font_path, max(8, s - 1))
        s += 1


def _brain_mask(box: int) -> Image.Image:
    """8-bit mask of the brain silhouette in a box*box tile."""
    mask = Image.new("L", (box, box), 0)
    d = ImageDraw.Draw(mask)
    l, t, r, b = (v * box for v in _BRAIN_CORE)
    d.ellipse((l, t, r, b), fill=255)
    for cx, cy, rad in _BRAIN_BUMPS:
        d.ellipse(((cx - rad) * box, (cy - rad) * box, (cx + rad) * box, (cy + rad) * box), fill=255)
    l, t, r, b = (v * box for v in _BRAIN_STEM)
    d.ellipse((l, t, r, b), fill=255)
    return mask


def _text_mask(box: int, text: str, frac: float, dy: float) -> Image.Image:
    """8-bit mask of centered text (dy nudges the center as a fraction of box)."""
    mask = Image.new("L", (box, box), 0)
    d = ImageDraw.Draw(mask)
    f = _fit(_font_path(), box, text, frac)
    b = d.textbbox((0, 0), text, font=f)
    d.text(((box - (b[2] - b[0])) // 2 - b[0],
            (box - (b[3] - b[1])) // 2 - b[1] + round(dy * box)),
           text, font=f, fill=255)
    return mask


def render(size: int, color: tuple[int, int, int], frac: float) -> Image.Image:
    """Brain silhouette in `color` with "SB" knocked out (transparent letters)."""
    box = size * _SS
    # Letters sit in the visual center of the mass (the stem pulls the geometric
    # center down-right), hence the slight upward nudge.
    alpha = ImageChops.subtract(_brain_mask(box), _text_mask(box, "SB", frac, -0.045))
    img = Image.new("RGBA", (box, box), color + (255,))
    img.putalpha(alpha)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = Path(__file__).parent
    render(44, (0, 0, 0), 0.52).save(here / "icon_mac.png")         # black template (macOS tints it)
    render(32, (74, 144, 217), 0.56).save(here / "icon_win.ico")    # blue for the Windows tray
    render(44, (74, 144, 217), 0.52).save(here / "icon_linux.png")  # same blue for the Linux tray
    print("wrote icon_mac.png (brain/SB, template) + icon_win.ico + icon_linux.png (brain/SB, blue)")


if __name__ == "__main__":
    main()
