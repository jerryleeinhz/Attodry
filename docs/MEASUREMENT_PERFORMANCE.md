# Measurement timing and verification

## Temperature readback evidence (2026-11-19)

Each full snapshot still uses the same ten DLL calls in the same order. The
three temperature getters supply return codes and float32 evidence in the
existing timing object, with no separate success event or instrument retry.
Raw log records grow by the size of those fields. A failed combination
snapshot adds one audit event before cleanup; it never joins qualification or
formal samples. No writes, waits, asynchronous reads or control changes
result from the diagnostic.

## Record-only SR865A analog output status (2026-11-17)

The CH1/CH2 analog output classifier uses the existing four status replies and
pure CPU checks. It adds zero queries, writes, settling sleeps, status resets,
threads or retry budgets. Current/latched analog-only status follows the clean
control's qualification/capture flow without another 10TC requalification.
Raw output flags and default analysis exclusion remain intact. Input, digital
Data Channel scaling, unknown, lock and error decisions retain their guards.
New execution/status metadata bind archive interpretation; historical source
migration gates do not change.

## Fresh snapshot and fixed-role capture reuse (2026-11-16)

SR865A harmonic/sensitivity/time-constant compatibility fields now derive from
one fresh native receiver snapshot. Each mixed pair settings verification saves
four native queries (HARM, IVMD/SCAL, OFLT), retaining the native interpretation
checks, source-disconnected invariants and partial failure evidence. There is
no cross-call settings cache, new write or new wait.

Combination fixed singleton XX h1 / XY h2 capture can now obtain both selected
channels from one actual serial XX->XY pair. The new `fixed_role_single_pair_v1`
layout records scalar harmonic null and explicit `harmonics_by_role`; it does
not fabricate companion pairs or duplicate samples. Multi-harmonic selections,
bounded auto range and standalone capture retain their existing scheduling.
The original settling deadline, source OFF boundaries and formal native checks
remain. One normal mixed-role group removes a duplicate pair acquisition and
its environmental before/after bracket; no claim of simultaneous reads is made.

Only the immediately first environmental module may reuse the sample-entry
observation. Same-window/driver/state identity, unchanged contents, complete
telemetry and age <= min(1 s, environmental poll intervals) are required. It is
one-use, invalidated by intervening operations, and never duplicated in window
statistics. Old/stale/incomplete evidence causes a fresh capture. For the common
outer-environment/XXh1/XYh2 case, seven environment snapshots become four when
both optimizations apply; actual per-pair brackets and the final observation
remain. Temperature captures have ten state DLL calls plus two power reads;
magnetic-only captures have ten state calls. These are call-count savings,
not measured instrument throughput or a change to safety tolerances.

A two-condition fake comparison toggling only the shared-pair flag (fresh native
snapshot reuse present in both variants) reduces detector queries from XX
286 / XY 1273 to XX 196 / XY 809, a total of 554 fewer queries. Detector writes,
source query/write counts and requested wait trace are identical. Startup and
cleanup are included in those counts. The combined temperature fixture verifies
four distinct real environmental observations and unchanged selected-channel
counts; these fake budgets do not predict real VISA latency or convergence.

The existing FULL-synchronous raw audit remains enabled. No source live-update,
concurrent GPIB reader, changed dwell/TC, field-speed adjustment or historical
source-version recovery exception is introduced. Real wait-time findings and
the offline replay command are in TEMPERATURE_QUALIFICATION_TIMING_REVIEW.md.

Validation: the 979-case guarded broad run had two optional skips and exposed
legacy test-fixture assumptions; all affected cases and new paths pass the final
114-case guarded rerun (63.2 s). Historical source-transition gates are unchanged.

Review command counts, serial response ownership, qualification boundaries,
remaining waits, raw evidence and failure/recovery cost for every measurement
feature. Offline timings do not establish instrument settling or real throughput.

## OFF ZERO monitor and 6221 startup shutdown (2026-11-16)

The explicit OFF ZERO read path uses 16 queries per 2400 snapshot, or 17 with
opt-in error-queue consumption. These include identity/function/format and
protection checks, one latest-buffer query, and OFF/ZERO state brackets. It sends
zero instrument writes, trigger commands or source ON transitions. Standalone
monitor preparation does send authorized zero/configuration/OFF-mode writes once,
then reuses the same adapter/transport; each configured delay is between complete
monitor rounds. Buffer freshness remains unknown despite repeated requests.
Normal preserve-mode and ON-only scan paths retain their original read behavior.

