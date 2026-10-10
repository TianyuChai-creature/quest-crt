"""HTTP and WebRTC connections for one SDK-owned runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quest_xr_bridge.coordinates import COORDINATE_PRESETS, remap_axes
from quest_xr_bridge.pose import CoordinateTransformRequest, VideoPeerClose, WebRTCOffer, WebRTCVideoOffer
from quest_xr_bridge.runtime import PoseRuntime, PoseSourceBusyError, PoseStreamProcessor
from quest_xr_bridge.telemetry import format_status_line
from quest_xr_bridge.video import VideoDisplayConfig, VideoManager, VideoUnavailableError

logger = logging.getLogger(__name__)
POSE_HANDSHAKE_TIMEOUT = 10
_PACKAGED_STATIC = Path(__file__).parent / "static"
STATIC_DIR = (
    _PACKAGED_STATIC if _PACKAGED_STATIC.is_dir() else Path(__file__).parent.parent / "static"
)


async def close_pose_peer(runtime: PoseRuntime, peer: RTCPeerConnection) -> None:
    deadline = runtime.peer_timeouts.pop(peer, None)
    if deadline is not None and deadline is not asyncio.current_task():
        deadline.cancel()
        await asyncio.gather(deadline, return_exceptions=True)
    processor = runtime.processors.pop(peer, None)
    runtime.peers.discard(peer)
    if processor is not None:
        processor.close()
    if peer.connectionState != "closed":
        await peer.close()
    if processor is not None:
        await asyncio.to_thread(processor.wait_closed)


async def _report_status(runtime: PoseRuntime) -> None:
    while True:
        await asyncio.sleep(1)
        if runtime.active_source.describe()["active"]:
            logger.info(format_status_line(runtime.health()))


def create_app(runtime: PoseRuntime, video: VideoManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        lag_task = asyncio.create_task(runtime.event_loop_lag.monitor())
        status_task = asyncio.create_task(_report_status(runtime))
        try:
            yield
        finally:
            for task in (lag_task, status_task):
                task.cancel()
            await asyncio.gather(lag_task, status_task, return_exceptions=True)
            await asyncio.gather(*(close_pose_peer(runtime, p) for p in list(runtime.peers)))
            await asyncio.to_thread(video.stop)

    app = FastAPI(title="Quest XR Bridge", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime, app.state.video = runtime, video
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/viewer")
    async def viewer() -> FileResponse:
        return FileResponse(STATIC_DIR / "viewer.html", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {**runtime.health(), "video": video.snapshot()}

    @app.get("/api/coordinate-transform")
    async def get_transform() -> dict[str, Any]:
        return runtime.coordinates.describe()

    @app.put("/api/coordinate-transform")
    async def set_transform(update: CoordinateTransformRequest) -> dict[str, Any]:
        if update.preset is not None:
            name = update.preset.lower().strip()
            transform = COORDINATE_PRESETS.get(name)
            if transform is None:
                raise HTTPException(422, "unknown coordinate preset")
        else:
            try:
                transform = remap_axes(update.axes or ())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            name = update.name or "custom"
        runtime.coordinates.configure(name, transform)
        return runtime.coordinates.describe()

    @app.get("/api/video/config")
    async def get_video() -> dict[str, Any]:
        return video.snapshot()

    @app.put("/api/video/config")
    async def set_video(update: dict[str, Any]) -> dict[str, Any]:
        try:
            display = VideoDisplayConfig(**{**video.snapshot()["display"], **update})
            video.set_display(display)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return video.snapshot()

    @app.post("/api/video/offer")
    async def video_offer(offer: WebRTCVideoOffer) -> dict[str, str]:
        try:
            return await asyncio.to_thread(
                video.offer, offer.sdp, offer.type, peer_id=offer.peer_id
            )
        except VideoUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, TimeoutError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/video/close")
    async def video_close(peer: VideoPeerClose) -> dict[str, bool]:
        try:
            return {"closed": await asyncio.to_thread(video.close_peer, peer.peer_id)}
        except VideoUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, TimeoutError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/webrtc/offer")
    async def pose_offer(offer: WebRTCOffer, request: Request) -> dict[str, str]:
        if runtime.active_source.describe()["active"] or runtime.peers:
            raise HTTPException(409, "another Quest is already connected")
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        runtime.peers.add(peer)

        async def expire_handshake() -> None:
            await asyncio.sleep(POSE_HANDSHAKE_TIMEOUT)
            if peer in runtime.peers and peer not in runtime.processors:
                await close_pose_peer(runtime, peer)

        runtime.peer_timeouts[peer] = asyncio.create_task(expire_handshake())
        client = request.client
        client_name = f"{client.host}:{client.port}" if client else "unknown"

        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if (
                peer not in runtime.peers
                or channel.label != "pose"
                or channel.ordered
                or channel.maxPacketLifeTime != 30
            ):
                channel.close()
                asyncio.create_task(close_pose_peer(runtime, peer))
                return
            if peer in runtime.processors:
                channel.close()
                return
            try:
                processor = PoseStreamProcessor(runtime, client_name)
            except PoseSourceBusyError:
                channel.close()
                asyncio.create_task(close_pose_peer(runtime, peer))
                return
            runtime.processors[peer] = processor
            deadline = runtime.peer_timeouts.pop(peer, None)
            if deadline is not None:
                deadline.cancel()

            @channel.on("message")
            def on_message(message: str | bytes) -> None:
                processor.process(message, time.time_ns() / 1e6, time.monotonic_ns() / 1e6)

            @channel.on("close")
            def on_close() -> None:
                asyncio.create_task(close_pose_peer(runtime, peer))

        @peer.on("connectionstatechange")
        async def state_changed() -> None:
            if peer.connectionState in {"failed", "disconnected", "closed"}:
                await close_pose_peer(runtime, peer)

        try:
            async with asyncio.timeout(10):
                await peer.setRemoteDescription(RTCSessionDescription(offer.sdp, offer.type))
                await peer.setLocalDescription(await peer.createAnswer())
            if peer.localDescription is None:
                raise ValueError("WebRTC answer not created")
            return {"sdp": peer.localDescription.sdp, "type": peer.localDescription.type}
        except (Exception, asyncio.CancelledError) as exc:
            await close_pose_peer(runtime, peer)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(400, f"pose negotiation failed: {exc}") from exc

    @app.websocket("/ws")
    async def output(websocket: WebSocket) -> None:
        await websocket.accept()
        subscription, event = runtime.notifier.subscribe()
        event.set()
        last_generation = -1
        receive = asyncio.create_task(websocket.receive())
        update: asyncio.Task[bool] | None = None
        try:
            while True:
                update = asyncio.create_task(event.wait())
                done, _ = await asyncio.wait({receive, update}, return_when=asyncio.FIRST_COMPLETED)
                if receive in done:
                    message = receive.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    receive = asyncio.create_task(websocket.receive())
                if update not in done:
                    update.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await update
                    continue
                event.clear()
                generation, text = runtime.output_snapshot()
                if text is None or generation == last_generation:
                    continue
                try:
                    await asyncio.wait_for(websocket.send_text(text), timeout=0.25)
                except TimeoutError:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            websocket.close(code=1013, reason="consumer too slow"),
                            timeout=0.25,
                        )
                    break
                last_generation = generation
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            receive.cancel()
            if update is not None:
                update.cancel()
            await asyncio.gather(receive, *([update] if update else []), return_exceptions=True)
            runtime.notifier.unsubscribe(subscription)

    return app
