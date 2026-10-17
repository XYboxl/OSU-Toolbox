"""Versioned persistence for global and per-tool GUI settings."""

import json
import os
import tempfile
from pathlib import Path


SETTINGS_VERSION = 3


def default_settings_path():
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "osu-toolbox" / "settings.json"


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_settings_path()

    @staticmethod
    def _empty_state():
        return {"global": {}, "tools": {}}

    def load_state(self):
        if not self.path.is_file():
            return self._empty_state(), None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty_state(), "设置文件损坏，已恢复默认值"
        if not isinstance(payload, dict):
            return self._empty_state(), "设置文件损坏，已恢复默认值"

        version = payload.get("version")
        if version == 1:
            render = payload.get("render")
            tools = {"render": render if isinstance(render, dict) else {}}
            return {"global": {}, "tools": tools}, None
        if version == 2:
            tools = payload.get("tools")
            if not isinstance(tools, dict):
                return self._empty_state(), "设置文件损坏，已恢复默认值"
            return {"global": {}, "tools": self._clean_tools(tools)}, None
        if version != SETTINGS_VERSION:
            return self._empty_state(), "设置文件版本不兼容，已恢复默认值"

        global_settings = payload.get("global")
        tools = payload.get("tools")
        if not isinstance(global_settings, dict) or not isinstance(tools, dict):
            return self._empty_state(), "设置文件损坏，已恢复默认值"
        return {
            "global": dict(global_settings),
            "tools": self._clean_tools(tools),
        }, None

    def load(self):
        state, warning = self.load_state()
        return state["tools"], warning

    def save_state(self, global_settings, tool_settings):
        payload = {
            "version": SETTINGS_VERSION,
            "global": global_settings if isinstance(global_settings, dict) else {},
            "tools": tool_settings if isinstance(tool_settings, dict) else {},
        }
        return self._write(payload)

    def save(self, tool_settings):
        state, _warning = self.load_state()
        return self.save_state(state["global"], tool_settings)

    def reset(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            return f"设置重置失败：{exc}"
        return None

    def _write(self, payload):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix="settings-",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.remove(temp_name)
        except OSError as exc:
            return f"设置保存失败：{exc}"
        return None

    @staticmethod
    def _clean_tools(tools):
        return {
            str(tool_id): values
            for tool_id, values in tools.items()
            if isinstance(values, dict)
        }
