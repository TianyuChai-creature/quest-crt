import importlib.util
import unittest
from array import array
from unittest.mock import Mock

from examples.frozen_stereo import prepare_eye, start_frozen_video


class FrozenProfileTests(unittest.TestCase):
    def test_frozen_geometry_and_format_are_explicit(self):
        service = Mock()
        start_frozen_video(service)
        display = service.set_video_display.call_args.args[0]
        config = service.start_video.call_args.args[0]
        self.assertEqual((display.projection, display.height_m, display.distance_m,
                          display.aspect_ratio, display.offset_y_m),
                         ("plane", 8, 7, 1.66667, -1))
        self.assertEqual((config.width, config.height, config.mode, config.fps),
                         (1278, 360, "stereo", 60))
        self.assertEqual([call[0] for call in service.method_calls],
                         ["set_video_display", "start_video"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional example requires Pillow")
    def test_crop_row_phase_rgb_and_owned_output(self):
        row = b"\0\0\xff" + b"\0\xff\0" * 1278 + b"\0\0\xff"
        source = bytearray((row + b"\xff\0\0" * 1280) * 360)
        result = prepare_eye(source)
        self.assertEqual(len(result), 1278 * 360 * 3)
        self.assertGreaterEqual(min(result[1::3]), 250)
        self.assertLessEqual(max(result[0::3] + result[2::3]), 5)
        source[:] = b"\0" * len(source)
        self.assertGreaterEqual(min(result[1::3]), 250)
        for invalid in (b"short", array("H", [0]) * (1280 * 720 * 3 // 2),
                        memoryview(bytearray(1280 * 720 * 6))[::2]):
            with self.assertRaises(ValueError):
                prepare_eye(invalid)
