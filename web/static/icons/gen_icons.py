#!/usr/bin/env python3
"""Generate Clip Engine PWA icons — run from web/static/icons/ directory."""
import math
import os
from PIL import Image, ImageDraw

BG_DARK   = (15,  15,  15,  255)
BG_LIGHT  = (0,   229, 255, 255)
ACCENT    = (0,   229, 255, 255)
DARK      = (15,  15,  15,  255)

def draw_icon(size: int, bg: tuple, symbol_color: tuple) -> Image.Image:
    img = Image.new("RGBA", (size, size), bg)
    d   = ImageDraw.Draw(img)

    cx = cy = size / 2.0

    # --- 'C' ring (arc gap on the right, ±32°) ---
    r_outer = size * 0.36
    r_inner = size * 0.22
    line_w  = max(1, int(r_outer - r_inner))

    # Draw arc as filled ellipse minus inner ellipse, masked by an angular wedge
    # Use polygon of arc points for the ring shape
    steps = 360
    GAP_START, GAP_END = -32, 32   # degrees — gap on right side

    def pt(angle_deg, r):
        rad = math.radians(angle_deg)
        return (cx + r * math.cos(rad), cy + r * math.sin(rad))

    arc_pts = []
    for a in range(GAP_END, 360 + GAP_START):   # from 32° → 328°
        arc_pts.append(pt(a, r_outer))
    for a in range(360 + GAP_START, GAP_END, -1):
        arc_pts.append(pt(a, r_inner))
    if len(arc_pts) > 2:
        d.polygon(arc_pts, fill=symbol_color)

    # --- central dot ---
    dot_r = size * 0.07
    d.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=symbol_color
    )

    return img


def save(path: str, img: Image.Image):
    rgb = Image.new("RGB", img.size, (0, 0, 0))
    rgb.paste(img, mask=img.split()[3])
    rgb.save(path, "PNG", optimize=True)
    print(f"  {path}")


os.makedirs(".", exist_ok=True)

# Standard icons — dark bg, cyan symbol
for size in (192, 512):
    save(f"icon-{size}.png", draw_icon(size, BG_DARK, ACCENT))

# Maskable icons — cyan bg, dark symbol (safe-zone compliant: symbol fits in 80% radius)
for size in (192, 512):
    save(f"icon-maskable-{size}.png", draw_icon(size, BG_LIGHT, DARK))

print("Icons generated.")
