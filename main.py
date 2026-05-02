import argparse
import sys

import numpy as np

from tomodachi_common import (
    COLOR_PANEL_HOME,
    build_color_layers,
    emit_canvas_move,
    emit_color_switch,
    fmt_seconds,
    load_quantized_image,
    order_row_snake,
)
from main_brush import generate_commands as generate_brush_commands


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a macro for Tomodachi Life drawing."
    )
    parser.add_argument("image", help="Path to the 256x256 RGBA image.")
    parser.add_argument(
        "--mode",
        choices=("pixel", "brush"),
        default="pixel",
        help="Drawing mode. Pixel paints 1x1 pixels; brush uses multi-size coverage. Default: pixel.",
    )
    parser.add_argument(
        "--press",
        type=float,
        default=0.075,
        help="Button hold time in seconds. Default: 0.075.",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.075,
        help="Wait time after moves or selector navigation in seconds. Default: 0.075.",
    )
    parser.add_argument(
        "--min-gain",
        type=int,
        default=1,
        help="Minimum newly covered pixels for brush mode. Default: 1.",
    )
    parser.add_argument(
        "--merge-threshold",
        type=float,
        default=0,
        help="Merge quantized colors within this RGB distance before planning. Default: 0.",
    )
    parser.add_argument(
        "--return-home-per-layer",
        action="store_true",
        help="Return canvas cursor to (0,0) after finishing each color layer.",
    )
    return parser.parse_args()


def generate_pixel_commands(image, press, wait, merge_threshold=0, return_home_per_layer=False):
    keys, opaque = load_quantized_image(image, merge_threshold=merge_threshold)
    layers = build_color_layers(keys, opaque)
    press_text = fmt_seconds(press)
    wait_text = fmt_seconds(wait)

    commands = []
    current_color = COLOR_PANEL_HOME
    current_pos = (0, 0)

    for layer in layers:
        if not np.any(layer.mask):
            continue

        current_color = emit_color_switch(
            commands,
            current_color,
            layer.key,
            press_text,
            wait_text,
        )

        points = order_row_snake([tuple(map(int, point)) for point in np.argwhere(layer.mask)])
        for point in points:
            current_pos = emit_canvas_move(
                commands,
                current_pos,
                point,
                press_text,
                wait_text,
            )
            commands.append(f"A {press_text}")

        if return_home_per_layer:
            current_pos = emit_canvas_move(
                commands,
                current_pos,
                (0, 0),
                press_text,
                wait_text,
            )
            current_color = emit_color_switch(
                commands,
                current_color,
                (current_color[0], current_color[1], 0),
                press_text,
                wait_text,
            )

    return commands, len(layers)


def main():
    args = parse_args()
    if args.mode == "brush":
        commands, color_count = generate_brush_commands(
            args.image,
            args.press,
            args.wait,
            args.min_gain,
            args.merge_threshold,
            args.return_home_per_layer,
        )
        tag = "main_brush"
    else:
        commands, color_count = generate_pixel_commands(
            args.image,
            args.press,
            args.wait,
            args.merge_threshold,
            args.return_home_per_layer,
        )
        tag = "main_pixel"

    print(
        f"[{tag}] colors={color_count} macro_lines={len(commands)}",
        file=sys.stderr,
    )
    print("\n".join(commands))


if __name__ == "__main__":
    main()
