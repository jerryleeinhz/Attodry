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

    def range_configuration(self):
        return self.text.replace(
            'current_rms_points_a = [0.000001, 0.000002]',
            'current_rms_ranges_a = [{ min = 0.000001, max = 0.000002, scale = "linear", points = 2 }]'
        ).replace(
            'frequency_points_hz = [17.777, 200.0]',
            'frequency_ranges_hz = [{ min = 17.777, max = 200.0, scale = "linear", points = 2 }]')

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

    def test_ranges_preserve_exact_plan_and_recovery_settings(self):
        explicit = self.config()
        ranges = self.range_configuration()
        self.path.write_text(ranges, encoding="utf-8")
        generated = self.config()
        self.assertEqual(generated.plan.conditions(), explicit.plan.conditions())
        self.assertEqual(generated.snapshot["axes"], explicit.snapshot["axes"])
        self.assertEqual(generated.snapshot["hardware"]["excitation"],
                         explicit.snapshot["hardware"]["excitation"])
        self.assertEqual(configuration_changes(explicit.snapshot, generated.snapshot), [])
        self.assertNotEqual(generated.snapshot["config_sha256"], explicit.snapshot["config_sha256"])
        for changed in (ranges.replace('max = 0.000002', 'max = 0.000003'),
                        ranges.replace('points = 2', 'points = 3', 1)):
            self.path.write_text(changed, encoding="utf-8")
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "new run"):
                configuration_changes(explicit.snapshot, self.config().snapshot)
        self.assertEqual(self.manager.opened, [])

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
        config = self.config()
        self.assertEqual(config.excitation.current_rms_points_a,
                         (0.5e-6, 2.5e-6, 4.5e-6, 6.5e-6, 8.5e-6, 10.5e-6))
        self.assertEqual(config.excitation.frequency_points_hz, (25.0,))
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

    def test_six_nanamp_points_quantize_before_write_and_validate_in_analysis(self):
        import math
        import struct
        from attodry_control.combination_analysis import load_combination_snapshot
        text = self.text.replace('current_rms_points_a = [0.000001, 0.000002]',
            'current_rms_points_a = [1e-9, 11e-9, 21e-9, 31e-9, 41e-9, 51e-9]')
        text = text.replace('frequency_points_hz = [17.777, 200.0]', 'frequency_points_hz = [25.0]')
        self.path.write_text(text, encoding="utf-8")
        query = self.source.query
        def rounded(command):
            value = query(command)
            return format(struct.unpack("f", struct.pack("f", float(value)))[0], ".6e") if command == "SOUR:WAVE:AMPL?" else value
        self.source.query = rounded
        config = self.config()
        result = self.execute(config)
        self.assertEqual(result["status"], "completed", result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 6)
        rows = load_combination_snapshot(self.database).rows
        self.assertEqual(len(rows), 6)
        for row, target in zip(rows, config.excitation.current_rms_points_a):
            self.assertEqual(row["requested.excitation_current_rms_a"], target)
            self.assertEqual(row["excitation_programming.requested_current_rms_a"], target)
            self.assertEqual(row["actual.excitation_current_rms_a"], row["status.excitation"]["current_peak_a"]/math.sqrt(2))
        self.assertEqual(len([c for c in self.source.commands if c.startswith("SOUR:WAVE:AMPL ")]), 6)
        self.assertEqual(len([v for k, v in self.events() if k == "excitation_amplitude_quantized"]), 6)

    def test_preview_marks_quantized_duplicate_coordinates_without_merging_conditions(self):
        import contextlib
        import io
        from attodry_control.combination_cli import run
        text = self.text.replace('current_rms_points_a = [0.000001, 0.000002]',
                                 'current_rms_points_a = [21e-9, 21.00001e-9]')
        self.path.write_text(text, encoding="utf-8")
        config = self.config()
        summary = launch_summary(config, self.database, "preview")
        points = summary["excitation_amplitude_programming"]
        self.assertEqual(summary["total_conditions"], 4)
        self.assertEqual(points[0]["same_effective_source_point_indices"], [0, 1])
        self.assertEqual(points[2]["same_effective_source_point_indices"], [2, 3])
        self.assertNotEqual(points[0]["requested_current_rms_a"], points[1]["requested_current_rms_a"])
        self.assertIn("Programmed A peak", launch_text(summary, width=180))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run(["describe-hardware", "--config", str(self.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["excitation_amplitude_programming"], points)
        self.assertEqual(self.manager.opened, [])
        self.assertFalse(self.database.exists())
        result = self.execute(config)
        self.assertEqual(result["status"], "completed", result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)

    def test_31_log_frequencies_with_seven_digit_readback_reach_analysis(self):
        import sqlite3
        from attodry_control.combination_analysis import load_combination_snapshot
        from attodry_control.commissioning_6221_analysis import load_6221_sweep
        text = self.text.replace('current_rms_points_a = [0.000001, 0.000002]',
                                 'current_rms_points_a = [10e-9]')
        text = text.replace('frequency_points_hz = [17.777, 200.0]',
            'frequency_ranges_hz = [{ min = 17, max = 7000, scale = "log", points = 31 }]')
        text = text.replace('xx_harmonics = [1, 2]', 'xx_harmonics = [1]')
        text = text.replace('xy_harmonics = [1, 2]', 'xy_harmonics = [2]')
        self.path.write_text(text, encoding="utf-8")
        query = self.source.query
        def rounded(command):
            value = query(command)
            return format(float(value), '.6e') if command == 'SOUR:WAVE:FREQ?' else value
        self.source.query = rounded
        config = self.config()
        self.assertEqual(self.execute(config)["status"], "completed")
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 31)
        rows = load_combination_snapshot(self.database).rows
        self.assertEqual(len(rows), 31)
        for row, requested in zip(rows, config.excitation.frequency_points_hz):
            self.assertEqual(row["requested.excitation_frequency_hz"], requested)
            self.assertEqual(row["actual.excitation_frequency_hz"], float(format(requested, '.6e')))
        self.assertEqual(rows[1]["actual.excitation_frequency_hz"], 20.77801)
        self.assertEqual(load_6221_sweep(self.database, "current-run").metadata["selected_channel_rows"], 62)
        self.assertEqual(len([c for c in self.source.commands if c.startswith('SOUR:WAVE:FREQ ')]),31)
        # Make coordinates internally consistent but wrong for the target.
        with closing(sqlite3.connect(self.database)) as db, db:
            identity, raw = db.execute("SELECT rowid,payload_json FROM combination_samples "
                                      "WHERE condition_id='condition-000001'").fetchone()
            sample = json.loads(raw)
            sample['actual']['excitation_frequency_hz'] = 207.7801
            source = next(r for r in sample['reads'] if r['module'] == 'excitation')
            source['actual']['excitation_frequency_hz'] = 207.7801
            for state in (source['status'],source['status']['after_sample']):
                state['frequency_hz'] = 207.7801
                state['actual']['excitation_frequency_hz'] = 207.7801
            db.execute('UPDATE combination_samples SET payload_json=? WHERE rowid=?', (json.dumps(sample),identity))
        with closing(open_readonly(self.database)) as db, self.assertRaisesRegex(ValueError,'source setting differs'):
            validate_accepted_records(db, "current-run")
        with self.assertRaisesRegex(ValueError,'source setting differs'):
            load_combination_snapshot(self.database)

    def test_analysis_and_recovery_both_reject_tampered_quantization(self):
        import sqlite3
        from attodry_control.combination_analysis import load_combination_snapshot
        self.assertEqual(self.execute()["status"], "completed")
        with closing(sqlite3.connect(self.database)) as db, db:
            identity, raw = db.execute("SELECT rowid,payload_json FROM combination_samples LIMIT 1").fetchone()
            sample = json.loads(raw)
            source = next(r for r in sample["reads"] if r["module"] == "excitation")
            source["status"]["amplitude_quantization"]["programmed_current_peak_a"] *= 10
            db.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?", (json.dumps(sample), identity))
        with closing(open_readonly(self.database)) as db, self.assertRaises(ValueError):
            validate_accepted_records(db, "current-run")
        with self.assertRaisesRegex(ValueError, "quantization"):
            load_combination_snapshot(self.database)

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
        # The first segment used literal points. The next process may use an
        # equivalent ranges spelling, retaining the original accepted prefix.
        self.path.write_text(self.range_configuration(), encoding="utf-8")
        recovered_config = self.config()
        self.assertEqual(configuration_changes(config.snapshot, recovered_config.snapshot), [])
        review = build_recovery(recovered_config, self.database, "current-run", acknowledge_failure=True,
                                manual_verification=True)
        self.assertEqual(review["next_condition"]["condition_id"], "condition-000001")
        second = self.execute(recovered_config, recovery=review)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(self.source.commands.count("SOUR:WAVE:INIT"), 3)
        with closing(open_readonly(self.database)) as db:
            attempts = [tuple(r) for r in db.execute(
                "SELECT condition_id,attempt_index,status FROM combination_attempts ORDER BY condition_id,attempt_index")]
        self.assertIn(("condition-000001", 0, "rejected"), attempts)
        self.assertIn(("condition-000001", 1, "accepted"), attempts)
        self.assertEqual(sum(r[0] == "condition-000000" for r in attempts), 1)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)

    def failed_historical_relock(self):
        """Replay the old second-point LIAS8/current0 plus delayed stop ACK."""
        from attodry_control.external_lockin_points import ExternalLockinPointSession
        from attodry_control.keithley6221_ac import Keithley6221Ac
        self.source.state['STAT:MEAS:COND'] = '0'
        xy_query, source_query = self.xy.query, self.source.query
        fault = [False]
        def query(command):
            if command == 'LIAS?' and self.source.commands.count('SOUR:WAVE:INIT') == 2:
                # Return the history only at the post-settle qualification read.
                if self.clock.now >= 4.5 * 3:
                    fault[0] = True
                    return '8'
            return xy_query(command)
        def source(command):
            if command == 'STAT:OPER:COND?' and fault[0] and self.source.state['OUTP:STAT'] in ('0', 'OFF'):
                return '16'
            return source_query(command)
        self.xy.query, self.source.query = query, source
        def old_stop(driver, audit):
            state = driver._core()
            return state
        with patch.object(ExternalLockinPointSession, '_requalify_reference', lambda _: None), \
                patch.object(Keithley6221Ac, '_confirm_stopped', old_stop):
            result = self.execute()
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('lockin_xy reference unlocked', result['error'])
        return result

    def test_reviewed_relock_cleanup_is_resolved_fresh_without_manual_claim(self):
        from attodry_control.combination_resume import digest
        self.failed_historical_relock()
        config = self.config()
        with patch('attodry_control.combination_resume.RELOCK_PREDECESSOR_SETS',
                   {digest(config.snapshot['source_sha256'])}):
            review = build_recovery(config, self.database, 'current-run', acknowledge_failure=True)
            self.assertIsNotNone(review['review']['deferred_cleanup_resolution'], review)
            self.assertTrue(review['review']['ready'])
            self.assertFalse(review['review']['manual_verification'])
            self.assertTrue(review['review']['cleanup_review']['manual_verification_required'])
            self.devices()
            result = self.execute(config, recovery=review)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 3)
        events = self.events()
        resolved = [v for k, v in events if k == 'recovery_cleanup_resolution_verified']
        self.assertEqual(len(resolved), 1)
        self.assertFalse(resolved[0]['source']['output_enabled'])
        self.assertFalse(resolved[0]['original_cleanup_rewritten'])

    def test_unresolved_source_state_still_prevents_resume_output(self):
        from attodry_control.combination_resume import digest
        self.failed_historical_relock()
        config = self.config()
        with patch('attodry_control.combination_resume.RELOCK_PREDECESSOR_SETS',
                   {digest(config.snapshot['source_sha256'])}):
            review = build_recovery(config, self.database, 'current-run', acknowledge_failure=True)
            self.assertTrue(review['review']['ready'], review)
            self.devices()
            self.source.state['OUTP:STAT'] = '1'
            result = self.execute(config, recovery=review)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('SOUR:WAVE:INIT', self.source.commands)

    def test_relock_cleanup_resolution_rejects_unknown_and_secondary_faults(self):
        from attodry_control.combination_resume import digest, external_relock_cleanup_resolution
        from attodry_control.combination_store import recovery_parent_state
        self.failed_historical_relock()
        config = self.config()
        with closing(open_readonly(self.database)) as db, patch(
                'attodry_control.combination_resume.RELOCK_PREDECESSOR_SETS',
                {digest(config.snapshot['source_sha256'])}):
            state = recovery_parent_state(db, 'current-run')
            parent = json.loads(state['executions'][-1]['plan_json'])
            self.assertIsNotNone(external_relock_cleanup_resolution(state, parent, db))
            for key, value in [('output_enabled', True), ('measurement_event', 8),
                               ('measurement_condition', 8), ('interlock_raw', '0'),
                               ('dc_current_setpoint_a', 1e-9), ('operation_condition', 256),
                               ('output_enabled', 0), ('compliance_voltage_v', 99)]:
                with self.subTest(key=key, value=value):
                    altered = deepcopy(state)
                    cleanup = json.loads(altered['run']['cleanup_json'])
                    cleanup['actions'][0]['result']['final'][key] = value
                    altered['run']['cleanup_json'] = json.dumps(cleanup)
                    self.assertIsNone(external_relock_cleanup_resolution(altered, parent, db))
            altered = deepcopy(state)
            altered['run']['error'] = 'OSError: communication failed'
            self.assertIsNone(external_relock_cleanup_resolution(altered, parent, db))
            review = build_recovery(config, self.database, 'current-run', acknowledge_failure=True)
            review['review']['deferred_cleanup_resolution']['status_sha256'] = '0' * 64
            self.devices()
            with self.assertRaisesRegex(ValueError, 'Deferred cleanup'):
                self.execute(config, recovery=review)
            self.assertFalse(self.source.commands)

    def test_five_nanoamp_points_relock_with_latched_history_every_point(self):
        text = self.text.replace('current_rms_points_a = [0.000001, 0.000002]',
            'current_rms_ranges_a = [{ min = 1e-9, max = 51e-9, scale = "linear", points = 5 }]')
        text = text.replace('frequency_points_hz = [17.777, 200.0]', 'frequency_points_hz = [25.0]')
        self.path.write_text(text, encoding='utf-8')
        write, query = self.source.write, self.xy.query
        pending = [False]
        def source_write(command):
            write(command)
            if command == 'SOUR:WAVE:INIT':
                pending[0] = True
        def xy_query(command):
            if command == 'LIAS?' and pending[0]:
                pending[0] = False
                return '8'
            return query(command)
        self.source.write, self.xy.query = source_write, xy_query
        result = self.execute()
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['cleanup']['clean'])
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 5)
        proofs = [v for k, v in self.events() if k == 'external_reference_requalification']
        self.assertEqual(len(proofs), 5)
        self.assertTrue(all(p['completed'] and p['transition_unlock_observed'] for p in proofs))
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 5)

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
