"""回归测试：变速工具的命名、谱面时间轴缩放、音频重采样与右键/CLI 集成。"""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from osu_toolbox.speed_change import (  # noqa: E402
    SpeedChangeError,
    change_speed,
    find_ffmpeg,
    normalized_speed,
    plan_speed_change,
    transform_mc_text,
    transform_osu_text,
    transform_sm_text,
)
from osu_toolbox.speed_settings import (  # noqa: E402
    DEFAULT_SPEED_DIFFICULTY_MARKER,
    DEFAULT_SPEED_FILE_SUFFIX,
    apply_file_marker,
    format_difficulty_marker,
    format_file_marker,
    normalize_speed_settings,
)


OSU_TEMPLATE = (
    "osu file format v14\n"
    "\n"
    "[General]\n"
    "AudioFilename: {audio}\n"
    "AudioLeadIn: {lead_in}\n"
    "PreviewTime: {preview}\n"
    "Mode: 3\n"
    "\n"
    "[Metadata]\n"
    "Title:{title}\n"
    "Version:{version}\n"
    "\n"
    "[Difficulty]\n"
    "CircleSize:4\n"
    "\n"
    "[Events]\n"
    '0,0,"bg.jpg",0,0\n'
    "2,5000,10000\n"
    "\n"
    "[TimingPoints]\n"
    "0,500,4,1,0,100,1,0\n"
    "1000,-50,4,1,0,100,0,0\n"
    "\n"
    "[HitObjects]\n"
    "64,192,1000,1,0,0:0:0:0:\n"
    "128,192,2000,128,0,4000:0:0:0:0:\n"
    "192,192,3000,2,0,B|100:100|200:300,1,280\n"
)


def _osu_text(**overrides):
    values = {
        "audio": "song.wav",
        "lead_in": "1000",
        "preview": "20000",
        "title": "T",
        "version": "Hard",
    }
    values.update(overrides)
    return OSU_TEMPLATE.format(**values)


def _ffmpeg_path():
    """Return a usable FFmpeg path, or ``None`` when none is installed."""
    found = shutil.which("ffmpeg") or find_ffmpeg()
    return found if found and found != "ffmpeg" else None


FFMPEG = _ffmpeg_path()


class SpeedMarkerTests(unittest.TestCase):
    def test_default_marker_keeps_its_leading_space(self):
        self.assertEqual(DEFAULT_SPEED_FILE_SUFFIX, " {num}x")
        self.assertEqual(DEFAULT_SPEED_DIFFICULTY_MARKER, " {num}x")
        self.assertEqual(format_file_marker(" {num}x", 1.5), " 1.5x")
        self.assertEqual(format_difficulty_marker(" {num}x", 1.5), " 1.5x")

    def test_custom_marker_patterns(self):
        self.assertEqual(format_file_marker("_{num}x", 0.95), "_0.95x")
        self.assertEqual(format_file_marker("-{num}x", 2), "-2x")
        self.assertEqual(format_file_marker("[{num}]", 1.1), "[1.1]")
        # A pattern without the placeholder still gets the multiplier.
        self.assertEqual(format_file_marker("_speed", 1.5), "_speed1.5")

    def test_file_marker_strips_characters_windows_rejects(self):
        self.assertEqual(format_file_marker('a/b:{num}x', 1.5), "a_b_1.5x")
        # A difficulty name is free text, so only line breaks are removed.
        self.assertEqual(format_difficulty_marker("a/b:{num}", 1.5), "a/b:1.5")

    def test_apply_file_marker_targets_the_extension(self):
        self.assertEqual(apply_file_marker("C:/a/song.mp3", "_{num}x", 1.5), "C:/a/song_1.5x.mp3")
        self.assertEqual(apply_file_marker("song", "_{num}x", 1.5), "song_1.5x")
        self.assertEqual(apply_file_marker("a.b/song", "_{num}x", 1.5), "a.b/song_1.5x")

    def test_blank_and_missing_settings_fall_back_to_the_default(self):
        self.assertEqual(normalize_speed_settings({}), {
            "speed_keep_pitch": False,
            "speed_file_suffix": " {num}x",
            "speed_difficulty_marker": " {num}x",
        })
        # A whitespace-only pattern means "no marker" rather than a marker made
        # of spaces, and a missing key keeps the default including its leading
        # space.
        self.assertEqual(
            normalize_speed_settings({"speed_file_suffix": "   "})["speed_file_suffix"],
            "",
        )
        self.assertEqual(
            normalize_speed_settings({"speed_file_suffix": None})["speed_file_suffix"],
            "{num}x",
        )
        self.assertEqual(
            normalize_speed_settings("junk")["speed_file_suffix"], " {num}x"
        )

    def test_explicit_marker_is_preserved_verbatim(self):
        settings = normalize_speed_settings({"speed_file_suffix": "_{num}x"})
        self.assertEqual(settings["speed_file_suffix"], "_{num}x")
        self.assertEqual(settings["speed_difficulty_marker"], " {num}x")


class SpeedValidationTests(unittest.TestCase):
    def test_rejects_a_no_op_and_out_of_range_values(self):
        for value in (1.0, 0, -1, 0.001, 100, float("nan"), "abc", None):
            with self.subTest(value=value):
                with self.assertRaises(SpeedChangeError):
                    normalized_speed(value)

    def test_accepts_the_menu_presets(self):
        for value in (0.9, 0.95, 1.05, 1.1, 1.5, 2):
            with self.subTest(value=value):
                self.assertEqual(normalized_speed(value), value)


class OsuTimelineTests(unittest.TestCase):
    def test_speed_changed_chart_does_not_keep_the_original_online_ids(self):
        text = _osu_text().replace(
            "Version:Hard", "Version:Hard\nBeatmapID: 123456\nBeatmapSetID: 654321"
        )
        for speed in (0.9, 1.5):
            with self.subTest(speed=speed):
                scaled, _info = transform_osu_text(text, speed)
                self.assertIn("BeatmapID: 0\n", scaled)
                self.assertIn("BeatmapSetID: -1\n", scaled)
                self.assertNotIn("123456", scaled)
                self.assertNotIn("654321", scaled)

    def test_every_absolute_time_shrinks_by_the_multiplier(self):
        """Times shrink with the music, which is resampled to 1/speed of its length.

        A 1.5x speed change leaves the track two thirds as long, so a note that
        sat at 1000 ms has to sit at 667 ms to stay on the same beat.  Every
        absolute time is written as a whole millisecond: a fractional one made
        the toolbox's own readers reject the whole chart.
        """
        scaled, info = transform_osu_text(
            _osu_text(), 1.5, difficulty_marker=" 1.5x"
        )
        self.assertEqual(info["version"], "Hard 1.5x")
        lines = scaled.splitlines()
        self.assertIn("Version:Hard 1.5x", lines)
        self.assertIn("AudioLeadIn: 667", lines)
        self.assertIn("PreviewTime: 13333", lines)
        # Break events carry two absolute timestamps.
        self.assertIn("2,3333,6667", lines)
        # The background event has no time and must stay untouched.
        self.assertIn('0,0,"bg.jpg",0,0', lines)
        # The beat grid shortens with the timeline, so notes keep their beats.
        # Only the beat length itself stays fractional: it is a duration, not a
        # timestamp.
        self.assertIn("0,333.3333,4,1,0,100,1,0", lines)
        # An inherited (negative) point keeps its velocity multiplier.
        self.assertIn("667,-50,4,1,0,100,0,0", lines)
        self.assertIn("64,192,667,1,0,0:0:0:0:", lines)
        # A mania hold's tail time is the leading token of the fifth field.
        self.assertIn("128,192,1333,128,0,2667:0:0:0:0:", lines)
        # A slider's curve, repeat count and pixel length are geometry, not
        # time, and the game derives each edge's time from the curve and the
        # beat length: the whole tail is preserved byte for byte.
        self.assertIn("192,192,2000,2,0,B|100:100|200:300,1,280", lines)

    def test_the_written_chart_is_whole_milliseconds_throughout(self):
        """Every timestamp field is an integer, as the format defines it.

        A reader that parses these fields as integers used to fail on the whole
        file, so a speed-changed chart could not be loaded at all.
        """
        scaled, _info = transform_osu_text(_osu_text(), 1.05)
        section = None
        for line in scaled.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped
                continue
            if not stripped or stripped.startswith("//"):
                continue
            with self.subTest(section=section, line=stripped):
                if section == "[HitObjects]":
                    for index in (2,):
                        self.assertNotIn(".", stripped.split(",")[index])
                    fifth = stripped.split(",")[5]
                    if fifth:
                        self.assertNotIn(".", fifth.split(":")[0])
                elif section == "[TimingPoints]":
                    self.assertNotIn(".", stripped.split(",")[0])
                elif section == "[Events]" and stripped[:1] in ("1", "2"):
                    for part in stripped.split(",")[1:3]:
                        if part:
                            self.assertNotIn(".", part)
                elif section == "[General]" and stripped.startswith(
                    ("AudioLeadIn", "PreviewTime")
                ):
                    self.assertNotIn(".", stripped.split(":", 1)[1])

    def test_the_toolbox_can_read_the_speed_changed_chart_back(self):
        """The written chart must load through the shared reader, notes and all.

        The exact quotient (``667.3333``) is not a millisecond value any reader
        accepts, so a speed-changed chart could not be loaded at all -- every
        note of it was lost to the render and replay tools.
        """
        from osu_toolbox.parsing.osu_file import parse_osu_file

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.osu"
            source.write_text(_osu_text(), encoding="utf-8")
            original = parse_osu_file(source)
            self.assertTrue(original.hit_objects)
            for speed in (1.5, 1.1, 1.05, 0.9):
                with self.subTest(speed=speed):
                    scaled, _info = transform_osu_text(_osu_text(), speed)
                    marked = root / f"song {speed}.osu"
                    marked.write_text(scaled, encoding="utf-8")
                    parsed = parse_osu_file(marked)
                    self.assertEqual(
                        len(parsed.hit_objects), len(original.hit_objects)
                    )
                    for before, after in zip(original.hit_objects, parsed.hit_objects):
                        self.assertEqual(before.col, after.col)
                        self.assertEqual(before.is_hold, after.is_hold)
                        self.assertAlmostEqual(before.time / speed, after.time, delta=1.0)

    def test_every_note_keeps_its_beat_position(self):
        """The invariant a speed change must hold: notes stay on their beats.

        A note's beat coordinate is its time on the timing grid.  The chart and
        the music are scaled by the same ratio, so a note played on beat 2 of the
        original is played on beat 2 of the speed-changed chart -- and the whole
        timeline shrinks to 1/speed, exactly like the audio.  Rounding each time
        to a whole millisecond leaves at most half a millisecond of drift.
        """
        source = _osu_text()
        scaled, _info = transform_osu_text(source, 1.5, difficulty_marker="")

        def notes_and_grid(text):
            section = None
            grid = []
            notes = []
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped[1:-1]
                elif stripped and section == "TimingPoints":
                    parts = stripped.split(",")
                    if len(parts) >= 2 and float(parts[1]) > 0:
                        grid.append((float(parts[0]), float(parts[1])))
                elif stripped and section == "HitObjects":
                    parts = stripped.split(",")
                    notes.append(float(parts[2]))
            return grid, notes

        before_grid, before_notes = notes_and_grid(source)
        after_grid, after_notes = notes_and_grid(scaled)
        self.assertEqual(len(before_notes), len(after_notes))
        # The grid's origin and its beat length shrink by the same ratio, which
        # is what keeps the beat numbers below identical.  Compared by
        # multiplication because a chart may legitimately start at 0 ms.
        self.assertAlmostEqual(before_grid[0][0], after_grid[0][0] * 1.5, delta=1.0)
        self.assertAlmostEqual(before_grid[0][1], after_grid[0][1] * 1.5, delta=0.01)
        origin, beat_length = before_grid[0]
        new_origin, new_length = after_grid[0]
        for before, after in zip(before_notes, after_notes):
            with self.subTest(note=before):
                self.assertAlmostEqual(
                    (before - origin) / beat_length,
                    (after - new_origin) / new_length,
                    delta=0.01,
                )
                # …and the timeline itself really did shrink.
                self.assertAlmostEqual(before, after * 1.5, delta=1.0)

    _EVENTS = (
        "osu file format v14\n"
        "\n"
        "[General]\n"
        "AudioFilename: song.mp3\n"
        "Mode: 3\n"
        "\n"
        "[Metadata]\n"
        "Title:T\n"
        "Version:Hard\n"
        "\n"
        "[Events]\n"
        '0,0,"bg.jpg",0,0\n'
        '1,2000,"video.mp4"\n'
        "2,5000,10000\n"
        'Sprite,Background,Centre,"sb.png",320,240\n'
        " F,0,5000,10000,0,1\n"
        " L,5000,3\n"
        "  M,0,5000,10000,0,0,320,240\n"
        " T,hit,5000,10000,0\n"
        'Sample,5000,0,"clap.wav",100\n'
        "\n"
        "[TimingPoints]\n"
        "0,500,4,1,0,100,1,0\n"
        "\n"
        "[HitObjects]\n"
        "64,192,5000,1,0,0:0:0:0:\n"
    )

    def test_event_lines_only_scale_real_timestamps(self):
        """Only time fields move in ``[Events]``.

        A video's file name, a command's easing index, a loop's repeat count and
        a trigger's name are not times, and the leading spaces of a command nest
        it inside the sprite, loop or trigger above it: rewriting either loses
        the video or breaks the storyboard.  Every scaled time is a whole
        millisecond, like the rest of the file.
        """
        scaled, _info = transform_osu_text(self._EVENTS, 1.5)
        lines = scaled.splitlines()
        # The background event carries no time at all.
        self.assertIn('0,0,"bg.jpg",0,0', lines)
        # A video's start time scales and rounds; its file name must survive.
        self.assertIn('1,1333,"video.mp4"', lines)
        # A break carries two timestamps.
        self.assertIn("2,3333,6667", lines)
        # A sprite line is geometry only.
        self.assertIn('Sprite,Background,Centre,"sb.png",320,240', lines)
        # A command is ``letter,easing,start,end``; the easing index is not a time.
        self.assertIn(" F,0,3333,6667,0,1", lines)
        # A loop's repeat count must not move.
        self.assertIn(" L,3333,3", lines)
        # The indentation of a nested command is part of its meaning.
        self.assertIn("  M,0,3333,6667,0,0,320,240", lines)
        # A trigger's name and group number are not times.
        self.assertIn(" T,hit,3333,6667,0", lines)
        # A storyboard sample's time scales; its layer and name do not.
        self.assertIn('Sample,3333,0,"clap.wav",100', lines)

    def test_untouched_sections_are_byte_identical(self):
        source = _osu_text()
        scaled, _info = transform_osu_text(source, 1.5)
        # ``splitlines`` keeps the trailing empty line of the file's final
        # newline, so the transform's trailing newline is normalised away on
        # both sides before comparing line by line.
        source_lines = source.rstrip("\n").splitlines()
        scaled_lines = scaled.rstrip("\n").splitlines()
        self.assertEqual(len(source_lines), len(scaled_lines))
        for expected, actual in zip(source_lines, scaled_lines):
            # Hit objects only change their own timestamps; the slider's tail is
            # covered by ``test_every_absolute_time_shrinks_by_the_multiplier``.
            if expected.startswith(("64,192", "128,192", "192,192")):
                continue
            if expected.startswith(("0,500", "1000,-50")):
                continue
            if expected in ("AudioLeadIn: 1000", "PreviewTime: 20000", "2,5000,10000"):
                continue
            self.assertEqual(expected, actual)

    def test_an_empty_description_in_notes_is_left_alone(self):
        source = "#NOTES::\n\n\n:\n:\n:\n1000\n0000\n;\n"
        scaled, _info = transform_sm_text(source, 1.5, difficulty_marker=" 1.5x")
        self.assertEqual(scaled, source)

    def test_crlf_and_bom_are_preserved(self):
        source = _osu_text().replace("\n", "\r\n")
        scaled, _info = transform_osu_text(source, 1.5)
        # Every line break stays a CRLF pair; the transform must not mix in a
        # bare LF anywhere.
        self.assertNotIn("\n", scaled.replace("\r\n", ""))
        self.assertIn("AudioLeadIn: 667", scaled)

    def test_the_file_s_structure_is_reproduced_exactly(self):
        """Only time fields change: same lines, same ending, no blank line added.

        Splitting kept the final break as an empty last line and then restored it
        again, so every run appended one more blank line to the file.
        """
        for source in (_osu_text(), _osu_text().replace("\n", "\r\n")):
            with self.subTest(ending=repr(source[-2:])):
                scaled, _info = transform_osu_text(source, 1.5)
                self.assertEqual(len(scaled.splitlines()), len(source.splitlines()))
                self.assertTrue(scaled.endswith(source[-2:]))
        without_final_break = _osu_text().rstrip("\n")
        scaled, _info = transform_osu_text(without_final_break, 1.5)
        self.assertFalse(scaled.endswith("\n"))

    def test_a_chart_without_the_marker_setting_is_renamed_only(self):
        scaled, info = transform_osu_text(_osu_text(), 1.5, difficulty_marker="")
        self.assertEqual(info["version"], "Hard")
        self.assertIn("Version:Hard", scaled.splitlines())

    def test_the_song_name_is_marked_beside_the_difficulty_name(self):
        """``Title``/``TitleUnicode`` are the song name and carry the marker too.

        ``Version`` is the difficulty name; the song name lives in
        ``Title``/``TitleUnicode`` and must say the file was speed-changed as well.
        """
        scaled, _info = transform_osu_text(
            _osu_text(), 1.5, difficulty_marker=" 1.5x"
        )
        lines = scaled.splitlines()
        self.assertIn("Title:T 1.5x", lines)
        self.assertIn("Version:Hard 1.5x", lines)

    def test_titleunicode_is_marked_when_present(self):
        source = _osu_text() + ""  # the template carries Title only
        source = source.replace("Title:T\n", "Title:T\nTitleUnicode:歌\n")
        scaled, _info = transform_osu_text(source, 1.5, difficulty_marker=" 1.5x")
        lines = scaled.splitlines()
        self.assertIn("Title:T 1.5x", lines)
        self.assertIn("TitleUnicode:歌 1.5x", lines)

    def test_the_song_name_marker_can_be_set_on_its_own(self):
        scaled, _info = transform_osu_text(
            _osu_text(), 1.5, difficulty_marker="", song_marker=" 1.5x"
        )
        lines = scaled.splitlines()
        self.assertIn("Title:T 1.5x", lines)
        # The difficulty name is untouched when only the song marker is asked for.
        self.assertIn("Version:Hard", lines)

    def test_preview_time_of_minus_one_is_kept(self):
        scaled, _info = transform_osu_text(_osu_text(preview="-1"), 2)
        self.assertIn("PreviewTime: -1", scaled.splitlines())


