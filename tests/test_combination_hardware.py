from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows, select_series
from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import load_electrical_combination, run_electrical_combination
from attodry_control.combination_store import CombinationStore, open_readonly, run_snapshot
from tests.test_sr830 import TrackingVisaResource, FakeResourceManager, responses
from tests.test_three_smu import FakeAdapter


ROOT = Path(__file__).resolve().parents[1]


class ElectricalCombinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "hardware.local.toml"
        self.database = self.directory / "scan.sqlite"
        source = (ROOT / "config/hardware.example.toml").read_text(encoding="utf-8")
        source = source.replace("CHANGE_ME_SR830_XX_VISA_ADDRESS", "FAKE::XX")
        source = source.replace("CHANGE_ME_SR830_XY_VISA_ADDRESS", "FAKE::XY")
        source = source.replace("CHANGE_ME_BIAS_SMU_VISA_ADDRESS", "FAKE::BIAS")
        source = source.replace('max_abs_voltage_v = "CHANGE_ME"', 'max_abs_voltage_v = 0.01')
        source = source.replace('max_abs_current_a = "CHANGE_ME"', 'max_abs_current_a = 0.000001')
        source = re.sub(r"(?m)^excitation_ranges = \[.*?^\]", "excitation_points_v_rms = [0.004, 0.008]",
                        source, flags=re.DOTALL)
        source = source.replace('{ min = -0.1, max = 0.1, scale = "linear", step = 0.05 }',
                                '{ min = -0.0001, max = 0.0001, scale = "linear", points = 3 }')
        self.base = source
        self.write_config()
        shutil.copyfile(ROOT / "config/lockin_safety.toml", self.directory / "lockin_safety.toml")
        self.frequency = {"hz": 17.777}
        self.xx = TrackingVisaResource(responses(1), shared_frequency=self.frequency, name="xx")
        self.xy = TrackingVisaResource(responses(0), shared_frequency=self.frequency, name="xy")
        for resource in (self.xx, self.xy):
            resource.responses["OFLT?"] = "9"
        self.manager = FakeResourceManager({"FAKE::XX": self.xx, "FAKE::XY": self.xy})
        self.log = []
        self.adapters = {}
        self.modify_adapter = lambda adapter: None
        sleep = patch("attodry_control.lockin_test.time.sleep", return_value=None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def write_config(self, order=("smu", "lockin"), extra="", source=None):
        self.path.write_text((source or self.base) + """
[combination_scan]
backend = "hardware"
order = """ + json.dumps(order) + """
samples_per_condition = 1
repeats = 1
run_name = "offline_electrical_fixture"
note = "Fake devices only"
""" + extra, encoding="utf-8")

    def factory(self, role, config):
        adapter = FakeAdapter(role, self.log, config)
        original = adapter.read
        def read():
            self.log.append(("read", role, adapter.source, float(self.xx.responses["SLVL?"])))
            return original()
        adapter.read = read
        self.modify_adapter(adapter)
        self.adapters[role] = adapter
        return adapter

    def execute(self, run_id="test", **kwargs):
        config = load_electrical_combination(self.path)
        with CombinationStore(self.database) as store:
            return run_electrical_combination(
                config, store, run_id, authorize_hardware=True,
                confirm_xy_sine_disconnected=True, smu_adapter_factory=self.factory,
                manager_factory=lambda: self.manager, sleep=lambda _: None, **kwargs)

    def events(self):
        with closing(open_readonly(self.database)) as db:
            return [(r[0], json.loads(r[1])) for r in db.execute(
                "SELECT event_type,payload_json FROM combination_events ORDER BY event_id")]

    def test_both_loop_orders_fresh_reads_harmonics_regroup_and_cleanup(self):
        for order in (("smu", "lockin"), ("lockin", "smu")):
            self.write_config(order)
            run_id = "-".join(order)
            result = self.execute(run_id)
            self.assertEqual(result["status"], "completed", result)
            rows = load_combination_rows(self.database, run_id=run_id)
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(not row["simulated"] for row in rows))
            self.assertTrue(all("measured.lockin_xy_h2_amplitude_v" in row for row in rows))
            self.assertTrue(all("measured.lockin_xx_h2_amplitude_v" not in row for row in rows))
            groups = select_series(rows, x="measured.smu_bias_voltage_v",
                                   y="measured.lockin_xy_h1_amplitude_v",
                                   group_by=("requested.lockin_excitation_v_rms",))
            self.assertEqual([len(group.rows) for group in groups], [3, 3])
            self.assertEqual(self.adapters["smu_bias"].read_count, 8)  # enable + six + zero
            self.assertFalse(self.adapters["smu_bias"].output)
            self.assertEqual(float(self.xx.responses["SLVL?"]), .004)
            self.assertEqual(self.xx.responses["HARM?"].strip(), "1")
            self.assertTrue(self.xx.closed and self.xy.closed and self.manager.closed)
        self.assertEqual(set(self.adapters), {"smu_bias"})

    def test_only_requested_modules_open(self):
        for order in (("smu",), ("lockin",)):
            self.write_config(order)
            self.manager.opened.clear()
            self.adapters.clear()
            result = self.execute(order[0])
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(bool(self.manager.opened), "lockin" in order)
            self.assertEqual(bool(self.adapters), "smu" in order)

    def test_authorization_before_any_resource(self):
        config = load_electrical_combination(self.path)
        for flags in ({}, {"authorize_hardware": True}):
            with self.subTest(flags=flags), CombinationStore(self.database) as store:
                with self.assertRaises(ValueError):
                    run_electrical_combination(config, store, "unauthorized",
                        smu_adapter_factory=self.factory, manager_factory=lambda: self.manager, **flags)
        self.assertEqual(self.manager.opened, [])
        self.assertEqual(self.adapters, {})

    def test_static_limits_and_unsupported_modules(self):
        for order in (("unknown", "lockin"), (), ("smu", "smu")):
            self.write_config(order)
            with self.assertRaises(ValueError):
                load_electrical_combination(self.path)
        self.write_config(source=self.base.replace("max_abs_voltage_v = 0.01",
                                                  "max_abs_voltage_v = 0.00001"))
        with self.assertRaises(ValueError):
            load_electrical_combination(self.path)
        self.assertEqual(self.log, [])
        self.assertEqual(self.manager.opened, [])

    def test_cross_module_duplicate_address_rejected(self):
        self.write_config(source=self.base.replace("FAKE::BIAS", "FAKE::XX"))
        with self.assertRaisesRegex(ValueError, "distinct"):
            load_electrical_combination(self.path)

    def test_lockin_preflight_failure_has_no_writes(self):
        self.xy.responses["LIAS?"] = "1"
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.xx.writes + self.xy.writes, [])
        self.assertFalse(any(e[0] in ("source", "configure", "output") for e in self.log))
        self.assertTrue(self.xx.closed and self.xy.closed)
        self.assertTrue(any(kind == "lockin_raw_role" for kind, _ in self.events()))

    def test_panel_time_constant_mismatch_rejected_before_smu_enable(self):
        self.xy.responses["OFLT?"] = "10"
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertIn("time_constant differs", result["error"])
        self.assertEqual(self.xx.writes + self.xy.writes, [])

    def test_smu_trip_retained_excluded_and_zero_off(self):
        self.modify_adapter = lambda a: setattr(a, "trip_on_read", 2)
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertFalse(load_combination_rows(self.database, audit=True)[0]["clean"])
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertEqual(float(self.xx.responses["SLVL?"]), .004)

    def test_nonfinite_smu_is_rejected_before_lockin_formal_sampling(self):
        self.modify_adapter = lambda a: setattr(a, "current_override", float("nan"))
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertIn("not finite", result["error"])
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_keyboard_interrupt_cleans_both_modules(self):
        self.modify_adapter = lambda a: setattr(a, "interrupt_on_read", 2)
        result = self.execute()
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertEqual(float(self.xx.responses["SLVL?"]), .004)
        self.assertEqual(load_combination_rows(self.database), ())

    def test_communication_failure_requires_manual_verification(self):
        self.modify_adapter = lambda a: setattr(a, "fail_on_read", 2)
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertFalse(result["cleanup"]["clean"])
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_lockin_failure_does_not_skip_smu_cleanup(self):
        self.xx.fail_write = "SLVL 0.008"
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertEqual(self.adapters["smu_bias"].source, 0)
        self.assertTrue(self.manager.closed)

    def test_failed_xy_read_retains_preceding_xx(self):
        original = self.xy.query
        count = 0
        def query(command):
            nonlocal count
            if command == "SNAP? 1,2,3,4,9":
                count += 1
                if count == 3:
                    raise OSError("XY disconnected")
            return original(command)
        self.xy.query = query
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(k == "lockin_raw_role" and p["role"] == "lockin_xx"
                            and p["method"] == "read_harmonic_sample" for k, p in self.events()))
        self.assertFalse(self.adapters["smu_bias"].output)

    def test_file_monitor_and_offline_describe(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli(["describe-hardware", "--config", str(self.path)]), 0)
        self.assertEqual(json.loads(out.getvalue())["plan"]["mode"], "hardware")
        self.assertEqual(self.manager.opened, [])
        self.execute()
        before = len(self.xx.queries)
        snapshot = run_snapshot(self.database, "test")
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(len(self.xx.queries), before)

    def test_repeat_and_duplicate_smu_points_preserved(self):
        source = self.base.replace(
            '{ min = -0.0001, max = 0.0001, scale = "linear", points = 3 }',
            '{ min = 0.0, max = 0.0001, scale = "linear", points = 2 }')
        source = source.replace("bidirectional = false\n# Active", "bidirectional = true\n# Active")
        self.write_config(source=source)
        text = self.path.read_text(encoding="utf-8").replace("repeats = 1", "repeats = 2")
        self.path.write_text(text, encoding="utf-8")
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 12)
        self.assertEqual([r["requested.smu_bias_v"] for r in rows[:6]], [0, 0, .0001, .0001, 0, 0])

    def test_audit_failure_cannot_prevent_cleanup_or_close(self):
        event = CombinationStore.event
        def fail_recording(store, run_id, kind, payload):
            if kind in {"lockin_formal_pair", "lockin_command_attempt", "cleanup_zero"}:
                if any(a.read_count >= 2 for a in self.adapters.values()):
                    raise OSError("disk unavailable")
            return event(store, run_id, kind, payload)
        with patch.object(CombinationStore, "event", fail_recording):
            result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertEqual(self.adapters["smu_bias"].source, 0)
        self.assertEqual(float(self.xx.responses["SLVL?"]), .004)
        self.assertTrue(self.manager.closed)

    def test_unknown_active_output_is_not_taken_over_or_reported_clean(self):
        self.modify_adapter = lambda a: setattr(a, "output", True)
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertFalse(any(e[0] in {"output", "source", "configure"} for e in self.log))
        self.assertEqual(self.manager.opened, [])
        self.assertTrue(any(k == "smu_preflight_role" for k, _ in self.events()))

    def test_existing_run_is_not_reopened(self):
        self.execute()
        before = len(self.log), len(self.xx.queries)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.execute()
        self.assertEqual((len(self.log), len(self.xx.queries)), before)

    def test_smu_only_ignores_incomplete_inactive_lockin_and_cryo(self):
        source = self.base.replace('model = "SR830"', 'model = "UNKNOWN"')
        self.write_config(("smu",), source=source)
        (self.directory / "lockin_safety.toml").unlink()
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.manager.opened, [])

    def test_shared_table_does_not_break_standalone_loaders(self):
        from attodry_control.config import (
            load_config, load_temperature_operation_config,
            load_temperature_excitation_operation_config, load_magnetic_field_operation_config)
        for loader in (load_config, load_temperature_operation_config,
                       load_temperature_excitation_operation_config, load_magnetic_field_operation_config):
            with self.subTest(loader=loader.__name__):
                loader(self.path)

    def test_unsupported_harmonic_fails_before_connection(self):
        self.write_config(source=self.base.replace("frequency_hz = 17.777", "frequency_hz = 50000.0"))
        with self.assertRaisesRegex(ValueError, "harmonic detection frequency"):
            self.execute()
        self.assertEqual(self.log, [])
        self.assertEqual(self.manager.opened, [])

    def test_two_samples_per_leaf_are_not_standalone_sweep_repeats(self):
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "samples_per_condition = 1", "samples_per_condition = 2"), encoding="utf-8")
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 12)
        self.assertEqual(self.adapters["smu_bias"].read_count, 14)
        self.assertEqual([r["sample_index"] for r in rows], [0, 1] * 6)

    def test_close_failure_excludes_completed_samples(self):
        def bad_close():
            raise OSError("close failed")
        self.manager.close = bad_close
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(len(load_combination_rows(self.database, audit=True)), 6)
        self.assertTrue(any(e[0] == "close" for e in self.log))

    def test_non_oserror_transport_failure_never_certifies_later_recovery(self):
        class FakeVisaTimeout(Exception):
            pass
        def inject(adapter):
            original = adapter.read
            def read():
                if adapter.read_count == 1:
                    adapter.read_count += 1
                    raise FakeVisaTimeout("VISA timeout")
                return original()
            adapter.read = read
        self.modify_adapter = inject
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertFalse(result["cleanup"]["clean"])
        self.assertFalse(self.adapters["smu_bias"].output)
        self.assertEqual(self.adapters["smu_bias"].source, 0)


if __name__ == "__main__":
    unittest.main()
