from datetime import datetime, timezone
import unittest

from app import health_payload


class PerpsHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.market = {
            "worker_running": True,
            "last_success_at": self.now.isoformat(),
        }

    def perps_status(self, **updates):
        status = {
            "worker_state": "running",
            "last_success_at": self.now.isoformat(),
            "heartbeat_fresh": True,
            "cycles": 1,
            "open_positions": 0,
            "research_only": True,
            "live_execution_enabled": False,
        }
        status.update(updates)
        return status

    def test_enabled_perps_worker_is_a_required_health_component(self):
        healthy, payload = health_payload(
            self.market,
            now=self.now,
            perps_worker_snapshot=self.perps_status(),
        )
        self.assertTrue(healthy)
        self.assertIn("perps_paper_worker", payload["required_components"])
        self.assertEqual(payload["components"]["perps_paper_worker"]["status"], "healthy")

    def test_stale_perps_heartbeat_fails_readiness(self):
        healthy, payload = health_payload(
            self.market,
            now=self.now,
            perps_worker_snapshot=self.perps_status(heartbeat_fresh=False),
        )
        self.assertFalse(healthy)
        component = payload["components"]["perps_paper_worker"]
        self.assertEqual(component["status"], "stale")
        self.assertIn("perps_paper_worker", payload["required_component_failures"])

    def test_protective_exit_block_degrades_required_service_health(self):
        healthy, payload = health_payload(
            self.market,
            now=self.now,
            perps_worker_snapshot=self.perps_status(
                worker_state="degraded",
                last_cycle_warning="protective_exit_blocked",
                exit_blocks_last_cycle=1,
                last_exit_block_reason="insufficient_visible_depth",
            ),
        )
        self.assertFalse(healthy)
        component = payload["components"]["perps_paper_worker"]
        self.assertEqual(component["status"], "degraded")
        self.assertEqual(component["exit_blocks_last_cycle"], 1)
        self.assertEqual(component["last_exit_block_reason"], "insufficient_visible_depth")

    def test_disabled_perps_worker_is_not_required(self):
        healthy, payload = health_payload(self.market, now=self.now)
        self.assertTrue(healthy)
        self.assertNotIn("perps_paper_worker", payload["components"])
        self.assertNotIn("perps_paper_worker", payload["required_components"])


if __name__ == "__main__":
    unittest.main()
