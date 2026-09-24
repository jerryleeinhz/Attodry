"""Real point/driver code with fake DLL and fake VISA only."""
from contextlib import redirect_stdout
from itertools import permutations
import io
import json
import re
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_store import CombinationStore
from attodry_control.cryostat_points import CryostatPointSession
from tests.test_attodry import FakeAttoDryDll, StepClock
from tests import test_combination_hardware as fixtures


class CryostatCombinationTests(unittest.TestCase):
    write_config = fixtures.ElectricalCombinationTests.write_config
    factory = fixtures.ElectricalCombinationTests.factory
    events = fixtures.ElectricalCombinationTests.events

    def setUp(self):
        fixtures.ElectricalCombinationTests.setUp(self)
        self.base = re.sub(r"(?m)^temperature_ranges = \[.*?^\]",
            'start_k = 2.0\nstop_k = 2.0\nstep_k = 0.1', self.base, flags=re.DOTALL)
        self.base = self.base.replace('stable_dwell_s = 30.0', 'stable_dwell_s = 3.0')
        self.base = self.base.replace('stable_dwell_s = 10.0', 'stable_dwell_s = 3.0')
        self.base = self.base.replace('wait_timeout_s = 1800.0', 'wait_timeout_s = 20.0')
        self.base = self.base.replace('wait_timeout_s = 7200.0', 'wait_timeout_s = 20.0')
        self.dll = FakeAttoDryDll()
        self.dll.temperature_follows_setpoint = True

    def execute(self, run_id="test", **kwargs):
        config = load_hardware_combination(self.path)
        with CombinationStore(self.database) as store:
            return run_hardware_combination(config, store, run_id,
                authorize_hardware=True, authorize_cryostat=True,
                confirm_xy_sine_disconnected=True, dll=self.dll, monotonic=StepClock(),
                smu_adapter_factory=self.factory, manager_factory=lambda: self.manager,
                sleep=lambda _: None, **kwargs)

    def test_all_64_nonempty_orders_share_one_connection(self):
        modules = ("temperature", "magnetic", "smu", "lockin")
        orders = [order for count in range(1, 5) for order in permutations(modules, count)]
        self.assertEqual(len(orders), 64)
        for order in orders:
            with self.subTest(order=order):
                self.dll = FakeAttoDryDll()
                self.dll.temperature_follows_setpoint = True
                self.write_config(order)
                result = self.execute("-".join(order))
                self.assertEqual(result["status"], "completed", result)
                environmental = bool(set(order) & {"temperature", "magnetic"})
                self.assertEqual(self.dll.events.count("connect"), int(environmental))
                self.assertEqual(self.dll.events.count("disconnect"), int(environmental))
                self.assertEqual(self.dll.events.count("end"), int(environmental))
                self.assertEqual("set_temperature" in self.dll.events, "temperature" in order)
                self.assertEqual("toggle_field_control" in self.dll.events, "magnetic" in order)
                rows = load_combination_rows(self.database, run_id="-".join(order))
                self.assertEqual(len(rows), (3 if "smu" in order else 1) * (2 if "lockin" in order else 1))

    def test_cryostat_needs_additional_authorization_before_any_io(self):
        self.write_config(("temperature", "smu"))
        config = load_hardware_combination(self.path)
        with CombinationStore(self.database) as store, self.assertRaisesRegex(ValueError, "cryostat"):
            run_hardware_combination(config, store, "no", authorize_hardware=True,
                dll=self.dll, smu_adapter_factory=self.factory)
        self.assertEqual(self.dll.events, [])
        self.assertEqual(self.adapters, {})
        other = self.directory / "must-not-create.sqlite"
        with self.assertRaisesRegex(ValueError, "authorize-cryostat"):
            cli(["run", "--config", str(self.path), "--database", str(other),
                 "--run-id", "no", "--authorize-combination"])
        self.assertFalse(other.exists())

    def test_describe_four_axes_is_offline(self):
        self.write_config(("temperature", "smu", "magnetic", "lockin"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["describe-hardware", "--config", str(self.path)]), 0)
        plan = json.loads(output.getvalue())["plan"]
        self.assertEqual(plan["field_limit_policy"], "universal-3T")
        self.assertEqual(plan["hardware"]["effective_cryostat"]["magnet"]["limits"]["hardware_z_max_t"], 3)
        self.assertEqual(self.dll.events, [])

    def test_ordered_magnetic_duplicates_reversal_and_temperature_requalification(self):
        source = self.base.replace('{ bx_t = 0.0, bz_t = 0.0 },',
            '{ bx_t = 0.01, bz_t = 0.0 },\n{ bx_t = 0.01, bz_t = 0.0 },\n{ bx_t = -0.01, bz_t = 0.0 },')
        self.write_config(("temperature", "magnetic", "lockin"), source=source)
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual([r["requested.field_x_t"] for r in rows], [.01, .01, .01, .01, -.01, -.01])
        events = self.events()
        self.assertEqual(sum(k == "temperature_qualification_completed" for k, _ in events), 6)
        points = [v for k, v in events if k == "magnetic_point_event" and v.get("event") == "point_completed"]
        self.assertEqual(len(points), 3)
        for row in rows:
            window = row["status.temperature"]
            self.assertEqual(window["sampling"], "synchronous_brackets_not_continuous")
            stages = [r["stage"] for r in window["samples"]]
            self.assertEqual(stages[0], "before_sample")
            self.assertEqual(stages[-1], "after_sample")
            self.assertIn("before", stages)
            self.assertIn("after", stages)
            self.assertEqual(row["actual.temperature_k"], window["temperature_mean_k"])

    def test_temperature_readback_not_setpoint_and_inner_reset(self):
        self.dll.temperature_follows_setpoint = False
        source = self.base.replace('start_k = 2.0\nstop_k = 2.0', 'start_k = 2.1\nstop_k = 2.1')
        self.write_config(("temperature", "smu"), source=source)
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(all(r["actual.temperature_k"] == 2.0 and r["requested.temperature_k"] == 2.1
                            for r in load_combination_rows(self.database)))
        self.dll.temperature_follows_setpoint = True
        source = self.base.replace('stop_k = 2.0', 'stop_k = 2.1')
        self.write_config(("smu", "temperature"), source=source)
        result = self.execute("reset")
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database, run_id="reset")
        self.assertEqual([r["requested.temperature_k"] for r in rows], [2, 2.1] * 3)

    def test_preflight_limits_and_static_temperature_reset_reject(self):
        source = self.base.replace('bz_t = 0.0 },', 'bz_t = 3.01 },')
        self.write_config(("magnetic",), source=source)
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        source = self.base.replace('stop_k = 2.0', 'stop_k = 2.3').replace('max_delta_k = 250.0', 'max_delta_k = 0.11')
        self.write_config(("smu", "temperature"), source=source)
        with self.assertRaisesRegex(ValueError, "reset"):
            load_hardware_combination(self.path)
        self.assertEqual(self.dll.events, [])
        self.dll.bz_t = self.dll.setpoint_z_t = 3.01
        self.write_config(("temperature", "smu"))
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertNotIn("set_temperature", self.dll.events)
        self.assertEqual(self.adapters, {})
        self.assertTrue(result["cleanup"]["manual_verification_required"])

    def test_drift_during_electrical_read_rejects_and_cleans_all(self):
        self.write_config(("temperature", "magnetic", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:  # First formal, after source enable.
                    self.dll.sample_temperature_k = 2.1
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("drifted", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertTrue(any(k == "smu_formal_sample" for k, _ in self.events()))
        self.assertIn("sweep_zero", self.dll.events)
        self.assertEqual(self.dll.temperature_control, 0)
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_communication_failure_retains_last_state_and_still_attempts_cleanup(self):
        self.write_config(("temperature", "magnetic", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.return_codes["get_field_x"] = 1
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        actions = {a["module"]: a for a in result["cleanup"]["actions"]}
        self.assertIn("last_confirmed_state_not_current", actions["magnetic"]["result"])
        self.assertFalse(actions["magnetic"]["clean"])
        self.assertFalse(actions["temperature"]["clean"])
        self.assertEqual(self.dll.events.count("disconnect"), 1)
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_zero_and_hold_policies_and_stale_target_preload(self):
        for policy in ("hold", "zero"):
            self.dll = FakeAttoDryDll()
            self.dll.temperature_follows_setpoint = True
            self.dll.user_temperature_k = 300
            source = self.base.replace('normal_end_field_policy = "hold"', f'normal_end_field_policy = "{policy}"')
            source = source.replace('{ bx_t = 0.0, bz_t = 0.0 },', '{ bx_t = 0.01, bz_t = 0.0 },')
            self.write_config(("temperature", "magnetic"), source=source)
            result = self.execute(policy)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(self.dll.targets_at_temperature_enable, [2.0])
            self.assertEqual(self.dll.temperature_control, 1)
            self.assertEqual("sweep_zero" in self.dll.events, policy == "zero")
            self.assertAlmostEqual(self.dll.bx_t, .01 if policy == "hold" else 0)

    def test_keyboard_interrupt_still_cleans_and_closes_once(self):
        self.write_config(("temperature", "magnetic", "smu"))
        with patch.object(CryostatPointSession, "begin_sample", side_effect=KeyboardInterrupt):
            result = self.execute()
        self.assertEqual(result["status"], "interrupted", result)
        self.assertIn("sweep_zero", self.dll.events)
        self.assertEqual(self.dll.temperature_control, 0)
        self.assertEqual(self.dll.events.count("disconnect"), 1)
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_segments_preserve_boundary_and_descending_metadata(self):
        source = re.sub(r"(?m)^points = \[.*?^\]", '''axis = "x"
segments = [
  { min = 0.0, max = 0.01, points = 2 },
  { min = 0.0, max = 0.01, points = 2, direction = "descending" },
]''', self.base, count=1, flags=re.DOTALL)
        self.write_config(("magnetic",), source=source)
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual([r["requested.field_x_t"] for r in rows], [0, .01, .01, 0])
        self.assertEqual([r["axes.magnetic.segment"] for r in rows], ["0", "0", "1", "1"])
        self.assertEqual([r["axes.magnetic.direction"] for r in rows],
                         ["ascending", "ascending", "descending", "descending"])

    def test_float32_boundary_rejected_without_connection(self):
        # Exact decimal hypot=3, but binary32 component rounding exceeds 3.
        source = self.base.replace('{ bx_t = 0.0, bz_t = 0.0 },', '{ bx_t = 1.8, bz_t = 2.4 },')
        self.write_config(("magnetic",), source=source)
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        self.assertEqual(self.dll.events, [])

    def test_failed_field_write_zeroes_and_disables_temperature(self):
        self.write_config(("temperature", "magnetic"), source=self.base.replace(
            '{ bx_t = 0.0, bz_t = 0.0 },', '{ bx_t = 0.01, bz_t = 0.0 },'))
        self.dll.return_codes["set_field_x"] = 1
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("sweep_zero", self.dll.events)
        self.assertEqual(self.dll.temperature_control, 0)
        self.assertTrue(result["cleanup"]["manual_verification_required"])

    def test_audit_failure_does_not_skip_safety_actions(self):
        self.write_config(("temperature", "magnetic", "smu"))
        original = CombinationStore.event
        def event(store, run_id, kind, payload):
            if kind == "environment_formal_window":
                raise OSError("fake disk full")
            return original(store, run_id, kind, payload)
        with patch.object(CombinationStore, "event", event):
            result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("sweep_zero", self.dll.events)
        self.assertEqual(self.dll.temperature_control, 0)
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_descending_temperature_waits_for_cooldown_or_times_out(self):
        self.dll.sample_temperature_k = 2.5
        self.dll.temperature_follows_setpoint = False
        self.write_config(("temperature",))
        result = self.execute()
        # Not an immediate false overshoot, nor a falsely accepted 2.5K plateau.
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("timed out", result["error"])
        self.assertEqual(self.dll.temperature_control, 0)
        self.dll = FakeAttoDryDll()
        self.dll.sample_temperature_k = 2.5
        self.dll.temperature_follows_setpoint = False
        original = self.dll.AttoDRY_Interface_getSampleTemperature
        def read(pointer):
            if self.dll.temperature_control:
                self.dll.sample_temperature_k = max(2, self.dll.sample_temperature_k - .06)
            return original(pointer)
        self.dll.AttoDRY_Interface_getSampleTemperature = read
        result = self.execute("cooldown")
        self.assertEqual(result["status"], "completed", result)

    def test_cleanup_failure_promotes_remaining_environment_to_failure_policy(self):
        self.write_config(("temperature", "magnetic", "smu"))
        from attodry_control.three_smu import ThreeSmuSession
        original = ThreeSmuSession.cleanup_points
        def cleanup(session, *args, **kwargs):
            result = original(session, *args, **kwargs)
            result["manual_verification_required"] = True
            return result
        with patch.object(ThreeSmuSession, "cleanup_points", cleanup):
            result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("sweep_zero", self.dll.events)
        self.assertEqual(self.dll.temperature_control, 0)

    def test_partial_connection_and_close_failures_do_not_double_end(self):
        self.write_config(("temperature", "magnetic"))
        self.dll.return_codes["is_initialized"] = 1
        result = self.execute("connect-failed")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.dll.events.count("disconnect"), 1)
        self.assertEqual(self.dll.events.count("end"), 1)
        self.dll = FakeAttoDryDll()
        self.dll.temperature_follows_setpoint = True
        self.dll.return_codes["disconnect"] = 1
        result = self.execute("close-failed")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.dll.events.count("disconnect"), 1)
        self.assertEqual(self.dll.events.count("end"), 1)
        self.assertEqual(load_combination_rows(self.database), ())
