"""Sequential four-channel temperature audit on an already-owned connection.

Callers persist ``start``'s raw state and validate it before calling ``finish``.
Query brackets describe host calls; they do not certify firmware refresh times.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import time
from typing import Callable
from uuid import uuid4

from .models import CryostatState


TEMPERATURE_TELEMETRY_POLICY = {
    "version": 1,
    "sampling": "sequential_on_owned_connection",
    "timestamps": "host_query_brackets_not_firmware_sample_times",
    "missing_power": "null_not_zero_or_previous_value",
}


class TemperatureTelemetryRecorder:
    """Link immutable raw temperature events to fresh/partial power events."""

    def __init__(self, driver, emit: Callable[[str, dict], None]) -> None:
        self.driver = driver
        self.emit = emit
        self.prefix = uuid4().hex
        self.sequence = 0
        self.last_record: dict | None = None

    def start(self, state: CryostatState, **metadata) -> dict:
        self.sequence += 1
        timing = (deepcopy(getattr(self.driver, "last_state_read_timing", None))
                  if getattr(self.driver, "last_confirmed_state", None) is state
                  else None)
        finished = timing or {}
        record = {
            "sample_id": f"{self.prefix}:{self.sequence}",
            "telemetry_version": 1,
            "captured_unix_s": (finished["finished_unix_s"] if "finished_unix_s" in finished
                                else time.time()),
            "captured_monotonic_s": (finished["finished_monotonic_s"] if "finished_monotonic_s" in finished
                                     else time.monotonic()),
            "state": asdict(state),
            "state_read_timing": timing,
            "heater_power": {"sample_w": None, "vti_w": None},
            "telemetry_complete": False,
            **metadata,
        }
        self.last_record = deepcopy(record)
        return record

    def finish(self, record: dict) -> dict:
        """Read powers after caller safety validation; persist even partial failure."""
        completed = deepcopy(record)
        completed.pop("event", None)
        # Reset per-attempt metadata so a failing alternate driver cannot expose
        # a previous sample's powers as a new observation.
        self.driver.last_heater_power_read_attempt = None
        try:
            powers = self.driver.read_heater_powers()
        except BaseException as exc:
            self._merge_attempt(completed)
            completed["heater_power_read_error"] = f"{type(exc).__name__}: {exc}"
            completed["telemetry_complete"] = False
            self.last_record = deepcopy(completed)
            try:
                self.emit("temperature_telemetry", completed)
            except BaseException as audit_error:
                exc.add_note(f"Temperature telemetry audit also failed: {audit_error}")
            raise
        self._merge_attempt(completed)
        completed["heater_power"] = asdict(powers)
        completed["telemetry_complete"] = True
        self.last_record = deepcopy(completed)
        self.emit("temperature_telemetry", completed)
        return completed

    def _merge_attempt(self, record: dict) -> None:
        attempt = getattr(self.driver, "last_heater_power_read_attempt", None)
        if isinstance(attempt, dict):
            record["heater_power"] = deepcopy(attempt["heater_power"])
            record["heater_power_read_timing"] = deepcopy(attempt["timing"])
            record["heater_power_reads"] = deepcopy(attempt["reads"])
        else:
            record["heater_power_read_timing"] = None
            record["heater_power_reads"] = {}
