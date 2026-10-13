from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.photoresponse import PhotoresponseRunner, SimulatedElectrical
from attodry_control.photoresponse_analysis import load_results, export_results
from attodry_control.photoresponse_cld import CldController, SimulatedCld
from attodry_control.photoresponse_cli import _run
from attodry_control.photoresponse_combination import PhotoresponseCombinationPlan
from attodry_control.photoresponse_config import load_photoresponse_config
from attodry_control.photoresponse_environment import open_environment
from attodry_control.photoresponse_power import open_photoresponse_power
from attodry_control.photoresponse_smu import PhotoresponseSmu, SimulatedSmu, normalized_resource
from attodry_control.photoresponse_storage import PhotoStore
from attodry_control.photonic_smu import SOURCE_SHA256
from attodry_control.photonic_smu.three_smu_config import ChannelPlan, ChannelRole, FinishAction, SourceMode
from test_photoresponse import Clock
from test_photoresponse_integration import environment_config, hardware_setup


ROOT = Path(__file__).resolve().parents[1]


def configuration(kind="nkt", short=True):
    config = load_photoresponse_config(ROOT / f"config/photoresponse_smu_{kind}_simulation.toml")
    if short:
        config = replace(config, nkt=replace(config.nkt, points=config.nkt.points[:1]),
            power=None, bias_points=config.bias_points[:1], samples_per_window=1, combination_order=None)
    return config


class RecordingSmu(SimulatedSmu):
    def __init__(self, role, config, log):
        super().__init__(role, config)
        self.log, self.reads, self.fail, self.offset, self.trip = log, 0, False, 0., False

    def set_source(self, value):
        self.log.append((self.role, "source", value))
        super().set_source(value)

    def set_output(self, enabled):
        self.log.append((self.role, "output", enabled))
        super().set_output(enabled)

    def read(self):
        self.reads += 1
        self.log.append((self.role, "read"))
        if self.fail: raise OSError("injected gate communication failure")
        return replace(super().read(), source_setpoint=self.source + self.offset, compliance_trip=self.trip)

    def close(self):
        self.log.append((self.role, "close"))
        super().close()


def setup(config=None, on_event=None, factory=None):
    config = config or configuration()
    clock, log, adapters = Clock(), [], []
    def recording_factory(role, h):
        adapter = RecordingSmu(role, h, log)
        adapters.append(adapter)
        return adapter
    smu = PhotoresponseSmu(config, simulated=True, on_event=on_event,
        adapter_factory=factory or recording_factory, sleep=clock.sleep, clock=clock)
    cld = hasattr(config.nkt, "diode_model")
    backend = (SimulatedCld if cld else SimulatedNkt)(config.nkt)
    laser = (CldController if cld else NktController)(config.nkt, backend, clock=clock, sleep=clock.sleep)
    electrical = SimulatedElectrical(config, clock=clock)
    power = open_photoresponse_power(config.power, laser, clock=clock, sleep=clock.sleep) if config.power else None
    environment = open_environment(config.environment) if config.environment else None
    runner = PhotoresponseRunner(config, laser, electrical, smu=smu, power=power,
        environment=environment, clock=clock, sleep=clock.sleep, on_event=on_event)
    return runner, smu, adapters, log, electrical


