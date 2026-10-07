"""Mixed SR830 excitation/SR865A receiver E2E checks with injected resources."""
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import re
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import (
    load_hardware_combination, run_hardware_combination,
)
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.config import load_config
from attodry_control.electrical_lockin_backend import ElectricalLockinError
from attodry_control.lockin_test import run as standalone_cli
from attodry_control.sr830 import AuthorizationRequired, Sr830Error
from tests import test_combination_hardware as combination_fixture
from tests.test_electrical_lockin_backend import FakeResource as ReceiverResource
from tests.test_sr830 import FakeResourceManager


class TrackingReceiverResource(ReceiverResource):
    """Reuse native receiver responses while following the single XX source."""

    def __init__(self, frequency):
        super().__init__("SR865A", "xy")
        self.frequency = frequency
        self.closed = False
        self.clear_calls = 0
        self.responses.update({"SLVL?": "1.2", "SOFF?": "0.7", "REFM?": "1",
            "BLAZEX?": "0", "PHAS?": "37.5", "SNAP? X,Y": "1e-6,2e-7"})

    def query(self, command):
        if command in {"FREQEXT?", "FREQDET?"}:
            self.queries.append(command)
            harmonic = int(self.responses["HARM?"]) if command == "FREQDET?" else 1
            return str(self.frequency["hz"] * harmonic)
        return super().query(command)

    def close(self):
        self.closed = True

    def clear(self):
        # Standalone sweeps clear VISA queues; native fault responses remain intact.
        self.clear_calls += 1


