from __future__ import annotations

import unittest

from quest_crt.binary_protocol import (
    LEGACY_PACKET_SIZE,
    PACKET_SIZE,
    decode_pose_packet,
    encode_pose_packet,
)


def pose_frame_v2() -> dict[str, object]:
    left_points = [[index / 100, 1.0, -0.2] for index in range(21)]
    right_points = [[-index / 100, 1.1, -0.3] for index in range(21)]
    return {
        "type": "pose",
        "version": 2,
        "session_id": "a752e716-5364-477e-860a-62723e50d561",
        "seq": 1234,
        "timestamp_ms": 12345.5,
        "capture_epoch_ms": 1_784_628_000_123.25,
        "reference_space": "local-floor",
        "units": "meters",
        "hands": {
            "left": {
                "tracked": True,
                "points": left_points,
                "wrist_orientation": [0.0, 0.0, 0.0, 1.0],
            },
            "right": {
                "tracked": True,
                "points": right_points,
                "wrist_orientation": [0.1, 0.2, 0.3, 0.9],
            },
        },
        "elbows": {
            "left": {"tracked": True, "position": [-0.2, 0.8, -0.1]},
            "right": {"tracked": False, "position": None},
        },
    }


def pose_frame_v4() -> dict[str, object]:
    frame = pose_frame_v2()
    frame["version"] = 4
    frame["reference_space"] = "spine-upper-scapula"
    frame["shoulders"] = {
        "left": {"tracked": True, "position": [0.0, 0.1, 0.15]},
        "right": {"tracked": True, "position": [0.0, 0.1, -0.15]},
    }
    return frame


class BinaryPoseProtocolTests(unittest.TestCase):
    def test_v2_round_trip_legacy_size(self) -> None:
        source = pose_frame_v2()

        packet = encode_pose_packet(source)
        decoded = decode_pose_packet(packet)

        self.assertEqual(len(packet), LEGACY_PACKET_SIZE)
        self.assertEqual(LEGACY_PACKET_SIZE, 604)
        self.assertEqual(decoded["version"], 2)
        self.assertEqual(decoded["session_id"], source["session_id"])
        self.assertEqual(decoded["seq"], 1234)
        self.assertAlmostEqual(decoded["timestamp_ms"], 12345.5)
        self.assertAlmostEqual(decoded["capture_epoch_ms"], 1_784_628_000_123.25)
        self.assertTrue(decoded["hands"]["left"]["tracked"])
        self.assertAlmostEqual(decoded["hands"]["right"]["points"][20][0], -0.2)
        self.assertIsNone(decoded["elbows"]["right"]["position"])
        self.assertNotIn("shoulders", decoded)

    def test_v4_round_trip_includes_shoulders(self) -> None:
        source = pose_frame_v4()

        packet = encode_pose_packet(source)
        decoded = decode_pose_packet(packet)

        self.assertEqual(len(packet), PACKET_SIZE)
        self.assertEqual(PACKET_SIZE, 628)
        self.assertEqual(decoded["version"], 4)
        self.assertEqual(decoded["reference_space"], "spine-upper-scapula")
        self.assertTrue(decoded["shoulders"]["left"]["tracked"])
        self.assertAlmostEqual(decoded["shoulders"]["left"]["position"][2], 0.15)
        self.assertAlmostEqual(decoded["shoulders"]["right"]["position"][2], -0.15)

    def test_invalid_size_and_magic_are_rejected(self) -> None:
        packet = encode_pose_packet(pose_frame_v2())

        with self.assertRaisesRegex(ValueError, "604|628"):
            decode_pose_packet(packet[:-1])

        invalid_magic = bytearray(packet)
        invalid_magic[0] = 0
        with self.assertRaisesRegex(ValueError, "magic"):
            decode_pose_packet(invalid_magic)

    def test_partial_nan_vector_is_rejected(self) -> None:
        packet = bytearray(encode_pose_packet(pose_frame_v2()))
        # First float starts after header (44 bytes). Make only one component NaN.
        packet[44:48] = bytes.fromhex("0000c07f")

        with self.assertRaisesRegex(ValueError, "finite or entirely NaN"):
            decode_pose_packet(packet)


if __name__ == "__main__":
    unittest.main()
