import argparse
import json
import math
from pathlib import Path

from .tomodachi_common import normalize_color_after_selection


CANVAS_WIDTH = 256
CANVAS_HEIGHT = 256

HUE_STEPS = 200
SAT_STEPS = 214
VAL_STEPS = 112
BRUSH_SIZES = (1, 3, 7, 13, 19, 27)

MOVE_BUTTONS = {
    (0, 1): "DPAD_RIGHT",
    (0, -1): "DPAD_LEFT",
    (1, 0): "DPAD_DOWN",
    (-1, 0): "DPAD_UP",
}


def fmt_seconds(value):
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text}s"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a calibration macro that probes one color axis and paints dots."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=12,
        help="Number of probe colors/dots to generate. Default: 12.",
    )
    parser.add_argument(
        "--axis",
        choices=("vertical", "horizontal"),
        default="vertical",
        help="Axis to sweep on the color panel. Default: vertical.",
    )
    parser.add_argument(
        "--output",
        default="color_probe_macro.txt",
        help="Output macro file. Default: color_probe_macro.txt.",
    )
    parser.add_argument(
        "--meta",
        default="color_probe_meta.json",
        help="Output metadata JSON file. Default: color_probe_meta.json.",
    )
    parser.add_argument(
        "--press",
        type=float,
        default=0.075,
        help="Button hold time in seconds. Default: 0.075.",
    )
    parser.add_argument(
        "--color-gap",
        type=float,
        default=0.2,
        help="Gap between the two Y presses. Default: 0.2.",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=0.1,
        help="Pause before and after opening the color panel or brush panel. Default: 0.1.",
    )
    parser.add_argument(
        "--canvas-margin",
        type=int,
        default=24,
        help="Margin used when laying out painted dots on the canvas. Default: 24.",
    )
    parser.add_argument(
        "--brush-size",
        type=int,
        default=27,
        choices=BRUSH_SIZES,
        help="Brush size to use for all probe dots. Default: 27.",
    )
    parser.add_argument(
        "--probe-spacing",
        type=int,
        default=32,
        help="Spacing between probe dots on the canvas. Default: 32.",
    )
    return parser.parse_args()


def linspace_steps(max_value, count):
    if count <= 1:
        return [0]
    return [int(round(i * max_value / (count - 1))) for i in range(count)]


def descending_steps(max_value, count):
    return list(reversed(linspace_steps(max_value, count)))


def build_panel_targets(axis, count):
    if axis == "vertical":
        hue = HUE_STEPS - 1
        sat = SAT_STEPS - 1
        values = descending_steps(VAL_STEPS - 1, count)
        return [(hue, sat, value) for value in values]

    hue = HUE_STEPS - 1
    value = VAL_STEPS - 1
    sats = descending_steps(SAT_STEPS - 1, count)
    return [(hue, sat, value) for sat in sats]


def build_canvas_positions(count, margin, spacing):
    cols = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / cols))

    block_w = (cols - 1) * spacing
    block_h = (rows - 1) * spacing
    start_x = min(margin, CANVAS_WIDTH - 1 - block_w - margin)
    start_y = min(margin, CANVAS_HEIGHT - 1 - block_h - margin)
    start_x = max(0, start_x)
    start_y = max(0, start_y)

    positions = []
    for row in range(rows):
        cols_order = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        for col in cols_order:
            positions.append((start_x + col * spacing, start_y + row * spacing))
    return positions[:count]


def emit_step(commands, button, press_text, wait_text):
    commands.append(f"{button} {press_text}")
    commands.append(wait_text)


def switch_brush(commands, current_size, target_size, press_text, pair_gap_text, padding_text):
    if current_size == target_size:
        return current_size

    commands.append(padding_text)
    emit_step(commands, "X", "0.1s", pair_gap_text)
    emit_step(commands, "X", "0.1s", pair_gap_text)

    current_level = BRUSH_SIZES.index(current_size)
    target_level = BRUSH_SIZES.index(target_size)

    while current_level < target_level:
        emit_step(commands, "DPAD_RIGHT", "0.1s", pair_gap_text)
        current_level += 1

    while current_level > target_level:
        emit_step(commands, "DPAD_LEFT", "0.1s", pair_gap_text)
        current_level -= 1

    emit_step(commands, "A", "0.1s", pair_gap_text)
    emit_step(commands, "A", "0.1s", pair_gap_text)
    commands.append(padding_text)
    return target_size


