"""Phase 3: ZED Mini SBS capture with latest-only freshness queue.

Design: docs/phase3-zed-nvenc.md §2 (architecture), §3 (Freshness Contract),
§7 (exposure/gain telemetry). ZED capture is always 2560x720 SBS @60 — the
downstream mode (A: 30fps passthrough, B: 1920x540 resize) decides what the
encoder consumes, never the camera.

The grab loop lives in its own thread; consumers call `wait_fresh()` /
`latest()` and always get the newest capture. A slot overwrite counts as a
dropped (old) frame — no backlog is ever allowed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

try:  # ZED SDK is optional at import time (synthetic fallback path)
    import pyzed.sl as sl  # type: ignore[import-not-found]
except ImportError:
    sl = None

CAPTURE_WIDTH = 2560  # HD720 SBS: 2 x 1280
CAPTURE_HEIGHT = 720
CAPTURE_FPS = 60

# Freshness Contract: latest-only slot; an overwrite drops the old frame.
# One slot is enough (recv() grabs at most every 1/30s); two would allow the
# grab thread to run ahead by exactly one frame — keep it minimal: 1 slot +
# a monotonic "generation" so consumers can tell a new frame from a stale one.
MAX_CAPTURE_SLOTS = 1


@dataclass
class ZedFrame:
    """One captured SBS frame plus capture-side telemetry."""

    rgba: np.ndarray  # 2560x720 RGBA (U8_C4), zero-copy view into the ZED Mat
    pts_90k: int  # ZED capture timestamp, 90 kHz units (RTP timestamp basis)
    capture_ns: int  # ZED SDK IMAGE timestamp (ns)
    exposure_level: int  # 0-100 level, NOT us (see ZedTelemetry.exposure_level)
    gain: float


@dataclass
class ZedTelemetry:
    exposure_level: int = 0
    gain: float = 0.0
    capture_fps: float = 0.0
    # Mean interval between successive successful grab() returns. At N fps
    # capture this is ~1/N s (16.7 ms at 60 fps) — it is the capture cadence,
    # NOT camera latency. Real capture latency needs IMAGE timestamp vs host
    # receive timestamp (Phase 5); we keep both clocks' last values below.
    capture_interval_ms: float = 0.0
    image_timestamp_ns: int = 0  # last ZED IMAGE timestamp (camera clock)
    host_receive_timestamp_ns: int = 0  # last grab return, perf_counter clock
    frames_captured: int = 0
    frames_dropped: int = 0  # old-frame drops (slot overwrite), Freshness Contract


class ZedSbsSource(threading.Thread):
    """Latest-only SBS capture source.

    `wait_fresh(after_generation)` blocks until a capture newer than
    `after_generation` lands (used by the async track via asyncio.to_thread);
    `latest()` returns (generation, frame) immediately.
    """

    def __init__(self, fps: int = CAPTURE_FPS) -> None:
        super().__init__(daemon=True, name="zed-grab")
        self._fps = fps
        self._lock = threading.Lock()
        self._new_frame = threading.Condition(self._lock)
        self._frame: ZedFrame | None = None
        self._generation = 0
        self._telemetry = ZedTelemetry()
        self._running = threading.Event()
        self._cam = None
        self.error: str | None = None
        if sl is None:
            self.error = "pyzed not importable; ZED capture unavailable"

    @property
    def available(self) -> bool:
        return sl is not None and self._cam is not None

    def telemetry(self) -> ZedTelemetry:
        with self._lock:
            return ZedTelemetry(**vars(self._telemetry))

    def latest(self) -> tuple[int, ZedFrame] | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._generation, self._frame

    def wait_fresh(self, after_generation: int, timeout: float = 2.0) -> tuple[int, ZedFrame] | None:
        """Block until a frame with generation > after_generation exists."""
        with self._new_frame:
            while self._generation <= after_generation:
                if not self.is_alive() or not self._running.is_set():
                    return None
                if not self._new_frame.wait(timeout):
                    return None
            return self._generation, self._frame  # type: ignore[return-value]

    def start_capture(self) -> None:
        self._running.set()
        self.start()

    def stop_capture(self) -> None:
        self._running.clear()
        with self._new_frame:
            self._new_frame.notify_all()

    # -- grab loop ----------------------------------------------------------

    def run(self) -> None:
        if sl is None:
            return
        cam = sl.Camera()
        init = sl.InitParameters()
        init.camera_resolution = sl.RESOLUTION.HD720
        init.camera_fps = self._fps
        init.depth_mode = sl.DEPTH_MODE.NONE
        init.sdk_verbose = False
        status = cam.open(init)
        if status != sl.ERROR_CODE.SUCCESS:
            self.error = f"ZED open failed: {status}"
            log.error(self.error)
            return
        self._cam = cam
        runtime = sl.RuntimeParameters()
        mat = sl.Mat()
        grabbed = 0
        period_sum = 0.0
        period_samples = 0
        last_perf = time.perf_counter()
        while self._running.is_set():
            grab_start = time.perf_counter()
            err = cam.grab(runtime)
            if err != sl.ERROR_CODE.SUCCESS:
                continue  # SDK may skip frames; keep sampling
            cam.retrieve_image(mat, sl.VIEW.SIDE_BY_SIDE, sl.MEM.CPU)
            ts = cam.get_timestamp(sl.TIME_REFERENCE.IMAGE)
            ns = ts.get_nanoseconds()
            # Telemetry: exposure/gain readback every capture (cheap register
            # read). pyzed >= 5.x: VIDEO_SETTINGS (was CAMERA_SETTINGS), and
            # get_camera_settings returns (ERROR_CODE, value).
            # EXPOSURE semantics (sl/Camera.hpp VIDEO_SETTINGS): a 0-100 LEVEL
            # mapped linearly as a percentage of the max exposure value for the
            # current framerate (60fps: 100 -> 10.84 ms, 0 -> 0.171 ms). It is
            # NOT microseconds; real exposure time in us is only exposed via
            # VIDEO_SETTINGS.EXPORSURE_TIME on GMSL2 ZED-X cameras — not on
            # ZED Mini (USB). So telemetry reports the level, never "us".
            exposure_err, exposure = cam.get_camera_settings(
                sl.VIDEO_SETTINGS.EXPOSURE
            )
            gain_err, gain = cam.get_camera_settings(sl.VIDEO_SETTINGS.GAIN)
            if exposure_err != sl.ERROR_CODE.SUCCESS:
                exposure = None
            if gain_err != sl.ERROR_CODE.SUCCESS:
                gain = None
            now_perf = time.perf_counter()
            period_ms = (now_perf - last_perf) * 1000.0
            last_perf = now_perf
            grabbed += 1
            period_sum += period_ms
            period_samples += 1
            if period_samples >= 30:
                with self._lock:
                    # fps from the current 30-sample window only (grabbed is
                    # cumulative and would inflate the rate).
                    self._telemetry.capture_fps = period_samples / (
                        period_sum / 1000.0
                    )
                    # Loop cadence (== 1/fps at steady state), NOT latency.
                    self._telemetry.capture_interval_ms = period_sum / period_samples
                    self._telemetry.frames_captured = grabbed
                period_sum = period_samples = 0
            # pts in 90 kHz units, monotonic from the camera clock.
            pts_90k = ns * 90 // 1_000_000
            frame = ZedFrame(
                rgba=mat.get_data(),  # numpy view, zero-copy (CPU mat)
                pts_90k=pts_90k,
                capture_ns=ns,
                exposure_level=int(exposure) if exposure else 0,
                gain=float(gain) if gain else 0.0,
            )
            with self._new_frame:
                if self._frame is not None:
                    self._telemetry.frames_dropped += 1  # old frame dropped
                self._frame = frame
                self._generation += 1
                # Keep both clocks for the Phase 5 capture-latency measurement:
                # image (camera clock) vs host receive (perf_counter, monotonic
                # — deltas only, no cross-clock absolute comparison).
                self._telemetry.exposure_level = frame.exposure_level
                self._telemetry.gain = frame.gain
                self._telemetry.image_timestamp_ns = ns
                self._telemetry.host_receive_timestamp_ns = now_perf * 1e9
                self._new_frame.notify_all()
        cam.close()
        self._cam = None
