from __future__ import annotations

import math
import socket
import struct
import unittest

from quest_crt.stable_stream import StreamBus, StreamEnvelope
from quest_crt.stream_protocol import (
    STREAM_MAGIC,
    STREAM_VERSION,
    decode_stream_envelope,
    encode_stream_envelope,
)
from quest_crt.stream_udp import UdpStreamPublisher


def _sample_pose() -> dict:
    def hand(tracked: bool = True) -> dict:
        return {
            "tracked": tracked,
            "wrist": {
                "position": [0.1, 0.2, 0.3],
                "orientation": [0.0, 0.0, 0.0, 1.0],
            },
            "landmarks": [[float(i), 0.0, 0.0] for i in range(21)],
        }

    return {
        "type": "pose",
        "representation": "hts-wrist-relative",
        "seq": 42,
        "hands": {"left": hand(), "right": hand()},
        "elbows": {
            "left": {"tracked": True, "position": [1.0, 2.0, 3.0]},
            "right": {"tracked": False, "position": None},
        },
        "coordinate_transform": {"name": "webxr"},
    }


class StreamProtocolTests(unittest.TestCase):
    def test_roundtrip_ok_with_pose(self) -> None:
        env = StreamEnvelope(
            stream_seq=7,
            pose_generation=3,
            pose_seq=42,
            t_stream_mono_ms=1234.5,
            capture_age_ms=12.0,
            ingress_transport="webrtc",
            quality="ok",
            pose=_sample_pose(),
        )
        raw = encode_stream_envelope(env)
        self.assertTrue(raw.startswith(STREAM_MAGIC))
        self.assertEqual(raw[4], STREAM_VERSION)
        decoded = decode_stream_envelope(raw)
        self.assertEqual(decoded["stream_seq"], 7)
        self.assertEqual(decoded["pose_generation"], 3)
        self.assertEqual(decoded["pose_seq"], 42)
        self.assertEqual(decoded["quality"], "ok")
        self.assertEqual(decoded["ingress_transport"], "webrtc")
        self.assertAlmostEqual(decoded["capture_age_ms"], 12.0)
        pose = decoded["pose"]
        assert pose is not None
        self.assertEqual(pose["hands"]["left"]["landmarks"][0], [0.0, 0.0, 0.0])
        self.assertEqual(pose["hands"]["left"]["landmarks"][5], [5.0, 0.0, 0.0])
        self.assertEqual(pose["elbows"]["left"]["position"], [1.0, 2.0, 3.0])
        self.assertIsNone(pose["elbows"]["right"]["position"])
        self.assertEqual(pose["coordinate_transform"]["name"], "webxr")

    def test_lost_header_only(self) -> None:
        env = StreamEnvelope(
            stream_seq=1,
            pose_generation=0,
            pose_seq=None,
            t_stream_mono_ms=1.0,
            capture_age_ms=None,
            ingress_transport="none",
            quality="lost",
            pose=None,
        )
        raw = encode_stream_envelope(env)
        self.assertEqual(len(raw), 52)
        decoded = decode_stream_envelope(raw)
        self.assertEqual(decoded["quality"], "lost")
        self.assertIsNone(decoded["pose"])
        self.assertIsNone(decoded["capture_age_ms"])
        self.assertIsNone(decoded["pose_seq"])

    def test_reject_bad_magic(self) -> None:
        with self.assertRaises(ValueError):
            decode_stream_envelope(b"XXXX" + b"\x00" * 48)


class UdpStreamTests(unittest.TestCase):
    def test_udp_publisher_sends_qstr(self) -> None:
        bus = StreamBus()
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.bind(("127.0.0.1", 0))
        recv.settimeout(2.0)
        port = recv.getsockname()[1]
        pub = UdpStreamPublisher(bus, host="127.0.0.1", port=port)
        pub.start()
        try:
            env = StreamEnvelope(
                stream_seq=9,
                pose_generation=1,
                pose_seq=1,
                t_stream_mono_ms=10.0,
                capture_age_ms=1.0,
                ingress_transport="webrtc",
                quality="held",
                pose=_sample_pose(),
            )
            bus.publish(env)
            data, _addr = recv.recvfrom(65535)
            self.assertTrue(data.startswith(STREAM_MAGIC))
            decoded = decode_stream_envelope(data)
            self.assertEqual(decoded["stream_seq"], 9)
            self.assertEqual(decoded["quality"], "held")
        finally:
            pub.stop()
            recv.close()


if __name__ == "__main__":
    unittest.main()
