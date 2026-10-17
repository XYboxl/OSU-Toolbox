"""osu!mania 判定、分数、准确率、血条与 life graph。"""

import math
from dataclasses import dataclass

from ..parsing.osu_file import HitObject
from .mods import (
    MOD_DOUBLE_TIME,
    MOD_EASY,
    MOD_FADE_IN,
    MOD_FLASHLIGHT,
    MOD_HALF_TIME,
    MOD_HARD_ROCK,
    MOD_NIGHTCORE,
    MOD_SCORE_V2,
)

HIT_VALUE_TO_COUNT = {
    320: "geki",
    300: "300",
    200: "katu",
    100: "100",
    50: "50",
    0: "miss",
}


@dataclass
class KeyEvent:
    time: int
    col: int
    down: bool
    seq: int


@dataclass
class JudgementEvent:
    time: int
    col: int
    value: int
    is_hold: bool
    error_ms: float = 0.0
    # LN 视觉元数据：头部是否被有效命中；断体（提前松键）时刻，-1 表示未断体
    hold_head_hit: bool = True
    hold_drop_ms: float = -1.0
    hold_head_time_ms: float = None


@dataclass
class ActiveHold:
    obj: HitObject
    head_error: int
    body_broken: bool = False
    holding: bool = True
    break_time: float = -1.0
    head_time_ms: float = None


def _round_abs_ms(value):
    return int(math.floor(abs(value) + 0.5))


def mania_hit_windows(od, score_v2=False):
    if score_v2:
        perfect = 22.4 - 0.6 * od if od <= 5 else 24.9 - 1.1 * od
    else:
        perfect = 16
    return {
        "perfect": int(math.floor(perfect)),
        "great": int(math.floor(64 - 3 * od)),
        "good": int(math.floor(97 - 3 * od)),
        "ok": int(math.floor(127 - 3 * od)),
        "meh": int(math.floor(151 - 3 * od)),
        "miss": int(math.floor(188 - 3 * od)),
    }


def judge_tap_error(error_ms, windows):
    rounded = _round_abs_ms(error_ms)
    if rounded <= windows["perfect"]:
        return 320
    if rounded <= windows["great"]:
        return 300
    if rounded <= windows["good"]:
        return 200
    if rounded <= windows["ok"]:
        return 100
    if error_ms < 0 and rounded <= windows["meh"]:
        return 50
    return 0


def judge_hold_error(head_error, tail_error, body_broken, windows):
    head = _round_abs_ms(head_error)
    tail = _round_abs_ms(tail_error)
    total = head + tail
    if head <= windows["perfect"] * 1.2 and total <= windows["perfect"] * 2.4:
        value = 320
    elif head <= windows["great"] * 1.1 and total <= windows["great"] * 2.2:
        value = 300
    elif head <= windows["good"] and total <= windows["good"] * 2:
        value = 200
    elif head <= windows["ok"] and total <= windows["ok"] * 2:
        value = 100
    else:
        value = 50
    if body_broken:
        value = min(value, 50)
    return value


def replay_key_events(replay, key_count):
    events = []
    mask = (1 << key_count) - 1
    prev = 0
    for frame in replay.frames:
        keys = int(frame.keys) & mask
        changed = keys ^ prev
        if changed:
            for col in range(key_count):
                bit = 1 << col
                if changed & bit:
                    events.append(KeyEvent(frame.time, col, bool(keys & bit), frame.seq))
        prev = keys
    events.sort(key=lambda e: (e.time, e.seq, 0 if e.down else 1, e.col))
    return events


def _mark_judgement(
    events,
    time_ms,
    col,
    value,
    is_hold,
    error_ms=0.0,
    hold_head_hit=True,
    hold_drop_ms=-1.0,
    hold_head_time_ms=None,
):
    events.append(JudgementEvent(
        int(round(time_ms)),
        col,
        value,
        is_hold,
        float(error_ms),
        bool(hold_head_hit),
        float(hold_drop_ms),
        None if hold_head_time_ms is None else float(hold_head_time_ms),
    ))


