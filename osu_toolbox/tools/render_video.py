"""Registered video-rendering tool and its orchestration logic."""

import os
import time

from osu_toolbox import config
from osu_toolbox.schema import (
    ChoiceSpec,
    GroupSpec,
    ParamSpec,
    ParamValidationError,
    coerce_params,
)

from .base import (
    ConfirmationRequest,
    PrefillResult,
    PreparedRun,
    ToolDefinition,
    ToolCancelledError,
    ToolResult,
    ToolSpec,
)


_DEFAULTS = config.DEFAULT_RENDER_PARAMS

_COMPAT_CHOICES = (
    ChoiceSpec(config.COMPAT_STABLE_REPLAY, "osu!stable 回放", config.COMPAT_STABLE_REPLAY),
    ChoiceSpec(config.COMPAT_LAZER_CLASSIC, "osu!lazer classic", config.COMPAT_LAZER_CLASSIC),
)

_SCROLL_DIRECTION_CHOICES = (
    ChoiceSpec("auto", "跟随皮肤", "auto"),
    ChoiceSpec("down", "下落", "down"),
    ChoiceSpec("up", "上落", "up"),
)

_LETTERBOX_CHOICES = (
    ChoiceSpec("follow", "跟随谱面", None),
    ChoiceSpec("on", "强制黑边", True),
    ChoiceSpec("off", "禁用黑边", False),
)

_SONG_PROGRESS_CHOICES = (
    ChoiceSpec("off", "关闭", config.SONG_PROGRESS_OFF, aliases=(False,)),
    ChoiceSpec("pie", "进度饼", config.SONG_PROGRESS_PIE, aliases=(True,)),
    ChoiceSpec("top_right", "右上进度条", config.SONG_PROGRESS_TOP_RIGHT),
    ChoiceSpec("bottom_right", "右下进度条", config.SONG_PROGRESS_BOTTOM_RIGHT),
    ChoiceSpec("bottom", "底部进度条", config.SONG_PROGRESS_BOTTOM),
)

RENDER_GROUPS = (
    GroupSpec("input", "输入文件"),
    GroupSpec("basic", "基础设置"),
    GroupSpec("advanced", "高级设置", collapsible=True),
    GroupSpec("display", "显示选项", collapsible=True),
    GroupSpec("output", "输出"),
)

