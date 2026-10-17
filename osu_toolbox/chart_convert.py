"""Conversion between osu!, Malody and StepMania charts.

The converter normalises every source into one shared document model (notes
with semantic types, timing and scroll control, sound triggers, resource
references and structured diagnostics) and drives every decision from the
capability tables: the preflight (``preflight_conversion``) and the writers
share ``plan_conversion``, so what the user is told before the run is exactly
what the run does.  Features a target cannot express are reported with their
strategy and counts instead of being silently dropped; conditions that would
produce a known-wrong chart (unsupported gameplay mode, unreadable source,
changed output plan) stop the conversion.
"""

from dataclasses import dataclass, field
from fractions import Fraction
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import codecs
import csv
import json
import math
import os
from pathlib import Path
import posixpath
import re
import tempfile
import zipfile


SUPPORTED_CHART_EXTENSIONS = (".osu", ".mc", ".sm")
SUPPORTED_INPUT_EXTENSIONS = SUPPORTED_CHART_EXTENSIONS + (".osz", ".mcz")
SUPPORTED_OUTPUT_EXTENSIONS = SUPPORTED_CHART_EXTENSIONS + (".osz", ".mcz")


def archive_chart_extension(extension):
    extension = str(extension).lower()
    return {".osz": ".osu", ".mcz": ".mc"}.get(extension, extension)


# Semantic note types carried by the shared model.  ``tap``/``hold`` are the
# playable basics every supported target expresses; the rest come from
# StepMania note data and need an explicit per-target strategy (see the
# capability table and ``_conversion_plan``).
NOTE_TYPE_TAP = "tap"
NOTE_TYPE_HOLD = "hold"
NOTE_TYPE_ROLL = "roll"
NOTE_TYPE_MINE = "mine"
NOTE_TYPE_LIFT = "lift"
NOTE_TYPE_FAKE = "fake"
NOTE_TYPE_AUTO_KEYSOUND = "auto_keysound"
NOTE_TYPES = (
    NOTE_TYPE_TAP, NOTE_TYPE_HOLD, NOTE_TYPE_ROLL, NOTE_TYPE_MINE,
    NOTE_TYPE_LIFT, NOTE_TYPE_FAKE, NOTE_TYPE_AUTO_KEYSOUND,
)


@dataclass
class ChartNoteData:
    time: int
    column: int
    end_time: int = None
    # Semantic type.  Holds and rolls both carry ``end_time``; a plain
    # ``ChartNoteData(time, column, end)`` from older callers still reads and
    # writes as a hold because writers key the tail off ``end_time``.
    note_type: str = NOTE_TYPE_TAP
    # Keysound bound to this note (played when the player hits it).  Empty
    # when the note is silent or uses the target's default hit sound.
    sound: str = ""
    sound_volume: object = None
    # Stable identity inside the source document: the parser assigns a
    # sequential ordinal, so it never depends on runtime object addresses.
    source_id: object = None
    # Format-specific附加标记 kept for same-format rewriting (for example an
    # osu! hit-sound byte and sample sets).  Cross-format conversion reads
    # semantics from the dedicated fields above and never from here.
    extra: object = None


@dataclass
class ChartSoundEvent:
    """A sound triggered by the chart timeline rather than by a playable note."""

    time: float
    sound: str
    volume: object = None
    kind: str = "auto"
    # Lane the trigger sits in for lane-based formats (StepMania ``K``).
    column: object = None


@dataclass
class ChartScrollEvent:
    """One scroll-velocity (SV) change.

    ``multiplier`` uses osu! semantics: the factor applied on top of the
    scroll speed derived from the active BPM.  ``beat`` keeps the source
    beat anchor so a beat-based target does not have to round-trip through
    milliseconds.  ``extra`` retains raw source fields (sample set, volume,
    kiai flag, ...) that are not scroll information but travel on the same
    source record, so a same-format rewrite can emit them back unchanged.
    """

    time: float
    multiplier: float
    beat: float = None
    interpolation: str = "step"
    extra: object = ()
    source_id: object = None


@dataclass
class ChartDiagnostic:
    """One structured conversion finding.

    ``severity`` is ``info`` (worth knowing), ``warning`` (caveat, no data
    change), ``loss`` (semantics dropped or approximated) or ``error``
    (blocks output: a known-wrong result must never be written).
    ``code`` is a stable internal identifier; ``message`` is the Chinese
    user-facing text; ``count``/``detail`` quantify the impact.
    """

    code: str
    message: str
    severity: str = "warning"
    count: int = 0
    detail: str = ""


@dataclass
class ChartDocument:
    title: str = ""
    subtitle: str = ""
    artist: str = ""
    creator: str = ""
    version: str = "Converted"
    source: str = ""
    tags: str = ""
    audio_filename: str = ""
    background_filename: str = ""
    cover_filename: str = ""
    key_count: int = 4
    notes: list = field(default_factory=list)
    timings: list = field(default_factory=list)
    difficulty_name: str = ""
    chart_type: str = "dance-single"
    meter: str = "1"
    # Canonical audio timeline baseline: the elapsed audio time (in
    # milliseconds) at chart beat 0 before any same-beat pause event. A
    # positive value means beat 0 occurs after the audio starts; a negative
    # value means it occurs before the audio origin. ``_beat_to_time`` adds
    # timing-event durations when a format carries them.
    beat_zero_ms: float = None
    # ``audio_delay_ms``/``offset_ms`` are retained as source-format aliases.
    # They are deliberately not used as a universal playback delay: osu!'s
    # AudioLeadIn is a separate pre-audio silence setting.
    offset_ms: float = 0.0
    audio_delay_ms: float = 0.0
    audio_lead_in_ms: float = 0.0
    extra: dict = field(default_factory=dict)
    # Beat coordinates for ``timings``. Keeping these anchors avoids losing
    # non-zero timing starts and makes beat/time conversion independent of the
    # source format.
    timing_beats: list = field(default_factory=list)
    # StepMania timing events represented as ``(beat, duration_ms, kind)``.
    # ``kind`` is ``"delay"`` for a delay before the beat and ``"stop"`` for
    # a stop after the beat.  Keeping them in the shared model prevents later
    # BPM segments and notes from silently drifting after an event.
    timing_events: list = field(default_factory=list)
    chart_description: str = ""
    # Automatically triggered sounds (no playable note attached), e.g.
    # StepMania ``K`` markers.  Note-bound keysounds live on the note itself.
    sound_events: list = field(default_factory=list)
    # Scroll-velocity changes, kept separate from BPM/timing control.
    scrolls: list = field(default_factory=list)
    # Structured findings produced while parsing and while planning/writing.
    diagnostics: list = field(default_factory=list)

    def __post_init__(self):
        # ``offset_ms`` and ``audio_delay_ms`` remain compatibility aliases for
        # callers of the initial converter API.  New code uses beat_zero_ms for
        # chart/audio alignment and audio_lead_in_ms for osu! pre-roll silence.
        if not self.audio_delay_ms and self.offset_ms:
            self.audio_delay_ms = self.offset_ms
        elif not self.offset_ms and self.audio_delay_ms:
            self.offset_ms = self.audio_delay_ms
        if self.timing_events:
            self.timing_events = sorted(
                self.timing_events,
                key=lambda item: (_safe_float(item[0]), str(item[2]).casefold()),
            )
        if self.beat_zero_ms is None:
            # A source-level audio delay describes where beat zero falls
            # relative to the audio origin.  It must take precedence over a
            # default timing point at beat zero; otherwise a document created
            # by a new decoder would silently lose its alignment metadata.
            if self.audio_delay_ms or self.offset_ms:
                self.beat_zero_ms = -_safe_float(self.audio_delay_ms or self.offset_ms)
            elif self.timings:
                self.beat_zero_ms = _safe_float(self.timings[0][0])
            else:
                self.beat_zero_ms = 0.0


class ChartConversionError(ValueError):
    pass


# Byte-order marks that identify a chart's encoding.  ``.osu`` and ``.mc`` are
# both plain text containers, and editors (and some collectors) do save them as
# UTF-16, so the BOM has to be honoured instead of guessed at: a UTF-16 body
# decodes "successfully" as cp936 into NUL-riddled mojibake that no parser can
# read.
_BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_chart_bytes(data):
    """Decode chart bytes, returning ``(text, had_utf8_bom)``.

    The BOM decides first, then a strict UTF-8 attempt, then the legacy
    single-byte encodings a chart may have been saved in.  ``latin-1`` is last
    because it decodes anything at all, so it can never fail and must not mask
    a better candidate.
    """
    if not isinstance(data, (bytes, bytearray)):
        return str(data), False
    data = bytes(data)
    for mark, encoding in _BYTE_ORDER_MARKS:
        if data.startswith(mark):
            try:
                return data.decode(encoding), encoding.startswith("utf-8")
            except UnicodeDecodeError:
                break
    if len(data) >= 2 and data[0] != 0 and data[1] == 0:
        try:
            return data.decode("utf-16-le"), False
        except UnicodeDecodeError:
            pass
    if len(data) >= 2 and data[0] == 0 and data[1] != 0:
        try:
            return data.decode("utf-16-be"), False
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            return data.decode(encoding), False
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), False


def read_chart_text(path):
    """Read a chart file as text plus whether it carried a UTF-8 BOM."""
    return decode_chart_bytes(Path(path).read_bytes())


def _read_text(path):
    text, _bom = read_chart_text(path)
    return text


def _clean(value):
    return str(value or "").strip()


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _normalise_sm_meter(value, default=1):
    parsed = _safe_float(value, math.nan)
    if not math.isfinite(parsed) or parsed <= 0:
        return int(default)
    return max(1, int(parsed))


# StepMania MSD readers remove ``//`` comments during initial scanning before
# splitting values into colon-separated fields.  A comment therefore does not
# just hide text: it deletes the rest of the line, which shifts every following
# field.  Escapes are only recognised for ``\\``, ``\;``, ``\:`` and ``\#``;
# there is no escape for ``//``.  Keep the normaliser and the writer aware of
# both facts.
_MSD_UNESCAPE = {"\\": "\\", ";": ";", ":": ":", "#": "#"}
_MSD_ESCAPE = {"\\": "\\\\", ";": "\\;", ":": "\\:", "#": "\\#"}


def _msd_unescape(text):
    """Resolve MSD escape sequences exactly once, left to right."""
    if "\\" not in text:
        return text
    result = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            index += 1
            result.append(_MSD_UNESCAPE.get(text[index], text[index]))
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _msd_escape(text):
    return "".join(_MSD_ESCAPE.get(char, char) for char in text)


def _collapse_sm_comment_markers(text):
    """Remove every ``//`` sequence so no parser can read it as a comment.

    ``//`` has no escape in the MSD format, so it cannot be preserved; runs of
    slashes are collapsed to a single ``/`` instead of a single
    ``str.replace`` pass, which would leave ``//`` behind for runs of three or
    more.
    """
    if "/" not in text:
        return text
    return re.sub(r"/{2,}", "/", text)


def _normalise_sm_field(value, *, notes_field=False):
    """Make user metadata safe for StepMania delimiters and comments.

    Every MSD escape is applied, not just the comment marker:

    * ``//`` cannot be escaped at all, so slash runs collapse to one ``/``;
    * ``;`` would close the value early;
    * ``#`` is only special at the start of a line, but a title such as
      ``#hash`` would otherwise make the reader recover as if a ``;`` were
      missing and split the value in two;
    * ``:`` separates the chart fields inside ``#NOTES``.

    ``notes_field`` selects the ``#NOTES`` behaviour, where a colon must stay
    inside its field.  Elsewhere a colon is ordinary text.
    """
    text = _clean(value).replace("\r", " ").replace("\n", " ")
    text = _collapse_sm_comment_markers(text)
    if ";" in text:
        text = text.replace(";", ",")
    return _msd_escape(text)


def _normalise_sm_resource(value):
    """Make a referenced file name safe without changing its meaning."""
    text = _clean(value).replace("\r", " ").replace("\n", " ")
    return _collapse_sm_comment_markers(text)


def _msd_values(text):
    """Split MSD text into ``(name, [fields])`` values.

    Follows StepMania MSD scanning semantics: ``//`` comments are removed
    while scanning, ``;`` closes a value, ``:`` opens a new field, a ``#`` that
    is the first non-blank character of a line recovers from a missing ``;``,
    and ``\\`` makes the following character ordinary.  Escapes are kept
    untouched in the returned fields so callers can decide whether to resolve
    them, which is what keeps an escaped colon inside ``#NOTES`` from being
    mistaken for a field separator.

    The scanner keeps one persistent buffer plus a write cursor: a field
    records only the characters up to ``cursor``, and the recovery rule
    inspects the raw buffer *including* characters written after the previous
    ``:`` or ``;``.  Rebuilding the buffer per field instead would make
    ``#TITLE:#hash`` look like a tag at the start of a line and silently split
    the title in two.
    """
    values = []
    fields = None
    processed = []
    written = 0
    index = 0
    length = len(text)

    def push(char):
        nonlocal written
        if written < len(processed):
            processed[written] = char
        else:
            processed.append(char)
        written += 1

    def add_param():
        fields.append("".join(processed[:written]))

    def close_value():
        add_param()
        values.append(fields)

    while index < length:
        char = text[index]
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            # A comment swallows the rest of the line, including its newline.
            while index < length and text[index] != "\n":
                index += 1
            push("\n")
            continue
        if fields is None:
            # Outside a value only ``#`` is meaningful; an escape still
            # consumes the following character so ``\#`` cannot start a tag.
            if char == "#":
                fields = []
                written = 0
            elif char == "\\":
                index += 2
                continue
            index += 1
            continue
        if char == "#" and _msd_tag_starts_line(processed, written):
            close_value()
            fields = []
            written = 0
            index += 1
            continue
        if char == ":":
            add_param()
            written = 0
            index += 1
            continue
        if char == ";":
            close_value()
            fields = None
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            # Keep the escape pair for the caller to resolve.
            push(char)
            push(text[index + 1])
            index += 2
            continue
        push(char)
        index += 1
    if fields is not None:
        # An unterminated value is still usable, exactly as StepMania does.
        close_value()
    return values


def _msd_tag_starts_line(processed, written):
    """Return whether the pending buffer ends on a line of its own."""
    for offset in range(written - 1, -1, -1):
        char = processed[offset]
        if char in "\r\n":
            return True
        if char not in " \t":
            return False
    return True


def _sm_tag_values(text):
    """Return the last value of each ``#TAG`` (already split into fields)."""
    result = {}
    for fields in _msd_values(text):
        if not fields:
            continue
        # The ``#`` is a scanner delimiter, not part of the parameter, and the
        # reader already resolved any escape while scanning.
        name = _clean(fields[0]).strip().upper()
        if name:
            result[name] = fields[1:]
    return result


def _sm_single(tags, name, default=""):
    """Return a tag's first field, unescaped and trimmed."""
    fields = tags.get(name)
    if not fields:
        return default
    return _msd_unescape(fields[0]).strip()


def _sm_notes_body(fields):
    """Return the note-data text of a ``#NOTES`` value.

    ``fields`` includes the tag name, so an SM3.9/ITG chart has exactly seven
    fields and the note data is the last one.  Extra colons inside the note
    data were escaped by the reader; re-escape them here so re-joining cannot
    reintroduce a field separator.
    """
    if len(fields) < 7:
        return ""
    if len(fields) == 7:
        return fields[6]
    return ":".join(_msd_escape(_msd_unescape(item)) for item in fields[6:])


# StepMania and ArrowVortex readers derive the number of lanes from the note
# rows rather than from the steps type, so a mismatched pair silently
# reinterprets every row.  The writer therefore always keeps the declared
# type consistent with the lane count.
#
# Standard measure subdivisions for StepMania format.  Readers allocate 192
# rows per measure section, so a measure must stay within 192 rows and on
# one of these counts for readers to recover the grid without loss.
SM_MEASURE_SUBDIVISIONS = (4, 8, 12, 16, 24, 32, 48, 64, 96, 192)
SM_ROWS_PER_MEASURE = 192


def _sm_local_beat_fraction(local_beat):
    """Clamp a local beat to a bounded rational before grid arithmetic.

    Rounding to millisecond precision first removes the last-bit float noise a
    round trip through milliseconds leaves behind, for example 0.498 becoming
    0.49800000000000055.  Without that step ``limit_denominator`` keeps the
    noise as a different, slightly closer fraction and the note can snap to the
    neighbouring row.
    """
    value = round(float(local_beat), 3)
    return Fraction(value).limit_denominator(2048)


def _sm_global_row(beat):
    """Snap a beat onto the shared grid: ``(global_row, measure, local_row)``.

    One global row index drives both the measure and the row.  Choosing the
    measure first breaks at a measure boundary: an exact boundary can come back
    from ``_time_to_beat`` as 387.9999999999995, which belongs on row 0 of
    measure 97, while the nearest row inside measure 96 is row 191 -- almost a
    whole row away.  Rounding the global row to nearest also means a beat just
    past a row midpoint snaps forward instead of staying a whole row behind.
    """
    total = _sm_local_beat_fraction(beat) * SM_ROWS_PER_MEASURE / 4
    row = round(total)
    measure, local_row = divmod(row, SM_ROWS_PER_MEASURE)
    return max(0, row), max(0, measure), local_row


SM_CHART_TYPES = (
    ("dance-single", 4), ("dance-double", 8), ("dance-couple", 8),    ("dance-solo", 6), ("dance-threepanel", 3), ("dance-routine", 8),
    ("pump-single", 5), ("pump-halfdouble", 6), ("pump-double", 10),
    ("pump-couple", 10), ("pump-routine", 10),
    ("kb7-single", 7),
    ("ez2-single", 5), ("ez2-real", 7), ("ez2-double", 10),
    ("para-single", 5),
    ("ds3ddx-single", 8),
    ("bm-single5", 6), ("bm-double5", 12), ("bm-single7", 8), ("bm-double7", 16),
    ("maniax-single", 4), ("maniax-double", 8),
    ("techno-single4", 4), ("techno-single5", 5), ("techno-single8", 8),
    ("techno-double4", 8), ("techno-double5", 10), ("techno-double8", 16),
    ("popn-five", 5), ("popn-nine", 9),
    ("lights-cabinet", 6),
)
_SM_TYPE_COLUMNS = dict(SM_CHART_TYPES)
_SM_COLUMN_TYPE = {}
for _sm_type, _sm_columns in SM_CHART_TYPES:
    _SM_COLUMN_TYPE.setdefault(_sm_columns, _sm_type)
del _sm_type, _sm_columns


def _sm_chart_type_for_columns(chart_type, key_count):
    """Return a steps type whose lane count matches ``key_count``.

    An existing type is preserved when it is already consistent, so the
    original dance-solo/kb7-single/... choice survives a conversion.
    """
    key_count = max(1, _safe_int(key_count, 4))
    candidate = _clean(chart_type).casefold()
    if _SM_TYPE_COLUMNS.get(candidate) == key_count:
        return candidate
    return _SM_COLUMN_TYPE.get(key_count) or _SM_COLUMN_TYPE.get(4, "dance-single")


# SM chart readers map unrecognised ``#NOTES`` difficulty text onto an Edit
# difficulty slot ("edit0"/"Edit").  Write a standard recognized difficulty
# name and keep the authored text in the description field, where it is
# free-form and therefore lossless (preserving conventions like "ArrowVortex edit0").
_SM_DIFFICULTY_NAMES = ("beginner", "easy", "medium", "hard", "challenge")


def _sm_legacy_difficulty(version):
    """Map the pre-SM5 aliases StepMania still accepts onto valid names."""
    folded = _clean(version).casefold()
    if folded in ("smaniac", "challenge"):
        return "challenge"
    if folded in ("edit", "expert"):
        return "hard"
    return ""


def _sm_difficulty_fields(document):
    """Return ``(description, difficulty)`` for a ``#NOTES`` chart header."""
    version = _clean(document.version or document.difficulty_name or "Converted")
    description = _clean(document.chart_description)
    if version and version.casefold() not in description.casefold():
        description = f"{description} {version}".strip() if description else version
    difficulty = _sm_legacy_difficulty(version)
    if not difficulty:
        # An unknown name removes the difficulty from the song wheel in SM5,
        # so keep a valid enum and preserve the text in the description.
        difficulty = "edit" if description else "medium"
    return _normalise_sm_field(description, notes_field=True), _normalise_sm_field(difficulty, notes_field=True)


