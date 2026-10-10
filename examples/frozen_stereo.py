"""Reproduce the accepted 0.3.0 image profile; the host owns camera capture.

Install Pillow only when using prepare_eye(). Already prepared RGB can be
submitted directly without Pillow. No camera driver or background task is started.
"""

from io import BytesIO

from quest_xr_bridge import QuestServer, VideoConfig, VideoDisplayConfig


def start_frozen_video(service: QuestServer, *, worker_python: str = "/usr/bin/python3") -> None:
    """Configure the accepted geometry and start its RGB video source."""
    service.set_video_display(VideoDisplayConfig(
        projection="plane", height_m=8, distance_m=7, aspect_ratio=1.66667,
        offset_x_m=0, offset_y_m=-1, swap_eyes=False, saturation=1, gamma=1,
    ))
    service.start_video(VideoConfig(
        width=1278, height=360, mode="stereo", fps=60, worker_python=worker_python,
    ))


def prepare_eye(rgb: object) -> bytes:
    """Crop/decimate a 1280x720 RGB888 eye image and apply a JPEG80 round trip.

    Input is a contiguous unsigned-byte buffer; output owns its RGB memory.
    Camera synchronization, calibration, and capture timestamps remain external.
    """
    from PIL import Image

    view = memoryview(rgb)
    if not view.c_contiguous or view.itemsize != 1 or view.format != "B":
        raise ValueError("expected a contiguous uint8 RGB buffer")
    if view.nbytes != 1280 * 720 * 3:
        raise ValueError("expected one 1280x720 RGB888 eye image")
    raw = view.cast("B")
    cropped = b"".join(raw[(y * 1280 + 1) * 3:(y * 1280 + 1279) * 3]
                       for y in range(0, 720, 2))
    with BytesIO() as buffer:
        with Image.frombytes("RGB", (1278, 360), cropped) as image:
            image.save(buffer, format="JPEG", quality=80)
        buffer.seek(0)
        with Image.open(buffer) as decoded:
            return decoded.tobytes()
