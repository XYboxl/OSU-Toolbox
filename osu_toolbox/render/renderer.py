"""ManiaRenderer：legacy mania 逐帧合成渲染器。"""

import math
from collections import OrderedDict

import numpy as np
import pygame

from .. import config
from ..legacy_constants import (
    LEGACY_ACCURACY_COUNTER_SCALE,
    LEGACY_COLUMN_LINE_WIDTH_SCALE,
    LEGACY_COMBO_GHOST_MS,
    LEGACY_FIXED_WIDTH_EXCLUDE_CHARS,
    LEGACY_MANIA_HEALTH_BG_OFFSET_X_480,
    LEGACY_MANIA_HEALTH_NEW_OFFSET_X_480,
    LEGACY_MANIA_HEALTH_NEW_OFFSET_Y_480,
    LEGACY_MANIA_HEALTH_OLD_OFFSET_X_480,
    LEGACY_MANIA_HEALTH_OLD_OFFSET_Y_480,
    LEGACY_MANIA_HEALTH_ROTATION_DEG,
    LEGACY_MANIA_HEALTH_SPRITE_SCALE,
    LEGACY_HIT_ERROR_BAR_HEIGHT_768,
    LEGACY_HIT_ERROR_BAR_WIDTH_768,
    LEGACY_HIT_ERROR_BOTTOM_MARGIN_768,
    LEGACY_HIT_ERROR_CHEVRON_768,
    LEGACY_HIT_ERROR_COLOUR_GREAT,
    LEGACY_HIT_ERROR_COLOUR_MEH,
    LEGACY_HIT_ERROR_COLOUR_OK,
    LEGACY_HIT_ERROR_TICK_WIDTH_768,
    LEGACY_JUDGEMENT_MISS_ROTATION_MS,
    LEGACY_LETTERBOX_HEIGHT_RATIO,
    LEGACY_NOTE_BODY_REPEAT,
    LEGACY_NOTE_BODY_REPEAT_BOTTOM,
    LEGACY_NOTE_BODY_REPEAT_TOP,
    LEGACY_NOTE_BODY_REPEAT_TOP_AND_BOTTOM,
    LEGACY_NOTE_BODY_STRETCH,
    LEGACY_NOTE_BODY_STYLES,
    LEGACY_POSITION_SCALE_FACTOR,
    LEGACY_ROTATION_QUANT_DEG,
    LEGACY_SCORE_COUNTER_SCALE,
    LEGACY_SCORE_HUD_OFFSET_X_480,
    LEGACY_SCORE_NEW_SKIN_GAP_Y_480,
    LEGACY_SONG_PROGRESS_BORDER_768,
    LEGACY_SONG_PROGRESS_BOTTOM_RIGHT_WIDTH_480,
    LEGACY_SONG_PROGRESS_BOTTOM_RIGHT_X_480,
    LEGACY_SONG_PROGRESS_BOTTOM_RIGHT_Y_480,
    LEGACY_SONG_PROGRESS_BOTTOM_Y_480,
    LEGACY_SONG_PROGRESS_COLOUR,
    LEGACY_SONG_PROGRESS_DOT_768,
    LEGACY_SONG_PROGRESS_INITIAL_ACCURACY,
    LEGACY_SONG_PROGRESS_INTRO_COLOUR,
    LEGACY_SONG_PROGRESS_LINEAR_BACKGROUND,
    LEGACY_SONG_PROGRESS_LINEAR_FILL,
    LEGACY_SONG_PROGRESS_LINEAR_HEIGHT_480,
    LEGACY_SONG_PROGRESS_PIE_ARC_STEP_RAD,
    LEGACY_SONG_PROGRESS_PIE_OFFSET_X_480,
    LEGACY_SONG_PROGRESS_PIE_RADIUS_480,
    LEGACY_SONG_PROGRESS_SIZE_768,
    LEGACY_SONG_PROGRESS_SUPERSAMPLE,
    LEGACY_SONG_PROGRESS_TOP_RIGHT_SCORE_HEIGHT_480,
    LEGACY_SONG_PROGRESS_TOP_RIGHT_WIDTH_480,
    LEGACY_SONG_PROGRESS_TOP_RIGHT_X_480,
)
from ..legacy_math import (
    _easing_out_quad,
    _legacy_apply_with_doubled_alpha,
    _legacy_combo_ghost_state,
    _legacy_combo_burst_state,
    _legacy_combo_pop_scale_y,
    _legacy_combo_show_alpha,
    _legacy_disallow_zero_alpha,
    _legacy_health_flash_state,
    _legacy_hit_explosion_alpha,
    _legacy_hold_light_alpha,
    _legacy_hit_error_tick_alpha,
    _legacy_internal_screen_scale,
    _legacy_judgement_alpha,
    _legacy_judgement_line_colour,
    _legacy_judgement_scale,
    _legacy_key_warning_alpha,
    _legacy_deterministic_index,
    _legacy_miss_rotation_deg,
)
from ..mania.timeline import (
    _legacy_directional_position,
    _legacy_judgement_position,
    stage_geometry,
)
from ..skin.assets import SkinAssets
from .geometry import lane_geometry, resolve_stage_layout


# Stable's default mania keyboard layout. The renderer accepts explicit labels
# from replay/profile metadata; the exporter uses these when none are set.
_DEFAULT_MANIA_BINDINGS = {
    1: ("K",),
    2: ("D", "K"),
    3: ("D", "F", "K"),
    4: ("D", "F", "J", "K"),
    5: ("D", "F", "Space", "J", "K"),
    6: ("S", "D", "F", "J", "K", "L"),
    7: ("S", "D", "F", "Space", "J", "K", "L"),
    8: ("A", "S", "D", "F", "J", "K", "L", ";"),
}


