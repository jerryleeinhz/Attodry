"""Recovery audit/claim regressions using SQLite and a synthetic station only."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from copy import deepcopy

from attodry_control.combination_scan import (
    AxisPoint, CombinationPlan, ScanAxis, SimulatedCombinationStation, _run_combination,
)
from attodry_control.combination_store import (
    CombinationStore, encode, hardware_recovery_state, run_snapshot,
    utc_now, validate_accepted_records,
)


class RecordedStation(SimulatedCombinationStation):
    """Synthetic measurements shaped like owned hardware's durable raw evidence."""
    def __init__(self, harmonics_by_role=None):
        super().__init__()
        self.harmonics_by_role = harmonics_by_role or {"xx": [1, 2], "xy": [1, 2]}

    def read(self, module):
        result = super().read(module)
        if module in {"temperature", "magnetic"}:
            state = {"sample_temperature_k": self.values.get("temperature_k", 11.7),
                     "user_temperature_k": self.values.get("temperature_k", 11.7),
                     "vti_temperature_k": 4.7, "error_code": 0,
                     "temperature_control_enabled": True, "field_control_enabled": True,
                     "field": {"bx_t": self.values.get("field_x_t", 0),
                               "bz_t": self.values.get("field_z_t", 0)}}
            state["field_setpoint"] = deepcopy(state["field"])
            observations = [{"captured_at_utc": utc_now(), "state": deepcopy(state)} for _ in range(2)]
            result["status"] = {"sampling": "synchronous_brackets_not_continuous",
                "samples": observations, "temperature_mean_k": state["sample_temperature_k"],
                "field_mean": {"field_x_t": state["field"]["bx_t"], "field_z_t": state["field"]["bz_t"]}}
        elif module == "lockin":
            pairs, measured = [], {}
            for harmonic in sorted({h for values in self.harmonics_by_role.values() for h in values}):
                selected = [role for role, values in self.harmonics_by_role.items() if harmonic in values]
                role_harmonics = {"lockin_" + role: harmonic if role in selected else 1
                                  for role in ("xx", "xy")}
                settings = {"roles": {role: {"harmonic": h, "sensitivity_code": 26,
                    "sensitivity_full_scale_v": 1.0, "reserve_code": 1,
                    "reserve_mode": "normal", "time_constant_s": 1.0, "time_constant_code": 10}
                    for role, h in role_harmonics.items()}}
                pair = {"harmonic": harmonic, "harmonics_by_role": role_harmonics,
                        "sampling_policy": "independent_roles", "sample_index": 0,
                        "selected_roles": selected, "settings_verified": True,
                        "settings_before": deepcopy(settings), "settings_after": deepcopy(settings),
                        "overload_policy": "record_continue", "blocking_problems": [],
                        "problems": [], "continued_overload_problems": [],
                        "valid_for_analysis_by_role": {"lockin_xx": True, "lockin_xy": True},
                        "problems_by_role": {"lockin_xx": [], "lockin_xy": []}}
                for role, factor in (("xx", 1.0), ("xy", .1)):
                    value = self.values["lockin_excitation_v_rms"] * factor / harmonic
                    raw = {"role": role, "harmonic": role_harmonics["lockin_" + role],
                           "x_v": value, "y_v": 0.0, "amplitude_v": value, "phase_deg": 0.0,
                           "frequency_hz": self.values["lockin_frequency_hz"], "locked": True}
                    pair["lockin_" + role] = {"reading": raw, "error_status": 0,
                        "captured_at_utc": utc_now(), "lia_status": {"raw": 0,
                            **{flag: False for flag in ("input_or_reserve_overload", "filter_overload",
                                 "output_overload", "reference_unlocked", "frequency_range_changed",
                                 "time_constant_changed")}}}
                    if role in selected:
                        measured.update({f"lockin_{role}_h{harmonic}_{metric}": raw[metric]
                            for metric in ("x_v", "y_v", "amplitude_v", "phase_deg")})
                pairs.append(pair)
            result["status"] = {"samples": pairs}
            result["measurements"] = measured
        result["captured_at_utc"] = utc_now()
        return result


def recorded_sample(condition, harmonics_by_role=None):
    station = RecordedStation(harmonics_by_role)
    station.values.update(condition["requested"])
    start = utc_now()
    reads = [station.read(module) for module in condition["loop_order"]]
    return {"acquisition_accepted": True, "simulated": False, "clean": True,
            "started_at_utc": start, "finished_at_utc": utc_now(), "reads": reads,
            "actual": {key: value for reading in reads for key, value in reading["actual"].items()},
            "measurements": {key: value for reading in reads for key, value in reading["measurements"].items()}}


