"""Registered speed-change tool.

Changes the speed of charts and their music together.  Archive inputs produce
one new archive holding every speed-changed chart plus its music; plain charts
produce a new chart file and a new music file next to the source.
"""

import json
import os
from pathlib import Path
import re
import time

from osu_toolbox.schema import (
    ChoiceSpec,
    GroupSpec,
    ParamSpec,
    ParamValidationError,
    coerce_params,
)
from osu_toolbox.speed_change import (
    MAX_SPEED,
    MIN_SPEED,
    STORYBOARD_WARNING,
    SpeedCancelledError,
    SpeedChangeError,
    _song_groups,
    change_speed,
    normalized_speed,
    plan_speed_change,
)
from osu_toolbox.speed_settings import (
    DEFAULT_SPEED_FILE_SUFFIX,
    normalize_speed_settings,
    format_file_marker,
)

from .base import (
    ConfirmationRequest,
    PrefillResult,
    PreparedRun,
    ToolCancelledError,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)


SUPPORTED_SPEED_INPUT_EXTENSIONS = (".osu", ".mc", ".sm", ".osz", ".mcz")

OUTPUT_LOCATION_CHOICES = (
    ChoiceSpec("source", "输入文件所在目录", "source"),
    ChoiceSpec("workdir", "程序工作目录", "workdir"),
)

SPEED_CHANGE_GROUPS = (
    GroupSpec("input", "输入（至少填一项）"),
    # The two groups below carry defaults that suit most runs, so they collapse:
    # the form opens showing only what the user must fill in.
    GroupSpec("speed", "变速设置（可选，留空用默认值）", collapsible=True),
    GroupSpec("output", "输出设置（可选，留空用默认值）", collapsible=True),
)

SPEED_CHANGE_PARAMS = (
    ParamSpec(
        "input_paths", "谱面或归档文件", "path", None, "input",
        "支持 .osu、.mc、.sm、.osz、.mcz，可多选；与“文件夹”至少填一个",
        persist=False, dialog_mode="open_file",
        filters=(("谱面或归档", "*.osu;*.mc;*.sm;*.osz;*.mcz"),), multiple=True,
        placeholder="可多选；与下面的文件夹至少填一个",
    ),
    ParamSpec(
        "input_folders", "文件夹", "path", None, "input",
        "另存完整副本，内部谱面和音乐文件名追加变速标识；可多选，与文件输入合并，至少填一个",
        persist=False, dialog_mode="folder", multiple=True,
        placeholder="可多选；为每个文件夹生成完整变速副本",
    ),
    ParamSpec(
        "speed", "变速倍率", "float", 1.5, "speed",
        "大于 1 加速，小于 1 减速；变速后谱面与音乐严格同比缩放",
        unit="倍",
        minimum=MIN_SPEED,
        maximum=MAX_SPEED,
    ),
    ParamSpec(
        "keep_pitch", "保持音调", "bool", False, "speed",
        "开启时保持原音调；关闭时音调随速度变化",
    ),
    ParamSpec(
        "file_suffix", "文件名标识", "text", None, "speed",
        "追加到文件或文件夹名，{num} 表示倍率；留空使用默认值",
        persist=False,
        placeholder="{num}x（留空则用设置里的默认值）",
    ),
    ParamSpec(
        "difficulty_marker_pattern", "难度名标识", "text", None, "speed",
        "{num} 表示倍率；单谱面填 none 关闭，文件夹和归档改用文件名标识",
        persist=False,
        placeholder="{num}x（单谱面填 none 表示不加）",
    ),
    ParamSpec(
        "output_location", "输出位置", "choice", "source", "output",
        "单文件放在原文件旁，文件夹副本放在原文件夹旁；也可统一到程序目录",
        choices=OUTPUT_LOCATION_CHOICES,
    ),
    ParamSpec(
        "separate_folder", "指定输出目录", "bool", False, "output",
        "使用下方目录存放结果；文件夹始终生成完整副本",
    ),
    ParamSpec(
        "output_folder", "输出目录", "path", None, "output",
        "启用“指定输出目录”后必填",
        persist=False, dialog_mode="folder",
        placeholder="开启上面的开关后必填",
    ),
)

