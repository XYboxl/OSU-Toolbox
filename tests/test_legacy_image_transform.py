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
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


def make_image(width, height, alpha=1.0):
    a = np.full((height, width, 1), float(alpha), dtype=np.float32)
    rgb = np.repeat(a, 3, axis=2) * 255.0
    return (rgb, a, width, height)


def make_blit_renderer(width=64, height=64):
    renderer = object.__new__(core.ManiaRenderer)
    renderer.width = width
    renderer.height = height
    renderer.frame = np.zeros((height, width, 3), dtype=np.uint8)
    renderer._image_transform_cache = OrderedDict()
    return renderer


class RotationBlitTests(unittest.TestCase):
    def test_rotation_zero_matches_default_path(self):
        img = make_image(8, 6)
        plain = make_blit_renderer()
        plain._blit_skin_image(img, 10, 12)
        rotated_zero = make_blit_renderer()
        rotated_zero._blit_skin_image(img, 10, 12, rotation=0.0)
        np.testing.assert_array_equal(plain.frame, rotated_zero.frame)

    def test_small_rotation_quantizes_to_zero(self):
        img = make_image(8, 6)
        plain = make_blit_renderer()
        plain._blit_skin_image(img, 10, 12)
        tiny = make_blit_renderer()
        tiny._blit_skin_image(img, 10, 12, rotation=0.2)
        np.testing.assert_array_equal(plain.frame, tiny.frame)

    def test_rotation_quantized_to_half_degree_shares_cache(self):
        img = make_image(8, 6)
        renderer = make_blit_renderer()
        renderer._blit_skin_image(img, 10, 12, rotation=0.3)
        entries = len(renderer._image_transform_cache)
        renderer._blit_skin_image(img, 10, 12, rotation=0.4)
        self.assertEqual(len(renderer._image_transform_cache), entries)
        renderer._blit_skin_image(img, 10, 12, rotation=1.4)
        self.assertEqual(len(renderer._image_transform_cache), entries + 1)

    def test_rotation_preserves_centre_block(self):
        size = 11
        a = np.zeros((size, size, 1), dtype=np.float32)
        a[4:7, 4:7, 0] = 1.0
        rgb = np.repeat(a, 3, axis=2) * 255.0
        img = (rgb, a, size, size)
        renderer = make_blit_renderer()
        renderer._blit_skin_image(img, 20, 20, rotation=45.0)
        cx = 20 + size // 2
        cy = 20 + size // 2
        neighbourhood = renderer.frame[cy - 2:cy + 3, cx - 2:cx + 3]
        self.assertGreater(int(neighbourhood.max()), 0)
        far = renderer.frame.copy()
        far[cy - 8:cy + 9, cx - 8:cx + 9] = 0
        self.assertEqual(int(far.max()), 0)

    def test_rotated_sprite_is_actually_tilted(self):
        img = make_image(20, 4)
        straight = make_blit_renderer()
        straight._blit_skin_image(img, 20, 30)
        tilted = make_blit_renderer()
        tilted._blit_skin_image(img, 20, 30, rotation=5.5)
        self.assertTrue((straight.frame != tilted.frame).any())

    def test_stable_mania_health_clip_rotates_upward_from_anchor(self):
        img = make_image(10, 6)
        renderer = make_blit_renderer()
        renderer._blit_stable_mania_health(
            img, 20.0, 30.0, clip_w=4,
        )
        ys, xs = np.where(renderer.frame[:, :, 0] > 0)
        scaled_clip = round(4 * core.LEGACY_MANIA_HEALTH_SPRITE_SCALE)
        scaled_height = round(6 * core.LEGACY_MANIA_HEALTH_SPRITE_SCALE)
        self.assertEqual((xs.min(), xs.max() + 1), (20, 20 + scaled_height))
        self.assertEqual((ys.min(), ys.max() + 1), (30 - scaled_clip, 30))


