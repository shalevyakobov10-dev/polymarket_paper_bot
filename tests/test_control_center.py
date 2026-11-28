import unittest

from control_center import CONTROL_CENTER_HTML


class ControlCenterReadinessTests(unittest.TestCase):
    def test_live_readiness_card_uses_read_only_execution_readiness_endpoint(self):
        self.assertIn('id="card-execution-readiness"', CONTROL_CENTER_HTML)
        self.assertIn('url:"/execution-readiness"', CONTROL_CENTER_HTML)
        self.assertIn('id==="execution-readiness"', CONTROL_CENTER_HTML)
        self.assertIn("Live execution readiness", CONTROL_CENTER_HTML)
        self.assertIn("Perps live release gates", CONTROL_CENTER_HTML)

    def test_dashboard_displays_adapter_and_shared_risk_blockers(self):
        self.assertIn("Event gateway present / integrated", CONTROL_CENTER_HTML)
        self.assertIn("Perps gateway present / integrated", CONTROL_CENTER_HTML)
        self.assertIn("Shared admission wired to both engines", CONTROL_CENTER_HTML)
        self.assertIn("Combined daily loss enforced", CONTROL_CENTER_HTML)
        self.assertIn("Combined drawdown enforced", CONTROL_CENTER_HTML)
        self.assertIn("Runtime geoblock checked by this endpoint", CONTROL_CENTER_HTML)


if __name__ == "__main__":
    unittest.main()
