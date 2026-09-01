from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Generator, Protocol
from uuid import uuid4

from .keithley2400 import (
    KeithleyConfigurationReadback,
    KeithleyPreflight,
    KeithleyReading,
    open_keithley2400,
)
from .three_smu_config import (
    FinishAction,
    SEMANTIC_ROLES,
    ScanPoint,
    SmuHardwareConfig,
    SourceMode,
    ThreeSmuHardwareConfig,
    ThreeSmuScanPlan,
    active_smu_roles,
    generate_scan_points,
    validate_plan_targets,
)


class ThreeSmuError(RuntimeError):
    pass


class ThreeSmuWriteNotAuthorized(ThreeSmuError):
    pass


class ThreeSmuSafetyError(ThreeSmuError):
    pass


class UnknownActiveOutput(ThreeSmuSafetyError):
    pass


class SmuAdapter(Protocol):
    role: str

    def preflight(self) -> KeithleyPreflight: ...

    def zero_residual(self, mode: Any) -> None: ...

    def configure(self, config: SmuHardwareConfig) -> KeithleyConfigurationReadback: ...

    def set_source(self, value: float) -> None: ...

    def set_output(self, enabled: bool) -> None: ...

    def read(self) -> KeithleyReading: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TimedReading:
    timestamp: str
    reading: KeithleyReading


@dataclass(frozen=True, slots=True)
class ThreeSmuSample:
    point_index: int
    repeat_index: int
    segment: str
    elapsed_s: float
    coordinates: dict[str, float]
    readings: dict[str, TimedReading]
    clean: bool
    problems: tuple[str, ...]


