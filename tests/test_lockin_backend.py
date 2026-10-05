from dataclasses import FrozenInstanceError
import math
import unittest

from attodry_control.lockin_backend import (
    LockinBackendError,
    LockinModel,
    RoleHarmonicPlan,
    capabilities_for,
    create_lockin_backend,
    validate_role_harmonics,
)
from attodry_control.models import LockinRole
from attodry_control.sr830 import Sr830Error
from attodry_control.sr865a import Sr865aError


class FakeResource:
    def __init__(self, model="SR830", role="xx"):
        self.queries = []
        self.writes = []
        self.responses = {
            "*IDN?": f"Stanford_Research_Systems,{model},{role},v1.00\n",
            "FMOD?": "0", "RSLP?": "1", "FREQ?": "50027",
            "HARM?": "1", "SLVL?": "0.004", "ISRC?": "1",
            "IGND?": "0", "ICPL?": "0", "ILIN?": "0",
            "SENS?": "17", "RMOD?": "1", "OFLT?": "10",
            "OFSL?": "3", "PHAS?": "0", "SNAP? 1,2,3,4,9": "3,4,7,12,50027",
            "LIAS?": "0", "ERRS?": "0", "*ESR?": "0",
            "RSRC?": "1", "RTRG?": "1", "REFZ?": "1",
            "IVMD?": "0", "SCAL?": "9", "IRNG?": "2",
            "ADVFILT?": "0", "SYNC?": "0", "SOFF?": "0",
            "REFM?": "0", "BLAZEX?": "2", "FREQEXT?": "50027",
            "FREQDET?": "50027", "SNAP? X,Y": "3,4", "CUROVLDSTAT?": "0",
        }
        if model == "SR865A":
            self.responses["OFLT?"] = "12"

    def query(self, command):
        self.queries.append(command)
        response = self.responses[command]
        if isinstance(response, Exception):
            raise response
        return response

    def write(self, command):
        self.writes.append(command)
        name, value = command.split(" ", 1)
        self.responses[name + "?"] = value
        if name == "HARM":
            self.responses["FREQDET?"] = str(float(self.responses["FREQEXT?"]) * int(value))

    def clear(self):
        raise AssertionError("Foundation must not clear an injected resource.")

    def close(self):
        raise AssertionError("The caller owns the resource.")


def backend(model="SR830", role="lockin_xx"):
    resource = FakeResource(model, role)
    return create_lockin_backend(model=model, role=role, resource=resource), resource


