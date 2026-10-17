"""Explorer notices are optional; actual failures always remain visible."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from osu_toolbox.gui.global_settings import (
    default_global_settings, global_settings_schema, normalize_global_settings,
)
from osu_toolbox.gui.settings import SettingsStore
from osu_toolbox.quick import _load_global_settings, format_shell_message


class ShellNoticePolicyTests(unittest.TestCase):
    def test_success_is_silent_with_either_setting(self):
        for enabled in (False, True):
            self.assertIsNone(format_shell_message(
                {"status": "ok", "outputs": ["result.osu"]}, show_notices=enabled,
            ))

    def test_nonfatal_notices_respect_the_setting(self):
        for key in ("warnings", "missing_resources", "renamed_members", "skipped_archives"):
            with self.subTest(key=key):
                payload = {key: ["资源已调整"]}
                self.assertIsNone(format_shell_message(payload, show_notices=False))
                message, icon = format_shell_message(payload, show_notices=True)
                self.assertIn("资源已调整", message)
                self.assertEqual(icon, 0x30)

    def test_failure_is_visible_with_either_setting(self):
        for enabled in (False, True):
            message, icon = format_shell_message(
                {"status": "error", "message": "无法读取谱面"}, 2, show_notices=enabled,
            )
            self.assertEqual(message, "无法读取谱面")
            self.assertEqual(icon, 0x10)

    def test_partial_failure_is_an_error_even_with_a_zero_exit_code(self):
        message, icon = format_shell_message(
            {"status": "ok", "errors": ["音乐变速失败"], "warnings": ["普通说明"]},
            show_notices=False,
        )
        self.assertIn("结果不完整", message)
        self.assertIn("音乐变速失败", message)
        self.assertNotIn("普通说明", message)
        self.assertEqual(icon, 0x10)

    def test_cancelling_does_not_show_another_dialog(self):
        for enabled in (False, True):
            self.assertIsNone(format_shell_message(
                {"status": "error", "code": "cancelled", "message": "已取消变速"},
                2, show_notices=enabled,
            ))

    def test_many_notices_are_deduplicated_and_bounded(self):
        message, _icon = format_shell_message({
            "warnings": ["一", "二", "三", "四", "五"],
            "missing_resources": ["一"],
            "outputs": ["a"] * 100,
        })
        self.assertEqual(message, "已完成，请留意：\n一\n二\n三\n另有 2 项。")
        self.assertLessEqual(message.count("\n"), 4)
        self.assertNotIn("结果位置", message)

    def test_audio_error_keeps_the_filename_and_omits_repeated_explanation(self):
        message, _icon = format_shell_message({
            "errors": [
                "hard.osu 的音乐未变速：无法读取音频采样率：bad.mp3；该文件可能不是可解码的音频",
            ],
        }, show_notices=False)
        self.assertEqual(message, "部分内容处理失败，结果不完整。\n音频无法读取：bad.mp3")

    def test_existing_output_error_has_a_short_actionable_message(self):
        message, _icon = format_shell_message(
            {"code": "output_exists", "message": "long --force explanation"}, 2,
            show_notices=False,
        )
        self.assertEqual(message, "输出已存在，未覆盖。\n请重命名已有结果后重试。")

    def test_non_string_progress_payloads_are_not_shown_as_notices(self):
        self.assertIsNone(format_shell_message({"warnings": [{"fraction": 1.0}, "", None]}))

    def test_unstructured_failures_have_a_readable_fallback(self):
        message, icon = format_shell_message([], 1, show_notices=False)
        self.assertEqual(message, "操作失败，请检查输入。")
        self.assertEqual(icon, 0x10)

    def test_very_long_diagnostics_are_bounded(self):
        message, _icon = format_shell_message({"warnings": ["说明" * 1000]})
        self.assertLess(len(message), 200)
        self.assertTrue(message.endswith("…"))


class ShellNoticeSettingsTests(unittest.TestCase):
    def test_older_settings_default_to_enabled_and_keep_path_normalization(self):
        settings = normalize_global_settings({"osu_directory": "./songs"})
        self.assertTrue(settings["shell_menu_notifications"])
        self.assertEqual(settings["osu_directory"], str(Path("songs").resolve()))
        self.assertTrue(default_global_settings()["shell_menu_notifications"])

    def test_boolean_normalization_does_not_treat_the_switch_as_a_path(self):
        for raw, expected in ((False, False), (True, True), ("false", False), ("true", True)):
            with self.subTest(raw=raw):
                self.assertIs(normalize_global_settings({
                    "shell_menu_notifications": raw,
                })["shell_menu_notifications"], expected)

    def test_the_switch_belongs_to_general_settings_and_not_menu_registration(self):
        schema = global_settings_schema()
        spec = next(param for param in schema["params"] if param["key"] == "shell_menu_notifications")
        self.assertEqual(spec["group"], "general")
        self.assertEqual(spec["kind"], "bool")
        self.assertTrue(spec["persist"])
        self.assertEqual(len(schema["shell_menu"]["params"]), 3)

    def test_save_restart_menu_read_and_reset_share_the_same_value(self):
        from osu_toolbox.gui.bridge import Api
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            menu = Mock()
            menu.executable_path = str(Path(directory) / "osu-toolbox-menu.exe")
            menu.status.return_value = {"state": "disabled"}
            menu.uninstall_all.return_value = {"ok": True}
            api = Api(directory, settings_store=SettingsStore(path), shell_menu_manager=menu)
            saved = api.save_global_settings({"shell_menu_notifications": False})
            self.assertEqual(saved["status"], "ok")
            self.assertIs(json.loads(path.read_text(encoding="utf-8"))["global"]["shell_menu_notifications"], False)
            restarted = Api(directory, settings_store=SettingsStore(path), shell_menu_manager=menu)
            self.assertIs(restarted.get_bootstrap()["global_settings"]["shell_menu_notifications"], False)
            with patch("osu_toolbox.gui.settings.default_settings_path", return_value=path):
                self.assertIs(_load_global_settings()["shell_menu_notifications"], False)
                reset = restarted.reset_all_settings()
                self.assertEqual(reset["status"], "ok")
                self.assertTrue(reset["global_settings"]["shell_menu_notifications"])
                self.assertFalse(path.exists())
                self.assertTrue(_load_global_settings()["shell_menu_notifications"])
            menu.apply.assert_not_called()

    def test_damaged_setting_values_fall_back_without_crashing_the_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            SettingsStore(path).save_state({"shell_menu_notifications": "invalid"}, {})
            with patch("osu_toolbox.gui.settings.default_settings_path", return_value=path):
                self.assertEqual(_load_global_settings(), default_global_settings())

    def test_custom_speed_input_remains_available_when_notices_are_disabled(self):
        import io
        from osu_toolbox import quick
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "chart.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Metadata]\nTitle:Song\nVersion:Hard\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            settings = default_global_settings()
            settings["shell_menu_notifications"] = False
            with patch.object(quick, "_load_global_settings", return_value=settings), \
                 patch.object(quick, "_prompt_speed", return_value="1.2") as prompt:
                code = quick.main([
                    "--operation", "speed-change", "--prompt-speed", "--json",
                    "--dry-run", str(chart),
                ], stdout=output)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["speed"], 1.2)
            prompt.assert_called_once_with()
            self.assertEqual(list(Path(directory).iterdir()), [chart])


class ShellPartialFailureProtocolTests(unittest.TestCase):
    def test_one_failed_input_is_reported_when_other_inputs_succeed(self):
        from osu_toolbox import speed_change
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController
        chart_text = (
            "osu file format v14\n[General]\nMode:3\n"
            "[Metadata]\nTitle:Song\nVersion:Hard\n"
            "[Difficulty]\nCircleSize:4\n[HitObjects]\n"
            "64,192,1000,1,0,0:0:0:0:\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "good.osu", root / "bad.osu"]
            for source in sources:
                source.write_text(chart_text, encoding="utf-8")
            controller = SpeedChangeController()
            prepared = controller.prepare(
                {"input_paths": list(map(str, sources)), "speed": 1.1}, ToolContext(directory),
            )
            write_chart = speed_change._write_chart_text

            def write(path, *args, **kwargs):
                if Path(path).name.startswith("bad"):
                    raise PermissionError("无法写入结果")
                return write_chart(path, *args, **kwargs)

            with patch("osu_toolbox.speed_change._write_chart_text", side_effect=write):
                result = controller.run(prepared, lambda *_: None)
            self.assertTrue((root / "good 1.1x.osu").is_file())
            self.assertFalse((root / "bad 1.1x.osu").exists())
            message, icon = format_shell_message(result.data, show_notices=False)
            self.assertIn("bad.osu", message)
            self.assertIn("无法写入结果", message)
            self.assertEqual(icon, 0x10)
            for source in sources:
                self.assertEqual(source.read_text(encoding="utf-8"), chart_text)


if __name__ == "__main__":
    unittest.main()