def move_color_panel(commands, current, target, press_text, wait_text):
    cur_h, cur_s, cur_v = current
    tar_h, tar_s, tar_v = target

    if tar_h > cur_h:
        for _ in range(tar_h - cur_h):
            emit_step(commands, "ZR", press_text, wait_text)
    else:
        for _ in range(cur_h - tar_h):
            emit_step(commands, "ZL", press_text, wait_text)

    if tar_s > cur_s:
        for _ in range(tar_s - cur_s):
            emit_step(commands, "DPAD_RIGHT", press_text, wait_text)
    else:
        for _ in range(cur_s - tar_s):
            emit_step(commands, "DPAD_LEFT", press_text, wait_text)

    if tar_v > cur_v:
        for _ in range(tar_v - cur_v):
            emit_step(commands, "DPAD_UP", press_text, wait_text)
    else:
        for _ in range(cur_v - tar_v):
            emit_step(commands, "DPAD_DOWN", press_text, wait_text)

    return target


def move_canvas(commands, current, target, press_text, wait_text):
    cur_x, cur_y = current
    tar_x, tar_y = target

    if tar_x > cur_x:
        for _ in range(tar_x - cur_x):
            emit_step(commands, MOVE_BUTTONS[(0, 1)], press_text, wait_text)
    else:
        for _ in range(cur_x - tar_x):
            emit_step(commands, MOVE_BUTTONS[(0, -1)], press_text, wait_text)

    if tar_y > cur_y:
        for _ in range(tar_y - cur_y):
            emit_step(commands, MOVE_BUTTONS[(1, 0)], press_text, wait_text)
    else:
        for _ in range(cur_y - tar_y):
            emit_step(commands, MOVE_BUTTONS[(-1, 0)], press_text, wait_text)

    return target


def main():
    args = parse_args()
    press_text = fmt_seconds(args.press)
    color_gap_text = fmt_seconds(args.color_gap)
    padding_text = fmt_seconds(args.padding)
    move_wait_text = fmt_seconds(args.press)

    panel_targets = build_panel_targets(args.axis, args.count)
    canvas_positions = build_canvas_positions(args.count, args.canvas_margin, args.probe_spacing)

    commands = []
    current_panel = (0, 0, 0)
    current_canvas = (0, 0)
    current_brush = 1

    meta = {
        "count": args.count,
        "axis": args.axis,
        "canvas_size": [CANVAS_WIDTH, CANVAS_HEIGHT],
        "samples": [],
    }

    current_brush = switch_brush(
        commands,
        current_brush,
        args.brush_size,
        press_text,
        color_gap_text,
        padding_text,
    )

    for index, (panel_target, canvas_target) in enumerate(zip(panel_targets, canvas_positions)):
        commands.append(padding_text)
        emit_step(commands, "Y", press_text, color_gap_text)
        emit_step(commands, "Y", press_text, move_wait_text)
        commands.append(padding_text)

        current_panel = move_color_panel(commands, current_panel, panel_target, press_text, move_wait_text)
        emit_step(commands, "A", press_text, move_wait_text)
        commands.append(padding_text)
        current_panel = normalize_color_after_selection(panel_target)

        current_canvas = move_canvas(commands, current_canvas, canvas_target, press_text, move_wait_text)
        emit_step(commands, "A", press_text, move_wait_text)
        commands.append(padding_text)

        meta["samples"].append(
            {
                "index": index,
                "panel_hsv": list(panel_target),
                "canvas_xy": list(canvas_target),
                "brush_size": args.brush_size,
            }
        )

    Path(args.output).write_text("\n".join(commands), encoding="utf-8")
    Path(args.meta).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"saved {args.output} with {args.count} probe points; metadata -> {args.meta}"
    )


if __name__ == "__main__":
    main()
