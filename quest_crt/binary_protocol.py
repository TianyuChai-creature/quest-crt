"""Compact binary transport for Quest pose frames."""

from __future__ import annotations

import math
import struct
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

MAGIC = b"QCRT"
LEGACY_BINARY_VERSION = 1
SHOULDER_BINARY_VERSION = 2
BINARY_VERSION = 3
_HEADER = struct.Struct("<4sBBHIdd16s")
_LEGACY_FLOAT_COUNT = 140
_FLOAT_COUNT = 146
LEGACY_PACKET_SIZE = _HEADER.size + _LEGACY_FLOAT_COUNT * 4
PACKET_SIZE = _HEADER.size + _FLOAT_COUNT * 4

_LEFT_HAND_TRACKED = 1 << 0
_RIGHT_HAND_TRACKED = 1 << 1
_LEFT_ELBOW_TRACKED = 1 << 2
_RIGHT_ELBOW_TRACKED = 1 << 3
_LEFT_SHOULDER_TRACKED = 1 << 4
_RIGHT_SHOULDER_TRACKED = 1 << 5


def encode_pose_packet(frame: Mapping[str, Any]) -> bytes:
    """Encode one raw pose frame into the matching fixed-size wire format."""
    pose_version = int(frame["version"])
    if pose_version == 2:
        binary_version = LEGACY_BINARY_VERSION
        float_count = _LEGACY_FLOAT_COUNT
        packet_size = LEGACY_PACKET_SIZE
    elif pose_version == 3:
        binary_version = SHOULDER_BINARY_VERSION
        float_count = _FLOAT_COUNT
        packet_size = PACKET_SIZE
    elif pose_version == 4:
        binary_version = BINARY_VERSION
        float_count = _FLOAT_COUNT
        packet_size = PACKET_SIZE
    else:
        raise ValueError(f"unsupported pose version {pose_version}")
    expected_reference_space = "spine-upper-scapula" if pose_version == 4 else "local-floor"
    if frame["reference_space"] != expected_reference_space:
        raise ValueError(
            f"pose v{pose_version} must use reference_space {expected_reference_space!r}"
        )

    hands = frame["hands"]
    elbows = frame["elbows"]
    left_hand = hands["left"]
    right_hand = hands["right"]
    left_elbow = elbows["left"]
    right_elbow = elbows["right"]

    flags = 0
    flags |= _LEFT_HAND_TRACKED if left_hand["tracked"] else 0
    flags |= _RIGHT_HAND_TRACKED if right_hand["tracked"] else 0
    flags |= _LEFT_ELBOW_TRACKED if left_elbow["tracked"] else 0
    flags |= _RIGHT_ELBOW_TRACKED if right_elbow["tracked"] else 0

    shoulders: Mapping[str, Any] | None = None
    if pose_version >= 3:
        shoulders = frame["shoulders"]
        flags |= _LEFT_SHOULDER_TRACKED if shoulders["left"]["tracked"] else 0
        flags |= _RIGHT_SHOULDER_TRACKED if shoulders["right"]["tracked"] else 0

    packet = bytearray(packet_size)
    _HEADER.pack_into(
        packet,
        0,
        MAGIC,
        binary_version,
        flags,
        0,
        int(frame["seq"]),
        float(frame["timestamp_ms"]),
        float(frame["capture_epoch_ms"]),
        uuid.UUID(str(frame["session_id"])).bytes,
    )

    values: list[float] = []
    for hand in (left_hand, right_hand):
        for point in hand["points"]:
            values.extend(_encode_vector(point, 3))
    for hand in (left_hand, right_hand):
        values.extend(_encode_vector(hand["wrist_orientation"], 4))
    for elbow in (left_elbow, right_elbow):
        values.extend(_encode_vector(elbow["position"], 3))
    if shoulders is not None:
        for shoulder in (shoulders["left"], shoulders["right"]):
            values.extend(_encode_vector(shoulder["position"], 3))

    if len(values) != float_count:
        raise ValueError(f"binary pose payload must contain {float_count} float values")
    struct.pack_into(f"<{float_count}f", packet, _HEADER.size, *values)
    return bytes(packet)


