"""Reference recovery across complete optical/electrical windows with fake devices."""
from contextlib import closing, contextmanager
import json
import tomllib
import unittest
from unittest.mock import patch

from tests import test_combination_photonics as fixtures
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import HardwareCombinationStation
from attodry_control.combination_store import open_readonly
from attodry_control.optical_points import OpticalPointSession
from attodry_control.reference_transients import ReferenceTransientError


XX = "measured.lockin_xx_h1_x_v"
XY = "measured.lockin_xy_h1_x_v"


class PhotonicsReferenceRecoveryCombinationTests(unittest.TestCase):
    write_config = fixtures.PhotonicsCombinationTests.write_config
    on_lockin_query = fixtures.PhotonicsCombinationTests.on_lockin_query
    backend_factory = fixtures.PhotonicsCombinationTests.backend_factory
    pem_factory = fixtures.PhotonicsCombinationTests.pem_factory
    meter_factory = fixtures.PhotonicsCombinationTests.meter_factory
    execute = fixtures.PhotonicsCombinationTests.execute
    events = fixtures.PhotonicsCombinationTests.events
    shared_visa_setup = fixtures.PhotonicsCombinationTests.shared_visa_setup
    assert_shared_cleanup_protection = fixtures.PhotonicsCombinationTests.assert_shared_cleanup_protection

    def setUp(self):
        fixtures.PhotonicsCombinationTests.setUp(self)
        self.shared_visa_setup()
        document = tomllib.loads(self.path.read_text(encoding="utf-8"))
        document["nkt_run"]["points"] = document["nkt_run"]["points"][:1]
        document["power_feedback"]["target_powers_w"] = document["power_feedback"]["target_powers_w"][:1]
        document["lockin_xy"]["harmonics"] = [1]
        document["photonics_lockin"].update(
            reference_transient_policy="wait_stable",
            reference_recovery_timeout_s=20.0,
            reference_recovery_consecutive_good=2,
        )
        self.path.write_text(fixtures.toml_document(document), encoding="utf-8")
        self.phase = None
        self.window_index = 0
        self.begin_counts = []
        self.module_windows = []
        self.guard_observations = []
        self.station = None
        self.injected = 0
        self.formal_snaps = 0
        self.phase_frequencies = 0

    def configure_recovery(self, **values):
        document = tomllib.loads(self.path.read_text(encoding="utf-8"))
        document["photonics_lockin"].update(values)
        self.path.write_text(fixtures.toml_document(document), encoding="utf-8")

    def stored_samples(self):
        with closing(open_readonly(self.database)) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM combination_samples ORDER BY sample_index")]

    def inject_frequency(self, phase, *, persistent=False, window=None):
        """Return a valid numeric but unapproved observation, without a setting write."""
        resource = self.manager.resources["FAKE::XX"]
        original = resource.query

        def query(command):
            response = original(command)
            if self.phase == "formal" and command == "SNAP? 1,2,3,4,9":
                self.formal_snaps += 1
            if self.phase == phase and command == "FREQ?":
                self.phase_frequencies += 1
            if phase == "formal":
                # Two setting diagnostics and the before bracket precede
                # the actual formal SNAP in this single-harmonic fixture.
                selected = (self.phase == phase and command == "SNAP? 1,2,3,4,9"
                            and self.formal_snaps == 4)
            else:
                # The diagnostic FREQ is retained separately; the following
                # independent frequency query drives the reference guard.
                selected = (self.phase == phase and command == "FREQ?"
                            and self.phase_frequencies == 2)
            if selected and (window is None or self.window_index == window) and (persistent or not self.injected):
                self.injected += 1
                self.log.append(("injected_reference", phase, command, 40179.0))
                if phase == "formal":
                    # A distinct rejected voltage proves that a later good read
                    # is not patched into this failed pair.
                    return "0.000777,0.0002,0.000802,14,40179"
                return "40179"
            return response

        resource.query = query

    @contextmanager
    def observed_windows(self, *, after_power_fault=False):
        begin = HardwareCombinationStation.begin_sample
        read = HardwareCombinationStation.read
        end = HardwareCombinationStation.end_sample
        guard = OpticalPointSession.guard_reference_recovery

        def begin_window(station):
            self.station = station
            self.window_index += 1
            self.begin_counts.append(station.lockin.sample_count)
            self.formal_snaps = 0
            self.phase_frequencies = 0
            self.phase = "begin"
            try:
                return begin(station)
            finally:
                self.phase = None

        def read_module(station, module):
            self.module_windows.append((self.window_index, module))
            self.phase = "formal" if module == "lockin" else None
            try:
                result = read(station, module)
                result["status"]["synthetic_window"] = self.window_index
                return result
            except ReferenceTransientError:
                if after_power_fault:
                    # The after bracket must reject this true optical fault
                    # instead of concealing it behind the reference transient.
                    self.meter.resource.power_provider = lambda: 0.000049
                raise
            finally:
                self.phase = None

        def end_window(station, reads):
            self.phase = "end"
            self.phase_frequencies = 0
            try:
                return end(station, reads)
            finally:
                self.phase = None

        def recovery_guard(optical, *, deadline):
            before = self.hardware_writes()
            optical_deadline = optical.deadline
            active = optical.sample_active
            try:
                return guard(optical, deadline=deadline)
            finally:
                self.guard_observations.append({
                    "deadline": deadline, "active": active,
                    "optical_deadline_before": optical_deadline,
                    "optical_deadline_after": optical.deadline,
                    "writes_before": before, "writes_after": self.hardware_writes(),
                })

        with patch.object(HardwareCombinationStation, "begin_sample", begin_window), \
                patch.object(HardwareCombinationStation, "read", read_module), \
                patch.object(HardwareCombinationStation, "end_sample", end_window), \
                patch.object(OpticalPointSession, "guard_reference_recovery", recovery_guard):
            yield

    def hardware_writes(self):
        return {
            "lockin": tuple(tuple(resource.writes) for resource in self.manager.resources.values()),
            "nkt": tuple(command for command in self.backend.commands
                         if command[0] in {"emission", "configure"}) if self.backend else (),
            "pem": tuple(command for command in self.pem.transport.commands
                         if command.startswith((":MOD:AMP ", ":SYS:PEMO "))) if self.pem else (),
            "pm": tuple(command for operation, command in self.meter.resource.commands
                        if operation == "write") if self.meter else (),
        }

    def assert_complete_fresh_retry(self, result, *, partial_modules):
        self.assertEqual(result["status"], "completed", result["error"])
        self.assert_shared_cleanup_protection(result)
        self.assertEqual(self.injected, 1)
        self.assertEqual(self.begin_counts, [0, 0, 1])
        self.assertEqual(self.station.lockin.sample_count, 2)
        self.assertFalse(self.station.optical.sample_active)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["clean"] and XX in row and XY in row for row in rows))
        payloads = self.stored_samples()
        self.assertEqual(len(payloads), 2)
        self.assertEqual([sample["reads"][0]["status"]["synthetic_window"]
                          for sample in payloads], [2, 3])
        for sample in payloads:
            windows = {reading["status"]["synthetic_window"] for reading in sample["reads"]}
            self.assertEqual(len(windows), 1)
            optical = next(reading for reading in sample["reads"] if reading["module"] == "optical")
            self.assertEqual([evidence["phase"] for evidence in optical["status"]["sample_brackets"]],
                             ["before_electrical", "formal", "after_electrical"])
        rejected = [payload for kind, payload in self.events() if kind == "reference_sample_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["sample_index"], 0)
        self.assertEqual(rejected[0]["candidate_index"], 1)
        self.assertFalse(rejected[0]["accepted"])
        self.assertFalse(rejected[0]["valid_for_analysis"])
        self.assertEqual([reading["module"] for reading in rejected[0]["partial_reads"]], partial_modules)
        self.assertEqual([payload["sample_index"] for kind, payload in self.events()
                          if kind == "formal_sample"], [0, 1])
        self.assertTrue(self.guard_observations)
        self.assertEqual(len({item["deadline"] for item in self.guard_observations}), 1)
        for item in self.guard_observations:
            self.assertFalse(item["active"])
            self.assertEqual(item["writes_before"], item["writes_after"])
            self.assertEqual(item["optical_deadline_before"], item["optical_deadline_after"])

    def test_formal_pair_transient_repeats_optical_and_both_lockins(self):
        self.inject_frequency("formal")
        with self.observed_windows():
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assert_complete_fresh_retry(result, partial_modules=["optical"])
        self.assertEqual(self.module_windows[:4],
                         [(1, "optical"), (1, "lockin"), (2, "optical"), (2, "lockin")])
        raw = [payload["sample"] for kind, payload in self.events()
               if kind == "lockin_raw_role" and payload["role"] == "lockin_xx"]
        self.assertTrue(any(sample["reference_frequency_hz"] == 40179
                            and sample["x_v"] == 0.000777 for sample in raw))
        self.assertTrue(all(row[XX] != 0.000777 for row in load_combination_rows(self.database)))

    def test_end_reference_transient_discards_complete_candidate_and_repeats_all_reads(self):
        self.inject_frequency("end")
        with self.observed_windows():
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assert_complete_fresh_retry(result, partial_modules=["optical", "lockin"])
        operations = [payload for kind, payload in self.events()
                      if kind == "lockin_reference_operation_rejected"]
        self.assertEqual(len(operations), 1)
        candidates = operations[0]["rejected_formal_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["rejected_reference_candidate"])
        self.assertFalse(candidates[0]["valid_for_analysis"])
        self.assertTrue(all(entry["valid_for_analysis_by_role"] ==
                            {"lockin_xx": False, "lockin_xy": False}
                            for entry in candidates[0]["status"]["samples"]))

    def test_begin_reference_failure_closes_open_optical_bracket_before_retry(self):
        self.inject_frequency("begin")
        with self.observed_windows():
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assert_complete_fresh_retry(result, partial_modules=[])
        self.assertEqual(self.module_windows[:2], [(2, "optical"), (2, "lockin")])

    def test_after_power_fault_is_primary_and_reference_transient_is_not_retried(self):
        self.inject_frequency("formal")
        with self.observed_windows(after_power_fault=True):
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertIn("Formal optical power left its qualified target", result["error"])
        self.assertEqual(self.begin_counts, [0])
        self.assertEqual(self.station.lockin.sample_count, 0)
        self.assertFalse(self.station.optical.sample_active)
        self.assertEqual(self.stored_samples(), [])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in self.events()))
        self.assert_shared_cleanup_protection(result)

    def test_repeated_end_transients_share_deadline_and_never_count_rejected_candidates(self):
        self.configure_recovery(reference_recovery_timeout_s=3.0)
        self.inject_frequency("end", persistent=True)
        with self.observed_windows():
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertGreaterEqual(self.injected, 2)
        self.assertIn("deadline", result["error"].lower())
        self.assertTrue(all(count == 0 for count in self.begin_counts))
        self.assertEqual(self.station.lockin.sample_count, 0)
        self.assertFalse(self.station.optical.sample_active)
        self.assertEqual(self.stored_samples(), [])
        started = [payload for kind, payload in self.events()
                   if kind == "reference_recovery_started"]
        self.assertGreaterEqual(len(started), 2)
        self.assertEqual(len({item["deadline_monotonic"] for item in started}), 1)
        self.assertTrue(self.guard_observations)
        for item in self.guard_observations:
            self.assertFalse(item["active"])
            self.assertEqual(item["writes_before"], item["writes_after"])
            self.assertEqual(item["optical_deadline_before"], item["optical_deadline_after"])
        self.assert_shared_cleanup_protection(result)

    def test_dirty_nonreference_read_cannot_be_hidden_by_after_reference_transient(self):
        self.inject_frequency("end")
        with self.observed_windows():
            original = HardwareCombinationStation.read

            def dirty_optical(station, module):
                reading = original(station, module)
                if module == "optical":
                    reading.update(clean=False, valid_for_analysis=False,
                                   problems=["synthetic nonreference readback failure"])
                return reading

            with patch.object(HardwareCombinationStation, "read", dirty_optical):
                result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertIn("Noncontinuable formal readback", result["error"])
        self.assertEqual(self.injected, 1)
        self.assertEqual(self.begin_counts, [0])
        self.assertEqual(self.module_windows, [(1, "optical")])
        self.assertEqual(self.station.lockin.sample_count, 0)
        self.assertFalse(self.station.optical.sample_active)
        self.assertEqual(self.stored_samples(), [])
        self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in self.events()))
        raw = [payload["reading"] for kind, payload in self.events() if kind == "raw_reading"]
        self.assertEqual(len(raw), 1)
        self.assertFalse(raw[0]["clean"])
        self.assertEqual(raw[0]["problems"], ["synthetic nonreference readback failure"])
        self.assert_shared_cleanup_protection(result)

    def test_second_sample_retry_preserves_interval_and_accepted_sample_count(self):
        self.inject_frequency("end", window=2)
        sleeps = []
        original_sleep = self.clock.sleep

        def sleep(seconds):
            if self.station is not None:
                sleeps.append((self.window_index, self.phase,
                               self.station.lockin.sample_count, seconds,
                               self.station.optical.sample_active))
            return original_sleep(seconds)

        self.clock.sleep = sleep
        with self.observed_windows():
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(self.injected, 1)
        self.assertEqual(self.begin_counts, [0, 1, 1])
        self.assertEqual(self.station.lockin.sample_count, 2)
        self.assertFalse(self.station.optical.sample_active)
        samples = self.stored_samples()
        self.assertEqual(len(samples), 2)
        self.assertEqual([sample["reads"][0]["status"]["synthetic_window"] for sample in samples], [1, 3])
        self.assertTrue(all(len({reading["status"]["synthetic_window"] for reading in sample["reads"]}) == 1
                            for sample in samples))
        self.assertTrue(any(window == 3 and phase == "formal" and count == 1
                            and abs(seconds - 0.01) < 1e-12 and active
                            for window, phase, count, seconds, active in sleeps))
        rejected = [payload for kind, payload in self.events() if kind == "reference_sample_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["sample_index"], 1)
        self.assertEqual(rejected[0]["candidate_index"], 1)
        self.assertFalse(rejected[0]["valid_for_analysis"])
        self.assertTrue(self.guard_observations)
        self.assertFalse(self.guard_observations[0]["active"])
        self.assertTrue(any(item["active"] for item in self.guard_observations))
        for item in self.guard_observations:
            self.assertEqual(item["writes_before"], item["writes_after"])
            self.assertEqual(item["optical_deadline_before"], item["optical_deadline_after"])
        optical = next(reading for reading in samples[1]["reads"] if reading["module"] == "optical")
        phases = [item["phase"] for item in optical["status"]["sample_brackets"]]
        self.assertEqual(phases[0], "before_electrical")
        self.assertEqual(phases[-1], "after_electrical")
        self.assertEqual(phases.count("formal"), 1)
        self.assertIn("reference_recovery", phases)
        self.assertEqual(len(load_combination_rows(self.database)), 2)
        self.assert_shared_cleanup_protection(result)

    def test_plain_record_continue_unlock_remains_diagnostic_without_recovery(self):
        self.configure_recovery(reference_unlock_policy="record_continue")
        resource = self.manager.resources["FAKE::XX"]
        original = resource.on_query

        def unlock(device, command):
            original(device, command)
            if self.phase == "formal" and command == "LIAS?":
                device.responses[command] = "8"

        resource.on_query = unlock
        with self.observed_windows():
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(self.begin_counts, [0, 1])
        self.assertEqual(self.station.lockin.sample_count, 2)
        self.assertFalse(self.guard_observations)
        self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in self.events()))
        audit = load_combination_rows(self.database, audit=True)
        default = load_combination_rows(self.database)
        self.assertEqual(len(audit), 2)
        self.assertEqual(len(default), 2)
        self.assertTrue(all(XX in row and XY in row and not row["clean"] for row in audit))
        self.assertTrue(all(XX not in row and XY not in row for row in default))
        self.assertTrue(all(row["valid_for_analysis_by_role"] ==
                            {"lockin_xx": False, "lockin_xy": False} for row in audit))
        self.assert_shared_cleanup_protection(result)


if __name__ == "__main__":
    unittest.main()
