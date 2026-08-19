#!/usr/bin/env python3
"""Open a ZED camera, verify HD720@60 capture, and save H1 evidence."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--min-fps", type=float, default=50.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/quest-crt-zed-check"),
    )
    args = parser.parse_args()
    if args.frames < 2:
        parser.error("--frames must be at least 2")

    try:
        import pyzed.sl as sl
    except ImportError as exc:
        print(f"FAIL: pyzed is unavailable: {exc}")
        return 2

    camera = sl.Camera()
    init = sl.InitParameters()
    init.camera_resolution = sl.RESOLUTION.HD720
    init.camera_fps = 60
    init.depth_mode = sl.DEPTH_MODE.NONE
    init.sdk_verbose = False

    status = camera.open(init)
    if status != sl.ERROR_CODE.SUCCESS:
        print(f"FAIL: ZED open failed: {status}")
        if "SENSORS MODULE MCU" in str(status):
            print(
                "The ZED-M HID MCU has an invalid serial. Replug directly first; "
                "the firmware-level ZED_Diagnostic -r recovery requires explicit approval."
            )
        return 2

    runtime = sl.RuntimeParameters()
    camera_timestamps_ns: list[int] = []
    host_timestamps_s: list[float] = []
    grab_failures = 0
    try:
        while len(camera_timestamps_ns) < args.frames:
            status = camera.grab(runtime)
            if status != sl.ERROR_CODE.SUCCESS:
                grab_failures += 1
                continue
            camera_timestamps_ns.append(
                camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds()
            )
            host_timestamps_s.append(time.perf_counter())

        args.output_dir.mkdir(parents=True, exist_ok=True)
        images = {
            "sbs": (sl.VIEW.SIDE_BY_SIDE, args.output_dir / "zed_sbs.png"),
            "left": (sl.VIEW.LEFT, args.output_dir / "zed_left.png"),
            "right": (sl.VIEW.RIGHT, args.output_dir / "zed_right.png"),
        }
        image_sizes: dict[str, list[int]] = {}
        image_paths: dict[str, str] = {}
        for name, (view, path) in images.items():
            image = sl.Mat()
            camera.retrieve_image(image, view, sl.MEM.CPU)
            image_sizes[name] = [image.get_width(), image.get_height()]
            write_status = image.write(str(path))
            if write_status != sl.ERROR_CODE.SUCCESS:
                raise RuntimeError(f"failed to write {path}: {write_status}")
            image_paths[name] = str(path)

        info = camera.get_camera_information()
        exposure_status, exposure = camera.get_camera_settings(sl.VIDEO_SETTINGS.EXPOSURE)
        gain_status, gain = camera.get_camera_settings(sl.VIDEO_SETTINGS.GAIN)
    finally:
        camera.close()

    intervals_ms = [
        (right - left) * 1000
        for left, right in zip(host_timestamps_s, host_timestamps_s[1:])
    ]
    fps = (len(host_timestamps_s) - 1) / (
        host_timestamps_s[-1] - host_timestamps_s[0]
    )
    timestamps_monotonic = all(
        right > left
        for left, right in zip(camera_timestamps_ns, camera_timestamps_ns[1:])
    )
    passed = (
        fps >= args.min_fps
        and timestamps_monotonic
        and image_sizes["sbs"] == [2560, 720]
        and image_sizes["left"] == [1280, 720]
        and image_sizes["right"] == [1280, 720]
    )
    report = {
        "passed": passed,
        "sdk_version": sl.Camera.get_sdk_version(),
        "camera_model": str(info.camera_model),
        "serial_number": info.serial_number,
        "firmware_version": info.camera_configuration.firmware_version,
        "requested_frames": args.frames,
        "captured_frames": len(camera_timestamps_ns),
        "grab_failures": grab_failures,
        "capture_fps": round(fps, 3),
        "interval_ms_median": round(statistics.median(intervals_ms), 3),
        "interval_ms_p95": round(sorted(intervals_ms)[int(len(intervals_ms) * 0.95) - 1], 3),
        "camera_timestamps_monotonic": timestamps_monotonic,
        "exposure_level": exposure if exposure_status == sl.ERROR_CODE.SUCCESS else None,
        "gain": gain if gain_status == sl.ERROR_CODE.SUCCESS else None,
        "image_sizes": image_sizes,
        "images": image_paths,
    }
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