def decode_pose_packet(packet: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode and structurally validate one fixed-size binary pose packet."""
    view = memoryview(packet)
    if len(view) < _HEADER.size:
        raise ValueError(
            f"binary pose packet must be exactly {LEGACY_PACKET_SIZE} or {PACKET_SIZE} bytes"
        )

    magic, version, flags, reserved, seq, timestamp_ms, capture_epoch_ms, session_bytes = (
        _HEADER.unpack_from(view)
    )
    if magic != MAGIC:
        raise ValueError("invalid binary pose magic")
    if version == LEGACY_BINARY_VERSION:
        pose_version = 2
        float_count = _LEGACY_FLOAT_COUNT
        expected_size = LEGACY_PACKET_SIZE
        allowed_flags = 0x0F
    elif version == SHOULDER_BINARY_VERSION:
        pose_version = 3
        float_count = _FLOAT_COUNT
        expected_size = PACKET_SIZE
        allowed_flags = 0x3F
    elif version == BINARY_VERSION:
        pose_version = 4
        float_count = _FLOAT_COUNT
        expected_size = PACKET_SIZE
        allowed_flags = 0x3F
    else:
        raise ValueError(f"unsupported binary pose version {version}")
    if len(view) != expected_size:
        raise ValueError(f"binary pose packet must be exactly {expected_size} bytes")
    if reserved != 0:
        raise ValueError("binary pose reserved bits must be zero")
    if flags & ~allowed_flags:
        raise ValueError("binary pose contains unknown tracking flags")

    values = struct.unpack_from(f"<{float_count}f", view, _HEADER.size)
    offset = 0

    def take_vector(size: int) -> list[float] | None:
        nonlocal offset
        vector = values[offset : offset + size]
        offset += size
        if all(math.isnan(value) for value in vector):
            return None
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("binary pose vectors must be finite or entirely NaN")
        return [float(value) for value in vector]

    hand_points: list[list[list[float] | None]] = []
    for _ in range(2):
        hand_points.append([take_vector(3) for _ in range(21)])
    orientations = [take_vector(4), take_vector(4)]
    elbow_positions = [take_vector(3), take_vector(3)]
    shoulder_positions = [take_vector(3), take_vector(3)] if pose_version >= 3 else None

    result = {
        "type": "pose",
        "version": pose_version,
        "session_id": str(uuid.UUID(bytes=session_bytes)),
        "seq": seq,
        "timestamp_ms": timestamp_ms,
        "capture_epoch_ms": capture_epoch_ms,
        "reference_space": "spine-upper-scapula" if pose_version == 4 else "local-floor",
        "units": "meters",
        "hands": {
            "left": {
                "tracked": bool(flags & _LEFT_HAND_TRACKED),
                "points": hand_points[0],
                "wrist_orientation": orientations[0],
            },
            "right": {
                "tracked": bool(flags & _RIGHT_HAND_TRACKED),
                "points": hand_points[1],
                "wrist_orientation": orientations[1],
            },
        },
        "elbows": {
            "left": {
                "tracked": bool(flags & _LEFT_ELBOW_TRACKED),
                "position": elbow_positions[0],
            },
            "right": {
                "tracked": bool(flags & _RIGHT_ELBOW_TRACKED),
                "position": elbow_positions[1],
            },
        },
    }
    if shoulder_positions is not None:
        result["shoulders"] = {
            "left": {
                "tracked": bool(flags & _LEFT_SHOULDER_TRACKED),
                "position": shoulder_positions[0],
            },
            "right": {
                "tracked": bool(flags & _RIGHT_SHOULDER_TRACKED),
                "position": shoulder_positions[1],
            },
        }
    return result


def _encode_vector(vector: Sequence[float] | None, size: int) -> list[float]:
    if vector is None:
        return [math.nan] * size
    if len(vector) != size:
        raise ValueError(f"expected a {size}-component vector")
    values = [float(value) for value in vector]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("pose vectors must contain finite values")
    return values
