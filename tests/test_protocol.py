from __future__ import annotations

import unittest

from pydantic import ValidationError

from server import PoseFrame


def valid_frame_v2() -> dict[str, object]:
    point = [0.1, 1.0, -0.4]
    hand = {
        "tracked": True,
        "points": [point for _ in range(21)],
        "wrist_orientation": [0.0, 0.0, 0.0, 1.0],
    }
    return {
        "type": "pose",
        "version": 2,
        "session_id": "protocol-test",
        "seq": 1,
        "timestamp_ms": 1.0,
        "capture_epoch_ms": 1_700_000_000_001.0,
        "reference_space": "local-floor",
        "units": "meters",
        "hands": {"left": hand, "right": hand},
        "elbows": {
            "left": {"tracked": True, "position": point},
            "right": {"tracked": True, "position": point},
        },
    }


def valid_frame_v4() -> dict[str, object]:
    frame = valid_frame_v2()
    frame["version"] = 4
    frame["reference_space"] = "spine-upper-scapula"
    point = [0.0, 0.1, 0.15]
    frame["shoulders"] = {
        "left": {"tracked": True, "position": point},
        "right": {"tracked": True, "position": [0.0, 0.1, -0.15]},
    }
    return frame


class PoseProtocolTests(unittest.TestCase):
    def test_pose_v2_accepts_wrist_orientations(self) -> None:
        frame = PoseFrame.model_validate(valid_frame_v2())

        self.assertEqual(frame.version, 2)
        self.assertEqual(frame.capture_epoch_ms, 1_700_000_000_001.0)
        self.assertEqual(frame.hands.left.wrist_orientation, (0.0, 0.0, 0.0, 1.0))
        self.assertIsNone(frame.shoulders)

    def test_pose_v4_requires_shoulders_and_spine_space(self) -> None:
        frame = PoseFrame.model_validate(valid_frame_v4())

        self.assertEqual(frame.version, 4)
        self.assertEqual(frame.reference_space, "spine-upper-scapula")
        assert frame.shoulders is not None
        self.assertTrue(frame.shoulders.left.tracked)
        self.assertEqual(frame.shoulders.left.position, (0.0, 0.1, 0.15))

    def test_pose_v4_rejects_missing_shoulders(self) -> None:
        source = valid_frame_v4()
        del source["shoulders"]
        with self.assertRaises(ValidationError):
            PoseFrame.model_validate(source)

    def test_pose_v2_allows_legacy_frame_without_capture_epoch(self) -> None:
        source = valid_frame_v2()
        del source["capture_epoch_ms"]

        frame = PoseFrame.model_validate(source)

        self.assertIsNone(frame.capture_epoch_ms)

    def test_pose_v1_is_rejected(self) -> None:
        source = valid_frame_v2()
        source["version"] = 1

        with self.assertRaises(ValidationError):
            PoseFrame.model_validate(source)

    def test_zero_wrist_quaternion_is_rejected(self) -> None:
        source = valid_frame_v2()
        source["hands"]["left"]["wrist_orientation"] = [0.0, 0.0, 0.0, 0.0]  # type: ignore[index]

        with self.assertRaises(ValidationError):
            PoseFrame.model_validate(source)


if __name__ == "__main__":
    unittest.main()
