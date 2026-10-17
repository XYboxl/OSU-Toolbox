"""Independent output checks for the conversion correctness review."""

from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from osu_toolbox.chart_convert import (
    ChartConversionError, ChartDocument, ChartNoteData, ChartScrollEvent,
    ChartSoundEvent, convert_paths, parse_chart, plan_conversion,
    preflight_conversion, write_chart,
)


def beat(raw):
    return Fraction(raw[0]) + Fraction(raw[1], raw[2])


def mc_time(data, raw):
    """Integrate the final JSON's BPM sequence without converter helpers."""
    points = sorted((beat(item['beat']), Fraction(str(item['bpm']))) for item in data['time'])
    position = beat(raw)
    previous, bpm = points[0]
    elapsed = previous * 60000 / bpm
    for anchor, next_bpm in points[1:]:
        if anchor > position:
            break
        elapsed += (anchor - previous) * 60000 / bpm
        previous, bpm = anchor, next_bpm
    elapsed += (position - previous) * 60000 / bpm
    bgm = next((item for item in data['note'] if item.get('type') == 1 and 'column' not in item), {})
    return float(elapsed - Fraction(str(bgm.get('offset', 0))))


def osu(objects, timing=None):
    return ('osu file format v14\n[General]\nMode:3\nAudioFilename:song.ogg\n'
            '[Metadata]\nTitle:T\nVersion:Hard\n[Difficulty]\nCircleSize:4\n'
            '[TimingPoints]\n' + '\n'.join(timing or ['0,500,4,1,0,100,1,0'])
            + '\n[HitObjects]\n' + '\n'.join(objects) + '\n')


def mc(notes, timing=None, effects=None, offset=0):
    return {'meta': {'mode': 0, 'mode_ext': {'column': 4}, 'song': {'title': 'T'}},
            'time': timing or [{'beat': [0, 0, 1], 'bpm': 120}],
            'effect': effects or [],
            'note': notes + [{'beat': [0, 0, 1], 'type': 1, 'sound': 'song.ogg', 'offset': offset}]}


def sm_files(text):
    raw = re.search(r'#KEYSOUNDS:((?:\\.|[^;])*)', text).group(1)
    fields = re.split(r'(?<!\\):', raw)
    if len(fields) != 1:
        raise AssertionError('Multiple list fields')
    return [re.sub(r'\\([\\;:#])', r'\1', name) for name in raw.split(',')]


class ReviewOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'song.ogg').write_bytes(b'resource-only')

    def source(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value) if isinstance(value, dict) else value, encoding='utf-8')
        return path

    def convert(self, path, extension):
        return Path(convert_paths([path], extension, self.root)[0])

    def test_sm_standard_lists_from_all_sources_and_shared_keysounds(self):
        for extension in ('.osu', '.mc', '.sm'):
            with self.subTest(extension=extension):
                if extension == '.osu':
                    value = osu(['64,192,0,1,0,0:0:0:0:kick.wav',
                                 '192,192,500,1,0,0:0:0:0:snare.wav',
                                 '320,192,1000,1,0,0:0:0:0:kick.wav'])
                elif extension == '.mc':
                    value = mc([{'beat': [i, 0, 1], 'column': i, 'sound': sound}
                                for i, sound in enumerate(['kick.wav', 'snare.wav', 'kick.wav'])])
                else:
                    value = ('#TITLE:T;#MUSIC:song.ogg;#BPMS:0=120;'
                             '#KEYSOUNDS:kick.wav,snare.wav;'
                             '#NOTES2:dance-single:T:Hard:5:0,0,0,0,0:'
                             '1[0]000\n01[1]00\n00K[0]0\n0000;')
                path = self.source('list-' + extension[1:] + extension, value)
                output = self.convert(path, '.sm').read_text(encoding='utf-8')
                files = sm_files(output)
                self.assertEqual(files, ['kick.wav', 'snare.wav'])
                references = re.findall(r'([1234MLFK])\[(\d+)\]', output)
                self.assertEqual([(marker, files[int(index)]) for marker, index in references],
                                 [('1', 'kick.wav'), ('1', 'snare.wav'),
                                  ('K' if extension == '.sm' else '1', 'kick.wav')])

    def test_sm_keysound_path_escaping_preserves_identity(self):
        sound = 'samples/kick:#;\\hit.wav'
        doc = ChartDocument(notes=[ChartNoteData(0, 0, sound=sound)], timings=[(0, 500, 4)])
        output = self.root / 'escaped.sm'
        write_chart(doc, output, '.sm')
        self.assertEqual(sm_files(output.read_text(encoding='utf-8')), [sound])
        self.assertEqual(parse_chart(output)[0].notes[0].sound, sound)

    def test_sm_unrepresentable_list_path_is_rejected_before_output(self):
        source = self.source('comma.mc', mc([{'beat': [1, 0, 1], 'column': 0, 'sound': 'kick,1.wav'}]))
        with self.assertRaisesRegex(ChartConversionError, '路径'):
            preflight_conversion([source], '.sm', self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def fine_timing(self):
        timing = [{'beat': [0, 0, 1], 'bpm': 120}]
        for i in range(1, 41):
            anchor = Fraction(i) + Fraction(-2 if i % 2 else 2, 1000)
            whole = anchor.numerator // anchor.denominator
            fraction = anchor - whole
            timing.append({'beat': [whole, fraction.numerator, fraction.denominator], 'bpm': 240 if i % 2 else 120})
        return timing

    def test_mc_final_bpm_integration_preserves_the_15480_ms_event(self):
        data = mc([{'beat': [41, 0, 1], 'column': 0}], self.fine_timing())
        source = self.source('fine.mc', data)
        for route in ('write', 'convert'):
            with self.subTest(route=route):
                if route == 'write':
                    output = self.root / 'write.mc'
                    write_chart(parse_chart(source)[0], output, '.mc')
                else:
                    output = self.convert(source, '.mc')
                final = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(final['time'], data['time'])
                self.assertAlmostEqual(mc_time(final, final['note'][0]['beat']), 15480, places=7)

    def test_mc_fine_controls_taps_holds_scrolls_offsets_and_negative_times(self):
        for offset in (-125, 175):
            with self.subTest(offset=offset):
                notes = [{'beat': [-1, 0, 1], 'column': 0}]
                for i in (1, 5, 12, 25, 40):
                    notes += [{'beat': [i, 0, 1], 'column': 1},
                              {'beat': [i, 1, 2], 'endbeat': [i + 1, 0, 1], 'column': 2}]
                effects = [{'beat': point['beat'], 'scroll': 2} for point in self.fine_timing()[1::5]]
                data = mc(notes, self.fine_timing(), effects, offset)
                source = self.source(f'fine{offset}.mc', data)
                final = json.loads(self.convert(source, '.mc').read_text(encoding='utf-8'))
                played = [item for item in final['note'] if 'column' in item]
                for original, actual in zip(notes, played):
                    for key in ('beat', 'endbeat'):
                        if key in original:
                            self.assertLessEqual(abs(mc_time(final, actual[key]) - mc_time(data, original[key])), 1)
                for original, actual in zip(effects, final['effect']):
                    self.assertLessEqual(abs(mc_time(final, actual['beat']) - mc_time(data, original['beat'])), 1)

    def test_mc_precision_statistics_measure_the_final_json(self):
        doc = ChartDocument(notes=[ChartNoteData(1995, 0), ChartNoteData(100, 1, 1996)],
                            timings=[(0, 2000, 4)])
        plan = plan_conversion(doc, '.mc')
        final = json.loads(plan.output_text)
        heads = [abs(mc_time(final, item['beat']) - note.time) for item, note in zip(final['note'], sorted(doc.notes, key=lambda note: note.time))]
        diagnostic = next(item for item in plan.diagnostics if item.code == 'mc-beat-precision')
        self.assertIn(f'{max(heads):.3f}', diagnostic.message)
        self.assertEqual(diagnostic.count, 2)
        self.assertEqual(diagnostic.severity, 'loss')

    def test_missing_bgm_never_promotes_a_keysound(self):
        data = mc([{'beat': [1, 0, 1], 'column': 0, 'sound': 'hit.wav'},
                   {'beat': [2, 0, 1], 'column': 1, 'sound': 'other.wav'}])
        data['note'].pop()
        source = self.source('silent.mc', data)
        doc = parse_chart(source)[0]
        self.assertEqual(doc.audio_filename, '')
        self.assertEqual([note.sound for note in doc.notes], ['hit.wav', 'other.wav'])
        paths, warnings = preflight_conversion([source], '.osu', self.root)
        events = []
        output = convert_paths([source], '.osu', self.root, expected_outputs=paths, emit_event=lambda k,v: events.append((k,v)))
        self.assertNotIn('AudioFilename: hit.wav', Path(output[0]).read_text(encoding='utf-8'))
        self.assertTrue(any('未声明主音乐' in message for message in warnings))
        self.assertTrue(any('未声明主音乐' in str(message) for kind,message in events))

    def test_bgm_position_and_same_file_keysound_keep_separate_roles(self):
        for index in (0, 1, 2):
            data = mc([{'beat': [1, 0, 1], 'column': 0, 'sound': 'song.ogg'},
                       {'beat': [2, 0, 1], 'column': 1, 'sound': 'other.wav'}], offset=-250)
            data['note'].insert(index, data['note'].pop())
            doc = parse_chart(self.source(f'roles{index}.mc', data))[0]
            self.assertEqual((doc.audio_filename, doc.audio_delay_ms), ('song.ogg', -250))
            self.assertEqual([note.sound for note in doc.notes], ['song.ogg', 'other.wav'])
            final = json.loads(plan_conversion(doc, '.mc').output_text)
            self.assertEqual([note.get('sound') for note in final['note']], ['song.ogg', 'other.wav', 'song.ogg'])

    def test_multiple_bgm_declarations_are_explicitly_blocked(self):
        data = mc([{'beat': [1, 0, 1], 'column': 0}])
        data['note'].append({'beat': [2, 0, 1], 'type': 1, 'sound': 'other.wav', 'offset': 0})
        source = self.source('multi-bgm.mc', data)
        for action in (lambda: preflight_conversion([source], '.osu', self.root),
                       lambda: self.convert(source, '.osu')):
            with self.assertRaisesRegex(ChartConversionError, '多个主音乐'):
                action()
        self.assertFalse((self.root / 'multi-bgm.osu').exists())

    def test_unpositioned_sound_without_bgm_evidence_is_an_auto_event(self):
        data = mc([{'beat': [1, 0, 1], 'sound': 'hit.wav'}, {'beat': [2, 0, 1], 'column': 0}])
        doc = parse_chart(self.source('auto.mc', data))[0]
        self.assertEqual(doc.audio_filename, 'song.ogg')
        self.assertEqual([(e.time,e.sound) for e in doc.sound_events], [(500, 'hit.wav')])
        plan = plan_conversion(doc, '.osu')
        self.assertEqual(next(d.count for d in plan.diagnostics if d.code == 'drop-auto-keysound'), 1)

    def test_osu_mixed_timing_points_are_sorted_and_ties_keep_all_fields(self):
        points = ['0,500,3,1,0,100,1,0', '1000,-50,4,2,5,20,0,1',
                  '500,-100,5,3,2,80,0,0', '1000,250,7,1,3,80,1,0',
                  '1000,-25,6,3,7,0,0,1', '1000,200,8,2,4,90,1,1']
        source = self.source('timing.osu', osu(['64,192,1500,1,0'], points))
        text = self.convert(source, '.osu').read_text(encoding='utf-8')
        rows = text.split('[TimingPoints]\n')[1].split('\n[')[0].strip().splitlines()
        self.assertEqual([tuple(line.split(',')[1:]) for line in rows],
                         [tuple(line.split(',')[1:]) for line in sorted(points, key=lambda line: float(line.split(',')[0]))])
        self.assertEqual([float(line.split(',')[0]) for line in rows], [0,500,1000,1000,1000,1000])

    def test_generated_osu_ties_apply_bpm_before_scroll(self):
        doc = ChartDocument(notes=[ChartNoteData(500,0)], timings=[(0,500,4),(500,250,4)],
                            scrolls=[ChartScrollEvent(500,2)])
        text = plan_conversion(doc, '.osu').output_text
        rows = text.split('[TimingPoints]\n')[1].split('\n[')[0].strip().splitlines()
        self.assertEqual([line.split(',')[6] for line in rows], ['1','1','0'])

    def test_mc_mixed_effects_keep_fields_values_and_identity(self):
        effects = [{'beat':[1,0,1], 'scroll':2, 'showbar':False, 'sign':3, 'jump':4},
                   {'beat':[1,0,1], 'scroll':0, 'value':None, 'other':0},
                   {'beat':[1,0,1], 'scroll':-1}, {'beat':[1,0,1], 'hs':False}]
        data = mc([{'beat':[2,0,1], 'column':0}], effects=effects)
        original = deepcopy(data)
        source = self.source('effect.mc', data)
        doc = parse_chart(source)[0]
        saved = deepcopy(doc)
        final = json.loads(self.convert(source, '.mc').read_text(encoding='utf-8'))
        self.assertEqual(final['effect'], effects)
        first = plan_conversion(doc, '.mc')
        self.assertEqual(doc, saved)
        self.assertEqual(first.output_text, plan_conversion(doc, '.mc').output_text)
        self.assertEqual(json.loads(source.read_text(encoding='utf-8')), original)
        for extension in ('.osu','.sm'):
            diagnostic = next(d for d in plan_conversion(doc, extension).diagnostics if d.code == 'mc-effect-fields-lost')
            self.assertEqual(diagnostic.count, 3)
            self.assertIn('showbar', diagnostic.message)

    def test_preflight_and_execution_match_collapse_and_default_sound_losses(self):
        for name, obj, code in [('short', '64,192,0,128,0,1:0:0:0:0:', 'mc-tail-clamped'),
                                ('sound', '64,192,500,1,2,0:0:0:0:', 'osu-default-sound-lost')]:
            source = self.source(name+'.osu', osu([obj]))
            doc = parse_chart(source)[0]
            plan = plan_conversion(doc, '.mc')
            diagnostic = next(d for d in plan.diagnostics if d.code == code)
            self.assertEqual(diagnostic.count, 1)
            destination = self.root / (name+'-out')
            paths, warnings = preflight_conversion([source], '.mc', destination)
            self.assertEqual((paths,warnings), preflight_conversion([source], '.mc', destination))
            self.assertFalse(destination.exists())
            events=[]
            final = convert_paths([source], '.mc', destination, expected_outputs=paths, emit_event=lambda k,v:events.append((k,v)))
            self.assertEqual(warnings.count(name+'.osu：'+diagnostic.message), 1)
            self.assertEqual([str(v) for k,v in events].count(name+'.osu：'+diagnostic.message), 1)
            data=json.loads(Path(final[0]).read_text(encoding='utf-8'))
            self.assertNotIn('endbeat', data['note'][0])

    def test_preflight_uses_actual_sm_conflicts_extensions_and_volume(self):
        doc = ChartDocument(notes=[ChartNoteData(0,0,1,sound='hit.wav',sound_volume=25),
                                  ChartNoteData(0,0), ChartNoteData(0,1,sound='other.wav')], timings=[(0,500,4)])
        plan = plan_conversion(doc, '.sm')
        self.assertEqual({d.code:d.count for d in plan.diagnostics},
                         {'sm-grid-conflict':1, 'sm-hold-extended':1, 'sm-grid-precision':1, 'sound-volume-lost':1})
        source=self.source('conflict.osu',osu(['64,192,0,128,0,1:0:0:0:25:hit.wav','64,192,0,1,0']))
        _, warnings=preflight_conversion([source],'.sm',self.root)
        self.assertTrue(any('同格冲突' in w for w in warnings))
        self.assertTrue(any('过短长条' in w for w in warnings))
        self.assertTrue(any('非默认音量' in w for w in warnings))

    def test_preflight_nonpositive_sv_matches_final_osu_controls(self):
        source=self.source('sv.mc',mc([{'beat':[1,0,1],'column':0}],effects=[{'beat':[0,0,1],'scroll':0},{'beat':[1,0,1],'scroll':-2}]))
        _, warnings=preflight_conversion([source],'.osu',self.root)
        self.assertTrue(any('2 个非正' in w for w in warnings))
        text=self.convert(source,'.osu').read_text(encoding='utf-8')
        self.assertNotIn(',0,0\n', text.split('[TimingPoints]\n')[1].split('[HitObjects]')[0])

    def test_plain_representable_chart_has_no_content_loss_noise(self):
        source=self.source('plain.osu',osu(['64,192,0,1,0','192,192,500,128,0,1000:0:0:0:0:']))
        for extension in ('.osu','.mc','.sm','.mcz'):
            plan=plan_conversion(parse_chart(source)[0],extension)
            self.assertEqual(plan.diagnostics, [])
            self.assertEqual(preflight_conversion([source],extension,self.root)[1], [])

    def test_preflight_content_change_is_blocked_with_the_same_output_path(self):
        source=self.source('changed.osu',osu(['64,192,0,1,0']))
        out=self.root/'out'
        paths,_=preflight_conversion([source],'.mc',out)
        source.write_text(osu(['64,192,0,128,0,1:0:0:0:0:']), encoding='utf-8')
        with self.assertRaisesRegex(ChartConversionError,'输入内容与预检'):
            convert_paths([source],'.mc',out,expected_outputs=paths)
        self.assertFalse(out.exists())

    def test_archive_preflight_reports_final_loss_and_keeps_resources(self):
        source=self.root/'source.osz'
        with zipfile.ZipFile(source,'w') as archive:
            archive.writestr('short.osu',osu(['64,192,0,128,0,1:0:0:0:0:']))
            archive.writestr('song.ogg',b'resource-only')
        paths,warnings=preflight_conversion([source],'.mcz',self.root)
        self.assertTrue(any('1 个长条尾部' in w for w in warnings))
        output=convert_paths([source],'.mcz',self.root,expected_outputs=paths)[0]
        with zipfile.ZipFile(output) as archive:
            data=json.loads(archive.read('short.mc'))
            self.assertNotIn('endbeat',data['note'][0])
            self.assertEqual(archive.read('song.ogg'),b'resource-only')

    def test_link_identity_survives_skipped_events_at_all_positions(self):
        for position in (0,1,2):
            items=[{'beat':[1,0,1],'column':0},{'beat':[3,0,1],'column':0}]
            items.insert(position,{'beat':[0,0,1],'sound':'song.ogg','type':1,'offset':0})
            ids=[i for i,item in enumerate(items) if 'column' in item]
            items[ids[0]]['link']={'a':ids[1]};items[ids[1]]['link']={'b':ids[0]}
            data=mc([]);data['note']=items
            doc=parse_chart(self.source(f'link{position}.mc',data))[0]
            self.assertEqual([(n.time,n.end_time,n.note_type) for n in doc.notes],[(500,1500,'hold')])
            final=json.loads(plan_conversion(doc,'.mc').output_text)
            self.assertEqual(len([n for n in final['note'] if 'column' in n]),1)
            self.assertAlmostEqual(mc_time(final,final['note'][0]['endbeat']),1500)

    def test_multiple_links_and_native_endbeat_are_separate_paths(self):
        data=mc([{'beat':[1,0,1],'column':0,'link':{'a':2}},
                 {'beat':[1,0,1],'column':1,'endbeat':[2,0,1]},
                 {'beat':[3,0,1],'column':0,'link':{'b':0}},
                 {'beat':[2,0,1],'column':2,'link':{'a':4}},
                 {'beat':[4,0,1],'column':2,'link':{'b':3}}])
        doc=parse_chart(self.source('links.mc',data))[0]
        self.assertEqual([(n.time,n.column,n.end_time) for n in doc.notes],[(500,0,1500),(500,1,1000),(1000,2,2000)])

    def test_invalid_links_are_diagnosed_and_refused(self):
        for target, column, reverse in [(99,0,False),(0,0,False),(1,1,False),(1,0,True),(2,0,False)]:
            items=[{'beat':[1,0,1],'column':0,'link':{'a':target}},
                   {'beat':[0 if reverse else 3,0,1],'column':column},
                   {'beat':[0,0,1],'type':1,'sound':'song.ogg','offset':0}]
            data=mc([]);data['note']=items
            source=self.source(f'bad{target}{column}{reverse}.mc',data)
            doc=parse_chart(source)[0]
            self.assertTrue(any(d.code=='mc-link-invalid' and d.severity=='error' for d in doc.diagnostics))
            with self.assertRaisesRegex(ChartConversionError,'链接无效'):
                preflight_conversion([source],'.osu',self.root)

    def test_duplicate_and_cycle_links_are_not_silently_taps(self):
        for items in ([{'beat':[1,0,1],'column':0,'link':{'a':2}},
                       {'beat':[2,0,1],'column':0,'link':{'a':2}}, {'beat':[3,0,1],'column':0}],
                      [{'beat':[1,0,1],'column':0,'link':{'a':1}}, {'beat':[3,0,1],'column':0,'link':{'a':0}}]):
            source=self.source('bad-links.mc',mc(items))
            with self.assertRaisesRegex(ChartConversionError,'链接无效'):
                self.convert(source,'.mc')

    def test_nonkey_and_ambiguous_modes_are_safely_refused(self):
        for mode in (1,3,5,7,18):
            for native_width in (True,False):
                data=mc([{'beat':[1,0,1],'column':0}]);data['meta']['mode']=mode
                if not native_width: del data['meta']['mode_ext']
                with self.assertRaisesRegex(ChartConversionError,'Key'):
                    parse_chart(self.source(f'mode{mode}{native_width}.mc',data))

    def test_missing_native_key_width_is_explicitly_diagnosed(self):
        data=mc([{'beat':[1,0,1],'column':3}]);del data['meta']['mode_ext']
        doc=parse_chart(self.source('width.mc',data))[0]
        self.assertEqual(doc.key_count,4)
        self.assertTrue(any(d.code=='mc-key-width-inferred' for d in doc.diagnostics))

    def test_controller_uses_final_losses_and_detects_changed_input(self):
        from osu_toolbox.tools.base import ToolContext
        from osu_toolbox.tools.chart_convert import ChartConvertController
        source=self.source('controller.osu',osu(['64,192,0,128,0,1:0:0:0:0:']))
        controller=ChartConvertController()
        context=ToolContext(str(self.root),output_root=str(self.root/'out'))
        prepared=controller.prepare({'input_paths':[str(source)],'target_format':'.mc','output_location':'workdir'},context)
        self.assertTrue(any('1 个长条尾部' in message for message in prepared.warnings))
        source.write_text(osu(['64,192,500,1,2,0:0:0:0:']),encoding='utf-8')
        with self.assertRaisesRegex(ChartConversionError,'输入内容与预检'):
            controller.run(prepared,lambda kind,payload:None)
        self.assertFalse((self.root/'out').exists())

    def test_right_menu_uses_the_same_content_diagnostics(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from osu_toolbox.quick import _run_chart_convert
        source=self.source('menu.osu',osu(['64,192,0,128,0,1:0:0:0:0:']))
        _,warnings=preflight_conversion([source],'.mc',self.root)
        with patch('osu_toolbox.quick._chart_difficulty_overrides',return_value=(8,8)):
            result=_run_chart_convert(SimpleNamespace(force=False,dry_run=False),str(source),str(self.root/'menu.mc'),'.mc')
        message=next(w for w in warnings if '长条尾部' in w)
        self.assertIn(message,result['warnings'])
        self.assertNotIn('endbeat',json.loads((self.root/'menu.mc').read_text(encoding='utf-8'))['note'][0])

    def test_real_nonkey_structure_is_not_a_keyboard_width(self):
        # Minimal non-Key chart structure; the declared mode must be rejected.
        data={'meta':{'mode':5,'mode_ext':{'bar_begin':2}},
              'time':[{'beat':[0,0,1],'bpm':178}],
              'note':[{'beat':[2,0,4],'column':0,'style':0}]}
        source=self.source('nonkey.mc',data)
        with self.assertRaisesRegex(ChartConversionError,'mode: 5'):
            parse_chart(source)

    def test_explicit_default_osu_samples_have_no_loss_noise(self):
        source=self.source('defaults.osu',osu(['64,192,0,1,1,1:1:0:0:']))
        self.assertEqual(plan_conversion(parse_chart(source)[0],'.mc').diagnostics,[])

    def test_link_to_a_skipped_event_is_rejected_without_array_compression(self):
        data=mc([{'beat':[1,0,1],'column':0,'link':{'a':1}},
                 {'beat':[2,0,1],'unrecognised':True}, {'beat':[3,0,1],'column':0}])
        source=self.source('skipped.mc',data)
        with self.assertRaisesRegex(ChartConversionError,'链接无效'):
            self.convert(source,'.mc')

    def test_zero_keysound_volume_is_not_silently_called_full_for_osu(self):
        source=self.source('zero.mc',mc([{'beat':[1,0,1],'column':0,'sound':'hit.wav','vol':0}]))
        plan=plan_conversion(parse_chart(source)[0],'.osu')
        diagnostic=next(item for item in plan.diagnostics if item.code=='osu-zero-volume-lost')
        self.assertEqual(diagnostic.count,1)
        self.assertIn('零音量',diagnostic.message)

    def test_mc_rational_approximation_is_not_a_fixed_192nd_grid(self):
        doc=ChartDocument(notes=[ChartNoteData(500/7,0)],timings=[(0,500,4)])
        final=json.loads(plan_conversion(doc,'.mc').output_text)
        self.assertEqual(final['note'][0]['beat'],[0,1,7])
        self.assertNotEqual((beat(final['note'][0]['beat'])*192).denominator,1)
        self.assertAlmostEqual(mc_time(final,final['note'][0]['beat']),500/7)

    def test_nondefault_meter_is_reported_before_sm_output(self):
        source=self.source('meter.osu',osu(['64,192,0,1,0'],['0,500,3,1,0,100,1,0']))
        plan=plan_conversion(parse_chart(source)[0],'.sm')
        self.assertEqual(next(d.count for d in plan.diagnostics if d.code=='sm-meter-lost'),1)
        self.assertTrue(any('1 个非 4/4' in w for w in preflight_conversion([source],'.sm',self.root)[1]))

    def test_timing_flag_losses_count_points_not_the_bitmask_value(self):
        source=self.source('barline.osu',osu(['64,192,0,1,0'],['0,500,4,1,0,100,1,8']))
        diagnostic=next(d for d in plan_conversion(parse_chart(source)[0],'.mc').diagnostics if d.code=='timing-sample-lost')
        self.assertEqual(diagnostic.count,1)

    def test_sm_precision_and_sound_loss_only_count_surviving_markers(self):
        # The first tap is replaced by an exactly representable hold head.
        source=self.source('displaced.osu',osu(['64,192,1,1,2,0:0:0:25:hit.wav',
                                              '64,192,0,128,0,500:0:0:0:0:']))
        plan=plan_conversion(parse_chart(source)[0],'.sm')
        self.assertEqual({d.code:d.count for d in plan.diagnostics},{'sm-grid-conflict':1})


if __name__ == '__main__':
    unittest.main()
