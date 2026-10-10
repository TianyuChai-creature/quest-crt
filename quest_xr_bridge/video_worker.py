"""Native GStreamer worker. Executed as a file by the configured system Python.

Only stdlib and gi are imported here; the SDK owns and unlinks shared memory.
The worker attaches through mmap, avoiding a second resource_tracker owner.
"""

from __future__ import annotations

import fcntl
import json
import mmap
import os
import struct
import sys
import threading
import time
import traceback
from collections import deque
from uuid import UUID

_HEADER = struct.Struct("<iiQQ")
_TWCC = "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"
_OUTPUT_LOCK = threading.Lock()
# ITU-T H.264 table A-1: max macroblocks/frame, macroblocks/sec, Baseline Mbps.
_LEVEL_LIMITS = (
    (31, 3600, 108000, 14),
    (32, 5120, 216000, 20),
    (40, 8192, 245760, 20),
    (41, 8192, 245760, 50),
    (42, 8704, 522240, 50),
)
_FMTP_DIAGNOSTIC_KEYS = (
    "profile-level-id",
    "packetization-mode",
    "level-asymmetry-allowed",
    "max-recv-level",
    "max-fs",
    "max-mbps",
    "max-smbps",
    "max-br",
    "max-cpb",
    "max-dpb",
)
_H264_LEVEL_IDS = {10, 11, 12, 13, 20, 21, 22, 30, 31, 32, 40, 41, 42, 50, 51, 52, 60, 61, 62}
# ITU-T H.265 table A.6: level_idc, level, max luma samples/frame and /sec, Main Mbps.
_HEVC_LEVEL_LIMITS = (
    (60, "2", 122880, 3686400, 1.5),
    (63, "2.1", 245760, 7372800, 3),
    (90, "3", 552960, 16588800, 6),
    (93, "3.1", 983040, 33177600, 10),
    (120, "4", 2228224, 66846720, 12),
    (123, "4.1", 2228224, 133693440, 20),
    (150, "5", 8912896, 267386880, 25),
    (153, "5.1", 8912896, 534773760, 40),
    (156, "5.2", 8912896, 1069547520, 60),
    (180, "6", 35651584, 1069547520, 60),
    (183, "6.1", 35651584, 2139095040, 120),
    (186, "6.2", 35651584, 4278190080, 240),
)


def h265_receive_level(parameters: dict) -> int:
    values = {}
    for name, default in (
        ("profile-space", "0"),
        ("profile-id", "1"),
        ("tier-flag", "0"),
        ("level-id", "93"),
    ):
        value = parameters.get(name, default)
        if not value.isascii() or not value.isdecimal():
            raise ValueError(f"H.265 {name} must be a decimal integer")
        values[name] = int(value)
    if values["profile-space"] != 0 or values["profile-id"] != 1:
        raise ValueError("H.265 requires profile-space=0/profile-id=1 (8-bit Main)")
    if values["tier-flag"] != 0:
        raise ValueError("H.265 requires tier-flag=0 (Main tier)")
    if parameters.get("tx-mode", "SRST").upper() != "SRST":
        raise ValueError("H.265 requires tx-mode=SRST for the single video track")
    level = values["level-id"]
    if level not in {row[0] for row in _HEVC_LEVEL_LIMITS}:
        raise ValueError("H.265 level-id is not a supported HEVC level_idc")
    maximum = parameters.get("max-recv-level-id")
    if maximum is not None:
        if not maximum.isascii() or not maximum.isdecimal():
            raise ValueError("H.265 max-recv-level-id must be a decimal integer")
        highest = int(maximum)
        if highest not in {row[0] for row in _HEVC_LEVEL_LIMITS} or highest <= level:
            raise ValueError("H.265 max-recv-level-id must declare a valid higher receive level")
        level = highest
    return level


def choose_hevc_level(width, height, fps, start_mbps, max_mbps, receiver_level=150):
    samples = width * height
    for level_id, level, max_picture, max_rate, bitrate in _HEVC_LEVEL_LIMITS:
        if (
            level_id <= receiver_level
            and samples <= max_picture
            and samples * fps <= max_rate
            and max(width, height) ** 2 <= max_picture * 8
            and start_mbps <= bitrate
        ):
            return level, min(max_mbps, bitrate)
    raise ValueError(
        "receiver H.265 Main tier level cannot carry the configured dimensions/fps/bitrate"
    )


def hevc_level_id(level: str) -> int:
    return next(row[0] for row in _HEVC_LEVEL_LIMITS if row[1] == level)


def restore_payload_on_caps(pad, info, payload: int, Gst):
    # Synchronous CAPS handling precedes RTP header allocation; no packet bytes are rewritten.
    if info.get_event().type == Gst.EventType.CAPS:
        payloader = pad.get_parent_element()
        if payloader is not None:
            payloader.set_property("pt", payload)
    return Gst.PadProbeReturn.OK


