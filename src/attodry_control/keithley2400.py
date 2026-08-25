from __future__ import annotations

from dataclasses import dataclass
import importlib
import math
import re
import time
from typing import Any

from .three_smu_config import SmuHardwareConfig, SourceMode


class Keithley2400Error(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KeithleyPreflight:
    identity: str
    source_mode: SourceMode
    source_setpoint: float
    output_enabled: bool
    voltage_v: float | None = None
    current_a: float | None = None
    compliance_limit: float | None = None
    source_range: float | None = None
    measure_range: float | None = None
    four_wire: bool | None = None
    status: str | None = None
    status_query_consumed: bool = False


@dataclass(frozen=True, slots=True)
class KeithleyReading:
    voltage_v: float
    current_a: float
    source_setpoint: float
    output_enabled: bool
    compliance_trip: bool
    near_compliance: bool
    status: str | None
    status_query_consumed: bool = False

    @property
    def resistance_ohm(self) -> float | None:
        if self.current_a == 0:
            return None
        return self.voltage_v / self.current_a


def open_keithley2400(
    role: str,
    config: SmuHardwareConfig,
) -> "QcodesKeithley2400":
    """Import and construct QCoDeS only after the caller authorizes connection."""

    candidates = (
        ("qcodes.instrument_drivers.Keithley", "Keithley2400"),
        ("qcodes.instrument_drivers.Keithley.Keithley_2400", "Keithley2400"),
        ("qcodes.instrument_drivers.Keithley.Keithley_2400", "Keithley_2400"),
    )
    failures: list[str] = []
    driver: Any = None
    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
            driver = getattr(module, class_name)
            break
        except (ImportError, AttributeError) as exc:
            failures.append(f"{module_name}.{class_name}: {exc}")
    if driver is None:
        raise Keithley2400Error(
            "Could not import a supported QCoDeS Keithley 2400 driver. "
            "Install the hardware extra in the target 'lyr' environment. "
            + " | ".join(failures)
        )
    unique_name = f"k2400_{role}_{time.time_ns()}"
    instrument = driver(unique_name, config.address)
    try:
        adapter = QcodesKeithley2400(role, instrument)
        adapter.set_timeout(config.timeout_ms)
        return adapter
    except Exception:
        instrument.close()
        raise


class QcodesKeithley2400:
    """Narrow, exception-transparent adapter for the QCoDeS Keithley 2400."""

    def __init__(self, role: str, instrument: Any) -> None:
        self.role = role
        self.instrument = instrument
        self.config: SmuHardwareConfig | None = None
        self._status_consumption_authorized = False

    def set_timeout(self, timeout_ms: int | None) -> None:
        if timeout_ms is None:
            raise Keithley2400Error(f"{self.role} timeout is not configured")
        timeout_parameter = getattr(self.instrument, "timeout", None)
        if callable(timeout_parameter):
            timeout_parameter(timeout_ms / 1000.0)
            return
        handle = getattr(self.instrument, "visa_handle", None)
        if handle is None:
            raise Keithley2400Error(
                f"{self.role} QCoDeS driver exposes no timeout control"
            )
        handle.timeout = timeout_ms

    def preflight(self) -> KeithleyPreflight:
        identity = self.ask("*IDN?").strip()
        if not identity:
            raise Keithley2400Error(f"{self.role} returned an empty *IDN?")
        raw_mode = self.ask(":SOUR:FUNC?").strip().strip('"').upper()
        if raw_mode.startswith("VOLT"):
            mode = SourceMode.VOLTAGE
        elif raw_mode.startswith("CURR"):
            mode = SourceMode.CURRENT
        else:
            raise Keithley2400Error(
                f"{self.role} returned unknown source mode {raw_mode!r}"
            )
        setpoint = self._query_source(mode)
        output = _parse_bool(self.ask(":OUTP?"), f"{self.role} output")
        values = _parse_float_list(self.ask(":READ?"))
        if len(values) < 2 or not all(math.isfinite(value) for value in values[:2]):
            raise Keithley2400Error(
                f"{self.role} :READ? did not return finite voltage/current"
            )
        source_function = "VOLT" if mode is SourceMode.VOLTAGE else "CURR"
        measure_function = "CURR" if mode is SourceMode.VOLTAGE else "VOLT"
        compliance = self._query_float(
            f":SENS:{measure_function}:PROT?", f"{self.role} compliance"
        )
        source_range = self._query_float(
            f":SOUR:{source_function}:RANG?", f"{self.role} source range"
        )
        measure_range = self._query_float(
            f":SENS:{measure_function}:RANG?", f"{self.role} measure range"
        )
        four_wire = _parse_bool(self.ask(":SYST:RSEN?"), f"{self.role} remote sense")
        status: str | None = None
        if self._status_consumption_authorized:
            status = self.ask(":SYST:ERR?").strip() or "0,No error"
        return KeithleyPreflight(
            identity=identity,
            source_mode=mode,
            source_setpoint=setpoint,
            output_enabled=output,
            voltage_v=values[0],
            current_a=values[1],
            compliance_limit=compliance,
            source_range=source_range,
            measure_range=measure_range,
            four_wire=four_wire,
            status=status,
            status_query_consumed=self._status_consumption_authorized,
        )

    def authorize_status_consumption(self) -> None:
        """Permit ``:SYST:ERR?`` during the subsequently audited run only."""

        self._status_consumption_authorized = True

    def zero_residual(self, mode: SourceMode) -> None:
        if mode is SourceMode.VOLTAGE:
            self.instrument.volt(0.0)
        else:
            self.instrument.curr(0.0)

    def configure(self, config: SmuHardwareConfig) -> None:
        self.config = config
        mode = "VOLT" if config.source_mode is SourceMode.VOLTAGE else "CURR"
        self.instrument.mode(mode)
        if config.source_mode is SourceMode.VOLTAGE:
            assert config.compliance_current_a is not None
            self.instrument.compliancei(config.compliance_current_a)
        else:
            assert config.compliance_voltage_v is not None
            self.instrument.compliancev(config.compliance_voltage_v)
        assert config.nplc is not None
        self.instrument.nplci(config.nplc)
        self.instrument.nplcv(config.nplc)
        source_function = "VOLT" if config.source_mode is SourceMode.VOLTAGE else "CURR"
        measure_function = "CURR" if config.source_mode is SourceMode.VOLTAGE else "VOLT"
        self.write(
            f":SOUR:{source_function}:RANG:AUTO "
            f"{'ON' if config.source_auto_range else 'OFF'}"
        )
        self.write(
            f":SENS:{measure_function}:RANG:AUTO "
            f"{'ON' if config.measure_auto_range else 'OFF'}"
        )
        self.write(f":SYST:RSEN {'ON' if config.four_wire else 'OFF'}")

    def set_source(self, value: float) -> None:
        config = self._require_configured()
        if config.source_mode is SourceMode.VOLTAGE:
            self.instrument.volt(float(value))
        else:
            self.instrument.curr(float(value))

    def set_output(self, enabled: bool) -> None:
        self.instrument.output("on" if enabled else "off")

    def read(self) -> KeithleyReading:
        config = self._require_configured()
        values = _parse_float_list(self.ask(":READ?"))
        if len(values) < 2:
            raise Keithley2400Error(
                f"{self.role} :READ? returned fewer than voltage and current"
            )
        voltage, current = values[:2]
        if not math.isfinite(voltage) or not math.isfinite(current):
            raise Keithley2400Error(f"{self.role} returned non-finite V/I readback")
        source = self._query_source(config.source_mode)
        output = _parse_bool(self.ask(":OUTP?"), f"{self.role} output")
        trip_command = (
            "SENS:CURR:PROT:TRIP?"
            if config.source_mode is SourceMode.VOLTAGE
            else "SENS:VOLT:PROT:TRIP?"
        )
        trip = _parse_bool(self.ask(trip_command), f"{self.role} compliance trip")
        status: str | None = None
        if self._status_consumption_authorized:
            error = self.ask(":SYST:ERR?").strip()
            status = error or "0,No error"
        near = (
            abs(current) >= 0.98 * float(config.compliance_current_a)
            if config.source_mode is SourceMode.VOLTAGE
            else abs(voltage) >= 0.98 * float(config.compliance_voltage_v)
        )
        return KeithleyReading(
            voltage_v=voltage,
            current_a=current,
            source_setpoint=source,
            output_enabled=output,
            compliance_trip=trip,
            near_compliance=near,
            status=status,
            status_query_consumed=self._status_consumption_authorized,
        )

    def close(self) -> None:
        self.instrument.close()

    def write(self, command: str) -> None:
        self.instrument.write(command)

    def ask(self, command: str) -> str:
        return str(self.instrument.ask(command))

    def _query_source(self, mode: SourceMode) -> float:
        command = ":SOUR:VOLT?" if mode is SourceMode.VOLTAGE else ":SOUR:CURR?"
        try:
            value = float(self.ask(command).strip())
        except ValueError as exc:
            raise Keithley2400Error(
                f"{self.role} returned invalid source readback"
            ) from exc
        if not math.isfinite(value):
            raise Keithley2400Error(f"{self.role} returned non-finite source readback")
        return value

    def _query_float(self, command: str, name: str) -> float:
        try:
            value = float(self.ask(command).strip())
        except ValueError as exc:
            raise Keithley2400Error(f"{name} returned invalid numeric value") from exc
        if not math.isfinite(value):
            raise Keithley2400Error(f"{name} returned non-finite numeric value")
        return value

    def _require_configured(self) -> SmuHardwareConfig:
        if self.config is None:
            raise Keithley2400Error(f"{self.role} has not been configured")
        return self.config


def _parse_float_list(raw: str) -> list[float]:
    values: list[float] = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if token:
            try:
                values.append(float(token))
            except ValueError as exc:
                raise Keithley2400Error(f"Invalid numeric response {raw!r}") from exc
    return values


def _parse_bool(raw: str, name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "on", "true"}:
        return True
    if normalized in {"0", "off", "false"}:
        return False
    try:
        number = float(normalized)
    except ValueError as exc:
        raise Keithley2400Error(f"{name} returned invalid boolean {raw!r}") from exc
    if number in {0.0, 1.0}:
        return bool(number)
    raise Keithley2400Error(f"{name} returned invalid boolean {raw!r}")
