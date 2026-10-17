"""谱面/回放解析的通用文件工具。"""

import hashlib
import os


MALODY_REPLAY_DIRNAME = "replay"
MALODY_BEATMAP_DIRNAME = "beatmap"
OSU_REPLAY_DIRNAME = "Replays"
OSU_BEATMAP_DIRNAME = "Songs"

_REPLAY_LAYOUTS = {
    ".mr": (
        MALODY_REPLAY_DIRNAME,
        MALODY_BEATMAP_DIRNAME,
        ".mc",
        "malody_directory",
    ),
    ".osr": (
        OSU_REPLAY_DIRNAME,
        OSU_BEATMAP_DIRNAME,
        ".osu",
        "osu_directory",
    ),
}


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_beatmap_by_md5(md5_hex, search_root, extensions=(".osu",)):
    if not md5_hex or not search_root:
        return None
    if not os.path.isdir(search_root):
        return None
    target = md5_hex.lower()
    extensions = tuple(str(item).lower() for item in extensions)
    for root, _, files in os.walk(search_root):
        for name in files:
            if not name.lower().endswith(extensions):
                continue
            path = os.path.join(root, name)
            try:
                if _file_md5(path).lower() == target:
                    return path
            except OSError:
                continue
    return None


def replay_beatmap_search_roots(
    replay_path,
    *,
    osu_directory=None,
    malody_directory=None,
):
    """Return only format-specific, high-confidence beatmap roots."""
    replay_path = os.path.abspath(os.fspath(replay_path))
    extension = os.path.splitext(replay_path)[1].lower()
    layout = _REPLAY_LAYOUTS.get(extension)
    if layout is None:
        return ()
    replay_dirname, beatmap_dirname, _beatmap_extension, setting_key = layout
    configured = {
        "osu_directory": osu_directory,
        "malody_directory": malody_directory,
    }[setting_key]
    candidates = []
    if configured:
        configured = os.path.abspath(os.fspath(configured))
        root = (
            configured
            if os.path.basename(configured).casefold() == beatmap_dirname.casefold()
            else os.path.join(configured, beatmap_dirname)
        )
        candidates.append(root)

    replay_parent = os.path.dirname(replay_path)
    if os.path.basename(replay_parent).casefold() == replay_dirname.casefold():
        candidates.append(os.path.join(os.path.dirname(replay_parent), beatmap_dirname))

    result = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen or not os.path.isdir(candidate):
            continue
        seen.add(normalized)
        result.append(os.path.abspath(candidate))
    return tuple(result)


def find_beatmap_for_replay(
    md5_hex,
    replay_path,
    *,
    osu_directory=None,
    malody_directory=None,
):
    extension = os.path.splitext(os.fspath(replay_path))[1].lower()
    layout = _REPLAY_LAYOUTS.get(extension)
    if layout is None:
        return None
    beatmap_extension = layout[2]
    roots = replay_beatmap_search_roots(
        replay_path,
        osu_directory=osu_directory,
        malody_directory=malody_directory,
    )
    for root in roots:
        matched = find_beatmap_by_md5(md5_hex, root, (beatmap_extension,))
        if matched:
            return matched
    return None


__all__ = (
    "MALODY_BEATMAP_DIRNAME",
    "MALODY_REPLAY_DIRNAME",
    "OSU_BEATMAP_DIRNAME",
    "OSU_REPLAY_DIRNAME",
    "_file_md5",
    "find_beatmap_by_md5",
    "find_beatmap_for_replay",
    "replay_beatmap_search_roots",
)
