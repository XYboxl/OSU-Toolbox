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
from tests.test_beatmap_events import write_beatmap


def make_image(width, height):
    a = np.ones((height, width, 1), dtype=np.float32)
    a[0, 0, 0] = 0.25
    rgb = np.repeat(a, 3, axis=2) * 255.0
    return (rgb, a, width, height)


def make_renderer(arrow=None, upside_down=False):
    renderer = object.__new__(core.ManiaRenderer)
    renderer.assets = SimpleNamespace(warning_arrow=arrow)
    renderer.geometry = SimpleNamespace(upside_down=upside_down)
    renderer.stage_ranges = ((200.0, 400.0), (500.0, 700.0))
    calls = []

    def blit(image, x, y, **kwargs):
        calls.append((image, x, y, kwargs))

    renderer._blit_skin_image = blit
    return renderer, calls


class WarningTimeTests(unittest.TestCase):
    @staticmethod
    def _beatmap(events=""):
        objects = "\n".join([
            "64,192,5000,1,0,0:0:0:0:",
            "192,192,6000,1,0,0:0:0:0:",
        ])
        temp_dir = tempfile.TemporaryDirectory()
        path = write_beatmap(
            temp_dir.name,
            events=events,
            hit_objects=objects,
        )
        return temp_dir, core.parse_osu_file(path)

    def test_prestart_warnings_use_fixed_one_second_offsets(self):
        temp_dir, beatmap = self._beatmap()
        self.addCleanup(temp_dir.cleanup)
        frames = core.FrameStateIterable(beatmap, 4, 0.0, 480.0, fps=60)
        self.assertEqual(frames.warning_times, (2000.0, 3000.0, 4000.0))

    def test_first_timing_segment_is_extended_before_warning_cutoff(self):
        temp_dir, beatmap = self._beatmap()
        self.addCleanup(temp_dir.cleanup)
        beatmap.timing_points[0].time = 3000.0
        frames = core.FrameStateIterable(beatmap, 4, 0.0, 480.0, fps=60)
        self.assertEqual(frames.scroll_path.first_timing_time, -3000.0)
        self.assertEqual(frames.warning_times, (2000.0, 3000.0, 4000.0))

    def test_breaks_do_not_create_warning_arrows(self):
        temp_dir, beatmap = self._beatmap(events="2,1200,5200")
        self.addCleanup(temp_dir.cleanup)
        frames = core.FrameStateIterable(beatmap, 4, 0.0, 480.0, fps=60)
        self.assertEqual(frames.warning_times, (2000.0, 3000.0, 4000.0))

    def test_warning_uses_the_note_scroll_path(self):
        temp_dir, beatmap = self._beatmap()
        self.addCleanup(temp_dir.cleanup)
        core.configure_runtime(pre_roll_ms=4000.0, scroll_ar_override=5.0)
        try:
            frames = core.FrameStateIterable(
                beatmap, 4, 0.0, 420.0, fps=10,
            )
            states = list(frames)
        finally:
            core.configure_runtime(pre_roll_ms=None, scroll_ar_override=None)
        approaching = next(state for state in states if state["time_ms"] >= 3400.0)
        past_judge = next(state for state in states if state["time_ms"] >= 4100.0)
        offscreen = next(state for state in states if state["time_ms"] >= 4200.0)
        self.assertEqual(len(approaching["warning_arrows"]), 1)
        self.assertAlmostEqual(
            approaching["warning_arrows"][0],
            frames._screen_position(4000.0, approaching["time_ms"]),
        )
        self.assertGreater(past_judge["warning_arrows"][0], 420.0)
        self.assertEqual(offscreen["warning_arrows"], [])

    def test_entry_uses_actual_scaled_texture_height_and_exit_uses_raw_480(self):
        temp_dir, beatmap = self._beatmap()
        self.addCleanup(temp_dir.cleanup)
        core.configure_runtime(
            pre_roll_ms=2000.0,
            scroll_ar_override=5.0,
            show_warning_arrow=True,
        )
        try:
            frames = core.FrameStateIterable(
                beatmap,
                4,
                0.0,
                420.0,
                fps=1000,
                warning_arrow_height_480=24.0,
            )
            first_warning = frames.warning_times[0]
            entry_time = frames.scroll_path.time_for_raw_position(
                first_warning,
                -24.0,
                frames.hit_position,
            )
            fixed_entry_time = frames.scroll_path.time_for_raw_position(
                first_warning,
                -480.0,
                frames.hit_position,
            )
            exit_time = frames.scroll_path.time_for_raw_position(
                first_warning,
                480.0,
                frames.hit_position,
            )
            early = entered = pre_exit = post_exit = None
            for state in frames:
                time_ms = state["time_ms"]
                if early is None and time_ms >= 0.0:
                    early = state
                if entered is None and time_ms >= entry_time:
                    entered = state
                if time_ms < exit_time:
                    pre_exit = state
                elif (
                    post_exit is None
                    and time_ms > exit_time + frames.frame_dt_ms
                ):
                    post_exit = state
                    break
        finally:
            core.configure_runtime(pre_roll_ms=None, scroll_ar_override=None)

        self.assertGreater(entry_time, fixed_entry_time)
        self.assertEqual(early["warning_arrows"], [])
        self.assertEqual(len(entered["warning_arrows"]), 1)
        self.assertGreaterEqual(entered["warning_arrows"][0], -24.0)
        self.assertGreater(max(pre_exit["warning_arrows"]), 479.0)
        self.assertTrue(
            all(position < 400.0 for position in post_exit["warning_arrows"])
        )


