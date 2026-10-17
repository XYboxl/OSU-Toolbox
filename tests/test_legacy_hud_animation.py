import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


def hit(time_ms, value=320, col=0):
    return SimpleNamespace(time=float(time_ms), col=col, value=value, is_hold=False)


def make_animator(**overrides):
    kwargs = dict(
        key_count=4,
        start_ms=0.0,
        first_hit_ms=0.0,
        last_hit_ms=10000.0,
        breaks=(),
        letterbox=False,
        collect_hit_errors=False,
    )
    kwargs.update(overrides)
    return core.LegacyHudAnimator(**kwargs)


class RollingCounterTests(unittest.TestCase):
    def test_score_rolls_1000ms_outquad(self):
        anim = make_animator()
        first = anim.advance(0.0, 16.7, 0, 1000, 100.0, 1.0, [hit(0)], 0)
        self.assertEqual(first["score_display"], 0)
        mid = anim.advance(500.0, 16.7, 0, 1000, 100.0, 1.0, [], 0)
        self.assertEqual(mid["score_display"], 750)
        done = anim.advance(1000.0, 16.7, 0, 1000, 100.0, 1.0, [], 0)
        self.assertEqual(done["score_display"], 1000)

    def test_accuracy_rolls_375ms(self):
        anim = make_animator()
        anim.advance(0.0, 16.7, 0, 0, 90.0, 1.0, [hit(0)], 0)
        mid = anim.advance(187.5, 16.7, 0, 0, 90.0, 1.0, [], 0)
        self.assertAlmostEqual(mid["acc_display"], 100.0 - 10.0 * 0.75, places=6)
        done = anim.advance(375.0, 16.7, 0, 0, 90.0, 1.0, [], 0)
        self.assertAlmostEqual(done["acc_display"], 90.0, places=6)

    def test_rolling_restarts_from_current_display_value(self):
        roll = core._RollingValue(0.0, 1000.0)
        roll.set_target(1000.0, 0.0)
        halfway = roll.value_at(500.0)
        roll.set_target(2000.0, 500.0)
        self.assertAlmostEqual(roll.value_at(500.0), halfway, places=6)


