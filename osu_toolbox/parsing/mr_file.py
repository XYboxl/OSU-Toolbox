"""Malody ``.mr`` replay parsing, editing, and perfect replay generation.

The format is intentionally implemented with the standard library only.  The
reader is strict about record boundaries but tolerant of unknown bytes so files
written by different Malody versions can be round-tripped without losing data.
"""

import hashlib
import json
import math
import os
import struct
import tempfile
import time
from dataclasses import dataclass, field


MR_HEADER = "mr format head"
MR_DATA_HEADER = "mr data"
MALODY_VERSION = b"\x07\x03\x04\x00"

# Malody replay MOD bit flags.  Keep the names aligned with the format
# reference so the application can expose choices without asking for a mask.
MOD_AUTO = 1
MOD_LUCK = 2
MOD_FLIP = 4
MOD_CONST = 8
MOD_DASH = 16
MOD_RUSH = 32
MOD_HIDE = 64
MOD_ORIGIN = 128
MOD_SLOW = 256
MOD_DEATH = 512

MR_MODS = (
    ("MOD_AUTO", MOD_AUTO, "Auto"),
    ("MOD_LUCK", MOD_LUCK, "Luck"),
    ("MOD_FLIP", MOD_FLIP, "Flip"),
    ("MOD_CONST", MOD_CONST, "Const"),
    ("MOD_DASH", MOD_DASH, "Dash"),
    ("MOD_RUSH", MOD_RUSH, "Rush"),
    ("MOD_HIDE", MOD_HIDE, "Hide"),
    ("MOD_ORIGIN", MOD_ORIGIN, "Origin"),
    ("MOD_SLOW", MOD_SLOW, "Slow"),
    ("MOD_DEATH", MOD_DEATH, "Death"),
)


@dataclass
class MrEvent:
    """One Malody replay event (1 = key down, 2 = key up)."""

    time: int
    event: int
    column: int

    def __post_init__(self):
        self.time = int(self.time)
        self.event = int(self.event)
        self.column = int(self.column)
        if not -2147483648 <= self.time <= 2147483647:
            raise ValueError(".mr 事件时间必须在有符号 32 位整数范围内")
        if not 0 <= self.event <= 255:
            raise ValueError(".mr 事件类型必须在无符号字节范围内")
        if not 0 <= self.column <= 255:
            raise ValueError(".mr 事件列号必须在无符号字节范围内")

    def as_dict(self):
        return {"time": self.time, "event": self.event, "column": self.column}


@dataclass
class ChartNote:
    time: int
    column: int
    end_time: int = None

    def __post_init__(self):
        self.time = int(self.time)
        self.column = int(self.column)
        if self.end_time is not None:
            self.end_time = int(self.end_time)


@dataclass
class ChartData:
    notes: list
    chart_version: str = ""
    music: str = ""
    artist: str = ""
    key_count: int = 4


