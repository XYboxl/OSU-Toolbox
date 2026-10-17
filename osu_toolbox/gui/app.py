"""Installed application entry point.

The module stays import-light so the same console executable can dispatch a
GUI-free shell operation before WebView2 and rendering dependencies load.
"""

from osu_toolbox.launcher import main as _launch


def main(argv=None):
    return _launch(argv)


if __name__ == "__main__":
    raise SystemExit(main())
