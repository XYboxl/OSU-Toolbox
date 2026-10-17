"""Legacy mania stage geometry and timing-aware scroll movement."""

import bisect
import math
from dataclasses import dataclass, replace

from .. import config


@dataclass(frozen=True)
class StageGeometry:
    y_spawn: float
    y_judge: float
    stage_y1: float
    stage_y2: float
    upside_down: bool


@dataclass(frozen=True)
class LegacyTimingSegment:
    time: float
    beat_length: float
    meter: int = 4
    effects: int = 0


def _timing_effects(timing_point):
    try:
        return int(getattr(timing_point, "effects", 0))
    except (TypeError, ValueError):
        return 0


def _valid_timing_points(timing_points):
    points = []
    for order, timing_point in enumerate(timing_points or ()):
        try:
            time = float(timing_point.time)
            beat_length = float(timing_point.beat_length)
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(time) or not math.isfinite(beat_length) or beat_length == 0.0:
            continue
        try:
            meter = max(1, int(timing_point.meter))
        except (AttributeError, TypeError, ValueError):
            meter = 4
        points.append((
            order,
            LegacyTimingSegment(
                time,
                beat_length,
                meter,
                _timing_effects(timing_point),
            ),
        ))
    points.sort(key=lambda item: (item[1].time, item[0]))
    return [item[1] for item in points]


def _legacy_timing_lists(timing_points):
    """Build Stable's effective movement and uninherited timing lists."""
    source = _valid_timing_points(timing_points)
    if not source:
        fallback = LegacyTimingSegment(-4000.0, 500.0, 4, 0)
        return (fallback,), (fallback,), (fallback,)

    first_positive = next(
        (point.beat_length for point in source if point.beat_length > 0.0),
        500.0,
    )
    current_redline = (
        source[0].beat_length if source[0].beat_length > 0.0
        else first_positive
    )
    effective = []
    redlines = []

    for point in source:
        if point.beat_length < 0.0:
            multiplier = min(10000.0, max(10.0, -point.beat_length)) / 100.0
            beat_length = current_redline * multiplier
        else:
            beat_length = point.beat_length

        segment = replace(point, beat_length=beat_length)
        while effective and segment.time <= effective[-1].time:
            effective.pop()
        if not effective or segment.beat_length != effective[-1].beat_length:
            effective.append(segment)

        if point.beat_length > 0.0:
            redlines.append(segment)
            current_redline = point.beat_length

    if not effective:
        effective.append(LegacyTimingSegment(source[0].time, first_positive, 4, 0))
    if not redlines:
        redlines.append(LegacyTimingSegment(source[0].time, first_positive, 4, 0))

    # Stable moves the first timing segment backwards by whole measures until
    # it is negative, then subtracts one additional measure.
    first = source[0]
    measure = first.beat_length * first.meter
    if measure <= 0.0 or not math.isfinite(measure):
        measure = redlines[0].beat_length * redlines[0].meter
    extended_time = first.time
    if measure > 0.0 and math.isfinite(measure):
        if extended_time >= 0.0:
            extended_time -= (math.floor(extended_time / measure) + 1.0) * measure
        extended_time -= measure

    effective[0] = replace(effective[0], time=extended_time)
    redlines[0] = replace(redlines[0], time=extended_time)
    return tuple(source), tuple(effective), tuple(redlines)


def _legacy_dominant_bpm(source, map_end_ms):
    """Return the duration-weighted dominant BPM used by Stable fixed speed."""
    boundary = max(0.0, float(map_end_ms))
    current_beat_length = 0.0
    durations = {}

    for index in range(len(source) - 1, -1, -1):
        point = source[index]
        if point.beat_length > 0.0:
            current_beat_length = point.beat_length
        if (
            current_beat_length != 0.0
            and point.time <= boundary
            and (point.beat_length > 0.0 or index == 0)
        ):
            segment_start = 0.0 if index == 0 else point.time
            duration = max(0, int(boundary - segment_start))
            durations[current_beat_length] = (
                durations.get(current_beat_length, 0) + duration
            )
            boundary = point.time

    if durations:
        dominant_beat_length = max(durations, key=durations.get)
    else:
        dominant_beat_length = next(
            (point.beat_length for point in source if point.beat_length > 0.0),
            600.0,
        )
    if dominant_beat_length <= 0.0:
        return 100.0
    return max(1.0, float(round(60000.0 / dominant_beat_length)))


