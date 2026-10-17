"""Project metadata exposed to GUI and public API consumers."""

from dataclasses import asdict, dataclass
from email.utils import parseaddr


DISTRIBUTION_NAME = "osu-toolbox"
PROJECT_NAME = "OSU 工具箱"
PROJECT_TAGLINE = "osu!mania/Malody 实用工具合集"
PROJECT_FOCUS = "无"
PROJECT_QQ = "753831679"
PROJECT_STATUS = "正式版"
PROJECT_FUTURE_NOTE = "更多 OSU 工具将在后续版本加入。"
_FALLBACK_SUMMARY = "osu!mania 工具箱"


def _read_metadata(name):
    """Read installed distribution metadata for ``name``, or ``None``.

    ``importlib.metadata`` costs about 100 ms of interpreter time on Windows
    because it pulls in ``email``, ``zipfile`` and ``inspect``.  Only the About
    dialog needs it, so the import lives in this seam rather than at module
    load; it is also the single point a caller can stub.
    """
    from importlib.metadata import PackageNotFoundError, metadata

    try:
        return metadata(name)
    except PackageNotFoundError:
        return None


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    tagline: str
    focus: str
    version: str
    summary: str
    author_name: str
    author_email: str
    qq: str
    status: str
    future_note: str

    def as_dict(self):
        return asdict(self)


def get_project_info():
    package_metadata = _read_metadata(DISTRIBUTION_NAME)
    if package_metadata is None:
        return ProjectInfo(
            PROJECT_NAME,
            PROJECT_TAGLINE,
            PROJECT_FOCUS,
            "0+unknown",
            _FALLBACK_SUMMARY,
            "",
            "",
            PROJECT_QQ,
            PROJECT_STATUS,
            PROJECT_FUTURE_NOTE,
        )

    parsed_name, parsed_email = parseaddr(package_metadata.get("Author-email", ""))
    return ProjectInfo(
        PROJECT_NAME,
        PROJECT_TAGLINE,
        PROJECT_FOCUS,
        package_metadata.get("Version", "0+unknown"),
        package_metadata.get("Summary", _FALLBACK_SUMMARY),
        package_metadata.get("Author", "") or parsed_name,
        parsed_email,
        PROJECT_QQ,
        PROJECT_STATUS,
        PROJECT_FUTURE_NOTE,
    )


def get_project_version():
    return get_project_info().version
