"""PEM harmonic declarations and acquisition with only synthetic transports."""
from contextlib import closing
import json
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_store import open_readonly
from attodry_control.combination_terminal import launch_text
from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.photonics_lockin_helpers import (
    FakeManager, reversed_sine_document, reversed_sine_safety_document,
)
from tests import test_combination_photonics as combination_fixture


def two_f_document():
    document = reversed_sine_document()
    document["photonics_lockin"].update(
        pem_reference_harmonic=2, reference_min_hz=98000.0,
        reference_max_hz=102000.0, reference_expected_hz=100054.0,
    )
    document["lockin_xy"]["harmonics"] = [1, 2]
    return document


class PemReferenceHarmonicTests(unittest.TestCase):
    def setUp(self):
        # Fail before opening a real transport even if a factory injection is lost.
        guard = patch.dict("sys.modules", {"pyvisa": None, "serial": None})
        guard.start()
        self.addCleanup(guard.stop)

    def load(self, document):
        return load_photonics_lockin_config(
            "fake.toml", document=document,
            safety_document=reversed_sine_safety_document(),
        )

    def station(self, *, multiplier=2, references=None, pem_frequency=50027.0):
        document = two_f_document() if multiplier == 2 else reversed_sine_document()
        if multiplier == 1:
            document["photonics_lockin"]["pem_reference_harmonic"] = 1
        config = SimpleNamespace(reference_topology="pem_xy_xx_sine", lockin=self.load(document))
        station = HardwareCombinationStation(config)
        events = []
        station.set_event_sink(lambda kind, payload: events.append((kind, payload)))
        expected = multiplier * pem_frequency
        station.lockin = SimpleNamespace(
            wait_for_reference_lock=lambda: None,
            run_reference_operation=lambda stage, operation: operation(),
            read_reference_frequencies=lambda **_: (
                {"xx": expected, "xy": expected} if references is None else references
            ),
        )
        station.optical = SimpleNamespace(
            pem=object(), last_state={"pem": {"frequency_hz": pem_frequency}},
        )
        return station, events

    def test_omitted_multiplier_preserves_one_f_configuration_and_grid(self):
        config = self.load(reversed_sine_document())
        self.assertEqual(config.pem_reference_harmonic, 1)
        session = PhotonicsLockinPointSession("fake.toml", config, lambda *_: None)
        self.assertEqual(session.grid[0].frequency_hz, 50027.0)
        self.assertEqual(session.grid[0].metadata()["pem_reference_harmonic"], 1)

    def test_exact_integer_one_and_two_are_the_only_multiplier_values(self):
        for multiplier in (1, 2):
            document = reversed_sine_document()
            document["photonics_lockin"]["pem_reference_harmonic"] = multiplier
            with self.subTest(multiplier=multiplier):
                self.assertEqual(self.load(document).pem_reference_harmonic, multiplier)
        for multiplier in (
            0, -1, 3, 4, True, False, 1.0, 2.0, "1", "2", "2f", None,
            [], {}, float("nan"), float("inf"),
        ):
            document = reversed_sine_document()
            document["photonics_lockin"]["pem_reference_harmonic"] = multiplier
            with self.subTest(multiplier=multiplier), self.assertRaisesRegex(
                ValueError, "pem_reference_harmonic"
            ):
                self.load(document)

    def test_two_f_preserves_mechanical_frequency_and_records_expected_external_reference(self):
        station, events = self.station()
        station._verify_optical_reference()
        self.assertEqual(station.optical.last_state["pem"]["frequency_hz"], 50027.0)
        event = next(payload for kind, payload in events if kind == "photonics_reference_check")
        self.assertEqual(event["pem_reference_harmonic"], 2)
        self.assertEqual(event["pem_frequency_hz"], 50027.0)
        self.assertEqual(event["expected_external_reference_hz"], 100054.0)
        self.assertEqual(event["xx_reference_frequency_hz"], 100054.0)
        self.assertEqual(event["xy_reference_frequency_hz"], 100054.0)

    def test_one_f_crosscheck_remains_one_to_one(self):
        station, events = self.station(multiplier=1)
        station._verify_optical_reference()
        event = next(payload for kind, payload in events if kind == "photonics_reference_check")
        self.assertEqual(event["pem_reference_harmonic"], 1)
        self.assertEqual(event["expected_external_reference_hz"], 50027.0)

    def test_matching_pair_at_wrong_pem_ratio_still_fails(self):
        # Both references pass their shared approved interval and pair check.
        station, events = self.station(references={"xx": 100040.0, "xy": 100040.0})
        session = PhotonicsLockinPointSession("fake.toml", station.config.lockin, lambda *_: None)
        session._check_frequencies({"xx": 100040.0, "xy": 100040.0})
        with self.assertRaisesRegex(ValueError, "PEM frequency"):
            station._verify_optical_reference()
        self.assertTrue(any(kind == "photonics_reference_check" for kind, _ in events))

    def test_pem_tolerance_is_in_actual_external_reference_hz_and_checks_each_role(self):
        for role in ("xx", "xy"):
            for difference in (-1.0, 1.0):
                references = {"xx": 100054.0, "xy": 100054.0}
                references[role] += difference
                station, _ = self.station(references=references)
                with self.subTest(role=role, difference=difference):
                    station._verify_optical_reference()
            references = {"xx": 100054.5, "xy": 100054.5}
            references[role] = 100055.001
            station, _ = self.station(references=references)
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "PEM frequency"):
                station._verify_optical_reference()

    def test_two_f_pair_and_interval_checks_remain_independent(self):
        session = PhotonicsLockinPointSession("fake.toml", self.load(two_f_document()), lambda *_: None)
        for frequency in (98000.0, 100054.0, 102000.0):
            session._check_frequencies({"xx": frequency, "xy": frequency})
        with self.assertRaisesRegex(ValueError, "external references disagree"):
            session._check_frequencies({"xx": 100054.0, "xy": 100055.001})
        with self.assertRaisesRegex(ValueError, "approved interval"):
            session._check_frequencies({"xx": 97999.999, "xy": 98000.0})
        with self.assertRaises(ValueError):
            session._check_frequencies({"xx": 102000.001, "xy": 102000.001})

    def test_sr830_h2_at_two_f_rejects_offline_while_sr865a_h2_remains_available(self):
        document = two_f_document()
        config = self.load(document)
        self.assertEqual(config.lockin_xx.harmonics, (1,))
        self.assertEqual(config.lockin_xy.harmonics, (1, 2))
        document["lockin_xx"]["harmonics"] = [2]
        with self.assertRaises(ValueError):
            self.load(document)

    def make_combination_fixture(self):
        # Compose the existing complete fake station fixture; do not inherit and
        # rediscover all its unrelated regression tests in this new test module.
        fixture = combination_fixture.PhotonicsCombinationTests(
            "test_loading_preview_no_io_and_model_parameters_visible"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        document = tomllib.loads(fixture.path.read_text(encoding="utf-8"))
        lockin = two_f_document()
        for key in ("lockin_xx", "lockin_xy", "photonics_lockin", "lockin_sweep"):
            document[key] = lockin[key]
        document["lockin_sweep"]["excitation_points_v_rms"] = [0.004]
        document["combination_scan"]["reference_topology"] = "pem_xy_xx_sine"
        document["nkt_run"]["points"] = document["nkt_run"]["points"][:1]
        document["power_feedback"]["target_powers_w"] = [0.000005]
        fixture.path.write_text(combination_fixture.toml_document(document), encoding="utf-8")
        (fixture.directory / "photonics_lockin_safety.toml").write_text(
            combination_fixture.toml_document(reversed_sine_safety_document()), encoding="utf-8",
        )
        fixture.manager = FakeManager(document)
        for resource in fixture.manager.resources.values():
            resource.responses.update({"FREQ?": "100054", "FREQEXT?": "100054", "FREQDET?": "100054"})
            resource.on_query = fixture.on_lockin_query
            resource.on_write = lambda r, c: fixture.log.append(("lockin", r.role, c))
        xy = fixture.manager.resources["FAKE::XY"]
        xy.responses["SLVL?"], xy.responses["BLAZEX?"] = "0.2", "0"
        return fixture

    def test_summary_disambiguates_lockin_harmonics_from_pem_harmonics_without_io(self):
        fixture = self.make_combination_fixture()
        config = load_hardware_combination(fixture.path)
        summary = launch_summary(config, fixture.database, "two-f-preview")
        lockin = summary["lockin"]
        self.assertEqual(lockin["pem_reference_harmonic"], 2)
        self.assertEqual(lockin["pem_harmonics_by_role"], {"xx": [2], "xy": [2, 4]})
        self.assertEqual(lockin["expected_detection_hz_by_role"], {"xx": [100054.0], "xy": [100054.0, 200108.0]})
        self.assertEqual(config.optical.pem.frequency_min_hz, 49000.0)
        self.assertEqual(config.optical.pem.frequency_max_hz, 51000.0)
        self.assertIn("2f", launch_text(summary))
        self.assertEqual(fixture.manager.opened, [])
        self.assertIsNone(fixture.backend)

    def test_full_fake_run_with_matching_but_wrong_references_fails_before_emission(self):
        fixture = self.make_combination_fixture()
        for resource in fixture.manager.resources.values():
            resource.responses.update({"FREQ?": "100060", "FREQEXT?": "100060", "FREQDET?": "100060"})
        result = fixture.execute("two-f-wrong-ratio", confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("PEM frequency", result["error"])
        self.assertFalse(any(command == ("emission", True) for command in fixture.backend.commands))
        with closing(open_readonly(fixture.database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM combination_samples WHERE run_id=?", ("two-f-wrong-ratio",)
            ).fetchone()[0], 0)
        checks = [payload for kind, payload in fixture.events() if kind == "photonics_reference_check"]
        self.assertTrue(checks)
        self.assertEqual(checks[-1]["pem_reference_harmonic"], 2)
        self.assertEqual(checks[-1]["expected_external_reference_hz"], 100054.0)
        self.assertEqual(fixture.backend.source["emission_state"], 0)
        self.assertFalse(fixture.pem.output_command)
        self.assertTrue(fixture.manager.closed and fixture.pem.closed and fixture.meter.closed)

    def test_complete_two_f_fake_run_persists_frequency_meaning_and_never_selects_hardware_output(self):
        fixture = self.make_combination_fixture()
        result = fixture.execute("two-f-fake", confirm_xy_sine_disconnected=False)
        self.assertEqual(result["status"], "completed", result["error"] or result["cleanup"]["errors"])
        with closing(open_readonly(fixture.database)) as connection:
            plan = json.loads(connection.execute(
                "SELECT plan_json FROM combination_runs WHERE run_id=?", ("two-f-fake",)
            ).fetchone()[0])
            samples = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM combination_samples WHERE run_id=?", ("two-f-fake",)
            )]
        self.assertEqual(plan["hardware"]["lockin"]["pem_reference_harmonic"], 2)
        self.assertEqual(len(samples), 2)
        observed = set()
        for sample in samples:
            lockin = next(read for read in sample["reads"] if read["module"] == "lockin")
            self.assertEqual(lockin["status"]["pem_reference_harmonic"], 2)
            for pair in lockin["status"]["samples"]:
                for role, metadata in pair["harmonic_metadata"].items():
                    harmonic = pair["selected_harmonics"][role]
                    self.assertEqual(metadata["pem_reference_harmonic"], 2)
                    self.assertEqual(metadata["pem_harmonic"], 2 * harmonic)
                    self.assertEqual(metadata["reference_frequency_hz"], 100054.0)
                    self.assertEqual(metadata["detection_frequency_hz"], 100054.0 * harmonic)
                    observed.add((role, harmonic, metadata["pem_harmonic"]))
        self.assertEqual(observed, {("xx", 1, 2), ("xy", 1, 2), ("xy", 2, 4)})
        raw_roles = [payload for kind, payload in fixture.events() if kind == "lockin_raw_role"]
        self.assertTrue(raw_roles)
        for payload in raw_roles:
            self.assertEqual(payload["pem_reference_harmonic"], 2)
            self.assertEqual(payload["pem_harmonic"], 2 * payload["sample"]["harmonic"])
        self.assertFalse(any(item[0] == "pem" and item[1].startswith(":DET:HARMT") for item in fixture.log))
        self.assertFalse(any(command.startswith("FREQ ") for resource in fixture.manager.resources.values()
                             for command in resource.writes))
        self.assertEqual(fixture.manager.resources["FAKE::XY"].responses["SLVL?"], "0.2")
        self.assertEqual(fixture.backend.source["emission_state"], 0)
        self.assertFalse(fixture.pem.output_command)
        self.assertTrue(fixture.manager.closed and fixture.pem.closed and fixture.meter.closed)


if __name__ == "__main__":
    unittest.main()
