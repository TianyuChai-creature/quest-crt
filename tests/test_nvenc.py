"""Phase 3 tests: NVENC encoder, encoded-packet track, freshness, PLI bridge.

Design: docs/phase3-zed-nvenc.md §2/§4/§6. Hardware-dependent tests skip
gracefully when h264_nvenc is unavailable.
"""

from __future__ import annotations

import time
from fractions import Fraction

import av
import numpy as np
import pytest

from quest_crt.nvenc import (
    MODE_A,
    MODE_B,
    EncodedVideoTrack,
    NvEncH264Encoder,
    VIDEO_TIME_BASE,
)
from quest_crt.zed_source import ZedFrame, ZedTelemetry

pytestmark = pytest.mark.skipif(
    av.Codec("h264_nvenc", "w") is None,
    reason="h264_nvenc not available in this PyAV build",
)


def _nvenc_codec() -> bool:
    try:
        return av.Codec("h264_nvenc", "w") is not None
    except Exception:
        return False


def _rgba_frame(width: int = 2560, height: int = 720) -> np.ndarray:
    # Moving vertical stripe so consecutive frames differ.
    data = np.zeros((height, width, 4), dtype=np.uint8)
    data[:, ::8, 0] = 255  # red columns; motion shifts the phase
    return data


class FakeSource:
    """ZedSbsSource stand-in: produces frames at a fixed rate without a camera."""

    def __init__(self, fps: int = 60) -> None:
        self._fps = fps
        self._interval = 1.0 / fps
        self._gen = 0
        self._t = 0
        self._frame: ZedFrame | None = None
        self._deadline = 0.0
        self.error: str | None = None
        self.captured = 0

    def telemetry(self) -> ZedTelemetry:
        return ZedTelemetry()

    def wait_fresh(self, after_generation: int, timeout: float = 2.0):
        now = time.monotonic()
        if now < self._deadline:
            time.sleep(self._deadline - now)
        self._deadline = time.monotonic() + self._interval
        self._t += 1
        self._gen += 1
        self.captured += 1
        self._frame = ZedFrame(
            rgba=_rgba_frame(),
            pts_90k=self._t * 3000,  # 1/30 s at 90 kHz, or 1/60 -> 1500
            capture_ns=0,
            exposure_level=100,
            gain=1.0,
        )
        return self._gen, self._frame


def test_encoder_is_nvenc() -> None:
    enc = NvEncH264Encoder(MODE_A)
    assert enc.encoder_name == "h264_nvenc"
    assert enc.profile == "baseline"


def test_encode_packet_pts_and_timebase() -> None:
    # Structural delay: input N's packet comes out at call N+2; the pts FIFO
    # must attribute it to the right input (NVENC_OUTPUT_DELAY_FRAMES).
    enc = NvEncH264Encoder(MODE_A)
    enc.encode(_rgba_frame(), pts_90k=100)
    enc.encode(_rgba_frame(), pts_90k=200)
    packets = enc.encode(_rgba_frame(), pts_90k=300)
    assert len(packets) == 1
    assert packets[0].pts == 100
    assert packets[0].time_base == VIDEO_TIME_BASE == Fraction(1, 90000)


def test_delayed_packet_pts_traceable_to_capture() -> None:
    """A delayed packet must carry the ORIGINAL input's pts (docs §4), never
    the currently-latest frame's, and frame_to_packet_ms (B) must be measured.
    This is what keeps Phase 5 capture-to-display latency traceable."""
    enc = NvEncH264Encoder(MODE_B)
    capture_pts = [3000 * (i + 1) for i in range(6)]  # distinct capture stamps
    emitted: list[tuple[int, float]] = []  # (packet_pts, output wall time)
    wall_before = time.perf_counter()
    for pts in capture_pts:
        for packet in enc.encode(_rgba_frame(), pts_90k=pts):
            emitted.append((packet.pts, time.perf_counter()))
    emitted.extend((p.pts, time.perf_counter()) for p in enc.flush())
    wall_after = time.perf_counter()

    # Every emitted packet's pts is one of the ORIGINAL capture pts, in
    # submission order — no packet is ever bound to a newer frame.
    assert [pts for pts, _ in emitted] == capture_pts, (
        f"packet pts {[p for p, _ in emitted]} != capture pts {capture_pts}"
    )
    # And the packet was emitted after its input was submitted (delay >= 0),
    # so output wall time is a valid latency measure for its own frame.
    for pts, out_wall in emitted:
        assert out_wall >= wall_before
        assert pts > 0
    assert out_wall <= wall_after

    tel = enc.telemetry()
    assert tel.frame_to_packet_ms_avg > 0.0
    assert tel.frame_to_packet_ms_p95 >= tel.frame_to_packet_ms_avg


