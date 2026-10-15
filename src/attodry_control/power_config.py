"""PM and stability contracts extracted from codex/pem-module optical_config.

NKT feedback retains the existing policy; CLD adds a bounded current actuator.
NKT constant-power mode adds a proportional, step-bounded power-setpoint actuator.
No instrument I/O is performed by configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from .nkt_config import NktError, keys, number, integer, text


def boolean(value, name):
    if type(value) is not bool:
        raise NktError(f"{name} must be boolean")
    return value


@dataclass(frozen=True)
class PmConfig:
    backend: str
    io_timeout_s: float
    wavelength_tolerance_nm: float
    average_count: int
    auto_range: bool
    max_power_w: float
    measurement_plane: str
    range_w: float | None = None
    resource: str = ""
    expected_serial: str = ""
    expected_sensor_serial: str = ""

    def validate(self):
        if self.backend not in ("simulation", "visa"):
            raise NktError("PM100D backend must be simulation or visa")
        number(self.io_timeout_s, "io_timeout_s", 0.01, 60)
        number(self.wavelength_tolerance_nm, "wavelength_tolerance_nm", 0, 1)
        integer(self.average_count, "average_count", 1, 1000000)
        boolean(self.auto_range, "auto_range")
        number(self.max_power_w, "max_power_w", 1e-15, 1e6)
        text(self.measurement_plane, "measurement_plane")
        if not self.auto_range and self.range_w is None:
            raise NktError("Fixed PM range requires range_w")
        if self.range_w is not None:
            number(self.range_w, "range_w", 1e-15, 1e6)
        for name in ("resource", "expected_serial", "expected_sensor_serial"):
            text(getattr(self, name), name, empty=True)

    def require_hardware(self, writes=False):
        self.validate()
        if self.backend != "visa" or not self.resource or "CHANGE_ME" in self.resource:
            raise NktError("Configure explicit PM100D VISA resource before connection")
        if writes and any(not v or "CHANGE_ME" in v for v in
                          (self.expected_serial, self.expected_sensor_serial)):
            raise NktError("Confirm console and sensor serials before PM setting writes")
        if "CHANGE_ME" in self.measurement_plane:
            raise NktError("Document the PM measurement plane before hardware connection")


@dataclass(frozen=True)
class WindowConfig:
    duration_s: float
    min_samples: int
    sample_interval_s: float
    max_peak_to_peak_w: float

    def validate(self):
        number(self.duration_s, "duration_s", 0.001, 3600)
        integer(self.min_samples, "min_samples", 2, 100000)
        number(self.sample_interval_s, "sample_interval_s", 0.001, self.duration_s)
        number(self.max_peak_to_peak_w, "max_peak_to_peak_w", 0, 1e6)


ACTUATORS = ("varia_nd", "source_current", "source_power", "cld_current")


@dataclass(frozen=True)
class FeedbackConfig:
    target_power_w: float | None
    target_tolerance_w: float | None
    max_power_w: float
    timeout_s: float
    hold_s: float
    nd_min_pct: float | None
    nd_max_pct: float | None
    nd_step_pct: float | None
    increasing_nd_increases_power: bool | None
    window: WindowConfig
    target_powers_w: tuple[float, ...] | None = None
    target_tolerance_fraction: float | None = None
    actuator: str = "varia_nd"
    source_current_min_pct: float | None = None
    source_current_max_pct: float | None = None
    source_current_step_pct: float | None = None
    minimum_signal_power_w: float | None = None
    reduce_above_power_w: float | None = None
    current_min_a: float | None = None
    current_max_a: float | None = None
    current_step_a: float | None = None
    source_power_min_pct: float | None = None
    source_power_max_pct: float | None = None
    source_power_step_pct: float | None = None

    def _targets(self):
        if (self.target_power_w is None) == (self.target_powers_w is None):
            raise NktError("Provide exactly one of target_power_w or target_powers_w")
        if self.target_powers_w is not None:
            if type(self.target_powers_w) is not tuple or not 1 <= len(self.target_powers_w) <= 10000:
                raise NktError("target_powers_w must contain 1 to 10000 explicit targets")
            return self.target_powers_w
        return (self.target_power_w,)

    def _tolerance(self, target):
        if (self.target_tolerance_w is None) == (self.target_tolerance_fraction is None):
            raise NktError("Provide exactly one of target_tolerance_w or target_tolerance_fraction")
        if self.target_tolerance_fraction is not None:
            return target * number(self.target_tolerance_fraction, "target_tolerance_fraction", 0, 1)
        return number(self.target_tolerance_w, "target_tolerance_w", 0, target)

    def for_point(self, index, point_count):
        """Resolve one explicit NKT point to scalar W target and tolerance."""
        integer(point_count, "point_count", 1, 10000)
        integer(index, "point index", 0, point_count - 1)
        targets = self._targets()
        if self.target_powers_w is not None and len(targets) != point_count:
            raise NktError("target_powers_w length must match nkt_run.points")
        limit = number(self.max_power_w, "feedback max_power_w", 1e-15, 1e6)
        target = number(targets[index] if self.target_powers_w is not None else targets[0],
                        "target_power_w", 1e-15, limit)
        resolved = replace(self, target_power_w=target, target_tolerance_w=self._tolerance(target),
                           target_powers_w=None, target_tolerance_fraction=None)
        resolved.validate()
        return resolved

    def validate(self):
        if self.actuator not in ACTUATORS:
            raise NktError("Feedback actuator must be varia_nd, source_current, source_power or cld_current")
        self.window.validate()
        number(self.max_power_w, "feedback max_power_w", 1e-15, 1e6)
        if self.reduce_above_power_w is not None:
            if self.actuator not in ("source_current", "source_power"):
                raise NktError("reduce_above_power_w requires the source_current or source_power actuator")
            number(self.reduce_above_power_w, "reduce_above_power_w", 1e-15, self.max_power_w)
            if self.reduce_above_power_w >= self.max_power_w:
                raise NktError("Reduce-above threshold must be strictly below the hard power limit")
        for value in self._targets():
            target = number(value, "target_power_w", 1e-15, self.max_power_w)
            tolerance = self._tolerance(target)
            if target + tolerance > self.max_power_w:
                raise NktError("Target tolerance exceeds power limit")
            if self.reduce_above_power_w is not None and target + tolerance > self.reduce_above_power_w:
                raise NktError("Target tolerance exceeds the reduce-above power threshold")
            if self.actuator in ("source_current", "source_power", "cld_current"):
                signal = number(self.minimum_signal_power_w, "minimum_signal_power_w", 1e-15, self.max_power_w)
                if signal >= target - tolerance:
                    raise NktError("Minimum signal power must be below every target tolerance lower bound")
        number(self.timeout_s, "feedback timeout_s", self.window.duration_s, 86400)
        number(self.hold_s, "hold_s", 0, self.timeout_s)
        from .nkt_config import tenths
        nd_values = (self.nd_min_pct, self.nd_max_pct, self.nd_step_pct,
                     self.increasing_nd_increases_power)
        current_values = (self.source_current_min_pct, self.source_current_max_pct,
                          self.source_current_step_pct)
        power_values = (self.source_power_min_pct, self.source_power_max_pct,
                        self.source_power_step_pct)
        cld_values = (self.current_min_a, self.current_max_a, self.current_step_a)
        if self.actuator != "cld_current" and any(value is not None for value in cld_values):
            raise NktError("CLD current parameters require cld_current actuator")
        if self.actuator != "source_power" and any(value is not None for value in power_values):
            raise NktError("NKT power setpoint parameters require source_power actuator")
        if self.actuator == "cld_current":
            if any(value is not None for value in nd_values + current_values):
                raise NktError("cld_current feedback must not contain NKT actuator parameters")
            number(self.current_min_a, "current_min_a", 0, 1)
            number(self.current_max_a, "current_max_a", self.current_min_a, 1)
            number(self.current_step_a, "current_step_a", 1e-12, self.current_max_a)
            return
        if self.actuator == "varia_nd":
            if any(value is not None for value in current_values) or self.minimum_signal_power_w is not None:
                raise NktError("varia_nd feedback must not contain source current parameters")
            number(self.nd_min_pct, "nd_min_pct", 0, 100)
            number(self.nd_max_pct, "nd_max_pct", self.nd_min_pct, 100)
            number(self.nd_step_pct, "nd_step_pct", 0.1, 100)
            for value in nd_values[:3]:
                tenths(value)
            boolean(self.increasing_nd_increases_power, "increasing_nd_increases_power")
        elif self.actuator == "source_power":
            if any(value is not None for value in nd_values + current_values):
                raise NktError("source_power feedback must not contain ND or source current parameters")
            number(self.source_power_min_pct, "source_power_min_pct", 0, 100)
            number(self.source_power_max_pct, "source_power_max_pct", self.source_power_min_pct, 100)
            number(self.source_power_step_pct, "source_power_step_pct", 0.1, 100)
            for value in power_values:
                tenths(value)
        else:
            if any(value is not None for value in nd_values):
                raise NktError("source_current feedback must not contain ND parameters")
            number(self.source_current_min_pct, "source_current_min_pct", 0, 100)
            number(self.source_current_max_pct, "source_current_max_pct", self.source_current_min_pct, 100)
            number(self.source_current_step_pct, "source_current_step_pct", 0.1, 100)
            for value in current_values:
                tenths(value)


@dataclass(frozen=True)
class PhotoresponsePowerConfig:
    meter: PmConfig
    feedback: FeedbackConfig
    nd_control_verified: bool = False

    @property
    def targets(self):
        return self.feedback._targets()

    def for_target(self, index):
        return self.feedback.for_point(index, len(self.targets))

    def validate(self, optical):
        self.meter.validate()
        self.feedback.validate()
        boolean(self.nd_control_verified, "nd_control_verified")
        f = self.feedback
        if (optical.backend == "simulation") != (self.meter.backend == "simulation"):
            raise NktError("Power meter and light source must share simulation/hardware mode")
        if f.max_power_w > self.meter.max_power_w:
            raise NktError("Feedback limit exceeds PM limit")
        is_cld = hasattr(optical, "diode_model")
        if is_cld != (f.actuator == "cld_current"):
            raise NktError("Power actuator must match the selected light source")
        if is_cld:
            number(f.current_max_a, "feedback CLD maximum", 0, optical.max_current_a)
            if f.current_step_a <= optical.current_tolerance_a:
                raise NktError("CLD feedback step must exceed current readback tolerance")
            if len({p.wavelength_nm for p in optical.points}) != 1:
                raise NktError("CLD uses one fixed nominal wavelength")
            for p in optical.points:
                number(p.current_a, "initial LD current", f.current_min_a, f.current_max_a)
        else:
            if optical.mode != "varia_bandpass":
                raise NktError("NKT power feedback requires a VARIA wavelength point")
            if f.actuator == "source_current":
                if optical.source_control != "current":
                    raise NktError("source_current requires NKT current control")
                number(f.source_current_max_pct, "feedback NKT maximum", 0, optical.max_level_pct)
                for p in optical.points:
                    number(p.source_level_pct, "initial source current", f.source_current_min_pct,
                           f.source_current_max_pct)
            elif f.actuator == "source_power":
                if optical.source_control != "power":
                    raise NktError("source_power requires NKT power control")
                number(f.source_power_max_pct, "feedback NKT maximum", 0, optical.max_level_pct)
                for p in optical.points:
                    number(p.source_level_pct, "initial source power setpoint", f.source_power_min_pct,
                           f.source_power_max_pct)
            else:
                if optical.backend != "simulation" and not self.nd_control_verified:
                    raise NktError("Hardware ND feedback requires verified optical direction/control")
                for p in optical.points:
                    number(p.nd_pct, "initial ND", f.nd_min_pct, f.nd_max_pct)


def load_photoresponse_power(doc):
    if "pm100d" not in doc and "power_feedback" not in doc:
        return None
    if "pm100d" not in doc or "power_feedback" not in doc:
        raise NktError("Photoresponse closed loop requires both pm100d and power_feedback")
    pm = doc["pm100d"]
    optional = {"range_w", "resource", "expected_serial", "expected_sensor_serial"}
    keys(pm, "pm100d", {f.name for f in fields(PmConfig)} - optional, optional)
    raw = dict(doc["power_feedback"])
    verified = raw.pop("nd_control_verified", False)
    optional = {"target_power_w", "target_powers_w", "target_tolerance_w", "target_tolerance_fraction",
                "actuator", "nd_min_pct", "nd_max_pct", "nd_step_pct", "increasing_nd_increases_power",
                "source_current_min_pct", "source_current_max_pct", "source_current_step_pct",
                "minimum_signal_power_w", "reduce_above_power_w", "current_min_a", "current_max_a",
                "current_step_a", "source_power_min_pct", "source_power_max_pct", "source_power_step_pct"}
    keys(raw, "power_feedback", {f.name for f in fields(FeedbackConfig)} - optional, optional)
    keys(raw["window"], "power_feedback.window", {f.name for f in fields(WindowConfig)})
    values = {"target_power_w": None, "target_tolerance_w": None, "nd_min_pct": None,
              "nd_max_pct": None, "nd_step_pct": None, "increasing_nd_increases_power": None,
              **raw, "window": WindowConfig(**raw["window"])}
    if "target_powers_w" in values:
        if type(values["target_powers_w"]) is not list:
            raise NktError("target_powers_w must be an explicit array")
        values["target_powers_w"] = tuple(values["target_powers_w"])
    return PhotoresponsePowerConfig(PmConfig(**pm), FeedbackConfig(**values), verified)
