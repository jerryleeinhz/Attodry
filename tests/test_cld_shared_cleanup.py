"""CLD optical shutdown must not close a shared VISA manager before XX protection."""
from contextlib import closing
import json
import unittest
from unittest.mock import patch

from tests import test_internal_optical_combination as internal_helpers
from tests.test_photonics_chopper_template import substitute
from attodry_control.combination_store import open_readonly
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.lockin_points import LockinPointSession
from attodry_control.optical_points import OpticalPointSession
from attodry_control.photoresponse_cld import SimulatedCld
from attodry_control.pm100d import Pm100d, SimulatedPmResource


class CldSharedCleanupTests(unittest.TestCase):
    write_config = internal_helpers.InternalOpticalCombinationTests.write_config
    write_document = internal_helpers.InternalOpticalCombinationTests.write_document
    factory = internal_helpers.InternalOpticalCombinationTests.factory
    events = internal_helpers.InternalOpticalCombinationTests.events
    execute = internal_helpers.InternalOpticalCombinationTests.execute
    chopper_factory = internal_helpers.InternalOpticalCombinationTests.chopper_factory

    def setUp(self):
        internal_helpers.InternalOpticalCombinationTests.setUp(self)
        from tests.test_photonics_chopper_template import PhotonicsChopperTemplateTests
        template = substitute(PhotonicsChopperTemplateTests.document(self, "cld"))
        for name in ("nkt_source", "nkt_varia", "nkt_run"):
            self.document.pop(name)
        self.document["cld1010"] = template["cld1010"]
        self.document["cld1010"].update(diode_wiring_confirmed=True,
            stable_dwell_s=0.1, timeout_s=1.0, poll_interval_s=0.01)
        self.document["power_feedback"] = template["power_feedback"]
        self.document["power_feedback"].update(hold_s=0.04)
        self.document["power_feedback"]["window"].update(duration_s=0.04, sample_interval_s=0.01)
        self.document["pm100d"]["max_power_w"] = self.document["power_feedback"]["max_power_w"]
        self.document["optical_scan"]["laser_source"] = "cld"
        self.write_document()
        self.shared_log = []
        self.inject_close_failure = False
        original_close = self.manager.close
        def close_manager():
            if self.manager.closed:
                return
            self.shared_log.append(("manager_close",))
            for resource in self.manager.resources.values():
                resource.close()
            original_close()
        self.manager.close = close_manager
        for role, resource in (("xx", self.xx), ("xy", self.xy)):
            for operation in ("query", "write"):
                original = getattr(resource, operation)
                def guarded(command, original=original, role=role, operation=operation, resource=resource):
                    if self.manager.closed or resource.closed:
                        raise OSError("InvalidSession: shared default VISA manager closed")
                    self.shared_log.append((role, operation, command))
                    return original(command)
                setattr(resource, operation, guarded)

    def backend_factory(self, config):
        backend = SimulatedCld(config)
        backend.is_hardware = True
        backend.state["identity"] = ["Thorlabs", config.expected_model, config.expected_serial, "fake-1"]
        backend.state["diagnostics"] = {"qualification": "unqualified",
            "ld_connection_raw": 0, "tec_connection_raw": 0, "measurement_condition_raw": 0}
        backend.closed = False
        def close():
            self.shared_log.append(("cld_backend_close",))
            backend.closed = True
            self.manager.close()
            if self.inject_close_failure:
                raise OSError("injected CLD resource close failure")
        backend.close = close
        self.backend = backend
        return backend

    def meter_factory(self, config):
        resource = SimulatedPmResource(lambda: (
            5e-6 * self.backend.state["getLdCurrSetpoint"] / 0.01
            if self.backend.state["getLdOutputState"] else 0))
        original = resource.query
        def query(command):
            if self.manager.closed or resource.closed:
                raise OSError("InvalidSession: shared PM VISA manager closed")
            return original(command)
        resource.query = query
        self.meter = Pm100d(config, resource, is_hardware=True, authorize_settings=True,
            authorize_measurement=True, clock=self.clock)
        return self.meter

    def test_complete_scan_protects_xx_before_shared_manager_or_cld_backend_closes(self):
        result = self.execute("cld-shared")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        actions = {action["module"]: action["result"] for action in result["cleanup"]["actions"]}
        self.assertTrue(actions["lockin"]["source_protection_verified"])
        close_index = next(i for i, event in enumerate(self.shared_log) if event[0] == "manager_close")
        self.assertTrue(any(event == ("xx", "query", "SLVL?") for event in self.shared_log[:close_index]))
        self.assertFalse(any(event[0] in ("xx", "xy") for event in self.shared_log[close_index:]))
        self.assertEqual(sum(event[0] == "cld_backend_close" for event in self.shared_log), 1)
        self.assertFalse(self.backend.state["getLdOutputState"])
        self.assertEqual(self.backend.state["getLdCurrSetpoint"], 0)
        self.assertTrue(self.backend.closed and self.manager.closed and self.meter.closed)
        with closing(open_readonly(self.database)) as db:
            audit = [json.loads(row[0]) for row in db.execute(
                "SELECT payload_json FROM combination_events WHERE event_type='optical_audit'")]
        self.assertEqual(len(audit), 1)

    def test_close_failure_is_retained_after_protection_and_never_certified(self):
        self.inject_close_failure = True
        result = self.execute("cld-close-failure")
        self.assertFalse(result["cleanup"]["clean"])
        self.assertIn("injected CLD resource close failure", str(result["cleanup"]))
        self.assertFalse(self.backend.state["getLdOutputState"])
        self.assertEqual(self.backend.state["getLdCurrSetpoint"], 0)
        self.assertTrue(self.backend.closed and self.manager.closed and self.meter.closed)
        self.assertEqual(sum(event[0] == "cld_backend_close" for event in self.shared_log), 1)

    def test_cld1015_980nm_combination_archives_model_samples_and_complete_cleanup(self):
        self.document["cld1010"].update(expected_model="CLD1015", wavelength_nm=980.0)
        self.write_document()
        result = self.execute("cld1015-980nm")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
        with closing(open_readonly(self.database)) as db:
            plan = json.loads(db.execute("SELECT plan_json FROM combination_runs").fetchone()[0])
            samples = [json.loads(row[0]) for row in db.execute(
                "SELECT payload_json FROM combination_samples")]
        self.assertEqual(plan["hardware"]["optical"]["nkt"]["expected_model"], "CLD1015")
        self.assertTrue(samples)
        for sample in samples:
            self.assertEqual(sample["actual"]["optical_wavelength_nm"], 980)
        self.assertFalse(self.backend.state["getLdOutputState"])
        self.assertEqual(self.backend.state["getLdCurrSetpoint"], 0)
        self.assertTrue(self.backend.closed and self.manager.closed)

    def test_unverified_electrical_protection_still_closes_cld_resources_once(self):
        with patch.object(LockinPointSession, "cleanup", side_effect=OSError("XX protection unknown")):
            result = self.execute("cld-electrical-failure")
        self.assertFalse(result["cleanup"]["clean"])
        self.assertIn("XX protection unknown", str(result["cleanup"]))
        self.assertTrue(self.backend.closed and self.manager.closed and self.meter.closed)
        self.assertFalse(self.backend.state["getLdOutputState"])
        self.assertEqual(sum(event[0] == "cld_backend_close" for event in self.shared_log), 1)

    def test_without_pm_audit_waits_for_laser_transport_final_close(self):
        self.document["optical_scan"].update(mode="direct", use_power_meter=False)
        self.document.pop("power_feedback")
        self.document.pop("pm100d")
        self.write_document()
        result = self.execute("cld-no-pm")
        self.assertEqual(result["status"], "completed", result)
        with closing(open_readonly(self.database)) as db:
            events = [(row[0], json.loads(row[1])) for row in db.execute(
                "SELECT event_type,payload_json FROM combination_events ORDER BY event_id")]
        self.assertEqual(sum(kind == "optical_audit" for kind, _ in events), 1)
        partial = [payload["evidence"] for kind, payload in events if kind == "optical_cleanup"]
        self.assertTrue(any(item["laser_cleanup_pending"] for item in partial))
        self.assertFalse(partial[-1]["laser_cleanup_pending"])
        self.assertTrue(partial[-1]["actions"]["nkt"]["transport_closed"])

    def test_partial_open_defers_close_and_only_protects_identified_ld(self):
        config = load_hardware_combination(self.path).optical
        events = []
        def fail_meter(_config):
            raise OSError("PM opening failed before source ownership")
        session = OpticalPointSession(config, lambda kind, payload: events.append((kind, payload)),
            backend_factory=self.backend_factory, meter_factory=fail_meter,
            clock=self.clock, sleep=self.clock.sleep)
        with self.assertRaisesRegex(OSError, "PM opening"):
            session.open(authorize_writes=True, confirm_manual_route=True)
        self.assertIsNone(session.engine)
        self.backend.state.update(getLdOutputState=True, getLdCurrSetpoint=0.007)
        writes = []
        self.backend.set = lambda *arguments: writes.append(arguments)
        partial = session.cleanup(finish_pem=False, close_meter=False)
        self.assertFalse(self.backend.closed or self.manager.closed)
        self.assertTrue(partial["laser_cleanup_pending"])
        self.assertTrue(partial["actions"]["nkt"]["ownership_not_acquired"])
        self.assertFalse(partial["off_confirmed"] or partial["verified"])
        self.assertFalse(any(kind == "optical_audit" for kind, _ in events))
        final = session.close(finish_pem=False)
        session.close(finish_pem=False)
        self.assertTrue(self.backend.closed and self.manager.closed)
        self.assertFalse(final["laser_cleanup_pending"])
        self.assertFalse(final["off_confirmed"] or final["verified"])
        self.assertTrue(final["actions"]["nkt"]["ownership_not_acquired"])
        self.assertEqual(writes, [("switchLdOutput", False)])
        self.assertTrue(self.backend.state["getLdOutputState"])
        self.assertEqual(self.backend.state["getLdCurrSetpoint"], 0.007)
        self.assertEqual(sum(event[0] == "cld_backend_close" for event in self.shared_log), 1)
        self.assertEqual(sum(kind == "optical_audit" for kind, _ in events), 1)
        self.assertTrue(final["startup_off"]["off_confirmed"])
        self.assertTrue(final["actions"]["nkt"]["ld_only_cleanup"])

    def test_initially_on_is_off_before_lockin_connection_failure(self):
        original_factory = self.backend_factory
        def initially_on(config):
            backend = original_factory(config)
            backend.state.update(getLdOutputState=True, getLdCurrSetpoint=0.007)
            original_set = backend.set
            def record_set(*args):
                self.shared_log.append(("cld_write", *args))
                return original_set(*args)
            backend.set = record_set
            return backend
        self.backend_factory = initially_on
        def fail_lockin(_):
            self.assertFalse(self.backend.state["getLdOutputState"])
            self.assertEqual(self.shared_log[0], ("cld_write", "switchLdOutput", False))
            raise OSError("injected lockin connection failure")
        with patch.object(LockinPointSession, "open", fail_lockin):
            result = self.execute("startup-lockin-failure")
        self.assertEqual(result["status"], "failed")
        self.assertIn("injected lockin", result["error"])
        self.assertEqual(self.backend.state["getLdCurrSetpoint"], 0.007)
        self.assertTrue(all(event[1:] == ("switchLdOutput", False)
                            for event in self.shared_log if event[0] == "cld_write"))
        audit = [payload for kind, payload in self.events() if kind == "optical_audit"]
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["cld_startup_off"]["off_confirmed"])
        self.assertTrue(self.backend.closed)

    def test_unconfirmed_startup_off_blocks_all_other_connections_and_is_not_replayed(self):
        config = load_hardware_combination(self.path).optical
        backend = self.backend_factory(config.nkt)
        backend.state["getLdOutputState"] = True
        calls = []
        backend.set = lambda *args: calls.append(args)  # ACK without changing state.
        session = OpticalPointSession(config, backend_factory=lambda _: backend,
            meter_factory=lambda _: self.fail("PM must not open"), clock=self.clock, sleep=self.clock.sleep)
        with self.assertRaises(Exception):
            session.open(authorize_writes=True, confirm_manual_route=True)
        self.assertEqual(calls, [("switchLdOutput", False)])
        with self.assertRaises(Exception):
            session.open(authorize_writes=True, confirm_manual_route=True)
        with self.assertRaises(Exception):
            session.prepare_cld_startup(authorize_writes=True, confirm_manual_route=True)
        self.assertEqual(len(calls), 1)
        final = session.close()
        self.assertFalse(final["startup_off"]["off_confirmed"])
        self.assertTrue(final["startup_off"]["errors"])
        self.assertFalse(final["verified"])
        self.assertTrue(backend.closed)

    def test_startup_audit_failure_preserves_off_evidence_without_opening_pm(self):
        config = load_hardware_combination(self.path).optical
        def fail_event(*args):
            raise OSError("audit unavailable")
        session = OpticalPointSession(config, fail_event, backend_factory=self.backend_factory,
            meter_factory=lambda _: self.fail("PM must not open"), clock=self.clock, sleep=self.clock.sleep)
        with self.assertRaisesRegex(Exception, "audit unavailable"):
            session.open(authorize_writes=True, confirm_manual_route=True)
        self.assertIsNone(session.engine)
        final = session.close()
        self.assertTrue(final["startup_off"]["off_confirmed"])
        self.assertTrue(final["startup_off"]["errors"])
        self.assertFalse(final["verified"])
        self.assertTrue(self.backend.closed)
        self.assertEqual(session.audit_snapshot["cld_startup_off"], final["startup_off"])


if __name__ == "__main__":
    unittest.main()
