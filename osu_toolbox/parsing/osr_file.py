""".osr 回放文件解析。"""

import lzma
import struct
from dataclasses import dataclass

from ..mania.mods import MOD_TARGET_PRACTICE


@dataclass
class ReplayFrame:
    time: int
    delta: int
    x: float
    y: float
    keys: int
    seq: int


@dataclass
class OsrReplay:
    mode: int
    game_version: int
    beatmap_md5: str
    player_name: str
    replay_md5: str
    count_300: int
    count_100: int
    count_50: int
    count_geki: int
    count_katu: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    mods: int
    life_graph: str
    timestamp_ticks: int
    online_score_id: int
    frames: list
    rng_seed: int = None


class OsrReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, fmt):
        size = struct.calcsize("<" + fmt)
        if self.pos + size > len(self.data):
            raise ValueError("回放文件不完整，缺少必要字段")
        value = struct.unpack_from("<" + fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def read_bytes(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError("回放文件中的数据长度无效或内容不完整")
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def read_uleb128(self):
        result = 0
        shift = 0
        while True:
            b = self.read("B")
            result |= (b & 0x7f) << shift
            if (b & 0x80) == 0:
                return result
            shift += 7
            if shift >= 64:
                raise ValueError("回放文件中的字符串长度无效")

    def read_string(self):
        marker = self.read("B")
        if marker == 0x00:
            return ""
        if marker != 0x0b:
            raise ValueError(f"回放文件中的字符串标记无效：{marker}")
        length = self.read_uleb128()
        return self.read_bytes(length).decode("utf-8", errors="replace")


def _decompress_osr_replay(data):
    for fmt in (lzma.FORMAT_AUTO, lzma.FORMAT_ALONE):
        try:
            return lzma.decompress(data, format=fmt)
        except lzma.LZMAError:
            pass
    raise ValueError("回放数据无法解压，文件可能已损坏")


def _parse_replay_frames(text, mode):
    frames = []
    rng_seed = None
    current_time = 0
    for seq, chunk in enumerate(text.split(",")):
        if not chunk:
            continue
        parts = chunk.split("|")
        if len(parts) < 4:
            continue
        delta = int(float(parts[0]))
        if delta == -12345:
            try:
                rng_seed = int(float(parts[3]))
            except ValueError:
                rng_seed = None
            continue
        current_time += delta
        x = float(parts[1])
        y = float(parts[2])
        z = int(float(parts[3]))
        if mode == 3:
            # osu!mania stable/lazer replays store the column bitfield in x.
            # Some third-party writers put it in z; keep that as a fallback.
            keys = int(x) if int(x) != 0 else z
        else:
            keys = z
        frames.append(ReplayFrame(current_time, delta, x, y, keys, seq))
    return frames, rng_seed


def parse_osr_file(path):
    with open(path, "rb") as f:
        reader = OsrReader(f.read())

    mode = reader.read("B")
    game_version = reader.read("i")
    beatmap_md5 = reader.read_string()
    player_name = reader.read_string()
    replay_md5 = reader.read_string()
    count_300 = reader.read("H")
    count_100 = reader.read("H")
    count_50 = reader.read("H")
    count_geki = reader.read("H")
    count_katu = reader.read("H")
    count_miss = reader.read("H")
    score = reader.read("i")
    max_combo = reader.read("H")
    perfect = bool(reader.read("B"))
    mods = reader.read("i")
    life_graph = reader.read_string()
    timestamp_ticks = reader.read("q")
    compressed_len = reader.read("i")
    compressed = reader.read_bytes(compressed_len)
    replay_data = _decompress_osr_replay(compressed).decode("utf-8", errors="replace")
    online_score_id = reader.read("q") if reader.pos + 8 <= len(reader.data) else 0
    if mods & MOD_TARGET_PRACTICE and reader.pos + 8 <= len(reader.data):
        reader.read("d")
    frames, rng_seed = _parse_replay_frames(replay_data, mode)

    return OsrReplay(
        mode=mode,
        game_version=game_version,
        beatmap_md5=beatmap_md5,
        player_name=player_name,
        replay_md5=replay_md5,
        count_300=count_300,
        count_100=count_100,
        count_50=count_50,
        count_geki=count_geki,
        count_katu=count_katu,
        count_miss=count_miss,
        score=score,
        max_combo=max_combo,
        perfect=perfect,
        mods=mods,
        life_graph=life_graph,
        timestamp_ticks=timestamp_ticks,
        online_score_id=online_score_id,
        frames=frames,
        rng_seed=rng_seed,
    )
