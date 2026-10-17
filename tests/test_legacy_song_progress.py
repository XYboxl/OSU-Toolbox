import math
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


def make_glyph(width=10, height=20, visible=True):
    alpha = np.ones((height, width, 1), dtype=np.float32)
    if not visible:
        alpha.fill(0.0)
    rgb = np.repeat(alpha, 3, axis=2) * 255.0
    return rgb, alpha, width, height


def make_skin_renderer(width=1920, height=1080, version=2.7):
    renderer = object.__new__(core.ManiaRenderer)
    renderer.width = width
    renderer.height = height
    renderer.assets = SimpleNamespace(
        score_font={ch: make_glyph() for ch in "0123456789.%"},
    )
    renderer.skin_config = SimpleNamespace(score_overlap=0.0, version=version)
    renderer._init_song_progress_layout()
    return renderer


class SongProgressSpriteTests(unittest.TestCase):
    def setUp(self):
        core.configure_runtime(
            width=1920,
            height=1080,
            show_song_progress=core.SONG_PROGRESS_PIE,
        )

    def tearDown(self):
        core.configure_runtime(
            width=960,
            height=540,
            show_song_progress=core.SONG_PROGRESS_PIE,
        )

    def test_pie_sprite_alpha_area_matches_signed_fraction(self):
        renderer = make_skin_renderer()
        full = renderer._song_progress_sprite(1.0)
        full_area = float(full[1].sum()) / core.LEGACY_SONG_PROGRESS_COLOUR[3]
        for progress in (0.25, 0.5, 0.75, -0.25, -0.5, -0.75):
            with self.subTest(progress=progress):
                sprite = renderer._song_progress_sprite(progress)
                colour = (
                    core.LEGACY_SONG_PROGRESS_INTRO_COLOUR
                    if progress < 0.0
                    else core.LEGACY_SONG_PROGRESS_COLOUR
                )
                covered = float(sprite[1].sum()) / colour[3]
                self.assertLess(
                    abs(covered - full_area * abs(progress)) / full_area,
                    0.035,
                )

    def test_signed_progress_uses_opposite_halves(self):
        renderer = make_skin_renderer()
        positive = renderer._song_progress_sprite(0.5)[1][:, :, 0]
        negative = renderer._song_progress_sprite(-0.5)[1][:, :, 0]
        mid = positive.shape[1] // 2
        self.assertGreater(positive[:, mid:].sum(), positive[:, :mid].sum() * 4)
        self.assertGreater(negative[:, :mid].sum(), negative[:, mid:].sum() * 4)

    def test_arc_endpoint_is_continuous_and_exact(self):
        first = core.ManiaRenderer._song_progress_arc_points(
            0.5001, 10.0, (0.0, 0.0),
        )
        second = core.ManiaRenderer._song_progress_arc_points(
            0.5002, 10.0, (0.0, 0.0),
        )
        self.assertNotEqual(first[-1], second[-1])
        expected_angle = -math.pi / 2.0 + math.tau * 0.5001
        self.assertAlmostEqual(first[-1][0], math.cos(expected_angle) * 10.0)
        self.assertAlmostEqual(first[-1][1], math.sin(expected_angle) * 10.0)

    def test_progress_below_minus_one_remains_visually_full(self):
        renderer = make_skin_renderer()
        minus_one = renderer._song_progress_sprite(-1.0)[1]
        minus_two = renderer._song_progress_sprite(-2.0)[1]
        np.testing.assert_allclose(minus_two, minus_one)

    def test_legacy_positive_intro_argument_is_accepted(self):
        renderer = make_skin_renderer()
        legacy = renderer._song_progress_sprite(0.25, True)
        signed = renderer._song_progress_sprite(-0.25)
        np.testing.assert_allclose(legacy[1], signed[1])

    def test_generated_ring_is_static_and_reused(self):
        renderer = make_skin_renderer()
        first = renderer._song_progress_ring()
        second = renderer._song_progress_ring()
        self.assertIs(first, second)
        self.assertEqual(
            first[2],
            round(core.LEGACY_SONG_PROGRESS_SIZE_768 * core._legacy_internal_screen_scale()),
        )


