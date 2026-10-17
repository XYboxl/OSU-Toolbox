"""Regression cover for the chart conversion time model and scroll migration.

Three areas are locked down here:

* FIX-03 -- a source stop/delay must keep every click at the same
  music-relative millisecond.  ``.mc`` cannot express either event, so the notes
  are written on the pure BPM grid; ``.osu`` keeps milliseconds directly;
  ``.sm`` keeps the tags and the beat coordinates.
* FIX-06 -- quantisation must stay visible.  The StepMania 192-row grid and the
  Malody bounded-denominator rational approximation may extend, drop or displace a note, but every
  one of those decisions has to be counted and reported, and a note the grid
  *can* express must not move at all.
* M3 -- scroll-velocity migration: osu! inherited points to Malody ``effect``
  and back, and the counted, diagnosed drop toward StepMania.

No assertion below relies on the project's own parse -> write -> parse round
trip as its only evidence: a mistake shared by the reader and the writer would
cancel out.  Every expected millisecond value is derived by hand from the
fixture's BPM/stop/delay -- the arithmetic is written beside the assertion --
and then read out of the written file's own fields (the ``.mc``
``time``/``beat``/``endbeat`` arrays, the ``.sm`` note rows and the ``.osu``
timestamps).  The project parser is used only as a secondary sanity check.
"""

import json
import tempfile
import unittest
from pathlib import Path

