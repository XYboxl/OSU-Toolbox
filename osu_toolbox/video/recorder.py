"""FFmpeg 单进程视频录制。"""

import os
import subprocess
import threading
from queue import Empty, Full, Queue

import numpy as np

from .. import config
from ..external_tools import require_ffmpeg


class VideoRecorder:
    ENCODER_CMDS = {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p1", "-pix_fmt", "yuv420p"],
        "h264_qsv": ["-c:v", "h264_qsv", "-preset", "veryfast"],
        "h264_amf": ["-c:v", "h264_amf", "-quality", "speed", "-pix_fmt", "yuv420p"],
    }

    SOFTWARE_CMD = [
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-threads", "1", "-pix_fmt", "yuv420p", "-crf", "18",
    ]

    @staticmethod
    def _find_ffmpeg():
        return require_ffmpeg()

    @staticmethod
    def _detect_hw_encoders():
        try:
            ffmpeg = VideoRecorder._find_ffmpeg()
        except FileNotFoundError:
            return []
        try:
            out = subprocess.run(
                [ffmpeg, "-encoders"], capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return []
        return [enc for enc in ("h264_qsv", "h264_nvenc", "h264_amf") if enc in out]

    def __init__(
        self,
        width,
        height,
        fps=None,
        output="output.mp4",
        allow_hw=True,
        quiet=False,
        cancel_event=None,
    ):
        self.width = width
        self.height = height
        self.fps = config.FPS if fps is None else fps
        self.output = output
        self.quiet = quiet
        self.cancel_event = cancel_event
        self.proc = None
        self.ffmpeg_path = self._find_ffmpeg()
        self.hw_candidates = self._detect_hw_encoders() if allow_hw else []
        self.active_encoder = None
        self._empty_frame = np.zeros((height, width, 3), dtype=np.uint8)
        self._frame_queue = None
        self._writer_thread = None
        self._write_error = None

    def _build_cmd_for(self, encoder):
        base = [
            self.ffmpeg_path, "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
            "-i", "-",
        ]
        extra = self.ENCODER_CMDS.get(encoder, self.SOFTWARE_CMD)
        return base + extra + [self.output]

    def _writer_loop(self):
        while True:
            item = self._frame_queue.get()
            if item is None:
                self._frame_queue.task_done()
                break
            try:
                view = memoryview(item).cast("B")
                while view:
                    written = self.proc.stdin.write(view)
                    if not written:
                        raise BrokenPipeError("ffmpeg stdin closed during frame write")
                    view = view[written:]
            except Exception as e:
                self._write_error = e
                self._frame_queue.task_done()
                break
            self._frame_queue.task_done()

    def _cancelled(self):
        return bool(self.cancel_event is not None and self.cancel_event.is_set())

    @staticmethod
    def _close_stdin_immediately(proc):
        """非阻塞关闭 ffmpeg stdin：绕过 BufferedWriter.close() 的 flush 阻塞。

        BufferedWriter.close() 会先把缓冲刷进管道，ffmpeg 停读（编码器卡死）
        时 flush 在 C 层永久阻塞且 try/except 抓不住。直接关闭底层 fd 不 flush，
        丢尾部缓冲可接受——仅在编码已失败/卡死的清理路径使用。
        """
        if proc is None or proc.stdin is None:
            return
        try:
            fd = proc.stdin.fileno()
        except Exception:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass

    @staticmethod
    def _kill_and_wait(proc, timeout=None):
        """强制终止并有限等待，避免 kill 后无界 wait 卡死。"""
        if proc is None:
            return
        timeout = config.ENCODER_SHUTDOWN_TIMEOUT_SEC if timeout is None else timeout
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            pass

    def start(self):
        for enc in self.hw_candidates + [None]:
            cmd = self._build_cmd_for(enc)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except FileNotFoundError:
                continue
            if proc.poll() is None:
                enc_name = enc or "libx264 (software)"
                self.proc = proc
                self.active_encoder = enc_name
                self._frame_queue = Queue(maxsize=config.FRAME_QUEUE_SIZE)
                self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
                self._writer_thread.start()
                if not self.quiet:
                    print(f"Recording to {self.output} [{enc_name}] ...")
                return
            proc.kill()
        if not self.quiet:
            print("WARNING: ffmpeg not found or no encoder works, video export disabled.")

    def capture_frame(self, frame_array=None):
        if not self.proc or not self._frame_queue or self._write_error:
            return False
        if self._cancelled():
            self.cancel()
            return False
        if self.proc.poll() is not None:
            self._write_error = RuntimeError(f"ffmpeg exited with code {self.proc.returncode}")
            return False
        frame = frame_array if frame_array is not None else self._empty_frame
        queued_frame = frame.copy()
        while True:
            if self._cancelled():
                self.cancel()
                return False
            if self._write_error:
                return False
            if not self.proc or self.proc.poll() is not None:
                code = self.proc.returncode if self.proc else "unknown"
                self._write_error = RuntimeError(f"ffmpeg exited with code {code}")
                return False
            try:
                # Backpressure is expected when high-resolution encoding is slower
                # than frame composition. Poll so cancellation and encoder failure
                # remain responsive without treating normal slowness as an error.
                self._frame_queue.put(queued_frame, timeout=0.25)
                return True
            except Full:
                continue
            except Exception as e:
                if not self.quiet:
                    print(f"WARNING: capture frame failed ({e})")
                self._write_error = e
                self._shutdown_proc()
                return False

    def _shutdown_proc(self):
        if self.proc:
            self._close_stdin_immediately(self.proc)
            try:
                self.proc.wait(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                self._kill_and_wait(self.proc)
            self.proc = None

    def cancel(self):
        self._shutdown_proc()
        if self._writer_thread is not None and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
        if self._frame_queue is not None:
            while True:
                try:
                    self._frame_queue.get_nowait()
                    self._frame_queue.task_done()
                except Empty:
                    break

    def stop(self):
        if self._cancelled():
            self.cancel()
            return False
        if self._write_error:
            if not self.quiet:
                print(f"WARNING: encode failed ({self._write_error})")
            self.cancel()
            return False
        if self._frame_queue is not None and self._writer_thread is not None:
            while self._writer_thread.is_alive():
                if self._cancelled():
                    self.cancel()
                    return False
                if self._write_error:
                    self.cancel()
                    return False
                try:
                    self._frame_queue.put(None, timeout=0.25)
                    break
                except Full:
                    if not self.proc or self.proc.poll() is not None:
                        code = self.proc.returncode if self.proc else "unknown"
                        self._write_error = RuntimeError(f"ffmpeg exited with code {code}")
                        self.cancel()
                        return False
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
                if self._writer_thread.is_alive() and not self._write_error:
                    self._write_error = TimeoutError("encoder writer thread did not stop")
                    self._shutdown_proc()
        if self._write_error:
            if not self.quiet:
                print(f"WARNING: encode failed ({self._write_error})")
            self.cancel()
            return False
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                # flush 阻塞或已关闭时回退非阻塞关闭，避免挂起
                self._close_stdin_immediately(self.proc)
            try:
                self.proc.wait(timeout=config.ENCODER_SHUTDOWN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                self._kill_and_wait(self.proc)
                if not self.quiet:
                    print("WARNING: encode failed (ffmpeg did not exit)")
                self.proc = None
                return False
            if not self.quiet:
                print(f"Done: {self.output}")
            self.proc = None
        return bool(os.path.exists(self.output) and os.path.getsize(self.output) > 0)