def mark_xy_h2_overload(reading):
    pair = next(p for p in reading["status"]["samples"] if p["harmonic"] == 2)
    problem = "lockin_xy input/reserve overload"
    raw = pair["lockin_xy"]
    raw["lia_status"].update(raw=1, input_or_reserve_overload=True)
    raw["reading"]["overload"] = True
    pair.update(problems=[problem], continued_overload_problems=[problem])
    pair["problems_by_role"]["lockin_xy"] = [problem]
    pair["valid_for_analysis_by_role"]["lockin_xy"] = False
    reading.update(clean=False, overload_continuation=True, problems=[problem])
    return reading
from attodry_control.combination_resume import (
    RECOVERY_CONTRACT, _cleanup_review, configuration_changes, digest, scan_contract,
)


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scan.sqlite"
        self.run_id = "20261014T124726025034Z_T-e-sweep_17-250K_0.05-3V_200Hz"
        self.plan = CombinationPlan((
            ScanAxis("temperature", tuple(AxisPoint({"temperature_k": 11.7 + i})
                                           for i in range(18))),
            ScanAxis("lockin", tuple(AxisPoint({"lockin_excitation_v_rms": .1 + i * .07,
                                                "lockin_frequency_hz": 200.0})
                                    for i in range(19))),
        ))
        self.old_plan = json.loads(encode({**self.plan.snapshot(), "mode": "hardware",
            "config_sha256": "old", "source_sha256": {"fake_station.py": "offline-fixture"},
            "hardware": {"lockin": {"harmonics_by_role": {"xx": [1, 2], "xy": [1, 2]},
                **{role: {"time_constant_s": 1.0, "sensitivity_full_scale_v": 1.0,
                          "sensitivity_mode": "fixed", "reserve_mode": "normal"}
                   for role in ("lockin_xx", "lockin_xy")}}}}))
        self.new_plan = {**self.old_plan, "config_sha256": "new"}

    def legacy_failure(self, accepted=6, orphan=False):
        """Build the unchanged schema-1 shape of the user's 6/342 audit."""
        with CombinationStore(self.path) as store:
            store.begin_run(self.run_id, self.old_plan, self.plan.conditions())
            for condition in self.plan.conditions()[:accepted]:
                attempt = store.begin_attempt(self.run_id, condition["condition_id"])
                store.sample(self.run_id, condition["condition_id"], attempt, 0,
                             {**recorded_sample(condition), "original": condition["sequence_index"]})
                store.finish_attempt(self.run_id, condition["condition_id"], attempt, expected_samples=1)
            if accepted < len(self.plan.conditions()):
                condition = self.plan.conditions()[accepted]["condition_id"]
                attempt = store.begin_attempt(self.run_id, condition)
                store.sample(self.run_id, condition, attempt, 0,
                             {"clean": False, "reads": [], "partial_h1": True})
                if not orphan:
                    store.finish_attempt(self.run_id, condition, attempt, expected_samples=1,
                                         error="AttoDryError: Temperature drifted from qualified baseline")
            store.finish_run(self.run_id, "failed", {
                "clean": False, "manual_verification_required": True,
                "communication_uncertain": False, "actions": [],
            }, "AttoDryError: Temperature drifted from qualified baseline")

    def review(self, snapshot=None):
        snapshot = self.new_plan if snapshot is None else snapshot
        state = hardware_recovery_state(self.path, self.run_id)
        original = json.loads(state["run"]["plan_json"])
        parent = json.loads(state["executions"][-1]["plan_json"]) if state["executions"] else original
        accepted = set(state["accepted_condition_ids"])
        pending = [c for c in state["conditions"] if c["condition_id"] not in accepted]
        return {"expected_parent_state_sha256": state["state_sha256"],
                "original_conditions": state["conditions"],
                "next_condition": pending[0] if pending else None,
                "cleanup_only": not pending,
                "review": {"contract": RECOVERY_CONTRACT, "ready": True,
                           "acknowledged_failure": True, "manual_verification": True,
                           "protection_changes_approved": False,
                           "config_sha256": snapshot["config_sha256"],
                           "scan_contract_sha256": digest(scan_contract(original)),
                           "configuration_changes": configuration_changes(parent, snapshot),
                           "cleanup_review": _cleanup_review(
                               json.loads(state["run"]["cleanup_json"] or "{}"),
                               [axis["module"] for axis in original["axes"]])}}

    def test_schema1_6_of_342_recovers_whole_seventh_without_rewriting_history(self):
        self.legacy_failure()
        with closing(sqlite3.connect(self.path)) as conn:
            original_run = conn.execute("SELECT * FROM combination_runs").fetchone()
            original_attempts = conn.execute("SELECT * FROM combination_attempts").fetchall()
            original_samples = conn.execute("SELECT * FROM combination_samples").fetchall()
        recovery = self.review()
        station = RecordedStation()
        with CombinationStore(self.path) as store:
            result = _run_combination(self.plan, store, self.run_id, station=station,
                                      snapshot=self.new_plan, recovery=recovery)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(op[:2] == ("read", "lockin") for op in station.operations), 336)
        first_apply = next(op for op in station.operations if op[:2] == ("apply", "lockin"))
        self.assertEqual(first_apply[2], self.plan.axes[1].points[6].values)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT plan_json FROM combination_runs").fetchone()[0],
                             original_run[2])
            self.assertEqual(conn.execute("SELECT * FROM combination_attempts WHERE attempt_index=0 "
                                          "ORDER BY condition_id LIMIT 7").fetchall(), original_attempts)
            self.assertEqual(conn.execute("SELECT * FROM combination_samples WHERE attempt_index=0 "
                                          "ORDER BY condition_id LIMIT 7").fetchall(), original_samples)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM combination_attempts "
                                          "WHERE status='accepted'").fetchone()[0], 342)
            self.assertEqual(conn.execute("SELECT attempt_index FROM combination_attempts "
                                          "WHERE condition_id='condition-000006' AND status='accepted'")
                             .fetchone()[0], 1)
            executions = conn.execute("SELECT kind,status,plan_json,review_json,error "
                                      "FROM combination_executions ORDER BY sequence_index").fetchall()
            self.assertEqual([(e[0], e[1]) for e in executions],
                             [("legacy", "failed"), ("resume", "completed")])
            self.assertEqual(json.loads(executions[0][3])["original_run"]["error"], original_run[-1])
            self.assertEqual(json.loads(executions[1][2])["config_sha256"], "new")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM combination_attempt_executions")
                             .fetchone()[0], 343)
        snapshot = run_snapshot(self.path, self.run_id)
        self.assertEqual(snapshot["accepted_conditions"], 342)
        self.assertEqual([e["accepted_conditions"] for e in snapshot["executions"]], [6, 336])

    def test_claim_is_atomic_and_cannot_be_replayed_by_second_writer(self):
        self.legacy_failure()
        recovery = self.review()
        with CombinationStore(self.path) as first, CombinationStore(self.path) as second:
            accepted = first.begin_hardware_execution(
                self.run_id, self.new_plan, self.plan.conditions(), recovery=recovery)
            self.assertEqual(len(accepted), 6)
            with self.assertRaisesRegex(ValueError, "changed after review"):
                second.begin_hardware_execution(
                    self.run_id, self.new_plan, self.plan.conditions(), recovery=recovery)
            with self.assertRaisesRegex(ValueError, "terminal"):
                second.begin_hardware_execution(
                    self.run_id, self.new_plan, self.plan.conditions(), recovery=self.review())

    def test_changed_raw_sample_or_configuration_state_invalidates_review(self):
        self.legacy_failure()
        recovery = self.review()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE combination_samples SET payload_json=? WHERE condition_id=?",
                         (encode({"clean": True, "changed": True}), "condition-000000"))
            conn.commit()
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "changed after review"):
                store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                               recovery=recovery)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM combination_executions")
                             .fetchone()[0], 0)

    def test_started_orphan_is_rejected_without_reclassifying_it(self):
        self.legacy_failure(orphan=True)
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "Started/orphan"):
                store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                               recovery=self.review())
            self.assertEqual(store.connection.execute("SELECT status FROM combination_attempts "
                              "WHERE condition_id='condition-000006'").fetchone()[0], "started")

    def test_changed_condition_order_or_boundary_is_rejected_before_open(self):
        self.legacy_failure()
        recovery = self.review()
        recovery["next_condition"] = self.plan.conditions()[7]
        station = SimulatedCombinationStation()
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "boundary"):
                _run_combination(self.plan, store, self.run_id, station=station,
                                 snapshot=self.new_plan, recovery=recovery)
        self.assertEqual(station.operations, [])
        changed = self.plan.conditions()
        changed[6]["requested"]["lockin_excitation_v_rms"] += .01
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "ordered condition"):
                store.begin_hardware_execution(self.run_id, self.new_plan, changed,
                                               recovery=self.review())

    def test_all_accepted_failure_uses_explicit_cleanup_only_hook_and_no_formal_reads(self):
        self.legacy_failure(accepted=342)
        class CleanupStation(RecordedStation):
            def prepare_cleanup_recovery(self, condition):
                self.operations.append(("prepare_cleanup_recovery", condition["condition_id"]))
                return {"ready": True}
        station = CleanupStation()
        with CombinationStore(self.path) as store:
            result = _run_combination(self.plan, store, self.run_id, station=station,
                                      snapshot=self.new_plan, recovery=self.review())
        self.assertEqual(result["status"], "completed")
        self.assertIn(("prepare_cleanup_recovery", "condition-000341"), station.operations)
        self.assertFalse(any(op[0] in {"read", "apply"} for op in station.operations))
        self.assertEqual(run_snapshot(self.path, self.run_id)["executions"][-1]["kind"], "cleanup_only")

    def test_cleanup_only_cannot_complete_without_station_qualification_hook(self):
        self.legacy_failure(accepted=342)
        with CombinationStore(self.path) as store:
            result = _run_combination(self.plan, store, self.run_id,
                                      station=RecordedStation(), snapshot=self.new_plan,
                                      recovery=self.review())
        self.assertEqual(result["status"], "failed")
        self.assertIn("cannot qualify", result["error"])

    def test_initial_hardware_run_creates_segment_and_attempt_ownership(self):
        plan = CombinationPlan((ScanAxis("temperature", (AxisPoint({"temperature_k": 11.7}),)),))
        snapshot = {**plan.snapshot(), "mode": "hardware"}
        with CombinationStore(self.path) as store:
            result = _run_combination(plan, store, self.run_id,
                                      station=SimulatedCombinationStation(), snapshot=snapshot)
            with self.assertRaisesRegex(ValueError, "not active"):
                store.begin_attempt(self.run_id, "condition-000000")
        self.assertEqual(result["status"], "completed")
        state = hardware_recovery_state(self.path, self.run_id)
        self.assertEqual(state["executions"][0]["kind"], "initial")
        self.assertEqual(state["attempt_executions"][0]["execution_id"], result["execution_id"])

    def test_second_recovery_retains_both_failed_segments_and_restarts_whole_pending_condition(self):
        self.legacy_failure()
        class FailsAgain(RecordedStation):
            def __init__(self):
                super().__init__()
                self.qualifications = 0
            def qualify(self, modules):
                self.qualifications += 1
                if self.qualifications == 3:
                    raise ValueError("Temperature drifted again")
                return super().qualify(modules)
        with CombinationStore(self.path) as store:
            result = _run_combination(self.plan, store, self.run_id, station=FailsAgain(),
                                      snapshot=self.new_plan, recovery=self.review())
        self.assertEqual(result["status"], "failed")
        state = hardware_recovery_state(self.path, self.run_id)
        self.assertEqual(len(state["accepted_condition_ids"]), 8)
        third_plan = {**self.new_plan, "config_sha256": "third"}
        review = self.review(third_plan)
        self.assertEqual(review["next_condition"]["condition_id"], "condition-000008")
        station = RecordedStation()
        with CombinationStore(self.path) as store:
            result = _run_combination(self.plan, store, self.run_id, station=station,
                                      snapshot=third_plan,
                                      recovery=review)
        self.assertEqual(result["status"], "completed")
        snapshot = run_snapshot(self.path, self.run_id)
        self.assertEqual([e["status"] for e in snapshot["executions"]],
                         ["failed", "failed", "completed"])
        self.assertEqual([e["accepted_conditions"] for e in snapshot["executions"]], [6, 2, 334])
        self.assertEqual(snapshot["executions"][1]["error"], "ValueError: Temperature drifted again")

    def test_noncontiguous_accepted_prefix_is_never_silently_skipped(self):
        self.legacy_failure()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE combination_attempts SET status='rejected' "
                         "WHERE condition_id='condition-000000'")
            conn.commit()
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "contiguous prefix"):
                store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                               recovery=self.review())

    def test_backend_rechecks_acknowledgment_and_configuration_contract_before_claim(self):
        self.legacy_failure()
        for flag in ("ready", "acknowledged_failure"):
            recovery = self.review()
            recovery["review"][flag] = False
            with CombinationStore(self.path) as store:
                with self.assertRaisesRegex(ValueError, "explicitly acknowledged"):
                    store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                                   recovery=recovery)
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM combination_executions")
                                 .fetchone()[0], 0)
        recovery = self.review()
        changed = {**self.new_plan, "hardware": {"lockin": {"harmonics_by_role": {"xx": [1]}}}}
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "harmonics changed"):
                store.begin_hardware_execution(self.run_id, changed, self.plan.conditions(),
                                               recovery=recovery)

    def test_backend_rejects_forged_current_configuration_binding(self):
        self.legacy_failure()
        recovery = self.review()
        recovery["review"]["config_sha256"] = "different-config"
        with CombinationStore(self.path) as store:
            with self.assertRaisesRegex(ValueError, "does not bind"):
                store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                               recovery=recovery)

    def test_accepted_flag_cannot_hide_missing_harmonic_settings_or_unlock(self):
        self.legacy_failure()
        with closing(sqlite3.connect(self.path)) as conn:
            original = json.loads(conn.execute("SELECT payload_json FROM combination_samples "
                "WHERE condition_id='condition-000000'").fetchone()[0])
        changes = (
            lambda s: s.update(reads=[]),
            lambda s: s["reads"][1]["status"]["samples"].pop(),
            lambda s: s["reads"][1]["status"]["samples"][0].pop("settings_after"),
            lambda s: s["reads"][1]["status"]["samples"][0]["lockin_xy"]["reading"].update(locked=False),
            lambda s: s["reads"][1]["status"]["samples"][0]["lockin_xx"].update(error_status=32),
            lambda s: s["reads"][1]["status"]["samples"][0]["lockin_xx"]["lia_status"].update(raw=8),
            lambda s: s["reads"][0]["status"]["samples"][0]["state"].update(error_code=4),
        )
        for mutate in changes:
            payload = deepcopy(original)
            mutate(payload)
            with closing(sqlite3.connect(self.path)) as conn:
                conn.execute("UPDATE combination_samples SET payload_json=? "
                             "WHERE condition_id='condition-000000'", (encode(payload),))
                conn.commit()
            with CombinationStore(self.path) as store:
                with self.assertRaisesRegex(ValueError, "incomplete/invalid raw evidence"):
                    store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                                   recovery=self.review())
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM combination_executions")
                                 .fetchone()[0], 0)

    def test_legitimate_record_continue_invalid_samples_remain_accepted(self):
        self.legacy_failure()
        with closing(sqlite3.connect(self.path)) as conn:
            payload = json.loads(conn.execute("SELECT payload_json FROM combination_samples "
                "WHERE condition_id='condition-000000'").fetchone()[0])
            mark_xy_h2_overload(payload["reads"][1])
            payload["clean"] = False
            conn.execute("UPDATE combination_samples SET payload_json=? "
                         "WHERE condition_id='condition-000000'", (encode(payload),))
            conn.commit()
        with CombinationStore(self.path) as store:
            audit = validate_accepted_records(store.connection, self.run_id)
            self.assertEqual(audit, {"accepted_conditions": 6, "accepted_samples": 6})
            accepted = store.begin_hardware_execution(self.run_id, self.new_plan, self.plan.conditions(),
                                                      recovery=self.review())
            self.assertIn("condition-000000", accepted)

    def test_partial_group_overload_is_rejected_audit_not_final_formal_quality(self):
        self.plan = CombinationPlan((
            ScanAxis("temperature", (AxisPoint({"temperature_k": 11.7}),)),
            ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .1, "lockin_frequency_hz": 200.0}),)),
        ), samples_per_condition=2)
        self.old_plan = json.loads(encode({**self.old_plan, **self.plan.snapshot(), "mode": "hardware"}))
        self.new_plan = {**self.old_plan, "config_sha256": "new"}
        class FailsSecondGroup(RecordedStation):
            def __init__(self):
                super().__init__()
                self.lockin_reads = 0
            def read(self, module):
                reading = super().read(module)
                if module == "lockin":
                    self.lockin_reads += 1
                    if self.lockin_reads == 2:
                        raise ValueError("temperature drift before second group's complete pair")
                    mark_xy_h2_overload(reading)
                return reading
        with CombinationStore(self.path) as store:
            failed = _run_combination(self.plan, store, self.run_id, station=FailsSecondGroup(),
                                      snapshot=self.old_plan)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["overload_summary"]["formal_pairs"], 0)
        self.assertEqual(failed["rejected_attempt_audit"]["formal_pairs"], 2)
        with CombinationStore(self.path) as store:
            result = _run_combination(self.plan, store, self.run_id, station=RecordedStation(),
                                      snapshot=self.new_plan, recovery=self.review())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["overload_summary"]["formal_pairs"], 4)
        self.assertEqual(result["overload_summary"]["overloaded_pairs_by_role"]["lockin_xy"], 0)
        self.assertEqual(result["rejected_attempt_audit"]["formal_pairs"], 2)
        self.assertEqual(result["rejected_attempt_audit"]["overloaded_pairs_by_role"]["lockin_xy"], 1)
        with CombinationStore(self.path) as store:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM combination_samples").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
