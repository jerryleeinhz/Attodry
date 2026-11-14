"""Condition boundaries must bracket every setting and every formal group."""
from contextlib import closing
import tempfile
from pathlib import Path
import unittest

from attodry_control.combination_scan import (
    AxisPoint, CombinationPlan, ScanAxis, SimulatedCombinationStation,
    run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore, open_readonly


class GatedStation(SimulatedCombinationStation):
    def __init__(self, *, fail_begin=False, fail_end=False):
        super().__init__()
        self.on = False
        self.fail_begin, self.fail_end = fail_begin, fail_end

    def begin_condition(self, condition):
        self.operations.append(("begin_condition", condition["condition_id"]))
        if self.fail_begin:
            raise OSError("OFF confirmation unavailable")
        self.on = False

    def prepare_lockin_ranges(self, condition):
        assert not self.on, "Range change while excitation ON"
        self.operations.append(("prepare_ranges", condition["condition_id"]))

    def apply(self, module, point):
        assert not self.on, "Axis change while excitation ON"
        return super().apply(module, point)

    def qualify_axis(self, module, point, scope):
        assert not self.on, "Axis qualification while excitation ON"
        return super().qualify_axis(module, point, scope)

    def qualify(self, modules):
        self.on = True
        return super().qualify(modules)

    def read(self, module):
        assert self.on, "Formal read with excitation OFF"
        return super().read(module)

    def end_condition(self, condition):
        assert self.on
        self.operations.append(("end_condition", condition["condition_id"]))
        self.on = False
        if self.fail_end:
            raise OSError("Condition OFF verification failed")

    def cleanup(self, module, failed):
        self.on = False
        return super().cleanup(module, failed)


class ConditionOutputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "scan.sqlite"
        self.source = ScanAxis("excitation", tuple(AxisPoint({
            "excitation_current_rms_a": a, "excitation_frequency_hz": 450.0,
        }) for a in (1e-7, 2e-7)))
        self.detector = ScanAxis("lockin", (AxisPoint({}, metadata={"detector_only": True}),))

    def execute(self, plan, station, *, resume=False):
        with CombinationStore(self.path) as store:
            return run_simulated_combination(plan, store, "gated", station=station, resume=resume)

    def test_all_settings_and_outer_qualifications_are_off_with_multiple_samples(self):
        for outer in (
            ScanAxis("temperature", tuple(AxisPoint({"temperature_k": t}) for t in (2, 7))),
            ScanAxis("magnetic", tuple(AxisPoint({"field_x_t": b, "field_z_t": 0}) for b in (0, .1))),
            ScanAxis("smu", tuple(AxisPoint({"smu_bias_v": v}) for v in (0, .1))),
        ):
            with self.subTest(outer=outer.module):
                self.path.unlink(missing_ok=True)
                station = GatedStation()
                plan = CombinationPlan((outer, self.source, self.detector), samples_per_condition=2)
                result = self.execute(plan, station)
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(sum(op[0] == "begin_condition" for op in station.operations), 4)
                self.assertEqual(sum(op[0] == "end_condition" for op in station.operations), 4)
                self.assertEqual(sum(op[0] == "qualify" for op in station.operations), 4)
                with closing(open_readonly(self.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM combination_samples").fetchone()[0], 8)

    def test_begin_off_failure_prevents_all_axis_and_range_changes(self):
        station = GatedStation(fail_begin=True)
        result = self.execute(CombinationPlan((self.source, self.detector)), station)
        self.assertEqual(result["status"], "failed")
        self.assertIn("OFF confirmation unavailable", result["error"])
        self.assertFalse(any(op[0] in {"apply", "prepare_ranges", "qualify", "read"}
                             for op in station.operations))
        self.assertTrue(station.closed)

    def test_inner_smu_changes_remain_off_when_source_coordinate_is_unchanged(self):
        station = GatedStation()
        smu = ScanAxis("smu", tuple(AxisPoint({"smu_bias_v": v}) for v in (0, .1)))
        plan = CombinationPlan((self.source, smu, self.detector), repeats=2)
        result = self.execute(plan, station)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(sum(op[0] == "begin_condition" for op in station.operations), 8)
        self.assertEqual(sum(op[:2] == ("apply", "excitation") for op in station.operations), 4)
        self.assertEqual(sum(op[0] == "end_condition" for op in station.operations), 8)

    def test_end_off_failure_rejects_attempt_and_preserves_acquired_raw_sample(self):
        station = GatedStation(fail_end=True)
        result = self.execute(CombinationPlan((self.source, self.detector)), station)
        self.assertEqual(result["status"], "failed")
        with closing(open_readonly(self.path)) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT status FROM combination_attempts")], ["rejected"])
            self.assertEqual(db.execute("SELECT count(*) FROM combination_samples").fetchone()[0], 1)
        self.assertEqual(sum(op[0] == "begin_condition" for op in station.operations), 1)
        self.assertTrue(station.closed)

    def test_resume_runs_guard_again_only_for_pending_whole_condition(self):
        plan = CombinationPlan((self.source, self.detector))
        class FailSecond(GatedStation):
            def begin_condition(self, condition):
                if condition["sequence_index"] == 1:
                    raise RuntimeError("Injected pending condition failure")
                return super().begin_condition(condition)
        first = self.execute(plan, FailSecond())
        self.assertEqual(first["status"], "failed")
        station = GatedStation()
        result = self.execute(plan, station, resume=True)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual([op[1] for op in station.operations if op[0] == "begin_condition"],
                         ["condition-000001"])
        self.assertEqual(sum(op[0] == "end_condition" for op in station.operations), 1)

    def test_existing_station_without_external_excitation_has_no_new_hooks(self):
        class Legacy(SimulatedCombinationStation):
            def begin_condition(self, condition):
                raise AssertionError("Legacy source called external condition hook")
            end_condition = begin_condition
        legacy_axis = ScanAxis("lockin", (AxisPoint({
            "lockin_excitation_v_rms": .004, "lockin_frequency_hz": 17.0}),))
        self.assertEqual(self.execute(CombinationPlan((legacy_axis,)), Legacy())["status"], "completed")


if __name__ == "__main__":
    unittest.main()
