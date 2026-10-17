"""legacy mania 渲染的纯函数（easing / alpha / scale）。

只读常量来自 legacy_constants；唯一动态依赖是 config.SCALE（随
configure_runtime 重算），因此必须用 config.SCALE 属性访问。
"""

import math

from . import config
from .legacy_constants import (
    LEGACY_COMBO_GHOST_ALPHA,
    LEGACY_COMBO_GHOST_MS,
    LEGACY_COMBO_GHOST_SCALE,
    LEGACY_COMBO_BURST_FADE_IN_MS,
    LEGACY_COMBO_BURST_FADE_OUT_START_MS,
    LEGACY_COMBO_BURST_MOVE_MS,
    LEGACY_COMBO_BURST_TOTAL_MS,
    LEGACY_KEY_WARNING_FADE_MS,
    LEGACY_KEY_WARNING_HOLD_MS,
    LEGACY_KEY_WARNING_TOTAL_MS,
    LEGACY_COMBO_POP_FADE_IN_MS,
    LEGACY_COMBO_POP_MS,
    LEGACY_COMBO_POP_SCALE_Y,
    LEGACY_HEALTH_BULGE_FROM,
    LEGACY_HEALTH_BULGE_MS,
    LEGACY_HEALTH_BULGE_TO,
    LEGACY_HEALTH_FLASH_MS,
    LEGACY_HEALTH_FLASH_SCALE,
    LEGACY_HEALTH_FLASH_SCALE_EPIC,
    LEGACY_HIT_ERROR_TICK_ALPHA,
    LEGACY_HIT_ERROR_TICK_FADE_IN_MS,
    LEGACY_HIT_ERROR_TICK_FADE_OUT_MS,
    LEGACY_HIT_EXPLOSION_FADE_IN_MS,
    LEGACY_HIT_EXPLOSION_FADE_OUT_MS,
    LEGACY_HIT_EXPLOSION_TOTAL_MS,
    LEGACY_HOLD_LIGHT_FADE_IN_MS,
    LEGACY_HOLD_LIGHT_FADE_OUT_MS,
    LEGACY_JUDGEMENT_FADE_IN_MS,
    LEGACY_JUDGEMENT_FADE_OUT_MS,
    LEGACY_JUDGEMENT_HOLD_MS,
    LEGACY_JUDGEMENT_MISS_ROTATION_DEG,
    LEGACY_JUDGEMENT_TOTAL_MS,
    LEGACY_POSITION_SCALE_FACTOR,
)


def _legacy_internal_screen_scale():
    """Scale osu!lazer's x768 legacy UI pixels to the current output height."""
    return config.SCALE / LEGACY_POSITION_SCALE_FACTOR


def _legacy_disallow_zero_alpha(colour):
    r, g, b, a = colour
    return r, g, b, (1.0 if a <= 0.0 else a)


def _legacy_apply_with_doubled_alpha(colour):
    r, g, b, a = colour
    _, _, _, colour_alpha = _legacy_disallow_zero_alpha(colour)
    return r, g, b, a * colour_alpha


def _legacy_judgement_line_colour(colour):
    r, g, b, a = _legacy_disallow_zero_alpha(colour)
    return r, g, b, a * 0.9


def _legacy_hit_explosion_alpha(age_ms):
    age_ms = max(0.0, float(age_ms))
    if age_ms < LEGACY_HIT_EXPLOSION_FADE_IN_MS:
        return age_ms / LEGACY_HIT_EXPLOSION_FADE_IN_MS
    if age_ms < LEGACY_HIT_EXPLOSION_TOTAL_MS:
        return 1.0 - (age_ms - LEGACY_HIT_EXPLOSION_FADE_IN_MS) / LEGACY_HIT_EXPLOSION_FADE_OUT_MS
    return 0.0


def _legacy_hit_explosion_fps(frame_count):
    if frame_count <= 0:
        return 0.0
    frame_length = max(1000.0 / 60.0, 170.0 / float(frame_count))
    return 1000.0 / frame_length


