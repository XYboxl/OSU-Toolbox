import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import osu_toolbox as core


class SkinIniParsingTests(unittest.TestCase):
    def test_directory_without_skin_ini_uses_latest_default_mania(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = core.load_skin_config(temp_dir, 4)

        self.assertIsNotNone(config)
        self.assertEqual(config.version_text, "latest")
        self.assertEqual(config.version, 2.7)
        self.assertEqual(config.key_count, 4)
        self.assertIsNone(config.skin_ini_path)
        self.assertFalse(config.skin_mania_config_found)

    def test_relative_skin_path_prefers_current_working_directory(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            skin = Path(temp_dir) / "skins" / "custom"
            skin.mkdir(parents=True)
            (skin / "skin.ini").write_text(
                "[Mania]\nKeys: 4\nColumnStart: 123\n",
                encoding="utf-8",
            )
            try:
                os.chdir(temp_dir)
                config = core.load_skin_config("./skins/custom", 4)
            finally:
                os.chdir(previous)

        self.assertEqual(config.root, str(skin.resolve()))
        self.assertEqual(config.column_start, 123.0)
        self.assertTrue(config.skin_mania_config_found)
        self.assertEqual(config.skin_mania_keys, (4,))

    def test_latest_version_token_is_case_sensitive(self):
        latest = core.SkinConfig("", {"Version": "latest"}, 4)
        self.assertEqual(latest.version, 2.7)
        with self.assertRaisesRegex(core.SkinConfigError, "invalid skin Version"):
            core.SkinConfig("", {"Version": "LATEST"}, 4)

    def test_stable_single_parser_rejects_python_only_spellings(self):
        sentinel = 123.0
        for value in ("1_0", "inf", "+inf", "infinity", "nan"):
            with self.subTest(value=value):
                self.assertEqual(core._parse_float(value, sentinel), sentinel)

    def test_stable_single_parser_accepts_float_syntax_and_rounds_to_float32(self):
        self.assertEqual(core._parse_float("  +2.5e2  ", -1.0), 250.0)
        self.assertEqual(core._parse_float(".5", -1.0), 0.5)
        self.assertEqual(core._parse_float("1.", -1.0), 1.0)
        self.assertEqual(core._parse_float("1,234.5", -1.0), 1234.5)
        self.assertEqual(core._parse_float("16777217", -1.0), 16777216.0)
        self.assertTrue(math.isinf(core._parse_float("Infinity", -1.0)))
        self.assertTrue(math.isnan(core._parse_float("NaN", -1.0)))

    def test_stable_single_and_double_overflow_have_different_fallbacks(self):
        self.assertEqual(core._parse_float("3.5e38", 9.0), 9.0)
        self.assertEqual(core.SkinConfig("", {"Version": "3.5e38"}, 4).version, 3.5e38)
        for value in ("1e309", "1_0", ""):
            with self.subTest(value=value), self.assertRaisesRegex(
                core.SkinConfigError,
                "invalid skin Version",
            ):
                core.SkinConfig("", {"Version": value}, 4)

    def test_invalid_float_list_elements_become_zero(self):
        self.assertEqual(
            core._split_ints("1e2,1_0,.5,inf", 9.0, 4),
            [100.0, 0.0, 0.5, 0.0],
        )

    def test_nonstandard_scorebar_resource_keys_are_ignored(self):
        config = core.SkinConfig(
            "",
            {
                "ScoreBarBg": "custom-bg",
                "ScoreBarColour": "custom-colour",
                "ScoreBarMarker": "custom-marker",
                "ScoreBarKi": "custom-ki",
            },
            4,
        )
        self.assertEqual(config.scorebar_bg, "scorebar-bg")
        self.assertEqual(config.scorebar_colour, "scorebar-colour")
        self.assertEqual(config.scorebar_marker, "scorebar-marker")
        self.assertEqual(config.scorebar_ki, "scorebar-ki")

    def test_missing_matching_mania_keeps_general_and_fonts_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "AnimationFramerate: 12",
                    "[Colours]",
                    "ColourKeyWarning: 12,34,56",
                    "[Fonts]",
                    "ScorePrefix: custom-score",
                    "[Mania]",
                    "Keys: 7",
                    "ColumnWidth: 70,70,70,70,70,70,70",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertIsNotNone(config)
        self.assertEqual(config.version, 2.7)
        self.assertEqual(config.animation_framerate, 12.0)
        self.assertEqual(config.colour_key_warning, (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(config.score_prefix, "custom-score")
        self.assertEqual(config.column_width, [30.0] * 4)

    def test_bom_and_inline_comments_are_parsed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General] // global settings",
                    "Version: 2.7 // current stable version",
                    "AnimationFramerate: 12 // frames per second",
                    "[Mania] // four keys",
                    "Keys: +04 // signed and zero-padded",
                    "ColumnWidth: 40,40,40,40 // lane widths",
                    "HitPosition: 300 // playfield coordinate",
                ]),
                encoding="utf-8-sig",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.version, 2.7)
        self.assertEqual(config.animation_framerate, 12)
        self.assertEqual(config.column_width, [40.0] * 4)
        self.assertEqual(config.hit_position, 300)

    def test_section_and_parameter_names_are_case_sensitive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[general]",
                    "Version: 1.0",
                    "[mania]",
                    "Keys: 4",
                    "ColumnWidth: 90,90,90,90",
                    "[General]",
                    "version: 2.7",
                    "Version: 1.5",
                    "[Mania]",
                    "keys: 7",
                    "Keys: 4",
                    "ColumnWidth: 41,41,41,41",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.version, 1.5)
        self.assertEqual(config.column_width, [41.0] * 4)

    def test_indented_section_header_is_not_recognised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    " [Mania]",
                    "Keys: 4",
                    "ColumnWidth: 90,90,90,90",
                    "[Mania]",
                    "Keys: 4",
                    "ColumnWidth: 41,41,41,41",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.column_width, [41.0] * 4)

    def test_section_name_ends_at_first_closing_bracket(self):
        for header in ("[Mania] ignored [General]", "[Mania"):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / "skin.ini").write_text(
                    "\n".join([
                        header,
                        "Keys: 4",
                        "ColumnWidth: 43,43,43,43",
                    ]),
                    encoding="utf-8",
                )
                config = core.load_skin_config(str(root), 4)

            self.assertEqual(config.column_width, [43.0] * 4)

    def test_parameter_value_uses_only_second_colon_segment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7: ignored",
                    "[Fonts]",
                    "ScorePrefix: first:ignored",
                    "[Mania]",
                    "Keys: 4",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.version, 2.7)
        self.assertEqual(config.score_prefix, "first")

    def test_key_count_uses_stable_int_syntax(self):
        for keys in ("04", "+4"):
            with self.subTest(keys=keys), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / "skin.ini").write_text(
                    "\n".join([
                        "[Mania]",
                        f"Keys: {keys}",
                        "ColumnWidth: 41,41,41,41",
                    ]),
                    encoding="utf-8",
                )
                config = core.load_skin_config(str(root), 4)
                self.assertEqual(config.column_width, [41.0] * 4)

    def test_first_duplicate_parameter_and_keys_win(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "Version: 1.0",
                    "[Mania]",
                    "Keys: 4",
                    "Keys: 7",
                    "ColumnWidth: 42,42,42,42",
                    "ColumnWidth: 90,90,90,90",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.version, 2.7)
        self.assertEqual(config.column_width, [42.0] * 4)

    def test_fields_are_read_only_from_their_stable_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "AnimationFramerate: 12",
                    "ScorePrefix: wrong-general",
                    "[Colours]",
                    "ColourKeyWarning: 1,2,3",
                    "[Fonts]",
                    "ScorePrefix: custom-score",
                    "ScoreOverlap: 5",
                    "AnimationFramerate: 99",
                    "HitPosition: 250",
                    "[Mania]",
                    "Keys: 4",
                    "Version: 1.0",
                    "ScorePrefix: wrong-mania",
                    "HitPosition: 300",
                    "ColourKeyWarning: 12,34,56",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.version, 2.7)
        self.assertEqual(config.animation_framerate, 12)
        self.assertEqual(config.score_prefix, "custom-score")
        self.assertEqual(config.score_overlap, 5)
        self.assertEqual(config.hit_position, 300)
        self.assertEqual(
            config.colour_key_warning,
            (12 / 255, 34 / 255, 56 / 255, 1.0),
        )

    def test_stable_defaults_for_optional_mania_fields(self):
        config = core.SkinConfig("", {"Version": "2.7"}, 4)
        self.assertEqual(config.column_right, 19.0)
        self.assertEqual(config.score_position, 325.0)
        self.assertEqual(config.barline_height, 1.2)
        self.assertEqual(config.combo_burst_style, 1)
        self.assertTrue(config.separate_score)
        self.assertFalse(config.has_column_right)
        self.assertFalse(config.has_split_stages)
        self.assertEqual(
            config.colour_light,
            [(1.0, 1.0, 1.0, 1.0)] * 4,
        )
        self.assertEqual(
            config.colour_hold,
            (1.0, 199.0 / 255.0, 51.0 / 255.0, 1.0),
        )

    def test_colour_light_override_does_not_change_other_column_defaults(self):
        config = core.SkinConfig(
            "", {"ColourLight1": "55,255,255,128"}, 3,
        )
        self.assertEqual(
            config.colour_light,
            [
                (55 / 255, 1.0, 1.0, 128 / 255),
                (1.0, 1.0, 1.0, 1.0),
                (1.0, 1.0, 1.0, 1.0),
            ],
        )

    def test_stable_default_column_resource_classes(self):
        odd = core.SkinConfig("", {"Version": "2.7"}, 5)
        even = core.SkinConfig("", {"Version": "2.7"}, 6)
        self.assertEqual(
            odd.fallback_column_index, ["1", "2", "S", "2", "1"],
        )
        self.assertEqual(
            even.fallback_column_index, ["1", "2", "1", "1", "2", "1"],
        )

    def test_beatmap_special_style_selects_outer_or_inner_columns(self):
        outer = core.SkinConfig(
            "", {"Version": "2.7", "SpecialStyle": "Left"}, 6, True,
        )
        inner = core.SkinConfig(
            "", {"Version": "2.7", "SpecialStyle": "Right"}, 6, True,
        )
        ignored = core.SkinConfig(
            "", {"Version": "2.7", "SpecialStyle": "Left"}, 6, False,
        )
        self.assertEqual(
            outer.fallback_column_index, ["S", "1", "2", "1", "2", "1"],
        )
        self.assertEqual(
            inner.fallback_column_index, ["1", "2", "1", "2", "1", "S"],
        )
        self.assertNotIn("S", ignored.fallback_column_index)

    def test_split_stage_reverses_special_style_on_right_stage(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "SpecialStyle": "Left",
                "SplitStages": "1",
            },
            12,
            True,
        )
        self.assertEqual(
            config.fallback_column_index,
            [
                "S", "1", "2", "1", "2", "1",
                "1", "2", "1", "2", "1", "S",
            ],
        )

    def test_pre_25_note_flip_options_are_ignored(self):
        params = {
            "UpsideDown": "1",
            "KeyFlipWhenUpsideDown": "1",
            "NoteFlipWhenUpsideDown": "1",
        }
        old = core.SkinConfig("", {"Version": "2.4", **params}, 1)
        new = core.SkinConfig("", {"Version": "2.5", **params}, 1)
        self.assertEqual(old.key_flip, [True])
        self.assertEqual(old.note_flip, [False])
        self.assertEqual(old.note_head_flip, [False])
        self.assertEqual(old.note_body_flip, [False])
        self.assertEqual(old.note_tail_flip, [False])
        self.assertEqual(new.note_flip, [True])
        self.assertEqual(new.note_head_flip, [True])
        self.assertEqual(new.note_body_flip, [True])
        self.assertEqual(new.note_tail_flip, [True])

    def test_global_note_piece_flip_settings_allow_column_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "[Mania]",
                    "Keys: 3",
                    "NoteFlipWhenUpsideDown: 0",
                    "NoteFlipWhenUpsideDownH: 1",
                    "NoteFlipWhenUpsideDownL: 0",
                    "NoteFlipWhenUpsideDownT: 1",
                    "NoteFlipWhenUpsideDown0H: 0",
                    "NoteFlipWhenUpsideDown1L: 1",
                    "NoteFlipWhenUpsideDown2T: 0",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 3)

        self.assertEqual(config.note_flip, [False, False, False])
        self.assertEqual(config.note_head_flip, [False, True, True])
        self.assertEqual(config.note_body_flip, [False, True, False])
        self.assertEqual(config.note_tail_flip, [True, True, False])

    def test_explicit_empty_font_prefixes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[Fonts]",
                    "ScorePrefix:",
                    "ComboPrefix:",
                    "[Mania]",
                    "Keys: 4",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)

        self.assertEqual(config.score_prefix, "")
        self.assertEqual(config.combo_prefix, "")

    def test_stable_booleans_accept_words_and_any_int32(self):
        config = core.SkinConfig(
            "",
            {
                "KeysUnderNotes": " TrUe ",
                "JudgementLine": "FALSE",
                "UpsideDown": "-2",
                "SeparateScore": "0",
                "SplitStages": "not-a-bool",
            },
            4,
        )
        self.assertTrue(config.keys_under_notes)
        self.assertFalse(config.judgement_line)
        self.assertTrue(config.upside_down)
        self.assertFalse(config.separate_score)
        self.assertFalse(config.split_stages)
        self.assertFalse(config.has_split_stages)

    def test_stable_int_fields_reject_fractional_text(self):
        config = core.SkinConfig(
            "",
            {
                "AnimationFramerate": "12.5",
                "HitPosition": "300.5",
                "LightPosition": "301.5",
                "ComboPosition": "302.5",
                "ScorePosition": "303.5",
                "LightFramePerSecond": "24.5",
                "ScoreOverlap": "4.5",
                "ComboOverlap": "5.5",
            },
            4,
        )
        self.assertEqual(config.animation_framerate, -1)
        self.assertEqual(config.hit_position, 402.0)
        self.assertEqual(config.light_position, 413.0)
        self.assertEqual(config.combo_position, 111.0)
        self.assertEqual(config.score_position, 325.0)
        self.assertEqual(config.light_frame_per_second, 60.0)
        self.assertEqual(config.score_overlap, 0)
        self.assertEqual(config.combo_overlap, 0)

        valid = core.SkinConfig(
            "",
            {"HitPosition": "+0300", "ScoreOverlap": "-02"},
            4,
        )
        self.assertEqual(valid.hit_position, 300)
        self.assertEqual(valid.score_overlap, -2)

    def test_stable_rgba_parses_int32_then_converts_to_bytes(self):
        valid = core.SkinConfig("", {"Colour1": "1,2,3,4"}, 1)
        self.assertEqual(valid.colours[0], (1 / 255, 2 / 255, 3 / 255, 4 / 255))

        wrapped = core.SkinConfig("", {"Colour1": "-1,256,257,-2"}, 1)
        self.assertEqual(
            wrapped.colours[0],
            (1.0, 0.0, 1 / 255, 254 / 255),
        )

        for value in (
            "1,2",
            "1,2,3,4,5",
            "1.0,2,3",
            "1,two,3",
            "2147483648,2,3",
        ):
            with self.subTest(value=value):
                config = core.SkinConfig("", {"Colour1": value}, 1)
                self.assertEqual(config.colours[0], (0.0, 0.0, 0.0, 1.0))

    def test_stable_mania_enums_accept_names_and_numeric_values(self):
        named = core.SkinConfig(
            "",
            {"SpecialStyle": "rIgHt", "ComboBurstStyle": "BoTh"},
            6,
        )
        numeric = core.SkinConfig(
            "",
            {"SpecialStyle": "1", "ComboBurstStyle": "0"},
            6,
        )
        self.assertEqual(named.special_style, 2)
        self.assertEqual(named.combo_burst_style, 2)
        self.assertEqual(numeric.special_style, 1)
        self.assertEqual(numeric.combo_burst_style, 0)

    def test_stable_mania_enums_preserve_undefined_integer_values(self):
        config = core.SkinConfig(
            "",
            {"SpecialStyle": "3", "ComboBurstStyle": "-1"},
            6,
        )
        self.assertEqual(config.special_style, 3)
        self.assertEqual(config.combo_burst_style, -1)

    def test_invalid_stable_mania_enums_use_defaults(self):
        for value in ("invalid", "1.5", "1e0", "1_0", "", "2147483648"):
            with self.subTest(value=value):
                config = core.SkinConfig(
                    "",
                    {"SpecialStyle": value, "ComboBurstStyle": value},
                    6,
                )
                self.assertEqual(config.special_style, 0)
                self.assertEqual(config.combo_burst_style, 1)

    def test_stable_post_parse_clamps_stage_geometry_values(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "ColumnWidth": "300,4,20,100.5",
                "ColumnSpacing": "-200,-4,12",
                "ColumnLineWidth": "0.5,0,1.99,2,3",
                "StageSeparation": "1",
            },
            4,
        )
        self.assertEqual(config.column_width, [100.0, 5.0, 20.0, 100.0])
        self.assertEqual(config.column_spacing, [-5.0, -4.0, 12.0])
        self.assertEqual(config.column_line_width, [2.0, 0.0, 2.0, 2.0, 3.0])
        self.assertEqual(config.stage_separation, 5.0)

    def test_stable_clamps_preserve_nan_and_bound_infinities(self):
        config = core.SkinConfig(
            "",
            {
                "Version": "2.7",
                "ColumnWidth": "NaN,Infinity,-Infinity,30",
                "ColumnSpacing": "Infinity,-Infinity,NaN",
                "StageSeparation": "NaN",
            },
            4,
        )
        self.assertTrue(math.isnan(config.column_width[0]))
        self.assertEqual(config.column_width[1:], [100.0, 5.0, 30.0])
        self.assertEqual(config.column_spacing[0], 3.4028234663852886e38)
        self.assertEqual(config.column_spacing[1], -5.0)
        self.assertTrue(math.isnan(config.column_spacing[2]))
        self.assertTrue(math.isnan(config.stage_separation))

        positive = core.SkinConfig("", {"StageSeparation": "Infinity"}, 4)
        negative = core.SkinConfig("", {"StageSeparation": "-Infinity"}, 4)
        self.assertTrue(math.isinf(positive.stage_separation))
        self.assertEqual(negative.stage_separation, 5.0)

    def test_note_height_nan_uses_column_width_but_infinity_is_explicit(self):
        fallback = core.SkinConfig(
            "",
            {"WidthForNoteHeightScale": "NaN"},
            4,
        )
        explicit = core.SkinConfig(
            "",
            {"WidthForNoteHeightScale": "Infinity"},
            4,
        )
        self.assertEqual(fallback.width_for_note_height_scale, 30.0)
        self.assertTrue(fallback.width_for_note_height_scale_uses_column_width)
        self.assertTrue(math.isinf(explicit.width_for_note_height_scale))
        self.assertFalse(explicit.width_for_note_height_scale_uses_column_width)

    def test_nonpositive_note_height_scale_uses_smallest_column_width(self):
        for value in ("0", "-1"):
            with self.subTest(value=value):
                config = core.SkinConfig(
                    "",
                    {
                        "ColumnWidth": "40,30,50,60",
                        "WidthForNoteHeightScale": value,
                    },
                    4,
                )
                self.assertEqual(config.width_for_note_height_scale, 30.0)

    def test_first_duplicate_key_count_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "skin.ini").write_text(
                "\n".join([
                    "[General]",
                    "Version: 2.7",
                    "[Mania]",
                    "Keys: 4",
                    "ColumnWidth: 40,40,40,40",
                    "[Mania]",
                    "Keys: 4",
                    "ColumnWidth: 90,90,90,90",
                ]),
                encoding="utf-8",
            )
            config = core.load_skin_config(str(root), 4)
            self.assertEqual(config.column_width, [40.0] * 4)

    def test_explicit_density_suffix_is_normalised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "image@2x.png").write_bytes(b"not decoded by this test")
            candidates = list(core.iter_skin_image_candidates(
                str(root), "image@2x.png",
            ))
            self.assertEqual(candidates, [(str(root / "image@2x.png"), True)])

    def test_density_suffix_matching_is_case_sensitive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hd = root / "image@2X@2x.png"
            sd = root / "image@2X.png"
            hd.write_bytes(b"high density")
            sd.write_bytes(b"ordinary explicit name")

            candidates = list(core.iter_skin_image_candidates(
                str(root),
                "image@2X.png",
            ))
            self.assertEqual(
                candidates,
                [(str(hd), True), (str(sd), False)],
            )

    def test_nested_skin_paths_are_case_insensitive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "Mania"
            nested.mkdir()
            (nested / "Note.PNG").write_bytes(b"not decoded by this test")
            candidates = list(core.iter_skin_image_candidates(
                str(root), r"mania\note",
            ))
            self.assertEqual(len(candidates), 1)
            self.assertTrue(os.path.samefile(candidates[0][0], nested / "Note.PNG"))
            self.assertFalse(candidates[0][1])

    def test_extensionless_reference_in_dotted_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "Mania.v2"
            nested.mkdir()
            hd = nested / "note@2x.png"
            sd = nested / "note.png"
            hd.write_bytes(b"hd")
            sd.write_bytes(b"sd")
            candidates = list(core.iter_skin_image_candidates(
                str(root), "Mania.v2/note",
            ))
            self.assertEqual(
                candidates,
                [(str(hd), True), (str(sd), False)],
            )

    def test_animation_reference_in_dotted_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "Mania.v2"
            nested.mkdir()
            (nested / "note-0@2x.png").write_bytes(b"frame zero")
            (nested / "note-1.png").write_bytes(b"frame one")
            frames = list(core.iter_skin_animation_candidates(
                str(root), "Mania.v2/note",
            ))
            self.assertEqual(
                [(Path(path).name, density) for path, density in frames],
                [("note-0@2x.png", True), ("note-1.png", False)],
            )

    def test_animation_density_is_resolved_per_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "anim-0@2x.png").write_bytes(b"frame zero")
            (root / "anim-1.png").write_bytes(b"frame one")
            (root / "anim-2@2x.png").write_bytes(b"frame two")
            frames = list(core.iter_skin_animation_candidates(
                str(root), "anim@2x.png",
            ))
            self.assertEqual(
                [(Path(path).name, density) for path, density in frames],
                [
                    ("anim-0@2x.png", True),
                    ("anim-1.png", False),
                    ("anim-2@2x.png", True),
                ],
            )

    def test_static_lookup_does_not_fall_back_to_animation_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "only-0.png").write_bytes(b"animation frame")
            self.assertEqual(
                list(core.iter_skin_image_candidates(str(root), "only")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