RESULT_COLUMNS = ("输入文件", "变速倍率", "输出文件")

# Writing this in a marker field switches that marker off for one run.
SUPPRESS_MARKER = "none"


def _path_values(value):
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace("\r", "").split("\n")
    result = []
    for item in values:
        path = str(item or "").strip().strip('"')
        if path and path not in result:
            result.append(path)
    return result


def _resolve_user_path(value):
    """Turn a user-supplied path into an absolute one without corrupting it.

    ``os.path.expandvars`` is destructive for real paths: a folder named
    ``100%TEMP%`` becomes ``100C:\\Users\\...\\Temp``.  ``os.path.expanduser``
    has the same problem for a name containing ``~``.  Environment variables are
    convenient when typed by hand, so expansion is a *fallback*: the literal
    path wins whenever it already exists.  The shell entry point applies no
    expansion at all for the same reason (see ``quick._absolute_input``).
    """
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    literal = os.path.abspath(text)
    if os.path.exists(literal):
        return literal
    expanded = os.path.abspath(
        os.path.expandvars(os.path.expanduser(text))
    )
    return expanded


def _collect_input_values(raw_params):
    """Merge the file field and the folder field into one ``(submitted, path, field)`` list.

    Windows' file dialog cannot select files and folders at the same time, so
    the form carries two fields; the user may fill either or both.  The owning
    field travels with each value so a validation error can point at the box the
    user actually filled in.  Values are de-duplicated case-insensitively, so
    naming a folder and a chart inside it is harmless.
    """
    merged = []
    seen = set()
    for key in ("input_paths", "input_folders"):
        raw = raw_params.get(key)
        # A multi-select result must arrive as separate values.  If a caller
        # hands over the list itself or a JSON string of one, flatten it here
        # rather than letting a naive ``str()`` glue the entries together --
        # ``str(["a", "b"])`` once turned five chosen folders into the single
        # nonsense path ``aD:\\bD:\\c``, and every one of them was lost.
        values = _flatten_path_values(raw)
        for value in values:
            resolved = _resolve_user_path(value)
            if not resolved:
                continue
            marker = os.path.normcase(resolved)
            if marker in seen:
                continue
            seen.add(marker)
            # A folder input carries itself as the selection root: the result
            # folders are the songs inside it.  A chart named directly carries
            # no root, because collection mode must collect it as its own song
            # folder rather than merging it into whatever folder it sits in.
            root = resolved if os.path.isdir(resolved) else None
            merged.append((value, resolved, key, root))
    return merged


