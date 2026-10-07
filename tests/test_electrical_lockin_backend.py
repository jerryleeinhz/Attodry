"""Fake-only electrical receiver ownership, native state and mixed-pair gates."""
from dataclasses import asdict
from types import SimpleNamespace
import json
import unittest

from attodry_control.electrical_lockin_backend import (
    ElectricalLockinError, ElectricalPairController, Sr865aReceiver,
    create_electrical_lockin, native_status_problems, pair_controller,
)
from attodry_control.models import LockinRole
from attodry_control.lockin_model_settings import ElectricalSettingCodes
from attodry_control.sr830 import AuthorizationRequired, DualSr830Controller, Sr830
from tests.test_lockin_backend import FakeResource


def role_config(model="SR865A", role=LockinRole.XY, *, edge="rising", current_status=True):
    return SimpleNamespace(model=model, role=role, external_reference_edge=edge,
        sr865a=SimpleNamespace(input_range_v_peak=0.3, reference_input_impedance_ohm=1_000_000.0,
                              current_status_supported=current_status, sync_output_mode="preserve"))


def receiver(*, authorized=True, current_status=True, edge="rising"):
    resource = FakeResource("SR865A", "xy")
    config = role_config(current_status=current_status, edge=edge)
    return Sr865aReceiver(resource, config=config, authorize_writes=authorized), resource


