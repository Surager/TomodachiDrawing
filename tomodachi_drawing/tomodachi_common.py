import colorsys
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


CANVAS_WIDTH = 256
CANVAS_HEIGHT = 256

HUE_STEPS = 200
SAT_STEPS = 214
VAL_STEPS = 112

# Calibration derived from the observed hue probe sequence.
HUE_PANEL_ANCHORS = np.array([0, 18, 36, 54, 72, 90, 109, 127, 145, 163, 181, 199], dtype=np.float32)
HUE_CLOCKWISE_ANCHORS = np.array(
    [0.0, 45.6, 62.1, 81.3, 146.7, 171.2, 187.6, 211.9, 277.9, 296.9, 313.7, 351.1],
    dtype=np.float32,
)
SOURCE_TO_PANEL_HUE_MIRROR = False

# Vertical calibration from the observed top-right -> downward probe sequence.
VALUE_PANEL_ANCHORS = np.array([0, 10, 20, 30, 40, 50, 61, 71, 81, 91, 101, 111], dtype=np.float32)
VALUE_RGB_LEVELS = np.array(
    [0.0, 85 / 255.0, 119 / 255.0, 142 / 255.0, 162 / 255.0, 180 / 255.0,
     196 / 255.0, 208 / 255.0, 222 / 255.0, 233 / 255.0, 244 / 255.0, 1.0],
    dtype=np.float32,
)

# Horizontal calibration from the observed right -> left probe sequence.
SAT_PANEL_ANCHORS = np.array([0, 19, 39, 58, 78, 97, 116, 136, 155, 175, 194, 213], dtype=np.float32)
SAT_RGB_LEVELS = np.array(
    [
        0.0,
        11 / 255.0,
        22 / 255.0,
        34 / 255.0,
        46 / 255.0,
        59 / 255.0,
        75 / 255.0,
        93 / 255.0,
        113 / 255.0,
        137 / 255.0,
        170 / 255.0,
        1.0,
    ],
    dtype=np.float32,
)

BRUSH_SIZES = (1, 3, 7, 13, 19, 27)
BRUSH_LEVELS = {size: index for index, size in enumerate(BRUSH_SIZES)}

MOVE_BUTTONS = {
    (0, 1): "DPAD_RIGHT",
    (0, -1): "DPAD_LEFT",
    (1, 0): "DPAD_DOWN",
    (-1, 0): "DPAD_UP",
}

COLOR_MOVE_BUTTONS = {
    (0, 1): "DPAD_RIGHT",
    (0, -1): "DPAD_LEFT",
    (1, 0): "DPAD_DOWN",
    (-1, 0): "DPAD_UP",
}

COLOR_PANEL_HOME = (0, 0, 0)
BRUSH_HOME = 1
BRUSH_SELECT_PRESS = 0.2
BRUSH_SELECT_DPAD_PRESS = 0.2
BRUSH_SELECT_GAP = 0.75
BRUSH_SELECT_PADDING = 0.75
COLOR_SELECT_PRESS = 0.2
COLOR_SELECT_GAP = 0.75
COLOR_SELECT_PADDING = 0.75


@dataclass(frozen=True)
class ColorLayer:
    key: tuple[int, int, int]
    mask: np.ndarray
    count: int


def fmt_seconds(value):
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text}s"


def panel_hue_to_rgb_hue(panel_hue):
    clockwise = np.interp(panel_hue, HUE_PANEL_ANCHORS, HUE_CLOCKWISE_ANCHORS)
    return (360.0 - clockwise) % 360.0


def rgb_hue_to_panel_hue(rgb_hue):
    clockwise = (360.0 - rgb_hue) % 360.0
    return np.interp(clockwise, HUE_CLOCKWISE_ANCHORS, HUE_PANEL_ANCHORS)


def panel_value_to_rgb_value(panel_value):
    return np.interp(panel_value, VALUE_PANEL_ANCHORS, VALUE_RGB_LEVELS)


