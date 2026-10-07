import math
import unittest

from attodry_control.models import LockinRole
from attodry_control.sr865a import AuthorizationRequired, Sr865a, Sr865aError
from attodry_control import sr865a_settings as settings


class FakeResource:
    def __init__(self):
        self.responses = {
            "*IDN?": "Stanford_Research_Systems,SR865A,000111,v1.23\n",
            "IVMD?": "0", "RSRC?": "1", "RTRG?": "1", "REFZ?": "1",
            "HARM?": "2", "OFLT?": "12", "SCAL?": "6", "IRNG?": "2",
            "OFSL?": "3", "ADVFILT?": "0", "SYNC?": "0", "ISRC?": "1",
            "ICPL?": "0", "IGND?": "0", "PHAS?": "0", "SLVL?": "0.004",
            "SOFF?": "0", "REFM?": "0", "BLAZEX?": "2",
            "FREQEXT?": "50027", "FREQINT?": "1000", "FREQDET?": "100054",
            "SNAP? X,Y": "0.003,0.004", "LIAS?": "0", "ERRS?": "0",
            "*ESR?": "0", "CUROVLDSTAT?": "0",
        }
        self.events = []
        self.writes = []
        self.ignore_writes = False
        self.fail_write = False
        self.closed = False

    def query(self, command):
        self.events.append(("query", command))
        response = self.responses[command]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            return response.pop(0)
        return response

    def write(self, command):
        self.events.append(("write", command))
        self.writes.append(command)
        if self.fail_write:
            raise OSError("injected write failure")
        if not self.ignore_writes:
            name, value = command.split(" ", 1)
            self.responses[name + "?"] = value

    def close(self):
        self.closed = True


class Sr865aMappingTests(unittest.TestCase):
    def test_tables_round_trip_and_model_specific_direction(self):
        for values, encoder, decoder in (
            (settings.TIME_CONSTANTS_S, settings.time_constant_code, settings.time_constant_seconds),
            (settings.SENSITIVITIES_V, settings.sensitivity_code, settings.sensitivity_full_scale_v),
            (settings.INPUT_RANGES_V_PEAK, settings.input_range_code, settings.input_range_v_peak),
        ):
            for index, value in enumerate(values):
                self.assertEqual(encoder(value), index)
                self.assertEqual(decoder(index), value)
        self.assertEqual(settings.time_constant_code(1), 12)
        self.assertEqual(settings.time_constant_seconds(10), 0.1)
        self.assertEqual(settings.sensitivity_code(1), 0)
        self.assertEqual(settings.sensitivity_code(1e-9), 27)

    def test_discrete_settings_reject_boolean_nonfinite_and_nearest_guess(self):
        for encoder in (settings.time_constant_code, settings.sensitivity_code, settings.input_range_code):
            for bad in (True, False, math.nan, math.inf, -math.inf, "1", 0.12345):
                with self.subTest(encoder=encoder, value=bad), self.assertRaises(ValueError):
                    encoder(bad)
        for decoder in (settings.time_constant_seconds, settings.sensitivity_full_scale_v, settings.input_range_v_peak):
            for bad in (True, -1, 999, 1.0):
                with self.subTest(decoder=decoder, value=bad), self.assertRaises(ValueError):
                    decoder(bad)

    def test_frequency_and_harmonic_boundaries(self):
        self.assertEqual(settings.validate_harmonic_frequency(3, 50027), 150081)
        self.assertEqual(settings.validate_harmonic_frequency(99, 0.001), 0.099)
        self.assertLess(settings.validate_harmonic_frequency(2, 1999999), 4e6)
        for harmonic, frequency in ((0, 10), (100, 10), (True, 10), (2, 2e6),
                                    (1, 4e6), (1, 0.0009), (1, math.nan), (1, True)):
            with self.subTest(harmonic=harmonic, frequency=frequency), self.assertRaises(ValueError):
                settings.validate_harmonic_frequency(harmonic, frequency)


