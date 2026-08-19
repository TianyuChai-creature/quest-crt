#!/usr/bin/env python3
"""Verify the Viewer and StablePoseStream downstream contracts."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import ssl
import sys
import time
import uuid
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quest_crt.stream_protocol import decode_stream_envelope


def _pose_frame() -> dict[str, object]:
    hand = {"tracked": False, "points": [None] * 21, "wrist_orientation": None}
    joint = {"tracked": False, "position": None}
    return {
        "type": "pose",
        "version": 4,
        "session_id": str(uuid.uuid4()),
        "seq": 1,
        "timestamp_ms": time.monotonic_ns() / 1_000_000,
        "capture_epoch_ms": time.time_ns() / 1_000_000,
        "reference_space": "spine-upper-scapula",
        "units": "meters",
        "hands": {"left": hand, "right": hand},
        "elbows": {"left": joint, "right": joint},
        "shoulders": {"left": joint, "right": joint},
    }


async def _check(
    host: str, pose_port: int, output_port: int, *, live: bool = False
) -> dict[str, object]:
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    viewer_url = f"wss://{host}:{output_port}/ws"
    stream_url = f"wss://{host}:{output_port}/ws/stream"
    ingress_url = f"wss://{host}:{pose_port}/ws"

    async with contextlib.AsyncExitStack() as stack:
        viewer = await stack.enter_async_context(websockets.connect(viewer_url, ssl=tls))
        stream = await stack.enter_async_context(
            websockets.connect(stream_url, ssl=tls, max_size=8 * 1024 * 1024)
        )
        if not live:
            ingress = await stack.enter_async_context(
                websockets.connect(ingress_url, ssl=tls)
            )
            await ingress.send(json.dumps(_pose_frame(), separators=(",", ":")))
        viewer_frame = json.loads(await asyncio.wait_for(viewer.recv(), timeout=3.0))
        target_seq = int(viewer_frame["seq"])

        stream_packet = b""
        stream_frame: dict[str, object] | None = None
        for _ in range(30):
            raw = await asyncio.wait_for(stream.recv(), timeout=1.0)
            if not isinstance(raw, bytes):
                raise AssertionError("/ws/stream default format must be binary")
            envelope = decode_stream_envelope(raw)
            if envelope["pose_seq"] >= target_seq and envelope["pose"] is not None:
                stream_packet = raw
                stream_frame = envelope
                break

    if not live:
        assert viewer_frame["seq"] == 1
    assert viewer_frame["representation"] == "hts-wrist-relative"
    assert viewer_frame["coordinate_transform"]["name"] == "body"
    assert set(viewer_frame) >= {"hands", "elbows", "shoulders"}
    assert len(viewer_frame["hands"]["left"]["landmarks"]) == 21
    assert len(viewer_frame["hands"]["right"]["landmarks"]) == 21

    assert stream_frame is not None, "stable stream never published injected pose"
    assert stream_packet.startswith(b"QSTR")
    assert stream_frame["quality"] in {"ok", "held", "stale"}
    pose = stream_frame["pose"]
    assert isinstance(pose, dict)
    assert len(pose["hands"]["left"]["landmarks"]) == 21
    assert len(pose["hands"]["right"]["landmarks"]) == 21

    return {
        "viewer": {
            "seq": viewer_frame["seq"],
            "representation": viewer_frame["representation"],
            "transform": viewer_frame["coordinate_transform"]["name"],
            "landmarks_per_hand": 21,
            "shoulders": "present",
        },
        "stable_stream": {
            "magic": stream_packet[:4].decode("ascii"),
            "packet_bytes": len(stream_packet),
            "pose_seq": stream_frame["pose_seq"],
            "quality": stream_frame["quality"],
            "landmarks_per_hand": 21,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--pose-port", type=int, default=8000)
    parser.add_argument("--output-port", type=int, default=8001)
    parser.add_argument(
        "--live",
        action="store_true",
        help="observe the active Quest instead of injecting a frame",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                _check(args.host, args.pose_port, args.output_port, live=args.live)
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
