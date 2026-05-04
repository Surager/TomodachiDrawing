#!/usr/bin/env python3
"""
在 256×256 画布上，用若干纯黑色 2×3（宽×高，单位：像素）长方形平铺，用于稳定性测试。

默认透明背景；矩形之间留出固定缝隙（水平、垂直相同）；不足一整格的边缘留白。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 256
RECT_W = 2
RECT_H = 3


def parse_args():
    repo_root = Path(__file__).resolve().parents[1]
    default_out = repo_root / "output" / "stability" / "black_rect_grid_256.png"
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_out,
        help=f"输出 PNG 路径（默认: {default_out})",
    )
    p.add_argument(
        "--gap",
        type=int,
        default=1,
        metavar="N",
        help="相邻黑块之间的间隙（像素），水平与垂直相同；默认 1",
    )
    p.add_argument(
        "--opaque-bg",
        type=int,
        nargs=3,
        metavar=("R", "G", "B"),
        default=None,
        help="若指定，则使用不透明 RGB 背景（默认不填：全透明）",
    )
    return p.parse_args()


def main():
    args = parse_args()
    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)

    gap = max(0, args.gap)
    step_x = RECT_W + gap
    step_y = RECT_H + gap

    if args.opaque_bg is not None:
        r, g, b = args.opaque_bg
        img = Image.new("RGBA", (CANVAS, CANVAS), (r, g, b, 255))
    else:
        img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    count = 0
    y = 0
    while y + RECT_H <= CANVAS:
        x = 0
        while x + RECT_W <= CANVAS:
            draw.rectangle(
                [x, y, x + RECT_W - 1, y + RECT_H - 1],
                fill=(0, 0, 0, 255),
            )
            count += 1
            x += step_x
        y += step_y

    img.save(out, format="PNG")
    print(
        f"Wrote {out} ({CANVAS}×{CANVAS}, {RECT_W}×{RECT_H} rects, gap={gap}, count={count})"
    )


if __name__ == "__main__":
    main()
