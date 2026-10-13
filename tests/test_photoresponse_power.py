from dataclasses import replace
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.photoresponse import PhotoresponseRunner, SimulatedElectrical
from attodry_control.photoresponse_cld import CldController, SimulatedCld
from attodry_control.photoresponse_config import load_photoresponse_config
from attodry_control.photoresponse_environment import open_environment
from attodry_control.photoresponse_power import open_photoresponse_power
from attodry_control.photoresponse_combination import PhotoresponseCombinationPlan
from attodry_control.photoresponse_analysis import load_results, export_results
from attodry_control.photoresponse_cli import _run
from attodry_control.power_feedback import PowerWindow, next_cld_current_a, next_nd_pct, next_source_current_pct
from attodry_control.photoresponse_storage import PhotoStore
from test_photoresponse import Clock, example, setup as open_loop_setup
from test_photoresponse_integration import hardware_setup, environment_config


ROOT = Path(__file__).resolve().parents[1]


def config_for(kind="nkt"):
    return load_photoresponse_config(ROOT / f"config/photoresponse_power_{kind}_simulation.toml")


def setup(kind="nkt", config=None):
    config = config or config_for(kind)
    clock = Clock()
    cls, controller = (SimulatedNkt, NktController) if kind == "nkt" else (SimulatedCld, CldController)
    backend = cls(config.nkt)
    laser = controller(config.nkt, backend, clock=clock, sleep=clock.sleep)
    electrical = SimulatedElectrical(config, clock=clock)
    power = open_photoresponse_power(config.power, laser, clock=clock, sleep=clock.sleep)
    environment = open_environment(config.environment) if config.environment else None
    runner = PhotoresponseRunner(config, laser, electrical, power=power, environment=environment, clock=clock, sleep=clock.sleep)
    return runner, backend, electrical, power, clock


