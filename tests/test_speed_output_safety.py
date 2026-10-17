"""Protect existing results and keep chart/audio destinations consistent."""
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from osu_toolbox.speed_change import (change_speed, resample_audio, SpeedChangeError,
                                     _write_chart_text, SpeedCancelledError)
from osu_toolbox.tools import ToolContext
from osu_toolbox.tools.speed_change import SpeedChangeController


CHART = """osu file format v14
[General]
AudioFilename: music.wav
Mode: 3
[Metadata]
Title: Song
Version: Hard
[Difficulty]
CircleSize: 4
[TimingPoints]
0,500,4,1,0,100,1,0
[HitObjects]
64,192,1000,1,0,0:0:0:0:
"""


class SpeedOutputSafetyTests(unittest.TestCase):
    def prepare(self, root, output=None):
        source = root / "input"
        source.mkdir()
        chart = source / "chart.osu"
        chart.write_text(CHART, encoding="utf-8")
        (source / "music.wav").write_bytes(b"original audio")
        controller = SpeedChangeController()
        raw = dict(input_paths=[str(chart)], speed=1.1)
        if output:
            raw.update(output_location="workdir")
        return source, controller, controller.prepare(raw, ToolContext(str(root), str(output or root), {}))

    @staticmethod
    def encode(_registry, source, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"converted")

    def test_custom_output_contains_both_chart_and_audio_and_leaves_input_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result"
            source, controller, prepared = self.prepare(root, output)
            before = {path.name: path.read_bytes() for path in source.iterdir()}
            self.assertIn(str(output / "music 1.1x.wav"), prepared.params["planned_files"])
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", self.encode):
                controller.run(prepared, lambda *_: None)
            self.assertIn("AudioFilename: music 1.1x.wav",
                          (output / "chart 1.1x.osu").read_text(encoding="utf-8"))
            self.assertEqual((output / "music 1.1x.wav").read_bytes(), b"converted")
            self.assertEqual({path.name: path.read_bytes() for path in source.iterdir()}, before)

    def test_existing_audio_alone_requires_confirmation_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, controller, _ = self.prepare(root)
            target = source / "music 1.1x.wav"
            target.write_bytes(b"keep")
            prepared = controller.prepare(dict(input_paths=[str(source / "chart.osu")], speed=1.1),
                                          ToolContext(str(root), str(root), {}))
            self.assertIsNotNone(prepared.confirmation)
            self.assertIn(str(target), prepared.confirmation.message)
            with self.assertRaises(ValueError):
                controller.run(prepared, lambda *_: None)
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertFalse((source / "chart 1.1x.osu").exists())

    def test_a_new_file_between_preflight_and_execution_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, controller, prepared = self.prepare(Path(directory))
            target = source / "chart 1.1x.osu"
            target.write_bytes(b"another program")
            with self.assertRaises(ValueError):
                controller.run(prepared, lambda *_: None)
            self.assertEqual(target.read_bytes(), b"another program")
            self.assertFalse((source / "music 1.1x.wav").exists())

    def test_shared_audio_in_one_run_is_allowed_without_overwrite_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.osu"
            second = root / "b.osu"
            first.write_text(CHART, encoding="utf-8")
            second.write_text(CHART, encoding="utf-8")
            (root / "music.wav").write_bytes(b"original")
            with patch("osu_toolbox.speed_change.resample_audio") as encode:
                encode.side_effect = lambda _source, destination, *_args, **_kwargs: Path(destination).write_bytes(b"converted")
                result = change_speed([first, second], 1.1, file_pattern=" {num}x",
                                      difficulty_pattern=" {num}x", allow_overwrite=False)
            self.assertEqual(result["failures"], [])
            self.assertEqual(encode.call_count, 1)
            self.assertEqual((root / "music 1.1x.wav").read_bytes(), b"converted")

    def test_distinct_inputs_cannot_silently_share_a_custom_output_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for name in ("one", "two"):
                folder = root / name
                folder.mkdir()
                chart = folder / "chart.osu"
                chart.write_text(CHART, encoding="utf-8")
                (folder / "music.wav").write_bytes(name.encode())
                inputs.append(chart)
            with self.assertRaises(SpeedChangeError):
                change_speed(inputs, 1.1, file_pattern=" {num}x", difficulty_pattern=" {num}x",
                             output_directory=root / "out")
            self.assertFalse((root / "out").exists())

    def test_failed_audio_encoding_keeps_an_existing_result_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.wav", root / "result.wav"
            source.write_bytes(b"source")
            target.write_bytes(b"old result")
            def fail(command, *_):
                Path(command[-1]).write_bytes(b"partial")
                raise OSError("encoder failed")
            with patch("osu_toolbox.speed_change.probe_audio", return_value=(44100, 128000)), \
                 patch("osu_toolbox.speed_change._run_process", side_effect=fail):
                with self.assertRaises(OSError):
                    resample_audio(source, target, 1.1, ffmpeg="ffmpeg")
            self.assertEqual(target.read_bytes(), b"old result")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["result.wav", "source.wav"])

    def test_a_failed_chart_write_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "chart.osu"
            target.write_bytes(b"previous chart")
            with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    _write_chart_text(target, "new chart", False)
            self.assertEqual(target.read_bytes(), b"previous chart")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cancelled_archive_replacement_keeps_the_previous_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pack.osz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("chart.osu", CHART.replace("music.wav", ""))
                archive.writestr("resource", b"original")
            target = root / "pack 1.1x.osz"
            target.write_bytes(b"old archive")
            cancelled = threading.Event()
            def emit(kind, _payload):
                if kind == "log":
                    cancelled.set()
            with self.assertRaises(SpeedCancelledError):
                change_speed([source], 1.1, file_pattern=" {num}x", difficulty_pattern=" {num}x",
                             cancel_event=cancelled, emit_event=emit)
            self.assertEqual(target.read_bytes(), b"old archive")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["pack 1.1x.osz", "pack.osz"])

    def test_archive_missing_audio_is_reported_as_an_incomplete_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pack.osz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("chart.osu", CHART)
            result = change_speed([source], 1.1, file_pattern=" {num}x", difficulty_pattern=" {num}x")
            self.assertEqual(result["failures"], [])
            self.assertTrue(result["entries"][0]["warnings"])


