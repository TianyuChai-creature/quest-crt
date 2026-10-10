"""Check the single-port pose path; optionally inject synthetic QCRT over WebRTC."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import ssl
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import websockets
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from quest_xr_bridge.binary_protocol import encode_pose_packet


def frame(session: str, seq: int) -> dict:
    hand = {"tracked": False, "points": [None] * 21, "wrist_orientation": None}
    joint = {"tracked": False, "position": None}
    return {
        "type": "pose",
        "version": 5,
        "session_id": session,
        "seq": seq,
        "timestamp_ms": time.monotonic_ns() / 1e6,
        "capture_epoch_ms": time.time_ns() / 1e6,
        "reference_space": "spine-upper-scapula",
        "units": "meters",
        "hands": {"left": hand, "right": hand},
        "elbows": {"left": joint, "right": joint},
        "shoulders": {"left": joint, "right": joint},
        "head": {"tracked": False, "yaw_deg": None, "pitch_deg": None},
    }


def request(url: str, context: ssl.SSLContext, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=context, timeout=10) as response:
        return json.load(response)


async def check(url: str, context: ssl.SSLContext, seconds: float, inject: bool) -> dict:
    parsed = urlsplit(url)
    ws_url = f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/ws"
    peer = None
    sender = None
    async with websockets.connect(
        ws_url, ssl=context if parsed.scheme == "https" else None
    ) as output:
        try:
            if inject:
                peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                channel = peer.createDataChannel("pose", ordered=False, maxPacketLifeTime=30)
                opened = asyncio.Event()
                channel.on("open", opened.set)
                await peer.setLocalDescription(await peer.createOffer())
                answer = await asyncio.to_thread(
                    request,
                    url + "/api/webrtc/offer",
                    context,
                    {"sdp": peer.localDescription.sdp, "type": "offer"},
                )
                await peer.setRemoteDescription(RTCSessionDescription(**answer))
                await asyncio.wait_for(opened.wait(), 10)
                session = str(uuid.uuid4())

                async def publish():
                    seq = 0
                    while True:
                        seq += 1
                        if channel.bufferedAmount == 0:
                            channel.send(encode_pose_packet(frame(session, seq)))
                        await asyncio.sleep(1 / 90)

                sender = asyncio.create_task(publish())
            started = time.monotonic()
            sequences = []
            last = None
            while time.monotonic() - started < seconds or not sequences:
                raw = json.loads(await asyncio.wait_for(output.recv(), max(seconds, 3)))
                assert raw["version"] == 5 and raw["reference_space"] == "spine-upper-scapula"
                assert raw["representation"] == "hts-wrist-relative"
                assert {
                    "head",
                    "hands",
                    "shoulders",
                    "elbows",
                    "coordinate_transform",
                } <= raw.keys()
                for side in ("left", "right"):
                    assert len(raw["hands"][side]["landmarks"]) == 21
                    assert len(raw["hands"][side]["radii"]) == 21
                if last is not None and raw["session_id"] == last["session_id"]:
                    assert raw["seq"] > last["seq"], "old or repeated pose"
                sequences.append(raw["seq"])
                last = raw
            return {
                "frames": len(sequences),
                "observed_fps": len(sequences) / (time.monotonic() - started),
                "last_seq": sequences[-1],
                "coordinate_transform": last["coordinate_transform"]["name"],
            }
        finally:
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
            if peer is not None:
                await peer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--ca", help="trusted certificate PEM, e.g. certs/cert.pem")
    parser.add_argument("--insecure", action="store_true", help="accept a development certificate")
    parser.add_argument(
        "--inject", action="store_true", help="use a synthetic source; rejected if Quest is active"
    )
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error("--seconds must be within (0, 300]")
    context = (
        ssl._create_unverified_context()
        if args.insecure
        else ssl.create_default_context(cafile=args.ca)
    )
    url = args.url.rstrip("/")
    result = asyncio.run(check(url, context, args.seconds, args.inject))
    result["health"] = request(url + "/health", context)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