RENDER_PARAMS = (
    ParamSpec(
        "beatmap_path", "谱面", "path", _DEFAULTS["beatmap_path"], "input",
        ".osu 谱面文件", required=True, persist=False,
        dialog_mode="open_file", filters=(("谱面", "*.osu"),),
    ),
    ParamSpec(
        "replay_path", "回放", "path", _DEFAULTS["replay_path"], "input",
        ".osr 回放文件；留空时使用 Auto", persist=False,
        dialog_mode="open_file", filters=(("回放", "*.osr"),),
    ),
    ParamSpec(
        "skin_dir", "皮肤目录", "path", _DEFAULTS["skin_dir"], "input",
        "用户选择的自定义 osu! 皮肤目录", required=True, persist=False,
        dialog_mode="folder",
    ),
    ParamSpec(
        "background_path", "背景图", "path", _DEFAULTS["background_path"], "input",
        "留空时自动读取谱面背景", persist=False,
        dialog_mode="open_file",
        filters=(("图片文件", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),),
    ),
    ParamSpec(
        "output_file", "输出视频", "path", _DEFAULTS["output_file"], "output",
        "输出 MP4 路径", persist=False, dialog_mode="save_file",
        filters=(("MP4 视频", "*.mp4"),),
    ),
    ParamSpec("key_count", "键数", "optional_int", _DEFAULTS["key_count"], "basic", "留空时使用谱面 CS"),
    ParamSpec("fps", "帧率", "int", _DEFAULTS["fps"], "basic", "输出视频帧率", unit="FPS"),
    ParamSpec("width", "视频宽度", "int", _DEFAULTS["width"], "basic", "输出视频宽度", unit="px"),
    ParamSpec("height", "视频高度", "int", _DEFAULTS["height"], "basic", "输出视频高度", unit="px"),
    ParamSpec(
        "compatibility_mode", "兼容模式", "choice", _DEFAULTS["compatibility_mode"],
        "basic", "选择 stable legacy 或 lazer classic 行为", choices=_COMPAT_CHOICES,
    ),
    ParamSpec(
        "scroll_speed", "下落速度", "float", _DEFAULTS["scroll_speed"], "basic",
        "下落速度，核心运行时限制为 1 到 40", minimum=1.0, maximum=40.0,
    ),
    ParamSpec(
        "scroll_direction", "滚动方向", "choice", _DEFAULTS["scroll_direction"], "basic",
        "自动跟随皮肤，或强制下落/上落", choices=_SCROLL_DIRECTION_CHOICES,
    ),
    ParamSpec("scroll_speed_mult", "速度倍率", "float", _DEFAULTS["scroll_speed_mult"], "basic", "下落速度倍率"),
    ParamSpec(
        "n_parallel", "并行分段", "int", _DEFAULTS["n_parallel"], "advanced",
        "FFmpeg 并行渲染分段数", minimum=1,
    ),
    ParamSpec("hit_flash_ms", "击打反馈", "float", _DEFAULTS["hit_flash_ms"], "advanced", "击打反馈持续时间", unit="ms"),
    ParamSpec("tail_ms", "尾帧长度", "float", _DEFAULTS["tail_ms"], "advanced", "谱面结束后的尾帧长度", unit="ms"),
    ParamSpec("pre_roll_ms", "片头空白", "optional_float", _DEFAULTS["pre_roll_ms"], "advanced", "留空时自动计算", unit="ms"),
    ParamSpec("scroll_ar_override", "AR 覆盖", "optional_float", _DEFAULTS["scroll_ar_override"], "advanced", "留空时使用谱面 AR"),
    ParamSpec(
        "background_dim", "背景压暗", "float", _DEFAULTS["background_dim"], "advanced",
        "背景压暗比例", minimum=0.0, maximum=1.0,
    ),
    ParamSpec(
        "letterbox_in_breaks", "休息段黑边", "choice", _DEFAULTS["letterbox_in_breaks"], "advanced",
        "跟随谱面或强制覆盖", choices=_LETTERBOX_CHOICES,
    ),
    ParamSpec("receptor_pressed", "接收器固定按下", "bool", _DEFAULTS["receptor_pressed"], "advanced", "强制使用按下状态的 receptor"),
    ParamSpec("show_hold_tail", "显示 LN 尾端", "bool", _DEFAULTS["show_hold_tail"], "display", "绘制长按音符尾端"),
    ParamSpec("show_health_bar", "显示血条", "bool", _DEFAULTS["show_health_bar"], "display", "绘制 legacy 血条"),
    ParamSpec("show_barlines", "显示小节线", "bool", _DEFAULTS["show_barlines"], "display", "绘制小节线"),
    ParamSpec(
        "show_song_progress", "歌曲进度", "choice", _DEFAULTS["show_song_progress"],
        "display", "选择 osu!stable 歌曲进度样式", choices=_SONG_PROGRESS_CHOICES,
    ),
    ParamSpec("show_hit_error", "显示误差条", "bool", _DEFAULTS["show_hit_error"], "display", "绘制 hit error meter"),
    ParamSpec("show_warning_arrow", "显示警告箭头", "bool", _DEFAULTS["show_warning_arrow"], "display", "绘制开始与休息段警告箭头"),
)

RENDER_PARAM_KEYS = tuple(spec.key for spec in RENDER_PARAMS)


def _output_candidate(output_file, output_root=None):
    path = str(output_file or "output.mp4").strip().strip('"')
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path) and output_root:
        path = os.path.join(os.path.abspath(output_root), path)
    if not os.path.splitext(path)[1]:
        path += ".mp4"
    return os.path.abspath(path)