class MalodyTimelineTests(unittest.TestCase):
    def _document(self, **meta_overrides):
        meta = {
            "mode": 0,
            "mode_ext": {"column": 4},
            "version": "Hard",
            "song": {"title": "T", "file": "song.mp3"},
        }
        meta.update(meta_overrides)
        return json.dumps({
            "meta": meta,
            "time": [{"beat": [0, 0, 1], "bpm": 120}, {"beat": [16, 0, 1], "bpm": 180}],
            "note": [
                {"beat": [4, 0, 1], "column": 0},
                {"beat": [8, 0, 1], "column": 1, "endbeat": [12, 0, 1]},
                {"beat": [0, 0, 1], "sound": "song.mp3", "offset": 150, "type": 1},
            ],
        })

    def test_bpm_and_offsets_scale_while_beats_stay(self):
        scaled, info = transform_mc_text(self._document(), 1.5, difficulty_marker=" 1.5x")
        root = json.loads(scaled)
        self.assertEqual([item["bpm"] for item in root["time"]], [180.0, 270.0])
        self.assertEqual(info["version"], "Hard 1.5x")
        self.assertEqual(root["meta"]["version"], "Hard 1.5x")
        self.assertEqual(root["meta"]["song"]["file"], "song.mp3")
        # Beats are relative to the scaled BPM and must not move.
        self.assertEqual(root["note"][0]["beat"], [4, 0, 1])
        self.assertEqual(root["note"][1]["endbeat"], [12, 0, 1])
        # Malody stores the audio offset as an integer.
        self.assertEqual(root["note"][2]["offset"], 100)

    def test_the_song_title_is_marked_and_titleorg_is_left_alone(self):
        """``meta.song.title`` is the song name; ``version`` is the difficulty.

        ``titleorg`` keeps the untouched original the format stores for reference,
        so marking it would destroy the only copy of the authored title.
        """
        document = json.dumps({
            "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": "Hard",
                     "song": {"title": "T", "titleorg": "T", "artist": "A"}},
            "time": [{"beat": [0, 0, 1], "bpm": 120}],
            "note": [],
        })
        scaled, info = transform_mc_text(document, 1.5, difficulty_marker=" 1.5x")
        song = json.loads(scaled)["meta"]["song"]
        self.assertEqual(song["title"], "T 1.5x")
        self.assertEqual(song["titleorg"], "T")
        self.assertEqual(json.loads(scaled)["meta"]["version"], "Hard 1.5x")
        self.assertEqual(info["song_title"], "T")

    def test_millisecond_note_times_shrink_with_the_music(self):
        """A millisecond note time is a position in the track, so it shrinks too.

        Malody also stores notes on beats, and those must not move; a note that
        carries its own millisecond ``time`` is an absolute position in the
        audio, so it scales like the audio offset and the note keeps its place.
        The field stays a whole millisecond, so the value is written back as an
        integer rather than as a long fraction.
        """
        document = json.dumps({
            "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "a.mp3"}},
            "time": [{"beat": [0, 0, 1], "bpm": 120}],
            "note": [{"time": 1000, "column": 0, "end_time": 2001}],
        })
        scaled, _info = transform_mc_text(document, 2)
        note = json.loads(scaled)["note"][0]
        self.assertEqual(note["time"], 500)
        self.assertEqual(note["end_time"], 1000)
        self.assertIsInstance(note["time"], int)

    def test_the_audio_note_stays_last_and_its_offset_scales(self):
        """Malody's BGM entry is the trailing note and carries the alignment.

        The offset is the chart/audio alignment in milliseconds, so scaling the
        tempo scales it by ``1 / speed``: the chart's beat-zero still lines up
        with the same moment of the now shorter track.  Measured on real charts,
        a 1.5x change turns 354 ms into 236 ms and 197 ms into 131 ms.
        """
        for original, speed, expected in (
            (354, 1.5, 236),
            (197, 1.5, 131),
            (204, 1.5, 136),
            (-329.478, 1.5, -220),
            (500, 2.0, 250),
            (0, 1.5, 0),
        ):
            with self.subTest(offset=original, speed=speed):
                document = json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4},
                             "song": {"title": "T", "file": "a.ogg"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 150}],
                    "note": [
                        {"beat": [4, 0, 1], "column": 0},
                        {"beat": [8, 0, 1], "column": 1, "endbeat": [12, 0, 1]},
                        {"beat": [0, 0, 1], "sound": "a.ogg", "offset": original,
                         "vol": 100, "type": 1},
                    ],
                })
                scaled, _info = transform_mc_text(document, speed)
                notes = json.loads(scaled)["note"]
                # The audio entry is still the last element, still an integer.
                self.assertEqual(notes[-1].get("type"), 1)
                self.assertEqual(notes[-1]["sound"], "a.ogg")
                self.assertIsInstance(notes[-1]["offset"], int)
                self.assertEqual(notes[-1]["offset"], expected)
                # Nothing was added or dropped.
                self.assertEqual(len(notes), 3)
                self.assertEqual(sum(1 for n in notes if n.get("type") == 1), 1)

    def test_the_audio_note_survives_a_chart_without_other_notes(self):
        document = json.dumps({
            "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "a.ogg"}},
            "time": [{"beat": [0, 0, 1], "bpm": 120}],
            "note": [{"beat": [0, 0, 1], "sound": "a.ogg", "offset": 100,
                      "vol": 100, "type": 1}],
        })
        scaled, info = transform_mc_text(document, 1.5)
        notes = json.loads(scaled)["note"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["offset"], 67)
        self.assertEqual(info["audio_filename"], "a.ogg")


class StepManiaTimelineTests(unittest.TestCase):
    _SM = (
        "#TITLE:Test;\n"
        "#MUSIC:song.ogg;\n"
        "#OFFSET:-0.150000;\n"
        "#BPMS:0.000000=120.000000,16.000000=180.000000;\n"
        "#STOPS:8.000000=0.500000;\n"
        "#DELAYS:4.000000=0.250000;\n"
        "#NOTES:\n"
        "     dance-single:\n"
        "     Hard:\n"
        "     Challenge:\n"
        "     10:\n"
        "     0.000,0.000,0.000,0.000,0.000:\n"
        "1000\n"
        "0000\n"
        ";\n"
    )

    def test_bpm_stops_and_offset_scale(self):
        scaled, info = transform_sm_text(self._SM, 1.5, difficulty_marker=" 1.5x")
        self.assertIn("#BPMS:0.000000=180.000000,16.000000=270.000000;", scaled)
        self.assertIn("#STOPS:8.000000=0.333333;", scaled)
        self.assertIn("#DELAYS:4.000000=0.166667;", scaled)
        self.assertIn("#OFFSET:-0.100000;", scaled)
        # StepMania has no per-file version field, so the marker names the
        # difficulty inside #NOTES and the note data stays untouched.
        self.assertIn("     Hard 1.5x:", scaled)
        self.assertIn("1000\n0000", scaled)
        self.assertEqual(info["audio_filename"], "song.ogg")

    def test_the_song_title_is_marked_beside_the_difficulty(self):
        """``#TITLE`` is the song name and carries the marker too.

        ``#NOTES`` description is the difficulty name; ``#TITLE`` names the song,
        so a speed-changed file says so in both places.
        """
        scaled, _info = transform_sm_text(self._SM, 1.5, difficulty_marker=" 1.5x")
        self.assertIn("#TITLE:Test 1.5x;", scaled)
        self.assertIn("     Hard 1.5x:", scaled)

    def test_chart_without_marker_keeps_its_difficulty_description(self):
        scaled, _info = transform_sm_text(self._SM, 2)
        self.assertIn("     Hard:", scaled)


class NamingPlanTests(unittest.TestCase):
    def test_plan_names_every_output_with_the_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            (root / "song.wav").write_bytes(b"audio")
            plan = plan_speed_change(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(plan[0]["outputs"], [str(root / "song 1.5x.osu")])
            rows = dict(plan[0]["rows"])
            self.assertEqual(Path(rows["音乐"]).name, "song 1.5x.wav")

    def test_plan_uses_the_configured_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            plan = plan_speed_change(
                [chart], 1.1,
                file_pattern="_{num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(plan[0]["outputs"], [str(root / "song_1.1x.osu")])

    def test_plan_finds_the_music_a_numbered_storage_level_points_at(self):
        """``Song/0/chart.mc`` names ``../audio.mp3``; the plan must list it.

        Malody keeps a chart set one level below its track, so the search has to
        reach the song folder.  Reading only the chart's own folder planned no
        music at all while the chart itself was still rewritten, which produced a
        chart that could never line up with its music.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            song = root / "Song"
            (song / "0").mkdir(parents=True)
            (song / "audio.mp3").write_bytes(b"audio")
            (song / "0" / "chart.mc").write_text(
                json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": "Hard",
                             "song": {"title": "T", "file": "../audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 120}],
                    "note": [{"beat": [4, 0, 1], "column": 0}],
                }),
                encoding="utf-8",
            )
            plan = plan_speed_change(
                [song / "0" / "chart.mc"], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            rows = dict(plan[0]["rows"])
            self.assertEqual(Path(rows["音乐"]), song / "audio 1.5x.mp3")


@unittest.skipUnless(FFMPEG, "需要 FFmpeg")
class AudioResampleTests(unittest.TestCase):
    """The audio path is verified against a real FFmpeg run."""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG

    def _make_audio(self, path, seconds=2.0, frequency=440):
        command = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={seconds}",
            "-ar", "44100", "-ac", "1", str(path),
        ]
        completed = subprocess.run(command, capture_output=True)
        if completed.returncode != 0 or not Path(path).is_file():
            self.skipTest("FFmpeg 无法生成测试音频")

    def _duration_ms(self, path):
        completed = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True,
        )
        for line in completed.stderr.splitlines():
            if "Duration:" in line:
                stamp = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
                hours, minutes, seconds = stamp.split(":")
                return (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000.0
        self.fail(f"无法读取音频时长：{path}")

    def _dominant_frequency(self, path):
        completed = subprocess.run(
            [
                self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path),
                "-f", "f32le", "-ac", "1", "-ar", "44100", "-",
            ],
            capture_output=True,
        )
        import numpy as np

        samples = np.frombuffer(completed.stdout, dtype="<f4")
        size = 1 << 14
        self.assertGreater(samples.size, size)
        window = samples[:size] * np.hanning(size)
        spectrum = np.abs(np.fft.rfft(window))
        return float(np.fft.rfftfreq(size, 1 / 44100.0)[int(np.argmax(spectrum))])

    def test_speed_change_shortens_the_audio_and_raises_its_pitch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.wav"
            self._make_audio(source, seconds=2.0, frequency=440)
            destination = root / "song 1.5x.wav"
            from osu_toolbox.speed_change import resample_audio

            resample_audio(source, destination, 1.5, ffmpeg=self.ffmpeg)
            self.assertTrue(destination.is_file())
            duration = self._duration_ms(destination)
            self.assertAlmostEqual(duration, 2000.0 / 1.5, delta=120.0)
            # ``asetrate`` changes the pitch with the tempo, so 440 Hz becomes
            # roughly 660 Hz instead of staying at 440 Hz.
            self.assertAlmostEqual(self._dominant_frequency(destination), 660.0, delta=25.0)

    def test_slow_down_lengthens_the_audio_and_lowers_its_pitch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.wav"
            self._make_audio(source, seconds=2.0, frequency=440)
            destination = root / "song 0.9x.wav"
            from osu_toolbox.speed_change import resample_audio

            resample_audio(source, destination, 0.9, ffmpeg=self.ffmpeg)
            self.assertAlmostEqual(self._duration_ms(destination), 2000.0 / 0.9, delta=150.0)
            self.assertAlmostEqual(self._dominant_frequency(destination), 396.0, delta=20.0)


@unittest.skipUnless(FFMPEG, "需要 FFmpeg")
class SpeedChangeFileTests(unittest.TestCase):
    """Single-file and archive runs, end to end through the public API."""

    def _write_song(self, root, name="song", audio="song.wav"):
        chart = root / f"{name}.osu"
        chart.write_text(_osu_text(audio=audio), encoding="utf-8")
        audio_path = root / audio
        audio_path.write_bytes(b"placeholder")
        return chart, audio_path

    def _make_audio(self, path, seconds=1.0):
        completed = subprocess.run(
            [
                FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-ar", "44100", "-ac", "1", str(path),
            ],
            capture_output=True,
        )
        if completed.returncode != 0:
            self.skipTest("FFmpeg 无法生成测试音频")

    def test_single_chart_produces_a_marked_chart_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart, audio = self._write_song(root)
            original = chart.read_text(encoding="utf-8").replace(
                "Version:Hard", "Version:Hard\nBeatmapID:123456\nBeatmapSetID:654321"
            )
            chart.write_text(original, encoding="utf-8")
            self._make_audio(audio)
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            marked_chart = root / "song 1.5x.osu"
            marked_audio = root / "song 1.5x.wav"
            self.assertTrue(marked_chart.is_file())
            self.assertTrue(marked_audio.is_file())
            # The source is untouched.
            self.assertEqual(chart.read_text(encoding="utf-8"), original)
            text = marked_chart.read_text(encoding="utf-8")
            self.assertIn("AudioFilename: song 1.5x.wav", text)
            self.assertIn("Version:Hard 1.5x", text)
            self.assertIn("AudioLeadIn: 667", text)
            self.assertIn("BeatmapID:0", text.splitlines())
            self.assertIn("BeatmapSetID:-1", text.splitlines())
            # A chart named directly keeps the song name it was authored with:
            # only the file name and the difficulty name say it changed.
            self.assertIn("Title:T\n", text)
            self.assertNotIn("Title:T 1.5x", text)

    def test_a_chart_from_a_selected_folder_marks_the_song_name(self):
        """A folder input marks the song name; a file input does not.

        Every chart the selected folder holds is one song of a collection, so
        its song name says it was speed-changed too.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "Song"
            folder.mkdir()
            chart, audio = self._write_song(folder)
            self._make_audio(audio)
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
                folders_selected=[folder],
            )
            self.assertEqual(outcome["failures"], [])
            text = (folder / "song 1.5x.osu").read_text(encoding="utf-8")
            self.assertIn("Title:T 1.5x", text)
            self.assertIn("Version:Hard 1.5x", text)

    def test_a_chart_naming_its_music_one_level_up_takes_the_music_along(self):
        """A Malody chart set is speed-changed together with its track.

        ``Song/0/chart.mc`` names ``../audio.mp3``.  Rewriting the chart while
        leaving the track at its old tempo is a chart that can never line up, so
        the music has to be found one level up, transcoded beside itself and
        referenced by its new name.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            song = root / "Song"
            (song / "0").mkdir(parents=True)
            self._make_audio(song / "audio.mp3")
            (song / "0" / "chart.mc").write_text(
                json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": "Hard",
                             "song": {"title": "T", "file": "../audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 120}],
                    "note": [{"beat": [4, 0, 1], "column": 0}],
                }),
                encoding="utf-8",
            )
            outcome = change_speed(
                [song / "0" / "chart.mc"], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            self.assertTrue((song / "audio 1.5x.mp3").is_file())
            payload = json.loads(
                (song / "0" / "chart 1.5x.mc").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["meta"]["song"]["file"], "../audio 1.5x.mp3")
            self.assertEqual(payload["time"][0]["bpm"], 180.0)

    def test_marker_is_renamed_but_not_applied_when_it_is_switched_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart, audio = self._write_song(root)
            self._make_audio(audio)
            outcome = change_speed(
                [chart], 1.5,
                file_pattern="_{num}x",
                difficulty_pattern="",
                difficulty_marker=False,
            )
            self.assertEqual(outcome["failures"], [])
            text = (root / "song_1.5x.osu").read_text(encoding="utf-8")
            # The file name always carries the marker; only the difficulty name
            # is affected by the difficulty-marker switch.
            self.assertIn("Version:Hard\n", text)
            self.assertIn("AudioFilename: song_1.5x.wav", text)

    def test_an_empty_difficulty_pattern_leaves_the_name_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart, audio = self._write_song(root)
            self._make_audio(audio)
            outcome = change_speed(
                [chart], 1.5,
                file_pattern="_{num}x",
                difficulty_pattern="",
            )
            self.assertEqual(outcome["failures"], [])
            self.assertIn(
                "Version:Hard\n", (root / "song_1.5x.osu").read_text(encoding="utf-8")
            )

    def test_parallel_run_changes_every_selected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b", "c", "d"):
                chart, audio = self._write_song(root, name=name, audio=f"{name}.wav")
                self._make_audio(audio)
            outcome = change_speed(
                [root / f"{name}.osu" for name in ("a", "b", "c", "d")], 1.1,
                file_pattern="_{num}x",
                difficulty_pattern="_{num}x",
            )
            self.assertEqual(outcome["failures"], [])
            self.assertEqual(len(outcome["entries"]), 4)
            for name in ("a", "b", "c", "d"):
                self.assertTrue((root / f"{name}_1.1x.osu").is_file())
                self.assertTrue((root / f"{name}_1.1x.wav").is_file())

    def test_shared_audio_is_resampled_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "shared.wav"
            self._make_audio(shared)
            for name in ("easy", "hard"):
                (root / f"{name}.osu").write_text(
                    _osu_text(audio="shared.wav", version=name), encoding="utf-8"
                )
            calls = []
            import osu_toolbox.speed_change as speed_change_module

            original = speed_change_module.resample_audio

            def counting(source, destination, speed, **kwargs):
                calls.append((str(source), str(destination)))
                return original(source, destination, speed, **kwargs)

            speed_change_module.resample_audio = counting
            try:
                outcome = change_speed(
                    [root / "easy.osu", root / "hard.osu"], 1.5,
                    file_pattern=" {num}x",
                    difficulty_pattern=" {num}x",
                )
            finally:
                speed_change_module.resample_audio = original
            self.assertEqual(outcome["failures"], [])
            self.assertEqual(len(calls), 1, calls)
            self.assertEqual(Path(calls[0][1]).parent, root)
            self.assertEqual(Path(calls[0][1]).suffix, ".wav")
            self.assertTrue((root / "shared 1.5x.wav").is_file())

    def test_archive_produces_one_new_archive_and_leaves_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "song.wav"
            self._make_audio(audio, seconds=1.0)
            source = root / "pack.osz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("easy.osu", _osu_text(version="Easy"))
                archive.writestr("hard.osu", _osu_text(version="Hard"))
                archive.write(audio, "song.wav")
                archive.writestr("bg.jpg", b"image")
                archive.writestr("readme.txt", b"readme")
            before = source.read_bytes()
            outcome = change_speed(
                [source], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            marked = root / "pack 1.5x.osz"
            self.assertTrue(marked.is_file())
            # The source archive is not modified and the members are not added
            # to it.
            self.assertEqual(source.read_bytes(), before)
            with zipfile.ZipFile(marked) as archive:
                names = sorted(archive.namelist())
                self.assertEqual(
                    names,
                    [
                        "bg.jpg", "easy 1.5x.osu", "hard 1.5x.osu",
                        "readme.txt", "song 1.5x.wav",
                    ],
                )
                chart = archive.read("easy 1.5x.osu").decode("utf-8")
                self.assertIn("AudioFilename: song 1.5x.wav", chart)
                self.assertIn("Version:Easy 1.5x", chart)
                self.assertIn("AudioLeadIn: 667", chart)
                # An archive is a pack of songs, so its members mark the song
                # name as well -- unlike a plain chart named on its own.
                self.assertIn("Title:T 1.5x", chart)
                # Resources that are neither a chart nor its music travel along.
                self.assertEqual(archive.read("readme.txt"), b"readme")
                self.assertEqual(archive.read("bg.jpg"), b"image")

    def test_archive_marks_member_names_and_metadata_with_the_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pack.mcz"
            document = json.dumps({
                "meta": {
                    "mode": 0, "mode_ext": {"column": 4}, "version": "Hard",
                    "song": {"title": "T", "file": "song.mp3"},
                },
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [4, 0, 1], "column": 0}],
            })
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("song.mc", document)
            outcome = change_speed(
                [source], 1.5,
                file_pattern="_{num}x",
                difficulty_pattern="",
                difficulty_marker=False,
            )
            self.assertEqual(outcome["failures"], [])
            with zipfile.ZipFile(root / "pack_1.5x.mcz") as archive:
                names = archive.namelist()
                self.assertEqual(names, ["song_1.5x.mc"])
                payload = json.loads(archive.read("song_1.5x.mc"))
            # Archives always mark both metadata fields, using the container marker as fallback.
            self.assertEqual(payload["meta"]["version"], "Hard_1.5x")
            self.assertEqual(payload["meta"]["song"]["title"], "T_1.5x")
            self.assertEqual(payload["time"][0]["bpm"], 180.0)

    def test_a_missing_audio_reference_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(audio="absent.mp3"), encoding="utf-8")
            warnings = []
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
                emit_event=lambda kind, payload: warnings.append((kind, payload)),
            )
            self.assertEqual(outcome["failures"], [])
            self.assertTrue((root / "song 1.5x.osu").is_file())
            self.assertIn(
                "AudioFilename: absent.mp3",
                (root / "song 1.5x.osu").read_text(encoding="utf-8"),
            )
            self.assertTrue(any(kind == "warning" for kind, _ in warnings))


@unittest.skipUnless(FFMPEG, "需要 FFmpeg")
class StoryboardNoticeTests(unittest.TestCase):
    """A standalone ``.osb`` is reported because only ``.osu`` [Events] scale."""

    def _audio(self, path):
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ar", "44100", "-ac", "1", str(path),
            ],
            capture_output=True,
        )

    def test_single_chart_reports_its_companion_osb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            self._audio(root / "song.wav")
            (root / "song.osb").write_text(
                "[Events]\n F,0,5000,1,0,0,0,0,1\n", encoding="utf-8"
            )
            warnings = []
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
                emit_event=lambda kind, payload: warnings.append((kind, str(payload))),
            )
            self.assertEqual(outcome["failures"], [])
            self.assertEqual(
                [asset["name"] for asset in outcome["entries"][0]["assets"]],
                ["song.osb"],
            )
            self.assertTrue(
                any("song.osb" in text and ".osb" in text for _kind, text in warnings),
                warnings,
            )
            # The storyboard is reported, not rewritten or renamed.
            self.assertTrue((root / "song.osb").is_file())
            self.assertFalse((root / "song 1.5x.osb").exists())

    def test_an_unrelated_osb_next_to_the_chart_is_not_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            self._audio(root / "song.wav")
            (root / "other.osb").write_text("[Events]\n", encoding="utf-8")
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["entries"][0]["assets"], [])

    def test_archive_reports_matching_osb_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "song.wav"
            self._audio(audio)
            source = root / "pack.osz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("song.osu", _osu_text())
                archive.write(audio, "song.wav")
                archive.writestr("song.osb", "[Events]\n F,0,5000,1,0,0,0,0,1\n")
                archive.writestr("unrelated.osb", "[Events]\n")
            warnings = []
            outcome = change_speed(
                [source], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
                emit_event=lambda kind, payload: warnings.append((kind, str(payload))),
            )
            self.assertEqual(outcome["failures"], [])
            self.assertEqual(
                [asset["name"] for asset in outcome["entries"][0]["assets"]],
                ["song.osb"],
            )
            with zipfile.ZipFile(root / "pack 1.5x.osz") as archive:
                self.assertIn("song.osb", archive.namelist())

    def test_the_plan_lists_the_storyboard_in_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            (root / "song.wav").write_bytes(b"audio")
            (root / "song.osb").write_text("[Events]\n", encoding="utf-8")
            plan = plan_speed_change(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(
                [label for label, _value in plan[0]["rows"]], ["谱面", "音乐", "故事板"]
            )

    def test_tool_prepare_warns_and_result_reports_the_storyboard(self):
        from osu_toolbox.speed_change import STORYBOARD_WARNING
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            self._audio(root / "song.wav")
            (root / "song.osb").write_text("[Events]\n", encoding="utf-8")
            controller = SpeedChangeController()
            prepared = controller.prepare(
                {"input_paths": [str(chart)], "speed": 1.5},
                ToolContext(
                    search_root=str(root),
                    output_root=str(root),
                    global_settings={"speed_file_suffix": " {num}x"},
                ),
            )
            self.assertIn(
                STORYBOARD_WARNING.format(name="song.osb"), prepared.warnings
            )
            events = []
            controller.run(prepared, lambda kind, payload: events.append((kind, payload)))
            self.assertTrue(
                any(
                    kind == "warning" and "song.osb" in str(payload)
                    for kind, payload in events
                ),
                events,
            )


class ChartEncodingTests(unittest.TestCase):
    """A chart's byte-order mark decides how it is decoded.

    A UTF-16 ``.mc`` used to decode "successfully" as cp936 into NUL-riddled
    mojibake, so the JSON parse failed and no output file was ever written.
    """

    _DOC = {
        "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": "Hard"},
        "time": [{"beat": [0, 0, 1], "bpm": 180}],
        "note": [{"beat": [4, 0, 1], "column": 0}],
    }

    def test_bom_aware_decoding_round_trips_every_encoding(self):
        from osu_toolbox.chart_convert import decode_chart_bytes, read_chart_text

        payload = json.dumps(self._DOC, ensure_ascii=False)
        for encoding, bom_expected in (
            ("utf-8", False),
            ("utf-8-sig", True),
            ("utf-16", False),
            ("utf-16-le", False),
            ("utf-16-be", False),
            ("utf-32", False),
            ("gbk", False),
        ):
            with self.subTest(encoding=encoding):
                data = payload.encode(encoding)
                text, bom = decode_chart_bytes(data)
                self.assertEqual(json.loads(text), self._DOC)
                self.assertEqual(bom, bom_expected)

        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "song.mc"
            chart.write_text(payload, encoding="utf-16")
            text, bom = read_chart_text(chart)
            self.assertEqual(json.loads(text), self._DOC)
            self.assertFalse(bom)

    def test_utf8_bom_is_reported_so_it_can_be_written_back(self):
        from osu_toolbox.chart_convert import read_chart_text

        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "song.mc"
            chart.write_text(json.dumps(self._DOC), encoding="utf-8-sig")
            _text, bom = read_chart_text(chart)
            self.assertTrue(bom)

    def test_utf16_mc_is_speed_changed_instead_of_failing(self):
        from osu_toolbox.chart_convert import read_chart_text

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.mc"
            chart.write_text(json.dumps(self._DOC, ensure_ascii=False), encoding="utf-16")
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            marked = root / "song 1.5x.mc"
            self.assertTrue(marked.is_file())
            # The output is written as UTF-8, so any tool (and the shared
            # reader) can read it back.
            text, _bom = read_chart_text(marked)
            self.assertEqual(json.loads(text)["time"][0]["bpm"], 270.0)

    def test_utf16_osu_is_speed_changed_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-16")
            outcome = change_speed(
                [chart], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            text = (root / "song 1.5x.osu").read_text(encoding="utf-8")
            self.assertIn("AudioLeadIn: 667", text)

    def test_sm_is_decoded_through_the_same_helper(self):
        from osu_toolbox.chart_convert import _read_text

        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "song.sm"
            chart.write_text("#TITLE:Test;\n#BPMS:0.000000=120.000000;\n", encoding="utf-16")
            self.assertIn("#TITLE:Test;", _read_text(chart))


class SpeedFailureReportingTests(unittest.TestCase):
    """A failed input must never look like a successful run with no output."""

    def _controller(self, root):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        return SpeedChangeController(), ToolContext(
            search_root=str(root),
            output_root=str(root),
            global_settings={},
        )

    def _chart_referencing(self, root, audio):
        chart = root / "song.mc"
        chart.write_text(json.dumps({
            "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": audio}},
            "time": [{"beat": [0, 0, 1], "bpm": 180}],
            "note": [{"beat": [4, 0, 1], "column": 0}],
        }), encoding="utf-8")
        return chart

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_undecodable_audio_still_writes_the_chart_and_says_so(self):
        """A failed transcode must never cost the chart.

        The user-visible symptom was "music but no chart": the encoder creates
        an empty file before failing, and the chart was only written after a
        successful transcode.  The chart is valid output either way, so it is
        written first and the audio problem is reported.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.mp3").write_bytes(b"this is not audio")
            chart = self._chart_referencing(root, "bad.mp3")
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": [str(chart)], "speed": 1.5}, context
            )
            events = []
            result = controller.run(
                prepared, lambda kind, payload: events.append((kind, payload))
            )
            marked = root / "song 1.5x.mc"
            self.assertEqual(result.data["paths"], [str(marked)])
            self.assertTrue(marked.is_file())
            payload = json.loads(marked.read_text(encoding="utf-8"))
            self.assertEqual(payload["time"][0]["bpm"], 270.0)
            # The chart keeps its original audio reference, so it still opens…
            self.assertEqual(payload["meta"]["song"]["file"], "bad.mp3")
            # …and no empty audio artifact is left behind.
            self.assertFalse((root / "bad 1.5x.mp3").exists())
            self.assertTrue(
                any(
                    kind in ("log", "warning") and "bad.mp3" in str(value)
                    for kind, value in events
                ),
                events,
            )

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_a_mix_of_good_and_bad_audio_reports_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-ac", "1", str(root / "good.wav"),
                ],
                capture_output=True,
            )
            good = root / "good.mc"
            good.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "good.wav"}},
                "time": [{"beat": [0, 0, 1], "bpm": 180}],
                "note": [{"beat": [4, 0, 1], "column": 0}],
            }), encoding="utf-8")
            (root / "bad.mp3").write_bytes(b"nope")
            bad = self._chart_referencing(root, "bad.mp3")
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": [str(good), str(bad)], "speed": 1.5}, context
            )
            events = []
            result = controller.run(prepared, lambda kind, payload: events.append((kind, payload)))
            paths = sorted(Path(item).name for item in result.data["paths"])
            # Both charts are produced; only the music of one is missing.
            self.assertEqual(paths, ["good 1.5x.mc", "song 1.5x.mc"])
            self.assertTrue((root / "good 1.5x.wav").is_file())
            self.assertFalse((root / "bad 1.5x.mp3").exists())
            self.assertTrue(
                any(
                    kind == "warning" and "bad.mp3" in str(value)
                    for kind, value in events
                ),
                events,
            )

    def test_a_pathological_mc_reports_a_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.mc"
            chart.write_bytes(bytes(range(256)) * 4)
            controller, context = self._controller(root)
            with self.assertRaises(Exception) as caught:
                controller.prepare({"input_paths": [str(chart)], "speed": 1.5}, context)
            self.assertIn("Malody", str(caught.exception))


