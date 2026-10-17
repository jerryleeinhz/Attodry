"""Monotonic phase timing for the single-owner measurement coordinator.

Stage duration is inclusive. Exclusive time subtracts immediate child stages and
audit callbacks performed at that level; only exclusive times can be summed
across nested stages. The timing event's own audit happens after its stage ends.
"""
from __future__ import annotations

from contextlib import contextmanager
import time

from .combination_store import utc_now


TIMING_POLICY = "monotonic-stages-exclusive-audit-v1"


class MeasurementTiming:
    def __init__(self, event, *, monotonic=None):
        self.event = event
        self.monotonic = monotonic or time.monotonic
        self.started = self.monotonic()
        self.stack = []
        self.sequence = 0
        self.audit_calls = 0
        self.audit_duration_s = 0.0
        self.audit_errors = []
        self.stages = {}

    def emit(self, kind, payload):
        """Measure the existing event callback without recursively auditing it."""
        started = self.monotonic()
        try:
            return self.event(kind, payload)
        except BaseException as error:
            self.audit_errors.append(f"{kind}: {type(error).__name__}: {error}")
            raise
        finally:
            elapsed = max(0.0, self.monotonic() - started)
            self.audit_calls += 1
            self.audit_duration_s += elapsed
            if self.stack:
                self.stack[-1]["audit_duration_s"] += elapsed

    @contextmanager
    def timed_stage(self, stage, **context):
        """Record successful, failed and interrupted blocks at the same boundary.

        A failed timing audit is raised after the operation has run. If the
        operation already failed, preserve that primary exception and attach the
        audit error as a note. Callers retain the audit errors for final cleanup.
        """
        self.sequence += 1
        frame = {**context, "stage_id": f"stage-{self.sequence:08d}",
                 "parent_id": self.stack[-1]["stage_id"] if self.stack else None,
                 "stage": stage, "policy": TIMING_POLICY,
                 "started_at_utc": utc_now(), "started_monotonic_s": self.monotonic(),
                 "children_duration_s": 0.0, "audit_duration_s": 0.0,
                 "completed": False}
        self.stack.append(frame)
        primary = None
        try:
            yield frame
            frame["completed"] = True
        except BaseException as error:
            primary = error
            frame["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            frame["finished_monotonic_s"] = self.monotonic()
            frame["finished_at_utc"] = utc_now()
            frame["duration_s"] = max(0.0,
                frame["finished_monotonic_s"] - frame["started_monotonic_s"])
            frame["exclusive_duration_s"] = max(0.0, frame["duration_s"]
                - frame["children_duration_s"] - frame["audit_duration_s"])
            self.stack.pop()
            if self.stack:
                self.stack[-1]["children_duration_s"] += frame["duration_s"]
            totals = self.stages.setdefault(stage, {"count": 0, "failed": 0,
                "inclusive_duration_s": 0.0, "exclusive_duration_s": 0.0})
            totals["count"] += 1
            totals["failed"] += int(not frame["completed"])
            totals["inclusive_duration_s"] += frame["duration_s"]
            totals["exclusive_duration_s"] += frame["exclusive_duration_s"]
            try:
                self.emit("measurement_stage_timing", frame)
            except BaseException as audit_error:
                if primary is None:
                    raise
                primary.add_note(f"Stage timing audit failed: {audit_error}")

    def summary(self):
        elapsed = max(0.0, self.monotonic() - self.started)
        exclusive = sum(stage["exclusive_duration_s"] for stage in self.stages.values())
        return {"policy": TIMING_POLICY, "elapsed_s": elapsed,
                "stage_count": self.sequence, "stages": self.stages,
                "exclusive_stage_duration_s": exclusive,
                "audit_calls": self.audit_calls, "audit_duration_s": self.audit_duration_s,
                "audit_errors": list(self.audit_errors),
                "unattributed_duration_s": max(0.0, elapsed - exclusive - self.audit_duration_s),
                "summation": "sum exclusive stage time plus audit time; never sum inclusive nested stages",
                "scope": "through the last completed stage; excludes writing this summary"}
