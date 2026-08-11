from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from server import (
    PoseSourceBusyError,
    PoseStreamProcessor,
    active_pose_source,
    transport_sessions,
)


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


class TransportSessionLeaseTests(unittest.TestCase):
    """PoseStreamProcessor owns its transport-session lease lifecycle.

    The global ``transport_sessions`` singleton is shared across tests and
    empty entries linger until their (real-clock) lease passes, so these
    tests assert on the specific session entry instead of global counts.
    """

    def _session_channels(self, tsid: str) -> list[str] | None:
        for session in transport_sessions.describe()["sessions"]:
            if session["transport_session_id"] == tsid:
                return session["channels"]
        return None

    def test_processor_releases_lease_on_close(self) -> None:
        with patch("server.POSE_LOG_ENABLED", False):
            lease = transport_sessions.begin_channel("ts-lease-1", "pose", "quest-a")
            self.assertEqual(self._session_channels("ts-lease-1"), ["pose"])

            processor = PoseStreamProcessor(
                "webrtc", "quest-a", transport_session_id="ts-lease-1", lease=lease
            )
            processor.close()
            processor.wait_closed(timeout=1)

        # The lease token was released; the empty session entry lingers
        # until the lease window passes (real clock, so not yet reaped).
        self.assertEqual(self._session_channels("ts-lease-1"), [])

    def test_busy_rejection_releases_lease(self) -> None:
        with patch("server.POSE_LOG_ENABLED", False):
            first = PoseStreamProcessor("webrtc", "quest-a")
            try:
                lease = transport_sessions.begin_channel("ts-lease-2", "pose", "quest-b")
                self.assertEqual(self._session_channels("ts-lease-2"), ["pose"])
                with self.assertRaises(PoseSourceBusyError):
                    PoseStreamProcessor(
                        "wss",
                        "quest-b",
                        transport_session_id="ts-lease-2",
                        lease=lease,
                    )
                # The rejected processor released its lease inside __init__;
                # the empty session entry lingers (avoids reconnect churn).
                self.assertEqual(self._session_channels("ts-lease-2"), [])
            finally:
                first.close()
                first.wait_closed(timeout=1)


if __name__ == "__main__":
    unittest.main()
