"""One Quest source, bounded pose processing, and latest-value output."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quest_xr_bridge.binary_protocol import PACKET_SIZE, decode_pose_packet
from quest_xr_bridge.coordinates import (
    COORDINATE_PRESETS,
    DEFAULT_COORDINATE_PRESET,
    AxisTransform,
    to_hts_wrist_relative_frame,
    transform_pose_frame,
)
from quest_xr_bridge.pose_log import LOG_RETENTION_MAX_BYTES, AsyncPoseLog, LogRetentionManager
from quest_xr_bridge.telemetry import IngressTelemetry, build_health_report

logger = logging.getLogger(__name__)
SOURCE_STALE_AFTER = 0.25


class PoseSourceBusyError(RuntimeError):
    """Another Quest owns this service's ingress."""


class ActivePoseSource:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: object | None = None
        self._client: str | None = None
        self._connected_at: float | None = None

    def acquire(self, owner: object, client: str) -> bool:
        with self._lock:
            if self._owner is not None:
                return False
            self._owner, self._client = owner, client
            self._connected_at = time.monotonic()
            return True

    def release(self, owner: object) -> None:
        with self._lock:
            if self._owner is owner:
                self._owner = self._client = self._connected_at = None

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._owner is not None,
                "transport": "webrtc" if self._owner is not None else None,
                "client": self._client,
                "connected_for_ms": (
                    (time.monotonic() - self._connected_at) * 1000
                    if self._connected_at is not None
                    else None
                ),
            }