def _audio_path(value):
    value = _clean(value)
    if not value or value.casefold() in {"none", "null", "false", "true"}:
        return ""
    return value


def _mapping_value(mapping, *keys):
    """Read the first non-empty value from a mapping using case-insensitive keys."""
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(str(key).casefold())
        if value is not None and value != "":
            return value
    return None


def _mc_audio_metadata(data, meta, song):
    """Extract audio path and chart/audio alignment from common .mc variants."""
    containers = (song, meta, data)
    audio_filename = ""
    for container in containers:
        candidate = _audio_path(_mapping_value(
            container, "file", "music", "audio", "sound", "audio_file", "audioFilename"
        ))
        if candidate:
            audio_filename = candidate
            break
    audio_delay = None
    for container in containers:
        value = _mapping_value(
            container, "audio_offset", "audioOffset", "audio_delay", "audioDelay", "offset"
        )
        if value is not None:
            # Keep an explicit zero. It is a meaningful alignment and must not
            # fall through to a legacy alias from another container.
            audio_delay = _safe_float(value, 0.0)
            break
    return audio_filename, (0.0 if audio_delay is None else audio_delay)


def _is_mc_audio_event(item):
    """Return whether an .mc note is a chart-level audio event."""
    if not isinstance(item, dict):
        return False
    where = item.get("where") if isinstance(item.get("where"), dict) else {}
    has_position = (
        any(key in item for key in ("column", "x"))
        or any(key in where for key in ("x", "y", "z"))
    )
    if has_position:
        return False
    has_audio = any(_audio_path(item.get(key)) for key in ("sound", "music", "audio"))
    # An unpositioned type-1 or sound/offset record declares the music.
    # A path alone does not establish its playback role.
    return bool(_safe_int(item.get("type"), 0) == 1 or (has_audio and "offset" in item))


def _first_present(mapping, *keys):
    """Return the first explicitly supplied value, including numeric zero."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping[key] is not None and mapping[key] != "":
            return mapping[key]
    return None


def _mc_integer_offset(value, default=0):
    """Return a Malody ``offset`` as a whole number of milliseconds.

    ``offset`` is an integer field in the native format; writing a float such
    as ``0.0`` or ``-644.0`` makes Malody misread the chart/audio alignment.
    Round to nearest so the value stays as accurate as millisecond timestamps
    allow instead of drifting by up to a millisecond.
    """
    if value is None or value == "":
        return int(default)
    return int(round(_safe_float(value, default)))


def _normalise_timing_entries(entries, default_bpm=120.0):
    """Return sorted, deduplicated ``(beat, bpm, meter)`` timing entries."""
    cleaned = []
    for beat, bpm, meter in entries:
        beat = _safe_float(beat)
        bpm = _safe_float(bpm, default_bpm)
        if bpm <= 0:
            continue
        cleaned.append((beat, bpm, max(1, _safe_int(meter, 4))))
    cleaned.sort(key=lambda item: item[0])
    result = []
    for item in cleaned:
        if result and item[0] == result[-1][0]:
            result[-1] = item
        else:
            result.append(item)
    if not result:
        result = [(0.0, default_bpm, 4)]
    if result[0][0] > 0:
        result.insert(0, (0.0, result[0][1], result[0][2]))
    return result


def _parse_osu(path):
    sections = {}
    section = None
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, [])
        elif section and line and not line.startswith("//"):
            sections[section].append(line)

    def values(name):
        result = {}
        for line in sections.get(name, ()):
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip()] = value.strip()
        return result

    general = values("General")
    metadata = values("Metadata")
    difficulty = values("Difficulty")
    # The file extension does not make a chart a key chart: only osu!mania
    # (Mode: 3) maps objects onto lanes.  A standard/taiko/catch beatmap would
    # silently turn circles into columns, which is not a conversion.
    mode = _safe_int(general.get("Mode"), 0)
    _OSU_MODE_NAMES = {0: "osu!", 1: "osu!taiko", 2: "osu!catch", 3: "osu!mania"}
    if mode != 3:
        raise ChartConversionError(
            f"{Path(path).name} 不是 osu!mania 谱面（Mode: {mode}，"
            f"{_OSU_MODE_NAMES.get(mode, '未知玩法')}）；转谱仅支持 osu!mania"
        )
    key_count = max(1, _safe_int(difficulty.get("CircleSize"), 4))
    notes = []
    skipped_objects = 0
    stray_flag_objects = 0
    source_ordinal = 0
    for line in sections.get("HitObjects", ()):
        parts = line.split(",")
        if len(parts) < 5:
            # A truncated row is as unreadable as a malformed one; count it
            # instead of letting it vanish without a diagnostic.
            skipped_objects += 1
            continue
        try:
            # A time is a whole millisecond, but the field may carry decimals:
            # an editor or a rate-changing tool can write a fractional one and
            # the game truncates it, so reading it strictly would drop the note.
            x = int(float(parts[0]))
            time = int(float(parts[2]))
            object_type = int(float(parts[3]))
        except ValueError:
            skipped_objects += 1
            continue
        column = min(key_count - 1, max(0, int(x * key_count / 512)))
        # Mania objects are taps (bit 0) and holds (bit 7).  Additional flag
        # bits (combo colouring and the like) must not make a note disappear,
        # but bits that name another gameplay concept (slider/spinner) have no
        # mania meaning and are counted instead of silently reinterpreted.
        hit_sound = _safe_int(parts[4], 0) if len(parts) > 4 else 0
        sample_text = ""
        if object_type & 128 and len(parts) >= 6:
            sample_text = parts[5].split(":", 1)[1] if ":" in parts[5] else ""
        elif len(parts) >= 6:
            sample_text = parts[5]
        sample_fields = sample_text.split(":", 4) if sample_text else []
        sample_file = sample_fields[4].strip() if len(sample_fields) > 4 else ""
        sample_volume = (
            _safe_int(sample_fields[3], 0) if len(sample_fields) > 3 else None
        )
        note_extra = None
        if hit_sound or any(_safe_int(item, 0) for item in sample_fields[:3]):
            # Default-skin hit flags and sample-set choices travel on the note
            # so a same-format rewrite can emit them back; they are not files
            # and are never invented as one.
            note_extra = {
                "osu_hit_sound": hit_sound,
                "osu_sample_sets": tuple(_safe_int(item, 0) for item in sample_fields[:3]),
            }
        if object_type & 128 and len(parts) >= 6:
            end_time = _safe_int(parts[5].split(":", 1)[0], time)
            notes.append(ChartNoteData(
                time, column, end_time if end_time > time else None,
                note_type=NOTE_TYPE_HOLD if end_time > time else NOTE_TYPE_TAP,
                sound=sample_file,
                sound_volume=sample_volume if sample_volume else None,
                source_id=source_ordinal,
                extra=note_extra,
            ))
        elif object_type & 1:
            notes.append(ChartNoteData(
                time, column,
                sound=sample_file,
                sound_volume=sample_volume if sample_volume else None,
                source_id=source_ordinal,
                extra=note_extra,
            ))
        else:
            skipped_objects += 1
            continue
        if object_type & 10:
            # Bits 1 (slider) and 3 (spinner) name standard-mode concepts; in
            # a mania chart they are stray flags on an otherwise valid note.
            stray_flag_objects += 1
        source_ordinal += 1

    timings = []
    scrolls = []
    red_extras = {}
    timing_records = []
    for line in sections.get("TimingPoints", ()):
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 2:
            continue
        time = _safe_float(parts[0])
        beat_length = _safe_float(parts[1])
        timing_records.append(tuple(parts))
        # The explicit osu! uninherited flag is authoritative when present.
        # This prevents slider-velocity (inherited) points from being treated
        # as BPM changes merely because a malformed file uses a positive
        # magnitude.
        uninherited = 1
        if len(parts) > 6 and parts[6]:
            try:
                uninherited = _safe_int(parts[6], 1)
            except (TypeError, ValueError):
                uninherited = 1
        if uninherited != 1:
            # Inherited (green) point: scroll-velocity multiplier plus the
            # sample/volume/kiai fields that ride on the same record.  The
            # multiplier is a pure scroll control, never folded into BPM.
            if beat_length < 0:
                scrolls.append(ChartScrollEvent(
                    time=time,
                    multiplier=-100.0 / beat_length,
                    extra=tuple(parts[2:]),
                ))
            continue
        if beat_length <= 0:
            continue
        meter = max(1, _safe_int(parts[2], 4)) if len(parts) > 2 else 4
        timings.append((time, beat_length, meter))
        if len(parts) > 3:
            # Sample set, sample index, volume and kiai ride on red points
            # too.  They are not scroll or BPM information: keep them beside
            # the point so a same-format rewrite can emit them back and a
            # cross-format conversion can report what it cannot carry.
            red_extras[time] = (
                parts[3] if len(parts) > 3 else "1",
                parts[4] if len(parts) > 4 else "0",
                parts[5] if len(parts) > 5 else "100",
                parts[7] if len(parts) > 7 else "0",
            )
    timings.sort(key=lambda item: item[0])
    deduplicated_timings = []
    for timing in timings:
        if deduplicated_timings and timing[0] == deduplicated_timings[-1][0]:
            deduplicated_timings[-1] = timing
        else:
            deduplicated_timings.append(timing)
    timings = deduplicated_timings
    if not timings:
        timings = [(0.0, 500.0, 4)]
    # osu! timing points carry absolute milliseconds but no beat number, so the
    # beat grid needs an origin.  Normally the first uninherited point is that
    # origin.  It must not be when the chart has content *before* it: JACK-DELTA
    # puts its first red line at 37700ms while its notes start at 300ms and the
    # audio has music from 0ms, so taking that point as beat zero pushed beats
    # 0.75..93.5 before beat zero and told Malody to start the song 37.7s in.
    #
    # Extending the first beat length back to 0ms fixes that without touching
    # the beat<->time mapping, because the added point repeats the same BPM.
    # The branch is deliberately narrow: when nothing precedes the first point
    # the authored anchor is kept exactly, so no beat numbers are renumbered.
    # (First-point beats are all exactly 0.0 by construction, hence the raw
    # comparison.)
    content_times = [note.time for note in notes]
    earliest_content = min(content_times) if content_times else timings[0][0]
    if timings[0][0] > 0 and earliest_content < timings[0][0]:
        timings.insert(0, (0.0, timings[0][1], timings[0][2]))
    timing_beats = []
    beat = 0.0
    previous_time, previous_length = timings[0][0], timings[0][1]
    timing_beats.append(beat)
    for time, beat_length, _ in timings[1:]:
        beat += (time - previous_time) / previous_length
        timing_beats.append(beat)
        previous_time, previous_length = time, beat_length
    for scroll in scrolls:
        # Anchor every scroll change on the beat grid so a beat-based target
        # does not have to round-trip through integer milliseconds.
        anchor = 0
        for index in range(1, len(timings)):
            if timings[index][0] > scroll.time:
                break
            anchor = index
        anchor_time, anchor_length, _ = timings[anchor]
        scroll.beat = timing_beats[anchor] + (scroll.time - anchor_time) / anchor_length
    background_filename = ""
    for line in sections.get("Events", ()):
        try:
            parts = [item.strip() for item in next(csv.reader([line], skipinitialspace=True))]
        except (csv.Error, StopIteration):
            continue
        if len(parts) >= 3 and parts[0] in ("0", "Background"):
            background_filename = _clean(parts[2])
            break
    # `AudioLeadIn` is a gameplay pre-roll in osu!, not a chart/audio
    # alignment offset. Hitobject and timing point timestamps are already
    # relative to the audio origin and must remain unchanged. Formats such as
    # Malody and StepMania encode beat-zero alignment separately; that value is
    # represented by `beat_zero_ms` below.
    audio_lead_in_ms = max(0.0, _safe_float(general.get("AudioLeadIn"), 0.0))
    title = metadata.get("TitleUnicode") or metadata.get("Title", "")
    subtitle = metadata.get("SubtitleUnicode") or metadata.get("Subtitle", "")
    artist = metadata.get("ArtistUnicode") or metadata.get("Artist", "")
    extra = {
        "osu_general": general,
        "osu_editor": values("Editor"),
        "osu_metadata": metadata,
        "osu_difficulty": difficulty,
        "osu_red_extras": red_extras,
        "osu_timing_records": timing_records,
        "osu_timing_state": (deepcopy(timings), deepcopy(scrolls)),
        "osu_sections": {
            name: list(values_list)
            for name, values_list in sections.items()
            if name not in {"General", "Editor", "Metadata", "Difficulty", "TimingPoints", "HitObjects"}
        },
    }
    diagnostics = []
    if skipped_objects:
        diagnostics.append(ChartDiagnostic(
            code="osu-unreadable-object",
            message=f"{skipped_objects} 个无法识别的对象已跳过（不是合法的 mania 音符）",
            severity="warning",
            count=skipped_objects,
        ))
    if stray_flag_objects:
        diagnostics.append(ChartDiagnostic(
            code="osu-stray-object-flag",
            message=f"{stray_flag_objects} 个音符带有非 mania 的附加标记，已按普通音符保留",
            severity="info",
            count=stray_flag_objects,
        ))
    return [ChartDocument(
        title=title,
        subtitle=subtitle,
        artist=artist,
        creator=metadata.get("Creator", ""),
        version=metadata.get("Version", "Converted"),
        source=metadata.get("Source", ""),
        tags=metadata.get("Tags", ""),
        audio_filename=general.get("AudioFilename", "").strip('"'),
        background_filename=background_filename,
        cover_filename=background_filename,
        key_count=key_count,
        notes=sorted(notes, key=lambda item: (item.time, item.column)),
        timings=timings,
        timing_beats=timing_beats,
        beat_zero_ms=timings[0][0] if timings else 0.0,
        # osu! has no chart/audio offset field. Keep the compatibility aliases
        # at zero and retain the independent pre-roll in `audio_lead_in_ms`.
        offset_ms=0.0,
        audio_delay_ms=0.0,
        audio_lead_in_ms=audio_lead_in_ms,
        difficulty_name=metadata.get("Version", ""),
        extra=extra,
        scrolls=scrolls,
        diagnostics=diagnostics,
    )]


def _beat_value(raw):
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, (list, tuple)) or not raw:
        return 0.0
    values = [_safe_float(item) for item in raw]
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0] + values[1] / 4.0
    denominator = values[2] or 1.0
    return values[0] + values[1] / denominator


def _beat_tuple(value, max_denominator=192):
    fraction = Fraction(float(value)).limit_denominator(max_denominator)
    whole = fraction.numerator // fraction.denominator
    remainder = fraction - whole
    if not remainder:
        return [whole, 0, 4]
    denominator = fraction.denominator
    numerator = (remainder * denominator).numerator
    return [whole, numerator, denominator]


def _mc_segments(changes):
    changes = [(item[0], item[1], 4) for item in changes]
    changes = [(beat, bpm) for beat, bpm, _ in _normalise_timing_entries(changes)]
    segments = []
    current_ms = 0.0
    previous_beat, previous_bpm = changes[0]
    if previous_beat:
        current_ms = previous_beat * 60000.0 / previous_bpm
    segments.append((previous_beat, current_ms, previous_bpm))
    for beat, bpm in changes[1:]:
        current_ms += (beat - previous_beat) * 60000.0 / previous_bpm
        segments.append((beat, current_ms, bpm))
        previous_beat, previous_bpm = beat, bpm
    return segments


def _beat_to_ms_float(beat, segments):
    segment = segments[0]
    for candidate in segments[1:]:
        if candidate[0] > beat:
            break
        segment = candidate
    return segment[1] + (beat - segment[0]) * 60000.0 / segment[2]


def _beat_to_ms(beat, segments):
    """Convert a beat coordinate to the integer timestamp used by notes."""
    return int(round(_beat_to_ms_float(beat, segments)))


def _mc_parse_data(data):
    if not isinstance(data, dict):
        raise ChartConversionError(".mc chart root must be an object")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    song = meta.get("song") if isinstance(meta.get("song"), dict) else {}
    raw_timing = data.get("time") or data.get("timing") or []
    timing_entries = []
    for item in raw_timing if isinstance(raw_timing, list) else ():
        if not isinstance(item, dict):
            continue
        bpm = _safe_float(item.get("bpm"), 120.0)
        if bpm > 0:
            timing_entries.append((
                _beat_value(item.get("beat", 0)),
                bpm,
                item.get("signature", item.get("meter", 4)),
            ))
    timing_entries = _normalise_timing_entries(timing_entries)
    changes = [(beat, bpm) for beat, bpm, _ in timing_entries]
    segments = _mc_segments(changes)
    raw_notes = data.get("note") or data.get("notes") or []
    if not isinstance(raw_notes, list):
        raise ChartConversionError(".mc note data must be a list")
    audio_filename, audio_delay_ms = _mc_audio_metadata(data, meta, song)
    audio_note_delay = None
    diagnostics = []
    bgm_indices = [i for i, item in enumerate(raw_notes) if _is_mc_audio_event(item)]
    if len(bgm_indices) > 1:
        diagnostics.append(ChartDiagnostic("mc-multiple-bgm", "多个主音乐声明无法可靠合成为单条主音乐，已拒绝输出", "error", len(bgm_indices)))
    notes = []
    pending_links = []
    max_column = -1
    for item in raw_notes:
        if not _is_mc_audio_event(item):
            continue
        candidate = _audio_path(_mapping_value(item, "sound", "music", "audio"))
        if candidate:
            audio_filename = candidate
        item_delay = _mapping_value(
            item, "audio_offset", "audioOffset", "audio_delay", "audioDelay", "offset"
        )
        if audio_note_delay is None and item_delay is not None:
            # Native Malody stores the chart/audio alignment on the BGM note.
            # Prefer it over legacy metadata aliases when present, including a
            # deliberate zero value.
            audio_note_delay = _safe_float(item_delay, audio_delay_ms)
    if audio_note_delay is not None:
        audio_delay_ms = audio_note_delay
    timings = []
    timing_beats = []
    for beat, bpm, meter in timing_entries:
        timings.append((_beat_to_ms_float(beat, segments) - audio_delay_ms, 60000.0 / bpm, meter))
        timing_beats.append(beat)
    scrolls = []
    raw_effect = data.get("effect")
    for effect_index, item in enumerate(raw_effect if isinstance(raw_effect, list) else ()):
        if not isinstance(item, dict):
            continue
        multiplier = _safe_float(item.get("scroll"), math.nan)
        if not math.isfinite(multiplier):
            # sign/jump/hs/showbar and friends are presentation effects outside
            # this converter's scope; they stay in ``mc_root`` and pass through
            # on a same-format rewrite.
            continue
        beat = _beat_value(item.get("beat", 0))
        scrolls.append(ChartScrollEvent(
            time=_beat_to_ms_float(beat, segments) - audio_delay_ms,
            multiplier=multiplier,
            beat=beat,
            extra=deepcopy(item),
            source_id=effect_index,
        ))
    mode_ext = meta.get("mode_ext") if isinstance(meta.get("mode_ext"), dict) else {}
    mode_hint = _safe_int(mode_ext.get("column"), 0)
    declared_mode = _safe_int(meta.get("mode"), -1)
    if declared_mode != 0 or isinstance(meta.get("mode"), bool) or _safe_float(meta.get("mode"), -1) != 0:
        raise ChartConversionError(
            f".mc 玩法声明 mode: {meta.get('mode')} 不是 Key 玩法的可靠声明（Key 为 mode: 0）；"
            "旧版 mode=键数与非 Key 玩法存在歧义，转谱仅支持 Malody Key，无法安全转谱"
        )
    if not (1 <= mode_hint <= 18):
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        mode_hint = _safe_int(info.get("key"), 0)
        diagnostics.append(ChartDiagnostic(
            "mc-key-width-inferred", "Key 谱面缺少有效的 mode_ext.column，按列位置推断键数，稀疏谱面可能不完整", "warning", 1,
        ))
    sound_events = []
    skipped_events = 0
    source_notes = {}
    for source_index, item in enumerate(raw_notes):
        if not isinstance(item, dict):
            skipped_events += 1
            continue
        if _is_mc_audio_event(item):
            continue
        if not any(key in item for key in ("column", "x", "where")) and _audio_path(item.get("sound")):
            beat = _beat_value(item.get("beat", 0))
            sound_events.append(ChartSoundEvent(
                _beat_to_ms_float(beat, segments) - audio_delay_ms,
                _audio_path(item["sound"]), _first_present(item, "vol", "volume"),
            ))
            continue
        where = item.get("where") if isinstance(item.get("where"), dict) else {}
        if "column" in item:
            column = _safe_int(item.get("column"), -1)
        elif "x" in item:
            column = _safe_int(item.get("x"), -1)
        elif "x" in where:
            raw_x = _safe_float(where.get("x"), -1.0)
            column = int(raw_x * (mode_hint or 4)) if 0.0 <= raw_x <= 1.0 else _safe_int(raw_x, -1)
        else:
            column = -1
        if column < 0:
            skipped_events += 1
            continue
        when = item.get("when") if isinstance(item.get("when"), dict) else {}
        raw_start = item.get("time", when.get("t"))
        beat_value = item.get("beat", when.get("b", 0))
        fallback_start = _beat_to_ms_float(_beat_value(beat_value), segments) - audio_delay_ms
        if isinstance(raw_start, (list, tuple)):
            start = _beat_to_ms_float(_beat_value(raw_start), segments) - audio_delay_ms
        elif raw_start is None:
            start = fallback_start
        else:
            start = _safe_float(raw_start, fallback_start) - audio_delay_ms
        start = int(round(start))
        end = None
        if "end_time" in item:
            raw_end = item.get("end_time")
            end = (_beat_to_ms_float(_beat_value(raw_end), segments) - audio_delay_ms
                   if isinstance(raw_end, (list, tuple))
                   else _safe_float(raw_end, start) - audio_delay_ms)
        elif "endbeat" in item:
            end = _beat_to_ms_float(_beat_value(item.get("endbeat")), segments) - audio_delay_ms
        if end is not None:
            end = int(round(end))
        # A positioned note carrying its own sound file is a playable
        # keysound, not the BGM: the note stays playable and keeps its sound.
        note_sound = _audio_path(_mapping_value(item, "sound", "music", "audio")) or ""
        note_volume = _first_present(item, "vol", "volume")
        notes.append(ChartNoteData(
            start, column, end if end is not None and end > start else None,
            note_type=NOTE_TYPE_HOLD if end is not None and end > start else NOTE_TYPE_TAP,
            sound=note_sound,
            sound_volume=_safe_int(note_volume, 100) if note_volume is not None else None,
            source_id=source_index,
            extra={"mc_note_fields": deepcopy({key: value for key, value in item.items()
                   if key not in {"beat", "endbeat", "time", "end_time", "when", "where", "column", "x", "link", "sound", "music", "audio", "vol", "volume"}})},
        ))
        source_notes[source_index] = notes[-1]
        pending_links.append((source_index, item.get("link")))
        max_column = max(max_column, column)
    consumed = set()
    proposals = []
    invalid_links = []
    for source_index, link in pending_links:
        if link is None:
            continue
        if not isinstance(link, dict) or any(key not in {"a", "b"} for key in link):
            invalid_links.append(source_index)
            continue
        if "b" in link and (not isinstance(link["b"], int) or isinstance(link["b"], bool)
                            or (link["b"] != -1 and link["b"] not in source_notes)):
            invalid_links.append(source_index)
            continue
        if "a" not in link or link["a"] == -1:
            continue
        target = _safe_int(link.get("a"), -1)
        head, tail = source_notes[source_index], source_notes.get(target)
        tail_link = raw_notes[target].get("link", {}) if tail is not None else {}
        valid = (
            isinstance(link.get("a"), int) and not isinstance(link.get("a"), bool)
            and tail is not None and target != source_index
            and head.column == tail.column and tail.time > head.time
            and head.end_time is None and tail.end_time is None
            and not tail.sound
            and isinstance(tail_link, dict)
            and _safe_int(tail_link.get("a"), -1) == -1
            and _safe_int(tail_link.get("b"), source_index) == source_index
            and not any(key not in {"a", "b"} for key in link)
        )
        if valid:
            proposals.append((source_index, target))
        else:
            invalid_links.append(source_index)
    target_counts = {}
    for head_id, tail_id in proposals:
        target_counts[tail_id] = target_counts.get(tail_id, 0) + 1
    for head_id, tail_id in proposals:
        if target_counts[tail_id] != 1 or head_id in target_counts:
            invalid_links.append(head_id)
            continue
        source_notes[head_id].end_time = source_notes[tail_id].time
        source_notes[head_id].note_type = NOTE_TYPE_HOLD
        consumed.add(tail_id)
    for source_index, link in pending_links:
        if isinstance(link, dict) and _safe_int(link.get("b"), -1) >= 0 and source_index not in consumed:
            invalid_links.append(source_index)
    if invalid_links:
        diagnostics.append(ChartDiagnostic(
            "mc-link-invalid", f"{len(set(invalid_links))} 个数组索引兼容链接无效（目标、方向、列或占用不一致），已拒绝输出",
            "error", len(set(invalid_links)),
        ))
    notes = [note for note in notes if note.source_id not in consumed]
    key_count = max(1, mode_hint if 1 <= mode_hint <= 18 else (max_column + 1 or 4))
    song_title = song.get("title") or meta.get("title", "")
    song_subtitle = song.get("subtitle") or meta.get("subtitle", "")
    song_artist = song.get("artist") or meta.get("artist", "")
    if not audio_filename:
        diagnostics.append(ChartDiagnostic("missing-bgm", "谱面未声明主音乐；保留按键音与自动声音角色，不以短音效替代主音乐", "warning", 1))
    if skipped_events:
        diagnostics.append(ChartDiagnostic(
            code="mc-unreadable-event",
            message=f"{skipped_events} 个无法识别的 .mc 事件已跳过（缺少列位置或无法解析）",
            severity="warning",
            count=skipped_events,
        ))
    return ChartDocument(
        title=_clean(song_title),
        subtitle=_clean(song_subtitle),
        artist=_clean(song_artist),
        creator=_clean(meta.get("creator") or meta.get("author")),
        version=_clean(meta.get("version") or meta.get("difficulty") or "Converted"),
        source=_clean(meta.get("source")),
        tags=_clean(meta.get("tags")),
        audio_filename=audio_filename,
        background_filename=_clean(meta.get("background") or song.get("background")),
        cover_filename=_clean(meta.get("cover") or song.get("cover") or meta.get("background")),
        offset_ms=audio_delay_ms,
        audio_delay_ms=audio_delay_ms,
        beat_zero_ms=-audio_delay_ms,
        key_count=key_count,
        notes=sorted(notes, key=lambda item: (item.time, item.column)),
        timings=timings or [(0.0, 500.0, 4)],
        timing_beats=timing_beats or [0.0],
        difficulty_name=_clean(meta.get("difficulty") or meta.get("version")),
        extra={"mc_root": data},
        scrolls=scrolls,
        sound_events=sound_events,
        diagnostics=diagnostics,
    )


def _parse_mc(path):
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError) as exc:
        raise ChartConversionError(f"Unable to read .mc chart: {exc}")
    return [_mc_parse_data(data)]


def _sm_tag_text(tags, name):
    """Return a tag's full value, unescaped and colon-rejoined."""
    fields = tags.get(name)
    if not fields:
        return ""
    return ":".join(_msd_unescape(item) for item in fields)


