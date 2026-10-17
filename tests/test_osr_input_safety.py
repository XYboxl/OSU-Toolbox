"""Replay header widths and truncation must follow the binary format."""
import lzma
import struct
import tempfile
import unittest
from pathlib import Path

from osu_toolbox.parsing.osr_file import OsrReader, parse_osr_file


class OsrInputSafetyTests(unittest.TestCase):
    def test_large_judgement_counts_and_combo_are_unsigned(self):
        compressed = lzma.compress(b"0|0|0|0,")
        counts = (40000, 33000, 65535, 36000, 34000, 35000)
        blob = (struct.pack("<Bi", 3, 20261007) + b"\0\0\0" +
                struct.pack("<6HiHBi", *counts, 1000000, 50000, 1, 0) +
                b"\0" + struct.pack("<qi", 0, len(compressed)) + compressed +
                struct.pack("<q", 123))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.osr"
            path.write_bytes(blob)
            replay = parse_osr_file(path)
        self.assertEqual((replay.count_300, replay.count_100, replay.count_50,
                          replay.count_geki, replay.count_katu, replay.count_miss), counts)
        self.assertEqual(replay.max_combo, 50000)

    def test_truncated_headers_and_invalid_byte_lengths_report_value_errors(self):
        for operation in (lambda: OsrReader(b"\1").read("i"),
                          lambda: OsrReader(b"abc").read_bytes(4),
                          lambda: OsrReader(b"abc").read_bytes(-1),
                          lambda: OsrReader(b"\x0b\x04abc").read_string(),
                          lambda: OsrReader(b"\x0b" + b"\x80" * 12).read_string()):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()
