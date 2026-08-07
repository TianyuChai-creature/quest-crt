from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from server import PoseSourceBusyError, PoseStreamProcessor, active_pose_source


class LatestIngressTests(unittest.TestCase):
    def test_second_pose_source_is_rejected_until_first_closes(self) -> None:
        with patch("server.POSE_LOG_ENABLED", False):
            first = PoseStreamProcessor("webrtc", "quest-a")
            try:
                with self.assertRaises(PoseSourceBusyError):
                    PoseStreamProcessor("wss", "quest-b")
                self.assertEqual(active_pose_source.describe()["client"], "quest-a")
            finally:
                first.close()
                first.wait_closed(timeout=1)

            replacement = PoseStreamProcessor("wss", "quest-b")
            replacement.close()
            replacement.wait_closed(timeout=1)

        self.assertFalse(active_pose_source.describe()["active"])

    def test_slow_processing_coalesces_pending_messages_to_latest(self) -> None:
        processing_started = threading.Event()
        release_processing = threading.Event()
        processed: list[bytes] = []

        def process(
            _processor: PoseStreamProcessor,
            message: str | bytes,
            _epoch_ms: float,
            _monotonic_ms: float,
        ) -> None:
            processed.append(bytes(message))
            if len(processed) == 1:
                processing_started.set()
                self.assertTrue(release_processing.wait(timeout=1))

        with (
            patch("server.POSE_LOG_ENABLED", False),
            patch.object(PoseStreamProcessor, "_process", process),
        ):
            processor = PoseStreamProcessor("test", "unit")
            processor.process(b"first", 1.0, 1.0)
            self.assertTrue(processing_started.wait(timeout=1))
            processor.process(b"old", 2.0, 2.0)
            processor.process(b"latest", 3.0, 3.0)
            processor.close()
            release_processing.set()
            processor.wait_closed(timeout=1)

        self.assertEqual(processed, [b"first", b"latest"])

    def test_unordered_binary_arrival_keeps_highest_pending_sequence(self) -> None:
        processing_started = threading.Event()
        release_processing = threading.Event()
        processed: list[bytes] = []

        def packet(seq: int) -> bytes:
            value = bytearray(44)
            value[:4] = b"QCRT"
            value[8:12] = seq.to_bytes(4, "little")
            value[28:44] = b"same-session-id!"
            return bytes(value)

        def process(
            _processor: PoseStreamProcessor,
            message: str | bytes,
            _epoch_ms: float,
            _monotonic_ms: float,
        ) -> None:
            processed.append(bytes(message))
            if len(processed) == 1:
                processing_started.set()
                self.assertTrue(release_processing.wait(timeout=1))

        with (
            patch("server.POSE_LOG_ENABLED", False),
            patch.object(PoseStreamProcessor, "_process", process),
        ):
            processor = PoseStreamProcessor("test", "unit")
            processor.process(packet(1), 1.0, 1.0)
            self.assertTrue(processing_started.wait(timeout=1))
            processor.process(packet(3), 3.0, 3.0)
            processor.process(packet(2), 2.0, 2.0)
            processor.close()
            release_processing.set()
            processor.wait_closed(timeout=1)

        sequences = [int.from_bytes(message[8:12], "little") for message in processed]
        self.assertEqual(sequences, [1, 3])


if __name__ == "__main__":
    unittest.main()
