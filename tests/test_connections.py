import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from aiortc import RTCConfiguration, RTCPeerConnection
from test_binary_protocol import pose_frame_v5

from quest_xr_bridge.binary_protocol import decode_pose_packet, encode_pose_packet
from quest_xr_bridge.pose import WebRTCOffer, WebRTCVideoOffer
from quest_xr_bridge.runtime import PoseRuntime
from quest_xr_bridge.server import create_app
from quest_xr_bridge.video import VideoManager


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfinished_pose_handshake_releases_the_source_slot(self):
        runtime = PoseRuntime()
        app = create_app(runtime, VideoManager())
        route = next(r.endpoint for r in app.routes if r.path == "/api/webrtc/offer")
        client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        client.createDataChannel("pose", ordered=False, maxPacketLifeTime=30)
        try:
            await client.setLocalDescription(await client.createOffer())
            # Keep the initial ICE checks out of this accelerated expiry test.
            with patch("quest_xr_bridge.server.POSE_HANDSHAKE_TIMEOUT", 1):
                answer = await route(
                    WebRTCOffer(sdp=client.localDescription.sdp, type="offer"),
                    SimpleNamespace(client=None),
                )
            self.assertEqual(answer["type"], "answer")
            await asyncio.sleep(1.2)
            self.assertFalse(runtime.peers)
            self.assertFalse(runtime.peer_timeouts)
            self.assertFalse(runtime.active_source.describe()["active"])
        finally:
            await client.close()

    async def test_slow_output_and_video_signalling_do_not_block_other_output(self):
        runtime, video = PoseRuntime(), VideoManager()
        app = create_app(runtime, video)
        queues = [asyncio.Queue(), asyncio.Queue()]
        sent = [[], []]
        slow_entered = asyncio.Event()
        video_entered, release_video = threading.Event(), threading.Event()

        async def output(index):
            await queues[index].put({"type": "websocket.connect"})

            async def send(message):
                if index == 0 and message["type"] == "websocket.send":
                    slow_entered.set()
                    await asyncio.Event().wait()
                sent[index].append(message)
                if index == 0 and message["type"] == "websocket.close":
                    await asyncio.Event().wait()

            await app(
                {
                    "type": "websocket",
                    "path": "/ws",
                    "headers": [],
                    "query_string": b"",
                    "scheme": "wss",
                    "client": ("127.0.0.1", index),
                },
                queues[index].get,
                send,
            )

        def stalled_offer(*_, **kwargs):
            self.assertEqual(kwargs["peer_id"], peer_id)
            video_entered.set()
            release_video.wait(2)
            return {"type": "answer", "sdp": "test answer"}

        tasks = [asyncio.create_task(output(index)) for index in (0, 1)]
        offer_route = next(r.endpoint for r in app.routes if r.path == "/api/video/offer")
        peer_id = uuid4()
        video_task = None
        try:
            await asyncio.sleep(0.01)
            with patch.object(video, "offer", stalled_offer):
                video_task = asyncio.create_task(
                    offer_route(WebRTCVideoOffer(sdp="test", type="offer", peer_id=peer_id))
                )
                self.assertTrue(await asyncio.to_thread(video_entered.wait, 1))
                raw = decode_pose_packet(encode_pose_packet(pose_frame_v5()))
                runtime.latest_pose.publish(raw)
                await asyncio.wait_for(slow_entered.wait(), 1)
                raw = {**raw, "seq": raw["seq"] + 1}
                runtime.latest_pose.publish(raw)

                async def await_latest():
                    while not any(
                        m["type"] == "websocket.send" and json.loads(m["text"])["seq"] == raw["seq"]
                        for m in sent[1]
                    ):
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(await_latest(), 1)
                self.assertFalse(video_task.done())
                await asyncio.wait_for(tasks[0], 1)
                self.assertTrue(
                    any(m["type"] == "websocket.close" and m["code"] == 1013 for m in sent[0])
                )
                release_video.set()
                await asyncio.wait_for(video_task, 1)
        finally:
            release_video.set()
            await queues[1].put({"type": "websocket.disconnect", "code": 1000})
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *tasks, *([video_task] if video_task else []), return_exceptions=True
            )
