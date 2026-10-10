"""Fixed 804-byte QCRT v5 transport for complete Quest pose frames."""

from __future__ import annotations

import math
import struct
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from quest_xr_bridge.joint_radii import decode_joint_radii, encode_joint_radii
from quest_xr_bridge.pose import PoseFrame

MAGIC = b"QCRT"
BINARY_VERSION = 5
_HEADER = struct.Struct("<4sBBHIdd16s")
_PAYLOAD = struct.Struct("<190f")
PACKET_SIZE = _HEADER.size + _PAYLOAD.size
_ALLOWED_FLAGS = 0x7F


def encode_pose_packet(frame: Mapping[str, Any]) -> bytes:
    """Validate and encode pose v5, including 42 optional joint radii."""
    pose = PoseFrame.model_validate(frame)
    hands = (pose.hands.left, pose.hands.right)
    elbows = (pose.elbows.left, pose.elbows.right)
    shoulders = (pose.shoulders.left, pose.shoulders.right)
    flags = sum(
        int(item.tracked) << bit
        for bit, item in enumerate((*hands, *elbows, *shoulders, pose.head))
    )
    values: list[float] = []
    for hand in hands:
        for point in hand.points:
            values.extend(_encode_vector(point, 3))
    for hand in hands:
        values.extend(_encode_vector(hand.wrist_orientation, 4))
    for joint in (*elbows, *shoulders):
        values.extend(_encode_vector(joint.position, 3))
    values.extend(
        _encode_vector((pose.head.yaw_deg, pose.head.pitch_deg) if pose.head.tracked else None, 2)
    )
    for hand in hands:
        values.extend(encode_joint_radii(hand.radii))
    try:
        payload = _PAYLOAD.pack(*values)
        for i, hand in enumerate(hands):
            if hand.wrist_orientation is not None and not any(
                struct.unpack_from("<4f", payload, (126 + i * 4) * 4)
            ):
                raise ValueError("wrist orientation must remain non-zero in float32")
        return (
            _HEADER.pack(
                MAGIC,
                BINARY_VERSION,
                flags,
                0,
                pose.seq,
                pose.timestamp_ms,
                pose.capture_epoch_ms,
                pose.session_id.bytes,
            )
            + payload
        )
    except (OverflowError, struct.error) as exc:
        raise ValueError("pose values must fit the QCRT binary fields") from exc


def decode_pose_packet(packet: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode and validate one QCRT v5 frame; NaN represents unavailable data."""
    view = memoryview(packet)
    if view.nbytes != PACKET_SIZE or not view.c_contiguous:
        raise ValueError(f"binary pose packet must be exactly {PACKET_SIZE} bytes")
    magic, version, flags, reserved, seq, timestamp_ms, capture_epoch_ms, session_bytes = (
        _HEADER.unpack_from(view)
    )
    if magic != MAGIC:
        raise ValueError("invalid binary pose magic")
    if version != BINARY_VERSION:
        raise ValueError(f"unsupported binary pose version {version}")
    if reserved != 0:
        raise ValueError("binary pose reserved bits must be zero")
    if flags & ~_ALLOWED_FLAGS:
        raise ValueError("binary pose contains unknown tracking flags")

    values = _PAYLOAD.unpack_from(view, _HEADER.size)
    offset = 0

    def take_vector(size: int) -> list[float] | None:
        nonlocal offset
        vector = values[offset : offset + size]
        offset += size
        if all(math.isnan(value) for value in vector):
            return None
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("binary pose vectors must be finite or entirely NaN")
        return list(vector)

    points = [[take_vector(3) for _ in range(21)] for _ in range(2)]
    orientations = [take_vector(4) for _ in range(2)]
    elbows = [take_vector(3) for _ in range(2)]
    shoulders = [take_vector(3) for _ in range(2)]
    head = take_vector(2)
    radii = [decode_joint_radii(values[offset + i * 21 : offset + (i + 1) * 21]) for i in range(2)]
    frame = {
        "type": "pose",
        "version": 5,
        "session_id": uuid.UUID(bytes=session_bytes),
        "seq": seq,
        "timestamp_ms": timestamp_ms,
        "capture_epoch_ms": capture_epoch_ms,
        "reference_space": "spine-upper-scapula",
        "units": "meters",
        "hands": {
            side: {
                "tracked": bool(flags & (1 << i)),
                "points": points[i],
                "wrist_orientation": orientations[i],
                "radii": radii[i],
            }
            for i, side in enumerate(("left", "right"))
        },
        "elbows": {
            side: {"tracked": bool(flags & (1 << (i + 2))), "position": elbows[i]}
            for i, side in enumerate(("left", "right"))
        },
        "shoulders": {
            side: {"tracked": bool(flags & (1 << (i + 4))), "position": shoulders[i]}
            for i, side in enumerate(("left", "right"))
        },
        "head": {
            "tracked": bool(flags & (1 << 6)),
            "yaw_deg": None if head is None else head[0],
            "pitch_deg": None if head is None else head[1],
        },
    }
    return PoseFrame.model_validate(frame).model_dump(mode="json")


def _encode_vector(vector: Sequence[float] | None, size: int) -> list[float]:
    return [math.nan] * size if vector is None else list(vector)
