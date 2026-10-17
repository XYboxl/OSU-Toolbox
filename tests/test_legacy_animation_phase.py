import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import osu_toolbox as core
from osu_toolbox import SkinConfig
from osu_toolbox.video import ffmpeg


class LegacyAnimationPhaseRoutingTests(unittest.TestCase):
    @staticmethod
    def _state(time_ms, animation_time_ms):
        return {
            "time_ms": time_ms,
            "animation_time_ms": animation_time_ms,
            "notes": [],
            "score": 0,
            "combo": 0,
        }

    @staticmethod
    def _renderer():
        renderer = MagicMock()
        renderer.lane_rects = []
        renderer.finish_frame.return_value = object()
        return renderer

    def test_global_assets_receive_animation_clock_not_song_clock(self):
        renderer = self._renderer()
        skin_config = SimpleNamespace(keys_under_notes=False)

        ffmpeg.render_frame_state(
            renderer,
            skin_config,
            self._state(-2500.0, 375.0),
        )

        renderer.draw_stage_hints.assert_called_once_with(375.0)
        renderer.draw_stage_foreground.assert_called_once_with(375.0)
        self.assertEqual(renderer.draw_health_bar.call_args.args[-1], 375.0)

    def test_fresh_segment_renderer_receives_unrebased_animation_clock(self):
        skin_config = SimpleNamespace(keys_under_notes=False)
        first_renderer = self._renderer()
        second_renderer = self._renderer()

        ffmpeg.render_frame_state(
            first_renderer,
            skin_config,
            self._state(4000.0, 6500.0),
        )
        ffmpeg.render_frame_state(
            second_renderer,
            skin_config,
            self._state(4016.0, 6516.0),
        )

        first_renderer.draw_stage_foreground.assert_called_once_with(6500.0)
        second_renderer.draw_stage_foreground.assert_called_once_with(6516.0)

    def test_legacy_state_without_animation_clock_keeps_compatibility(self):
        renderer = self._renderer()
        skin_config = SimpleNamespace(keys_under_notes=False)
        state = self._state(1234.0, 0.0)
        del state["animation_time_ms"]

        ffmpeg.render_frame_state(renderer, skin_config, state)

        renderer.draw_stage_foreground.assert_called_once_with(1234.0)
        self.assertEqual(renderer.draw_health_bar.call_args.args[-1], 1234.0)

    def test_parallel_batch_cache_materializes_once_and_preserves_timestamps(self):
        expected_states = [
            self._state(1000.0 + index * 16.0, 5000.0 + index * 16.0)
            for index in range(6)
        ]

        class RepeatableStates:
            def __init__(self, values):
                self.values = values
                self.iterations = 0

            def __len__(self):
                return len(self.values)

            def __iter__(self):
                self.iterations += 1
                return iter(self.values)

        states = RepeatableStates(expected_states)
        captured_batches = []
        statuses = []

        def capture_segments(
            args_list,
            worker_count,
            cancel_event=None,
            status_cb=None,
            progress_cb=None,
        ):
            self.assertEqual(states.iterations, 1)
            self.assertTrue(
                all(isinstance(args[0], ffmpeg._FrameStateBatchFile) for args in args_list),
            )
            captured_batches.extend(list(args[0]) for args in args_list)
            if progress_cb is not None:
                progress_cb(len(expected_states), len(expected_states))
            return [None] * len(args_list), False

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            ffmpeg,
            "_render_segments_parallel",
            side_effect=capture_segments,
        ), patch.object(
            ffmpeg,
            "_retry_missing_segments",
            return_value=([None, None], False),
        ), patch.object(
            ffmpeg,
            "_valid_temp_video",
            return_value=False,
        ), redirect_stdout(io.StringIO()):
            result = ffmpeg.render_frame_states_to_video(
                states,
                1,
                SkinConfig("", {"Version": "2.7"}, 1),
                str(Path(temp_dir) / "unused.mp4"),
                fps=60.0,
                n_parallel=2,
                status_cb=statuses.append,
            )

        self.assertIs(result, False)
        self.assertEqual(len(captured_batches), 2)
        flattened = [
            state
            for batch in captured_batches
            for state in batch
        ]
        self.assertEqual(
            [state["animation_time_ms"] for state in flattened],
            [state["animation_time_ms"] for state in expected_states],
        )
        self.assertEqual(captured_batches[1][0]["animation_time_ms"], 5048.0)
        self.assertTrue(any("frame states" in message for message in statuses))
        self.assertTrue(any("Starting 2 render workers" in message for message in statuses))

    def test_hold_body_uses_per_note_animation_age(self):
        renderer = self._renderer()
        renderer.lane_rects = [(0, 0, 10, 10)]
        skin_config = SimpleNamespace(keys_under_notes=False)
        state = self._state(1000.0, 1000.0)
        state["notes"] = [{
            "col": 0,
            "y": 8.0,
            "tail_y": 2.0,
            "is_hold": True,
            "is_holding": False,
            "hold_animation_age_ms": 345.0,
        }]
        state["hold_light_states"] = (("hold", 999.0),)

        ffmpeg.render_frame_state(renderer, skin_config, state)

        self.assertEqual(renderer.draw_skin_hold.call_args.args[-1], 345.0)

    def test_each_hold_body_keeps_its_own_animation_age(self):
        renderer = self._renderer()
        renderer.lane_rects = [(0, 0, 10, 10)]
        skin_config = SimpleNamespace(keys_under_notes=False)
        state = self._state(1000.0, 1000.0)
        state["notes"] = [
            {
                "col": 0,
                "y": 8.0,
                "tail_y": 2.0,
                "is_hold": True,
                "is_holding": True,
                "hold_animation_age_ms": age_ms,
            }
            for age_ms in (120.0, 450.0)
        ]

        ffmpeg.render_frame_state(renderer, skin_config, state)

        self.assertEqual(
            [call.args[-1] for call in renderer.draw_skin_hold.call_args_list],
            [120.0, 450.0],
        )


