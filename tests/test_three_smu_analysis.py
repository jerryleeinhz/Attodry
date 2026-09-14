import csv
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.three_smu_analysis import (
    build_map,
    discover_three_smu_runs,
    load_three_smu_rows,
    resolve_run_dir,
)
from attodry_control.three_smu_config import SEMANTIC_ROLES
from attodry_control.three_smu_plot import load_three_smu_plot_samples


def make_run(
    root: Path,
    *,
    status: str = "completed",
    clean: bool = True,
    bias_values: tuple[float, ...] = (0.1,),
    active_roles: tuple[str, ...] = SEMANTIC_ROLES,
) -> Path:
    run_dir = root / "run"
    run_dir.mkdir()
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "status": status,
                "accepted": status == "completed",
                "active_roles": list(active_roles),
            }
        ),
        encoding="utf-8",
    )
    fields = [
        "point_index", "repeat_index", "segment", "elapsed_s",
        "sample_clean", "problems",
    ]
    for role in SEMANTIC_ROLES:
        fields.extend([
            f"{role}_coordinate", f"{role}_timestamp",
            f"{role}_source_setpoint", f"{role}_voltage_v",
            f"{role}_current_a", f"{role}_resistance_ohm",
            f"{role}_output_enabled", f"{role}_compliance_trip",
            f"{role}_status",
        ])
    with (run_dir / "data.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        index = 0
        for bias in bias_values:
            for top in (0.0, 1.0):
                for bottom in (0.0, 2.0):
                    row = {
                        "point_index": index,
                        "repeat_index": 0,
                        "segment": "serpentine",
                        "elapsed_s": index,
                        "sample_clean": clean,
                        "problems": "" if clean else "injected problem",
                    }
                    coordinates = {
                        "smu_bias": bias,
                        "gate_top": top,
                        "gate_bottom": bottom,
                    }
                    for role in SEMANTIC_ROLES:
                        if role not in active_roles:
                            continue
                        row.update({
                            f"{role}_coordinate": coordinates[role],
                            f"{role}_timestamp": f"2026-01-01T00:00:0{index}Z",
                            f"{role}_source_setpoint": coordinates[role],
                            f"{role}_voltage_v": coordinates[role],
                            f"{role}_current_a": (index + 1) * 1e-6,
                            f"{role}_resistance_ohm": 1000.0,
                            f"{role}_output_enabled": role != "gate_bottom",
                            f"{role}_compliance_trip": False,
                            f"{role}_status": "0,No error",
                        })
                    writer.writerow(row)
                    index += 1
    (run_dir / "raw.jsonl").write_text("", encoding="utf-8")
    return run_dir


class ThreeSmuAnalysisTests(unittest.TestCase):
    def test_default_loader_returns_only_completed_clean_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = make_run(Path(directory))
            rows = load_three_smu_rows(run_dir)
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(row.accepted and row.clean for row in rows))
            self.assertEqual(resolve_run_dir(run_dir / "data.csv"), run_dir)

    def test_rejected_run_requires_explicit_audit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = make_run(Path(directory), status="rejected")
            self.assertEqual(load_three_smu_rows(run_dir), ())
            rows = load_three_smu_rows(run_dir, include_rejected=True)
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(not row.accepted for row in rows))

    def test_problem_rows_require_separate_problem_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = make_run(Path(directory), clean=False)
            self.assertEqual(load_three_smu_rows(run_dir), ())
            rows = load_three_smu_rows(run_dir, include_problem=True)
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(not row.clean for row in rows))

    def test_segment_and_role_filters_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = make_run(Path(directory))
            rows = load_three_smu_rows(
                run_dir, segment="serpentine", role="gate_top"
            )
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row.role == "gate_top" for row in rows))
            self.assertEqual(
                load_three_smu_rows(run_dir, segment="missing"), ()
            )

    def test_schema_five_bottom_only_rows_skip_off_role_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = make_run(Path(directory), active_roles=("gate_bottom",))
            rows = load_three_smu_rows(run_dir)
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row.role == "gate_bottom" for row in rows))
            self.assertEqual(load_three_smu_rows(run_dir, role="smu_bias"), ())

    def test_plot_loader_reconstructs_one_wide_sample_per_formal_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = load_three_smu_plot_samples(make_run(Path(directory)))
        self.assertEqual(len(samples), 4)
        self.assertEqual(set(samples[0].readings), set(SEMANTIC_ROLES))
        self.assertEqual(samples[0].readings["smu_bias"].conductance_s, 0.001)

    def test_rectangular_two_gate_map_uses_bias_measurement_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = load_three_smu_rows(make_run(Path(directory)))
            map_data = build_map(
                rows,
                x_role="gate_top",
                y_role="gate_bottom",
                value_role="smu_bias",
                value_field="current_a",
            )
            self.assertEqual(map_data.x_values, (0.0, 1.0))
            self.assertEqual(map_data.y_values, (0.0, 2.0))
            self.assertEqual(len(map_data.values), 2)
            self.assertEqual(len(map_data.values[0]), 2)

    def test_two_gate_map_can_select_a_fixed_bias_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = load_three_smu_rows(
                make_run(Path(directory), bias_values=(0.1, 0.2))
            )
            with self.assertRaisesRegex(ValueError, "Duplicate map coordinate"):
                build_map(rows, x_role="gate_top", y_role="gate_bottom")
            map_data = build_map(
                rows,
                x_role="gate_top",
                y_role="gate_bottom",
                fixed_coordinates={"smu_bias": 0.2},
            )
            self.assertEqual(map_data.x_values, (0.0, 1.0))

    def test_discovery_defaults_to_completed_accepted_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = make_run(root)
            (root / "incomplete").mkdir()
            summaries = discover_three_smu_runs(root)
            self.assertEqual([item.run_dir for item in summaries], [run_dir])


if __name__ == "__main__":
    unittest.main()
