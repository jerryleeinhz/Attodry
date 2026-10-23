"""Hardware-free, frozen planar focus calibration in absolute micrometres."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math


class PositionerError(ValueError):
    """Invalid position plan or unverified positioner state."""


def finite_number(value, name, *, positive=False, nonnegative=False):
    if type(value) not in (int, float):
        raise PositionerError(name + " must be a finite number, not boolean")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise PositionerError(name + " must be a finite number") from exc
    if not math.isfinite(result):
        raise PositionerError(name + " must be a finite number")
    if positive and result <= 0:
        raise PositionerError(name + " must be positive")
    if nonnegative and result < 0:
        raise PositionerError(name + " must be nonnegative")
    return result


def _keys(value, name, required, optional=()):
    if not isinstance(value, dict):
        raise PositionerError(name + " must be a table")
    missing = set(required) - set(value)
    unknown = set(value) - set(required) - set(optional)
    if missing or unknown:
        raise PositionerError(f"{name}: missing {sorted(missing)}, unknown {sorted(unknown)}")


def _text(value, name):
    if type(value) is not str or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PositionerError(name + " must be a nonempty single-line string")
    return value


@dataclass(frozen=True)
class FocusPoint:
    x_um: float
    y_um: float
    z_um: float


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _hull(points):
    ordered = sorted(points)
    lower, upper = [], []
    for point in ordered:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(ordered):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


@dataclass(frozen=True)
class FocusPlane:
    points: tuple[FocusPoint, ...]
    coordinate_frame: str
    z_offset_um: float
    max_residual_um: float
    rms_residual_um: float
    x0_um: float
    y0_um: float
    z0_um: float
    slope_x: float
    slope_y: float
    residuals_um: tuple[float, ...]
    condition_number: float
    hull_xy_um: tuple[tuple[float, float], ...]
    calibration_sha256: str
    calibration_temperature_k: float | None = None
    temperature_tolerance_k: float | None = None
    calibration_conditions: str = "unspecified; no environmental compensation certified"

    def validate(self):
        """Also reject hand-built/replaced dataclasses with unverified fit values."""
        table = {"model": "plane", "coordinate_frame": self.coordinate_frame,
                 "points": [{"x_um": p.x_um, "y_um": p.y_um, "z_um": p.z_um} for p in self.points],
                 "max_residual_um": self.max_residual_um, "rms_residual_um": self.rms_residual_um,
                 "z_offset_um": self.z_offset_um, "calibration_conditions": self.calibration_conditions}
        if self.calibration_temperature_k is not None or self.temperature_tolerance_k is not None:
            table.update(calibration_temperature_k=self.calibration_temperature_k,
                         temperature_tolerance_k=self.temperature_tolerance_k)
        if self != fit_focus_plane(table):
            raise PositionerError("Focus plane no longer matches its frozen calibration inputs")

    def validate_xy(self, x_um, y_um):
        x = finite_number(x_um, "focus X")
        y = finite_number(y_um, "focus Y")
        # Scale-normalized half-plane tests; no rectangular substitute for hull.
        scale = max(max(abs(px - self.x0_um), abs(py - self.y0_um))
                    for px, py in self.hull_xy_um)
        point = ((x - self.x0_um) / scale, (y - self.y0_um) / scale)
        hull = tuple(((px - self.x0_um) / scale, (py - self.y0_um) / scale)
                     for px, py in self.hull_xy_um)
        for a, b in zip(hull, hull[1:] + hull[:1]):
            if _cross(a, b, point) < -1e-12:
                raise PositionerError("XY target lies outside focus calibration convex hull")
        return x, y

    def evaluate(self, x_um, y_um):
        x, y = self.validate_xy(x_um, y_um)
        result = self.z0_um + self.slope_x * (x - self.x0_um) + self.slope_y * (y - self.y0_um) + self.z_offset_um
        return finite_number(result, "computed focus Z")

    def focus_error(self, x_um, y_um, z_um):
        return finite_number(z_um, "actual Z") - self.evaluate(x_um, y_um)

    def validate_temperature(self, temperature_k):
        if self.calibration_temperature_k is None:
            return
        value = finite_number(temperature_k, "focus calibration actual temperature", positive=True)
        if abs(value - self.calibration_temperature_k) > self.temperature_tolerance_k:
            raise PositionerError("Actual temperature is outside focus calibration applicability")

    def snapshot(self):
        return {
            "model": "plane", "coordinate_frame": self.coordinate_frame,
            "points": [{"x_um": p.x_um, "y_um": p.y_um, "z_um": p.z_um} for p in self.points],
            "z_offset_um": self.z_offset_um, "max_residual_um": self.max_residual_um,
            "rms_residual_um": self.rms_residual_um,
            "center_um": {"x": self.x0_um, "y": self.y0_um, "z": self.z0_um},
            "slope_x": self.slope_x, "slope_y": self.slope_y,
            "residuals_um": list(self.residuals_um), "rms_actual_residual_um": math.sqrt(math.fsum(v * v for v in self.residuals_um) / len(self.points)),
            "max_actual_residual_um": max(abs(v) for v in self.residuals_um),
            "normalized_design_condition_number": self.condition_number, "rank": 3,
            "hull_xy_um": [list(point) for point in self.hull_xy_um],
            "extrapolation_policy": "reject", "calibration_sha256": self.calibration_sha256,
            "calibration_temperature_k": self.calibration_temperature_k,
            "temperature_tolerance_k": self.temperature_tolerance_k,
            "calibration_conditions": self.calibration_conditions,
            "redundant_points": len(self.points) > 3,
            "accuracy_note": "Fit residuals do not certify optical focus accuracy",
        }


def fit_focus_plane(table, *, coordinate_frame=None):
    """Fit all supplied points; reject degeneracy, residual failures and extrapolation.

    The normalized design matrix must have condition <= 1e6. XY is centered and
    both columns share their largest absolute spread as scale, making this test
    independent of coordinate offset/unit while retaining narrow-slice geometry.
    """
    try:
        return _fit_focus_plane(table, coordinate_frame=coordinate_frame)
    except (OverflowError, ZeroDivisionError) as exc:
        raise PositionerError("Focus calibration overflows the supported numerical range") from exc


def _fit_focus_plane(table, *, coordinate_frame=None):
    _keys(table, "positioner.focus_plane", {"model", "coordinate_frame", "points", "max_residual_um", "rms_residual_um"},
          {"z_offset_um", "extrapolation_policy", "calibration_temperature_k", "temperature_tolerance_k", "calibration_conditions"})
    if table["model"] != "plane" or table.get("extrapolation_policy", "reject") != "reject":
        raise PositionerError("focus_plane supports model=plane and extrapolation_policy=reject only")
    frame = _text(table["coordinate_frame"], "focus_plane.coordinate_frame")
    if coordinate_frame is not None and frame != coordinate_frame:
        raise PositionerError("Focus calibration and positioner coordinate_frame must match")
    source = table["points"]
    if not isinstance(source, list) or not 3 <= len(source) <= 100000:
        raise PositionerError("focus_plane.points requires 3..100000 absolute XYZ points")
    points = []
    for index, value in enumerate(source):
        _keys(value, f"focus_plane.points[{index}]", {"x_um", "y_um", "z_um"})
        points.append(FocusPoint(*(finite_number(value[key], f"focus point {index}.{key}") for key in ("x_um", "y_um", "z_um"))))
    if len({(p.x_um, p.y_um) for p in points}) != len(points):
        raise PositionerError("Duplicate focus calibration XY positions are ambiguous")
    n = len(points)
    # Mean around a local anchor avoids overflow summing large absolute offsets.
    x0 = points[0].x_um + math.fsum((p.x_um - points[0].x_um) / n for p in points)
    y0 = points[0].y_um + math.fsum((p.y_um - points[0].y_um) / n for p in points)
    z0 = points[0].z_um + math.fsum((p.z_um - points[0].z_um) / n for p in points)
    scale = max(max(abs(p.x_um - x0), abs(p.y_um - y0)) for p in points)
    if not math.isfinite(scale) or scale <= 0:
        raise PositionerError("Degenerate focus calibration spread")
    u = [(p.x_um - x0) / scale for p in points]
    v = [(p.y_um - y0) / scale for p in points]
    w = [p.z_um - z0 for p in points]
    uu = math.fsum(a * a for a in u)
    vv = math.fsum(a * a for a in v)
    uv = math.fsum(a * b for a, b in zip(u, v))
    det = uu * vv - uv * uv
    largest = (uu + vv + math.hypot(uu - vv, 2 * uv)) / 2
    smallest = det / largest if largest > 0 else 0
    condition = math.sqrt(max(n, largest) / min(n, smallest)) if smallest > 0 else math.inf
    if not math.isfinite(condition) or condition > 1e6:
        raise PositionerError("Focus calibration is collinear or ill-conditioned (normalized condition > 1e6)")
    uw = math.fsum(a * b for a, b in zip(u, w))
    vw = math.fsum(a * b for a, b in zip(v, w))
    slope_x = (uw * vv - vw * uv) / det / scale
    slope_y = (vw * uu - uw * uv) / det / scale
    residuals = tuple(p.z_um - (z0 + slope_x * (p.x_um - x0) + slope_y * (p.y_um - y0)) for p in points)
    maximum = finite_number(table["max_residual_um"], "focus max_residual_um", positive=True)
    rms = finite_number(table["rms_residual_um"], "focus rms_residual_um", positive=True)
    if not all(math.isfinite(value) for value in (slope_x, slope_y, *residuals)):
        raise PositionerError("Focus fit overflowed finite coordinates")
    actual_rms = math.sqrt(math.fsum(value * value for value in residuals) / n)
    if max(abs(value) for value in residuals) > maximum or actual_rms > rms:
        raise PositionerError("Focus plane fit exceeds configured residual threshold")
    temperature = tolerance = None
    if ("calibration_temperature_k" in table) != ("temperature_tolerance_k" in table):
        raise PositionerError("Focus calibration temperature and tolerance must be supplied together")
    if "calibration_temperature_k" in table:
        temperature = finite_number(table["calibration_temperature_k"], "calibration_temperature_k", positive=True)
        tolerance = finite_number(table["temperature_tolerance_k"], "temperature_tolerance_k", positive=True)
    offset = finite_number(table.get("z_offset_um", 0.0), "focus z_offset_um")
    conditions = _text(table.get("calibration_conditions", "unspecified; no environmental compensation certified"), "calibration_conditions")
    payload = {"model": "plane", "coordinate_frame": frame, "points": [[p.x_um, p.y_um, p.z_um] for p in points],
               "z_offset_um": offset, "max_residual_um": maximum, "rms_residual_um": rms,
               "extrapolation_policy": "reject", "calibration_temperature_k": temperature,
               "temperature_tolerance_k": tolerance, "calibration_conditions": conditions,
               "algorithm": "centered-shared-scale-ls-v1", "condition_limit": 1e6}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return FocusPlane(tuple(points), frame, offset, maximum, rms, x0, y0, z0, slope_x, slope_y,
                      residuals, condition, _hull([(p.x_um, p.y_um) for p in points]), digest,
                      temperature, tolerance, conditions)
