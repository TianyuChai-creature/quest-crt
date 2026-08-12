from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import queue
import socket
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TextIO

import uvicorn
from aiortc import RTCPeerConnection, RTCSessionDescription
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from quest_crt.coordinates import (
    COORDINATE_PRESETS,
    DEFAULT_COORDINATE_PRESET,
    AxisTransform,
    remap_axes,
    to_hts_wrist_relative_frame,
    transform_pose_frame,
)
from quest_crt.binary_protocol import decode_pose_packet
from quest_crt.stable_stream import StreamClock
from quest_crt.stream_protocol import encode_stream_envelope
from quest_crt.stream_udp import UdpStreamPublisher
from quest_crt.telemetry import IngressTelemetry, StatusReporter, build_health_report
from quest_crt.transport_session import (
    ChannelLease,
    TransportSessionError,
    TransportSessionManager,
    validate_transport_session_id,
)
from quest_crt.video_app import VIDEO_PORT, build_video_app

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "quest_crt" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
VIEWER_HTML = STATIC_DIR / "viewer.html"
CERT_DIR = ROOT / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
LOG_DIR = ROOT / "logs"

HOST = os.environ.get("POSE_HOST", "0.0.0.0")
PORT = int(os.environ.get("POSE_PORT", "8000"))
OUTPUT_PORT = int(os.environ.get("OUTPUT_PORT", "8001"))
POSE_LOG_ENABLED = os.environ.get("POSE_LOG_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VIEWER_SOURCE_STALE_AFTER = 0.25
POSE_LOG_QUEUE_FRAMES = int(os.environ.get("POSE_LOG_QUEUE_FRAMES", "2048"))
EVENT_LOOP_LAG_INTERVAL = 0.01
EVENT_LOOP_LAG_WARN_AFTER = 0.05
LOG_SEGMENT_MAX_BYTES = 256 * 1024 * 1024
LOG_SEGMENT_MAX_SECONDS = 15 * 60
LOG_RETENTION_MAX_BYTES = 5 * 1024 * 1024 * 1024
# StablePoseStream (sensing main channel; decoupled from Viewer event delivery)
STREAM_HZ = float(os.environ.get("STREAM_HZ", "72"))
STREAM_HOLD_MS = float(os.environ.get("STREAM_HOLD_MS", "100"))
STREAM_LOST_MS = float(os.environ.get("STREAM_LOST_MS", "500"))
# /ws/stream wire format: binary (default) or json
STREAM_WS_FORMAT = os.environ.get("STREAM_WS_FORMAT", "binary").strip().lower()
if STREAM_WS_FORMAT not in ("binary", "json"):
    STREAM_WS_FORMAT = "binary"
STREAM_UDP_ENABLED = os.environ.get("STREAM_UDP", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STREAM_UDP_HOST = os.environ.get("STREAM_UDP_HOST", "127.0.0.1")
STREAM_UDP_PORT = int(os.environ.get("STREAM_UDP_PORT", "9100"))

PosePoint = tuple[float, float, float]
PoseQuaternion = tuple[float, float, float, float]


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
                print(f"Unable to remove old pose log {path}: {exc}", flush=True)
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

    def __enter__(self) -> RotatingPoseLog:
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
        self._thread.start()

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

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is self._STOP:
                        return
                    payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    self._log.write(payload)
                finally:
                    self._queue.task_done()
        except Exception as exc:
            print(f"Pose log writer failed: {exc}", flush=True)
        finally:
            self._log.close()


log_retention = LogRetentionManager(LOG_DIR, LOG_RETENTION_MAX_BYTES)


class PoseHand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked: bool
    points: list[PosePoint | None] = Field(min_length=21, max_length=21)
    wrist_orientation: PoseQuaternion | None

    @model_validator(mode="after")
    def tracked_requires_all_pose_data(self) -> PoseHand:
        if self.tracked and any(point is None for point in self.points):
            raise ValueError("tracked hand must contain all 21 points")
        if self.tracked and self.wrist_orientation is None:
            raise ValueError("tracked hand must contain a wrist orientation")
        if (self.points[0] is None) != (self.wrist_orientation is None):
            raise ValueError("wrist point and wrist orientation availability must match")
        if self.wrist_orientation is not None and not any(
            component != 0 for component in self.wrist_orientation
        ):
            raise ValueError("wrist orientation must have non-zero norm")
        return self


class PoseJoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked: bool
    position: PosePoint | None

    @model_validator(mode="after")
    def tracked_matches_position(self) -> PoseJoint:
        if self.tracked != (self.position is not None):
            raise ValueError("tracked must match position availability")
        return self


class PoseHands(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: PoseHand
    right: PoseHand


class PoseJoints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: PoseJoint
    right: PoseJoint


# Backward-compatible alias (older stream-only tree used PoseElbows).
PoseElbows = PoseJoints


class PoseFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pose"]
    version: Literal[2, 3, 4]
    session_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    timestamp_ms: float = Field(ge=0)
    capture_epoch_ms: float | None = Field(default=None, ge=0)
    reference_space: Literal["local-floor", "spine-upper-scapula"]
    units: Literal["meters"]
    hands: PoseHands
    elbows: PoseJoints
    shoulders: PoseJoints | None = None

    @model_validator(mode="after")
    def fields_match_protocol_version(self) -> PoseFrame:
        if self.version >= 3 and self.shoulders is None:
            raise ValueError(f"pose v{self.version} must contain shoulders")
        if self.version == 2 and self.shoulders is not None:
            raise ValueError("pose v2 must not contain shoulders")
        expected_reference_space = (
            "spine-upper-scapula" if self.version == 4 else "local-floor"
        )
        if self.reference_space != expected_reference_space:
            raise ValueError(
                f"pose v{self.version} must use reference_space {expected_reference_space!r}"
            )
        return self


class WebRTCOffer(BaseModel):
    """Browser SDP offer used to establish the pose data channel."""

    model_config = ConfigDict(extra="forbid")

    sdp: str = Field(min_length=1)
    type: Literal["offer"]
    transport_session_id: str = Field(min_length=1, max_length=64)


class PoseDisconnectRequest(BaseModel):
    """Diagnostic lever for Phase 2 device acceptance (see
    docs/phase2-quest-acceptance.md): close the pose WebRTC peers of one
    transport session so the page's pose reconnect path is exercised
    without touching the video channel."""

    model_config = ConfigDict(extra="forbid")

    transport_session_id: str = Field(min_length=1, max_length=64)


class PoseSourceBusyError(RuntimeError):
    """Raised when another Quest already owns the single ingress slot."""


class ActivePoseSource:
    """Allow exactly one Quest WebRTC or WSS ingress connection at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: object | None = None
        self._transport: str | None = None
        self._client_name: str | None = None
        self._connected_at: float | None = None

    def acquire(self, owner: object, transport: str, client_name: str) -> bool:
        with self._lock:
            if self._owner is not None:
                return False
            self._owner = owner
            self._transport = transport
            self._client_name = client_name
            self._connected_at = time.monotonic()
            return True

    def release(self, owner: object) -> None:
        with self._lock:
            if self._owner is not owner:
                return
            self._owner = None
            self._transport = None
            self._client_name = None
            self._connected_at = None

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._owner is not None,
                "transport": self._transport,
                "client": self._client_name,
                "connected_for_ms": (
                    max(0.0, (time.monotonic() - self._connected_at) * 1000)
                    if self._connected_at is not None
                    else None
                ),
            }


active_pose_source = ActivePoseSource()
transport_sessions = TransportSessionManager()
video_app = build_video_app(transport_sessions)


class PoseStreamProcessor:
    """Shared validation, metrics, logging, and publication for WSS/WebRTC."""

    def __init__(
        self,
        transport: str,
        client_name: str,
        transport_session_id: str | None = None,
        lease: ChannelLease | None = None,
    ) -> None:
        self._transport = transport
        self._client_name = client_name
        self._transport_session_id = transport_session_id
        self._lease = lease
        if not active_pose_source.acquire(self, transport, client_name):
            active = active_pose_source.describe()
            self._release_lease()
            raise PoseSourceBusyError(
                f"active Quest is {active['client']} via {active['transport']}"
            )

        self._pose_log: AsyncPoseLog | None = None
        try:
            if POSE_LOG_ENABLED:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                self._pose_log = AsyncPoseLog(LOG_DIR, stamp, log_retention)
        except BaseException:
            active_pose_source.release(self)
            self._release_lease()
            raise

        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._received = 0
        self._missing = 0
        self._interval_received = 0
        self._interval_started = time.monotonic()
        self._minimum_clock_delta_ms: float | None = None
        self._condition = threading.Condition()
        self._pending: tuple[
            str | bytes, float, float, tuple[bytes, int] | None
        ] | None = None
        self._ingress_dropped = 0
        self._closed = False

        print(f"Quest connected: {client_name} via {transport}", flush=True)
        if transport == "wss":
            print(
                "WARNING: ingress is WSS fallback — prefer WebRTC for low-latency sensing",
                flush=True,
            )
        print(
            f"Pose log: {self._pose_log.path if self._pose_log is not None else 'disabled'}",
            flush=True,
        )
        self._worker = threading.Thread(
            target=self._run,
            name=f"pose-process-{transport}-{client_name}",
            daemon=False,
        )
        try:
            self._worker.start()
        except BaseException:
            if self._pose_log is not None:
                self._pose_log.close()
            active_pose_source.release(self)
            self._release_lease()
            raise

    def process(
        self,
        message: str | bytes,
        server_received_epoch_ms: float,
        server_received_monotonic_ms: float,
    ) -> None:
        """Publish work to a single latest-value slot without blocking ingress."""
        order = self._binary_message_order(message)
        with self._condition:
            if self._closed:
                return
            if self._pending is not None:
                self._ingress_dropped += 1
                pending_order = self._pending[3]
                if (
                    order is not None
                    and pending_order is not None
                    and order[0] == pending_order[0]
                    and order[1] <= pending_order[1]
                ):
                    return
            self._pending = (
                message,
                server_received_epoch_ms,
                server_received_monotonic_ms,
                order,
            )
            self._condition.notify()

    @staticmethod
    def _binary_message_order(message: str | bytes) -> tuple[bytes, int] | None:
        if isinstance(message, str) or len(message) < 44 or message[:4] != b"QCRT":
            return None
        session_id = bytes(message[28:44])
        seq = int.from_bytes(message[8:12], "little")
        return session_id, seq

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._pending is None:
                        return
                    message, epoch_ms, monotonic_ms, _ = self._pending
                    self._pending = None
                self._process(message, epoch_ms, monotonic_ms)
        finally:
            if self._pose_log is not None:
                self._pose_log.close()
            active_pose_source.release(self)
            self._release_lease()
            ingress_telemetry.clear()
            print(
                f"Quest disconnected: {self._client_name} via {self._transport}",
                flush=True,
            )

    def _process(
        self,
        message: str | bytes,
        server_received_epoch_ms: float,
        server_received_monotonic_ms: float,
    ) -> None:
        """Validate, publish and enqueue logging outside the RTC event loop."""
        try:
            if isinstance(message, str):
                frame = PoseFrame.model_validate_json(message)
            else:
                frame = PoseFrame.model_validate(decode_pose_packet(message))
        except (ValidationError, ValueError) as exc:
            print(
                f"Invalid pose frame via {self._transport} from {self._client_name}: {exc}",
                flush=True,
            )
            return

        if self._transport_session_id is not None:
            transport_sessions.touch(self._transport_session_id)

        if frame.session_id != self._session_id:
            self._session_id = frame.session_id
            self._last_seq = None
            self._received = 0
            self._missing = 0
            self._interval_received = 0
            self._interval_started = time.monotonic()
            self._minimum_clock_delta_ms = None

        if self._last_seq is not None:
            if frame.seq <= self._last_seq:
                return
            self._missing += max(0, frame.seq - self._last_seq - 1)
        self._last_seq = frame.seq
        self._received += 1
        self._interval_received += 1

        frame_data = frame.model_dump(mode="json")
        if frame_data.get("shoulders") is None:
            frame_data.pop("shoulders", None)
        frame_data["ingress_transport"] = self._transport
        frame_data["server_received_epoch_ms"] = server_received_epoch_ms
        frame_data["server_received_monotonic_ms"] = server_received_monotonic_ms
        if frame.capture_epoch_ms is not None:
            clock_delta_ms = server_received_epoch_ms - frame.capture_epoch_ms
            self._minimum_clock_delta_ms = (
                clock_delta_ms
                if self._minimum_clock_delta_ms is None
                else min(self._minimum_clock_delta_ms, clock_delta_ms)
            )
            frame_data["estimated_transport_latency_ms"] = clock_delta_ms
            frame_data["relative_transport_delay_ms"] = (
                clock_delta_ms - self._minimum_clock_delta_ms
            )
        else:
            frame_data["estimated_transport_latency_ms"] = None
            frame_data["relative_transport_delay_ms"] = None

        if self._pose_log is not None:
            self._pose_log.submit(frame_data)
        latest_pose.publish(frame_data)

        now = time.monotonic()
        elapsed = now - self._interval_started
        if elapsed >= 1:
            fps = self._interval_received / elapsed
            expected = self._received + self._missing
            loss = 100 * self._missing / expected if expected else 0.0
            log_drop = self._pose_log.dropped if self._pose_log else 0
            ingress_telemetry.record(
                fps=fps,
                seq=frame.seq,
                transport=self._transport,
                ingress_drop=self._ingress_dropped,
                log_drop=log_drop,
                effective_loss_pct=loss,
            )
            # Detailed ingress line retained; unified [status] line is 1 Hz elsewhere.
            print(
                f"fps={fps:5.1f} | seq={frame.seq} | effective_loss={loss:5.2f}% | "
                f"transport={self._transport} | "
                f"ingress_drop={self._ingress_dropped} | "
                f"log_drop={log_drop}",
                flush=True,
            )
            self._interval_received = 0
            self._interval_started = now

    def _release_lease(self) -> None:
        """Release the transport-session lease, if held. Idempotent."""
        if self._lease is not None:
            transport_sessions.end_channel(self._lease)
            self._lease = None

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify()

    def wait_closed(self, timeout: float | None = None) -> None:
        self._worker.join(timeout)


class CoordinateTransformRequest(BaseModel):
    """Select either a named preset or a custom signed-axis permutation."""

    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    axes: list[str] | None = Field(default=None, min_length=3, max_length=3)
    name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def exactly_one_transform_source(self) -> CoordinateTransformRequest:
        if (self.preset is None) == (self.axes is None):
            raise ValueError("provide exactly one of preset or axes")
        return self


class RelayUpdateNotifier:
    """Wake Viewer event loops when the relay's latest state changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_subscription_id = 0
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}

    def subscribe(self) -> tuple[int, asyncio.Event]:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._lock:
            self._next_subscription_id += 1
            subscription_id = self._next_subscription_id
            self._subscribers[subscription_id] = (loop, event)
        return subscription_id, event

    def unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def notify(self) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.items())

        stale_subscriptions: list[int] = []
        for subscription_id, (loop, event) in subscribers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                stale_subscriptions.append(subscription_id)

        if stale_subscriptions:
            with self._lock:
                for subscription_id in stale_subscriptions:
                    self._subscribers.pop(subscription_id, None)


class LatestPose:
    """Thread-safe single-frame relay between the two Uvicorn event loops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._frame: dict[str, Any] | None = None
        self._published_at: float | None = None

    def publish(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._generation += 1
            self._frame = frame
            self._published_at = time.monotonic()
        relay_update_notifier.notify()

    def snapshot(self) -> tuple[int, dict[str, Any] | None, float | None]:
        with self._lock:
            return self._generation, self._frame, self._published_at


class CoordinateTransformState:
    """Thread-safe runtime coordinate convention for Viewer output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._name = DEFAULT_COORDINATE_PRESET
        self._transform = COORDINATE_PRESETS[self._name]

    def configure(self, name: str, transform: AxisTransform) -> None:
        with self._lock:
            self._generation += 1
            self._name = name
            self._transform = transform
        relay_update_notifier.notify()

    def snapshot(self) -> tuple[int, str, AxisTransform]:
        with self._lock:
            return self._generation, self._name, self._transform

    def describe(self) -> dict[str, Any]:
        generation, name, transform = self.snapshot()
        return {
            "generation": generation,
            "name": name,
            # Pose v4 ingress is spine-upper body frame; presets remap that basis.
            "source": "spine-upper-scapula",
            "axes": list(transform.axes),
            "matrix": [list(row) for row in transform.matrix],
            "determinant": transform.determinant,
            "changes_handedness": transform.changes_handedness,
            "presets": {
                preset_name: list(preset.axes) for preset_name, preset in COORDINATE_PRESETS.items()
            },
        }


relay_update_notifier = RelayUpdateNotifier()
latest_pose = LatestPose()
coordinate_transform_state = CoordinateTransformState()


def _stream_build_output(frame: dict[str, Any]) -> dict[str, Any]:
    _, transform_name, transform = coordinate_transform_state.snapshot()
    return make_output_pose(frame, transform_name, transform)


stream_clock = StreamClock(
    snapshot_pose=latest_pose.snapshot,
    build_output=_stream_build_output,
    hz=STREAM_HZ,
    hold_ms=STREAM_HOLD_MS,
    lost_ms=STREAM_LOST_MS,
)
ingress_telemetry = IngressTelemetry()


def _compose_health() -> dict[str, Any]:
    report = build_health_report(
        latest_pose_snapshot=latest_pose.snapshot(),
        active_source=active_pose_source.describe(),
        event_loop_lag=event_loop_lag.describe(),
        stable_stream=stream_clock.describe(),
        ingress=ingress_telemetry.describe(),
        pose_log_enabled=POSE_LOG_ENABLED,
        lag_warn_ms=EVENT_LOOP_LAG_WARN_AFTER * 1000,
        pose_age_warn_ms=VIEWER_SOURCE_STALE_AFTER * 1000,
    )
    report["transport_sessions"] = transport_sessions.describe()
    report["video"] = {
        "port": VIDEO_PORT,
        "registry": video_app.registry.describe(),
    }
    report["stable_stream"] = {
        **report["stable_stream"],
        "ws_format_default": STREAM_WS_FORMAT,
        "ws_path": "/ws/stream",
        "udp": (
            udp_stream_publisher.describe()
            if udp_stream_publisher is not None
            else {"enabled": False}
        ),
    }
    return report


status_reporter = StatusReporter(_compose_health, interval_s=1.0)
udp_stream_publisher: UdpStreamPublisher | None = None


class EventLoopLagMonitor:
    """Measure scheduling stalls on the main pose-ingress event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent_ms: deque[float] = deque(maxlen=2048)
        self._maximum_ms = 0.0
        self._warnings = 0

    def record(self, lag_seconds: float) -> None:
        lag_ms = max(0.0, lag_seconds * 1000)
        with self._lock:
            self._recent_ms.append(lag_ms)
            self._maximum_ms = max(self._maximum_ms, lag_ms)
            if lag_seconds >= EVENT_LOOP_LAG_WARN_AFTER:
                self._warnings += 1

    def describe(self) -> dict[str, float | int]:
        with self._lock:
            recent = sorted(self._recent_ms)
            p99_index = max(0, min(len(recent) - 1, int(len(recent) * 0.99)))
            p99 = recent[p99_index] if recent else 0.0
            warn_ms = EVENT_LOOP_LAG_WARN_AFTER * 1000
            return {
                "recent_p99_ms": p99,
                "maximum_ms": self._maximum_ms,
                "stalls_over_50ms": self._warnings,
                "warn_threshold_ms": warn_ms,
            }


event_loop_lag = EventLoopLagMonitor()


async def monitor_event_loop_lag() -> None:
    loop = asyncio.get_running_loop()
    expected = loop.time() + EVENT_LOOP_LAG_INTERVAL
    while True:
        await asyncio.sleep(max(0.0, expected - loop.time()))
        now = loop.time()
        lag = now - expected
        event_loop_lag.record(lag)
        if lag >= EVENT_LOOP_LAG_WARN_AFTER:
            print(f"Pose event-loop stall: {lag * 1000:.1f} ms", flush=True)
        expected += EVENT_LOOP_LAG_INTERVAL
        if expected <= now:
            expected = now + EVENT_LOOP_LAG_INTERVAL


def make_output_pose(
    frame: dict[str, Any], transform_name: str, transform: AxisTransform
) -> dict[str, Any]:
    """HTS wrist-relative hands + world shoulder/elbow/wrist for dual consumers.

    - real-Teleop (``/ws``): needs shoulders/elbows/wrist in body-consistent frame
    - DIME (``/ws/stream``): uses wrist-relative 21 landmarks from the same pose
    """
    output = transform_pose_frame(frame, transform)
    output = to_hts_wrist_relative_frame(output)
    output["coordinate_transform"] = {
        "name": transform_name,
        "source": frame.get("reference_space", "spine-upper-scapula"),
        "axes": list(transform.axes),
        "matrix": [list(row) for row in transform.matrix],
        "determinant": transform.determinant,
        "changes_handedness": transform.changes_handedness,
    }
    return output


def get_lan_ip() -> str:
    """Return the LAN address normally used to reach this PC."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return "127.0.0.1"


def certificate_contains_ip(cert_path: Path, lan_ip: str) -> bool:
    if not cert_path.is_file():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return ipaddress.ip_address(lan_ip) in san.get_values_for_type(x509.IPAddress)
    except (ValueError, x509.ExtensionNotFound):
        return False


def ensure_certificate(lan_ip: str) -> None:
    """Create a development certificate whose SAN includes the current LAN IP."""

    if KEY_FILE.is_file() and certificate_contains_ip(CERT_FILE, lan_ip):
        return

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "quest-crt.local")])
    now = datetime.now(timezone.utc)
    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName("quest-crt.local"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(lan_ip)),
    ]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.chmod(0o600)


