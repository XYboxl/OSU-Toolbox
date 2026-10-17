"""skin.ini 解析与皮肤图片引用候选。"""

import math
import os
import re
import sys

import numpy as np

from ..legacy_constants import (
    LEGACY_DEFAULT_COLUMN_START_480,
    LEGACY_DEFAULT_COLUMN_WIDTH_480,
    LEGACY_DEFAULT_COMBO_POSITION_480,
    LEGACY_DEFAULT_HIT_POSITION_480,
    LEGACY_DEFAULT_LIGHT_FPS,
    LEGACY_DEFAULT_LIGHT_POSITION_480,
    LEGACY_DEFAULT_SCORE_POSITION_480,
    LEGACY_INVALID_LIGHT_FPS,
    LEGACY_NOTE_BODY_REPEAT_BOTTOM,
    LEGACY_NOTE_BODY_REPEAT_TOP,
    LEGACY_NOTE_BODY_REPEAT_TOP_AND_BOTTOM,
    LEGACY_NOTE_BODY_STRETCH,
)


_GENERAL_PARAMS = frozenset({
    "AnimationFramerate",
    "ComboBurstRandom",
    "Version",
})
_FONT_PARAMS = frozenset({
    "ComboOverlap",
    "ComboPrefix",
    "ScoreOverlap",
    "ScorePrefix",
})
_MANIA_PARAMS = frozenset({
    "BarlineHeight",
    "ColourBarline",
    "ColourBreak",
    "ColourColumnLine",
    "ColourHold",
    "ColourJudgementLine",
    "ColourKeyWarning",
    "ColumnLineWidth",
    "ColumnRight",
    "ColumnSpacing",
    "ColumnStart",
    "ColumnWidth",
    "ComboBurstStyle",
    "ComboPosition",
    "Hit0",
    "Hit50",
    "Hit100",
    "Hit200",
    "Hit300",
    "Hit300g",
    "HitPosition",
    "JudgementLine",
    "KeyFlipWhenUpsideDown",
    "KeysUnderNotes",
    "LightFramePerSecond",
    "LightingL",
    "LightingLWidth",
    "LightingN",
    "LightingNWidth",
    "LightPosition",
    "NoteBodyStyle",
    "NoteFlipWhenUpsideDown",
    "NoteFlipWhenUpsideDownH",
    "NoteFlipWhenUpsideDownL",
    "NoteFlipWhenUpsideDownT",
    "ScorePosition",
    "SeparateScore",
    "SpecialStyle",
    "SplitStages",
    "StageBottom",
    "StageHint",
    "StageLeft",
    "StageLight",
    "StageRight",
    "StageSeparation",
    "UpsideDown",
    "WarningArrow",
    "WidthForNoteHeightScale",
})
_MANIA_INDEXED_PARAM = re.compile(
    r"(?:Colour(?:Light)?[0-9]+"
    r"|KeyFlipWhenUpsideDown[0-9]+D?"
    r"|KeyImage[0-9]+D?"
    r"|NoteBodyStyle[0-9]+"
    r"|NoteFlipWhenUpsideDown[0-9]+[HLT]?"
    r"|NoteImage[0-9]+[HLT]?)\Z"
)
_STABLE_INT = re.compile(r"[+-]?[0-9]+\Z")
_STABLE_FLOAT = re.compile(
    r"[+-]?(?:"
    r"(?:[0-9]+(?:,[0-9]+)*)(?:\.[0-9]*)?"
    r"|\.[0-9]+"
    r")(?:[eE][+-]?[0-9]+)?\Z"
)


class SkinConfigError(ValueError):
    """Raised when a user-selected custom skin cannot be used."""


def _is_mania_param(key):
    return key in _MANIA_PARAMS or _MANIA_INDEXED_PARAM.fullmatch(key) is not None