class CombinationConfigTests(unittest.TestCase):
    def test_examples_cover_nkt_wave_power_bias_and_fixed_cld_power(self):
        for kind, expected in (("nkt", 8), ("cld", 6)):
            c = config_for(kind)
            plan = PhotoresponseCombinationPlan(c)
            self.assertEqual(len(list(plan.conditions())), expected)
            self.assertIn("pm100d", plan.modules)
        self.assertEqual({p.wavelength_nm for p in config_for("cld").nkt.points}, {760.})

    def test_order_duplicate_values_and_all_multivalue_axes(self):
        c = config_for()
        c = replace(c, bias_points=(0., 0.), combination_order=("bias", "target_power_w", "optical"))
        c.validate()
        leaves = list(PhotoresponseCombinationPlan(c).conditions())
        self.assertEqual([leaf["bias"]["index"] for leaf in leaves], [0]*4 + [1]*4)
        for order in (("optical",), ("bias", "bias"), ("unknown",)):
            with self.assertRaises(NktError):
                replace(c, combination_order=order).validate()

    def test_environment_wraparound_is_preflighted(self):
        c = config_for()
        env = environment_config()
        points = tuple({**env.points[0], "temperature_k": t} for t in (2., 3., 4.))
        c = replace(c, environment=replace(env, points=points, max_temperature_step_k=1),
                    combination_order=("optical", "environment", "target_power_w", "bias"))
        with self.assertRaisesRegex(NktError, "wrap-around"):
            c.validate()
        replace(c, combination_order=("environment", "optical", "target_power_w", "bias")).validate()

    def test_optional_current_limit_only_for_no_excitation_2182(self):
        c = replace(example(), max_abs_current_a=None)
        c.validate()
        self.assertTrue(open_loop_setup(c)[0].run()["completed"])
        for mode, detector, bias in (("dc_current_voltage", "keithley2182a", (0.,)),
                                    ("voltage_bias_current", "keithley2400", (0.,)),
                                    ("open_circuit_voltage", "keithley2400", (None,))):
            with self.assertRaises(NktError):
                replace(c, mode=mode, detector=detector, bias_points=bias).validate()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            content = (ROOT / "config/photoresponse_simulation.toml").read_text()
            path.write_text(content.replace("max_abs_current_a = 0.00001", ""))
            self.assertIsNone(load_photoresponse_config(path).max_abs_current_a)

    def test_feedback_bounds_modes_and_meter_are_validated_before_io(self):
        c = config_for()
        invalid = [replace(c.power, feedback=replace(c.power.feedback, source_current_max_pct=11)),
                   replace(c.power, feedback=replace(c.power.feedback, source_current_min_pct=6)),
                   replace(c.power, meter=replace(c.power.meter, max_power_w=.001)),
                   replace(c.power, meter=replace(c.power.meter, backend="visa"))]
        for power in invalid:
            with self.assertRaises(NktError):
                replace(c, power=power).validate()
        with self.assertRaises(NktError):
            replace(c, nkt=replace(c.nkt, source_control="power")).validate()
        cld = config_for("cld")
        with self.assertRaises(NktError):
            replace(cld, power=replace(cld.power, feedback=replace(cld.power.feedback, current_max_a=.031))).validate()
        for kind in ("nkt", "cld"):
            with self.assertRaises(NktError):
                load_photoresponse_config(ROOT / f"config/photoresponse_power_{kind}_hardware.example.toml")

    def test_public_factory_invalid_policy_or_missing_permission_never_opens(self):
        c = config_for()
        optical = replace(c.nkt, backend="nkt_sdk")
        laser = SimpleNamespace(config=optical, backend=SimpleNamespace(is_hardware=True))
        power = replace(c.power, meter=replace(c.power.meter, backend="visa"))
        invalid = replace(power, feedback=replace(power.feedback, source_current_max_pct=11))
        with patch("attodry_control.pm100d.Pm100d.open") as opened:
            with self.assertRaises(NktError):
                open_photoresponse_power(invalid, laser, authorize_connection=True, authorize_writes=True)
            with self.assertRaises(NktError):
                open_photoresponse_power(power, laser)
            laser.backend.is_hardware = False
            with self.assertRaises(NktError):
                open_photoresponse_power(power, laser, authorize_connection=True, authorize_writes=True)
            opened.assert_not_called()

    def test_plan_hash_includes_power_and_order(self):
        c = config_for()
        with tempfile.TemporaryDirectory() as td:
            store = PhotoStore(Path(td) / "audit.sqlite")
            try:
                run_id, _ = store.start(c)
                for other in (replace(c, combination_order=("bias", "optical", "target_power_w")),
                              replace(c, power=replace(c.power, feedback=replace(c.power.feedback, target_powers_w=(.0007, .0011))))):
                    with self.assertRaises(NktError):
                        store.start(other, resume=run_id)
            finally:
                store.close()


