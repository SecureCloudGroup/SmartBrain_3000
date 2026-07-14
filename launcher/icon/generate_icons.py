#!/usr/bin/env python3
"""Generate the launcher's tray icons — stdlib only, no image libraries.

Two outputs, both a rounded square (a neutral placeholder mark; swap in a designed asset later):
  icon_mac.png — black on transparent, used as a macOS *template* icon so the menu bar tints it
                 for light/dark automatically.
  icon_win.ico — a mid-blue fill visible on both light and dark Windows taskbars.

Run from this directory:  python3 generate_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SS = 4  # supersampling factor, box-averaged down for smooth edges


def _inside_rounded(x: float, y: float, w: float) -> bool:
    """Is (x, y) inside a rounded square inscribed in a w×w box?"""
    lo, hi = w * 0.10, w * 0.90
    radius = w * 0.26
    if x < lo or x > hi or y < lo or y > hi:
        return False
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= radius * radius


def render(size: int, color: tuple[int, int, int] | None) -> bytes:
    """RGBA bytes for a size×size rounded square. color=None means black (for a template icon)."""
    w = size * SS
    r, g, b = color or (0, 0, 0)
    buf = bytearray(size * size * 4)
    for oy in range(size):
        for ox in range(size):
            hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    if _inside_rounded(ox * SS + sx + 0.5, oy * SS + sy + 0.5, w):
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
        raw.append(0)  # filter type 0 (none) per scanline
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
    (here / "icon_mac.png").write_bytes(png_bytes(44, render(44, None)))
    win_png = png_bytes(32, render(32, (74, 144, 217)))
    (here / "icon_win.ico").write_bytes(ico_bytes(win_png, 32))
    print("wrote icon_mac.png and icon_win.ico")


if __name__ == "__main__":
    main()
