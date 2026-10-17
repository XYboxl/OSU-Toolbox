"""pywebview application host."""

import sys
from importlib.resources import as_file, files
from pathlib import Path

from osu_toolbox import SHELL_MENU_EXE_NAME, get_project_info

from .bridge import Api
from .runtime import runtime_error_message, webview2_runtime_available


def _application_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    entry_point = Path(sys.argv[0]).resolve()
    if entry_point.suffix.lower() == ".exe":
        return entry_point.parent
    return Path(__file__).resolve().parents[2]


def _search_root():
    if getattr(sys, "frozen", False):
        return _application_root()
    return Path(__file__).resolve().parents[2]


def _shell_executable():
    if getattr(sys, "frozen", False):
        return _application_root() / SHELL_MENU_EXE_NAME
    return Path(sys.executable).resolve().parent / SHELL_MENU_EXE_NAME


def main():
    if not webview2_runtime_available():
        print(runtime_error_message(), file=sys.stderr)
        return 1
    try:
        import webview
    except ImportError:
        print("pywebview 未安装，请先运行 uv sync。", file=sys.stderr)
        return 1

    project = get_project_info()
    api = Api(
        _search_root(),
        output_root=_application_root(),
        shell_executable_path=str(_shell_executable()),
    )
    resource = files("osu_toolbox.gui").joinpath("assets", "index.html")
    try:
        with as_file(resource) as index_path:
            window = webview.create_window(
                project.name,
                url=index_path.as_uri(),
                js_api=api,
                width=1080,
                height=720,
                min_size=(900, 620),
                text_select=True,
            )
            api._set_window(window)
            window.events.closing += api._handle_closing
            webview.start(gui="edgechromium", debug=False, private_mode=True)
    except Exception as exc:
        print(f"GUI 启动失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