class LegacyScrollPath:
    """Piecewise Stable mania movement in the legacy 480p coordinate space."""

    def __init__(
        self,
        timing_points,
        map_end_ms,
        speed_setting,
        speed_multiplier=1.0,
        clock_rate=1.0,
        bpm_scaled=False,
        constant_rate=None,
    ):
        source, effective, redlines = _legacy_timing_lists(timing_points)
        self.source_segments = source
        self.segments = effective
        self.redline_segments = redlines
        self.first_timing_time = float(effective[0].time)
        self.dominant_bpm = _legacy_dominant_bpm(source, map_end_ms)
        self.speed_setting = max(1, min(40, int(round(float(speed_setting)))))
        self.speed_multiplier = max(0.000001, float(speed_multiplier))
        self.clock_rate = max(0.000001, float(clock_rate))
        self.bpm_scaled = bool(bpm_scaled)

        if constant_rate is None:
            if self.bpm_scaled:
                effective_speed = float(self.speed_setting)
            else:
                effective_speed = (
                    self.speed_setting
                    * 100.0
                    / max(self.dominant_bpm * self.clock_rate, 1.0)
                )
            effective_speed *= self.speed_multiplier
            rates = [
                21.0 * effective_speed / segment.beat_length
                for segment in effective
            ]
            self.constant = False
        else:
            rate = max(0.000001, float(constant_rate))
            rates = [rate] * len(effective)
            effective_speed = rate * effective[0].beat_length / 21.0
            self.constant = True

        self.effective_speed = effective_speed
        self._starts = tuple(segment.time for segment in effective)
        self._rates = tuple(max(0.000001, rate) for rate in rates)
        cumulative = [0.0]
        for index in range(1, len(effective)):
            cumulative.append(
                cumulative[-1]
                + (self._starts[index] - self._starts[index - 1])
                * self._rates[index - 1]
            )
        self._cumulative = tuple(cumulative)

    @classmethod
    def constant_path(
        cls,
        timing_points,
        map_end_ms,
        speed_setting,
        raw_rate,
    ):
        return cls(
            timing_points,
            map_end_ms,
            speed_setting,
            constant_rate=raw_rate,
        )

    def _segment_index(self, time_ms):
        return max(0, bisect.bisect_right(self._starts, float(time_ms)) - 1)

    def rate_at(self, time_ms):
        return self._rates[self._segment_index(time_ms)]

    def distance_at(self, time_ms):
        time_ms = float(time_ms)
        if time_ms <= self._starts[0]:
            return 0.0
        index = self._segment_index(time_ms)
        return (
            self._cumulative[index]
            + (time_ms - self._starts[index]) * self._rates[index]
        )

    def displacement(self, start_ms, end_ms):
        return self.distance_at(end_ms) - self.distance_at(start_ms)

    def time_at_distance(self, distance):
        distance = float(distance)
        if distance <= 0.0:
            return self._starts[0]
        index = max(0, bisect.bisect_right(self._cumulative, distance) - 1)
        return (
            self._starts[index]
            + (distance - self._cumulative[index]) / self._rates[index]
        )

    def time_for_raw_position(self, target_ms, raw_position, hit_position):
        target_distance = self.distance_at(target_ms)
        desired_distance = (
            target_distance + float(raw_position) - float(hit_position)
        )
        return self.time_at_distance(desired_distance)

    def raw_position(self, target_ms, current_ms, hit_position):
        return (
            float(hit_position)
            + self.displacement(target_ms, current_ms)
        )


def build_legacy_scroll_path(
    timing_points,
    map_end_ms,
    hit_position,
    beatmap_ar=5.0,
    clock_rate=1.0,
):
    """Build Stable movement, retaining explicit AR and lazer overrides."""
    hit_position = max(1.0, float(hit_position))
    if (
        config.COMPATIBILITY_MODE != config.COMPAT_STABLE_REPLAY
        or config.SCROLL_AR_OVERRIDE is not None
    ):
        if config.SCROLL_AR_OVERRIDE is not None:
            preempt_ms, _ = compute_scroll(
                0.0,
                hit_position,
                config.SCROLL_AR_OVERRIDE,
            )
        else:
            speed_setting = max(1.0, min(40.0, float(config.SCROLL_SPEED)))
            preempt_ms = 11485.0 / speed_setting
        preempt_ms /= max(0.000001, float(config.SCROLL_SPEED_MULT))
        return LegacyScrollPath.constant_path(
            timing_points,
            map_end_ms,
            config.SCROLL_SPEED,
            hit_position / max(1.0, preempt_ms),
        )

    return LegacyScrollPath(
        timing_points,
        map_end_ms,
        config.SCROLL_SPEED,
        speed_multiplier=config.SCROLL_SPEED_MULT,
        clock_rate=clock_rate,
        bpm_scaled=False,
    )


