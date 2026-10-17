"""皮肤图片资源的加载与预缩放（RGBA numpy 数组，在每个渲染进程内构造）。"""

import math
import os
from dataclasses import dataclass

import numpy as np
import pygame

from .. import config
from ..legacy_constants import (
    LEGACY_HIT_TARGET_IMAGE_Y_SCALE,
    LEGACY_JUDGEMENT_FPS,
    LEGACY_MANIA_BODY_FRAME_MS,
    LEGACY_MANIA_NOTE_FPS,
    LEGACY_POSITION_SCALE_FACTOR,
    LEGACY_SKIN_SOURCE_BEATMAP,
    LEGACY_SKIN_SOURCE_SELECTED,
)
from ..legacy_math import (
    _legacy_deterministic_index,
    _legacy_disallow_zero_alpha,
    _legacy_hit_explosion_fps,
    _legacy_internal_screen_scale,
)
from .ini import (
    iter_skin_image_candidates,
    resolve_skin_animation_frame,
)


# Sources with an edge this short carry no detail to interpolate, so the plain
# scaler reproduces the smooth one byte for byte (verified over the local test
# skins).  Skins use 1x1 fully transparent PNGs as "nothing to draw"
# placeholders -- test4 names one "full" for StageHint/StageLeft/StageRight/
# StageLight/KeyImage -- and the loader then upscales them by ~1000x.  pygame
# 2.6.1's smoothscale faults on that combination, so degenerate sources take
# the plain scaler and avoid the faulty path without changing any pixel.
DEGENERATE_SCALE_EDGE = 2


@dataclass(frozen=True)
class SkinImageResource:
    present: bool = False
    image: object = None
    width: int = 0
    height: int = 0
    source: int = 0


@dataclass(frozen=True)
class _SkinProvider:
    source: int
    root: str


@dataclass(frozen=True)
class _ResolvedSurface:
    source: int
    surface: object
    is_2x: bool
    path: str = ""


@dataclass(frozen=True)
class _LoadedImage:
    image: object
    source: int


@dataclass(frozen=True)
class SkinFontGlyph:
    image: object
    logical_width: float
    logical_height: float

    def __iter__(self):
        return iter(self.image)

    def __len__(self):
        return len(self.image)

    def __getitem__(self, item):
        return self.image[item]


