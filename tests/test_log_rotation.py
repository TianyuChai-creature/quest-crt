from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server import AsyncPoseLog, LogRetentionManager, RotatingPoseLog


class RotatingPoseLogTests(unittest.TestCase):
    def test_async_log_serializes_records_on_background_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            retention = LogRetentionManager(directory, max_bytes=1_000)
            pose_log = AsyncPoseLog(directory, "async", retention, queue_frames=4)
            path = pose_log.path

            pose_log.submit({"seq": 1})
            pose_log.submit({"seq": 2})
            pose_log.close()
            pose_log.wait_closed(timeout=1)

            self.assertFalse(pose_log._thread.is_alive())
            self.assertEqual(path.read_text(encoding="utf-8"), '{"seq":1}\n{"seq":2}\n')

    def test_rotates_before_size_limit_would_be_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            retention = LogRetentionManager(directory, max_bytes=1_000)

            with RotatingPoseLog(
                directory,
                "size",
                retention,
                segment_max_bytes=12,
                segment_max_seconds=60,
            ) as pose_log:
                pose_log.write("12345")
                pose_log.write("67890")
                pose_log.write("abcde")

            segments = sorted(directory.glob("pose_*.jsonl"))
            self.assertEqual(len(segments), 2)
            self.assertEqual(segments[0].read_text(encoding="utf-8"), "12345\n67890\n")
            self.assertEqual(segments[1].read_text(encoding="utf-8"), "abcde\n")

    def test_rotates_when_time_limit_is_reached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            retention = LogRetentionManager(directory, max_bytes=1_000)
            now = [0.0]

            with RotatingPoseLog(
                directory,
                "time",
                retention,
                segment_max_bytes=1_000,
                segment_max_seconds=10,
                clock=lambda: now[0],
            ) as pose_log:
                pose_log.write("first")
                now[0] = 10.0
                pose_log.write("second")

            segments = sorted(directory.glob("pose_*.jsonl"))
            self.assertEqual(len(segments), 2)
            self.assertEqual(segments[0].read_text(encoding="utf-8"), "first\n")
            self.assertEqual(segments[1].read_text(encoding="utf-8"), "second\n")

    def test_retention_deletes_oldest_closed_segments_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            retention = LogRetentionManager(directory, max_bytes=10)

            with RotatingPoseLog(
                directory,
                "retention",
                retention,
                segment_max_bytes=5,
                segment_max_seconds=60,
            ) as pose_log:
                pose_log.write("1234")
                pose_log.write("5678")
                pose_log.write("abcd")

                segments_during_write = sorted(directory.glob("pose_*.jsonl"))
                self.assertEqual(len(segments_during_write), 2)
                self.assertEqual(pose_log.path, segments_during_write[-1])

            segments = sorted(directory.glob("pose_*.jsonl"))
            self.assertEqual(len(segments), 2)
            self.assertFalse((directory / "pose_retention_part0001.jsonl").exists())
            self.assertTrue((directory / "pose_retention_part0002.jsonl").exists())
            self.assertTrue((directory / "pose_retention_part0003.jsonl").exists())
            self.assertLessEqual(sum(path.stat().st_size for path in segments), 10)


if __name__ == "__main__":
    unittest.main()
