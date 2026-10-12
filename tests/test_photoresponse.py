from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.nkt_config import NktError, NktPoint
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.photoresponse import PhotoresponseRunner, SimulatedElectrical, differential
from attodry_control.photoresponse_config import load_photoresponse_config
from attodry_control.photoresponse_cli import main, audit_safe
from attodry_control.photoresponse_analysis import load_results


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/photoresponse_simulation.toml"


class Clock:
    def __init__(self):
        self.value = 100000.0

    def __call__(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


def example(**changes):
    return replace(load_photoresponse_config(EXAMPLE), **changes)


def setup(config=None, optical_class=SimulatedNkt, electrical_class=SimulatedElectrical, on_event=None):
    config = config or example()
    clock = Clock()
    optical = optical_class(config.nkt)
    laser = NktController(config.nkt, optical, clock=clock, sleep=clock.sleep)
    electrical = electrical_class(config, clock=clock)
    runner = PhotoresponseRunner(config, laser, electrical, clock=clock, sleep=clock.sleep, on_event=on_event)
    return runner, optical, electrical, clock


class ConfigTests(unittest.TestCase):
    def test_all_modes_and_no_excitation_is_not_zero_current(self):
        c = example()
        self.assertEqual(c.bias_points, (None,))
        self.assertEqual(c.excitation, "none")
        for mode, points, detector in (("dc_current_voltage", (0.0, 1e-6), "keithley2182a"),
                                       ("voltage_bias_current", (0.0, 1e-3), "keithley2400")):
            with self.subTest(mode=mode):
                config = replace(c, mode=mode, bias_points=points, detector=detector)
                config.validate()

    def test_unsafe_config_rejected_before_constructing_controllers(self):
        c = example()
        cases = [replace(c, mode="unknown"), replace(c, samples_per_window=True),
                 replace(c, sample_interval_s=0), replace(c, settle_s=float("nan")),
                 replace(c, max_abs_voltage_v=0), replace(c, bias_points=(0.0,)),
                 replace(c, mode="dc_current_voltage", bias_points=(1.0,)),
                 replace(c, mode="voltage_bias_current", bias_points=(0.0,)),
                 replace(c, nkt=replace(c.nkt, backend="nkt_sdk")),
                 replace(c, nkt=replace(c.nkt, emit=False)),
                 replace(c, nkt=replace(c.nkt, dwell_s=1)),
                 replace(c, repeats=10000)]
        for config in cases:
            with self.subTest(config=config), self.assertRaises(NktError):
                config.validate()

    def test_strict_loader_mode_fields_and_unrelated_unconfigured_modules(self):
        original = EXAMPLE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.toml"
            path.write_text(original + '\n[cryostat]\nunconfigured = true\n', encoding="utf-8")
            load_photoresponse_config(path)
            invalid = [original.replace("samples_per_window =", "sample_per_window ="),
                       original.replace('mode = "open_circuit_voltage"', 'mode = "dc_current_voltage"'),
                       original.replace('[photoresponse]', '[photoresponse]\nbias_current_points_a = [0.0]'),
                       original.replace('backend = "simulation"', 'backend = "nkt_sdk"')]
            for content in invalid:
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(NktError):
                    load_photoresponse_config(path)
            current = original.replace('mode = "open_circuit_voltage"', 'mode = "dc_current_voltage"')
            current = current.replace('[photoresponse]', '[photoresponse]\nbias_current_points_a = [0.0]')
            path.write_text(current, encoding="utf-8")
            self.assertEqual(load_photoresponse_config(path).bias_points, (0.0,))


class NktSessionTests(unittest.TestCase):
    def test_external_setting_change_is_rejected_before_giving_light(self):
        for kind in ("source", "filter", "emission"):
            with self.subTest(kind=kind):
                runner, backend, _, _ = setup()
                runner.laser.begin()
                runner.laser.prepare_point(runner.config.nkt.points[0])
                if kind == "source":
                    backend.source["level_pct"] = runner.config.nkt.max_level_pct
                elif kind == "filter":
                    backend.filter["lower_edge_nm"] += 1
                else:
                    backend.source["emission_state"] = 3
                    backend.source["status_bits"] |= 1
                with self.assertRaises(NktError):
                    runner.laser.set_emission(True)
                self.assertNotIn(("emission", True), backend.commands)
                self.assertTrue(runner.laser.finish()["off_confirmed"])

    def test_unprepared_closed_and_failed_sessions_cannot_emit(self):
        runner, backend, _, _ = setup()
        laser = runner.laser
        with self.assertRaises(NktError):
            laser.prepare_point(laser.config.points[0])
        self.assertEqual(backend.commands, [])
        laser.begin()
        with self.assertRaises(NktError):
            laser.set_emission(True)
        laser.prepare_point(laser.config.points[0])
        backend.source["interlock"] = 0
        with self.assertRaises(NktError):
            laser.verify_point(emitting=False)
        before = len(backend.commands)
        with self.assertRaises(NktError):
            laser.set_emission(True)
        self.assertEqual(len(backend.commands), before)
        self.assertTrue(laser.finish()["off_confirmed"])
        with self.assertRaises(NktError):
            laser.begin()

    def test_hardware_permission_cannot_be_bypassed_by_public_session(self):
        runner, backend, _, _ = setup()
        backend.is_hardware = True
        runner.laser.config = replace(runner.config.nkt, backend="nkt_sdk", port="FAKE",
                                     dll_path="FAKE", source_serial="FAKE", varia_serial="FAKE2")
        for method in (runner.laser.begin, runner.laser.run, lambda: runner.laser.set_emission(True)):
            with self.assertRaises(NktError):
                method()
            self.assertEqual(backend.commands, [])

    def test_point_outside_plan_is_rejected_without_configure(self):
        runner, backend, _, _ = setup()
        runner.laser.begin()
        with self.assertRaises(NktError):
            runner.laser.prepare_point(NktPoint(1, 600, 20, 10))
        self.assertFalse(any(c[0] == "configure" for c in backend.commands))
        runner.laser.finish()


class AcquisitionTests(unittest.TestCase):
    def test_no_excitation_voltage_and_linear_drift(self):
        runner, _, electrical, _ = setup()
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        self.assertEqual(len(record["conditions"]), 2)
        for row in record["conditions"]:
            self.assertTrue(row["accepted"])
            self.assertIsNone(row["requested_current_a"])
            self.assertAlmostEqual(row["result"]["delta_voltage_v"], 5e-6, places=12)
            self.assertEqual([w["phase"] for w in row["windows"]], ["dark_before", "light", "dark_after"])
            self.assertEqual(sum(len(w["samples"]) for w in row["windows"]), 9)
            self.assertTrue(all(s["current_a"] is None for w in row["windows"] for s in w["samples"]))
        self.assertFalse(any(c[0].startswith("source_") for c in electrical.commands))
        self.assertIsNone(record["environment"])
        self.assertIsNone(record["measured_power_w"])
        self.assertEqual(record["laser_audit"]["record_type"], "nkt_point_session")
        self.assertNotIn("outcome", record["laser_audit"])

    def test_bias_scan_zero_is_connected_and_current_mode_has_correct_units(self):
        for mode, detector, points, key, delta in (
                ("dc_current_voltage", "keithley2182a", (0.0, 1e-6), "delta_voltage_v", 5e-6),
                ("voltage_bias_current", "keithley2400", (0.0, 1e-3), "delta_current_a", 2e-9)):
            with self.subTest(mode=mode):
                runner, _, electrical, _ = setup(example(mode=mode, detector=detector, bias_points=points))
                record = runner.run()
                self.assertTrue(record["completed"], record["error"])
                self.assertEqual(len(record["conditions"]), 4)
                sample = record["conditions"][0]["windows"][0]["samples"][0]
                self.assertTrue(sample["electrical_before"]["source"]["output_enabled"])
                self.assertEqual(sample["electrical_before"]["source"]["setpoint"], 0)
                for row in record["conditions"]:
                    self.assertAlmostEqual(row["result"][key], delta, places=12)
                self.assertIn(("source_zero_disable",), electrical.commands)

    def test_interrupted_light_retains_partial_and_cleans_both(self):
        class Interrupted(SimulatedElectrical):
            def read(self, *, illuminated):
                if illuminated:
                    raise KeyboardInterrupt("operator stop")
                return super().read(illuminated=illuminated)
        runner, _, electrical, _ = setup(electrical_class=Interrupted)
        record = runner.run()
        self.assertEqual(record["outcome"], "interrupted")
        row = record["conditions"][0]
        self.assertEqual(len(row["windows"][0]["samples"]), 3)
        self.assertIn("KeyboardInterrupt", row["windows"][1]["samples"][0]["error"])
        self.assertFalse(row["accepted"])
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(electrical.closed)

    def test_optical_change_during_electrical_read_rejects_raw_sample(self):
        runner, backend, electrical, _ = setup()
        read = electrical.read
        def changed(*, illuminated):
            result = read(illuminated=illuminated)
            if illuminated:
                backend.source["level_pct"] = 99
            return result
        electrical.read = changed
        record = runner.run()
        sample = record["conditions"][0]["windows"][1]["samples"][0]
        self.assertIsNotNone(sample["voltage_v"])
        self.assertFalse(sample["valid"])
        self.assertFalse(record["completed"])

    def test_nan_overload_and_limit_retain_rejected_raw(self):
        for value in (float("nan"), float("inf"), 9.9e37, 0.02):
            with self.subTest(value=value):
                class Bad(SimulatedElectrical):
                    def read(self, *, illuminated):
                        result = super().read(illuminated=illuminated)
                        result["voltage_v"] = value
                        return result
                runner, _, _, _ = setup(electrical_class=Bad)
                record = runner.run()
                self.assertFalse(record["completed"])
                self.assertTrue(record["cleanup"]["laser_off_confirmed"])
                self.assertIn("raw_reading", record["conditions"][0]["windows"][0]["samples"][0])
                json.dumps(audit_safe(record), allow_nan=False)

    def test_unknown_preflight_is_never_taken_over(self):
        runner, _, electrical, _ = setup(example(mode="dc_current_voltage", bias_points=(0.0,)))
        electrical.state["source"]["output_enabled"] = True
        electrical.state["source"]["setpoint"] = 1e-6
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertFalse(any(c[0].startswith("source_") for c in electrical.commands))
        self.assertFalse(record["cleanup"]["electrical_safe_confirmed"])

    def test_primary_and_two_cleanup_errors_are_retained(self):
        class Lost(SimulatedElectrical):
            def read(self, *, illuminated):
                raise OSError("primary read")
            def cleanup(self):
                raise OSError("source cleanup")
            def close(self):
                raise OSError("detector close")
        runner, _, _, _ = setup(electrical_class=Lost)
        record = runner.run()
        self.assertIn("primary read", record["error"])
        self.assertEqual(len(record["cleanup"]["errors"]), 2)
        self.assertIsNotNone(record["last_confirmed_electrical_state"])
        self.assertFalse(record["electrical_state_current"])

    def test_cleanup_failure_rejects_previously_complete_points(self):
        class CloseFails(SimulatedElectrical):
            def close(self):
                raise OSError("close failed")
        runner, _, _, _ = setup(electrical_class=CloseFails)
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(all(row["result"] is not None and not row["accepted"] for row in record["conditions"]))

    def test_lost_laser_retains_last_confirmed_on(self):
        runner, backend, electrical, _ = setup()
        read = electrical.read
        def lost():
            raise OSError("laser cable lost")
        def detector(*, illuminated):
            result = read(illuminated=illuminated)
            if illuminated:
                backend.read_source = lost
                backend.set_emission = lambda enabled: lost()
            return result
        electrical.read = detector
        record = runner.run()
        self.assertFalse(record["cleanup"]["laser_off_confirmed"])
        self.assertEqual(record["laser_audit"]["last_confirmed_state"]["source"]["readback"]["emission_state"], 3)
        self.assertFalse(record["laser_audit"]["source_state_current"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])

    def test_audit_failure_still_cleans_and_does_not_hide_primary(self):
        class BadRead(SimulatedElectrical):
            def read(self, *, illuminated):
                raise OSError("detector lost")
        def sink(event):
            if event["phase"] == "raw_sample":
                raise OSError("disk full")
        runner, _, _, _ = setup(electrical_class=BadRead, on_event=sink)
        record = runner.run()
        self.assertIn("detector lost", record["error"])
        self.assertIn("disk full", record["conditions"][0]["windows"][0]["samples"][0]["audit_error"])
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])

    def test_hardware_injection_blocked_before_io(self):
        runner, backend, electrical, clock = setup()
        for obj in (backend, electrical):
            obj.is_hardware = True
            with self.assertRaises(NktError):
                PhotoresponseRunner(runner.config, runner.laser, electrical)
            obj.is_hardware = False
        self.assertEqual(backend.commands, [])
        self.assertEqual(electrical.commands, [])


