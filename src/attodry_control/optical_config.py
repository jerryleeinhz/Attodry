"""Strict optical-module configuration; importing this module performs no I/O."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path
import tomllib

from .nkt_config import OTHER_TABLES, NKT_TABLES, keys, number, integer, text, NktError


OPTICAL_TABLES = {"pem", "pem_run", "pm100d", "pm100d_run", "optical_scan", "power_feedback"}


def document(path):
    with Path(path).open("rb") as stream:
        doc = tomllib.load(stream)
    keys(doc, "top level", set(), OTHER_TABLES | NKT_TABLES | OPTICAL_TABLES)
    return doc


def boolean(value, name):
    if type(value) is not bool:
        raise NktError(f"{name} must be boolean")
    return value


def peak_retardance_nm(wavelength_nm, waves):
    return number(wavelength_nm, "wavelength_nm", 0.001, 1e6) * number(waves, "waves", 0.000001, 10)


@dataclass(frozen=True)
class PemConfig:
    backend: str
    io_timeout_s: float
    settle_timeout_s: float
    poll_interval_s: float
    wavelength_min_nm: float
    wavelength_max_nm: float
    amplitude_min_nm: float
    amplitude_max_nm: float
    amplitude_tolerance_nm: float
    frequency_min_hz: float
    frequency_max_hz: float
    finish_action: str
    port: str = ""
    expected_idn: str = ""

    def validate(self):
        if self.backend not in ("simulation", "serial"):
            raise NktError("PEM backend must be simulation or serial")
        number(self.io_timeout_s, "io_timeout_s", 0.3, 60)
        number(self.settle_timeout_s, "settle_timeout_s", 0.3, 3600)
        number(self.poll_interval_s, "poll_interval_s", 0.001, self.settle_timeout_s)
        number(self.wavelength_min_nm, "wavelength_min_nm", 0.001, 1e6)
        number(self.wavelength_max_nm, "wavelength_max_nm", self.wavelength_min_nm, 1e6)
        number(self.amplitude_min_nm, "amplitude_min_nm", 0, 1e6)
        number(self.amplitude_max_nm, "amplitude_max_nm", self.amplitude_min_nm, 1e6)
        number(self.amplitude_tolerance_nm, "amplitude_tolerance_nm", 0, 1)
        number(self.frequency_min_hz, "frequency_min_hz", 1, 1e7)
        number(self.frequency_max_hz, "frequency_max_hz", self.frequency_min_hz, 1e7)
        if self.finish_action not in ("disable_ack", "leave_active"):
            raise NktError("PEM finish_action must be disable_ack or leave_active; physical-off query unavailable")
        text(self.port, "port", empty=True)
        text(self.expected_idn, "expected_idn", empty=True)

    def target(self, wavelength_nm, waves):
        self.validate()
        number(wavelength_nm, "PEM wavelength", self.wavelength_min_nm, self.wavelength_max_nm)
        return number(peak_retardance_nm(wavelength_nm, waves), "PEM amplitude",
                      self.amplitude_min_nm, self.amplitude_max_nm)

    def require_hardware(self, writes=False):
        self.validate()
        if self.backend != "serial" or not self.port or "CHANGE_ME" in self.port:
            raise NktError("Configure PEM serial port before connection")
        if writes and (not self.expected_idn or "CHANGE_ME" in self.expected_idn):
            raise NktError("Confirm exact PEM firmware IDN before writes")


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
        if self.actuator not in ("varia_nd", "source_current"):
            raise NktError("Feedback actuator must be varia_nd or source_current")
        self.window.validate()
        number(self.max_power_w, "feedback max_power_w", 1e-15, 1e6)
        if self.reduce_above_power_w is not None:
            if self.actuator != "source_current":
                raise NktError("reduce_above_power_w requires the source_current actuator")
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
            if self.actuator == "source_current":
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
        if self.actuator == "varia_nd":
            if any(value is not None for value in current_values) or self.minimum_signal_power_w is not None:
                raise NktError("varia_nd feedback must not contain source current parameters")
            number(self.nd_min_pct, "nd_min_pct", 0, 100)
            number(self.nd_max_pct, "nd_max_pct", self.nd_min_pct, 100)
            number(self.nd_step_pct, "nd_step_pct", 0.1, 100)
            for value in nd_values[:3]:
                tenths(value)
            boolean(self.increasing_nd_increases_power, "increasing_nd_increases_power")
        else:
            if any(value is not None for value in nd_values):
                raise NktError("source_current feedback must not contain ND parameters")
            number(self.source_current_min_pct, "source_current_min_pct", 0, 100)
            number(self.source_current_max_pct, "source_current_max_pct", self.source_current_min_pct, 100)
            number(self.source_current_step_pct, "source_current_step_pct", 0.1, 100)
            for value in current_values:
                tenths(value)


@dataclass(frozen=True)
class ScanConfig:
    mode: str
    use_pem: bool
    use_power_meter: bool
    sample_interval_s: float
    peak_retardance_waves: float | None = None
    feedback: FeedbackConfig | None = None

    def validate(self, nkt, pem=None, pm=None):
        nkt.validate()
        if self.mode not in ("direct", "power_stabilized"):
            raise NktError("Optical mode must be direct or power_stabilized")
        boolean(self.use_pem, "use_pem")
        boolean(self.use_power_meter, "use_power_meter")
        number(self.sample_interval_s, "sample_interval_s", 0.001, 3600)
        if self.use_pem != (pem is not None) or self.use_power_meter != (pm is not None):
            raise NktError("Explicit enabled devices must match supplied configuration")
        if (pem or pm) and nkt.mode == "broadband":
            raise NktError("Broadband has no single wavelength; explicit spectral strategy not implemented")
        if pem:
            for point in nkt.points:
                pem.target(point.wavelength_nm, self.peak_retardance_waves)
        elif self.peak_retardance_waves is not None:
            raise NktError("Retardance requires PEM enabled")
        if pm:
            pm.validate()
        if self.mode == "power_stabilized":
            if not pm or not self.feedback or not nkt.emit or nkt.mode != "varia_bandpass":
                raise NktError("Feedback requires PM, VARIA and explicit emission")
            self.feedback.validate()
            if self.feedback.actuator == "varia_nd":
                if nkt.backend == "nkt_sdk" and not nkt.varia_nd_control_verified:
                    raise NktError("VARIA ND optical control is unverified; hardware feedback is disabled")
            else:
                if nkt.source_control != "current":
                    raise NktError("source_current feedback requires NKT current control mode")
                number(self.feedback.source_current_max_pct, "source current maximum", 0, nkt.max_level_pct)
            if self.feedback.max_power_w > pm.max_power_w:
                raise NktError("Feedback limit exceeds PM limit")
            for i, point in enumerate(nkt.points):
                self.feedback.for_point(i, len(nkt.points))
                if self.feedback.actuator == "varia_nd":
                    number(point.nd_pct, "initial ND", self.feedback.nd_min_pct, self.feedback.nd_max_pct)
                else:
                    if point.nd_pct is not None:
                        raise NktError("source_current feedback must omit point nd_pct to preserve ND")
                    if point.pulse_picker_ratio is not None:
                        raise NktError("source_current feedback must omit point pulse_picker_ratio to preserve PP")
                    number(point.source_level_pct, "initial source current", self.feedback.source_current_min_pct,
                           self.feedback.source_current_max_pct)
        elif self.feedback is not None:
            raise NktError("direct must not contain active feedback")


def load_pem_config(path):
    table = document(path).get("pem", {})
    required = {f.name for f in fields(PemConfig)} - {"port", "expected_idn"}
    keys(table, "pem", required, {"port", "expected_idn"})
    config = PemConfig(**table)
    config.validate()
    return config


def load_pm_config(path):
    table = document(path).get("pm100d", {})
    optional = {"range_w", "resource", "expected_serial", "expected_sensor_serial"}
    required = {f.name for f in fields(PmConfig)} - optional
    keys(table, "pm100d", required, optional)
    config = PmConfig(**table)
    config.validate()
    return config


def load_scan_config(path, nkt):
    doc = document(path)
    table = doc.get("optical_scan", {})
    keys(table, "optical_scan", {"mode", "use_pem", "use_power_meter", "sample_interval_s"},
         {"peak_retardance_waves"})
    feedback = None
    if table["mode"] == "power_stabilized":
        raw = doc.get("power_feedback", {})
        names = {f.name for f in fields(FeedbackConfig)}
        optional = {"target_power_w", "target_powers_w", "target_tolerance_w", "target_tolerance_fraction",
                    "actuator", "nd_min_pct", "nd_max_pct", "nd_step_pct", "increasing_nd_increases_power",
                    "source_current_min_pct", "source_current_max_pct", "source_current_step_pct",
                    "minimum_signal_power_w", "reduce_above_power_w"}
        keys(raw, "power_feedback", names - optional, optional)
        window = raw["window"]
        keys(window, "power_feedback.window", {f.name for f in fields(WindowConfig)})
        values = {"target_power_w": None, "target_tolerance_w": None,
                  "nd_min_pct": None, "nd_max_pct": None, "nd_step_pct": None,
                  "increasing_nd_increases_power": None, **raw,
                  "window": WindowConfig(**window)}
        if "target_powers_w" in values:
            if type(values["target_powers_w"]) is not list:
                raise NktError("target_powers_w must be an array")
            values["target_powers_w"] = tuple(values["target_powers_w"])
        feedback = FeedbackConfig(**values)
    pem = load_pem_config(path) if table["use_pem"] else None
    pm = load_pm_config(path) if table["use_power_meter"] else None
    config = ScanConfig(**table, feedback=feedback)
    config.validate(nkt, pem, pm)
    return config, pem, pm
