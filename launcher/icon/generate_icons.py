#!/usr/bin/env python3
"""Generate the launcher's icons — stdlib only, no image libraries.

Outputs:
  icon_mac.png  — menu-bar tray glyph, black on transparent (a rounded-square RING, used as a macOS
                  *template* icon so the bar tints it for light/dark). A ring, not a filled box, so it
                  reads as an icon rather than a black blob.
  icon_win.ico  — the same ring in mid-blue for the Windows system tray (visible on light + dark bars).
  icon_app.png  — 512px COLOURED app icon (a blue rounded tile with a white ring). CI turns this into
                  SmartBrain.icns so the .app shows a real icon in Finder instead of a blank sheet.

Run from this directory:  python3 generate_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SS = 4  # supersampling, box-averaged down for smooth edges


def _rounded(x: float, y: float, w: float, lo_f: float, hi_f: float, r_f: float) -> bool:
    """Is (x, y) inside a rounded square inscribed in a w×w box (bounds/radius as fractions of w)?"""
    lo, hi, radius = w * lo_f, w * hi_f, w * r_f
    if x < lo or x > hi or y < lo or y > hi:
        return False
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= radius * radius


def _disc(x: float, y: float, w: float, r_f: float) -> bool:
    dx, dy = x - w / 2, y - w / 2
    return dx * dx + dy * dy <= (w * r_f) ** 2


def _in_ring(x: float, y: float, w: float) -> bool:
    """A rounded-square outline: inside the outer rounded square, outside a smaller inner one."""
    return _rounded(x, y, w, 0.08, 0.92, 0.30) and not _rounded(x, y, w, 0.28, 0.72, 0.22)


def _app_rgb(x: float, y: float, w: float):
    """Colour for the app tile: blue rounded tile with a white ring; None = transparent."""
    if not _rounded(x, y, w, 0.05, 0.95, 0.23):
        return None
    if _disc(x, y, w, 0.30) and not _disc(x, y, w, 0.19):
        return (255, 255, 255)
    return (74, 144, 217)


def render(size: int, kind: str, color=(0, 0, 0)) -> bytes:
    """RGBA bytes. kind='ring' → a ring in `color`; kind='app' → the colour app tile."""
    w = size * SS
    buf = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            r = g = b = 0
            hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    px, py = ox * SS + sx + 0.5, oy * SS + sy + 0.5
                    if kind == "app":
                        c = _app_rgb(px, py, w)
                        if c is not None:
                            r, g, b = c
                            hits += 1
                    elif _in_ring(px, py, w):
                        r, g, b = color
                        hits += 1
            i = (oy * size + ox) * 4
            buf[i], buf[i + 1], buf[i + 2] = r, g, b
            buf[i + 3] = 255 * hits // (SS * SS)
    return bytes(buf)


def _chunk(typ: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def png_bytes(size: int, rgba: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 per scanline
        raw += rgba[y * size * 4 : (y + 1) * size * 4]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def ico_bytes(png: bytes, size: int) -> bytes:
    """Wrap a PNG as a single-image .ico (PNG-in-ICO, supported on Windows Vista+)."""
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), 6 + 16)
    return header + entry + png


def main() -> None:
    here = Path(__file__).resolve().parent
    (here / "icon_mac.png").write_bytes(png_bytes(44, render(44, "ring", (0, 0, 0))))
    (here / "icon_win.ico").write_bytes(ico_bytes(png_bytes(32, render(32, "ring", (74, 144, 217))), 32))
    (here / "icon_app.png").write_bytes(png_bytes(512, render(512, "app")))
    print("wrote icon_mac.png, icon_win.ico, icon_app.png")


if __name__ == "__main__":
    main()
