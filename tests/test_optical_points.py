from dataclasses import replace
import unittest

from optical_helpers import EXAMPLE, ROOT, devices
from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.optical_points import OpticalPointConfig, OpticalPointSession, load_optical_point_config


class OpticalPointSessionTests(unittest.TestCase):
    def make(self, *, source_current=False, events=None):
        clock, nkt, scan, backend, pem, meter = devices()
        if source_current:
            cfg = load_optical_point_config(ROOT / "config/optical_source_current_simulation.toml")
            nkt, scan = cfg.nkt, cfg.scan
            backend = SimulatedNkt(nkt)
            pem.config, meter.config = cfg.pem, cfg.pm
            meter.resource.power_provider = lambda: (2 * scan.feedback.max_power_w *
                (backend.source["level_pct"] / nkt.max_level_pct) ** 2
                if backend.source["emission_state"] == 3 else 0)
        else:
            cfg = OpticalPointConfig(nkt, scan, pem.config, meter.config)
        session = OpticalPointSession(cfg, events, backend_factory=lambda _: backend,
            pem_factory=lambda _: pem, meter_factory=lambda _: meter, clock=clock, sleep=clock.sleep)
        return session, backend, pem, meter, clock

    def start(self, session, index=0):
        session.open()
        session.configure()
        session.set_point(index)
        session.qualify()

    def test_loading_and_construction_do_not_open_resources(self):
        cfg = load_optical_point_config(EXAMPLE)
        calls = []
        OpticalPointSession(cfg, backend_factory=lambda _: calls.append("opened"))
        self.assertEqual(calls, [])
        self.assertEqual(len(cfg.points), len(cfg.nkt.points))

    def test_prepare_leaves_off_until_explicit_qualification_barrier(self):
        session, backend, pem, meter, _ = self.make()
        session.open()
        self.assertFalse(any(command[0] in {"configure", "emission"} for command in backend.commands))
        session.configure()
        session.set_point(0)
        self.assertFalse(any(command == ("emission", True) for command in backend.commands))
        self.assertEqual(backend.source["emission_state"], 0)
        self.assertIs(pem.output_command, True)
        session.qualify()
        self.assertEqual(backend.source["emission_state"], 3)
        session.cleanup()

    def test_one_sample_has_exact_coordinates_and_fresh_power_brackets(self):
        session, _, _, meter, _ = self.make()
        self.start(session)
        session.begin_sample()
        row = session.read()
        session.end_sample([row])
        self.assertEqual(set(row["actual"]), set(session.config.points[0]))
        self.assertEqual(set(row["measurements"]), {"optical_power_w", "pem_frequency_hz"})
        evidence = row["status"]["sample_brackets"]
        self.assertEqual([e["phase"] for e in evidence], ["before_electrical", "formal", "after_electrical"])
        sequences = [e["power_sample"]["sequence"] for e in evidence]
        self.assertEqual(sequences, sorted(set(sequences)))
        session.begin_sample()
        self.assertGreater(session.read()["status"]["power_sample"]["sequence"], sequences[-1])
        session.end_sample()
        session.cleanup()

    def test_wavelength_change_updates_pem_retardance_and_meter_correction(self):
        session, backend, pem, meter, _ = self.make()
        self.start(session)
        session.suspend()
        session.set_point(1)
        self.assertEqual(backend.source["emission_state"], 0)
        wavelength = session.config.nkt.points[1].wavelength_nm
        self.assertAlmostEqual(pem.target_nm, wavelength * session.config.scan.peak_retardance_waves)
        self.assertEqual(meter.target_nm, wavelength)
        session.qualify()
        session.cleanup()

    def test_suspend_before_other_axis_change_preserves_reference(self):
        session, backend, pem, _, _ = self.make()
        self.start(session)
        session.suspend()
        self.assertEqual(backend.source["emission_state"], 0)
        self.assertIs(pem.output_command, True)
        with self.assertRaises(NktError):
            session.begin_sample()
        session.qualify()
        session.cleanup()

    def test_source_current_feedback_resolves_each_target_without_nd_changes(self):
        session, backend, _, _, _ = self.make(source_current=True)
        original_nd, original_pp = backend.filter["nd_pct"], backend.source["pulse_picker_ratio"]
        session.open()
        session.configure()
        for index in range(len(session.config.points)):
            session.set_point(index)
            session.qualify()
            session.begin_sample()
            row = session.read()
            session.end_sample([row])
            target = session.config.points[index]["optical_target_power_w"]
            self.assertLessEqual(abs(row["measurements"]["optical_power_w"] - target), target * 0.1 + 1e-18)
            self.assertEqual(backend.filter["nd_pct"], original_nd)
            self.assertEqual(backend.source["pulse_picker_ratio"], original_pp)
            self.assertTrue(row["status"]["feedback"]["source_current_iterations"])
        session.cleanup()

    def test_formal_power_drift_rejects_without_feedback_writes(self):
        session, backend, _, meter, _ = self.make(source_current=True)
        self.start(session)
        session.begin_sample()
        writes = len([c for c in backend.commands if c[0] in {"configure", "emission"}])
        meter.resource.power_provider = lambda: session.feedback.target_power_w * 1.2
        with self.assertRaisesRegex(NktError, "Formal optical power"):
            session.end_sample()
        self.assertFalse(session.qualified)
        self.assertFalse(session.sample_active)
        self.assertEqual(len([c for c in backend.commands if c[0] in {"configure", "emission"}]), writes)
        session.cleanup()

    def test_state_change_after_electrical_rejects_condition(self):
        session, backend, _, _, _ = self.make()
        self.start(session)
        session.begin_sample()
        row = session.read()
        backend.source["level_pct"] += 1
        with self.assertRaises(NktError):
            session.end_sample([row])
        self.assertFalse(session.qualified)
        self.assertFalse(session.sample_active)
        session.cleanup()

    def test_changes_inside_formal_sample_are_forbidden(self):
        session, _, _, _, _ = self.make()
        self.start(session)
        session.begin_sample()
        for action in (session.suspend, lambda: session.set_point(0), session.qualify):
            with self.assertRaises(NktError):
                action()
        session.end_sample()
        session.cleanup()

    def test_cleanup_can_defer_pem_until_excitation_is_protected(self):
        session, backend, pem, meter, _ = self.make()
        self.start(session)
        first = session.cleanup(finish_pem=False)
        self.assertTrue(first["off_confirmed"])
        self.assertTrue(first["reference_cleanup_pending"])
        self.assertTrue(first["verified"])
        self.assertEqual(backend.source["emission_state"], 0)
        self.assertIs(pem.output_command, True)
        self.assertFalse(pem.closed)
        self.assertTrue(meter.closed)
        final = session.close()
        self.assertFalse(final["reference_cleanup_pending"])
        self.assertTrue(final["actions"]["pem"]["disable_acknowledged"])
        self.assertFalse(final["actions"]["pem"]["physical_off_confirmed"])
        commands = list(backend.commands)
        session.cleanup()
        self.assertEqual(backend.commands, commands)

    def test_cleanup_attempts_later_devices_when_off_fails(self):
        session, backend, pem, meter, _ = self.make()
        self.start(session)
        def fail(_):
            raise OSError("OFF disconnected")
        backend.set_emission = fail
        result = session.cleanup()
        self.assertFalse(result["verified"])
        self.assertFalse(result["off_confirmed"])
        self.assertTrue(pem.closed)
        self.assertTrue(meter.closed)
        self.assertIn("OFF disconnected", str(result["errors"]))

    def test_audit_failure_cannot_prevent_cleanup(self):
        session, _, pem, meter, _ = self.make()
        self.start(session)
        def fail(*_):
            raise OSError("audit disk full")
        session.event_sink = fail
        result = session.cleanup()
        self.assertFalse(result["verified"])
        self.assertTrue(pem.closed)
        self.assertTrue(meter.closed)

    def test_failed_power_sample_retains_raw_transcript_in_cleanup(self):
        session, _, _, meter, _ = self.make()
        self.start(session)
        session.begin_sample()
        meter.resource.power_provider = lambda: float("nan")
        with self.assertRaises(NktError):
            session.read()
        result = session.cleanup()
        self.assertTrue(any(row["command"] == "READ?" and row.get("reply") == "nan"
                            for row in result["device_transcripts"]["pm100d"]))
        self.assertFalse(result["state_current"])
        self.assertTrue(result["point_evidence"])

    def test_close_preserves_pem_reference_when_excitation_cleanup_is_unknown(self):
        session, backend, pem, _, _ = self.make()
        self.start(session)
        session.cleanup(finish_pem=False)
        result = session.close(finish_pem=False)
        self.assertTrue(result["off_confirmed"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["reference_left_active_or_unknown"])
        self.assertTrue(result["manual_verification_required"])
        self.assertIs(pem.output_command, True)
        self.assertTrue(pem.closed)
        self.assertEqual(result["actions"]["pem"]["action"], "close_without_disable")
        self.assertFalse(session.close()["verified"])
        self.assertIs(pem.output_command, True)

    def test_preflight_on_source_is_not_taken_over_or_turned_off(self):
        session, backend, pem, meter, _ = self.make()
        backend.source.update(emission_state=3, status_bits=1)
        with self.assertRaisesRegex(NktError, "no automatic takeover"):
            session.open()
        result = session.cleanup()
        self.assertFalse(result["off_confirmed"])
        self.assertFalse(any(command[0] == "emission" for command in backend.commands))
        self.assertTrue(pem.closed)
        self.assertTrue(meter.closed)

    def test_hardware_authorization_checked_before_factories(self):
        cfg = load_optical_point_config(EXAMPLE)
        cfg = replace(cfg, nkt=replace(cfg.nkt, backend="nkt_sdk", port="SIMULATED-NKT"),
                      pem=replace(cfg.pem, backend="serial", port="SIMULATED-PEM"), pm=replace(cfg.pm, backend="visa"))
        calls = []
        session = OpticalPointSession(cfg, backend_factory=lambda _: calls.append("connected"))
        with self.assertRaisesRegex(NktError, "explicit authorization"):
            session.open()
        self.assertEqual(calls, [])

    def test_no_meter_never_fabricates_watts(self):
        session, _, _, _, _ = self.make()
        session.config = replace(session.config, scan=replace(session.config.scan, use_power_meter=False), pm=None)
        self.start(session)
        session.begin_sample()
        row = session.read()
        session.end_sample([row])
        self.assertNotIn("optical_power_w", row["measurements"])
        self.assertIsNone(row["status"]["measurement_plane"])
        session.cleanup()

    def test_mixed_backend_and_coordinate_shape_are_rejected_without_io(self):
        cfg = load_optical_point_config(EXAMPLE)
        with self.assertRaisesRegex(NktError, "one hardware/simulation mode"):
            replace(cfg, pem=replace(cfg.pem, backend="serial")).validate()
        points = list(cfg.nkt.points)
        points[1] = replace(points[1], nd_pct=None)
        with self.assertRaisesRegex(NktError, "identical coordinate keys"):
            replace(cfg, nkt=replace(cfg.nkt, points=tuple(points))).validate()


if __name__ == "__main__":
    unittest.main()
