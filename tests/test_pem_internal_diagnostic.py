"""Synthetic lifecycle/fault tests; resources never connect to real hardware."""
from dataclasses import dataclass, replace
from contextlib import closing
import builtins
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from attodry_control.lockin_backend import BackendSettings, BackendStatus, LockinModel
from attodry_control.models import LockinRole
from attodry_control.pem_internal_config import DiagnosticConfig
from attodry_control.pem_internal_diagnostic import analyze, run, run_diagnostic, simulate
from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.sr830 import LiaStatus
from attodry_control.sr865a import Sr865aCaptureError, Sr865aSettings
from tests.photonics_lockin_helpers import reversed_sine_document, reversed_sine_safety_document


@dataclass(frozen=True)
class NativeStatus:
    reference_unlock_latched: bool = False
    input_overload_latched: bool = False
    output_scale_overload_latched: bool = False
    filter_fault_latched: bool = False
    configuration_changed_latched: bool = False
    power_on_latched: bool = False
    unknown_status_bits: int = 0


@dataclass(frozen=True)
class Sample:
    reference_frequency_hz: float
    harmonic: int
    status: BackendStatus


@dataclass(frozen=True)
class Capture:
    actual_rate_hz: float
    x_v: tuple[float, ...]
    y_v: tuple[float, ...]
    r_v: tuple[float, ...]
    t_s: tuple[float, ...]
    completed: bool = True
    cleanup_errors: tuple[str, ...] = ()
    capture_owned: bool = True
    stop_verified: bool = True
    capture_configuration_restored: bool = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Resource:
    def __init__(self, owner, role):
        self.owner, self.role = owner, role
        self.timeout = None
        self.closed = False

    def close(self):
        self.owner.log.append(f"{self.role}:resource_close")
        self.closed = True
        if self.owner.fault == "resource_close" and self.role == "lockin_xy":
            raise RuntimeError("injected resource close failure")

    def query_binary_values(self, *args, **kwargs):
        raise AssertionError("This orchestration fake uses its injected capture backend")


class Manager:
    def __init__(self, owner):
        self.owner = owner
        self.resources = []
        self.closed = False

    def open_resource(self, address):
        role = "lockin_xx" if address == "FAKE::XX" else "lockin_xy"
        self.owner.log.append(f"{role}:open")
        if self.owner.fault == "partial_open" and role == "lockin_xy":
            raise RuntimeError("injected second resource open failure")
        resource = Resource(self.owner, role)
        self.resources.append(resource)
        return resource

    def close(self):
        self.owner.log.append("manager:close")
        self.closed = True


