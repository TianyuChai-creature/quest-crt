"""StablePoseStream: fixed-rate sensing snapshots, decoupled from any consumer engine.

Produces StreamEnvelope ticks from a latest-only pose hub. Transports (WSS/UDP)
are adapters; this module only owns cadence, quality, and fan-out with drop-old
backpressure.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

Quality = Literal["ok", "held", "stale", "lost"]

PoseSnapshotFn = Callable[[], tuple[int, dict[str, Any] | None, float | None]]
BuildOutputFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class StreamEnvelope:
    """One fixed-rate sensing snapshot for downstream consumers."""

    stream_seq: int
    pose_generation: int
    pose_seq: int | None
    t_stream_mono_ms: float
    capture_age_ms: float | None
    ingress_transport: str
    quality: Quality
    pose: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "stream_envelope",
            "stream_seq": self.stream_seq,
            "pose_generation": self.pose_generation,
            "pose_seq": self.pose_seq,
            "t_stream_mono_ms": self.t_stream_mono_ms,
            "capture_age_ms": self.capture_age_ms,
            "ingress_transport": self.ingress_transport,
            "quality": self.quality,
            "pose": self.pose,
        }


class StreamBus:
    """Fan-out with per-subscriber capacity-1 queues (drop intermediate ticks)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue[StreamEnvelope]] = []

    def subscribe(self) -> queue.Queue[StreamEnvelope]:
        q: queue.Queue[StreamEnvelope] = queue.Queue(maxsize=1)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[StreamEnvelope]) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, envelope: StreamEnvelope) -> int:
        """Push latest envelope to all subscribers; replace pending if full.

        Returns number of subscribers that received (or retained) this tick.
        """
        with self._lock:
            targets = list(self._subs)
        delivered = 0
        for q in targets:
            try:
                q.put_nowait(envelope)
                delivered += 1
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(envelope)
                    delivered += 1
                except queue.Full:
                    pass
        return delivered


class StreamClock:
    """Fixed-Hz clock that snapshots the pose hub into StreamEnvelope ticks."""

    def __init__(
        self,
        *,
        snapshot_pose: PoseSnapshotFn,
        build_output: BuildOutputFn,
        bus: StreamBus | None = None,
        hz: float = 72.0,
        hold_ms: float = 100.0,
        lost_ms: float = 500.0,
        idle_hz: float = 5.0,
    ) -> None:
        if hz <= 0:
            raise ValueError(f"hz must be positive, got {hz}")
        if hold_ms < 0 or lost_ms < hold_ms:
            raise ValueError("require 0 <= hold_ms <= lost_ms")
        self._snapshot_pose = snapshot_pose
        self._build_output = build_output
        self.bus = bus if bus is not None else StreamBus()
        self.hz = float(hz)
        self.hold_ms = float(hold_ms)
        self.lost_ms = float(lost_ms)
        self.idle_hz = max(float(idle_hz), 0.5)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stream_seq = 0
        self._last_pose_generation = -1
        self._last_envelope: StreamEnvelope | None = None
        self._ticks = 0
        self._quality_counts: dict[Quality, int] = {
            "ok": 0,
            "held": 0,
            "stale": 0,
            "lost": 0,
        }
        self._interval_ticks = 0
        self._interval_started = time.monotonic()
        self._last_out_fps = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="stable-pose-stream", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def describe(self) -> dict[str, Any]:
        with self._lock:
            total = max(self._ticks, 1)
            last = self._last_envelope
            return {
                "stream_hz": self.hz,
                "hold_ms": self.hold_ms,
                "lost_ms": self.lost_ms,
                "subscribers": self.bus.subscriber_count(),
                "stream_seq": self._stream_seq,
                "out_fps": self._last_out_fps,
                "ticks": self._ticks,
                "quality_ratio": {
                    k: self._quality_counts[k] / total for k in self._quality_counts
                },
                "last_quality": last.quality if last else None,
                "last_capture_age_ms": last.capture_age_ms if last else None,
                "last_ingress_transport": last.ingress_transport if last else None,
            }

    def build_envelope(self, now: float | None = None) -> StreamEnvelope:
        """Pure snapshot → envelope (also used by tests without starting the thread)."""
        t = time.monotonic() if now is None else float(now)
        generation, frame, published_at = self._snapshot_pose()
        t_ms = t * 1000.0
        if frame is None or published_at is None:
            return self._make_envelope(
                generation=0,
                pose_seq=None,
                t_ms=t_ms,
                age_ms=None,
                transport="none",
                quality="lost",
                pose=None,
                gen_for_track=generation,
            )

        age_ms = max(0.0, (t - published_at) * 1000.0)
        transport = str(frame.get("ingress_transport") or "unknown")
        pose_seq = frame.get("seq")
        pose_seq_i = int(pose_seq) if isinstance(pose_seq, (int, float)) else None

        if age_ms > self.lost_ms:
            quality: Quality = "lost"
            pose_out: dict[str, Any] | None = None
        else:
            try:
                pose_out = self._build_output(frame)
            except Exception:
                pose_out = None
                quality = "lost"
            else:
                if age_ms <= self.hold_ms:
                    quality = "ok" if generation != self._last_pose_generation else "held"
                else:
                    quality = "stale"

        return self._make_envelope(
            generation=generation,
            pose_seq=pose_seq_i,
            t_ms=t_ms,
            age_ms=age_ms,
            transport=transport,
            quality=quality,
            pose=pose_out,
            gen_for_track=generation,
        )

    def _make_envelope(
        self,
        *,
        generation: int,
        pose_seq: int | None,
        t_ms: float,
        age_ms: float | None,
        transport: str,
        quality: Quality,
        pose: dict[str, Any] | None,
        gen_for_track: int,
    ) -> StreamEnvelope:
        with self._lock:
            self._stream_seq += 1
            seq = self._stream_seq
            self._last_pose_generation = gen_for_track
            env = StreamEnvelope(
                stream_seq=seq,
                pose_generation=generation,
                pose_seq=pose_seq,
                t_stream_mono_ms=t_ms,
                capture_age_ms=age_ms,
                ingress_transport=transport,
                quality=quality,
                pose=pose,
            )
            self._last_envelope = env
            self._ticks += 1
            self._quality_counts[quality] += 1
            self._interval_ticks += 1
            now = time.monotonic()
            elapsed = now - self._interval_started
            if elapsed >= 1.0:
                self._last_out_fps = self._interval_ticks / elapsed
                self._interval_ticks = 0
                self._interval_started = now
        return env

    def _period(self) -> float:
        if self.bus.subscriber_count() <= 0:
            return 1.0 / self.idle_hz
        return 1.0 / self.hz

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_tick:
                self._stop.wait(next_tick - now)
                if self._stop.is_set():
                    break
            period = self._period()
            next_tick = max(time.monotonic(), next_tick) + period
            # Still tick when no subscribers so describe() stays warm, but skip fan-out cost
            # of build when idle? Building is cheap enough; skip publish if no subs.
            if self.bus.subscriber_count() <= 0:
                continue
            envelope = self.build_envelope()
            self.bus.publish(envelope)
