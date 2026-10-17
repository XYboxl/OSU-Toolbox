import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from osu_toolbox import cli
from osu_toolbox.tools import ConfirmationRequest, PreparedRun, ToolResult


class _FakeController:
    def __init__(self, output_file, confirmation=False):
        self.output_file = output_file
        self.confirmation = confirmation
        self.prepared = None
        self.run_count = 0

    def prepare(self, raw_params, context):
        self.prepared = (raw_params, context)
        request = None
        if self.confirmation:
            request = ConfirmationRequest("Overwrite", "Output exists")
        return PreparedRun({"output_file": self.output_file}, confirmation=request)

    def run(self, prepared, emit_event, cancel_event=None):
        self.run_count += 1
        emit_event("log", "fake render")
        return ToolResult("file", {"path": self.output_file})


class CliTests(unittest.TestCase):
    def test_parser_forwards_render_options(self):
        args = cli.build_parser().parse_args([
            "map.osu",
            "--skin-dir", "skin",
            "--fps", "120",
            "--n-parallel", "2",
            "--no-show-health-bar",
        ])

        params = cli._raw_params(args)

        self.assertEqual(params["beatmap_path"], "map.osu")
        self.assertEqual(params["skin_dir"], "skin")
        self.assertEqual(params["fps"], 120)
        self.assertEqual(params["n_parallel"], 2)
        self.assertFalse(params["show_health_bar"])

    def test_existing_output_requires_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "render.mp4"
            output.write_bytes(b"existing")
            controller = _FakeController(str(output), confirmation=True)
            definition = SimpleNamespace(
                controller=controller,
                spec=SimpleNamespace(result_kind="file"),
            )
            registry = SimpleNamespace(get=lambda tool_id: definition)
            stderr = io.StringIO()

            with patch.object(cli, "build_default_registry", return_value=registry):
                with redirect_stderr(stderr):
                    result = cli.main([
                        "map.osu",
                        "--skin-dir", "skin",
                        "--output", str(output),
                    ])

        self.assertEqual(result, 2)
        self.assertEqual(controller.run_count, 0)
        self.assertIn("--force", stderr.getvalue())

    def test_force_runs_controller_and_reports_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = str(Path(temporary) / "render.mp4")
            controller = _FakeController(output, confirmation=True)
            definition = SimpleNamespace(
                controller=controller,
                spec=SimpleNamespace(result_kind="file"),
            )
            registry = SimpleNamespace(get=lambda tool_id: definition)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch.object(cli, "build_default_registry", return_value=registry):
                with redirect_stdout(stdout):
                    with redirect_stderr(stderr):
                        result = cli.main([
                            "map.osu",
                            "--skin-dir", "skin",
                            "--output", output,
                            "--force",
                        ])

        self.assertEqual(result, 0)
        self.assertEqual(controller.run_count, 1)
        self.assertIn(f"Output: {output}", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
