"""UDP transport adapter for StablePoseStream (same binary envelope as /ws/stream)."""

from __future__ import annotations

import queue
import socket
import threading
from typing import TYPE_CHECKING

from quest_crt.stream_protocol import encode_stream_envelope

if TYPE_CHECKING:
    from quest_crt.stable_stream import StreamBus


class UdpStreamPublisher:
    """Subscribe to StreamBus and send binary envelopes to host:port."""

    def __init__(
        self,
        bus: StreamBus,
        *,
        host: str = "127.0.0.1",
        port: int = 9100,
    ) -> None:
        self._bus = bus
        self.host = str(host)
        self.port = int(port)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._q: queue.Queue | None = None
        self._sent = 0
        self._errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._q = self._bus.subscribe()
        self._thread = threading.Thread(
            target=self._run, name="stable-stream-udp", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._q is not None:
            self._bus.unsubscribe(self._q)
            self._q = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def describe(self) -> dict:
        return {
            "enabled": True,
            "host": self.host,
            "port": self.port,
            "sent": self._sent,
            "errors": self._errors,
        }

    def _run(self) -> None:
        assert self._q is not None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Best-effort larger buffer; ignore failures.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            except OSError:
                pass
            addr = (self.host, self.port)
            while not self._stop.is_set():
                try:
                    envelope = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    packet = encode_stream_envelope(envelope)
                    sock.sendto(packet, addr)
                    self._sent += 1
                except Exception:
                    self._errors += 1
        finally:
            sock.close()
