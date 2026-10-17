"""Headless regression tests for the registry-driven pywebview GUI."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib.metadata import PackageNotFoundError, metadata
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class RenderToolSchemaTests(unittest.TestCase):
    def setUp(self):
        from osu_toolbox import config
        from osu_toolbox.tools.render_video import RENDER_PARAM_KEYS, RENDER_PARAMS

        self.config = config
        self.keys = RENDER_PARAM_KEYS
        self.specs = RENDER_PARAMS

    def test_schema_has_exactly_27_unique_keys(self):
        self.assertEqual(len(self.keys), 27)
        self.assertEqual(len(set(self.keys)), 27)

    def test_schema_defaults_drive_config_defaults(self):
        expected = {spec.key: spec.default for spec in self.specs}
        self.assertEqual(self.config.DEFAULT_RENDER_PARAMS, expected)

    def test_tool_definition_is_json_serializable(self):
        from osu_toolbox.tools.render_video import RENDER_VIDEO_TOOL

        payload = RENDER_VIDEO_TOOL.spec.as_dict()
        self.assertEqual(payload["id"], "render")
        self.assertEqual(payload["concurrency"], "exclusive")
        self.assertEqual(payload["result_kind"], "file")
        json.dumps(payload, ensure_ascii=False)

    def test_letterbox_schema_covers_three_values(self):
        spec = next(item for item in self.specs if item.key == "letterbox_in_breaks")
        self.assertEqual({choice.value for choice in spec.choices}, {None, True, False})
        self.assertEqual(len({choice.label for choice in spec.choices}), 3)

    def test_song_progress_schema_covers_all_stable_modes(self):
        spec = next(item for item in self.specs if item.key == "show_song_progress")
        self.assertEqual(spec.kind, "choice")
        self.assertEqual(spec.default, self.config.SONG_PROGRESS_PIE)
        self.assertEqual(
            {choice.value for choice in spec.choices},
            self.config.SONG_PROGRESS_MODES,
        )

        from osu_toolbox import coerce_param

        self.assertEqual(coerce_param(spec, False), self.config.SONG_PROGRESS_OFF)
        self.assertEqual(coerce_param(spec, True), self.config.SONG_PROGRESS_PIE)

    def test_custom_skin_directory_is_required(self):
        spec = next(spec for spec in self.specs if spec.key == "skin_dir")
        self.assertTrue(spec.required)

    def test_prepare_normalizes_contract_and_types(self):
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.render_video import RenderVideoController

        with tempfile.TemporaryDirectory() as temp_dir:
            beatmap = Path(temp_dir) / "map.osu"
            replay = Path(temp_dir) / "play.osr"
            skin = Path(temp_dir) / "skin"
            beatmap.write_text("", encoding="utf-8")
            replay.write_text("", encoding="utf-8")
            skin.mkdir()
            prepared = RenderVideoController().prepare({
                "beatmap_path": str(beatmap),
                "replay_path": str(replay),
                "skin_dir": str(skin),
                "key_count": "4",
                "fps": "120",
                "scroll_speed": "100",
                "background_dim": "-1",
                "letterbox_in_breaks": "on",
                "show_health_bar": False,
                "show_song_progress": "bottom_right",
                "n_parallel": "0",
            }, ToolContext(temp_dir))
        params = prepared.params
        self.assertEqual(set(params), set(self.keys) | {"replay_directories"})
        self.assertEqual(params["key_count"], 4)
        self.assertEqual(params["fps"], 120)
        self.assertEqual(params["scroll_speed"], 40.0)
        self.assertEqual(params["background_dim"], 0.0)
        self.assertIs(params["letterbox_in_breaks"], True)
        self.assertIs(params["show_health_bar"], False)
        self.assertEqual(params["show_song_progress"], "bottom_right")
        self.assertEqual(params["n_parallel"], 1)
        self.assertEqual(params["skin_dir"], str(skin.resolve()))

    def test_prepare_reports_field_for_missing_beatmap(self):
        from osu_toolbox import ParamValidationError
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.render_video import RenderVideoController

        with self.assertRaises(ParamValidationError) as context:
            RenderVideoController().prepare({}, ToolContext(str(ROOT)))
        self.assertEqual(context.exception.key, "beatmap_path")

    def test_prepare_reports_field_for_missing_skin(self):
        from osu_toolbox import ParamValidationError
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.render_video import RenderVideoController

        with tempfile.TemporaryDirectory() as temp_dir:
            beatmap = Path(temp_dir) / "map.osu"
            beatmap.write_text("", encoding="utf-8")
            with self.assertRaises(ParamValidationError) as context:
                RenderVideoController().prepare(
                    {"beatmap_path": str(beatmap)},
                    ToolContext(temp_dir),
                )
        self.assertEqual(context.exception.key, "skin_dir")

    def test_persistence_excludes_every_path(self):
        from osu_toolbox import default_params, persistent_params

        params = default_params(self.specs)
        saved = persistent_params(params, self.specs)
        path_keys = {
            spec.key for spec in self.specs if spec.dialog_mode is not None
        }
        self.assertFalse(path_keys & set(saved))

    def test_beatmap_prefill_uses_existing_background_and_key_count(self):
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.render_video import RenderVideoController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            background = root / "bg.jpg"
            background.write_bytes(b"image")
            beatmap = root / "map.osu"
            beatmap.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:7\n"
                "[Events]\n0,0,\"bg.jpg\",0,0\n",
                encoding="utf-8",
            )
            result = RenderVideoController().prefill(
                {"beatmap_path": str(beatmap)},
                "beatmap_path",
                ToolContext(temp_dir),
            )
        self.assertEqual(result.params["key_count"], 7)
        self.assertEqual(result.params["background_path"], str(background))

    def test_missing_background_is_treated_as_empty_during_prepare(self):
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.render_video import RenderVideoController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            beatmap = root / "map.osu"
            skin = root / "skin"
            beatmap.write_text("", encoding="utf-8")
            skin.mkdir()
            prepared = RenderVideoController().prepare({
                "beatmap_path": str(beatmap),
                "skin_dir": str(skin),
                "background_path": str(root / "missing.png"),
            }, ToolContext(temp_dir))
        self.assertIsNone(prepared.params["background_path"])
        self.assertTrue(any("背景图不存在" in warning for warning in prepared.warnings))

    def test_render_output_defaults_to_context_output_root(self):
        from osu_toolbox.tools import ToolContext
        from osu_toolbox.tools.render_video import RenderVideoController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "charts"
            source_dir.mkdir()
            beatmap = source_dir / "map.osu"
            skin = root / "skin"
            beatmap.write_text("", encoding="utf-8")
            skin.mkdir()
            prepared = RenderVideoController().prepare({
                "beatmap_path": str(beatmap),
                "skin_dir": str(skin),
            }, ToolContext(temp_dir, output_root=temp_dir))
        self.assertEqual(prepared.params["output_file"], str(root / "output.mp4"))


class ProjectInfoTests(unittest.TestCase):
    def assert_display_copy_is_centralized(self, info, project_info):
        expected = {
            "name": project_info.PROJECT_NAME,
            "tagline": project_info.PROJECT_TAGLINE,
            "focus": project_info.PROJECT_FOCUS,
            "qq": project_info.PROJECT_QQ,
            "status": project_info.PROJECT_STATUS,
            "future_note": project_info.PROJECT_FUTURE_NOTE,
        }
        actual = {field: getattr(info, field) for field in expected}
        self.assertEqual(actual, expected)
        self.assertTrue(all(value.strip() for value in actual.values()))

    def test_project_info_reads_installed_package_metadata(self):
        from osu_toolbox import project_info

        package_metadata = {
            "Version": "9.8.7",
            "Summary": "Package summary fixture",
            "Author": "Package Author",
            "Author-email": "Release Contact <release@example.com>",
        }
        with patch.object(project_info, "_read_metadata", return_value=package_metadata):
            info = project_info.get_project_info()

        self.assertEqual(info.version, "9.8.7")
        self.assertEqual(info.summary, "Package summary fixture")
        self.assertEqual(info.author_name, "Package Author")
        self.assertEqual(info.author_email, "release@example.com")
        self.assert_display_copy_is_centralized(info, project_info)

    def test_project_info_falls_back_without_installed_metadata(self):
        from osu_toolbox import project_info

        with patch.object(project_info, "_read_metadata", return_value=None):
            info = project_info.get_project_info()
        self.assertEqual(info.version, "0+unknown")
        self.assertEqual(info.summary, project_info._FALLBACK_SUMMARY)
        self.assertEqual(info.author_name, "")
        self.assertEqual(info.author_email, "")
        self.assert_display_copy_is_centralized(info, project_info)


class SettingsStoreTests(unittest.TestCase):
    def test_v3_round_trip_preserves_global_and_tool_settings(self):
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            store = SettingsStore(path)
            settings = {"render": {"fps": 144}, "future": {"mode": "fast"}}
            global_settings = {
                "osu_directory": str(Path(temp_dir) / "osu!"),
                "malody_directory": str(Path(temp_dir) / "Malody"),
            }
            self.assertIsNone(store.save_state(global_settings, settings))
            state, warning = store.load_state()
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(warning)
        self.assertEqual(state, {"global": global_settings, "tools": settings})
        self.assertEqual(payload["version"], 3)
        self.assertEqual(payload["global"], global_settings)
        self.assertEqual(payload["tools"], settings)

    def test_v1_loads_and_next_save_migrates_to_v3(self):
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text(
                json.dumps({"version": 1, "render": {"fps": 120}}),
                encoding="utf-8",
            )
            store = SettingsStore(path)
            loaded, warning = store.load()
            self.assertIsNone(warning)
            self.assertEqual(loaded, {"render": {"fps": 120}})
            self.assertIsNone(store.save(loaded))
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {
            "version": 3,
            "global": {},
            "tools": {"render": {"fps": 120}},
        })

    def test_v2_loads_with_empty_global_settings(self):
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text(
                json.dumps({"version": 2, "tools": {"render": {"fps": 120}}}),
                encoding="utf-8",
            )
            state, warning = SettingsStore(path).load_state()
        self.assertIsNone(warning)
        self.assertEqual(state, {
            "global": {},
            "tools": {"render": {"fps": 120}},
        })

    def test_reset_removes_settings_file(self):
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            store = SettingsStore(path)
            self.assertIsNone(store.save_state({"osu_directory": "test"}, {}))
            self.assertTrue(path.is_file())
            self.assertIsNone(store.reset())
            self.assertFalse(path.exists())

    def test_corrupt_and_unknown_versions_fall_back(self):
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            store = SettingsStore(path)
            path.write_text("not json", encoding="utf-8")
            loaded, warning = store.load()
            self.assertEqual(loaded, {})
            self.assertIn("损坏", warning)
            path.write_text('{"version": 99, "tools": {}}', encoding="utf-8")
            loaded, warning = store.load()
            self.assertEqual(loaded, {})
            self.assertIn("版本", warning)


class _FakeSettingsStore:
    def __init__(self, loaded=None, warning=None):
        self.loaded = loaded or {}
        self.warning = warning
        self.saved = None

    def load(self):
        return dict(self.loaded), self.warning

    def save(self, settings):
        self.saved = dict(settings)
        return None


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.beatmap = self.root / "map.osu"
        self.beatmap.write_text("", encoding="utf-8")

    def _api(self, store=None):
        from osu_toolbox.gui.bridge import Api

        return Api(str(self.root), settings_store=store or _FakeSettingsStore())

    def test_bootstrap_contains_registered_tools_and_settings(self):
        api = self._api(_FakeSettingsStore({"render": {"fps": 144}}, "settings warning"))
        data = api.get_bootstrap()
        self.assertEqual(
            [tool["id"] for tool in data["tools"]],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change"],
        )
        self.assertEqual(len(data["tools"][0]["params"]), 27)
        self.assertEqual(len(data["tools"][1]["params"]), 16)
        self.assertEqual(len(data["tools"][2]["params"]), 17)
        self.assertEqual(len(data["tools"][4]["params"]), 9)
        self.assertEqual(data["project"]["version"], metadata("osu-toolbox")["Version"])
        self.assertEqual(data["project"]["author_name"], "Difficile")
        self.assertEqual(data["settings"]["render"]["fps"], 144)
        self.assertEqual(data["global_settings"], {
            "osu_directory": None,
            "malody_directory": None,
            "shell_menu_notifications": True,
            "speed_keep_pitch": False,
            "speed_file_suffix": " {num}x",
            "speed_difficulty_marker": " {num}x",
            "chart_overall_difficulty": 8.0,
            "chart_hp_drain_rate": 8.0,
        })
        self.assertEqual(
            [group["label"] for group in data["global_settings_schema"]["groups"]],
            ["通用"],
        )
        self.assertEqual(
            [
                group["label"]
                for group in data["global_settings_schema"]["speed_tool"]["groups"]
            ],
            ["变速工具"],
        )
        self.assertEqual(
            [
                group["label"]
                for group in data["global_settings_schema"]["chart_tool"]["groups"]
            ],
            ["转谱工具"],
        )
        self.assertEqual(
            [group["label"] for group in data["settings_categories"]],
            ["通用", "变速工具", "转谱工具", "右键菜单"],
        )
        self.assertEqual(data["settings_warning"], "settings warning")
        self.assertIsNone(data["active_run"])

    def test_bootstrap_migrates_legacy_song_progress_bools(self):
        for legacy, expected in ((False, "off"), (True, "pie")):
            with self.subTest(legacy=legacy):
                api = self._api(_FakeSettingsStore({
                    "render": {"show_song_progress": legacy},
                }))
                data = api.get_bootstrap()
                self.assertEqual(
                    data["settings"]["render"]["show_song_progress"],
                    expected,
                )

    def test_validation_error_identifies_field(self):
        api = self._api()
        result = api.start_tool("render", {
            "beatmap_path": str(self.root / "missing.osu"),
        })
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "validation_error")
        self.assertEqual(result["field"], "beatmap_path")

    def test_file_dialog_is_scoped_to_tool_schema(self):
        api = self._api()

        class FakeWindow:
            def create_file_dialog(self, dialog_type, **kwargs):
                self.dialog_type = dialog_type
                self.kwargs = kwargs
                return (str(self_path),)

        self_path = self.beatmap
        window = FakeWindow()
        api._set_window(window)
        fake_webview = SimpleNamespace(OPEN_DIALOG=1, SAVE_DIALOG=2, FOLDER_DIALOG=3)
        with patch.dict(sys.modules, {"webview": fake_webview}):
            result = api.pick_path("render", "beatmap_path")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(self.beatmap))
        self.assertEqual(window.kwargs["file_types"], ("谱面 (*.osu)",))
        self.assertFalse(api.pick_path("render", "fps")["ok"])
        self.assertFalse(api.pick_path("missing", "beatmap_path")["ok"])
        with patch.dict(sys.modules, {"webview": fake_webview}):
            global_result = api.pick_global_path("osu_directory")
        self.assertTrue(global_result["ok"])
        self.assertEqual(window.dialog_type, fake_webview.FOLDER_DIALOG)
        self.assertFalse(api.pick_global_path("missing")["ok"])

    def test_file_dialog_filters_match_pywebview_syntax(self):
        from webview.util import parse_file_type

        from osu_toolbox.gui.bridge import _dialog_file_types
        from osu_toolbox.tools.registry import build_default_registry

        for definition in build_default_registry().list_definitions():
            for spec in definition.spec.params:
                for file_type in _dialog_file_types(spec.filters):
                    parse_file_type(file_type)

    def test_prefill_api_returns_schema_scoped_values(self):
        self.beatmap.write_text(
            "osu file format v14\n[General]\nMode:3\n"
            "[Difficulty]\nCircleSize:6\n",
            encoding="utf-8",
        )
        result = self._api().prefill_tool(
            "render",
            {"beatmap_path": str(self.beatmap)},
            "beatmap_path",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["params"]["key_count"], 6)
        self.assertNotIn("unknown", result["params"])

    def test_main_runtime_uses_application_root_for_outputs(self):
        api = self._api()
        self.assertEqual(api._runtime._context.output_root, str(self.root))

    def test_global_settings_save_updates_tool_context(self):
        osu_directory = self.root / "osu!"
        malody_directory = self.root / "Malody"
        result = self._api().save_global_settings({
            "osu_directory": str(osu_directory),
            "malody_directory": str(malody_directory),
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["global_settings"]["osu_directory"], str(osu_directory))

    def test_reset_all_settings_clears_global_tools_and_file(self):
        from osu_toolbox.gui.bridge import Api
        from osu_toolbox.gui.settings import SettingsStore
        from osu_toolbox.shell_menu import ShellMenuManager

        settings_path = self.root / "settings.json"
        store = SettingsStore(settings_path)
        store.save_state(
            {"osu_directory": str(self.root / "osu!")},
            {"render": {"fps": 144}},
        )
        api = Api(
            str(self.root),
            settings_store=store,
            shell_menu_manager=ShellMenuManager(
                str(self.root / "osu-toolbox-menu.exe"),
                winreg_module=object(),
            ),
        )
        result = api.reset_all_settings()
        bootstrap = api.get_bootstrap()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(settings_path.exists())
        self.assertTrue(all(not values for values in bootstrap["settings"].values()))
        self.assertEqual(bootstrap["global_settings"], {
            "osu_directory": None,
            "malody_directory": None,
            "shell_menu_notifications": True,
            "speed_keep_pitch": False,
            "speed_file_suffix": " {num}x",
            "speed_difficulty_marker": " {num}x",
            "chart_overall_difficulty": 8.0,
            "chart_hp_drain_rate": 8.0,
        })


class RuntimeAndAssetTests(unittest.TestCase):
    def test_runtime_override_is_available(self):
        from osu_toolbox.gui.runtime import webview2_runtime_available

        with patch.dict(os.environ, {"OSU_TOOLBOX_SKIP_WEBVIEW2_CHECK": "1"}):
            self.assertTrue(webview2_runtime_available())

    def test_missing_runtime_exits_before_importing_webview(self):
        from osu_toolbox.gui import web_app

        stderr = io.StringIO()
        with patch.object(web_app, "webview2_runtime_available", return_value=False):
            with redirect_stderr(stderr):
                self.assertEqual(web_app.main(), 1)
        self.assertIn("WebView2 Runtime", stderr.getvalue())

    def test_frozen_application_root_is_executable_directory(self):
        from osu_toolbox.gui import web_app

        executable = ROOT / "bundle" / "osu-toolbox.exe"
        with patch.object(web_app.sys, "frozen", True, create=True):
            with patch.object(web_app.sys, "executable", str(executable)):
                self.assertEqual(web_app._application_root(), executable.parent)
                self.assertEqual(web_app._search_root(), executable.parent)
                self.assertEqual(
                    web_app._shell_executable(),
                    executable.parent / "osu-toolbox-menu.exe",
                )

    def test_development_shell_entry_is_next_to_python(self):
        from osu_toolbox.gui import web_app

        with patch.object(web_app.sys, "frozen", False, create=True):
            self.assertEqual(
                web_app._shell_executable(),
                Path(web_app.sys.executable).resolve().parent / "osu-toolbox-menu.exe",
            )

    def test_static_assets_are_packaged_and_tool_driven(self):
        assets = files("osu_toolbox.gui").joinpath("assets")
        html = assets.joinpath("index.html").read_text(encoding="utf-8")
        css = assets.joinpath("style.css").read_text(encoding="utf-8")
        js = assets.joinpath("app.js").read_text(encoding="utf-8")
        self.assertIn('href="style.css?v=', html)
        self.assertIn('src="app.js?v=', html)
        self.assertIn("--accent", css)
        self.assertIn("window.addEventListener('pywebviewready', initialize)", js)
        self.assertIn("关于本项目", html)
        self.assertIn('id="tool-list"', html)
        self.assertIn('id="tool-page"', html)
        self.assertIn('id="about-page"', html)
        self.assertIn('id="settings-page"', html)
        self.assertIn('id="reset-settings-button"', html)
        self.assertIn("你确认要重置所有设置吗？", js)
        self.assertIn("state.bootstrap.tools", js)
        self.assertIn("window.pywebview.api.start_tool", js)
        self.assertIn("window.pywebview.api.cancel_tool", js)
        self.assertIn("window.pywebview.api.prefill_tool", js)
        self.assertIn("window.pywebview.api.save_global_settings", js)
        self.assertIn("window.pywebview.api.reset_all_settings", js)
        self.assertIn("input.multiple = Boolean(spec.multiple)", js)
        self.assertIn("input.selectedOptions", js)
        # A picker result must be joined before it reaches an input's value: an
        # array assigned to ``input.value`` stringifies with no separator, which
        # turned five chosen folders into one impossible path.
        self.assertIn("function dialogPathValue(result)", js)
        self.assertIn("dialogPathValue(result.path)", js)
        self.assertNotIn(": result.path;", js)
        # Optional/required marking and the empty-field hint must stay wired up:
        # without them a form of optional fields reads as if everything is
        # mandatory and blank inputs look like mistakes.  Only mandatory fields
        # carry a marker; an optional label is plain text plus the hint.
        self.assertIn("（可选，留空用默认值）", js)
        self.assertIn("input.placeholder = spec.placeholder", js)
        self.assertIn("function fieldLabel(spec, group)", js)
        self.assertNotIn("- ${spec.label}", js)
        self.assertNotIn("- ${group.label}", js)
        self.assertIn('id="cancel-button"', html)
        self.assertNotIn("start_render", js)
        self.assertNotIn("render-card", html)
        self.assertNotIn('id="render-page"', html)
        self.assertNotIn("Difficile", html)
        self.assertNotIn("753831679", html)
        self.assertNotIn("xyboxl@163.com", html)
        self.assertNotIn("toolId === 'render'", js)

    def test_project_entry_point_targets_gui_without_typer(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('osu-toolbox = "osu_toolbox.gui.app:main"', project)
        self.assertIn('name = "Difficile"', project)
        self.assertNotIn('"typer', project.lower())
        cli_dir = ROOT / "osu_toolbox" / "cli"
        self.assertFalse(any(cli_dir.glob("*.py")))


if __name__ == "__main__":
    unittest.main()
