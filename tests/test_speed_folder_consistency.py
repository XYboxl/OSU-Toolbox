"""GUI、右键菜单的文件夹变速布局、内容与安全边界对照。"""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from osu_toolbox.schema import ParamValidationError
from osu_toolbox.tools.base import ToolContext
from osu_toolbox.tools.speed_change import SpeedChangeController
from tests import test_speed_change as fixtures


def _fake_resample(source, target, *_args, **_kwargs):
    Path(target).write_bytes(b"converted:" + Path(source).read_bytes())


def _original_tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _result_tree(root):
    result = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            result[name] = None
        elif path.suffix.lower() in (".osz", ".mcz"):
            with zipfile.ZipFile(path) as archive:
                result[name] = {member: archive.read(member) for member in archive.namelist()}
        else:
            result[name] = path.read_bytes()
    return result


def _song(root, title="T", version="Hard"):
    root.mkdir(parents=True)
    text = fixtures._osu_text(audio="song.wav").replace("Title:T\n", f"Title:{title}\n")
    text = text.replace("Version:Hard", f"Version:{version}")
    (root / "hard.osu").write_text(text, encoding="utf-8")
    (root / "song.wav").write_bytes(b"audio")
    (root / "Empty" / "Nested").mkdir(parents=True)
    return root


class SpeedFolderGuiBaselineTests(fixtures.SpeedFolderMenuLayoutTests):
    """Apply the menu's complete-folder contract to the GUI entry point too."""

    def _prepare(self, source, settings=None, **raw):
        controller = SpeedChangeController()
        context = ToolContext(str(source.parent), str(source.parent), settings or {}, invocation="gui")
        return controller, controller.prepare({"input_folders": str(source), "speed": 1.1, **raw}, context)


