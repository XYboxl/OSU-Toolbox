"""Explicit registry for tools shipped by the application."""

import re

from osu_toolbox.schema import SUPPORTED_PARAM_KINDS

from .base import (
    SUPPORTED_CONCURRENCY,
    SUPPORTED_RESULT_KINDS,
    ToolDefinition,
)


_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_DIALOG_MODES = frozenset(("open_file", "save_file", "folder"))


class ToolRegistry:
    def __init__(self, definitions=()):
        self._definitions = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition):
        self._validate(definition)
        tool_id = definition.spec.id
        if tool_id in self._definitions:
            raise ValueError(f"duplicate tool id: {tool_id}")
        self._definitions[tool_id] = definition
        return definition

    def unregister(self, tool_id):
        try:
            return self._definitions.pop(tool_id)
        except KeyError:
            raise KeyError(f"unknown tool: {tool_id}")

    def get(self, tool_id):
        try:
            return self._definitions[tool_id]
        except KeyError:
            raise KeyError(f"unknown tool: {tool_id}")

    def list_definitions(self):
        return tuple(self._definitions.values())

    def list_specs(self):
        return tuple(item.spec for item in self._definitions.values())

    @staticmethod
    def _validate(definition):
        spec = definition.spec
        if not _TOOL_ID_PATTERN.fullmatch(spec.id):
            raise ValueError(f"invalid tool id: {spec.id}")
        if not spec.title or not spec.description or not spec.category or not spec.action_label:
            raise ValueError(f"tool metadata is incomplete: {spec.id}")
        if not callable(getattr(definition.controller, "prepare", None)):
            raise ValueError(f"tool controller has no prepare method: {spec.id}")
        if not callable(getattr(definition.controller, "run", None)):
            raise ValueError(f"tool controller has no run method: {spec.id}")
        if spec.result_kind not in SUPPORTED_RESULT_KINDS:
            raise ValueError(f"unsupported result kind: {spec.result_kind}")
        if spec.concurrency not in SUPPORTED_CONCURRENCY:
            raise ValueError(f"unsupported concurrency policy: {spec.concurrency}")
        group_keys = [group.key for group in spec.groups]
        if len(group_keys) != len(set(group_keys)):
            raise ValueError(f"duplicate group key in tool: {spec.id}")
        param_keys = [param.key for param in spec.params]
        if len(param_keys) != len(set(param_keys)):
            raise ValueError(f"duplicate parameter key in tool: {spec.id}")
        known_groups = set(group_keys)
        for param in spec.params:
            if param.kind not in SUPPORTED_PARAM_KINDS:
                raise ValueError(f"unsupported parameter kind: {param.kind}")
            if param.group not in known_groups:
                raise ValueError(
                    f"unknown group {param.group!r} for parameter {param.key!r}",
                )
            if param.dialog_mode is not None and param.dialog_mode not in _DIALOG_MODES:
                raise ValueError(f"unsupported dialog mode: {param.dialog_mode}")


def build_default_registry():
    from .chart_convert import CHART_CONVERT_TOOL
    from .render_video import RENDER_VIDEO_TOOL
    from .replay_edit import REPLAY_EDIT_TOOL
    from .replay_generate import REPLAY_GENERATE_TOOL
    from .speed_change import SPEED_CHANGE_TOOL

    registry = ToolRegistry()
    registry.register(RENDER_VIDEO_TOOL)
    registry.register(REPLAY_GENERATE_TOOL)
    registry.register(REPLAY_EDIT_TOOL)
    registry.register(CHART_CONVERT_TOOL)
    registry.register(SPEED_CHANGE_TOOL)
    return registry
