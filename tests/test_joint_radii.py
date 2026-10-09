from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import queue
import socket
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import WebSocketDisconnect
from pydantic import ValidationError

import server
from quest_crt import COORDINATE_PRESETS, StreamBus, decode_stream_envelope, encode_stream_envelope
from quest_crt.binary_protocol import decode_pose_packet, encode_pose_packet
from quest_crt.stable_stream import StreamEnvelope
from quest_crt.stream_udp import UdpStreamPublisher
from test_binary_protocol import pose_frame_v5


class JointRadiiTests(unittest.TestCase):
    def frame(self):
        frame = pose_frame_v5()
        frame["hands"]["left"]["radii"] = [0.008, None, 0.0] + [0.01] * 18
        frame["hands"]["right"]["radii"] = [None] * 21
        return frame

    def output(self):
        validated = server.PoseFrame.model_validate(self.frame()).model_dump(mode="json")
        return server.make_output_pose(validated, "flu", COORDINATE_PRESETS["flu"])

    def envelope(self):
        return StreamEnvelope(1, 1, 1, 10.0, 0.0, "webrtc", "ok", self.output())

    def test_radii_survive_ingress_transform_and_output(self):
        packet = encode_pose_packet(self.frame())
        self.assertEqual(len(packet), 804)
        self.assertEqual(packet[4], 5)
        decoded = decode_pose_packet(packet)
        validated = server.PoseFrame.model_validate(decoded).model_dump(mode="json")
        output = server.make_output_pose(validated, "flu", COORDINATE_PRESETS["flu"])
        self.assertAlmostEqual(output["hands"]["left"]["radii"][0], 0.008)
        self.assertEqual(output["hands"]["left"]["radii"][1:3], [None, 0.0])
        self.assertEqual(output["hands"]["right"]["radii"], [None] * 21)
        self.assertEqual(output["head"], decoded["head"])

    def test_legacy_ingress_and_qstr_bytes_stay_compatible(self):
        self.assertEqual(len(encode_pose_packet(pose_frame_v5())), 636)
        output = server.make_output_pose(pose_frame_v5(), "body", COORDINATE_PRESETS["body"])
        self.assertEqual(output["hands"]["left"]["radii"], [None] * 21)
        env = self.envelope().to_dict()
        with_radii = encode_stream_envelope(env)
        for hand in env["pose"]["hands"].values():
            hand.pop("radii")
        self.assertEqual(with_radii, encode_stream_envelope(env))
        self.assertNotIn("radii", decode_stream_envelope(with_radii)["pose"]["hands"]["left"])

    def test_qstr_v2_roundtrip_and_invalid_radii(self):
        packet = encode_stream_envelope(self.envelope(), version=2)
        self.assertEqual(len(packet), len(encode_stream_envelope(self.envelope())) + 168)
        radii = decode_stream_envelope(packet)["pose"]["hands"]["left"]["radii"]
        self.assertAlmostEqual(radii[0], 0.008)
        self.assertEqual(radii[1:3], [None, 0.0])
        with self.assertRaises(ValueError):
            decode_stream_envelope(packet[:-1])
        corrupt = bytearray(packet)
        struct.pack_into("<f", corrupt, len(packet) - 168, -0.1)
        with self.assertRaises(ValueError):
            decode_stream_envelope(corrupt)
        env = self.envelope().to_dict()
        env.update(quality="lost", pose=None)
        self.assertIsNone(decode_stream_envelope(encode_stream_envelope(env, version=2))["pose"])

    def test_json_and_binary_reject_invalid_radius_values(self):
        for radii in ([0.01] * 20, [-0.01] * 21, [float("inf")] * 21, [float("nan")] * 21):
            frame = self.frame()
            frame["hands"]["left"]["radii"] = radii
            with self.assertRaises(ValidationError):
                server.PoseFrame.model_validate(frame)
            with self.assertRaises(ValueError):
                encode_pose_packet(frame)
        frame = self.frame()
        frame["hands"]["left"]["tracked"] = False
        frame["hands"]["left"]["points"][3] = None
        with self.assertRaises(ValidationError):
            server.PoseFrame.model_validate(frame)
        corrupt = bytearray(encode_pose_packet(self.frame()))
        struct.pack_into("<f", corrupt, 636, -0.1)
        with self.assertRaises(ValueError):
            decode_pose_packet(corrupt)

    def test_both_browser_encoders_match_python_decoder(self):
        output = subprocess.check_output(
            ["node", str(Path(__file__).with_suffix(".cjs")), "--packets"], text=True
        )
        for packet in json.loads(output):
            frame = decode_pose_packet(base64.b64decode(packet))
            server.PoseFrame.model_validate(frame)
            self.assertAlmostEqual(frame["hands"]["left"]["radii"][8], 0.008)
            self.assertEqual(frame["hands"]["right"]["radii"], [None] * 21)

    def test_udp_v2_transmits_radii(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(2)
            bus = StreamBus()
            publisher = UdpStreamPublisher(bus, port=receiver.getsockname()[1], version=2)
            publisher.start()
            try:
                bus.publish(self.envelope())
                packet, _ = receiver.recvfrom(65535)
                radii = decode_stream_envelope(packet)["pose"]["hands"]["left"]["radii"]
                self.assertAlmostEqual(radii[0], 0.008)
                self.assertEqual(radii[1:3], [None, 0.0])
            finally:
                publisher.stop()

    def test_cli_resolves_source_and_wheel_layouts(self):
        from quest_crt import __main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "quest_crt"
            package.mkdir()
            with patch.object(cli, "__file__", str(package / "__main__.py")), patch.object(cli, "run_path") as run:
                cli.main()
                run.assert_called_with(str(Path(directory) / "server.py"), run_name="__main__")
                (package / "server.py").touch()
                cli.main()
                run.assert_called_with(str(package / "server.py"), run_name="__main__")

    def test_websocket_version_selection_and_json_output(self):
        envelope = self.envelope()
        for params in ({}, {"version": "2"}, {"format": "json"}, {"version": "invalid"}):
            messages = []
            class WebSocket:
                client = None
                query_params = params
                async def accept(self):
                    pass
                async def close(self, **kwargs):
                    messages.append(kwargs)
                async def send_bytes(self, value):
                    messages.append(value)
                    raise WebSocketDisconnect()
                async def send_text(self, value):
                    messages.append(json.loads(value))
                    raise WebSocketDisconnect()
            q = queue.Queue()
            q.put(envelope)
            with patch.object(server.stream_clock.bus, "subscribe", return_value=q), patch.object(server.stream_clock.bus, "unsubscribe"):
                asyncio.run(server.stable_stream_websocket(WebSocket()))
            if params.get("format") == "json":
                self.assertEqual(messages[0]["pose"]["hands"]["left"]["radii"][0], 0.008)
            elif params.get("version") == "invalid":
                self.assertEqual(messages[0]["code"], 1008)
            else:
                self.assertEqual(messages[0][4], int(params.get("version", "1")))


if __name__ == "__main__":
    unittest.main()
