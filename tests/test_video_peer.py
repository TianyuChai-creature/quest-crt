import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from quest_xr_bridge.pose import WebRTCOffer
from quest_xr_bridge.runtime import PoseRuntime
from quest_xr_bridge.server import create_app
from quest_xr_bridge.video import VideoManager, VideoUnavailableError


async def post(app, path, body):
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "server": ("test", 443),
            "client": ("test", 1),
        },
        receive,
        send,
    )
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(raw)


class VideoPeerEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = PoseRuntime()
        self.video = Mock(spec=VideoManager)
        self.app = create_app(self.runtime, self.video)

    async def test_offer_passes_nonce_without_changing_the_sdp_answer(self):
        nonce = uuid4()
        answer = {"type": "answer", "sdp": "fixture answer"}
        self.video.offer.return_value = answer
        status, body = await post(
            self.app,
            "/api/video/offer",
            {
                "type": "offer",
                "sdp": "fixture offer",
                "peer_id": str(nonce),
            },
        )
        self.assertEqual((status, body), (200, answer))
        self.video.offer.assert_called_once_with("fixture offer", "offer", peer_id=nonce)
        self.video.close_peer.assert_not_called()

    async def test_close_passes_nonce_and_preserves_pose_runtime(self):
        nonce = uuid4()
        self.video.close_peer.return_value = True
        before = self.runtime.active_source.describe()
        status, body = await post(self.app, "/api/video/close", {"peer_id": str(nonce)})
        self.assertEqual((status, body), (200, {"closed": True}))
        self.video.close_peer.assert_called_once_with(nonce)
        self.video.offer.assert_not_called()
        self.assertEqual(self.runtime.active_source.describe(), before)
        self.assertFalse(self.runtime.peers)

    async def test_invalid_bodies_fail_before_any_backend_operation(self):
        valid_offer = {"sdp": "fixture", "type": "offer", "peer_id": str(uuid4())}
        cases = [
            ("/api/video/offer", {"sdp": "fixture", "type": "offer"}),
            ("/api/video/offer", {**valid_offer, "type": "answer"}),
            ("/api/video/offer", {**valid_offer, "sdp": ""}),
            ("/api/video/offer", {**valid_offer, "sdp": "x" * 1_000_001}),
            ("/api/video/offer", {**valid_offer, "unknown": True}),
            ("/api/video/close", {}),
            ("/api/video/close", {"peer_id": str(uuid4()), "sdp": "unexpected"}),
        ]
        for invalid in (None, True, 1, "invalid", "x" * 1000, []):
            cases.append(("/api/video/offer", {**valid_offer, "peer_id": invalid}))
            cases.append(("/api/video/close", {"peer_id": invalid}))
        for path, body in cases:
            with self.subTest(path=path, fields=list(body)):
                status, _ = await post(self.app, path, body)
                self.assertEqual(status, 422)
        self.video.offer.assert_not_called()
        self.video.close_peer.assert_not_called()
        self.assertEqual(WebRTCOffer(sdp="pose offer", type="offer").type, "offer")

    async def test_unavailable_offer_reports_503_and_idle_close_is_a_noop(self):
        nonce = str(uuid4())
        self.video.offer.side_effect = VideoUnavailableError("video unavailable")
        status, body = await post(
            self.app,
            "/api/video/offer",
            {
                "sdp": "fixture",
                "type": "offer",
                "peer_id": nonce,
            },
        )
        self.assertEqual((status, body), (503, {"detail": "video unavailable"}))
        idle_app = create_app(PoseRuntime(), VideoManager())
        self.assertEqual(
            await post(idle_app, "/api/video/close", {"peer_id": nonce}), (200, {"closed": False})
        )


if __name__ == "__main__":
    unittest.main()