class SkinTextLayoutTests(unittest.TestCase):
    def setUp(self):
        core.configure_runtime(width=1920, height=1080)

    def tearDown(self):
        core.configure_runtime(width=960, height=540)

    def test_height_comes_from_first_loaded_glyph(self):
        renderer = make_skin_renderer()
        glyphs = {
            "1": make_glyph(height=12),
            "2": make_glyph(height=40),
        }
        _, height = renderer._measure_skin_text(glyphs, "12", 0.0)
        self.assertEqual(height, 12.0)

    def test_only_fixed_width_text_has_trailing_overlap(self):
        renderer = make_skin_renderer()
        glyphs = {"1": make_glyph(), "2": make_glyph(), "5": make_glyph()}
        fixed, _ = renderer._measure_skin_text(
            glyphs, "12", 3.0, fixed_width=True,
        )
        normal, _ = renderer._measure_skin_text(
            glyphs, "12", 3.0, fixed_width=False,
        )
        self.assertEqual(fixed, 14.0)
        self.assertEqual(normal, 17.0)

    def test_transparent_canvas_contributes_to_cached_layout(self):
        renderer = make_skin_renderer()
        renderer.assets.score_font["%"] = make_glyph(
            width=300, height=20, visible=False,
        )
        renderer._init_song_progress_layout()
        wide_center = renderer._song_progress_pie_center

        renderer.assets.score_font["%"] = make_glyph(
            width=20, height=20, visible=False,
        )
        self.assertEqual(renderer._song_progress_pie_center, wide_center)
        renderer._init_song_progress_layout()
        self.assertGreater(renderer._song_progress_pie_center[0], wide_center[0])

    def test_pie_layout_uses_stable_initial_accuracy_canvas(self):
        renderer = make_skin_renderer()
        overlap = renderer.skin_config.score_overlap * core._legacy_internal_screen_scale()
        acc_w, acc_h = renderer._measure_skin_text(
            renderer.assets.score_font,
            "100.0%",
            overlap,
            scale=core.LEGACY_ACCURACY_COUNTER_SCALE,
            fixed_width=True,
        )
        _, score_h = renderer._measure_skin_text(
            renderer.assets.score_font,
            "00000000",
            overlap,
            scale=core.LEGACY_SCORE_COUNTER_SCALE,
            fixed_width=True,
        )
        scale = renderer.height / 480.0
        self.assertEqual(
            renderer._song_progress_pie_center,
            (
                renderer.width - 6.0 * scale - acc_w - 24.0 * scale,
                score_h + 3.0 * scale + acc_h / 2.0,
            ),
        )

    def test_version_one_skin_has_no_accuracy_vertical_gap(self):
        renderer = make_skin_renderer(version=1.0)
        overlap = 0.0
        _, acc_h = renderer._measure_skin_text(
            renderer.assets.score_font,
            "100.0%",
            overlap,
            scale=core.LEGACY_ACCURACY_COUNTER_SCALE,
            fixed_width=True,
        )
        _, score_h = renderer._measure_skin_text(
            renderer.assets.score_font,
            "00000000",
            overlap,
            scale=core.LEGACY_SCORE_COUNTER_SCALE,
            fixed_width=True,
        )
        self.assertEqual(renderer._song_progress_pie_center[1], score_h + acc_h / 2.0)

    def test_reference_skin_centres_match_stable_captures(self):
        expected = {
            "test1": (23.925, 87.48),
            "test2": (493.725, 90.1125),
            "test4": (1720.47, 90.99),
        }
        if not all((ROOT / "skins" / name).is_dir() for name in expected):
            self.skipTest("local Stable reference skins are unavailable")
        for skin_name, expected_centre in expected.items():
            with self.subTest(skin=skin_name):
                skin = core.load_skin_config(str(ROOT / "skins" / skin_name), 4)
                loader = object.__new__(core.SkinAssets)
                loader.config = skin
                renderer = object.__new__(core.ManiaRenderer)
                renderer.width = 1920
                renderer.height = 1080
                renderer.skin_config = skin
                renderer.assets = SimpleNamespace(
                    score_font=loader._load_font_glyphs(
                        skin.score_prefix, "0123456789,.%",
                    ),
                )
                renderer._init_song_progress_layout()
                self.assertAlmostEqual(
                    renderer._song_progress_pie_center[0], expected_centre[0],
                    places=6,
                )
                self.assertAlmostEqual(
                    renderer._song_progress_pie_center[1], expected_centre[1],
                    places=6,
                )


