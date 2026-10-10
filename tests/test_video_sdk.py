from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import weakref
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from quest_xr_bridge.video import VideoConfig, VideoDisplayConfig, VideoManager, VideoUnavailableError
from quest_xr_bridge.video_worker import (
    VideoWorker,
    choose_hevc_level,
    choose_level,
    hevc_level_id,
    pack_stereo,
    restore_payload_on_caps,
    validate_send_answer,
    verify_webrtc_plugin,
)

# This exercises real process / pipe / mmap ownership. It does not emulate NVENC.
_CONTROL_WORKER = """
import fcntl, json, mmap, os, signal, struct, sys, time
from collections import deque
header = struct.Struct("<iiQQ")
fd = os.open("/dev/shm/" + sys.argv[1], os.O_RDWR)
size = int(sys.argv[2])
memory = mmap.mmap(fd, header.size + size * 2)
active_peer = None
cancelled = deque(maxlen=64)
def reply(request, result):
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["op"] == "start":
        fps = request['config']['fps']
        if request['config']['fps'] == 1:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
        reply(request, {"ready": True})
        if request['config']['fps'] == 2:
            time.sleep(60)
    elif request["op"] == "stop":
        if fps == 3:
            os._exit(7)
        if fps == 4:
            print(json.dumps({"event": "error", "error": "native error during stop"}), flush=True)
            break
        reply(request, {})
        if fps == 5:
            time.sleep(60)
        break
    elif request["op"] == "close_peer":
        peer_id = request["peer_id"]
        if peer_id not in cancelled:
            cancelled.append(peer_id)
        closed = active_peer == peer_id
        if closed:
            active_peer = None
            print(json.dumps({"event": "stats", "stats": {"peer_connected": False, "peer_connection_state": "closed"}}), flush=True)
        reply(request, {"closed": closed})
    elif request["peer_id"] in cancelled:
        print(json.dumps({"id": request["id"], "error": "cancelled", "kind": "ValueError"}), flush=True)
    elif request["sdp"] == "crash":
        os._exit(7)
    elif request["sdp"] == "signal":
        os.kill(os.getpid(), signal.SIGTERM)
    elif request["sdp"] == "hang":
        time.sleep(60)
    else:
        active_peer = request["peer_id"]
        print(json.dumps({"event": "stats", "stats": {"peer_connected": True, "peer_connection_state": "connected"}}), flush=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
        pending, processing, sequence, timestamp = header.unpack_from(memory)
        if request["sdp"] == "claim":
            header.pack_into(memory, 0, -1, pending, sequence, timestamp)
            processing, pending = pending, -1
        elif request["sdp"] == "release":
            header.pack_into(memory, 0, pending, -1, sequence, timestamp)
            processing = -1
        data = lambda slot: None if slot < 0 else memory[header.size + slot * size:header.size + (slot + 1) * size].hex()
        reply(request, {"type": "answer", "sdp": "fake control answer", "pending": data(pending), "processing": data(processing)})
        fcntl.flock(fd, fcntl.LOCK_UN)
memory.close()
os.close(fd)
"""


class VideoSDKTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.worker = Path(self.directory.name) / "control_worker.py"
        self.worker.write_text(_CONTROL_WORKER)
        self.worker_patch = patch("quest_xr_bridge.video._WORKER_PATH", self.worker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.manager = VideoManager()

    def tearDown(self) -> None:
        self.manager.stop(timeout=0.1)
        self.directory.cleanup()

    def start_fake(self, mode="stereo") -> None:
        with patch("quest_xr_bridge.video._WORKER_PATH", self.worker):
            self.manager.start(VideoConfig(4, 2, mode=mode, worker_python=sys.executable))

    def offer_fake(self, sdp, **kwargs):
        return self.manager.offer(sdp, peer_id=uuid4(), **kwargs)

    def assert_memory_removed(self, name: str) -> None:
        self.assertFalse(Path("/dev/shm", name).exists())
        self.assertIsNone(self.manager._memory)
        self.assertIsNone(self.manager._process)

    def test_repeated_lifecycle_and_json_snapshot(self) -> None:
        for generation in range(1, 3):
            self.start_fake()
            name = self.manager._memory.name
            self.assertTrue(self.manager.snapshot()["running"])
            self.assertEqual(self.manager.snapshot()["stream_generation"], generation)
            self.assertIsNone(self.manager.snapshot()["frame_age_ms"])
            self.manager.set_display(VideoDisplayConfig(swap_eyes=True, saturation=0.9, gamma=1.1))
            json.dumps(self.manager.snapshot())
            self.manager.stop()
            self.assert_memory_removed(name)
            self.assertFalse(self.manager._reader.is_alive())
            self.assertFalse(self.manager._stderr_reader.is_alive())
        self.assertTrue(self.manager.snapshot()["display"]["swap_eyes"])
        self.assertFalse(self.manager.submit(bytes(24), bytes(24)))

    def test_latest_publication_preserves_processing_frame_and_caller_ownership(self) -> None:
        self.start_fake()
        left, right = bytearray(range(24)), bytearray(range(24, 48))
        original = bytes(left + right)
        self.assertTrue(self.manager.submit(left, right, timestamp_ns=1))
        left[:] = right[:] = bytes(24)
        inspect = lambda operation: self.manager._request(
            "inspect", {"sdp": operation, "peer_id": str(uuid4())}, 1
        )
        self.assertEqual(inspect("claim")["processing"], original.hex())
        self.manager.submit(bytes([1]) * 24, bytes([2]) * 24, timestamp_ns=2)
        self.manager.submit(bytes([3]) * 24, bytes([4]) * 24, timestamp_ns=3)
        snapshot = inspect("inspect")
        self.assertEqual(snapshot["processing"], original.hex())
        self.assertEqual(snapshot["pending"], (bytes([3]) * 24 + bytes([4]) * 24).hex())
        self.assertEqual(self.manager.snapshot()["dropped_frames"], 1)
        self.assertIsNotNone(self.manager.snapshot()["frame_age_ms"])
        inspect("release")

    def test_peer_close_rpc_keeps_worker_and_shared_source_and_ignores_old_nonce(self) -> None:
        self.start_fake()
        old, current = uuid4(), uuid4()
        self.manager.offer("inspect", peer_id=old)
        old_process, name = self.manager._process, self.manager._memory.name
        self.manager.submit(bytes(24), bytes(24), timestamp_ns=1)
        generation = self.manager.snapshot()["stream_generation"]
        self.manager.offer("inspect", peer_id=current)
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.manager.offer("inspect", peer_id=old)
        process, name = self.manager._process, self.manager._memory.name
        self.assertNotEqual(process.pid, old_process.pid)
        self.assertEqual(old_process.poll(), 0)
        self.assertEqual(self.manager.snapshot()["submitted_frames"], 1)
        self.assertEqual(self.manager.snapshot()["stream_generation"], generation)
        before = bytes(self.manager._memory.buf)
        self.assertFalse(self.manager.close_peer(old))
        self.assertTrue(self.manager.close_peer(current))
        self.assertFalse(self.manager.snapshot()["peer_connected"])
        self.assertEqual(self.manager.snapshot()["peer_connection_state"], "closed")
        self.assertFalse(self.manager.close_peer(current))
        self.assertIs(self.manager._process, process)
        self.assertEqual(self.manager._memory.name, name)
        self.assertEqual(bytes(self.manager._memory.buf), before)
        self.assertTrue(self.manager.snapshot()["running"])
        self.assertTrue(self.manager.submit(bytes(24), bytes(24), timestamp_ns=2))
        cancelled = uuid4()
        self.assertFalse(self.manager.close_peer(cancelled))
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.manager.offer("inspect", peer_id=cancelled)
        self.manager.offer("inspect", peer_id=uuid4())

    def test_recycle_preserves_source_accepts_submit_and_remembers_cancelled_offer(self):
        self.start_fake()
        self.offer_fake("inspect")
        memory = self.manager._memory
        old_readers = self.manager._reader, self.manager._stderr_reader
        entered, release = threading.Event(), threading.Event()
        spawn = self.manager._spawn_process
        failures = []
        nonce = uuid4()

        def delayed_spawn(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            spawn(*args)

        def offering():
            try:
                self.manager.offer("inspect", peer_id=nonce)
            except Exception as exc:  # noqa: BLE001 - Return a thread failure to the assertions.
                failures.append(exc)

        with patch.object(self.manager, "_spawn_process", side_effect=delayed_spawn):
            thread = threading.Thread(target=offering)
            thread.start()
            self.assertTrue(entered.wait(2))
            snapshot = self.manager.snapshot()
            self.assertTrue(snapshot["running"])
            self.assertFalse(snapshot["worker_running"])
            self.assertIs(self.manager._memory, memory)
            self.assertTrue(all(not reader.is_alive() for reader in old_readers))
            started = time.monotonic()
            self.assertTrue(self.manager.submit(bytes(24), bytes(24), timestamp_ns=10))
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(self.manager.close_peer(nonce))
            with self.assertRaisesRegex(ValueError, "in progress"):
                self.offer_fake("inspect")
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertRegex(str(failures[0]), "cancelled")
        self.assertTrue(self.manager.snapshot()["worker_running"])
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.manager.offer("inspect", peer_id=nonce)
        self.assertTrue(self.manager.submit(bytes(24), bytes(24), timestamp_ns=11))

    def test_stop_during_recycle_prevents_new_child_and_cleans_source(self):
        self.start_fake()
        self.offer_fake("inspect")
        name = self.manager._memory.name
        entered, release = threading.Event(), threading.Event()
        spawn = self.manager._spawn_process
        failures = []

        def delayed_spawn(*args):
            entered.set()
            release.wait(2)
            spawn(*args)

        def offering():
            try:
                self.offer_fake("inspect")
            except Exception as exc:  # noqa: BLE001 - Return a thread failure to the assertions.
                failures.append(exc)

        with patch.object(self.manager, "_spawn_process", side_effect=delayed_spawn):
            thread = threading.Thread(target=offering)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.manager.stop()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(failures[0], VideoUnavailableError)
        self.assert_memory_removed(name)
        self.assertFalse(self.manager.submit(bytes(24), bytes(24)))

    def test_recycle_failure_does_not_hide_worker_crash_or_reuse_its_source(self):
        for fps, expected_error in ((3, "returncode=7"), (4, "native error during stop")):
            with self.subTest(fps=fps):
                self.manager.start(
                    VideoConfig(4, 2, mode="stereo", fps=fps, worker_python=sys.executable)
                )
                self.offer_fake("inspect")
                name, process = self.manager._memory.name, self.manager._process
                readers = self.manager._reader, self.manager._stderr_reader
                with self.assertRaisesRegex(VideoUnavailableError, expected_error):
                    self.offer_fake("inspect")
                self.assert_memory_removed(name)
                self.assertIsNotNone(process.poll())
                self.assertTrue(all(not reader.is_alive() for reader in readers))
                self.assertFalse(self.manager.snapshot()["running"])
                self.assertFalse(self.manager.submit(bytes(24), bytes(24)))

    def test_recycle_spawn_failure_disposes_source_and_requires_explicit_start(self):
        self.start_fake()
        self.offer_fake("inspect")
        name = self.manager._memory.name
        with (
            patch("quest_xr_bridge.video.subprocess.Popen", side_effect=OSError("spawn failed")),
            self.assertRaisesRegex(VideoUnavailableError, "spawn failed"),
        ):
            self.offer_fake("inspect")
        self.assert_memory_removed(name)
        self.assertFalse(self.manager.snapshot()["running"])
        with self.assertRaises(VideoUnavailableError):
            self.offer_fake("inspect")

    def test_recycle_republishes_last_source_after_worker_released_its_slot(self):
        self.start_fake()
        image = bytes(range(24))
        self.manager.submit(image, image, timestamp_ns=100)
        self.manager.offer("claim", peer_id=uuid4())
        self.manager._request("inspect", {"sdp": "release", "peer_id": str(uuid4())}, 1)
        self.assertEqual(struct.unpack_from("<ii", self.manager._memory.buf), (-1, -1))
        answer = self.manager.offer("inspect", peer_id=uuid4())
        self.assertEqual(answer["pending"], (image + image).hex())
        self.assertEqual(self.manager.snapshot()["submitted_frames"], 1)
        with self.assertRaisesRegex(ValueError, "increase monotonically"):
            self.manager.submit(image, image, timestamp_ns=100)

    def test_final_reap_cannot_preserve_source_after_a_natural_nonzero_exit(self):
        for last_poll in (7, None):
            with self.subTest(termination_intent=last_poll is None):
                self.start_fake()
                name, real_process = self.manager._memory.name, self.manager._process
                self.manager._stop_process(1, keep_source=True)
                self.manager._join_readers((self.manager._reader, self.manager._stderr_reader))
                self.assertEqual(real_process.poll(), 0)
                process = Mock(stdin=None, stdout=None, stderr=None, returncode=7)
                process.poll.side_effect = [None, None, None, last_poll]
                self.manager._process = process
                with (
                    patch.object(
                        self.manager, "_request", side_effect=TimeoutError("stop raced exit")
                    ),
                    self.assertRaisesRegex(VideoUnavailableError, "returncode=7"),
                ):
                    self.manager._stop_process(1, keep_source=True)
                self.assert_memory_removed(name)
                self.assertFalse(self.manager.snapshot()["running"])
                self.assertEqual(process.terminate.called, last_poll is None)

    def test_peer_id_validation_precedes_any_worker_request(self) -> None:
        for peer_id in (None, True, 3, b"uuid", "invalid", "x" * 1000):
            with self.subTest(peer_id=str(peer_id)[:40]):
                with self.assertRaises(ValueError):
                    self.manager.offer("fixture", peer_id=peer_id)
                with self.assertRaises(ValueError):
                    self.manager.close_peer(peer_id)
        self.assertFalse(self.manager.close_peer(uuid4()))

    def test_validation_does_not_publish_a_partial_stereo_pair(self) -> None:
        self.start_fake()
        before = bytes(self.manager._memory.buf)
        for left, right, timestamp in (
            (bytes(23), bytes(24), 1),
            (bytes(24), None, 1),
            (memoryview(bytes(48))[::2], bytes(24), 1),
            (bytes(24), bytes(24), float("nan")),
        ):
            with self.assertRaises(ValueError):
                self.manager.submit(left, right, timestamp)
            self.assertEqual(bytes(self.manager._memory.buf), before)
        self.manager.submit(bytes(24), bytes(24), timestamp_ns=2)
        with self.assertRaises(ValueError):
            self.manager.submit(bytes(24), bytes(24), timestamp_ns=2)

    def test_mono_rejects_right_and_configs_reject_nonfinite_or_invalid_sizes(self) -> None:
        self.start_fake("mono")
        with self.assertRaises(ValueError):
            self.manager.submit(bytes(24), bytes(24))
        self.assertTrue(self.manager.submit(bytes(24)))
        for fields in (
            {"width": 3},
            {"fps": True},
            {"fps": 61},
            {"mode": "sbs"},
            {"start_bitrate_mbps": float("inf")},
            {"max_bitrate_mbps": 1},
        ):
            with self.assertRaises(ValueError):
                VideoConfig(**({"width": 4, "height": 2} | fields))
        for fields in (
            {"gamma": 0},
            {"gamma": float("nan")},
            {"saturation": float("inf")},
            {"swap_eyes": 1},
        ):
            with self.assertRaises(ValueError):
                VideoDisplayConfig(**fields)

    def test_passive_crash_reclaims_process_and_shared_memory(self) -> None:
        self.start_fake()
        name = self.manager._memory.name
        with self.assertRaises(VideoUnavailableError):
            self.offer_fake("crash")
        deadline = time.monotonic() + 2
        while self.manager._process is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assert_memory_removed(name)
        self.assertIsNotNone(self.manager.snapshot()["error"])
        self.assertEqual(self.manager.snapshot()["worker_exit_code"], 7)
        self.assertIn("returncode=7", self.manager.snapshot()["error"])
        self.assertFalse(self.manager.snapshot()["worker_exit_forced_cleanup"])

    def test_signal_exit_is_preserved_and_stale_peer_state_is_not_reported_live(self) -> None:
        self.start_fake()
        self.manager._worker_stats = {
            "peer_connected": True,
            "peer_connection_state": "connected",
            "negotiated_codec": "H265",
            "negotiation_stage": "answer_ready",
        }
        with self.assertRaises(VideoUnavailableError):
            self.offer_fake("signal")
        deadline = time.monotonic() + 2
        while self.manager._process is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["worker_exit_code"], -15)
        self.assertIn("SIGTERM", snapshot["error"])
        self.assertFalse(snapshot["peer_connected"])
        self.assertEqual(snapshot["peer_connection_state"], "closed")
        self.assertEqual(snapshot["negotiation_stage"], "stopped")
        self.assertIsNone(snapshot["negotiated_codec"])
        self.assertFalse(snapshot["worker_exit_forced_cleanup"])

    def test_hung_worker_can_be_stopped_after_offer_timeout(self) -> None:
        self.start_fake()
        process, name = self.manager._process, self.manager._memory.name
        with self.assertRaises(TimeoutError):
            self.offer_fake("hang", timeout=0.05)
        self.manager.stop(timeout=0.05)
        self.assertIsNotNone(process.poll())
        self.assert_memory_removed(name)

    def test_stop_reaps_worker_that_acknowledges_but_never_exits(self):
        self.manager.start(VideoConfig(4, 2, fps=5, worker_python=sys.executable))
        name, process = self.manager._memory.name, self.manager._process
        self.manager.stop(timeout=0.05)
        self.assertIsNotNone(process.poll())
        self.assert_memory_removed(name)
        self.assertTrue(self.manager.snapshot()["worker_exit_forced_cleanup"])

    def test_native_dependency_failure_has_no_resources_left(self) -> None:
        with (
            patch(
                "quest_xr_bridge.video._WORKER_PATH",
                Path(__file__).resolve().parents[1] / "quest_xr_bridge/video_worker.py",
            ),
            patch.dict(
                os.environ,
                {
                    "GST_PLUGIN_SYSTEM_PATH_1_0": "",
                    "GST_PLUGIN_PATH_1_0": "",
                    "GST_REGISTRY": str(Path(self.directory.name) / "empty-gst-registry.bin"),
                },
            ),
            self.assertRaises(VideoUnavailableError),
        ):
            self.manager.start(VideoConfig(1280, 720, mode="stereo"), timeout=5)
        self.assertIsNone(self.manager._process)
        self.assertIsNone(self.manager._memory)
        self.assertFalse(self.manager.snapshot()["running"])
        self.assertTrue(self.manager.snapshot()["error"])

    def test_webrtc_guard_checks_loaded_plugin_marker_without_rejecting_compatible_core(
        self,
    ) -> None:
        plugin = SimpleNamespace(
            is_loaded=lambda: True,
            get_version=lambda: "1.24.13",
            get_package=lambda: "GStreamer Bad Plug-ins (quest-crt DTLS owner fix)",
        )
        loaded = SimpleNamespace(get_plugin=lambda: plugin)
        registry = Mock()
        registry.load.return_value = loaded
        Gst = SimpleNamespace(
            ElementFactory=SimpleNamespace(find=lambda _name: registry),
            version=lambda: (1, 24, 2, 0),
        )
        verify_webrtc_plugin(Gst)
        registry.load.assert_called_once()
        registry.get_plugin.assert_not_called()

    def test_webrtc_guard_rejects_old_stock_or_unloaded_native_plugin(self) -> None:
        for version, package, loaded in (
            ("1.24.12", "quest-crt DTLS owner fix", True),
            ("1.24.13", "GStreamer Bad Plug-ins", True),
            ("1.24.13", "quest-crt DTLS owner fix", False),
            ("unknown", "quest-crt DTLS owner fix", True),
        ):
            with self.subTest(version=version, package=package, loaded=loaded):
                plugin = SimpleNamespace(
                    is_loaded=lambda loaded=loaded: loaded,
                    get_version=lambda version=version: version,
                    get_package=lambda package=package: package,
                )
                feature = SimpleNamespace(get_plugin=lambda plugin=plugin: plugin)
                Gst = SimpleNamespace(
                    ElementFactory=SimpleNamespace(
                        find=lambda _name, feature=feature: SimpleNamespace(load=lambda: feature)
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "deploy the patched native runtime"):
                    verify_webrtc_plugin(Gst)
        Gst = SimpleNamespace(
            ElementFactory=SimpleNamespace(find=lambda _name: SimpleNamespace(load=lambda: None))
        )
        with self.assertRaisesRegex(RuntimeError, "loaded webrtc plugin"):
            verify_webrtc_plugin(Gst)

    def test_video_platform_requirement_is_checked_when_started(self) -> None:
        with (
            patch("quest_xr_bridge.video.sys.platform", "win32"),
            self.assertRaisesRegex(VideoUnavailableError, "requires Linux"),
        ):
            self.manager.start(VideoConfig(4, 2))
        self.assertFalse(self.manager.submit(bytes(24)))
        self.assertIsNone(self.manager._memory)
        self.assertIsNone(self.manager._process)

    def test_start_timeout_kills_worker_that_ignores_termination(self) -> None:
        with (
            patch("quest_xr_bridge.video._WORKER_PATH", self.worker),
            self.assertRaises(VideoUnavailableError),
        ):
            self.manager.start(VideoConfig(4, 2, fps=1, worker_python=sys.executable), timeout=0.05)
        self.assertIsNone(self.manager._memory)
        self.assertIsNone(self.manager._process)
        self.assertFalse(self.manager._reader.is_alive())
        self.assertFalse(self.manager._stderr_reader.is_alive())

    def test_control_pipe_write_has_a_deadline(self) -> None:
        with patch("quest_xr_bridge.video._WORKER_PATH", self.worker):
            self.manager.start(VideoConfig(4, 2, fps=2, worker_python=sys.executable))
        name = self.manager._memory.name
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.offer_fake("x" * 200_000, timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assert_memory_removed(name)

    def test_concurrent_stop_does_not_deadlock_a_blocked_control_writer(self) -> None:
        with patch("quest_xr_bridge.video._WORKER_PATH", self.worker):
            self.manager.start(VideoConfig(4, 2, fps=2, worker_python=sys.executable))
        name = self.manager._memory.name
        failures = []
        stop_failures = []

        def blocked_offer():
            try:
                self.offer_fake("x" * 200_000, timeout=0.1)
            except Exception as exc:  # noqa: BLE001 - Pass thread failures to the test's assertions.
                failures.append(exc)

        offering = threading.Thread(target=blocked_offer)
        offering.start()
        time.sleep(0.02)

        def stopping_worker():
            try:
                self.manager.stop(timeout=0.15)
            except Exception as exc:  # noqa: BLE001 - Pass thread failures to the test's assertions.
                stop_failures.append(exc)

        stopping = threading.Thread(target=stopping_worker)
        stopping.start()
        offering.join(2)
        stopping.join(2)
        self.assertFalse(offering.is_alive())
        self.assertFalse(stopping.is_alive())
        self.assertTrue(failures)
        self.assertEqual(stop_failures, [])
        self.assert_memory_removed(name)

    def test_stereo_rows_and_h264_levels_preserve_requested_geometry(self) -> None:
        left = bytes(range(12))
        right = bytes(range(12, 24))
        self.assertIsInstance(pack_stereo(left + right, 2, 2), bytes)
        self.assertEqual(
            bytes(pack_stereo(left + right, 2, 2)), left[:6] + right[:6] + left[6:] + right[6:]
        )
        self.assertEqual(choose_level(640, 480, 30, 8, 16, receiver_level=31), ("3.1", 14))
        self.assertEqual(choose_level(2560, 720, 60, 8, 16), ("4.2", 16))
        self.assertEqual(choose_level(2556, 360, 60, 8, 16), ("4", 16))
        with self.assertRaises(ValueError):
            choose_level(2560, 720, 60, 8, 16, receiver_level=31)

    def test_rejected_offer_preserves_an_existing_peer_or_pending_negotiation(self) -> None:
        worker = VideoWorker.__new__(VideoWorker)
        worker.pending_offer = 1
        worker.peer = existing_peer = object()
        worker.offer = Mock(side_effect=ValueError("negotiation already in progress"))
        worker._close_peer = Mock()
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            worker.command({"id": 2, "op": "offer", "sdp": "rejected"})
        worker._close_peer.assert_not_called()
        self.assertIs(worker.peer, existing_peer)
        self.assertEqual(worker.pending_offer, 1)
        self.assertEqual(reply.call_args.args[0]["id"], 2)
        worker.pending_offer = 2
        with patch("quest_xr_bridge.video_worker.emit"):
            worker.command({"id": 2, "op": "offer", "sdp": "rejected"})
        worker._close_peer.assert_called_once()

    def callback_worker(self):
        worker = VideoWorker.__new__(VideoWorker)
        queued = []
        worker.GLib = SimpleNamespace(
            PRIORITY_DEFAULT=0,
            idle_add=lambda callback, *args, **kwargs: queued.append((callback, args)),
            source_remove=Mock(),
        )
        worker.Gst = SimpleNamespace(
            State=SimpleNamespace(NULL="null"),
            StateChangeReturn=SimpleNamespace(FAILURE="failure"),
        )
        worker.pipeline = worker.peer = worker.source = worker.encoder = worker.gcc = None
        worker._peer_generation = 1
        worker._peer_lock = threading.RLock()
        worker._signal_handlers = []
        worker._pending_promise = None
        worker._offer_timer = None
        worker._connection_timer = None
        worker.peer_nonce = None
        worker._cancelled_peer_ids = deque(maxlen=64)
        worker.pending_offer = None
        worker.negotiated_codec = None
        worker.frames_sent = 0
        worker.estimated_bitrate = 8_000_000
        worker.level = "4.1"
        worker.bitrate_limit = 16
        worker.last_capture = None
        worker.negotiation_stage = "idle"
        worker._stage_started = time.monotonic()
        worker.config = {"start_bitrate_mbps": 8}
        worker.loop = Mock()
        worker.GLib.timeout_add = Mock(return_value=456)
        worker.GstWebRTC = SimpleNamespace(
            WebRTCPeerConnectionState=SimpleNamespace(
                CONNECTED="connected", DISCONNECTED="disconnected", FAILED="failed", CLOSED="closed"
            )
        )
        return worker, queued

    def test_late_bitrate_notify_is_applied_on_glib_only_to_its_encoder(self) -> None:
        worker, queued = self.callback_worker()
        worker.encoder = old_encoder = Mock()
        worker.bitrate_limit = 16
        worker.estimated_bitrate = 8_000_000
        worker.gcc = gcc = Mock()
        gcc.get_property.return_value = 6_000_000
        worker._bitrate_changed(gcc, None, 1)
        old_encoder.set_property.assert_not_called()
        worker.encoder = new_encoder = Mock()
        worker.gcc = new_gcc = Mock()
        new_gcc.get_property.return_value = 6_000_000
        worker._peer_generation = 2
        callback, args = queued.pop()
        self.assertEqual(args, (1,))
        self.assertFalse(callback(*args))
        old_encoder.set_property.assert_not_called()
        new_encoder.set_property.assert_not_called()
        self.assertEqual(worker.estimated_bitrate, 8_000_000)
        worker._bitrate_changed(gcc, None, 1)
        self.assertEqual(queued, [])
        worker._bitrate_changed(new_gcc, None, 2)
        callback, args = queued.pop()
        self.assertFalse(callback(*args))
        new_encoder.set_property.assert_called_once_with("bitrate", 6000)

    def test_late_ice_notify_cannot_answer_a_replacement_peer(self) -> None:
        worker, queued = self.callback_worker()
        worker.GstWebRTC.WebRTCICEGatheringState = SimpleNamespace(COMPLETE="complete")
        worker.peer = old_peer = Mock()
        worker.pending_offer = 1
        worker._ice_changed(old_peer, None, 1)
        self.assertEqual(worker.pending_offer, 1)
        worker.peer = new_peer = Mock()
        worker._peer_generation = 2
        worker.pending_offer = 2
        worker._offer_timer = 123
        description = SimpleNamespace(sdp=SimpleNamespace(as_text=lambda: "new answer"))
        new_peer.get_property.side_effect = lambda name: {
            "ice-gathering-state": "complete",
            "local-description": description,
            "connection-state": "new",
        }[name]
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            callback, args = queued.pop()
            self.assertEqual(args, (1,))
            self.assertFalse(callback(*args))
            reply.assert_not_called()
            old_peer.get_property.assert_not_called()
            self.assertEqual(worker.pending_offer, 2)
            worker._ice_changed(new_peer, None, 2)
            worker._ice_changed(new_peer, None, 2)
            for callback, args in queued:
                self.assertFalse(callback(*args))
            reply.assert_called_once_with(
                {"id": 2, "result": {"type": "answer", "sdp": "new answer"}}
            )
            worker.GLib.source_remove.assert_called_once_with(123)
            self.assertIsNone(worker._offer_timer)

    def test_old_peer_cannot_request_gcc_for_a_new_encoder(self) -> None:
        worker, _queued = self.callback_worker()
        worker.peer = new_peer = object()
        worker.encoder = Mock()
        worker.bitrate_limit = 16
        gcc = Mock()
        factory = Mock(return_value=gcc)
        worker.Gst = SimpleNamespace(ElementFactory=SimpleNamespace(make=factory))
        self.assertIsNone(worker._create_gcc(object(), None, 1))
        factory.assert_not_called()

        def reconnect_during_factory(_name):
            worker.peer = object()
            worker.encoder = Mock()
            worker._peer_generation += 1
            return gcc

        factory.side_effect = reconnect_during_factory
        self.assertIsNone(worker._create_gcc(new_peer, None, 1))
        gcc.connect.assert_not_called()

    def test_current_gcc_signal_owns_only_generation_and_keeps_bitrate_limit(self) -> None:
        worker, queued = self.callback_worker()
        worker.peer = peer = Mock()
        worker.encoder = encoder = Mock()
        worker.bitrate_limit = 16
        gcc = Mock()
        gcc.connect.return_value = 42
        gcc.get_property.return_value = 32_000_000
        worker.Gst.ElementFactory = SimpleNamespace(make=Mock(return_value=gcc))
        self.assertIs(worker._create_gcc(peer, None, 1), gcc)
        gcc.connect.assert_called_once_with("notify::estimated-bitrate", worker._bitrate_changed, 1)
        self.assertEqual(worker._signal_handlers, [(gcc, 42)])
        self.assertIs(worker.gcc, gcc)
        worker._bitrate_changed(gcc, None, 1)
        callback, args = queued.pop()
        self.assertEqual(args, (1,))
        self.assertFalse(callback(*args))
        encoder.set_property.assert_called_once_with("bitrate", 16000)
        worker._close_peer()
        gcc.disconnect.assert_called_once_with(42)

    def test_current_promise_accepts_another_boxed_wrapper_and_advances_negotiation(self) -> None:
        worker, queued = self.callback_worker()
        worker.peer = peer = Mock()
        worker.pending_offer = 1
        promises, callbacks = [], []

        def new_promise(callback, *_):
            callbacks.append(callback)
            promise = Mock()
            promises.append(promise)
            return promise

        worker.Gst.Promise = SimpleNamespace(new_with_change_func=new_promise)
        worker._new_promise(worker._remote_set, 1)
        # GI can wrap the same GstMiniObject in another Python boxed object.
        callback_wrapper = Mock()
        callback_wrapper.get_reply.return_value = None
        callbacks[0](callback_wrapper)
        callback, args = queued.pop()
        self.assertFalse(callback(*args))
        self.assertEqual(len(promises), 2)
        self.assertIs(worker._pending_promise, promises[1])
        peer.emit.assert_called_once_with("create-answer", None, promises[1])
        worker._close_peer()
        promises[0].interrupt.assert_not_called()
        promises[1].interrupt.assert_called_once()

    def test_close_disconnects_signals_interrupts_promise_and_cancels_timer_before_null(
        self,
    ) -> None:
        worker, _queued = self.callback_worker()
        operations = []
        worker.pipeline = pipeline = Mock()
        worker.peer = peer = Mock()
        worker.encoder = Mock()
        worker.gcc = gcc = Mock()
        bus = pipeline.get_bus.return_value
        for obj, handler in ((peer, 11), (gcc, 12), (bus, 13)):
            obj.disconnect.side_effect = lambda handler: operations.append(("disconnect", handler))
            worker._signal_handlers.append((obj, handler))
        worker._pending_promise = promise = Mock()
        promise.interrupt.side_effect = lambda: operations.append(("interrupt",))
        worker._offer_timer = 14
        worker.GLib.source_remove.side_effect = lambda timer: operations.append(("timer", timer))
        pipeline.set_state.side_effect = lambda state: operations.append(("state", state))
        self.assertFalse(worker._close_peer())
        self.assertEqual(operations[-1], ("state", "null"))
        self.assertEqual(
            operations[:-1],
            [
                ("timer", 14),
                ("disconnect", 11),
                ("disconnect", 12),
                ("disconnect", 13),
                ("interrupt",),
            ],
        )
        self.assertEqual(worker._peer_generation, 2)
        self.assertEqual(worker._signal_handlers, [])
        for field in ("pipeline", "peer", "encoder", "gcc", "_pending_promise", "_offer_timer"):
            self.assertIsNone(getattr(worker, field))
        bus.remove_signal_watch.assert_called_once()
        worker._close_peer()
        promise.interrupt.assert_called_once()
        pipeline.set_state.assert_called_once_with("null")

    def test_promise_callbacks_do_not_hold_old_peer_or_change_new_negotiation(self) -> None:
        worker, queued = self.callback_worker()

        class Peer:
            pass

        worker.peer = old_peer = Peer()
        old_peer_ref = weakref.ref(old_peer)
        callbacks = []
        promise = Mock()
        worker.Gst.Promise = SimpleNamespace(
            new_with_change_func=lambda callback, *_: callbacks.append(callback) or promise
        )
        worker._new_promise(worker._remote_set, 1)
        del old_peer
        worker._close_peer()
        self.assertIsNone(old_peer_ref())
        worker.peer = new_peer = Mock()
        worker._pending_promise = current_promise = Mock()
        callbacks[0](promise)
        callback, args = queued.pop()
        self.assertEqual(args, (promise, 1))
        self.assertFalse(callback(*args))
        for callback in (worker._remote_set, worker._answer_created, worker._local_set):
            self.assertFalse(callback(promise, 1))
        promise.get_reply.assert_not_called()
        new_peer.emit.assert_not_called()
        self.assertIs(worker._pending_promise, current_promise)

    def test_old_bus_and_connection_callbacks_cannot_stop_new_peer(self) -> None:
        worker, queued = self.callback_worker()
        worker._peer_generation = 2
        worker.peer = new_peer = Mock()
        worker._fatal = Mock()
        worker._close_peer = Mock()
        message = Mock()
        worker._bus_error(Mock(), message, 1)
        message.parse_error.assert_not_called()
        worker._fatal.assert_not_called()
        worker._connection_changed(Mock(), None, 1)
        self.assertEqual(queued, [])
        self.assertFalse(worker._apply_connection_state(1))
        worker._close_peer.assert_not_called()
        new_peer.get_property.assert_not_called()

    def test_failed_negotiation_replies_once_before_closing_and_cancelling_timeout(self) -> None:
        for name in ("failed", "closed", "disconnected"):
            with self.subTest(state=name):
                worker, queued = self.callback_worker()
                state = SimpleNamespace(value_nick=name)
                worker.GstWebRTC = SimpleNamespace(
                    WebRTCPeerConnectionState=SimpleNamespace(
                        FAILED=state, CLOSED=state, DISCONNECTED=state, CONNECTED="connected"
                    )
                )
                worker.peer = peer = Mock()
                peer.get_property.return_value = state
                worker.pending_offer = 17
                worker._offer_timer = 123
                worker._connection_changed(peer, None, 1)
                callback, args = queued.pop()
                with patch("quest_xr_bridge.video_worker.emit") as reply:
                    self.assertFalse(callback(*args))
                    self.assertFalse(callback(*args))
                reply.assert_called_once_with(
                    {
                        "id": 17,
                        "error": f"video peer {name} during negotiation",
                        "kind": "ValueError",
                    }
                )
                self.assertIsNone(worker.peer)
                self.assertIsNone(worker.pending_offer)
                worker.GLib.source_remove.assert_called_once_with(123)

    def test_close_current_pending_peer_replies_error_then_releases_media_without_stopping_worker(
        self,
    ) -> None:
        worker, _queued = self.callback_worker()
        worker.peer_nonce = nonce = str(uuid4())
        worker.peer = Mock()
        worker.source = Mock()
        worker.encoder = Mock()
        worker.gcc = Mock()
        worker.pipeline = pipeline = Mock()
        worker.pending_offer = 17
        worker._offer_timer, worker._connection_timer = 123, 456
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            worker.command({"id": 18, "op": "close_peer", "peer_id": nonce})
        self.assertEqual(reply.call_count, 3)
        self.assertEqual(
            reply.call_args_list[0].args[0],
            {"id": 17, "error": "video peer closed during negotiation", "kind": "ValueError"},
        )
        self.assertEqual(reply.call_args_list[1].args[0]["event"], "stats")
        self.assertFalse(reply.call_args_list[1].args[0]["stats"]["peer_connected"])
        self.assertEqual(reply.call_args_list[2].args[0], {"id": 18, "result": {"closed": True}})
        self.assertIsNone(worker.source)
        self.assertIsNone(worker.encoder)
        self.assertIsNone(worker.gcc)
        self.assertIsNone(worker.peer_nonce)
        self.assertIn(nonce, worker._cancelled_peer_ids)
        self.assertEqual(
            [call.args[0] for call in worker.GLib.source_remove.call_args_list], [123, 456]
        )
        pipeline.set_state.assert_called_once_with("null")
        worker.loop.quit.assert_not_called()

    def test_old_peer_close_cannot_close_replacement_and_duplicate_nonce_cannot_replace_it(
        self,
    ) -> None:
        worker, _queued = self.callback_worker()
        worker.peer_nonce = current = str(uuid4())
        old = str(uuid4())
        worker.pipeline = pipeline = Mock()
        worker.peer = peer = Mock()
        worker._parse_offer = Mock()
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            worker.command({"id": 9, "op": "close_peer", "peer_id": old})
        reply.assert_called_once_with({"id": 9, "result": {"closed": False}})
        self.assertIs(worker.peer, peer)
        self.assertEqual(worker.peer_nonce, current)
        pipeline.set_state.assert_not_called()
        for nonce, message in ((old, "cancelled"), (current, "unique")):
            with self.assertRaisesRegex(ValueError, message):
                worker.offer({"peer_id": nonce, "type": "offer", "sdp": "not parsed"})
        worker._parse_offer.assert_not_called()

    def test_close_before_offer_is_rejected_and_cancelled_nonce_storage_is_bounded(self) -> None:
        worker, _queued = self.callback_worker()
        ids = [str(uuid4()) for _ in range(80)]
        with patch("quest_xr_bridge.video_worker.emit"):
            for index, nonce in enumerate(ids):
                worker.command({"id": index + 1, "op": "close_peer", "peer_id": nonce})
        self.assertEqual(list(worker._cancelled_peer_ids), ids[-64:])
        worker._remember_cancelled(ids[-1])
        self.assertEqual(len(worker._cancelled_peer_ids), 64)
        worker._parse_offer = Mock()
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            worker.command(
                {"id": 81, "op": "offer", "peer_id": ids[-1], "type": "offer", "sdp": "late"}
            )
        self.assertEqual(reply.call_args.args[0]["kind"], "ValueError")
        worker._parse_offer.assert_not_called()
        self.assertIsNone(worker.peer)

    def test_connected_cancels_deadline_and_stale_deadline_cannot_close_new_peer(self) -> None:
        worker, _queued = self.callback_worker()
        worker.peer = peer = Mock()
        peer.get_property.return_value = "connected"
        worker._connection_timer = 456
        worker._close_peer = Mock()
        self.assertFalse(worker._apply_connection_state(1))
        worker.GLib.source_remove.assert_called_once_with(456)
        self.assertIsNone(worker._connection_timer)
        worker._connection_timer = 789
        worker._peer_generation = 2
        self.assertFalse(worker._connection_timeout(1))
        self.assertEqual(worker._connection_timer, 789)
        worker._close_peer.assert_not_called()

    def test_answer_without_connected_receiver_is_reclaimed_at_deadline(self) -> None:
        worker, _queued = self.callback_worker()
        worker.peer_nonce = nonce = str(uuid4())
        worker.peer = peer = Mock()
        peer.get_property.return_value = "connecting"
        worker.source = Mock()
        worker.pipeline = pipeline = Mock()
        worker._connection_timer = 456
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            self.assertFalse(worker._connection_timeout(1))
        self.assertIsNone(worker.peer)
        self.assertIsNone(worker.source)
        self.assertIn(nonce, worker._cancelled_peer_ids)
        self.assertIsNone(worker._connection_timer)
        worker.GLib.source_remove.assert_not_called()
        pipeline.set_state.assert_called_once_with("null")
        worker.loop.quit.assert_not_called()
        reply.assert_not_called()

    def test_replacement_pipeline_restarts_frame_clock_without_resetting_total_frames(self) -> None:
        worker, _queued = self.callback_worker()
        worker.first_timestamp, worker.last_capture = 1_000, 2_000
        worker.next_push = 5_000.0
        worker.frames_sent = 10
        worker._close_peer()
        self.assertIsNone(worker.first_timestamp)
        self.assertIsNone(worker.last_capture)
        self.assertEqual(worker.next_push, 0)
        self.assertEqual(worker.frames_sent, 10)
        worker.config = {"mode": "mono", "width": 2}
        worker.width, worker.height, worker.fps, worker.slot_size = 2, 2, 60, 12
        header = struct.Struct("<iiQQ")
        worker.memory = bytearray(header.size + worker.slot_size * 2)
        buffers = [SimpleNamespace(fill=Mock()), SimpleNamespace(fill=Mock())]
        worker.Gst.Buffer = SimpleNamespace(new_allocate=Mock(side_effect=buffers))
        worker.Gst.SECOND = 1_000_000_000
        worker.Gst.FlowReturn = SimpleNamespace(OK="ok", FLUSHING="flushing")
        worker.GstVideo = SimpleNamespace(
            buffer_add_video_meta_full=Mock(),
            VideoFrameFlags=SimpleNamespace(NONE=0),
            VideoFormat=SimpleNamespace(RGB="rgb"),
        )
        worker.source = Mock()
        worker.source.emit.return_value = "ok"
        with tempfile.TemporaryFile() as memory_file:
            worker.fd = memory_file.fileno()
            for index, timestamp in enumerate((2_000_000_000, 2_016_666_666)):
                header.pack_into(worker.memory, 0, 0, -1, index + 1, timestamp)
                with patch("quest_xr_bridge.video_worker.time.monotonic", return_value=100 + index):
                    self.assertTrue(worker._pump())
        self.assertEqual((buffers[0].pts, buffers[0].dts), (0, 0))
        self.assertEqual((buffers[1].pts, buffers[1].dts), (16_666_666, 16_666_666))
        self.assertEqual(worker.last_capture, 2_016_666_666)
        self.assertEqual(worker.frames_sent, 12)

    def test_timeout_clears_its_source_before_close_without_cancelling_new_timer(self) -> None:
        worker, _queued = self.callback_worker()
        worker.pending_offer = 7
        worker.negotiation_stage = "set_remote_wait_promise"
        worker._offer_timer = 123
        self.assertFalse(worker._offer_timeout(6, 0))
        self.assertEqual(worker._offer_timer, 123)
        worker.GLib.source_remove.assert_not_called()
        with patch("quest_xr_bridge.video_worker.emit") as reply:
            self.assertFalse(worker._offer_timeout(7, 1))
        self.assertIsNone(worker._offer_timer)
        worker.GLib.source_remove.assert_not_called()
        self.assertEqual(reply.call_args.args[0]["kind"], "TimeoutError")

    def test_null_failure_is_fatal_and_cannot_be_silently_replaced(self) -> None:
        worker, _queued = self.callback_worker()
        worker.pipeline = pipeline = Mock()
        pipeline.set_state.return_value = "failure"
        with (
            patch("quest_xr_bridge.video_worker.emit") as reply,
            self.assertRaisesRegex(RuntimeError, "failed to enter NULL"),
        ):
            worker._close_peer()
        worker.loop.quit.assert_called_once()
        self.assertEqual(reply.call_args.args[0]["event"], "error")

    def video_offer_parser(self, fmtp: str, *, twcc=True, feedback=True, codec="H264", payload=96):
        attrs = [("rtpmap", f"{payload} {codec}/90000"), ("fmtp", f"{payload} " + fmtp)]
        if twcc:
            attrs.append(
                (
                    "extmap",
                    "3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
                )
            )
        if feedback:
            attrs.append(("rtcp-fb", f"{payload} transport-cc"))
        media = SimpleNamespace(
            get_media=lambda: "video",
            attributes_len=lambda: len(attrs),
            get_attribute=lambda index: SimpleNamespace(key=attrs[index][0], value=attrs[index][1]),
        )
        description = SimpleNamespace(medias_len=lambda: 1, get_media=lambda _index: media)
        worker = VideoWorker.__new__(VideoWorker)
        worker.GstSdp = SimpleNamespace(
            SDPMessage=SimpleNamespace(new=lambda: (0, description)),
            SDPResult=SimpleNamespace(OK=0),
            sdp_message_parse_buffer=lambda *_: 0,
        )
        worker.width, worker.height, worker.fps = 2560, 720, 60
        worker.config = {"start_bitrate_mbps": 8, "max_bitrate_mbps": 16}
        return worker

    def test_declared_max_recv_level_allows_higher_receive_level(self) -> None:
        worker = self.video_offer_parser(
            "profile-level-id=42e01f;packetization-mode=1;max-recv-level=e02a"
        )
        with patch("quest_xr_bridge.video_worker.sys.stderr"):
            self.assertEqual(worker._parse_offer("fixture")[3:], ("4.2", 16, "H264"))

    def test_malformed_or_incompatible_max_recv_level_is_rejected(self) -> None:
        for maximum in ("2a", "zz2a", "002a", "e01f", "e0ff"):
            with self.subTest(maximum=maximum):
                worker = self.video_offer_parser(
                    "profile-level-id=42e01f;packetization-mode=1;max-recv-level=" + maximum
                )
                with (
                    patch("quest_xr_bridge.video_worker.sys.stderr"),
                    self.assertRaisesRegex(ValueError, "max-recv-level"),
                ):
                    worker._parse_offer("fixture")

    def test_plain_level_31_and_missing_twcc_have_distinct_diagnostics(self) -> None:
        worker = self.video_offer_parser(
            "profile-level-id=42e01f;packetization-mode=1;max-fs=7200;max-mbps=432000"
        )
        with (
            patch("quest_xr_bridge.video_worker.sys.stderr"),
            self.assertRaisesRegex(ValueError, "highest level 3.1.*2560x720@60.*4.2"),
        ):
            worker._parse_offer("fixture")

    def test_hevc_main_declared_level_150_carries_sbs720p60(self) -> None:
        worker = self.video_offer_parser(
            "profile-id=1;tier-flag=0;level-id=150;tx-mode=SRST", codec="H265", payload=35
        )
        with patch("quest_xr_bridge.video_worker.sys.stderr"):
            self.assertEqual(worker._parse_offer("fixture")[3:], ("4.1", 16, "H265"))
        self.assertEqual(choose_hevc_level(2560, 720, 60, 8, 16, 150), ("4.1", 16))
        self.assertEqual(hevc_level_id("4.1"), 123)

    def test_low_payload_restore_uses_public_property_before_rtp_buffers(self) -> None:
        payloader = Mock()
        pad = SimpleNamespace(get_parent_element=lambda: payloader)
        Gst = SimpleNamespace(
            EventType=SimpleNamespace(CAPS="caps"), PadProbeReturn=SimpleNamespace(OK="ok")
        )
        info = SimpleNamespace(get_event=lambda: SimpleNamespace(type="caps"))
        self.assertEqual(restore_payload_on_caps(pad, info, 49, Gst), "ok")
        payloader.set_property.assert_called_once_with("pt", 49)
        info = SimpleNamespace(get_event=lambda: SimpleNamespace(type="segment"))
        restore_payload_on_caps(pad, info, 49, Gst)
        payloader.set_property.assert_called_once()

    def test_answer_requires_a_real_sendonly_sender_and_selected_payload(self) -> None:
        for direction, port, payloads, accepted in (
            ("sendonly", 9, ["49"], True),
            ("inactive", 9, ["49"], False),
            ("recvonly", 9, ["49"], False),
            ("sendonly", 0, ["49"], False),
            ("sendonly", 9, ["96"], False),
        ):
            media = SimpleNamespace(
                get_media=lambda: "video",
                get_port=lambda port=port: port,
                attributes_len=lambda: 1,
                get_attribute=lambda _, direction=direction: SimpleNamespace(key=direction),
                formats_len=lambda payloads=payloads: len(payloads),
                get_format=lambda index, payloads=payloads: payloads[index],
            )
            description = SimpleNamespace(
                medias_len=lambda: 1, get_media=lambda _, media=media: media
            )
            if accepted:
                validate_send_answer(description, 49)
            else:
                with self.assertRaises(ValueError):
                    validate_send_answer(description, 49)

    def test_hevc_rejects_insufficient_or_invalid_profile_tier_and_level(self) -> None:
        for fmtp, reason in (
            ("profile-id=2;tier-flag=0;level-id=150", "8-bit Main"),
            ("profile-id=1;tier-flag=1;level-id=150", "Main tier"),
            ("profile-id=1;tier-flag=0;level-id=120", "insufficient"),
            ("profile-id=1;tier-flag=0;level-id=93", "insufficient"),
            ("profile-id=1;tier-flag=0;level-id=1.50", "decimal integer"),
            ("profile-id=1;tier-flag=0;level-id=255", "level_idc"),
            ("profile-id=1;tier-flag=0;level-id=150;tx-mode=MRST", "SRST"),
        ):
            with self.subTest(fmtp=fmtp):
                worker = self.video_offer_parser(fmtp, codec="H265")
                with (
                    patch("quest_xr_bridge.video_worker.sys.stderr"),
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    worker._parse_offer("fixture")
        worker = self.video_offer_parser("profile-level-id=42e02a;packetization-mode=1", twcc=False)
        with self.assertRaisesRegex(ValueError, "TWCC RTP header extension"):
            worker._parse_offer("fixture")
        worker = self.video_offer_parser(
            "profile-level-id=42e02a;packetization-mode=1", feedback=False
        )
        with (
            patch("quest_xr_bridge.video_worker.sys.stderr"),
            self.assertRaisesRegex(ValueError, "RTCP transport-cc feedback"),
        ):
            worker._parse_offer("fixture")


if __name__ == "__main__":
    unittest.main()