def _skin_root(skin_dir):
    """返回皮肤绝对根目录；不存在返回 None"""
    if not skin_dir:
        return None
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.isabs(skin_dir):
        root = skin_dir
    else:
        # CLI paths are normally relative to the caller's working directory.
        # Keep the package-relative fallbacks for existing GUI callers.
        candidates = (
            os.path.abspath(skin_dir),
            os.path.join(base, skin_dir),
            os.path.join(base, "skins", skin_dir),
        )
        root = next(
            (candidate for candidate in candidates if os.path.isdir(candidate)),
            None,
        )
    return os.path.abspath(root) if root and os.path.isdir(root) else None


def _resolve_skin_ini(root):
    """Resolve skin.ini without relying on a case-insensitive filesystem."""
    exact = os.path.join(root, "skin.ini")
    if os.path.isfile(exact):
        return exact
    try:
        match = next(
            name for name in os.listdir(root)
            if name.casefold() == "skin.ini"
        )
    except (OSError, StopIteration):
        return None
    path = os.path.join(root, match)
    return path if os.path.isfile(path) else None


def parse_skin_ini_mania(root, key_count, _status=None):
    """解析通用字段与匹配 key_count 的 [Mania] 块。

    Stable 对现有但没有 skin.ini 的皮肤目录按最新版本处理；skin.ini
    存在但没有匹配的 Mania 块时，仍保留通用字段并构造默认 Mania 配置。
    """
    ini = _resolve_skin_ini(root)
    if ini is None:
        if _status is not None:
            _status.update({
                "ini_path": None,
                "mania_keys": (),
                "matched_mania_keys": False,
            })
        return {"Version": "latest"}

    blocks = []
    sections = {"General": {}, "Colours": {}, "Fonts": {}}
    cur = None
    with open(ini, encoding="utf-8-sig", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if line.startswith("["):
                close = line.find("]")
                section = line.strip("[") if close < 0 else line[1:close]
                if section == "Mania":
                    cur = {}
                    blocks.append(cur)
                else:
                    cur = sections.get(section)
                continue

            line = line.split("//", 1)[0].strip()
            if not line or line.startswith("//") or line.startswith("`"):
                continue
            if cur is None or ":" not in line:
                continue
            parts = line.split(":")
            key = parts[0].strip()
            if key:
                cur.setdefault(key, parts[1].strip())

    # 只选取 Keys == key_count 的块。Stable 会为不匹配的键数创建默认配置，
    # 不会误用其他键数的第一个块。
    chosen = None
    mania_keys = []
    for b in blocks:
        parsed_keys = _parse_int(b.get("Keys"), None)
        if parsed_keys is not None:
            mania_keys.append(parsed_keys)
        if parsed_keys == key_count:
            chosen = b
            break
    if _status is not None:
        _status.update({
            "ini_path": ini,
            "mania_keys": tuple(mania_keys),
            "matched_mania_keys": chosen is not None,
        })
    params = {
        key: value
        for key, value in sections["General"].items()
        if key in _GENERAL_PARAMS
    }
    params.update({
        key: value
        for key, value in sections["Fonts"].items()
        if key in _FONT_PARAMS
    })
    if chosen is not None:
        params.update({
            key: value
            for key, value in chosen.items()
            if _is_mania_param(key)
        })
    return params


def iter_skin_image_candidates(root, ref):
    """osu! 静态图片候选：可省扩展名，优先 @2x。"""
    if not ref:
        return
    ref = _normalise_skin_image_ref(ref)
    candidates = []
    base, ext = os.path.splitext(ref)
    if ext:
        is_2x_ref = base.endswith("@2x")
        if not is_2x_ref:
            candidates.append((base + "@2x" + ext, True))
        candidates.append((ref, is_2x_ref))
    else:
        base = ref
        candidates.append((base + "@2x.png", True))
        candidates.append((base + ".png", False))
        candidates.append((base + "@2x.jpg", True))
        candidates.append((base + ".jpg", False))
    seen = set()
    for rel, is_2x in candidates:
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        p = _resolve_skin_path(root, rel)
        if p is not None:
            yield p, is_2x


def iter_skin_animation_candidates(root, ref):
    """osu! 动画帧：每一帧独立优先 @2x，首个缺号结束序列。"""
    if not ref:
        return
    idx = 0
    while True:
        path, is_2x = resolve_skin_animation_frame(root, ref, idx)
        if path is None:
            break
        yield path, is_2x
        idx += 1


def resolve_skin_animation_frame(root, ref, index):
    """Resolve one dashed Stable animation frame without crossing providers."""
    if not ref:
        return None, False
    ref = _normalise_skin_image_ref(ref)
    base, ext = os.path.splitext(ref)
    if ext:
        frame_ref = f"{base}-{int(index)}{ext}"
    else:
        frame_ref = f"{ref}-{int(index)}"
    return resolve_skin_image(root, frame_ref)


def _normalise_skin_image_ref(ref):
    """Match LegacySkin.GetTexture's user-controlled lookup normalisation."""
    ref = str(ref).strip().replace("\\", "/")
    base, ext = os.path.splitext(ref)
    base = base.replace("@2x", "")
    return base + ext


def _resolve_skin_path(root, relative_path):
    """Resolve a skin path case-insensitively without escaping the skin root."""
    root = os.path.abspath(root)
    current = root
    parts = [part for part in relative_path.replace("\\", "/").split("/") if part not in ("", ".")]
    for part in parts:
        if part == "..":
            return None
        exact = os.path.join(current, part)
        if os.path.exists(exact):
            current = exact
            continue
        try:
            match = next(
                name for name in os.listdir(current)
                if name.casefold() == part.casefold()
            )
        except (OSError, StopIteration):
            return None
        current = os.path.join(current, match)
    resolved = os.path.abspath(current)
    try:
        if os.path.commonpath((root, resolved)) != root:
            return None
    except ValueError:
        return None
    return resolved if os.path.isfile(resolved) else None


def resolve_skin_image(root, ref):
    """osu! 图片路径解析：可省扩展名，优先 @2x。返回 (绝对路径, is_2x) 或 (None, False)"""
    for p, is_2x in iter_skin_image_candidates(root, ref):
        return p, is_2x
    return None, False


def _split_ints(s, default, count):
    """Parse legacy float arrays without extending the final supplied value."""
    count = max(0, int(count))
    if isinstance(default, (list, tuple, np.ndarray)):
        defaults = [float(v) for v in default[:count]]
        defaults.extend([0.0] * (count - len(defaults)))
    else:
        defaults = [float(default)] * count
    if s is None:
        return defaults
    vals = list(defaults)
    for idx, part in enumerate(str(s).split(',')):
        if idx >= count:
            break
        vals[idx] = _parse_stable_float(part, 0.0, single=True)
    return vals


def _parse_bool(value, default=False):
    if value is None:
        return default
    text = str(value).strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    parsed = _parse_int(text, None)
    return default if parsed is None else parsed != 0


def _column_bool(params, base, idx, default):
    return _parse_bool(params.get(f"{base}{idx}"), default)


def _parse_stable_float(value, default, single):
    if value is None:
        return default
    text = str(value).strip()
    special = {
        "Infinity": float("inf"),
        "-Infinity": float("-inf"),
        "NaN": float("nan"),
    }.get(text)
    if special is not None:
        return special
    if _STABLE_FLOAT.fullmatch(text) is None:
        return default
    try:
        parsed = float(text.replace(",", ""))
    except (OverflowError, ValueError):
        return default
    if not np.isfinite(parsed):
        return default
    limit = float(np.finfo(np.float32).max) if single else sys.float_info.max
    if abs(parsed) > limit:
        return default
    if not single:
        return parsed
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        parsed_single = np.float32(parsed)
    return float(parsed_single) if np.isfinite(parsed_single) else default


def _parse_float(value, default):
    return _parse_stable_float(value, default, single=True)


def _stable_clamp(value, lower, upper):
    """Match Stable's clamp helper, including its NaN behaviour."""
    if math.isnan(lower) or math.isnan(upper):
        return float("nan")
    if value > upper:
        return upper
    if value < lower:
        return lower
    return value


def _parse_skin_version(value):
    if value is None:
        return "1.0", 1.0
    text = str(value).strip()
    if text == "latest":
        return text, 2.7
    version = _parse_stable_float(text, None, single=False)
    if version is None:
        raise SkinConfigError(f"invalid skin Version: {text!r}")
    return text, version


def _parse_note_body_style(value, default):
    if value is None:
        return default
    names = {
        "stretch": LEGACY_NOTE_BODY_STRETCH,
        "repeattop": LEGACY_NOTE_BODY_REPEAT_TOP,
        "repeatbottom": LEGACY_NOTE_BODY_REPEAT_BOTTOM,
        "repeattopandbottom": LEGACY_NOTE_BODY_REPEAT_TOP_AND_BOTTOM,
    }
    text = str(value).strip()
    named_style = names.get(text.lower())
    if named_style is not None:
        return named_style
    style = _parse_int(text, None)
    return default if style is None else style


def _parse_enum(value, names, default):
    if value is None:
        return default
    text = str(value).strip()
    named_value = names.get(text.lower())
    if named_value is not None:
        return named_value
    enum_value = _parse_int(text, None)
    return default if enum_value is None else enum_value


def _parse_int(value, default):
    if value is None:
        return default
    text = str(value).strip()
    if _STABLE_INT.fullmatch(text) is None:
        return default
    try:
        parsed = int(text, 10)
    except ValueError:
        return default
    if not -(2 ** 31) <= parsed < 2 ** 31:
        return default
    return parsed


def _parse_rgba(value, default):
    if not value:
        return default
    raw_parts = str(value).split(",")
    if len(raw_parts) not in (3, 4):
        return default
    parts = []
    for raw in raw_parts:
        parsed = _parse_int(raw, None)
        if parsed is None:
            return default
        parts.append(parsed & 0xFF)
    if len(parts) == 3:
        parts.append(255)
    return tuple(part / 255.0 for part in parts)


def _legacy_fallback_column_index(
    col_idx, key_count, special_style=0, split_stages=False,
):
    """Return Stable's default mania resource class: 1, 2, or S."""
    if key_count <= 0:
        return "1"

    first_stage_count = (key_count + 1) // 2 if split_stages else key_count
    second_stage = split_stages and col_idx >= first_stage_count
    if second_stage:
        local_index = col_idx - first_stage_count
        stage_columns = key_count - first_stage_count
        if special_style == 1:
            special_style = 2
        elif special_style == 2:
            special_style = 1
    else:
        local_index = col_idx
        stage_columns = first_stage_count

    supports_special_style = stage_columns > 4 and stage_columns % 2 == 0
    styled_special = special_style != 0 and supports_special_style
    if styled_special:
        special_index = stage_columns - 1 if special_style == 2 else 0
    elif stage_columns % 2 == 1:
        special_index = stage_columns // 2
    else:
        special_index = -1
    if local_index == special_index:
        return "S"

    if styled_special:
        alternate = (
            (local_index % 2 == 1 and special_style == 2)
            or (local_index % 2 == 0 and special_style == 1)
        )
    else:
        middle = stage_columns // 2
        alternate = (
            stage_columns % 2 == 1
            and local_index != middle
            and local_index % 2 == 1
        ) or (
            stage_columns % 2 == 0
            and (
                (local_index < middle and local_index % 2 == 1)
                or (local_index >= middle and local_index % 2 == 0)
            )
        )
    return "2" if alternate else "1"


class SkinConfig:
    """从 skin.ini 提取的 mania 几何与图片引用（参考系 640×480 osu!像素）"""

    def __init__(
        self,
        root,
        params,
        key_count,
        beatmap_special_style=False,
        beatmap_root=None,
    ):
        self.root = root
        self.beatmap_root = (
            os.path.abspath(beatmap_root)
            if beatmap_root and os.path.isdir(beatmap_root)
            else None
        )
        self.key_count = key_count
        self.beatmap_special_style = bool(beatmap_special_style)
        self.version_text, self.version = _parse_skin_version(params.get("Version", "1.0"))
        self.supports_mania_25 = self.version >= 2.5
        self.animation_framerate = _parse_int(params.get("AnimationFramerate"), -1)
        self.has_column_start = params.get("ColumnStart") is not None
        self.has_column_right = params.get("ColumnRight") is not None
        self.column_start = _parse_float(params.get("ColumnStart"), LEGACY_DEFAULT_COLUMN_START_480)
        self.column_right = _parse_float(params.get("ColumnRight"), 19.0)
        self.column_width = [
            _stable_clamp(value, 5.0, 100.0)
            for value in _split_ints(
                params.get("ColumnWidth"),
                LEGACY_DEFAULT_COLUMN_WIDTH_480,
                key_count,
            )
        ]
        self.column_spacing = _split_ints(params.get("ColumnSpacing"), 0.0, max(0, key_count - 1))
        for index, spacing in enumerate(self.column_spacing):
            self.column_spacing[index] = _stable_clamp(
                spacing,
                -self.column_width[index + 1],
                float(np.finfo(np.float32).max),
            )
        self.column_line_width = _split_ints(
            params.get("ColumnLineWidth"), 2.0, key_count + 1,
        )
        self.column_line_width = [
            2.0 if 0.0 < value < 2.0 else value
            for value in self.column_line_width
        ]
        self.barline_height = _parse_float(params.get("BarlineHeight"), 1.2)
        self.special_style = _parse_enum(
            params.get("SpecialStyle"),
            {"none": 0, "left": 1, "right": 2},
            0,
        )
        self.combo_burst_style = _parse_enum(
            params.get("ComboBurstStyle"),
            {"left": 0, "right": 1, "both": 2},
            1,
        )
        # ComboBurstRandom is a [General] switch.  It controls the order of
        # numbered comboburst resources; the side selected by ComboBurstStyle
        # is a separate event-level choice.
        self.combo_burst_random = _parse_bool(
            params.get("ComboBurstRandom"), False,
        )
        split_stages = _parse_bool(params.get("SplitStages"), None)
        self.has_split_stages = split_stages is not None
        self.split_stages = bool(split_stages)
        self.stage_separation = _stable_clamp(
            _parse_float(params.get("StageSeparation"), 40.0),
            5.0,
            float("inf"),
        )
        self.separate_score = _parse_bool(params.get("SeparateScore"), True)
        hit_position = _parse_int(params.get("HitPosition"), LEGACY_DEFAULT_HIT_POSITION_480)
        self.hit_position = max(240.0, min(480.0, hit_position))
        self.score_position = _parse_int(params.get("ScorePosition"), LEGACY_DEFAULT_SCORE_POSITION_480)
        self.combo_position = _parse_int(params.get("ComboPosition"), LEGACY_DEFAULT_COMBO_POSITION_480)
        self.light_position = _parse_int(params.get("LightPosition"), LEGACY_DEFAULT_LIGHT_POSITION_480)
        light_fps = _parse_int(params.get("LightFramePerSecond"), LEGACY_DEFAULT_LIGHT_FPS)
        if params.get("LightFramePerSecond") is not None and light_fps <= 0:
            light_fps = LEGACY_INVALID_LIGHT_FPS
        self.light_frame_per_second = light_fps
        min_note_width = min(self.column_width) if self.column_width else 30.0
        note_height_scale = _parse_float(params.get("WidthForNoteHeightScale"), 0.0)
        self.width_for_note_height_scale_uses_column_width = not (
            note_height_scale > 0
        )
        self.width_for_note_height_scale = (
            note_height_scale if note_height_scale > 0 else min_note_width
        )
        self.keys_under_notes = _parse_bool(params.get("KeysUnderNotes"), False)
        self.judgement_line = _parse_bool(params.get("JudgementLine"), True)
        self.upside_down = _parse_bool(params.get("UpsideDown"), False)
        key_flip = _parse_bool(params.get("KeyFlipWhenUpsideDown"), True)
        note_flip = _parse_bool(params.get("NoteFlipWhenUpsideDown"), True)
        self.key_flip = []
        self.key_down_flip = []
        self.note_flip = []
        self.note_head_flip = []
        self.note_body_flip = []
        self.note_tail_flip = []
        for i in range(key_count):
            kf = _column_bool(params, "KeyFlipWhenUpsideDown", i, key_flip)
            self.key_flip.append(kf)
            self.key_down_flip.append(_column_bool(params, "KeyFlipWhenUpsideDown", f"{i}D", kf))
            nf = _column_bool(params, "NoteFlipWhenUpsideDown", i, note_flip)
            nhf = _column_bool(params, "NoteFlipWhenUpsideDown", "H", nf)
            nlf = _column_bool(params, "NoteFlipWhenUpsideDown", "L", nf)
            ntf = _column_bool(params, "NoteFlipWhenUpsideDown", "T", nf)
            self.note_flip.append(self.supports_mania_25 and nf)
            self.note_head_flip.append(
                self.supports_mania_25
                and _column_bool(params, "NoteFlipWhenUpsideDown", f"{i}H", nhf)
            )
            self.note_body_flip.append(
                self.supports_mania_25
                and _column_bool(params, "NoteFlipWhenUpsideDown", f"{i}L", nlf)
            )
            self.note_tail_flip.append(
                self.supports_mania_25
                and _column_bool(params, "NoteFlipWhenUpsideDown", f"{i}T", ntf)
            )
        default_body_style = (
            LEGACY_NOTE_BODY_STRETCH
            if self.version < 2.5
            else LEGACY_NOTE_BODY_REPEAT_BOTTOM
        )
        body_style = _parse_note_body_style(
            params.get("NoteBodyStyle"),
            default_body_style,
        )
        self.note_body_style = []
        for i in range(key_count):
            self.note_body_style.append(_parse_note_body_style(
                params.get(f"NoteBodyStyle{i}"),
                body_style,
            ))
        self.colours = [
            _parse_rgba(params.get(f"Colour{i + 1}"), (0.0, 0.0, 0.0, 1.0))
            for i in range(key_count)
        ]
        self.colour_light = [
            _parse_rgba(params.get(f"ColourLight{i + 1}"), (1.0, 1.0, 1.0, 1.0))
            for i in range(key_count)
        ]
        self.colour_column_line = _parse_rgba(params.get("ColourColumnLine"), (1.0, 1.0, 1.0, 1.0))
        self.colour_judgement_line = _parse_rgba(params.get("ColourJudgementLine"), (1.0, 1.0, 1.0, 1.0))
        self.colour_barline = _parse_rgba(params.get("ColourBarline"), (1.0, 1.0, 1.0, 1.0))
        self.colour_key_warning = _parse_rgba(params.get("ColourKeyWarning"), (0.0, 0.0, 0.0, 1.0))
        self.colour_hold = _parse_rgba(params.get("ColourHold"), (1.0, 199.0 / 255.0, 51.0 / 255.0, 1.0))
        self.colour_break = _parse_rgba(params.get("ColourBreak"), (1.0, 0.0, 0.0, 1.0))
        # 每列图片引用
        split_stages = key_count > 1 and (
            self.split_stages if self.has_split_stages else key_count >= 10
        )
        resolved_special_style = (
            self.special_style if self.beatmap_special_style else 0
        )
        self.fallback_column_index = [
            _legacy_fallback_column_index(
                i, key_count, resolved_special_style, split_stages,
            )
            for i in range(key_count)
        ]
        self.note = [
            params.get(f"NoteImage{i}") or f"mania-note{self.fallback_column_index[i]}"
            for i in range(key_count)
        ]
        self.note_head = [
            params.get(f"NoteImage{i}H") or f"mania-note{self.fallback_column_index[i]}H"
            for i in range(key_count)
        ]
        self.note_body = [
            params.get(f"NoteImage{i}L") or f"mania-note{self.fallback_column_index[i]}L"
            for i in range(key_count)
        ]
        self.note_tail = [
            params.get(f"NoteImage{i}T") or f"mania-note{self.fallback_column_index[i]}T"
            for i in range(key_count)
        ]
        self.key_image = [
            params.get(f"KeyImage{i}") or f"mania-key{self.fallback_column_index[i]}"
            for i in range(key_count)
        ]
        self.key_down_image = [
            params.get(f"KeyImage{i}D") or f"mania-key{self.fallback_column_index[i]}D"
            for i in range(key_count)
        ]
        self.lighting_n = params.get("LightingN") or "lightingN"
        self.lighting_l = params.get("LightingL") or "lightingL"
        self.lighting_n_width = _split_ints(params.get("LightingNWidth"), 0.0, key_count)
        self.lighting_l_width = _split_ints(params.get("LightingLWidth"), 0.0, key_count)
        self.hit_images = {
            0: params.get("Hit0") or "mania-hit0",
            50: params.get("Hit50") or "mania-hit50",
            100: params.get("Hit100") or "mania-hit100",
            200: params.get("Hit200") or "mania-hit200",
            300: params.get("Hit300") or "mania-hit300",
            320: params.get("Hit300g") or "mania-hit300g",
        }
        self.hit300g = self.hit_images[320]
        self.hit300 = self.hit_images[300]
        self.stage_hint = params.get("StageHint") or "mania-stage-hint"
        self.stage_light = params.get("StageLight") or "mania-stage-light"
        self.stage_left = params.get("StageLeft") or "mania-stage-left"
        self.stage_right = params.get("StageRight") or "mania-stage-right"
        self.stage_bottom = params.get("StageBottom") or "mania-stage-bottom"
        self.warning_arrow = params.get("WarningArrow") or "mania-warningarrow"
        self.scorebar_bg = "scorebar-bg"
        self.scorebar_colour = "scorebar-colour"
        self.scorebar_marker = "scorebar-marker"
        self.scorebar_ki = "scorebar-ki"
        self.scorebar_kidanger = "scorebar-kidanger"
        self.scorebar_kidanger2 = "scorebar-kidanger2"
        self.score_prefix = params.get("ScorePrefix", "score")
        self.combo_prefix = params.get("ComboPrefix", "score")
        self.score_overlap = _parse_int(params.get("ScoreOverlap"), 0)
        self.combo_overlap = _parse_int(params.get("ComboOverlap"), 0)


def load_skin_config(
    skin_dir,
    key_count,
    beatmap_special_style=False,
    beatmap_root=None,
):
    """Load one required user-selected custom skin configuration."""
    if not skin_dir:
        raise SkinConfigError("a custom skin directory is required")
    root = _skin_root(skin_dir)
    if not root:
        raise SkinConfigError(f"skin directory does not exist: {skin_dir}")
    status = {}
    params = parse_skin_ini_mania(root, key_count, _status=status)
    config = SkinConfig(
        root,
        params,
        key_count,
        beatmap_special_style,
        beatmap_root=beatmap_root,
    )
    config.skin_ini_path = status.get("ini_path")
    config.skin_mania_keys = status.get("mania_keys", ())
    config.skin_mania_config_found = bool(status.get("matched_mania_keys"))
    return config
