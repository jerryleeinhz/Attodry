"""Offline range-rule preview and read-only monitor evidence contracts."""
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from attodry_control.combination_cli import run as cli
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_store import CombinationStore, run_snapshot
from attodry_control.combination_terminal import launch_text, snapshot_text
from tests import test_combination_range_rules as fixtures


class LockinRangePreviewMonitorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InternalRangeRuleIntegrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def load(self):
        return self.fixture.load()

    def begin(self, run_id="profiles", *, legacy=False):
        config = self.load()
        store = CombinationStore(self.fixture.database)
        self.addCleanup(store.connection.close)
        store.begin_run(run_id, config.plan.snapshot() if legacy else config.snapshot,
                        config.plan.conditions())
        return store

    @staticmethod
    def profile(name="high", *, verified=True, xx=.02, xy=.02):
        return dict(rule_name=name,
                    requested_full_scales_v={"lockin_xx": .02, "lockin_xy": .02},
                    confirmed_full_scales_v={"lockin_xx": xx, "lockin_xy": xy},
                    verified=verified, attempted_roles=["lockin_xx", "lockin_xy"])

    def test_launch_summary_and_text_show_resolved_rule_scales(self):
        config = self.load()
        summary = launch_summary(config, self.fixture.database, "preview-only")
        ranges = summary["lockin_range_rules"]
        self.assertEqual(ranges, config.lockin_range_schedule.snapshot())
        self.assertEqual([row["rule_name"] for row in ranges["preview"]], ["low", "high"])
        self.assertEqual(ranges["preview"][1]["outer_indices"], {"optical": 1})
        self.assertEqual(ranges["preview"][1]["requested"]["optical_target_power_w"], 10e-6)
        self.assertEqual(ranges["preview"][1]["full_scales_v"],
                         {"lockin_xx": .02, "lockin_xy": .02})
        text = launch_text(summary, width=220)
        self.assertIn("Lock-in range rules: 2 | Resolved outer visits: 2", text)
        self.assertIn("Range rule low: lockin_xx=10 mV, lockin_xy=10 mV", text)
        self.assertIn("Range rule high: lockin_xx=20 mV, lockin_xy=20 mV", text)
        self.assertEqual(self.fixture.manager.opened, [])
        self.assertFalse(self.fixture.database.exists())

    def test_cli_describe_associates_each_full_condition_with_its_profile(self):
        output = io.StringIO()
        with redirect_stdout(output), patch(
                "attodry_control.internal_lockin_group.InternalLockinGroupSession.open",
                side_effect=AssertionError("Describe must not open a lock-in")):
            code = cli(["describe-hardware", "--config", str(self.fixture.path)])
        self.assertEqual(code, 0)
        preview = json.loads(output.getvalue())
        conditions = preview["conditions"]
        self.assertEqual(len(conditions), 4)
        self.assertEqual([row["lockin_range_profile"]["rule_name"] for row in conditions],
                         ["low", "low", "high", "high"])
        self.assertEqual([row["lockin_range_profile"]["full_scales_v"]["lockin_xy"]
                          for row in conditions], [.01, .01, .02, .02])
        self.assertEqual([row["sequence_index"] for row in conditions], list(range(4)))
        self.assertEqual(len(preview["plan"]["lockin_range_rules"]["preview"]), 2)
        self.assertEqual(self.fixture.manager.opened, [])
        self.assertFalse(self.fixture.database.exists())

    def test_cli_no_rules_preserves_absent_profile_metadata(self):
        self.fixture.document["combination_scan"].pop("lockin_range_rules")
        self.fixture.write_document()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["describe-hardware", "--config", str(self.fixture.path)]), 0)
        preview = json.loads(output.getvalue())
        self.assertNotIn("lockin_range_rules", preview["plan"])
        self.assertTrue(all("lockin_range_profile" not in row for row in preview["conditions"]))

    def test_attempt_replaces_prior_verified_profile_in_monitor(self):
        store = self.begin()
        store.event("profiles", "lockin_range_profile_applied", self.profile("previous"))
        attempt = self.profile("next", verified=False, xx=.01, xy=.01)
        store.event("profiles", "lockin_range_profile_attempt", attempt)
        store.event("profiles", "scan_phase", {"phase": "axis_apply", "module": "optical"})
        snapshot = run_snapshot(self.fixture.database, "profiles")
        self.assertEqual(snapshot["lockin_range_profile"]["rule_name"], "next")
        self.assertIs(snapshot["lockin_range_profile"]["verified"], False)
        self.assertEqual(snapshot["last_event"]["event_type"], "scan_phase")
        text = snapshot_text(snapshot, width=220)
        self.assertIn("Lock-in range: next", text)
        self.assertIn("Verified: False", text)
        self.assertNotIn("Lock-in range: previous", text)

    def test_failed_profile_retains_partial_unknown_and_error_evidence(self):
        store = self.begin()
        failed = self.profile("failed_readback", verified=False, xx=.02, xy=None)
        failed.update(error_type="OSError", error="XY readback unavailable")
        store.event("profiles", "lockin_range_profile_failed", failed)
        before = store.connection.execute("SELECT count(*) FROM combination_events").fetchone()[0]
        snapshot = run_snapshot(self.fixture.database, "profiles")
        profile = snapshot["lockin_range_profile"]
        self.assertEqual(profile["error"], "XY readback unavailable")
        self.assertEqual(profile["error_type"], "OSError")
        self.assertIs(profile["confirmed_full_scales_v"]["lockin_xy"], None)
        self.assertEqual(profile["requested_full_scales_v"]["lockin_xy"], .02)
        self.assertIs(profile["verified"], False)
        text = snapshot_text(snapshot, width=220)
        self.assertIn("lockin_xx=20 mV", text)
        self.assertIn("lockin_xy=unknown", text)
        self.assertIn("Verified: False", text)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM combination_events").fetchone()[0], before)

    def test_monitor_supports_applied_then_selected_event_with_condition_context(self):
        store = self.begin()
        for event in ("lockin_range_profile_applied", "lockin_range_selected"):
            with self.subTest(event=event):
                payload = self.profile(event)
                if event == "lockin_range_selected":
                    payload.update(condition_id="condition-000002", outer_indices=[1],
                                   requested={"optical_target_power_w": 10e-6})
                store.event("profiles", event, payload)
                snapshot = run_snapshot(self.fixture.database, "profiles")
                profile = snapshot["lockin_range_profile"]
                self.assertEqual(profile["rule_name"], event)
                self.assertTrue(profile["verified"])
                self.assertIn("created_at_utc", profile)
                self.assertIn("Lock-in range: " + event, snapshot_text(snapshot, width=220))
                self.assertIn("Verified: True", snapshot_text(snapshot, width=220))
                if event == "lockin_range_selected":
                    self.assertEqual(profile["condition_id"], "condition-000002")

    def test_no_rule_legacy_database_needs_no_range_schema_or_events(self):
        self.fixture.document["combination_scan"].pop("lockin_range_rules")
        self.fixture.write_document()
        store = self.begin("legacy", legacy=True)
        store.connection.execute("DROP TABLE combination_skipped_conditions")
        store.connection.commit()
        snapshot = run_snapshot(self.fixture.database, "legacy")
        self.assertIsNone(snapshot["lockin_range_profile"])
        self.assertNotIn("Lock-in range:", snapshot_text(snapshot, width=220))
        self.assertEqual(snapshot["total_conditions"], 4)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM combination_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