@unittest.skipUnless(FFMPEG, "需要 FFmpeg")
class KeepPitchTests(unittest.TestCase):
    """``keep_pitch`` trades the pitch shift for a time-stretch stage."""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG

    def _audio(self, path, seconds=2.0, frequency=440):
        subprocess.run(
            [
                self.ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={seconds}",
                "-ar", "44100", "-ac", "1", str(path),
            ],
            capture_output=True,
        )

    def _analyse(self, path):
        import numpy as np

        completed = subprocess.run(
            [
                self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path),
                "-f", "f32le", "-ac", "1", "-ar", "44100", "-",
            ],
            capture_output=True,
        )
        samples = np.frombuffer(completed.stdout, dtype="<f4")
        size = 1 << 14
        self.assertGreater(samples.size, size)
        window = samples[:size] * np.hanning(size)
        spectrum = np.abs(np.fft.rfft(window))
        pitch = float(np.fft.rfftfreq(size, 1 / 44100.0)[int(np.argmax(spectrum))])
        info = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True,
        ).stderr
        stamp = next(
            line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            for line in info.splitlines() if "Duration:" in line
        )
        hours, minutes, seconds = stamp.split(":")
        return pitch, int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def test_keep_pitch_preserves_the_pitch_and_still_scales_the_duration(self):
        from osu_toolbox.speed_change import resample_audio

        for speed, default_pitch in ((0.9, 396.0), (1.5, 660.0), (2.0, 880.0)):
            with self.subTest(speed=speed):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "song.wav"
                    self._audio(source)
                    plain = root / "plain.wav"
                    kept = root / "kept.wav"
                    resample_audio(source, plain, speed, ffmpeg=self.ffmpeg)
                    resample_audio(
                        source, kept, speed, ffmpeg=self.ffmpeg, keep_pitch=True
                    )
                    plain_pitch, plain_duration = self._analyse(plain)
                    kept_pitch, kept_duration = self._analyse(kept)
                    # Default: the pitch follows the tempo.
                    self.assertAlmostEqual(plain_pitch, default_pitch, delta=25.0)
                    # keep_pitch: the pitch stays where it was.
                    self.assertAlmostEqual(kept_pitch, 440.0, delta=25.0)
                    # Both scale the duration identically.
                    self.assertAlmostEqual(kept_duration, 2.0 / speed, delta=0.15)
                    self.assertAlmostEqual(
                        plain_duration, kept_duration, delta=0.15
                    )

    def test_keep_pitch_keeps_the_source_sample_rate(self):
        from osu_toolbox.speed_change import resample_audio

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.wav"
            self._audio(source)
            kept = root / "kept.wav"
            resample_audio(source, kept, 2.0, ffmpeg=self.ffmpeg, keep_pitch=True)
            info = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-i", str(kept)],
                capture_output=True, text=True,
            ).stderr
            self.assertIn("44100 Hz", info)

    def test_extreme_multipliers_are_split_across_atempo_stages(self):
        from osu_toolbox.speed_change import _atempo_factors

        for speed in (0.05, 0.5, 0.9, 1.5, 2.0, 4.0, 20.0):
            with self.subTest(speed=speed):
                factors = _atempo_factors(speed)
                product = 1.0
                for factor in factors:
                    self.assertGreaterEqual(factor, 0.5 - 1e-9)
                    self.assertLessEqual(factor, 2.0 + 1e-9)
                    product *= factor
                self.assertAlmostEqual(product, speed, places=9)

    def test_the_filter_chain_matches_the_mode(self):
        from osu_toolbox.speed_change import _build_audio_command

        source = Path("song.mp3")
        plain = _build_audio_command(
            "ffmpeg", source, Path("out.mp3"), 1.5, ".mp3", 44100
        )
        self.assertIn("asetrate=44100*1.5,aresample=async=1", plain)
        kept = _build_audio_command(
            "ffmpeg", source, Path("out.mp3"), 1.5, ".mp3", 44100, keep_pitch=True
        )
        filter_value = kept[kept.index("-filter:a") + 1]
        self.assertNotIn("asetrate", filter_value)
        self.assertEqual(filter_value, "atempo=1.5,aresample=async=1")

    def test_an_ogg_source_falls_back_to_pinning_the_output_rate(self):
        """``asetrate`` moves the stream rate and libvorbis refuses odd rates.

        A 44.1 kHz stereo Vorbis source leaves ``asetrate`` at 66150 Hz, which
        ``libvorbis`` rejects outright, so the whole speed change failed and the
        user was left with an empty audio file.  A second command variant pins
        the output back to the source rate and succeeds.
        """
        from osu_toolbox.speed_change import _audio_attempts, resample_audio

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.ogg"
            completed = subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-ar", "44100", "-ac", "2", "-c:a", "libvorbis", str(source),
                ],
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-400:])

            attempts = list(
                _audio_attempts(FFMPEG, source, root / "o.ogg", 1.5, ".ogg", 44100, None)
            )
            self.assertEqual(len(attempts), 2, attempts)
            self.assertNotIn("-ar", attempts[0])
            self.assertIn("-ar", attempts[1])
            self.assertEqual(attempts[1][attempts[1].index("-ar") + 1], "44100")

            destination = root / "song 1.5x.ogg"
            resample_audio(source, destination, 1.5, ffmpeg=FFMPEG)
            self.assertTrue(destination.is_file())
            self.assertGreater(destination.stat().st_size, 0)

    def test_probe_audio_survives_a_non_utf8_metadata_stderr(self):
        """FFmpeg echoes the file name; Windows' default codec is not UTF-8.

        ``subprocess.run(..., text=True)`` decoded FFmpeg's stderr with the
        locale codec (GBK on a Chinese Windows), so a song whose metadata held
        a byte GBK cannot map raised ``UnicodeDecodeError`` and failed the whole
        speed change before any file was written.
        """
        from osu_toolbox.speed_change import probe_audio

        payload = (
            b"Input #0, mp3, from 'song.mp3':\n"
            b"    artist          : \x80\x81\x82 broken\n"
            b"  Duration: 00:03:00.00, bitrate: 192 kb/s\n"
            b"  Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp, 192 kb/s\n"
        )
        original = subprocess.run

        def fake_run(*args, **kwargs):
            self.assertNotIn("text", kwargs, "text=True reintroduces the locale codec")
            self.assertEqual(kwargs.get("encoding"), None)
            return subprocess.CompletedProcess(args, 0, b"", payload)

        subprocess.run = fake_run
        try:
            rate, bitrate = probe_audio("ffmpeg", "song.mp3")
        finally:
            subprocess.run = original
        self.assertEqual(rate, 44100)
        self.assertEqual(bitrate, 192000)

    def test_a_failed_transcode_leaves_no_empty_artifact(self):
        from osu_toolbox.speed_change import SpeedChangeError, resample_audio

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken.ogg"
            source.write_bytes(b"this is not audio at all")
            destination = root / "broken 1.5x.ogg"
            with self.assertRaises(SpeedChangeError):
                resample_audio(source, destination, 1.5, ffmpeg=FFMPEG)
            self.assertFalse(destination.exists())

    def test_the_controller_and_the_setting_both_drive_the_flag(self):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            controller = SpeedChangeController()
            # Setting only.
            prepared = controller.prepare(
                {"input_paths": [str(chart)], "speed": 1.5},
                ToolContext(
                    search_root=str(root),
                    output_root=str(root),
                    global_settings={"speed_keep_pitch": True},
                ),
            )
            self.assertTrue(prepared.params["keep_pitch"])
            # The per-run parameter wins over the setting, in both directions.
            prepared = controller.prepare(
                {"input_paths": [str(chart)], "speed": 1.5, "keep_pitch": False},
                ToolContext(
                    search_root=str(root),
                    output_root=str(root),
                    global_settings={"speed_keep_pitch": True},
                ),
            )
            self.assertFalse(prepared.params["keep_pitch"])
            prepared = controller.prepare(
                {"input_paths": [str(chart)], "speed": 1.5, "keep_pitch": True},
                ToolContext(
                    search_root=str(root),
                    output_root=str(root),
                    global_settings={"speed_keep_pitch": False},
                ),
            )
            self.assertTrue(prepared.params["keep_pitch"])

    def test_the_exposed_schema_declares_the_switch(self):
        from osu_toolbox.speed_settings import (
            SPEED_SETTING_PARAMS,
            normalize_speed_settings,
        )
        from osu_toolbox.tools.speed_change import SPEED_CHANGE_PARAMS

        setting = next(
            spec for spec in SPEED_SETTING_PARAMS if spec.key == "speed_keep_pitch"
        )
        self.assertEqual(setting.kind, "bool")
        self.assertFalse(setting.default)
        self.assertEqual(setting.group, "speed_tool")
        self.assertIn("音调", setting.help)
        self.assertFalse(normalize_speed_settings({})["speed_keep_pitch"])
        self.assertTrue(
            normalize_speed_settings({"speed_keep_pitch": "true"})["speed_keep_pitch"]
        )

        tool_param = next(
            spec for spec in SPEED_CHANGE_PARAMS if spec.key == "keep_pitch"
        )
        self.assertEqual(tool_param.kind, "bool")
        self.assertFalse(tool_param.default)
        self.assertIn("音调", tool_param.help)

    def test_the_command_line_switch_selects_the_mode(self):
        from osu_toolbox.quick import build_parser

        args = build_parser().parse_args(
            ["--operation", "speed-change", "--target-format", "1.5", "x.osu"]
        )
        self.assertFalse(args.keep_pitch)
        args = build_parser().parse_args(
            ["--operation", "speed-change", "--target-format", "1.5", "--keep-pitch", "x.osu"]
        )
        self.assertTrue(args.keep_pitch)