def rgb_value_to_panel_value(rgb_value):
    return np.interp(rgb_value, VALUE_RGB_LEVELS, VALUE_PANEL_ANCHORS)


def panel_saturation_to_rgb_saturation(panel_saturation):
    return np.interp(panel_saturation, SAT_PANEL_ANCHORS, SAT_RGB_LEVELS)


def rgb_saturation_to_panel_saturation(rgb_saturation):
    return np.interp(rgb_saturation, SAT_RGB_LEVELS, SAT_PANEL_ANCHORS)


def merge_similar_quantized_colors(keys, opaque, threshold):
    if threshold <= 0 or not np.any(opaque):
        return keys

    active_keys, counts = np.unique(keys[opaque], return_counts=True)
    active_keys = active_keys[active_keys >= 0]
    if active_keys.size <= 1:
        return keys

    count_lookup = {
        int(key): int(count)
        for key, count in zip(*np.unique(keys[opaque], return_counts=True))
        if int(key) >= 0
    }
    ordered_keys = sorted(active_keys.tolist(), key=lambda key: (-count_lookup[int(key)], int(key)))
    rgbs = {
        int(key): np.array(color_key_to_rgb(decode_color_key(int(key))), dtype=np.float32)
        for key in ordered_keys
    }

    unassigned = set(map(int, ordered_keys))
    mapping = {}
    threshold_sq = float(threshold) * float(threshold)

    for rep_key in ordered_keys:
        rep_key = int(rep_key)
        if rep_key not in unassigned:
            continue

        rep_rgb = rgbs[rep_key]
        group = []
        for key in list(unassigned):
            delta = rgbs[key] - rep_rgb
            if float(delta.dot(delta)) <= threshold_sq:
                group.append(key)

        for key in group:
            mapping[key] = rep_key
            unassigned.remove(key)

    if not mapping:
        return keys

    merged = keys.copy()
    for source_key, target_key in mapping.items():
        if source_key != target_key:
            merged[keys == source_key] = target_key
    return merged


def load_quantized_image(path, merge_threshold=0):
    image = Image.open(path).convert("RGBA")
    if image.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise ValueError(
            f"expected image size 256x256, got {image.size[0]}x{image.size[1]}"
        )

    rgba = np.array(image, dtype=np.uint8)
    alpha = rgba[:, :, 3] > 0

    hsv = Image.fromarray(rgba[:, :, :3], mode="RGB").convert("HSV")
    hsv_arr = np.array(hsv, dtype=np.uint8)

    source_hue = hsv_arr[:, :, 0].astype(np.float32) * 360.0 / 255.0
    hue = np.rint(rgb_hue_to_panel_hue(source_hue)).astype(np.int32) % HUE_STEPS
    source_sat = hsv_arr[:, :, 1].astype(np.float32) / 255.0
    sat = np.rint(rgb_saturation_to_panel_saturation(source_sat)).astype(np.int32)
    source_val = hsv_arr[:, :, 2].astype(np.float32) / 255.0
    val = np.rint(rgb_value_to_panel_value(source_val)).astype(np.int32)

    if SOURCE_TO_PANEL_HUE_MIRROR:
        hue = (-hue) % HUE_STEPS

    keys = hue * (SAT_STEPS * VAL_STEPS) + sat * VAL_STEPS + val
    keys = keys.astype(np.int32)
    keys[~alpha] = -1
    keys = merge_similar_quantized_colors(keys, alpha, merge_threshold)

    return keys, alpha


def decode_color_key(key):
    hue = key // (SAT_STEPS * VAL_STEPS)
    rest = key % (SAT_STEPS * VAL_STEPS)
    sat = rest // VAL_STEPS
    val = rest % VAL_STEPS
    return int(hue), int(sat), int(val)


def encode_color_key(color):
    hue, sat, val = color
    return int(hue) * (SAT_STEPS * VAL_STEPS) + int(sat) * VAL_STEPS + int(val)


