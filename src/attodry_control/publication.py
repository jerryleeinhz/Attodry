from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from statistics import median
import tomllib
from typing import Callable, Iterable, Sequence

from .analysis import AnalysisRow, GateLeakageRow
from .models import LockinRole


class PublicationDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GateCalibration:
    top_capacitance_f_per_m2: float
    bottom_capacitance_f_per_m2: float
    top_offset_v: float = 0.0
    bottom_offset_v: float = 0.0
    density_offset_per_m2: float = 0.0
    displacement_offset_c_per_m2: float = 0.0

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("Gate calibration values must be finite.")
        if self.top_capacitance_f_per_m2 <= 0:
            raise ValueError("top_capacitance_f_per_m2 must be positive.")
        if self.bottom_capacitance_f_per_m2 <= 0:
            raise ValueError("bottom_capacitance_f_per_m2 must be positive.")

    def convert(self, top_v: float, bottom_v: float) -> tuple[float, float]:
        elementary_charge_c = 1.602176634e-19
        top_charge = self.top_capacitance_f_per_m2 * (top_v - self.top_offset_v)
        bottom_charge = self.bottom_capacitance_f_per_m2 * (
            bottom_v - self.bottom_offset_v
        )
        density = (
            (top_charge + bottom_charge) / elementary_charge_c
            + self.density_offset_per_m2
        )
        displacement = (
            (bottom_charge - top_charge) / 2.0
            + self.displacement_offset_c_per_m2
        )
        return density, displacement


_CALIBRATION_KEYS = {
    "top_capacitance_f_per_m2",
    "bottom_capacitance_f_per_m2",
    "top_offset_v",
    "bottom_offset_v",
    "density_offset_per_m2",
    "displacement_offset_c_per_m2",
}


def load_gate_calibration(path: str | Path) -> GateCalibration:
    source = Path(path)
    try:
        with source.open("rb") as file:
            root = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PublicationDataError(
            f"Cannot read gate calibration {source}: {exc}"
        ) from exc
    section = root.get("gate_calibration")
    if not isinstance(section, dict):
        raise PublicationDataError(
            "Gate calibration requires a [gate_calibration] table."
        )
    required = {"top_capacitance_f_per_m2", "bottom_capacitance_f_per_m2"}
    missing = required - set(section)
    unknown = set(section) - _CALIBRATION_KEYS
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise PublicationDataError("Invalid gate calibration: " + "; ".join(details))
    values: dict[str, float] = {}
    for key in _CALIBRATION_KEYS:
        raw = section.get(key, 0.0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PublicationDataError(f"gate_calibration.{key} must be numeric.")
        value = float(raw)
        if not math.isfinite(value):
            raise PublicationDataError(f"gate_calibration.{key} must be finite.")
        values[key] = value
    try:
        return GateCalibration(**values)
    except ValueError as exc:
        raise PublicationDataError(str(exc)) from exc


def generate_publication_plots(
    rows: Sequence[AnalysisRow],
    leakage_rows: Sequence[GateLeakageRow],
    output_dir: str | Path,
    *,
    total_series_resistance_ohm: float | None = None,
    gate_calibration: GateCalibration | None = None,
    formats: Sequence[str] = ("png", "pdf"),
) -> dict[str, object]:
    """Generate only figures supported by measured, accepted rows and a manifest."""

    if total_series_resistance_ohm is not None and (
        not math.isfinite(total_series_resistance_ohm)
        or total_series_resistance_ohm <= 0
    ):
        raise PublicationDataError(
            "total_series_resistance_ohm must be finite and positive."
        )
    normalized_formats = tuple(dict.fromkeys(formats))
    if not normalized_formats or any(item not in {"png", "pdf"} for item in normalized_formats):
        raise PublicationDataError("formats must contain only 'png' and/or 'pdf'.")
    accepted_rows = tuple(row for row in rows if row.accepted)
    accepted_leakage = tuple(row for row in leakage_rows if row.accepted)
    if not accepted_rows:
        raise PublicationDataError("No accepted transport rows are available.")
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Publication plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    suite = _PublicationSuite(
        rows=accepted_rows,
        leakage_rows=accepted_leakage,
        output_dir=output,
        formats=normalized_formats,
        total_series_resistance_ohm=total_series_resistance_ohm,
        gate_calibration=gate_calibration,
        plt=plt,
    )
    suite.run()
    records_csv = _write_analysis_records(
        accepted_rows,
        output / "analysis_records.csv",
        total_series_resistance_ohm,
    )
    fit_csv = _write_fit_summary(suite.fits, output / "fit_summary.csv")
    manifest: dict[str, object] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "accepted_transport_rows": len(accepted_rows),
        "excluded_nonaccepted_transport_rows": len(rows) - len(accepted_rows),
        "accepted_gate_samples": len(accepted_leakage),
        "excluded_nonaccepted_gate_samples": len(leakage_rows) - len(accepted_leakage),
        "total_series_resistance_ohm": total_series_resistance_ohm,
        "gate_calibration": (
            None if gate_calibration is None else asdict(gate_calibration)
        ),
        "analysis_records_csv": str(records_csv),
        "fit_summary_csv": str(fit_csv),
        "figures": suite.figures,
        "limitations": [
            "Only accepted attempts are used for publication figures.",
            "Signed resistance uses lock-in X divided by an explicitly supplied RMS current.",
            "The excitation current is an estimate from the complete series-path resistance, not an independent current measurement.",
            "Vxy alone is not a Hall coefficient; geometry and antisymmetrization are not inferred.",
            "No Nernst, scattering-rate, or microscopic-mechanism quantity is inferred from transport voltage alone.",
        ],
    }
    manifest_path = output / "analysis_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    manifest["manifest"] = str(manifest_path)
    return manifest


