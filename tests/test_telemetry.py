from __future__ import annotations

import unittest

from quest_crt.telemetry import (
    IngressTelemetry,
    build_health_report,
    format_status_line,
)


class IngressTelemetryTests(unittest.TestCase):
    def test_record_and_describe(self) -> None:
        tel = IngressTelemetry()
        tel.record(
            fps=72.5,
            seq=10,
            transport="webrtc",
            ingress_drop=1,
            log_drop=2,
            effective_loss_pct=0.5,
        )
        d = tel.describe()
        self.assertEqual(d["transport"], "webrtc")
        self.assertAlmostEqual(d["fps"], 72.5)
        self.assertEqual(d["ingress_drop"], 1)
        self.assertFalse(d["stats_stale"])
        tel.clear()
        self.assertTrue(tel.describe()["stats_stale"])


class HealthReportTests(unittest.TestCase):
    def test_webrtc_healthy(self) -> None:
        report = build_health_report(
            latest_pose_snapshot=(
                3,
                {"seq": 9, "ingress_transport": "webrtc"},
                1000.0,
            ),
            active_source={"active": True, "transport": "webrtc", "client": "q"},
            event_loop_lag={"recent_p99_ms": 2.0, "maximum_ms": 5.0, "stalls_over_50ms": 0},
            stable_stream={
                "stream_hz": 72,
                "out_fps": 71.5,
                "subscribers": 1,
                "quality_ratio": {"ok": 0.8, "held": 0.15, "stale": 0.05, "lost": 0.0},
                "last_quality": "ok",
            },
            ingress={
                "fps": 72.0,
                "transport": "webrtc",
                "ingress_drop": 0,
                "log_drop": 0,
            },
            pose_log_enabled=False,
            now=1000.01,
        )
        self.assertTrue(report["healthy"])
        self.assertFalse(report["ingress_degraded"])
        self.assertIn("held_ratio", report["stable_stream"])
        self.assertEqual(report["ingress"]["preferred_transport"], "webrtc")

    def test_wss_warns_and_unhealthy(self) -> None:
        report = build_health_report(
            latest_pose_snapshot=(
                1,
                {"seq": 1, "ingress_transport": "wss"},
                1000.0,
            ),
            active_source={"active": True, "transport": "wss"},
            event_loop_lag={"recent_p99_ms": 1.0, "maximum_ms": 2.0, "stalls_over_50ms": 0},
            stable_stream={
                "stream_hz": 72,
                "out_fps": 72.0,
                "subscribers": 0,
                "quality_ratio": {},
            },
            ingress={"fps": 60.0, "transport": "wss", "ingress_drop": 0, "log_drop": 0},
            pose_log_enabled=True,
            now=1000.01,
        )
        self.assertTrue(report["ingress_degraded"])
        self.assertFalse(report["healthy"])
        self.assertTrue(any("wss" in w for w in report["warnings"]))
        self.assertTrue(any("POSE_LOG" in w for w in report["warnings"]))
        line = format_status_line(report)
        self.assertIn("[status]", line)
        self.assertIn("DEGRADED", line)


if __name__ == "__main__":
    unittest.main()