class CapabilityTests(unittest.TestCase):
    def test_factory_and_capabilities_do_not_perform_io(self):
        for model in LockinModel:
            adapter, resource = backend(model)
            self.assertEqual(adapter.capabilities.model, model)
            self.assertEqual(resource.queries, [])
            self.assertEqual(resource.writes, [])
            with self.assertRaises(FrozenInstanceError):
                adapter.capabilities.maximum_detection_hz = 1

    def test_unknown_model_and_numbered_role_reject_without_io(self):
        resource = FakeResource()
        for model, role in (("SR865", "lockin_xx"), ("sr830", "lockin_xy"), ("SR830", "1")):
            with self.assertRaises(ValueError):
                create_lockin_backend(model=model, role=role, resource=resource)
        self.assertEqual(resource.queries + resource.writes, [])

    def test_per_model_detection_boundaries(self):
        old = capabilities_for("SR830")
        new = capabilities_for("SR865A")
        self.assertEqual(old.validate_detection(51_000, 2), 102_000)
        self.assertEqual(new.validate_detection(1_999_999, 2), 3_999_998)
        for caps, frequency, harmonic in (
            (old, 51_000.001, 2), (old, 50_027, 3),
            (new, 2_000_000, 2), (new, 4_000_000, 1),
        ):
            with self.assertRaises(ValueError):
                caps.validate_detection(frequency, harmonic)

    def test_nonfinite_bool_and_software_harmonic_scope_reject(self):
        for model in LockinModel:
            caps = capabilities_for(model)
            for frequency in (True, False, float("inf"), float("nan"), -1, 0):
                with self.subTest(model=model, frequency=frequency), self.assertRaises(ValueError):
                    caps.validate_detection(frequency, 1)
            for harmonic in (True, False, 1.0, 0, 4, 99):
                with self.subTest(model=model, harmonic=harmonic), self.assertRaises(ValueError):
                    caps.validate_detection(50_027, harmonic)
        self.assertEqual(capabilities_for("SR865A").maximum_hardware_harmonic, 99)

    def test_ladders_are_physical_ascending_even_when_codes_reverse(self):
        for model in LockinModel:
            caps = capabilities_for(model)
            self.assertEqual(caps.sensitivity_full_scales_v, tuple(sorted(caps.sensitivity_full_scales_v)))
            self.assertIn(0.001, caps.sensitivity_full_scales_v)
            self.assertIn(1.0, caps.time_constants_s)

    def test_mixed_role_plan_does_not_union_harmonics(self):
        plan = (
            RoleHarmonicPlan(LockinRole.XX, LockinModel.SR865A, 50_027, 3),
            RoleHarmonicPlan(LockinRole.XY, LockinModel.SR830, 50_027, 2),
        )
        self.assertEqual(validate_role_harmonics(plan), (150_081, 100_054))
        xx, xx_resource = backend("SR865A", "lockin_xx")
        xy, xy_resource = backend("SR830", "lockin_xy")
        for adapter, item in zip((xx, xy), plan):
            adapter.set_harmonic(item.harmonic, authorize_writes=True)
        self.assertEqual(xx_resource.writes, ["HARM 3"])
        self.assertEqual(xy_resource.writes, ["HARM 2"])

    def test_invalid_second_role_rejects_entire_preflight_without_writes(self):
        plan = (
            RoleHarmonicPlan(LockinRole.XX, LockinModel.SR865A, 50_027, 3),
            RoleHarmonicPlan(LockinRole.XY, LockinModel.SR830, 50_027, 3),
        )
        with self.assertRaises(ValueError):
            validate_role_harmonics(plan)
        with self.assertRaises(ValueError):
            validate_role_harmonics((plan[0], plan[0]))


