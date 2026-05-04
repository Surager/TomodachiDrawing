import argparse
import sys

import numpy as np

from .tomodachi_common import (
    BRUSH_HOME,
    BRUSH_SIZES,
    COLOR_PANEL_HOME,
    build_color_layers,
    center_windows,
    centers_that_fit,
    collapse_macro_loop_blocks,
    emit_brush_switch,
    emit_canvas_move,
    emit_color_switch,
    fmt_seconds,
    load_quantized_image,
    order_nearest_neighbor,
    order_row_snake,
    plan_visit_order,
    route_length,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a brush-aware macro for Tomodachi Life drawing."
    )
    parser.add_argument("image", help="Path to the 256x256 RGBA image.")
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
        help="Minimum newly covered pixels for a brush stamp. Default: 1.",
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


def stamp_square(mask_shape, center, size):
    radius = size // 2
    row, col = center
    top = max(0, row - radius)
    left = max(0, col - radius)
    bottom = min(mask_shape[0], row + radius + 1)
    right = min(mask_shape[1], col + radius + 1)
    stamped = np.zeros(mask_shape, dtype=bool)
    stamped[top:bottom, left:right] = True
    return stamped


def build_large_brush_plan(mask, min_gain):
    """Pick brush stamps by global max newly-covered pixels each step."""
    remaining = mask.copy()
    flat_plan = []
    large_sizes = [size for size in BRUSH_SIZES if size >= 3]

    while True:
        best_gain = -1
        best_size = None
        best_top = None
        best_left = None

        for size in large_sizes:
            if remaining.shape[0] < size or remaining.shape[1] < size:
                continue

            valid_centers = centers_that_fit(mask, size)
            if not np.any(valid_centers):
                continue

            gains = center_windows(remaining, size)
            gains = gains.copy()
            gains[~valid_centers] = -1
            index = int(gains.argmax())
            gain = int(gains.flat[index])
            if gain < min_gain:
                continue

            if gain > best_gain or (gain == best_gain and (best_size is None or size > best_size)):
                best_gain = gain
                best_size = size
                best_top = index // gains.shape[1]
                best_left = index % gains.shape[1]

        if best_gain < min_gain or best_size is None:
            break

        radius = best_size // 2
        center = (best_top + radius, best_left + radius)
        flat_plan.append((best_size, center))
        remaining[best_top : best_top + best_size, best_left : best_left + best_size] = False

    grouped = []
    for size in reversed(large_sizes):
        centers = [center for entry_size, center in flat_plan if entry_size == size]
        if centers:
            grouped.append((size, centers))
    return grouped


def build_pixel_fill_plan(mask, covered, start):
    residual = mask & ~covered
    points = [tuple(map(int, point)) for point in np.argwhere(residual)]
    if not points:
        return []

    row_path = order_row_snake(points)
    nearest_path = order_nearest_neighbor(points, start)
    row_distance = route_length(start, row_path)
    nearest_distance = route_length(start, nearest_path)
    return nearest_path if nearest_distance < row_distance else row_path


def generate_commands(image, press, wait, min_gain, merge_threshold=0, return_home_per_layer=False):
    keys, opaque = load_quantized_image(image, merge_threshold=merge_threshold)
    layers = build_color_layers(keys, opaque)
    press_text = fmt_seconds(press)
    wait_text = fmt_seconds(wait)

    commands = []
    current_color = COLOR_PANEL_HOME
    current_pos = (0, 0)
    current_brush = BRUSH_HOME

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

        brush_plan = build_large_brush_plan(layer.mask, min_gain)
        covered = np.zeros_like(layer.mask, dtype=bool)

        for size, centers in brush_plan:
            centers = plan_visit_order(centers, current_pos)
            current_brush = emit_brush_switch(
                commands,
                current_brush,
                size,
                press_text,
                wait_text,
            )
            for center in centers:
                current_pos = emit_canvas_move(
                    commands,
                    current_pos,
                    center,
                    press_text,
                    wait_text,
                )
                commands.append(f"A {press_text}")
                covered |= stamp_square(layer.mask.shape, center, size)

        fill_points = build_pixel_fill_plan(layer.mask, covered, current_pos)
        if fill_points:
            current_brush = emit_brush_switch(
                commands,
                current_brush,
                1,
                press_text,
                wait_text,
            )
            for point in fill_points:
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

    return collapse_macro_loop_blocks(commands), len(layers)


def main():
    args = parse_args()
    commands, layer_count = generate_commands(
        args.image,
        args.press,
        args.wait,
        args.min_gain,
        args.merge_threshold,
        args.return_home_per_layer,
    )
    print(
        f"[main_brush] colors={layer_count} macro_lines={len(commands)}",
        file=sys.stderr,
    )
    print("\n".join(commands))


if __name__ == "__main__":
    main()
