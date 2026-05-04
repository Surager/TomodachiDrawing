import argparse
import sys

from .tomodachi_common import (
    build_color_layers,
    dump_point_layers,
    load_quantized_image,
    save_quantized_preview,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Preview a Tomodachi Life drawing image as quantized pixels."
    )
    parser.add_argument("image", help="Path to the source image.")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output preview image path, for example preview.png.",
    )
    parser.add_argument(
        "--dump",
        help="Optional JSON file for per-color pixel point lists.",
    )
    parser.add_argument(
        "--merge-threshold",
        type=float,
        default=0,
        help="Merge quantized colors within this RGB distance before previewing. Default: 0.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    keys, opaque = load_quantized_image(args.image, merge_threshold=args.merge_threshold)
    layers = build_color_layers(keys, opaque)
    save_quantized_preview(keys, opaque, args.output)

    if args.dump:
        dump_point_layers(layers, args.dump)

    print(
        f"[preview] saved={args.output} colors={len(layers)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