def _parse_sm_bpms(value):
    changes = []
    for item in value.split(","):
        if "=" not in item:
            continue
        beat, bpm = item.split("=", 1)
        number = _safe_float(bpm, 120.0)
        if number > 0:
            changes.append((_safe_float(beat), number))
    return [(beat, bpm) for beat, bpm, _ in _normalise_timing_entries(
        [(beat, bpm, 4) for beat, bpm in changes], default_bpm=120.0
    )]


def _parse_sm_events(value, kind):
    """Parse positive StepMania beat-duration events into canonical metadata."""
    events = []
    for item in value.split(","):
        if "=" not in item:
            continue
        beat, duration = item.split("=", 1)
        beat = _safe_float(beat, math.nan)
        duration_ms = _safe_float(duration, math.nan) * 1000.0
        if math.isfinite(beat) and math.isfinite(duration_ms) and duration_ms > 0:
            events.append((beat, duration_ms, kind))
    events.sort(key=lambda event: (event[0], event[2]))
    return events


# ``[index]`` after a note symbol references ``#KEYSOUNDS``. An unclosed span
# runs to the end of the row, which is how the reference loaders read it.
_SM_KEYSOUND_RE = re.compile(r"\[[^\]]*\]?")


def _sm_row_lines(measure):
    """Return the note rows of one measure, dropping comments and separators.

    A row may carry per-note keysound references as ``[index]``; ArrowVortex
    removes those spans while collecting a measure's symbols, so they must
    neither widen a lane row nor be read as note markers.
    """
    rows = []
    for raw in measure.splitlines():
        line = raw.strip()
        if not line:
            continue
        # A comment is removed by the MSD reader and leaves the row ending
        # short. Drop the tail instead of accepting a truncated lane row.
        line = re.split(r"//", line, maxsplit=1)[0]
        line = _SM_KEYSOUND_RE.sub("", line)
        # Readers ignore whitespace inside note data, so a spaced-out row is
        # still one lane row rather than one lane per character.
        line = re.sub(r"\s+", "", line)
        # ``&`` separates players; the shared model keeps the first player,
        # so the width must come from that player's lanes too.
        line = line.split("&", 1)[0]
        if not line:
            continue
        # Stray separators between measures must not be read as note rows.
        if not line.strip(","):
            continue
        rows.append(line)
    return rows


def _sm_keysound_total(measure_bodies):
    """Count the per-note keysound references the shared model cannot carry."""
    total = 0
    for measure in measure_bodies:
        for raw in measure.splitlines():
            total += len(_SM_KEYSOUND_RE.findall(re.split(r"//", raw, maxsplit=1)[0]))
    return total


def _sm_keysound_list(tags):
    """Return the ``#KEYSOUNDS`` files in index order.

    Standard lists use commas inside one field. Extra colon fields are
    accepted only for compatibility with earlier toolbox output.
    """
    fields = tags.get("KEYSOUNDS")
    if not fields:
        return []
    files = [_msd_unescape(item).strip() for item in fields]
    if len(files) == 1 and "," in files[0]:
        files = [item.strip() for item in files[0].split(",")]
    return [item for item in files if item]


def _sm_parse_row(line):
    """Return ``(lane_symbols, keysound_refs)`` for one note row.

    A ``[index]`` span belongs to the most recent non-empty lane symbol; a
    span written before any symbol attaches to the first non-empty lane of
    the row.  An unclosed span runs to the end of the row, which is how the
    reference loaders read it.  ``&`` starts another player's lanes: the
    shared model is single-player, so only the first player is returned and
    the caller reports the rest.
    """
    line = line.split("&", 1)[0]
    symbols = []
    refs = []
    column = 0
    last_note_column = None
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "[":
            end = line.find("]", index + 1)
            if end < 0:
                span = line[index + 1:]
                index = length
            else:
                span = line[index + 1:end]
                index = end + 1
            refs.append([last_note_column, span.strip()])
            continue
        if char.isspace():
            index += 1
            continue
        symbols.append(char)
        if char != "0":
            last_note_column = column
            for ref in refs:
                if ref[0] is None:
                    ref[0] = column
        column += 1
        index += 1
    return "".join(symbols), [(target, span) for target, span in refs]


# StepMania note-data markers the shared model understands.  ``0`` is an empty
# lane; ``1`` tap; ``2``/``4`` hold/roll head; ``3`` tail; ``M`` mine; ``L``
# lift; ``F`` fake; ``K`` automatic keysound.  Anything else is located,
# counted and reported instead of being silently dropped.
SM_NOTE_MARKERS = frozenset("01234MLFK")


def _sm_measure_note_rows(measure):
    """Yield ``(lane_symbols, keysound_refs)`` for each real note row."""
    rows = []
    for raw in measure.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.split(r"//", line, maxsplit=1)[0]
        symbols, refs = _sm_parse_row(line)
        symbols = re.sub(r"\s+", "", symbols)
        if not symbols:
            continue
        if not symbols.strip(","):
            continue
        rows.append((symbols, refs))
    return rows


def _sm_multiplayer(measure_bodies):
    """Return whether a row carries StepMania's ``&`` player separator."""
    for measure in measure_bodies:
        for raw in measure.splitlines():
            if "&" in re.split(r"//", raw, maxsplit=1)[0]:
                return True
    return False


def _sm_row_width(rows):
    """Return the lane count of a measure, ignoring trailing separators."""
    widths = set()
    for row in rows:
        # A measure-separating comma must never be counted as a lane.
        row = row.rstrip(",")
        if row.strip(","):
            widths.add(len(row))
    # The widest row is the real lane row: a dropped comment can only make a
    # row shorter, never longer than the lane count.
    return max(widths) if widths else 0


def _sm_key_count(measure_bodies):
    """Infer the lane count from real note rows only.

    The previous implementation used the longest line of the whole note body,
    so the trailing comma that separates two measures ("1100,") was counted as
    a fifth lane and every 4K chart was read back as 5K.
    """
    counts = []
    for measure in measure_bodies:
        rows = _sm_row_lines(measure)
        if not rows:
            continue
        width = _sm_row_width(rows)
        if width:
            counts.append(width)
    if not counts:
        return 4
    tally = {}
    for width in counts:
        tally[width] = tally.get(width, 0) + 1
    return max(tally.items(), key=lambda item: (item[1], item[0]))[0]


def _parse_sm(path):
    text = _read_text(path)
    tags = _sm_tag_values(text)
    title = _sm_single(tags, "TITLE")
    artist = _sm_single(tags, "ARTIST")
    subtitle = _sm_single(tags, "SUBTITLE")
    creator = _sm_single(tags, "CREDIT")
    music = _sm_single(tags, "MUSIC")
    jacket = _sm_single(tags, "JACKET")
    background = _sm_single(tags, "BACKGROUND")
    if not background:
        first = _sm_tag_text(tags, "BGCHANGES").split(",", 1)[0].strip()
        if "=" in first:
            background = _clean(first.split("=", 1)[1].split("=", 1)[0])
        elif first:
            background = _clean(first)
    offset_ms = _safe_float(_sm_single(tags, "OFFSET"), 0.0) * 1000.0
    beat_zero_ms = -offset_ms
    bpm_changes = _parse_sm_bpms(_sm_tag_text(tags, "BPMS"))
    timing_events = (
        _parse_sm_events(_sm_tag_text(tags, "STOPS"), "stop")
        + _parse_sm_events(_sm_tag_text(tags, "FREEZES"), "stop")
        + _parse_sm_events(_sm_tag_text(tags, "DELAYS"), "delay")
    )
    # Duplicate legacy aliases at one beat should not apply their duration
    # twice. Keep the first event by kind, while allowing a stop and a delay
    # at the same beat to coexist as StepMania does.
    deduplicated_events = {}
    for event in sorted(timing_events, key=lambda item: (item[0], item[2])):
        deduplicated_events.setdefault((event[0], event[2]), event)
    timing_events = list(deduplicated_events.values())
    segments = _mc_segments(bpm_changes)
    keysound_files = _sm_keysound_list(tags)
    charts = []
    skipped_chart_types = []
    for fields in _msd_values(text):
        if not fields or _msd_unescape(fields[0]).strip().upper() not in ("NOTES", "NOTES2"):
            continue
        # ``fields[0]`` is the tag name; the chart fields follow it.
        chart_fields = fields[1:]
        if len(fields) < 7:
            # StepMania requires at least 7 fields in a #NOTES tag and skips
            # charts with fewer fields rather than guessing.  A chart that
            # lost a field to an unescaped ``//`` comment lands here too.
            continue
        chart_type = _msd_unescape(chart_fields[0]).strip()
        chart_description = _msd_unescape(chart_fields[1]).strip()
        difficulty_name = _msd_unescape(chart_fields[2]).strip()
        meter = _normalise_sm_meter(_msd_unescape(chart_fields[3]).strip())
        if chart_type and chart_type.casefold() not in _SM_TYPE_COLUMNS:
            # A steps type outside the supported key/dance families cannot be
            # reinterpreted as a key chart; skipping it is explicit, and the
            # caller reports how many charts were skipped and why.
            skipped_chart_types.append((chart_type, difficulty_name))
            continue
        body = _sm_notes_body(fields)
        measures = body.split(",")
        keysound_total = _sm_keysound_total(measures)
        multiplayer = _sm_multiplayer(measures)
        # The lane count comes from the actual rows, which is also what both
        # StepMania and ArrowVortex do when they read the note data.
        key_count = max(1, _sm_key_count(measures))
        notes = []
        sound_events = []
        unknown_markers = {}
        dropped_keysound_refs = 0
        active = {}
        source_ordinal = 0
        # Every comma-delimited entry is a measure. Empty measures still
        # consume four beats and must therefore advance the measure index.
        for measure_index, measure in enumerate(measures):
            rows = _sm_measure_note_rows(measure)
            if not rows:
                continue
            for row_index, (row, refs) in enumerate(rows):
                beat = measure_index * 4.0 + row_index * 4.0 / len(rows)
                time = int(round(
                    _beat_to_ms_float(beat, segments)
                    + beat_zero_ms
                    + _event_delta_for_beat(timing_events, beat, include_at=True)
                ))
                row_notes = {}
                auto_events = {}
                for column, marker in enumerate(row):
                    if marker == "0":
                        continue
                    if marker not in SM_NOTE_MARKERS:
                        unknown_markers[marker] = unknown_markers.get(marker, 0) + 1
                        continue
                    if marker == "1":
                        # A tap is independent from any previous lane state.
                        active.pop(column, None)
                        note = ChartNoteData(time, column, source_id=source_ordinal)
                        notes.append(note)
                        row_notes[column] = note
                        source_ordinal += 1
                    elif marker in ("2", "4"):
                        # StepMania repeats the head marker for every hold-body
                        # row; only the first row creates a note.
                        if column not in active:
                            note_type = NOTE_TYPE_ROLL if marker == "4" else NOTE_TYPE_HOLD
                            note = ChartNoteData(time, column, note_type=note_type, source_id=source_ordinal)
                            active[column] = note
                            notes.append(note)
                            source_ordinal += 1
                        row_notes[column] = active[column]
                    elif marker == "3":
                        note = active.pop(column, None)
                        if note is not None:
                            note.end_time = time
                    elif marker == "K":
                        # An automatic keysound: a sound trigger on the
                        # timeline, not a note the player hits.
                        auto_events[column] = ChartSoundEvent(time, "", kind="auto", column=column)
                    else:
                        # Mine/lift/fake are point actions with no tail.
                        note_type = {
                            "M": NOTE_TYPE_MINE, "L": NOTE_TYPE_LIFT, "F": NOTE_TYPE_FAKE,
                        }[marker]
                        note = ChartNoteData(time, column, note_type=note_type, source_id=source_ordinal)
                        notes.append(note)
                        row_notes[column] = note
                        source_ordinal += 1
                for target, span in refs:
                    ref_index = _safe_int(span, -1) if span.isdigit() else -1
                    sound_file = (
                        keysound_files[ref_index]
                        if 0 <= ref_index < len(keysound_files) else ""
                    )
                    if target is None or (target not in row_notes and target not in auto_events):
                        dropped_keysound_refs += 1
                        continue
                    if target in auto_events:
                        auto_events[target].sound = sound_file
                        if not sound_file:
                            dropped_keysound_refs += 1
                        continue
                    note = row_notes[target]
                    if not sound_file:
                        dropped_keysound_refs += 1
                    elif note.sound:
                        # One note carries one keysound; stacking is reported,
                        # not silently merged or reduced.
                        dropped_keysound_refs += 1
                    else:
                        note.sound = sound_file
                sound_events.extend(auto_events.values())
        timings = [(
            _beat_to_ms_float(beat, segments)
            + beat_zero_ms
            + _event_delta_for_beat(timing_events, beat, include_at=True),
            60000.0 / bpm,
            4,
        ) for beat, bpm in bpm_changes]
        extra = {}
        if chart_type:
            extra["sm_source_chart_type"] = chart_type
        if keysound_total:
            # Kept so the conversion can report the keysounds it had to drop.
            extra["sm_keysound_count"] = keysound_total
        if multiplayer:
            extra["sm_multiplayer"] = True
        diagnostics = []
        if multiplayer:
            diagnostics.append(ChartDiagnostic(
                code="sm-multiplayer",
                message="含多人（&）谱面数据：仅保留第一个玩家的音符，其余玩家数据未转换",
                severity="loss",
            ))
        if unknown_markers:
            total_unknown = sum(unknown_markers.values())
            symbols = "、".join(repr(char) for char in sorted(unknown_markers))
            diagnostics.append(ChartDiagnostic(
                code="sm-unknown-marker",
                message=f"含 {total_unknown} 个无法识别的音符标记（{symbols}），已跳过",
                severity="loss",
                count=total_unknown,
            ))
        if dropped_keysound_refs:
            diagnostics.append(ChartDiagnostic(
                code="sm-keysound-ref-dropped",
                message=f"{dropped_keysound_refs} 处按键音引用无效或无法附加到音符，已丢弃",
                severity="loss",
                count=dropped_keysound_refs,
            ))
        charts.append(ChartDocument(
            title=title,
            subtitle=subtitle,
            artist=artist,
            creator=creator,
            version=difficulty_name or "Converted",
            audio_filename=music,
            background_filename=background,
            cover_filename=jacket or background,
            key_count=key_count,
            notes=sorted(notes, key=lambda item: (item.time, item.column)),
            timings=timings or [(-offset_ms, 500.0, 4)],
            timing_beats=[beat for beat, _ in bpm_changes] or [0.0],
            difficulty_name=difficulty_name,
            chart_type=_sm_chart_type_for_columns(chart_type or "dance-single", key_count),
            meter=str(meter),
            chart_description=chart_description,
            beat_zero_ms=beat_zero_ms,
            offset_ms=offset_ms,
            audio_delay_ms=offset_ms,
            timing_events=timing_events,
            sound_events=sound_events,
            extra=extra,
            diagnostics=diagnostics,
        ))
    if skipped_chart_types and charts:
        # Some difficulties were readable; the skipped ones are reported on
        # every surviving document so the conversion cannot hide them.
        names = "、".join(sorted({item[0] for item in skipped_chart_types}))
        for chart in charts:
            chart.diagnostics.append(ChartDiagnostic(
                code="sm-unsupported-steps-type",
                message=f"{len(skipped_chart_types)} 个难度使用不支持的舞型（{names}），未转换",
                severity="warning",
                count=len(skipped_chart_types),
            ))
    if not charts:
        unsupported = sorted({item[0] for item in skipped_chart_types})
        if skipped_chart_types:
            raise ChartConversionError(
                ".sm 谱面不含支持的按键舞型（不支持的类型："
                + "、".join(unsupported)
                + "）；转谱仅支持 "
                + "、".join(sorted(_SM_TYPE_COLUMNS))
            )
        raise ChartConversionError(".sm file does not contain a readable #NOTES chart")
    return charts