class ComboAnimationTests(unittest.TestCase):
    def test_combo_increment_sets_pop_age(self):
        anim = make_animator()
        state = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [hit(0)], 0)
        self.assertEqual(state["combo_display"], 1)
        self.assertEqual(state["combo_pop_age_ms"], 0.0)
        state = anim.advance(100.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertEqual(state["combo_pop_age_ms"], 100.0)
        state = anim.advance(400.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertEqual(state["combo_pop_age_ms"], -1.0)

    def test_combo_break_emits_ghost_and_rolls_down(self):
        anim = make_animator()
        anim.advance(0.0, 16.7, 5, 0, 100.0, 1.0, [hit(0)], 0)
        broke = anim.advance(100.0, 16.7, 0, 0, 100.0, 1.0, [hit(100, value=0)], 0)
        self.assertEqual(broke["combo_ghost_value"], 5)
        self.assertEqual(broke["combo_ghost_age_ms"], 0.0)
        self.assertEqual(broke["combo_display"], 5)
        halfway = anim.advance(150.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertEqual(halfway["combo_display"], round(5 * 0.5))
        done = anim.advance(200.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertEqual(done["combo_display"], 0)
        faded = anim.advance(320.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertEqual(faded["combo_ghost_value"], 0)
        self.assertEqual(faded["combo_ghost_age_ms"], -1.0)

    def test_combo_increment_during_roll_cancels_roll(self):
        anim = make_animator()
        anim.advance(0.0, 16.7, 10, 0, 100.0, 1.0, [hit(0)], 0)
        anim.advance(50.0, 16.7, 0, 0, 100.0, 1.0, [hit(50, value=0)], 0)
        resumed = anim.advance(80.0, 16.7, 1, 0, 100.0, 1.0, [hit(80)], 0)
        self.assertEqual(resumed["combo_display"], 1)
        self.assertEqual(resumed["combo_pop_age_ms"], 0.0)

    def test_combo_holding_reflects_hold_bits(self):
        anim = make_animator()
        held = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [hit(0)], 0b0010)
        self.assertTrue(held["combo_holding"])
        released = anim.advance(50.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertFalse(released["combo_holding"])


class HealthAnimationTests(unittest.TestCase):
    def setUp(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_LAZER_CLASSIC,
        )

    def tearDown(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
        )

    def test_health_rise_uses_stable_frame_multiplier_and_divisor(self):
        for fps in (30.0, 60.0, 120.0):
            with self.subTest(fps=fps):
                dt = 1000.0 / fps
                anim = make_animator()
                anim._health_intro_active = False
                multiplier = dt / core.LEGACY_FRAME_INTERVAL_MS
                expected = min(1.0, multiplier / core.LEGACY_HEALTH_RISE_DIVISOR)
                state = anim.advance(0.0, dt, 0, 0, 100.0, 1.0, [], 0)
                self.assertAlmostEqual(state["health_display"], expected, places=9)
                expected += (
                    (1.0 - expected)
                    / core.LEGACY_HEALTH_RISE_DIVISOR
                    * multiplier
                )
                state = anim.advance(dt, dt, 0, 0, 100.0, 1.0, [], 0)
                self.assertAlmostEqual(state["health_display"], expected, places=9)

    def test_health_fall_uses_stable_frame_multiplier_and_divisor(self):
        for fps in (30.0, 60.0, 120.0):
            with self.subTest(fps=fps):
                dt = 1000.0 / fps
                anim = make_animator()
                anim._health_intro_active = False
                anim.health_display = 1.0
                multiplier = dt / core.LEGACY_FRAME_INTERVAL_MS
                expected = 1.0 - multiplier / core.LEGACY_HEALTH_FALL_DIVISOR
                state = anim.advance(0.0, dt, 0, 0, 100.0, 0.0, [], 0)
                self.assertAlmostEqual(state["health_display"], expected, places=9)

    def test_marker_bulge_settles_at_0_8(self):
        anim = make_animator()
        anim._health_intro_active = False
        start = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [hit(0)], 0)
        self.assertAlmostEqual(start["health_marker_scale"], 1.2, places=6)
        mid = anim.advance(75.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(mid["health_marker_scale"], 1.0, places=6)
        settled = anim.advance(1000.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(settled["health_marker_scale"], 0.8, places=6)

    def test_flash_epic_threshold_is_half(self):
        anim = make_animator()
        state = anim.advance(0.0, 16.7, 1, 0, 100.0, 0.6, [hit(0)], 0)
        self.assertEqual(state["health_flash_age_ms"], 0.0)
        self.assertTrue(state["health_flash_epic"])
        anim2 = make_animator()
        state2 = anim2.advance(0.0, 16.7, 1, 0, 100.0, 0.3, [hit(0)], 0)
        self.assertFalse(state2["health_flash_epic"])

    def test_intro_climbs_continuously_without_flash(self):
        anim = make_animator(start_ms=0.0, first_hit_ms=5000.0)
        first = anim.advance(0.0, 100.0, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(first["health_display"], 0.01, places=9)
        self.assertEqual(first["health_flash_age_ms"], -1.0)
        self.assertAlmostEqual(first["health_marker_scale"], 1.2, places=6)
        second = anim.advance(100.0, 50.0, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(second["health_display"], 0.015, places=9)
        self.assertEqual(second["health_flash_age_ms"], -1.0)
        restarted = anim.advance(150.0, 50.0, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(restarted["health_display"], 0.02, places=9)
        self.assertAlmostEqual(restarted["health_marker_scale"], 1.2, places=6)

    def test_first_judgement_does_not_end_intro_growth(self):
        anim = make_animator(start_ms=0.0, first_hit_ms=5000.0)
        anim.advance(0.0, 100.0, 0, 0, 100.0, 1.0, [], 0)
        judged = anim.advance(100.0, 100.0, 1, 0, 100.0, 1.0, [hit(100)], 0)
        self.assertTrue(anim._health_intro_active)
        self.assertAlmostEqual(judged["health_display"], 0.02, places=9)
        later = anim.advance(200.0, 100.0, 1, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(later["health_display"], 0.03, places=9)

    def test_falling_health_disables_intro_then_uses_rise_tracking(self):
        anim = make_animator(start_ms=0.0, first_hit_ms=5000.0)
        anim.health_display = 0.5
        fallen = anim.advance(
            0.0, core.LEGACY_FRAME_INTERVAL_MS,
            0, 0, 100.0, 0.2, [], 0,
        )
        self.assertFalse(anim._health_intro_active)
        expected_fall = 0.5 - 0.3 / core.LEGACY_HEALTH_FALL_DIVISOR
        self.assertAlmostEqual(fallen["health_display"], expected_fall, places=9)
        risen = anim.advance(
            core.LEGACY_FRAME_INTERVAL_MS,
            core.LEGACY_FRAME_INTERVAL_MS,
            0, 0, 100.0, 1.0, [], 0,
        )
        expected_rise = expected_fall + (
            1.0 - expected_fall
        ) / core.LEGACY_HEALTH_RISE_DIVISOR
        self.assertAlmostEqual(risen["health_display"], expected_rise, places=9)

    def test_stable_mania_disables_intro_bulge_and_flash(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
        )
        anim = make_animator(start_ms=0.0, first_hit_ms=5000.0)
        state = anim.advance(
            0.0,
            core.LEGACY_FRAME_INTERVAL_MS,
            1,
            0,
            100.0,
            1.0,
            [hit(0)],
            0,
        )
        self.assertFalse(anim._health_intro_active)
        self.assertAlmostEqual(
            state["health_display"],
            1.0 / core.LEGACY_HEALTH_RISE_DIVISOR,
            places=9,
        )
        self.assertEqual(state["health_marker_scale"], 1.0)
        self.assertEqual(state["health_flash_age_ms"], -1.0)
        self.assertFalse(state["health_flash_epic"])


class ProgressAndOverlayTests(unittest.TestCase):
    def test_progress_intro_then_gameplay(self):
        anim = make_animator(start_ms=-2000.0, first_hit_ms=1000.0, last_hit_ms=11000.0)
        early = anim.advance(-2000.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertTrue(early["progress_is_intro"])
        self.assertAlmostEqual(early["progress"], -1.0, places=6)
        mid_intro = anim.advance(-500.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(mid_intro["progress"], -0.5, places=6)
        start = anim.advance(1000.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertFalse(start["progress_is_intro"])
        self.assertAlmostEqual(start["progress"], 0.0, places=6)
        mid = anim.advance(6000.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(mid["progress"], 0.5, places=6)
        end = anim.advance(12000.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(end["progress"], 1.0, places=6)

    def test_progress_clamps_to_stable_signed_range(self):
        anim = make_animator(
            start_ms=-2000.0, first_hit_ms=1000.0, last_hit_ms=11000.0,
        )
        before = anim.advance(-10000.0, 0.0, 0, 0, 100.0, 1.0, [], 0)
        self.assertEqual(before["progress"], -2.0)
        after = anim.advance(50000.0, 0.0, 0, 0, 100.0, 1.0, [], 0)
        self.assertEqual(after["progress"], 1.0)

    def test_letterbox_envelope_inside_break(self):
        anim = make_animator(breaks=((1000.0, 4000.0),), letterbox=True)
        outside = anim.advance(500.0, 16.7, 0, 0, 100.0, 1.0, [hit(0)], 0)
        self.assertEqual(outside["letterbox"], 0.0)
        fading_in = anim.advance(1100.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(fading_in["letterbox"], 0.5, places=6)
        full = anim.advance(2500.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(full["letterbox"], 1.0, places=6)
        fading_out = anim.advance(3900.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertAlmostEqual(fading_out["letterbox"], 0.5, places=6)
        after = anim.advance(4100.0, 16.7, 0, 0, 100.0, 1.0, [], 0)
        self.assertEqual(after["letterbox"], 0.0)

    def test_short_break_is_ignored(self):
        anim = make_animator(breaks=((1000.0, 1200.0),), letterbox=True)
        self.assertEqual(anim.breaks, ())
        inside = anim.advance(1100.0, 16.7, 0, 0, 100.0, 1.0, [hit(0)], 0)
        self.assertEqual(inside["letterbox"], 0.0)

class HoldLightAndErrorTests(unittest.TestCase):
    def test_hold_light_phases(self):
        anim = make_animator()
        held = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [hit(0)], 0b0001)
        self.assertEqual(held["hold_light_states"][0], ("hold", 0.0))
        still = anim.advance(100.0, 16.7, 1, 0, 100.0, 1.0, [], 0b0001)
        self.assertEqual(still["hold_light_states"][0], ("hold", 100.0))
        released = anim.advance(200.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertEqual(released["hold_light_states"][0], ("release", 0.0, 200.0))
        fading = anim.advance(300.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertEqual(fading["hold_light_states"][0], ("release", 100.0, 300.0))
        gone = anim.advance(340.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertIsNone(gone["hold_light_states"][0])
        self.assertIsNone(held["hold_light_states"][1])

    def test_hit_errors_collected_with_smoothed_average(self):
        anim = make_animator(collect_hit_errors=True)
        event = hit(0)
        event.error_ms = -12.0
        state = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [event], 0)
        self.assertEqual(state["hit_errors"], ((-12.0, 0.0),))
        self.assertAlmostEqual(state["hit_error_avg"], -1.2, places=9)
        later = anim.advance(50.0, 16.7, 1, 0, 100.0, 1.0, [], 0)
        self.assertEqual(later["hit_errors"], ((-12.0, 50.0),))

    def test_hit_errors_disabled_stay_empty(self):
        anim = make_animator(collect_hit_errors=False)
        event = hit(0)
        event.error_ms = 10.0
        state = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [event], 0)
        self.assertEqual(state["hit_errors"], ())
        self.assertEqual(state["hit_error_avg"], 0.0)


class FrameStateIntegrationTests(unittest.TestCase):
    def test_iter_frame_states_carries_animator_fields(self):
        beatmap = core.parse_osu_file(str(ROOT / "songs" / "edm.osu"))
        frame_states, _ = core.iter_frame_states(
            beatmap, 4, 0.0, 480.0, fps=30, replay=None,
        )
        required = {
            "score_display", "acc_display", "combo_display",
            "combo_pop_age_ms", "combo_ghost_value", "combo_ghost_age_ms",
            "combo_holding", "health_display", "health_marker_scale",
            "health_flash_age_ms", "health_flash_epic", "progress",
            "progress_is_intro", "letterbox", "warning_arrows",
            "hold_light_states", "hit_errors", "hit_error_avg",
        }
        seen_gameplay = False
        for index, state in enumerate(frame_states):
            self.assertTrue(required.issubset(state.keys()))
            self.assertGreaterEqual(state["health_display"], 0.0)
            self.assertLessEqual(state["health_display"], 1.0)
            self.assertLessEqual(state["score_display"], max(state["score"], 1))
            if not state["progress_is_intro"]:
                seen_gameplay = True
            if index >= 240:
                break
        self.assertTrue(seen_gameplay)


if __name__ == "__main__":
    unittest.main()
