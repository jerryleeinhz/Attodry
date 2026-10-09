from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from optical_helpers import EXAMPLE, ROOT, devices, scanner
from attodry_control.config import load_config, load_temperature_operation_config
from attodry_control.nkt_config import NktError, NktPoint, load_nkt_config
from attodry_control.optical_config import load_pem_config, load_pm_config, load_scan_config, ScanConfig
from attodry_control.optical_scan import OpticalScan, accepted_points
from attodry_control.pem_test import main as pem_main
from attodry_control.pm100d_test import main as pm_main
from attodry_control.optical_test import main as scan_main


class JointTests(unittest.TestCase):
    def test_three_device_barrier_and_accepted_only_after_cleanup(self):
        s = scanner()
        original = s.nkt.backend.set_emission
        barriers = []
        def emission(enabled):
            if enabled:
                barriers.append((s.pem.target_nm, s.meter.target_nm))
                self.assertEqual(s.pem.output_command, True)
                self.assertTrue(s.pem.verify_ready()["stable"])
                self.assertTrue(all(not row["accepted"] for row in s.record["points"]))
            return original(enabled)
        s.nkt.backend.set_emission = emission
        result = s.run()
        self.assertTrue(result["completed"], result["error"])
        self.assertEqual(barriers, [(158.25, 633), (158.75, 635)])
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
        self.assertEqual(len(accepted_points(result)), 2)
        self.assertTrue(all(sample["accepted"] for row in result["points"] for sample in row["power_samples"]))
        self.assertTrue(s.meter.closed and s.pem.closed)
        self.assertTrue(all(not event["point"]["accepted"] for event in result["events"]
                            if event["phase"] == "point_complete"))

    def test_direct_without_pm_or_pem_has_null_watts_zero_dependency(self):
        s = scanner(use_pem=False, use_pm=False)
        result = s.run()
        self.assertTrue(result["completed"])
        self.assertTrue(all(row["measured_power_w"] is None for row in result["points"]))
        self.assertNotIn("pem_transcript", result)
        self.assertNotIn("pm100d_transcript", result)
        self.assertTrue(all(not row["nd_iterations"] for row in result["points"]))
        s = scanner(use_pm=False)
        self.assertTrue(s.run()["completed"])

    def test_preparation_failure_prevents_any_emission(self):
        for device in ("pem", "meter"):
            s = scanner()
            getattr(s, device).prepare = lambda *a, **kw: (_ for _ in ()).throw(NktError("not ready"))
            result = s.run()
            self.assertFalse(result["completed"])
            self.assertNotIn(("emission", True), s.nkt.backend.commands)
            self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
            self.assertEqual(accepted_points(result), [])

    def test_busy_filter_never_reaches_other_device_preparation(self):
        s = scanner()
        original = s.nkt.backend.configure
        def configure(point):
            original(point)
            s.nkt.backend.filter["moving"] = True
        s.nkt.backend.configure = configure
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertNotIn(("emission", True), s.nkt.backend.commands)
        self.assertFalse(any(cmd.startswith(":MOD:AMP ") for cmd in s.pem.transport.commands))

    def test_post_preparation_drift_blocks_emission(self):
        s = scanner()
        original = s.meter.prepare
        def prepare(wavelength):
            value = original(wavelength)
            s.pem.transport.stable = 0
            return value
        s.meter.prepare = prepare
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertNotIn(("emission", True), s.nkt.backend.commands)

    def test_power_acquisition_drift_and_interrupt_preserve_raw_and_off(self):
        s = scanner()
        def power():
            s.pem.transport.stable = 0
            return 0.001
        s.meter.resource.power_provider = power
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertEqual(len(result["points"][0]["power_samples"]), 1)
        self.assertFalse(result["points"][0]["power_samples"][0]["accepted"])
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
        s = scanner()
        s.meter.resource.power_provider = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        result = s.run()
        self.assertEqual(result["outcome"], "interrupted")
        self.assertTrue(s.meter.closed and s.pem.closed)
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])

    def test_preflight_active_device_not_taken_over_or_switched_off(self):
        s = scanner()
        s.nkt.backend.source.update(emission_state=3, status_bits=1)
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertFalse(result["cleanup"]["nkt"]["off_confirmed"])
        self.assertFalse(any(cmd[0] in ("emission", "configure") for cmd in s.nkt.backend.commands))
        self.assertEqual(s.pem.transport.commands, [])

    def test_cleanup_failure_rejects_every_prior_point_and_retains_primary_error(self):
        s = scanner()
        s.meter.resource.power_provider = lambda: (_ for _ in ()).throw(ValueError("primary"))
        original = s.nkt.backend.set_emission
        def emission(enabled):
            if not enabled and s.nkt.backend.source["emission_state"] == 3:
                raise OSError("cleanup-off")
            original(enabled)
        s.nkt.backend.set_emission = emission
        result = s.run()
        self.assertIn("primary", result["error"])
        self.assertIn("cleanup-off", result["cleanup"]["nkt"]["errors"][0])
        self.assertFalse(result["cleanup"]["nkt"]["off_confirmed"])
        self.assertEqual(result["nkt_last_confirmed_state"]["source"]["readback"]["emission_state"], 3)
        self.assertEqual(accepted_points(result), [])

    def test_pm_close_and_pem_disable_errors_reject_normal_completed_points(self):
        for device in ("pem", "pm"):
            s = scanner()
            if device == "pem":
                original = s.pem.transport.exchange
                s.pem.transport.exchange = lambda cmd: "[PEMOUT](1)\n" if cmd == ":SYS:PEMO 0" else original(cmd)
            else:
                s.meter.resource.close = lambda: (_ for _ in ()).throw(OSError("close failed"))
            result = s.run()
            self.assertFalse(result["completed"])
            self.assertTrue(all(not row["accepted"] for row in result["points"]))

    def test_feedback_converges_effective_nd_and_off_before_each_revision(self):
        s = scanner(mode="power_stabilized", provider=lambda b: 0.002 * 10 ** (-b.filter["nd_pct"] / 100))
        result = s.run()
        self.assertTrue(result["completed"], result["error"])
        for row in result["points"]:
            self.assertTrue(row["feedback_result"]["target_in_tolerance"])
            self.assertTrue(row["feedback_result"]["power_window_stable"])
            self.assertNotEqual(row["requested"]["nd_pct"], row["effective_requested"]["nd_pct"])
            self.assertEqual(row["readback"]["nkt"]["filter"]["nd_pct"], row["effective_requested"]["nd_pct"])
            self.assertTrue(all(not sample["accepted"] for sample in row["power_samples"] if sample["phase"] == "tuning"))
            for change in row["nd_iterations"]:
                self.assertLessEqual(abs(change["from_nd_pct"] - change["to_nd_pct"]), 5)
        commands = s.nkt.backend.commands
        for i, cmd in enumerate(commands):
            if cmd[0] == "configure":
                prior = [c for c in commands[:i] if c[0] == "emission"]
                self.assertEqual(prior[-1], ("emission", False))

    def test_feedback_without_pem_and_failure_never_falls_back_to_direct(self):
        s = scanner(mode="power_stabilized", use_pem=False, provider=lambda b: 0.001)
        self.assertTrue(s.run()["completed"])
        s = scanner(mode="power_stabilized", provider=lambda b: 0.003)
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertIn("unreachable", result["error"])
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
        self.assertEqual(accepted_points(result), [])

    def power_scan(self, targets):
        s = scanner(mode="power_stabilized",
                    provider=lambda b: 0.002 * 10 ** (-b.filter["nd_pct"] / 100))
        nkt = replace(s.nkt_config, points=tuple(s.nkt_config.points[0] for _ in targets))
        cfg = replace(s.config, feedback=replace(s.config.feedback, target_power_w=None,
                      target_tolerance_w=None, target_powers_w=tuple(targets),
                      target_tolerance_fraction=0.10))
        return OpticalScan(nkt, cfg, s.nkt.backend, pem=s.pem, meter=s.meter,
                           clock=s.clock, sleep=s.sleep)

    def test_fixed_wavelength_power_scan_resolves_each_target_and_tolerance(self):
        s = self.power_scan((0.0008, 0.0012))
        result = s.run()
        self.assertTrue(result["completed"], result["error"])
        self.assertEqual([row["center_setpoint_nm"] for row in result["points"]], [633, 633])
        self.assertEqual([row["target_power_w"] for row in result["points"]], [0.0008, 0.0012])
        self.assertNotEqual(result["points"][0]["effective_requested"]["nd_pct"],
                            result["points"][1]["effective_requested"]["nd_pct"])
        for row in result["points"]:
            self.assertAlmostEqual(row["target_tolerance_w"], row["target_power_w"] * 0.1)
            self.assertTrue(row["feedback_result"]["target_in_tolerance"])
            self.assertTrue(all(w["target_power_w"] == row["target_power_w"]
                                for w in row["feedback_windows"]))
            self.assertTrue(row["accepted"])

    def test_relative_tolerance_wavelength_scan_keeps_fixed_power(self):
        s = scanner(mode="power_stabilized", provider=lambda b: 0.001)
        cfg = replace(s.config, feedback=replace(s.config.feedback, target_tolerance_w=None,
                                                target_tolerance_fraction=0.10))
        s = OpticalScan(s.nkt_config, cfg, s.nkt.backend, pem=s.pem, meter=s.meter,
                        clock=s.clock, sleep=s.sleep)
        result = s.run()
        self.assertTrue(result["completed"], result["error"])
        self.assertEqual([row["center_setpoint_nm"] for row in result["points"]], [633, 635])
        self.assertEqual([row["target_power_w"] for row in result["points"]], [0.001, 0.001])
        self.assertEqual([row["pem_preparation"]["requested_amplitude_nm"]
                          for row in result["points"]], [158.25, 158.75])

    def test_second_target_formal_drift_reuses_its_resolved_feedback(self):
        s = self.power_scan((0.0008, 0.0012))
        changed = [False]
        def power():
            row = s.record["points"][-1]
            if len(s.record["points"]) == 2 and any(
                    sample["phase"] == "formal" for sample in row["power_samples"]):
                changed[0] = True
            return 0.002 * (1.3 if changed[0] else 1) * 10 ** (-s.nkt.backend.filter["nd_pct"] / 100)
        s.meter.resource.power_provider = power
        result = s.run()
        self.assertTrue(result["completed"], result["error"])
        row = result["points"][1]
        self.assertTrue(any(sample["phase"] == "rejected_formal_drift" for sample in row["power_samples"]))
        self.assertTrue(all(not sample["accepted"] for sample in row["power_samples"]
                            if sample["phase"] == "rejected_formal_drift"))
        self.assertEqual(row["feedback_result"]["target_power_w"], 0.0012)
        self.assertTrue(row["feedback_result"]["target_in_tolerance"])

    def test_unreachable_later_power_target_rejects_prior_points(self):
        s = self.power_scan((0.0012, 0.0001))
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertIn("unreachable", result["error"])
        self.assertEqual(len(result["points"]), 2)
        self.assertTrue(result["points"][0]["power_samples"])
        self.assertFalse(any(row["accepted"] for row in result["points"]))
        self.assertEqual(accepted_points(result), [])
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])

    def test_sensor_50uw_trip_rejects_before_post_acquisition_checks(self):
        for power in (-1e-9, 50.1e-6):
            s = scanner(mode="power_stabilized", provider=lambda b: power)
            s.meter.config = replace(s.meter.config, max_power_w=50e-6)
            feedback = replace(s.config.feedback, target_power_w=30e-6, target_tolerance_w=None,
                               target_tolerance_fraction=0.10, max_power_w=50e-6)
            s = OpticalScan(s.nkt_config, replace(s.config, feedback=feedback), s.nkt.backend,
                            pem=s.pem, meter=s.meter, clock=s.clock, sleep=s.sleep)
            result = s.run()
            self.assertFalse(result["completed"])
            self.assertTrue(s.meter.poisoned)
            self.assertEqual(result["points"][0]["nd_iterations"], [])
            self.assertEqual(result["points"][0]["power_samples"], [])
            self.assertEqual(s.meter.transcript[-1]["command"], "READ?")
            self.assertEqual(float(s.meter.transcript[-1]["reply"]), power)
            self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
            self.assertTrue(s.pem.closed and s.meter.closed)

    def test_feedback_timeout_unstable_zero_saturated_and_invalid_power(self):
        for provider in (lambda b: 0, lambda b: float("nan"), lambda b: 0.02):
            s = scanner(mode="power_stabilized", provider=provider)
            result = s.run()
            self.assertFalse(result["completed"])
            self.assertEqual(result["points"][0]["nd_iterations"], [])
            self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
        count = [0]
        def noise(b):
            count[0] += 1
            return 0.001 + (1 if count[0] % 2 else -1) * 0.0001
        s = scanner(mode="power_stabilized", provider=noise)
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertIn("timeout", result["error"].lower())
        self.assertLessEqual(s.clock(), 3.1)

    def test_formal_drift_restarts_window_dwell_and_retains_rejected_samples(self):
        calls = [0]
        def drifting(b):
            calls[0] += 1
            # The initial target is stable. Change during formal acquisition.
            return (0.001 if calls[0] < 11 else 0.002 * 10 ** (-b.filter["nd_pct"] / 100))
        s = scanner(mode="power_stabilized", provider=drifting)
        result = s.run()
        self.assertTrue(result["completed"], result["error"])
        self.assertTrue(any(sample["phase"] == "rejected_formal_drift" for row in result["points"]
                            for sample in row["power_samples"]))
        self.assertTrue(all(not sample["accepted"] for row in result["points"] for sample in row["power_samples"]
                            if sample["phase"] == "rejected_formal_drift"))

    def test_point_deadline_covers_slow_preparation_not_reset_each_iteration(self):
        s = scanner(mode="power_stabilized")
        original = s.meter.prepare
        def slow(wavelength):
            s.sleep(4)
            return original(wavelength)
        s.meter.prepare = slow
        result = s.run()
        self.assertFalse(result["completed"])
        self.assertNotIn(("emission", True), s.nkt.backend.commands)

    def test_interrupt_during_nd_revision_uses_one_session_and_final_off(self):
        s = scanner(mode="power_stabilized", provider=lambda b: 0.002)
        original = s.nkt.backend.configure
        calls = [0]
        def configure(point):
            calls[0] += 1
            if calls[0] > 1:
                raise KeyboardInterrupt()
            return original(point)
        s.nkt.backend.configure = configure
        result = s.run()
        self.assertEqual(result["outcome"], "interrupted")
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])
        self.assertEqual(sum(c[0] == "close" for c in s.nkt.backend.commands), 1)