class SetterTests(unittest.TestCase):
    def test_one_second_has_model_specific_codes_and_verified_readback(self):
        for model, expected in (("SR830", "OFLT 10"), ("SR865A", "OFLT 12")):
            adapter, resource = backend(model)
            resource.responses["OFLT?"] = "0"
            adapter.set_time_constant(1.0, authorize_writes=True)
            self.assertEqual(resource.writes, [expected])
            self.assertIn("*IDN?", resource.queries)
            self.assertEqual(resource.queries[-1], "OFLT?")

    def test_same_voltage_uses_opposite_model_code_order(self):
        for model, expected in (("SR830", "SENS 17"), ("SR865A", "SCAL 9")):
            adapter, resource = backend(model)
            resource.responses["SENS?"] = "16"
            adapter.set_sensitivity(0.001, authorize_writes=True)
            self.assertEqual(resource.writes, [expected])

    def test_wrong_or_prefixed_identity_prevents_any_write(self):
        for model in LockinModel:
            for identity in (
                "Stanford_Research_Systems,SR860,001,v1.0",
                f"prefixStanford_Research_Systems,{model},001,v1.0",
            ):
                adapter, resource = backend(model)
                resource.responses["*IDN?"] = identity
                with self.assertRaises((LockinBackendError, Sr830Error, Sr865aError)):
                    adapter.set_time_constant(1.0, authorize_writes=True)
                self.assertEqual(resource.writes, [])

    def test_setters_without_authorization_have_no_io(self):
        for model in LockinModel:
            adapter, resource = backend(model)
            for operation in (
                lambda: adapter.set_time_constant(1.0),
                lambda: adapter.set_sensitivity(0.001),
                lambda: adapter.set_harmonic(1),
            ):
                with self.assertRaises(LockinBackendError):
                    operation()
            self.assertEqual(resource.queries + resource.writes, [])

    def test_bad_physical_values_reject_before_any_io(self):
        for model in LockinModel:
            adapter, resource = backend(model)
            for value in (True, False, math.nan, math.inf, -1, "1", 0.007):
                for setter in (adapter.set_time_constant, adapter.set_sensitivity):
                    with self.assertRaises(ValueError):
                        setter(value, authorize_writes=True)
            with self.assertRaises(ValueError):
                adapter.set_harmonic(True, authorize_writes=True)
            self.assertEqual(resource.queries + resource.writes, [])

    def test_current_input_cannot_be_mislabeled_as_voltage_sensitivity(self):
        for model, query, current_code in (("SR830", "ISRC?", "2"), ("SR865A", "IVMD?", "1")):
            adapter, resource = backend(model)
            resource.responses[query] = current_code
            with self.assertRaises((LockinBackendError, Sr865aError)):
                adapter.set_sensitivity(0.001, authorize_writes=True)
            self.assertEqual(resource.writes, [])

    def test_actual_reference_drift_rejects_before_harmonic_write(self):
        adapter, resource = backend("SR830", "lockin_xy")
        resource.responses["FREQ?"] = "51001"
        with self.assertRaises(ValueError):
            adapter.set_harmonic(2, authorize_writes=True)
        self.assertEqual(resource.writes, [])

    def test_malformed_previous_sr830_setting_prevents_writes(self):
        for query, action in (
            ("OFLT?", lambda device: device.set_time_constant(1.0, authorize_writes=True)),
            ("SENS?", lambda device: device.set_sensitivity(0.001, authorize_writes=True)),
            ("HARM?", lambda device: device.set_harmonic(2, authorize_writes=True)),
        ):
            adapter, resource = backend("SR830")
            resource.responses[query] = "99"
            with self.assertRaises(Sr830Error):
                action(adapter)
            self.assertEqual(resource.writes, [])

    def test_unchanged_sr830_settings_skip_writes(self):
        adapter, resource = backend("SR830")
        adapter.set_time_constant(1.0, authorize_writes=True)
        adapter.set_sensitivity(0.001, authorize_writes=True)
        adapter.set_harmonic(1, authorize_writes=True)
        self.assertEqual(resource.writes, [])

    def test_successful_and_failed_writes_are_retained_in_audit(self):
        adapter, resource = backend("SR830")
        adapter.set_harmonic(2, authorize_writes=True)
        writes = [item for item in adapter.audit if item.operation == "write"]
        self.assertEqual([item.command for item in writes], ["HARM 2"])
        self.assertIsNone(writes[0].error)

        def fail_write(command):
            raise OSError("injected send failure")

        resource.write = fail_write
        with self.assertRaises(OSError):
            adapter.set_time_constant(3.0, authorize_writes=True)
        self.assertEqual(adapter.audit[-1].operation, "write")
        self.assertEqual(adapter.audit[-1].command, "OFLT 11")
        self.assertIn("OSError", adapter.audit[-1].error)

    def test_source_policy_blocks_normal_settings_but_permits_protective_reduction(self):
        for model, dc_mode in (("SR830", "not_supported"), ("SR865A", "common")):
            adapter, resource = backend(model)
            resource.responses["SLVL?"] = "0.01"
            options = {"minimum_v": 0.004, "maximum_v": 0.005,
                       "expected_dc_mode": dc_mode, "authorize_writes": True}
            with self.assertRaises(LockinBackendError):
                adapter.set_source_amplitude(0.004, **options)
            self.assertEqual(resource.writes, [])
            result = adapter.set_source_amplitude(0.004, protect=True, **options)
            self.assertEqual(result["source_voltage_v"], 0.004)

    def test_xy_cannot_write_the_disconnected_source(self):
        adapter, resource = backend("SR865A", "lockin_xy")
        with self.assertRaises(ValueError):
            adapter.set_source_amplitude(0.004, minimum_v=1e-9, maximum_v=0.01,
                                         expected_dc_mode="common", authorize_writes=True)
        self.assertEqual(resource.queries + resource.writes, [])


