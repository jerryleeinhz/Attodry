"""Whole optical coordinator with overloaded synthetic mixed-model lock-ins."""
import tomllib
import unittest

from tests import test_combination_photonics as fixtures
from attodry_control.combination_analysis import load_combination_rows, select_series
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_terminal import launch_text


class PhotonicsOverloadCombinationTests(unittest.TestCase):
    setUp = fixtures.PhotonicsCombinationTests.setUp
    write_config = fixtures.PhotonicsCombinationTests.write_config
    on_lockin_query = fixtures.PhotonicsCombinationTests.on_lockin_query
    backend_factory = fixtures.PhotonicsCombinationTests.backend_factory
    pem_factory = fixtures.PhotonicsCombinationTests.pem_factory
    meter_factory = fixtures.PhotonicsCombinationTests.meter_factory
    execute = fixtures.PhotonicsCombinationTests.execute
    events = fixtures.PhotonicsCombinationTests.events
    shared_visa_setup = fixtures.PhotonicsCombinationTests.shared_visa_setup
    assert_shared_cleanup_protection = fixtures.PhotonicsCombinationTests.assert_shared_cleanup_protection

    def inject(self, role, bits):
        self.shared_visa_setup()
        doc = tomllib.loads(self.path.read_text(encoding="utf-8"))
        doc["photonics_lockin"]["overload_policy"] = "record_continue"
        self.path.write_text(fixtures.toml_document(doc), encoding="utf-8")
        resource = self.manager.resources["FAKE::" + role.upper()]
        original = resource.on_query

        def overload(resource, command):
            original(resource, command)
            if command == "LIAS?" and self.backend and self.backend.source["emission_state"] == 3:
                resource.responses[command] = str(bits)

        resource.on_query = overload

    def assert_continued(self, role, bits):
        self.inject(role, bits)
        result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assert_shared_cleanup_protection(result)
        self.assertEqual(result["overload_summary"]["data_quality"], "overload_recorded")
        self.assertGreater(result["overload_summary"]["continued_overload_pairs"], 0)
        audit = load_combination_rows(self.database, audit=True)
        default = load_combination_rows(self.database)
        self.assertEqual(len(audit), 6)
        self.assertEqual(len(default), 6)
        affected = f"measured.lockin_{role}_h{1 if role == 'xx' else 2}_x_v"
        other = "measured.lockin_xy_h2_x_v" if role == "xx" else "measured.lockin_xx_h1_x_v"
        self.assertTrue(all(not row["clean"] and affected in row for row in audit))
        self.assertTrue(all(affected not in row and other in row for row in default))
        self.assertTrue(select_series(default, x="measured.optical_power_w", y=other))
        self.assertFalse(any(affected in row for row in default))

    def test_sr830_overload_keeps_sr865a_companion_and_completes_cleanup(self):
        self.assert_continued("xx", 1)

    def test_launch_preview_displays_resolved_overload_policy_before_io(self):
        self.inject("xx", 1)
        config = load_hardware_combination(self.path)
        summary = launch_summary(config, self.database, "preview")
        self.assertEqual(summary["lockin"]["overload_policy"], "record_continue")
        self.assertIn("Overload: record_continue", launch_text(summary))
        self.assertEqual(self.manager.opened, [])
        self.assertIsNone(self.backend)

    def test_sr865a_latched_only_overload_keeps_sr830_companion(self):
        # Current register stays zero while each latched window contains bit 4.
        self.assert_continued("xy", 16)

    def test_overload_with_reference_unlock_still_fails_and_retains_raw(self):
        self.inject("xy", 24)
        result = self.execute(confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertTrue(any(kind == "lockin_raw_role" and payload["role"] == "lockin_xy"
                            and payload["sample"]["status"]["native_status"]["reference_unlock_latched"]
                            for kind, payload in self.events()))
        self.assertEqual(self.backend.source["emission_state"], 0)
        self.assertFalse(self.pem.output_command)


if __name__ == "__main__":
    unittest.main()