class FileDialogFilterTests(unittest.TestCase):
    """The GUI file picker must offer every supported format.

    pywebview's Windows backend joins the entries with ``|`` and re-parses each
    one, so an entry has to be a complete ``描述 (模式)`` filter.  When the list
    degenerates to a single ``*.osu`` filter, ``.mc`` charts become unpickable
    and the user cannot start a speed change at all.
    """

    def test_each_supported_extension_gets_its_own_filter(self):
        from osu_toolbox.gui.bridge import _dialog_file_types

        filters = _dialog_file_types((("谱面或归档", "*.osu;*.mc;*.sm;*.osz;*.mcz"),))
        for extension in (".osu", ".mc", ".sm", ".osz", ".mcz"):
            with self.subTest(extension=extension):
                self.assertIn(f"谱面或归档 (*{extension})", filters)
        # The combined entry comes first so it is the dialog's default.
        self.assertEqual(filters[0], "谱面或归档 (*.osu;*.mc;*.sm;*.osz;*.mcz)")

    def test_filters_round_trip_through_the_backend_parser(self):
        from webview.util import parse_file_type

        from osu_toolbox.gui.bridge import _dialog_file_types
        from osu_toolbox.tools.registry import build_default_registry

        for definition in build_default_registry().list_definitions():
            for spec in definition.spec.params:
                filters = _dialog_file_types(spec.filters)
                for item in filters:
                    with self.subTest(tool=definition.spec.id, item=item):
                        description, pattern = parse_file_type(item)
                        self.assertTrue(pattern.startswith("*"))
                        self.assertTrue(description.strip())
                if not filters:
                    continue
                # Reproduce the backend join and confirm every filter survived
                # as a description/pattern pair.
                joined = "|".join(
                    "{0} ({1})|{1}".format(*parse_file_type(f)) for f in filters
                )
                pairs = joined.split("|")
                self.assertEqual(
                    len(pairs), 2 * len(filters), f"{joined!r} is not pair-shaped"
                )
                for index in range(0, len(pairs), 2):
                    self.assertTrue(pairs[index].strip(), joined)
                    self.assertTrue(pairs[index + 1].startswith("*"), joined)

    def test_the_speed_tool_offers_charts_and_archives(self):
        from osu_toolbox.gui.bridge import _dialog_file_types
        from osu_toolbox.tools.speed_change import SPEED_CHANGE_PARAMS

        spec = next(item for item in SPEED_CHANGE_PARAMS if item.key == "input_paths")
        filters = _dialog_file_types(spec.filters)
        self.assertEqual(
            filters[0], "谱面或归档 (*.osu;*.mc;*.sm;*.osz;*.mcz)"
        )
        self.assertEqual(len(filters), 6)


