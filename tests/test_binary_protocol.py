from __future__ import annotations

import math
import struct
import unittest

from quest_xr_bridge.binary_protocol import (
    BINARY_VERSION,
    PACKET_SIZE,
    decode_pose_packet,
    encode_pose_packet,
)


def pose_frame_v5(tracked: bool = True) -> dict:
    return {
        "type": "pose",
        "version": 5,
        "session_id": "a752e716-5364-477e-860a-62723e50d561",
        "seq": 1234,
        "timestamp_ms": 12345.5,
        "capture_epoch_ms": 1_784_628_000_123.25,
        "reference_space": "spine-upper-scapula",
        "units": "meters",
        "hands": {
            "left": {
                "tracked": True,
                "points": [[index / 100, 1.0, -0.2] for index in range(21)],
                "wrist_orientation": [0.0, 0.0, 0.0, 1.0],
            },
            "right": {
                "tracked": True,
                "points": [[-index / 100, 1.1, -0.3] for index in range(21)],
                "wrist_orientation": [0.1, 0.2, 0.3, 0.9],
            },
        },
        "elbows": {
            "left": {"tracked": True, "position": [-0.2, 0.8, -0.1]},
            "right": {"tracked": False, "position": None},
        },
        "shoulders": {
            "left": {"tracked": True, "position": [0.0, 0.1, 0.15]},
            "right": {"tracked": True, "position": [0.0, 0.1, -0.15]},
        },
        "head": {
            "tracked": tracked,
            "yaw_deg": 30.0 if tracked else None,
            "pitch_deg": -20.0 if tracked else None,
        },
    }


class BinaryPoseProtocolTests(unittest.TestCase):
    def test_v5_round_trip_preserves_layout_and_defaults(self) -> None:
        source = pose_frame_v5()
        packet = encode_pose_packet(source)
        self.assertEqual(PACKET_SIZE, 804)
        self.assertEqual(len(packet), 804)
        self.assertEqual(packet[:4], b"QCRT")
        self.assertEqual(packet[4], BINARY_VERSION)
        self.assertEqual(packet[5], 0x77)
        self.assertEqual(packet[6:8], b"\x00\x00")
        self.assertEqual(struct.unpack_from("<I", packet, 8)[0], source["seq"])
        self.assertAlmostEqual(struct.unpack_from("<f", packet, 604)[0], 0.0)
        self.assertEqual(struct.unpack_from("<2f", packet, 628), (30.0, -20.0))
        self.assertTrue(all(math.isnan(x) for x in struct.unpack_from("<42f", packet, 636)))
        for raw in (packet, bytearray(packet), memoryview(packet)):
            result = decode_pose_packet(raw)
            self.assertEqual(result["version"], 5)
            self.assertEqual(result["session_id"], source["session_id"])
            self.assertEqual(result["seq"], source["seq"])
            self.assertEqual(result["timestamp_ms"], source["timestamp_ms"])
            self.assertEqual(result["capture_epoch_ms"], source["capture_epoch_ms"])
            self.assertEqual(result["reference_space"], "spine-upper-scapula")
            self.assertEqual(result["head"], source["head"])
            self.assertAlmostEqual(result["shoulders"]["left"]["position"][2], 0.15)
            self.assertIsNone(result["elbows"]["right"]["position"])
            self.assertEqual(result["hands"]["left"]["radii"], [None] * 21)
            self.assertNotIn("video_return", result)

    def test_unavailable_tracking_uses_nan_and_round_trips(self) -> None:
        frame = pose_frame_v5(False)
        frame["hands"]["right"] = {
            "tracked": False,
            "points": [None] * 21,
            "wrist_orientation": None,
        }
        frame["shoulders"]["right"] = {"tracked": False, "position": None}
        packet = encode_pose_packet(frame)
        self.assertEqual(packet[5], 0x15)
        result = decode_pose_packet(packet)
        self.assertEqual(result["head"], frame["head"])
        self.assertEqual(result["hands"]["right"]["points"], [None] * 21)
        self.assertIsNone(result["hands"]["right"]["wrist_orientation"])
        self.assertIsNone(result["shoulders"]["right"]["position"])

    def test_obsolete_sizes_versions_and_header_fields_are_rejected(self) -> None:
        packet = encode_pose_packet(pose_frame_v5())
        for size in (0, 44, 604, 628, 636, 803, 805):
            with self.subTest(size=size), self.assertRaises(ValueError):
                decode_pose_packet(packet[:size] if size < 804 else packet + b"\x00")
        with self.assertRaises(ValueError):
            decode_pose_packet(memoryview(packet + packet)[::2])
        for index, value in (
            (0, 0),
            (4, 1),
            (4, 2),
            (4, 3),
            (4, 4),
            (4, 6),
            (5, 0xF7),
            (6, 1),
            (7, 1),
        ):
            corrupt = bytearray(packet)
            corrupt[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                decode_pose_packet(corrupt)

    def test_decoder_rejects_nonfinite_timestamps_and_zero_sequence(self) -> None:
        for offset, fmt, value in (
            (8, "I", 0),
            (12, "d", math.nan),
            (12, "d", math.inf),
            (20, "d", -1.0),
            (20, "d", math.inf),
        ):
            packet = bytearray(encode_pose_packet(pose_frame_v5()))
            struct.pack_into("<" + fmt, packet, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                decode_pose_packet(packet)

    def test_decoder_rejects_inconsistent_tracking_and_vectors(self) -> None:
        cases = (
            (44, "f", (math.nan,)),
            (44, "3f", (math.nan,) * 3),
            (548, "4f", (0.0,) * 4),
            (604, "3f", (math.nan,) * 3),
            (628, "f", (math.inf,)),
            (636, "f", (-0.1,)),
        )
        for offset, fmt, values in cases:
            packet = bytearray(encode_pose_packet(pose_frame_v5()))
            struct.pack_into("<" + fmt, packet, offset, *values)
            with self.subTest(offset=offset, fmt=fmt), self.assertRaises(ValueError):
                decode_pose_packet(packet)
        packet = bytearray(encode_pose_packet(pose_frame_v5()))
        packet[5] &= ~(1 << 6)
        with self.assertRaises(ValueError):
            decode_pose_packet(packet)

    def test_encoder_rejects_invalid_headers_and_float32_overflow(self) -> None:
        for field, values in {
            "version": (1, 2, 3, 4, 6),
            "session_id": ("", "not-a-uuid"),
            "seq": (0, -1, 0x100000000, 1.5, True, "1"),
            "timestamp_ms": (-1, math.nan, math.inf, "1", True),
            "capture_epoch_ms": (-1, math.nan, math.inf, None),
            "reference_space": ("local-floor",),
        }.items():
            for value in values:
                frame = pose_frame_v5()
                frame[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    encode_pose_packet(frame)
        frame = pose_frame_v5()
        frame["hands"]["left"]["points"][1][0] = 1e39
        with self.assertRaises(ValueError):
            encode_pose_packet(frame)
        frame = pose_frame_v5()
        frame["hands"]["left"]["wrist_orientation"] = [1e-50, 0.0, 0.0, 0.0]
        with self.assertRaises(ValueError):
            encode_pose_packet(frame)


if __name__ == "__main__":
    unittest.main()