class RelayUpdateNotifier:
    """Wake async subscribers safely from the pose processing thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 0
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}

    def subscribe(self) -> tuple[int, asyncio.Event]:
        loop, event = asyncio.get_running_loop(), asyncio.Event()
        with self._lock:
            self._next_id += 1
            self._subscribers[self._next_id] = (loop, event)
            return self._next_id, event

    def unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def notify(self) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.items())
        for key, (loop, event) in subscribers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                self.unsubscribe(key)


class LatestPose:
    def __init__(self, notifier: RelayUpdateNotifier) -> None:
        self._notifier = notifier
        self._lock = threading.Lock()
        self._generation = 0
        self._frame: dict[str, Any] | None = None
        self._published_at: float | None = None

    def publish(self, frame: dict[str, Any] | None) -> None:
        with self._lock:
            self._generation += 1
            self._frame = frame
            self._published_at = time.monotonic() if frame is not None else None
        self._notifier.notify()

    def snapshot(self) -> tuple[int, dict[str, Any] | None, float | None]:
        with self._lock:
            return self._generation, self._frame, self._published_at


class CoordinateTransformState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._name = DEFAULT_COORDINATE_PRESET
        self._transform = COORDINATE_PRESETS[self._name]

    def configure(self, name: str, transform: AxisTransform) -> None:
        with self._lock:
            self._generation += 1
            self._name, self._transform = name, transform

    def snapshot(self) -> tuple[int, str, AxisTransform]:
        with self._lock:
            return self._generation, self._name, self._transform

    def describe(self) -> dict[str, Any]:
        generation, name, transform = self.snapshot()
        return {
            "generation": generation,
            "name": name,
            "source": "spine-upper-scapula",
            "axes": list(transform.axes),
            "matrix": [list(row) for row in transform.matrix],
            "determinant": transform.determinant,
            "changes_handedness": transform.changes_handedness,
            "presets": {key: list(value.axes) for key, value in COORDINATE_PRESETS.items()},
        }


def make_output_pose(frame: dict[str, Any], name: str, transform: AxisTransform) -> dict[str, Any]:
    """Body-frame joints and wrist-local landmarks for generic consumers."""
    output = to_hts_wrist_relative_frame(transform_pose_frame(frame, transform))
    output["coordinate_transform"] = {
        "name": name,
        "source": frame["reference_space"],
        "axes": list(transform.axes),
        "matrix": [list(row) for row in transform.matrix],
        "determinant": transform.determinant,
        "changes_handedness": transform.changes_handedness,
    }
    return output


class EventLoopLagMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: deque[float] = deque(maxlen=2048)
        self._maximum = 0.0
        self._warnings = 0

    def record(self, lag_seconds: float) -> None:
        lag_ms = max(0.0, lag_seconds * 1000)
        with self._lock:
            self._recent.append(lag_ms)
            self._maximum = max(self._maximum, lag_ms)
            self._warnings += lag_ms >= 50

    def describe(self) -> dict[str, Any]:
        with self._lock:
            values = sorted(self._recent)
            return {
                "recent_p99_ms": values[min(len(values) - 1, int(len(values) * 0.99))]
                if values
                else 0,
                "maximum_ms": self._maximum,
                "stalls_over_50ms": self._warnings,
            }

    async def monitor(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            target = loop.time() + 0.01
            await asyncio.sleep(0.01)
            self.record(loop.time() - target)


class PoseRuntime:
    def __init__(self, *, record_poses: bool = False, log_dir: Path | None = None) -> None:
        self.record_poses = record_poses
        self.log_dir = log_dir or Path.cwd() / "logs"
        self.retention = LogRetentionManager(self.log_dir, LOG_RETENTION_MAX_BYTES)
        self.active_source = ActivePoseSource()
        self.notifier = RelayUpdateNotifier()
        self.latest_pose = LatestPose(self.notifier)
        self.coordinates = CoordinateTransformState()
        self.ingress = IngressTelemetry()
        self.event_loop_lag = EventLoopLagMonitor()
        self.peers: set[Any] = set()
        self.peer_timeouts: dict[Any, asyncio.Task[None]] = {}
        self.processors: dict[Any, PoseStreamProcessor] = {}
        self._output_lock = threading.Lock()
        self._output_key: tuple[int, int] | None = None
        self._output_text: str | None = None

    def output_snapshot(self) -> tuple[int, str | None]:
        generation, frame, published_at = self.latest_pose.snapshot()
        if (
            frame is None
            or published_at is None
            or time.monotonic() - published_at > SOURCE_STALE_AFTER
        ):
            return generation, None
        transform_generation, name, transform = self.coordinates.snapshot()
        with self._output_lock:
            if (generation, transform_generation) != self._output_key:
                self._output_text = json.dumps(
                    make_output_pose(frame, name, transform),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                self._output_key = generation, transform_generation
            return generation, self._output_text

    def health(self) -> dict[str, Any]:
        return build_health_report(
            latest_pose_snapshot=self.latest_pose.snapshot(),
            active_source=self.active_source.describe(),
            event_loop_lag=self.event_loop_lag.describe(),
            ingress=self.ingress.describe(),
            pose_log_enabled=self.record_poses,
        )


class PoseStreamProcessor:
    """Decode outside the RTC loop; replace queued work with the newest frame."""

    def __init__(self, runtime: PoseRuntime, client_name: str) -> None:
        self.runtime, self._client_name = runtime, client_name
        if not runtime.active_source.acquire(self, client_name):
            raise PoseSourceBusyError("another Quest is already active")
        self._condition = threading.Condition()
        self._pending: tuple[bytes, float, float, tuple[bytes, int]] | None = None
        self._closed = False
        self._ingress_dropped = self._invalid = self._received = self._missing = 0
        self._last_seq: int | None = None
        self._session_id: str | None = None
        self._interval_received = 0
        self._interval_started = time.monotonic()
        self._minimum_delta: float | None = None
        self._pose_log: AsyncPoseLog | None = None
        try:
            if runtime.record_poses:
                self._pose_log = AsyncPoseLog(
                    runtime.log_dir,
                    datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f"),
                    runtime.retention,
                )
            self._worker = threading.Thread(
                target=self._run, name="quest-pose-process", daemon=False
            )
            self._worker.start()
        except BaseException:
            try:
                if self._pose_log is not None:
                    self._pose_log.close()
                    self._pose_log.wait_closed(5)
            finally:
                runtime.active_source.release(self)
            raise

    def process(self, message: str | bytes, epoch_ms: float, monotonic_ms: float) -> None:
        if (
            not isinstance(message, bytes)
            or len(message) != PACKET_SIZE
            or message[:4] != b"QCRT"
            or message[4] != 5
        ):
            self._invalid += 1
            return
        order = message[28:44], int.from_bytes(message[8:12], "little")
        with self._condition:
            if self._closed:
                return
            if self._pending is not None:
                self._ingress_dropped += 1
                pending_order = self._pending[3]
                if order[0] == pending_order[0] and order[1] <= pending_order[1]:
                    return
            self._pending = message, epoch_ms, monotonic_ms, order
            self._condition.notify()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    message, epoch_ms, monotonic_ms, _ = self._pending
                    self._pending = None
                self._process(message, epoch_ms, monotonic_ms)
        finally:
            if self._pose_log is not None:
                self._pose_log.close()
                self._pose_log.wait_closed()
            # Clear old state before allowing a replacement source to publish.
            self.runtime.latest_pose.publish(None)
            self.runtime.ingress.clear()
            self.runtime.active_source.release(self)

    def _process(self, message: bytes, epoch_ms: float, monotonic_ms: float) -> None:
        try:
            frame = decode_pose_packet(message)
        except ValueError as exc:
            self._invalid += 1
            logger.warning("Invalid Quest frame: %s", exc)
            return
        if frame["session_id"] != self._session_id:
            self._session_id = frame["session_id"]
            self._last_seq = None
            self._received = self._missing = self._interval_received = 0
            self._interval_started = time.monotonic()
            self._minimum_delta = None
        seq = frame["seq"]
        if self._last_seq is not None:
            if seq <= self._last_seq:
                return
            self._missing += seq - self._last_seq - 1
        self._last_seq = seq
        self._received += 1
        self._interval_received += 1
        delta = epoch_ms - frame["capture_epoch_ms"]
        self._minimum_delta = (
            delta if self._minimum_delta is None else min(delta, self._minimum_delta)
        )
        frame.update(
            {
                "ingress_transport": "webrtc",
                "server_received_epoch_ms": epoch_ms,
                "server_received_monotonic_ms": monotonic_ms,
                "estimated_transport_latency_ms": delta,
                "relative_transport_delay_ms": delta - self._minimum_delta,
            }
        )
        with self._condition:
            if self._closed:
                return
            if self._pose_log is not None:
                self._pose_log.submit(frame)
            self.runtime.latest_pose.publish(frame)
        now = time.monotonic()
        elapsed = now - self._interval_started
        self.runtime.ingress.record(
            fps=self._interval_received / elapsed if elapsed > 0 else 0,
            seq=seq,
            transport="webrtc",
            ingress_drop=self._ingress_dropped,
            log_drop=self._pose_log.dropped if self._pose_log else 0,
            effective_loss_pct=100 * self._missing / (self._received + self._missing),
        )
        if elapsed >= 1:
            self._interval_started, self._interval_received = now, 0

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()

    def wait_closed(self, timeout: float | None = None) -> None:
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise TimeoutError("pose processor did not stop")
