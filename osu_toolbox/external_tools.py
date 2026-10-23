"""External executables resolved from the application directory and PATH."""

import os
from pathlib import Path
import sys


FFMPEG_NOT_FOUND_MESSAGE = (
    "未找到 FFmpeg。请将 ffmpeg.exe 放在程序同目录（源码运行时为项目根目录），"
    "或将 FFmpeg 所在目录加入系统 PATH 后重试。"
)


def find_ffmpeg():
    """Return an absolute executable path, or None when FFmpeg is missing."""
    if getattr(sys, "frozen", False):
        application_root = Path(sys.executable).resolve().parent
    else:
        application_root = Path(__file__).resolve().parent.parent
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    directories = [application_root]
    directories.extend(
        Path(value.strip('"'))
        for value in os.environ.get("PATH", "").split(os.pathsep)
        if value.strip('"')
    )
    for directory in directories:
        candidate = (directory / name).resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def require_ffmpeg():
    executable = find_ffmpeg()
    if executable is None:
        raise FileNotFoundError(FFMPEG_NOT_FOUND_MESSAGE)
    return executable