def color_key_to_rgb(color):
    hue, sat, val = color
    h = panel_hue_to_rgb_hue(float(hue)) / 360.0
    s = panel_saturation_to_rgb_saturation(float(sat))
    v = panel_value_to_rgb_value(float(val))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (
        int(round(r * 255)),
        int(round(g * 255)),
        int(round(b * 255)),
    )


def build_color_layers(keys, opaque):
    flat_keys = keys.reshape(-1)
    flat_opaque = opaque.reshape(-1)
    active_indices = np.flatnonzero(flat_opaque)
    active_keys = flat_keys[active_indices]

    if active_indices.size == 0:
        return []

    order = np.argsort(active_keys, kind="mergesort")
    sorted_indices = active_indices[order]
    sorted_keys = active_keys[order]

    layers = []
    start = 0
    total = sorted_keys.size
    while start < total:
        key = int(sorted_keys[start])
        end = start + 1
        while end < total and int(sorted_keys[end]) == key:
            end += 1
        layer = np.zeros((CANVAS_HEIGHT, CANVAS_WIDTH), dtype=bool)
        layer.reshape(-1)[sorted_indices[start:end]] = True
        layers.append(
            ColorLayer(
                key=decode_color_key(key),
                mask=layer,
                count=end - start,
            )
        )
        start = end

    layers.sort(key=lambda item: (-item.count, item.key))
    return layers


def save_quantized_preview(keys, opaque, output_path):
    canvas = np.zeros((CANVAS_HEIGHT, CANVAS_WIDTH, 4), dtype=np.uint8)
    canvas[:, :, 3] = 0

    for color_key in np.unique(keys[opaque]):
        if int(color_key) < 0:
            continue
        color = decode_color_key(int(color_key))
        rgb = color_key_to_rgb(color)
        mask = keys == color_key
        canvas[mask, 0] = rgb[0]
        canvas[mask, 1] = rgb[1]
        canvas[mask, 2] = rgb[2]
        canvas[mask, 3] = 255

    image = Image.fromarray(canvas, mode="RGBA")
    image.save(output_path)


def dump_point_layers(layers, output_path):
    payload = {
        "size": [CANVAS_WIDTH, CANVAS_HEIGHT],
        "layers": [
            {
                "color": list(layer.key),
                "count": int(layer.count),
                "points": [
                    [int(col), int(row)]
                    for row, col in np.argwhere(layer.mask)
                ],
            }
            for layer in layers
        ],
    }
    Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# nxbt LOOP expansion (aligned with third_party/nxbt InputParser.parse_loops).
MIN_MACRO_LOOP_REPEATS = 5
MAX_MACRO_LOOP_BLOCK_LINES = 128


def _nxbt_parse_loops(macro):
    """Expand LOOP blocks to a flat list of lines (nxbt-compatible)."""
    parsed = []
    i = 0
    n = len(macro)
    while i < n:
        line = macro[i]
        if line.startswith("LOOP"):
            parts = line.split(" ", 1)
            if len(parts) < 2:
                raise ValueError(f"Invalid LOOP line: {line!r}")
            loop_count = int(parts[1])
            if i + 1 >= n:
                raise ValueError("LOOP has no body")
            nxt = macro[i + 1]
            if nxt.startswith("\t"):
                loop_delimiter = "\t"
            elif nxt.startswith("    "):
                loop_delimiter = "    "
            else:
                loop_delimiter = "  "
            j = i + 1
            loop_buffer = []
            while j < n and macro[j].startswith(loop_delimiter):
                loop_buffer.append(macro[j].replace(loop_delimiter, "", 1))
                j += 1
            i = j - 1
            if any(s.startswith("LOOP") for s in loop_buffer):
                loop_buffer = _nxbt_parse_loops(loop_buffer)
            parsed.extend(loop_buffer * loop_count)
        else:
            parsed.append(line)
        i += 1
    return parsed


