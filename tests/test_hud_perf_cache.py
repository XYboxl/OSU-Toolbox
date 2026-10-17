import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


def make_image(alpha):
    a = np.asarray(alpha, dtype=np.float32)[..., None]
    h, w = a.shape[0], a.shape[1]
    rgb = np.repeat(a, 3, axis=2)
    return (rgb, a, w, h)


def make_renderer():
    return object.__new__(core.ManiaRenderer)


class HudGlyphCacheTests(unittest.TestCase):
    def test_alpha_bounds_computed_once_per_image(self):
        renderer = make_renderer()
        img = make_image([[0.0, 1.0], [0.0, 1.0]])
        calls = []
        original = core.ManiaRenderer._skin_image_alpha_bounds

        def counting(image, threshold=0.001):
            calls.append(id(image))
            return original(image, threshold)

        renderer._skin_image_alpha_bounds = counting
        first = renderer._glyph_bounds(img)
        second = renderer._glyph_bounds(img)
        self.assertEqual(first, (1, 0, 2, 2))
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)
        self.assertTrue(renderer._skin_image_visible(img))
        self.assertEqual(len(calls), 1)

    def test_transparent_image_stays_invisible_through_cache(self):
        renderer = make_renderer()
        img = make_image(np.zeros((3, 3), dtype=np.float32))
        self.assertIsNone(renderer._glyph_bounds(img))
        self.assertFalse(renderer._skin_image_visible(img))
        self.assertFalse(renderer._skin_image_visible(img))

    def test_measure_matches_cold_renderer_for_every_text(self):
        glyphs = {
            "0": make_image([[1.0, 1.0, 0.0]] * 4),
            "1": make_image([[0.0, 1.0]] * 5),
            "5": make_image([[1.0, 1.0]] * 6),
            "%": make_image(np.zeros((4, 4), dtype=np.float32)),
            ".": make_image([[1.0]]),
        }
        warm = make_renderer()
        for ch in glyphs:
            warm._measure_skin_text(glyphs, ch * 3, 2, scale=1.0, fixed_width=True)
        texts = ["0123", "55.5%", "%%%", "0.15%", ""]
        for text in texts:
            for fixed_width in (False, True):
                with self.subTest(text=text, fixed_width=fixed_width):
                    cold = make_renderer()
                    self.assertEqual(
                        warm._measure_skin_text(
                            glyphs, text, 2, scale=0.96, fixed_width=fixed_width
                        ),
                        cold._measure_skin_text(
                            glyphs, text, 2, scale=0.96, fixed_width=fixed_width
                        ),
                    )

    def test_transparent_glyph_keeps_its_canvas_advance(self):
        renderer = make_renderer()
        glyphs = {
            "5": make_image([[1.0, 1.0]] * 4),
            "%": make_image(np.zeros((4, 100), dtype=np.float32)),
        }
        width, height = renderer._measure_skin_text(
            glyphs, "55%", 0, scale=1.0, fixed_width=True,
        )
        self.assertEqual((width, height), (104, 4))


if __name__ == "__main__":
    unittest.main()
