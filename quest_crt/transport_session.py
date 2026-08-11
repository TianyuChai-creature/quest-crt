"""Transport-session ownership and lifecycle for Quest channels.

Phase 0 of the stereo-vision revision (see ``docs/stage1-revise.md``):

* ``transport_session_id``: page/Quest-client lifetime identity. Carries the
  pose WebRTC connection, the pose WSS fallback, and (Phase 1+) the video
  WebRTC connection. Generated at page initialization on the Quest side.
* ``pose_stream_id`` (the existing ``PoseFrame.session_id``): per-XR-session
  pose stream identity. Not owned by this module; left untouched.

Design rules:

* Every channel attachment gets a unique ``ChannelLease`` token. A stale
  close callback from a superseded PeerConnection can only remove its own
  token, so it can never evict the replacement connection's entry.
* ``generation`` is a per-channel monotonic counter used for observability;
  correctness comes from the opaque token.
* ``end_channel`` releases a token but keeps the (now empty) session entry
  until ``lease_ms`` of inactivity passes and a later ``begin_channel`` or
  ``describe()`` lazily reaps it. A client that closes and reconnects within
  the lease window therefore reuses one stable session entry instead of
  churning a fresh one each time.
* The single-control-source gate stays in ``ActivePoseSource`` (server.py).
  This manager only owns transport-session bookkeeping and leases.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Literal

ChannelKind = Literal["pose", "video"]

TRANSPORT_SESSION_ID_MAX_LEN = 64
TRANSPORT_SESSION_LEASE_MS = 10_000
_CHANNELS: tuple[ChannelKind, ...] = ("pose", "video")


class TransportSessionError(ValueError):
    """Raised for a malformed transport_session_id or an unknown channel."""


def validate_transport_session_id(transport_session_id: str) -> None:
    if not isinstance(transport_session_id, str) or not transport_session_id:
        raise TransportSessionError("transport_session_id is required")
    if len(transport_session_id) > TRANSPORT_SESSION_ID_MAX_LEN:
        raise TransportSessionError(
            f"transport_session_id too long: {len(transport_session_id)} "
            f"(max {TRANSPORT_SESSION_ID_MAX_LEN})"
        )
    if not all(
        char.isascii() and (char.isalnum() or char in "-_") for char in transport_session_id
    ):
        raise TransportSessionError(
            "transport_session_id contains invalid characters"
        )


class ChannelLease:
    """Opaque handle proving one attachment of one channel to a session."""

    __slots__ = ("transport_session_id", "channel", "token", "generation")

    def __init__(
        self,
        transport_session_id: str,
        channel: ChannelKind,
        token: str,
        generation: int,
    ) -> None:
        self.transport_session_id = transport_session_id
        self.channel = channel
        self.token = token
        self.generation = generation


class _SessionEntry:
    """One transport session and its per-channel attachments."""

    __slots__ = (
        "transport_session_id",
        "channels",
        "generations",
        "last_seen_monotonic_ms",
        "created_at_monotonic_ms",
        "client_name",
    )

    def __init__(
        self,
        transport_session_id: str,
        client_name: str,
        now_monotonic_ms: float,
    ) -> None:
        self.transport_session_id = transport_session_id
        self.channels: dict[ChannelKind, set[str]] = {
            "pose": set(),
            "video": set(),
        }
        self.generations: dict[ChannelKind, int] = {"pose": 0, "video": 0}
        self.last_seen_monotonic_ms = now_monotonic_ms
        self.created_at_monotonic_ms = now_monotonic_ms
        self.client_name = client_name

    def empty(self) -> bool:
        return not any(self.channels.values())

    def channels_in_use(self) -> list[str]:
        return [channel for channel, tokens in self.channels.items() if tokens]


class TransportSessionManager:
    """Thread-safe ownership and lease tracking keyed by transport_session_id."""

    def __init__(
        self,
        lease_ms: float = TRANSPORT_SESSION_LEASE_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # time.monotonic() returns seconds; keep the public knob in ms.
        self._lease_seconds = lease_ms / 1000.0
        self._lease_ms = lease_ms
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionEntry] = {}

    def begin_channel(
        self,
        transport_session_id: str,
        channel: ChannelKind,
        client_name: str,
    ) -> ChannelLease:
        """Attach one channel to a session and return its unique lease."""
        validate_transport_session_id(transport_session_id)
        if channel not in _CHANNELS:
            raise TransportSessionError(f"unknown channel: {channel!r}")
        now = self._clock()
        with self._lock:
            self._reap_expired_locked(now)
            entry = self._sessions.get(transport_session_id)
            if entry is None:
                entry = _SessionEntry(transport_session_id, client_name, now)
                self._sessions[transport_session_id] = entry
            token = uuid.uuid4().hex
            entry.channels[channel].add(token)
            entry.generations[channel] += 1
            entry.last_seen_monotonic_ms = now
            entry.client_name = client_name
            return ChannelLease(
                transport_session_id, channel, token, entry.generations[channel]
            )

    def end_channel(self, lease: ChannelLease) -> bool:
        """Release one channel attachment.

        The session entry stays in place (empty) until the lease window
        passes and a later sweep reaps it — a reconnecting client keeps a
        stable session identity.

        Returns ``False`` when the lease is stale (already released or the
        session entry no longer owns this token) — callers should treat that
        as "nothing to clean up", never as a session teardown signal.
        """
        with self._lock:
            entry = self._sessions.get(lease.transport_session_id)
            if entry is None:
                return False
            tokens = entry.channels.get(lease.channel)
            if tokens is None or lease.token not in tokens:
                return False
            tokens.remove(lease.token)
            entry.last_seen_monotonic_ms = self._clock()
            return True

    def touch(self, transport_session_id: str) -> None:
        """Refresh a session's lease (call on each accepted pose frame)."""
        with self._lock:
            entry = self._sessions.get(transport_session_id)
            if entry is not None:
                entry.last_seen_monotonic_ms = self._clock()

    def describe(self) -> dict[str, Any]:
        """Health-surface summary of all known sessions."""
        now = self._clock()
        with self._lock:
            self._reap_expired_locked(now)
            sessions = []
            for entry in self._sessions.values():
                sessions.append(
                    {
                        "transport_session_id": entry.transport_session_id,
                        "client": entry.client_name,
                        "channels": entry.channels_in_use(),
                        "generations": dict(entry.generations),
                        "idle_for_ms": max(
                            0.0, (now - entry.last_seen_monotonic_ms) * 1000
                        ),
                        "created_for_ms": max(
                            0.0, (now - entry.created_at_monotonic_ms) * 1000
                        ),
                    }
                )
            sessions.sort(key=lambda item: item["created_for_ms"], reverse=True)
            return {
                "active_sessions": len(sessions),
                "lease_ms": self._lease_ms,
                "sessions": sessions,
            }

    def _reap_expired_locked(self, now: float) -> None:
        for tsid, entry in list(self._sessions.items()):
            if (
                entry.empty()
                and now - entry.last_seen_monotonic_ms >= self._lease_seconds
            ):
                self._sessions.pop(tsid, None)