def validate_send_answer(description, payload: int) -> None:
    videos = [
        description.get_media(i)
        for i in range(description.medias_len())
        if description.get_media(i).get_media() == "video"
    ]
    if len(videos) != 1:
        raise ValueError("video answer must contain exactly one video media section")
    media = videos[0]
    directions = {media.get_attribute(i).key for i in range(media.attributes_len())}
    if media.get_port() == 0 or "sendonly" not in directions:
        raise ValueError("video answer has no active sendonly media sender")
    if str(payload) not in [media.get_format(i) for i in range(media.formats_len())]:
        raise ValueError("video answer did not negotiate the selected RTP payload")


def h264_receive_level(parameters: dict) -> int:
    profile = parameters.get("profile-level-id", "42000a")
    if len(profile) != 6:
        raise ValueError("profile-level-id must contain exactly six hexadecimal digits")
    try:
        profile_bytes = bytes.fromhex(profile)
    except ValueError as exc:
        raise ValueError("profile-level-id must contain exactly six hexadecimal digits") from exc
    if len(profile_bytes) != 3 or profile_bytes[0] != 0x42 or profile_bytes[1] & 0x0F:
        raise ValueError("only valid H.264 baseline/constrained-baseline profiles are supported")
    level = profile_bytes[2]
    if level not in _H264_LEVEL_IDS:
        raise ValueError("profile-level-id has an unsupported H.264 level_idc")
    maximum = parameters.get("max-recv-level")
    if maximum is None:
        return level
    if len(maximum) != 4:
        raise ValueError(
            "max-recv-level must contain four hexadecimal digits (profile-iop+level_idc)"
        )
    try:
        received = bytes.fromhex(maximum)
    except ValueError as exc:
        raise ValueError(
            "max-recv-level must contain four hexadecimal digits (profile-iop+level_idc)"
        ) from exc
    if len(received) != 2 or received[0] & 0x0F:
        raise ValueError("max-recv-level contains invalid profile-iop constraints")
    if bool(received[0] & 0x40) != bool(profile_bytes[1] & 0x40):
        raise ValueError("max-recv-level sub-profile does not match profile-level-id")
    if received[1] not in _H264_LEVEL_IDS or received[1] <= level:
        raise ValueError("max-recv-level must declare a valid level above profile-level-id")
    return received[1]


def choose_level(
    width: int, height: int, fps: int, start_mbps: float, max_mbps: float, receiver_level=42
):
    columns, rows = (width + 15) // 16, (height + 15) // 16
    blocks = columns * rows
    for level, max_frame, max_rate, bitrate in _LEVEL_LIMITS:
        if (
            level <= receiver_level
            and blocks <= max_frame
            and blocks * fps <= max_rate
            and max(columns, rows) ** 2 <= max_frame * 8
            and start_mbps <= bitrate
        ):
            return f"{level / 10:g}", min(max_mbps, bitrate)
    raise ValueError(
        "receiver H.264 level cannot carry the configured dimensions, fps and start bitrate"
    )


def pack_stereo(raw: bytes, width: int, height: int) -> bytes:
    row = width * 3
    eye_size = row * height
    image = bytearray(len(raw))
    for y in range(height):
        image[y * row * 2 : (y * 2 + 1) * row] = raw[y * row : (y + 1) * row]
        image[(y * 2 + 1) * row : (y + 1) * row * 2] = raw[
            eye_size + y * row : eye_size + (y + 1) * row
        ]
    # PyGObject copies bytes directly; bytearray takes its slow per-element conversion path.
    return bytes(image)


def emit(message: dict) -> None:
    with _OUTPUT_LOCK:
        print(json.dumps(message, separators=(",", ":")), flush=True)


def verify_webrtc_plugin(Gst) -> None:
    """Check the loaded compatibility marker; this is not an authenticity check."""
    feature = Gst.ElementFactory.find("webrtcbin")
    loaded = feature.load() if feature is not None else None
    plugin = loaded.get_plugin() if loaded is not None else None
    requirement = (
        "video requires a loaded webrtc plugin >= 1.24.13 with "
        "'quest-crt DTLS owner fix'; deploy the patched native runtime"
    )
    if plugin is None or not plugin.is_loaded():
        raise RuntimeError(requirement)
    try:
        version = tuple(int(part) for part in plugin.get_version().split(".")[:3])
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(requirement) from exc
    if (
        len(version) != 3
        or version < (1, 24, 13)
        or "quest-crt DTLS owner fix" not in (plugin.get_package() or "")
    ):
        raise RuntimeError(requirement)