class _PublicationSuite:
    def __init__(
        self,
        *,
        rows: tuple[AnalysisRow, ...],
        leakage_rows: tuple[GateLeakageRow, ...],
        output_dir: Path,
        formats: tuple[str, ...],
        total_series_resistance_ohm: float | None,
        gate_calibration: GateCalibration | None,
        plt: object,
    ) -> None:
        self.rows = rows
        self.leakage_rows = leakage_rows
        self.output_dir = output_dir
        self.formats = formats
        self.series_resistance = total_series_resistance_ohm
        self.gate_calibration = gate_calibration
        self.plt = plt
        self.figures: list[dict[str, object]] = []
        self.fits: list[dict[str, object]] = []

    def run(self) -> None:
        self._sweep_figure(
            "current_response",
            "Excitation current dependence",
            "Current (A RMS, estimated)",
            "excitation",
            self._current,
            unavailable=(
                None
                if self.series_resistance is not None
                else "requires explicit total_series_resistance_ohm"
            ),
        )
        self._harmonic_scaling()
        self._sweep_figure(
            "frequency_response",
            "Frequency response",
            "Frequency (Hz)",
            "frequency",
            lambda row: row.frequency_hz,
        )
        self._sweep_figure(
            "temperature_dependence",
            "Temperature dependence",
            "Temperature (K)",
            "temperature",
            lambda row: row.temperature_k,
        )
        self._field_dependence()
        self._sweep_figure(
            "angle_dependence",
            "Vector-field angle dependence",
            "Angle from +Z (deg)",
            "angle",
            lambda row: row.angle_deg_from_z,
            minimum_unique=3,
        )
        self._magnetochiral_gamma()
        self._temperature_field_map()
        self._gate_resistance_map()
        self._gate_leakage()
        self._n_d_map()
        for key, reason in (
            ("harmonic_ratio_db", "no directly measured harmonic-ratio dB field"),
            ("nernst_temperature_field_map", "requires thermal-gradient and Nernst calibration"),
            ("hall_coefficient", "requires sample geometry and an explicit antisymmetrization protocol"),
            ("scattering_rate", "cannot be inferred from the recorded transport voltages"),
        ):
            self._skip(key, reason)

    def _sweep_figure(
        self,
        key: str,
        title: str,
        x_label: str,
        varying: str,
        x_value: Callable[[AnalysisRow], float | None],
        *,
        unavailable: str | None = None,
        minimum_unique: int = 2,
    ) -> None:
        if unavailable:
            self._skip(key, unavailable)
            return
        groups = _group_sweeps(self.rows, varying)
        series: list[tuple[str, tuple[float, ...], tuple[float, ...]]] = []
        for group_key, grouped in groups.items():
            x, y = _median_series(grouped, x_value, lambda row: row.x_v)
            if len(set(x)) >= minimum_unique:
                series.append((_short_label(group_key), x, y))
        if not series:
            self._skip(key, f"requires at least {minimum_unique} distinct {varying} values under fixed conditions")
            return
        with self.plt.rc_context(_paper_style()):
            figure, axis = self.plt.subplots(figsize=(6.4, 4.2), constrained_layout=True)
            for label, x, y in series[:12]:
                axis.plot(x, y, marker="o", markersize=3.5, linewidth=1.2, label=label)
            axis.set_xlabel(x_label)
            axis.set_ylabel("Signed lock-in X (V)")
            axis.set_title(title)
            axis.grid(True, alpha=0.2)
            if len(series) > 1:
                axis.legend(fontsize=7, frameon=False)
            self._save(key, figure)

    def _field_dependence(self) -> None:
        bx_range = max(row.bx_t for row in self.rows) - min(row.bx_t for row in self.rows)
        bz_range = max(row.bz_t for row in self.rows) - min(row.bz_t for row in self.rows)
        if bx_range == 0 and bz_range == 0:
            self._skip("field_dependence", "requires at least two vector-field points")
            return
        if bx_range and not bz_range:
            value = lambda row: row.bx_t
            label = "Bx (T)"
        elif bz_range and not bx_range:
            value = lambda row: row.bz_t
            label = "Bz (T)"
        else:
            value = lambda row: row.field_magnitude_t
            label = "|B| (T); see manifest for Bx/Bz metadata"
        self._sweep_figure(
            "field_dependence",
            "Vector-field dependence",
            label,
            "field",
            value,
        )

    def _harmonic_scaling(self) -> None:
        if self.series_resistance is None:
            self._skip("harmonic_scaling", "requires explicit total_series_resistance_ohm")
            return
        groups = _group_sweeps(
            tuple(row for row in self.rows if row.harmonic in (2, 3)),
            "excitation",
        )
        series: list[tuple[str, tuple[float, ...], tuple[float, ...], dict[str, float]]] = []
        for group_key, grouped in groups.items():
            harmonic = grouped[0].harmonic
            x, y = _median_series(
                grouped,
                lambda row: abs(self._current(row) or 0.0) ** harmonic,
                lambda row: abs(row.x_v),
            )
            if len(set(x)) < 2:
                continue
            fit = _linear_fit(x, y)
            fit_record = {
                "figure": "harmonic_scaling",
                "series": _short_label(group_key),
                "harmonic": harmonic,
                **fit,
            }
            self.fits.append(fit_record)
            series.append((_short_label(group_key), x, y, fit))
        if not series:
            self._skip("harmonic_scaling", "requires h2/h3 data at two or more excitation currents")
            return
        with self.plt.rc_context(_paper_style()):
            figure, axes = self.plt.subplots(1, 2, figsize=(9.0, 3.8), constrained_layout=True)
            for label, x, y, fit in series:
                axis = axes[0] if "h2" in label else axes[1]
                axis.scatter(x, y, s=18, label=label)
                line_x = (min(x), max(x))
                line_y = tuple(fit["slope"] * value + fit["intercept"] for value in line_x)
                axis.plot(line_x, line_y, linewidth=1.0)
                axis.set_xlabel("|I|² (A²)" if axis is axes[0] else "|I|³ (A³)")
                axis.set_ylabel("|X| (V)")
                axis.grid(True, alpha=0.2)
                axis.legend(fontsize=7, frameon=False)
            axes[0].set_title("Second harmonic scaling")
            axes[1].set_title("Third harmonic scaling")
            self._save("harmonic_scaling", figure)

    def _magnetochiral_gamma(self) -> None:
        if self.series_resistance is None:
            self._skip("magnetochiral_gamma", "requires explicit total_series_resistance_ohm")
            return
        by_key = {(row.condition_id, row.role, row.harmonic): row for row in self.rows}
        points: list[tuple[float, float]] = []
        for row in self.rows:
            if row.harmonic != 1 or row.field_magnitude_t <= 1e-12:
                continue
            second = by_key.get((row.condition_id, row.role, 2))
            current = self._current(row)
            if second is None or current in (None, 0.0) or row.x_v == 0:
                continue
            gamma = 2.0 * second.x_v / (
                row.x_v * row.field_magnitude_t * abs(current)
            )
            points.append((row.field_magnitude_t, gamma))
        if len({x for x, _ in points}) < 2:
            self._skip("magnetochiral_gamma", "requires paired h1/h2 data at two or more nonzero fields")
            return
        points.sort()
        with self.plt.rc_context(_paper_style()):
            figure, axis = self.plt.subplots(figsize=(6.0, 4.0), constrained_layout=True)
            axis.plot([x for x, _ in points], [y for _, y in points], marker="o")
            axis.set_xlabel("|B| (T)")
            axis.set_ylabel("γ = 2V₂ω/(V₁ω|B||I|)")
            axis.set_title("Magnetochiral coefficient proxy")
            axis.grid(True, alpha=0.2)
            self._save("magnetochiral_gamma", figure)

    def _temperature_field_map(self) -> None:
        selected = [
            row
            for row in self.rows
            if row.role is LockinRole.XX
            and row.harmonic == 2
            and row.field_magnitude_t > 1e-12
        ]
        complete: list[tuple[tuple[tuple[str, object], ...], object]] = []
        for group_key, grouped in _group_fixed(
            selected, {"temperature", "bx", "bz", "angle"}
        ).items():
            grid = _rectangular_grid(
                grouped,
                lambda row: row.field_magnitude_t,
                lambda row: row.temperature_k,
                lambda row: row.x_v / row.field_magnitude_t,
            )
            if grid is not None:
                complete.append((group_key, grid))
        if not complete:
            self._skip("temperature_field_v2_over_b_map", "requires a complete accepted 2D temperature-|B| grid with nonzero field")
            return
        complete.sort(key=lambda item: (-len(item[1][0]) * len(item[1][1]), item[0]))
        selected_key, grid = complete[0]
        x, y, values = grid
        with self.plt.rc_context(_paper_style()):
            figure, axis = self.plt.subplots(figsize=(6.0, 4.5), constrained_layout=True)
            image = axis.pcolormesh(x, y, values, shading="nearest")
            axis.set_xlabel("|B| (T)")
            axis.set_ylabel("Temperature (K)")
            axis.set_title("V₂ω / |B|")
            figure.colorbar(image, ax=axis, label="V/T")
            self._save(
                "temperature_field_v2_over_b_map",
                figure,
                metadata={
                    "complete_groups": len(complete),
                    "selected_fixed_conditions": dict(selected_key),
                },
            )

    def _gate_resistance_map(self) -> None:
        if self.series_resistance is None:
            self._skip("gate_resistance_map", "requires explicit total_series_resistance_ohm")
            return
        selected = [row for row in self.rows if row.role is LockinRole.XX and row.harmonic == 1]
        complete: list[tuple[tuple[tuple[str, object], ...], object]] = []
        for group_key, grouped in _group_fixed(
            selected, {"top_gate", "bottom_gate"}
        ).items():
            grid = _rectangular_grid(
                grouped,
                lambda row: row.gate_bottom_v,
                lambda row: row.gate_top_v,
                lambda row: row.x_v / (row.excitation_v / self.series_resistance),
            )
            if grid is not None:
                complete.append((group_key, grid))
        if not complete:
            self._skip("gate_resistance_map", "requires a complete accepted Vtop-Vbottom grid")
            return
        complete.sort(key=lambda item: (-len(item[1][0]) * len(item[1][1]), item[0]))
        selected_key, grid = complete[0]
        x, y, values = grid
        with self.plt.rc_context(_paper_style()):
            figure, axis = self.plt.subplots(figsize=(6.0, 4.8), constrained_layout=True)
            image = axis.pcolormesh(x, y, values, shading="nearest")
            axis.set_xlabel("Bottom gate (V)")
            axis.set_ylabel("Top gate (V)")
            axis.set_title("Signed Rxx")
            figure.colorbar(image, ax=axis, label="Ω")
            self._save(
                "gate_resistance_map",
                figure,
                metadata={
                    "complete_groups": len(complete),
                    "selected_fixed_conditions": dict(selected_key),
                },
            )

    def _gate_leakage(self) -> None:
        selected = [
            row
            for row in self.leakage_rows
            if row.top_leakage_a is not None and row.bottom_leakage_a is not None
        ]
        by_scan: dict[str, list[GateLeakageRow]] = defaultdict(list)
        for row in selected:
            by_scan[_safe_scan_key(row.scan_id, row.condition_id)].append(row)
        eligible = [item for item in by_scan.items() if len(item[1]) >= 2]
        if not eligible:
            self._skip("gate_leakage", "requires at least two accepted samples with both gate-current readbacks")
            return
        eligible.sort(key=lambda item: (-len(item[1]), item[0]))
        selected_scan, selected = eligible[0]
        values = [max(abs(row.top_leakage_a or 0.0), abs(row.bottom_leakage_a or 0.0)) for row in selected]
        with self.plt.rc_context(_paper_style()):
            figure, axis = self.plt.subplots(figsize=(6.0, 4.8), constrained_layout=True)
            image = axis.scatter(
                [row.gate_bottom_v for row in selected],
                [row.gate_top_v for row in selected],
                c=values,
                s=55,
            )
            axis.set_xlabel("Bottom gate (V)")
            axis.set_ylabel("Top gate (V)")
            axis.set_title("Maximum absolute gate leakage")
            figure.colorbar(image, ax=axis, label="A")
            self._save(
                "gate_leakage",
                figure,
                metadata={
                    "eligible_scan_ids": len(eligible),
                    "selected_scan_id": selected_scan,
                },
            )

    def _n_d_map(self) -> None:
        if self.gate_calibration is None:
            self._skip("n_d_resistance_map", "requires an explicit gate calibration")
            return
        if self.series_resistance is None:
            self._skip("n_d_resistance_map", "requires explicit total_series_resistance_ohm")
            return
        selected = [row for row in self.rows if row.role is LockinRole.XX and row.harmonic == 1]
        point_groups: list[
            tuple[tuple[tuple[str, object], ...], list[tuple[float, float, float]]]
        ] = []
        for group_key, grouped in _group_fixed(
            selected, {"top_gate", "bottom_gate"}
        ).items():
            points: list[tuple[float, float, float]] = []
            for row in grouped:
                current = self._current(row)
                if current in (None, 0.0):
                    continue
                density, displacement = self.gate_calibration.convert(
                    row.gate_top_v, row.gate_bottom_v
                )
                points.append((density, displacement, row.x_v / current))
            if len(points) >= 3:
                point_groups.append((group_key, points))
        if not point_groups:
            self._skip("n_d_resistance_map", "requires at least three accepted calibrated gate points")
            return
        point_groups.sort(key=lambda item: (-len(item[1]), item[0]))
        selected_key, points = point_groups[0]
        with self.plt.rc_context(_paper_style()):
            figure, axis = self.plt.subplots(figsize=(6.0, 4.8), constrained_layout=True)
            image = axis.scatter(
                [point[0] for point in points],
                [point[1] for point in points],
                c=[point[2] for point in points],
                s=60,
            )
            axis.set_xlabel("Carrier density n (m⁻²)")
            axis.set_ylabel("Displacement D (C m⁻²)")
            axis.set_title("Signed Rxx in n-D coordinates")
            figure.colorbar(image, ax=axis, label="Ω")
            self._save(
                "n_d_resistance_map",
                figure,
                metadata={
                    "eligible_groups": len(point_groups),
                    "selected_fixed_conditions": dict(selected_key),
                },
            )

    def _current(self, row: AnalysisRow) -> float | None:
        if self.series_resistance is None:
            return None
        return row.excitation_v / self.series_resistance

    def _save(
        self,
        key: str,
        figure: object,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        outputs: list[str] = []
        for extension in self.formats:
            path = self.output_dir / f"{key}.{extension}"
            figure.savefig(path, dpi=220)
            outputs.append(str(path))
        self.plt.close(figure)
        entry: dict[str, object] = {
            "key": key,
            "status": "generated",
            "outputs": outputs,
        }
        if metadata:
            entry.update(metadata)
        self.figures.append(entry)

    def _skip(self, key: str, reason: str) -> None:
        self.figures.append({"key": key, "status": "skipped", "reason": reason})


def _paper_style() -> dict[str, object]:
    return {
        "font.size": 9,
        "axes.labelsize": 10,
        "axes.titlesize": 10,
        "axes.linewidth": 0.8,
        "xtick.direction": "in",
        "ytick.direction": "in",
        "xtick.top": True,
        "ytick.right": True,
        "savefig.bbox": "tight",
    }


def _condition_key(row: AnalysisRow, varying: str) -> tuple[tuple[str, object], ...]:
    values: dict[str, object] = {
        "scan_id": _safe_scan_key(row.scan_id, row.condition_id),
        "role": row.role.value,
        "harmonic": row.harmonic,
        "temperature": round(row.temperature_k, 6),
        "bx": round(row.bx_t, 9),
        "bz": round(row.bz_t, 9),
        "angle": round(row.angle_deg_from_z, 6),
        "excitation": round(row.excitation_v, 12),
        "frequency": round(row.frequency_hz, 6),
        "top_gate": round(row.gate_top_v, 9),
        "bottom_gate": round(row.gate_bottom_v, 9),
    }
    remove = {
        "field": {"bx", "bz", "angle"},
        "angle": {"bx", "bz", "angle"},
    }.get(varying, {varying})
    for key in remove:
        values.pop(key, None)
    return tuple(sorted(values.items()))


def _group_sweeps(
    rows: Iterable[AnalysisRow], varying: str
) -> dict[tuple[tuple[str, object], ...], list[AnalysisRow]]:
    groups: dict[tuple[tuple[str, object], ...], list[AnalysisRow]] = defaultdict(list)
    for row in rows:
        groups[_condition_key(row, varying)].append(row)
    return groups


def _group_fixed(
    rows: Iterable[AnalysisRow], excluded: set[str]
) -> dict[tuple[tuple[str, object], ...], list[AnalysisRow]]:
    groups: dict[tuple[tuple[str, object], ...], list[AnalysisRow]] = defaultdict(list)
    for row in rows:
        values: dict[str, object] = {
            "scan_id": _safe_scan_key(row.scan_id, row.condition_id),
            "role": row.role.value,
            "harmonic": row.harmonic,
            "temperature": round(row.temperature_k, 6),
            "bx": round(row.bx_t, 9),
            "bz": round(row.bz_t, 9),
            "angle": round(row.angle_deg_from_z, 6),
            "excitation": round(row.excitation_v, 12),
            "frequency": round(row.frequency_hz, 6),
            "top_gate": round(row.gate_top_v, 9),
            "bottom_gate": round(row.gate_bottom_v, 9),
        }
        for key in excluded:
            values.pop(key, None)
        groups[tuple(sorted(values.items()))].append(row)
    return groups


def _safe_scan_key(scan_id: str, condition_id: str) -> str:
    """Never infer a multi-point scan boundary for migrated legacy rows."""

    if scan_id == "legacy":
        return f"legacy-condition:{condition_id}"
    return scan_id


def _short_label(key: tuple[tuple[str, object], ...]) -> str:
    values = dict(key)
    return f"{str(values['role']).upper()} h{values['harmonic']}"


def _median_series(
    rows: Iterable[AnalysisRow],
    x_value: Callable[[AnalysisRow], float | None],
    y_value: Callable[[AnalysisRow], float | None],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    bins: dict[float, list[float]] = defaultdict(list)
    for row in rows:
        x = x_value(row)
        y = y_value(row)
        if x is None or y is None or not math.isfinite(x) or not math.isfinite(y):
            continue
        bins[float(x)].append(float(y))
    points = sorted((x, median(values)) for x, values in bins.items())
    return tuple(x for x, _ in points), tuple(y for _, y in points)


def _rectangular_grid(
    rows: Iterable[AnalysisRow],
    x_value: Callable[[AnalysisRow], float],
    y_value: Callable[[AnalysisRow], float],
    z_value: Callable[[AnalysisRow], float],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[tuple[float, ...], ...]] | None:
    points: dict[tuple[float, float], list[float]] = defaultdict(list)
    for row in rows:
        try:
            x, y, z = float(x_value(row)), float(y_value(row)), float(z_value(row))
        except (ValueError, ZeroDivisionError):
            continue
        if all(math.isfinite(value) for value in (x, y, z)):
            points[(x, y)].append(z)
    x_values = tuple(sorted({key[0] for key in points}))
    y_values = tuple(sorted({key[1] for key in points}))
    if len(x_values) < 2 or len(y_values) < 2:
        return None
    if len(points) != len(x_values) * len(y_values):
        return None
    values = tuple(
        tuple(median(points[(x, y)]) for x in x_values) for y in y_values
    )
    return x_values, y_values, values


def _linear_fit(x: Sequence[float], y: Sequence[float]) -> dict[str, float]:
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("Linear fit requires paired values at two or more points.")
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    denominator = sum((value - mean_x) ** 2 for value in x)
    if denominator == 0:
        raise ValueError("Linear fit x values must vary.")
    slope = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y)) / denominator
    intercept = mean_y - slope * mean_x
    residual = sum((b - (slope * a + intercept)) ** 2 for a, b in zip(x, y))
    total = sum((b - mean_y) ** 2 for b in y)
    r_squared = 1.0 if total == 0 and residual == 0 else (0.0 if total == 0 else 1.0 - residual / total)
    return {"slope": slope, "intercept": intercept, "r_squared": r_squared}


def _write_analysis_records(
    rows: Sequence[AnalysisRow],
    path: Path,
    total_series_resistance_ohm: float | None,
) -> Path:
    names = [field.name for field in AnalysisRow.__dataclass_fields__.values()]
    names.extend(("estimated_current_a_rms", "signed_resistance_ohm"))
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=names)
        writer.writeheader()
        for row in rows:
            record = asdict(row)
            record["role"] = row.role.value
            current = (
                None
                if total_series_resistance_ohm is None
                else row.excitation_v / total_series_resistance_ohm
            )
            record["estimated_current_a_rms"] = current
            record["signed_resistance_ohm"] = (
                None if current in (None, 0.0) else row.x_v / current
            )
            writer.writerow(record)
    return path


def _write_fit_summary(fits: Sequence[dict[str, object]], path: Path) -> Path:
    fieldnames = ("figure", "series", "harmonic", "slope", "intercept", "r_squared")
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(fits)
    return path
