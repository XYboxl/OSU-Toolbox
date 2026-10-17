import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


class LegacySkinAnimationAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def tearDown(self):
        core.configure_runtime(width=960, height=540)

    @staticmethod
    def _save_frame(root, name, value, size=(8, 8)):
        surface = pygame.Surface(size, pygame.SRCALPHA)
        surface.fill((value, value, value, 255))
        pygame.image.save(surface, str(root / name))

    @staticmethod
    def _frame_value(image):
        return round(float(image[0][0, 0, 0]))

    def _assets(
        self,
        root,
        extra_ini=(),
        lane_rects=None,
        stage_ranges=None,
        note_ref="tap",
        head_ref="head",
        beatmap_root=None,
    ):
        lines = [
            "[General]",
            "Version: 2.7",
            "AnimationFramerate: 5",
            "",
            "[Mania]",
            "Keys: 1",
            "ColumnWidth: 30",
            f"NoteImage0: {note_ref}",
            f"NoteImage0H: {head_ref}",
            "NoteImage0L: body",
            "NoteImage0T: tail",
            "StageLight: stage-light",
            "StageHint: stage-hint",
            "StageBottom: stage-bottom",
        ]
        lines.extend(extra_ini)
        (root / "skin.ini").write_text("\n".join(lines), encoding="utf-8")
        config = core.load_skin_config(
            str(root),
            1,
            beatmap_root=beatmap_root,
        )
        return core.SkinAssets(
            config,
            lane_rects or [(0, 0, 30, 480)],
            stage_ranges=stage_ranges,
        )

    def test_note_and_hold_piece_animation_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for prefix in ("tap", "head", "body", "tail"):
                self._save_frame(root, f"{prefix}-0.png", 64)
                self._save_frame(root, f"{prefix}-1.png", 192)
            assets = self._assets(root)

            self.assertEqual(self._frame_value(assets.note_for_age(0, 0)), 64)
            self.assertEqual(
                self._frame_value(assets.note_for_age(0, 1000 / 60.0)), 192,
            )
            self.assertEqual(
                self._frame_value(assets.note_for_age(0, 2000 / 60.0)), 64,
            )
            self.assertEqual(
                self._frame_value(assets.hold_piece_for_age(0, "head", 1000 / 60.0)),
                192,
            )
            self.assertEqual(
                self._frame_value(assets.hold_piece_for_age(0, "tail", 1000 / 60.0)),
                192,
            )
            self.assertEqual(
                self._frame_value(assets.hold_piece_for_age(0, "body", 60, False)),
                64,
            )
            self.assertEqual(
                self._frame_value(assets.hold_piece_for_age(0, "body", 30, True)),
                192,
            )
            self.assertEqual(
                self._frame_value(assets.hold_piece_for_age(0, "body", 30, False)),
                192,
            )

    def test_global_and_event_animation_frame_rates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for prefix in (
                "stage-light", "stage-bottom", "scorebar-colour",
            ):
                self._save_frame(root, f"{prefix}-0.png", 64)
                self._save_frame(root, f"{prefix}-1.png", 192)
            self._save_frame(root, "scorebar-bg.png", 64)
            self._save_frame(root, "scorebar-marker.png", 64)
            assets = self._assets(root, [
                "LightFramePerSecond: 10",
            ])

            self.assertGreater(
                self._frame_value(assets.stage_light_for_age(0, 100)), 128,
            )
            self.assertLess(
                self._frame_value(assets.stage_light_for_age(0, 200)), 128,
            )
            self.assertGreater(
                self._frame_value(assets.stage_bottom_for_age(1000 / 60.0)), 128,
            )
            self.assertGreater(
                self._frame_value(assets.scorebar_colour_for_time(200)), 128,
            )
            self.assertLess(
                self._frame_value(assets.scorebar_colour_for_time(400)), 128,
            )

    def test_frame_selection_uses_timestamps_not_render_fps(self):
        frames = [object(), object(), object()]
        expected = frames[1]
        for render_fps in (30.0, 60.0, 120.0):
            frame_number = round(0.2 * render_fps)
            timestamp_ms = frame_number * 1000.0 / render_fps
            with self.subTest(render_fps=render_fps):
                self.assertIs(
                    core.SkinAssets._loop_frame_for_age(
                        frames, 5.0, timestamp_ms,
                    ),
                    expected,
                )

    def test_fresh_segment_asset_loader_keeps_absolute_phase(self):
        frames = [object(), object(), object()]
        boundary_time_ms = 1266.0
        before_boundary = core.SkinAssets._loop_frame_for_age(
            frames, 5.0, boundary_time_ms,
        )
        after_fresh_loader = core.SkinAssets._loop_frame_for_age(
            list(frames), 5.0, boundary_time_ms,
        )
        self.assertIs(after_fresh_loader, before_boundary)

    def test_looping_and_one_shot_animation_lifetimes_are_distinct(self):
        frames = [object(), object(), object()]
        self.assertIs(
            core.SkinAssets._loop_frame_for_age(frames, 10.0, 400.0),
            frames[1],
        )
        self.assertIs(
            core.SkinAssets._once_frame_for_age(frames, 10.0, 400.0),
            frames[-1],
        )

    def test_non_finite_animation_ages_do_not_crash(self):
        frames = [object(), object(), object()]
        self.assertIs(
            core.SkinAssets._loop_frame_for_age(frames, 10.0, float("nan")),
            frames[0],
        )
        self.assertIs(
            core.SkinAssets._once_frame_for_age(frames, 10.0, float("inf")),
            frames[-1],
        )

    def test_stage_hint_numbered_frames_do_not_form_an_animation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_frame(root, "stage-hint-0.png", 64, size=(10, 2))
            self._save_frame(root, "stage-hint-1.png", 192, size=(10, 2))
            assets = self._assets(
                root,
                stage_ranges=((0, 90), (100, 160)),
            )

            self.assertIsNone(assets.stage_hint_for_time(0, 0))
            self.assertIsNone(assets.stage_hint_for_time(1, 1000))

    def test_stage_hint_always_uses_the_static_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_frame(root, "stage-hint.png", 224, size=(10, 2))
            self._save_frame(root, "stage-hint-0.png", 32, size=(10, 2))
            self._save_frame(root, "stage-hint-1.png", 64, size=(10, 2))
            assets = self._assets(root)

            first = assets.stage_hint_for_time(0, 0)
            self.assertIs(first, assets.stage_hint_for_time(0, 5000))
            self.assertGreater(self._frame_value(first), 200)

    def test_stage_hint_numbered_frames_do_not_block_beatmap_static_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            selected = base / "selected"
            beatmap = base / "beatmap"
            selected.mkdir()
            beatmap.mkdir()
            self._save_frame(selected, "stage-hint-0.png", 32, size=(10, 2))
            self._save_frame(beatmap, "stage-hint.png", 224, size=(10, 2))

            assets = self._assets(selected, beatmap_root=beatmap)

            self.assertGreater(
                self._frame_value(assets.stage_hint_for_time(0, 1000)),
                200,
            )

    def test_transparent_animation_is_an_explicit_resource(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transparent = pygame.Surface((8, 8), pygame.SRCALPHA)
            pygame.image.save(transparent, str(root / "tail-0.png"))
            pygame.image.save(transparent, str(root / "tail-1.png"))
            assets = self._assets(root)
            self.assertEqual(len(assets.tail_frames[0]), 2)
            self.assertIs(assets.tail[0], assets.tail_frames[0][0])
            self.assertEqual(float(assets.tail[0][1].max()), 0.0)

    def test_missing_frame_ends_animation_sequence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_frame(root, "tap-0.png", 64)
            self._save_frame(root, "tap-2.png", 192)
            assets = self._assets(root)
            self.assertEqual(len(assets.note_frames[0]), 1)

    def test_jpg_static_and_animation_resources_are_loaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_frame(root, "tap-0.jpg", 64)
            self._save_frame(root, "tap-1.jpg", 192)
            self._save_frame(root, "head@2x.jpg", 128, size=(16, 16))
            assets = self._assets(
                root,
                note_ref="tap.jpg",
                head_ref="head.jpg",
            )

            self.assertEqual(len(assets.note_frames[0]), 2)
            self.assertIsNotNone(assets.head[0])
            self.assertEqual(assets.head[0][2], 30)
            self.assertGreater(assets.head[0][3], 0)

    def test_corrupt_animation_frame_terminates_the_sequence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_frame(root, "tap-0.png", 64)
            (root / "tap-1.png").write_bytes(b"not an image")
            self._save_frame(root, "tap-2.png", 192)
            assets = self._assets(root)

            self.assertEqual(len(assets.note_frames[0]), 1)
            self.assertLess(self._frame_value(assets.note_frames[0][0]), 128)

    def test_corrupt_hd_resource_does_not_fall_through_to_sd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "tap@2x.png").write_bytes(b"not an image")
            self._save_frame(root, "tap.png", 192)
            assets = self._assets(root, note_ref="tap.png")

            self.assertIsNone(assets.note[0])
            self.assertEqual(assets.note_frames[0], [])

    def test_missing_configured_name_uses_conventional_mania_resource(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._save_frame(root, "mania-noteS.png", 192)
            assets = self._assets(root)

            self.assertGreater(self._frame_value(assets.note[0]), 128)

    def test_transparent_static_mania_resources_remain_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transparent = pygame.Surface((8, 8), pygame.SRCALPHA)
            for name in (
                "body.png",
                "stage-light.png",
                "mania-stage-left.png",
                "mania-stage-right.png",
                "stage-bottom.png",
                "mania-warningarrow.png",
            ):
                pygame.image.save(transparent, str(root / name))
            assets = self._assets(root)

            for image in (
                assets.body[0],
                assets.stage_light[0],
                assets.stage_left,
                assets.stage_right,
                assets.stage_bottom,
                assets.warning_arrow,
            ):
                self.assertIsNotNone(image)
                self.assertEqual(float(image[1].max()), 0.0)

    def test_configured_reference_resolves_across_all_providers_before_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            selected = base / "selected"
            beatmap = base / "beatmap"
            selected.mkdir()
            beatmap.mkdir()
            self._save_frame(selected, "tap.png", 96)
            self._save_frame(beatmap, "mania-noteS.png", 224)

            assets = self._assets(
                selected,
                beatmap_root=beatmap,
            )
            self.assertAlmostEqual(self._frame_value(assets.note[0]), 96, delta=2)

            self._save_frame(beatmap, "tap.png", 224)
            assets = self._assets(
                selected,
                beatmap_root=beatmap,
            )
            self.assertGreater(self._frame_value(assets.note[0]), 200)

    def test_higher_provider_static_beats_lower_provider_animation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            selected = base / "selected"
            beatmap = base / "beatmap"
            selected.mkdir()
            beatmap.mkdir()
            self._save_frame(beatmap, "tap.png", 224)
            self._save_frame(selected, "tap-0.png", 48)
            self._save_frame(selected, "tap-1.png", 96)

            assets = self._assets(selected, beatmap_root=beatmap)
            self.assertEqual(assets.note_frames[0], [])
            self.assertGreater(self._frame_value(assets.note[0]), 200)

    def test_animation_sequence_is_locked_to_frame_zero_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            selected = base / "selected"
            beatmap = base / "beatmap"
            selected.mkdir()
            beatmap.mkdir()
            self._save_frame(beatmap, "tap-0.png", 224)
            self._save_frame(selected, "tap.png", 48)
            self._save_frame(selected, "tap-1.png", 64)
            self._save_frame(selected, "tap-2.png", 96)

            assets = self._assets(selected, beatmap_root=beatmap)
            self.assertEqual(len(assets.note_frames[0]), 1)
            self.assertGreater(
                self._frame_value(assets.note_frames[0][0]),
                200,
            )

    def test_missing_selected_resource_is_not_synthesised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            selected = base / "selected"
            selected.mkdir()

            assets = self._assets(selected)
            self.assertIsNone(assets.note[0])


if __name__ == "__main__":
    unittest.main()