def parse_chart(path):
    extension = Path(path).suffix.lower()
    if extension == ".osu":
        return _parse_osu(path)
    if extension == ".mc":
        return _parse_mc(path)
    if extension == ".sm":
        return _parse_sm(path)
    raise ChartConversionError(f"Unsupported chart format: {extension}")


def _timing_for_time(document, time_ms):
    timing = document.timings[0] if document.timings else (0.0, 500.0, 4)
    for candidate in document.timings[1:]:
        if candidate[0] > time_ms:
            break
        timing = candidate
    return timing


def _document_timing_points(document):
    """Return ``(beat, time_ms, beat_length, meter)`` points in beat order."""
    timings = list(document.timings or ((0.0, 500.0, 4),))
    beats = list(document.timing_beats or ())
    if len(beats) != len(timings):
        # Backward-compatible fallback for documents created by older code:
        # assign beat zero to the first timing point and integrate forward.
        beats = [0.0]
        for index in range(1, len(timings)):
            previous_time, previous_length, _ = timings[index - 1]
            current_time = timings[index][0]
            beats.append(beats[-1] + (current_time - previous_time) / previous_length)
    points = [
        (
            _safe_float(beat),
            _safe_float(time) - _event_delta_at_beat(document, _safe_float(beat), include_at=True),
            _safe_float(length, 500.0),
            max(1, _safe_int(meter, 4)),
        )
        for beat, (time, length, meter) in zip(beats, timings)
        if _safe_float(length, 500.0) > 0
    ]
    points.sort(key=lambda item: (item[0], item[1]))
    return points or [(0.0, 0.0, 500.0, 4)]


def _timing_anchor_delta(document, points=None):
    """Return the correction from a timing map's zero to canonical beat 0."""
    points = points or _document_timing_points(document)
    if not points or abs(points[0][0]) > 1e-9:
        return 0.0
    return _safe_float(document.beat_zero_ms, points[0][1]) - points[0][1]


def _event_delta_for_beat(events, beat, include_at=False):
    """Return elapsed milliseconds contributed by timing events before a beat."""
    delta = 0.0
    for event_beat, duration_ms, kind in events or ():
        if event_beat < beat or (
            include_at and event_beat == beat and str(kind).casefold() == "delay"
        ):
            delta += _safe_float(duration_ms)
    return delta


def _event_delta_at_beat(document, beat, include_at=False):
    return _event_delta_for_beat(document.timing_events, beat, include_at=include_at)


def _base_time_to_beat(document, time_ms):
    points = _document_timing_points(document)
    # Timing points normally already contain canonical audio-relative times.
    # The delta matters for documents with an implicit/default timing point
    # (for example an .mc containing only a BGM offset).
    time_ms = _safe_float(time_ms) - _timing_anchor_delta(document, points)
    point = points[0]
    for candidate in points[1:]:
        if candidate[1] > time_ms:
            break
        point = candidate
    return point[0] + (time_ms - point[1]) / point[2]


def _base_beat_to_time(document, beat):
    points = _document_timing_points(document)
    beat = _safe_float(beat)
    point = points[0]
    for candidate in points[1:]:
        if candidate[0] > beat:
            break
        point = candidate
    return point[1] + (beat - point[0]) * point[2] + _timing_anchor_delta(document, points)


def _event_delta_through_beat(events, beat):
    """Return elapsed milliseconds of every event at or before ``beat``.

    This is the position *after* the beat's own notes: a delay at the beat
    happens before its notes and a stop after them, and both are over by the
    time the following interval starts.
    """
    delta = 0.0
    for event_beat, duration_ms, _kind in events or ():
        if _safe_float(event_beat) <= beat:
            delta += _safe_float(duration_ms)
    return delta


def _time_to_beat(document, time_ms):
    """Invert the canonical beat/time map, including stops and delays."""
    events = document.timing_events or ()
    if not events:
        return _base_time_to_beat(document, time_ms)

    points = _document_timing_points(document)
    boundaries = sorted({point[0] for point in points} | {float(event[0]) for event in events})
    if not boundaries:
        return _base_time_to_beat(document, time_ms)

    def event_adjustment(beat, include_at=False):
        return _event_delta_at_beat(document, beat, include_at=include_at)

    # Search each linear beat interval.  An interval starts once every event
    # at its left edge is over (a stop belongs to that edge, not to the
    # interval's beat progress) and ends right before the right edge's
    # same-beat events.  A timestamp inside a stop/delay gap has no unique
    # beat and snaps to the event anchor.
    intervals = []
    outer_length = max(4.0, abs(boundaries[-1] - boundaries[0]) + 4.0)
    intervals.append((boundaries[0] - outer_length, boundaries[0]))
    intervals.extend(zip(boundaries, boundaries[1:]))
    intervals.append((boundaries[-1], boundaries[-1] + outer_length))
    target = _safe_float(time_ms)
    for start_beat, end_beat in intervals:
        if end_beat <= start_beat:
            continue
        start_time = _base_beat_to_time(document, start_beat) + _event_delta_through_beat(events, start_beat)
        end_time = _base_beat_to_time(document, end_beat) + event_adjustment(end_beat, include_at=False)
        if start_time <= target <= end_time or end_time <= target <= start_time:
            base_start = _base_beat_to_time(document, start_beat)
            base_end = _base_beat_to_time(document, end_beat)
            if abs(base_end - base_start) < 1e-9:
                return start_beat
            ratio = (target - start_time) / (end_time - start_time)
            return start_beat + ratio * (end_beat - start_beat)
        # A stop/delay gap has no unique beat. Snap it to the event anchor:
        # a delay's gap sits before the notes at its beat, a stop's after.
        for event_beat, duration_ms, kind in events:
            if str(kind).casefold() == "delay":
                gap_start = _base_beat_to_time(document, event_beat) + event_adjustment(event_beat, include_at=False)
            else:
                gap_start = _base_beat_to_time(document, event_beat) + event_adjustment(event_beat, include_at=True)
            if gap_start < target < gap_start + _safe_float(duration_ms):
                return event_beat
    return _base_time_to_beat(document, target - event_adjustment(_base_time_to_beat(document, target), include_at=True))


def _beat_to_time(document, beat):
    """Convert a beat to canonical audio-relative milliseconds."""
    beat = _safe_float(beat)
    return _base_beat_to_time(document, beat) + _event_delta_at_beat(document, beat, include_at=True)


def _audio_alignment_ms(document):
    """Return the Malody/StepMania offset for canonical beat-zero time."""
    # Both formats express the position of beat 0 relative to the audio
    # origin as the negated elapsed time.  Do not use ``audio_delay_ms`` here:
    # for osu! that field is the independent AudioLeadIn pre-roll setting.
    # Timing events at beat zero (notably a StepMania delay) are part of the
    # actual beat-zero time even though ``beat_zero_ms`` remains the format's
    # explicit OFFSET alias. Query the canonical mapper so those events are
    # included exactly once when exporting to a format with an audio offset.
    return -_beat_to_time(document, 0.0)


# ---------------------------------------------------------------------------
# Conversion capability model
#
# Every conversion decision is driven by the tables below, never by ad-hoc
# writer defaults.  ``plan_conversion`` inspects the *actual* document
# features and returns the strategies the writer will execute, so the
# preflight and the writer always reach the same conclusion.
#
# Status levels:
# * ``full``       -- the target expresses the feature's necessary semantics
# * ``approximate``-- semantics partly kept; the difference is measurable
# * ``downgrade``  -- a registered explicit-loss strategy is applied
# * ``unsupported``-- no reliable handling; reported, and never silently
#                     turned into something playable
# ---------------------------------------------------------------------------

CAPABILITY_FULL = "full"
CAPABILITY_APPROXIMATE = "approximate"
CAPABILITY_DOWNGRADE = "downgrade"
CAPABILITY_UNSUPPORTED = "unsupported"

# Strategy codes referenced by the capability table and surfaced in
# diagnostics.  Each has a fixed Chinese explanation so the preflight
# summary and the emitted diagnostics never drift apart.
_STRATEGY_TEXT = {
    "keep": "完整保留",
    "roll-as-hold": "roll 转为普通长条（目标没有连续触发玩法）",
    "drop-special": "丢弃目标无法表达的特殊音符",
    "preserve-time": "保留点击的音乐相对时间（节拍/视觉为近似）",
    "drop-scroll": "丢弃滚动速度变化（不影响点击时间）",
    "drop-sound-event": "丢弃自动声音事件（目标无时间轴声音语义）",
    "scroll-as-sv": "滚动控制转为继承速度点",
    "volume-lost": "声音音量信息丢失",
    "drop-meter": "非 4/4 拍号未迁移",
}

# Per-target capability for note types and chart features.  These tables
# describe what this project implements and verifies, not what the format
# could theoretically carry.
TARGET_CAPABILITIES = {
    ".osu": {
        NOTE_TYPE_TAP: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_HOLD: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_ROLL: (CAPABILITY_DOWNGRADE, "roll-as-hold"),
        NOTE_TYPE_MINE: (CAPABILITY_DOWNGRADE, "drop-special"),
        NOTE_TYPE_LIFT: (CAPABILITY_DOWNGRADE, "drop-special"),
        NOTE_TYPE_FAKE: (CAPABILITY_DOWNGRADE, "drop-special"),
        "keysound": (CAPABILITY_FULL, "keep"),
        "auto_keysound": (CAPABILITY_DOWNGRADE, "drop-sound-event"),
        "bpm": (CAPABILITY_FULL, "keep"),
        "stop": (CAPABILITY_APPROXIMATE, "preserve-time"),
        "delay": (CAPABILITY_APPROXIMATE, "preserve-time"),
        "sv": (CAPABILITY_FULL, "keep"),
        "meter": (CAPABILITY_FULL, "keep"),
    },
    ".mc": {
        NOTE_TYPE_TAP: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_HOLD: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_ROLL: (CAPABILITY_DOWNGRADE, "roll-as-hold"),
        NOTE_TYPE_MINE: (CAPABILITY_DOWNGRADE, "drop-special"),
        NOTE_TYPE_LIFT: (CAPABILITY_DOWNGRADE, "drop-special"),
        NOTE_TYPE_FAKE: (CAPABILITY_DOWNGRADE, "drop-special"),
        "keysound": (CAPABILITY_FULL, "keep"),
        "auto_keysound": (CAPABILITY_DOWNGRADE, "drop-sound-event"),
        "bpm": (CAPABILITY_FULL, "keep"),
        "stop": (CAPABILITY_APPROXIMATE, "preserve-time"),
        "delay": (CAPABILITY_APPROXIMATE, "preserve-time"),
        "sv": (CAPABILITY_FULL, "scroll-as-sv"),
        "meter": (CAPABILITY_FULL, "keep"),
    },
    ".sm": {
        NOTE_TYPE_TAP: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_HOLD: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_ROLL: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_MINE: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_LIFT: (CAPABILITY_FULL, "keep"),
        NOTE_TYPE_FAKE: (CAPABILITY_FULL, "keep"),
        "keysound": (CAPABILITY_FULL, "keep"),
        "auto_keysound": (CAPABILITY_FULL, "keep"),
        "bpm": (CAPABILITY_FULL, "keep"),
        "stop": (CAPABILITY_FULL, "keep"),
        "delay": (CAPABILITY_FULL, "keep"),
        "sv": (CAPABILITY_DOWNGRADE, "drop-scroll"),
        "meter": (CAPABILITY_FULL, "keep"),
    },
}

_NOTE_TYPE_NAMES = {
    NOTE_TYPE_TAP: "普通",
    NOTE_TYPE_HOLD: "长条",
    NOTE_TYPE_ROLL: "roll",
    NOTE_TYPE_MINE: "地雷",
    NOTE_TYPE_LIFT: "抬手",
    NOTE_TYPE_FAKE: "fake",
    NOTE_TYPE_AUTO_KEYSOUND: "自动按键音",
}


def _note_type_label(note_type):
    """Return the display label: latin names get spaces, CJK names do not."""
    name = _NOTE_TYPE_NAMES.get(note_type, note_type)
    return f" {name} " if name.isascii() else name


@dataclass
class FeatureStrategy:
    """One feature's conversion decision for a concrete document."""

    feature: str
    status: str
    strategy: str
    count: int = 0

    @property
    def strategy_text(self):
        return _STRATEGY_TEXT.get(self.strategy, self.strategy)


@dataclass
class ConversionPlan:
    """The strategies one document converts with, preflight == writer."""

    target_format: str
    strategies: dict = field(default_factory=dict)
    output_text: str = ""
    diagnostics: list = field(default_factory=list)

    def strategy_for(self, feature, default=("full", "keep")):
        entry = self.strategies.get(feature)
        if entry is None:
            return default
        return entry.status, entry.strategy

    def note_action(self, note):
        """Return ``(action, strategy)`` for one note.

        ``action`` is ``"keep"`` (write as-is), ``"hold"`` (write as a plain
        hold) or ``"drop"`` (omit with a diagnostic).
        """
        note_type = note.note_type if note.note_type in NOTE_TYPES else NOTE_TYPE_TAP
        if note_type == NOTE_TYPE_TAP and note.end_time is not None and note.end_time > note.time:
            note_type = NOTE_TYPE_HOLD
        _status, strategy = self.strategy_for(note_type)
        if strategy == "keep":
            return "keep", strategy
        if strategy == "roll-as-hold":
            return "hold", strategy
        return "drop", strategy


def _document_note_type(document, note):
    if note.note_type in NOTE_TYPES and note.note_type != NOTE_TYPE_TAP:
        return note.note_type
    if note.end_time is not None and note.end_time > note.time:
        return NOTE_TYPE_HOLD
    return NOTE_TYPE_TAP


def _strategy_plan(document, target_format):
    """Inspect one document and return the strategies for ``target_format``.

    Only features actually present in the document produce entries, so a
    plain tap/hold chart never triggers theoretical-loss noise.
    """
    target_format = str(target_format).lower()
    table = TARGET_CAPABILITIES.get(target_format, {})
    strategies = {}

    def register(feature, count):
        if not count:
            return
        status, strategy = table.get(feature, (CAPABILITY_UNSUPPORTED, "drop-special"))
        strategies[feature] = FeatureStrategy(feature, status, strategy, count)

    type_counts = {}
    keysound_count = 0
    for note in document.notes:
        note_type = _document_note_type(document, note)
        type_counts[note_type] = type_counts.get(note_type, 0) + 1
        if note.sound:
            keysound_count += 1
    for note_type, count in sorted(type_counts.items()):
        register(note_type, count)
    register("keysound", keysound_count)
    register("auto_keysound", len(document.sound_events or ()))
    event_counts = {}
    for _event_beat, _duration, kind in document.timing_events or ():
        kind = str(kind).casefold()
        event_counts[kind] = event_counts.get(kind, 0) + 1
    register("stop", event_counts.get("stop", 0))
    register("delay", event_counts.get("delay", 0))
    register("sv", len(document.scrolls or ()))
    return ConversionPlan(target_format=target_format, strategies=strategies)


def _plan_diagnostics(plan):
    """Build the Chinese loss summary a conversion plan implies.

    Used by the preflight; writers emit the same findings through document
    diagnostics while they execute the identical strategies.
    """
    summary = []
    for feature in sorted(plan.strategies):
        entry = plan.strategies[feature]
        if entry.status == CAPABILITY_FULL:
            continue
        if feature == "stop" or feature == "delay":
            label = "stop" if feature == "stop" else "delay"
            summary.append(
                f"{entry.count} 个 {label}：{_STRATEGY_TEXT['preserve-time']}"
            )
        elif feature == "sv":
            summary.append(f"{entry.count} 个滚动速度变化：{entry.strategy_text}")
        elif feature == "auto_keysound":
            summary.append(f"{entry.count} 个自动声音事件：{entry.strategy_text}")
        elif feature == "keysound":
            summary.append(f"{entry.count} 个按键音：{entry.strategy_text}")
        else:
            summary.append(f"{entry.count} 个{_note_type_label(feature)}音符：{entry.strategy_text}")
    return summary


def _remap_reference(reference_map, value):
    """Return the archive-adjusted reference for ``value`` (identity by default)."""
    if not reference_map or not value:
        return value
    return reference_map.get(value, value)


def _planned_notes(document, plan):
    """Split notes by the plan into ``(kept, dropped, downgraded)``.

    ``kept`` is ``[(note, action)]`` in time order; ``dropped`` maps
    ``(note_type, strategy)`` to a count; ``downgraded`` maps the strategy to
    a count.  Every writer uses this single partition so the reported counts
    can never disagree with what was written.
    """
    kept = []
    dropped = {}
    downgraded = {}
    for note in sorted(document.notes, key=lambda item: (item.time, item.column)):
        action, strategy = plan.note_action(note)
        if action == "drop":
            key = (_document_note_type(document, note), strategy)
            dropped[key] = dropped.get(key, 0) + 1
            continue
        if action != "keep":
            downgraded[strategy] = downgraded.get(strategy, 0) + 1
        kept.append((note, action))
    return kept, dropped, downgraded


