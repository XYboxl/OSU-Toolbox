"""The settings page must save what the user typed.

``buildSettingsPage`` renders fields from three schema sections but registers
only some of them, and a field that is rendered without being registered is read
back from the stale bootstrap copy when the form is submitted.  The save then
succeeds and reports 设置已保存 while the edit silently reverts, which is exactly
what happened to 变速工具's 文件名标识 / 难度名标识 / 保持音调.

These tests run the real ``app.js`` in node against a minimal DOM, so they cover
the shipped artifact rather than a copy of its logic.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osu_toolbox.gui.global_settings import (  # noqa: E402
    global_settings_schema,
    shell_menu_settings_schema,
)

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "js" / "settings_flow.js"
NODE = shutil.which("node")


def _bootstrap():
    """A bootstrap payload shaped exactly like ``Bridge.get_bootstrap``."""
    return {
        "project": {},
        "tools": [],
        "global_settings_schema": global_settings_schema(),
        "shell_menu_schema": shell_menu_settings_schema(),
        "settings_categories": [],
        "global_settings": {
            "osu_directory": None,
            "malody_directory": None,
            "shell_menu_notifications": True,
            "speed_keep_pitch": False,
            "speed_file_suffix": " {num}x",
            "speed_difficulty_marker": " {num}x",
            "chart_overall_difficulty": 8.0,
            "chart_hp_drain_rate": 8.0,
            "shell_menu": {
                "shell_menu_replay_generate": False,
                "shell_menu_chart_convert": True,
                "shell_menu_speed_change": True,
            },
        },
        "shell_menu_settings": {
            "shell_menu_replay_generate": False,
            "shell_menu_chart_convert": True,
            "shell_menu_speed_change": True,
        },
        "shell_menu": {"scope": "当前用户", "message": "已注册", "items": []},
        "settings": {},
        "settings_warning": None,
        "active_run": None,
    }


@unittest.skipUnless(NODE, "需要 node 才能驱动界面脚本")
class SettingsFormFlowTests(unittest.TestCase):
    """驱动真实 app.js 的设置表单往返。"""

    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._temp.cleanup)
        path = Path(cls._temp.name) / "bootstrap.json"
        path.write_text(json.dumps(_bootstrap(), ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [NODE, str(DRIVER), str(path)],
            capture_output=True,
            cwd=str(HERE.parent),
        )
        if completed.returncode != 0:
            raise AssertionError(
                "界面脚本驱动失败："
                + completed.stderr.decode("utf-8", errors="replace")
            )
        cls.report = json.loads(completed.stdout.decode("utf-8"))

    def test_the_form_reports_every_settings_category(self):
        self.assertEqual(
            self.report["categories"],
            ["通用", "变速工具", "转谱工具", "右键菜单"],
        )

    def test_the_speed_tool_fields_are_rendered(self):
        self.assertEqual(
            sorted(self.report["speed_fields_rendered"]),
            [
                "setting-speed_difficulty_marker",
                "setting-speed_file_suffix",
                "setting-speed_keep_pitch",
            ],
        )

    def test_the_speed_tool_fields_show_the_stored_values(self):
        before = self.report["speed_before"]
        self.assertEqual(before["file_suffix"]["value"], " {num}x")
        self.assertEqual(before["difficulty_marker"]["value"], " {num}x")
        self.assertFalse(before["keep_pitch"]["checked"])

    def test_editing_the_speed_tool_fields_reaches_the_backend(self):
        """The bug: the save returned 设置已保存 while sending the old values."""
        saved = self.report["speed_saved"]
        self.assertEqual(saved["speed_file_suffix"], "_{num}x")
        self.assertEqual(saved["speed_difficulty_marker"], " [{num}x]")
        self.assertTrue(saved["speed_keep_pitch"])

    def test_the_edited_values_stay_in_the_form(self):
        after = self.report["speed_after"]
        self.assertEqual(after["file_suffix"]["value"], "_{num}x")
        self.assertEqual(after["difficulty_marker"]["value"], " [{num}x]")
        self.assertTrue(after["keep_pitch"]["checked"])

    def test_leaving_the_category_keeps_an_unsaved_edit(self):
        self.assertEqual(self.report["speed_revisited"]["value"], "__{num}x")

    def test_the_other_categories_still_save_their_own_fields(self):
        general = self.report["general_saved"]
        self.assertEqual(general["osu_directory"], "D:\\osu!")
        # The categories share one form, so one save submits all of them.
        self.assertEqual(general["speed_file_suffix"], "__{num}x")
        self.assertTrue(general["speed_keep_pitch"])
        self.assertFalse(self.report["shell_saved"]["shell_menu_chart_convert"])

    def test_the_menu_notice_switch_saves_and_survives_category_changes(self):
        self.assertEqual(self.report["notifications_before"]["type"], "checkbox")
        self.assertTrue(self.report["notifications_before"]["checked"])
        self.assertFalse(self.report["general_saved"]["shell_menu_notifications"])
        self.assertFalse(self.report["notifications_after"]["checked"])
        self.assertFalse(self.report["notifications_revisited"]["checked"])
        self.assertFalse(self.report["chart_saved"]["shell_menu_notifications"])

    def test_the_chart_tool_difficulty_fields_are_rendered_and_saved(self):
        """A field that renders without being registered silently reverts."""
        self.assertEqual(
            sorted(self.report["chart_fields_rendered"]),
            [
                "setting-chart_hp_drain_rate",
                "setting-chart_overall_difficulty",
            ],
        )
        self.assertEqual(self.report["chart_before"]["od"]["value"], "8")
        self.assertEqual(self.report["chart_before"]["hp"]["value"], "8")
        saved = self.report["chart_saved"]
        self.assertEqual(saved["chart_overall_difficulty"], "9")
        self.assertEqual(saved["chart_hp_drain_rate"], "7.5")
        # The new values stay in the form instead of reverting to bootstrap.
        self.assertEqual(self.report["chart_after"]["od"]["value"], "9")
        self.assertEqual(self.report["chart_after"]["hp"]["value"], "7.5")

    def test_menu_edits_apply_only_when_saved(self):
        self.assertEqual(self.report["shell_calls_before_edit"], 0)
        self.assertEqual(self.report["shell_calls_after_edit"], 0)
        self.assertEqual(self.report["shell_calls_after_save"], 1)
        self.assertFalse(self.report["shell_draft_revisited"]["checked"])
        self.assertEqual(len(self.report["shell_fields"]), 3)
        self.assertNotIn("setting-shell_menu_enabled", self.report["shell_fields"])
        self.assertFalse(self.report["shell_save_hidden"])

    def test_uninstall_clears_checkboxes_and_other_saves_do_not_register_again(self):
        self.assertTrue(all(not value["checked"] for value in self.report["shell_after_uninstall"].values()))
        self.assertEqual(self.report["shell_calls_after_general_save"], self.report["shell_calls_after_save"])
        self.assertEqual(self.report["uninstall_calls"], 1)
        self.assertIn("右键菜单已卸载", self.report["toasts"])

    def test_a_failed_save_keeps_the_draft_for_retry_and_reports_the_error(self):
        self.assertTrue(self.report["shell_after_error"]["checked"])
        self.assertFalse(self.report["shell_stored_after_error"])
        self.assertIn("注册表写入被拒绝", self.report["toasts"])

    def test_each_save_reports_success(self):
        self.assertEqual(self.report["save_calls"], 4)
        self.assertIn("设置已保存", self.report["toasts"])
        self.assertIn("右键菜单设置已保存", self.report["toasts"])


if __name__ == "__main__":
    unittest.main()
