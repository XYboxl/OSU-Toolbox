"""Perfect Malody replay generation tool."""

import os
import time

from osu_toolbox import MOD_AUTO, atomic_write_replay, generate_perfect_replay
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
    MOD_CHOICES,
    REPLAY_GROUPS,
    RESULT_PARAMS,
    apply_replay_params,
    output_candidate,
    output_confirmation,
    replay_prefill_params,
    validate_chart,
    validate_editable_params,
)


_GENERATE_RESULT_PARAMS = tuple(
    ParamSpec(
        spec.key,
        spec.label,
        spec.kind,
        [MOD_AUTO] if spec.key == "mod" else spec.default,
        spec.group,
        spec.help,
        unit=spec.unit,
        minimum=spec.minimum,
        maximum=spec.maximum,
        choices=MOD_CHOICES if spec.key == "mod" else spec.choices,
        dialog_mode=spec.dialog_mode,
        filters=spec.filters,
        required=spec.required,
        persist=spec.persist,
        multiple=spec.multiple,
    )
    for spec in RESULT_PARAMS
)

REPLAY_GENERATE_PARAMS = (
    ParamSpec(
        "chart_path", "谱面文件", "path", None, "input",
        "选择用于生成完美回放的 .osu 或 .mc 谱面",
        required=True,
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
    *_GENERATE_RESULT_PARAMS,
    EVENTS_PARAM,
    ParamSpec(
        "output_file", "输出回放", "path", "", "output",
        "主程序中默认输出到主程序目录，文件名沿用原谱面并使用 .mr 扩展名",
        persist=False,
        dialog_mode="save_file",
        filters=(("回放文件", "*.mr"),),
    ),
)


class ReplayGenerateController:
    def prefill(self, raw_params, changed_key, context):
        if changed_key != "chart_path":
            return PrefillResult({})
        chart_path = validate_chart((raw_params or {}).get("chart_path"))
        replay = generate_perfect_replay(chart_path)
        output_file = output_candidate(
            None,
            chart_path,
            output_root=context.output_root,
        )
        return PrefillResult(replay_prefill_params(replay, output_file))

    def prepare(self, raw_params, context):
        raw_params = raw_params if isinstance(raw_params, dict) else {}
        params = coerce_params(REPLAY_GENERATE_PARAMS, raw_params)
        params["chart_path"] = validate_chart(params["chart_path"])
        params["metadata_fields"] = tuple(
            key for key in ("chart_version", "music", "artist") if key in raw_params
        )
        validate_editable_params(params)
        params["output_file"] = output_candidate(
            params["output_file"],
            params["chart_path"],
            output_root=context.output_root,
        )
        return PreparedRun(
            params,
            confirmation=output_confirmation(params["output_file"]),
        )

    def run(self, prepared, emit_event, cancel_event=None):
        params = prepared.params
        if cancel_event is not None and cancel_event.is_set():
            raise ToolCancelledError("回放生成已取消")
        emit_event("progress", {"current": 0.1, "total": 1, "fraction": 0.1})
        started = time.perf_counter()
        replay = generate_perfect_replay(params["chart_path"])
        emit_event("timing", {
            "phase": "file_parse",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        started = time.perf_counter()
        apply_replay_params(replay, params)
        emit_event("timing", {
            "phase": "transform",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        emit_event("log", f"已生成 {len(replay.events)} 个真实回放事件")
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
            "label": "生成的回放",
        })


REPLAY_GENERATE_TOOL = ToolDefinition(
    spec=ToolSpec(
        id="replay-generate",
        title="回放生成",
        description="从 .osu 或 .mc 谱面生成完美的 Malody 回放",
        category="回放工具",
        action_label="生成完美回放",
        groups=REPLAY_GROUPS,
        params=REPLAY_GENERATE_PARAMS,
        result_kind="file",
        concurrency="exclusive",
    ),
    controller=ReplayGenerateController(),
)


__all__ = (
    "REPLAY_GENERATE_PARAMS",
    "REPLAY_GENERATE_TOOL",
    "ReplayGenerateController",
)
