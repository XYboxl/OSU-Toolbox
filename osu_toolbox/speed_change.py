"""变速工具核心：按倍率同时缩放谱面时间轴与音乐。

设计约束：

* **谱面不做格式转换**。变速只是整体时间缩放，.osu/.mc/.sm 都在文本层改写
  真正表示时间的字段，其它内容（滑条几何、音效、编辑器状态、未知段落）
  原样保留。走 ``chart_convert`` 的读写器会丢掉大量信息，因此这里只借用它
  的标签扫描/转义工具函数。
* **时间与音乐同向缩放**。音频用 ``asetrate`` 变快，倍率 ``s`` 的成品只有原来
  的 ``1/s`` 长，所以谱面里每个绝对时间都除以 ``s``，单拍的 beat length 也跟着
  除以 ``s``——两者同向才能让每个音符留在原来的节拍上（见 :func:`_scale`）。
* **默认随速改变音调**。变速通过 FFmpeg 的 ``asetrate`` 重采样实现：时长与
  采样率同时改变，一次重采样即可完成，不需要耗时的相位声码器。
* **按唯一音频文件去重**。一份音频被多个谱面引用时只转码一次，多文件并行时
  由 :class:`_AudioJobRegistry` 保证同一目标不会被重复编码。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile

from .chart_convert import (
    SUPPORTED_INPUT_EXTENSIONS,
    _discover_archive,
    _extract_archive_file,
    _msd_escape,
    _msd_unescape,
    _normalise_sm_field,
    _resource_archive_name,
    _safe_float,
    _staged_output,
    _sm_notes_body,
    read_chart_text,
)

from .speed_settings import (
    format_difficulty_marker,
    format_file_marker,
    format_speed_number,
)


ARCHIVE_EXTENSIONS = (".osz", ".mcz")
CHART_EXTENSIONS = (".osu", ".mc", ".sm")
# 变速时随速改变音调的音频扩展名。其它扩展名（视频、未知容器）原样保留。
AUDIO_EXTENSIONS = (
    ".mp3", ".ogg", ".oga", ".wav", ".flac", ".m4a", ".aac", ".opus", ".wma",
    ".aiff", ".aif", ".ape", ".mp4",
)
# 右键菜单用这个值显式关闭默认的难度名标识。
SUPPRESS_DIFFICULTY_MARKER = "none"
# osu! 的故事板容器。变速工具只缩放 ``.osu`` 内嵌的 ``[Events]`` 指令；独立的
# ``.osb`` 需要整套 storyboard 命令解析，目前只检测并提示，不改写也不改名。
STORYBOARD_EXTENSIONS = (".osb",)
# 检测到独立故事板时给用户看的提示（单文件与归档共用同一措辞）。
STORYBOARD_WARNING = (
    "{name} 带有独立的故事板文件（.osb），当前版本暂不解析 .osb，"
    "该故事板不会被变速；如需变速后的故事板请手工处理或反馈需求"
)
AUDIO_FORMATS = {
    ".mp3": ("libmp3lame", "192k"),
    ".wav": ("pcm_s16le", None),
    ".flac": ("flac", None),
    ".m4a": ("aac", "192k"),
    ".aac": ("aac", "192k"),
    ".opus": ("libopus", "160k"),
    ".wma": ("wmav2", "192k"),
    ".aiff": ("pcm_s16be", None),
    ".aif": ("pcm_s16be", None),
}
DEFAULT_AUDIO_FORMAT = ("libvorbis", "192k")
# 倍率合法区间：低于 0.05 或高于 20 的倍率在节奏游戏里没有意义。
MIN_SPEED = 0.05
MAX_SPEED = 20.0
# 并发上限：每个音频转码是一个 FFmpeg 进程，过多会互相抢 CPU。
MAX_WORKERS = 8
_AUDIO_TIMEOUT_SEC = 1800.0
_CANCEL_POLL_SEC = 0.05
_FFMPEG_PROBE_TIMEOUT_SEC = 15.0

_MD_NUMBER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*)$")
# Storyboard commands written as ``letter,easing,start,end,values``.  The two
# control commands ``L`` (loop: ``start,repeatCount``) and ``T`` (trigger:
# ``name,start,end,group``) carry a shorter field list and are handled apart.
_SB_COMMANDS = frozenset(("F", "M", "MX", "MY", "S", "V", "R", "C", "P"))


class SpeedChangeError(ValueError):
    """用户可纠正的变速错误。"""


class SpeedCancelledError(RuntimeError):
    """变速任务被取消。"""


# --------------------------------------------------------------------------
# 倍率
# --------------------------------------------------------------------------

def _difficulty_marker_for(options, speed):
    """Return the difficulty-name marker, or ``""`` when it is switched off."""
    if not options.get("difficulty_marker", True):
        return ""
    pattern = options["difficulty_pattern"]
    if not str(pattern or "").strip():
        return ""
    return format_difficulty_marker(pattern, speed)


def normalized_speed(speed):
    """Validate a speed multiplier, rejecting a no-op."""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        raise SpeedChangeError("变速倍率必须是数字")
    if not math.isfinite(value) or value < MIN_SPEED or value > MAX_SPEED:
        raise SpeedChangeError(f"变速倍率必须在 {MIN_SPEED:g} 到 {MAX_SPEED:g} 之间")
    if abs(value - 1.0) < 1e-9:
        raise SpeedChangeError("变速倍率不能是 1.0")
    return value


def _scale(value, speed):
    """Scale one absolute millisecond value onto the speed-changed timeline.

    The music is resampled to ``1 / speed`` of its length, so every timestamp a
    chart carries shrinks by that same factor: a note that sat 2000 ms into the
    original track sits 1333 ms into the faster one.  A beat length shrinks with
    it, which is what keeps each note on the beat it was authored on -- scaling
    times and beat lengths in opposite directions moved a note from beat 0.5 to
    beat 1.125, so a speed-changed chart could never line up with its music.
    """
    return _safe_float(value) / speed


def _number(value):
    """Render a scaled millisecond value without trailing float noise."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _milliseconds(value, speed):
    """Scale one absolute time and render it as whole milliseconds.

    Every absolute time a chart carries is a millisecond integer, and readers
    parse it as one.  Writing the exact quotient instead (``667.3333``) made the
    toolbox's own readers reject the whole chart -- they could no longer load a
    single speed-changed file -- and no other reader expects a fractional
    millisecond either.  Rounding costs at most half a millisecond, which is the
    same tolerance any converted chart already carries.
    """
    return str(int(round(_scale(value, speed))))


# --------------------------------------------------------------------------
# 谱面时间轴缩放（文本层，保留全部原始内容）
# --------------------------------------------------------------------------

def _split_lines(text):
    """Split a chart into lines, reporting its own line ending and final break."""
    ending = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    if trailing and text.endswith(ending):
        # Drop the final break here so ``_join_lines`` can restore it exactly
        # once; leaving it in the last element added a second, empty line.
        text = text[: -len(ending)]
    return text.split(ending), ending, trailing


def _join_lines(lines, ending, trailing):
    text = ending.join(lines)
    return text + ending if trailing else text


def _read_chart_text(path):
    """Read a chart, returning ``(text, had_utf8_bom)``.

    Delegates to the shared decoder so ``.osu``, ``.mc`` and ``.sm`` all honour
    a byte-order mark: a UTF-16 chart used to decode into NUL-riddled mojibake
    that no parser could read, which made the speed change fail without ever
    writing an output file.
    """
    return read_chart_text(path)


def _write_chart_text(path, text, bom, allow_overwrite=True):
    data = text.encode("utf-8")
    with _staged_output(path, allow_overwrite) as staged:
        staged.write_bytes(b"\xef\xbb\xbf" + data if bom else data)


def _scale_leading_time(value, speed):
    """Scale the leading time of a colon-joined field, keeping its tail.

    osu! packs several values into one field (``4000:0:0:0:0:`` is a mania hold's
    tail time followed by its hit sample).  Only the first token is a timestamp,
    so the rest must be handed back byte-for-byte.
    """
    head, separator, tail = value.partition(":")
    return f"{_milliseconds(head, speed)}{separator}{tail}"


def _scale_osu_hit_object(line, speed):
    """Scale one hit object's timestamps, leaving everything else alone.

    A hit object's only timestamps are its own start time and the leading token
    of the fifth field -- a mania hold's tail or a spinner's end time.  What
    follows a slider is its curve, its repeat count, its pixel length and its
    edge sound data: none of those is a time, and the game derives every slider
    edge's time from the curve and the (already scaled) beat length, so the whole
    tail is handed back byte for byte.
    """
    parts = line.split(",")
    if len(parts) < 5:
        return line
    try:
        object_type = int(float(parts[3]))
    except (TypeError, ValueError):
        return line
    parts[2] = _milliseconds(parts[2], speed)
    if object_type & 128 and len(parts) > 5:
        # Mania hold: the tail time is the leading token of the fifth field.
        parts[5] = _scale_leading_time(parts[5], speed)
    elif object_type & 8 and len(parts) > 5:
        # Spinner end time.
        parts[5] = _scale_leading_time(parts[5], speed)
    return ",".join(parts)


def _scale_osu_timing_point(line, speed):
    parts = line.split(",")
    if len(parts) < 2:
        return line
    beat_length = _safe_float(parts[1], math.nan)
    if not math.isfinite(beat_length) or beat_length == 0.0:
        return line
    parts[0] = _milliseconds(parts[0], speed)
    if beat_length > 0:
        # Only uninherited (red) points carry a real beat length; a negative
        # value is a slider-velocity multiplier, already relative to the
        # preceding red line, so it must stay untouched.  A beat shortens with
        # the timeline, so the grid stays under the notes it was written for.
        # The beat length itself stays fractional: it is a duration in
        # milliseconds per beat, not a timestamp.
        parts[1] = _number(_scale(beat_length, speed))
    while len(parts) < 8:
        parts.append("0")
    return ",".join(parts[:8])


def _scale_event_field(field, speed):
    """Scale one event field's time, keeping the field's own padding."""
    text = field.strip()
    if not text:
        return field
    leading = field[: len(field) - len(field.lstrip())]
    trailing = field[len(field.rstrip()):]
    return f"{leading}{_milliseconds(text, speed)}{trailing}"


def _scale_osu_event_line(line, speed):
    """Scale the absolute times one ``[Events]`` line carries.

    Only the fields that really hold a time are rewritten: a break event's two
    timestamps, a background video's or storyboard sample's start time, and a
    storyboard command's start and end.  Everything else -- a video's file name,
    a command's easing index, a loop's repeat count, a trigger's name -- is left
    alone, and so is every field's spacing.  The leading spaces of a command are
    part of its meaning: they nest it inside the sprite, loop or trigger above
    it, so a rewritten line keeps them.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("//"):
        return line
    # Split without stripping so each field keeps its own padding.
    parts = line.split(",")
    head = parts[0].strip()
    if head in ("0", "Background"):
        return line
    if head in ("1", "Video"):
        # ``1,start,"file"``: one start time, and then the video's own file name.
        indexes = (1,)
    elif head == "Sample":
        # ``Sample,start,layer,"file",volume``: one start time.
        indexes = (1,)
    elif head in ("2", "Break"):
        # A break carries a start and an end.
        indexes = (1, 2)
    elif head in _SB_COMMANDS:
        # ``letter,easing,start,end,...``: the easing index is not a time.
        indexes = (2, 3)
    elif head == "L":
        # ``L,start,repeatCount``: the repeat count is not a time.
        indexes = (1,)
    elif head == "T":
        # ``T,name,start,end,group``: name and group are not times.
        indexes = (2, 3)
    else:
        return line
    for index in indexes:
        if len(parts) > index:
            parts[index] = _scale_event_field(parts[index], speed)
    return ",".join(parts)


def _append_osu_field_value(lines, key, suffix):
    """Append text to a metadata field's value, keeping its original spacing.

    osu! beatmaps in the wild are not consistently written as ``Key: Value``;
    rewriting the whole line would normalise files that the transform is
    supposed to leave alone.
    """
    for index, line in enumerate(lines):
        match = _MD_NUMBER_LINE.match(line.strip())
        if not match or match.group(1) != key:
            continue
        lines[index] = re.sub(
            rf"^(\s*{re.escape(key)}\s*:\s*)(.*)$",
            lambda found: f"{found.group(1)}{found.group(2)}{suffix}",
            line,
        )
        return True
    return False


def transform_osu_text(text, speed, *, difficulty_marker="", song_marker=None):
    """Scale an ``.osu`` chart's timeline, returning ``(text, info)``.

    Every absolute time a chart carries -- timing points, hit objects, break and
    storyboard events, ``AudioLeadIn``, ``PreviewTime`` -- shrinks by ``speed``,
    matching the music, which is resampled to ``1 / speed`` of its length; beat
    lengths shrink with them, so each note keeps the beat it was authored on.
    Nothing else moves: slider curves, repeat counts and pixel lengths are
    geometry, not time.

    ``difficulty_marker`` lands in ``Version``, the difficulty name.
    ``song_marker`` lands in ``Title`` and ``TitleUnicode``, the song name, so the
    marked file's song name says it was speed-changed too; ``None`` means "use the
    difficulty marker" and ``""`` leaves the song name as authored.
    """
    if song_marker is None:
        song_marker = difficulty_marker
    lines, ending, trailing = _split_lines(text)
    sections = {}
    current = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(index)
    info = {"audio_filename": "", "version": ""}
    for index in sections.get("TimingPoints", ()):
        if lines[index].strip():
            lines[index] = _scale_osu_timing_point(lines[index], speed)
    for index in sections.get("HitObjects", ()):
        if lines[index].strip():
            lines[index] = _scale_osu_hit_object(lines[index], speed)
    for index in sections.get("Events", ()):
        lines[index] = _scale_osu_event_line(lines[index], speed)
    for index in sections.get("General", ()):
        match = _MD_NUMBER_LINE.match(lines[index].strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key == "AudioFilename":
            info["audio_filename"] = value
            continue
        if key == "AudioLeadIn":
            # osu!'s pre-roll silence is real playback time, so it shrinks with
            # the music; the chart's own timestamps stay audio-relative.
            lines[index] = re.sub(
                rf"^(\s*{key}\s*:\s*).*$",
                lambda found: f"{found.group(1)}{_milliseconds(value, speed)}",
                lines[index],
            )
            continue
        if key == "PreviewTime":
            preview = _safe_float(value, math.nan)
            # -1 means "no preview point"; scaling it would turn it into -2.
            if math.isfinite(preview) and preview >= 0:
                scaled_preview = _milliseconds(preview, speed)
                lines[index] = re.sub(
                    rf"^(\s*{key}\s*:\s*).*$",
                    lambda found: f"{found.group(1)}{scaled_preview}",
                    lines[index],
                )
    for index in sections.get("Metadata", ()):
        match = _MD_NUMBER_LINE.match(lines[index].strip())
        if not match:
            continue
        key = match.group(1)
        if key == "Version":
            info["version"] = match.group(2).strip()
        elif key in ("BeatmapID", "BeatmapSetID"):
            # A speed-changed copy must not identify itself as the online original.
            value = "0" if key == "BeatmapID" else "-1"
            lines[index] = re.sub(
                rf"^(\s*{key}\s*:\s*).*$",
                lambda found: f"{found.group(1)}{value}",
                lines[index],
            )
    if difficulty_marker and info["version"]:
        _append_osu_field_value(lines, "Version", difficulty_marker)
    for key in ("Title", "TitleUnicode"):
        if song_marker:
            _append_osu_field_value(lines, key, song_marker)
    info["version"] = f"{info['version']}{difficulty_marker}"
    return _join_lines(lines, ending, trailing), info


def _mc_scale_offset(value, speed):
    """Scale a Malody audio offset, which the format stores as an integer."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        if not value.strip():
            return value
        try:
            return str(int(round(float(value) / speed)))
        except ValueError:
            return value
    try:
        return int(round(float(value) / speed))
    except (TypeError, ValueError):
        return value


