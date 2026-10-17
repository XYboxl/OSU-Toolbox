import unittest
from types import SimpleNamespace

import osu_toolbox as core
from osu_toolbox.parsing.osu_file import _parse_timing_point


class TimingPointParsingTests(unittest.TestCase):
    def test_effects_field_is_preserved(self):
        point = _parse_timing_point("123,500,4,2,0,70,1,9")
        self.assertEqual(point.effects, 9)

    def test_invalid_effects_falls_back_to_zero(self):
        point = _parse_timing_point("123,500,4,2,0,70,1,invalid")
        self.assertEqual(point.effects, 0)


class LegacyScrollPathTests(unittest.TestCase):
    def setUp(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
            scroll_speed=20,
            scroll_speed_mult=1.0,
            scroll_ar_override=None,
        )

    @staticmethod
    def _point(time, beat_length, meter=4, effects=0):
        return core.TimingPoint(
            float(time),
            float(beat_length),
            int(meter),
            beat_length < 0,
            int(effects),
        )

    def test_first_timing_point_extends_two_measures_before_zero(self):
        path = core.LegacyScrollPath(
            [self._point(3000, 500)],
            8000,
            20,
        )
        self.assertEqual(path.first_timing_time, -3000.0)

    def test_fixed_speed_is_normalised_by_dominant_bpm(self):
        slow = core.LegacyScrollPath(
            [self._point(0, 600)],
            5000,
            28,
        )
        fast = core.LegacyScrollPath(
            [self._point(0, 300)],
            5000,
            28,
        )
        expected_rate = 21.0 * 28.0 * 100.0 / 60000.0
        self.assertAlmostEqual(slow.rate_at(1000), expected_rate)
        self.assertAlmostEqual(fast.rate_at(1000), expected_rate)

    def test_inherited_sv_is_integrated_across_segments(self):
        path = core.LegacyScrollPath(
            [
                self._point(0, 500),
                self._point(1000, -50),
            ],
            4000,
            20,
            bpm_scaled=True,
        )
        self.assertEqual(
            [(segment.time, segment.beat_length) for segment in path.segments],
            [(-4000.0, 500.0), (1000.0, 250.0)],
        )
        expected = 500.0 * (21.0 * 20.0 / 500.0)
        expected += 500.0 * (21.0 * 20.0 / 250.0)
        self.assertAlmostEqual(path.displacement(500.0, 1500.0), expected)

    def test_mania_sv_multiplier_uses_ten_to_ten_thousand_clamp(self):
        path = core.LegacyScrollPath(
            [
                self._point(0, 500),
                self._point(1000, -5),
                self._point(2000, -20000),
            ],
            4000,
            20,
        )
        self.assertEqual(
            [segment.beat_length for segment in path.segments],
            [500.0, 50.0, 50000.0],
        )

    def test_inverse_time_matches_raw_position_across_sv_boundary(self):
        path = core.LegacyScrollPath(
            [
                self._point(0, 500),
                self._point(1000, -50),
            ],
            4000,
            20,
            bpm_scaled=True,
        )
        target = 1500.0
        entry = path.time_for_raw_position(target, 0.0, 420.0)
        self.assertAlmostEqual(path.raw_position(target, entry, 420.0), 0.0)


class LegacyBarlineTests(unittest.TestCase):
    @staticmethod
    def _point(time, beat_length, meter=4, effects=0):
        return core.TimingPoint(
            float(time),
            float(beat_length),
            int(meter),
            beat_length < 0,
            int(effects),
        )

    def test_omit_first_barline_only_skips_redline_boundary(self):
        times = core.iter_measure_barline_times(
            [
                self._point(0, 500),
                self._point(2000, 250, meter=3, effects=8),
            ],
            -5000,
            4000,
        )
        self.assertEqual(times, [-4000.0, -2000.0, 0.0, 2750.0, 3500.0])

    def test_inherited_points_do_not_start_new_measures(self):
        times = core.iter_measure_barline_times(
            [
                self._point(0, 500),
                self._point(750, -50),
            ],
            0,
            4500,
        )
        self.assertEqual(times, [0.0, 2000.0, 4000.0])

    def test_positive_height_barline_uses_centre_entry_and_full_lifetime(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
            scroll_speed=20,
            scroll_speed_mult=1.0,
            scroll_ar_override=5.0,
            pre_roll_ms=4000.0,
            show_barlines=True,
            show_warning_arrow=False,
        )
        beatmap = core.Beatmap(
            mode=3,
            circle_size=4.0,
            approach_rate=5.0,
            overall_difficulty=5.0,
            hp_drain_rate=5.0,
            audio_lead_in=0,
            audio_filename="",
            title="",
            artist="",
            version="",
            hit_objects=[core.HitObject(0, 5000)],
            timing_points=[self._point(0, 500)],
        )
        for y_top, y_judge, entry_y, exit_y in (
            (0.0, 420.0, 0.0, 491.75),
            (480.0, 60.0, 480.0, -11.75),
        ):
            with self.subTest(y_top=y_top, y_judge=y_judge):
                frames = core.FrameStateIterable(
                    beatmap,
                    4,
                    y_top,
                    y_judge,
                    fps=1000,
                    skin_config=SimpleNamespace(barline_height=12.0),
                )
                wanted = {799.0, 800.0, 2205.0, 2206.0}
                states = {}
                for state in frames:
                    time_ms = round(state["time_ms"], 6)
                    if time_ms in wanted:
                        states[time_ms] = state
                    if time_ms > max(wanted):
                        break

                self.assertEqual(states[799.0]["barlines"], [])
                self.assertEqual(len(states[800.0]["barlines"]), 1)
                self.assertAlmostEqual(
                    states[800.0]["barlines"][0],
                    entry_y,
                )
                self.assertAlmostEqual(
                    states[2205.0]["barlines"][0],
                    exit_y,
                )
                self.assertEqual(states[2206.0]["barlines"], [])