class PowerAcquisitionTests(unittest.TestCase):
    def test_environment_inner_axis_repeated_leaves_have_fresh_all_module_records(self):
        c = config_for()
        c = replace(c, nkt=replace(c.nkt, points=(c.nkt.points[0],)), environment=environment_config(),
                    combination_order=("bias", "environment", "target_power_w"), samples_per_window=1)
        runner, backend, electrical, _, _ = setup(config=c)
        prepare = runner.environment.prepare
        def move(point):
            self.assertFalse(electrical.state["source"]["output_enabled"])
            self.assertEqual(backend.source["emission_state"], 0)
            return prepare(point)
        runner.environment.prepare = move
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        self.assertEqual(len(record["conditions"]), 8)
        for row in record["conditions"]:
            sample = row["windows"][1]["samples"][0]
            self.assertEqual(sample["environment_after"]["measurement_temperature_k"], row["requested_environment"]["temperature_k"])
            self.assertEqual({r["module"] for r in sample["module_readings"]}, set(runner.plan.modules))

    def test_nkt_and_cld_complete_with_brackets_and_module_records(self):
        for kind in ("nkt", "cld"):
            runner, backend, electrical, power, _ = setup(kind)
            record = runner.run()
            self.assertTrue(record["completed"], (record["error"], record["cleanup"]))
            self.assertTrue(power.meter.closed)
            for row in record["conditions"]:
                self.assertTrue(row["power"]["qualified"])
                self.assertGreater(row["power"]["formal_mean_power_w"], 0)
                for window in row["windows"]:
                    for sample in window["samples"]:
                        self.assertEqual({r["module"] for r in sample["module_readings"]}, set(runner.plan.modules))
                        self.assertEqual(sample["axis_indices"], row["axis_indices"])
                        if window["phase"] == "light":
                            self.assertEqual(sample["power_before"]["after"]["wavelength_nm"], row["requested_optical"]["wavelength_nm"])
                            self.assertLess(sample["power_before"]["sequence"], sample["power_after"]["sequence"])
                            if kind == "cld":
                                optical = sample["module_readings"][0]
                                self.assertGreater(optical["measurements"]["ld_current_a"], 0)
                        else:
                            self.assertIsNone(sample["power_before"]["power_w"])
                if kind == "nkt":
                    source = next(r for r in row["actual"] if r["module"] == "keithley6221_source")
                    self.assertIsNone(source["measurements"]["current_a"])

    def test_nd_feedback_reuses_bounded_direction_policy(self):
        c = config_for()
        f = replace(c.power.feedback, actuator="varia_nd", source_current_min_pct=None,
                    source_current_max_pct=None, source_current_step_pct=None, minimum_signal_power_w=None,
                    target_powers_w=(.00063,), target_tolerance_w=.00001,
                    nd_min_pct=0., nd_max_pct=80., nd_step_pct=10., increasing_nd_increases_power=False)
        runner, _, _, _, _ = setup(config=replace(c, power=replace(c.power, feedback=f)))
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        self.assertTrue(all(a["actuator"] == "varia_nd" for r in record["conditions"] for a in r["power"]["adjustments"]))

    def test_adjustment_cannot_write_while_lit_or_outside_approved_step(self):
        for kind in ("nkt", "cld"):
            runner, backend, _, power, _ = setup(kind)
            laser = runner.laser
            try:
                laser.begin()
                laser.prepare_point(runner.config.nkt.points[0])
                excessive = 8. if kind == "nkt" else .027
                with self.assertRaisesRegex(NktError, "single step"):
                    laser.adjust_power(excessive, power.config)
                laser.set_emission(True)
                before = backend.source["level_pct"] if kind == "nkt" else backend.state["getLdCurrSetpoint"]
                with self.assertRaises(NktError):
                    laser.adjust_power(before, power.config)
                actual = backend.source["level_pct"] if kind == "nkt" else backend.state["getLdCurrSetpoint"]
                self.assertEqual(actual, before)
            finally:
                laser.finish()
                power.close()

    def test_negative_dark_noise_is_not_acquired_or_used_to_search(self):
        runner, backend, _, power, _ = setup()
        original = power.meter.resource.power_provider
        calls = []
        def provider():
            on = backend.source["emission_state"] == 3
            calls.append(on)
            return original() if on else -1e-9
        power.meter.resource.power_provider = provider
        self.assertTrue(runner.run()["completed"])
        self.assertTrue(calls and all(calls))

    def test_formal_target_and_within_target_peak_to_peak_drift_reject_without_retuning(self):
        for drift, p2p in ((.0001, .000001), (.0000007, .0000005)):
            c = config_for()
            f = replace(c.power.feedback, window=replace(c.power.feedback.window, max_peak_to_peak_w=p2p))
            runner, _, _, power, _ = setup(config=replace(c, power=replace(c.power, feedback=f)))
            original = power.meter.resource.power_provider
            adjustments_at_drift = []
            def provider():
                if power.row and power.row["qualified"]:
                    adjustments_at_drift.append(len(power.row["adjustments"]))
                    return original() + drift
                return original()
            power.meter.resource.power_provider = provider
            record = runner.run()
            self.assertFalse(record["completed"])
            self.assertIn("Formal power drift", record["error"])
            row = record["conditions"][0]
            self.assertFalse(row["power"]["samples"][-1]["window_valid"])
            self.assertEqual(len(row["power"]["adjustments"]), adjustments_at_drift[0])
            self.assertTrue(record["cleanup"]["laser_off_confirmed"])
            self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])

    def test_pm_before_fault_stops_electrical_read_immediately(self):
        runner, _, electrical, power, _ = setup()
        original = power.meter.resource.power_provider
        def provider():
            if power.row and power.row["qualified"]:
                electrical.state["compliance_tripped"] = True
            return original()
        power.meter.resource.power_provider = provider
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertEqual([cmd for cmd in electrical.commands if cmd[0] == "read"], [("read", False)] * runner.config.samples_per_window)

    def test_invalid_pm_reply_hard_limit_and_after_status_are_retained(self):
        for kind in ("nan", "hard_limit", "after_status"):
            runner, _, _, power, _ = setup()
            resource = power.meter.resource
            original = resource.query
            def query(command):
                if command == "READ?":
                    if kind == "nan": return "nan"
                    if kind == "hard_limit": return "0.0027"  # Below PM max; above feedback max.
                    resource.values["STAT:QUES:COND?"] = "1"
                return original(command)
            resource.query = query
            record = runner.run()
            self.assertFalse(record["completed"])
            entries = record["conditions"][0]["power"]["samples"]
            failed = next(e for e in entries if not e["valid"])
            self.assertIn("error", failed)
            self.assertTrue(any(e["command"] == "READ?" and e["reply"] is not None for e in failed["pm_transcript"]))
            self.assertTrue(record["cleanup"]["laser_off_confirmed"])

    def test_timeout_including_final_read_and_no_signal_never_increases_output(self):
        for slow in (False, True):
            runner, _, _, power, clock = setup()
            def provider():
                if slow:
                    clock.sleep(power.feedback.timeout_s + 1)
                    return .0008
                return 0.
            power.meter.resource.power_provider = provider
            record = runner.run()
            self.assertFalse(record["completed"])
            self.assertFalse(record["conditions"][0]["power"]["adjustments"])
            self.assertIn("timed out" if slow else "minimum_signal", record["error"])

    def test_cld_direction_reversal_never_expands_a_small_approved_step(self):
        c = config_for("cld")
        f = replace(c.power.feedback, current_step_a=.000015, target_powers_w=(.0008,),
                    timeout_s=.2, hold_s=0)
        runner, backend, _, power, _ = setup("cld", replace(c, power=replace(c.power, feedback=f)))
        power.meter.resource.power_provider = lambda: .0007 if backend.state["getLdCurrSetpoint"] < .01001 else .0009
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("timed out", record["error"])
        adjustments = record["conditions"][0]["power"]["adjustments"]
        self.assertGreater(len(adjustments), 1)
        self.assertTrue(all(abs(a["requested"] - a["before"]) <= f.current_step_a + 1e-12 for a in adjustments))

    def test_electrical_settle_follows_last_feedback_emission(self):
        runner, backend, electrical, power, clock = setup()
        emission_times, read_times = [], []
        original_set, original_read = backend.set_emission, electrical.read
        def set_emission(enabled):
            original_set(enabled)
            if enabled:
                emission_times.append(clock())
        def read(*, illuminated):
            if illuminated:
                read_times.append((clock(), emission_times[-1]))
            return original_read(illuminated=illuminated)
        backend.set_emission, electrical.read = set_emission, read
        self.assertTrue(runner.run()["completed"])
        self.assertTrue(read_times)
        self.assertTrue(all(now - last >= runner.config.settle_s for now, last in read_times))

    def test_unreachable_target_stops_at_approved_bound(self):
        c = config_for()
        c = replace(c, power=replace(c.power, feedback=replace(c.power.feedback, source_current_max_pct=5., target_powers_w=(.0012,))))
        runner, backend, _, _, _ = setup(config=c)
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("boundary", record["error"])
        self.assertLessEqual(backend.source["level_pct"], 5.)

    def test_cleanup_and_interrupt_always_close_pm_and_electrical(self):
        for interrupt in (False, True):
            runner, backend, electrical, power, _ = setup()
            if interrupt:
                power.meter.resource.power_provider = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
            else:
                power.meter.resource.close = lambda: (_ for _ in ()).throw(OSError("close failed"))
            record = runner.run()
            self.assertFalse(record["completed"])
            self.assertTrue(electrical.closed)
            self.assertEqual(backend.source["emission_state"], 0)
            self.assertTrue(power.meter.closed)
            if not interrupt:
                self.assertTrue(any("PM100D close" in e for e in record["cleanup"]["errors"]))

    def test_no_excitation_relay_host_is_query_only_snapshot(self):
        runner, host, _, electrical, _ = hardware_setup("open_circuit_voltage", "keithley2182a", True)
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        self.assertIsNone(electrical.source)
        sample = record["conditions"][0]["windows"][1]["samples"][0]
        host_row = next(r for r in sample["module_readings"] if r["module"] == "keithley6221_relay_host")
        self.assertFalse(host_row["actual"]["output_enabled"])
        self.assertIsNone(host_row["actual"]["measured_current_a"])
        self.assertNotIn("OUTP:STAT 1", host.commands)

    def test_csv_exposes_requested_measured_plane_order_and_indices(self):
        record = setup()[0].run()
        with tempfile.TemporaryDirectory() as td:
            path, output = Path(td)/"record.json", Path(td)/"data.csv"
            path.write_text(json.dumps(record))
            rows = load_results(path)
            self.assertEqual(rows[0]["target_power_w"], .0008)
            self.assertGreater(rows[0]["measured_power_w"], 0)
            self.assertNotIn("responsivity", rows[0])
            export_results(path, output)
            with output.open(encoding="utf-8-sig") as stream:
                row = next(csv.DictReader(stream))
            for key in ("target_power_w", "measured_power_w", "measurement_plane", "combination_order", "axis_index_target_power_w"):
                self.assertIn(key, row)

    def test_cli_factory_failure_closes_already_created_optical_resource(self):
        c = config_for()
        backend = SimulatedNkt(c.nkt)
        with patch("attodry_control.photoresponse_cli.SimulatedNkt", return_value=backend), patch(
                "attodry_control.photoresponse_power.open_photoresponse_power", side_effect=OSError("factory failure")):
            record = _run(c, SimpleNamespace(authorize_environment=False, confirm_route=False), lambda _: None, 1)
        self.assertFalse(record["completed"])
        self.assertIn(("close",), backend.commands)