class NumericInputSafetyTests(unittest.TestCase):
    def test_non_finite_numbers_are_rejected_before_runtime_configuration(self):
        from osu_toolbox.schema import ParamSpec, ParamValidationError, coerce_param
        for kind in ("float", "optional_float", "int", "optional_int"):
            spec = ParamSpec("number", "number", kind, 1, "input", "")
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(kind=kind, value=value):
                    with self.assertRaises(ParamValidationError) as error:
                        coerce_param(spec, value)
                    self.assertEqual(error.exception.key, "number")


class QuickMenuDiagnosticTests(unittest.TestCase):
    def test_menu_conversion_notices_follow_the_final_location_and_exclude_progress(self):
        import io
        import json
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "chart.osu"
            chart.write_text(CHART, encoding="utf-8")
            (root / "music.wav").write_bytes(b"audio")
            for same_directory in (True, False):
                with self.subTest(same_directory=same_directory):
                    target = root / ("same.mc" if same_directory else "another/custom.mc")
                    output, errors = io.StringIO(), io.StringIO()
                    code = main(["--context", "shell", "--operation", "chart-convert",
                                 str(chart), "--target-format", ".mc", "-o", str(target), "--json"],
                                stdout=output, stderr=errors)
                    self.assertEqual(code, 0, errors.getvalue())
                    warnings = json.loads(output.getvalue())["warnings"]
                    self.assertFalse(any("fraction" in value or "current" in value for value in warnings))
                    self.assertEqual(bool(warnings), not same_directory)


    def test_menu_publish_protects_a_file_created_during_staging(self):
        from osu_toolbox.quick import _publish_chart_output, QuickCommandError
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "staged.mc", root / "result.mc"
            source.write_bytes(b"new chart")
            copy = shutil.copy2
            def race(original, temporary):
                result = copy(original, temporary)
                target.write_bytes(b"another program")
                return result
            with patch("osu_toolbox.quick.shutil.copy2", side_effect=race):
                with self.assertRaises(QuickCommandError):
                    _publish_chart_output(source, target)
            self.assertEqual(target.read_bytes(), b"another program")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["result.mc", "staged.mc"])
