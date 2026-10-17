import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from osu_toolbox.chart_convert import (
    ChartConversionError,
    archive_chart_extension,
    convert_paths,
    parse_chart,
)
from osu_toolbox.chart_convert import (
    _msd_unescape,
    _msd_values as _project_msd_values,
    _normalise_sm_field,
    _normalise_sm_meter,
    _normalise_sm_resource,
    SM_MEASURE_SUBDIVISIONS,
    SM_ROWS_PER_MEASURE,
)
from osu_toolbox.tools.chart_convert import ChartConvertController
from osu_toolbox.tools.base import ToolContext


def _reference_msd_values(text):
    """Reference implementation of StepMania MSD parsing with unescaping.

    Deliberately not shared with the production code: the tests must be able
    to disagree with it.  The subtle part is that the write cursor is only reset
    by ``#`` or ``:`` -- never by ``;`` -- and the missing-';' recovery rule
    reads back the raw buffer, which still holds characters written for earlier
    fields.  That is why a ``//`` comment inside a ``#NOTES`` difficulty field
    can merge two values and shift the meter onto the radar row.
    """
    values = []
    fields = None
    processed = []
    # -1 indicates state before entering the first value.
    length_written = -1

    def add_param():
        fields.append("".join(processed[:length_written]))

    index = 0
    total = len(text)
    reading_value = False
    while index < total:
        if index + 1 < total and text[index] == "/" and text[index + 1] == "/":
            while index < total and text[index] != "\n":
                index += 1
            continue
        if reading_value and text[index] == "#":
            first_char = True
            cursor = length_written
            while cursor > 0 and processed[cursor - 1] not in "\r\n":
                if processed[cursor - 1] in " \t":
                    cursor -= 1
                    continue
                first_char = False
                break
            if not first_char:
                processed[length_written:length_written + 1] = [text[index]]
                length_written += 1
                index += 1
                continue
            length_written = cursor
            while length_written > 0 and processed[length_written - 1] in "\r\n \t":
                length_written -= 1
            add_param()
            length_written = 0
            reading_value = False
        if not reading_value and text[index] == "#":
            values.append([])
            fields = values[-1]
            reading_value = True
        if not reading_value:
            index += 2 if text[index] == "\\" else 1
            continue
        if length_written != -1 and text[index] in ":;":
            add_param()
        if text[index] == "#":
            index += 1
            length_written = 0
            continue
        if text[index] == ":":
            index += 1
            length_written = 0
            continue
        if text[index] == ";":
            reading_value = False
            index += 1
            continue
        if text[index] == "\\":
            index += 1
        if index < total:
            processed[length_written:length_written + 1] = [text[index]]
            length_written += 1
            index += 1
    if reading_value:
        add_param()
    return values


def _notes_fields(text):
    """Return the six chart fields of the single ``#NOTES`` value.

    The ``#NOTES`` tag name is dropped and raw fields are stripped, matching
    the behavior of SM chart editors before splitting fields.
    """
    for fields in _reference_msd_values(text):
        if fields[:1] == ["NOTES"]:
            chart_fields = fields[1:]
            if len(chart_fields) < 6:
                raise AssertionError(
                    f"#NOTES produced only {len(fields)} fields; both "
                    "StepMania and ArrowVortex need at least 7"
                )
            return [item.strip() for item in chart_fields]
    raise AssertionError("no #NOTES value found")


