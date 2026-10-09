"""Continuous gate illumination contracts using injected, offline instruments."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
import tomllib
import unittest
from unittest.mock import patch

import test_combination_photonics as fixture
from photonics_lockin_helpers import (
    FakeManager, reversed_sine_document, reversed_sine_safety_document,
)
from tests.test_three_smu import FakeAdapter
from tests.test_three_smu_config import OPERATION_TEXT
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.optical_points import OpticalPointSession
from attodry_control.reference_transients import ReferenceTransientError


class ContinuousGateCombinationTests(unittest.TestCase):
    def make(self, policy="continuous_gate_scan", *, repeats=1, duplicate=False,
             bottom=True):
        case = fixture.PhotonicsCombinationTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        optical_points = doc["nkt_run"]["points"][:2]
        if duplicate:
            optical_points[1] = dict(optical_points[0])
        doc["nkt_run"]["points"] = optical_points
        doc["power_feedback"]["target_powers_w"] = [5e-6, 5e-6 if duplicate else 22.5e-6]
        reversed_doc = reversed_sine_document()
        for key in ("lockin_xx", "lockin_xy", "photonics_lockin", "lockin_sweep"):
            doc[key] = reversed_doc[key]
        doc["lockin_sweep"]["excitation_points_v_rms"] = [.006]
        smu = tomllib.loads(OPERATION_TEXT)
        smu.pop("smu_bias")
        smu["three_smu_run"].update(delay_s=.03, serpentine=False)
        smu["three_smu_run"]["smu_bias"] = {"role": "off", "bidirectional": False}
        smu["three_smu_run"]["gate_top"]["points"] = [-.1, 0.0, .1]
        if bottom:
            smu["three_smu_run"]["gate_bottom"] = {
                "role": "fixed", "bidirectional": False, "fixed": .05}
        else:
            smu.pop("gate_bottom")
            smu["three_smu_run"]["gate_bottom"] = {"role": "off", "bidirectional": False}
        if policy == "continuous_gate_scan":
            for role in ("gate_top", "gate_bottom"):
                if smu["three_smu_run"][role]["role"] != "off":
                    smu["three_smu_run"][role]["ramp"] = {
                        "max_step_v": .5, "step_interval_s": .2,
                        "readback_tolerance_v": .05, "timeout_s": 600.0}
        doc.update(smu)
        combo = doc["combination_scan"]
        combo.update(order=["optical", "smu", "lockin"],
                     reference_topology="pem_xy_xx_sine", repeats=repeats)
        if policy is not None:
            combo["illumination_policy"] = policy
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        (case.directory / "photonics_lockin_safety.toml").write_text(
            fixture.toml_document(reversed_sine_safety_document()), encoding="utf-8")
        case.manager = FakeManager(doc)
        xy = case.manager.resources["FAKE::XY"]
        xy.responses["SLVL?"], xy.responses["BLAZEX?"] = "0.2", "0"
        for resource in case.manager.resources.values():
            resource.on_query = case.on_lockin_query
            resource.on_write = lambda r, c: case.log.append(("lockin", r.role, c))
        case.adapters = {}
        case.modify_adapter = lambda adapter: None
        case.gate_writes = []
        case.fault_seen = False
        sleep = case.clock.sleep
        def observed_sleep(seconds):
            case.log.append(("sleep", seconds, case.clock()))
            return sleep(seconds)
        case.clock.sleep = observed_sleep
        def factory(role, config):
            adapter = FakeAdapter(role, case.log, config)
            original_source, original_read = adapter.set_source, adapter.read
            def set_source(value):
                backend = case.backend
                emission = None if backend is None else backend.source["emission_state"]
                state = None if backend is None else (
                    dict(backend.source), dict(backend.filter))
                optical_writes = () if backend is None else tuple(
                    command for command in backend.commands if command[0] in {"configure", "emission"})
                source_writes = tuple(c for c in case.manager.resources["FAKE::XX"].writes
                                      if c.startswith("SLVL "))
                entry = ("gate_write", role, value, emission, case.clock(), state,
                         optical_writes, source_writes)
                case.log.append(entry)
                case.gate_writes.append(entry)
                return original_source(value)
            def read():
                case.log.append(("gate_read", role, adapter.source, case.clock()))
                return original_read()
            adapter.set_source, adapter.read = set_source, read
            case.modify_adapter(adapter)
            case.adapters[role] = adapter
            return adapter
        case.smu_factory = factory
        return case

    def execute(self, case, run_id="continuous", **kwargs):
        original = CombinationStore.event
        def event(store, actual_run_id, kind, payload):
            case.log.append(("event", kind, deepcopy(payload)))
            return original(store, actual_run_id, kind, payload)
        with patch.object(CombinationStore, "event", event):
            return case.execute(run_id, confirm_xy_sine_disconnected=False,
                                smu_adapter_factory=case.smu_factory, **kwargs)

    def samples(self, case):
        with closing(open_readonly(case.database)) as connection:
            return [json.loads(r[0]) for r in connection.execute(
                "SELECT payload_json FROM combination_samples ORDER BY rowid")]

    def assert_failed_raw_and_off_first(self, case, result, *, status="failed",
                                       manual=False, off_confirmed=True):
        self.assertEqual(result["status"], status, result)
        self.assertEqual(load_combination_rows(case.database), ())
        with closing(open_readonly(case.database)) as connection:
            attempts = connection.execute("SELECT status FROM combination_attempts").fetchall()
        self.assertTrue(attempts)
        self.assertTrue(all(row[0] == "rejected" for row in attempts))
        self.assertTrue(any(kind == "condition_started" for kind, _ in case.events()))
        self.assertTrue(any(kind == "gate_ramp_write_attempt" for kind, _ in case.events()))
        self.assertTrue(any(kind == "gate_ramp_error" for kind, _ in case.events()))
        fault = next(i for i, entry in enumerate(case.log) if entry[0] == "fault")
        off = next(i for i, entry in enumerate(case.log[fault + 1:], fault + 1)
                   if entry == ("laser", False))
        electrical = [i for i, entry in enumerate(case.log[fault + 1:], fault + 1)
                      if entry[0] == "gate_write" or
                      (entry[:2] == ("lockin", "lockin_xx") and entry[2].startswith("SLVL "))]
        self.assertTrue(electrical)
        self.assertLess(off, min(electrical))
        actions = {item["module"]: item for item in result["cleanup"]["actions"]}
        self.assertEqual(actions["optical"]["result"]["off_confirmed"], off_confirmed)
        self.assertEqual(result["cleanup"]["manual_verification_required"], manual)
        if off_confirmed:
            self.assertEqual(case.backend.source["emission_state"], 0)
        if not manual:
            self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        else:
            self.assertFalse(result["cleanup"]["clean"], result["cleanup"])
            self.assertTrue(all(action["output_off_confirmed"] and not action["zero_readback_recorded"]
                                for action in actions["smu"]["result"]["actions"]))
        for adapter in case.adapters.values():
            self.assertEqual(adapter.source, 0)
            self.assertFalse(adapter.output)
        self.assertTrue(case.manager.closed and case.meter.closed and case.pem.closed)

    def test_default_policy_keeps_dark_gate_changes_and_full_qualification_each_gate(self):
        for policy in (None, "off_before_axis_change"):
            with self.subTest(policy=policy):
                case = self.make(policy)
                result = self.execute(case, "legacy-" + str(policy))
                self.assertEqual(result["status"], "completed", result)
                qualification = [payload for kind, payload in case.events()
                                 if kind == "optical_condition_qualified"]
                self.assertEqual(len(qualification), 6)
                target_events = [entry for entry in case.log
                    if entry[:2] == ("event", "source_set")]
                self.assertEqual(len(target_events), 12)
                for entry in target_events:
                    index = case.log.index(entry)
                    prior = next(item for item in reversed(case.log[:index])
                                 if item[0] == "gate_write")
                    self.assertEqual(prior[3], 0)

    def test_qualification_precedes_first_lit_gate_and_settings_are_held_for_all_gates(self):
        case = self.make()
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        groups = [i for i, entry in enumerate(case.log)
                  if entry[:2] == ("event", "continuous_optical_group_qualified")]
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(load_combination_rows(case.database)), 12)
        for group, boundary in zip(groups, groups[1:] + [len(case.log)]):
            writes = [entry for entry in case.log[group + 1:boundary]
                      if entry[0] == "gate_write" and entry[3] == 3]
            self.assertEqual([w[2] for w in writes if w[1] == "gate_top"], [-.1, 0, .1])
            self.assertTrue(all(w[2] == .05 for w in writes if w[1] == "gate_bottom"))
            self.assertTrue(all(write[5:] == writes[0][5:] for write in writes))
            self.assertAlmostEqual(float(writes[0][7][-1].split()[1]), .006)
        self.assertEqual(set(case.adapters), {"gate_top", "gate_bottom"})
        self.assertTrue(any(w[1:4] == ("gate_bottom", .05, 3) for w in case.gate_writes))
        self.assertFalse(any(command.startswith(("SLVL ", "SOFF ", "REFM ", "BLAZEX ", "FREQ "))
                             for command in case.manager.resources["FAKE::XY"].writes))
        self.assertEqual(case.backend.source["emission_state"], 0)

    def test_each_formal_row_has_fresh_power_brackets_and_fresh_gate_reads(self):
        case = self.make()
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        samples = self.samples(case)
        self.assertEqual(len(samples), 12)
        sequences = []
        for sample in samples:
            optical = next(read for read in sample["reads"] if read["module"] == "optical")
            brackets = optical["status"]["sample_brackets"]
            self.assertEqual([item["phase"] for item in brackets],
                             ["before_electrical", "formal", "after_electrical"])
            self.assertTrue(all(item["state"]["nkt"]["source"]["emission_state"] == 3
                                for item in brackets))
            current_sequences = [item["power_sample"]["sequence"] for item in brackets]
            self.assertEqual(current_sequences, sorted(current_sequences))
            sequences.extend(current_sequences)
            smu = next(read for read in sample["reads"] if read["module"] == "smu")
            self.assertEqual(set(smu["status"]["readings"]), {"gate_top", "gate_bottom"})
            self.assertTrue(sample["acquisition_accepted"])
        self.assertEqual(len(set(sequences)), 36)
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len([entry for entry in case.log
                             if entry[:2] == ("event", "smu_formal_sample")]), 12)

    def test_each_gate_obeys_smu_delay_and_electrical_qualification_before_formal_read(self):
        case = self.make(bottom=False)
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        for index, entry in enumerate(case.log):
            if entry[0] != "gate_write" or entry[3] != 3:
                continue
            following = case.log[index + 1:]
            read = next(item for item in following if item[:2] == ("gate_read", "gate_top"))
            self.assertGreaterEqual(read[3] - entry[4] + 1e-12, .03)
            formal = next(i for i, item in enumerate(following)
                          if item[:2] == ("event", "formal_sample"))
            self.assertTrue(any(item[:2] == ("event", "lockin_point_qualification")
                                for item in following[:formal]))
        self.assertEqual(set(case.adapters), {"gate_top"})

    def test_harmonic_transitions_retain_settled_evidence_under_continuous_light(self):
        case = self.make()
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc["lockin_xy"]["harmonics"] = [1, 2]
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        for sample in self.samples(case):
            lockin = next(read for read in sample["reads"] if read["module"] == "lockin")
            pairs = lockin["status"]["samples"]
            self.assertEqual([entry["selected_harmonics"].get("xy") for entry in pairs], [1, 2])
            self.assertIn("settled", pairs[1]["bracket_samples"])
            self.assertTrue(all(entry["settings_verified"] for entry in pairs))
        self.assertEqual(len([payload for kind, payload in case.events()
                              if kind == "optical_condition_qualified"]), 2)

    def test_duplicate_optical_rows_and_repeats_start_distinct_groups_preserving_indices(self):
        case = self.make(repeats=2, duplicate=True)
        config = load_hardware_combination(case.path)
        self.assertEqual(config.plan.axes[0].points[0].values, config.plan.axes[0].points[1].values)
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(case.database)
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({row["condition_id"] for row in rows}), 12)
        qualifications = [payload for kind, payload in case.events()
                          if kind == "continuous_optical_group_qualified"]
        self.assertEqual(len(qualifications), 4)
        conditions = config.plan.conditions()
        self.assertEqual([(row["repeat_index"], row["axes"]["optical"]["index"],
                           row["axes"]["smu"]["index"]) for row in conditions],
                         [(repeat, optical, gate) for repeat in range(2)
                          for optical in range(2) for gate in range(3)])
        self.assertEqual({(row["repeat_index"], row["axes.optical.index"], row["axes.smu.index"])
                          for row in rows},
                         {(repeat, optical, gate) for repeat in range(2)
                          for optical in range(2) for gate in range(3)})
        self.assertTrue(all(row["axes.lockin.index"] == 0 for row in rows))
        self.assertEqual([payload["condition_id"] for payload in qualifications],
                         [conditions[index]["condition_id"] for index in (0, 3, 6, 9)])
        self.assertEqual([payload["optical_point_index"] for payload in qualifications], [0, 1, 0, 1])
        self.assertEqual([payload["group_sequence"] for payload in qualifications], [1, 2, 3, 4])

    def test_policy_is_visible_offline_and_unknown_optional_keys_reject_without_io(self):
        case = self.make()
        config = load_hardware_combination(case.path)
        self.assertEqual(config.snapshot["illumination_policy"], "continuous_gate_scan")
        self.assertEqual(launch_summary(config, case.database, "preview")["illumination_policy"],
                         "continuous_gate_scan")
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc["combination_scan"]["illumination_polciy"] = "continuous_gate_scan"
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_hardware_combination(case.path)
        self.assertEqual(case.manager.opened, [])
        self.assertEqual(case.adapters, {})
        self.assertIsNone(case.backend)

    def test_unsupported_policy_modes_and_axes_reject_before_factories(self):
        case = self.make()
        base = tomllib.loads(case.path.read_text(encoding="utf-8"))
        variants = []
        for value in ("continuous", "", True, 1):
            doc = deepcopy(base)
            doc["combination_scan"]["illumination_policy"] = value
            variants.append(("policy " + repr(value), doc))
        for order in (["smu", "optical", "lockin"], ["optical", "lockin", "smu"],
                      ["optical", "lockin"], ["optical", "smu"],
                      ["temperature", "optical", "smu", "lockin"],
                      ["magnetic", "optical", "smu", "lockin"]):
            doc = deepcopy(base)
            doc["combination_scan"]["order"] = order
            variants.append(("order " + repr(order), doc))
        for mode in ("frequency", "frequency_excitation"):
            doc = deepcopy(base)
            doc["combination_scan"]["lockin_mode"] = mode
            variants.append((mode, doc))
        doc = deepcopy(base)
        doc["lockin_sweep"]["excitation_points_v_rms"] = [.004, .006]
        variants.append(("multiple excitation points", doc))
        doc = deepcopy(base)
        doc["gate_top"]["source_mode"] = "current"
        doc["three_smu_run"]["gate_top"]["points"] = [-1e-6, 0, 1e-6]
        variants.append(("current source gate", doc))
        doc = deepcopy(base)
        doc["smu_bias"] = {**doc["gate_top"], "address": "FAKE::BIAS"}
        doc["three_smu_run"]["smu_bias"] = {"role": "fixed", "bidirectional": False, "fixed": 0.0}
        variants.append(("active bias even at zero", doc))
        doc = deepcopy(base)
        doc["three_smu_run"].update(mode="software_pulse", pulse_high_s=.1, pulse_period_s=.2)
        doc["three_smu_run"]["gate_top"]["points"] = [-.1, .1]
        variants.append(("software pulse", doc))
        for role in ("gate_top", "gate_bottom"):
            doc = deepcopy(base)
            doc["three_smu_run"][role].pop("ramp")
            variants.append(("missing " + role + " direct zero tolerance", doc))
        doc = deepcopy(base)
        doc["nkt_run"]["emit"] = False
        variants.append(("dark optical mode", doc))
        doc = deepcopy(base)
        doc["optical_scan"]["use_power_meter"] = False
        variants.append(("unmonitored optical mode", doc))
        for description, doc in variants:
            with self.subTest(description=description):
                case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_hardware_combination(case.path)
        self.assertEqual(case.manager.opened, [])
        self.assertEqual(case.adapters, {})
        self.assertIsNone(case.backend)

    def test_compliance_and_unknown_gate_readback_abort_keep_raw_and_verify_off_first(self):
        for fault in ("compliance", "unknown"):
            with self.subTest(fault=fault):
                case = self.make()
                def modify(adapter):
                    original = adapter.read
                    def read():
                        reading = original()
                        if adapter.role == "gate_top" and adapter.source == -.1 and not case.fault_seen:
                            case.fault_seen = True
                            case.log.append(("fault", fault))
                            return replace(reading, **({"compliance_trip": True} if fault == "compliance"
                                                      else {"output_enabled": None}))
                        return reading
                    adapter.read = read
                case.modify_adapter = modify
                result = self.execute(case)
                self.assert_failed_raw_and_off_first(case, result, manual=True)
                rejected = [payload for kind, payload in case.events()
                            if kind == "gate_ramp_readback" and payload["problems"]]
                self.assertEqual(len(rejected), 1)
                evidence = rejected[0]["reading"]
                self.assertEqual(evidence["source_setpoint"], -.1)
                if fault == "compliance":
                    self.assertTrue(evidence["compliance_trip"])
                else:
                    self.assertIsNone(evidence["output_enabled"])

    def test_gate_communication_failure_does_not_certify_clean_cleanup(self):
        case = self.make()
        def modify(adapter):
            original = adapter.read
            def read():
                if adapter.role == "gate_top" and adapter.source == -.1 and not case.fault_seen:
                    case.fault_seen = True
                    case.log.append(("fault", "communication"))
                    raise OSError("synthetic illuminated gate communication failure")
                return original()
            adapter.read = read
        case.modify_adapter = modify
        result = self.execute(case)
        self.assert_failed_raw_and_off_first(case, result, manual=True)
        self.assertTrue(result["cleanup"]["communication_uncertain"])
        self.assertFalse(result["cleanup"]["clean"])
        self.assertIn("illuminated gate communication", result["error"])

    def test_reference_retry_keeps_lit_gate_settings_and_reacquires_fresh_complete_window(self):
        case = self.make()
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc["photonics_lockin"].update(reference_transient_policy="wait_stable",
            reference_recovery_timeout_s=20.0, reference_recovery_consecutive_good=2)
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        original_read = HardwareCombinationStation.read
        original_guard = OpticalPointSession.guard_reference_recovery
        guards = []
        def read(station, module):
            if module == "lockin" and not case.fault_seen:
                case.fault_seen = True
                raise ReferenceTransientError("synthetic valid reference mismatch",
                    kind="reference_pair_mismatch", evidence={"synthetic": True})
            return original_read(station, module)
        def guard(optical, *, deadline):
            before = (optical.deadline, deepcopy(case.backend.source),
                tuple(c for c in case.backend.commands if c[0] in {"emission", "configure"}),
                tuple(c for c in case.manager.resources["FAKE::XX"].writes if c.startswith("SLVL ")))
            result = original_guard(optical, deadline=deadline)
            after = (optical.deadline, deepcopy(case.backend.source),
                tuple(c for c in case.backend.commands if c[0] in {"emission", "configure"}),
                tuple(c for c in case.manager.resources["FAKE::XX"].writes if c.startswith("SLVL ")))
            guards.append((before, after))
            return result
        with patch.object(HardwareCombinationStation, "read", read), \
                patch.object(OpticalPointSession, "guard_reference_recovery", guard):
            result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        self.assertTrue(guards)
        self.assertTrue(all(before == after and before[1]["emission_state"] == 3
                            for before, after in guards))
        rejected = [payload for kind, payload in case.events() if kind == "reference_sample_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]["accepted"])
        self.assertEqual([reading["module"] for reading in rejected[0]["partial_reads"]], ["optical", "smu"])
        self.assertEqual(len(self.samples(case)), 12)
        self.assertEqual(len(load_combination_rows(case.database)), 12)
        self.assertEqual(len([payload for kind, payload in case.events()
                              if kind == "optical_condition_qualified"]), 2)

    def test_optical_guard_after_gate_write_aborts_without_retuning(self):
        case = self.make()
        original = OpticalPointSession._guard_sample
        def unsafe(session, phase, **kwargs):
            if case.adapters["gate_top"].source == -.1 and not case.fault_seen:
                case.fault_seen = True
                case.log.append(("fault", "optical_power"))
                case.meter.resource.power_provider = lambda: .00006
            return original(session, phase, **kwargs)
        with patch.object(OpticalPointSession, "_guard_sample", unsafe):
            result = self.execute(case)
        self.assert_failed_raw_and_off_first(case, result, manual=True)
        self.assertIn("power", result["error"].lower())
        fault = next(i for i, item in enumerate(case.log) if item[0] == "fault")
        self.assertFalse(any(item[0] == "gate_write" and item[3] == 3
                             for item in case.log[fault + 1:]))
        self.assertEqual(len([payload for kind, payload in case.events()
                              if kind == "optical_condition_qualified"]), 1)

    def test_cancellation_during_lit_gate_write_runs_verified_off_first_cleanup(self):
        case = self.make()
        def modify(adapter):
            original = adapter.set_source
            def set_source(value):
                original(value)
                if adapter.role == "gate_top" and value == -.1 and not case.fault_seen:
                    case.fault_seen = True
                    case.log.append(("fault", "cancel"))
                    raise KeyboardInterrupt()
            adapter.set_source = set_source
        case.modify_adapter = modify
        result = self.execute(case)
        self.assert_failed_raw_and_off_first(case, result, status="interrupted", manual=True)

    def test_failed_optical_off_is_unclean_even_when_electrical_cleanup_succeeds(self):
        case = self.make()
        original_factory = case.backend_factory
        def backend_factory(config):
            backend = original_factory(config)
            original = backend.set_emission
            def emission(enabled):
                if not enabled and case.fault_seen:
                    case.log.append(("laser", False))
                    raise OSError("synthetic laser OFF failure")
                return original(enabled)
            backend.set_emission = emission
            return backend
        case.backend_factory = backend_factory
        def modify(adapter):
            original = adapter.read
            def read():
                reading = original()
                if adapter.role == "gate_top" and adapter.source == -.1 and not case.fault_seen:
                    case.fault_seen = True
                    case.log.append(("fault", "compliance"))
                    return replace(reading, compliance_trip=True)
                return reading
            adapter.read = read
        case.modify_adapter = modify
        result = self.execute(case)
        self.assert_failed_raw_and_off_first(case, result, manual=True, off_confirmed=False)
        self.assertFalse(result["cleanup"]["clean"])
        self.assertIn("laser OFF failure", str(result["cleanup"]["errors"]))


if __name__ == "__main__":
    unittest.main()
