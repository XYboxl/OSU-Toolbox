""".osu 谱面文件解析。"""

import csv

from dataclasses import dataclass

from .. import config


@dataclass
class HitObject:
    col: int
    time: int
    is_hold: bool = False
    end_time: int = 0


@dataclass
class TimingPoint:
    time: float
    beat_length: float
    meter: int = 4
    inherited: bool = False
    effects: int = 0


@dataclass
class Beatmap:
    mode: int
    circle_size: float
    approach_rate: float
    overall_difficulty: float
    hp_drain_rate: float
    audio_lead_in: int
    audio_filename: str
    title: str
    artist: str
    version: str
    hit_objects: list
    timing_points: list
    background: str = ""
    background_offset: tuple = (0, 0)
    breaks: tuple = ()
    letterbox_in_breaks: bool = False
    special_style: bool = False


def mania_column(x, key_count):
    return min(key_count - 1, max(0, int(x * key_count / 512)))


def _parse_beatmap_events(event_lines):
    """[Events]：背景 `0,0,"bg",x,y` 与 break `2,start,end`；容错跳过畸形行。"""
    background = ""
    background_offset = (0, 0)
    breaks = []
    for line in event_lines:
        try:
            parts = [
                p.strip()
                for p in next(csv.reader([line], skipinitialspace=True))
            ]
        except (csv.Error, StopIteration):
            continue
        if not parts:
            continue
        head = parts[0]
        if head in ("0", "Background") and len(parts) >= 3 and not background:
            background = parts[2]
            off_x = off_y = 0
            try:
                if len(parts) > 3 and parts[3]:
                    off_x = int(float(parts[3]))
                if len(parts) > 4 and parts[4]:
                    off_y = int(float(parts[4]))
            except ValueError:
                off_x = off_y = 0
            background_offset = (off_x, off_y)
        elif head in ("2", "Break") and len(parts) >= 3:
            try:
                start = float(parts[1])
                end = float(parts[2])
            except ValueError:
                continue
            if end > start:
                breaks.append((start, end))
    breaks.sort()
    return background, background_offset, tuple(breaks)


def parse_osu_file(path):
    general, difficulty, metadata = {}, {}, {}
    hit_object_lines = []
    timing_point_lines = []
    event_lines = []
    section = None

    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue

            if section in ("General", "Editor", "Metadata", "Difficulty"):
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key, val = key.strip(), val.strip()
                if section == "General":
                    general[key] = val
                elif section == "Difficulty":
                    difficulty[key] = val
                elif section == "Metadata":
                    metadata[key] = val
            elif section == "HitObjects":
                hit_object_lines.append(line)
            elif section == "TimingPoints":
                timing_point_lines.append(line)
            elif section == "Events":
                event_lines.append(line)

    key_count = max(1, int(round(float(difficulty.get("CircleSize", config.KEY_COUNT)))))
    hit_objects = []
    for line in hit_object_lines:
        ho = _parse_hit_object(line, key_count)
        if ho:
            hit_objects.append(ho)
    hit_objects.sort(key=lambda o: o.time)
    timing_points = []
    for line in timing_point_lines:
        tp = _parse_timing_point(line)
        if tp:
            timing_points.append(tp)
    timing_points.sort(key=lambda p: p.time)
    audio_filename = general.get("AudioFilename", "").strip().strip('"')
    background, background_offset, breaks = _parse_beatmap_events(event_lines)
    return Beatmap(
        mode=int(general.get("Mode", 0)),
        circle_size=float(difficulty.get("CircleSize", 4)),
        approach_rate=float(difficulty.get("ApproachRate", 5)),
        overall_difficulty=float(difficulty.get("OverallDifficulty", 5)),
        hp_drain_rate=float(difficulty.get("HPDrainRate", 5)),
        audio_lead_in=int(general.get("AudioLeadIn", 0)),
        audio_filename=audio_filename,
        title=metadata.get("TitleUnicode") or metadata.get("Title", ""),
        artist=metadata.get("ArtistUnicode") or metadata.get("Artist", ""),
        version=metadata.get("Version", ""),
        hit_objects=hit_objects,
        timing_points=timing_points,
        background=background,
        background_offset=background_offset,
        breaks=breaks,
        letterbox_in_breaks=general.get("LetterboxInBreaks", "0").strip() == "1",
        special_style=general.get("SpecialStyle", "0").strip() == "1",
    )


def _parse_timing_point(line):
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None
    try:
        time_ms = float(parts[0])
        beat_length = float(parts[1])
    except ValueError:
        return None
    meter = 4
    if len(parts) > 2 and parts[2]:
        try:
            meter = int(float(parts[2]))
        except ValueError:
            meter = 4
    meter = max(1, meter)
    inherited = False
    if len(parts) > 6 and parts[6]:
        try:
            inherited = int(float(parts[6])) == 0
        except ValueError:
            inherited = beat_length < 0
    else:
        inherited = beat_length < 0
    effects = 0
    if len(parts) > 7 and parts[7]:
        try:
            effects = int(float(parts[7]))
        except (OverflowError, ValueError):
            effects = 0
    return TimingPoint(time_ms, beat_length, meter, inherited, effects)


def _parse_hit_object(line, key_count):
    parts = line.split(",")
    if len(parts) < 4:
        return None
    # A time is read as a whole millisecond, but the field itself may carry
    # decimals: an editor or a rate-changing tool can write a fractional one,
    # and the game itself accepts it (it truncates).  Reading it strictly used
    # to reject such a chart entirely, so every note of it disappeared.
    x = int(float(parts[0]))
    t = int(float(parts[2]))
    type_int = int(float(parts[3]))
    col = mania_column(x, key_count)

    if type_int & 128:
        if len(parts) < 6:
            return None
        end_time = int(float(parts[5].split(":")[0]))
        return HitObject(col, t, is_hold=True, end_time=end_time)
    if type_int & 1:
        return HitObject(col, t)
    return None