def _mc_scale_time(value, speed):
    """Scale a Malody millisecond note time.

    A ``time``/``end_time`` is the note's position in the track in whole
    milliseconds, so it shrinks with the music exactly like the audio offset
    does and is written back as an integer; a beat tuple is already
    tempo-relative and must not move.
    """
    if isinstance(value, (list, tuple)):
        return value
    return int(round(_scale(value, speed)))


def _mc_audio_reference(root, meta, song):
    for container in (song, meta, root):
        if not isinstance(container, dict):
            continue
        for key in ("file", "music", "audio", "sound", "audio_file", "audioFilename"):
            candidate = container.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    notes = root.get("note") or root.get("notes")
    if isinstance(notes, list):
        for item in notes:
            if not isinstance(item, dict):
                continue
            for key in ("sound", "music", "audio"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return ""


def transform_mc_text(text, speed, *, difficulty_marker="", song_marker=None):
    """Scale a Malody ``.mc`` chart, returning ``(text, info)``.

    ``version`` is the difficulty name and takes ``difficulty_marker``;
    ``meta.song.title`` is the song name and takes ``song_marker`` (``None`` means
    "use the difficulty marker").  ``titleorg`` is the untouched original the
    format keeps for reference, so it is left alone.
    """
    if song_marker is None:
        song_marker = difficulty_marker
    try:
        root = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise SpeedChangeError(
            f"无法解析 .mc 谱面：{exc}；该文件可能不是有效的 Malody 谱面，"
            "或其编码/语法不受支持"
        )
    if not isinstance(root, dict):
        raise SpeedChangeError(".mc 谱面根节点必须是对象")
    meta = root.get("meta") if isinstance(root.get("meta"), dict) else {}
    song = meta.get("song") if isinstance(meta.get("song"), dict) else {}
    reference = _mc_audio_reference(root, meta, song)
    version = str(meta.get("version") or meta.get("difficulty") or "")
    for key in ("time", "timing"):
        entries = root.get(key)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            bpm = _safe_float(item.get("bpm"), 0.0)
            if bpm > 0:
                # Beats stay put; a higher BPM makes the same beats play faster,
                # which is exactly how Malody expresses tempo.
                item["bpm"] = bpm * speed
    notes = root.get("note") or root.get("notes")
    if isinstance(notes, list):
        for item in notes:
            if not isinstance(item, dict):
                continue
            if "time" in item:
                item["time"] = _mc_scale_time(item["time"], speed)
            if "end_time" in item:
                item["end_time"] = _mc_scale_time(item["end_time"], speed)
            if "offset" in item:
                item["offset"] = _mc_scale_offset(item["offset"], speed)
    for container in (meta, song):
        if not isinstance(container, dict):
            continue
        for key in ("audio_offset", "audioOffset", "audio_delay", "audioDelay", "offset"):
            if key in container:
                container[key] = _mc_scale_offset(container[key], speed)
    if difficulty_marker and isinstance(meta, dict):
        if version:
            meta["version"] = f"{version}{difficulty_marker}"
        version = f"{version}{difficulty_marker}"
    song_title = ""
    if song_marker and isinstance(song, dict) and song.get("title"):
        song_title = str(song["title"])
        song["title"] = f"{song_title}{song_marker}"
    return json.dumps(root, ensure_ascii=False, indent=2) + "\n", {
        "audio_filename": reference,
        "version": version,
        "song_title": song_title,
    }


def _sm_replace_tag(text, tag, transform):
    """Rewrite one MSD tag's fields, leaving every other byte untouched."""
    pattern = re.compile(r"#" + re.escape(tag) + r"\s*:", re.IGNORECASE)
    position = 0
    result = []
    while True:
        match = pattern.search(text, position)
        if match is None:
            result.append(text[position:])
            break
        end = text.find(";", match.end())
        if end == -1:
            result.append(text[position:])
            break
        result.append(text[position:match.end()])
        result.append(transform(text[match.end():end]))
        position = end
    return "".join(result)


def _sm_tag_text(text, tag):
    pattern = re.compile(r"#" + re.escape(tag) + r"\s*:", re.IGNORECASE)
    match = pattern.search(text)
    if match is None:
        return ""
    end = text.find(";", match.end())
    if end == -1:
        return ""
    return _msd_unescape(text[match.end():end]).strip()


def _scale_sm_bpms(value, speed):
    items = []
    for item in str(value or "").split(","):
        if "=" not in item:
            continue
        beat, bpm = item.split("=", 1)
        number = _safe_float(bpm, math.nan)
        if not math.isfinite(number):
            continue
        items.append(f"{beat.strip()}={number * speed:.6f}")
    return ",".join(items)


def _scale_sm_event_duration(value, speed):
    items = []
    for item in str(value or "").split(","):
        if "=" not in item:
            continue
        beat, duration = item.split("=", 1)
        number = _safe_float(duration, math.nan)
        if not math.isfinite(number):
            continue
        items.append(f"{beat.strip()}={number / speed:.6f}")
    return ",".join(items)


def _scale_sm_offset(value, speed):
    number = _safe_float(value, math.nan)
    if not math.isfinite(number):
        return str(value)
    return f"{number / speed:.6f}"


# A ``#NOTES`` header has six colon-terminated fields.  The tag's own colon
# opens the first one (the steps type), then ``description``, ``difficulty``,
# ``meter`` and ``radar values`` each carry one more, so only five separators
# remain inside the body before the note data starts.
_SM_NOTES_FIELD_SEPARATORS = 5


def _split_msd_notes(body):
    """Split a ``#NOTES`` body into ``(fields, spans, note_data)``.

    ``body`` is the text between ``#NOTES:`` and the closing ``;``.  The note
    data itself may contain escaped colons, so the split stops after the radar
    values field.  ``spans`` gives each field's ``(start, end)`` inside the
    body so a caller can rewrite one field without touching the file's own
    spacing.  A header with an unexpected shape is reported as ``None`` rather
    than guessed at.
    """
    fields = []
    spans = []
    cursor = 0
    for _ in range(_SM_NOTES_FIELD_SEPARATORS):
        index = body.find(":", cursor)
        if index == -1:
            return None, None, ""
        fields.append(body[cursor:index])
        spans.append((cursor, index))
        cursor = index + 1
    return fields, spans, body[cursor:]


def _sm_mark_notes_header(body, marker):
    """Append the marker to one ``#NOTES`` chart's description field."""
    fields, spans, note_data = _split_msd_notes(body)
    if fields is None:
        return body
    description = _msd_unescape(fields[1])
    if not description.strip():
        # A blank description is what a bare ``#NOTES::`` header looks like;
        # there is nothing to mark.
        return body
    # Keep the field's own padding: a canonical ``.sm`` puts a newline and
    # indentation before each field, and rewriting the whole field would
    # flatten the file.
    padding = description[: len(description) - len(description.lstrip())]
    text = description.strip()
    marked = _msd_escape(_normalise_sm_field(f"{text}{marker}", notes_field=True))
    start, end = spans[1]
    return f"{body[:start]}{padding}{marked}{body[end:]}"


def _sm_mark_difficulties(text, marker):
    pattern = re.compile(r"#NOTES\s*:", re.IGNORECASE)
    position = 0
    result = []
    while True:
        match = pattern.search(text, position)
        if match is None:
            result.append(text[position:])
            break
        end = text.find(";", match.end())
        if end == -1:
            result.append(text[position:])
            break
        result.append(text[position:match.end()])
        result.append(_sm_mark_notes_header(text[match.end():end], marker))
        position = end
    return "".join(result)


def _sm_first_notes_description(text):
    """Return the first ``#NOTES`` description, which names the difficulty."""
    pattern = re.compile(r"#NOTES\s*:", re.IGNORECASE)
    match = pattern.search(text)
    if match is None:
        return ""
    end = text.find(";", match.end())
    body = text[match.end():end if end != -1 else len(text)]
    fields, _spans, _note_data = _split_msd_notes(body)
    if fields is None:
        return ""
    return _msd_unescape(fields[1]).strip()


def transform_sm_text(text, speed, *, difficulty_marker="", song_marker=None):
    """Scale a StepMania ``.sm`` chart, returning ``(text, info)``.

    ``#NOTES`` descriptions are the difficulty names and take
    ``difficulty_marker``; ``#TITLE`` is the song name and takes ``song_marker``
    (``None`` means "use the difficulty marker").  A ``.sm`` file lists several
    difficulties against one audio, so the two fields answer different questions
    and are marked separately.
    """
    if song_marker is None:
        song_marker = difficulty_marker
    reference = _sm_tag_text(text, "MUSIC")
    version = _sm_first_notes_description(text)
    result = _sm_replace_tag(text, "BPMS", lambda value: _scale_sm_bpms(value, speed))
    result = _sm_replace_tag(result, "OFFSET", lambda value: _scale_sm_offset(value, speed))
    for tag in ("STOPS", "FREEZES", "DELAYS"):
        result = _sm_replace_tag(
            result, tag, lambda value: _scale_sm_event_duration(value, speed)
        )
    if difficulty_marker:
        result = _sm_mark_difficulties(result, difficulty_marker)
        version = f"{version}{difficulty_marker}"
    if song_marker:
        result = _sm_replace_tag(
            result, "TITLE", lambda value: f"{value}{song_marker}"
        )
    return result, {"audio_filename": reference, "version": version}


def transform_chart_text(
    text, extension, speed, *, difficulty_marker="", song_marker=None
):
    """Dispatch to the format-specific timeline scaler.

    The two markers answer different fields: ``difficulty_marker`` marks the
    difficulty name (``.osu`` ``Version``, ``.mc`` ``version``, ``.sm`` ``#NOTES``
    description) and ``song_marker`` marks the song name (``.osu`` ``Title`` /
    ``TitleUnicode``, ``.mc`` ``meta.song.title``, ``.sm`` ``#TITLE``).  Passing
    ``None`` marks the song name with the same text as the difficulty, which is
    what a run normally wants: both carry the speed suffix.
    """
    if song_marker is None:
        song_marker = difficulty_marker
    extension = str(extension).lower()
    if extension == ".osu":
        return transform_osu_text(
            text, speed, difficulty_marker=difficulty_marker, song_marker=song_marker
        )
    if extension == ".mc":
        return transform_mc_text(
            text, speed, difficulty_marker=difficulty_marker, song_marker=song_marker
        )
    if extension == ".sm":
        return transform_sm_text(
            text, speed, difficulty_marker=difficulty_marker, song_marker=song_marker
        )
    raise SpeedChangeError(f"不支持的谱面格式：{extension}")


def _rewrite_audio_reference(text, extension, old_reference, new_reference):
    """Point a chart at renamed audio without touching anything else."""
    if not old_reference or old_reference == new_reference:
        return text
    if extension == ".osu":
        lines, ending, trailing = _split_lines(text)
        for index, line in enumerate(lines):
            match = _MD_NUMBER_LINE.match(line.strip())
            if not match or match.group(1) != "AudioFilename":
                continue
            lines[index] = re.sub(
                r"^(\s*AudioFilename\s*:\s*).*$",
                lambda found: f"{found.group(1)}{new_reference}",
                line,
            )
            return _join_lines(lines, ending, trailing)
        return text
    if extension == ".mc":
        try:
            root = json.loads(text)
        except (TypeError, ValueError):
            return text
        _replace_mc_audio_paths(root, old_reference, new_reference)
        return json.dumps(root, ensure_ascii=False, indent=2) + "\n"
    if extension == ".sm":
        return _sm_replace_tag(
            text,
            "MUSIC",
            lambda _value: _msd_escape(_normalise_sm_field(new_reference)),
        )
    return text


def _replace_mc_audio_paths(node, old_reference, new_reference):
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key in ("file", "music", "audio", "sound", "audio_file", "audioFilename")
                and isinstance(value, str)
                and value.strip() == str(old_reference).strip()
            ):
                node[key] = new_reference
            else:
                _replace_mc_audio_paths(value, old_reference, new_reference)
    elif isinstance(node, list):
        for item in node:
            _replace_mc_audio_paths(item, old_reference, new_reference)