class ConfigAndCliTests(unittest.TestCase):
    def test_point_session_options_reject_before_standalone_resource_factories(self):
        original = (ROOT / "config/optical_source_current_simulation.toml").read_text(encoding="utf-8")
        for table, key, value in (("optical_scan", "target_deviation_policy", "record_continue"),
                                  ("power_feedback", "target_mapping", "cartesian")):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "config.toml"
                path.write_text(original.replace('[' + table + ']', '[' + table + ']\n' +
                    key + ' = "' + value + '"'), encoding="utf-8")
                with patch("attodry_control.optical_cli.SimulatedNkt") as backend, \
                     patch("attodry_control.optical_cli._device") as device, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(scan_main(["simulate", "--config", str(path)]), 2)
                backend.assert_not_called()
                device.assert_not_called()
                self.assertIn("standalone optical scan", errors.getvalue())

    def test_target_mapping_loading_and_legacy_length_contract(self):
        original = (ROOT / "config/optical_source_current_simulation.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            nkt = load_nkt_config(ROOT / "config/optical_source_current_simulation.toml")
            for value in ('"paired"', '"cartesian"', '"auto"', 'true', '2', '["paired"]'):
                with self.subTest(value=value):
                    path.write_text(original.replace('[power_feedback]',
                        '[power_feedback]\ntarget_mapping = ' + value), encoding="utf-8")
                    if value in ('"paired"', '"cartesian"'):
                        config, _, _ = load_scan_config(path, nkt)
                        self.assertEqual(config.feedback.target_mapping, json.loads(value))
                    else:
                        with self.assertRaises(NktError):
                            load_scan_config(path, nkt)
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(NktError, "length must match"):
                load_scan_config(path, replace(nkt, points=nkt.points[:1]))
            config, _, _ = load_scan_config(path, nkt)
            self.assertEqual(config.feedback.target_mapping, "paired")
        s = scanner(mode="power_stabilized")
        s.config = replace(s.config, feedback=replace(s.config.feedback, target_mapping="cartesian"))
        with self.assertRaisesRegex(NktError, "standalone optical scan"):
            s.run()
        self.assertEqual(s.nkt.backend.commands, [])

    def test_target_deviation_policy_is_explicit_validated_and_point_session_only(self):
        original = (ROOT / "config/optical_source_current_simulation.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text(original, encoding="utf-8")
            nkt = load_nkt_config(path)
            config, pem, pm = load_scan_config(path, nkt)
            self.assertEqual(config.target_deviation_policy, "abort")
            for value in ('"abort"', '"record_continue"', '"ignore"', 'true', '1', '["abort"]'):
                with self.subTest(value=value):
                    path.write_text(original.replace('[optical_scan]',
                        '[optical_scan]\ntarget_deviation_policy = ' + value), encoding="utf-8")
                    if value in ('"abort"', '"record_continue"'):
                        loaded, _, _ = load_scan_config(path, nkt)
                        self.assertEqual(loaded.target_deviation_policy, json.loads(value))
                    else:
                        with self.assertRaises(NktError):
                            load_scan_config(path, nkt)
        s = scanner(mode="power_stabilized")
        s.config = replace(s.config, target_deviation_policy="record_continue")
        with self.assertRaisesRegex(NktError, "standalone optical scan"):
            s.run()
        self.assertEqual(s.nkt.backend.commands, [])

    def test_unverified_feedback_cli_rejects_before_sdk_construction_and_still_simulates(self):
        original = EXAMPLE.read_text().replace('backend = "simulation"', 'backend = "nkt_sdk"', 1)
        original = original.replace('mode = "direct"', 'mode = "power_stabilized"')
        original = original.replace('[nkt_source]', '[nkt_source]\naddress = 1')
        original = original.replace('[nkt_varia]', '[nkt_varia]\naddress = 2')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text(original)
            with patch("attodry_control.nkt_sdk.NktSdk") as sdk, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                status = scan_main(["run", "--config", str(path), "--authorize-writes", "--confirm-manual-route"])
            self.assertEqual(status, 2)
            self.assertIn("ND optical control is unverified", errors.getvalue())
            sdk.assert_not_called()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(scan_main(["simulate", "--config", str(path)]), 0)

    def test_hardware_feedback_requires_verified_optical_nd_control(self):
        s = scanner(mode="power_stabilized")
        nkt = replace(s.nkt_config, backend="nkt_sdk")
        with self.assertRaisesRegex(NktError, "ND optical control is unverified"):
            s.config.validate(nkt, s.pem.config, s.meter.config)
        s.config.validate(replace(nkt, varia_nd_control_verified=True), s.pem.config, s.meter.config)
        # Simulation remains available without claiming real optical capability.
        s.config.validate(s.nkt_config, s.pem.config, s.meter.config)

    def test_direct_without_nd_keeps_existing_setting(self):
        s = scanner()
        nkt = replace(s.nkt_config, points=tuple(replace(p, nd_pct=None) for p in s.nkt_config.points))
        s.nkt.backend.filter["nd_pct"] = 25.0
        result = OpticalScan(nkt, s.config, s.nkt.backend, pem=s.pem, meter=s.meter,
                             clock=s.clock, sleep=s.sleep).run()
        self.assertTrue(result["completed"], result["error"])
        self.assertTrue(all(row["requested"]["nd_pct"] is None for row in result["points"]))
        self.assertTrue(all(row["readback"]["nkt"]["filter"]["nd_pct"] == 25.0
                            for row in result["points"]))
        self.assertTrue(all(not row["nd_iterations"] for row in result["points"]))

    def test_strict_optical_keys_and_independent_unused_tables(self):
        original = EXAMPLE.read_text()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            for content, loader in ((original.replace('frequency_min_hz =', 'frequency_min ='), load_pem_config),
                                    (original.replace('average_count =', 'averages ='), load_pm_config)):
                path.write_text(content)
                with self.assertRaises(NktError):
                    loader(path)
            path.write_text(original + '\n[cryostat]\nunconfigured = true\n')
            load_pem_config(path)
            load_pm_config(path)
            load_nkt_config(path)
            path.write_text(original.replace('[pem]', '[pme]'))
            with self.assertRaises(NktError):
                load_pm_config(path)

    def test_endpoint_broadband_missing_feedback_and_disabled_unrelated_config(self):
        clock, nkt, scan, backend, pem, meter = devices()
        for point in (NktPoint(8, 400, 10, 20), NktPoint(8, 840, 10, 20)):
            with self.assertRaises(NktError):
                scan.validate(replace(nkt, points=(point,)), pem.config, meter.config)
        broadband = replace(nkt, mode="broadband", points=(NktPoint(8),))
        with self.assertRaises(NktError):
            scan.validate(broadband, pem.config, meter.config)
        for invalid in (replace(scan, mode="power_stabilized"), replace(scan, use_power_meter=False)):
            with self.assertRaises(NktError):
                invalid.validate(nkt, pem.config, meter.config)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            original = EXAMPLE.read_text().replace('use_pem = true', 'use_pem = false').replace(
                'use_power_meter = true', 'use_power_meter = false').replace('peak_retardance_waves = 0.25\n\n#', '\n#')
            # Own disabled fields may be placeholders; they are not parsed.
            original = original.replace('average_count = 1', 'average_count = "CHANGE_ME"')
            original = original.replace('frequency_min_hz = 49000.0', 'frequency_min_hz = "CHANGE_ME"')
            path.write_text(original)
            cfg, pc, mc = load_scan_config(path, load_nkt_config(path))
            self.assertIsNone(pc)
            self.assertIsNone(mc)

    def test_shared_project_and_temperature_loaders_ignore_optical_placeholders(self):
        extra = ''.join(f'\n[{table}]\nunconfigured = "CHANGE_ME"\n' for table in
                        ("pem", "pem_run", "pm100d", "pm100d_run", "optical_scan", "power_feedback"))
        with tempfile.TemporaryDirectory() as td:
            for name, loader in (("simulation.toml", load_config),
                                 ("hardware.example.toml", load_temperature_operation_config)):
                source = ROOT / "config" / name
                path = Path(td) / name
                content = source.read_text(encoding="utf-8")
                path.write_text(content if "[pem]" in content else content + extra, encoding="utf-8")
                # Existing loaders locate independent lockin_safety alongside config.
                (Path(td) / "lockin_safety.toml").write_text(
                    (ROOT / "config/lockin_safety.toml").read_text(encoding="utf-8"), encoding="utf-8")
                loader(path)

    def test_help_describe_validate_and_unauthorized_run_zero_factories(self):
        with patch('attodry_control.optical_cli.SerialTransport') as serial, patch(
             'attodry_control.optical_cli.Pm100d.open') as visa, patch('attodry_control.nkt_sdk.NktSdk') as sdk:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                for main in (pem_main, pm_main, scan_main):
                    self.assertEqual(main(["describe"]), 0)
                    self.assertEqual(main(["validate-config", "--config", str(EXAMPLE)]), 0)
                    self.assertEqual(main(["run", "--config", str(EXAMPLE)]), 2)
                    with self.assertRaises(SystemExit) as exc:
                        main(["--help"])
                    self.assertEqual(exc.exception.code, 0)
            serial.assert_not_called()
            visa.assert_not_called()
            sdk.assert_not_called()

    def test_all_simulate_clis_emit_audit_and_do_not_open_hardware(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            original = EXAMPLE.read_text().replace('../run_data/optical_simulation', 'data').replace(
                '../run_data/pem_simulation', 'data').replace('../run_data/pm100d_simulation', 'data')
            path.write_text(original)
            with patch('attodry_control.optical_cli.SerialTransport') as serial, patch(
                 'attodry_control.optical_cli.Pm100d.open') as visa, redirect_stdout(io.StringIO()):
                for main in (pem_main, pm_main, scan_main):
                    self.assertEqual(main(["simulate", "--config", str(path)]), 0)
                path.write_text(original.replace('mode = "direct"', 'mode = "power_stabilized"'))
                self.assertEqual(scan_main(["simulate", "--config", str(path)]), 0)
                serial.assert_not_called()
                visa.assert_not_called()
            records = list((Path(td) / 'data').glob('*.json'))
            self.assertEqual(len(records), 4)
            for record_path in records:
                record = json.loads(record_path.read_text())
                self.assertTrue(record["completed"])
                self.assertTrue(record["simulated"])
                lines = record_path.with_suffix('.jsonl').read_text().splitlines()
                self.assertTrue(lines)
                for line in lines:
                    json.loads(line)
