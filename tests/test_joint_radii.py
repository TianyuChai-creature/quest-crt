from __future__ import annotations

import base64
import json
import struct
import subprocess
import unittest
from pathlib import Path

from pydantic import ValidationError
from test_binary_protocol import pose_frame_v5

from quest_xr_bridge.binary_protocol import decode_pose_packet, encode_pose_packet
from quest_xr_bridge.coordinates import (
    COORDINATE_PRESETS,
    to_hts_wrist_relative_frame,
    transform_pose_frame,
)
from quest_xr_bridge.pose import PoseFrame


class JointRadiiTests(unittest.TestCase):
    def frame(self) -> dict:
        frame = pose_frame_v5()
        frame["hands"]["left"]["radii"] = [0.008, None, 0.0] + [0.01] * 18
        frame["hands"]["right"]["radii"] = [None] * 21
        return frame

    def test_radii_survive_binary_validation_coordinate_and_wrist_output(self) -> None:
        decoded = decode_pose_packet(encode_pose_packet(self.frame()))
        validated = PoseFrame.model_validate(decoded).model_dump(mode="json")
        transformed = transform_pose_frame(validated, COORDINATE_PRESETS["flu"])
        output = to_hts_wrist_relative_frame(transformed)
        self.assertAlmostEqual(output["hands"]["left"]["radii"][0], 0.008)
        self.assertEqual(output["hands"]["left"]["radii"][1:3], [None, 0.0])
        self.assertEqual(output["hands"]["right"]["radii"], [None] * 21)
        self.assertEqual(output["head"], decoded["head"])
        self.assertEqual(output["session_id"], decoded["session_id"])
        self.assertEqual(output["seq"], decoded["seq"])
        self.assertEqual(output["hands"]["left"]["landmarks"][0], [0.0, 0.0, 0.0])
        self.assertEqual(
            output["hands"]["left"]["wrist"]["position"], transformed["hands"]["left"]["points"][0]
        )

    def test_absent_radii_always_use_full_packet_and_default_to_null(self) -> None:
        packet = encode_pose_packet(pose_frame_v5())
        self.assertEqual(len(packet), 804)
        self.assertEqual(decode_pose_packet(packet)["hands"]["left"]["radii"], [None] * 21)

    def test_model_and_encoder_reject_invalid_radius_values(self) -> None:
        for radii in (
            [0.01] * 20,
            [0.01] * 22,
            [-0.01] * 21,
            [float("inf")] * 21,
            [float("nan")] * 21,
            [True] * 21,
            ["0.01"] * 21,
            None,
        ):
            frame = self.frame()
            frame["hands"]["left"]["radii"] = radii
            with self.subTest(radii=radii):
                with self.assertRaises(ValidationError):
                    PoseFrame.model_validate(frame)
                with self.assertRaises(ValueError):
                    encode_pose_packet(frame)
        frame = self.frame()
        frame["hands"]["left"]["tracked"] = False
        frame["hands"]["left"]["points"][3] = None
        with self.assertRaises(ValidationError):
            PoseFrame.model_validate(frame)
        frame["hands"]["left"]["radii"][3] = None
        decoded = decode_pose_packet(encode_pose_packet(frame))
        self.assertIsNone(decoded["hands"]["left"]["points"][3])
        self.assertIsNone(decoded["hands"]["left"]["radii"][3])

    def test_wire_rejects_negative_and_infinite_radii(self) -> None:
        for radius in (-0.1, float("inf"), -float("inf")):
            packet = bytearray(encode_pose_packet(self.frame()))
            struct.pack_into("<f", packet, 636, radius)
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                decode_pose_packet(packet)

    def test_browser_encoder_matches_python_decoder(self) -> None:
        output = subprocess.check_output(
            ["node", str(Path(__file__).with_suffix(".cjs")), "--packets"], text=True
        )
        packets = json.loads(output)
        self.assertTrue(packets)
        for packet in packets:
            frame = decode_pose_packet(base64.b64decode(packet))
            self.assertAlmostEqual(frame["hands"]["left"]["radii"][8], 0.008)
            self.assertEqual(frame["hands"]["right"]["radii"], [None] * 21)


if __name__ == "__main__":
    unittest.main()