class RenderVideoController:
    @staticmethod
    def _beatmap_prefill(beatmap_path):
        from osu_toolbox import parse_osu_file

        beatmap_path = os.path.abspath(os.fspath(beatmap_path))
        if not os.path.isfile(beatmap_path):
            raise ParamValidationError("beatmap_path", f"谱面不存在：{beatmap_path}")
        if os.path.splitext(beatmap_path)[1].lower() != ".osu":
            raise ParamValidationError("beatmap_path", "谱面文件必须是 .osu 文件")
        beatmap = parse_osu_file(beatmap_path)
        background_path = None
        if beatmap.background:
            candidate = os.path.abspath(os.path.join(
                os.path.dirname(beatmap_path),
                beatmap.background,
            ))
            if os.path.isfile(candidate):
                background_path = candidate
        return {
            "beatmap_path": beatmap_path,
            "key_count": max(1, int(round(beatmap.circle_size))),
            "background_path": background_path,
        }

    def prefill(self, raw_params, changed_key, context):
        from osu_toolbox import find_beatmap_for_replay, parse_osr_file

        raw_params = raw_params if isinstance(raw_params, dict) else {}
        if changed_key == "beatmap_path":
            return PrefillResult(self._beatmap_prefill(raw_params.get("beatmap_path")))
        if changed_key != "replay_path":
            return PrefillResult({})
        replay_path = os.path.abspath(os.fspath(raw_params.get("replay_path") or ""))
        if not os.path.isfile(replay_path):
            raise ParamValidationError("replay_path", f"回放不存在：{replay_path}")
        if os.path.splitext(replay_path)[1].lower() != ".osr":
            raise ParamValidationError("replay_path", "回放文件必须是 .osr 文件")
        replay = parse_osr_file(replay_path)
        matched = find_beatmap_for_replay(
            replay.beatmap_md5,
            replay_path,
            osu_directory=context.global_settings.get("osu_directory"),
            malody_directory=context.global_settings.get("malody_directory"),
        )
        if not matched:
            return PrefillResult({}, ("未找到与回放 MD5 匹配的本地谱面，请手动选择谱面。",))
        return PrefillResult(self._beatmap_prefill(matched))

    def prepare(self, raw_params, context):
        from osu_toolbox import load_skin_config, scroll_direction_conflict

        params = coerce_params(RENDER_PARAMS, raw_params)
        beatmap_path = params["beatmap_path"]
        if not beatmap_path:
            raise ParamValidationError("beatmap_path", "请选择 .osu 谱面文件")
        if not os.path.isfile(beatmap_path):
            raise ParamValidationError("beatmap_path", f"谱面不存在: {beatmap_path}")
        params["beatmap_path"] = os.path.abspath(beatmap_path)
        replay_path = params["replay_path"]
        if replay_path:
            if not os.path.isfile(replay_path):
                raise ParamValidationError("replay_path", f"回放不存在: {replay_path}")
            params["replay_path"] = os.path.abspath(replay_path)
        if not params["skin_dir"]:
            raise ParamValidationError("skin_dir", "请选择自定义皮肤目录")
        output_root = context.output_root or os.path.dirname(params["beatmap_path"])
        params["output_file"] = _output_candidate(params["output_file"], output_root)
        params["replay_directories"] = dict(context.global_settings)

        warnings = []
        if params["background_path"]:
            background_path = os.path.abspath(params["background_path"])
            if os.path.isfile(background_path):
                params["background_path"] = background_path
            else:
                warnings.append(
                    f"背景图不存在，已按未填写处理：{background_path}",
                )
                params["background_path"] = None
        try:
            skin = load_skin_config(params["skin_dir"], params["key_count"] or 4)
            params["skin_dir"] = skin.root
            conflict = scroll_direction_conflict(params["scroll_direction"], skin)
            if conflict:
                warnings.append(f"{conflict}\n已按所选方向继续渲染。")
        except Exception as exc:
            raise ParamValidationError("skin_dir", str(exc))

        confirmation = None
        if os.path.exists(params["output_file"]):
            confirmation = ConfirmationRequest(
                title="覆盖输出文件",
                message=f"覆盖已有文件？\n{params['output_file']}",
                confirm_label="覆盖",
            )
        return PreparedRun(params, tuple(warnings), confirmation)

    def run(self, prepared, emit_event, cancel_event=None):
        params = prepared.params

        def log(message):
            emit_event("log", str(message))

        def progress(done, total):
            if total <= 0:
                return
            emit_event("progress", {
                "current": done,
                "total": total,
                "fraction": max(0.0, min(1.0, float(done) / float(total))),
            })

        log("正在加载渲染组件……")
        from osu_toolbox import (
            SkinAssets, find_audio_file, find_beatmap_for_replay, iter_frame_states,
            load_skin_config, parse_osr_file, parse_osu_file,
            render_frame_states_to_video, RenderCancelledError, resolve_stage_layout,
            resolve_scroll, stage_geometry,
        )

        config.configure_runtime(
            compatibility_mode=params["compatibility_mode"],
            fps=params["fps"],
            width=params["width"],
            height=params["height"],
            n_parallel=params["n_parallel"],
            pre_roll_ms=params["pre_roll_ms"],
            scroll_speed=params["scroll_speed"],
            scroll_direction=params["scroll_direction"],
            scroll_ar_override=params["scroll_ar_override"],
            scroll_speed_mult=params["scroll_speed_mult"],
            hit_flash_ms=params["hit_flash_ms"],
            tail_ms=params["tail_ms"],
            show_hold_tail=params["show_hold_tail"],
            show_health_bar=params["show_health_bar"],
            show_barlines=params["show_barlines"],
            show_song_progress=params["show_song_progress"],
            show_hit_error=params["show_hit_error"],
            show_warning_arrow=params["show_warning_arrow"],
            background_path=params["background_path"],
            background_dim=params["background_dim"],
            letterbox_in_breaks=params["letterbox_in_breaks"],
            receptor_pressed=params["receptor_pressed"],
        )

        beatmap_path = params["beatmap_path"]
        log(f"正在读取谱面：{beatmap_path}")
        beatmap = parse_osu_file(beatmap_path)
        if beatmap.mode != 3:
            raise ValueError(f"需要 osu!mania 谱面（Mode=3），实际为 Mode={beatmap.mode}")

        replay = None
        if params.get("replay_path"):
            log(f"正在读取回放：{params['replay_path']}")
            replay = parse_osr_file(params["replay_path"])
            if replay.mode != 3:
                raise ValueError(f"需要 osu!mania 回放（mode=3），实际为 mode={replay.mode}")
            matched = find_beatmap_for_replay(
                replay.beatmap_md5,
                params["replay_path"],
                osu_directory=params["replay_directories"].get("osu_directory"),
                malody_directory=params["replay_directories"].get("malody_directory"),
            )
            if matched:
                log(f"已按回放 MD5 匹配谱面：{matched}")
                beatmap_path = matched
                beatmap = parse_osu_file(matched)
            else:
                log(f"警告：未找到匹配回放 MD5 的谱面：{replay.beatmap_md5}")

        beatmap_keys = max(1, int(round(beatmap.circle_size)))
        keys = params["key_count"] or beatmap_keys
        skin_config = load_skin_config(
            params["skin_dir"],
            keys,
            beatmap.special_style,
            beatmap_root=os.path.dirname(os.path.abspath(beatmap_path)),
        )
        skin_ini_path = getattr(skin_config, "skin_ini_path", None)
        skin_mania_keys = getattr(skin_config, "skin_mania_keys", ())
        skin_refs = {
            "note": skin_config.note[0] if skin_config.note else None,
            "key": skin_config.key_image[0] if skin_config.key_image else None,
            "stage": skin_config.stage_left,
        }
        log(
            f"皮肤：{params['skin_dir']} "
            f"（skin.ini={skin_ini_path or '缺失'}，键数={keys}，"
            f"Mania 区块={skin_mania_keys}，资源={skin_refs}，"
            f"列起点={skin_config.column_start}，"
            f"列宽={skin_config.column_width}）"
        )
        if not skin_ini_path:
            log(
                "警告：未找到 skin.ini，正在使用默认 Mania 配置和默认资源名称。"
            )
        elif not getattr(skin_config, "skin_mania_config_found", False):
            log(
                f"警告：skin.ini 中没有 [Mania] Keys: {keys} 区块，"
                "正在使用默认 Mania 配置。"
            )

        if config.BACKGROUND_PATH is None and beatmap.background:
            background_path = os.path.join(
                os.path.dirname(os.path.abspath(beatmap_path)),
                beatmap.background,
            )
            if os.path.isfile(background_path):
                config.configure_runtime(background_path=background_path)
                log(f"背景图：{beatmap.background}")

        geometry = stage_geometry(params["height"], skin_config)
        stage_layout = resolve_stage_layout(
            params["width"],
            params["height"],
            keys,
            skin_config,
        )
        warning_arrow_height_480 = (
            SkinAssets.warning_arrow_height_480(
                skin_config,
                stage_layout.scale_factor,
            )
            if config.SHOW_WARNING_ARROW
            else 0.0
        )
        _, _, scroll_descriptor = resolve_scroll(
            geometry.y_spawn,
            geometry.y_judge,
            beatmap.approach_rate,
        )
        log(
            f"{beatmap.artist} - {beatmap.title} [{beatmap.version}] | "
            f"{len(beatmap.hit_objects)} 个音符（{scroll_descriptor}）"
        )

        if replay is not None:
            log("正在根据回放生成帧状态……")
        else:
            log("正在根据谱面 Auto 生成帧状态……")
        started_at = time.time()
        frame_states, start_song_ms = iter_frame_states(
            beatmap,
            keys,
            geometry.y_spawn,
            geometry.y_judge,
            params["fps"],
            replay=replay,
            height=params["height"],
            skin_config=skin_config,
            warning_arrow_height_480=warning_arrow_height_480,
        )
        max_frames = len(frame_states)
        log(
            f"共 {max_frames} 帧（{max_frames / params['fps']:.1f} 秒），"
            f"帧状态生成耗时 {time.time() - started_at:.2f} 秒"
        )
        if max_frames:
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelledError("任务已取消")
            progress(1, 3)
            audio_path = find_audio_file(beatmap_path, beatmap.audio_filename)
            try:
                ok = render_frame_states_to_video(
                    frame_states,
                    keys,
                    skin_config,
                    params["output_file"],
                    fps=params["fps"],
                    width=params["width"],
                    height=params["height"],
                    n_parallel=params["n_parallel"],
                    audio_path=audio_path,
                    start_song_ms=start_song_ms,
                    song_progress_mode=params["show_song_progress"],
                    progress_cb=lambda done, total: progress(
                        1.0 + (float(done) / float(total) if total else 0.0),
                        3.0,
                    ),
                    cancel_event=cancel_event,
                    status_cb=log,
                    allow_overwrite=bool(params.get("_allow_overwrite")),
                )
            except RenderCancelledError as exc:
                raise ToolCancelledError(str(exc)) from exc
            if not ok:
                raise RuntimeError("视频导出失败")
            progress(3, 3)

        return ToolResult("file", {
            "path": params["output_file"],
            "name": os.path.basename(params["output_file"]),
            "label": "输出视频",
        })


RENDER_VIDEO_TOOL = ToolDefinition(
    spec=ToolSpec(
        id="render",
        title="渲染视频",
        description="将 osu!mania 谱面或回放渲染为 MP4 视频",
        category="视频",
        action_label="开始渲染",
        groups=RENDER_GROUPS,
        params=RENDER_PARAMS,
        result_kind="file",
        concurrency="exclusive",
    ),
    controller=RenderVideoController(),
)