def judge_replay(beatmap, replay, key_count):
    score_v2 = bool(replay.mods & MOD_SCORE_V2)
    windows = mania_hit_windows(beatmap.overall_difficulty, score_v2)
    by_col = [[] for _ in range(key_count)]
    for obj in beatmap.hit_objects:
        if 0 <= obj.col < key_count:
            by_col[obj.col].append(obj)

    key_events = replay_key_events(replay, key_count)
    index = [0] * key_count
    active = [None] * key_count
    judgements = []

    def timeout_until(now):
        for col in range(key_count):
            hold = active[col]
            if hold is not None and hold.obj.end_time + windows["ok"] < now:
                _mark_judgement(
                    judgements,
                    hold.obj.end_time + windows["ok"],
                    col,
                    0,
                    True,
                    hold_drop_ms=hold.break_time if hold.body_broken else -1.0,
                    hold_head_time_ms=hold.head_time_ms,
                )
                active[col] = None
                index[col] += 1
            while active[col] is None and index[col] < len(by_col[col]):
                obj = by_col[col][index[col]]
                deadline = obj.time + windows["ok"]
                if deadline >= now:
                    break
                # 头部从未被有效命中（含按得太晚）：LN 全程自由下落
                _mark_judgement(
                    judgements, deadline, col, 0, obj.is_hold,
                    hold_head_hit=False,
                )
                index[col] += 1

    for ev in key_events:
        timeout_until(ev.time)
        col = ev.col
        hold = active[col]

        if hold is not None:
            if ev.down:
                hold.holding = True
                continue
            tail_error = ev.time - hold.obj.end_time
            if ev.time < hold.obj.end_time - windows["meh"]:
                if not hold.body_broken:
                    hold.break_time = float(ev.time)
                hold.body_broken = True
                hold.holding = False
                continue
            if ev.time > hold.obj.end_time + windows["ok"]:
                value = 0
            else:
                value = judge_hold_error(hold.head_error, tail_error, hold.body_broken, windows)
            _mark_judgement(
                judgements, ev.time, col, value, True, tail_error,
                hold_drop_ms=hold.break_time if hold.body_broken else -1.0,
                hold_head_time_ms=hold.head_time_ms,
            )
            active[col] = None
            index[col] += 1
            continue

        if not ev.down:
            continue

        while index[col] < len(by_col[col]):
            obj = by_col[col][index[col]]
            if ev.time < obj.time - windows["miss"]:
                break
            if ev.time > obj.time + windows["ok"]:
                _mark_judgement(judgements, obj.time + windows["ok"], col, 0, obj.is_hold)
                index[col] += 1
                continue
            if obj.is_hold:
                active[col] = ActiveHold(
                    obj,
                    ev.time - obj.time,
                    head_time_ms=float(ev.time),
                )
            else:
                value = judge_tap_error(ev.time - obj.time, windows)
                _mark_judgement(
                    judgements, ev.time, col, value, False, ev.time - obj.time,
                )
                index[col] += 1
            break

    end_time = 0
    if beatmap.hit_objects:
        end_time = max(o.end_time if o.is_hold else o.time for o in beatmap.hit_objects)
    if replay.frames:
        end_time = max(end_time, max(f.time for f in replay.frames))
    timeout_until(end_time + windows["ok"] + 1)
    judgements.sort(key=lambda e: (e.time, e.col, e.value))
    return judgements, windows


def _judgement_count_summary(judgements):
    counts = {"300": 0, "100": 0, "50": 0, "geki": 0, "katu": 0, "miss": 0}
    for ev in judgements:
        counts[HIT_VALUE_TO_COUNT.get(ev.value, "miss")] += 1
    return counts


def _mania_mod_multiplier(mods):
    mult = 1.0
    if mods & MOD_EASY:
        mult *= 0.5
    if mods & MOD_HALF_TIME:
        mult *= 0.5
    return mult


def _mania_mod_divider(mods):
    divider = 1.0
    if mods & MOD_HARD_ROCK:
        divider *= 1.08
    if mods & (MOD_DOUBLE_TIME | MOD_NIGHTCORE):
        divider *= 1.1
    if mods & MOD_FADE_IN:
        divider *= 1.06
    if mods & MOD_FLASHLIGHT:
        divider *= 1.06
    return divider