class Backend:
    def __init__(self, owner, role):
        self.owner, self.role = owner, role
        self.audit = []
        self.protect_calls = 0
        self.frequency = 43210.0  # Stored internal oscillator is not active yet.
        config = owner.station.lockin.lockin_xy
        self.native = Sr865aSettings(
            identity="SRS,SR865A,FAKE,2.11", reference_source="external",
            external_reference_edge="rising", reference_input_impedance_ohm=1e6,
            harmonic=2, time_constant_s=config.time_constant_s,
            sensitivity_full_scale_v=config.sensitivity_full_scale_v,
            input_range_v_peak=config.input_range_v_peak, filter_slope_db_oct=24,
            advanced_filter=False, synchronous_filter=False, input_mode="a_minus_b",
            input_coupling="ac", shield_grounding="float", phase_shift_deg=0.0,
            sine_output_v=owner.station.lockin.reference_output.amplitude_v_rms,
            source_offset_v=0.0, source_dc_mode="common", sync_output_mode="unipolar_sync", raw=(),
        )

    def read_identity(self):
        self.owner.log.append(f"{self.role}:identity")
        return self.native.identity if self.role == "lockin_xy" else "SRS,SR830,FAKE,1.0"

    def read_source_state(self):
        if self.role == "lockin_xx":
            return {"source_voltage_v": 0.004, "dc_offset_v": 0.0, "dc_mode": "not_supported"}
        return {"source_voltage_v": self.native.sine_output_v,
                "dc_offset_v": self.native.source_offset_v, "dc_mode": self.native.source_dc_mode}

    def set_source_amplitude(self, value, **kwargs):
        if not kwargs.get("authorize_writes") or not kwargs.get("protect"):
            raise AssertionError("Source protection must be explicitly authorized")
        self.protect_calls += 1
        self.owner.log.append("xx:protect")
        if self.owner.fault == "xx_cleanup_protect" and self.protect_calls > 1:
            raise RuntimeError("injected unknown XX output during cleanup")
        return self.read_source_state()

    def read_settings(self):
        native = self.native
        if self.owner.in_capture and self.owner.fault == "settings":
            native = replace(native, sensitivity_full_scale_v=native.sensitivity_full_scale_v * 2)
        return BackendSettings(LockinRole.XY, LockinModel.SR865A, native.identity,
                               native.time_constant_s, native.sensitivity_full_scale_v,
                               native.harmonic, native.reference_source,
                               native.input_range_v_peak, native, ())

    def read_internal_frequency(self):
        return self.frequency

    def configure_internal_reference(self, frequency, **kwargs):
        self.owner.log.append("xy:internal_reference")
        self.frequency = frequency
        self.native = replace(self.native, reference_source="internal")
        return frequency

    def configure_external_reference(self, *, edge, input_impedance_ohm, **kwargs):
        self.owner.log.append("xy:restore_external")
        if self.owner.fault == "xy_restore":
            raise RuntimeError("injected XY restoration failure")
        self.native = replace(self.native, reference_source="external", external_reference_edge=edge,
                              reference_input_impedance_ohm=input_impedance_ohm)

    def restore_stored_internal_frequency(self, frequency, **kwargs):
        self.owner.log.append("xy:restore_frequency")
        self.frequency = frequency

    def configure_fixed_measurement(self, config, **kwargs):
        self.owner.log.append("xy:fixed_measurement")
        self.native = replace(self.native, time_constant_s=config.time_constant_s,
                              sensitivity_full_scale_v=config.sensitivity_full_scale_v,
                              input_range_v_peak=config.input_range_v_peak,
                              phase_shift_deg=config.phase_shift_deg,
                              shield_grounding=config.shield_grounding,
                              filter_slope_db_oct=24, advanced_filter=False, synchronous_filter=False)

    def set_harmonic(self, harmonic, **kwargs):
        self.native = replace(self.native, harmonic=harmonic)
        self.owner.log.append(f"xy:harmonic:{harmonic}")

    def read_sample(self, **kwargs):
        return Sample(self.frequency, self.native.harmonic, self.read_reference_status())

    def read_capture_status(self):
        return 0

    def read_reference_status(self, **kwargs):
        fault = self.owner.fault if self.owner.in_capture else None
        if self.role == "lockin_xx":
            lias = LiaStatus(0, False, False, False, False, False, False, False)
            if fault == "xx_unknown":
                lias = replace(lias, raw=128)
            return BackendStatus(True, False, False, False, "latched", (lias, 0), True)
        native = NativeStatus()
        locked, overloaded, instrument_error = True, False, False
        if fault == "overload":
            overloaded = True
            native = replace(native, input_overload_latched=True)
        elif fault == "unlock":
            locked = False
            native = replace(native, reference_unlock_latched=True)
        elif fault == "instrument":
            instrument_error = True
        elif fault == "unknown":
            native = replace(native, unknown_status_bits=0x8000)
        elif fault == "configuration_latch":
            native = replace(native, configuration_changed_latched=True)
        elif fault == "unknown_locked":
            locked = None
        elif fault == "unknown_overload":
            overloaded = None
        return BackendStatus(locked, overloaded, False, instrument_error,
                             "instantaneous_and_latched", native, not (overloaded or not locked or instrument_error))

    def capture_xy(self, duration, minimum_rate, *, on_poll, clock, sleep, **kwargs):
        self.owner.log.append("xy:capture")
        self.owner.in_capture = True
        rate = max(1024.0, minimum_rate)
        result = Capture(rate, (1e-6,) * 8, (0.0,) * 8, (1e-6,) * 8, tuple(i / rate for i in range(8)))
        try:
            on_poll()
            sleep(duration)
            on_poll()
            if self.owner.fault == "capture_cleanup":
                result = replace(result, completed=False, stop_verified=False,
                                 cleanup_errors=("capture stop unverified",))
                raise Sr865aCaptureError("capture cleanup failed", result)
            return result
        except BaseException as exc:
            if isinstance(exc, Sr865aCaptureError):
                raise
            partial = replace(result, completed=False, x_v=result.x_v[:4], y_v=result.y_v[:4],
                              r_v=result.r_v[:4], t_s=result.t_s[:4])
            raise Sr865aCaptureError(f"guard failed: {type(exc).__name__}: {exc}", partial) from exc
        finally:
            self.owner.in_capture = False