webrtc_peer_connections: set[RTCPeerConnection] = set()
webrtc_processors: dict[RTCPeerConnection, PoseStreamProcessor] = {}
# tsid per peer — lets the diagnostic pose-disconnect endpoint target one
# transport session without touching the video channel.
webrtc_tsids: dict[RTCPeerConnection, str] = {}


async def close_webrtc_peer(peer: RTCPeerConnection) -> None:
    """Close and forget one WebRTC peer and its pose processor."""
    if peer not in webrtc_peer_connections:
        return
    webrtc_peer_connections.discard(peer)
    webrtc_tsids.pop(peer, None)
    processor = webrtc_processors.pop(peer, None)
    if processor is not None:
        processor.close()
    if peer.connectionState != "closed":
        await peer.close()


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    lag_task = asyncio.create_task(monitor_event_loop_lag())
    try:
        yield
    finally:
        lag_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lag_task
        await asyncio.gather(
            *(close_webrtc_peer(peer) for peer in list(webrtc_peer_connections)),
            return_exceptions=True,
        )


app = FastAPI(title="Quest CRT", docs_url=None, redoc_url=None, lifespan=app_lifespan)
viewer_app = FastAPI(title="Quest CRT Viewer", docs_url=None, redoc_url=None)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        INDEX_HTML,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return _compose_health()


