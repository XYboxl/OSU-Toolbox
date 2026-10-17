import os
import sys
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import pygame

ROOT = Path(__file__).resolve().parents[1]
SKINS = ROOT / "skins"
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core
from osu_toolbox.legacy_constants import (
    LEGACY_SKIN_SOURCE_BEATMAP,
    LEGACY_SKIN_SOURCE_SELECTED,
)


COMPATIBILITY_MODES = (
    core.COMPAT_STABLE_REPLAY,
    core.COMPAT_LAZER_CLASSIC,
)
OUTPUT_SIZES = (
    (960, 540),
    (1920, 1080),
)


class LegacySkinGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def tearDown(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
            width=960,
            height=540,
            scroll_direction="auto",
        )

    def _load_receptors(self, skin_dir, width, height, mode):
        core.configure_runtime(
            compatibility_mode=mode,
            width=width,
            height=height,
        )
        config = core.load_skin_config(str(skin_dir), 4)
        self.assertIsNotNone(config)
        lane_rects, _ = core.lane_geometry(width, height, 4, config)
        loader = object.__new__(core.SkinAssets)
        loader.config = config
        receptor = []
        receptor_down = []
        for index, (x1, _, x2, _) in enumerate(lane_rects):
            lane_width = x2 - x1
            receptor.append(loader._load_receptor(
                config.key_image[index],
                lane_width,
                flip_y=config.upside_down and config.key_flip[index],
            ))
            receptor_down.append(loader._load_receptor(
                config.key_down_image[index],
                lane_width,
                flip_y=config.upside_down and config.key_down_flip[index],
            ))
        return (
            config,
            SimpleNamespace(
                receptor=receptor,
                receptor_down=receptor_down,
            ),
            lane_rects,
        )

    def _load_health_renderer(
        self,
        skin_dir,
        width,
        height,
        mode,
        beatmap_root=None,
    ):
        core.configure_runtime(
            compatibility_mode=mode,
            width=width,
            height=height,
        )
        config = core.load_skin_config(
            str(skin_dir),
            4,
            beatmap_root=beatmap_root,
        )
        self.assertIsNotNone(config)
        loader = object.__new__(core.SkinAssets)
        loader.config = config
        loader._load_scorebar_resources(config)
        lane_rects, _ = core.lane_geometry(width, height, 4, config)
        renderer = object.__new__(core.ManiaRenderer)
        renderer.width = width
        renderer.height = height
        renderer.stage_x2 = lane_rects[-1][2]
        renderer.assets = loader
        return config, renderer

    @staticmethod
    def _alpha_bounds(image):
        alpha = image[1][:, :, 0]
        ys, xs = np.where(alpha > 0.001)
        if not len(xs):
            return None
        return (
            int(xs.min()),
            int(ys.min()),
            int(xs.max()) + 1,
            int(ys.max()) + 1,
        )

    @staticmethod
    def _save_surface(path, size, colour):
        surface = pygame.Surface(size, pygame.SRCALPHA)
        surface.fill(colour)
        pygame.image.save(surface, str(path))

    @staticmethod
    def _write_minimal_skin(root, extra_lines):
        lines = [
            "[General]",
            "Version: 2.7",
            "",
            "[Mania]",
            "Keys: 4",
            "ColumnStart: 136",
            "ColumnWidth: 30,30,30,30",
            *extra_lines,
            "",
        ]
        (root / "skin.ini").write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _skin_image(width, height, alpha_rows=None):
        alpha = np.ones((height, width, 1), dtype=np.float32)
        if alpha_rows is not None:
            alpha[:, :, 0] = np.asarray(alpha_rows, dtype=np.float32).reshape(height, 1)
        rgb_pm = np.repeat(alpha, 3, axis=2) * 255.0
        return rgb_pm, alpha, width, height

    @staticmethod
    def _trace_health_draw(renderer, health, **kwargs):
        calls = []

        def blit(image, x, y, **kwargs):
            calls.append(("image", image, x, y, kwargs))

        def clipped(image, x, y, **kwargs):
            calls.append(("clipped", image, x, y, kwargs))

        def blend(*args, **kwargs):
            calls.append(("blend", args, kwargs))

        renderer._blit_skin_image = blit
        renderer._blit_skin_image_clipped = clipped
        renderer._blend_rect = blend
        renderer.draw_health_bar(health, **kwargs)
        return calls

    def test_receptor_canvas_uses_lane_width_and_legacy_texture_height(self):
        expected_540p = {
            "test1": (78, 200),
            "test2": (78, 132),
            "test3": (83, 132),
        }
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                for skin_name in expected_540p:
                    with self.subTest(mode=mode, size=(width, height), skin=skin_name):
                        config, assets, lane_rects = self._load_receptors(
                            SKINS / skin_name,
                            width,
                            height,
                            mode,
                        )
                        receptor = assets.receptor[0]
                        self.assertIsNotNone(receptor)
                        path, is_2x = core.resolve_skin_image(
                            config.root,
                            config.key_image[0],
                        )
                        source = pygame.image.load(path)
                        display_height = source.get_height() / (2.0 if is_2x else 1.0)
                        expected_width = lane_rects[0][2] - lane_rects[0][0]
                        expected_height = round(display_height * height / 768.0)
                        self.assertEqual(receptor[2:], (expected_width, expected_height))
                        if height == 540:
                            self.assertEqual(receptor[2:], expected_540p[skin_name])

                        bounds = self._alpha_bounds(receptor)
                        self.assertIsNotNone(bounds)
                        visible_width = bounds[2] - bounds[0]
                        visible_height = bounds[3] - bounds[1]
                        self.assertGreater(visible_width / visible_height, 0.85)
                        self.assertLess(visible_width / visible_height, 1.2)

    def test_arrow_receptor_preserves_the_full_unmasked_canvas(self):
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                with self.subTest(mode=mode, size=(width, height)):
                    config, assets, _ = self._load_receptors(
                        SKINS / "test2",
                        width,
                        height,
                        mode,
                    )
                    receptor = assets.receptor[0]
                    path, _ = core.resolve_skin_image(config.root, config.key_image[0])
                    source = pygame.image.load(path)
                    expected = pygame.transform.smoothscale(
                        source,
                        (receptor[2], receptor[3]),
                    )
                    expected_rgba = core.SkinAssets._to_rgba_from_surface(expected)
                    np.testing.assert_allclose(
                        receptor[1][:, :, 0],
                        expected_rgba[:, :, 3].astype(np.float32) / 255.0,
                        atol=1.0 / 255.0,
                    )

    def test_degenerate_skin_sources_bypass_smoothscale(self):
        """1x1 placeholder art must not reach smoothscale.

        pygame 2.6.1's ``smoothscale`` intermittently faults with an access
        violation when a 1x1 source is upscaled by ~1000x, which is exactly
        what skins using "nothing to draw" placeholders request (test4 names a
        1x1 transparent PNG ``full`` for StageHint/StageLeft/StageRight/
        StageLight/KeyImage).  A source that small carries no detail, so the
        plain scaler reproduces smoothscale byte for byte.  This pins both the
        routing and the pixel equivalence, and confirms detailed art still
        takes the smooth path.
        """
        degenerate_sizes = ((1102, 1590), (4167, 1249), (225, 225), (7, 3))
        for edge in (1, 2):
            with self.subTest(edge=edge):
                source = pygame.Surface((edge, edge), pygame.SRCALPHA)
                source.fill((12, 34, 56, 78))
                for size in degenerate_sizes:
                    produced = core.SkinAssets._scale_surface(source, size)
                    self.assertEqual(produced.get_size(), size)
                    np.testing.assert_array_equal(
                        core.SkinAssets._to_rgba_from_surface(produced),
                        core.SkinAssets._to_rgba_from_surface(
                            pygame.transform.scale(source, size)
                        ),
                    )

        # Detailed art must keep the smooth path: the two scalers really do
        # differ there, so an exact match identifies which one ran.
        detailed = pygame.image.load(str(SKINS / "test4" / "hit300.png"))
        produced = core.SkinAssets._to_rgba_from_surface(
            core.SkinAssets._scale_surface(detailed, (225, 225))
        )
        smooth = core.SkinAssets._to_rgba_from_surface(
            pygame.transform.smoothscale(detailed, (225, 225))
        )
        nearest = core.SkinAssets._to_rgba_from_surface(
            pygame.transform.scale(detailed, (225, 225))
        )
        np.testing.assert_array_equal(produced, smooth)
        self.assertFalse(np.array_equal(smooth, nearest))

    def test_transparent_pressed_receptor_hides_normal_receptor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            extra = []
            for column in range(4):
                extra.extend([
                    f"KeyImage{column}: key",
                    f"KeyImage{column}D: key-down",
                ])
            self._write_minimal_skin(root, extra)

            normal = pygame.Surface((32, 64), pygame.SRCALPHA)
            normal.fill((0, 0, 0, 0))
            pygame.draw.rect(normal, (255, 255, 255, 255), (2, 20, 28, 28))
            pygame.image.save(normal, str(root / "key.png"))
            self._save_surface(root / "key-down.png", (32, 64), (0, 0, 0, 0))

            for mode in COMPATIBILITY_MODES:
                for width, height in OUTPUT_SIZES:
                    with self.subTest(mode=mode, size=(width, height)):
                        core.configure_runtime(
                            compatibility_mode=mode,
                            width=width,
                            height=height,
                        )
                        config = core.load_skin_config(str(root), 4)
                        renderer = core.ManiaRenderer(width, height, 4, config)
                        pressed = renderer.assets.receptor_down[0]
                        self.assertIsNotNone(pressed)
                        self.assertEqual(float(pressed[1].max()), 0.0)

                        renderer.reset_frame()
                        before = renderer.frame.copy()
                        renderer.draw_receptors(set())
                        self.assertFalse(np.array_equal(renderer.frame, before))

                        renderer.reset_frame()
                        before = renderer.frame.copy()
                        renderer.draw_receptors({0, 1, 2, 3})
                        self.assertTrue(np.array_equal(renderer.frame, before))

    def test_receptor_anchors_to_full_key_area_edge(self):
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                for direction in ("down", "up"):
                    with self.subTest(
                        mode=mode,
                        size=(width, height),
                        direction=direction,
                    ):
                        _, assets, lane_rects = self._load_receptors(
                            SKINS / "test2",
                            width,
                            height,
                            mode,
                        )
                        renderer = object.__new__(core.ManiaRenderer)
                        renderer.height = height
                        renderer.geometry = SimpleNamespace(
                            upside_down=direction == "up",
                        )
                        renderer.lane_rects = lane_rects
                        renderer.assets = assets
                        calls = []
                        renderer._blit_skin_image = (
                            lambda image, x, y, **kwargs:
                            calls.append((image, x, y, kwargs))
                        )

                        renderer.draw_receptors(set())

                        self.assertEqual(len(calls), 4)
                        for col, call in enumerate(calls):
                            receptor = assets.receptor[col]
                            expected_y = 0 if direction == "up" else height - receptor[3]
                            self.assertIs(call[0], receptor)
                            self.assertEqual(call[2], expected_y)

    def test_scorebar_resources_use_768p_texture_scale(self):
        references = (
            ("test2", "scorebar_colour", "scorebar_colour"),
            ("test3", "scorebar_bg", "scorebar_bg"),
            ("test3", "scorebar_colour", "scorebar_colour"),
        )
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                for skin_name, attribute, config_attribute in references:
                    with self.subTest(
                        mode=mode,
                        size=(width, height),
                        skin=skin_name,
                        resource=attribute,
                    ):
                        config, renderer = self._load_health_renderer(
                            SKINS / skin_name,
                            width,
                            height,
                            mode,
                        )
                        resource = getattr(renderer.assets, attribute)
                        path, is_2x = core.resolve_skin_image(
                            config.root,
                            getattr(config, config_attribute),
                        )
                        source = pygame.image.load(path)
                        density_scale = 0.5 if is_2x else 1.0
                        expected = (
                            round(source.get_width() * density_scale * height / 768.0),
                            round(source.get_height() * density_scale * height / 768.0),
                        )
                        self.assertTrue(resource.present)
                        self.assertEqual((resource.width, resource.height), expected)

    def test_stage_side_resources_use_768p_width_and_full_screen_height(self):
        height = 1080
        core.configure_runtime(width=1920, height=height)
        skin = core.load_skin_config(str(SKINS / "test2"), 4)
        layout = core.resolve_stage_layout(1920, height, 4, skin)
        assets = core.SkinAssets(
            skin,
            layout.lane_rects,
            stage_ranges=layout.stage_ranges,
            stage_scale=layout.scale_factor,
        )
        path, is_2x = core.resolve_skin_image(skin.root, skin.stage_right)
        source = pygame.image.load(path)
        density_scale = 0.5 if is_2x else 1.0
        expected = (
            round(source.get_width() * density_scale * height / 768.0),
            height,
        )
        self.assertEqual(assets.stage_right[2:], expected)

    def test_test2_stage_right_preserves_canvas_and_anchors_to_screen(self):
        skin_dir = SKINS / "test2"
        if not skin_dir.is_dir():
            self.skipTest("local test2 Stable reference skin is unavailable")

        width, height = 1920, 1080
        core.configure_runtime(width=width, height=height)
        skin = core.load_skin_config(str(skin_dir), 4)
        layout = core.resolve_stage_layout(width, height, 4, skin)
        assets = core.SkinAssets(
            skin,
            layout.lane_rects,
            stage_ranges=layout.stage_ranges,
            stage_scale=layout.scale_factor,
        )
        stage_right = assets.stage_right
        path, is_2x = core.resolve_skin_image(skin.root, skin.stage_right)
        source = pygame.image.load(path)
        density_scale = 0.5 if is_2x else 1.0
        natural_size = (
            round(source.get_width() * density_scale * height / 768.0),
            round(source.get_height() * density_scale * height / 768.0),
        )
        expected_size = (natural_size[0], height)

        self.assertEqual(source.get_size(), (1054, 1540))
        self.assertEqual(natural_size, (741, 1083))
        self.assertEqual(expected_size, (741, 1080))
        self.assertEqual(stage_right[2:], expected_size)
        expected_surface = core.SkinAssets._scale_surface(source, expected_size)
        expected_rgba = core.SkinAssets._to_rgba_from_surface(expected_surface)
        np.testing.assert_allclose(
            stage_right[1][:, :, 0],
            expected_rgba[:, :, 3].astype(np.float32) / 255.0,
            atol=1.0 / 255.0,
        )

        renderer = object.__new__(core.ManiaRenderer)
        renderer.width = width
        renderer.height = height
        renderer.frame = np.zeros((height, width, 3), dtype=np.uint8)
        renderer.skin_config = skin
        renderer.lane_rects = layout.lane_rects
        renderer.stage_ranges = layout.stage_ranges
        renderer.assets = assets
        renderer.y_judge = 0.0
        renderer._paint_background_image = lambda: False
        renderer._blend_rect = lambda *args, **kwargs: None
        renderer._draw_vline = lambda *args, **kwargs: None
        calls = []
        renderer._blit_skin_image = (
            lambda image, x, y, **kwargs:
            calls.append((image, x, y, kwargs))
        )

        renderer._build_bg_layer()

        right_call = next(call for call in calls if call[0] is stage_right)
        self.assertEqual(layout.stage_ranges, ((641.25, 1284.75),))
        self.assertEqual(right_call[1:3], (1285, 0))
        self.assertEqual(right_call[3]["override_h"], height)

    def test_stage_bottom_anchors_to_full_playfield_edge(self):
        image = self._skin_image(20, 50)
        for upside_down, expected_y in ((False, 150), (True, 0)):
            with self.subTest(upside_down=upside_down):
                renderer = object.__new__(core.ManiaRenderer)
                renderer.height = 200
                renderer.stage_y1 = 40
                renderer.stage_y2 = 120
                renderer.stage_ranges = ((10, 90),)
                renderer.geometry = SimpleNamespace(upside_down=upside_down)
                renderer.assets = SimpleNamespace(
                    stage_bottom_for_age=lambda _age: image,
                )
                calls = []
                renderer._blit_skin_image = (
                    lambda value, x, y, **kwargs:
                    calls.append((value, x, y, kwargs))
                )

                renderer.draw_stage_foreground(0.0)

                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2], expected_y)

    def test_test4_stage_bottom_matches_stable_screen_edge_anchor(self):
        skin_dir = SKINS / "test4"
        if not skin_dir.is_dir():
            self.skipTest("local test4 Stable reference skin is unavailable")

        width, height = 1920, 1080
        core.configure_runtime(width=width, height=height)
        skin = core.load_skin_config(str(skin_dir), 4)
        layout = core.resolve_stage_layout(width, height, 4, skin)
        assets = core.SkinAssets(
            skin,
            layout.lane_rects,
            stage_ranges=layout.stage_ranges,
            stage_scale=layout.scale_factor,
        )
        stage_bottom = assets.stage_bottom_for_age(0.0)
        self.assertIsNotNone(stage_bottom)
        self.assertEqual(stage_bottom[2:], (4167, 1249))

        renderer = object.__new__(core.ManiaRenderer)
        renderer.height = height
        renderer.stage_ranges = layout.stage_ranges
        renderer.geometry = SimpleNamespace(upside_down=False)
        renderer.assets = assets
        calls = []
        renderer._blit_skin_image = (
            lambda image, x, y, **kwargs:
            calls.append((image, x, y, kwargs))
        )

        renderer.draw_stage_foreground(0.0)

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], stage_bottom)
        self.assertEqual(calls[0][1:3], (-1195, -169))

    def test_score_and_combo_fonts_use_768p_texture_scale(self):
        for skin_name, font_name, glyphs_name in (
            ("test1", "score_prefix", "score_font"),
            ("test2", "score_prefix", "score_font"),
            ("test2", "combo_prefix", "combo_font"),
        ):
            with self.subTest(skin=skin_name, font=glyphs_name):
                height = 1080
                config, renderer = self._load_health_renderer(
                    SKINS / skin_name,
                    1920,
                    height,
                    core.COMPAT_STABLE_REPLAY,
                )
                loader = object.__new__(core.SkinAssets)
                loader.config = config
                glyphs = loader._load_font_glyphs(
                    getattr(config, font_name), "0",
                )
                glyph = glyphs["0"]
                path, is_2x = core.resolve_skin_image(
                    config.root,
                    f"{getattr(config, font_name)}-0",
                )
                source = pygame.image.load(path)
                density_scale = 0.5 if is_2x else 1.0
                expected = (
                    round(source.get_width() * density_scale * height / 768.0),
                    round(source.get_height() * density_scale * height / 768.0),
                )
                self.assertEqual(glyph[2:4], expected)

    def test_transparent_scorebar_resources_remain_present(self):
        for skin_name, expected_new_style in (
            ("test1", True),
            ("test2", True),
            ("test3", False),
        ):
            with self.subTest(skin=skin_name):
                _, renderer = self._load_health_renderer(
                    SKINS / skin_name,
                    960,
                    540,
                    core.COMPAT_STABLE_REPLAY,
                )
                assets = renderer.assets
                self.assertTrue(assets.scorebar_bg.present)
                self.assertEqual(
                    assets.scorebar_marker.present,
                    expected_new_style,
                )
                if skin_name != "test3":
                    self.assertEqual(float(assets.scorebar_bg.image[1].max()), 0.0)

    def test_transparent_custom_scorebar_suppresses_fallback(self):
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                with self.subTest(mode=mode, size=(width, height)):
                    core.configure_runtime(
                        compatibility_mode=mode,
                        width=width,
                        height=height,
                    )
                    _, renderer = self._load_health_renderer(
                        SKINS / "test1",
                        width,
                        height,
                        mode,
                    )
                    renderer.frame = np.zeros((height, width, 3), dtype=np.uint8)
                    renderer._tmp_color = np.empty(3, dtype=np.float32)
                    renderer._image_transform_cache = OrderedDict()
                    before = renderer.frame.copy()
                    renderer.draw_health_bar(0.49)
                    self.assertTrue(np.array_equal(renderer.frame, before))

    def test_new_style_scorebar_offsets_tint_and_marker_position(self):
        health_values = (0.0, 0.19, 0.49, 0.5, 1.0)
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                core.configure_runtime(
                    compatibility_mode=mode,
                    width=width,
                    height=height,
                )
                _, renderer = self._load_health_renderer(
                    SKINS / "test2",
                    width,
                    height,
                    mode,
                )
                bg = renderer.assets.scorebar_bg.image
                colour = renderer.assets.scorebar_colour.image
                marker = renderer.assets.scorebar_marker.image
                fill_x = round(7.5 * height / 480.0)
                fill_y = round(7.8 * height / 480.0)
                for health in health_values:
                    with self.subTest(mode=mode, size=(width, height), health=health):
                        calls = self._trace_health_draw(renderer, health)
                        self.assertFalse(any(call[0] == "blend" for call in calls))
                        stable = mode == core.COMPAT_STABLE_REPLAY
                        if stable:
                            self.assertFalse(any(
                                call[0] == "clipped" for call in calls
                            ))
                            self.assertFalse(any(
                                call[0] == "image" and call[1] is marker
                                for call in calls
                            ))
                            bg_call = calls[0]
                            bg_anchor = (
                                renderer.stage_x2
                                + core.LEGACY_MANIA_HEALTH_BG_OFFSET_X_480
                                * height / 480.0,
                                float(height),
                            )
                            self.assertIs(bg_call[1], bg)
                            self.assertEqual(
                                bg_call[2:4],
                                core.ManiaRenderer._stable_mania_health_blit_position(
                                    *bg_anchor, bg[2], bg[3],
                                ),
                            )
                            fill_width = round(colour[2] * health)
                            if fill_width <= 0:
                                self.assertEqual(len(calls), 1)
                                continue
                            fill_call = calls[1]
                            fill_anchor = (
                                renderer.stage_x2
                                + core.LEGACY_MANIA_HEALTH_NEW_OFFSET_X_480
                                * height / 480.0,
                                core.LEGACY_MANIA_HEALTH_NEW_OFFSET_Y_480
                                * height / 480.0,
                            )
                            self.assertIs(fill_call[1], colour)
                            self.assertEqual(fill_call[4]["clip_w"], fill_width)
                            self.assertEqual(
                                fill_call[2:4],
                                core.ManiaRenderer._stable_mania_health_blit_position(
                                    *fill_anchor,
                                    fill_width,
                                    colour[3],
                                ),
                            )
                            self.assertEqual(
                                fill_call[4]["rotation"],
                                core.LEGACY_MANIA_HEALTH_ROTATION_DEG,
                            )
                            self.assertEqual(
                                fill_call[4]["scale"],
                                core.LEGACY_MANIA_HEALTH_SPRITE_SCALE,
                            )
                            continue
                        clipped = next(call for call in calls if call[0] == "clipped")
                        marker_call = [
                            call for call in calls
                            if call[0] == "image" and call[1] is marker
                        ][0]
                        fill_width = round(colour[2] * health)
                        tint = core.ManiaRenderer._legacy_health_colour(health)
                        self.assertEqual(clipped[2:4], (fill_x, fill_y))
                        self.assertEqual(clipped[4]["clip_w"], fill_width)
                        self.assertEqual(clipped[4]["clip_h"], colour[3])
                        self.assertEqual(clipped[4]["tint"], tint)
                        self.assertEqual(
                            marker_call[2:4],
                            (
                                round(fill_x + fill_width - marker[2] / 2.0),
                                round(fill_y + colour[3] / 2.0 - marker[3] / 2.0),
                            ),
                        )
                        self.assertEqual(marker_call[4]["tint"], tint)
                        self.assertEqual(
                            marker_call[4]["additive"],
                            health >= 0.5,
                        )

    def test_old_style_scorebar_offsets_and_marker_thresholds(self):
        health_values = (0.0, 0.19, 0.49, 0.5, 1.0)
        for mode in COMPATIBILITY_MODES:
            for width, height in OUTPUT_SIZES:
                core.configure_runtime(
                    compatibility_mode=mode,
                    width=width,
                    height=height,
                )
                _, renderer = self._load_health_renderer(
                    SKINS / "test3",
                    width,
                    height,
                    mode,
                )
                bg = renderer.assets.scorebar_bg.image
                colour = renderer.assets.scorebar_colour.image
                expected_markers = (
                    renderer.assets.scorebar_kidanger2.image,
                    renderer.assets.scorebar_kidanger2.image,
                    renderer.assets.scorebar_kidanger.image,
                    renderer.assets.scorebar_ki.image,
                    renderer.assets.scorebar_ki.image,
                )
                fill_x = round(3.0 * height / 480.0)
                fill_y = round(10.0 * height / 480.0)
                for health, marker in zip(health_values, expected_markers):
                    with self.subTest(mode=mode, size=(width, height), health=health):
                        calls = self._trace_health_draw(renderer, health)
                        self.assertFalse(any(call[0] == "blend" for call in calls))
                        stable = mode == core.COMPAT_STABLE_REPLAY
                        if stable:
                            self.assertFalse(any(
                                call[0] == "clipped" for call in calls
                            ))
                            self.assertFalse(any(
                                call[0] == "image" and any(
                                    call[1] is candidate
                                    for candidate in expected_markers
                                )
                                for call in calls
                            ))
                            bg_call = calls[0]
                            bg_anchor = (
                                renderer.stage_x2
                                + core.LEGACY_MANIA_HEALTH_BG_OFFSET_X_480
                                * height / 480.0,
                                float(height),
                            )
                            self.assertIs(bg_call[1], bg)
                            self.assertEqual(
                                bg_call[2:4],
                                core.ManiaRenderer._stable_mania_health_blit_position(
                                    *bg_anchor, bg[2], bg[3],
                                ),
                            )
                            fill_width = round(colour[2] * health)
                            if fill_width <= 0:
                                self.assertEqual(len(calls), 1)
                                continue
                            fill_call = calls[1]
                            fill_anchor = (
                                renderer.stage_x2
                                + core.LEGACY_MANIA_HEALTH_OLD_OFFSET_X_480
                                * height / 480.0,
                                core.LEGACY_MANIA_HEALTH_OLD_OFFSET_Y_480
                                * height / 480.0,
                            )
                            self.assertIs(fill_call[1], colour)
                            self.assertEqual(fill_call[4]["clip_w"], fill_width)
                            self.assertEqual(
                                fill_call[2:4],
                                core.ManiaRenderer._stable_mania_health_blit_position(
                                    *fill_anchor,
                                    fill_width,
                                    colour[3],
                                ),
                            )
                            continue
                        clipped = next(call for call in calls if call[0] == "clipped")
                        marker_call = [
                            call for call in calls
                            if call[0] == "image" and call[1] is marker
                        ][0]
                        fill_width = round(colour[2] * health)
                        self.assertEqual(clipped[2:4], (fill_x, fill_y))
                        self.assertEqual(clipped[4]["clip_w"], fill_width)
                        self.assertIsNone(clipped[4]["tint"])
                        self.assertEqual(
                            marker_call[2:4],
                            (
                                round(fill_x + fill_width - marker[2] / 2.0),
                                round(fill_y - marker[3] / 2.0),
                            ),
                        )
                        self.assertIsNone(marker_call[4]["tint"])
                        self.assertFalse(marker_call[4]["additive"])

    def test_test4_stable_health_rotates_and_grows_up_from_bottom(self):
        skin_dir = SKINS / "test4"
        if not skin_dir.is_dir():
            self.skipTest("local test4 Stable reference skin is unavailable")

        width, height = 1920, 1080
        _, renderer = self._load_health_renderer(
            skin_dir,
            width,
            height,
            core.COMPAT_STABLE_REPLAY,
        )
        bg = renderer.assets.scorebar_bg.image
        colour = renderer.assets.scorebar_colour.image
        self.assertFalse(renderer.assets.scorebar_marker.present)
        self.assertEqual(renderer.stage_x2, 1440)
        self.assertEqual(bg[2:], (704, 66))
        self.assertEqual(colour[2:], (610, 14))

        def rotated_canvas_bounds(call):
            image, x, y, kwargs = call[1], call[2], call[3], call[4]
            clip_w = kwargs.get("clip_w", image[2])
            scale = kwargs["scale"]
            scaled_w = max(1, round(clip_w * scale))
            scaled_h = max(1, round(image[3] * scale))
            draw_x = x + round((scaled_w - scaled_h) / 2.0)
            draw_y = y + round((scaled_h - scaled_w) / 2.0)
            return (
                draw_x,
                draw_y,
                draw_x + scaled_h,
                draw_y + scaled_w,
            )

        traces = {}
        for display in (1.0, 0.5):
            with self.subTest(health_display=display):
                calls = self._trace_health_draw(
                    renderer,
                    1.0,
                    health_display=display,
                )
                self.assertEqual([call[0] for call in calls], ["image", "image"])
                bg_call, fill_call = calls
                self.assertIs(bg_call[1], bg)
                self.assertIs(fill_call[1], colour)
                self.assertEqual(
                    bg_call[2:4],
                    core.ManiaRenderer._stable_mania_health_blit_position(
                        renderer.stage_x2
                        + core.LEGACY_MANIA_HEALTH_BG_OFFSET_X_480
                        * height / 480.0,
                        float(height),
                        bg[2],
                        bg[3],
                    ),
                )
                fill_width = round(colour[2] * display)
                self.assertEqual(fill_call[4]["clip_w"], fill_width)
                self.assertEqual(
                    fill_call[2:4],
                    core.ManiaRenderer._stable_mania_health_blit_position(
                        renderer.stage_x2
                        + core.LEGACY_MANIA_HEALTH_OLD_OFFSET_X_480
                        * height / 480.0,
                        core.LEGACY_MANIA_HEALTH_OLD_OFFSET_Y_480
                        * height / 480.0,
                        fill_width,
                        colour[3],
                    ),
                )
                for call in (bg_call, fill_call):
                    self.assertEqual(
                        call[4]["rotation"],
                        core.LEGACY_MANIA_HEALTH_ROTATION_DEG,
                    )
                    self.assertEqual(
                        call[4]["scale"],
                        core.LEGACY_MANIA_HEALTH_SPRITE_SCALE,
                    )
                self.assertEqual(
                    rotated_canvas_bounds(bg_call),
                    (1442, 587, 1488, 1080),
                )
                traces[display] = rotated_canvas_bounds(fill_call)

        self.assertEqual(traces[1.0], (1458, 649, 1468, 1076))
        self.assertEqual(traces[0.5], (1458, 862, 1468, 1076))
        self.assertEqual(traces[1.0][3], traces[0.5][3])
        self.assertGreater(traces[0.5][1], traces[1.0][1])

    def test_scorebar_background_is_resource_group_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_minimal_skin(root, [])
            self._save_surface(root / "scorebar-bg.png", (20, 10), (255, 255, 255, 255))

            core.configure_runtime(width=960, height=540)
            _, renderer = self._load_health_renderer(
                root,
                960,
                540,
                core.COMPAT_STABLE_REPLAY,
            )
            calls = self._trace_health_draw(renderer, 0.5)
            self.assertEqual([call[0] for call in calls], ["image"])
            self.assertIs(calls[0][1], renderer.assets.scorebar_bg.image)

            os.remove(root / "scorebar-bg.png")
            self._save_surface(root / "scorebar-colour.png", (20, 4), (255, 255, 255, 255))
            _, renderer = self._load_health_renderer(
                root,
                960,
                540,
                core.COMPAT_STABLE_REPLAY,
            )
            calls = self._trace_health_draw(renderer, 0.5)
            self.assertEqual(calls, [])

    def test_scorebar_style_uses_stable_resource_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            selected = base / "selected"
            selected.mkdir()
            self._write_minimal_skin(selected, [])
            self._save_surface(
                selected / "scorebar-bg.png",
                (40, 20),
                (255, 255, 255, 255),
            )
            self._save_surface(
                selected / "scorebar-colour.png",
                (30, 8),
                (255, 255, 255, 255),
            )
            _, renderer = self._load_health_renderer(
                selected,
                960,
                540,
                core.COMPAT_STABLE_REPLAY,
            )
            self.assertEqual(
                renderer.assets.scorebar_colour.source,
                LEGACY_SKIN_SOURCE_SELECTED,
            )
            self.assertFalse(renderer.assets.scorebar_marker.present)
            self.assertFalse(renderer.assets.scorebar_ki.present)
            self.assertFalse(renderer.assets.scorebar_kidanger.present)
            self.assertFalse(renderer.assets.scorebar_kidanger2.present)
            calls = self._trace_health_draw(renderer, 0.5)
            self.assertEqual([call[0] for call in calls], ["image", "image"])
            old_fill = calls[1]
            self.assertEqual(
                old_fill[4]["clip_w"],
                round(renderer.assets.scorebar_colour.width * 0.5),
            )

            self._save_surface(
                selected / "scorebar-marker.png",
                (6, 6),
                (255, 255, 255, 255),
            )
            _, renderer = self._load_health_renderer(
                selected,
                960,
                540,
                core.COMPAT_STABLE_REPLAY,
            )
            self.assertEqual(
                renderer.assets.scorebar_marker.source,
                LEGACY_SKIN_SOURCE_SELECTED,
            )
            calls = self._trace_health_draw(renderer, 0.5)
            self.assertEqual([call[0] for call in calls], ["image", "image"])
            new_fill = calls[1]
            self.assertEqual(
                new_fill[4]["clip_w"],
                round(renderer.assets.scorebar_colour.width * 0.5),
            )
            self.assertNotEqual(old_fill[2:4], new_fill[2:4])

    def test_scorebar_group_is_locked_to_beatmap_or_selected_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            beatmap = base / "beatmap"
            selected = base / "selected"
            beatmap.mkdir()
            selected.mkdir()
            self._write_minimal_skin(selected, [])

            self._save_surface(
                beatmap / "scorebar-bg.png",
                (40, 20),
                (255, 255, 255, 255),
            )
            self._save_surface(
                beatmap / "scorebar-colour.png",
                (30, 8),
                (255, 255, 255, 255),
            )
            self._save_surface(
                selected / "scorebar-marker.png",
                (6, 6),
                (255, 255, 255, 255),
            )
            _, renderer = self._load_health_renderer(
                selected,
                960,
                540,
                core.COMPAT_STABLE_REPLAY,
                beatmap_root=beatmap,
            )
            self.assertEqual(
                renderer.assets.scorebar_bg.source,
                LEGACY_SKIN_SOURCE_BEATMAP,
            )
            self.assertEqual(
                renderer.assets.scorebar_colour.source,
                LEGACY_SKIN_SOURCE_BEATMAP,
            )
            self.assertEqual(renderer.assets.scorebar_colour_frames, [])
            self.assertFalse(renderer.assets.scorebar_marker.present)

            os.remove(beatmap / "scorebar-bg.png")
            os.remove(beatmap / "scorebar-colour.png")
            self._save_surface(
                selected / "scorebar-bg.png",
                (40, 20),
                (255, 255, 255, 255),
            )
            for name in ("scorebar-colour-0.png", "scorebar-colour-1.png"):
                self._save_surface(
                    selected / name,
                    (30, 8),
                    (255, 255, 255, 255),
                )
            _, renderer = self._load_health_renderer(
                selected,
                960,
                540,
                core.COMPAT_STABLE_REPLAY,
                beatmap_root=beatmap,
            )
            self.assertEqual(
                renderer.assets.scorebar_bg.source,
                LEGACY_SKIN_SOURCE_SELECTED,
            )
            self.assertEqual(
                renderer.assets.scorebar_colour.source,
                LEGACY_SKIN_SOURCE_SELECTED,
            )
            self.assertEqual(len(renderer.assets.scorebar_colour_frames), 2)
            self.assertEqual(
                renderer.assets.scorebar_marker.source,
                LEGACY_SKIN_SOURCE_SELECTED,
            )
            calls = self._trace_health_draw(renderer, 0.5)
            self.assertEqual([call[0] for call in calls], ["image", "image"])
            self.assertEqual(
                calls[1][4]["clip_w"],
                round(renderer.assets.scorebar_colour.width * 0.5),
            )

    def test_note_body_style_defaults_follow_skin_version(self):
        cases = (
            ("1.0", core.LEGACY_NOTE_BODY_STRETCH),
            ("2.4", core.LEGACY_NOTE_BODY_STRETCH),
            ("2.5", core.LEGACY_NOTE_BODY_REPEAT_BOTTOM),
            ("2.7", core.LEGACY_NOTE_BODY_REPEAT_BOTTOM),
            ("latest", core.LEGACY_NOTE_BODY_REPEAT_BOTTOM),
        )
        for version, expected in cases:
            with self.subTest(version=version):
                config = core.SkinConfig(
                    "",
                    {"Version": version},
                    4,
                )
                self.assertEqual(config.note_body_style, [expected] * 4)

    def test_note_body_style_accepts_named_and_undefined_numeric_values(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "latest",
                "NoteBodyStyle0": "0",
                "NoteBodyStyle1": "1",
                "NoteBodyStyle2": "2",
                "NoteBodyStyle3": "3",
                "NoteBodyStyle4": "RepeatTopAndBottom",
                "NoteBodyStyle5": "Repeat",
                "NoteBodyStyle6": "5",
            },
            7,
        )
        self.assertEqual(config.note_body_style, [
            core.LEGACY_NOTE_BODY_STRETCH,
            core.LEGACY_NOTE_BODY_REPEAT,
            core.LEGACY_NOTE_BODY_REPEAT_TOP,
            core.LEGACY_NOTE_BODY_REPEAT_BOTTOM,
            core.LEGACY_NOTE_BODY_REPEAT_TOP_AND_BOTTOM,
            core.LEGACY_NOTE_BODY_REPEAT_BOTTOM,
            5,
        ])

    def test_column_right_compresses_stage_to_stable_screen_boundary(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "ColumnStart": "700",
                "ColumnRight": "19",
                "ColumnWidth": "100,100,100,100",
                "ColumnSpacing": "30,30,30",
            },
            4,
        )
        layout = core.resolve_stage_layout(1920, 1080, 4, config)
        logical_width = 1920 / (1080 / 480)
        self.assertLess(layout.scale_factor, 1.0)
        self.assertLessEqual(
            layout.lane_rects[-1][2],
            round((int(np.ceil(logical_width)) - config.column_right) * 1080 / 480),
        )

    def test_stage_compression_uses_ceiled_stable_logical_width(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "ColumnStart": "850",
                "ColumnRight": "19",
                "ColumnWidth": "100,100,100,100",
                "ColumnSpacing": "30,30,30",
            },
            4,
        )
        layout = core.resolve_stage_layout(1000, 539, 4, config)
        self.assertAlmostEqual(layout.scale_factor, 22.0 / 490.0)

    def test_test4_extreme_widths_resolve_like_stable(self):
        config = core.load_skin_config(str(SKINS / "test4"), 4)
        self.assertEqual(config.column_width, [100.0] * 4)
        layout = core.resolve_stage_layout(1920, 1080, 4, config)
        self.assertEqual(layout.scale_factor, 1.0)
        self.assertEqual(layout.lane_rects[0][0], 338)
        self.assertEqual(layout.lane_rects[-1][2], 1440)

    def test_test4_score_position_uses_absolute_coordinate_in_both_scrolls(self):
        config = core.load_skin_config(str(SKINS / "test4"), 4)
        self.assertEqual(config.score_position, 230.0)
        # test4's 220 HitPosition is clamped independently to Stable's 240.
        self.assertEqual(config.hit_position, 240.0)
        self.assertEqual(
            core._legacy_judgement_position(config, 480, False),
            230.0,
        )
        self.assertEqual(
            core._legacy_judgement_position(config, 480, True),
            250.0,
        )
        self.assertEqual(
            core._legacy_judgement_position(config, 960, False),
            460.0,
        )
        self.assertEqual(
            core._legacy_judgement_position(config, 960, True),
            500.0,
        )

    def test_split_stage_layout_includes_scaled_stage_separation(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "ColumnStart": "136",
                "ColumnWidth": "30,30,30,30",
                "SplitStages": "1",
                "StageSeparation": "40",
            },
            4,
        )
        layout = core.resolve_stage_layout(960, 540, 4, config)
        self.assertEqual(layout.column_stage_indices, (0, 0, 1, 1))
        self.assertEqual(len(layout.stage_ranges), 2)
        self.assertAlmostEqual(
            layout.stage_ranges[1][0] - layout.stage_ranges[0][1],
            40 * 540 / 480,
        )

    def test_split_stage_cross_spacing_leads_the_second_stage(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "ColumnStart": "136",
                "ColumnWidth": "30,30,30,30",
                "ColumnSpacing": "1,7,2",
                "SplitStages": "1",
                "StageSeparation": "40",
            },
            4,
        )
        layout = core.resolve_stage_layout(640, 480, 4, config)
        self.assertEqual(layout.stage_ranges, ((136.0, 197.0), (237.0, 306.0)))
        self.assertEqual(layout.lane_rects[2][0], 244)
        self.assertEqual(
            layout.lane_rects[2][0] - layout.stage_ranges[1][0],
            7.0,
        )

    def test_split_stages_scale_stage_hint_per_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_surface(root / "hint.png", (10, 2), (255, 255, 255, 255))
            core.configure_runtime(width=640, height=480)
            config = core.SkinConfig(
                str(root),
                {
                    "Version": "2.7",
                    "ColumnStart": "136",
                    "ColumnWidth": "30,30,30,30,30",
                    "SplitStages": "1",
                    "StageSeparation": "40",
                    "StageHint": "hint",
                },
                5,
            )
            layout = core.resolve_stage_layout(640, 480, 5, config)
            assets = core.SkinAssets(
                config,
                layout.lane_rects,
                False,
                layout.stage_ranges,
            )
            self.assertEqual([image[2] for image in assets.stage_hints], [90, 60])
            self.assertEqual(
                [image[3] for image in assets.stage_hints],
                [26, 17],
            )

    def test_barline_height_is_not_multiplied_by_its_default(self):
        core.configure_runtime(width=1366, height=768)
        renderer = object.__new__(core.ManiaRenderer)
        renderer.skin_config = core.SkinConfig(
            "", {"Version": "2.7", "BarlineHeight": "3"}, 4,
        )
        renderer.stage_ranges = ((10, 100),)
        calls = []
        renderer._blend_rect = lambda *args: calls.append(args)
        renderer.draw_barlines([20])
        self.assertEqual(calls[0][0:4], (10, 18.5, 100, 21.5))

    def test_transparent_hold_tail_remains_an_explicit_resource(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_minimal_skin(root, [
                "NoteImage0: note",
                "NoteImage0H: head",
                "NoteImage0L: body",
                "NoteImage0T: tail",
            ])
            self._save_surface(root / "note.png", (8, 8), (0, 0, 255, 255))
            self._save_surface(root / "head.png", (8, 8), (255, 0, 0, 255))
            self._save_surface(root / "body.png", (8, 8), (255, 255, 255, 255))
            self._save_surface(root / "tail.png", (2, 2), (0, 0, 0, 0))

            core.configure_runtime(width=960, height=540)
            config = core.load_skin_config(str(root), 4)
            lane_rects, _ = core.lane_geometry(960, 540, 4, config)
            assets = core.SkinAssets(config, lane_rects)

            self.assertIsNotNone(assets.tail[0])
            self.assertEqual(float(assets.tail[0][1].max()), 0.0)

    def test_missing_hold_tail_falls_back_to_head_then_note(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_minimal_skin(root, [
                "NoteImage0: note",
                "NoteImage0H: head",
                "NoteImage0L: body",
                "NoteImage0T: missing-tail",
            ])
            self._save_surface(root / "note.png", (8, 8), (0, 0, 255, 255))
            self._save_surface(root / "head.png", (8, 8), (255, 0, 0, 255))
            self._save_surface(root / "body.png", (8, 8), (255, 255, 255, 255))

            core.configure_runtime(width=960, height=540)
            config = core.load_skin_config(str(root), 4)
            lane_rects, _ = core.lane_geometry(960, 540, 4, config)
            lane_width = lane_rects[0][2] - lane_rects[0][0]
            note_height_width = config.width_for_note_height_scale * core.SCALE

            assets = core.SkinAssets(config, lane_rects)
            loader = object.__new__(core.SkinAssets)
            loader.config = config
            expected_head = loader._load_scaled(
                config.note_head[0],
                lane_width,
                height_scale_w=note_height_width,
                allow_transparent=True,
                flip_y=True,
            )
            np.testing.assert_array_equal(assets.tail[0][0], expected_head[0])

            os.remove(root / "head.png")
            assets = core.SkinAssets(config, lane_rects)
            expected_note = loader._load_scaled(
                config.note[0],
                lane_width,
                height_scale_w=note_height_width,
                allow_transparent=True,
                flip_y=True,
            )
            np.testing.assert_array_equal(assets.tail[0][0], expected_note[0])

    def test_identical_hold_body_transforms_share_pixel_exact_arrays(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_minimal_skin(root, [
                "NoteImage0L: body",
                "NoteImage1L: body",
                "NoteImage2L: body",
                "NoteImage3L: body",
            ])
            surface = pygame.Surface((8, 32), pygame.SRCALPHA)
            for y in range(32):
                surface.fill((y * 7, 255 - y * 5, y * 3, 255), (0, y, 8, 1))
            pygame.image.save(surface, str(root / "body.png"))

            core.configure_runtime(width=960, height=540)
            config = core.load_skin_config(str(root), 4)
            lane_rects = [
                (index * 30, 0, (index + 1) * 30, 480)
                for index in range(4)
            ]
            assets = core.SkinAssets(config, lane_rects)

            first = assets.body[0]
            self.assertTrue(all(image is first for image in assets.body[1:]))

            loader = object.__new__(core.SkinAssets)
            loader.config = config
            expected = loader._load_body(
                config.note_body[0],
                30,
                config.width_for_note_height_scale * core.SCALE,
            )
            np.testing.assert_array_equal(first[0], expected[0])
            np.testing.assert_array_equal(first[1], expected[1])
            self.assertEqual(first[2:], expected[2:])

    def test_repeat_bottom_anchors_opaque_body_rows_at_the_tail_join(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer._body_row_cache = OrderedDict()
        captured = []
        renderer._blit_premul = (
            lambda rgb, alpha, x, y, w, h:
            captured.append(alpha[:, 0, 0].copy())
        )
        body = self._skin_image(1, 4, (0.0, 0.0, 1.0, 1.0))

        renderer._blit_skin_body(
            body,
            0,
            0,
            2,
            core.LEGACY_NOTE_BODY_REPEAT_BOTTOM,
        )
        np.testing.assert_array_equal(captured.pop(), (1.0, 1.0))

        for undefined_style in (core.LEGACY_NOTE_BODY_REPEAT, 5, -1):
            renderer._blit_skin_body(body, 0, 0, 2, undefined_style)
            np.testing.assert_array_equal(captured.pop(), (1.0, 1.0))

        renderer._blit_skin_body(
            body,
            0,
            0,
            2,
            core.LEGACY_NOTE_BODY_REPEAT_TOP,
        )
        np.testing.assert_array_equal(captured.pop(), (0.0, 0.0))

    def test_repeat_body_can_expose_the_tail_facing_texture_endpoint(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer._body_row_cache = OrderedDict()
        captured = []
        renderer._blit_premul = (
            lambda rgb, alpha, x, y, w, h:
            captured.append(alpha[:, 0, 0].copy())
        )
        body = self._skin_image(1, 4, (0.25, 0.5, 0.75, 1.0))

        renderer._blit_skin_body(
            body,
            0,
            0,
            2,
            core.LEGACY_NOTE_BODY_REPEAT_BOTTOM,
            repeat_anchor="top",
        )
        np.testing.assert_array_equal(captured.pop(), (0.25, 0.5))

        renderer._blit_skin_body(
            body,
            0,
            0,
            2,
            core.LEGACY_NOTE_BODY_REPEAT_TOP,
            repeat_anchor="bottom",
        )
        np.testing.assert_array_equal(captured.pop(), (0.75, 1.0))

    def test_transparent_tail_anchors_body_texture_to_the_tail_edge(self):
        for direction, head_y, tail_y, expected_anchor in (
            ("down", 100.0, 40.0, "top"),
            ("up", 40.0, 100.0, "bottom"),
        ):
            with self.subTest(direction=direction):
                renderer = object.__new__(core.ManiaRenderer)
                renderer.geometry = SimpleNamespace(
                    upside_down=direction == "up",
                )
                renderer.stage_y1 = 0.0
                renderer.stage_y2 = 200.0
                renderer.skin_config = SimpleNamespace(
                    note_body_style=[core.LEGACY_NOTE_BODY_REPEAT_BOTTOM],
                )
                head = self._skin_image(20, 20)
                body = self._skin_image(20, 4)
                tail = self._skin_image(20, 10, np.zeros(10))
                renderer.assets = SimpleNamespace(
                    head=[head],
                    note=[None],
                    body=[body],
                    tail=[tail],
                )
                body_calls = []
                image_calls = []
                renderer._blit_skin_body = (
                    lambda image, x, y, height, style, **kwargs:
                    body_calls.append((image, x, y, height, style, kwargs))
                )
                renderer._blit_skin_image = (
                    lambda image, x, y, **kwargs:
                    image_calls.append((image, x, y, kwargs))
                )

                renderer.draw_skin_hold(0, 10, 30, head_y, tail_y)

                self.assertEqual(len(body_calls), 1)
                self.assertEqual(body_calls[0][5]["repeat_anchor"], expected_anchor)
                self.assertFalse(any(call[0] is tail for call in image_calls))

    def test_skin_hold_body_joins_the_body_facing_tail_edge(self):
        for direction, head_y, tail_y, expected in (
            ("down", 100.0, 40.0, (40, 50)),
            ("up", 40.0, 100.0, (50, 50)),
        ):
            with self.subTest(direction=direction):
                renderer = object.__new__(core.ManiaRenderer)
                renderer.geometry = SimpleNamespace(
                    upside_down=direction == "up",
                )
                renderer.stage_y1 = 0.0
                renderer.stage_y2 = 200.0
                renderer.skin_config = SimpleNamespace(
                    note_body_style=[core.LEGACY_NOTE_BODY_REPEAT_BOTTOM],
                )
                head = self._skin_image(20, 20)
                body = self._skin_image(20, 4)
                tail = self._skin_image(20, 10)
                renderer.assets = SimpleNamespace(
                    head=[head],
                    note=[None],
                    body=[body],
                    tail=[tail],
                )
                body_calls = []
                image_calls = []
                renderer._blit_skin_body = (
                    lambda image, x, y, height, style, **kwargs:
                    body_calls.append((image, x, y, height, style, kwargs))
                )
                renderer._blit_skin_image = (
                    lambda image, x, y, **kwargs:
                    image_calls.append((image, x, y, kwargs))
                )

                renderer.draw_skin_hold(
                    0,
                    10,
                    30,
                    head_y,
                    tail_y,
                )

                self.assertEqual(len(body_calls), 1)
                body_call = body_calls[0]
                self.assertIs(body_call[0], body)
                self.assertEqual(body_call[2:4], expected)
                self.assertEqual(body_call[5]["source_offset"], 0.0)
                self.assertEqual(body_call[5]["full_height"], 50.0)
                self.assertIsNone(body_call[5]["repeat_anchor"])
                tail_call = next(call for call in image_calls if call[0] is tail)
                expected_tail_y = 30 if direction == "down" else 100
                self.assertEqual(tail_call[2], expected_tail_y)

    def test_skin_hold_tail_rounding_is_not_filled_by_the_body(self):
        def image_from_alpha(alpha_mask, colour):
            alpha = np.asarray(alpha_mask, dtype=np.float32)[:, :, None]
            rgb = np.asarray(colour, dtype=np.float32).reshape(1, 1, 3)
            height, width = alpha.shape[:2]
            return rgb * alpha, alpha, width, height

        down_tail_alpha = np.asarray([
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0],
            [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
            [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ], dtype=np.float32)

        for direction, tail_alpha, head_y, tail_y, corner, tail_join, body_join in (
            ("down", down_tail_alpha, 70.0, 30.0, (25, 10), (28, 16), (29, 16)),
            ("up", np.flipud(down_tail_alpha), 10.0, 50.0, (54, 10), (51, 16), (50, 16)),
        ):
            with self.subTest(direction=direction):
                renderer = object.__new__(core.ManiaRenderer)
                renderer.width = 40
                renderer.height = 80
                renderer.frame = np.zeros((80, 40, 3), dtype=np.uint8)
                renderer._image_transform_cache = OrderedDict()
                renderer._body_row_cache = OrderedDict()
                renderer.geometry = SimpleNamespace(
                    upside_down=direction == "up",
                )
                renderer.stage_y1 = 0.0
                renderer.stage_y2 = 80.0
                renderer.skin_config = SimpleNamespace(
                    note_body_style=[core.LEGACY_NOTE_BODY_STRETCH],
                )
                renderer.assets = SimpleNamespace(
                    head=[image_from_alpha(np.ones((8, 12)), (0, 255, 0))],
                    note=[None],
                    body=[image_from_alpha(np.ones((2, 12)), (0, 0, 255))],
                    tail=[image_from_alpha(tail_alpha, (255, 0, 0))],
                )

                renderer.draw_skin_hold(0, 10, 22, head_y, tail_y)

                np.testing.assert_array_equal(renderer.frame[corner], (0, 0, 0))
                np.testing.assert_array_equal(renderer.frame[tail_join], (255, 0, 0))
                np.testing.assert_array_equal(renderer.frame[body_join], (0, 0, 255))


if __name__ == "__main__":
    unittest.main()
