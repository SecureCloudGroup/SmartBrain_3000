#!/usr/bin/env python3
"""Generate the launcher's tray icons — an "SB" monogram.

Outputs (committed next to this script; the launcher embeds them via //go:embed):
  icon_mac.png  — 44x44 menu-bar glyph: "SB" in BLACK on transparent, used as a macOS *template*
                  icon so the menu bar tints it for light/dark automatically.
  icon_win.ico  — 32x32 "SB" in mid-blue for the Windows system tray (reads on light + dark bars).

  icon_app.png is NOT generated here: it is the brand asset (assets/SmartBrain_Avatar.png resized to
  512px) used for the Finder/Dock icon — regenerate with
  `sips -z 512 512 ../../assets/SmartBrain_Avatar.png --out icon_app.png`. CI turns it into .icns.

Needs Pillow + a bold sans TTF (tries DejaVu Sans Bold / Arial Bold / Helvetica). Run from this
directory:  python3 generate_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]
_SS = 4  # supersample, then average down for smooth edges


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


def render(size: int, color: tuple[int, int, int], frac: float) -> Image.Image:
    fp = _font_path()
    box = size * _SS
    img = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _fit(fp, box, "SB", frac)
    b = d.textbbox((0, 0), "SB", font=f)
    d.text(((box - (b[2] - b[0])) // 2 - b[0], (box - (b[3] - b[1])) // 2 - b[1]),
           "SB", font=f, fill=color + (255,))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = Path(__file__).parent
    render(44, (0, 0, 0), 0.80).save(here / "icon_mac.png")       # black template (macOS tints it)
    render(32, (74, 144, 217), 0.84).save(here / "icon_win.ico")  # blue for the Windows tray
    print("wrote icon_mac.png (SB, template) + icon_win.ico (SB, blue)")


if __name__ == "__main__":
    main()
