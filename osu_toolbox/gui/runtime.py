"""Windows WebView2 runtime detection."""

import os
import sys
from pathlib import Path


WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _registry_has_webview2():
    try:
        import winreg
    except ImportError:
        return False

    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    paths = (
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients",
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients",
    )
    for root in roots:
        for path in paths:
            try:
                with winreg.OpenKey(root, path) as clients:
                    count = winreg.QueryInfoKey(clients)[0]
                    for index in range(count):
                        child_name = winreg.EnumKey(clients, index)
                        try:
                            with winreg.OpenKey(clients, child_name) as child:
                                name = str(winreg.QueryValueEx(child, "name")[0])
                                version = str(winreg.QueryValueEx(child, "pv")[0])
                        except OSError:
                            continue
                        if "webview2" in name.lower() and version not in ("", "0.0.0.0"):
                            return True
            except OSError:
                continue
    return False


def _filesystem_has_webview2():
    bases = (
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    )
    relative_paths = (
        Path("Microsoft") / "EdgeWebView" / "Application",
        Path("Microsoft") / "EdgeWebView2" / "Application",
    )
    for base in bases:
        if not base:
            continue
        for relative in relative_paths:
            application_dir = Path(base) / relative
            if application_dir.is_dir():
                try:
                    if any(child.is_dir() for child in application_dir.iterdir()):
                        return True
                except OSError:
                    pass
    return False


def webview2_runtime_available():
    if os.environ.get("OSU_TOOLBOX_SKIP_WEBVIEW2_CHECK") == "1":
        return True
    if sys.platform != "win32":
        return False
    return _registry_has_webview2() or _filesystem_has_webview2()


def runtime_error_message():
    return (
        "未检测到 Microsoft Edge WebView2 Runtime，请安装后重新启动 osu-toolbox。\n"
        f"下载地址: {WEBVIEW2_DOWNLOAD_URL}"
    )
