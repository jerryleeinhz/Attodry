"""Explicit CLD model selection and 980 nm acquisition, with fake hardware only."""
from dataclasses import replace
from pathlib import Path
import tomllib
import unittest
from unittest.mock import Mock

from optical_helpers import Clock
from tests.test_cld_drivers import Dll, configuration, controller, visa_backend
from tests.test_cld_startup_voltage_recheck import StartupBackend
from tests.test_photonics_chopper_template import substitute
from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_cld import (
    CldController, CldPoint, SimulatedCld, TL4000, VisaCld, load_cld_config,
)


ROOT = Path(__file__).resolve().parents[1]
MODELS = ("CLD1010LP", "CLD1015")


class CldModelTests(unittest.TestCase):
    def document(self):
        return substitute(tomllib.loads(
            (ROOT / "config/photonics_cld.example.toml").read_text(encoding="utf-8")))

    def test_legacy_table_defaults_to_cld1010lp_and_explicit_980nm_is_archived(self):
        doc = self.document()
        doc["cld1010"].pop("expected_model", None)
        legacy = load_cld_config(doc, ROOT / "config/fake.toml")
        self.assertEqual(legacy.expected_model, "CLD1010LP")
        doc["cld1010"].update(expected_model="CLD1015", wavelength_nm=980.0)
        selected = load_cld_config(doc, ROOT / "config/fake.toml")
        self.assertEqual(selected.expected_model, "CLD1015")
        self.assertTrue(all(point.wavelength_nm == 980 for point in selected.points))
        snapshot = selected.snapshot()
        self.assertEqual(snapshot["expected_model"], "CLD1015")
        self.assertEqual(snapshot["points"][0]["wavelength_nm"], 980)
        for name in ("max_current_a", "max_tec_current_a", "temperature_min_c",
                     "temperature_max_c", "temperature_c", "stable_dwell_s"):
            self.assertEqual(getattr(selected, name), getattr(legacy, name))

    def test_unknown_models_and_existing_current_ceiling_fail_before_open_or_init(self):
        for model in ("CLD1000", "CLD1011LP", "cld1015", "", None, ["CLD1015"]):
            with self.subTest(model=model):
                config = configuration(expected_model=model)
                factory = Mock()
                with self.assertRaisesRegex(NktError, "expected_model"):
                    VisaCld(config, authorize_connection=True, manager_factory=factory)
                factory.assert_not_called()
                dll = Dll()
                with self.assertRaisesRegex(NktError, "expected_model"):
                    TL4000(replace(config, backend="tl4000", dll_path="fake.dll"),
                           authorize_connection=True, authorize_writes=True, dll=dll)
                self.assertEqual(dll.calls, [])
        for model in MODELS:
            with self.subTest(model=model):
                configuration(expected_model=model, max_current_a=1.0).validate()
                with self.assertRaises(NktError):
                    configuration(expected_model=model, max_current_a=1.000001).validate()

    def test_matching_visa_snapshot_keeps_exact_identity_and_existing_query_budget(self):
        for model in MODELS:
            with self.subTest(model=model):
                config = configuration(expected_model=model, points=(CldPoint(.01, 980),))
                driver, resource, manager = visa_backend(config, writes=False)
                self.addCleanup(driver.close)
                resource.values["*IDN?"] = f"Thorlabs,{model},TEST_SERIAL,2.3.0"
                state = driver.snapshot()
                self.assertEqual(state["identity"][1], model)
                self.assertEqual(sum(kind == "query" for kind, _ in resource.calls), 25)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
                driver.close()
                manager.close.assert_called_once()

    def test_selected_visa_model_rejects_other_model_and_drift_without_setting_write(self):
        for expected, observed in (("CLD1010LP", "CLD1015"), ("CLD1015", "CLD1010LP")):
            with self.subTest(expected=expected, observed=observed):
                driver, resource, _ = visa_backend(configuration(expected_model=expected))
                self.addCleanup(driver.close)
                resource.values["*IDN?"] = f"Thorlabs,{expected},TEST_SERIAL,2.3.0"
                driver.snapshot()
                resource.values["*IDN?"] = f"Thorlabs,{observed},TEST_SERIAL,2.3.0"
                with self.assertRaisesRegex(NktError, "manufacturer/model/serial"):
                    driver.set("setLdCurrSetpoint", .01)
                self.assertEqual(driver.transcript[-1]["response"], resource.values["*IDN?"] + "\n")
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))

    def test_cld1015_rejects_wrong_manufacturer_serial_or_missing_firmware(self):
        for identity in ("Other,CLD1015,TEST_SERIAL,2.3.0",
                         "Thorlabs,CLD1015,WRONG,2.3.0", "Thorlabs,CLD1015,TEST_SERIAL,"):
            with self.subTest(identity=identity):
                driver, resource, _ = visa_backend(configuration(expected_model="CLD1015"))
                self.addCleanup(driver.close)
                resource.values["*IDN?"] = identity
                with self.assertRaises(NktError):
                    driver.set("setLdCurrSetpoint", .01)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))

    def test_both_models_use_same_980nm_lifecycle_commands_waits_and_cleanup(self):
        traces = []
        for model in MODELS:
            with self.subTest(model=model):
                config = configuration(expected_model=model, points=(CldPoint(.01, 980),))
                driver, resource, _ = visa_backend(config)
                self.addCleanup(driver.close)
                resource.values["*IDN?"] = f"Thorlabs,{model},TEST_SERIAL,2.3.0"
                session = controller(driver)
                session.begin(authorize_writes=True, confirm_manual_route=True)
                session.prepare_point(config.points[0])
                result = session.set_emission(True)
                self.assertEqual(result["nominal_wavelength_nm"], 980)
                self.assertEqual(result["source"]["identity"][1], model)
                cleanup = session.finish()
                self.assertTrue(cleanup["off_confirmed"])
                self.assertTrue(cleanup["transport_closed"])
                self.assertEqual(cleanup["errors"], [])
                self.assertEqual(float(resource.values["SOUR1:CURR:LEV:IMM:AMPL?"]), 0)
                traces.append((resource.calls, session.clock()))
        self.assertEqual(traces[0], traces[1])

    def test_cld1015_protection_and_unknown_connection_still_block_emission(self):
        for command in ("OUTP1:PROT:INTL:TRIP?", "OUTP2:PROT:TRAN:TRIP?",
                        "OUTP1:PROT:CONN:TRIP?", "OUTP2:PROT:CONN:TRIP?"):
            with self.subTest(command=command):
                driver, resource, _ = visa_backend(configuration(expected_model="CLD1015"))
                self.addCleanup(driver.close)
                resource.values["*IDN?"] = "Thorlabs,CLD1015,TEST_SERIAL,2.3.0"
                resource.values[command] = "1"
                session = controller(driver)
                try:
                    with self.assertRaises(NktError):
                        session.begin(authorize_writes=True, confirm_manual_route=True)
                        session.prepare_point(driver.config.points[0])
                        session.set_emission(True)
                    self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
                finally:
                    session.finish()

    def test_native_selected_model_is_exact_and_raw_mismatches_are_preserved(self):
        for expected in MODELS:
            for observed in MODELS:
                with self.subTest(expected=expected, observed=observed):
                    config = configuration(backend="tl4000", dll_path="fake.dll", expected_model=expected)
                    dll = Dll()
                    dll.identity[1] = observed.encode("ascii")
                    driver = TL4000(config, authorize_connection=True, authorize_writes=True, dll=dll)
                    self.addCleanup(driver.close)
                    if expected == observed:
                        self.assertEqual(driver.snapshot()["identity"][1], expected)
                    else:
                        with self.assertRaises(NktError):
                            driver.snapshot()
                        self.assertEqual(driver.transcript[-1]["raw_identity"][1], observed)
                        self.assertFalse(any(name.startswith(("set", "switch")) for name, _ in dll.calls))
        for index, value in ((0, b"Other"), (2, b"WRONG"), (3, b"")):
            with self.subTest(index=index):
                config = configuration(backend="tl4000", dll_path="fake.dll", expected_model="CLD1015")
                dll = Dll()
                dll.identity[1] = b"CLD1015"
                dll.identity[index] = value
                driver = TL4000(config, authorize_connection=True, authorize_writes=True, dll=dll)
                self.addCleanup(driver.close)
                with self.assertRaises(NktError):
                    driver.snapshot()
                self.assertEqual(driver.transcript[-1]["raw_identity_hex"][index], value.hex())

    def test_simulation_preserves_selected_model(self):
        config = configuration(expected_model="CLD1015", backend="simulation")
        self.assertEqual(SimulatedCld(config).snapshot()["identity"][1], "CLD1015")

    def test_cld1015_persistent_negative_startup_rejects_after_the_same_bounded_wait(self):
        config = configuration(expected_model="CLD1015", poll_interval_s=.2)
        backend = StartupBackend(config, lambda n: {"measLdVolt": -.0001696199})
        clock = Clock()
        session = CldController(config, backend, clock=clock, sleep=clock.sleep)
        session.begin(authorize_writes=True, confirm_manual_route=True)
        session.prepare_point(config.points[0])
        start = clock()
        with self.assertRaisesRegex(NktError, "diode voltage"):
            session.set_emission(True)
        self.assertAlmostEqual(clock()-start, 2.)
        self.assertTrue(any(row["phase"] == "cld_startup_voltage_wait" for row in session.record["events"]))
        self.assertTrue(session.failed)
        self.assertEqual(backend.writes.count(("switchLdOutput", True)), 1)
        self.assertTrue(session.finish()["off_confirmed"])


if __name__ == "__main__":
    unittest.main()