class ThreeSmuSession:
    """One safety engine shared by CLI and Notebook consumers."""

    def __init__(
        self,
        hardware: ThreeSmuHardwareConfig,
        plan: ThreeSmuScanPlan,
        adapters: dict[str, SmuAdapter],
        preflight: dict[str, KeithleyPreflight],
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hardware = hardware
        self.plan = plan
        self.adapters = adapters
        self.preflight = preflight
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_confirmed: dict[str, TimedReading] = {}
        self.last_commanded: dict[str, float] = {}
        self._active_roles = set(active_smu_roles(plan))
        self.output_enabled: dict[str, bool] = {
            role: False for role in self._active_roles
        }
        self.last_run_dir: Path | None = None
        self._configured: set[str] = set()
        self._run_active = False
        self._recorder: _RunRecorder | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        hardware: ThreeSmuHardwareConfig,
        plan: ThreeSmuScanPlan,
        *,
        authorize_writes: bool = False,
        authorize_status_consumption: bool = False,
        adapter_factory: Callable[[str, SmuHardwareConfig], SmuAdapter] = open_keithley2400,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "ThreeSmuSession":
        validate_plan_targets(hardware, plan)
        if not authorize_writes:
            raise ThreeSmuWriteNotAuthorized(
                "Three-SMU connection and writes require authorize_writes=True"
            )
        if not authorize_status_consumption:
            raise ThreeSmuWriteNotAuthorized(
                "Three-SMU runs require authorize_status_consumption=True because "
                "the Keithley error-queue query consumes status entries"
            )
        adapters: dict[str, SmuAdapter] = {}
        active_roles = active_smu_roles(plan)
        try:
            for role in active_roles:
                adapters[role] = adapter_factory(role, hardware.require_role(role))
            for adapter in adapters.values():
                authorize_status = getattr(adapter, "authorize_status_consumption", None)
                if callable(authorize_status):
                    authorize_status()
            preflight = {role: adapters[role].preflight() for role in active_roles}
            active = [
                role for role, state in preflight.items() if state.output_enabled
            ]
            if active:
                raise UnknownActiveOutput(
                    "Preflight found output already enabled on "
                    + ", ".join(active)
                    + "; no setting write was sent. Check those active SMUs manually."
                )
            identities = [state.identity.strip() for state in preflight.values()]
            if len(set(identities)) != len(identities):
                raise ThreeSmuSafetyError(
                    "Preflight identities are not distinct; no setting write was sent"
                )
            preflight_errors: list[str] = []
            for role, state in preflight.items():
                config = hardware.require_role(role)
                assert config.max_abs_voltage_v is not None
                assert config.max_abs_current_a is not None
                if state.source_mode is not config.source_mode:
                    preflight_errors.append(
                        f"{role} source mode is {state.source_mode.value}, expected "
                        f"{config.source_mode.value}"
                    )
                if not math.isfinite(state.source_setpoint):
                    preflight_errors.append(f"{role} source setpoint is not finite")
                if not state.status_query_consumed:
                    preflight_errors.append(
                        f"{role} status queue was not explicitly queried"
                    )
                elif not _status_is_clean(state.status):
                    preflight_errors.append(
                        f"{role} instrument status is not clean: {state.status}"
                    )
                if state.voltage_v is not None:
                    if not math.isfinite(state.voltage_v):
                        preflight_errors.append(
                            f"{role} voltage readback is not finite"
                        )
                    elif abs(state.voltage_v) > config.max_abs_voltage_v:
                        preflight_errors.append(
                            f"{role} voltage readback {state.voltage_v:g} V exceeds "
                            f"max_abs_voltage_v {config.max_abs_voltage_v:g} V"
                        )
                if state.current_a is not None:
                    if not math.isfinite(state.current_a):
                        preflight_errors.append(
                            f"{role} current readback is not finite"
                        )
                    elif abs(state.current_a) > config.max_abs_current_a:
                        preflight_errors.append(
                            f"{role} current readback {state.current_a:g} A exceeds "
                            f"max_abs_current_a {config.max_abs_current_a:g} A"
                        )
            if preflight_errors:
                raise ThreeSmuSafetyError(
                    "Preflight rejected unsafe or unexpected instrument state; no "
                    "setting write was sent: " + "; ".join(preflight_errors)
                )
            return cls(
                hardware,
                plan,
                adapters,
                preflight,
                sleep=sleep,
                monotonic=monotonic,
            )
        except Exception:
            for adapter in adapters.values():
                try:
                    adapter.close()
                except Exception:
                    pass
            raise

    def __enter__(self) -> "ThreeSmuSession":
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        if self._run_active:
            reason = (
                "context exited before scan generator completed"
                if exc is None
                else f"context exited with {type(exc).__name__}: {exc}"
            )
            self._abort_active_run(reason, interrupted=isinstance(exc, KeyboardInterrupt))
        if exc is None:
            self.close()
        else:
            try:
                self.close()
            except Exception:
                # Preserve the acquisition exception; cleanup audit already marks
                # any state that could not be confirmed.
                pass

    def run(
        self,
        *,
        output_dir: str | Path,
        run_name: str = "",
        note: str = "",
        config_path: str | Path | None = None,
    ) -> Generator[ThreeSmuSample, None, None]:
        if self._closed:
            raise ThreeSmuError("Session is closed")
        if self._run_active or self._recorder is not None:
            raise ThreeSmuError("A Three-SMU session supports one audited run")
        recorder = _RunRecorder(
            Path(output_dir),
            self.hardware,
            self.plan,
            self.preflight,
            run_name=run_name,
            note=note,
            config_path=None if config_path is None else Path(config_path),
        )
        self._recorder = recorder
        self.last_run_dir = recorder.run_dir
        self._run_active = True
        started = self.monotonic()
        completed = False
        try:
            self._configure(recorder)
            for point in generate_scan_points(self.plan):
                self._apply_point(point, recorder)
                if self.plan.delay_s:
                    self.sleep(self.plan.delay_s)
                for repeat_index in range(self.plan.samples_per_point):
                    sample = self._formal_sample(point, repeat_index, started, recorder)
                    recorder.sample(sample)
                    if not sample.clean:
                        raise ThreeSmuSafetyError("; ".join(sample.problems))
                    yield sample
                if point.post_delay_s:
                    self.sleep(point.post_delay_s)
            if self.plan.finish_action is FinishAction.ZERO_DISABLE:
                cleanup = self._cleanup(recorder, reason="normal completion")
                if cleanup["manual_verification_required"]:
                    message = (
                        "Normal-end cleanup could not confirm zero/output-off on "
                        "all active SMUs; check their front panels manually"
                    )
                    recorder.finalize("rejected", cleanup=cleanup, error=message)
                    self._run_active = False
                    raise ThreeSmuSafetyError(message)
            else:
                cleanup = {
                    "result": "authorized_hold",
                    "manual_verification_required": False,
                    "actions": [],
                }
                recorder.event("finish_hold", cleanup)
            recorder.finalize("completed", cleanup=cleanup)
            completed = True
        except GeneratorExit:
            self._abort_active_run("scan consumer closed generator", interrupted=True)
            raise
        except KeyboardInterrupt as exc:
            self._abort_active_run(str(exc) or "KeyboardInterrupt", interrupted=True)
            raise
        except BaseException as exc:
            self._abort_active_run(
                f"{type(exc).__name__}: {exc}",
                interrupted=False,
            )
            raise
        finally:
            if completed:
                self._run_active = False

    def close(self) -> None:
        if self._closed:
            return
        errors: list[str] = []
        for role, adapter in self.adapters.items():
            try:
                adapter.close()
            except Exception as exc:
                errors.append(f"{role}: {exc}")
        self._closed = True
        if errors:
            raise ThreeSmuError("Could not close all SMUs: " + "; ".join(errors))

    def _configure(self, recorder: "_RunRecorder") -> None:
        for role in active_smu_roles(self.plan):
            adapter = self.adapters[role]
            config = self.hardware.require_role(role)
            self._configured.add(role)
            adapter.zero_residual(self.preflight[role].source_mode)
            zero_state = adapter.preflight()
            zero_problems = self._preflight_problems(
                role,
                zero_state,
                expected_output=False,
                expected_source=0.0,
            )
            if zero_problems:
                recorder.event(
                    "preconfigure_zero_rejected",
                    {
                        "role": role,
                        "state": _jsonable(asdict(zero_state)),
                        "problems": zero_problems,
                    },
                )
                raise ThreeSmuSafetyError(
                    f"{role} residual-zero readback rejected: "
                    + "; ".join(zero_problems)
                )
            recorder.event(
                "preconfigure_zero",
                {"role": role, "state": _jsonable(asdict(zero_state))},
            )
            configuration = adapter.configure(config)
            adapter.set_source(0.0)
            self.last_commanded[role] = 0.0
            configured_state = adapter.preflight()
            problems = self._preflight_problems(
                role,
                configured_state,
                expected_output=False,
                expected_source=0.0,
            )
            recorder.event(
                "configure",
                {
                    "role": role,
                    "configuration_readback": _jsonable(asdict(configuration)),
                    "state": _jsonable(asdict(configured_state)),
                    "problems": problems,
                },
            )
            if problems:
                raise ThreeSmuSafetyError("; ".join(problems))
        for role in active_smu_roles(self.plan):
            adapter = self.adapters[role]
            adapter.set_output(True)
            self.output_enabled[role] = True
            reading = self._read_one(role)
            problems = self._reading_problems(
                role,
                reading.reading,
                expected_output=True,
            )
            recorder.event(
                "output_enable",
                {
                    "role": role,
                    "reading": _timed_reading_dict(reading),
                    "problems": problems,
                },
            )
            if problems:
                raise ThreeSmuSafetyError("; ".join(problems))

    def _apply_point(self, point: ScanPoint, recorder: "_RunRecorder") -> None:
        for role in active_smu_roles(self.plan):
            if role not in point.coordinates:
                continue
            target = point.coordinates[role]
            self._validate_source_target(role, target)
            self.adapters[role].set_source(target)
            self.last_commanded[role] = target
            recorder.event("source_set", {"role": role, "target": target})

    def _formal_sample(
        self,
        point: ScanPoint,
        repeat_index: int,
        started: float,
        recorder: "_RunRecorder",
    ) -> ThreeSmuSample:
        readings: dict[str, TimedReading] = {}
        problems: list[str] = []
        for role in active_smu_roles(self.plan):
            try:
                timed = self._read_one(role)
                readings[role] = timed
                problems.extend(
                    self._reading_problems(
                        role,
                        timed.reading,
                        expected_output=role in self._active_roles,
                    )
                )
            except Exception as exc:
                recorder.event(
                    "sample_partial",
                    {
                        "point_index": point.index,
                        "repeat_index": repeat_index,
                        "readings": {
                            role_name: _timed_reading_dict(value)
                            for role_name, value in readings.items()
                        },
                        "failed_role": role,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                raise
        return ThreeSmuSample(
            point_index=point.index,
            repeat_index=repeat_index,
            segment=point.segment,
            elapsed_s=self.monotonic() - started,
            coordinates=dict(point.coordinates),
            readings=readings,
            clean=not problems,
            problems=tuple(problems),
        )

    def _read_one(self, role: str) -> TimedReading:
        reading = self.adapters[role].read()
        timed = TimedReading(_now_iso(), reading)
        self.last_confirmed[role] = timed
        return timed

    def _reading_problems(
        self,
        role: str,
        reading: KeithleyReading,
        *,
        expected_output: bool,
    ) -> list[str]:
        config = self.hardware.require_role(role)
        problems: list[str] = []
        assert config.max_abs_voltage_v is not None
        assert config.max_abs_current_a is not None
        if reading.output_enabled != expected_output:
            problems.append(
                f"{role} output readback is {reading.output_enabled}, expected {expected_output}"
            )
        if not math.isfinite(reading.source_setpoint):
            problems.append(f"{role} source setpoint readback is not finite")
        if abs(reading.voltage_v) > config.max_abs_voltage_v:
            problems.append(
                f"{role} voltage {reading.voltage_v:g} V exceeds "
                f"max_abs_voltage_v {config.max_abs_voltage_v:g} V"
            )
        if abs(reading.current_a) > config.max_abs_current_a:
            problems.append(
                f"{role} current {reading.current_a:g} A exceeds "
                f"max_abs_current_a {config.max_abs_current_a:g} A"
            )
        if reading.compliance_trip:
            problems.append(f"{role} compliance trip")
        if not reading.status_query_consumed:
            problems.append(f"{role} status queue was not explicitly queried")
        elif not _status_is_clean(reading.status):
            problems.append(f"{role} instrument error: {reading.status}")
        return problems

    def _preflight_problems(
        self,
        role: str,
        state: KeithleyPreflight,
        *,
        expected_output: bool,
        expected_source: float | None = None,
    ) -> list[str]:
        config = self.hardware.require_role(role)
        problems: list[str] = []
        assert config.max_abs_voltage_v is not None
        assert config.max_abs_current_a is not None
        if state.output_enabled != expected_output:
            problems.append(
                f"{role} output readback is {state.output_enabled}, "
                f"expected {expected_output}"
            )
        if state.source_mode is not config.source_mode:
            problems.append(
                f"{role} source mode is {state.source_mode.value}, expected "
                f"{config.source_mode.value}"
            )
        if not math.isfinite(state.source_setpoint):
            problems.append(f"{role} source setpoint readback is not finite")
        elif expected_source is not None and state.source_setpoint != expected_source:
            problems.append(
                f"{role} source setpoint readback is {state.source_setpoint:g}, "
                f"expected {expected_source:g}"
            )
        if state.voltage_v is None:
            if state.output_enabled:
                problems.append(f"{role} voltage readback is unavailable")
        elif not math.isfinite(state.voltage_v):
            problems.append(f"{role} voltage readback is not finite")
        elif abs(state.voltage_v) > config.max_abs_voltage_v:
            problems.append(
                f"{role} voltage {state.voltage_v:g} V exceeds "
                f"max_abs_voltage_v {config.max_abs_voltage_v:g} V"
            )
        if state.current_a is None:
            if state.output_enabled:
                problems.append(f"{role} current readback is unavailable")
        elif not math.isfinite(state.current_a):
            problems.append(f"{role} current readback is not finite")
        elif abs(state.current_a) > config.max_abs_current_a:
            problems.append(
                f"{role} current {state.current_a:g} A exceeds "
                f"max_abs_current_a {config.max_abs_current_a:g} A"
            )
        if not state.status_query_consumed:
            problems.append(f"{role} status queue was not explicitly queried")
        elif not _status_is_clean(state.status):
            problems.append(f"{role} instrument error: {state.status}")
        return problems

    def _validate_source_target(self, role: str, target: float) -> None:
        if not math.isfinite(target):
            raise ThreeSmuSafetyError(f"{role} source target must be finite")
        config = self.hardware.require_role(role)
        limit = (
            config.max_abs_voltage_v
            if config.source_mode is SourceMode.VOLTAGE
            else config.max_abs_current_a
        )
        assert limit is not None
        if abs(target) > limit:
            raise ThreeSmuSafetyError(
                f"{role} source target {target:g} exceeds max_abs limit {limit:g}"
            )

    def _cleanup(self, recorder: "_RunRecorder", *, reason: str) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        cleanup_errors: list[dict[str, str]] = []
        manual = False
        for role in active_smu_roles(self.plan):
            if role not in self._configured:
                continue
            zero_readback_recorded = False
            try:
                self.adapters[role].set_source(0.0)
                self.last_commanded[role] = 0.0
                if self.output_enabled[role]:
                    if self.plan.delay_s:
                        self.sleep(self.plan.delay_s)
                    timed = self._read_one(role)
                    problems = self._reading_problems(
                        role,
                        timed.reading,
                        expected_output=True,
                    )
                    zero_payload = {"reading": _timed_reading_dict(timed)}
                else:
                    zero_state = self.adapters[role].preflight()
                    problems = self._preflight_problems(
                        role,
                        zero_state,
                        expected_output=False,
                        expected_source=0.0,
                    )
                    zero_payload = {"state": _jsonable(asdict(zero_state))}
                zero_readback_recorded = not problems
                recorder.event(
                    "cleanup_zero",
                    {
                        "role": role,
                        "target": 0.0,
                        **zero_payload,
                        "problems": problems,
                    },
                )
                if problems:
                    raise ThreeSmuSafetyError("; ".join(problems))
            except Exception as exc:
                manual = True
                cleanup_errors.append(
                    {
                        "role": role,
                        "stage": "zero",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            output_off_confirmed = False
            disabled_state: KeithleyPreflight | None = None
            try:
                self.adapters[role].set_output(False)
                self.output_enabled[role] = False
                disabled_state = self.adapters[role].preflight()
                problems = self._preflight_problems(
                    role,
                    disabled_state,
                    expected_output=False,
                    expected_source=0.0,
                )
                output_off_confirmed = not problems
                if problems:
                    manual = True
                    cleanup_errors.append(
                        {
                            "role": role,
                            "stage": "disable_readback",
                            "error": "; ".join(problems),
                        }
                    )
                recorder.event(
                    "cleanup_disable",
                    {
                        "role": role,
                        "state": _jsonable(asdict(disabled_state)),
                        "problems": problems,
                    },
                )
            except Exception as exc:
                manual = True
                cleanup_errors.append(
                    {
                        "role": role,
                        "stage": "disable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                recorder.event(
                    "cleanup_disable_error",
                    {"role": role, "error": f"{type(exc).__name__}: {exc}"},
                )
            actions.append(
                {
                    "role": role,
                    "zero_readback_recorded": zero_readback_recorded,
                    "output_off_confirmed": output_off_confirmed,
                    "final_state": (
                        None
                        if disabled_state is None
                        else _jsonable(asdict(disabled_state))
                    ),
                }
            )
        result = {
            "result": "manual_verification_required" if manual else "confirmed_safe",
            "reason": reason,
            "manual_verification_required": manual,
            "actions": actions,
            "cleanup_errors": cleanup_errors,
            "last_confirmed": {
                role: _timed_reading_dict(reading)
                for role, reading in self.last_confirmed.items()
            },
        }
        recorder.event("cleanup_complete", result)
        return result

    def _abort_active_run(self, reason: str, *, interrupted: bool) -> None:
        if not self._run_active or self._recorder is None:
            return
        recorder = self._recorder
        recorder.event("error", {"message": reason})
        cleanup = self._cleanup(recorder, reason=reason)
        recorder.finalize(
            "interrupted" if interrupted else "rejected",
            cleanup=cleanup,
            error=reason,
        )
        self._run_active = False


class _RunRecorder:
    def __init__(
        self,
        output_root: Path,
        hardware: ThreeSmuHardwareConfig,
        plan: ThreeSmuScanPlan,
        preflight: dict[str, KeithleyPreflight],
        *,
        run_name: str,
        note: str,
        config_path: Path | None,
    ) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = output_root / f"{stamp}_{uuid4().hex[:8]}"
        self.run_dir.mkdir()
        self.raw_path = self.run_dir / "raw.jsonl"
        self.csv_path = self.run_dir / "data.csv"
        self.metadata_path = self.run_dir / "metadata.json"
        self._raw = self.raw_path.open("w", encoding="utf-8")
        self._csv = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._csv, fieldnames=_csv_fields())
        self._writer.writeheader()
        self._closed = False
        active_roles = active_smu_roles(plan)
        self.metadata: dict[str, Any] = {
            "schema_version": 5,
            "code_version": _code_version(),
            "status": "running",
            "accepted": False,
            "started_at": _now_iso(),
            "hardware": {
                role: _jsonable(asdict(hardware.require_role(role)))
                for role in active_roles
            },
            "active_roles": list(active_roles),
            "off_roles": [
                role for role in SEMANTIC_ROLES if role not in active_roles
            ],
            "plan": _jsonable(asdict(plan)),
            "preflight": {
                role: _jsonable(asdict(state)) for role, state in preflight.items()
            },
            "requested": {
                "hardware": {
                    role: _jsonable(asdict(hardware.require_role(role)))
                    for role in active_roles
                },
                "plan": _jsonable(asdict(plan)),
            },
            "actual_preflight": {
                role: _jsonable(asdict(state)) for role, state in preflight.items()
            },
            "run_name": run_name,
            "note": note,
            "provenance": {
                "config_path": None if config_path is None else str(config_path.resolve()),
                "import_path": str(Path(__file__).resolve()),
                **_git_provenance(),
            },
        }
        self._write_metadata()
        self.event("start", {"run_dir": str(self.run_dir)})
        for role, state in preflight.items():
            self.event(
                "preflight",
                {"role": role, "state": _jsonable(asdict(state))},
            )

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        record = {
            "timestamp": _now_iso(),
            "event": event_type,
            "payload": _jsonable(payload),
        }
        self._raw.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._raw.flush()

    def sample(self, sample: ThreeSmuSample) -> None:
        record: dict[str, Any] = {
            "point_index": sample.point_index,
            "repeat_index": sample.repeat_index,
            "segment": sample.segment,
            "elapsed_s": sample.elapsed_s,
            "sample_clean": sample.clean,
            "problems": "; ".join(sample.problems),
        }
        for role in SEMANTIC_ROLES:
            if role in sample.coordinates:
                record[f"{role}_coordinate"] = sample.coordinates[role]
                record[f"{role}_requested_source"] = sample.coordinates[role]
            timed = sample.readings.get(role)
            if timed is None:
                continue
            reading = timed.reading
            record.update(
                {
                    f"{role}_timestamp": timed.timestamp,
                    f"{role}_source_setpoint": reading.source_setpoint,
                    f"{role}_voltage_v": reading.voltage_v,
                    f"{role}_current_a": reading.current_a,
                    f"{role}_resistance_ohm": reading.resistance_ohm,
                    f"{role}_output_enabled": reading.output_enabled,
                    f"{role}_compliance_trip": reading.compliance_trip,
                    f"{role}_status": reading.status,
                }
            )
        self._writer.writerow(record)
        self._csv.flush()
        self.event("sample", _sample_dict(sample))

    def finalize(
        self,
        status: str,
        *,
        cleanup: dict[str, Any],
        error: str | None = None,
    ) -> None:
        if self._closed:
            return
        self.metadata.update(
            {
                "status": status,
                "accepted": status == "completed",
                "ended_at": _now_iso(),
                "cleanup": _jsonable(cleanup),
                "error": error,
            }
        )
        self._write_metadata()
        self.event("run_finalized", {"status": status, "error": error})
        self._raw.close()
        self._csv.close()
        self._closed = True

    def _write_metadata(self) -> None:
        self.metadata_path.write_text(
            json.dumps(self.metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _status_is_clean(status: str | None) -> bool:
    if status is None:
        return False
    prefix = status.strip().split(",", 1)[0].strip()
    try:
        return int(float(prefix)) == 0
    except ValueError:
        return False


def _csv_fields() -> list[str]:
    fields = [
        "point_index",
        "repeat_index",
        "segment",
        "elapsed_s",
        "sample_clean",
        "problems",
    ]
    for role in SEMANTIC_ROLES:
        fields.extend(
            [
                f"{role}_coordinate",
                f"{role}_requested_source",
                f"{role}_timestamp",
                f"{role}_source_setpoint",
                f"{role}_voltage_v",
                f"{role}_current_a",
                f"{role}_resistance_ohm",
                f"{role}_output_enabled",
                f"{role}_compliance_trip",
                f"{role}_status",
            ]
        )
    return fields


def _sample_dict(sample: ThreeSmuSample) -> dict[str, Any]:
    return {
        "point_index": sample.point_index,
        "repeat_index": sample.repeat_index,
        "segment": sample.segment,
        "elapsed_s": sample.elapsed_s,
        "coordinates": sample.coordinates,
        "readings": {
            role: _timed_reading_dict(reading)
            for role, reading in sample.readings.items()
        },
        "clean": sample.clean,
        "problems": sample.problems,
    }


def _timed_reading_dict(timed: TimedReading) -> dict[str, Any]:
    return {
        "timestamp": timed.timestamp,
        **_jsonable(asdict(timed.reading)),
        "resistance_ohm": timed.reading.resistance_ohm,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _code_version() -> str:
    try:
        return version("attodry-transport-control")
    except PackageNotFoundError:
        return "0.1.0+uninstalled"


def _git_provenance() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
        return {"git_commit": commit, "git_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_dirty": None}