from osu_toolbox.chart_convert import (
    # ``ChartScrollEvent``/``write_chart`` cover the one document no single
    # source format can hold: a chart carrying both a StepMania stop and an
    # osu! scroll change (FIX-03 + M3 together).
    ChartScrollEvent,
    convert_paths,
    parse_chart,
    plan_conversion,
    write_chart,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _osu_chart(objects, timing_points=("0,500,4,1,0,100,1,0",), key_count=4):
    """Return a minimal osu!mania chart.

    ``[General]`` always declares ``Mode: 3``: the extension alone never makes
    a file a key chart.
    """
    return (
        "osu file format v14\n"
        "[General]\n"
        "AudioFilename: song.mp3\n"
        "Mode: 3\n"
        "[Metadata]\n"
        "Title: T\n"
        "Artist: A\n"
        "Version: Hard\n"
        "[Difficulty]\n"
        f"CircleSize: {key_count}\n"
        "[TimingPoints]\n"
        + "\n".join(timing_points)
        + "\n[HitObjects]\n"
        + "\n".join(objects)
        + "\n"
    )


def _sm_chart(measures, *, bpms="0.000000=120.000000", stops=None, delays=None,
              offset="0.000000", music="song.ogg", difficulty="Hard"):
    """Return a one-chart StepMania file around ``measures`` (row text).

    A measure is one ``#NOTES`` row block; rows are four beats apart divided by
    the row count, so a four-row measure puts its rows on beats 0, 1, 2, 3.
    """
    tags = [
        "#TITLE:T;",
        f"#MUSIC:{music};",
        f"#OFFSET:{offset};",
        f"#BPMS:{bpms};",
    ]
    if stops:
        tags.append(f"#STOPS:{stops};")
    if delays:
        tags.append(f"#DELAYS:{delays};")
    return (
        "".join(tags)
        + f"#NOTES:\n dance-single:\n :\n {difficulty}:\n 5:\n 0.000:\n"
        + measures
        + "\n;\n"
    )


# ---------------------------------------------------------------------------
# Independent readers of the written files
# ---------------------------------------------------------------------------


def _mc_beat(raw):
    """Beat value of a Malody ``[whole, numerator, denominator]`` tuple."""
    whole, numerator, denominator = raw
    return float(whole) + float(numerator) / float(denominator)


def _mc_chart_relative_ms(beat, time_entries):
    """Elapsed chart milliseconds of ``beat`` under a written ``time`` array.

    Hand model of the format: entry *i* means "from ``beat_i`` on the tempo is
    ``bpm_i``", the first entry sits at ``beat_0 * 60000 / bpm_0`` on the chart
    clock, and the chart clock integrates every earlier segment.  The
    chart/audio ``offset`` field is deliberately not applied here.
    """
    entries = sorted(time_entries, key=lambda item: _mc_beat(item["beat"]))
    segment_beat = _mc_beat(entries[0]["beat"])
    segment_bpm = float(entries[0]["bpm"])
    elapsed = segment_beat * 60000.0 / segment_bpm
    for entry in entries[1:]:
        next_beat = _mc_beat(entry["beat"])
        next_bpm = float(entry["bpm"])
        if next_beat > beat:
            break
        elapsed += (next_beat - segment_beat) * 60000.0 / segment_bpm
        segment_beat, segment_bpm = next_beat, next_bpm
    return elapsed + (beat - segment_beat) * 60000.0 / segment_bpm


def _mc_audio_ms(beat, data):
    """Millisecond of ``beat`` on the written ``.mc`` audio timeline."""
    return _mc_chart_relative_ms(beat, data["time"]) - _mc_offset_ms(data)


def _mc_offset_ms(data):
    """The chart/audio ``offset`` the writer put on the Malody BGM note."""
    for item in data["note"]:
        if item.get("type") == 1:
            return item["offset"]
    raise AssertionError("the written .mc has no BGM note carrying the offset")


def _mc_played_notes(data):
    """The positioned (playable) notes of a written ``.mc``."""
    return [item for item in data["note"] if "column" in item]


def _mc_effects(data):
    """Malody ``effect`` entries restricted to scroll changes."""
    return [
        item for item in data.get("effect") or ()
        if isinstance(item, dict) and "scroll" in item
    ]


def _sm_note_rows(text):
    """Note rows of a file written by this converter, grouped per measure."""
    body = text.split("#NOTES:", 1)[1].rsplit(":", 1)[1].rsplit(";", 1)[0]
    return [
        [row.strip() for row in measure.splitlines() if row.strip()]
        for measure in body.split(",")
    ]


def _sm_cells(text):
    """``{(measure, row, column): marker}`` for every non-empty written cell."""
    cells = {}
    for measure_index, rows in enumerate(_sm_note_rows(text)):
        for row_index, row in enumerate(rows):
            for column, cell in enumerate(row):
                if cell != "0":
                    cells[(measure_index, row_index, column)] = cell
    return cells


def _osu_section(text, name):
    """Return the non-empty lines of one ``[name]`` section."""
    body = text.split(f"[{name}]", 1)[1]
    body = body.split("\n[", 1)[0]
    return [line.strip() for line in body.splitlines() if line.strip()]


def _osu_hit_objects(text):
    """Return ``[(time_ms, x, object_type)]`` of the written ``.osu``."""
    objects = []
    for line in _osu_section(text, "HitObjects"):
        parts = line.split(",")
        objects.append((int(parts[2]), int(parts[0]), int(parts[3])))
    return objects


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


class ConversionTestCase(unittest.TestCase):
    """Fixture, conversion and event helpers shared by the three areas."""

    def fixture(self, root, name, text):
        path = Path(root) / name
        path.write_text(text, encoding="utf-8")
        return path

    def convert(self, root, source, target_format, label):
        """Convert one chart into ``root/label`` and return ``(path, events)``.

        Each target gets its own folder so two conversions of the same fixture
        cannot claim the same output name, and every emitted event is captured
        in call order.
        """
        destination = Path(root) / label
        destination.mkdir(parents=True, exist_ok=True)
        events = []
        outputs = convert_paths(
            [source],
            target_format,
            destination,
            emit_event=lambda kind, payload: events.append((kind, payload)),
        )
        return Path(outputs[0]), events

    def messages(self, events, kind=None):
        return [
            payload for event_kind, payload in events
            if kind is None or event_kind == kind
        ]

    def expect_message(self, events, needle, kind=None):
        matches = [
            payload for payload in self.messages(events, kind) if needle in payload
        ]
        self.assertTrue(
            matches,
            f"no {kind or 'any'} message contains {needle!r}: "
            f"{self.messages(events, kind)!r}",
        )
        return matches[0]


# ---------------------------------------------------------------------------
# FIX-03 -- stop/delay keep the music-relative click time
# ---------------------------------------------------------------------------


class StopDelayTimeModelTests(ConversionTestCase):
    """FIX-03: pauses never move a click, only its beat coordinate."""

    def _pause_fixture(self, root):
        """120 BPM, stop after beat 1 (0.5 s), delay before beat 2 (0.25 s).

        One measure of four rows puts the taps on beats 0, 1, 2 and 3:
            0 * 500                               =    0 ms
            1 * 500                               =  500 ms
            2 * 500 + 500 (stop) + 250 (delay)    = 1750 ms
            3 * 500 + 500 (stop) + 250 (delay)    = 2250 ms
        """
        return self.fixture(root, "events.sm", _sm_chart(
            "1000\n0100\n0010\n0001",
            stops="1.000000=0.500000",
            delays="2.000000=0.250000",
        ))

    def test_pauses_keep_click_times_on_the_mc_beat_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._pause_fixture(root)
            document = parse_chart(source)[0]
            plan = plan_conversion(document, ".mc")
            self.assertEqual(plan.strategy_for("stop"), ("approximate", "preserve-time"))
            self.assertEqual(plan.strategy_for("delay"), ("approximate", "preserve-time"))
            self.assertEqual(plan.strategies["stop"].count, 1)
            self.assertEqual(plan.strategies["delay"].count, 1)

            output, events = self.convert(root, source, ".mc", "mc")
            message = self.expect_message(events, "stop/delay", kind="warning")
            self.assertIn("音乐相对时间", message)

            data = json.loads(output.read_text(encoding="utf-8"))
            played = _mc_played_notes(data)
            # .mc has no stop/delay field, so the clicks are written on the pure
            # 120 BPM grid (500 ms per beat): 0, 500, 1750 and 2250 ms are beats
            # 0, 1, 3.5 and 4.5 there.
            self.assertEqual(
                [item["beat"] for item in played],
                [[0, 0, 4], [1, 0, 4], [3, 1, 2], [4, 1, 2]],
            )
            offset = _mc_offset_ms(data)
            self.assertIsInstance(offset, int)
            self.assertEqual(offset, 0)
            derived = [
                _mc_beat(item["beat"]) * 60000.0 / float(data["time"][0]["bpm"]) - offset
                for item in played
            ]
            self.assertEqual([round(value) for value in derived], [0, 500, 1750, 2250])
            self.assertEqual(
                [round(_mc_audio_ms(_mc_beat(item["beat"]), data)) for item in played],
                [0, 500, 1750, 2250],
            )

    def test_pauses_keep_click_times_as_milliseconds_in_osu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._pause_fixture(root)
            output, events = self.convert(root, source, ".osu", "osu")
            message = self.expect_message(events, "音乐相对时间", kind="warning")
            self.assertIn("近似", message)

            objects = _osu_hit_objects(output.read_text(encoding="utf-8"))
            # Same arithmetic as above: 0/500/1750/2250 ms, one lane each.
            self.assertEqual([item[0] for item in objects], [0, 500, 1750, 2250])
            self.assertEqual([item[1] for item in objects], [64, 192, 320, 448])
            self.assertEqual([item[2] for item in objects], [1, 1, 1, 1])

    def test_pauses_survive_an_sm_rewrite_with_unchanged_beats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._pause_fixture(root)
            plan = plan_conversion(parse_chart(source)[0], ".sm")
            self.assertEqual(plan.strategy_for("stop"), ("full", "keep"))
            self.assertEqual(plan.strategy_for("delay"), ("full", "keep"))

            output, events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            self.assertIn("#STOPS:1.000000=0.500000;", text)
            self.assertIn("#DELAYS:2.000000=0.250000;", text)
            # A native pause keeps the beat coordinates: the four taps stay on
            # rows 0, 48, 96 and 144 of measure 0 (48 rows per beat).
            self.assertEqual(_sm_cells(text), {
                (0, 0, 0): "1",
                (0, 48, 1): "1",
                (0, 96, 2): "1",
                (0, 144, 3): "1",
            })
            self.assertEqual(
                [message for message in self.messages(events) if "stop/delay" in message],
                [],
            )

    def test_a_hold_head_and_tail_keep_their_time_across_a_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Eight rows per measure put the head on beat 0.5 (row 1) and the
            # tail on beat 2.5 (row 5); the stop pauses after beat 1:
            #     head 0.5 * 500                       =  250 ms
            #     tail 2.5 * 500 + 500 (stop)          = 1750 ms
            source = self.fixture(root, "hold.sm", _sm_chart(
                "0000\n2000\n0000\n0000\n0000\n3000\n0000\n0000",
                stops="1.000000=0.500000",
            ))
            parsed = parse_chart(source)[0]
            self.assertEqual(
                [(note.time, note.end_time) for note in parsed.notes],
                [(250, 1750)],
            )

            output, events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            played = _mc_played_notes(data)
            self.assertEqual(len(played), 1)
            # 250 ms and 1750 ms are beats 0.5 and 3.5 of the pure BPM grid.
            self.assertEqual(played[0]["beat"], [0, 1, 2])
            self.assertEqual(played[0]["endbeat"], [3, 1, 2])
            self.assertEqual(
                round(_mc_audio_ms(_mc_beat(played[0]["beat"]), data)), 250
            )
            self.assertEqual(
                round(_mc_audio_ms(_mc_beat(played[0]["endbeat"]), data)), 1750
            )
            self.assertEqual(
                [
                    message for message in self.messages(events)
                    if "压到" in message or "精度" in message or "无法精确表达" in message
                ],
                [],
            )

    def test_a_stop_across_a_bpm_change_keeps_both_segments_timed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # #BPMS:0=120,4=240 (500 ms then 250 ms per beat), #STOPS:2=0.250.
            # Measure 0 has four rows (beats 0, 1, 2, 3), measure 1 two rows
            # (beats 4 and 6):
            #     beat 0 -> 0                                        =    0 ms
            #     beat 1 -> 500                                      =  500 ms
            #     beat 2 -> 1000  (the stop pauses after this click) = 1000 ms
            #     beat 3 -> 1500 + 250                               = 1750 ms
            #     beat 4 -> 2000 + 250                               = 2250 ms
            #     beat 6 -> 2000 + 250 + 2 * 250                     = 2750 ms
            source = self.fixture(root, "multi.sm", _sm_chart(
                "1000\n0100\n0010\n0001\n,\n1000\n0100",
                bpms="0.000000=120.000000,4.000000=240.000000",
                stops="2.000000=0.250000",
            ))
            self.assertEqual(
                [note.time for note in parse_chart(source)[0].notes],
                [0, 500, 1000, 1750, 2250, 2750],
            )

            output, events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    (_mc_beat(item["beat"]), float(item["bpm"]))
                    for item in data["time"]
                ],
                [(0.0, 120.0), (4.0, 240.0)],
            )
            played = _mc_played_notes(data)
            # Pure BPM grid: the stop's 250 ms push the notes after it 0.5 beats
            # in the 120 BPM segment and 1 beat in the 240 BPM segment.
            self.assertEqual(
                [_mc_beat(item["beat"]) for item in played],
                [0.0, 1.0, 2.0, 3.5, 5.0, 7.0],
            )
            self.assertEqual(
                [round(_mc_audio_ms(_mc_beat(item["beat"]), data)) for item in played],
                [0, 500, 1000, 1750, 2250, 2750],
            )

            osu_output, _osu_events = self.convert(root, source, ".osu", "osu")
            self.assertEqual(
                [item[0] for item in _osu_hit_objects(osu_output.read_text(encoding="utf-8"))],
                [0, 500, 1000, 1750, 2250, 2750],
            )

    def test_a_delay_on_beat_zero_and_a_negative_chart_origin_keep_their_times(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # #OFFSET:1.0 puts beat 0 at -1000 ms; a delay (0.25 s) is applied
            # before a click on its own beat, a stop (0.125 s) after it.  Two
            # rows in a measure are 4 / 2 = two beats apart, so the rows sit on
            # beats 0 and 2:
            #     beat 0 -> 0 * 500 - 1000 + 250                    =  -750 ms
            #     beat 2 -> 2 * 500 - 1000 + 250 + 125              =   375 ms
            source = self.fixture(root, "negative.sm", _sm_chart(
                "1000\n0100",
                offset="1.000000",
                delays="0.000000=0.250000",
                stops="0.000000=0.125000",
            ))
            self.assertEqual(
                [note.time for note in parse_chart(source)[0].notes],
                [-750, 375],
            )

            output, events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            played = _mc_played_notes(data)
            offset = _mc_offset_ms(data)
            self.assertIsInstance(offset, int)
            self.assertEqual(offset, 1000)
            # The .mc grid is anchored by the pure BPM zero: beat = (ms + 1000)/500.
            self.assertEqual([item["beat"] for item in played], [[0, 1, 2], [2, 3, 4]])
            self.assertEqual(
                [_mc_beat(item["beat"]) for item in played], [0.5, 2.75]
            )
            self.assertEqual(
                [round(_mc_audio_ms(_mc_beat(item["beat"]), data)) for item in played],
                [-750, 375],
            )
            self.expect_message(events, "音乐相对时间", kind="warning")

    def test_a_negative_mc_beat_row_shifts_into_range_and_keeps_its_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "negative.mc", json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4},
                         "song": {"title": "T"}, "version": "Hard"},
                "time": [{"beat": [0, 0, 4], "bpm": 120}],
                "note": [
                    {"beat": [-1, 0, 4], "column": 0},
                    {"beat": [0, 0, 4], "column": 1},
                ],
            }))
            # A .sm row cannot be negative, so beat -1 becomes beat 0 and the
            # beat-0 note moves to beat 1; the 500 ms shift is carried by
            # #OFFSET:0.500000, which keeps both clicks at -500 ms and 0 ms.
            output, _events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            self.assertIn("#OFFSET:0.500000;", text)
            self.assertIn("#BPMS:0.000000=120.000000,1.000000=120.000000;", text)
            self.assertEqual(_sm_cells(text), {(0, 0, 0): "1", (0, 48, 1): "1"})
            self.assertEqual(
                [(note.time, note.column) for note in parse_chart(output)[0].notes],
                [(-500, 0), (0, 1)],
            )


