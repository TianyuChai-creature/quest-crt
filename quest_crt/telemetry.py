"""Ingress / stream health aggregation for quest-crt (P1 observability)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


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
    stable_stream: dict[str, Any],
    ingress: dict[str, Any],
    pose_log_enabled: bool,
    lag_warn_ms: float = 50.0,
    pose_age_warn_ms: float = 250.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Compose /health payload + human warnings (no engine-specific fields)."""
    t = time.monotonic() if now is None else float(now)
    generation, frame, published_at = latest_pose_snapshot
    transport = None
    if frame is not None:
        transport = frame.get("ingress_transport")
    pose_age_ms = (
        max(0.0, (t - published_at) * 1000) if published_at is not None else None
    )

    ingress_transport = transport or ingress.get("transport") or active_source.get(
        "transport"
    )
    ingress_degraded = ingress_transport == "wss"
    lag_p99 = float(event_loop_lag.get("recent_p99_ms") or 0.0)
    lag_max = float(event_loop_lag.get("maximum_ms") or 0.0)
    lag_degraded = lag_p99 >= lag_warn_ms or lag_max >= lag_warn_ms * 2

    quality_ratio = stable_stream.get("quality_ratio") or {}
    stream_subscribers = int(stable_stream.get("subscribers") or 0)
    stream_out_fps = float(stable_stream.get("out_fps") or 0.0)
    stream_hz = float(stable_stream.get("stream_hz") or 0.0)

    warnings: list[str] = []
    if ingress_degraded:
        warnings.append(
            "ingress_transport=wss (fallback); prefer WebRTC for low-latency sensing"
        )
    if pose_age_ms is not None and pose_age_ms > pose_age_warn_ms and active_source.get(
        "active"
    ):
        warnings.append(f"pose_age_ms={pose_age_ms:.1f} exceeds {pose_age_warn_ms:g}ms")
    if lag_degraded:
        warnings.append(
            f"event_loop_lag elevated (p99={lag_p99:.1f}ms max={lag_max:.1f}ms)"
        )
    if pose_log_enabled:
        warnings.append(
            "POSE_LOG_ENABLED=1; for low-latency streaming set POSE_LOG_ENABLED=0"
        )
    if stream_subscribers > 0 and stream_hz > 0 and stream_out_fps < stream_hz * 0.5:
        warnings.append(
            f"stable_stream out_fps={stream_out_fps:.1f} << stream_hz={stream_hz:g}"
        )

    healthy = (
        not ingress_degraded
        and not lag_degraded
        and (pose_age_ms is None or pose_age_ms <= pose_age_warn_ms or not active_source.get("active"))
    )

    return {
        "status": "ok",
        "healthy": healthy,
        "warnings": warnings,
        "latest_pose": {
            "generation": generation,
            "seq": frame.get("seq") if frame else None,
            "transport": transport,
            "age_ms": pose_age_ms,
        },
        "ingress": {
            **ingress,
            "transport": ingress_transport,
            "degraded": ingress_degraded,
            "preferred_transport": "webrtc",
        },
        "active_source": active_source,
        "event_loop_lag": {
            **event_loop_lag,
            "warn_threshold_ms": lag_warn_ms,
            "degraded": lag_degraded,
        },
        "stable_stream": {
            **stable_stream,
            "held_ratio": float(quality_ratio.get("held") or 0.0),
            "stale_ratio": float(quality_ratio.get("stale") or 0.0),
            "ok_ratio": float(quality_ratio.get("ok") or 0.0),
            "lost_ratio": float(quality_ratio.get("lost") or 0.0),
        },
        "pose_log_enabled": pose_log_enabled,
        "ingress_degraded": ingress_degraded,
    }


def format_status_line(report: dict[str, Any]) -> str:
    """One-line terminal summary for operators."""
    ingress = report.get("ingress") or {}
    pose = report.get("latest_pose") or {}
    stream = report.get("stable_stream") or {}
    lag = report.get("event_loop_lag") or {}
    warnings = report.get("warnings") or []

    transport = ingress.get("transport") or "none"
    deg = " DEGRADED" if report.get("ingress_degraded") else ""
    age = pose.get("age_ms")
    age_s = f"{age:5.1f}" if isinstance(age, (int, float)) else "  n/a"
    line = (
        f"[status] ingress={transport}{deg} "
        f"fps={float(ingress.get('fps') or 0):5.1f} "
        f"age_ms={age_s} "
        f"in_drop={int(ingress.get('ingress_drop') or 0)} "
        f"log_drop={int(ingress.get('log_drop') or 0)} | "
        f"stream_hz={float(stream.get('stream_hz') or 0):g} "
        f"out_fps={float(stream.get('out_fps') or 0):5.1f} "
        f"sub={int(stream.get('subscribers') or 0)} "
        f"q={stream.get('last_quality') or '-'} "
        f"held={float(stream.get('held_ratio') or 0):.2f} "
        f"stale={float(stream.get('stale_ratio') or 0):.2f} | "
        f"lag_p99={float(lag.get('recent_p99_ms') or 0):4.1f}ms"
    )
    if warnings:
        line += " | WARN: " + "; ".join(warnings[:2])
    return line


class StatusReporter:
    """Background 1 Hz status printer."""

    def __init__(
        self,
        build_report: Callable[[], dict[str, Any]],
        *,
        interval_s: float = 1.0,
    ) -> None:
        self._build_report = build_report
        self._interval_s = max(float(interval_s), 0.2)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="quest-crt-status", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                report = self._build_report()
                # Only print when something is alive (source or stream subs)
                active = bool((report.get("active_source") or {}).get("active"))
                subs = int((report.get("stable_stream") or {}).get("subscribers") or 0)
                if not active and subs <= 0:
                    continue
                print(format_status_line(report), flush=True)
            except Exception as exc:  # noqa: BLE001 — never kill reporter
                print(f"[status] error: {exc}", flush=True)