class ReceiverTests(unittest.TestCase):
    def test_constructor_and_unauthorized_writes_have_no_io(self):
        adapter, resource = receiver(authorized=False)
        for call in (lambda: adapter.set_sensitivity(6), lambda: adapter.set_harmonic(2),
                     lambda: adapter.set_fixed_setting("time_constant", 12), adapter.configure_receiver):
            with self.assertRaises(AuthorizationRequired):
                call()
        self.assertEqual(resource.queries + resource.writes, [])

    def test_source_frequency_and_reserve_writes_are_rejected_without_io(self):
        adapter, resource = receiver()
        for call in (lambda: adapter.set_sine_output(0.1), adapter.set_minimum_sine_output,
                     lambda: adapter.set_internal_reference_frequency(50000),
                     lambda: adapter.configure_xx_minimum_excitation(50000),
                     lambda: adapter.set_reserve_mode(0)):
            with self.assertRaises(ElectricalLockinError):
                call()
        self.assertEqual(resource.queries + resource.writes, [])
        self.assertIsNone(adapter.read_reserve_mode())

    def test_diagnostic_retains_native_codes_and_disconnected_source_state(self):
        adapter, resource = receiver()
        resource.responses.update({"SLVL?": "1.2", "SOFF?": "-0.5", "REFM?": "1"})
        diagnostic = adapter.read_diagnostic(consume_status_latches=True)
        self.assertEqual((diagnostic.model, diagnostic.reference_mode, diagnostic.sensitivity,
                          diagnostic.time_constant), ("SR865A", 1, 9, 12))
        self.assertEqual(diagnostic.reference_source, "external")
        self.assertIsNone(diagnostic.reserve_mode)
        self.assertIsNone(diagnostic.line_filter)
        self.assertEqual(diagnostic.sensitivity_full_scale_v, 0.001)
        self.assertEqual(diagnostic.time_constant_s, 1.0)
        self.assertEqual(diagnostic.native_settings.source_offset_v, -0.5)
        self.assertEqual(diagnostic.native_settings.sine_output_v, 1.2)
        self.assertEqual(diagnostic.native_status.current_status_raw, 0)
        self.assertEqual(resource.writes, [])
        self.assertIn("SCAL?", resource.queries)
        self.assertNotIn("SENS?", resource.queries)
        self.assertNotIn("RMOD?", resource.queries)

    def test_native_sensitivity_and_time_constant_codes_do_not_use_sr830_tables(self):
        adapter, resource = receiver()
        adapter.query_identity()
        adapter.set_sensitivity(6)
        adapter.set_fixed_setting("time_constant", 13)
        self.assertEqual(resource.writes, ["SCAL 6", "OFLT 13"])
        self.assertEqual(adapter.read_sensitivity(), 6)
        self.assertEqual(adapter.read_time_constant(), 13)
        self.assertEqual(adapter.audit[-1].command, "OFLT?")

    def test_receiver_configuration_preserves_source_and_only_writes_input_settings(self):
        adapter, resource = receiver()
        resource.responses.update({"SLVL?": "1.2", "SOFF?": "0.7", "REFM?": "1", "BLAZEX?": "0"})
        adapter.query_identity()
        result = adapter.configure_receiver()
        self.assertEqual(resource.writes, ["IRNG 1"])
        self.assertEqual(result["source_output_policy"], "preserve_disconnected_receiver")
        self.assertFalse(result["source_write_attempted"])
        for field in ("sine_output_v", "source_offset_v", "source_dc_mode", "sync_output_mode", "phase_shift_deg"):
            self.assertEqual(result["before"][field], result["after"][field])
        restored = json.loads(json.dumps(result))
        self.assertEqual(restored["before"]["source_offset_v"], 0.7)
        self.assertTrue(any(record["command"] == "IRNG 1" for record in restored["raw"]))
        self.assertFalse(any(command.split()[0] in {"SLVL", "SOFF", "REFM", "BLAZEX", "FREQ", "FREQINT"}
                             for command in resource.writes))

    def test_receiver_ignored_irng_fails_with_retained_write_audit(self):
        adapter, resource = receiver()
        resource.write = lambda command: resource.writes.append(command)
        adapter.query_identity()
        with self.assertRaises(ElectricalLockinError):
            adapter.configure_receiver()
        self.assertEqual(resource.writes, ["IRNG 1"])
        self.assertTrue(any(record.command == "IRNG 1" for record in adapter.audit))
        with self.assertRaises(ElectricalLockinError):
            adapter.set_sensitivity(6)

    def test_receiver_configures_rc_filters_without_changing_blazex(self):
        adapter, resource = receiver()
        resource.responses.update({"ADVFILT?": "1", "SYNC?": "1", "BLAZEX?": "0"})
        adapter.query_identity()
        result = adapter.configure_receiver()
        self.assertEqual(resource.writes, ["IRNG 1", "ADVFILT 0", "SYNC 0"])
        self.assertFalse(result["after"]["advanced_filter"])
        self.assertFalse(result["after"]["synchronous_filter"])
        self.assertEqual(result["after"]["sync_output_mode"], "blazex")
        self.assertNotIn("BLAZEX 0", resource.writes)

    def test_runtime_invariants_preserve_complete_native_evidence_with_no_writes(self):
        adapter, resource = receiver()
        adapter.query_identity()
        adapter.configure_receiver()
        previous_writes = list(resource.writes)
        result = adapter.read_receiver_invariants()
        self.assertEqual(result["input_range_v_peak"], 0.3)
        self.assertEqual(result["reference_input_impedance_ohm"], 1_000_000.0)
        self.assertEqual(result["reference_source"], "external")
        self.assertEqual(result["source_output_baseline"]["sine_output_v"], 0.004)
        self.assertEqual(resource.writes, previous_writes)
        self.assertTrue(result["raw"])
        json.dumps(result)

    def test_runtime_source_drift_fails_with_partial_native_evidence_without_writes(self):
        for query, value in (("SLVL?", "0.005"), ("SOFF?", "0.1"), ("REFM?", "1"), ("BLAZEX?", "0"), ("PHAS?", "1")):
            adapter, resource = receiver()
            adapter.query_identity()
            adapter.configure_receiver()
            previous_writes = list(resource.writes)
            resource.responses[query] = value
            with self.assertRaises(ElectricalLockinError) as raised:
                adapter.read_receiver_invariants()
            self.assertEqual(resource.writes, previous_writes)
            self.assertEqual(raised.exception.evidence["source_output_baseline"]["sine_output_v"], 0.004)
            self.assertTrue(raised.exception.raw)
            json.dumps(raised.exception.evidence)
            # A second configuration must not silently adopt the changed output.
            with self.assertRaises(ElectricalLockinError):
                adapter.configure_receiver()
            self.assertEqual(resource.writes, previous_writes)

    def test_harmonic_uses_sr865a_detection_range_and_derived_phase(self):
        adapter, resource = receiver()
        resource.responses.update({"FREQEXT?": "50000", "FREQDET?": "50000", "SNAP? X,Y": "0.003,0.004"})
        adapter.query_identity()
        adapter.set_harmonic(3)
        sample = adapter.read_harmonic_sample(3)
        self.assertEqual(sample.reading.frequency_hz, 50000)
        self.assertEqual(sample.reading.detection_frequency_hz, 150000)
        self.assertEqual(sample.reading.amplitude_v, 0.005)
        self.assertAlmostEqual(sample.reading.phase_deg, 53.13010235415598)
        self.assertEqual(sample.model, "SR865A")
        self.assertEqual(resource.writes, ["HARM 3"])

    def test_zero_signal_keeps_undefined_phase_in_serialized_sample(self):
        adapter, resource = receiver()
        resource.responses["SNAP? X,Y"] = "0,0"
        sample = adapter.read_harmonic_sample(1)
        self.assertIsNone(sample.reading.phase_deg)
        self.assertIsNone(asdict(sample)["reading"]["phase_deg"])
        self.assertEqual(resource.writes, [])

    def test_wrong_identity_and_query_failure_retain_raw_audit(self):
        adapter, resource = receiver()
        resource.responses["*IDN?"] = "Stanford_Research_Systems,SR830,xy,v1.0"
        with self.assertRaises(ElectricalLockinError):
            adapter.read_diagnostic(consume_status_latches=True)
        self.assertEqual(resource.writes, [])
        self.assertEqual(adapter.audit[-1].command, "*IDN?")
        adapter, resource = receiver()
        resource.responses["SNAP? X,Y"] = OSError("injected response failure")
        with self.assertRaises(ElectricalLockinError):
            adapter.read_harmonic_sample(1)
        self.assertEqual(adapter.audit[-1].command, "SNAP? X,Y")
        self.assertIsNotNone(adapter.audit[-1].error)

    def test_native_latched_overload_survives_clean_current_status(self):
        adapter, resource = receiver()
        resource.responses["LIAS?"] = "16"
        sample = adapter.read_harmonic_sample(1)
        self.assertEqual(sample.lia_status.raw, 16)
        self.assertTrue(sample.lia_status.input_or_reserve_overload)
        self.assertFalse(sample.lia_status.native_status.input_overload)
        self.assertTrue(sample.lia_status.native_status.input_overload_latched)

    def test_missing_current_status_unknown_bits_and_events_are_not_clean(self):
        adapter, _ = receiver(current_status=False)
        status, _ = adapter.read_status_latches()
        self.assertFalse(status.status_known)
        self.assertIn("safety status is incomplete", native_status_problems(status))
        for command, value, expected in (("LIAS?", "4", "unknown native status bits"),
                                         ("*ESR?", "64", "configuration changed"),
                                         ("*ESR?", "128", "instrument power-on"),
                                         ("*ESR?", "32", "instrument error")):
            adapter, resource = receiver()
            resource.responses[command] = value
            status, _ = adapter.read_status_latches()
            self.assertIn(expected, native_status_problems(status))

    def test_unknown_native_fixed_code_and_unsupported_mode_have_no_writes(self):
        adapter, resource = receiver()
        adapter.query_identity()
        resource.responses["ISRC?"] = "99"
        with self.assertRaises(ElectricalLockinError):
            adapter.set_fixed_setting("input_mode", 1)
        with self.assertRaises(ValueError):
            adapter.set_fixed_setting("reference_mode", 0)
        with self.assertRaises(ValueError):
            adapter.set_fixed_setting("line_filter", 0)
        self.assertEqual(resource.writes, [])

    def test_fixed_settings_validate_entire_native_plan_before_io_and_authorization(self):
        resource = FakeResource("SR865A", "xy")
        adapter = create_electrical_lockin(resource, role_config())
        valid = ElectricalSettingCodes("SR865A", 1, 1, 1, 0, 0, 12, 3, 6)
        invalid = ElectricalSettingCodes("SR865A", 1, 1, 1, 0, 0, 12, 3, 99)
        with self.assertRaises(ValueError):
            adapter.write_fixed_settings(invalid)
        with self.assertRaises(AuthorizationRequired):
            adapter.write_fixed_settings(valid)
        self.assertEqual(resource.queries + resource.writes, [])
        adapter.query_identity()
        previous_queries = list(resource.queries)
        with self.assertRaises(AuthorizationRequired):
            adapter.write_fixed_settings(valid)
        self.assertEqual(resource.queries, previous_queries)
        self.assertEqual(resource.writes, [])

    def test_fixed_settings_apply_only_native_input_filter_and_scal_commands(self):
        adapter, resource = receiver()
        adapter.query_identity()
        requested = ElectricalSettingCodes("SR865A", 1, 1, 0, 1, 1, 13, 2, 6)
        adapter.write_fixed_settings(requested)
        self.assertEqual(resource.writes, ["ISRC 0", "IGND 1", "ICPL 1", "OFLT 13", "OFSL 2", "SCAL 6"])
        commands = [record.command for record in adapter.audit if record.response is None]
        self.assertEqual(commands, resource.writes)
        adapter.write_fixed_settings(requested)
        self.assertEqual(len(resource.writes), 6)