class WarningAssetMeasurementTests(unittest.TestCase):
    def test_at2x_density_and_stage_scale_match_loaded_sprite_height(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "[Mania]",
                    "Keys: 4",
                    "WarningArrow: custom-warning",
                ]),
                encoding="utf-8",
            )
            surface = pygame.Surface((32, 64), pygame.SRCALPHA)
            surface.fill((255, 255, 255, 255))
            pygame.image.save(surface, str(root / "custom-warning@2x.png"))

            core.configure_runtime(width=640, height=480)
            skin = core.load_skin_config(str(root), 4)
            stage_scale = 0.75
            measured = core.SkinAssets.warning_arrow_height_480(
                skin,
                stage_scale,
            )
            self.assertAlmostEqual(measured, 15.0)

            layout = core.resolve_stage_layout(640, 480, 4, skin)
            assets = core.SkinAssets(
                skin,
                layout.lane_rects,
                stage_ranges=layout.stage_ranges,
                stage_scale=stage_scale,
            )
            self.assertEqual(assets.warning_arrow[3], 15)

    def test_missing_custom_warning_sprite_has_zero_lifecycle_height(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skin = core.load_skin_config(temp_dir, 4)
            self.assertEqual(
                core.SkinAssets.warning_arrow_height_480(skin),
                0.0,
            )


class WarningDrawTests(unittest.TestCase):
    def test_missing_image_is_noop(self):
        renderer, calls = make_renderer(arrow=None)
        renderer.draw_warning_arrows((400.0,))
        self.assertEqual(calls, [])

    def test_one_arrow_is_drawn_at_each_stage_centre(self):
        arrow = make_image(16, 24)
        renderer, calls = make_renderer(arrow=arrow)
        renderer.draw_warning_arrows((400.0,))
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[1] for call in calls], [292, 592])
        for _, _, y, kwargs in calls:
            self.assertEqual(y, 400)
            self.assertEqual(kwargs, {})

    def test_upside_down_flips_and_bottom_aligns_the_image(self):
        arrow = make_image(16, 24)
        renderer, calls = make_renderer(arrow=arrow, upside_down=True)
        renderer.draw_warning_arrows((100.0,))
        drawn = calls[0][0]
        np.testing.assert_array_equal(drawn[1], arrow[1][::-1])
        self.assertEqual(drawn[2], arrow[2])
        self.assertEqual([call[2] for call in calls], [76, 76])
        renderer.draw_warning_arrows((100.0,))
        self.assertIs(calls[2][0], drawn)

    def test_entry_anchor_is_fully_offscreen_in_both_directions(self):
        arrow = make_image(16, 24)
        renderer, calls = make_renderer(arrow=arrow)
        renderer.draw_warning_arrows((-24.0,))
        self.assertEqual(calls[0][2], -24)

        renderer, calls = make_renderer(arrow=arrow, upside_down=True)
        renderer.draw_warning_arrows((504.0,))
        self.assertEqual(calls[0][2], 480)

    def test_disabled_flag_draws_nothing(self):
        arrow = make_image(16, 24)
        renderer, calls = make_renderer(arrow=arrow)
        core.configure_runtime(show_warning_arrow=False)
        try:
            renderer.draw_warning_arrows((100.0,))
            self.assertEqual(calls, [])
        finally:
            core.configure_runtime(show_warning_arrow=True)


if __name__ == "__main__":
    unittest.main()