@app.post("/api/webrtc/offer")
async def webrtc_offer(offer: WebRTCOffer, request: Request) -> dict[str, str]:
    """Answer a browser offer for an unordered, unreliable pose data channel."""
    try:
        validate_transport_session_id(offer.transport_session_id)
    except TransportSessionError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid transport_session_id: {exc}",
        ) from exc
    active = active_pose_source.describe()
    if active["active"]:
        raise HTTPException(
            status_code=409,
            detail=f"another Quest is already active: {active['client']} via {active['transport']}",
        )
    peer = RTCPeerConnection()
    webrtc_peer_connections.add(peer)
    webrtc_tsids[peer] = offer.transport_session_id
    client = request.client
    client_name = f"{client.host}:{client.port}" if client else "unknown"

    @peer.on("datachannel")
    def on_datachannel(channel: Any) -> None:
        if channel.label != "pose":
            channel.close()
            return

        if peer in webrtc_processors:
            print(f"Rejected additional WebRTC pose channel from {client_name}", flush=True)
            channel.close()
            return
        try:
            lease = transport_sessions.begin_channel(
                offer.transport_session_id, "pose", client_name
            )
            processor = PoseStreamProcessor(
                "webrtc",
                client_name,
                transport_session_id=offer.transport_session_id,
                lease=lease,
            )
        except PoseSourceBusyError as exc:
            print(f"Rejected Quest via WebRTC from {client_name}: {exc}", flush=True)
            channel.close()
            asyncio.create_task(close_webrtc_peer(peer))
            return
        except TransportSessionError as exc:
            print(f"Rejected Quest via WebRTC from {client_name}: {exc}", flush=True)
            channel.close()
            asyncio.create_task(close_webrtc_peer(peer))
            return
        webrtc_processors[peer] = processor
        print(
            f"WebRTC pose channel: ordered={channel.ordered} "
            f"maxRetransmits={channel.maxRetransmits} "
            f"maxPacketLifeTime={channel.maxPacketLifeTime}",
            flush=True,
        )

        @channel.on("message")
        def on_message(message: str | bytes) -> None:
            processor.process(
                message,
                time.time_ns() / 1_000_000,
                time.monotonic_ns() / 1_000_000,
            )

        @channel.on("close")
        def on_close() -> None:
            if webrtc_processors.get(peer) is processor:
                webrtc_processors.pop(peer, None)
            processor.close()
            asyncio.create_task(close_webrtc_peer(peer))

    @peer.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        if peer.connectionState in {"failed", "closed", "disconnected"}:
            await close_webrtc_peer(peer)

    try:
        await peer.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
    except Exception as exc:
        await close_webrtc_peer(peer)
        raise HTTPException(status_code=400, detail=f"WebRTC negotiation failed: {exc}") from exc

    local_description = peer.localDescription
    if local_description is None:
        await close_webrtc_peer(peer)
        raise HTTPException(status_code=500, detail="WebRTC answer was not created")
    return {"sdp": local_description.sdp, "type": local_description.type}


