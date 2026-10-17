"""变速工具的命名标识规则与设置项。

只依赖标准库与 ``schema``，因此 GUI、右键菜单与核心变速逻辑都可以安全引用，
不会把解析/渲染依赖拉进冷启动路径。
"""

import re

from .schema import GroupSpec, ParamSpec, coerce_params


SPEED_SETTING_GROUPS = (GroupSpec("speed_tool", "变速工具"),)

# 默认标识：一个空格加 ``{num}x``，例如 ``speed 1.5x``。
DEFAULT_SPEED_FILE_SUFFIX = " {num}x"
DEFAULT_SPEED_DIFFICULTY_MARKER = " {num}x"

SPEED_SETTING_PARAMS = (
    ParamSpec(
        "speed_keep_pitch",
        "变速时保持音调",
        "bool",
        False,
        "speed_tool",
        "关闭（默认）时音调随速度一起变化；开启后变速会额外做一次时间伸缩以还原原音调，"
        "音频转码耗时略有增加",
        persist=True,
    ),
    ParamSpec(
        "speed_file_suffix",
        "文件名标识",
        "text",
        " {num}x",
        "speed_tool",
        "追加到变速后的谱面、音乐与归档文件名；{num} 会替换为倍率，默认前缀一个空格（“ {num}x”）",
        persist=True,
    ),
    ParamSpec(
        "speed_difficulty_marker",
        "难度名标识",
        "text",
        " {num}x",
        "speed_tool",
        "追加到变速后的谱面难度名（.osu 的 Version、.mc 的 version、.sm 的难度描述）；{num} 会替换为倍率",
        persist=True,
    ),
)

_NUMBER_TOKEN = "{num}"
_FALLBACK_PATTERN = " {num}x"
# 倍率文本里的数字位数上限：0.05x 一档时最多两位小数，再多就是手输的怪值。
_NUMBER_MAX_DECIMALS = 4
# Windows 文件名不允许的字符；难度名是自由文本，不做这层过滤。
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def default_speed_settings():
    """Return the effective defaults, which keep the marker's leading space."""
    return {spec.key: spec.default for spec in SPEED_SETTING_PARAMS}


def normalize_speed_settings(raw):
    """Return the 变速工具 settings, defaulting only when a key is absent.

    A ``text`` parameter is trimmed by :func:`coerce_param`, but leading and
    trailing spaces are meaningful for the markers (the default is
    ``" {num}x"``), so a string is taken verbatim.  An empty string is a
    deliberate request for no marker and must survive, so it is kept as-is too;
    only a missing or unusable value falls back to the default.
    """
    values = default_speed_settings()
    raw = raw if isinstance(raw, dict) else {}
    try:
        coerced = coerce_params(SPEED_SETTING_PARAMS, raw)
    except (TypeError, ValueError):
        coerced = {}
    for spec in SPEED_SETTING_PARAMS:
        if spec.key not in raw:
            continue
        value = raw.get(spec.key)
        if spec.kind == "bool":
            try:
                values[spec.key] = coerce_params((spec,), raw)[spec.key]
            except (TypeError, ValueError):
                pass
            continue
        if isinstance(value, str):
            # ``"   "`` normalises to no marker rather than to a marker made of
            # spaces.
            values[spec.key] = value if value.strip() else ""
            continue
        candidate = coerced.get(spec.key)
        if isinstance(candidate, str):
            values[spec.key] = candidate
    return values


def format_speed_number(speed):
    """Return the shortest exact decimal text for a speed multiplier."""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return "1"
    text = f"{value:.{_NUMBER_MAX_DECIMALS}f}".rstrip("0").rstrip(".")
    return text or "0"


def _marker(pattern, speed):
    template = str(pattern or "")
    if not template.strip():
        template = _FALLBACK_PATTERN
    number = format_speed_number(speed)
    if _NUMBER_TOKEN in template:
        return template.replace(_NUMBER_TOKEN, number)
    # 没写占位符时把倍率补在末尾，用户的标识仍然生效。
    return f"{template}{number}"


def format_file_marker(pattern, speed):
    """Return the file-name marker, with characters Windows rejects removed."""
    marker = _marker(pattern, speed)
    return _INVALID_FILENAME_CHARS.sub("_", marker)


def format_difficulty_marker(pattern, speed):
    """Return the difficulty-name marker; free text, so only line breaks go."""
    return _marker(pattern, speed).replace("\r", " ").replace("\n", " ")


def apply_file_marker(path, pattern, speed):
    """Insert the marker before a path's extension (``song.mp3`` -> ``song 1.5x.mp3``)."""
    text = str(path)
    stem, extension = _split_suffix(text)
    return f"{stem}{format_file_marker(pattern, speed)}{extension}"


def _split_suffix(text):
    index = text.rfind(".")
    slash = max(text.rfind("/"), text.rfind("\\"))
    if index <= slash + 1:
        return text, ""
    return text[:index], text[index:]


__all__ = (
    "DEFAULT_SPEED_DIFFICULTY_MARKER",
    "DEFAULT_SPEED_FILE_SUFFIX",
    "SPEED_SETTING_GROUPS",
    "SPEED_SETTING_PARAMS",
    "apply_file_marker",
    "default_speed_settings",
    "format_difficulty_marker",
    "format_file_marker",
    "format_speed_number",
    "normalize_speed_settings",
)