def _note_plan_diagnostics(document, dropped, downgraded):
    """Append the diagnostics implied by a note partition to ``document``."""
    for (note_type, strategy), count in sorted(dropped.items()):
        document.diagnostics.append(ChartDiagnostic(
            code=f"drop-{note_type}",
            message=f"{count} 个{_note_type_label(note_type)}音符：{_STRATEGY_TEXT.get(strategy, strategy)}",
            severity="loss",
            count=count,
        ))
    for strategy, count in sorted(downgraded.items()):
        document.diagnostics.append(ChartDiagnostic(
            code=f"downgrade-{strategy}",
            message=f"{count} 个 roll 音符：{_STRATEGY_TEXT.get(strategy, strategy)}",
            severity="loss",
            count=count,
        ))


def _pause_preservation_diagnostic(document, plan):
    """Report stop/delay handling when the target cannot express them."""
    total = 0
    for feature in ("stop", "delay"):
        entry = plan.strategies.get(feature)
        if entry is not None and entry.strategy == "preserve-time":
            total += entry.count
    if total:
        document.diagnostics.append(ChartDiagnostic(
            code="pause-preserve-time",
            message=(
                f"{total} 个 stop/delay 目标无法原生表达："
                "按音乐相对时间拟合音符，具体量化差异另行报告；节拍位置与视觉停顿为近似"
            ),
            severity="loss",
            count=total,
        ))


def _auto_sound_diagnostics(document, plan, kept_events):
    """Report automatic sound events the target dropped."""
    entry = plan.strategies.get("auto_keysound")
    if entry is None:
        return
    dropped = entry.count - kept_events
    if dropped > 0:
        document.diagnostics.append(ChartDiagnostic(
            code="drop-auto-keysound",
            message=f"{dropped} 个自动声音事件：{_STRATEGY_TEXT['drop-sound-event']}",
            severity="loss",
            count=dropped,
        ))


def _timing_sample_fields(document):
    """Count timing points whose sample/volume/kiai fields change something.

    Both red (BPM) and green (inherited/SV) osu! points carry sample set,
    sample index, volume and kiai fields.  They are not scroll or beat
    information; a target without those fields must report the loss instead
    of folding it into SV or dropping it silently.
    """
    records = document.extra.get("osu_timing_records")
    if records is not None:
        return sum(
            bool(_safe_int(parts[3], 1) not in (0, 1)
             or _safe_int(parts[4], 0) != 0
             or _safe_int(parts[5], 100) != 100
             or _safe_int(parts[7], 0) & 9)
            for parts in records if len(parts) >= 8
        )
    count = 0
    red_extras = document.extra.get("osu_red_extras", {})
    if isinstance(red_extras, dict):
        for sample_set, sample_index, volume, effects in red_extras.values():
            if (_safe_int(sample_set, 1) not in (0, 1)
                or _safe_int(sample_index, 0) != 0
                or _safe_int(volume, 100) != 100
                or _safe_int(effects, 0) & 9):
                count += 1
    for scroll in document.scrolls or ():
        if isinstance(scroll.extra, dict):
            continue
        extra = tuple(scroll.extra or ())
        if len(extra) >= 6:
            _meter, sample_set, sample_index, volume, _inherited, effects = extra[:6]
        elif len(extra) >= 5:
            _meter, sample_set, sample_index, volume, effects = extra[:5]
        else:
            continue
        if (
            _safe_int(sample_set, 1) != 1
            or _safe_int(sample_index, 0) != 0
            or _safe_int(volume, 100) != 100
            or _safe_int(effects, 0) & 1
        ):
            count += 1
    return count


def _timing_sample_diagnostics(document, target_format):
    """Report sample/volume/kiai changes a non-osu target cannot carry."""
    if target_format == ".osu":
        return
    count = _timing_sample_fields(document)
    if count:
        document.diagnostics.append(ChartDiagnostic(
            code="timing-sample-lost",
            message=(
                f"{count} 个时间点附带的音量/采样组/kiai/小节线变化未迁移"
                "（目标格式无对应字段；节拍与滚动已保留）"
            ),
            severity="loss",
            count=count,
        ))


def _clamped_column(document, column):
    """Clamp a note column into the declared key count.

    A column outside ``0..key_count-1`` comes from a source whose declared
    width disagrees with its notes; writing it through would either break the
    target file or silently move the note to another lane on re-read.
    """
    column = int(column)
    if 0 <= column < document.key_count:
        return column, False
    return min(document.key_count - 1, max(0, column)), True


def _column_clamp_diagnostic(document, clamped_count):
    if clamped_count:
        document.diagnostics.append(ChartDiagnostic(
            code="column-out-of-range",
            message=f"{clamped_count} 个音符的列超出声明键数范围，已钳制到边界列",
            severity="warning",
            count=clamped_count,
        ))


def _osu_difficulty_value(value):
    """Format an OD/HP override the way osu! writes it (0-10, decimals kept)."""
    number = max(0.0, min(10.0, _safe_float(value, 0.0)))
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _write_osu(document, path, reference_map=None, overall_difficulty=None, hp_drain_rate=None):
    plan = _strategy_plan(document, ".osu")
    x_for_column = lambda column: int((column + 0.5) * 512 / document.key_count)
    general = dict(document.extra.get("osu_general", {}))
    editor = dict(document.extra.get("osu_editor", {}))
    metadata = dict(document.extra.get("osu_metadata", {}))
    difficulty = dict(document.extra.get("osu_difficulty", {}))
    lines = ["osu file format v14", "", "[General]"]
    audio_filename = _remap_reference(reference_map, document.audio_filename)
    background_filename = _remap_reference(reference_map, document.background_filename)
    cover_filename = _remap_reference(reference_map, document.cover_filename)
    general.update({"AudioFilename": audio_filename, "Mode": "3"})
    # ``AudioLeadIn`` is an independent, non-negative pre-roll field in osu!.
    # Preserve it when a source format supplied one, but never synthesize it
    # from beat-zero alignment: doing so would move every note twice on import.
    explicit_lead_in = _safe_float(getattr(document, "audio_lead_in_ms", 0.0), 0.0)
    lead_in = max(0, int(round(explicit_lead_in)))
    general["AudioLeadIn"] = str(lead_in)
    for key, default in (("AudioLeadIn", "0"), ("PreviewTime", "-1"), ("SpecialStyle", "0")):
        general.setdefault(key, default)
    for key, value in general.items():
        lines.append(f"{key}: {value}")
    if editor:
        lines.extend(["", "[Editor]"])
        lines.extend(f"{key}: {value}" for key, value in editor.items())
    metadata.update({
        "Title": document.title, "TitleUnicode": document.title,
        "Subtitle": document.subtitle, "SubtitleUnicode": document.subtitle,
        "Artist": document.artist, "ArtistUnicode": document.artist,
        "Creator": document.creator, "Version": document.version,
        "Source": document.source, "Tags": document.tags,
    })
    lines.extend(["", "[Metadata]"])
    lines.extend(f"{key}: {value}" for key, value in metadata.items())
    difficulty.update({
        "HPDrainRate": difficulty.get("HPDrainRate", "5"),
        "CircleSize": str(document.key_count),
        "OverallDifficulty": difficulty.get("OverallDifficulty", "5"),
        "ApproachRate": difficulty.get("ApproachRate", "5"),
        "SliderMultiplier": difficulty.get("SliderMultiplier", "1.4"),
        "SliderTickRate": difficulty.get("SliderTickRate", "1"),
    })
    # OD/HP only exist on .osu charts.  A caller-supplied value replaces the
    # source's own field, so every converted .osu chart carries the same value.
    if hp_drain_rate is not None:
        difficulty["HPDrainRate"] = _osu_difficulty_value(hp_drain_rate)
    if overall_difficulty is not None:
        difficulty["OverallDifficulty"] = _osu_difficulty_value(overall_difficulty)
    lines.extend(["", "[Difficulty]"])
    lines.extend(f"{key}: {value}" for key, value in difficulty.items())
    lines.extend(["", "[TimingPoints]"])
    timing_start = len(lines)
    red_extras = document.extra.get("osu_red_extras", {})
    if not isinstance(red_extras, dict):
        red_extras = {}
    for time, beat_length, meter in document.timings or ((0.0, 500.0, 4),):
        sample_set, sample_index, volume, effects = red_extras.get(
            time, ("1", "0", "100", "0")
        )
        lines.append(
            f"{time:.3f},{beat_length:.12g},{meter},{sample_set},{sample_index},{volume},1,{effects}"
        )
    dropped_sv = 0
    for scroll in document.scrolls or ():
        multiplier = _safe_float(scroll.multiplier, 1.0)
        if multiplier <= 0:
            # osu! inherited points only express positive multipliers; a zero
            # or negative scroll speed has no representation.
            dropped_sv += 1
            continue
        beat_length = -100.0 / multiplier
        if isinstance(scroll.extra, (tuple, list)) and len(scroll.extra) >= 6:
            # Same-format rewrite: keep the sample set, volume and kiai
            # fields that rode on the source point (meter, sample set,
            # sample index, volume, the source inherited flag, effects).
            meter_text, sample_set, sample_index, volume, _inherited, effects = scroll.extra[:6]
            lines.append(
                f"{_safe_float(scroll.time):.3f},{beat_length:.12g},{meter_text},"
                f"{sample_set},{sample_index},{volume},0,{effects}"
            )
        elif isinstance(scroll.extra, (tuple, list)) and len(scroll.extra) >= 5:
            meter_text, sample_set, sample_index, volume, effects = scroll.extra[:5]
            lines.append(
                f"{_safe_float(scroll.time):.3f},{beat_length:.12g},{meter_text},"
                f"{sample_set},{sample_index},{volume},0,{effects}"
            )
        else:
            lines.append(f"{_safe_float(scroll.time):.3f},{beat_length:.12g},4,1,0,100,0,0")
    timing_lines = lines[timing_start:]
    source_state = document.extra.get("osu_timing_state")
    if source_state == (document.timings, document.scrolls):
        timing_lines = [",".join(((f"{_safe_float(parts[0]):.3f}" if abs(_safe_float(parts[0]) - round(_safe_float(parts[0]), 3)) < 1e-10 else parts[0]),) + parts[1:])
                        for parts in document.extra.get("osu_timing_records", ())]
    # Stable sorting retains source order on ties. Generated ties use BPM then SV.
    lines[timing_start:] = sorted(timing_lines, key=lambda line: _safe_float(line.split(",", 1)[0]))
    if dropped_sv:
        document.diagnostics.append(ChartDiagnostic(
            code="drop-nonpositive-sv",
            message=f"{dropped_sv} 个非正滚动速度（SV）变化无法表达为 osu! 继承点，已丢弃",
            severity="loss",
            count=dropped_sv,
        ))
    other_sections = document.extra.get("osu_sections", {})
    if not isinstance(other_sections, dict):
        other_sections = {}
    event_background = background_filename or cover_filename
    if event_background and "Events" not in other_sections:
        lines.extend([
            "",
            "[Events]",
            "//Background and Video events",
            f'0,0,"{event_background}",0,0',
        ])
    for section_name, section_lines in other_sections.items():
        lines.extend(["", f"[{section_name}]"])
        lines.extend(str(item) for item in section_lines)
    lines.extend(["", "[HitObjects]"])
    kept, dropped, downgraded = _planned_notes(document, plan)
    clamped_columns = 0
    for note, action in kept:
        column, was_clamped = _clamped_column(document, note.column)
        clamped_columns += int(was_clamped)
        is_hold = note.end_time is not None and note.end_time > note.time
        object_type = 128 if is_hold else 1
        output_time = int(round(note.time))
        extra = note.extra if isinstance(note.extra, dict) else {}
        hit_sound = _safe_int(extra.get("osu_hit_sound"), 0)
        sample_sets = extra.get("osu_sample_sets") or (0, 0, 0)
        sound = _remap_reference(reference_map, note.sound)
        volume = _safe_int(note.sound_volume, 0) if note.sound_volume is not None else 0
        hit_sample = f"{sample_sets[0]}:{sample_sets[1]}:{sample_sets[2]}:{volume}:{sound}"
        if is_hold:
            output_end = int(round(note.end_time))
            lines.append(
                f"{x_for_column(column)},192,{output_time},{object_type},{hit_sound},{output_end}:{hit_sample}"
            )
        else:
            lines.append(
                f"{x_for_column(column)},192,{output_time},{object_type},{hit_sound},{hit_sample}"
            )
    _column_clamp_diagnostic(document, clamped_columns)
    _note_plan_diagnostics(document, dropped, downgraded)
    _pause_preservation_diagnostic(document, plan)
    _auto_sound_diagnostics(document, plan, 0)
    return _store_chart_text(path, "\n".join(lines) + "\n")


def _mc_timing_entries(document, timing_points):
    """Return the ``time`` array for one Malody chart.

    ``signature`` is written only when it carries information:

    * it is not 4 (Malody's default, which its own exports omit), or
    * the source chart spelled it out, which happens for every entry of a chart
      that came from Malody itself and should survive a conversion unchanged.

    Without the second rule a Malody -> Malody conversion silently dropped a
    field the original had, which is what the round-trip check on real charts
    caught.
    """
    source_entries = []
    source_root = document.extra.get("mc_root") if isinstance(document.extra, dict) else None
    if isinstance(source_root, dict) and isinstance(source_root.get("time"), list):
        source_entries = source_root["time"]
    had_signature_anywhere = any(
        isinstance(item, dict) and "signature" in item for item in source_entries
    )
    entries = []
    for index, (beat, _time, beat_length, meter) in enumerate(timing_points):
        source = next((item for item in source_entries
                       if isinstance(item, dict)
                       and abs(_beat_value(item.get("beat", 0)) - beat) < 1e-10
                       and abs(_safe_float(item.get("bpm")) - 60000.0 / beat_length) < 1e-8), None)
        anchor = deepcopy(source["beat"]) if source and isinstance(source.get("beat"), list) else _beat_tuple(beat, 1000000)
        entry = {"beat": anchor, "bpm": 60000.0 / beat_length}
        meter = max(1, _safe_int(meter, 4))
        spelled_out = (
            index < len(source_entries)
            and isinstance(source_entries[index], dict)
            and "signature" in source_entries[index]
        )
        if meter != 4 or spelled_out or (had_signature_anywhere and index == 0):
            entry["signature"] = meter
        entries.append(entry)
    return entries


def _mc_beat_fit(document, notes_actions):
    """Fit note times with bounded-denominator rationals on the final timeline.

    Returns ``(fitted, stats)`` where ``fitted`` is
    ``[(note, action, start_tuple, end_tuple_or_None)]`` and ``stats``
    carries the worst head/tail round-trip error in milliseconds plus the
    number of hold tails the approximation collapsed onto their head.
    """
    fitted = []
    max_head_error = 0.0
    max_tail_error = 0.0
    clamped_tails = 0
    affected = 0
    collapsed_tail_error = 0.0
    for note, action in notes_actions:
        start_beat = _base_time_to_beat(document, note.time)
        start_tuple = _beat_tuple(start_beat)
        head_error = abs(_base_beat_to_time(document, _beat_value(start_tuple)) - note.time)
        max_head_error = max(max_head_error, head_error)
        end_tuple = None
        tail_error = 0.0
        if note.end_time is not None and note.end_time > note.time:
            end_beat = _base_time_to_beat(document, note.end_time)
            candidate = _beat_tuple(end_beat)
            tail_error = abs(_base_beat_to_time(document, _beat_value(candidate)) - note.end_time)
            max_tail_error = max(max_tail_error, tail_error)
            if _beat_value(candidate) <= _beat_value(start_tuple):
                # The approximation folded the tail onto the head; writing
                # it would erase the hold, so the tail is dropped and counted.
                clamped_tails += 1
                collapsed_tail_error = max(collapsed_tail_error, tail_error)
            else:
                end_tuple = candidate
                max_tail_error = max(max_tail_error, tail_error)
        affected += int(max(head_error, tail_error if end_tuple is not None else 0) > 0.5 + 1e-7)
        fitted.append((note, action, start_tuple, end_tuple))
    stats = {
        "max_head_error": max_head_error,
        "max_tail_error": max_tail_error,
        "clamped_tails": clamped_tails,
        "affected": affected,
        "collapsed_tail_error": collapsed_tail_error,
    }
    return fitted, stats


def _mc_precision_threshold_ms(document):
    """Threshold separating warning and loss severity for measured time errors."""
    return 1.0


