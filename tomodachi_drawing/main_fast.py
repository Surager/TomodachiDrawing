import argparse
import sys

import numpy as np

from .tomodachi_common import (
    COLOR_PANEL_HOME,
    build_color_layers,
    build_column_path,
    build_component_path,
    build_row_path,
    emit_canvas_move,
    emit_color_switch,
    fmt_seconds,
    load_quantized_image,
    route_length,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a faster macro for Tomodachi Life drawing."
    )
    parser.add_argument("image", help="Path to the 256x256 RGBA image.")
    parser.add_argument(
        "--strategy",
        choices=("auto", "row", "column", "component"),
        default="auto",
        help="Path planning strategy for each color layer. Default: auto.",
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
    return parser.parse_args()


def choose_path(mask, start, strategy):
    builders = {
        "row": build_row_path,
        "column": build_column_path,
        "component": build_component_path,
    }

    if strategy == "auto":
        candidates = builders.items()
    else:
        candidates = [(strategy, builders[strategy])]

    best_name = None
    best_path = None
    best_score = None

    for name, builder in candidates:
        path = builder(mask)
        score = route_length(start, path)
        if best_score is None or score < best_score:
            best_name = name
            best_path = path
            best_score = score

    return best_name, best_path


def main():
    args = parse_args()
    keys, opaque = load_quantized_image(args.image)
    layers = build_color_layers(keys, opaque)
    press_text = fmt_seconds(args.press)
    wait_text = fmt_seconds(args.wait)

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

        _, path = choose_path(layer.mask, current_pos, args.strategy)
        for point in path:
            current_pos = emit_canvas_move(
                commands,
                current_pos,
                point,
                press_text,
                wait_text,
            )
            commands.append(f"A {press_text}")

    print(
        f"[main_fast] colors={len(layers)} macro_lines={len(commands)}",
        file=sys.stderr,
    )
    print("\n".join(commands))


if __name__ == "__main__":
    main()
