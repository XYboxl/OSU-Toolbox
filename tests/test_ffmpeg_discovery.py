"""FFmpeg discovery boundaries and missing-dependency errors."""

import io
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from osu_toolbox import external_tools, speed_change


CHART = (
    "osu file format v14\n[General]\nAudioFilename:song.wav\nMode:3\n"
    "[Metadata]\nTitle:Song\nVersion:Hard\n"
    "[TimingPoints]\n0,500,4,1,0,100,1,0\n"
    "[HitObjects]\n64,192,1000,1,0,0:0:0:0:\n"
)


def _executable(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    path.write_bytes(b"test executable")
    path.chmod(0o755)
    return path.resolve()


class FFmpegDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.application = self.root / "application"
        self.application.mkdir()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.object(external_tools.sys, "frozen", True, create=True))
        contexts.enter_context(patch.object(
            external_tools.sys, "executable", str(self.application / "osu-toolbox.exe"),
        ))
        contexts.enter_context(patch.dict(os.environ, {"PATH": ""}))

    def test_frozen_program_prefers_its_own_directory_over_path(self):
        local = _executable(self.application)
        other = _executable(self.root / "system-tools")
        with patch.dict(os.environ, {"PATH": str(other.parent)}):
            self.assertEqual(external_tools.find_ffmpeg(), str(local))

    def test_source_run_uses_its_project_root(self):
        local = _executable(self.application)
        with patch.object(external_tools.sys, "frozen", False), patch.object(
            external_tools, "__file__", str(self.application / "osu_toolbox" / "external_tools.py"),
        ):
            self.assertEqual(external_tools.find_ffmpeg(), str(local))

    def test_path_directories_are_searched_in_the_configured_order(self):
        first = _executable(self.root / "first-tools")
        second = _executable(self.root / "second-tools")
        paths = os.pathsep.join((str(self.root / "missing-tools"), str(first.parent), str(second.parent)))
        with patch.dict(os.environ, {"PATH": paths}):
            self.assertEqual(external_tools.find_ffmpeg(), str(first))

    def test_unrelated_directories_and_bundle_resources_are_not_fallbacks(self):
        _executable(self.root / "other-application")
        _executable(self.application / "_main")
        _executable(self.application / "_menu")
        with patch.object(external_tools.sys, "_MEIPASS", str(self.application / "_main"), create=True):
            self.assertIsNone(external_tools.find_ffmpeg())
            self.assertIsNone(speed_change.find_ffmpeg())

    def test_current_directory_is_not_searched_implicitly(self):
        current = _executable(self.root / "current-directory").parent
        old = Path.cwd()
        try:
            os.chdir(current)
            self.assertIsNone(external_tools.find_ffmpeg())
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(external_tools.find_ffmpeg())
            with patch.dict(os.environ, {"PATH": str(current)}):
                self.assertEqual(external_tools.find_ffmpeg(), str(current / _executable_name()))
        finally:
            os.chdir(old)

    def test_missing_dependency_has_an_actionable_chinese_message(self):
        with self.assertRaisesRegex(FileNotFoundError, "未找到 FFmpeg.*程序同目录.*PATH"):
            external_tools.require_ffmpeg()

    def test_missing_ffmpeg_stops_each_speed_input_before_creating_outputs(self):
        song = self.root / "Song"
        song.mkdir()
        chart = song / "hard.osu"
        chart.write_text(CHART, encoding="utf-8")
        (song / "song.wav").write_bytes(b"original audio")
        archive = self.root / "Pack.osz"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("hard.osu", CHART)
            package.writestr("song.wav", b"original audio")
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for kind, source, options in (
            ("file", chart, {}),
            ("archive", archive, {}),
            ("folder", chart, {"folder_outputs": {str(song): str(self.root / "Song 1.5x")}}),
            ("collection", chart, {"output_folder": str(self.root / "collection")}),
        ):
            with self.subTest(kind=kind), self.assertRaisesRegex(speed_change.SpeedChangeError, "未找到 FFmpeg"):
                speed_change.change_speed(
                    [source], 1.5, file_pattern=" {num}x", difficulty_pattern=" {num}x", **options,
                )
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.root / "Song 1.5x").exists())
        self.assertFalse((self.root / "collection").exists())

    def test_missing_ffmpeg_is_not_reported_as_undecodable_audio(self):
        source = self.root / "song.wav"
        source.write_bytes(b"audio")
        destination = self.root / "output" / "song 1.5x.wav"
        with patch.object(speed_change, "probe_audio") as probe:
            with self.assertRaisesRegex(speed_change.SpeedChangeError, "未找到 FFmpeg"):
                speed_change.resample_audio(source, destination, 1.5)
        probe.assert_not_called()
        self.assertFalse(destination.parent.exists())

    def test_removed_executable_is_reported_as_a_missing_dependency(self):
        with patch.object(speed_change.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(speed_change.SpeedChangeError, "未找到 FFmpeg"):
                speed_change.probe_audio("ffmpeg", "song.wav")

    def test_video_recorder_reports_missing_ffmpeg_before_starting_a_process(self):
        from osu_toolbox.video.recorder import VideoRecorder

        with patch("osu_toolbox.video.recorder.subprocess.Popen") as process:
            with self.assertRaisesRegex(FileNotFoundError, "未找到 FFmpeg"):
                VideoRecorder(2, 2, allow_hw=False)
        process.assert_not_called()

    def test_video_export_reports_missing_ffmpeg_before_creating_outputs(self):
        from osu_toolbox.video.ffmpeg import render_frame_states_to_video

        destination = self.root / "output" / "video.mp4"
        with self.assertRaisesRegex(FileNotFoundError, "未找到 FFmpeg"):
            render_frame_states_to_video([{}], 4, object(), destination)
        self.assertFalse(destination.parent.exists())

    def test_gui_speed_controller_preserves_the_missing_dependency_message(self):
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        chart = self.root / "hard.osu"
        chart.write_text(CHART, encoding="utf-8")
        controller = SpeedChangeController()
        prepared = controller.prepare({"input_paths": str(chart), "speed": 1.5}, ToolContext(str(self.root)))
        with self.assertRaisesRegex(ValueError, "未找到 FFmpeg"):
            controller.run(prepared, lambda *args: None)

    def test_shell_speed_command_returns_the_missing_dependency_message(self):
        from osu_toolbox.quick import main

        chart = self.root / "hard.osu"
        chart.write_text(CHART, encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        code = main([
            "--json", "--context", "shell", "--operation", "speed-change", "--speed", "1.5", str(chart),
        ], stdout=output, stderr=errors)
        self.assertNotEqual(code, 0)
        self.assertIn("未找到 FFmpeg", errors.getvalue())
        self.assertFalse((self.root / "hard 1.5x.osu").exists())


def _executable_name():
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


if __name__ == "__main__":
    unittest.main()
