from __future__ import annotations

import unittest

from quest_crt.transport_session import (
    TRANSPORT_SESSION_ID_MAX_LEN,
    TransportSessionError,
    TransportSessionManager,
    validate_transport_session_id,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ValidateTransportSessionIdTests(unittest.TestCase):
    def test_accepts_uuid_style_id(self) -> None:
        validate_transport_session_id("2f7c1d6e-9a4b-4f5d-b3c2-8a1e0d9f6a71")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(TransportSessionError):
            validate_transport_session_id("")
        with self.assertRaises(TransportSessionError):
            validate_transport_session_id("   ")

    def test_rejects_oversized(self) -> None:
        with self.assertRaises(TransportSessionError):
            validate_transport_session_id("x" * (TRANSPORT_SESSION_ID_MAX_LEN + 1))

    def test_rejects_special_characters(self) -> None:
        with self.assertRaises(TransportSessionError):
            validate_transport_session_id("bad id/with/slashes")
        with self.assertRaises(TransportSessionError):
            validate_transport_session_id("bad中文id")


class TransportSessionManagerTests(unittest.TestCase):
    def test_begin_end_happy_path(self) -> None:
        clock = FakeClock()
        manager = TransportSessionManager(clock=clock)
        lease = manager.begin_channel("ts-1", "pose", "quest-a")
        self.assertEqual(lease.channel, "pose")
        self.assertEqual(lease.generation, 1)
        self.assertEqual(manager.describe()["active_sessions"], 1)
        self.assertTrue(manager.end_channel(lease))

        # The session entry lingers empty until the lease window passes.
        self.assertEqual(manager.describe()["active_sessions"], 1)
        self.assertEqual(manager.describe()["sessions"][0]["channels"], [])
        clock.advance(11.0)
        manager.begin_channel("ts-probe", "pose", "quest-a")
        self.assertEqual(manager.describe()["active_sessions"], 1)

    def test_superseded_close_cannot_evict_replacement(self) -> None:
        clock = FakeClock()
        manager = TransportSessionManager(clock=clock)
        first = manager.begin_channel("ts-1", "pose", "quest-a")
        second = manager.begin_channel("ts-1", "pose", "quest-a")
        self.assertEqual(first.generation, 1)
        self.assertEqual(second.generation, 2)

        # The superseded connection's delayed close removes only its own
        # token; the replacement's attachment stays attached.
        self.assertTrue(manager.end_channel(first))
        self.assertEqual(manager.describe()["active_sessions"], 1)
        self.assertEqual(
            manager.describe()["sessions"][0]["channels"], ["pose"]
        )

        self.assertTrue(manager.end_channel(second))
        self.assertEqual(manager.describe()["sessions"][0]["channels"], [])

    def test_end_channel_twice_is_stale_on_second_call(self) -> None:
        manager = TransportSessionManager(clock=FakeClock())
        lease = manager.begin_channel("ts-1", "pose", "quest-a")
        self.assertTrue(manager.end_channel(lease))
        self.assertFalse(manager.end_channel(lease))

    def test_end_channel_keeps_other_channel_attached(self) -> None:
        clock = FakeClock()
        manager = TransportSessionManager(clock=clock)
        pose = manager.begin_channel("ts-1", "pose", "quest-a")
        video = manager.begin_channel("ts-1", "video", "quest-a")
        self.assertTrue(manager.end_channel(pose))
        self.assertEqual(manager.describe()["active_sessions"], 1)
        self.assertEqual(
            manager.describe()["sessions"][0]["channels"], ["video"]
        )
        self.assertTrue(manager.end_channel(video))
        self.assertEqual(manager.describe()["sessions"][0]["channels"], [])

    def test_unknown_channel_is_rejected(self) -> None:
        manager = TransportSessionManager(clock=FakeClock())
        with self.assertRaises(TransportSessionError):
            manager.begin_channel("ts-1", "depth", "quest-a")  # type: ignore[arg-type]

    def test_reap_removes_only_empty_expired_sessions(self) -> None:
        clock = FakeClock(start=100.0)
        manager = TransportSessionManager(lease_ms=10_000, clock=clock)
        stale = manager.begin_channel("ts-stale", "pose", "quest-a")
        self.assertTrue(manager.end_channel(stale))
        live = manager.begin_channel("ts-live", "pose", "quest-a")

        clock.advance(20.0)
        # A live session with an attached channel must survive the lease window.
        manager.begin_channel("ts-probe", "pose", "quest-a")
        self.assertEqual(manager.describe()["active_sessions"], 2)

        self.assertTrue(manager.end_channel(live))
        # The emptied live session lingers until its lease window passes.
        self.assertEqual(manager.describe()["active_sessions"], 2)
        clock.advance(11.0)
        manager.begin_channel("ts-probe-2", "pose", "quest-a")
        # ts-live (empty, expired) was reaped during the attach sweep.
        self.assertEqual(manager.describe()["active_sessions"], 2)

    def test_touch_refreshes_last_seen_prevents_reap(self) -> None:
        clock = FakeClock(start=0.0)
        manager = TransportSessionManager(lease_ms=10_000, clock=clock)
        lease = manager.begin_channel("ts-1", "pose", "quest-a")
        self.assertTrue(manager.end_channel(lease))  # session now empty
        clock.advance(9.0)
        manager.touch("ts-1")  # refresh lease on the empty session
        clock.advance(9.0)  # 18s since attach, only 9s since touch
        manager.begin_channel("ts-2", "pose", "quest-a")  # triggers reap
        self.assertEqual(manager.describe()["active_sessions"], 2)
        clock.advance(2.0)  # 11s since touch → lease expired
        manager.begin_channel("ts-3", "pose", "quest-a")
        # ts-1 was reaped; ts-2 (channel alive) and ts-3 remain.
        self.assertEqual(manager.describe()["active_sessions"], 2)

    def test_reconnect_within_lease_reuses_session_entry(self) -> None:
        clock = FakeClock(start=0.0)
        manager = TransportSessionManager(lease_ms=10_000, clock=clock)
        first = manager.begin_channel("ts-1", "pose", "quest-a")
        self.assertTrue(manager.end_channel(first))
        clock.advance(0.5)  # reconnect within the lease window
        second = manager.begin_channel("ts-1", "pose", "quest-a")
        self.assertEqual(second.generation, 2)
        self.assertEqual(manager.describe()["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