# --------------------------------------------------------------------------
# 音频变速（默认随速改变音调）
# --------------------------------------------------------------------------

def find_ffmpeg():
    """Locate FFmpeg without importing the video stack."""
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "ffmpeg.exe"
    if local.is_file():
        return str(local)
    try:
        from .video.recorder import VideoRecorder
    except Exception:
        return "ffmpeg"
    return VideoRecorder._find_ffmpeg()


def _audio_encoder(extension):
    return AUDIO_FORMATS.get(str(extension).lower(), DEFAULT_AUDIO_FORMAT)


_STREAM_RATE = re.compile(r"Audio:.*?(\d+)\s*Hz")
_STREAM_BITRATE = re.compile(r"Audio:.*?(\d+)\s*kb/s")


def probe_audio(ffmpeg, path):
    """Return ``(sample_rate, bitrate)`` for a file's first audio stream.

    The bundled FFmpeg has no ``ffprobe`` beside it, so the stream header that
    ``ffmpeg -i`` prints to stderr is parsed instead.  ``sample_rate`` has to be
    a concrete number: this build evaluates ``asetrate``'s expression with a
    variable table that does not know ``sample_rate``.

    The stderr is decoded as UTF-8 with replacement instead of text mode: FFmpeg
    echoes the file name, and metadata in a non-GBK encoding made the default
    Windows codec raise ``UnicodeDecodeError``, which failed the whole change.
    """
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=_FFMPEG_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    text = (completed.stderr or b"").decode("utf-8", errors="replace")
    rate = _STREAM_RATE.search(text)
    bitrate = _STREAM_BITRATE.search(text)
    return (
        int(rate.group(1)) if rate else None,
        int(bitrate.group(1)) * 1000 if bitrate else None,
    )


def _audio_attempts(
    ffmpeg,
    source,
    destination,
    speed,
    extension,
    sample_rate,
    bitrate=None,
    keep_pitch=False,
):
    """Yield the FFmpeg commands to try, in order, for one audio speed change.

    ``asetrate`` reinterprets the samples at ``rate * speed``, which makes
    playback both faster and higher while leaving the *stream* at the shifted
    rate.  Not every encoder accepts an arbitrary rate: ``libvorbis`` refused
    66150 Hz outright for a stereo Vorbis source, so a second attempt pins the
    output back to the source's own rate (``-ar``) -- the pitch shift is already
    baked into the samples, so that only costs a resample.

    ``keep_pitch`` skips ``asetrate`` entirely and uses ``atempo``, which keeps
    the stream rate untouched; there is nothing to retry in that case.
    """
    codec, default_bitrate = _audio_encoder(extension)
    base = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source), "-vn"]
    tail = ["-c:a", codec]
    effective = bitrate or default_bitrate
    if effective is not None:
        tail.extend(("-b:a", effective))

    if keep_pitch:
        # ``atempo`` is the pitch-preserving time stretcher, run on its own: it
        # accepts 0.5..2.0 per instance, so extreme multipliers are split.
        stages = [f"atempo={factor:.10g}" for factor in _atempo_factors(speed)]
        stages.append("aresample=async=1")
        yield base + ["-filter:a", ",".join(stages)] + tail + [str(destination)]
        return

    stages = f"asetrate={sample_rate}*{speed:.10g},aresample=async=1"
    yield base + ["-filter:a", stages] + tail + [str(destination)]
    if sample_rate:
        yield (
            base
            + ["-filter:a", stages, "-ar", str(sample_rate)]
            + tail
            + [str(destination)]
        )


def _build_audio_command(
    ffmpeg,
    source,
    destination,
    speed,
    extension,
    sample_rate,
    bitrate=None,
    keep_pitch=False,
):
    """Return the first FFmpeg command for one audio speed change."""
    return next(
        _audio_attempts(
            ffmpeg, source, destination, speed, extension, sample_rate, bitrate, keep_pitch
        )
    )


# ``atempo`` accepts at most 2.0 and at least 0.5 per instance.
_ATEMPO_MAX = 2.0
_ATEMPO_MIN = 0.5


def _atempo_factors(value):
    """Split one tempo factor into a chain of factors ``atempo`` accepts."""
    factors = []
    remaining = float(value)
    while remaining > _ATEMPO_MAX:
        factors.append(_ATEMPO_MAX)
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        factors.append(_ATEMPO_MIN)
        remaining /= _ATEMPO_MIN
    factors.append(remaining)
    return factors


