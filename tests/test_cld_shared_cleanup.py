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

    def test_partial_open_defers_backend_close_and_never_writes_to_unknown_source(self):
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
        self.assertEqual(writes, [])
        self.assertTrue(self.backend.state["getLdOutputState"])
        self.assertEqual(self.backend.state["getLdCurrSetpoint"], 0.007)
        self.assertEqual(sum(event[0] == "cld_backend_close" for event in self.shared_log), 1)
        self.assertEqual(sum(kind == "optical_audit" for kind, _ in events), 1)


if __name__ == "__main__":
    unittest.main()