def _write_mc(document, path, reference_map=None):
    plan = _strategy_plan(document, ".mc")
    root = document.extra.get("mc_root")
    if not isinstance(root, dict):
        root = {}
    root = dict(root)
    meta = dict(root.get("meta") or {})
    song = dict(meta.get("song") or {})
    song.update({"title": document.title, "artist": document.artist})
    if document.subtitle:
        song["subtitle"] = document.subtitle
    # Malody mode is an enum (0 = key), while the keyboard width belongs in mode_ext.column.
    mode_ext_value = meta.get("mode_ext")
    mode_ext = dict(mode_ext_value) if isinstance(mode_ext_value, dict) else {}
    mode_ext["column"] = int(document.key_count)
    meta.update({"mode": 0, "mode_ext": mode_ext, "song": song, "version": document.version})
    if document.creator:
        meta["creator"] = document.creator
    if document.source:
        meta["source"] = document.source
    if document.tags:
        meta["tags"] = document.tags
    background_filename = _remap_reference(reference_map, document.background_filename)
    cover_filename = _remap_reference(reference_map, document.cover_filename)
    audio_filename = _remap_reference(reference_map, document.audio_filename)
    if background_filename:
        meta["background"] = background_filename
    # ``cover`` is only a distinct field when it differs from the background;
    # synthesising it would add a metadata key to every chart that never had one
    # (a Malody chart uses ``background`` alone).
    if cover_filename and cover_filename != background_filename:
        meta["cover"] = cover_filename
    root["meta"] = meta
    root["time"] = _mc_timing_entries(document, _document_timing_points(document))
    audio_delay_ms = int(round(-_base_beat_to_time(document, 0.0)))
    final_segments = _mc_segments([(_beat_value(item["beat"]), item["bpm"]) for item in root["time"]])
    final_timeline = ChartDocument(
        timings=[(time - audio_delay_ms, 60000.0 / bpm, 4) for beat, time, bpm in final_segments],
        timing_beats=[beat for beat, time, bpm in final_segments],
        beat_zero_ms=-audio_delay_ms,
    )
    control_errors = [abs(_base_beat_to_time(final_timeline, _beat_value(entry["beat"]))
                          - (point[1] + _timing_anchor_delta(document)))
                      for entry, point in zip(root["time"], _document_timing_points(document))]
    affected_controls = [error for error in control_errors if error > 0.5 + 1e-7]
    if affected_controls:
        document.diagnostics.append(ChartDiagnostic(
            "mc-timing-precision", f"{len(affected_controls)} 个 BPM 锚点在最终 .mc 时间轴上最大时间误差 {max(affected_controls):.3f} ms",
            "loss" if max(affected_controls) > 1 else "warning", len(affected_controls),
        ))
    # Scroll changes use the same final beat timeline as the notes, so a source
    # stop/delay shifts them exactly like the clicks they accompany.  Effect
    # fields this converter does not model (sign/jump/hs/showbar) pass
    # through unchanged from a Malody source.
    effects = []
    sv_errors = []
    scroll_by_id = {scroll.source_id: scroll for scroll in document.scrolls if scroll.source_id is not None}
    raw_effect = root.get("effect")
    for effect_index, item in enumerate(raw_effect if isinstance(raw_effect, list) else ()):
        if not isinstance(item, dict):
            continue
        entry = deepcopy(item)
        scroll = scroll_by_id.get(effect_index)
        if scroll is not None:
            entry["scroll"] = _safe_float(scroll.multiplier, 1.0)
            # Keep the exact source anchor when it still describes this event.
            original_beat = _beat_value(entry.get("beat", 0))
            if abs(_base_beat_to_time(final_timeline, original_beat) - scroll.time) > 1e-7:
                entry["beat"] = _beat_tuple(_base_time_to_beat(final_timeline, scroll.time))
        if scroll is not None:
            sv_errors.append(abs(_base_beat_to_time(final_timeline, _beat_value(entry.get("beat", 0))) - scroll.time))
        effects.append(entry)
    for scroll in document.scrolls or ():
        if scroll.source_id is not None and scroll.source_id in scroll_by_id and isinstance(raw_effect, list):
            continue
        entry = {"beat": _beat_tuple(_base_time_to_beat(final_timeline, scroll.time)),
                 "scroll": _safe_float(scroll.multiplier, 1.0)}
        sv_errors.append(abs(_base_beat_to_time(final_timeline, _beat_value(entry["beat"])) - scroll.time))
        effects.append(entry)
    if effects:
        effects.sort(key=lambda item: _beat_value(item.get("beat", 0)))
        root["effect"] = effects
    elif "effect" in root:
        del root["effect"]
    raw_notes = root.get("note") if isinstance(root.get("note"), list) else []
    kept, dropped, downgraded = _planned_notes(document, plan)
    fitted, fit_stats = _mc_beat_fit(final_timeline, kept)
    chart_notes = []
    clamped_columns = 0
    for note, _action, start_tuple, end_tuple in fitted:
        column, was_clamped = _clamped_column(document, note.column)
        clamped_columns += int(was_clamped)
        extra = note.extra if isinstance(note.extra, dict) else {}
        entry = deepcopy(extra.get("mc_note_fields", {}))
        entry.update({"beat": start_tuple, "column": column})
        if end_tuple is not None:
            entry["endbeat"] = end_tuple
        if note.sound:
            # A positioned note with its own sound is a playable keysound.
            entry["sound"] = _remap_reference(reference_map, note.sound)
            if note.sound_volume is not None:
                entry["vol"] = _safe_int(note.sound_volume, 100)
        chart_notes.append(entry)
    sound_notes = [deepcopy(item) for item in raw_notes if _is_mc_audio_event(item)]
    # Malody has one offset value for chart/audio alignment. Emit the pure
    # beat-zero position of the BPM map: notes are placed on that same map,
    # so source stop/delay time is carried by the note beats, never hidden
    # inside the offset.
    #
    # ``offset`` is an integer field: every native .mc stores it as a whole
    # number of milliseconds, and a float makes Malody read the alignment
    # incorrectly.  Round to nearest rather than truncating so the alignment
    # stays as accurate as the millisecond timestamps allow.
    audio_delay_ms = int(round(-_base_beat_to_time(document, 0.0)))
    for item in sound_notes:
        # A carried-over note must not reintroduce a float offset either.
        item["offset"] = _mc_integer_offset(
            _first_present(item, "offset", "audio_offset", "audioOffset",
                           "audio_delay", "audioDelay")
        )
        item["vol"] = _safe_int(item.get("vol"), 100)
        if item.get("sound"):
            item["sound"] = _remap_reference(reference_map, item["sound"])
    if audio_filename or audio_delay_ms or sound_notes:
        sound_note = sound_notes[0] if sound_notes else {"beat": [0, 0, 1], "vol": 100, "type": 1}
        if not sound_notes:
            sound_note["beat"] = [0, 0, 1]
        sound_note.update({
            "sound": audio_filename,
            "offset": audio_delay_ms,
            "type": 1,
        })
        chart_notes.append(sound_note)
        chart_notes.extend(sound_notes[1:])
    root["note"] = chart_notes
    _column_clamp_diagnostic(document, clamped_columns)
    _note_plan_diagnostics(document, dropped, downgraded)
    _pause_preservation_diagnostic(document, plan)
    _auto_sound_diagnostics(document, plan, 0)
    _timing_sample_diagnostics(document, ".mc")
    if fit_stats["clamped_tails"]:
        document.diagnostics.append(ChartDiagnostic(
            code="mc-tail-clamped",
            message=f"{fit_stats['clamped_tails']} 个长条尾部在 .mc 拍位近似后与头部重合，已丢失（尾部最大误差 {fit_stats['collapsed_tail_error']:.3f} ms）",
            severity="loss",
            count=fit_stats["clamped_tails"],
        ))
    affected_sv = [error for error in sv_errors if error > 0.5 + 1e-7]
    if affected_sv:
        document.diagnostics.append(ChartDiagnostic(
            "mc-scroll-precision", f"{len(affected_sv)} 个滚动事件经 .mc 有理数近似后最大时间误差 {max(affected_sv):.3f} ms",
            "loss" if max(affected_sv) > 1 else "warning", len(affected_sv),
        ))
    threshold = _mc_precision_threshold_ms(document)
    worst = max(fit_stats["max_head_error"], fit_stats["max_tail_error"])
    if fit_stats["affected"]:
        document.diagnostics.append(ChartDiagnostic(
            code="mc-beat-precision",
            message=(
                f"{fit_stats['affected']} 个音符经 .mc 分母不超过 192 的有理数近似后，"
                f"头最大误差 {fit_stats['max_head_error']:.3f} ms，尾最大误差 {fit_stats['max_tail_error']:.3f} ms"
            ),
            severity="loss" if worst > threshold else "warning",
            count=fit_stats["affected"],
            detail=f"max_error_ms={worst:.3f}",
        ))
    return _store_chart_text(path, json.dumps(root, ensure_ascii=False, indent=2) + "\n")


# Markers written for each semantic note type in StepMania output.
_SM_TYPE_MARKER = {
    NOTE_TYPE_TAP: "1",
    NOTE_TYPE_HOLD: "2",
    NOTE_TYPE_ROLL: "4",
    NOTE_TYPE_MINE: "M",
    NOTE_TYPE_LIFT: "L",
    NOTE_TYPE_FAKE: "F",
    NOTE_TYPE_AUTO_KEYSOUND: "K",
}

# Complete long notes reserve their endpoints before point-marker priority is
# applied. Conflicting long notes are removed with both endpoints; point
# markers keep the existing same-cell priority without interrupting a hold.
_SM_MARKER_PRIORITY = {"3": 60, "2": 50, "4": 50, "1": 40, "M": 30, "K": 20, "L": 10, "F": 5}

_SM_MARKER_NAMES = {
    "1": "普通", "2": "长条头", "3": "长条尾", "4": "roll 头",
    "M": "地雷", "L": "抬手", "F": "fake", "K": "自动按键音",
}


@dataclass(eq=False)
class _SmPlacement:
    ordinal: int
    start_row: int
    end_row: object
    column: int
    marker: str
    source: object
    extended: bool = False


def _sm_beat_shift(document, plan):
    kept, _dropped, _downgraded = _planned_notes(document, plan)
    beats = [point[0] for point in _document_timing_points(document)] + [0.0]
    for note, _action in kept:
        beats.append(_time_to_beat(document, note.time))
        if note.end_time is not None:
            beats.append(_time_to_beat(document, note.end_time))
    beats.extend(_time_to_beat(document, _safe_float(event.time)) for event in document.sound_events or ())
    beats.extend(_safe_float(event[0]) for event in document.timing_events or ())
    return max(0.0, -min(beats))


def _sm_validate_layout(cells):
    """Check the generated endpoint pairs against their planning identities."""
    active = {}
    for (row, column), (marker, placement) in sorted(cells.items()):
        if marker in ("2", "4"):
            if column in active or placement.end_row is None or row != placement.start_row:
                raise ChartConversionError("SM 最终布局存在未配对或重叠的长条头，无法写出")
            active[column] = placement
        elif marker == "3":
            head = active.pop(column, None)
            if head is None or head.ordinal != placement.ordinal or row != head.end_row or row <= head.start_row:
                raise ChartConversionError("SM 最终布局存在孤立尾部或长条头尾身份不一致，无法写出")
        elif marker == "1" and column in active:
            raise ChartConversionError("SM 最终布局的普通音符中断了长条，无法写出")
    if active:
        raise ChartConversionError("SM 最终布局存在缺少尾部的长条，无法写出")


def _sm_grid_layout(document, plan, final_timeline):
    """Fit complete notes on global rows using the serialized time controls."""
    _kept, dropped, downgraded = _planned_notes(document, plan)
    placements = []
    clamped_columns = 0
    for ordinal, note in enumerate(document.notes):
        action, _strategy = plan.note_action(note)
        if action == "drop":
            continue
        column, clamped = _clamped_column(document, note.column)
        clamped_columns += int(clamped)
        marker = "2" if action == "hold" else _SM_TYPE_MARKER.get(_document_note_type(document, note), "1")
        start = _sm_global_row(_time_to_beat(final_timeline, note.time))[0]
        end = None
        extended = False
        if marker in ("2", "4"):
            if note.end_time is None or note.end_time <= note.time:
                raise ChartConversionError("SM 长条缺少有效尾部，无法生成完整头尾")
            end = _sm_global_row(_time_to_beat(final_timeline, note.end_time))[0]
            extended = end <= start
            end = max(start + 1, end)
        placements.append(_SmPlacement(ordinal, start, end, column, marker, note, extended))
    for index, event in enumerate(document.sound_events or ()):
        column, clamped = _clamped_column(document, _safe_int(getattr(event, "column", 0), 0))
        clamped_columns += int(clamped)
        start = _sm_global_row(_time_to_beat(final_timeline, _safe_float(event.time)))[0]
        placements.append(_SmPlacement(len(document.notes) + index, start, None, column, "K", event))

    cells, intervals, long_conflicts = {}, {}, {}
    for placement in sorted((item for item in placements if item.end_row is not None),
                            key=lambda item: (item.start_row, item.ordinal)):
        lane = intervals.setdefault(placement.column, [])
        previous = lane[-1] if lane else None
        if previous is not None and placement.start_row <= previous.end_row:
            if max(previous.source.time, placement.source.time) <= min(previous.source.end_time, placement.source.end_time):
                reason = "source"
            elif previous.extended and placement.start_row == previous.end_row:
                reason = "extension"
            else:
                reason = "quantized"
            long_conflicts[reason] = long_conflicts.get(reason, 0) + 1
            continue
        lane.append(placement)
        cells[(placement.start_row, placement.column)] = (placement.marker, placement)
        cells[(placement.end_row, placement.column)] = ("3", placement)

    conflicts = {}
    interruptions = 0
    lane_cursors = {}
    for placement in sorted((item for item in placements if item.end_row is None),
                            key=lambda item: (item.start_row, item.column, item.source.time, item.ordinal)):
        key = (placement.start_row, placement.column)
        lane = intervals.get(placement.column, ())
        cursor = lane_cursors.get(placement.column, 0)
        while cursor < len(lane) and lane[cursor].end_row < placement.start_row:
            cursor += 1
        lane_cursors[placement.column] = cursor
        if (placement.marker == "1" and cursor < len(lane)
                and lane[cursor].start_row < placement.start_row < lane[cursor].end_row):
            interruptions += 1
            continue
        previous = cells.get(key)
        if previous is not None:
            if _SM_MARKER_PRIORITY[placement.marker] >= _SM_MARKER_PRIORITY[previous[0]]:
                conflict = (previous[0], placement.marker)
                cells[key] = (placement.marker, placement)
            else:
                conflict = (placement.marker, previous[0])
            conflicts[conflict] = conflicts.get(conflict, 0) + 1
            continue
        cells[key] = (placement.marker, placement)
    _sm_validate_layout(cells)

    stats = {
        "conflicts": conflicts, "long_conflicts": long_conflicts,
        "interruptions": interruptions, "extended_holds": 0,
        "max_extension_ms": 0.0, "max_extended_tail_error": 0.0,
        "max_head_error": 0.0, "max_tail_error": 0.0, "affected_notes": 0,
        "max_sound_error": 0.0, "affected_sound_events": 0,
        "dropped": dropped, "downgraded": downgraded, "clamped_columns": clamped_columns,
    }
    surviving = {placement.ordinal: placement for _marker, placement in cells.values()}
    document.extra["_sm_surviving_notes"] = [item.source for item in surviving.values() if isinstance(item.source, ChartNoteData)]
    document.extra["_sm_surviving_events"] = [item.source for item in surviving.values() if isinstance(item.source, ChartSoundEvent)]
    approximate_features = set()
    for placement in surviving.values():
        actual_head = _beat_to_time(final_timeline, placement.start_row / 48.0)
        head_error = abs(actual_head - placement.source.time)
        tail_error = 0.0
        if placement.end_row is not None:
            actual_tail = _beat_to_time(final_timeline, placement.end_row / 48.0)
            tail_error = abs(actual_tail - placement.source.end_time)
            if placement.extended:
                stats["extended_holds"] += 1
                stats["max_extension_ms"] = max(stats["max_extension_ms"], abs(actual_tail - actual_head))
                stats["max_extended_tail_error"] = max(stats["max_extended_tail_error"], tail_error)
        affected = max(head_error, tail_error) > 0.5 + 1e-7
        feature = (NOTE_TYPE_AUTO_KEYSOUND if placement.marker == "K"
                   else _document_note_type(document, placement.source))
        if affected or placement.extended:
            approximate_features.add(feature)
        if (isinstance(placement.source, ChartNoteData)
                and head_error > 0.5 + 1e-7 and placement.source.sound):
            approximate_features.add("keysound")
        if placement.marker == "K":
            stats["max_sound_error"] = max(stats["max_sound_error"], head_error)
            stats["affected_sound_events"] += int(affected)
        else:
            stats["max_head_error"] = max(stats["max_head_error"], head_error)
            stats["max_tail_error"] = max(stats["max_tail_error"], tail_error)
            stats["affected_notes"] += int(affected)
    document.extra["_sm_approximate_features"] = approximate_features
    rows_by_measure = {}
    for (global_row, column), entry in cells.items():
        measure, row = divmod(global_row, SM_ROWS_PER_MEASURE)
        _count, marks = rows_by_measure.setdefault(measure, (SM_ROWS_PER_MEASURE, {}))
        marks.setdefault(row, {})[column] = entry
    return rows_by_measure, stats


def _write_sm(document, path, reference_map=None):
    plan = _strategy_plan(document, ".sm")
    timing_points = _document_timing_points(document)
    beat_shift = _sm_beat_shift(document, plan)
    bpm_values = []
    for beat, _, beat_length, _ in timing_points:
        shifted_beat = beat + beat_shift
        value = f"{shifted_beat:.6f}={60000.0 / beat_length:.6f}"
        if bpm_values and abs(shifted_beat - float(bpm_values[-1].split("=", 1)[0])) < 1e-6:
            bpm_values[-1] = value
        else:
            bpm_values.append(value)
    if beat_shift and (not bpm_values or not bpm_values[0].startswith("0.")):
        active_point = timing_points[0]
        for candidate in timing_points[1:]:
            if candidate[0] > -beat_shift:
                break
            active_point = candidate
        synthetic = f"0.000000={60000.0 / active_point[2]:.6f}"
        if not bpm_values or abs(float(bpm_values[0].split("=", 1)[0])) > 1e-6:
            bpm_values.insert(0, synthetic)

    # Keysound index: every referenced sound file gets a stable slot and the
    # note rows reference it.  The list is rebuilt from the notes/events that
    # survive the plan, so the file never names an unused sound.
    keysound_files = []
    keysound_index = {}

    def keysound_slot(sound):
        sound = _remap_reference(reference_map, sound)
        if not sound:
            return None
        if sound not in keysound_index:
            keysound_index[sound] = len(keysound_files)
            keysound_files.append(sound)
        return keysound_index[sound]

    base_row_count = SM_ROWS_PER_MEASURE
    sm_offset_ms = -_base_beat_to_time(document, -beat_shift)
    serialized_offset = float(f"{sm_offset_ms / 1000.0:.6f}") * 1000.0
    final_segments = _mc_segments([tuple(float(v) for v in entry.split("=")) for entry in bpm_values])
    final_events = [(round(_safe_float(beat) + beat_shift, 6), round(_safe_float(duration) / 1000, 6) * 1000, kind)
                    for beat, duration, kind in document.timing_events if str(kind).casefold() in ("stop", "delay")]
    final_timeline = ChartDocument(
        timings=[(time - serialized_offset + _event_delta_for_beat(final_events, beat, include_at=True),
                  60000.0 / bpm, 4) for beat, time, bpm in final_segments],
        timing_beats=[beat for beat, time, bpm in final_segments],
        beat_zero_ms=-serialized_offset,
        timing_events=final_events,
    )
    rows_by_measure, stats = _sm_grid_layout(document, plan, final_timeline)

    max_measure = max(rows_by_measure, default=0)
    measures = []
    for measure_index in range(max_measure + 1):
        row_count, lane_marks = rows_by_measure.get(measure_index, (base_row_count, {}))
        rows = [None] * row_count
        for row in range(row_count):
            marks = lane_marks.get(row)
            if not marks:
                rows[row] = "0" * document.key_count
                continue
            cells = []
            for column in range(document.key_count):
                entry = marks.get(column)
                if entry is None:
                    cells.append("0")
                    continue
                marker, placement = entry
                cell = marker
                sound = getattr(placement.source, "sound", "") if marker != "3" else ""
                slot = keysound_slot(sound) if sound else None
                if slot is not None:
                    cell += f"[{slot}]"
                cells.append(cell)
            rows[row] = "".join(cells)
        measures.append("\n".join(rows))
    if measures:
        # No leading newline: ArrowVortex counts lanes from the first note row
        # and StepMania counts rows as symbols/columns, so a blank first line
        # would shift every note by one row.  Commas only separate measures, so
        # the last one is omitted as well.
        body = ",\n".join(measures)
    else:
        body = "\n".join(["0" * document.key_count] * base_row_count)
    # The shifted event tags carry their own elapsed time. Keep OFFSET limited
    # to the beat-zero baseline so a delay at output beat 0 is not counted a
    # second time when the file is loaded again.
    sm_offset_ms = -_base_beat_to_time(document, -beat_shift)
    if abs(sm_offset_ms) < 0.0005:
        sm_offset_ms = 0.0
    chart_type = _sm_chart_type_for_columns(document.chart_type, document.key_count)
    chart_description, difficulty = _sm_difficulty_fields(document)
    chart_fields = (
        _normalise_sm_field(chart_type, notes_field=True),
        chart_description,
        difficulty,
        _normalise_sm_meter(document.meter),
    )
    background_filename = _remap_reference(reference_map, document.background_filename)
    cover_filename = _remap_reference(reference_map, document.cover_filename)
    audio_filename = _remap_reference(reference_map, document.audio_filename)
    header = []
    if background_filename:
        header.append(f"#BACKGROUND:{_normalise_sm_resource(background_filename)};")
    if cover_filename and cover_filename != background_filename:
        header.append(f"#JACKET:{_normalise_sm_resource(cover_filename)};")
    lines = [
        f"#TITLE:{_normalise_sm_field(document.title)};",
        f"#SUBTITLE:{_normalise_sm_field(document.subtitle)};",
        f"#ARTIST:{_normalise_sm_field(document.artist)};",
    ]
    lines.extend(header)
    lines.extend([
        f"#MUSIC:{_normalise_sm_resource(audio_filename)};",
        f"#OFFSET:{sm_offset_ms / 1000.0:.6f};",
        f"#BPMS:{','.join(bpm_values)};",
    ])
    event_values = {"stop": [], "delay": []}
    for event_beat, duration_ms, kind in document.timing_events or ():
        kind = str(kind).casefold()
        if kind not in event_values:
            continue
        event_values[kind].append(
            f"{_safe_float(event_beat) + beat_shift:.6f}={_safe_float(duration_ms) / 1000.0:.6f}"
        )
    if event_values["stop"]:
        lines.append(f"#STOPS:{','.join(event_values['stop'])};")
    if event_values["delay"]:
        lines.append(f"#DELAYS:{','.join(event_values['delay'])};")
    if keysound_files:
        if any("," in name or "//" in name for name in keysound_files):
            raise ChartConversionError("SM 按键音路径包含无法可靠表达的逗号或连续斜线，无法写出")
        escaped = ",".join(_msd_escape(name) for name in keysound_files)
        lines.append(f"#KEYSOUNDS:{escaped};")
    if document.creator:
        lines.append(f"#CREDIT:{_normalise_sm_field(document.creator)};")
    # Every chart field ends with ':' and the note data follows on the same
    # line; the value is closed by the final ';'.  Keeping the fields on one
    # line means a stray '#' can never be mistaken for a missing ';' recovery.
    lines.extend([
        "",
        "#NOTES:",
        "".join(f"{field}:" for field in chart_fields)
        + "0.000,0.000,0.000,0.000,0.000:"
        + body
        + ";",
        "",
    ])
    _note_plan_diagnostics(document, stats["dropped"], stats["downgraded"])
    _column_clamp_diagnostic(document, stats["clamped_columns"])
    _timing_sample_diagnostics(document, ".sm")
    sv_entry = plan.strategies.get("sv")
    if sv_entry is not None and sv_entry.strategy == "drop-scroll":
        document.diagnostics.append(ChartDiagnostic(
            code="drop-scroll",
            message=f"{sv_entry.count} 个滚动速度（SV）变化：{_STRATEGY_TEXT['drop-scroll']}",
            severity="loss",
            count=sv_entry.count,
        ))
    if stats["conflicts"]:
        total = sum(stats["conflicts"].values())
        breakdown = "、".join(
            f"{_SM_MARKER_NAMES.get(dropped_m, dropped_m)}→{_SM_MARKER_NAMES.get(kept_m, kept_m)} {count} 处"
            for (dropped_m, kept_m), count in sorted(stats["conflicts"].items())
        )
        document.diagnostics.append(ChartDiagnostic(
            code="sm-grid-conflict",
            message=(
                f"{total} 个音符标记在 SM 网格上同格冲突被丢弃（{breakdown}）；"
                "保留优先级：尾部＞长条头＞普通＞特殊，不做移动"
            ),
            severity="loss",
            count=total,
        ))
    if stats["long_conflicts"]:
        labels = {"source": "原时间重叠或首尾相接", "quantized": "量化后发生碰撞", "extension": "最小一行延长后发生碰撞"}
        total = sum(stats["long_conflicts"].values())
        reasons = "、".join(f"{labels[reason]} {count} 条" for reason, count in sorted(stats["long_conflicts"].items()))
        document.diagnostics.append(ChartDiagnostic(
            "sm-hold-conflict", f"{total} 条长条在最终 SM 布局中同轨无法共存，已整条丢弃（{reasons}）；"
            "按量化头位置和稳定源序号保留先建立的完整长条，同时撤销被丢弃长条的头尾和声音引用", "loss", total,
        ))
    if stats["interruptions"]:
        total = stats["interruptions"]
        document.diagnostics.append(ChartDiagnostic(
            "sm-hold-interruption", f"{total} 个普通音符位于最终 SM 布局的已保留长条内部，已丢弃以保持完整头尾", "loss", total,
        ))
    if stats["extended_holds"]:
        document.diagnostics.append(ChartDiagnostic(
            code="sm-hold-extended",
            message=(
                f"{stats['extended_holds']} 个过短长条在 SM 网格上被延长至最小一行"
                f"（量化尾部再延长最大 {stats['max_extension_ms']:.3f} ms；"
                f"相对源尾最终误差最大 {stats['max_extended_tail_error']:.3f} ms）"
            ),
            severity="info",
            count=stats["extended_holds"],
        ))
    points = timing_points or [(0.0, 0.0, 500.0, 4)]
    threshold = max(point[2] for point in points) / 96.0 + 1.0
    worst = max(stats["max_head_error"], stats["max_tail_error"])
    if stats["affected_notes"]:
        document.diagnostics.append(ChartDiagnostic(
            code="sm-grid-precision",
            message=(
                f"{stats['affected_notes']} 个音符经 SM 网格（192 行/小节）拟合后，"
                f"头最大误差 {stats['max_head_error']:.3f} ms，尾最大误差 {stats['max_tail_error']:.3f} ms"
            ),
            severity="loss" if worst > threshold else "warning",
            count=stats["affected_notes"],
            detail=f"max_error_ms={worst:.3f}",
        ))
    if stats["affected_sound_events"]:
        sound_error = stats["max_sound_error"]
        document.diagnostics.append(ChartDiagnostic(
            code="sm-sound-precision",
            message=(
                f"{stats['affected_sound_events']} 个自动声音事件经 SM 网格（192 行/小节）拟合后，"
                f"触发时间最大误差 {sound_error:.3f} ms"
            ),
            severity="loss" if sound_error > threshold else "warning",
            count=stats["affected_sound_events"],
            detail=f"max_error_ms={sound_error:.3f}",
        ))
    return _store_chart_text(path, "\n".join(lines))


