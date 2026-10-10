"""Pose ingress statistics and health, independent of video state."""

from __future__ import annotations

import threading
import time
from typing import Any


class IngressTelemetry:
    """Latest 1 Hz ingress stats published by PoseStreamProcessor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fps = 0.0
        self._seq: int | None = None
        self._transport: str | None = None
        self._ingress_drop = 0
        self._log_drop = 0
        self._effective_loss_pct = 0.0
        self._updated_at: float | None = None

    def record(
        self,
        *,
        fps: float,
        seq: int | None,
        transport: str,
        ingress_drop: int,
        log_drop: int,
        effective_loss_pct: float,
    ) -> None:
        with self._lock:
            self._fps = float(fps)
            self._seq = int(seq) if seq is not None else None
            self._transport = str(transport)
            self._ingress_drop = int(ingress_drop)
            self._log_drop = int(log_drop)
            self._effective_loss_pct = float(effective_loss_pct)
            self._updated_at = time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._fps = 0.0
            self._seq = None
            self._transport = None
            self._ingress_drop = 0
            self._log_drop = 0
            self._effective_loss_pct = 0.0
            self._updated_at = None

    def describe(self) -> dict[str, Any]:
        with self._lock:
            age = (
                max(0.0, (time.monotonic() - self._updated_at) * 1000)
                if self._updated_at is not None
                else None
            )
            stale = age is None or age > 2500.0
            return {
                "fps": self._fps if not stale else 0.0,
                "seq": self._seq,
                "transport": self._transport,
                "ingress_drop": self._ingress_drop,
                "log_drop": self._log_drop,
                "effective_loss_pct": self._effective_loss_pct,
                "stats_age_ms": age,
                "stats_stale": stale,
            }


def build_health_report(
    *,
    latest_pose_snapshot: tuple[int, dict[str, Any] | None, float | None],
    active_source: dict[str, Any],
    event_loop_lag: dict[str, Any],
    ingress: dict[str, Any],
    pose_log_enabled: bool,
    lag_warn_ms: float = 50.0,
    pose_age_warn_ms: float = 250.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Pose health does not depend on a camera or video transport."""
    current = time.monotonic() if now is None else now
    generation, frame, published_at = latest_pose_snapshot
    age_ms = max(0.0, (current - published_at) * 1000) if published_at is not None else None
    stale = bool(active_source.get("active")) and (age_ms is None or age_ms > pose_age_warn_ms)
    lag = float(event_loop_lag.get("recent_p99_ms") or 0)
    degraded = lag >= lag_warn_ms
    warnings = []
    if stale:
        warnings.append("pose source has no fresh frame")
    if degraded:
        warnings.append(f"event loop p99 lag {lag:.1f}ms exceeds {lag_warn_ms:g}ms")
    return {
        "status": "ok",
        "healthy": not stale and not degraded,
        "warnings": warnings,
        "latest_pose": {
            "generation": generation,
            "seq": frame.get("seq") if frame else None,
            "transport": "webrtc" if frame else None,
            "age_ms": age_ms,
        },
        "ingress": ingress,
        "active_source": active_source,
        "event_loop_lag": {
            **event_loop_lag,
            "degraded": degraded,
            "warn_threshold_ms": lag_warn_ms,
        },
        "pose_log_enabled": pose_log_enabled,
    }


def format_status_line(report: dict[str, Any]) -> str:
    ingress, pose = report.get("ingress", {}), report.get("latest_pose", {})
    lag = report.get("event_loop_lag", {})
    age = pose.get("age_ms")
    age_text = f"{age:.1f}" if age is not None else "n/a"
    return (
        f"[status] pose={ingress.get('transport') or 'none'} "
        f"fps={ingress.get('fps', 0):.1f} age_ms={age_text} "
        f"in_drop={ingress.get('ingress_drop', 0)} log_drop={ingress.get('log_drop', 0)} "
        f"lag_p99_ms={lag.get('recent_p99_ms', 0):.1f}"
    )