@unittest.skipUnless(FFMPEG, "需要 FFmpeg")
class SpeedCollectionLayoutTests(unittest.TestCase):
    """输出到独立文件夹：一个歌曲一个文件夹，源目录不变。"""

    _OSU = (
        "osu file format v14\n\n[General]\nAudioFilename: audio.mp3\nMode: 3\n"
        "[Metadata]\nTitle:{title}\nVersion:{version}\n[Difficulty]\nCircleSize:4\n"
        '[Events]\n0,0,"cover.jpg",0,0\n'
        "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,1000,1,0,0:0:0:0:\n"
    )

    def _song(self, root, title, versions):
        folder = root / title
        (folder / "sub").mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ar", "44100", "-ac", "1", "-b:a", "128k", str(folder / "audio.mp3"),
            ],
            capture_output=True,
        )
        (folder / "cover.jpg").write_bytes(b"image")
        (folder / "readme.txt").write_bytes(b"notes")
        (folder / "sub" / "extra.png").write_bytes(b"image2")
        for version in versions:
            (folder / f"{version}.osu").write_text(
                self._OSU.format(title=title, version=version), encoding="utf-8"
            )
        return folder

    def _controller(self, root):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        return SpeedChangeController(), ToolContext(
            search_root=str(root), output_root=str(root), global_settings={}
        )

    def test_each_source_folder_becomes_one_song_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            self._song(songs, "Odo", ["Easy", "Hard"])
            self._song(songs, "Ghost", ["Insane"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(songs / "Odo"), str(songs / "Ghost" / "Insane.osu")],
                    "speed": 1.5,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            result = controller.run(prepared, lambda *_args: None)
            self.assertEqual(result.data["columns"], ("输入文件", "变速倍率", "输出文件"))
            self.assertEqual(
                sorted(item.name for item in out.iterdir()), ["Ghost", "Odo 1.5x"]
            )
            # The folder structure survives while charts and music gain the marker.
            self.assertEqual(
                sorted(item.name for item in (out / "Odo 1.5x").iterdir()),
                [
                    "Easy 1.5x.osu", "Hard 1.5x.osu", "audio 1.5x.mp3",
                    "cover.jpg", "readme.txt", "sub",
                ],
            )
            self.assertTrue((out / "Odo 1.5x" / "sub" / "extra.png").is_file())
            text = (out / "Odo 1.5x" / "Hard 1.5x.osu").read_text(encoding="utf-8")
            self.assertIn("AudioFilename: audio 1.5x.mp3", text)
            self.assertIn("Version:Hard 1.5x", text)
            # A chart reached through the selected folder marks the song name.
            self.assertIn("Title:Odo 1.5x", text)
            # The note kept its beat: at 500 ms/beat, 1000 ms was beat 2, and the
            # faster music puts beat 2 at 667 ms.
            self.assertIn("64,192,667,", text)
            # The source tree is untouched.
            self.assertEqual(
                sorted(item.name for item in (songs / "Odo").iterdir()),
                ["Easy.osu", "Hard.osu", "audio.mp3", "cover.jpg", "readme.txt", "sub"],
            )
            self.assertFalse((songs / "Odo" / "Hard 1.5x.osu").exists())

    def test_the_copy_speed_changes_the_music_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            self._song(songs, "Odo", ["Hard"])
            original = (songs / "Odo" / "audio.mp3").stat().st_size
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(songs / "Odo" / "Hard.osu")],
                    "speed": 2.0,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            controller.run(prepared, lambda *_args: None)
            copied = out / "Odo" / "audio 2x.mp3"
            self.assertTrue(copied.is_file())
            # The copied audio is a re-encode of the speed-changed source, not
            # a byte copy of the original, and the original is untouched.
            self.assertNotEqual(copied.read_bytes(), (songs / "Odo" / "audio.mp3").read_bytes())
            self.assertGreater(copied.stat().st_size, 0)
            self.assertEqual((songs / "Odo" / "audio.mp3").stat().st_size, original)
            # The unmarked original is not duplicated into the collection.
            self.assertFalse((out / "Odo" / "audio.mp3").exists())

    def test_two_charts_from_one_folder_share_a_single_result_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            self._song(songs, "Odo", ["Easy", "Hard"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [
                        str(songs / "Odo" / "Easy.osu"),
                        str(songs / "Odo" / "Hard.osu"),
                    ],
                    "speed": 1.1,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            controller.run(prepared, lambda *_args: None)
            self.assertEqual(sorted(item.name for item in out.iterdir()), ["Odo"])
            self.assertEqual(
                sorted(item.name for item in (out / "Odo").iterdir()),
                [
                    "Easy 1.1x.osu", "Hard 1.1x.osu", "audio 1.1x.mp3",
                    "cover.jpg", "readme.txt", "sub",
                ],
            )
            # Both charts point at the same marked music, which was produced once.
            for version in ("Easy", "Hard"):
                text = (out / "Odo" / f"{version} 1.1x.osu").read_text(encoding="utf-8")
                self.assertIn("AudioFilename: audio 1.1x.mp3", text)

    def test_same_named_sources_do_not_collide(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            first = root / "pack-a" / "Odo"
            second = root / "pack-b" / "Odo"
            self._song(root / "pack-a", "Odo", ["Hard"])
            self._song(root / "pack-b", "Odo", ["Easy"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(first / "Hard.osu"), str(second / "Easy.osu")],
                    "speed": 1.1,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            controller.run(prepared, lambda *_args: None)
            self.assertEqual(sorted(item.name for item in out.iterdir()), ["Odo", "Odo-2"])

    def test_a_folder_s_own_archive_is_repacked_too(self):
        """Everything in the folder is speed-changed, archives included.

        Malody stores a song both loose and packed (``Song/0.mc`` next to
        ``Song.mcz``).  Selecting the folder must speed-change both: the loose
        charts, music and archives gain the marker. The source is untouched.
        """
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            archive = folder / "pack.mcz"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("song.mc", json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0}],
                }))
                handle.write(folder / "audio.mp3", "audio.mp3")
            before = archive.read_bytes()
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            self.assertEqual(prepared.params["planned_outputs"], [str(out / "Odo 1.5x")])
            controller.run(prepared, lambda *_args: None)

            result = out / "Odo 1.5x"
            self.assertTrue((result / "Hard 1.5x.osu").is_file())
            self.assertTrue((result / "audio 1.5x.mp3").is_file())
            self.assertFalse((result / "pack.mcz").exists())
            repacked = result / "pack 1.5x.mcz"
            self.assertTrue(repacked.is_file())
            with zipfile.ZipFile(repacked) as handle:
                self.assertEqual(
                    sorted(handle.namelist()), ["audio 1.5x.mp3", "song 1.5x.mc"]
                )
            # The source archive keeps its bytes and its member names.
            self.assertEqual(archive.read_bytes(), before)
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual(sorted(handle.namelist()), ["audio.mp3", "song.mc"])

    def test_the_tree_copy_never_overwrites_a_speed_changed_result(self):
        """Colliding archive outputs each keep their own transformed contents."""
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            chart = json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "audio.mp3"}},
                "time": [{"beat": [0, 0, 1], "bpm": 180}],
                "note": [{"beat": [4, 0, 1], "column": 0}],
            })
            with zipfile.ZipFile(folder / "pack.mcz", "w") as handle:
                handle.writestr("song.mc", chart)
                handle.write(folder / "audio.mp3", "audio.mp3")
            # An existing marked archive is a separate input, and is transformed too.
            with zipfile.ZipFile(folder / "pack 1.5x.mcz", "w") as handle:
                handle.writestr("earlier.mc", chart)
                handle.write(folder / "audio.mp3", "audio.mp3")
                handle.writestr("stale.txt", "an earlier result")

            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            controller.run(prepared, lambda *_args: None)

            result = out / "Odo 1.5x"
            with zipfile.ZipFile(result / "pack 3 1.5x.mcz") as handle:
                self.assertEqual(sorted(handle.namelist()), ["audio 1.5x.mp3", "song 1.5x.mc"])
                self.assertEqual(json.loads(handle.read("song 1.5x.mc"))["time"][0]["bpm"], 270.0)
            with zipfile.ZipFile(result / "pack 2 1.5x.mcz") as handle:
                self.assertEqual(sorted(handle.namelist()), ["audio 1.5x.mp3", "earlier 1.5x.mc", "stale.txt"])
                self.assertEqual(json.loads(handle.read("earlier 1.5x.mc"))["time"][0]["bpm"], 270.0)
                self.assertEqual(handle.read("stale.txt"), b"an earlier result")

    def test_a_folder_holding_marked_files_does_not_overwrite_them(self):
        """Different input names remain different in the complete folder copy."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            # A leftover from an earlier 1.5x run, beside the original.
            (folder / "Hard 1.5x.osu").write_text(
                self._OSU.format(title="Odo", version="Hard Old"), encoding="utf-8"
            )
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            controller.run(prepared, lambda *_args: None)

            result = out / "Odo 1.5x"
            versions = {}
            for name in ("Hard 2 1.5x.osu", "Hard 3 1.5x.osu"):
                path = result / name
                self.assertTrue(path.is_file(), name)
                versions[name] = path.read_text(encoding="utf-8").split(
                    "Version:"
                )[1].split("\n")[0]
            # One result per source chart, each marked exactly once.
            self.assertEqual(
                sorted(versions.values()), ["Hard 1.5x", "Hard Old 1.5x"]
            )
            self.assertFalse((result / "Hard 1.5x 1.5x.osu").exists())
            self.assertEqual({p.name for p in result.glob("*.osu")},
                             {"Hard 2 1.5x.osu", "Hard 3 1.5x.osu"})

    def test_an_archive_picked_as_a_file_is_still_repacked(self):
        """Naming the archive itself is the one case that produces a new archive."""
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            archive = folder / "pack.mcz"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("song.mc", json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0}],
                }))
                handle.write(folder / "audio.mp3", "audio.mp3")
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": str(archive), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            controller.run(prepared, lambda *_args: None)
            self.assertTrue((out / "Odo" / "pack 1.5x.mcz").is_file())
            self.assertFalse((out / "Odo" / "pack.mcz").exists())

    def test_a_song_with_both_charts_and_an_archive_keeps_both(self):
        """An archive in the folder must not hide the plain charts beside it."""
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            with zipfile.ZipFile(folder / "pack.mcz", "w") as handle:
                handle.writestr("song.mc", json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0}],
                }))
                handle.write(folder / "audio.mp3", "audio.mp3")
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            controller.run(prepared, lambda *_args: None)
            result = out / "Odo 1.5x"
            # The loose chart was speed-changed, not skipped in favour of the pack.
            self.assertTrue((result / "Hard 1.5x.osu").is_file())
            self.assertTrue((result / "audio 1.5x.mp3").is_file())
            # The pack beside it and its members are renamed too.
            self.assertTrue((result / "pack 1.5x.mcz").is_file())
            self.assertFalse((result / "pack.mcz").exists())

    def test_archive_members_and_metadata_gain_the_marker(self):
        """Members retain their distinct stems and gain the speed marker."""
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = songs / "the world of scarlet"
            folder.mkdir(parents=True)
            for level, version in (("0", "Sir This Way"), ("1", "Another")):
                (folder / f"{level}.mc").write_text(json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": version,
                             "song": {"title": "the world of scarlet",
                                      "file": f"{level}.ogg"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0},
                             {"beat": [0, 0, 1], "sound": f"{level}.ogg", "offset": 0,
                              "vol": 100, "type": 1}],
                }), encoding="utf-8")
                subprocess.run(
                    [
                        FFMPEG, "-hide_banner", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ar", "44100", "-ac", "1", str(folder / f"{level}.ogg"),
                    ],
                    capture_output=True,
                )
            (folder / "bg.jpg").write_bytes(b"image")
            archive = folder / "the world of scarlet.mcz"
            with zipfile.ZipFile(archive, "w") as handle:
                for name in ("0.mc", "0.ogg", "1.mc", "1.ogg", "bg.jpg"):
                    handle.write(folder / name, name)

            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": str(archive), "speed": 1.1,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            controller.run(prepared, lambda *_args: None)

            produced = out / "the world of scarlet" / "the world of scarlet 1.1x.mcz"
            self.assertTrue(produced.is_file())
            with zipfile.ZipFile(produced) as handle:
                self.assertEqual(
                    sorted(handle.namelist()),
                    [
                        "0 1.1x.mc",
                        "0 1.1x.ogg",
                        "1 1.1x.mc",
                        "1 1.1x.ogg",
                        "bg.jpg",
                    ],
                )
                payload = json.loads(handle.read("0 1.1x.mc"))
                second = json.loads(handle.read("1 1.1x.mc"))
            # Each chart points at the music renamed beside it, and the difficulty
            # name carries its own marker.
            self.assertEqual(payload["meta"]["song"]["file"], "0 1.1x.ogg")
            self.assertEqual(payload["meta"]["version"], "Sir This Way 1.1x")
            self.assertEqual(second["meta"]["song"]["file"], "1 1.1x.ogg")
            self.assertEqual(second["meta"]["version"], "Another 1.1x")

    def test_existing_marked_member_names_survive_without_new_collisions(self):
        """Distinct original member names stay distinct with collision counters.

        ``0.ogg`` beside an existing ``0 1.5x.ogg`` would collide once the marker
        is applied; the fallback counter goes *before* the marker, so nobody reads
        ``0 1.5x 2.ogg`` as "1.5x doubled".
        """
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pack.osz"
            with zipfile.ZipFile(source, "w") as handle:
                handle.writestr("easy.osu", _osu_text(version="Easy"))
                # A member already carrying the marked name.
                handle.writestr("easy 1.5x.osu", _osu_text(version="Old"))
            outcome = change_speed(
                [source], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            with zipfile.ZipFile(root / "pack 1.5x.osz") as handle:
                self.assertEqual(
                    sorted(handle.namelist()),
                    ["easy 2 1.5x.osu", "easy 3 1.5x.osu"],
                )

    def test_an_archive_keeps_a_meaningful_member_stem(self):
        """Each difficulty keeps its own stem before the speed marker."""
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pack.osz"
            with zipfile.ZipFile(source, "w") as handle:
                for version in ("Easy", "Hard"):
                    handle.writestr(f"{version.lower()}.osu", _osu_text(version=version))
            outcome = change_speed(
                [source], 1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
            )
            self.assertEqual(outcome["failures"], [])
            with zipfile.ZipFile(root / "pack 1.5x.osz") as handle:
                # Several difficulties have to stay distinguishable, so their own
                # names survive and only the marker is added.
                self.assertEqual(
                    sorted(handle.namelist()),
                    ["easy 1.5x.osu", "hard 1.5x.osu"],
                )
                self.assertIn("Version:Easy 1.5x", handle.read("easy 1.5x.osu").decode("utf-8"))

    def test_an_archive_stays_an_archive_inside_its_folder(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            archive = folder / "pack.mcz"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("song.mc", json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "song": {"file": "audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0}],
                }))
                handle.write(folder / "audio.mp3", "audio.mp3")
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(archive)],
                    "speed": 1.5,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            controller.run(prepared, lambda *_args: None)
            result_archive = out / "Odo" / "pack 1.5x.mcz"
            self.assertTrue(result_archive.is_file())
            with zipfile.ZipFile(result_archive) as handle:
                # Every member keeps the name it was shipped with and only gains
                # the marker, so the inside matches the marked archive name.
                self.assertEqual(
                    sorted(handle.namelist()), ["audio 1.5x.mp3", "song 1.5x.mc"]
                )
                payload = json.loads(handle.read("song 1.5x.mc"))
            self.assertEqual(payload["time"][0]["bpm"], 270.0)
            self.assertEqual(payload["meta"]["song"]["file"], "audio 1.5x.mp3")
            # The source archive is unchanged.
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual(
                    json.loads(handle.read("song.mc"))["time"][0]["bpm"], 180.0
                )
                self.assertEqual(sorted(handle.namelist()), ["audio.mp3", "song.mc"])

    def test_a_selected_library_becomes_one_complete_marked_folder(self):
        """A selected library becomes one complete copy without splitting songs."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            self._song(songs, "Odo", ["Easy", "Hard"])
            self._song(songs, "Ghost", ["Insane"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(songs)],
                    "speed": 1.1,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            self.assertEqual(
                prepared.params["planned_outputs"],
                [str(out / "songs 1.1x")],
            )
            controller.run(prepared, lambda *_args: None)
            self.assertEqual(
                sorted(item.name for item in out.iterdir()), ["songs 1.1x"]
            )
            # A song folder is copied whole, with its own internal structure.
            self.assertTrue((out / "songs 1.1x" / "Odo" / "Easy 1.1x.osu").is_file())
            self.assertTrue((out / "songs 1.1x" / "Ghost" / "Insane 1.1x.osu").is_file())
            self.assertTrue((out / "songs 1.1x" / "Odo" / "sub" / "extra.png").is_file())
            # The original library is not duplicated under an unmarked name.
            self.assertFalse((out / "songs").exists())

    def test_a_deeply_nested_song_keeps_its_folder_chain(self):
        """``Song/0/chart.mc`` must land in ``<result>/Song/0/``."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "beatmap"
            out = root / "out"
            for title in ("Song A", "Song B"):
                nested = library / title / "0"
                nested.mkdir(parents=True)
                (nested / "chart.mc").write_text(json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": "Hard",
                             "song": {"title": title, "file": "../audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0},
                             {"beat": [0, 0, 1], "sound": "../audio.mp3", "offset": 0,
                              "vol": 100, "type": 1}],
                }), encoding="utf-8")
                (library / title / "cover.jpg").write_bytes(b"image")
                subprocess.run(
                    [
                        FFMPEG, "-hide_banner", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ar", "44100", "-ac", "1",
                        str(library / title / "audio.mp3"),
                    ],
                    capture_output=True,
                )
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(library), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            controller.run(prepared, lambda *_args: None)

            # The whole library is copied, with the
            # chart's own ``0`` level is kept inside the song, so the song name
            # is never replaced by a number and the chain survives whole.
            self.assertEqual(
                sorted(item.name for item in out.iterdir()), ["beatmap 1.5x"]
            )
            for title in ("Song A", "Song B"):
                with self.subTest(song=title):
                    result = out / "beatmap 1.5x" / title
                    chart = result / "0" / "chart 1.5x.mc"
                    self.assertTrue(chart.is_file())
                    self.assertTrue((result / "cover.jpg").is_file())
                    self.assertTrue((result / "audio 1.5x.mp3").is_file())
                    payload = json.loads(chart.read_text(encoding="utf-8"))
                    # The cross-folder reference still resolves after extraction.
                    reference = payload["meta"]["song"]["file"]
                    self.assertEqual(reference, "../audio 1.5x.mp3")
                    self.assertTrue((chart.parent / reference).resolve().is_file())
            # No stray folder named after the intermediate directory.
            self.assertFalse((out / "0").exists())
            self.assertFalse((out / "0-2").exists())

    def test_selected_storage_folders_each_get_a_complete_marked_copy(self):
        """Each selected storage folder keeps its identity and brings its music."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "beatmap"
            out = root / "out"
            for level in ("0", "1"):
                nested = library / "Song A" / level
                nested.mkdir(parents=True)
                (nested / "chart.mc").write_text(json.dumps({
                    "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": level,
                             "song": {"title": "Song A", "file": "../audio.mp3"}},
                    "time": [{"beat": [0, 0, 1], "bpm": 180}],
                    "note": [{"beat": [4, 0, 1], "column": 0},
                             {"beat": [0, 0, 1], "sound": "../audio.mp3", "offset": 0,
                              "vol": 100, "type": 1}],
                }), encoding="utf-8")
            (library / "Song A" / "cover.jpg").write_bytes(b"image")
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-ac", "1",
                    str(library / "Song A" / "audio.mp3"),
                ],
                capture_output=True,
            )
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": "\n".join([
                    str(library / "Song A" / "0"), str(library / "Song A" / "1"),
                 ]),
                 "speed": 1.5, "separate_folder": True, "output_folder": str(out)},
                context,
            )
            self.assertEqual(prepared.params["planned_outputs"],
                             [str(out / "0 1.5x"), str(out / "1 1.5x")])
            controller.run(prepared, lambda *_args: None)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["0 1.5x", "1 1.5x"])
            for level in ("0", "1"):
                result = out / f"{level} 1.5x"
                chart = result / "chart 1.5x.mc"
                self.assertTrue(chart.is_file())
                self.assertTrue((result / "audio 1.5x.mp3").is_file())
                payload = json.loads(chart.read_text(encoding="utf-8"))
                self.assertEqual(payload["meta"]["song"]["file"], "audio 1.5x.mp3")
                self.assertEqual(payload["meta"]["song"]["title"], "Song A 1.5x")
                self.assertEqual(payload["meta"]["version"], f"{level} 1.5x")
            self.assertTrue((library / "Song A" / "audio.mp3").is_file())
            self.assertFalse((out / "Song A").exists())

    def test_a_folder_input_without_the_switch_creates_a_sibling_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            self._song(songs, "Odo", ["Hard"])
            self._song(songs, "Ghost", ["Insane"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": [str(songs)], "speed": 1.1}, context
            )
            controller.run(prepared, lambda *_args: None)
            destination = root / "songs 1.1x"
            self.assertEqual(prepared.params["planned_outputs"], [str(destination)])
            self.assertTrue((destination / "Odo" / "Hard 1.1x.osu").is_file())
            self.assertTrue((destination / "Ghost" / "Insane 1.1x.osu").is_file())
            self.assertFalse((songs / "Odo" / "Hard 1.1x.osu").exists())
            self.assertFalse((songs / "Ghost" / "Insane 1.1x.osu").exists())
            self.assertFalse((root / "out").exists())

    def test_the_switch_requires_an_output_folder(self):
        from osu_toolbox.schema import ParamValidationError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            self._song(songs, "Odo", ["Hard"])
            controller, context = self._controller(root)
            with self.assertRaises(ParamValidationError) as caught:
                controller.prepare(
                    {
                        "input_paths": [str(songs / "Odo" / "Hard.osu")],
                        "speed": 1.5,
                        "separate_folder": True,
                    },
                    context,
                )
            self.assertEqual(caught.exception.key, "output_folder")

    def test_a_dead_audio_file_keeps_the_chart_and_reports_the_music(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = songs / "Odo"
            folder.mkdir(parents=True)
            (folder / "audio.mp3").write_bytes(b"not audio")
            (folder / "Hard.osu").write_text(
                self._OSU.format(title="Odo", version="Hard"), encoding="utf-8"
            )
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(folder / "Hard.osu")],
                    "speed": 1.5,
                    "separate_folder": True,
                    "output_folder": str(out),
                },
                context,
            )
            events = []
            result = controller.run(prepared, lambda kind, payload: events.append((kind, payload)))
            # Collection mode reports one output per song folder.
            self.assertEqual(result.data["paths"], [str(out / "Odo")])
            marked = out / "Odo" / "Hard 1.5x.osu"
            self.assertTrue(marked.is_file())
            text = marked.read_text(encoding="utf-8")
            self.assertIn("64,192,667,", text)
            # The chart was named directly, so its song name is left as authored
            # even though the output is a collection folder.
            self.assertIn("Title:Odo\n", text)
            self.assertNotIn("Title:Odo 1.5x", text)
            # The failed music is not pretended to exist, and its empty artifact
            # is cleaned up instead of being shipped.
            self.assertIn("AudioFilename: audio.mp3", text)
            self.assertFalse((out / "Odo" / "audio 1.5x.mp3").exists())
            self.assertTrue(
                any("audio.mp3" in str(value) for _kind, value in events), events
            )

    def test_the_plan_lists_song_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            self._song(songs, "Odo", ["Hard"])
            plan = plan_speed_change(
                [songs / "Odo" / "Hard.osu"],
                1.5,
                file_pattern=" {num}x",
                difficulty_pattern=" {num}x",
                output_folder=str(out),
            )
            self.assertEqual(plan[0]["outputs"], [str(out / "Odo")])
            self.assertEqual(plan[0]["rows"][0], ("歌曲文件夹", str(out / "Odo")))

    def test_every_finished_file_is_logged_with_its_reason_on_failure(self):
        """Each produced file reports itself, so a long run is not a black box.

        The music line carries the transcode result and the reason when it fails;
        the chart line reports that it fell back to the original reference.  Both
        go to the run log only -- nothing is written to a log file, so nothing
        accumulates on disk.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            # Not decodable audio: the transcode must fail and say why.
            (folder / "audio.mp3").write_bytes(b"not audio at all")
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            events = []
            controller.run(prepared, lambda kind, payload: events.append((kind, payload)))

            lines = [
                payload for kind, payload in events if kind in ("log", "warning")
            ]
            warnings = [payload for kind, payload in events if kind == "warning"]
            joined = "\n".join(lines)
            self.assertIn("音乐未变速", joined)
            self.assertIn("无法读取音频采样率", joined)
            self.assertIn("保留原音频引用", joined)
            self.assertIn("谱面已生成", joined)
            self.assertTrue(warnings, "失败必须在警告里出现，否则结果面板看不出问题")
            # The chart survives the failed track.
            self.assertTrue((out / "Odo 1.5x" / "Hard 1.5x.osu").is_file())
            # Nothing was archived to disk: the log lives in the session only.
            self.assertEqual(
                sorted(item.name for item in out.rglob("*") if item.suffix == ".log"),
                [],
            )

    def test_each_finished_file_is_logged_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "songs"
            out = root / "out"
            folder = self._song(songs, "Odo", ["Hard"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5,
                 "separate_folder": True, "output_folder": str(out)},
                context,
            )
            events = []
            controller.run(prepared, lambda kind, payload: events.append((kind, payload)))
            logged = [payload for kind, payload in events if kind == "log"]
            self.assertTrue(
                any("音乐已变速" in line and "audio 1.5x.mp3" in line for line in logged),
                logged,
            )
            self.assertTrue(
                any("谱面已生成" in line and "Hard 1.5x.osu" in line for line in logged),
                logged,
            )


class SpeedMarkerOverrideTests(unittest.TestCase):
    """The tool form must let one run change the marker without touching settings."""

    @staticmethod
    def _context(root, **settings):
        from osu_toolbox.tools.base import ToolContext

        return ToolContext(
            search_root=str(root), output_root=str(root), global_settings=dict(settings)
        )

    def _prepare(self, root, raw, **settings):
        from osu_toolbox.tools.speed_change import SpeedChangeController

        chart = root / "song.osu"
        chart.write_text(_osu_text(), encoding="utf-8")
        prepared = SpeedChangeController().prepare(
            {"input_paths": [str(chart)], "speed": 1.5, **raw},
            self._context(root, **settings),
        )
        return prepared.params

    def test_the_form_defaults_to_the_configured_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            params = self._prepare(
                root,
                {},
                speed_file_suffix="_{num}x",
                speed_difficulty_marker=" [{num}x]",
            )
            self.assertEqual(params["file_pattern"], "_{num}x")
            self.assertEqual(params["difficulty_pattern"], " [{num}x]")
            self.assertIn(str(root / "song_1.5x.osu"), params["planned_outputs"])

    def test_a_per_run_pattern_overrides_the_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            params = self._prepare(
                root,
                {"file_suffix": " ({num}x)", "difficulty_marker_pattern": "【{num}x】"},
                speed_file_suffix="_{num}x",
                speed_difficulty_marker=" [{num}x]",
            )
            self.assertEqual(params["file_pattern"], " ({num}x)")
            self.assertEqual(params["difficulty_pattern"], "【{num}x】")
            self.assertIn(str(root / "song (1.5x).osu"), params["planned_outputs"])

    def test_each_field_falls_back_on_its_own(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = {
                "speed_file_suffix": "_{num}x",
                "speed_difficulty_marker": " [{num}x]",
            }
            # Only the difficulty field is set: the file name keeps the setting.
            params = self._prepare(
                root, {"difficulty_marker_pattern": "-{num}"}, **configured
            )
            self.assertEqual(params["file_pattern"], "_{num}x")
            self.assertEqual(params["difficulty_pattern"], "-{num}")
            # Only the file field is set: the difficulty keeps the setting.
            params = self._prepare(root, {"file_suffix": "-{num}"}, **configured)
            self.assertEqual(params["file_pattern"], "-{num}")
            self.assertEqual(params["difficulty_pattern"], " [{num}x]")

    def test_a_blank_field_is_treated_as_use_the_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for blank in ("", "   "):
                with self.subTest(blank=repr(blank)):
                    params = self._prepare(
                        root,
                        {"file_suffix": blank, "difficulty_marker_pattern": blank},
                        speed_file_suffix="_{num}x",
                        speed_difficulty_marker=" [{num}x]",
                    )
                    self.assertEqual(params["file_pattern"], "_{num}x")
                    self.assertEqual(params["difficulty_pattern"], " [{num}x]")

    def test_writing_none_switches_the_difficulty_marker_off_for_this_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            params = self._prepare(
                root,
                {"difficulty_marker_pattern": "NONE"},
                speed_file_suffix="_{num}x",
                speed_difficulty_marker=" [{num}x]",
            )
            # The file name keeps the setting; only the difficulty name is clear.
            self.assertEqual(params["file_pattern"], "_{num}x")
            self.assertEqual(params["difficulty_pattern"], "")

    def test_the_file_marker_cannot_be_switched_off(self):
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            with self.assertRaises(ParamValidationError) as caught:
                SpeedChangeController().prepare(
                    {
                        "input_paths": [str(chart)],
                        "speed": 1.5,
                        "file_suffix": "none",
                    },
                    self._context(root),
                )
            self.assertEqual(caught.exception.key, "file_suffix")
            self.assertIn("覆盖", str(caught.exception))

    def test_the_override_does_not_persist_to_the_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {"speed_file_suffix": "_{num}x"}
            params = self._prepare(
                root, {"file_suffix": "-{num}"}, **settings
            )
            self.assertEqual(params["file_pattern"], "-{num}")
            # Nothing wrote back into the context's settings.
            self.assertEqual(settings["speed_file_suffix"], "_{num}x")

    def test_the_form_exposes_both_pattern_fields(self):
        from osu_toolbox.tools.speed_change import SPEED_CHANGE_PARAMS

        for key in ("file_suffix", "difficulty_marker_pattern"):
            with self.subTest(key=key):
                spec = next(item for item in SPEED_CHANGE_PARAMS if item.key == key)
                self.assertEqual(spec.kind, "text")
                self.assertFalse(spec.persist, "per-run fields must not persist")
                self.assertEqual(spec.group, "speed")
                self.assertIn("{num}", spec.help)

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_the_per_run_patterns_reach_the_written_files(self):
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-ac", "1", str(root / "song.wav"),
                ],
                capture_output=True,
            )
            prepared = SpeedChangeController().prepare(
                {
                    "input_paths": [str(chart)],
                    "speed": 1.5,
                    "file_suffix": " ({num}x)",
                    "difficulty_marker_pattern": "【{num}x】",
                },
                self._context(root, speed_file_suffix="_{num}x"),
            )
            SpeedChangeController().run(prepared, lambda *_args: None)
            marked = root / "song (1.5x).osu"
            self.assertTrue(marked.is_file())
            self.assertTrue((root / "song (1.5x).wav").is_file())
            text = marked.read_text(encoding="utf-8")
            self.assertIn("AudioFilename: song (1.5x).wav", text)
            self.assertIn("Version:Hard【1.5x】", text)


class SpeedFolderInputTests(unittest.TestCase):
    """A folder input means "everything inside it" in either output mode."""

    _OSU = SpeedCollectionLayoutTests._OSU

    @staticmethod
    def _audio(path):
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ar", "44100", "-ac", "1", "-b:a", "128k", str(path),
            ],
            capture_output=True,
        )

    def _song(self, root, title, versions):
        folder = root / title
        folder.mkdir(parents=True, exist_ok=True)
        self._audio(folder / "audio.mp3")
        for version in versions:
            (folder / f"{version}.osu").write_text(
                self._OSU.format(title=title, version=version), encoding="utf-8"
            )
        return folder

    def _controller(self, root):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        return SpeedChangeController(), ToolContext(
            search_root=str(root), output_root=str(root), global_settings={}
        )

    def test_a_folder_input_creates_one_complete_sibling_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "Songs"
            self._song(songs, "Odo", ["Easy", "Hard"])
            self._song(songs, "Ghost", ["Insane"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": [str(songs)], "speed": 1.5}, context
            )
            destination = root / "Songs 1.5x"
            self.assertEqual(prepared.params["planned_outputs"], [str(destination)])
            before = {p.relative_to(songs): p.read_bytes() for p in songs.rglob("*") if p.is_file()}
            controller.run(prepared, lambda *_args: None)
            for relative in (
                "Odo/Easy 1.5x.osu", "Odo/Hard 1.5x.osu", "Ghost/Insane 1.5x.osu",
                "Odo/audio 1.5x.mp3", "Ghost/audio 1.5x.mp3",
            ):
                with self.subTest(relative=relative):
                    self.assertTrue((destination / relative).is_file(), relative)
            self.assertEqual({p.relative_to(songs): p.read_bytes() for p in songs.rglob("*") if p.is_file()}, before)
            self.assertFalse((destination / "Ghost" / "Easy 1.5x.osu").exists())
            self.assertFalse((destination / "Odo" / "Insane 1.5x.osu").exists())

    def test_a_folder_input_marks_metadata_and_internal_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "Songs"
            self._song(songs, "Odo", ["Hard"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {"input_paths": [str(songs)], "speed": 1.5}, context
            )
            controller.run(prepared, lambda *_args: None)
            text = (root / "Songs 1.5x" / "Odo" / "Hard 1.5x.osu").read_text(encoding="utf-8")
            self.assertIn("AudioFilename: audio 1.5x.mp3", text)
            self.assertIn("Version:Hard 1.5x", text)
            self.assertTrue((root / "Songs 1.5x" / "Odo" / "audio 1.5x.mp3").is_file())

    def test_the_input_field_decides_whether_the_song_name_is_marked(self):
        """Folder field marks the song name; file field leaves it as authored.

        The same chart is speed-changed twice: once as a folder input and once as
        a file input.  Only the folder run may touch ``Title``.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "Songs"
            folder = self._song(songs, "Odo", ["Hard"])
            chart = folder / "Hard.osu"
            marked = folder / "Hard 1.5x.osu"
            controller, context = self._controller(root)

            prepared = controller.prepare(
                {"input_folders": str(folder), "speed": 1.5}, context
            )
            controller.run(prepared, lambda *_args: None)
            from_folder = (songs / "Odo 1.5x" / "Hard 1.5x.osu").read_text(encoding="utf-8")
            self.assertIn("Title:Odo 1.5x", from_folder)
            self.assertIn("Version:Hard 1.5x", from_folder)

            prepared = controller.prepare(
                {"input_paths": str(chart), "speed": 1.5}, context
            )
            self.assertIsNone(prepared.confirmation)
            prepared.params["_allow_overwrite"] = True
            controller.run(prepared, lambda *_args: None)
            from_file = marked.read_text(encoding="utf-8")
            self.assertIn("Title:Odo\n", from_file)
            self.assertNotIn("Title:Odo 1.5x", from_file)
            # The file name and the difficulty name still say what changed.
            self.assertIn("Version:Hard 1.5x", from_file)

    def test_a_folder_and_a_chart_inside_it_do_not_duplicate_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "Songs"
            self._song(songs, "Odo", ["Easy", "Hard"])
            controller, context = self._controller(root)
            prepared = controller.prepare(
                {
                    "input_paths": [str(songs), str(songs / "Odo" / "Hard.osu")],
                    "speed": 1.5,
                },
                context,
            )
            self.assertEqual(len(prepared.params["input_paths"]), 2)
            self.assertEqual(len(prepared.params["planned_outputs"]), 1)

    def test_an_empty_folder_is_rejected(self):
        from osu_toolbox.schema import ParamValidationError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            controller, context = self._controller(root)
            with self.assertRaises(ParamValidationError) as caught:
                controller.prepare(
                    {"input_paths": [str(empty)], "speed": 1.5}, context
                )
            self.assertIn("没有可变速", str(caught.exception))

    def test_a_folder_input_still_works_with_the_workdir_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "inputs" / "Songs"
            self._song(songs, "Odo", ["Easy", "Hard"])
            controller, context = self._controller(root / "app")
            prepared = controller.prepare(
                {
                    "input_paths": [str(songs)],
                    "speed": 1.1,
                    "output_location": "workdir",
                },
                context,
            )
            destination = root / "app" / "Songs 1.1x"
            self.assertEqual(prepared.params["planned_outputs"], [str(destination)])
            controller.run(prepared, lambda *_args: None)
            self.assertTrue((destination / "Odo" / "Easy 1.1x.osu").is_file())
            self.assertTrue((destination / "Odo" / "Hard 1.1x.osu").is_file())
            self.assertFalse((songs / "Odo" / "Easy 1.1x.osu").exists())