@app.post("/api/webrtc/pose/disconnect")
async def pose_disconnect(request: PoseDisconnectRequest) -> dict[str, int]:
    """Close the pose WebRTC peers of one transport session.

    The page's connectionstatechange handler sees the drop and reconnects
    on its own (same tsid, new lease generation) — this is the pose-only
    reconnect lever for the Phase 2 device acceptance.
    """
    try:
        validate_transport_session_id(request.transport_session_id)
    except TransportSessionError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid transport_session_id: {exc}",
        ) from exc
    targets = [
        peer for peer, tsid in webrtc_tsids.items() if tsid == request.transport_session_id
    ]
    for peer in targets:
        await close_webrtc_peer(peer)
    return {"closed": len(targets)}


@viewer_app.get("/")
async def viewer_index() -> FileResponse:
    return FileResponse(
        VIEWER_HTML,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@viewer_app.get("/health")
async def viewer_health() -> dict[str, Any]:
    # Same sensing health surface on 8001 for stream consumers.
    return _compose_health()


@app.get("/api/coordinate-transform")
@viewer_app.get("/api/coordinate-transform")
async def get_coordinate_transform() -> dict[str, Any]:
    return coordinate_transform_state.describe()


@app.put("/api/coordinate-transform")
@viewer_app.put("/api/coordinate-transform")
async def set_coordinate_transform(request: CoordinateTransformRequest) -> dict[str, Any]:
    if request.preset is not None:
        preset_name = request.preset.lower().strip()
        transform = COORDINATE_PRESETS.get(preset_name)
        if transform is None:
            raise HTTPException(
                status_code=422,
                detail=f"unknown preset; choose one of {', '.join(COORDINATE_PRESETS)}",
            )
        name = preset_name
    else:
        try:
            transform = remap_axes(request.axes or ())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        name = request.name or "custom"

    coordinate_transform_state.configure(name, transform)
    return coordinate_transform_state.describe()


@viewer_app.websocket("/ws/stream")
async def stable_stream_websocket(websocket: WebSocket) -> None:
    """Fixed-rate sensing stream (StablePoseStream). Engine-agnostic.

    Default wire format is versioned **binary** (magic QSTR). Override with
    query ``?format=json`` or env ``STREAM_WS_FORMAT=json``.
    Per-subscriber queue capacity is 1 (latest-only backpressure).
    """
    await websocket.accept()
    client = websocket.client
    client_name = f"{client.host}:{client.port}" if client else "unknown"
    fmt = (websocket.query_params.get("format") or STREAM_WS_FORMAT).strip().lower()
    if fmt not in ("binary", "json"):
        fmt = "binary"
    sub_q = stream_clock.bus.subscribe()
    print(
        f"Stable stream connected: {client_name} format={fmt} "
        f"(hz={STREAM_HZ:g}, hold_ms={STREAM_HOLD_MS:g}, lost_ms={STREAM_LOST_MS:g})",
        flush=True,
    )
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                envelope = await loop.run_in_executor(
                    None, lambda: sub_q.get(timeout=1.0)
                )
            except queue.Empty:
                if websocket.client_state.name == "DISCONNECTED":
                    break
                continue
            if fmt == "json":
                await websocket.send_text(
                    json.dumps(
                        envelope.to_dict(), ensure_ascii=False, separators=(",", ":")
                    )
                )
            else:
                await websocket.send_bytes(encode_stream_envelope(envelope))
    except WebSocketDisconnect as exc:
        print(
            f"Stable stream disconnected: {client_name} "
            f"(code={exc.code}, reason={exc.reason!r})",
            flush=True,
        )
    finally:
        stream_clock.bus.unsubscribe(sub_q)


@viewer_app.websocket("/ws")
async def viewer_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    subscription_id, update_event = relay_update_notifier.subscribe()
    client = websocket.client
    client_name = f"{client.host}:{client.port}" if client else "unknown"
    interval_sent = 0
    interval_started: float | None = None
    last_sent_at: float | None = None
    last_output_seq: int | None = None
    last_pose_generation = -1
    last_transform_generation = -1

    print(f"Viewer output connected: {client_name}", flush=True)
    update_event.set()
    disconnect_task = asyncio.create_task(websocket.receive())
    update_task: asyncio.Task[bool] | None = None

    try:
        while True:
            update_task = asyncio.create_task(update_event.wait())
            done, _ = await asyncio.wait(
                {disconnect_task, update_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                message = disconnect_task.result()
                if message["type"] == "websocket.disconnect":
                    print(
                        f"Viewer output disconnected: {client_name} "
                        f"(code={message.get('code')}, reason={message.get('reason', '')!r})",
                        flush=True,
                    )
                    break
                disconnect_task = asyncio.create_task(websocket.receive())
            if update_task not in done:
                update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await update_task
                continue

            update_event.clear()

            pose_generation, frame, published_at = latest_pose.snapshot()
            transform_generation, transform_name, transform = coordinate_transform_state.snapshot()
            if frame is None or published_at is None:
                continue

            if time.monotonic() - published_at > VIEWER_SOURCE_STALE_AFTER:
                continue
            if (
                pose_generation == last_pose_generation
                and transform_generation == last_transform_generation
            ):
                continue

            output = make_output_pose(frame, transform_name, transform)
            await websocket.send_text(
                json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            )
            last_pose_generation = pose_generation
            last_transform_generation = transform_generation
            last_output_seq = int(output["seq"])

            now = time.monotonic()
            if last_sent_at is None or now - last_sent_at > VIEWER_SOURCE_STALE_AFTER:
                interval_sent = 0
                interval_started = now
            last_sent_at = now
            interval_sent += 1
            if interval_started is not None and now - interval_started >= 1:
                output_fps = interval_sent / (now - interval_started)
                print(
                    f"out_fps={output_fps:5.1f} | seq={last_output_seq} | "
                    f"delivery=latest-only | transform={transform_name}",
                    flush=True,
                )
                interval_sent = 0
                interval_started = now
    except WebSocketDisconnect as exc:
        print(
            f"Viewer output disconnected: {client_name} (code={exc.code}, reason={exc.reason!r})",
            flush=True,
        )
    finally:
        disconnect_task.cancel()
        if update_task is not None:
            update_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await disconnect_task
        relay_update_notifier.unsubscribe(subscription_id)


@app.websocket("/ws")
async def pose_websocket(websocket: WebSocket) -> None:
    client = websocket.client
    client_name = f"{client.host}:{client.port}" if client else "unknown"
    transport_session_id = (websocket.query_params.get("tsid") or "").strip()
    try:
        validate_transport_session_id(transport_session_id)
        lease = transport_sessions.begin_channel(
            transport_session_id, "pose", client_name
        )
    except TransportSessionError as exc:
        print(
            f"Rejected Quest via WSS from {client_name}: invalid tsid ({exc})",
            flush=True,
        )
        await websocket.close(code=1008, reason="transport_session_id required")
        return
    try:
        processor = PoseStreamProcessor(
            "wss",
            client_name,
            transport_session_id=transport_session_id,
            lease=lease,
        )
    except PoseSourceBusyError as exc:
        print(f"Rejected Quest via WSS from {client_name}: {exc}", flush=True)
        await websocket.close(code=1008, reason="another Quest is already active")
        return

    try:
        await websocket.accept()
    except BaseException:
        processor.close()
        raise

    try:
        while True:
            raw = await websocket.receive_text()
            processor.process(
                raw,
                time.time_ns() / 1_000_000,
                time.monotonic_ns() / 1_000_000,
            )
    except WebSocketDisconnect:
        pass
    finally:
        processor.close()


def main() -> None:
    global udp_stream_publisher
    lan_ip = get_lan_ip()
    ensure_certificate(lan_ip)
    stream_clock.start()
    status_reporter.start()
    if STREAM_UDP_ENABLED:
        udp_stream_publisher = UdpStreamPublisher(
            stream_clock.bus,
            host=STREAM_UDP_HOST,
            port=STREAM_UDP_PORT,
        )
        udp_stream_publisher.start()
    print(f"Quest page: https://{lan_ip}:{PORT}/", flush=True)
    print(f"3D viewer:  https://{lan_ip}:{OUTPUT_PORT}/", flush=True)
    print(
        f"Stable stream WSS: wss://{lan_ip}:{OUTPUT_PORT}/ws/stream "
        f"(default_format={STREAM_WS_FORMAT}, hz={STREAM_HZ:g}, "
        f"hold_ms={STREAM_HOLD_MS:g}, lost_ms={STREAM_LOST_MS:g})",
        flush=True,
    )
    print(
        f"  binary magic=QSTR v1; JSON via ?format=json or STREAM_WS_FORMAT=json",
        flush=True,
    )
    if STREAM_UDP_ENABLED:
        print(
            f"Stable stream UDP: {STREAM_UDP_HOST}:{STREAM_UDP_PORT} "
            f"(same QSTR binary envelope)",
            flush=True,
        )
    else:
        print(
            "Stable stream UDP: off (set STREAM_UDP=1 STREAM_UDP_PORT=9100 to enable)",
            flush=True,
        )
    print(f"Health:     https://{lan_ip}:{PORT}/health", flush=True)
    print(
        f"Video signaling: https://{lan_ip}:{VIDEO_PORT}/ "
        f"(POST /api/webrtc/video/offer)",
        flush=True,
    )
    print(f"Certificate: {CERT_FILE}", flush=True)
    if POSE_LOG_ENABLED:
        print(
            "NOTE: pose JSONL logging is ON. For low-latency sensing/streaming, "
            "prefer POSE_LOG_ENABLED=0 (async log still competes for disk/CPU).",
            flush=True,
        )
    else:
        print("Pose JSONL logging: disabled (POSE_LOG_ENABLED=0)", flush=True)

    viewer_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": viewer_app,
            "host": HOST,
            "port": OUTPUT_PORT,
            "ssl_certfile": str(CERT_FILE),
            "ssl_keyfile": str(KEY_FILE),
        },
        name="quest-crt-viewer",
        daemon=True,
    )
    viewer_thread.start()

    video_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": video_app,
            "host": HOST,
            "port": VIDEO_PORT,
            "ssl_certfile": str(CERT_FILE),
            "ssl_keyfile": str(KEY_FILE),
        },
        name="quest-crt-video",
        daemon=True,
    )
    video_thread.start()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        ssl_certfile=str(CERT_FILE),
        ssl_keyfile=str(KEY_FILE),
    )


if __name__ == "__main__":
    main()