class Optical:
    def __init__(self, owner):
        self.owner = owner
        self.sample_active = False

    def open(self, **kwargs):
        self.owner.log.append("optical:open")

    def configure(self):
        self.owner.log.append("optical:configure")

    def set_point(self, index):
        self.owner.log.append(f"optical:set_point:{index}")

    def observe_dark(self, **kwargs):
        self.owner.log.append("optical:dark_guard")
        return {"state": {"pem": {"frequency_hz": 50027.0, "stable": True}, "source": {"emission": False}}}

    def qualify(self):
        self.owner.log.append("optical:qualify")

    def begin_sample(self):
        self.owner.log.append("optical:begin_sample")
        self.sample_active = True

    def monitor_sample(self, **kwargs):
        self.owner.log.append("optical:light_guard")
        if self.owner.fault == "power_drift":
            raise ValueError("optical power drift exceeded approved tolerance")
        if self.owner.fault == "interrupt":
            raise KeyboardInterrupt("operator interrupt")
        return {"power_w": 1e-5, "state": {"pem": {"frequency_hz": 50027.0, "stable": True},
                                          "source": {"emission": True}}}

    def end_sample(self):
        self.owner.log.append("optical:end_sample")
        self.sample_active = False

    def suspend(self):
        self.owner.log.append("optical:suspend")

    def cleanup(self, **kwargs):
        self.owner.log.append("optical:off")
        self.sample_active = False
        return {"verified": True, "source_off": True, "errors": []}

    def close(self, *, finish_pem):
        self.owner.log.append("optical:pem_finish" if finish_pem else "optical:pem_preserve")
        self.owner.log.append("optical:close")
        return {"verified": finish_pem, "reference_cleanup_pending": not finish_pem,
                "meter_cleanup_pending": False, "errors": []}


class Harness:
    def __init__(self, directory, *, fault=None):
        self.directory = Path(directory)
        self.log, self.in_capture, self.fault = [], False, fault
        self.clock = Clock()
        station_path = self.directory / "station.toml"
        diagnostic_path = self.directory / "diagnostic.toml"
        safety_path = self.directory / "lockin-safety.toml"
        for path in (station_path, diagnostic_path, safety_path):
            path.write_text("# Synthetic test configuration; no physical addresses\n", encoding="utf-8")
        document = reversed_sine_document()
        document["photonics_lockin"].update(overload_policy="record_continue", reference_unlock_policy="record_continue")
        lockin = load_photonics_lockin_config(station_path, document=document,
                                             safety_document=reversed_sine_safety_document())
        lockin = replace(lockin, safety_path=str(safety_path),
                         safety_sha256=hashlib.sha256(safety_path.read_bytes()).hexdigest())
        optical = SimpleNamespace(nkt=SimpleNamespace(backend="nkt_sdk"), pem=object(), pm=object(),
                                  points=({"wavelength_nm": 630.0},))
        self.station = SimpleNamespace(path=station_path, reference_topology="pem_xy_xx_sine", lockin=lockin,
                                       optical=optical, smu=None, cryostat=None,
                                       database_path=self.directory / "combination.sqlite",
                                       snapshot={"config_sha256": hashlib.sha256(station_path.read_bytes()).hexdigest()})
        self.config = DiagnosticConfig(
            config_path=diagnostic_path, station_path=station_path, run_name="synthetic", note="fake only",
            output_directory=self.directory / "output", optical_point_indices=(0,),
            internal_frequencies_hz=(50000.0,), include_pem_frequency=False,
            time_constants_s=(0.001,), harmonics=(1, 2), capture_duration_s=0.2,
            minimum_capture_rate_hz=1000.0, capture_timeout_s=1.0, monitor_interval_s=0.1,
            pem_frequency_observation_s=0, xx_excitation_disconnected=True, capture_buffer_available=True,
        )
        self.manager = Manager(self)
        self.xx, self.xy = Backend(self, "lockin_xx"), Backend(self, "lockin_xy")
        self.optical = Optical(self)

    def manager_factory(self, config):
        self.log.append("manager:create")
        return self.manager

    def backend_factory(self, *, role, **kwargs):
        return self.xx if role == "lockin_xx" else self.xy

    def optical_factory(self, *args, **kwargs):
        return self.optical

    def run(self, **kwargs):
        options = {"authorize_optical": True, "confirm_optical_route": True,
                   "manager_factory": self.manager_factory, "backend_factory": self.backend_factory,
                   "optical_factory": self.optical_factory, "clock": self.clock, "sleep": self.clock.sleep}
        options.update(kwargs)
        return run_diagnostic(self.config, self.station, self.directory / "run", **options)


