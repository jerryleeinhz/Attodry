"""Dual SR830/internal XX optical coordination with owned fake transports only."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from optical_helpers import Clock
from tests import test_combination_hardware as electrical
from tests.test_combination_photonics import toml_document
from tests.test_three_smu_config import OPERATION_TEXT
from attodry_control.chopper import ChopperSession, SimulatedChopperTransport
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.optical_points import OpticalPointSession
from attodry_control.pm100d import Pm100d, SimulatedPmResource


ROOT = Path(__file__).resolve().parents[1]


class InternalOpticalCombinationTests(unittest.TestCase):
    def setUp(self):
        electrical.ElectricalCombinationTests.setUp(self)
        self.clock = Clock()
        self.frequency["hz"] = 100.0
        self.backend = self.meter = self.chopper = None
        self.optical_log = []
        self.modify_chopper = lambda session: None
        self.document = tomllib.loads(self.base)
        optical = tomllib.loads((ROOT / "config/optical_source_current_simulation.toml").read_text())
        optical.pop("pem")
        optical["optical_scan"].update(use_pem=False)
        optical["optical_scan"].pop("peak_retardance_waves")
        optical["nkt_source"].update(dll_path="fake.dll", port="FAKE_NKT", address=1,
                                      expected_serial="SIMULATION")
        optical["nkt_varia"].update(address=2, expected_serial="SIMULATION")
        optical["nkt_run"].update(backend="nkt_sdk", points=optical["nkt_run"]["points"][:1])
        optical["pm100d"].update(backend="visa", resource="FAKE::PM",
            expected_serial="SIMULATION", expected_sensor_serial="SIMULATION")
        optical["power_feedback"]["target_powers_w"] = [5e-6]
        self.document.update(optical)
        for role in ("lockin_xx", "lockin_xy"):
            self.document[role].update(frequency_hz=100.0, time_constant_s=0.01)
        self.document["lockin_xx"]["sine_output_connected"] = False
        self.document["lockin_sweep"].update(excitation_points_v_rms=[0.004],
            excitation_xx_harmonics=[1], excitation_xy_harmonics=[1],
            settle_time_constants=5.0, sample_interval_time_constants=1.0)
        self.document["combination_scan"] = dict(backend="hardware", order=["optical", "lockin"],
            reference_topology="internal_xx_xy", samples_per_condition=2, repeats=1,
            run_name="fake_internal_optics", note="No instrument connections")
        self.write_document()

    write_config = electrical.ElectricalCombinationTests.write_config
    factory = electrical.ElectricalCombinationTests.factory
    events = electrical.ElectricalCombinationTests.events

    def write_document(self):
        self.path.write_text(toml_document(self.document), encoding="utf-8")

    def backend_factory(self, config):
        self.backend = SimulatedNkt(config)
        self.backend.is_hardware = True
        original = self.backend.set_emission
        def emission(enabled):
            self.optical_log.append(("emission", enabled, self.clock()))
            return original(enabled)
        self.backend.set_emission = emission
        return self.backend

    def meter_factory(self, config):
        resource = SimulatedPmResource(lambda: (
            0.0001 * (self.backend.source["level_pct"] / 40) ** 2
            if self.backend.source["emission_state"] == 3 else 0))
        self.meter = Pm100d(config, resource, is_hardware=True, authorize_settings=True,
                           authorize_measurement=True, clock=self.clock)
        return self.meter

    def chopper_factory(self, config):
        self.chopper = ChopperSession(config, authorized=True,
            transport=SimulatedChopperTransport(config), clock=self.clock, sleep=self.clock.sleep)
        self.modify_chopper(self.chopper)
        return self.chopper

    def enable_chopper(self, backend="serial"):
        self.document["chopper"] = dict(enabled=True, backend=backend, model="MC2000B",
            port="FAKE_CHOPPER", expected_idn="Thorlabs MC2000B FAKE", blade="MC1F10",
            reference_source="external", reference_track="single", harmonic_n=1, harmonic_d=1,
            manual_phase_lock_confirmed=True, ready_timeout_s=1.0, poll_interval_s=0.01,
            consecutive_good=2)
        self.write_document()

    def execute(self, run_id="internal", **kwargs):
        config = load_hardware_combination(self.path)
        flags = dict(authorize_hardware=True, confirm_xy_sine_disconnected=True,
                     authorize_optical=True, confirm_optical_route=True)
        flags.update(kwargs)
        with CombinationStore(self.database) as store:
            return run_hardware_combination(config, store, run_id,
                manager_factory=lambda: self.manager, smu_adapter_factory=self.factory,
                sleep=self.clock.sleep, monotonic=self.clock,
                optical_backend_factory=self.backend_factory, meter_factory=self.meter_factory,
                chopper_factory=self.chopper_factory, **flags)

    def assert_closed_laser_off(self):
        self.assertEqual(self.backend.source["emission_state"], 0)
        self.assertTrue(self.meter.closed)
        self.assertTrue(self.xx.closed and self.xy.closed and self.manager.closed)
        if self.chopper is not None:
            self.assertTrue(self.chopper.closed)

    def test_no_pem_h1_channels_sqlite_and_disconnected_sample_metadata(self):
        config = load_hardware_combination(self.path)
        self.assertFalse(config.optical.scan.use_pem)
        self.assertIsNone(config.optical.pem)
        self.assertFalse(config.snapshot["hardware"]["lockin"]["sample_excitation_connected"])
        self.assertEqual(self.manager.opened, [])
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("measured.lockin_xx_h1_x_v", row)
            self.assertIn("measured.lockin_xy_h1_x_v", row)
            self.assertNotIn("measured.lockin_xy_h2_x_v", row)
            self.assertIn("measured.optical_power_w", row)
        self.assertFalse(any(command == "HARM 2" for command in self.xx.writes + self.xy.writes))
        self.assertFalse(any(kind.startswith("pem") for kind, _ in self.events()))
        event_names = [kind for kind, _ in self.events()]
        ready = event_names.index("lockin_readiness")
        light = event_names.index("optical_condition_qualified")
        settled = event_names.index("lockin_point_qualification")
        self.assertLess(ready, light)
        self.assertLess(light, settled)
        with closing(open_readonly(self.database)) as db:
            samples = [json.loads(row[0]) for row in db.execute(
                "SELECT payload_json FROM combination_samples ORDER BY rowid")]
        for sample in samples:
            optical = next(read for read in sample["reads"] if read["module"] == "optical")
            phases = [bracket["phase"] for bracket in optical["status"]["sample_brackets"]]
            self.assertEqual(phases[0], "before_electrical")
            self.assertEqual(phases[-1], "after_electrical")
            self.assertEqual(phases.count("formal"), 1)
        self.assert_closed_laser_off()

    def test_chopper_prepared_in_dark_held_during_sampling_and_stopped_after_laser(self):
        self.enable_chopper()
        def observe(session):
            exchange = session.transport.exchange
            def logged(command, **kwargs):
                self.optical_log.append(("chopper", command, self.backend.source["emission_state"]))
                return exchange(command, **kwargs)
            session.transport.exchange = logged
        self.modify_chopper = observe
        result = self.execute("chopper")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        writes = [item for item in self.optical_log if item[0] == "chopper" and "=" in item[1]]
        self.assertEqual([item[1] for item in writes], ["enable=1", "enable=0"])
        self.assertTrue(all(item[2] == 0 for item in writes))
        self.assertEqual(len(load_combination_rows(self.database)), 2)
        self.assert_closed_laser_off()

    def test_fresh_chopper_drift_fails_formal_guard_and_preserves_failed_attempt(self):
        self.enable_chopper()
        def drift_on_light(session):
            exchange = session.transport.exchange
            def exchange_with_drift(command, **kwargs):
                if self.backend and self.backend.source["emission_state"] == 3:
                    session.transport.values["nharmonic"] = 2
                return exchange(command, **kwargs)
            session.transport.exchange = exchange_with_drift
        self.modify_chopper = drift_on_light
        result = self.execute("drift")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("Chopper", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        with closing(open_readonly(self.database)) as db:
            rows = db.execute("SELECT status FROM combination_attempts").fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(row[0] == "rejected" for row in rows))
        self.assert_closed_laser_off()

    def test_loader_rejects_xy_sample_connection_and_impossible_chopping(self):
        original = deepcopy(self.document)
        self.document["lockin_xy"]["sine_output_connected"] = True
        self.write_document()
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        self.document = original
        self.enable_chopper()
        self.document["lockin_xx"]["frequency_hz"] = 50000.0
        self.write_document()
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        self.document["lockin_xx"]["frequency_hz"] = 100.0
        self.document["chopper"]["harmonic_n"] = 2
        self.write_document()
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        self.assertIsNone(self.backend)

    def test_unauthorized_call_has_no_factory_or_database_acquisition(self):
        self.enable_chopper()
        with self.assertRaises(ValueError):
            self.execute("unauthorized", authorize_optical=False)
        self.assertEqual(self.manager.opened, [])
        self.assertIsNone(self.backend)
        self.assertIsNone(self.chopper)

    def test_manual_chopper_without_phase_confirmation_fails_before_illumination(self):
        self.enable_chopper("manual")
        self.document["chopper"]["manual_phase_lock_confirmed"] = False
        self.write_document()
        result = self.execute("manual-unconfirmed")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("phase lock", result["error"])
        self.assertFalse(any(item[0:2] == ("emission", True) for item in self.optical_log))
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_closed_laser_off()

    def test_continuous_direct_gates_qualify_optical_once_per_group(self):
        smu = tomllib.loads(OPERATION_TEXT)
        smu.pop("smu_bias")
        smu.pop("gate_bottom")
        smu["three_smu_run"].update(delay_s=0.03, serpentine=False)
        smu["three_smu_run"]["smu_bias"] = {"role": "off", "bidirectional": False}
        smu["three_smu_run"]["gate_bottom"] = {"role": "off", "bidirectional": False}
        smu["three_smu_run"]["gate_top"] = dict(role="sweep", bidirectional=False,
            points=[-0.1, 0.0, 0.1], zero_readback_tolerance_v=0.05)
        self.document.update(smu)
        self.document["nkt_run"]["points"] *= 2
        self.document["power_feedback"]["target_powers_w"] = [5e-6, 22.5e-6]
        self.document["combination_scan"].update(order=["optical", "smu", "lockin"],
            illumination_policy="continuous_gate_scan", samples_per_condition=1)
        self.enable_chopper()
        qualified = []
        qualify = OpticalPointSession.qualify
        def counted(session, *args, **kwargs):
            result = qualify(session, *args, **kwargs)
            qualified.append(session.index)
            return result
        with patch.object(OpticalPointSession, "qualify", counted):
            result = self.execute("continuous")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(qualified, [0, 1])
        self.assertEqual(len(load_combination_rows(self.database)), 6)
        self.assertEqual(self.chopper.transport.commands.count("enable=1"), 1)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        self.assertEqual(set(self.adapters), {"gate_top"})
        self.assertFalse(self.adapters["gate_top"].output)
        self.assertEqual(self.adapters["gate_top"].source, 0)
        self.assert_closed_laser_off()


if __name__ == "__main__":
    unittest.main()
