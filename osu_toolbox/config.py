"""运行时配置。

所有随入口参数变化的全局设置集中在此，`configure_runtime` 同步入口设置并
在末尾重算缩放派生常量。拆分后的各子模块统一用 `config.X` 属性访问，避免
`from config import X` 在 `configure_runtime` 后拿到过期快照。
"""

_UNSET = object()

COMPAT_STABLE_REPLAY = "stable-replay"
COMPAT_LAZER_CLASSIC = "lazer-classic"

SONG_PROGRESS_OFF = "off"
SONG_PROGRESS_PIE = "pie"
SONG_PROGRESS_TOP_RIGHT = "top_right"
SONG_PROGRESS_BOTTOM_RIGHT = "bottom_right"
SONG_PROGRESS_BOTTOM = "bottom"
SONG_PROGRESS_MODES = frozenset((
    SONG_PROGRESS_OFF,
    SONG_PROGRESS_PIE,
    SONG_PROGRESS_TOP_RIGHT,
    SONG_PROGRESS_BOTTOM_RIGHT,
    SONG_PROGRESS_BOTTOM,
))

# Runtime and tool defaults share this mapping. The registered render tool uses
# these values when it builds its parameter schema.
DEFAULT_RENDER_PARAMS = {
    "beatmap_path": "",
    "replay_path": None,
    "skin_dir": None,
    "background_path": None,
    "output_file": "output.mp4",
    "key_count": None,
    "fps": 60,
    "width": 960,
    "height": 540,
    "compatibility_mode": COMPAT_STABLE_REPLAY,
    "scroll_speed": 28.0,
    "scroll_direction": "auto",
    "scroll_speed_mult": 1.0,
    "n_parallel": 4,
    "hit_flash_ms": 80.0,
    "tail_ms": 2000.0,
    "pre_roll_ms": None,
    "scroll_ar_override": None,
    "background_dim": 0.8,
    "letterbox_in_breaks": None,
    "receptor_pressed": False,
    "show_hold_tail": True,
    "show_health_bar": True,
    "show_barlines": True,
    "show_song_progress": SONG_PROGRESS_PIE,
    "show_hit_error": True,
    "show_warning_arrow": True,
}

KEY_COUNT = 4
FPS = DEFAULT_RENDER_PARAMS["fps"]
WIDTH, HEIGHT = DEFAULT_RENDER_PARAMS["width"], DEFAULT_RENDER_PARAMS["height"]
# 像素常量基于 480p 设计，统一按高度等比缩放（osu! 原生做法）
SCALE = HEIGHT / 480.0
# 没有逐帧按键状态时的接收器默认状态。正常导出会按列动态切换 KeyImage/KeyImageD。
RECEPTOR_PRESSED = DEFAULT_RENDER_PARAMS["receptor_pressed"]
N_PARALLEL = DEFAULT_RENDER_PARAMS["n_parallel"]
HIT_FLASH_MS = DEFAULT_RENDER_PARAMS["hit_flash_ms"]
TAIL_MS = DEFAULT_RENDER_PARAMS["tail_ms"]  # 尾帧长度
# 额外片头空白（毫秒），叠加在自动对齐之上；None 则自动（早谱面补 scroll、晚谱面跳过空白）
PRE_ROLL_MS = DEFAULT_RENDER_PARAMS["pre_roll_ms"]
COMPATIBILITY_MODE = DEFAULT_RENDER_PARAMS["compatibility_mode"]
# 下落速度：None 使用谱面 AR；>1 更快，<1 更慢
SCROLL_SPEED = DEFAULT_RENDER_PARAMS["scroll_speed"]
# 滚动方向：auto 跟随皮肤 UpsideDown / down 下落 / up 上落。
# 显式方向与皮肤声明不一致时由 GUI Bridge 提示并继续渲染。
SCROLL_DIRECTION = DEFAULT_RENDER_PARAMS["scroll_direction"]
SCROLL_AR_OVERRIDE = DEFAULT_RENDER_PARAMS["scroll_ar_override"]
SCROLL_SPEED_MULT = DEFAULT_RENDER_PARAMS["scroll_speed_mult"]
# LN 尾端：False 时不绘制尾部标记，仅保留 head + body
SHOW_HOLD_TAIL = DEFAULT_RENDER_PARAMS["show_hold_tail"]
SHOW_HEALTH_BAR = DEFAULT_RENDER_PARAMS["show_health_bar"]
SHOW_BARLINES = DEFAULT_RENDER_PARAMS["show_barlines"]
SONG_PROGRESS_MODE = DEFAULT_RENDER_PARAMS["show_song_progress"]
# Compatibility flag for callers that only distinguish visible and hidden.
SHOW_SONG_PROGRESS = SONG_PROGRESS_MODE != SONG_PROGRESS_OFF
SHOW_HIT_ERROR = DEFAULT_RENDER_PARAMS["show_hit_error"]
SHOW_WARNING_ARROW = DEFAULT_RENDER_PARAMS["show_warning_arrow"]
# 背景图路径；None 时若谱面 [Events] 声明了背景则自动取谱面目录下的文件
BACKGROUND_PATH = DEFAULT_RENDER_PARAMS["background_path"]
BACKGROUND_DIM = DEFAULT_RENDER_PARAMS["background_dim"]
# None = 跟随谱面 [General] LetterboxInBreaks；True/False 强制覆盖
LETTERBOX_IN_BREAKS = DEFAULT_RENDER_PARAMS["letterbox_in_breaks"]
MANIA_JUDGEMENT_MAX_STAGE_WIDTH_RATIO = 0.56
HIT_LIGHT_H = int(round(96 * SCALE))
HIT_LIGHT_MIN_H = int(round(32 * SCALE))

