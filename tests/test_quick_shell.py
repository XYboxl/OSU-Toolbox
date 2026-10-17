import io
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from osu_toolbox.quick import (
    CONTEXT_SHELL,
    _absolute_input,
    _publish_chart_output,
    main,
)
import osu_toolbox.shell_menu as shell_menu
from osu_toolbox.shell_menu import (
    DIRECTORY_BACKGROUND_KEY_PATH,
    DIRECTORY_KEY_PATH,
    ShellMenuManager,
    shell_menu_command,
    shell_menu_schema,
)


class _Key:
    def __init__(self, path):
        self.path = path

    def Close(self):
        return None


class _FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 1
    KEY_READ = 2
    REG_SZ = 1
    KEY_WOW64_64KEY = 0

    def __init__(self):
        self.values = {}

    def CreateKeyEx(self, _root, path, *_args):
        self.values.setdefault(path, {})
        return _Key(path)

    def OpenKey(self, _root, path, *_args):
        if path not in self.values:
            raise FileNotFoundError(path)
        return _Key(path)

    def SetValueEx(self, key, name, _reserved, _kind, value):
        self.values.setdefault(key.path, {})[name] = value

    def QueryValueEx(self, key, name):
        if name not in self.values.get(key.path, {}):
            raise FileNotFoundError(name)
        return self.values[key.path][name], self.REG_SZ

    def QueryInfoKey(self, key):
        prefix = key.path + "\\"
        children = {
            path[len(prefix):].split("\\", 1)[0]
            for path in self.values
            if path.startswith(prefix) and path[len(prefix):]
        }
        return len(children), 0, 0

    def EnumKey(self, key, index):
        prefix = key.path + "\\"
        children = sorted({
            path[len(prefix):].split("\\", 1)[0]
            for path in self.values
            if path.startswith(prefix) and path[len(prefix):]
        })
        if index >= len(children):
            raise OSError(index)
        return children[index]

    def DeleteKeyEx(self, _root, path, *_args):
        if any(item != path and item.startswith(path + "\\") for item in self.values):
            raise OSError("has children")
        self.values.pop(path, None)