def _terminate(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except Exception:
        pass


def _run_process(command, cancel_event, timeout):
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                _terminate(process)
                raise SpeedCancelledError("变速任务已取消")
            if time.monotonic() >= deadline:
                _terminate(process)
                raise SpeedChangeError("音频转码超时")
            time.sleep(_CANCEL_POLL_SEC)
        return process.returncode
    except BaseException:
        _terminate(process)
        raise


def resample_audio(
    source, destination, speed, *, cancel_event=None, ffmpeg=None, keep_pitch=False
):
    """Publish audio only after encoding completes successfully."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise SpeedChangeError("变速输出不能覆盖原音乐")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _staged_output(destination, True) as staged:
        _resample_audio_file(
            source, staged, speed, cancel_event=cancel_event,
            ffmpeg=ffmpeg, keep_pitch=keep_pitch,
        )
        _check_cancelled(cancel_event)
    return destination


def _resample_audio_file(
    source, destination, speed, *, cancel_event=None, ffmpeg=None, keep_pitch=False
):
    """Speed up one audio file, optionally keeping the original pitch.

    Every command variant is tried in order; a partial output is always removed
    before the call returns, so a failed conversion cannot leave a 0-byte file
    behind that looks like a finished result.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise SpeedChangeError(f"音频文件不存在：{source}")
    executable = ffmpeg or find_ffmpeg()
    extension = destination.suffix.lower()
    sample_rate, probed = probe_audio(executable, source)
    if not sample_rate:
        raise SpeedChangeError(
            f"无法读取音频采样率：{source.name}；该文件可能不是可解码的音频"
        )
    bitrate = None
    if probed:
        # Preserve the source's own bitrate so a low-bitrate map does not grow
        # and a high-bitrate one does not lose quality needlessly.  A lossless
        # source's rate is capped because its "bitrate" is a size, not quality.
        _codec, default_bitrate = _audio_encoder(extension)
        if default_bitrate is not None:
            bitrate = f"{max(64, min(320, probed // 1000))}k"
    attempts = list(
        _audio_attempts(
            executable,
            source,
            destination,
            speed,
            extension,
            sample_rate,
            bitrate,
            keep_pitch=keep_pitch,
        )
    )
    returncode = None
    try:
        for command in attempts:
            returncode = _run_process(command, cancel_event, _AUDIO_TIMEOUT_SEC)
            if returncode == 0 and destination.is_file() and destination.stat().st_size > 0:
                return destination
            # A failed encoder can still create an empty file; clear it before
            # the next attempt so a stale artifact is never mistaken for output.
            _remove_partial(destination)
    except BaseException:
        _remove_partial(destination)
        raise
    _remove_partial(destination)
    raise SpeedChangeError(
        f"音频转码失败（ffmpeg 退出码 {returncode}）：{source.name}；"
        f"已尝试 {len(attempts)} 种编码参数，该音频的格式或采样率可能不受支持"
    )


def _remove_partial(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _place_audio(staged, target):
    """Move a transcoded track from its temporary name onto its final name."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, target)


class _AudioJobRegistry:
    """Transcode each source once, even across parallel workers."""

    def __init__(self, ffmpeg, speed, cancel_event=None, keep_pitch=False, allow_overwrite=True):
        self._ffmpeg = ffmpeg
        self._speed = speed
        self._cancel_event = cancel_event
        self._keep_pitch = bool(keep_pitch)
        self._allow_overwrite = bool(allow_overwrite)
        self._condition = threading.Condition()
        self._states = {}
        self._errors = {}

    @staticmethod
    def _key(source, destination):
        return f"{Path(source).resolve()}\0{Path(destination).resolve()}".casefold()

    def ensure(self, source, destination):
        key = self._key(source, destination)
        with self._condition:
            while key in self._states:
                state = self._states[key]
                if state == "done":
                    return
                if state == "failed":
                    raise self._errors[key]
                self._condition.wait(0.2)
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise SpeedCancelledError("变速任务已取消")
            self._states[key] = "running"
        try:
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with _staged_output(destination, self._allow_overwrite) as staged:
                resample_audio(
                    source, staged, self._speed,
                    cancel_event=self._cancel_event,
                    ffmpeg=self._ffmpeg,
                    keep_pitch=self._keep_pitch,
                )
                _check_cancelled(self._cancel_event)
        except BaseException as exc:
            with self._condition:
                self._states[key] = "failed"
                self._errors[key] = exc
                self._condition.notify_all()
            raise
        with self._condition:
            self._states[key] = "done"
            self._condition.notify_all()


# --------------------------------------------------------------------------
# 输出规划
# --------------------------------------------------------------------------

def is_archive(path):
    return Path(path).suffix.lower() in ARCHIVE_EXTENSIONS


def is_chart(path):
    return Path(path).suffix.lower() in CHART_EXTENSIONS


def is_audio(path):
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def is_storyboard(path):
    return Path(path).suffix.lower() in STORYBOARD_EXTENSIONS


def companion_storyboards(chart_path):
    """Return the ``.osb`` storyboards that belong to a chart.

    osu! loads a chart's standalone storyboard from a sibling ``.osb`` whose
    name matches the ``.osu`` file, so only that file is reported: an unrelated
    ``.osb`` next to the chart is not part of this package.  The directory is
    listed rather than probing ``song.osb``/``song.OSB`` directly, because a
    case-insensitive filesystem would report the same file twice under both
    spellings.
    """
    chart = Path(chart_path)
    try:
        siblings = list(chart.parent.iterdir())
    except OSError:
        return []
    return sorted(
        item
        for item in siblings
        if item.is_file()
        and is_storyboard(item)
        and item.stem.casefold() == chart.stem.casefold()
    )


def _archive_storyboard_members(chart_stems, members):
    """Map each chart stem inside an archive to its ``.osb`` member, if any."""
    storyboards = {
        name: name
        for name in members
        if name and not name.endswith("/") and is_storyboard(name)
    }
    if not storyboards:
        return {}
    found = {}
    for stem in chart_stems:
        directory, _sep, _name = str(stem).replace("\\", "/").rpartition("/")
        for name in storyboards:
            member_directory, _member_sep, member_name = name.rpartition("/")
            if member_directory != directory:
                continue
            if Path(member_name).stem.casefold() != Path(stem).name.casefold():
                continue
            found[stem] = name
            break
    return found


def _archive_member_name(path, marker):
    """Rename an archive member: ``song.osu`` -> ``song 1.5x.osu``."""
    text = str(path).replace("\\", "/")
    directory, _sep, name = text.rpartition("/")
    stem, extension = os.path.splitext(name)
    renamed = f"{stem}{marker}{extension}"
    return f"{directory}/{renamed}" if directory else renamed


def _archive_output_members(root, original, entries, audio, file_marker):
    """Reserve distinct chart/music names while leaving other members intact."""
    root = Path(root).resolve()
    original_paths = {
        (root / name.replace("\\", "/")).resolve(): name
        for name in original if name and not name.endswith("/")
    }
    taken = {
        _output_key(path, root) for path in original_paths
        if file_marker in path.stem
    }
    reserve = _unique_path_for(taken, root, file_marker)
    outputs = {}
    for path in [*sorted(audio), *(chart for chart, _scaled, _reference in entries)]:
        target = reserve(path)
        outputs[path] = re.sub(
            r"[^/\\]+$", lambda _match: target.name, original_paths[path]
        )
    return outputs


def _marked_file_stem(name, marker, existing=()):
    """Append a marker once, protecting names already present in the source."""
    stem = Path(name).stem
    if marker and stem.endswith(marker):
        return stem
    if marker and f"{stem}{marker}{Path(name).suffix}".casefold() in {
        value.casefold() for value in existing
    }:
        return f"{stem} 2{marker}"
    return f"{stem}{marker}"


def _countered(stem, index, marker):
    """Insert a disambiguating counter before an already-present marker."""
    if marker and stem.endswith(marker):
        return f"{stem[: -len(marker)]} {index}{marker}"
    return f"{stem} {index}"


def _marked_names_in(directory, marker):
    """Return the casefolded names in ``directory`` that already carry ``marker``.

    Running 1.1x twice leaves ``0.mc`` beside ``0 1.1x.mc``.  Both are legitimate
    files, so neither is dropped: the already-marked one keeps its name, and the
    unmarked one takes a counter (``0 2 1.1x.mc``) instead of overwriting it.
    """
    if not marker:
        return set()
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return set()
    return {
        entry.name.casefold() for entry in entries
        if entry.is_file() and marker in entry.stem
    }


def _unique_path_for(taken, destination_root, marker=""):
    """Return a function that marks one output path and claims it.

    Callers pass the target in its *source* shape (``Hard.osu``); the returned
    function applies the marker itself, so the marker is applied in exactly one
    place and a source name that already carries it is never marked twice.

    Names are compared relative to the song's result folder (``0/chart 1.5x.mc``),
    because two subfolders legitimately produce the same file name and only the
    whole path tells them apart.  A name already taken gets a counter before the
    marker (``Hard 2 1.5x.osu``), which can never be read as another speed.
    """
    def unique(target):
        target = Path(target)
        source_stem = target.stem
        candidate = target.with_name(
            f"{_marked_file_stem(target.name, marker)}{target.suffix}"
        )
        key = _output_key(candidate, destination_root)
        if key not in taken:
            taken.add(key)
            return candidate
        # The counter belongs to the source stem -- the level just above the
        # marker -- so the result reads ``Hard 2 1.5x.osu`` and never
        # ``Hard 1.5x 2 1.5x.osu``.
        if marker and source_stem.endswith(marker):
            source_stem = source_stem[: -len(marker)]
        index = 2
        while True:
            stem = f"{_countered(source_stem, index, marker)}{marker}"
            candidate = target.with_name(f"{stem}{target.suffix}")
            if _output_key(candidate, destination_root) not in taken:
                taken.add(_output_key(candidate, destination_root))
                return candidate
            index += 1

    return unique


def _output_key(target, destination_root):
    """Return the song-relative key one output path is compared by."""
    target = Path(target)
    try:
        return str(target.relative_to(destination_root)).casefold()
    except ValueError:
        return target.name.casefold()


def _reference_stop(chart_path, reference):
    """Return the folder a chart's own resource references may search up to.

    osu! keeps a chart and its music in one folder, so the chart's own folder is
    the boundary.  Malody stores a chart set in a numbered storage level
    (``Song/0``) while the track it names sits in ``Song`` above it, and a
    reference of the form ``../audio.mp3`` can only ever mean that: without
    letting it reach one level up, a single ``.mc`` was rewritten while its music
    was left at the old tempo, which desynced the result instead of failing.
    """
    folder = Path(chart_path).resolve().parent
    text = str(reference or "").strip().strip('"').replace("\\", "/")
    if not text.startswith("../"):
        return folder
    while _is_storage_level(folder) and folder.parent != folder:
        folder = folder.parent
    return folder


def _resolve_reference(chart_path, reference, stop):
    """Resolve a chart resource reference, searching upwards like osu! does."""
    text = str(reference or "").strip().strip('"')
    if not text:
        return None
    relative = Path(text.replace("\\", "/"))
    if relative.is_absolute():
        return None
    current = Path(chart_path).resolve().parent
    boundary = Path(stop).resolve()
    while True:
        candidate = (current / relative).resolve()
        try:
            candidate.relative_to(boundary)
            inside = True
        except ValueError:
            inside = False
        if inside and candidate.is_file():
            return candidate
        if current == boundary or boundary not in current.parents:
            return None
        current = current.parent


def _plain_audio_output(source, audio_source, marker, output_directory):
    output_directory = Path(output_directory)
    if output_directory.resolve() == source.parent.resolve():
        return audio_source.with_name(f"{audio_source.stem}{marker}{audio_source.suffix}")
    try:
        relative = audio_source.relative_to(source.parent)
    except ValueError:
        relative = Path(audio_source.name)
    return output_directory / relative.with_name(f"{relative.stem}{marker}{relative.suffix}")


def _plain_chart_outputs(source, speed, file_pattern, output_directory, with_sources=False):
    """Plan the chart file (and its audio) written for one plain chart."""
    text, _bom = _read_chart_text(source)
    _scaled, info = transform_chart_text(text, source.suffix, speed)
    marker = format_file_marker(file_pattern, speed)
    audio_outputs = []
    reference = info.get("audio_filename")
    audio_source = _resolve_reference(source, reference, _reference_stop(source, reference))
    if audio_source is not None and is_audio(audio_source):
        audio_outputs.append(
            _plain_audio_output(source, audio_source, marker, output_directory)
        )
    chart_output = Path(output_directory) / f"{source.stem}{marker}{source.suffix}"
    if with_sources:
        return chart_output, audio_outputs, [audio_source] if audio_outputs else []
    return chart_output, audio_outputs


def _archive_storyboard_names(source):
    """Return the ``.osb`` members of an archive that match a contained chart."""
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return []
    stems = [Path(name).stem for name in members if is_chart(name)]
    return sorted(_archive_storyboard_members(stems, members).values())


def _plan_collection(
    input_paths,
    speed,
    output_folder,
    *,
    file_pattern,
    difficulty_pattern,
    folders,
    source_folder=None,
    folder_outputs=None,
    include_audio=True,
):
    """Plan complete folder copies and optional song collections."""
    sources = [_resolve_input(value) for value in input_paths]
    _root, layout, _owners, _loose = _collection_layout(
        sources, output_folder, folders, source_folder, folder_outputs
    )
    plan = []
    for group, destination in layout:
        file_marker = format_file_marker(file_pattern, speed)
        song_root = Path(group["parent"])
        taken = {
            name for name in _source_names(group, song_root)
            if file_marker in Path(name).stem
        }
        reserve = _unique_path_for(taken, destination, file_marker)
        audio_names = {}
        rows = [("变速文件夹" if group.get("source_folder") else "歌曲文件夹", str(destination))]
        for archive, selection in group["archives"]:
            relative = _song_item_path(archive, selection, song_root, destination)
            rows.append(("归档", str(reserve(relative))))
        for chart, selection in group["charts"]:
            relative = _song_item_path(chart, selection, song_root, destination)
            text, _bom = _read_chart_text(chart)
            _scaled, info = transform_chart_text(text, chart.suffix, speed)
            reference = info.get("audio_filename") or ""
            reference_root = _collection_reference_root(
                chart, reference, song_root, group.get("source_folder")
            )
            _marked_audio_reference(
                chart, reference, speed, file_marker,
                (Path(selection), song_root, destination), audio_names, reserve,
                reference_root=reference_root,
            )
            rows.append(("谱面", str(reserve(relative))))
        if include_audio:
            rows.extend(("音乐", str(target)) for target in audio_names.values())
            rows.append(("其它文件", "原文件夹内容一并复制"))
        plan.append({
            "source": str(song_root),
            "outputs": [str(destination)],
            "rows": rows,
            "assets": [],
        })
    return plan


def _plan_target(output_directory, source):
    """Return the directory a planned output goes to (per-input by default)."""
    return Path(output_directory) if output_directory else source.parent


def plan_speed_change(
    input_paths,
    speed,
    *,
    file_pattern,
    difficulty_pattern,
    output_directory=None,
    output_folder=None,
    folders=None,
    source_folder=None,
    folder_outputs=None,
    include_audio=True,
):
    """Return the outputs a speed change would create, without writing anything."""
    speed = normalized_speed(speed)
    marker = format_file_marker(file_pattern, speed)
    difficulty_marker = format_difficulty_marker(difficulty_pattern, speed)
    if output_folder or folder_outputs:
        plan = _plan_collection(
            input_paths,
            speed,
            output_folder,
            file_pattern=file_pattern,
            difficulty_pattern=difficulty_pattern,
            folders=folders or (),
            source_folder=source_folder,
            folder_outputs=folder_outputs,
            include_audio=include_audio,
        )
        if not output_folder:
            selected = [Path(folder).resolve() for folder in folder_outputs]
            loose = [
                value for value in input_paths
                if not any(folder in Path(value).resolve().parents for folder in selected)
            ]
            plan.extend(plan_speed_change(
                loose, speed, file_pattern=file_pattern,
                difficulty_pattern=difficulty_pattern,
                output_directory=output_directory, include_audio=include_audio,
            ))
        return plan
    plan = []
    for value in input_paths:
        source = _resolve_input(value)
        if is_archive(source):
            archive_output = _plan_target(output_directory, source) / (
                f"{source.stem}{marker}{source.suffix}"
            )
            rows = [("归档", str(archive_output))]
            if include_audio:
                with tempfile.TemporaryDirectory(prefix="osu-toolbox-speed-plan-") as root:
                    _extract_archive_file(source, root)
                    entries, audio = _archive_chart_entries(
                        _discover_archive(root), speed, difficulty_marker, root
                    )
                    with zipfile.ZipFile(source) as archive:
                        members = _archive_output_members(
                            root, archive.namelist(), entries, audio, marker
                        )
                    rows.extend(
                        ("谱面", f"（归档内）{members[chart]}")
                        for chart, _scaled, _reference in entries
                    )
                    rows.extend(
                        ("音乐", f"（归档内）{members[track]}") for track in sorted(audio)
                    )
            storyboards = _archive_storyboard_names(source)
            rows.extend(
                ("故事板", f"（归档内）{name}（未变速）") for name in storyboards
            )
            plan.append({
                "source": str(source),
                "outputs": [str(archive_output)],
                "rows": rows,
                "assets": [
                    {"kind": "storyboard", "name": name} for name in storyboards
                ],
            })
            continue
        chart_output, audio_outputs = _plain_chart_outputs(
            source, speed, file_pattern, _plan_target(output_directory, source)
        )
        rows = [("谱面", str(chart_output))]
        if include_audio:
            rows.extend(("音乐", str(item)) for item in audio_outputs)
        storyboards = companion_storyboards(source)
        rows.extend(("故事板", f"{item}（未变速）") for item in storyboards)
        plan.append({
            "source": str(source),
            "outputs": [str(chart_output)],
            "audio_outputs": [str(path) for path in audio_outputs] if include_audio else [],
            "rows": rows,
            "assets": [
                {"kind": "storyboard", "name": item.name} for item in storyboards
            ],
        })
    return plan


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _resolve_input(value):
    source = Path(str(value).strip().strip('"')).resolve()
    if not source.is_file():
        raise SpeedChangeError(f"输入文件不存在：{source}")
    extension = source.suffix.lower()
    if extension not in SUPPORTED_INPUT_EXTENSIONS:
        raise SpeedChangeError(f"不支持的输入格式：{extension}")
    return source


def _worker_count(item_count):
    cpu = os.cpu_count() or 4
    return max(1, min(MAX_WORKERS, item_count, cpu))


def _emit(emit_event, kind, payload):
    if emit_event is not None:
        emit_event(kind, payload)


def _log(emit_event, message):
    """Report one finished piece of work to the run log.

    One line per produced file, so a long run shows what it is doing instead of
    only a per-input summary.  The log lives in the session's log panel only --
    nothing here is ever written to a file on disk.
    """
    _emit(emit_event, "log", message)


def _log_failure(emit_event, message):
    """Report one piece of work that could not be finished, with its reason.

    The reason travels with the message, because a run that quietly leaves the
    music unconverted looks identical to a successful one in the result table.
    """
    _emit(emit_event, "warning", message)
    _log(emit_event, message)


def _log_file(emit_event, action, path):
    """Log one produced file with its name and its full path."""
    path = Path(path)
    _log(emit_event, f"{action}：{path.name}  →  {path}")


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SpeedCancelledError("变速任务已取消")


def _reference_with_renamed_audio(reference, renamed):
    """Keep a reference's directory part while swapping the file name."""
    text = str(reference or "").replace("\\", "/")
    directory, _sep, _name = text.rpartition("/")
    return f"{directory}/{renamed}" if directory else renamed


def _plain_chart_job(index, source, speed, options, registry, emit_event, cancel_event):
    """Speed-change one chart beside itself.

    Audio is transcoded before the chart is written, so its reference only
    changes after the marked track exists. A failed transcode still produces
    the scaled chart with its original audio reference and reports the failure.

    The song name is marked only when the chart was reached through a selected
    folder, so naming one file changes that file and not the song it belongs to.
    """
    _check_cancelled(cancel_event)
    file_marker = format_file_marker(options["file_pattern"], speed)
    difficulty_marker = _difficulty_marker_for(options, speed)
    warnings = []
    text, bom = _read_chart_text(source)
    scaled, info = transform_chart_text(
        text,
        source.suffix,
        speed,
        difficulty_marker=difficulty_marker,
        song_marker=_song_marker_for(source, options.get("folders_selected")),
    )
    details = []
    assets = [
        {"kind": "storyboard", "path": str(item), "name": item.name}
        for item in companion_storyboards(source)
    ]
    for asset in assets:
        _emit(emit_event, "warning", STORYBOARD_WARNING.format(name=asset["name"]))

    output_directory = _output_directory(options, source)
    reference = info.get("audio_filename") or ""
    audio_source = _resolve_reference(source, reference, _reference_stop(source, reference))
    audio_output = None
    marked_reference = None
    if reference and audio_source is None:
        warnings.append(
            f"{source.name} 引用的音乐不存在：{reference}（未变速，输出仍指向原音乐）"
        )
    elif audio_source is not None and is_audio(audio_source):
        audio_output = _plain_audio_output(source, audio_source, file_marker, output_directory)
        marked_reference = (
            _reference_with_renamed_audio(reference, audio_output.name)
            if output_directory.resolve() == source.parent.resolve()
            else os.path.relpath(audio_output, output_directory).replace("\\", "/")
        )
        try:
            registry.ensure(audio_source, audio_output)
        except SpeedCancelledError:
            raise
        except Exception as exc:
            audio_output = None
            marked_reference = None
            warnings.append(f"{source.name} 的音乐未变速：{exc}")
    if marked_reference:
        scaled = _rewrite_audio_reference(
            scaled, source.suffix, reference, marked_reference
        )
        details.append(("音乐", str(audio_output)))

    output_directory = _output_directory(options, source)
    output_directory.mkdir(parents=True, exist_ok=True)
    chart_output = output_directory / f"{source.stem}{file_marker}{source.suffix}"
    _check_cancelled(cancel_event)
    _write_chart_text(chart_output, scaled, bom, options.get("allow_overwrite", True))
    details.insert(0, ("谱面", str(chart_output)))
    _log_file(emit_event, "谱面已生成", chart_output)
    if audio_output is not None and marked_reference:
        _log_file(emit_event, "音乐已变速", audio_output)
    for message in warnings:
        _log_failure(emit_event, message)
    return {
        "index": index,
        "source": str(source),
        "outputs": [str(chart_output)],
        "outputs_detail": details,
        "assets": assets,
        "warnings": warnings,
        "error": None,
    }


def _archive_chart_entries(charts, speed, difficulty_marker, root=None):
    """Rewrite every chart in an extracted archive and collect its audio."""
    entries = []
    audio = {}
    for chart in charts:
        text, _bom = _read_chart_text(chart)
        scaled, info = transform_chart_text(
            text, chart.suffix, speed, difficulty_marker=difficulty_marker
        )
        reference = info.get("audio_filename") or ""
        entries.append((chart, scaled, reference))
        if not reference:
            continue
        name = _resource_archive_name(reference)
        if not is_audio(name):
            continue
        source = _resolve_reference(chart, reference, root or chart.parent)
        if source is not None and source not in audio:
            audio[source] = reference
    return entries, audio


def _write_speed_changed_archive(
    index,
    source,
    destination,
    speed,
    *,
    file_marker,
    difficulty_marker,
    registry,
    emit_event,
    cancel_event,
    allow_overwrite=True,
):
    """Write ``source`` into a new archive at ``destination``.

    Every chart and its referenced music are speed-changed and renamed together.
    Directory paths and unreferenced resources are preserved.
    """
    _check_cancelled(cancel_event)
    destination.parent.mkdir(parents=True, exist_ok=True)
    difficulty_marker = difficulty_marker or file_marker or format_difficulty_marker(" {num}x", speed)
    chart_details = []
    audio_details = []
    assets = []
    warnings = []
    with _staged_output(destination, allow_overwrite) as archive_target, tempfile.TemporaryDirectory(prefix="osu-toolbox-speed-archive-") as root:
        _extract_archive_file(source, root)
        charts = _discover_archive(root)
        if not charts:
            raise SpeedChangeError(f"归档内没有可读谱面：{source.name}")
        entries, audio = _archive_chart_entries(charts, speed, difficulty_marker, root)
        for chart, _scaled, reference in entries:
            if reference and _resolve_reference(chart, reference, root) is None:
                message = f"{chart.name} 引用的音乐不存在：{reference}（归档结果不完整）"
                warnings.append(message)
                _log_failure(emit_event, message)
        used_names = set()
        handled_names = set()
        renamed_audio = {}
        with zipfile.ZipFile(source) as source_archive:
            original = source_archive.namelist()
            original_paths = {
                (Path(root) / name.replace("\\", "/")).resolve()
                .relative_to(Path(root).resolve()).as_posix().casefold(): name
                for name in original if name and not name.endswith("/")
            }
            output_members = _archive_output_members(
                root, original, entries, audio, file_marker
            )
            storyboards = _archive_storyboard_members(
                [chart.stem for chart, _scaled, _reference in entries], original
            )
            for member in storyboards.values():
                assets.append({"kind": "storyboard", "path": member, "name": member})
            with zipfile.ZipFile(archive_target, "w", zipfile.ZIP_DEFLATED) as out_archive:
                # Each shared track is written once under its reserved new name.
                for order, audio_source in enumerate(sorted(audio)):
                    _check_cancelled(cancel_event)
                    stage = Path(root) / f".speed-audio-{order}{audio_source.suffix}"
                    try:
                        registry.ensure(audio_source, stage)
                    except SpeedCancelledError:
                        raise
                    except Exception as exc:
                        # The chart is still written and keeps its original
                        # reference, so one bad track never costs the archive.
                        message = f"音乐未变速（归档内 {audio_source.name}）：{exc}"
                        warnings.append(message)
                        _log_failure(emit_event, message)
                        _remove_partial(stage)
                        continue
                    original_member = original_paths[
                        audio_source.relative_to(root).as_posix().casefold()
                    ]
                    handled_names.add(original_member.casefold())
                    member = output_members[audio_source]
                    used_names.add(member.casefold())
                    with stage.open("rb") as audio_input, out_archive.open(member, "w") as audio_output:
                        shutil.copyfileobj(audio_input, audio_output)
                    renamed_audio[audio_source] = Path(root) / member.replace("\\", "/")
                    audio_details.append(("音乐", f"（归档内）{member}"))
                    _log(emit_event, f"音乐已变速（归档内）：{member}")
                for chart, scaled, reference in entries:
                    _check_cancelled(cancel_event)
                    original_member = original_paths[chart.relative_to(root).as_posix().casefold()]
                    handled_names.add(original_member.casefold())
                    member = output_members[chart]
                    audio_source = _resolve_reference(chart, reference, root)
                    if audio_source in renamed_audio:
                        marked_reference = os.path.relpath(
                            renamed_audio[audio_source], chart.parent
                        ).replace("\\", "/")
                        scaled = _rewrite_audio_reference(
                            scaled, chart.suffix, reference, marked_reference
                        )
                    used_names.add(member.casefold())
                    out_archive.writestr(member, scaled)
                    chart_details.append(("谱面", f"（归档内）{member}"))
                    _log(emit_event, f"谱面已生成（归档内）：{member}")
                for name in original:
                    if not name:
                        continue
                    if name.casefold() in handled_names:
                        continue
                    info = source_archive.getinfo(name)
                    out_archive.writestr(info, source_archive.read(name))
        _check_cancelled(cancel_event)
    details = [("归档", str(destination)), *audio_details, *chart_details]
    _log_file(emit_event, "归档已生成", destination)
    for asset in assets:
        details.append(("故事板", f"（归档内）{asset['name']}（未变速）"))
        _log_failure(emit_event, STORYBOARD_WARNING.format(name=asset["name"]))
    return {
        "index": index,
        "source": str(source),
        "outputs": [str(destination)],
        "outputs_detail": details,
        "assets": assets,
        # The names this archive's rewrite produced, so the rest of the run knows
        # which paths are already taken.
        "members": sorted(used_names),
        "warnings": warnings,
        "error": None,
    }


def _archive_job(index, source, speed, options, registry, emit_event, cancel_event):
    """Speed-change one archive into a new archive beside the source.

    The source archive is never modified and no speed-changed member is added
    to it: the result is always a separate archive.
    """
    file_marker = format_file_marker(options["file_pattern"], speed)
    destination = _output_directory(options, source) / (
        f"{source.stem}{file_marker}{source.suffix}"
    )
    return _write_speed_changed_archive(
        index,
        source,
        destination,
        speed,
        file_marker=file_marker,
        difficulty_marker=_difficulty_marker_for(options, speed),
        registry=registry,
        emit_event=emit_event,
        cancel_event=cancel_event,
        allow_overwrite=options.get("allow_overwrite", True),
    )
    _check_cancelled(cancel_event)
def _unique_directory(parent, name, used):
    """Return a non-colliding subdirectory of ``parent``."""
    candidate = Path(parent) / name
    if str(candidate).casefold() not in used:
        used.add(str(candidate).casefold())
        return candidate
    index = 2
    while True:
        candidate = Path(parent) / f"{name}-{index}"
        if str(candidate).casefold() not in used:
            used.add(str(candidate).casefold())
            return candidate
        index += 1


def _group_inputs_by_directory(sources):
    """Group inputs by their own directory, preserving first-seen order."""
    groups = {}
    for source in sources:
        groups.setdefault(str(source.parent), []).append(source)
    return list(groups.values())


def _is_storage_level(folder):
    """Return whether a folder is a numbered chart-set level (``Song/0``)."""
    return Path(folder).name.isdigit()


def _innermost_selection(source, folders):
    """Return the deepest selected folder containing ``source``, if any."""
    source = Path(source)
    found = None
    for value in folders:
        folder = Path(value)
        if not (folder == source or folder in source.parents):
            continue
        if found is None or len(folder.parts) > len(found.parts):
            found = folder
    return found


def _copy_roots(group):
    """Return the folders one song's result is a copy of.

    Copying the song folder itself is what makes a chart's own references keep
    working: Malody stores a chart set in ``Song/0`` while the music it names sits
    in ``Song``, so selecting ``Song/0`` alone would ship a chart whose music was
    left behind.  Only the song folder is copied -- never the library or theme
    folder around it -- so one song's files can never drag a neighbour along.
    """
    selected = [Path(value) for value in group["selected"]]
    parent = Path(group["parent"])
    roots = [
        folder for folder in selected
        if folder == parent or parent in folder.parents
    ]
    if str(parent) not in roots:
        roots.append(str(parent))
    return roots


def _song_groups(sources, folders):
    """Group the selected files into the song folders a collection produces.

    A selected folder is split into one group per song, judged from the real
    layout: a folder holding a chart or an archive directly is a song itself and
    contributes its contents, while a folder whose charts all live in song
    folders of their own -- a beatmap library, a theme folder -- is split at that
    level instead.  Selecting a whole library therefore yields one result folder
    per song rather than one copy of the library.

    A song's group copies the song folder itself, so every level inside survives:
    ``Song/0/chart.mc`` becomes ``Song/0/chart 1.1x.mc`` inside the ``Song``
    result, and a numbered storage level never names the result.  Two storage
    levels of the same song, such as ``Song/0`` and ``Song/1``, merge into that
    one song.  Copying the song rather than the selection is also what keeps the
    music: Malody stores a chart set in ``Song/0`` while the track it names sits
    in ``Song``.

    A file named directly is its own selection: the folder holding it is the
    song, so naming one chart inside a library yields that song alone rather than
    the library.  Order follows the selection, so results are stable.

    Returns ``(groups, owners)``; ``owners`` maps every input file to the
    ``(selection, song root)`` it belongs to, which is what decides where the
    file lands once the song's result folder exists.  Every archive the
    selection reaches ends up in ``group["archives"]``: archives are speed-changed
    like everything else.
    """
    named = {}
    for item in folders:
        named.setdefault(os.path.normcase(str(Path(item))), Path(item))
    sources = [Path(source) for source in sources]
    groups = {}
    splits = {}
    owners = {}

    def holds_any(folder):
        """Return whether a chart sits *directly* inside ``folder``."""
        return any(source.parent == folder for source in sources)

    def child_holds_any(folder):
        """Return whether a chart sits directly inside one of ``folder``'s children."""
        return any(
            source.parent != folder and folder in source.parent.parents
            for source in sources
        )

    def contains_any(folder):
        """Return whether a chart sits anywhere inside ``folder``."""
        return any(folder in source.parents for source in sources)

    def create(name, root, key):
        group = groups.setdefault(
            key,
            {"name": name, "parent": root, "folders": [], "selected": [],
             "charts": [], "archives": []},
        )
        if str(root) not in group["folders"]:
            group["folders"].append(str(root))
        return group

    def groups_for(folder, origin):
        """Return the song groups ``folder`` splits into, tagged with their origin.

        ``origin`` is the folder the user selected that this branch came from.
        The tag matters because a song's result is a copy *of the selection*: a
        chart reached through the library sits at ``<library>/<song>/...``, so
        dropping the library level would lose the structure the user pointed at.
        """
        folder = Path(folder)
        marker = os.path.normcase(str(folder))
        if marker not in splits:
            if holds_any(folder):
                # The folder names its own charts, so it is the song.  A numbered
                # level reached this way is storage instead: the song is the
                # folder holding it, and the level keeps its place inside it.
                if _is_storage_level(folder) and folder.name:
                    name = folder.parent.name or folder.name
                    key = ("child", os.path.normcase(str(folder.parent)), name.casefold())
                    group = create(name, folder.parent, key)
                else:
                    group = create(folder.name, folder, ("song", marker))
                splits[marker] = [(group, folder)]
            elif contains_any(folder) and not child_holds_any(folder):
                # The charts are deeper, and no child names one of its own: every
                # level down to them belongs to this song, so it is the song --
                # which is what keeps ``Song/0/chart.mc`` whole.
                splits[marker] = [(create(folder.name, folder, ("song", marker)), folder)]
            else:
                # Charts sit in several child folders, so this one is a wrapper
                # and its children hold the songs.
                branches = []
                for child in sorted(
                    (item for item in folder.iterdir() if item.is_dir()),
                    key=lambda path: path.name.casefold(),
                ):
                    for group, root, _origin in groups_for(child, origin):
                        branches.append((group, root))
                splits[marker] = branches
        return [(group, root, origin) for group, root in splits[marker]]

    for folder in named.values():
        groups_for(folder, folder)
    for source in sources:
        # A folder the user selected is split into its songs; a file named
        # directly is filed under the folder holding it.  Either way the walk
        # starts at that folder, so a chart standing several levels down still
        # names its own song instead of the level it happens to sit in.
        selection = _innermost_selection(source, folders) or source.parent
        branches = groups_for(selection, selection)
        target = next(
            (item for item in branches if item[1] == source.parent), None
        )
        if target is None:
            branches = groups_for(source.parent, source.parent)
            target = next(
                (item for item in branches if item[1] == source.parent), None
            )
        if target is None:
            continue
        group, _root, origin = target
        # The song's result is a copy of the folder the user selected, so the
        # chain from the selection down to the song is reproduced; a directly
        # named chart contributes the song folder alone.
        contribution = (
            str(group["parent"])
            if os.path.normcase(str(selection)) in named
            else str(origin)
        )
        if contribution not in group["selected"]:
            group["selected"].append(contribution)
        owners[str(source)] = (str(origin), str(group["parent"]))
        # Every archive is repacked, whether the user picked it as a file or it
        # was found inside a selected folder: everything in the folder is
        # speed-changed, archives included.
        bucket = group["archives"] if is_archive(source) else group["charts"]
        if all(item != (source, origin) for item in bucket):
            bucket.append((source, origin))
    return [
        group for group in groups.values()
        if group["charts"] or group["archives"]
    ], owners


def _collection_destination(output_folder, source_folder=None):
    root = Path(output_folder).resolve()
    if source_folder:
        source = Path(source_folder).resolve()
        if root == source or source in root.parents:
            raise SpeedChangeError("输出目录必须位于原文件夹之外")
    return root


def _collection_groups(sources, folders, source_folder=None):
    if not source_folder:
        return _song_groups(sources, folders or ())
    root = Path(source_folder).resolve()
    sources = [Path(source) for source in sources]
    if not root.is_dir() or any(root not in source.parents for source in sources):
        raise SpeedChangeError("文件夹变速输入必须位于所选文件夹内")
    group = {
        "name": root.name, "parent": root, "folders": [str(root)],
        "selected": [str(root)],
        "charts": [(source, root) for source in sources if not is_archive(source)],
        "archives": [(source, root) for source in sources if is_archive(source)],
    }
    owners = {str(source): (str(root), str(root)) for source in sources}
    return [group], owners


def _collection_layout(sources, output_folder, folders, source_folder=None, folder_outputs=None):
    """Resolve all folder destinations once for both planning and execution."""
    root = Path(output_folder).resolve() if output_folder else None
    selections = [
        (Path(source).resolve(), Path(destination).resolve())
        for source, destination in (folder_outputs or {}).items()
    ]
    if source_folder:
        if selections:
            raise SpeedChangeError("文件夹输出配置不能重复指定")
        selections = [(Path(source_folder).resolve(), _collection_destination(output_folder, source_folder))]
    selected = [source for source, _destination in selections]
    if source_folder and any(selected[0] not in value.parents for value in sources):
        raise SpeedChangeError("文件夹变速输入必须位于所选文件夹内")
    destinations = [destination for _source, destination in selections]
    if len(set(selected)) != len(selected) or any(
        left in right.parents or right in left.parents
        for index, left in enumerate(selected) for right in selected[index + 1:]
    ):
        raise SpeedChangeError("文件夹输入重叠，请只保留最外层文件夹")
    if len(set(destinations)) != len(destinations):
        raise SpeedChangeError("多个文件夹使用同一个输出目录")
    for destination in destinations:
        if any(destination == source or source in destination.parents for source in selected):
            raise SpeedChangeError("输出目录必须位于原文件夹之外")
    if any(
        left in right.parents or right in left.parents
        for index, left in enumerate(destinations) for right in destinations[index + 1:]
    ):
        raise SpeedChangeError("变速文件夹的输出目录不能相互嵌套")

    layout, owners = [], {}
    for source, destination in selections:
        members = [value for value in sources if source in value.parents]
        if not members:
            raise SpeedChangeError(f"文件夹内没有可变速的谱面或归档：{source}")
        groups, selected_owners = _collection_groups(members, [source], source)
        group = groups[0]
        group["source_folder"] = str(source)
        layout.append((group, destination))
        owners.update(selected_owners)
    loose = [source for source in sources if str(source) not in owners]
    if root and loose:
        groups, collection_owners = _song_groups(loose, folders or ())
        used = {str(destination).casefold() for destination in destinations}
        for group in groups:
            destination = _unique_directory(root, group["name"], used)
            if any(destination == source or source in destination.parents for source in selected):
                raise SpeedChangeError("输出目录必须位于原文件夹之外")
            layout.append((group, destination))
        owners.update(collection_owners)
        loose = []
    copy_roots = {
        Path(value).resolve()
        for group, _destination in layout for value in _copy_roots(group)
    }
    if any(
        destination == source or source in destination.parents
        for _group, destination in layout for source in copy_roots
    ):
        raise SpeedChangeError("输出目录必须位于原文件夹之外")
    return root, layout, owners, loose


def _song_item_path(source, selection, song_root, destination):
    """Return where one item lands inside a song's result folder.

    ``song_root`` is the source folder the result is named after and
    ``destination`` is that result folder.  An item underneath ``song_root`` is
    filed relative to it, so the song folder's own contents land directly in the
    result.  An item from a wrapper the user selected -- a numbered storage level
    or a directly named chart -- keeps that wrapper's place instead, so ``Song/0``
    becomes ``<result>/0`` and the whole chain the user selected survives.
    """
    source = Path(source)
    song_root = Path(song_root)
    if song_root == source or song_root in source.parents:
        relative = source.relative_to(song_root)
    else:
        relative = Path(Path(selection).name) / source.relative_to(selection)
    return Path(destination) / relative


def _copy_remaining_files(source, destination, handled, skip_names=(), claimed=(), *, preserve_empty=False):
    """Copy everything under ``source`` to ``destination`` except ``handled``.

    The whole folder travels along, not just the files a chart names: a song
    folder legitimately holds things the charts never mention (a video, extra
    artwork, notes), and copying only the referenced subset would silently drop
    them from the collection.

    ``claimed`` names the destinations this song already produced.  A generic copy
    must never land on one of them: a file that was speed-changed already holds the
    speed-changed content, and copying the source over it would replace the result
    with the original.
    """
    handled = {str(item).casefold() for item in handled}
    skip_names = {str(item).casefold() for item in skip_names}
    claimed = {str(item).casefold() for item in claimed}
    copied = []
    for root, directory_names, file_names in os.walk(source):
        directory_names.sort()
        if preserve_empty:
            (destination / Path(root).relative_to(source)).mkdir(parents=True, exist_ok=True)
        for file_name in sorted(file_names):
            full = Path(root) / file_name
            if str(full).casefold() in handled or str(full).casefold() in skip_names:
                continue
            relative = full.relative_to(source)
            target = destination / relative
            if str(target).casefold() in claimed:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full, target)
            copied.append(str(target))
            claimed.add(str(target).casefold())
    return copied


def _output_directory(options, source):
    """Return where one input's results go.

    With no explicit output directory every source writes beside itself: one
    folder input therefore produces results in each chart's own directory
    rather than collecting them all in the first one.
    """
    configured = options.get("output_directory")
    return Path(configured) if configured else source.parent


def _song_marker_for(source, folders_selected):
    """Return the song-name marker one plain chart takes.

    A chart the user named directly keeps the song name it was authored with:
    the marked file name and the marked difficulty name already say what was
    changed, and the song name is shared metadata rather than a per-file label.
    A chart reached through a folder the user selected is one song of a
    collection, so its song name carries the marker as well.

    ``None`` means "the same text as the difficulty name" and ``""`` means "leave
    the song name as authored", exactly as ``transform_chart_text`` reads them.
    """
    source = Path(source)
    for value in folders_selected or ():
        folder = Path(value)
        if folder == source or folder in source.parents:
            return None
    return ""


def _chart_output_path(source_root, source, destination_root, file_marker):
    """Return the marked output path of one chart in its destination tree.

    The chart's own subdirectory is preserved, so a chart stored as
    ``Song/0/chart.mc`` lands in ``<destination>/0/chart 1.5x.mc`` instead of
    being flattened next to the song folder's other files.
    """
    relative = source.relative_to(source_root)
    marked = relative.parent / f"{relative.stem}{file_marker}{relative.suffix}"
    return destination_root / marked


def _marked_audio_reference(
    chart, reference, speed, file_marker, placement, audio_names, reserve,
    reference_root=None,
):
    """Return the marked reference for a chart's music, or ``None``.

    ``None`` means the chart names no music, names something that is not audio,
    or names a file that is not there -- in every case there is nothing to
    transcode and the chart keeps the reference it was authored with.  The
    returned reference is the relative path from the chart's *new* directory to
    the music's *new* file: every level between the two is reproduced inside the
    result, so rewriting the marked file name is all a chart ever needs, whatever
    subfolders it and its music sit in.  ``audio_names`` caches the source-to-
    marked names so several charts sharing one track all point at one output, and
    ``reserve`` gives the target a name no other output has claimed.
    """
    if not reference:
        return None
    selection, song_root, destination_root = placement
    # The search reaches up to the song folder, not just the selection: Malody
    # keeps a chart set in ``Song/0`` while the music it names sits in ``Song``,
    # so stopping at the selection would leave that music behind.
    audio_source = _resolve_reference(chart, reference, reference_root or song_root)
    if audio_source is None or not is_audio(audio_source):
        return None
    marked = audio_names.get(audio_source)
    if marked is None:
        # ``reserve`` applies the marker and claims the name in one step.
        audio_path = (
            destination_root / audio_source.name
            if reference_root and song_root not in audio_source.parents
            else _song_item_path(audio_source, selection, song_root, destination_root)
        )
        marked = reserve(audio_path)
        audio_names[audio_source] = marked
    # Rebuild the reference the way the chart wrote it: a relative path from the
    # chart's own new directory to the marked file, so a chart and its music in
    # different subfolders still resolve.
    chart_target = _song_item_path(chart, selection, song_root, destination_root)
    try:
        relative_to_chart = os.path.relpath(marked, chart_target.parent)
    except ValueError:
        relative_to_chart = marked.name
    return str(relative_to_chart).replace("\\", "/")


def _render_marked_chart(
    source,
    destination,
    speed,
    difficulty_marker,
    reference,
    marked_reference,
    song_marker=None,
):
    """Transform one chart and write it to ``destination``.

    The chart is read and rewritten exactly once: the audio reference is
    swapped in memory before the write, so nothing has to be re-read and
    transformed afterwards.  ``song_marker`` decides whether the song name is
    marked; ``None`` keeps the default of marking it like the difficulty name.
    Returns ``(info, assets)``.
    """
    text, bom = _read_chart_text(source)
    scaled, info = transform_chart_text(
        text,
        source.suffix,
        speed,
        difficulty_marker=difficulty_marker,
        song_marker=song_marker,
    )
    if reference and marked_reference and marked_reference != reference:
        scaled = _rewrite_audio_reference(scaled, source.suffix, reference, marked_reference)
    assets = [
        {"kind": "storyboard", "path": str(item), "name": item.name}
        for item in companion_storyboards(source)
    ]
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_chart_text(destination, scaled, bom)
    return info, assets


def _collection_chart_job(
    folder, charts, speed, patterns, registry, cancel_event, emit_event, *, finalize=True
):
    """Copy one selected folder into the collection and speed-change it there.

    Three steps, in this order for a reason:

    1. **audio jobs** are resolved and returned (or run here when
       ``finalize=True``).  The caller can run every folder's jobs on one pool
       so concurrency follows the number of *tracks* rather than the number of
       selected folders -- a single chosen folder holding twenty songs is the
       common case, and tying concurrency to the folder count left it serial.
    2. **charts** are rendered using only the references whose transcode really
       succeeded, so a chart never points at a track that was not produced.
    3. **the rest of the tree** is copied, after the audio has landed, so a
       generic copy can never overwrite a transcoded track.

    Output paths come from ``folder["owners"]``, which pairs every input file with
    the folder the user selected it through.  That is what keeps a whole chain:
    a chart stored in ``Song/0/chart.mc`` keeps its ``0`` inside the ``Song``
    result, and a library selection keeps each song's own subfolder.
    """
    destination_root = Path(folder["destination"])
    file_marker = patterns["file_marker"]
    charts = [item for item in charts if is_chart(item)]
    details = []
    assets = []
    handled = set()
    staged_audio = []
    staged_names = []
    warnings = []
    audio_jobs = []
    # One source audio file may be shared by several charts; every chart keeps
    # the same marked name for it, and it is transcoded only once.
    audio_names = {}
    queued_audio = set()
    # The marked paths the chart rewrite produces; the tree copy below must not
    # land on one of them (see ``_copy_remaining_files``).
    produced = []
    # Every source file that already carries the marker is a result of an earlier
    # run, so its name is reserved.  Nothing else is: two charts in different
    # subfolders legitimately produce the same file name, and that is what the
    # destination's own structure keeps apart.
    taken = folder.setdefault("taken", set(folder.get("marked_names") or ()))
    unique = _unique_path_for(taken, destination_root, file_marker)

    destination_root.mkdir(parents=True, exist_ok=True)

    # Pass 1: read every chart once, learn its audio reference, queue the tracks.
    plans = []
    for order, chart in enumerate(charts):
        _check_cancelled(cancel_event)
        # The source chart itself never travels along: its marked rewrite is the
        # result, and copying the original too would ship an unchanged duplicate.
        handled.add(chart)
        selection, song_root = folder["owners"].get(
            str(chart), (str(chart.parent), str(chart.parent))
        )
        placement = (Path(selection), Path(song_root), destination_root)
        # A chart named directly keeps the song name it was authored with; one
        # reached through a selected folder marks it beside the difficulty name.
        song_marker = _song_marker_for(chart, folder.get("folders_selected"))
        if song_marker is None:
            song_marker = patterns["difficulty_marker"]
        text, _bom = _read_chart_text(chart)
        _scaled, probe = transform_chart_text(
            text,
            chart.suffix,
            speed,
            difficulty_marker=patterns["difficulty_marker"],
            song_marker=song_marker,
        )
        reference = probe.get("audio_filename") or ""
        reference_root = _collection_reference_root(
            chart, reference, placement[1], folder.get("source_folder")
        )
        marked_reference = _marked_audio_reference(
            chart, reference, speed, file_marker, placement, audio_names, unique,
            reference_root=reference_root if folder.get("source_folder") else None,
        )
        if reference and marked_reference is None:
            warnings.append(
                f"{chart.name} 引用的音乐不存在或不是音频：{reference}（未变速）"
            )
        if marked_reference:
            audio_source = _resolve_reference(chart, reference, reference_root)
            # Already claimed by ``_marked_audio_reference``, so shared tracks all
            # point at the one file that was reserved for them.
            audio_target = audio_names[audio_source]
            if audio_source not in queued_audio:
                queued_audio.add(audio_source)
                staged = destination_root / f".speed-audio-{order}{audio_source.suffix}"
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged_names.append(str(staged))
                handled.add(audio_source)
                produced.append(audio_target)
                if finalize:
                    if _run_audio_job(
                        staged, audio_target, audio_source, registry, warnings, cancel_event
                    ):
                        _place_audio(staged, audio_target)
                        audio_jobs.append((staged, audio_target, audio_source))
                        details.append(("音乐", str(audio_target)))
                else:
                    # The caller runs the track and appends the detail row only once
                    # it has really landed, so the report never promises a file that
                    # a failed transcoding did not produce.
                    audio_jobs.append((staged, audio_target, audio_source))
        # The chart's own target is claimed here, so the tree copy below knows
        # which paths are taken and the write uses exactly this name.
        target = unique(
            _marked_chart_path(chart, placement, destination_root, file_marker)
        )
        produced.append(target)
        plans.append(
            (chart, placement, reference, marked_reference, target, song_marker)
        )
    entry = {
        "folder": str(destination_root),
        "charts": [str(item) for item in charts],
        "details": details,
        "assets": assets,
        "warnings": warnings,
        "copied": 0,
        # Carried to the finishing step, which copies the rest of the tree.
        "_handled": handled,
        "_staged": set(staged_names),
        "_produced": produced,
    }

    if not finalize:
        # The caller runs the queued tracks and then renders the charts, so a
        # chart is only rewired once its track really exists.
        return entry, audio_jobs, plans

    _render_collection_charts(
        plans, speed, patterns, details, assets, entry, emit_event
    )
    copied = _copy_song_tree(folder, handled, skip_names=staged_names)
    details.append(("其它文件", f"复制 {len(copied)} 个"))
    entry["copied"] = len(copied)
    _log(emit_event, f"{folder['name']} 其它文件复制完成：{len(copied)} 个")
    for message in entry["warnings"]:
        _log_failure(emit_event, message)
    return entry, audio_jobs, plans


def _collection_reference_root(chart, reference, song_root, source_folder):
    """Include parent music when a complete numbered storage folder is copied."""
    if source_folder:
        reference_stop = _reference_stop(chart, reference)
        if reference_stop != song_root and song_root not in reference_stop.parents:
            return reference_stop
    return song_root


def _marked_chart_path(chart, placement, destination_root, file_marker):
    """Return where one source chart sits inside its result folder.

    The caller marks the name; this only works out the place, so the marker is
    applied in one place for charts, music and archives alike.
    """
    selection, song_root, _destination = placement
    return _song_item_path(chart, selection, song_root, destination_root)


def _render_collection_charts(
    plans, speed, patterns, details, assets, entry, emit_event=None
):
    """Write every chart, rewiring only the references whose track was produced.

    A track that failed to transcode leaves its chart pointing at the original
    file: the chart still opens, and the missing conversion is reported instead
    of shipping a reference to a file that was never created.
    """
    for chart, _placement, reference, marked_reference, target, song_marker in plans:
        if marked_reference:
            candidate = (target.parent / marked_reference).resolve()
            if not candidate.is_file():
                marked_reference = None
                # The failed transcode was already logged with its reason when
                # the track ran; this line says what the chart does instead.
                _log(
                    emit_event,
                    f"{chart.name} 保留原音频引用（音乐未变速）：{reference}",
                )
        _info, chart_assets = _render_marked_chart(
            chart,
            target,
            speed,
            patterns["difficulty_marker"],
            reference,
            marked_reference,
            song_marker,
        )
        assets.extend(chart_assets)
        details.append(("谱面", str(target)))
        _log_file(emit_event, "谱面已生成", target)


def _run_audio_job_quiet(job, registry, entry, cancel_event, emit_event=None):
    """Run one queued track and place it, recording a failure on ``entry``.

    Each track logs its own line the moment it settles, so a collection run
    reports every file it finishes instead of only the per-song summary.  A track
    that cannot be transcoded logs the reason with it: the chart is still written
    and keeps its original reference, so this is where a missing conversion
    becomes visible.  The message is marked reported so the song's closing
    roll-up does not repeat it.
    """
    staged, target, source = job
    if not _run_audio_job(
        staged, target, source, registry, entry["warnings"], cancel_event
    ):
        _report_warning(emit_event, entry, entry["warnings"][-1])
        entry["_handled"].discard(source)
        entry["_produced"] = [path for path in entry["_produced"] if path != target]
        return False
    try:
        _place_audio(staged, target)
    except OSError as exc:
        # The chart is rendered afterwards and only rewires a track that really
        # landed, so a failed move downgrades to "music not speed-changed".
        entry["warnings"].append(f"{Path(target).name} 的音乐未变速：{exc}")
        _remove_partial(staged)
        _report_warning(emit_event, entry, entry["warnings"][-1])
        entry["_handled"].discard(source)
        entry["_produced"] = [path for path in entry["_produced"] if path != target]
        return False
    entry["details"].append(("音乐", str(target)))
    _log_file(emit_event, "音乐已变速", target)
    return True


def _report_warning(emit_event, entry, message):
    """Log one failure now and remember that it has been reported."""
    _log_failure(emit_event, message)
    reported = entry.setdefault("_reported", set())
    reported.add(message)


def _copy_song_tree(folder, handled, skip_names=()):
    """Copy every selected folder of one song into its result, keeping its place.

    A folder the user selected is copied whole, so nothing the charts never
    mention is lost.  Where it lands follows the same rule the files do: the
    folder the song is named after contributes its contents, and a storage level
    such as ``Song/0`` contributes its own subfolder.
    """
    destination_root = Path(folder["destination"])
    song_root = Path(folder["source"])
    copied = []
    claimed = set(folder.get("claimed") or ())
    for value in folder["sources"]:
        source = Path(value)
        relative = _song_item_path(source, source, song_root, destination_root)
        produced = _copy_remaining_files(
            source, relative, handled, skip_names=skip_names, claimed=claimed,
            preserve_empty=bool(folder.get("source_folder")),
        )
        copied.extend(produced)
        claimed.update(str(item) for item in produced)
    return copied


def _finish_collection_folder(
    index, folders, results, plans, speed, patterns, emit_event
):
    """Render a folder's charts and copy the rest of its tree, after its audio.

    A song of archives only has no chart phase (``plans`` is ``None``), but its
    tree still has to be copied, so that is done here for both cases.
    """
    entry = results[index]
    if entry.get("error"):
        return
    folder = folders[index]
    try:
        if plans is not None:
            _render_collection_charts(
                plans, speed, patterns,
                entry["details"], entry["assets"], entry, emit_event,
            )
        handled = entry.pop("_handled", set())
        staged_names = entry.pop("_staged", set())
        # Every path this song produced: the tree copy must not land on one of
        # them, or the original would replace the speed-changed result.
        folder["claimed"] = [
            str(item) for item in entry.pop("_produced", [])
        ]
        copied = _copy_song_tree(folder, handled, skip_names=staged_names)
        entry["details"].append(("其它文件", f"复制 {len(copied)} 个"))
        entry["copied"] = len(copied)
        _log(emit_event, f"{folder['name']} 其它文件复制完成：{len(copied)} 个")
    except Exception as exc:
        entry["error"] = str(exc)
        _log_failure(emit_event, f"{folder['name']} 变速失败：{exc}")
    # Warnings surface once, when the song is finished: anything already logged
    # while it happened (a failed track) is not reported a second time.
    reported = entry.pop("_reported", set())
    for message in dict.fromkeys(entry["warnings"]):
        if message not in reported:
            _log_failure(emit_event, message)




def _run_audio_job(staged, target, source, registry, warnings, cancel_event):
    """Transcode one queued track, recording any failure in ``warnings``."""
    try:
        registry.ensure(source, staged)
    except SpeedCancelledError:
        raise
    except Exception as exc:
        # The chart is written afterwards and keeps its original audio
        # reference, so a failed transcode never costs the whole run.
        warnings.append(f"{Path(target).name} 的音乐未变速：{exc}")
        _remove_partial(staged)
        return False
    return True


def _collection_entry(folder):
    """Return a blank result entry for one song folder."""
    return {
        "folder": str(folder["destination"]),
        "charts": [],
        "details": [],
        "assets": [],
        "warnings": [],
        "copied": 0,
        "_handled": set(),
        "_staged": set(),
    }


def _written_details(entries):
    """Return ``(归档, path)`` rows for the archives already written."""
    return [
        ("归档", str(target))
        for entry in entries
        for target in entry["charts"]
    ]


def _written_warnings(entries):
    """Return the warnings the archives already written reported."""
    return [
        message for entry in entries for message in entry.get("warnings") or ()
    ]


def _archive_folder_job(folder, job, speed, patterns, registry, cancel_event, emit_event):
    """Speed-change one archive into its song folder inside the collection.

    An archive input stays an archive: the result is a new ``.osz``/``.mcz``
    named after the source with the marker, holding every chart and its music
    speed-changed and renamed the same way the beside-the-source mode does.  It
    lands where the archive itself sits inside the song, so an archive in a
    subfolder keeps that subfolder.
    """
    archive, selection = job
    destination_root = Path(folder["destination"])
    song_root = Path(folder["source"])
    reserve = _unique_path_for(
        folder.setdefault("taken", set(folder.get("marked_names") or ())),
        destination_root, patterns["file_marker"],
    )
    destination = reserve(_song_item_path(
        archive, selection, song_root, destination_root
    ))
    destination.parent.mkdir(parents=True, exist_ok=True)
    entry = _write_speed_changed_archive(
        folder["index"],
        archive,
        destination,
        speed,
        file_marker=patterns["file_marker"],
        difficulty_marker=patterns["difficulty_marker"],
        registry=registry,
        emit_event=emit_event,
        cancel_event=cancel_event,
    )
    entry["folder"] = str(destination_root)
    entry["charts"] = [str(destination)]
    # The source archive never travels along: its marked rewrite is the result,
    # and copying the original too would ship two copies of the same pack.
    entry["_handled"] = {archive}
    return entry


def _source_names(group, song_root):
    """Return the paths, relative to the song root, that the source holds.

    Only the names that already carry the marker matter: they are the results of
    an earlier speed change, and a new output must not land on one.  Unmarked
    names are not reserved -- the tree copy brings them over as they are.
    """
    names = set()
    song_root = Path(song_root)
    for value in [*group["selected"], *group["folders"]]:
        folder = Path(value)
        if not folder.is_dir():
            continue
        for item in folder.rglob("*"):
            if not item.is_file():
                continue
            try:
                names.add(str(item.relative_to(song_root)).casefold())
            except ValueError:
                names.add(item.name.casefold())
    return names


def _change_speed_to_collection(
    sources,
    speed,
    output_folder,
    *,
    file_pattern,
    difficulty_pattern,
    difficulty_marker,
    keep_pitch,
    folders_selected=None,
    item_owners=None,
    source_folder=None,
    folder_outputs=None,
    output_directory=None,
    allow_overwrite=True,
    emit_event,
    cancel_event,
    max_workers=None,
):
    """Transform complete folder copies and optional single-file collections.

    Selected folders keep their entire tree, marking chart and music names.
    Direct files can share a song collection or retain their normal per-file outputs.
    All folder tracks use one pool, sized by work rather than folder count;
    charts are written only after their audio tasks have finished.
    """
    patterns = {
        "difficulty_marker": (
            format_difficulty_marker(difficulty_pattern, speed)
            if difficulty_marker and str(difficulty_pattern or "").strip()
            else ""
        ),
        "file_marker": format_file_marker(file_pattern, speed),
    }
    root, layout, owners, loose = _collection_layout(
        sources, output_folder, folders_selected, source_folder, folder_outputs
    )
    options = {
        "file_pattern": file_pattern,
        "difficulty_pattern": difficulty_pattern,
        "output_directory": str(output_directory) if output_directory else None,
        "difficulty_marker": bool(difficulty_marker),
        "keep_pitch": bool(keep_pitch),
        "allow_overwrite": bool(allow_overwrite),
        "folders_selected": tuple(folders_selected or ()),
    }
    plain_outputs = _validate_plain_outputs(loose, speed, options, input_sources=sources)
    if any(
        destination == Path(value) or destination in Path(value).parents
        for _group, destination in layout for value in plain_outputs
    ):
        raise SpeedChangeError("单文件输出不能位于另一个变速结果文件夹内")
    folders = []
    owners = {**(item_owners or {}), **owners}
    for index, (group, destination) in enumerate(layout):
        song_root = Path(group["parent"])
        folder_source = group.get("source_folder")
        folder_patterns = dict(patterns)
        if folder_source:
            if difficulty_marker and not folder_patterns["difficulty_marker"]:
                folder_patterns["difficulty_marker"] = format_difficulty_marker(file_pattern, speed)
        folders.append({
            "index": index,
            "name": group["name"],
            "source": str(song_root),
            "sources": _copy_roots(group),
            "destination": destination,
            "charts": [item for item, _selection in group["charts"]],
            "archives": list(group["archives"]),
            "owners": owners,
            "source_folder": folder_source,
            "patterns": folder_patterns,
            "folders_selected": (folder_source,) if folder_source else tuple(folders_selected or ()),
            "marked_names": [
                name for name in _source_names(group, song_root)
                if folder_patterns["file_marker"] in Path(name).stem
            ] if folder_patterns["file_marker"] else [],
        })
    _check_cancelled(cancel_event)
    claimed = []
    try:
        for folder in folders:
            if folder["source_folder"]:
                folder["destination"].mkdir(parents=True, exist_ok=False)
                claimed.append(folder["destination"])
        if root and any(not folder["source_folder"] for folder in folders):
            root.mkdir(parents=True, exist_ok=True)
    except OSError:
        for destination in reversed(claimed):
            try:
                destination.rmdir()
            except OSError:
                pass
        raise
    registry = _AudioJobRegistry(find_ffmpeg(), speed, cancel_event, keep_pitch, allow_overwrite)
    # Concurrency follows the amount of *work*, not the number of selections: a
    # single chosen folder may hold twenty songs, and sizing the pool by the
    # folder count left all twenty tracks transcoding one after another.
    audio_total = sum(len(folder["charts"]) for folder in folders) + sum(
        len(folder["archives"]) for folder in folders
    )
    workers = max_workers or _worker_count(max(len(folders) + len(loose), audio_total + len(loose)))
    results = [None] * len(folders)
    plain_results = [None] * len(loose)
    total = max(1, len(folders) + len(loose))
    counter = {"done": 0}
    lock = threading.Lock()
    started = time.perf_counter()
    batches = {}
    plans_by_folder = {}

    def report_finished(label):
        with lock:
            counter["done"] += 1
            done = counter["done"]
        _log(emit_event, f"[{done}/{total}] {label} 变速完成")
        _emit(emit_event, "progress", {
            "current": done, "total": total, "fraction": done / total,
        })

    def run_plain(index, source):
        _check_cancelled(cancel_event)
        job = _archive_job if is_archive(source) else _plain_chart_job
        return job(index + len(folders), source, speed, options, registry, emit_event, cancel_event)

    def run_one(index, folder):
        _check_cancelled(cancel_event)
        patterns = folder["patterns"]
        # An archive is materialised in one step, so it has no queued audio to
        # hand to the shared pool.  A song holding both archives and plain charts
        # gets both: the archives are written, then the charts are read and their
        # tracks queued, and the tree is copied once afterwards.
        written = [
            _archive_folder_job(
                folder, job, speed, patterns, registry, cancel_event, emit_event
            )
            for job in folder["archives"]
        ]
        charts = folder["charts"]
        if charts:
            # Phase one only: the charts are read and the tracks are queued, but
            # the audio stays pending so every folder's jobs share one pool.
            entry, audio_jobs, plans = _collection_chart_job(
                folder, charts, speed, patterns, registry, cancel_event,
                emit_event, finalize=False,
            )
        else:
            # Nothing but archives: they are already written, so there is no
            # chart phase and the tree still has to be copied afterwards.
            entry = _collection_entry(folder)
            audio_jobs, plans = None, None
        entry["details"] = _written_details(written) + entry["details"]
        entry["warnings"] = _written_warnings(written) + entry["warnings"]
        entry["charts"] = [
            target for item in written for target in item["charts"]
        ] + entry["charts"]
        for item in written:
            entry["_handled"] = entry["_handled"] | item.get("_handled", set())
        entry["_produced"] = entry.get("_produced", []) + [
            str(target) for item in written for target in item["charts"]
        ]
        return entry, audio_jobs, plans

    with ThreadPoolExecutor(max_workers=workers) as pool:
        planning = {
            pool.submit(run_one, index, folder): ("folder", index)
            for index, folder in enumerate(folders)
        }
        planning.update({
            pool.submit(run_plain, index, source): ("file", index)
            for index, source in enumerate(loose)
        })
        for future in as_completed(planning):
            kind, index = planning[future]
            if kind == "file":
                try:
                    plain_results[index] = future.result()
                except SpeedCancelledError:
                    for item in planning:
                        item.cancel()
                    raise
                except Exception as exc:
                    plain_results[index] = {
                        "index": index + len(folders), "source": str(loose[index]),
                        "outputs": [], "outputs_detail": [], "error": str(exc),
                    }
                    _log_failure(emit_event, f"{loose[index].name} 变速失败：{exc}")
                report_finished(loose[index].name)
                continue
            label = folders[index]["name"]
            plans = None
            try:
                entry, audio_jobs, plans = future.result()
            except SpeedCancelledError:
                for item in planning:
                    item.cancel()
                raise
            except Exception as exc:
                entry = {
                    "folder": str(folders[index]["destination"]),
                    "charts": [],
                    "details": [],
                    "assets": [],
                    "warnings": [],
                    "copied": 0,
                    "error": str(exc),
                }
                audio_jobs = []
                _emit(emit_event, "warning", f"{label} 变速失败：{exc}")
            results[index] = entry
            plans_by_folder[index] = plans
            # Every folder's tracks join one pool, so a single folder holding
            # many songs is scheduled exactly like many folders holding one
            # each.  The pool is never blocked waiting for its own jobs: the
            # audio futures are collected after every planning job is in flight.
            batches[index] = [
                pool.submit(
                    _run_audio_job_quiet, job, registry, entry, cancel_event,
                    emit_event,
                )
                for job in (audio_jobs or [])
            ]
        audio_done = [future for group in batches.values() for future in group]
        for future in as_completed(audio_done):
            try:
                future.result()
            except SpeedCancelledError:
                raise
            except Exception as exc:
                _emit(emit_event, "warning", f"音乐转码任务异常：{exc}")
        # Charts, then everything else: rendering only now means a chart can be
        # rewired to a track that really exists.
        for index in range(len(folders)):
            if results[index] is None:
                continue
            _finish_collection_folder(
                index, folders, results, plans_by_folder.get(index), speed,
                folders[index]["patterns"], emit_event,
            )
            report_finished(folders[index]["name"])
    elapsed = (time.perf_counter() - started) * 1000.0
    _emit(emit_event, "timing", {"phase": "speed_change", "elapsed_ms": elapsed})
    for result in results:
        if result is None or result.get("error"):
            # A failed folder reports its own reason; do not bury it under
            # warnings about assets that were never reached.
            continue
        for asset in result.get("assets") or ():
            # Only ``.osu``'s own ``[Events]`` commands are scaled; a standalone
            # ``.osb`` needs the full storyboard grammar, so it is reported.
            _emit(emit_event, "warning", STORYBOARD_WARNING.format(name=asset["name"]))
    entries = [
        {
            "index": index,
            "source": str(folders[index]["source"]),
            "outputs": [(result or {}).get("folder") or str(folders[index]["destination"])],
            "outputs_detail": (result or {}).get("details") or [],
            "assets": (result or {}).get("assets") or [],
            "warnings": (result or {}).get("warnings") or [],
            "error": (result or {}).get("error"),
        }
        for index, result in enumerate(results)
    ]
    entries.extend(entry for entry in plain_results if entry is not None)
    failures = [entry for entry in entries if entry["error"]]
    return {
        "entries": entries,
        "failures": failures,
        "elapsed_ms": elapsed,
        "workers": workers,
        "output_folder": str(root) if root else None,
    }


def _validate_plain_outputs(sources, speed, options, *, input_sources=None):
    """Reject collisions before either the file or folder batch writes."""
    input_sources = sources if input_sources is None else input_sources
    planned = {}
    marker = format_file_marker(options["file_pattern"], speed)
    for source in sources:
        if is_archive(source):
            files = [(_output_directory(options, source) / (
                f"{source.stem}{marker}{source.suffix}"
            ), source)]
        else:
            chart, audio, audio_inputs = _plain_chart_outputs(
                source, speed, options["file_pattern"], _output_directory(options, source), with_sources=True
            )
            files = [(chart, source), *zip(audio, audio_inputs)]
        for destination, original in files:
            key = str(destination.resolve()).casefold()
            if destination.resolve() == original.resolve() or destination.resolve() in input_sources:
                raise SpeedChangeError(f"输出不能覆盖原始输入：{destination}")
            if key in planned and planned[key] != original:
                raise SpeedChangeError(f"多个输入使用同一个输出名称：{destination}；请选择收集到独立目录")
            planned[key] = original
            if destination.exists() and not options.get("allow_overwrite", True):
                raise SpeedChangeError(f"输出已存在，请重新提交并确认覆盖：{destination}")

    return planned


def change_speed(
    input_paths,
    speed,
    *,
    file_pattern,
    difficulty_pattern,
    output_directory=None,
    output_folder=None,
    folders_selected=None,
    item_owners=None,
    source_folder=None,
    folder_outputs=None,
    difficulty_marker=True,
    keep_pitch=False,
    emit_event=None,
    cancel_event=None,
    max_workers=None,
    allow_overwrite=True,
):
    """Speed-change charts and audio without modifying the inputs.

    By default files gain marked names beside their sources. The optional
    output_directory relocates them. output_folder collects directly selected
    files into song folders. folder_outputs maps each complete folder selection
    to its exact new destination, preserving structure and marking chart/music names.
    source_folder is the single-folder form with an exact output_folder.
    """
    speed = normalized_speed(speed)
    sources = [_resolve_input(value) for value in input_paths]
    if output_folder or folder_outputs:
        return _change_speed_to_collection(
            sources,
            speed,
            output_folder,
            file_pattern=file_pattern,
            difficulty_pattern=difficulty_pattern,
            difficulty_marker=difficulty_marker,
            keep_pitch=keep_pitch,
            folders_selected=folders_selected,
            item_owners=item_owners,
            source_folder=source_folder,
            folder_outputs=folder_outputs,
            output_directory=output_directory,
            allow_overwrite=allow_overwrite,
            emit_event=emit_event,
            cancel_event=cancel_event,
            max_workers=max_workers,
        )
    options = {
        "file_pattern": file_pattern,
        "difficulty_pattern": difficulty_pattern,
        "output_directory": str(output_directory) if output_directory else None,
        "difficulty_marker": bool(difficulty_marker),
        "keep_pitch": bool(keep_pitch),
        "allow_overwrite": bool(allow_overwrite),
        # The folders the user selected decide which charts count as part of a
        # collection: only those mark the song name.
        "folders_selected": tuple(folders_selected or ()),
    }
    registry = _AudioJobRegistry(find_ffmpeg(), speed, cancel_event, keep_pitch, allow_overwrite)
    workers = max_workers or _worker_count(len(sources))
    results = [None] * len(sources)
    total = max(1, len(sources))
    counter = {"done": 0}
    lock = threading.Lock()
    started = time.perf_counter()

    _validate_plain_outputs(sources, speed, options)

    def run_one(index, source):
        _check_cancelled(cancel_event)
        if is_archive(source):
            return _archive_job(index, source, speed, options, registry, emit_event, cancel_event)
        return _plain_chart_job(index, source, speed, options, registry, emit_event, cancel_event)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(run_one, index, source)
            for index, source in enumerate(sources)
        ]
        for index, future in enumerate(futures):
            try:
                results[index] = future.result()
            except SpeedCancelledError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:
                results[index] = {
                    "index": index,
                    "source": str(sources[index]),
                    "outputs": [],
                    "outputs_detail": [],
                    "error": str(exc),
                }
                _log_failure(emit_event, f"{sources[index].name} 变速失败：{exc}")
            with lock:
                counter["done"] += 1
                done = counter["done"]
            _log(emit_event, f"[{done}/{total}] {sources[index].name} 变速完成")
            _emit(emit_event, "progress", {
                "current": done,
                "total": total,
                "fraction": done / total,
            })
    elapsed = (time.perf_counter() - started) * 1000.0
    _emit(emit_event, "timing", {"phase": "speed_change", "elapsed_ms": elapsed})
    finished = [entry for entry in results if entry is not None]
    failures = [entry for entry in finished if entry["error"]]
    return {
        "entries": finished,
        "failures": failures,
        "elapsed_ms": elapsed,
        "workers": workers,
    }


__all__ = (
    "ARCHIVE_EXTENSIONS",
    "AUDIO_EXTENSIONS",
    "CHART_EXTENSIONS",
    "MAX_SPEED",
    "MIN_SPEED",
    "STORYBOARD_EXTENSIONS",
    "STORYBOARD_WARNING",
    "SpeedCancelledError",
    "SpeedChangeError",
    "change_speed",
    "find_ffmpeg",
    "is_archive",
    "is_audio",
    "is_chart",
    "is_storyboard",
    "companion_storyboards",
    "normalized_speed",
    "plan_speed_change",
    "probe_audio",
    "resample_audio",
    "transform_chart_text",
    "transform_mc_text",
    "transform_osu_text",
    "transform_sm_text",
)
