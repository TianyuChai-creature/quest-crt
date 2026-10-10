from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class CertificateTests(unittest.TestCase):
    def test_external_tls_files_are_reused_without_overwrite(self) -> None:
        from quest_xr_bridge.sdk import ensure_certificate

        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "external-cert.pem"
            key = Path(directory) / "external-key.pem"
            cert.write_text("external-cert", encoding="utf-8")
            key.write_text("external-key", encoding="utf-8")
            ensure_certificate("192.168.1.4", cert, key, external=True)
            self.assertEqual(cert.read_text(encoding="utf-8"), "external-cert")
            self.assertEqual(key.read_text(encoding="utf-8"), "external-key")

    def test_missing_external_tls_file_does_not_replace_existing_file(self) -> None:
        from quest_xr_bridge.sdk import ensure_certificate

        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "external-cert.pem"
            key = Path(directory) / "missing-key.pem"
            cert.write_text("external-cert", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                ensure_certificate("192.168.1.4", cert, key, external=True)
            self.assertEqual(cert.read_text(encoding="utf-8"), "external-cert")
            self.assertFalse(key.exists())


if __name__ == "__main__":
    unittest.main()
