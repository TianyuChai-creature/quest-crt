"""Camera-independent RGB video input, isolated in a GStreamer worker process."""

from __future__ import annotations

import json
import math
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any, Literal

try:
    import fcntl
except ImportError:
    fcntl = None


class VideoUnavailableError(RuntimeError):
    """The native video backend could not start or has stopped."""


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True)
class CameraIntrinsics:
    """Pinhole intrinsics in pixels for the submitted rectified eye image."""

    fx: float
    fy: float
    cx: float
    cy: float

    def __post_init__(self) -> None:
        for name in ("fx", "fy", "cx", "cy"):
            _finite(getattr(self, name), name)
        if self.fx <= 0 or self.fy <= 0:
            raise ValueError("fx and fy must be positive")


@dataclass(frozen=True)
class VideoConfig:
    width: int
    height: int
    mode: Literal["mono", "stereo"] = "mono"
    fps: int = 60
    start_bitrate_mbps: float = 8
    max_bitrate_mbps: float = 16
    worker_python: str = "/usr/bin/python3"
    left_intrinsics: CameraIntrinsics | None = None
    right_intrinsics: CameraIntrinsics | None = None

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0 or value % 2:
                raise ValueError(f"{name} must be a positive even integer")
        if self.mode not in ("mono", "stereo"):
            raise ValueError("mode must be mono or stereo")
        if type(self.fps) is not int or not 1 <= self.fps <= 60:
            raise ValueError("fps must be an integer within [1, 60]")
        for name in ("start_bitrate_mbps", "max_bitrate_mbps"):
            _finite(getattr(self, name), name)
        if not 0 < self.start_bitrate_mbps <= self.max_bitrate_mbps <= 50:
            raise ValueError("require 0 < start_bitrate_mbps <= max_bitrate_mbps <= 50")
        encoded_width = self.width * (2 if self.mode == "stereo" else 1)
        if encoded_width > 4096 or self.height > 4096:
            raise ValueError("encoded video dimensions exceed the NVENC 4096-pixel limit")
        macroblocks = ((encoded_width + 15) // 16) * ((self.height + 15) // 16)
        if macroblocks > 8704 or macroblocks * self.fps > 522240:
            raise ValueError("video dimensions and fps exceed H.264 level 4.2")
        if not isinstance(self.worker_python, str) or not self.worker_python:
            raise ValueError("worker_python must name a Python executable")
        for name in ("left_intrinsics", "right_intrinsics"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, CameraIntrinsics):
                raise TypeError(f"{name} must be CameraIntrinsics or None")
        if self.mode == "mono" and self.right_intrinsics is not None:
            raise ValueError("mono input only uses left_intrinsics")
        if self.mode == "stereo" and (self.left_intrinsics is None) != (
            self.right_intrinsics is None
        ):
            raise ValueError("stereo input requires intrinsics for both eyes")


@dataclass(frozen=True)
class VideoDisplayConfig:
    swap_eyes: bool = False
    saturation: float = 1
    gamma: float = 1
    projection: Literal["camera", "plane"] = "camera"
    height_m: float = 1
    distance_m: float = 1
    aspect_ratio: float | None = None
    offset_x_m: float = 0
    offset_y_m: float = 0

    def __post_init__(self) -> None:
        _finite(self.saturation, "saturation")
        _finite(self.gamma, "gamma")
        if not 0 <= self.saturation <= 2:
            raise ValueError("saturation must be within [0, 2]")
        if not 0.5 <= self.gamma <= 2:
            raise ValueError("gamma must be within [0.5, 2]")
        if type(self.swap_eyes) is not bool:
            raise ValueError("swap_eyes must be boolean")
        if self.projection not in ("camera", "plane"):
            raise ValueError("projection must be camera or plane")
        for name in ("height_m", "distance_m", "offset_x_m", "offset_y_m"):
            _finite(getattr(self, name), name)
        if not 0.05 <= self.height_m <= 100 or not 0.05 <= self.distance_m <= 100:
            raise ValueError("height_m and distance_m must be within [0.05, 100]")
        if abs(self.offset_x_m) > 100 or abs(self.offset_y_m) > 100:
            raise ValueError("plane offsets must be within [-100, 100]")
        if self.aspect_ratio is not None:
            _finite(self.aspect_ratio, "aspect_ratio")
            if not 0.1 <= self.aspect_ratio <= 10:
                raise ValueError("aspect_ratio must be within [0.1, 10] or None")


# Shared header: pending slot, processing slot, pending sequence, capture timestamp.
# flock makes publication / claiming atomic; a processing slot is never overwritten.
_HEADER = struct.Struct("<iiQQ")
_WORKER_PATH = Path(__file__).with_name("video_worker.py")


class VideoManager:
    """Own one video process. submit() copies RGB8 buffers; callers may then reuse them.

    Stereo inputs must already be synchronized and rectified. timestamp_ns, when
    supplied, is an increasing capture timestamp on one monotonic camera clock.
    Video failures do not call into or own any pose service.
    """

    def __init__(self) -> None:
        self._lifecycle = threading.RLock()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._offer_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._memory: SharedMemory | None = None
        self._lock_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._config: VideoConfig | None = None
        self._display = VideoDisplayConfig()
        self._enabled = False
        self._error: str | None = None
        self._expected_exit = False
        self._worker_used = False
        self._worker_ready = False
        self._worker_peer_id: str | None = None
        self._cancelled_peer_ids: deque[str] = deque(maxlen=64)
        self._request_id = 0
        self._stream_generation = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._stderr: deque[str] = deque(maxlen=20)
        self._last_submit_ns: int | None = None
        self._last_capture_ns: int | None = None
        self._submitted = 0
        self._dropped = 0
        self._latest_slot = -1
        self._worker_stats: dict[str, Any] = {}
        self._worker_exit_code: int | None = None
        self._worker_exit_forced_cleanup = False

    def start(self, config: VideoConfig, timeout: float = 10) -> None:
        previous = (self._reader, self._stderr_reader)
        try:
            self._start_process(config, timeout)
        finally:
            if self._reader is not previous[0] or not self._enabled:
                self._join_readers(previous)
            if not self._enabled:
                self._join_readers((self._reader, self._stderr_reader))

    def _start_process(self, config: VideoConfig, timeout: float) -> None:
        if not isinstance(config, VideoConfig):
            raise TypeError("config must be VideoConfig")
        self._check_timeout(timeout)
        if sys.platform != "linux" or fcntl is None:
            with self._lock:
                self._error = "RGB video requires Linux with GStreamer and NVIDIA NVENC"
            raise VideoUnavailableError(self._error)
        previous = (self._reader, self._stderr_reader)
        self._stop_process(timeout=timeout)
        self._join_readers(previous)
        with self._lifecycle:
            with self._lock:
                self._config = config
                self._stream_generation += 1
                self._error = None
                self._stderr.clear()
                self._submitted = self._dropped = 0
                self._latest_slot = -1
                self._last_submit_ns = self._last_capture_ns = None
                self._worker_stats.clear()
                self._worker_exit_code = None
                self._worker_exit_forced_cleanup = False
                self._expected_exit = False
            try:
                slot_size = config.width * config.height * 3 * (2 if config.mode == "stereo" else 1)
                memory = SharedMemory(create=True, size=_HEADER.size + slot_size * 2)
                with self._lock:
                    self._memory = memory
                    self._lock_fd = os.open(f"/dev/shm/{memory.name}", os.O_RDWR)
                    _HEADER.pack_into(memory.buf, 0, -1, -1, 0, 0)
            except BaseException as exc:
                with self._lock:
                    self._error = str(exc)
                self._stop_process(timeout=min(timeout, 1))
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise VideoUnavailableError(str(exc)) from exc
        self._spawn_process(memory, timeout)

    def _spawn_process(self, memory: SharedMemory, timeout: float) -> None:
        # Only setup holds lifecycle; ready/probe waits must allow stop().
        with self._lifecycle, self._lock:
            if self._memory is not memory:
                raise VideoUnavailableError("video source stopped during worker startup")
            if self._process is not None:
                raise VideoUnavailableError("previous video worker has not been reaped")
            config = self._config
            assert config is not None
            try:
                process = subprocess.Popen(
                    [
                        config.worker_python,
                        "-u",
                        str(_WORKER_PATH),
                        memory.name,
                        str((memory.size - _HEADER.size) // 2),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                self._error = str(exc)
                self._dispose(None)
                raise VideoUnavailableError(str(exc)) from exc
            self._process = process
            self._expected_exit = self._worker_used = False
            self._worker_ready = False
            self._worker_peer_id = None
            self._stderr.clear()
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            self._reader = threading.Thread(
                target=self._read_replies,
                args=(process,),
                name="quest-video-control",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                args=(process,),
                name="quest-video-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
        try:
            self._request(
                "start",
                {"config": asdict(config), "frames_sent": self._worker_stats.get("frames_sent", 0)},
                timeout,
                process=process,
            )
            with self._lock:
                if (
                    self._process is not process
                    or process.poll() is not None
                    or self._expected_exit
                ):
                    raise VideoUnavailableError(self._error or "video worker exited during startup")
                self._enabled = True
                self._worker_ready = True
        except BaseException as exc:
            with self._lifecycle:
                with self._lock:
                    current = self._process is process
                    if current:
                        self._error = str(exc)
                if current:
                    self._stop_process(timeout=min(timeout, 1))
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise VideoUnavailableError(str(exc)) from exc

    @staticmethod
    def _check_timeout(timeout: float) -> None:
        _finite(timeout, "timeout")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

    def _request(
        self,
        operation: str,
        fields: dict[str, Any],
        timeout: float,
        *,
        process: subprocess.Popen[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            process = process if process is not None else self._process
            if process is None or self._process is not process or process.poll() is not None:
                raise VideoUnavailableError(self._error or "video is not running")
            self._request_id += 1
            request_id = self._request_id
            reply: dict[str, Any] = {"event": threading.Event()}
            self._pending[request_id] = reply
        write_timed_out = False
        try:
            deadline = time.monotonic() + timeout
            message = (json.dumps({"id": request_id, "op": operation, **fields}) + "\n").encode()
            if not self._write_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                raise TimeoutError(f"video {operation} control writer timed out")
            try:
                assert process.stdin is not None
                remaining = memoryview(message)
                try:
                    while remaining:
                        available = deadline - time.monotonic()
                        if (
                            available <= 0
                            or not select.select([], [process.stdin], [], available)[1]
                        ):
                            # A partial JSON write cannot be followed by another request safely.
                            with self._lock:
                                self._error = f"video {operation} write timed out"
                            write_timed_out = True
                            raise TimeoutError(f"video {operation} write timed out")
                        try:
                            written = os.write(process.stdin.fileno(), remaining)
                        except BlockingIOError:
                            continue
                        previous, remaining = remaining, remaining[written:]
                        previous.release()
                finally:
                    remaining.release()
            finally:
                self._write_lock.release()
            if not reply["event"].wait(max(0, deadline - time.monotonic())):
                raise TimeoutError(f"video {operation} timed out")
            if "error" in reply:
                if reply.get("kind") == "ValueError":
                    raise ValueError(reply["error"])
                if reply.get("kind") == "TimeoutError":
                    raise TimeoutError(reply["error"])
                raise VideoUnavailableError(reply["error"])
            return reply["result"]
        except TimeoutError:
            if write_timed_out:
                with self._lifecycle:
                    self._dispose(process)
            raise
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and reply.get("kind") == "ValueError":
                raise
            raise VideoUnavailableError(f"video control pipe failed: {exc}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        try:
            for line in process.stderr:
                with self._lock:
                    if self._process is process:
                        self._stderr.append(line.strip()[:1000])
        except (OSError, ValueError):
            pass

    def _read_replies(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                message = json.loads(line)
                with self._lock:
                    if self._process is not process:
                        break
                    if message.get("event") == "error":
                        self._error = str(message["error"])
                        self._enabled = False
                        break
                    elif message.get("event") == "stats":
                        self._worker_stats = message["stats"]
                    else:
                        reply = self._pending.get(message.get("id"))
                        if reply is not None:
                            reply.update(message)
                            reply["event"].set()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            with self._lock:
                if self._process is process:
                    self._error = f"video control protocol failed: {exc}"
        finally:
            self._reader_exited(process)

    def _reader_exited(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is not process:
                return
            message = self._error or "video worker exited"
            for reply in self._pending.values():
                reply.update(error=message)
                reply["event"].set()
            # The owner is waiting/joining this expected exit. It disposes
            # the child and chooses whether to retain the shared source.
            if self._expected_exit:
                return
        with self._lifecycle:
            if self._process is process:
                with self._lock:
                    expected_exit = self._expected_exit
                    self._enabled = False
                if not expected_exit:
                    # EOF can precede waitpid becoming ready; preserve a natural
                    # exit status before cleanup considers sending a signal.
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                self._dispose(process)
                with self._lock:
                    if not expected_exit:
                        code = self._worker_exit_code
                        suffix = f"returncode={code}"
                        if code is not None and code < 0:
                            try:
                                suffix += f", {signal.Signals(-code).name}"
                            except ValueError:
                                suffix += f", signal {-code}"
                        if self._worker_exit_forced_cleanup:
                            suffix += ", forced cleanup"
                        detail = "; ".join(self._stderr)
                        self._error = f"{self._error or 'video worker exited'} ({suffix})"
                        if detail:
                            self._error += f": {detail}"

    def _dispose(self, process: subprocess.Popen[str] | None, *, keep_source: bool = False) -> bool:
        forced_cleanup = False
        cleanup_codes: tuple[int, ...] = ()
        if process is not None:
            if process.poll() is None:
                forced_cleanup = True
                process.terminate()
                cleanup_codes = (-signal.SIGTERM,)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    cleanup_codes += (-signal.SIGKILL,)
                    process.wait(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        with self._lock:
            if process is not None and self._process is not process:
                return False
            if process is not None:
                self._worker_exit_code = process.returncode
                self._worker_exit_forced_cleanup = forced_cleanup
                intentional_signal = process.returncode in cleanup_codes
                if keep_source and (
                    not self._enabled or (process.returncode != 0 and not intentional_signal)
                ):
                    keep_source = False
                    self._error = (
                        self._error
                        or f"video worker exited unexpectedly during recycle (returncode={process.returncode})"
                    )
            self._process = None
            self._worker_ready = False
            if (
                self._worker_peer_id is not None
                and self._worker_peer_id not in self._cancelled_peer_ids
            ):
                self._cancelled_peer_ids.append(self._worker_peer_id)
            self._worker_peer_id = None
            self._worker_stats.update(
                peer_connected=False,
                peer_connection_state="closed",
                negotiated_codec=None,
                negotiation_stage="stopped",
            )
            if keep_source:
                return True
            self._enabled = False
            self._last_submit_ns = self._last_capture_ns = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            if self._memory is not None:
                self._memory.close()
                self._memory.unlink()
                self._memory = None
            return False

    def stop(self, timeout: float = 10) -> None:
        readers = (self._reader, self._stderr_reader)
        self._stop_process(timeout)
        self._join_readers(readers)

    @staticmethod
    def _join_readers(readers: tuple[threading.Thread | None, ...]) -> None:
        for thread in readers:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
                if thread.is_alive():
                    raise RuntimeError("video control reader did not exit after process shutdown")

    def _stop_process(self, timeout: float, *, keep_source: bool = False) -> None:
        self._check_timeout(timeout)
        with self._lifecycle:
            with self._lock:
                process = self._process
                dead_before_stop = process is not None and process.poll() is not None
                self._expected_exit = True
                self._worker_ready = False
                if not keep_source:
                    self._enabled = False
            if process is not None:
                deadline = time.monotonic() + timeout
                if process.poll() is None:
                    try:
                        self._request("stop", {}, min(timeout, 1))
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except (
                        VideoUnavailableError,
                        TimeoutError,
                        subprocess.TimeoutExpired,
                        OSError,
                    ):
                        pass
                with self._lock:
                    preserve = (
                        keep_source
                        and self._enabled
                        and not dead_before_stop
                        and process.poll() in (None, 0)
                    )
                    if keep_source and not preserve:
                        self._error = (
                            self._error
                            or f"video worker exited unexpectedly during recycle (returncode={process.poll()})"
                        )
                try:
                    preserve = self._dispose(process, keep_source=preserve)
                except BaseException:
                    # Never let another child attach to a source still owned by
                    # a worker whose termination could not be confirmed.
                    with self._lock:
                        self._enabled = False
                    raise
                if keep_source and not preserve:
                    raise VideoUnavailableError(self._error)
            else:
                self._dispose(None, keep_source=keep_source)

    def submit(self, left: Any, right: Any = None, timestamp_ns: int | None = None) -> bool:
        with self._lock:
            config = self._config
            if not self._enabled or self._memory is None:
                return False
            assert config is not None and self._memory is not None and self._lock_fd is not None
            if (config.mode == "stereo") != (right is not None):
                raise ValueError("stereo requires both eyes; mono accepts only left")
            captured = time.monotonic_ns() if timestamp_ns is None else timestamp_ns
            if type(captured) is not int or not 0 <= captured <= (1 << 64) - 1:
                raise ValueError("timestamp_ns must be a nonnegative uint64 integer")
            if self._last_capture_ns is not None and captured <= self._last_capture_ns:
                raise ValueError("timestamp_ns must increase monotonically")
            size = config.width * config.height * 3
            buffers: list[memoryview] = []
            try:
                for image in (left, right) if config.mode == "stereo" else (left,):
                    view = memoryview(image)
                    buffers.append(view)
                    if not view.c_contiguous or view.format != "B" or view.nbytes != size:
                        raise ValueError(
                            f"each RGB8 image must be contiguous and exactly {size} bytes"
                        )
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
                try:
                    pending, processing, sequence, _ = _HEADER.unpack_from(self._memory.buf)
                    slot = pending if pending >= 0 else (1 if processing == 0 else 0)
                    if pending >= 0:
                        self._dropped += 1
                    offset = _HEADER.size + slot * size * len(buffers)
                    for view in buffers:
                        flat = view.cast("B")
                        try:
                            self._memory.buf[offset : offset + size] = flat
                        finally:
                            flat.release()
                        offset += size
                    _HEADER.pack_into(self._memory.buf, 0, slot, processing, sequence + 1, captured)
                    self._latest_slot = slot
                finally:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                for view in buffers:
                    view.release()
            self._submitted += 1
            self._last_submit_ns = time.monotonic_ns()
            self._last_capture_ns = captured
            return True

    def set_display(self, config: VideoDisplayConfig) -> None:
        if not isinstance(config, VideoDisplayConfig):
            raise TypeError("display must be VideoDisplayConfig")
        with self._lock:
            self._display = config

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self._enabled and self._memory is not None
            worker_running = self._process is not None and self._process.poll() is None
            snapshot = {
                "enabled": running,
                "negotiated_codec": self._worker_stats.get("negotiated_codec"),
                "running": running,
                "worker_running": worker_running,
                "error": self._error,
                "config": asdict(self._config) if self._config is not None else None,
                "stream_generation": self._stream_generation,
                "display": asdict(self._display),
                "frame_age_ms": (time.monotonic_ns() - self._last_submit_ns) / 1e6
                if running and self._last_submit_ns is not None
                else None,
                "submitted_frames": self._submitted,
                "dropped_frames": self._dropped,
                **self._worker_stats,
                "worker_exit_code": self._worker_exit_code,
                "worker_exit_forced_cleanup": self._worker_exit_forced_cleanup,
            }
            if not self._worker_ready or not worker_running or not running:
                snapshot.update(
                    negotiated_codec=None,
                    peer_connected=False,
                    peer_connection_state="closed",
                    ice_gathering_state="new",
                    negotiation_stage="stopped",
                    negotiation_stage_age_ms=None,
                    last_capture_timestamp_ns=None,
                )
            return snapshot

    @staticmethod
    def _peer_id(peer_id: Any) -> str:
        from uuid import UUID

        if not isinstance(peer_id, UUID) and (not isinstance(peer_id, str) or len(peer_id) > 36):
            raise ValueError("peer_id must be a UUID of at most 36 characters")
        try:
            return str(peer_id if isinstance(peer_id, UUID) else UUID(peer_id))
        except ValueError as exc:
            raise ValueError("peer_id must be a valid UUID") from exc

    def offer(
        self, sdp: str, type: str = "offer", *, peer_id: Any, timeout: float = 10
    ) -> dict[str, Any]:
        self._check_timeout(timeout)
        peer_id = self._peer_id(peer_id)
        if type != "offer" or not isinstance(sdp, str) or not sdp or len(sdp) > 1_000_000:
            raise ValueError("video requires a nonempty SDP offer of at most 1 MB")
        if not self._offer_lock.acquire(blocking=False):
            raise ValueError("video negotiation already in progress")
        deadline = time.monotonic() + timeout

        def remaining():
            available = deadline - time.monotonic()
            if available <= 0:
                raise TimeoutError("video offer timed out during worker recycle")
            return available

        try:
            with self._lock:
                if peer_id in self._cancelled_peer_ids:
                    raise ValueError("video peer was cancelled")
                if not self._enabled or self._memory is None:
                    raise VideoUnavailableError(self._error or "video is not running")
                if peer_id == self._worker_peer_id:
                    raise ValueError("video peer_id must be fresh for each offer")
                memory, used, previous_process = self._memory, self._worker_used, self._process
            if used:
                readers = (self._reader, self._stderr_reader)
                try:
                    self._stop_process(remaining(), keep_source=True)
                finally:
                    if previous_process is None or previous_process.poll() is not None:
                        self._join_readers(readers)
                with self._lock:
                    if self._memory is not memory or not self._enabled:
                        raise VideoUnavailableError("video source stopped during worker recycle")
                    # No old worker can still own a processing slot now.
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
                    try:
                        pending, _processing, sequence, captured = _HEADER.unpack_from(memory.buf)
                        _HEADER.pack_into(
                            memory.buf,
                            0,
                            pending if pending >= 0 else self._latest_slot,
                            -1,
                            sequence,
                            captured,
                        )
                    finally:
                        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._spawn_process(memory, remaining())
            with self._lock:
                if peer_id in self._cancelled_peer_ids:
                    raise ValueError("video peer was cancelled")
                if self._memory is not memory or not self._enabled:
                    raise VideoUnavailableError("video source stopped during offer")
                process = self._process
                self._worker_used = True
                self._worker_peer_id = peer_id
            return self._request(
                "offer",
                {"sdp": sdp, "type": type, "peer_id": peer_id, "timeout": timeout},
                remaining(),
                process=process,
            )
        finally:
            self._offer_lock.release()

    def close_peer(self, peer_id: Any, timeout: float = 10) -> bool:
        self._check_timeout(timeout)
        peer_id = self._peer_id(peer_id)
        with self._lock:
            if peer_id not in self._cancelled_peer_ids:
                self._cancelled_peer_ids.append(peer_id)
            process = self._process
            if peer_id != self._worker_peer_id or process is None or process.poll() is not None:
                return False
        return self._request("close_peer", {"peer_id": peer_id}, timeout, process=process)["closed"]