class SkinAssets:
    """加载并预缩放皮肤图片为 RGBA numpy 数组（在每个渲染进程内构造）"""

    @classmethod
    def warning_arrow_height_480(cls, config_, stage_scale=1.0):
        """Return Stable's raw 480p height for the selected warning sprite."""
        if config_ is None:
            return 0.0
        resolver = object.__new__(cls)
        resolver.config = config_
        resolver._providers = tuple(
            _SkinProvider(source, root)
            for source, root in (
                (
                    LEGACY_SKIN_SOURCE_BEATMAP,
                    getattr(config_, "beatmap_root", None),
                ),
                (
                    LEGACY_SKIN_SOURCE_SELECTED,
                    getattr(config_, "root", None),
                ),
            )
            if root
        )
        for ref in cls._reference_chain(
            getattr(config_, "warning_arrow", None),
            "mania-warningarrow",
        ):
            resolved = resolver._resolve_surface(ref)
            if resolved is None or resolved.surface is None:
                continue
            _, source_height = resolved.surface.get_size()
            _, display_height = cls._display_size(
                1,
                source_height,
                resolved.is_2x,
            )
            return max(
                0.0,
                display_height
                * float(stage_scale)
                / LEGACY_POSITION_SCALE_FACTOR,
            )
        return 0.0

    def __init__(
        self,
        config_,
        lane_rects,
        upside_down=None,
        stage_ranges=None,
        stage_scale=1.0,
    ):
        if config_ is None:
            raise ValueError("SkinAssets requires a custom SkinConfig")
        self.config = config_
        self._providers = tuple(
            _SkinProvider(source, root)
            for source, root in (
                (LEGACY_SKIN_SOURCE_BEATMAP, getattr(config_, "beatmap_root", None)),
                (LEGACY_SKIN_SOURCE_SELECTED, getattr(config_, "root", None)),
            )
            if root
        )
        self.upside_down = bool(
            config_.upside_down if upside_down is None else upside_down
        )
        self.note = [None] * config_.key_count
        self.note_frames = [[] for _ in range(config_.key_count)]
        self.head = [None] * config_.key_count
        self.head_frames = [[] for _ in range(config_.key_count)]
        self.body = [None] * config_.key_count
        self.body_frames = [[] for _ in range(config_.key_count)]
        self._body_image_cache = {}
        self.tail = [None] * config_.key_count
        self.tail_frames = [[] for _ in range(config_.key_count)]
        self.receptor = [None] * config_.key_count
        self.receptor_down = [None] * config_.key_count
        self.light_n = [None] * config_.key_count
        self.light_l = [None] * config_.key_count
        self.light_n_frames = [[] for _ in range(config_.key_count)]
        self.light_l_frames = [[] for _ in range(config_.key_count)]
        self.light_n_fps = [0.0] * config_.key_count
        self.light_l_fps = [0.0] * config_.key_count
        self.stage_light = [None] * config_.key_count
        self.stage_light_frames = [[] for _ in range(config_.key_count)]
        self.stage_light_fps = [0.0] * config_.key_count
        self.stage_hint = None
        self.stage_hints = []
        self.stage_left = None
        self.stage_right = None
        self.stage_bottom = None
        self.stage_bottom_frames = []
        self.stage_bottom_fps = 0.0
        self.warning_arrow = None
        self.combo_burst = []
        self.combo_burst_right = []
        self.scorebar_bg = SkinImageResource()
        self.scorebar_colour = SkinImageResource()
        self.scorebar_colour_frames = []
        self.scorebar_colour_fps = 0.0
        self.scorebar_colour_source = 0
        self.scorebar_marker = SkinImageResource()
        self.scorebar_ki = SkinImageResource()
        self.scorebar_kidanger = SkinImageResource()
        self.scorebar_kidanger2 = SkinImageResource()
        self.hit_result = None
        self.hit_result_frames = []
        self.hit_result_fps = 0.0
        self.hit_result_by_value = {}
        self.hit_result_frames_by_value = {}
        self.hit_result_fps_by_value = {}
        self.score_font = {}
        self.combo_font = {}
        stage_scale = float(stage_scale)
        note_height_w = config_.width_for_note_height_scale * config.SCALE
        if config_.width_for_note_height_scale_uses_column_width:
            note_height_w *= stage_scale
        for i, (x1, _, x2, _) in enumerate(lane_rects):
            lane_w = x2 - x1
            fallback_class = config_.fallback_column_index[i]
            note_fallback = f"mania-note{fallback_class}"
            head_fallback = f"mania-note{fallback_class}H"
            body_fallback = f"mania-note{fallback_class}L"
            tail_fallback = f"mania-note{fallback_class}T"
            key_fallback = f"mania-key{fallback_class}"
            key_down_fallback = f"mania-key{fallback_class}D"
            flip_note = self.upside_down and config_.note_flip[i]
            flip_head = self.upside_down and config_.note_head_flip[i]
            flip_body = self.upside_down and config_.note_body_flip[i]
            flip_tail = True
            if self.upside_down and config_.supports_mania_25 and config_.note_tail_flip[i]:
                flip_tail = not flip_tail
            flip_key = self.upside_down and config_.key_flip[i]
            flip_key_down = self.upside_down and config_.key_down_flip[i]
            self.note_frames[i], self.note[i] = self._load_scaled_asset(
                config_.note[i], note_fallback, lane_w,
                height_scale_w=note_height_w,
                flip_y=flip_note,
                allow_transparent=True,
            )
            self.head_frames[i], self.head[i] = self._load_scaled_asset(
                config_.note_head[i], head_fallback, lane_w,
                height_scale_w=note_height_w,
                flip_y=flip_head,
                allow_transparent=True,
            )
            for tail_ref, fallback_ref in (
                (config_.note_tail[i], tail_fallback),
                (config_.note_head[i], head_fallback),
                (config_.note[i], note_fallback),
            ):
                self.tail_frames[i], self.tail[i] = self._load_scaled_asset(
                    tail_ref, fallback_ref, lane_w,
                    height_scale_w=note_height_w,
                    flip_y=flip_tail,
                    allow_transparent=True,
                )
                if self.tail[i] is not None:
                    break
            self.receptor[i] = self._load_receptor_with_fallback(
                config_.key_image[i], key_fallback, lane_w, flip_y=flip_key,
            )
            self.receptor_down[i] = self._load_receptor_with_fallback(
                config_.key_down_image[i], key_down_fallback, lane_w,
                flip_y=flip_key_down,
            )
            self.body_frames[i], self.body[i] = self._load_body_asset(
                config_.note_body[i], body_fallback, lane_w, note_height_w,
                flip_y=flip_body,
            )
            # LegacyHitExplosion / LegacyBodyPiece：原生纹理尺寸 ×(H/768)×
            # ExplosionScale；不再强制拉伸到列宽。
            light_base_scale = _legacy_internal_screen_scale() * stage_scale
            n_scale = light_base_scale * self._legacy_explosion_scale(
                config_, config_.lighting_n_width[i], config_.column_width[i],
            )
            l_scale = light_base_scale * self._legacy_explosion_scale(
                config_, config_.lighting_l_width[i], config_.column_width[i],
            )
            self.light_n_frames[i], self.light_n[i] = self._load_free_asset(
                config_.lighting_n, "lightingN", scale=n_scale,
            )
            self.light_l_frames[i], self.light_l[i] = self._load_free_asset(
                config_.lighting_l, "lightingL", scale=l_scale,
            )
            if self.light_n_frames[i]:
                self.light_n_fps[i] = _legacy_hit_explosion_fps(len(self.light_n_frames[i]))
            if self.light_l_frames[i]:
                self.light_l_fps[i] = _legacy_hit_explosion_fps(len(self.light_l_frames[i]))
            self.stage_light_frames[i], self.stage_light[i] = self._load_scaled_asset(
                config_.stage_light, "mania-stage-light",
                lane_w,
                tint=_legacy_disallow_zero_alpha(config_.colour_light[i]),
                flip_y=self.upside_down,
                allow_transparent=True,
            )
            if self.stage_light_frames[i]:
                self.stage_light_fps[i] = self._effective_animation_fps(
                    config_.light_frame_per_second,
                    len(self.stage_light_frames[i]),
                )
        if stage_ranges is None:
            stage_ranges = ((lane_rects[0][0], lane_rects[-1][2]),)
        stage_widths = [stage_x2 - stage_x1 for stage_x1, stage_x2 in stage_ranges]
        for stage_w in stage_widths:
            image = self._load_scaled_static(
                config_.stage_hint, "mania-stage-hint",
                stage_w,
                height_scale_w=stage_w * LEGACY_HIT_TARGET_IMAGE_Y_SCALE,
                flip_y=self.upside_down,
                allow_transparent=True,
            )
            self.stage_hints.append(image)
        self.stage_hint = self.stage_hints[0] if self.stage_hints else None
        stage_w = lane_rects[-1][2] - lane_rects[0][0]
        self.stage_left = self._load_free_with_fallback(
            config_.stage_left, "mania-stage-left",
            scale=_legacy_internal_screen_scale(), target_h=config.HEIGHT,
            allow_transparent=True,
        )
        self.stage_right = self._load_free_with_fallback(
            config_.stage_right, "mania-stage-right",
            scale=_legacy_internal_screen_scale(), target_h=config.HEIGHT,
            allow_transparent=True,
        )
        stage_bottom_scale = LEGACY_POSITION_SCALE_FACTOR * _legacy_internal_screen_scale()
        self.stage_bottom_frames, self.stage_bottom = self._load_free_asset(
            config_.stage_bottom, "mania-stage-bottom",
            scale=stage_bottom_scale,
        )
        if self.stage_bottom_frames:
            self.stage_bottom_fps = LEGACY_MANIA_NOTE_FPS
        self.warning_arrow = self._load_free_with_fallback(
            config_.warning_arrow, "mania-warningarrow",
            scale=_legacy_internal_screen_scale() * stage_scale,
            allow_transparent=True,
        )
        self.combo_burst = self._load_combo_burst_resources()
        self.combo_burst_right = [
            self._flip_image_x(image) for image in self.combo_burst
        ]
        self._load_scorebar_resources(config_)
        judgement_max_w = stage_w * config.MANIA_JUDGEMENT_MAX_STAGE_WIDTH_RATIO
        for value, ref in config_.hit_images.items():
            fallback_ref = {
                0: "mania-hit0",
                50: "mania-hit50",
                100: "mania-hit100",
                200: "mania-hit200",
                300: "mania-hit300",
                320: "mania-hit300g",
            }[value]
            frames = self._load_hit_result_frames(
                ref, judgement_max_w, fallback_ref=fallback_ref,
            )
            if frames:
                self.hit_result_frames_by_value[value] = frames
                self.hit_result_by_value[value] = frames[0]
                self.hit_result_fps_by_value[value] = LEGACY_JUDGEMENT_FPS
        self.hit_result_frames = (
            self.hit_result_frames_by_value.get(320)
            or self.hit_result_frames_by_value.get(300)
            or []
        )
        if self.hit_result_frames:
            self.hit_result = self.hit_result_frames[0]
            self.hit_result_fps = (
                self.hit_result_fps_by_value.get(320)
                or self.hit_result_fps_by_value.get(300)
                or 0.0
            )
        self.score_font = self._load_font_glyphs(config_.score_prefix, "0123456789,.%")
        self.combo_font = self._load_font_glyphs(config_.combo_prefix, "0123456789x")

    def _load_scorebar_resources(self, config_):
        scorebar_scale = _legacy_internal_screen_scale()
        self.scorebar_colour_frames = []
        self.scorebar_colour_fps = 0.0
        self.scorebar_colour_source = 0
        self.scorebar_colour = SkinImageResource()
        self.scorebar_marker = SkinImageResource()
        self.scorebar_ki = SkinImageResource()
        self.scorebar_kidanger = SkinImageResource()
        self.scorebar_kidanger2 = SkinImageResource()
        self.scorebar_bg = self._load_image_resource(
            config_.scorebar_bg, scale=scorebar_scale,
            fallback_ref="scorebar-bg",
        )
        if not self.scorebar_bg.present:
            return

        source = self.scorebar_bg.source
        (
            self.scorebar_colour_frames,
            scorebar_colour,
            self.scorebar_colour_source,
        ) = self._load_free_asset_resource(
            config_.scorebar_colour, "scorebar-colour", scale=scorebar_scale,
            preserve_transparent=True,
            source=source,
        )
        if self.scorebar_colour_frames:
            first_colour = self.scorebar_colour_frames[0]
            self.scorebar_colour = SkinImageResource(
                present=True,
                image=first_colour,
                width=first_colour[2],
                height=first_colour[3],
                source=self.scorebar_colour_source,
            )
            self.scorebar_colour_fps = self._effective_animation_fps(
                config_.animation_framerate,
                len(self.scorebar_colour_frames),
            )
        else:
            self.scorebar_colour = self._image_resource(
                scorebar_colour,
                self.scorebar_colour_source,
            )
        self.scorebar_marker = self._load_image_resource(
            config_.scorebar_marker, scale=scorebar_scale,
            fallback_ref="scorebar-marker",
            source=source,
        )
        self.scorebar_ki = self._load_image_resource(
            config_.scorebar_ki, scale=scorebar_scale,
            fallback_ref="scorebar-ki",
            source=source,
        )
        self.scorebar_kidanger = self._load_image_resource(
            config_.scorebar_kidanger, scale=scorebar_scale,
            fallback_ref="scorebar-kidanger",
            source=source,
        )
        self.scorebar_kidanger2 = self._load_image_resource(
            config_.scorebar_kidanger2, scale=scorebar_scale,
            fallback_ref="scorebar-kidanger2",
            source=source,
        )

    def _load_combo_burst_resources(self):
        """Load Stable's numbered mania combobursts, then the singleton form.

        ``comboburst-mania-0.png`` etc. are variants, not animation frames.
        A gap terminates the numbered sequence in Stable; when no numbered
        variant exists, the unnumbered image is used as a singleton.
        """
        scale = _legacy_internal_screen_scale()
        for ref in ("comboburst-mania", "comboburst"):
            frames, image = self._load_free_asset(
                ref,
                None,
                scale=scale,
                preserve_transparent=True,
            )
            if frames:
                return frames
            if image is not None:
                return [image]
        return []

    @staticmethod
    def _reference_chain(ref, fallback_ref=None):
        seen = set()
        for value in (ref, fallback_ref):
            if not value:
                continue
            key = str(value).strip().replace("\\", "/").casefold()
            if key in seen:
                continue
            seen.add(key)
            yield value

    def _provider_for_source(self, source):
        for provider in self._resource_providers():
            if provider.source == source:
                return provider
        return None

    def _resource_providers(self):
        providers = getattr(self, "_providers", None)
        if providers is None:
            config_ = getattr(self, "config", None)
            providers = tuple(
                _SkinProvider(source, root)
                for source, root in (
                    (
                        LEGACY_SKIN_SOURCE_BEATMAP,
                        getattr(config_, "beatmap_root", None),
                    ),
                    (
                        LEGACY_SKIN_SOURCE_SELECTED,
                        getattr(config_, "root", None),
                    ),
                )
                if root
            )
            self._providers = providers
        return providers

    @staticmethod
    def _decode_surface(path):
        try:
            return pygame.image.load(path)
        except Exception:
            return None

    def _resolve_surface(self, ref, source=None):
        providers = self._resource_providers()
        if source is not None:
            provider = self._provider_for_source(source)
            providers = (provider,) if provider is not None else ()
        for provider in providers:
            candidate = next(
                iter_skin_image_candidates(provider.root, ref),
                None,
            )
            if candidate is None:
                continue
            path, is_2x = candidate
            return _ResolvedSurface(
                provider.source,
                self._decode_surface(path),
                is_2x,
                path,
            )
        return None

    def _resolve_animation_surfaces(self, ref, source=None):
        first_ref = None
        providers = self._resource_providers()
        if source is not None:
            provider = self._provider_for_source(source)
            providers = (provider,) if provider is not None else ()
        for provider in providers:
            path, is_2x = resolve_skin_animation_frame(provider.root, ref, 0)
            if path is None:
                continue
            first_ref = _ResolvedSurface(
                provider.source,
                self._decode_surface(path),
                is_2x,
                path,
            )
            break
        if first_ref is None or first_ref.surface is None:
            return [], 0

        frames = [first_ref]
        provider = self._provider_for_source(first_ref.source)
        index = 1
        while provider is not None:
            path, is_2x = resolve_skin_animation_frame(
                provider.root,
                ref,
                index,
            )
            if path is None:
                break
            surface = self._decode_surface(path)
            if surface is None:
                break
            frames.append(_ResolvedSurface(
                first_ref.source,
                surface,
                is_2x,
                path,
            ))
            index += 1
        return frames, first_ref.source

    def _resolve_asset_surfaces(self, ref, source=None):
        frames, animation_source = self._resolve_animation_surfaces(
            ref, source=source,
        )
        static = self._resolve_surface(ref, source=source)
        static_valid = static is not None and static.surface is not None
        if frames and (
            not static_valid or animation_source >= static.source
        ):
            return frames, None, animation_source
        if static_valid:
            return [], static, static.source
        return [], None, 0

    def _load_scaled_asset(self, ref, fallback_ref, lane_w, **kwargs):
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            surfaces, static, _ = self._resolve_asset_surfaces(candidate_ref)
            if surfaces:
                frames = []
                for surface in surfaces:
                    image = self._scale_resolved_surface(
                        surface,
                        lane_w,
                        **kwargs,
                    )
                    if image is None:
                        break
                    frames.append(image)
                if frames:
                    return frames, frames[0]
            elif static is not None:
                image = self._scale_resolved_surface(static, lane_w, **kwargs)
                if image is not None:
                    return [], image
        return [], None

    def _load_scaled_static(self, ref, fallback_ref, lane_w, **kwargs):
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            resolved = self._resolve_surface(candidate_ref)
            if resolved is None or resolved.surface is None:
                continue
            image = self._scale_resolved_surface(resolved, lane_w, **kwargs)
            if image is not None:
                return image
        return None

    def _load_free_asset(self, ref, fallback_ref, **kwargs):
        frames, image, _ = self._load_free_asset_resource(
            ref,
            fallback_ref,
            **kwargs,
        )
        return frames, image

    def _load_free_asset_resource(
        self, ref, fallback_ref, source=None, **kwargs,
    ):
        static_kwargs = dict(kwargs)
        static_kwargs.pop("preserve_transparent", None)
        static_kwargs["allow_transparent"] = True
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            surfaces, static, resolved_source = self._resolve_asset_surfaces(
                candidate_ref, source=source,
            )
            if surfaces:
                frames = []
                for surface in surfaces:
                    image = self._scale_free_resolved_surface(
                        surface,
                        kwargs.get("scale"),
                        kwargs.get("max_w"),
                        allow_transparent=True,
                    )
                    if image is None:
                        break
                    frames.append(image)
                if frames:
                    return frames, frames[0], resolved_source
            elif static is not None:
                image = self._scale_free_resolved_surface(
                    static,
                    static_kwargs.get("scale"),
                    static_kwargs.get("max_w"),
                    allow_transparent=True,
                )
                if image is not None:
                    return [], image, resolved_source
        return [], None, 0

    def _load_free_with_fallback(self, ref, fallback_ref, **kwargs):
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            resource = self._load_free_scaled_resource(candidate_ref, **kwargs)
            if resource is not None:
                return resource.image
        return None

    def _load_receptor_with_fallback(
        self, ref, fallback_ref, lane_w, flip_y=False,
    ):
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            resource = self._load_receptor_resource(
                candidate_ref,
                lane_w,
                flip_y=flip_y,
            )
            if resource is not None:
                return resource.image
        return None

    def _load_body_asset(
        self, ref, fallback_ref, lane_w, height_scale_w, flip_y=False,
    ):
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            surfaces, static, _ = self._resolve_asset_surfaces(candidate_ref)
            if surfaces:
                frames = []
                for surface in surfaces:
                    image = self._body_from_resolved_surface(
                        surface,
                        lane_w,
                        height_scale_w,
                        flip_y=flip_y,
                    )
                    if image is None:
                        break
                    frames.append(image)
                if frames:
                    return frames, frames[0]
            elif static is not None:
                image = self._body_from_resolved_surface(
                    static,
                    lane_w,
                    height_scale_w,
                    flip_y=flip_y,
                )
                if image is not None:
                    return [], image
        return [], None

    @staticmethod
    def _flip_image_x(image):
        if image is None:
            return None
        rgb_pm, alpha, width, height = image
        return (
            np.ascontiguousarray(rgb_pm[:, ::-1]),
            np.ascontiguousarray(alpha[:, ::-1]),
            width,
            height,
        )

    def combo_burst_for_event(self, event_index, side="left"):
        """Return a deterministic variant and side-specific orientation."""
        variants = self.combo_burst
        if not variants:
            return None
        random_order = bool(getattr(self.config, "combo_burst_random", False))
        if random_order:
            index = _legacy_deterministic_index(event_index, len(variants), 0xCB)
        else:
            index = int(event_index) % len(variants)
        if str(side).lower() == "right":
            right = self.combo_burst_right
            if len(right) != len(variants):
                right = self.combo_burst_right = [
                    self._flip_image_x(image) for image in variants
                ]
            return right[index]
        return variants[index]

    @staticmethod
    def _legacy_explosion_scale(config, width_480, column_width_480):
        """LegacySkin.lookupForMania 的 ExplosionScale / HoldNoteLightScale。

        版本 < 2.5 固定 1；否则 (LightingNWidth 或 ColumnWidth)×1.6/48，
        即 480 空间宽度 / 30。
        """
        if config.version < 2.5:
            return 1.0
        width = width_480 if width_480 > 0 else column_width_480
        return max(0.0, float(width)) / 30.0

    @staticmethod
    def _to_rgba(path):
        surf = pygame.image.load(path)
        w, h = surf.get_size()
        data = pygame.image.tostring(surf, "RGBA")
        return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)

    @staticmethod
    def _premul(arr):
        a = arr[:, :, 3:4].astype(np.float32) / 255.0
        rgb_pm = arr[:, :, :3].astype(np.float32) * a
        return rgb_pm, a

    @staticmethod
    def _display_size(width, height, is_2x):
        scale_adjust = 2.0 if is_2x else 1.0
        return width / scale_adjust, height / scale_adjust

    def _load_scaled(self, ref, lane_w, target_h=None, crop_visible=False, allow_transparent=False, height_scale_w=None, tint=None, flip_y=False):
        """加载并按列宽缩放（保持原图长宽比）→ (rgb_pm, alpha, w, h)"""
        resource = self._load_scaled_resource(
            ref,
            lane_w,
            target_h=target_h,
            crop_visible=crop_visible,
            allow_transparent=allow_transparent,
            height_scale_w=height_scale_w,
            tint=tint,
            flip_y=flip_y,
        )
        return resource.image if resource is not None else None

    def _load_scaled_resource(self, ref, lane_w, **kwargs):
        if not ref:
            return None
        resolved = self._resolve_surface(ref)
        if resolved is None or resolved.surface is None:
            return None
        image = self._scale_resolved_surface(resolved, lane_w, **kwargs)
        return (
            _LoadedImage(image, resolved.source)
            if image is not None
            else None
        )

    def _scale_resolved_surface(
        self,
        resolved,
        lane_w,
        target_h=None,
        crop_visible=False,
        allow_transparent=False,
        height_scale_w=None,
        tint=None,
        flip_y=False,
    ):
        surf = resolved.surface
        ow, oh = surf.get_size()
        if ow == 0 or oh == 0:
            return None
        if crop_visible:
            surf = self._crop_visible_surface(surf)
            if surf is None:
                return None
            ow, oh = surf.get_size()
        display_w, display_h = self._display_size(ow, oh, resolved.is_2x)
        if flip_y:
            surf = pygame.transform.flip(surf, False, True)
        tw = max(1, int(round(lane_w)))
        if target_h is None:
            h_w = float(height_scale_w) if height_scale_w is not None else tw
            th = max(1, int(round(display_h * h_w / display_w)))
        else:
            th = max(1, int(round(target_h)))
        surf = self._scale_surface(surf, (tw, th))
        arr = self._to_rgba_from_surface(surf)
        rgb_pm, a = self._premul(arr)
        if tint is not None:
            tint_rgb = np.array(tint[:3], dtype=np.float32).reshape(1, 1, 3)
            tint_alpha = float(tint[3]) if len(tint) > 3 else 1.0
            rgb_pm = rgb_pm * tint_rgb * tint_alpha
            a = a * tint_alpha
        return (rgb_pm, a, tw, th)

    def _load_scaled_animation(self, ref, lane_w, target_h=None, height_scale_w=None, tint=None, flip_y=False, allow_transparent=False):
        surfaces, _ = self._resolve_animation_surfaces(ref)
        frames = []
        for surface in surfaces:
            image = self._scale_resolved_surface(
                surface,
                lane_w,
                target_h=target_h,
                height_scale_w=height_scale_w,
                tint=tint,
                flip_y=flip_y,
                allow_transparent=allow_transparent,
            )
            if image is None:
                break
            frames.append(image)
        return frames

    @staticmethod
    def _effective_animation_fps(configured_fps, frame_count):
        if frame_count <= 0:
            return 0.0
        if configured_fps is None or configured_fps <= 0:
            return float(frame_count)
        return float(configured_fps)

    def _scale_free_surface(
        self,
        surf,
        is_2x,
        scale=None,
        max_w=None,
        allow_transparent=False,
        target_h=None,
    ):
        if scale is None:
            scale = config.SCALE
        ow, oh = surf.get_size()
        if ow == 0 or oh == 0:
            return None
        factor = scale * (0.5 if is_2x else 1.0)
        tw = max(1, int(round(ow * factor)))
        th = (
            max(1, int(round(target_h)))
            if target_h is not None
            else max(1, int(round(oh * factor)))
        )
        if max_w is not None and tw > max_w:
            fit = float(max_w) / float(tw)
            tw = max(1, int(round(tw * fit)))
            th = max(1, int(round(th * fit)))
        surf = self._scale_surface(surf, (tw, th))
        arr = self._to_rgba_from_surface(surf)
        rgb_pm, a = self._premul(arr)
        return (rgb_pm, a, tw, th)

    def _scale_free_resolved_surface(
        self,
        resolved,
        scale=None,
        max_w=None,
        allow_transparent=False,
        target_h=None,
    ):
        return self._scale_free_surface(
            resolved.surface,
            resolved.is_2x,
            scale,
            max_w,
            allow_transparent=allow_transparent,
            target_h=target_h,
        )

    def _load_free_animation(self, ref, scale=None, max_w=None, preserve_transparent=False):
        surfaces, _ = self._resolve_animation_surfaces(ref)
        frames = []
        for surface in surfaces:
            frame = self._scale_free_resolved_surface(
                surface,
                scale,
                max_w,
                allow_transparent=True,
            )
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _load_hit_result_frames(self, ref, max_w, fallback_ref=None):
        frames, image = self._load_free_asset(
            ref,
            fallback_ref,
            max_w=max_w,
            preserve_transparent=True,
        )
        return frames or ([image] if image is not None else [])

    @staticmethod
    def _animation_frame_index(frame_count, fps, age_ms, loop):
        if frame_count <= 1:
            return 0
        try:
            fps = float(fps)
            age_ms = float(age_ms)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(fps) or fps <= 0.0:
            return 0
        if math.isnan(age_ms) or age_ms <= 0.0:
            return 0
        if math.isinf(age_ms):
            return 0 if loop else frame_count - 1
        frame_index = math.floor(age_ms * fps / 1000.0)
        if loop:
            return frame_index % frame_count
        return min(frame_index, frame_count - 1)

    @classmethod
    def _once_frame_for_age(cls, frames, fps, age_ms):
        if not frames:
            return None
        return frames[
            cls._animation_frame_index(len(frames), fps, age_ms, loop=False)
        ]

    @classmethod
    def _loop_frame_for_age(cls, frames, fps, age_ms):
        if not frames:
            return None
        return frames[
            cls._animation_frame_index(len(frames), fps, age_ms, loop=True)
        ]

    def note_for_age(self, col_idx, age_ms):
        if not (0 <= col_idx < len(self.note)):
            return None
        return self._loop_frame_for_age(
            self.note_frames[col_idx], LEGACY_MANIA_NOTE_FPS, age_ms,
        ) or self.note[col_idx]

    def hold_piece_for_age(self, col_idx, piece, age_ms, is_holding=False):
        if not (0 <= col_idx < len(self.head)):
            return None
        if piece == "head":
            frames, fallback = self.head_frames[col_idx], self.head[col_idx]
            fps = LEGACY_MANIA_NOTE_FPS
        elif piece == "tail":
            frames, fallback = self.tail_frames[col_idx], self.tail[col_idx]
            fps = LEGACY_MANIA_NOTE_FPS
        elif piece == "body":
            frames, fallback = self.body_frames[col_idx], self.body[col_idx]
            fps = 1000.0 / LEGACY_MANIA_BODY_FRAME_MS
        else:
            return None
        return self._loop_frame_for_age(frames, fps, age_ms) or fallback

    def scorebar_colour_for_time(self, time_ms):
        if self.scorebar_colour_frames:
            return self._loop_frame_for_age(
                self.scorebar_colour_frames,
                self.scorebar_colour_fps,
                time_ms,
            )
        return self.scorebar_colour.image if self.scorebar_colour.present else None

    def _hit_result_key(self, value):
        if value in self.hit_result_frames_by_value:
            return value
        if value == 320 and 300 in self.hit_result_frames_by_value:
            return 300
        return None

    def hit_result_for_age(self, age_ms, value=320):
        key = self._hit_result_key(value)
        if key is None:
            return None
        return self._loop_frame_for_age(
            self.hit_result_frames_by_value[key],
            self.hit_result_fps_by_value.get(key, 0.0),
            age_ms,
        )

    def stage_light_for_age(self, col_idx, age_ms):
        if not (0 <= col_idx < len(self.stage_light)):
            return None
        if self.stage_light_frames[col_idx]:
            return self._loop_frame_for_age(
                self.stage_light_frames[col_idx],
                self.stage_light_fps[col_idx],
                age_ms,
            )
        return self.stage_light[col_idx]

    def stage_hint_for_time(self, stage_idx, time_ms):
        if not (0 <= stage_idx < len(self.stage_hints)):
            return None
        return self.stage_hints[stage_idx]

    def stage_bottom_for_age(self, age_ms):
        if self.stage_bottom_frames:
            return self._loop_frame_for_age(self.stage_bottom_frames, self.stage_bottom_fps, age_ms)
        return self.stage_bottom

    def lighting_for_age(self, col_idx, is_hold, age_ms):
        if not (0 <= col_idx < len(self.light_n)):
            return None
        frames = self.light_l_frames[col_idx] if is_hold else self.light_n_frames[col_idx]
        fps = self.light_l_fps[col_idx] if is_hold else self.light_n_fps[col_idx]
        if frames:
            return self._once_frame_for_age(frames, fps, age_ms)
        return self.light_l[col_idx] if is_hold else self.light_n[col_idx]

    def hold_light_for_age(self, col_idx, age_ms):
        """LegacyBodyPiece 的 lightingL：按住期间循环播放（非一次性）。"""
        if not (0 <= col_idx < len(self.light_l)):
            return None
        frames = self.light_l_frames[col_idx]
        if frames:
            fps = self.light_l_fps[col_idx]
            return self._loop_frame_for_age(frames, fps, age_ms)
        return self.light_l[col_idx]

    def _load_free_scaled(self, ref, scale=None, max_w=None, allow_transparent=False):
        resource = self._load_free_scaled_resource(
            ref,
            scale=scale,
            max_w=max_w,
            allow_transparent=allow_transparent,
        )
        return resource.image if resource is not None else None

    def _load_free_scaled_resource(
        self,
        ref,
        scale=None,
        max_w=None,
        allow_transparent=False,
        target_h=None,
        source=None,
    ):
        if not ref:
            return None
        resolved = self._resolve_surface(ref, source=source)
        if resolved is None or resolved.surface is None:
            return None
        image = self._scale_free_resolved_surface(
            resolved,
            scale,
            max_w,
            allow_transparent=allow_transparent,
            target_h=target_h,
        )
        return (
            _LoadedImage(image, resolved.source)
            if image is not None
            else None
        )

    @staticmethod
    def _image_resource(image, source=0):
        if image is None:
            return SkinImageResource()
        return SkinImageResource(
            present=True,
            image=image,
            width=image[2],
            height=image[3],
            source=source,
        )

    def _load_image_resource(
        self, ref, scale=None, max_w=None, fallback_ref=None, source=None,
    ):
        for candidate_ref in self._reference_chain(ref, fallback_ref):
            resource = self._load_free_scaled_resource(
                candidate_ref,
                scale=scale,
                max_w=max_w,
                allow_transparent=True,
                source=source,
            )
            if resource is not None:
                return self._image_resource(resource.image, resource.source)
        return SkinImageResource()

    def _load_font_glyph(self, ref):
        resolved = self._resolve_surface(ref)
        if resolved is None or resolved.surface is None:
            return None
        source_w, source_h = resolved.surface.get_size()
        density = 2.0 if resolved.is_2x else 1.0
        screen_scale = _legacy_internal_screen_scale()
        img = self._scale_free_resolved_surface(
            resolved,
            scale=screen_scale,
            allow_transparent=True,
        )
        if img is None:
            return None
        return SkinFontGlyph(
            image=img,
            logical_width=source_w / density * screen_scale,
            logical_height=source_h / density * screen_scale,
        )

    def _load_font_glyphs(self, prefix, chars):
        glyphs = {}
        names = {",": "comma", ".": "dot", "%": "percent", "x": "x", "X": "x"}
        for ch in chars:
            suffix = names.get(ch, ch)
            img = self._load_font_glyph(f"{prefix}-{suffix}")
            if img is not None:
                glyphs[ch] = img
        return glyphs

    def _load_receptor(self, ref, lane_w, flip_y=False):
        resource = self._load_receptor_resource(ref, lane_w, flip_y=flip_y)
        return resource.image if resource is not None else None

    def _load_receptor_resource(self, ref, lane_w, flip_y=False):
        if not ref:
            return None
        resolved = self._resolve_surface(ref)
        if resolved is None or resolved.surface is None:
            return None
        image = self._receptor_from_resolved_surface(
            resolved,
            lane_w,
            flip_y=flip_y,
        )
        return (
            _LoadedImage(image, resolved.source)
            if image is not None
            else None
        )

    def _receptor_from_resolved_surface(self, resolved, lane_w, flip_y=False):
        surf = resolved.surface
        ow, oh = surf.get_size()
        if ow == 0 or oh == 0:
            return None
        _, display_h = self._display_size(ow, oh, resolved.is_2x)
        tw = max(1, int(round(lane_w)))
        th = max(1, int(round(display_h * _legacy_internal_screen_scale())))
        if flip_y:
            surf = pygame.transform.flip(surf, False, True)
        surf = self._scale_surface(surf, (tw, th))
        arr = self._to_rgba_from_surface(surf)
        rgb_pm, a = self._premul(arr)
        return (rgb_pm, a, tw, th)

    def _load_body(self, ref, lane_w, height_scale_w, flip_y=False):
        """LN body：缩放到列宽、保留全高，供纵向按区域拉伸采样 → (rgb_pm, alpha, h)"""
        if not ref:
            return None
        resolved = self._resolve_surface(ref)
        if resolved is None or resolved.surface is None:
            return None
        return self._body_from_resolved_surface(
            resolved,
            lane_w,
            height_scale_w,
            flip_y=flip_y,
        )

    def _body_from_resolved_surface(
        self,
        resolved,
        lane_w,
        height_scale_w,
        flip_y=False,
    ):
        surf = resolved.surface
        ow, oh = surf.get_size()
        if ow == 0 or oh == 0:
            return None
        display_w, display_h = self._display_size(ow, oh, resolved.is_2x)
        tw = max(1, int(round(lane_w)))
        th = max(1, int(round(display_h * float(height_scale_w) / display_w)))
        cache = getattr(self, "_body_image_cache", None)
        if cache is None:
            cache = self._body_image_cache = {}
        path = getattr(resolved, "path", "")
        cache_key = None
        if path:
            cache_key = (
                int(resolved.source),
                os.path.normcase(os.path.abspath(path)),
                bool(resolved.is_2x),
                tw,
                th,
                bool(flip_y),
            )
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        if flip_y:
            surf = pygame.transform.flip(surf, False, True)
        surf = self._scale_surface(surf, (tw, th))
        arr = self._to_rgba_from_surface(surf)
        rgb_pm, a = self._premul(arr)
        image = (rgb_pm, a, tw, th)
        if cache_key is not None:
            cache[cache_key] = image
        return image

    def _load_body_animation(self, ref, lane_w, height_scale_w, flip_y=False):
        surfaces, _ = self._resolve_animation_surfaces(ref)
        frames = []
        for surface in surfaces:
            image = self._body_from_resolved_surface(
                surface,
                lane_w,
                height_scale_w,
                flip_y=flip_y,
            )
            if image is None:
                break
            frames.append(image)
        return frames

    @staticmethod
    def _to_rgba_from_surface(surf):
        w, h = surf.get_size()
        data = pygame.image.tostring(surf, "RGBA")
        return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)

    @staticmethod
    def _scale_surface(surf, size):
        width, height = surf.get_size()
        edge = DEGENERATE_SCALE_EDGE
        if width <= edge or height <= edge:
            return pygame.transform.scale(surf, size)
        try:
            return pygame.transform.smoothscale(surf, size)
        except ValueError:
            return pygame.transform.scale(surf, size)

    @staticmethod
    def _has_visible_pixels(arr):
        return bool(np.max(arr[:, :, 3]) > 0)

    def _crop_visible_surface(self, surf):
        arr = self._to_rgba_from_surface(surf)
        alpha = arr[:, :, 3]
        ys, xs = np.where(alpha > 0)
        if len(xs) == 0 or len(ys) == 0:
            return None
        x1, x2 = int(xs.min()), int(xs.max()) + 1
        y1, y2 = int(ys.min()), int(ys.max()) + 1
        return surf.subsurface(pygame.Rect(x1, y1, x2 - x1, y2 - y1)).copy()

    def _crop_visible_y_surface(self, surf):
        arr = self._to_rgba_from_surface(surf)
        alpha = arr[:, :, 3]
        ys, _ = np.where(alpha > 0)
        if len(ys) == 0:
            return None
        y1, y2 = int(ys.min()), int(ys.max()) + 1
        return surf.subsurface(pygame.Rect(0, y1, surf.get_width(), y2 - y1)).copy()
