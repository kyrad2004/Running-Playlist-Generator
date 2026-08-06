#!/usr/bin/env python3
"""Generate the app icon used when registering the Strava and Spotify apps.

Design: seven rounded bars whose heights trace a footfall rhythm — an audio
waveform and a running cadence at once. Rendered at 4x and downsampled, which
antialiases the curves better than drawing at final size.

    python assets/make_icon.py

Writes app_icon.png (512px) and app_icon_124.png (Strava's 124px minimum).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent
SIZE = 512
SUPERSAMPLE = 4

# Deliberately not Strava orange — this is our app, and borrowing their brand
# colour would imply an affiliation that doesn't exist.
BG_TOP = (18, 24, 38)
BG_BOTTOM = (28, 38, 58)
BAR_TOP = (255, 138, 92)
BAR_BOTTOM = (255, 92, 122)
ACCENT = (94, 234, 212)

# Alternating tall/short reads as footfalls rather than a generic hump.
BAR_HEIGHTS = (0.42, 0.86, 0.54, 1.00, 0.60, 0.90, 0.40)


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """A 1px-wide gradient stretched to a square — cheaper than per-pixel work."""
    strip = Image.new("RGB", (1, size))
    pixels = strip.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        pixels[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return strip.resize((size, size), Image.NEAREST)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    return mask


def build(size: int) -> Image.Image:
    s = size * SUPERSAMPLE
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Background: gradient clipped to a squircle-ish rounded square.
    background = vertical_gradient(s, BG_TOP, BG_BOTTOM).convert("RGBA")
    canvas.paste(background, (0, 0), rounded_mask(s, int(s * 0.22)))

    # Bars, centred as a group.
    count = len(BAR_HEIGHTS)
    bar_w = s * 0.078
    gap = s * 0.038
    total_w = count * bar_w + (count - 1) * gap
    x0 = (s - total_w) / 2
    max_h = s * 0.50
    mid_y = s * 0.52

    bar_mask = Image.new("L", (s, s), 0)
    draw = ImageDraw.Draw(bar_mask)
    for i, height_ratio in enumerate(BAR_HEIGHTS):
        height = max_h * height_ratio
        left = x0 + i * (bar_w + gap)
        draw.rounded_rectangle(
            (left, mid_y - height / 2, left + bar_w, mid_y + height / 2),
            radius=bar_w / 2,
            fill=255,
        )

    bars = vertical_gradient(s, BAR_TOP, BAR_BOTTOM).convert("RGBA")
    canvas.paste(bars, (0, 0), bar_mask)

    # The tallest bar takes the accent colour: the beat the cadence locks to.
    # A floating dot was the first attempt, but it degraded into a stray speck
    # below ~24px. A full bar survives every size Strava and Spotify render at.
    tallest = BAR_HEIGHTS.index(max(BAR_HEIGHTS))
    height = max_h * BAR_HEIGHTS[tallest]
    left = x0 + tallest * (bar_w + gap)
    accent_mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(accent_mask).rounded_rectangle(
        (left, mid_y - height / 2, left + bar_w, mid_y + height / 2),
        radius=bar_w / 2,
        fill=255,
    )
    canvas.paste(Image.new("RGBA", (s, s), (*ACCENT, 255)), (0, 0), accent_mask)

    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    icon = build(SIZE)
    icon.save(OUT_DIR / "app_icon.png")
    # Strava requires at least 124x124; ship that exact size too.
    icon.resize((124, 124), Image.LANCZOS).save(OUT_DIR / "app_icon_124.png")
    print(f"wrote {OUT_DIR / 'app_icon.png'} (512px)")
    print(f"wrote {OUT_DIR / 'app_icon_124.png'} (124px)")


if __name__ == "__main__":
    main()
