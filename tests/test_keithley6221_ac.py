"""Command/response model; no real resource manager or instrument imports."""
from copy import deepcopy
import math
import unittest
from dataclasses import asdict

from attodry_control.excitation_6221_config import parse_excitation_6221
from attodry_control.keithley6221_ac import Keithley6221Ac, Keithley6221Error, validate_active_state


def source_config(**overrides):
    return parse_excitation_6221({
        "resource": "GPIB0::12::INSTR", "expected_idn": "MODEL 6221",
        "inner_shield": "output_low", "output_low_grounding": "floating", "phase_marker_enabled": True,
        "max_current_peak_a": .001, "compliance_voltage_v": 1.0, "offset_a": 0.0,
        "phase_marker_line": 3, "phase_marker_phase_deg": 0.0,
        "current_rms_points_a": [1e-6, 2e-6], "frequency_points_hz": [200., 300.], **overrides})


class Fake6221:
    def __init__(self):
        self.commands = []
        self.queries = []
        self.fail_write = None
        self.fail_query = None
        self.ignore_init = False
        self.state = {
            "*IDN": "KEITHLEY INSTRUMENTS INC.,MODEL 6221,123456,A03",
            "OUTP:STAT": "0", "SOUR:CURR": "0", "SOUR:CURR:COMP": "1",
            "OUTP:ISH": "OLOW", "OUTP:LTE": "0",
            "STAT:OPER:COND": "1024", "STAT:MEAS:COND": "2", "STAT:MEAS:EVEN": "0",
            "STAT:QUES:COND": "0", "SYST:ERR": '0,"No error"', "TRIG:OLIN": "2",
            "SOUR:WAVE:FUNC": "SIN", "SOUR:WAVE:RANG": "BEST",
            "SOUR:WAVE:OFFS": "0", "SOUR:WAVE:EXTR": "0", "SOUR:WAVE:EXTR:ILIN": "0",
            "SOUR:WAVE:PMAR:STAT": "0", "SOUR:WAVE:PMAR:OLIN": "3", "SOUR:WAVE:PMAR": "180",
            "SOUR:WAVE:DUR:TIME": "INF", "SOUR:WAVE:AMPL": ".001", "SOUR:WAVE:FREQ": "1000"}

    def query(self, command):
        self.queries.append(command)
        if command == self.fail_query:
            raise OSError("injected query failure")
        value = self.state[command[:-1]]
        if command == "STAT:MEAS:EVEN?":
            self.state["STAT:MEAS:EVEN"] = "0"
        return value

    def write(self, command):
        self.commands.append(command)
        if self.fail_write == command:
            raise OSError("injected write failure")
        if command == "SOUR:WAVE:ABOR":
            self.state.update({"OUTP:STAT": "0", "SOUR:CURR": "0", "STAT:OPER:COND": "1152"})
        elif command == "SOUR:WAVE:INIT":
            if not self.ignore_init:
                self.state.update({"OUTP:STAT": "1", "STAT:OPER:COND": "16"})
        elif command == "SOUR:WAVE:ARM":
            self.state["SOUR:CURR"] = "0"
        else:
            key, value = command.split(" ", 1)
            self.state[key] = value