class QuickCommandTests(unittest.TestCase):
    def test_chart_publish_falls_back_when_replace_crosses_drives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged.mc"
            destination = root / "published.mc"
            staged.write_bytes(b"chart")
            original_replace = os.replace
            def replace(source, target):
                if Path(source) == staged:
                    raise OSError(17, "cross-device")
                return original_replace(source, target)
            with patch(
                "osu_toolbox.quick.os.replace",
                side_effect=replace,
            ):
                _publish_chart_output(staged, destination)
            self.assertEqual(destination.read_bytes(), b"chart")

    def test_import_isolation(self):
        result = subprocess.run(
            [sys.executable, "-c", (
                "import sys; from osu_toolbox.launcher import is_quick_invocation; "
                "assert is_quick_invocation(['--context','shell']); "
                "print([m for m in ('pygame','OpenGL','webview','numpy') if m in sys.modules])"
            )],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_full_quick_invocation_keeps_heavy_modules_unloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "map.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n",
                encoding="utf-8",
            )
            prefixes = (
                "pygame", "OpenGL", "webview", "numpy",
                "osu_toolbox.mania.frames", "osu_toolbox.render",
                "osu_toolbox.skin.assets", "osu_toolbox.video",
            )
            script = (
                "import sys; from osu_toolbox.launcher import main; "
                f"code=main(['--context','shell','--operation','replay-generate',"
                f"'--dry-run',{str(chart)!r}]); assert code == 0, code; "
                f"prefixes={prefixes!r}; "
                "blocked=[name for name in sys.modules if any("
                "name == prefix or name.startswith(prefix + '.') for prefix in prefixes)]; "
                "print('MODULES=' + repr(blocked))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout.splitlines()[-1], "MODULES=[]")

    def test_unc_path_is_accepted_as_an_absolute_shell_input(self):
        path = r"\\server\share\谱面.osu"
        with patch("osu_toolbox.quick.os.path.isfile", return_value=True):
            self.assertEqual(_absolute_input(path, CONTEXT_SHELL), path)

    def test_shell_path_with_percent_name_is_not_environment_expanded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "100%TEMP%.osu"
            path.write_text("osu file format v14\n", encoding="utf-8")
            self.assertEqual(_absolute_input(str(path), CONTEXT_SHELL), str(path))

    def test_replay_result_keeps_its_operation_name(self):
        from osu_toolbox.parsing.mr_file import MrReplay
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "chart.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n",
                encoding="utf-8",
            )
            replay = root / "source.mr"
            MrReplay(events=[]).write(replay)
            for operation, source in (("replay-generate", chart), ("replay-edit", replay)):
                with self.subTest(operation=operation):
                    output = io.StringIO()
                    code = main([
                        "--context", "shell", "--operation", operation,
                        "--json", "--dry-run", str(source),
                    ], stdout=output)
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(output.getvalue())["operation"], operation)

    def test_generate_rejects_existing_output_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "map.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n",
                encoding="utf-8",
            )
            output = root / "map.mr"
            output.write_bytes(b"keep")
            error = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "replay-generate",
                str(chart),
            ], stderr=error)
            self.assertEqual(code, 2)
            self.assertEqual(output.read_bytes(), b"keep")
            self.assertIn("--force", error.getvalue())

    def test_edit_round_trip_and_event_count(self):
        from osu_toolbox.parsing.mr_file import MrEvent, MrReplay

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mr"
            MrReplay(events=[MrEvent(10, 1, 0)]).write(source)
            code = main([
                "--context", "shell", "--operation", "replay-edit", str(source),
                "--score", "123", "--events-json",
                json.dumps([{"time": 20, "event": 1, "column": 2}]),
            ])
            self.assertEqual(code, 0)
            edited = MrReplay.read(root / "source-edited.mr")
            self.assertEqual(edited.score, 123)
            self.assertEqual(edited.total, 2)

    def test_edit_preserves_fields_not_supplied_on_the_command_line(self):
        from osu_toolbox.parsing.mr_file import MOD_HIDE, MrEvent, MrReplay

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mr"
            MrReplay(
                chart_version="Hard",
                music="Song",
                artist="Artist",
                score=11,
                max_combo=22,
                best=3,
                cool=4,
                good=5,
                miss=6,
                mod=MOD_HIDE,
                judge=4,
                playing_time=123456,
                events=[MrEvent(10, 1, 0)],
            ).write(source)
            code = main([
                "--context", "shell", "--operation", "replay-edit", str(source),
                "--score", "99",
            ])
            self.assertEqual(code, 0)
            edited = MrReplay.read(root / "source-edited.mr")
            self.assertEqual(edited.score, 99)
            self.assertEqual(edited.max_combo, 22)
            self.assertEqual(edited.mod, MOD_HIDE)
            self.assertEqual(edited.judge, 4)
            self.assertEqual(edited.playing_time, 123456)
            self.assertEqual(
                (edited.chart_version, edited.music, edited.artist),
                ("Hard", "Song", "Artist"),
            )

    def test_unicode_and_space_path_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="osu 工具 ") as directory:
            root = Path(directory)
            chart = root / "谱面 空格.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n",
                encoding="utf-8",
            )
            code = main([
                "--context", "shell", "--operation", "replay-generate", str(chart),
            ])
            self.assertEqual(code, 0)
            self.assertTrue((root / "谱面 空格.mr").is_file())

    def test_edit_chart_path_uses_shared_controller_md5_logic(self):
        import hashlib
        from osu_toolbox.parsing.mr_file import MrReplay

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mr"
            chart = root / "original.mc"
            MrReplay(events=[]).write(source)
            chart.write_bytes(b"original chart")
            code = main([
                "--context", "shell", "--operation", "replay-edit", str(source),
                "--chart-path", str(chart),
            ])
            self.assertEqual(code, 0)
            edited = MrReplay.read(root / "source-edited.mr")
            self.assertEqual(edited.md5, hashlib.md5(b"original chart").hexdigest())

    def test_atomic_write_failure_preserves_existing_output(self):
        from osu_toolbox.parsing.mr_file import MrReplay, atomic_write_replay

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.mr"
            output.write_bytes(b"original")
            with patch(
                "osu_toolbox.parsing.mr_file.tempfile.mkstemp",
                side_effect=PermissionError("只读目录"),
            ):
                with self.assertRaises(PermissionError):
                    atomic_write_replay(MrReplay(events=[]), output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"original")

    def test_atomic_replace_failure_preserves_existing_output_and_cleans_temp(self):
        from osu_toolbox.parsing.mr_file import MrReplay, atomic_write_replay

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "existing.mr"
            output.write_bytes(b"original")
            with patch(
                "osu_toolbox.parsing.mr_file.os.replace",
                side_effect=PermissionError("文件被占用"),
            ):
                with self.assertRaises(PermissionError):
                    atomic_write_replay(MrReplay(events=[]), output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(root.iterdir()), [output])

    def test_atomic_write_does_not_clobber_a_racing_destination(self):
        from osu_toolbox.parsing.mr_file import MrReplay, atomic_write_replay

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "raced.mr"
            original_write = MrReplay.write

            def write_then_race(replay, path):
                result = original_write(replay, path)
                output.write_bytes(b"created by another process")
                return result

            with patch.object(MrReplay, "write", write_then_race):
                with self.assertRaises(FileExistsError):
                    atomic_write_replay(MrReplay(events=[]), output)
            self.assertEqual(output.read_bytes(), b"created by another process")
            self.assertEqual(list(root.iterdir()), [output])

    def test_error_startup_is_timed_through_the_unified_launcher(self):
        from osu_toolbox.quick import measure_startup

        missing = str(Path(tempfile.gettempdir()) / "missing-quick-start.osu")
        result = measure_startup([
            "--context", "shell", "--operation", "replay-generate",
            "--dry-run", missing,
        ], repetitions=2)
        self.assertEqual(result["returncodes"], [2, 2])
        self.assertEqual(len(result["samples_ms"]), 2)
        self.assertTrue(all(value >= 0 for value in result["samples_ms"]))


class _StubSettingsStore:
    """Minimal settings store standing in for the GUI's persisted file."""

    def __init__(self, state):
        self._state = state

    def load_state(self):
        return self._state, None


def _shell_settings_state(overall_difficulty=8.0, hp_drain_rate=8.0):
    """A persisted settings blob with the 转谱工具 values the menu reads."""
    return {
        "global": {
            "chart_overall_difficulty": overall_difficulty,
            "chart_hp_drain_rate": hp_drain_rate,
            "shell_menu": {
                "shell_menu_enabled": True,
                "shell_menu_chart_convert": True,
            },
        },
        "tools": {},
    }