class MissRotationSeedTests(unittest.TestCase):
    def test_deterministic_and_bounded(self):
        first = core._legacy_miss_rotation_deg(1234.5, 2)
        second = core._legacy_miss_rotation_deg(1234.5, 2)
        self.assertEqual(first, second)
        self.assertLessEqual(
            abs(first), core.LEGACY_JUDGEMENT_MISS_ROTATION_DEG,
        )
        other_col = core._legacy_miss_rotation_deg(1234.5, 3)
        self.assertNotEqual(first, other_col)

    def test_distribution_covers_both_signs(self):
        values = [
            core._legacy_miss_rotation_deg(t * 37.0, t % 4)
            for t in range(1, 201)
        ]
        self.assertTrue(any(v > 1.0 for v in values))
        self.assertTrue(any(v < -1.0 for v in values))
        self.assertTrue(
            all(abs(v) <= core.LEGACY_JUDGEMENT_MISS_ROTATION_DEG for v in values),
        )

    def test_draw_hit_flash_rotates_only_miss(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.lane_rects = [(0, 0, 40, 540)]
        renderer.stage_cx = 20.0
        renderer.y_score = 100.0
        renderer.y_judge = 400.0
        result_img = make_image(30, 10)
        renderer.assets = SimpleNamespace(
            hit_result_for_age=lambda age, value: result_img,
            lighting_for_age=lambda col, is_hold, age: None,
        )
        calls = []

        def blit(image, x, y, **kwargs):
            calls.append(kwargs)

        renderer._blit_skin_image = blit
        renderer.draw_hit_flash(0, 1.0, False, True, 100.0, 0, 1234.5)
        expected = core._legacy_miss_rotation_deg(1234.5, 0)
        self.assertAlmostEqual(calls[0]["rotation"], expected, places=6)
        calls.clear()
        renderer.draw_hit_flash(0, 1.0, False, True, 100.0, 320, 1234.5)
        for kwargs in calls:
            self.assertEqual(kwargs.get("rotation", 0.0), 0.0)

    def test_separate_score_routes_results_to_the_hit_stage(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.stage_ranges = ((0.0, 40.0), (80.0, 140.0))
        renderer.column_stage_indices = (0, 0, 1, 1)
        renderer.skin_config = SimpleNamespace(separate_score=True)
        self.assertEqual(renderer.judgement_stage_indices(0), (0,))
        self.assertEqual(renderer.judgement_stage_indices(3), (1,))

    def test_combined_score_broadcasts_results_to_every_stage(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.stage_ranges = ((0.0, 40.0), (80.0, 140.0))
        renderer.column_stage_indices = (0, 0, 1, 1)
        renderer.skin_config = SimpleNamespace(separate_score=False)
        self.assertEqual(renderer.judgement_stage_indices(0), (0, 1))
        self.assertEqual(renderer.judgement_stage_indices(3), (0, 1))

    def test_draw_hit_flash_centres_result_on_each_requested_stage(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.lane_rects = [(0, 0, 40, 540)]
        renderer.stage_cx = 70.0
        renderer.stage_ranges = ((0.0, 40.0), (80.0, 140.0))
        renderer.y_score = 100.0
        renderer.y_judge = 400.0
        result_img = make_image(30, 10)
        renderer.assets = SimpleNamespace(
            hit_result_for_age=lambda age, value: result_img,
            lighting_for_age=lambda col, is_hold, age: None,
        )
        calls = []
        renderer._blit_skin_image = (
            lambda image, x, y, **kwargs: calls.append((x, y, kwargs))
        )
        renderer.draw_hit_flash(
            0, 1.0, False, True, 100.0, 320, 1234.5, (0, 1),
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0] - calls[0][0], 90)


class ExplosionScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def tearDown(self):
        core.configure_runtime(width=960, height=540)

    def test_explosion_scale_formula(self):
        old = SimpleNamespace(version=1.0)
        self.assertEqual(
            core.SkinAssets._legacy_explosion_scale(old, 60.0, 30.0), 1.0,
        )
        new = SimpleNamespace(version=2.7)
        self.assertAlmostEqual(
            core.SkinAssets._legacy_explosion_scale(new, 30.0, 45.0), 1.0,
        )
        self.assertAlmostEqual(
            core.SkinAssets._legacy_explosion_scale(new, 0.0, 45.0), 1.5,
        )
        self.assertAlmostEqual(
            core.SkinAssets._legacy_explosion_scale(new, 60.0, 30.0), 2.0,
        )

    def test_lighting_loads_native_size_times_screen_scale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "",
                    "[Mania]",
                    "Keys: 4",
                    "ColumnWidth: 30,30,30,30",
                    "",
                ]),
                encoding="utf-8",
            )
            surface = pygame.Surface((100, 50), pygame.SRCALPHA)
            surface.fill((255, 255, 255, 255))
            pygame.image.save(surface, str(root / "lightingN.png"))
            core.configure_runtime(width=960, height=540)
            config = core.load_skin_config(str(root), 4)
            self.assertIsNotNone(config)
            lane_rects, _ = core.lane_geometry(960, 540, 4, config)
            assets = core.SkinAssets(config, lane_rects)
            light = assets.light_n[0]
            self.assertIsNotNone(light)
            screen_scale = core._legacy_internal_screen_scale()
            self.assertEqual(light[2], max(1, round(100 * screen_scale)))
            self.assertEqual(light[3], max(1, round(50 * screen_scale)))

    def test_stage_compression_scales_lighting_and_fallback_note_height(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skin_ini = [
                "[General]",
                "Version: 2.7",
                "",
                "[Mania]",
                "Keys: 4",
                "ColumnStart: 0",
                "ColumnRight: 0",
                "ColumnWidth: 100,100,100,100",
                "LightingNWidth: 60,60,60,60",
                "LightingLWidth: 0,0,0,0",
                "NoteImage0: note",
            ]
            (root / "skin.ini").write_text(
                "\n".join([*skin_ini, "WidthForNoteHeightScale: 0"]),
                encoding="utf-8",
            )
            note = pygame.Surface((30, 60), pygame.SRCALPHA)
            note.fill((255, 255, 255, 255))
            pygame.image.save(note, str(root / "note.png"))
            lighting = pygame.Surface((100, 50), pygame.SRCALPHA)
            lighting.fill((255, 255, 255, 255))
            pygame.image.save(lighting, str(root / "lightingN.png"))
            pygame.image.save(lighting, str(root / "lightingL.png"))

            core.configure_runtime(width=200, height=480)
            config = core.load_skin_config(str(root), 4)
            layout = core.resolve_stage_layout(200, 480, 4, config)
            self.assertEqual(layout.scale_factor, 0.5)
            assets = core.SkinAssets(
                config,
                layout.lane_rects,
                stage_ranges=layout.stage_ranges,
                stage_scale=layout.scale_factor,
            )
            self.assertEqual((assets.note[0][2], assets.note[0][3]), (50, 100))
            self.assertEqual((assets.light_n[0][2], assets.light_n[0][3]), (62, 31))
            self.assertEqual((assets.light_l[0][2], assets.light_l[0][3]), (104, 52))

            (root / "skin.ini").write_text(
                "\n".join([*skin_ini, "WidthForNoteHeightScale: 40"]),
                encoding="utf-8",
            )
            explicit = core.load_skin_config(str(root), 4)
            explicit_assets = core.SkinAssets(
                explicit,
                layout.lane_rects,
                stage_ranges=layout.stage_ranges,
                stage_scale=layout.scale_factor,
            )
            self.assertEqual(
                (explicit_assets.note[0][2], explicit_assets.note[0][3]),
                (50, 80),
            )


if __name__ == "__main__":
    unittest.main()
