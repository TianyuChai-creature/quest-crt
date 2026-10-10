import json
import ssl
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quest_xr_bridge import CameraIntrinsics, QuestServer, VideoConfig, VideoDisplayConfig


class VideoDisplayTests(unittest.TestCase):
    def test_defaults_preserve_original_colors(self):
        display = VideoDisplayConfig()
        self.assertEqual((display.gamma, display.saturation, display.swap_eyes), (1, 1, False))

    def test_invalid_gamma_and_saturation_are_rejected(self):
        for name, values in (
            ("gamma", [0.49, 2.01, True, None, float("inf"), float("nan")]),
            ("saturation", [-0.01, 2.01, False, None, float("inf"), float("nan")]),
        ):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    VideoDisplayConfig(**{name: value})

    def test_plane_geometry_validation_and_configuration(self):
        display = VideoDisplayConfig(projection="plane", height_m=8, distance_m=7,
                                     aspect_ratio=1.66667, offset_y_m=-1)
        self.assertEqual((display.height_m, display.distance_m), (8, 7))
        for name in ("height_m", "distance_m", "aspect_ratio", "offset_x_m", "offset_y_m"):
            for value in (True, float("nan"), float("inf"), "1"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    VideoDisplayConfig(**{name: value})
        for update in ({"projection": "sphere"}, {"height_m": 0}, {"distance_m": -1},
                       {"aspect_ratio": 0}, {"offset_x_m": 101}):
            with self.assertRaises(ValueError):
                VideoDisplayConfig(**update)

    def test_api_updates_independent_settings_without_starting_video(self):
        with tempfile.TemporaryDirectory() as directory, QuestServer(
            host="127.0.0.1", port=0, data_dir=directory
        ) as service:
            context = ssl._create_unverified_context()

            def request(update=None):
                data = None if update is None else json.dumps(update).encode()
                message = Request(
                    service.url + "/api/video/config", data=data,
                    method="GET" if data is None else "PUT",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(message, context=context, timeout=2) as response:
                    return json.load(response)

            snapshot = request({"gamma": 1.2, "saturation": 0.85})
            self.assertEqual(snapshot["display"]["gamma"], 1.2)
            self.assertEqual(snapshot["display"]["saturation"], 0.85)
            self.assertFalse(snapshot["running"])
            snapshot = request({"projection": "plane", "height_m": 8, "distance_m": 7,
                                "aspect_ratio": 1.66667, "offset_y_m": -1})
            self.assertEqual(snapshot["display"]["distance_m"], 7)
            self.assertEqual(snapshot["display"]["gamma"], 1.2)
            for update in ({"radius_m": 1}, {"gamma": 0}, {"saturation": 3}, {"saturation": None}):
                with self.assertRaises(HTTPError) as caught:
                    request(update)
                self.assertEqual(caught.exception.code, 422)
                caught.exception.close()
            self.assertEqual(request()["display"], snapshot["display"])

    def test_rectified_intrinsics_are_finite_and_stereo_pairs_are_explicit(self):
        intrinsics = CameraIntrinsics(727.8, 727.8, 626.9, 360.7)
        config = VideoConfig(1280, 720, mode="stereo", left_intrinsics=intrinsics,
                             right_intrinsics=intrinsics)
        self.assertIs(config.left_intrinsics, intrinsics)
        self.assertIsNone(VideoConfig(1280, 720).left_intrinsics)
        for field in ("fx", "fy", "cx", "cy"):
            for value in (None, True, float("inf"), float("nan")):
                parameters = {"fx": 727.8, "fy": 727.8, "cx": 626.9, "cy": 360.7, field: value}
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    CameraIntrinsics(**parameters)
        for fx, fy in ((0, 1), (1, 0), (-1, 1)):
            with self.assertRaises(ValueError):
                CameraIntrinsics(fx, fy, 0, 0)
        with self.assertRaises(ValueError):
            VideoConfig(1280, 720, mode="stereo", left_intrinsics=intrinsics)
        with self.assertRaises(ValueError):
            VideoConfig(1280, 720, right_intrinsics=intrinsics)
        with self.assertRaises(TypeError):
            VideoConfig(1280, 720, left_intrinsics={"fx": 1, "fy": 1, "cx": 0, "cy": 0})


if __name__ == "__main__":
    unittest.main()
