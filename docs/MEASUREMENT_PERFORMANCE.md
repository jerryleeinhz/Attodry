# Measurement timing and verification

Review command counts, serial response ownership, qualification boundaries,
remaining waits, raw evidence and failure/recovery cost for every measurement
feature. Offline timings do not establish instrument settling or real throughput.

## External 6221 AC excitation (2026-11-13)

The D04 frequency serialization fix only adds an exact seven-significant-digit
comparison to the shared live/archive setting validator. It adds no I/O, audit
event, rewrite, retry, sleep or per-point verification. The original 17–7000 Hz
31-point grid stays unchanged, with source-setting readbacks kept separately.

V4 amplitude quantization is pure CPU work: compile before connection, send one
decimal amplitude command in the existing point transition, and validate existing
readbacks against that setting. Command/query budgets below are unchanged. There
is one extra `excitation_amplitude_quantized` audit event per changed source point,
no extra VISA command, no extra settle, no corrective rewrite and no retry loop.
Duplicate effective settings preserve original conditions and the existing source
transition policy. Analysis validates materialized source evidence without any
additional database query or instrument access. Fake transport tests exercise all
six 1–51 nA RMS targets, binary32/seven-digit serialization, range/limit boundaries,
whole-condition recovery and true configuration/protection failures.

Initial detector latches form an archived nonformal baseline before configuration.
This classification reuses the existing reads: no extra queries, writes or waits.
Instantaneous SR865A faults still reject, and post-settle qualification/cleanup
retain their normal checks. Historical latches cannot be relabeled during capture.

Optional current/frequency ranges use linear `points` or exact-dividing positive
`step`, and logarithmic `points`. Both compile only during file parsing, before
any resource connection. Decimal step validation and count checks precede allocation;
no per-point step calculation or extra source action is added. Generated points use
the existing immutable coordinate fields. No per-point runtime matcher, query, write, thread or wait is
added. Equivalent list/range configurations have identical fake driver traces;
recovery compares the expanded grid rather than the TOML syntax. Counts are
bounded before point allocation, including the Cartesian source-grid product.
Verification: 130 hardware-blocked tests passed; explicit lists, point counts and
steps have identical fake source write/query traces. No real timing claim is added.

The new source driver uses one serial owner with injected transport. Its command
budget is asserted by fake-transport tests: preflight 13 queries / 0 writes,
configuration 60 / 13, changed source point 62 / 5, unchanged point or formal readback
24 / 0, and cleanup 11 / 2. A formal sample group brackets its entire h1/h2 detector
capture with two source readbacks (48 queries); it does not repeat source setup
per harmonic. Source command/status/error responses remain auditable. No source
sleep, concurrent reads, OS process scans or additional instrument reader exists.
The v2 connection contract adds two queries per core state, two connection writes
once during configuration, and an immediate connection readback before WAVE setup.
It adds no settling sleeps or connection writes per harmonic/condition/cleanup.
V3 adds one serial dedicated interlock query per core state; no new writes or
waits. Raw direct evidence prevents the D04 COND-bit inference from rejecting a
closed interlock. Failed queries/events still end the attempt; no retry loop.

Changed points conservatively abort/verify OFF, program, arm and initiate the
waveform. Live amplitude/frequency update optimization is deferred until physical
commissioning establishes its behavior. Repeated coordinates require no source
write; recorded source visits still remain distinct. A source transition starts
the detector shared monotonic deadline after source verification (conservative
settling), final harmonic settings precede the remaining wait, and an already
settled qualification does not restart that deadline. XX and XY wait together
and read sequentially. Source-free detector cleanup runs only after source OFF.

The existing coordinator still qualifies temperature once per axis visit and
uses hard-protected temperature telemetry during later electrical samples. A
partial setting or source/lock failure ends the condition; resume repeats it as
a whole, retains earlier accepted conditions, and redoes fresh source preflight.
Analysis changes perform no instrument queries or waits. These budgets are
software call counts, not measured VISA latency or real scan-duration promises.

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

## External-source relock and stop acknowledgement budgets

Only a changed 6221 coordinate opens relock qualification. Each serialized poll
uses two SR830 and four SR865A status queries; successful lock adds mode, slope
and frequency checks for both roles (six queries). No writes are added. Without
observed unlock, elapsed settling time counts. With unlock, the shared deadline
resets once at lock confirmation: one full settle after lock, not two unconditional
settles. Repeated coordinates do not reopen latch history.

Relock has a 30 s / 301-poll cap with at most 100 ms between polls. A settled/formal
unlock stops rather than requalifying repeatedly. Stop ACK adds eleven core/error
queries per poll, limited to 2 s / 21 reads; immediate ACK keeps the existing eleven
queries. An in-flight VISA timeout is additional. Neither loop repeats source
commands. Raw polls/timestamps/deadlines and partial failures remain auditable.
The narrowly reviewed deferred-cleanup recovery adds one inactive-source settings
verification after detector setup; ordinary scans pay no recovery overhead.

## 6221 commissioning analysis snapshots (2026-11-13)

This analysis-only feature adds zero hardware queries, writes, waits, watchers
or acquisition retries. The run catalog reads only run metadata. Explicit Load
uses the existing execution-aware SQLite snapshot reader in one short read-only
transaction; decoding, native channel filtering, plotting and fitting occur
after the connection closes. One loaded snapshot feeds all plots and exports;
changing point exclusions never rereads a growing database. Grouping is indexed
by configuration/contract/frequency/repeat/outer visit. User-selected curve groups
are fitted once per render; report overlays reuse those fits. No database or
scan schema is migrated. Fake acquisition fixtures verify the same-run accepted
prefix across recovery; offline UI checks verify stale-result clearing and exports.