class MixedLockinIntegrationTests(unittest.TestCase):
    def fixture(self, *, mode="excitation", zero_xy=False, current_status=True):
        fixture = combination_fixture.ElectricalCombinationTests("test_only_requested_modules_open")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before, xy = fixture.base.split("[lockin_xy]", 1)
        xy, after = xy.split("[lockin_sweep]", 1)
        xy = xy.replace('model = "SR830"', 'model = "SR865A"')
        xy = xy.replace('reserve_mode = "normal"', '')
        source = before + "[lockin_xy]" + xy + """
[lockin_xy.sr865a]
input_range_v_peak = 0.3
reference_input_impedance_ohm = 1000000.0
current_status_supported = """ + str(current_status).lower() + """
sync_output_mode = "preserve"
""" + "[lockin_sweep]" + after
        if mode == "frequency":
            source = re.sub(r"(?m)^frequency_ranges = \[.*?^\]",
                "frequency_points_hz = [100000.0, 102000.0]", source, flags=re.DOTALL)
            source = source.replace("frequency_xy_harmonics = [1, 2, 3]", "frequency_xy_harmonics = [2]")
        fixture.write_config(order=("lockin",), source=source,
                             extra=f'lockin_mode = "{mode}"\n')
        fixture.xy = TrackingReceiverResource(fixture.frequency)
        if zero_xy:
            fixture.xy.responses["SNAP? X,Y"] = "0,0"
        fixture.manager = FakeResourceManager({"FAKE::XX": fixture.xx, "FAKE::XY": fixture.xy})
        return fixture

    def execute(self, fixture, run_id="mixed", **flags):
        config = load_hardware_combination(fixture.path)
        with CombinationStore(fixture.database) as store:
            return run_hardware_combination(config, store, run_id,
                manager_factory=lambda: fixture.manager, sleep=lambda _: None,
                authorize_hardware=True, confirm_xy_sine_disconnected=True, **flags)

    def events(self, fixture):
        with closing(open_readonly(fixture.database)) as db:
            return [(row[0], json.loads(row[1])) for row in db.execute(
                "SELECT event_type,payload_json FROM combination_events ORDER BY event_id")]

    def assert_receiver_owns_no_source_writes(self, fixture, *, allow_sync_filter_write=False):
        prohibited = {"SLVL", "SOFF", "REFM", "BLAZEX", "FREQ", "FREQINT", "PHAS", "SYNC",
                      "SENS", "RMOD"}
        if allow_sync_filter_write:
            prohibited.remove("SYNC")
        self.assertFalse(any(command.split()[0] in prohibited for command in fixture.xy.writes),
                         fixture.xy.writes)
        self.assertEqual(fixture.xy.responses["SLVL?"], "1.2")
        self.assertEqual(fixture.xy.responses["SOFF?"], "0.7")
        self.assertEqual(fixture.xy.responses["REFM?"], "1")
        self.assertEqual(fixture.xy.responses["BLAZEX?"], "0")
        self.assertEqual(fixture.xy.responses["PHAS?"], "37.5")

    def test_native_config_formal_sampling_and_complete_cleanup_preserve_receiver_source(self):
        fixture = self.fixture()
        result = self.execute(fixture)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"].get("errors"))
        self.assertIn("SCAL 6", fixture.xy.writes)  # 10 mV; SR830 native code would be 20.
        self.assertIn("IRNG 1", fixture.xy.writes)  # Independent 300 mV peak input range.
        self.assertIn("OFLT 11", fixture.xy.writes)  # 300 ms; SR830 native code would be 9.
        self.assertEqual(int(fixture.xy.responses["SCAL?"]), 6)
        self.assertEqual(int(fixture.xy.responses["IRNG?"]), 1)
        self.assertEqual(int(fixture.xy.responses["OFLT?"]), 11)
        self.assertEqual(int(fixture.xy.responses["HARM?"]), 1)
        self.assertEqual(int(fixture.xx.responses["HARM?"]), 1)
        self.assertAlmostEqual(float(fixture.xx.responses["SLVL?"]), .004)
        self.assertTrue(any(command.startswith("SLVL ") for command in fixture.xx.writes))
        self.assert_receiver_owns_no_source_writes(fixture)
        self.assertTrue(fixture.xx.closed and fixture.xy.closed and fixture.manager.closed)
        rows = load_combination_rows(fixture.database, run_id="mixed")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("measured.lockin_xy_h2_amplitude_v" in row for row in rows))
        formal = [payload for name, payload in self.events(fixture) if name == "lockin_formal_pair"]
        self.assertTrue(formal)
        self.assertTrue(all(payload["settings_verified"] for payload in formal))
        self.assertTrue(all(payload["lockin_xy"]["model"] == "SR865A" for payload in formal))

    def test_undefined_phase_from_zero_xy_survives_raw_and_sqlite_round_trip(self):
        fixture = self.fixture(zero_xy=True)
        result = self.execute(fixture, "zero-xy")
        self.assertEqual(result["status"], "completed", result.get("error"))
        formal = [payload for name, payload in self.events(fixture) if name == "lockin_formal_pair"]
        self.assertTrue(formal)
        self.assertTrue(all(payload["lockin_xy"]["reading"]["phase_deg"] is None for payload in formal))
        rows = load_combination_rows(fixture.database, run_id="zero-xy")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["measured.lockin_xy_h2_amplitude_v"], 0.0)
            self.assertIsNone(row.get("measured.lockin_xy_h2_phase_deg"))
        self.assert_receiver_owns_no_source_writes(fixture)

    def test_missing_authorization_and_wiring_confirmation_reject_before_any_io(self):
        fixture = self.fixture()
        config = load_hardware_combination(fixture.path)
        for flags in ({}, {"authorize_hardware": True}):
            with self.subTest(flags=flags), CombinationStore(fixture.database) as store:
                with self.assertRaises(ValueError):
                    run_hardware_combination(config, store, "unauthorized",
                        manager_factory=lambda: fixture.manager, **flags)
        self.assertEqual(fixture.manager.opened, [])
        self.assertEqual(fixture.xx.queries + fixture.xx.writes + fixture.xy.queries + fixture.xy.writes, [])

    def test_h2_bounded_auto_uses_native_scal_ladder_and_restores_baseline(self):
        fixture = self.fixture()
        source = fixture.path.read_text(encoding="utf-8")
        source += """
[lockin_xy.harmonic_settings.h2]
sensitivity_mode = "bounded_auto"
sensitivity_full_scale_v = 0.002
autorange_min_full_scale_v = 0.002
autorange_max_full_scale_v = 0.010
autorange_target_occupancy = 0.85
autorange_stable_samples = 2
"""
        fixture.path.write_text(source, encoding="utf-8")
        result = self.execute(fixture, "native-auto")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertIn("SCAL 8", fixture.xy.writes)
        self.assertEqual({command for command in fixture.xy.writes if command.startswith("SCAL ")},
                         {"SCAL 6", "SCAL 8"})
        self.assertEqual([command for command in fixture.xy.writes if command.startswith("IRNG ")],
                         ["IRNG 1"])
        self.assertEqual(int(fixture.xy.responses["SCAL?"]), 6)
        self.assertEqual(int(fixture.xy.responses["IRNG?"]), 1)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"].get("errors"))
        self.assert_receiver_owns_no_source_writes(fixture)

    def test_bounded_auto_widens_physically_with_decreasing_native_scal_code(self):
        fixture = self.fixture()
        source = fixture.path.read_text(encoding="utf-8") + """
[lockin_xy.harmonic_settings.h2]
sensitivity_mode = "bounded_auto"
sensitivity_full_scale_v = 0.002
autorange_min_full_scale_v = 0.002
autorange_max_full_scale_v = 0.010
autorange_target_occupancy = 0.85
autorange_stable_samples = 2
"""
        source = source.replace("excitation_points_v_rms = [0.004, 0.008]",
                                "excitation_points_v_rms = [0.004, 0.008, 0.012]")
        fixture.path.write_text(source, encoding="utf-8")
        original_write = fixture.xx.write
        def write(command):
            original_write(command)
            if command == "SLVL 0.012":
                fixture.xy.responses["SNAP? X,Y"] = "0.003,0"
        fixture.xx.write = write
        result = self.execute(fixture, "native-widen")
        self.assertEqual(result["status"], "completed", result.get("error"))
        codes = [int(command.split()[1]) for command in fixture.xy.writes if command.startswith("SCAL ")]
        narrowed = codes.index(8)
        self.assertIn(6, codes[narrowed + 1:])
        self.assertEqual(set(codes), {6, 8})
        self.assertEqual(int(fixture.xy.responses["IRNG?"]), 1)
        self.assert_receiver_owns_no_source_writes(fixture)

    def test_runtime_native_and_source_drift_cannot_certify_formal_window_or_cleanup(self):
        for command, changed in (("IRNG?", "2"), ("REFZ?", "0"), ("SYNC?", "1"),
                                 ("SLVL?", "0.6"), ("SOFF?", "-0.3"), ("BLAZEX?", "2")):
            with self.subTest(command=command):
                fixture = self.fixture()
                original_write = fixture.xx.write
                def write(wire_command):
                    original_write(wire_command)
                    if wire_command == "SLVL 0.008":
                        fixture.xy.responses[command] = changed
                fixture.xx.write = write
                result = self.execute(fixture, "runtime-native-drift")
                self.assertEqual(result["status"], "failed", result.get("error"))
                lockin_cleanup = next(action for action in result["cleanup"]["actions"]
                                      if action["module"] == "lockin")["result"]
                self.assertFalse(lockin_cleanup["verified"], lockin_cleanup.get("errors"))
                self.assertTrue(result["cleanup"]["manual_verification_required"])
                commands = [wire for wire in fixture.xy.writes
                            if wire.startswith(command.removesuffix("?") + " ")]
                self.assertEqual(commands, ["IRNG 1"] if command == "IRNG?" else [])

    def standalone_config(self, fixture):
        config = load_config(fixture.path)
        return replace(config, lockin_sweep=replace(
            config.lockin_sweep, output_directory=fixture.path.parent / "sweeps"))

    def test_mixed_standalone_excitation_command_authorizes_and_keeps_old_flag_compatible(self):
        for flags in ([], ["--authorize-writes"]):
            with self.subTest(flags=flags):
                fixture = self.fixture()
                output = io.StringIO()
                with patch("attodry_control.lockin_test.load_config", return_value=self.standalone_config(fixture)), \
                        redirect_stdout(output):
                    self.assertEqual(standalone_cli(
                        ["sweep-excitation", "--config", str(fixture.path), *flags],
                        resource_manager_factory=lambda: fixture.manager), 0)
                record = json.loads(output.getvalue())
                self.assertTrue(record["completed"], record.get("error"))
                self.assertEqual(record["write_authorization"], "run_command")
                self.assertEqual(record["requested_points_v_rms"], [.004, .008])
                self.assertTrue(record["cleanup"]["verified"])
                self.assertEqual(float(fixture.xx.responses["SLVL?"]), .004)
                self.assertEqual(int(fixture.xy.responses["HARM?"]), 1)
                self.assert_receiver_owns_no_source_writes(fixture)
                self.assertTrue(fixture.xx.closed and fixture.xy.closed and fixture.manager.closed)

    def test_mixed_standalone_excitation_without_flag_still_blocks_unknown_status(self):
        fixture = self.fixture()
        fixture.xy.responses["CUROVLDSTAT?"] = "4"
        output = io.StringIO()
        with patch("attodry_control.lockin_test.load_config", return_value=self.standalone_config(fixture)), \
                redirect_stdout(output), self.assertRaisesRegex(Sr830Error, "unknown"):
            standalone_cli(["sweep-excitation", "--config", str(fixture.path)],
                           resource_manager_factory=lambda: fixture.manager)
        record = json.loads(output.getvalue())
        self.assertFalse(record["completed"])
        self.assertEqual(record["points"], [])
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])
        self.assertTrue(fixture.xx.closed and fixture.xy.closed and fixture.manager.closed)

    def test_other_mixed_standalone_sweeps_require_explicit_flag_before_factory(self):
        for command in ("sweep-frequency", "sweep-frequency-excitation"):
            with self.subTest(command=command):
                fixture = self.fixture()
                calls = []
                def factory():
                    calls.append("opened")
                    return fixture.manager
                with patch("attodry_control.lockin_test.load_config", return_value=self.standalone_config(fixture)), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                        self.assertRaisesRegex(AuthorizationRequired, "--authorize-writes"):
                    standalone_cli([command, "--config", str(fixture.path)],
                                   resource_manager_factory=factory)
                self.assertEqual(calls, [])
                self.assertEqual(fixture.manager.opened, [])
                self.assertEqual(fixture.xx.queries + fixture.xy.queries + fixture.xx.writes + fixture.xy.writes, [])

    def apply_fixture(self):
        fixture = self.fixture()
        source = fixture.path.read_text(encoding="utf-8").replace(
            'output_directory = "../run_data/commissioning"', 'output_directory = "sweeps"')
        fixture.path.write_text(source, encoding="utf-8")
        # apply-toml preflight requires the existing fixed filter/input settings.
        fixture.xy.responses["OFLT?"] = "11"
        return fixture

    def apply_arguments(self, fixture):
        return ["apply-toml", "--config", str(fixture.path), "--role", "lockin_xy",
                "--authorize-writes", "--authorize-status-latch-consumption",
                "--confirm-xy-sine-disconnected"]

    def saved_apply_records(self, fixture, outcome):
        paths = sorted((fixture.path.parent / "sweeps").glob(f"*_apply_toml_{outcome}.json"))
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def test_apply_toml_native_xy_is_authorized_preserves_source_and_is_idempotent(self):
        fixture = self.apply_fixture()
        arguments = self.apply_arguments(fixture)
        unauthorised = [argument for argument in arguments if argument != "--authorize-writes"]
        calls = []
        def factory():
            calls.append("opened")
            return fixture.manager
        with self.assertRaises(AuthorizationRequired):
            standalone_cli(unauthorised, resource_manager_factory=factory)
        self.assertEqual(calls, [])
        self.assertEqual(fixture.xx.queries + fixture.xy.queries + fixture.xx.writes + fixture.xy.writes, [])
        fixture.xy.responses.update({"REFZ?": "0", "ADVFILT?": "1", "SYNC?": "1"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(standalone_cli(arguments, resource_manager_factory=factory), 0)
        record = json.loads(output.getvalue())
        self.assertTrue(record["completed"])
        self.assertTrue(record["write_performed"])
        for wire in ("IRNG 1", "REFZ 1", "ADVFILT 0", "SYNC 0", "SCAL 6"):
            self.assertIn(wire, fixture.xy.writes)
        self.assertEqual(record["receiver_invariants"]["input_range_v_peak"], .3)
        self.assertEqual(record["receiver_invariants"]["reference_input_impedance_ohm"], 1_000_000.0)
        self.assertFalse(record["receiver_invariants"]["advanced_filter"])
        self.assertFalse(record["receiver_invariants"]["synchronous_filter"])
        self.assertIsNone(record["requested"]["reserve_mode"])
        self.assertEqual(fixture.xx.writes, [])
        self.assert_receiver_owns_no_source_writes(fixture, allow_sync_filter_write=True)
        self.assertTrue(fixture.manager.closed and fixture.xy.closed and fixture.xx.closed)
        saved = self.saved_apply_records(fixture, "completed")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["receiver_invariants"]["source_output_baseline"]["phase_shift_deg"], 37.5)

        # Reopen new fake VISA handles against the same already-applied physical state.
        previous_xx, previous_xy = fixture.xx, fixture.xy
        fixture.xx = type(previous_xx)(dict(previous_xx.responses), shared_frequency=fixture.frequency, name="xx")
        fixture.xy = TrackingReceiverResource(fixture.frequency)
        fixture.xy.responses = dict(previous_xy.responses)
        fixture.manager = FakeResourceManager({"FAKE::XX": fixture.xx, "FAKE::XY": fixture.xy})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(standalone_cli(arguments, resource_manager_factory=factory), 0)
        repeated = json.loads(output.getvalue())
        self.assertTrue(repeated["completed"])
        self.assertFalse(repeated["write_performed"])
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])
        self.assert_receiver_owns_no_source_writes(fixture)
        self.assertEqual(len(self.saved_apply_records(fixture, "completed")), 2)

    def test_apply_toml_wider_scal_cannot_excuse_native_output_overload(self):
        fixture = self.apply_fixture()
        fixture.xy.responses["LIAS?"] = str(16 | 256)
        with redirect_stdout(io.StringIO()), self.assertRaises(Sr830Error):
            standalone_cli(self.apply_arguments(fixture), resource_manager_factory=lambda: fixture.manager)
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])
        saved = self.saved_apply_records(fixture, "rejected")
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0]["completed"])
        self.assertIn("overload", saved[0]["error"])
        native = saved[0]["before"]["lockin_xy"]["native_status"]
        self.assertEqual(native["lia_status_raw"], 272)
        self.assertTrue(native["input_overload_latched"])
        self.assertTrue(native["output_scale_overload_latched"])

    def test_apply_toml_ignored_irng_keeps_failed_wire_command_in_persistent_audit(self):
        fixture = self.apply_fixture()
        original_write = fixture.xy.write
        def write(command):
            if command == "IRNG 1":
                fixture.xy.writes.append(command)
            else:
                original_write(command)
        fixture.xy.write = write
        with redirect_stdout(io.StringIO()), self.assertRaises(ElectricalLockinError):
            standalone_cli(self.apply_arguments(fixture), resource_manager_factory=lambda: fixture.manager)
        saved = self.saved_apply_records(fixture, "rejected")
        self.assertEqual(len(saved), 1)
        record = saved[0]
        self.assertFalse(record["completed"])
        self.assertTrue(record["write_performed"])
        self.assertTrue(record["manual_verification_required"])
        serialized = json.dumps(record)
        self.assertIn('"command": "IRNG 1"', serialized)
        self.assertIn('"command": "IRNG?"', serialized)
        self.assertEqual(fixture.xy.writes, ["IRNG 1"])
        self.assert_receiver_owns_no_source_writes(fixture)

    def test_apply_toml_native_query_failure_audit_survives_xy_or_xx_target(self):
        for role in ("lockin_xy", "lockin_xx"):
            with self.subTest(role=role):
                fixture = self.apply_fixture()
                fixture.xy.responses["SNAP? X,Y"] = TimeoutError("injected receiver read failure")
                arguments = self.apply_arguments(fixture)
                arguments[arguments.index("--role") + 1] = role
                with redirect_stdout(io.StringIO()), self.assertRaises(ElectricalLockinError):
                    standalone_cli(arguments, resource_manager_factory=lambda: fixture.manager)
                saved = self.saved_apply_records(fixture, "rejected")
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]["target_role"], role)
                self.assertTrue(saved[0]["manual_verification_required"])
                serialized = json.dumps(saved[0])
                self.assertIn('"command": "SNAP? X,Y"', serialized)
                self.assertIn("injected receiver read failure", serialized)
                self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])

    def test_unknown_native_flags_reject_before_setting_writes_and_keep_raw_evidence(self):
        for command, raw in (("LIAS?", "4"), ("CUROVLDSTAT?", "4"), ("*ESR?", "64")):
            with self.subTest(command=command):
                fixture = self.fixture()
                fixture.xy.responses[command] = raw
                result = self.execute(fixture, "native-fault")
                self.assertEqual(result["status"], "failed", result.get("error"))
                self.assertTrue("unknown" in result["error"] or "configuration changed" in result["error"], result["error"])
                self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])
                self.assertIn(command, fixture.xy.queries)
                self.assertFalse(any(name == "lockin_formal_pair" for name, _ in self.events(fixture)))
                raw_roles = [payload for name, payload in self.events(fixture) if name == "lockin_raw_role"]
                self.assertTrue(any(payload["role"] == "lockin_xy" for payload in raw_roles))

    def test_unavailable_current_status_cannot_certify_mixed_preflight(self):
        fixture = self.fixture(current_status=False)
        result = self.execute(fixture, "no-current-status")
        self.assertEqual(result["status"], "failed", result.get("error"))
        self.assertIn("incomplete", result["error"])
        self.assertNotIn("CUROVLDSTAT?", fixture.xy.queries)
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])

    def test_high_frequency_xx_h1_and_xy_h2_use_role_specific_capabilities(self):
        fixture = self.fixture(mode="frequency")
        result = self.execute(fixture, "high-frequency")
        self.assertEqual(result["status"], "completed", result.get("error"))
        rows = load_combination_rows(fixture.database, run_id="high-frequency")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("measured.lockin_xx_h1_amplitude_v", row)
            self.assertIn("measured.lockin_xy_h2_amplitude_v", row)
            self.assertNotIn("measured.lockin_xx_h2_amplitude_v", row)
        formal = [payload for name, payload in self.events(fixture) if name == "lockin_formal_pair"]
        self.assertTrue(formal)
        self.assertTrue(all(payload["lockin_xx"]["reading"]["harmonic"] == 1 for payload in formal))
        self.assertTrue(all(payload["lockin_xy"]["reading"]["harmonic"] == 2 for payload in formal))
        self.assertIn("FREQ 100000", fixture.xx.writes)
        self.assertIn("FREQ 102000", fixture.xx.writes)
        self.assertEqual(fixture.frequency["hz"], 17.777)
        self.assertNotIn("HARM 2", fixture.xx.writes)
        self.assert_receiver_owns_no_source_writes(fixture)

    def test_unsupported_xx_h2_rejects_offline_even_when_xy_h2_is_supported(self):
        fixture = self.fixture(mode="frequency")
        source = fixture.path.read_text(encoding="utf-8")
        source = source.replace("frequency_xx_harmonics = [1]", "frequency_xx_harmonics = [2]")
        source = source.replace("skip_unsupported_harmonics = true", "skip_unsupported_harmonics = false")
        fixture.path.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SR830"):
            load_hardware_combination(fixture.path)
        self.assertEqual(fixture.manager.opened, [])
        self.assertEqual(fixture.xx.queries + fixture.xy.queries + fixture.xx.writes + fixture.xy.writes, [])
