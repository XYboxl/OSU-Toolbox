import os
import sys
import tempfile
import unittest
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
    rgb = np.repeat(a, 3, axis=2)
    return (rgb, a, width, height)


class HealthBarAnimationDrawTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def setUp(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_LAZER_CLASSIC,
            width=960,
            height=540,
            scroll_direction="auto",
        )

    def tearDown(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
            width=960,
            height=540,
            scroll_direction="auto",
        )

    @staticmethod
    def _save_surface(path, size, colour):
        surface = pygame.Surface(size, pygame.SRCALPHA)
        surface.fill(colour)
        pygame.image.save(surface, str(path))

    def _new_style_renderer(self, temp_dir):
        root = Path(temp_dir)
        (root / "skin.ini").write_text(
            "\n".join([
                "[General]",
                "Version: 2.7",
                "",
                "[Mania]",
                "Keys: 4",
                "",
            ]),
            encoding="utf-8",
        )
        self._save_surface(root / "scorebar-bg.png", (30, 12), (255, 255, 255, 255))
        self._save_surface(root / "scorebar-colour.png", (20, 4), (255, 255, 255, 255))
        self._save_surface(root / "scorebar-marker.png", (6, 6), (255, 255, 255, 255))
        core.configure_runtime(width=960, height=540)
        config = core.load_skin_config(str(root), 4)
        self.assertIsNotNone(config)
        loader = object.__new__(core.SkinAssets)
        loader.config = config
        scale = core._legacy_internal_screen_scale()
        assets = SimpleNamespace(
            scorebar_bg=loader._load_image_resource(config.scorebar_bg, scale=scale),
            scorebar_colour=loader._load_image_resource(config.scorebar_colour, scale=scale),
            scorebar_marker=loader._load_image_resource(config.scorebar_marker, scale=scale),
            scorebar_ki=loader._load_image_resource(config.scorebar_ki, scale=scale),
            scorebar_kidanger=loader._load_image_resource(config.scorebar_kidanger, scale=scale),
            scorebar_kidanger2=loader._load_image_resource(config.scorebar_kidanger2, scale=scale),
        )
        renderer = object.__new__(core.ManiaRenderer)
        renderer.width = 960
        renderer.height = 540
        renderer.stage_x2 = 620.0
        renderer.assets = assets
        return renderer

    @staticmethod
    def _trace(renderer, *args, **kwargs):
        calls = []

        def blit(image, x, y, **kw):
            calls.append(("image", image, x, y, kw))

        def clipped(image, x, y, **kw):
            calls.append(("clipped", image, x, y, kw))

        def blend(*a, **kw):
            calls.append(("blend", a, kw))

        renderer._blit_skin_image = blit
        renderer._blit_skin_image_clipped = clipped
        renderer._blend_rect = blend
        renderer.draw_health_bar(*args, **kwargs)
        return calls

    def test_stable_mania_rotates_and_anchors_health_beside_stage(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = self._new_style_renderer(temp_dir)
            bg = renderer.assets.scorebar_bg.image
            colour = renderer.assets.scorebar_colour.image
            calls = self._trace(renderer, 0.3, 0.8)
            marker = renderer.assets.scorebar_marker.image
            self.assertEqual([call[0] for call in calls], ["image", "image"])
            self.assertFalse(any(call[1] is marker for call in calls))

            bg_call, fill_call = calls
            scale_480 = 540.0 / 480.0
            bg_anchor = (
                renderer.stage_x2
                + core.LEGACY_MANIA_HEALTH_BG_OFFSET_X_480 * scale_480,
                540.0,
            )
            self.assertEqual(
                bg_call[2:4],
                core.ManiaRenderer._stable_mania_health_blit_position(
                    *bg_anchor, bg[2], bg[3],
                ),
            )
            fill_w = round(colour[2] * 0.8)
            fill_anchor = (
                renderer.stage_x2
                + core.LEGACY_MANIA_HEALTH_NEW_OFFSET_X_480 * scale_480,
                core.LEGACY_MANIA_HEALTH_NEW_OFFSET_Y_480 * scale_480,
            )
            self.assertEqual(
                fill_call[2:4],
                core.ManiaRenderer._stable_mania_health_blit_position(
                    *fill_anchor, fill_w, colour[3],
                ),
            )
            self.assertEqual(fill_call[4]["clip_w"], fill_w)
            for call in calls:
                self.assertEqual(
                    call[4]["rotation"],
                    core.LEGACY_MANIA_HEALTH_ROTATION_DEG,
                )
                self.assertEqual(
                    call[4]["scale"],
                    core.LEGACY_MANIA_HEALTH_SPRITE_SCALE,
                )

    def test_lazer_new_style_health_keeps_mode_specific_tint(self):
        core.configure_runtime(compatibility_mode=core.COMPAT_LAZER_CLASSIC)
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = self._new_style_renderer(temp_dir)
            calls = self._trace(renderer, 0.3, 0.8)
            clipped = next(call for call in calls if call[0] == "clipped")
            self.assertEqual(
                clipped[4]["tint"],
                core.ManiaRenderer._legacy_health_colour(0.3),
            )
            marker = renderer.assets.scorebar_marker.image
            marker_call = next(
                call for call in calls
                if call[0] == "image" and call[1] is marker
            )
            self.assertFalse(marker_call[4]["additive"])

    def test_health_flash_ghost_after_marker_without_tint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = self._new_style_renderer(temp_dir)
            marker = renderer.assets.scorebar_marker.image
            calls = self._trace(
                renderer, 0.8, 0.8, 1.0, 60.0, True,
            )
            marker_calls = [
                call for call in calls
                if call[0] == "image" and call[1] is marker
            ]
            self.assertEqual(len(marker_calls), 2)
            main, ghost = marker_calls
            self.assertEqual(
                main[4]["tint"],
                core.ManiaRenderer._legacy_health_colour(0.8),
            )
            self.assertTrue(main[4]["additive"])
            self.assertIsNone(ghost[4].get("tint"))
            self.assertTrue(ghost[4]["additive"])
            expected_scale, expected_alpha = core._legacy_health_flash_state(60.0, True)
            self.assertAlmostEqual(ghost[4]["scale"], expected_scale, places=6)
            self.assertAlmostEqual(ghost[4]["alpha_scale"], expected_alpha, places=6)

    def test_stable_mania_hides_marker_and_ignores_flash(self):
        core.configure_runtime(
            compatibility_mode=core.COMPAT_STABLE_REPLAY,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = self._new_style_renderer(temp_dir)
            marker = renderer.assets.scorebar_marker.image
            calls = self._trace(
                renderer, 0.1, 0.1, 1.0, 60.0, False,
            )
            marker_calls = [
                call for call in calls
                if call[0] == "image" and call[1] is marker
            ]
            self.assertEqual(marker_calls, [])

    def test_health_default_args_keep_legacy_call_sequence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = self._new_style_renderer(temp_dir)
            calls = self._trace(renderer, 0.5)
            self.assertEqual(
                [call[0] for call in calls],
                ["image", "clipped", "image"],
            )

    def test_marker_scale_scales_around_centre(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            renderer = self._new_style_renderer(temp_dir)
            colour = renderer.assets.scorebar_colour.image
            marker = renderer.assets.scorebar_marker.image
            calls = self._trace(renderer, 1.0, 1.0, 0.8)
            marker_call = next(
                call for call in calls
                if call[0] == "image" and call[1] is marker
            )
            fill_x = round(7.5 * 540 / 480.0)
            fill_y = round(7.8 * 540 / 480.0)
            fill_w = round(colour[2] * 1.0)
            expected_x = round(fill_x + fill_w - (marker[2] * 0.8) / 2)
            expected_y = round(fill_y + colour[3] / 2.0 - (marker[3] * 0.8) / 2)
            self.assertEqual(marker_call[2:4], (expected_x, expected_y))
            self.assertAlmostEqual(marker_call[4]["scale"], 0.8, places=6)


class ComboAnimationDrawTests(unittest.TestCase):
    def _combo_renderer(self):
        renderer = object.__new__(core.ManiaRenderer)
        glyphs = {str(d): make_image(4, 8) for d in range(10)}
        renderer.assets = SimpleNamespace(combo_font=glyphs)
        renderer.skin_config = SimpleNamespace(
            combo_overlap=0.0,
            colour_break=(1.0, 0.0, 0.0, 1.0),
            colour_hold=(1.0, 191.0 / 255.0, 51.0 / 255.0, 1.0),
        )
        renderer.stage_cx = 100.0
        renderer.y_combo = 60.0
        calls = []

        def record(glyphs_arg, text, x, y, overlap, **kwargs):
            calls.append((text, x, y, kwargs))

        renderer._blit_skin_text = record
        return renderer, calls

    def test_combo_ghost_uses_break_colour_additive(self):
        renderer, calls = self._combo_renderer()
        renderer.draw_combo_display(
            5, pop_age_ms=150.0, ghost_value=8, ghost_age_ms=100.0,
        )
        self.assertEqual(len(calls), 2)
        ghost_text, _, _, ghost_kwargs = calls[0]
        self.assertEqual(ghost_text, "8")
        self.assertTrue(ghost_kwargs["additive"])
        self.assertEqual(ghost_kwargs["tint"], (1.0, 0.0, 0.0, 1.0))
        expected_scale, expected_alpha = core._legacy_combo_ghost_state(100.0)
        self.assertAlmostEqual(ghost_kwargs["scale"], expected_scale, places=6)
        self.assertAlmostEqual(ghost_kwargs["alpha_scale"], expected_alpha, places=6)
        main_text, _, _, main_kwargs = calls[1]
        self.assertEqual(main_text, "5")
        self.assertIsNone(main_kwargs.get("tint"))

    def test_combo_pop_squashes_vertically_only(self):
        renderer, calls = self._combo_renderer()
        renderer.draw_combo_display(3, pop_age_ms=0.0)
        _, _, _, kwargs = calls[0]
        expected = round(core._legacy_combo_pop_scale_y(0.0) * 128.0) / 128.0
        self.assertAlmostEqual(kwargs["scale_y"], expected, places=6)
        self.assertNotIn("scale", kwargs)

    def test_combo_show_fade_only_from_hidden(self):
        renderer, calls = self._combo_renderer()
        renderer.draw_combo_display(1, show_age_ms=60.0)
        self.assertAlmostEqual(calls[0][3]["alpha_scale"], 0.5, places=6)
        calls.clear()
        renderer.draw_combo_display(2, show_age_ms=-1.0)
        self.assertAlmostEqual(calls[0][3]["alpha_scale"], 1.0, places=6)

    def test_combo_holding_tints_main_text(self):
        renderer, calls = self._combo_renderer()
        renderer.draw_combo_display(4, holding=True)
        self.assertEqual(calls[0][3]["tint"], renderer.skin_config.colour_hold)

    def test_show_age_field_set_only_when_appearing(self):
        anim = core.LegacyHudAnimator(
            key_count=4, start_ms=0.0, first_hit_ms=0.0, last_hit_ms=10000.0,
        )
        event = SimpleNamespace(time=0.0, col=0, value=320, is_hold=False)
        first = anim.advance(0.0, 16.7, 1, 0, 100.0, 1.0, [event], 0)
        self.assertEqual(first["combo_show_age_ms"], 0.0)
        second = anim.advance(200.0, 16.7, 2, 0, 100.0, 1.0, [], 0)
        self.assertEqual(second["combo_show_age_ms"], -1.0)
        self.assertEqual(second["combo_pop_age_ms"], 0.0)


class HoldLightDrawTests(unittest.TestCase):
    def _renderer(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.lane_rects = [(0, 0, 40, 540), (40, 0, 80, 540)]
        renderer.y_judge = 400.0
        renderer.geometry = SimpleNamespace(upside_down=False)
        img = make_image(20, 20)
        ages = []

        def hold_light_for_age(col, age_ms):
            ages.append((col, age_ms))
            return img

        renderer.assets = SimpleNamespace(hold_light_for_age=hold_light_for_age)
        calls = []

        def blit(image, x, y, **kwargs):
            calls.append((image, x, y, kwargs))

        renderer._blit_skin_image = blit
        return renderer, calls, ages

    def test_holding_column_draws_additive_light(self):
        renderer, calls, _ = self._renderer()
        renderer.draw_hold_lights((("hold", 40.0), None))
        self.assertEqual(len(calls), 1)
        _, _, _, kwargs = calls[0]
        self.assertTrue(kwargs["additive"])
        self.assertAlmostEqual(kwargs["alpha_scale"], 0.5, places=6)

    def test_release_fades_out_then_stops(self):
        renderer, calls, _ = self._renderer()
        renderer.draw_hold_lights((("release", 60.0), None))
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0][3]["alpha_scale"], 0.5, places=6)
        calls.clear()
        renderer.draw_hold_lights((("release", 130.0), None))
        self.assertEqual(calls, [])

    def test_hold_light_loops_frames(self):
        loader = object.__new__(core.SkinAssets)
        frames = [make_image(4, 4), make_image(4, 4), make_image(4, 4)]
        loader.light_l = [frames[0]]
        loader.light_l_frames = [frames]
        loader.light_l_fps = [30.0]
        first = loader.hold_light_for_age(0, 0.0)
        wrapped = loader.hold_light_for_age(0, 100.0)
        self.assertIs(first, frames[0])
        self.assertIs(wrapped, frames[0])
        self.assertIs(loader.hold_light_for_age(0, 33.4), frames[1])


class StageLightDrawTests(unittest.TestCase):
    def test_animation_restarts_from_key_press_age(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.lane_rects = [(0, 0, 40, 540)]
        renderer.y_light = 400.0
        renderer.geometry = SimpleNamespace(upside_down=False)
        image = make_image(20, 20)
        ages = []

        def stage_light_for_age(col, age_ms):
            ages.append((col, age_ms))
            return image

        renderer.assets = SimpleNamespace(
            stage_light_for_age=stage_light_for_age,
        )
        renderer._blit_skin_image = lambda *args, **kwargs: None

        renderer.draw_stage_lights(
            ((1.0, 1.0, 37.0),),
            animation_time_ms=9000.0,
        )

        self.assertEqual(ages, [(0, 37.0)])


if __name__ == "__main__":
    unittest.main()
