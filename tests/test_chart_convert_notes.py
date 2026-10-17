"""Regression cover for chart conversion playability, keysounds and special notes.

The three areas this module locks down:

* FIX-05 -- only key play is convertible.  An osu! mode other than mania, a
  Malody mode that cannot be a keyboard width and a StepMania file whose
  ``#NOTES`` carry no supported steps type are rejected instead of being
  silently reinterpreted as lanes, while extra object flag bits and a sparse
  column set must not distort a chart that *is* key play.
* M4 -- keysound migration across all three formats: ``.osu`` hitSample,
  ``.mc`` note ``sound`` and StepMania ``#KEYSOUNDS`` + ``[i]`` references,
  including archive packing, missing files, BGM versus keysound, stacked or
  out-of-range references and automatic ``K`` triggers.
* M5 -- StepMania special note types (hold/roll/mine/lift/fake), cross-measure
  head/tail pairing, the counted losses toward osu!/Malody, same-cell grid
  conflicts, unknown markers and multiplayer rows.
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from osu_toolbox.chart_convert import (
    ChartConversionError,
    convert_paths,
    parse_chart,
    plan_conversion,
)


def _osu_chart(objects, *, key_count=4, mode=3, audio="song.mp3"):
    """Return a minimal osu!mania chart body.

    The ``[General]`` section always declares the gameplay, because the
    extension alone never makes a file a key chart.
    """
    return (
        "osu file format v14\n"
        "[General]\n"
        f"AudioFilename: {audio}\n"
        f"Mode: {mode}\n"
        "[Metadata]\n"
        "Title: T\n"
        "Artist: A\n"
        "Version: Hard\n"
        "[Difficulty]\n"
        f"CircleSize: {key_count}\n"
        "[TimingPoints]\n"
        "0,500,4,1,0,100,1,0\n"
        "[HitObjects]\n"
        f"{objects}\n"
    )


def _sm_chart(body, *, chart_type="dance-single", music="song.ogg", keysounds=None,
              difficulty="Hard", meter="5"):
    """Return a one-chart StepMania file around ``body`` (measure rows)."""
    keysound_tag = f"#KEYSOUNDS:{':'.join(keysounds)};" if keysounds else ""
    return (
        f"#TITLE:T;#MUSIC:{music};{keysound_tag}#BPMS:0=120;"
        "#NOTES:\n"
        f" {chart_type}:\n :\n {difficulty}:\n {meter}:\n 0.000:\n"
        f"{body}\n;"
    )


def _mc_chart(notes, *, columns=4, mode=0, audio=None):
    """Return a minimal Malody key chart as JSON text.

    ``columns=None`` omits the native ``mode_ext.column`` declaration, which
    is how a file with no keyboard width at all looks.
    """
    song = {"title": "T"}
    if audio:
        song["file"] = audio
    meta = {"mode": mode, "song": song, "version": "Hard"}
    if columns is not None:
        meta["mode_ext"] = {"column": columns}
    return json.dumps({
        "meta": meta,
        "time": [{"beat": [0, 0, 1], "bpm": 120}],
        "note": list(notes),
    })


def _collect():
    """Return ``(events, emit_event)`` recording every emitted (kind, payload)."""
    events = []
    return events, (lambda kind, payload: events.append((kind, payload)))


def _warnings(events):
    return [payload for kind, payload in events if kind == "warning"]


def _notes(document):
    return [
        (note.time, note.column, note.note_type, note.end_time, note.sound, note.sound_volume)
        for note in document.notes
    ]


def _sound_events(document):
    return [
        (event.time, event.column, event.sound, event.volume, event.kind)
        for event in document.sound_events
    ]


def _diagnostic(document, code):
    for diagnostic in document.diagnostics:
        if diagnostic.code == code:
            return diagnostic
    raise AssertionError(f"missing diagnostic {code!r}: {document.diagnostics}")


def _sm_measure_rows(text):
    """Return the written note rows grouped by measure.

    Only valid for a file written by this converter, which puts the chart
    header and the note data on one line and escapes ``:`` inside the rows.
    """
    body = text.split("#NOTES:", 1)[1].rsplit(":", 1)[1].rsplit(";", 1)[0]
    return [
        [line.strip() for line in measure.splitlines() if line.strip()]
        for measure in body.split(",")
    ]


class GameplayValidationTests(unittest.TestCase):
    """FIX-05: only key play converts, and valid key play stays intact."""

    def test_osu_modes_other_than_mania_are_rejected(self):
        """A standard/taiko/catch beatmap must never be read as lanes."""
        for mode, name in ((0, "osu!"), (1, "osu!taiko"), (2, "osu!catch")):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / f"mode{mode}.osu"
                    source.write_text(
                        _osu_chart(
                            "256,192,1000,2,0,B|300:200|350:250,1,140\n"
                            "64,192,1500,8,0,2000",
                            mode=mode,
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ChartConversionError) as raised:
                        parse_chart(source)
                    message = str(raised.exception)
                    self.assertIn("不是 osu!mania 谱面", message)
                    self.assertIn(f"Mode: {mode}", message)
                    self.assertIn(name, message)
                    # Reading is already fatal, so a batch conversion must not
                    # write any output for it either.
                    with self.assertRaises(ChartConversionError):
                        convert_paths([source], ".mc", root)
                    self.assertEqual(list(root.glob("*.mc")), [])

    def test_mc_mode_outside_the_key_range_is_rejected(self):
        """``meta.mode`` above the keyboard range is not a key chart."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.mc"
            source.write_text(
                _mc_chart([{"beat": [0, 0, 1], "column": 0}], columns=None, mode=19),
                encoding="utf-8",
            )
            with self.assertRaises(ChartConversionError) as raised:
                parse_chart(source)
            message = str(raised.exception)
            self.assertIn("不是 Key 玩法", message)
            self.assertIn("19", message)
            with self.assertRaises(ChartConversionError):
                convert_paths([source], ".osu", root)

    def test_mc_key_width_comes_from_mode_ext_column(self):
        """``mode=0`` plus ``mode_ext.column`` is the native key declaration."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "key.mc"
            source.write_text(
                _mc_chart([
                    {"beat": [0, 0, 1], "column": 0},
                    {"beat": [0, 2, 1], "column": 6},
                ], columns=7),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.key_count, 7)
            self.assertEqual([(note.time, note.column) for note in document.notes],
                             [(0, 0), (1000, 6)])
            target = Path(convert_paths([source], ".mc", root)[0])
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["meta"]["mode"], 0)
            self.assertEqual(payload["meta"]["mode_ext"]["column"], 7)
            self.assertEqual(parse_chart(target)[0].key_count, 7)

    def test_mc_ambiguous_legacy_mode_is_rejected(self):
        """A width-like mode alone does not identify a Key chart."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.mc"
            payload = json.loads(_mc_chart([{"beat": [0, 0, 1], "column": 3}]))
            del payload["meta"]["mode_ext"]
            payload["meta"]["mode"] = 7
            source.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ChartConversionError, "存在歧义"):
                parse_chart(source)

    def test_sm_without_a_supported_steps_type_is_rejected(self):
        """``guitar-single`` names no keyboard family and cannot be read."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "guitar.sm"
            source.write_text(
                _sm_chart("1000", chart_type="guitar-single"),
                encoding="utf-8",
            )
            with self.assertRaises(ChartConversionError) as raised:
                parse_chart(source)
            message = str(raised.exception)
            self.assertIn("不含支持的按键舞型", message)
            self.assertIn("guitar-single", message)
            with self.assertRaises(ChartConversionError):
                convert_paths([source], ".osu", root)

    def test_sm_skips_unsupported_steps_types_beside_a_playable_one(self):
        """A readable difficulty converts; the unsupported one is reported."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mixed.sm"
            source.write_text(
                "#TITLE:T;#MUSIC:song.ogg;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 5:\n 0.000:\n1000\n;"
                "#NOTES:\n guitar-single:\n :\n Expert:\n 9:\n 0.000:\n1000\n;",
                encoding="utf-8",
            )
            documents = parse_chart(source)
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0].chart_type, "dance-single")
            self.assertEqual([(note.time, note.column) for note in documents[0].notes],
                             [(0, 0)])
            diagnostic = _diagnostic(documents[0], "sm-unsupported-steps-type")
            self.assertEqual(diagnostic.count, 1)
            self.assertIn("未转换", diagnostic.message)
            self.assertIn("guitar-single", diagnostic.message)

            events, emit_event = _collect()
            outputs = convert_paths([source], ".osu", root, emit_event=emit_event)
            self.assertEqual(len(outputs), 1)
            self.assertEqual(parse_chart(outputs[0])[0].chart_type, "dance-single")
            self.assertTrue(
                any("未转换" in warning and "guitar-single" in warning
                    for warning in _warnings(events)),
                events,
            )

    def test_osu_extra_object_flag_bits_keep_hold_and_tap(self):
        """Flag bits beside tap/hold (129, 5) must not drop the object."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "flags.osu"
            source.write_text(
                _osu_chart(
                    "64,192,1000,129,0,2200:0:0:0:0:snare.wav\n"
                    "192,192,1500,5,0,0:0:0:0:kick.wav",
                    key_count=7,
                ),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(_notes(document), [
                (1000, 0, "hold", 2200, "snare.wav", None),
                (1500, 2, "tap", None, "kick.wav", None),
            ])
            self.assertEqual([note.source_id for note in document.notes], [0, 1])

            target = Path(convert_paths([source], ".osu", root)[0])
            converted = parse_chart(target)[0]
            self.assertEqual(_notes(converted), _notes(document))

    def test_declared_key_count_survives_a_sparse_column_set(self):
        """CircleSize is the lane count even when only three lanes are used."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sparse.osu"
            source.write_text(
                _osu_chart(
                    "64,192,1000,1,0\n"
                    "256,192,1500,1,0\n"
                    "448,192,2000,1,0\n",
                    key_count=7,
                ),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.key_count, 7)
            self.assertEqual([note.column for note in document.notes], [0, 3, 6])

            target = Path(convert_paths([source], ".mc", root)[0])
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["meta"]["mode_ext"]["column"], 7)
            self.assertEqual(parse_chart(target)[0].key_count, 7)

            rewritten = Path(convert_paths([source], ".sm", root)[0])
            rows = _sm_measure_rows(rewritten.read_text(encoding="utf-8"))
            self.assertTrue(all(len(row) == 7 for measure in rows for row in measure), rows)
            self.assertEqual(parse_chart(rewritten)[0].key_count, 7)

    def test_osu_unreadable_objects_are_skipped_and_reported(self):
        """A lone spinner bit, a malformed or a truncated row never becomes a note."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "junk.osu"
            source.write_text(
                _osu_chart(
                    "64,192,1000,1,0\n"
                    "256,192,1200,8,0,2000\n"
                    "320,192,notanumber,1,0\n"
                    "1,2\n",
                ),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual([(note.time, note.column) for note in document.notes],
                             [(1000, 0)])
            diagnostic = _diagnostic(document, "osu-unreadable-object")
            # Spinner bit, non-numeric time and the truncated ``1,2`` row.
            self.assertEqual(diagnostic.count, 3)
            self.assertEqual(diagnostic.severity, "warning")
            self.assertIn("无法识别的对象", diagnostic.message)

            events, emit_event = _collect()
            convert_paths([source], ".mc", root, emit_event=emit_event)
            self.assertTrue(
                any("无法识别的对象" in warning for warning in _warnings(events)),
                events,
            )

    def test_mc_events_without_a_column_are_skipped_and_reported(self):
        """An unpositioned, non-audio event has no lane to live in."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "junk.mc"
            source.write_text(
                _mc_chart([
                    {"beat": [0, 0, 1]},
                    "garbage",
                    {"beat": [0, 1, 1], "column": 3},
                ]),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.key_count, 4)
            self.assertEqual([(note.time, note.column) for note in document.notes],
                             [(500, 3)])
            diagnostic = _diagnostic(document, "mc-unreadable-event")
            self.assertEqual(diagnostic.count, 2)
            self.assertEqual(diagnostic.severity, "warning")
            self.assertIn("无法识别的 .mc 事件", diagnostic.message)

            events, emit_event = _collect()
            convert_paths([source], ".osu", root, emit_event=emit_event)
            self.assertTrue(
                any("无法识别的 .mc 事件" in warning for warning in _warnings(events)),
                events,
            )