class SmuConfigTests(unittest.TestCase):
    def test_reference_driver_bytes_unchanged(self):
        for name, digest in SOURCE_SHA256.items():
            self.assertEqual(hashlib.sha256((ROOT / "src/attodry_control/photonic_smu" / name).read_bytes()).hexdigest(), digest)

    def test_single_sample_source_and_observable_are_explicit(self):
        c = configuration("cld")
        self.assertEqual(c.quantity, "voltage_v")
        self.assertEqual(c.excitation, "smu_current")
        for mode, bias in (("open_circuit_voltage", (None,)), ("dc_current_voltage", (0.,)),
                           ("voltage_bias_current", (0.,))):
            with self.assertRaisesRegex(NktError, "explicit SMU excitation"):
                replace(c, mode=mode, bias_points=bias,
                    detector="keithley2400" if mode == "voltage_bias_current" else "keithley2182a").validate()
        for bad in (replace(c, smu=None), replace(c, detector="keithley2400"),
                    replace(c, max_abs_current_a=None), replace(c, max_abs_voltage_v=.001)):
            with self.assertRaises(NktError): bad.validate()
        voltage_hw = replace(c.smu.hardware, smu_bias=replace(c.smu.hardware.smu_bias, source_mode=SourceMode.VOLTAGE))
        replace(c, mode="smu_voltage_voltage", smu=replace(c.smu, hardware=voltage_hw)).validate()

    def test_gate_requires_voltage_source_and_safe_finish(self):
        c = configuration()
        bad = [replace(c.smu, plan=replace(c.smu.plan, finish_action=FinishAction.HOLD)),
               replace(c.smu, hardware=replace(c.smu.hardware,
                    gate_top=replace(c.smu.hardware.gate_top, source_mode=SourceMode.CURRENT))),
               replace(c.smu, plan=replace(c.smu.plan,
                    gate_top=ChannelPlan(ChannelRole.FIXED, False, fixed=0.)))]
        for smu in bad:
            with self.assertRaises(NktError): replace(c, smu=smu).validate()

    def test_resource_aliases_rejected_before_any_factory(self):
        self.assertEqual(normalized_resource(" asrl007::instr "), normalized_resource("COM7"))
        self.assertEqual(normalized_resource("GPIB::22"), normalized_resource("gpib0::22::0::instr"))
        c = configuration()
        for address in (" fake::gate_top::instr ", "FAKE::GATE_TOP::INSTR"):
            bad = replace(c, smu=replace(c.smu, hardware=replace(c.smu.hardware,
                gate_bottom=replace(c.smu.hardware.gate_bottom, address=address))))
            factory = Mock()
            with self.assertRaisesRegex(NktError, "share resource"):
                PhotoresponseSmu(bad, simulated=True, adapter_factory=factory)
            factory.assert_not_called()
            with patch("attodry_control.nkt_control.SimulatedNkt") as opened:
                with self.assertRaises(NktError): _run(bad, SimpleNamespace(), Mock(), 1)
                opened.assert_not_called()
        # One instrument cannot concurrently be a gate and the selected 2400 detector.
        hardware_config = hardware_setup(mode="voltage_bias_current", detector="keithley2400", relay=False)[0].config
        c = replace(hardware_config, smu=c.smu)
        detector_address = c.hardware.addresses()["detector"]
        c = replace(c, smu=replace(c.smu, hardware=replace(c.smu.hardware,
            gate_bottom=replace(c.smu.hardware.gate_bottom, address=" " + detector_address.lower() + " "))))
        with self.assertRaisesRegex(NktError, "share resource"): c.validate()

    def test_smu_opt_in_and_plan_hash(self):
        source = (ROOT / "config/photoresponse_smu_nkt_simulation.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "disabled.toml"
            path.write_text(source.replace("enabled = true", "enabled = false").replace('"smu", ', ''))
            c = load_photoresponse_config(path)
            self.assertIsNone(c.smu)
            self.assertNotIn("gate_bottom", PhotoresponseCombinationPlan(c).modules)
            store = PhotoStore(Path(td) / "audit.sqlite")
            try:
                enabled = configuration()
                run_id, _ = store.start(enabled)
                changed = replace(enabled, smu=replace(enabled.smu, plan=replace(enabled.smu.plan,
                    gate_bottom=replace(enabled.smu.plan.gate_bottom, points=(-.002, .002)))))
                with self.assertRaisesRegex(NktError, "configuration|plan|hash"):
                    store.start(changed, resume=run_id)
            finally: store.close()

    def test_hardware_needs_separate_authorization_and_identities_before_open(self):
        c = replace(hardware_setup()[0].config, smu=configuration().smu)
        factory = Mock()
        with self.assertRaisesRegex(NktError, "wiring"):
            PhotoresponseSmu(c, simulated=False, authorize_writes=True, adapter_factory=factory)
        c = replace(c, smu=replace(c.smu, wiring_confirmed=True))
        with self.assertRaisesRegex(NktError, "expected_idn"):
            PhotoresponseSmu(c, simulated=False, authorize_writes=True, adapter_factory=factory)
        c = replace(c, smu=replace(c.smu, expected_idn={r:"EXACT,"+r for r in c.smu.roles}))
        with self.assertRaisesRegex(NktError, "authorize-smu"):
            PhotoresponseSmu(c, simulated=False, adapter_factory=factory)
        factory.assert_not_called()
        log, opened = [], []
        def fake(role, h):
            adapter = RecordingSmu(role, h, log)
            opened.append(adapter)
            return adapter
        with self.assertRaisesRegex(NktError, "identity mismatch"):
            PhotoresponseSmu(c, simulated=False, authorize_writes=True, adapter_factory=fake)
        self.assertTrue(all(a.closed for a in opened))
        self.assertFalse(any(e[1] in ("source", "output") for e in log))


class SmuRunTests(unittest.TestCase):
    def test_two_examples_complete_and_each_leaf_has_selected_roles(self):
        for kind, count in (("nkt", 16), ("cld", 8)):
            c = configuration(kind, short=False)
            runner, smu, adapters, log, electrical = setup(c)
            record = runner.run()
            self.assertTrue(record["completed"], record["error"] or record["cleanup"])
            self.assertEqual(len(record["conditions"]), count)
            self.assertTrue(record["cleanup"]["smu_safe_confirmed"])
            self.assertEqual({a.role for a in adapters}, set(c.smu.roles))
            for row in record["conditions"]:
                self.assertIn("smu", row["axis_indices"])
                for window in row["windows"]:
                    for sample in window["samples"]:
                        modules = {r["module"]: r for r in sample["module_readings"]}
                        self.assertEqual(set(modules), set(runner.plan.modules))
                        self.assertEqual(modules["gate_top"]["actual"]["source_setpoint"], 0.)
                        self.assertIn("leakage_current_a", modules["gate_bottom"]["measurements"])
                        self.assertIsNone(sample["current_a"])
                if kind == "cld":
                    self.assertEqual(row["requested_current_a"], row["requested_smu"]["coordinates"]["smu_bias"])
                    self.assertFalse(any(c[0] == "source_set" for c in electrical.commands))
            self.assertTrue(all(a.closed and not a.output and a.source == 0. for a in adapters))

    def test_smu_failure_preserves_partial_and_rejects_complete_dld(self):
        runner, smu, adapters, log, electrical = setup()
        original = electrical.read
        def fail_second_role(**kw):
            reading = original(**kw)
            adapters[-1].fail = True
            return reading
        electrical.read = fail_second_role
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertFalse(record["cleanup"]["smu_safe_confirmed"])
        self.assertTrue(all(not a.output and a.closed for a in adapters))
        partial = [e for e in record["smu_audit"]["events"] if e["phase"] == "smu_sample_partial"]
        self.assertTrue(partial)
        self.assertIn("gate_top", json.dumps(partial))
        self.assertFalse(any(row["accepted"] for row in record["conditions"]))

    def test_external_source_change_rejected_before_next_detector_read(self):
        runner, smu, adapters, log, electrical = setup()
        original = smu.prepare
        def changed(*args):
            result = original(*args)
            adapters[0].offset = .001
            return result
        smu.prepare = changed
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("source setpoint differs", record["error"])
        self.assertFalse(any(c[0] == "read" for c in electrical.commands))
        self.assertFalse(record["smu_audit"]["last_confirmed"]["clean"])

    def test_gate_fault_during_power_read_stops_detector(self):
        runner, smu, adapters, log, electrical = setup(configuration(short=False))
        original = runner.power.meter.read_sample
        def trip(*args, **kwargs):
            result = original(*args, **kwargs)
            adapters[0].trip = True
            return result
        runner.power.meter.read_sample = trip
        record = runner.run()
        self.assertFalse(record["completed"])
        reads = [c for c in electrical.commands if c[0] == "read"]
        self.assertEqual(reads, [("read", False)] * runner.config.samples_per_window)
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])

    def test_environment_transition_closes_and_repreflights_smus(self):
        c = configuration()
        env = environment_config()
        env = replace(env, points=(env.points[0], {**env.points[0], "temperature_k": env.points[0]["temperature_k"] + .1}))
        runner, smu, adapters, log, _ = setup(replace(c, environment=env))
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        self.assertEqual(len(adapters), 4)  # Two selected roles, two independently preflighted sessions.
        self.assertEqual(len(record["smu_audit"]["cleanups"]), 2)
        self.assertTrue(all(a.closed and not a.output for a in adapters))

    def test_failed_environment_transition_keeps_smu_safety_unknown(self):
        c = configuration()
        env = environment_config()
        env = replace(env, points=(env.points[0], {**env.points[0], "temperature_k": env.points[0]["temperature_k"] + .1}))
        runner, smu, adapters, log, _ = setup(replace(c, environment=env))
        adapters[0].close = Mock(side_effect=OSError("transition close failure"))
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertFalse(record["cleanup"]["smu_safe_confirmed"])
        self.assertEqual(len(adapters), 2)
        self.assertTrue(all(row["environment_index"] == 0 for row in record["conditions"]))
        self.assertTrue(record["smu_audit"]["errors"])
        with self.assertRaises(NktError): smu.begin()
        with self.assertRaises(NktError): smu.finish()
        smu.close()  # Resource close stays idempotent; finish never promotes failed evidence.

    def test_partial_open_and_close_failure_release_other_roles(self):
        log, adapters = [], []
        def factory(role, h):
            if adapters: raise OSError("second resource unavailable")
            item = RecordingSmu(role, h, log)
            adapters.append(item)
            return item
        with self.assertRaises(OSError): setup(factory=factory)
        self.assertTrue(adapters[0].closed)
        self.assertFalse(any(row[1] in ("source", "output") for row in log))
        runner, smu, adapters, _, _ = setup()
        adapters[0].close = Mock(side_effect=OSError("close failure"))
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(adapters[1].closed)
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])

    def test_interruption_and_audit_failure_still_attempt_all_cleanup(self):
        for failure in (KeyboardInterrupt(), OSError("audit unavailable")):
            runner, smu, adapters, log, electrical = setup()
            if isinstance(failure, KeyboardInterrupt):
                electrical.read = Mock(side_effect=failure)
            else:
                smu.on_event = Mock(side_effect=failure)
            record = runner.run()
            self.assertFalse(record["completed"])
            self.assertTrue(record["cleanup"]["laser_off_confirmed"])
            self.assertTrue(all(a.closed and not a.output for a in adapters))

    def test_voltage_smu_excitation_does_not_become_detector_current(self):
        c = configuration("cld")
        hardware = replace(c.smu.hardware, smu_bias=replace(c.smu.hardware.smu_bias, source_mode=SourceMode.VOLTAGE))
        c = replace(c, mode="smu_voltage_voltage", smu=replace(c.smu, hardware=hardware))
        record = setup(c)[0].run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        for row in record["conditions"]:
            self.assertEqual(row["requested_voltage_v"], row["requested_smu"]["coordinates"]["smu_bias"])
            self.assertIsNone(row["requested_current_a"])
            self.assertIn("delta_voltage_v", row["result"])
            self.assertNotIn("delta_current_a", row["result"])

    def test_2400_detector_and_distinct_gates_keep_photocurrent_and_leakage_separate(self):
        c = configuration()
        c = replace(c, mode="voltage_bias_current", detector="keithley2400", bias_points=(.001,))
        record = setup(c)[0].run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        light = record["conditions"][0]["windows"][1]["samples"][0]
        modules = {r["module"]: r for r in light["module_readings"]}
        self.assertEqual(light["current_a"], modules["keithley2400"]["measurements"]["current_a"])
        self.assertNotEqual(light["current_a"], modules["gate_bottom"]["measurements"]["leakage_current_a"])
        self.assertIn("delta_current_a", record["conditions"][0]["result"])

    def test_analysis_exports_gate_and_sample_source_separately(self):
        runner, *_ = setup(configuration("cld"))
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        with tempfile.TemporaryDirectory() as td:
            path, csv = Path(td) / "run.json", Path(td) / "run.csv"
            path.write_text(json.dumps(record))
            rows = load_results(path)
            self.assertIn("gate_bottom_leakage_current_a", rows[0])
            self.assertIn("smu_bias_current_a", rows[0])
            self.assertNotIn("delta_current_a", rows[0])
            export_results(path, csv)
            self.assertIn("requested_gate_bottom", csv.read_text(encoding="utf-8-sig"))
            record["conditions"][0]["accepted"] = False
            path.write_text(json.dumps(record))
            self.assertEqual(len(load_results(path)), len(rows) - 1)
            record["cleanup"]["smu_safe_confirmed"] = False
            path.write_text(json.dumps(record))
            self.assertEqual(load_results(path), [])
            store = PhotoStore(Path(td) / "audit.sqlite")
            try:
                store.start(runner.config)
                with self.assertRaisesRegex(NktError, "Unsafe accepted promotion"):
                    store.finalize(record)
                record["cleanup"].pop("smu_safe_confirmed")
                with self.assertRaisesRegex(NktError, "Unsafe accepted promotion"):
                    store.finalize(record)
            finally: store.close()


if __name__ == "__main__": unittest.main()
