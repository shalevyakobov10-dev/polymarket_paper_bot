"""Guardrails for the checked-in environment template.

Duplicate dotenv assignments are especially dangerous for safety settings:
python-dotenv uses the last assignment, which can silently override the
operator-facing value documented earlier in the file.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"


def parse_environment_template(path: Path = ENV_EXAMPLE) -> dict[str, str]:
    values: dict[str, str] = {}
    duplicates: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise AssertionError(f"empty environment key on line {line_number}")
        if key in values:
            duplicates.append(f"{key} (line {line_number})")
        values[key] = value.strip()
    if duplicates:
        raise AssertionError("duplicate environment assignments: " + ", ".join(duplicates))
    return values


class EnvironmentTemplateTests(unittest.TestCase):
    def test_environment_template_has_no_duplicate_assignments(self):
        values = parse_environment_template()
        self.assertTrue(values)

    def test_perps_defaults_are_explicitly_paper_only(self):
        values = parse_environment_template()
        self.assertEqual(values.get("ENABLE_PERPS_MARKET_DATA_WORKER"), "true")
        self.assertEqual(values.get("ENABLE_PERPS_PAPER_ENGINE"), "true")
        self.assertEqual(values.get("DRY_RUN"), "true")
        self.assertEqual(values.get("LIVE_EXECUTION_ENABLED"), "false")
        self.assertEqual(values.get("LIVE_TRADING_ENABLED"), "false")
        self.assertEqual(values.get("LIVE_TRADING_ARMED"), "false")

    def test_event_live_kill_switch_uses_protected_separate_operator_mount(self):
        values = parse_environment_template()
        self.assertEqual(
            values.get("EVENT_LIVE_KILL_SWITCH_PATH"),
            "/etc/polymarket/event_live_kill_switch.json",
        )
        self.assertEqual(values.get("EVENT_LIVE_KILL_SWITCH_OWNER_UID"), "0")
        self.assertNotIn("/app/data", values["EVENT_LIVE_KILL_SWITCH_PATH"])
        self.assertNotIn("./data", values["EVENT_LIVE_KILL_SWITCH_PATH"])

    def test_event_order_journal_path_is_explicit_and_distinct_from_perps_journal(self):
        values = parse_environment_template()
        self.assertEqual(values.get("ORDER_JOURNAL_DB"), "./data/order_journal.sqlite3")
        self.assertEqual(values.get("PERPS_ORDER_JOURNAL_DB"), "./data/perps_order_journal.sqlite3")
        self.assertNotEqual(values["ORDER_JOURNAL_DB"], values["PERPS_ORDER_JOURNAL_DB"])

    def test_perps_backup_settings_have_one_canonical_destination(self):
        values = parse_environment_template()
        self.assertEqual(values.get("PERPS_BACKUP_DIRECTORY"), "./data/perps_backups")
        self.assertEqual(values.get("PERPS_BACKUP_INTERVAL_HOURS"), "6")
        self.assertEqual(values.get("PERPS_BACKUP_RETAIN"), "30")


if __name__ == "__main__":
    unittest.main()
