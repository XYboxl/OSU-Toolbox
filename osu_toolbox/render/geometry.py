"""轨道矩形和 legacy split-stage 几何计算。"""

import math
from dataclasses import dataclass

from ..mania.timeline import stage_geometry


@dataclass(frozen=True)
class StageLayout:
    lane_rects: tuple
    stage_ranges: tuple
    column_stage_indices: tuple
    scale_factor: float


def resolve_stage_layout(width, height, key_count, skin):
    scale = float(height) / 480.0
    geometry = stage_geometry(height, skin)
    stage_y1 = int(round(geometry.stage_y1))
    stage_y2 = int(round(geometry.stage_y2))
    logical_width = float(math.ceil(float(width) / scale))

    widths = [float(value) for value in skin.column_width[:key_count]]
    spacings = [float(value) for value in skin.column_spacing[:max(0, key_count - 1)]]
    split = key_count > 1 and (
        skin.split_stages if skin.has_split_stages else key_count >= 10
    )
    first_count = (key_count + 1) // 2 if split else key_count
    total_width = sum(widths) + sum(spacings)
    if split:
        total_width += skin.stage_separation

    start = min(float(skin.column_start), logical_width - float(skin.column_right))
    right_reserve = min(
        float(skin.column_right), logical_width - float(skin.column_start),
    )
    overflow = max(
        0.0,
        start + total_width + right_reserve - logical_width,
    )
    factor = (
        (total_width - overflow) / total_width
        if total_width > 0.0 else 1.0
    )

    rects = []
    stage_indices = []
    stage_ranges = []
    x = start
    stage_start = x
    stage_index = 0
    for index in range(key_count):
        if split and index == first_count:
            stage_ranges.append((stage_start * scale, x * scale))
            x += skin.stage_separation * factor
            stage_start = x
            stage_index = 1
        if index > 0:
            x += spacings[index - 1] * factor
        x1 = x * scale
        x += widths[index] * factor
        x2 = x * scale
        rects.append((
            int(round(x1)), stage_y1,
            int(round(x2)), stage_y2,
        ))
        stage_indices.append(stage_index)
    stage_ranges.append((stage_start * scale, x * scale))

    return StageLayout(
        lane_rects=tuple(rects),
        stage_ranges=tuple(stage_ranges),
        column_stage_indices=tuple(stage_indices),
        scale_factor=factor,
    )


def lane_geometry(width, height, key_count, skin=None):
    """Return lane rectangles using the resolved stage bounds."""
    scale = height / 480.0
    geometry = stage_geometry(height, skin)
    stage_y1 = int(round(geometry.stage_y1))
    stage_y2 = int(round(geometry.stage_y2))
    if skin is not None:
        layout = resolve_stage_layout(width, height, key_count, skin)
        rects = list(layout.lane_rects)
        lane_w = rects[0][2] - rects[0][0] if rects else 0
        return rects, lane_w

    margin_x = int(width * 0.08)
    total_width = width - 2 * margin_x
    lane_w = total_width // key_count
    gap = 2
    rects = []
    for i in range(key_count):
        x1 = margin_x + i * lane_w + gap
        x2 = margin_x + (i + 1) * lane_w - gap
        rects.append((x1, stage_y1, x2, stage_y2))
    return rects, lane_w
