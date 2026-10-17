"""Small command-line entry point for the registered video renderer.

The CLI deliberately stays at the application boundary: parsing and terminal
output live here, while validation and rendering continue to use the same
controller as the GUI.
"""

import argparse
import os
import sys
import threading

from .tools import (
    ParamValidationError,
    ToolCancelledError,
    ToolContext,
    build_default_registry,
)


_COMPATIBILITY_MODES = ("stable-replay", "lazer-classic")
_SCROLL_DIRECTIONS = ("auto", "down", "up")
_SONG_PROGRESS_MODES = ("off", "pie", "top_right", "bottom_right", "bottom")
_LETTERBOX_MODES = ("follow", "on", "off")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="osu-toolbox-render",
        description="Render an osu!mania beatmap or replay to an MP4 video.",
    )
    parser.add_argument("beatmap", metavar="BEATMAP", help="path to a .osu beatmap")
    parser.add_argument(
        "--skin-dir", "--skin", dest="skin_dir", required=True,
        help="path to the osu! skin directory",
    )
    parser.add_argument("--replay", dest="replay_path", help="optional .osr replay")
    parser.add_argument(
        "-o", "--output", dest="output_file", default=None,
        help="output MP4 path (default: output.mp4)",
    )
    parser.add_argument(
        "--search-root", default=None,
        help="directory to search when matching a replay beatmap (default: cwd)",
    )
    parser.add_argument("--key-count", type=int, default=None)
    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument(
        "--compatibility-mode", choices=_COMPATIBILITY_MODES, default=None,
        help="renderer compatibility mode",
    )
    parser.add_argument("--scroll-speed", type=float, default=None)
    parser.add_argument(
        "--scroll-direction", choices=_SCROLL_DIRECTIONS, default=None,
    )
    parser.add_argument("--scroll-speed-mult", type=float, default=None)
    parser.add_argument("--scroll-ar-override", type=float, default=None)
    parser.add_argument("--n-parallel", "--parallel", dest="n_parallel", type=int, default=None)
    parser.add_argument("--hit-flash-ms", type=float, default=None)
    parser.add_argument("--tail-ms", type=float, default=None)
    parser.add_argument("--pre-roll-ms", type=float, default=None)
    parser.add_argument("--background", dest="background_path", default=None)
    parser.add_argument("--background-dim", type=float, default=None)
    parser.add_argument(
        "--letterbox-in-breaks", choices=_LETTERBOX_MODES, default=None,
    )
    parser.add_argument(
        "--show-song-progress", choices=_SONG_PROGRESS_MODES, default=None,
    )
    for option in (
        "receptor-pressed",
        "show-hold-tail",
        "show-health-bar",
        "show-barlines",
        "show-hit-error",
        "show-warning-arrow",
    ):
        parser.add_argument(
            f"--{option}", dest=option.replace("-", "_"),
            action=argparse.BooleanOptionalAction, default=None,
        )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing output file",
    )
    return parser


def _raw_params(args):
    params = {
        "beatmap_path": args.beatmap,
        "skin_dir": args.skin_dir,
    }
    fields = (
        "replay_path",
        "output_file",
        "key_count",
        "fps",
        "width",
        "height",
        "compatibility_mode",
        "scroll_speed",
        "scroll_direction",
        "scroll_speed_mult",
        "scroll_ar_override",
        "n_parallel",
        "hit_flash_ms",
        "tail_ms",
        "pre_roll_ms",
        "background_path",
        "background_dim",
        "letterbox_in_breaks",
        "show_song_progress",
        "receptor_pressed",
        "show_hold_tail",
        "show_health_bar",
        "show_barlines",
        "show_hit_error",
        "show_warning_arrow",
    )
    for field in fields:
        value = getattr(args, field)
        if value is not None:
            params[field] = value
    return params


def _event_emitter():
    progress_state = {"active": False}

    def emit(event_type, payload):
        if event_type == "progress" and isinstance(payload, dict):
            current = payload.get("current", 0)
            total = payload.get("total", 0)
            print(
                f"\rProgress: {current}/{total}",
                end="",
                file=sys.stderr,
                flush=True,
            )
            progress_state["active"] = True
            return
        if progress_state["active"]:
            print(file=sys.stderr)
            progress_state["active"] = False
        if event_type == "warning":
            print(f"WARNING: {payload}", file=sys.stderr)
        elif event_type == "log":
            print(payload, file=sys.stderr)

    def finish():
        if progress_state["active"]:
            print(file=sys.stderr)
            progress_state["active"] = False

    return emit, finish


def main(argv=None):
    args = build_parser().parse_args(argv)
    definition = build_default_registry().get("render")
    context = ToolContext(
        search_root=os.path.abspath(args.search_root or os.getcwd()),
    )
    try:
        prepared = definition.controller.prepare(_raw_params(args), context)
    except ParamValidationError as exc:
        print(f"Invalid parameter {exc.key}: {exc}", file=sys.stderr)
        return 2
    except (TypeError, ValueError) as exc:
        print(f"Invalid render parameters: {exc}", file=sys.stderr)
        return 2

    for warning in prepared.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if prepared.confirmation is not None and not args.force:
        output = prepared.params.get("output_file", "output.mp4")
        print(
            f"Output already exists: {output}\n"
            "Use --force to overwrite it.",
            file=sys.stderr,
        )
        return 2

    emit_event, finish_events = _event_emitter()
    cancel_event = threading.Event()
    try:
        result = definition.controller.run(
            prepared,
            emit_event,
            cancel_event=cancel_event,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        print("\nRender cancelled.", file=sys.stderr)
        return 130
    except ToolCancelledError as exc:
        print(f"Render cancelled: {exc}", file=sys.stderr)
        return 130
    except Exception as exc:
        emit_event("log", f"ERROR: {exc}")
        return 1
    finally:
        finish_events()

    if result.kind != definition.spec.result_kind:
        print(
            f"Unexpected tool result kind: {result.kind}",
            file=sys.stderr,
        )
        return 1
    output = result.data.get("path") if isinstance(result.data, dict) else None
    print(f"Output: {output or prepared.params.get('output_file', 'output.mp4')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