Already-OFF/DC-zero 6221 preflight retains its existing query count and zero
writes. Healthy active/DC-residue takeover adds one ABOR, OFF and DC-zero write,
plus bounded OFF and zero acknowledgements (existing 2 s / 21-poll budget each).
There is no unconditional new sleep; only actual acknowledgement lag waits.
Archive protection before ABOR; repeated/unknown faults do not reset the budget
or permit INIT. No second instrument session or background monitor is introduced.
The final affected rerun passed 193 offline cases and 232 subtests in 83.09 s;
the broader pass also covered 753 cases before resolving its 11 failures, with
two skips. All original failures were rechecked. Raw startup proof remains JSON
serializable (no reference cycle), and the unchanged source-version guard is
tested against an explicitly historical 81-file fixture and rejects today's
additional modules. These timings are test costs, not real measurement speed.

## Unified plotting interactions (2026-11-16)

Notebook option changes borrow private snapshot rows without copying full nested
instrument histories. Full option updates select rows once for all filters;
column catalogs and unique values are reused until the selection or refresh
generation changes. Presentation changes invalidate only the current figure.
Rendering detaches the selected result once for independent edits and export.
Exact-value filtering uses sets for finite built-in scalars, preserving the
historical equality path for nonfinite or unhashable audit values. Dense colorbar
group checks traverse each key's identities once instead of once per curve.

The loader reads run plans/cleanup once and removes these repeated columns from
the sample JOIN. Native status and provenance remain complete; source validation
and channel quality are retained. Snapshot normalization uses builtin fast paths
with the identical digest and a generic Mapping fallback. SQLite connection
ownership and the short read-only transaction remain unchanged. No instrument
query/write, connection, settle, poll, retry or acquisition wait is introduced.

A local 100-row repetition of one archived sample measured median load time
0.866 s before and 0.606 s after (three repetitions), with identical rows and
snapshot digests. This is a representative synthetic analysis workload, not a
complete experiment, browser benchmark or measurement-throughput claim.
For a separate 1260-row repeated-sample UI comparison with two filters, full
option updates took 14.893 s before and 0.065 s after; changing the selected
filter took 14.955 s and 0.013 s respectively, retaining the same 630 observations.
Title invalidation took 14.557 s before and below 1 ms after. These single-action
local backend timings exclude browser transfer and figure rendering; ordinary
rendering still pays for detached selected output and Matplotlib drawing.
Verification: 182 relevant offline tests and 81 subtests passed in 59.89 s,
including 14 new interaction/equality/isolation/snapshot regressions. These tests
cover preview, audit, terminal-prefix, legacy/execution provenance, quality,
heatmaps, colorbars, statistics, exports and refresh behavior.

## Command-scoped magnetic boundaries (offline)

Readback mode and previous nonzero command context are derived from already
captured state and updated in memory. Nominal-boundary and inactive-axis checks
reuse the existing synchronous reads, audit events and exact-float32 transition
planner. Recovery applies the archived new policy to fresh state and its pending
path instead of substituting a universal 3 T ceiling. No extra instrument
connection, query/write, stability dwell, poll, fixed delay, background monitor,
status clearing or retry is introduced. Failures still use original cleanup and
retain all evidence; a strict-boundary rejection cannot claim a verified zero.
Accepted-prefix archive review replays the already recorded `cryostat_state`
register history once per execution. It caches that trace and uses time-indexed
lookup for formal brackets, checking zero-transition context against original
evidence. This adds one read-only SQLite query per new-policy execution during
archive review, with no extra acquisition I/O or changes to legacy review paths.

Validation spans 488 distinct hardware-blocked cases. The 306-case primary run
passed in 233.909 s. The 179-case supplementary run exposed two old universal-3 T/
two-start-path expectations; after updating them to the new versioned contract,
the affected recovery/tolerance set passed 32 cases in 53.847 s. All 36 new
boundary/context/ACK tests passed in 11.801 s; the scoped parser passed 55 config
cases in 0.184 s. These are offline regression costs, not station scan speed.