class PemInternalDiagnosticTests(unittest.TestCase):
    def make(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(temporary.name, **kwargs)

    def test_authorization_and_physical_declarations_reject_before_instrument_io(self):
        for change in ("authorize", "route", "xx", "buffer"):
            with self.subTest(change=change):
                harness = self.make()
                kwargs = {}
                if change == "authorize":
                    kwargs["authorize_optical"] = False
                elif change == "route":
                    kwargs["confirm_optical_route"] = False
                elif change == "xx":
                    harness.config = replace(harness.config, xx_excitation_disconnected=False)
                else:
                    harness.config = replace(harness.config, capture_buffer_available=False)
                with self.assertRaisesRegex(ValueError, "requires optical authorization"):
                    harness.run(**kwargs)
                self.assertEqual(harness.log, [])
                self.assertFalse((harness.directory / "run").exists())

    def test_frequency_and_optical_index_bounds_reject_before_io(self):
        for changes in ({"internal_frequencies_hz": (48999.0,)}, {"internal_frequencies_hz": (51001.0,)},
                        {"optical_point_indices": (1,)}):
            harness = self.make()
            harness.config = replace(harness.config, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                harness.run()
            self.assertEqual(harness.log, [])

    def test_complete_matrix_restores_baseline_and_protects_before_reference_cleanup(self):
        harness = self.make()
        baseline = harness.xy.native
        result = harness.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual([row["illumination"] for row in result["captures"]], ["dark", "light", "dark", "light"])
        self.assertTrue(all(row["analysis"]["valid_for_diagnostic_analysis"] for row in result["captures"]))
        self.assertFalse(result["formal_hall_eligible"])
        self.assertTrue(result["cleanup"]["verified"])
        self.assertEqual(harness.xy.native, baseline)
        self.assertEqual(harness.xy.frequency, 43210.0)
        tail = harness.log[harness.log.index("optical:off"):]
        self.assertLess(tail.index("optical:off"), tail.index("xx:protect"))
        self.assertLess(tail.index("xx:protect"), tail.index("xy:restore_external"))
        self.assertLess(tail.index("xy:restore_external"), tail.index("optical:pem_finish"))
        self.assertLess(tail.index("optical:pem_finish"), tail.index("lockin_xy:resource_close"))
        self.assertTrue(harness.manager.closed)
        self.assertTrue(all(resource.closed for resource in harness.manager.resources))
        self.assertEqual(len(list((harness.directory / "run").glob("capture-*.csv"))), 4)
        archived = json.loads((harness.directory / "run/result.json").read_text(encoding="utf-8"))
        self.assertEqual(archived["status"], result["status"])
        self.assertEqual(len(archived["file_sha256"]), 3)

    def test_optical_drift_rejects_whole_light_capture_and_retains_partial_data(self):
        harness = self.make(fault="power_drift")
        result = harness.run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("power drift", result["error"])
        self.assertEqual(len(result["captures"]), 2)
        light = result["captures"][-1]
        self.assertEqual(light["illumination"], "light")
        self.assertEqual(len(light["capture"]["x_v"]), 4)
        self.assertFalse(light["capture"]["completed"])
        self.assertFalse(light["analysis"]["valid_for_diagnostic_analysis"])
        self.assertTrue((harness.directory / "run" / light["raw_csv"]).exists())
        self.assertTrue(result["cleanup"]["verified"])
        self.assertFalse(harness.optical.sample_active)

    def test_record_continue_keeps_overload_and_unlock_captures_invalid(self):
        for fault, reason in (("overload", "overload_recorded"), ("unlock", "reference_unlock_recorded")):
            harness = self.make(fault=fault)
            with self.subTest(fault=fault):
                result = harness.run()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(result["captures"]), 4)
                for row in result["captures"]:
                    self.assertIn(reason, row["analysis"]["exclusion_reasons"])
                    self.assertFalse(row["analysis"]["valid_for_diagnostic_analysis"])
                self.assertTrue(result["cleanup"]["verified"])

    def test_unknown_instrument_configuration_and_setting_faults_remain_fatal(self):
        for fault in ("instrument", "unknown", "configuration_latch", "settings", "xx_unknown",
                      "unknown_locked", "unknown_overload"):
            harness = self.make(fault=fault)
            with self.subTest(fault=fault):
                result = harness.run()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(result["captures"]), 1)
                self.assertFalse(result["captures"][0]["analysis"]["valid_for_diagnostic_analysis"])
                self.assertTrue(result["cleanup"]["verified"])

    def test_restore_and_resource_close_failures_do_not_certify_cleanup(self):
        for fault, expected in (("xy_restore", "xy_restore"), ("resource_close", "resource close")):
            harness = self.make(fault=fault)
            with self.subTest(fault=fault):
                result = harness.run()
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["cleanup"]["verified"])
                self.assertTrue(result["cleanup"]["manual_verification_required"])
                self.assertTrue(any(expected in error for error in result["cleanup"]["errors"]))

    def test_unverified_xx_cleanup_leaves_pem_and_xy_reference_active_or_unknown(self):
        harness = self.make(fault="xx_cleanup_protect")
        result = harness.run()
        self.assertFalse(result["cleanup"]["xx_protection_verified"])
        self.assertFalse(result["cleanup"]["xy_restored"])
        self.assertNotIn("xy:restore_external", harness.log)
        self.assertNotIn("optical:pem_finish", harness.log)
        self.assertIn("optical:pem_preserve", harness.log)
        self.assertFalse(result["cleanup"]["verified"])

    def test_capture_stop_fault_is_not_hidden_by_later_device_cleanup(self):
        harness = self.make(fault="capture_cleanup")
        result = harness.run()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup"]["verified"])
        self.assertTrue(any("capture stop" in error for error in result["cleanup"]["errors"]))

    def test_partial_open_failure_closes_first_resource_and_manager(self):
        harness = self.make(fault="partial_open")
        result = harness.run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["captures"], [])
        self.assertEqual(len(harness.manager.resources), 1)
        self.assertTrue(harness.manager.resources[0].closed)
        self.assertTrue(harness.manager.closed)
        self.assertNotIn("optical:open", harness.log)

    def test_active_database_rejects_without_opening_instruments_or_archive(self):
        harness = self.make()
        with closing(sqlite3.connect(harness.station.database_path)) as connection:
            connection.execute("CREATE TABLE combination_runs (run_id TEXT, status TEXT)")
            connection.execute("INSERT INTO combination_runs VALUES ('existing', 'active')")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "active run"):
            harness.run()
        self.assertEqual(harness.log, [])
        self.assertFalse((harness.directory / "run").exists())

    def test_interrupt_is_retained_and_cleanup_still_runs(self):
        harness = self.make(fault="interrupt")
        result = harness.run()
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["cleanup"]["verified"])
        self.assertFalse(result["captures"][-1]["capture"]["completed"])

    def test_offline_simulation_analysis_and_monitor_never_use_instrument_factories(self):
        harness = self.make()
        with patch("attodry_control.pem_internal_diagnostic._manager", side_effect=AssertionError("real VISA manager forbidden")):
            result = simulate(harness.config, harness.station, harness.directory / "simulation")
            self.assertTrue(result["simulated"])
            self.assertEqual(harness.log, [])
            directory = harness.directory / "simulation"
            self.assertTrue(analyze(directory).exists())
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(run(["monitor", "--run-directory", str(directory)]), 0)
                self.assertIn('"simulated": true', output.getvalue())
            self.assertEqual(harness.log, [])

    def test_cli_describe_and_validate_do_not_import_actual_instrument_packages(self):
        harness = self.make()
        original_import = builtins.__import__

        def without_hardware(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"pyvisa", "serial", "qcodes"}:
                raise AssertionError(f"Offline command imported {name}")
            return original_import(name, *args, **kwargs)

        with patch("attodry_control.pem_internal_diagnostic.load_diagnostic_config", return_value=harness.config), \
                patch("attodry_control.pem_internal_diagnostic.load_reviewed", return_value=harness.station), \
                patch("builtins.__import__", side_effect=without_hardware):
            for command in ("describe", "validate-config"):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(run([command, "--config", str(harness.config.config_path)]), 0)
                    self.assertIn('"formal_hall_eligible": false', output.getvalue())
        self.assertEqual(harness.log, [])

    def test_failed_run_analysis_retains_numbers_without_promoting_capture_or_mutating_raw(self):
        harness = self.make(fault="xy_restore")
        result = harness.run()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["captures"][0]["capture"]["validity"])
        source = harness.directory / "run/result.json"
        original = source.read_bytes()
        path = analyze(source.parent)
        summaries = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(summaries[0]["valid_for_diagnostic_analysis"])
        self.assertIn("run_terminal_failed", summaries[0]["exclusion_reasons"])
        self.assertIn("final_cleanup_not_verified", summaries[0]["exclusion_reasons"])
        self.assertIsNotNone(summaries[0]["r_mean_v"])
        self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
