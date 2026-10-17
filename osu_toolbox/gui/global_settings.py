"""Explicit schema and normalization for application-wide settings."""

import os

from osu_toolbox import (
    CHART_SETTING_GROUPS,
    CHART_SETTING_PARAMS,
    SHELL_MENU_SETTING_GROUPS,
    SHELL_MENU_SETTING_PARAMS,
    SPEED_SETTING_GROUPS,
    SPEED_SETTING_PARAMS,
    default_chart_settings,
    default_shell_menu_settings,
    default_speed_settings,
    normalize_chart_settings,
    normalize_shell_menu_settings,
    normalize_speed_settings,
    shell_menu_schema,
)
from osu_toolbox.schema import GroupSpec, ParamSpec, coerce_params, default_params


GLOBAL_SETTING_GROUPS = (
    GroupSpec("general", "通用"),
)

GLOBAL_SETTING_PARAMS = (
    ParamSpec(
        "osu_directory",
        "osu! 所在目录",
        "path",
        None,
        "general",
        "选择同时包含 Replays 和 Songs 文件夹的 osu! 数据目录",
        dialog_mode="folder",
        persist=True,
    ),
    ParamSpec(
        "malody_directory",
        "Malody 所在目录",
        "path",
        None,
        "general",
        "选择同时包含 replay 和 beatmap 文件夹的 Malody 数据目录",
        dialog_mode="folder",
        persist=True,
    ),
    ParamSpec(
        "shell_menu_notifications",
        "右键菜单提示",
        "bool",
        True,
        "general",
        "关闭后只显示失败报错",
        persist=True,
    ),
)


def default_global_settings():
    values = default_params(GLOBAL_SETTING_PARAMS)
    values.update(default_speed_settings())
    values.update(default_chart_settings())
    return values


def normalize_global_settings(raw):
    values = coerce_params(GLOBAL_SETTING_PARAMS, raw)
    for spec in GLOBAL_SETTING_PARAMS:
        if spec.kind == "path" and values[spec.key]:
            values[spec.key] = os.path.abspath(
                os.path.expandvars(os.path.expanduser(values[spec.key])),
            )
    values.update(normalize_speed_settings(raw))
    values.update(normalize_chart_settings(raw))
    return values


def global_settings_schema():
    schema = {
        "groups": [group.as_dict() for group in GLOBAL_SETTING_GROUPS],
        "params": [param.as_dict() for param in GLOBAL_SETTING_PARAMS],
    }
    # Keep the original fields stable for older clients while advertising the
    # newer categories through an additive field.
    schema["categories"] = [
        *schema["groups"],
        *[group.as_dict() for group in SPEED_SETTING_GROUPS],
        *[group.as_dict() for group in CHART_SETTING_GROUPS],
        *[group.as_dict() for group in SHELL_MENU_SETTING_GROUPS],
    ]
    schema["speed_tool"] = {
        "groups": [group.as_dict() for group in SPEED_SETTING_GROUPS],
        "params": [param.as_dict() for param in SPEED_SETTING_PARAMS],
    }
    schema["chart_tool"] = {
        "groups": [group.as_dict() for group in CHART_SETTING_GROUPS],
        "params": [param.as_dict() for param in CHART_SETTING_PARAMS],
    }
    schema["shell_menu"] = shell_menu_schema()
    return schema


def shell_menu_settings_schema(executable_path=None):
    """Return the dedicated right-click settings category schema."""
    return shell_menu_schema(executable_path)


__all__ = (
    "CHART_SETTING_GROUPS",
    "CHART_SETTING_PARAMS",
    "GLOBAL_SETTING_GROUPS",
    "GLOBAL_SETTING_PARAMS",
    "SHELL_MENU_SETTING_GROUPS",
    "SHELL_MENU_SETTING_PARAMS",
    "SPEED_SETTING_GROUPS",
    "SPEED_SETTING_PARAMS",
    "default_chart_settings",
    "default_global_settings",
    "default_shell_menu_settings",
    "default_speed_settings",
    "global_settings_schema",
    "normalize_chart_settings",
    "normalize_global_settings",
    "normalize_shell_menu_settings",
    "normalize_speed_settings",
    "shell_menu_settings_schema",
)