class ManiaRenderer:
    def __init__(self, width=None, height=None, key_count=4, skin_config=None):
        if skin_config is None:
            raise ValueError("ManiaRenderer requires a custom SkinConfig")
        if width is None:
            width = config.WIDTH
        if height is None:
            height = config.HEIGHT
        if int(width) != config.WIDTH or int(height) != config.HEIGHT:
            config.configure_runtime(width=width, height=height)
        self.width = width
        self.height = height
        self.key_count = key_count
        self._buffers = [
            np.zeros((height, width, 3), dtype=np.uint8)
            for _ in range(config.NUM_FRAME_BUFFERS)
        ]
        self._buf_idx = 0
        self.frame = self._buffers[0]
        self._tmp_color = np.empty(3, dtype=np.float32)
        self.skin_config = skin_config
        self.geometry = stage_geometry(height, skin_config)
        self.lane_rects, self.lane_w = lane_geometry(width, height, key_count, skin_config)
        self.stage_layout = resolve_stage_layout(
            width, height, key_count, skin_config,
        )
        self.y_top = self.geometry.y_spawn
        self.y_judge = self.geometry.y_judge
        self.stage_y1 = self.geometry.stage_y1
        self.stage_y2 = self.geometry.stage_y2
        self.y_light = _legacy_directional_position(
            skin_config.light_position, height, self.geometry.upside_down,
        )
        self.y_score = _legacy_judgement_position(
            skin_config, height, self.geometry.upside_down,
        )
        # LegacyManiaComboCounter.updateAnchor: on upscroll the counter
        # anchors to the bottom edge with Y = -|ComboPosition|.
        combo_y = skin_config.combo_position * height / 480.0
        self.y_combo = (
            height - combo_y if self.geometry.upside_down else combo_y
        )
        self.stage_x1 = self.lane_rects[0][0]
        self.stage_x2 = self.lane_rects[-1][2]
        self.stage_cx = (self.stage_x1 + self.stage_x2) / 2
        self.stage_ranges = self.stage_layout.stage_ranges
        self.column_stage_indices = self.stage_layout.column_stage_indices
        self._image_transform_cache = OrderedDict()
        self._body_row_cache = OrderedDict()
        self._alpha_bounds_cache = {}
        self.assets = SkinAssets(
            skin_config,
            self.lane_rects,
            self.geometry.upside_down,
            self.stage_ranges,
            self.stage_layout.scale_factor,
        )
        self._build_bg_layer()
        self._init_static_hud()

    @staticmethod
    def _surface_to_rgba_array(surface):
        w, h = surface.get_size()
        data = pygame.image.tostring(surface, "RGBA")
        return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)

    def _blend_rect(self, x1, y1, x2, y2, color):
        x1, x2 = int(round(x1)), int(round(x2))
        y1, y2 = int(round(y1)), int(round(y2))
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(self.width, x2)
        y2 = min(self.height, y2)
        if x1 >= x2 or y1 >= y2:
            return
        r, g, b = int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        alpha = color[3] if len(color) > 3 else 1.0
        sub = self.frame[y1:y2, x1:x2]
        if alpha >= 0.99:
            sub[:] = (r, g, b)
        elif alpha > 0.001:
            inv = 1.0 - alpha
            # 复用预分配数组，避免每次调用分配临时对象
            self._tmp_color[0] = r * alpha
            self._tmp_color[1] = g * alpha
            self._tmp_color[2] = b * alpha
            sub[:] = (sub * inv + self._tmp_color).astype(np.uint8)

    def _draw_vline(self, x, y1, y2, color, width=1):
        self._blend_rect(x, y1, x + width, y2, color)

    def _blit_rgba_array(self, arr, x, y, w, h):
        """通用 RGBA blit，arr 为原始 RGBA 数组（未预乘）"""
        if w == 0 or h == 0:
            return
        x, y = int(x), int(y)
        x1 = max(0, x)
        y1 = max(0, y)
        sx = -min(0, x)
        sy = -min(0, y)
        x2 = min(self.width, x + w)
        y2 = min(self.height, y + h)
        if x1 >= x2 or y1 >= y2:
            return
        src = arr[sy:sy + y2 - y1, sx:sx + x2 - x1]
        dst = self.frame[y1:y2, x1:x2]
        a = src[:, :, 3:4] / 255.0
        dst[:] = (dst * (1 - a) + src[:, :, :3] * a).astype(np.uint8)

    def _blit_premul(self, premul_rgb, alpha, x, y, w, h):
        """高速 blit：使用预乘 alpha 缓存数组，跳过除法运算"""
        if w == 0 or h == 0:
            return
        x, y = int(x), int(y)
        x1 = max(0, x)
        y1 = max(0, y)
        sx = -min(0, x)
        sy = -min(0, y)
        x2 = min(self.width, x + w)
        y2 = min(self.height, y + h)
        if x1 >= x2 or y1 >= y2:
            return
        pm = premul_rgb[sy:sy + y2 - y1, sx:sx + x2 - x1]   # float32 预乘 RGB
        a  = alpha[sy:sy + y2 - y1, sx:sx + x2 - x1]         # float32 alpha (H,W,1)
        dst = self.frame[y1:y2, x1:x2]
        dst[:] = (dst * (1.0 - a) + pm).astype(np.uint8)

    def _blit_premul_additive(self, premul_rgb, alpha, x, y, w, h):
        if w == 0 or h == 0:
            return
        x, y = int(x), int(y)
        x1 = max(0, x)
        y1 = max(0, y)
        sx = -min(0, x)
        sy = -min(0, y)
        x2 = min(self.width, x + w)
        y2 = min(self.height, y + h)
        if x1 >= x2 or y1 >= y2:
            return
        pm = premul_rgb[sy:sy + y2 - y1, sx:sx + x2 - x1]
        dst = self.frame[y1:y2, x1:x2]
        dst[:] = np.minimum(255.0, dst.astype(np.float32) + pm).astype(np.uint8)

    @staticmethod
    def _rotate_premul(rgb_pm, a, w, h, rotation_deg):
        """逆映射最近邻旋转；返回 (rgb, a, w, h, dx, dy)，dx/dy 保持中心不动。"""
        rad = math.radians(rotation_deg)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        if abs(cos_r) < 1e-7:
            cos_r = 0.0
        elif abs(abs(cos_r) - 1.0) < 1e-7:
            cos_r = math.copysign(1.0, cos_r)
        if abs(sin_r) < 1e-7:
            sin_r = 0.0
        elif abs(abs(sin_r) - 1.0) < 1e-7:
            sin_r = math.copysign(1.0, sin_r)
        bw = max(1, int(math.ceil(abs(w * cos_r) + abs(h * sin_r))))
        bh = max(1, int(math.ceil(abs(w * sin_r) + abs(h * cos_r))))
        ox = np.arange(bw, dtype=np.float32) - (bw - 1) / 2.0
        oy = np.arange(bh, dtype=np.float32) - (bh - 1) / 2.0
        sx = ox[None, :] * cos_r + oy[:, None] * sin_r + (w - 1) / 2.0
        sy = -ox[None, :] * sin_r + oy[:, None] * cos_r + (h - 1) / 2.0
        ix = np.rint(sx).astype(np.int64)
        iy = np.rint(sy).astype(np.int64)
        valid = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        ix = np.clip(ix, 0, w - 1)
        iy = np.clip(iy, 0, h - 1)
        mask = valid[..., None].astype(np.float32)
        out_rgb = rgb_pm[iy, ix] * mask
        out_a = a[iy, ix] * mask
        dx = int(round((w - bw) / 2.0))
        dy = int(round((h - bh) / 2.0))
        return out_rgb, out_a, bw, bh, dx, dy

    def _blit_skin_image(self, img, x, y, override_h=None, alpha_scale=1.0, scale=1.0, additive=False, tint=None, scale_y=None, rotation=0.0, clip_w=None, clip_h=None):
        """blit 预乘皮肤图 (rgb_pm,a,w,h)；override_h 纵向拉伸，scale 等比缩放，
        scale_y 额外纵向缩放，rotation 绕图像中心旋转（度）。"""
        if img is None:
            return
        source_rgb_pm, source_a, source_w, source_h = img
        clip_w = source_w if clip_w is None else int(round(clip_w))
        clip_h = source_h if clip_h is None else int(round(clip_h))
        clip_w = max(0, min(source_w, clip_w))
        clip_h = max(0, min(source_h, clip_h))
        if clip_w <= 0 or clip_h <= 0:
            return
        override_h = (
            None if override_h is None
            else max(1, int(round(override_h)))
        )
        scale = 1.0 if scale is None else float(scale)
        if scale <= 0:
            return
        scale_y_eff = 1.0 if scale_y is None else float(scale_y)
        if scale_y_eff <= 0:
            return
        rotation_q = (
            round(float(rotation) / LEGACY_ROTATION_QUANT_DEG)
            * LEGACY_ROTATION_QUANT_DEG
        )
        alpha_scale = max(0.0, float(alpha_scale))
        effective_alpha = alpha_scale if alpha_scale < 0.999 else 1.0
        tint_key = (
            None if tint is None
            else tuple(round(float(value), 6) for value in tint)
        )
        cache_key = (
            id(img),
            clip_w,
            clip_h,
            override_h,
            round(scale, 6),
            round(scale_y_eff, 6),
            round(rotation_q, 6),
            tint_key,
            round(effective_alpha, 6),
        )
        cached = self._image_transform_cache.get(cache_key)
        if cached is not None:
            self._image_transform_cache.move_to_end(cache_key)
            rgb_pm, a, w, h, dx, dy = cached
        else:
            rgb_pm = source_rgb_pm[:clip_h, :clip_w]
            a = source_a[:clip_h, :clip_w]
            w, h = clip_w, clip_h
            dx = dy = 0
            if override_h is not None and override_h != h and h > 0:
                ys = (np.arange(override_h) * h / override_h).astype(np.int64)
                ys = np.clip(ys, 0, h - 1)
                rgb_pm = rgb_pm[ys]
                a = a[ys]
                h = override_h
            if abs(scale - 1.0) > 0.001:
                tw = max(1, int(round(w * scale)))
                th = max(1, int(round(h * scale)))
                ys = (np.arange(th) * h / th).astype(np.int64)
                xs = (np.arange(tw) * w / tw).astype(np.int64)
                ys = np.clip(ys, 0, h - 1)
                xs = np.clip(xs, 0, w - 1)
                rgb_pm = rgb_pm[ys][:, xs]
                a = a[ys][:, xs]
                w, h = tw, th
            if abs(scale_y_eff - 1.0) > 0.001 and h > 0:
                th = max(1, int(round(h * scale_y_eff)))
                ys = (np.arange(th) * h / th).astype(np.int64)
                ys = np.clip(ys, 0, h - 1)
                rgb_pm = rgb_pm[ys]
                a = a[ys]
                h = th
            if abs(rotation_q) >= LEGACY_ROTATION_QUANT_DEG / 2.0:
                rgb_pm, a, w, h, dx, dy = self._rotate_premul(
                    rgb_pm, a, w, h, rotation_q,
                )
            if tint is not None:
                tint_rgb = np.array(tint[:3], dtype=np.float32).reshape(1, 1, 3)
                tint_alpha = float(tint[3]) if len(tint) > 3 else 1.0
                rgb_pm = rgb_pm * tint_rgb * tint_alpha
                a = a * tint_alpha
            if effective_alpha < 1.0:
                rgb_pm = rgb_pm * effective_alpha
                a = a * effective_alpha
            cached = (rgb_pm, a, w, h, dx, dy)
            self._image_transform_cache[cache_key] = cached
            self._image_transform_cache.move_to_end(cache_key)
            while len(self._image_transform_cache) > 384:
                self._image_transform_cache.popitem(last=False)
        if additive:
            self._blit_premul_additive(rgb_pm, a, x + dx, y + dy, w, h)
        else:
            self._blit_premul(rgb_pm, a, x + dx, y + dy, w, h)

    def _blit_skin_image_clipped(self, img, x, y, clip_w=None, clip_h=None, alpha_scale=1.0, tint=None, additive=False):
        if img is None:
            return
        rgb_pm, a, w, h = img
        clip_w = w if clip_w is None else int(round(clip_w))
        clip_h = h if clip_h is None else int(round(clip_h))
        clip_w = max(0, min(w, clip_w))
        clip_h = max(0, min(h, clip_h))
        if clip_w <= 0 or clip_h <= 0:
            return
        rgb_pm = rgb_pm[:clip_h, :clip_w]
        a = a[:clip_h, :clip_w]
        if tint is not None:
            tint_rgb = np.array(tint[:3], dtype=np.float32).reshape(1, 1, 3)
            tint_alpha = float(tint[3]) if len(tint) > 3 else 1.0
            rgb_pm = rgb_pm * tint_rgb * tint_alpha
            a = a * tint_alpha
        if alpha_scale < 0.999:
            alpha_scale = max(0.0, float(alpha_scale))
            rgb_pm = rgb_pm * alpha_scale
            a = a * alpha_scale
        if additive:
            self._blit_premul_additive(rgb_pm, a, x, y, clip_w, clip_h)
        else:
            self._blit_premul(rgb_pm, a, x, y, clip_w, clip_h)

    def _blit_skin_body(
        self,
        img,
        x,
        y,
        height,
        style=LEGACY_NOTE_BODY_REPEAT_BOTTOM,
        source_offset=0,
        full_height=None,
        repeat_anchor=None,
    ):
        if img is None:
            return
        rgb_pm, a, w, h = img
        height = max(1, int(round(height)))
        source_offset = max(0, int(round(source_offset)))
        full_height = max(height, int(round(full_height if full_height is not None else height)))
        try:
            style = int(style)
        except (TypeError, ValueError):
            style = LEGACY_NOTE_BODY_REPEAT_BOTTOM
        if style == LEGACY_NOTE_BODY_REPEAT or style not in LEGACY_NOTE_BODY_STYLES:
            style = LEGACY_NOTE_BODY_REPEAT_BOTTOM
        if repeat_anchor not in (None, "top", "bottom"):
            repeat_anchor = None
        cache_key = (h, height, int(style), source_offset, full_height, repeat_anchor)
        ys = self._body_row_cache.get(cache_key)
        if ys is None:
            rows = np.arange(height, dtype=np.int64)
            absolute_rows = rows + source_offset
            if style == LEGACY_NOTE_BODY_STRETCH:
                ys = ((rows + source_offset) * h / full_height).astype(np.int64)
                ys = np.clip(ys, 0, h - 1)
            elif repeat_anchor == "top":
                ys = absolute_rows % h
            elif repeat_anchor == "bottom":
                ys = (absolute_rows + h - (full_height % h)) % h
            elif style == LEGACY_NOTE_BODY_REPEAT_BOTTOM:
                ys = (absolute_rows + h - (full_height % h)) % h
            elif style == LEGACY_NOTE_BODY_REPEAT_TOP_AND_BOTTOM:
                top_rows = absolute_rows % h
                bottom_rows = (absolute_rows + h - (full_height % h)) % h
                ys = np.where(
                    absolute_rows < full_height / 2.0,
                    top_rows,
                    bottom_rows,
                )
            elif style == LEGACY_NOTE_BODY_REPEAT_TOP:
                ys = absolute_rows % h
            else:
                ys = (absolute_rows + h - (full_height % h)) % h
            self._body_row_cache[cache_key] = ys
            while len(self._body_row_cache) > 256:
                self._body_row_cache.popitem(last=False)
        else:
            self._body_row_cache.move_to_end(cache_key)
        self._blit_premul(rgb_pm[ys], a[ys], x, y, w, height)

    @staticmethod
    def _skin_image_alpha_bounds(img, threshold=0.001):
        if img is None:
            return None
        alpha = img[1][:, :, 0]
        ys, xs = np.where(alpha > threshold)
        if not len(xs):
            return None
        return (
            int(xs.min()),
            int(ys.min()),
            int(xs.max()) + 1,
            int(ys.max()) + 1,
        )

    def _glyph_bounds(self, img, threshold=0.001):
        # Memoised alpha bounds. Entries pin the image object so a recycled
        # id() can never alias a different array; lazily created because
        # tests build renderers via object.__new__ without __init__.
        if img is None:
            return None
        cache = getattr(self, "_alpha_bounds_cache", None)
        if cache is None:
            cache = self._alpha_bounds_cache = {}
        key = (id(img), threshold)
        entry = cache.get(key)
        if entry is None:
            entry = (img, self._skin_image_alpha_bounds(img, threshold))
            cache[key] = entry
        return entry[1]

    def _skin_image_visible(self, img, threshold=0.001):
        return self._glyph_bounds(img, threshold) is not None

    def _skin_text_glyph_size(self, glyphs, img, scale):
        w = getattr(img, "logical_width", img[2])
        h = getattr(img, "logical_height", img[3])
        # Transparent legacy glyphs still reserve their full canvas. Skins use
        # this deliberately (most often score-percent) to reposition the
        # accuracy counter and song-progress pie without drawing the glyph.
        return max(0.0, float(w) * scale), max(0.0, float(h) * scale)

    def _skin_text_advance(self, glyphs, ch, img, scale, fixed_width):
        w, _ = self._skin_text_glyph_size(glyphs, img, scale)
        if not fixed_width or ch in LEGACY_FIXED_WIDTH_EXCLUDE_CHARS:
            return w
        ref = glyphs.get("5")
        if ref is None:
            return w
        ref_w, _ = self._skin_text_glyph_size(glyphs, ref, scale)
        return ref_w or w

    def _skin_text_x_offset(self, glyphs, ch, img, scale, fixed_width):
        if not fixed_width or ch in LEGACY_FIXED_WIDTH_EXCLUDE_CHARS:
            return 0.0
        width, _ = self._skin_text_glyph_size(glyphs, img, scale)
        advance = self._skin_text_advance(
            glyphs, ch, img, scale, fixed_width,
        )
        return (advance - width) / 2.0

    def _measure_skin_text(self, glyphs, text, overlap, scale=1.0, fixed_width=False):
        if scale <= 0:
            return 0, 0
        overlap = float(overlap) * scale
        width = 0.0
        height = 0.0
        count = 0
        for ch in text:
            img = glyphs.get(ch)
            if img is None:
                continue
            w, h = self._skin_text_glyph_size(glyphs, img, scale)
            advance = self._skin_text_advance(glyphs, ch, img, scale, fixed_width)
            if count:
                width -= overlap
            width += advance
            if height == 0.0:
                height = h
            count += 1
        # Stable's fixed-width pass subtracts ScoreOverlap after every glyph;
        # normal sprite text only subtracts it between adjacent glyphs.
        if count and fixed_width:
            width -= overlap
        return max(0.0, width), height

    def _blit_skin_text(self, glyphs, text, x, y, overlap, scale=1.0, fixed_width=False, alpha_scale=1.0, tint=None, additive=False, scale_y=None):
        if scale <= 0:
            return
        overlap = float(overlap) * scale
        cx = float(x)
        first = True
        for ch in text:
            img = glyphs.get(ch)
            if img is None:
                continue
            if not first:
                cx -= overlap
            advance = self._skin_text_advance(glyphs, ch, img, scale, fixed_width)
            if self._skin_image_visible(img):
                draw_x = cx + self._skin_text_x_offset(
                    glyphs, ch, img, scale, fixed_width,
                )
                self._blit_skin_image(
                    img,
                    int(round(draw_x)),
                    int(round(y)),
                    scale=scale,
                    alpha_scale=alpha_scale,
                    tint=tint,
                    additive=additive,
                    scale_y=scale_y,
                )
            cx += advance
            first = False

    def _paint_background_image(self):
        """把谱面背景 cover 缩放并按 (1-dim) 静态烘焙进背景层。"""
        path = config.BACKGROUND_PATH
        if not path:
            return False
        try:
            surf = pygame.image.load(str(path))
        except Exception:
            return False
        sw, sh = surf.get_size()
        if sw <= 0 or sh <= 0:
            return False
        factor = max(self.width / float(sw), self.height / float(sh))
        tw = max(1, int(round(sw * factor)))
        th = max(1, int(round(sh * factor)))
        surf = SkinAssets._scale_surface(surf, (tw, th))
        arr = SkinAssets._to_rgba_from_surface(surf)
        x0 = max(0, (tw - self.width) // 2)
        y0 = max(0, (th - self.height) // 2)
        crop = arr[y0:y0 + self.height, x0:x0 + self.width, :3].astype(np.float32)
        if crop.shape[0] < self.height or crop.shape[1] < self.width:
            padded = np.zeros((self.height, self.width, 3), dtype=np.float32)
            padded[:crop.shape[0], :crop.shape[1]] = crop
            crop = padded
        brightness = max(0.0, 1.0 - config.BACKGROUND_DIM)
        self.frame[:] = (crop * brightness).astype(np.uint8)
        return True

    def _build_bg_layer(self):
        self.frame[:] = (13, 13, 26)
        self._paint_background_image()
        stage_top = 0
        stage_bottom = self.height
        for i, (x1, _, x2, _) in enumerate(self.lane_rects):
            bg_color = _legacy_apply_with_doubled_alpha(
                self.skin_config.colours[i],
            )
            self._blend_rect(x1, stage_top, x2, stage_bottom, bg_color)
        x1 = self.lane_rects[0][0]
        x2 = self.lane_rects[-1][2]
        y_top = stage_top
        y_bot = stage_bottom
        line_col = _legacy_apply_with_doubled_alpha(
            self.skin_config.colour_column_line,
        )
        if line_col[3] > 0:
            for i, (lx1, _, lx2, _) in enumerate(self.lane_rects):
                left_w = self.skin_config.column_line_width[
                    min(i, len(self.skin_config.column_line_width) - 1)
                ]
                if left_w > 0:
                    width_px = (
                        left_w
                        * _legacy_internal_screen_scale()
                        * LEGACY_COLUMN_LINE_WIDTH_SCALE
                    )
                    self._draw_vline(
                        lx1, y_top, y_bot, line_col,
                        max(1, int(round(width_px))),
                    )
                right_w = self.skin_config.column_line_width[
                    min(i + 1, len(self.skin_config.column_line_width) - 1)
                ]
                has_right_line = (
                    self.skin_config.version >= 2.4
                    or i == len(self.lane_rects) - 1
                )
                if has_right_line and right_w > 0:
                    width_px = (
                        right_w
                        * _legacy_internal_screen_scale()
                        * LEGACY_COLUMN_LINE_WIDTH_SCALE
                    )
                    self._draw_vline(
                        lx2, y_top, y_bot, line_col,
                        max(1, int(round(width_px))),
                    )

        stage_h = max(1, y_bot - y_top)
        for stage_x1, stage_x2 in self.stage_ranges:
            if self.assets.stage_left is not None:
                _, _, sw, _ = self.assets.stage_left
                self._blit_skin_image(
                    self.assets.stage_left,
                    int(round(stage_x1 - sw)),
                    int(round(y_top)),
                    override_h=stage_h,
                )
            if self.assets.stage_right is not None:
                self._blit_skin_image(
                    self.assets.stage_right,
                    int(round(stage_x2)),
                    int(round(y_top)),
                    override_h=stage_h,
                )
        y = self.y_judge
        if self.skin_config.judgement_line:
            line_col = _legacy_judgement_line_colour(
                self.skin_config.colour_judgement_line,
            )
            if line_col[3] > 0:
                for stage_x1, stage_x2 in self.stage_ranges:
                    self._blend_rect(
                        stage_x1, y, stage_x2, y + 1, line_col,
                    )
        self._bg_layer = self.frame.copy()

    def draw_receptors(self, pressed_cols=None):
        # LegacyKeyArea spans the full column. Key textures anchor to the
        # screen edge; their transparent canvas places the visible receptor at
        # HitPosition.
        y = 0.0 if self.geometry.upside_down else float(self.height)
        for i, (lx1, _, lx2, _) in enumerate(self.lane_rects):
            if pressed_cols is None:
                pressed = config.RECEPTOR_PRESSED
            elif isinstance(pressed_cols, int):
                pressed = bool(pressed_cols & (1 << i))
            else:
                pressed = i in pressed_cols
            rec = self.assets.receptor_down[i] if pressed and self.assets.receptor_down[i] is not None else self.assets.receptor[i]
            if rec is not None:
                _, _, rw, rh = rec
                cx = (lx1 + lx2) / 2
                top = self._hit_aligned_top(y, rh)
                self._blit_skin_image(rec, int(round(cx - rw / 2)), int(round(top)))

    def _init_static_hud(self):
        saved = self.frame
        self.frame = self._bg_layer
        self._init_song_progress_layout()
        self.frame = saved
        for buf in self._buffers:
            buf[:] = self._bg_layer

    def reset_frame(self):
        self.frame[:] = self._bg_layer

    def draw_stage_hints(self, time_ms=0.0):
        for stage_idx, (stage_x1, stage_x2) in enumerate(self.stage_ranges):
            stage_hint = self.assets.stage_hint_for_time(stage_idx, time_ms)
            if stage_hint is None:
                continue
            _, _, sw, sh = stage_hint
            cx = (stage_x1 + stage_x2) / 2.0
            self._blit_skin_image(
                stage_hint,
                int(round(cx - sw / 2.0)),
                int(round(self.y_judge - sh / 2.0)),
            )

    def draw_letterbox(self, fade=0.0):
        """break 期间的上下黑边；层级在背景之上、stage/HUD 之下（★近似）。"""
        fade = max(0.0, min(1.0, float(fade)))
        if fade <= 0.0:
            return
        bar_h = int(round(self.height * LEGACY_LETTERBOX_HEIGHT_RATIO))
        if bar_h <= 0:
            return
        self._blend_rect(0, 0, self.width, bar_h, (0.0, 0.0, 0.0, fade))
        self._blend_rect(
            0, self.height - bar_h, self.width, self.height,
            (0.0, 0.0, 0.0, fade),
        )

    def finish_frame(self):
        done = self.frame
        self._buf_idx = (self._buf_idx + 1) % config.NUM_FRAME_BUFFERS
        self.frame = self._buffers[self._buf_idx]
        return done

    def draw_barlines(self, barlines=None):
        if not config.SHOW_BARLINES or not barlines:
            return
        if self.skin_config.barline_height <= 0:
            return
        line_h = max(1, int(round(
            self.skin_config.barline_height * _legacy_internal_screen_scale()
        )))
        color = self.skin_config.colour_barline
        if color[3] <= 0:
            return
        for y in barlines:
            for stage_x1, stage_x2 in self.stage_ranges:
                top = float(y) - line_h / 2.0
                self._blend_rect(stage_x1, top, stage_x2, top + line_h, color)

    def draw_stage_lights(self, light_states=None, animation_time_ms=0.0):
        if light_states is None:
            return
        for i, (x1, _, x2, _) in enumerate(self.lane_rects):
            if i >= len(light_states) or light_states[i] is None:
                continue
            alpha, scale_y, age_ms = light_states[i]
            if alpha <= 0.0 or scale_y <= 0.0:
                continue
            img = self.assets.stage_light_for_age(i, age_ms)
            if img is None:
                continue
            _, _, sw, sh = img
            draw_h = max(1, int(round(sh * scale_y)))
            cx = (x1 + x2) / 2
            draw_y = self.y_light if self.geometry.upside_down else self.y_light - draw_h
            self._blit_skin_image(
                img,
                int(round(cx - sw / 2)),
                int(round(draw_y)),
                override_h=draw_h,
                alpha_scale=alpha,
            )

    def draw_stage_foreground(self, age_ms=0.0):
        img = self.assets.stage_bottom_for_age(age_ms)
        if img is None:
            return
        _, _, sw, sh = img
        by = 0 if self.geometry.upside_down else self.height - sh
        for stage_x1, stage_x2 in self.stage_ranges:
            bx = int(round((stage_x1 + stage_x2) / 2 - sw / 2))
            self._blit_skin_image(img, bx, by)

    def _hit_origin_is_top(self):
        return self.geometry.upside_down

    def _hit_aligned_top(self, y, h):
        # osu!mania anchors notes/receptors at the hit edge: bottom for downscroll, top for upscroll.
        return float(y) if self._hit_origin_is_top() else float(y) - float(h)

    def _hit_aligned_span(self, y, h):
        top = self._hit_aligned_top(y, h)
        return top, top + float(h)

    def _hit_aligned_center(self, y, h):
        top, bottom = self._hit_aligned_span(y, h)
        return (top + bottom) / 2

    def _tail_body_join_edge(self, tail_draw_y, tail, fallback_y):
        bounds = self._glyph_bounds(tail)
        if bounds is None:
            return float(fallback_y)
        # The legacy tail texture is inverted with the scroll direction. Join
        # at the body-facing visible edge so transparent padding and rounded
        # corners remain part of the tail instead of being filled by the body.
        row = bounds[1] if self._hit_origin_is_top() else bounds[3]
        return float(tail_draw_y + row)

    def _hold_tail_visible(self, tail_y, tail_h=None):
        # osu! clips hold tails through the hold-note container.  For the
        # immediate-mode renderer, gate only the SPAWN side so tall tail art
        # cannot appear before its anchor enters the playfield; the judgement
        # side stays open so missed/dropped holds can fall past the line
        # (screen clipping retires them naturally).
        tail_y = float(tail_y)
        if self._hit_origin_is_top():
            return tail_y <= self.stage_y2
        return self.stage_y1 <= tail_y

    def draw_skin_tap(self, col_idx, x1, x2, y, animation_age_ms=0.0):
        """Draw a custom-skin tap texture aligned to its hit edge."""
        note_for_age = getattr(self.assets, "note_for_age", None)
        img = (
            note_for_age(col_idx, animation_age_ms)
            if note_for_age is not None else self.assets.note[col_idx]
        )
        if img is None:
            return
        _, _, w, h = img
        cx = (x1 + x2) / 2
        top = self._hit_aligned_top(y, h)
        self._blit_skin_image(img, int(round(cx - w / 2)), int(round(top)))

    def draw_skin_hold(self, col_idx, x1, x2, head_y, tail_y, head_animation_age_ms=0.0, tail_animation_age_ms=None, is_holding=False, hold_animation_age_ms=0.0):
        """Draw custom-skin LN body, tail and head textures."""
        if tail_animation_age_ms is None:
            tail_animation_age_ms = head_animation_age_ms
        hold_piece_for_age = getattr(self.assets, "hold_piece_for_age", None)
        if hold_piece_for_age is None:
            head = self.assets.head[col_idx] or self.assets.note[col_idx]
            body = self.assets.body[col_idx]
            tail = self.assets.tail[col_idx]
        else:
            head = hold_piece_for_age(
                col_idx, "head", head_animation_age_ms,
            ) or self.assets.note_for_age(col_idx, head_animation_age_ms)
            body = hold_piece_for_age(
                col_idx, "body", hold_animation_age_ms,
                is_holding=is_holding,
            )
            tail = hold_piece_for_age(
                col_idx, "tail", tail_animation_age_ms,
            )
        if head is None and body is None and tail is None:
            return
        cx = (x1 + x2) / 2
        head_h = head[3] if head is not None else 0
        tail_h = tail[3] if tail is not None else 0
        tail_top, tail_bottom = self._hit_aligned_span(tail_y, tail_h)
        tail_draw_y = int(round(tail_top))
        tail_has_pixels = self._skin_image_visible(tail)
        tail_visible = (
            config.SHOW_HOLD_TAIL
            and tail_has_pixels
            and self._hold_tail_visible(tail_y, tail_h)
        )
        tail_body_edge = (
            self._tail_body_join_edge(tail_draw_y, tail, tail_y)
            if tail_has_pixels
            else tail_y
        )
        head_body_edge = self._hit_aligned_center(head_y, head_h) if head_h else head_y
        # Keep the head underlap, but stop at the tail's visible joining edge.
        # Extending to the tail centre fills its transparent rounded silhouette.
        if body is not None:
            _, _, bw, _ = body
            full_top = min(head_body_edge, tail_body_edge)
            full_bottom = max(head_body_edge, tail_body_edge)
            # 只裁出生侧；判定线一侧放开（屏幕裁剪负责离场），
            # 使未命中/断体的 LN 能越线下落。
            if self._hit_origin_is_top():
                top = full_top
                bot = min(self.stage_y2, full_bottom)
            else:
                top = max(self.stage_y1, full_top)
                bot = full_bottom
            seg_h = int(round(bot - top))
            if seg_h > 0:
                self._blit_skin_body(
                    body,
                    int(round(cx - bw / 2)),
                    int(round(top)),
                    seg_h,
                    self.skin_config.note_body_style[col_idx],
                    source_offset=top - full_top,
                    full_height=full_bottom - full_top,
                    repeat_anchor=(
                        None
                        if tail_has_pixels
                        else ("bottom" if self._hit_origin_is_top() else "top")
                    ),
                )
        if tail_visible:
            _, _, tw, th = tail
            self._blit_skin_image(tail, int(round(cx - tw / 2)), int(round(tail_draw_y)))
        if head is not None:
            _, _, hw, hh = head
            head_top = self._hit_aligned_top(head_y, hh)
            self._blit_skin_image(head, int(round(cx - hw / 2)), int(round(head_top)))

    def judgement_stage_indices(self, col_idx):
        """Return the stages that receive this column's judgement sprite."""
        if not self.stage_ranges:
            return ()
        if not self.skin_config.separate_score:
            return tuple(range(len(self.stage_ranges)))
        return (self.column_stage_indices[col_idx],)

    def draw_hit_flash(self, col_idx, alpha=0.5, is_hold=False, show_result=True, hit_age_ms=0.0, hit_value=320, event_time_ms=None, result_stage_indices=None):
        """判定线轻量击打反馈（单列一次 blend）"""
        x1, _, x2, _ = self.lane_rects[col_idx]
        cx = (x1 + x2) / 2
        if hit_value > 0:
            # Judgement explosions always use lightingN. lightingL is drawn by
            # draw_hold_lights while a hold remains active.
            explosion_alpha = _legacy_hit_explosion_alpha(hit_age_ms)
            img = self.assets.lighting_for_age(col_idx, False, hit_age_ms)
            if img is not None and explosion_alpha > 0.0:
                _, _, iw, ih = img
                self._blit_skin_image(
                    img,
                    int(round(cx - iw / 2)),
                    int(round(self.y_judge - ih / 2)),
                    alpha_scale=explosion_alpha,
                    additive=True,
                )
        result_img = (
            self.assets.hit_result_for_age(hit_age_ms, hit_value)
            if show_result else None
        )
        if result_img is not None:
            result_alpha = _legacy_judgement_alpha(hit_age_ms)
            if result_alpha <= 0.0:
                return False
            result_scale = _legacy_judgement_scale(hit_age_ms, hit_value)
            rotation = 0.0
            if hit_value <= 0 and event_time_ms is not None:
                target_deg = _legacy_miss_rotation_deg(event_time_ms, col_idx)
                rotation = target_deg * _easing_out_quad(
                    hit_age_ms / LEGACY_JUDGEMENT_MISS_ROTATION_MS,
                )
            _, _, rw, rh = result_img
            result_centres = (self.stage_cx,)
            if result_stage_indices is not None:
                result_centres = tuple(
                    (self.stage_ranges[index][0] + self.stage_ranges[index][1])
                    / 2.0
                    for index in result_stage_indices
                )
            for result_cx in result_centres:
                self._blit_skin_image(
                    result_img,
                    int(round(result_cx - rw * result_scale / 2)),
                    int(round(self.y_score - rh * result_scale / 2)),
                    alpha_scale=result_alpha,
                    scale=result_scale,
                    rotation=rotation,
                )
            return True
        return False

    def draw_hold_lights(self, hold_light_states=()):
        if not hold_light_states:
            return
        for col_idx, state in enumerate(hold_light_states):
            if state is None or col_idx >= len(self.lane_rects):
                continue
            phase, phase_age_ms = state[:2]
            animation_age_ms = state[2] if len(state) > 2 else phase_age_ms
            alpha = _legacy_hold_light_alpha(phase, phase_age_ms)
            if alpha <= 0.0:
                continue
            img = self.assets.hold_light_for_age(col_idx, animation_age_ms)
            if img is None:
                continue
            x1, _, x2, _ = self.lane_rects[col_idx]
            cx = (x1 + x2) / 2
            _, _, iw, ih = img
            self._blit_skin_image(
                img,
                int(round(cx - iw / 2)),
                int(round(self.y_judge - ih / 2)),
                alpha_scale=alpha,
                additive=True,
            )

    def _warning_arrow_image(self):
        """mania-warningarrow：倒板时垂直翻转（缓存翻转变体）。"""
        img = self.assets.warning_arrow
        if img is None:
            return None
        if not self.geometry.upside_down:
            return img
        flipped = getattr(self, "_warning_arrow_flipped", None)
        if flipped is None:
            rgb_pm, a, w, h = img
            flipped = (
                np.ascontiguousarray(rgb_pm[::-1]),
                np.ascontiguousarray(a[::-1]),
                w,
                h,
            )
            self._warning_arrow_flipped = flipped
        return flipped

    def draw_warning_arrows(self, positions=()):
        """Draw Stable's pre-play warning sprite at each stage centre."""
        if not config.SHOW_WARNING_ARROW or not positions:
            return
        img = self._warning_arrow_image()
        if img is None:
            return
        _, _, iw, ih = img
        for position in positions:
            draw_y = int(round(
                float(position) - ih
                if self.geometry.upside_down else float(position)
            ))
            for stage_x1, stage_x2 in self.stage_ranges:
                draw_x = int(round((stage_x1 + stage_x2 - iw) / 2.0))
                self._blit_skin_image(img, draw_x, draw_y)

    def draw_combo_burst(self, age_ms=-1.0, event_index=0):
        """Draw one Stable mania comboburst milestone.

        The event index is deliberately supplied by ``FrameStateIterable``;
        selecting a side or numbered variant here is therefore deterministic
        even when the video is rendered in multiple worker processes.
        """
        if age_ms is None or float(age_ms) < 0.0:
            return
        movement, alpha = _legacy_combo_burst_state(age_ms)
        if alpha <= 0.0:
            return
        style = int(getattr(self.skin_config, "combo_burst_style", 1))
        if style == 0:
            side = "left"
        elif style == 1:
            side = "right"
        elif style == 2:
            side = (
                "left"
                if _legacy_deterministic_index(event_index, 2, 0x51) == 0
                else "right"
            )
        else:
            return
        image_for_event = getattr(self.assets, "combo_burst_for_event", None)
        if image_for_event is not None:
            image = image_for_event(event_index, side)
        else:
            variants = getattr(self.assets, "combo_burst", ())
            image = variants[int(event_index) % len(variants)] if variants else None
        if image is None:
            return
        _, _, iw, ih = image
        stage_idx = 0
        if len(self.stage_ranges) > 1 and side == "right":
            stage_idx = len(self.stage_ranges) - 1
        stage_x1, stage_x2 = self.stage_ranges[stage_idx]
        # Stable interpolates between the stage's horizontal edges.  The
        # sprite origin is BottomRight on the left and BottomLeft on the
        # right, so convert those origin coordinates to our top-left blit
        # convention below.
        travel = max(0.0, float(stage_x2 - stage_x1))
        if side == "left":
            x = stage_x1 + travel * movement - iw
        else:
            x = stage_x2 - travel * movement
        if self.geometry.upside_down:
            y = self.stage_y1
        else:
            y = self.stage_y2 - ih
        self._blit_skin_image(
            image,
            int(round(x)),
            int(round(y)),
            alpha_scale=alpha,
        )

    # Plural alias keeps the call site readable alongside draw_warning_arrows
    # and is useful to API users that treat all transient overlays uniformly.
    draw_combo_bursts = draw_combo_burst

    @staticmethod
    def _normalise_key_bindings(key_count, bindings=None):
        if bindings is None:
            values = _DEFAULT_MANIA_BINDINGS.get(int(key_count))
            if values is None:
                alphabet = list("ASDFGHJKL;ZXCVBNM")
                values = tuple(
                    alphabet[i] if i < len(alphabet) else f"K{i + 1}"
                    for i in range(max(0, int(key_count)))
                )
        else:
            values = tuple(bindings)
        result = []
        for index in range(max(0, int(key_count))):
            value = values[index] if index < len(values) else f"K{index + 1}"
            if isinstance(value, (tuple, list)):
                parts = [str(part).strip() for part in value if str(part).strip()]
                value = "/".join(parts)
            value = str(value).strip()
            if value.lower() == "space":
                value = "SPACE"
            result.append(value or f"K{index + 1}")
        return tuple(result)

    def _key_warning_sprite(self, text, colour):
        """Lazily rasterise one key label in the skin's warning colour."""
        cache = getattr(self, "_key_warning_sprite_cache", None)
        if cache is None:
            cache = self._key_warning_sprite_cache = {}
        scale = _legacy_internal_screen_scale()
        size = max(10, int(round(18.0 * scale)))
        colour_key = tuple(round(float(v), 6) for v in colour)
        key = (str(text), colour_key, size)
        cached = cache.get(key)
        if cached is not None:
            return cached
        rgb = tuple(
            max(0, min(255, int(round(float(v) * 255.0))))
            for v in colour[:3]
        )
        font = pygame.font.Font(None, size)
        surface = font.render(str(text), True, rgb)
        arr = self._surface_to_rgba_array(surface)
        alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
        alpha *= max(0.0, min(1.0, float(colour[3] if len(colour) > 3 else 1.0)))
        premul = arr[:, :, :3].astype(np.float32) * alpha
        cached = (premul, alpha, surface.get_width(), surface.get_height())
        cache[key] = cached
        return cached

    def _key_warning_center_y(self, col_idx):
        """Return Stable's per-column key-label centre in screen pixels."""
        receptor = self.assets.receptor[col_idx]
        if receptor is None:
            return None
        receptor_h = float(receptor[3])
        return (
            receptor_h / 2.0
            if self.geometry.upside_down
            else self.height - receptor_h / 2.0
        )

    def draw_key_warning(self, age_ms=-1.0, bindings=None):
        """Draw the pre-play key binding reminder using ColourKeyWarning."""
        if age_ms is None:
            return
        alpha = _legacy_key_warning_alpha(age_ms)
        if alpha <= 0.0:
            return
        colour = getattr(
            self.skin_config,
            "colour_key_warning",
            (0.0, 0.0, 0.0, 1.0),
        )
        labels = self._normalise_key_bindings(self.key_count, bindings)
        for col_idx, label in enumerate(labels):
            if col_idx >= len(self.lane_rects):
                break
            sprite = self._key_warning_sprite(label, colour)
            _, _, sw, sh = sprite
            x1, _, x2, _ = self.lane_rects[col_idx]
            x = (x1 + x2 - sw) / 2.0
            center_y = self._key_warning_center_y(col_idx)
            if center_y is None:
                continue
            y = center_y - sh / 2.0
            self._blit_premul(
                sprite[0], sprite[1], int(round(x)), int(round(y)), sw, sh,
            ) if alpha >= 0.999 else self._blit_skin_image(
                sprite,
                int(round(x)),
                int(round(y)),
                alpha_scale=alpha,
            )

    draw_key_warnings = draw_key_warning

    @staticmethod
    def _format_legacy_accuracy(acc):
        acc_percent = max(0.0, min(100.0, float(acc)))
        acc_percent = math.floor(acc_percent * 100.0 + 1e-9) / 100.0
        return f"{acc_percent:.2f}%"

    @staticmethod
    def _legacy_health_colour(health):
        hp = max(0.0, min(1.0, float(health)))
        if hp < 0.2:
            t = (0.2 - hp) / 0.2
            return t, 0.0, 0.0, 1.0
        if hp < 0.5:
            v = hp / 0.5
            return v, v, v, 1.0
        return 1.0, 1.0, 1.0, 1.0

    def _legacy_old_health_marker(self, health):
        if health < 0.2:
            resource = self.assets.scorebar_kidanger2
        elif health < 0.5:
            resource = self.assets.scorebar_kidanger
        else:
            resource = self.assets.scorebar_ki
        return resource.image if resource.present else None

    @staticmethod
    def _stable_mania_health_blit_position(
        anchor_x, anchor_y, source_width, source_height,
    ):
        scaled_w = max(1, int(round(
            source_width * LEGACY_MANIA_HEALTH_SPRITE_SCALE,
        )))
        scaled_h = max(1, int(round(
            source_height * LEGACY_MANIA_HEALTH_SPRITE_SCALE,
        )))
        rotation_dx = int(round((scaled_w - scaled_h) / 2.0))
        rotation_dy = int(round((scaled_h - scaled_w) / 2.0))
        return (
            int(round(anchor_x)) - rotation_dx,
            int(round(anchor_y)) - scaled_w - rotation_dy,
        )

    def _blit_stable_mania_health(self, image, anchor_x, anchor_y, clip_w=None):
        if image is None:
            return
        _, _, source_w, source_h = image
        clipped_w = source_w if clip_w is None else max(
            0, min(source_w, int(round(clip_w))),
        )
        if clipped_w <= 0:
            return
        x, y = self._stable_mania_health_blit_position(
            anchor_x, anchor_y, clipped_w, source_h,
        )
        self._blit_skin_image(
            image,
            x,
            y,
            scale=LEGACY_MANIA_HEALTH_SPRITE_SCALE,
            rotation=LEGACY_MANIA_HEALTH_ROTATION_DEG,
            clip_w=clipped_w,
        )

    def _draw_stable_mania_health(self, bg, colour, display, is_new_style):
        scale_480 = config.SCALE
        stage_right = float(self.stage_x2)
        self._blit_stable_mania_health(
            bg,
            stage_right + LEGACY_MANIA_HEALTH_BG_OFFSET_X_480 * scale_480,
            float(self.height),
        )
        if colour is None:
            return
        _, _, colour_w, _ = colour
        offset_x = (
            LEGACY_MANIA_HEALTH_NEW_OFFSET_X_480
            if is_new_style else LEGACY_MANIA_HEALTH_OLD_OFFSET_X_480
        )
        offset_y = (
            LEGACY_MANIA_HEALTH_NEW_OFFSET_Y_480
            if is_new_style else LEGACY_MANIA_HEALTH_OLD_OFFSET_Y_480
        )
        self._blit_stable_mania_health(
            colour,
            stage_right + offset_x * scale_480,
            offset_y * scale_480,
            clip_w=colour_w * display,
        )

    def draw_health_bar(self, health=1.0, health_display=None, marker_scale=1.0, flash_age_ms=-1.0, flash_epic=False, time_ms=0.0):
        if not config.SHOW_HEALTH_BAR:
            return
        health = max(0.0, min(1.0, float(health)))
        # LegacyHealthDisplay: 平滑值只决定填充宽度与 marker 位置；tint、
        # additive 与 ki 纹理选择始终用原始 health（lazer 用 Current.Value）。
        display = (
            health if health_display is None
            else max(0.0, min(1.0, float(health_display)))
        )
        marker_scale = max(0.0, float(marker_scale))
        position_scale = LEGACY_POSITION_SCALE_FACTOR * _legacy_internal_screen_scale()
        stable_mania = config.COMPATIBILITY_MODE == config.COMPAT_STABLE_REPLAY
        bg_resource = self.assets.scorebar_bg
        if not bg_resource.present:
            return
        colour_resource = self.assets.scorebar_colour
        marker_resource = self.assets.scorebar_marker
        is_new_style = marker_resource.present
        bg = bg_resource.image
        colour_for_time = getattr(
            self.assets, "scorebar_colour_for_time", None,
        )
        colour = (
            colour_for_time(time_ms)
            if colour_resource.present and colour_for_time is not None
            else colour_resource.image if colour_resource.present else None
        )
        marker = (
            marker_resource.image
            if is_new_style else self._legacy_old_health_marker(health)
        )
        if stable_mania:
            self._draw_stable_mania_health(
                bg, colour, display, is_new_style,
            )
            return

        fill_offset_x = 7.5 if is_new_style else 3.0
        fill_offset_y = 7.8 if is_new_style else 10.0
        fill_x = int(round(fill_offset_x * position_scale))
        fill_y = int(round(fill_offset_y * position_scale))
        fill_tint = self._legacy_health_colour(health) if is_new_style else None
        self._blit_skin_image(bg, 0, 0)
        fill_w = 0
        fill_h = 0
        if colour is not None:
            _, _, cw, ch = colour
            fill_w = int(round(cw * display))
            self._blit_skin_image_clipped(
                colour,
                fill_x,
                fill_y,
                clip_w=fill_w,
                clip_h=ch,
                tint=fill_tint,
            )
            fill_h = ch
        if marker is None:
            return
        _, _, mw, mh = marker
        marker_center_x = fill_x + fill_w
        marker_center_y = fill_y + (fill_h / 2.0 if is_new_style else 0.0)
        mx = int(round(marker_center_x - (mw * marker_scale) / 2))
        my = int(round(marker_center_y - (mh * marker_scale) / 2))
        self._blit_skin_image(
            marker,
            mx,
            my,
            scale=marker_scale if abs(marker_scale - 1.0) > 1e-9 else 1.0,
            tint=fill_tint if is_new_style else None,
            additive=is_new_style and health >= 0.5,
        )
        if flash_age_ms < 0.0:
            return
        flash_scale, flash_alpha = _legacy_health_flash_state(
            flash_age_ms, flash_epic,
        )
        if flash_alpha <= 0.0:
            return
        fx = int(round(marker_center_x - (mw * flash_scale) / 2))
        fy = int(round(marker_center_y - (mh * flash_scale) / 2))
        self._blit_skin_image(
            marker,
            fx,
            fy,
            scale=flash_scale,
            alpha_scale=flash_alpha,
            additive=bool(flash_epic),
        )

    def draw_combo_display(self, combo=87, pop_age_ms=-1.0, ghost_value=0, ghost_age_ms=-1.0, holding=False, show_age_ms=-1.0):
        if not self.assets.combo_font:
            return
        overlap = self.skin_config.combo_overlap * _legacy_internal_screen_scale()
        if ghost_value > 0 and ghost_age_ms >= 0.0:
            ghost_scale, ghost_alpha = _legacy_combo_ghost_state(ghost_age_ms)
            if ghost_alpha > 0.0:
                ghost_text = str(ghost_value)
                gw, gh = self._measure_skin_text(
                    self.assets.combo_font, ghost_text, overlap,
                    scale=ghost_scale,
                )
                self._blit_skin_text(
                    self.assets.combo_font,
                    ghost_text,
                    self.stage_cx - gw / 2,
                    self.y_combo - gh / 2,
                    overlap,
                    scale=ghost_scale,
                    alpha_scale=ghost_alpha,
                    tint=self.skin_config.colour_break,
                    additive=True,
                )
        if combo <= 0:
            return
        text = str(combo)
        # 1/128 quantisation avoids per-frame transform-cache churn.
        scale_y = round(_legacy_combo_pop_scale_y(pop_age_ms) * 128.0) / 128.0
        alpha = _legacy_combo_show_alpha(show_age_ms)
        tint = self.skin_config.colour_hold if holding else None
        tw, th = self._measure_skin_text(self.assets.combo_font, text, overlap)
        self._blit_skin_text(
            self.assets.combo_font,
            text,
            self.stage_cx - tw / 2,
            self.y_combo - (th * scale_y) / 2,
            overlap,
            alpha_scale=alpha,
            tint=tint,
            scale_y=scale_y if abs(scale_y - 1.0) > 1e-9 else None,
        )

    def draw_score_display(self, score="123,456"):
        score_str = f"{score:08d}" if isinstance(score, int) else str(score)
        if not self.assets.score_font:
            return
        overlap = self.skin_config.score_overlap * _legacy_internal_screen_scale()
        scale = LEGACY_SCORE_COUNTER_SCALE
        tw, _ = self._measure_skin_text(
            self.assets.score_font,
            score_str,
            overlap,
            scale=scale,
            fixed_width=True,
        )
        margin_x = LEGACY_SCORE_HUD_OFFSET_X_480 * self.height / 480.0
        self._blit_skin_text(
            self.assets.score_font,
            score_str,
            self.width - tw - margin_x,
            0,
            overlap,
            scale=scale,
            fixed_width=True,
        )

    def draw_acc_display(self, acc=100.0):
        acc_str = self._format_legacy_accuracy(acc)
        if not self.assets.score_font:
            return
        overlap = self.skin_config.score_overlap * _legacy_internal_screen_scale()
        scale = LEGACY_ACCURACY_COUNTER_SCALE
        tw, _ = self._measure_skin_text(
            self.assets.score_font,
            acc_str,
            overlap,
            scale=scale,
            fixed_width=True,
        )
        if tw > 0:
            _, score_h = self._measure_skin_text(
                self.assets.score_font,
                "00000000",
                overlap,
                scale=LEGACY_SCORE_COUNTER_SCALE,
                fixed_width=True,
            )
            position_scale = self.height / 480.0
            margin_x = LEGACY_SCORE_HUD_OFFSET_X_480 * position_scale
            margin_y = (
                LEGACY_SCORE_NEW_SKIN_GAP_Y_480 * position_scale
                if self.skin_config.version > 1.0 else 0.0
            )
            self._blit_skin_text(
                self.assets.score_font,
                acc_str,
                self.width - tw - margin_x,
                score_h + margin_y,
                overlap,
                scale=scale,
                fixed_width=True,
            )

    def _init_song_progress_layout(self):
        position_scale = self.height / 480.0
        score_height = (
            LEGACY_SONG_PROGRESS_TOP_RIGHT_SCORE_HEIGHT_480 * position_scale
        )
        if self.assets.score_font:
            overlap = (
                self.skin_config.score_overlap * _legacy_internal_screen_scale()
            )
            accuracy_width, accuracy_height = self._measure_skin_text(
                self.assets.score_font,
                LEGACY_SONG_PROGRESS_INITIAL_ACCURACY,
                overlap,
                scale=LEGACY_ACCURACY_COUNTER_SCALE,
                fixed_width=True,
            )
            _, measured_score_height = self._measure_skin_text(
                self.assets.score_font,
                "00000000",
                overlap,
                scale=LEGACY_SCORE_COUNTER_SCALE,
                fixed_width=True,
            )
            if measured_score_height > 0.0:
                score_height = measured_score_height
            accuracy_y = score_height
            if self.skin_config.version > 1.0:
                accuracy_y += LEGACY_SCORE_NEW_SKIN_GAP_Y_480 * position_scale
        else:
            accuracy_width = 0.0
            accuracy_height = 0.0
            accuracy_y = score_height

        accuracy_right = (
            self.width - LEGACY_SCORE_HUD_OFFSET_X_480 * position_scale
        )
        self._song_progress_score_height = score_height
        self._song_progress_pie_center = (
            accuracy_right
            - accuracy_width
            - LEGACY_SONG_PROGRESS_PIE_OFFSET_X_480 * position_scale,
            accuracy_y + accuracy_height / 2.0,
        )

    def _song_progress_ring(self):
        """Rasterise Stable's client-owned gameplay progress ring."""
        ring = getattr(self, "_progress_ring", None)
        if ring is not None:
            return ring
        s = _legacy_internal_screen_scale()
        box = max(3, int(round(LEGACY_SONG_PROGRESS_SIZE_768 * s)))
        border = max(1.0, LEGACY_SONG_PROGRESS_BORDER_768 * s)
        dot_radius = max(0.5, LEGACY_SONG_PROGRESS_DOT_768 * s / 2.0)
        ss = LEGACY_SONG_PROGRESS_SUPERSAMPLE
        n = box * ss
        u = ((np.arange(n, dtype=np.float32) + 0.5) / ss) - box / 2.0
        rr = np.sqrt(u[None, :] ** 2 + u[:, None] ** 2)
        outer = box / 2.0
        inner = outer - border
        mask = ((rr <= outer) & (rr >= inner)) | (rr <= dot_radius)
        alpha = mask.reshape(box, ss, box, ss).mean(axis=(1, 3)).astype(np.float32)
        a = alpha[..., None]
        rgb_pm = np.repeat(a, 3, axis=2) * 255.0
        ring = (rgb_pm, a, box, box)
        self._progress_ring = ring
        return ring

    @staticmethod
    def _song_progress_arc_points(progress, radius, centre):
        progress = max(-2.0, min(1.0, float(progress)))
        if progress == 0.0 or radius <= 0.0:
            return ()
        # Values below -1 occur only before the normal rendered lead-in. They
        # wind the same full circle more than once in Stable and are visually
        # equivalent to a single full fan.
        geometry_progress = max(-1.0, progress)
        start = -math.pi / 2.0
        end = start + geometry_progress * math.tau
        arc_start, arc_end = sorted((start, end))
        cx, cy = centre
        points = [(float(cx), float(cy))]
        angle = arc_start
        while angle < arc_end:
            points.append((
                cx + math.cos(angle) * radius,
                cy + math.sin(angle) * radius,
            ))
            angle += LEGACY_SONG_PROGRESS_PIE_ARC_STEP_RAD
        points.append((
            cx + math.cos(arc_end) * radius,
            cy + math.sin(arc_end) * radius,
        ))
        return tuple(points)

    def _song_progress_sprite(self, progress, is_intro=None):
        progress = max(-2.0, min(1.0, float(progress)))
        if is_intro is True and progress > 0.0:
            progress = -progress
        radius = LEGACY_SONG_PROGRESS_PIE_RADIUS_480 * self.height / 480.0
        d = max(3, int(math.ceil(radius * 2.0)) + 1)
        ss = LEGACY_SONG_PROGRESS_SUPERSAMPLE
        n = d * ss
        surface = pygame.Surface((n, n), flags=pygame.SRCALPHA, depth=32)
        points = self._song_progress_arc_points(
            progress,
            radius * ss,
            (n / 2.0, n / 2.0),
        )
        if points:
            pygame.draw.polygon(
                surface,
                (255, 255, 255, 255),
                [(int(round(x)), int(round(y))) for x, y in points],
            )
        raw_alpha = self._surface_to_rgba_array(surface)[:, :, 3].astype(
            np.float32
        ) / 255.0
        alpha = raw_alpha.reshape(d, ss, d, ss).mean(axis=(1, 3))
        colour = (
            LEGACY_SONG_PROGRESS_INTRO_COLOUR
            if progress < 0.0 else LEGACY_SONG_PROGRESS_COLOUR
        )
        a = (alpha * colour[3])[..., None]
        rgb = np.empty((d, d, 3), dtype=np.float32)
        rgb[..., 0] = colour[0] * 255.0
        rgb[..., 1] = colour[1] * 255.0
        rgb[..., 2] = colour[2] * 255.0
        return rgb * a, a, d, d

    def _draw_linear_song_progress(self, progress, mode):
        position_scale = self.height / 480.0
        height = LEGACY_SONG_PROGRESS_LINEAR_HEIGHT_480 * position_scale
        if mode == config.SONG_PROGRESS_BOTTOM:
            x = 0.0
            y = LEGACY_SONG_PROGRESS_BOTTOM_Y_480 * position_scale
            width = float(self.width)
        elif mode == config.SONG_PROGRESS_BOTTOM_RIGHT:
            x = (
                self.width
                - LEGACY_SONG_PROGRESS_BOTTOM_RIGHT_X_480 * position_scale
            )
            y = LEGACY_SONG_PROGRESS_BOTTOM_RIGHT_Y_480 * position_scale
            width = (
                LEGACY_SONG_PROGRESS_BOTTOM_RIGHT_WIDTH_480 * position_scale
            )
        else:
            x = self.width - LEGACY_SONG_PROGRESS_TOP_RIGHT_X_480 * position_scale
            y = self._song_progress_score_height - 2.0 * position_scale
            width = LEGACY_SONG_PROGRESS_TOP_RIGHT_WIDTH_480 * position_scale
        self._blend_rect(
            x,
            y,
            x + width,
            y + height,
            LEGACY_SONG_PROGRESS_LINEAR_BACKGROUND,
        )
        fill = max(0.0, min(1.0, float(progress)))
        if fill > 0.0:
            self._blend_rect(
                x,
                y,
                x + width * fill,
                y + height,
                LEGACY_SONG_PROGRESS_LINEAR_FILL,
            )

    def draw_song_progress(self, progress, is_intro=None, acc_value=None):
        mode = config.SONG_PROGRESS_MODE
        if mode == config.SONG_PROGRESS_OFF:
            return
        if not hasattr(self, "_song_progress_pie_center"):
            self._init_song_progress_layout()
        progress = max(-2.0, min(1.0, float(progress)))
        if is_intro is True and progress > 0.0:
            progress = -progress
        if mode != config.SONG_PROGRESS_PIE:
            self._draw_linear_song_progress(progress, mode)
            return
        ring = self._song_progress_ring()
        box = ring[2]
        cx, cy = self._song_progress_pie_center
        left = int(round(cx - box / 2.0))
        top = int(round(cy - box / 2.0))
        pie = self._song_progress_sprite(progress)
        pie_x = left + int(round((box - pie[2]) / 2.0))
        pie_y = top + int(round((box - pie[3]) / 2.0))
        self._blit_premul(pie[0], pie[1], pie_x, pie_y, pie[2], pie[3])
        self._blit_premul(ring[0], ring[1], left, top, box, box)

    def draw_hit_error_meter(self, errors=(), average=0.0, windows=None):
        """底部中央误差条（★几何为近似;stable 在 mania 下沿用 std 三色）。"""
        if not config.SHOW_HIT_ERROR or not windows:
            return
        great, ok, meh = windows
        if meh <= 0:
            return
        s = _legacy_internal_screen_scale()
        half_len = LEGACY_HIT_ERROR_BAR_HEIGHT_768 / 2.0 * s
        px_per_ms = half_len / float(meh)
        cx = self.width / 2.0
        bar_th = max(1.0, LEGACY_HIT_ERROR_BAR_WIDTH_768 * s)
        cy = (
            self.height
            - LEGACY_HIT_ERROR_BOTTOM_MARGIN_768 * s
            - bar_th / 2.0
        )
        y1 = cy - bar_th / 2.0
        y2 = cy + bar_th / 2.0
        for window, colour in (
            (meh, LEGACY_HIT_ERROR_COLOUR_MEH),
            (ok, LEGACY_HIT_ERROR_COLOUR_OK),
            (great, LEGACY_HIT_ERROR_COLOUR_GREAT),
        ):
            half = window * px_per_ms
            self._blend_rect(cx - half, y1, cx + half, y2, (*colour, 1.0))
        tick_h = LEGACY_HIT_ERROR_TICK_WIDTH_768 * s
        tick_w = max(1.0, 2.0 * s)
        self._blend_rect(
            cx - tick_w / 2.0,
            cy - tick_h / 2.0,
            cx + tick_w / 2.0,
            cy + tick_h / 2.0,
            (1.0, 1.0, 1.0, 0.9),
        )
        for error, age in errors:
            alpha = _legacy_hit_error_tick_alpha(age)
            if alpha <= 0.0:
                continue
            clamped = max(-float(meh), min(float(meh), float(error)))
            x = cx + clamped * px_per_ms
            magnitude = abs(clamped)
            if magnitude <= great:
                colour = LEGACY_HIT_ERROR_COLOUR_GREAT
            elif magnitude <= ok:
                colour = LEGACY_HIT_ERROR_COLOUR_OK
            else:
                colour = LEGACY_HIT_ERROR_COLOUR_MEH
            self._blend_rect(
                x - tick_w / 2.0,
                cy - tick_h / 2.0,
                x + tick_w / 2.0,
                cy + tick_h / 2.0,
                (*colour, alpha),
            )
        avg_clamped = max(-float(meh), min(float(meh), float(average)))
        avg_x = cx + avg_clamped * px_per_ms
        arrow_w = LEGACY_HIT_ERROR_CHEVRON_768 * s
        arrow_h = max(1.0, 4.0 * s)
        arrow_gap = max(1.0, 2.0 * s)
        self._blend_rect(
            avg_x - arrow_w / 2.0,
            cy - tick_h / 2.0 - arrow_gap - arrow_h,
            avg_x + arrow_w / 2.0,
            cy - tick_h / 2.0 - arrow_gap,
            (1.0, 1.0, 1.0, 0.9),
        )
