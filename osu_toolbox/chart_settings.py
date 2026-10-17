"""转谱工具的 OD/HP 默认值与“转谱工具”设置分类。

只依赖标准库与 ``schema``，因此 GUI、右键菜单与核心转换逻辑都可以安全引用，
不会把解析/转换依赖拉进冷启动路径。
"""

import math

from .schema import GroupSpec, ParamSpec, coerce_param


CHART_SETTING_GROUPS = (GroupSpec("chart_tool", "转谱工具"),)

# osu! 的 OD/HP 合法范围。
CHART_DIFFICULTY_MIN = 0.0
CHART_DIFFICULTY_MAX = 10.0
DEFAULT_CHART_OVERALL_DIFFICULTY = 8.0
DEFAULT_CHART_HP_DRAIN_RATE = 8.0

CHART_SETTING_PARAMS = (
    ParamSpec(
        "chart_overall_difficulty",
        "OD（OverallDifficulty）",
        "float",
        DEFAULT_CHART_OVERALL_DIFFICULTY,
        "chart_tool",
        "转谱写出的 .osu 谱面统一使用的 OverallDifficulty；"
        "目标为 .osu 或 .osz 时生效，.mc、.sm 及对应归档没有该字段。"
        "转谱表单里留空即使用该值",
        minimum=CHART_DIFFICULTY_MIN,
        maximum=CHART_DIFFICULTY_MAX,
        persist=True,
    ),
    ParamSpec(
        "chart_hp_drain_rate",
        "HP（HPDrainRate）",
        "float",
        DEFAULT_CHART_HP_DRAIN_RATE,
        "chart_tool",
        "转谱写出的 .osu 谱面统一使用的 HPDrainRate；"
        "目标为 .osu 或 .osz 时生效，.mc、.sm 及对应归档没有该字段。"
        "转谱表单里留空即使用该值",
        minimum=CHART_DIFFICULTY_MIN,
        maximum=CHART_DIFFICULTY_MAX,
        persist=True,
    ),
)


def default_chart_settings():
    return {spec.key: spec.default for spec in CHART_SETTING_PARAMS}


def normalize_chart_settings(raw):
    """Return the 转谱工具 settings, defaulting only when a value is unusable."""
    values = default_chart_settings()
    raw = raw if isinstance(raw, dict) else {}
    for spec in CHART_SETTING_PARAMS:
        if spec.key not in raw:
            continue
        try:
            values[spec.key] = coerce_param(spec, raw[spec.key])
        except (TypeError, ValueError):
            pass
    return values


def _difficulty_value(value, fallback):
    """Return one usable OD/HP value, falling back when it is empty or invalid."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return min(CHART_DIFFICULTY_MAX, max(CHART_DIFFICULTY_MIN, number))


def resolve_chart_difficulty(raw_settings, overall_difficulty=None, hp_drain_rate=None):
    """Return the effective ``(OD, HP)`` one 转谱 run writes into ``.osu``.

    A per-run value wins; an empty or unusable one falls back to the 转谱工具
    setting.  The GUI form and the right-click menu therefore share this single
    answer: the form only carries what the user typed for that run, and the menu
    -- which has no form -- reads the setting directly.
    """
    values = normalize_chart_settings(raw_settings)
    return (
        _difficulty_value(overall_difficulty, values["chart_overall_difficulty"]),
        _difficulty_value(hp_drain_rate, values["chart_hp_drain_rate"]),
    )


__all__ = (
    "CHART_DIFFICULTY_MAX",
    "CHART_DIFFICULTY_MIN",
    "CHART_SETTING_GROUPS",
    "CHART_SETTING_PARAMS",
    "DEFAULT_CHART_HP_DRAIN_RATE",
    "DEFAULT_CHART_OVERALL_DIFFICULTY",
    "default_chart_settings",
    "normalize_chart_settings",
    "resolve_chart_difficulty",
)