def mania_score_progress(judgements, object_count, mods):
    """Stable osu!mania ScoreV1 progress."""
    if object_count <= 0:
        return []
    hit_value = {320: 320, 300: 300, 200: 200, 100: 100, 50: 50, 0: 0}
    hit_bonus_value = {320: 32, 300: 32, 200: 16, 100: 8, 50: 4, 0: 0}
    hit_bonus = {320: 2, 300: 1, 200: 0, 100: 0, 50: 0, 0: 0}
    hit_punishment = {320: 0, 300: 0, 200: 8, 100: 24, 50: 44, 0: 999999}
    mod_multiplier = _mania_mod_multiplier(mods)
    mod_divider = _mania_mod_divider(mods)

    score = 0.0
    bonus = 100.0
    out = []
    for ev in judgements:
        value = ev.value
        bonus = min(100.0, max(0.0, bonus + hit_bonus[value] - hit_punishment[value] / mod_divider))
        base_score = hit_value[value] * 1000000.0 / 320.0 / object_count * 0.5
        bonus_score = hit_bonus_value[value] * math.sqrt(bonus) * 1000000.0 / 320.0 / object_count * 0.5
        score += (base_score + bonus_score) * mod_multiplier
        out.append((ev.time, int(round(score))))
    return out


def mania_lazer_score_progress(judgements, object_count, mods=0):
    """Current lazer osu!mania normalised score, using one result per parsed object."""
    if object_count <= 0:
        return []

    accuracy_value = {320: 305.0, 300: 300.0, 200: 200.0, 100: 100.0, 50: 50.0, 0: 0.0}
    combo_value = {320: 300.0, 300: 300.0, 200: 200.0, 100: 100.0, 50: 50.0, 0: 0.0}
    min_combo_factor = 0.5
    max_combo_factor = math.log(400.0, 4.0)

    maximum_combo_portion = 0.0
    for combo_after in range(1, object_count + 1):
        factor = min(max(min_combo_factor, math.log(combo_after, 4.0)), max_combo_factor)
        maximum_combo_portion += 300.0 * factor

    combo = 0
    judged = 0
    current_combo_portion = 0.0
    current_accuracy_score = 0.0
    out = []
    for ev in judgements:
        judged += 1
        if ev.value > 0:
            combo += 1
        else:
            combo = 0

        if combo > 0:
            combo_factor = min(max(min_combo_factor, math.log(combo, 4.0)), max_combo_factor)
        else:
            combo_factor = min_combo_factor
        current_combo_portion += combo_value.get(ev.value, 0.0) * combo_factor
        current_accuracy_score += accuracy_value.get(ev.value, 0.0)

        accuracy = current_accuracy_score / (305.0 * judged)
        combo_progress = current_combo_portion / maximum_combo_portion if maximum_combo_portion > 0 else 1.0
        accuracy_progress = judged / float(object_count)
        score = (
            150000.0 * combo_progress
            + 850000.0 * math.pow(accuracy, 2.0 + 2.0 * accuracy) * accuracy_progress
        )
        out.append((ev.time, int(round(score))))
    return out


def mania_accuracy_progress(judgements, mods=0):
    score_v2 = bool(mods & MOD_SCORE_V2)
    max_value = 305.0 if score_v2 else 300.0
    hit_value = {320: 305.0 if score_v2 else 300.0, 300: 300.0, 200: 200.0, 100: 100.0, 50: 50.0, 0: 0.0}
    total = 0.0
    judged = 0
    out = []
    for ev in judgements:
        judged += 1
        total += hit_value.get(ev.value, 0.0)
        out.append((ev.time, 100.0 * total / (max_value * judged)))
    return out


def _judgements_by_object(objects, judgements, key_count):
    by_col = [[] for _ in range(key_count)]
    for ev in judgements:
        if 0 <= ev.col < key_count:
            by_col[ev.col].append(ev)

    col_pos = [0] * key_count
    out = {}
    for obj in objects:
        if not (0 <= obj.col < key_count):
            continue
        pos = col_pos[obj.col]
        if pos < len(by_col[obj.col]):
            out[id(obj)] = by_col[obj.col][pos]
            col_pos[obj.col] += 1
    return out


def _difficulty_range(difficulty, minimum, midpoint, maximum):
    difficulty = float(difficulty)
    if difficulty > 5.0:
        return midpoint + (maximum - midpoint) * ((difficulty - 5.0) / 5.0)
    if difficulty < 5.0:
        return midpoint + (midpoint - minimum) * ((difficulty - 5.0) / 5.0)
    return midpoint


