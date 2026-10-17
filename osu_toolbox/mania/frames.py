"""逐帧状态生成：FrameStateIterable、LegacyHudAnimator、_RollingValue。"""

import math

from .. import config
from ..legacy_constants import (
    LEGACY_ACCURACY_ROLL_MS,
    LEGACY_BREAK_MIN_DURATION_MS,
    LEGACY_COMBO_GHOST_MS,
    LEGACY_COMBO_BURST_TOTAL_MS,
    LEGACY_COMBO_POP_FADE_IN_MS,
    LEGACY_COMBO_POP_MS,
    LEGACY_COMBO_ROLL_MS_PER_UNIT,
    LEGACY_FRAME_INTERVAL_MS,
    LEGACY_HEALTH_BULGE_MS,
    LEGACY_HEALTH_EPIC_CUTOFF,
    LEGACY_HEALTH_FALL_DIVISOR,
    LEGACY_HEALTH_FLASH_MS,
    LEGACY_HEALTH_INTRO_LEAD_IN_MS,
    LEGACY_HEALTH_INTRO_RATE_PER_MS,
    LEGACY_HEALTH_RISE_DIVISOR,
    LEGACY_HIT_ERROR_AVG_SMOOTH,
    LEGACY_HIT_ERROR_MAX_TICKS,
    LEGACY_HIT_ERROR_TICK_FADE_IN_MS,
    LEGACY_HIT_ERROR_TICK_FADE_OUT_MS,
    LEGACY_HIT_EXPLOSION_TOTAL_MS,
    LEGACY_HOLD_FALL_FREE,
    LEGACY_HOLD_LIGHT_FADE_OUT_MS,
    LEGACY_JUDGEMENT_TOTAL_MS,
    LEGACY_KEY_RELEASE_DELAY_MS,
    LEGACY_KEY_WARNING_TOTAL_MS,
    LEGACY_LETTERBOX_FADE_IN_MS,
    LEGACY_LETTERBOX_FADE_OUT_MS,
    LEGACY_MANIA_BODY_FRAME_MS,
    LEGACY_SCORE_ROLL_MS,
    LEGACY_STAGE_LIGHT_FADE_OUT_MS,
    LEGACY_WARNING_ARROW_COUNT,
    LEGACY_WARNING_ARROW_SPACING_MS,
)
from ..legacy_math import (
    _easing_out_quad,
    _legacy_health_bulge_scale,
)
from .judgement import (
    JudgementEvent,
    _judgement_count_summary,
    _judgements_by_object,
    judge_replay,
    mania_accuracy_progress,
    mania_hit_windows,
    mania_lazer_score_progress,
    mania_health_progress,
    mania_score_progress,
    parse_life_graph,
)
from .mods import (
    MOD_DOUBLE_TIME,
    MOD_HALF_TIME,
    MOD_NIGHTCORE,
    MOD_SCORE_V2,
)
from .timeline import (
    build_legacy_scroll_path,
    iter_measure_barline_times,
)


class _RollingValue:
    """lazer RollingCounter: a transform that restarts from the display value."""

    __slots__ = ("start", "target", "t0", "duration")

    def __init__(self, value, duration_ms):
        self.start = float(value)
        self.target = float(value)
        self.t0 = float("-inf")
        self.duration = float(duration_ms)

    def set_target(self, value, t_ms):
        value = float(value)
        if value == self.target:
            return
        self.start = self.value_at(t_ms)
        self.target = value
        self.t0 = float(t_ms)

    def value_at(self, t_ms):
        if self.duration <= 0.0:
            return self.target
        progress = (float(t_ms) - self.t0) / self.duration
        if progress >= 1.0:
            return self.target
        if progress <= 0.0:
            return self.start
        return self.start + (self.target - self.start) * _easing_out_quad(progress)


