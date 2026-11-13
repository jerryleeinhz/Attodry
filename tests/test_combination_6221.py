"""Complete mixed-detector scans, archive review and recovery with fake resources."""
from contextlib import closing
from copy import deepcopy
import json
import re
from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination, run_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_resume import build_recovery, configuration_changes
from attodry_control.combination_store import CombinationStore, open_readonly, validate_accepted_records
from attodry_control.combination_terminal import launch_text
from tests.test_keithley6221_ac import Fake6221
from tests.test_lockin_settling import Clock
from tests.test_mixed_lockin_integration import TrackingReceiverResource
from tests.test_sr830 import TrackingVisaResource, FakeResourceManager, responses

ROOT = Path(__file__).resolve().parents[1]


class SourceResource(Fake6221):
    def __init__(self, frequency):
        super().__init__()
        self.frequency, self.closed = frequency, False

    def write(self, command):
        super().write(command)
        if command.startswith("SOUR:WAVE:FREQ "):
            self.frequency["hz"] = float(command.split()[1])

    def close(self):
        self.closed = True


class Combination6221Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / "hardware.local.toml"
        self.database = self.directory / "scan.sqlite"
        self.text = (ROOT / "config/hardware.6221.example.toml").read_text(encoding="utf-8")
        self.text = (self.text.replace("CHANGE_ME_6221_VISA_ADDRESS", "FAKE::SOURCE")
            .replace("CHANGE_ME_SR830_XX_VISA_ADDRESS", "FAKE::XX")
            .replace("CHANGE_ME_SR865A_XY_VISA_ADDRESS", "FAKE::XY")
            .replace("current_status_supported = false", "current_status_supported = true"))
        self.path.write_text(self.text, encoding="utf-8")
        shutil.copyfile(ROOT / "config/lockin_safety.toml", self.directory / "lockin_safety.toml")
        self.devices()

    def devices(self):
        self.frequency = {"hz": 17.777}
        self.source = SourceResource(self.frequency)
        self.xx = TrackingVisaResource(responses(0), shared_frequency=self.frequency, name="xx")
        self.xx.responses["OFLT?"] = "9"
        self.xy = TrackingReceiverResource(self.frequency)
        self.manager = FakeResourceManager({"FAKE::SOURCE": self.source, "FAKE::XX": self.xx, "FAKE::XY": self.xy})
        self.clock = Clock()

    def config(self):
        return load_hardware_combination(self.path)

    def execute(self, config=None, recovery=None, **kwargs):
        with CombinationStore(self.database) as store:
            return run_hardware_combination(config or self.config(), store, "current-run",
                authorize_hardware=True, confirm_xy_sine_disconnected=True,
                manager_factory=lambda: self.manager, monotonic=self.clock.monotonic,
                sleep=self.clock.sleep, recovery=recovery, **kwargs)

    def events(self):
        with closing(open_readonly(self.database)) as db:
            return [(r[0], json.loads(r[1])) for r in db.execute(
                "SELECT event_type,payload_json FROM combination_events ORDER BY event_id")]

    def test_full_grid_native_pairs_no_detector_source_writes_and_cleanup(self):
        config = self.config()
        conditions = config.plan.conditions()
        self.assertEqual(len(conditions), 4)
        self.assertEqual(conditions[0]["requested"], {
            "excitation_current_rms_a": 1e-6, "excitation_frequency_hz": 17.777})
        self.assertNotIn("lockin_excitation_v_rms", json.dumps(config.snapshot))
        result = self.execute(config)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        formal = [p for kind, p in self.events() if kind == "lockin_formal_pair"]
        self.assertEqual(len(formal), 8)
        self.assertTrue(all(p["lockin_xy"]["model"] == "SR865A" for p in formal))
        for resource in (self.xx, self.xy):
            self.assertFalse(any(c.split()[0] in {"SLVL", "FREQ", "FREQINT", "SOFF", "PHAS"}
                                 for c in resource.writes), resource.writes)
        self.assertEqual(self.source.commands.count("SOUR:WAVE:INIT"), 4)
        self.assertEqual(self.source.state["OUTP:STAT"], "OFF")
        self.assertTrue(self.source.closed and self.xx.closed and self.xy.closed)
        cleanups = [p["module"] for k, p in self.events() if k == "cleanup_started"]
        self.assertEqual(cleanups, ["excitation", "lockin"])
        with closing(open_readonly(self.database)) as db:
            proof = validate_accepted_records(db, "current-run", config.snapshot)
        self.assertEqual(proof["accepted_conditions"], 4)

    def test_preview_units_and_no_second_grid(self):
        config = self.config()
        summary = launch_summary(config, self.database, "current-run")
        self.assertEqual(summary["total_conditions"], 4)
        text = launch_text(summary, width=160)
        self.assertIn("6221", text)
        self.assertIn("not measured current", text)
        self.assertIn("Arms", text)
        self.assertEqual(len(config.plan.axes[-1].points), 1)

    def test_topology_wiring_and_duplicate_resources_rejected_offline(self):
        bad = [self.text.replace('excitation_source = "keithley6221"', 'excitation_source = "sr830"'),
               self.text.replace('order = ["excitation", "lockin"]', 'order = ["lockin", "excitation"]'),
               self.text.replace('reference_source = "external_ttl"', 'reference_source = "internal"', 1),
               self.text.replace('sine_output_connected = false', 'sine_output_connected = true', 1),
               self.text.replace('"FAKE::SOURCE"', '"FAKE::XX"'),
               self.text.replace('lockin_mode = "detector"', 'lockin_mode = "frequency"'),
               self.text.replace('current_status_supported = true', 'current_status_supported = false')]
        for source in bad:
            self.path.write_text(source, encoding="utf-8")
            with self.subTest(source=source[-80:]), self.assertRaises(ValueError):
                self.config()
        self.assertEqual(self.manager.opened, [])

    def test_requested_25hz_six_rms_points_with_split_harmonics(self):
        text = (ROOT / "config/hardware.6221_25Hz.example.toml").read_text(encoding="utf-8")
        text = (text.replace("CHANGE_ME_6221_VISA_ADDRESS", "FAKE::SOURCE")
            .replace("CHANGE_ME_SR830_XX_VISA_ADDRESS", "FAKE::XX")
            .replace("CHANGE_ME_SR865A_XY_VISA_ADDRESS", "FAKE::XY")
            .replace("current_status_supported = false", "current_status_supported = true"))
        self.path.write_text(text, encoding="utf-8")
        self.assertIn('channel_label = "Vxx/b axis 04/06"', text)
        self.assertIn('channel_label = "Vxy/a axis 05/09"', text)
        safety = self.directory / "lockin_safety.toml"
        safety.write_text(re.sub(r"allowed_fixed_full_scales_v = (\[[^\]]+\])",
            lambda m: "allowed_fixed_full_scales_v = " + json.dumps(sorted(set(json.loads(m[1])) | {0.5})),
            safety.read_text()), encoding="utf-8")
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 6)
        pairs = [v for k, v in self.events() if k == "lockin_formal_pair"]
        self.assertEqual(len(pairs), 12)
        self.assertEqual({(p["harmonic"], tuple(p["selected_roles"])) for p in pairs},
                         {(1, ("xx",)), (2, ("xy",))})

    def test_source_preflight_failure_never_writes_or_opens_detectors(self):
        self.source.state["OUTP:STAT"] = "1"
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.xx.writes + self.xy.writes + self.source.commands, [])
        self.assertEqual(self.manager.opened, ["FAKE::SOURCE"])

    def test_changed_source_contract_cannot_resume_old_topology(self):
        snapshot = self.config().snapshot
        changed = deepcopy(snapshot)
        changed["excitation_source"] = "sr830"
        with self.assertRaisesRegex(ValueError, "new run"):
            configuration_changes(snapshot, changed)
        for key, value in (("inner_shield", "guard"), ("output_low_grounding", "earth")):
            changed = deepcopy(snapshot)
            changed["hardware"]["excitation"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration_changes(snapshot, changed)

    def test_failed_second_condition_resume_repeats_whole_condition(self):
        config = self.config()
        original = CombinationStore.event
        failed = [False]
        def event(store, run_id, kind, payload):
            original(store, run_id, kind, payload)
            if kind == "condition_started" and payload["condition_id"] == "condition-000001" and not failed[0]:
                failed[0] = True
                raise RuntimeError("injected before second condition")
        with patch.object(CombinationStore, "event", event):
            first = self.execute(config)
        self.assertEqual(first["status"], "failed", first)
        self.devices()
        review = build_recovery(config, self.database, "current-run", acknowledge_failure=True,
                                manual_verification=True)
        self.assertEqual(review["next_condition"]["condition_id"], "condition-000001")
        second = self.execute(config, recovery=review)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(self.source.commands.count("SOUR:WAVE:INIT"), 3)
        with closing(open_readonly(self.database)) as db:
            attempts = [tuple(r) for r in db.execute(
                "SELECT condition_id,attempt_index,status FROM combination_attempts ORDER BY condition_id,attempt_index")]
        self.assertIn(("condition-000001", 0, "rejected"), attempts)
        self.assertIn(("condition-000001", 1, "accepted"), attempts)
        self.assertEqual(sum(r[0] == "condition-000000" for r in attempts), 1)

    def test_raw_source_tamper_blocks_recovery_validation(self):
        config = self.config()
        result = self.execute(config)
        self.assertEqual(result["status"], "completed", result)
        with CombinationStore(self.database) as store:
            row = store.connection.execute("SELECT rowid,payload_json FROM combination_samples LIMIT 1").fetchone()
            raw = json.loads(row["payload_json"])
            raw["reads"][0]["status"]["after_sample"]["measurement_condition"] = 10
            store.connection.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?",
                                     (json.dumps(raw), row["rowid"]))
            store.connection.commit()
            with self.assertRaisesRegex(ValueError, "invalid raw evidence"):
                validate_accepted_records(store.connection, "current-run", config.snapshot)

    def test_current_rules_apply_before_capture_without_adding_points(self):
        rules = """
[[combination_scan.lockin_range_rules]]
name = "small_current"
match = { excitation_current_rms_a = [0.000001] }
lockin_xx_full_scale_v = 1.0
lockin_xy_full_scale_v = 0.010
[[combination_scan.lockin_range_rules]]
name = "large_current"
match = { excitation_current_rms_a = [0.000002] }
lockin_xx_full_scale_v = 1.0
lockin_xy_full_scale_v = 0.100
"""
        self.path.write_text(self.text + rules, encoding="utf-8")
        config = self.config()
        self.assertEqual(len(config.plan.conditions()), 4)
        result = self.execute(config)
        self.assertEqual(result["status"], "completed", result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)
        selected = [p["rule_name"] for k, p in self.events() if k == "lockin_range_selected"]
        self.assertEqual(selected, ["small_current", "large_current"] * 2)

    def test_native_pair_source_provenance_must_match_the_condition(self):
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        with CombinationStore(self.database) as store:
            row = store.connection.execute("SELECT rowid,payload_json FROM combination_samples LIMIT 1").fetchone()
            original = json.loads(row["payload_json"])
            for location in ("status", "pair"):
                raw = deepcopy(original)
                detector = next(read for read in raw["reads"] if read["module"] == "lockin")
                context = (detector["status"] if location == "status"
                           else detector["status"]["samples"][0])
                context["excitation_source"]["frequency_hz"] = 123.0
                store.connection.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?",
                                         (json.dumps(raw), row["rowid"]))
                store.connection.commit()
                with self.subTest(location=location), self.assertRaisesRegex(ValueError, "invalid raw evidence"):
                    validate_accepted_records(store.connection, "current-run")

    def test_source_brackets_must_cover_every_native_read(self):
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        with CombinationStore(self.database) as store:
            row = store.connection.execute("SELECT rowid,payload_json FROM combination_samples LIMIT 1").fetchone()
            raw = json.loads(row["payload_json"])
            # Both timestamps still fit the overall sample, but no longer
            # bracket the detector: a clean flag alone cannot pass recovery.
            raw["reads"][0]["status"]["captured_at_utc"] = raw["reads"][0]["status"]["after_sample"]["captured_at_utc"]
            store.connection.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?",
                                     (json.dumps(raw), row["rowid"]))
            store.connection.commit()
            with self.assertRaisesRegex(ValueError, "invalid raw evidence"):
                validate_accepted_records(store.connection, "current-run")

    def test_post_sample_source_failure_retains_primary_and_attempts_environment(self):
        station = HardwareCombinationStation(self.config())
        calls = []
        def source_failure():
            calls.append("source")
            raise OSError("source disconnected")
        def environment_failure(reads):
            calls.append("environment")
            raise RuntimeError("secondary environment failure")
        station.excitation = SimpleNamespace(readback=source_failure)
        station.cryo = SimpleNamespace(end_sample=environment_failure)
        station.set_event_sink(lambda *_: None)
        with self.assertRaisesRegex(OSError, "source disconnected") as context:
            station.end_sample([])
        self.assertEqual(calls, ["source", "environment"])
        self.assertTrue(any("secondary environment failure" in note for note in context.exception.__notes__))

    def test_latched_source_compliance_after_capture_rejects_attempt(self):
        original_query = self.xy.query
        def query(command):
            answer = original_query(command)
            if command == "SNAP? X,Y":
                self.source.state["STAT:MEAS:EVEN"] = "8"
            return answer
        self.xy.query = query
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("compliance", result["error"])
        self.assertEqual(sum(k == "condition_accepted" for k, _ in self.events()), 0)
        self.assertTrue(any(k == "lockin_formal_pair" for k, _ in self.events()))
        self.assertEqual(self.source.state["OUTP:STAT"], "OFF")

    def test_temperature_qualifies_once_per_visit_around_current_grid(self):
        from tests.test_attodry import FakeAttoDryDll
        thermal = (ROOT / "config/hardware.example.toml").read_text(encoding="utf-8")
        thermal = "[cryostat]" + thermal.split("[cryostat]", 1)[1].split("[visa]", 1)[0]
        thermal = re.sub(r"(?m)^temperature_ranges = \[.*?^\]",
                         "start_k = 2.0\nstop_k = 2.1\nstep_k = 0.1", thermal, flags=re.DOTALL)
        thermal = thermal.replace("stable_dwell_s = 30.0", "stable_dwell_s = 3.0")
        self.path.write_text(self.text.replace('order = ["excitation", "lockin"]',
            'order = ["temperature", "excitation", "lockin"]') + "\n" + thermal, encoding="utf-8")
        dll = FakeAttoDryDll()
        dll.temperature_follows_setpoint = True
        config = self.config()
        result = self.execute(config, dll=dll, authorize_cryostat=True)
        self.assertEqual(result["status"], "completed", result)
        events = self.events()
        self.assertEqual(sum(k == "temperature_qualification_completed" for k, _ in events), 2)
        self.assertEqual(sum(k == "condition_accepted" for k, _ in events), 8)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 8)


if __name__ == "__main__":
    unittest.main()
