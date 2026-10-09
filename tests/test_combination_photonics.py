"""Whole photonics coordinator with synthetic transports; no instrument connection."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from optical_helpers import Clock
from attodry_control.analysis_observations import channel_quality
from photonics_lockin_helpers import (
    FakeManager, PHOTONICS_LOCKIN_TOML, PHOTONICS_LOCKIN_SAFETY_TOML,
    reversed_sine_document, reversed_sine_safety_document)
from attodry_control.combination_analysis import load_combination_rows, select_series
from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination, run_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.combination_terminal import launch_text
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.optical_scan import OpticalScan
from attodry_control.pem import Pem, SimulatedPemTransport
from attodry_control.pm100d import Pm100d, SimulatedPmResource

ROOT = Path(__file__).resolve().parents[1]


def toml_document(document):
    lines = []
    def encoded(value):
        if isinstance(value, list):
            return "[" + ", ".join(encoded(item) for item in value) + "]"
        if isinstance(value, dict):
            return "{ " + ", ".join(key + " = " + encoded(item) for key, item in value.items()) + " }"
        return json.dumps(value)
    def emit(table, prefix=""):
        if prefix:
            lines.append("[" + prefix + "]")
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(key + " = " + encoded(value))
        for key, value in table.items():
            if isinstance(value, dict):
                emit(value, (prefix + "." if prefix else "") + key)
    emit(document)
    return "\n".join(lines) + "\n"


class SharedVisaManager(FakeManager):
    """Closing a default PyVISA manager invalidates every instrument session."""

    def __init__(self, document, log):
        super().__init__(document)
        self.log = log
        self.created_resources = list(self.resources.values())
        for resource in self.created_resources:
            self.guard(resource)

    def guard(self, resource):
        for operation in ("query", "write"):
            original = getattr(resource, operation)
            def guarded(command, original=original, operation=operation):
                if self.closed or resource.closed:
                    raise OSError("InvalidSession: shared VISA manager closed")
                self.log.append(("visa_" + operation, getattr(resource, "role", "pm100d"), command))
                return original(command)
            setattr(resource, operation, guarded)

    def close(self):
        if self.closed:
            return
        self.log.append(("manager_close",))
        for resource in self.created_resources:
            resource.close()
        self.closed = True


class PhotonicsCombinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "hardware.local.toml"
        self.database = self.directory / "scan.sqlite"
        (self.directory / "photonics_lockin_safety.toml").write_text(
            PHOTONICS_LOCKIN_SAFETY_TOML, encoding="utf-8")
        optical = (ROOT / "config/optical_source_current_simulation.toml").read_text(encoding="utf-8")
        optical = optical.replace('[nkt_source]', '[nkt_source]\ndll_path = "fake.dll"\nport = "FAKE_NKT"\naddress = 1\nexpected_serial = "SIMULATION"')
        optical = optical.replace('[nkt_varia]', '[nkt_varia]\naddress = 2\nexpected_serial = "SIMULATION"')
        optical = optical.replace('backend = "simulation"', 'backend = "nkt_sdk"', 1)
        optical = optical.replace('[pem]\nbackend = "simulation"', '[pem]\nbackend = "serial"\nport = "FAKE_PEM"\nexpected_idn = "Hinds PEM 200 controller SIMULATION"')
        optical = optical.replace('[pm100d]\nbackend = "simulation"', '[pm100d]\nbackend = "visa"\nresource = "FAKE::PM"\nexpected_serial = "SIMULATION"\nexpected_sensor_serial = "SIMULATION"')
        # Give power scan real wavelength variation without changing target pairing.
        optical = optical.replace('wavelength_nm = 633.0', 'wavelength_nm = 650.0', 1)
        self.base = '[project]\nmode = "hardware"\ndatabase_path = "scan.sqlite"\n' + PHOTONICS_LOCKIN_TOML + optical
        self.write_config()
        self.clock = Clock()
        self.manager = FakeManager()
        self.log = []
        self.backend = self.pem = self.meter = None
        self.formal_queries = []
        for resource in self.manager.resources.values():
            resource.on_write = lambda r, c: self.log.append(("lockin", r.role, c))
            resource.on_query = self.on_lockin_query

    def write_config(self, order=("optical", "lockin"), source=None, extra=""):
        self.path.write_text((source or self.base) + '\n[combination_scan]\nbackend = "hardware"\norder = ' + json.dumps(order) + '''
reference_topology = "pem_xx_xy"
lockin_mode = "excitation"
samples_per_condition = 2
repeats = 1
run_name = "synthetic_photonics"
note = "No hardware"
''' + extra, encoding="utf-8")

    def on_lockin_query(self, resource, command):
        if command.startswith("SNAP?") and self.backend and self.backend.source["emission_state"] == 3:
            source = float(self.manager.resources["FAKE::XX"].responses["SLVL?"])
            self.formal_queries.append((resource.role, source, self.backend.source["level_pct"],
                                        self.backend.filter["lower_edge_nm"]))
            if resource.model == "SR865A":
                resource.responses["SNAP? X,Y"] = f"{source / 10},{source / 20}"

    def backend_factory(self, config):
        self.backend = SimulatedNkt(config)
        # Synthetic injected backend exercises gated hardware-mode coordinator.
        self.backend.is_hardware = True
        original = self.backend.set_emission
        def emission(enabled):
            self.log.append(("laser", enabled))
            return original(enabled)
        self.backend.set_emission = emission
        return self.backend

    def pem_factory(self, config):
        transport = SimulatedPemTransport()
        transport.frequency_hz = 50027
        original = transport.exchange
        def exchange(command):
            self.log.append(("pem", command, float(self.manager.resources["FAKE::XX"].responses["SLVL?"])))
            return original(command)
        transport.exchange = exchange
        self.pem = Pem(config, transport, is_hardware=True, authorize_writes=True,
                       clock=self.clock, sleep=self.clock.sleep)
        return self.pem

    def meter_factory(self, config):
        resource = SimulatedPmResource(lambda: 0.0001 * (self.backend.source["level_pct"] / 40) ** 2
            if self.backend.source["emission_state"] == 3 else 0)
        self.meter = Pm100d(config, resource, is_hardware=True, authorize_settings=True,
                           authorize_measurement=True, clock=self.clock)
        return self.meter

    def execute(self, run_id="test", **kwargs):
        config = load_hardware_combination(self.path)
        flags = dict(authorize_hardware=True, confirm_xy_sine_disconnected=True,
                     authorize_optical=True, confirm_optical_route=True)
        flags.update(kwargs)
        with CombinationStore(self.database) as store:
            return run_hardware_combination(config, store, run_id,
                manager_factory=lambda: self.manager, sleep=self.clock.sleep, monotonic=self.clock,
                optical_backend_factory=self.backend_factory, pem_factory=self.pem_factory,
                meter_factory=self.meter_factory, **flags)

    def events(self):
        with closing(open_readonly(self.database)) as connection:
            return [(r[0], json.loads(r[1])) for r in connection.execute(
                "SELECT event_type,payload_json FROM combination_events ORDER BY event_id")]

    def test_loading_preview_no_io_and_model_parameters_visible(self):
        config = load_hardware_combination(self.path)
        summary = launch_summary(config, self.database, "preview")
        text = launch_text(summary)
        self.assertEqual(summary["lockin"]["roles"]["lockin_xx"]["model"], "SR865A")
        self.assertEqual(summary["lockin"]["harmonics_by_role"], {"xx": [1, 3], "xy": [2]})
        self.assertEqual(summary["total_conditions"], 6)
        self.assertIn("optical", text)
        self.assertEqual(self.manager.opened, [])
        self.assertIsNone(self.backend)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["describe-hardware", "--config", str(self.path)]), 0)
        self.assertIn("optical", output.getvalue())

    def test_launch_summary_resolves_independent_reference_unlock_policy_without_io(self):
        source = self.base.replace('[photonics_lockin]',
                                   '[photonics_lockin]\nreference_unlock_policy = "record_continue"')
        self.write_config(source=source)
        summary = launch_summary(load_hardware_combination(self.path), self.database, "preview")
        self.assertEqual(summary["lockin"]["reference_unlock_policy"], "record_continue")
        self.assertEqual(summary["lockin"]["overload_policy"], "abort")
        self.assertEqual(self.manager.opened, [])

    def test_both_orders_fresh_electrical_power_brackets_and_cleanup(self):
        # Separate fixtures/resources ensure both orders exercise complete startup.
        for order in (("optical", "lockin"), ("lockin", "optical")):
            with self.subTest(order=order):
                if self.backend is not None:
                    self.manager = FakeManager()
                    for resource in self.manager.resources.values():
                        resource.on_write = lambda r, c: self.log.append(("lockin", r.role, c))
                        resource.on_query = self.on_lockin_query
                self.log.clear()
                self.write_config(order)
                run_id = "-".join(order)
                result = self.execute(run_id)
                self.assertEqual(result["status"], "completed", result["error"] or result["cleanup"]["errors"])
                rows = load_combination_rows(self.database, run_id=run_id)
                self.assertEqual(len(rows), 12)
                self.assertEqual(result["overload_summary"]["formal_pairs"], 24)
                self.assertTrue(all("measured.lockin_xx_h3_x_v" in row and
                    "measured.lockin_xy_h2_x_v" in row and
                    "measured.lockin_xy_h3_x_v" not in row for row in rows))
                for row in rows:
                    self.assertAlmostEqual(row["measured.lockin_xx_h1_x_v"],
                                           row["requested.lockin_excitation_v_rms"] / 10)
                    for channel in ("lockin_xx_h1_x_v", "lockin_xx_h3_amplitude_v", "lockin_xy_h2_amplitude_v"):
                        self.assertEqual(channel_quality(row, "measured." + channel), ("clear", ()))
                groups = select_series(rows, x="measured.optical_power_w",
                    y="measured.lockin_xx_h1_x_v", group_by=("requested.lockin_excitation_v_rms",),
                    filters={"requested.optical_wavelength_nm": 633.0, "sample_index": 0})
                self.assertEqual([len(g.rows) for g in groups], [2, 2])
                # Bracket enrichment is persisted in formal sample payloads, not prior raw events.
                with closing(open_readonly(self.database)) as connection:
                    samples = [json.loads(r[0]) for r in connection.execute(
                        "SELECT payload_json FROM combination_samples WHERE run_id=?", (run_id,))]
                for sample in samples:
                    optical = next(r for r in sample["reads"] if r["module"] == "optical")
                    self.assertEqual([r["phase"] for r in optical["status"]["sample_brackets"]],
                                     ["before_electrical", "formal", "after_electrical"])
                self.assertEqual(self.backend.source["emission_state"], 0)
                self.assertEqual(self.pem.output_command, False)
                self.assertTrue(self.meter.closed and self.pem.closed and self.manager.closed)
                self.assertTrue(all(not c.startswith("FREQ ") for r in self.manager.resources.values() for c in r.writes))
                self.assertNotIn("HARM 3", self.manager.resources["FAKE::XY"].writes)
                # Every PEM adjustment holds the source low, even optical-inner resets.
                self.assertTrue(all(item[2] <= .004 for item in self.log
                    if item[0] == "pem" and item[1].startswith((":MOD:AMP ", ":SYS:PEMO "))))
                disable_index = next(i for i, item in enumerate(self.log) if item[:2] == ("pem", ":SYS:PEMO 0"))
                final_low = max(i for i, item in enumerate(self.log[:disable_index])
                    if item[:2] == ("lockin", "lockin_xx") and item[2] == "SLVL 0.004")
                self.assertTrue(any(item == ("laser", False) for item in self.log[:final_low]))

    def test_authorization_before_resource_factories(self):
        for flags in ({"authorize_optical": False}, {"confirm_optical_route": False},
                      {"authorize_hardware": False}, {"confirm_xy_sine_disconnected": False}):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.execute(**flags)
        self.assertEqual(self.manager.opened, [])
        self.assertIsNone(self.backend)

    def test_target_deviation_keeps_both_orders_accepted_and_actual_power_analysis(self):
        stabilize = OpticalScan._stabilize
        def drifting(engine, row, point, wavelength, deadline, cfg, **kwargs):
            backend = engine.nkt.backend
            engine.meter.resource.power_provider = lambda: (
                0.0001 * (backend.source["level_pct"] / 40) ** 2
                if backend.source["emission_state"] == 3 else 0)
            result = stabilize(engine, row, point, wavelength, deadline, cfg, **kwargs)
            engine.meter.resource.power_provider = lambda: cfg.target_power_w * 0.8
            return result
        for order in (("optical", "lockin"), ("lockin", "optical")):
            with self.subTest(order=order):
                self.manager = FakeManager()
                for resource in self.manager.resources.values():
                    resource.on_query = self.on_lockin_query
                    resource.on_write = lambda r, c: self.log.append(("lockin", r.role, c))
                source = self.base.replace('[optical_scan]',
                    '[optical_scan]\ntarget_deviation_policy = "record_continue"')
                self.write_config(order, source=source)
                run_id = "actual-power-" + "-".join(order)
                with patch.object(OpticalScan, "_stabilize", drifting):
                    result = self.execute(run_id)
                self.assertEqual(result["status"], "completed", result)
                self.assertTrue(result["cleanup"]["clean"])
                rows = load_combination_rows(self.database, run_id=run_id)
                self.assertEqual(len(rows), 12)
                for row in rows:
                    target = row["requested.optical_target_power_w"]
                    self.assertEqual(row["actual.optical_target_power_w"], target)
                    self.assertAlmostEqual(row["measured.optical_power_w"], target * 0.8)
                    self.assertFalse(row["optical_power_quality.target_in_tolerance"])
                    self.assertTrue(row["optical_power_quality.target_deviation_continued"])
                    self.assertTrue(row["accepted"])
                    status = row["status.optical"]
                    self.assertEqual(status["power_bracket_summary"]["target_deviation_count"], 3)
                    self.assertEqual([e["phase"] for e in status["sample_brackets"]],
                                     ["before_electrical", "formal", "after_electrical"])
                    self.assertEqual(status["power_target_w"], target)
                    self.assertTrue(all(channel_quality(row, "measured." + channel) == ("clear", ())
                        for channel in ("lockin_xx_h1_x_v", "lockin_xy_h2_x_v")))
                groups = select_series(rows, x="measured.optical_power_w",
                    y="measured.lockin_xy_h2_x_v", filters={"sample_index": 0,
                    "requested.optical_wavelength_nm": 633.0}, sort_x=True)
                self.assertTrue(groups)
                self.assertTrue(all("requested.optical_target_power_w" not in dict(g.group) for g in groups))
                self.assertEqual(sum(len(g.rows) for g in groups), 4)
                self.assertEqual(self.backend.source["emission_state"], 0)

    def test_cartesian_grid_two_wavelengths_three_powers_runs_complete_plan(self):
        doc = tomllib.loads(self.path.read_text(encoding="utf-8"))
        doc["power_feedback"]["target_mapping"] = "cartesian"
        doc["nkt_run"]["points"] = doc["nkt_run"]["points"][:2]
        doc["optical_scan"]["target_deviation_policy"] = "record_continue"
        self.path.write_text(toml_document(doc), encoding="utf-8")
        cfg = load_hardware_combination(self.path)
        self.assertEqual(len(cfg.plan.conditions()), 12)
        self.assertEqual(len(cfg.optical.nkt.points), 2)
        self.assertEqual(len(cfg.optical.points), 6)
        self.assertIn("Expanded points: 6", launch_text(launch_summary(cfg, self.database, "grid")))
        result = self.execute("cartesian-grid")
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database, run_id="cartesian-grid")
        self.assertEqual(len(rows), 24)
        combinations = {(r["requested.optical_wavelength_nm"], r["requested.optical_target_power_w"],
                         r["requested.lockin_excitation_v_rms"]) for r in rows}
        self.assertEqual(len(combinations), 12)
        self.assertTrue(all(r["accepted"] for r in rows))

    def test_reversed_sine_reference_both_orders_and_daily_cli_preserve_xy_output(self):
        for order in (("optical", "lockin"), ("lockin", "optical")):
            with self.subTest(order=order):
                self.write_config(order)
                doc = tomllib.loads(self.path.read_text(encoding="utf-8"))
                reversed_doc = reversed_sine_document()
                for key in ("lockin_xx", "lockin_xy", "photonics_lockin", "lockin_sweep"):
                    doc[key] = reversed_doc[key]
                doc["combination_scan"]["reference_topology"] = "pem_xy_xx_sine"
                self.path.write_text(toml_document(doc), encoding="utf-8")
                (self.directory / "photonics_lockin_safety.toml").write_text(
                    toml_document(reversed_sine_safety_document()), encoding="utf-8")
                self.manager = FakeManager(doc)
                xy = self.manager.resources["FAKE::XY"]
                xy.responses["SLVL?"], xy.responses["BLAZEX?"] = "0.2", "0"
                for resource in self.manager.resources.values():
                    resource.on_query = self.on_lockin_query
                    resource.on_write = lambda r, c: self.log.append(("lockin", r.role, c))
                config = load_hardware_combination(self.path)
                summary = launch_summary(config, self.database, "preview")
                self.assertIn("PEM -> XY -> XX (XY SINE OUT)", launch_text(summary))
                self.assertEqual(summary["lockin"]["reference_output"]["amplitude_v_rms"], 0.2)
                run_id = "reversed-" + "-".join(order)
                if order == ("optical", "lockin"):
                    result = self.execute(run_id, confirm_xy_sine_disconnected=False)
                    self.assertEqual(result["status"], "completed", result)
                else:
                    from attodry_control.combination_hardware import run_hardware_combination as real_run
                    def injected(config, store, run_id, **kwargs):
                        return real_run(config, store, run_id, manager_factory=lambda: self.manager,
                            sleep=self.clock.sleep, monotonic=self.clock,
                            optical_backend_factory=self.backend_factory, pem_factory=self.pem_factory,
                            meter_factory=self.meter_factory, **kwargs)
                    with patch("attodry_control.combination_hardware.run_hardware_combination", side_effect=injected):
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(cli(["run", "--config", str(self.path), "--run-id", run_id,
                                "--authorize-optical", "--confirm-optical-route"]), 0)
                rows = load_combination_rows(self.database, run_id=run_id)
                self.assertEqual(len(rows), 12)
                self.assertTrue(all("measured.lockin_xx_h1_x_v" in row and
                    "measured.lockin_xy_h2_x_v" in row for row in rows))
                self.assertEqual(xy.responses["SLVL?"], "0.2")
                self.assertEqual(self.manager.resources["FAKE::XX"].responses["RSLP?"], "0")
                self.assertFalse(any(command.startswith(("SLVL ", "SOFF ", "REFM ", "BLAZEX ", "FREQ ")) for command in xy.writes))
                self.assertEqual(self.backend.source["emission_state"], 0)
                self.assertFalse(self.pem.output_command)

    def test_duplicate_resources_and_internal_frequency_rejected_offline(self):
        for source in (self.base.replace('resource = "FAKE::PM"', 'resource = "FAKE::XX"'),
                       self.base.replace('port = "FAKE_PEM"', 'port = "FAKE_NKT"')):
            self.write_config(source=source)
            with self.assertRaises(ValueError):
                load_hardware_combination(self.path)
        self.write_config()
        self.path.write_text(self.path.read_text().replace('lockin_mode = "excitation"', 'lockin_mode = "frequency"'))
        with self.assertRaisesRegex(ValueError, "PEM owns"):
            load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])

    def test_formal_power_drift_rejects_raw_attempt_and_stops(self):
        original = HardwareCombinationStation.read
        def drift(station, module):
            reading = original(station, module)
            if module == "lockin":
                self.meter.resource.power_provider = lambda: 0.000049
            return reading
        with patch.object(HardwareCombinationStation, "read", drift):
            result = self.execute()
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertIn("Formal optical power", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertTrue(any(k == "lockin_point_samples" for k, _ in self.events()))
        self.assertEqual(self.backend.source["emission_state"], 0)
        self.assertEqual(self.pem.output_command, False)

    def test_reference_mismatch_cannot_start_laser(self):
        factory = self.pem_factory
        def wrong_frequency(config):
            pem = factory(config)
            pem.transport.frequency_hz = 50040
            return pem
        self.pem_factory = wrong_frequency
        for order in (("optical", "lockin"), ("lockin", "optical")):
            with self.subTest(order=order):
                self.manager = FakeManager()
                self.write_config(order, source=self.base.replace('[0.004, 0.006]', '[0.006, 0.004]'))
                result = self.execute("mismatch-" + "-".join(order))
                self.assertEqual(result["status"], "failed", result["error"])
                self.assertIn("PEM frequency differs", result["error"])
                self.assertFalse(any(command == ("emission", True) for command in self.backend.commands))
                self.assertNotIn("SLVL 0.006", self.manager.resources["FAKE::XX"].writes)

    def shared_visa_setup(self):
        doc = tomllib.loads(self.path.read_text(encoding="utf-8"))
        reversed_doc = reversed_sine_document()
        for key in ("lockin_xx", "lockin_xy", "photonics_lockin", "lockin_sweep"):
            doc[key] = reversed_doc[key]
        doc["lockin_sweep"]["excitation_points_v_rms"] = [.004]
        doc["combination_scan"]["reference_topology"] = "pem_xy_xx_sine"
        self.path.write_text(toml_document(doc), encoding="utf-8")
        (self.directory / "photonics_lockin_safety.toml").write_text(
            toml_document(reversed_sine_safety_document()), encoding="utf-8")
        self.manager = SharedVisaManager(doc, self.log)
        xy = self.manager.resources["FAKE::XY"]
        xy.responses["SLVL?"], xy.responses["BLAZEX?"] = "0.2", "0"
        for resource in self.manager.resources.values():
            resource.on_query = self.on_lockin_query
            resource.on_write = lambda r, c: self.log.append(("lockin", r.role, c))
        factory = self.meter_factory
        def shared_meter(config):
            meter = factory(config)
            meter.manager = self.manager
            self.manager.created_resources.append(meter.resource)
            self.manager.guard(meter.resource)
            return meter
        self.meter_factory = shared_meter

    def assert_shared_cleanup_protection(self, result):
        actions = {action["module"]: action for action in result["cleanup"]["actions"]}
        self.assertTrue(actions["optical"]["result"]["off_confirmed"])
        self.assertTrue(actions["optical"]["result"]["meter_cleanup_pending"])
        self.assertTrue(actions["lockin"]["result"]["source_protection_verified"], actions)
        self.assertTrue(actions["lockin"]["result"]["reference_output_preserved"], actions)
        self.assertFalse(any(command.startswith(("SLVL ", "SOFF ", "REFM "))
                             for command in self.manager.resources["FAKE::XY"].writes))
        closing = next(i for i, item in enumerate(self.log) if item == ("manager_close",))
        final_low = max(i for i, item in enumerate(self.log[:closing])
                        if item == ("visa_query", "lockin_xx", "SLVL?"))
        self.assertTrue(any(item == ("laser", False) for item in self.log[:final_low]))
        self.assertFalse(any(item[0] in ("visa_query", "visa_write") for item in self.log[closing:]))
        self.assertEqual(self.backend.source["emission_state"], 0)
        self.assertTrue(self.manager.closed)
        self.assertTrue(self.meter.closed)
        final = next(payload for kind, payload in self.events() if kind == "optical_final_cleanup")
        self.assertFalse(final["meter_cleanup_pending"])

    def test_shared_visa_success_closes_manager_after_source_protection(self):
        self.shared_visa_setup()
        result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "completed", result)
        self.assert_shared_cleanup_protection(result)

    def test_shared_visa_configure_frequency_failure_protects_source_before_manager_close(self):
        self.shared_visa_setup()
        self.manager.resources["FAKE::XY"].responses["FREQEXT?"] = "50040"
        result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("external references disagree", result["error"])
        self.assertFalse(any(command == ("emission", True) for command in self.backend.commands))
        with closing(open_readonly(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM combination_attempts").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM combination_samples").fetchone()[0], 0)
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assert_shared_cleanup_protection(result)

    def test_shared_visa_formal_sample_failure_preserves_cleanup_sessions(self):
        self.shared_visa_setup()
        sample = HardwareCombinationStation.read
        def fail_read(station, module):
            if module == "lockin":
                raise OSError("synthetic formal acquisition failure")
            return sample(station, module)
        with patch.object(HardwareCombinationStation, "read", fail_read):
            result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("synthetic formal acquisition failure", result["error"])
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assert_shared_cleanup_protection(result)

    def test_shared_visa_pm_close_failure_occurs_after_source_protection(self):
        self.shared_visa_setup()
        factory = self.meter_factory
        def close_failure(config):
            meter = factory(config)
            def failed_close():
                self.log.append(("pm_close_failed",))
                raise OSError("synthetic PM close failure")
            meter.resource.close = failed_close
            return meter
        self.meter_factory = close_failure
        result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("synthetic PM close failure", str(result["cleanup"]["errors"]))
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        actions = {action["module"]: action for action in result["cleanup"]["actions"]}
        self.assertTrue(actions["optical"]["result"]["off_confirmed"])
        self.assertTrue(actions["lockin"]["result"]["source_protection_verified"], actions)
        self.assertTrue(actions["lockin"]["result"]["reference_output_preserved"], actions)
        first_close = next(i for i, item in enumerate(self.log) if item == ("pm_close_failed",))
        self.assertTrue(any(item == ("visa_query", "lockin_xx", "SLVL?")
                            for item in self.log[:first_close]))
        self.assertEqual(self.backend.source["emission_state"], 0)
        final = next(payload for kind, payload in self.events() if kind == "optical_final_cleanup")
        self.assertFalse(final["verified"])
        self.assertFalse(final["meter_cleanup_pending"])
        self.assertIn("synthetic PM close failure", str(final["errors"]))

    def test_unverified_excitation_cleanup_preserves_pem_reference(self):
        self.write_config(source=self.base.replace('[0.004, 0.006]', '[0.006]'))
        xx = self.manager.resources["FAKE::XX"]
        original = HardwareCombinationStation.cleanup
        def cleanup(station, module, failed):
            if module == "lockin":
                def failed_write(resource, command):
                    if command.startswith("SLVL "):
                        raise OSError("synthetic source cleanup disconnected")
                xx.on_write = failed_write
            return original(station, module, failed)
        with patch.object(HardwareCombinationStation, "cleanup", cleanup):
            result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup"]["clean"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(self.backend.source["emission_state"], 0)
        self.assertNotIn(":SYS:PEMO 0", self.pem.transport.commands)
        self.assertTrue(self.pem.closed)
        self.assertTrue(any(k == "optical_final_cleanup" and p["reference_left_active_or_unknown"]
                            for k, p in self.events()))

    def test_partial_optical_preflight_does_not_disable_uncertified_reference(self):
        factory = self.meter_factory
        def wrong_identity(config):
            meter = factory(config)
            meter.resource.values["*IDN?"] = "THORLABS,PM100D,WRONG,v1"
            return meter
        self.meter_factory = wrong_identity
        result = self.execute()
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertNotIn(":SYS:PEMO 0", self.pem.transport.commands)
        self.assertTrue(self.pem.closed)
        self.assertFalse(result["cleanup"]["clean"])

    def test_sub_4mv_source_preserves_role_specific_limits(self):
        (self.directory / "photonics_lockin_safety.toml").write_text(
            PHOTONICS_LOCKIN_SAFETY_TOML.replace('cleanup_source_voltage_v = 0.004',
                                               'cleanup_source_voltage_v = 0.0001'), encoding="utf-8")
        self.write_config(source=self.base.replace('excitation_points_v_rms = [0.004, 0.006]',
                                                  'excitation_points_v_rms = [0.0001]'))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result["error"] or result["cleanup"]["errors"])
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["actual.lockin_excitation_v_rms"] == .0001 for row in rows))
        self.assertEqual(float(self.manager.resources["FAKE::XX"].responses["SLVL?"]), .0001)


if __name__ == "__main__":
    unittest.main()