class ElectricalPairTests(unittest.TestCase):
    def pair(self, *, current_status=True, edge="rising"):
        xx_resource = FakeResource("SR830", "xx")
        xx_resource.responses["FMOD?"] = "1"
        xx = Sr830(xx_resource, LockinRole.XX)
        xy, xy_resource = receiver(current_status=current_status, edge=edge)
        return pair_controller(xx, xy), xx_resource, xy_resource

    def test_factory_is_offline_and_preserves_old_pair_controller(self):
        xx_resource, xy_resource = FakeResource(), FakeResource()
        xx = create_electrical_lockin(xx_resource, role_config("SR830", LockinRole.XX))
        xy = create_electrical_lockin(xy_resource, role_config("SR830", LockinRole.XY))
        self.assertIsInstance(pair_controller(xx, xy), DualSr830Controller)
        self.assertEqual(xx_resource.queries + xy_resource.queries, [])
        with self.assertRaises(ValueError):
            create_electrical_lockin(xy_resource, role_config("SR865A", LockinRole.XX))

    def test_mixed_preflight_accepts_disconnected_nonminimum_xy_source_without_writes(self):
        controller, xx_resource, xy_resource = self.pair()
        xy_resource.responses.update({"SLVL?": "1.2", "SOFF?": "0.7"})
        xx, xy = controller.verify_existing_configuration(frequency_hz=50027)
        self.assertEqual(xy.sine_output_v, 1.2)
        self.assertEqual(xx.sine_output_v, 0.004)
        self.assertEqual(xx_resource.writes + xy_resource.writes, [])

    def test_mixed_preflight_requires_xx_minimum_and_verified_xy_current_status(self):
        for source_v in ("0.1", "0.003", "0.005"):
            controller, xx_resource, xy_resource = self.pair()
            xx_resource.responses["SLVL?"] = source_v
            with self.assertRaisesRegex(ElectricalLockinError, "4 mVrms"):
                controller.verify_existing_configuration(frequency_hz=50027)
            self.assertEqual(xx_resource.writes + xy_resource.writes, [])
        controller, _, _ = self.pair(current_status=False)
        with self.assertRaisesRegex(ElectricalLockinError, "incomplete"):
            controller.verify_existing_configuration(frequency_hz=50027)

    def test_sr865a_output_scale_unlock_and_unknown_faults_remain_strict(self):
        for command, value in (("LIAS?", "1"), ("CUROVLDSTAT?", "8"),
                               ("LIAS?", "4"), ("*ESR?", "32")):
            controller, xx_resource, xy_resource = self.pair()
            xy_resource.responses[command] = value
            with self.assertRaises(ElectricalLockinError):
                controller.verify_existing_configuration(frequency_hz=50027, ignore_output_overload=True)
            self.assertEqual(xx_resource.writes + xy_resource.writes, [])

    def test_external_reference_edge_keeps_explicit_falling_configuration(self):
        controller, _, xy_resource = self.pair(edge="falling")
        xy_resource.responses["RTRG?"] = "2"
        controller.verify_existing_configuration(frequency_hz=50027)

    def test_wrong_reference_and_harmonic_fail_before_setting_writes(self):
        for command, value in (("RSRC?", "0"), ("HARM?", "2")):
            controller, xx_resource, xy_resource = self.pair()
            xy_resource.responses[command] = value
            if command == "HARM?":
                xy_resource.responses["FREQDET?"] = "100054"
            with self.assertRaises(ElectricalLockinError):
                controller.verify_existing_configuration(frequency_hz=50027)
            self.assertEqual(xx_resource.writes + xy_resource.writes, [])

    def test_mixed_source_identity_must_be_exact_before_any_write(self):
        controller, xx_resource, xy_resource = self.pair()
        xx_resource.responses["*IDN?"] = "prefixStanford_Research_Systems,SR830,xx,v1.0"
        with self.assertRaisesRegex(ElectricalLockinError, "identity"):
            controller.verify_existing_configuration(frequency_hz=50027)
        self.assertEqual(xx_resource.writes + xy_resource.writes, [])


if __name__ == "__main__":
    unittest.main()
