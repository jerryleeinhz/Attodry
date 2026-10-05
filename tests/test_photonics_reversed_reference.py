"""Synthetic tests for preserved XY sine reference and SR830 sample excitation."""
import unittest
from types import SimpleNamespace

from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.photonics_lockin_helpers import FakeManager, reversed_sine_document, reversed_sine_safety_document


class ReversedReferenceTests(unittest.TestCase):
    def load(self, doc=None):
        return load_photonics_lockin_config("fake.toml", document=reversed_sine_document() if doc is None else doc,
            safety_document=reversed_sine_safety_document())

    def make(self, document=None):
        doc = reversed_sine_document() if document is None else document
        manager = FakeManager(doc)
        xy = manager.resources["FAKE::XY"]
        xy.responses["SLVL?"] = "0.2"
        xy.responses["BLAZEX?"] = "0"
        waits, events = [], []
        session = PhotonicsLockinPointSession("fake.toml", self.load(doc),
            lambda kind, payload: events.append((kind, payload)), manager_factory=lambda: manager, sleep=waits.append)
        self.addCleanup(session.close)
        return session, manager, events, waits

    def ready(self):
        session, manager, events, waits = self.make()
        session.open()
        session.configure()
        session.set_point(0.006)
        return session, manager, events, waits

    def test_explicit_roles_physical_connection_and_source_definitions(self):
        cfg = self.load()
        self.assertEqual(cfg.reference_topology, "pem_xy_xx_sine")
        self.assertEqual((cfg.lockin_xx.model, cfg.lockin_xy.model), ("SR830", "SR865A"))
        self.assertEqual(cfg.lockin_xx.reference_source, "external_sine")
        self.assertEqual(cfg.lockin_xx.external_reference_edge, "sine_zero_crossing")
        self.assertTrue(cfg.lockin_xy.sine_output_connected)
        self.assertFalse(cfg.reference_output.sample_connected)
        self.assertEqual(cfg.source.dc_mode, "not_supported")
        self.assertEqual(cfg.source.amplitude_definition, "sr830_instrument_rms_setting")

    def test_wrong_model_reference_trigger_or_disconnected_declaration_reject(self):
        for role, key, value in (
            ("lockin_xx", "model", "SR865A"), ("lockin_xy", "model", "SR830"),
            ("lockin_xx", "reference_source", "external_ttl"),
            ("lockin_xx", "external_reference_edge", "rising"),
            ("lockin_xy", "reference_source", "external_sine"),
            ("lockin_xy", "sine_output_connected", False),
        ):
            doc = reversed_sine_document()
            doc[role][key] = value
            with self.subTest(role=role, key=key), self.assertRaises(ValueError):
                self.load(doc)

    def test_reference_output_requires_explicit_read_only_contract(self):
        for key in reversed_sine_document()["photonics_lockin"]["reference_output"]:
            doc = reversed_sine_document()
            del doc["photonics_lockin"]["reference_output"][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(doc)
        for key, value in (("sample_connected", True), ("sample_connected", 0), ("load", "50_ohm"),
                           ("destination", "sample"), ("dc_offset_v", 0.01), ("amplitude_v_rms", True)):
            doc = reversed_sine_document()
            doc["photonics_lockin"]["reference_output"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.load(doc)

    def test_reversed_sr830_still_cannot_measure_h3_at_50_khz(self):
        doc = reversed_sine_document()
        doc["lockin_xx"]["harmonics"] = [3]
        with self.assertRaises(ValueError):
            self.load(doc)
        doc = reversed_sine_document()
        doc["lockin_xy"]["harmonics"] = [2, 3]
        self.assertEqual(self.load(doc).lockin_xy.harmonics, (2, 3))

    def test_preserve_blazex_required_and_ac_scope_unchanged(self):
        for table, key, value in (("sr865a", "sync_output_mode", "unipolar_sync"),
                                  (None, "input_coupling", "dc")):
            doc = reversed_sine_document()
            target = doc["lockin_xy"] if table is None else doc["lockin_xy"][table]
            target[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(doc)

    def test_reference_amplitude_mismatch_preflight_closes_without_any_write(self):
        session, manager, _, _ = self.make()
        manager.resources["FAKE::XY"].responses["SLVL?"] = "0.1"
        with self.assertRaisesRegex(ValueError, "XY SINE"):
            session.open()
        self.assertTrue(manager.closed)
        self.assertTrue(all(not resource.writes for resource in manager.resources.values()))

    def test_explicit_ground_is_preserved_in_measurement_configuration_and_verified(self):
        doc = reversed_sine_document()
        doc["lockin_xy"]["shield_grounding"] = "ground"
        doc["photonics_lockin"]["reference_output"].update(amplitude_v_rms=0.4, dc_mode="difference")
        session, manager, _, _ = self.make(doc)
        xy = manager.resources["FAKE::XY"]
        xy.responses["IGND?"] = "1"
        xy.responses["SLVL?"], xy.responses["REFM?"] = "0.40000000596", "1"
        session.open()
        session.configure()
        session.set_point(0.006)
        self.assertTrue(session.sample_point()["clean"])
        self.assertNotIn("IGND 0", xy.writes)
        self.assertFalse(any(command.startswith(("SLVL ", "SOFF ", "REFM ")) for command in xy.writes))
        self.assertEqual(xy.responses["IGND?"], "1")
        xy.responses["IGND?"] = "0"
        with self.assertRaises(ValueError):
            session.sample_point()
        self.assertTrue(session.cleanup()["source_protection_verified"])

    def test_pem_crosscheck_rejects_xy_outside_pem_tolerance_even_when_pair_agrees(self):
        from attodry_control.combination_hardware import HardwareCombinationStation
        # Each chain link differs by <= 1 Hz; total PEM-to-XY differs by 1.5 Hz.
        config = SimpleNamespace(reference_topology="pem_xy_xx_sine", lockin=self.load())
        station = HardwareCombinationStation(config)
        station.set_event_sink(lambda *_: None)
        station.lockin = SimpleNamespace(read_reference_frequencies=lambda: {"xx": 50027.75, "xy": 50028.5})
        station.optical = SimpleNamespace(pem=object(), last_state={"pem": {"frequency_hz": 50027.0}})
        with self.assertRaisesRegex(ValueError, "PEM frequency"):
            station._verify_optical_reference()

    def test_sine_trigger_and_sampling_never_write_xy_source_or_blazex(self):
        session, manager, events, _ = self.ready()
        sample = session.sample_point()
        self.assertTrue(sample["clean"])
        self.assertIn("lockin_xx_h1_x_v", sample["measurements"])
        self.assertIn("lockin_xy_h2_x_v", sample["measurements"])
        self.assertIn("RSLP 0", manager.resources["FAKE::XX"].writes)
        self.assertTrue(any(payload.get("reference_topology") == "pem_xy_xx_sine" for _, payload in events))
        cleanup = session.cleanup()
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["reference_output_preserved"])
        self.assertEqual(manager.resources["FAKE::XY"].responses["SLVL?"], "0.2")
        self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.004")
        for command in manager.resources["FAKE::XY"].writes:
            self.assertFalse(command.startswith(("SLVL ", "SOFF ", "REFM ", "BLAZEX ", "FREQ ")))
        self.assertFalse(any(command.startswith("FREQ ") or command == "FMOD 1"
            for command in manager.resources["FAKE::XX"].writes))

    def test_changed_xy_reference_blocks_new_excitation_but_cleanup_still_reduces_xx(self):
        for query, value in (("SLVL?", "0.1"), ("SOFF?", "0.01"), ("REFM?", "1")):
            session, manager, _, _ = self.ready()
            xx, xy = manager.resources["FAKE::XX"], manager.resources["FAKE::XY"]
            xy.responses[query] = value
            before = len(xx.writes)
            with self.subTest(query=query), self.assertRaises(ValueError):
                session.set_point(0.004)
            self.assertEqual(len(xx.writes), before)
            cleanup = session.cleanup()
            self.assertTrue(cleanup["source_protection_verified"])
            self.assertFalse(cleanup["reference_output_preserved"])
            self.assertFalse(cleanup["verified"])
            self.assertEqual(xx.responses["SLVL?"], "0.004")
            self.assertFalse(any(command.startswith(("SLVL ", "SOFF ", "REFM ")) for command in xy.writes))

    def test_changed_unused_blazex_mode_is_detected_without_restoration_write(self):
        session, manager, _, _ = self.ready()
        xy = manager.resources["FAKE::XY"]
        xy.responses["BLAZEX?"] = "2"
        with self.assertRaises(ValueError):
            session.sample_point()
        self.assertFalse(any(command.startswith("BLAZEX ") for command in xy.writes))
        cleanup = session.cleanup()
        self.assertTrue(cleanup["source_protection_verified"])
        self.assertFalse(cleanup["verified"])

    def test_reference_pair_drift_and_unlock_reject_formal_sample(self):
        for query, value in (("FREQEXT?", "50030"), ("CUROVLDSTAT?", "8")):
            session, manager, _, _ = self.ready()
            manager.resources["FAKE::XY"].responses[query] = value
            with self.subTest(query=query), self.assertRaises(ValueError):
                session.sample_point()
            self.assertTrue(session.cleanup()["source_protection_verified"])

    def test_optical_reference_transition_preserves_xy_source_and_restores_only_xx(self):
        session, manager, _, _ = self.ready()
        session.prepare_reference_transition()
        self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.004")
        self.assertEqual(manager.resources["FAKE::XY"].responses["SLVL?"], "0.2")
        session.restore_source_after_reference_transition()
        self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.006")
        self.assertTrue(session.sample_point()["clean"])
        self.assertTrue(session.cleanup()["verified"])


if __name__ == "__main__":
    unittest.main()