def _store_chart_text(path, text):
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text


def _content_loss_diagnostics(document, plan):
    kept, _dropped, _downgraded = _planned_notes(document, plan)
    if plan.target_format == ".sm":
        kept = [(note, "keep") for note in document.extra.get("_sm_surviving_notes", ())]
    if plan.target_format != ".osu":
        def default_sound_changed(note):
            extra = note.extra if isinstance(note.extra, dict) else {}
            flags = _safe_int(extra.get("osu_hit_sound"), 0) & 14
            sets = list(extra.get("osu_sample_sets", ())) + [0, 0, 0]
            general = document.extra.get("osu_general", {})
            default_set = {"normal": 1, "soft": 2, "drum": 3}.get(str(general.get("SampleSet", "Normal")).casefold(), 1)
            normal_set = sets[0] or default_set
            return bool(flags or (not note.sound and (normal_set != 1 or sets[2])))
        count = sum(default_sound_changed(note) for note, _action in kept)
        if count:
            document.diagnostics.append(ChartDiagnostic(
                "osu-default-sound-lost", f"{count} 个音符的默认音效标记/采样组未迁移（不生成默认皮肤音效文件）", "loss", count,
            ))
    if plan.target_format == ".osu":
        count = sum(bool(note.sound) and note.sound_volume == 0 for note, _action in kept)
        if count:
            document.diagnostics.append(ChartDiagnostic(
                "osu-zero-volume-lost", f"{count} 个按键音的零音量无法保留为 osu! 静音（0 表示继承时间点音量）", "loss", count,
            ))
    if plan.target_format == ".sm":
        meter_count = sum(point[3] != 4 for point in _document_timing_points(document))
        if meter_count:
            document.diagnostics.append(ChartDiagnostic(
                "sm-meter-lost", f"{meter_count} 个非 4/4 拍号未迁移（当前 SM 写出器没有拍号输出）", "loss", meter_count,
            ))
        count = sum(note.sound_volume not in (None, 100) and bool(note.sound) for note, _action in kept)
        count += sum(event.volume not in (None, 100) for event in document.extra.get("_sm_surviving_events", ()))
        if count:
            document.diagnostics.append(ChartDiagnostic(
                "sound-volume-lost", f"{count} 个声音的非默认音量未迁移（SM 按键音索引没有音量字段）", "loss", count,
            ))
    if plan.target_format != ".mc":
        root = document.extra.get("mc_root", {})
        effects = root.get("effect", ()) if isinstance(root, dict) else ()
        fields = sorted({key for item in effects if isinstance(item, dict)
                         for key in item if key not in {"beat", "scroll"}})
        count = sum(isinstance(item, dict) and any(key not in {"beat", "scroll"} for key in item) for item in effects)
        if count:
            document.diagnostics.append(ChartDiagnostic(
                "mc-effect-fields-lost", f"{count} 个 effect 对象的附加字段未迁移：{'、'.join(fields)}", "loss", count,
            ))


def plan_conversion(document, target_format, reference_map=None, overall_difficulty=None, hp_drain_rate=None):
    """Build deterministic final text and findings without changing the source."""
    target_format = archive_chart_extension(str(target_format).lower())
    plan = _strategy_plan(document, target_format)
    candidate = deepcopy(document)
    # Previously emitted findings must not accumulate on repeated planning.
    candidate.diagnostics = []
    if target_format == ".osu":
        plan.output_text = _write_osu(candidate, None, reference_map, overall_difficulty, hp_drain_rate)
    elif target_format == ".mc":
        plan.output_text = _write_mc(candidate, None, reference_map)
    elif target_format == ".sm":
        plan.output_text = _write_sm(candidate, None, reference_map)
    else:
        raise ChartConversionError(f"不支持的输出格式：{target_format}")
    _content_loss_diagnostics(candidate, plan)
    plan.diagnostics = candidate.diagnostics
    codes = {item.code for item in plan.diagnostics}
    if target_format == ".sm":
        approximate_features = candidate.extra.get("_sm_approximate_features", ())
    elif codes & {"mc-beat-precision", "mc-tail-clamped"}:
        approximate_features = (NOTE_TYPE_TAP, NOTE_TYPE_HOLD)
    else:
        approximate_features = ()
    for feature in approximate_features:
        entry = plan.strategies.get(feature)
        if entry is not None and entry.status == CAPABILITY_FULL:
            entry.status = CAPABILITY_APPROXIMATE
    if "sm-hold-conflict" in codes:
        for feature in (NOTE_TYPE_HOLD, NOTE_TYPE_ROLL):
            if feature in plan.strategies:
                plan.strategies[feature].status = CAPABILITY_DOWNGRADE
    if "sm-hold-interruption" in codes and NOTE_TYPE_TAP in plan.strategies:
        plan.strategies[NOTE_TYPE_TAP].status = CAPABILITY_DOWNGRADE
    if "drop-nonpositive-sv" in codes and "sv" in plan.strategies:
        plan.strategies["sv"].status = CAPABILITY_DOWNGRADE
    if "mc-scroll-precision" in codes and "sv" in plan.strategies:
        plan.strategies["sv"].status = CAPABILITY_APPROXIMATE
    if codes & {"sound-volume-lost", "osu-zero-volume-lost"} and "keysound" in plan.strategies:
        plan.strategies["keysound"].status = CAPABILITY_APPROXIMATE
    if "sm-meter-lost" in codes:
        plan.strategies["meter"] = FeatureStrategy("meter", CAPABILITY_DOWNGRADE, "drop-meter", sum(point[3] != 4 for point in _document_timing_points(document)))
    if "mc-timing-precision" in codes:
        plan.strategies["bpm"] = FeatureStrategy("bpm", CAPABILITY_APPROXIMATE, "keep", len(document.timings))
    if target_format == ".sm":
        surviving_notes = candidate.extra.get("_sm_surviving_notes", ())
        surviving_counts = {
            "keysound": sum(bool(note.sound) for note in surviving_notes),
            "auto_keysound": len(candidate.extra.get("_sm_surviving_events", ()))
                + sum(_document_note_type(candidate, note) == NOTE_TYPE_AUTO_KEYSOUND for note in surviving_notes),
        }
        auto_entry = plan.strategies.get("auto_keysound")
        if auto_entry is not None:
            auto_entry.count = len(document.sound_events or ()) + sum(
                _document_note_type(document, note) == NOTE_TYPE_AUTO_KEYSOUND for note in document.notes)
        for feature, count in surviving_counts.items():
            entry = plan.strategies.get(feature)
            if entry is not None and count < entry.count:
                entry.status = CAPABILITY_DOWNGRADE
    return plan


def write_chart(
    document, path, extension, reference_map=None,
    overall_difficulty=None, hp_drain_rate=None,
):
    _check_document_errors(path, document)
    plan = plan_conversion(document, extension, reference_map, overall_difficulty, hp_drain_rate)
    for diagnostic in plan.diagnostics:
        # Keep one current finding per code when callers rewrite a document.
        document.diagnostics = [item for item in document.diagnostics if item.code != diagnostic.code]
        document.diagnostics.append(diagnostic)
    _check_document_errors(path, document)
    _store_chart_text(path, plan.output_text)


def _discover_archive(root):
    charts = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_CHART_EXTENSIONS:
            charts.append(path)
    return charts

def _extract_archive_file(source, root):
    try:
        with zipfile.ZipFile(source) as archive:
            _extract_archive(archive, root)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ChartConversionError(f"Unable to read archive {Path(source).name}: {exc}") from exc