class PowerPolicyTests(unittest.TestCase):
    def test_freshness_window_duplicate_and_gap(self):
        c = config_for().power.feedback.window
        window = PowerWindow(c, epoch_s=0, max_power_w=.003)
        def sample(seq, at):
            return {"sequence":seq, "valid":True, "unit":"W", "power_w":.001,
                    "started_monotonic_s":at, "finished_monotonic_s":at}
        for i, at in enumerate((0., .01, .02)):
            result = window.add(sample(i, at))
        self.assertTrue(result["power_window_stable"])
        with self.assertRaises(NktError): window.add(sample(2, .02))
        self.assertFalse(window.add(sample(3, .1))["power_window_stable"])

    def test_cld_policy_preserves_bounds_and_source_policies_reject_wrong_actuator(self):
        f = config_for("cld").power.for_target(0)
        self.assertAlmostEqual(next_cld_current_a(.01, .0006, f), .012)
        self.assertAlmostEqual(next_cld_current_a(.01, .001, f), .008)
        for current, power in ((f.current_max_a, .0006), (f.current_min_a, .001), (.01, 0)):
            with self.assertRaises(NktError): next_cld_current_a(current, power, f)
        with self.assertRaises(NktError): next_nd_pct(1, .001, f)
        with self.assertRaises(NktError): next_source_current_pct(1, .001, f)


if __name__ == "__main__":
    unittest.main()
