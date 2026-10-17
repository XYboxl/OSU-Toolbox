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


OSU_TEMPLATE = """osu file format v14

[General]
AudioFilename: audio.mp3
Mode: 3
LetterboxInBreaks: {letterbox}
SpecialStyle: {special_style}

[Difficulty]
HPDrainRate:5
CircleSize:4
OverallDifficulty:8
ApproachRate:5

[Events]
{events}

[TimingPoints]
0,500,4,2,0,50,1,0

[HitObjects]
{hit_objects}
"""

DEFAULT_OBJECTS = "\n".join([
    "64,192,500,1,0,0:0:0:0:",
    "192,192,700,1,0,0:0:0:0:",
    "320,192,6000,1,0,0:0:0:0:",
])


def write_beatmap(
    root, events="", letterbox=0, special_style=0,
    hit_objects=DEFAULT_OBJECTS,
):
    path = Path(root) / "synthetic.osu"
    path.write_text(
        OSU_TEMPLATE.format(
            letterbox=letterbox,
            special_style=special_style,
            events=events,
            hit_objects=hit_objects,
        ),
        encoding="utf-8",
    )
    return str(path)


class EventParsingTests(unittest.TestCase):
    def test_parse_background_filename_and_offset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, events='0,0,"bg.jpg",12,-5')
            beatmap = core.parse_osu_file(path)
            self.assertEqual(beatmap.background, "bg.jpg")
            self.assertEqual(beatmap.background_offset, (12, -5))

    def test_parse_background_filename_with_commas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(
                temp_dir,
                events='0,0,"artist, title.jpg",12,-5',
            )
            beatmap = core.parse_osu_file(path)
            self.assertEqual(beatmap.background, "artist, title.jpg")
            self.assertEqual(beatmap.background_offset, (12, -5))

    def test_parse_background_filename_with_escaped_quote(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(
                temp_dir,
                events='0,0,"artist ""mix"".jpg",0,0',
            )
            beatmap = core.parse_osu_file(path)
            self.assertEqual(beatmap.background, 'artist "mix".jpg')

    def test_parse_background_tolerates_space_before_quote(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(
                temp_dir,
                events='0, 0, "spaced, name.jpg", 3, 4',
            )
            beatmap = core.parse_osu_file(path)
            self.assertEqual(beatmap.background, "spaced, name.jpg")
            self.assertEqual(beatmap.background_offset, (3, 4))

    def test_parse_break_periods_sorted_and_validated(self):
        events = "\n".join([
            "2,5000,6000",
            "2,1000,4000",
            "2,bad,4000",
            "2,4000,3000",
            "Break,7000,8000",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, events=events)
            beatmap = core.parse_osu_file(path)
            self.assertEqual(
                beatmap.breaks,
                ((1000.0, 4000.0), (5000.0, 6000.0), (7000.0, 8000.0)),
            )

    def test_letterbox_flag_from_general(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            on = core.parse_osu_file(write_beatmap(temp_dir, letterbox=1))
            self.assertTrue(on.letterbox_in_breaks)
        with tempfile.TemporaryDirectory() as temp_dir:
            off = core.parse_osu_file(write_beatmap(temp_dir, letterbox=0))
            self.assertFalse(off.letterbox_in_breaks)

    def test_special_style_flag_from_general(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            on = core.parse_osu_file(
                write_beatmap(temp_dir, special_style=1),
            )
            self.assertTrue(on.special_style)
        with tempfile.TemporaryDirectory() as temp_dir:
            off = core.parse_osu_file(
                write_beatmap(temp_dir, special_style=0),
            )
            self.assertFalse(off.special_style)

    def test_events_section_tolerates_malformed_lines(self):
        events = "\n".join([
            "garbage",
            "0",
            "2,only-one-field",
            'Video,0,"movie.avi"',
            '0,0,"real.png"',
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, events=events)
            beatmap = core.parse_osu_file(path)
            self.assertEqual(beatmap.background, "real.png")
            self.assertEqual(beatmap.breaks, ())

    def test_break_does_not_affect_health_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plain = core.parse_osu_file(write_beatmap(temp_dir))
        with tempfile.TemporaryDirectory() as temp_dir:
            with_break = core.parse_osu_file(
                write_beatmap(temp_dir, events="2,1200,5200"),
            )
        judgements = [
            core.JudgementEvent(o.time, o.col, 320, o.is_hold)
            for o in plain.hit_objects
        ]
        base = core.mania_health_progress(
            plain.hit_objects, judgements, plain.hp_drain_rate,
        )
        breaky = core.mania_health_progress(
            with_break.hit_objects, judgements, with_break.hp_drain_rate,
        )
        self.assertEqual(base, breaky)


class ReplayHealthTests(unittest.TestCase):
    def tearDown(self):
        core.configure_runtime(compatibility_mode=core.COMPAT_STABLE_REPLAY)

    def test_gameplay_health_ignores_osr_results_life_graph(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            beatmap = core.parse_osu_file(write_beatmap(temp_dir))
        replay = SimpleNamespace(
            mods=0,
            frames=[],
            life_graph="0|0.01,10000|0.01,",
            count_geki=0,
            count_300=0,
            count_katu=0,
            count_100=0,
            count_50=0,
            count_miss=len(beatmap.hit_objects),
        )
        core.configure_runtime(compatibility_mode=core.COMPAT_STABLE_REPLAY)
        frames = core.FrameStateIterable(
            beatmap, 4, 0.0, 480.0, fps=60, replay=replay,
        )

        self.assertEqual(frames.life_graph, [(0.0, 0.01), (10000.0, 0.01)])
        self.assertTrue(frames.health_progress)
        first = next(iter(frames))
        self.assertEqual(first["health"], 1.0)
        post_miss = next(
            state for state in frames
            if state["time_ms"] >= beatmap.hit_objects[0].time
        )
        self.assertNotEqual(post_miss["health"], 0.01)


class LetterboxFrameStateTests(unittest.TestCase):
    def tearDown(self):
        core.configure_runtime(
            width=960, height=540, letterbox_in_breaks=None,
        )

    def _letterbox_by_time(self, path):
        beatmap = core.parse_osu_file(path)
        frames, _ = core.iter_frame_states(beatmap, 4, 0.0, 480.0, fps=60)
        samples = {}
        for state in frames:
            samples[round(state["time_ms"])] = state["letterbox"]
        return samples

    def test_letterbox_envelope_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, events="2,1200,5200", letterbox=1)
            samples = self._letterbox_by_time(path)
        mid = [v for t, v in samples.items() if 2500 <= t <= 3500]
        self.assertTrue(mid)
        self.assertTrue(all(abs(v - 1.0) < 1e-6 for v in mid))
        outside = [v for t, v in samples.items() if t <= 1100 or t >= 5300]
        self.assertTrue(all(v == 0.0 for v in outside))

    def test_letterbox_disabled_without_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, events="2,1200,5200", letterbox=0)
            samples = self._letterbox_by_time(path)
        self.assertTrue(all(v == 0.0 for v in samples.values()))

    def test_short_break_never_shows_letterbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, events="2,1200,1500", letterbox=1)
            samples = self._letterbox_by_time(path)
        self.assertTrue(all(v == 0.0 for v in samples.values()))


class LetterboxAndBackgroundDrawTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def tearDown(self):
        core.configure_runtime(
            width=960, height=540,
            background_path=None, background_dim=0.8,
        )

    def test_draw_letterbox_blends_top_and_bottom(self):
        renderer = object.__new__(core.ManiaRenderer)
        renderer.width = 64
        renderer.height = 64
        renderer.frame = np.full((64, 64, 3), 200, dtype=np.uint8)
        renderer.draw_letterbox(1.0)
        bar_h = round(64 * core.LEGACY_LETTERBOX_HEIGHT_RATIO)
        self.assertEqual(int(renderer.frame[0].max()), 0)
        self.assertEqual(int(renderer.frame[bar_h - 1].max()), 0)
        self.assertEqual(int(renderer.frame[32].min()), 200)
        self.assertEqual(int(renderer.frame[-1].max()), 0)

    def test_background_painted_with_dim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bg = Path(temp_dir) / "bg.png"
            surface = pygame.Surface((100, 50))
            surface.fill((200, 0, 0))
            pygame.image.save(surface, str(bg))
            core.configure_runtime(background_path=str(bg), background_dim=0.5)
            renderer = object.__new__(core.ManiaRenderer)
            renderer.width = 64
            renderer.height = 64
            renderer.frame = np.zeros((64, 64, 3), dtype=np.uint8)
            painted = renderer._paint_background_image()
            self.assertTrue(painted)
            red = renderer.frame[:, :, 0].astype(np.int64)
            self.assertTrue(int(red.min()) >= 95 and int(red.max()) <= 105)
            self.assertEqual(int(renderer.frame[:, :, 1].max()), 0)

    def test_background_missing_file_is_noop(self):
        core.configure_runtime(background_path="__missing__.png")
        renderer = object.__new__(core.ManiaRenderer)
        renderer.width = 8
        renderer.height = 8
        renderer.frame = np.zeros((8, 8, 3), dtype=np.uint8)
        self.assertFalse(renderer._paint_background_image())


if __name__ == "__main__":
    unittest.main()
