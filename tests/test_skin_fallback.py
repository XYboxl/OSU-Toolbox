import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import pygame

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


class CustomSkinRequirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def tearDown(self):
        core.configure_runtime(width=960, height=540, scroll_direction="auto")

    @staticmethod
    def _write_skin_ini(root):
        (root / "skin.ini").write_text(
            "\n".join([
                "[General]",
                "Version: 2.7",
                "[Mania]",
                "Keys: 4",
            ]),
            encoding="utf-8",
        )

    def test_missing_skin_directory_is_rejected(self):
        with self.assertRaisesRegex(
            core.SkinConfigError,
            "custom skin directory is required",
        ):
            core.load_skin_config(None, 4)
        with self.assertRaisesRegex(
            core.SkinConfigError,
            "skin directory does not exist",
        ):
            core.load_skin_config(str(ROOT / "missing-skin"), 4)

    def test_renderer_and_asset_loader_require_skin_config(self):
        core.configure_runtime(width=640, height=480)
        with self.assertRaisesRegex(ValueError, "custom SkinConfig"):
            core.ManiaRenderer(640, 480, 4, None)
        with self.assertRaisesRegex(ValueError, "custom SkinConfig"):
            core.SkinAssets(None, [(0, 0, 30, 480)])

    def test_missing_custom_resources_are_not_synthesised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            core.configure_runtime(width=640, height=480)
            skin = core.load_skin_config(temp_dir, 4)
            renderer = core.ManiaRenderer(640, 480, 4, skin)

            self.assertEqual(renderer.assets.score_font, {})
            self.assertEqual(renderer.assets.combo_font, {})
            self.assertEqual(renderer.assets.receptor, [None] * 4)
            self.assertEqual(renderer.assets.note, [None] * 4)
            self.assertFalse(renderer.assets.scorebar_bg.present)

            renderer.reset_frame()
            before = renderer.frame.copy()
            renderer.draw_receptors(0)
            renderer.draw_skin_tap(0, *(
                renderer.lane_rects[0][0], renderer.lane_rects[0][2]
            ), renderer.y_judge - 30)
            renderer.draw_score_display(12345)
            renderer.draw_acc_display(98.76)
            renderer.draw_combo_display(123)
            renderer.draw_health_bar(0.5)

            np.testing.assert_array_equal(renderer.frame, before)

    def test_partial_custom_font_does_not_inject_missing_glyphs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_skin_ini(root)
            custom = pygame.Surface((13, 17), pygame.SRCALPHA)
            custom.fill((255, 80, 40, 255))
            pygame.image.save(custom, str(root / "score-5.png"))
            transparent = pygame.Surface((19, 11), pygame.SRCALPHA)
            pygame.image.save(transparent, str(root / "score-percent.png"))

            core.configure_runtime(width=640, height=480)
            skin = core.load_skin_config(str(root), 4)
            renderer = core.ManiaRenderer(640, 480, 4, skin)

            custom_five = renderer.assets.score_font["5"]
            explicit_percent = renderer.assets.score_font["%"]
            self.assertTrue(hasattr(custom_five, "logical_width"))
            self.assertTrue(hasattr(explicit_percent, "logical_width"))
            self.assertNotIn("1", renderer.assets.score_font)
            self.assertEqual(set(renderer.assets.combo_font), {"5"})
            self.assertEqual(explicit_percent.logical_width, 19 * 480 / 768)
            self.assertFalse(np.any(explicit_percent[1] > 0.0))

            renderer.reset_frame()
            renderer.draw_score_display(15001)
            renderer.draw_acc_display(95.25)
            renderer.draw_song_progress(0.25, False, 95.25)
            renderer.draw_combo_display(515)


if __name__ == "__main__":
    unittest.main()
