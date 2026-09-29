#!/usr/bin/env python3
"""Regenerate the themed Videomancer wizard images embedded in main.py.

The purple original (_SPLASH_IMG_B64) is the source. Each pixel is treated as
a mix of black, white and one art colour (the robe blue or the hands/brim
magenta); the mix is kept and the colours swapped, so anti-aliased edges stay
smooth. The neon version also gets a green sticker outline and a soft outer
glow. Results are written back into main.py as _SPLASH_<NAME>_IMG_B64.

    python3 scripts/theme_character.py        (needs Pillow and numpy)
"""
import base64
import io
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

MAIN = Path(__file__).resolve().parent.parent / "main.py"

# Colours of the original art
ROBE = np.array([0x48, 0x2c, 0xc8], float)     # blue-violet robe and hat
ACCENT = np.array([0x98, 0x14, 0xb8], float)   # magenta hands, brim, belt, shoes
H_ROBE, H_ACC = 250.0, 288.0                   # their hues (degrees)

ART = {
    "AMBER": dict(robe="#e0852a", accent="#ffc861", white="#fff1d6"),
    "NEON": dict(robe="#0f6a36", accent="#39ff88", white="#e8fff1",
                 ring="#39ff88", glow="#16ff66"),
}
OUT_WIDTH = 544        # largest use in the app is 340 px wide
JPEG_QUALITY = 90
GLOW_MARGIN = 28       # black border added so the glow isn't cut off where the art meets the frame


def hexrgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], float)


def hue(p):
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    mx, mn = p.max(-1), p.min(-1)
    c = np.maximum(mx - mn, 1e-6)
    return np.where(mx == r, ((g - b) / c) % 6,
                    np.where(mx == g, (b - r) / c + 2, (r - g) / c + 4)) * 60


def flood(mask, seed):
    """Pixels of `mask` connected to `seed`."""
    m = Image.fromarray((mask * 255).astype(np.uint8)).copy()
    ImageDraw.floodfill(m, seed, 128)
    return np.asarray(m) == 128


def ring_mask(p):
    """The white sticker outline: the white region met first from the left."""
    white = p.min(-1) > 140
    y = white.shape[0] // 2
    ring = flood(white, (int(np.nonzero(white[y])[0][0]), y))
    grown = Image.fromarray((ring * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
    return np.asarray(grown) > 0


def recolor(p, robe, accent, white, ring=None, glow=None, glow_px=10):
    chroma = p.max(-1) - p.min(-1)
    k = np.clip((hue(p) - H_ROBE) / (H_ACC - H_ROBE), 0, 1)[..., None]   # 0 robe … 1 accent
    src = ROBE * (1 - k) + ACCENT * k
    t = np.clip(chroma / (src.max(-1) - src.min(-1)), 0, 1)[..., None]    # art-colour amount
    u = np.clip((p.min(-1)[..., None] - t * src.min(-1)[..., None]) / 255.0, 0, 1 - t)  # white
    whites = np.broadcast_to(hexrgb(white), p.shape).copy()
    if ring:
        whites[ring_mask(p)] = hexrgb(ring)
    new = hexrgb(robe) * (1 - k) + hexrgb(accent) * k
    out = t * new + u * whites                                            # + black × rest
    if glow:
        # background = dark pixels connected to the frame (padding joins all four corners)
        outside = flood(np.pad(p.max(-1) <= 60, 1, constant_values=True), (0, 0))[1:-1, 1:-1]
        halo = Image.fromarray((~outside * 255).astype(np.uint8))
        halo = halo.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(glow_px))
        a = (np.asarray(halo).astype(float) / 255.0)[..., None] * 0.85
        out = np.where(outside[..., None], out * (1 - a) + hexrgb(glow) * a, out)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


def main():
    text = MAIN.read_bytes().decode("utf-8")
    src_b64 = re.search(r'^_SPLASH_IMG_B64 = """(.*?)"""', text, re.S | re.M).group(1)
    p = np.asarray(Image.open(io.BytesIO(base64.b64decode(src_b64))).convert("RGB")).astype(float)
    for name, colours in ART.items():
        src = p
        if colours.get("glow"):
            m = GLOW_MARGIN
            src = np.pad(p, ((m, m), (m, m), (0, 0)))
        img = recolor(src, **colours)
        img = img.resize((OUT_WIDTH, round(img.height * OUT_WIDTH / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=JPEG_QUALITY)
        block = f'_SPLASH_{name}_IMG_B64 = """{base64.b64encode(buf.getvalue()).decode()}"""'
        pat = re.compile(rf'^_SPLASH_{name}_IMG_B64 = """.*?"""', re.S | re.M)
        if pat.search(text):
            text = pat.sub(lambda _m: block, text)
        else:                                   # first run: add after the original
            m = re.search(r'^_SPLASH_IMG_B64 = """.*?"""\n', text, re.S | re.M)
            text = text[:m.end()] + block + "\n" + text[m.end():]
        print(f"{name.lower():6} {len(buf.getvalue()) / 1024:.0f} KB")
    MAIN.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
