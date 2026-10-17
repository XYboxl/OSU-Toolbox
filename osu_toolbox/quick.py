"""Lightweight, GUI-free command dispatcher used by Explorer integrations.

Only the standard library is imported at module load time.  Replay parsing and
writing are imported inside the operation handlers so a cold shell invocation
does not initialize pywebview, pygame, OpenGL, NumPy or FFmpeg.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


CONTEXT_SHELL = "shell"
CONTEXT_QUICK = "quick"
SUPPORTED_CONTEXTS = (CONTEXT_SHELL, CONTEXT_QUICK)
OPERATION_CHART_CONVERT = "chart-convert"
OPERATION_FOLDER_CONVERT = "folder-convert"
OPERATION_ARCHIVE_CONVERT = "archive-convert"
OPERATION_SPEED_CHANGE = "speed-change"
OPERATION_ARCHIVE_SPEED_CHANGE = "archive-speed-change"
OPERATION_FOLDER_SPEED_CHANGE = "folder-speed-change"
SUPPORTED_OPERATIONS = (
    "replay-generate",
    "replay-edit",
    OPERATION_CHART_CONVERT,
    # The Explorer menu registers a distinct operation per input kind, so the
    # registry commands already in the wild use these names.  Each is a thin
    # dispatcher onto the same conversion core.
    OPERATION_FOLDER_CONVERT,
    OPERATION_ARCHIVE_CONVERT,
    # 变速 has one operation per input kind for the same reason: the archive
    # item carries its own (archive-shaped) submenu.
    OPERATION_SPEED_CHANGE,
    OPERATION_ARCHIVE_SPEED_CHANGE,
    OPERATION_FOLDER_SPEED_CHANGE,
)
# Folder conversion operations write archives; speed changes use their own dispatcher.
FOLDER_OPERATIONS = (OPERATION_CHART_CONVERT, OPERATION_FOLDER_CONVERT)
# Extensions that are archives rather than plain charts.
ARCHIVE_EXTENSIONS = (".osz", ".mcz")
# Plain charts and the archive each one is exported to when a folder is
# converted.  An archive never becomes a plain file: one archive would explode
# into one file per chart.  Which of the two archives a plain chart becomes is
# decided by the requested target, since both .osu and .sm can hold either.
PLAIN_CHART_EXTENSIONS = (".osu", ".mc", ".sm")
SPEED_CHANGE_EXTENSIONS = PLAIN_CHART_EXTENSIONS + ARCHIVE_EXTENSIONS
SPEED_CHANGE_OPERATIONS = (
    OPERATION_SPEED_CHANGE, OPERATION_ARCHIVE_SPEED_CHANGE, OPERATION_FOLDER_SPEED_CHANGE,
)


class QuickCommandError(ValueError):
    """A user-correctable error in a shell command invocation."""

    def __init__(self, message, code="invalid_input"):
        super().__init__(message)
        self.code = code


def build_parser():
    parser = argparse.ArgumentParser(
        prog="osu-toolbox-menu",
        description="OSU 工具箱轻量文件操作入口",
    )
    parser.add_argument(
        "--context",
        choices=SUPPORTED_CONTEXTS,
        default=CONTEXT_SHELL,
        help="调用上下文；右键菜单使用 shell",
    )
    parser.add_argument(
        "--operation",
        required=True,
        choices=SUPPORTED_OPERATIONS,
        help="要执行的文件操作",
    )
    parser.add_argument("path", metavar="PATH", help="谱面、归档、回放文件或文件夹")
    parser.add_argument(
        "-o", "--output", dest="output", default=None,
        help="输出文件；默认写入输入文件同目录",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="明确允许替换已存在的输出文件",
    )
    parser.add_argument(
        "--dry-run", "--no-write", action="store_true", dest="dry_run",
        help="只校验并生成计划，不写入文件",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")

    # Replay editing options. They are harmless for replay-generate and are
    # kept in one parser so the launcher has a stable protocol.
    parser.add_argument("--speed", type=float, default=None)
    parser.add_argument("--chart-path", "--chart", dest="chart_path", default=None)
    parser.add_argument("--chart-version", default=None)
    parser.add_argument("--music", default=None)
    parser.add_argument("--artist", default=None)
    for field in ("score", "max-combo", "best", "cool", "good", "miss", "judge", "playing-time"):
        parser.add_argument(f"--{field}", dest=field.replace("-", "_"), type=int, default=None)
    parser.add_argument(
        "--mod", action="append", dest="mods", default=None,
        help="MOD 名称，可重复或使用逗号分隔",
    )
    parser.add_argument("--clear-mod", action="store_true")
    parser.add_argument("--events-json", default=None)
    parser.add_argument("--events-file", default=None)
    # ``--target-format`` carries the speed multiplier for the 变速 operations,
    # so it accepts any text and each operation validates what it was given.
    parser.add_argument("--target-format", default=None, help="转谱目标格式或变速倍率")
    # 变速 arguments.
    parser.add_argument(
        "--prompt-speed", action="store_true", dest="prompt_speed",
        help="弹出输入框询问变速倍率（右键菜单的“自定义倍率”使用）",
    )
    parser.add_argument(
        "--speed-suffix", default=None,
        help="变速后的文件名标识，例如“_{num}x”；默认读取设置",
    )
    parser.add_argument(
        "--difficulty-marker", default=None,
        help="变速后的难度名标识；传空字符串或 none 表示不添加",
    )
    parser.add_argument(
        "--keep-pitch", action="store_true", dest="keep_pitch",
        help="变速时保持原音调（默认音调随速度变化）",
    )
    parser.add_argument(
        "--output-directory", default=None,
        help="变速输出目录；默认写入输入文件所在目录",
    )
    return parser


def _absolute_input(path, context):
    if not path or not str(path).strip():
        raise QuickCommandError("必须提供一个输入文件", "missing_input")
    raw_path = str(path).strip().strip('"')
    if context == CONTEXT_SHELL:
        if not os.path.isabs(raw_path):
            raise QuickCommandError("右键菜单输入必须是绝对路径", "relative_input")
        # Explorer already supplies the exact absolute path. Expanding
        # environment variables here would corrupt valid names containing %.
        value = os.path.abspath(raw_path)
    else:
        value = os.path.abspath(
            os.path.expandvars(os.path.expanduser(raw_path)),
        )
    if os.path.isdir(value):
        # The folder menu passes "%V", which is the folder itself.
        return value
    if not os.path.isfile(value):
        raise QuickCommandError(f"输入文件不存在：{value}", "input_missing")
    return value


def _output_path(value, source, edited=False):
    source = os.path.abspath(source)
    root = os.path.dirname(source)
    if value:
        output = os.path.expandvars(os.path.expanduser(str(value).strip().strip('"')))
        if not os.path.isabs(output):
            output = os.path.join(root, output)
    else:
        stem = os.path.splitext(os.path.basename(source))[0]
        output = os.path.join(root, f"{stem}{'-edited' if edited else ''}.mr")
    output = os.path.abspath(output)
    if os.path.splitext(output)[1].lower() != ".mr":
        output = os.path.splitext(output)[0] + ".mr"
    return output


def _check_output(output, force, dry_run=False):
    parent = os.path.dirname(output) or os.getcwd()
    if not os.path.isdir(parent):
        raise QuickCommandError(f"输出目录不存在：{parent}", "output_directory_missing")
    if os.path.exists(output) and not force and not dry_run:
        raise QuickCommandError(
            f"输出文件已存在：{output}；如需替换请显式使用 --force",
            "output_exists",
        )


def _load_replay_api():
    # This function is the only import boundary for the replay implementation.
    from .parsing.mr_file import MrReplay, generate_perfect_replay
    from .tools.replay_edit import ReplayEditController
    from .tools.replay_generate import ReplayGenerateController

    return (
        MrReplay,
        generate_perfect_replay,
        ReplayGenerateController,
        ReplayEditController,
    )


def _load_global_settings():
    """Read persisted global settings for a shell invocation.

    The GUI runtime normally owns the settings store; a right-click menu call
    has no GUI, so the same file is read directly.  A missing or damaged file
    simply falls back to defaults.  The schema sections (变速工具, 转谱工具)
    are normalised into the same mapping, so a menu call sees exactly the values
    the GUI saved.
    """
    from .gui.global_settings import default_global_settings, normalize_global_settings
    from .gui.settings import SettingsStore

    try:
        state, _warning = SettingsStore().load_state()
        return normalize_global_settings(state.get("global") or {})
    except (OSError, ValueError, TypeError):
        return default_global_settings()


def _chart_difficulty_overrides():
    """Return the ``(OD, HP)`` the shell 转谱 entries write into ``.osu``.

    A menu call has no form to fill in, so the 转谱工具 settings are the only
    source; they default to 8 and share ``resolve_chart_difficulty`` with the
    GUI form's empty-field fallback.
    """
    from .chart_settings import resolve_chart_difficulty

    return resolve_chart_difficulty(_load_global_settings())


def _speed_multiplier(args):
    """Resolve the requested multiplier from the menu-encoded arguments."""
    from .speed_change import SpeedChangeError, normalized_speed

    raw = args.speed if args.speed is not None else args.target_format
    try:
        return normalized_speed(raw)
    except SpeedChangeError as exc:
        raise QuickCommandError(str(exc), "invalid_speed")


# ``_prompt_speed`` returns this when no dialog can be shown, so the caller can
# tell "the user cancelled" apart from "there is no way to ask".
SPEED_PROMPT_UNAVAILABLE = object()


def _prompt_speed(default=1.5):
    """Ask for a multiplier with a small dialog, for the 自定义 menu entry."""
    try:
        import tkinter
        from tkinter import simpledialog
    except ImportError:
        return SPEED_PROMPT_UNAVAILABLE
    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        answer = simpledialog.askstring(
            "变速",
            "变速倍率（如 1.1）：",
            initialvalue=str(default),
            parent=root,
        )
    except Exception:
        return SPEED_PROMPT_UNAVAILABLE
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    if answer is None:
        raise QuickCommandError("已取消变速", "cancelled")
    return answer


def _run_speed_change(args, source, output_directory):
    """Speed-change a chart, archive or folder through the registered tool."""
    from .schema import ParamValidationError
    from .speed_change import SpeedChangeError, normalized_speed
    from .tools.base import ToolCancelledError, ToolContext
    from .tools.speed_change import SpeedChangeController

    notices = []

    def capture_notice(kind, payload):
        if kind == "warning" and isinstance(payload, str):
            notices.append(payload)

    if args.prompt_speed:
        answer = _prompt_speed()
        if answer is SPEED_PROMPT_UNAVAILABLE:
            raise QuickCommandError(
                "当前环境无法弹出倍率输入框，请改用固定倍率的菜单项",
                "speed_prompt_unavailable",
            )
        try:
            speed = normalized_speed(answer)
        except SpeedChangeError as exc:
            raise QuickCommandError(str(exc), "invalid_speed")
    else:
        speed = _speed_multiplier(args)
    raw = {
        "input_folders" if os.path.isdir(source) else "input_paths": [source],
        "speed": speed,
        "output_location": "workdir" if output_directory else "source",
    }
    if args.keep_pitch:
        raw["keep_pitch"] = True
    context = ToolContext(
        search_root=os.path.dirname(source) or os.getcwd(),
        output_root=output_directory or os.path.dirname(source) or os.getcwd(),
        global_settings=_load_global_settings(),
        invocation="shell",
    )
    if args.speed_suffix is not None and args.speed_suffix != "":
        # An empty suffix cannot select a file name, so it falls back to the
        # configured marker instead of producing an unnamed output.
        context.global_settings["speed_file_suffix"] = args.speed_suffix
    if args.difficulty_marker is not None:
        # An empty value clears the configured marker; the 转谱 and 变速 menu
        # entries pass the exact value they want.
        context.global_settings["speed_difficulty_marker"] = args.difficulty_marker
    if args.keep_pitch:
        context.global_settings["speed_keep_pitch"] = True
    controller = SpeedChangeController()
    try:
        prepared = controller.prepare(raw, context)
    except ParamValidationError as exc:
        raise QuickCommandError(str(exc), "invalid_input")
    prepared.params["_allow_overwrite"] = bool(args.force)
    if prepared.confirmation is not None and not args.force and not args.dry_run:
        raise QuickCommandError(
            "输出文件已存在，未执行变速；如需替换请显式使用 --force",
            "output_exists",
        )
    if args.dry_run:
        outputs = prepared.params["planned_outputs"]
        return {
            "status": "ok",
            "operation": OPERATION_SPEED_CHANGE,
            "input": source,
            "output": outputs[0] if len(outputs) == 1 else outputs,
            "outputs": outputs,
            "speed": speed,
            "dry_run": True,
        }
    try:
        result = controller.run(prepared, capture_notice)
    except ToolCancelledError as exc:
        raise QuickCommandError(str(exc), "cancelled")
    except (OSError, ValueError) as exc:
        raise QuickCommandError(str(exc), "operation_failed")
    paths = result.data.get("paths") or []
    return {
        "status": "ok",
        "operation": OPERATION_SPEED_CHANGE,
        "input": source,
        "output": paths[0] if len(paths) == 1 else paths,
        "outputs": paths,
        "speed": speed,
        "rows": result.data.get("rows") or [],
        "warnings": list(dict.fromkeys([*(result.data.get("warnings") or []), *notices])),
        "errors": result.data.get("errors") or [],
        "dry_run": False,
    }


def _short_shell_message(message, limit=160):
    text = " ".join(message.split())
    if "无法读取音频采样率：" in text:
        filename = text.split("无法读取音频采样率：", 1)[1].split("；", 1)[0]
        text = f"音频无法读取：{filename}"
    elif "输出为单个谱面文件，未复制其引用的资源文件" in text:
        filename = text.split(" 输出为单个谱面文件", 1)[0]
        text = f"{filename}：配套资源未复制，请与原资源放在一起。"
    text = text.replace("（未变速，输出仍指向原音乐）", "")
    text = text.replace("（归档结果不完整）", "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _shell_message_items(values):
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(
        _short_shell_message(item)
        for item in values if isinstance(item, str) and item.strip()
    ))


def format_shell_message(payload, returncode=0, *, show_notices=True, fallback=""):
    """Return a concise menu message and icon, or None when no dialog is needed."""
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("code") == "cancelled":
        return None
    if returncode or payload.get("status") == "error":
        if payload.get("code") == "output_exists":
            text = "输出已存在，未覆盖。\n请重命名已有结果后重试。"
        else:
            message = payload.get("message") or fallback or "操作失败，请检查输入。"
            text = _short_shell_message(str(message), limit=240)
        return text, 0x10

    errors = _shell_message_items(payload.get("errors"))
    if errors:
        messages, heading, icon = errors, "部分内容处理失败，结果不完整。", 0x10
    elif show_notices:
        messages = []
        for key in ("warnings", "missing_resources"):
            messages.extend(_shell_message_items(payload.get(key)))
        messages.extend(
            f"重名文件已改名：{item}"
            for item in _shell_message_items(payload.get("renamed_members"))
        )
        messages.extend(
            f"已跳过内嵌归档：{Path(item).name}"
            for item in _shell_message_items(payload.get("skipped_archives"))
        )
        messages = list(dict.fromkeys(messages))
        heading, icon = "已完成，请留意：", 0x30
    else:
        return None
    if not messages:
        return None
    lines = [heading, *messages[:3]]
    if len(messages) > 3:
        lines.append(f"另有 {len(messages) - 3} 项。")
    return "\n".join(lines), icon


def _emit_result(result, *, json_output, stdout):
    if json_output:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), file=stdout)
        return
    operation = result.get("operation")
    output = result.get("output")
    if result.get("dry_run"):
        print(f"已校验 {operation}，计划输出：{output}", file=stdout)
    elif operation == "replay-generate":
        print(f"已生成完美回放：{output}", file=stdout)
    elif operation == "chart-convert":
        print(f"已完成转谱：{output}", file=stdout)
        # The menu has no log panel, so a conversion diagnostic (a reference
        # that names a file which is not there, a member that had to be
        # renamed) has to be printed here or the user never learns why the
        # result looks wrong.
        for message in result.get("warnings") or ():
            print(f"  提示：{message}", file=stdout)
        for message in result.get("renamed_members") or ():
            print(f"  提示：归档成员重名已改名：{message}", file=stdout)
        for message in result.get("missing_resources") or ():
            print(f"  提示：{message}", file=stdout)
    elif operation == OPERATION_SPEED_CHANGE:
        outputs = result.get("outputs") or [output]
        print(f"已完成变速（{result.get('speed')}倍）：", file=stdout)
        for item in outputs:
            print(f"  {item}", file=stdout)
        for message in result.get("warnings") or ():
            print(f"  提示：{message}；本次结果不完整", file=stdout)
    else:
        print(f"已保存编辑后的回放：{output}", file=stdout)


def _emit_error(error, *, json_output, stderr):
    payload = {"status": "error", "code": error.code, "message": str(error)}
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=stderr)
    else:
        print(f"错误：{error}", file=stderr)


def _run_generate(args, source, output):
    timings = {}
    started = time.perf_counter()
    MrReplay, _generate_perfect_replay, controller_type, _edit_controller = _load_replay_api()
    timings["module_import"] = round((time.perf_counter() - started) * 1000.0, 3)
    from .tools.base import ToolContext

    controller = controller_type()
    started = time.perf_counter()
    prepared = controller.prepare(
        {"chart_path": source, "output_file": output},
        ToolContext(os.path.dirname(source)),
    )
    timings["prepare"] = round((time.perf_counter() - started) * 1000.0, 3)
    if prepared.confirmation is not None and not args.force and not args.dry_run:
        raise QuickCommandError(
            f"输出文件已存在：{output}；如需替换请显式使用 --force",
            "output_exists",
        )
    prepared.params["_allow_overwrite"] = bool(args.force)
    if not args.dry_run:
        def emit_event(event_type, payload):
            if event_type == "timing" and isinstance(payload, dict):
                timings[str(payload.get("phase"))] = round(
                    float(payload.get("elapsed_ms", 0.0)),
                    3,
                )

        try:
            result = controller.run(prepared, emit_event)
        except FileExistsError:
            raise QuickCommandError(
                f"输出文件已存在：{output}；如需替换请显式使用 --force",
                "output_exists",
            )
        replay = MrReplay.read(result.data["path"])
    else:
        started = time.perf_counter()
        replay = _generate_perfect_replay(source)
        timings["file_parse"] = round((time.perf_counter() - started) * 1000.0, 3)
    return {
        "status": "ok",
        "operation": "replay-generate",
        "input": source,
        "output": output,
        "events": len(replay.events),
        "dry_run": bool(args.dry_run),
        "timings_ms": timings,
    }


def _parse_mods(values):
    from .parsing.mr_file import MR_MODS

    by_name = {}
    for name, value, label in MR_MODS:
        for candidate in (name, name.removeprefix("MOD_"), label):
            by_name[str(candidate).casefold()] = value
    selected = []
    for raw in values or ():
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            try:
                value = int(token, 0)
            except ValueError:
                value = by_name.get(token.casefold())
            if value is None or value < 0:
                raise QuickCommandError(f"不支持的 MOD：{token}", "invalid_mod")
            if value not in selected:
                selected.append(value)
    return selected


def _run_edit(args, source, output):
    timings = {}
    started = time.perf_counter()
    MrReplay, _generate, _generate_controller, edit_controller_type = _load_replay_api()
    timings["module_import"] = round((time.perf_counter() - started) * 1000.0, 3)
    from .tools.base import ToolContext

    raw = {
        "replay_path": source,
        "output_file": output,
        "speed": args.speed if args.speed is not None else 1.0,
        "events_json": args.events_json or "",
    }
    for field in (
        "chart_path", "chart_version", "music", "artist", "score",
        "max_combo", "best", "cool", "good", "miss", "judge",
        "playing_time",
    ):
        value = getattr(args, field)
        if value is not None:
            raw[field] = value
    if args.mods is not None or args.clear_mod:
        raw["mod"] = _parse_mods(args.mods) if not args.clear_mod else []
    if args.events_file:
        try:
            raw["events_json"] = Path(args.events_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise QuickCommandError(f"事件文件无法读取：{exc}", "events_read_failed")
    controller = edit_controller_type()
    started = time.perf_counter()
    prepared = controller.prepare(raw, ToolContext(os.path.dirname(source)))
    timings["prepare"] = round((time.perf_counter() - started) * 1000.0, 3)
    prepared.params["_allow_overwrite"] = bool(args.force)
    if not args.dry_run:
        def emit_event(event_type, payload):
            if event_type == "timing" and isinstance(payload, dict):
                timings[str(payload.get("phase"))] = round(
                    float(payload.get("elapsed_ms", 0.0)),
                    3,
                )

        try:
            result = controller.run(prepared, emit_event)
        except FileExistsError:
            raise QuickCommandError(
                f"输出文件已存在：{output}；如需替换请显式使用 --force",
                "output_exists",
            )
        replay = MrReplay.read(result.data["path"])
    else:
        started = time.perf_counter()
        replay = MrReplay.read(source)
        timings["file_parse"] = round((time.perf_counter() - started) * 1000.0, 3)
    return {
        "status": "ok",
        "operation": "replay-edit",
        "input": source,
        "output": output,
        "events": len(replay.events),
        "dry_run": bool(args.dry_run),
        "timings_ms": timings,
    }


def _chart_target_for_source(extension):
    return {
        ".osu": ".mc", ".mc": ".osu", ".sm": ".osu",
        ".osz": ".mcz", ".mcz": ".osz",
    }.get(extension, ".osu")


def _publish_chart_output(staged, destination, *, force=False):
    from .chart_convert import _staged_output

    parent = os.path.dirname(destination) or os.getcwd()
    os.makedirs(parent, exist_ok=True)
    try:
        with _staged_output(destination, force) as temporary:
            shutil.copy2(staged, temporary)
    except FileExistsError as exc:
        raise QuickCommandError(
            f"输出文件已存在：{destination}；如需覆盖请显式使用 --force",
            "output_exists",
        ) from exc


def _unpack_archive(archive, destination):
    """Extract ``archive`` into ``destination``, rejecting unsafe members."""
    import zipfile

    root = os.path.abspath(destination)
    os.makedirs(root, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            name = info.filename.replace("\\", "/")
            target = os.path.abspath(os.path.join(root, *name.split("/")))
            if target != root and not target.startswith(root + os.sep):
                raise QuickCommandError(
                    f"归档包含不安全的路径：{info.filename}", "unsafe_archive_member"
                )
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with source.open(info) as reader, open(target, "wb") as writer:
                shutil.copyfileobj(reader, writer)


def _convert_folder_archive(source, target_format, force, staging, destination):
    """Convert one archive into another, unpacking only to re-pack it.

    The archive is expanded into ``staging`` and then converted there, so the
    chart is re-read by the normal parser instead of being rewritten blindly.
    ``destination`` is supplied by the caller so the output follows the source
    file's own location and stem rather than whatever name ``convert_paths``
    would derive.
    """
    from .chart_convert import convert_paths

    # A unique staging directory per archive: reusing one path would let
    # leftovers from the previous archive interfere with the next.
    slot = tempfile.mkdtemp(prefix="archive-", dir=staging)
    _unpack_archive(source, slot)
    overall_difficulty, hp_drain_rate = _chart_difficulty_overrides()
    staged = convert_paths(
        [source], target_format, slot, allow_overwrite=True,
        overall_difficulty=overall_difficulty,
        hp_drain_rate=hp_drain_rate,
    )
    _publish_chart_output(staged[0], destination, force=force)
    return destination


def _chart_reference_map(path, document, member, resource_members, source_root):
    """Return the reference rewrites one chart member needs.

    A resource member that had to be renamed -- or that ended up somewhere other
    than beside the chart -- leaves the chart pointing at the old name unless the
    reference is rewritten, so every writer that packs a chart asks for the same
    mapping instead of each keeping its own copy of the rule.
    """
    from .chart_convert import (
        _document_resource_references,
        _relative_reference,
        _resolve_resource_path,
    )

    reference_map = {}
    chart_dir = os.path.dirname(os.path.abspath(path))
    for _role, reference in _document_resource_references(document):
        resolved, _base = _resolve_resource_path(chart_dir, reference, source_root)
        if resolved is None:
            continue
        resource_member = resource_members.get(str(resolved).casefold())
        if resource_member is None:
            continue
        new_reference = _relative_reference(member, resource_member)
        if new_reference != reference:
            reference_map[reference] = new_reference
    return reference_map


def _convert_chart_into(
    path, documents, chart_extension, archive, prefix, planner, resource_members,
    source_root, overall_difficulty=None, hp_drain_rate=None,
):
    """Write one source chart's difficulties into ``archive``.

    The member keeps the source stem with the archive's chart extension, so the
    chart stays in the same relative place as its resources.  ``planner``
    guarantees the member name is unique in the package; when a resource had
    to be renamed to keep it unique, the chart's references are rewritten to
    match (``reference_map``), so audio never breaks silently.
    """
    from .chart_convert import archive_member_name, write_chart

    written = []
    for document in documents:
        member = planner.claim(f"{prefix}{archive_member_name(path, document, len(documents))}{chart_extension}")
        reference_map = _chart_reference_map(
            path, document, member, resource_members, source_root,
        )
        with tempfile.NamedTemporaryFile(suffix=chart_extension, delete=False) as temp:
            staged = temp.name
        try:
            write_chart(
                document, staged, chart_extension, reference_map=reference_map,
                overall_difficulty=overall_difficulty,
                hp_drain_rate=hp_drain_rate,
            )
            archive.write(staged, member)
        finally:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass
        written.append(member)
    return written


def _write_staged_chart(
    archive, document, member, chart_extension, reference_map=None,
    overall_difficulty=None, hp_drain_rate=None,
):
    """Write one document into ``archive`` under ``member`` via the writer.

    Used where a member would otherwise be copied verbatim, so the written
    chart still carries the requested OD/HP.
    """
    from .chart_convert import write_chart

    with tempfile.NamedTemporaryFile(suffix=chart_extension, delete=False) as temp:
        staged = temp.name
    try:
        write_chart(
            document, staged, chart_extension, reference_map=reference_map,
            overall_difficulty=overall_difficulty,
            hp_drain_rate=hp_drain_rate,
        )
        archive.write(staged, member)
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass


def _chart_source_keys(source):
    """Map ``(directory, stem)`` to the chart files sitting there."""
    keys = {}
    for root, directory_names, file_names in os.walk(source):
        directory_names.sort()
        for file_name in file_names:
            extension = os.path.splitext(file_name)[1].lower()
            if extension not in PLAIN_CHART_EXTENSIONS:
                continue
            stem = os.path.splitext(file_name)[0]
            keys.setdefault((root, stem), {})[extension] = os.path.join(root, file_name)
    return keys


def _pack_folder_archive(
    source, target_format, destination,
    overall_difficulty=None, hp_drain_rate=None,
):
    """Pack a whole folder into one archive of ``target_format``.

    Every chart becomes the plain chart that format contains (``.mcz`` holds
    ``.mc``, ``.osz`` holds ``.osu``) and is packed with the audio and artwork
    it names, at unchanged relative paths.  Other files travel along untouched.
    Nothing is converted into a per-chart archive: one folder yields exactly one
    archive, which is how Malody ships its beatmaps.

    ``overall_difficulty``/``hp_drain_rate`` are written into every ``.osu``
    member.  A chart that already is ``.osu`` cannot be copied byte for byte
    then: the whole point of the setting is one uniform OD/HP across the
    archive, so it is rewritten through the same writer (keeping its member
    name) instead.
    """
    import zipfile

    from .chart_convert import (
        ArchiveMemberPlanner,
        _missing_resource_messages,
        analyze_chart_resources,
        parse_chart,
    )

    chart_extension = ".mc" if target_format == ".mcz" else ".osu"
    rewrite_difficulty = chart_extension == ".osu" and (
        overall_difficulty is not None or hp_drain_rate is not None
    )
    converted = []
    copied = []
    skipped_archives = []
    missing_resources = []

    sources = _chart_source_keys(source)

    def text_sources(full):
        """Prefer the plain text chart that matches the archive when present.

        A Malody export of an osu! folder keeps ``song.osu`` and ``song.mc``
        side by side and both describe the same chart; reading the matching one
        avoids converting the wrong source.
        """
        directory, file_name = os.path.split(full)
        stem = os.path.splitext(file_name)[0]
        siblings = sources.get((directory, stem), {})
        return siblings.get(chart_extension, full)

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        planner = ArchiveMemberPlanner()
        packed = set()
        # Resources first so a chart is never missing its audio, even when the
        # artwork sits outside the chart's own directory.  Names are claimed
        # with the same planner the charts use, so nothing overwrites anything.
        resource_members = {}
        resource_files = []
        for (directory, _stem), siblings in sorted(sources.items()):
            primary = siblings.get(chart_extension) or sorted(siblings.values())[0]
            documents = parse_chart(primary)
            found, missing = analyze_chart_resources([(primary, documents)], source)
            missing_resources.extend(
                _missing_resource_messages(primary, missing)
            )
            for reference in found:
                key = str(reference.resolved).casefold()
                if key in resource_members:
                    continue
                relative = os.path.relpath(str(reference.resolved), source).replace("\\", "/")
                member = planner.claim(relative)
                resource_members[key] = member
                resource_files.append((reference.resolved, member, relative))
        for resolved, member, relative in resource_files:
            archive.write(str(resolved), member)
            # The walk below identifies a file by its path inside the folder, so
            # that -- not the absolute path, and not the (possibly renamed)
            # member name -- is what it has to be skipped by: a mismatch here
            # packs the same file a second time under a second name.
            packed.add(relative.casefold())
        for root, directory_names, file_names in os.walk(source):
            directory_names.sort()
            for file_name in sorted(file_names):
                full = os.path.join(root, file_name)
                relative = os.path.relpath(full, source).replace("\\", "/")
                if relative.casefold() in packed or relative in planner:
                    continue
                extension = os.path.splitext(file_name)[1].lower()
                if extension in ARCHIVE_EXTENSIONS:
                    # A chart container cannot be loaded from inside another
                    # one, so it is reported instead of being packed.
                    skipped_archives.append(full)
                    continue
                if extension in PLAIN_CHART_EXTENSIONS:
                    selected = text_sources(full)
                    if selected != full:
                        # Its equivalent is already the source we convert.
                        continue
                    documents = parse_chart(full)
                    prefix = relative[: len(relative) - len(os.path.basename(relative))]
                    if extension == chart_extension:
                        member = planner.claim(relative)
                        # A resource member that had to be renamed breaks this
                        # chart's reference unless it is rewritten, so even a
                        # same-format chart goes through the writer then.
                        reference_map = _chart_reference_map(
                            full, documents[0], member, resource_members, source,
                        )
                        if rewrite_difficulty or reference_map:
                            _write_staged_chart(
                                archive, documents[0], member, chart_extension,
                                reference_map=reference_map,
                                overall_difficulty=overall_difficulty,
                                hp_drain_rate=hp_drain_rate,
                            )
                        else:
                            archive.write(full, member)
                        packed.add(relative.casefold())
                        copied.append(member)
                        continue
                    names = _convert_chart_into(
                        full, documents, chart_extension, archive, prefix,
                        planner, resource_members, source,
                        overall_difficulty=overall_difficulty,
                        hp_drain_rate=hp_drain_rate,
                    )
                    packed.update(name.casefold() for name in names)
                    converted.extend(names)
                    continue
                archive.write(full, relative)
                packed.add(relative.casefold())
                planner.add(relative)
                copied.append(relative)
    return {
        "converted": converted,
        "copied": copied,
        "skipped_archives": skipped_archives,
        "missing_resources": sorted(set(missing_resources)),
        "renamed_members": [
            f"{requested} → {granted}" for requested, granted in planner.renamed
        ],
    }


def _run_folder_convert(args, source, target_format):
    """Pack everything under ``source`` into a single archive of the target format.

    The folder becomes one ``.mcz``/``.osz`` next to it, named after the folder.
    Charts inside are converted to the plain format that archive contains and
    packed with their resources at unchanged relative paths; nested archives are
    reported instead of being packed, because a chart container cannot be loaded
    from inside another one.
    """
    destination = os.path.abspath(source) + target_format
    parent = os.path.dirname(os.path.abspath(source)) or os.getcwd()
    if os.path.exists(destination) and not args.force:
        raise QuickCommandError(
            f"输出文件已存在：{destination}；如需覆盖请显式使用 --force",
            "output_exists",
        )
    os.makedirs(parent, exist_ok=True)
    overall_difficulty, hp_drain_rate = _chart_difficulty_overrides()
    with tempfile.TemporaryDirectory(prefix="osu-toolbox-quick-folder-") as staging:
        staged = os.path.join(staging, os.path.basename(destination))
        result = _pack_folder_archive(
            source, target_format, staged,
            overall_difficulty=overall_difficulty,
            hp_drain_rate=hp_drain_rate,
        )
        _publish_chart_output(staged, destination, force=args.force)
    outputs = [destination]
    return {
        "status": "ok",
        "operation": "chart-convert",
        "input": source,
        "output": destination,
        "outputs": outputs,
        "converted": len(result["converted"]),
        "converted_members": result["converted"],
        "copied_members": result["copied"],
        "skipped_archives": result["skipped_archives"],
        "missing_resources": result["missing_resources"],
        "renamed_members": result["renamed_members"],
        "overall_difficulty": overall_difficulty,
        "hp_drain_rate": hp_drain_rate,
        "dry_run": bool(args.dry_run),
    }


def _run_chart_convert(args, source, output, target_format):
    from .chart_convert import convert_paths

    requested = os.path.abspath(output)
    outputs = []
    messages = []

    def capture_diagnostic(kind, payload):
        if kind != "warning":
            return
        message = str(payload)
        if (
            "输出为单个谱面文件，未复制其引用的资源文件" in message
            and os.path.normcase(os.path.dirname(requested)) == os.path.normcase(os.path.dirname(source))
            and os.path.splitext(source)[1].lower() in PLAIN_CHART_EXTENSIONS
        ):
            return
        messages.append(message)

    overall_difficulty, hp_drain_rate = _chart_difficulty_overrides()
    with tempfile.TemporaryDirectory(prefix="osu-toolbox-quick-chart-") as temp_dir:
        staged = convert_paths(
            [source], target_format, temp_dir, allow_overwrite=True,
            # A menu conversion has no log panel, so what the conversion has to
            # say (for example a reference that names a file which is not
            # there) is carried out in the result instead of being dropped.
            emit_event=capture_diagnostic,
            overall_difficulty=overall_difficulty,
            hp_drain_rate=hp_drain_rate,
        )
        if len(staged) == 1:
            # One output goes to the destination the caller already knows about
            # (``-o`` or ``<source stem>.<target>``), which is also the name the
            # converter derives for a single-chart source.
            _publish_chart_output(staged[0], requested, force=args.force)
            outputs = [requested]
        else:
            for item in staged:
                destination = os.path.join(os.path.dirname(requested), os.path.basename(item))
                _publish_chart_output(item, destination, force=args.force)
                outputs.append(destination)
    return {
        "status": "ok",
        "operation": "chart-convert",
        "input": source,
        "output": outputs[0] if len(outputs) == 1 else outputs,
        "outputs": outputs,
        "warnings": messages,
        "overall_difficulty": overall_difficulty,
        "hp_drain_rate": hp_drain_rate,
        "dry_run": bool(args.dry_run),
    }


def _target_format(args, extension, *, is_folder=False):
    """Resolve the requested target, defaulting for a single file only."""
    requested = (args.target_format or "").lower() or None
    if is_folder:
        if requested not in ARCHIVE_EXTENSIONS:
            raise QuickCommandError(
                "文件夹转谱必须指定 .osz 或 .mcz；归档格式不再转换成单文件",
                "invalid_target_format",
            )
        return requested
    if requested:
        return requested
    return _chart_target_for_source(extension)


def _run_folder(args, source):
    """Dispatch a folder invocation, which is archive-shaped by design."""
    if args.operation not in FOLDER_OPERATIONS:
        raise QuickCommandError(
            "文件夹右键菜单只支持转谱操作", "unsupported_folder_operation"
        )
    target_format = _target_format(args, "", is_folder=True)
    started = time.perf_counter()
    if args.dry_run:
        planned = _plan_folder(source, target_format)
        result = {
            "status": "ok", "operation": "chart-convert", "input": source,
            "output": planned, "outputs": planned,
            "converted": len(planned), "dry_run": True,
        }
    else:
        result = _run_folder_convert(args, source, target_format)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return result


def _plan_folder(source, target_format):
    """Return the single archive a folder conversion would write."""
    return [os.path.abspath(source) + target_format]


def _require_input_kind(source, operation, extension):
    """Reject an input that does not match the operation the menu registered."""
    is_directory = os.path.isdir(source)
    if operation == OPERATION_FOLDER_SPEED_CHANGE:
        if not is_directory:
            raise QuickCommandError("该操作需要传入文件夹", "invalid_input_kind")
        return
    if operation in SPEED_CHANGE_OPERATIONS:
        if is_directory:
            raise QuickCommandError("文件夹变速请使用文件夹菜单项", "invalid_input_kind")
        if operation == OPERATION_ARCHIVE_SPEED_CHANGE and extension not in ARCHIVE_EXTENSIONS:
            raise QuickCommandError(
                "该操作需要传入 .osz 或 .mcz 归档文件", "invalid_extension"
            )
        if operation == OPERATION_SPEED_CHANGE and extension in ARCHIVE_EXTENSIONS:
            raise QuickCommandError(
                "归档变速请使用归档菜单项", "invalid_extension"
            )
        return
    if operation == OPERATION_FOLDER_CONVERT:
        if not is_directory:
            raise QuickCommandError("该操作需要传入文件夹", "invalid_input_kind")
        return
    if is_directory:
        if operation in FOLDER_OPERATIONS:
            return
        raise QuickCommandError("文件夹只支持转谱或变速操作", "invalid_input_kind")
    if operation == OPERATION_ARCHIVE_CONVERT and extension not in ARCHIVE_EXTENSIONS:
        raise QuickCommandError(
            "该操作需要传入 .osz 或 .mcz 归档文件", "invalid_extension"
        )


def run(args, *, stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    source = _absolute_input(args.path, args.context)
    operation = args.operation
    extension = "" if os.path.isdir(source) else os.path.splitext(source)[1].lower()
    _require_input_kind(source, operation, extension)
    if os.path.isdir(source) and operation not in SPEED_CHANGE_OPERATIONS:
        result = _run_folder(args, source)
        _emit_result(result, json_output=args.json_output, stdout=stdout)
        return 0
    if operation in SPEED_CHANGE_OPERATIONS:
        if not os.path.isdir(source) and extension not in SPEED_CHANGE_EXTENSIONS:
            raise QuickCommandError(
                "变速输入必须是 .osu、.mc、.sm、.osz 或 .mcz 文件", "invalid_extension"
            )
        started = time.perf_counter()
        output_directory = None
        if args.output_directory:
            output_directory = os.path.abspath(
                os.path.expandvars(os.path.expanduser(str(args.output_directory).strip().strip('"')))
            )
        elif args.output:
            output_directory = os.path.dirname(
                os.path.abspath(
                    os.path.expandvars(os.path.expanduser(str(args.output).strip().strip('"')))
                )
            )
        result = _run_speed_change(args, source, output_directory)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        _emit_result(result, json_output=args.json_output, stdout=stdout)
        return 0
    if operation == "replay-generate":
        if extension not in (".osu", ".mc"):
            raise QuickCommandError("回放生成输入必须是 .osu 或 .mc 文件", "invalid_extension")
        output = _output_path(args.output, source)
    elif operation == "replay-edit":
        if extension != ".mr":
            raise QuickCommandError("回放编辑输入必须是 .mr 文件", "invalid_extension")
        output = _output_path(args.output, source, edited=True)
    elif operation in (OPERATION_CHART_CONVERT, OPERATION_ARCHIVE_CONVERT):
        # ``archive-convert`` is chart conversion restricted to archive inputs;
        # the body below already enforces the archive-target rule.
        if extension not in (".osu", ".mc", ".sm", ".osz", ".mcz"):
            raise QuickCommandError("转谱输入必须是 .osu、.mc、.sm、.osz 或 .mcz 文件", "invalid_extension")
        target_format = args.target_format or _chart_target_for_source(extension)
        if extension in ARCHIVE_EXTENSIONS and target_format not in ARCHIVE_EXTENSIONS:
            raise QuickCommandError(
                "归档文件只能转换为另一个归档（.osz / .mcz）",
                "invalid_target_format",
            )
        target_ext = target_format
        if args.output:
            output = os.path.expandvars(os.path.expanduser(str(args.output).strip().strip('"')))
            if not os.path.isabs(output):
                output = os.path.join(os.path.dirname(source), output)
            output = os.path.abspath(output)
            if os.path.splitext(output)[1].lower() != target_format:
                output = os.path.splitext(output)[0] + target_format
        else:
            stem = os.path.splitext(os.path.basename(source))[0]
            if extension == target_format:
                stem += "-converted"
            output = os.path.join(os.path.dirname(source), stem + target_ext)
    else:
        raise QuickCommandError(f"未知操作：{operation}", "unknown_operation")
    # Chart conversion may produce one file per difficulty (for example a
    # multi-chart .sm input), so its controller validates each final path.
    if operation not in (OPERATION_CHART_CONVERT, OPERATION_ARCHIVE_CONVERT):
        _check_output(output, args.force, args.dry_run)
    started = time.perf_counter()
    if operation == "replay-generate":
        result = _run_generate(args, source, output)
    elif operation == "replay-edit":
        result = _run_edit(args, source, output)
    else:
        target_format = args.target_format or _chart_target_for_source(extension)
        if args.dry_run:
            from .chart_convert import planned_output_paths
            output_directory = os.path.dirname(output) or os.path.dirname(source)
            planned = planned_output_paths([source], target_format, output_directory)
            result = {"status": "ok", "operation": OPERATION_CHART_CONVERT, "input": source,
                      "output": planned[0] if len(planned) == 1 else planned,
                      "outputs": planned, "dry_run": True}
        else:
            result = _run_chart_convert(args, source, output, target_format)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    _emit_result(result, json_output=args.json_output, stdout=stdout)
    return 0


def main(argv=None, *, stdout=None, stderr=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # Keep the callable entry point return-code based for Explorer and
        # automation callers while preserving argparse's normal diagnostics.
        return int(exc.code or 0)
    try:
        return run(args, stdout=stdout, stderr=stderr)
    except KeyboardInterrupt:
        error = QuickCommandError("操作已取消", "cancelled")
        _emit_error(error, json_output=args.json_output, stderr=stderr or sys.stderr)
        return 130
    except QuickCommandError as exc:
        _emit_error(exc, json_output=args.json_output, stderr=stderr or sys.stderr)
        return 2
    except (ValueError, TypeError) as exc:
        error = QuickCommandError(str(exc), "invalid_input")
        _emit_error(error, json_output=args.json_output, stderr=stderr or sys.stderr)
        return 2
    except OSError as exc:
        error = QuickCommandError(str(exc), "operation_failed")
        _emit_error(error, json_output=args.json_output, stderr=stderr or sys.stderr)
        return 1
    except Exception as exc:
        error = QuickCommandError(str(exc), "operation_failed")
        _emit_error(error, json_output=args.json_output, stderr=stderr or sys.stderr)
        return 1


def measure_startup(argv, repetitions=3, python_executable=None):
    """Run a protocol invocation repeatedly and return cold-start samples.

    This helper is intentionally subprocess-based: each sample represents a
    fresh interpreter and therefore catches accidental GUI dependency imports
    that an in-process timer would hide.
    """
    import subprocess

    if repetitions < 1:
        raise ValueError("repetitions 必须大于 0")
    executable = python_executable or sys.executable
    command = [executable, "-m", "osu_toolbox", *list(argv)]
    samples = []
    returncodes = []
    for _ in range(int(repetitions)):
        started = time.perf_counter()
        completed = subprocess.run(command, capture_output=True, text=True)
        samples.append(round((time.perf_counter() - started) * 1000.0, 3))
        returncodes.append(completed.returncode)
    return {
        "samples_ms": samples,
        "min_ms": min(samples),
        "median_ms": sorted(samples)[len(samples) // 2],
        "max_ms": max(samples),
        "returncodes": returncodes,
    }


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "CONTEXT_QUICK",
    "CONTEXT_SHELL",
    "OPERATION_ARCHIVE_CONVERT",
    "OPERATION_ARCHIVE_SPEED_CHANGE",
    "OPERATION_CHART_CONVERT",
    "OPERATION_FOLDER_CONVERT",
    "OPERATION_SPEED_CHANGE",
    "QuickCommandError",
    "SPEED_CHANGE_OPERATIONS",
    "SUPPORTED_CONTEXTS",
    "SUPPORTED_OPERATIONS",
    "build_parser",
    "format_shell_message",
    "main",
    "measure_startup",
    "run",
)
