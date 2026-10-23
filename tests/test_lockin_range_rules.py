"""Pure plan/range compilation; no device adapters or hardware are used."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import math
import unittest

from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.lockin_range_rules import compile_lockin_range_rules


SCALES = {"lockin_xx": (20e-6, 50e-6, 100e-6),
          "lockin_xy": (20e-6, 50e-6, 100e-6)}
LOCKIN = ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .004,
                                      "lockin_frequency_hz": 17.0}),
                            AxisPoint({"lockin_excitation_v_rms": .008,
                                      "lockin_frequency_hz": 17.0})))


def axis(module, points):
    return ScanAxis(module, tuple(AxisPoint(point) for point in points))


def rule(name="all", match=None, **extra):
    return {"name": name, "match": {"temperature_k": [2, 4]} if match is None else match,
            "lockin_xx_full_scale_v": 50e-6, "lockin_xy_full_scale_v": 20e-6, **extra}


class LockinRangeRuleTests(unittest.TestCase):
    def setUp(self):
        self.temperature = axis("temperature", [{"temperature_k": 2}, {"temperature_k": 4}])
        self.plan = CombinationPlan((self.temperature, LOCKIN), repeats=2,
                                    samples_per_condition=3)

    def compile(self, rules=None, plan=None, scales=None):
        return compile_lockin_range_rules([rule()] if rules is None else rules,
                                         self.plan if plan is None else plan,
                                         SCALES if scales is None else scales)

    def test_temperature_rules_keep_plan_conditions_repeats_and_inner_points(self):
        before = deepcopy(self.plan.conditions())
        schedule = self.compile([rule("cold", {"temperature_k": [2]}),
                                 rule("warm", {"temperature_k": [4]},
                                      lockin_xx_full_scale_v=100e-6)])
        self.assertEqual(schedule.outer_modules, ("temperature",))
        self.assertEqual(len(schedule.entries), 2)
        self.assertEqual(schedule.resolve({"temperature": 0, "lockin": 1}).rule_name, "cold")
        self.assertEqual(schedule.resolve((1,)).full_scales_v["lockin_xx"], 100e-6)
        self.assertEqual(self.plan.conditions(), before)
        self.assertEqual(len(before), 8)

    def test_joint_coordinates_are_and_lists_are_or_with_all_outer_axes(self):
        smu = axis("smu", [{"smu_bias_v": .01, "gate_top_v": 1},
                           {"smu_bias_v": .02, "gate_top_v": 2}])
        magnetic = axis("magnetic", [{"field_x_t": 0, "field_z_t": -.1},
                                     {"field_x_t": 0, "field_z_t": .1}])
        plan = CombinationPlan((magnetic, smu, self.temperature, LOCKIN))
        rules = [rule("low", {"field_z_t": [-.1, .1], "smu_bias_v": [.01],
                               "temperature_k": [2, 4]}),
                 rule("high", {"smu_bias_v": [.02]},
                      lockin_xy_full_scale_v=100e-6)]
        schedule = self.compile(rules, plan)
        self.assertEqual(len(schedule.preview()), 8)
        self.assertEqual(schedule.resolve((1, 0, 1)).rule_name, "low")
        self.assertEqual(schedule.resolve((0, 1, 0)).rule_name, "high")
        self.assertEqual(schedule.resolve((0, 1, 0)).requested["smu_bias_v"], .02)

    def test_omitted_coordinates_do_not_limit_matching(self):
        schedule = self.compile()
        self.assertEqual([entry.rule_name for entry in schedule.entries], ["all", "all"])

    def test_duplicate_visits_have_same_ranges_by_default(self):
        duplicate = axis("magnetic", [{"field_x_t": 0, "field_z_t": .1}] * 2)
        schedule = self.compile([rule(match={"field_z_t": [.1]})],
                                CombinationPlan((duplicate, LOCKIN)))
        self.assertEqual(schedule.resolve((0,)).ranges, schedule.resolve((1,)).ranges)

    def test_point_indices_distinguish_duplicate_visits_without_direction_guessing(self):
        duplicate = axis("magnetic", [{"field_x_t": 0, "field_z_t": .1}] * 2)
        schedule = self.compile([
            rule("up", {"field_z_t": [.1]}, point_indices={"magnetic": [0]}),
            rule("down", {"field_z_t": [.1]}, point_indices={"magnetic": [1]},
                 lockin_xy_full_scale_v=100e-6)], CombinationPlan((duplicate, LOCKIN)))
        self.assertEqual(schedule.resolve((0,)).rule_name, "up")
        self.assertEqual(schedule.resolve((1,)).rule_name, "down")

    def test_point_indices_alone_are_supported(self):
        schedule = self.compile([rule(match={}, point_indices={"temperature": [0, 1]})])
        self.assertEqual(len(schedule.entries), 2)

    def test_aux_is_independent_and_all_active_ranges_are_required(self):
        scales = {**SCALES, "lockin_aux": (2e-6, 5e-6)}
        with self.assertRaisesRegex(ValueError, "every active"):
            self.compile(scales=scales)
        schedule = self.compile([rule(lockin_aux_full_scale_v=5e-6)], scales=scales)
        self.assertEqual(schedule.resolve((0,)).full_scales_v["lockin_aux"], 5e-6)
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            self.compile([rule(lockin_aux_full_scale_v=5e-6)])

    def test_missing_xy_range_is_rejected(self):
        raw = rule()
        del raw["lockin_xy_full_scale_v"]
        with self.assertRaisesRegex(ValueError, "every active"):
            self.compile([raw])

    def test_range_must_be_finite_positive_and_in_supported_allowed_intersection(self):
        for value in (0, -1, math.inf, math.nan, True, "50e-6", 30e-6):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "supported and allowed"):
                self.compile([rule(lockin_xx_full_scale_v=value)])

    def test_unknown_rule_fields_and_roles_are_rejected(self):
        for raw in (rule(priority=1), rule(default=True), rule(lockin_1_full_scale_v=50e-6)):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "unsupported fields"):
                self.compile([raw])
        with self.assertRaisesRegex(ValueError, "roles"):
            self.compile(scales={"lockin_xx": (50e-6,)})

    def test_empty_and_wrong_type_rule_arrays_are_errors(self):
        for raw in ([], (), {}, "rules", None):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "nonempty array"):
                compile_lockin_range_rules(raw, self.plan, SCALES)

    def test_rule_names_are_nonblank_and_unique(self):
        for name in (None, "", " ", 1):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "name"):
                self.compile([rule(name)])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.compile([rule(), rule()])

    def test_selector_cannot_be_empty_or_wrong_type(self):
        for raw in (rule(match={}), rule(match=[]), rule(point_indices=[])):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.compile([raw])

    def test_coordinate_values_are_unique_nonempty_finite_arrays(self):
        for values in ([], (), [2, 2], [True], [None], [math.inf], [math.nan], ["2"]):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "finite array"):
                self.compile([rule(match={"temperature_k": values})])

    def test_unknown_or_inner_coordinate_is_rejected(self):
        for key in ("temperature", "measured_temperature_k", "lockin_frequency_hz", "field_z_t"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "before lockin"):
                self.compile([rule(match={key: [2]})])
        plan = CombinationPlan((LOCKIN, self.temperature))
        with self.assertRaisesRegex(ValueError, "outside lockin"):
            self.compile(plan=plan)
        plan = CombinationPlan((self.temperature, LOCKIN, axis("smu", [{"gate_top_v": 0}])))
        with self.assertRaisesRegex(ValueError, "before lockin"):
            self.compile([rule(match={"gate_top_v": [0]})], plan)

    def test_electrical_plan_does_not_gain_optical_or_rotator_hardware(self):
        for module, coordinates in (("optical", {"optical_target_power_w": 1e-6}),
                                    ("rotator", {"rotator_1_angle_deg": 0})):
            plan = CombinationPlan((axis(module, [coordinates]), LOCKIN))
            with self.subTest(module=module), self.assertRaisesRegex(ValueError, "Unknown"):
                self.compile([rule(match={key: [value]
                                          for key, value in coordinates.items()})], plan)

    def test_coordinate_matching_uses_exact_plan_values_without_readback_tolerance(self):
        with self.assertRaisesRegex(ValueError, "absent from the plan"):
            self.compile([rule(match={"temperature_k": [2, 4.000000001]})])

    def test_point_indices_are_unique_zero_based_integers_within_outer_axis(self):
        for values in ([], [0, 0], [-1], [2], [True], [0.0], (0,), "0"):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "planned indices"):
                self.compile([rule(point_indices={"temperature": values})])
        with self.assertRaisesRegex(ValueError, "before lockin"):
            self.compile([rule(point_indices={"lockin": [0]})])

    def test_missing_condition_is_rejected_before_runtime(self):
        with self.assertRaisesRegex(ValueError, "unmatched.*temperature.*1"):
            self.compile([rule(match={"temperature_k": [2]})])

    def test_overlap_is_rejected_without_priority_or_order_override(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.compile([rule(), rule("overlap", {"temperature_k": [2]})])

    def test_unreachable_joint_rule_is_rejected(self):
        magnetic = axis("magnetic", [{"field_x_t": .1, "field_z_t": 0},
                                     {"field_x_t": 0, "field_z_t": .1}])
        plan = CombinationPlan((magnetic, LOCKIN))
        with self.assertRaisesRegex(ValueError, "Unreachable.*impossible"):
            self.compile([rule("all", {"field_x_t": [0, .1]}),
                          rule("impossible", {"field_x_t": [.1], "field_z_t": [.1]})], plan)

    def test_resolution_validates_outer_indices_and_ignores_inner_mapping_keys(self):
        schedule = self.compile()
        for indices in ({}, (), (0, 0), (-1,), (2,), (True,), (0.0,), [0]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                schedule.resolve(indices)
        self.assertEqual(schedule.resolve({"temperature": 1, "lockin": 99}).outer_indices, (1,))

    def test_snapshot_is_json_safe_recompilable_and_cannot_mutate_schedule(self):
        schedule = self.compile()
        snapshot = json.loads(json.dumps(schedule.snapshot(), allow_nan=False))
        again = self.compile(snapshot["rules"])
        self.assertEqual(again, schedule)
        snapshot["preview"][0]["full_scales_v"]["lockin_xx"] = 1
        returned = schedule.resolve((0,)).full_scales_v
        returned["lockin_xx"] = 1
        self.assertEqual(schedule.resolve((0,)).full_scales_v["lockin_xx"], 50e-6)
        with self.assertRaises(FrozenInstanceError):
            schedule.entries = ()

    def test_condition_preview_binds_every_existing_condition_and_repeat(self):
        schedule = self.compile([rule("cold", {"temperature_k": [2]}),
                                 rule("warm", {"temperature_k": [4]})])
        before = deepcopy(self.plan.conditions())
        rows = schedule.condition_preview(self.plan)
        self.assertEqual(len(rows), 8)
        self.assertEqual([row["condition_id"] for row in rows],
                         [item["condition_id"] for item in before])
        self.assertEqual([row["repeat_index"] for row in rows], [0] * 4 + [1] * 4)
        self.assertEqual([row["rule_name"] for row in rows],
                         ["cold", "cold", "warm", "warm"] * 2)
        self.assertEqual(rows[1]["outer_indices"], {"temperature": 0})
        self.assertEqual(rows[1]["requested"], before[1]["requested"])
        self.assertEqual(self.plan.conditions(), before)
        json.dumps(rows, allow_nan=False)
        rows[0]["full_scales_v"]["lockin_xy"] = 1
        rows[0]["requested"]["temperature_k"] = 99
        self.assertEqual(schedule.resolve((0,)).full_scales_v["lockin_xy"], 20e-6)
        self.assertEqual(self.plan.conditions(), before)

    def test_condition_preview_keeps_indices_for_duplicate_and_return_visits(self):
        magnetic = axis("magnetic", [{"field_x_t": 0, "field_z_t": value}
                                     for value in (0, .1, 0, -.1, 0)])
        plan = CombinationPlan((magnetic, LOCKIN), repeats=2)
        schedule = self.compile([rule("first", {}, point_indices={"magnetic": [0, 1]}),
                                 rule("return", {}, point_indices={"magnetic": [2, 3, 4]})], plan)
        rows = schedule.condition_preview(plan)
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["rule_name"], "first")
        self.assertEqual(rows[4]["rule_name"], "return")
        self.assertEqual(rows[4]["outer_indices"], {"magnetic": 2})
        self.assertEqual(rows[4]["requested"]["field_z_t"], 0)

    def test_condition_preview_rejects_changed_outer_plan(self):
        schedule = self.compile()
        plans = [CombinationPlan((LOCKIN, self.temperature)),
                 CombinationPlan((self.temperature,)),
                 CombinationPlan((axis("temperature", [{"temperature_k": 2}]), LOCKIN)),
                 CombinationPlan((axis("temperature", [{"temperature_k": 2},
                                                        {"temperature_k": 5}]), LOCKIN))]
        for plan in plans:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                schedule.condition_preview(plan)

    def test_condition_preview_rejects_added_outer_coordinate(self):
        original = CombinationPlan((axis("smu", [{"gate_top_v": 1}]), LOCKIN))
        schedule = self.compile([rule(match={"gate_top_v": [1]})], original)
        changed = CombinationPlan((axis("smu", [{"gate_top_v": 1, "gate_bottom_v": 2}]),
                                   LOCKIN))
        with self.assertRaisesRegex(ValueError, "compiled outer coordinates"):
            schedule.condition_preview(changed)

    def test_no_lockin_axis_cannot_enable_rules(self):
        with self.assertRaisesRegex(ValueError, "active lockin"):
            self.compile(plan=CombinationPlan((self.temperature,)))


if __name__ == "__main__":
    unittest.main()