class LegacyNoteAnimationStateTests(unittest.TestCase):
    def setUp(self):
        runtime = {
            "compatibility_mode": core.COMPATIBILITY_MODE,
            "scroll_speed": core.SCROLL_SPEED,
            "scroll_speed_mult": core.SCROLL_SPEED_MULT,
            "scroll_ar_override": core.SCROLL_AR_OVERRIDE,
            "pre_roll_ms": core.PRE_ROLL_MS,
            "show_barlines": core.SHOW_BARLINES,
            "show_warning_arrow": core.SHOW_WARNING_ARROW,
        }
        self.addCleanup(core.configure_runtime, **runtime)
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
            scroll_speed=20,
            scroll_speed_mult=1.0,
            scroll_ar_override=None,
            pre_roll_ms=0.0,
            show_barlines=False,
            show_warning_arrow=False,
        )

    @staticmethod
    def _beatmap(hit_objects):
        return core.Beatmap(
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
            hit_objects=hit_objects,
            timing_points=[core.TimingPoint(0.0, 500.0)],
        )

    @staticmethod
    def _replay(
        key_frames, count_geki=0, count_300=0, count_50=0, miss=0,
    ):
        return SimpleNamespace(
            mods=0,
            frames=[
                SimpleNamespace(time=float(time), keys=int(keys), seq=seq)
                for seq, (time, keys) in enumerate(key_frames)
            ],
            life_graph="",
            count_geki=count_geki,
            count_300=count_300,
            count_katu=0,
            count_100=0,
            count_50=count_50,
            count_miss=miss,
        )

    @staticmethod
    def _states_after(frames, targets):
        pending = iter(sorted(targets))
        target = next(pending, None)
        result = {}
        for state in frames:
            while target is not None and state["time_ms"] >= target:
                result[target] = state
                target = next(pending, None)
            if target is None:
                break
        return result

    def test_tap_phase_starts_at_int_truncated_movement_entry(self):
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 100)]),
            4,
            0.0,
            419.0,
            fps=1000,
        )
        exact_entry = frames.scroll_path.time_for_raw_position(
            100.0,
            0.0,
            frames.hit_position,
        )
        self.assertLess(exact_entry, 0.0)
        self.assertNotEqual(exact_entry, float(int(exact_entry)))

        iterator = iter(frames)
        next(iterator)
        state = next(iterator)
        note = next(note for note in state["notes"] if not note["is_hold"])

        self.assertAlmostEqual(
            note["animation_age_ms"],
            max(0.0, state["time_ms"] - int(exact_entry)),
        )
        self.assertLess(note["animation_age_ms"], 1.0)

    def test_ln_head_and_tail_use_their_own_movement_entries(self):
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 2000)]),
            4,
            0.0,
            419.0,
            fps=1000,
        )
        state = self._states_after(frames, [1500.0])[1500.0]
        note = next(note for note in state["notes"] if note["is_hold"])
        head_entry = int(frames.scroll_path.time_for_raw_position(
            1000.0,
            0.0,
            frames.hit_position,
        ))
        tail_entry = int(frames.scroll_path.time_for_raw_position(
            2000.0,
            0.0,
            frames.hit_position,
        ))

        self.assertAlmostEqual(
            note["head_animation_age_ms"],
            state["time_ms"] - head_entry,
        )
        self.assertAlmostEqual(
            note["tail_animation_age_ms"],
            state["time_ms"] - tail_entry,
        )
        self.assertAlmostEqual(
            note["head_animation_age_ms"] - note["tail_animation_age_ms"],
            tail_entry - head_entry,
        )

    def test_ln_body_freezes_on_release_and_resumes_from_frozen_phase(self):
        replay = self._replay(
            [(1000.0, 1), (1505.0, 0), (1800.0, 1), (3000.0, 0)],
            count_50=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 3000)]),
            4,
            0.0,
            419.0,
            fps=1000,
            replay=replay,
        )
        states = self._states_after(frames, [900.0, 1200.0, 1600.0, 1700.0, 1900.0])
        notes = {
            target: next(note for note in state["notes"] if note["is_hold"])
            for target, state in states.items()
        }
        self.assertFalse(notes[900.0]["hold_animation_started"])
        self.assertEqual(notes[900.0]["hold_animation_age_ms"], 0.0)
        self.assertTrue(notes[1200.0]["is_holding"])
        self.assertAlmostEqual(
            notes[1200.0]["hold_animation_age_ms"],
            states[1200.0]["time_ms"] - 1000.0,
        )

        frozen_age = 480.0
        self.assertFalse(notes[1600.0]["is_holding"])
        self.assertTrue(notes[1600.0]["hold_animation_started"])
        self.assertAlmostEqual(notes[1600.0]["hold_animation_age_ms"], frozen_age)
        self.assertAlmostEqual(notes[1700.0]["hold_animation_age_ms"], frozen_age)

        self.assertTrue(notes[1900.0]["is_holding"])
        self.assertAlmostEqual(
            notes[1900.0]["hold_animation_age_ms"],
            frozen_age + states[1900.0]["time_ms"] - 1800.0,
        )

    def test_ln_body_starts_at_the_actual_early_head_hit(self):
        replay = self._replay(
            [(950.0, 1), (2000.0, 0)],
            count_300=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 2000)]),
            4,
            0.0,
            419.0,
            fps=1000,
            replay=replay,
        )
        state = self._states_after(frames, [975.0])[975.0]
        note = next(note for note in state["notes"] if note["is_hold"])
        self.assertEqual(frames.holds[0][5], 950.0)
        self.assertTrue(note["hold_animation_started"])
        self.assertTrue(note["is_holding"])
        self.assertAlmostEqual(
            note["hold_animation_age_ms"],
            state["time_ms"] - 950.0,
        )

    def test_ln_body_starts_at_the_actual_late_head_hit(self):
        replay = self._replay(
            [(1050.0, 1), (2000.0, 0)],
            count_300=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 2000)]),
            4,
            0.0,
            419.0,
            fps=1000,
            replay=replay,
        )
        state = self._states_after(frames, [1075.0])[1075.0]
        note = next(note for note in state["notes"] if note["is_hold"])

        self.assertEqual(frames.holds[0][5], 1050.0)
        self.assertTrue(note["is_holding"])
        self.assertAlmostEqual(
            note["hold_animation_age_ms"], state["time_ms"] - 1050.0,
        )

    def test_missed_ln_head_keeps_body_on_frame_zero(self):
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 2000)]),
            4,
            0.0,
            419.0,
            fps=1000,
            replay=self._replay([], miss=1),
        )
        state = self._states_after(frames, [1200.0])[1200.0]
        note = next(note for note in state["notes"] if note["is_hold"])

        self.assertIsNone(frames.holds[0][5])
        self.assertFalse(note["hold_animation_started"])
        self.assertFalse(note["is_holding"])
        self.assertEqual(note["hold_animation_age_ms"], 0.0)

    def test_key_events_before_the_valid_head_hit_do_not_start_body(self):
        replay = self._replay(
            [(700.0, 1), (710.0, 0), (1000.0, 1), (2000.0, 0)],
            count_geki=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 2000)]),
            4,
            0.0,
            419.0,
            fps=1000,
            replay=replay,
        )
        state = self._states_after(frames, [1025.0])[1025.0]
        note = next(note for note in state["notes"] if note["is_hold"])

        self.assertTrue(note["is_holding"])
        self.assertAlmostEqual(
            note["hold_animation_age_ms"], state["time_ms"] - 1000.0,
        )

    def test_early_head_hit_before_movement_entry_is_not_lost(self):
        core.configure_runtime(scroll_speed=40, scroll_speed_mult=2.0)
        replay = self._replay(
            [
                (1000.0, 2),
                (1010.0, 0),
                (2827.0, 1),
                (4000.0, 0),
            ],
            count_geki=1,
            count_50=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([
                core.HitObject(1, 1000),
                core.HitObject(0, 3000, True, 4000),
            ]),
            4,
            0.0,
            419.0,
            fps=1000,
            replay=replay,
        )
        entry_ms = frames.scroll_path.time_for_raw_position(
            3000.0,
            0.0,
            frames.hit_position,
        )
        self.assertLess(2827.0, entry_ms)

        state = self._states_after(frames, [2900.0])[2900.0]
        note = next(note for note in state["notes"] if note["is_hold"])

        self.assertTrue(note["hold_animation_started"])
        self.assertTrue(note["is_holding"])
        self.assertAlmostEqual(
            note["hold_animation_age_ms"], state["time_ms"] - 2827.0,
        )

    def test_autoplay_starts_each_overlapping_ln_in_the_same_column(self):
        frames = core.FrameStateIterable(
            self._beatmap([
                core.HitObject(0, 1000, True, 3000),
                core.HitObject(0, 1500, True, 2500),
            ]),
            4,
            0.0,
            419.0,
            fps=1000,
        )
        state = self._states_after(frames, [1600.0])[1600.0]
        notes = sorted(
            (note for note in state["notes"] if note["is_hold"]),
            key=lambda note: note["hold_animation_age_ms"],
        )

        self.assertEqual(len(notes), 2)
        self.assertTrue(all(note["is_holding"] for note in notes))
        self.assertAlmostEqual(
            notes[0]["hold_animation_age_ms"], state["time_ms"] - 1500.0,
        )
        self.assertAlmostEqual(
            notes[1]["hold_animation_age_ms"], state["time_ms"] - 1000.0,
        )

    def test_ln_body_keeps_a_press_and_release_between_output_frames(self):
        replay = self._replay(
            [(1000.0, 1), (1005.0, 0)],
            miss=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 3000)]),
            4,
            0.0,
            419.0,
            fps=60,
            replay=replay,
        )
        state = self._states_after(frames, [1020.0])[1020.0]
        note = next(note for note in state["notes"] if note["is_hold"])
        self.assertTrue(note["hold_animation_started"])
        self.assertFalse(note["is_holding"])
        self.assertAlmostEqual(
            note["hold_animation_age_ms"],
            0.0,
        )

    def test_ln_body_consumes_multiple_transitions_between_output_frames(self):
        replay = self._replay(
            [
                (1000.0, 1),
                (1035.0, 0),
                (1040.0, 1),
                (1065.0, 0),
                (1070.0, 1),
                (1105.0, 0),
                (1110.0, 1),
                (1110.0, 0),
            ],
            miss=1,
        )
        frames = core.FrameStateIterable(
            self._beatmap([core.HitObject(0, 1000, True, 3000)]),
            4,
            0.0,
            419.0,
            fps=10,
            replay=replay,
        )
        state = self._states_after(frames, [1120.0])[1120.0]
        note = next(note for note in state["notes"] if note["is_hold"])

        self.assertTrue(note["hold_animation_started"])
        self.assertFalse(note["is_holding"])
        self.assertEqual(note["hold_animation_age_ms"], 60.0)


if __name__ == "__main__":
    unittest.main()
