from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.nkt_config import NktError, NktPoint, load_nkt_config
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.nkt_sdk import NktSdk, NktSdkError, _CtypesApi
from attodry_control.nkt_test import main
from attodry_control.config import load_config, load_temperature_operation_config


ROOT = Path(__file__).resolve().parents[1]


def example():
    return load_nkt_config(ROOT / "config/nkt_simulation.toml")


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def run(config=None, backend=None, **kwargs):
    config = config or example()
    backend = backend or SimulatedNkt(config)
    clock = Clock()
    return NktController(config, backend, clock=clock, sleep=clock.sleep, **kwargs).run()


class ConfigTests(unittest.TestCase):
    def test_example_and_center_bandwidth(self):
        c = example()
        self.assertEqual(c.points[0].edges_nm, (590, 610))
        self.assertEqual(c.points[1].edges_nm, (635, 665))

    def test_invalid_values_and_mode_capabilities(self):
        c = example()
        cases = [
            replace(c, max_level_pct=float("nan")), replace(c, max_level_pct=True),
            replace(c, dwell_s=-1), replace(c, timeout_s=0), replace(c, points=()),
            replace(c, emit="true"), replace(c, mode="unknown"),
            replace(c, points=(NktPoint(float("inf"), 600, 20, 10),)),
            replace(c, points=(NktPoint(11, 600, 20, 10),)),
            replace(c, points=(NktPoint(5, 400, 20, 10),)),
            replace(c, points=(NktPoint(5, 840, 20, 10),)),
            replace(c, points=(NktPoint(5, 600, 101, 10),)),
            replace(c, points=(NktPoint(5, 600.03, 20, 10),)),
            replace(c, points=(NktPoint(5, 600, 20, 100.1),)),
            replace(c, points=(NktPoint(5, 600, 20, 10, 2),)),
            replace(c, mode="broadband"), replace(c, mode="lltf_swir"),
            replace(c, mode="lltf_swir", points=(NktPoint(5, 999),)),
            replace(c, mode="lltf_swir", points=(NktPoint(5, 1500, 5),)),
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(NktError):
                item.validate()

    def test_strict_active_tables_and_unused_modules(self):
        original = (ROOT / "config/nkt_simulation.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text(original + '\n[cryostat]\nunconfigured = true\n')
            load_nkt_config(path)
            for content in (original.replace('nd_pct =', 'nd_percent ='),
                            original.replace('[nkt_varia]', '[nkt_vaira]'),
                            original.replace('max_level_pct = 10.0', 'max_level_pct = "CHANGE_ME"')):
                path.write_text(content)
                with self.assertRaises(NktError):
                    load_nkt_config(path)

    def test_other_loaders_accept_nkt_placeholders(self):
        path = ROOT / "config/hardware.example.toml"
        load_config(path)
        load_temperature_operation_config(path)

    def test_hardware_fails_before_loading_unverified_lltf(self):
        c = replace(example(), backend="nkt_sdk", mode="lltf_swir", points=(NktPoint(1, 1550),))
        with self.assertRaisesRegex(NktError, "LLTF"):
            c.require_hardware()


class ControllerTests(unittest.TestCase):
    def test_all_modes_and_no_fabricated_measurement(self):
        for mode, points in (("broadband", (NktPoint(1),)),
                             ("varia_bandpass", example().points),
                             ("lltf_swir", (NktPoint(1, 1000), NktPoint(2, 2300)))):
            with self.subTest(mode=mode):
                result = run(replace(example(), mode=mode, points=points, dwell_s=0.25))
                self.assertTrue(result["completed"])
                self.assertTrue(result["cleanup"]["off_confirmed"])
                self.assertTrue(all(p["accepted"] for p in result["points"]))
                self.assertTrue(all(p["measured_power_w"] is None for p in result["points"]))

    def test_no_automatic_takeover_or_interlock_reset(self):
        for field, value in (("emission_state", 3), ("interlock", 0), ("status_bits", 0x8000)):
            b = SimulatedNkt(example())
            b.source[field] = value
            result = run(backend=b)
            self.assertFalse(result["completed"])
            self.assertFalse(any(c[0] in ("emission", "configure") for c in b.commands))

    def test_filter_busy_timeout_and_partial_record(self):
        class Busy(SimulatedNkt):
            def configure(self, point):
                super().configure(point)
                self.filter["moving"] = True
        result = run(backend=Busy(example()))
        self.assertIn("Timeout", result["error"])
        self.assertTrue(result["cleanup"]["off_confirmed"])
        self.assertFalse(result["points"][0]["accepted"])

    def test_emission_intermediate_states_wait_for_confirmed_on(self):
        class Delayed(SimulatedNkt):
            pending = []
            def set_emission(self, enabled):
                super().set_emission(enabled)
                self.pending = [7, 2, 3] if enabled else []
            def read_source(self):
                if self.pending:
                    self.source["emission_state"] = self.pending.pop(0)
                return super().read_source()
        result = run(backend=Delayed(example()))
        self.assertTrue(result["completed"], result)
        transitions = [e["readback"]["emission_state"] for e in result["events"]
                       if e["phase"] == "emission_settle" and e["role"] == "source"]
        self.assertEqual(transitions[:3], [7, 2, 3])

    def test_filter_read_failure_does_not_block_source_off(self):
        class FilterLost(SimulatedNkt):
            failed = False
            def configure(self, point):
                super().configure(point)
                self.failed = True
            def read_filter(self):
                if self.failed:
                    raise OSError("filter disconnected")
                return super().read_filter()
        result = run(backend=FilterLost(example()))
        self.assertIn("disconnected", result["error"])
        self.assertTrue(result["cleanup"]["off_confirmed"])
        self.assertTrue(result["source_state_current"])
        self.assertFalse(result["state_current"])

    def test_disconnect_preserves_last_confirmed_on_without_claiming_off(self):
        class Lost(SimulatedNkt):
            reads = 0
            def read_source(self):
                if self.source["emission_state"] == 3:
                    self.reads += 1
                if self.reads >= 2:
                    raise OSError("lost connection")
                return super().read_source()
            def set_emission(self, enabled):
                if self.reads >= 2:
                    raise OSError("cannot send off")
                super().set_emission(enabled)
        result = run(backend=Lost(example()))
        self.assertFalse(result["completed"])
        self.assertFalse(result["state_current"])
        self.assertFalse(result["cleanup"]["off_confirmed"])
        self.assertEqual(result["last_confirmed_state"]["source"]["readback"]["emission_state"], 3)
        self.assertTrue(result["cleanup"]["errors"])

    def test_interrupt_after_first_point_rejects_run_and_cleans_up(self):
        class Interrupted(SimulatedNkt):
            count = 0
            def configure(self, point):
                self.count += 1
                if self.count == 2:
                    raise KeyboardInterrupt()
                super().configure(point)
        result = run(backend=Interrupted(example()))
        self.assertEqual(result["outcome"], "interrupted")
        self.assertEqual(len(result["points"]), 2)
        self.assertIsNotNone(result["points"][0]["readback"])
        self.assertTrue(result["cleanup"]["off_confirmed"])
        self.assertFalse(any(p["accepted"] for p in result["points"]))

    def test_external_change_rejected(self):
        class Changed(SimulatedNkt):
            def set_emission(self, enabled):
                super().set_emission(enabled)
                if enabled:
                    self.source["level_pct"] = 100
        result = run(backend=Changed(example()))
        self.assertFalse(result["completed"])
        self.assertTrue(result["cleanup"]["off_confirmed"])

    def test_power_meter_interface_requires_plane_and_records_watts(self):
        class Meter:
            def read_power_w(self, wavelength_nm):
                return wavelength_nm * 1e-6
        result = run(power_meter=Meter(), measurement_plane="sample")
        self.assertAlmostEqual(result["points"][0]["measured_power_w"], 0.0006)
        with self.assertRaises(NktError):
            run(power_meter=Meter())

    def test_invalid_power_meter_value_cleans_up(self):
        class Meter:
            def read_power_w(self, wavelength_nm):
                return float("nan")
        result = run(power_meter=Meter(), measurement_plane="sample")
        self.assertFalse(result["completed"])
        self.assertTrue(result["cleanup"]["off_confirmed"])

    def test_readback_failure_and_cleanup_failure_both_retained(self):
        class Broken(SimulatedNkt):
            def configure(self, point):
                raise RuntimeError("primary problem")
            def close(self):
                raise RuntimeError("close problem")
        result = run(backend=Broken(example()))
        self.assertIn("primary problem", result["error"])
        self.assertIn("close problem", result["cleanup"]["errors"][0])


class FakeApi:
    """Independent raw register emulator, including variable-width PP replies."""
    def __init__(self):
        self.calls = []
        self.data = {}
        for address, typ, serial in ((1, 0x60, b"SOURCE01"), (2, 0x68, b"VARIA001")):
            self.data[address, 0x61] = bytes([typ])
            self.data[address, 0x65] = serial
            self.data[address, 0x64] = b"\x01\x02"
            self.data[address, 0x66] = b"\x00\x00"
        for reg, value, width in ((0x30, 0, 1), (0x31, 0, 2), (0x32, 2, 2),
                                  (0x37, 50, 2), (0x38, 50, 2), (0x34, 1, 1)):
            self.data[1, reg] = value.to_bytes(width, "little")
        for reg, value in ((0x32, 100), (0x33, 6100), (0x34, 5900), (0x13, 123)):
            self.data[2, reg] = value.to_bytes(2, "little")

    def open(self, port):
        self.calls.append(("open", port))
        return 0

    def close(self, port):
        self.calls.append(("close", port))
        return 0

    def read(self, port, address, register):
        self.calls.append(("read", address, register))
        return (0, self.data[address, register]) if (address, register) in self.data else (4, b"")

    def write(self, port, address, register, value, width):
        self.calls.append(("write", address, register, value, width))
        self.data[address, register] = value.to_bytes(width, "little")
        if address == 1 and register == 0x30:
            self.data[1, 0x66] = (1 if value == 3 else 0).to_bytes(2, "little")
        return 0


def hardware_config(**kwargs):
    return replace(example(), backend="nkt_sdk", port="SIMULATED_PORT", dll_path="unused/NKTPDLL.dll",
                   source_serial="SOURCE01", varia_serial="VARIA001", **kwargs)


class SdkTests(unittest.TestCase):
    def tearDown(self):
        NktSdk._owned_ports.clear()

    def test_no_implicit_io_and_authorization(self):
        api = FakeApi()
        b = NktSdk(hardware_config(), api=api)
        with self.assertRaises(NktError):
            b.open()
        with self.assertRaises(NktError):
            b.read_source()
        self.assertEqual(api.calls, [])

    def test_reads_units_monitor_and_variable_width(self):
        api = FakeApi()
        b = NktSdk(hardware_config(), api=api).open(authorize_connection=True)
        self.assertEqual(b.read_source()["pulse_picker_ratio"], 1)
        api.data[1, 0x34] = (300).to_bytes(2, "little")
        self.assertEqual(b.read_source()["pulse_picker_ratio"], 300)
        self.assertIsNone(b.read_filter()["monitor_pct"])
        self.assertNotIn(("read", 2, 0x13), api.calls)
        b.close()
        b = NktSdk(hardware_config(varia_monitor_present=True), api=api).open(authorize_connection=True)
        self.assertEqual(b.read_filter()["monitor_pct"], 12.3)
        self.assertEqual(b.read_filter()["bandwidth_setpoint_nm"], 20)
        b.close()
        self.assertFalse(any(c[0] == "write" for c in api.calls))

    def test_unknown_identity_or_bad_length_blocks_settings(self):
        for reg, value in ((0x61, b"\x61"), (0x65, b"WRONG001"), (0x30, b"\x00\x00")):
            api = FakeApi()
            api.data[1, reg] = value
            b = NktSdk(hardware_config(), api=api, authorize_writes=True).open(authorize_connection=True)
            with self.assertRaises(NktSdkError):
                b.configure(example().points[0])
            b.close()
            self.assertFalse(any(c[0] == "write" for c in api.calls))

    def test_readonly_adapter_blocks_every_write(self):
        api = FakeApi()
        b = NktSdk(hardware_config(), api=api).open(authorize_connection=True)
        for operation in (lambda: b.set_emission(True), lambda: b.set_emission(False),
                          lambda: b.configure(example().points[0])):
            with self.assertRaises(NktError):
                operation()
        b.close()
        self.assertFalse(any(c[0] == "write" for c in api.calls))

    def test_single_bus_owner(self):
        b = NktSdk(hardware_config(), api=FakeApi()).open(authorize_connection=True)
        second = FakeApi()
        with self.assertRaises(NktError):
            NktSdk(hardware_config(), api=second).open(authorize_connection=True)
        self.assertEqual(second.calls, [])
        b.close()

    def test_register_order_and_full_fake_hardware_run(self):
        c = hardware_config()
        api = FakeApi()
        b = NktSdk(c, api=api, authorize_writes=True).open(authorize_connection=True)
        clock = Clock()
        result = NktController(c, b, clock=clock, sleep=clock.sleep).run(
            authorize_writes=True, confirm_manual_route=True)
        self.assertTrue(result["completed"], result)
        writes = [x for x in api.calls if x[0] == "write"]
        upper = writes.index(("write", 2, 0x33, 6650, 2))
        lower = writes.index(("write", 2, 0x34, 6350, 2))
        self.assertLess(upper, lower)
        self.assertFalse(any(x[1] == 1 and x[2] in (0x31, 0x32, 0x36) for x in writes))
        self.assertEqual(api.data[1, 0x30], b"\x00")
        self.assertEqual(len(b.command_log), len(writes))
        self.assertTrue(all(entry["result"] == 0 for entry in b.command_log))

    def test_sdk_error_does_not_become_zero(self):
        api = FakeApi()
        del api.data[1, 0x38]
        b = NktSdk(hardware_config(), api=api).open(authorize_connection=True)
        with self.assertRaisesRegex(NktSdkError, "result=4"):
            b.read_source()
        self.assertIn("01/30", b.last_partial_readback)
        b.close()

    def test_pp_write_preserves_variable_read_width(self):
        c = hardware_config(allowed_pp_ratios=(2, 300))
        api = FakeApi()
        b = NktSdk(c, api=api, authorize_writes=True).open(authorize_connection=True)
        for ratio, width in ((2, 1), (300, 2)):
            b.configure(replace(c.points[0], pulse_picker_ratio=ratio))
            self.assertIn(("write", 1, 0x34, ratio, width), api.calls)
            self.assertEqual(b.read_source()["pulse_picker_ratio"], ratio)
        b.close()

    def test_setting_readback_mismatch_rejected(self):
        class IgnoredWrite(FakeApi):
            def write(self, *args):
                return 0
        b = NktSdk(hardware_config(), api=IgnoredWrite(), authorize_writes=True).open(authorize_connection=True)
        with self.assertRaisesRegex(NktSdkError, "readback mismatch"):
            b.configure(replace(example().points[0], source_level_pct=8))
        b.close()

    def test_failed_open_closes_and_does_not_leak_owner(self):
        class FailedOpen(FakeApi):
            def open(self, port):
                super().open(port)
                return 1
        api = FailedOpen()
        with self.assertRaises(NktSdkError):
            NktSdk(hardware_config(), api=api).open(authorize_connection=True)
        self.assertEqual(api.calls[-1], ("close", "SIMULATED_PORT"))
        self.assertEqual(NktSdk._owned_ports, set())

    @unittest.skipUnless(os.name == "nt", "Native binding targets Windows")
    def test_ctypes_signatures_and_raw_reply_buffer(self):
        class Function:
            def __init__(self, callback):
                self.callback = callback
            def __call__(self, *args):
                return self.callback(*args)
        class Dll:
            openPorts = Function(lambda *args: 0)
            closePorts = Function(lambda *args: 0)
            registerWriteU8 = Function(lambda *args: 0)
            registerWriteU16 = Function(lambda *args: 0)
            @staticmethod
            def reply(port, address, register, data, size, index):
                ctypes.memmove(data, b"\x2c\x01", 2)
                ctypes.cast(size, ctypes.POINTER(ctypes.c_ubyte))[0] = 2
                return 0
            registerRead = Function(reply)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "NKTPDLL.dll"
            path.write_bytes(b"fake file; never loaded")
            with patch("attodry_control.nkt_sdk.ctypes.CDLL", return_value=Dll()):
                api = _CtypesApi(path)
                self.assertEqual(api.read("SIMULATED", 1, 0x34), (0, b"\x2c\x01"))
                self.assertEqual(api.dll.registerRead.argtypes[-1], ctypes.c_short)
                self.assertEqual(api.dll.registerWriteU16.argtypes[-2], ctypes.c_ushort)
                self.assertEqual(api.dll.openPorts.restype, ctypes.c_ubyte)


class CliTests(unittest.TestCase):
    def invoke(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(args)

    def test_describe_and_validate_do_not_load_sdk(self):
        with patch("attodry_control.nkt_sdk._CtypesApi", side_effect=AssertionError("DLL touched")):
            self.assertEqual(self.invoke(["describe"]), 0)
            self.assertEqual(self.invoke(["validate-config", "--config", str(ROOT / "config/nkt_simulation.toml")]), 0)

    def test_simulation_writes_audit_files_and_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "demo.toml"
            text = (ROOT / "config/nkt_simulation.toml").read_text().replace(
                '"../run_data/nkt_simulation"', '"records"')
            path.write_text(text)
            with patch("attodry_control.nkt_sdk._CtypesApi", side_effect=AssertionError("DLL touched")):
                self.assertEqual(self.invoke(["simulate", "--config", str(path)]), 0)
            files = list((Path(td) / "records").glob("*.json"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text())
            self.assertTrue(record["simulated"])
            self.assertTrue(record["completed"])
            self.assertGreater(len(files[0].with_suffix(".jsonl").read_text().splitlines()), 5)

    def test_hardware_flags_checked_before_dll(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.toml"
            content = (ROOT / "config/nkt_simulation.toml").read_text()
            content = content.replace('[nkt_source]', '[nkt_source]\nport="SIMULATED_PORT"\naddress=1\nexpected_serial="SOURCE01"\ndll_path="test/NKTPDLL.dll"')
            content = content.replace('[nkt_varia]', '[nkt_varia]\naddress=2\nexpected_serial="VARIA001"')
            content = content.replace('backend = "simulation"', 'backend = "nkt_sdk"')
            path.write_text(content)
            with patch("attodry_control.nkt_sdk._CtypesApi", side_effect=AssertionError("DLL touched")) as loader:
                for command in ("diagnose", "run"):
                    self.assertEqual(self.invoke([command, "--config", str(path)]), 2)
                loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
