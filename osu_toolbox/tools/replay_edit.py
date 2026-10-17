"""Malody replay parameter editing tool."""

import os
import time

from osu_toolbox import MR_MODS, MrReplay, _file_md5, atomic_write_replay
from osu_toolbox.schema import ParamSpec, coerce_params

from .base import (
    PrefillResult,
    PreparedRun,
    ToolCancelledError,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from .replay import (
    EVENTS_PARAM,
    METADATA_PARAMS,
    REPLAY_GROUPS,
    RESULT_PARAMS,
    apply_replay_params,
    output_candidate,
    output_confirmation,
    replay_prefill_params,
    validate_chart,
    validate_editable_params,
    validate_replay,
)


REPLAY_EDIT_PARAMS = (
    ParamSpec(
        "replay_path", "回放文件", "path", None, "input",
        "选择需要编辑参数的 .mr 回放文件",
        required=True,
        persist=False,
        dialog_mode="open_file",
        filters=(("回放文件", "*.mr"),),
    ),
    ParamSpec(
        "chart_path", "原始谱面", "path", None, "input",
        "可选；仅根据用户选择的 .osu 或 .mc 原始文件重新计算谱面 MD5",
        persist=False,
        dialog_mode="open_file",
        filters=(("谱面文件", "*.osu;*.mc"),),
    ),
    ParamSpec(
        "speed", "播放速度", "float", 1.0, "input",
        "2.0 表示两倍速，0.5 表示半速",
        unit="倍",
        minimum=0.01,
        maximum=100.0,
    ),
    *METADATA_PARAMS,
    *RESULT_PARAMS,
    EVENTS_PARAM,
    ParamSpec(
        "output_file", "输出回放", "path", "", "output",
        "主程序中默认输出到主程序目录，并在原文件名后添加 -edited",
        persist=False,
        dialog_mode="save_file",
        filters=(("回放文件", "*.mr"),),
    ),
)


class ReplayEditController:
    def prefill(self, raw_params, changed_key, context):
        if changed_key != "replay_path":
            return PrefillResult({})
        replay_path = validate_replay((raw_params or {}).get("replay_path"))
        replay = MrReplay.read(replay_path)
        output_file = output_candidate(
            None,
            replay_path,
            edited=True,
            output_root=context.output_root,
        )
        known_mod_mask = 0
        for _key, value, _label in MR_MODS:
            known_mod_mask |= value
        warnings = ()
        if replay.mod & ~known_mod_mask:
            warnings = ("回放包含当前格式资料未登记的 MOD 位，保存时只保留界面列出的 MOD。",)
        return PrefillResult(
            replay_prefill_params(replay, output_file),
            warnings,
        )

    def prepare(self, raw_params, context):
        raw_params = raw_params if isinstance(raw_params, dict) else {}
        params = coerce_params(REPLAY_EDIT_PARAMS, raw_params)
        params["replay_path"] = validate_replay(params["replay_path"])
        if params["chart_path"]:
            params["chart_path"] = validate_chart(params["chart_path"])
        if "judge" not in raw_params:
            params["judge"] = None
        params["metadata_fields"] = tuple(
            key for key in ("chart_version", "music", "artist") if key in raw_params
        )
        validate_editable_params(params)
        params["output_file"] = output_candidate(
            params["output_file"],
            params["replay_path"],
            edited=True,
            output_root=context.output_root,
        )
        return PreparedRun(
            params,
            confirmation=output_confirmation(params["output_file"]),
        )

    def run(self, prepared, emit_event, cancel_event=None):
        params = prepared.params
        if cancel_event is not None and cancel_event.is_set():
            raise ToolCancelledError("回放编辑已取消")
        emit_event("progress", {"current": 0.1, "total": 1, "fraction": 0.1})
        started = time.perf_counter()
        replay = MrReplay.read(params["replay_path"])
        emit_event("timing", {
            "phase": "file_parse",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        emit_event("log", f"已读取 {len(replay.events)} 个回放事件")
        started = time.perf_counter()
        if params["chart_path"]:
            replay.update_metadata(md5=_file_md5(params["chart_path"]))
        apply_replay_params(replay, params)
        emit_event("timing", {
            "phase": "transform",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        emit_event("progress", {"current": 0.8, "total": 1, "fraction": 0.8})
        started = time.perf_counter()
        atomic_write_replay(
            replay,
            params["output_file"],
            overwrite=bool(params.get("_allow_overwrite")),
        )
        emit_event("timing", {
            "phase": "output_write",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        emit_event("progress", {"current": 1, "total": 1, "fraction": 1.0})
        return ToolResult("file", {
            "path": params["output_file"],
            "name": os.path.basename(params["output_file"]),
            "label": "编辑后的回放",
        })


REPLAY_EDIT_TOOL = ToolDefinition(
    spec=ToolSpec(
        id="replay-edit",
        title="回放编辑",
        description="修改 Malody 回放的谱面信息、成绩与事件",
        category="回放工具",
        action_label="保存编辑后的回放",
        groups=REPLAY_GROUPS,
        params=REPLAY_EDIT_PARAMS,
        result_kind="file",
        concurrency="exclusive",
    ),
    controller=ReplayEditController(),
)


__all__ = (
    "REPLAY_EDIT_PARAMS",
    "REPLAY_EDIT_TOOL",
    "ReplayEditController",
)
