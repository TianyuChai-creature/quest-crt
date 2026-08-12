"""Phase 1 tests: synthetic SBS source, video signaling, lifecycle.

The negotiation tests are real aiortc clients over httpx ASGITransport —
same loop, host candidates only, no STUN — which is how aiortc's own
suite exercises peer connections.
"""

from __future__ import annotations

import asyncio
import json
import re
import unittest

from unittest.mock import patch

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCRtpReceiver

import server
from quest_crt.transport_session import TransportSessionManager
from quest_crt.video_app import (
    SYNTH_HEIGHT,
    SYNTH_WIDTH,
    SyntheticSbsTrack,
    build_video_app,
)

VIDEO_PORT = 8002


def _session_channels(manager: TransportSessionManager, tsid: str) -> list[str] | None:
    for session in manager.describe()["sessions"]:
        if session["transport_session_id"] == tsid:
            return session["channels"]
    return None


async def _wait_for(predicate, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within timeout")


class SyntheticSbsTrackTests(unittest.TestCase):
    def test_frames_have_sbs_geometry_and_markers_move(self) -> None:
        async def run() -> None:
            track = SyntheticSbsTrack()
            frame0 = await track.recv()
            frame1 = await track.recv()
            self.assertEqual((frame0.width, frame0.height), (SYNTH_WIDTH, SYNTH_HEIGHT))
            self.assertEqual(frame0.format.name, "rgb24")
            self.assertEqual(frame1.pts - frame0.pts, 3000)  # 1/30 s at 90 kHz

            b0 = bytes(frame0.planes[0])
            b1 = bytes(frame1.planes[0])
            self.assertNotEqual(b0, b1)  # the marker must have moved

            def black_at(blob: bytes, x: int, row: int) -> bool:
                offset = row * SYNTH_WIDTH * 3 + x * 3
                return blob[offset] == blob[offset + 1] == blob[offset + 2] == 0

            # frame 1: left marker at cols 2..13, right marker at cols
            # 3..14 (right half offset by SYNTH_WIDTH // 2). The two halves
            # differ at each marker's leading edge only.
            self.assertTrue(black_at(b1, 2, 0))
            self.assertFalse(black_at(b1, 14, 0))
            self.assertTrue(black_at(b1, SYNTH_WIDTH // 2 + 3, 0))
            self.assertFalse(black_at(b1, SYNTH_WIDTH // 2 + 2, 0))

        asyncio.run(run())


class VideoSignalingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = TransportSessionManager()
        self.app = build_video_app(self.manager)
        self.tsid = "video-signaling-tsid"

    async def _post_offer(self, tsid: str, sdp: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://video-signaling"
        ) as client:
            return await client.post(
                "/api/webrtc/video/offer",
                json={"sdp": sdp, "type": "offer", "transport_session_id": tsid},
            )

    async def _browser_pc(
        self, h264: bool = True
    ) -> tuple[RTCPeerConnection, object, object]:
        browser = RTCPeerConnection()
        transceiver = browser.addTransceiver("video", direction="recvonly")
        if h264:
            # Mirror the Quest page: setCodecPreferences([h264]) is the only
            # lever that makes the offer (and therefore the answer) H.264 —
            # aiortc's server-side codecPreferences is frozen at
            # setRemoteDescription (docs/stage1-revise.md §1.1 row 2).
            caps = RTCRtpReceiver.getCapabilities("video").codecs
            h264_caps = [c for c in caps if "H264" in c.mimeType.upper()]
            transceiver.setCodecPreferences(h264_caps)
        control = browser.createDataChannel("video-control")
        offer = await browser.createOffer()
        await browser.setLocalDescription(offer)
        return browser, control, offer

    async def _connect(self, browser: RTCPeerConnection, answer: dict[str, str]) -> None:
        connected = asyncio.Event()

        @browser.on("connectionstatechange")
        def _on_state() -> None:
            if browser.connectionState == "connected":
                connected.set()

        await browser.setRemoteDescription(RTCSessionDescription(**answer))
        await asyncio.wait_for(connected.wait(), 15)

    async def test_invalid_transport_session_id_rejected(self) -> None:
        browser, _control, offer = await self._browser_pc()
        try:
            response = await self._post_offer("bad id/with/slashes", offer.sdp)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.app.registry.peers, 0)
            self.assertIsNone(_session_channels(self.manager, self.tsid))
        finally:
            await browser.close()

    async def test_recvonly_offer_answered_sendonly_with_sctp(self) -> None:
        browser, _control, offer = await self._browser_pc()
        try:
            response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 200)
            answer = response.json()
            self.assertIn("a=sendonly", answer["sdp"])
            self.assertIn("m=application", answer["sdp"])
            self.assertIn("webrtc-datachannel", answer["sdp"])

            # Lease attached at offer time; registry owns the peer.
            self.assertEqual(self.app.registry.peers, 1)
            self.assertEqual(_session_channels(self.manager, self.tsid), ["video"])

            await self._connect(browser, answer)

            # Browser close -> server-side connection state -> lease release.
            await browser.close()
            await _wait_for(lambda: self.app.registry.peers == 0)
            self.assertEqual(_session_channels(self.manager, self.tsid), [])
        finally:
            await browser.close()

    async def test_browser_receives_synthetic_track(self) -> None:
        browser, _control, offer = await self._browser_pc()
        got_frame = asyncio.Event()
        received: dict[str, int] = {}

        @browser.on("track")
        async def on_track(track: object) -> None:
            frame = await track.recv()  # type: ignore[attr-defined]
            received["width"] = frame.width
            received["height"] = frame.height
            got_frame.set()

        try:
            response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 200)
            await self._connect(browser, response.json())
            await asyncio.wait_for(got_frame.wait(), 20)
            self.assertEqual(received, {"width": SYNTH_WIDTH, "height": SYNTH_HEIGHT})
        finally:
            await browser.close()

    async def test_video_control_ping_pong(self) -> None:
        browser, control, offer = await self._browser_pc()
        pong = asyncio.Event()
        pong_data: dict[str, object] = {}
        opened = asyncio.Event()

        @control.on("open")
        def _on_open() -> None:
            opened.set()

        @control.on("message")
        def _on_message(message: str) -> None:
            pong_data.update(json.loads(message))
            pong.set()

        try:
            response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 200)
            await self._connect(browser, response.json())
            await asyncio.wait_for(opened.wait(), 15)
            control.send(json.dumps({"type": "ping", "sent_at": 12345}))
            await asyncio.wait_for(pong.wait(), 15)
            self.assertEqual(pong_data["type"], "pong")
            self.assertEqual(pong_data["ping_sent_at"], 12345)
            self.assertIsInstance(pong_data["received_at"], int)
        finally:
            await browser.close()

    async def test_negotiated_codec_is_h264_recorded(self) -> None:
        """Phase 2 gate: the answer must negotiate H.264 (not VP8), and the
        registry must record mimeType/pt/profile-level-id/packetization-mode.
        """
        browser, _control, offer = await self._browser_pc(h264=True)
        try:
            response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 200)
            answer = response.json()
            # H.264 must be the first non-rtx payload of the video m-line
            # (the answer mirrors the offer's codec order).
            rtpmaps = re.findall(r"^a=rtpmap:(\d+) ([\w/]+)\r?$", answer["sdp"], re.M)
            non_rtx = [mime for _pt, mime in rtpmaps if not mime.startswith("rtx")]
            # Both H.264 variants (42001f / 42e01f) come first; VP8 is gone.
            self.assertEqual(non_rtx, ["H264/90000", "H264/90000"])
            codecs = self.app.registry.describe()["negotiated_codecs"]
            self.assertEqual(len(codecs), 1)
            negotiated = codecs[0]["negotiated_codec"]
            self.assertEqual(negotiated["mimeType"], "H264")
            self.assertEqual(negotiated["clockRate"], 90000)
            self.assertEqual(negotiated["packetizationMode"], "1")
            self.assertIn(
                negotiated["profileLevelId"], ("42001f", "42e01f")
            )
        finally:
            await browser.close()

    async def test_malformed_sdp_releases_lease(self) -> None:
        response = await self._post_offer(self.tsid, "this is not an sdp")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app.registry.peers, 0)
        # Lease was attached then released; the empty session entry lingers.
        self.assertEqual(_session_channels(self.manager, self.tsid), [])

    async def test_set_remote_description_failure_releases_lease(self) -> None:
        browser, _control, offer = await self._browser_pc()
        original = RTCPeerConnection.setRemoteDescription

        async def failing_offer(self: object, description: object) -> None:
            if description.type == "offer":  # type: ignore[attr-defined]
                raise ValueError("simulated setRemoteDescription failure")
            return await original(self, description)  # type: ignore[arg-type]

        try:
            with patch.object(
                RTCPeerConnection, "setRemoteDescription", failing_offer
            ):
                response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.app.registry.peers, 0)
            self.assertEqual(_session_channels(self.manager, self.tsid), [])
        finally:
            await browser.close()

    async def test_create_answer_failure_releases_lease(self) -> None:
        browser, _control, offer = await self._browser_pc()

        async def failing_answer(self: object) -> None:
            raise ValueError("simulated createAnswer failure")

        try:
            with patch.object(
                RTCPeerConnection, "createAnswer", failing_answer
            ):
                response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.app.registry.peers, 0)
            self.assertEqual(_session_channels(self.manager, self.tsid), [])
        finally:
            await browser.close()

    async def test_negotiation_timeout_releases_lease(self) -> None:
        """A client that offers but never connects must not pin the channel:
        the negotiation guard closes the peer after the timeout."""
        manager = TransportSessionManager()
        app = build_video_app(manager, negotiation_timeout=0.3)
        browser, _control, offer = await self._browser_pc()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://video-signaling"
            ) as client:
                response = await client.post(
                    "/api/webrtc/video/offer",
                    json={
                        "sdp": offer.sdp,
                        "type": "offer",
                        "transport_session_id": self.tsid,
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(app.registry.peers, 1)
            self.assertEqual(_session_channels(manager, self.tsid), ["video"])
            await browser.close()  # never answer; disappear mid-negotiation
            await _wait_for(lambda: app.registry.peers == 0)
            self.assertEqual(_session_channels(manager, self.tsid), [])
        finally:
            await browser.close()

    async def test_superseded_video_peer_close_keeps_new_lease(self) -> None:
        """Old video PC's delayed close must not evict the replacement:
        token/generation semantics on the video channel (Phase 0 design)."""
        first, _c1, offer1 = await self._browser_pc()
        response1 = await self._post_offer(self.tsid, offer1.sdp)
        self.assertEqual(response1.status_code, 200)
        await self._connect(first, response1.json())

        second, _c2, offer2 = await self._browser_pc()
        response2 = await self._post_offer(self.tsid, offer2.sdp)
        self.assertEqual(response2.status_code, 200)
        await self._connect(second, response2.json())

        try:
            self.assertEqual(self.app.registry.peers, 2)
            for session in self.manager.describe()["sessions"]:
                if session["transport_session_id"] == self.tsid:
                    self.assertEqual(session["channels"], ["video"])
                    self.assertEqual(session["generations"]["video"], 2)

            # Superseded connection's close removes only its own token.
            await first.close()
            await _wait_for(lambda: self.app.registry.peers == 1)
            self.assertEqual(_session_channels(self.manager, self.tsid), ["video"])

            await second.close()
            await _wait_for(lambda: self.app.registry.peers == 0)
            self.assertEqual(_session_channels(self.manager, self.tsid), [])
        finally:
            await first.close()
            await second.close()

    async def test_video_disconnect_endpoint_closes_peer_and_releases_lease(self) -> None:
        """Diagnostic lever: POST /api/webrtc/video/disconnect closes the
        video peers of a tsid and releases the lease (the page then
        reconnects on its own)."""
        browser, _control, offer = await self._browser_pc()
        try:
            response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 200)
            await self._connect(browser, response.json())
            self.assertEqual(self.app.registry.peers, 1)

            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://video-signaling"
            ) as client:
                response = await client.post(
                    "/api/webrtc/video/disconnect",
                    json={"transport_session_id": self.tsid},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"closed": 1})
            await _wait_for(lambda: self.app.registry.peers == 0)
            self.assertEqual(_session_channels(self.manager, self.tsid), [])
        finally:
            await browser.close()

    async def test_unknown_datachannel_label_is_closed(self) -> None:
        browser = RTCPeerConnection()
        browser.addTransceiver("video", direction="recvonly")
        wrong = browser.createDataChannel("pose")  # not video-control
        closed = asyncio.Event()

        @wrong.on("close")
        def _on_close() -> None:
            closed.set()

        try:
            offer = await browser.createOffer()
            await browser.setLocalDescription(offer)
            response = await self._post_offer(self.tsid, offer.sdp)
            self.assertEqual(response.status_code, 200)
            await self._connect(browser, response.json())
            await asyncio.wait_for(closed.wait(), 15)
        finally:
            await browser.close()