class ConfigTests(unittest.TestCase):
    def test_output_contract_is_explicit_and_rejects_unsupported_values(self):
        for key, value in (("inner_shield", "unknown"), ("output_low_grounding", "ground"),
                           ("phase_marker_enabled", False), ("phase_marker_enabled", 1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                source_config(**{key: value})
        for key in ("inner_shield", "output_low_grounding", "phase_marker_enabled"):
            table = asdict(source_config()); del table[key]
            with self.assertRaises(ValueError):
                parse_excitation_6221(table)

    def test_grid_preserves_order_duplicates_and_units(self):
        c = source_config(current_rms_points_a=[2e-6, 1e-6, 2e-6], frequency_points_hz=[300, 200])
        self.assertEqual(len(c.grid()), 6)
        self.assertEqual(c.grid()[0], {"excitation_current_rms_a": 2e-6, "excitation_frequency_hz": 300.})
        self.assertEqual(c.grid()[3]["excitation_frequency_hz"], 200.)
        self.assertEqual(c.grid()[0], c.grid()[2])

    def test_unknown_and_missing_rejected(self):
        for table in ({}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                parse_excitation_6221(table)

    def test_bad_scalars_and_lists_rejected(self):
        cases = [{"max_current_peak_a": True}, {"max_current_peak_a": float("inf")},
                 {"current_rms_points_a": [float("nan")]}, {"frequency_points_hz": [True]},
                 {"frequency_points_hz": [0]}, {"frequency_points_hz": [100001]},
                 {"current_rms_points_a": [0]}, {"current_rms_points_a": [-1e-6]},
                 {"current_rms_points_a": []}, {"phase_marker_line": True},
                 {"phase_marker_line": 0}, {"phase_marker_phase_deg": 361},
                 {"compliance_voltage_v": .105}, {"compliance_voltage_v": 106},
                 {"resource": ""}, {"expected_idn": "MODEL 6220"}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                source_config(**case)

    def test_peak_plus_offset_checked_not_rms_only(self):
        with self.assertRaisesRegex(ValueError, "peak current"):
            source_config(max_current_peak_a=2e-6, current_rms_points_a=[1e-6], offset_a=1e-6)

    def test_public_placeholders_rejected_before_resource_access(self):
        for field, value in (("resource", "CHANGE_ME_6221"),
                             ("resource", "GPIB0::change_me::INSTR"),
                             ("expected_idn", "MODEL 6221 CHANGE_ME")):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "placeholder"):
                source_config(**{field: value})

    def test_maximum_product_rejected_before_expansion(self):
        with self.assertRaisesRegex(ValueError, "100000"):
            source_config(current_rms_points_a=[1e-6] * 501, frequency_points_hz=[200] * 200)


class DriverTests(unittest.TestCase):
    def test_connection_options_and_cleanup_never_change_grounding(self):
        for shield, command in (("output_low", "OLOW"), ("guard", "GUAR")):
            for ground, value in (("floating", "OFF"), ("earth", "ON")):
                r = Fake6221()
                d = Keithley6221Ac(r, source_config(inner_shield=shield, output_low_grounding=ground), authorize_writes=True)
                d.preflight(); d.configure(); state = d.set_point(1e-6, 200)
                self.assertIn("OUTP:ISH " + command, r.commands)
                self.assertIn("OUTP:LTE " + value, r.commands)
                self.assertTrue(validate_active_state(state, d.config, 1e-6, 200))
                count = len(r.commands)
                self.assertTrue(d.cleanup()["verified"])
                self.assertEqual(r.commands[count:], ["SOUR:WAVE:ABOR", "OUTP:STAT OFF"])

    def test_connection_write_requires_off_and_readback_before_wave_settings(self):
        r, d, _ = self.make(); d.preflight()
        r.state["OUTP:STAT"] = "1"
        with self.assertRaises(Keithley6221Error): d.configure()
        self.assertEqual(r.commands, [])
        r, d, _ = self.make(); d.preflight()
        original = r.write
        def ignore(command):
            original(command)
            if command == "OUTP:LTE OFF": r.state["OUTP:LTE"] = "ON"
        r.write = ignore
        with self.assertRaisesRegex(Keithley6221Error, "connection"): d.configure()
        self.assertNotIn("SOUR:WAVE:FUNC SIN", r.commands)
        self.assertTrue(d.cleanup()["output_off_confirmed"])

    def test_connection_faults_fail_live_and_archived_without_repair(self):
        for key, value in (("OUTP:ISH", "GUAR"), ("OUTP:LTE", "ON"), ("OUTP:ISH", "?")):
            r, d, _ = self.active(); count = len(r.commands)
            r.state[key] = value
            with self.subTest(key=key), self.assertRaises(Keithley6221Error): d.readback()
            self.assertEqual(len(r.commands), count)
        r, d, _ = self.active(); state = d.readback()
        for key in ("inner_shield_raw", "inner_shield", "output_low_earth_connected", "output_low_grounding"):
            bad = deepcopy(state); del bad[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_active_state(bad, d.config, 1e-6, 200)

    def test_partial_connection_write_failure_still_aborts_off(self):
        r, d, _ = self.make(); d.preflight(); r.fail_write = "OUTP:LTE OFF"
        with self.assertRaises(OSError): d.configure()
        self.assertNotIn("SOUR:WAVE:FUNC SIN", r.commands)
        self.assertTrue(d.cleanup()["output_off_confirmed"])

    def test_legacy_archive_has_no_invented_connection_proof_and_cannot_drive(self):
        from attodry_control.excitation_6221_config import Excitation6221Config
        r, d, _ = self.active(); state = d.readback(); old = asdict(d.config)
        for key in ("inner_shield", "output_low_grounding", "phase_marker_enabled"): del old[key]
        for key in ("inner_shield_raw", "inner_shield", "output_low_earth_connected", "output_low_grounding"): del state[key]
        legacy = Excitation6221Config(**old)
        self.assertTrue(validate_active_state(state, legacy, 1e-6, 200))
        self.assertNotIn("inner_shield", state)
        with self.assertRaises(ValueError): Keithley6221Ac(r, legacy)
        from attodry_control.combination_store import _validate_excitation_context
        snapshot = {"measurement_contract": "electrical-excitation-detectors-v1",
                    "hardware": {"excitation": old}}
        condition = {"requested": {"excitation_current_rms_a": 1e-6, "excitation_frequency_hz": 200}}
        _validate_excitation_context(state, snapshot, condition)
        snapshot["measurement_contract"] = "electrical-excitation-detectors-v2"
        with self.assertRaises((KeyError, ValueError)):
            _validate_excitation_context(state, snapshot, condition)

    def make(self, *, authorize=True, event=None):
        resource = Fake6221()
        events = []
        driver = Keithley6221Ac(resource, source_config(), event or (lambda k, p: events.append((k, p))),
                                authorize_writes=authorize, monotonic=lambda: len(resource.commands) * 1.)
        return resource, driver, events

    def active(self):
        resource, driver, events = self.make()
        driver.preflight()
        driver.configure()
        driver.set_point(1e-6, 200)
        return resource, driver, events

    def test_no_io_by_construction_and_no_write_without_authorization(self):
        resource, driver, _ = self.make(authorize=False)
        self.assertEqual(resource.queries + resource.commands, [])
        driver.preflight()
        self.assertNotIn("STAT:MEAS:EVEN?", resource.queries)
        with self.assertRaisesRegex(Keithley6221Error, "authorization"):
            driver.configure()
        self.assertEqual(resource.commands, [])
        self.assertFalse(driver.cleanup()["output_off_confirmed"])

    def test_preflight_requires_identity_exact_model_off_zero(self):
        for key, value in (("*IDN", "KEITHLEY,MODEL 6220,123,A03"), ("OUTP:STAT", "1"),
                           ("SOUR:CURR", "1e-10"), ("TRIG:OLIN", "3"),
                           ("STAT:MEAS:COND", "0"), ("STAT:MEAS:COND", "10"),
                           ("STAT:OPER:COND", "8"), ("STAT:QUES:COND", "1")):
            r, d, _ = self.make()
            r.state[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Keithley6221Error):
                d.preflight()
            self.assertEqual(r.commands, [])

    def test_rms_conversion_phase_marker_and_start_evidence(self):
        r, d, events = self.active()
        state = d.readback()
        self.assertAlmostEqual(float(r.state["SOUR:WAVE:AMPL"]), math.sqrt(2) * 1e-6)
        self.assertTrue(state["output_enabled"] and state["wave_started"])
        self.assertEqual(state["phase_marker_line"], 3)
        self.assertEqual(state["phase_marker_phase_deg"], 0)
        self.assertEqual(state["current_basis"], "source_setting_readback")
        self.assertEqual(state["actual"]["excitation_current_rms_a"], 1e-6)
        self.assertTrue(validate_active_state(state, d.config, 1e-6, 200))
        self.assertNotIn("SOUR:WAVE:ARM?", r.queries)
        self.assertNotIn("*RST", r.commands)
        self.assertNotIn("*CLS", r.commands)
        self.assertTrue(any(k == "excitation_command_attempt" for k, _ in events))

    def test_changed_point_aborts_before_settings_and_unchanged_never_writes(self):
        r, d, _ = self.active()
        count, change = len(r.commands), d.last_change_monotonic_s
        d.set_point(1e-6, 200)
        self.assertEqual(len(r.commands), count)
        self.assertEqual(d.last_change_monotonic_s, change)
        d.set_point(2e-6, 300)
        self.assertEqual(r.commands[count], "SOUR:WAVE:ABOR")
        self.assertEqual(r.commands[count + 3:count + 5], ["SOUR:WAVE:ARM", "SOUR:WAVE:INIT"])

    def test_missing_start_ack_fails_not_return_success(self):
        r, d, _ = self.make()
        d.preflight(); d.configure()
        r.ignore_init = True
        with self.assertRaisesRegex(Keithley6221Error, "start/output"):
            d.set_point(1e-6, 200)
        self.assertTrue(d.faulted)
        self.assertTrue(d.cleanup()["output_off_confirmed"])

    def test_compliance_latch_survives_cleared_current_condition(self):
        r, d, events = self.active()
        r.state["STAT:MEAS:EVEN"] = "8"
        with self.assertRaisesRegex(Keithley6221Error, "compliance"):
            d.readback()
        self.assertTrue(any(k == "excitation_raw_state" and p["measurement_event"] == 8 for k, p in events))
        with self.assertRaisesRegex(Keithley6221Error, "faulted"):
            d.set_point(2e-6, 300)

    def test_no_unknown_or_nonfinite_status_or_setting_accepted(self):
        cases = (("OUTP:STAT", "maybe"), ("STAT:OPER:COND", "65536"),
                 ("STAT:OPER:COND", "-1"), ("STAT:MEAS:COND", "18"),
                 ("SOUR:WAVE:AMPL", "nan"), ("SOUR:WAVE:FREQ", "300"),
                 ("SOUR:WAVE:OFFS", "1e-4"), ("SOUR:CURR:COMP", "2"),
                 ("SOUR:WAVE:DUR:TIME", "1e99"), ("SOUR:WAVE:PMAR:STAT", "OFF"))
        for key, value in cases:
            r, d, _ = self.active()
            r.state[key] = value
            with self.subTest(key=key), self.assertRaises(Keithley6221Error):
                d.readback()

    def test_active_dc_setpoint_change_rejects_even_with_wave_started(self):
        r, d, _ = self.active()
        r.state["SOUR:CURR"] = "1e-12"
        with self.assertRaisesRegex(Keithley6221Error, "DC zero"):
            d.readback()

    def test_runtime_phase_marker_trigger_conflict_rejects(self):
        for line in ("3", "0", "7"):
            r, d, _ = self.active()
            r.state["TRIG:OLIN"] = line
            with self.subTest(line=line), self.assertRaisesRegex(Keithley6221Error, "configuration"):
                d.readback()

    def test_error_queue_stops_and_records_raw_error(self):
        r, d, events = self.active()
        r.state["SYST:ERR"] = '-221,"Settings conflict"'
        with self.assertRaisesRegex(Keithley6221Error, "Settings conflict"):
            d.readback()
        self.assertTrue(any(p.get("raw") == '-221,"Settings conflict"' for _, p in events))

    def test_incomplete_error_queue_is_unknown_not_clear(self):
        for raw in ("0", "0,", "0, ", "not an error response"):
            r, d, _ = self.active()
            r.state["SYST:ERR"] = raw
            with self.subTest(raw=raw), self.assertRaisesRegex(Keithley6221Error, "Malformed"):
                d.readback()

    def test_partial_configuration_failure_still_cleans_and_no_replay(self):
        r, d, _ = self.make()
        d.preflight()
        r.fail_write = "SOUR:WAVE:FUNC SIN"
        with self.assertRaises(OSError):
            d.configure()
        self.assertTrue(d.owned)
        result = d.cleanup()
        self.assertTrue(result["verified"] and result["output_off_confirmed"])
        count = len(r.commands)
        self.assertEqual(d.cleanup(), result)
        self.assertEqual(len(r.commands), count)
        with self.assertRaises(Keithley6221Error):
            d.configure()

    def test_abort_failure_attempts_independent_disable_but_not_certified(self):
        r, d, _ = self.active()
        r.fail_write = "SOUR:WAVE:ABOR"
        result = d.cleanup()
        self.assertIn("OUTP:STAT OFF", r.commands)
        self.assertFalse(result["verified"])
        self.assertTrue(result["errors"])

    def test_cleanup_query_failure_does_not_infer_off_from_commands(self):
        r, d, _ = self.active()
        r.fail_query = "OUTP:STAT?"
        result = d.cleanup()
        self.assertFalse(result["verified"] or result["output_off_confirmed"])
        self.assertIsNone(result["final"])
        self.assertTrue(result["last_confirmed"]["output_enabled"])

    def test_cleanup_still_commands_when_audit_sink_fails(self):
        r, d, _ = self.active()
        d.event = lambda *a: (_ for _ in ()).throw(OSError("audit failed"))
        count = len(r.commands)
        result = d.cleanup()
        self.assertEqual(r.commands[count:], ["SOUR:WAVE:ABOR", "OUTP:STAT OFF"])
        self.assertFalse(result["verified"])

    def test_source_status_alternate_documented_formats(self):
        r, d, _ = self.active()
        r.state["STAT:OPER:COND"] = "#H10"
        r.state["STAT:MEAS:COND"] = "#B10"
        self.assertTrue(d.readback()["wave_started"])

    def test_archive_validation_rejects_tampering(self):
        _, d, _ = self.active()
        original = d.readback()
        cases = ({"current_basis": "measured"}, {"measurement_condition": 10},
                 {"wave_started": False}, {"output_enabled": 1},
                 {"error_queue": {"code": False, "raw": '0,"No error"'}},
                 {"phase_marker_line": 4}, {"current_peak_a": 2e-6},
                 {"duration_time_s": "9.9E37"},
                 {"dc_current_setpoint_a": 1e-12}, {"dc_current_setpoint_a": False},
                 {"dc_current_setpoint_a": None}, {"dc_current_setpoint_a": float("nan")},
                 {"trigger_output_line": 3}, {"trigger_output_line": True},
                 {"trigger_output_line": None},
                 {"actual": {"excitation_current_rms_a": 2e-6, "excitation_frequency_hz": 200.}})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises((ValueError, Keithley6221Error)):
                validate_active_state({**deepcopy(original), **changes}, d.config, 1e-6, 200)

    def test_numeric_overflow_sentinel_does_not_prove_infinite_duration(self):
        r, d, _ = self.active()
        r.state["SOUR:WAVE:DUR:TIME"] = "9.9E37"
        with self.assertRaisesRegex(Keithley6221Error, "duration"):
            d.readback()

    def test_live_and_archive_share_small_current_and_frequency_tolerances(self):
        for peak_delta, frequency_delta, accepted in (
                (5e-16, 5e-10, True), (5e-15, 0, False), (0, 2e-9, False)):
            with self.subTest(peak_delta=peak_delta, frequency_delta=frequency_delta):
                resource = Fake6221()
                config = source_config(current_rms_points_a=[1e-11], frequency_points_hz=[.001])
                driver = Keithley6221Ac(resource, config, authorize_writes=True)
                driver.preflight()
                driver.configure()
                state = driver.set_point(1e-11, .001)
                peak = math.sqrt(2) * 1e-11 + peak_delta
                frequency = .001 + frequency_delta
                resource.state["SOUR:WAVE:AMPL"] = repr(peak)
                resource.state["SOUR:WAVE:FREQ"] = repr(frequency)
                state.update(current_peak_a=peak, frequency_hz=frequency,
                    actual={"excitation_current_rms_a": peak / math.sqrt(2),
                            "excitation_frequency_hz": frequency})
                if accepted:
                    actual = driver.readback()
                    self.assertTrue(validate_active_state(actual, config, 1e-11, .001))
                    self.assertTrue(validate_active_state(state, config, 1e-11, .001))
                else:
                    with self.assertRaisesRegex(Keithley6221Error, "current/frequency"):
                        driver.readback()
                    with self.assertRaisesRegex(ValueError, "source setting differs"):
                        validate_active_state(state, config, 1e-11, .001)

    def test_serial_query_write_budget_and_no_driver_sleep(self):
        r, d, _ = self.make()
        d.preflight()
        self.assertEqual((len(r.queries), len(r.commands)), (12, 0))
        d.configure()
        self.assertEqual((len(r.queries), len(r.commands)), (69, 13))
        d.set_point(1e-6, 200)
        self.assertEqual((len(r.queries), len(r.commands)), (128, 18))
        before = len(r.queries), len(r.commands)
        d.readback()
        self.assertEqual((len(r.queries) - before[0], len(r.commands) - before[1]), (23, 0))
        before = len(r.queries), len(r.commands)
        d.cleanup()
        self.assertEqual((len(r.queries) - before[0], len(r.commands) - before[1]), (10, 2))

    def test_failed_query_retains_primary_when_audit_also_fails(self):
        r, d, _ = self.active()
        r.fail_query = "OUTP:STAT?"
        def audit(kind, payload):
            if kind == "excitation_query_failed":
                raise RuntimeError("audit failure")
        d.event = audit
        with self.assertRaisesRegex(OSError, "query failure") as cm:
            d.readback()
        self.assertIn("audit failure", cm.exception.__notes__[0])


if __name__ == "__main__":
    unittest.main()
