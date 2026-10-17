"""SM precision diagnostics checked against final rows and timing tags."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import wave

from osu_toolbox.chart_convert import (
    ChartDocument, ChartNoteData, ChartSoundEvent, convert_paths, parse_chart,
    plan_conversion, preflight_conversion, write_chart,
)
from test_chart_convert_sm_holds import scan_sm, sm_time


class SmPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("song.wav", "hit.wav", "ghost.wav"):
            with wave.open(str(self.root / name), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\x00\x00" * 16000)

    def document(self, notes=(), events=()):
        return ChartDocument(key_count=4, audio_filename="song.wav",
                             timings=[(0, 500, 4)], notes=list(notes),
                             sound_events=list(events))

    def plan(self, document):
        original = deepcopy(document)
        plan = plan_conversion(document, ".sm")
        again = plan_conversion(document, ".sm")
        self.assertEqual(document, original)
        self.assertEqual((plan.output_text, plan.diagnostics),
                         (again.output_text, again.diagnostics))
        destination = self.root / "plan.sm"
        write_chart(document, destination, ".sm")
        self.assertEqual(destination.read_text(encoding="utf-8"), plan.output_text)
        scan_sm(plan.output_text)
        return plan

    def finding(self, plan, code, count):
        findings = [item for item in plan.diagnostics if item.code == code]
        self.assertEqual(len(findings), 1, plan.diagnostics)
        self.assertEqual(findings[0].count, count)
        return findings[0]

    def test_public_mc_automatic_sound_precision_reaches_preflight_and_execution(self):
        for bpm in (120, 10):
            with self.subTest(bpm=bpm):
                source = self.root / f"auto-{bpm}.mc"
                data = {
                    "meta": {"mode": 0, "mode_ext": {"column": 4}},
                    "time": [{"beat": [0, 0, 1], "bpm": bpm}],
                    "note": [
                        {"beat": [0, 0, 1], "type": 1, "sound": "song.wav", "offset": 0},
                        {"beat": [0, 1, 7], "sound": "hit.wav"},
                        {"beat": [0, 1, 12], "column": 2, "sound": "hit.wav"},
                    ],
                }
                source.write_text(json.dumps(data), encoding="utf-8")
                document = parse_chart(source)[0]
                plan = self.plan(document)
                finding = self.finding(plan, "sm-sound-precision", 1)
                self.assertEqual(scan_sm(plan.output_text)[1],
                                 [(4, 2, "1", "hit.wav"), (7, 0, "K", "hit.wav")])
                error = abs(sm_time(plan.output_text, 7) - document.sound_events[0].time)
                self.assertIn(f"触发时间最大误差 {error:.3f} ms", finding.message)
                self.assertEqual(finding.severity, "warning")
                self.assertEqual(plan.strategy_for("auto_keysound")[0], "approximate")
                self.assertEqual(plan.strategy_for("keysound")[0], "full")
                self.assertEqual(plan.strategy_for("tap")[0], "full")
                target = self.root / f"auto-{bpm}-out"
                expected, warnings = preflight_conversion([source], ".sm", target)
                message = f"{source.name}：{finding.message}"
                self.assertIn(message, warnings)
                events = []
                outputs = convert_paths(
                    [source], ".sm", target, expected_outputs=expected,
                    emit_event=lambda kind, payload: events.append((kind, payload)),
                )
                self.assertEqual(sum(payload == message for kind, payload in events), 1)
                parsed = parse_chart(outputs[0])[0]
                self.assertEqual(parsed.sound_events[0].time, round(sm_time(plan.output_text, 7)))

    def test_special_points_report_precision_for_their_own_type(self):
        for kind, marker in (("mine", "M"), ("lift", "L"), ("fake", "F")):
            with self.subTest(kind=kind):
                plan = self.plan(self.document([
                    ChartNoteData(100, 0, note_type=kind),
                    ChartNoteData(500, 1),
                ]))
                self.assertEqual(scan_sm(plan.output_text)[1],
                                 [(10, 0, marker, ""), (48, 1, "1", "")])
                error = abs(sm_time(plan.output_text, 10) - 100)
                finding = self.finding(plan, "sm-grid-precision", 1)
                self.assertIn(f"头最大误差 {error:.3f} ms", finding.message)
                self.assertEqual(plan.strategy_for(kind)[0], "approximate")
                self.assertEqual(plan.strategy_for("tap")[0], "full")

    def test_automatic_note_and_event_share_sound_precision_without_double_counting(self):
        plan = self.plan(self.document(
            [ChartNoteData(100, 0, note_type="auto_keysound", sound="hit.wav"),
             ChartNoteData(500, 2)],
            [ChartSoundEvent(150, "hit.wav", column=1)],
        ))
        finding = self.finding(plan, "sm-sound-precision", 2)
        errors = (abs(sm_time(plan.output_text, 10) - 100),
                  abs(sm_time(plan.output_text, 14) - 150))
        self.assertIn(f"触发时间最大误差 {max(errors):.3f} ms", finding.message)
        self.assertNotIn("sm-grid-precision", {item.code for item in plan.diagnostics})
        self.assertEqual(plan.strategies["auto_keysound"].count, 2)
        self.assertEqual(plan.strategy_for("auto_keysound")[0], "approximate")
        self.assertEqual(plan.strategy_for("tap")[0], "full")

    def test_roll_precision_updates_only_affected_capabilities(self):
        plan = self.plan(self.document([
            ChartNoteData(100, 0, 101, note_type="roll", sound="hit.wav"),
            ChartNoteData(500, 1, 1000),
            ChartNoteData(1500, 2),
        ]))
        self.finding(plan, "sm-hold-extended", 1)
        self.finding(plan, "sm-grid-precision", 1)
        self.assertEqual(plan.strategy_for("roll")[0], "approximate")
        self.assertEqual(plan.strategy_for("keysound")[0], "approximate")
        self.assertEqual(plan.strategy_for("hold")[0], "full")
        self.assertEqual(plan.strategy_for("tap")[0], "full")
        self.assertEqual(scan_sm(plan.output_text)[0],
                         [(10, 11, 0, "roll", "hit.wav"), (48, 96, 1, "hold", "")])

    def test_precision_does_not_replace_whole_note_or_sound_downgrades(self):
        plan = self.plan(self.document(
            [ChartNoteData(100, 0, 101, note_type="roll",
                           sound="hit.wav", sound_volume=50),
             ChartNoteData(100, 0, 500, note_type="roll", sound="ghost.wav")],
            [ChartSoundEvent(100, "hit.wav", column=1),
             ChartSoundEvent(100, "ghost.wav", column=0)],
        ))
        self.finding(plan, "sm-hold-conflict", 1)
        self.finding(plan, "sm-grid-precision", 1)
        self.finding(plan, "sm-sound-precision", 1)
        for feature in ("roll", "keysound", "auto_keysound"):
            self.assertEqual(plan.strategy_for(feature)[0], "downgrade")

    def test_discarded_points_do_not_contribute_precision_or_affect_kept_types(self):
        exact = 10 * 500 / 48
        plan = self.plan(self.document(
            [ChartNoteData(0, 0, 1000), ChartNoteData(exact, 1),
             ChartNoteData(100, 1, note_type="fake")],
            [ChartSoundEvent(1, "ghost.wav", column=0)],
        ))
        self.finding(plan, "sm-grid-conflict", 2)
        self.assertFalse({"sm-grid-precision", "sm-sound-precision"} &
                         {item.code for item in plan.diagnostics})
        self.assertEqual(plan.strategy_for("hold")[0], "full")
        self.assertEqual(plan.strategy_for("tap")[0], "full")
        self.assertEqual(plan.strategy_for("auto_keysound")[0], "downgrade")
        self.assertNotIn("ghost.wav", plan.output_text)

    def test_exact_and_sub_millisecond_roles_have_no_precision_noise(self):
        plan = self.plan(self.document(
            [ChartNoteData(0, 0, 500, note_type="roll"),
             ChartNoteData(500.4, 1, note_type="mine"),
             ChartNoteData(1000.4, 2, note_type="lift"),
             ChartNoteData(1500.4, 3, note_type="fake"),
             ChartNoteData(2000.4, 1, note_type="auto_keysound", sound="hit.wav")],
            [ChartSoundEvent(2500.4, "hit.wav", column=2)],
        ))
        self.assertEqual(plan.diagnostics, [])
        for feature in ("roll", "mine", "lift", "fake", "auto_keysound", "keysound"):
            self.assertEqual(plan.strategy_for(feature)[0], "full")

    def test_all_role_errors_use_final_offset_bpm_stop_and_delay(self):
        controls = ("#OFFSET:0.125;\n#BPMS:0=120,4=240;\n"
                    "#STOPS:4=0.100;\n#DELAYS:4=0.020;\n")
        sound_time = sm_time(controls, 193) + 1
        note_time = sm_time(controls, 200) + 1
        document = ChartDocument(
            key_count=4, audio_filename="song.wav",
            timings=[(sm_time(controls, 0), 500, 4),
                     (sm_time(controls, 192), 250, 4)],
            timing_beats=[0, 4], beat_zero_ms=-125,
            timing_events=[(4, 100, "stop"), (4, 20, "delay")],
            notes=[ChartNoteData(note_time, 1, note_type="mine")],
            sound_events=[ChartSoundEvent(sound_time, "hit.wav", column=0)],
        )
        plan = self.plan(document)
        self.assertEqual(scan_sm(plan.output_text)[1],
                         [(193, 0, "K", "hit.wav"), (200, 1, "M", "")])
        sound_error = abs(sm_time(plan.output_text, 193) - sound_time)
        note_error = abs(sm_time(plan.output_text, 200) - note_time)
        self.assertIn(f"触发时间最大误差 {sound_error:.3f} ms",
                      self.finding(plan, "sm-sound-precision", 1).message)
        self.assertIn(f"头最大误差 {note_error:.3f} ms",
                      self.finding(plan, "sm-grid-precision", 1).message)
        self.assertEqual(plan.strategy_for("auto_keysound")[0], "approximate")
        self.assertEqual(plan.strategy_for("mine")[0], "approximate")


if __name__ == "__main__":
    unittest.main()