class KeysoundMigrationTests(unittest.TestCase):
    """M4: per-note keysounds and timeline sounds survive the conversion."""

    def test_osu_hit_sample_keysounds_reach_mc_and_sm(self):
        """Every target has its own way to name a note's sound."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ks.osu"
            source.write_text(
                _osu_chart(
                    "64,192,1000,1,0,0:0:0:0:kick.wav\n"
                    "192,192,1500,1,0,0:0:0:0:snare.wav\n"
                ),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual([(note.column, note.sound) for note in document.notes],
                             [(0, "kick.wav"), (1, "snare.wav")])

            target = Path(convert_paths([source], ".mc", root)[0])
            payload = json.loads(target.read_text(encoding="utf-8"))
            positioned = [item for item in payload["note"] if "column" in item]
            self.assertEqual([(item["column"], item["sound"]) for item in positioned],
                             [(0, "kick.wav"), (1, "snare.wav")])
            self.assertEqual([(note.column, note.sound) for note in parse_chart(target)[0].notes],
                             [(0, "kick.wav"), (1, "snare.wav")])

            rewritten = Path(convert_paths([source], ".sm", root)[0])
            text = rewritten.read_text(encoding="utf-8")
            self.assertIn("#KEYSOUNDS:kick.wav,snare.wav;", text)
            # The [i] reference has to sit in the same lane as the note it
            # sounds for, so the index and the column cannot drift apart.
            self.assertIn("1[0]000", text)
            self.assertIn("01[1]00", text)
            self.assertEqual(
                [(note.time, note.column, note.sound)
                 for note in parse_chart(rewritten)[0].notes],
                [(1000, 0, "kick.wav"), (1500, 1, "snare.wav")],
            )

    def test_mc_note_keysound_reaches_the_osu_hit_sample(self):
        """A positioned ``sound`` that is not the BGM is a playable keysound."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ks.mc"
            source.write_text(
                _mc_chart([
                    {"beat": [0, 0, 1], "column": 2, "sound": "hit.wav", "vol": 80},
                    {"beat": [0, 0, 1], "sound": "song.mp3", "offset": 0, "type": 1, "vol": 100},
                ], audio="song.mp3"),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(_notes(document), [(0, 2, "tap", None, "hit.wav", 80)])

            target = Path(convert_paths([source], ".osu", root)[0])
            text = target.read_text(encoding="utf-8")
            self.assertIn("AudioFilename: song.mp3", text)
            self.assertIn("0:0:0:80:hit.wav", text)
            self.assertEqual(_notes(parse_chart(target)[0]),
                             [(0, 2, "tap", None, "hit.wav", 80)])

    def test_sm_keysound_list_and_index_references_round_trip(self):
        """``#KEYSOUNDS`` plus ``1[0]``/``K[1]`` keep both kinds of sound."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ks.sm"
            source.write_text(
                _sm_chart("1[0]000\n0000\nK[1]000\n0000",
                          keysounds=["kick.wav", "snare.wav"]),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual([(note.time, note.column, note.sound) for note in document.notes],
                             [(0, 0, "kick.wav")])
            self.assertEqual(_sound_events(document),
                             [(1000, 0, "snare.wav", None, "auto")])

            rewritten = Path(convert_paths([source], ".sm", root)[0])
            text = rewritten.read_text(encoding="utf-8")
            self.assertIn("#KEYSOUNDS:kick.wav,snare.wav;", text)
            self.assertIn("1[0]000", text)
            self.assertIn("K[1]000", text)
            converted = parse_chart(rewritten)[0]
            self.assertEqual([(note.time, note.column, note.sound) for note in converted.notes],
                             [(0, 0, "kick.wav")])
            self.assertEqual(_sound_events(converted), _sound_events(document))

    def test_keysounds_are_packed_into_a_mcz_with_matching_references(self):
        """.mcz carries the keysound files and the chart still finds them."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ks.osu"
            source.write_text(
                _osu_chart("64,192,1000,1,0,0:0:0:0:kick.wav\n"),
                encoding="utf-8",
            )
            (root / "song.mp3").write_bytes(b"audio")
            (root / "kick.wav").write_bytes(b"kick")
            packed = Path(convert_paths([source], ".mcz", root)[0])
            with zipfile.ZipFile(packed) as archive:
                names = archive.namelist()
                self.assertIn("kick.wav", names)
                self.assertIn("song.mp3", names)
                chart_name = next(name for name in names if name.endswith(".mc"))
                payload = json.loads(archive.read(chart_name).decode("utf-8"))
            positioned = [item for item in payload["note"] if "column" in item]
            self.assertEqual([item["sound"] for item in positioned], ["kick.wav"])

    def test_a_missing_keysound_file_is_reported_by_name(self):
        """The reference is reported even though the conversion continues."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ks.osu"
            source.write_text(
                _osu_chart("64,192,1000,1,0,0:0:0:0:kick.wav\n"),
                encoding="utf-8",
            )
            events, emit_event = _collect()
            outputs = convert_paths([source], ".mc", root, emit_event=emit_event)
            self.assertEqual(len(outputs), 1)
            self.assertTrue(
                any("按键音" in warning and "kick.wav" in warning
                    for warning in _warnings(events)),
                events,
            )
            self.assertFalse(
                any("kick.wav" in payload for kind, payload in events if kind == "log"),
                events,
            )

    def test_a_bgm_event_stays_the_main_audio_beside_keysound_notes(self):
        """A ``type: 1`` note without a column is the music, never a keysound."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bgm.mc"
            source.write_text(
                _mc_chart([
                    {"beat": [0, 0, 1], "sound": "song.mp3", "offset": 0, "type": 1, "vol": 100},
                    {"beat": [0, 1, 1], "column": 2, "sound": "hit.wav", "vol": 80},
                ], audio="song.mp3"),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.audio_filename, "song.mp3")
            self.assertEqual(_notes(document), [(500, 2, "tap", None, "hit.wav", 80)])

            target = Path(convert_paths([source], ".osu", root)[0])
            text = target.read_text(encoding="utf-8")
            self.assertIn("AudioFilename: song.mp3", text)
            self.assertIn("hit.wav", text)
            self.assertEqual(parse_chart(target)[0].audio_filename, "song.mp3")

    def test_stacked_and_out_of_range_keysound_references_are_reported(self):
        """One note carries one sound; bad indices are dropped and counted."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "multi.sm"
            source.write_text(
                _sm_chart("1[0][1]000\n0000\n1[7]000\n0000",
                          keysounds=["kick.wav", "snare.wav"]),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual([(note.time, note.column, note.sound) for note in document.notes],
                             [(0, 0, "kick.wav"), (1000, 0, "")])
            diagnostic = _diagnostic(document, "sm-keysound-ref-dropped")
            self.assertEqual(diagnostic.count, 2)
            self.assertEqual(diagnostic.severity, "loss")
            self.assertIn("无法附加", diagnostic.message)

            events, emit_event = _collect()
            rewritten = Path(convert_paths([source], ".sm", root, emit_event=emit_event)[0])
            self.assertEqual(
                [note.sound for note in parse_chart(rewritten)[0].notes],
                ["kick.wav", ""],
            )
            self.assertTrue(
                any("无法附加" in warning for warning in _warnings(events)),
                events,
            )

    def test_auto_keysound_markers_stay_triggers_and_never_become_taps(self):
        """``K`` is a timeline sound: dropped toward osu!, kept toward .sm."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "auto.sm"
            source.write_text(
                _sm_chart("K[0]000\n0000\n1000\n0000", keysounds=["kick.wav"]),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual([(note.time, note.column) for note in document.notes],
                             [(1000, 0)])
            self.assertEqual(_sound_events(document),
                             [(0, 0, "kick.wav", None, "auto")])

            events, emit_event = _collect()
            target = Path(convert_paths([source], ".osu", root, emit_event=emit_event)[0])
            converted = parse_chart(target)[0]
            self.assertEqual([(note.time, note.column) for note in converted.notes],
                             [(1000, 0)])
            self.assertEqual(converted.sound_events, [])
            self.assertTrue(
                any("自动声音事件" in warning for warning in _warnings(events)),
                events,
            )

            rewritten = Path(convert_paths([source], ".sm", root)[0])
            text = rewritten.read_text(encoding="utf-8")
            self.assertIn("K[0]000", text)
            self.assertEqual([(event.time, event.sound, event.kind)
                              for event in parse_chart(rewritten)[0].sound_events],
                             [(0, "kick.wav", "auto")])


class StepManiaSpecialNoteTests(unittest.TestCase):
    """M5: special note types, grid conflicts and player separation."""

    SPECIAL_BODY = "24MLF00\n0000000\n3000000\n0300000"

    def test_special_note_types_survive_a_sm_rewrite(self):
        """Hold, roll, mine, lift and fake all come back unchanged."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "special.sm"
            source.write_text(
                _sm_chart(self.SPECIAL_BODY, chart_type="kb7-single"),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            expected = [
                (0, 0, "hold", 1000, "", None),
                (0, 1, "roll", 1500, "", None),
                (0, 2, "mine", None, "", None),
                (0, 3, "lift", None, "", None),
                (0, 4, "fake", None, "", None),
            ]
            self.assertEqual(_notes(document), expected)
            plan = plan_conversion(document, ".sm")
            for note in document.notes:
                self.assertEqual(plan.note_action(note), ("keep", "keep"), note.note_type)

            rewritten = Path(convert_paths([source], ".sm", root)[0])
            self.assertEqual(parse_chart(rewritten)[0].key_count, 7)
            self.assertEqual(_notes(parse_chart(rewritten)[0]), expected)

    def test_a_roll_spanning_measures_pairs_across_the_rewrite(self):
        """A head in one measure and a tail in a later measure stay paired."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cross.sm"
            source.write_text(
                _sm_chart(
                    "4000\n0000\n0000\n0000\n,\n"
                    "0000\n0000\n0000\n0000\n,\n"
                    "3000\n0000\n0000\n0000"
                ),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(_notes(document), [(0, 0, "roll", 4000, "", None)])

            rewritten = Path(convert_paths([source], ".sm", root)[0])
            converted = parse_chart(rewritten)[0]
            self.assertEqual(_notes(converted), [(0, 0, "roll", 4000, "", None)])
            # The head stays on the first measure's first row and the tail on
            # the third measure's first row: the pairing is not shifted.
            measures = _sm_measure_rows(rewritten.read_text(encoding="utf-8"))
            self.assertEqual(measures[0][0], "4000")
            self.assertEqual(measures[2][0], "3000")

    def test_special_notes_convert_to_osu_with_counted_losses(self):
        """osu! keeps the roll as a plain hold and drops mine/lift/fake."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "special.sm"
            source.write_text(
                _sm_chart(self.SPECIAL_BODY, chart_type="kb7-single"),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            plan = plan_conversion(document, ".osu")
            self.assertEqual(plan.strategy_for("roll"), ("downgrade", "roll-as-hold"))
            actions = {note.column: plan.note_action(note) for note in document.notes}
            self.assertEqual(actions[0], ("keep", "keep"))
            self.assertEqual(actions[1], ("hold", "roll-as-hold"))
            for column in (2, 3, 4):
                self.assertEqual(actions[column], ("drop", "drop-special"))

            events, emit_event = _collect()
            target = Path(convert_paths([source], ".osu", root, emit_event=emit_event)[0])
            converted = parse_chart(target)[0]
            self.assertEqual(_notes(converted), [
                (0, 0, "hold", 1000, "", None),
                (0, 1, "hold", 1500, "", None),
            ])
            warnings = _warnings(events)
            self.assertTrue(any("roll" in warning and "长条" in warning for warning in warnings),
                            warnings)
            self.assertTrue(any("地雷" in warning for warning in warnings), warnings)
            self.assertTrue(any("抬手" in warning for warning in warnings), warnings)
            self.assertTrue(any("fake" in warning for warning in warnings), warnings)
            # No dropped special note is turned into something playable.
            self.assertEqual(
                sorted((note.time, note.column) for note in converted.notes),
                [(0, 0), (0, 1)],
            )

    def test_a_same_cell_conflict_keeps_one_marker_and_reports_it(self):
        """Two markers on one grid cell keep the higher priority one."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "conflict.sm"
            # 384 rows in one measure put the tap and the mine half a row
            # apart, which snaps them onto the same output cell.
            source.write_text(
                _sm_chart("\n".join(["1000", "M000"] + ["0000"] * 382)),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(
                [(note.time, note.column, note.note_type) for note in document.notes],
                [(0, 0, "tap"), (5, 0, "mine")],
            )

            events, emit_event = _collect()
            rewritten = Path(convert_paths([source], ".sm", root, emit_event=emit_event)[0])
            converted = parse_chart(rewritten)[0]
            self.assertEqual(
                [(note.time, note.column, note.note_type) for note in converted.notes],
                [(0, 0, "tap")],
            )
            self.assertTrue(
                any("同格冲突" in warning for warning in _warnings(events)),
                events,
            )

    def test_unknown_note_markers_are_counted(self):
        """An unknown lane symbol is skipped, never guessed at."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "unknown.sm"
            source.write_text(
                _sm_chart("x000\n0000\nx000\n0000"),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.notes, [])
            diagnostic = _diagnostic(document, "sm-unknown-marker")
            self.assertEqual(diagnostic.count, 2)
            self.assertEqual(diagnostic.severity, "loss")
            self.assertIn("无法识别的音符标记", diagnostic.message)

            events, emit_event = _collect()
            convert_paths([source], ".sm", root, emit_event=emit_event)
            self.assertTrue(
                any("无法识别的音符标记" in warning for warning in _warnings(events)),
                events,
            )

    def test_multiplayer_rows_keep_the_first_player_only(self):
        """``&`` separates players; only the first player's lanes survive."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "multi.sm"
            source.write_text(
                _sm_chart(
                    "1000&0001\n0100&0000\n0000&0000\n0000&0000"
                ),
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertTrue(document.extra.get("sm_multiplayer"))
            self.assertEqual(document.key_count, 4)
            self.assertEqual([(note.time, note.column) for note in document.notes],
                             [(0, 0), (500, 1)])

            events, emit_event = _collect()
            rewritten = Path(convert_paths([source], ".sm", root, emit_event=emit_event)[0])
            self.assertEqual(
                [(note.time, note.column) for note in parse_chart(rewritten)[0].notes],
                [(0, 0), (500, 1)],
            )
            self.assertTrue(
                any("多人" in warning for warning in _warnings(events)),
                events,
            )


if __name__ == "__main__":
    unittest.main()
