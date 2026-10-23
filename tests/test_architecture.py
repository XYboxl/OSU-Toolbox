"""架构约束回归测试：核心、工具平台、GUI 依赖方向 + config 访问。

- 依赖方向（硬约束）：核心层不得 import tools/gui；
  tools 只调核心公共 API；gui 只调根包元数据与 tools 公共 API。
- config 访问：运行时可变常量必须经 `config.X` 属性访问，禁止
  `from config import X` 快照导入（configure_runtime 重算后取不到新值）。
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 包根 = osu_toolbox/ 子目录：包内所有 *.py（含子包 __init__.py）即模块
PACKAGE = ROOT / "osu_toolbox"

# 层 → 不得 import 的层（含子包与同级模块）
CORE_LAYERS = {"parsing", "skin", "mania"}
APPLICATION_LAYER = {"tools"}
PRESENTATION_LAYER = {"gui"}
RENDER_VIDEO = {"render", "video"}


def _current_package(module, path):
    """模块所在包：普通子模块取父包，__init__.py（包本身）取自身。"""
    if path.name == "__init__.py":
        return module
    return module.rsplit(".", 1)[0] if "." in module else ""


def _iter_imports(tree, module, path):
    """Yield (target_module, imported_names) for osu_toolbox imports."""
    package = _current_package(module, path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = package
            for _ in range(1, node.level):
                base = base.rsplit(".", 1)[0] if "." in base else ""
            if node.module:
                target = ".".join(filter(None, [base, node.module]))
            else:
                # `from . import X`：X 为当前包成员/子模块，模块路径即包本身
                target = base
            names = tuple(alias.name for alias in node.names)
            yield target, names
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("osu_toolbox"):
                    yield alias.name, ()


class ArchitectureConstraintTests(unittest.TestCase):
    def test_production_code_has_no_hardcoded_absolute_machine_paths(self):
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(
                        re.match(r"^(?:[A-Za-z]:[\\/]|/(?:home|Users|mnt|tmp)/)", node.value),
                        f"{path.relative_to(ROOT)}:{node.lineno} contains an absolute machine path",
                    )

    def _module_files(self):
        files = {}
        for path in PACKAGE.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(PACKAGE)
            parts = ["osu_toolbox"] + list(rel.parts[:-1])
            if rel.name != "__init__.py":
                parts.append(rel.stem)
            files[".".join(parts)] = path
        return files

    def _top_subpackage(self, target):
        """osu_toolbox.<sub>.<rest> → <sub>；config/legacy_* 等根模块返回根名。"""
        parts = target.split(".")
        if len(parts) < 2:
            return target
        return parts[1]

    def test_core_layers_never_import_tool_render_video(self):
        files = self._module_files()
        forbidden = APPLICATION_LAYER | PRESENTATION_LAYER | RENDER_VIDEO
        for module, path in sorted(files.items()):
            if self._top_subpackage(module) not in CORE_LAYERS:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, _ in _iter_imports(tree, module, path):
                if not target.startswith("osu_toolbox"):
                    continue
                if self._top_subpackage(target) in forbidden:
                    self.fail(f"{module} imports forbidden layer {target}")

    def test_render_and_video_never_import_application_or_gui(self):
        files = self._module_files()
        forbidden = APPLICATION_LAYER | PRESENTATION_LAYER
        for module, path in sorted(files.items()):
            if self._top_subpackage(module) not in RENDER_VIDEO:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, _ in _iter_imports(tree, module, path):
                if not target.startswith("osu_toolbox"):
                    continue
                if self._top_subpackage(target) in forbidden:
                    self.fail(f"{module} imports forbidden layer {target}")

    def test_application_layer_uses_public_core_api(self):
        files = self._module_files()
        for module, path in sorted(files.items()):
            if self._top_subpackage(module) not in APPLICATION_LAYER:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, _ in _iter_imports(tree, module, path):
                if not target.startswith("osu_toolbox"):
                    continue
                sub = self._top_subpackage(target)
                if target == "osu_toolbox" or sub in {"schema", "tools"}:
                    continue
                self.fail(
                    f"{module} imports implementation detail {target}; "
                    "use osu_toolbox public API",
                )

    def test_gui_uses_tool_platform_public_api(self):
        files = self._module_files()
        for module, path in sorted(files.items()):
            if self._top_subpackage(module) not in PRESENTATION_LAYER:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, _ in _iter_imports(tree, module, path):
                if not target.startswith("osu_toolbox"):
                    continue
                sub = self._top_subpackage(target)
                if target == "osu_toolbox" or sub in {"gui", "tools"}:
                    continue
                self.fail(
                    f"{module} imports implementation detail {target}; "
                    "use osu_toolbox.tools public API",
                )

    def test_no_snapshot_import_of_runtime_config(self):
        # 禁止 `from ..config import X` / `from osu_toolbox.config import X`
        # 快照导入可变常量；函数 `configure_runtime`（包级 re-export）除外。
        files = self._module_files()
        for module, path in sorted(files.items()):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, names in _iter_imports(tree, module, path):
                if target == "osu_toolbox.config" or target.startswith("osu_toolbox.config."):
                    if names and set(names) <= {"configure_runtime"}:
                        continue
                    self.fail(
                        f"{module} snapshot-imports runtime config ({target}); "
                        "use `config.X` attribute access",
                    )

    def test_config_recompute_drives_scale(self):
        from osu_toolbox import config

        config.configure_runtime(width=960, height=540)
        self.assertEqual(config.SCALE, 540 / 480.0)
        self.assertEqual(config.HIT_LIGHT_H, int(round(96 * 540 / 480.0)))
        config.configure_runtime(width=1920, height=1080)
        self.assertEqual(config.SCALE, 1080 / 480.0)
        self.assertEqual(config.HIT_LIGHT_H, int(round(96 * 1080 / 480.0)))


# Modules the Explorer right-click menu must not pay for.  Every entry point
# imports the package root first, so an eager import here costs the menu that
# interpreter time before its own work can start (measured at ~230 ms before
# these were deferred).
_MENU_FORBIDDEN_MODULES = (
    "importlib.metadata",
    "email",
    "zipfile",
    "inspect",
    "hashlib",
    "concurrent.futures",
    "osu_toolbox.parsing",
    "osu_toolbox.mania",
    "osu_toolbox.chart_convert",
    "osu_toolbox.speed_change",
    "osu_toolbox.project_info",
    "numpy",
    "pygame",
    "OpenGL",
    "webview",
)


class PackageRootStartupTests(unittest.TestCase):
    """The package root must stay cheap for the shell entry point."""

    def _modules_after(self, code):
        import json
        import subprocess
        import sys

        script = (
            "import json, sys; "
            f"{code}; "
            "print(json.dumps(sorted(sys.modules)))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return set(json.loads(completed.stdout.splitlines()[-1]))

    def test_importing_the_root_package_avoids_every_heavy_module(self):
        loaded = self._modules_after("import osu_toolbox")
        blocked = sorted(
            name
            for name in _MENU_FORBIDDEN_MODULES
            if name in loaded
        )
        self.assertEqual(
            blocked,
            [],
            f"importing osu_toolbox eagerly loads {blocked}",
        )

    def test_importing_the_shell_entry_point_avoids_every_heavy_module(self):
        loaded = self._modules_after("import osu_toolbox.quick")
        blocked = sorted(name for name in _MENU_FORBIDDEN_MODULES if name in loaded)
        self.assertEqual(
            blocked,
            [],
            f"importing the shell entry point eagerly loads {blocked}",
        )

    def test_gui_bootstrap_does_not_load_rendering_dependencies(self):
        loaded = self._modules_after(
            "from osu_toolbox.gui.bridge import Api; Api('.').get_bootstrap()"
        )
        heavy = ("numpy", "pygame", "OpenGL", "osu_toolbox.render.renderer",
                 "osu_toolbox.skin.assets", "osu_toolbox.video.pipeline")
        self.assertEqual([name for name in heavy if name in loaded], [])

    def test_deferred_names_still_resolve_through_the_public_api(self):
        import osu_toolbox

        for name in (
            "parse_osu_file",
            "MrReplay",
            "generate_perfect_replay",
            "convert_paths",
            "change_speed",
            "format_file_marker",
            "judge_replay",
            "build_legacy_scroll_path",
            "find_beatmap_by_md5",
            "get_project_info",
            "ProjectInfo",
            "get_project_version",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(osu_toolbox, name))
        # The version is computed on first access rather than at import.
        self.assertTrue(str(osu_toolbox.__version__))
        self.assertEqual(osu_toolbox.__version__, osu_toolbox.get_project_version())

    def test_an_unknown_attribute_still_raises(self):
        import osu_toolbox

        with self.assertRaises(AttributeError):
            osu_toolbox.definitely_not_a_real_name


if __name__ == "__main__":
    unittest.main()
