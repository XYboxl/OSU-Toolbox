"""Shared replay-tool parameters and validation helpers."""

import json
import os

from osu_toolbox import MR_MODS, MrEvent
from osu_toolbox.schema import ChoiceSpec, GroupSpec, ParamSpec, ParamValidationError

from .base import ConfirmationRequest


REPLAY_GROUPS = (
    GroupSpec("input", "输入文件"),
    GroupSpec("metadata", "回放元数据", collapsible=True),
    GroupSpec("results", "成绩数据", collapsible=True),
    GroupSpec("events", "事件数据", collapsible=True),
    GroupSpec("output", "输出文件"),
)

MOD_CHOICES = tuple(ChoiceSpec(key, label, value) for key, value, label in MR_MODS)

JUDGE_CHOICES = tuple(
    ChoiceSpec(f"judge_{letter.lower()}", f"{letter} 判", value)
    for value, letter in enumerate("ABCDE")
)

METADATA_PARAMS = (
    ParamSpec("chart_version", "谱面难度", "text", "", "metadata", "可修改或清空谱面难度"),
    ParamSpec("music", "歌曲标题", "text", "", "metadata", "可修改或清空歌曲标题"),
    ParamSpec("artist", "艺术家", "text", "", "metadata", "可修改或清空艺术家"),
)

RESULT_PARAMS = (
    ParamSpec("score", "分数", "optional_int", None, "results", "留空则保留生成值或原值", minimum=0),
    ParamSpec("max_combo", "最大连击", "optional_int", None, "results", "留空则保留生成值或原值", minimum=0),
    ParamSpec("best", "Best 数量", "optional_int", None, "results", "留空则保留生成值或原值", minimum=0),
    ParamSpec("cool", "Cool 数量", "optional_int", None, "results", "留空则保留生成值或原值", minimum=0),
    ParamSpec("good", "Bad 数量", "optional_int", None, "results", "留空则保留生成值或原值", minimum=0),
    ParamSpec("miss", "Miss 数量", "optional_int", None, "results", "留空则保留生成值或原值", minimum=0),
    ParamSpec(
        "mod", "MOD", "choice", None, "results",
        "可多选；不选择任何项目时保存为无 MOD",
        choices=MOD_CHOICES,
        multiple=True,
    ),
    ParamSpec(
        "judge", "Judge 类型", "choice", 0, "results",
        "A 判到 E 判分别写入数值 0 到 4，只能选择一项",
        choices=JUDGE_CHOICES,
    ),
    ParamSpec(
        "playing_time", "记录时间戳", "optional_int", None, "results",
        "填写秒级时间戳，留空则保留生成值或原值",
        minimum=0,
    ),
)

EVENTS_PARAM = ParamSpec(
    "events_json", "事件 JSON", "text", "", "events",
    "每项包含 time、event、column；留空保留生成事件或原事件，填写 [] 可清空",
)

EDITABLE_NUMERIC_FIELDS = (
    "score", "max_combo", "best", "cool", "good", "miss", "judge", "playing_time",
)

_NUMERIC_LABELS = {
    "score": "分数",
    "max_combo": "最大连击",
    "best": "Best 数量",
    "cool": "Cool 数量",
    "good": "Bad 数量",
    "miss": "Miss 数量",
    "judge": "Judge 类型",
    "playing_time": "记录时间戳",
}


def output_candidate(value, source_path, *, edited=False, output_root=None):
    source = os.path.abspath(os.fspath(source_path)) if source_path else None
    default_root = (
        os.path.abspath(os.fspath(output_root))
        if output_root
        else os.path.dirname(source) if source else os.getcwd()
    )
    if value:
        path = str(value).strip().strip('"')
        if not os.path.isabs(path):
            path = os.path.join(default_root, path)
    elif source:
        stem = os.path.splitext(os.path.basename(source))[0]
        suffix = "-edited" if edited else ""
        path = os.path.join(default_root, f"{stem}{suffix}.mr")
    else:
        filename = "edited.mr" if edited else "perfect.mr"
        path = os.path.join(default_root, filename)
    path = os.path.expandvars(os.path.expanduser(path))
    if os.path.splitext(path)[1].lower() != ".mr":
        path = os.path.splitext(path)[0] + ".mr"
    return os.path.abspath(path)


