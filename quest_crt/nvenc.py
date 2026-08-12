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

# OBSERVED (2026-08-12, delay matrix: preset/rc/tune/zerolatency all give the
# same shape), not a hardware conclusion: in the current PyAV/FFmpeg
# h264_nvenc integration, input N's packet is emitted when input N+2 is
# submitted — an observed packet-output delay of ~2 frames (~66 ms at 30 fps,
# ~33 ms at 60 fps). Root cause not yet proven to be NVENC hardware itself.
# It is bounded and constant (no backlog accumulation), is recorded in the
# Mode A/B latency comparison (docs §6), and outputs are matched back to
# their input pts via a FIFO in NvEncH264Encoder.encode().
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
    keyframes: int = 0  # IDR count == encoder rebuild count (docs §4)
    encode_ms_avg: float = 0.0  # (A) one encode() call, incl. copies/swscale
    encode_ms_p95: float = 0.0
    # (B) frame_to_packet_ms: input frame submission -> its encoded packet
    # actually available to the RTP sender (FIFO-attributed, docs §4). This is
    # the metric teleoperation cares about, and where the observed ~2-frame
    # packet-output delay shows up.
    frame_to_packet_ms_avg: float = 0.0
    frame_to_packet_ms_p95: float = 0.0
    copies_rgb_to_av: int = 0  # numpy RGBA -> AVFrame (docs §6 copy ledger)
    copies_swscale: int = 0  # resize + yuv420p conversion (docs §6)
    pli_count: int = 0  # PLI/FIR requests received via the bridge
    last_pli_wall_ns: int = 0  # monotonic ns of the last PLI/FIR
    last_idr_wall_ns: int = 0  # monotonic ns when the first IDR packet was emitted
    pli_to_idr_ms_last: float = -1.0  # last measured PLI -> first IDR packet
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
        self._f2p_times: list[float] = []
        # Input FIFO for the delayed output: (pts_90k, submit_wall_ns). The
        # packet that comes out now belongs to the entry at the front — this
        # is what keeps the delayed packet traceable to its ORIGINAL capture
        # frame (not the currently-latest one).
        self._pending_pts: list[tuple[int, int]] = []
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
            self._telemetry.pli_count += 1
            self._telemetry.last_pli_wall_ns = time.perf_counter_ns()

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
        IDR, so a requested IDR rebuilds the context. Measured rebuild cost
        on this driver/GPU: context open alone ~425 ms (close+open 360-470
        ms) — a PLI therefore stalls the stream ~0.5 s (PLI -> first IDR
        packet). Recorded as a real fact with real impact; only occurs on
        the (rare) PLI/FIR event; not yet optimized (no native NVENC SDK
        switch for this). Freshness-safe: in-flight frames are dropped
        rather than queued."""
        with self._lock:
            if not self._idr_requested:
                return
            self._idr_requested = False
            self._pending_pts.clear()
        self._close()
        self._open()
        with self._lock:
            # keyframes == encoder rebuild count (every rebuild's first frame
            # is an IDR; see docs §4).
            self._telemetry.keyframes += 1

    def flush(self) -> list[av.Packet]:
        """Drain remaining buffered packets (tests / shutdown only)."""
        packets = self._ctx.encode(None)
        now_ns = time.perf_counter_ns()
        for packet in packets:
            if self._pending_pts:
                pts, submit_ns = self._pending_pts.pop(0)
                packet.pts = pts
                self._note_packet(now_ns, submit_ns, packet)
            packet.time_base = VIDEO_TIME_BASE
        return packets

    @staticmethod
    def _rolling_stats(times: list[float]) -> tuple[float, float]:
        """(avg, p95) over the last <=300 samples, truncating the list."""
        if len(times) > 300:
            del times[: len(times) - 300]
        avg = sum(times) / len(times)
        p95 = sorted(times)[int(len(times) * 0.95) - 1]
        return avg, p95

    def _note_packet(self, now_ns: int, submit_ns: int, packet: av.Packet) -> None:
        """Attribute an emitted packet: (B) frame_to_packet delay + IDR stamps."""
        with self._lock:
            self._f2p_times.append((now_ns - submit_ns) / 1e6)
            self._telemetry.frame_to_packet_ms_avg, self._telemetry.frame_to_packet_ms_p95 = (
                self._rolling_stats(self._f2p_times)
            )
            if packet.is_keyframe:
                # Guard: only measure PLI->IDR when this IDR responds to a
                # PLI newer than the previous IDR — otherwise mixing a stale
                # IDR stamp with a fresh PLI stamp yields garbage (measured:
                # 109 s across connections).
                if self._telemetry.last_pli_wall_ns > self._telemetry.last_idr_wall_ns:
                    self._telemetry.pli_to_idr_ms_last = (
                        now_ns - self._telemetry.last_pli_wall_ns
                    ) / 1e6
                self._telemetry.last_idr_wall_ns = now_ns

    def encode(self, rgba: Any, pts_90k: int) -> list[av.Packet]:
        """Encode one RGBA numpy frame -> list of av.Packets (usually one).

        Actual data path (docs §6 copy ledger; hardware encode validated,
        GPU / low-copy capture-to-encoder path NOT yet optimized):

          ZED sl.Mat (CPU memory, sl.MEM.CPU)
            -> numpy view (zero-copy, no copy)
            -> AVFrame from_ndarray (CPU copy: numpy RGBA -> AVFrame)
            -> swscale (CPU: resize [Mode B] + RGB -> yuv420p)
            -> NVENC (GPU upload happens inside the encoder)

        The emitted packet's pts is FIFO-attributed to the ORIGINAL capture
        frame's pts_90k (output arrives delayed by NVENC_OUTPUT_DELAY_FRAMES),
        so a delayed packet stays traceable to its source capture timestamp.
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

        # submit time = the moment the frame is handed to the encoder.
        submit_ns = time.perf_counter_ns()
        packets = self._ctx.encode(frame)
        # Output arrives delayed by NVENC_OUTPUT_DELAY_FRAMES frames; the
        # packet that comes out now belongs to the input at the FIFO front
        # (pts + submission wall time). Freshness cap: never grow unbounded.
        self._pending_pts.append((pts_90k, submit_ns))
        if len(self._pending_pts) > NVENC_OUTPUT_DELAY_FRAMES + 2:
            del self._pending_pts[: len(self._pending_pts) - NVENC_OUTPUT_DELAY_FRAMES]
        now_ns = time.perf_counter_ns()
        for packet in packets:
            if self._pending_pts:
                pts, prev_submit_ns = self._pending_pts.pop(0)
                packet.pts = pts
                self._note_packet(now_ns, prev_submit_ns, packet)
            packet.time_base = VIDEO_TIME_BASE

        encode_ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._telemetry.frames_encoded += 1
            self._encode_times.append(encode_ms)
            self._telemetry.encode_ms_avg, self._telemetry.encode_ms_p95 = (
                self._rolling_stats(self._encode_times)
            )
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
