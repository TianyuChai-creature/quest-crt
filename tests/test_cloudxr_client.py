from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.prepare_cloudxr_client import INJECTION_MARKER, prepare_client
from server import app, ensure_certificate


class CloudXRClientTests(unittest.TestCase):
    def test_prepare_injects_before_official_bundle_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "official"
            output = root / "generated"
            source.mkdir()
            original = '<html><head><script defer="defer" src="bundle.js"></script></head></html>'
            (source / "index.html").write_text(original, encoding="utf-8")
            (source / "bundle.js").write_bytes(b"official")
            (source / "bundle.emulator.js").write_bytes(b"emulator")

            prepare_client(source, output)

            generated = (output / "index.html").read_text(encoding="utf-8")
            self.assertLess(generated.index(INJECTION_MARKER), generated.index('src="bundle.js"'))
            self.assertIn("qcrt-entry", generated)
            self.assertIn("qcrt-video-toggle", generated)
            self.assertIn('panelHiddenAtStart: "true"', generated)
            self.assertEqual((source / "index.html").read_text(encoding="utf-8"), original)

    def test_pose_entry_makes_video_optional(self) -> None:
        index = (Path(__file__).parents[1] / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('<input id="video-mode" type="checkbox" />', index)
        self.assertIn(":48322/client/", index)
        self.assertIn('enterButton.addEventListener("click", enterXR)', index)

    def test_cloudxr_origin_can_preflight_webrtc_offer(self) -> None:
        messages: list[dict[str, object]] = []
        request_sent = False

        async def receive() -> dict[str, object]:
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "OPTIONS",
            "scheme": "https",
            "path": "/api/webrtc/offer",
            "raw_path": b"/api/webrtc/offer",
            "query_string": b"",
            "headers": [
                (b"origin", b"https://192.168.8.122:48322"),
                (b"access-control-request-method", b"POST"),
                (b"access-control-request-headers", b"content-type"),
            ],
            "client": ("192.168.8.222", 12345),
            "server": ("192.168.8.122", 8000),
        }
        asyncio.run(app(scope, receive, send))

        start = next(message for message in messages if message["type"] == "http.response.start")
        headers = dict(start["headers"])
        self.assertEqual(start["status"], 200)
        self.assertEqual(headers[b"access-control-allow-origin"], b"https://192.168.8.122:48322")

    def test_external_tls_files_are_reused_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "server.crt"
            key = Path(directory) / "server.key"
            cert.write_text("cloudxr-cert", encoding="utf-8")
            key.write_text("cloudxr-key", encoding="utf-8")
            with (
                patch("server.POSE_CERT_FILE", str(cert)),
                patch("server.CERT_FILE", cert),
                patch("server.KEY_FILE", key),
            ):
                ensure_certificate("192.168.8.122")
            self.assertEqual(cert.read_text(encoding="utf-8"), "cloudxr-cert")
            self.assertEqual(key.read_text(encoding="utf-8"), "cloudxr-key")


if __name__ == "__main__":
    unittest.main()