class SongProgressModeGeometryTests(unittest.TestCase):
    def tearDown(self):
        core.configure_runtime(
            width=960,
            height=540,
            show_song_progress=core.SONG_PROGRESS_PIE,
        )

    def _linear_calls(self, width, height, mode, progress=0.5):
        core.configure_runtime(
            width=width,
            height=height,
            show_song_progress=mode,
        )
        renderer = make_skin_renderer(width, height)
        calls = []
        renderer._blend_rect = lambda *args: calls.append(args)
        renderer.draw_song_progress(progress)
        return renderer, calls

    def test_off_mode_draws_nothing(self):
        core.configure_runtime(show_song_progress=core.SONG_PROGRESS_OFF)
        renderer = make_skin_renderer()
        renderer._blend_rect = lambda *args: self.fail("linear draw")
        renderer._blit_premul = lambda *args: self.fail("pie draw")
        renderer.draw_song_progress(0.5)

    def test_pie_mode_uses_cached_centre_for_changing_accuracy(self):
        core.configure_runtime(show_song_progress=core.SONG_PROGRESS_PIE)
        renderer = make_skin_renderer()
        calls = []
        renderer._blit_premul = lambda *args: calls.append(args)
        renderer.draw_song_progress(0.25, acc_value=100.0)
        first_ring = calls[-1][2:4]
        calls.clear()
        renderer.draw_song_progress(0.75, acc_value=1.0)
        self.assertEqual(calls[-1][2:4], first_ring)

    def test_linear_modes_match_stable_480p_geometry(self):
        cases = {
            core.SONG_PROGRESS_TOP_RIGHT: (462.0, 17.2, 170.0),
            core.SONG_PROGRESS_BOTTOM_RIGHT: (522.0, 460.0, 118.0),
            core.SONG_PROGRESS_BOTTOM: (0.0, 477.0, 640.0),
        }
        for mode, (x, y, width) in cases.items():
            with self.subTest(mode=mode):
                _, calls = self._linear_calls(640, 480, mode)
                self.assertEqual(len(calls), 2)
                background, fill = calls
                self.assertAlmostEqual(background[0], x)
                self.assertAlmostEqual(background[1], y)
                self.assertAlmostEqual(background[2], x + width)
                self.assertAlmostEqual(background[3], y + 3.0)
                self.assertEqual(background[4], core.LEGACY_SONG_PROGRESS_LINEAR_BACKGROUND)
                self.assertAlmostEqual(fill[2], x + width * 0.5)
                self.assertEqual(fill[4], core.LEGACY_SONG_PROGRESS_LINEAR_FILL)

    def test_linear_geometry_scales_by_output_height(self):
        _, calls = self._linear_calls(
            1920, 1080, core.SONG_PROGRESS_BOTTOM_RIGHT,
        )
        background = calls[0]
        scale = 1080.0 / 480.0
        self.assertAlmostEqual(background[0], 1920.0 - 118.0 * scale)
        self.assertAlmostEqual(background[1], 460.0 * scale)
        self.assertAlmostEqual(background[2], 1920.0)
        self.assertAlmostEqual(background[3], (460.0 + 3.0) * scale)

    def test_linear_intro_progress_clamps_fill_to_zero(self):
        _, calls = self._linear_calls(
            640, 480, core.SONG_PROGRESS_BOTTOM, progress=-0.5,
        )
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
