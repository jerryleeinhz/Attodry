"""Recovery review, CLI authorization and ownership; fake/file-only inputs."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_ownership import RecoveryOwner, validate_inventory
from attodry_control.combination_resume import build_recovery, configuration_changes, scan_contract
from attodry_control.combination_store import CombinationStore
from tests import test_combination_cryostat as fixtures


class RecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CryostatCombinationTests("test_all_64_nonempty_orders_share_one_connection")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.write_config(("temperature", "lockin"))
        self.config = load_hardware_combination(self.fixture.path)
        self.database = self.fixture.database
        self.snapshot = deepcopy(self.config.snapshot)
        self.snapshot["resume_supported"] = False
        self.snapshot.pop("hardware_resume_contract", None)
        with CombinationStore(self.database) as store:
            store.begin_run("old", self.snapshot, self.config.plan.conditions())
            store.finish_run("old", "failed", {"clean": False, "communication_uncertain": False,
                "actions": [{"module": m, "failure_requires_manual_review": True,
                             "result": {"verified": True, "errors": []}}
                            for m in ("lockin", "temperature")]}, "Temperature drifted")

    def review(self, config=None, **kwargs):
        return build_recovery(config or self.config, self.database, "old", **kwargs)

    def test_review_is_readonly_and_acknowledgment_is_separate(self):
        before = self.database.read_bytes()
        result = self.review()
        self.assertFalse(result["review"]["ready"])
        self.assertEqual(result["next_condition"]["condition_id"], "condition-000000")
        self.assertFalse(result["review"]["cleanup_review"]["manual_verification_required"])
        self.assertTrue(self.review(acknowledge_failure=True)["review"]["ready"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_temperature_criteria_changed_but_original_contract_retained(self):
        snapshot = deepcopy(self.config.snapshot)
        for key in ("temperature", "effective_cryostat"):
            snapshot["hardware"][key]["temperature_stability"]["criteria"]["stable_range"] = .08
        config = replace(self.config, snapshot=snapshot)
        result = self.review(config, acknowledge_failure=True)
        self.assertTrue(result["review"]["ready"])
        self.assertEqual(len(result["review"]["configuration_changes"]), 2)
        self.assertEqual(scan_contract(self.snapshot), scan_contract(snapshot))

    def test_unknown_source_version_rejected(self):
        snapshot = deepcopy(self.config.snapshot)
        snapshot["source_sha256"] = {"unexpected.py": "0" * 64}
        with self.assertRaisesRegex(ValueError, "compatibility"):
            self.review(replace(self.config, snapshot=snapshot), acknowledge_failure=True)

    def test_coordinates_harmonics_and_hard_bound_changes_refused(self):
        for mutate in (
            lambda s: s["axes"][0]["points"][0]["values"].update(temperature_k=2.1),
            lambda s: s["hardware"]["lockin"]["harmonics_by_role"].update(xx=[1, 2]),
            lambda s: s["hardware"]["temperature"]["cryostat"].update(temperature_max_k=400),
            lambda s: s["hardware"]["temperature"]["temperature_run"].update(max_overshoot_k=2),
        ):
            snapshot = deepcopy(self.config.snapshot)
            mutate(snapshot)
            with self.assertRaises(ValueError):
                configuration_changes(self.snapshot, snapshot)

    def test_lockin_range_reserve_tc_and_unused_metadata(self):
        snapshot = deepcopy(self.config.snapshot)
        role = snapshot["hardware"]["lockin"]["lockin_xx"]
        role.update(sensitivity_full_scale_v=.05, time_constant_s=1, reserve_mode="low_noise")
        changes = configuration_changes(self.snapshot, snapshot)
        self.assertGreaterEqual(len(changes), 3)
        snapshot["hardware"]["lockin"]["lockin_sweep"]["note"] = "recovery"
        snapshot["hardware"]["temperature"]["temperature_run"]["interrupt_policy"] = "continue"
        self.assertEqual(configuration_changes(self.snapshot, snapshot), changes)

    def test_smu_counter_limit_is_protection_and_source_limit_stays_fixed(self):
        self.fixture.write_config(("smu",))
        snapshot = load_hardware_combination(self.fixture.path).snapshot
        changed = deepcopy(snapshot)
        changed["hardware"]["smu"]["hardware"]["smu_bias"]["max_abs_current_a"] *= 2
        self.assertEqual(configuration_changes(snapshot, changed)[0]["category"], "protection_increase")
        changed["hardware"]["smu"]["hardware"]["smu_bias"]["max_abs_voltage_v"] *= 2
        with self.assertRaisesRegex(ValueError, "new run"):
            configuration_changes(snapshot, changed)

    def test_uncertain_cleanup_needs_physical_review_but_keeps_old_record(self):
        with CombinationStore(self.database) as store:
            store.finish_run("old", "failed", {"clean": False, "communication_uncertain": True,
                "actions": [{"module": "temperature", "result": {"verified": False}}]}, "USB lost")
        result = self.review(acknowledge_failure=True)
        self.assertFalse(result["review"]["ready"])
        self.assertTrue(self.review(acknowledge_failure=True, manual_verification=True)["review"]["ready"])
        with CombinationStore(self.database) as store:
            row = store.connection.execute("SELECT error,cleanup_json FROM combination_runs WHERE run_id='old'").fetchone()
            self.assertEqual(row["error"], "USB lost")
            self.assertTrue(json.loads(row["cleanup_json"])["communication_uncertain"])

    def test_missing_cleanup_module_cannot_be_acknowledged_as_verified(self):
        with CombinationStore(self.database) as store:
            store.finish_run("old", "failed", {"clean": False, "communication_uncertain": False,
                "actions": [{"module": "temperature", "result": {"verified": True}}]}, "missing lockin")
        review = self.review(acknowledge_failure=True)["review"]
        self.assertFalse(review["ready"])
        self.assertIn("lockin", review["cleanup_review"]["unresolved_modules"])

    def test_add_and_remove_harmonic_override_preserves_measurement_identity(self):
        original = deepcopy(self.snapshot)
        original["hardware"]["lockin"]["harmonics_by_role"]["xx"] = [1, 2]
        changed = deepcopy(original)
        changed["hardware"]["lockin"]["lockin_xx"]["harmonic_settings"] = [
            {"harmonic": 2, "sensitivity_mode": "fixed", "sensitivity_full_scale_v": .01,
             "reserve_mode": "normal"}]
        differences = configuration_changes(original, changed)
        self.assertTrue(any("harmonic_settings.2.sensitivity_full_scale_v" in c["path"] for c in differences))
        self.assertTrue(configuration_changes(changed, original))

    def test_cli_check_never_constructs_writer_or_owner_or_hardware(self):
        for flags, expected in (([], 2), (["--acknowledge-failure"], 0)):
            with patch("attodry_control.combination_cli.CombinationStore", side_effect=AssertionError("writer")), \
                 patch("attodry_control.combination_hardware.run_hardware_combination", side_effect=AssertionError("I/O")), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli(["resume", "--check", "--json", "--config", str(self.fixture.path),
                              "--database", str(self.database), "--run-id", "old", *flags])
            self.assertEqual(result, expected)
            self.assertIn('"accepted_conditions": 0', output.getvalue())

    def test_auto_and_changed_grid_refused_before_hardware(self):
        with self.assertRaisesRegex(ValueError, "exact original"):
            build_recovery(self.config, self.database, "auto")
        changed = replace(self.config, plan=replace(self.config.plan, samples_per_condition=2))
        with self.assertRaisesRegex(ValueError, "groups/repeats"):
            self.review(changed, acknowledge_failure=True)


class OwnershipTests(unittest.TestCase):
    def own(self):
        return {"pid": os.getpid(), "parent": 0, "name": "python.exe", "created": "2026-10-14T00:00Z",
                "command": "python -m attodry_control.combination_cli resume --run-id old"}

    def test_unknown_acquisition_and_gui_blocked_idle_analysis_allowed(self):
        own = self.own()
        kernel = {"pid": 99, "parent": 0, "name": "python.exe", "created": "fixed",
                  "command": "python -m ipykernel_launcher -f C:/runtime/kernel-abc-def.json"}
        self.assertEqual(len(validate_inventory([own, kernel], own_pid=os.getpid(), idle_kernels={"abc-def"})), 2)
        for row in (kernel, {**kernel, "command": "python -m attodry_control.combination_cli run"},
                    {**kernel, "name": "attoDRY.exe"}):
            with self.assertRaises(ValueError):
                validate_inventory([own, row], own_pid=os.getpid(), idle_kernels=set())

    def test_owner_check_requires_lock_and_rejects_config_mutation(self):
        fixture = RecoveryReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner = RecoveryOwner(fixture.config, inventory=lambda: [self.own()], idle_reader=lambda _: set())
        with self.assertRaisesRegex(ValueError, "exclusive"):
            owner.check()
        with owner.acquire():
            self.assertEqual(owner.check()["pid"], os.getpid())
            with self.assertRaisesRegex(ValueError, "exclusive station lock"):
                with RecoveryOwner(fixture.config, inventory=lambda: [self.own()], idle_reader=lambda _: set()).acquire():
                    self.fail("second owner entered")
            fixture.config.path.write_text(fixture.config.path.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Configuration changed"):
                owner.check()