@dataclass
class MrReplay:
    """Decoded ``.mr`` file with safe editing helpers."""

    md5: str = ""
    chart_version: str = ""
    music: str = ""
    artist: str = ""
    score: int = 0
    max_combo: int = 0
    best: int = 0
    cool: int = 0
    good: int = 0
    miss: int = 0
    mod: int = 0
    judge: int = 0
    playing_time: int = 0
    events: list = field(default_factory=list)
    malody_version: bytes = MALODY_VERSION
    data_version: bytes = None
    unknown_score: bytes = b"\x00\x00\x00\x00"
    unknown_2: bytes = b"\x00"
    unknown_3: bytes = b"\x00\x00\x00\x00"
    header: str = MR_HEADER
    data_header: str = MR_DATA_HEADER

    def __post_init__(self):
        self.events = [_coerce_event(event) for event in self.events]
        if len(self.malody_version) != 4:
            raise ValueError("Malody 版本字段必须包含 4 个字节")
        if self.data_version is None:
            self.data_version = self.malody_version
        if len(self.data_version) != 4:
            raise ValueError("数据版本字段必须包含 4 个字节")
        for name in ("unknown_score", "unknown_3"):
            value = getattr(self, name)
            if len(value) != 4:
                raise ValueError(f"{name} 必须包含 4 个字节")
        if len(self.unknown_2) != 1:
            raise ValueError("unknown_2 必须包含 1 个字节")

    @property
    def total(self):
        return len(self.events) + 1

    @property
    def MD5(self):  # Compatibility with the early recordmaker API.
        return self.md5

    @MD5.setter
    def MD5(self, value):
        self.md5 = str(value or "")

    @property
    def HEAD(self):
        return self.header

    @HEAD.setter
    def HEAD(self, value):
        self.header = str(value or "")

    @property
    def MALODY_VERSION(self):
        return self.malody_version

    @MALODY_VERSION.setter
    def MALODY_VERSION(self, value):
        value = bytes(value)
        if len(value) != 4:
            raise ValueError("Malody 版本字段必须包含 4 个字节")
        self.malody_version = value

    @property
    def TOTAL(self):
        return self.total

    @property
    def PLAYINGTIME(self):
        return self.playing_time

    @PLAYINGTIME.setter
    def PLAYINGTIME(self, value):
        self.playing_time = int(value)

    @property
    def CAHRT_VERSION(self):
        return self.chart_version

    @CAHRT_VERSION.setter
    def CAHRT_VERSION(self, value):
        self.chart_version = str(value or "")

    @property
    def SCORE(self):
        return self.score

    @SCORE.setter
    def SCORE(self, value):
        self.score = int(value)

    @property
    def MAXCOMBO(self):
        return self.max_combo

    @MAXCOMBO.setter
    def MAXCOMBO(self, value):
        self.max_combo = int(value)

    @property
    def BEST(self):
        return self.best

    @BEST.setter
    def BEST(self, value):
        self.best = int(value)

    @property
    def COOL(self):
        return self.cool

    @COOL.setter
    def COOL(self, value):
        self.cool = int(value)

    @property
    def GOOD(self):
        return self.good

    @GOOD.setter
    def GOOD(self, value):
        self.good = int(value)

    @property
    def MISS(self):
        return self.miss

    @MISS.setter
    def MISS(self, value):
        self.miss = int(value)

    @property
    def MOD(self):
        return self.mod

    @MOD.setter
    def MOD(self, value):
        self.mod = int(value)

    @property
    def JUDGE(self):
        return self.judge

    @JUDGE.setter
    def JUDGE(self, value):
        self.judge = int(value)

    @property
    def CHART(self):
        return [event.as_dict() for event in self.events]

    @CHART.setter
    def CHART(self, value):
        self.events = [_coerce_event(event) for event in (value or ())]
        self.events.sort(key=lambda item: (item.time, 0 if item.event == 1 else 1, item.column))

    @property
    def MUSIC(self):
        return self.music

    @MUSIC.setter
    def MUSIC(self, value):
        self.music = str(value or "")

    @property
    def ARTIST(self):
        return self.artist

    @ARTIST.setter
    def ARTIST(self, value):
        self.artist = str(value or "")

    @classmethod
    def open(cls, path):
        return cls.read(path)

    @classmethod
    def read(cls, path):
        with open(path, "rb") as stream:
            data = stream.read()
        reader = _BinaryReader(data)
        header = reader.read_string()
        version = reader.read_bytes(4)
        md5 = reader.read_string()
        chart_version = reader.read_string()
        music = reader.read_string()
        artist = reader.read_string()
        score = reader.read_i32()
        max_combo = reader.read_i32()
        best = reader.read_i32()
        cool = reader.read_i32()
        good = reader.read_i32()
        miss = reader.read_i32()
        unknown_score = reader.read_bytes(4)
        mod = reader.read_i32()
        judge = reader.read_i32()
        data_header = reader.read_string()
        data_version = reader.read_bytes(4)
        total = reader.read_i32()
        unknown_2 = reader.read_bytes(1)
        playing_time = reader.read_i32()
        unknown_3 = reader.read_bytes(4)
        if total < 0:
            raise ValueError(".mr 事件总数不能为负数")
        remaining = len(data) - reader.pos
        if remaining % 6:
            raise ValueError(".mr 事件记录不完整")
        events = []
        for _ in range(remaining // 6):
            events.append(MrEvent(reader.read_i32(), reader.read_u8(), reader.read_u8()))
        return cls(
            md5=md5,
            chart_version=chart_version,
            music=music,
            artist=artist,
            score=score,
            max_combo=max_combo,
            best=best,
            cool=cool,
            good=good,
            miss=miss,
            mod=mod,
            judge=judge,
            playing_time=playing_time,
            events=events,
            malody_version=version,
            data_version=data_version,
            unknown_score=unknown_score,
            unknown_2=unknown_2,
            unknown_3=unknown_3,
            header=header,
            data_header=data_header,
        )

    def write(self, path):
        writer = _BinaryWriter()
        writer.write_string(self.header)
        writer.write_bytes(self.malody_version)
        writer.write_string(self.md5)
        writer.write_string(self.chart_version)
        writer.write_string(self.music)
        writer.write_string(self.artist)
        writer.write_i32(self.score)
        writer.write_i32(self.max_combo)
        writer.write_i32(self.best)
        writer.write_i32(self.cool)
        writer.write_i32(self.good)
        writer.write_i32(self.miss)
        writer.write_bytes(self.unknown_score)
        writer.write_i32(self.mod)
        writer.write_i32(self.judge)
        writer.write_string(self.data_header)
        writer.write_bytes(self.data_version)
        writer.write_i32(self.total)
        writer.write_bytes(self.unknown_2)
        writer.write_i32(self.playing_time)
        writer.write_bytes(self.unknown_3)
        for event in self.events:
            writer.write_i32(event.time)
            writer.write_u8(event.event)
            writer.write_u8(event.column)
        with open(path, "wb") as stream:
            stream.write(writer.data)
        return path

    def write_atomic(self, path, *, overwrite=False):
        """Write through a validated same-directory temporary file."""
        return atomic_write_replay(self, path, overwrite=overwrite)

    def scale(self, rate):
        """Change playback speed in-place; ``rate=2`` plays twice as fast."""
        rate = _positive_rate(rate)
        for event in self.events:
            event.time = int(event.time / rate)
        self.events.sort(key=lambda item: (item.time, 0 if item.event == 1 else 1, item.column))
        return self

    def scaled(self, rate):
        copy = self.clone()
        return copy.scale(rate)

    def update_metadata(self, *, md5=None, chart_version=None, music=None, artist=None):
        if md5 is not None:
            md5 = str(md5).strip().lower()
            if len(md5) != 32 or any(char not in "0123456789abcdef" for char in md5):
                raise ValueError("谱面 MD5 必须是 32 位十六进制字符串")
            self.md5 = md5
        if chart_version is not None:
            self.chart_version = str(chart_version)
        if music is not None:
            self.music = str(music)
        if artist is not None:
            self.artist = str(artist)
        return self

    def clone(self):
        return MrReplay(
            md5=self.md5,
            chart_version=self.chart_version,
            music=self.music,
            artist=self.artist,
            score=self.score,
            max_combo=self.max_combo,
            best=self.best,
            cool=self.cool,
            good=self.good,
            miss=self.miss,
            mod=self.mod,
            judge=self.judge,
            playing_time=self.playing_time,
            events=[MrEvent(e.time, e.event, e.column) for e in self.events],
            malody_version=self.malody_version,
            data_version=self.data_version,
            unknown_score=self.unknown_score,
            unknown_2=self.unknown_2,
            unknown_3=self.unknown_3,
            header=self.header,
            data_header=self.data_header,
        )

    # Early recordmaker method names are kept as small, corrected wrappers.
    def addMod(self, mod):
        self.mod |= int(mod)

    def minsMod(self, mod):
        self.mod &= ~int(mod)

    def rate(self, rate):
        return self.scale(rate)


MRFile = MrReplay


class _BinaryReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read_bytes(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError(".mr 文件意外结束")
        value = self.data[self.pos:self.pos + count]
        self.pos += count
        return value

    def read_i32(self):
        return struct.unpack("<i", self.read_bytes(4))[0]

    def read_u8(self):
        return self.read_bytes(1)[0]

    def read_string(self):
        length = self.read_i32()
        if length < 0:
            raise ValueError(".mr 字符串长度不能为负数")
        return self.read_bytes(length).decode("utf-8", errors="replace")


class _BinaryWriter:
    def __init__(self):
        self.data = bytearray()

    def write_bytes(self, value):
        self.data.extend(value)

    def write_i32(self, value):
        self.data.extend(struct.pack("<i", int(value)))

    def write_u8(self, value):
        self.data.extend(struct.pack("<B", int(value)))

    def write_string(self, value):
        encoded = str(value or "").encode("utf-8")
        self.write_i32(len(encoded))
        self.write_bytes(encoded)


def _positive_rate(value):
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError("播放速度必须是大于 0 的数字")
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("播放速度必须是大于 0 的数字")
    return rate


def _coerce_event(value):
    if isinstance(value, MrEvent):
        return value
    if isinstance(value, dict):
        return MrEvent(value.get("time", 0), value.get("event", 0), value.get("column", 0))
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return MrEvent(value[0], value[1], value[2])
    raise TypeError(".mr 事件必须是 MrEvent 或事件映射")


def _md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _beat_value(raw):
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"无效的 beat 值：{raw!r}")
    values = [float(item) for item in raw]
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0] + values[1] / 4.0
    denominator = values[2] or 1.0
    return values[0] + values[1] / denominator


