import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core

MALODY_OSU = (
    "Various Artists - Malody 4K Extra Dan v3-Jack ([GS]hina) "
    "[Extra-7 LiFE Garden (1.05x)  YuzakiNASB].osu"
)


def make_meter_renderer(width=1920, height=1080):
    renderer = object.__new__(core.ManiaRenderer)
    renderer.width = width
    renderer.height = height
    calls = []

    def blend(x1, y1, x2, y2, colour):
        calls.append((x1, y1, x2, y2, colour))

    renderer._blend_rect = blend
    return renderer, calls


class ErrorPropagationTests(unittest.TestCase):
    def test_mark_judgement_carries_error(self):
        events = []
        core._mark_judgement(events, 1000.0, 2, 300, False, -13.5)
        self.assertEqual(events[0].error_ms, -13.5)
        legacy = core.JudgementEvent(1000, 2, 300, False)
        self.assertEqual(legacy.error_ms, 0.0)

    def test_replay_judgements_carry_real_errors(self):
        beatmap = core.parse_osu_file(str(ROOT / "songs" / MALODY_OSU))
        replay = core.parse_osr_file(str(ROOT / "songs" / "test.osr"))
        judgements, windows = core.judge_replay(beatmap, replay, 4)
        hit_events = [ev for ev in judgements if ev.value > 0]
        self.assertTrue(hit_events)
        self.assertTrue(any(ev.error_ms != 0.0 for ev in hit_events))
        tap_events = [ev for ev in hit_events if not ev.is_hold]
        # 早按 MEH(50) 的误差上界是 meh 窗口;判定用四舍五入所以放宽 1ms
        limit = windows["meh"] + 1.0
        self.assertTrue(
            all(abs(ev.error_ms) <= limit for ev in tap_events),
        )

    def test_autoplay_errors_are_zero(self):
        beatmap = core.parse_osu_file(str(ROOT / "songs" / "edm.osu"))
        frames = core.FrameStateIterable(beatmap, 4, 0.0, 480.0, fps=60)
        self.assertTrue(
            all(ev.error_ms == 0.0 for ev in frames.judgements),
        )

    def test_windows_flow_into_frame_states(self):
        beatmap = core.parse_osu_file(str(ROOT / "songs" / "edm.osu"))
        frames = core.FrameStateIterable(beatmap, 4, 0.0, 480.0, fps=60)
        od = beatmap.overall_difficulty
        expected = (
            int(64 - 3 * od),
            int(127 - 3 * od),
            int(151 - 3 * od),
        )
        self.assertEqual(frames._hit_error_windows, expected)
        first = next(iter(frames))
        self.assertEqual(first["hit_error_windows"], expected)


class MeterDrawTests(unittest.TestCase):
    WINDOWS = (40, 103, 127)

    def test_bands_cover_windows_symmetrically(self):
        renderer, calls = make_meter_renderer()
        renderer.draw_hit_error_meter((), 0.0, self.WINDOWS)
        scale = core._legacy_internal_screen_scale()
        half_len = core.LEGACY_HIT_ERROR_BAR_HEIGHT_768 / 2.0 * scale
        px_per_ms = half_len / 127.0
        cx = 1920 / 2.0
        meh_band = calls[0]
        self.assertAlmostEqual(meh_band[0], cx - 127 * px_per_ms, places=6)
        self.assertAlmostEqual(meh_band[2], cx + 127 * px_per_ms, places=6)
        great_band = calls[2]
        self.assertAlmostEqual(great_band[0], cx - 40 * px_per_ms, places=6)
        self.assertEqual(
            great_band[4][:3], core.LEGACY_HIT_ERROR_COLOUR_GREAT,
        )
        self.assertEqual(len(calls), 5)

    def test_tick_position_and_colour(self):
        renderer, calls = make_meter_renderer()
        renderer.draw_hit_error_meter(((60.0, 100.0),), 0.0, self.WINDOWS)
        scale = core._legacy_internal_screen_scale()
        px_per_ms = (core.LEGACY_HIT_ERROR_BAR_HEIGHT_768 / 2.0 * scale) / 127.0
        cx = 1920 / 2.0
        tick = calls[4]
        tick_centre = (tick[0] + tick[2]) / 2.0
        self.assertAlmostEqual(tick_centre, cx + 60 * px_per_ms, places=5)
        self.assertEqual(tick[4][:3], core.LEGACY_HIT_ERROR_COLOUR_OK)
        self.assertAlmostEqual(
            tick[4][3], core.LEGACY_HIT_ERROR_TICK_ALPHA, places=6,
        )

    def test_tick_invisible_at_age_zero(self):
        # lazer 包络:淡入从 0 开始,age=0 时刻度不可见
        renderer, calls = make_meter_renderer()
        renderer.draw_hit_error_meter(((60.0, 0.0),), 0.0, self.WINDOWS)
        self.assertEqual(len(calls), 5)

    def test_average_arrow_follows_value(self):
        renderer, calls = make_meter_renderer()
        renderer.draw_hit_error_meter((), -30.0, self.WINDOWS)
        scale = core._legacy_internal_screen_scale()
        px_per_ms = (core.LEGACY_HIT_ERROR_BAR_HEIGHT_768 / 2.0 * scale) / 127.0
        cx = 1920 / 2.0
        arrow = calls[-1]
        arrow_centre = (arrow[0] + arrow[2]) / 2.0
        self.assertAlmostEqual(arrow_centre, cx - 30 * px_per_ms, places=5)

    def test_disabled_or_missing_windows_draws_nothing(self):
        renderer, calls = make_meter_renderer()
        renderer.draw_hit_error_meter((), 0.0, None)
        self.assertEqual(calls, [])
        core.configure_runtime(show_hit_error=False)
        try:
            renderer.draw_hit_error_meter((), 0.0, self.WINDOWS)
            self.assertEqual(calls, [])
        finally:
            core.configure_runtime(show_hit_error=True)


if __name__ == "__main__":
    unittest.main()