def iter_measure_barline_times(timing_points, start_ms, end_ms):
    """Generate Stable measure lines, including first-point extension/effects."""
    _, _, redlines = _legacy_timing_lists(timing_points)
    start_ms = float(start_ms)
    end_ms = float(end_ms)
    out = []

    for index, point in enumerate(redlines):
        next_time = (
            redlines[index + 1].time - 1.0
            if index + 1 < len(redlines)
            else end_ms + 1.0
        )
        segment_end = min(end_ms + 1.0, next_time)
        measure_ms = point.beat_length * max(1, point.meter)
        if measure_ms <= 0.0 or segment_end <= point.time:
            continue

        count = max(0, math.ceil((start_ms - point.time) / measure_ms))
        time_ms = point.time + count * measure_ms
        while time_ms < segment_end - 1e-7:
            is_segment_first = abs(time_ms - point.time) <= 1e-7
            if not (is_segment_first and point.effects & 8):
                out.append(round(time_ms, 6))
            time_ms += measure_ms

    return sorted(set(out))


def resolve_upside_down(skin=None):
    if config.SCROLL_DIRECTION == "up":
        return True
    if config.SCROLL_DIRECTION == "down":
        return False
    if config.COMPATIBILITY_MODE == config.COMPAT_LAZER_CLASSIC:
        return False
    return bool(skin and skin.upside_down)


def scroll_direction_conflict(direction, skin):
    """Return a user-facing warning when an override conflicts with the skin."""
    if direction == "auto" or skin is None:
        return None
    skin_upside = bool(skin.upside_down)
    wants_upside = direction == "up"
    if wants_upside == skin_upside:
        return None
    skin_direction = "上落式" if skin_upside else "下落式"
    requested_direction = "上落式" if wants_upside else "下落式"
    return f"所选皮肤使用{skin_direction}，但渲染器设置为{requested_direction}。"


def stage_geometry(height, skin=None):
    scale = float(height) / 480.0
    upside_down = resolve_upside_down(skin)
    hit_position = skin.hit_position if skin is not None else 420.0
    if upside_down:
        y_judge = (480.0 - hit_position) * scale
        return StageGeometry(float(height), y_judge, y_judge, float(height), True)
    y_judge = hit_position * scale
    return StageGeometry(0.0, y_judge, 0.0, y_judge, False)


def _legacy_directional_position(raw_position, height, upside_down):
    raw_position = float(raw_position)
    position_480 = 480.0 - raw_position if upside_down else raw_position
    return position_480 * float(height) / 480.0


def _legacy_judgement_position(skin, height, upside_down):
    if skin is None:
        return None
    score_position = float(skin.score_position)
    position_480 = 480.0 - score_position if upside_down else score_position
    return position_480 * float(height) / 480.0


def compute_scroll(y_top, y_judge, approach_rate):
    """Return (preempt_ms, signed speed_px_per_ms) for an AR override."""
    ar = approach_rate
    preempt_ms = (1200 - 120 * (ar - 5)) if ar >= 5 else (1200 + 120 * (5 - ar))
    preempt_ms = max(preempt_ms, 1)
    speed = (y_judge - y_top) / preempt_ms
    return preempt_ms, speed


def resolve_scroll(y_top, y_judge, beatmap_ar):
    """Resolve the legacy compatibility descriptor and constant approximation."""
    if config.SCROLL_AR_OVERRIDE is not None:
        preempt_ms, speed = compute_scroll(y_top, y_judge, config.SCROLL_AR_OVERRIDE)
        descriptor = f"AR {config.SCROLL_AR_OVERRIDE:g}"
    else:
        speed_setting = max(1.0, min(40.0, float(config.SCROLL_SPEED)))
        preempt_ms = 11485.0 / speed_setting
        speed = (y_judge - y_top) / preempt_ms
        descriptor = f"speed {speed_setting:g}"
    if config.SCROLL_SPEED_MULT != 1.0:
        preempt_ms /= config.SCROLL_SPEED_MULT
        speed *= config.SCROLL_SPEED_MULT
    return preempt_ms, speed, descriptor


def compute_timeline_start_ms(objects, preempt_ms):
    """Compatibility helper for constant-scroll callers."""
    min_hit = min(obj.time for obj in objects)
    start = float(min_hit - preempt_ms)
    if config.PRE_ROLL_MS is not None:
        start -= config.PRE_ROLL_MS
    return start