def test_pli_telemetry_timestamps() -> None:
    """PLI -> rebuild -> first IDR packet must be observable (docs §4):
    pli_count, last_pli_wall_ns, keyframes (=rebuilds), last_idr_wall_ns,
    pli_to_idr_ms_last."""
    enc = NvEncH264Encoder(MODE_A)
    emitted: list[av.Packet] = []
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=0))
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=3000))
    enc.request_idr()
    t_pli = time.perf_counter()
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=6000))
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=9000))
    emitted.extend(enc.flush())
    assert any(p.is_keyframe for p in emitted)

    tel = enc.telemetry()
    assert tel.pli_count == 1
    assert abs(tel.last_pli_wall_ns / 1e9 - t_pli) < 0.1
    assert tel.keyframes == 1  # one rebuild
    assert tel.last_idr_wall_ns > tel.last_pli_wall_ns
    assert 0.0 <= tel.pli_to_idr_ms_last < 2000.0  # bounded, in ms


def test_force_idr_produces_keyframe() -> None:
    enc = NvEncH264Encoder(MODE_A)
    emitted: list[av.Packet] = []
    for index in range(5):
        if index == 2:
            enc.request_idr()
        emitted.extend(enc.encode(_rgba_frame(), pts_90k=index * 3000))
    emitted.extend(enc.flush())
    keyframes = [p for p in emitted if p.is_keyframe]
    assert len(keyframes) == 1, f"expected exactly one IDR, got {len(keyframes)}"
    assert keyframes[0].pts == 6000  # the forced input's pts
    assert enc.telemetry().keyframes == 1


def test_mode_b_resize_output_1920x540() -> None:
    enc = NvEncH264Encoder(MODE_B)
    enc.encode(_rgba_frame(), pts_90k=3000)
    enc.encode(_rgba_frame(), pts_90k=6000)
    packets = enc.encode(_rgba_frame(), pts_90k=9000)
    assert packets and packets[0].is_keyframe  # first frame of the stream
    dec = av.CodecContext.create(av.Codec("h264", "r"))
    dec.pix_fmt = "yuv420p"
    frames = dec.decode(packets[0])
    assert frames and frames[0].width == 1920 and frames[0].height == 540


def test_profile_high_is_legal() -> None:
    # High is a legal NVENC profile (docs §5 efficiency candidate); assert
    # the encoder opens with it and produces packets.
    enc = NvEncH264Encoder(MODE_A, profile="high")
    assert enc.profile == "high"
    enc.encode(_rgba_frame(), pts_90k=3000)
    enc.encode(_rgba_frame(), pts_90k=6000)
    packets = enc.encode(_rgba_frame(), pts_90k=9000)
    assert packets


@pytest.mark.asyncio
async def test_track_mode_a_outputs_30fps() -> None:
    enc = NvEncH264Encoder(MODE_A)
    track = EncodedVideoTrack(FakeSource(fps=60), enc)
    t0 = time.monotonic()
    count = 0
    while time.monotonic() - t0 < 1.0:
        packet = await track.recv()
        count += 1
        assert packet.pts > 0
    assert 20 <= count <= 40, f"Mode A expected ~30 fps, got {count}"


@pytest.mark.asyncio
async def test_track_mode_b_outputs_60fps() -> None:
    enc = NvEncH264Encoder(MODE_B)
    track = EncodedVideoTrack(FakeSource(fps=60), enc)
    t0 = time.monotonic()
    count = 0
    while time.monotonic() - t0 < 1.0:
        await track.recv()
        count += 1
    assert 45 <= count <= 75, f"Mode B expected ~60 fps, got {count}"


def test_idr_bridge_wrapper_requests_encoder_idr() -> None:
    """Mirror of the video_app sender patch: wrapping _send_keyframe must
    forward PLI/FIR to the encoder so the NEXT frame is an IDR."""
    enc = NvEncH264Encoder(MODE_A)

    class FakeSender:
        def __init__(self) -> None:
            self.called = 0

        def _send_keyframe(self) -> None:  # aiortc's internal hook
            self.called += 1

    sender = FakeSender()
    orig = sender._send_keyframe

    def _bridged_send_keyframe() -> None:
        orig()
        enc.request_idr()

    sender._send_keyframe = _bridged_send_keyframe

    # two frames before the PLI, then a PLI, then frames after it
    emitted: list[av.Packet] = []
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=0))
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=3000))
    sender._send_keyframe()
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=6000))
    emitted.extend(enc.encode(_rgba_frame(), pts_90k=9000))
    emitted.extend(enc.flush())
    keyframes = [p for p in emitted if p.is_keyframe]
    assert sender.called == 1
    assert len(keyframes) == 1
    assert keyframes[0].pts == 6000  # the frame right after the PLI
    assert enc.telemetry().frames_encoded == 4