def validate_chart(path, key="chart_path"):
    if not path:
        raise ParamValidationError(key, "请选择 .osu 或 .mc 谱面文件")
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path):
        raise ParamValidationError(key, f"谱面文件不存在：{path}")
    if os.path.splitext(path)[1].lower() not in (".osu", ".mc"):
        raise ParamValidationError(key, "谱面文件必须是 .osu 或 .mc 文件")
    return path


def validate_replay(path, key="replay_path"):
    if not path:
        raise ParamValidationError(key, "请选择 .mr 回放文件")
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path):
        raise ParamValidationError(key, f"回放文件不存在：{path}")
    if os.path.splitext(path)[1].lower() != ".mr":
        raise ParamValidationError(key, "回放文件必须是 .mr 文件")
    return path


def parse_events_json(value):
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ParamValidationError("events_json", f"事件 JSON 无法解析：{exc}")
    if not isinstance(payload, list):
        raise ParamValidationError("events_json", "事件 JSON 必须是数组")
    events = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ParamValidationError("events_json", f"第 {index + 1} 个事件必须是对象")
        try:
            event = MrEvent(int(item["time"]), int(item["event"]), int(item["column"]))
        except (KeyError, TypeError, ValueError):
            raise ParamValidationError(
                "events_json",
                f"第 {index + 1} 个事件必须包含有效的 time、event、column",
            )
        events.append(event.as_dict())
    return events


def validate_editable_params(params):
    params["events"] = parse_events_json(params["events_json"])
    for key in EDITABLE_NUMERIC_FIELDS:
        value = params[key]
        if value is not None and value < 0:
            raise ParamValidationError(key, f"{_NUMERIC_LABELS[key]}不能小于 0")
    if params["judge"] is not None and params["judge"] > 4:
        raise ParamValidationError("judge", "Judge 类型必须在 0 到 4 之间")
    return params


def apply_replay_params(replay, params):
    metadata_fields = params.get("metadata_fields", ())
    replay.update_metadata(**{
        key: params.get(key) or ""
        for key in ("chart_version", "music", "artist")
        if key in metadata_fields
    })
    for field in EDITABLE_NUMERIC_FIELDS:
        value = params[field]
        if value is not None:
            setattr(replay, field, value)
    if params["mod"] is not None:
        replay.mod = 0
        for mod in params["mod"]:
            replay.mod |= int(mod)
    if params["events"] is not None:
        replay.CHART = params["events"]
    if params["speed"] != 1.0:
        replay.scale(params["speed"])
    return replay


def replay_prefill_params(replay, output_file):
    selected_mods = [value for _key, value, _label in MR_MODS if replay.mod & value]
    return {
        "chart_version": replay.chart_version,
        "music": replay.music,
        "artist": replay.artist,
        "score": replay.score,
        "max_combo": replay.max_combo,
        "best": replay.best,
        "cool": replay.cool,
        "good": replay.good,
        "miss": replay.miss,
        "mod": selected_mods,
        "judge": replay.judge,
        "playing_time": replay.playing_time,
        "events_json": json.dumps(replay.CHART, ensure_ascii=False, separators=(",", ":")),
        "output_file": output_file,
    }


def output_confirmation(path):
    if not os.path.exists(path):
        return None
    return ConfirmationRequest(
        title="覆盖输出文件",
        message=f"覆盖已有文件？\n{path}",
        confirm_label="覆盖",
    )


__all__ = (
    "EDITABLE_NUMERIC_FIELDS",
    "EVENTS_PARAM",
    "JUDGE_CHOICES",
    "METADATA_PARAMS",
    "MOD_CHOICES",
    "REPLAY_GROUPS",
    "RESULT_PARAMS",
    "apply_replay_params",
    "output_candidate",
    "output_confirmation",
    "replay_prefill_params",
    "validate_chart",
    "validate_editable_params",
    "validate_replay",
)