def _extract_archive(archive, root):
    root = Path(root).resolve()
    seen_files = set()
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ChartConversionError(f"Archive contains unsafe path: {info.filename}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        key = target.relative_to(root).as_posix().casefold()
        if key in seen_files:
            raise ChartConversionError(f"归档成员路径重复，无法安全解包：{info.filename}")
        seen_files.add(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(target, "wb") as destination:
            destination.write(source.read())


def _chart_stem(path, document, include_difficulty):
    """Return the output stem for one chart.

    ``include_difficulty`` is decided by the caller from how many charts the
    source yields: a ``.sm`` carrying several ``#NOTES`` needs the difficulty to
    keep its outputs apart, while a single-chart source is named after the file
    itself (``BOOM.sm`` -> ``BOOM.osu``) -- the same rule archive members use,
    so one chart has one name in every container.
    """
    stem = Path(path).stem
    difficulty = re.sub(r"[^A-Za-z0-9_.-]+", "_", document.difficulty_name or "").strip("_")
    return f"{stem}-{difficulty}" if difficulty and include_difficulty else stem

def _plain_output_stem(path, document, target_format, total_charts=1):
    stem = _chart_stem(path, document, include_difficulty=total_charts > 1)
    if Path(path).suffix.lower() == str(target_format).lower():
        stem += "-converted"
    return stem


def archive_member_name(path, document, total_charts=1):
    """Return the archive member name for one chart converted from ``path``.

    A single-chart source keeps its own stem (``song.osu`` -> ``song.mc``); a
    multi-chart source such as a StepMania file adds the difficulty so the
    members stay distinguishable.
    """
    return _chart_stem(path, document, include_difficulty=total_charts > 1)

def _resource_archive_name(reference):
    """Return the archive member name for a resource the chart mentions."""
    text = _clean(reference).replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    if not name or name in (".", ".."):
        raise ChartConversionError(f"Unsafe resource reference: {reference!r}")
    return name


def _resolve_resource_path(directory, reference, stop=None):
    """Locate a referenced resource, searching upwards like osu! does.

    A chart does not have to live beside its audio: a folder such as
    ``set/nested/hard.osu`` normally names ``song.mp3`` that sits at the
    package root in ``set/``.  Look in the chart's own directory first, then in
    each parent up to (and including) ``stop``.  Returns ``None`` when nothing
    matches, so callers never hand a non-existent path to ``zipfile.write``.
    """
    relative = Path(_clean(reference).replace("\\", "/"))
    if relative.is_absolute():
        return None, directory
    directory = Path(directory).resolve()
    boundary = Path(stop).resolve() if stop is not None else directory
    candidate_dir = directory
    while True:
        candidate = (candidate_dir / relative).resolve()
        try:
            candidate.relative_to(boundary)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            return candidate, candidate_dir
        if candidate_dir == boundary or boundary not in candidate_dir.parents:
            return None, directory
        candidate_dir = candidate_dir.parent
        if candidate_dir == candidate_dir.parent:
            return None, directory


def _archive_member_for_resource(resolved, reference, stop):
    """Return the archive member name for a resolved resource.

    The member has to sit where the chart's own reference will look for it once
    the archive is extracted, and three cases cover every real chart:

    * the resource is the stop directory's direct child -- the reference names
      exactly the basename, so the member is that basename;
    * the resource sits deeper inside the tree -- the tree is preserved and the
      member keeps its archive-relative path, so a subfolder layout survives;
    * the resource was found above the stop directory -- there is no valid
      archive location for it, so the basename is the least surprising choice.
    """
    stop = Path(stop).resolve() if stop is not None else None
    if stop is not None and resolved.parent == stop:
        return resolved.name
    if stop is not None:
        try:
            return resolved.relative_to(stop).as_posix()
        except ValueError:
            return resolved.name
    return _resource_archive_name(reference)


def _document_resource_references(document):
    """Yield ``(role, reference)`` for every resource a document names.

    Roles are ``audio`` (BGM), ``background``, ``cover`` and ``keysound``
    (note-bound or automatic sounds).  What is named is reported separately
    from what exists: callers must never learn about a missing file only
    because a collector filtered it out first.
    """
    yielded = set()
    background_value = _clean(document.background_filename)
    cover_value = _clean(document.cover_filename)
    for role, value in (
        ("audio", document.audio_filename),
        ("background", background_value),
        # A cover that is literally the background (every .osu chart) is one
        # file and one fact: reporting both roles would double-count it.
        ("cover", "" if cover_value == background_value else cover_value),
    ):
        value = _clean(value)
        if value and (role, value) not in yielded:
            yielded.add((role, value))
            yield role, value
    for note in document.notes or ():
        value = _clean(getattr(note, "sound", ""))
        if value and ("keysound", value) not in yielded:
            yielded.add(("keysound", value))
            yield "keysound", value
    for event in document.sound_events or ():
        value = _clean(getattr(event, "sound", ""))
        if value and ("keysound", value) not in yielded:
            yielded.add(("keysound", value))
            yield "keysound", value


@dataclass
class ResourceReference:
    """One chart resource reference and its resolution state."""

    chart_path: object
    role: str
    reference: str
    resolved: object = None
    member: str = ""


def analyze_chart_resources(chart_pairs, stop=None):
    """Separate what the charts name from what exists on disk.

    ``chart_pairs`` is ``[(chart_path, documents), ...]``; each reference is
    resolved from its own chart's directory up to ``stop``.  Returns
    ``(found, missing)``: found references carry the resolved path and their
    natural archive member name (deduplicated by resolved file, so a shared
    audio is packed once); missing references are deduplicated by role and
    reference text, so one absent file is reported once.
    """
    found = []
    missing = []
    seen_paths = set()
    seen_missing = set()
    for chart_path, documents in chart_pairs:
        chart_dir = Path(chart_path).resolve().parent
        for document in documents:
            for role, reference in _document_resource_references(document):
                resolved, _base = _resolve_resource_path(chart_dir, reference, stop)
                if resolved is None:
                    key = (role, reference.replace("\\", "/").casefold())
                    if key not in seen_missing:
                        seen_missing.add(key)
                        missing.append(ResourceReference(chart_path, role, reference))
                    continue
                key = str(resolved).casefold()
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                member = _archive_member_for_resource(resolved, reference, stop)
                found.append(ResourceReference(chart_path, role, reference, resolved, member))
    return found, missing


def _referenced_resources_for_charts(chart_pairs, directory, stop=None):
    """Yield ``(absolute_path, member_name)`` for referenced files that exist.

    Kept for callers that only pack; missing references are available from
    ``analyze_chart_resources`` and must be reported by the caller.
    """
    found, _missing = analyze_chart_resources(chart_pairs, stop or directory)
    for reference in found:
        yield reference.resolved, reference.member


def _referenced_resources(documents, directory, stop=None):
    """Yield ``(absolute_path, member_name)`` for documents under one directory."""
    pairs = [
        (Path(directory) / f"chart{index}.osu", [document])
        for index, document in enumerate(documents)
    ]
    return _referenced_resources_for_charts(pairs, directory, stop)


class ArchiveMemberPlanner:
    """Hands out unique archive member names.

    Names are compared with separators normalised and case folded, so
    ``a/b.png`` versus ``a\\b.png`` and ``Song.mp3`` versus ``song.mp3``
    collide the way a Windows extraction would.  A collision is resolved by
    inserting a counter before the extension; a member is never overwritten.
    Every rename is recorded in :attr:`renamed`: a member whose name changed
    without a word is exactly what makes a converted archive look like
    something renamed a file for no reason, and a chart whose reference was
    rewritten to follow it needs that rename reported.
    """

    def __init__(self):
        self._used = set()
        self.renamed = []

    @staticmethod
    def _key(name):
        return str(name).replace("\\", "/").casefold()

    def claim(self, name):
        name = str(name).replace("\\", "/").lstrip("/")
        candidate = name
        index = 2
        while self._key(candidate) in self._used:
            stem, separator, extension = name.rpartition(".")
            if separator:
                candidate = f"{stem}-{index}.{extension}"
            else:
                candidate = f"{name}-{index}"
            index += 1
        self._used.add(self._key(candidate))
        if candidate != name:
            self.renamed.append((name, candidate))
        return candidate

    def add(self, name):
        """Register a name written by someone else (no renaming)."""
        self._used.add(self._key(name))

    def __contains__(self, name):
        return self._key(name) in self._used


def _archive_member_prefix(chart_path, root):
    """Return the archive-relative directory a chart lives in, with a slash.

    A chart inside an archive can sit in a subfolder (``Song A/0/chart.osu``),
    and flattening it would drop the structure the archive was shipped with as
    well as break any resource reference that points out of that subfolder.
    """
    try:
        relative = Path(chart_path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return ""
    parent = relative.parent.as_posix()
    return "" if parent == "." else f"{parent}/"


def _chart_member_stem(chart_path, root, document, documents_from_source):
    """Return the member stem (no extension) for one chart inside an archive.

    The identity is the chart's own location inside the source (relative
    path without extension), plus its difficulty when one source file yields
    several charts.  It never collapses two different source files onto the
    same name the way an outer-archive stem would.
    """
    try:
        relative = Path(chart_path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        relative = Path(Path(chart_path).name)
    stem = relative.with_suffix("").as_posix()
    difficulty = re.sub(r"[^A-Za-z0-9_.-]+", "_", document.difficulty_name or "").strip("_")
    if len(documents_from_source) > 1 and difficulty:
        stem = f"{stem}-{difficulty}"
    return stem


def _relative_reference(chart_member, resource_member):
    """Return the reference a chart member needs to reach a resource member."""
    chart_dir = posixpath.dirname(str(chart_member).replace("\\", "/"))
    resource_member = str(resource_member).replace("\\", "/")
    if not chart_dir:
        return resource_member
    return posixpath.relpath(resource_member, chart_dir)


def _load_source_charts(source):
    """Return ``(chart_pairs, temp_handle, chart_root)`` for one input.

    ``chart_pairs`` is ``[(chart_path, [documents])]`` parsed from each
    chart's own location, so member names and resource lookups keep the
    chart's identity.  Archive inputs are unpacked into a temporary
    directory whose handle the caller must keep alive while using the pairs.
    """
    source = Path(source)
    source_ext = source.suffix.lower()
    if source_ext in SUPPORTED_CHART_EXTENSIONS:
        return [(source, parse_chart(source))], None, source.parent
    if source_ext in (".osz", ".mcz"):
        temp_handle = tempfile.TemporaryDirectory(prefix="osu-toolbox-chart-")
        try:
            _extract_archive_file(source, temp_handle.name)
            chart_paths = _discover_archive(temp_handle.name)
            if not chart_paths:
                raise ChartConversionError(f"归档中没有可读取的谱面：{source.name}")
            pairs = [(chart_path, parse_chart(chart_path)) for chart_path in chart_paths]
        except Exception:
            temp_handle.cleanup()
            raise
        return pairs, temp_handle, Path(temp_handle.name)
    raise ChartConversionError(f"不支持的输入格式：{source_ext}")


def _claim_output_path(directory, stem, extension, used):
    """Claim a batch-unique output path.

    Only the batch's own names participate: an output that already exists on
    disk is a separate overwrite question the caller asks the user about,
    never a reason to silently rename a file the user was already shown.
    """
    candidate = Path(directory) / f"{stem}{extension}"
    index = 2
    while str(candidate).casefold() in used:
        candidate = Path(directory) / f"{stem}-{index}{extension}"
        index += 1
    used.add(str(candidate).casefold())
    return candidate


def _plan_source_outputs(source, chart_pairs, chart_root, target_format, output_directory, used):
    """Plan the outputs of one loaded source.

    Returns ``[(kind, destination, detail)]`` where ``kind`` is ``"archive"``
    (one archive holding every chart of the source) or ``"plain"`` (one file
    per document, ``detail`` being ``(chart_path, document)``).
    """
    source = Path(source)
    source_ext = source.suffix.lower()
    output_directory = Path(output_directory)
    if target_format in (".osz", ".mcz"):
        stem = source.stem + ("-converted" if source_ext == target_format else "")
        return [("archive", _claim_output_path(output_directory, stem, target_format, used), None)]
    jobs = []
    for chart_path, documents in chart_pairs:
        for document in documents:
            if source_ext in (".osz", ".mcz"):
                stem = _chart_member_stem(chart_path, chart_root, document, documents)
                # A plain output cannot carry folders: the base name is kept
                # and the dropped structure is reported during execution.
                stem = stem.replace("\\", "/").rsplit("/", 1)[-1]
            else:
                stem = _plain_output_stem(chart_path, document, target_format, len(documents))
            jobs.append(("plain", _claim_output_path(output_directory, stem, target_format, used), (chart_path, document)))
    return jobs


def planned_output_paths(input_paths, target_format, output_directory):
    """Return the deterministic output names without writing files."""
    target_format = str(target_format).lower()
    if target_format not in SUPPORTED_OUTPUT_EXTENSIONS:
        raise ChartConversionError(f"Unsupported output format: {target_format}")
    output_directory = Path(output_directory)
    used = set()
    planned = []
    for source_value in input_paths:
        source = Path(source_value).resolve()
        pairs, temp_handle, chart_root = _load_source_charts(source)
        try:
            for _kind, destination, _detail in _plan_source_outputs(
                source, pairs, chart_root, target_format, output_directory, used
            ):
                planned.append(str(destination))
        finally:
            if temp_handle is not None:
                temp_handle.cleanup()
    return planned


_RESOURCE_ROLE_NAMES = {"audio": "主音乐", "background": "背景", "cover": "封面", "keysound": "按键音"}

# A trailing counter a previous export left behind: ``BOOM2.mp3``, ``song-2.mp3``
# and ``song 2.mp3`` all name the file beside them as ``BOOM.mp3``/``song.mp3``.
_DANGLING_COUNTER = re.compile(r"^(?P<base>.+?)[ _-]?\d+$")


def _counter_free_stem(stem):
    """Return ``stem`` without one trailing counter, or ``""`` when it has none."""
    match = _DANGLING_COUNTER.match(str(stem))
    if not match:
        return ""
    base = match.group("base").rstrip(" _-")
    return base if base and base != str(stem) else ""


def _near_miss_resource(chart_dir, reference):
    """Return a sibling file that differs from ``reference`` by a dangling counter.

    Exports from other tools leave references like ``#MUSIC:BOOM2.mp3`` beside a
    file that is actually called ``BOOM.mp3``.  The converter never substitutes
    one for the other -- the closest name is still a guess, and guessing wrong
    would attach another song's audio -- but naming the candidate turns an
    unexplainable "missing file" into something the user can act on.  Either
    side may carry the counter: the chart's name or the file that is there.
    """
    relative = Path(str(reference).replace("\\", "/"))
    suffix = relative.suffix.casefold()
    if not suffix:
        return ""
    reference_stem = relative.stem.casefold()
    reference_base = _counter_free_stem(reference_stem)
    directory = Path(chart_dir) / relative.parent
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return ""
    for candidate in entries:
        if not candidate.is_file() or candidate.suffix.casefold() != suffix:
            continue
        candidate_stem = candidate.stem.casefold()
        # Either the file dropped the counter the chart still names, or the file
        # carries a counter the chart never had.  When neither name has one the
        # candidate is unrelated, so nothing is offered.
        if reference_base and candidate_stem == reference_base:
            return candidate.name
        if _counter_free_stem(candidate_stem) == reference_stem:
            return candidate.name
    return ""


def _missing_resource_messages(source, missing):
    """Build the Chinese diagnostics for references that name absent files."""
    name = Path(source).name
    messages = []
    for reference in missing:
        role = _RESOURCE_ROLE_NAMES.get(reference.role, reference.role)
        chart_label = Path(reference.chart_path).name
        if chart_label != name:
            message = f"{name}（{chart_label}）引用的{role}不存在：{reference.reference}"
        else:
            message = f"{name} 引用的{role}不存在：{reference.reference}"
        hint = _near_miss_resource(Path(reference.chart_path).parent, reference.reference)
        if hint:
            # Say who owns the name: the chart wrote it, the converter only
            # looks for exactly that name and never renames a resource.
            message += (
                f"；同目录存在 {hint}，可能就是它（转谱只按谱面写的名字查找，"
                "不会自动替换，请确认后改名或修正谱面引用）"
            )
        messages.append(message)
    return messages


def _document_label(source, document, multi_document):
    label = Path(source).name
    if multi_document:
        version = _clean(document.version or document.difficulty_name)
        if version:
            label = f"{label}（{version}）"
    return label


def _emit_document_diagnostics(emit_event, source, document, multi_document=False):
    """Send a document's non-fatal diagnostics through the event channel."""
    if not emit_event:
        return
    label = _document_label(source, document, multi_document)
    for diagnostic in document.diagnostics or ():
        if diagnostic.severity == "error":
            continue
        kind = "log" if diagnostic.severity == "info" else "warning"
        emit_event(kind, f"{label}：{diagnostic.message}")


def _check_document_errors(source, document, multi_document=False):
    """Raise when a document carries a blocking (error-level) diagnostic."""
    errors = [item for item in document.diagnostics or () if item.severity == "error"]
    if errors:
        label = _document_label(source, document, multi_document)
        raise ChartConversionError(f"{label}：" + "；".join(item.message for item in errors))


def _write_chart_member(
    archive, document, member, chart_extension, reference_map,
    overall_difficulty=None, hp_drain_rate=None,
):
    """Render one chart into an archive under ``member``."""
    suffix = chart_extension
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        write_chart(
            document, temp_path, chart_extension, reference_map=reference_map,
            overall_difficulty=overall_difficulty,
            hp_drain_rate=hp_drain_rate,
        )
        archive.write(temp_path, member)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_archive_output(
    source, chart_pairs, chart_root, destination, target_format, emit_event,
    overall_difficulty=None, hp_drain_rate=None,
):
    """Write every chart of one source into a single new archive."""
    chart_extension = archive_chart_extension(target_format)
    planner = ArchiveMemberPlanner()
    found, _missing = analyze_chart_resources(chart_pairs, chart_root)
    # Resources are claimed before charts, so a chart member can never
    # overwrite the music it references.
    resource_members = {}
    for reference in found:
        member = planner.claim(reference.member)
        resource_members[str(reference.resolved).casefold()] = member
    chart_jobs = []
    for chart_path, documents in chart_pairs:
        for document in documents:
            stem = _chart_member_stem(chart_path, chart_root, document, documents)
            member = planner.claim(f"{stem}{chart_extension}")
            chart_jobs.append((chart_path, document, member))
    total_documents = len(chart_jobs)
    for _chart_path, document, _member in chart_jobs:
        _check_document_errors(source, document, total_documents > 1)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for reference in found:
            member = resource_members[str(reference.resolved).casefold()]
            archive.write(reference.resolved, member)
        for chart_path, document, member in chart_jobs:
            # Renamed resources must be renamed in the chart's references too:
            # a member move without a reference update silently breaks audio.
            reference_map = {}
            chart_dir = Path(chart_path).resolve().parent
            for _role, reference in _document_resource_references(document):
                resolved, _base = _resolve_resource_path(chart_dir, reference, chart_root)
                if resolved is None:
                    continue
                resource_member = resource_members.get(str(resolved).casefold())
                if resource_member is None:
                    continue
                new_reference = _relative_reference(member, resource_member)
                if new_reference != reference:
                    reference_map[reference] = new_reference
            _write_chart_member(
                archive, document, member, chart_extension, reference_map,
                overall_difficulty=overall_difficulty,
                hp_drain_rate=hp_drain_rate,
            )
            _emit_document_diagnostics(emit_event, source, document, total_documents > 1)
    _report_member_renames(emit_event, source, planner)


def _report_member_renames(emit_event, source, planner):
    """Report archive members whose name had to change to stay unique.

    The chart's own references are rewritten to the new name, so the archive
    stays consistent -- but a name that changed silently is what makes a user
    think the converter renamed their audio for no reason.
    """
    if not emit_event:
        return
    for requested, granted in planner.renamed:
        emit_event(
            "warning",
            f"{Path(source).name} 内的归档成员重名：{requested} → {granted}"
            "（谱面内引用已同步改写）",
        )


@contextmanager
def _staged_output(destination, allow_overwrite):
    """Publish a completed file without exposing partial conversion output."""
    destination = Path(destination)
    descriptor, name = tempfile.mkstemp(
        prefix=".osu-toolbox-chart-", suffix=destination.suffix,
        dir=str(destination.parent),
    )
    os.close(descriptor)
    staged = Path(name)
    try:
        yield staged
        if allow_overwrite:
            os.replace(staged, destination)
        elif os.name == "nt":
            os.rename(staged, destination)
        else:
            os.link(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def convert_paths(
    input_paths, target_format, output_directory, emit_event=None,
    allow_overwrite=False, expected_outputs=None,
    overall_difficulty=None, hp_drain_rate=None, expected_inputs=None,
):
    """Convert one or more paths and return the generated output paths.

    Planning and execution share one code path, so the paths shown before
    the run are exactly the paths written.  ``expected_outputs`` carries the
    preflight's list: when the fresh plan differs (files changed since the
    preflight), the run stops instead of writing to paths the user never
    saw.  Existing files are never overwritten without ``allow_overwrite``.

    ``emit_event`` receives ``("warning", message)`` for losses and missing
    resources, ``("log", message)`` for informational notes and
    ``("progress", payload)`` after each input.

    ``overall_difficulty``/``hp_drain_rate`` replace the source's own OD/HP on
    every written ``.osu`` chart, inside an ``.osz`` archive included; ``None``
    keeps whatever the source carried.  Other targets have no such fields and
    ignore both values.
    """
    target_format = str(target_format).lower()
    if target_format not in SUPPORTED_OUTPUT_EXTENSIONS:
        raise ChartConversionError(f"Unsupported output format: {target_format}")
    input_paths = list(input_paths)
    expected_inputs = expected_inputs if expected_inputs is not None else getattr(expected_outputs, "source_fingerprints", None)
    if expected_inputs is not None and expected_inputs != _input_fingerprints(input_paths):
        raise ChartConversionError("输入内容与预检结果不一致，请重新检查后再运行")
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    used = set()
    outputs = []
    loaded = []
    try:
        for source_value in input_paths:
            source = Path(source_value).resolve()
            pairs, temp_handle, chart_root = _load_source_charts(source)
            jobs = _plan_source_outputs(source, pairs, chart_root, target_format, output_directory, used)
            loaded.append((source, pairs, temp_handle, chart_root, jobs))
            for _kind, destination, _detail in jobs:
                outputs.append(str(destination))
    except Exception:
        for _source, _pairs, temp_handle, _chart_root, _jobs in loaded:
            if temp_handle is not None:
                temp_handle.cleanup()
        raise
    try:
        if expected_outputs is not None:
            expected = [str(item) for item in expected_outputs]
            if [item.casefold() for item in outputs] != [item.casefold() for item in expected]:
                for _source, _pairs, temp_handle, _chart_root, _jobs in loaded:
                    if temp_handle is not None:
                        temp_handle.cleanup()
                raise ChartConversionError(
                    "输出计划与预检结果不一致（输入或输出目录可能已变化），请重新检查后再运行"
                )
        conflicts = [path for path in outputs if Path(path).exists()]
        if conflicts and not allow_overwrite:
            for _source, _pairs, temp_handle, _chart_root, _jobs in loaded:
                if temp_handle is not None:
                    temp_handle.cleanup()
            listing = "\n".join(conflicts)
            raise FileExistsError(f"以下输出文件已存在，未覆盖：\n{listing}")
        total = max(1, len(loaded))
        plain_scope_noted = set()
        for input_index, (source, pairs, temp_handle, chart_root, jobs) in enumerate(loaded):
            try:
                total_documents = sum(len(documents) for _path, documents in pairs)
                _found, missing = analyze_chart_resources(pairs, chart_root)
                for kind, destination, detail in jobs:
                    if kind == "archive":
                        with _staged_output(destination, allow_overwrite) as staged:
                            _write_archive_output(
                                source, pairs, chart_root, staged, target_format, emit_event,
                                overall_difficulty=overall_difficulty,
                                hp_drain_rate=hp_drain_rate,
                            )
                        continue
                    chart_path, document = detail
                    _check_document_errors(source, document, total_documents > 1)
                    prefix = _archive_member_prefix(chart_path, chart_root)
                    if prefix and Path(source).suffix.lower() in (".osz", ".mcz"):
                        # A plain file cannot carry a folder, so the archive's
                        # own subfolders are reported rather than dropped.
                        if emit_event:
                            emit_event(
                                "warning",
                                f"{source.name} 内的 {prefix}{Path(chart_path).name} 位于子目录，"
                                "导出为单文件后其目录结构不会保留",
                            )
                    references = list(_document_resource_references(document))
                    if references and emit_event:
                        output_dir = Path(destination).resolve().parent
                        chart_dir = Path(chart_path).resolve().parent
                        if output_dir != chart_dir:
                            key = str(source).casefold()
                            if key not in plain_scope_noted:
                                plain_scope_noted.add(key)
                                emit_event(
                                    "warning",
                                    f"{source.name} 输出为单个谱面文件，未复制其引用的资源文件；"
                                    "输出目录与原谱面不同，谱面可能找不到这些资源",
                                )
                    with _staged_output(destination, allow_overwrite) as staged:
                        write_chart(
                            document, staged, target_format,
                            overall_difficulty=overall_difficulty,
                            hp_drain_rate=hp_drain_rate,
                        )
                    _emit_document_diagnostics(emit_event, source, document, total_documents > 1)
                if emit_event:
                    for message in _missing_resource_messages(source, missing):
                        emit_event("warning", message)
            finally:
                if temp_handle is not None:
                    temp_handle.cleanup()
            if emit_event:
                emit_event("progress", {"current": input_index + 1, "total": total, "fraction": (input_index + 1) / total})
    except Exception:
        for _source, _pairs, temp_handle, _chart_root, _jobs in loaded:
            if temp_handle is not None:
                temp_handle.cleanup()
        raise
    return outputs


class _PlannedOutputs(list):
    """Output paths carrying the input-content snapshot for execution."""

    def __init__(self, paths=(), source_fingerprints=None):
        super().__init__(paths)
        self.source_fingerprints = source_fingerprints or {}


def _input_fingerprints(input_paths):
    return {str(Path(value).resolve()).casefold(): hashlib.sha256(Path(value).read_bytes()).hexdigest()
            for value in input_paths}


def preflight_conversion(input_paths, target_format, output_directory):
    """Return ``(planned_paths, warnings)`` for the tool preflight.

    The warnings describe what the conversion will actually do with the
    features these inputs really contain: strategies from the same
    ``plan_conversion`` the writers execute, parse-level findings, and
    missing resources.  Features that do not appear produce no noise.
    """
    target_format = str(target_format).lower()
    if target_format not in SUPPORTED_OUTPUT_EXTENSIONS:
        raise ChartConversionError(f"Unsupported output format: {target_format}")
    chart_format = (
        archive_chart_extension(target_format)
        if target_format in (".osz", ".mcz") else target_format
    )
    output_directory = Path(output_directory)
    used = set()
    input_paths = list(input_paths)
    planned = _PlannedOutputs(source_fingerprints=_input_fingerprints(input_paths))
    warnings = []
    for source_value in input_paths:
        source = Path(source_value).resolve()
        pairs, temp_handle, chart_root = _load_source_charts(source)
        try:
            jobs = _plan_source_outputs(source, pairs, chart_root, target_format, output_directory, used)
            planned.extend(str(destination) for _kind, destination, _detail in jobs)
            total_documents = sum(len(documents) for _path, documents in pairs)
            for _chart_path, documents in pairs:
                for document in documents:
                    label = _document_label(source, document, total_documents > 1)
                    plan = plan_conversion(document, chart_format)
                    _check_document_errors(source, document, total_documents > 1)
                    for diagnostic in list(document.diagnostics or ()) + plan.diagnostics:
                        if diagnostic.severity in ("info", "loss", "warning", "error"):
                            warnings.append(f"{label}：{diagnostic.message}")
            _found, missing = analyze_chart_resources(pairs, chart_root)
            warnings.extend(_missing_resource_messages(source, missing))
        finally:
            if temp_handle is not None:
                temp_handle.cleanup()
    return planned, warnings


__all__ = (
    "CAPABILITY_APPROXIMATE", "CAPABILITY_DOWNGRADE", "CAPABILITY_FULL", "CAPABILITY_UNSUPPORTED",
    "ArchiveMemberPlanner", "ChartConversionError", "ChartDiagnostic", "ChartDocument",
    "ChartNoteData", "ChartScrollEvent", "ChartSoundEvent", "ConversionPlan",
    "NOTE_TYPES", "SUPPORTED_CHART_EXTENSIONS", "SUPPORTED_INPUT_EXTENSIONS",
    "SUPPORTED_OUTPUT_EXTENSIONS", "TARGET_CAPABILITIES", "analyze_chart_resources",
    "archive_chart_extension", "convert_paths", "decode_chart_bytes", "parse_chart",
    "plan_conversion", "planned_output_paths", "preflight_conversion", "read_chart_text",
    "write_chart",
)
