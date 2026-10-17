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


def make_image(width, height):
    alpha = np.ones((height, width, 1), dtype=np.float32)
    rgb = np.repeat(alpha, 3, axis=2) * 255.0
    return rgb, alpha, width, height


def make_key_warning_renderer(receptors, upside_down=False):
    renderer = object.__new__(core.ManiaRenderer)
    renderer.width = 640
    renderer.height = 480
    renderer.key_count = len(receptors)
    renderer.y_judge = 400.0
    renderer.lane_rects = [
        (100.0 + 40.0 * index, 0.0, 140.0 + 40.0 * index, 480.0)
        for index in range(len(receptors))
    ]
    renderer.geometry = SimpleNamespace(upside_down=upside_down)
    renderer.assets = SimpleNamespace(receptor=receptors)
    renderer.skin_config = SimpleNamespace(
        colour_key_warning=(1.0, 1.0, 1.0, 1.0),
    )
    sprite = make_image(10, 6)
    renderer._key_warning_sprite = lambda _text, _colour: sprite
    calls = []
    renderer._blit_premul = lambda _rgb, _alpha, x, y, w, h: calls.append(
        (x, y, w, h, 1.0)
    )
    renderer._blit_skin_image = lambda _image, x, y, **kwargs: calls.append(
        (x, y, sprite[2], sprite[3], kwargs.get("alpha_scale", 1.0))
    )
    return renderer, calls


def make_combo_burst_renderer(style, upside_down=False):
    renderer = object.__new__(core.ManiaRenderer)
    renderer.skin_config = SimpleNamespace(combo_burst_style=style)
    renderer.geometry = SimpleNamespace(upside_down=upside_down)
    renderer.stage_ranges = [(100.0, 200.0), (300.0, 450.0)]
    renderer.stage_y1 = 20.0
    renderer.stage_y2 = 460.0
    image = make_image(30, 50)
    selections = []

    def image_for_event(event_index, side):
        selections.append((event_index, side))
        return image

    renderer.assets = SimpleNamespace(
        combo_burst=[image],
        combo_burst_for_event=image_for_event,
    )
    calls = []
    renderer._blit_skin_image = lambda selected, x, y, **kwargs: calls.append(
        (selected, x, y, kwargs)
    )
    return renderer, selections, calls


class KeyWarningTests(unittest.TestCase):
    def test_labels_use_each_receptor_canvas_centre(self):
        renderer, calls = make_key_warning_renderer([
            make_image(40, 96),
            make_image(40, 64),
        ])

        renderer.draw_key_warning(0.0, bindings=("D", "K"))

        self.assertEqual(calls[0][:4], (115, 429, 10, 6))
        self.assertEqual(calls[1][:4], (155, 445, 10, 6))

    def test_upside_down_mirrors_receptor_centre(self):
        renderer, calls = make_key_warning_renderer(
            [make_image(40, 96)], upside_down=True,
        )

        renderer.draw_key_warning(0.0, bindings=("D",))

        self.assertEqual(calls[0][:4], (115, 45, 10, 6))

    def test_missing_receptor_skips_key_warning(self):
        renderer, calls = make_key_warning_renderer([None])

        renderer.draw_key_warning(0.0, bindings=("D",))

        self.assertEqual(calls, [])

    def test_key_warning_alpha_matches_stable_envelope(self):
        self.assertEqual(core._legacy_key_warning_alpha(0.0), 1.0)
        self.assertEqual(core._legacy_key_warning_alpha(500.0), 1.0)
        self.assertAlmostEqual(core._legacy_key_warning_alpha(2000.0), 0.5)
        self.assertEqual(core._legacy_key_warning_alpha(3500.0), 0.0)


class ComboBurstTests(unittest.TestCase):
    def test_combo_burst_timing_matches_stable(self):
        self.assertEqual(core._legacy_combo_burst_state(-1.0), (1.0, 0.0))
        _, alpha = core._legacy_combo_burst_state(125.0)
        self.assertAlmostEqual(alpha, 0.5)
        _, alpha = core._legacy_combo_burst_state(400.0)
        self.assertEqual(alpha, 1.0)
        _, alpha = core._legacy_combo_burst_state(900.0)
        self.assertAlmostEqual(alpha, 0.5)
        self.assertEqual(core._legacy_combo_burst_state(1400.0), (1.0, 0.0))

    def test_left_and_right_use_outer_split_stage_edges(self):
        movement, _ = core._legacy_combo_burst_state(400.0)
        left, left_selections, left_calls = make_combo_burst_renderer(0)
        right, right_selections, right_calls = make_combo_burst_renderer(1)

        left.draw_combo_burst(400.0, event_index=3)
        right.draw_combo_burst(400.0, event_index=4)

        self.assertEqual(left_selections, [(3, "left")])
        self.assertEqual(right_selections, [(4, "right")])
        self.assertEqual(left_calls[0][1], int(round(100.0 + 100.0 * movement - 30.0)))
        self.assertEqual(right_calls[0][1], int(round(450.0 - 150.0 * movement)))
        self.assertEqual(left_calls[0][2], 410)
        self.assertEqual(right_calls[0][2], 410)

    def test_upside_down_anchors_burst_to_stage_top(self):
        renderer, _, calls = make_combo_burst_renderer(0, upside_down=True)

        renderer.draw_combo_burst(400.0)

        self.assertEqual(calls[0][2], 20)

    def test_undefined_combo_burst_style_draws_nothing(self):
        renderer, selections, calls = make_combo_burst_renderer(-1)

        renderer.draw_combo_burst(400.0)

        self.assertEqual(selections, [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
