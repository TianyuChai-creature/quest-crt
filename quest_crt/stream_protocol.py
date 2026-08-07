"""Binary StablePoseStream wire format (versioned, engine-agnostic).

Packet layout (little-endian), magic ``QSTR``:

Header (48 bytes)::

    4s magic      b"QSTR"
    B  version   1
    B  quality   0=ok 1=held 2=stale 3=lost
    B  flags     bit0 pose_present, bit1 elbows_present
    B  reserved  0
    I  stream_seq
    I  pose_generation
    i  pose_seq          (-1 if none)
    d  t_stream_mono_ms
    d  capture_age_ms    (NaN if none)
    B  ingress           0=none 1=webrtc 2=wss 3=unknown
    15s transform_name   ASCII, NUL-padded

Optional pose body (when flags.pose_present):
  140 floats: left hand (70) + right hand (70)
    each hand: wrist_pos[3] + wrist_ori[4] + landmarks[21*3]
    missing vectors are all-NaN; tracked bit is in quality path via pose JSON
    tracked flags packed in 1 byte after floats? — use first of each hand:
  We store tracked as: non-NaN wrist_pos implies tracked for consumers;
  also bit in hand header: 2 bytes hand_flags (L tracked, R tracked) before floats.

  Layout refinement:
    B left_tracked
    B right_tracked
    140f hand payload

Optional elbows (when flags.elbows_present): 6 floats Lxyz Rxyz.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from quest_crt.stable_stream import Quality, StreamEnvelope

STREAM_MAGIC = b"QSTR"
STREAM_VERSION = 1

_QUALITY_TO_U8: dict[str, int] = {"ok": 0, "held": 1, "stale": 2, "lost": 3}
_U8_TO_QUALITY: dict[int, Quality] = {v: k for k, v in _QUALITY_TO_U8.items()}  # type: ignore[misc]

_INGRESS_TO_U8 = {"none": 0, "webrtc": 1, "wss": 2, "unknown": 3}
_U8_TO_INGRESS = {v: k for k, v in _INGRESS_TO_U8.items()}

_FLAG_POSE = 1 << 0
_FLAG_ELBOWS = 1 << 1

# magic, ver, quality, flags, reserved, stream_seq, pose_gen, pose_seq, t_ms, age_ms, ingress, name
_HEADER = struct.Struct("<4sBBBBIIiddB15s")
# 4+4+4+4+4+8+8+1+15 = 52
assert _HEADER.size == 52

_HAND_FLOATS = 70  # 3 + 4 + 63
_POSE_FLOATS = _HAND_FLOATS * 2
_ELBOW_FLOATS = 6


def _pack_name(name: str | None) -> bytes:
    raw = (name or "").encode("ascii", errors="replace")[:15]
    return raw.ljust(15, b"\x00")


def _unpack_name(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _vec(values: Any, size: int) -> list[float]:
    if values is None:
        return [math.nan] * size
    if len(values) != size:
        raise ValueError(f"expected {size} components, got {len(values)}")
    out = [float(v) for v in values]
    if any(not math.isfinite(v) for v in out):
        raise ValueError("vector components must be finite")
    return out


def _maybe_vec(values: Any, size: int) -> list[float]:
    if values is None:
        return [math.nan] * size
    return _vec(values, size)


def _hand_floats(hand: Mapping[str, Any] | None) -> tuple[bool, list[float]]:
    if not hand:
        return False, [math.nan] * _HAND_FLOATS
    tracked = bool(hand.get("tracked"))
    wrist = hand.get("wrist") or {}
    floats: list[float] = []
    floats.extend(_maybe_vec(wrist.get("position"), 3))
    floats.extend(_maybe_vec(wrist.get("orientation"), 4))
    landmarks = hand.get("landmarks") or [None] * 21
    if len(landmarks) != 21:
        raise ValueError("hand landmarks must have length 21")
    for lm in landmarks:
        floats.extend(_maybe_vec(lm, 3))
    if len(floats) != _HAND_FLOATS:
        raise ValueError("internal hand float count mismatch")
    return tracked, floats


def _decode_hand(tracked: bool, floats: Sequence[float]) -> dict[str, Any]:
    pos = floats[0:3]
    ori = floats[3:7]
    lms = [list(floats[7 + i * 3 : 10 + i * 3]) for i in range(21)]

    def clean(v: list[float]) -> list[float] | None:
        if all(math.isnan(x) for x in v):
            return None
        if any(not math.isfinite(x) for x in v if not math.isnan(x)):
            raise ValueError("invalid hand vector")
        if any(math.isnan(x) for x in v):
            raise ValueError("partial NaN vector")
        return [float(x) for x in v]

    return {
        "tracked": tracked,
        "wrist": {
            "position": clean(pos),
            "orientation": clean(ori),
        },
        "landmarks": [clean(lm) for lm in lms],
    }


def encode_stream_envelope(envelope: StreamEnvelope | Mapping[str, Any]) -> bytes:
    """Encode one StreamEnvelope to versioned binary."""
    if isinstance(envelope, StreamEnvelope):
        d = envelope.to_dict()
    else:
        d = dict(envelope)

    quality = str(d.get("quality") or "lost")
    if quality not in _QUALITY_TO_U8:
        raise ValueError(f"unknown quality {quality!r}")

    transport = str(d.get("ingress_transport") or "none")
    ingress_u8 = _INGRESS_TO_U8.get(transport, 3)

    pose = d.get("pose")
    flags = 0
    body = bytearray()
    transform_name = ""

    if pose is not None and quality != "lost":
        flags |= _FLAG_POSE
        hands = pose.get("hands") or {}
        lt, lf = _hand_floats(hands.get("left"))
        rt, rf = _hand_floats(hands.get("right"))
        body.append(1 if lt else 0)
        body.append(1 if rt else 0)
        body.extend(struct.pack(f"<{_POSE_FLOATS}f", *lf, *rf))
        elbows = pose.get("elbows")
        if isinstance(elbows, Mapping):
            flags |= _FLAG_ELBOWS
            ef: list[float] = []
            for side in ("left", "right"):
                el = elbows.get(side) or {}
                ef.extend(_maybe_vec(el.get("position"), 3))
            body.extend(struct.pack(f"<{_ELBOW_FLOATS}f", *ef))
        ct = pose.get("coordinate_transform") or {}
        transform_name = str(ct.get("name") or "")

    age = d.get("capture_age_ms")
    age_f = float("nan") if age is None else float(age)
    pose_seq = d.get("pose_seq")
    pose_seq_i = -1 if pose_seq is None else int(pose_seq)

    header = _HEADER.pack(
        STREAM_MAGIC,
        STREAM_VERSION,
        _QUALITY_TO_U8[quality],
        flags,
        0,
        int(d.get("stream_seq") or 0) & 0xFFFFFFFF,
        int(d.get("pose_generation") or 0) & 0xFFFFFFFF,
        pose_seq_i,
        float(d.get("t_stream_mono_ms") or 0.0),
        age_f,
        ingress_u8,
        _pack_name(transform_name),
    )
    return header + bytes(body)


def decode_stream_envelope(packet: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode one binary envelope into StreamEnvelope-compatible dict."""
    view = memoryview(packet)
    if len(view) < _HEADER.size:
        raise ValueError(f"stream packet too short: {len(view)}")

    (
        magic,
        version,
        quality_u8,
        flags,
        reserved,
        stream_seq,
        pose_generation,
        pose_seq_i,
        t_ms,
        age_f,
        ingress_u8,
        name_raw,
    ) = _HEADER.unpack_from(view)

    if magic != STREAM_MAGIC:
        raise ValueError("invalid stream magic")
    if version != STREAM_VERSION:
        raise ValueError(f"unsupported stream version {version}")
    if reserved != 0:
        raise ValueError("stream reserved must be 0")
    if quality_u8 not in _U8_TO_QUALITY:
        raise ValueError(f"unknown quality code {quality_u8}")

    quality = _U8_TO_QUALITY[quality_u8]
    transport = _U8_TO_INGRESS.get(int(ingress_u8), "unknown")
    age_ms = None if math.isnan(age_f) else float(age_f)
    pose_seq = None if pose_seq_i < 0 else int(pose_seq_i)
    transform_name = _unpack_name(bytes(name_raw))

    offset = _HEADER.size
    pose: dict[str, Any] | None = None

    if flags & _FLAG_POSE:
        if len(view) < offset + 2 + _POSE_FLOATS * 4:
            raise ValueError("stream packet truncated (pose)")
        left_tracked = view[offset] != 0
        right_tracked = view[offset + 1] != 0
        offset += 2
        floats = struct.unpack_from(f"<{_POSE_FLOATS}f", view, offset)
        offset += _POSE_FLOATS * 4
        left = _decode_hand(left_tracked, floats[:_HAND_FLOATS])
        right = _decode_hand(right_tracked, floats[_HAND_FLOATS:])
        pose = {
            "type": "pose",
            "representation": "hts-wrist-relative",
            "seq": pose_seq,
            "hands": {"left": left, "right": right},
            "coordinate_transform": {"name": transform_name} if transform_name else None,
        }
        if flags & _FLAG_ELBOWS:
            if len(view) < offset + _ELBOW_FLOATS * 4:
                raise ValueError("stream packet truncated (elbows)")
            ef = struct.unpack_from(f"<{_ELBOW_FLOATS}f", view, offset)
            offset += _ELBOW_FLOATS * 4

            def elbow(i: int) -> dict[str, Any]:
                v = list(ef[i * 3 : i * 3 + 3])
                if all(math.isnan(x) for x in v):
                    return {"tracked": False, "position": None}
                return {"tracked": True, "position": [float(x) for x in v]}

            pose["elbows"] = {"left": elbow(0), "right": elbow(1)}

    if flags & ~(_FLAG_POSE | _FLAG_ELBOWS):
        raise ValueError("stream packet has unknown flags")

    return {
        "type": "stream_envelope",
        "stream_seq": int(stream_seq),
        "pose_generation": int(pose_generation),
        "pose_seq": pose_seq,
        "t_stream_mono_ms": float(t_ms),
        "capture_age_ms": age_ms,
        "ingress_transport": transport,
        "quality": quality,
        "pose": pose,
    }


def stream_packet_size_for(envelope: StreamEnvelope | Mapping[str, Any]) -> int:
    """Return encoded size without allocating (for tests/docs)."""
    return len(encode_stream_envelope(envelope))
