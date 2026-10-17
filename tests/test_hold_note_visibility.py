import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core
from test_beatmap_events import write_beatmap

LN_OBJECTS = "\n".join([
    "64,192,1000,128,0,3000:0:0:0:0:",
    "192,192,6000,1,0,0:0:0:0:",
])


def make_replay(key_frames, miss=0, count_300=0):
    frames = [
        SimpleNamespace(time=float(time), keys=int(keys), seq=seq)
        for seq, (time, keys) in enumerate(key_frames)
    ]
    return SimpleNamespace(
        mods=0,
        frames=frames,
        life_graph="",
        count_geki=0,
        count_300=count_300,
        count_katu=0,
        count_100=0,
        count_50=0,
        count_miss=miss,
    )


class HoldNoteConsumptionTests(unittest.TestCase):
    """被按住的 LN 在判定线处被消耗，尾端不越线下落。"""

    def _frames(self, y_top, y_judge):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, hit_objects=LN_OBJECTS)
            beatmap = core.parse_osu_file(path)
        return core.FrameStateIterable(beatmap, 4, y_top, y_judge, fps=60)

    def test_downscroll_tail_never_passes_judgement_line(self):
        seen_active = seen_after = False
        for state in self._frames(0.0, 480.0):
            t = state["time_ms"]
            holds = [n for n in state["notes"] if n["is_hold"]]
            if t >= 3000.0:
                self.assertEqual(holds, [])
                if t <= 3200.0:
                    seen_after = True
                continue
            for note in holds:
                self.assertLessEqual(note["tail_y"], 480.0 + 1e-6)
                if t >= 1000.0:
                    seen_active = True
                    self.assertEqual(note["y"], 480.0)
        self.assertTrue(seen_active)
        self.assertTrue(seen_after)

    def test_upscroll_tail_never_passes_judgement_line(self):
        seen_active = False
        for state in self._frames(480.0, 0.0):
            t = state["time_ms"]
            holds = [n for n in state["notes"] if n["is_hold"]]
            if t >= 3000.0:
                self.assertEqual(holds, [])
                continue
            for note in holds:
                self.assertGreaterEqual(note["tail_y"], -1e-6)
                if t >= 1000.0:
                    seen_active = True
                    self.assertEqual(note["y"], 0.0)
        self.assertTrue(seen_active)

    def test_unhit_approach_is_untouched(self):
        frames = self._frames(0.0, 480.0)
        approach_heads = []
        for state in frames:
            if state["time_ms"] >= 1000.0:
                break
            for note in state["notes"]:
                if note["is_hold"]:
                    approach_heads.append(note["y"])
        self.assertTrue(approach_heads)
        self.assertTrue(all(y <= 480.0 + 1e-6 for y in approach_heads))
        self.assertLess(approach_heads[0], approach_heads[-1])


class MissedHoldFallThroughTests(unittest.TestCase):
    """未命中/断体的 LN 自由下落，而不是钉在判定线上。"""

    def _replay_frames(self, replay):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_beatmap(temp_dir, hit_objects=LN_OBJECTS)
            beatmap = core.parse_osu_file(path)
        return core.FrameStateIterable(
            beatmap, 4, 0.0, 480.0, fps=60, replay=replay,
        )

    def test_untouched_hold_falls_freely_past_line(self):
        frames = self._replay_frames(make_replay([], miss=2))
        preempt = frames.preempt_ms
        heads = []
        for state in frames:
            t = state["time_ms"]
            holds = [n for n in state["notes"] if n["is_hold"]]
            if 1000.0 <= t <= 3000.0 and holds:
                heads.append((t, holds[0]["y"]))
            if t >= 3050.0:
                self.assertEqual(holds, [])
        self.assertTrue(heads)
        self.assertGreater(heads[-1][1], 500.0)
        ys = [y for _, y in heads]
        self.assertEqual(ys, sorted(ys))
        self.assertNotIn(480.0, ys[3:])

    def test_dropped_hold_falls_from_break_time(self):
        replay = make_replay([(1000.0, 1), (1500.0, 0)], miss=2)
        frames = self._replay_frames(replay)
        preempt = frames.preempt_ms
        speed = frames.speed
        for state in frames:
            t = state["time_ms"]
            holds = [n for n in state["notes"] if n["is_hold"]]
            if 1050.0 <= t <= 1450.0:
                self.assertTrue(holds)
                self.assertEqual(holds[0]["y"], 480.0)
            elif 1550.0 <= t <= 2900.0:
                self.assertTrue(holds)
                expected = 480.0 + (t - 1500.0) * speed
                self.assertAlmostEqual(holds[0]["y"], expected, places=6)
                self.assertLess(holds[0]["tail_y"], holds[0]["y"])
            elif t >= 3050.0:
                self.assertEqual(holds, [])

    def test_missed_hold_never_lights_even_when_key_held(self):
        replay = make_replay([(1200.0, 1), (4000.0, 0)], miss=2)
        frames = self._replay_frames(replay)
        for state in frames:
            t = state["time_ms"]
            if 1300.0 <= t <= 2900.0:
                self.assertIsNone(state["hold_light_states"][0])
                self.assertFalse(state["combo_holding"])

    def test_held_hold_still_lights_before_break(self):
        replay = make_replay([(1000.0, 1), (1500.0, 0)], miss=2)
        frames = self._replay_frames(replay)
        lit = False
        for state in frames:
            t = state["time_ms"]
            if 1050.0 <= t <= 1450.0:
                if state["hold_light_states"][0] is not None:
                    lit = True
            if 1650.0 <= t <= 2900.0:
                self.assertIsNone(state["hold_light_states"][0])
        self.assertTrue(lit)


if __name__ == "__main__":
    unittest.main()
