"""Regression tests for the chart conversion integrity fixes (FIX-01/02/04).

Only the public conversion API is used: the plan the user is shown before a run
must be exactly what the writer produces, every archive member must keep a
stable identity derived from its source chart, and a resource a chart names but
that does not exist must be diagnosed instead of silently dropped.
"""

import json
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from osu_toolbox.chart_convert import (
    ArchiveMemberPlanner,
    ChartConversionError,
    analyze_chart_resources,
    convert_paths,
    parse_chart,
    planned_output_paths,
    preflight_conversion,
)
from osu_toolbox.tools.base import ToolContext
from osu_toolbox.tools.chart_convert import ChartConvertController


def _osu_text(version="Hard", audio="song.mp3", background=None, objects=None, key_count=4):
    """Return a minimal mania ``.osu`` body.

    ``objects`` is ``[(column, start_ms, end_ms_or_None, sample_file)]``.
    ``Mode: 3`` is mandatory: the converter refuses a chart whose declared game
    mode is not mania, so every fixture has to carry it.
    """
    if objects is None:
        objects = [(0, 1000, None, "")]
    lines = [
        "osu file format v14",
        "[General]",
        f"AudioFilename: {audio}",
        "Mode: 3",
        "[Metadata]",
        "Title: T",
        "Artist: A",
        "Creator: C",
        f"Version: {version}",
        "[Difficulty]",
        f"CircleSize: {key_count}",
    ]
    if background:
        lines.extend(["[Events]", f'0,0,"{background}",0,0'])
    lines.extend(["[TimingPoints]", "0,500,4,1,0,100,1,0", "[HitObjects]"])
    for column, start, end, sample in objects:
        x = int((column + 0.5) * 512 / key_count)
        if end is None:
            lines.append(f"{x},192,{start},1,0,0:0:0:0:{sample}")
        else:
            lines.append(f"{x},192,{start},128,0,{end}:0:0:0:0:{sample}")
    return "\n".join(lines) + "\n"


def _mc_text(version="Hard", columns=(0,)):
    """Return a minimal Malody Key ``.mc`` body with one note per column."""
    return json.dumps({
        "meta": {"mode": 0, "mode_ext": {"column": 4}, "version": version},
        "time": [{"beat": [0, 0, 1], "bpm": 120}],
        "note": [{"beat": [0, 0, 1], "column": column} for column in columns],
    })


def _sm_text(charts=(("Easy", "1000\n0000\n"), ("Hard", "0100\n0000\n")), title="T"):
    """Return a StepMania body with one ``#NOTES`` per ``(difficulty, rows)``."""
    parts = [f"#TITLE:{title};#BPMS:0=120;"]
    for difficulty, rows in charts:
        parts.append(f"#NOTES:\n dance-single:\n :\n {difficulty}:\n 5:\n 0.000:\n{rows};")
    return "".join(parts)


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_zip(path, members):
    """Write an archive whose values are ``str`` (encoded) or ``bytes``."""
    path = Path(path)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload if isinstance(payload, bytes) else payload.encode("utf-8"))
    return path


def _warnings(events):
    return [payload for kind, payload in events if kind == "warning"]