def _mania_positive_health(value, hp_drain_rate, multiplier):
    if value >= 320:
        increase = 0.0055 - hp_drain_rate * 0.0005
    elif value >= 300:
        increase = 0.0050 - hp_drain_rate * 0.0005
    elif value >= 200:
        increase = 0.0040 - hp_drain_rate * 0.0004
    else:
        increase = 0.0
    return multiplier * increase


def _mania_hp_multiplier(objects, hp_drain_rate):
    """Port LegacyDrainingHealthProcessor's recovery iteration to the parsed object model."""
    objects = sorted(objects, key=lambda obj: obj.time)
    if not objects:
        return 1.0

    lowest_hp_ever = _difficulty_range(hp_drain_rate, 0.975, 0.8, 0.3)
    lowest_hp_end = _difficulty_range(hp_drain_rate, 0.99, 0.9, 0.4)
    recovery_required = _difficulty_range(hp_drain_rate, 0.04, 0.02, 0.0)
    test_drop = 0.00025
    multiplier = 1.0

    # The local parser stores one aggregate judgement per LN. Nested head/tail
    # judgements are therefore intentionally omitted from this legacy iteration.
    for _ in range(20000):
        current_hp = 1.0
        uncapped_hp = 1.0
        last_time = float(objects[0].time)
        failed = False

        for obj in objects:
            gap_drop = test_drop * max(0.0, float(obj.time) - last_time)
            current_hp = max(0.0, current_hp - gap_drop)
            uncapped_hp = max(0.0, uncapped_hp - gap_drop)
            last_time = float(obj.end_time if obj.is_hold else obj.time)

            if current_hp <= lowest_hp_ever:
                test_drop *= 0.96
                failed = True
                break

            duration = max(0.0, float(obj.end_time - obj.time)) if obj.is_hold else 0.0
            hp_reduction = test_drop * duration
            hp_overkill = max(0.0, hp_reduction - current_hp)
            current_hp = max(0.0, current_hp - hp_reduction)
            uncapped_hp = max(0.0, uncapped_hp - hp_reduction)

            if hp_overkill > 0.0 and current_hp - hp_overkill <= lowest_hp_ever:
                test_drop *= 0.96
                failed = True
                break

            increase = _mania_positive_health(320, hp_drain_rate, multiplier)
            uncapped_hp += increase
            current_hp = max(0.0, min(1.0, current_hp + increase))

        if not failed and current_hp < lowest_hp_end:
            test_drop *= 0.94
            multiplier *= 1.01
            failed = True

        recovery = (uncapped_hp - 1.0) / max(1, len(objects))
        if not failed and recovery < recovery_required:
            test_drop *= 0.96
            multiplier *= 1.01
            failed = True

        if not failed:
            return multiplier
        if not math.isfinite(multiplier):
            return 1.0

    return multiplier


def _health_after_judgement(health, value, hp_drain_rate, hp_multiplier):
    if value >= 200:
        delta = _mania_positive_health(value, hp_drain_rate, hp_multiplier)
    elif value >= 100:
        delta = 0.0
    elif value >= 50:
        delta = -(hp_drain_rate + 1.0) * 0.0016
    else:
        # LN results are aggregate in this renderer, so a failed LN uses the
        # top-level miss penalty rather than separate head/tail miss penalties.
        delta = -(hp_drain_rate + 1.0) * 0.0075
    return max(0.0, min(1.0, health + delta))


def mania_health_progress(objects, judgements, hp_drain_rate):
    multiplier = _mania_hp_multiplier(objects, hp_drain_rate)
    health = 1.0
    out = []
    for ev in judgements:
        health = _health_after_judgement(health, ev.value, hp_drain_rate, multiplier)
        out.append((float(ev.time), health))
    return out


def parse_life_graph(value):
    points = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw or "|" not in raw:
            continue
        time_text, health_text = raw.split("|", 1)
        try:
            time_ms = float(time_text)
            health = max(0.0, min(1.0, float(health_text)))
        except ValueError:
            continue
        if math.isfinite(time_ms) and math.isfinite(health):
            points.append((time_ms, health))
    if not points:
        return []
    points.sort(key=lambda point: point[0])
    deduped = []
    for point in points:
        if deduped and point[0] == deduped[-1][0]:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped
