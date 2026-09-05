#!/usr/bin/env python3
"""One-shot real-device check for StablePoseStream (friendly, single command).

Usage (server already running, Quest already streaming)::

  uv run python scripts/check_stream.py
  uv run python scripts/check_stream.py --seconds 5
  uv run python scripts/check_stream.py --udp 127.0.0.1:9100

Exit 0 = OK enough to use; non-zero = fix Quest/server first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quest_crt.stream_protocol import decode_stream_envelope  # noqa: E402


def _fetch_health(url: str, timeout: float = 3.0) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ok(msg: str) -> None:
    print(f"  OK   {msg}")


def _bad(msg: str) -> None:
    print(f"  FAIL {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN {msg}")


async def _sample_wss(url: str, seconds: float) -> tuple[int, float, Counter, list[float]]:
    import websockets

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ts: list[float] = []
    quals: Counter = Counter()
    ages: list[float] = []
    async with websockets.connect(url, ssl=ctx, max_size=8 * 1024 * 1024) as ws:
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            if isinstance(raw, str):
                raise RuntimeError("got text/JSON; expected binary stream (default /ws/stream)")
            env = decode_stream_envelope(raw)
            ts.append(time.monotonic())
            quals[env["quality"]] += 1
            if env.get("capture_age_ms") is not None:
                ages.append(float(env["capture_age_ms"]))
    n = len(ts)
    hz = (n - 1) / (ts[-1] - ts[0]) if n > 1 else 0.0
    return n, hz, quals, ages


def _sample_udp(host: str, port: int, seconds: float) -> tuple[int, float, Counter, list[float]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeError(
            f"cannot bind UDP {host}:{port} ({exc}). "
            "Stop other listeners or change STREAM_UDP_PORT / --udp."
        ) from exc
    sock.settimeout(1.0)
    ts: list[float] = []
    quals: Counter = Counter()
    ages: list[float] = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        env = decode_stream_envelope(data)
        ts.append(time.monotonic())
        quals[env["quality"]] += 1
        if env.get("capture_age_ms") is not None:
            ages.append(float(env["capture_age_ms"]))
    sock.close()
    n = len(ts)
    hz = (n - 1) / (ts[-1] - ts[0]) if n > 1 else 0.0
    return n, hz, quals, ages


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--health", default="https://127.0.0.1:8000/health")
    p.add_argument("--stream", default="wss://127.0.0.1:8001/ws/stream")
    p.add_argument(
        "--udp",
        default=None,
        metavar="HOST:PORT",
        help="if set, sample UDP instead of WSS (server needs STREAM_UDP=1)",
    )
    p.add_argument("--seconds", type=float, default=3.0, help="sample window (default 3s)")
    p.add_argument("--expect-hz", type=float, default=90.0)
    args = p.parse_args(argv)

    print("quest-crt stream check")
    print(f"  health = {args.health}")
    print(f"  stream = {args.udp or args.stream}")
    print(f"  window = {args.seconds:g}s")
    print()

    fails = 0

    # --- health ---
    print("[1/2] health")
    try:
        h = _fetch_health(args.health)
    except Exception as exc:
        _bad(f"cannot reach health: {exc}")
        print()
        print("Start server first, e.g.:")
        print(
            "  POSE_LOG_ENABLED=0 STREAM_HZ=90 STREAM_UDP=1 "
            "uv run python server.py"
        )
        print("Then on Quest: open :8000, WebRTC, start XR streaming.")
        return 2

    transport = (h.get("ingress") or {}).get("transport") or (
        (h.get("latest_pose") or {}).get("transport")
    )
    age = (h.get("latest_pose") or {}).get("age_ms")
    fps = (h.get("ingress") or {}).get("fps")
    degraded = bool(h.get("ingress_degraded"))
    healthy = h.get("healthy")
    warnings = h.get("warnings") or []

    if transport == "webrtc":
        _ok(f"ingress={transport}")
    elif transport == "wss":
        _warn("ingress=wss (fallback; WebRTC preferred)")
        fails += 1
    else:
        _bad(f"ingress={transport!r} (is Quest streaming?)")
        fails += 1

    if age is not None and age < 150:
        _ok(f"pose age_ms={age:.1f}")
    elif age is not None:
        _warn(f"pose age_ms={age:.1f} (stale?)")
        fails += 1
    else:
        _bad("no latest pose age (no frames yet?)")
        fails += 1

    if fps is not None and fps >= 30:
        _ok(f"ingress fps≈{float(fps):.1f}")
    elif fps is not None:
        _warn(f"ingress fps≈{float(fps):.1f} (low)")
        fails += 1

    if degraded:
        _warn("ingress_degraded=true")
    if warnings:
        for w in warnings[:3]:
            _warn(w)

    # --- stream sample ---
    print()
    print("[2/2] stream sample")
    try:
        if args.udp:
            host, _, port_s = args.udp.partition(":")
            host = host or "127.0.0.1"
            port = int(port_s or "9100")
            n, hz, quals, ages = _sample_udp(host, port, args.seconds)
        else:
            n, hz, quals, ages = asyncio.run(_sample_wss(args.stream, args.seconds))
    except Exception as exc:
        _bad(f"stream sample failed: {exc}")
        print()
        print("RESULT: FAIL")
        return 2

    expect = float(args.expect_hz)
    lo, hi = expect * 0.75, expect * 1.15
    if n < max(10, int(args.seconds * expect * 0.4)):
        _bad(f"only {n} packets in {args.seconds:g}s")
        fails += 1
    else:
        _ok(f"packets={n}")

    if lo <= hz <= hi:
        _ok(f"rate≈{hz:.1f} Hz (target {expect:g})")
    else:
        _bad(f"rate≈{hz:.1f} Hz (want ~{expect:g})")
        fails += 1

    good = quals.get("ok", 0) + quals.get("held", 0)
    total_q = sum(quals.values()) or 1
    if good / total_q >= 0.5:
        _ok(f"quality={dict(quals)}")
    else:
        _warn(f"quality={dict(quals)} (move hand into view / wait for tracking)")
        if quals.get("lost", 0) == total_q:
            fails += 1

    if ages:
        med = statistics.median(ages)
        if med < 100:
            _ok(f"capture_age_ms median={med:.1f}")
        else:
            _warn(f"capture_age_ms median={med:.1f}")

    print()
    if fails == 0:
        print("RESULT: PASS  — sensing stream looks good")
        return 0
    print("RESULT: FAIL  — fix Quest WebRTC / server / tracking, then re-run")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