def _member_names(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        return sorted(archive.namelist())


def _read_member(archive_path, member):
    with zipfile.ZipFile(archive_path) as archive:
        return archive.read(member)


def _parsed_member(archive_path, member, directory, name=None):
    """Extract one archive member and parse it as a chart."""
    target = Path(directory) / (name or Path(member).name)
    target.write_bytes(_read_member(archive_path, member))
    return parse_chart(target)[0]


class OutputPlanIntegrityTests(unittest.TestCase):
    """FIX-01: the plan shown before the run is exactly what the run writes."""

    def test_archive_to_plain_plan_matches_the_written_outputs(self):
        """Planning an archive input must not fail and must agree with the run.

        ``planned_output_paths`` used to leave the unpacked-archive handle
        unbound on this path; a plan that cannot even be produced also cannot
        be compared with the execution result the user finally gets.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = _write_zip(root / "pack.osz", {"chart.osu": _osu_text()})
            out = root / "out"
            planned = planned_output_paths([archive], ".mc", out)
            self.assertEqual(planned, planned_output_paths([archive], ".mc", out))
            self.assertEqual([Path(item).name for item in planned], ["chart.mc"])
            written = convert_paths([archive], ".mc", out)
            self.assertEqual(planned, written)
            self.assertTrue(Path(written[0]).is_file())

    def test_mixed_input_plan_matches_the_written_outputs_in_order(self):
        """A mixed batch keeps input order and one identical name per output.

        A plain chart, an archive whose member identity differs from the outer
        package name, and a multi-difficulty StepMania file all run through the
        same planner, so the planned list must match the produced list item for
        item.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = _write(root / "plain.osu", _osu_text(version="Plain"))
            archive = _write_zip(root / "pack.osz", {"chart.osu": _osu_text(version="Packed")})
            steps = _write(root / "tune.sm", _sm_text())
            out = root / "out"
            inputs = [plain, archive, steps]
            planned = planned_output_paths(inputs, ".mc", out)
            written = convert_paths(inputs, ".mc", out)
            self.assertEqual(planned, written)
            self.assertEqual(
                [Path(item).name for item in planned],
                ["plain.mc", "chart.mc", "tune-Easy.mc", "tune-Hard.mc"],
            )
            self.assertEqual(
                [parse_chart(item)[0].version for item in written[2:]],
                ["Easy", "Hard"],
            )

    def test_expected_outputs_guard_accepts_the_plan_and_rejects_a_stale_one(self):
        """Execution re-checks the preflight plan instead of trusting it.

        When the fresh plan differs from ``expected_outputs`` the run must stop
        before writing anything, so a user never ends up with files at paths
        that were never shown (or confirmed) beforehand.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "plain.osu", _osu_text())
            out = root / "out"
            planned = planned_output_paths([source], ".mc", out)
            self.assertEqual(
                convert_paths([source], ".mc", out, expected_outputs=planned),
                planned,
            )
            stale = [str(out / "stale.mc")]
            with self.assertRaises(ChartConversionError) as caught:
                convert_paths([source], ".mc", out, expected_outputs=stale)
            self.assertIn("输出计划与预检结果不一致", str(caught.exception))
            self.assertFalse((out / "stale.mc").exists())

    def test_existing_output_needs_overwrite_while_a_batch_collision_does_not(self):
        """Existing files are an overwrite question; in-batch duplicates are not.

        Two inputs that want the same output name are disambiguated inside the
        batch (``-2``), but an output that already exists on disk must be
        refused until the caller allows overwriting, and a refused run must
        leave the existing file untouched.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write(root / "first" / "osu.osu", _osu_text(version="A"))
            second = _write(root / "second" / "osu.osu", _osu_text(version="B"))
            out = root / "out"
            written = convert_paths([first, second], ".mc", out)
            self.assertEqual([Path(item).name for item in written], ["osu.mc", "osu-2.mc"])
            self.assertEqual([parse_chart(item)[0].version for item in written], ["A", "B"])

            (out / "osu.mc").write_text("keep-me", encoding="utf-8")
            with self.assertRaises(FileExistsError) as caught:
                convert_paths([first, second], ".mc", out)
            message = str(caught.exception)
            self.assertIn("以下输出文件已存在", message)
            self.assertIn("osu.mc", message)
            self.assertEqual((out / "osu.mc").read_text(encoding="utf-8"), "keep-me")

            convert_paths([first, second], ".mc", out, allow_overwrite=True)
            self.assertEqual(parse_chart(out / "osu.mc")[0].version, "A")
            self.assertEqual(parse_chart(out / "osu-2.mc")[0].version, "B")


class OutputCommitTests(unittest.TestCase):
    def test_failed_writes_preserve_existing_outputs_and_remove_staging_files(self):
        for extension, writer in ((".mc", "write_chart"), (".mcz", "_write_archive_output")):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = _write(root / "chart.osu", _osu_text())
                output = root / "out"
                output.mkdir()
                destination = output / ("chart" + extension)
                destination.write_bytes(b"previous result")

                def fail(*args, **kwargs):
                    path = args[1] if writer == "write_chart" else args[3]
                    Path(path).write_bytes(b"partial result")
                    raise OSError("write interrupted")

                with patch("osu_toolbox.chart_convert." + writer, side_effect=fail):
                    with self.assertRaisesRegex(OSError, "write interrupted"):
                        convert_paths([source], extension, output, allow_overwrite=True)
                self.assertEqual(destination.read_bytes(), b"previous result")
                self.assertEqual(list(output.iterdir()), [destination])

    def test_failed_new_outputs_leave_no_partial_files(self):
        for extension, writer in ((".mc", "write_chart"), (".mcz", "_write_archive_output")):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = _write(root / "chart.osu", _osu_text())
                output = root / "out"

                def fail(*args, **kwargs):
                    path = args[1] if writer == "write_chart" else args[3]
                    Path(path).write_bytes(b"partial result")
                    raise OSError("write interrupted")

                with patch("osu_toolbox.chart_convert." + writer, side_effect=fail):
                    with self.assertRaisesRegex(OSError, "write interrupted"):
                        convert_paths([source], extension, output)
                self.assertEqual(list(output.iterdir()), [])

    def test_an_output_created_during_conversion_is_not_overwritten(self):
        from osu_toolbox.chart_convert import write_chart

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "chart.osu", _osu_text())
            output = root / "out"
            destination = output / "chart.mc"

            def write_with_rival(*args, **kwargs):
                write_chart(*args, **kwargs)
                destination.write_bytes(b"another process result")

            with patch("osu_toolbox.chart_convert.write_chart", side_effect=write_with_rival):
                with self.assertRaises(FileExistsError):
                    convert_paths([source], ".mc", output)
            self.assertEqual(destination.read_bytes(), b"another process result")
            self.assertEqual(list(output.iterdir()), [destination])


