"""Failure-recovery and cancellation tests for video export."""

import tempfile
import threading
import unittest
from pathlib import Path
from queue import Full
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from osu_toolbox import SkinConfig
from osu_toolbox import config
from osu_toolbox.tools.render_video import RenderVideoController
from osu_toolbox.video import ffmpeg
from osu_toolbox.video.recorder import VideoRecorder


def _segment_args(index, path, heartbeat=None):
    return (
        [{}],
        index,
        960,
        540,
        60,
        4,
        None,
        "test-run",
        {},
        True,
        str(path),
        str(heartbeat or f"{path}.heartbeat"),
    )


class VideoPipelineRecoveryTests(unittest.TestCase):
    def test_a_late_output_is_not_replaced_without_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged, output = root / "staged.mp4", root / "output.mp4"
            staged.write_bytes(b"new video")
            output.write_bytes(b"another program")
            with self.assertRaises(FileExistsError):
                ffmpeg._replace_output_file(staged, output, allow_overwrite=False)
            self.assertEqual(output.read_bytes(), b"another program")
            self.assertEqual(staged.read_bytes(), b"new video")
            ffmpeg._replace_output_file(staged, output, allow_overwrite=True)
            self.assertEqual(output.read_bytes(), b"new video")
            self.assertFalse(staged.exists())

    def test_render_controller_forwards_configured_parallel_count(self):
        params = dict(config.DEFAULT_RENDER_PARAMS)
        params.update({
            "beatmap_path": "map.osu",
            "skin_dir": "skin",
            "output_file": "output.mp4",
            "key_count": 4,
            "n_parallel": 4,
            "search_root": ".",
        })
        beatmap = SimpleNamespace(
            mode=3,
            circle_size=4,
            special_style=False,
            background=None,
            approach_rate=5.0,
            artist="artist",
            title="title",
            version="difficulty",
            hit_objects=[],
            audio_filename="",
        )
        skin = SkinConfig("", {"Version": "2.7"}, 4)
        geometry = SimpleNamespace(y_spawn=0.0, y_judge=100.0)
        layout = SimpleNamespace(scale_factor=1.0)

        with patch("osu_toolbox.tools.render_video.config.configure_runtime"), patch(
            "osu_toolbox.parse_osu_file",
            return_value=beatmap,
        ), patch(
            "osu_toolbox.load_skin_config",
            return_value=skin,
        ), patch(
            "osu_toolbox.stage_geometry",
            return_value=geometry,
        ), patch(
            "osu_toolbox.resolve_stage_layout",
            return_value=layout,
        ), patch(
            "osu_toolbox.SkinAssets.warning_arrow_height_480",
            return_value=0.0,
        ), patch(
            "osu_toolbox.resolve_scroll",
            return_value=(0.0, 0.0, "scroll"),
        ), patch(
            "osu_toolbox.iter_frame_states",
            return_value=([{}], 0.0),
        ), patch(
            "osu_toolbox.find_audio_file",
            return_value=None,
        ), patch(
            "osu_toolbox.render_frame_states_to_video",
            return_value=True,
        ) as render_video:
            RenderVideoController().run(
                SimpleNamespace(params=params),
                lambda event, payload: None,
            )

        self.assertEqual(render_video.call_args.kwargs["n_parallel"], 4)

    def test_heartbeat_records_completed_frame_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            heartbeat = Path(temp_dir) / "segment.heartbeat"
            args = _segment_args(0, "segment.mp4", heartbeat)

            ffmpeg._write_heartbeat(heartbeat, 37)

            self.assertGreater(heartbeat.stat().st_size, 0)
            self.assertEqual(ffmpeg._heartbeat_progress(args), 37)

    def test_retry_renders_only_invalid_segments_with_software_encoder(self):
        args = [
            _segment_args(0, "segment-0.mp4"),
            _segment_args(1, "segment-1.mp4"),
            _segment_args(2, "segment-2.mp4"),
        ]
        valid = {"segment-0.mp4", "segment-2.mp4"}
        calls = []

        def retry(retry_args, timeout_s=None, cancel_event=None):
            calls.append(retry_args)
            valid.add(retry_args[10])
            return retry_args[10]

        with patch.object(ffmpeg, "_valid_temp_video", side_effect=lambda path: path in valid), patch.object(
            ffmpeg,
            "_render_encode_segment_with_timeout",
            side_effect=retry,
        ):
            fixed, ok = ffmpeg._retry_missing_segments(
                args,
                ["segment-0.mp4", None, "segment-2.mp4"],
            )

        self.assertTrue(ok)
        self.assertEqual(fixed, ["segment-0.mp4", "segment-1.mp4", "segment-2.mp4"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 1)
        self.assertIs(calls[0][9], False)
        self.assertEqual(calls[0][11], "segment-1.mp4.heartbeat")

    def test_parallel_timeout_still_collects_other_ready_results(self):
        class FakeJob:
            def __init__(self, ready, result=None):
                self._ready = ready
                self._result = result

            def ready(self):
                return self._ready

            def get(self):
                return self._result

        class FakePool:
            def __init__(self):
                self._pool = []
                self.jobs = [FakeJob(False), FakeJob(True, "segment-1.mp4")]

            def apply_async(self, func, call_args):
                return self.jobs.pop(0)

            def terminate(self):
                pass

        args = [
            _segment_args(0, "segment-0.mp4"),
            _segment_args(1, "segment-1.mp4"),
        ]
        with patch("multiprocessing.Pool", return_value=FakePool()), patch.object(
            ffmpeg,
            "_segment_timeout_seconds",
            return_value=0.0,
        ), patch.object(
            ffmpeg,
            "_valid_temp_video",
            side_effect=lambda path: path == "segment-1.mp4",
        ):
            results, timed_out = ffmpeg._render_segments_parallel(args, 2)

        self.assertTrue(timed_out)
        self.assertEqual(results, [None, "segment-1.mp4"])

    def test_cancel_preserves_existing_output_and_removes_workspace(self):
        cancel_event = threading.Event()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output.mp4"
            output.write_bytes(b"original")

            def cancel_during_segment(args, timeout_s=None, cancel_event=None):
                Path(args[10]).write_bytes(b"partial")
                cancel_event.set()
                raise ffmpeg.RenderCancelledError("cancelled")

            with patch.object(
                ffmpeg,
                "_render_encode_segment_with_timeout",
                side_effect=cancel_during_segment,
            ):
                with self.assertRaises(ffmpeg.RenderCancelledError):
                    ffmpeg.render_frame_states_to_video(
                        [{}],
                        4,
                        SkinConfig("", {"Version": "2.7"}, 4),
                        output,
                        n_parallel=1,
                        cancel_event=cancel_event,
                    )

            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(root.glob(".osu-toolbox-render-*")), [])

    def test_mux_failure_preserves_existing_output_and_removes_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output.mp4"
            output.write_bytes(b"original")

            def render_segment(args, timeout_s=None):
                Path(args[10]).write_bytes(b"video")
                return args[10]

            with patch.object(
                ffmpeg,
                "_render_encode_segment_with_timeout",
                side_effect=render_segment,
            ), patch.object(
                ffmpeg,
                "_valid_temp_video",
                side_effect=lambda path: bool(path and Path(path).is_file()),
            ), patch.object(
                ffmpeg,
                "mux_audio_to_video",
                return_value=False,
            ):
                result = ffmpeg.render_frame_states_to_video(
                    [{}],
                    4,
                    SkinConfig("", {"Version": "2.7"}, 4),
                    output,
                    n_parallel=1,
                    audio_path=str(root / "audio.mp3"),
                )

            self.assertFalse(result)
            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(root.glob(".osu-toolbox-render-*")), [])

    def test_final_candidate_is_created_next_to_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output.mp4"
            output.write_bytes(b"original")
            staged_paths = []

            def render_segment(args, timeout_s=None):
                Path(args[10]).write_bytes(b"video")
                return args[10]

            def mux_audio(
                video_path,
                audio_path,
                output_path,
                duration_sec,
                song_start_ms=0.0,
                cancel_event=None,
            ):
                staged_path = Path(output_path)
                staged_paths.append(staged_path)
                staged_path.write_bytes(b"muxed")
                return True

            with patch.object(
                ffmpeg,
                "_render_encode_segment_with_timeout",
                side_effect=render_segment,
            ), patch.object(
                ffmpeg,
                "_valid_temp_video",
                side_effect=lambda path: bool(
                    path and Path(path).is_file() and Path(path).stat().st_size
                ),
            ), patch.object(
                ffmpeg,
                "mux_audio_to_video",
                side_effect=mux_audio,
            ):
                result = ffmpeg.render_frame_states_to_video(
                    [{}],
                    4,
                    SkinConfig("", {"Version": "2.7"}, 4),
                    output,
                    n_parallel=1,
                    audio_path=str(root / "audio.mp3"),
                )

            self.assertTrue(result)
            self.assertEqual(output.read_bytes(), b"muxed")
            self.assertEqual(len(staged_paths), 1)
            self.assertEqual(staged_paths[0].parent, root)
            self.assertTrue(staged_paths[0].name.startswith(".osu-toolbox-output-"))
            self.assertEqual(list(root.glob(".osu-toolbox-output-*")), [])
            self.assertEqual(list(root.glob(".osu-toolbox-render-*")), [])


class VideoRecorderBackpressureTests(unittest.TestCase):
    def test_temporary_full_queue_is_backpressure_not_failure(self):
        class FakeProcess:
            returncode = None

            def poll(self):
                return None

        class SlowQueue:
            def __init__(self):
                self.attempts = 0

            def put(self, item, timeout=None):
                self.attempts += 1
                if self.attempts < 3:
                    raise Full

        recorder = VideoRecorder(2, 2, fps=60, allow_hw=False, quiet=True)
        recorder.proc = FakeProcess()
        recorder._frame_queue = SlowQueue()

        self.assertTrue(recorder.capture_frame(np.zeros((2, 2, 3), dtype=np.uint8)))
        self.assertIsNone(recorder._write_error)
        self.assertEqual(recorder._frame_queue.attempts, 3)


if __name__ == "__main__":
    unittest.main()
