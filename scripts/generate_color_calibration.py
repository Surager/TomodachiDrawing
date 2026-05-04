import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw


CANVAS_SIZE = 256


SWATCHES = [
    {"name": "red", "rgb": (255, 64, 64)},
    {"name": "orange", "rgb": (255, 160, 64)},
    {"name": "yellow", "rgb": (255, 240, 64)},
    {"name": "yellow_green", "rgb": (176, 255, 64)},
    {"name": "green", "rgb": (64, 224, 96)},
    {"name": "cyan", "rgb": (64, 240, 224)},
    {"name": "blue", "rgb": (64, 128, 255)},
    {"name": "indigo", "rgb": (96, 64, 255)},
    {"name": "purple", "rgb": (176, 64, 255)},
    {"name": "magenta", "rgb": (255, 64, 224)},
    {"name": "pink", "rgb": (255, 96, 160)},
    {"name": "gray", "rgb": (160, 160, 160)},
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a strict 256x256 calibration image with square color swatches."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="color_calibration.png",
        help="Output PNG path. Default: color_calibration.png.",
    )
    parser.add_argument(
        "--meta",
        help="Optional JSON metadata path describing swatch positions and colors.",
    )
    return parser.parse_args()


def build_layout(count):
    cols = 4
    rows = (count + cols - 1) // cols
    swatch_size = 40
    gap_x = 12
    gap_y = 12

    total_w = cols * swatch_size + (cols - 1) * gap_x
    total_h = rows * swatch_size + (rows - 1) * gap_y
    left = (CANVAS_SIZE - total_w) // 2
    top = (CANVAS_SIZE - total_h) // 2
    return cols, rows, swatch_size, gap_x, gap_y, left, top


def main():
    args = parse_args()

    image = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    cols, rows, swatch_size, gap_x, gap_y, left, top = build_layout(len(SWATCHES))
    meta = {
        "canvas_size": [CANVAS_SIZE, CANVAS_SIZE],
        "swatch_size": swatch_size,
        "gap": [gap_x, gap_y],
        "items": [],
    }

    for index, swatch in enumerate(SWATCHES):
        row = index // cols
        col = index % cols
        x0 = left + col * (swatch_size + gap_x)
        y0 = top + row * (swatch_size + gap_y)
        x1 = x0 + swatch_size - 1
        y1 = y0 + swatch_size - 1

        draw.rectangle((x0, y0, x1, y1), fill=swatch["rgb"])
        meta["items"].append(
            {
                "index": index,
                "name": swatch["name"],
                "rgb": list(swatch["rgb"]),
                "bbox": [x0, y0, x1, y1],
            }
        )

    output_path = Path(args.output)
    image.save(output_path)

    if args.meta:
        Path(args.meta).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"saved {output_path} ({CANVAS_SIZE}x{CANVAS_SIZE})")


if __name__ == "__main__":
    main()
