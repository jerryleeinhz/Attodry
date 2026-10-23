"""Strict, hardware-free configuration for two optical waveplate rotators."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
import re

from .nkt_config import NktError, keys, number, text
from .scan_grid import MAX_GRID_POINTS, expand_grid


class RotatorError(NktError):
    pass


ROLES = ("rotator_1", "rotator_2")


@dataclass(frozen=True)
class RotatorDeviceConfig:
    role: str
    serial_number: str
    label: str = ""
    controller: str = "KDC101"
    stage_model: str = "PRM1/MZ8"
    min_angle_deg: float = 0.0
    max_angle_deg: float = 360.0
    home_policy: str = "if_needed"
    position_tolerance_deg: float = 0.1
    settle_s: float = 0.5
    poll_interval_s: float = 0.1
    move_timeout_s: float = 60.0
    home_timeout_s: float = 120.0
    dll_directory: str = ""
    optical_zero_deg: float | None = None

    @property
    def angle_key(self):
        return self.role + "_angle_deg"

    def validate(self):
        if self.role not in ROLES:
            raise RotatorError("Unknown optical rotator role")
        prefix = self.role + "."
        if self.controller != "KDC101" or self.stage_model != "PRM1/MZ8":
            raise RotatorError(prefix + "only confirmed KDC101 + PRM1/MZ8 is supported")
        if type(self.serial_number) is not str or not re.fullmatch(r"27\d{6}", self.serial_number):
            raise RotatorError(prefix + "serial_number must be an explicit 8-digit KDC101 serial starting with 27")
        text(self.label, prefix + "label", empty=True)
        if any(ord(char) < 32 or ord(char) == 127 for char in self.label):
            raise RotatorError(prefix + "label must be a single line without control characters")
        low = number(self.min_angle_deg, prefix + "min_angle_deg", 0, 360)
        high = number(self.max_angle_deg, prefix + "max_angle_deg", 0, 360)
        if low >= high:
            raise RotatorError(prefix + "min_angle_deg must be less than max_angle_deg")
        if self.home_policy not in ("if_needed", "always", "require_homed"):
            raise RotatorError(prefix + "home_policy must be if_needed, always or require_homed")
        for name in ("position_tolerance_deg", "poll_interval_s", "move_timeout_s", "home_timeout_s"):
            value = number(getattr(self, name), prefix + name, 0, 1e9)
            if value <= 0:
                raise RotatorError(prefix + name + " must be positive")
        if self.position_tolerance_deg > 1:
            raise RotatorError(prefix + "position_tolerance_deg must be at most 1 degree")
        if self.poll_interval_s > min(self.move_timeout_s, self.home_timeout_s):
            raise RotatorError(prefix + "poll_interval_s must not exceed either motion timeout")
        number(self.settle_s, prefix + "settle_s", 0, 1e9)
        if self.settle_s >= min(self.move_timeout_s, self.home_timeout_s):
            raise RotatorError(prefix + "settle_s must be less than either motion timeout")
        text(self.dll_directory, prefix + "dll_directory", empty=True)
        if self.optical_zero_deg is not None:
            number(self.optical_zero_deg, prefix + "optical_zero_deg", -1e300, 1e300)

    def validate_angle(self, value):
        try:
            return number(value, self.angle_key, self.min_angle_deg, self.max_angle_deg)
        except NktError as exc:
            raise RotatorError(str(exc)) from exc


@dataclass(frozen=True)
class RotatorConfig:
    devices: tuple[RotatorDeviceConfig, ...]
    points: tuple[dict[str, float], ...]

    @property
    def active_keys(self):
        return tuple(device.angle_key for device in self.devices)

    def validate(self):
        if not self.devices or len(self.devices) > 2:
            raise RotatorError("rotator requires one or two enabled devices")
        roles, serials = set(), set()
        for device in self.devices:
            device.validate()
            if device.role in roles or device.serial_number in serials:
                raise RotatorError("Enabled rotator roles and serial numbers must be distinct")
            roles.add(device.role)
            serials.add(device.serial_number)
        if not 1 <= len(self.points) <= MAX_GRID_POINTS:
            raise RotatorError(f"rotator grid requires 1..{MAX_GRID_POINTS} points")
        for point in self.points:
            if not isinstance(point, dict) or set(point) != set(self.active_keys):
                raise RotatorError("Every rotator point must contain exactly the enabled angle keys")
            for device in self.devices:
                device.validate_angle(point[device.angle_key])

    def snapshot(self):
        self.validate()
        return {"devices": {device.role: asdict(device) for device in self.devices},
                "points": [dict(point) for point in self.points],
                "angle_coordinate": "mechanical degrees; optical zero is separate"}


def load_rotator_config(document):
    """Return enabled-only configuration, or None when both slots are absent/off.

    Disabled slots allow retained known hardware fields without parsing them;
    misspelled keys are always rejected and no disabled device is instantiated.
    """
    try:
        return _load(document)
    except NktError as exc:
        if isinstance(exc, RotatorError):
            raise
        raise RotatorError(str(exc)) from exc


def _load(document):
    if not isinstance(document, dict):
        raise RotatorError("rotator configuration document must be a table")
    allowed = {field.name for field in fields(RotatorDeviceConfig)} - {"role"}
    scan = document.get("rotator_scan", {})
    grid_keys = {role + suffix for role in ROLES for suffix in ("_angles_deg", "_ranges")}
    keys(scan, "rotator_scan", set(), grid_keys)
    devices, grids = [], []
    for role in ROLES:
        table = document.get(role, {})
        keys(table, role, set(), allowed | {"enabled"})
        enabled = table.get("enabled", False)
        if type(enabled) is not bool:
            raise RotatorError(role + ".enabled must be boolean")
        supplied_grid = any(role + suffix in scan for suffix in ("_angles_deg", "_ranges"))
        if not enabled:
            if supplied_grid:
                raise RotatorError(role + " is disabled but has an angle grid")
            # Labels still need to be safe display strings even on disabled slots.
            if "label" in table:
                text(table["label"], role + ".label", empty=True)
                if any(ord(c) < 32 or ord(c) == 127 for c in table["label"]):
                    raise RotatorError(role + ".label must be a single line")
            continue
        if "serial_number" not in table:
            raise RotatorError(role + ".serial_number is required for an enabled device")
        device = RotatorDeviceConfig(role=role, **{key: value for key, value in table.items()
                                                if key != "enabled"})
        device.validate()
        values = expand_grid(scan, role + "_angles_deg", role + "_ranges", "rotator_scan")
        grids.append(tuple(device.validate_angle(value.value) for value in values))
        devices.append(device)
    if not devices:
        return None
    count = math.prod(len(grid) for grid in grids)
    if count > MAX_GRID_POINTS:
        raise RotatorError(f"rotator Cartesian grid exceeds {MAX_GRID_POINTS} points")
    from itertools import product
    points = tuple({device.angle_key: value for device, value in zip(devices, values)}
                   for values in product(*grids))
    result = RotatorConfig(tuple(devices), points)
    result.validate()
    return result
