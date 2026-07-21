#!/usr/bin/env python3
"""Derive every raster brand asset from the one mascot source.

The source (assets/SmartBrain_Avatar.png) is the full circle badge: mascot + sparkles +
baked "SmartBrain" wordmark. At small sizes that whole badge is illegible mush, so this
script crops the mascot's FACE (glasses + wink + smile — the identity) and composes it
onto correctly-shaped tiles per platform rule:

  web/static/icons/icon-192.png, icon-512.png      "any" PWA icons — badge-colored circle,
                                                   transparent corners (no white bleed)
  web/static/icons/icon-maskable-192.png, -512.png full-bleed square, face inside the 80%
                                                   maskable safe zone (Android masks freely)
  web/static/icons/apple-touch-icon.png            180px full-bleed square (iOS rounds it)
  web/static/icons/favicon-32.png                  browser-tab mark
  web/static/icons/mark-64.png                     in-app header mark (~30 CSS px @2x)
  launcher/icon/icon_app.png                       512px Dock/Finder icon — circle tile with
                                                   the ~8% transparent margin macOS expects
                                                   (CI turns it into .icns; replaces the old
                                                   raw `sips` resize of the full badge)
  landing/mark-64.png, landing/favicon-32.png      landing-page copies (that dir deploys
                                                   standalone to the VPS)
  landing/og.png                                   1200x630 social card — here the FULL badge
                                                   (wordmark included) is right, it renders large

The tray icons (SB monogram) are NOT touched — see launcher/icon/generate_icons.py.

Needs Pillow. Run from the repo root:  python3 tools/brand/make_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets" / "SmartBrain_Avatar.png"

# Face crop on the 1254x1254 source: the whole mascot — brain, glasses, wink, smile and
# the saluting hand/arm. The sparkle ticks and circuit tails are ERASED (rects below)
# rather than crop-line sliced: both dissolve into noise at icon sizes, and a crop that
# cuts through them leaves amputated fragments at the tile edge.
FACE_BOX = (165, 150, 1030, 800)
ERASE = [
    (165, 150, 395, 320),  # sparkle ticks top-left
    (165, 300, 330, 455),  # lowest sparkle tick, beside the glasses (stops shy of the hand)
    (380, 760, 900, 800),  # circuit-tail stubs under the brain
]
# A pixel that is badge background (inside the circle, clear of art/wordmark) — sampled so
# tiles match the badge exactly and the rectangular face crop pastes in seamlessly.
BG_SAMPLE = (180, 850)
_SS = 4  # supersample factor; compose big, LANCZOS down for smooth circle edges


def _face(src: Image.Image) -> Image.Image:
    from PIL import ImageDraw

    clean = src.copy()
    d = ImageDraw.Draw(clean)
    for box in ERASE:
        d.rectangle(box, fill=_bg(src))
    return clean.crop(FACE_BOX)


def _bg(src: Image.Image) -> tuple[int, int, int, int]:
    r, g, b, *rest = src.convert("RGBA").getpixel(BG_SAMPLE)
    return (r, g, b, 255)


def _paste_face(canvas: Image.Image, face: Image.Image, width: int, dy_frac: float = -0.02) -> None:
    """Center the face at the given pixel width (optically a touch above center)."""
    scale = width / face.width
    f = face.resize((width, max(1, round(face.height * scale))), Image.LANCZOS)
    x = (canvas.width - f.width) // 2
    y = (canvas.height - f.height) // 2 + round(canvas.height * dy_frac)
    canvas.paste(f, (x, y))


def circle_mark(src: Image.Image, size: int, face_frac: float, margin_frac: float = 0.0) -> Image.Image:
    """Badge-colored circle on transparent, face at face_frac of the circle diameter."""
    s = size * _SS
    diam = round(s * (1 - 2 * margin_frac))
    tile = Image.new("RGBA", (diam, diam), _bg(src))
    _paste_face(tile, _face(src), round(diam * face_frac))
    mask = Image.new("L", (diam, diam), 0)
    from PIL import ImageDraw

    ImageDraw.Draw(mask).ellipse((0, 0, diam - 1, diam - 1), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    off = (s - diam) // 2
    out.paste(tile, (off, off), mask)
    return out.resize((size, size), Image.LANCZOS)


def square_tile(src: Image.Image, size: int, face_frac: float) -> Image.Image:
    """Full-bleed badge-colored square (maskable / apple-touch: the platform shapes it)."""
    s = size * _SS
    tile = Image.new("RGBA", (s, s), _bg(src))
    _paste_face(tile, _face(src), round(s * face_frac))
    return tile.resize((size, size), Image.LANCZOS)


def og_card(src: Image.Image, w: int = 1200, h: int = 630) -> Image.Image:
    """Social card: the full badge (wordmark and all) large on the badge background."""
    s = _SS
    card = Image.new("RGBA", (w * s, h * s), _bg(src))
    badge = src.convert("RGBA")
    bh = round(h * s * 0.82)
    badge = badge.resize((bh, bh), Image.LANCZOS)
    # The source has white corners outside its circle — mask to the circle so only the
    # badge lands on the card.
    from PIL import ImageDraw

    mask = Image.new("L", (bh, bh), 0)
    inset = round(bh * 0.012)  # cut the white fringe where the source circle antialiases out
    ImageDraw.Draw(mask).ellipse((inset, inset, bh - 1 - inset, bh - 1 - inset), fill=255)
    card.paste(badge, ((w * s - bh) // 2, (h * s - bh) // 2), mask)
    return card.resize((w, h), Image.LANCZOS)


def main() -> None:
    src = Image.open(SRC).convert("RGBA")
    icons = ROOT / "web" / "static" / "icons"
    landing = ROOT / "landing"
    launcher = ROOT / "launcher" / "icon"

    circle_mark(src, 512, 0.74).save(icons / "icon-512.png")
    circle_mark(src, 192, 0.74).save(icons / "icon-192.png")
    square_tile(src, 512, 0.62).save(icons / "icon-maskable-512.png")
    square_tile(src, 192, 0.62).save(icons / "icon-maskable-192.png")
    square_tile(src, 180, 0.70).save(icons / "apple-touch-icon.png")
    circle_mark(src, 32, 0.88).save(icons / "favicon-32.png")
    circle_mark(src, 64, 0.80).save(icons / "mark-64.png")

    circle_mark(src, 512, 0.74, margin_frac=0.08).save(launcher / "icon_app.png")

    circle_mark(src, 64, 0.80).save(landing / "mark-64.png")
    circle_mark(src, 32, 0.88).save(landing / "favicon-32.png")
    og_card(src).convert("RGB").save(landing / "og.png")

    for p in sorted([*icons.glob("*.png"), landing / "og.png", launcher / "icon_app.png"]):
        print(f"  {p.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