# ---------------------------------------------------------------------------
# FIX-06 -- quantisation is visible and counted
# ---------------------------------------------------------------------------


class GridQuantisationTests(ConversionTestCase):
    """FIX-06: the grid may change a chart, but never silently."""

    def test_a_too_short_hold_is_extended_to_the_smallest_sm_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "short.osu", _osu_chart(
                ["64,192,1995,128,0,1996:0:0:0:0:"],
            ))
            # 1995 ms and 1996 ms are beats 3.99 and 3.992 at 120 BPM; one row
            # of the 192-row measure is 500 / 48 = 10.42 ms, so both ends round
            # onto global row 192 (measure 1, row 0).  A hold may not vanish, so
            # the tail moves to the next row: 193 * 4 / 192 * 500 = 2010.4 ms.
            output, events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            self.assertEqual(_sm_cells(text), {(1, 0, 0): "2", (1, 1, 0): "3"})
            message = self.expect_message(events, "过短长条", kind="log")
            self.assertIn("延长", message)
            self.assertIn("最小一行", message)

            rewritten = parse_chart(output)[0]
            holds = [note for note in rewritten.notes if note.end_time is not None]
            self.assertEqual(len(holds), 1)
            self.assertGreater(holds[0].end_time, holds[0].time)

    def test_a_tap_and_a_hold_head_in_one_cell_keep_the_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "conflict.osu", _osu_chart([
                "64,192,1995,1,0",
                "64,192,1996,128,0,2600:0:0:0:0:",
            ]))
            # Tap (beat 3.99) and hold head (beat 3.992) share column 0 and both
            # land on global row 192 = measure 1 row 0; the tail at 2600 ms is
            # beat 5.2 = global row 249.6 -> row 250, i.e. measure 1 row 58.
            output, events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            self.assertEqual(_sm_cells(text), {(1, 0, 0): "2", (1, 58, 0): "3"})
            message = self.expect_message(events, "同格冲突", kind="warning")
            self.assertIn("丢弃", message)
            self.assertIn("普通→长条头 1 处", message)
            self.assertIn("不做移动", message)

    def test_two_taps_one_millisecond_apart_report_one_cell_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "twotaps.osu", _osu_chart([
                "64,192,1995,1,0",
                "64,192,1996,1,0",
            ]))
            output, events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            # Both taps land on the same row (192 = measure 1 row 0); exactly one
            # marker survives and the loss is counted as one conflict.
            self.assertEqual(_sm_cells(text), {(1, 0, 0): "1"})
            message = self.expect_message(events, "同格冲突", kind="warning")
            self.assertIn("普通→普通 1 处", message)

    def test_measure_boundary_notes_and_a_cross_measure_hold_keep_their_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "boundary.osu", _osu_chart([
                "64,192,2000,1,0",
                "64,192,4000,1,0",
                "192,192,1750,128,0,2250:0:0:0:0:",
            ]))
            # 120 BPM puts a measure boundary every 2000 ms (beat 4) and half a
            # measure every 1000 ms: 1750 ms is beat 3.5 (row 168), 2000 ms is
            # beat 4 (measure 1 row 0), 2250 ms is beat 4.5 (measure 1 row 24)
            # and 4000 ms is beat 8 (measure 2 row 0).  The hold therefore pairs
            # its head and tail across the measure boundary.
            output, events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            self.assertEqual(_sm_cells(text), {
                (0, 168, 1): "2",
                (1, 0, 0): "1",
                (1, 24, 1): "3",
                (2, 0, 0): "1",
            })
            self.assertEqual(
                [
                    message for message in self.messages(events)
                    if "同格冲突" in message or "过短长条" in message
                ],
                [],
            )
            rewritten = parse_chart(output)[0]
            self.assertEqual(
                sorted(
                    (note.time, note.column, note.end_time)
                    for note in rewritten.notes
                ),
                [(1750, 1, 2250), (2000, 0, None), (4000, 0, None)],
            )

    def test_a_short_hold_collapsing_on_the_mc_grid_loses_its_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 30 BPM (2000 ms per beat) makes the smallest positive fraction (1/192) span 10.42 ms, so a
            # 1 ms hold cannot be expressed: 1995 ms is beat 0.9975 and 1996 ms
            # is beat 0.998, and both round to the rational 1/1 (399/400 and
            # 499/500 have no denominator <= 192 that is closer).  A tail that
            # lands on its head would erase the hold, so it is dropped.
            source = self.fixture(root, "collapse.osu", _osu_chart(
                ["64,192,1995,128,0,1996:0:0:0:0:"],
                timing_points=("0,2000,4,1,0,100,1,0",),
            ))
            output, events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            played = _mc_played_notes(data)
            self.assertEqual(len(played), 1)
            self.assertEqual(played[0]["beat"], [1, 0, 4])
            self.assertNotIn("endbeat", played[0])
            message = self.expect_message(events, "长条尾部在 .mc 拍位近似后与头部重合")
            self.assertIn("已丢失", message)

    def test_representable_notes_convert_without_a_precision_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Whole and half beats at 120 BPM: 0, 250, 500, 750 and 1000 ms.
            source = self.fixture(root, "grid.osu", _osu_chart([
                "64,192,0,1,0",
                "192,192,250,1,0",
                "320,192,500,128,0,1000:0:0:0:0:",
                "448,192,750,1,0",
                "64,192,1000,1,0",
            ]))

            sm_output, sm_events = self.convert(root, source, ".sm", "sm")
            sm_text = sm_output.read_text(encoding="utf-8")
            # Exact rows: beat = ms / 500, and 48 rows per beat.
            self.assertEqual(_sm_cells(sm_text), {
                (0, 0, 0): "1",
                (0, 24, 1): "1",
                (0, 48, 2): "2",
                (0, 72, 3): "1",
                (0, 96, 0): "1",
                (0, 96, 2): "3",
            })
            self.assertEqual(
                [
                    message for message in self.messages(sm_events)
                    if "精度" in message or "无法精确表达" in message or "同格冲突" in message
                ],
                [],
            )

            mc_output, mc_events = self.convert(root, source, ".mc", "mc")
            data = json.loads(mc_output.read_text(encoding="utf-8"))
            played = _mc_played_notes(data)
            # 250 ms steps are quarters of a beat: 0, 0.5, 1, 1.5 and 2.
            self.assertEqual(
                [_mc_beat(item["beat"]) for item in played],
                [0.0, 0.5, 1.0, 1.5, 2.0],
            )
            hold = next(item for item in played if "endbeat" in item)
            self.assertEqual((hold["beat"], hold["endbeat"]), ([1, 0, 4], [2, 0, 4]))
            self.assertEqual(
                [round(_mc_audio_ms(_mc_beat(item["beat"]), data)) for item in played],
                [0, 250, 500, 750, 1000],
            )
            self.assertEqual(
                [
                    message for message in self.messages(mc_events)
                    if "精度" in message or "无法精确表达" in message
                ],
                [],
            )

    def test_a_note_after_a_stop_keeps_its_sm_beat(self):
        """A stop must not be read back as beat progress on the SM grid.

        With ``#BPMS:0=120,4=240`` and ``#STOPS:2=0.250`` the note on beat 3 is
        at 1500 + 250 = 1750 ms and belongs on row 144 of measure 0 -- a row the
        192-row grid expresses exactly, so no time may be lost.  Inverting the
        written milliseconds back to a beat must not convert the stop's pause
        into beat progress.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "stoplane.sm", _sm_chart(
                "1000\n0100\n0010\n0001\n,\n1000\n0100\n0010\n0001",
                bpms="0.000000=120.000000,4.000000=240.000000",
                stops="2.000000=0.250000",
            ))
            output, _events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            # The stop is native to .sm: every beat keeps its own row, so the
            # eight taps stay on rows 0/48/96/144 of both measures.
            self.assertEqual(_sm_cells(text), {
                (0, 0, 0): "1",
                (0, 48, 1): "1",
                (0, 96, 2): "1",
                (0, 144, 3): "1",
                (1, 0, 0): "1",
                (1, 48, 1): "1",
                (1, 96, 2): "1",
                (1, 144, 3): "1",
            })


# ---------------------------------------------------------------------------
# M3 -- scroll-velocity migration
# ---------------------------------------------------------------------------


class ScrollVelocityMigrationTests(ConversionTestCase):
    """M3: osu! inherited points <-> Malody ``effect`` -> StepMania drop."""

    def _sv_source(self, root, timing_points=(
        "0,500,4,1,0,100,1,0",
        "750,-50,4,2,3,80,0,1",
    ), objects=("64,192,0,1,0", "192,192,750,1,0")):
        return self.fixture(
            root, "sv.osu", _osu_chart(list(objects), timing_points=timing_points)
        )

    def test_inherited_timing_point_fields_survive_an_osu_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root)
            # The multiplier is -100 / beatLength = -100 / -50 = 2, and the
            # sample set, sample index, volume and kiai flag ride on the same
            # record, so a same-format rewrite must emit the line unchanged.
            output, _events = self.convert(root, source, ".osu", "osu")
            self.assertEqual(_osu_section(output.read_text(encoding="utf-8"), "TimingPoints"), [
                "0.000,500,4,1,0,100,1,0",
                "750.000,-50,4,2,3,80,0,1",
            ])

    def test_scroll_changes_become_mc_effects_and_return_as_inherited_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root)
            plan = plan_conversion(parse_chart(source)[0], ".mc")
            self.assertEqual(plan.strategy_for("sv"), ("full", "scroll-as-sv"))

            output, _events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            # 750 ms is 750 / 500 = 1.5 beats at 120 BPM.
            self.assertEqual(_mc_effects(data), [{"beat": [1, 1, 2], "scroll": 2.0}])
            self.assertEqual(
                [_mc_beat(item["beat"]) for item in _mc_effects(data)], [1.5]
            )

            back_root = Path(directory) / "back"
            back_root.mkdir()
            back = Path(convert_paths([output], ".osu", back_root)[0])
            lines = _osu_section(back.read_text(encoding="utf-8"), "TimingPoints")
            self.assertEqual(lines, [
                "0.000,500,4,1,0,100,1,0",
                "750.000,-50,4,1,0,100,0,0",
            ])

    def test_scroll_changes_are_dropped_with_a_diagnostic_for_sm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root)
            plan = plan_conversion(parse_chart(source)[0], ".sm")
            self.assertEqual(plan.strategy_for("sv"), ("downgrade", "drop-scroll"))

            output, events = self.convert(root, source, ".sm", "sm")
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("scroll", text.casefold())
            self.assertNotIn("#SV", text)
            self.expect_message(events, "滚动速度", kind="warning")
            # The clicks are untouched: 0 ms and 750 ms are beats 0 and 1.5.
            self.assertEqual(_sm_cells(text), {(0, 0, 0): "1", (0, 72, 1): "1"})
            self.assertEqual(
                [(note.time, note.column) for note in parse_chart(output)[0].notes],
                [(0, 0), (750, 1)],
            )

    def test_bpm_changes_leave_the_scroll_multiplier_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root, timing_points=(
                "0,500,4,1,0,100,1,0",
                "2000,250,4,1,0,100,1,0",
                "3000,-50,4,1,0,100,0,0",
            ), objects=("64,192,0,1,0", "64,192,2000,1,0"))
            output, _events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            # 0 ms is beat 0 and 2000 ms is beat (2000 - 0) / 500 = 4; the
            # 240 BPM segment then puts 3000 ms at 4 + 1000 / 250 = 8 beats.
            self.assertEqual(
                [(_mc_beat(item["beat"]), float(item["bpm"])) for item in data["time"]],
                [(0.0, 120.0), (4.0, 240.0)],
            )
            # -100 / -50 = 2 keeps its own multiplier on its own beat.
            self.assertEqual(_mc_effects(data), [{"beat": [8, 0, 4], "scroll": 2.0}])

    def test_several_scroll_controls_leave_the_bpm_map_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root, timing_points=(
                "0,500,4,1,0,100,1,0",
                "2000,250,4,1,0,100,1,0",
                "3000,-50,4,1,0,100,0,0",
                "3500,-200,4,1,0,100,0,0",
            ), objects=("64,192,0,1,0", "64,192,2000,1,0"))
            output, _events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                [(_mc_beat(item["beat"]), float(item["bpm"])) for item in data["time"]],
                [(0.0, 120.0), (4.0, 240.0)],
            )
            # 3000 ms -> beat 8 (multiplier -100 / -50 = 2) and
            # 3500 ms -> 4 + 1500 / 250 = 10 (multiplier -100 / -200 = 0.5).
            self.assertEqual(_mc_effects(data), [
                {"beat": [8, 0, 4], "scroll": 2.0},
                {"beat": [10, 0, 4], "scroll": 0.5},
            ])

    def test_a_bpm_change_and_a_scroll_control_can_share_one_beat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root, timing_points=(
                "0,500,4,1,0,100,1,0",
                "1000,250,4,1,0,100,1,0",
                "1000,-100,4,1,0,100,0,0",
            ), objects=("64,192,0,1,0", "64,192,1000,1,0"))
            output, _events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            # 1000 ms is beat 2 for both controls, and -100 / -100 = 1.
            self.assertEqual(
                [(_mc_beat(item["beat"]), float(item["bpm"])) for item in data["time"]],
                [(0.0, 120.0), (2.0, 240.0)],
            )
            self.assertEqual(_mc_effects(data), [{"beat": [2, 0, 4], "scroll": 1.0}])

    def test_a_scroll_back_to_default_stays_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._sv_source(root, timing_points=(
                "0,500,4,1,0,100,1,0",
                "750,-50,4,2,3,80,0,1",
                "1250,-100,4,2,3,80,0,1",
            ), objects=("64,192,0,1,0", "64,192,750,1,0"))
            output, _events = self.convert(root, source, ".mc", "mc")
            data = json.loads(output.read_text(encoding="utf-8"))
            # -100 / -50 = 2 at 750 ms (beat 1.5) and -100 / -100 = 1 at
            # 1250 ms (beat 2.5): a return to the default speed is still an
            # explicit control, not an omission.
            self.assertEqual(_mc_effects(data), [
                {"beat": [1, 1, 2], "scroll": 2.0},
                {"beat": [2, 1, 2], "scroll": 1.0},
            ])
            back_root = Path(directory) / "back"
            back_root.mkdir()
            back = Path(convert_paths([output], ".osu", back_root)[0])
            self.assertEqual(_osu_section(back.read_text(encoding="utf-8"), "TimingPoints"), [
                "0.000,500,4,1,0,100,1,0",
                "750.000,-50,4,1,0,100,0,0",
                "1250.000,-100,4,1,0,100,0,0",
            ])

    def test_non_positive_scroll_speeds_are_dropped_for_osu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root, "nonpositive.mc", json.dumps({
                "meta": {"mode": 0, "mode_ext": {"column": 4},
                         "song": {"title": "T"}, "version": "Hard"},
                "time": [{"beat": [0, 0, 4], "bpm": 120}],
                "effect": [
                    {"beat": [1, 0, 4], "scroll": 0},
                    {"beat": [2, 0, 4], "scroll": -0.5},
                    {"beat": [3, 0, 4], "scroll": 1.5},
                ],
                "note": [
                    {"beat": [0, 0, 4], "column": 0},
                    {"beat": [2, 0, 4], "column": 1},
                ],
            }))
            output, events = self.convert(root, source, ".osu", "osu")
            # An osu! inherited point only expresses a positive multiplier; the
            # zero and negative controls have no representation and are counted.
            message = self.expect_message(events, "非正滚动速度", kind="warning")
            self.assertIn("2 个", message)
            self.assertEqual(_osu_section(output.read_text(encoding="utf-8"), "TimingPoints"), [
                "0.000,500,4,1,0,100,1,0",
                "1500.000,-66.6666666667,4,1,0,100,0,0",
            ])
            # -100 / 1.5 = -66.67 at 1500 ms; the clicks keep their own times.
            self.assertEqual(
                [item[0] for item in _osu_hit_objects(output.read_text(encoding="utf-8"))],
                [0, 1000],
            )

    def test_a_scroll_and_a_pause_share_one_beat_grid_in_mc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # No source format carries both a StepMania pause and an osu! scroll
            # change, so the two features are merged onto one document: the
            # clicks come from the stop/delay fixture (0/500/1750/2250 ms) and
            # the scroll control is anchored at 500 ms.
            source = self.fixture(root, "combined.sm", _sm_chart(
                "1000\n0100\n0010\n0001",
                stops="1.000000=0.500000",
                delays="2.000000=0.250000",
            ))
            document = parse_chart(source)[0]
            document.scrolls.append(
                ChartScrollEvent(time=500.0, multiplier=2.0, beat=1.0)
            )
            output = Path(directory) / "combined.mc"
            write_chart(document, output, ".mc")

            data = json.loads(output.read_text(encoding="utf-8"))
            played = _mc_played_notes(data)
            # The effect and the clicks are remapped onto the same pure BPM grid:
            # the pause is expressed only by the note beats, so 500 ms is beat 1
            # for the scroll control and for the click that happens there.
            self.assertEqual(_mc_effects(data), [{"beat": [1, 0, 4], "scroll": 2.0}])
            self.assertEqual(
                [_mc_beat(item["beat"]) for item in played], [0.0, 1.0, 3.5, 4.5]
            )
            self.assertEqual(
                [round(_mc_audio_ms(_mc_beat(item["beat"]), data)) for item in played],
                [0, 500, 1750, 2250],
            )
            self.assertEqual(
                round(_mc_audio_ms(_mc_beat(_mc_effects(data)[0]["beat"]), data)), 500
            )


class TimingSampleFieldTests(unittest.TestCase):
    """Sample/volume/kiai fields are modelled separately from SV and BPM."""

    def _chart(self, root, name="samples.osu"):
        source = root / name
        source.write_text(
            "osu file format v14\n[General]\nAudioFilename: song.mp3\nMode: 3\n"
            "[Metadata]\nTitle: T\nVersion: Hard\n[Difficulty]\nCircleSize: 4\n"
            "[TimingPoints]\n"
            # Red point with sample set 2, index 5, volume 80 and kiai on.
            "0,500,3,2,5,80,1,1\n"
            # Plain red point with defaults.
            "1000,250,4,1,0,100,1,0\n"
            # Green point (SV x2) whose own volume is 60.
            "1500,-50,4,3,0,60,0,0\n"
            "[HitObjects]\n64,192,0,1,0\n",
            encoding="utf-8",
        )
        return source

    def test_red_and_green_sample_fields_survive_an_osu_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._chart(root)
            output = Path(convert_paths([source], ".osu", root)[0])
            timing_lines = [
                line for line in output.read_text(encoding="utf-8").splitlines()
                if line and line[0].isdigit() and ",192," not in line
            ]
            self.assertEqual(
                timing_lines,
                ["0.000,500,3,2,5,80,1,1", "1000.000,250,4,1,0,100,1,0", "1500.000,-50,4,3,0,60,0,0"],
            )

    def test_sample_changes_are_reported_when_the_target_cannot_carry_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._chart(root)
            for target in (".mc", ".sm"):
                with self.subTest(target=target):
                    events = []
                    convert_paths(
                        [source], target, root,
                        emit_event=lambda kind, payload: events.append((kind, payload)),
                    )
                    warnings = [payload for kind, payload in events if kind == "warning"]
                    matches = [item for item in warnings if "音量/采样组/kiai" in item]
                    self.assertEqual(len(matches), 1, warnings)
                    self.assertIn("2 个时间点", matches[0])

    def test_default_sample_fields_produce_no_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "plain.osu"
            source.write_text(
                "osu file format v14\n[General]\nMode: 3\n[Metadata]\nTitle: T\n"
                "[Difficulty]\nCircleSize: 4\n[TimingPoints]\n0,500,4,1,0,100,1,0\n"
                "[HitObjects]\n64,192,0,1,0\n",
                encoding="utf-8",
            )
            events = []
            convert_paths(
                [source], ".mc", root,
                emit_event=lambda kind, payload: events.append((kind, payload)),
            )
            self.assertFalse(
                any("音量/采样组/kiai" in str(payload) for _kind, payload in events),
                events,
            )


if __name__ == "__main__":
    unittest.main()
