"""Exercise delayed bridge responses against the shipped interface."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from osu_toolbox.gui.global_settings import global_settings_schema, shell_menu_settings_schema
from osu_toolbox.schema import GroupSpec, ParamSpec
from osu_toolbox.tools import ToolSpec

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required")
class TaskFormFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        params = (
            ParamSpec("path", "path", "path", "", "input", "", dialog_mode="open_file"),
            ParamSpec("value", "value", "text", "", "input", ""),
        )
        tools = [ToolSpec(id=key, title=key, description=key, category="test",
                          action_label="start", groups=(GroupSpec("input", "input"),),
                          params=params, result_kind="text").as_dict() for key in ("a", "b")]
        bootstrap = dict(project={}, tools=tools, global_settings_schema=global_settings_schema(),
                         shell_menu_schema=shell_menu_settings_schema(), settings_categories=[],
                         global_settings={}, shell_menu_settings={}, shell_menu={}, settings={},
                         settings_warning=None, active_run=None)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bootstrap.json"
            fixture.write_text(json.dumps(bootstrap), encoding="utf-8")
            completed = subprocess.run([NODE, str(HERE / "js" / "task_flow.js"), str(fixture)],
                                       capture_output=True, cwd=str(HERE.parent), timeout=20)
        if completed.returncode:
            raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
        cls.report = json.loads(completed.stdout.decode("utf-8"))

    def test_preflight_disables_repeated_submission_immediately(self):
        self.assertTrue(self.report["preparing"]["disabled"])
        self.assertEqual(self.report["preparing"]["calls"], 1)
        self.assertNotEqual(self.report["preparing"]["label"], "start")

    def test_switching_tools_drops_a_late_confirmation(self):
        self.assertTrue(self.report["stale_confirmation_hidden"])

    def test_confirmation_stays_bound_to_its_original_tool(self):
        self.assertEqual(self.report["confirmed_tool"], "a")

    def test_events_are_serialized_and_log_updates_are_batched(self):
        self.assertEqual(self.report["log"], dict(writes=1, lines=200, scrollTop=200, drains=1))
        self.assertIn("warning", self.report["partial_toast"])

    def test_late_file_and_metadata_responses_do_not_replace_user_edits(self):
        self.assertEqual(self.report["picker_value"], "keep")
        self.assertEqual(self.report["edited_value"], "my edit")

    def test_a_bridge_failure_is_visible_and_allows_retry(self):
        self.assertTrue(self.report["retry_enabled"])
        self.assertIn("disconnected", self.report["toasts"])

    def test_logs_from_another_running_tool_do_not_enter_the_current_tool(self):
        self.assertEqual(self.report["other_tool_log"], "")
        self.assertEqual(self.report["background_running"], "运行中")

    def test_entering_another_tool_resets_logs_and_scroll_position(self):
        self.assertEqual(self.report["switch_reset"], dict(text="", scrollTop=0))
        self.assertEqual(self.report["finished_entry"], "")

    def test_returning_to_the_same_tool_starts_with_fresh_logs(self):
        self.assertEqual(self.report["same_tool_reset"], dict(text="", scrollTop=0))
        self.assertEqual(self.report["fresh_log"], "second visit")

    def test_a_late_log_batch_cannot_restore_logs_after_reentry(self):
        self.assertEqual(self.report["stale_after_reentry"], "")
        self.assertEqual(self.report["current_after_reentry"], "new visit")

    def test_manual_clear_drops_pending_logs_and_accepts_new_logs(self):
        self.assertEqual(self.report["manual_clear"], dict(text="", scrollTop=0))
        self.assertEqual(self.report["fresh_after_clear"], "after clear")

    def test_background_completion_is_processed_without_polluting_logs(self):
        self.assertEqual(self.report["background_done"], dict(text="after clear", disabled=False))
