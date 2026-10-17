"""Minimal process bootstrap shared by GUI and Explorer entry points."""

import sys


def is_quick_invocation(argv):
    """Detect shell mode without importing any application dependencies."""
    values = tuple(argv or ())
    if "--operation" in values or any(value.startswith("--operation=") for value in values):
        return True
    inline_context = next(
        (value.split("=", 1)[1] for value in values if value.startswith("--context=")),
        None,
    )
    if inline_context is not None:
        return inline_context in ("shell", "quick")
    try:
        context_index = values.index("--context")
        context = values[context_index + 1]
    except (ValueError, IndexError):
        return False
    return context in ("shell", "quick")


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if is_quick_invocation(values):
        from .quick import main as quick_main

        return quick_main(values)
    # Import the WebView host only after the quick branch has been ruled out.
    from .gui.web_app import main as gui_main

    return gui_main()


__all__ = ("is_quick_invocation", "main")