class Sr865aAdapterTests(unittest.TestCase):
    def setUp(self):
        self.resource = FakeResource()
        self.driver = Sr865a(self.resource, LockinRole.XX)

    def verify_identity(self):
        self.driver.query_identity()

    def test_firmware_version_prefix_accepts_actual_uppercase_v_and_retains_strict_identity(self):
        self.resource.responses["*IDN?"] = "Stanford_Research_Systems,SR865A,SYNTHETIC,V1.51"
        self.assertEqual(self.driver.query_identity(), self.resource.responses["*IDN?"])
        self.assertEqual(self.resource.writes, [])
        for identity in ("Stanford_Research_Systems,SR865,SYNTHETIC,V1.51",
                         "Other,SR865A,SYNTHETIC,V1.51", "Stanford_Research_Systems,SR865A,,V1.51",
                         "Stanford_Research_Systems,SR865A,SYNTHETIC,V1", "Stanford_Research_Systems,SR865A,SYNTHETIC,V1.51,extra"):
            self.resource.responses["*IDN?"] = identity
            with self.subTest(identity=identity), self.assertRaises(Sr865aError):
                self.driver.query_identity()

    def sample(self):
        return self.driver.read_sample(consume_status_latches=True, current_status_supported=True)

    def test_constructor_and_unauthorized_write_perform_no_io(self):
        self.assertEqual(self.resource.events, [])
        for call in (
            lambda: self.driver.set_time_constant(1),
            lambda: self.driver.set_sensitivity(0.01),
            lambda: self.driver.set_input_range(0.1),
            lambda: self.driver.set_harmonic(3),
            lambda: self.driver.configure_reference("internal"),
        ):
            with self.assertRaises(AuthorizationRequired):
                call()
        self.assertEqual(self.resource.events, [])

    def test_authorized_setter_requires_prior_identity(self):
        with self.assertRaisesRegex(Sr865aError, "identity"):
            self.driver.set_time_constant(1, authorized=True)
        self.assertEqual(self.resource.events, [])

    def test_wrong_identity_and_malformed_identity_reject_before_writes(self):
        for bad in ("Stanford_Research_Systems,SR830,000111,v1.23",
                    "Stanford_Research_Systems,SR865A_BAD,000111,v1.23",
                    "Stanford_Research_Systems,SR865A,,v1.23",
                    "prefixStanford_Research_Systems,SR865A,000111,v1.23",
                    "Stanford_Research_Systems,SR865A,000111,unknown"):
            self.resource.responses["*IDN?"] = bad
            with self.subTest(bad=bad), self.assertRaises(Sr865aError):
                self.verify_identity()
            with self.assertRaises(Sr865aError):
                self.driver.set_time_constant(1, authorized=True)
        self.assertEqual(self.resource.writes, [])

    def test_settings_preserve_voltage_units_filter_and_output_metadata(self):
        result = self.driver.read_settings()
        self.assertEqual((result.time_constant_s, result.sensitivity_full_scale_v,
                          result.input_range_v_peak), (1.0, 0.01, 0.1))
        self.assertFalse(result.advanced_filter)
        self.assertFalse(result.synchronous_filter)
        self.assertEqual(result.source_dc_mode, "common")
        self.assertEqual(result.source_offset_v, 0)
        self.assertEqual(result.source_amplitude_definition, "differential_rms_into_50_ohm_loads")
        self.assertEqual(result.sync_output_mode, "unipolar_sync")
        self.assertEqual(result.raw[0].response, self.resource.responses["*IDN?"])
        self.assertEqual(self.resource.writes, [])

    def test_current_input_is_not_mislabeled_as_voltage(self):
        self.resource.responses["IVMD?"] = "1"
        self.verify_identity()
        for call in (self.driver.read_settings, self.sample,
                     lambda: self.driver.set_sensitivity(0.01, authorized=True),
                     lambda: self.driver.set_input_range(0.1, authorized=True)):
            self.verify_identity()
            with self.assertRaisesRegex(Sr865aError, "Current-input"):
                call()
        self.assertEqual(self.resource.writes, [])

    def test_physical_setters_use_independent_tables_and_verify(self):
        self.verify_identity()
        self.assertEqual(self.driver.set_time_constant(1, authorized=True), 1)
        self.assertEqual(self.driver.set_sensitivity(0.001, authorized=True), 0.001)
        self.assertEqual(self.driver.set_input_range(0.3, authorized=True), 0.3)
        self.assertEqual(self.resource.writes, ["OFLT 12", "SCAL 9", "IRNG 1"])
        for command in self.resource.writes:
            position = self.resource.events.index(("write", command))
            query = command.split()[0] + "?"
            self.assertEqual(self.resource.events[position - 1], ("query", query))
            self.assertEqual(self.resource.events[position + 1], ("query", query))

    def test_ignored_setting_fails_and_revokes_verified_identity(self):
        self.verify_identity()
        self.resource.ignore_writes = True
        with self.assertRaisesRegex(Sr865aError, "readback"):
            self.driver.set_time_constant(3, authorized=True)
        with self.assertRaisesRegex(Sr865aError, "identity"):
            self.driver.set_sensitivity(0.001, authorized=True)
        self.assertEqual(self.resource.writes, ["OFLT 13"])

    def test_malformed_prewrite_state_stops_before_setting(self):
        for bad in ("999", "1.0", "nan", ""):
            self.resource.responses["OFLT?"] = bad
            self.verify_identity()
            with self.subTest(bad=bad), self.assertRaises(Sr865aError):
                self.driver.set_time_constant(1, authorized=True)
        self.assertEqual(self.resource.writes, [])

    def test_external_reference_requires_explicit_inputs_and_never_writes_frequency(self):
        self.verify_identity()
        self.driver.configure_reference("external_ttl", edge="rising",
                                        input_impedance_ohm=1e6, authorized=True)
        self.assertEqual(self.resource.writes, ["REFZ 1", "RTRG 1", "RSRC 1"])
        self.assertFalse(any(event[1].startswith("FREQ") for event in self.resource.events))

    def test_invalid_reference_plan_has_zero_io(self):
        for kwargs in ({"source": "external_ttl", "edge": "rising"},
                       {"source": "external_ttl", "edge": "guess", "input_impedance_ohm": 50},
                       {"source": "external_ttl", "edge": "rising", "input_impedance_ohm": True},
                       {"source": "internal", "edge": "rising"}, {"source": "dual"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.driver.configure_reference(**kwargs, authorized=True)
        self.assertEqual(self.resource.events, [])

    def test_harmonic_uses_actual_external_frequency(self):
        self.verify_identity()
        self.assertEqual(self.driver.set_harmonic(3, authorized=True), 3)
        self.assertEqual(self.resource.writes, ["HARM 3"])
        self.assertNotIn(("query", "FREQINT?"), self.resource.events)

    def test_harmonic_rejects_actual_boundary_before_write(self):
        self.verify_identity()
        self.resource.responses["FREQEXT?"] = "2000000"
        with self.assertRaisesRegex(ValueError, "4 MHz"):
            self.driver.set_harmonic(2, authorized=True)
        self.assertEqual(self.resource.writes, [])

    def test_harmonic_detects_drift_after_write_without_retry_or_clipping(self):
        self.verify_identity()
        self.resource.responses["FREQEXT?"] = ["1999999", "2000000"]
        with self.assertRaisesRegex(ValueError, "4 MHz"):
            self.driver.set_harmonic(2, authorized=True)
        self.assertEqual(self.resource.writes, ["HARM 2"])

    def test_snapshot_uses_xy_and_labels_derived_and_sequential_values(self):
        result = self.sample()
        self.assertAlmostEqual(result.amplitude_v, 0.005)
        self.assertAlmostEqual(result.phase_deg, 53.13010235415598)
        self.assertEqual(result.amplitude_phase_source, "derived_from_simultaneous_xy")
        self.assertTrue(result.frequencies_are_sequential)
        self.assertTrue(result.status.valid)
        self.assertEqual(result.reference_frequency_hz, 50027)
        self.assertEqual(result.detection_frequency_hz, 100054)
        self.assertEqual(result.captured_at_utc.utcoffset().total_seconds(), 0)
        commands = [record.command for record in result.raw]
        self.assertIn("SNAP? X,Y", commands)
        self.assertNotIn("SNAPD?", commands)
        self.assertNotIn("SNAP? 1,2,3,4,9", commands)

    def test_zero_xy_has_undefined_phase(self):
        self.resource.responses["SNAP? X,Y"] = "0,0"
        self.assertIsNone(self.sample().phase_deg)

    def test_snapshot_rejects_truncation_nonfinite_and_overflow(self):
        for bad in ("1", "1,2,3", "nan,0", "1,inf", "oops,1", "1.7e308,1.7e308"):
            self.resource.responses["SNAP? X,Y"] = bad
            with self.subTest(bad=bad), self.assertRaises(Sr865aError):
                self.sample()
            self.assertTrue(any(r.command == "SNAP? X,Y" and r.response == bad for r in self.driver.audit))

    def test_inconsistent_or_unsupported_reference_sample_is_rejected(self):
        self.resource.responses["FREQDET?"] = "50027"
        with self.assertRaisesRegex(Sr865aError, "Detection frequency"):
            self.sample()
        self.resource.responses["RSRC?"] = "2"
        with self.assertRaisesRegex(Sr865aError, "Dual/chop"):
            self.sample()

    def test_latches_do_not_claim_present_lock(self):
        status = self.driver.read_status(consume_status_latches=True)
        self.assertIsNone(status.locked)
        self.assertIsNone(status.input_overload)
        self.assertIsNone(status.valid)
        self.assertEqual(status.lia_status_raw, 0)
        self.assertNotIn(("query", "CUROVLDSTAT?"), self.resource.events)

    def test_nondestructive_status_does_not_consume_latches(self):
        status = self.driver.read_status(consume_status_latches=False, current_status_supported=True)
        self.assertTrue(status.locked)
        self.assertIsNone(status.instrument_error)
        self.assertIsNone(status.valid)
        self.assertEqual(self.resource.events, [("query", "CUROVLDSTAT?")])

    def test_historical_unlock_rejects_even_when_currently_locked(self):
        self.resource.responses["LIAS?"] = "8"
        status = self.driver.read_status(consume_status_latches=True, current_status_supported=True)
        self.assertTrue(status.locked)
        self.assertTrue(status.reference_unlock_latched)
        self.assertFalse(status.valid)

    def test_input_and_output_overloads_remain_distinct(self):
        self.resource.responses["CUROVLDSTAT?"] = "16"
        status = self.driver.read_status(consume_status_latches=True, current_status_supported=True)
        self.assertTrue(status.input_overload)
        self.assertFalse(status.output_scale_overload)
        self.assertFalse(status.valid)
        self.resource.responses["CUROVLDSTAT?"] = "256"
        status = self.driver.read_status(consume_status_latches=True, current_status_supported=True)
        self.assertFalse(status.input_overload)
        self.assertTrue(status.output_scale_overload)

    def test_unknown_or_filter_status_bits_cannot_be_accepted(self):
        for command, raw in (("LIAS?", "4"), ("LIAS?", "32"),
                             ("CUROVLDSTAT?", "4"), ("ERRS?", "4")):
            self.resource.responses.update({"LIAS?": "0", "ERRS?": "0", "CUROVLDSTAT?": "0"})
            self.resource.responses[command] = raw
            with self.subTest(command=command):
                self.assertFalse(self.driver.read_status(consume_status_latches=True,
                                                        current_status_supported=True).valid)

    def test_standard_command_error_is_not_hidden_by_zero_errs(self):
        self.resource.responses["*ESR?"] = "32"
        status = self.driver.read_status(consume_status_latches=True, current_status_supported=True)
        self.assertEqual(status.error_status_raw, 0)
        self.assertTrue(status.instrument_error)
        self.assertFalse(status.valid)

    def test_power_on_and_front_panel_changes_invalidate_acquisition(self):
        for raw, field in ((128, "power_on_latched"), (64, "configuration_changed_latched")):
            self.resource.responses["*ESR?"] = str(raw)
            with self.subTest(raw=raw):
                status = self.sample().status
                self.assertTrue(getattr(status, field))
                self.assertFalse(status.instrument_error)
                self.assertFalse(status.valid)

    def test_invalid_status_flags_reject_before_sample_io(self):
        for consume, current in ((1, False), (False, 1), (None, True), ("true", False)):
            with self.subTest(consume=consume, current=current), self.assertRaises(ValueError):
                self.driver.read_sample(consume_status_latches=consume,
                                        current_status_supported=current)
        self.assertEqual(self.resource.events, [])

    def test_actual_detection_limit_is_strict_even_with_rounding_tolerance(self):
        self.resource.responses.update({"HARM?": "1", "FREQEXT?": "3999999", "FREQDET?": "4000000"})
        with self.assertRaisesRegex(Sr865aError, "Detection frequency"):
            self.sample()

    def test_zero_actual_detection_is_not_accepted_with_low_frequency_tolerance(self):
        self.resource.responses.update({"HARM?": "1", "FREQEXT?": "0.001", "FREQDET?": "0"})
        with self.assertRaisesRegex(Sr865aError, "Detection frequency"):
            self.sample()

    def test_out_of_range_readbacks_revoke_identity(self):
        for command, value, reader in (
            ("FREQEXT?", "4000001", self.driver.read_external_frequency),
            ("HARM?", "100", self.driver.read_harmonic),
            ("SLVL?", "5", self.driver.read_sine_output),
            ("SOFF?", "6", self.driver.read_source_offset),
            ("LIAS?", "65536", lambda: self.driver.read_status(consume_status_latches=True)),
        ):
            self.verify_identity()
            previous = self.resource.responses[command]
            self.resource.responses[command] = value
            with self.subTest(command=command), self.assertRaises(Sr865aError):
                reader()
            with self.assertRaisesRegex(Sr865aError, "identity"):
                self.driver.set_time_constant(1, authorized=True)
            self.resource.responses[command] = previous
        self.assertEqual(self.resource.writes, [])

    def test_status_does_not_silently_retry_unsupported_current_query(self):
        self.resource.responses["CUROVLDSTAT?"] = TimeoutError("unsupported or timed out")
        with self.assertRaises(Sr865aError):
            self.driver.read_status(consume_status_latches=True, current_status_supported=True)
        self.assertEqual(self.resource.events, [("query", "CUROVLDSTAT?")])
        self.assertIn("timed out", self.driver.audit[-1].error)

    def test_failures_retain_audit_and_no_automatic_restoration(self):
        self.verify_identity()
        self.resource.fail_write = True
        with self.assertRaises(Sr865aError):
            self.driver.set_time_constant(3, authorized=True)
        self.assertEqual(self.resource.writes, ["OFLT 13"])
        self.assertIn("injected write failure", self.driver.audit[-1].error)
        self.assertIsNone(self.driver.audit[-1].response)

    def test_source_and_dc_reads_are_bounded_and_no_source_writer_exists(self):
        for command, method, invalid in (
            ("SLVL?", self.driver.read_sine_output, "5"),
            ("SOFF?", self.driver.read_source_offset, "5.1"),
            ("REFM?", self.driver.read_source_dc_mode, "2"),
        ):
            self.resource.responses[command] = invalid
            with self.subTest(command=command), self.assertRaises(Sr865aError):
                method()
        self.assertFalse(hasattr(self.driver, "set_sine_output"))
        self.assertEqual(self.resource.writes, [])

    def test_close_closes_only_injected_resource(self):
        self.driver.close()
        self.assertTrue(self.resource.closed)
        self.assertEqual(self.resource.events, [])


if __name__ == "__main__":
    unittest.main()