def _legacy_judgement_alpha(age_ms):
    age_ms = max(0.0, float(age_ms))
    fade_out_start = LEGACY_JUDGEMENT_FADE_IN_MS + LEGACY_JUDGEMENT_HOLD_MS
    if age_ms < LEGACY_JUDGEMENT_FADE_IN_MS:
        return _easing_out_quad(age_ms / LEGACY_JUDGEMENT_FADE_IN_MS)
    if age_ms < fade_out_start:
        return 1.0
    if age_ms < LEGACY_JUDGEMENT_TOTAL_MS:
        return 1.0 - _easing_in_quad(
            (age_ms - fade_out_start) / LEGACY_JUDGEMENT_FADE_OUT_MS,
        )
    return 0.0


def _legacy_judgement_scale(age_ms, hit_value):
    age_ms = max(0.0, float(age_ms))
    if hit_value <= 0:
        if age_ms < 100.0:
            return 1.2 + (1.0 - 1.2) * _easing_out_quad(age_ms / 100.0)
        return 1.0
    if age_ms < 40.0:
        return 0.8 + (1.0 - 0.8) * (age_ms / 40.0)
    if age_ms < 80.0:
        return 0.85 + (0.7 - 0.85) * ((age_ms - 40.0) / 40.0)
    if age_ms < 180.0:
        return 0.7
    if age_ms < LEGACY_JUDGEMENT_TOTAL_MS:
        return 0.7 + (0.4 - 0.7) * _easing_in_quad((age_ms - 180.0) / 40.0)
    return 0.4


def _easing_out_quad(progress):
    progress = max(0.0, min(1.0, float(progress)))
    return progress * (2.0 - progress)


def _easing_in_quad(progress):
    progress = max(0.0, min(1.0, float(progress)))
    return progress * progress


def _easing_out_circ(progress):
    """Stable easing id 22 (OutCirc), clamped like osu.Framework."""
    progress = max(0.0, min(1.0, float(progress)))
    return math.sqrt(max(0.0, 1.0 - (progress - 1.0) ** 2))


def _easing_out_quint(progress):
    progress = max(0.0, min(1.0, float(progress)))
    return 1.0 - (1.0 - progress) ** 5


def _easing_in_quint(progress):
    progress = max(0.0, min(1.0, float(progress)))
    return progress ** 5


def _legacy_combo_pop_scale_y(age_ms):
    if age_ms < 0.0:
        return 1.0
    eased = _easing_out_quad(age_ms / LEGACY_COMBO_POP_MS)
    return LEGACY_COMBO_POP_SCALE_Y + (1.0 - LEGACY_COMBO_POP_SCALE_Y) * eased


def _legacy_combo_show_alpha(age_ms):
    # FadeIn(120) 只作用于计数器从隐藏(0)转为可见的时刻；连击期间恒为 1。
    if age_ms < 0.0:
        return 1.0
    return max(0.0, min(1.0, age_ms / LEGACY_COMBO_POP_FADE_IN_MS))


def _legacy_combo_ghost_state(age_ms):
    if age_ms < 0.0 or age_ms >= LEGACY_COMBO_GHOST_MS:
        return 1.0, 0.0
    progress = age_ms / LEGACY_COMBO_GHOST_MS
    scale = 1.0 + (LEGACY_COMBO_GHOST_SCALE - 1.0) * progress
    alpha = LEGACY_COMBO_GHOST_ALPHA * (1.0 - progress)
    return scale, alpha


def _legacy_combo_burst_state(age_ms):
    """Return (movement progress, alpha) for one legacy comboburst."""
    age_ms = float(age_ms)
    if age_ms < 0.0 or age_ms >= LEGACY_COMBO_BURST_TOTAL_MS:
        return 1.0, 0.0
    movement = _easing_out_circ(age_ms / LEGACY_COMBO_BURST_MOVE_MS)
    if age_ms < LEGACY_COMBO_BURST_FADE_IN_MS:
        alpha = age_ms / LEGACY_COMBO_BURST_FADE_IN_MS
    elif age_ms < LEGACY_COMBO_BURST_FADE_OUT_START_MS:
        alpha = 1.0
    else:
        alpha = 1.0 - (
            (age_ms - LEGACY_COMBO_BURST_FADE_OUT_START_MS)
            / (LEGACY_COMBO_BURST_TOTAL_MS - LEGACY_COMBO_BURST_FADE_OUT_START_MS)
        )
    return movement, max(0.0, min(1.0, alpha))