def flatten_macro_lines(raw_lines):
    """Remove blanks and # comments, expand LOOP… blocks for execution or preview."""
    filtered = [
        s for s in raw_lines if s.strip() != "" and not s.strip().startswith("#")
    ]
    if not filtered:
        return []
    return _nxbt_parse_loops(filtered)


def collapse_macro_loop_blocks(commands, min_repeats=MIN_MACRO_LOOP_REPEATS):
    """Turn >= min_repeats consecutive copies of the same line block into nxbt LOOP syntax."""
    if not commands or len(commands) < min_repeats:
        return list(commands)
    n = len(commands)
    out = []
    i = 0
    while i < n:
        best_len = 0
        best_count = 0
        max_block = min(MAX_MACRO_LOOP_BLOCK_LINES, n - i)
        for block_len in range(max_block, 0, -1):
            block = commands[i : i + block_len]
            j = i + block_len
            count = 1
            while j + block_len <= n and commands[j : j + block_len] == block:
                count += 1
                j += block_len
            if count >= min_repeats:
                best_len = block_len
                best_count = count
                break
        if best_len:
            block = commands[i : i + best_len]
            out.append(f"LOOP {best_count}")
            for bline in block:
                out.append("\t" + bline)
            i += best_len * best_count
        else:
            out.append(commands[i])
            i += 1
    return out


def emit_button(commands, button, press_text, wait_text, include_wait=True):
    commands.append(f"{button} {press_text}")
    if include_wait:
        commands.append(wait_text)


def emit_canvas_move(commands, start, target, press_text, wait_text):
    row, col = start
    target_row, target_col = target

    step_col = 1 if target_col > col else -1
    while col != target_col:
        col += step_col
        emit_button(commands, MOVE_BUTTONS[(0, step_col)], press_text, wait_text)

    step_row = 1 if target_row > row else -1
    while row != target_row:
        row += step_row
        emit_button(commands, MOVE_BUTTONS[(step_row, 0)], press_text, wait_text)

    return row, col


def emit_color_move(commands, start, target, press_text, wait_text):
    sat, val = start
    target_sat, target_val = target

    step_sat = 1 if target_sat > sat else -1
    while sat != target_sat:
        sat += step_sat
        emit_button(commands, COLOR_MOVE_BUTTONS[(0, step_sat)], press_text, wait_text)

    step_val = 1 if target_val > val else -1
    while val != target_val:
        val += step_val
        emit_button(commands, COLOR_MOVE_BUTTONS[(-step_val, 0)], press_text, wait_text)

    return sat, val


def color_selection_resets_hue(target_color):
    _, sat, val = target_color
    return sat == 0 or val == 0


def normalize_color_after_selection(target_color):
    hue, sat, val = target_color
    if val == 0:
        return 0, 0, 0
    if sat == 0:
        return 0, 0, val
    return hue, sat, val


def emit_hue_move(commands, start_hue, target_hue, press_text, wait_text):
    if target_hue > start_hue:
        for _ in range(target_hue - start_hue):
            emit_button(commands, "ZR", press_text, wait_text)
    else:
        for _ in range(start_hue - target_hue):
            emit_button(commands, "ZL", press_text, wait_text)
    return target_hue


def emit_color_switch(commands, current_color, target_color, press_text, wait_text):
    if current_color == target_color:
        return current_color

    color_press_text = fmt_seconds(COLOR_SELECT_PRESS) # 0.1s
    color_gap_text = fmt_seconds(COLOR_SELECT_GAP) # 0.5s
    color_padding_text = fmt_seconds(COLOR_SELECT_PADDING) # 0.5s

    for _ in range(2): commands.append(color_padding_text) # 0.75s
    emit_button(commands, "Y", color_press_text, color_gap_text) # 0.1s + 0.5s
    emit_button(commands, "Y", color_press_text, color_gap_text) # 0.1s + 0.5s

    cur_h, cur_s, cur_v = current_color
    target_h, target_s, target_v = target_color

    cur_h = emit_hue_move(commands, cur_h, target_h, press_text, wait_text)
    cur_s, cur_v = emit_color_move(
        commands,
        (cur_s, cur_v),
        (target_s, target_v),
        press_text,
        wait_text,
    )
    
    commands.append(color_gap_text)
    emit_button(commands, "A", color_press_text, color_gap_text) # 0.1s + 0.5s
    for _ in range(2): commands.append(color_padding_text) # 0.75s
    return normalize_color_after_selection(target_color)


