"""Bounded background pose recording and retention."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self, TextIO

logger = logging.getLogger(__name__)
POSE_LOG_QUEUE_FRAMES = 2048
LOG_SEGMENT_MAX_BYTES = 256 * 1024 * 1024
LOG_SEGMENT_MAX_SECONDS = 15 * 60
LOG_RETENTION_MAX_BYTES = 5 * 1024 * 1024 * 1024


class LogRetentionManager:
    """Track generated log sizes and delete the oldest closed segments."""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self._directory = directory
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._sizes: dict[Path, int] = {}
        self._active: set[Path] = set()
        self._total_bytes = 0
        self._initialized = False

    def register_active(self, path: Path) -> None:
        with self._lock:
            self._initialize_locked()
            size = path.stat().st_size if path.is_file() else 0
            previous_size = self._sizes.get(path, 0)
            self._sizes[path] = size
            self._total_bytes += size - previous_size
            self._active.add(path)
            self._enforce_locked()

    def note_size(self, path: Path, size: int) -> None:
        with self._lock:
            self._initialize_locked()
            previous_size = self._sizes.get(path, 0)
            self._sizes[path] = size
            self._total_bytes += size - previous_size
            self._enforce_locked()

    def close_active(self, path: Path) -> None:
        with self._lock:
            self._initialize_locked()
            self._active.discard(path)
            self._enforce_locked()

    def _initialize_locked(self) -> None:
        if self._initialized:
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        for path in self._directory.glob("pose_*.jsonl"):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self._sizes[path] = size
            self._total_bytes += size
        self._initialized = True

    def _enforce_locked(self) -> None:
        if self._total_bytes <= self._max_bytes:
            return

        candidates: list[tuple[int, str, Path]] = []
        for path in self._sizes:
            if path in self._active:
                continue
            try:
                modified_ns = path.stat().st_mtime_ns
            except OSError:
                modified_ns = 0
            candidates.append((modified_ns, path.name, path))

        for _, _, path in sorted(candidates):
            if self._total_bytes <= self._max_bytes:
                break
            size = self._sizes.pop(path, 0)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._sizes[path] = size
                logger.warning("Unable to remove old pose log %s: %s", path, exc)
                continue
            self._total_bytes -= size


class RotatingPoseLog:
    """Write JSONL records into size/time bounded segments."""

    def __init__(
        self,
        directory: Path,
        stamp: str,
        retention: LogRetentionManager,
        *,
        segment_max_bytes: int = LOG_SEGMENT_MAX_BYTES,
        segment_max_seconds: float = LOG_SEGMENT_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._directory = directory
        self._stamp = stamp
        self._retention = retention
        self._segment_max_bytes = segment_max_bytes
        self._segment_max_seconds = segment_max_seconds
        self._clock = clock
        self._part = 0
        self._file: TextIO | None = None
        self._path: Path | None = None
        self._size = 0
        self._started_at = 0.0
        self._open_next_segment()

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("pose log is closed")
        return self._path

    def write(self, payload: str) -> None:
        record = payload + "\n"
        record_size = len(record.encode("utf-8"))
        elapsed = self._clock() - self._started_at
        if self._size > 0 and (
            self._size + record_size > self._segment_max_bytes
            or elapsed >= self._segment_max_seconds
        ):
            self._rotate()

        if self._file is None:
            raise RuntimeError("pose log is closed")
        self._file.write(record)
        self._size += record_size
        self._retention.note_size(self.path, self._size)

    def close(self) -> None:
        if self._file is None or self._path is None:
            return
        path = self._path
        self._file.close()
        self._file = None
        self._path = None
        self._retention.close_active(path)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _rotate(self) -> None:
        self.close()
        self._open_next_segment()

    def _open_next_segment(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._part += 1
        path = self._directory / f"pose_{self._stamp}_part{self._part:04d}.jsonl"
        self._file = path.open("x", encoding="utf-8", buffering=1)
        self._path = path
        self._size = 0
        self._started_at = self._clock()
        self._retention.register_active(path)


class AsyncPoseLog:
    """Serialize and write pose records without blocking the RTC event loop."""

    _STOP = object()

    def __init__(
        self,
        directory: Path,
        stamp: str,
        retention: LogRetentionManager,
        *,
        queue_frames: int = POSE_LOG_QUEUE_FRAMES,
    ) -> None:
        if queue_frames < 1:
            raise ValueError("pose log queue must contain at least one frame")
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue(queue_frames)
        self._dropped = 0
        self._state_lock = threading.Lock()
        self._closed = False
        self._log = RotatingPoseLog(directory, stamp, retention)
        self._thread = threading.Thread(
            target=self._run,
            name=f"pose-log-{stamp}",
            daemon=False,
        )
        try:
            self._thread.start()
        except BaseException:
            self._closed = True
            self._log.close()
            raise

    @property
    def path(self) -> Path:
        return self._log.path

    @property
    def dropped(self) -> int:
        with self._state_lock:
            return self._dropped

    def submit(self, frame: dict[str, Any]) -> None:
        """Enqueue without waiting; evict the oldest log-only frame on overflow."""
        with self._state_lock:
            if self._closed:
                return
        try:
            self._queue.put_nowait(frame)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._queue.task_done()
        with self._state_lock:
            self._dropped += 1
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            with self._state_lock:
                self._dropped += 1

    def close(self) -> None:
        """Request an ordered background shutdown without joining the caller."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        while True:
            try:
                self._queue.put_nowait(self._STOP)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    continue
                self._queue.task_done()
                with self._state_lock:
                    self._dropped += 1

    def wait_closed(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("pose log writer did not stop")

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is self._STOP:
                        return
                    payload = json.dumps(
                        item, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    )
                    self._log.write(payload)
                finally:
                    self._queue.task_done()
        except Exception:
            logger.exception("Pose log writer failed")
        finally:
            self._log.close()