## Automatic dense-curve colorbars (2026-11-14)

Color selection operates on the same cached observations and prepared curve
statistics used for drawing. A shared color context counts actual drawable
groups, normalizes numeric coordinates or records curve-index identities, and
adds one Matplotlib colorbar for a dense plot, including four-channel cards.
The presentation change adds no database read, hardware call, acquisition wait,
background refresh, data interpolation or statistical aggregation. Existing
export uses the rendered snapshot and records the mapping in its report.
The 120 relevant hardware-blocked tests passed on 2026-11-15 in 26.229 s.
An offline 765-row,
51-group check measured about 19.3 ms for preparation and 4.01 ms median for the
prewarmed color context (10 repetitions); the context's first call included a
Matplotlib import. These local display timings are not acquisition throughput.

## No-write connection-preflight review (2026-11-14)

The exceptional recovery review reads a bounded global event tail and requires
the complete known preflight signature. It performs no instrument I/O or wait
and is repeated inside the existing atomic claim. After a proven no-write failure,
the existing deferred-resolution hook adds a fresh source OFF verification after
normal detector setup; it never starts a waveform or changes protection. Missing
events or any write/attempt/fault outside the reviewed signature block recovery.
Verification: 51 hardware-blocked cases passed in 49.435 s. No new polling,
instrument session, actuator write or retry loop is part of the file proof.

## Condition-gated 6221 output (2026-11-14)

Each external-source condition now has an explicit OFF bracket. The serial owner
reads/archives source status before stopping, sends ABOR/OFF only when needed,
and reuses the bounded OFF acknowledgement. This intentionally adds source I/O
at condition boundaries; it does not create a second session or background poll.
Source activation follows environmental qualification and starts the existing
full shared detector settle. All sample groups within one condition share that
active interval; no per-harmonic or per-sample stop/start is added.

An unchanged requested source point must still start afresh after the condition
OFF, so the old read-only shortcut cannot reuse a stopped waveform or stale
settling proof. Added lifecycle/phase/timing events preserve their real I/O and
audit cost. Protection failure ends the condition and keeps raw partial data;
it does not add a retry loop. Regression call counts and elapsed test time are
recorded with the verification result, not claimed as real station throughput.
Normal active stop uses 35 source queries and two writes; an already-OFF fresh
proof uses 24 queries and zero writes. There is no fixed extra sleep; only the
existing 2 s / 21-poll stop acknowledgement can wait for actual stop-bit lag.
The deferred-source range-context correction copies existing verified evidence
in memory and adds no hardware I/O or wait.

Verification: 297 distinct hardware-blocked regression cases passed, including
32 new cases. Final affected suites passed 78 cases in 86.087 s; final recovery/
atomic cases passed 40 in 32.188 s. Targeted source/range/fake-temperature cases
passed 16 in 24.801 s. These are offline timings, not measured station speed.

## SR865A External 200 ppm allowance (2026-11-14)

The native External relative consistency constant changes from 100 to 200 ppm;
the 5 mHz fundamental floor, existing serial queries/writes, three-observation
budget and 1 s recheck intervals do not change. A formerly rejected 117–121 ppm
pair can now proceed on its first observation and ordinary status verification,
avoiding its former extra observations/waits. Healthy in-bound observations add
no I/O, wait, background task or source action. Larger discrepancies retain the
same bounded rejection path and all status checks.

This is not a hardware accuracy or measured-throughput claim. Stored actual
thresholds/raw readings remain the audit basis; old records are not recomputed.
The user requested no deployment or restart of the currently active scan.

Verification: 295 hardware-blocked offline tests passed in 103.682 s, including
14 new cases. The first-read trace comparison confirms no extra I/O or wait for
newly accepted pairs; beyond-bound cases retain three observations/two 1 s waits.

## Pre-capture external coordinate re-observation (2026-11-13)

The shared pre-capture hook delegates to the original one-read implementation.
Normal external points retain their original coordinate/status/read counts and
waits. Only a typed frequency coordinate mismatch before formal capture adds
strict status/settings checks and at most two additional coordinate observations,
with two 1 s waits. Every query uses the existing serial owner; no setting write,
source command, extra session or repeat of acquired detector data is introduced.

