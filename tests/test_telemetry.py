import unittest

from quest_xr_bridge.telemetry import IngressTelemetry, build_health_report, format_status_line


class TelemetryTests(unittest.TestCase):
    def test_ingress_clear_and_fresh_statistics(self):
        stats = IngressTelemetry()
        stats.record(
            fps=72, seq=10, transport="webrtc", ingress_drop=1, log_drop=2, effective_loss_pct=0.5
        )
        self.assertEqual(stats.describe()["seq"], 10)
        self.assertFalse(stats.describe()["stats_stale"])
        stats.clear()
        self.assertTrue(stats.describe()["stats_stale"])

    def test_pose_health_reports_stale_without_video_or_fixed_stream(self):
        report = build_health_report(
            latest_pose_snapshot=(1, {"seq": 1}, 10),
            active_source={"active": True},
            event_loop_lag={"recent_p99_ms": 1},
            ingress={"fps": 72},
            pose_log_enabled=False,
            now=10.3,
        )
        self.assertFalse(report["healthy"])
        self.assertAlmostEqual(report["latest_pose"]["age_ms"], 300)
        self.assertNotIn("stable_stream", report)
        self.assertNotIn("video", report)
        self.assertIn("fps=72.0", format_status_line(report))
        report = build_health_report(
            latest_pose_snapshot=(0, None, None),
            active_source={"active": False},
            event_loop_lag={},
            ingress={},
            pose_log_enabled=False,
        )
        self.assertTrue(report["healthy"])