class SpeedInputFieldTests(unittest.TestCase):
    """The form needs one field per dialog kind, because Windows offers no both.

    A single ``open_file`` field made folders unreachable: the browse button
    only ever opened a file dialog, so "folder support" existed in the core and
    was unreachable from the UI.
    """

    def test_the_form_marks_required_and_optional_sections(self):
        """The form must say what may be left alone.

        Seven of the nine fields run on defaults; without a marker the form
        reads as if every field must be filled in.
        """
        from osu_toolbox.tools.speed_change import SPEED_CHANGE_GROUPS, SPEED_CHANGE_PARAMS

        groups = {group.key: group for group in SPEED_CHANGE_GROUPS}
        self.assertEqual([group.key for group in SPEED_CHANGE_GROUPS], ["input", "speed", "output"])
        self.assertFalse(groups["input"].collapsible)
        self.assertTrue(groups["speed"].collapsible)
        self.assertTrue(groups["output"].collapsible)
        self.assertIn("至少填一项", groups["input"].label)
        for key in ("speed", "output"):
            with self.subTest(group=key):
                self.assertIn("可选", groups[key].label)
                self.assertIn("默认值", groups[key].label)

        marked = [spec for spec in SPEED_CHANGE_PARAMS if spec.required]
        self.assertEqual(marked, [], "no single field is mandatory; one of the two input fields is")
        # Only the input group is on the always-open form, and its fields carry
        # text hints for what an empty box does.
        for key in ("input_paths", "input_folders"):
            spec = next(item for item in SPEED_CHANGE_PARAMS if item.key == key)
            with self.subTest(field=key):
                self.assertEqual(spec.group, "input")
                self.assertTrue(spec.placeholder)
                self.assertIn("至少填一个", spec.help)

    def test_the_form_marks_the_pattern_hints(self):
        from osu_toolbox.tools.speed_change import SPEED_CHANGE_PARAMS

        for key, needle in (
            ("file_suffix", "{num}x"),
            ("difficulty_marker_pattern", "none"),
            ("output_folder", "必填"),
        ):
            with self.subTest(field=key):
                spec = next(item for item in SPEED_CHANGE_PARAMS if item.key == key)
                self.assertIn(needle, spec.placeholder)

    def test_the_form_separates_files_from_folders(self):
        from osu_toolbox.tools.speed_change import SPEED_CHANGE_PARAMS

        files = next(item for item in SPEED_CHANGE_PARAMS if item.key == "input_paths")
        folders = next(item for item in SPEED_CHANGE_PARAMS if item.key == "input_folders")
        self.assertEqual(files.dialog_mode, "open_file")
        self.assertTrue(files.multiple)
        self.assertTrue(files.filters)
        self.assertEqual(folders.dialog_mode, "folder")
        self.assertTrue(folders.multiple, "one folder at a time is not batch work")
        self.assertFalse(folders.required)
        self.assertEqual(folders.group, "input")
        self.assertIn("合并", folders.help)

    def _build(self, root):
        songs = root / "Songs"
        for title, versions in (("Odo", ["Easy", "Hard"]), ("Ghost", ["Insane"])):
            folder = songs / title
            folder.mkdir(parents=True, exist_ok=True)
            for version in versions:
                (folder / f"{version}.osu").write_text(
                    _osu_text(version=version), encoding="utf-8"
                )
        single = root / "single"
        single.mkdir(parents=True, exist_ok=True)
        (single / "Solo.osu").write_text(_osu_text(version="Solo"), encoding="utf-8")
        return songs, single

    def _prepare(self, root, raw):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        return SpeedChangeController().prepare(
            {"speed": 1.1, **raw},
            ToolContext(search_root=str(root), output_root=str(root), global_settings={}),
        )

    def _names(self, root, prepared):
        return sorted(
            str(Path(item).relative_to(root)) for item in prepared.params["input_paths"]
        )

    def test_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs, single = self._build(root)
            prepared = self._prepare(root, {
                "input_paths": [str(songs / "Odo" / "Easy.osu"), str(single / "Solo.osu")],
            })
            self.assertEqual(
                self._names(root, prepared),
                [r"Songs\Odo\Easy.osu", r"single\Solo.osu"],
            )

    def test_folders_only_expand_recursively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs, _single = self._build(root)
            prepared = self._prepare(root, {"input_folders": [str(songs)]})
            self.assertEqual(
                self._names(root, prepared),
                [
                    r"Songs\Ghost\Insane.osu",
                    r"Songs\Odo\Easy.osu",
                    r"Songs\Odo\Hard.osu",
                ],
            )

    def test_both_fields_are_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs, single = self._build(root)
            prepared = self._prepare(root, {
                "input_paths": [str(single / "Solo.osu")],
                "input_folders": [str(songs)],
            })
            self.assertEqual(
                self._names(root, prepared),
                [
                    r"Songs\Ghost\Insane.osu",
                    r"Songs\Odo\Easy.osu",
                    r"Songs\Odo\Hard.osu",
                    r"single\Solo.osu",
                ],
            )

    def test_a_folder_and_a_chart_inside_it_are_merged_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs, _single = self._build(root)
            prepared = self._prepare(root, {
                "input_paths": [str(songs / "Odo" / "Hard.osu")],
                "input_folders": [str(songs)],
            })
            self.assertEqual(
                self._names(root, prepared),
                [
                    r"Songs\Ghost\Insane.osu",
                    r"Songs\Odo\Easy.osu",
                    r"Songs\Odo\Hard.osu",
                ],
            )

    def test_both_fields_empty_is_rejected(self):
        from osu_toolbox.schema import ParamValidationError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._build(root)
            with self.assertRaises(ParamValidationError) as caught:
                self._prepare(root, {})
            self.assertEqual(caught.exception.key, "input_paths")

    def test_an_empty_folder_names_the_folder_field(self):
        from osu_toolbox.schema import ParamValidationError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            with self.assertRaises(ParamValidationError) as caught:
                self._prepare(root, {"input_folders": [str(empty)]})
            self.assertEqual(caught.exception.key, "input_folders")

    def test_a_folder_field_accepts_multi_line_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs, _single = self._build(root)
            prepared = self._prepare(root, {
                "input_folders": f"{songs / 'Odo'}\n{songs / 'Ghost'}",
            })
            self.assertEqual(len(prepared.params["input_paths"]), 3)

    def test_the_command_line_still_passes_files_directly(self):
        """``input_folders`` is a form field; the shell entry uses paths only."""
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs, _single = self._build(root)
            prepared = SpeedChangeController().prepare(
                {"input_paths": [str(songs / "Odo" / "Easy.osu")], "speed": 1.5},
                ToolContext(
                    search_root=str(root), output_root=str(root), global_settings={}
                ),
            )
            self.assertEqual(len(prepared.params["input_paths"]), 1)


class SpeedPathResolutionTests(unittest.TestCase):
    """A path the user picked is used literally, never shell-expanded.

    ``expandvars``/``expanduser`` corrupted real folder names: a folder called
    ``100%TEMP%`` became ``100C:\\Users\\...\\Temp``, which is neither a folder
    nor a file, so a folder-only run failed with "输入文件不存在" pointing at the
    wrong field.
    """

    def _song(self, root, name):
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Hard.osu").write_text(_osu_text(), encoding="utf-8")
        return folder

    def test_a_literal_name_with_percent_survives(self):
        from osu_toolbox.tools.speed_change import _resolve_user_path

        with tempfile.TemporaryDirectory() as directory:
            folder = self._song(Path(directory), "100%TEMP%")
            self.assertEqual(_resolve_user_path(str(folder)), str(folder))

    def test_a_literal_name_with_tilde_survives(self):
        from osu_toolbox.tools.speed_change import _resolve_user_path

        with tempfile.TemporaryDirectory() as directory:
            folder = self._song(Path(directory), "My ~ Songs")
            self.assertEqual(_resolve_user_path(str(folder)), str(folder))

    def test_a_literal_name_with_a_percent_inside_a_song_folder(self):
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            folder = self._song(Path(directory), "Song 50%off & ~more")
            values = _collect_input_values({"input_folders": str(folder)})
            expanded, _roots = _expand_input_values(values)
            self.assertEqual([Path(item).name for item in expanded], ["Hard.osu"])

    def test_environment_variables_still_expand_as_a_fallback(self):
        from osu_toolbox.tools.speed_change import _resolve_user_path

        with tempfile.TemporaryDirectory() as directory:
            folder = self._song(Path(directory), "Songs")
            with patch.dict(os.environ, {"OSU_TOOLBOX_TEST_DIR": str(folder)}):
                self.assertEqual(
                    _resolve_user_path("%OSU_TOOLBOX_TEST_DIR%"), str(folder)
                )

    def test_a_literal_path_wins_over_an_expansion(self):
        """A real folder must never be redirected by an env var of the same text."""
        from osu_toolbox.tools.speed_change import _resolve_user_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            literal = self._song(root, "%OSU_TOOLBOX_TEST_DIR%")
            other = self._song(root, "Other")
            with patch.dict(os.environ, {"OSU_TOOLBOX_TEST_DIR": str(other)}):
                self.assertEqual(_resolve_user_path(str(literal)), str(literal))

    def test_a_missing_folder_names_the_folder_field(self):
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "nope")
            with self.assertRaises(ParamValidationError) as caught:
                _expand_input_values(_collect_input_values({"input_folders": missing}))
            self.assertEqual(caught.exception.key, "input_folders")
            self.assertIn("路径不存在", str(caught.exception))
            self.assertIn("重新选择文件夹", str(caught.exception))

    def test_a_missing_file_names_the_file_field(self):
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "nope.osu")
            with self.assertRaises(ParamValidationError) as caught:
                _expand_input_values(_collect_input_values({"input_paths": missing}))
            self.assertEqual(caught.exception.key, "input_paths")
            self.assertIn("路径不存在", str(caught.exception))

    def test_an_unsupported_extension_says_what_is_supported(self):
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "cover.jpg"
            other.write_bytes(b"image")
            with self.assertRaises(ParamValidationError) as caught:
                _expand_input_values(_collect_input_values({"input_paths": str(other)}))
            self.assertIn(".osu", str(caught.exception))
            self.assertIn(".mcz", str(caught.exception))

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_a_folder_only_run_through_the_tool_succeeds(self):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "Songs"
            self._song(songs, "Odo")
            self._song(songs, "100%TEMP%")
            for name in ("Odo", "100%TEMP%"):
                subprocess.run(
                    [
                        FFMPEG, "-hide_banner", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ar", "44100", "-ac", "1", str(songs / name / "audio.mp3"),
                    ],
                    capture_output=True,
                )
            controller = SpeedChangeController()
            prepared = controller.prepare(
                {"input_folders": str(songs), "speed": 1.5},
                ToolContext(
                    search_root=str(root), output_root=str(root), global_settings={}
                ),
            )
            controller.run(prepared, lambda *_args: None)
            self.assertTrue((root / "Songs 1.5x" / "Odo" / "Hard 1.5x.osu").is_file())
            self.assertTrue((root / "Songs 1.5x" / "100%TEMP%" / "Hard 1.5x.osu").is_file())


    def test_multi_select_results_are_never_glued_together(self):
        """A multi-select list must stay separate values.

        The reported failure was five chosen folders arriving as one string with
        no separators -- ``D:\\maps\\aD:\\maps\\bD:\\maps\\c`` -- which is how a JS
        array stringifies when it is assigned to ``input.value`` directly.  Every
        picked folder was silently lost and the message only said "路径不存在".
        """
        import json as json_module

        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folders = [self._song(root, name) for name in ("A", "B", "C")]
            expected = len(folders)  # one chart each
            for label, raw in (
                ("list", [str(item) for item in folders]),
                ("json string", json_module.dumps([str(item) for item in folders])),
                ("newline string", "\n".join(str(item) for item in folders)),
                ("nested list", [[str(item)] for item in folders]),
                ("set", {str(item) for item in folders}),
            ):
                with self.subTest(form=label):
                    values = _collect_input_values({"input_folders": raw})
                    self.assertEqual(len(values), len(folders), label)
                    expanded, _roots = _expand_input_values(values)
                    self.assertEqual(len(expanded), expected, label)

    def test_a_glued_multi_select_is_recovered_not_discarded(self):
        """Five folders glued into one string must still be processed.

        The frontend bug that produced ``aD:\\bD:\\c`` is fixed, but a client
        that still sends it must not lose the whole selection: when every piece
        exists, the split is certainly the intended reading.
        """
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
            _split_concatenated_paths,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folders = [self._song(root, name) for name in ("A", "B", "C")]
            glued = "".join(str(item) for item in folders)
            recovered = _split_concatenated_paths(glued)
            self.assertEqual(recovered, [str(item) for item in folders])

            values = _collect_input_values({"input_folders": glued})
            self.assertEqual(len(values), 3)
            expanded, _roots = _expand_input_values(values)
            self.assertEqual(len(expanded), 3)

    def test_a_glued_string_with_a_missing_piece_is_not_split(self):
        """A split that produces a non-existent path is not a real split."""
        from osu_toolbox.tools.speed_change import _split_concatenated_paths

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self._song(root, "A")
            glued = f"{folder}{root / 'does-not-exist'}"
            self.assertIsNone(_split_concatenated_paths(glued))

    def test_normal_paths_are_never_split(self):
        from osu_toolbox.tools.speed_change import _split_concatenated_paths

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self._song(root, "Odo")
            self.assertIsNone(_split_concatenated_paths(str(folder)))
            self.assertIsNone(_split_concatenated_paths(str(root)))
            self.assertIsNone(_split_concatenated_paths(r"\\server\share\maps"))

    def test_a_glued_path_is_recognised_and_explained(self):
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
            _looks_like_concatenated_paths,
        )

        base = r"D:\malody\beatmap"
        glued = base + r"\_song_5022" + base + r"\_song_6000" + base + r"\ANOTHER"
        self.assertTrue(_looks_like_concatenated_paths(glued))
        self.assertFalse(_looks_like_concatenated_paths(base + r"\_song_5022"))
        self.assertFalse(_looks_like_concatenated_paths(r"\\server\share\maps"))

        with self.assertRaises(ParamValidationError) as caught:
            _expand_input_values(_collect_input_values({"input_folders": glued}))
        message = str(caught.exception)
        self.assertIn("拼接成了一个", message)
        self.assertIn("重新用「浏览」选择", message)

    def test_a_missing_path_error_shows_what_was_received(self):
        """The diagnosis depends on the exact submitted string.

        A trailing space, a stray quote or a second line pasted into the box all
        render the same in the form but are different values by the time they
        reach the validator, so the message has to quote what arrived.
        """
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "nope")
            with self.assertRaises(ParamValidationError) as caught:
                _expand_input_values(_collect_input_values({"input_folders": missing}))
            message = str(caught.exception)
            self.assertIn("文件夹", message, "the owning field must be named")
            self.assertIn("路径不存在", message)
            self.assertIn("收到的值", message)
            self.assertIn(missing, message)

    def test_two_paths_glued_into_one_box_are_reported(self):
        """A multi-line value that lost its newline becomes one invalid path."""
        from osu_toolbox.schema import ParamValidationError
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._song(root, "Odo")
            second = self._song(root, "Ghost")
            # Copied from a terminal, the separator can arrive as a literal
            # backslash-n rather than a line break.
            glued = f"{first}\\n{second}"
            with self.assertRaises(ParamValidationError) as caught:
                _expand_input_values(_collect_input_values({"input_folders": glued}))
            self.assertIn("收到的值", str(caught.exception))
            # A real line break is still two folders.
            expanded, _roots = _expand_input_values(
                _collect_input_values({"input_folders": f"{first}\n{second}"})
            )
            self.assertEqual(len(expanded), 2)

    def test_trailing_whitespace_is_tolerated(self):
        from osu_toolbox.tools.speed_change import (
            _collect_input_values,
            _expand_input_values,
        )

        with tempfile.TemporaryDirectory() as directory:
            folder = self._song(Path(directory), "Odo")
            for value in (f"  {folder}  ", f'"{folder}"', f"{folder}\\", f"{folder}/"):
                with self.subTest(value=value):
                    expanded, _roots = _expand_input_values(
                        _collect_input_values({"input_folders": value})
                    )
                    self.assertEqual(len(expanded), 1, value)

    def test_prepare_reports_how_many_inputs_were_resolved(self):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            songs = root / "Songs"
            self._song(songs, "Odo")
            self._song(songs, "Ghost")
            prepared = SpeedChangeController().prepare(
                {"input_folders": str(songs), "speed": 1.5},
                ToolContext(
                    search_root=str(root), output_root=str(root), global_settings={}
                ),
            )
            summary = prepared.warnings[0]
            self.assertIn("1 项", summary)
            self.assertIn("2 个谱面/归档", summary)


