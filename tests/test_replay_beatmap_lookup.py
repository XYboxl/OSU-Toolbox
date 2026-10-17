import hashlib
import tempfile
import unittest
from pathlib import Path


class ReplayBeatmapLookupTests(unittest.TestCase):
    @staticmethod
    def _md5(path):
        return hashlib.md5(path.read_bytes()).hexdigest()

    def test_mr_uses_sibling_malody_beatmap_directory(self):
        from osu_toolbox import find_beatmap_for_replay

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            replay = root / "replay" / "play.mr"
            chart = root / "beatmap" / "map.mc"
            replay.parent.mkdir()
            chart.parent.mkdir()
            replay.write_bytes(b"replay")
            chart.write_bytes(b"malody chart")
            matched = find_beatmap_for_replay(self._md5(chart), replay)
        self.assertEqual(matched, str(chart))

    def test_osr_uses_sibling_osu_songs_directory(self):
        from osu_toolbox import find_beatmap_for_replay

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            replay = root / "Replays" / "play.osr"
            chart = root / "Songs" / "set" / "map.osu"
            replay.parent.mkdir()
            chart.parent.mkdir(parents=True)
            replay.write_bytes(b"replay")
            chart.write_bytes(b"osu chart")
            matched = find_beatmap_for_replay(self._md5(chart), replay)
        self.assertEqual(matched, str(chart))

    def test_nonstandard_replay_directory_is_not_searched(self):
        from osu_toolbox import find_beatmap_for_replay

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            replay = root / "exports" / "play.mr"
            chart = root / "beatmap" / "map.mc"
            replay.parent.mkdir()
            chart.parent.mkdir()
            replay.write_bytes(b"replay")
            chart.write_bytes(b"chart")
            matched = find_beatmap_for_replay(self._md5(chart), replay)
        self.assertIsNone(matched)

    def test_configured_game_directory_guides_nonstandard_replay(self):
        from osu_toolbox import find_beatmap_for_replay

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            replay = root / "exports" / "play.mr"
            malody = root / "Malody"
            chart = malody / "beatmap" / "map.mc"
            replay.parent.mkdir()
            chart.parent.mkdir(parents=True)
            replay.write_bytes(b"replay")
            chart.write_bytes(b"chart")
            matched = find_beatmap_for_replay(
                self._md5(chart),
                replay,
                malody_directory=malody,
            )
        self.assertEqual(matched, str(chart))

    def test_extension_selects_format_and_unknown_extension_does_nothing(self):
        from osu_toolbox import find_beatmap_for_replay

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            malody = root / "Malody"
            osu = root / "osu!"
            mc = malody / "beatmap" / "map.mc"
            osu_map = osu / "Songs" / "map.osu"
            mc.parent.mkdir(parents=True)
            osu_map.parent.mkdir(parents=True)
            mc.write_bytes(b"same")
            osu_map.write_bytes(b"same")
            md5 = self._md5(mc)
            mr_match = find_beatmap_for_replay(
                md5, root / "play.mr",
                malody_directory=malody,
                osu_directory=osu,
            )
            osr_match = find_beatmap_for_replay(
                md5, root / "play.osr",
                malody_directory=malody,
                osu_directory=osu,
            )
            unknown = find_beatmap_for_replay(
                md5, root / "play.bin",
                malody_directory=malody,
                osu_directory=osu,
            )
        self.assertEqual(mr_match, str(mc))
        self.assertEqual(osr_match, str(osu_map))
        self.assertIsNone(unknown)

    def test_directory_constants_match_current_game_layouts(self):
        from osu_toolbox import (
            MALODY_BEATMAP_DIRNAME,
            MALODY_REPLAY_DIRNAME,
            OSU_BEATMAP_DIRNAME,
            OSU_REPLAY_DIRNAME,
        )

        self.assertEqual(MALODY_REPLAY_DIRNAME, "replay")
        self.assertEqual(MALODY_BEATMAP_DIRNAME, "beatmap")
        self.assertEqual(OSU_REPLAY_DIRNAME, "Replays")
        self.assertEqual(OSU_BEATMAP_DIRNAME, "Songs")


if __name__ == "__main__":
    unittest.main()
