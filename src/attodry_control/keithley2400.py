from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import math
import re
import time
from typing import Any, Callable

from .three_smu_config import OffState, SmuHardwareConfig, SourceMode


KEITHLEY_2400_TIMEOUT_MS = 5000


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
    off_state: str | None = None


@dataclass(frozen=True, slots=True)
class KeithleyReading:
    voltage_v: float
    current_a: float
    source_setpoint: float
    output_enabled: bool
    compliance_trip: bool
    status: str | None
    status_query_consumed: bool = False

    @property
    def resistance_ohm(self) -> float | None:
        if self.current_a == 0:
            return None
        return self.voltage_v / self.current_a


@dataclass(frozen=True, slots=True)
class KeithleyMonitorReading:
    """One query-only Keithley 2400 state snapshot for the live monitor."""

    identity: str
    source_mode: SourceMode
    source_setpoint: float
    output_enabled: bool
    voltage_v: float | None
    current_a: float | None
    compliance_limit: float
    source_range: float
    measure_range: float
    four_wire: bool
    compliance_trip: bool | None
    status: str | None
    status_queue_consumed: bool
    off_state: str | None = None
    reading_origin: str | None = None
    measurement_freshness: str | None = None
    received_at_utc: str | None = None
    raw_response: str | None = None
    device_timestamp_s: float | None = None

    @property
    def resistance_ohm(self) -> float | None:
        if self.voltage_v is None or self.current_a in (None, 0):
            return None
        return self.voltage_v / self.current_a


