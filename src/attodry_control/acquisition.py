from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Callable, Iterable

from .cleanup import CleanupReport
from .config import FieldEndPolicy, TemperatureInterruptPolicy
from .models import CryostatState
from .records import AttemptStatus, ExperimentCondition, RawStationSample
from .simulation import InterruptedAttemptState, SimulationStation
from .storage import RunStore


class RetryLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    run_id: str
    accepted_conditions: int
    rejected_attempts: int


class SimulationRunEngine:
    """Audited end-to-end executor that cannot construct real hardware."""

    def __init__(
        self,
        *,
        store: RunStore,
        run_id: str,
        station_factory: Callable[[], SimulationStation],
        max_attempts_per_condition: int = 2,
        normal_end_field_policy: FieldEndPolicy = FieldEndPolicy.HOLD,
        interrupt_policy: TemperatureInterruptPolicy = (
            TemperatureInterruptPolicy.ABORT
        ),
        confirmation: Callable[[str], str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not run_id.strip():
            raise ValueError("run_id must be non-empty.")
        if max_attempts_per_condition <= 0:
            raise ValueError("max_attempts_per_condition must be positive.")
        self.store = store
        self.run_id = run_id
        self.station_factory = station_factory
        self.max_attempts_per_condition = max_attempts_per_condition
        self.normal_end_field_policy = normal_end_field_policy
        self.interrupt_policy = interrupt_policy
        self.confirmation = confirmation
        self.now = now

    def start_new(
        self,
        conditions: Iterable[ExperimentCondition],
        *,
        config_snapshot: dict[str, object],
    ) -> ExecutionSummary:
        ordered = self._ordered_conditions(conditions)
        created = self.now()
        self.store.create_run(self.run_id, config_snapshot, created_at_utc=created)
        for condition in ordered:
            self.store.register_condition(self.run_id, condition)
        self.store.append_event(
            self.run_id,
            event_type="run_started",
            message="simulation acquisition started",
            payload={"condition_count": len(ordered)},
            created_at_utc=self.now(),
        )
        return self._execute(ordered)

    def resume(
        self, conditions: Iterable[ExperimentCondition]
    ) -> ExecutionSummary:
        ordered = self._ordered_conditions(conditions)
        recovered = self.store.reject_incomplete_attempts(
            self.run_id,
            completed_at_utc=self.now(),
        )
        pending = self.store.pending_condition_ids(self.run_id)
        self.store.append_event(
            self.run_id,
            event_type="run_resumed",
            message="simulation acquisition resumed",
            payload={
                "recovered_incomplete_attempts": recovered,
                "resume_strategy": "repeat-first-pending-condition",
                "resume_condition_id": pending[0] if pending else None,
            },
            created_at_utc=self.now(),
        )
        return self._execute(ordered)

    def _execute(
        self, conditions: tuple[ExperimentCondition, ...]
    ) -> ExecutionSummary:
        pending = set(self.store.pending_condition_ids(self.run_id))
        accepted_conditions = 0
        rejected_attempts = 0
        for condition in conditions:
            if condition.condition_id not in pending:
                continue
            accepted = False
            automatic_recoveries = 0
            attempts_allowed = self.max_attempts_per_condition
            attempts_used = 0
            confirmation_retry_used = False
            while attempts_used < attempts_allowed:
                attempts_used += 1
                started = self.now()
                attempt_index = self.store.start_attempt(
                    self.run_id,
                    condition.condition_id,
                    started_at_utc=started,
                )
                station = self.station_factory()
                try:
                    temperature_prequalified = (
                        self.store.has_temperature_qualification(
                            self.run_id, condition.temperature_k
                        )
                    )
                    if temperature_prequalified:
                        outcome = station.run_attempt(
                            condition,
                            attempt_index=attempt_index,
                            started_at_utc=started,
                            now=self.now,
                            temperature_prequalified=True,
                        )
                    else:
                        outcome = station.run_attempt(
                            condition,
                            attempt_index=attempt_index,
                            started_at_utc=started,
                            now=self.now,
                        )
                except KeyboardInterrupt as exc:
                    interrupted = getattr(exc, "attempt_state", None)
                    if isinstance(interrupted, InterruptedAttemptState):
                        for sample in interrupted.station_samples:
                            self.store.append_station_sample(self.run_id, sample)
                            self._save_temperature_qualification(condition, sample)
                        for raw in interrupted.raw_readings:
                            self.store.append_raw_reading(self.run_id, raw)
                    self.store.reject_incomplete_attempts(
                        self.run_id,
                        completed_at_utc=self.now(),
                        reason="operator interrupt; hardware cleanup attempted",
                    )
                    action = self._interrupt_action(automatic_recoveries)
                    self.store.append_event(
                        self.run_id,
                        event_type="run_interrupted",
                        message="operator interrupt; recovery decision recorded",
                        payload={
                            **_cleanup_audit_payload(
                                None if interrupted is None else interrupted.cleanup
                            ),
                            "policy": self.interrupt_policy.value,
                            "action": action,
                            "resume_strategy": "repeat-interrupted-condition",
                            "resume_condition_id": condition.condition_id,
                            "resume_attempt_index": attempt_index,
                        },
                        level="WARNING",
                        condition_id=condition.condition_id,
                        attempt_index=attempt_index,
                        created_at_utc=self.now(),
                    )
                    if action in {"continue", "continue-after-confirmation"}:
                        if (
                            action == "continue-after-confirmation"
                            and not confirmation_retry_used
                        ):
                            confirmation_retry_used = True
                            attempts_allowed += 1
                        rejected_attempts += 1
                        automatic_recoveries += 1
                        continue
                    self.store.set_run_status(self.run_id, "aborted")
                    raise

                if outcome.attempt.status is AttemptStatus.ACCEPTED:
                    shutdown = station.shutdown_normal(
                        zero_field=(
                            self.normal_end_field_policy is FieldEndPolicy.ZERO
                        ),
                        last_confirmed_cryostat_state=(
                            outcome.last_confirmed_cryostat_state
                        ),
                    )
                    outcome = replace(
                        outcome,
                        cleanup=shutdown,
                        last_confirmed_cryostat_state=(
                            shutdown.last_confirmed_cryostat_state
                        ),
                    )
                    if not shutdown.succeeded:
                        outcome = replace(
                            outcome,
                            attempt=replace(
                                outcome.attempt,
                                status=AttemptStatus.REJECTED,
                                rejection_reason=(
                                    "normal shutdown could not confirm electrical "
                                    "outputs safe"
                                ),
                            ),
                            accepted_result=None,
                            cleanup=shutdown,
                        )

                for station_sample in outcome.station_samples:
                    self._save_temperature_qualification(condition, station_sample)

                for station_sample in outcome.station_samples:
                    self.store.append_station_sample(self.run_id, station_sample)
                for raw in outcome.raw_readings:
                    self.store.append_raw_reading(self.run_id, raw)
                self.store.complete_attempt(self.run_id, outcome.attempt)
                self.store.append_event(
                    self.run_id,
                    event_type="attempt_completed",
                    message=f"attempt {outcome.attempt.status.value}",
                    payload={
                        "raw_reading_count": len(outcome.raw_readings),
                        **_cleanup_audit_payload(
                            outcome.cleanup,
                            fallback_state=outcome.last_confirmed_cryostat_state,
                        ),
                    },
                    level=(
                        "INFO"
                        if outcome.attempt.status is AttemptStatus.ACCEPTED
                        else "WARNING"
                    ),
                    condition_id=condition.condition_id,
                    attempt_index=attempt_index,
                    created_at_utc=self.now(),
                )
                if outcome.attempt.status is AttemptStatus.ACCEPTED:
                    accepted = True
                    accepted_conditions += 1
                    self.store.save_checkpoint(
                        self.run_id,
                        next_sequence_index=condition.sequence_index + 1,
                        updated_at_utc=self.now(),
                    )
                    break
                rejected_attempts += 1

            if not accepted:
                self.store.set_run_status(self.run_id, "failed")
                raise RetryLimitExceeded(
                    f"Condition {condition.condition_id!r} did not produce an "
                    f"accepted attempt after {self.max_attempts_per_condition} tries."
                )

        self.store.set_run_status(self.run_id, "complete")
        self.store.append_event(
            self.run_id,
            event_type="run_completed",
            message="all pending simulation conditions accepted",
            payload={
                "accepted_conditions": accepted_conditions,
                "rejected_attempts": rejected_attempts,
            },
            created_at_utc=self.now(),
        )
        return ExecutionSummary(
            run_id=self.run_id,
            accepted_conditions=accepted_conditions,
            rejected_attempts=rejected_attempts,
        )

    def _interrupt_action(self, automatic_recoveries: int) -> str:
        policy = self.interrupt_policy
        if policy is TemperatureInterruptPolicy.ABORT:
            return "abort"
        if (
            policy is TemperatureInterruptPolicy.CONTINUE
            and automatic_recoveries >= 1
        ):
            policy = TemperatureInterruptPolicy.WAIT_CONFIRMATION
        if policy is TemperatureInterruptPolicy.WAIT_CONFIRMATION:
            answerer = self.confirmation or input
            try:
                answer = answerer(
                    "Temperature/acquisition state was cleaned up. Repeat the "
                    "interrupted condition? [y/N] "
                )
            except KeyboardInterrupt:
                return "abort"
            if answer.strip().lower() not in {"y", "yes", "continue"}:
                return "abort"
            return "continue-after-confirmation"
        return "continue"

    def _save_temperature_qualification(
        self, condition: ExperimentCondition, station_sample: RawStationSample
    ) -> None:
        cryostat = station_sample.cryostat
        if (
            cryostat.temperature_control_enabled
            and cryostat.error_code == 0
            and abs(cryostat.user_temperature_k - condition.temperature_k) <= 1e-4
        ):
            self.store.save_temperature_qualification(
                self.run_id,
                target_k=condition.temperature_k,
                state=cryostat,
                qualified_at_utc=station_sample.captured_at_utc,
            )

    @staticmethod
    def _ordered_conditions(
        conditions: Iterable[ExperimentCondition],
    ) -> tuple[ExperimentCondition, ...]:
        ordered = tuple(sorted(conditions, key=lambda item: item.sequence_index))
        ids = [condition.condition_id for condition in ordered]
        indices = [condition.sequence_index for condition in ordered]
        if len(ids) != len(set(ids)) or len(indices) != len(set(indices)):
            raise ValueError("Condition IDs and sequence indices must be unique.")
        return ordered


def _cleanup_audit_payload(
    report: CleanupReport | None,
    *,
    fallback_state: CryostatState | None = None,
) -> dict[str, object]:
    state = fallback_state if report is None else report.last_confirmed_cryostat_state
    return {
        "cleanup_succeeded": None if report is None else report.succeeded,
        "field_zero_required": None if report is None else report.field_zero_required,
        "field_zero_confirmed": None if report is None else report.field_zero_confirmed,
        "last_confirmed_cryostat_state": (
            None if state is None else _cryostat_state_payload(state)
        ),
        "cleanup_events": (
            []
            if report is None
            else [
                {
                    "action": event.action.value,
                    "succeeded": event.succeeded,
                    "detail": event.detail,
                }
                for event in report.events
            ]
        ),
    }


def _cryostat_state_payload(state: CryostatState) -> dict[str, object]:
    return {
        "sample_temperature_k": state.sample_temperature_k,
        "user_temperature_k": state.user_temperature_k,
        "vti_temperature_k": state.vti_temperature_k,
        "bx_t": state.field.bx_t,
        "bz_t": state.field.bz_t,
        "bx_setpoint_t": state.field_setpoint.bx_t,
        "bz_setpoint_t": state.field_setpoint.bz_t,
        "temperature_control_enabled": state.temperature_control_enabled,
        "field_control_enabled": state.field_control_enabled,
        "error_code": state.error_code,
        "error_message": state.error_message,
    }
