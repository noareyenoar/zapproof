"""Render the Zapproof mark from the construction spec, with no image dependency.

The mark is defined in `distribution/brand/IMAGE_PROMPTS.md`: a payment-path line that
stops at a vertical bar, with one accent dot just before the bar. This script draws it
with a tiny supersampling rasterizer and writes PNGs with a built-in encoder, so it
needs nothing beyond the standard library.

Run:
    uv run python scripts/render_brand.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path("distribution/brand")
INK = (0x0B, 0x12, 0x20)
PAPER = (0xF7, 0xF5, 0xF0)
ACCENT = (0x2F, 0x6B, 0xFF)
DARK_ACCENT = (0x5B, 0x8C, 0xFF)

# Construction spec at 400 x 400.
LINE = ((96.0, 200.0), (268.0, 200.0), 5.0)
BAR = ((268.0, 148.0), (268.0, 252.0), 6.0)
DOT = ((224.0, 200.0), 9.0)


def _segment_distance(
    px: float, py: float, a: tuple[float, float], b: tuple[float, float]
) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _inside_line(x: float, y: float) -> bool:
    return _segment_distance(x, y, LINE[0], LINE[1]) <= LINE[2]


def _inside_bar(x: float, y: float) -> bool:
    return _segment_distance(x, y, BAR[0], BAR[1]) <= BAR[2]


def _inside_dot(x: float, y: float) -> bool:
    cx, cy = DOT[0]
    return math.hypot(x - cx, y - cy) <= DOT[1]


def render_mark(size: int, ink: tuple[int, int, int], accent: tuple[int, int, int],
                background: tuple[int, int, int] | None = None) -> bytes:
    """Return raw RGBA rows for `size` x `size`, anti-aliased by 4x4 supersampling."""
    scale = size / 400.0
    samples = 4
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0
        for x in range(size):
            r = g = b = a = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = (x + (sx + 0.5) / samples) / scale
                    py = (y + (sy + 0.5) / samples) / scale
                    if _inside_dot(px, py):
                        cr, cg, cb, ca = accent[0], accent[1], accent[2], 255
                    elif _inside_bar(px, py) or _inside_line(px, py):
                        cr, cg, cb, ca = ink[0], ink[1], ink[2], 255
                    elif background is not None:
                        cr, cg, cb, ca = background[0], background[1], background[2], 255
                    else:
                        cr = cg = cb = ca = 0
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            n = samples * samples
            if a:
                rows.extend((r // a, g // a, b // a, a // n))
            else:
                rows.extend((0, 0, 0, 0))
    return bytes(rows)


def render_simplified(size: int, ink: tuple[int, int, int]) -> bytes:
    """The 32/16 px favicon: bar and dot only, no horizontal line."""
    samples = 8
    bar_w = 4.0 if size == 32 else 2.0
    bar_h = 18.0 if size == 32 else 9.0
    bar_x = size / 2 + 3 if size == 32 else size / 2 + 1
    dot_r = 2.5 if size == 32 else 1.25
    dot_x = bar_x - 8 if size == 32 else bar_x - 4
    bar_top = (size - bar_h) / 2
    bar_bottom = bar_top + bar_h
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            hit = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = x + (sx + 0.5) / samples
                    py = y + (sy + 0.5) / samples
                    in_bar = abs(px - bar_x) <= bar_w / 2 and bar_top <= py <= bar_bottom
                    in_dot = math.hypot(px - dot_x, py - (bar_top + bar_h / 2)) <= dot_r
                    if in_bar or in_dot:
                        hit += 1
            alpha = round(255 * hit / (samples * samples))
            rows.extend((ink[0], ink[1], ink[2], alpha))
    return bytes(rows)


def write_png(path: Path, size: int, rgba_rows: bytes) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rgba_rows, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">
  <title>Zapproof</title>
  <desc>A payment path stops at a checkpoint bar; one dot is checked before it.</desc>
  <g fill="none" stroke="#0B1220" stroke-linecap="round">
    <path d="M96 200H268" stroke-width="10"/>
    <path d="M268 148V252" stroke-width="12"/>
  </g>
  <circle cx="224" cy="200" r="9" fill="#2F6BFF"/>
</svg>
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "zapproof-mark-400.svg").write_text(SVG, encoding="utf-8")
    write_png(OUT / "zapproof-mark-400.png", 400, render_mark(400, INK, ACCENT))
    write_png(OUT / "zapproof-mark-dark-400.png", 400, render_mark(400, PAPER, DARK_ACCENT))
    write_png(
        OUT / "zapproof-apple-touch-180.png",
        180,
        render_mark(180, INK, ACCENT, background=PAPER),
    )
    write_png(OUT / "zapproof-favicon-32.png", 32, render_simplified(32, INK))
    write_png(OUT / "zapproof-favicon-16.png", 16, render_simplified(16, INK))
    for name in (
        "zapproof-mark-400.svg",
        "zapproof-mark-400.png",
        "zapproof-mark-dark-400.png",
        "zapproof-apple-touch-180.png",
        "zapproof-favicon-32.png",
        "zapproof-favicon-16.png",
    ):
        path = OUT / name
        print(f"{path} {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
