from dataclasses import replace
import unittest
from unittest.mock import Mock

from optical_helpers import EXAMPLE, ROOT, devices
from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.optical_points import OpticalPointConfig, OpticalPointSession, load_optical_point_config


class OpticalPointSessionTests(unittest.TestCase):
    def make(self, *, source_current=False, events=None, target_policy="abort"):
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
        cfg = replace(cfg, scan=replace(cfg.scan, target_deviation_policy=target_policy))
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

    def test_boundary_drift_after_tuning_records_actual_watts_without_retuning(self):
        for policy in ("abort", "record_continue"):
            with self.subTest(policy=policy):
                session, backend, _, meter, _ = self.make(source_current=True, target_policy=policy)
                self.addCleanup(session.cleanup)
                meter.config = replace(meter.config, max_power_w=0.002)
                feedback = replace(session.config.scan.feedback, max_power_w=0.002,
                                   target_powers_w=(0.001,) * 3)
                session.config = replace(session.config, pm=meter.config,
                    scan=replace(session.config.scan, feedback=feedback))
                meter.resource.power_provider = lambda: 0.000901547726
                session.open()
                session.configure()
                session.set_point(0)
                stabilize = session.engine._stabilize
                writes_at_tuning_finish = []
                def stabilized(*args, **kwargs):
                    result = stabilize(*args, **kwargs)
                    meter.resource.power_provider = lambda: 0.00089845655
                    writes_at_tuning_finish.append([c for c in backend.commands if c[0] in {"configure", "emission"}])
                    return result
                session.engine._stabilize = stabilized
                if policy == "abort":
                    with self.assertRaisesRegex(NktError, "Formal optical power left"):
                        session.qualify()
                    self.assertFalse(session.qualified)
                else:
                    session.qualify()
                    self.assertAlmostEqual(session.row["feedback_result"]["mean_power_w"], 0.000901547726)
                    session.begin_sample()
                    row = session.read()
                    session.end_sample([row])
                    self.assertEqual(row["actual"]["optical_target_power_w"], 0.001)
                    self.assertAlmostEqual(row["measurements"]["optical_power_w"], 0.00089845655)
                    assessment = row["status"]["power_target_assessment"]
                    self.assertFalse(assessment["target_in_tolerance"])
                    self.assertTrue(assessment["target_deviation_continued"])
                    self.assertAlmostEqual(assessment["target_deviation_w"], -0.00010154345)
                    self.assertTrue(row["clean"])
                    self.assertEqual(row["problems"], [])
                    summary = row["status"]["power_bracket_summary"]
                    self.assertEqual(summary["count"], 3)
                    self.assertEqual(summary["target_deviation_count"], 3)
                    self.assertEqual(len(set(summary["sequences"])), 3)
                self.assertEqual([c for c in backend.commands if c[0] in {"configure", "emission"}],
                                 writes_at_tuning_finish[0])

    def test_target_continuation_covers_requalification_recovery_and_every_bracket(self):
        session, backend, _, meter, clock = self.make(source_current=True,
                                                     target_policy="record_continue")
        self.addCleanup(session.cleanup)
        self.start(session)
        commands = [c for c in backend.commands if c[0] in {"configure", "emission"}]
        meter.resource.power_provider = lambda: session.feedback.target_power_w * 1.2
        session.qualify()
        session.guard_reference_recovery(deadline=clock() + 1)
        session.begin_sample()
        session.monitor_sample()
        row = session.read()
        session.end_sample([row])
        self.assertEqual([item["phase"] for item in row["status"]["sample_brackets"]],
                         ["before_electrical", "diagnostic_capture", "formal", "after_electrical"])
        self.assertTrue(all(item["power_target_assessment"]["target_deviation_continued"]
                            for item in row["status"]["sample_brackets"]))
        self.assertTrue(session.qualified)
        self.assertEqual([c for c in backend.commands if c[0] in {"configure", "emission"}], commands)

    def test_target_continuation_keeps_power_and_meter_faults_fatal(self):
        for fault in ("maximum", "minimum", "nan", "meter", "state", "reduce"):
            with self.subTest(fault=fault):
                session, backend, _, meter, _ = self.make(source_current=True,
                                                         target_policy="record_continue")
                self.addCleanup(session.cleanup)
                if fault == "reduce":
                    session.config = replace(session.config, scan=replace(session.config.scan,
                        feedback=replace(session.config.scan.feedback, reduce_above_power_w=0.000045)))
                self.start(session)
                commands = [c for c in backend.commands if c[0] in {"configure", "emission"}]
                if fault == "maximum":
                    meter.resource.power_provider = lambda: 0.000051
                elif fault == "minimum":
                    meter.resource.power_provider = lambda: 0.0000001
                elif fault == "nan":
                    meter.resource.power_provider = lambda: float("nan")
                elif fault == "meter":
                    meter.read_sample = Mock(side_effect=OSError("meter connection lost"))
                elif fault == "state":
                    backend.source["level_pct"] += 1
                else:
                    meter.resource.power_provider = lambda: 0.000046
                with self.assertRaises((NktError, OSError, ValueError)):
                    session.begin_sample()
                self.assertFalse(session.qualified)
                self.assertEqual([c for c in backend.commands if c[0] in {"configure", "emission"}], commands)

    def test_initial_unreachable_target_still_fails_with_continuation(self):
        session, _, _, meter, _ = self.make(source_current=True, target_policy="record_continue")
        self.addCleanup(session.cleanup)
        meter.resource.power_provider = lambda: 0.000002
        session.open()
        session.configure()
        session.set_point(0)
        with self.assertRaises(NktError):
            session.qualify()
        self.assertFalse(session.qualified)

    def test_each_sample_retains_its_own_reading_and_bracket_summary(self):
        session, _, _, meter, _ = self.make(source_current=True, target_policy="record_continue")
        self.addCleanup(session.cleanup)
        self.start(session)
        rows = []
        for factor in (0.8, 1.2):
            values = iter(session.feedback.target_power_w * value
                          for value in (factor, factor + 0.01, factor + 0.02))
            meter.resource.power_provider = lambda: next(values)
            session.begin_sample()
            row = session.read()
            session.end_sample([row])
            rows.append(row)
            self.assertAlmostEqual(row["measurements"]["optical_power_w"],
                                   session.feedback.target_power_w * (factor + 0.01))
            self.assertAlmostEqual(row["status"]["power_bracket_summary"]["mean_power_w"],
                                   row["measurements"]["optical_power_w"])
        self.assertLess(max(rows[0]["status"]["power_bracket_summary"]["sequences"]),
                        min(rows[1]["status"]["power_bracket_summary"]["sequences"]))

    def test_cartesian_grid_expands_one_row_with_five_or_six_requested_powers(self):
        for count in (5, 6):
            with self.subTest(count=count):
                session, _, _, meter, _ = self.make(source_current=True)
                targets = (10e-6, 100e-6, 500e-6, 1e-3, 2e-3, 3e-3)[:count]
                nkt = replace(session.config.nkt, points=session.config.nkt.points[:1])
                meter.config = replace(meter.config, max_power_w=0.004)
                feedback = replace(session.config.scan.feedback, target_mapping="cartesian",
                                   target_powers_w=targets, max_power_w=0.004)
                cfg = replace(session.config, nkt=nkt, pm=meter.config,
                              scan=replace(session.config.scan, feedback=feedback))
                cfg.validate()
                self.assertEqual(len(cfg.nkt.points), 1)
                self.assertEqual(len(cfg.points), count)
                self.assertEqual([p["optical_target_power_w"] for p in cfg.points], list(targets))
                self.assertEqual(cfg.snapshot()["point_grid"]["expanded_point_count"], count)

    def test_cartesian_two_wavelengths_preserve_order_and_each_prepared_target(self):
        session, _, pem, meter, _ = self.make(source_current=True, target_policy="record_continue")
        self.addCleanup(session.cleanup)
        points = (session.config.nkt.points[0], replace(session.config.nkt.points[0], wavelength_nm=650.0))
        session.config = replace(session.config, nkt=replace(session.config.nkt, points=points),
            scan=replace(session.config.scan, feedback=replace(session.config.scan.feedback,
                         target_mapping="cartesian")))
        # The fake provider retains the original physical current/power response.
        targets = session.config.scan.feedback.target_powers_w
        self.assertEqual([(p["optical_wavelength_nm"], p["optical_target_power_w"])
                          for p in session.config.points],
                         [(w, t) for w in (633.0, 650.0) for t in targets])
        session.open()
        session.configure()
        for index in range(6):
            session.set_point(index)
            self.assertEqual(session.feedback.target_power_w, targets[index % 3])
            self.assertEqual(meter.target_nm, (633.0, 650.0)[index // 3])
            self.assertEqual(pem.target_nm, meter.target_nm * 0.25)
            session.qualify()
            session.begin_sample()
            row = session.read()
            session.end_sample([row])
            self.assertEqual(row["status"]["power_target_w"], targets[index % 3])

    def test_cartesian_grid_keeps_scalar_targets_and_rejects_oversized_product(self):
        session, _, _, _, _ = self.make(source_current=True)
        fb = session.config.scan.feedback
        scalar = replace(fb, target_mapping="cartesian", target_powers_w=None, target_power_w=5e-6)
        cfg = replace(session.config, scan=replace(session.config.scan, feedback=scalar))
        cfg.validate()
        self.assertEqual(len(cfg.points), 3)
        self.assertTrue(all(p["optical_target_power_w"] == 5e-6 for p in cfg.points))
        fb = replace(fb, target_mapping="cartesian", target_powers_w=(5e-6,) * 5001)
        cfg = replace(session.config, nkt=replace(session.config.nkt, points=session.config.nkt.points[:2]),
                      scan=replace(session.config.scan, feedback=fb))
        with self.assertRaisesRegex(NktError, "expanded optical point count"):
            cfg.validate()
        with self.assertRaisesRegex(NktError, "Expanded optical point count"):
            _ = cfg.points

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

    def test_cleanup_can_defer_shared_meter_manager_until_electrical_protection(self):
        session, backend, pem, meter, _ = self.make()
        meter.manager = Mock()
        self.start(session)
        first = session.cleanup(finish_pem=False, close_meter=False)
        self.assertTrue(first["off_confirmed"])
        self.assertTrue(first["meter_cleanup_pending"])
        self.assertTrue(first["reference_cleanup_pending"])
        self.assertTrue(first["verified"])
        self.assertFalse(meter.closed)
        meter.manager.close.assert_not_called()
        self.assertIs(pem.output_command, True)
        self.assertEqual(backend.source["emission_state"], 0)
        final = session.close()
        self.assertFalse(final["meter_cleanup_pending"])
        self.assertFalse(final["reference_cleanup_pending"])
        self.assertTrue(final["verified"])
        self.assertTrue(meter.closed)
        meter.manager.close.assert_called_once()

    def test_cleanup_meter_deferral_requires_explicit_boolean(self):
        for value in (0, 1, None, "false"):
            session, backend, _, meter, _ = self.make()
            with self.subTest(value=value), self.assertRaisesRegex(NktError, "close_meter.*boolean"):
                session.cleanup(close_meter=value)
            self.assertEqual(backend.commands, [])
            self.assertFalse(meter.closed)

    def test_deferred_meter_close_failure_is_retained_in_final_cleanup(self):
        session, _, pem, meter, _ = self.make()
        self.start(session)
        meter.manager = Mock()
        def failed_close():
            raise OSError("synthetic deferred PM close failure")
        meter.resource.close = failed_close
        first = session.cleanup(finish_pem=False, close_meter=False)
        self.assertTrue(first["verified"])
        self.assertTrue(first["meter_cleanup_pending"])
        final = session.close()
        self.assertFalse(final["verified"])
        self.assertFalse(final["meter_cleanup_pending"])
        self.assertIn("synthetic deferred PM close failure", str(final["errors"]))
        meter.manager.close.assert_called_once()
        self.assertTrue(pem.closed)

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
