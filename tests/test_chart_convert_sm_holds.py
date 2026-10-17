"""SM output hold integrity checked from serialized rows and timing tags."""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import zipfile

from osu_toolbox.chart_convert import (
    ChartDocument, ChartNoteData, ChartSoundEvent, convert_paths, parse_chart,
    plan_conversion, preflight_conversion, write_chart,
)


def sm_tag(text, name):
    match = re.search(r'#' + name + r':([^;]*);', text)
    return match.group(1) if match else ''


def sm_time(text, row):
    """Integrate the final tags using exact decimal fractions, independently."""
    target = Fraction(row, 48)
    points = sorted(tuple(Fraction(value) for value in entry.split('='))
                    for entry in sm_tag(text, 'BPMS').split(','))
    position = Fraction(0)
    bpm = points[0][1]
    elapsed = -Fraction(sm_tag(text, 'OFFSET') or '0') * 1000
    for anchor, next_bpm in points:
        if anchor > target:
            break
        elapsed += (anchor - position) * 60000 / bpm
        position, bpm = anchor, next_bpm
    elapsed += (target - position) * 60000 / bpm
    for name in ('STOPS', 'DELAYS'):
        for entry in filter(None, sm_tag(text, name).split(',')):
            anchor, duration = map(Fraction, entry.split('='))
            if anchor < target or (name == 'DELAYS' and anchor == target):
                elapsed += duration * 1000
    return float(elapsed)


def scan_sm(text):
    """Return complete holds and point markers; reject unpaired output rows."""
    files = sm_tag(text, 'KEYSOUNDS').split(',') if sm_tag(text, 'KEYSOUNDS') else []
    match = re.search(r'#NOTES2?:(.*?);', text, re.S)
    body = match.group(1).split(':', 5)[5]
    active, holds, points = {}, [], []
    cell_pattern = re.compile(r'([01234MKLF])(?:\[(\d+)\])?')
    for measure, block in enumerate(body.split(',')):
        rows = [line.strip() for line in block.splitlines() if line.strip()]
        assert len(rows) in (4, 8, 12, 16, 24, 32, 48, 64, 96, 192), len(rows)
        for local, line in enumerate(rows):
            cells = list(cell_pattern.finditer(line))
            assert ''.join(cell.group(0) for cell in cells) == line, line
            row = measure * 192 + local * 192 // len(rows)
            for column, cell in enumerate(cells):
                marker, index = cell.groups()
                sound = files[int(index)] if index is not None else ''
                if marker in ('2', '4'):
                    assert column not in active, ('nested heads', column, row)
                    active[column] = (row, marker, sound)
                elif marker == '3':
                    assert column in active, ('orphan tail', column, row)
                    assert index is None, ('tail keysound', column, row)
                    start, head, bound = active.pop(column)
                    assert row > start, ('empty hold', start, row)
                    holds.append((start, row, column, 'roll' if head == '4' else 'hold', bound))
                elif marker != '0':
                    assert marker != '1' or column not in active, ('interrupted hold', column, row)
                    points.append((row, column, marker, sound))
    assert not active, ('orphan heads', active)
    return sorted(holds), sorted(points)


def osu_text(notes):
    return ('osu file format v14\n[General]\nAudioFilename:song.wav\nMode:3\n'
            '[Metadata]\nTitle:SM Hold Integrity\nArtist:Test\nCreator:Test\nVersion:Hard\n'
            '[Difficulty]\nHPDrainRate:8\nCircleSize:4\nOverallDifficulty:8\n'
            '[TimingPoints]\n0,500,4,1,0,100,1,0\n[HitObjects]\n' + '\n'.join(notes) + '\n')


class SmHoldIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with wave.open(str(self.root / 'song.wav'), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\x00\x00' * 32000)

    def document(self, notes, **kwargs):
        return ChartDocument(key_count=4, audio_filename='song.wav',
                             notes=notes, timings=[(0, 500, 4)], **kwargs)

    def assert_plan(self, document, holds, points=None):
        original = deepcopy(document)
        plan = plan_conversion(document, '.sm')
        self.assertEqual(document, original)
        again = plan_conversion(document, '.sm')
        self.assertEqual((plan.output_text, plan.diagnostics), (again.output_text, again.diagnostics))
        actual_holds, actual_points = scan_sm(plan.output_text)
        self.assertEqual(actual_holds, holds)
        if points is not None:
            self.assertEqual(actual_points, points)
        destination = self.root / 'plan.sm'
        write_chart(document, destination, '.sm')
        self.assertEqual(destination.read_text(encoding='utf-8'), plan.output_text)
        parsed = parse_chart(destination)[0]
        expected = sorted((round(sm_time(plan.output_text, start)), column,
                           round(sm_time(plan.output_text, end)), kind, sound)
                          for start, end, column, kind, sound in holds)
        self.assertEqual(sorted((note.time, note.column, note.end_time, note.note_type, note.sound)
                                for note in parsed.notes if note.end_time is not None), expected)
        return plan

    def diagnostic(self, plan, code, count):
        matching = [item for item in plan.diagnostics if item.code == code]
        self.assertEqual(len(matching), 1, plan.diagnostics)
        self.assertEqual(matching[0].count, count)
        return matching[0]

    def source(self, name, pairs):
        source = self.root / name
        source.write_text(osu_text([f'64,192,{start},128,0,{end}:0:0:0:0:'
                                    for start, end in pairs]), encoding='utf-8')
        return source

    def check_public(self, source, holds):
        document = parse_chart(source)[0]
        plan = plan_conversion(document, '.sm')
        target = self.root / (source.stem + '-' + source.suffix[1:] + '-out')
        planned, warnings = preflight_conversion([source], '.sm', target)
        self.assertEqual(preflight_conversion([source], '.sm', target), (planned, warnings))
        self.assertFalse(target.exists())
        events = []
        outputs = convert_paths([source], '.sm', target, expected_outputs=planned,
                                emit_event=lambda kind, payload: events.append((kind, payload)))
        text = Path(outputs[0]).read_text(encoding='utf-8')
        self.assertEqual(text, plan.output_text)
        self.assertEqual(scan_sm(text)[0], holds)
        for finding in plan.diagnostics:
            message = f'{source.name}：{finding.message}'
            self.assertIn(message, warnings)
            self.assertEqual(sum(payload == message for _kind, payload in events), 1)
        return plan, warnings, outputs

    def test_normal_short_hold_keeps_both_markers_and_measures_the_final_tail(self):
        plan = self.assert_plan(self.document([ChartNoteData(100, 0, 101)]), [(10, 11, 0, 'hold', '')])
        self.diagnostic(plan, 'sm-hold-extended', 1)
        finding = self.diagnostic(plan, 'sm-grid-precision', 1)
        self.assertIn('尾最大误差 13.583 ms', finding.message)

    def test_last_row_extension_creates_the_next_measure(self):
        source = self.source('boundary.osu', [(1990, 1991)])
        plan, _warnings, paths = self.check_public(source, [(191, 192, 0, 'hold', '')])
        self.diagnostic(plan, 'sm-hold-extended', 1)
        self.assertIn('尾最大误差 9.000 ms', self.diagnostic(plan, 'sm-grid-precision', 1).message)
        self.assertEqual([(note.time, note.end_time) for note in parse_chart(paths[0])[0].notes], [(1990, 2000)])

    def test_a_tail_already_in_the_next_measure_is_not_extended(self):
        plan = self.assert_plan(self.document([ChartNoteData(1990, 0, 2000)]), [(191, 192, 0, 'hold', '')])
        self.assertNotIn('sm-hold-extended', {item.code for item in plan.diagnostics})

    def test_extension_resolves_next_measure_tap_and_long_note_conflicts(self):
        for other, code in [(ChartNoteData(2000, 0), 'sm-grid-conflict'),
                            (ChartNoteData(2000, 0, 2500), 'sm-hold-conflict')]:
            with self.subTest(code=code):
                plan = self.assert_plan(self.document([ChartNoteData(1990, 0, 1991), other]),
                                        [(191, 192, 0, 'hold', '')], [])
                self.diagnostic(plan, code, 1)
                self.diagnostic(plan, 'sm-hold-extended', 1)

    def test_nested_crossing_and_cross_measure_overlaps_keep_the_first_whole_hold(self):
        for pairs, expected in [([(0, 1500), (500, 1000)], (0, 144)),
                                ([(0, 1000), (500, 1500)], (0, 96)),
                                ([(1500, 3000), (2500, 3500)], (144, 288))]:
            with self.subTest(pairs=pairs):
                plan = self.assert_plan(self.document([ChartNoteData(a, 0, b) for a, b in pairs]),
                                        [(expected[0], expected[1], 0, 'hold', '')])
                self.diagnostic(plan, 'sm-hold-conflict', 1)
                self.assertEqual(plan.strategy_for('hold')[0], 'downgrade')

    def test_adjacent_holds_remove_the_second_tail_and_allow_a_later_hold(self):
        plan = self.assert_plan(self.document([ChartNoteData(0, 0, 500), ChartNoteData(500, 0, 1000),
                                               ChartNoteData(1000, 0, 1500)]),
                                [(0, 48, 0, 'hold', ''), (96, 144, 0, 'hold', '')])
        self.diagnostic(plan, 'sm-hold-conflict', 1)
        self.assertNotIn('sm-grid-conflict', {item.code for item in plan.diagnostics})

    def test_separated_source_holds_report_quantized_collision(self):
        plan = self.assert_plan(self.document([ChartNoteData(0, 0, 499), ChartNoteData(501, 0, 1000)]),
                                [(0, 48, 0, 'hold', '')])
        self.assertIn('量化后', self.diagnostic(plan, 'sm-hold-conflict', 1).message)

    def test_same_quantized_start_uses_the_source_sequence(self):
        notes = [ChartNoteData(1, 0, 1000, sound='first.wav'),
                 ChartNoteData(0, 0, 1500, sound='second.wav')]
        plan = self.assert_plan(self.document(notes), [(0, 96, 0, 'hold', 'first.wav')])
        self.diagnostic(plan, 'sm-hold-conflict', 1)
        self.assertNotIn('second.wav', sm_tag(plan.output_text, 'KEYSOUNDS'))

    def test_source_identity_never_depends_on_missing_duplicate_or_unhashable_ids(self):
        for source_id in (None, 5, ['duplicate']):
            with self.subTest(source_id=source_id):
                notes = [ChartNoteData(0, 0, 1500, source_id=source_id),
                         ChartNoteData(500, 0, 1000, source_id=source_id),
                         ChartNoteData(500, 1, 1000, source_id=source_id)]
                plan = self.assert_plan(self.document(notes),
                                        [(0, 144, 0, 'hold', ''), (48, 96, 1, 'hold', '')])
                self.diagnostic(plan, 'sm-hold-conflict', 1)
        identical = [ChartNoteData(0, 0, 500), ChartNoteData(0, 0, 500)]
        self.diagnostic(self.assert_plan(self.document(identical), [(0, 48, 0, 'hold', '')]), 'sm-hold-conflict', 1)

    def test_overlapping_different_lanes_both_survive_without_loss(self):
        plan = self.assert_plan(self.document([ChartNoteData(0, 0, 1500), ChartNoteData(500, 1, 1000)]),
                                [(0, 144, 0, 'hold', ''), (48, 96, 1, 'hold', '')])
        self.assertEqual(plan.diagnostics, [])

    def test_discarded_short_hold_has_no_extension_precision_or_ghost_sound_statistics(self):
        notes = [ChartNoteData(0, 0, 2000),
                 ChartNoteData(1990, 0, 1991, sound='ghost.wav', sound_volume=25)]
        plan = self.assert_plan(self.document(notes), [(0, 192, 0, 'hold', '')])
        self.assertEqual({item.code: item.count for item in plan.diagnostics}, {'sm-hold-conflict': 1})
        self.assertNotIn('ghost.wav', plan.output_text)

    def test_roll_and_keysound_survive_as_a_whole_and_tail_has_no_sound_reference(self):
        notes = [ChartNoteData(0, 0, 1500, note_type='roll', sound='roll.wav'),
                 ChartNoteData(500, 0, 1000, sound='ghost.wav', sound_volume=25),
                 ChartNoteData(1500, 0, sound='lost-tap.wav')]
        plan = self.assert_plan(self.document(notes), [(0, 144, 0, 'roll', 'roll.wav')], [])
        self.diagnostic(plan, 'sm-hold-conflict', 1)
        self.diagnostic(plan, 'sm-grid-conflict', 1)
        self.assertEqual(sm_tag(plan.output_text, 'KEYSOUNDS'), 'roll.wav')
        self.assertNotIn('sound-volume-lost', {item.code for item in plan.diagnostics})
        self.assertEqual(plan.strategy_for('roll')[0], 'downgrade')
        self.assertEqual(plan.strategy_for('keysound')[0], 'downgrade')

    def test_interior_tap_cannot_interrupt_a_kept_hold_but_other_roles_remain(self):
        notes = [ChartNoteData(0, 0, 1500), ChartNoteData(500, 0, sound='lost.wav'),
                 ChartNoteData(1000, 0, note_type='mine', sound='mine.wav')]
        plan = self.assert_plan(self.document(notes, sound_events=[ChartSoundEvent(750, 'auto.wav', column=0)]),
                                [(0, 144, 0, 'hold', '')],
                                [(72, 0, 'K', 'auto.wav'), (96, 0, 'M', 'mine.wav')])
        self.diagnostic(plan, 'sm-hold-interruption', 1)
        self.assertNotIn('lost.wav', sm_tag(plan.output_text, 'KEYSOUNDS'))

    def test_special_and_automatic_markers_at_endpoints_keep_existing_priorities(self):
        notes = [ChartNoteData(0, 0, 500), ChartNoteData(0, 0, note_type='mine'),
                 ChartNoteData(500, 0, note_type='lift')]
        plan = self.assert_plan(self.document(notes, sound_events=[ChartSoundEvent(500, 'lost.wav', column=0)]),
                                [(0, 48, 0, 'hold', '')], [])
        self.diagnostic(plan, 'sm-grid-conflict', 3)
        self.assertNotIn('lost.wav', plan.output_text)

    def test_automatic_note_model_keeps_its_role_without_duplicate_loss_counting(self):
        for event_time in (250, 0):
            with self.subTest(event_time=event_time):
                notes = [ChartNoteData(0, 0, 500),
                         ChartNoteData(0, 1, note_type='auto_keysound', sound='auto.wav')]
                events = [ChartSoundEvent(event_time, 'event.wav', column=1)]
                expected = [(0, 1, 'K', 'auto.wav'), (24, 1, 'K', 'event.wav')] if event_time else [(0, 1, 'K', 'event.wav')]
                plan = self.assert_plan(self.document(notes, sound_events=events), [(0, 48, 0, 'hold', '')], expected)
                self.assertEqual(plan.strategies['auto_keysound'].count, 2)
                self.assertNotIn('drop-auto-keysound', {item.code for item in plan.diagnostics})
                if event_time:
                    self.assertEqual(plan.diagnostics, [])
                    self.assertEqual(plan.strategy_for('auto_keysound')[0], 'full')
                else:
                    self.diagnostic(plan, 'sm-grid-conflict', 1)
                    self.assertEqual(plan.strategy_for('auto_keysound')[0], 'downgrade')
                    self.assertNotIn('auto.wav', plan.output_text)

    def test_point_priority_cannot_reintroduce_an_interior_tap(self):
        for points in ([ChartNoteData(500, 0, note_type='mine'), ChartNoteData(500, 0)],
                       [ChartNoteData(500, 0), ChartNoteData(500, 0, note_type='mine')]):
            with self.subTest(order=[note.note_type for note in points]):
                plan = self.assert_plan(self.document([ChartNoteData(0, 0, 1000)] + points),
                                        [(0, 96, 0, 'hold', '')], [(48, 0, 'M', '')])
                self.diagnostic(plan, 'sm-hold-interruption', 1)

    def test_a_losing_short_hold_does_not_turn_a_quantized_collision_into_an_extension(self):
        plan = self.assert_plan(self.document([ChartNoteData(0, 0, 499), ChartNoteData(501, 0, 502)]),
                                [(0, 48, 0, 'hold', '')])
        finding = self.diagnostic(plan, 'sm-hold-conflict', 1)
        self.assertIn('量化后发生碰撞 1 条', finding.message)
        self.assertNotIn('最小一行延长后发生碰撞', finding.message)
        self.assertNotIn('sm-hold-extended', {item.code for item in plan.diagnostics})

    def test_extension_and_tail_error_use_final_offset_bpm_stop_and_delay(self):
        controls = ('#OFFSET:0.125000123;\n#BPMS:0=120.123456789,4=180.987654321;\n'
                    '#STOPS:4=0.1001234567;\n#DELAYS:0=0.0501234567,4=0.0201234567;\n')
        bpm0, bpm1 = 120.123456789, 180.987654321
        head = sm_time(controls, 191)
        doc = ChartDocument(key_count=4, audio_filename='song.wav',
                            timings=[(sm_time(controls, 0), 60000 / bpm0, 4),
                                     (sm_time(controls, 192), 60000 / bpm1, 4)],
                            timing_beats=[0, 4], beat_zero_ms=-125.000123,
                            timing_events=[(0, 50.1234567, 'delay'), (4, 100.1234567, 'stop'),
                                           (4, 20.1234567, 'delay')],
                            notes=[ChartNoteData(head, 0, head + 1),
                                   ChartNoteData(sm_time(controls, 193), 1, sm_time(controls, 200))])
        plan = self.assert_plan(doc, [(191, 192, 0, 'hold', ''), (193, 200, 1, 'hold', '')])
        extension = sm_time(plan.output_text, 192) - sm_time(plan.output_text, 191)
        error = abs(sm_time(plan.output_text, 192) - (head + 1))
        self.assertIn(f'延长最大 {extension:.3f} ms', self.diagnostic(plan, 'sm-hold-extended', 1).message)
        self.assertIn(f'尾最大误差 {error:.3f} ms', self.diagnostic(plan, 'sm-grid-precision', 1).message)
        self.assertNotEqual(round(extension, 3), round(error, 3))

    def test_public_osu_and_mc_paths_match_preflight_and_execution(self):
        source = self.source('overlap.osu', [(0, 1500), (500, 1000)])
        plan, _warnings, _outputs = self.check_public(source, [(0, 144, 0, 'hold', '')])
        self.diagnostic(plan, 'sm-hold-conflict', 1)
        data = {'meta': {'mode': 0, 'mode_ext': {'column': 4}}, 'time': [{'beat': [0, 0, 1], 'bpm': 120}],
                'note': [{'beat': [0, 0, 1], 'column': 0, 'endbeat': [3, 0, 1]},
                         {'beat': [1, 0, 1], 'column': 0, 'endbeat': [2, 0, 1]},
                         {'beat': [0, 0, 1], 'type': 1, 'sound': 'song.wav', 'offset': 0}]}
        source = self.root / 'overlap.mc'
        source.write_text(json.dumps(data), encoding='utf-8')
        self.diagnostic(self.check_public(source, [(0, 144, 0, 'hold', '')])[0], 'sm-hold-conflict', 1)

    def test_archive_member_uses_the_same_complete_hold_layout(self):
        data = {'meta': {'mode': 0, 'mode_ext': {'column': 4}},
                'time': [{'beat': [0, 0, 1], 'bpm': 120}],
                'note': [{'beat': [0, 0, 1], 'column': 0, 'endbeat': [3, 0, 1]},
                         {'beat': [1, 0, 1], 'column': 0, 'endbeat': [2, 0, 1]},
                         {'beat': [0, 0, 1], 'type': 1, 'sound': 'song.wav', 'offset': 0}]}
        archive = self.root / 'hold.mcz'
        with zipfile.ZipFile(archive, 'w') as packaged:
            packaged.writestr('hold.mc', json.dumps(data))
            packaged.write(self.root / 'song.wav', 'song.wav')
        target = self.root / 'archive-out'
        planned, warnings = preflight_conversion([archive], '.sm', target)
        self.assertFalse(target.exists())
        loss = next(message for message in warnings if '1 条长条' in message)
        events = []
        outputs = convert_paths([archive], '.sm', target, expected_outputs=planned,
                                emit_event=lambda kind, payload: events.append(payload))
        self.assertEqual(events.count(loss), 1)
        self.assertEqual(scan_sm(Path(outputs[0]).read_text(encoding='utf-8'))[0],
                         [(0, 144, 0, 'hold', '')])

    def test_controller_and_right_menu_deliver_the_same_whole_hold_loss(self):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.chart_convert import ChartConvertController
        from osu_toolbox.quick import _run_chart_convert
        source = self.source('entry.osu', [(0, 1500), (500, 1000)])
        finding = self.diagnostic(plan_conversion(parse_chart(source)[0], '.sm'), 'sm-hold-conflict', 1)
        message = f'{source.name}：{finding.message}'
        controller = ChartConvertController()
        prepared = controller.prepare({'input_paths': [str(source)], 'target_format': '.sm',
                                       'output_location': 'workdir'},
                                      ToolContext(str(self.root), output_root=str(self.root / 'gui')))
        self.assertIn(message, prepared.warnings)
        events = []
        controller.run(prepared, lambda kind, payload: events.append(payload))
        self.assertEqual(events.count(message), 1)
        self.assertEqual(scan_sm((self.root / 'gui' / 'entry.sm').read_text(encoding='utf-8'))[0],
                         [(0, 144, 0, 'hold', '')])
        with patch('osu_toolbox.quick._chart_difficulty_overrides', return_value=(8, 8)):
            result = _run_chart_convert(SimpleNamespace(force=False, dry_run=False), str(source),
                                        str(self.root / 'menu.sm'), '.sm')
        self.assertIn(message, result['warnings'])
        self.assertEqual(scan_sm((self.root / 'menu.sm').read_text(encoding='utf-8'))[0],
                         [(0, 144, 0, 'hold', '')])


if __name__ == '__main__':
    unittest.main()
