"""Mode-transition safety and archived optical plotting contracts, offline only."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optical_helpers import Clock
from tests.test_nkt import FakeApi, example, hardware_config
from tests.test_notebook_optical_source_grouping import optical_rows, POWER, SOURCE
from tests.test_unified_plotting import observation, Y
from attodry_control.analysis_observations import channel_quality, qualify_observations
from attodry_control.nkt_config import NktError, NktPoint, load_nkt_config
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.nkt_sdk import NktSdk
from attodry_control.unified_plotting import _known_label, _unresolved_dimensions


class NktControlModeContracts(unittest.TestCase):
    def make(self, *, allowed=True):
        config = replace(example(), source_control="power", allow_control_mode_change=allowed)
        backend = SimulatedNkt(config)
        backend.source["control"] = "current"
        clock = Clock()
        controller = NktController(config, backend, clock=clock, sleep=clock.sleep)
        return controller, backend

    def settings(self, backend):
        return [item for item in backend.commands if item[0] not in ("read_source", "read_filter", "close")]

    def test_default_is_false_and_mode_mismatch_never_writes_during_preflight(self):
        self.assertFalse(example().allow_control_mode_change)
        controller, backend = self.make(allowed=False)
        with self.assertRaisesRegex(NktError, "control mode"):
            controller.begin_session()
        self.assertEqual(self.settings(backend), [])
        self.assertEqual(backend.source["control"], "current")
        self.assertFalse(controller.session_owned)

    def test_explicit_optin_delays_mode_write_until_configure_session(self):
        controller, backend = self.make()
        source, _ = controller.begin_session()
        self.assertEqual(source["control"], "current")
        self.assertTrue(controller.session_owned)
        self.assertEqual(self.settings(backend), [])
        controller.configure_session()
        self.assertEqual(self.settings(backend), [("control", "power")])
        self.assertEqual(backend.source["control"], "power")
        self.assertEqual(backend.source["emission_state"], 0)
        controller.configure_session()
        self.assertEqual(self.settings(backend), [("control", "power")])
        self.assertTrue(controller.finish_session()["off_confirmed"])

    def test_active_unknown_or_hard_fault_rejects_preflight_without_mode_write(self):
        bad = ({"emission_state": 3, "status_bits": 1}, {"emission_state": 2},
               {"status_bits": 0x8000}, {"interlock": 0}, {"control": "unsupported_setup_7"})
        for change in bad:
            controller, backend = self.make()
            backend.source.update(change)
            original_mode = backend.source["control"]
            with self.subTest(change=change), self.assertRaises(NktError):
                controller.begin_session()
            self.assertEqual(self.settings(backend), [])
            self.assertEqual(backend.source["control"], original_mode)

    def test_configure_rechecks_fresh_off_health_and_supported_mode(self):
        for change in ({"emission_state": 3, "status_bits": 1}, {"emission_state": 2},
                       {"status_bits": 0x8000}, {"interlock": 0}, {"control": "unsupported_setup_7"}):
            controller, backend = self.make()
            controller.begin_session()
            backend.source.update(change)
            original_mode = backend.source["control"]
            with self.subTest(change=change), self.assertRaises(NktError):
                controller.configure_session()
            self.assertEqual(self.settings(backend), [])
            self.assertEqual(backend.source["control"], original_mode)
            controller.finish_session()

    def test_loader_optin_is_strict_boolean_and_keeps_omitted_compatibility(self):
        source = (Path(__file__).resolve().parents[1] / "config/nkt_simulation.toml").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.toml"
            for value, expected in (("true", True), ("false", False)):
                path.write_text(source.replace('[nkt_source]\n',
                    '[nkt_source]\nallow_control_mode_change = ' + value + '\n'), encoding="utf-8")
                self.assertIs(load_nkt_config(path).allow_control_mode_change, expected)
            path.write_text(source.replace('[nkt_source]\n',
                '[nkt_source]\nallow_control_mode_change = "true"\n'), encoding="utf-8")
            with self.assertRaises(NktError):
                load_nkt_config(path)

    def sdk(self, *, writable=True):
        api = FakeApi()
        backend = NktSdk(hardware_config(source_control="power", allow_control_mode_change=True),
                         api=api, authorize_writes=writable).open(authorize_connection=True)
        self.addCleanup(backend.close)
        return backend, api

    def test_sdk_setup_031_is_read_before_write_and_verified_without_nd_write(self):
        backend, api = self.sdk()
        backend.set_control("power")
        writes = [item for item in api.calls if item[0] == "write"]
        self.assertEqual(writes, [("write", 1, 0x31, 1, 2)])
        position = api.calls.index(writes[0])
        self.assertIn(("read", 1, 0x31), api.calls[:position])
        self.assertIn(("read", 1, 0x30), api.calls[:position])
        self.assertIn(("read", 1, 0x32), api.calls[:position])
        self.assertIn(("read", 1, 0x66), api.calls[:position])
        self.assertEqual(api.calls[position + 1], ("read", 1, 0x31))
        backend.set_control("power")
        self.assertEqual([item for item in api.calls if item[0] == "write"], writes)
        backend.close()

    def test_sdk_mode_optin_does_not_bypass_unverified_nd_boundary(self):
        backend, api = self.sdk()
        backend.set_control("power")
        mode_writes = [item for item in api.calls if item[0] == "write"]
        with self.assertRaisesRegex(NktError, "not optically verified"):
            backend.configure(NktPoint(8, 650, 30, 20))
        self.assertEqual([item for item in api.calls if item[0] == "write"], mode_writes)
        self.assertEqual(api.data[2, 0x32], (100).to_bytes(2, "little"))
        backend.close()

    def test_sdk_unauthorized_active_unknown_and_hard_status_never_change_setup(self):
        backend, api = self.sdk(writable=False)
        with self.assertRaises(NktError):
            backend.set_control("power")
        self.assertFalse(any(item[0] == "write" for item in api.calls))
        backend.close()
        for register, raw in ((0x30, b"\x03"), (0x31, b"\x07\x00"),
                              (0x66, b"\x00\x80"), (0x32, b"\x00\x00")):
            backend, api = self.sdk()
            api.data[1, register] = raw
            with self.subTest(register=register), self.assertRaises(NktError):
                backend.set_control("power")
            self.assertFalse(any(item[0] == "write" for item in api.calls))
            backend.close()


class OpticalAnalysisContracts(unittest.TestCase):
    DELTA = "measured.lockin_xy_h2_delta_amplitude_v"
    PHASE = "measured.lockin_xy_h2_delta_phase_deg"

    def delta_row(self):
        row = observation(value=1e-6)
        row["measured.lockin_xy_h2_phase_deg"] = 10
        row[self.DELTA] = 0.5e-6
        row[self.PHASE] = 20
        row["status.lockin"]["background"] = {"baseline_id": "fake-baseline", "valid_for_analysis": True,
            "channels": {"xy:h2": {"valid_for_analysis": True, "phase_defined": True},
                         "xx:h1": {"valid_for_analysis": False, "phase_defined": False}}}
        return row

    def unresolved(self, rows, *, plot_keys=(POWER, Y), group_keys=()):
        return _unresolved_dimensions(rows, plot_keys=plot_keys, group_keys=group_keys, filter_keys=())

    def feedback_rows(self, actuator="source_current", *, cld=False):
        rows = optical_rows()
        for index, row in enumerate(rows):
            row["status.optical"] = {"power_target_w": row["requested.optical_target_power_w"],
                                     "feedback_actuator": actuator}
            if cld:
                row.pop(SOURCE)
                row.pop("actual.optical_source_level_pct")
                row["requested.optical_current_a"] = 0.02 + index * 0.01
                row["actual.optical_current_a"] = 0.02 + index * 0.01
        return rows

    def test_delta_channel_requires_both_formal_status_and_matched_background(self):
        row = self.delta_row()
        original = deepcopy(row)
        self.assertEqual(channel_quality(row, self.DELTA), ("clear", ()))
        self.assertEqual(channel_quality(row, self.PHASE), ("clear", ()))
        row["status.lockin"]["background"]["channels"]["xy:h2"]["valid_for_analysis"] = False
        self.assertEqual(channel_quality(row, self.DELTA), ("flagged", ("invalid_dark_background",)))
        kept, report = qualify_observations([row], [self.DELTA])
        self.assertEqual(kept, ())
        self.assertEqual(report["excluded_quality_count"], 1)
        self.assertEqual(channel_quality(original, Y), ("clear", ()))

    def test_zero_differential_phase_is_flagged_and_original_overload_stays_flagged(self):
        row = self.delta_row()
        row["status.lockin"]["background"]["channels"]["xy:h2"]["phase_defined"] = False
        self.assertEqual(channel_quality(row, self.PHASE), ("flagged", ("undefined_differential_phase",)))
        self.assertEqual(channel_quality(row, self.DELTA), ("clear", ()))
        row["status.lockin"]["samples"][1]["lockin_xy"]["lia_status"]["raw"] = 4
        self.assertIn("output_overload", channel_quality(row, self.DELTA)[1])
        row = self.delta_row()
        del row["status.lockin"]["background"]
        self.assertEqual(channel_quality(row, self.DELTA), ("unknown", ("missing_matched_dark_background",)))

    def test_delta_labels_have_difference_symbol_and_correct_units(self):
        for field, suffix, unit in (("x_v", "ΔX", "V"), ("y_v", "ΔY", "V"),
                                    ("amplitude_v", "ΔR", "V RMS"), ("phase_deg", "ΔPhase", "deg")):
            label, actual_unit = _known_label("lockin_xy_h2_delta_" + field)
            self.assertEqual(label, "Lock-in XY h2 " + suffix)
            self.assertEqual(actual_unit, unit)

    def test_only_confirmed_feedback_starts_stop_being_unresolved_conditions(self):
        for actuator, cld in (("source_current", False), ("source_power", False), ("cld_current", True)):
            rows = self.feedback_rows(actuator, cld=cld)
            before = deepcopy(rows)
            self.assertEqual(self.unresolved(rows), [])
            self.assertEqual(rows, before)
            key = "requested.optical_current_a" if cld else SOURCE
            rows[1]["status.optical"].pop("power_target_w")
            self.assertIn((key, 2), self.unresolved(rows))
        for actuator in (None, "varia_nd", "unknown", "cld_current"):
            with self.subTest(actuator=actuator):
                self.assertIn((SOURCE, 2), self.unresolved(self.feedback_rows(actuator)))
        self.assertIn((SOURCE, 2), self.unresolved(optical_rows()))

    def test_wavelength_bandwidth_gate_and_target_remain_independent_guards(self):
        for key, value in (("requested.optical_wavelength_nm", 620),
                           ("requested.optical_bandwidth_nm", 10),
                           ("requested.gate_bottom_v", 6)):
            rows = self.feedback_rows()
            rows[1][key] = value
            with self.subTest(key=key):
                self.assertIn((key, 2), self.unresolved(rows))
                self.assertNotIn((SOURCE, 2), self.unresolved(rows))
        rows = self.feedback_rows()
        self.assertIn(("requested.optical_target_power_w", 2), self.unresolved(rows,
                      plot_keys=("actual.gate_bottom_v", Y)))

    def test_explicit_start_grouping_and_archived_feedback_fallback_still_work(self):
        self.assertNotIn((SOURCE, 2), self.unresolved(optical_rows(), group_keys=(SOURCE,)))
        rows = self.feedback_rows()
        for row in rows:
            row["status.optical"].pop("feedback_actuator")
            row["status.optical"]["feedback"] = {"feedback_config": {"actuator": "source_current"}}
        self.assertEqual(self.unresolved(rows), [])

    def test_small_delta_power_curve_renders_with_feedback_start_provenance(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from attodry_control.unified_plotting import render_plot
        rows = self.feedback_rows()
        for index, row in enumerate(rows):
            row[self.DELTA] = (index + 1) * 0.5e-6
            row["status.lockin"]["background"] = self.delta_row()["status.lockin"]["background"]
        figure, data = render_plot(rows, dict(x=POWER, y=self.DELTA, statistics="raw"))
        try:
            figure.canvas.draw()
            self.assertEqual(data["report"]["plotted_row_count"], 2)
            self.assertEqual(data["report"]["excluded_quality_count"], 0)
            self.assertIn("ΔR", figure.axes[0].get_ylabel())
        finally:
            plt.close(figure)


if __name__ == "__main__":
    unittest.main()
