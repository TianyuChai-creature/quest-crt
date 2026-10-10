from __future__ import annotations

import json
import logging
import signal
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from quest_xr_bridge import QuestServer, VideoConfig, VideoUnavailableError


def health(service):
    context = ssl._create_unverified_context()
    with urllib.request.urlopen(service.url + "/health", context=context, timeout=3) as response:
        return json.load(response)


class SDKTests(unittest.TestCase):
    def test_concurrent_recorders_cannot_delete_each_others_active_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            first = QuestServer(host="127.0.0.1", port=0, data_dir=directory, record_poses=True)
            second = QuestServer(host="127.0.0.1", port=0, data_dir=directory, record_poses=True)
            with first:
                with self.assertRaisesRegex(RuntimeError, "separate data_dir"):
                    second.start()
                self.assertTrue(first.running)
                self.assertFalse(second.running)
                second.stop()
            with second:
                self.assertTrue(second.running)

    def test_context_exception_stops_server_and_releases_port(self):
        with tempfile.TemporaryDirectory() as directory:
            service = QuestServer(host="127.0.0.1", port=0, data_dir=directory)
            self.assertFalse(service.running)
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaisesRegex(RuntimeError, "host exception"), service:
                self.assertTrue(health(service)["healthy"])
                port = service._socket.getsockname()[1]
                self.assertIs(service.start(), service)
                raise RuntimeError("host exception")
            self.assertFalse(service.running)
            service.stop()
            with socket.create_server(("127.0.0.1", port)):
                pass
            self.assertFalse(any(t.name == "quest-xr-bridge-server" for t in threading.enumerate()))

    def test_repeated_start_is_clean_and_does_not_change_host_signals_or_logging(self):
        before_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        before_logging = {
            name: (
                logging.getLogger(name).level,
                list(logging.getLogger(name).handlers),
                logging.getLogger(name).propagate,
            )
            for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access")
        }
        with tempfile.TemporaryDirectory() as directory:
            service = QuestServer(host="127.0.0.1", port=0, data_dir=directory)
            for _ in range(2):
                service.start()
                self.assertFalse(health(service)["active_source"]["active"])
                self.assertIsNone(health(service)["latest_pose"]["seq"])
                service.stop()
        self.assertEqual(before_signals, {sig: signal.getsignal(sig) for sig in before_signals})
        after_logging = {
            name: (
                logging.getLogger(name).level,
                list(logging.getLogger(name).handlers),
                logging.getLogger(name).propagate,
            )
            for name in before_logging
        }
        self.assertEqual(before_logging, after_logging)

    def test_port_conflict_is_returned_without_startup_side_effects(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            socket.create_server(("127.0.0.1", 0)) as occupied,
        ):
            service = QuestServer(
                host="127.0.0.1", port=occupied.getsockname()[1], data_dir=directory
            )
            with self.assertRaises(OSError):
                service.start()
            self.assertFalse(service.running)
            self.assertEqual(list(Path(directory).iterdir()), [])
            service.stop()

    def test_invalid_tls_start_failure_leaves_no_worker_or_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert", Path(directory) / "key"
            cert.write_text("invalid cert")
            key.write_text("invalid key")
            service = QuestServer(host="127.0.0.1", port=0, cert_file=cert, key_file=key)
            with self.assertRaises(ssl.SSLError):
                service.start()
            self.assertFalse(service.running)
            self.assertIsNone(service._thread)
            self.assertEqual(cert.read_text(), "invalid cert")

    def test_video_error_does_not_stop_pose_service(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            QuestServer(host="127.0.0.1", port=0, data_dir=directory) as service,
        ):
            with (
                patch.object(
                    service._video, "start", side_effect=VideoUnavailableError("no encoder")
                ),
                self.assertRaises(VideoUnavailableError),
            ):
                service.start_video(VideoConfig(width=1280, height=720, mode="stereo"))
            self.assertTrue(service.running)
            self.assertTrue(health(service)["healthy"])
