from __future__ import annotations

import unittest
from math import cos, pi, sin

from quest_crt.coordinates import (
    COORDINATE_PRESETS,
    DEFAULT_COORDINATE_PRESET,
    flip_axis,
    remap_axes,
    to_hts_wrist_relative_frame,
    transform_pose_frame,
    wrist_local_to_world,
)


class CoordinateTransformTests(unittest.TestCase):
    def test_single_axis_flip(self) -> None:
        transform = flip_axis("z")

        self.assertEqual(transform.apply((1, 2, 3)), (1.0, 2.0, -3.0))
        self.assertEqual(transform.determinant, -1)
        self.assertTrue(transform.changes_handedness)

    def test_signed_axis_remap(self) -> None:
        transform = remap_axes(("-z", "-x", "y"))

        self.assertEqual(transform.apply((1, 2, 3)), (-3.0, -1.0, 2.0))
        self.assertEqual(transform.determinant, 1)
        self.assertFalse(transform.changes_handedness)

    def test_presets(self) -> None:
        point = (1, 2, 3)

        self.assertEqual(set(COORDINATE_PRESETS), {"body", "webxr", "rfu", "flu"})
        self.assertEqual(COORDINATE_PRESETS["body"].apply(point), (1.0, 2.0, 3.0))
        # body -> WebXR axis remap (Z forward-ish conventions from Pose v4 body)
        self.assertEqual(COORDINATE_PRESETS["webxr"].apply(point), (3.0, 2.0, -1.0))
        self.assertEqual(COORDINATE_PRESETS["rfu"].apply(point), (3.0, 1.0, 2.0))
        self.assertEqual(COORDINATE_PRESETS["flu"].apply(point), (1.0, -3.0, 2.0))
        self.assertEqual(DEFAULT_COORDINATE_PRESET, "body")
        self.assertEqual(COORDINATE_PRESETS[DEFAULT_COORDINATE_PRESET].determinant, 1)
        self.assertFalse(COORDINATE_PRESETS[DEFAULT_COORDINATE_PRESET].changes_handedness)

    def test_rejects_duplicate_or_unknown_axes(self) -> None:
        with self.assertRaises(ValueError):
            remap_axes(("x", "x", "z"))
        with self.assertRaises(ValueError):
            remap_axes(("x", "y", "forward"))
        with self.assertRaises(ValueError):
            flip_axis("forward")  # type: ignore[arg-type]

    def test_pose_frame_is_transformed_without_mutating_source(self) -> None:
        source = {
            "type": "pose",
            "seq": 5,
            "hands": {
                "left": {
                    "tracked": True,
                    "points": [[1, 2, 3], None],
                    "wrist_orientation": [0, 0, 0, 1],
                },
                "right": {
                    "tracked": False,
                    "points": [None],
                    "wrist_orientation": None,
                },
            },
            "elbows": {
                "left": {"tracked": True, "position": [4, 5, 6]},
                "right": {"tracked": False, "position": None},
            },
            "shoulders": {
                "left": {"tracked": True, "position": [7, 8, 9]},
                "right": {"tracked": False, "position": None},
            },
        }

        result = transform_pose_frame(source, flip_axis("z"))

        self.assertEqual(result["hands"]["left"]["points"][0], [1.0, 2.0, -3.0])
        self.assertEqual(
            result["hands"]["left"]["wrist_orientation"],
            [0.0, 0.0, 0.0, 1.0],
        )
        self.assertIsNone(result["hands"]["left"]["points"][1])
        self.assertEqual(result["elbows"]["left"]["position"], [4.0, 5.0, -6.0])
        self.assertIsNone(result["elbows"]["right"]["position"])
        self.assertEqual(result["shoulders"]["left"]["position"], [7.0, 8.0, -9.0])
        self.assertIsNone(result["shoulders"]["right"]["position"])
        self.assertEqual(source["hands"]["left"]["points"][0], [1, 2, 3])
        self.assertEqual(source["shoulders"]["left"]["position"], [7, 8, 9])
        self.assertEqual(result["seq"], 5)

    def test_hts_frame_uses_each_hands_wrist_as_landmark_origin(self) -> None:
        half_turn = pi / 4
        wrist_orientation = [0.0, 0.0, sin(half_turn), cos(half_turn)]
        source = {
            "type": "pose",
            "version": 2,
            "hands": {
                "left": {
                    "tracked": True,
                    "points": [[1, 2, 3], [1, 3, 3]],
                    "wrist_orientation": wrist_orientation,
                },
                "right": {
                    "tracked": True,
                    "points": [[4, 5, 6], [3, 5, 6]],
                    "wrist_orientation": wrist_orientation,
                },
            },
            "elbows": {
                "left": {"tracked": True, "position": [0, 1, 2]},
                "right": {"tracked": True, "position": [3, 4, 5]},
            },
        }

        output = to_hts_wrist_relative_frame(source)

        self.assertEqual(output["representation"], "hts-wrist-relative")
        self.assertEqual(output["hands"]["left"]["wrist"]["position"], [1, 2, 3])
        self.assertEqual(output["hands"]["right"]["wrist"]["position"], [4, 5, 6])
        self.assertEqual(output["hands"]["left"]["landmarks"][0], [0.0, 0.0, 0.0])
        self.assertPointAlmostEqual(
            output["hands"]["left"]["landmarks"][1],
            (1.0, 0.0, 0.0),
        )
        self.assertPointAlmostEqual(
            output["hands"]["right"]["landmarks"][1],
            (0.0, 1.0, 0.0),
        )
        reconstructed = wrist_local_to_world(
            output["hands"]["left"]["landmarks"][1],
            output["hands"]["left"]["wrist"]["position"],
            output["hands"]["left"]["wrist"]["orientation"],
        )
        self.assertPointAlmostEqual(reconstructed, (1.0, 3.0, 3.0))
        self.assertEqual(output["elbows"], source["elbows"])

    def test_orientation_and_points_remain_consistent_after_basis_change(self) -> None:
        half_turn = pi / 4
        source = {
            "hands": {
                "left": {
                    "tracked": True,
                    "points": [[1, 2, 3], [1, 3, 3]],
                    "wrist_orientation": [0, 0, sin(half_turn), cos(half_turn)],
                },
                "right": {
                    "tracked": False,
                    "points": [None],
                    "wrist_orientation": None,
                },
            },
            "elbows": {
                "left": {"tracked": False, "position": None},
                "right": {"tracked": False, "position": None},
            },
        }

        transformed = transform_pose_frame(source, COORDINATE_PRESETS["flu"])
        output = to_hts_wrist_relative_frame(transformed)
        reconstructed = wrist_local_to_world(
            output["hands"]["left"]["landmarks"][1],
            output["hands"]["left"]["wrist"]["position"],
            output["hands"]["left"]["wrist"]["orientation"],
        )

        self.assertPointAlmostEqual(
            reconstructed,
            transformed["hands"]["left"]["points"][1],
        )

    def assertPointAlmostEqual(
        self,
        actual: list[float] | tuple[float, float, float],
        expected: list[float] | tuple[float, float, float],
    ) -> None:
        for actual_value, expected_value in zip(actual, expected, strict=True):
            self.assertAlmostEqual(actual_value, expected_value)


if __name__ == "__main__":
    unittest.main()