class ArchiveInputCollisionTests(unittest.TestCase):
    def test_equivalent_input_members_are_rejected_instead_of_losing_a_chart(self):
        for names in (("chart.osu", "Chart.osu"), ("chart.osu", "nested/../chart.osu")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = _write_zip(root / "pack.osz", {
                    names[0]: _osu_text(version="First"),
                    names[1]: _osu_text(version="Second"),
                })
                output = root / "out"
                with self.assertRaisesRegex(ChartConversionError, "归档成员路径重复"):
                    preflight_conversion([source], ".mcz", output)
                with self.assertRaisesRegex(ChartConversionError, "归档成员路径重复"):
                    convert_paths([source], ".mcz", output)
                self.assertFalse((output / "pack.mcz").exists())


class ArchiveMemberIdentityTests(unittest.TestCase):
    """FIX-02: a member is named after its own source chart, never a neighbour."""

    def test_two_charts_with_the_same_difficulty_keep_their_own_identity(self):
        """Same difficulty name in different source files must not merge.

        Both charts are called ``Hard``, so an outer-package identity would
        collapse them onto one member and silently drop the second chart's
        notes.  Each member must instead be the source file's own stem, with
        its own data.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = _write_zip(root / "pack.osz", {
                "a.osu": _osu_text(version="Hard", objects=[(0, 1000, None, "")]),
                "b.osu": _osu_text(version="Hard", objects=[(1, 1000, None, "")]),
            })
            packed = Path(convert_paths([archive], ".mcz", root / "out")[0])
            names = _member_names(packed)
            self.assertEqual(names, ["a.mc", "b.mc"])
            self.assertNotIn("pack-Hard.mc", names)
            columns = {
                name: [note.column for note in _parsed_member(packed, name, root).notes]
                for name in names
            }
            self.assertEqual(columns, {"a.mc": [0], "b.mc": [1]})

    def test_same_stem_in_two_formats_produces_two_members(self):
        """``a.osu`` and ``a.mc`` are different charts and must both survive."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = _write_zip(root / "pack.osz", {
                "a.osu": _osu_text(version="FromOsu"),
                "a.mc": _mc_text(version="FromMc"),
            })
            packed = Path(convert_paths([archive], ".mcz", root / "out")[0])
            names = [name for name in _member_names(packed) if name.endswith(".mc")]
            self.assertEqual(names, ["a-2.mc", "a.mc"])
            self.assertEqual(len({name.casefold() for name in names}), len(names))
            versions = sorted(_parsed_member(packed, name, root, name=f"stem-{name}").version
                              for name in names)
            self.assertEqual(versions, ["FromMc", "FromOsu"])

    def test_a_nested_chart_keeps_its_archive_folder(self):
        """A chart inside an archive subfolder keeps that folder in placement.

        Flattening it would break the reference a nested chart uses to reach a
        resource that sits in the package root.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = _write_zip(root / "pack.osz", {"sub/inner.osu": _osu_text(version="Inner")})
            packed = Path(convert_paths([archive], ".mcz", root / "out")[0])
            self.assertIn("sub/inner.mc", _member_names(packed))

    def test_a_shared_resource_is_packed_once(self):
        """Two charts naming the same audio must yield one audio member."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = _write_zip(root / "pack.osz", {
                "a.osu": _osu_text(audio="song.mp3"),
                "b.osu": _osu_text(audio="song.mp3"),
                "song.mp3": b"shared-audio",
            })
            packed = Path(convert_paths([archive], ".mcz", root / "out")[0])
            names = _member_names(packed)
            self.assertEqual([name for name in names if name.endswith(".mp3")], ["song.mp3"])
            self.assertEqual(_read_member(packed, "song.mp3"), b"shared-audio")

    def test_a_resource_never_overwrites_a_chart_member(self):
        """A resource and a chart that want the same name must both survive.

        Resources are claimed before charts, so the chart is the one that gets
        the ``-2`` counter and the chart's own reference still names a member
        that exists in the archive (resources and charts share one namespace).
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_resource = _mc_text(version="Resource", columns=(3,)).encode("utf-8")
            archive = _write_zip(root / "pack.osz", {
                "inner.osu": _osu_text(background="inner.mc", objects=[(1, 1000, None, "")]),
                # Any member with a chart extension is read as a chart, so the
                # colliding resource has to be a parseable chart file as well.
                "inner.mc": raw_resource,
            })
            first = Path(convert_paths([archive], ".mcz", root / "out")[0])
            second = Path(convert_paths([archive], ".mcz", root / "out2")[0])
            names = _member_names(first)
            self.assertEqual(names, _member_names(second))
            self.assertEqual(names, ["inner-2.mc", "inner-3.mc", "inner.mc"])
            self.assertEqual(len({name.casefold() for name in names}), len(names))
            # The resource is copied verbatim: no chart output may occupy it.
            self.assertEqual(_read_member(first, "inner.mc"), raw_resource)
            references = {}
            for name in names:
                if name == "inner.mc":
                    continue
                document = _parsed_member(first, name, root, name=f"conflict-{name}")
                if document.background_filename:
                    references[name] = document.background_filename
            self.assertEqual(list(references.values()), ["inner.mc"])
            self.assertIn("inner.mc", names)

    def test_case_equivalent_references_share_a_member_but_inputs_cannot_collide(self):
        """Reference aliases may share a file; conflicting source files cannot."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner = ArchiveMemberPlanner()
            self.assertEqual(planner.claim("Song.mp3"), "Song.mp3")
            self.assertEqual(planner.claim("song.mp3"), "song-2.mp3")

            single = _write_zip(root / "case.osz", {
                "a.osu": _osu_text(audio="Song.mp3"),
                "song.mp3": b"audio",
            })
            packed = Path(convert_paths([single], ".mcz", root / "out")[0])
            self.assertEqual(
                [name for name in _member_names(packed) if name.endswith(".mp3")],
                ["song.mp3"],
            )
            document = _parsed_member(packed, "a.mc", root)
            self.assertEqual(document.audio_filename, "song.mp3")

            both = _write_zip(root / "both.osz", {
                "a.osu": _osu_text(audio="Song.mp3"),
                "b.osu": _osu_text(audio="song.mp3"),
                "Song.mp3": b"song-upper",
                "song.mp3": b"song-lower",
            })
            with self.assertRaisesRegex(ChartConversionError, "归档成员路径重复"):
                convert_paths([both], ".mcz", root / "out2")
            self.assertFalse((root / "out2" / "both.mcz").exists())

    def test_archive_member_planner_treats_windows_equivalent_names_as_one(self):
        """Separators and case fold, and a collision never overwrites a member."""
        planner = ArchiveMemberPlanner()
        self.assertEqual(planner.claim("a\\b.png"), "a/b.png")
        self.assertIn("a\\b.png", planner)
        self.assertEqual(planner.claim("a/b.png"), "a/b-2.png")
        self.assertNotEqual("a/b.png".casefold(), "a/b-2.png".casefold())

        upper = planner.claim("X.PNG")
        lower = planner.claim("x.png")
        self.assertEqual((upper, lower), ("X.PNG", "x-2.png"))
        self.assertNotEqual(upper.casefold(), lower.casefold())

        planner.add("readme.txt")
        self.assertIn("README.TXT", planner)
        self.assertEqual(planner.claim("readme.txt"), "readme-2.txt")
        self.assertEqual(planner.claim("bundle"), "bundle")
        self.assertEqual(planner.claim("bundle"), "bundle-2")
        self.assertEqual(planner.claim("\\leading.png"), "leading.png")