class PoseVideoCoexistenceTests(unittest.IsolatedAsyncioTestCase):
    """Phase 1 acceptance: two PCs share one transport_session and each
    channel tears down independently (docs/stage1-revise.md §4 Phase 1).

    Uses the production wiring: server.app for pose, server.video_app for
    video, both bound to the shared transport_sessions singleton.
    """

    async def asyncSetUp(self) -> None:
        self._log_enabled = server.POSE_LOG_ENABLED
        server.POSE_LOG_ENABLED = False
        self.tsid = "coexist-tsid-1"

    async def asyncTearDown(self) -> None:
        server.POSE_LOG_ENABLED = self._log_enabled

    async def _pose_pc(self) -> RTCPeerConnection:
        pose = RTCPeerConnection()
        pose.createDataChannel("pose")
        offer = await pose.createOffer()
        await pose.setLocalDescription(offer)
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://pose-signaling"
        ) as client:
            response = await client.post(
                "/api/webrtc/offer",
                json={
                    "sdp": offer.sdp,
                    "type": "offer",
                    "transport_session_id": self.tsid,
                },
            )
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        await pose.setRemoteDescription(RTCSessionDescription(**answer))
        return pose

    async def _video_pc(self) -> RTCPeerConnection:
        video = RTCPeerConnection()
        video.addTransceiver("video", direction="recvonly")
        video.createDataChannel("video-control")
        offer = await video.createOffer()
        await video.setLocalDescription(offer)
        transport = httpx.ASGITransport(app=server.video_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://video-signaling"
        ) as client:
            response = await client.post(
                "/api/webrtc/video/offer",
                json={
                    "sdp": offer.sdp,
                    "type": "offer",
                    "transport_session_id": self.tsid,
                },
            )
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        await video.setRemoteDescription(RTCSessionDescription(**answer))
        return video

    async def _wait_connected(self, pc: RTCPeerConnection) -> None:
        connected = asyncio.Event()

        @pc.on("connectionstatechange")
        def _on_state() -> None:
            if pc.connectionState == "connected":
                connected.set()

        await asyncio.wait_for(connected.wait(), 15)

    async def test_pose_disconnect_closes_pose_only_video_intact(self) -> None:
        """Diagnostic lever: POST /api/webrtc/pose/disconnect closes the
        pose peers of a tsid; the video channel stays untouched."""
        pose = await self._pose_pc()
        try:
            await self._wait_connected(pose)
            await _wait_for(
                lambda: _session_channels(server.transport_sessions, self.tsid)
                == ["pose"]
            )

            video = await self._video_pc()
            try:
                await self._wait_connected(video)
                await _wait_for(
                    lambda: set(
                        _session_channels(server.transport_sessions, self.tsid) or []
                    )
                    == {"pose", "video"}
                )
                self.assertTrue(server.active_pose_source.describe()["active"])

                transport = httpx.ASGITransport(app=server.app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://pose-signaling"
                ) as client:
                    response = await client.post(
                        "/api/webrtc/pose/disconnect",
                        json={"transport_session_id": self.tsid},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"closed": 1})

                await _wait_for(
                    lambda: _session_channels(server.transport_sessions, self.tsid)
                    == ["video"]
                )
                await _wait_for(
                    lambda: not server.active_pose_source.describe()["active"]
                )
                # The video peer survived the pose drop.
                self.assertEqual(server.video_app.registry.peers, 1)
            finally:
                await video.close()
        finally:
            await pose.close()

    async def test_video_disconnect_closes_video_only_pose_intact(self) -> None:
        """Diagnostic lever: POST /api/webrtc/video/disconnect closes the
        video peers of a tsid; the pose channel stays untouched."""
        pose = await self._pose_pc()
        try:
            await self._wait_connected(pose)
            await _wait_for(
                lambda: _session_channels(server.transport_sessions, self.tsid)
                == ["pose"]
            )

            video = await self._video_pc()
            try:
                await self._wait_connected(video)
                await _wait_for(
                    lambda: set(
                        _session_channels(server.transport_sessions, self.tsid) or []
                    )
                    == {"pose", "video"}
                )

                transport = httpx.ASGITransport(app=server.video_app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://video-signaling"
                ) as client:
                    response = await client.post(
                        "/api/webrtc/video/disconnect",
                        json={"transport_session_id": self.tsid},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"closed": 1})

                await _wait_for(
                    lambda: _session_channels(server.transport_sessions, self.tsid)
                    == ["pose"]
                )
                await _wait_for(lambda: server.video_app.registry.peers == 0)
                # The pose processor is untouched.
                self.assertTrue(server.active_pose_source.describe()["active"])
            finally:
                await video.close()
        finally:
            await pose.close()

    async def test_pose_and_video_coexist_and_tear_down_independently(self) -> None:
        pose = await self._pose_pc()
        try:
            await self._wait_connected(pose)
            # Pose lease attaches when the server sees the "pose" DataChannel.
            await _wait_for(
                lambda: _session_channels(server.transport_sessions, self.tsid)
                == ["pose"]
            )

            video = await self._video_pc()
            try:
                await self._wait_connected(video)
                await _wait_for(
                    lambda: set(
                        _session_channels(server.transport_sessions, self.tsid) or []
                    )
                    == {"pose", "video"}
                )

                # Video down: pose stays untouched.
                await video.close()
                await _wait_for(
                    lambda: _session_channels(server.transport_sessions, self.tsid)
                    == ["pose"]
                )
            finally:
                await video.close()

            # Pose down: its own processor and lease go away.
            await pose.close()
            await _wait_for(
                lambda: _session_channels(server.transport_sessions, self.tsid) is None
            )
        finally:
            await pose.close()


if __name__ == "__main__":
    unittest.main()
