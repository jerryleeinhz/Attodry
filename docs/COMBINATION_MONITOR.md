# Combination monitor: progress, phase and stored environment

The monitor reads one fixed run from SQLite. It uses the archived ordered plan
and committed records; it does not connect instruments, consume status latches,
inspect OS processes or load today's hardware TOML as measurement evidence.

```powershell
python -m attodry_control.combination_cli monitor --config config/hardware.local.toml
python -m attodry_control.combination_cli monitor --database PATH --run-id RUN_ID --once
```

`--json` returns the same structured evidence. The selected run is resolved once
when monitoring starts; a later run does not silently replace it.

## Progress and phases

The following is an illustration, not a current instrument state:

```text
Status: active | Accepted conditions: 4/342 | Process liveness: unknown
Temperature point: 2/18 | Fully completed in scope: 1/18 | target 25 K
Lock-in point: 2/19 | Fully completed in scope: 1/19
Point scope: temperature point 2
Formal samples in attempt: 2/3 | started (provisional until accepted)
Current phase (last recorded): temperature -> Axis qualification
```

- **Current point** is the one-based visit index, including duplicate values and
  return scans. Entering point two does not mean two points have completed.
- **Fully completed in scope** counts axis visits whose entire inner grid has
  accepted attempts and the expected number of formal samples. The scope is the
  current repeat and outer-axis index prefix.
- **Accepted conditions** counts accepted full conditions across execution
  segments. A started or rejected attempt never completes an axis visit.
- **Formal samples in attempt** is provisional until the full attempt is accepted.
- **Phase** is an explicit acquisition marker for preflight, applying a target,
  axis/condition qualification, Lock-in settling, formal sampling or cleanup.
  Raw and timing events do not overwrite the last phase marker.

Terminal status overrides an old active phase. Old records without explicit
markers report `unavailable`; an old database timestamp does not establish
process liveness, output state or a completed measurement.

## Current environment and last formal measurement

The latest sample temperature or Bx/Bz readback appears separately from the last
accepted formal measurement. It must belong to the current execution, repeat,
axis visit, condition and attempt, follow the current axis entry/temperature
command, and have valid acquisition and recording times. Another visit, recovery,
preflight, cleanup, corrupt/unknown record or terminal run is unavailable. A
previous formal temperature is never substituted for a missing current readback.

Qualification mode, target tolerance, stable range, dwell and timeout come from
the current execution's archived effective configuration. A reviewed recovery
therefore shows that segment's parameters rather than the original run's values.
The last accepted formal sample retains its own execution ID and per-module
capture times, including when it comes from an earlier segment. Rejected or
partial raw candidates cannot replace it; instrument transcripts are omitted
from this small projection.

The display distinguishes three kinds of time:

- **Age / elapsed observation time** is calculated from stored UTC for display.
- **Field sub-wait elapsed** is shown only when a recorded field sample matches
  the same visit, attempt, waypoint and exact readback.
- **Continuous stable-window accumulation / remaining qualification timeout**
  stays unknown unless the archive supplies that measurement. UTC age is not
  substituted for the acquisition controller's monotonic clock.

Text may refresh displayed environment ages every five seconds during active
temperature/magnetic application or qualification. JSON is emitted only when
stored evidence changes. No background acquisition watcher or automatic retry is
created by this command.

## Compatibility and cost

The same `--once` view displays **Measurement timing (stored summary)** after
the current execution has written its timing summary. The stage table reports
exclusive time, count and failed count. Elapsed, audit and unattributed time are
shown separately. Nested inclusive durations must not be added together.
`--json` exposes the same evidence under `measurement_timing_summary`, including
its recording UTC and scope. The scope ends at the last close stage and excludes
the summary's own write and later terminal bookkeeping. A running or legacy
execution without a summary does not inherit one from a previous segment.

Individual `measurement_stage_timing` audit events retain the module, sample,
parent stage and monotonic boundaries for detailed offline investigation.
These durations measure work already performed; they do not estimate remaining
thermal convergence time or shorten the existing acquisition waits.

The reader uses the existing short read-only SQLite transaction, including the
execution and accepted-attempt projections, and closes before rendering. It adds
no schema migration, instrument queries, setting writes or physical waits.
Explicit phase and timing markers add SQLite audit writes during new runs; they
do not make historical runs more certain or change their acceptance semantics.

The last measurement range profile remains visible with its original provenance;
after Lock-in cleanup starts it is marked superseded by cleanup. It is not a new
live range query.
