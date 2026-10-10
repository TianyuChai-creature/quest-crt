from __future__ import annotations

import asyncio
import json
import math
import time
import unittest
from copy import deepcopy
from uuid import UUID

from pydantic import ValidationError
from test_binary_protocol import pose_frame_v5

from quest_xr_bridge.pose import CoordinateTransformRequest, PoseFrame, WebRTCOffer


class PoseProtocolTests(unittest.TestCase):
    def test_one_application_exposes_the_core_routes(self) -> None:
        from quest_xr_bridge.runtime import PoseRuntime
        from quest_xr_bridge.server import create_app
        from quest_xr_bridge.video import VideoManager

        app = create_app(PoseRuntime(), VideoManager())
        routes = set()
        for route in app.routes:
            methods = getattr(route, "methods", None)
            if methods:
                routes.update((method, route.path) for method in methods)
            else:
                routes.add(("MOUNT" if route.path == "/static" else "WS", route.path))
        self.assertEqual(
            routes,
            {
                ("GET", "/openapi.json"),
                ("HEAD", "/openapi.json"),
                ("MOUNT", "/static"),
                ("GET", "/"),
                ("GET", "/viewer"),
                ("GET", "/health"),
                ("GET", "/api/video/config"),
                ("PUT", "/api/video/config"),
                ("GET", "/api/coordinate-transform"),
                ("PUT", "/api/coordinate-transform"),
                ("POST", "/api/webrtc/offer"),
                ("POST", "/api/video/offer"),
                ("POST", "/api/video/close"),
                ("WS", "/ws"),
            },
        )

    def test_pose_v5_has_complete_body_frame_and_independent_default_radii(self) -> None:
        frame = PoseFrame.model_validate(pose_frame_v5())
        self.assertEqual(frame.version, 5)
        self.assertEqual(frame.reference_space, "spine-upper-scapula")
        self.assertIsInstance(frame.session_id, UUID)
        self.assertEqual(frame.model_dump(mode="json")["session_id"], pose_frame_v5()["session_id"])
        self.assertEqual(frame.hands.left.wrist_orientation, (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(frame.hands.left.radii, [None] * 21)
        frame.hands.left.radii[0] = 0.01
        self.assertEqual(frame.hands.right.radii, [None] * 21)

    def test_required_pose_v5_fields_cannot_be_omitted(self) -> None:
        for field in ("shoulders", "head", "capture_epoch_ms", "hands", "elbows"):
            frame = pose_frame_v5()
            del frame[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PoseFrame.model_validate(frame)
        frame = pose_frame_v5()
        frame["video_return"] = True
        with self.assertRaises(ValidationError):
            PoseFrame.model_validate(frame)

    def test_points_and_quaternion_require_finite_numbers(self) -> None:
        for value in (math.nan, math.inf, -math.inf, "1", True):
            frame = pose_frame_v5()
            frame["hands"]["left"]["points"][1][0] = value
            with self.subTest(point=value), self.assertRaises(ValidationError):
                PoseFrame.model_validate(frame)
            frame = pose_frame_v5()
            frame["hands"]["left"]["wrist_orientation"][0] = value
            with self.subTest(quaternion=value), self.assertRaises(ValidationError):
                PoseFrame.model_validate(frame)
        frame = pose_frame_v5()
        frame["hands"]["left"]["wrist_orientation"] = [0.0] * 4
        with self.assertRaises(ValidationError):
            PoseFrame.model_validate(frame)

    def test_partial_hand_can_retain_available_positions_and_radii(self) -> None:
        frame = pose_frame_v5()
        hand = frame["hands"]["left"]
        hand["tracked"] = False
        hand["points"][3] = None
        hand["radii"] = [0.008] * 21
        hand["radii"][3] = None
        self.assertFalse(PoseFrame.model_validate(frame).hands.left.tracked)
        for invalid in (True, 1, "false"):
            altered = deepcopy(frame)
            altered["hands"]["left"]["tracked"] = invalid
            with self.subTest(tracked=invalid), self.assertRaises(ValidationError):
                PoseFrame.model_validate(altered)
        for field, value in (("wrist_orientation", None), ("points", [None] * 21)):
            altered = deepcopy(frame)
            altered["hands"]["left"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PoseFrame.model_validate(altered)

    def test_joints_and_head_flags_match_availability(self) -> None:
        self.assertFalse(PoseFrame.model_validate(pose_frame_v5(False)).head.tracked)
        for group in ("shoulders", "elbows"):
            for tracked, position in ((True, None), (False, [1.0, 2.0, 3.0])):
                frame = pose_frame_v5()
                frame[group]["left"] = {"tracked": tracked, "position": position}
                with self.subTest(group=group, tracked=tracked), self.assertRaises(ValidationError):
                    PoseFrame.model_validate(frame)
        for field, value in (
            ("yaw_deg", 181.0),
            ("yaw_deg", math.nan),
            ("pitch_deg", 91.0),
            ("pitch_deg", None),
            ("tracked", False),
        ):
            frame = pose_frame_v5()
            frame["head"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                PoseFrame.model_validate(frame)

    def test_offer_and_coordinate_request_boundaries(self) -> None:
        self.assertEqual(WebRTCOffer(sdp="v=0", type="offer").type, "offer")
        for offer in (
            {"sdp": "", "type": "offer"},
            {"sdp": "v=0", "type": "answer"},
            {"sdp": "v=0", "type": "offer", "extra": 1},
        ):
            with self.subTest(offer=offer), self.assertRaises(ValidationError):
                WebRTCOffer.model_validate(offer)
        self.assertEqual(CoordinateTransformRequest(preset="body").preset, "body")
        self.assertEqual(CoordinateTransformRequest(axes=["x", "-z", "y"]).axes, ["x", "-z", "y"])
        for request in (
            {},
            {"preset": "body", "axes": ["x", "y", "z"]},
            {"axes": ["x", "y"]},
            {"preset": "body", "name": ""},
        ):
            with self.subTest(request=request), self.assertRaises(ValidationError):
                CoordinateTransformRequest.model_validate(request)


class PoseWebRTCIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pose_channel_emits_complete_output_and_isolates_runtimes(self) -> None:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

        from quest_xr_bridge.binary_protocol import encode_pose_packet
        from quest_xr_bridge.runtime import PoseRuntime
        from quest_xr_bridge.server import close_pose_peer, create_app
        from quest_xr_bridge.video import VideoManager

        runtime, isolated = PoseRuntime(), PoseRuntime()
        app, other_app = create_app(runtime, VideoManager()), create_app(isolated, VideoManager())
        self.assertIsNot(app.state.runtime, other_app.state.runtime)
        client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        channel = client.createDataChannel("pose", ordered=False, maxPacketLifeTime=30)
        opened = asyncio.Event()
        channel.on("open", opened.set)
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        output_task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "scheme": "wss",
                    "path": "/ws",
                    "raw_path": b"/ws",
                    "root_path": "",
                    "query_string": b"",
                    "headers": [],
                    "client": ("127.0.0.1", 1),
                    "server": ("localhost", 8000),
                    "subprotocols": [],
                },
                incoming.get,
                outgoing.put,
            )
        )
        processors = []

        async def http(method: str, path: str, body: dict) -> tuple[int, dict]:
            messages = []
            request = asyncio.Queue()
            request.put_nowait(
                {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
            )

            async def send(message):
                messages.append(message)

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "scheme": "https",
                    "method": method,
                    "path": path,
                    "raw_path": path.encode(),
                    "root_path": "",
                    "query_string": b"",
                    "headers": [(b"content-type", b"application/json")],
                    "client": ("127.0.0.1", 1),
                    "server": ("localhost", 8000),
                },
                request.get,
                send,
            )
            return messages[0]["status"], json.loads(b"".join(m.get("body", b"") for m in messages))

        try:
            async with asyncio.timeout(15):
                self.assertEqual((await outgoing.get())["type"], "websocket.accept")
                await client.setLocalDescription(await client.createOffer())
                status, answer = await http(
                    "POST",
                    "/api/webrtc/offer",
                    {
                        "sdp": client.localDescription.sdp,
                        "type": client.localDescription.type,
                    },
                )
                self.assertEqual(status, 200)
                await client.setRemoteDescription(RTCSessionDescription(**answer))
                await opened.wait()
                processors.extend(runtime.processors.values())
                frame = pose_frame_v5()
                frame["capture_epoch_ms"] = time.time_ns() / 1e6
                frame["hands"]["left"]["radii"] = [0.008] + [None] * 20
                channel.send(encode_pose_packet(frame))
                output = json.loads((await outgoing.get())["text"])
                self.assertEqual(output["session_id"], frame["session_id"])
                self.assertEqual(output["seq"], frame["seq"])
                self.assertEqual(output["head"], frame["head"])
                self.assertEqual(output["representation"], "hts-wrist-relative")
                self.assertEqual(output["coordinate_transform"]["name"], "body")
                self.assertEqual(output["ingress_transport"], "webrtc")
                self.assertIn("server_received_epoch_ms", output)
                self.assertIn("estimated_transport_latency_ms", output)
                for side in ("left", "right"):
                    self.assertEqual(len(output["hands"][side]["landmarks"]), 21)
                    self.assertEqual(len(output["hands"][side]["radii"]), 21)
                    self.assertEqual(output["hands"][side]["landmarks"][0], [0.0, 0.0, 0.0])
                    self.assertTrue(output["shoulders"][side]["tracked"])
                self.assertAlmostEqual(output["hands"]["left"]["radii"][0], 0.008)
                self.assertIsNone(output["elbows"]["right"]["position"])
                self.assertIsNone(isolated.latest_pose.snapshot()[1])
                self.assertFalse(isolated.active_source.describe()["active"])

                status, config = await http("PUT", "/api/coordinate-transform", {"preset": "flu"})
                self.assertEqual(status, 200)
                self.assertEqual(config["name"], "flu")
                await asyncio.sleep(0.02)
                self.assertTrue(outgoing.empty())
                frame["seq"] += 1
                channel.send(encode_pose_packet(frame))
                changed = json.loads((await outgoing.get())["text"])
                self.assertEqual(changed["seq"], frame["seq"])
                self.assertEqual(changed["coordinate_transform"]["name"], "flu")
        finally:
            processors.extend(p for p in runtime.processors.values() if p not in processors)
            await asyncio.gather(*(close_pose_peer(runtime, peer) for peer in list(runtime.peers)))
            await client.close()
            for processor in processors:
                processor.close()
                processor.wait_closed(timeout=1)
                self.assertFalse(processor._worker.is_alive())
            incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            try:
                await asyncio.wait_for(output_task, timeout=1)
            finally:
                if not output_task.done():
                    output_task.cancel()
                await asyncio.gather(output_task, return_exceptions=True)
            self.assertFalse(runtime.peers)
            self.assertFalse(runtime.processors)
            self.assertFalse(runtime.active_source.describe()["active"])


if __name__ == "__main__":
    unittest.main()
