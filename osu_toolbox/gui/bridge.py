"""Generic pywebview bridge for registered toolbox tools."""

import inspect
import os

from osu_toolbox import (
    ShellMenuManager,
    get_project_info,
)
from osu_toolbox.tools import build_default_registry

from .settings import SettingsStore
from .global_settings import (
    GLOBAL_SETTING_PARAMS,
    global_settings_schema,
    shell_menu_settings_schema,
)
from .tool_runtime import ToolRuntime


def _dialog_file_types(filters):
    """Build pywebview file-dialog filters from a schema filter list.

    Each returned entry is one complete ``描述 (模式)`` filter, because
    pywebview's Windows backend joins the entries without re-adding a
    separator::

        dialog.Filter = '|'.join(
            ['{0} ({1})|{1}'.format(*parse_file_type(f)) for f in file_types]
        )

    Feeding it several bare entries therefore yields
    ``"谱面或归档 (*.osu)|*.osu|谱面或归档 (*.mc)|*.mc"``, whose tail Windows
    reads as the pattern of the first filter.  The dialog then shows one entry
    literally named "谱面或归档 (*.osu)" and only ``*.osu`` files can be
    picked -- the bug that limited the 变速 dialog to ``.osu``.

    A schema filter may list several patterns separated by ``;``: every pattern
    becomes its own entry so each format stays reachable, and the whole set is
    also offered first as one combined entry.
    """
    result = []
    for label, pattern in filters:
        items = [item.strip() for item in str(pattern).split(";") if item.strip()]
        if not items:
            continue
        if len(items) > 1:
            result.append(f"{label} ({';'.join(items)})")
        for item in items:
            result.append(f"{label} ({item})")
    return tuple(result)