def _legacy_key_warning_alpha(age_ms):
    """Alpha envelope for the pre-play mania key binding reminder."""
    age_ms = float(age_ms)
    if age_ms < 0.0 or age_ms >= LEGACY_KEY_WARNING_TOTAL_MS:
        return 0.0
    if age_ms < LEGACY_KEY_WARNING_HOLD_MS:
        return 1.0
    return max(
        0.0,
        min(1.0, 1.0 - (age_ms - LEGACY_KEY_WARNING_HOLD_MS) / LEGACY_KEY_WARNING_FADE_MS),
    )


def _legacy_deterministic_index(sequence_index, count, salt=0):
    """Stable, process-independent pseudo-random index for replay rendering."""
    count = int(count)
    if count <= 0:
        return 0
    x = (
        (int(sequence_index) + 1) * 0x9E3779B97F4A7C15
        + (int(salt) + 1) * 0xBF58476D1CE4E5B9
    ) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 31
    return int(x % count)


def _legacy_health_flash_state(age_ms, epic):
    if age_ms < 0.0 or age_ms >= LEGACY_HEALTH_FLASH_MS:
        return 1.0, 0.0
    eased = _easing_out_quad(age_ms / LEGACY_HEALTH_FLASH_MS)
    target = (
        LEGACY_HEALTH_FLASH_SCALE_EPIC if epic else LEGACY_HEALTH_FLASH_SCALE
    )
    return 1.0 + (target - 1.0) * eased, 1.0 - eased


def _legacy_health_bulge_scale(age_ms):
    if age_ms < 0.0:
        return 1.0
    progress = max(0.0, min(1.0, age_ms / LEGACY_HEALTH_BULGE_MS))
    return (
        LEGACY_HEALTH_BULGE_FROM
        + (LEGACY_HEALTH_BULGE_TO - LEGACY_HEALTH_BULGE_FROM) * progress
    )


def _legacy_hold_light_alpha(phase, age_ms):
    if phase == "hold":
        return max(0.0, min(1.0, age_ms / LEGACY_HOLD_LIGHT_FADE_IN_MS))
    if phase == "release":
        return max(0.0, 1.0 - age_ms / LEGACY_HOLD_LIGHT_FADE_OUT_MS)
    return 0.0


def _legacy_hit_error_tick_alpha(age_ms):
    if age_ms < 0.0:
        return 0.0
    if age_ms < LEGACY_HIT_ERROR_TICK_FADE_IN_MS:
        return LEGACY_HIT_ERROR_TICK_ALPHA * _easing_out_quint(
            age_ms / LEGACY_HIT_ERROR_TICK_FADE_IN_MS
        )
    fade_age = age_ms - LEGACY_HIT_ERROR_TICK_FADE_IN_MS
    if fade_age >= LEGACY_HIT_ERROR_TICK_FADE_OUT_MS:
        return 0.0
    return LEGACY_HIT_ERROR_TICK_ALPHA * (
        1.0 - _easing_in_quint(fade_age / LEGACY_HIT_ERROR_TICK_FADE_OUT_MS)
    )


def _legacy_miss_rotation_deg(time_ms, col):
    """miss 判定的确定性随机倾角（splitmix64）。

    lazer 用 RNG.NextSingle(±5.73°)；这里以判定时间(0.1ms 量化)+列号播种,
    保证并行分段与重复渲染得到相同角度。
    """
    x = (
        int(round(float(time_ms) * 10.0)) * 0x9E3779B97F4A7C15
        + (int(col) + 1) * 0xBF58476D1CE4E5B9
    ) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 31
    return ((x / 2.0 ** 64) * 2.0 - 1.0) * LEGACY_JUDGEMENT_MISS_ROTATION_DEG
