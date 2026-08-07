from __future__ import annotations

import time
import unittest

from quest_crt.stable_stream import StreamBus, StreamClock, StreamEnvelope


class StreamBusTests(unittest.TestCase):
    def test_capacity_one_keeps_latest(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e1 = StreamEnvelope(1, 1, 1, 0.0, 0.0, "webrtc", "ok", {"seq": 1})
        e2 = StreamEnvelope(2, 2, 2, 1.0, 0.0, "webrtc", "ok", {"seq": 2})
        bus.publish(e1)
        bus.publish(e2)
        self.assertEqual(q.get_nowait().stream_seq, 2)
        self.assertTrue(q.empty())


class StreamClockTests(unittest.TestCase):
    def test_quality_ok_held_stale_lost(self) -> None:
        state: dict = {
            "gen": 0,
            "frame": None,
            "published_at": None,
        }

        def snapshot():
            return state["gen"], state["frame"], state["published_at"]

        def build(frame: dict) -> dict:
            return {"seq": frame["seq"], "hands": frame.get("hands", {})}

        clock = StreamClock(
            snapshot_pose=snapshot,
            build_output=build,
            hz=100.0,
            hold_ms=100.0,
            lost_ms=500.0,
        )

        # no pose
        env = clock.build_envelope(now=1000.0)
        self.assertEqual(env.quality, "lost")

        # fresh pose → ok
        state["gen"] = 1
        state["frame"] = {"seq": 10, "ingress_transport": "webrtc", "hands": {}}
        state["published_at"] = 1000.0
        env = clock.build_envelope(now=1000.02)
        self.assertEqual(env.quality, "ok")
        self.assertEqual(env.pose_seq, 10)
        self.assertEqual(env.ingress_transport, "webrtc")

        # same generation shortly after → held
        env = clock.build_envelope(now=1000.05)
        self.assertEqual(env.quality, "held")

        # age past hold, before lost → stale
        env = clock.build_envelope(now=1000.0 + 0.2)
        self.assertEqual(env.quality, "stale")

        # age past lost → lost
        env = clock.build_envelope(now=1000.0 + 0.6)
        self.assertEqual(env.quality, "lost")
        self.assertIsNone(env.pose)

    def test_clock_publishes_to_subscriber(self) -> None:
        published_at = time.monotonic()
        frame = {"seq": 1, "ingress_transport": "webrtc", "hands": {}}

        def snapshot():
            return 1, frame, published_at

        clock = StreamClock(
            snapshot_pose=snapshot,
            build_output=lambda f: {"seq": f["seq"]},
            hz=50.0,
            hold_ms=100.0,
            lost_ms=500.0,
        )
        q = clock.bus.subscribe()
        clock.start()
        try:
            env = q.get(timeout=1.0)
            self.assertIsInstance(env, StreamEnvelope)
            self.assertGreaterEqual(env.stream_seq, 1)
            self.assertIn(env.quality, ("ok", "held", "stale"))
        finally:
            clock.stop()
            clock.bus.unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