def emit_brush_switch(commands, current_size, target_size, press_text, wait_text):
    if current_size == target_size:
        return current_size

    brush_press_text = fmt_seconds(BRUSH_SELECT_PRESS)
    brush_dpad_text = fmt_seconds(BRUSH_SELECT_DPAD_PRESS)
    brush_gap_text = fmt_seconds(BRUSH_SELECT_GAP)
    brush_padding_text = fmt_seconds(BRUSH_SELECT_PADDING)

    for _ in range(2): commands.append(brush_padding_text) # 0.75s
    emit_button(commands, "X", brush_press_text, brush_gap_text)
    emit_button(commands, "X", brush_press_text, brush_gap_text)

    current_level = BRUSH_LEVELS[current_size]
    target_level = BRUSH_LEVELS[target_size]

    while current_level < target_level:
        emit_button(commands, "DPAD_RIGHT", brush_dpad_text, brush_gap_text)
        current_level += 1

    while current_level > target_level:
        emit_button(commands, "DPAD_LEFT", brush_dpad_text, brush_gap_text)
        current_level -= 1

    emit_button(commands, "A", brush_press_text, brush_gap_text)
    emit_button(commands, "A", brush_press_text, brush_gap_text)
    for _ in range(2): commands.append(brush_padding_text) # 0.75s
    return target_size


def center_windows(mask, size):
    if size == 1:
        return mask.astype(np.int32)

    arr = mask.astype(np.int32)
    padded = np.pad(arr, ((1, 0), (1, 0)), mode="constant", constant_values=0)
    integral = padded.cumsum(axis=0).cumsum(axis=1)
    sums = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return sums


def centers_that_fit(mask, size):
    if size == 1:
        return mask.copy()
    return center_windows(mask, size) == size * size


def greedy_brush_centers(color_mask, size, remaining, min_gain=1):
    if not np.any(color_mask):
        return []

    if size == 1:
        points = np.argwhere(remaining)
        return [tuple(map(int, point)) for point in points]

    valid_centers = centers_that_fit(color_mask, size)
    radius = size // 2
    centers = []

    while True:
        gains = center_windows(remaining, size)
        gains = gains.copy()
        gains[~valid_centers] = -1

        index = int(gains.argmax())
        gain = int(gains.flat[index])
        if gain < min_gain:
            break

        top = index // gains.shape[1]
        left = index % gains.shape[1]
        centers.append((top + radius, left + radius))
        remaining[top : top + size, left : left + size] = False

    return centers


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def order_row_snake(points):
    rows = {}
    for row, col in points:
        rows.setdefault(row, []).append(col)

    ordered = []
    left_to_right = True
    for row in sorted(rows):
        cols = sorted(rows[row], reverse=not left_to_right)
        for col in cols:
            ordered.append((row, col))
        left_to_right = not left_to_right
    return ordered


def order_column_snake(points):
    cols = {}
    for row, col in points:
        cols.setdefault(col, []).append(row)

    ordered = []
    top_to_bottom = True
    for col in sorted(cols):
        rows = sorted(cols[col], reverse=not top_to_bottom)
        for row in rows:
            ordered.append((row, col))
        top_to_bottom = not top_to_bottom
    return ordered


def order_nearest_neighbor(points, start):
    remaining = set(points)
    ordered = []
    current = start

    while remaining:
        target = min(remaining, key=lambda point: (manhattan(current, point), point[0], point[1]))
        ordered.append(target)
        remaining.remove(target)
        current = target

    return ordered


