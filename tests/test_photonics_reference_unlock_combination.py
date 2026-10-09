"""Reference-unlock diagnostics through real coordinator/SQLite with fake devices."""
from contextlib import closing
import json
import sqlite3
import tomllib
import unittest
from unittest.mock import patch

from tests import test_combination_photonics as fixtures
from attodry_control.analysis_observations import channel_quality
from attodry_control.combination_analysis import load_combination_rows, select_series
from attodry_control.combination_store import open_readonly


XX = "measured.lockin_xx_h1_x_v"
XY = "measured.lockin_xy_h2_x_v"


class PhotonicsReferenceUnlockCombinationTests(unittest.TestCase):
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

    def inject(self, role="xy", *, unlock_policy="record_continue", lia=8,
               current=0, after_bracket_only=False, instrument_error=0):
        self.shared_visa_setup()
        document = tomllib.loads(self.path.read_text(encoding="utf-8"))
        document["photonics_lockin"]["overload_policy"] = "record_continue"
        if unlock_policy is not None:
            document["photonics_lockin"]["reference_unlock_policy"] = unlock_policy
        self.path.write_text(fixtures.toml_document(document), encoding="utf-8")
        self.formal_active = False
        self.lia_count = 0
        resource = self.manager.resources["FAKE::" + role.upper()]
        original = resource.on_query

        def status(resource, command):
            original(resource, command)
            if command == "CUROVLDSTAT?":
                resource.responses[command] = str(current if self.formal_active else 0)
            if not self.formal_active:
                return
            if command == "LIAS?":
                self.lia_count += 1
                if not after_bracket_only or self.lia_count == 3:
                    resource.responses[command] = str(lia)
            if command == "ERRS?" and instrument_error:
                resource.responses[command] = str(instrument_error)

        resource.on_query = status

    def run_formal_fault(self):
        original = fixtures.HardwareCombinationStation.read

        def read(station, module):
            if module != "lockin":
                return original(station, module)
            self.formal_active, self.lia_count = True, 0
            try:
                return original(station, module)
            finally:
                self.formal_active = False

        with patch.object(fixtures.HardwareCombinationStation, "read", read):
            return self.execute(confirm_xy_sine_disconnected=False)

    def stored_readings(self):
        with closing(open_readonly(self.database)) as connection:
            payloads = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM combination_samples ORDER BY condition_id,sample_index")]
        return [next(read for read in payload["reads"] if read["module"] == "lockin")
                for payload in payloads]

    def alter_stored_readings(self, change):
        # Corrupt a synthetic archive after acquisition; no source config is read.
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for rowid, encoded in connection.execute(
                    "SELECT rowid,payload_json FROM combination_samples").fetchall():
                payload = json.loads(encoded)
                reading = next(read for read in payload["reads"] if read["module"] == "lockin")
                change(reading)
                connection.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?",
                                   (json.dumps(payload), rowid))

    def assert_unlock_diagnostic_complete(self, result):
        self.assertEqual(result["status"], "completed", result["error"])
        self.assert_shared_cleanup_protection(result)
        self.assertTrue(result["cleanup"]["clean"])
        self.assertGreater(result["overload_summary"]["continued_reference_unlock_pairs"], 0)
        audit = load_combination_rows(self.database, audit=True)
        default = load_combination_rows(self.database)
        self.assertEqual(len(audit), 6)
        self.assertEqual(len(default), 6)
        for row in audit:
            self.assertFalse(row["clean"])
            self.assertIn(XX, row)
            self.assertIn(XY, row)
            for column in (XX, XY):
                self.assertEqual(channel_quality(row, column)[0], "flagged")
            self.assertEqual(row["valid_for_analysis_by_role"],
                             {"lockin_xx": False, "lockin_xy": False})
        for row in default:
            self.assertNotIn(XX, row)
            self.assertNotIn(XY, row)
            self.assertIn("measured.optical_power_w", row)
            self.assertTrue(row["accepted"])
        for reading in self.stored_readings():
            self.assertFalse(reading["clean"])
            self.assertFalse(reading["valid_for_analysis"])
            self.assertTrue(reading["reference_unlock_continuation"])
            self.assertEqual(reading["status"]["reference_unlock_policy"], "record_continue")
            for entry in reading["status"]["samples"]:
                self.assertEqual(entry["reference_unlock_policy"], "record_continue")
                self.assertTrue(entry["continued_reference_unlock_problems"])
                self.assertEqual(entry["valid_for_analysis_by_role"],
                                 {"lockin_xx": False, "lockin_xy": False})

    def test_sr830_unlock_invalidates_both_roles_and_keeps_raw_diagnostics(self):
        self.inject("xx")
        result = self.run_formal_fault()
        self.assert_unlock_diagnostic_complete(result)
        self.assertEqual(result["overload_summary"]["continued_overload_pairs"], 0)
        for reading in self.stored_readings():
            self.assertFalse(reading["overload_continuation"])
            for entry in reading["status"]["samples"]:
                self.assertEqual(entry["continued_overload_problems"], [])
                native = entry["samples"]["xx"]["status"]["native_status"]
                self.assertTrue(native[0]["reference_unlocked"])

    def test_sr865a_latched_unlock_invalidates_locked_companion(self):
        self.inject("xy", current=0)
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())
        for reading in self.stored_readings():
            for entry in reading["status"]["samples"]:
                status = entry["samples"]["xy"]["status"]
                self.assertTrue(status["locked"])
                self.assertTrue(status["native_status"]["reference_unlock_latched"])
                self.assertTrue(entry["samples"]["xx"]["status"]["locked"])

    def test_sr865a_current_unlock_is_also_explicit_diagnostic(self):
        self.inject("xy", lia=0, current=8)
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())
        self.assertTrue(all(not entry["samples"]["xy"]["status"]["locked"]
                            for reading in self.stored_readings()
                            for entry in reading["status"]["samples"]))

    def test_unlock_only_after_formal_bracket_still_invalidates_both(self):
        self.inject("xy", after_bracket_only=True)
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())
        for reading in self.stored_readings():
            for entry in reading["status"]["samples"]:
                self.assertFalse(entry["samples"]["xy"]["status"]["native_status"]["reference_unlock_latched"])
                self.assertTrue(entry["bracket_samples"]["after"]["xy"]["status"]["native_status"]["reference_unlock_latched"])

    def test_omitted_unlock_policy_still_aborts_under_overload_opt_in(self):
        self.inject("xy", unlock_policy=None)
        result = self.run_formal_fault()
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertTrue(any(kind == "lockin_raw_role" and payload["role"] == "lockin_xy"
                            and payload["sample"]["status"]["native_status"]["reference_unlock_latched"]
                            for kind, payload in self.events()))
        self.assert_shared_cleanup_protection(result)

    def test_overload_alone_retains_the_unaffected_companion(self):
        self.inject("xy", lia=16)
        result = self.run_formal_fault()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assert_shared_cleanup_protection(result)
        self.assertEqual(result["overload_summary"]["continued_reference_unlock_pairs"], 0)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(XX in row and XY not in row for row in rows))
        self.assertTrue(select_series(rows, x="measured.optical_power_w", y=XX))

    def test_unknown_status_mixed_with_unlock_remains_fatal(self):
        # SR865A bit 2 is unknown; bit 3 is reference unlock.
        self.inject("xy", lia=12)
        result = self.run_formal_fault()
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_shared_cleanup_protection(result)

    def test_filter_fault_mixed_with_unlock_remains_fatal(self):
        # SR865A bit 5 is synchronous-filter fault; bit 3 is unlock.
        self.inject("xy", lia=40)
        result = self.run_formal_fault()
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_shared_cleanup_protection(result)

    def test_instrument_error_mixed_with_unlock_remains_fatal(self):
        self.inject("xy", instrument_error=4)
        result = self.run_formal_fault()
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_shared_cleanup_protection(result)

    def test_legacy_archive_without_unlock_policy_cannot_be_reinterpreted(self):
        self.inject("xy")
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())

        def omit_policy(reading):
            reading["status"].pop("reference_unlock_policy")
            for entry in reading["status"]["samples"]:
                entry.pop("reference_unlock_policy")

        self.alter_stored_readings(omit_policy)
        self.assertEqual(load_combination_rows(self.database), ())
        audit = load_combination_rows(self.database, audit=True)
        self.assertEqual(len(audit), 6)
        self.assertTrue(all(XX in row and XY in row and not row["accepted"] for row in audit))

    def test_archive_cannot_assert_companion_valid_after_shared_unlock(self):
        self.inject("xy")
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())

        def make_companion_valid(reading):
            for entry in reading["status"]["samples"]:
                entry["valid_for_analysis_by_role"]["lockin_xx"] = True

        self.alter_stored_readings(make_companion_valid)
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(len(load_combination_rows(self.database, audit=True)), 6)

    def test_archive_top_level_abort_cannot_authorize_entry_unlock(self):
        self.inject("xy")
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())
        self.alter_stored_readings(lambda reading: reading["status"].update(
            reference_unlock_policy="abort"))
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(len(load_combination_rows(self.database, audit=True)), 6)

    def test_archive_missing_top_level_policy_cannot_borrow_entry_opt_in(self):
        self.inject("xy")
        self.assert_unlock_diagnostic_complete(self.run_formal_fault())
        self.alter_stored_readings(lambda reading: reading["status"].pop(
            "reference_unlock_policy"))
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(len(load_combination_rows(self.database, audit=True)), 6)


if __name__ == "__main__":
    unittest.main()