class SpeedShellMenuTests(unittest.TestCase):
    def test_menu_declares_speed_items_for_files_and_archives_only(self):
        from osu_toolbox.quick import SUPPORTED_OPERATIONS
        from osu_toolbox.shell_menu import SHELL_MENU_ITEMS

        items = {item.operation: item for item in SHELL_MENU_ITEMS}
        plain = items["speed-change"]
        self.assertEqual(plain.title, "变速")
        self.assertEqual(plain.extensions, (".osu", ".mc", ".sm"))
        self.assertFalse(plain.folder)
        archive = items["archive-speed-change"]
        self.assertEqual(archive.extensions, (".osz", ".mcz"))
        # 变速 must never register a folder entry: the Explorer menu passes one
        # path, and bulk/folder work belongs to the GUI.
        self.assertFalse(archive.folder)
        self.assertFalse(archive.folder_children)
        self.assertEqual(archive.target_keys, (".osz", ".mcz"))
        self.assertEqual(plain.target_keys, (".osu", ".mc", ".sm"))
        for item in SHELL_MENU_ITEMS:
            self.assertIn(item.operation, SUPPORTED_OPERATIONS)

    def test_speed_operations_register_their_own_input_scopes(self):
        from osu_toolbox.shell_menu import SHELL_MENU_ITEMS
        for item in SHELL_MENU_ITEMS:
            if not item.operation.endswith("speed-change"):
                continue
            with self.subTest(operation=item.operation):
                self.assertEqual(item.folder, item.operation == "folder-speed-change")
                self.assertEqual("folder" in item.target_keys, item.folder)

    def test_speed_submenu_carries_the_presets_and_a_custom_entry(self):
        from osu_toolbox.shell_menu import shell_menu_schema

        schema = shell_menu_schema(r"C:\x\osu-toolbox-menu.exe")
        items = {item["operation"]: item for item in schema["items"]}
        children = items["speed-change"]["submenu"]
        self.assertEqual(
            [child["target_format"] for child in children],
            ["0.9", "0.95", "1.05", "1.1", ""],
        )
        self.assertEqual(
            [child["title"] for child in children],
            ["变速 0.9x", "变速 0.95x", "变速 1.05x", "变速 1.1x", "自定义倍率…"],
        )
        preset = next(child for child in children if child["target_format"] == "1.5" or child["target_format"] == "1.05")
        self.assertIn("--target-format 1.05", preset["command"])
        custom = children[-1]
        self.assertTrue(custom["prompt_speed"])
        self.assertIn("--prompt-speed", custom["command"])

    def test_archive_speed_submenu_is_archive_labelled(self):
        from osu_toolbox.shell_menu import shell_menu_schema

        schema = shell_menu_schema(r"C:\x\osu-toolbox-menu.exe")
        items = {item["operation"]: item for item in schema["items"]}
        archive = items["archive-speed-change"]
        # Archive targets are file associations, not a folder menu.
        self.assertFalse(archive["folder"])
        self.assertEqual(archive["file_types"], ["*.osz", "*.mcz"])
        self.assertEqual(
            [child["title"] for child in archive["submenu"]],
            [
                "变速 0.9x（归档）", "变速 0.95x（归档）", "变速 1.05x（归档）",
                "变速 1.1x（归档）", "自定义倍率…（归档）",
            ],
        )
        # Folder conversion and folder speed share the same folder scope.
        self.assertTrue(items["folder-convert"]["folder"])
        self.assertEqual(items["folder-convert"]["file_types"], ["目录"])

    def test_folder_speed_menu_uses_the_same_presets_and_folder_path(self):
        from osu_toolbox.shell_menu import shell_menu_schema
        items = {item["operation"]: item for item in shell_menu_schema()["items"]}
        folder = items["folder-speed-change"]
        self.assertTrue(folder["folder"])
        self.assertEqual(folder["setting_key"], "shell_menu_speed_change")
        self.assertEqual(folder["file_types"], ["目录"])
        self.assertEqual([child["target_format"] for child in folder["submenu"]],
                         ["0.9", "0.95", "1.05", "1.1", ""])
        for child in folder["submenu"]:
            self.assertTrue(child["command"].endswith('"%V"'))
        self.assertIn("--prompt-speed", folder["submenu"][-1]["command"])

    def test_speed_setting_registers_under_the_shell_menu_switch(self):
        from osu_toolbox.shell_menu import (
            SHELL_MENU_SETTING_PARAMS,
            default_shell_menu_settings,
        )

        keys = {spec.key for spec in SHELL_MENU_SETTING_PARAMS}
        self.assertIn("shell_menu_speed_change", keys)
        self.assertFalse(default_shell_menu_settings()["shell_menu_speed_change"])

    def test_registry_writes_speed_submenu_commands(self):
        from tests.test_quick_shell import _FakeWinreg
        from osu_toolbox.shell_menu import ShellMenuManager

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "osu-toolbox-menu.exe"
            executable.write_bytes(b"launcher")
            registry = _FakeWinreg()
            manager = ShellMenuManager(str(executable), registry)
            result = manager.apply({
                "shell_menu_enabled": True,
                "shell_menu_speed_change": True,
            })
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "enabled")
            child = manager.submenu_command_key_path("speed-change", ".osu", "speed-105")
            self.assertIn("--target-format 1.05", registry.values[child][""])
            custom = manager.submenu_command_key_path("speed-change", ".osu", "speed-custom")
            self.assertIn("--prompt-speed", registry.values[custom][""])
            # File and archive operations keep their association scopes;
            # the folder operation owns the Directory and Background keys.
            self.assertIn(
                manager.key_path("archive-speed-change", ".osz"), registry.values
            )
            for operation in ("speed-change", "archive-speed-change"):
                with self.subTest(operation=operation):
                    with self.assertRaises(ValueError):
                        manager.key_path(operation, "folder")
                    with self.assertRaises(ValueError):
                        manager.background_key_path(operation)
            folder = manager.key_path("folder-speed-change", "folder")
            self.assertIn(folder, registry.values)
            self.assertIn(manager.background_key_path("folder-speed-change"), registry.values)
            folder_command = manager.submenu_command_key_path("folder-speed-change", "folder", "speed-105")
            self.assertIn('--operation folder-speed-change', registry.values[folder_command][""])
            self.assertIn('--target-format 1.05', registry.values[folder_command][""])
            self.assertTrue(registry.values[folder_command][""].endswith('"%V"'))
            manager.uninstall_all()
            self.assertFalse(registry.values)


