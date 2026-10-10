from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from test_binary_protocol import pose_frame_v5

from quest_xr_bridge.binary_protocol import PACKET_SIZE, encode_pose_packet
from quest_xr_bridge.runtime import PoseRuntime, PoseSourceBusyError, PoseStreamProcessor


def packet(seq: int) -> bytes:
    value = bytearray(PACKET_SIZE)
    value[:4] = b"QCRT"
    value[4] = 5
    value[8:12] = seq.to_bytes(4, "little")
    value[28:44] = b"same-session-id!"
    return bytes(value)


class LatestIngressTests(unittest.TestCase):
    def test_start_failure_releases_source_even_when_log_shutdown_fails(self):
        runtime = PoseRuntime(record_poses=True)
        with (
            patch("quest_xr_bridge.runtime.AsyncPoseLog") as log,
            patch(
                "quest_xr_bridge.runtime.threading.Thread.start",
                side_effect=RuntimeError("thread unavailable"),
            ),
        ):
            log.return_value.wait_closed.side_effect = TimeoutError("slow disk")
            with self.assertRaises(TimeoutError):
                PoseStreamProcessor(runtime, "unit")
        self.assertFalse(runtime.active_source.describe()["active"])

    def test_source_release_clears_old_state_before_replacement(self):
        runtime = PoseRuntime()
        first = PoseStreamProcessor(runtime, "quest-a")
        try:
            with self.assertRaises(PoseSourceBusyError):
                PoseStreamProcessor(runtime, "quest-b")
            first._process(encode_pose_packet(pose_frame_v5()), 1000, 1000)
            self.assertIsNotNone(runtime.latest_pose.snapshot()[1])
        finally:
            first.close()
            first.wait_closed(1)
        self.assertIsNone(runtime.latest_pose.snapshot()[1])
        second = PoseStreamProcessor(runtime, "quest-b")
        try:
            second._process(encode_pose_packet(pose_frame_v5()), 2000, 2000)
            self.assertEqual(runtime.ingress.describe()["seq"], 1234)
            self.assertEqual(runtime.active_source.describe()["client"], "quest-b")
        finally:
            second.close()
            second.wait_closed(1)
        self.assertFalse(runtime.active_source.describe()["active"])

    def test_pending_burst_keeps_highest_sequence(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        processed = []

        def process(_processor, message, _epoch, _mono):
            processed.append(int.from_bytes(message[8:12], "little"))
            if len(processed) == 1:
                started.set()
                release.wait(2)
            else:
                finished.set()

        with patch.object(PoseStreamProcessor, "_process", process):
            processor = PoseStreamProcessor(PoseRuntime(), "unit")
            try:
                processor.process(packet(1), 1, 1)
                self.assertTrue(started.wait(1))
                for seq in (2, 4, 3):
                    processor.process(packet(seq), seq, seq)
                release.set()
                self.assertTrue(finished.wait(1))
            finally:
                release.set()
                processor.close()
                processor.wait_closed(1)
        self.assertEqual(processed, [1, 4])

    def test_repeated_sequence_is_not_republished_and_text_is_not_accepted(self):
        runtime = PoseRuntime()
        processor = PoseStreamProcessor(runtime, "unit")
        try:
            data = encode_pose_packet(pose_frame_v5())
            processor._process(data, 1, 1)
            generation = runtime.latest_pose.snapshot()[0]
            processor._process(data, 2, 2)
            processor.process(json.dumps(pose_frame_v5()), 3, 3)
            self.assertEqual(runtime.latest_pose.snapshot()[0], generation)
            self.assertEqual(processor._invalid, 1)
        finally:
            processor.close()
            processor.wait_closed(1)

    def test_consumers_share_serialized_output_and_coordinate_changes_wait_for_new_frame(self):
        runtime = PoseRuntime()
        raw = pose_frame_v5()
        runtime.latest_pose.publish(raw)
        with patch(
            "quest_xr_bridge.runtime.make_output_pose",
            wraps=__import__("quest_xr_bridge.runtime", fromlist=["make_output_pose"]).make_output_pose,
        ) as build:
            first = runtime.output_snapshot()
            second = runtime.output_snapshot()
            self.assertEqual(first, second)
            self.assertEqual(build.call_count, 1)
        output = json.loads(first[1])
        self.assertEqual(output["coordinate_transform"]["name"], "body")
        self.assertEqual(len(output["hands"]["left"]["landmarks"]), 21)
        with patch(
            "quest_xr_bridge.runtime.time.monotonic",
            return_value=runtime.latest_pose.snapshot()[2] + 0.251,
        ):
            self.assertIsNone(runtime.output_snapshot()[1])
