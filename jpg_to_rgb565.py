"""
PC-side conversion tool -- NOT a board script, run this with the project's .venv on your PC.

Converts an image (JPEG, PNG, ...) into a raw RGB565 file that a board script can load
straight into memory and send to the panel, with no decoding on the board. Used by
seasons_fade.py.

The image is scaled to cover 480x320 (landscape) and center-cropped to fit. It is then
rotated into the panel's native portrait orientation (320x480), using the same mapping as
st77922.ST77922Landscape (physical px = logical y, physical py = 479 - logical x), so the
board can send it in ordinary full-width 40-row bands. Pixels are native little-endian RGB565
(no byte swap: the display bus does that itself).

Uses Pillow:
    .venv\\Scripts\\python.exe -m pip install pillow

Usage:
    .venv\\Scripts\\python.exe jpg_to_rgb565.py pictures\\seasons\\*.jpg [-o out_dir]

Each output file is named after its input, with a .rgb565 extension (307,200 bytes).
"""

import argparse
import glob
import os
import struct

from PIL import Image

LOGICAL_W, LOGICAL_H = 480, 320


def convert(src, dst):
    img = Image.open(src).convert("RGB")
    # Scale to cover the whole screen, then center-crop the overflow.
    scale = max(LOGICAL_W / img.width, LOGICAL_H / img.height)
    w, h = round(img.width * scale), round(img.height * scale)
    img = img.resize((w, h), Image.LANCZOS)
    left, top = (w - LOGICAL_W) // 2, (h - LOGICAL_H) // 2
    img = img.crop((left, top, left + LOGICAL_W, top + LOGICAL_H))

    # ROTATE_90 is counter-clockwise: new(x, y) = old(479 - y, x), which is exactly
    # ST77922Landscape's physical(px, py) = logical(479 - py, px).
    img = img.transpose(Image.ROTATE_90)
    assert img.size == (LOGICAL_H, LOGICAL_W)

    rgb = img.tobytes()
    out = bytearray()
    for i in range(0, len(rgb), 3):
        r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
        out += struct.pack("<H", ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
    with open(dst, "wb") as f:
        f.write(out)
    print("%s -> %s (%d bytes)" % (src, dst, len(out)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("images", nargs="+")
    parser.add_argument("-o", "--out-dir", default=None,
                        help="output directory (default: next to each input)")
    args = parser.parse_args()

    paths = []
    for pattern in args.images:  # Windows shells don't expand wildcards themselves
        paths += glob.glob(pattern) or [pattern]
    for src in paths:
        name = os.path.splitext(os.path.basename(src))[0] + ".rgb565"
        out_dir = args.out_dir or os.path.dirname(src)
        if args.out_dir:
            os.makedirs(out_dir, exist_ok=True)
        convert(src, os.path.join(out_dir, name))


if __name__ == "__main__":
    main()