def _flatten_path_values(raw):
    """Normalise one field's value into a list of path strings."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        result = []
        for item in raw:
            result.extend(_flatten_path_values(item))
        return result
    text = str(raw).strip()
    if not text:
        return []
    # A JSON array arrives when a caller passes the picker result straight
    # through; accept it so the entries stay separate.
    if text.startswith("[") and text.endswith("]"):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            return _flatten_path_values(decoded)
    values = _path_values(text)
    # Last line of defence: a client that glued a multi-select result into one
    # string.  Recovering here keeps a five-folder selection from being thrown
    # away over a UI bug that has already been fixed.
    recovered = []
    for value in values:
        parts = _split_concatenated_paths(value)
        recovered.extend(parts if parts else [value])
    return recovered


FIELD_LABELS = {
    "input_paths": "谱面或归档文件",
    "input_folders": "文件夹",
}


def _describe_value(value):
    """Render a submitted path for a diagnostic message.

    The exact string matters when a user reports "路径不存在": a trailing space,
    a stray quote or a second line pasted into the box all look identical in the
    form but are different values here.
    """
    text = str(value)
    label = f"共 {len(text)} 个字符"
    if text != text.strip():
        label += "，首尾有空白"
    return f"{text!r}（{label}）"


def _looks_like_concatenated_paths(path):
    """Return whether one string looks like several absolute paths glued together.

    ``D:\\maps\\aD:\\maps\\b`` has a second drive/UNC prefix in the middle, which
    no real single path contains.
    """
    text = str(path)
    inner = text[2:] if len(text) > 2 and text[1] == ":" else text
    return bool(re.search(r"[A-Za-z]:[\\/]", inner) or "\\\\" in inner[1:])


def _split_concatenated_paths(path):
    """Split a glued string back into the paths it was made of, or return ``None``.

    A multi-select result once reached the backend as one string with no
    separators, so every chosen folder was lost.  When each piece of such a
    string exists as a folder or file the split is certainly the intended
    reading, so it is recovered instead of failing the whole run; otherwise the
    caller reports the problem.
    """
    text = str(path)
    prefix = r"[A-Za-z]:[\\/]"
    if text.startswith("\\\\"):
        starts = [match.start() for match in re.finditer(prefix, text) if match.start() > 1]
    else:
        starts = [match.start() for match in re.finditer(prefix, text) if match.start() > 0]
    if not starts:
        return None
    boundaries = [0, *starts, len(text)]
    parts = [
        text[boundaries[index]:boundaries[index + 1]]
        for index in range(len(boundaries) - 1)
    ]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return None
    if not all(os.path.isdir(part) or os.path.isfile(part) for part in parts):
        return None
    return parts


def _expand_input_values(values):
    """Expand files for preflight while retaining each selected folder root.

    Archives participate in the same run as plain charts. Files reached through
    overlapping selections are handled once, under the outermost chosen folder.
    """
    normalized = []
    seen = set()
    folders = []

    def add(path):
        marker = os.path.normcase(os.path.abspath(path))
        if marker in seen:
            return
        seen.add(marker)
        normalized.append(path)

    def fail(field, message):
        raise ParamValidationError(field, f"{FIELD_LABELS.get(field, field)}：{message}")

    for original, path, field, root in values:
        path = os.path.abspath(path)
        if os.path.isdir(path):
            folder = Path(path)
            found = sorted(
                item
                for item in folder.rglob("*")
                if item.is_file()
                and item.suffix.lower() in SUPPORTED_SPEED_INPUT_EXTENSIONS
            )
            if not found:
                fail(field, f"文件夹内没有可变速的谱面或归档：{path}")
            if str(folder) not in folders:
                folders.append(str(folder))
            for item in found:
                add(str(item))
            continue
        if not os.path.isfile(path):
            # Report what was actually submitted: the parsed path can differ
            # from the typed text, and that difference is the whole diagnosis.
            hint = (
                "请用「浏览」重新选择文件夹"
                if field == "input_folders"
                else "请确认文件路径，或改填到「文件夹」字段"
            )
            detail = f"收到的值：{_describe_value(original)}"
            if len(values) == 1 and _looks_like_concatenated_paths(path):
                # Several picks glued into one string: every one of them would
                # otherwise be silently dropped, so say what happened.
                detail += (
                    "；这看起来是多个路径被拼接成了一个（多选结果没有分开保存），"
                    "请重新用「浏览」选择"
                )
            fail(field, f"路径不存在或不是可读取的文件/文件夹：{path}；{hint}。{detail}")
        if Path(path).suffix.lower() not in SUPPORTED_SPEED_INPUT_EXTENSIONS:
            fail(
                field,
                f"不支持的输入格式：{Path(path).suffix}；"
                "支持 .osu、.mc、.sm、.osz、.mcz",
            )
        add(path)
    return normalized, folders


def _naming_patterns(raw_params, context):
    """Return the marker patterns this run uses.

    The 变速工具 settings provide the defaults; the two text fields in the tool
    form override them for this run only and are never written back.  A blank
    field falls back to the setting, so clearing the box restores the default.
    To switch a marker off in one run, write the literal ``none`` in the field
    -- the same word the 右键菜单 submenu entries use.
    """
    settings = _speed_tool_settings(context)
    configured_file = settings.get("speed_file_suffix") or DEFAULT_SPEED_FILE_SUFFIX
    configured_difficulty = settings.get("speed_difficulty_marker") or ""

    def override(key, fallback, *, required):
        if key not in (raw_params or {}):
            return fallback
        text = str(raw_params.get(key) or "")
        if not text.strip():
            return fallback
        if text.strip().casefold() == SUPPRESS_MARKER:
            if required:
                raise ParamValidationError(
                    key, "文件名标识不能为空，否则输出会覆盖原文件"
                )
            return ""
        return text

    return (
        override("file_suffix", configured_file, required=True),
        override("difficulty_marker_pattern", configured_difficulty, required=False),
    )


def _speed_tool_settings(context):
    raw = getattr(context, "global_settings", None) or {}
    return normalize_speed_settings(raw)


def _folder_output_path(source, parent, pattern, speed, reserved):
    source = Path(source).resolve()
    parent = Path(parent).resolve()
    if not source.name:
        raise ParamValidationError("input_folders", "请选择具体文件夹，不能直接选择磁盘根目录")
    if parent == source or source in parent.parents:
        raise ParamValidationError("output_folder", "输出目录必须位于原文件夹之外")
    marker = format_file_marker(pattern, speed).rstrip(" .")
    counter = 1
    while True:
        name = source.name if counter == 1 else f"{source.name} {counter}"
        candidate = parent / f"{name}{marker}"
        key = str(candidate).casefold()
        if not candidate.exists() and key not in reserved:
            reserved.add(key)
            return str(candidate)
        counter += 1


class SpeedChangeController:
    def prefill(self, raw_params, changed_key, context):
        return PrefillResult({})

    def prepare(self, raw_params, context):
        raw_params = dict(raw_params or {})
        for key in ("input_paths", "input_folders"):
            if isinstance(raw_params.get(key), (list, tuple)):
                raw_params[key] = "\n".join(str(item) for item in raw_params[key])
        params = coerce_params(SPEED_CHANGE_PARAMS, raw_params)
        try:
            params["speed"] = normalized_speed(params.get("speed"))
        except SpeedChangeError as exc:
            raise ParamValidationError("speed", str(exc))
        # The form separates files from folders because Windows' file dialog
        # cannot offer both; the two fields are one input list here.
        input_values = _collect_input_values(raw_params)
        if not input_values:
            raise ParamValidationError(
                "input_paths", "请选择至少一个谱面、归档或文件夹"
            )
        separate_folder = bool(params.get("separate_folder"))
        # A folder means "everything inside it" in either output mode; the mode
        # only decides where the results land.  Expanding here (rather than
        # passing the folder through) also lets the planner show the real file
        # list before anything is written.
        normalized, folders_selected = _expand_input_values(input_values)
        params["input_paths"] = normalized
        params["folders_selected"] = folders_selected
        if separate_folder:
            folder = params.get("output_folder")
            if not folder:
                raise ParamValidationError(
                    "output_folder", "请选择输出目录"
                )
            folder = os.path.abspath(
                os.path.expandvars(os.path.expanduser(str(folder)))
            )
            if os.path.isfile(folder):
                raise ParamValidationError(
                    "output_folder", f"输出目录不能是文件：{folder}"
                )
            params["output_folder"] = folder
        else:
            params["output_folder"] = None
        # Files land beside their sources; folder copies land beside the
        # selected folder, unless a shared destination was chosen.
        output_directory = (
            context.output_root if params["output_location"] == "workdir" else None
        )
        params["output_directory"] = (
            os.path.abspath(output_directory) if output_directory else None
        )
        file_pattern, difficulty_pattern = _naming_patterns(raw_params, context)
        params["file_pattern"] = file_pattern
        params["difficulty_pattern"] = difficulty_pattern
        selected = list(dict.fromkeys(Path(folder).resolve() for folder in folders_selected))
        selected = [
            folder for folder in selected
            if not any(other in folder.parents for other in selected)
        ]
        folders_selected = [str(folder) for folder in selected]
        params["folders_selected"] = folders_selected
        if context.invocation == "shell" and folders_selected and len(input_values) != 1:
            raise ParamValidationError("input_folders", "右键变速每次请选择一个文件夹")
        folder_outputs, reserved = {}, set()
        for source in selected:
            parent = params["output_folder"] or params["output_directory"] or source.parent
            parent = Path(parent).resolve()
            if any(parent == folder or folder in parent.parents for folder in selected):
                raise ParamValidationError("output_folder", "输出目录必须位于原文件夹之外")
            folder_outputs[str(source)] = _folder_output_path(
                source, parent, file_pattern, params["speed"], reserved
            )
        params["folder_outputs"] = folder_outputs
        keep_pitch = bool(params.get("keep_pitch"))
        if "keep_pitch" not in raw_params:
            # The 变速工具 setting is the default; the per-run checkbox overrides
            # it when the caller supplied one.
            keep_pitch = bool(_speed_tool_settings(context).get("speed_keep_pitch"))
        params["keep_pitch"] = keep_pitch
        # Which song folder each chart belongs to, and where inside it the chart
        # sits, is decided here: only the tool knows which folders the user
        # actually selected.  The plan and the run share this one answer, so the
        # preview can never disagree with the written result.
        item_owners = {}
        if params["output_folder"]:
            loose = [
                value for value in normalized
                if not any(folder in Path(value).resolve().parents for folder in selected)
            ]
            _collection, item_owners = _song_groups(loose, ())
        params["item_owners"] = item_owners
        try:
            plan = plan_speed_change(
                normalized,
                params["speed"],
                file_pattern=file_pattern,
                difficulty_pattern=difficulty_pattern,
                output_directory=params["output_directory"],
                output_folder=params["output_folder"],
                folders=folders_selected,
                folder_outputs=folder_outputs,
            )
        except (OSError, ValueError, SpeedChangeError) as exc:
            raise ParamValidationError("input_paths", str(exc))
        params["plan"] = plan
        planned = [path for item in plan for path in item["outputs"]]
        params["planned_outputs"] = planned
        storyboards = [
            asset["name"]
            for item in plan
            for asset in item.get("assets") or ()
            if asset.get("kind") == "storyboard"
        ]
        warnings = tuple(dict.fromkeys(
            STORYBOARD_WARNING.format(name=name) for name in storyboards
        ))
        # Say what the run resolved to before anything is written: a folder
        # input turning into N charts is the single most useful fact when the
        # result is not what the user expected.
        summary = f"输入解析：{len(input_values)} 项 → {len(normalized)} 个谱面/归档"
        if folder_outputs:
            summary += f"；新建 {len(folder_outputs)} 个变速文件夹"
        if params["output_folder"]:
            summary += f"；输出目录 {params['output_folder']}"
        warnings = (summary, *warnings)
        params["planned_files"] = list(dict.fromkeys([
            path for item in plan for path in [*item["outputs"], *item.get("audio_outputs", ())]
        ]))
        existing = [path for path in params["planned_files"] if os.path.exists(path)]
        confirmation = None
        if existing:
            confirmation = ConfirmationRequest(
                "确认覆盖输出",
                "覆盖以下文件？\n" + "\n".join(existing),
                "覆盖",
            )
        return PreparedRun(params, warnings=warnings, confirmation=confirmation)

    def run(self, prepared, emit_event, cancel_event=None):
        params = prepared.params
        if cancel_event is not None and cancel_event.is_set():
            raise ToolCancelledError("变速任务已取消")
        started = time.perf_counter()

        def emit(kind, payload):
            emit_event(kind, payload)
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelledError("变速任务已取消")

        try:
            outcome = change_speed(
                params["input_paths"],
                params["speed"],
                file_pattern=params["file_pattern"],
                difficulty_pattern=params["difficulty_pattern"],
                output_directory=params["output_directory"],
                output_folder=params["output_folder"],
                folders_selected=params.get("folders_selected"),
                item_owners=params.get("item_owners"),
                folder_outputs=params.get("folder_outputs"),
                keep_pitch=bool(params.get("keep_pitch")),
                allow_overwrite=bool(params.get("_allow_overwrite")),
                emit_event=emit,
                cancel_event=cancel_event,
            )
        except SpeedCancelledError:
            raise ToolCancelledError("变速任务已取消")
        except SpeedChangeError as exc:
            raise ValueError(str(exc))
        entries = outcome["entries"]
        failures = outcome["failures"]
        done = [entry for entry in entries if not entry["error"]]
        partial = [entry for entry in done if entry.get("warnings")]
        for entry in failures:
            # A failed input must never be silent: without this the run looks
            # like a success that produced no files, and the user is left with
            # no explanation for the missing output.
            emit_event(
                "log",
                f"错误：{Path(entry['source']).name} 变速失败：{entry['error']}",
            )
        for entry in partial:
            # Each problem was already logged with its reason at the moment it
            # happened, so this is only the roll-up that tells the user the run
            # finished with problems rather than burying it among the successes.
            for message in dict.fromkeys(entry["warnings"]):
                emit_event("log", f"本次运行有未完成项：{message}")
        if not entries:
            raise ValueError("没有可变速的输入文件")
        if len(failures) == len(entries):
            raise ValueError(
                "所有输入都变速失败："
                + "；".join(
                    f"{Path(item['source']).name}：{item['error']}" for item in failures
                )
            )
        rows = []
        outputs = []
        speed_text = f"{params['speed']:g}"
        for entry in entries:
            targets = entry["outputs"] or ["（未生成）"]
            for target in targets:
                rows.append((Path(entry["source"]).name, speed_text, target))
            outputs.extend(entry["outputs"])
        if not outputs:
            raise ValueError(
                "变速未生成任何输出文件："
                + "；".join(
                    f"{Path(item['source']).name}：{item['error']}" for item in failures
                )
            )
        emit_event("timing", {
            "phase": "speed_change",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        emit_event(
            "log",
            f"变速完成：{len(entries) - len(failures)}/{len(entries)} 个输入，"
            f"{outcome['workers']} 个并发任务，耗时 {outcome['elapsed_ms'] / 1000.0:.2f} 秒",
        )
        if failures:
            emit_event(
                "warning",
                "；".join(f"{Path(item['source']).name}：{item['error']}" for item in failures),
            )
        if partial:
            emit_event(
                "warning",
                "；".join(
                    dict.fromkeys(
                        message
                        for entry in partial
                        for message in entry["warnings"]
                    )
                ),
            )
        storyboards = [
            asset["name"]
            for entry in entries
            for asset in entry.get("assets") or ()
            if asset.get("kind") == "storyboard"
        ]
        for name in dict.fromkeys(storyboards):
            # Only ``.osu``'s own ``[Events]`` commands are scaled; a standalone
            # ``.osb`` needs the full storyboard command grammar, so it is
            # reported instead of being silently left at the old tempo.
            emit_event("warning", STORYBOARD_WARNING.format(name=name))
        if len(outputs) != len(params["planned_outputs"]):
            # The caller validated a plan; a mismatch means a file appeared or
            # disappeared between planning and execution.
            emit_event("warning", "实际输出与预检计划不一致，请检查结果表格")
        return ToolResult("table", {
            "columns": RESULT_COLUMNS,
            "rows": rows,
            "paths": outputs,
            "warnings": list(dict.fromkeys(
                warning for entry in entries for warning in entry.get("warnings") or ()
            )),
            "errors": list(dict.fromkeys([
                *(f"{Path(entry['source']).name}：{entry['error']}" for entry in failures),
                *(message for entry in partial for message in entry["warnings"]),
            ])),
        })


SPEED_CHANGE_TOOL = ToolDefinition(
    spec=ToolSpec(
        id="speed-change",
        title="变速",
        description="同时调整谱面与音乐速度，支持文件、文件夹和归档",
        category="谱面工具",
        action_label="开始变速",
        groups=SPEED_CHANGE_GROUPS,
        params=SPEED_CHANGE_PARAMS,
        result_kind="table",
        concurrency="exclusive",
    ),
    controller=SpeedChangeController(),
)


__all__ = (
    "SPEED_CHANGE_GROUPS",
    "SPEED_CHANGE_PARAMS",
    "SPEED_CHANGE_TOOL",
    "SUPPORTED_SPEED_INPUT_EXTENSIONS",
    "SpeedChangeController",
)