def _mc_chart(data):
    if not isinstance(data, dict):
        raise ValueError(".mc 谱面的根节点必须是对象")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    raw_mode = meta.get("mode")
    try:
        mode = float(raw_mode)
    except (TypeError, ValueError):
        mode = math.nan
    if isinstance(raw_mode, bool) or not math.isfinite(mode) or mode != 0:
        raise ValueError("回放生成只支持 Malody Key 谱面（meta.mode=0）")
    song = meta.get("song") if isinstance(meta.get("song"), dict) else {}
    notes_raw = data.get("note") or data.get("notes") or []
    if not isinstance(notes_raw, list):
        raise ValueError(".mc 谱面的 note 字段必须是数组")
    timing_raw = data.get("time") or data.get("timing") or []
    changes = []
    for item in timing_raw if isinstance(timing_raw, list) else ():
        if not isinstance(item, dict):
            continue
        try:
            beat = _beat_value(item.get("beat", 0))
            bpm = float(item.get("bpm", 120))
        except (TypeError, ValueError):
            continue
        if math.isfinite(beat) and math.isfinite(bpm) and bpm > 0:
            changes.append((beat, bpm))
    changes.sort(key=lambda item: item[0])
    if not changes:
        changes = [(0.0, 120.0)]
    segments = []
    current_ms = 0.0
    previous_beat, previous_bpm = changes[0]
    if previous_beat != 0:
        current_ms = previous_beat * 60000.0 / previous_bpm
    segments.append((previous_beat, current_ms, previous_bpm))
    for beat, bpm in changes[1:]:
        current_ms += (beat - previous_beat) * 60000.0 / previous_bpm
        segments.append((beat, current_ms, bpm))
        previous_beat, previous_bpm = beat, bpm

    def beat_to_ms(raw):
        beat = _beat_value(raw)
        segment = segments[0]
        for candidate in segments[1:]:
            if candidate[0] > beat:
                break
            segment = candidate
        return int(round(segment[1] + (beat - segment[0]) * 60000.0 / segment[2]))

    parsed = []
    max_column = -1
    for raw in notes_raw:
        if not isinstance(raw, dict) or "column" not in raw:
            continue
        try:
            column = int(raw["column"])
            if "time" in raw:
                start = int(round(float(raw["time"])))
            else:
                start = beat_to_ms(raw.get("beat", 0))
            end = None
            if "end_time" in raw:
                end = int(round(float(raw["end_time"])))
            elif "endbeat" in raw:
                end = beat_to_ms(raw["endbeat"])
        except (TypeError, ValueError, OverflowError):
            continue
        if end is not None and end <= start:
            end = None
        if column < 0:
            continue
        max_column = max(max_column, column)
        parsed.append(ChartNote(start, column, end))
    mode_ext = meta.get("mode_ext") if isinstance(meta.get("mode_ext"), dict) else {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    try:
        key_count = int(mode_ext.get("column", info.get("key", 0)))
    except (TypeError, ValueError, OverflowError):
        key_count = 0
    inferred_keys = key_count if 1 <= key_count <= 18 else max_column + 1
    key_count = max(4 if inferred_keys < 1 else inferred_keys, max_column + 1, 1)
    parsed = [ChartNote(item.time, min(key_count - 1, item.column), item.end_time) for item in parsed]
    parsed.sort(key=lambda item: (item.time, item.column, item.end_time or item.time))
    return ChartData(
        notes=parsed,
        chart_version=str(meta.get("version") or meta.get("difficulty") or ""),
        music=str(song.get("title") or meta.get("title") or ""),
        artist=str(song.get("artist") or meta.get("artist") or ""),
        key_count=key_count,
    )


def _osu_chart(path):
    from .osu_file import parse_osu_file

    beatmap = parse_osu_file(path)
    if beatmap.mode != 3:
        raise ValueError("回放生成只支持 osu!mania 谱面（Mode=3）")
    key_count = max(1, int(round(beatmap.circle_size)))
    notes = [ChartNote(item.time, min(key_count - 1, max(0, item.col)), item.end_time if item.is_hold else None)
             for item in beatmap.hit_objects]
    return ChartData(notes, beatmap.version, beatmap.title, beatmap.artist, key_count)


def load_chart(path):
    path = os.path.abspath(os.fspath(path))
    extension = os.path.splitext(path)[1].lower()
    if extension == ".osu":
        return _osu_chart(path)
    if extension == ".mc":
        with open(path, "r", encoding="utf-8-sig", errors="replace") as stream:
            return _mc_chart(json.load(stream))
    raise ValueError("只支持 .osu 和 .mc 谱面")


def generate_perfect_replay(chart_path, output_path=None):
    chart_path = os.path.abspath(os.fspath(chart_path))
    chart = load_chart(chart_path)
    events = []
    for note in chart.notes:
        events.append(MrEvent(note.time, 1, note.column))
        events.append(MrEvent(note.end_time if note.end_time is not None else note.time, 2, note.column))
    events.sort(key=lambda item: (item.time, 0 if item.event == 1 else 1, item.column))
    replay = MrReplay(
        md5=_md5(chart_path),
        chart_version=chart.chart_version,
        music=chart.music,
        artist=chart.artist,
        score=len(chart.notes) * 2000,
        max_combo=len(chart.notes),
        best=len(chart.notes),
        mod=1,
        judge=0,
        playing_time=int(time.time()),
        events=events,
    )
    if output_path is not None:
        atomic_write_replay(replay, output_path, overwrite=True)
    return replay


def edit_replay_metadata(replay_path, output_path=None, *, chart_path=None,
                         chart_version=None, music=None, artist=None):
    replay = MrReplay.read(replay_path)
    if chart_path:
        extension = os.path.splitext(os.fspath(chart_path))[1].lower()
        if extension not in (".osu", ".mc"):
            raise ValueError("chart_path 必须指向原始 .osu 或 .mc 文件")
    md5 = _md5(chart_path) if chart_path else None
    replay.update_metadata(md5=md5, chart_version=chart_version, music=music, artist=artist)
    if output_path is not None:
        atomic_write_replay(replay, output_path, overwrite=True)
    return replay


def scale_replay(replay_path, output_path=None, rate=1.0):
    replay = MrReplay.read(replay_path).scale(rate)
    if output_path is not None:
        atomic_write_replay(replay, output_path, overwrite=True)
    return replay


def atomic_write_replay(replay, path, *, overwrite=False):
    """Serialize a replay through a validated same-directory temp file.

    The destination is left untouched when serialization or read-back
    validation fails. Existing files are replaced only when ``overwrite`` is
    explicitly requested by the caller.
    """
    if not isinstance(replay, MrReplay):
        raise TypeError("replay 必须是 MrReplay 实例")
    destination = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(destination) or os.getcwd()
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"输出目录不存在：{parent}")
    if os.path.exists(destination) and not overwrite:
        raise FileExistsError(destination)
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination)}-",
            suffix=".tmp",
            dir=parent,
        )
        os.close(fd)
        replay.write(temp_name)
        MrReplay.read(temp_name)
        if overwrite:
            os.replace(temp_name, destination)
        elif os.name == "nt":
            # Windows rename is atomic and refuses an existing destination,
            # closing the race between the initial check and final commit.
            os.rename(temp_name, destination)
        else:
            # A same-directory hard link provides no-clobber publication on
            # platforms where rename would replace an existing destination.
            os.link(temp_name, destination)
            os.remove(temp_name)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass
    return destination


__all__ = (
    "ChartData", "ChartNote", "MALODY_VERSION", "MRFile", "MR_MODS", "MrEvent", "MrReplay",
    "MOD_AUTO", "MOD_LUCK", "MOD_FLIP", "MOD_CONST", "MOD_DASH", "MOD_RUSH",
    "MOD_HIDE", "MOD_ORIGIN", "MOD_SLOW", "MOD_DEATH",
    "edit_replay_metadata", "generate_perfect_replay", "load_chart", "scale_replay",
    "atomic_write_replay",
)
