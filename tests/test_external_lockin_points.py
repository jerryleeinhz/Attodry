"""Injected VISA only: external-source ownership, native settings and waits."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from attodry_control import config as cfg
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.external_lockin_points import (
    ExternalLockinPointSession, parse_external_lockin_acquisition,
)
from attodry_control.sr830 import Sr830Error, AuthorizationRequired
from attodry_control.sr830_settings import ReferenceSource, ExternalReferenceEdge
from tests import test_mixed_lockin_integration as mixed_fixture


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class ExternalLockinTests(unittest.TestCase):
    def fixture(self, *, authorized=True, harmonics=(1, 2)):
        helper = mixed_fixture.MixedLockinIntegrationTests()
        f = helper.fixture()
        self.addCleanup(helper.doCleanups)
        original_write = f.xx.write
        def external_write(command):
            original_write(command)
            if command.startswith(("FMOD ", "RSLP ")):
                key, value = command.split()
                f.xx.responses[key + "?"] = value
        f.xx.write = external_write
        old = load_hardware_combination(f.path).lockin
        xx = replace(old.lockin_xx, reference_source=ReferenceSource.EXTERNAL_TTL,
                     external_reference_edge=ExternalReferenceEdge.RISING, sine_output_connected=False)
        xy = old.lockin_xy
        table = {"xx_harmonics": list(harmonics), "xy_harmonics": list(harmonics),
                 "settle_time_constants": 15, "sample_interval_time_constants": 1,
                 "overload_policy": "record_continue", "skip_unsupported_harmonics": False}
        acquisition = parse_external_lockin_acquisition(table, xx, xy)
        config = SimpleNamespace(lockin_xx=xx, lockin_xy=xy, lockin_safety=old.lockin_safety,
            visa=old.visa, lockin_acquisition=acquisition,
            excitation_6221=SimpleNamespace(frequency_points_hz=(17.777, 200)))
        events = []
        clock = Clock()
        session = ExternalLockinPointSession(f.path, config, lambda k,v: events.append((k,v)),
            manager_factory=lambda: f.manager, authorize_writes=authorized,
            sleep=clock.sleep, monotonic=lambda: clock.now)
        self.addCleanup(session.close)
        return f, config, session, clock, events, table

    def start(self, f, s):
        s.open()
        s.configure()
        s.prepare_source_change(17.777)
        s.source_changed(1e-6, 17.777, source_record={"source_kind": "keithley6221_ac",
            "current_basis": "source_setting_readback", "actual": {
                "excitation_current_rms_a": 1e-6, "excitation_frequency_hz": 17.777}})

    def test_no_io_until_authorized_and_no_source_commands_anywhere(self):
        f, c, s, clock, events, table = self.fixture(authorized=False)
        self.assertEqual(f.manager.opened, [])
        with self.assertRaises(AuthorizationRequired):
            s.open()
        self.assertEqual(f.manager.opened, [])

    def test_complete_native_formal_harmonics_and_cleanup_preserve_disconnected_outputs(self):
        f, c, s, clock, events, table = self.fixture()
        before = (f.xx.responses["SLVL?"], f.xy.responses["SLVL?"], f.xy.responses["SOFF?"])
        self.start(f, s)
        s.qualify()
        reading = s.sample_point()
        self.assertTrue(reading["clean"], reading["problems"])
        self.assertEqual(reading["actual"], {})
        self.assertIn("lockin_xx_h2_amplitude_v", reading["measurements"])
        self.assertIn("lockin_xy_h2_amplitude_v", reading["measurements"])
        pairs = [v for k,v in events if k == "lockin_formal_pair"]
        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(v["settings_verified"] for v in pairs))
        self.assertTrue(all(v["excitation_source"]["source_kind"] == "keithley6221_ac" for v in pairs))
        self.assertTrue(all("source_v_rms" not in v["sweep_point"] for v in pairs))
        s.source_stopped({"verified": True, "output_off_confirmed": True})
        f.frequency["hz"] = 0.0
        cleanup = s.cleanup()
        self.assertTrue(cleanup["verified"], cleanup["errors"])
        self.assertEqual(int(f.xx.responses["HARM?"]), 1)
        self.assertEqual(int(f.xy.responses["HARM?"]), 1)
        forbidden = {"SLVL", "SOFF", "REFM", "BLAZEX", "FREQ", "FREQINT", "PHAS"}
        self.assertFalse(any(w.split()[0] in forbidden for w in f.xx.writes + f.xy.writes))
        self.assertEqual(before, (f.xx.responses["SLVL?"], f.xy.responses["SLVL?"], f.xy.responses["SOFF?"]))
        self.assertIn("FMOD 0", f.xx.writes)
        self.assertIn("SCAL 6", f.xy.writes)
        self.assertNotIn("SENS", {w.split()[0] for w in f.xy.writes})

    def test_source_off_unlock_is_explicit_nonformal_evidence_then_fresh_lock_is_required(self):
        f, c, s, clock, events, table = self.fixture(harmonics=(1,))
        f.xx.responses["LIAS?"] = "8"
        f.xy.responses["LIAS?"] = "8"
        f.xy.responses["CUROVLDSTAT?"] = "8"
        self.start(f, s)
        f.xx.responses["LIAS?"] = "0"
        f.xy.responses["LIAS?"] = "0"
        f.xy.responses["CUROVLDSTAT?"] = "0"
        s.qualify()
        self.assertTrue(s.sample_point()["clean"])
        f.xy.responses["CUROVLDSTAT?"] = "8"
        with self.assertRaises(Sr830Error):
            s.sample_point()
        self.assertTrue(any(v["source_inactive_or_transition"] for k,v in events if k == "external_lockin_status"))

    def test_shared_deadline_reuses_elapsed_time_and_unchanged_qualification_does_not_wait_twice(self):
        f, c, s, clock, events, table = self.fixture(harmonics=(1,))
        self.start(f, s)
        clock.now += 2
        s.qualify()
        self.assertEqual(clock.waits, [2.5])  # 15 * .3 s, with 2 s already elapsed.
        s.qualify()
        s.sample_point()
        self.assertEqual(clock.waits, [2.5])

    def test_initial_latched_overload_is_history_then_requires_fresh_settled_status(self):
        f, c, s, clock, events, table = self.fixture(harmonics=(1,))
        f.xx.responses['LIAS?'] = '13'
        f.xy.responses['LIAS?'] = '1819'
        f.xy.responses['CUROVLDSTAT?'] = '8'
        s.open()
        baseline = [v for k, v in events if k == 'external_lockin_status'][-1]
        self.assertTrue(baseline['initial_latch_baseline'])
        self.assertEqual(baseline['roles']['lockin_xx']['lia_status']['raw'], 13)
        self.assertFalse(f.xx.writes or f.xy.writes)
        s.configure()
        s.source_changed(1e-6, 17.777, source_record={'source_kind': 'keithley6221_ac'})
        with self.assertRaisesRegex(Sr830Error, 'baseline'):
            s._status(allow_unlock=True, initial_baseline=True)
        # Unlocked after the settling window must still reject acquisition.
        with self.assertRaises(Sr830Error): s.qualify()
        f.xx.responses['LIAS?'] = '0'
        f.xy.responses['LIAS?'] = '0'
        f.xy.responses['CUROVLDSTAT?'] = '0'
        s.qualify()
        self.assertTrue(s.sample_point()['clean'])

    def test_initial_baseline_does_not_excuse_present_sr865a_input_overload(self):
        f, c, s, clock, events, table = self.fixture()
        f.xy.responses['CUROVLDSTAT?'] = '16'
        with self.assertRaisesRegex(Sr830Error, 'current input overload'):
            s.open()
        self.assertFalse(f.xx.writes or f.xy.writes)

    def test_reference_drift_and_mode_change_reject_before_formal(self):
        f, c, s, clock, events, table = self.fixture(harmonics=(1,))
        self.start(f, s)
        f.frequency["hz"] = 20
        with self.assertRaises(Sr830Error):
            s.qualify()
        f.frequency["hz"] = 17.777
        f.xx.responses["FMOD?"] = "1"
        with self.assertRaisesRegex(Sr830Error, "reference setting"):
            s.sample_point()
        self.assertFalse(any(k == "lockin_formal_pair" for k,v in events))

    def test_partial_setting_failure_has_attempt_evidence_and_does_not_touch_source(self):
        f, c, s, clock, events, table = self.fixture()
        s.open()
        original = f.xy.write
        def failed(command):
            if command.startswith("SCAL "):
                raise OSError("injected sensitivity failure")
            return original(command)
        f.xy.write = failed
        with self.assertRaises(Sr830Error):
            s.configure()
        self.assertTrue(any(k == "lockin_command_attempt" and v["method"] == "set_sensitivity" for k,v in events))
        self.assertFalse(any(w.startswith(("SLVL ", "FREQ ")) for w in f.xx.writes))

    def test_repeated_source_coordinates_neither_park_harmonic_nor_restart_settling(self):
        f, c, s, clock, events, table = self.fixture(harmonics=(2,))
        self.start(f, s)
        s.qualify()
        self.assertEqual(clock.waits, [4.5])
        writes = (list(f.xx.writes), list(f.xy.writes))
        s.prepare_source_change(17.777)
        s.source_changed(1e-6, 17.777, index=1, source_record={"source_kind": "keithley6221_ac"})
        s.qualify()
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(writes, (f.xx.writes, f.xy.writes))

    def test_cleanup_attempts_both_roles_after_one_restore_failure(self):
        f, c, s, clock, events, table = self.fixture(harmonics=(2,))
        self.start(f, s)
        s.qualify()
        s.source_stopped({"verified": True, "output_off_confirmed": True})
        original = f.xx.write
        def failed(command):
            if command == "HARM 1":
                raise OSError("XX cleanup failure")
            return original(command)
        f.xx.write = failed
        cleanup = s.cleanup()
        self.assertFalse(cleanup["verified"])
        self.assertEqual(int(f.xy.responses["HARM?"]), 1)
        self.assertTrue(any("lockin_xx" in error for error in cleanup["errors"]))

    def test_unmatched_physical_idn_rejects_before_detector_writes(self):
        f, c, s, clock, events, table = self.fixture()
        f.xx.responses["*IDN?"] = "prefixStanford_Research_Systems,SR830,SN,1.0"
        with self.assertRaises(Sr830Error):
            s.open()
        self.assertEqual(f.xx.writes + f.xy.writes, [])

    def test_unknown_native_status_or_error_never_excused_by_source_off(self):
        for command, value in (("ERRS?", "1"), ("CUROVLDSTAT?", "256"), ("*ESR?", "128")):
            with self.subTest(command=command):
                f, c, s, clock, events, table = self.fixture()
                f.xy.responses[command] = value
                with self.assertRaises(Sr830Error):
                    s.open()
                self.assertFalse(f.xx.writes or f.xy.writes)

    def test_uncertain_source_off_cannot_certify_cleanup(self):
        f, c, s, clock, events, table = self.fixture()
        self.start(f, s)
        with self.assertRaises(Sr830Error):
            s.source_stopped({"verified": True, "output_off_confirmed": False})
        self.assertFalse(s.cleanup()["verified"])

    def test_source_off_proof_survives_partial_detector_open(self):
        f, c, s, clock, events, table = self.fixture()
        original = f.manager.open_resource
        def failed(address, *args, **kwargs):
            if address == c.lockin_xy.address:
                raise OSError("injected second detector open failure")
            return original(address, *args, **kwargs)
        f.manager.open_resource = failed
        with self.assertRaises(OSError):
            s.open()
        proof = {"verified": True, "output_off_confirmed": True, "last_confirmed_state": {"output": False}}
        s.source_stopped(proof)
        proof["last_confirmed_state"]["output"] = True
        cleanup = s.cleanup()
        self.assertTrue(cleanup["verified"])
        self.assertFalse(cleanup["attempted"])
        self.assertFalse(cleanup["source_off_proof"]["last_confirmed_state"]["output"])
        self.assertFalse(f.xx.writes or f.xy.writes)

    def test_cleanup_rejects_changed_ttl_reference_edge_for_each_role(self):
        for role in ("lockin_xx", "lockin_xy"):
            with self.subTest(role=role):
                f, c, s, clock, events, table = self.fixture(harmonics=(1,))
                self.start(f, s)
                s.qualify()
                s.source_stopped({"verified": True, "output_off_confirmed": True})
                resource = f.xx if role == "lockin_xx" else f.xy
                command = "RSLP?" if role == "lockin_xx" else "RTRG?"
                resource.responses[command] = "2"
                cleanup = s.cleanup()
                self.assertFalse(cleanup["verified"])
                self.assertTrue(any(role in error for error in cleanup["errors"]), cleanup)
                self.assertEqual(cleanup["final"][role]["reference_slope"], 2)

    def test_config_validation_rejects_invalid_harmonics_unknown_keys_and_unqualified_topology(self):
        f, c, s, clock, events, table = self.fixture()
        for changes in ({"xx_harmonics": [True]}, {"xy_harmonics": [2,2]},
                        {"xx_harmonics": [], "xy_harmonics": []}, {"settle_time_constants": 0},
                        {"sample_interval_time_constants": float("nan")}, {"unknown": 1}):
            with self.subTest(changes=changes), self.assertRaises(cfg.ConfigError):
                parse_external_lockin_acquisition({**table, **changes}, c.lockin_xx, c.lockin_xy)
        with self.assertRaises(cfg.ConfigError):
            parse_external_lockin_acquisition(table, replace(c.lockin_xx, sine_output_connected=True), c.lockin_xy)
        with self.assertRaises(ValueError):
            ExternalLockinPointSession(f.path, SimpleNamespace(**{**vars(c),
                "excitation_6221": SimpleNamespace(frequency_points_hz=(100000,))}), lambda *a: None)
        self.assertEqual(f.manager.opened, [])

    def test_external_role_parser_is_explicit_and_legacy_xx_stays_internal(self):
        f, c, s, clock, events, table = self.fixture()
        xx = dict(cfg._load_document(f.path)["lockin_xx"])
        xx.update(reference_source="external_ttl", external_reference_edge="rising", sine_output_connected=False)
        parsed = cfg._parse_lockin(xx, c.lockin_xx.role, "lockin_xx",
            c.lockin_safety.lockin_xx, c.lockin_safety, external_excitation=True)
        self.assertIs(parsed.reference_source, ReferenceSource.EXTERNAL_TTL)
        self.assertFalse(parsed.sine_output_connected)
        with self.assertRaises(cfg.ConfigError):
            cfg._parse_lockin(xx, c.lockin_xx.role, "lockin_xx", c.lockin_safety.lockin_xx, c.lockin_safety)
        for changes in ({"sine_output_connected": True}, {"reference_source": "internal"}):
            with self.subTest(changes=changes), self.assertRaises(cfg.ConfigError):
                cfg._parse_lockin({**xx, **changes}, c.lockin_xx.role, "lockin_xx",
                    c.lockin_safety.lockin_xx, c.lockin_safety, external_excitation=True)


if __name__ == "__main__":
    unittest.main()
