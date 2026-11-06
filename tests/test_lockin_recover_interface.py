"""Transport-only recovery with the same AUX policy as combination scans."""
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch

from attodry_control.lockin_test import run
from attodry_control.hardware_lease import station_hardware_lease
from tests import test_combination_hardware as fixtures
from tests.test_three_lockin import AUX, POLICY
from tests.test_sr830 import FakeVisaResource, FakeResourceManager


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.ElectricalCombinationTests.setUp
    write_config = fixtures.ElectricalCombinationTests.write_config

    def prepare(self, aux=AUX):
        self.write_config(order=("optical", "smu", "lockin"), extra=aux)
        safety = self.directory / "lockin_safety.toml"
        safety.write_text(safety.read_text() + POLICY)
        self.resources = {f"FAKE::{role}": FakeVisaResource({}) for role in ("XX", "XY", "AUX")}
        self.manager = FakeResourceManager(self.resources)

    def recover(self, *extra, factory=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(["recover-interface", "--config", str(self.path), *extra],
                       resource_manager_factory=factory or (lambda: self.manager))
        return code, json.loads(output.getvalue())

    def test_three_roles_clear_once_no_queries_or_settings_writes(self):
        self.prepare()
        code, result = self.recover()
        self.assertEqual(code, 0)
        self.assertTrue(result["completed"])
        self.assertFalse(result["identity_verified"])
        for resource in self.resources.values():
            self.assertEqual(resource.clear_calls, 1)
            self.assertEqual(resource.queries, [])
            self.assertEqual(resource.writes, [])
            self.assertTrue(resource.closed)
        self.assertTrue(self.manager.closed)
        self.assertEqual(result["interface_clear"]["lockin_aux"]["model"], "SR865A")

    def test_disabled_aux_not_opened_or_cleared(self):
        self.prepare(AUX.replace("enabled = true", "enabled = false"))
        code, result = self.recover()
        self.assertEqual(code, 0)
        self.assertNotIn("lockin_aux", result["interface_clear"])
        self.assertEqual(self.resources["FAKE::AUX"].clear_calls, 0)
        self.assertFalse(self.resources["FAKE::AUX"].closed)

    def test_aux_policy_is_validated_before_manager_creation(self):
        self.prepare()
        p = self.directory / "lockin_safety.toml"
        p.write_text(p.read_text().replace("allowed_fixed_full_scales_v = [0.001, 0.0002]",
                                          "allowed_fixed_full_scales_v = [0.0002]"))
        factory = Mock()
        with self.assertRaises(ValueError):
            self.recover(factory=factory)
        factory.assert_not_called()

    def test_unknown_safety_field_still_rejected(self):
        self.prepare()
        p = self.directory / "lockin_safety.toml"
        p.write_text('unexpected = true\n' + p.read_text())
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self.recover(factory=factory)
        factory.assert_not_called()

    def test_duplicate_aux_address_after_override_rejected(self):
        self.prepare()
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.recover("--xx-address", "fake::aux", factory=factory)
        factory.assert_not_called()

    def test_active_station_blocks_clear(self):
        self.prepare()
        factory = Mock()
        with station_hardware_lease(self.path):
            code, result = self.recover(factory=factory)
        self.assertEqual(code, 1)
        self.assertFalse(result["interface_clear"]["attempted"])
        factory.assert_not_called()

    def test_open_failure_closes_previous_resources_without_clearing(self):
        self.prepare()
        original = self.manager.open_resource
        def opened(address):
            if address == "FAKE::AUX":
                raise OSError("AUX unavailable")
            return original(address)
        self.manager.open_resource = opened
        code, result = self.recover()
        self.assertEqual(code, 1)
        self.assertFalse(result["completed"])
        for key in ("FAKE::XX", "FAKE::XY"):
            self.assertTrue(self.resources[key].closed)
            self.assertEqual(self.resources[key].clear_calls, 0)
        self.assertTrue(self.manager.closed)

    def test_clear_failure_reported_no_retry_sessions_closed(self):
        self.prepare()
        clear = Mock(side_effect=OSError("clear timeout"))
        self.resources["FAKE::AUX"].clear = clear
        code, result = self.recover()
        self.assertEqual(code, 1)
        self.assertFalse(result["completed"])
        self.assertIn("clear timeout", result["interface_clear"]["errors"][0])
        clear.assert_called_once_with()
        self.assertTrue(all(r.closed for r in self.resources.values()))

    def test_close_failure_does_not_report_success(self):
        self.prepare()
        self.resources["FAKE::AUX"].close = Mock(side_effect=OSError("close failed"))
        code, result = self.recover()
        self.assertEqual(code, 1)
        self.assertFalse(result["completed"])
        self.assertTrue(self.resources["FAKE::XX"].closed)
        self.assertTrue(self.resources["FAKE::XY"].closed)
        self.assertTrue(self.manager.closed)

    def test_configured_visa_backend_is_used(self):
        self.prepare()
        self.path.write_text(self.path.read_text().replace('backend = "default"', 'backend = "@test"'))
        factory = Mock(return_value=self.manager)
        with patch("attodry_control.lockin_test._load_resource_manager_factory", return_value=factory), redirect_stdout(io.StringIO()):
            code = run(["recover-interface", "--config", str(self.path)])
        self.assertEqual(code, 0)
        factory.assert_called_once_with("@test")
