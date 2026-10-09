"""Hardware-free configuration for asynchronous PEM amplitude diagnostics.

Station instruments and optical limits have one source: the referenced station
TOML. Loading this file never opens an instrument or certifies physical wiring.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import hashlib
from pathlib import Path
import tomllib

from .nkt_config import NKT_TABLES, OTHER_TABLES
from .sr865a_settings import TIME_CONSTANTS_S, validate_harmonic_frequency


TABLE_NAME = "pem_internal_diagnostic"


def _number(value, name, *, minimum=0.0, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if value < minimum or (positive and value <= 0):
        raise ValueError(f"{name} is below its allowed minimum.")
    return float(value)


def _text(value, name, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise ValueError(f"{name} must be a {'possibly empty ' if empty else ''}string.")
    return value


def _array(value, name, convert):
    if not isinstance(value, list) or not value or len(value) > 10000:
        raise ValueError(f"{name} must contain 1 to 10000 values.")
    converted = tuple(convert(item, f"{name}[{index}]") for index, item in enumerate(value))
    if len(set(converted)) != len(converted):
        raise ValueError(f"{name} must not contain duplicates.")
    return converted


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer within [{minimum}, {maximum}].")
    return value


@dataclass(frozen=True, slots=True)
class DiagnosticConfig:
    config_path: Path
    station_path: Path
    run_name: str
    note: str
    output_directory: Path
    optical_point_indices: tuple[int, ...]
    internal_frequencies_hz: tuple[float, ...]
    include_pem_frequency: bool
    time_constants_s: tuple[float, ...]
    harmonics: tuple[int, ...]
    capture_duration_s: float
    minimum_capture_rate_hz: float
    capture_timeout_s: float
    monitor_interval_s: float
    pem_frequency_observation_s: float
    xx_excitation_disconnected: bool
    capture_buffer_available: bool
    schema_version: int = 1
    config_sha256: str = ""

    def validate(self):
        _integer(self.schema_version, "schema_version", 1, 1)
        _text(self.run_name, "run_name")
        _text(self.note, "note", empty=True)
        for name in ("include_pem_frequency", "xx_excitation_disconnected", "capture_buffer_available"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean.")
        for name, convert in (
            ("optical_point_indices", lambda v, n: _integer(v, n, 0, 9999)),
            ("internal_frequencies_hz", lambda v, n: _number(v, n, positive=True)),
            ("time_constants_s", lambda v, n: _number(v, n, positive=True)),
            ("harmonics", lambda v, n: _integer(v, n, 1, 99)),
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise ValueError(f"{name} must be an immutable tuple.")
            _array(list(value), name, convert)
        for tau in self.time_constants_s:
            if tau not in TIME_CONSTANTS_S:
                raise ValueError(f"Unsupported SR865A time constant: {tau!r}.")
        for frequency in self.internal_frequencies_hz:
            for harmonic in self.harmonics:
                validate_harmonic_frequency(harmonic, frequency)
        for name in ("capture_duration_s", "minimum_capture_rate_hz", "capture_timeout_s", "monitor_interval_s"):
            _number(getattr(self, name), name, positive=True)
        _number(self.pem_frequency_observation_s, "pem_frequency_observation_s")
        if self.capture_timeout_s <= self.capture_duration_s:
            raise ValueError("capture_timeout_s must exceed capture_duration_s.")
        if self.monitor_interval_s > self.capture_duration_s:
            raise ValueError("monitor_interval_s must not exceed capture_duration_s.")
        if not 0.1 <= self.monitor_interval_s <= 5.0:
            raise ValueError("monitor_interval_s must be between 0.1 and 5 seconds.")
        if self.capture_duration_s * self.minimum_capture_rate_hz < 2:
            raise ValueError("A capture must contain at least two samples.")
        if self.pem_frequency_observation_s and self.pem_frequency_observation_s < self.monitor_interval_s:
            raise ValueError("PEM observation must span at least one monitor interval, or be zero.")
        return self

    def snapshot(self):
        result = asdict(self)
        for key in ("config_path", "station_path", "output_directory"):
            result[key] = str(result[key])
        return result


_REQUIRED = {
    "schema_version", "run_name", "note", "output_directory", "optical_point_indices",
    "internal_frequencies_hz", "include_pem_frequency", "time_constants_s", "harmonics",
    "capture_duration_s", "minimum_capture_rate_hz", "capture_timeout_s", "monitor_interval_s",
    "pem_frequency_observation_s", "xx_excitation_disconnected", "capture_buffer_available",
}


def load_diagnostic_config(path: str | Path) -> DiagnosticConfig:
    """Parse diagnostic keys only; station/profile validation belongs to preflight."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode("utf-8"))
    known_tables = OTHER_TABLES | NKT_TABLES | {TABLE_NAME}
    unknown_tables = document.keys() - known_tables
    if unknown_tables:
        raise ValueError(f"Unsupported top-level tables: {sorted(unknown_tables)}.")
    table = document.get(TABLE_NAME)
    if not isinstance(table, dict):
        raise ValueError(f"{TABLE_NAME} must be a TOML table.")
    missing = _REQUIRED - table.keys()
    unknown = table.keys() - _REQUIRED - {"station_config"}
    if missing or unknown:
        raise ValueError(f"{TABLE_NAME}: missing fields {sorted(missing)}, unsupported fields {sorted(unknown)}.")
    station = Path(_text(table.get("station_config", str(path)), "station_config"))
    directory = Path(_text(table["output_directory"], "output_directory"))
    arrays = {
        "optical_point_indices": lambda v, n: _integer(v, n, 0, 9999),
        "internal_frequencies_hz": lambda v, n: _number(v, n, positive=True),
        "time_constants_s": lambda v, n: _number(v, n, positive=True),
        "harmonics": lambda v, n: _integer(v, n, 1, 99),
    }
    values = {key: table[key] for key in _REQUIRED - arrays.keys() - {"output_directory"}}
    values.update({key: _array(table[key], key, convert) for key, convert in arrays.items()})
    config = DiagnosticConfig(
        config_path=path,
        station_path=(station if station.is_absolute() else path.parent / station).resolve(),
        output_directory=(directory if directory.is_absolute() else path.parent / directory).resolve(),
        config_sha256=hashlib.sha256(raw).hexdigest(),
        **values,
    )
    return config.validate()


load_pem_internal_config = load_diagnostic_config
