"""Runtime and worker propagation tests for Stable song-progress modes."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from osu_toolbox import SkinConfig, config
from osu_toolbox.video import ffmpeg


class SongProgressConfigTests(unittest.TestCase):
    def tearDown(self):
        config.configure_runtime(show_song_progress=config.SONG_PROGRESS_PIE)

    def test_default_mode_is_pie(self):
        self.assertEqual(
            config.DEFAULT_RENDER_PARAMS["show_song_progress"],
            config.SONG_PROGRESS_PIE,
        )

    def test_all_stable_modes_are_accepted(self):
        for mode in config.SONG_PROGRESS_MODES:
            with self.subTest(mode=mode):
                config.configure_runtime(show_song_progress=mode)
                self.assertEqual(config.SONG_PROGRESS_MODE, mode)
                self.assertEqual(
                    config.SHOW_SONG_PROGRESS,
                    mode != config.SONG_PROGRESS_OFF,
                )

    def test_legacy_bools_map_to_off_and_pie(self):
        config.configure_runtime(show_song_progress=False)
        self.assertEqual(config.SONG_PROGRESS_MODE, config.SONG_PROGRESS_OFF)
        self.assertIs(config.SHOW_SONG_PROGRESS, False)

        config.configure_runtime(show_song_progress=True)
        self.assertEqual(config.SONG_PROGRESS_MODE, config.SONG_PROGRESS_PIE)
        self.assertIs(config.SHOW_SONG_PROGRESS, True)

    def test_stable_display_names_are_normalized(self):
        aliases = {
            "TopRight": config.SONG_PROGRESS_TOP_RIGHT,
            "bottom-right": config.SONG_PROGRESS_BOTTOM_RIGHT,
            "BOTTOM": config.SONG_PROGRESS_BOTTOM,
        }
        for value, expected in aliases.items():
            with self.subTest(value=value):
                config.configure_runtime(show_song_progress=value)
                self.assertEqual(config.SONG_PROGRESS_MODE, expected)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            config.configure_runtime(show_song_progress="left")


class SongProgressWorkerTests(unittest.TestCase):
    def tearDown(self):
        config.configure_runtime(show_song_progress=config.SONG_PROGRESS_PIE)

    @staticmethod
    def _runtime_options(mode):
        return {
            "compatibility_mode": config.COMPAT_STABLE_REPLAY,
            "scroll_speed": 28.0,
            "scroll_direction": "auto",
            "scroll_ar_override": None,
            "scroll_speed_mult": 1.0,
            "hit_flash_ms": 80.0,
            "tail_ms": 2000.0,
            "show_hold_tail": True,
            "show_health_bar": True,
            "show_barlines": True,
            "song_progress_mode": mode,
            "show_hit_error": True,
            "show_warning_arrow": True,
            "background_path": None,
            "background_dim": 0.8,
            "letterbox_in_breaks": None,
            "receptor_pressed": False,
        }

    def test_worker_passes_mode_to_runtime_config(self):
        class Configured(Exception):
            pass

        options = self._runtime_options(config.SONG_PROGRESS_BOTTOM_RIGHT)
        with patch.object(
            ffmpeg.config,
            "configure_runtime",
            side_effect=Configured,
        ) as configure:
            with self.assertRaises(Configured):
                ffmpeg._render_encode_segment_impl((
                    [], 0, 960, 540, 60, 4, None, "test", options, False,
                ))

        self.assertEqual(
            configure.call_args.kwargs["show_song_progress"],
            config.SONG_PROGRESS_BOTTOM_RIGHT,
        )

    def test_worker_accepts_legacy_bool_payload(self):
        class Configured(Exception):
            pass

        options = self._runtime_options(config.SONG_PROGRESS_PIE)
        options.pop("song_progress_mode")
        options["show_song_progress"] = False
        with patch.object(
            ffmpeg.config,
            "configure_runtime",
            side_effect=Configured,
        ) as configure:
            with self.assertRaises(Configured):
                ffmpeg._render_encode_segment_impl((
                    [], 0, 960, 540, 60, 4, None, "test", options, False,
                ))

        self.assertIs(configure.call_args.kwargs["show_song_progress"], False)

    def test_export_interface_serializes_explicit_mode_for_worker(self):
        captured = []

        def capture(args, timeout_s=None):
            captured.append(args)
            return None

        with patch.object(
            ffmpeg,
            "_render_encode_segment_with_timeout",
            side_effect=capture,
        ), patch.object(
            ffmpeg,
            "_retry_missing_segments",
            return_value=([None], False),
        ), patch.object(ffmpeg, "_valid_temp_video", return_value=False):
            with redirect_stdout(io.StringIO()):
                result = ffmpeg.render_frame_states_to_video(
                    [{}],
                    4,
                    SkinConfig("", {"Version": "2.7"}, 4),
                    "unused.mp4",
                    n_parallel=1,
                    song_progress_mode="BottomRight",
                )

        self.assertIs(result, False)
        self.assertEqual(
            captured[0][8]["song_progress_mode"],
            config.SONG_PROGRESS_BOTTOM_RIGHT,
        )


if __name__ == "__main__":
    unittest.main()
