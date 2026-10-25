# Measurement timing and verification

Review command counts, serial response ownership, qualification boundaries,
remaining waits, raw evidence and failure/recovery cost for every measurement
feature. Offline timings do not establish instrument settling or real throughput.

## Recorded stages and read-only progress (2026-10-25)

The shared electrical coordinator reuses the published photonics
`MeasurementTiming` helper (`e96d4cd`). `measurement_stage_timing` records the
start/end, monotonic duration, parent stage and completed/error status for
preflight, range preparation, axis apply/qualification, condition qualification,
whole sample/window, individual module reads, sample storage, cleanup and close.
`measurement_timing_summary` retains stage totals and audit callback costs through
the final close stage; its own write and subsequent final run/quality bookkeeping
are outside that summary scope. Whole-condition recovery records a separate
summary in its own execution; monitor never presents an old segment's timing as
the new segment's result.

Inclusive stages nest. Only exclusive stage totals plus separately measured
audit time can be summed; parent/child inclusive durations must not be added.
This records actual time spent, without predicting convergence or changing any
settling duration. Successful, failed and interrupted blocks all retain an end
marker when the audit store is available. A timing/audit failure fails the
attempt, preserves the primary exception and never skips later independent
cleanup or connection close.

Explicit `scan_phase` start markers identify the operation in progress. The
existing positive Lock-in wait is bracketed by wait/resume phase records; no
additional delay or instrument query is inserted. Zero waits produce no wait
phase, and an interrupted wait does not claim that sampling resumed. Phase and
timing events add synchronous SQLite writes; they are not free. A simple
one-condition SMU/Lock-in fixture without a range schedule adds 12 phase events,
14 stage timing events and one timing summary, while its fake command sequence
and requested sleeps are identical with stage instrumentation disabled. Actual
event counts also depend on selected axes, profiles, samples and positive waits.

Monitor projects counts, archived requirements, current-visit environment and
the last accepted formal sample inside one short read-only transaction. It
does not load full sample histories or communication transcripts for display.
The text display may refresh evidence age every five seconds during a recorded
environment wait; JSON changes only when stored evidence changes. Neither
display creates an acquisition watcher, OS process inventory or hardware session.

Channel labels are archived metadata. They add no instrument commands, status
reads or waits. Analysis derives names from each observation's execution rather
than the current station TOML; unknown legacy labels remain unlabelled.

Fake clocks used for integration advance on simulated operations/sleeps, not
on reading the clock. Observation-based StepClock fixtures would otherwise
consume the safety timeout merely because duration instrumentation reads time.
Regression coverage compares complete fake action/sleep traces and verifies
deadline, cleanup and raw provenance independently.

## Existing combination timing

- Qualify temperature once when entering its axis point, before inner axes.
  Revisited indices, repeats and whole-condition recovery qualify afresh.
- Keep synchronous sample/VTI/heater telemetry and post-entry temperature quality
  flags. Hard bounds, overshoot, control/status and communication checks remain.
- Normal Lock-in acquisition shares one monotonic settling deadline per role.
  Source/reference and other-axis disturbances affect both detectors; elapsed
  thermal dwell or SMU delay counts. Detectors wait together and read serially.
- Settings, status, formal and cleanup queries on each connection have one
  response owner. No concurrent query thread or parallel instrument reader is
  introduced for performance or display.

## Outer-condition fixed sensitivity rules

Rules compile once before connecting hardware. The compiled outer visit and
complete condition bindings do not change the scan grid. Runtime selection is
an indexed lookup, never a threshold decision using actual readbacks.

The dual-SR830 profile adapter adds 18 setting queries per condition: eight for
the complete initial setting check, two explicit sensitivity readbacks and eight
for the complete final check. It sends at most one sensitivity write per changed
role. Native SR865A receiver-invariant verification adds its existing model
queries; SR830 query counts must not be applied to that model.

An unchanged physical profile still verifies settings, sends no sensitivity
write and does not reset the settling deadline. A changed profile restarts the
affected role's existing deadline; applying the profile itself adds no sleep.
Existing qualification supplements remaining time before formal capture.
Harmonic/frequency changes retain the profile and their existing guards.

Raw command attempts, partial readbacks, profile selection and verified settings
are retained. A partial application fails the condition; it is not automatically
retried and no mixed-state formal sample is acquired. Cleanup retains the
original baseline and its independent checks. Recovery reopens and verifies the
first pending condition with fresh environment qualification and range settings.

Analysis and accepted-record validation index archived bindings once per
execution, rather than scanning all condition bindings for each raw sample.
Read-only monitors use archived evidence and distinguish a measurement profile
from later cleanup; they send no extra instrument queries.

Fake-clock tests verify elapsed-time credit and deadline resets; fake transcripts
verify query/write counts, no writes for unchanged profiles, serial role order,
partial failures and immutable cleanup baselines. Real latency, saturation
recovery and signal quality need separately authorized bounded station testing.
No extra measurement-speed tuning is included with the range-rule feature.
