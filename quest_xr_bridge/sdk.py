"""Embed Quest XR Bridge in a Python host with an explicit lifetime."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from quest_xr_bridge.runtime import PoseRuntime
from quest_xr_bridge.server import create_app
from quest_xr_bridge.video import VideoConfig, VideoDisplayConfig, VideoManager

logger = logging.getLogger(__name__)


def get_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return "127.0.0.1"


def ensure_certificate(
    lan_ip: str, cert_file: Path, key_file: Path, *, external: bool = False
) -> None:
    """Keep supplied TLS files intact; otherwise maintain a development certificate."""
    if external:
        for path in (cert_file, key_file):
            if not path.is_file():
                raise FileNotFoundError(f"configured TLS file not found: {path}")
        return
    if key_file.is_file() and cert_file.is_file():
        try:
            cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
            addresses = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName,
            ).value.get_values_for_type(x509.IPAddress)
            if ipaddress.ip_address(
                lan_ip
            ) in addresses and cert.not_valid_after_utc > datetime.now(UTC):
                return
        except (ValueError, x509.ExtensionNotFound):
            pass
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "quest-xr-bridge.local")])
    now = datetime.now(UTC)
    addresses = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address(lan_ip)}
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("quest-xr-bridge.local"),
                    *[x509.IPAddress(address) for address in addresses],
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    # Create the key with private permissions before writing its bytes.
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.chmod(key_file, 0o600)
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


class QuestServer:
    """Synchronous controls; HTTP/pose run in a dedicated background event loop.

    Control methods are called serially by the host. submit_video is safe from
    camera threads. Use stop() or a context manager, rather than relying on GC.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        *,
        record_poses: bool = False,
        cert_file: str | Path | None = None,
        key_file: str | Path | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a nonempty address")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if bool(cert_file) != bool(key_file):
            raise ValueError("cert_file and key_file must be supplied together")
        self.host, self.port = host, port
        self.record_poses = bool(record_poses)
        self.data_dir = Path(data_dir) if data_dir is not None else Path.cwd()
        self.cert_file = Path(cert_file) if cert_file else self.data_dir / "certs" / "cert.pem"
        self.key_file = Path(key_file) if key_file else self.data_dir / "certs" / "key.pem"
        self._external_tls = cert_file is not None
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._error: BaseException | None = None
        self._socket: socket.socket | None = None
        self._url: str | None = None
        self._runtime: PoseRuntime | None = None
        self._recording_fd: int | None = None
        self._video = VideoManager()
        self._finished = threading.Event()

    @property
    def running(self) -> bool:
        return bool(
            self._thread and self._thread.is_alive() and self._server and self._server.started
        )

    @property
    def url(self) -> str:
        if self._url is None or not self.running:
            raise RuntimeError("QuestServer is not running")
        return self._url

    def start(self, timeout: float = 10) -> QuestServer:
        timeout = _timeout(timeout)
        if self.running:
            return self
        if self._thread is not None:
            self.stop(timeout)
        if self.host in {"0.0.0.0", "::"}:
            address = get_lan_ip()
        else:
            try:
                address = str(ipaddress.ip_address(self.host))
            except ValueError:
                address = socket.gethostbyname(self.host)
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        listener = socket.create_server(
            (self.host, self.port),
            family=family,
            dualstack_ipv6=family == socket.AF_INET6 and self.host == "::",
        )
        try:
            ensure_certificate(address, self.cert_file, self.key_file, external=self._external_tls)
            if self.record_poses:
                import fcntl

                directory = self.data_dir / "logs"
                directory.mkdir(parents=True, exist_ok=True)
                fd = os.open(directory / ".recording.lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    os.close(fd)
                    raise RuntimeError(
                        "another recorder owns this log directory; use a separate data_dir"
                    ) from exc
                self._recording_fd = fd
            runtime = PoseRuntime(record_poses=self.record_poses, log_dir=self.data_dir / "logs")
            config = uvicorn.Config(
                create_app(runtime, self._video),
                host=self.host,
                port=listener.getsockname()[1],
                ssl_certfile=str(self.cert_file),
                ssl_keyfile=str(self.key_file),
                log_config=None,
                lifespan="on",
                timeout_graceful_shutdown=2,
            )
            self._server = uvicorn.Server(config)
            self._runtime, self._socket = runtime, listener
            self._error = None
            self._finished.clear()
            self._thread = threading.Thread(target=self._run, name="quest-xr-bridge-server", daemon=False)
            self._thread.start()
        except BaseException:
            listener.close()
            self._socket = None
            self._release_recording()
            raise
        deadline = time.monotonic() + timeout
        while not self.running:
            if self._finished.wait(0.01):
                error = self._error or RuntimeError("QuestServer stopped during startup")
                self.stop(timeout)
                if isinstance(error, (SystemExit, KeyboardInterrupt)):
                    raise RuntimeError("QuestServer startup failed") from error
                raise error
            if time.monotonic() >= deadline:
                self.stop(timeout)
                raise TimeoutError("QuestServer startup timed out")
        url_host = f"[{address}]" if ":" in address else address
        self._url = f"https://{url_host}:{listener.getsockname()[1]}"
        return self

    def _run(self) -> None:
        server, listener = self._server, self._socket

        async def serve() -> None:
            self._loop = asyncio.get_running_loop()
            await server.serve(sockets=[listener])

        try:
            asyncio.run(serve())
        except BaseException as exc:  # noqa: BLE001 - Return thread failures, including SystemExit, to the host.
            self._error = exc
            logger.error("QuestServer failed: %s", exc)
        finally:
            if listener is not None:
                listener.close()
            self._loop = None
            self._finished.set()

    def stop(self, timeout: float = 10) -> None:
        timeout = _timeout(timeout)
        thread, server = self._thread, self._server
        if thread is not None and thread.is_alive():
            server.should_exit = True
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("QuestServer is still stopping; call stop again to wait")
        self._video.stop(timeout=timeout)
        if self._socket is not None:
            self._socket.close()
        self._release_recording()
        self._thread = self._server = self._socket = self._runtime = None
        self._url = None

    def _release_recording(self) -> None:
        if self._recording_fd is not None:
            os.close(self._recording_fd)
            self._recording_fd = None

    def start_video(self, config: VideoConfig, timeout: float = 10) -> None:
        if not self.running:
            raise RuntimeError("start QuestServer before video")
        self._video.start(config, timeout=_timeout(timeout))

    def submit_video(
        self, left_rgb: object, right_rgb: object = None, *, timestamp_ns: int | None = None
    ) -> bool:
        return self._video.submit(left_rgb, right_rgb, timestamp_ns=timestamp_ns)

    def set_video_display(self, config: VideoDisplayConfig) -> None:
        self._video.set_display(config)

    def stop_video(self, timeout: float = 10) -> None:
        self._video.stop(timeout=_timeout(timeout))

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()