class VideoWorker:
    def __init__(self, name: str, slot_size: int, config: dict) -> None:
        import gi

        for namespace in ("Gst", "GstSdp", "GstWebRTC", "GstRtp", "GstVideo"):
            gi.require_version(namespace, "1.0")
        from gi.repository import GLib, GObject, Gst, GstRtp, GstSdp, GstVideo, GstWebRTC

        self.GLib, self.GObject, self.Gst = GLib, GObject, Gst
        self.GstRtp, self.GstSdp, self.GstWebRTC = GstRtp, GstSdp, GstWebRTC
        self.GstVideo = GstVideo
        Gst.init(None)
        if Gst.version() < (1, 24, 0, 0):
            raise RuntimeError("video requires GStreamer >= 1.24")
        required = (
            "webrtcbin",
            "rtpgccbwe",
            "nvh264enc",
            "appsrc",
            "videoconvert",
            "h264parse",
            "rtph264pay",
            "nicesrc",
            "nicesink",
            "dtlssrtpenc",
            "dtlssrtpdec",
        )
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        if missing:
            raise RuntimeError("missing GStreamer video plugins: " + ", ".join(missing))
        verify_webrtc_plugin(Gst)
        if GstRtp.RTPHeaderExtension.create_from_uri(_TWCC) is None:
            raise RuntimeError("GStreamer TWCC RTP header extension is unavailable")
        self.config = config
        self.width = config["width"] * (2 if config["mode"] == "stereo" else 1)
        self.height = config["height"]
        self.fps = config["fps"]
        self.codec = "H264"
        self.negotiated_codec = None
        self.level, self.bitrate_limit = choose_level(
            self.width,
            self.height,
            self.fps,
            config["start_bitrate_mbps"],
            config["max_bitrate_mbps"],
        )
        self.slot_size = slot_size
        if slot_size != config["width"] * self.height * 3 * (
            2 if config["mode"] == "stereo" else 1
        ):
            raise ValueError("shared frame size does not match video config")
        self._probe_gpu()
        self._probed_codecs = {"H264"}
        self.fd = os.open("/dev/shm/" + name, os.O_RDWR)
        self.memory = mmap.mmap(self.fd, _HEADER.size + slot_size * 2)
        self.loop = GLib.MainLoop()
        self.pipeline = self.peer = self.source = self.encoder = None
        self.gcc = None
        self._peer_generation = 0
        self._peer_lock = threading.RLock()
        self._signal_handlers = []
        self._pending_promise = None
        self._offer_timer = None
        self._connection_timer = None
        self.peer_nonce = None
        self._cancelled_peer_ids = deque(maxlen=64)
        self.pending_offer: int | None = None
        self.first_timestamp: int | None = None
        self.next_push = 0.0
        self.frames_sent = 0
        self.last_capture = None
        self.estimated_bitrate = int(config["start_bitrate_mbps"] * 1_000_000)
        self.negotiation_stage = "idle"
        self._stage_started = time.monotonic()

    def _dispatch(self, callback, *args):
        return self.GLib.idle_add(callback, *args, priority=self.GLib.PRIORITY_DEFAULT)

    def _connect(self, obj, signal, callback, generation):
        handler = obj.connect(signal, callback, generation)
        self._signal_handlers.append((obj, handler))

    def _new_promise(self, callback, generation):
        promise = self.Gst.Promise.new_with_change_func(
            lambda promise, *_: self._dispatch(callback, promise, generation), None, None
        )
        self._pending_promise = promise
        return promise

    def _cancel_offer_timer(self):
        timer, self._offer_timer = self._offer_timer, None
        if timer is not None:
            self.GLib.source_remove(timer)

    def _cancel_connection_timer(self):
        timer, self._connection_timer = self._connection_timer, None
        if timer is not None:
            self.GLib.source_remove(timer)

    @staticmethod
    def _peer_id(value):
        if not isinstance(value, str) or len(value) > 36:
            raise ValueError("peer_id must be a UUID of at most 36 characters")
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("peer_id must be a valid UUID") from exc

    def _remember_cancelled(self, peer_id):
        if peer_id not in self._cancelled_peer_ids:
            self._cancelled_peer_ids.append(peer_id)

    def close_peer(self, request):
        peer_id = self._peer_id(request.get("peer_id"))
        self._remember_cancelled(peer_id)
        closed = peer_id == self.peer_nonce
        if closed:
            if self.pending_offer is not None:
                self._offer_error("video peer closed during negotiation")
            else:
                self._close_peer()
            self.stats()
        emit({"id": request["id"], "result": {"closed": closed}})

    def _stage(self, stage: str) -> None:
        self.negotiation_stage = stage
        self._stage_started = time.monotonic()

    def _encoder_chain(self, codec=None, level=None) -> str:
        codec, level = codec or self.codec, level or self.level
        if codec == "H265":
            return (
                "videoconvert ! video/x-raw,format=NV12 ! "
                "nvh265enc name=encoder bframes=0 zerolatency=true rc-mode=cbr "
                f"gop-size={self.fps} bitrate={int(self.config['start_bitrate_mbps'] * 1000)} ! "
                f"video/x-h265,profile=main,tier=main,level=(string){level},"
                "stream-format=byte-stream,alignment=au ! h265parse"
            )
        return (
            "videoconvert ! video/x-raw,format=NV12 ! "
            "nvh264enc name=encoder bframes=0 zerolatency=true rc-mode=cbr "
            f"gop-size={self.fps} bitrate={int(self.config['start_bitrate_mbps'] * 1000)} ! "
            f"video/x-h264,profile=constrained-baseline,level=(string){level},"
            "stream-format=byte-stream,alignment=au ! h264parse"
        )

    def _probe_gpu(self, codec=None, level=None) -> None:
        Gst = self.Gst
        pipeline = Gst.parse_launch(
            "videotestsrc num-buffers=2 pattern=smpte ! "
            f"video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1 ! "
            + self._encoder_chain(codec, level)
            + " ! fakesink sync=false"
        )
        try:
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("NVENC video probe could not start")
            message = pipeline.get_bus().timed_pop_filtered(
                5 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
            )
            if message is None:
                raise RuntimeError("NVENC video probe timed out")
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                raise RuntimeError(f"NVENC video probe failed: {error.message}; {debug or ''}")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def _parse_offer(self, sdp: str):
        GstSdp = self.GstSdp
        result, description = GstSdp.SDPMessage.new()
        if (
            result != GstSdp.SDPResult.OK
            or GstSdp.sdp_message_parse_buffer(sdp.encode(), description) != GstSdp.SDPResult.OK
        ):
            raise ValueError("invalid video SDP")
        videos = [
            description.get_media(i)
            for i in range(description.medias_len())
            if description.get_media(i).get_media() == "video"
        ]
        if len(videos) != 1:
            raise ValueError("video offer must contain exactly one video media section")
        media = videos[0]
        attrs = [
            (media.get_attribute(i).key, media.get_attribute(i).value)
            for i in range(media.attributes_len())
        ]
        ext_id = None
        for key, value in attrs:
            if key == "extmap" and value and _TWCC in value:
                number, _, direction = value.split()[0].partition("/")
                if direction not in ("sendonly", "inactive"):
                    ext_id = int(number)
        if ext_id is None or not 1 <= ext_id <= 255:
            raise ValueError("video offer must negotiate the TWCC RTP header extension")
        errors = []
        candidates = [
            (codec, key, value)
            for codec in ("H265", "H264")
            for key, value in attrs
            if key == "rtpmap" and value and value.split()[-1].upper() == codec + "/90000"
        ]
        for codec, _key, value in candidates:
            payload = int(value.split()[0])
            fmtp = next(
                (v for k, v in attrs if k == "fmtp" and v and v.startswith(str(payload) + " ")), ""
            )
            parameters = {
                name.strip().lower(): value.strip()
                for name, value in (
                    part.split("=", 1) for part in fmtp.partition(" ")[2].split(";") if "=" in part
                )
            }
            keys = _FMTP_DIAGNOSTIC_KEYS + (
                "profile-space",
                "profile-id",
                "tier-flag",
                "level-id",
                "max-recv-level-id",
                "tx-mode",
            )
            summary = {name: parameters[name][:80] for name in keys if name in parameters}
            print(
                f"Video {codec} fmtp: " + json.dumps({"payload": payload, "fmtp": summary}),
                file=sys.stderr,
                flush=True,
            )
            try:
                if not (35 <= payload <= 63 or 96 <= payload <= 127):
                    raise ValueError(f"{codec} payload type must be dynamic (35..63 or 96..127)")
                if codec == "H264" and parameters.get("packetization-mode") != "1":
                    raise ValueError("packetization-mode=1 is required")
                if not any(
                    k == "rtcp-fb" and v in (f"{payload} transport-cc", "* transport-cc")
                    for k, v in attrs
                ):
                    raise ValueError("RTCP transport-cc feedback is required")
                receive_level = (
                    h265_receive_level(parameters)
                    if codec == "H265"
                    else h264_receive_level(parameters)
                )
                select_level = choose_hevc_level if codec == "H265" else choose_level
                required_level, _ = select_level(
                    self.width,
                    self.height,
                    self.fps,
                    self.config["start_bitrate_mbps"],
                    self.config["max_bitrate_mbps"],
                )
                try:
                    level, bitrate_limit = select_level(
                        self.width,
                        self.height,
                        self.fps,
                        self.config["start_bitrate_mbps"],
                        self.config["max_bitrate_mbps"],
                        receive_level,
                    )
                except ValueError as exc:
                    highest = (
                        f"highest level {receive_level // 10}.{receive_level % 10}"
                        if codec == "H264"
                        else f"level-id {receive_level}"
                    )
                    raise ValueError(
                        f"{codec} receiver {highest} "
                        f"is insufficient; configured {self.width}x{self.height}@{self.fps} "
                        f"requires level >= {required_level} at {self.config['start_bitrate_mbps']} Mbps"
                    ) from exc
            except ValueError as exc:
                errors.append(f"PT{payload} {summary}: {exc}")
                continue
            return description, payload, ext_id, level, bitrate_limit, codec
        raise ValueError(
            "video codec negotiation rejected: "
            + ("; ".join(errors) if errors else "no H.265/H.264 codec offered")
        )

    def offer(self, request: dict) -> None:
        peer_id = self._peer_id(request.get("peer_id"))
        if peer_id in self._cancelled_peer_ids:
            raise ValueError("video peer_id has already been cancelled")
        if peer_id == self.peer_nonce:
            raise ValueError("video peer_id must be unique for each offer")
        if self.pending_offer is not None:
            raise ValueError("video negotiation is already in progress")
        if request.get("type") != "offer":
            raise ValueError("video requires an SDP offer")
        description, payload, ext_id, level, bitrate_limit, codec = self._parse_offer(
            request["sdp"]
        )
        if codec not in self._probed_codecs:
            required = (
                ("nvh265enc", "h265parse", "rtph265pay")
                if codec == "H265"
                else ("nvh264enc", "h264parse", "rtph264pay")
            )
            missing = [name for name in required if self.Gst.ElementFactory.find(name) is None]
            if missing:
                raise RuntimeError("missing native codec plugins: " + ", ".join(missing))
            self._probe_gpu(codec, level)
            self._probed_codecs.add(codec)
        self._close_peer()
        generation = self._peer_generation
        self.peer_nonce = peer_id
        self.codec, self.level, self.bitrate_limit = codec, level, bitrate_limit
        self.negotiated_codec = codec
        self.selected_payload = payload
        Gst = self.Gst
        self.pending_offer = request["id"]
        self._offer_timer = self.GLib.timeout_add(
            max(1, int(min(8, request.get("timeout", 10)) * 1000)),
            self._offer_timeout,
            request["id"],
            generation,
        )
        self._stage("pipeline_create")
        bridge = ""
        if 35 <= payload <= 63:
            factory = Gst.ElementFactory.find(f"rtp{codec.lower()}pay")
            offered_caps = Gst.Caps.from_string(f"application/x-rtp,payload=(int){payload}")
            source_caps = next(
                template.get_caps()
                for template in factory.get_static_pad_templates()
                if template.direction == Gst.PadDirection.SRC
            )
            if not source_caps.can_intersect(offered_caps):
                if Gst.ElementFactory.find("capssetter") is None:
                    raise RuntimeError(
                        "low RTP payload type requires a fixed payloader or capssetter"
                    )
                # shortcut: bridge only old templates excluding legal low PT; remove after native payloader upgrade.
                bridge = f' ! capssetter caps="application/x-rtp,payload=(int){payload}" join=true replace=false'
        self.pipeline = Gst.parse_launch(
            "webrtcbin name=peer bundle-policy=max-bundle "
            "appsrc name=source is-live=true format=time block=false max-buffers=1 leaky-type=downstream "
            f"caps=video/x-raw,format=RGB,width={self.width},height={self.height},framerate={self.fps}/1 ! "
            + self._encoder_chain()
            + f" ! rtp{codec.lower()}pay name=pay pt={payload} config-interval=-1"
            + bridge
            + " ! "
            f"application/x-rtp,media=video,encoding-name={codec},clock-rate=90000,payload={payload},"
            + "rtcp-fb-nack=(boolean)true,rtcp-fb-nack-pli=(boolean)true,"
            "rtcp-fb-ccm-fir=(boolean)true,rtcp-fb-transport-cc=(boolean)true ! peer."
        )
        self.peer = self.pipeline.get_by_name("peer")
        # NACK caps alone do not enable webrtcbin's bounded RTX packet history.
        self.peer.emit("get-transceiver", 0).set_property("do-nack", True)
        self.source = self.pipeline.get_by_name("source")
        self.encoder = self.pipeline.get_by_name("encoder")
        pay = self.pipeline.get_by_name("pay")
        if bridge:
            pay.get_static_pad("src").add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM,
                lambda pad, info: restore_payload_on_caps(pad, info, payload, Gst),
            )
        extension = self.GstRtp.RTPHeaderExtension.create_from_uri(_TWCC)
        if extension is None:
            raise RuntimeError("GStreamer TWCC RTP header extension is unavailable")
        extension.set_id(ext_id)
        pay.emit("add-extension", extension)
        signal = "request-post-rtp-aux-sender"
        if not self.GObject.signal_lookup(signal, self.peer.__gtype__):
            signal = "request-aux-sender"
        self._connect(self.peer, signal, self._create_gcc, generation)
        self._connect(self.peer, "notify::ice-gathering-state", self._ice_changed, generation)
        self._connect(self.peer, "notify::connection-state", self._connection_changed, generation)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._connect(bus, "message::error", self._bus_error, generation)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("video pipeline could not start")
        self._stage("pipeline_playing")
        offer = self.GstWebRTC.WebRTCSessionDescription.new(
            self.GstWebRTC.WebRTCSDPType.OFFER, description
        )
        promise = self._new_promise(self._remote_set, generation)
        self._stage("set_remote_description")
        self.peer.emit("set-remote-description", offer, promise)
        self._stage("set_remote_wait_promise")

    def _create_gcc(self, peer, transport, generation):
        with self._peer_lock:
            if generation != self._peer_generation or peer is not self.peer or self.encoder is None:
                return None
            bitrate_limit = self.bitrate_limit
        gcc = self.Gst.ElementFactory.make("rtpgccbwe")
        if gcc is None:
            self._dispatch(self._gcc_error, generation)
            return None
        gcc.set_property("max-bitrate", int(bitrate_limit * 1_000_000))
        gcc.set_property("estimated-bitrate", int(self.config["start_bitrate_mbps"] * 1_000_000))
        with self._peer_lock:
            if generation != self._peer_generation or peer is not self.peer or self.encoder is None:
                return None
            self._connect(gcc, "notify::estimated-bitrate", self._bitrate_changed, generation)
            self.gcc = gcc
        return gcc

    def _gcc_error(self, generation) -> bool:
        if generation == self._peer_generation:
            self._fatal("could not create required GCC bandwidth estimator")
        return False

    def _bitrate_changed(self, gcc, _property, generation) -> None:
        if generation == self._peer_generation and gcc is self.gcc:
            self._dispatch(self._apply_bitrate, generation)

    def _apply_bitrate(self, generation) -> bool:
        if generation != self._peer_generation or self.gcc is None or self.encoder is None:
            return False
        bitrate = min(
            self.gcc.get_property("estimated-bitrate"), int(self.bitrate_limit * 1_000_000)
        )
        self.estimated_bitrate = bitrate
        self.encoder.set_property("bitrate", max(1, bitrate // 1000))
        return False

    def _remote_set(self, promise, generation) -> bool:
        if (
            generation != self._peer_generation
            or self._pending_promise is None
            or self.peer is None
        ):
            return False
        self._pending_promise = None
        self._stage("set_remote_done")
        reply = promise.get_reply()
        if reply is not None and reply.has_field("error"):
            self._offer_error(str(reply.get_value("error")))
            return False
        if self.peer is not None and self.pending_offer is not None:
            promise = self._new_promise(self._answer_created, generation)
            self._stage("create_answer")
            self.peer.emit("create-answer", None, promise)
            self._stage("create_answer_wait_promise")
        return False

    def _answer_created(self, promise, generation) -> bool:
        if (
            generation != self._peer_generation
            or self._pending_promise is None
            or self.peer is None
        ):
            return False
        self._pending_promise = None
        self._stage("create_answer_done")
        reply = promise.get_reply()
        if reply is None or not reply.has_field("answer"):
            self._offer_error("GStreamer did not create a video answer")
            return False
        if self.peer is not None:
            answer = reply.get_value("answer")
            try:
                validate_send_answer(answer.sdp, self.selected_payload)
            except ValueError as exc:
                self._offer_error(str(exc))
                return False
            promise = self._new_promise(self._local_set, generation)
            self._stage("set_local_description")
            self.peer.emit("set-local-description", answer, promise)
            self._stage("set_local_wait_promise")
        return False

    def _local_set(self, promise, generation) -> bool:
        if (
            generation != self._peer_generation
            or self._pending_promise is None
            or self.peer is None
        ):
            return False
        self._pending_promise = None
        self._stage("set_local_done")
        reply = promise.get_reply()
        if reply is not None and reply.has_field("error"):
            self._offer_error(str(reply.get_value("error")))
        else:
            self._ice_changed(self.peer, None, generation)
        return False

    def _ice_changed(self, peer, _property, generation) -> None:
        if generation == self._peer_generation and peer is self.peer:
            self._dispatch(self._finish_ice, generation)

    def _finish_ice(self, generation) -> bool:
        if generation != self._peer_generation or self.peer is None or self.pending_offer is None:
            return False
        peer = self.peer
        if (
            peer.get_property("ice-gathering-state")
            == self.GstWebRTC.WebRTCICEGatheringState.COMPLETE
        ):
            description = peer.get_property("local-description")
            if description is not None:
                request_id, self.pending_offer = self.pending_offer, None
                self._cancel_offer_timer()
                self._stage("answer_ready")
                if (
                    peer.get_property("connection-state")
                    != self.GstWebRTC.WebRTCPeerConnectionState.CONNECTED
                ):
                    self._connection_timer = self.GLib.timeout_add(
                        10_000, self._connection_timeout, generation
                    )
                emit(
                    {
                        "id": request_id,
                        "result": {"type": "answer", "sdp": description.sdp.as_text()},
                    }
                )
        return False

    def _offer_error(self, message: str) -> None:
        if self.pending_offer is not None:
            emit({"id": self.pending_offer, "error": message, "kind": "ValueError"})
            self.pending_offer = None
        self._close_peer()

    def _offer_timeout(self, request_id: int, generation: int) -> bool:
        if generation != self._peer_generation:
            return False
        self._offer_timer = None
        if self.pending_offer == request_id:
            emit(
                {
                    "id": request_id,
                    "error": f"video negotiation timed out at {self.negotiation_stage}",
                    "kind": "TimeoutError",
                }
            )
            self.pending_offer = None
            self._close_peer()
        return False

    def _connection_changed(self, peer, _property, generation) -> None:
        if generation == self._peer_generation and peer is self.peer:
            self._dispatch(self._apply_connection_state, generation)

    def _connection_timeout(self, generation) -> bool:
        if generation != self._peer_generation:
            return False
        self._connection_timer = None
        if self.peer is not None and (
            self.peer.get_property("connection-state")
            != self.GstWebRTC.WebRTCPeerConnectionState.CONNECTED
        ):
            self._stage("connection_timeout")
            self._close_peer()
        return False

    def _apply_connection_state(self, generation) -> bool:
        if generation != self._peer_generation or self.peer is None:
            return False
        state = self.peer.get_property("connection-state")
        if state == self.GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            self._cancel_connection_timer()
        if state in (
            self.GstWebRTC.WebRTCPeerConnectionState.FAILED,
            self.GstWebRTC.WebRTCPeerConnectionState.CLOSED,
            self.GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED,
        ):
            if self.pending_offer is not None:
                self._offer_error(f"video peer {state.value_nick} during negotiation")
            else:
                self._close_peer()
        return False

    def _bus_error(self, bus, message, generation) -> None:
        if generation != self._peer_generation:
            return
        error, debug = message.parse_error()
        self._fatal(f"video pipeline failed: {error.message}; {debug or ''}")

    def _fatal(self, message: str) -> None:
        emit({"event": "error", "error": message})
        self.loop.quit()

    def _close_peer(self) -> bool:
        if self.pipeline is not None:
            self._stage("close_begin")
        # Never hold this lock during NULL: webrtcbin joins its native PC thread.
        with self._peer_lock:
            self._peer_generation += 1
            if self.peer_nonce is not None:
                self._remember_cancelled(self.peer_nonce)
            self.peer_nonce = None
            pipeline, self.pipeline = self.pipeline, None
            self.peer = self.source = self.encoder = self.gcc = None
            handlers, self._signal_handlers = self._signal_handlers, []
            promise, self._pending_promise = self._pending_promise, None
            self.pending_offer = None
            self.negotiated_codec = None
            self.first_timestamp = self.last_capture = None
            self.next_push = 0.0
        self._cancel_offer_timer()
        self._cancel_connection_timer()
        if pipeline is not None:
            self._stage("close_disconnect_signals")
        for obj, handler in handlers:
            obj.disconnect(handler)
        if promise is not None:
            promise.interrupt()
        if pipeline is not None:
            self._stage("close_remove_bus_watch")
            pipeline.get_bus().remove_signal_watch()
            self._stage("close_set_null")
            if pipeline.set_state(self.Gst.State.NULL) == self.Gst.StateChangeReturn.FAILURE:
                message = "video pipeline failed to enter NULL during close"
                self._fatal(message)
                raise RuntimeError(message)
            self._stage("close_done")
        return False

    def pump(self) -> bool:
        try:
            return self._pump()
        except Exception as exc:  # noqa: BLE001 - Report frame failures to the SDK before stopping GLib.
            self._fatal(f"video frame processing failed: {exc}")
            return False

    def _pump(self) -> bool:
        if self.source is None or time.monotonic() < self.next_push:
            return True
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        try:
            pending, processing, sequence, timestamp = _HEADER.unpack_from(self.memory)
            if pending < 0:
                return True
            if processing >= 0:
                raise RuntimeError("video slot protocol already has a processing frame")
            _HEADER.pack_into(self.memory, 0, -1, pending, sequence, timestamp)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        try:
            offset = _HEADER.size + pending * self.slot_size
            raw = self.memory[offset : offset + self.slot_size]
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            try:
                next_slot, _, sequence, next_timestamp = _HEADER.unpack_from(self.memory)
                _HEADER.pack_into(self.memory, 0, next_slot, -1, sequence, next_timestamp)
            finally:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        if self.config["mode"] == "stereo":
            raw = pack_stereo(raw, self.config["width"], self.height)
        Gst = self.Gst
        buffer = Gst.Buffer.new_allocate(None, len(raw), None)
        buffer.fill(0, raw)
        self.GstVideo.buffer_add_video_meta_full(
            buffer,
            self.GstVideo.VideoFrameFlags.NONE,
            self.GstVideo.VideoFormat.RGB,
            self.width,
            self.height,
            1,
            [0, 0, 0, 0],
            [self.width * 3, 0, 0, 0],
        )
        if self.first_timestamp is None:
            self.first_timestamp = timestamp
        buffer.pts = buffer.dts = timestamp - self.first_timestamp
        buffer.duration = Gst.SECOND // self.fps
        result = self.source.emit("push-buffer", buffer)
        if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING):
            self._fatal(f"video source rejected a frame: {result.value_nick}")
        self.frames_sent += 1
        self.last_capture = timestamp
        self.next_push = max(self.next_push + 1 / self.fps, time.monotonic())
        return True

    def stats(self) -> bool:
        connected = (
            self.peer is not None
            and self.peer.get_property("connection-state")
            == self.GstWebRTC.WebRTCPeerConnectionState.CONNECTED
        )
        emit(
            {
                "event": "stats",
                "stats": {
                    "peer_connected": connected,
                    "frames_sent": self.frames_sent,
                    "frames_submitted_to_encoder": self.frames_sent,
                    "estimated_bitrate_mbps": self.estimated_bitrate / 1e6,
                    "codec_level": self.level,
                    "negotiated_codec": self.negotiated_codec,
                    "negotiation_stage": self.negotiation_stage,
                    "negotiation_stage_age_ms": round(
                        (time.monotonic() - self._stage_started) * 1000, 1
                    ),
                    "peer_connection_state": self.peer.get_property("connection-state").value_nick
                    if self.peer is not None
                    else "closed",
                    "ice_gathering_state": self.peer.get_property("ice-gathering-state").value_nick
                    if self.peer is not None
                    else "new",
                    "encoder_bitrate_limit_mbps": self.bitrate_limit,
                    "last_capture_timestamp_ns": self.last_capture,
                },
            }
        )
        return True

    def command(self, request: dict) -> bool:
        try:
            if request["op"] == "offer":
                self.offer(request)
            elif request["op"] == "close_peer":
                self.close_peer(request)
            elif request["op"] == "stop":
                self.stats()
                emit({"id": request["id"], "result": {}})
                self.loop.quit()
            else:
                raise ValueError("unknown video operation")
        except Exception as exc:  # noqa: BLE001 - Every command failure must become an RPC error response.
            if self.pending_offer == request["id"]:
                self._close_peer()
            emit({"id": request["id"], "error": str(exc), "kind": type(exc).__name__})
        return False

    def read_commands(self) -> None:
        try:
            for line in sys.stdin:
                request = json.loads(line)
                self._dispatch(self.command, request)
        except Exception as exc:  # noqa: BLE001 - Report malformed IPC input before shutting down the worker.
            emit({"event": "error", "error": f"video control protocol failed: {exc}"})
        finally:
            self._dispatch(self.loop.quit)

    def close(self) -> None:
        try:
            self._close_peer()
        finally:
            self.memory.close()
            os.close(self.fd)


def main() -> None:
    worker = None
    request = None
    try:
        request = json.loads(sys.stdin.readline())
        if request.get("op") != "start":
            raise ValueError("video worker must receive start first")
        worker = VideoWorker(sys.argv[1], int(sys.argv[2]), request["config"])
        worker.frames_sent = request.get("frames_sent", 0)
        emit({"id": request["id"], "result": {"ready": True}})
        threading.Thread(target=worker.read_commands, daemon=True).start()
        worker.GLib.timeout_add(1, worker.pump, priority=worker.GLib.PRIORITY_DEFAULT_IDLE)
        worker.GLib.timeout_add_seconds(1, worker.stats)
        worker.loop.run()
    except Exception as exc:  # noqa: BLE001 - Return startup failures across the process boundary.
        traceback.print_exc(file=sys.stderr)
        if request is not None:
            emit({"id": request["id"], "error": str(exc), "kind": type(exc).__name__})
        else:
            emit({"event": "error", "error": str(exc)})
        sys.exit(1)
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    main()
