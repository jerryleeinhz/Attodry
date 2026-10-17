"""Strict optical-module configuration; importing this module performs no I/O."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path
import tomllib

from .nkt_config import OTHER_TABLES, NKT_TABLES, keys, number, integer, text, NktError


OPTICAL_TABLES = {"pem", "pem_run", "pm100d", "pm100d_run", "optical_scan", "power_feedback",
                  "chopper", "cld1010", "optical_background"}


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
    max_peak_to_peak_fraction: float | None = None
    noise_floor_w: float = 0.0
    max_drift_fraction_per_s: float | None = None

    def validate(self):
        number(self.duration_s, "duration_s", 0.001, 3600)
        integer(self.min_samples, "min_samples", 2, 100000)
        number(self.sample_interval_s, "sample_interval_s", 0.001, self.duration_s)
        number(self.max_peak_to_peak_w, "max_peak_to_peak_w", 0, 1e6)
        number(self.noise_floor_w, "noise_floor_w", 0, self.max_peak_to_peak_w)
        for name in ("max_peak_to_peak_fraction", "max_drift_fraction_per_s"):
            if getattr(self, name) is not None:
                number(getattr(self, name), name, 0, 1)

    def variation_limit_w(self, target_w):
        if self.max_peak_to_peak_fraction is None:
            return self.max_peak_to_peak_w
        number(target_w, "relative power-window target", 1e-15, 1e6)
        return min(self.max_peak_to_peak_w, max(self.noise_floor_w,
                   target_w * self.max_peak_to_peak_fraction))


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
    target_mapping: str = "paired"
    initial_current_mode: str = "point"
    initial_source_levels_pct: tuple[float, ...] | tuple[tuple[float, ...], ...] | None = None
    tuning_tolerance_fraction: float | None = None
    source_power_min_pct: float | None = None
    source_power_max_pct: float | None = None
    source_power_step_pct: float | None = None
    current_min_a: float | None = None
    current_max_a: float | None = None
    current_step_a: float | None = None

    def tuning_tolerance_w(self):
        return (self._tolerance(self.target_power_w) if self.tuning_tolerance_fraction is None
                else self.target_power_w * self.tuning_tolerance_fraction)

    @property
    def targets_per_optical_point(self):
        if self.target_mapping not in ("paired", "cartesian"):
            raise NktError("target_mapping must be paired or cartesian")
        return len(self._targets()) if self.target_mapping == "cartesian" else 1

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

    def _check_initial_current_mode(self):
        if self.initial_current_mode not in ("point", "per_target", "previous", "grid"):
            raise NktError("initial_current_mode must be point, per_target, previous or grid")
        if self.initial_current_mode != "point" and self.actuator not in ("source_current", "source_power"):
            raise NktError("initial_current_mode requires the source_current or source_power actuator")
        if self.initial_current_mode != "point" and self.target_mapping != "cartesian":
            raise NktError("Non-default initial_current_mode requires Cartesian target_mapping")
        levels = self.initial_source_levels_pct
        if self.initial_current_mode in ("point", "previous"):
            if levels is not None:
                raise NktError("point and previous initial_current_mode must omit initial_source_levels_pct")
            return
        if type(levels) is not tuple or not levels:
            raise NktError("initial_source_levels_pct must be a nonempty immutable array")

    def _validate_initial_current_values(self):
        """Validate the complete immutable current array during preflight."""
        self._check_initial_current_mode()
        if self.initial_source_levels_pct is None:
            return
        levels = self.initial_source_levels_pct
        lower, upper, _ = self.source_bounds()
        targets = self._targets()
        rows = (levels,) if self.initial_current_mode == "per_target" else levels
        from .nkt_config import tenths
        for row in rows:
            if type(row) is not tuple or len(row) != len(targets):
                raise NktError("initial_source_levels_pct requires exactly one current per target in every row")
            for value in row:
                tenths(number(value, "initial source current", lower, upper))

    def _check_initial_grid_size(self, point_count):
        """Constant-time outer dimensions; every row is checked by preflight."""
        integer(point_count, "point_count", 1, 10000)
        self._check_initial_current_mode()
        factor = self.targets_per_optical_point
        if self.target_mapping == "cartesian" and point_count % factor:
            raise NktError("Cartesian point_count must cover complete target groups")
        rows = point_count // factor
        if self.initial_current_mode == "per_target" and rows != 1:
            raise NktError("per_target initial_current_mode requires exactly one nkt_run.points row")
        if self.initial_current_mode == "grid" and len(self.initial_source_levels_pct) != rows:
            raise NktError("grid initial_source_levels_pct row count must match nkt_run.points")

    def validate_initial_currents(self, point_count, nkt_max_level_pct=None):
        """Check all starts against the expanded grid before constructing resources."""
        self._check_initial_grid_size(point_count)
        self._validate_initial_current_values()
        if nkt_max_level_pct is not None:
            number(nkt_max_level_pct, "NKT source current maximum", 0, 100)
            levels = self.initial_source_levels_pct
            if levels is not None:
                current_rows = (levels,) if self.initial_current_mode == "per_target" else levels
                for row in current_rows:
                    for value in row:
                        number(value, "initial source current", 0, nkt_max_level_pct)

    def initial_source_level_pct(self, index, point_count, point_default):
        """Resolve a static start; previous-mode history belongs to the point session.

        Indices use row-major expanded order (optical row, then target power).
        Previous returns the row's default; only a qualified runtime readback can
        replace it. Scalar resolved feedback objects do not retain plan arrays.
        """
        integer(point_count, "point_count", 1, 10000)
        integer(index, "point index", 0, point_count - 1)
        self._check_initial_grid_size(point_count)
        selected = point_default
        if self.initial_current_mode == "per_target":
            row = self.initial_source_levels_pct
            if len(row) != self.targets_per_optical_point:
                raise NktError("initial_source_levels_pct requires exactly one current per target")
            selected = row[index]
        elif self.initial_current_mode == "grid":
            factor = self.targets_per_optical_point
            row = self.initial_source_levels_pct[index // factor]
            if type(row) is not tuple or len(row) != factor:
                raise NktError("initial_source_levels_pct requires exactly one current per target in every row")
            selected = row[index % factor]
        from .nkt_config import tenths
        lower, upper = 0, 100
        if self.actuator in ("source_current", "source_power"):
            lower, upper, _ = self.source_bounds()
        value = number(selected, "initial source current", lower, upper)
        tenths(value)
        return value

    def for_point(self, index, point_count):
        """Resolve one explicit NKT point to scalar W target and tolerance."""
        integer(point_count, "point_count", 1, 10000)
        integer(index, "point index", 0, point_count - 1)
        self._check_initial_grid_size(point_count)
        if self.initial_current_mode in ("per_target", "grid"):
            self.initial_source_level_pct(index, point_count, None)
        targets = self._targets()
        factor = self.targets_per_optical_point
        if self.target_mapping == "cartesian" and point_count % factor:
            raise NktError("Cartesian point_count must cover complete target groups")
        if self.target_mapping == "paired" and self.target_powers_w is not None and len(targets) != point_count:
            raise NktError("target_powers_w length must match nkt_run.points")
        limit = number(self.max_power_w, "feedback max_power_w", 1e-15, 1e6)
        target_index = index % factor if self.target_mapping == "cartesian" else index
        target = number(targets[target_index] if self.target_powers_w is not None else targets[0],
                        "target_power_w", 1e-15, limit)
        resolved = replace(self, target_power_w=target, target_tolerance_w=self._tolerance(target),
                           target_powers_w=None, target_tolerance_fraction=None,
                           initial_current_mode="point", initial_source_levels_pct=None)
        resolved.validate()
        return resolved

    def validate(self):
        self.targets_per_optical_point
        if self.actuator not in ("varia_nd", "source_current", "source_power", "cld_current"):
            raise NktError("Feedback actuator must be varia_nd, source_current, source_power or cld_current")
        self.window.validate()
        number(self.max_power_w, "feedback max_power_w", 1e-15, 1e6)
        if self.reduce_above_power_w is not None:
            if self.actuator not in ("source_current", "source_power"):
                raise NktError("reduce_above_power_w requires the source_current actuator or the source_power actuator")
            number(self.reduce_above_power_w, "reduce_above_power_w", 1e-15, self.max_power_w)
            if self.reduce_above_power_w >= self.max_power_w:
                raise NktError("Reduce-above threshold must be strictly below the hard power limit")
        for value in self._targets():
            target = number(value, "target_power_w", 1e-15, self.max_power_w)
            tolerance = self._tolerance(target)
            if self.tuning_tolerance_fraction is not None:
                tuning = target * number(self.tuning_tolerance_fraction, "tuning_tolerance_fraction", 0, 1)
                if tuning > tolerance + 1e-15:
                    raise NktError("Tuning tolerance must not exceed formal target tolerance")
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
        if self.actuator == "varia_nd":
            if any(value is not None for value in current_values + power_values + cld_values) or self.minimum_signal_power_w is not None:
                raise NktError("varia_nd feedback must not contain source parameters")
            number(self.nd_min_pct, "nd_min_pct", 0, 100)
            number(self.nd_max_pct, "nd_max_pct", self.nd_min_pct, 100)
            number(self.nd_step_pct, "nd_step_pct", 0.1, 100)
            for value in nd_values[:3]:
                tenths(value)
            boolean(self.increasing_nd_increases_power, "increasing_nd_increases_power")
        elif self.actuator in ("source_current", "source_power"):
            unused = power_values if self.actuator == "source_current" else current_values
            if any(value is not None for value in nd_values + unused + cld_values):
                raise NktError("Source feedback must omit parameters of other actuators")
            lower, upper, step = self.source_bounds()
            for value in (lower, upper, step):
                tenths(value)
        else:
            if any(value is not None for value in nd_values + current_values + power_values):
                raise NktError("cld_current feedback must omit ND and percentage parameters")
            number(self.current_min_a, "current_min_a", 0, 1)
            number(self.current_max_a, "current_max_a", self.current_min_a, 1)
            number(self.current_step_a, "current_step_a", 1e-12, 1)
        self._validate_initial_current_values()

    def source_bounds(self):
        prefix = "source_power" if self.actuator == "source_power" else "source_current"
        lower = number(getattr(self, prefix + "_min_pct"), prefix + "_min_pct", 0, 100)
        upper = number(getattr(self, prefix + "_max_pct"), prefix + "_max_pct", lower, 100)
        step = number(getattr(self, prefix + "_step_pct"), prefix + "_step_pct", 0.1, 100)
        return lower, upper, step


@dataclass(frozen=True)
class ScanConfig:
    mode: str
    use_pem: bool
    use_power_meter: bool
    sample_interval_s: float
    peak_retardance_waves: float | None = None
    feedback: FeedbackConfig | None = None
    target_deviation_policy: str = "abort"
    laser_source: str = "nkt"
    max_power_retries: int = 0
    power_retry_timeout_s: float = 300.0

    def require_standalone_scan(self):
        if self.laser_source != "nkt" or self.max_power_retries or self.target_deviation_policy != "abort" or (self.feedback and
                (self.feedback.target_mapping != "paired" or self.feedback.initial_current_mode != "point")):
            raise NktError("target_deviation_policy, Cartesian target_mapping and initial_current_mode are optical point session "
                           "options, unsupported by the standalone optical scan")

    def validate(self, nkt, pem=None, pm=None):
        nkt.validate()
        from .photoresponse_cld import CldConfig
        cld = isinstance(nkt, CldConfig)
        if self.laser_source not in ("nkt", "cld") or cld != (self.laser_source == "cld"):
            raise NktError("optical_scan.laser_source must match the selected nkt or cld configuration")
        integer(self.max_power_retries, "max_power_retries", 0, 3)
        number(self.power_retry_timeout_s, "power_retry_timeout_s", 0.01, 86400)
        if self.mode not in ("direct", "power_stabilized"):
            raise NktError("Optical mode must be direct or power_stabilized")
        if self.target_deviation_policy not in ("abort", "record_continue"):
            raise NktError("target_deviation_policy must be abort or record_continue")
        if self.mode != "power_stabilized" and self.target_deviation_policy != "abort":
            raise NktError("target_deviation_policy requires power_stabilized mode")
        boolean(self.use_pem, "use_pem")
        boolean(self.use_power_meter, "use_power_meter")
        number(self.sample_interval_s, "sample_interval_s", 0.001, 3600)
        if self.use_pem != (pem is not None) or self.use_power_meter != (pm is not None):
            raise NktError("Explicit enabled devices must match supplied configuration")
        if (pem or pm) and getattr(nkt, "mode", None) == "broadband":
            raise NktError("Broadband has no single wavelength; explicit spectral strategy not implemented")
        if pem:
            for point in nkt.points:
                pem.target(point.wavelength_nm, self.peak_retardance_waves)
        elif self.peak_retardance_waves is not None:
            raise NktError("Retardance requires PEM enabled")
        if pm:
            pm.validate()
        if self.mode == "power_stabilized":
            if not pm or not self.feedback or not nkt.emit or (not cld and nkt.mode != "varia_bandpass"):
                raise NktError("Feedback requires PM, VARIA and explicit emission")
            self.feedback.validate()
            if cld:
                if self.feedback.actuator != "cld_current":
                    raise NktError("CLD requires cld_current feedback in amperes")
                if self.feedback.initial_current_mode != "point":
                    raise NktError("CLD starts use current_points_a; percentage start arrays are NKT-only")
                number(self.feedback.current_max_a, "CLD feedback maximum", 0, nkt.max_current_a)
            elif self.feedback.actuator == "varia_nd":
                if nkt.backend == "nkt_sdk" and not nkt.varia_nd_control_verified:
                    raise NktError("VARIA ND optical control is unverified; hardware feedback is disabled")
            else:
                control = {"source_current": "current", "source_power": "power"}.get(self.feedback.actuator)
                if control is None or nkt.source_control != control:
                    raise NktError("Source feedback actuator must match NKT current/power control mode")
                number(self.feedback.source_bounds()[1], "source feedback maximum", 0, nkt.max_level_pct)
            if self.feedback.max_power_w > pm.max_power_w:
                raise NktError("Feedback limit exceeds PM limit")
            factor = self.feedback.targets_per_optical_point
            integer(len(nkt.points) * factor, "expanded optical point count", 1, 10000)
            self.feedback.validate_initial_currents(len(nkt.points) * factor,
                                                   None if cld else nkt.max_level_pct)
            for i, point in enumerate(nkt.points):
                self.feedback.for_point(i * factor, len(nkt.points) * factor)
                if cld:
                    number(point.current_a, "initial CLD current", self.feedback.current_min_a, self.feedback.current_max_a)
                elif self.feedback.actuator == "varia_nd":
                    number(point.nd_pct, "initial ND", self.feedback.nd_min_pct, self.feedback.nd_max_pct)
                else:
                    if point.nd_pct is not None:
                        raise NktError("source_current feedback must omit point nd_pct to preserve ND")
                    if point.pulse_picker_ratio is not None:
                        raise NktError("source_current feedback must omit point pulse_picker_ratio to preserve PP")
                    low, high, _ = self.feedback.source_bounds()
                    number(point.source_level_pct, "initial source setting", low, high)
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
         {"peak_retardance_waves", "target_deviation_policy", "laser_source",
          "max_power_retries", "power_retry_timeout_s"})
    feedback = None
    if table["mode"] == "power_stabilized":
        raw = dict(doc.get("power_feedback", {}))
        names = {f.name for f in fields(FeedbackConfig)}
        optional = {"target_power_w", "target_powers_w", "target_tolerance_w", "target_tolerance_fraction",
                    "actuator", "nd_min_pct", "nd_max_pct", "nd_step_pct", "increasing_nd_increases_power",
                    "source_current_min_pct", "source_current_max_pct", "source_current_step_pct",
                    "minimum_signal_power_w", "reduce_above_power_w", "target_mapping",
                    "initial_current_mode", "initial_source_levels_pct", "tuning_tolerance_fraction",
                    "source_power_min_pct", "source_power_max_pct", "source_power_step_pct",
                    "current_min_a", "current_max_a", "current_step_a", "target_power_ranges"}
        keys(raw, "power_feedback", names - optional, optional)
        if "target_power_ranges" in raw:
            if "target_power_w" in raw or "target_powers_w" in raw:
                raise NktError("Provide exactly one of target_power_w, target_powers_w or target_power_ranges")
            from .scan_grid import expand_ranges
            raw["target_powers_w"] = [item.value for item in
                expand_ranges(raw.pop("target_power_ranges"), "power_feedback.target_power_ranges")]
        window = raw["window"]
        window_optional = {"max_peak_to_peak_fraction", "noise_floor_w", "max_drift_fraction_per_s"}
        keys(window, "power_feedback.window", {f.name for f in fields(WindowConfig)} - window_optional,
             window_optional)
        values = {"target_power_w": None, "target_tolerance_w": None,
                  "nd_min_pct": None, "nd_max_pct": None, "nd_step_pct": None,
                  "increasing_nd_increases_power": None, **raw,
                  "window": WindowConfig(**window)}
        if "target_powers_w" in values:
            if type(values["target_powers_w"]) is not list:
                raise NktError("target_powers_w must be an array")
            values["target_powers_w"] = tuple(values["target_powers_w"])
        if "initial_source_levels_pct" in values:
            if type(values["initial_source_levels_pct"]) is not list:
                raise NktError("initial_source_levels_pct must be an array")
            values["initial_source_levels_pct"] = tuple(
                tuple(row) if type(row) is list else row for row in values["initial_source_levels_pct"])
        feedback = FeedbackConfig(**values)
    pem = load_pem_config(path) if table["use_pem"] else None
    pm = load_pm_config(path) if table["use_power_meter"] else None
    config = ScanConfig(**table, feedback=feedback)
    config.validate(nkt, pem, pm)
    return config, pem, pm