class LegacyHudAnimator:
    """Sequential integrator for stateful HUD display values.

    Frame states are generated once, in order, in the main process and then
    sliced across parallel render workers, so every animation that depends on
    history (rolling counters, LegacyHealthDisplay's eased fill and intro
    climb, combo roll-down) must be resolved here and stored per frame.
    Pure functions of an event age stay in the renderer.
    """

    def __init__(
        self,
        key_count,
        start_ms,
        first_hit_ms,
        last_hit_ms,
        breaks=(),
        letterbox=False,
        collect_hit_errors=False,
    ):
        self.key_count = int(key_count)
        self.start_ms = float(start_ms)
        self.first_hit_ms = float(first_hit_ms)
        self.last_hit_ms = float(last_hit_ms)
        self.breaks = tuple(
            (float(start), float(end))
            for start, end in breaks
            if float(end) - float(start) >= LEGACY_BREAK_MIN_DURATION_MS
        )
        self.letterbox = bool(letterbox)
        self.collect_hit_errors = bool(collect_hit_errors)

        self.score_roll = _RollingValue(0.0, LEGACY_SCORE_ROLL_MS)
        self.acc_roll = _RollingValue(100.0, LEGACY_ACCURACY_ROLL_MS)

        self.prev_combo = 0
        self.combo_steady = 0
        self.combo_pop_t0 = float("-inf")
        self.combo_show_t0 = float("-inf")
        self.combo_ghost_value = 0
        self.combo_ghost_t0 = float("-inf")
        self.combo_roll_from = 0
        self.combo_roll_t0 = float("-inf")
        self.combo_roll_duration = 0.0

        # Combo bursts are milestone events (100, 200, ...).  Keep the
        # sequence number in the materialised frame state so worker segments
        # can select the same side/resource without sharing RNG state.
        self.combo_burst_baseline = 0
        self.combo_burst_serial = 0
        self.combo_burst_active_index = -1
        self.combo_burst_t0 = float("-inf")

        self.health_display = 0.0
        self.bulge_t0 = float("-inf")
        self.flash_t0 = float("-inf")
        self.flash_epic = False
        self._health_intro_active = True

        self.hold_phase = ["idle"] * self.key_count
        self.hold_t0 = [0.0] * self.key_count
        self.hold_release_t0 = [0.0] * self.key_count
        self.hit_error_ticks = []
        self.hit_error_avg = 0.0

    def _combo_display(self, t_ms):
        if self.combo_roll_t0 > float("-inf"):
            if self.combo_roll_duration <= 0.0:
                return 0
            progress = (t_ms - self.combo_roll_t0) / self.combo_roll_duration
            if progress >= 1.0:
                return 0
            return int(round(self.combo_roll_from * (1.0 - max(0.0, progress))))
        return self.combo_steady

    def advance(
        self,
        t_ms,
        dt_ms,
        combo,
        score,
        acc,
        health,
        frame_judgements,
        hold_active_bits=0,
    ):
        t_ms = float(t_ms)

        self.score_roll.set_target(score, t_ms)
        self.acc_roll.set_target(acc, t_ms)

        if combo != self.prev_combo:
            was_visible = self._combo_display(t_ms) > 0
            if combo == 0:
                ghost_from = self._combo_display(t_ms)
                if ghost_from > 0:
                    self.combo_ghost_value = int(ghost_from)
                    self.combo_ghost_t0 = t_ms
                    self.combo_roll_from = int(ghost_from)
                    self.combo_roll_t0 = t_ms
                    self.combo_roll_duration = (
                        ghost_from * LEGACY_COMBO_ROLL_MS_PER_UNIT
                    )
                self.combo_steady = 0
                self.combo_pop_t0 = float("-inf")
            else:
                if combo > self.prev_combo:
                    self.combo_pop_t0 = t_ms
                    if not was_visible:
                        self.combo_show_t0 = t_ms
                self.combo_roll_t0 = float("-inf")
                self.combo_steady = combo
            self.prev_combo = combo

        # Stable emits a burst whenever the combo crosses the next hundred.
        # A miss resets the milestone baseline, while an already visible
        # burst is allowed to finish on screen.  The loop also handles replay
        # frames which contain several judgements at the same timestamp.
        if combo <= 0:
            self.combo_burst_baseline = 0
        else:
            if combo < self.combo_burst_baseline:
                self.combo_burst_baseline = (int(combo) // 100) * 100
            threshold = self.combo_burst_baseline + 100
            while combo >= threshold:
                self.combo_burst_baseline = threshold
                self.combo_burst_active_index = self.combo_burst_serial
                self.combo_burst_serial += 1
                self.combo_burst_t0 = t_ms
                threshold += 100

        # Stable stores health on a 0..200 scale. These equations are the same
        # update in this renderer's normalized 0..1 domain.
        target_health = max(0.0, min(1.0, float(health)))
        frame_multiplier = max(0.0, float(dt_ms)) / LEGACY_FRAME_INTERVAL_MS
        stable_mania = (
            config.COMPATIBILITY_MODE == config.COMPAT_STABLE_REPLAY
        )
        if stable_mania:
            self._health_intro_active = False
            if self.health_display < target_health:
                self.health_display = min(
                    1.0,
                    self.health_display
                    + abs(target_health - self.health_display)
                    / LEGACY_HEALTH_RISE_DIVISOR
                    * frame_multiplier,
                )
            elif self.health_display > target_health:
                self.health_display = max(
                    0.0,
                    self.health_display
                    - abs(self.health_display - target_health)
                    / LEGACY_HEALTH_FALL_DIVISOR
                    * frame_multiplier,
                )
        elif self.health_display < target_health:
            if (
                self._health_intro_active
                and t_ms > self.first_hit_ms - LEGACY_HEALTH_INTRO_LEAD_IN_MS
            ):
                self.health_display = min(
                    1.0,
                    self.health_display
                    + LEGACY_HEALTH_INTRO_RATE_PER_MS * max(0.0, float(dt_ms)),
                )
                if t_ms - self.bulge_t0 >= LEGACY_HEALTH_BULGE_MS:
                    self.bulge_t0 = t_ms
            elif not self._health_intro_active:
                self.health_display = min(
                    1.0,
                    self.health_display
                    + abs(target_health - self.health_display)
                    / LEGACY_HEALTH_RISE_DIVISOR
                    * frame_multiplier,
                )
        elif self.health_display > target_health:
            self._health_intro_active = False
            self.health_display = max(
                0.0,
                self.health_display
                - abs(self.health_display - target_health)
                / LEGACY_HEALTH_FALL_DIVISOR
                * frame_multiplier,
            )

        for event in frame_judgements:
            if not stable_mania and event.value > 0:
                # Flash implies Bulge (LegacyMarker.Flash calls Bulge first);
                # multiple hits per frame debounce into one flash.
                self.flash_t0 = t_ms
                self.flash_epic = target_health >= LEGACY_HEALTH_EPIC_CUTOFF
                self.bulge_t0 = t_ms
                break

        if t_ms < self.first_hit_ms and self.first_hit_ms > self.start_ms:
            progress = -1.0 + (
                (t_ms - self.start_ms) / (self.first_hit_ms - self.start_ms)
            )
            progress_is_intro = True
        else:
            span = self.last_hit_ms - self.first_hit_ms
            frac = (t_ms - self.first_hit_ms) / span if span > 0.0 else 1.0
            progress = frac
            progress_is_intro = False
        progress = max(-2.0, min(1.0, progress))

        letterbox = 0.0
        if self.letterbox:
            for break_start, break_end in self.breaks:
                if break_start > t_ms:
                    break
                if t_ms <= break_end:
                    fade_in = (t_ms - break_start) / LEGACY_LETTERBOX_FADE_IN_MS
                    fade_out = (break_end - t_ms) / LEGACY_LETTERBOX_FADE_OUT_MS
                    letterbox = max(0.0, min(1.0, fade_in, fade_out))
                    break

        hold_states = []
        for col in range(self.key_count):
            active = (hold_active_bits >> col) & 1
            phase = self.hold_phase[col]
            if active:
                if phase != "hold":
                    self.hold_phase[col] = "hold"
                    self.hold_t0[col] = t_ms
                hold_states.append(("hold", t_ms - self.hold_t0[col]))
                continue
            if phase == "hold":
                self.hold_phase[col] = "release"
                self.hold_release_t0[col] = t_ms
                phase = "release"
            if phase == "release":
                release_age = t_ms - self.hold_release_t0[col]
                if release_age < LEGACY_HOLD_LIGHT_FADE_OUT_MS:
                    hold_states.append((
                        "release",
                        release_age,
                        t_ms - self.hold_t0[col],
                    ))
                    continue
                self.hold_phase[col] = "idle"
            hold_states.append(None)

        if self.collect_hit_errors:
            for event in frame_judgements:
                if event.value <= 0:
                    continue
                error = float(getattr(event, "error_ms", 0.0))
                self.hit_error_ticks.append((error, float(event.time)))
                self.hit_error_avg = (
                    self.hit_error_avg * LEGACY_HIT_ERROR_AVG_SMOOTH
                    + error * (1.0 - LEGACY_HIT_ERROR_AVG_SMOOTH)
                )
            max_age = (
                LEGACY_HIT_ERROR_TICK_FADE_IN_MS
                + LEGACY_HIT_ERROR_TICK_FADE_OUT_MS
            )
            while self.hit_error_ticks and (
                t_ms - self.hit_error_ticks[0][1] >= max_age
            ):
                self.hit_error_ticks.pop(0)
            if len(self.hit_error_ticks) > LEGACY_HIT_ERROR_MAX_TICKS:
                del self.hit_error_ticks[
                    : len(self.hit_error_ticks) - LEGACY_HIT_ERROR_MAX_TICKS
                ]
            hit_errors = tuple(
                (error, t_ms - event_time)
                for error, event_time in self.hit_error_ticks
            )
        else:
            hit_errors = ()

        pop_age = t_ms - self.combo_pop_t0
        show_age = t_ms - self.combo_show_t0
        ghost_age = t_ms - self.combo_ghost_t0
        flash_age = t_ms - self.flash_t0
        combo_burst_age = t_ms - self.combo_burst_t0
        ghost_active = 0.0 <= ghost_age < LEGACY_COMBO_GHOST_MS
        return {
            "score_display": int(self.score_roll.value_at(t_ms)),
            "acc_display": float(self.acc_roll.value_at(t_ms)),
            "combo_display": self._combo_display(t_ms),
            "combo_pop_age_ms": (
                pop_age if 0.0 <= pop_age <= LEGACY_COMBO_POP_MS else -1.0
            ),
            "combo_show_age_ms": (
                show_age
                if 0.0 <= show_age < LEGACY_COMBO_POP_FADE_IN_MS else -1.0
            ),
            "combo_ghost_value": self.combo_ghost_value if ghost_active else 0,
            "combo_ghost_age_ms": ghost_age if ghost_active else -1.0,
            "combo_burst_age_ms": (
                combo_burst_age
                if 0.0 <= combo_burst_age < LEGACY_COMBO_BURST_TOTAL_MS
                else -1.0
            ),
            "combo_burst_event_index": (
                self.combo_burst_active_index
                if 0.0 <= combo_burst_age < LEGACY_COMBO_BURST_TOTAL_MS
                else -1
            ),
            "combo_holding": bool(hold_active_bits),
            "health_display": max(0.0, min(1.0, self.health_display)),
            "health_marker_scale": (
                1.0 if stable_mania else
                _legacy_health_bulge_scale(t_ms - self.bulge_t0)
                if self.bulge_t0 > float("-inf") else 1.0
            ),
            "health_flash_age_ms": (
                flash_age
                if not stable_mania
                and 0.0 <= flash_age < LEGACY_HEALTH_FLASH_MS
                else -1.0
            ),
            "health_flash_epic": (
                False if stable_mania else bool(self.flash_epic)
            ),
            "progress": progress,
            "progress_is_intro": progress_is_intro,
            "letterbox": letterbox,
            "key_warning_age_ms": (
                t_ms - self.start_ms
                if 0.0 <= t_ms - self.start_ms < LEGACY_KEY_WARNING_TOTAL_MS
                else -1.0
            ),
            "hold_light_states": tuple(hold_states),
            "hit_errors": hit_errors,
            "hit_error_avg": (
                float(self.hit_error_avg) if self.collect_hit_errors else 0.0
            ),
        }


class FrameStateIterable:
    """Repeatable lazy frame source with cursor-based active object tracking."""

    def __init__(
        self,
        beatmap,
        key_count,
        y_top,
        y_judge,
        fps=None,
        replay=None,
        height=None,
        skin_config=None,
        warning_arrow_height_480=None,
    ):
        self.beatmap = beatmap
        self.key_count = int(key_count)
        self.y_top = float(y_top)
        self.y_judge = float(y_judge)
        self.fps = int(fps if fps is not None else config.FPS)
        self.replay = replay
        self.compatibility_mode = config.COMPATIBILITY_MODE
        self.frame_dt_ms = 1000.0 / float(self.fps)
        if height is None:
            largest_coordinate = max(abs(self.y_top), abs(self.y_judge))
            height = 480.0 if largest_coordinate <= 480.0 else config.HEIGHT
        self.screen_height = max(1.0, float(height))
        self.screen_scale = self.screen_height / 480.0
        self.direction_sign = 1.0 if self.y_judge >= self.y_top else -1.0
        if self.direction_sign > 0.0:
            self.hit_position = self.y_judge / self.screen_scale
        else:
            self.hit_position = 480.0 - self.y_judge / self.screen_scale
        self.hit_position = max(0.0, min(480.0, self.hit_position))
        self.barline_height = max(
            0.0,
            float(getattr(skin_config, "barline_height", 0.0)),
        )
        if warning_arrow_height_480 is None:
            warning_arrow_height_480 = 480.0
        try:
            warning_arrow_height_480 = float(warning_arrow_height_480)
        except (TypeError, ValueError):
            warning_arrow_height_480 = 0.0
        if not math.isfinite(warning_arrow_height_480):
            warning_arrow_height_480 = 0.0
        self.warning_arrow_height_480 = max(0.0, warning_arrow_height_480)
        self.warning_arrow_entry_position = -self.warning_arrow_height_480
        self.objects = list(beatmap.hit_objects)
        self.effect_window_ms = max(
            float(config.HIT_FLASH_MS),
            LEGACY_HIT_EXPLOSION_TOTAL_MS,
            LEGACY_JUDGEMENT_TOTAL_MS,
        )
        self.hit_flash_ms = float(config.HIT_FLASH_MS)
        self.stage_y1 = min(self.y_top, self.y_judge)
        self.stage_y2 = max(self.y_top, self.y_judge)

        if not self.objects:
            self.scroll_path = build_legacy_scroll_path(
                beatmap.timing_points,
                0.0,
                max(1.0, self.hit_position),
                beatmap.approach_rate,
            )
            self.preempt_ms = 0.0
            self.speed = (
                self.direction_sign
                * self.scroll_path.rate_at(0.0)
                * self.screen_scale
            )
            self.start_song_ms = 0.0
            self.end_ms = 0.0
            self.first_hit_time_ms = 0.0
            self.last_hit_time_ms = 0.0
            self.breaks = ()
            self.letterbox_enabled = False
            self.warning_times = ()
            self._hit_error_windows = (0, 0, 0)
            self.frame_count = 0
            self.judgements = []
            self.score_progress = []
            self.acc_progress = []
            self.health_progress = []
            self.life_graph = []
            self.holds = []
            self.taps = []
            self.barline_times = []
            self.replay_key_events = []
            self.autoplay_key_events = []
            return

        self.end_ms = max(o.end_time if o.is_hold else o.time for o in self.objects)
        self.first_hit_time_ms = float(min(o.time for o in self.objects))
        self.last_hit_time_ms = float(self.end_ms)
        mods = int(getattr(replay, "mods", 0)) if replay is not None else 0
        if mods & (MOD_DOUBLE_TIME | MOD_NIGHTCORE):
            clock_rate = 1.5
        elif mods & MOD_HALF_TIME:
            clock_rate = 0.75
        else:
            clock_rate = 1.0
        self.scroll_path = build_legacy_scroll_path(
            beatmap.timing_points,
            self.last_hit_time_ms,
            max(1.0, self.hit_position),
            beatmap.approach_rate,
            clock_rate=clock_rate,
        )
        first_entry_ms = self.scroll_path.time_for_raw_position(
            self.first_hit_time_ms,
            0.0,
            self.hit_position,
        )
        self.preempt_ms = max(0.0, self.first_hit_time_ms - first_entry_ms)
        self.speed = (
            self.direction_sign
            * self.scroll_path.rate_at(self.first_hit_time_ms)
            * self.screen_scale
        )

        warning_times = []
        warning_time = self.first_hit_time_ms - LEGACY_WARNING_ARROW_SPACING_MS
        while (
            warning_time > self.scroll_path.first_timing_time
            and len(warning_times) < LEGACY_WARNING_ARROW_COUNT
        ):
            warning_times.append(warning_time)
            warning_time -= LEGACY_WARNING_ARROW_SPACING_MS
        self.warning_times = tuple(sorted(warning_times))

        visible_warning_times = (
            self.warning_times
            if config.SHOW_WARNING_ARROW and self.warning_arrow_height_480 > 0.0
            else ()
        )
        start_candidates = [first_entry_ms]
        start_candidates.extend(
            self.scroll_path.time_for_raw_position(
                warning_time,
                self.warning_arrow_entry_position,
                self.hit_position,
            )
            for warning_time in visible_warning_times
        )
        self.start_song_ms = min(start_candidates)
        if config.PRE_ROLL_MS is not None:
            self.start_song_ms -= float(config.PRE_ROLL_MS)
        if replay is not None and replay.frames:
            self.end_ms = max(self.end_ms, max(f.time for f in replay.frames))
        total_ms = self.end_ms + config.TAIL_MS - self.start_song_ms
        self.frame_count = max(0, int(total_ms * self.fps / 1000.0) + 1)

        if replay is not None:
            judgements, judge_windows = judge_replay(beatmap, replay, self.key_count)
            replay_counts = {
                "geki": replay.count_geki,
                "300": replay.count_300,
                "katu": replay.count_katu,
                "100": replay.count_100,
                "50": replay.count_50,
                "miss": replay.count_miss,
            }
            count_summary = _judgement_count_summary(judgements)
            if count_summary != replay_counts:
                print(
                    "WARNING: replay judgement counts differ: "
                    f"calculated={count_summary}, osr={replay_counts}"
                )
            mods = replay.mods
        else:
            judgements = [
                JudgementEvent(o.time, o.col, 320, o.is_hold)
                for o in self.objects
            ]
            judge_windows = mania_hit_windows(
                beatmap.overall_difficulty,
                self.compatibility_mode == config.COMPAT_LAZER_CLASSIC,
            )
            mods = 0
        self._hit_error_windows = (
            judge_windows["great"],
            judge_windows["ok"],
            judge_windows["meh"],
        )

        self.judgements = sorted(judgements, key=lambda ev: (ev.time, ev.col))

        if self.compatibility_mode == config.COMPAT_LAZER_CLASSIC:
            self.score_progress = mania_lazer_score_progress(
                self.judgements, len(self.objects), mods,
            )
            accuracy_mods = mods | MOD_SCORE_V2
        else:
            self.score_progress = mania_score_progress(
                self.judgements, len(self.objects), mods,
            )
            accuracy_mods = mods
        self.acc_progress = mania_accuracy_progress(self.judgements, accuracy_mods)

        self.life_graph = []
        if (
            self.compatibility_mode == config.COMPAT_STABLE_REPLAY
            and replay is not None
        ):
            self.life_graph = parse_life_graph(replay.life_graph)
        self.health_progress = mania_health_progress(
            self.objects, self.judgements, beatmap.hp_drain_rate,
        )

        object_judgements = _judgements_by_object(
            self.objects, self.judgements, self.key_count,
        )
        self.holds = []
        self.taps = []
        for obj in self.objects:
            judgement = object_judgements.get(id(obj))
            if obj.is_hold:
                # fall_from: None=判定线消耗; LEGACY_HOLD_FALL_FREE=头未命中、
                # 全程自由下落; >=0=按住后断体、剩余段自该时刻从判定线下落。
                fall_from = None
                visible_until = float(obj.end_time)
                head_hit_time = float(obj.time) if replay is None else None
                animation_end_time = float(obj.end_time)
                if replay is not None and judgement is not None:
                    head_hit = bool(getattr(judgement, "hold_head_hit", True))
                    if head_hit:
                        candidate = getattr(
                            judgement,
                            "hold_head_time_ms",
                            None,
                        )
                        try:
                            candidate = float(candidate)
                        except (TypeError, ValueError):
                            candidate = float("nan")
                        head_hit_time = (
                            candidate
                            if math.isfinite(candidate)
                            else float(obj.time)
                        )
                    animation_end_time = max(
                        float(judgement.time),
                        head_hit_time if head_hit_time is not None else float(obj.time),
                    )
                    drop_ms = float(getattr(judgement, "hold_drop_ms", -1.0))
                    if judgement.value <= 0 and not head_hit:
                        fall_from = LEGACY_HOLD_FALL_FREE
                    elif drop_ms >= 0.0:
                        fall_from = drop_ms
                    if fall_from is None:
                        visible_until = float(judgement.time)
                    else:
                        visible_until = self.scroll_path.time_for_raw_position(
                            obj.end_time,
                            480.0,
                            self.hit_position,
                        )
                self.holds.append((
                    int(obj.col),
                    float(obj.time),
                    float(obj.end_time),
                    float(visible_until),
                    fall_from,
                    head_hit_time,
                    animation_end_time,
                ))
            else:
                judgement_time = (
                    obj.time if judgement is None else judgement.time
                )
                self.taps.append((
                    int(obj.col),
                    float(obj.time),
                    float(judgement_time),
                ))

        if config.SHOW_BARLINES:
            self.barline_times = iter_measure_barline_times(
                beatmap.timing_points,
                self.start_song_ms,
                self.end_ms + config.TAIL_MS,
            )
        else:
            self.barline_times = []

        self.breaks = tuple(
            (float(start), float(end))
            for start, end in (getattr(beatmap, "breaks", ()) or ())
        )
        if config.LETTERBOX_IN_BREAKS is None:
            self.letterbox_enabled = bool(
                getattr(beatmap, "letterbox_in_breaks", False),
            )
        else:
            self.letterbox_enabled = bool(config.LETTERBOX_IN_BREAKS)
        key_mask = (1 << self.key_count) - 1
        if replay is not None and replay.frames:
            self.replay_key_events = [
                (float(frame.time), int(frame.keys) & key_mask)
                for frame in replay.frames
            ]
        else:
            self.replay_key_events = []

        self.autoplay_key_events = []
        if replay is None:
            tap_press_ms = max(self.hit_flash_ms, self.frame_dt_ms)
            for obj in self.objects:
                start = float(obj.time)
                end = float(obj.end_time) + 1e-7 if obj.is_hold else start + tap_press_ms
                self.autoplay_key_events.append((start, 1, int(obj.col)))
                self.autoplay_key_events.append((end, -1, int(obj.col)))
            self.autoplay_key_events.sort(
                key=lambda event: (event[0], -event[1], event[2]),
            )

    def __len__(self):
        return self.frame_count

    def _screen_displacement(self, start_ms, end_ms):
        return (
            self.direction_sign
            * self.scroll_path.displacement(start_ms, end_ms)
            * self.screen_scale
        )

    def _screen_position(self, target_ms, current_ms):
        return self.y_judge + self._screen_displacement(target_ms, current_ms)

    def _movement_animation_start(self, target_ms):
        entry_ms = self.scroll_path.time_for_raw_position(
            target_ms,
            0.0,
            self.hit_position,
        )
        # Stable 对齐 MovementY 变换时间时进行整型截断。
        return float(int(entry_ms))

    def __iter__(self):
        if self.frame_count <= 0:
            return

        animator = LegacyHudAnimator(
            self.key_count,
            self.start_song_ms,
            self.first_hit_time_ms,
            self.last_hit_time_ms,
            breaks=self.breaks,
            letterbox=self.letterbox_enabled,
            collect_hit_errors=config.SHOW_HIT_ERROR,
        )

        hold_head_animation_starts = tuple(
            self._movement_animation_start(time_ms)
            for _, time_ms, _, _, _, _, _ in self.holds
        )
        hold_tail_animation_starts = tuple(
            self._movement_animation_start(end_time)
            for _, _, end_time, _, _, _, _ in self.holds
        )
        tap_animation_starts = tuple(
            self._movement_animation_start(time_ms)
            for _, time_ms, _ in self.taps
        )
        hold_entries = sorted(
            (
                self.scroll_path.time_for_raw_position(
                    time_ms,
                    0.0,
                    self.hit_position,
                ),
                idx,
            )
            for idx, (_, time_ms, _, _, _, _, _) in enumerate(self.holds)
        )
        hold_exits = sorted(
            (visible_until + self.frame_dt_ms + 1e-7, idx)
            for idx, (_, _, _, visible_until, _, _, _) in enumerate(self.holds)
        )
        tap_entries = sorted(
            (
                self.scroll_path.time_for_raw_position(
                    time_ms,
                    0.0,
                    self.hit_position,
                ),
                idx,
            )
            for idx, (_, time_ms, _) in enumerate(self.taps)
        )
        tap_exits = sorted(
            (judgement_time, idx)
            for idx, (_, _, judgement_time) in enumerate(self.taps)
        )
        bar_entries = [
            (
                self.scroll_path.time_for_raw_position(
                    bar_time,
                    0.0,
                    self.hit_position,
                ),
                idx,
            )
            for idx, bar_time in enumerate(self.barline_times)
        ]
        bar_exits = [
            (
                self.scroll_path.time_for_raw_position(
                    bar_time,
                    480.0 + self.barline_height,
                    self.hit_position,
                ) + 1e-7,
                idx,
            )
            for idx, bar_time in enumerate(self.barline_times)
        ]
        visible_warning_times = (
            self.warning_times
            if config.SHOW_WARNING_ARROW and self.warning_arrow_height_480 > 0.0
            else ()
        )
        warning_entries = [
            (
                self.scroll_path.time_for_raw_position(
                    warning_time,
                    self.warning_arrow_entry_position,
                    self.hit_position,
                ),
                idx,
            )
            for idx, warning_time in enumerate(visible_warning_times)
        ]
        warning_exits = [
            (
                self.scroll_path.time_for_raw_position(
                    warning_time,
                    480.0,
                    self.hit_position,
                ) + 1e-7,
                idx,
            )
            for idx, warning_time in enumerate(visible_warning_times)
        ]

        active_holds = {}
        active_taps = {}
        active_barlines = {}
        active_warnings = {}
        hold_enter_idx = hold_exit_idx = 0
        tap_enter_idx = tap_exit_idx = 0
        bar_enter_idx = bar_exit_idx = 0
        warning_enter_idx = warning_exit_idx = 0
        judgement_idx = score_idx = acc_idx = health_idx = 0
        hit_start_idx = hit_end_idx = 0
        replay_key_idx = autoplay_key_idx = 0
        combo = 0
        score = 0
        acc = 100.0
        health = 1.0
        current_replay_keys = 0
        autoplay_counts = [0] * self.key_count
        key_release_until = [float("-inf")] * self.key_count
        key_press_start = [None] * self.key_count
        stage_light_release_start = [None] * self.key_count
        hold_animation_started = [False] * len(self.holds)
        hold_animation_active = [False] * len(self.holds)
        hold_animation_age = [0.0] * len(self.holds)
        hold_animation_resumed_at = [None] * len(self.holds)
        # A legal early hit can occur before the LN's raw-0 movement entry.
        hold_event_indices_by_col = [[] for _ in range(self.key_count)]
        for hold_idx, hold in enumerate(self.holds):
            if hold[5] is not None:
                hold_event_indices_by_col[hold[0]].append(hold_idx)
        for indices in hold_event_indices_by_col:
            indices.sort(key=lambda hold_idx: self.holds[hold_idx][5])
        hold_event_cursors = [0] * self.key_count
        hold_event_candidates = [dict() for _ in range(self.key_count)]

        def freeze_hold_animation_age(age_ms):
            # Stable disables SpriteAnimation by pinning its current frame.
            return (
                math.floor(max(0.0, age_ms) / LEGACY_MANIA_BODY_FRAME_MS)
                * LEGACY_MANIA_BODY_FRAME_MS
            )

        def update_hold_animation(event_time, col, is_down):
            event_time = float(event_time)
            indices = hold_event_indices_by_col[col]
            cursor = hold_event_cursors[col]
            candidates = hold_event_candidates[col]
            while cursor < len(indices):
                hold_idx = indices[cursor]
                hold = self.holds[hold_idx]
                if hold[5] > event_time:
                    break
                if event_time <= hold[6]:
                    candidates[hold_idx] = hold
                cursor += 1
            hold_event_cursors[col] = cursor

            for hold_idx, hold in tuple(candidates.items()):
                if event_time > hold[6]:
                    del candidates[hold_idx]
                    continue
                head_hit_time = hold[5]
                animation_end_time = hold[6]
                if (
                    event_time < head_hit_time
                    or event_time > animation_end_time
                ):
                    continue
                if is_down and not hold_animation_active[hold_idx]:
                    hold_animation_started[hold_idx] = True
                    hold_animation_active[hold_idx] = True
                    hold_animation_resumed_at[hold_idx] = event_time
                elif not is_down and hold_animation_active[hold_idx]:
                    resumed_at = hold_animation_resumed_at[hold_idx]
                    hold_animation_age[hold_idx] = freeze_hold_animation_age(
                        hold_animation_age[hold_idx]
                        + max(0.0, event_time - resumed_at),
                    )
                    hold_animation_active[hold_idx] = False
                    hold_animation_resumed_at[hold_idx] = None

        for frame_idx in range(self.frame_count):
            t_ms = self.start_song_ms + frame_idx * self.frame_dt_ms

            while hold_enter_idx < len(hold_entries) and hold_entries[hold_enter_idx][0] <= t_ms:
                idx = hold_entries[hold_enter_idx][1]
                active_holds[idx] = self.holds[idx]
                hold_enter_idx += 1
            while hold_exit_idx < len(hold_exits) and hold_exits[hold_exit_idx][0] <= t_ms:
                active_holds.pop(hold_exits[hold_exit_idx][1], None)
                hold_exit_idx += 1
            while tap_enter_idx < len(tap_entries) and tap_entries[tap_enter_idx][0] <= t_ms:
                idx = tap_entries[tap_enter_idx][1]
                active_taps[idx] = self.taps[idx]
                tap_enter_idx += 1
            while tap_exit_idx < len(tap_exits) and tap_exits[tap_exit_idx][0] <= t_ms:
                active_taps.pop(tap_exits[tap_exit_idx][1], None)
                tap_exit_idx += 1
            while bar_enter_idx < len(bar_entries) and bar_entries[bar_enter_idx][0] <= t_ms:
                idx = bar_entries[bar_enter_idx][1]
                active_barlines[idx] = self.barline_times[idx]
                bar_enter_idx += 1
            while bar_exit_idx < len(bar_exits) and bar_exits[bar_exit_idx][0] <= t_ms:
                active_barlines.pop(bar_exits[bar_exit_idx][1], None)
                bar_exit_idx += 1
            while (
                warning_enter_idx < len(warning_entries)
                and warning_entries[warning_enter_idx][0] <= t_ms
            ):
                idx = warning_entries[warning_enter_idx][1]
                active_warnings[idx] = visible_warning_times[idx]
                warning_enter_idx += 1
            while (
                warning_exit_idx < len(warning_exits)
                and warning_exits[warning_exit_idx][0] <= t_ms
            ):
                active_warnings.pop(warning_exits[warning_exit_idx][1], None)
                warning_exit_idx += 1

            if self.replay_key_events:
                while (
                    replay_key_idx < len(self.replay_key_events)
                    and self.replay_key_events[replay_key_idx][0] <= t_ms
                ):
                    event_time, next_keys = self.replay_key_events[replay_key_idx]
                    pressed_bits = next_keys & ~current_replay_keys
                    released_bits = current_replay_keys & ~next_keys
                    for col in range(self.key_count):
                        bit = 1 << col
                        if pressed_bits & bit:
                            key_press_start[col] = event_time
                            stage_light_release_start[col] = None
                            update_hold_animation(event_time, col, True)
                        if released_bits & bit:
                            key_release_until[col] = event_time + LEGACY_KEY_RELEASE_DELAY_MS
                            stage_light_release_start[col] = event_time
                            update_hold_animation(event_time, col, False)
                    current_replay_keys = next_keys
                    replay_key_idx += 1
                raw_pressed_cols = current_replay_keys
            else:
                while (
                    autoplay_key_idx < len(self.autoplay_key_events)
                    and self.autoplay_key_events[autoplay_key_idx][0] <= t_ms
                ):
                    event_time, delta, col = self.autoplay_key_events[autoplay_key_idx]
                    before = autoplay_counts[col]
                    autoplay_counts[col] = max(0, before + delta)
                    after = autoplay_counts[col]
                    if delta > 0:
                        if before == 0:
                            key_press_start[col] = event_time
                            stage_light_release_start[col] = None
                        update_hold_animation(event_time, col, True)
                    elif before > 0 and after == 0:
                        key_release_until[col] = event_time + LEGACY_KEY_RELEASE_DELAY_MS
                        stage_light_release_start[col] = event_time
                        update_hold_animation(event_time, col, False)
                    autoplay_key_idx += 1
                raw_pressed_cols = 0
                for col, count in enumerate(autoplay_counts):
                    if count > 0:
                        raw_pressed_cols |= 1 << col

            if not self.replay_key_events and self.replay is not None:
                raw_pressed_cols = 0

            display_pressed_cols = raw_pressed_cols
            key_light_states = [None] * self.key_count
            for col in range(self.key_count):
                bit = 1 << col
                if t_ms <= key_release_until[col]:
                    display_pressed_cols |= bit
                if raw_pressed_cols & bit:
                    age_ms = (
                        max(0.0, t_ms - key_press_start[col])
                        if key_press_start[col] is not None else 0.0
                    )
                    key_light_states[col] = (1.0, 1.0, age_ms)
                elif stage_light_release_start[col] is not None:
                    release_age_ms = t_ms - stage_light_release_start[col]
                    if 0.0 <= release_age_ms < LEGACY_STAGE_LIGHT_FADE_OUT_MS:
                        progress = 1.0 - release_age_ms / LEGACY_STAGE_LIGHT_FADE_OUT_MS
                        age_ms = (
                            max(0.0, t_ms - key_press_start[col])
                            if key_press_start[col] is not None else release_age_ms
                        )
                        key_light_states[col] = (progress, progress, age_ms)

            frame_judgements = []
            while (
                judgement_idx < len(self.judgements)
                and self.judgements[judgement_idx].time <= t_ms
            ):
                judgement = self.judgements[judgement_idx]
                frame_judgements.append(judgement)
                combo = combo + 1 if judgement.value > 0 else 0
                judgement_idx += 1
            while (
                score_idx < len(self.score_progress)
                and self.score_progress[score_idx][0] <= t_ms
            ):
                score = self.score_progress[score_idx][1]
                score_idx += 1
            while (
                acc_idx < len(self.acc_progress)
                and self.acc_progress[acc_idx][0] <= t_ms
            ):
                acc = self.acc_progress[acc_idx][1]
                acc_idx += 1
            while (
                health_idx < len(self.health_progress)
                and self.health_progress[health_idx][0] <= t_ms
            ):
                health = self.health_progress[health_idx][1]
                health_idx += 1

            hold_active_bits = 0
            for hold_col, hold_start_ms, hold_end_ms, _, hold_fall, _, _ in active_holds.values():
                if hold_fall is not None and (hold_fall < 0.0 or t_ms >= hold_fall):
                    continue
                if hold_start_ms <= t_ms <= hold_end_ms:
                    if self.replay is None or (raw_pressed_cols >> hold_col) & 1:
                        hold_active_bits |= 1 << hold_col

            notes = []
            for hold_idx, hold in active_holds.items():
                (
                    col,
                    time_ms,
                    end_time,
                    _,
                    fall_from,
                    _,
                    animation_end_time,
                ) = hold
                if hold_animation_active[hold_idx]:
                    animation_time = min(t_ms, animation_end_time)
                    resumed_at = hold_animation_resumed_at[hold_idx]
                    body_animation_age = hold_animation_age[hold_idx] + max(
                        0.0, animation_time - resumed_at,
                    )
                    if t_ms >= animation_end_time:
                        hold_animation_age[hold_idx] = (
                            freeze_hold_animation_age(body_animation_age)
                        )
                        body_animation_age = hold_animation_age[hold_idx]
                        hold_animation_active[hold_idx] = False
                        hold_animation_resumed_at[hold_idx] = None
                else:
                    body_animation_age = hold_animation_age[hold_idx]
                body_active = hold_animation_active[hold_idx]

                tail_y = self._screen_position(end_time, t_ms)
                natural_head_y = self._screen_position(time_ms, t_ms)
                if fall_from is not None and fall_from < 0.0:
                    # 头部从未命中：整条 LN 按原始滚动公式自由下落
                    head_y = natural_head_y
                elif t_ms < time_ms:
                    head_y = natural_head_y
                elif fall_from is not None and t_ms >= fall_from:
                    # 按住后断体：剩余段（判定线→尾端）自断体时刻整体下落
                    head_y = self.y_judge + self._screen_displacement(
                        fall_from,
                        t_ms,
                    )
                else:
                    # LN 在判定线处被消耗：头钉在线上，尾端到线即整条画完，
                    # 之后不再绘制（晚松键只影响判定时间，不让尾部越线下落）。
                    if t_ms >= end_time:
                        continue
                    head_y = self.y_judge
                    if self.y_judge >= self.y_top:
                        tail_y = min(tail_y, self.y_judge)
                    else:
                        tail_y = max(tail_y, self.y_judge)
                notes.append({
                    "col": col,
                    "y": head_y,
                    "is_hold": True,
                    "tail_y": tail_y,
                    "head_animation_age_ms": max(
                        0.0,
                        t_ms - hold_head_animation_starts[hold_idx],
                    ),
                    "tail_animation_age_ms": max(
                        0.0,
                        t_ms - hold_tail_animation_starts[hold_idx],
                    ),
                    "hold_animation_age_ms": body_animation_age,
                    "hold_animation_started": hold_animation_started[hold_idx],
                    "is_holding": body_active,
                })
            for tap_idx, (col, time_ms, _) in active_taps.items():
                notes.append({
                    "col": col,
                    "y": self._screen_position(time_ms, t_ms),
                    "is_hold": False,
                    "hold_len": 0,
                    "animation_age_ms": max(
                        0.0,
                        t_ms - tap_animation_starts[tap_idx],
                    ),
                })

            while (
                hit_end_idx < len(self.judgements)
                and self.judgements[hit_end_idx].time <= t_ms
            ):
                hit_end_idx += 1
            while (
                hit_start_idx < hit_end_idx
                and self.judgements[hit_start_idx].time <= t_ms - self.effect_window_ms
            ):
                hit_start_idx += 1
            hits = []
            for idx in range(hit_end_idx - 1, hit_start_idx - 1, -1):
                event = self.judgements[idx]
                age_ms = t_ms - event.time
                alpha = (
                    max(0.0, 1.0 - age_ms / self.hit_flash_ms)
                    if self.hit_flash_ms > 0.0 else 0.0
                )
                hits.append((
                    int(event.col),
                    alpha,
                    bool(event.is_hold),
                    age_ms,
                    int(event.value),
                ))

            barlines = []
            for bar_time in active_barlines.values():
                barlines.append(float(self._screen_position(bar_time, t_ms)))

            warning_arrows = []
            for warning_time in active_warnings.values():
                warning_arrows.append(
                    float(self._screen_position(warning_time, t_ms))
                )

            state = {
                "notes": notes,
                "barlines": barlines,
                "warning_arrows": warning_arrows,
                "hits": hits,
                "combo": combo,
                "score": score,
                "acc": acc,
                "health": health,
                "pressed_cols": display_pressed_cols,
                "key_light_states": key_light_states,
                "hit_error_windows": self._hit_error_windows,
                "time_ms": t_ms,
                "animation_time_ms": frame_idx * self.frame_dt_ms,
                "frame": frame_idx,
            }
            state.update(animator.advance(
                t_ms,
                self.frame_dt_ms,
                combo,
                score,
                acc,
                health,
                frame_judgements,
                hold_active_bits,
            ))
            yield state


def iter_frame_states(
    beatmap,
    key_count,
    y_top,
    y_judge,
    fps=None,
    replay=None,
    height=None,
    skin_config=None,
    warning_arrow_height_480=None,
):
    frames = FrameStateIterable(
        beatmap,
        key_count,
        y_top,
        y_judge,
        fps=fps,
        replay=replay,
        height=height,
        skin_config=skin_config,
        warning_arrow_height_480=warning_arrow_height_480,
    )
    return frames, frames.start_song_ms


def build_frame_states(
    beatmap,
    key_count,
    y_top,
    y_judge,
    fps=None,
    replay=None,
    height=None,
    skin_config=None,
    warning_arrow_height_480=None,
):
    """Compatibility wrapper returning a fully materialised frame list."""
    frames, start_song_ms = iter_frame_states(
        beatmap,
        key_count,
        y_top,
        y_judge,
        fps=fps,
        replay=replay,
        height=height,
        skin_config=skin_config,
        warning_arrow_height_480=warning_arrow_height_480,
    )
    return list(frames), start_song_ms
