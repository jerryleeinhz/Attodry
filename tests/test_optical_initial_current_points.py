"""Fake optical lifecycle checks for target-specific and inherited starts."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from optical_helpers import ROOT, devices
from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.optical_points import OpticalPointSession, load_optical_point_config


class OpticalInitialCurrentPointTests(unittest.TestCase):
    def make(self, mode="previous", *, rows=1, levels=None, policy="abort"):
        clock, _, _, _, pem, meter = devices()
        original = load_optical_point_config(ROOT / "config/optical_source_current_simulation.toml")
        points = (original.nkt.points[0],)
        if rows == 2:
            points += (replace(points[0], wavelength_nm=650.0, source_level_pct=12.0),)
        feedback = replace(original.scan.feedback, target_mapping="cartesian",
            target_powers_w=(5e-6, 7.5e-6, 10e-6), initial_current_mode=mode,
            initial_source_levels_pct=levels)
        cfg = replace(original, nkt=replace(original.nkt, points=points),
            scan=replace(original.scan, feedback=feedback, target_deviation_policy=policy))
        backend = SimulatedNkt(cfg.nkt)
        pem.config, meter.config = cfg.pem, cfg.pm
        # The curve is deliberately unlike the stock quadratic simulation.
        # A 5 uW target qualifies at an observed 10%, not the configured 8%.
        meter.resource.power_provider = lambda: (
            0.5e-6 * backend.source["level_pct"]
            if backend.source["emission_state"] == 3 else 0.0)
        events = []
        session = OpticalPointSession(cfg, lambda kind, value: events.append((kind, value)),
            backend_factory=lambda _: backend, pem_factory=lambda _: pem,
            meter_factory=lambda _: meter, clock=clock, sleep=clock.sleep)
        self.addCleanup(session.cleanup)
        session.open()
        session.configure()
        return session, backend, pem, meter, clock, events

    def qualified(self, session, index):
        session.set_point(index)
        self.assertFalse(session.qualified)
        session.qualify()
        self.assertTrue(session.qualified)
        self.assertTrue(session.row["feedback_result"]["power_window_stable"])
        self.assertTrue(session.row["feedback_result"]["target_in_tolerance"])

    def test_per_target_starts_are_static_coordinates_and_real_prepared_settings(self):
        levels = (10.0, 14.0, 18.0)
        session, backend, pem, meter, _, events = self.make("per_target", levels=levels)
        self.assertEqual([p.source_level_pct for p in session.config.nkt_points], list(levels))
        self.assertEqual([p["optical_source_level_pct"] for p in session.config.points], list(levels))
        for index, level in enumerate(levels):
            session.set_point(index)
            self.assertEqual(backend.source["emission_state"], 0)
            self.assertEqual(backend.source["level_pct"], level)
            self.assertEqual(meter.target_nm, 633.0)
            self.assertEqual(pem.target_nm, 633.0 * 0.25)
            start = session.row["initial_current"]
            self.assertEqual(start["source"], "per_target")
            self.assertEqual(start["input_point_index"], 0)
            self.assertEqual(start["target_index"], index)
            self.assertEqual(start["selected_source_level_pct"], level)
            self.assertEqual(start["configured_source_level_pct"], 8.0)
            self.assertIsNone(start["previous_point_index"])
            session.qualify()
            self.assertTrue(session.qualified)
        prepared = [value for kind, value in events if kind == "optical_point_prepared"]
        self.assertEqual(len(prepared), 3)

    def test_grid_preserves_row_then_target_order_and_wavelength_preparations(self):
        levels = ((10.0, 14.0, 18.0), (12.0, 16.0, 20.0))
        session, backend, pem, meter, _, _ = self.make("grid", rows=2, levels=levels)
        fixed_nd = backend.filter["nd_pct"]
        fixed_pp = backend.source["pulse_picker_ratio"]
        expected = [(wave, target, levels[row][column])
            for row, wave in enumerate((633.0, 650.0))
            for column, target in enumerate((5e-6, 7.5e-6, 10e-6))]
        self.assertEqual([(p["optical_wavelength_nm"], p["optical_target_power_w"],
                           p["optical_source_level_pct"]) for p in session.config.points], expected)
        self.assertEqual([p.source_level_pct for p in session.config.nkt_points],
                         [value for row in levels for value in row])
        for index, (wave, target, current) in enumerate(expected):
            session.set_point(index)
            self.assertEqual(backend.source["level_pct"], current)
            self.assertEqual(backend.source["emission_state"], 0)
            self.assertEqual(meter.target_nm, wave)
            self.assertEqual(pem.target_nm, wave * 0.25)
            self.assertEqual(session.feedback.target_power_w, target)
            self.assertEqual(session.row["initial_current"]["input_point_index"], index // 3)
            self.assertEqual(session.row["initial_current"]["target_index"], index % 3)
            self.assertEqual(session.row["initial_current"]["source"], "grid")
            session.qualify()
            self.assertTrue(session.qualified)
            self.assertEqual(backend.filter["nd_pct"], fixed_nd)
            self.assertEqual(backend.source["pulse_picker_ratio"], fixed_pp)

    def test_previous_uses_qualified_actual_current_and_requalifies_each_target(self):
        session, backend, _, _, _, _ = self.make()
        self.qualified(session, 0)
        actual = session.last_state["nkt"]["source"]["level_pct"]
        self.assertEqual(actual, 10.0)
        self.assertNotEqual(actual, session.config.nkt.points[0].source_level_pct)
        session.suspend()
        session.set_point(1)
        self.assertEqual(backend.source["level_pct"], actual)
        self.assertFalse(session.qualified)
        start = session.row["initial_current"]
        self.assertEqual(start["source"], "previous")
        self.assertEqual(start["previous_point_index"], 0)
        self.assertEqual(start["configured_source_level_pct"], 8.0)
        self.assertEqual(start["selected_source_level_pct"], actual)
        self.assertEqual(session.row["power_samples"], [])
        session.qualify()
        self.assertTrue(session.row["source_current_iterations"])
        self.assertTrue(session.row["feedback_result"]["target_in_tolerance"])
        second = session.last_state["nkt"]["source"]["level_pct"]
        self.assertGreater(second, actual)
        session.set_point(2)
        self.assertEqual(backend.source["level_pct"], second)
        self.assertEqual(session.row["initial_current"]["previous_point_index"], 1)
        session.qualify()
        self.assertTrue(session.row["feedback_result"]["target_in_tolerance"])

    def test_previous_resets_at_new_row_even_when_wavelengths_are_identical(self):
        session, backend, pem, meter, _, _ = self.make(rows=2)
        # The original row identity, not wavelength equality, bounds inheritance.
        same_wave = replace(session.config.nkt.points[1], wavelength_nm=633.0)
        session.config = replace(session.config,
            nkt=replace(session.config.nkt, points=(session.config.nkt.points[0], same_wave)))
        for index in range(3):
            self.qualified(session, index)
        self.assertGreater(backend.source["level_pct"], 12.0)
        session.set_point(3)
        self.assertEqual(backend.source["level_pct"], 12.0)
        self.assertEqual(session.row["initial_current"]["source"], "point")
        self.assertIsNone(session.row["initial_current"]["previous_point_index"])
        self.assertEqual(meter.target_nm, 633.0)
        self.assertEqual(pem.target_nm, 633.0 * 0.25)
        session.qualify()
        self.assertTrue(session.qualified)

    def test_previous_same_index_retry_and_skipped_index_use_original_start(self):
        for next_index in (0, 2):
            with self.subTest(next_index=next_index):
                session, backend, _, _, _, _ = self.make()
                self.qualified(session, 0)
                self.assertEqual(backend.source["level_pct"], 10.0)
                session.set_point(next_index)
                self.assertEqual(backend.source["level_pct"], 8.0)
                self.assertEqual(session.row["initial_current"]["source"], "point")
                self.assertIsNone(session.row["initial_current"]["previous_point_index"])
                session.qualify()
                self.assertTrue(session.qualified)

    def test_previous_guard_failure_discards_qualified_start_and_cleanup_confirms_off(self):
        session, backend, _, meter, _, _ = self.make()
        self.qualified(session, 0)
        provider = meter.resource.power_provider
        meter.resource.power_provider = lambda: session.feedback.target_power_w * 1.2
        with self.assertRaises(NktError):
            session.begin_sample()
        self.assertFalse(session.qualified)
        meter.resource.power_provider = provider
        session.suspend()
        session.set_point(1)
        self.assertEqual(backend.source["level_pct"], 8.0)
        self.assertEqual(session.row["initial_current"]["source"], "point")
        result = session.cleanup()
        self.assertTrue(result["off_confirmed"])
        self.assertTrue(result["verified"])
        self.assertEqual(backend.source["emission_state"], 0)

    def test_previous_prepare_failure_does_not_leak_inherited_start_into_retry(self):
        session, backend, pem, _, _, _ = self.make()
        self.qualified(session, 0)
        with patch.object(pem, "prepare", side_effect=OSError("synthetic PEM prepare fault")):
            with self.assertRaisesRegex(OSError, "PEM prepare fault"):
                session.set_point(1)
        self.assertFalse(session.qualified)
        self.assertEqual(backend.source["emission_state"], 0)
        session.set_point(1)
        self.assertEqual(backend.source["level_pct"], 8.0)
        self.assertEqual(session.row["initial_current"]["source"], "point")
        session.qualify()
        self.assertTrue(session.qualified)

    def test_previous_dark_guard_failure_discards_qualified_start(self):
        for operation in ("observe_dark", "guard_reference_recovery"):
            with self.subTest(operation=operation):
                session, backend, _, _, clock, _ = self.make()
                self.qualified(session, 0)
                session.suspend()
                with patch.object(session.engine, "_verify", side_effect=OSError("synthetic dark guard fault")):
                    with self.assertRaisesRegex(OSError, "dark guard fault"):
                        getattr(session, operation)(deadline=clock() + 1)
                session.set_point(1)
                self.assertEqual(backend.source["level_pct"], 8.0)
                self.assertEqual(session.row["initial_current"]["source"], "point")

    def test_previous_qualification_failure_resets_next_start_and_cleanup_confirms_off(self):
        session, backend, _, _, _, _ = self.make()
        self.qualified(session, 0)
        session.set_point(1)
        with patch.object(session.engine, "_stabilize", side_effect=NktError("synthetic tuning fault")):
            with self.assertRaisesRegex(NktError, "tuning fault"):
                session.qualify()
        self.assertFalse(session.qualified)
        session.suspend()
        session.set_point(2)
        self.assertEqual(backend.source["level_pct"], 8.0)
        self.assertEqual(session.row["initial_current"]["source"], "point")
        result = session.cleanup()
        self.assertTrue(result["off_confirmed"])
        self.assertTrue(result["verified"])
        self.assertEqual(backend.source["emission_state"], 0)

    def test_previous_formal_brackets_are_fresh_without_feedback_writes(self):
        session, backend, _, _, _, _ = self.make(policy="record_continue")
        self.qualified(session, 0)
        session.set_point(1)
        session.qualify()
        writes = [c for c in backend.commands if c[0] in {"configure", "emission"}]
        session.begin_sample()
        row = session.read()
        session.end_sample([row])
        self.assertEqual([c for c in backend.commands if c[0] in {"configure", "emission"}], writes)
        brackets = row["status"]["sample_brackets"]
        self.assertEqual([value["phase"] for value in brackets],
                         ["before_electrical", "formal", "after_electrical"])
        sequences = [value["power_sample"]["sequence"] for value in brackets]
        self.assertEqual(len(set(sequences)), 3)
        self.assertEqual(row["status"]["feedback"]["initial_current"]["source"], "previous")

    def test_previous_nan_meter_fault_retains_raw_evidence_and_confirms_laser_off(self):
        session, backend, pem, meter, _, _ = self.make()
        self.qualified(session, 0)
        session.set_point(1)
        session.qualify()
        self.assertEqual(session.row["initial_current"]["source"], "previous")
        writes = [c for c in backend.commands if c[0] in {"configure", "emission"}]
        meter.resource.power_provider = lambda: float("nan")
        with self.assertRaises(NktError):
            session.begin_sample()
        self.assertFalse(session.qualified)
        self.assertFalse(session.sample_active)
        self.assertEqual([c for c in backend.commands if c[0] in {"configure", "emission"}], writes)
        # Do not reset an uncertain meter or attempt another scan point.
        result = session.cleanup()
        self.assertTrue(result["off_confirmed"])
        self.assertEqual(backend.source["emission_state"], 0)
        self.assertTrue(meter.closed)
        self.assertTrue(pem.closed)
        self.assertTrue(any(value["command"] == "READ?" and value.get("reply") == "nan"
                            for value in result["device_transcripts"]["pm100d"]))
        self.assertTrue(result["point_evidence"])
        self.assertFalse(result["state_current"])

    def test_default_point_starts_preserve_scalar_per_row_behavior(self):
        session, backend, _, _, _, _ = self.make("point", rows=2)
        for index in range(6):
            session.set_point(index)
            base = (8.0, 12.0)[index // 3]
            self.assertEqual(backend.source["level_pct"], base)
            self.assertEqual(session.row["initial_current"]["source"], "point")
            self.assertEqual(session.config.points[index]["optical_source_level_pct"], base)
            session.qualify()
            self.assertTrue(session.qualified)


if __name__ == "__main__":
    unittest.main()