class ChartConvertTests(unittest.TestCase):
    def test_osu_metadata_holds_and_mc_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: Title\nSubtitle: Subtitle\nArtist: Artist\nCreator: Mapper\nVersion: Hard\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,1000,1,0\n192,192,1500,128,0,2200:0:0:0:0:\n",
                encoding="utf-8",
            )
            target = convert_paths([source], ".mc", root)[0]
            document = parse_chart(target)[0]
            self.assertEqual((document.title, document.subtitle, document.artist, document.creator), ("Title", "Subtitle", "Artist", "Mapper"))
            self.assertEqual([(n.time, n.column, n.end_time) for n in document.notes], [(1000, 0, None), (1500, 1, 2200)])

    def test_sm_multiple_difficulties_split_to_osu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.sm"
            source.write_text(
                "#TITLE:Song;#ARTIST:Artist;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Easy:\n 1:\n 0.000:\n1000\n;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 5:\n 0.000:\n0100\n;",
                encoding="utf-8",
            )
            outputs = convert_paths([source], ".osu", root)
            self.assertEqual(len(outputs), 2)
            self.assertTrue(any("Easy" in Path(item).stem for item in outputs))
            self.assertTrue(any("Hard" in Path(item).stem for item in outputs))

    def test_archive_to_plain_discards_resources_and_archive_round_trip_keeps_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "inside.osu"
            # The chart must actually reference the audio and background to
            # have them carried into the archive.
            chart.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode:3\n"
                "[Metadata]\nTitle:X\nVersion:Easy\n[Difficulty]\nCircleSize:4\n"
                "[Events]\n0,0,\"cover.jpg\",0,0\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            archive = root / "pack.osz"
            with zipfile.ZipFile(archive, "w") as output:
                output.write(chart, "inside.osu")
                output.writestr("song.mp3", b"audio")
                output.writestr("cover.jpg", b"image")
            plain = convert_paths([archive], ".mc", root)
            self.assertEqual(len(plain), 1)
            self.assertEqual(Path(plain[0]).suffix, ".mc")
            packed = convert_paths([archive], ".mcz", root)
            with zipfile.ZipFile(packed[0]) as output:
                names = output.namelist()
                self.assertEqual(output.read("song.mp3"), b"audio")
                self.assertEqual(output.read("cover.jpg"), b"image")
            self.assertIn("song.mp3", names)
            self.assertTrue(any(name.endswith(".mc") for name in names))

    def test_plain_chart_to_archive_copies_referenced_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}, "background": "cover.jpg"},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0, "sound": "song.mp3"}],
            }), encoding="utf-8")
            (root / "song.mp3").write_bytes(b"audio")
            (root / "cover.jpg").write_bytes(b"image")
            archive_path = Path(convert_paths([source], ".osz", root)[0])
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.read("song.mp3"), b"audio")
                self.assertEqual(archive.read("cover.jpg"), b"image")
                chart_name = next(name for name in archive.namelist() if name.endswith(".osu"))
                chart_text = archive.read(chart_name).decode("utf-8")
            self.assertNotIn("AudioFilename: song.mp3", chart_text)
            self.assertIn("0:0:0:0:song.mp3", chart_text)
            self.assertIn('0,0,"cover.jpg",0,0', chart_text)

    def test_same_format_uses_converted_suffix_without_overwriting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.osu"
            source.write_text("osu file format v14\n[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[HitObjects]\n", encoding="utf-8")
            outputs = convert_paths([source], ".osu", root)
            self.assertEqual(Path(outputs[0]).name, "map-converted.osu")
            self.assertTrue(source.is_file())

    def test_plain_chart_to_archive_packs_only_referenced_resources(self):
        """A single chart must not drag in every file next to it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}, "background": "cover.jpg"},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0, "sound": "song.mp3"}],
            }), encoding="utf-8")
            (root / "song.mp3").write_bytes(b"audio")
            (root / "cover.jpg").write_bytes(b"image")
            # Everything below belongs to other charts in the same folder and
            # must not be packed.
            (root / "other-song.mp3").write_bytes(b"other audio")
            (root / "other-cover.jpg").write_bytes(b"other image")
            (root / "banner.png").write_bytes(b"banner")
            (root / "readme.txt").write_bytes(b"readme")
            (root / "video.mp4").write_bytes(b"video")
            (root / "other-chart.osu").write_text(
                "osu file format v14\n[General]\nMode:3\n", encoding="utf-8"
            )
            nested = root / "subfolder"
            nested.mkdir()
            (nested / "nested-art.png").write_bytes(b"nested")
            (nested / "nested-audio.ogg").write_bytes(b"nested audio")

            for extension in (".osz", ".mcz"):
                with self.subTest(extension=extension):
                    target = Path(convert_paths([source], extension, root)[0])
                    with zipfile.ZipFile(target) as archive:
                        names = sorted(archive.namelist())
                        self.assertEqual(archive.read("song.mp3"), b"audio")
                        self.assertEqual(archive.read("cover.jpg"), b"image")
                    self.assertEqual(len(names), 3)
                    self.assertIn("song.mp3", names)
                    self.assertIn("cover.jpg", names)
                    self.assertTrue(
                        any(name.endswith(archive_chart_extension(extension))
                            for name in names),
                        names,
                    )
                    for unwanted in (
                        "other-song.mp3", "other-cover.jpg", "banner.png",
                        "readme.txt", "video.mp4", "other-chart.osu",
                        "subfolder/nested-art.png", "subfolder/nested-audio.ogg",
                    ):
                        self.assertNotIn(unwanted, names)

    def test_plain_chart_to_archive_survives_a_missing_resource(self):
        """A resource that is not on disk is skipped, never fatal.

        The archive must still appear and must not carry a broken reference,
        otherwise one absent image would block archiving a whole chart.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0, "sound": "missing.mp3"}],
            }), encoding="utf-8")
            target = Path(convert_paths([source], ".osz", root)[0])
            self.assertTrue(target.is_file())
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            self.assertFalse(any(name.endswith(".mp3") for name in names), names)
            self.assertTrue(any(name.endswith(".osu") for name in names), names)

    def test_resource_reference_resolution_scope(self):
        """A nested chart may name audio that lives at the package root.

        osu! folders routinely keep the audio at the top and the charts in
        subfolders, so resolution walks upwards.  An individual chart's archive
        stays scoped to the chart's own folder so it cannot pack a neighbour's
        files, while folder packing may widen the search to the package root.
        """
        from osu_toolbox.chart_convert import _referenced_resources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            (root / "song.mp3").write_bytes(b"audio")
            (nested / "hard.osu").write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: B\nVersion: Hard\n[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            documents = parse_chart(nested / "hard.osu")
            # Chart-dir scope does not claim the audio one level up.
            self.assertEqual(list(_referenced_resources(documents, nested)), [])
            # Package scope resolves the very same reference.
            widened = list(_referenced_resources(documents, nested, stop=root))
            self.assertEqual(
                [(path.name, member) for path, member in widened],
                [("song.mp3", "song.mp3")],
            )
            self.assertTrue(widened[0][0].is_file())
            # The chart-dir scoped archive must not contain it either.
            target = Path(convert_paths([nested / "hard.osu"], ".osz", root)[0])
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            self.assertFalse(any(name.endswith(".mp3") for name in names), names)

    def test_archive_to_archive_drops_unrelated_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "inside.osu"
            chart.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode:3\n"
                "[Metadata]\nTitle:X\nVersion:Easy\n[Difficulty]\nCircleSize:4\n"
                "[Events]\n0,0,\"cover.jpg\",0,0\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            archive_path = root / "pack.osz"
            with zipfile.ZipFile(archive_path, "w") as output:
                output.write(chart, "inside.osu")
                output.writestr("song.mp3", b"audio")
                output.writestr("cover.jpg", b"image")
                output.writestr("unused-art.png", b"unused")
                output.writestr("readme.txt", b"readme")
            target = Path(convert_paths([archive_path], ".mcz", root)[0])
            with zipfile.ZipFile(target) as archive:
                names = sorted(archive.namelist())
                self.assertEqual(archive.read("song.mp3"), b"audio")
                self.assertEqual(archive.read("cover.jpg"), b"image")
            self.assertNotIn("unused-art.png", names)
            self.assertNotIn("readme.txt", names)
            self.assertTrue(any(name.endswith(".mc") for name in names))

    def test_archive_to_archive_tolerates_a_missing_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "broken.osz"
            with zipfile.ZipFile(archive_path, "w") as output:
                output.writestr(
                    "chart.osu",
                    "osu file format v14\n[General]\nAudioFilename: gone.mp3\nMode:3\n"
                    "[Metadata]\nTitle:X\nVersion:Easy\n[Difficulty]\nCircleSize:4\n"
                    "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n",
                )
            target = Path(convert_paths([archive_path], ".mcz", root)[0])
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            self.assertFalse(any(name.endswith(".mp3") for name in names))
            self.assertTrue(any(name.endswith(".mc") for name in names))

    def test_archive_conversion_converts_only_chart_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = (
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode:3\n"
                "[Metadata]\nTitle:X\nVersion:{version}\n[Difficulty]\nCircleSize:4\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n"
            )
            archive_path = root / "set.osz"
            with zipfile.ZipFile(archive_path, "w") as output:
                output.writestr("easy.osu", chart.format(version="Easy"))
                output.writestr("hard.osu", chart.format(version="Hard"))
                output.writestr("song.mp3", b"audio")
                output.writestr("readme.txt", b"readme")
            # Two chart members become two difficulties; non-chart members are
            # never parsed as charts.
            outputs = convert_paths([archive_path], ".mc", root)
            self.assertEqual(len(outputs), 2)
            versions = sorted(parse_chart(path)[0].version for path in outputs)
            self.assertEqual(versions, ["Easy", "Hard"])
            packed = Path(convert_paths([archive_path], ".mcz", root)[0])
            with zipfile.ZipFile(packed) as archive:
                # Members are named after the chart's own identity inside the
                # source archive, not after the outer package plus difficulty.
                self.assertEqual(
                    sorted(name for name in archive.namelist() if name.endswith(".mc")),
                    ["easy.mc", "hard.mc"],
                )
                self.assertEqual(archive.read("song.mp3"), b"audio")
                self.assertNotIn("readme.txt", archive.namelist())

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.osz"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.osu", "osu file format v14")
            with self.assertRaises(ChartConversionError):
                convert_paths([archive], ".mc", root)

    def test_controller_uses_context_output_root_for_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.osu"
            source.write_text("osu file format v14\n[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[HitObjects]\n64,192,0,1,0\n", encoding="utf-8")
            workdir = root / "output"
            prepared = ChartConvertController().prepare({
                "input_paths": [str(source)],
                "target_format": ".mc",
                "output_location": "workdir",
            }, ToolContext(str(root), output_root=str(workdir)))
            self.assertEqual(prepared.params["output_directory"], str(workdir.resolve()))

    def test_the_chart_count_decides_the_difficulty_suffix(self):
        """Only a source that yields several charts marks its outputs with the difficulty.

        One chart keeps the source file's own name in every container
        (``BOOM.sm`` -> ``BOOM.osu``/``BOOM.mc``, member ``BOOM.mc``) -- the same
        rule archive members use -- while a multi-chart ``.sm`` needs the
        difficulty to keep its files apart.  A same-format conversion stacks
        ``-converted`` on top.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "BOOM.sm"
            source.write_text(
                "#TITLE:BOOM;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Beginner:\n 1:\n 0.000:\n1000\n;",
                encoding="utf-8")
            for target, expected in (
                (".osu", "BOOM.osu"),
                (".mc", "BOOM.mc"),
                (".sm", "BOOM-converted.sm"),
            ):
                with self.subTest(target=target):
                    out = root / f"plain{target}"
                    out.mkdir(exist_ok=True)
                    written = convert_paths([source], target, out)
                    self.assertEqual([Path(item).name for item in written], [expected])

            single = root / "single"
            single.mkdir()
            archive = Path(convert_paths([source], ".mcz", single)[0])
            self.assertEqual(archive.name, "BOOM.mcz")
            with zipfile.ZipFile(archive) as packed:
                self.assertEqual(sorted(packed.namelist()), ["BOOM.mc"])

            multi = root / "MULTI.sm"
            multi.write_text(
                "#TITLE:MULTI;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Easy:\n 1:\n 0.000:\n1000\n;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 5:\n 0.000:\n0100\n;",
                encoding="utf-8")
            multi_out = root / "multi"
            multi_out.mkdir()
            self.assertEqual(
                sorted(Path(item).name for item in convert_paths([multi], ".mc", multi_out)),
                ["MULTI-Easy.mc", "MULTI-Hard.mc"],
            )
            multi_archive = Path(convert_paths([multi], ".mcz", multi_out)[0])
            with zipfile.ZipFile(multi_archive) as packed:
                self.assertEqual(
                    sorted(packed.namelist()),
                    ["MULTI-Easy.mc", "MULTI-Hard.mc"],
                )

            # A difficulty the file name cannot express leaves no suffix at all.
            blank = root / "blank.sm"
            blank.write_text(
                "#TITLE:T;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n 新手:\n 1:\n 0.000:\n1000\n;",
                encoding="utf-8")
            blank_out = root / "blank"
            blank_out.mkdir()
            written = convert_paths([blank], ".mc", blank_out)
            self.assertEqual([Path(item).name for item in written], ["blank.mc"])

    def test_difficulty_override_is_written_to_the_converted_osu_chart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            target = Path(convert_paths(
                [source], ".osu", root,
                overall_difficulty=8, hp_drain_rate=7.5,
            )[0])
            text = target.read_text(encoding="utf-8")
            self.assertIn("OverallDifficulty: 8\n", text)
            self.assertIn("HPDrainRate: 7.5\n", text)
            # The rest of the [Difficulty] section is still written.
            self.assertIn("CircleSize: 4\n", text)

    def test_archive_members_get_the_same_difficulty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            archive_path = Path(convert_paths(
                [source], ".osz", root,
                overall_difficulty=9, hp_drain_rate=3,
            )[0])
            with zipfile.ZipFile(archive_path) as archive:
                chart_name = next(
                    name for name in archive.namelist() if name.endswith(".osu")
                )
                text = archive.read(chart_name).decode("utf-8")
            # Archive members are read as raw bytes, so compare whole lines
            # instead of assuming one newline convention.
            lines = [line.strip() for line in text.splitlines()]
            self.assertIn("OverallDifficulty: 9", lines)
            self.assertIn("HPDrainRate: 3", lines)

    def test_without_an_override_the_source_difficulty_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nHPDrainRate: 4.5\nCircleSize: 4\nOverallDifficulty: 6\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            target = Path(convert_paths([source], ".osu", root)[0])
            text = target.read_text(encoding="utf-8")
            self.assertIn("HPDrainRate: 4.5\n", text)
            self.assertIn("OverallDifficulty: 6\n", text)

    def test_non_osu_targets_ignore_the_difficulty_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "map.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            # .mc has no OD/HP field at all, so the values must be ignored
            # rather than fail the conversion.
            target = Path(convert_paths(
                [source], ".mc", root,
                overall_difficulty=8, hp_drain_rate=8,
            )[0])
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("OverallDifficulty", json.dumps(payload))



    def test_sm_offset_sign_and_empty_measures_match_stepmania(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "offset.sm"
            source.write_text(
                "#TITLE:Offset;#OFFSET:0.5;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Test:\n 1:\n 0.000:\n"
                "1000\n,\n,\n1000\n;",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.offset_ms, 500.0)
            self.assertEqual([note.time for note in document.notes], [-500, 3500])
            commented = root / "commented.sm"
            commented.write_text(
                source.read_text(encoding="utf-8")
                .replace("1000\n,", "// measure 0\n1000\n,")
                .replace(",\n,\n1000", ",\n// measure 1\n,\n// measure 2\n1000"),
                encoding="utf-8",
            )
            self.assertEqual([note.time for note in parse_chart(commented)[0].notes], [-500, 3500])
            self.assertEqual(parse_chart(commented)[0].key_count, 4)
            self.assertEqual(document.timings[0][0], -500.0)
            output = convert_paths([source], ".osu", root)[0]
            self.assertEqual([note.time for note in parse_chart(output)[0].notes], [-500, 3500])

            negative = root / "negative.sm"
            negative.write_text(source.read_text(encoding="utf-8").replace("#OFFSET:0.5", "#OFFSET:-0.5"), encoding="utf-8")
            negative_document = parse_chart(negative)[0]
            self.assertEqual([note.time for note in negative_document.notes], [500, 4500])

    def test_sm_background_metadata_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "background.sm"
            source.write_text(
                "#TITLE:Song;#MUSIC:song.mp3;#BGCHANGES:0=cover.jpg=1.000=0=0;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 4:\n 0.000:\n1000\n;",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.background_filename, "cover.jpg")
            output = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["background"], "cover.jpg")
    def test_sm_to_mc_writes_music_and_offset_to_audio_note(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "offset.sm"
            source.write_text(
                "#TITLE:Song;#MUSIC:song.mp3;#OFFSET:0.5;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 4:\n 0.000:\n1000\n;",
                encoding="utf-8",
            )
            target = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(target.read_text(encoding="utf-8"))
            sound = next(note for note in data["note"] if note.get("type") == 1)
            self.assertEqual(sound["sound"], "song.mp3")
            self.assertEqual(sound["offset"], 500.0)

    def test_mc_writes_the_audio_note_last(self):
        """Malody keeps the BGM entry as the final element of ``note``.

        A reader that scans the array (or a tool that appends its own entry)
        depends on that position, so the writer must never emit it in the middle.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle:T\nVersion:Hard\n[Difficulty]\nCircleSize:4\n"
                "[TimingPoints]\n1000,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,1000,1,0,0:0:0:0:\n"
                "128,192,2000,1,0,0:0:0:0:\n",
                encoding="utf-8",
            )
            target = Path(convert_paths([source], ".mc", root)[0])
            notes = json.loads(target.read_text(encoding="utf-8"))["note"]
            self.assertEqual(notes[-1].get("type"), 1)
            self.assertEqual(notes[-1]["sound"], "song.mp3")
            # The chart/audio alignment rides on that entry, as an integer.
            self.assertIsInstance(notes[-1]["offset"], int)
            # Only one audio entry exists, and it is the trailing one.
            self.assertEqual(sum(1 for note in notes if note.get("type") == 1), 1)

    def test_mc_round_trip_preserves_the_malody_shape(self):
        """A Malody chart converted to Malody keeps every field it had.

        Checked against real charts: an unconditional ``signature`` rewrote
        every timing entry, a synthesised ``cover`` added a metadata key, and
        both made a "conversion" differ from its input for no reason.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "native.mc"
            original = {
                "meta": {
                    "$ver": 200,
                    "id": 12345,
                    "mode": 0,
                    "mode_ext": {"column": 4},
                    "song": {"title": "T", "artist": "A", "file": "song.ogg"},
                    "version": "Hard",
                    "creator": "someone",
                    "background": "bg.png",
                    "time": 1755615615,
                },
                "time": [
                    {"beat": [0, 0, 1], "bpm": 222.0, "signature": 4},
                    {"beat": [325, 0, 1], "bpm": 222.0, "signature": 4},
                ],
                "note": [
                    {"beat": [4, 0, 1], "column": 1},
                    {"beat": [8, 0, 1], "column": 2, "endbeat": [12, 0, 1]},
                    {"beat": [0, 0, 1], "sound": "song.ogg", "offset": 47, "vol": 100, "type": 1},
                ],
            }
            source.write_text(json.dumps(original), encoding="utf-8")
            target = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(target.read_text(encoding="utf-8"))

            self.assertEqual(sorted(data.keys()), sorted(original.keys()))
            self.assertEqual(sorted(data["meta"].keys()), sorted(original["meta"].keys()))
            self.assertNotIn("cover", data["meta"])
            self.assertEqual(data["meta"]["time"], 1755615615)
            self.assertEqual(data["meta"]["id"], 12345)
            self.assertEqual(data["meta"]["$ver"], 200)
            self.assertEqual(
                [sorted(entry.keys()) for entry in data["time"]],
                [sorted(entry.keys()) for entry in original["time"]],
            )
            self.assertEqual(data["time"][0]["signature"], 4)
            # The BGM entry stays last, with the same sound and offset.
            self.assertEqual(
                {key: data["note"][-1][key] for key in ("sound", "offset", "vol", "type")},
                {"sound": "song.ogg", "offset": 47, "vol": 100, "type": 1},
            )
            self.assertEqual(len(data["note"]), len(original["note"]))

    def test_mc_signature_is_emitted_when_it_is_not_common_time(self):
        """A non-4 signature must be written even if the source omitted it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "three-four.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4},
                         "song": {"title": "T", "file": "a.ogg"}},
                "time": [{"beat": [0, 0, 1], "bpm": 120.0, "signature": 3}],
                "note": [{"beat": [4, 0, 1], "column": 0},
                         {"beat": [0, 0, 1], "sound": "a.ogg", "offset": 0,
                          "vol": 100, "type": 1}],
            }), encoding="utf-8")
            target = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["time"][0]["signature"], 3)

    def test_mc_without_signature_stays_without_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "plain.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4},
                         "song": {"title": "T", "file": "a.ogg"}},
                "time": [{"beat": [0, 0, 1], "bpm": 120.0}],
                "note": [{"beat": [4, 0, 1], "column": 0},
                         {"beat": [0, 0, 1], "sound": "a.ogg", "offset": 0,
                          "vol": 100, "type": 1}],
            }), encoding="utf-8")
            target = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("signature", data["time"][0])
            self.assertNotIn("cover", data["meta"])

    def test_sm_jacket_is_written_to_mc_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "jacket.sm"
            source.write_text(
                "#TITLE:Song;#JACKET:cover.png;#BPMS:0=120;"
                "#NOTES:\n dance-single:\n :\n Hard:\n 4:\n 0.000:\n1000\n;",
                encoding="utf-8",
            )
            target = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["cover"], "cover.png")

    def test_sm_to_mc_writes_key_mode_and_mode_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.sm"
            source.write_text(
                "#TITLE:Song;#BPMS:0=120;#NOTES:\n dance-single:\n :\n Hard:\n 7:\n 0.000:\n"
                "1000000\n;",
                encoding="utf-8",
            )
            target = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["mode"], 0)
            self.assertEqual(data["meta"]["mode_ext"]["column"], 7)
            document = parse_chart(target)[0]
            self.assertEqual(document.key_count, 7)

    def test_mc_mode_extension_preserves_native_key_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "native.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 7}, "song": {"title": "Native"}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 6}],
            }), encoding="utf-8")
            document = parse_chart(source)[0]
            self.assertEqual(document.key_count, 7)
            self.assertEqual(document.notes[0].column, 6)
    def test_mc_audio_offset_and_background_are_written_to_osu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mc"
            source.write_text(json.dumps({
                "meta": {
                    "mode": 0,
                    "mode_ext": {"column": 4},
                    "song": {"title": "Title", "artist": "Artist"},
                    "background": "cover.jpg",
                },
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [
                    {"beat": [0, 0, 1], "column": 0},
                    {"beat": [0, 0, 1], "sound": "song.mp3", "vol": 100, "offset": 250, "type": 1},
                ],
            }), encoding="utf-8")
            document = parse_chart(source)[0]
            self.assertEqual((document.audio_filename, document.background_filename, document.audio_delay_ms), ("song.mp3", "cover.jpg", 250.0))
            output = Path(convert_paths([source], ".osu", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("AudioFilename: song.mp3", text)
            self.assertIn("[Events]", text)
            self.assertIn('0,0,"cover.jpg",0,0', text)
            self.assertIn("0.000,500", text)
            self.assertIn(",192,-250,1,0", text)

    def test_mc_audio_offset_is_written_as_an_integer(self):
        """``offset`` is an integer field in Malody's .mc format.

        Every native .mc stores a whole number of milliseconds; a float such as
        ``-644.0`` makes Malody read the chart/audio alignment incorrectly.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "offset.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: T\n[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n-60,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,3227,1,0\n",
                encoding="utf-8",
            )
            text = Path(convert_paths([source], ".mc", root)[0]).read_text(encoding="utf-8")
            data = json.loads(text)
            sound = next(item for item in data["note"] if item.get("type") == 1)
            self.assertIsInstance(sound["offset"], int)
            self.assertNotIsInstance(sound["offset"], bool)
            self.assertEqual(sound["offset"], 60)
            # The serialised JSON must not carry a decimal point either.
            self.assertIn('"offset": 60', text)
            self.assertNotIn('"offset": 60.0', text)

    def test_mc_negative_and_fractional_offsets_stay_integers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # With a single osu! timing point, beat zero sits at that point, so
            # the Malody offset is its negated time rounded to nearest.
            for name, timing, expected in (
                ("negative.osu", -644, 644),
                ("fractional.osu", 250.6, -251),
            ):
                source = root / name
                source.write_text(
                    "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                    "[Metadata]\nTitle: T\n[Difficulty]\nCircleSize: 4\n"
                    f"[TimingPoints]\n{timing},500,4,1,0,100,1,0\n"
                    "[HitObjects]\n64,192,1234,1,0\n",
                    encoding="utf-8",
                )
                target = Path(convert_paths([source], ".mc", root)[0])
                data = json.loads(target.read_text(encoding="utf-8"))
                sound = next(item for item in data["note"] if item.get("type") == 1)
                self.assertIsInstance(sound["offset"], int)
                self.assertEqual(sound["offset"], expected)
                # Rounding to nearest, not truncation, keeps the alignment as
                # accurate as whole milliseconds allow.
                document = parse_chart(target)[0]
                self.assertEqual(document.audio_delay_ms, float(expected))
                self.assertEqual(document.beat_zero_ms, float(-expected))

    def test_mc_offset_and_volume_are_integers_across_sample_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "shapes.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: T\n[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n644,333.333333333333,4,2,1,5,1,0\n"
                "[HitObjects]\n64,192,644,1,0\n192,192,977,128,0,1310:0:0:0:0:\n",
                encoding="utf-8",
            )
            for extension in (".mc", ".mcz"):
                target = Path(convert_paths([source], extension, root)[0])
                if extension == ".mc":
                    data = json.loads(target.read_text(encoding="utf-8"))
                else:
                    with zipfile.ZipFile(target) as archive:
                        name = next(n for n in archive.namelist() if n.endswith(".mc"))
                        data = json.loads(archive.read(name).decode("utf-8"))
                for item in data["note"]:
                    if item.get("type") != 1:
                        continue
                    self.assertIsInstance(item["offset"], int)
                    self.assertIsInstance(item["vol"], int)
                for point in data["time"]:
                    self.assertTrue(
                        all(isinstance(value, int) for value in point["beat"]),
                        point["beat"],
                    )

    def test_osu_audio_lead_in_is_independent_from_mc_audio_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lead-in.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nAudioLeadIn: 125\nMode: 3\n"
                "[Metadata]\nTitle: Title\n[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.audio_lead_in_ms, 125.0)
            self.assertEqual(document.audio_delay_ms, 0.0)
            output = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(output.read_text(encoding="utf-8"))
            sound = next(note for note in data["note"] if note.get("sound"))
            self.assertEqual(sound["offset"], 0.0)

    def test_mc_offset_is_written_to_osu_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "offset.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "sound": "song.mp3", "offset": 250, "type": 1}],
            }), encoding="utf-8")
            output = Path(convert_paths([source], ".osu", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("AudioLeadIn: 0", text)
            self.assertIn("-250.000,500", text)
    def test_mc_audio_delay_is_written_to_sm_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "delay.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "sound": "song.mp3", "offset": 250, "type": 1}],
            }), encoding="utf-8")
            output = Path(convert_paths([source], ".sm", root)[0])
            self.assertIn("#MUSIC:song.mp3;", output.read_text(encoding="utf-8"))
            self.assertIn("#OFFSET:0.250000;", output.read_text(encoding="utf-8"))

    def test_mc_regular_note_sound_and_cover_are_used_as_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "native.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}, "cover": "cover.png"},
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0, "sound": "song.ogg"}],
            }), encoding="utf-8")
            document = parse_chart(source)[0]
            self.assertEqual(document.audio_filename, "")
            self.assertEqual(document.notes[0].sound, "song.ogg")
            self.assertEqual(document.cover_filename, "cover.png")
            output = Path(convert_paths([source], ".osu", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("AudioFilename: song.ogg", text)
            self.assertIn("0:0:0:0:song.ogg", text)
            self.assertIn('0,0,"cover.png",0,0', text)

    def test_osu_audio_offset_and_background_are_written_to_mc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: Title\n[Difficulty]\nCircleSize: 4\n"
                "[Events]\n0,0,\"cover.jpg\",0,0\n"
                "[TimingPoints]\n-250,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,-250,1,0\n",
                encoding="utf-8",
            )
            output = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["background"], "cover.jpg")
            sound = next(note for note in data["note"] if note.get("sound"))
            # The first osu! timing point is at -250ms, so beat 0 is 250ms
            # before the audio origin and the Malody audio note must retain
            # that alignment explicitly.
            self.assertEqual((sound["sound"], sound["offset"], sound["type"]), ("song.mp3", 250.0, 1))

    def test_osu_to_sm_preserves_audio_lead_in_and_multiple_bpm_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "multi.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\n"
                "AudioLeadIn: 125\nMode: 3\n[Metadata]\nTitle: Multi\nVersion: Hard\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n"
                "0,500,4,1,0,100,1,0\n1000,250,4,1,0,100,1,0\n"
                "2500,750,4,1,0,100,1,0\n[HitObjects]\n"
                "64,192,0,1,0\n64,192,1000,1,0\n64,192,1500,1,0\n"
                "64,192,2500,1,0\n64,192,3250,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("#OFFSET:0.000000;", text)
            self.assertIn("#BPMS:0.000000=120.000000,2.000000=240.000000,8.000000=80.000000;", text)
            converted = parse_chart(output)[0]
            self.assertEqual(converted.timing_beats, original.timing_beats)
            self.assertEqual(
                [(note.time, note.column, note.end_time) for note in converted.notes],
                [(note.time, note.column, note.end_time) for note in original.notes],
            )

    def test_osu_first_timing_point_after_zero_remains_beat_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "late-red-line.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: Late\n[Difficulty]\nCircleSize: 4\n[TimingPoints]\n"
                "1000,500,4,1,0,100,1,0\n2000,250,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,1000,1,0\n64,192,2000,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            self.assertEqual(original.timing_beats, [0.0, 2.0])
            for extension in (".sm", ".mc"):
                output = Path(convert_paths([source], extension, root)[0])
                converted = parse_chart(output)[0]
                self.assertEqual(converted.timing_beats, original.timing_beats)
                self.assertEqual(
                    [(note.time, note.column) for note in converted.notes],
                    [(note.time, note.column) for note in original.notes],
                )
            sm_output = next(path for path in root.glob("*.sm") if path != source)
            self.assertIn("#OFFSET:-1.000000;", sm_output.read_text(encoding="utf-8"))
            mc_output = next(path for path in root.glob("*.mc"))
            mc_data = json.loads(mc_output.read_text(encoding="utf-8"))
            sound = next(note for note in mc_data["note"] if note.get("type") == 1)
            self.assertEqual(sound["offset"], -1000.0)

    def test_osu_notes_before_the_first_red_line_anchor_beat_zero_at_audio(self):
        """Notes before the first red line must not push beat zero forward.

        JACK-DELTA puts its first uninherited point at 37700ms while its notes
        start at 300ms and the audio has music from 0ms.  Anchoring beat zero on
        that point made 413 notes negative and told Malody to start the song
        37.7s in, silencing the entire intro.  The grid is instead extended back
        to the audio origin with the same beat length, which leaves every note
        time untouched.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "jack-delta.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: audio.ogg\nMode: 3\n"
                "[Metadata]\nTitle: J\n[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n37700,400,4,1,0,20,1,0\n"
                "[HitObjects]\n192,192,300,1,0\n320,192,500,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            self.assertEqual(original.beat_zero_ms, 0.0)
            self.assertEqual(original.timing_beats, [0.0, 94.25])
            self.assertEqual(
                [note.time for note in original.notes], [300, 500]
            )

            for extension in (".mc", ".mcz"):
                output = Path(convert_paths([source], extension, root)[0])
                if extension == ".mcz":
                    import zipfile

                    with zipfile.ZipFile(output) as archive:
                        name = next(n for n in archive.namelist() if n.endswith(".mc"))
                        with tempfile.TemporaryDirectory() as inner:
                            archive.extract(name, inner)
                            document = parse_chart(Path(inner) / name)[0]
                            data = json.loads(
                                (Path(inner) / name).read_text(encoding="utf-8")
                            )
                else:
                    document = parse_chart(output)[0]
                    data = json.loads(output.read_text(encoding="utf-8"))
                # The song starts at the beginning, not 37.7s in.
                sound = next(item for item in data["note"] if item.get("type") == 1)
                self.assertEqual(sound["offset"], 0)
                self.assertEqual(document.beat_zero_ms, 0.0)
                # No note sits before beat zero any more.
                for item in data["note"]:
                    if "column" in item:
                        self.assertGreaterEqual(item["beat"][0], 0, item)
                # And the notes still land on the times the source authored.
                self.assertEqual(
                    sorted(note.time for note in document.notes), [300, 500]
                )

    def test_osu_empty_lead_in_keeps_the_authored_beat_zero(self):
        """A gap before the first red line with no notes must not renumber beats.

        The anchor only moves when content actually precedes the first point;
        otherwise the authored anchor and its beat numbers are preserved.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "gap.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: G\n[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n3000,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,3000,1,0\n",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.timing_beats, [0.0])
            self.assertEqual(document.beat_zero_ms, 3000.0)
            self.assertEqual([note.time for note in document.notes], [3000])

    def test_sm_to_osu_writes_offset_once_with_multiple_bpm_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "multi.sm"
            source.write_text(
                "#TITLE:Multi;#MUSIC:song.mp3;#OFFSET:0.5;"
                "#BPMS:0=120,4=240,10=80;#NOTES:\n"
                " dance-single:\n :\n Hard:\n 4:\n 0.000:\n"
                "1000\n" + ",\n".join(["0000"] * 47) + ",\n"
                "1000\n" + ",\n".join(["0000"] * 71) + ",\n"
                "1000\n;",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".osu", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("AudioLeadIn: 0", text)
            self.assertIn("-500.000,500", text)
            self.assertIn(",192,-500,1,0", text)
            converted = parse_chart(output)[0]
            self.assertEqual(
                [(note.time, note.column, note.end_time) for note in converted.notes],
                [(note.time, note.column, note.end_time) for note in original.notes],
            )
            self.assertEqual(converted.timing_beats, original.timing_beats)

    def test_mc_to_sm_keeps_audio_delay_and_bpm_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "multi.mc"
            source.write_text(
                json.dumps({
                    "meta": {
                        "mode": 0,
                        "mode_ext": {"column": 4},
                        "song": {"title": "Multi", "file": "song.mp3"},
                    },
                    "time": [
                        {"beat": [0, 0, 1], "bpm": 120},
                        {"beat": [4, 0, 1], "bpm": 240},
                    ],
                    "note": [
                        {"beat": [0, 0, 1], "column": 0},
                        {"beat": [4, 0, 1], "column": 1},
                        {"beat": [0, 0, 1], "sound": "song.mp3", "offset": 250, "type": 1},
                    ],
                }),
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("#OFFSET:0.250000;", text)
            self.assertIn("#BPMS:0.000000=120.000000,4.000000=240.000000;", text)
            converted = parse_chart(output)[0]
            self.assertEqual(converted.timing_beats, original.timing_beats)
            self.assertEqual(
                [(note.time, note.column) for note in converted.notes],
                [(note.time, note.column) for note in original.notes],
            )

    def test_mc_offset_without_timing_array_is_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "implicit-timing.mc"
            source.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4}},
                "note": [
                    {"beat": [0, 0, 1], "column": 0},
                    {"beat": [0, 0, 1], "sound": "song.mp3", "offset": 250, "type": 1},
                ],
            }), encoding="utf-8")
            document = parse_chart(source)[0]
            self.assertEqual(document.beat_zero_ms, -250.0)
            output = Path(convert_paths([source], ".sm", root)[0])
            self.assertIn("#OFFSET:0.250000;", output.read_text(encoding="utf-8"))
            converted = parse_chart(output)[0]
            self.assertEqual([note.time for note in converted.notes], [-250])

    def test_mc_zero_metadata_offset_takes_precedence_over_legacy_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "zero-offset.mc"
            source.write_text(json.dumps({
                "meta": {
                    "mode": 0,
                    "mode_ext": {"column": 4},
                    "audio_offset": 0,
                    "offset": 250,
                },
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            document = parse_chart(source)[0]
            self.assertEqual(document.beat_zero_ms, 0.0)

    def test_mc_audio_variants_without_type_or_with_nested_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "variant.mc"
            source.write_text(json.dumps({
                "meta": {
                    "mode": 0,
                    "mode_ext": {"column": 4},
                    "song": {"file": "song.ogg", "offset": 175},
                },
                "audio_offset": 999,
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [
                    {"beat": [0, 0, 1], "column": 0},
                    {"beat": [0, 0, 1], "sound": "song.ogg", "offset": 175},
                ],
            }), encoding="utf-8")
            document = parse_chart(source)[0]
            self.assertEqual(document.audio_filename, "song.ogg")
            self.assertEqual(document.audio_delay_ms, 175.0)
            output = Path(convert_paths([source], ".mc", root)[0])
            data = json.loads(output.read_text(encoding="utf-8"))
            sound = next(item for item in data["note"] if item.get("sound"))
            self.assertEqual((sound["sound"], sound["offset"], sound["type"]), ("song.ogg", 175.0, 1))

    def test_sm_stops_and_delays_survive_multi_bpm_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "events.sm"
            source.write_text(
                "#TITLE:Events;#MUSIC:song.mp3;#BPMS:0=120,4=240;"
                "#STOPS:2=0.500000;#DELAYS:6=0.250000;#NOTES:\n"
                " dance-single:\n :\n Hard:\n 4:\n 0:\n"
                "1000\n,\n0000\n0000\n1000\n0000\n,\n"
                "1000\n0000\n0000\n0000\n,\n0000\n0000\n0000\n1000\n;",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("#STOPS:2.000000=0.500000;", text)
            self.assertIn("#DELAYS:6.000000=0.250000;", text)
            converted = parse_chart(output)[0]
            self.assertEqual(converted.timing_events, original.timing_events)
            self.assertEqual(
                [(note.time, note.column, note.end_time) for note in converted.notes],
                [(note.time, note.column, note.end_time) for note in original.notes],
            )

    def test_osu_explicit_lead_in_is_preserved_when_first_red_line_is_late(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "late-lead.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\n"
                "AudioLeadIn: 500\nMode: 3\n[Metadata]\nTitle: Late\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n"
                "1000,500,4,1,0,100,1,0\n[HitObjects]\n"
                "64,192,1000,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".osu", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertIn("AudioLeadIn: 500", text)
            self.assertIn("1000.000,500", text)
            self.assertEqual(
                [(note.time, note.column) for note in parse_chart(output)[0].notes],
                [(note.time, note.column) for note in original.notes],
            )

    def test_osu_positive_inherited_timing_point_is_not_a_bpm_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inherited.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: X\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n"
                "0,500,4,1,0,100,1,0\n1000,250,4,1,0,100,0,0\n"
                "[HitObjects]\n64,192,1000,1,0\n",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.timing_beats, [0.0])

    def test_osu_irregular_timestamps_stay_within_the_shared_sm_grid(self):
        """A .sm measure may not exceed the 192 rows every reader shares.

        ArrowVortex allocates exactly 192 rows per measure and derives its row
        step as ``192 / numLines`` with integer division, so a 1536-row measure
        makes the step 0 and collapses the entire measure onto one row.  Notes
        therefore snap to the 192nd grid: half a row at 120 BPM is 3.47ms, and
        that is the whole error budget this shared format can offer.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "irregular.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: X\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,123,1,0\n64,192,777,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".sm", root)[0])
            fields = _notes_fields(output.read_text(encoding="utf-8"))
            for measure in fields[5].split(","):
                rows = [line.strip() for line in measure.splitlines() if line.strip()]
                self.assertLessEqual(len(rows), SM_ROWS_PER_MEASURE)
            converted = parse_chart(output)[0]
            self.assertEqual(len(converted.notes), len(original.notes))
            self.assertEqual(
                [note.column for note in converted.notes],
                [note.column for note in original.notes],
            )
            worst = max(
                abs(a.time - b.time) for a, b in zip(original.notes, converted.notes)
            )
            self.assertLessEqual(worst, 4)

    def test_sm_note_exactly_on_a_measure_boundary_keeps_its_row(self):
        """A note on a measure boundary must not snap a row backwards.

        ``_time_to_beat`` returns 387.9999999999995 for an exact boundary, so
        deriving the measure first and the row second put the note on row 191
        of the previous measure instead of row 0 of the next one -- a whole row
        of error, and once it also folded 1077 of error.osu's notes onto row 0.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "boundary.osu"
            # 180 BPM with the first red line at 644ms, like the real fixture.
            # These times sit exactly on 0, 2 and 4 beats inside measures.
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: X\n"
                "[Difficulty]\nCircleSize: 4\n"
                "[TimingPoints]\n644,333.333333333333,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,644,1,0\n64,192,1977,1,0\n64,192,3310,1,0\n"
                "64,192,4643,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            output = Path(convert_paths([source], ".sm", root)[0])
            converted = parse_chart(output)[0]
            self.assertEqual(len(converted.notes), len(original.notes))
            worst = max(
                abs(a.time - b.time) for a, b in zip(original.notes, converted.notes)
            )
            self.assertLessEqual(worst, 1)
            # Every note must land on row 0 of some measure, never the last row
            # of the preceding one.
            fields = _notes_fields(output.read_text(encoding="utf-8"))
            measured = []
            for measure in fields[5].split(","):
                rows = [line.strip() for line in measure.splitlines() if line.strip()]
                hit = [index for index, row in enumerate(rows) if "1" in row]
                if hit:
                    measured.append((len(rows), hit))
            self.assertTrue(measured)
            for row_count, hit in measured:
                self.assertIn(0, hit)
                self.assertNotIn(row_count - 1, hit)

    def test_sm_measures_never_exceed_the_shared_row_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "off-grid.osu"
            # Beats that land between 192nd rows, which is what forces a row
            # grid decision.
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: X\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,333.333333333333,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,123,1,0\n128,192,3210,128,0,6543:0:0:0:0:\n",
                encoding="utf-8",
            )
            output = Path(convert_paths([source], ".sm", root)[0])
            fields = _notes_fields(output.read_text(encoding="utf-8"))
            counts = []
            for measure in fields[5].split(","):
                rows = [line.strip() for line in measure.splitlines() if line.strip()]
                counts.append(len(rows))
                self.assertLessEqual(len(rows), SM_ROWS_PER_MEASURE)
                self.assertIn(len(rows), SM_MEASURE_SUBDIVISIONS)
                for row in rows:
                    self.assertEqual(len(row), 4)
            self.assertTrue(counts)
            # The long note must survive the grid snap.
            converted = parse_chart(output)[0]
            self.assertEqual(len(converted.notes), 2)
            hold = next(note for note in converted.notes if note.end_time is not None)
            self.assertGreater(hold.end_time, hold.time)

    def test_sm_zero_meter_and_edit_description_are_normalised_for_arrowvortex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "arrowvortex.sm"
            source.write_text(
                "#TITLE:Rose;#MUSIC:rose.mp3;#BPMS:0=120;#NOTES:\n"
                " dance-single:\n ArrowVortex edit0:\n edit0:\n 0:\n"
                " 0.000,0.000,0.000,0.000,0.000:\n1000\n;",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.meter, "1")
            self.assertEqual(document.chart_description, "ArrowVortex edit0")
            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            fields = _notes_fields(text)
            self.assertEqual(fields[:5], [
                "dance-single", "ArrowVortex edit0", "edit", "1",
                "0.000,0.000,0.000,0.000,0.000",
            ])

    def test_sm_real_osu_version_with_slash_comment_keeps_fields_aligned(self):
        # Reproduces the reported bug: "Version:Extra-Final Infinity Heaven //
        # tera" used to be written verbatim, so every MSD reader deleted the
        # rest of that line, lost the trailing ':' and shifted the meter field
        # onto the radar values.  The meter then read 0 and the chart loaded as
        # an unnamed edit difficulty.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "error.osu"
            source.write_text(
                "osu file format v14\n"
                "[General]\nAudioFilename: HyuN - Infinity Heaven.mp3\n"
                "AudioLeadIn: 0\nMode: 3\n"
                "[Metadata]\nTitle:Malody 4K Extra Dan v3-Jack\n"
                "Artist:Various Artists\nCreator:[GS]hina\n"
                "Version:Extra-Final Infinity Heaven // tera\n"
                "[Difficulty]\nCircleSize:4\n"
                "[TimingPoints]\n644,333.333333333333,4,2,1,5,1,0\n"
                "[HitObjects]\n64,192,644,1,0\n192,192,977,128,0,1310:0:0:0:0:\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            self.assertEqual(original.version, "Extra-Final Infinity Heaven // tera")

            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            fields = _notes_fields(text)
            # StepMania rejects a #NOTES value with fewer than seven fields.
            self.assertGreaterEqual(len(fields), 6)
            self.assertEqual(fields[0], "dance-single")
            self.assertEqual(fields[1], "Extra-Final Infinity Heaven / tera")
            self.assertEqual(_normalise_sm_meter(fields[3]), 1)
            self.assertNotIn("//", text)
            # No chart field may carry a line break, or the missing-';'
            # recovery rule could treat the next line as a new tag.
            self.assertFalse(any("\n" in field for field in fields[:5]))

            converted = parse_chart(output)[0]
            self.assertEqual(converted.key_count, 4)
            self.assertEqual(converted.meter, "1")
            self.assertEqual(converted.version, "edit")
            self.assertEqual(converted.chart_description, "Extra-Final Infinity Heaven / tera")
            # Column order and count are exact; the timestamps land on the
            # shared 192nd grid, so they may move by half a row.  One row is
            # 333.33ms / 48 = 6.94ms here, so allow one millisecond of grid
            # snap for starts and the same for long-note tails.
            self.assertEqual(
                [(note.time, note.column) for note in converted.notes],
                [(note.time, note.column) for note in original.notes],
            )
            for source_note, converted_note in zip(original.notes, converted.notes):
                self.assertLessEqual(abs(source_note.time - converted_note.time), 2)
                if source_note.end_time is None:
                    self.assertIsNone(converted_note.end_time)
                else:
                    self.assertLessEqual(
                        abs(source_note.end_time - converted_note.end_time), 2
                    )

    def test_sm_note_rows_never_carry_a_trailing_measure_comma(self):
        # "1100," counted the separator as a fifth lane, so every 4K chart read
        # back as 5K.
        for key_count in (4, 7):
            with self.subTest(key_count=key_count):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "width.osu"
                    rows = []
                    for index in range(key_count):
                        rows.append(
                            f"{int((index + 0.5) * 512 / key_count)},192,{index * 250},1,0"
                        )
                    source.write_text(
                        "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: W\n"
                        f"[Difficulty]\nCircleSize: {key_count}\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                        "[HitObjects]\n" + "\n".join(rows) + "\n",
                        encoding="utf-8",
                    )
                    original = parse_chart(source)[0]
                    self.assertEqual(original.key_count, key_count)
                    output = Path(convert_paths([source], ".sm", root)[0])
                    text = output.read_text(encoding="utf-8")
                    body = _reference_msd_values(text)[0]
                    note_fields = [
                        fields for fields in _reference_msd_values(text)
                        if fields[:1] == ["NOTES"]
                    ][0]
                    for measure in note_fields[6].split(","):
                        for line in measure.splitlines():
                            row = line.strip()
                            if row:
                                self.assertNotIn(",", row)
                                self.assertEqual(len(row), key_count)
                    converted = parse_chart(output)[0]
                    self.assertEqual(converted.key_count, key_count)
                    self.assertEqual(
                        sorted((note.time, note.column) for note in converted.notes),
                        sorted((note.time, note.column) for note in original.notes),
                    )
                    # The lane count must not grow when written to .mc either.
                    mc_output = Path(convert_paths([source], ".mc", root)[0])
                    self.assertEqual(parse_chart(mc_output)[0].key_count, key_count)

    def test_sm_lanes_without_notes_are_kept_in_the_key_count(self):
        """A chart that never uses its last lane is still that many lanes.

        StepMania and ArrowVortex read the lane count from the width of a note
        row (``0`` marks a lane that stays empty), so lanes that carry no note
        must survive a conversion instead of shrinking the chart.
        """
        for key_count, chart_type, used_columns in (
            (4, "dance-single", (0, 1, 2)),
            (7, "kb7-single", (0, 2, 4)),
        ):
            with self.subTest(key_count=key_count):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "sparse.sm"
                    rows = []
                    for row_index in range(4):
                        row = ["0"] * key_count
                        if row_index < len(used_columns):
                            row[used_columns[row_index]] = "1"
                        rows.append("".join(row))
                    source.write_text(
                        "#TITLE:Sparse;#BPMS:0=120;#NOTES:\n"
                        f" {chart_type}:\n :\n Hard:\n 5:\n 0.000:\n"
                        + "\n".join(rows) + "\n;",
                        encoding="utf-8",
                    )
                    original = parse_chart(source)[0]
                    self.assertEqual(original.key_count, key_count)
                    self.assertEqual(
                        sorted({note.column for note in original.notes}), sorted(used_columns)
                    )
                    for target in (".sm", ".osu", ".mc"):
                        output = Path(convert_paths([source], target, root)[0])
                        converted = parse_chart(output)[0]
                        self.assertEqual(converted.key_count, key_count, target)
                        self.assertEqual(
                            sorted({note.column for note in converted.notes}),
                            sorted(used_columns),
                            target,
                        )
                        if target == ".sm":
                            self.assertEqual(converted.chart_type, chart_type)
                            note_fields = _notes_fields(output.read_text(encoding="utf-8"))
                            for measure in note_fields[5].split(","):
                                for line in measure.splitlines():
                                    row = line.strip()
                                    if row:
                                        self.assertEqual(len(row), key_count)

    def test_osu_source_keeps_lanes_that_carry_no_notes(self):
        """``CircleSize`` is the lane count even when a column stays empty."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sparse.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: S\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,1000,1,0\n192,192,1500,1,0\n320,192,2000,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            self.assertEqual(original.key_count, 4)
            output = Path(convert_paths([source], ".sm", root)[0])
            note_fields = _notes_fields(output.read_text(encoding="utf-8"))
            for measure in note_fields[5].split(","):
                for line in measure.splitlines():
                    row = line.strip()
                    if row:
                        self.assertEqual(len(row), 4)
            converted = parse_chart(output)[0]
            self.assertEqual(converted.key_count, 4)
            self.assertEqual(converted.chart_type, "dance-single")
            self.assertEqual(
                [(note.time, note.column) for note in converted.notes],
                [(note.time, note.column) for note in original.notes],
            )

    def test_sm_keysound_references_do_not_add_lanes_or_notes(self):
        """``[index]`` is a keysound reference, not a lane and not a note."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "keysounded.sm"
            source.write_text(
                "#TITLE:Keysound;#KEYSOUNDS:snd1.wav,snd2.wav;#BPMS:0=120;#NOTES:\n"
                " dance-single:\n :\n Hard:\n 5:\n 0.000:\n"
                "1000[1]\n0100[2]\n0010[1]\n0000\n;",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.key_count, 4)
            self.assertEqual(
                [(note.time, note.column) for note in document.notes],
                [(0, 0), (500, 1), (1000, 2)],
            )
            events = []
            output = Path(
                convert_paths(
                    [source], ".osu", root,
                    emit_event=lambda kind, payload: events.append((kind, payload)),
                )[0]
            )
            converted = parse_chart(output)[0]
            self.assertEqual(converted.key_count, 4)
            self.assertEqual(len(converted.notes), 3)
            self.assertTrue(
                any(kind == "warning" and "按键音" in str(payload) for kind, payload in events),
                events,
            )

    def test_sm_multiplayer_rows_are_reported(self):
        """``&`` separates players; the shared model is single-player only."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "routine.sm"
            source.write_text(
                "#TITLE:Routine;#BPMS:0=120;#NOTES:\n"
                " dance-routine:\n :\n Hard:\n 5:\n 0.000:\n"
                "1000&0000\n0100&0000\n0000&0001\n0000&0000\n;",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertTrue(document.extra.get("sm_multiplayer"))
            events = []
            convert_paths(
                [source], ".osu", root,
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            self.assertTrue(
                any(kind == "warning" and "多人" in str(payload) for kind, payload in events),
                events,
            )

    def test_sm_multi_measure_4k_and_7k_key_count_round_trip(self):
        for key_count, chart_type in ((4, "dance-single"), (7, "kb7-single")):
            with self.subTest(key_count=key_count):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "multi.sm"
                    measures = []
                    for measure_index in range(3):
                        rows = []
                        for row_index in range(192):
                            row = ["0"] * key_count
                            if row_index == measure_index:
                                row[measure_index % key_count] = "1"
                            if row_index == 96:
                                row[0] = "2"
                            if row_index == 120:
                                row[0] = "3"
                            rows.append("".join(row))
                        measures.append("\n".join(rows))
                    source.write_text(
                        "#TITLE:Multi;#BPMS:0=120;#NOTES:\n"
                        f" {chart_type}:\n :\n Hard:\n 9:\n 0.000:\n"
                        + ",\n".join(measures) + "\n;",
                        encoding="utf-8",
                    )
                    original = parse_chart(source)[0]
                    self.assertEqual(original.key_count, key_count)
                    self.assertEqual(original.chart_type, chart_type)
                    output = Path(convert_paths([source], ".sm", root)[0])
                    converted = parse_chart(output)[0]
                    self.assertEqual(converted.key_count, key_count)
                    self.assertEqual(converted.chart_type, chart_type)
                    self.assertEqual(
                        [(note.time, note.column, note.end_time) for note in converted.notes],
                        [(note.time, note.column, note.end_time) for note in original.notes],
                    )

    def test_sm_comment_markers_in_metadata_are_removed_everywhere(self):
        self.assertEqual(
            _normalise_sm_field("difficulty //// artist: part; x", notes_field=True),
            "difficulty / artist\\: part, x",
        )
        self.assertEqual(_normalise_sm_field("////"), "/")
        self.assertEqual(_normalise_sm_field("a ///// b"), "a / b")
        self.assertNotIn("//", _normalise_sm_field("tail //"))
        self.assertEqual(
            _normalise_sm_field("line\r\nbreak"), "line  break"
        )
        self.assertEqual(
            _normalise_sm_resource("http://host/cover.jpg"), "http:/host/cover.jpg"
        )
        # A resource escape would change the referenced file, so only the
        # comment marker is removed there.
        self.assertEqual(_normalise_sm_resource("a;b.png"), "a;b.png")

    def test_sm_written_metadata_never_starts_a_tag_without_a_semicolon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "meta"
            root.mkdir()
            source = root / "nasty.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle:#hash // and ; semis\nArtist:A//B\n"
                "Creator:C://D\nVersion:V #1 // v2\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("//", text)
            document = parse_chart(output)[0]
            self.assertIn("#hash", document.title)
            self.assertIn("/ and", document.title)
            self.assertNotIn("//", document.artist)
            self.assertEqual(document.creator, "C:/D")

    def test_sm_chart_type_is_kept_consistent_with_the_lane_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mismatch.sm"
            # A chart that declares dance-single but carries seven lanes makes
            # both readers reinterpret every row.
            source.write_text(
                "#TITLE:M;#BPMS:0=120;#NOTES:\n"
                " dance-single:\n :\n Hard:\n 5:\n 0.000:\n1000000\n;",
                encoding="utf-8",
            )
            document = parse_chart(source)[0]
            self.assertEqual(document.key_count, 7)
            self.assertEqual(document.chart_type, "kb7-single")
            self.assertEqual(document.extra["sm_source_chart_type"], "dance-single")
            output = Path(convert_paths([source], ".sm", root)[0])
            fields = _notes_fields(output.read_text(encoding="utf-8"))
            self.assertEqual(fields[0], "kb7-single")
            self.assertEqual(parse_chart(output)[0].key_count, 7)

    def test_sm_escaped_colon_in_metadata_survives_the_field_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "colon.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
                "[Metadata]\nTitle: T\nVersion: Artist: Subtitle (Hard)\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            output = Path(convert_paths([source], ".sm", root)[0])
            fields = _notes_fields(output.read_text(encoding="utf-8"))
            self.assertEqual(fields[2], "edit")
            self.assertEqual(_msd_unescape(fields[1]), "Artist: Subtitle (Hard)")
            document = parse_chart(output)[0]
            self.assertIn("Artist: Subtitle", document.chart_description)

    def test_notes_data_between_measures_is_padded_without_extra_commas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: X\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,0,1,0\n64,192,4000,1,0\n",
                encoding="utf-8",
            )
            output = Path(convert_paths([source], ".sm", root)[0])
            fields = _notes_fields(output.read_text(encoding="utf-8"))
            # The note data starts on the header line, so neither reader can
            # mistake a leading blank line for a note row.
            self.assertFalse(fields[5].startswith("\n"))
            self.assertFalse(fields[5].startswith(" "))
            # Measures are separated by commas only, so no stray empty measure
            # appears after the last real one.
            self.assertFalse(fields[5].rstrip().endswith(","))

    def test_osu_audio_lead_in_does_not_shift_rose_quartz_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rose-quartz.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: rose.mp3\n"
                "AudioLeadIn: 1500\nMode: 3\n[Metadata]\nTitle: Rose Quartz\n"
                "[Difficulty]\nCircleSize: 7\n[TimingPoints]\n"
                "-60,500,4,1,0,100,1,0\n[HitObjects]\n"
                "64,192,3227,1,0\n",
                encoding="utf-8",
            )
            original = parse_chart(source)[0]
            self.assertEqual(original.audio_lead_in_ms, 1500.0)
            self.assertEqual(original.notes[0].time, 3227)
            mc_output = Path(convert_paths([source], ".mc", root)[0])
            mc_data = json.loads(mc_output.read_text(encoding="utf-8"))
            sound = next(note for note in mc_data["note"] if note.get("sound"))
            self.assertEqual(sound["offset"], 60.0)
            sm_output = Path(convert_paths([source], ".sm", root)[0])
            sm_text = sm_output.read_text(encoding="utf-8")
            self.assertIn("#OFFSET:0.060000;", sm_text)
            self.assertNotIn("#OFFSET:1.560000;", sm_text)

    def test_reference_reader_reproduces_the_reported_meter_zero_shift(self):
        """The exact failure mechanism behind "edit0 has an invild meter 0".

        This pins the root cause with the independent reader: the old layout
        put the comment inside the difficulty field, so the reader deleted the
        rest of that line (including its trailing ':').  The value then had one
        field too few, the meter landed on the radar values and numeric parsing
        of the field fell back to 0.
        """

        def layout(fields, body):
            return (
                "#TITLE:Rose;#BPMS:0=120;#NOTES:\n"
                + "".join(f" {field}:\n" for field in fields)
                + body
                + ";"
            )

        broken = layout(
            ["dance-single", "", "Extra-Final Infinity Heaven // tera", "1",
             "0.000,0.000,0.000,0.000,0.000"],
            "1000\n",
        )
        broken_values = _reference_msd_values(broken)
        self.assertEqual(len(broken_values), 3)
        broken_fields = broken_values[2]
        # Only six chart fields survive, and the ':' that should have closed
        # the difficulty line was deleted with the comment.  The value then
        # swallowed the next line, so the meter field now carries the radar
        # row and atoi() of it is 0.
        self.assertEqual(len(broken_fields), 6)
        # The difficulty field absorbed the next line, so the value shifts by
        # one: the meter position now holds a non-numeric string and reads as 0.
        self.assertIn("Extra-Final Infinity Heaven", broken_fields[3])
        self.assertIn("1", broken_fields[3])
        self.assertIn("0.000,0.000", broken_fields[4])
        self.assertNotEqual(broken_fields[4].strip(), "1")

        fixed = layout(
            ["dance-single", "Extra-Final Infinity Heaven / tera", "edit", "1",
             "0.000,0.000,0.000,0.000,0.000"],
            "1000\n",
        )
        fixed_values = _reference_msd_values(fixed)
        self.assertEqual(len(fixed_values), 3)
        fixed_fields = fixed_values[2]
        self.assertEqual(len(fixed_fields), 7)
        self.assertEqual(fixed_fields[3].strip(), "edit")
        self.assertEqual(fixed_fields[4].strip(), "1")

    def test_sm_project_reader_matches_the_reference_msd_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "parity"
            root.mkdir()
            source = root / "nasty.osu"
            source.write_text(
                "osu file format v14\n[General]\nAudioFilename: a.mp3\nMode: 3\n"
                "[Metadata]\nTitle:T//x\nArtist:A;B\nVersion:V //// W #z\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            output = Path(convert_paths([source], ".sm", root)[0])
            text = output.read_text(encoding="utf-8")
            project = [
                [_msd_unescape(item) for item in fields]
                for fields in _project_msd_values(text)
            ]
            reference = _reference_msd_values(text)
            self.assertEqual(project, reference)
            # The project reader resolves escapes while scanning, so every
            # value must be free of the raw escape sequences the reference
            # reader already removed.
            self.assertNotIn("\\", "".join(
                item for fields in project for item in fields
            ))


if __name__ == "__main__":
    unittest.main()
