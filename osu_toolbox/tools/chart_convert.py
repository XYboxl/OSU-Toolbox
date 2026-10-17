"""Registered chart conversion tool."""

import os
from pathlib import Path
import time

from osu_toolbox.chart_convert import (
    ChartConversionError,
    SUPPORTED_INPUT_EXTENSIONS,
    convert_paths,
    preflight_conversion,
)
from osu_toolbox.chart_settings import resolve_chart_difficulty
from osu_toolbox.schema import ChoiceSpec, GroupSpec, ParamSpec, ParamValidationError, coerce_params

from .base import (
    ConfirmationRequest,
    PrefillResult,
    PreparedRun,
    ToolCancelledError,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)


TARGET_CHOICES = (
    ChoiceSpec(".osu", ".osu", ".osu"),
    ChoiceSpec(".mc", ".mc", ".mc"),
    ChoiceSpec(".sm", ".sm", ".sm"),
    ChoiceSpec(".osz", ".osz（归档）", ".osz"),
    ChoiceSpec(".mcz", ".mcz（归档）", ".mcz"),
)

OUTPUT_LOCATION_CHOICES = (
    ChoiceSpec("source", "输入文件所在目录", "source"),
    ChoiceSpec("workdir", "程序工作目录", "workdir"),
)

CHART_CONVERT_GROUPS = (
    GroupSpec("input", "输入谱面"),
    GroupSpec("output", "输出设置"),
)

CHART_CONVERT_PARAMS = (
    ParamSpec(
        "input_paths", "谱面文件", "path", None, "input",
        "支持 .osu、.mc、.sm，以及 .osz、.mcz 归档；可一次选择多个文件",
        required=True, persist=False, dialog_mode="open_file",
        filters=(("谱面或归档", "*.osu;*.mc;*.sm;*.osz;*.mcz"),), multiple=True,
    ),
    ParamSpec(
        "target_format", "目标格式", "choice", ".osu", "output",
        "选择普通谱面或归档格式", choices=TARGET_CHOICES,
    ),
    ParamSpec(
        "output_location", "输出位置", "choice", "source", "output",
        "默认输出到输入谱面所在目录，也可输出到程序工作目录",
        choices=OUTPUT_LOCATION_CHOICES,
    ),
    ParamSpec(
        "overall_difficulty", "OD（OverallDifficulty）", "optional_float", None, "output",
        "仅影响 .osu/.osz；留空使用转谱设置的默认值，本次填写不修改设置",
        minimum=0.0, maximum=10.0, persist=False,
        placeholder="留空则用设置里的默认值",
    ),
    ParamSpec(
        "hp_drain_rate", "HP（HPDrainRate）", "optional_float", None, "output",
        "仅影响 .osu/.osz；留空使用转谱设置的默认值，本次填写不修改设置",
        minimum=0.0, maximum=10.0, persist=False,
        placeholder="留空则用设置里的默认值",
    ),
)


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


class ChartConvertController:
    def prefill(self, raw_params, changed_key, context):
        return PrefillResult({})

    def prepare(self, raw_params, context):
        raw_params = dict(raw_params or {})
        if isinstance(raw_params.get("input_paths"), (list, tuple)):
            raw_params["input_paths"] = "\n".join(str(item) for item in raw_params["input_paths"])
        params = coerce_params(CHART_CONVERT_PARAMS, raw_params)
        input_paths = _path_values(params.get("input_paths"))
        if not input_paths:
            raise ParamValidationError("input_paths", "请选择至少一个谱面文件")
        normalized = []
        for value in input_paths:
            path = os.path.abspath(os.path.expandvars(os.path.expanduser(value)))
            if not os.path.isfile(path):
                raise ParamValidationError("input_paths", f"输入文件不存在：{path}")
            if Path(path).suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
                raise ParamValidationError("input_paths", f"不支持的输入格式：{Path(path).suffix}")
            normalized.append(path)
        params["input_paths"] = normalized
        output_dir = (
            context.output_root if params["output_location"] == "workdir"
            else os.path.dirname(normalized[0])
        )
        params["output_directory"] = os.path.abspath(output_dir or os.getcwd())
        try:
            planned, preflight_warnings = preflight_conversion(
                normalized, params["target_format"], params["output_directory"]
            )
        except (OSError, ValueError, ChartConversionError) as exc:
            raise ParamValidationError("input_paths", str(exc))
        params["planned_outputs"] = planned
        params["source_fingerprints"] = planned.source_fingerprints
        # The form carries only what the user typed for this run; an empty field
        # means "use the 转谱工具 setting".  Resolving here (not in the writer)
        # keeps the preview and the written result on one answer.
        overall_difficulty, hp_drain_rate = resolve_chart_difficulty(
            getattr(context, "global_settings", None),
            params.get("overall_difficulty"),
            params.get("hp_drain_rate"),
        )
        params["overall_difficulty"] = overall_difficulty
        params["hp_drain_rate"] = hp_drain_rate
        warnings = list(preflight_warnings)
        if params["target_format"] in (".osu", ".osz"):
            # The source's own OD/HP is replaced, so say so before the run: a
            # chart that arrives with different values is otherwise silently
            # rewritten.
            warnings.insert(0, (
                "转换出的 .osu 谱面将统一写入 "
                f"OD（OverallDifficulty）{overall_difficulty:g}、"
                f"HP（HPDrainRate）{hp_drain_rate:g}"
            ))
        existing = [path for path in planned if os.path.exists(path)]
        confirmation = None
        if existing:
            confirmation = ConfirmationRequest(
                "确认覆盖输出",
                "覆盖以下文件？\n" + "\n".join(existing),
                "覆盖",
            )
        return PreparedRun(params, warnings=tuple(warnings), confirmation=confirmation)

    def run(self, prepared, emit_event, cancel_event=None):
        params = prepared.params
        if cancel_event is not None and cancel_event.is_set():
            raise ToolCancelledError("转谱任务已取消")
        started = time.perf_counter()

        def emit(kind, payload):
            emit_event(kind, payload)
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelledError("转谱任务已取消")

        outputs = convert_paths(
            params["input_paths"], params["target_format"], params["output_directory"],
            emit_event=emit, allow_overwrite=bool(params.get("_allow_overwrite")),
            expected_outputs=params.get("planned_outputs"),
            expected_inputs=params.get("source_fingerprints"),
            overall_difficulty=params.get("overall_difficulty"),
            hp_drain_rate=params.get("hp_drain_rate"),
        )
        emit_event("timing", {
            "phase": "chart_convert",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        })
        emit_event("log", f"已转换 {len(outputs)} 个谱面输出")
        return ToolResult("table", {
            "columns": ("目标格式", "输出文件"),
            "rows": [(params["target_format"], item) for item in outputs],
            "paths": outputs,
        })


CHART_CONVERT_TOOL = ToolDefinition(
    spec=ToolSpec(
        id="chart-convert",
        title="转谱",
        description="转换 osu!、Malody、StepMania 谱面与归档格式",
        category="谱面工具",
        action_label="开始转谱",
        groups=CHART_CONVERT_GROUPS,
        params=CHART_CONVERT_PARAMS,
        result_kind="table",
        concurrency="exclusive",
    ),
    controller=ChartConvertController(),
)


__all__ = ("CHART_CONVERT_PARAMS", "CHART_CONVERT_TOOL", "ChartConvertController")