def route_length(start, points):
    current = start
    distance = 0
    for point in points:
        distance += manhattan(current, point)
        current = point
    return distance


def plan_visit_order(points, start):
    if not points:
        return []

    candidates = [
        ("row", order_row_snake(points)),
        ("column", order_column_snake(points)),
    ]
    if len(points) <= 2000:
        candidates.append(("nearest", order_nearest_neighbor(points, start)))

    return min(candidates, key=lambda item: route_length(start, item[1]))[1]


def build_span_variants(points):
    rows = {}
    cols = {}
    for row, col in points:
        rows.setdefault(row, []).append(col)
        cols.setdefault(col, []).append(row)

    for values in rows.values():
        values.sort()
    for values in cols.values():
        values.sort()

    variants = []
    row_keys = sorted(rows)
    for reverse_rows in (False, True):
        ordered_rows = row_keys[::-1] if reverse_rows else row_keys
        for first_left_to_right in (True, False):
            left_to_right = first_left_to_right
            path = []
            for row in ordered_rows:
                values = rows[row]
                if left_to_right:
                    span = range(values[0], values[-1] + 1)
                else:
                    span = range(values[-1], values[0] - 1, -1)
                for col in span:
                    path.append((row, col))
                left_to_right = not left_to_right
            variants.append(path)

    col_keys = sorted(cols)
    for reverse_cols in (False, True):
        ordered_cols = col_keys[::-1] if reverse_cols else col_keys
        for first_top_to_bottom in (True, False):
            top_to_bottom = first_top_to_bottom
            path = []
            for col in ordered_cols:
                values = cols[col]
                if top_to_bottom:
                    span = range(values[0], values[-1] + 1)
                else:
                    span = range(values[-1], values[0] - 1, -1)
                for row in span:
                    path.append((row, col))
                top_to_bottom = not top_to_bottom
            variants.append(path)

    return variants


def path_distance(path):
    if not path:
        return 0

    distance = 0
    prev_row, prev_col = path[0]
    for row, col in path[1:]:
        distance += abs(row - prev_row) + abs(col - prev_col)
        prev_row, prev_col = row, col
    return distance


def find_components(mask):
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    directions = (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    )

    components = []
    for start_row, start_col in np.argwhere(mask):
        start_row = int(start_row)
        start_col = int(start_col)
        if seen[start_row, start_col]:
            continue

        queue = deque([(start_row, start_col)])
        seen[start_row, start_col] = True
        component = []

        while queue:
            row, col = queue.popleft()
            component.append((row, col))
            for d_row, d_col in directions:
                next_row = row + d_row
                next_col = col + d_col
                if (
                    0 <= next_row < height
                    and 0 <= next_col < width
                    and mask[next_row, next_col]
                    and not seen[next_row, next_col]
                ):
                    seen[next_row, next_col] = True
                    queue.append((next_row, next_col))

        components.append(component)

    return components


def build_component_path(mask):
    variants_per_component = [
        build_span_variants(component) for component in find_components(mask)
    ]
    current = (0, 0)
    full_path = []

    while variants_per_component:
        best_score = None
        best_index = None
        best_variant = None

        for index, variants in enumerate(variants_per_component):
            for variant in variants:
                if not variant:
                    continue
                start = variant[0]
                score = (
                    abs(current[0] - start[0])
                    + abs(current[1] - start[1])
                    + path_distance(variant)
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best_index = index
                    best_variant = variant

        full_path.extend(best_variant)
        current = best_variant[-1]
        variants_per_component.pop(best_index)

    return full_path


def build_row_path(mask):
    points = [tuple(map(int, point)) for point in np.argwhere(mask)]
    variants = build_span_variants(points)
    return min(variants[:4], key=path_distance)


def build_column_path(mask):
    points = [tuple(map(int, point)) for point in np.argwhere(mask)]
    variants = build_span_variants(points)
    return min(variants[4:], key=path_distance)