Exceptional proofs retain actual coordinates, raw query times, wait/outcome and
failure evidence. Per-sample failed-proof guards prevent repeated calls from
resetting the observation budget. Existing source before/after formal brackets
still reject compliance or invalid source state. This bounded counter check does
not claim a new instrument accuracy or signal-settling performance.

Verification: 281 hardware-blocked offline regressions passed in 89.639 s,
including 17 new cases for the three-observation budget, unchanged normal I/O,
status/settings/source identity, audit-failure reuse, strict actual formal reads,
valid archives and compliance during the exceptional wait.

## Pending external reference frequency (2026-11-13)

The normal relock path retains its status/coordinate query order and existing
shared settling deadline. A typed early frequency mismatch can use the remaining
original 30 s / 301-poll budget for serial status/coordinate re-observations,
exceptional setting verification and the existing bounded poll wait. It adds no
source/detector writes or extra instrument owner. Persist every mismatch and
eventual success/timeout in the nonformal relock proof.

Strict target matching must succeed before the original full shared settle;
post-settle and formal mismatch behavior is unchanged. Persistent mismatches
exhaust one qualification window and fail rather than reopening it. Additional
query/audit cost is limited to affected transitions; an ordinary point gains no
new instrument query or wait. These bounds do not establish physical convergence.

Verification: 233 hardware-blocked offline regressions passed in 76.468 s,
including 16 new cases for pending/recovered observations, unchanged ordinary
I/O/waits, finite deadlines (including a late query return), raw archive integrity,
and source compliance during the exceptional wait.

## Recovered output-overload latch recheck (2026-11-13)

Ordinary external-source qualification reuses its existing status read and adds
no query, write or wait. Only a known-current-clean, output-only SR865A latch
under `record_continue`, after a controlled source change, may request one new
complete shared settling window. Both detectors wait together; their existing
serial owner then verifies coordinates, detector settings and strict native
status. There are no corrective setting writes, source commands, status-reset
commands or new instrument sessions. Persist raw candidate/recheck evidence,
requested wait and failure outcome, including an exhausted per-point budget.

The exceptional path costs at most one configured shared settle plus its bounded
verification queries and synchronous audit writes. A repeated fault ends the
attempt; it does not start a retry loop. The existing source before/after capture
checks still govern formal data. Software tests of command/wait budgets do not
establish physical convergence or real acquisition throughput.

Verification: 205 hardware-blocked offline regressions passed in 70.255 s,
including 16 new cases asserting normal-path I/O parity, one extra full shared
wait only for an eligible candidate, no corrective writes, strict final checks
and preserved source protection during the added wait.

## 6221 terminal-prefix fitting (2026-11-13)

Terminal-prefix selection uses the existing read-only snapshot. Accepted-record
integrity validation adds finite archive reads for terminal runs, including one
sample query per accepted condition, within that same transaction. Source checks
and fits add local CPU work for newly eligible samples; there is no background
poll, instrument query/write, settle, retry or acquisition change. Partial or
rejected attempts and native bad channels do not gain fit eligibility. Original
run/execution cleanup failures remain raw; plotting does not certify live safety.

Verification: 161 hardware-blocked offline regressions passed, including 17 new
terminal-prefix cases. These timings do not establish real acquisition throughput.

## LK_setup working-copy cleanup (2026-11-13)

Recording existing detector allowlists and ignoring generated artifacts adds no
instrument query/write, qualification, settle, retry or acquisition database I/O.
Personal Notebook copies retain explicit analysis parameters without changing
shared fit defaults. Verification: 118 hardware-blocked offline configuration,
range, mixed-detector and settings tests passed. This maintenance does not provide
new real measurement timing evidence or authorize a hardware run.

## Unified filter stacks and exclusions (2026-11-13)

Filter stack resolution and readable exclusion labels operate on the existing
materialized plotting snapshot. Changing Keep values, stack checkboxes, labels
or manual exclusions adds no database query, instrument call, acquisition wait,
retry or background poll. Source loading and explicit refresh keep their existing
read-only transaction boundaries. Group normalization and exclusion bookkeeping
add only local CPU work; exact observation identities and repeat counts remain
separate from their human-readable labels. Verification: 139 instrument-blocked
offline tests passed, including cached-source query-count checks and 21 new
grouping/UI regressions. This is not a real acquisition-speed measurement.

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