NUM_FRAME_BUFFERS = 3
FRAME_QUEUE_SIZE = 2
ENCODER_SHUTDOWN_TIMEOUT_SEC = 10
SEGMENT_TIMEOUT_MIN_SEC = 180

HOLD_CAP_H = int(round(16 * SCALE))
HOLD_TAIL_H = int(round(6 * SCALE))  # 尾端高度
TAP_NOTE_H = int(round(14 * SCALE))
HOLD_CAP_MARGIN_RATIO = 0.12
HOLD_TAIL_MARGIN_RATIO = 0.08
HOLD_BODY_MARGIN_RATIO = 0.12  # 实心头体宽度占比
HOLD_HEAD_ALPHA = 0.9
HOLD_TAIL_ALPHA = 0.55
HOLD_BODY_ALPHA = 0.3


def configure_runtime(
    *,
    compatibility_mode=_UNSET,
    fps=_UNSET,
    width=_UNSET,
    height=_UNSET,
    pre_roll_ms=_UNSET,
    scroll_speed=_UNSET,
    scroll_direction=_UNSET,
    scroll_ar_override=_UNSET,
    scroll_speed_mult=_UNSET,
    hit_flash_ms=_UNSET,
    tail_ms=_UNSET,
    show_hold_tail=_UNSET,
    show_health_bar=_UNSET,
    show_barlines=_UNSET,
    show_song_progress=_UNSET,
    show_hit_error=_UNSET,
    show_warning_arrow=_UNSET,
    background_path=_UNSET,
    background_dim=_UNSET,
    letterbox_in_breaks=_UNSET,
    receptor_pressed=_UNSET,
    n_parallel=_UNSET,
):
    """Synchronize mutable entry-point settings with scale-derived constants."""
    global FPS, WIDTH, HEIGHT, SCALE, N_PARALLEL
    global COMPATIBILITY_MODE, PRE_ROLL_MS, SCROLL_SPEED, SCROLL_DIRECTION
    global SCROLL_AR_OVERRIDE, SCROLL_SPEED_MULT
    global HIT_FLASH_MS, TAIL_MS, SHOW_HOLD_TAIL, SHOW_HEALTH_BAR, SHOW_BARLINES, RECEPTOR_PRESSED
    global SONG_PROGRESS_MODE, SHOW_SONG_PROGRESS, SHOW_HIT_ERROR, SHOW_WARNING_ARROW
    global BACKGROUND_PATH, BACKGROUND_DIM, LETTERBOX_IN_BREAKS
    global HIT_LIGHT_H, HIT_LIGHT_MIN_H, HOLD_CAP_H, HOLD_TAIL_H, TAP_NOTE_H

    if fps is not _UNSET:
        FPS = int(fps)
    if width is not _UNSET:
        WIDTH = int(width)
    if height is not _UNSET:
        HEIGHT = int(height)
    if n_parallel is not _UNSET:
        N_PARALLEL = int(n_parallel)
    if compatibility_mode is not _UNSET:
        compatibility_mode = str(compatibility_mode).strip().lower()
        if compatibility_mode not in (COMPAT_STABLE_REPLAY, COMPAT_LAZER_CLASSIC):
            raise ValueError(f"unsupported compatibility mode: {compatibility_mode}")
        COMPATIBILITY_MODE = compatibility_mode
    if pre_roll_ms is not _UNSET:
        PRE_ROLL_MS = pre_roll_ms
    if scroll_speed is not _UNSET:
        SCROLL_SPEED = max(1.0, min(40.0, float(scroll_speed)))
    if scroll_direction is not _UNSET:
        scroll_direction = str(scroll_direction).strip().lower()
        if scroll_direction not in ("auto", "down", "up"):
            raise ValueError(f"unsupported scroll direction: {scroll_direction}")
        SCROLL_DIRECTION = scroll_direction
    if scroll_ar_override is not _UNSET:
        SCROLL_AR_OVERRIDE = scroll_ar_override
    if scroll_speed_mult is not _UNSET:
        SCROLL_SPEED_MULT = float(scroll_speed_mult)
    if hit_flash_ms is not _UNSET:
        HIT_FLASH_MS = float(hit_flash_ms)
    if tail_ms is not _UNSET:
        TAIL_MS = float(tail_ms)
    if show_hold_tail is not _UNSET:
        SHOW_HOLD_TAIL = bool(show_hold_tail)
    if show_health_bar is not _UNSET:
        SHOW_HEALTH_BAR = bool(show_health_bar)
    if show_barlines is not _UNSET:
        SHOW_BARLINES = bool(show_barlines)
    if show_song_progress is not _UNSET:
        SONG_PROGRESS_MODE = normalize_song_progress_mode(show_song_progress)
        SHOW_SONG_PROGRESS = SONG_PROGRESS_MODE != SONG_PROGRESS_OFF
    if show_hit_error is not _UNSET:
        SHOW_HIT_ERROR = bool(show_hit_error)
    if show_warning_arrow is not _UNSET:
        SHOW_WARNING_ARROW = bool(show_warning_arrow)
    if background_path is not _UNSET:
        BACKGROUND_PATH = background_path
    if background_dim is not _UNSET:
        BACKGROUND_DIM = max(0.0, min(1.0, float(background_dim)))
    if letterbox_in_breaks is not _UNSET:
        LETTERBOX_IN_BREAKS = (
            None if letterbox_in_breaks is None else bool(letterbox_in_breaks)
        )
    if receptor_pressed is not _UNSET:
        RECEPTOR_PRESSED = bool(receptor_pressed)

    SCALE = HEIGHT / 480.0
    HIT_LIGHT_H = int(round(96 * SCALE))
    HIT_LIGHT_MIN_H = int(round(32 * SCALE))
    HOLD_CAP_H = int(round(16 * SCALE))
    HOLD_TAIL_H = int(round(6 * SCALE))
    TAP_NOTE_H = int(round(14 * SCALE))


def normalize_song_progress_mode(value):
    """Return the canonical Stable song-progress mode, accepting legacy bools."""
    if isinstance(value, bool):
        return SONG_PROGRESS_PIE if value else SONG_PROGRESS_OFF

    compact = str(value).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    aliases = {
        "off": SONG_PROGRESS_OFF,
        "pie": SONG_PROGRESS_PIE,
        "topright": SONG_PROGRESS_TOP_RIGHT,
        "bottomright": SONG_PROGRESS_BOTTOM_RIGHT,
        "bottom": SONG_PROGRESS_BOTTOM,
    }
    try:
        return aliases[compact]
    except KeyError:
        raise ValueError(f"unsupported song progress mode: {value}")
