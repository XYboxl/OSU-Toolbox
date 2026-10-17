import unittest

from osu_toolbox.video.ffmpeg import _draw_hit_effects


class _FakeRenderer:
    def __init__(self, separate_score):
        self.separate_score = separate_score
        self.calls = []

    def judgement_stage_indices(self, col):
        if not self.separate_score:
            return (0, 1)
        return (0 if col < 2 else 1,)

    def draw_hit_flash(self, *args):
        self.calls.append(args)
        return bool(args[3])


class SeparateScoreFrameTests(unittest.TestCase):
    def test_each_stage_keeps_its_latest_result(self):
        renderer = _FakeRenderer(separate_score=True)
        hits = [
            (3, 1.0, False, 10.0, 320),
            (0, 1.0, False, 20.0, 300),
            (2, 1.0, False, 30.0, 200),
        ]
        _draw_hit_effects(renderer, hits, 1000.0)
        self.assertEqual(
            [call[7] for call in renderer.calls],
            [(1,), (0,), ()],
        )

    def test_combined_score_broadcasts_only_the_latest_result(self):
        renderer = _FakeRenderer(separate_score=False)
        hits = [
            (3, 1.0, False, 10.0, 320),
            (0, 1.0, False, 20.0, 300),
        ]
        _draw_hit_effects(renderer, hits, 1000.0)
        self.assertEqual(
            [call[7] for call in renderer.calls],
            [(0, 1), ()],
        )


if __name__ == "__main__":
    unittest.main()
