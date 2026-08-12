"""Phase 3: NVENC H.264 encoder + encoded-packet track.

Design: docs/phase3-zed-nvenc.md §2 (encoded av.Packet -> MediaStreamTrack.recv()
-> aiortc packetizer/RTP), §4 (NVENC low-latency params, PLI/FIR -> external IDR
bridge), §5 (profile from SDP, never hardcoded), §6 (copy accounting).

Contract with aiortc (verified against 1.15.0): the sender's `pack()` path
converts `packet.pts` via `packet.time_base` to the RTP timestamp at
VIDEO_TIME_BASE = 1/90000 (`codecs/h264.py:301`). So every packet we emit must
carry `pts` in 90 kHz units with `time_base = 1/90000`.

PLI/FIR: aiortc sets an internal `__force_keyframe` flag on PLI/FIR
(`rtcrtpsender.py:277-281`), which the raw-frame path honors but the packet
path ignores (`:316-323`). The bridge lives in video_app.py: it wraps
`sender._send_keyframe()` and calls `encoder.request_idr()` here, so a PLI
forces the NEXT frame to be a true IDR inside NVENC.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import av
from aiortc import MediaStreamTrack

from quest_crt.zed_source import ZedSbsSource

log = logging.getLogger(__name__)

VIDEO_TIME_BASE = Fraction(1, 90000)

# NVENC low-latency (docs §4): B=0, lookahead=0, reorder off, p1 preset,
# single-pass CBR, bounded latency. `forced_idr=1` makes forced keyframes
# (pict_type=I) true IDRs, not plain intra frames.
NVENC_OPTIONS: dict[str, str] = {
    "preset": "p1",
    "tune": "ull",  # ultra-low-latency; verified in the delay matrix below
    "zerolatency": "1",
    "rc": "cbr",
    "bf": "0",
    "lookahead": "0",
    "g": "60",  # ~2 s GOP; recovery comes from PLI -> IDR, not frequent IDRs
    "forced_idr": "1",
}

# Measured 2026-08-12 (delay matrix: preset/rc/tune/zerolatency all give the
# same shape): ffmpeg's h264_nvenc wrapper emits input N's packet when input
# N+2 is submitted — a structural 2-frame delay (~66 ms at 30 fps, ~33 ms at
# 60 fps). It is bounded and constant (no backlog accumulation), and is
# recorded in the Mode A/B latency comparison (docs §6); outputs are matched
# back to their input pts via a FIFO in NvEncH264Encoder.encode().
NVENC_OUTPUT_DELAY_FRAMES = 2

# SDP profile-level-id -> NVENC profile (docs §5). Baseline is the Phase 2
# proven contract; High is the efficiency candidate; never hardcoded defaults
# beyond the fallback below.
PROFILE_MAP = {
    "42001f": "baseline",
    "42e01f": "constrained_baseline",  # nvenc may reject; fallback + log
    "4d001f": "high",
    "64001f": "high",
}


@dataclass(frozen=True)
class VideoMode:
    """Encoder output mode. ZED capture is always 2560x720 SBS @60."""

    name: str
    out_width: int
    out_height: int
    out_fps: int
    bitrate: int


MODE_A = VideoMode("A", 2560, 720, 30, 8_000_000)  # quality: 1280x720/eye @30
MODE_B = VideoMode("B", 1920, 540, 60, 6_000_000)  # motion: 960x540/eye @60

# Macroblock check (docs §1): A = 7,200 MB/frame -> 216,000 MB/s <= L4.0;
# B = 4,080 MB/frame -> 244,800 MB/s <= L4.0 MaxMBPS 245,760.


@dataclass
class EncoderTelemetry:
    frames_encoded: int = 0
    keyframes: int = 0
    encode_ms_avg: float = 0.0
    encode_ms_p95: float = 0.0
    copies_rgb_to_av: int = 0  # numpy RGBA -> AVFrame (docs §6 copy ledger)
    copies_swscale: int = 0  # resize + yuv420p conversion (docs §6)
    profile: str = ""


class NvEncH264Encoder:
    """Thread-safe PyAV h264_nvenc encoder.

    One input frame at a time (the track encodes on the sender's pace), so
    no input queue is needed — freshness is enforced upstream by the
    latest-only capture slot.
    """

    def __init__(self, mode: VideoMode, profile: str = "baseline") -> None:
        self._mode = mode
        self._profile = profile
        self._lock = threading.Lock()
        self._idr_requested = False
        self._telemetry = EncoderTelemetry()
        self._encode_times: list[float] = []
        self._pending_pts: list[int] = []  # input pts FIFO for delayed output
        self._ctx: Any = None
        self._open()

    def _open(self) -> None:
        codec = av.Codec("h264_nvenc", "w")
        ctx = codec.create()
        ctx.width = self._mode.out_width
        ctx.height = self._mode.out_height
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = Fraction(1, self._mode.out_fps)
        ctx.framerate = self._mode.out_fps
        ctx.bit_rate = self._mode.bitrate
        # maxrate/bufsize are read-only fields on CodecContext; set them as
        # AVOptions instead (they must exist before open()).
        ctx.options["maxrate"] = str(self._mode.bitrate)
        ctx.options["bufsize"] = str(self._mode.bitrate // self._mode.out_fps)
        ctx.gop_size = 60
        for key, value in NVENC_OPTIONS.items():
            ctx.options[key] = value
        try:
            ctx.options["profile"] = self._profile
            ctx.open()
        except Exception as exc:
            if self._profile != "baseline":
                log.warning(
                    "NVENC rejected profile %r (%s); falling back to baseline",
                    self._profile,
                    exc,
                )
                self._profile = "baseline"
                ctx.options["profile"] = "baseline"
                ctx.open()
            else:
                raise
        self._ctx = ctx
        self._telemetry.profile = self._profile

    @property
    def encoder_name(self) -> str:
        return "h264_nvenc"

    @property
    def profile(self) -> str:
        return self._profile

    def telemetry(self) -> EncoderTelemetry:
        with self._lock:
            return EncoderTelemetry(**vars(self._telemetry))

    def request_idr(self) -> None:
        """PLI/FIR bridge: force the next encoded frame to be an IDR."""
        with self._lock:
            self._idr_requested = True

    def _close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None

    def _maybe_rebuild_for_idr(self) -> None:
        """PLI/FIR path (docs §4). Measured 2026-08-12: PyAV 17 has no
        ``flags`` attribute (cannot set AV_FRAME_FLAG_KEY) and h264_nvenc
        ignores both ``pict_type=PictureType.I`` and ``key_frame=True``,
        while ffmpeg CLI's ``-force_key_frames`` (which sets FLAG_KEY) does
        force an IDR. A freshly opened encoder emits its first frame as an
        IDR, so a requested IDR rebuilds the context: ~tens of ms, bounded
        to the (rare) PLI/FIR event, and freshness-safe (in-flight frames
        are dropped rather than queued)."""
        with self._lock:
            if not self._idr_requested:
                return
            self._idr_requested = False
            self._pending_pts.clear()
        self._close()
        self._open()
        with self._lock:
            self._telemetry.keyframes += 1

    def flush(self) -> list[av.Packet]:
        """Drain remaining buffered packets (tests / shutdown only)."""
        packets = self._ctx.encode(None)
        for packet in packets:
            if self._pending_pts:
                packet.pts = self._pending_pts.pop(0)
            packet.time_base = VIDEO_TIME_BASE
        return packets

    def encode(self, rgba: Any, pts_90k: int) -> list[av.Packet]:
        """Encode one RGBA numpy frame -> list of av.Packets (usually one).

        Copy ledger (docs §6, all measured):
          1. numpy RGBA -> AVFrame (from_ndarray copy)
          2. swscale: resize (Mode B) + RGB -> yuv420p
          GPU upload happens inside NVENC itself.
        """
        mode = self._mode
        t0 = time.perf_counter()

        frame = av.VideoFrame.from_ndarray(rgba, format="rgba")
        self._telemetry.copies_rgb_to_av += 1
        # One swscale pass does resize + format conversion (docs §6).
        frame = frame.reformat(
            format="yuv420p", width=mode.out_width, height=mode.out_height
        )
        self._telemetry.copies_swscale += 1
        frame.pts = pts_90k
        frame.time_base = VIDEO_TIME_BASE

        self._maybe_rebuild_for_idr()

        packets = self._ctx.encode(frame)
        # Output arrives delayed by NVENC_OUTPUT_DELAY_FRAMES frames; the
        # packet that comes out now belongs to the input whose pts is at the
        # front of the FIFO. Freshness cap: never let the FIFO grow unbounded.
        self._pending_pts.append(pts_90k)
        if len(self._pending_pts) > NVENC_OUTPUT_DELAY_FRAMES + 2:
            del self._pending_pts[: len(self._pending_pts) - NVENC_OUTPUT_DELAY_FRAMES]
        for packet in packets:
            if self._pending_pts:
                packet.pts = self._pending_pts.pop(0)
            packet.time_base = VIDEO_TIME_BASE

        encode_ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._telemetry.frames_encoded += 1
            self._encode_times.append(encode_ms)
            if len(self._encode_times) > 300:
                self._encode_times = self._encode_times[-300:]
            self._telemetry.encode_ms_avg = sum(self._encode_times) / len(
                self._encode_times
            )
            self._telemetry.encode_ms_p95 = sorted(self._encode_times)[
                int(len(self._encode_times) * 0.95) - 1
            ]
        return packets


class EncodedVideoTrack(MediaStreamTrack):
    """MediaStreamTrack whose recv() returns encoded av.Packets.

    recv() runs on the sender's pace: it gates output rate (Mode A: 30fps
    from 60fps capture, taking the latest frame at each output tick), fetches
    the freshest capture, encodes, and returns the av.Packet — so aiortc only
    ever packetizes/RTPs it. Never buffers encoded frames (Freshness Contract).
    """

    kind = "video"

    def __init__(self, source: ZedSbsSource, encoder: NvEncH264Encoder) -> None:
        super().__init__()
        self._source = source
        self._encoder = encoder
        self._last_generation = 0
        self._last_output_t = 0.0
        self._interval = 1.0 / encoder._mode.out_fps  # noqa: SLF001

    def telemetry(self) -> dict[str, Any]:
        return {
            "encoder": self._encoder.telemetry(),
            "zed": self._source.telemetry(),
        }

    async def recv(self) -> av.Packet:
        mode = self._encoder._mode  # noqa: SLF001
        while True:
            # Rate gate: Mode A outputs every 1/30 s (capture is 60fps), Mode
            # B takes every fresh capture. Sleep first, then take the LATEST
            # frame (freshness: a stale capture is never encoded).
            if mode.out_fps == 30:
                now = time.monotonic()
                delay = self._interval - (now - self._last_output_t)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_output_t = time.monotonic()

            got = await asyncio.to_thread(
                self._source.wait_fresh, self._last_generation
            )
            if got is None:
                raise RuntimeError(f"ZED capture unavailable: {self._source.error}")
            generation, frame = got
            self._last_generation = generation

            packets = await asyncio.to_thread(
                self._encoder.encode, frame.rgba, frame.pts_90k
            )
            if packets:
                return packets[0]
            # NVENC startup window: the first two inputs produce no output
            # (structural delay, NVENC_OUTPUT_DELAY_FRAMES). Loop consumes a
            # fresh capture per iteration — bounded, never accumulates.
