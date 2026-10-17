import hashlib
import json
import tempfile
import unittest
from pathlib import Path


class ReplayCoreTests(unittest.TestCase):
    def test_mr_round_trip_uses_utf8_byte_lengths(self):
        from osu_toolbox.parsing.mr_file import MrEvent, MrReplay

        replay = MrReplay(
            md5="0123456789abcdef0123456789abcdef",
            chart_version="困难谱面",
            music="歌曲标题",
            artist="作曲者",
            events=[MrEvent(10, 1, 0), MrEvent(10, 2, 0)],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "roundtrip.mr"
            replay.write(path)
            loaded = MrReplay.read(path)
        self.assertEqual(loaded.chart_version, replay.chart_version)
        self.assertEqual(loaded.music, replay.music)
        self.assertEqual(loaded.artist, replay.artist)
        self.assertEqual(loaded.total, 3)
        self.assertEqual([event.as_dict() for event in loaded.events], [event.as_dict() for event in replay.events])

    def test_generate_osu_perfect_replay_and_scale(self):
        from osu_toolbox.parsing.mr_file import MrReplay, generate_perfect_replay

        osu = """osu file format v14
[General]
Mode: 3
[Metadata]
Title:Test
Artist:Artist
Version:Hard
[Difficulty]
CircleSize:4
[HitObjects]
64,192,1000,1,0,0:0:0:0:
192,192,1500,128,0,2000:0:0:0:
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            chart = Path(temp_dir) / "test.osu"
            output = Path(temp_dir) / "perfect.mr"
            chart.write_text(osu, encoding="utf-8")
            replay = generate_perfect_replay(chart, output)
            loaded = MrReplay.read(output)
            chart_md5 = hashlib.md5(chart.read_bytes()).hexdigest()
        self.assertEqual(loaded.md5, chart_md5)
        self.assertEqual([(e.time, e.event, e.column) for e in loaded.events], [(1000, 1, 0), (1000, 2, 0), (1500, 1, 1), (2000, 2, 1)])
        self.assertEqual(replay.max_combo, 2)
        self.assertEqual(loaded.total, 5)
        loaded.scale(2)
        self.assertEqual([event.time for event in loaded.events], [500, 500, 750, 1000])

    def test_perfect_generation_is_deterministic_without_timing_perturbation(self):
        from osu_toolbox.parsing.mr_file import generate_perfect_replay

        osu = """osu file format v14
[General]
Mode: 3
[Difficulty]
CircleSize:4
[HitObjects]
64,192,1000,1,0,0:0:0:0:
192,192,1500,128,0,2000:0:0:0:
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            chart = Path(temp_dir) / "deterministic.osu"
            chart.write_text(osu, encoding="utf-8")
            generated = [generate_perfect_replay(chart) for _ in range(5)]
        expected = [
            (1000, 1, 0),
            (1000, 2, 0),
            (1500, 1, 1),
            (2000, 2, 1),
        ]
        for replay in generated:
            self.assertEqual(
                [(event.time, event.event, event.column) for event in replay.events],
                expected,
            )

    def test_generate_mc_supports_two_part_beats_and_bpm_changes(self):
        from osu_toolbox.parsing.mr_file import generate_perfect_replay

        data = {
            "meta": {"version": "4K", "song": {"title": "曲", "artist": "人"}, "mode": 0, "mode_ext": {"column": 4}},
            "time": [{"beat": [0, 0, 4], "bpm": 120}, {"beat": [4, 0, 4], "bpm": 60}],
            "note": [{"beat": [1, 0], "column": 0}, {"beat": [4, 0, 4], "endbeat": [6, 0, 4], "column": 3}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            chart = Path(temp_dir) / "test.mc"
            chart.write_text(json.dumps(data), encoding="utf-8")
            replay = generate_perfect_replay(chart)
        self.assertEqual([(e.time, e.event, e.column) for e in replay.events], [(500, 1, 0), (500, 2, 0), (2000, 1, 3), (4000, 2, 3)])
        self.assertEqual(replay.music, "曲")

    def test_perfect_replay_rejects_non_key_mc_modes(self):
        from osu_toolbox.parsing.mr_file import generate_perfect_replay

        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "chart.mc"
            output = Path(directory) / "chart.mr"
            for mode in (None, 4, 5, True, "invalid"):
                with self.subTest(mode=mode):
                    chart.write_text(json.dumps({
                        "meta": {"mode": mode, "mode_ext": {"column": 4}},
                        "note": [{"beat": [0, 0, 1], "column": 0}],
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Key"):
                        generate_perfect_replay(chart, output)
                    self.assertFalse(output.exists())

    def test_mc_key_count_uses_the_declared_width_for_sparse_charts(self):
        from osu_toolbox.parsing.mr_file import load_chart

        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "chart.mc"
            chart.write_text(json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 7}},
                "note": [{"beat": [0, 0, 1], "column": 0}],
            }), encoding="utf-8")
            self.assertEqual(load_chart(chart).key_count, 7)

    def test_perfect_replay_rejects_non_mania_osu_charts(self):
        from osu_toolbox.parsing.mr_file import generate_perfect_replay

        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "chart.osu"
            output = Path(directory) / "chart.mr"
            chart.write_text(
                "osu file format v14\n[General]\nMode:0\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n64,192,1000,1,0,0:0:0:0:\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "osu!mania"):
                generate_perfect_replay(chart, output)
            self.assertFalse(output.exists())

    def test_metadata_md5_requires_chart_file_logic(self):
        from osu_toolbox.parsing.mr_file import MrReplay, edit_replay_metadata

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mr"
            chart = root / "map.mc"
            output = root / "edited.mr"
            chart.write_bytes(b"chart")
            MrReplay(events=[]).write(source)
            edited = edit_replay_metadata(source, output, chart_path=chart, music="New")
            self.assertEqual(edited.md5, hashlib.md5(b"chart").hexdigest())
            self.assertEqual(MrReplay.read(output).music, "New")
            with self.assertRaises(ValueError):
                edited.update_metadata(md5="not-an-md5")

    def test_total_is_derived_from_real_event_records(self):
        from osu_toolbox.parsing.mr_file import MrEvent, MrReplay

        replay = MrReplay(events=[MrEvent(0, 1, 0), MrEvent(0, 2, 0)])
        self.assertEqual(replay.total, 3)
        replay.CHART = [{"time": 10, "event": 1, "column": 2}]
        self.assertEqual(replay.total, 2)

    def test_editable_result_fields_include_mod_without_judge_reset_logic(self):
        from osu_toolbox.parsing.mr_file import MrReplay
        from osu_toolbox.tools.replay_edit import REPLAY_EDIT_PARAMS
        from osu_toolbox.tools.replay_generate import REPLAY_GENERATE_PARAMS

        required = {
            "score", "max_combo", "best", "cool", "good", "miss",
            "mod", "judge", "playing_time",
        }
        self.assertTrue(required <= {spec.key for spec in REPLAY_EDIT_PARAMS})
        self.assertTrue(required <= {spec.key for spec in REPLAY_GENERATE_PARAMS})
        replay = MrReplay(events=[])
        replay.score = 1234
        replay.mod = 16
        self.assertEqual((replay.score, replay.mod), (1234, 16))

    def test_controller_writes_all_editable_result_fields(self):
        from osu_toolbox.parsing.mr_file import MrReplay
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.replay_edit import ReplayEditController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mr"
            output = root / "edited.mr"
            MrReplay(events=[]).write(source)
            prepared = ReplayEditController().prepare({
                "replay_path": str(source),
                "output_file": str(output),
                "score": 1000,
                "max_combo": 3,
                "best": 3,
                "cool": 2,
                "good": 1,
                "miss": 0,
                "mod": [16],
                "judge": 2,
                "playing_time": 123,
            }, ToolContext(str(root)))
            ReplayEditController().run(prepared, lambda *_: None)
            edited = MrReplay.read(output)
        self.assertEqual((edited.score, edited.max_combo, edited.best), (1000, 3, 3))
        self.assertEqual((edited.cool, edited.good, edited.miss), (2, 1, 0))
        self.assertEqual((edited.mod, edited.judge, edited.playing_time), (16, 2, 123))

    def test_mod_choices_are_combined_into_a_bitmask(self):
        from osu_toolbox import MOD_AUTO, MOD_DEATH
        from osu_toolbox.parsing.mr_file import MrReplay
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.replay_edit import ReplayEditController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mr"
            output = root / "edited.mr"
            MrReplay(events=[]).write(source)
            prepared = ReplayEditController().prepare({
                "replay_path": str(source),
                "output_file": str(output),
                "mod": [MOD_AUTO, MOD_DEATH],
            }, ToolContext(str(root)))
            ReplayEditController().run(prepared, lambda *_: None)
            edited = MrReplay.read(output)
        self.assertEqual(edited.mod, MOD_AUTO | MOD_DEATH)

    def test_edit_controller_recomputes_md5_from_selected_chart(self):
        from osu_toolbox.parsing.mr_file import MrReplay
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.replay_edit import ReplayEditController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mr"
            chart = root / "map.mc"
            output = root / "edited.mr"
            MrReplay(events=[]).write(source)
            chart.write_bytes(b"selected chart")
            prepared = ReplayEditController().prepare({
                "replay_path": str(source),
                "chart_path": str(chart),
                "output_file": str(output),
            }, ToolContext(str(root)))
            ReplayEditController().run(prepared, lambda *_: None)
            edited = MrReplay.read(output)
        self.assertEqual(edited.md5, hashlib.md5(b"selected chart").hexdigest())

    def test_generate_defaults_to_auto_but_empty_selection_clears_mod(self):
        from osu_toolbox import MOD_AUTO
        from osu_toolbox.parsing.mr_file import MrReplay
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.replay_generate import (
            REPLAY_GENERATE_PARAMS,
            ReplayGenerateController,
        )

        mod_spec = next(spec for spec in REPLAY_GENERATE_PARAMS if spec.key == "mod")
        self.assertEqual(mod_spec.default, [MOD_AUTO])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chart = root / "same-name.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Metadata]\nTitle:T\nArtist:A\nVersion:V\n"
                "[Difficulty]\nCircleSize:4\n"
                "[HitObjects]\n64,192,1000,1,0,0:0:0:0:\n",
                encoding="utf-8",
            )
            controller = ReplayGenerateController()
            prepared = controller.prepare({
                "chart_path": str(chart),
                "mod": [],
            }, ToolContext(str(root)))
            self.assertEqual(prepared.params["output_file"], str(root / "same-name.mr"))
            controller.run(prepared, lambda *_: None)
            replay = MrReplay.read(root / "same-name.mr")
        self.assertEqual(replay.mod, 0)

    def test_main_and_lightweight_contexts_choose_different_output_directories(self):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.replay_generate import ReplayGenerateController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "charts"
            source_dir.mkdir()
            chart = source_dir / "map.osu"
            chart.write_text(
                "osu file format v14\n[General]\nMode:3\n"
                "[Difficulty]\nCircleSize:4\n[HitObjects]\n",
                encoding="utf-8",
            )
            controller = ReplayGenerateController()
            main = controller.prepare(
                {"chart_path": str(chart)},
                ToolContext(str(root), output_root=str(root)),
            )
            lightweight = controller.prepare(
                {"chart_path": str(chart)},
                ToolContext(str(root)),
            )
        self.assertEqual(main.params["output_file"], str(root / "map.mr"))
        self.assertEqual(
            lightweight.params["output_file"],
            str(source_dir / "map.mr"),
        )

    def test_edit_prefill_reads_all_fields_and_mod_choices(self):
        from osu_toolbox import MOD_AUTO, MOD_DEATH
        from osu_toolbox.parsing.mr_file import MrEvent, MrReplay
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.replay_edit import ReplayEditController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mr"
            MrReplay(
                chart_version="Hard",
                music="Title",
                artist="Artist",
                score=123,
                max_combo=9,
                best=8,
                cool=7,
                good=6,
                miss=5,
                mod=MOD_AUTO | MOD_DEATH,
                judge=2,
                playing_time=456,
                events=[MrEvent(100, 1, 2)],
            ).write(source)
            result = ReplayEditController().prefill(
                {"replay_path": str(source)},
                "replay_path",
                ToolContext(str(root)),
            )
        self.assertEqual(result.params["music"], "Title")
        self.assertEqual(result.params["mod"], [MOD_AUTO, MOD_DEATH])
        self.assertEqual(json.loads(result.params["events_json"]), [
            {"time": 100, "event": 1, "column": 2},
        ])
        self.assertEqual(result.params["output_file"], str(root / "source-edited.mr"))


class ReplayToolTests(unittest.TestCase):
    def test_registry_contains_split_replay_tools(self):
        from osu_toolbox.tools.registry import build_default_registry

        registry = build_default_registry()
        self.assertEqual(
            [spec.id for spec in registry.list_specs()],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change"],
        )
        generate = registry.get("replay-generate")
        edit = registry.get("replay-edit")
        self.assertIn("chart_path", {spec.key for spec in generate.spec.params})
        self.assertNotIn("replay_path", {spec.key for spec in generate.spec.params})
        self.assertIn("replay_path", {spec.key for spec in edit.spec.params})
        self.assertIn("chart_path", {spec.key for spec in edit.spec.params})
        for definition in (generate, edit):
            self.assertEqual(definition.spec.result_kind, "file")
            mod_spec = next(spec for spec in definition.spec.params if spec.key == "mod")
            self.assertTrue(mod_spec.multiple)
            self.assertEqual(
                [choice.label for choice in mod_spec.choices],
                ["Auto", "Luck", "Flip", "Const", "Dash", "Rush", "Hide", "Origin", "Slow", "Death"],
            )
            visible = [group.label for group in definition.spec.groups]
            visible.extend((definition.spec.title, definition.spec.description,
                            definition.spec.category, definition.spec.action_label))
            self.assertTrue(all(any(ord(char) > 127 for char in text) for text in visible))
            self.assertTrue(all(
                any(ord(char) > 127 for char in spec.help)
                for spec in definition.spec.params
            ))

    def test_judge_is_single_choice_from_a_through_e(self):
        from osu_toolbox.tools.registry import build_default_registry

        registry = build_default_registry()
        for tool_id in ("replay-generate", "replay-edit"):
            definition = registry.get(tool_id)
            judge = next(spec for spec in definition.spec.params if spec.key == "judge")
            self.assertEqual(judge.kind, "choice")
            self.assertFalse(judge.multiple)
            self.assertEqual(
                [(choice.label, choice.value) for choice in judge.choices],
                [("A 判", 0), ("B 判", 1), ("C 判", 2), ("D 判", 3), ("E 判", 4)],
            )


if __name__ == "__main__":
    unittest.main()