class AnalysisAndCliTests(unittest.TestCase):
    def test_asymmetric_times_interpolate_dark_and_reject_invalid_time(self):
        windows = [{"phase": phase, "samples": [{"valid": True, "started_s": t, "finished_s": t,
                                                 "voltage_v": 2*t + (5 if phase == "light" else 0)}]}
                   for phase, t in zip(("dark_before", "light", "dark_after"), (1, 2, 5))]
        result = differential(windows, "voltage_v")
        self.assertEqual(result["delta_voltage_v"], 5)
        self.assertEqual(result["interpolation_weight"], 0.25)
        windows[2]["samples"][0]["started_s"] = 1
        with self.assertRaises(NktError):
            differential(windows, "voltage_v")

    def test_accepted_only_loader_and_explicit_audit(self):
        record = setup()[0].run()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "record.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(len(load_results(path)), 2)
            record["cleanup"]["errors"] = ["failed close"]
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(load_results(path), [])
            self.assertTrue(all(not row["accepted"] for row in load_results(path, include_rejected=True)))

    def test_cli_outputs_json_journal_and_no_hardware_import(self):
        with tempfile.TemporaryDirectory() as td:
            config = example()
            config = replace(config, nkt=replace(config.nkt, output_directory=Path(td)))
            with patch("attodry_control.photoresponse_cli.load_photoresponse_config", return_value=config), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["simulate"]), 0)
            files = list(Path(td).glob("*_completed.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(len(load_results(files[0])), 2)
            lines = [json.loads(line) for line in next(Path(td).glob("*.jsonl")).read_text().splitlines()]
            self.assertEqual(sum(e["phase"] == "raw_sample" for e in lines), 18)
            self.assertEqual(lines[-1]["phase"], "run_end")

    def test_cli_rejects_hardware_without_backend_construction(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.toml"
            path.write_text(EXAMPLE.read_text().replace('backend = "simulation"', 'backend = "nkt_sdk"'))
            with patch("attodry_control.photoresponse_cli.SimulatedNkt") as factory, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["simulate", "--config", str(path)]), 2)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