class Api:
    def __init__(
        self,
        search_root,
        settings_store=None,
        registry=None,
        runtime_factory=ToolRuntime,
        output_root=None,
        shell_menu_manager=None,
        shell_executable_path=None,
    ):
        self._search_root = os.path.abspath(search_root)
        self._output_root = os.path.abspath(output_root or self._search_root)
        self._settings_store = settings_store or SettingsStore()
        self._registry = registry or build_default_registry()
        self._shell_menu_manager = shell_menu_manager or ShellMenuManager(shell_executable_path)
        runtime_parameters = inspect.signature(runtime_factory).parameters.values()
        supports_output_root = any(
            parameter.name == "output_root"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in runtime_parameters
        )
        supports_shell_menu = any(
            parameter.name == "shell_menu_manager"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in runtime_parameters
        )
        runtime_args = (
            self._registry,
            self._search_root,
            self._settings_store,
        )
        runtime_kwargs = {}
        if supports_output_root:
            runtime_kwargs["output_root"] = self._output_root
        if supports_shell_menu:
            runtime_kwargs["shell_menu_manager"] = self._shell_menu_manager
        self._runtime = runtime_factory(*runtime_args, **runtime_kwargs)
        self._window = None

    def _set_window(self, window):
        self._window = window

    def get_bootstrap(self):
        runtime_state = self._runtime.bootstrap_state()
        runtime_state.setdefault("shell_menu_settings", {})
        if "shell_menu" not in runtime_state:
            runtime_state["shell_menu"] = self._shell_menu_manager.status()
        global_schema = global_settings_schema()
        return {
            "project": get_project_info().as_dict(),
            "tools": [spec.as_dict() for spec in self._registry.list_specs()],
            "global_settings_schema": global_schema,
            "shell_menu_schema": shell_menu_settings_schema(
                self._shell_menu_manager.executable_path,
            ),
            # The categories the settings page shows are exactly the ones the
            # schema declares; rebuilding the list here let a new category be
            # saved but never appear in the UI.
            "settings_categories": list(global_schema.get("categories", ())),
            **runtime_state,
        }

    def pick_path(self, tool_id, param_key):
        try:
            definition = self._registry.get(tool_id)
        except KeyError:
            return {"ok": False, "message": "未注册的工具"}
        spec = next(
            (item for item in definition.spec.params if item.key == param_key),
            None,
        )
        return self._pick_path_for_spec(spec)

    def _pick_path_for_spec(self, spec):
        if self._window is None:
            return {"ok": False, "message": "窗口尚未就绪"}
        if spec is None or spec.dialog_mode is None:
            return {"ok": False, "message": "不支持的文件参数"}
        try:
            import webview

            dialog_types = {
                "open_file": webview.OPEN_DIALOG,
                "save_file": webview.SAVE_DIALOG,
                "folder": webview.FOLDER_DIALOG,
            }
            kwargs = {"allow_multiple": bool(spec.multiple)}
            if spec.filters and spec.dialog_mode != "folder":
                kwargs["file_types"] = _dialog_file_types(spec.filters)
            result = self._window.create_file_dialog(
                dialog_types[spec.dialog_mode],
                **kwargs,
            )
        except Exception as exc:
            return {"ok": False, "message": f"文件对话框打开失败: {exc}"}
        if not result:
            return {"ok": True, "path": None}
        if isinstance(result, (tuple, list)):
            path = [str(item) for item in result if item]
            if not spec.multiple:
                path = path[0] if path else None
        else:
            path = result
        if spec.multiple:
            return {"ok": True, "path": path if isinstance(path, list) else ([str(path)] if path else [])}
        return {"ok": True, "path": str(path) if path else None}

    def start_tool(self, tool_id, raw_params, confirmation_token=None):
        return self._runtime.start(tool_id, raw_params, confirmation_token)

    def pick_global_path(self, param_key):
        spec = next(
            (item for item in GLOBAL_SETTING_PARAMS if item.key == param_key),
            None,
        )
        return self._pick_path_for_spec(spec)

    def save_global_settings(self, raw_settings):
        return self._runtime.save_global_settings(raw_settings)

    def save_shell_menu_settings(self, raw_settings):
        method = getattr(self._runtime, "save_shell_menu_settings", None)
        if not callable(method):
            return {"status": "error", "code": "unsupported", "message": "不支持右键菜单设置"}
        return method(raw_settings)

    def shell_menu_status(self):
        method = getattr(self._runtime, "shell_menu_status", None)
        if callable(method):
            return method()
        return self._shell_menu_manager.status()

    def get_shell_menu_state(self):
        method = getattr(self._runtime, "get_shell_menu_state", None)
        if callable(method):
            return method()
        return {
            "settings": {},
            "status": self._shell_menu_manager.status(),
        }

    def enable_all_shell_menu(self):
        method = getattr(self._runtime, "set_all_shell_menu", None)
        if callable(method):
            return method(True)
        return {"status": "error", "code": "unsupported", "message": "不支持右键菜单设置"}

    def disable_all_shell_menu(self):
        method = getattr(self._runtime, "set_all_shell_menu", None)
        if callable(method):
            return method(False)
        return {"status": "error", "code": "unsupported", "message": "不支持右键菜单设置"}

    def enable_shell_menu(self, operation):
        method = getattr(self._runtime, "set_shell_menu_item", None)
        if callable(method):
            return method(operation, True)
        return {"status": "error", "code": "unsupported", "message": "不支持右键菜单设置"}

    def disable_shell_menu(self, operation):
        method = getattr(self._runtime, "set_shell_menu_item", None)
        if callable(method):
            return method(operation, False)
        return {"status": "error", "code": "unsupported", "message": "不支持右键菜单设置"}

    def repair_shell_menu(self):
        method = getattr(self._runtime, "repair_shell_menu", None)
        if callable(method):
            return method()
        return self._shell_menu_manager.repair({})

    def uninstall_shell_menu(self):
        method = getattr(self._runtime, "uninstall_shell_menu", None)
        if callable(method):
            return method()
        return self._shell_menu_manager.uninstall_all()

    def reset_all_settings(self):
        return self._runtime.reset_all_settings()

    def prefill_tool(self, tool_id, raw_params, changed_key):
        return self._runtime.prefill(tool_id, raw_params, changed_key)

    def drain_events(self, run_id=None):
        return self._runtime.drain_events(run_id)

    def cancel_tool(self, run_id=None):
        return self._runtime.cancel(run_id)

    def _handle_closing(self):
        if not self._runtime.is_running():
            return True
        self._runtime.cancel(self._runtime.active_run_id())
        self._runtime.warn_active_run("正在取消任务并清理临时文件，完成后可安全退出")
        return False
