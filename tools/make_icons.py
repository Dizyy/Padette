"""Draws every Padette icon from one design (a gamepad with mouse ears: a stick on the left,
four buttons on the right), so they can be remade any time:

    .venv\\Scripts\\python.exe tools\\make_icons.py

Writes client/icons/icon-{16,32,180,192,512}.png (square, full-bleed: phones round the
corners themselves), docs/logo.png (rounded corners, transparent outside) and docs/logo.svg.
16 and 32 px use a simpler drawing (no ear centres, round blobs) that still reads when tiny.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BG, SAGE, INK = (26, 25, 22, 255), (156, 197, 161, 255), (22, 36, 26, 255)
INK_SOFT = (22, 36, 26, 90)
SS = 4  # drawn this many times bigger, then scaled down: smooth edges

# The design, on a 120 x 120 grid: (kind, *values). Matches the approved mockup (2026-11-07).
DETAILED = [
    ("circle", 36, 38, 13, SAGE), ("circle", 84, 38, 13, SAGE),          # ears
    ("circle", 36, 38, 6, INK_SOFT), ("circle", 84, 38, 6, INK_SOFT),    # inside the ears
    ("rrect", 12, 50, 108, 90, 18, SAGE),                                 # body
    ("circle", 36, 70, 11, INK_SOFT), ("circle", 38, 68, 7.5, INK),       # stick: well + knob
    ("circle", 84, 62, 4, INK), ("circle", 84, 78, 4, INK),               # A B X Y
    ("circle", 76, 70, 4, INK), ("circle", 92, 70, 4, INK),
]
SIMPLE = [
    ("circle", 36, 38, 14, SAGE), ("circle", 84, 38, 14, SAGE),
    ("rrect", 12, 50, 108, 90, 18, SAGE),
    ("circle", 37, 69, 10, INK), ("circle", 84, 70, 10, INK),
]


def draw(size: int, shapes, scale: float = 1.0, rounded: bool = False) -> Image.Image:
    """`scale` < 1 shrinks the drawing towards the centre (room for round phone masks)."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    bg = ImageDraw.Draw(img)
    if rounded:
        bg.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 27 / 120, fill=BG)
    else:
        bg.rectangle((0, 0, big, big), fill=BG)
    unit = big / 120

    def p(v: float, axis_center: float = 60) -> float:
        return (axis_center + (v - axis_center) * scale) * unit

    for shape in shapes:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if shape[0] == "circle":
            _, cx, cy, r, color = shape
            r = r * scale * unit
            d.ellipse((p(cx) - r, p(cy) - r, p(cx) + r, p(cy) + r), fill=color)
        else:
            _, x0, y0, x1, y1, r, color = shape
            d.rounded_rectangle((p(x0), p(y0), p(x1), p(y1)), radius=r * scale * unit, fill=color)
        img = Image.alpha_composite(img, layer)
    return img.resize((size, size), Image.LANCZOS)


def svg() -> str:
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">',
             '  <rect width="120" height="120" rx="27" fill="#1a1916"/>']
    for shape in DETAILED:
        color = shape[-1]
        fill = "#%02x%02x%02x" % color[:3]
        opacity = f' opacity="{color[3] / 255:.2f}"' if color[3] < 255 else ""
        if shape[0] == "circle":
            _, cx, cy, r, _ = shape
            parts.append(f'  <circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"{opacity}/>')
        else:
            _, x0, y0, x1, y1, r, _ = shape
            parts.append(f'  <rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="{fill}"{opacity}/>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    icons = ROOT / "client" / "icons"
    for size in (16, 32):
        draw(size, SIMPLE).convert("RGB").save(icons / f"icon-{size}.png")
    for size in (180, 192, 512):
        # 0.86: keeps the ears inside the circle Android may cut the icon to ("maskable").
        draw(size, DETAILED, scale=0.86).convert("RGB").save(icons / f"icon-{size}.png")
    draw(512, DETAILED, rounded=True).save(ROOT / "docs" / "logo.png")
    (ROOT / "docs" / "logo.svg").write_text(svg(), encoding="utf-8")
    print("icons written")


if __name__ == "__main__":
    main()
