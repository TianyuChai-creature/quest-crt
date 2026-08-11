"""Phase 1: video signaling app (:8002) with a synthetic SBS source.

Browser is the offerer: a recvonly video transceiver plus a
``video-control`` DataChannel created before ``createOffer`` (so the SCTP
m-line is inside the offer). We answer **sendonly** — against aiortc 1.15.0
an answer to a recvonly offer only comes out ``sendonly`` when a local
track is attached before ``setRemoteDescription``; without one the answer
is directionless/inactive (see ``docs/stage1-revise.md`` §1).

No ZED / NVENC yet: the source is a procedurally generated SBS test
pattern, produced with plain ``bytearray`` (no numpy dependency).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import av
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from quest_crt.transport_session import (
    ChannelLease,
    TransportSessionError,
    TransportSessionManager,
    validate_transport_session_id,
)

HOST = os.environ.get("POSE_HOST", "0.0.0.0")
VIDEO_PORT = int(os.environ.get("VIDEO_PORT", "8002"))

# Synthetic source geometry: SBS, two equal halves (mirrors the eventual
# ZED 2560x720 layout at a CPU-friendly smoke size).
SYNTH_WIDTH = int(os.environ.get("VIDEO_SYNTH_WIDTH", "1280"))
SYNTH_HEIGHT = int(os.environ.get("VIDEO_SYNTH_HEIGHT", "360"))

# aiortc's VideoStreamTrack paces at a fixed VIDEO_PTIME of 1/30 s; the
# synthetic source is therefore 30 fps, matching the Phase 2 smoke target.
SYNTH_FPS = 30

_VIDEO_CONTROL_LABEL = "video-control"

# SMPTE-ish color bars, one per half.
_BAR_PALETTE = (
    (255, 255, 255),  # white
    (255, 255, 0),    # yellow
    (0, 255, 255),    # cyan
    (0, 255, 0),      # green
    (255, 0, 255),    # magenta
    (255, 0, 0),      # red
    (0, 0, 255),      # blue
)
# Black marker: the first bar is white, so a white marker would be
# invisible while it sweeps through it.
_MARKER = (0, 0, 0)
_MARKER_BYTES = bytes(_MARKER)
_MARKER_PX = 12


class SyntheticSbsTrack(VideoStreamTrack):
    """Procedural SBS test pattern, no external dependencies.

    Both halves show the same color bars; a white marker column sweeps
    across each half at a different speed so the SBS split is visibly
    alive (a decoder-side half mix-up would show the sweeps misaligned).
    """

    def __init__(self, width: int = SYNTH_WIDTH, height: int = SYNTH_HEIGHT) -> None:
        super().__init__()
        self._width = width
        self._height = height
        self._half = width // 2
        self._row_stride = width * 3
        self._half_stride = self._half * 3
        self._bars = self._build_bars()
        self._frame_index = 0

    def _build_bars(self) -> bytes:
        bar_width = self._half // len(_BAR_PALETTE)
        row = bytearray(self._half * 3)
        for index, (red, green, blue) in enumerate(_BAR_PALETTE):
            start = index * bar_width * 3
            row[start : start + bar_width * 3] = bytes((red, green, blue)) * bar_width
        return bytes(row)

    @staticmethod
    def _sweep(half: bytes, marker_col: int) -> bytes:
        """One half-row with the marker column spliced at marker_col."""
        if marker_col == 0:
            return _MARKER_BYTES * _MARKER_PX + half[_MARKER_PX * 3 :]
        return (
            half[: marker_col * 3]
            + _MARKER_BYTES * _MARKER_PX
            + half[(marker_col + _MARKER_PX) * 3 :]
        )

    async def recv(self) -> Any:
        pts, time_base = await self.next_timestamp()

        # Left marker sweeps at 2 px/frame, right at 3 px/frame.
        span = self._half - _MARKER_PX
        left_col = (self._frame_index * 2) % span
        right_col = (self._frame_index * 3) % span
        left_row = self._sweep(self._bars, left_col)
        right_row = self._sweep(self._bars, right_col)
        self._frame_index += 1

        buffer = bytearray(self._width * self._height * 3)
        for y in range(self._height):
            offset = y * self._row_stride
            buffer[offset : offset + self._half_stride] = left_row
            buffer[offset + self._half_stride : offset + self._row_stride] = right_row

        frame = av.VideoFrame(width=self._width, height=self._height, format="rgb24")
        frame.planes[0].update(bytes(buffer))
        frame.pts = pts
        frame.time_base = time_base
        return frame


class VideoWebRTCOffer(BaseModel):
    sdp: str = Field(min_length=1)
    type: Literal["offer"]
    transport_session_id: str = Field(min_length=1, max_length=64)


class VideoPeerRegistry:
    """Owns the video PeerConnections and their transport-session leases.

    Single event loop only (the video app's loop), so no locking.
    """

    def __init__(self, transport_sessions: TransportSessionManager) -> None:
        self._transport_sessions = transport_sessions
        self._peers: set[RTCPeerConnection] = set()
        self._by_tsid: dict[str, set[RTCPeerConnection]] = {}
        self._leases: dict[RTCPeerConnection, ChannelLease] = {}
        self._clients: dict[RTCPeerConnection, str] = {}
        self._negotiated: dict[RTCPeerConnection, dict[str, Any]] = {}
        self._guards: dict[RTCPeerConnection, asyncio.Task[None]] = {}

    @property
    def peers(self) -> int:
        return len(self._peers)

    def register(
        self,
        peer: RTCPeerConnection,
        transport_session_id: str,
        lease: ChannelLease,
        client_name: str,
    ) -> None:
        self._peers.add(peer)
        self._by_tsid.setdefault(transport_session_id, set()).add(peer)
        self._leases[peer] = lease
        self._clients[peer] = client_name

    def set_guard(self, peer: RTCPeerConnection, guard: asyncio.Task[None]) -> None:
        """Attach the negotiation-timeout guard (cancelled on connect/close)."""
        self._guards[peer] = guard

    def cancel_guard(self, peer: RTCPeerConnection) -> None:
        guard = self._guards.pop(peer, None)
        if guard is not None and not guard.done():
            guard.cancel()

    def set_negotiated_codec(
        self, peer: RTCPeerConnection, codec: dict[str, Any] | None
    ) -> None:
        self._negotiated[peer] = codec

    async def close(self, peer: RTCPeerConnection) -> None:
        """Release one video peer: lease token, registry entries, close."""
        if peer not in self._peers:
            return
        self._peers.discard(peer)
        lease = self._leases.pop(peer, None)
        self._clients.pop(peer, None)
        self._negotiated.pop(peer, None)
        self.cancel_guard(peer)
        for tsid, peers in list(self._by_tsid.items()):
            peers.discard(peer)
            if not peers:
                self._by_tsid.pop(tsid, None)
        if lease is not None:
            self._transport_sessions.end_channel(lease)
        if peer.connectionState != "closed":
            await peer.close()

    async def close_all(self) -> None:
        for peer in list(self._peers):
            await self.close(peer)

    def describe(self) -> dict[str, Any]:
        codecs = []
        for peer in sorted(self._peers, key=repr):
            client = self._clients.get(peer, "unknown")
            tsid = next(
                (
                    tsid
                    for tsid, peers in self._by_tsid.items()
                    if peer in peers
                ),
                None,
            )
            codecs.append(
                {
                    "transport_session_id": tsid,
                    "client": client,
                    "negotiated_codec": self._negotiated.get(peer),
                }
            )
        return {
            "peers": self.peers,
            "sessions": {
                tsid: [self._clients[peer] for peer in peers]
                for tsid, peers in sorted(self._by_tsid.items())
            },
            "negotiated_codecs": codecs,
        }


def parse_negotiated_video_codec(sdp: str) -> dict[str, Any] | None:
    """First non-rtx payload of the video m-line, with rtpmap/fmtp.

    The first non-rtx payload of the answer's video m-line is the codec the
    offerer (Quest browser) will actually use — it mirrors the offer's
    codec order, which the browser controls via ``setCodecPreferences``.
    Phase 2 gate: this must come out as H.264.
    """
    in_video = False
    rtpmap: dict[str, str] = {}
    fmtp: dict[str, str] = {}
    for raw in sdp.splitlines():
        line = raw.strip()
        if line.startswith("m="):
            if in_video:
                break
            in_video = line.startswith("m=video")
            continue
        if not in_video:
            continue
        if line.startswith("a=rtpmap:"):
            pt, spec = line[len("a=rtpmap:") :].split(" ", 1)
            rtpmap[pt] = spec
        elif line.startswith("a=fmtp:"):
            pt, spec = line[len("a=fmtp:") :].split(" ", 1)
            fmtp[pt] = spec
    for pt in rtpmap:
        spec = rtpmap[pt]
        if spec.startswith("rtx"):
            continue
        mime, clock = spec.split("/", 1)
        info: dict[str, Any] = {
            "payloadType": int(pt),
            "mimeType": mime,
            "clockRate": int(clock),
        }
        if pt in fmtp:
            info["sdpFmtpLine"] = fmtp[pt]
            params = dict(
                part.split("=", 1) for part in fmtp[pt].split(";") if "=" in part
            )
            info["profileLevelId"] = params.get("profile-level-id")
            info["packetizationMode"] = params.get("packetization-mode")
        return info
    return None


def build_video_app(
    transport_sessions: TransportSessionManager,
    negotiation_timeout: float = 20.0,
) -> FastAPI:
    """Create the :8002 signaling app (one app per event loop).

    ``negotiation_timeout`` bounds how long an offered-but-never-connected
    peer (and its lease) is kept before forced cleanup — a client that
    disappears mid-negotiation would otherwise pin the "video" channel
    forever, since ICE state transitions never fire.
    """
    registry = VideoPeerRegistry(transport_sessions)

    @asynccontextmanager
    async def video_lifespan(_: FastAPI):
        yield
        await registry.close_all()

    video_app = FastAPI(
        title="Quest CRT Video Signaling",
        docs_url=None,
        redoc_url=None,
        lifespan=video_lifespan,
    )
    # The Quest page (:POSE_PORT) and viewer (:OUTPUT_PORT) are different
    # origins from :VIDEO_PORT. LAN tool: allow all origins; tighten if this
    # ever leaves a trusted network.
    video_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST", "GET"],
        allow_headers=["*"],
    )
    video_app.registry = registry  # exposed for tests and /healthz

    @video_app.get("/healthz")
    async def video_healthz() -> dict[str, Any]:
        return {"ok": True, "registry": registry.describe()}

    @video_app.post("/api/webrtc/video/offer")
    async def video_webrtc_offer(
        offer: VideoWebRTCOffer, request: Request
    ) -> dict[str, str]:
        """Answer a browser recvonly offer for the SBS video stream."""
        try:
            validate_transport_session_id(offer.transport_session_id)
        except TransportSessionError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid transport_session_id: {exc}",
            ) from exc
        client = request.client
        client_name = f"{client.host}:{client.port}" if client else "unknown"

        peer = RTCPeerConnection()
        lease = transport_sessions.begin_channel(
            offer.transport_session_id, "video", client_name
        )
        registry.register(peer, offer.transport_session_id, lease, client_name)

        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if channel.label != _VIDEO_CONTROL_LABEL:
                channel.close()
                return

            @channel.on("message")
            def on_message(message: str | bytes) -> None:
                try:
                    payload = json.loads(message)
                except (TypeError, ValueError):
                    return
                if payload.get("type") == "ping":
                    channel.send(
                        json.dumps(
                            {
                                "type": "pong",
                                "ping_sent_at": payload.get("sent_at"),
                                "received_at": time.monotonic_ns() // 1_000_000,
                            }
                        )
                    )

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if peer.connectionState == "connected":
                registry.cancel_guard(peer)
            if peer.connectionState in {"failed", "closed", "disconnected"}:
                await registry.close(peer)

        try:
            await peer.setRemoteDescription(
                RTCSessionDescription(sdp=offer.sdp, type=offer.type)
            )
            # Signaling order per docs/stage1-revise.md §1.1 row 2: the
            # track only needs to exist before createAnswer. aiortc reuses
            # the recvonly transceiver it created for the remote m-line and
            # upgrades it to sendrecv, so the answer is still sendonly.
            peer.addTrack(SyntheticSbsTrack())
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
        except Exception as exc:
            await registry.close(peer)
            raise HTTPException(
                status_code=400, detail=f"WebRTC negotiation failed: {exc}"
            ) from exc

        negotiated = parse_negotiated_video_codec(answer.sdp)
        registry.set_negotiated_codec(peer, negotiated)
        if negotiated is None:
            print(
                f"WARNING: no video codec parsed from answer for {client_name}",
                flush=True,
            )
        elif negotiated.get("mimeType") != "H264":
            # Phase 2 gate: any non-H.264 result must be reported, not
            # silently accepted (docs/stage1-revise.md §4 Phase 2).
            print(
                f"WARNING: negotiated video codec is {negotiated['mimeType']} "
                f"pt={negotiated['payloadType']} (expected H264) for {client_name}",
                flush=True,
            )
        else:
            print(
                f"Negotiated video codec H264 pt={negotiated['payloadType']} "
                f"profile={negotiated.get('profileLevelId')} "
                f"packetization-mode={negotiated.get('packetizationMode')} "
                f"for {client_name}",
                flush=True,
            )

        # Guard against a client that offers but never connects: without
        # this, ICE state never transitions and the peer + lease would pin
        # the "video" channel forever.
        async def _negotiation_guard() -> None:
            await asyncio.sleep(negotiation_timeout)
            if peer.connectionState != "connected":
                print(
                    f"Video negotiation timed out for {client_name} "
                    f"(state={peer.connectionState}); closing peer",
                    flush=True,
                )
                await registry.close(peer)

        guard = asyncio.create_task(_negotiation_guard())
        registry.set_guard(peer, guard)

        local_description = peer.localDescription
        if local_description is None:
            await registry.close(peer)
            raise HTTPException(status_code=500, detail="WebRTC answer was not created")
        return {"sdp": local_description.sdp, "type": local_description.type}

    return video_app