class ReadbackTests(unittest.TestCase):
    def test_settings_share_physical_units_but_preserve_native_details(self):
        for model in LockinModel:
            adapter, resource = backend(model)
            readback = adapter.read_settings()
            self.assertEqual(readback.time_constant_s, 1.0)
            self.assertEqual(readback.sensitivity_full_scale_v, 0.001)
            self.assertEqual(readback.reference_source, "external")
            self.assertEqual(readback.input_range_v_peak, None if model == "SR830" else 0.1)
            self.assertTrue(readback.raw)
            self.assertIsNotNone(readback.native_settings)
            self.assertEqual(resource.writes, [])
            self.assertNotIn("LIAS?", resource.queries)

    def test_unobserved_status_stays_unknown_and_raw_queries_are_timed(self):
        for model in LockinModel:
            adapter, resource = backend(model)
            sample = adapter.read_sample(consume_status_latches=False)
            self.assertIsNone(sample.status.locked)
            self.assertIsNone(sample.status.clean)
            self.assertNotIn("LIAS?", resource.queries)
            self.assertNotIn("ERRS?", resource.queries)
            for query in sample.raw:
                self.assertIsNotNone(query.started_at_utc.utcoffset())
                self.assertGreaterEqual(query.completed_at_utc, query.started_at_utc)
                self.assertIsNotNone(query.response)

    def test_native_sr830_amplitude_is_not_recalculated(self):
        adapter, _ = backend("SR830")
        sample = adapter.read_sample(consume_status_latches=True)
        self.assertEqual(sample.amplitude_v, 7)
        self.assertEqual(sample.phase_deg, 12)
        self.assertEqual(sample.amplitude_phase_source, "instrument_snapshot")
        self.assertEqual(sample.detection_frequency_source, "derived_harmonic_times_reference")
        self.assertTrue(sample.status.clean)

    def test_unknown_sr830_status_bit_cannot_certify_clean(self):
        adapter, resource = backend("SR830")
        resource.responses["LIAS?"] = "128"
        sample = adapter.read_sample(consume_status_latches=True)
        self.assertIsNone(sample.status.locked)
        self.assertFalse(sample.status.clean)

    def test_sr830_transition_latches_require_session_requalification(self):
        for bit in (16, 32):
            adapter, resource = backend("SR830")
            resource.responses["LIAS?"] = str(bit)
            sample = adapter.read_sample(consume_status_latches=True)
            self.assertIsNone(sample.status.clean)

    def test_invalid_sr830_reference_mode_rejects_sample(self):
        adapter, resource = backend("SR830")
        resource.responses["FMOD?"] = "2"
        with self.assertRaises(LockinBackendError):
            adapter.read_sample(consume_status_latches=False)
        self.assertEqual(resource.writes, [])

    def test_sr865a_derived_zero_phase_is_undefined(self):
        adapter, resource = backend("SR865A")
        resource.responses["SNAP? X,Y"] = "0,0"
        sample = adapter.read_sample(consume_status_latches=False)
        self.assertEqual(sample.amplitude_v, 0)
        self.assertIsNone(sample.phase_deg)
        self.assertEqual(sample.amplitude_phase_source, "derived_from_snapshot_xy")
        self.assertEqual(sample.detection_frequency_source, "instrument_query")

    def test_sr865a_latched_fault_survives_current_clean_observation(self):
        adapter, resource = backend("SR865A")
        resource.responses["LIAS?"] = "8"
        sample = adapter.read_sample(consume_status_latches=True, current_status_supported=True)
        self.assertTrue(sample.status.locked)
        self.assertFalse(sample.status.clean)
        self.assertTrue(sample.status.native_status.reference_unlock_latched)

    def test_failed_query_preserves_partial_audit(self):
        adapter, resource = backend("SR830")
        resource.responses["OFLT?"] = TimeoutError("injected timeout")
        with self.assertRaises(TimeoutError):
            adapter.read_settings()
        self.assertGreater(len(adapter.audit), 1)
        self.assertEqual(adapter.audit[-1].command, "OFLT?")
        self.assertIn("TimeoutError", adapter.audit[-1].error)
        self.assertEqual(resource.writes, [])

    def test_invalid_status_flags_reject_before_io(self):
        adapter, resource = backend("SR830")
        with self.assertRaises(ValueError):
            adapter.read_sample(consume_status_latches=1)
        with self.assertRaises(ValueError):
            adapter.read_sample(consume_status_latches=False, current_status_supported=True)
        self.assertEqual(resource.queries + resource.writes, [])


if __name__ == "__main__":
    unittest.main()
