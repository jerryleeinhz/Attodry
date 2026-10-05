import copy
import unittest

from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.photonics_lockin_helpers import FakeManager, fixture_document, safety_document


class PhotonicsPointTests(unittest.TestCase):
    def make(self, document=None, event=None):
        document = fixture_document() if document is None else document
        cfg = load_photonics_lockin_config("fake.toml", document=document, safety_document=safety_document())
        mgr = FakeManager(document)
        events = []
        waits = []
        session = PhotonicsLockinPointSession("fake.toml", cfg,
            (lambda kind, payload: events.append((kind, payload))) if event is None else event,
            manager_factory=lambda: mgr, sleep=waits.append)
        return session, mgr, events, waits

    def ready(self, document=None):
        session, mgr, events, waits = self.make(document)
        session.open()
        session.protect_source()
        session.configure()
        session.set_point(0.006)
        session.qualify()
        return session, mgr, events, waits

    def test_construction_and_grid_are_pure(self):
        session, mgr, _, _ = self.make()
        self.assertEqual(mgr.opened, [])
        self.assertEqual([p.source_v_rms for p in session.grid], [0.004, 0.006])
        self.assertEqual(session.harmonics_by_role, {"xx": (1, 3), "xy": (2,)})

    def test_wrong_second_identity_means_no_writes_and_both_resources_closed(self):
        session, mgr, _, _ = self.make()
        mgr.resources["FAKE::XY"].responses["*IDN?"] = "Stanford_Research_Systems,SR865A,wrong,v1.00"
        with self.assertRaises(RuntimeError):
            session.open()
        self.assertTrue(mgr.closed)
        for resource in mgr.resources.values():
            self.assertEqual(resource.writes, [])
            self.assertTrue(resource.closed)

    def test_mixed_harmonics_sample_full_cycle_without_union_writes(self):
        session, mgr, events, waits = self.ready()
        sample = session.sample_point()
        self.assertTrue(sample["clean"])
        self.assertIn("lockin_xx_h3_x_v", sample["measurements"])
        self.assertIn("lockin_xy_h2_x_v", sample["measurements"])
        self.assertNotIn("HARM 3", mgr.resources["FAKE::XY"].writes)
        self.assertFalse(any(cmd.startswith("SLVL ") for cmd in mgr.resources["FAKE::XY"].writes))
        self.assertTrue(waits)
        self.assertTrue(any(kind == "lockin_model_io" for kind, _ in events))
        self.assertTrue(session.cleanup()["verified"])
        for resource in mgr.resources.values():
            self.assertFalse(any(command.startswith("FREQ ") or command in ("FMOD 1", "RSRC 0")
                                 for command in resource.writes))
        self.assertEqual(mgr.resources["FAKE::XX"].responses["SLVL?"], "0.004")
        session.close()

    def test_dual_sr865a_h3_is_available_to_xy(self):
        doc = fixture_document()
        doc["lockin_xy"]["model"] = "SR865A"
        del doc["lockin_xy"]["sr830"]
        doc["lockin_xy"]["sr865a"] = copy.deepcopy(doc["lockin_xx"]["sr865a"])
        doc["lockin_xy"]["harmonics"] = [1, 2, 3]
        session, mgr, _, _ = self.ready(doc)
        sample = session.sample_point()
        self.assertIn("lockin_xy_h3_amplitude_v", sample["measurements"])
        self.assertIn("HARM 3", mgr.resources["FAKE::XY"].writes)
        self.assertTrue(session.cleanup()["verified"])
        session.close()

    def test_source_protection_can_precede_reference_startup(self):
        session, mgr, _, _ = self.make()
        xx = mgr.resources["FAKE::XX"]
        xx.responses["SLVL?"] = "0.008"
        xx.responses["FREQEXT?"] = "0"
        xx.responses["RSRC?"] = "0"
        session.open()
        result = session.protect_source()
        self.assertTrue(result["verified"])
        self.assertEqual(xx.writes, ["SLVL 0.004"])
        xx.responses["FREQEXT?"] = "50027"
        session.configure()
        self.assertEqual(xx.responses["RSRC?"], "1")
        session.cleanup()
        session.close()

    def test_nonzero_source_dc_rejects_before_configuration_writes(self):
        session, mgr, _, _ = self.make()
        mgr.resources["FAKE::XX"].responses["SOFF?"] = "0.001"
        with self.assertRaises(ValueError):
            session.open()
        self.assertEqual(mgr.resources["FAKE::XX"].writes, [])

    def test_lock_loss_rejects_formal_sample_and_cleanup_reduces_source(self):
        session, mgr, _, _ = self.ready()
        mgr.resources["FAKE::XX"].responses["CUROVLDSTAT?"] = "8"
        with self.assertRaises(ValueError):
            session.sample_point()
        with self.assertRaises(RuntimeError):
            session.sample_point()
        result = session.cleanup()
        self.assertTrue(result["source_protection_verified"])
        self.assertEqual(mgr.resources["FAKE::XX"].responses["SLVL?"], "0.004")
        session.close()

    def test_reference_drift_rejects_before_new_harmonic_writes(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        writes = len(xx.writes)
        xx.responses["FREQEXT?"] = "52000"
        with self.assertRaises(ValueError):
            session.sample_point()
        self.assertEqual(len(xx.writes), writes)
        session.cleanup()
        session.close()

    def test_pair_reference_disagreement_rejects(self):
        session, mgr, _, _ = self.ready()
        mgr.resources["FAKE::XY"].responses["FREQ?"] = "50030"
        with self.assertRaises(ValueError):
            session.read_coordinates()
        session.cleanup()
        session.close()

    def test_front_panel_filter_change_cannot_be_accepted(self):
        session, mgr, _, _ = self.ready()
        mgr.resources["FAKE::XX"].responses["ADVFILT?"] = "1"
        with self.assertRaises(ValueError):
            session.sample_point()
        session.cleanup()
        session.close()

    def test_zero_xy_retains_undefined_phase_and_valid_voltage_metrics(self):
        session, mgr, _, _ = self.ready()
        mgr.resources["FAKE::XX"].responses["SNAP? X,Y"] = "0,0"
        sample = session.sample_point()
        self.assertTrue(sample["valid_for_analysis"])
        self.assertEqual(sample["measurements"]["lockin_xx_h1_amplitude_v"], 0)
        self.assertNotIn("lockin_xx_h1_phase_deg", sample["measurements"])
        self.assertIsNone(sample["status"]["samples"][0]["samples"]["xx"]["phase_deg"])
        session.cleanup()
        session.close()

    def test_companion_failure_retains_first_role_and_failed_command_audit(self):
        session, mgr, events, _ = self.ready()
        mgr.resources["FAKE::XY"].responses["SENS?"] = TimeoutError("injected companion read failure")
        with self.assertRaises(TimeoutError):
            session.sample_point()
        audits = [payload for kind, payload in events if kind == "lockin_model_io"]
        self.assertTrue(any(item["error"] for payload in audits for item in payload["raw"]))
        cleanup = session.cleanup()
        self.assertTrue(cleanup["source_protection_verified"])
        self.assertFalse(cleanup["verified"])
        session.close()

    def test_cleanup_does_not_increase_already_lower_source_or_hide_dc(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        xx.responses["SLVL?"] = "0.002"
        xx.responses["SOFF?"] = "0.001"
        count = len(xx.writes)
        cleanup = session.cleanup()
        self.assertFalse(cleanup["verified"])
        self.assertFalse(cleanup["source_protection_verified"])
        self.assertEqual(len(xx.writes), count)
        self.assertEqual(xx.responses["SLVL?"], "0.002")
        session.close()

    def test_cleanup_reduces_ac_even_if_nonzero_dc_appears_then_reports_failure(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        xx.responses["SOFF?"] = "0.001"
        cleanup = session.cleanup()
        self.assertEqual(xx.responses["SLVL?"], "0.004")
        self.assertFalse(cleanup["verified"])
        self.assertTrue(cleanup["manual_verification_required"])
        session.close()

    def test_audit_failure_during_cleanup_does_not_prevent_source_protection(self):
        session, mgr, _, _ = self.ready()
        session.event = lambda *_: (_ for _ in ()).throw(OSError("disk failure"))
        result = session.cleanup()
        self.assertEqual(mgr.resources["FAKE::XX"].responses["SLVL?"], "0.004")
        self.assertTrue(result["source_protection_verified"])
        self.assertFalse(result["verified"])
        session.close()

    def test_external_frequency_coordinate_cannot_write_new_frequency(self):
        session, mgr, _, _ = self.ready()
        writes = [len(r.writes) for r in mgr.resources.values()]
        with self.assertRaises(ValueError):
            session.set_point(0.006, 51000)
        self.assertEqual(writes, [len(r.writes) for r in mgr.resources.values()])
        session.cleanup()
        session.close()

    def test_context_brackets_each_formal_pair(self):
        session, _, _, _ = self.ready()
        calls = []
        session.sample_point(measurement_context=lambda *args: calls.append(args) or {"temperature_k": 4})
        self.assertEqual([args[0] for args in calls], ["before", "after", "before", "after"])
        session.cleanup()
        session.close()

    def test_unknown_status_cannot_be_cleared_away_as_configuration_transition(self):
        session, mgr, _, _ = self.make()
        session.open()
        mgr.resources["FAKE::XY"].responses["LIAS?"] = "128"
        # Preserve the injected unknown bit while configuration writes occur.
        mgr.resources["FAKE::XY"].on_query = lambda r, command: (
            r.responses.__setitem__("LIAS?", "128") if command == "LIAS?" else None)
        with self.assertRaises(ValueError):
            session.configure()
        self.assertFalse(session.configured)
        session.cleanup()
        session.close()

    def test_input_overload_latch_cannot_be_ignored_after_current_status_recovers(self):
        session, mgr, _, _ = self.make()
        session.open()
        mgr.resources["FAKE::XX"].responses["LIAS?"] = "16"
        with self.assertRaises(ValueError):
            session.configure()
        session.cleanup()
        session.close()

    def test_output_write_timeout_requires_manual_verification_when_cleanup_also_fails(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]

        def fail_source(resource, command):
            if command.startswith("SLVL "):
                raise TimeoutError("injected output command timeout")

        xx.on_write = fail_source
        result = session.cleanup()
        self.assertFalse(result["verified"])
        self.assertFalse(result["source_protection_verified"])
        self.assertTrue(result["manual_verification_required"])
        self.assertEqual(xx.responses["SLVL?"], "0.006")
        self.assertIs(session.cleanup(), result)
        session.close()

    def test_partial_resource_open_closes_already_open_first_resource(self):
        session, mgr, _, _ = self.make()
        original = mgr.open_resource

        def fail_second(address):
            if address == "FAKE::XY":
                raise OSError("injected resource-open failure")
            return original(address)

        mgr.open_resource = fail_second
        with self.assertRaises(OSError):
            session.open()
        self.assertTrue(mgr.resources["FAKE::XX"].closed)
        self.assertTrue(mgr.closed)

    def test_source_change_inside_formal_read_is_rejected(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        counter = {"snap": 0}

        def change_after_read(resource, command):
            if command == "SNAP? X,Y":
                counter["snap"] += 1
                if counter["snap"] == 2:
                    resource.responses["SLVL?"] = "0.007"

        xx.on_query = change_after_read
        with self.assertRaises(ValueError):
            session.sample_point()
        self.assertTrue(session.failed)
        session.cleanup()
        session.close()

    def test_optical_inner_transition_protects_then_restores_outer_excitation(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        session.prepare_reference_transition()
        self.assertEqual(xx.responses["SLVL?"], "0.004")
        self.assertEqual(session.read_coordinates()["lockin_excitation_v_rms"], 0.004)
        self.assertEqual(session.reference_transition_target, 0.006)
        with self.assertRaises(RuntimeError):
            session.sample_point()
        # A transient unlock latch from the known PEM setting transition is
        # retained, then a complete fresh clean interval is required.
        xx.responses["LIAS?"] = "8"
        session.restore_source_after_reference_transition()
        self.assertEqual(xx.responses["SLVL?"], "0.006")
        self.assertFalse(session.reference_transition_pending)
        session.qualify()
        self.assertTrue(session.sample_point()["clean"])
        session.cleanup()
        session.close()

    def test_optical_outer_transition_defers_inner_excitation_until_reference_verified(self):
        session, mgr, _, _ = self.make()
        session.open()
        session.configure()
        xx = mgr.resources["FAKE::XX"]
        session.prepare_reference_transition()
        session.set_point(0.006)
        self.assertEqual(xx.responses["SLVL?"], "0.004")
        self.assertEqual(session.reference_transition_target, 0.006)
        session.restore_source_after_reference_transition()
        self.assertEqual(xx.responses["SLVL?"], "0.006")
        self.assertTrue(session.sample_point()["clean"])
        session.cleanup()
        session.close()

    def test_reference_failure_during_optical_transition_never_restores_high_excitation(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        session.prepare_reference_transition()
        previous_writes = len(xx.writes)
        xx.responses["FREQEXT?"] = "52000"
        with self.assertRaises(ValueError):
            session.restore_source_after_reference_transition()
        self.assertEqual(xx.responses["SLVL?"], "0.004")
        self.assertEqual(len(xx.writes), previous_writes)
        session.cleanup()
        session.close()

    def test_no_write_cleanup_does_not_claim_source_protection(self):
        session, mgr, _, _ = self.make()
        session.open()
        cleanup = session.cleanup()
        self.assertTrue(cleanup["verified"])
        self.assertFalse(cleanup["source_protection_verified"])
        self.assertEqual(mgr.resources["FAKE::XX"].writes, [])
        session.close()

    def test_cleanup_rejects_changed_xx_serial_before_any_excitation_write(self):
        session, mgr, _, _ = self.ready()
        xx = mgr.resources["FAKE::XX"]
        previous_writes = len(xx.writes)
        xx.responses["*IDN?"] = "Stanford_Research_Systems,SR865A,replacement,v1.00"
        cleanup = session.cleanup()
        self.assertFalse(cleanup["source_protection_verified"])
        self.assertTrue(cleanup["manual_verification_required"])
        self.assertEqual(len(xx.writes), previous_writes)
        session.close()


if __name__ == "__main__":
    unittest.main()