class LegacyFrameScrollSynchronizationTests(unittest.TestCase):
    @staticmethod
    def _point(time, beat_length, meter=4, effects=0):
        return core.TimingPoint(
            float(time),
            float(beat_length),
            int(meter),
            beat_length < 0,
            int(effects),
        )

    def setUp(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
            scroll_speed=1,
            scroll_speed_mult=1.0,
            scroll_ar_override=None,
            pre_roll_ms=None,
            show_barlines=True,
            show_warning_arrow=True,
        )

    def test_all_scrolling_elements_stay_synchronised_across_sv_boundaries(self):
        beatmap = core.Beatmap(
            mode=3,
            circle_size=4.0,
            approach_rate=5.0,
            overall_difficulty=5.0,
            hp_drain_rate=5.0,
            audio_lead_in=0,
            audio_filename="",
            title="",
            artist="",
            version="",
            hit_objects=[
                core.HitObject(0, 5000),
                core.HitObject(1, 5200, True, 6500),
            ],
            timing_points=[
                self._point(0, 500),
                self._point(1000, -50),
                self._point(2000, 1000),
            ],
        )
        frames = core.FrameStateIterable(
            beatmap,
            4,
            0.0,
            420.0,
            fps=100,
            skin_config=SimpleNamespace(barline_height=12.0),
            warning_arrow_height_480=480.0,
        )
        wanted = {990.0, 1000.0, 1010.0, 1990.0, 2000.0, 2010.0}
        states = {}
        for state in frames:
            time_ms = round(state["time_ms"], 6)
            if time_ms in wanted:
                states[time_ms] = state
            if time_ms > max(wanted):
                break

        self.assertEqual(set(states), wanted)
        self.assertAlmostEqual(frames.scroll_path.rate_at(999.0), 0.07)
        self.assertAlmostEqual(frames.scroll_path.rate_at(1000.0), 0.14)
        self.assertAlmostEqual(frames.scroll_path.rate_at(2000.0), 0.035)

        def tap_y(state):
            return next(note["y"] for note in state["notes"] if not note["is_hold"])

        def hold_y(state):
            return next(note["y"] for note in state["notes"] if note["is_hold"])

        def hold_tail_y(state):
            return next(
                note["tail_y"] for note in state["notes"] if note["is_hold"]
            )

        def nearest_barline_y(state):
            expected = frames._screen_position(2000.0, state["time_ms"])
            return min(state["barlines"], key=lambda value: abs(value - expected))

        def nearest_warning_y(state):
            expected = frames._screen_position(2000.0, state["time_ms"])
            return min(
                state["warning_arrows"],
                key=lambda value: abs(value - expected),
            )

        getters = (
            tap_y,
            hold_y,
            hold_tail_y,
            nearest_barline_y,
            nearest_warning_y,
        )
        for getter in getters:
            with self.subTest(feature=getter.__name__, boundary="inherited"):
                self.assertAlmostEqual(
                    getter(states[1000.0]) - getter(states[990.0]),
                    0.7,
                )
                self.assertAlmostEqual(
                    getter(states[1010.0]) - getter(states[1000.0]),
                    1.4,
                )
            with self.subTest(feature=getter.__name__, boundary="redline"):
                self.assertAlmostEqual(
                    getter(states[2000.0]) - getter(states[1990.0]),
                    1.4,
                )
                self.assertAlmostEqual(
                    getter(states[2010.0]) - getter(states[2000.0]),
                    0.35,
                )

        for time_ms in wanted:
            self.assertAlmostEqual(
                nearest_barline_y(states[time_ms]),
                nearest_warning_y(states[time_ms]),
            )
        self.assertAlmostEqual(nearest_barline_y(states[1000.0]), 280.0)
        self.assertAlmostEqual(nearest_barline_y(states[2000.0]), 420.0)


if __name__ == "__main__":
    unittest.main()