class SpeedFolderConsistencyTests(unittest.TestCase):
    @staticmethod
    def _prepare(root, folders, *, invocation="gui", settings=None, **raw):
        controller = SpeedChangeController()
        context = ToolContext(str(root), str(root), settings or {}, invocation=invocation)
        params = {"input_folders": [str(folder) for folder in folders], "speed": 1.1, **raw}
        return controller, controller.prepare(params, context)

    @staticmethod
    def _library(root):
        song = _song(root / "Alpha")
        (song / "hard.osu").write_text(
            fixtures._osu_text(audio="song.wav").replace("Title:T\n", "Title:T\nTitleUnicode:歌曲\n"),
            encoding="utf-8",
        )
        storage = song / "0"
        storage.mkdir()
        mc = fixtures.MalodyTimelineTests()._document(song={"title": "T", "file": "../song.wav"})
        (storage / "0.mc").write_text(mc.replace("song.mp3", "../song.wav"), encoding="utf-8")
        sm = fixtures.StepManiaTimelineTests._SM.replace("song.ogg", "song.wav")
        (song / "chart.sm").write_text(sm, encoding="utf-8")
        (song / "art.png").write_bytes(b"image")
        (root / "library.txt").write_bytes(b"notes")
        _song(root / "Beta", title="Other")
        for suffix, name, chart in (
            (".osz", "chart.osu", fixtures._osu_text(audio="../song.wav")),
            (".mcz", "0.mc", mc),
        ):
            with zipfile.ZipFile(song / ("pack" + suffix), "w") as archive:
                archive.writestr("charts/" + name, chart)
                archive.writestr("song.wav", b"packed audio")
                archive.writestr("unused.wav", b"unused")
                archive.writestr("Empty/", b"")

    def test_gui_output_locations_match_the_menu_tree_and_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            self._library(template)
            expected = None
            settings = {"speed_file_suffix": " [rate {num}]", "speed_difficulty_marker": " ({num}x)"}
            modes = (
                ("menu", "shell", "source", False),
                ("gui-default", "gui", "source", False),
                ("gui-workdir", "gui", "workdir", False),
                ("gui-custom", "gui", "source", True),
            )
            for label, invocation, location, custom in modes:
                with self.subTest(mode=label):
                    source = root / label / "inputs" / "Library"
                    shutil.copytree(template, source)
                    before = _original_tree(source)
                    output_parent = root / label / "output"
                    raw = {"output_location": location}
                    if custom:
                        raw.update(separate_folder=True, output_folder=str(output_parent))
                    controller, prepared = self._prepare(
                        output_parent, [source], invocation=invocation, settings=settings, **raw
                    )
                    parent = output_parent if custom or location == "workdir" else source.parent
                    destination = parent / "Library [rate 1.1]"
                    self.assertEqual(prepared.params["planned_outputs"], [str(destination)])
                    self.assertIsNone(prepared.confirmation)
                    with patch("osu_toolbox.speed_change.resample_audio", _fake_resample):
                        result = controller.run(prepared, lambda *_: None)
                    self.assertEqual(result.data["paths"], [str(destination)])
                    self.assertEqual(result.data["errors"], [])
                    self.assertEqual(_original_tree(source), before)
                    tree = _result_tree(destination)
                    renamed = {
                        "Alpha/hard.osu": "Alpha/hard [rate 1.1].osu",
                        "Alpha/song.wav": "Alpha/song [rate 1.1].wav",
                        "Alpha/0/0.mc": "Alpha/0/0 [rate 1.1].mc",
                        "Alpha/chart.sm": "Alpha/chart [rate 1.1].sm",
                        "Alpha/pack.osz": "Alpha/pack [rate 1.1].osz",
                        "Alpha/pack.mcz": "Alpha/pack [rate 1.1].mcz",
                        "Beta/hard.osu": "Beta/hard [rate 1.1].osu",
                        "Beta/song.wav": "Beta/song [rate 1.1].wav",
                    }
                    self.assertEqual(set(tree), {renamed.get(name, name) for name in before})
                    if expected is None:
                        expected = tree
                    else:
                        self.assertEqual(tree, expected)
                    osu = (destination / "Alpha" / "hard [rate 1.1].osu").read_text(encoding="utf-8")
                    self.assertIn("Title:T (1.1x)", osu)
                    self.assertIn("TitleUnicode:歌曲 (1.1x)", osu)
                    self.assertIn("Version:Hard (1.1x)", osu)
                    self.assertIn("AudioFilename: song [rate 1.1].wav", osu)
                    mc = json.loads((destination / "Alpha" / "0" / "0 [rate 1.1].mc").read_text(encoding="utf-8"))
                    self.assertEqual(mc["meta"]["song"]["title"], "T (1.1x)")
                    self.assertEqual(mc["meta"]["song"]["file"], "../song [rate 1.1].wav")
                    self.assertEqual(mc["meta"]["version"], "Hard (1.1x)")

    def test_batch_reserves_same_named_folder_outputs_before_any_write(self):
        for custom in (False, True):
            with self.subTest(custom=custom), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = _song(root / "a" / "Song", title="A")
                second = _song(root / "b" / "Song", title="B")
                output = root / "out"
                output.mkdir()
                (output / "Song 1.1x").write_bytes(b"occupied")
                raw = {"output_location": "workdir"}
                if custom:
                    raw.update(separate_folder=True, output_folder=str(output))
                controller, prepared = self._prepare(output, [first, second], **raw)
                expected = [str(output / "Song 2 1.1x"), str(output / "Song 3 1.1x")]
                self.assertEqual(prepared.params["planned_outputs"], expected)
                with patch("osu_toolbox.speed_change.resample_audio", _fake_resample):
                    result = controller.run(prepared, lambda *_: None)
                self.assertEqual(result.data["paths"], expected)
                for title, path in zip(("A", "B"), expected):
                    text = (Path(path) / "hard 1.1x.osu").read_text(encoding="utf-8")
                    self.assertIn(f"Title:{title} 1.1x", text)
                    self.assertTrue((Path(path) / "song 1.1x.wav").is_file())
                self.assertEqual((output / "Song 1.1x").read_bytes(), b"occupied")

    def test_overlapping_folders_and_explicit_inside_files_are_processed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "Library"
            child = _song(selected / "Song")
            before = _original_tree(selected)
            controller, prepared = self._prepare(
                root, [child, selected], input_paths=str(child / "hard.osu")
            )
            self.assertEqual(prepared.params["folders_selected"], [str(selected)])
            self.assertEqual(prepared.params["planned_outputs"], [str(root / "Library 1.1x")])
            with patch("osu_toolbox.speed_change.resample_audio", side_effect=_fake_resample) as audio:
                controller.run(prepared, lambda *_: None)
            self.assertEqual(audio.call_count, 1)
            self.assertEqual(_original_tree(selected), before)
            destination = root / "Library 1.1x"
            self.assertEqual(set(_original_tree(destination)), {
                "Song", "Song/Empty", "Song/Empty/Nested",
                "Song/hard 1.1x.osu", "Song/song 1.1x.wav",
            })

    def test_case_aliases_of_a_folder_do_not_allocate_two_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _song(root / "Song")
            alias = Path(str(source).replace("Song", "sONG"))
            if not alias.is_dir():
                self.skipTest("requires a case-insensitive filesystem")
            controller, prepared = self._prepare(root, [source, alias])
            self.assertEqual(prepared.params["planned_outputs"], [str(root / "Song 1.1x")])
            with patch("osu_toolbox.speed_change.resample_audio", side_effect=_fake_resample) as audio:
                controller.run(prepared, lambda *_: None)
            self.assertEqual(audio.call_count, 1)

    def test_mixed_files_and_folders_keep_their_own_naming_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = _song(root / "Folder", title="Folder")
            loose = _song(root / "Loose", title="Loose")
            archive = root / "pack.osz"
            with zipfile.ZipFile(archive, "w") as packed:
                packed.writestr("hard.osu", fixtures._osu_text(audio="song.wav"))
                packed.writestr("song.wav", b"packed")
            settings = {"speed_file_suffix": " [{num}x]", "speed_difficulty_marker": ""}
            controller, prepared = self._prepare(
                root, [folder], settings=settings,
                input_paths=[str(loose / "hard.osu"), str(archive)],
                difficulty_marker_pattern="none",
            )
            expected = [str(root / "Folder [1.1x]"), str(loose / "hard [1.1x].osu"), str(root / "pack [1.1x].osz")]
            self.assertEqual(prepared.params["planned_outputs"], expected)
            events = []
            with patch("osu_toolbox.speed_change.resample_audio", _fake_resample):
                result = controller.run(prepared, lambda *event: events.append(event))
            self.assertEqual(result.data["paths"], expected)
            self.assertEqual(result.data["errors"], [])
            folder_text = (root / "Folder [1.1x]" / "hard [1.1x].osu").read_text(encoding="utf-8")
            self.assertIn("Title:Folder [1.1x]", folder_text)
            self.assertIn("Version:Hard [1.1x]", folder_text)
            self.assertIn("AudioFilename: song [1.1x].wav", folder_text)
            loose_text = Path(expected[1]).read_text(encoding="utf-8")
            self.assertIn("Title:Loose\n", loose_text)
            self.assertIn("Version:Hard\n", loose_text)
            self.assertIn("AudioFilename: song [1.1x].wav", loose_text)
            with zipfile.ZipFile(expected[2]) as packed:
                self.assertEqual(set(packed.namelist()), {"hard [1.1x].osu", "song [1.1x].wav"})
                self.assertIn("Title:T [1.1x]", packed.read("hard [1.1x].osu").decode("utf-8"))
            progress = [value["fraction"] for kind, value in events if kind == "progress"]
            self.assertEqual(progress, sorted(progress))
            self.assertEqual(progress[-1], 1.0)

    def test_batch_destination_cannot_be_inside_any_selected_source(self):
        for location in ("workdir", "custom"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = _song(root / "First")
                second = _song(root / "Second")
                unsafe = second / "output"
                raw = {"output_location": "workdir"}
                if location == "custom":
                    raw.update(separate_folder=True, output_folder=str(unsafe))
                with self.assertRaises(ParamValidationError) as error:
                    self._prepare(unsafe, [first, second], **raw)
                self.assertIn("原文件夹之外", str(error.exception))
                self.assertFalse(unsafe.exists())

    def test_single_file_collection_cannot_copy_itself_into_its_source_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _song(root / "Song")
            controller = SpeedChangeController()
            context = ToolContext(str(root), str(root), {})
            with self.assertRaises(ParamValidationError) as error:
                controller.prepare({
                    "input_paths": str(source / "hard.osu"), "speed": 1.1,
                    "separate_folder": True, "output_folder": str(source / "out"),
                }, context)
            self.assertIn("原文件夹之外", str(error.exception))
            self.assertFalse((source / "out").exists())

    def test_late_batch_collision_preserves_existing_contents_and_releases_empty_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _song(root / "First")
            second = _song(root / "Second")
            controller, prepared = self._prepare(root / "out", [first, second], output_location="workdir")
            first_out, second_out = map(Path, prepared.params["planned_outputs"])
            second_out.mkdir(parents=True)
            (second_out / "keep.txt").write_bytes(b"keep")
            before = [_original_tree(first), _original_tree(second)]
            with patch("osu_toolbox.speed_change.resample_audio") as audio:
                with self.assertRaises(FileExistsError):
                    controller.run(prepared, lambda *_: None)
            audio.assert_not_called()
            self.assertFalse(first_out.exists())
            self.assertEqual(_original_tree(second_out), {"keep.txt": b"keep"})
            self.assertEqual([_original_tree(first), _original_tree(second)], before)

    def test_one_library_uses_a_shared_pool_for_all_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Library"
            for index in range(4):
                _song(library / f"Song {index}", title=str(index))
            controller, prepared = self._prepare(root, [library])
            barrier = threading.Barrier(2)
            lock = threading.Lock()
            active = {"current": 0, "peak": 0}
            def convert(source, target, *_args, **_kwargs):
                with lock:
                    active["current"] += 1
                    active["peak"] = max(active["peak"], active["current"])
                try:
                    barrier.wait(timeout=3)
                    _fake_resample(source, target)
                finally:
                    with lock:
                        active["current"] -= 1
            with patch("osu_toolbox.speed_change.os.cpu_count", return_value=4):
                with patch("osu_toolbox.speed_change.resample_audio", side_effect=convert) as audio:
                    result = controller.run(prepared, lambda *_: None)
            self.assertEqual(result.data["errors"], [])
            self.assertEqual(audio.call_count, 4)
            self.assertGreaterEqual(active["peak"], 2)
            self.assertEqual(result.data["paths"], [str(root / "Library 1.1x")])

    @unittest.skipUnless(fixtures.FFMPEG, "FFmpeg unavailable")
    def test_real_gui_and_menu_audio_and_timeline_are_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = _song(root / "template")
            subprocess.run([
                fixtures.FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ar", "44100", "-ac", "1", str(template / "song.wav"),
            ], capture_output=True, check=True)
            before = _original_tree(template)
            outputs = []
            for invocation in ("gui", "shell"):
                source = root / invocation / "Song"
                shutil.copytree(template, source)
                controller, prepared = self._prepare(root / invocation, [source], invocation=invocation)
                result = controller.run(prepared, lambda *_: None)
                self.assertEqual(result.data["errors"], [])
                destination = Path(result.data["paths"][0])
                self.assertEqual(_original_tree(source), before)
                outputs.append(destination)
            self.assertEqual(_result_tree(outputs[0]), _result_tree(outputs[1]))
            def duration(path):
                decoded = subprocess.run([
                    fixtures.FFMPEG, "-hide_banner", "-nostdin", "-i", str(path),
                    "-f", "s16le", "-ac", "1", "-ar", "44100", "pipe:1",
                ], capture_output=True, check=True, timeout=20)
                self.assertTrue(decoded.stdout)
                return len(decoded.stdout) / (2 * 44100)
            self.assertAlmostEqual(duration(template / "song.wav") / duration(outputs[0] / "song 1.1x.wav"),
                                   1.1, delta=0.015)
            text = (outputs[0] / "hard 1.1x.osu").read_text(encoding="utf-8")
            self.assertIn("64,192,909,", text)
            self.assertIn("AudioFilename: song 1.1x.wav", text)


if __name__ == "__main__":
    unittest.main()
