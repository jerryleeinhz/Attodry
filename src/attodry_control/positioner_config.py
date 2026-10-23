"""Strict ANC350 XYZ scan configuration; this module never imports hardware."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
import re
from types import MappingProxyType
from collections.abc import Mapping

from .positioner_focus_plane import (FocusPlane, PositionerError, _keys, _text,
                                     finite_number, fit_focus_plane)
from .scan_grid import MAX_GRID_POINTS, expand_grid


ROLES = ("x", "y", "z")
ABSOLUTE_KEYS = tuple("positioner_" + role + "_um" for role in ROLES)
OFFSET_KEYS = tuple("positioner_" + role + "_offset_um" for role in ROLES)


@dataclass(frozen=True)
class PositionerAxisConfig:
    role: str
    index: int
    min_position_um: float
    max_position_um: float
    max_move_um: float
    position_tolerance_um: float
    expected_actuator_name: str
    expected_lut_name: str
    amplitude_v: float
    frequency_hz: float

    def validate(self):
        if self.role not in ROLES or type(self.index) is not int or self.index != ROLES.index(self.role):
            raise PositionerError("Confirmed XYZ map requires x=0, y=1, z=2")
        low = finite_number(self.min_position_um, self.role + ".min_position_um")
        high = finite_number(self.max_position_um, self.role + ".max_position_um")
        if low >= high or not math.isfinite(high - low):
            raise PositionerError(self.role + " requires finite min_position_um < max_position_um")
        for name in ("max_move_um", "position_tolerance_um", "amplitude_v", "frequency_hz"):
            finite_number(getattr(self, name), self.role + "." + name, positive=True)
        if self.position_tolerance_um >= high - low:
            raise PositionerError(self.role + " tolerance must be smaller than the configured axis span")
        _text(self.expected_actuator_name, self.role + ".expected_actuator_name")
        _text(self.expected_lut_name, self.role + ".expected_lut_name")

    @property
    def position_key(self):
        return "positioner_" + self.role + "_um"

    def validate_position(self, value):
        result = finite_number(value, self.position_key)
        if not self.min_position_um <= result <= self.max_position_um:
            raise PositionerError(self.role + " target/readback exceeds configured position limits")
        return result


def _absolute(values, name):
    if not isinstance(values, Mapping) or set(values) != set(ABSOLUTE_KEYS):
        raise PositionerError(name + " requires exactly absolute positioner_x_um/y_um/z_um")
    return {key: finite_number(values[key], name + "." + key) for key in ABSOLUTE_KEYS}


@dataclass(frozen=True)
class PositionerConfig:
    controller_serial_number: str
    expected_firmware_raw: int
    dll_path: str
    dll_sha256: str
    coordinate_frame: str
    coordinate_frame_confirmed: bool
    axes: tuple[PositionerAxisConfig, ...]
    mode: str
    z_mode: str
    points: tuple[Mapping[str, float], ...]
    move_order: tuple[str, ...]
    settle_s: float
    poll_interval_s: float
    move_timeout_s: float
    finish: str
    external_light_off_confirmed: bool
    focus_plane: FocusPlane | None = None
    focus_tolerance_um: float | None = None

    @property
    def active_keys(self):
        if self.mode == "absolute":
            return ABSOLUTE_KEYS
        return OFFSET_KEYS[:2] if self.z_mode == "focus_plane" else OFFSET_KEYS

    def validate(self):
        _text(self.controller_serial_number, "positioner.controller_serial_number")
        _text(self.dll_path, "positioner.dll_path")
        if type(self.dll_sha256) is not str or not re.fullmatch(r"[a-fA-F0-9]{64}", self.dll_sha256):
            raise PositionerError("positioner.dll_sha256 requires an explicit SHA256")
        if type(self.expected_firmware_raw) is not int or not 0 <= self.expected_firmware_raw <= 0xffffffff:
            raise PositionerError("positioner.expected_firmware_raw must be a uint32 integer")
        _text(self.coordinate_frame, "positioner.coordinate_frame")
        if self.coordinate_frame_confirmed is not True:
            raise PositionerError("positioner.coordinate_frame_confirmed must explicitly be true")
        if type(self.external_light_off_confirmed) is not bool:
            raise PositionerError("positioner.external_light_off_confirmed must be boolean")
        if self.mode not in ("absolute", "relative") or self.z_mode not in ("manual", "focus_plane"):
            raise PositionerError("positioner mode must be absolute/relative and z_mode manual/focus_plane")
        if self.finish not in ("hold", "return_start"):
            raise PositionerError("positioner.finish must be hold or return_start")
        if len(self.axes) != 3 or tuple(axis.role for axis in self.axes) != ROLES:
            raise PositionerError("positioner requires all three configured XYZ axes in order")
        for axis in self.axes:
            axis.validate()
        if len(self.move_order) != 3 or any(type(role) is not str for role in self.move_order) or set(self.move_order) != set(ROLES):
            raise PositionerError("positioner.move_order must explicitly order x, y, z once each")
        settle = finite_number(self.settle_s, "positioner.settle_s", positive=True)
        poll = finite_number(self.poll_interval_s, "positioner.poll_interval_s", positive=True)
        timeout = finite_number(self.move_timeout_s, "positioner.move_timeout_s", positive=True)
        if settle >= timeout or poll > settle:
            raise PositionerError("positioner requires poll_interval_s <= settle_s < move_timeout_s")
        if self.z_mode == "focus_plane":
            if not isinstance(self.focus_plane, FocusPlane) or self.focus_plane.coordinate_frame != self.coordinate_frame:
                raise PositionerError("focus_plane mode requires a fitted plane in the same coordinate frame")
            self.focus_plane.validate()
            finite_number(self.focus_tolerance_um, "positioner.focus_tolerance_um", positive=True)
            for point in self.focus_plane.points:
                for axis, value in zip(self.axes, (point.x_um, point.y_um, point.z_um)):
                    axis.validate_position(value)
        elif self.focus_plane is not None or self.focus_tolerance_um is not None:
            raise PositionerError("manual Z conflicts with focus_plane or focus_tolerance_um")
        if not 1 <= len(self.points) <= MAX_GRID_POINTS:
            raise PositionerError(f"positioner requires 1..{MAX_GRID_POINTS} scan points")
        for point in self.points:
            if not isinstance(point, Mapping) or set(point) != set(self.active_keys):
                raise PositionerError("positioner scan points do not match configured coordinate mode")
            for key, value in point.items():
                finite_number(value, key)
        if self.mode == "absolute":
            targets = self.resolve_all_points()
            self.validate_path(targets[0], targets[1:])

    def resolve_point(self, values, origin=None):
        if not isinstance(values, Mapping) or set(values) != set(self.active_keys):
            raise PositionerError("Positioner point has unexpected/missing coordinate keys")
        if self.mode == "relative":
            baseline = _absolute(origin, "Frozen relative origin")
            target = {ABSOLUTE_KEYS[index]: baseline[ABSOLUTE_KEYS[index]] + finite_number(values[key], key)
                      for index, key in enumerate(self.active_keys)}
        else:
            target = {key: finite_number(values[key], key) for key in self.active_keys}
        if self.z_mode == "focus_plane":
            z = self.focus_plane.evaluate(target[ABSOLUTE_KEYS[0]], target[ABSOLUTE_KEYS[1]])
            if self.mode == "absolute" and not math.isclose(target[ABSOLUTE_KEYS[2]], z, rel_tol=0, abs_tol=1e-9):
                raise PositionerError("Absolute focus Z differs from frozen focus-plane target")
            target[ABSOLUTE_KEYS[2]] = z
        for axis in self.axes:
            target[axis.position_key] = axis.validate_position(target[axis.position_key])
        return target

    def resolve_all_points(self, origin=None):
        return tuple(self.resolve_point(point, origin) for point in self.points)

    def validate_path(self, start, targets):
        """Return every sequential-axis waypoint; never split unsafe large moves.

        This checks configured position/step bounds and calibration coverage, not
        mechanical collision freedom. The operator must confirm the move order.
        """
        current = _absolute(start, "Path start")
        axes = {axis.role: axis for axis in self.axes}
        for axis in self.axes:
            axis.validate_position(current[axis.position_key])
        if self.focus_plane:
            self.focus_plane.validate_xy(current[ABSOLUTE_KEYS[0]], current[ABSOLUTE_KEYS[1]])
        steps = []
        for raw in targets:
            target = _absolute(raw, "Absolute path target")
            for axis in self.axes:
                axis.validate_position(target[axis.position_key])
            if self.focus_plane:
                self.focus_plane.validate_xy(target[ABSOLUTE_KEYS[0]], target[ABSOLUTE_KEYS[1]])
            for role in self.move_order:
                axis = axes[role]
                key = axis.position_key
                difference = abs(target[key] - current[key])
                if difference > axis.max_move_um:
                    raise PositionerError(role + " sequential move exceeds configured max_move_um")
                if difference:
                    current = dict(current)
                    current[key] = target[key]
                    if self.focus_plane:
                        self.focus_plane.validate_xy(current[ABSOLUTE_KEYS[0]], current[ABSOLUTE_KEYS[1]])
                    steps.append(dict(current))
        return tuple(steps)

    def snapshot(self):
        return {"controller": "ANC350 RES", "controller_serial_number": self.controller_serial_number,
                "expected_firmware_raw": self.expected_firmware_raw, "dll_path": self.dll_path,
                "dll_sha256": self.dll_sha256.lower(), "coordinate_frame": self.coordinate_frame,
                "coordinate_frame_confirmed": self.coordinate_frame_confirmed,
                "axes": {axis.role: asdict(axis) for axis in self.axes}, "mode": self.mode,
                "z_mode": self.z_mode, "points": [dict(point) for point in self.points],
                "move_order": list(self.move_order), "settle_s": self.settle_s,
                "poll_interval_s": self.poll_interval_s, "move_timeout_s": self.move_timeout_s,
                "finish": self.finish, "external_light_off_confirmed": self.external_light_off_confirmed,
                "focus_tolerance_um": self.focus_tolerance_um,
                "focus_plane": self.focus_plane.snapshot() if self.focus_plane else None,
                "units": "micrometres; SDK indices x=0,y=1,z=2",
                "path_safety": "Configured limits/order only; installation clearances require operator confirmation"}


def _expand_values(scan, role):
    try:
        return tuple(point.value for point in expand_grid(scan, role + "_values_um", role + "_ranges_um", "positioner.scan"))
    except (ValueError, TypeError) as exc:
        raise PositionerError(str(exc)) from exc


def _scan_points(scan, mode, z_mode, plane):
    _keys(scan, "positioner.scan", {"mode"}, {"points", "x_values_um", "x_ranges_um", "y_values_um", "y_ranges_um", "z_um", "order", "serpentine"})
    input_keys = ("x_um", "y_um") if z_mode == "focus_plane" else ("x_um", "y_um", "z_um")
    if "points" in scan:
        if set(scan) != {"mode", "points"}:
            raise PositionerError("positioner.scan explicit points conflict with grid fields")
        source = scan["points"]
        if not isinstance(source, list) or not 1 <= len(source) <= MAX_GRID_POINTS:
            raise PositionerError(f"positioner.scan.points requires 1..{MAX_GRID_POINTS} point tables")
        values = []
        for index, point in enumerate(source):
            _keys(point, f"positioner.scan.points[{index}]", input_keys)
            values.append(tuple(finite_number(point[key], "scan point " + key) for key in input_keys))
    else:
        x, y = _expand_values(scan, "x"), _expand_values(scan, "y")
        if len(x) * len(y) > MAX_GRID_POINTS:
            raise PositionerError(f"positioner XY grid exceeds {MAX_GRID_POINTS} points")
        order = scan.get("order", "xy")
        if order not in ("xy", "yx"):
            raise PositionerError("positioner.scan.order must be xy (X outer) or yx (Y outer)")
        serpent = scan.get("serpentine", False)
        if type(serpent) is not bool:
            raise PositionerError("positioner.scan.serpentine must be boolean")
        outer, inner = (x, y) if order == "xy" else (y, x)
        values = []
        if z_mode == "focus_plane" and "z_um" in scan:
            raise PositionerError("focus_plane mode forbids independent Z coordinates")
        z = finite_number(scan.get("z_um"), "positioner.scan.z_um") if z_mode == "manual" else None
        for row, first in enumerate(outer):
            for second in reversed(inner) if serpent and row % 2 else inner:
                pair = (first, second) if order == "xy" else (second, first)
                values.append(pair if z_mode == "focus_plane" else (*pair, z))
    points = []
    for value in values:
        if mode == "absolute":
            xyz = (*value, plane.evaluate(*value)) if z_mode == "focus_plane" else value
            point = dict(zip(ABSOLUTE_KEYS, xyz))
        else:
            point = dict(zip(OFFSET_KEYS, value))
        points.append(MappingProxyType(point))
    return tuple(points)


def load_positioner_config(document):
    """Read only nested [positioner] tables; absent/disabled returns None."""
    if not isinstance(document, dict):
        raise PositionerError("Positioner configuration document must be a table")
    table = document.get("positioner")
    if table is None:
        return None
    required = {"controller_serial_number", "expected_firmware_raw", "dll_path", "dll_sha256", "coordinate_frame",
                "coordinate_frame_confirmed", "axes", "scan", "z_mode", "move_order", "settle_s",
                "poll_interval_s", "move_timeout_s", "finish", "external_light_off_confirmed"}
    optional = {"enabled", "focus_plane", "focus_tolerance_um"}
    _keys(table, "positioner", set(), required | optional)
    enabled = table.get("enabled", False)
    if type(enabled) is not bool:
        raise PositionerError("positioner.enabled must be boolean")
    if not enabled:
        return None
    _keys(table, "positioner", required, optional)
    axes_table = table["axes"]
    _keys(axes_table, "positioner.axes", ROLES)
    axes = []
    axis_fields = {field.name for field in fields(PositionerAxisConfig)} - {"role"}
    for role in ROLES:
        _keys(axes_table[role], "positioner.axes." + role, axis_fields)
        axes.append(PositionerAxisConfig(role=role, **axes_table[role]))
    scan = table["scan"]
    if not isinstance(scan, dict) or scan.get("mode") not in ("absolute", "relative"):
        raise PositionerError("positioner.scan.mode must be absolute or relative")
    z_mode = table["z_mode"]
    if z_mode not in ("manual", "focus_plane"):
        raise PositionerError("positioner.z_mode must be manual or focus_plane")
    plane = fit_focus_plane(table["focus_plane"], coordinate_frame=table["coordinate_frame"]) if "focus_plane" in table else None
    if z_mode == "focus_plane" and plane is None:
        raise PositionerError("positioner.focus_plane is required in focus_plane mode")
    if type(table["move_order"]) is not list:
        raise PositionerError("positioner.move_order must be an explicit array of x/y/z")
    config = PositionerConfig(controller_serial_number=table["controller_serial_number"], expected_firmware_raw=table["expected_firmware_raw"],
          dll_path=table["dll_path"], dll_sha256=table["dll_sha256"], coordinate_frame=table["coordinate_frame"],
          coordinate_frame_confirmed=table["coordinate_frame_confirmed"], axes=tuple(axes), mode=scan["mode"],
          z_mode=z_mode, points=_scan_points(scan, scan["mode"], z_mode, plane), move_order=tuple(table["move_order"]),
          settle_s=table["settle_s"], poll_interval_s=table["poll_interval_s"], move_timeout_s=table["move_timeout_s"],
          finish=table["finish"], external_light_off_confirmed=table["external_light_off_confirmed"], focus_plane=plane,
          focus_tolerance_um=table.get("focus_tolerance_um"))
    config.validate()
    return config
