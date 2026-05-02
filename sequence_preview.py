import argparse
import re
from pathlib import Path

import numpy as np
from PIL import Image

from tomodachi_common import (
    BRUSH_HOME,
    BRUSH_LEVELS,
    BRUSH_SIZES,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    COLOR_PANEL_HOME,
    HUE_STEPS,
    SAT_STEPS,
    VAL_STEPS,
    color_key_to_rgb,
    normalize_color_after_selection,
)


TIME_RE = re.compile(r"^\d+(?:\.\d+)?s$")


def _stamp(canvas, center, size, color):
    radius = size // 2
    row, col = center
    top = max(0, row - radius)
    left = max(0, col - radius)
    bottom = min(CANVAS_HEIGHT, row + radius + 1)
    right = min(CANVAS_WIDTH, col + radius + 1)
    rgb = color_key_to_rgb(color)
    canvas[top:bottom, left:right, 0] = rgb[0]
    canvas[top:bottom, left:right, 1] = rgb[1]
    canvas[top:bottom, left:right, 2] = rgb[2]
    canvas[top:bottom, left:right, 3] = 255


def _parse_macro_lines(macro):
    if isinstance(macro, Path):
        text = macro.read_text(encoding="utf-8")
    elif isinstance(macro, str):
        try:
            candidate = Path(macro)
            if "\n" not in macro and candidate.exists():
                text = candidate.read_text(encoding="utf-8")
            else:
                text = macro
        except OSError:
            text = macro
    else:
        text = "\n".join(macro)

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def render_macro_preview(macro, output_path):
    """Replay this project's generated NXBT macro into a 256x256 preview PNG."""

    canvas = np.zeros((CANVAS_HEIGHT, CANVAS_WIDTH, 4), dtype=np.uint8)
    current_pos = [0, 0]
    current_color = COLOR_PANEL_HOME
    color_cursor = list(COLOR_PANEL_HOME)
    current_brush = BRUSH_HOME
    brush_level = BRUSH_LEVELS[current_brush]

    in_color_panel = False
    in_brush_panel = False
    brush_confirm_count = 0

    for line in _parse_macro_lines(macro):
        parts = line.split()
        if len(parts) == 1 and TIME_RE.match(parts[0]):
            continue
        if len(parts) < 2:
            continue

        controls = parts[:-1]
        if not TIME_RE.match(parts[-1]):
            continue

        if in_color_panel:
            if "ZR" in controls:
                color_cursor[0] = min(HUE_STEPS - 1, color_cursor[0] + 1)
            if "ZL" in controls:
                color_cursor[0] = max(0, color_cursor[0] - 1)
            if "DPAD_RIGHT" in controls:
                color_cursor[1] = min(SAT_STEPS - 1, color_cursor[1] + 1)
            if "DPAD_LEFT" in controls:
                color_cursor[1] = max(0, color_cursor[1] - 1)
            if "DPAD_UP" in controls:
                color_cursor[2] = min(VAL_STEPS - 1, color_cursor[2] + 1)
            if "DPAD_DOWN" in controls:
                color_cursor[2] = max(0, color_cursor[2] - 1)
            if "A" in controls:
                current_color = normalize_color_after_selection(tuple(color_cursor))
                color_cursor = list(current_color)
                in_color_panel = False
            continue

        if in_brush_panel:
            if "DPAD_RIGHT" in controls:
                brush_level = min(len(BRUSH_SIZES) - 1, brush_level + 1)
            if "DPAD_LEFT" in controls:
                brush_level = max(0, brush_level - 1)
            if "A" in controls:
                brush_confirm_count += 1
                current_brush = BRUSH_SIZES[brush_level]
                if brush_confirm_count >= 2:
                    in_brush_panel = False
                    brush_confirm_count = 0
            continue

        if "Y" in controls:
            in_color_panel = True
            color_cursor = list(current_color)
            continue

        if "X" in controls:
            in_brush_panel = True
            brush_level = BRUSH_LEVELS[current_brush]
            brush_confirm_count = 0
            continue

        if "DPAD_RIGHT" in controls:
            current_pos[1] = min(CANVAS_WIDTH - 1, current_pos[1] + 1)
        if "DPAD_LEFT" in controls:
            current_pos[1] = max(0, current_pos[1] - 1)
        if "DPAD_DOWN" in controls:
            current_pos[0] = min(CANVAS_HEIGHT - 1, current_pos[0] + 1)
        if "DPAD_UP" in controls:
            current_pos[0] = max(0, current_pos[0] - 1)
        if "A" in controls:
            _stamp(canvas, tuple(current_pos), current_brush, current_color)

    Image.fromarray(canvas, mode="RGBA").save(output_path)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Replay a generated TomodachiDrawing macro into a preview image."
    )
    parser.add_argument("macro", help="Path to the macro text file.")
    parser.add_argument("-o", "--output", required=True, help="Output PNG path.")
    return parser.parse_args()


def main():
    args = parse_args()
    render_macro_preview(args.macro, args.output)


if __name__ == "__main__":
    main()
