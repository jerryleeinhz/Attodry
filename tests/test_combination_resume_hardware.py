"""Reviewed hardware recovery through real point engines, with fake DLL/VISA only."""
import json
import unittest
from unittest.mock import patch

from attodry_control.attodry import AttoDryError
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_store import CombinationStore, hardware_recovery_state
from attodry_control.cryostat_points import CryostatPointSession
from tests.test_attodry import StepClock
from tests.test_combination_cryostat import CryostatCombinationTests
from tests.test_combination_hardware import ElectricalCombinationTests


class HardwareRecoveryTests(unittest.TestCase):
    setUp = CryostatCombinationTests.setUp
    write_config = ElectricalCombinationTests.write_config
    factory = ElectricalCombinationTests.factory
    events = ElectricalCombinationTests.events

    def execute(self, config, *, recovery=None, owner_guard=None):
        with CombinationStore(self.database) as store:
            return run_hardware_combination(config, store, "recovery-test",
                authorize_hardware=True, authorize_cryostat=True,
                confirm_xy_sine_disconnected=True, dll=self.dll, monotonic=StepClock(),
                smu_adapter_factory=self.factory, manager_factory=lambda: self.manager,
                sleep=lambda _: None, recovery=recovery, recovery_owner_guard=owner_guard)

    def seed(self, order, *, accepted=0, source=None):
        self.write_config(order, source=source)
        config = load_hardware_combination(self.path)
        conditions = config.plan.conditions()
        if accepted:
            # Genuine fake-instrument raw records are required to skip a point;
            # never manufacture accepted evidence from a clean/status label.
            self.assertEqual(accepted, len(conditions))
            result = self.execute(config)
            self.assertEqual(result["status"], "completed", result)
            cleanup = result["cleanup"]
            cleanup.update(clean=False, manual_verification_required=True)
            for action in cleanup["actions"]:
                action.update(clean=False, failure_requires_manual_review=True)
            with CombinationStore(self.database) as store:
                store.connection.execute("DROP TABLE combination_attempt_executions")
                store.connection.execute("DROP TABLE combination_executions")
                store.connection.execute("UPDATE combination_runs SET status='failed', cleanup_json=?, error=? "
                    "WHERE run_id='recovery-test'", (json.dumps(cleanup), "injected final close failure"))
                store.connection.commit()
            self.log.clear()
            self.xx.writes.clear()
            self.xy.writes.clear()
            return config
        with CombinationStore(self.database) as store:
            # Original schema-1 writer, deliberately without execution tables.
            store.begin_run("recovery-test", config.snapshot, conditions)
            store.finish_run("recovery-test", "failed", {
                "clean": False, "communication_uncertain": False,
                "manual_verification_required": True,
                "actions": [{"module": module, "failure_requires_manual_review": True,
                    "result": {"verified": True}} for module in order]}, "injected prior failure")
        return config

    def recovery(self, config, *, manual=False):
        return build_recovery(config, self.database, "recovery-test",
            acknowledge_failure=True, manual_verification=manual,
            approve_protection_changes=True)

    def test_recovery_without_process_guard_runs_fresh_preflight_and_cleanup(self):
        config = self.seed(("temperature", "lockin"))
        with patch("attodry_control.combination_ownership.process_inventory",
                   side_effect=AssertionError("Process inventory must not run")), \
             patch("attodry_control.combination_ownership._idle_jupyter",
                   side_effect=AssertionError("Jupyter inspection must not run")), \
             patch("attodry_control.combination_ownership.RecoveryOwner.acquire",
                   side_effect=AssertionError("Process lease must not be acquired")):
            result = self.execute(config, recovery=self.recovery(config), owner_guard=None)
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("recovery_preflight_verified", [kind for kind, _ in self.events()])
        self.assertTrue(result["cleanup"]["clean"])
        self.assertTrue(all(action["result"]["verified"] for action in result["cleanup"]["actions"]
                            if action["module"] in {"temperature", "lockin"}))
        self.assertTrue(self.manager.closed)
        self.assertFalse(self.dll.connected)

    def test_legacy_process_callback_is_ignored_between_preflights(self):
        config = self.seed(("temperature", "smu", "lockin"))
        calls = []
        def guard():
            calls.append(True)
            raise ValueError("unrelated process exited")
        result = self.execute(config, recovery=self.recovery(config), owner_guard=guard)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(calls, [])
        self.assertTrue(result["cleanup"]["clean"])
        self.assertTrue(self.manager.closed)
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertFalse(self.dll.connected)

    def test_unsafe_fresh_field_blocks_recovery_before_electrical_writes(self):
        config = self.seed(("temperature", "smu", "lockin"))
        self.dll.bx_t = self.dll.setpoint_x_t = 3.01
        result = self.execute(config, recovery=self.recovery(config, manual=True))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.manager.opened, [])
        self.assertEqual(self.adapters, {})
        self.assertNotIn("set_temperature", self.dll.events)

    def test_recovery_additional_3t_guard_rejects_current_single_z_4t(self):
        config = self.seed(("magnetic", "lockin"), source=self.base.replace(
            '{ bx_t = 0.0, bz_t = 0.0 },', '{ bx_t = 0.0, bz_t = 1.0 },'))
        self.dll.bz_t = self.dll.setpoint_z_t = 4.0
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.xx.writes + self.xy.writes, [])
        self.assertFalse(any(event.startswith("set_field") for event in self.dll.events))

    def test_acknowledged_review_is_required_before_connection(self):
        config = self.seed(("temperature", "lockin"))
        recovery = self.recovery(config)
        recovery["review"]["acknowledged_failure"] = False
        with self.assertRaisesRegex(ValueError, "acknowledged"):
            self.execute(config, recovery=recovery)
        self.assertEqual(self.dll.events, [])
        self.assertEqual(self.manager.opened, [])

    def test_changed_smu_compliance_is_verified_with_output_off_before_enable(self):
        self.seed(("smu", "lockin"))
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "max_abs_current_a = 0.000001", "max_abs_current_a = 0.000002"), encoding="utf-8")
        config = load_hardware_combination(self.path)
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.adapters["smu_bias"].config.max_abs_current_a, .000002)
        kinds = [kind for kind, _ in self.events()]
        self.assertLess(kinds.index("smu_recovery_zero_off"), kinds.index("configure"))
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_recovery_widens_range_before_reserve_and_preserves_phase(self):
        self.seed(("lockin",))
        self.xy.responses["SENS?"] = "20"
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "sensitivity_full_scale_v = 0.010", "sensitivity_full_scale_v = 0.100").replace(
            'reserve_mode = "normal"', 'reserve_mode = "high_reserve"'), encoding="utf-8")
        config = load_hardware_combination(self.path)
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "completed", result)
        self.assertLess(self.xy.writes.index("SENS 23"), self.xy.writes.index("RMOD 0"))
        self.assertFalse(any(command.startswith("PHAS ") for command in self.xx.writes + self.xy.writes))
        self.assertEqual(self.xy.responses["SENS?"].strip(), "23")

    def test_analysis_exit_after_accepted_condition_does_not_stop_scan_or_cleanup(self):
        config = self.seed(("temperature", "smu", "lockin"))
        stop = [False]
        calls = []
        event = CombinationStore.event
        def guard():
            calls.append(True)
            if stop[0]:
                raise ValueError("Analysis owner identities changed during recovery")
        def observe(store, run_id, kind, payload):
            result = event(store, run_id, kind, payload)
            if kind == "condition_accepted" and not stop[0]:
                stop[0] = True
            return result
        with patch.object(CombinationStore, "event", observe):
            result = self.execute(config, recovery=self.recovery(config), owner_guard=guard)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(stop[0])
        self.assertEqual(calls, [])
        state = hardware_recovery_state(self.database, "recovery-test")
        self.assertEqual(len(state["accepted_condition_ids"]), len(config.plan.conditions()))
        self.assertTrue(result["cleanup"]["clean"])
        self.assertFalse(result["cleanup"]["manual_verification_required"])
        self.assertTrue(all(action["result"]["verified"] for action in result["cleanup"]["actions"]
                            if action["module"] in {"temperature", "lockin"}))
        self.assertNotIn("recovery_owner_verification_failed", [kind for kind, _ in self.events()])
        self.assertTrue(self.manager.closed)
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertFalse(self.dll.connected)

    def test_process_change_after_last_accepted_does_not_block_cleanup(self):
        config = self.seed(("lockin",))
        stop = [False]
        calls = []
        event = CombinationStore.event
        def guard():
            calls.append(True)
            if stop[0]:
                raise ValueError("new owner before final cleanup")
        def observe(store, run_id, kind, payload):
            result = event(store, run_id, kind, payload)
            if kind == "condition_accepted" and payload["condition_id"] == "condition-000001":
                stop[0] = True
            return result
        with patch.object(CombinationStore, "event", observe):
            result = self.execute(config, recovery=self.recovery(config), owner_guard=guard)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(stop[0])
        self.assertEqual(calls, [])
        self.assertEqual(len(hardware_recovery_state(self.database, "recovery-test")["accepted_condition_ids"]), 2)
        self.assertTrue(result["cleanup"]["clean"])
        self.assertFalse(result["cleanup"]["manual_verification_required"])
        self.assertEqual(float(self.xx.responses["SLVL?"].strip()), .004)
        self.assertEqual(float(self.xy.responses["SLVL?"].strip()), .004)
        self.assertTrue(self.manager.closed)

    def test_legacy_process_callback_is_not_called_during_cleanup(self):
        config = self.seed(("lockin",))
        cleanup_started = [False]
        cleanup_guards = [0]
        event = CombinationStore.event
        def guard():
            if cleanup_started[0]:
                cleanup_guards[0] += 1
                raise ValueError("unrelated owner changed during cleanup")
        def observe(store, run_id, kind, payload):
            result = event(store, run_id, kind, payload)
            if kind == "cleanup_started":
                cleanup_started[0] = True
            return result
        with patch.object(CombinationStore, "event", observe):
            result = self.execute(config, recovery=self.recovery(config), owner_guard=guard)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(cleanup_started[0])
        self.assertEqual(cleanup_guards[0], 0)
        self.assertTrue(result["cleanup"]["clean"])
        self.assertTrue(result["cleanup"]["actions"][0]["result"]["verified"])
        self.assertFalse(result["cleanup"]["manual_verification_required"])
        self.assertTrue(self.manager.closed)

    def test_unknown_current_communication_cannot_be_excused_by_review(self):
        config = self.seed(("temperature", "lockin"))
        self.dll.return_codes["get_sample_temperature"] = 19
        result = self.execute(config, recovery=self.recovery(config, manual=True))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.xx.writes + self.xy.writes, [])
        self.assertNotIn("set_temperature", self.dll.events)

    def test_smu_nonzero_off_source_blocks_recovery_before_lockin_or_configuration(self):
        config = self.seed(("smu", "lockin"))
        self.modify_adapter = lambda adapter: setattr(adapter, "source", .0001)
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("zero/OFF", result["error"])
        self.assertEqual(self.manager.opened, [])
        self.assertFalse(any(event[0] in {"source", "output", "configure"} for event in self.log))

    def test_lockin_baseline_output_overload_blocks_recovery_before_smu_enable(self):
        config = self.seed(("smu", "lockin"))
        self.xy.responses["LIAS?"] = "4"
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("baseline repair", result["error"])
        self.assertEqual(self.xx.writes + self.xy.writes, [])
        self.assertFalse(any(event[0] in {"source", "output", "configure"} for event in self.log))

    def test_lockin_nonminimum_or_h2_baseline_requires_explicit_repair(self):
        for field, value in (("SLVL?", "0.008"), ("HARM?", "2")):
            with self.subTest(field=field):
                config = self.seed(("lockin",)) if field == "SLVL?" else load_hardware_combination(self.path)
                self.xx.responses[field] = value
                result = self.execute(config, recovery=self.recovery(config))
                self.assertEqual(result["status"], "failed", result)
                self.assertIn("configuration verification failed", result["error"])
                self.assertEqual(self.xx.writes + self.xy.writes, [])
                self.xx.responses[field] = "0.004" if field == "SLVL?" else "1"

    def test_magnetic_resume_requalifies_from_current_state_and_retains_history(self):
        config = self.seed(("magnetic", "lockin"))
        self.dll.bx_t = self.dll.setpoint_x_t = .02
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "completed", result)
        kinds = [kind for kind, _ in self.events()]
        self.assertEqual(kinds.count("recovery_magnetic_path_verified"), 2)
        self.assertIn("magnetic_point_event", kinds)
        self.assertTrue(result["cleanup"]["clean"])

    def test_cleanup_only_does_not_enable_smu_or_take_formal_pairs(self):
        config = self.seed(("temperature", "smu", "lockin"), accepted=6)
        previous_events = len(self.events())
        result = self.execute(config, recovery=self.recovery(config))
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(any(event[:1] == ("output",) and event[-1] is True for event in self.log))
        kinds = [kind for kind, _ in self.events()[previous_events:]]
        self.assertIn("cleanup_recovery_prepared", kinds)
        self.assertNotIn("lockin_formal_pair", kinds)
        self.assertNotIn("smu_formal_sample", kinds)
        self.assertTrue(self.dll.temperature_control)
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_6_of_342_temperature_failure_recovers_entire_seventh_condition(self):
        excitations = [.1 + .07 * i for i in range(11)] + [.85 + i / 7 for i in range(8)]
        source = self.base.replace('start_k = 2.0\nstop_k = 2.0\nstep_k = 0.1',
            'temperature_ranges = [\n{ min = 11.7, max = 110.0, scale = "linear", points = 4 },\n'
            '{ min = 115.0, max = 140.0, scale = "linear", points = 11 },\n'
            '{ min = 150.0, max = 250.0, scale = "linear", points = 3 }\n]')
        source = source.replace("excitation_points_v_rms = [0.004, 0.008]",
            "excitation_points_v_rms = " + json.dumps(excitations))
        source = source.replace("frequency_hz = 17.777", "frequency_hz = 200.0")
        source = source.replace('acceptance_mode = "stable-readback"', 'acceptance_mode = "target"')
        source = source.replace('acceptance_mode = "target"',
            'acceptance_mode = "target"\ntolerance_k = 0.05')
        self.write_config(("temperature", "lockin"), source=source)
        config = load_hardware_combination(self.path)
        self.assertEqual(len(config.plan.conditions()), 342)
        sample_count = [0]
        capture = CryostatPointSession.capture
        def fail_seventh(session, stage, *args, **kwargs):
            if stage == "before_sample":
                sample_count[0] += 1
            result = capture(session, stage, *args, **kwargs)
            if stage == "before_sample" and sample_count[0] == 7:
                # Inject the historical failure explicitly. New acquisitions
                # record ordinary drift instead of recreating this old gate.
                raise AttoDryError("Temperature drifted from qualified baseline")
            return result
        with patch.object(CryostatPointSession, "capture", fail_seventh):
            failed = self.execute(config)
        self.assertEqual(failed["status"], "failed", failed)
        with CombinationStore(self.database) as store:
            # Reproduce the deployed legacy schema-1 audit, which has no
            # execution ownership tables and advertises no hardware resume.
            # All original conditions, attempts, samples and cleanup stay intact.
            store.connection.execute("DROP TABLE combination_attempt_executions")
            store.connection.execute("DROP TABLE combination_executions")
            row = store.connection.execute(
                "SELECT plan_json FROM combination_runs WHERE run_id='recovery-test'").fetchone()
            legacy = json.loads(row[0])
            legacy["resume_supported"] = False
            legacy.pop("hardware_resume_contract", None)
            store.connection.execute("UPDATE combination_runs SET plan_json=? WHERE run_id='recovery-test'",
                (json.dumps(legacy),))
            store.connection.commit()
        original = hardware_recovery_state(self.database, "recovery-test")
        self.assertEqual(len(original["accepted_condition_ids"]), 6)
        self.assertFalse(failed["cleanup"]["clean"])
        self.assertFalse(self.dll.temperature_control)
        # Both stricter and looser stability policy changes are new execution
        # configuration; no old qualification mean is carried into this session.
        self.path.write_text(self.path.read_text(encoding="utf-8").replace("stable_range_k = 0.05",
            "stable_range_k = 0.08"), encoding="utf-8")
        resumed_config = load_hardware_combination(self.path)
        review = self.recovery(resumed_config)
        self.assertEqual(review["next_condition"]["condition_id"], "condition-000006")
        before_sample_hashes = {tuple(row[k] for k in ("condition_id", "attempt_index", "sample_index")):
                                row["payload_sha256"] for row in original["sample_manifest"]}
        result = self.execute(resumed_config, recovery=review)
        self.assertEqual(result["status"], "completed", result)
        final = hardware_recovery_state(self.database, "recovery-test")
        self.assertEqual(len(final["accepted_condition_ids"]), 342)
        self.assertEqual([execution["kind"] for execution in final["executions"]], ["legacy", "resume"])
        self.assertEqual([a["status"] for a in final["attempts"]
                          if a["condition_id"] == "condition-000006"], ["rejected", "accepted"])
        after_sample_hashes = {tuple(row[k] for k in ("condition_id", "attempt_index", "sample_index")):
                               row["payload_sha256"] for row in final["sample_manifest"]}
        self.assertTrue(all(after_sample_hashes[key] == value for key, value in before_sample_hashes.items()))
        kinds = [kind for kind, _ in self.events()]
        # One original entry plus all 18 fresh temperature entries on resume.
        self.assertEqual(kinds.count("temperature_qualification_started"), 19)
        self.assertTrue(result["cleanup"]["clean"])


if __name__ == "__main__":
    unittest.main()