class MissingResourceDiagnosticsTests(unittest.TestCase):
    """FIX-04: a named resource that does not exist is reported, exactly once."""

    def test_a_missing_main_audio_is_reported_once_for_an_archive(self):
        """The archive is still produced and the absent BGM is named once."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "song.osu", _osu_text(audio="not-there.mp3"))
            events = []
            written = convert_paths(
                [source], ".osz", root / "out",
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            self.assertTrue(Path(written[0]).is_file())
            self.assertEqual(_warnings(events), ["song.osu 引用的主音乐不存在：not-there.mp3"])

    def test_every_missing_resource_role_is_named_and_deduplicated(self):
        """Background, cover and keysound each get one diagnostic.

        A mania ``.osu`` has no separate cover field, so its background *is*
        the cover: one absent file is one fact and is reported once (as the
        background), not twice.  A format with a distinct cover (``.mc``)
        reports all three roles.  Two notes naming the same absent keysound
        must still produce a single keysound diagnostic.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "song.mp3").write_bytes(b"audio")
            source = _write(root / "song.osu", _osu_text(
                background="bg.png",
                objects=[(0, 1000, None, "hit.wav"), (1, 2000, None, "hit.wav")],
            ))
            events = []
            convert_paths(
                [source], ".osz", root / "out",
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            warnings = _warnings(events)
            self.assertEqual(len(warnings), len(set(warnings)))
            self.assertEqual(len(warnings), 2)
            self.assertEqual(len([item for item in warnings if "背景不存在" in item]), 1)
            self.assertEqual(len([item for item in warnings if "按键音不存在" in item]), 1)
            self.assertTrue(all("bg.png" in item or "hit.wav" in item for item in warnings))

    def test_a_distinct_cover_reports_all_three_roles(self):
        """Background, cover and keysound are three diagnostics when distinct."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "song.mp3").write_bytes(b"audio")
            source = _write(root / "song.mc", json.dumps({
                "meta": {
                    "mode": 0, "mode_ext": {"column": 4}, "version": "Hard",
                    "background": "bg.png", "cover": "cover.jpg",
                    "song": {"title": "T", "artist": "A", "file": "song.mp3"},
                },
                "time": [{"beat": [0, 0, 1], "bpm": 120}],
                "note": [
                    {"beat": [0, 0, 1], "column": 0, "sound": "hit.wav"},
                    {"beat": [0, 1, 2], "column": 1, "sound": "hit.wav"},
                ],
            }, ensure_ascii=False))
            events = []
            convert_paths(
                [source], ".mcz", root / "out",
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            warnings = _warnings(events)
            self.assertEqual(len(warnings), len(set(warnings)))
            self.assertEqual(len([item for item in warnings if "背景不存在" in item]), 1)
            self.assertEqual(len([item for item in warnings if "封面不存在" in item]), 1)
            self.assertEqual(len([item for item in warnings if "按键音不存在" in item]), 1)

    def test_a_plain_output_reports_the_missing_audio_and_the_dropped_resources(self):
        """A single-file output copies no resources, so it says so explicitly."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write(root / "charts" / "song.osu", _osu_text(audio="not-there.mp3"))
            events = []
            written = convert_paths(
                [source], ".mc", root / "out",
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            warnings = _warnings(events)
            self.assertTrue(Path(written[0]).is_file())
            self.assertIn("song.osu 引用的主音乐不存在：not-there.mp3", warnings)
            self.assertTrue(any(
                "输出为单个谱面文件，未复制其引用的资源文件" in item for item in warnings
            ))


class DanglingCounterDiagnosticsTests(unittest.TestCase):
    """A stale counter in a reference is explained, never silently substituted.

    Another tool's export can leave ``#MUSIC:BOOM2.mp3`` beside a file that is
    actually called ``BOOM.mp3``.  The converter keeps the reference exactly as
    the chart wrote it (guessing the "closest" file could attach another song's
    audio), but it names the nearby candidate so an unexplainable "missing
    file" becomes something the user can act on.
    """

    def _convert(self, root, audio, files):
        for name in files:
            (root / name).write_bytes(b"payload")
        source = _write(root / "BOOM.osu", _osu_text(audio=audio))
        events = []
        written = convert_paths(
            [source], ".osu", root / "out",
            emit_event=lambda kind, payload: events.append((kind, payload)),
        )
        return written, _warnings(events)

    def test_the_nearby_file_is_named_and_the_reference_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            written, warnings = self._convert(root, "BOOM2.mp3", ["BOOM.mp3"])
            message = next(item for item in warnings if "BOOM2.mp3" in item)
            self.assertIn("同目录存在 BOOM.mp3", message)
            self.assertIn("不会自动替换", message)
            # The written chart still names exactly what the source named.
            text = Path(written[0]).read_text(encoding="utf-8")
            self.assertIn("AudioFilename: BOOM2.mp3", text)

    def test_every_counter_spelling_is_recognised(self):
        for audio, present in (
            ("BOOM2.mp3", "BOOM.mp3"),
            ("BOOM-2.mp3", "BOOM.mp3"),
            ("BOOM 2.mp3", "BOOM.mp3"),
            # The file carries the counter the chart never had.
            ("BOOM.mp3", "BOOM2.mp3"),
        ):
            with self.subTest(audio=audio, present=present):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    _, warnings = self._convert(root, audio, [present])
                    self.assertTrue(
                        any(f"同目录存在 {present}" in item for item in warnings),
                        warnings,
                    )

    def test_an_unrelated_missing_file_gets_no_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, warnings = self._convert(root, "gone.mp3", ["other.mp3"])
            message = next(item for item in warnings if "gone.mp3" in item)
            self.assertNotIn("同目录存在", message)

    def test_a_different_extension_is_not_offered(self):
        """A same-stem file of another type is not the audio the chart named."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, warnings = self._convert(root, "BOOM2.mp3", ["BOOM.jpg"])
            message = next(item for item in warnings if "BOOM2.mp3" in item)
            self.assertNotIn("同目录存在", message)


class ArchiveMemberRenameReportingTests(unittest.TestCase):
    """A member that had to be renamed is recorded and reported, not silent."""

    def test_the_planner_records_every_rename(self):
        planner = ArchiveMemberPlanner()
        self.assertEqual(planner.claim("song.mp3"), "song.mp3")
        self.assertEqual(planner.renamed, [])
        self.assertEqual(planner.claim("song.mp3"), "song-2.mp3")
        self.assertEqual(planner.renamed, [("song.mp3", "song-2.mp3")])

    def test_a_member_that_had_to_be_renamed_is_reported(self):
        """Two charts want one member name; the rename must not be silent."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # One ``.sm`` with two charts carrying the same difficulty name, so
            # both members want ``song-Hard.osu`` and the second is renamed.
            source = _write(root / "song.sm", _sm_text((
                ("Hard", "1000\n0000\n"),
                ("Hard", "0100\n0000\n"),
            )))
            events = []
            written = convert_paths(
                [source], ".osz", root / "out",
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            self.assertEqual(_member_names(written[0]), ["song-Hard-2.osu", "song-Hard.osu"])
            self.assertTrue(
                any(
                    "归档成员重名" in item and "song-Hard.osu → song-Hard-2.osu" in item
                    for item in _warnings(events)
                ),
                _warnings(events),
            )


class ResourceAnalysisBoundaryTests(unittest.TestCase):
    """The resource lookup boundary decides what counts as found or missing."""

    def test_a_relative_parent_reference_follows_the_stop_directory(self):
        """``../song.mp3`` resolves at package scope and misses at chart scope."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            nested = package / "nested"
            nested.mkdir(parents=True)
            (package / "song.mp3").write_bytes(b"audio")
            chart = _write(nested / "hard.osu", _osu_text(audio="../song.mp3"))
            pairs = [(chart, parse_chart(chart))]

            found, missing = analyze_chart_resources(pairs, stop=package)
            self.assertEqual(
                [(item.role, item.reference, item.member) for item in found],
                [("audio", "../song.mp3", "song.mp3")],
            )
            self.assertEqual(missing, [])

            found, missing = analyze_chart_resources(pairs, stop=nested)
            self.assertEqual(found, [])
            self.assertEqual(
                [(item.role, item.reference) for item in missing],
                [("audio", "../song.mp3")],
            )

    def test_an_absolute_reference_is_missing_instead_of_raising(self):
        """An absolute reference cannot resolve inside the package.

        It belongs in ``missing`` and the run must continue; it must never
        crash the analysis, the preflight or the conversion.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            nested = package / "nested"
            nested.mkdir(parents=True)
            absolute = "C:/x/y.mp3" if os.name == "nt" else "/x/y.mp3"
            chart = _write(nested / "hard.osu", _osu_text(audio=absolute))
            pairs = [(chart, parse_chart(chart))]

            found, missing = analyze_chart_resources(pairs, stop=package)
            self.assertEqual(found, [])
            self.assertEqual(
                [(item.role, item.reference) for item in missing],
                [("audio", absolute)],
            )


class PreflightConsistencyTests(unittest.TestCase):
    """The preflight summary describes what the conversion will really do."""

    def test_loss_summary_names_the_special_notes_a_source_really_has(self):
        """Only features present are reported, and the plan matches the planner."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            special = _write(root / "special.sm", _sm_text((("Hard", "4M00\n0000\n"),)))
            planned, warnings = preflight_conversion([special], ".osu", out)
            self.assertEqual(planned, planned_output_paths([special], ".osu", out))
            self.assertTrue(any(re.search(r"个\s*roll\s*音符", item) for item in warnings),
                            warnings)
            self.assertTrue(any("地雷" in item for item in warnings), warnings)

            plain = _write(root / "plain.sm", _sm_text((("Hard", "2000\n3000\n"),)))
            planned, warnings = preflight_conversion([plain], ".osu", out)
            self.assertEqual(planned, planned_output_paths([plain], ".osu", out))
            self.assertEqual([Path(item).name for item in planned], ["plain.osu"])
            # A tap/hold chart keeps every note, so there is nothing to warn about.
            self.assertEqual(warnings, [])


class ChartConvertControllerIntegrityTests(unittest.TestCase):
    """The tool layer surfaces the same plan, warnings and overwrite question."""

    def test_prepare_reports_losses_plans_outputs_and_asks_before_overwriting(self):
        """Prepare must expose the plan, the loss summary and the conflict."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            out.mkdir()
            source = _write(root / "special.sm", _sm_text((("Hard", "4M00\n0000\n"),)))
            controller = ChartConvertController()
            raw_params = {
                "input_paths": [str(source)],
                "target_format": ".osu",
                "output_location": "workdir",
            }
            context = ToolContext(str(root), output_root=str(out))
            prepared = controller.prepare(raw_params, context)
            self.assertTrue(prepared.warnings)
            self.assertTrue(any(re.search(r"个\s*roll\s*音符", item) for item in prepared.warnings))
            self.assertTrue(any("地雷" in item for item in prepared.warnings))
            planned = prepared.params["planned_outputs"]
            self.assertEqual(planned, planned_output_paths([source], ".osu", out))
            self.assertIsNone(prepared.confirmation)

            Path(planned[0]).write_text("taken", encoding="utf-8")
            prepared = controller.prepare(raw_params, context)
            self.assertIsNotNone(prepared.confirmation)
            self.assertIn(Path(planned[0]).name, prepared.confirmation.message)

            prepared.params["_allow_overwrite"] = True
            result = controller.run(prepared, lambda kind, payload: None)
            self.assertEqual(list(result.data["paths"]), list(planned))
            self.assertNotEqual(Path(planned[0]).read_text(encoding="utf-8"), "taken")

    def test_difficulty_params_are_per_run_overrides_of_the_tool_settings(self):
        from osu_toolbox.chart_settings import (
            CHART_SETTING_PARAMS,
            resolve_chart_difficulty,
        )
        from osu_toolbox.tools.chart_convert import CHART_CONVERT_PARAMS

        specs = {spec.key: spec for spec in CHART_CONVERT_PARAMS}
        for key in ("overall_difficulty", "hp_drain_rate"):
            spec = specs[key]
            # Empty means "use the 转谱工具 setting", so the form has no
            # hard-coded default of its own and never persists one.
            self.assertEqual(spec.kind, "optional_float")
            self.assertIsNone(spec.default)
            self.assertFalse(spec.persist)
            self.assertEqual((spec.minimum, spec.maximum), (0.0, 10.0))

        settings = {spec.key: spec for spec in CHART_SETTING_PARAMS}
        self.assertEqual(settings["chart_overall_difficulty"].default, 8.0)
        self.assertEqual(settings["chart_hp_drain_rate"].default, 8.0)

        # An empty field falls back to the setting; a typed one wins for this
        # run only; both are clamped into osu!'s 0-10 range.
        raw = {
            "chart_overall_difficulty": 9.5,
            "chart_hp_drain_rate": 7,
        }
        self.assertEqual(resolve_chart_difficulty(raw), (9.5, 7.0))
        self.assertEqual(resolve_chart_difficulty(raw, 3, None), (3.0, 7.0))
        self.assertEqual(resolve_chart_difficulty(raw, "", "  "), (9.5, 7.0))
        self.assertEqual(resolve_chart_difficulty(raw, 12, -1), (10.0, 0.0))
        self.assertEqual(resolve_chart_difficulty({}), (8.0, 8.0))

    def test_prepare_uses_the_tool_settings_when_the_form_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            out.mkdir()
            source = _write(root / "plain.sm", _sm_text((("Hard", "1000\n"),)))
            context = ToolContext(
                str(root),
                output_root=str(out),
                global_settings={
                    "chart_overall_difficulty": 6,
                    "chart_hp_drain_rate": 4.5,
                },
            )
            prepared = ChartConvertController().prepare({
                "input_paths": [str(source)],
                "target_format": ".osu",
                "output_location": "workdir",
            }, context)
            self.assertTrue(
                any("OverallDifficulty" in item and "6" in item for item in prepared.warnings),
                prepared.warnings,
            )
            result = ChartConvertController().run(prepared, lambda kind, payload: None)
            text = Path(result.data["paths"][0]).read_text(encoding="utf-8")
            self.assertIn("OverallDifficulty: 6\n", text)
            self.assertIn("HPDrainRate: 4.5\n", text)

    def test_prepare_announces_the_difficulty_and_run_writes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            out.mkdir()
            source = _write(root / "plain.sm", _sm_text((("Hard", "1000\n"),)))
            controller = ChartConvertController()
            raw_params = {
                "input_paths": [str(source)],
                "target_format": ".osu",
                "output_location": "workdir",
                "overall_difficulty": 8,
                "hp_drain_rate": 6,
            }
            context = ToolContext(str(root), output_root=str(out))
            prepared = controller.prepare(raw_params, context)
            self.assertTrue(
                any("OverallDifficulty" in item and "8" in item for item in prepared.warnings),
                prepared.warnings,
            )
            result = controller.run(prepared, lambda kind, payload: None)
            text = Path(result.data["paths"][0]).read_text(encoding="utf-8")
            self.assertIn("OverallDifficulty: 8\n", text)
            self.assertIn("HPDrainRate: 6\n", text)

    def test_plain_targets_do_not_announce_a_difficulty_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            out.mkdir()
            source = _write(root / "plain.sm", _sm_text((("Hard", "1000\n"),)))
            prepared = ChartConvertController().prepare({
                "input_paths": [str(source)],
                "target_format": ".mc",
                "output_location": "workdir",
            }, ToolContext(str(root), output_root=str(out)))
            self.assertFalse(
                any("OverallDifficulty" in item for item in prepared.warnings),
                prepared.warnings,
            )


if __name__ == "__main__":
    unittest.main()