class SpeedFolderMenuLayoutTests(unittest.TestCase):
    def _prepare(self, source, settings=None, **raw):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.speed_change import SpeedChangeController
        controller = SpeedChangeController()
        context = ToolContext(str(source.parent), str(source.parent), settings or {}, invocation="shell")
        params = {"input_folders": str(source), "speed": 1.1, **raw}
        return controller, controller.prepare(params, context)

    @staticmethod
    def _fake_audio(_registry, source, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"converted:" + Path(source).read_bytes())

    def test_folder_and_file_names_and_metadata_use_their_own_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song.with.dot"
            root.mkdir()
            original = _osu_text().replace("Title:T\n", "Title:T\nTitleUnicode:歌曲\n")
            (root / "hard.osu").write_text(original, encoding="utf-8")
            (root / "song.wav").write_bytes(b"audio")
            (root / "Empty" / "Nested").mkdir(parents=True)
            (root / "image.png").write_bytes(b"image")
            controller, prepared = self._prepare(root, {
                "speed_file_suffix": " [rate {num}]",
                "speed_difficulty_marker": " ({num}x)",
            })
            destination = root.parent / "Song.with.dot [rate 1.1]"
            self.assertEqual(prepared.params["planned_outputs"], [str(destination)])
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", self._fake_audio):
                result = controller.run(prepared, lambda *_: None)
            self.assertEqual(result.data["paths"], [str(destination)])
            text = (destination / "hard [rate 1.1].osu").read_text(encoding="utf-8")
            self.assertIn("Title:T (1.1x)", text)
            self.assertIn("TitleUnicode:歌曲 (1.1x)", text)
            self.assertIn("Version:Hard (1.1x)", text)
            self.assertIn("AudioFilename: song [rate 1.1].wav", text)
            self.assertEqual((destination / "song [rate 1.1].wav").read_bytes(), b"converted:audio")
            self.assertEqual((destination / "image.png").read_bytes(), b"image")
            self.assertTrue((destination / "Empty" / "Nested").is_dir())
            self.assertEqual((root / "hard.osu").read_text(encoding="utf-8"), original)
            self.assertEqual((root / "song.wav").read_bytes(), b"audio")

    def test_entire_library_keeps_subdirectories_shared_audio_and_all_chart_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Library"
            song = root / "Song"
            storage = song / "0"
            storage.mkdir(parents=True)
            (song / "shared.wav").write_bytes(b"shared")
            (root / "library.txt").write_bytes(b"library note")
            (song / "hard.osu").write_text(_osu_text(audio="shared.wav"), encoding="utf-8")
            mc = MalodyTimelineTests()._document(song={"title": "T", "file": "../shared.wav"})
            mc = mc.replace("song.mp3", "../shared.wav")
            (storage / "0.mc").write_text(mc, encoding="utf-8")
            sm = StepManiaTimelineTests._SM.replace("song.ogg", "shared.wav")
            (song / "chart.sm").write_text(sm, encoding="utf-8")
            controller, prepared = self._prepare(root)
            calls = []
            def convert(registry, source, target):
                calls.append(str(source))
                self._fake_audio(registry, source, target)
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", convert):
                controller.run(prepared, lambda *_: None)
            destination = root.parent / "Library 1.1x"
            out_song = destination / "Song"
            self.assertEqual(calls, [str(song / "shared.wav")])
            self.assertEqual((destination / "library.txt").read_bytes(), b"library note")
            out_mc = json.loads((out_song / "0" / "0 1.1x.mc").read_text(encoding="utf-8"))
            self.assertEqual(out_mc["meta"]["song"]["title"], "T 1.1x")
            self.assertEqual(out_mc["meta"]["version"], "Hard 1.1x")
            self.assertEqual(out_mc["meta"]["song"]["file"], "../shared 1.1x.wav")
            out_sm = (out_song / "chart 1.1x.sm").read_text(encoding="utf-8")
            self.assertIn("#TITLE:Test 1.1x;", out_sm)
            self.assertIn("Hard 1.1x:", out_sm)
            self.assertIn("#MUSIC:shared 1.1x.wav;", out_sm)
            renamed = {
                Path("Song/hard.osu"): Path("Song/hard 1.1x.osu"),
                Path("Song/chart.sm"): Path("Song/chart 1.1x.sm"),
                Path("Song/0/0.mc"): Path("Song/0/0 1.1x.mc"),
                Path("Song/shared.wav"): Path("Song/shared 1.1x.wav"),
            }
            self.assertEqual({p.relative_to(destination) for p in destination.rglob("*")},
                             {renamed.get(p.relative_to(root), p.relative_to(root)) for p in root.rglob("*")})

    def test_archive_members_are_renamed_with_paths_and_other_assets_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Packs"
            root.mkdir()
            mc = MalodyTimelineTests()._document(song={"title": "T", "file": "../song.wav"})
            mc = mc.replace("song.mp3", "../song.wav")
            for extension, chart_name, chart_text in (
                (".osz", "easy.osu", _osu_text(audio="../song.wav")),
                (".mcz", "0.mc", mc),
            ):
                with zipfile.ZipFile(root / ("pack" + extension), "w") as handle:
                    handle.writestr("charts/" + chart_name, chart_text)
                    handle.writestr("song.wav", b"audio")
                    handle.writestr("unused.wav", b"unused")
                    handle.writestr("art/image.png", b"image")
                    handle.writestr("empty/", b"")
            controller, prepared = self._prepare(root)
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", self._fake_audio):
                controller.run(prepared, lambda *_: None)
            destination = root.parent / "Packs 1.1x"
            for extension, chart_name in ((".osz", "easy.osu"), (".mcz", "0.mc")):
                with zipfile.ZipFile(root / ("pack" + extension)) as original:
                    names = original.namelist()
                with zipfile.ZipFile(destination / ("pack 1.1x" + extension)) as handle:
                    renamed = {
                        "charts/" + chart_name: "charts/" + Path(chart_name).stem + " 1.1x" + Path(chart_name).suffix,
                        "song.wav": "song 1.1x.wav",
                    }
                    self.assertEqual(set(handle.namelist()), {renamed.get(name, name) for name in names})
                    self.assertEqual(handle.read("song 1.1x.wav"), b"converted:audio")
                    self.assertEqual(handle.read("unused.wav"), b"unused")
                    self.assertEqual(handle.read("art/image.png"), b"image")
                    text = handle.read(renamed["charts/" + chart_name]).decode("utf-8")
                    if extension == ".osz":
                        self.assertIn("Title:T 1.1x", text)
                        self.assertIn("Version:Hard 1.1x", text)
                        self.assertIn("AudioFilename: ../song 1.1x.wav", text)
                    else:
                        data = json.loads(text)
                        self.assertEqual(data["meta"]["song"]["title"], "T 1.1x")
                        self.assertEqual(data["meta"]["version"], "Hard 1.1x")
                        self.assertEqual(data["meta"]["song"]["file"], "../song 1.1x.wav")

    def test_numbered_storage_folder_brings_its_parent_audio_into_the_new_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / "Song"
            root = song / "0"
            root.mkdir(parents=True)
            (song / "song.mp3").write_bytes(b"audio")
            mc = MalodyTimelineTests()._document(song={"title": "T", "file": "../song.mp3"})
            mc = mc.replace('"sound": "song.mp3"', '"sound": "../song.mp3"')
            (root / "0.mc").write_text(mc, encoding="utf-8")
            controller, prepared = self._prepare(root)
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", self._fake_audio):
                controller.run(prepared, lambda *_: None)
            destination = song / "0 1.1x"
            self.assertEqual(prepared.params["planned_outputs"], [str(destination)])
            self.assertEqual((destination / "song 1.1x.mp3").read_bytes(), b"converted:audio")
            result = json.loads((destination / "0 1.1x.mc").read_text(encoding="utf-8"))
            self.assertEqual(result["meta"]["song"]["file"], "song 1.1x.mp3")
            self.assertEqual((song / "song.mp3").read_bytes(), b"audio")

    def test_existing_folders_and_files_are_skipped_with_a_counter_before_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "hard.osu").write_text(_osu_text(), encoding="utf-8")
            (root.parent / "Song 1.1x").mkdir()
            (root.parent / "Song 2 1.1x").write_bytes(b"existing file")
            _controller, prepared = self._prepare(root)
            self.assertEqual(prepared.params["planned_outputs"], [str(root.parent / "Song 3 1.1x")])
            self.assertIsNone(prepared.confirmation)
            self.assertEqual((root.parent / "Song 2 1.1x").read_bytes(), b"existing file")

    def test_a_folder_appearing_after_preview_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "hard.osu").write_text(_osu_text(), encoding="utf-8")
            controller, prepared = self._prepare(root)
            destination = Path(prepared.params["planned_outputs"][0])
            destination.mkdir()
            (destination / "keep.txt").write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                controller.run(prepared, lambda *_: None)
            self.assertEqual(list(destination.iterdir()), [destination / "keep.txt"])
            self.assertEqual((destination / "keep.txt").read_bytes(), b"keep")

    def test_an_explicit_output_inside_the_source_is_rejected_before_writing(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "hard.osu").write_text(_osu_text(), encoding="utf-8")
            error = io.StringIO()
            code = main(["--context", "shell", "--operation", "folder-speed-change",
                         "--target-format", "1.1", "--output-directory", str(root / "out"),
                         str(root)], stderr=error)
            self.assertEqual(code, 2)
            self.assertIn("原文件夹之外", error.getvalue())
            self.assertFalse((root / "out").exists())

    def test_folder_metadata_still_gets_a_marker_when_the_difficulty_setting_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "hard.osu").write_text(_osu_text(), encoding="utf-8")
            controller, prepared = self._prepare(root, {
                "speed_file_suffix": " ({num}x)", "speed_difficulty_marker": "",
            })
            controller.run(prepared, lambda *_: None)
            text = (root.parent / "Song (1.1x)" / "hard (1.1x).osu").read_text(encoding="utf-8")
            self.assertIn("Title:T (1.1x)", text)
            self.assertIn("Version:Hard (1.1x)", text)


    def test_folder_and_archive_outputs_have_the_same_members_and_contents(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            (root / "Charts").mkdir(parents=True)
            (root / "Assets").mkdir()
            (root / "Empty").mkdir()
            (root / "Assets" / "song.wav").write_bytes(b"audio")
            (root / "Assets" / "unused.wav").write_bytes(b"unused")
            (root / "Assets" / "image.jpg").write_bytes(b"image")
            (root / "Charts" / "easy.osu").write_text(_osu_text(audio="../Assets/song.wav"), encoding="utf-8")
            mc = MalodyTimelineTests()._document(song={"title": "T", "file": "../Assets/song.wav"})
            (root / "Charts" / "hard.mc").write_text(mc.replace("song.mp3", "../Assets/song.wav"), encoding="utf-8")
            sm = StepManiaTimelineTests._SM.replace("song.ogg", "../Assets/song.wav")
            (root / "Charts" / "chart.sm").write_text(sm, encoding="utf-8")
            archive = root.with_suffix(".osz")
            original = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with zipfile.ZipFile(archive, "w") as handle:
                for name, content in original.items():
                    handle.writestr(name, content)
                handle.writestr("Empty/", b"")
            archive_bytes = archive.read_bytes()
            settings = {"speed_file_suffix": " [{num}x]", "speed_difficulty_marker": " ({num}x)"}
            with patch("osu_toolbox.quick._load_global_settings", return_value=settings), patch(
                "osu_toolbox.speed_change._AudioJobRegistry.ensure", self._fake_audio
            ):
                for operation, source in (("folder-speed-change", root), ("archive-speed-change", archive)):
                    error = io.StringIO()
                    code = main(["--context", "shell", "--operation", operation, "--target-format", "1.1",
                                 str(source)], stdout=io.StringIO(), stderr=error)
                    self.assertEqual(code, 0, error.getvalue())
            destination = root.parent / "Song [1.1x]"
            renamed = {
                "Charts/easy.osu": "Charts/easy [1.1x].osu",
                "Charts/hard.mc": "Charts/hard [1.1x].mc",
                "Charts/chart.sm": "Charts/chart [1.1x].sm",
                "Assets/song.wav": "Assets/song [1.1x].wav",
            }
            with zipfile.ZipFile(root.parent / "Song [1.1x].osz") as handle:
                self.assertEqual({name for name in handle.namelist() if not name.endswith("/")},
                                 {renamed.get(name, name) for name in original})
                self.assertIn("Empty/", handle.namelist())
                for name in original:
                    folder_bytes = (destination / renamed.get(name, name)).read_bytes()
                    archive_content = handle.read(renamed.get(name, name))
                    if name.endswith((".osu", ".mc", ".sm")):
                        self.assertEqual(folder_bytes.splitlines(), archive_content.splitlines(), name)
                    else:
                        self.assertEqual(folder_bytes, archive_content, name)
            self.assertEqual({p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}, original)
            self.assertEqual(archive.read_bytes(), archive_bytes)

    def test_collision_names_and_audio_references_match_the_preview_for_folders_and_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "hard.osu").write_text(
                _osu_text().replace("Version:Hard", "Version:Hard\nBeatmapID:123456\nBeatmapSetID:654321"),
                encoding="utf-8",
            )
            (root / "hard 1.1x.osu").write_text(
                _osu_text(audio="song 1.1x.wav", version="Previous"), encoding="utf-8"
            )
            (root / "song.wav").write_bytes(b"original")
            (root / "song 1.1x.wav").write_bytes(b"previous")
            originals = {p.name: p.read_bytes() for p in root.iterdir()}
            archive = root.with_suffix(".osz")
            with zipfile.ZipFile(archive, "w") as handle:
                for name, content in originals.items():
                    handle.writestr(name, content)
            controller, prepared = self._prepare(root)
            archive_plan = plan_speed_change(
                [archive], 1.1, file_pattern=" {num}x", difficulty_pattern=" {num}x"
            )
            calls = []

            def convert(registry, source, target):
                calls.append(Path(source).name)
                self._fake_audio(registry, source, target)

            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", convert):
                result = controller.run(prepared, lambda *_: None)
                converted = change_speed(
                    [archive], 1.1, file_pattern=" {num}x", difficulty_pattern=" {num}x"
                )
            self.assertEqual(result.data["errors"], [])
            self.assertEqual(converted["failures"], [])
            self.assertCountEqual(calls, ["song.wav", "song 1.1x.wav"] * 2)
            expected_names = {
                "hard 2 1.1x.osu", "hard 3 1.1x.osu",
                "song 2 1.1x.wav", "song 3 1.1x.wav",
            }
            destination = Path(result.data["paths"][0])
            self.assertEqual({p.name for p in destination.iterdir()}, expected_names)
            planned_names = {
                Path(path).name for kind, path in prepared.params["plan"][0]["rows"]
                if kind in ("谱面", "音乐")
            }
            self.assertEqual(planned_names, expected_names)
            planned_members = {
                path.removeprefix("（归档内）") for kind, path in archive_plan[0]["rows"]
                if kind in ("谱面", "音乐")
            }
            self.assertEqual(planned_members, expected_names)
            with zipfile.ZipFile(archive.with_name("Song 1.1x.osz")) as handle:
                self.assertEqual(set(handle.namelist()), expected_names)
                self.assertEqual(len(handle.namelist()), len(expected_names))
                for name in expected_names:
                    folder_content = (destination / name).read_bytes()
                    if name.endswith(".osu"):
                        self.assertEqual(folder_content.splitlines(), handle.read(name).splitlines())
                        text = folder_content.decode("utf-8")
                        reference = next(line.split(":", 1)[1].strip() for line in text.splitlines()
                                         if line.startswith("AudioFilename:"))
                        expected_audio = b"converted:previous" if "Version:Previous" in text else b"converted:original"
                        if "Version:Hard" in text:
                            self.assertIn("BeatmapID:0", text.splitlines())
                            self.assertIn("BeatmapSetID:-1", text.splitlines())
                        self.assertEqual((destination / reference).read_bytes(), expected_audio)
                        self.assertEqual(handle.read(reference), expected_audio)
                    else:
                        self.assertEqual(folder_content, handle.read(name))
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, originals)

    def test_a_loose_chart_matching_an_archive_member_is_processed_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "easy.osu").write_text(_osu_text(version="Loose"), encoding="utf-8")
            (root / "song.wav").write_bytes(b"audio")
            with zipfile.ZipFile(root / "pack.osz", "w") as handle:
                handle.writestr("easy.osu", _osu_text(version="Packed"))
                handle.writestr("song.wav", b"audio")
            controller, prepared = self._prepare(root)
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", self._fake_audio):
                controller.run(prepared, lambda *_: None)
            destination = root.parent / "Song 1.1x"
            self.assertIn("Version:Loose 1.1x", (destination / "easy 1.1x.osu").read_text(encoding="utf-8"))
            with zipfile.ZipFile(destination / "pack 1.1x.osz") as handle:
                self.assertIn("Version:Packed 1.1x", handle.read("easy 1.1x.osu").decode("utf-8"))


    def test_audio_failure_preserves_original_music_and_reports_incomplete_outputs(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            (root / "hard.osu").write_text(_osu_text(), encoding="utf-8")
            (root / "song.wav").write_bytes(b"original audio")
            archive = root.with_suffix(".osz")
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("hard.osu", _osu_text())
                handle.writestr("song.wav", b"original audio")
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure",
                       side_effect=SpeedChangeError("Cannot read sample rate")):
                for operation, source in (("folder-speed-change", root), ("archive-speed-change", archive)):
                    output = io.StringIO()
                    code = main(["--context", "shell", "--operation", operation,
                                 "--target-format", "1.1", "--json", str(source)], stdout=output)
                    self.assertEqual(code, 0)
                    payload = json.loads(output.getvalue())
                    warnings = payload["warnings"]
                    self.assertTrue(any("Cannot read sample rate" in warning for warning in warnings), warnings)
                    from osu_toolbox.quick import format_shell_message
                    message, icon = format_shell_message(payload, show_notices=False)
                    self.assertIn("结果不完整", message)
                    self.assertIn("Cannot read sample rate", message)
                    self.assertEqual(icon, 0x10)
            destination = root.parent / "Song 1.1x"
            self.assertEqual((destination / "song.wav").read_bytes(), b"original audio")
            self.assertIn("AudioFilename: song.wav", (destination / "hard 1.1x.osu").read_text(encoding="utf-8"))
            with zipfile.ZipFile(root.parent / "Song 1.1x.osz") as handle:
                self.assertEqual(handle.read("song.wav"), b"original audio")
                self.assertEqual(handle.read("hard 1.1x.osu").splitlines(),
                                 (destination / "hard 1.1x.osu").read_bytes().splitlines())
            self.assertEqual((root / "song.wav").read_bytes(), b"original audio")

    def test_core_rejects_a_copied_folder_destination_inside_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Song"
            root.mkdir()
            chart = root / "hard.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            options = {"file_pattern": " {num}x", "difficulty_pattern": " {num}x",
                       "source_folder": str(root), "output_folder": str(root / "out")}
            for call in (plan_speed_change, change_speed):
                with self.subTest(call=call.__name__):
                    with self.assertRaises(SpeedChangeError):
                        call([chart], 1.1, **options)
            self.assertFalse((root / "out").exists())


    def test_archive_path_spelling_is_preserved_without_duplicate_members(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pack.osz"
            with zipfile.ZipFile(source, "w") as handle:
                handle.writestr("charts\\easy.osu", _osu_text(audio="../audio/song.wav"))
                handle.writestr("./audio/song.wav", b"audio")
                handle.writestr("other.wav", b"unused")
            with zipfile.ZipFile(source) as handle:
                names = set(handle.namelist())
            with patch("osu_toolbox.speed_change._AudioJobRegistry.ensure", self._fake_audio):
                code = main(["--context", "shell", "--operation", "archive-speed-change",
                             "--target-format", "1.1", str(source)], stdout=io.StringIO())
            self.assertEqual(code, 0)
            with zipfile.ZipFile(source.parent / "pack 1.1x.osz") as handle:
                self.assertEqual(set(handle.namelist()), {
                    "charts/easy 1.1x.osu", "./audio/song 1.1x.wav", "other.wav",
                })
                self.assertEqual(len(handle.namelist()), len(names))
                self.assertEqual(handle.read("./audio/song 1.1x.wav"), b"converted:audio")
                text = handle.read("charts/easy 1.1x.osu").decode("utf-8")
                self.assertIn("Version:Hard 1.1x", text)
                self.assertIn("AudioFilename: ../audio/song 1.1x.wav", text)


class SpeedCommandLineTests(unittest.TestCase):
    """The registered menu commands must be runnable exactly as written."""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG

    def _chart_with_audio(self, root, audio_bytes=b"not really audio"):
        chart = root / "song.osu"
        chart.write_text(_osu_text(), encoding="utf-8")
        (root / "song.wav").write_bytes(audio_bytes)
        return chart

    def test_dry_run_plans_the_marked_outputs(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = self._chart_with_audio(root)
            out = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "speed-change",
                "--target-format", "1.5", "--dry-run", "--json", str(chart),
            ], stdout=out)
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["speed"], 1.5)
            self.assertEqual(payload["outputs"], [str(root / "song 1.5x.osu")])

    def test_folder_input_is_rejected(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            error = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "speed-change",
                "--target-format", "1.5", str(directory),
            ], stderr=error)
            self.assertEqual(code, 2)
            self.assertIn("文件夹", error.getvalue())

    def test_folder_menu_plans_one_sibling_folder_for_nested_charts_and_archives(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "Song" / "0"
            nested.mkdir(parents=True)
            chart = nested / "hard.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            archive = root / "Song" / "pack.osz"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("easy.osu", _osu_text())
            out, error = io.StringIO(), io.StringIO()
            code = main(["--context", "shell", "--operation", "folder-speed-change",
                         "--target-format", "1.1", "--dry-run", "--json", str(root)],
                        stdout=out, stderr=error)
            self.assertEqual(code, 0, error.getvalue())
            destination = root.parent / f"{root.name} 1.1x"
            self.assertEqual(json.loads(out.getvalue())["outputs"], [str(destination)])
            self.assertFalse(destination.exists())
            self.assertFalse((nested / "hard 1.1x.osu").exists())

    def test_folder_menu_respects_an_explicit_output_directory(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._chart_with_audio(source)
            output = root / "out"
            out = io.StringIO()
            code = main(["--context", "shell", "--operation", "folder-speed-change",
                         "--target-format", "1.1", "--output-directory", str(output),
                         "--dry-run", "--json", str(source)], stdout=out)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["outputs"], [str(output / "source 1.1x")])

    def test_folder_menu_requires_a_folder_and_rejects_empty_inputs(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in (root, self._chart_with_audio(root)):
                error = io.StringIO()
                # The populated folder is not empty any more; use a fresh empty one.
                if source == root:
                    source = root / "empty"
                    source.mkdir()
                code = main(["--context", "shell", "--operation", "folder-speed-change",
                             "--target-format", "1.1", str(source)], stderr=error)
                self.assertEqual(code, 2)
                self.assertIn("文件夹", error.getvalue())

    def test_folder_menu_custom_speed_can_be_cancelled_without_writing(self):
        from osu_toolbox.quick import main, QuickCommandError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._chart_with_audio(root)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with patch("osu_toolbox.quick._prompt_speed", side_effect=QuickCommandError("已取消变速", "cancelled")):
                code = main(["--context", "shell", "--operation", "folder-speed-change",
                             "--prompt-speed", str(root)], stderr=io.StringIO())
            self.assertEqual(code, 2)
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_folder_menu_changes_music_charts_and_nested_archives_without_touching_sources(self):
        from osu_toolbox.quick import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "集合 %TEMP% 空格"
            song = root / "Song"
            nested = song / "Nested"
            nested.mkdir(parents=True)
            audio = song / "song.wav"
            generated = subprocess.run([self.ffmpeg, "-hide_banner", "-nostdin", "-y",
                                       "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                                       "-ar", "44100", "-ac", "1", str(audio)], capture_output=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            chart = song / "hard.osu"
            chart.write_text(_osu_text(audio="song.wav", title="Song", version="Hard"), encoding="utf-8")
            archive = nested / "pack.osz"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("easy.osu", _osu_text(audio="song.wav", title="Song", version="Easy"))
                handle.write(audio, "song.wav")
            (song / "cover.png").write_bytes(b"cover")
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            out, error = io.StringIO(), io.StringIO()
            code = main(["--context", "shell", "--operation", "folder-speed-change",
                         "--target-format", "1.5", "--json", str(root)], stdout=out, stderr=error)
            self.assertEqual(code, 0, error.getvalue())
            destination = root.parent / f"{root.name} 1.5x"
            output_song = destination / "Song"
            marked = output_song / "hard 1.5x.osu"
            self.assertEqual(json.loads(out.getvalue())["outputs"], [str(destination)])
            self.assertTrue(marked.is_file())
            self.assertIn("Title:Song 1.5x", marked.read_text(encoding="utf-8"))
            self.assertIn("Version:Hard 1.5x", marked.read_text(encoding="utf-8"))
            self.assertIn("AudioFilename: song 1.5x.wav", marked.read_text(encoding="utf-8"))
            self.assertIn("64,192,667,", marked.read_text(encoding="utf-8"))
            self.assertEqual((output_song / "cover.png").read_bytes(), b"cover")
            def decoded_samples(path):
                decoded = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(path),
                                          "-f", "s16le", "-ac", "1", "-ar", "44100", "-"],
                                         capture_output=True)
                self.assertEqual(decoded.returncode, 0, decoded.stderr)
                return len(decoded.stdout) // 2
            self.assertLess(decoded_samples(output_song / "song 1.5x.wav"), decoded_samples(audio) * .8)
            with zipfile.ZipFile(output_song / "Nested" / "pack 1.5x.osz") as handle:
                self.assertEqual(set(handle.namelist()), {"easy 1.5x.osu", "song 1.5x.wav"})
                text = handle.read("easy 1.5x.osu").decode("utf-8")
                self.assertIn("Version:Easy 1.5x", text)
                self.assertIn("Title:Song 1.5x", text)
                self.assertIn("AudioFilename: song 1.5x.wav", text)
            self.assertEqual({p: p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
            renamed = {
                Path("Song/hard.osu"): Path("Song/hard 1.5x.osu"),
                Path("Song/song.wav"): Path("Song/song 1.5x.wav"),
                Path("Song/Nested/pack.osz"): Path("Song/Nested/pack 1.5x.osz"),
            }
            self.assertEqual({p.relative_to(destination) for p in destination.rglob("*") if p.is_file()},
                             {renamed.get(p.relative_to(root), p.relative_to(root)) for p in before})
            outputs = {p: p.read_bytes() for p in destination.rglob("*") if p.is_file()}
            again = io.StringIO()
            code = main(["--context", "shell", "--operation", "folder-speed-change",
                         "--target-format", "1.5", "--json", str(root)], stdout=again, stderr=error)
            self.assertEqual(code, 0, error.getvalue())
            second = root.parent / f"{root.name} 2 1.5x"
            self.assertEqual(json.loads(again.getvalue())["outputs"], [str(second)])
            self.assertTrue((second / "Song" / "hard 1.5x.osu").is_file())
            for path, content in outputs.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertEqual({p: p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)

    def test_archive_input_is_rejected_by_the_plain_operation(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "pack.osz"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("song.osu", _osu_text())
            error = io.StringIO()
            code = main([
                "--context", "shell", "--operation", "speed-change",
                "--target-format", "1.5", str(archive),
            ], stderr=error)
            self.assertEqual(code, 2)
            self.assertIn("归档", error.getvalue())

    def test_invalid_speed_is_reported(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = self._chart_with_audio(root)
            for value in ("1", "0", "abc"):
                with self.subTest(value=value):
                    error = io.StringIO()
                    code = main([
                        "--context", "shell", "--operation", "speed-change",
                        "--target-format", value, str(chart),
                    ], stderr=error)
                    self.assertEqual(code, 2)
                    self.assertTrue(error.getvalue().strip())

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_menu_command_runs_end_to_end_on_a_real_chart(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            subprocess.run(
                [
                    self.ffmpeg, "-hide_banner", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-ac", "1", str(root / "song.wav"),
                ],
                capture_output=True,
            )
            # Exactly what the 1.05x file menu entry registers.
            code = main([
                "--context", "shell", "--operation", "speed-change",
                "--target-format", "1.05", "--difficulty-marker=", str(chart),
            ], stdout=io.StringIO())
            self.assertEqual(code, 0)
            marked = root / "song 1.05x.osu"
            self.assertTrue(marked.is_file())
            self.assertTrue((root / "song 1.05x.wav").is_file())
            text = marked.read_text(encoding="utf-8")
            self.assertIn("AudioFilename: song 1.05x.wav", text)
            # The file menu passes an empty marker, so the difficulty name is
            # left as authored.
            self.assertIn("Version:Hard\n", text)

    @unittest.skipUnless(FFMPEG, "需要 FFmpeg")
    def test_archive_menu_command_runs_end_to_end(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pack.osz"
            audio = root / "song.wav"
            subprocess.run(
                [
                    self.ffmpeg, "-hide_banner", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-ac", "1", str(audio),
                ],
                capture_output=True,
            )
            self.assertTrue(audio.is_file(), "FFmpeg 未能生成测试音频")
            with zipfile.ZipFile(source, "w") as handle:
                handle.writestr("easy.osu", _osu_text(version="Easy", audio="song.wav"))
                handle.writestr("hard.osu", _osu_text(version="Hard", audio="song.wav"))
                handle.write(audio, "song.wav")
            before = source.read_bytes()
            # Exactly what the archive 0.9x menu entry registers.
            code = main([
                "--context", "shell", "--operation", "archive-speed-change",
                "--target-format", "0.9", "--difficulty-marker=", str(source),
            ], stdout=io.StringIO())
            self.assertEqual(code, 0)
            marked = root / "pack 0.9x.osz"
            self.assertTrue(marked.is_file())
            with zipfile.ZipFile(marked) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["easy 0.9x.osu", "hard 0.9x.osu", "song 0.9x.wav"],
                )
                chart = archive.read("easy 0.9x.osu").decode("utf-8")
            self.assertIn("AudioFilename: song 0.9x.wav", chart)
            self.assertIn("Version:Easy 0.9x", chart)
            self.assertIn("Title:T 0.9x", chart)
            # The source archive keeps its original chart and audio names.
            self.assertEqual(source.read_bytes(), before)

    def test_difficulty_marker_can_be_overridden_from_the_command_line(self):
        from osu_toolbox.quick import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "song.osu"
            chart.write_text(_osu_text(), encoding="utf-8")
            code = main([
                "--context", "shell", "--operation", "speed-change",
                "--target-format", "1.5", "--speed-suffix", "[{num}]",
                "--difficulty-marker", "(变速{num}x)", str(chart),
            ], stdout=io.StringIO())
            self.assertEqual(code, 0)
            marked = root / "song[1.5].osu"
            self.assertTrue(marked.is_file())
            self.assertIn(
                "Version:Hard(变速1.5x)", marked.read_text(encoding="utf-8")
            )


if __name__ == "__main__":
    unittest.main()
