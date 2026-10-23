"""音频查找、音视频 mux 与分段并行渲染导出流水线。"""

import math
import os
import pickle
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager

import pygame

from .. import config
from ..render.renderer import ManiaRenderer
from .recorder import VideoRecorder


class RenderCancelledError(RuntimeError):
    """Raised after a video render has been cancelled and rolled back."""


class _FrameStateBatchFile:
    """Small worker payload pointing to one materialized frame-state batch."""

    def __init__(self, path, count):
        self.path = path
        self.count = int(count)

    def __len__(self):
        return self.count

    def __iter__(self):
        with open(self.path, "rb") as file_handle:
            yield from pickle.load(file_handle)


def _cancelled(cancel_event):
    return bool(cancel_event is not None and cancel_event.is_set())


def _raise_if_cancelled(cancel_event):
    if _cancelled(cancel_event):
        raise RenderCancelledError("video rendering cancelled")


def _emit_status(status_cb, message):
    if status_cb is not None:
        try:
            status_cb(str(message))
        except Exception:
            pass


def _terminate_subprocess(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
    except Exception:
        pass


def _run_cancellable_process(cmd, cancel_event=None, timeout=None, cwd=None):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if _cancelled(cancel_event):
                _terminate_subprocess(proc)
                raise RenderCancelledError("video rendering cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                _terminate_subprocess(proc)
                return None
            time.sleep(0.1)
        return proc.returncode
    except BaseException:
        _terminate_subprocess(proc)
        raise


def find_audio_file(osu_path, audio_filename):
    """在谱面同目录查找 AudioFilename，找不到则打印路径并返回 None"""
    if not audio_filename:
        print("WARNING: beatmap has no AudioFilename, output will have no audio")
        return None

    osu_dir = os.path.dirname(os.path.abspath(osu_path))
    candidates = []
    if os.path.isabs(audio_filename):
        candidates.append(audio_filename)
    else:
        candidates.append(os.path.join(osu_dir, audio_filename))
        candidates.append(os.path.join(osu_dir, os.path.basename(audio_filename)))

    tried = []
    seen = set()
    for path in candidates:
        path = os.path.normpath(path)
        if path in seen:
            continue
        seen.add(path)
        tried.append(path)
        if os.path.isfile(path):
            return path

    print(f"WARNING: audio file not found: {audio_filename}; output will have no audio")
    return None


def mux_audio_to_video(
    video_path,
    audio_path,
    output_path,
    duration_sec,
    song_start_ms=0.0,
    cancel_event=None,
):
    """合并音视频；song_start_ms 为视频第 0 帧对应的谱面/音频时间"""
    ffmpeg = VideoRecorder._find_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", video_path]
    if song_start_ms < 0:
        cmd.extend(["-itsoffset", f"{(-song_start_ms / 1000.0):.6f}"])
        cmd.extend(["-ss", "0", "-i", audio_path])
    elif song_start_ms > 0:
        cmd.extend(["-ss", f"{(song_start_ms / 1000.0):.6f}", "-i", audio_path])
    else:
        cmd.extend(["-ss", "0", "-i", audio_path])
    cmd.extend([
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{duration_sec:.6f}",
        output_path,
    ])
    try:
        returncode = _run_cancellable_process(cmd, cancel_event)
    except RenderCancelledError:
        raise
    except Exception:
        return False
    return returncode == 0


def resolve_output_path(output_file):
    output_file = str(output_file or "output.mp4").strip().strip('"')
    output_file = os.path.expandvars(os.path.expanduser(output_file))
    if not os.path.splitext(output_file)[1]:
        output_file = output_file + ".mp4"
    output_file = os.path.abspath(output_file)
    return output_file


def _replace_output_file(src, dst, allow_overwrite=True):
    try:
        if allow_overwrite:
            os.replace(src, dst)
        elif os.name == "nt":
            os.rename(src, dst)
        else:
            os.link(src, dst)
            os.unlink(src)
    except FileExistsError as exc:
        raise FileExistsError(f"输出已存在，请重新提交并确认覆盖：{dst}") from exc


def _draw_hit_effects(renderer, hits, time_ms):
    flashed_cols = set()
    result_stages_drawn = set()
    for hit in hits:
        col, alpha = hit[0], hit[1]
        is_hold = hit[2] if len(hit) > 2 else False
        hit_age_ms = (
            hit[3] if len(hit) > 3
            else (1.0 - alpha) * config.HIT_FLASH_MS
        )
        hit_value = hit[4] if len(hit) > 4 else 320
        if hit_value > 0 and col in flashed_cols:
            continue
        if hit_value > 0:
            flashed_cols.add(col)
        result_stage_indices = tuple(
            stage_idx
            for stage_idx in renderer.judgement_stage_indices(col)
            if stage_idx not in result_stages_drawn
        )
        drew_result = renderer.draw_hit_flash(
            col,
            alpha,
            is_hold,
            bool(result_stage_indices),
            hit_age_ms,
            hit_value,
            time_ms - hit_age_ms,
            result_stage_indices,
        )
        if drew_result:
            result_stages_drawn.update(result_stage_indices)


def render_frame_state(renderer, skin_config, state):
    """Render one frame state using the exact production video draw order."""
    st = state
    animation_time_ms = st.get(
        "animation_time_ms",
        st.get("time_ms", 0.0),
    )
    renderer.reset_frame()
    renderer.draw_stage_hints(animation_time_ms)
    letterbox = st.get("letterbox", 0.0)
    if letterbox > 0.0:
        renderer.draw_letterbox(letterbox)
    pressed_cols = st.get("pressed_cols")

    renderer.draw_stage_lights(
        st.get("key_light_states"),
        animation_time_ms,
    )
    renderer.draw_barlines(st.get("barlines"))

    if skin_config.keys_under_notes:
        renderer.draw_receptors(pressed_cols)

    lane_rects = renderer.lane_rects
    for note in st["notes"]:
        x1, _, x2, _ = lane_rects[note["col"]]
        if note["is_hold"]:
            hold_states = st.get("hold_light_states", ())
            hold_state = (
                hold_states[note["col"]]
                if note["col"] < len(hold_states)
                else None
            )
            body_age = note.get("hold_animation_age_ms")
            if body_age is None:
                body_age = (
                    hold_state[1]
                    if hold_state and hold_state[0] == "hold"
                    else 0.0
                )
            renderer.draw_skin_hold(
                note["col"], x1, x2, note["y"], note["tail_y"],
                note.get("head_animation_age_ms", 0.0),
                note.get("tail_animation_age_ms", 0.0),
                note.get("is_holding", False),
                body_age,
            )
        else:
            renderer.draw_skin_tap(
                note["col"], x1, x2, note["y"],
                note.get("animation_age_ms", 0.0),
            )

    if not skin_config.keys_under_notes:
        renderer.draw_receptors(pressed_cols)

    renderer.draw_stage_foreground(animation_time_ms)
    renderer.draw_hold_lights(st.get("hold_light_states", ()))
    renderer.draw_warning_arrows(st.get("warning_arrows", ()))

    _draw_hit_effects(
        renderer,
        st.get("hits", []),
        st.get("time_ms", 0.0),
    )

    combo_burst_age = st.get("combo_burst_age_ms", -1.0)
    if combo_burst_age >= 0.0:
        renderer.draw_combo_burst(
            combo_burst_age,
            st.get("combo_burst_event_index", 0),
        )
    key_warning_age = st.get("key_warning_age_ms", -1.0)
    if key_warning_age >= 0.0:
        renderer.draw_key_warning(key_warning_age)

    renderer.draw_health_bar(
        st.get("health", 1.0),
        st.get("health_display"),
        st.get("health_marker_scale", 1.0),
        st.get("health_flash_age_ms", -1.0),
        st.get("health_flash_epic", False),
        animation_time_ms,
    )
    renderer.draw_score_display(st.get("score_display", st["score"]))
    renderer.draw_acc_display(st.get("acc_display", st.get("acc", 100.0)))
    renderer.draw_song_progress(st.get("progress", 0.0))
    renderer.draw_combo_display(
        st.get("combo_display", st["combo"]),
        st.get("combo_pop_age_ms", -1.0),
        st.get("combo_ghost_value", 0),
        st.get("combo_ghost_age_ms", -1.0),
        st.get("combo_holding", False),
        st.get("combo_show_age_ms", -1.0),
    )
    renderer.draw_hit_error_meter(
        st.get("hit_errors", ()),
        st.get("hit_error_avg", 0.0),
        st.get("hit_error_windows"),
    )
    return renderer.finish_frame()


def _render_encode_segment(args):
    with _silence_process_stdio():
        return _render_encode_segment_impl(args)


def _render_encode_segment_impl(args):
    (
        batch_states,
        seg_idx,
        width,
        height,
        fps,
        key_count,
        skin_config,
        run_id,
        runtime_options,
        allow_hw,
        *extra,
    ) = args
    config.configure_runtime(
        compatibility_mode=runtime_options["compatibility_mode"],
        fps=fps,
        width=width,
        height=height,
        scroll_speed=runtime_options["scroll_speed"],
        scroll_direction=runtime_options["scroll_direction"],
        scroll_ar_override=runtime_options["scroll_ar_override"],
        scroll_speed_mult=runtime_options["scroll_speed_mult"],
        hit_flash_ms=runtime_options["hit_flash_ms"],
        tail_ms=runtime_options["tail_ms"],
        show_hold_tail=runtime_options["show_hold_tail"],
        show_health_bar=runtime_options["show_health_bar"],
        show_barlines=runtime_options["show_barlines"],
        show_song_progress=runtime_options.get(
            "song_progress_mode",
            runtime_options.get("show_song_progress", True),
        ),
        show_hit_error=runtime_options.get("show_hit_error", True),
        show_warning_arrow=runtime_options.get("show_warning_arrow", True),
        background_path=runtime_options.get("background_path"),
        background_dim=runtime_options.get("background_dim", 0.8),
        letterbox_in_breaks=runtime_options.get("letterbox_in_breaks"),
        receptor_pressed=runtime_options["receptor_pressed"],
    )
    os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.init()

    temp_file = (
        extra[0]
        if extra
        else f"_osurender_{run_id}_seg_{seg_idx}.mp4"
    )
    heartbeat_file = extra[1] if len(extra) > 1 else None
    recorder = None
    try:
        _write_heartbeat(heartbeat_file, 0)
        renderer = ManiaRenderer(width, height, key_count, skin_config)
        recorder = VideoRecorder(width, height, fps=fps, output=temp_file, allow_hw=allow_hw, quiet=True)
        recorder.start()
        if not recorder.proc:
            return None

        last_heartbeat = time.monotonic()
        completed_frames = 0
        for st in batch_states:
            if not recorder.capture_frame(
                render_frame_state(renderer, skin_config, st),
            ):
                break
            completed_frames += 1
            now = time.monotonic()
            if heartbeat_file and now - last_heartbeat >= 1.0:
                _write_heartbeat(heartbeat_file, completed_frames)
                last_heartbeat = now

        _write_heartbeat(heartbeat_file, completed_frames)
        if recorder.stop():
            return temp_file
    except Exception:
        if recorder is not None:
            recorder._shutdown_proc()
    finally:
        pygame.quit()
    if os.path.exists(temp_file):
        try:
            os.remove(temp_file)
        except OSError:
            pass
    return None


def _valid_temp_video(path):
    if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return False
    ffmpeg = VideoRecorder._find_ffmpeg()
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-v", "error",
                "-i", path,
                "-map", "0:v:0",
                "-frames:v", "1",
                "-f", "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        return False
    return result.returncode == 0


def _cleanup_paths(paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@contextmanager
def _silence_process_stdio():
    saved_stdout = saved_stderr = devnull = None
    try:
        try:
            saved_stdout = os.dup(1)
            saved_stderr = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        except OSError:
            yield
            return
        yield
    finally:
        try:
            import ctypes
            ctypes.CDLL("msvcrt" if os.name == "nt" else None).fflush(None)
        except Exception:
            pass
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if devnull is not None:
            os.close(devnull)


def _segment_timeout_seconds(batch, fps=None):
    return float(config.SEGMENT_TIMEOUT_MIN_SEC)


def _expected_segment_path(args):
    if len(args) > 10:
        return args[10]
    return f"_osurender_{args[7]}_seg_{args[1]}.mp4"


def _heartbeat_path(args):
    return args[11] if len(args) > 11 else None


def _heartbeat_mtime(args):
    path = _heartbeat_path(args)
    if not path:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _heartbeat_progress(args):
    path = _heartbeat_path(args)
    if not path:
        return 0
    try:
        with open(path, "r", encoding="ascii") as file_handle:
            return max(0, int(file_handle.read().strip() or 0))
    except (OSError, ValueError):
        return 0


def _write_heartbeat(path, completed_frames):
    if not path:
        return
    try:
        with open(path, "w", encoding="ascii") as file_handle:
            file_handle.write(f"{max(0, int(completed_frames))}\n")
    except OSError:
        pass


def _terminate_pool(pool):
    try:
        pool.terminate()
    except Exception:
        pass
    for process in tuple(getattr(pool, "_pool", ())):
        try:
            process.join(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
            if process.is_alive():
                process.kill()
                process.join(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
        except Exception:
            pass


def _recover_finished_segment(args, result):
    if _valid_temp_video(result):
        return result
    expected = _expected_segment_path(args)
    if expected != result and _valid_temp_video(expected):
        return expected
    return None


def _render_segments_parallel(
    args_list,
    worker_count,
    cancel_event=None,
    status_cb=None,
    progress_cb=None,
):
    from multiprocessing import Pool

    results = [None] * len(args_list)
    timed_out = False
    cancelled = False
    pool = Pool(worker_count)
    submitted_at = time.monotonic()
    jobs = [pool.apply_async(_render_encode_segment, (args,)) for args in args_list]
    last_activity = [submitted_at] * len(args_list)
    heartbeat_mtimes = [None] * len(args_list)
    completed_frames = [0] * len(args_list)
    total_frames = sum(len(args[0]) for args in args_list)
    last_reported_frames = 0
    last_progress_at = submitted_at
    last_status_at = submitted_at
    pending = set(range(len(jobs)))
    if progress_cb is not None:
        progress_cb(0, total_frames)
    try:
        while pending:
            if _cancelled(cancel_event):
                cancelled = True
                break
            now = time.monotonic()
            for i in tuple(pending):
                job = jobs[i]
                if job.ready():
                    pending.remove(i)
                    try:
                        results[i] = job.get()
                        completed_frames[i] = len(args_list[i][0])
                        _emit_status(
                            status_cb,
                            f"Segment {args_list[i][1] + 1}/{len(args_list)} finished",
                        )
                    except Exception as exc:
                        print(f"WARNING: segment {args_list[i][1]} failed ({exc})")
                    continue
                heartbeat_mtime = _heartbeat_mtime(args_list[i])
                if heartbeat_mtime != heartbeat_mtimes[i]:
                    heartbeat_mtimes[i] = heartbeat_mtime
                    if heartbeat_mtime is not None:
                        last_activity[i] = now
                        completed_frames[i] = max(
                            completed_frames[i],
                            min(
                                len(args_list[i][0]),
                                _heartbeat_progress(args_list[i]),
                            ),
                        )
                stall_timeout = _segment_timeout_seconds(
                    args_list[i][0],
                    args_list[i][4],
                )
                if now - last_activity[i] >= stall_timeout:
                    print(f"WARNING: segment {args_list[i][1]} made no progress before timeout")
                    timed_out = True
            if timed_out:
                break
            rendered_frames = sum(completed_frames)
            should_report_progress = (
                rendered_frames != last_reported_frames
                and (now - last_progress_at >= 0.5 or not pending)
            )
            if should_report_progress:
                last_reported_frames = rendered_frames
                last_progress_at = now
                if progress_cb is not None:
                    progress_cb(rendered_frames, total_frames)
                if now - last_status_at >= 5.0:
                    _emit_status(
                        status_cb,
                        f"Rendering frames: {rendered_frames}/{total_frames}",
                    )
                    last_status_at = now
            if pending:
                time.sleep(0.1)
    finally:
        _terminate_pool(pool)

    for i, args in enumerate(args_list):
        results[i] = _recover_finished_segment(args, results[i])
    if cancelled:
        raise RenderCancelledError("video rendering cancelled")
    return results, timed_out


def _render_encode_segment_with_timeout(args, timeout_s=None, cancel_event=None):
    """在一次性 Pool(1) 中执行分段渲染，超时强制终止。

    顺序重试/单段路径直接跑在父进程，一旦 VideoRecorder 卡死（管道写/关闭
    在 C 层阻塞）就整进程挂起；放进独立 worker 后可用 terminate 兜底。
    """
    from multiprocessing import Pool

    if timeout_s is None:
        timeout_s = _segment_timeout_seconds(args[0], args[4])
    pool = Pool(1)
    result = None
    timed_out = False
    cancelled = False
    try:
        job = pool.apply_async(_render_encode_segment, (args,))
        last_activity = time.monotonic()
        heartbeat_mtime = None
        while not job.ready():
            if _cancelled(cancel_event):
                cancelled = True
                break
            now = time.monotonic()
            current_mtime = _heartbeat_mtime(args)
            if current_mtime != heartbeat_mtime:
                heartbeat_mtime = current_mtime
                if current_mtime is not None:
                    last_activity = now
            if now - last_activity >= timeout_s:
                timed_out = True
                print(f"WARNING: segment {args[1]} made no progress before retry timeout")
                break
            time.sleep(0.1)
        if not timed_out and not cancelled and job.ready():
            try:
                result = job.get()
            except Exception as exc:
                print(f"WARNING: segment {args[1]} failed on retry ({exc})")
    finally:
        _terminate_pool(pool)
    result = _recover_finished_segment(args, result)
    if cancelled:
        raise RenderCancelledError("video rendering cancelled")
    return result


def _retry_missing_segments(
    args_list,
    results,
    cancel_event=None,
    status_cb=None,
):
    fixed = list(results)
    for i, (args, path) in enumerate(zip(args_list, fixed)):
        _raise_if_cancelled(cancel_event)
        if _valid_temp_video(path):
            continue
        seg_idx = args[1]
        print(f"Retrying segment {seg_idx} with software encoder...")
        _emit_status(
            status_cb,
            f"Retrying segment {seg_idx + 1}/{len(args_list)} with software encoder",
        )
        retry_args = (
            (*args[:9], False, *args[10:])
            if len(args) > 10
            else (*args[:-1], False)
        )
        fixed[i] = _render_encode_segment_with_timeout(
            retry_args,
            cancel_event=cancel_event,
        )
        if not _valid_temp_video(fixed[i]):
            print(f"ERROR: segment {seg_idx} failed after retry")
            return fixed, False
    return fixed, True


def render_frame_states_to_video(
    frame_states,
    key_count,
    skin_config,
    output_file,
    fps=None,
    width=None,
    height=None,
    n_parallel=None,
    audio_path=None,
    start_song_ms=0.0,
    progress_cb=None,
    song_progress_mode=None,
    cancel_event=None,
    status_cb=None,
    allow_overwrite=True,
):
    if skin_config is None:
        raise ValueError("rendering requires a custom SkinConfig")
    if fps is None:
        fps = config.FPS
    if width is None:
        width = config.WIDTH
    if height is None:
        height = config.HEIGHT
    if n_parallel is None:
        n_parallel = config.N_PARALLEL
    max_frames = len(frame_states)
    if max_frames == 0:
        print("ERROR: no frames to encode")
        return False

    def _report(done, total):
        if progress_cb is not None:
            try:
                progress_cb(done, total)
            except Exception:
                pass

    _raise_if_cancelled(cancel_event)
    VideoRecorder._find_ffmpeg()
    output_file = resolve_output_path(output_file)
    output_dir = os.path.dirname(output_file) or os.getcwd()
    created_output_dirs = []
    candidate_dir = output_dir
    while not os.path.isdir(candidate_dir):
        created_output_dirs.append(candidate_dir)
        parent_dir = os.path.dirname(candidate_dir)
        if parent_dir == candidate_dir:
            break
        candidate_dir = parent_dir
    os.makedirs(output_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=".osu-toolbox-render-", dir=output_dir)
    # Windows creates tempfile.mkdtemp() with a protected DACL. Files moved out
    # of it keep that DACL, so create the commit candidate beside the target.
    staged_output = os.path.join(
        output_dir,
        f".osu-toolbox-output-{uuid.uuid4().hex}.mp4",
    )
    t0 = time.time()
    committed = False

    try:
        n_parallel = max(1, int(n_parallel))
        worker_count = max(1, min(n_parallel, max_frames))
        if worker_count == 1:
            batches = [frame_states]
        else:
            cache_started = time.perf_counter()
            _emit_status(
                status_cb,
                f"Preparing {max_frames} frame states for {worker_count} segments",
            )
            materialized = []
            for state in frame_states:
                _raise_if_cancelled(cancel_event)
                materialized.append(state)
            seg_size = int(math.ceil(max_frames / float(worker_count)))
            batches = []
            for segment_index, start in enumerate(range(0, max_frames, seg_size)):
                _raise_if_cancelled(cancel_event)
                stop = min(start + seg_size, max_frames)
                batch_path = os.path.join(
                    work_dir,
                    f"frame-states-{segment_index:04d}.pickle",
                )
                with open(batch_path, "wb") as file_handle:
                    pickle.dump(
                        materialized[start:stop],
                        file_handle,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                batches.append(_FrameStateBatchFile(batch_path, stop - start))
            del materialized
            worker_count = len(batches)
            _emit_status(
                status_cb,
                f"Frame-state cache ready in {time.perf_counter() - cache_started:.2f}s",
            )

        run_id = f"{os.getpid()}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        allow_hw_in_workers = worker_count == 1
        print(f"Rendering: {max_frames} frames @ {fps} FPS, {worker_count} segment(s)")
        print(f"Output: {output_file}")

        runtime_options = {
            "compatibility_mode": config.COMPATIBILITY_MODE,
            "scroll_speed": config.SCROLL_SPEED,
            "scroll_direction": config.SCROLL_DIRECTION,
            "scroll_ar_override": config.SCROLL_AR_OVERRIDE,
            "scroll_speed_mult": config.SCROLL_SPEED_MULT,
            "hit_flash_ms": config.HIT_FLASH_MS,
            "tail_ms": config.TAIL_MS,
            "show_hold_tail": config.SHOW_HOLD_TAIL,
            "show_health_bar": config.SHOW_HEALTH_BAR,
            "show_barlines": config.SHOW_BARLINES,
            "song_progress_mode": (
                config.SONG_PROGRESS_MODE
                if song_progress_mode is None
                else config.normalize_song_progress_mode(song_progress_mode)
            ),
            "show_hit_error": config.SHOW_HIT_ERROR,
            "show_warning_arrow": config.SHOW_WARNING_ARROW,
            "background_path": config.BACKGROUND_PATH,
            "background_dim": config.BACKGROUND_DIM,
            "letterbox_in_breaks": config.LETTERBOX_IN_BREAKS,
            "receptor_pressed": config.RECEPTOR_PRESSED,
        }
        args_list = [
            (
                batch,
                i,
                width,
                height,
                fps,
                key_count,
                skin_config,
                run_id,
                runtime_options,
                allow_hw_in_workers,
                os.path.join(work_dir, f"segment-{i:04d}.mp4"),
                os.path.join(work_dir, f"segment-{i:04d}.heartbeat"),
            )
            for i, batch in enumerate(batches)
            if batch
        ]

        if worker_count == 1:
            raw_temp_files = [
                (
                    _render_encode_segment_with_timeout(args_list[0])
                    if cancel_event is None
                    else _render_encode_segment_with_timeout(
                        args_list[0],
                        cancel_event=cancel_event,
                    )
                )
            ]
            timed_out = False
        else:
            _emit_status(status_cb, f"Starting {worker_count} render workers")
            if cancel_event is None and status_cb is None and progress_cb is None:
                raw_temp_files, timed_out = _render_segments_parallel(
                    args_list,
                    worker_count,
                )
            else:
                raw_temp_files, timed_out = _render_segments_parallel(
                    args_list,
                    worker_count,
                    cancel_event,
                    status_cb,
                    _report,
                )
        if timed_out:
            print("WARNING: parallel rendering timed out; retrying only incomplete segments sequentially.")

        if cancel_event is None and status_cb is None:
            raw_temp_files, retry_ok = _retry_missing_segments(
                args_list,
                raw_temp_files,
            )
        else:
            raw_temp_files, retry_ok = _retry_missing_segments(
                args_list,
                raw_temp_files,
                cancel_event,
                status_cb,
            )
        temp_files = [f for f in raw_temp_files if _valid_temp_video(f)]
        if not temp_files:
            print("ERROR: all segments failed to encode")
            return False
        if not retry_ok or len(temp_files) != len(args_list):
            print(f"ERROR: {len(args_list) - len(temp_files)} segment(s) failed to encode")
            return False

        _report(len(temp_files), len(args_list))
        _raise_if_cancelled(cancel_event)

        ffmpeg = VideoRecorder._find_ffmpeg()
        temp_video = os.path.join(work_dir, "video-noaudio.mp4")
        list_file = os.path.join(work_dir, "segments.txt")

        if len(temp_files) > 1:
            print(f"Combining: {len(temp_files)} segments")
            _emit_status(status_cb, f"Combining {len(temp_files)} rendered segments")
            with open(list_file, "w", encoding="utf-8") as file_handle:
                for temp_file in temp_files:
                    file_handle.write(f"file '{os.path.basename(temp_file)}'\n")
            returncode = _run_cancellable_process(
                [
                    ffmpeg,
                    "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", list_file,
                    "-c", "copy",
                    temp_video,
                ],
                cancel_event,
                cwd=work_dir,
            )
            if returncode != 0 or not _valid_temp_video(temp_video):
                print(f"ERROR: segment concat failed (ffmpeg exit code {returncode})")
                return False
        else:
            _replace_output_file(temp_files[0], temp_video)

        _raise_if_cancelled(cancel_event)
        duration_sec = max_frames / fps
        if audio_path:
            print("Audio: muxing")
            _emit_status(status_cb, "Muxing audio")
            if not mux_audio_to_video(
                temp_video,
                audio_path,
                staged_output,
                duration_sec,
                start_song_ms,
                cancel_event,
            ):
                print("ERROR: audio mux failed")
                return False
            if not _valid_temp_video(staged_output):
                print("ERROR: muxed output validation failed")
                return False
            print("Audio: muxed")
        else:
            shutil.copyfile(temp_video, staged_output)

        _raise_if_cancelled(cancel_event)
        _replace_output_file(staged_output, output_file, allow_overwrite=allow_overwrite)
        committed = True
        _report(1, 1)
        print(f"Done: {output_file} ({time.time() - t0:.2f}s)")
        return True
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if os.path.exists(staged_output):
            try:
                os.remove(staged_output)
            except OSError:
                pass
        if not committed:
            for created_dir in created_output_dirs:
                try:
                    os.rmdir(created_dir)
                except OSError:
                    break
