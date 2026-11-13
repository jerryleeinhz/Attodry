"""Hardware-blocked LD-only commands and cross-config connection ownership."""
from copy import deepcopy
from dataclasses import replace
from functools import partial
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from optical_helpers import Clock
from test_cld_drivers import Dll, Resource, configuration
from attodry_control.cld_control import _RecordWriter, main, run_control
from attodry_control.cld_output import protected_ld_off
from attodry_control.hardware_lease import cld_hardware_lease
from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_cld import TL4000, VisaCld


class CldControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch("attodry_control.photoresponse_cld.cld_hardware_lease",
                        partial(cld_hardware_lease, directory=self.directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = configuration()
        self.resource = Resource()
        self.resource.values.update({"OUTP2:STAT?": "1", "SOUR1:CURR:LEV:IMM:AMPL?": ".01"})
        self.manager = Mock()
        self.manager.open_resource.return_value = self.resource
        self.records = []
        self.record = {}
        self.clock = Clock()

    def backend(self, config=None, **kwargs):
        return VisaCld(config or self.config, manager_factory=lambda: self.manager,
                       **kwargs)

    def run_command(self, action):
        return run_control(action, self.config, record=self.record,
                           persist=lambda r: self.records.append(deepcopy(r)),
                           backend_factory=self.backend, clock=self.clock,
                           sleep=self.clock.sleep)

    def writes(self):
        return [command for kind, command in self.resource.calls if kind == "write"]

    def test_on_preserves_settings_and_leaves_verified_on_after_close(self):
        original = deepcopy(self.resource.values)
        self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 1"])
        for name in ("OUTP2:STAT?", "SOUR1:CURR:LEV:IMM:AMPL?", "SOUR1:CURR:LIM:AMPL?",
                     "SOUR2:TEMP:SPO?", "OUTP1:PROT:TEMP:MODE?", "SOUR2:CURR:LIM:AMPL?"):
            self.assertEqual(self.resource.values[name], original[name])
        self.assertEqual(self.resource.values["OUTP1:STAT?"], "1")
        self.assertTrue(self.record["on"]["on_confirmed"])
        self.assertTrue(self.record["transport_closed"])
        self.assertFalse(self.record["manual_verification_required"])
        self.assertEqual(self.records[0]["status"], "connecting")
        self.assertGreaterEqual(self.clock(), self.config.stable_dwell_s)
        self.assertTrue(any(r.get("on", {}).get("on_attempted") and r["status"] == "qualifying"
                            for r in self.records))
        self.assertIn("captured_at_utc", self.record["on"]["after"])

    def test_already_on_is_qualified_without_output_rewrite(self):
        self.resource.values.update({"OUTP1:STAT?": "1", "SENS3:CURR:DC:DATA?": ".01",
                                     "SENS4:VOLT:DC:DATA?": "1.2"})
        self.run_command("on")
        self.assertEqual(self.writes(), [])
        self.assertTrue(self.record["on"]["already_on"])
        self.assertFalse(self.record["on"]["on_attempted"])

    def test_off_ignores_health_fault_but_verifies_identity_and_actual_state(self):
        self.resource.values.update({"OUTP1:STAT?": "1", "SENS2:TEMP:DATA?": "100",
                                     "OUTP1:PROT:CONN:TRIP?": "1"})
        self.run_command("off")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])
        queries = [value for kind, value in self.resource.calls if kind == "query"]
        self.assertEqual(queries, ["*IDN?", "OUTP1:STAT?", "SYST:ERR?", "OUTP1:STAT?", "OUTP1:STAT?"])
        self.assertTrue(self.record["off"]["off_confirmed"])
        self.assertTrue(self.record["off"]["before"]["ld_on"])
        self.assertFalse(self.record["off"]["after"]["ld_on"])
        self.assertEqual(self.resource.values["SOUR1:CURR:LEV:IMM:AMPL?"], ".01")
        self.assertEqual(self.resource.values["OUTP2:STAT?"], "1")

    def test_mismatched_identity_never_writes(self):
        self.resource.values["*IDN?"] = "Thorlabs,CLD1010LP,OTHER,2.3.0"
        with self.assertRaises(NktError):
            self.run_command("off")
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.record["off"]["identity_verified"])
        self.assertTrue(self.record["transport_closed"])

    def test_unknown_initial_ld_state_still_attempts_off_and_preserves_error(self):
        self.resource.values["OUTP1:STAT?"] = "unexpected"
        with self.assertRaises(NktError):
            self.run_command("off")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])
        self.assertTrue(self.record["off"]["off_confirmed"])
        self.assertIsNone(self.record["off"]["before"])
        self.assertTrue(self.record["manual_verification_required"])
        self.assertTrue(self.record["off"]["errors"])

    def test_on_failure_after_identity_always_protects_without_setting_changes(self):
        self.resource.values["OUTP1:PROT:INTL:TRIP?"] = "1"
        with self.assertRaises(NktError):
            self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])
        self.assertTrue(self.record["failed_on_off"]["off_confirmed"])
        self.assertTrue(self.record["manual_verification_required"])

    def test_poisoned_on_readback_still_sends_protective_off_once(self):
        original_write = self.resource.write
        def write(command):
            result = original_write(command)
            if command == "OUTP1:STAT 1":
                self.resource.failure = "SYST:ERR?"
            return result
        self.resource.write = write
        with self.assertRaises(NktError):
            self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 1", "OUTP1:STAT 0"])
        self.assertFalse(self.record["failed_on_off"]["off_confirmed"])
        self.assertTrue(self.record["failed_on_off"]["errors"])
        self.assertTrue(self.record["transport_closed"])

    def test_off_no_readback_certificate_on_failed_write(self):
        self.resource.failure = "OUTP1:STAT 0"
        with self.assertRaises(NktError):
            self.run_command("off")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])
        self.assertFalse(self.record["off"]["off_confirmed"])
        self.assertTrue(self.record["failed_transactions"])

    def test_disabled_tec_blocks_on_without_enabling_tec(self):
        self.resource.values["OUTP2:STAT?"] = "0"
        with self.assertRaisesRegex(NktError, "TEC must already be ON"):
            self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])

    def test_existing_current_above_limit_blocks_on(self):
        self.resource.values["SOUR1:CURR:LEV:IMM:AMPL?"] = ".04"
        with self.assertRaises(NktError):
            self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])

    def test_off_evidence_is_single_use(self):
        driver = self.backend(authorize_connection=True, authorize_writes=True)
        self.addCleanup(driver.close)
        record = {}
        protected_ld_off(self.config, driver, record=record, phase="test")
        with self.assertRaisesRegex(NktError, "single-use"):
            protected_ld_off(self.config, driver, record=record, phase="test")
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])

    def test_audit_failure_does_not_suppress_protective_off(self):
        driver = self.backend(authorize_connection=True, authorize_writes=True)
        self.addCleanup(driver.close)
        record = {}
        with self.assertRaises(NktError):
            protected_ld_off(self.config, driver, record=record, phase="test",
                             on_event=Mock(side_effect=OSError("audit disk unavailable")))
        self.assertEqual(self.writes(), ["OUTP1:STAT 0"])
        self.assertTrue(record["off_confirmed"])
        self.assertTrue(record["manual_verification_required"])

    def test_connection_serial_lease_blocks_alias_and_other_backend_before_open(self):
        first = self.backend(authorize_connection=True, authorize_writes=True)
        try:
            other_manager = Mock()
            with self.assertRaises(ValueError):
                VisaCld(replace(self.config, address="USB0::OTHER::INSTR"), authorize_connection=True,
                        authorize_writes=True, manager_factory=lambda: other_manager)
            other_manager.open_resource.assert_not_called()
            dll = Dll()
            with self.assertRaises(ValueError):
                TL4000(replace(self.config, backend="tl4000", dll_path="fake.dll"),
                       authorize_connection=True, authorize_writes=True, dll=dll)
            self.assertEqual(dll.calls, [])
        finally:
            first.close()
        with cld_hardware_lease(self.config.expected_serial, directory=self.directory.name):
            pass

    def test_lease_released_when_open_or_close_fails(self):
        self.manager.open_resource.side_effect = OSError("cannot open")
        with self.assertRaises(OSError):
            self.backend(authorize_connection=True, authorize_writes=True)
        self.manager.open_resource.side_effect = None
        driver = self.backend(authorize_connection=True, authorize_writes=True)
        self.resource.close = Mock(side_effect=OSError("close failed"))
        with self.assertRaises(NktError):
            driver.close()
        with cld_hardware_lease(self.config.expected_serial, directory=self.directory.name):
            pass

    def test_close_failure_is_command_failure_even_after_verified_off(self):
        self.resource.close = Mock(side_effect=OSError("close failed"))
        with self.assertRaises(NktError):
            self.run_command("off")
        self.assertTrue(self.record["off"]["off_confirmed"])
        self.assertFalse(self.record["transport_closed"])
        self.assertTrue(self.record["manual_verification_required"])

    def test_close_failure_after_verified_on_keeps_on_evidence_and_manual(self):
        self.resource.close = Mock(side_effect=OSError("close failed"))
        with self.assertRaises(NktError):
            self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 1"])
        self.assertTrue(self.record["on"]["on_confirmed"])
        self.assertFalse(self.record["transport_closed"])
        self.assertTrue(self.record["manual_verification_required"])
        self.assertEqual(self.record["status"], "failed")

    def test_final_audit_failure_after_verified_on_requires_manual_without_reopen(self):
        def persist(record):
            if "finished_at_utc" in record:
                raise OSError("final disk write failed")
        with self.assertRaisesRegex(NktError, "final audit"):
            run_control("on", self.config, record=self.record, persist=persist,
                        backend_factory=self.backend, clock=self.clock, sleep=self.clock.sleep)
        self.assertTrue(self.record["on"]["on_confirmed"])
        self.assertTrue(self.record["transport_closed"])
        self.assertTrue(self.record["manual_verification_required"])
        self.assertTrue(self.record["final_audit_persist_failed"])
        self.assertEqual(self.record["status"], "failed")
        self.assertEqual(self.writes(), ["OUTP1:STAT 1"])
        self.manager.open_resource.assert_called_once()

    def test_near_zero_post_on_voltage_uses_bounded_recheck_without_second_on(self):
        self.config = replace(self.config, timeout_s=5.)
        query = self.resource.query
        def response(command):
            if command == "SENS4:VOLT:DC:DATA?" and self.resource.values["OUTP1:STAT?"] == "1":
                self.resource.values[command] = "1.2" if self.clock() >= 2.1 else "-.0001"
            return query(command)
        self.resource.query = response
        self.run_command("on")
        self.assertEqual(self.writes(), ["OUTP1:STAT 1"])
        self.assertGreaterEqual(self.clock(), 2.1)
        self.assertEqual(self.record["on"]["after"]["state"]["measLdVolt"], 1.2)

    def test_native_off_preserves_settings_and_releases_serial_lease(self):
        dll = Dll()
        dll.values.update(getLdOutputState=1, getTecOutputState=1, getLdCurrSetpoint=.01)
        config = replace(self.config, backend="tl4000", dll_path="fake.dll")
        def factory(config, **kwargs):
            return TL4000(config, dll=dll, **kwargs)
        run_control("off", config, record=self.record,
                    persist=lambda r: self.records.append(deepcopy(r)), backend_factory=factory)
        settings = [(name, args[-1].value) for name, args in dll.calls if name.startswith(("switch", "set"))]
        self.assertEqual(settings, [("switchLdOutput", 0)])
        self.assertEqual(dll.values["getLdCurrSetpoint"], .01)
        self.assertEqual(dll.values["getTecOutputState"], 1)
        self.assertTrue(self.record["off"]["off_confirmed"])
        with cld_hardware_lease(self.config.expected_serial, directory=self.directory.name):
            pass

    def test_native_lease_released_after_failed_init(self):
        dll = Dll()
        dll.statuses["init"] = -5
        with self.assertRaises(NktError):
            TL4000(replace(self.config, backend="tl4000", dll_path="fake.dll"), dll=dll,
                   authorize_connection=True, authorize_writes=True)
        self.assertIn("close", [name for name, _ in dll.calls])
        with cld_hardware_lease(self.config.expected_serial, directory=self.directory.name):
            pass

    def test_record_created_exclusively_and_updated_to_valid_json(self):
        path = Path(self.directory.name) / "record.json"
        writer = _RecordWriter(path, {"status": "prepared"})
        with self.assertRaises(FileExistsError):
            _RecordWriter(path, {})
        writer({"status": "failed", "errors": ["original error"]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["errors"], ["original error"])

    def test_help_exits_without_backend(self):
        with patch("attodry_control.cld_control.VisaCld") as factory:
            with self.assertRaises(SystemExit) as raised:
                main(["off", "--help"])
            self.assertEqual(raised.exception.code, 0)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