class ShellMenuManagerTests(unittest.TestCase):
    _OSU_TEMPLATE = (
        "osu file format v14\n[General]\nAudioFilename: {audio}\nMode: 3\n"
        "[Metadata]\nTitle: {title}\nVersion: {version}\n"
        "[Difficulty]\nCircleSize: 4\n"
        "[Events]\n0,0,\"{bg}\",0,0\n"
        "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n"
    )

    def _pack_folder(self, folder, target, operation="folder-convert", extra=()):
        out, err = io.StringIO(), io.StringIO()
        code = main([
            "--context", "shell", "--operation", operation,
            "--target-format", target, "--json", *extra, str(folder),
        ], stdout=out, stderr=err)
        text = out.getvalue().strip() or err.getvalue().strip()
        return code, (json.loads(text) if text else {})

    def test_command_quotes_executable_and_placeholder(self):
        command = shell_menu_command(r"C:\Program Files\OSU\osu-toolbox-menu.exe", "replay-generate")
        self.assertIn('"C:\\Program Files\\OSU\\osu-toolbox-menu.exe"', command)
        self.assertIn('"%1"', command)

    def test_frozen_default_uses_adjacent_menu_executable(self):
        import osu_toolbox.shell_menu as shell_menu

        executable = Path(r"C:\Program Files\OSU\osu-toolbox.exe")
        with patch.object(shell_menu.sys, "frozen", True, create=True):
            with patch.object(shell_menu.sys, "executable", str(executable)):
                self.assertEqual(
                    shell_menu.resolve_shell_executable(),
                    str(executable.parent / "osu-toolbox-menu.exe"),
                )

    def test_fake_registry_enable_disable_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(str(executable), registry)
            enabled = manager.apply({
                "shell_menu_enabled": True,
                "shell_menu_replay_generate": True,
            })
            self.assertTrue(enabled["ok"])
            self.assertEqual(enabled["state"], "enabled")
            root_key = manager.key_path("replay-generate", ".osu")
            registry.values[root_key]["Icon"] = "broken icon"
            self.assertEqual(manager.status()["state"], "needs_repair")
            self.assertEqual(manager.repair({
                "shell_menu_enabled": True,
                "shell_menu_replay_generate": True,
            })["state"], "enabled")
            command_key = manager.command_key_path("replay-generate", ".osu")
            registry.values[command_key][""] = "broken command"
            self.assertEqual(manager.status()["state"], "needs_repair")
            self.assertEqual(manager.repair({
                "shell_menu_enabled": True,
                "shell_menu_replay_generate": True,
            })["state"], "enabled")
            disabled = manager.apply({"shell_menu_replay_generate": False})
            self.assertTrue(disabled["ok"])
            self.assertEqual(disabled["state"], "disabled")
            self.assertFalse(registry.values)

    def test_each_feature_enables_without_a_master_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            notify = Mock()
            manager = ShellMenuManager(executable, registry, notify_shell=notify)
            for setting_key in shell_menu.default_shell_menu_settings():
                result = manager.apply({setting_key: True})
                self.assertTrue(result["ok"])
                for item in result["items"]:
                    self.assertEqual(item["enabled"], item["setting_key"] == setting_key)
            self.assertTrue(manager.uninstall_all()["ok"])
            self.assertEqual(notify.call_count, 4)
            self.assertNotIn("shell_menu_enabled", shell_menu.default_shell_menu_settings())

    def test_a_failed_or_ignored_delete_never_reports_success(self):
        class DeniedDelete(_FakeWinreg):
            def DeleteKeyEx(self, *_args):
                raise PermissionError("删除被拒绝")

        class IgnoredDelete(_FakeWinreg):
            def DeleteKeyEx(self, *_args):
                pass

        for registry_class in (DeniedDelete, IgnoredDelete):
            with self.subTest(registry=registry_class.__name__), tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "osu-toolbox-menu.exe"
                executable.write_bytes(b"launcher")
                registry = registry_class()
                manager = ShellMenuManager(executable, registry)
                self.assertTrue(manager.apply({"shell_menu_replay_generate": True})["ok"])
                result = manager.uninstall_all()
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "registry_error")
                self.assertTrue(registry.values)
                self.assertFalse(manager.apply({})["ok"])

    def test_uninstall_removes_legacy_verbs_and_command_store_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(executable, registry)
            self.assertTrue(manager.apply({spec.key: True for spec in shell_menu.SHELL_MENU_SETTING_PARAMS})["ok"])
            parent = shell_menu.FILE_ASSOCIATION_KEY_TEMPLATE.format(extension=".mcz")
            registry.values[parent] = {}
            stale = parent + r"\osu-toolbox-chart-convert"
            foreign = parent + r"\osu-toolbox-custom"
            registry.values[stale] = {"osu-toolbox-managed": "1"}
            registry.values[foreign] = {"MUIVerb": "user rule"}
            store = shell_menu.SHELL_MENU_COMMAND_STORE_PATH
            registry.values[store] = {}
            legacy = manager.command_store_submenu_key_path("chart-convert", "to-osu")
            registry.values[legacy] = {}
            registry.values[legacy + r"\command"] = {"": "old"}
            obsolete = store + r"\osu-toolbox-obsolete"
            registry.values[obsolete] = {"osu-toolbox-managed": "1"}
            result = manager.uninstall_all()
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "disabled")
            self.assertEqual(result["message"], "右键菜单已卸载")
            self.assertEqual(set(registry.values), {parent, foreign, store})

    def test_unchecking_all_features_also_removes_obsolete_menu_keys(self):
        registry = _FakeWinreg()
        manager = ShellMenuManager(winreg_module=registry)
        parent = shell_menu.FILE_ASSOCIATION_KEY_TEMPLATE.format(extension=".osz")
        registry.values[parent] = {}
        registry.values[parent + r"\osu-toolbox-chart-convert"] = {"osu-toolbox-managed": "1"}
        result = manager.apply({})
        self.assertTrue(result["ok"])
        self.assertEqual(set(registry.values), {parent})

    def test_an_unreadable_legacy_marker_is_reported_as_a_cleanup_error(self):
        class DeniedMarker(_FakeWinreg):
            def QueryValueEx(self, key, name):
                if key.path.endswith("osu-toolbox-obsolete"):
                    raise PermissionError("旧版菜单无读取权限")
                return super().QueryValueEx(key, name)

        registry = DeniedMarker()
        manager = ShellMenuManager(winreg_module=registry)
        parent = shell_menu.DIRECTORY_KEY_PATH
        registry.values[parent] = {}
        registry.values[parent + r"\osu-toolbox-obsolete"] = {"osu-toolbox-managed": "1"}
        result = manager.uninstall_all()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "registry_error")

    def test_uninstall_stays_uninstalled_after_a_general_save_and_restart(self):
        from osu_toolbox.gui.bridge import Api
        from osu_toolbox.gui.settings import SettingsStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(executable, registry)
            api = Api(directory, settings_store=SettingsStore(path), shell_menu_manager=manager)
            self.assertEqual(api.save_shell_menu_settings({"shell_menu_chart_convert": True})["status"], "ok")
            result = api.uninstall_shell_menu()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["message"], "右键菜单已卸载")
            self.assertFalse(any(result["shell_menu_settings"].values()))
            self.assertEqual(api.save_global_settings({"speed_keep_pitch": True})["status"], "ok")
            restarted = Api(directory, settings_store=SettingsStore(path), shell_menu_manager=manager)
            self.assertFalse(any(restarted.get_bootstrap()["shell_menu_settings"].values()))
            self.assertFalse(registry.values)

    def test_legacy_settings_migrate_without_enabling_disabled_features(self):
        from osu_toolbox.gui.bridge import Api
        for nested in (True, False):
            for enabled in (True, False):
                with self.subTest(nested=nested, enabled=enabled):
                    settings = {"shell_menu_enabled": enabled, "shell_menu_chart_convert": True}
                    state = {"global": {"shell_menu": settings} if nested else settings, "tools": {}}
                    api = Api(".", settings_store=_StubSettingsStore(state),
                              shell_menu_manager=ShellMenuManager(winreg_module=_FakeWinreg()))
                    current = api.get_bootstrap()["shell_menu_settings"]
                    self.assertNotIn("shell_menu_enabled", current)
                    self.assertEqual(current["shell_menu_chart_convert"], enabled)

    def test_chart_convert_schema_exposes_cascade_submenu(self):
        schema = shell_menu_schema(r"C:\\Program Files\\OSU\\osu-toolbox-menu.exe")
        item = next(value for value in schema["items"] if value["operation"] == "chart-convert")
        self.assertEqual(item["title"], "转谱")
        self.assertEqual([child["target_format"] for child in item["submenu"]], [".osu", ".mc", ".sm", ".osz", ".mcz"])
        self.assertIn("--target-format .mc", next(child["command"] for child in item["submenu"] if child["target_format"] == ".mc"))

    def test_chart_convert_registry_writes_subcommands_and_child_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(str(executable), registry)
            result = manager.apply({
                "shell_menu_enabled": True,
                "shell_menu_chart_convert": True,
            })
            self.assertTrue(result["ok"])
            parent = manager.key_path("chart-convert", ".mc")
            self.assertNotIn("SubCommands", registry.values[parent])
            self.assertEqual(registry.values[parent][""], "")
            self.assertEqual(
                registry.values[parent]["ExtendedSubCommandsKey"],
                r"SystemFileAssociations\.mc\shell\osu-toolbox-chart-convert",
            )
            child_command = manager.submenu_command_key_path("chart-convert", ".mc", "to-mc")
            self.assertIn(parent + r"\shell", child_command)
            self.assertIn("--target-format .mc", registry.values[child_command][""])
            manager.uninstall_all()
            self.assertFalse(registry.values)

    def test_registry_access_error_is_not_reported_as_disabled(self):
        class DeniedWinreg(_FakeWinreg):
            def OpenKey(self, _root, path, *_args):
                raise PermissionError(path)

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            manager = ShellMenuManager(str(executable), DeniedWinreg())
            status = manager.status()
            self.assertEqual(status["state"], "needs_repair")
            self.assertTrue(status["items"][0]["errors"])
            result = manager.uninstall_all()
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "registry_error")

    def test_shell_settings_are_persisted_separately_from_general_settings(self):
        from osu_toolbox.gui.bridge import Api
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(str(executable), registry)
            api = Api(directory, settings_store=SettingsStore(path), shell_menu_manager=manager)
            result = api.save_shell_menu_settings({
                "shell_menu_enabled": True,
                "shell_menu_replay_generate": True,
            })
            self.assertEqual(result["status"], "ok")
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("shell_menu_enabled", state["global"]["shell_menu"])
            self.assertTrue(state["global"]["shell_menu"]["shell_menu_replay_generate"])

    def test_chart_tool_settings_are_normalized_and_persisted(self):
        from osu_toolbox.gui.bridge import Api
        from osu_toolbox.gui.settings import SettingsStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            api = Api(directory, settings_store=SettingsStore(path))
            # The settings form submits number fields as strings.
            result = api.save_global_settings({
                "chart_overall_difficulty": "9",
                "chart_hp_drain_rate": "12",
            })
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["global_settings"]["chart_overall_difficulty"], 9.0)
            # 0-10 is the only range osu! accepts.
            self.assertEqual(result["global_settings"]["chart_hp_drain_rate"], 10.0)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["global"]["chart_overall_difficulty"], 9.0)
            self.assertEqual(state["global"]["chart_hp_drain_rate"], 10.0)

            # A 转谱 run that leaves the form fields empty uses exactly these.
            with patch(
                "osu_toolbox.gui.settings.SettingsStore",
                lambda: _StubSettingsStore(state),
            ):
                from osu_toolbox.quick import _chart_difficulty_overrides

                self.assertEqual(_chart_difficulty_overrides(), (9.0, 10.0))

    def test_folder_menu_registers_directory_and_background_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(str(executable), registry)
            result = manager.apply({
                "shell_menu_enabled": True,
                "shell_menu_chart_convert": True,
            })
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "enabled")

            directory_key = manager.key_path("folder-convert", "folder")
            self.assertTrue(directory_key.startswith(DIRECTORY_KEY_PATH))
            background_key = manager.background_key_path("folder-convert")
            self.assertTrue(background_key.startswith(DIRECTORY_BACKGROUND_KEY_PATH))
            self.assertIn(background_key, registry.values)

            # The folder menu shares the 转谱 cascade with the file menu.
            self.assertEqual(
                registry.values[directory_key]["ExtendedSubCommandsKey"],
                r"Directory\shell\osu-toolbox-folder-convert",
            )
            for target in (".osz", ".mcz"):
                child = manager.submenu_command_key_path(
                    "folder-convert", "folder", f"to-{target[1:]}"
                )
                self.assertIn(f"--target-format {target}", registry.values[child][""])
            # A file association must be registered for the same setting.
            self.assertIn(manager.key_path("chart-convert", ".osu"), registry.values)

            manager.uninstall_all()
            self.assertFalse(registry.values)

    def test_folder_menu_children_and_archive_children_are_separated(self):
        schema = shell_menu_schema(r"C:\x\osu-toolbox-menu.exe")
        items = {item["operation"]: item for item in schema["items"]}

        plain = items["chart-convert"]
        self.assertEqual(plain["file_types"], ["*.osu", "*.mc", "*.sm"])
        self.assertEqual(
            [child["target_format"] for child in plain["submenu"]],
            [".osu", ".mc", ".sm", ".osz", ".mcz"],
        )

        archive = items["archive-convert"]
        self.assertEqual(archive["file_types"], ["*.osz", "*.mcz"])
        self.assertEqual(
            [child["target_format"] for child in archive["submenu"]],
            [".osz", ".mcz"],
        )

        folder = items["folder-convert"]
        self.assertTrue(folder["folder"])
        self.assertEqual(
            [child["target_format"] for child in folder["submenu"]],
            [".osz", ".mcz"],
        )
        # A folder is passed as %V; a file as %1.
        self.assertEqual(folder["argv"][-1], "%V")
        self.assertEqual(plain["argv"][-1], "%1")

    def test_obsolete_registrations_are_swept_but_foreign_keys_kept(self):
        """Re-enabling clears keys an older version wrote under old operations."""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(str(executable), registry)

            # Simulate the legacy layout: 转谱 on .osz/.mcz registered under the
            # chart-convert operation, which this version no longer declares.
            # The parent key must exist for the sweep to find its children.
            parent = r"Software\Classes\SystemFileAssociations\.mcz\shell"
            registry.values.setdefault(parent, {})
            stale = parent + r"\osu-toolbox-chart-convert"
            registry.values[stale] = {
                "MUIVerb": "转谱",
                "osu-toolbox-managed": "1",
            }
            # A hand-written rule with the same prefix must survive.
            foreign = r"Software\Classes\SystemFileAssociations\.mcz\shell\osu-toolbox-custom"
            registry.values[foreign] = {"MUIVerb": "手写规则"}

            result = manager.apply({
                "shell_menu_enabled": True,
                "shell_menu_chart_convert": True,
            })
            self.assertTrue(result["ok"], result)
            self.assertNotIn(stale, registry.values)
            self.assertIn(foreign, registry.values)
            # The declared archive operation is present instead.
            self.assertIn(manager.key_path("archive-convert", ".mcz"), registry.values)

    def test_registered_operations_are_accepted_by_the_command_line(self):
        """Every registry command must be runnable; a mismatch writes nothing.

        The folder menu used to register ``--operation folder-convert`` while
        the CLI only accepted ``chart-convert``, so argparse rejected it with
        exit 2 and no output file was ever produced.
        """
        from osu_toolbox.quick import SUPPORTED_OPERATIONS
        from osu_toolbox.shell_menu import SHELL_MENU_ITEMS

        for item in SHELL_MENU_ITEMS:
            self.assertIn(
                item.operation,
                SUPPORTED_OPERATIONS,
                f"{item.operation} is registered but not runnable",
            )

    def test_menu_commands_run_end_to_end(self):
        """Run every menu command exactly as registered, on a real folder."""
        import zipfile

        osu_text = (
            "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
            "[Metadata]\nTitle: A\nVersion: Easy\n[Difficulty]\nCircleSize: 4\n"
            "[Events]\n0,0,\"cover.jpg\",0,0\n"
            "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            chart = root / "song.osu"
            chart.write_text(osu_text, encoding="utf-8")
            other = root / "other.osu"
            other.write_text(osu_text.replace("Title: A", "Title: B"), encoding="utf-8")
            (root / "song.mp3").write_bytes(b"audio")
            (root / "cover.jpg").write_bytes(b"image")

            # folder-convert, as the Directory\shell key registers it: the
            # whole folder becomes one archive named after it.
            self.assertEqual(main([
                "--context", "shell", "--operation", "folder-convert",
                "--target-format", ".osz", str(root),
            ], stdout=io.StringIO()), 0)
            self.assertTrue((Path(directory) / "set.osz").is_file())
            with zipfile.ZipFile(Path(directory) / "set.osz") as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["cover.jpg", "other.osu", "song.mp3", "song.osu"],
                )

            # chart-convert on a single chart file.
            self.assertEqual(main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".mcz", str(chart),
            ], stdout=io.StringIO()), 0)
            self.assertTrue((root / "song.mcz").is_file())

            # archive-convert, as the SystemFileAssociations key registers it.
            self.assertEqual(main([
                "--context", "shell", "--operation", "archive-convert",
                "--target-format", ".osz", str(root / "song.mcz"),
            ], stdout=io.StringIO()), 0)
            with zipfile.ZipFile(root / "song.osz") as archive:
                self.assertIn("song.osu", archive.namelist())

            # An operation must reject the wrong input kind.
            error = io.StringIO()
            self.assertEqual(main([
                "--context", "shell", "--operation", "folder-convert",
                "--target-format", ".osz", str(chart),
            ], stderr=error), 2)
            self.assertIn("文件夹", error.getvalue())
            error = io.StringIO()
            self.assertEqual(main([
                "--context", "shell", "--operation", "archive-convert",
                "--target-format", ".osz", str(chart),
            ], stderr=error), 2)
            self.assertIn("归档", error.getvalue())

    def test_folder_becomes_one_archive_holding_every_chart(self):
        """One folder yields exactly one archive, not one per chart.

        Malody ships a folder as a single ``.mcz`` whose members are the
        contained plain charts plus their audio and artwork.
        """
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "1585559 Ado - Odo"
            root.mkdir()
            for version in ("x1.1", "x1.2"):
                (root / f"{version}.osu").write_text(
                    self._OSU_TEMPLATE.format(
                        audio=f"audio{version}.mp3", title="Odo",
                        version=version, bg="bg.png",
                    ), encoding="utf-8")
            (root / "audiox1.1.mp3").write_bytes(b"a1")
            (root / "audiox1.2.mp3").write_bytes(b"a2")
            (root / "bg.png").write_bytes(b"bg")

            code, payload = self._pack_folder(root, ".mcz")
            self.assertEqual(code, 0, payload)
            archive_path = root.parent / "1585559 Ado - Odo.mcz"
            self.assertTrue(archive_path.is_file())
            # The source folder is untouched, and no per-chart archive exists.
            self.assertEqual(
                sorted(p.name for p in root.iterdir()),
                ["audiox1.1.mp3", "audiox1.2.mp3", "bg.png", "x1.1.osu", "x1.2.osu"],
            )
            self.assertEqual(list(root.parent.glob("*.mcz")),
                             [archive_path])
            with zipfile.ZipFile(archive_path) as archive:
                names = sorted(archive.namelist())
            self.assertEqual(names, [
                "audiox1.1.mp3", "audiox1.2.mp3", "bg.png", "x1.1.mc", "x1.2.mc",
            ])
            self.assertEqual(payload["converted"], 2)

    def test_folder_pack_keeps_relative_paths_and_reports_nested_archives(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            (root / "sub").mkdir(parents=True)
            (root / "flat.osu").write_text(
                self._OSU_TEMPLATE.format(audio="a.mp3", title="F",
                                          version="Easy", bg="c.jpg"),
                encoding="utf-8")
            (root / "sub" / "deep.osu").write_text(
                self._OSU_TEMPLATE.format(audio="s.mp3", title="D",
                                          version="Hard", bg="s.jpg"),
                encoding="utf-8")
            (root / "a.mp3").write_bytes(b"a")
            (root / "c.jpg").write_bytes(b"c")
            (root / "sub" / "s.mp3").write_bytes(b"s")
            (root / "sub" / "s.jpg").write_bytes(b"sj")
            (root / "readme.txt").write_bytes(b"readme")
            with zipfile.ZipFile(root / "old.mcz", "w") as archive:
                archive.writestr("old.mc", "{}")

            code, payload = self._pack_folder(root, ".mcz")
            self.assertEqual(code, 0, payload)
            archive_path = root.parent / "set.mcz"
            with zipfile.ZipFile(archive_path) as archive:
                names = sorted(archive.namelist())
            # The nested chart keeps its directory, with its own resources in
            # the same place; unrelated files travel along.
            self.assertEqual(names, [
                "a.mp3", "c.jpg", "flat.mc", "readme.txt",
                "sub/deep.mc", "sub/s.jpg", "sub/s.mp3",
            ])
            # The archive already inside the folder is reported, not packed.
            self.assertEqual(
                [Path(p).name for p in payload["skipped_archives"]], ["old.mcz"]
            )
            # And it is left where it was.
            self.assertTrue((root / "old.mcz").is_file())

    def test_folder_pack_carries_every_file_in_the_folder(self):
        """The folder is the unit, so nothing inside it is dropped.

        A chart's audio is packed with it, and any other file the folder holds
        (an extra image, a readme) travels along too — Malody ships a beatmap
        folder's contents as-is.
        """
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.osu").write_text(
                self._OSU_TEMPLATE.format(audio="song.mp3", title="A",
                                          version="Easy", bg="cover.jpg"),
                encoding="utf-8")
            (root / "song.mp3").write_bytes(b"audio")
            (root / "cover.jpg").write_bytes(b"cover")
            (root / "readme.txt").write_bytes(b"readme")
            # A sibling outside the folder must never be pulled in.
            (root.parent / "outside.mp3").write_bytes(b"outside")

            code, payload = self._pack_folder(root, ".mcz")
            self.assertEqual(code, 0, payload)
            with zipfile.ZipFile(root.parent / "set.mcz") as archive:
                names = sorted(archive.namelist())
            self.assertEqual(
                names, ["cover.jpg", "readme.txt", "song.mc", "song.mp3"]
            )

    def test_folder_pack_uses_the_chart_matching_the_archive(self):
        """``song.osu`` and ``song.mc`` describe one chart; read the right one."""
        import json as json_module
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.osu").write_text(
                self._OSU_TEMPLATE.format(audio="a.mp3", title="FromOsu",
                                          version="Easy", bg="c.jpg"),
                encoding="utf-8")
            (root / "song.mc").write_text(json_module.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4},
                         "background": "c.jpg",
                         "song": {"title": "FromMc", "file": "a.mp3"}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0},
                         {"beat": [0, 0, 1], "sound": "a.mp3",
                          "offset": 10, "type": 1}],
            }), encoding="utf-8")
            (root / "a.mp3").write_bytes(b"audio")
            (root / "c.jpg").write_bytes(b"cover")

            code, payload = self._pack_folder(root, ".mcz")
            self.assertEqual(code, 0, payload)
            with zipfile.ZipFile(root.parent / "set.mcz") as archive:
                names = sorted(archive.namelist())
                chart = archive.read("song.mc").decode("utf-8")
            # The .mc was already the contained format, so it is carried over
            # verbatim rather than regenerated from the .osu.
            self.assertEqual(names, ["a.mp3", "c.jpg", "song.mc"])
            self.assertEqual(payload["converted"], 0)

    def test_folder_pack_leaves_charts_already_in_the_contained_format(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.osu").write_text(
                self._OSU_TEMPLATE.format(audio="a.mp3", title="A",
                                          version="Easy", bg="c.jpg"),
                encoding="utf-8")
            (root / "a.mp3").write_bytes(b"audio")
            (root / "c.jpg").write_bytes(b"cover")

            code, payload = self._pack_folder(root, ".osz")
            self.assertEqual(code, 0, payload)
            with zipfile.ZipFile(root.parent / "set.osz") as archive:
                names = sorted(archive.namelist())
            # .osz contains .osu, which the source already was: the member keeps
            # its name and is still reported as copied, not converted.
            self.assertEqual(names, ["a.mp3", "c.jpg", "song.osu"])
            self.assertEqual(payload["converted"], 0)

    def test_menu_chart_convert_writes_the_configured_difficulty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            with patch(
                "osu_toolbox.gui.settings.SettingsStore",
                lambda: _StubSettingsStore(_shell_settings_state(9.0, 2.5)),
            ):
                out, err = io.StringIO(), io.StringIO()
                code = main([
                    "--context", "shell", "--operation", "chart-convert",
                    "--target-format", ".osu", "--json", str(source),
                ], stdout=out, stderr=err)
            self.assertEqual(code, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["overall_difficulty"], 9.0)
            self.assertEqual(payload["hp_drain_rate"], 2.5)
            text = (root / "song.osu").read_text(encoding="utf-8")
            self.assertIn("OverallDifficulty: 9\n", text)
            self.assertIn("HPDrainRate: 2.5\n", text)

    def test_menu_chart_convert_defaults_to_eight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            with patch(
                "osu_toolbox.gui.settings.SettingsStore",
                lambda: _StubSettingsStore({"global": {}, "tools": {}}),
            ):
                code = main([
                    "--context", "shell", "--operation", "chart-convert",
                    "--target-format", ".osu", str(source),
                ], stdout=io.StringIO(), stderr=io.StringIO())
            self.assertEqual(code, 0)
            text = (root / "song.osu").read_text(encoding="utf-8")
            self.assertIn("OverallDifficulty: 8\n", text)
            self.assertIn("HPDrainRate: 8\n", text)

    def test_folder_convert_rewrites_an_osu_member_to_the_configured_difficulty(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.osu").write_text(
                self._OSU_TEMPLATE.format(audio="a.mp3", title="A",
                                          version="Easy", bg="c.jpg").replace(
                    "[Difficulty]\nCircleSize: 4\n",
                    "[Difficulty]\nHPDrainRate: 5\nCircleSize: 4\nOverallDifficulty: 5\n",
                ),
                encoding="utf-8")
            (root / "a.mp3").write_bytes(b"audio")
            (root / "c.jpg").write_bytes(b"cover")

            with patch(
                "osu_toolbox.gui.settings.SettingsStore",
                lambda: _StubSettingsStore(_shell_settings_state(8.0, 7.5)),
            ):
                code, payload = self._pack_folder(root, ".osz")
            self.assertEqual(code, 0, payload)
            # The chart stays a copy, but its OD/HP follow the setting.
            self.assertEqual(payload["converted"], 0)
            with zipfile.ZipFile(root.parent / "set.osz") as archive:
                names = sorted(archive.namelist())
                text = archive.read("song.osu").decode("utf-8")
            self.assertEqual(names, ["a.mp3", "c.jpg", "song.osu"])
            # The member is read as raw bytes, so compare whole lines rather
            # than assuming one newline convention.
            lines = [line.strip() for line in text.splitlines()]
            self.assertIn("OverallDifficulty: 8", lines)
            self.assertIn("HPDrainRate: 7.5", lines)

    def test_a_renamed_resource_is_packed_once_and_the_chart_follows_it(self):
        """A renamed resource member must be packed once and referenced by name.

        A resource member is only renamed when two members collide.  When that
        happens the chart inside the archive has to point at the new name, and
        the folder walk has to recognise the file as already packed by its path
        inside the folder -- not by the (now different) member name and not by
        its absolute path.
        """
        import zipfile

        from osu_toolbox.chart_convert import ArchiveMemberPlanner

        class CollidingPlanner(ArchiveMemberPlanner):
            """Simulate the collision that renames the audio member."""

            def claim(self, name):
                if str(name).lower().endswith(".mp3"):
                    granted = f"hit-{name}"
                    self._used.add(self._key(granted))
                    self.renamed.append((str(name), granted))
                    return granted
                return super().claim(name)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.osu").write_text(
                self._OSU_TEMPLATE.format(audio="song.mp3", title="A",
                                          version="Easy", bg="c.jpg"),
                encoding="utf-8")
            (root / "song.mp3").write_bytes(b"audio")
            (root / "c.jpg").write_bytes(b"cover")

            with patch("osu_toolbox.chart_convert.ArchiveMemberPlanner", CollidingPlanner):
                code, payload = self._pack_folder(root, ".osz")
            self.assertEqual(code, 0, payload)
            with zipfile.ZipFile(root.parent / "set.osz") as archive:
                names = sorted(archive.namelist())
                chart = archive.read("song.osu").decode("utf-8")
            self.assertEqual(names, ["c.jpg", "hit-song.mp3", "song.osu"])
            self.assertIn("AudioFilename: hit-song.mp3", chart)
            self.assertEqual(payload["renamed_members"], ["song.mp3 → hit-song.mp3"])

    def test_a_converted_folder_chart_follows_a_renamed_resource(self):
        """The conversion branch rewrites references through the same rule."""
        import zipfile

        from osu_toolbox.chart_convert import ArchiveMemberPlanner

        class CollidingPlanner(ArchiveMemberPlanner):
            def claim(self, name):
                if str(name).lower().endswith(".mp3"):
                    granted = f"hit-{name}"
                    self._used.add(self._key(granted))
                    self.renamed.append((str(name), granted))
                    return granted
                return super().claim(name)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.mc").write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": "Easy",
                         "song": {"file": "song.mp3"}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            (root / "song.mp3").write_bytes(b"audio")

            with patch("osu_toolbox.chart_convert.ArchiveMemberPlanner", CollidingPlanner):
                code, payload = self._pack_folder(root, ".osz")
            self.assertEqual(code, 0, payload)
            with zipfile.ZipFile(root.parent / "set.osz") as archive:
                names = sorted(archive.namelist())
                chart = archive.read("song.osu").decode("utf-8")
            self.assertEqual(names, ["hit-song.mp3", "song.osu"])
            self.assertIn("AudioFilename: hit-song.mp3", chart)

    def test_the_menu_names_the_file_a_missing_audio_reference_probably_meant(self):
        """Being faithful to the source must not leave the user without a clue."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "BOOM.osu"
            source.write_text(
                self._OSU_TEMPLATE.format(audio="BOOM2.mp3", title="BOOM",
                                          version="Easy", bg="c.jpg"),
                encoding="utf-8")
            (root / "BOOM.mp3").write_bytes(b"audio")
            (root / "c.jpg").write_bytes(b"cover")

            out = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".mc", "--force", str(source),
            ], stdout=out)
            self.assertEqual(code, 0)
            printed = out.getvalue()
            self.assertIn("BOOM2.mp3", printed)
            self.assertIn("同目录存在 BOOM.mp3", printed)

    def test_a_single_chart_source_keeps_the_source_file_name(self):
        """One chart is named after its file, in the menu and in the dry run.

        The menu fixes one destination path before converting, so a single
        output lands on that path; for a single-chart source the converter
        derives the very same name (no difficulty suffix), which is why the two
        agree here.
        """
        from osu_toolbox.chart_convert import preflight_conversion

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "BOOM.sm"
            source.write_text(
                "#TITLE:BOOM;#MUSIC:BOOM.mp3;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Beginner:\n 1:\n 0.000:\n1000\n;",
                encoding="utf-8")
            planned, _warnings = preflight_conversion([str(source)], ".osu", str(root))
            out = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".osu", "--json", str(source),
            ], stdout=out)
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(
                [Path(item).name for item in payload["outputs"]],
                ["BOOM.osu"],
            )
            self.assertEqual(
                [Path(item).name for item in planned],
                [Path(item).name for item in payload["outputs"]],
            )
            self.assertTrue((root / "BOOM.osu").is_file())

    def test_a_multi_chart_source_keeps_every_difficulty_in_its_name(self):
        """Several charts from one file must stay distinguishable."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "MULTI.sm"
            source.write_text(
                "#TITLE:MULTI;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Easy:\n 1:\n 0.000:\n1000\n;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 5:\n 0.000:\n0100\n;",
                encoding="utf-8")
            out = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".osu", "--json", str(source),
            ], stdout=out)
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(
                sorted(Path(item).name for item in payload["outputs"]),
                ["MULTI-Easy.osu", "MULTI-Hard.osu"],
            )

    def test_an_explicit_output_name_still_wins(self):
        """``-o`` asks for one exact path, so it overrides the derived name."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "BOOM.sm"
            source.write_text(
                "#TITLE:BOOM;#MUSIC:BOOM.mp3;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Beginner:\n 1:\n 0.000:\n1000\n;",
                encoding="utf-8")
            target = root / "custom.osu"
            code = main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".osu", "-o", str(target), str(source),
            ], stdout=io.StringIO())
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())
            self.assertFalse((root / "BOOM.osu").exists())

    def test_folder_pack_survives_a_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "song.osu").write_text(
                self._OSU_TEMPLATE.format(audio="a.mp3", title="A",
                                          version="Easy", bg="c.jpg"),
                encoding="utf-8")
            (root / "a.mp3").write_bytes(b"audio")
            (root / "c.jpg").write_bytes(b"cover")

            self.assertEqual(self._pack_folder(root, ".mcz")[0], 0)
            # Refuses to clobber without --force, succeeds with it.
            code, payload = self._pack_folder(root, ".mcz")
            self.assertEqual(code, 2)
            self.assertEqual(payload.get("code"), "output_exists")
            self.assertEqual(
                self._pack_folder(root, ".mcz", extra=("--force",))[0], 0
            )

    def test_folder_conversion_requires_an_archive_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            for target in (".mc", ".osu", ".sm"):
                error = io.StringIO()
                code = main([
                    "--context", "shell", "--operation", "chart-convert",
                    "--target-format", target, str(root),
                ], stderr=error)
                self.assertEqual(code, 2, target)
                self.assertIn("归档", error.getvalue())

    def test_archive_file_rejects_a_single_file_target(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "pack.osz"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("pack.osu", "osu file format v14\n[General]\nMode:3\n")
            error = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".mc", str(archive_path),
            ], stderr=error)
            self.assertEqual(code, 2)
            self.assertIn("归档", error.getvalue())
            # The supported direction still works.
            self.assertEqual(main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".mcz", str(archive_path),
            ], stdout=io.StringIO()), 0)
            self.assertTrue((Path(directory) / "pack.mcz").is_file())

    def test_folder_conversion_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "set"
            root.mkdir()
            (root / "easy.osu").write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "chart-convert",
                "--target-format", ".mcz", "--dry-run", "--json", str(root),
            ], stdout=out)
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["converted"], 1)
            self.assertFalse((root / "easy.mcz").exists())

    def test_folder_is_accepted_as_a_shell_input(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(_absolute_input(directory, CONTEXT_SHELL), directory)
            # A missing path is still reported rather than treated as a folder.
            with self.assertRaises(Exception):
                _absolute_input(str(Path(directory) / "nope"), CONTEXT_SHELL)


if __name__ == "__main__":
    unittest.main()