@dataclass(frozen=True, slots=True)
class KeithleyConfigurationReadback:
    compliance_limit: float
    source_range: float
    measure_range: float
    measurement_functions: tuple[str, ...] = ()
    concurrent_measurement: bool = False
    read_elements: tuple[str, ...] = ()
    configuration_audit: tuple[dict[str, Any], ...] = ()
    off_state: str | None = None


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
        adapter.set_timeout(KEITHLEY_2400_TIMEOUT_MS)
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
        self.configuration_audit: list[dict[str, Any]] = []

    def set_timeout(self, timeout_ms: int) -> None:
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
        mode = _parse_source_mode(self.ask(":SOUR:FUNC?"), self.role)
        setpoint = self._query_source(mode)
        output = _parse_bool(self.ask(":OUTP?"), f"{self.role} output")
        voltage_v: float | None = None
        current_a: float | None = None
        if output:
            values = _parse_float_list(self.ask(":READ?"))
            if len(values) < 2 or not all(
                math.isfinite(value) for value in values[:2]
            ):
                raise Keithley2400Error(
                    f"{self.role} :READ? did not return finite voltage/current"
                )
            voltage_v, current_a = values[:2]
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
            voltage_v=voltage_v,
            current_a=current_a,
            compliance_limit=compliance,
            source_range=source_range,
            measure_range=measure_range,
            four_wire=four_wire,
            status=status,
            status_query_consumed=self._status_consumption_authorized,
            off_state=(_parse_off_state(self.ask(":OUTP:SMOD?"), self.role).value
                       if self.config is not None and self.config.off_state is not OffState.PRESERVE else None),
        )

    def authorize_status_consumption(self) -> None:
        """Permit ``:SYST:ERR?`` during the subsequently audited run only."""

        self._status_consumption_authorized = True

    def zero_residual(self, mode: SourceMode) -> None:
        if mode is SourceMode.VOLTAGE:
            self.instrument.volt(0.0)
        else:
            self.instrument.curr(0.0)

    def configure(self, config: SmuHardwareConfig) -> KeithleyConfigurationReadback:
        self.configuration_audit = []
        self.config = config
        try:
            return self._configure(config)
        except BaseException as exc:
            self.configuration_audit.append({
                "stage": "configuration_failed", "error": f"{type(exc).__name__}: {exc}",
                "captured_unix_s": time.time(),
            })
            raise

    def _configure(self, config: SmuHardwareConfig) -> KeithleyConfigurationReadback:
        if not self._status_consumption_authorized:
            raise Keithley2400Error("Configuration requires error-queue consumption authorization")
        step = self._configuration_call
        output = step("initial", ":OUTP?", lambda: _parse_bool(self.ask(":OUTP?"), "output"))
        initial_mode = step("initial", ":SOUR:FUNC?",
                            lambda: _parse_source_mode(self.ask(":SOUR:FUNC?"), self.role))
        source = step("initial", "source setpoint", lambda: self._query_source(initial_mode))
        if output or source != 0.0:
            raise Keithley2400Error(f"{self.role} configuration requires output OFF and zero source")
        self._require_configuration_errors_clean("initial")
        mode = "VOLT" if config.source_mode is SourceMode.VOLTAGE else "CURR"
        sense = "CURR" if mode == "VOLT" else "VOLT"
        requested = config.max_abs_current_a if sense == "CURR" else config.max_abs_voltage_v
        assert requested is not None and config.nplc is not None
        step("functions", f"source mode {mode}", lambda: self.instrument.mode(mode))
        # QCoDeS mode selects only the opposite sense function. Enable both so
        # READ? reports measured V/I rather than substituting a source setpoint.
        for command in (":SENS:FUNC:CONC ON", ":SENS:FUNC:OFF:ALL",
                        ':SENS:FUNC "VOLT","CURR"', ":FORM:ELEM VOLT,CURR"):
            step("functions", command, lambda command=command: self.write(command))
        self._require_configuration_errors_clean("functions")
        for name in ("nplci", "nplcv"):
            step("settings", f"{name} {config.nplc}",
                 lambda name=name: getattr(self.instrument, name)(config.nplc))
        command = f":SYST:RSEN {'ON' if config.four_wire else 'OFF'}"
        step("settings", command, lambda: self.write(command))
        self._require_configuration_errors_clean("settings")
        step("range_state", f":SENS:{sense}:PROT:RSYN?",
             lambda: _parse_bool(self.ask(f":SENS:{sense}:PROT:RSYN?"), "range sync"))
        self._configure_autorange(mode, sense, "initial_autorange")
        self._prepare_compliance_range(sense, requested, force=False)
        for attempt in range(2):
            stage = f"compliance_attempt_{attempt + 1}"
            self._require_configuration_idle(config.source_mode, stage)
            # QCoDeS' {:f} parameter rounds 100 nA to zero. Send and audit
            # the same round-trip-safe scientific literal through its transport.
            command = f":SENS:{sense}:PROT {requested:.17e}"
            step(stage, command, lambda: self.write(command))
            actual = step(stage, f":SENS:{sense}:PROT?",
                          lambda: self._query_float(f":SENS:{sense}:PROT?", "compliance"))
            errors = self._configuration_errors(stage)
            # Only the documented range-compatibility rejection is recoverable,
            # once, while OFF. Never retry communication or unrelated errors.
            if errors and attempt == 0 and all(code == 822 for code, _ in errors):
                self._prepare_compliance_range(sense, requested, force=True)
                continue
            if errors:
                raise Keithley2400Error(f"{self.role} {stage} instrument errors: {errors}")
            self._verify_compliance(actual, requested, sense)
            break
        self._configure_autorange(mode, sense, "final_autorange")
        compliance = step("final", f":SENS:{sense}:PROT?",
                          lambda: self._query_float(f":SENS:{sense}:PROT?", "compliance"))
        self._verify_compliance(compliance, requested, sense)
        source_range = step("final", f":SOUR:{mode}:RANG?",
                            lambda: self._query_float(f":SOUR:{mode}:RANG?", "source range"))
        measure_range = step("final", f":SENS:{sense}:RANG?",
                             lambda: self._query_float(f":SENS:{sense}:RANG?", "measure range"))
        if source_range <= 0 or measure_range <= 0:
            raise Keithley2400Error(f"{self.role} returned a non-positive range")
        if self._nominal_sense_range(sense, measure_range) * .001 > requested * (1 + 1e-9):
            raise Keithley2400Error(f"{self.role} final sense range is incompatible with compliance")
        functions, concurrent, elements = step("final", "verify V/I functions", self._verify_vi_measurement)
        off_state = None
        if config.off_state is not OffState.PRESERVE:
            self._require_configuration_idle(config.source_mode, "off_state")
            command = ":OUTP:SMOD " + _OFF_STATE_SCPI[config.off_state]
            step("off_state", command, lambda: self.write(command))
            actual_off_state = step("off_state", ":OUTP:SMOD?",
                                    lambda: _parse_off_state(self.ask(":OUTP:SMOD?"), self.role))
            if actual_off_state is not config.off_state:
                raise Keithley2400Error(f"{self.role} output-off state readback differs from configuration")
            self._require_configuration_idle(config.source_mode, "off_state_final")
            self._require_configuration_errors_clean("off_state")
            # ZERO retains voltage-source current compliance; require the original
            # configured protection to remain valid after the OFF-state write.
            if config.source_mode is SourceMode.VOLTAGE and config.off_state is OffState.ZERO:
                self._verify_compliance(self._query_float(":SENS:CURR:PROT?", "OFF ZERO compliance"), requested, sense)
            off_state = actual_off_state.value
        final_output = step("final", ":OUTP?", lambda: _parse_bool(self.ask(":OUTP?"), "output"))
        final_source = step("final", "source setpoint", lambda: self._query_source(config.source_mode))
        if final_output or final_source != 0.0:
            raise Keithley2400Error(f"{self.role} configuration changed zero/OFF state")
        self._require_configuration_errors_clean("final")
        return KeithleyConfigurationReadback(
            compliance_limit=compliance, source_range=source_range, measure_range=measure_range,
            measurement_functions=functions, concurrent_measurement=concurrent,
            read_elements=elements, configuration_audit=tuple(self.configuration_audit),
            off_state=off_state,
        )

    def _configuration_call(
        self, stage: str, request: str, operation: Callable[[], Any]
    ) -> Any:
        entry: dict[str, Any] = {"stage": stage, "request": request, "captured_unix_s": time.time()}
        self.configuration_audit.append(entry)
        try:
            result = operation()
            entry["readback"] = result.value if isinstance(result, SourceMode) else result
            return result
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise

    def _require_configuration_idle(self, mode: SourceMode, stage: str) -> None:
        output = self._configuration_call(
            stage, ":OUTP?", lambda: _parse_bool(self.ask(":OUTP?"), "output")
        )
        source = self._configuration_call(
            stage, "source setpoint", lambda: self._query_source(mode)
        )
        if output or source != 0.0:
            raise Keithley2400Error(
                f"{self.role} {stage} requires output OFF and zero source"
            )

    def _configuration_errors(self, stage: str) -> list[tuple[int, str]]:
        errors: list[tuple[int, str]] = []
        for _ in range(16):
            raw = self._configuration_call(stage, ":SYST:ERR?", lambda: self.ask(":SYST:ERR?"))
            try:
                code = int(raw.split(",", 1)[0].strip())
            except ValueError as exc:
                raise Keithley2400Error(f"{self.role} malformed error response {raw!r}") from exc
            if code == 0:
                return errors
            errors.append((code, raw))
        raise Keithley2400Error(f"{self.role} error queue did not terminate within 16 queries")

    def _require_configuration_errors_clean(self, stage: str) -> None:
        errors = self._configuration_errors(stage)
        if errors:
            raise Keithley2400Error(f"{self.role} {stage} instrument errors: {errors}")

    def _configure_autorange(self, mode: str, sense: str, stage: str) -> None:
        for prefix in (f":SOUR:{mode}", f":SENS:{sense}", f":SENS:{mode}"):
            command = prefix + ":RANG:AUTO ON"
            self._configuration_call(stage, command, lambda command=command: self.write(command))
        self._require_configuration_errors_clean(stage)
        for prefix in (f":SOUR:{mode}", f":SENS:{sense}", f":SENS:{mode}"):
            command = prefix + ":RANG:AUTO?"
            enabled = self._configuration_call(stage, command,
                lambda command=command: _parse_bool(self.ask(command), "autorange"))
            if not enabled:
                raise Keithley2400Error(f"{self.role} autorange was not confirmed: {prefix}")

    @staticmethod
    def _nominal_sense_range(sense: str, readback: float) -> float:
        # RANG? may report 105% overrange. The manual's 0.1% rule uses
        # the nominal discrete range, not its 1.05 overrange readback.
        ranges = (1e-6, 1e-5, 1e-4, 1e-3, .01, .1, 1.0) if sense == "CURR" else (.2, 2., 20., 200.)
        for value in ranges:
            if any(math.isclose(readback, value * factor, rel_tol=1e-9) for factor in (1., 1.05)):
                return value
        return readback  # Unknown positive ranges use the conservative readback.

    def _prepare_compliance_range(self, sense: str, requested: float, *, force: bool) -> None:
        step = self._configuration_call
        stage = "compliance_range_prepare"
        current = step(stage, f":SENS:{sense}:RANG?",
                       lambda: self._query_float(f":SENS:{sense}:RANG?", "sense range"))
        if current <= 0:
            raise Keithley2400Error(f"{self.role} returned non-positive sense range")
        if not force and self._nominal_sense_range(sense, current) * .001 <= requested * (1 + 1e-9):
            return
        mode = SourceMode.VOLTAGE if sense == "CURR" else SourceMode.CURRENT
        self._require_configuration_idle(mode, stage)
        minimum = 1e-6 if sense == "CURR" else .2
        command = f":SENS:{sense}:RANG {minimum:.12g}"
        step(stage, command, lambda: self.write(command))
        self._require_configuration_errors_clean(stage)
        actual = step(stage, f":SENS:{sense}:RANG?",
                      lambda: self._query_float(f":SENS:{sense}:RANG?", "prepared sense range"))
        if actual <= 0 or self._nominal_sense_range(sense, actual) * .001 > requested * (1 + 1e-9):
            raise Keithley2400Error(f"{self.role} could not prepare a compatible compliance range")

    def _verify_compliance(self, actual: float, requested: float, sense: str) -> None:
        if actual <= 0 or actual > requested * (1 + 1e-9):
            unit = "A" if sense == "CURR" else "V"
            raise Keithley2400Error(
                f"{self.role} compliance readback {actual:g} {unit} exceeds max_abs limit {requested:g} {unit}"
            )

    def _verify_vi_measurement(self) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
        concurrent = _parse_bool(
            self.ask(":SENS:FUNC:CONC?"), f"{self.role} V/I measurement concurrency"
        )
        functions = tuple(
            item.strip().strip('\"\'').upper().removesuffix(":DC")
            for item in self.ask(":SENS:FUNC?").split(",")
        )
        elements = tuple(item.strip().upper() for item in self.ask(":FORM:ELEM?").split(","))
        if (not concurrent or len(functions) != 2
                or set(functions) != {"VOLT", "CURR"} or elements != ("VOLT", "CURR")):
            raise Keithley2400Error(
                f"{self.role} V/I measurement not verified: "
                f"concurrent={concurrent}, functions={functions}, elements={elements}"
            )
        return functions, concurrent, elements

    def set_source(self, value: float) -> None:
        config = self._require_configured()
        value = float(value)
        if not math.isfinite(value):
            raise Keithley2400Error(f"{self.role} source target must be finite")
        limit = (
            config.max_abs_voltage_v
            if config.source_mode is SourceMode.VOLTAGE
            else config.max_abs_current_a
        )
        assert limit is not None
        if abs(value) > limit:
            raise Keithley2400Error(
                f"{self.role} source target {value:g} exceeds max_abs limit {limit:g}"
            )
        if config.source_mode is SourceMode.VOLTAGE:
            self.instrument.volt(value)
        else:
            self.instrument.curr(value)

    def set_output(self, enabled: bool) -> None:
        self.instrument.output("on" if enabled else "off")

    def read(self) -> KeithleyReading:
        config = self._require_configured()
        if not _parse_bool(self.ask(":OUTP?"), f"{self.role} output"):
            raise Keithley2400Error(f"{self.role} cannot measure V/I with output OFF")
        self._verify_vi_measurement()
        values = _parse_float_list(self.ask(":READ?"))
        if len(values) != 2:
            raise Keithley2400Error(
                f"{self.role} :READ? did not match verified VOLT,CURR format"
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
        return KeithleyReading(
            voltage_v=voltage,
            current_a=current,
            source_setpoint=source,
            output_enabled=output,
            compliance_trip=trip,
            status=status,
            status_query_consumed=self._status_consumption_authorized,
        )

    def read_off_zero_monitor(self, *, consume_status_queue: bool = False) -> KeithleyMonitorReading:
        """Retrieve only latest buffered V/I, never trigger or enable the source."""
        config = self._require_configured()
        if config.off_state is not OffState.ZERO or config.source_mode is not SourceMode.VOLTAGE:
            raise Keithley2400Error("OFF ZERO monitoring requires configured voltage source and off_state='zero'")
        if consume_status_queue and not self._status_consumption_authorized:
            raise Keithley2400Error("OFF ZERO status consumption was not authorized")
        adapter = VisaKeithley2400Monitor(self.role, self)
        return adapter.read_monitor(consume_status_queue=consume_status_queue, allow_off_zero=True)

    def query(self, command: str) -> str:
        """Share this owned transport with the query-only monitor adapter."""
        return self.ask(command)

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


class VisaKeithley2400Monitor:
    """Query-only VISA adapter used exclusively by ``monitor-live``.

    It deliberately has no setting-write method, no configure method, and no
    cleanup action.  Closing a monitor releases the VISA resource but does not
    change the instrument output or setpoint.
    """

    def __init__(self, role: str, resource: Any) -> None:
        self.role = role
        self.resource = resource

    def read_monitor(
        self, *, consume_status_queue: bool = False, allow_off_zero: bool = False
    ) -> KeithleyMonitorReading:
        identity = self.ask("*IDN?").strip()
        if not identity:
            raise Keithley2400Error(f"{self.role} returned an empty *IDN?")
        mode = _parse_source_mode(self.ask(":SOUR:FUNC?"), self.role)
        source = self._query_source(mode)
        output = _parse_bool(self.ask(":OUTP?"), f"{self.role} output")
        voltage_v: float | None = None
        current_a: float | None = None
        off_state = None
        raw_response = None
        received_at_utc = None
        device_timestamp_s = None
        if allow_off_zero:
            if output or mode is not SourceMode.VOLTAGE:
                raise Keithley2400Error(f"{self.role} OFF ZERO monitor requires output OFF in voltage mode")
            off_state = _parse_off_state(self.ask(":OUTP:SMOD?"), self.role)
            if off_state is not OffState.ZERO:
                raise Keithley2400Error(f"{self.role} OFF ZERO monitor requires output-off state ZERO")
            voltage_v, current_a, raw_response, device_timestamp_s = self._latest_off_zero_vi()
            received_at_utc = datetime.now(timezone.utc).isoformat()
        if output:
            values = _parse_float_list(self.ask(":READ?"))
            if len(values) < 2 or not all(
                math.isfinite(value) for value in values[:2]
            ):
                raise Keithley2400Error(
                    f"{self.role} :READ? did not return finite voltage/current"
                )
            voltage_v, current_a = values[:2]
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
        four_wire = _parse_bool(
            self.ask(":SYST:RSEN?"), f"{self.role} remote sense"
        )
        trip: bool | None = None
        if output or allow_off_zero:
            trip_command = (
                "SENS:CURR:PROT:TRIP?"
                if mode is SourceMode.VOLTAGE
                else "SENS:VOLT:PROT:TRIP?"
            )
            trip = _parse_bool(
                self.ask(trip_command), f"{self.role} compliance trip"
            )
        status = (
            self.ask(":SYST:ERR?").strip() or "0,No error"
            if consume_status_queue
            else None
        )
        if allow_off_zero:
            final_output = _parse_bool(self.ask(":OUTP?"), f"{self.role} final output")
            final_off_state = _parse_off_state(self.ask(":OUTP:SMOD?"), self.role)
            if final_output or final_off_state is not OffState.ZERO:
                raise Keithley2400Error(f"{self.role} output/OFF state changed during OFF ZERO monitoring")
        return KeithleyMonitorReading(
            identity=identity,
            source_mode=mode,
            source_setpoint=source,
            output_enabled=output,
            voltage_v=voltage_v,
            current_a=current_a,
            compliance_limit=compliance,
            source_range=source_range,
            measure_range=measure_range,
            four_wire=four_wire,
            compliance_trip=trip,
            status=status,
            status_queue_consumed=consume_status_queue,
            off_state=off_state.value if off_state is not None else None,
            reading_origin="latest_buffer" if allow_off_zero else None,
            measurement_freshness="unknown" if allow_off_zero else None,
            received_at_utc=received_at_utc,
            raw_response=raw_response,
            device_timestamp_s=device_timestamp_s,
        )

    def _latest_off_zero_vi(self) -> tuple[float, float, str, float | None]:
        concurrent = _parse_bool(self.ask(":SENS:FUNC:CONC?"), f"{self.role} V/I concurrency")
        functions = {item.strip().strip('\"\'').upper().removesuffix(":DC")
                     for item in self.ask(":SENS:FUNC?").split(",")}
        elements = tuple(item.strip().upper() for item in self.ask(":FORM:ELEM?").split(","))
        if (not concurrent or not {"VOLT", "CURR"} <= functions
                or not {"VOLT", "CURR"} <= set(elements) or len(elements) != len(set(elements))
                or not set(elements) <= {"VOLT", "CURR", "RES", "TIME", "STAT"}):
            raise Keithley2400Error(f"{self.role} OFF ZERO V/I measurement functions/format are not verified")
        raw = self.ask(":SENS:DATA?").strip()
        values = _parse_float_list(raw)
        if len(values) != len(elements):
            raise Keithley2400Error(f"{self.role} OFF ZERO buffered data does not match verified format")
        measured = dict(zip(elements, values, strict=True))
        for field in ("VOLT", "CURR"):
            if not math.isfinite(measured[field]) or abs(measured[field]) >= 9e37:
                raise Keithley2400Error(f"{self.role} OFF ZERO buffered {field} is unavailable/non-finite")
        timestamp = measured.get("TIME")
        if timestamp is not None and (not math.isfinite(timestamp) or timestamp < 0 or timestamp >= 9e37):
            timestamp = None
        return measured["VOLT"], measured["CURR"], raw, timestamp

    def close(self) -> None:
        self.resource.close()

    def ask(self, command: str) -> str:
        return str(self.resource.query(command))

    def _query_source(self, mode: SourceMode) -> float:
        command = ":SOUR:VOLT?" if mode is SourceMode.VOLTAGE else ":SOUR:CURR?"
        return self._query_float(command, f"{self.role} source readback")

    def _query_float(self, command: str, name: str) -> float:
        try:
            value = float(self.ask(command).strip())
        except ValueError as exc:
            raise Keithley2400Error(f"{name} returned invalid numeric value") from exc
        if not math.isfinite(value):
            raise Keithley2400Error(f"{name} returned non-finite numeric value")
        return value


def open_keithley2400_monitor(
    role: str,
    config: SmuHardwareConfig,
    resource_manager: Any,
) -> VisaKeithley2400Monitor:
    """Open one VISA resource for query-only live monitoring.

    Setting the local VISA timeout is not an instrument setting write.  This
    helper intentionally never imports QCoDeS or sends a SCPI setting command.
    """

    resource = resource_manager.open_resource(config.address)
    try:
        resource.timeout = KEITHLEY_2400_TIMEOUT_MS
        return VisaKeithley2400Monitor(role, resource)
    except Exception:
        resource.close()
        raise


def _parse_float_list(raw: str) -> list[float]:
    values: list[float] = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if token:
            try:
                values.append(float(token))
            except ValueError as exc:
                raise Keithley2400Error(f"Invalid numeric response {raw!r}") from exc
    return values


def _parse_source_mode(raw: str, role: str) -> SourceMode:
    normalized = raw.strip().strip('"').upper()
    if normalized.startswith("VOLT"):
        return SourceMode.VOLTAGE
    if normalized.startswith("CURR"):
        return SourceMode.CURRENT
    raise Keithley2400Error(f"{role} returned unknown source mode {normalized!r}")


_OFF_STATE_SCPI = {OffState.NORMAL: "NORM", OffState.ZERO: "ZERO",
                   OffState.HIGH_IMPEDANCE: "HIMP", OffState.GUARD: "GUAR"}


def _parse_off_state(raw: str, role: str) -> OffState:
    normalized = raw.strip().strip('\"').upper()
    for prefix, state in (("NORM", OffState.NORMAL), ("ZERO", OffState.ZERO),
                          ("HIMP", OffState.HIGH_IMPEDANCE), ("GUAR", OffState.GUARD)):
        if normalized in {prefix, {OffState.NORMAL:"NORMAL", OffState.ZERO:"ZERO",
                                  OffState.HIGH_IMPEDANCE:"HIMPEDANCE", OffState.GUARD:"GUARD"}[state]}:
            return state
    raise Keithley2400Error(f"{role} returned unknown output-off state {raw!r}")


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
