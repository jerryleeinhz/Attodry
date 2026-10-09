# Hardware and safety guide

## User-approved continuous illumination and gate transitions (2026-10-06)

The operator approves optical qualification before a gate loop and small-step gate
voltage transitions; the follow-up authorization permits direct gate jumps.
Explicit `combination_scan.illumination_policy="continuous_gate_scan"`
requires optical -> smu -> lockin, the existing PEM topology, one fixed excitation
point, lit power-stabilized PM measurement and only voltage-source gate roles
with optional per-channel ramps. A continuous direct gate requires explicit
`zero_readback_tolerance_v`, finite/positive and within its physical voltage limit.
Bias, current/pulse or environment axes are rejected offline. Omission retains
OFF-before-every-axis behavior.

Prepare/select the fixed electrical point while dark, then qualify the light
before the first gate write. Inner gate changes preserve illumination; later
fixed-lockin axis applications verify/reuse the setting. Per-gate electrical
settling, fresh power guards, reference checks and all formal sample brackets
remain. No target-following correction occurs inside the gate loop. Each optical
index and repeat gets its own qualification; optical changes and global cleanup
remain OFF-first.

Optional `three_smu_run.gate_bottom.ramp` / `gate_top.ramp` carries explicit
max_step_v, step_interval_s, readback_tolerance_v and timeout_s, all positive and
finite, with tolerance below half a step. These are transition/verification
settings, not replacement sample V/I limits. Every transition starts from fresh
sensed voltage, records attempted writes and validates sensed V/I, output,
current compliance and errors at each intermediate step. Held-light guards
continue during steps. Intermediates are audit records, not formal grid points.
Small discrete steps do not guarantee current-peak or hardware slew bounds.

All observed SMU compliance/limit, unknown-status and communication faults abort;
lock-in continuation policies never excuse a gate fault. Guarded direct changes
use fresh sensed V/I, source, output, compliance and error checks before their
single target write and after the shared delay, with held optical guards.
The post-write source register must equal the requested target; mismatch is a
hard failure, with actual V/I retained. No new actual-voltage target tolerance
is inferred from the separate cleanup zero tolerance.
Normal direct return-to-zero uses one zero request and fresh measured-zero
verification within the explicit tolerance before certification and disable;
configured ramps retain their measured-zero verification. Failed/untrusted
transitions prioritize output disable, preserve last confirmed values and
uncertified actual-zero state; source-zero/OFF alone is not a discharged-capacitor
certificate. Legacy non-continuous direct/no-ramp behavior is retained.
Current station direct mode carries the prior 0.05 V zero tolerance and preserves
the 10 uA current limit. No current limit is silently widened. This user-approved
exception supersedes the earlier unconditional laser-OFF/direct-point statements
only for the opted-in path. Offline/target-offline verification is not real
illuminated commissioning.


## User-approved post-tuning optical target deviation (2026-10-06)

An additional explicit `power_feedback.target_mapping = "cartesian"` expands
each approved optical setting row across the validated target-power array.
Default `paired` retains one-to-one mapping. Expansion happens offline, is
limited to 10000 optical points and preserves all source/filter/PEM constraints.
Changing the number of requested conditions does not authorize higher power,
electrical excitation or different routing. Preview exposes the expanded plan.

The operator approves approximate target tuning followed by acquisition at
actual measured power. Optional `optical_scan.target_deviation_policy` is
`abort` (omitted default) or `record_continue`. Initial target tolerance,
window/hold qualification, bounded feedback and actuator stepping are unchanged.
Only subsequent target mismatch becomes informational, including post-tuning
dwell, requalification, formal brackets and reference-recovery monitoring.
Hold optical settings; no target-following writes inside electrical acquisition.
Actual watts, requested target, deviation and independently timestamped power
brackets are retained. Off-target alone does not invalidate Vxx/Vxy.

This exception does not cover hard power limits, reduction thresholds, minimum
valid illumination, PM saturation/invalid readbacks, settings, identity,
communication, reference or cleanup faults. The common optical point session
also supplies PEM diagnostic guards; that path uses the same explicit policy.
The standalone optical retuning loop rejects the non-default policy before I/O.
Historical data remains unchanged. See the photonics combination guide.

## Isolated-XX PEM internal-reference diagnostic (2026-10-06)

The user-selected SR865A diagnostic is a separate, explicitly authorized
development path. Its real entry requires confirmed physical disconnection
of XX SINE OUT from the sample, an available/unarmed XY capture buffer and
the optical authorization/route flags. Software 4 mV protection is not
physical isolation. The station's NKT power/current, PEM, PM and model limits
remain unchanged; original combination reference guards are not bypassed.

Prepare approved internal frequency while XY is still external before
switching reference source. Unknown, communication, settings, identity,
power and ownership/cleanup faults stop the run. Existing confirmed overload
and unlock record/continue policies retain invalid raw diagnostic buffers.
Check optical guards across each buffer, turn light OFF before analysis,
protect XX, restore XY, then finish PEM and close all resources. Every failed
cleanup action stays in the final audit even if a later action succeeds.

Internal clocks supply no PEM phase or signed Hall response. R has positive
noise bias, and a longer wait cannot recover amplitude suppressed by an
inappropriate low-pass time constant. No automatic inverse-filter correction
or conversion from measured voltage to photocurrent is performed. Full
operator instructions: [PEM_INTERNAL_DIAGNOSTIC_GUIDE.md](PEM_INTERNAL_DIAGNOSTIC_GUIDE.md).

## Bounded reference-transient recovery (2026-10-05)

The operator approves `reference_transient_policy = "wait_stable"` and a new
illuminated scan with the existing approved settings. Omission defaults to
`abort`. Only identified SR830 RANGE (bit 4), valid finite reference readings
outside the selected interval, reference-pair/PEM mismatch and SR865A detection
frequency mismatch may enter recovery. This does not prove a query glitch.
Actual configuration changes, SR830 TC bit 5, SR865A configuration/filter faults,
unknown status, communication, instrument and optical/environment faults retain
hard failures. No frequency, power, source or field limit is widened.

Recovery requires three consecutive one-second good polls, real lock evidence,
verified settings and the configured filter settling. A single 45 s deadline
covers recovery and retries for the failed operation; another anomaly cannot
reset it. Optical settings and approved electrical outputs remain held while
fresh power/state checks continue. No feedback retune or optical timeout reset
is authorized by recovery. A contaminated sample is retained as rejected audit,
then the entire before/all-modules/after window is reacquired. Existing pure
unlock/overload record-continue and default analysis invalidity remain intact.

## User-selected absolute photonics reference tolerance (2026-10-05)

The user explicitly removes the software-only
`pair_tolerance_hz <= reference_min_hz * 0.001` profile ceiling. The configured
tolerance must still be a positive finite number and is the sole difference
threshold for XX/XY/PEM fundamental-reference comparisons and SR865A
`FREQDET / harmonic` versus reference readbacks in this photonics path.
Actual reference intervals and model/harmonic detection limits remain enforced;
frequency disagreement beyond the configured tolerance still aborts, independently
of the status-only reference-unlock continuation policy. Source, power, field,
identity/settings and cleanup protection retain their existing contracts.

## User-authorized photonics reference-unlock continuation (2026-10-05)

The current user explicitly authorizes
`photonics_lockin.reference_unlock_policy = "record_continue"` on XX and XY,
then another illuminated scan. This supersedes the unlock-only rejection below
for this opted-in photonics profile; omission still defaults to `abort` and
ordinary electrical acquisition is unchanged. Current and consumed latched
reference-unlock events remain recorded in setup, transitions, qualification
and formal sampling. Any unlock within a formal pair or its probe brackets
invalidates both Vxx/Vxy roles because they depend on the common reference chain;
raw diagnostic data is retained but default analysis excludes the affected pair.
The finite 1 s/45 s reference wait records unlock timeout and continues only
under this explicit policy; frequency range/pair consistency, model capabilities,
identity/settings, source amplitude/DC, unknown status, PON, synchronous-filter,
instrument/communication and optical power checks keep their existing guards.
No sample-source, current, power or magnetic-field limit changes. Separate
overload policy remains responsible only for overload faults.

## User-authorized photonics overload continuation (2026-10-05)

For the current illuminated photonics scan, the user explicitly authorizes
`photonics_lockin.overload_policy = "record_continue"` for both XX SR830 and
XY SR865A. This scoped exception also covers overload observations during
reference polling, setup, transitions and qualification. Raw current/latched
status is retained; affected formal readings are invalid for analysis, while
an unaffected companion role remains usable. Missing policy defaults to `abort`.
Reference unlock still waits only in an expected reference transition and fails
in formal acquisition. Communication, identity/settings, unknown status, PON,
instrument errors, SR865A synchronous-filter faults, frequency, excitation and
optical power checks retain their original guards. No range or power limit is
increased. Real acquisition is explicitly authorized in the current session;
the historical read-only commissioning notes below describe earlier stages.

## New photonics profile precedence (updated 2026-10-05; offline implemented)

Current user-confirmed wiring supersedes the earlier arrangement below for the
explicit `pem_xy_xx_sine` profile: PEM REF OUT -> XY SR865A REF IN -> XY SINE OUT+
-> XX SR830 REF IN; XX SINE OUT excites the sample. Both sine outputs are
physically connected, but only XX reaches the sample. XX uses sine zero crossing,
not a TTL edge. XY source amplitude/DC and unused BlazeX mode are verified and
preserved without writes; source protection/cleanup applies to XX. No external
50 ohm sample termination is present; the user also confirms the total sample/
series-resistor load is much larger than 50 ohm (`high_impedance`). Instrument
SLVL settings still do not establish the actual sample voltage.
New-profile input shield grounding is explicitly configured as float or ground;
the historical profile retains its float requirement. This stage authorizes
read-only identity/settings queries, not output changes or illuminated acquisition.
Single sequential frequency/settings readbacks are not complete lock acceptance.

The current user request creates a separate
[photonics / nonlinear Hall work package](modules/INTEGRATION_PHOTONICS_NONLINEAR_HALL.md).
For this project, preserve `sqrt(Bx^2+Bz^2) <= 3 T`, including pure-axis operation.
The historical single-Z 9 T policy and readback margins below do not authorize
their use in the new profile. The new combination profile now applies this
strict invariant to plans, transitions, actual readbacks and cleanup, with
offline boundary tests. Real integrated commissioning remains pending.

The user explicitly selects PEM REF OUT -> **external-reference lockin_xx** ->
XX sync output -> external-reference lockin_xy for this profile. XX remains the
SINE OUT excitation source/Vxx channel and XY SINE OUT remains disconnected.
The ordinary internal-reference XX contract below and its existing scan code
remain unchanged. Do not apply those setup/sweep paths to the new topology.
The user subsequently confirmed SR865A for XX, with XY still SR830, and reports
manual PEM-to-XX lock. This explicitly selected SR865A is a narrow exception to
the historical excluded-model rule for the new photonics profile. Replacing XY
with SR865A is only being considered. The approved adapter and point session
are wired into the explicit `pem_xx_xy` combination profile; local fake tests
do not certify real software acquisition or the complete physical cascade.
Its BlazeX sync mode, source wiring/load/DC, independent input range and output
scale must be verified; SR830 source/command/status assumptions cannot carry over.
PEM-profile setup and cleanup must preserve external frequency ownership.
The current task implements unified TOML and optical/electrical combination
without connecting to instruments. Source wiring/load, zero DC/mode and allowed
amplitude/cleanup values are explicit validated inputs. Fixed 24 dB/oct RC
filters require at least 10 time constants; model input and output ranges are
separate. All preflights precede settings, laser OFF precedes axis changes, and
every PEM adjustment requires XX protection then reference requalification
before restoring excitation/emission. Cleanup disables PEM only after explicit
verified XX source protection; unknown output leaves the reference active or
unknown, closes communication and requires manual verification. Formal power
drift or invalid/unknown lock status rejects the point. See the
[photonics guide](PHOTONICS_COMBINATION_SCAN_GUIDE.md).
Earlier SR830 diagnostic authorization is not treated as authorization for new
SR865A commissioning, NKT emission or full optical/electrical/environment scans.
The SR865A manual describes sync as 2.5 V on printed page 62 and +/-2 V or
0–2 V on printed page 98. Neither establishes the SR830's reliable >3.5 V TTL
high condition. Do not infer XX-to-XY synchronization from PEM-to-XX success.
Keep source/load/trigger observations and verify both stages independently.

Historical dual-SR830 P1 diagnosis completed but TTL synchronization acceptance failed: XX
remained unlocked even after PEM activation/stability. SR830 baseline restoration
was verified; PEM disable ACK is not physical-off proof. The second diagnostic
queried NKT OFF before/after with no SDK writes. No automatic retry or unreviewed
trigger/level-conditioning change; see the
[commissioning evidence](PEM_REFERENCE_COMMISSIONING_20261004.md).

## Opt-in SR830 overload acquisition policy (2026-09-29)

At the user's request, `lockin_sweep.overload_policy` may explicitly allow
continued acquisition with invalid overload readings. `abort` remains the
default. `continue_unselected` only permits an unselected instrument's overload;
`record_continue` retains selected overloaded samples for diagnostic use.
Neither option excuses communication, reference unlock, configuration/readback,
excitation/environment or magnetic-limit failures. Initial preflight/setup and
final cleanup retain their strict checks. No inferred zero after communication
loss, automatic retry, or increased limit/range/reserve is introduced.

Keep raw faults and explicit per-role validity. Acquisition completion is not
proof of clean measurements. See [operation details](LOCKIN_DAILY_OPERATION.md)
and [high-impedance XX attenuation notes](LOCKIN_XX_ATTENUATION.md).

## Magnet coordinates and limits

The operator explicitly approved a separate software readback-policy change on
2026-09-30. The following rules supersede the previous exact-zero **readback**
classification; strict nominal command limits remain. This implementation has
offline validation only. It is not factory accuracy certification, station
deployment, authorization to replay failed scans, or real commissioning.

The active software coordinate system is X/Z:

- Z is the 9 T axial coil in the factory system sheet.
- X is the 3 T transverse coil called Y in the factory system sheet.
- No mechanical rotator is controlled.

Nominal requests and exact float32 commands use the configured X/Z limits,
bounded by the 2026-09-11 X 3 T / Z 9 T envelope. The 2026-09-30 combination
follow-up removes its extra single-Z 3 T cap; whole-plan vector targets remain
within the configured resultant limit (at most 3 T):

```text
abs(Bx) <= 3 T
abs(Bz) <= 9 T
if Bx != 0 and Bz != 0: sqrt(Bx^2 + Bz^2) <= 3 T
```

The current explicit `readback_tolerance_t` configuration archives
`planned-axis-readback-v3`. It retains the following complete-plan classification;
legacy `field_tolerance_t` configurations and v2 records retain their original rules.

`planned-axis-readback-v2` fixes mode from the **complete requested point list**:

- `single_x`: at least one nonzero X target; every Z target exactly zero.
- `single_z`: at least one nonzero Z target; every X target exactly zero.
- `vector`: all other plans, including a fixed nonzero other axis, an X-to-Z
  switch across separate points, and an all-zero plan. Every vector-plan target
  satisfies the nominal resultant limit, even pure-axis endpoints.

The legacy v2 actual readback limits are separate from command limits:

| Mode | Actual readback acceptance | Other axis |
|---|---|---|
| Single X | abs(Bx) <= configured X limit + 0.0005 T | abs(Bz) <=0.0005 T |
| Single Z | abs(Bz) <= configured Z limit + margin | abs(Bx) <=0.0005 T |
| Vector | each axis within its configured limit + margin; hypot(Bx,Bz) <= configured vector limit + 0.0005 T | Both raw axes retained |

Axis margin is 0.0005 T only for nominal limits <=3 T; larger Z limits,
including 9 T, get no margin. Thus 3 T becomes 3.0005 T **for actual readbacks**.
Pure-Z combination plans may request 4/9 T within their configured Z ceiling.
Reduced configured limits are retained
and receive the same fixed readback margin. The exact DLL binary32 representation
of 0.0005 T is accepted at the inactive-axis boundary; its effective threshold
(0.0005000000237487257 T) is archived. No readback is rounded to zero.

Preflight, each monitored read, mixed command/actual corner, formal-window data
and hold use the same run-wide mode. Current/error/control/stability/timeout and
non-finite-value protections remain. Raw Bx/Bz, calculated norm, mode, nominal
limits, effective thresholds and per-sample readback assessment are recorded.
Nonzero inactive readbacks and nominal-limit excursions inside the accepted
margin are explicit audit flags. An inactive-axis value beyond the bound still
aborts; it does not silently switch the run to vector mode.

The fixed 0.5 mT boundary margin is the operator's software acceptance policy,
not a manufacturer-specified worst-case residual-field error. New configuration:

```toml
[magnet]
readback_tolerance_t = 0.0015
setpoint_ack_tolerance_t = 0.0001
```

Replace `field_tolerance_t` with `readback_tolerance_t`; providing both rejects.
The unified positive tolerance, capped at the approved 1.5 mT, is used for each
actual-axis target error, arrival dwell, formal sampling and hold, and the
single-axis inactive readback guard. Verified zero instead requires the **actual
vector norm** <= that same value for the full stable dwell. X=Z=1.2 mT is within
each component tolerance but its 1.70 mT norm cannot certify zero. Both raw axes
are preserved. This does not extend nominal command limits, the 0.5 mT active-axis/
vector boundary margin, or the unextended high-Z ceiling. Actual ramp samples are
not rejected merely for distance to the destination; arrival waits for convergence.

Setpoint acknowledgement is a separate component comparison, default and maximum
0.1 mT, with the existing 30 s command timeout. It confirms controller register
receipt, not physical arrival. Actual stability timeout remains independently
configured per wait. Exact float32 command bits and strict endpoint/corner/step
validation remain; register quantization never changes the requested command.
Attempts/results archive both tolerances and confirmed setpoint errors.
Legacy `field_tolerance_t` remains capped at 1 mT and retains the v2 inactive
guard. Old saved driver acknowledgement protocols remain unchanged.
Generic diagnostics without a complete magnetic plan retain strict
legacy checks. Historical records without the new version retain their archived
rules; unknown or inconsistent declarations cannot certify completion.

A high-Z-to-X plan is vector mode and cannot include a >3 T endpoint. Command
endpoints and both candidate component-write corners remain strictly validated
after float32 conversion. Reject unsafe transitions, never silently switch to
`via_zero`. Setpoint acknowledgement does not prove the other coil has ramped
down: check the latest complete actual readback and each mixed command/actual
corner using the declared readback policy before writes. Existing cleanup gates
remain; failed communication cannot certify zero. Magnet temperature/readiness,
factory charging parameters and APS100 sweep rates are unchanged. Discrete
checks do not establish a continuous physical trajectory.

Combination snapshots now declare `planned-axis-configured-v2`; historical
`universal-3T` acquisitions retain their archived envelope. Owned zero/disable
recovery for a pure-Z plan can read up to the factory 9 T Z ceiling while still
requiring X setpoint zero and actual X within the independent 0.5 mT guard.
Other magnetic modes retain the recovery vector envelope of 3.0005 T and strict
3 T setpoints. Recovery cannot resume scanning or certify zero after unknown
communication, disabled field control or out-of-envelope readbacks.

Angle is reported relative to +Z:

```text
theta_deg = atan2(Bx, Bz) * 180 / pi
```

The sign-to-physical-direction convention must be confirmed during staged hardware commissioning and stored in the station snapshot.

## Field control

The legacy attoDRY interface uses a USB virtual COM port and a vendor `attoDRYxyz64bit.dll`. The driver must:

- verify the exact DLL path and architecture;
- check every DLL return code;
- wait for device initialization with a timeout;
- read current field, setpoint, control state, and error state before any write;
- implement idempotent `ensure_field_control(enabled)` by read-then-toggle only when required;
- require an explicit field-transition policy: `direct` segments the adjacent
  vector transition, while `via_zero` requests the conservative zero detour;
  software must never insert a zero detour transparently;
- convert every command endpoint to IEEE-754 binary32 before validating it, and
  validate the endpoint plus both possible X-then-Z and Z-then-X mixed corners
  against the single-axis/dual-axis envelope above; check both candidates and
  execute only an order whose intermediate corner is safe;
- choose only a verified mixed-corner write order for each waypoint, and retain
  the complete planned and executed waypoint path rather than assuming X-first;
- record both component setpoints and readbacks, with a durable pre-command
  attempt and post-acknowledgement result for each field-control toggle, changed
  X/Z component command, and sweep-to-zero command; every component command must
  include its exact float32 bits;
- never infer zero after a failed read.

Changing both components may produce a transient path that differs from the requested direction. Constant-direction or constant-magnitude ramps therefore require coordinated intermediate vector points and readback verification; setting X and Z independently once is not sufficient to promise the path.

`isZeroingField`, vendor action/error strings, and vendor logs may be retained as
optional diagnostics only. Verified zero requires the project checks on the
confirmed zero setpoint, actual Bx/Bz, enabled control, clear error state, and the
configured dwell; a diagnostic flag or log message cannot substitute for any of
those checks.

## Temperature stability

The reference programs demonstrated useful continuous checks but did not include timeout/error handling. The project definition of stable is:

- control is enabled;
- error code is clear;
- all samples in the configured dwell window are within the setpoint tolerance;
- the maximum minus minimum readback in that window is below the configured stable range;
- the wait has not exceeded its timeout.

All tolerances and dwell periods are configuration values and are stored with the run.

## Dual SR830 wiring

`lockin_xx`:

- internal reference;
- SINE OUT drives the sample excitation path;
- measures Vxx.

`lockin_xy`:

- external reference from `lockin_xx` TTL OUT;
- measures Vxy;
- SINE OUT is physically disconnected;
- software sets its output level to the known minimum but must not treat that as an electrical disconnect.

Before acquisition, diagnostics must verify distinct VISA addresses, both IDNs, reference modes, frequency consistency, harmonic, lock state, overload state, time constant, sensitivity, and source readback.

The pre-integration laboratory procedure is
[`DUAL_SR830_DEVICE_TEST.md`](DUAL_SR830_DEVICE_TEST.md). Its setting-write path
requires explicit authorization and confirmation that `lockin_xy` SINE OUT is
physically disconnected. The ordinary diagnostic path sends queries only;
reading `LIAS?` or `ERRS?` is separately opted in because those queries consume
latched status bits.

The user-approved daily combination CLI policy (2026-09-30) treats invoking
combination_cli run as authorization for its validated selected modules, including
status consumption. No repeated RUN prompt or mandatory CLI authorization flags.
When Lock-in is selected, TOML lockin_xy.sine_output_connected=false is the
operator's wiring declaration; true rejects before connection. Backend entry
points retain explicit authorization guards. Configuration/safety hash rechecks,
preflight, nominal <=3 T targets, declared readback bounds, error handling and cleanup remain.
describe-hardware is offline; monitor reads only saved files/SQLite and never
connects instruments. Removing a redundant prompt is not a new commissioning
authorization or permission to deploy/run experiments through this development task.

## Exception handling

The agreed default for a caught exception or `Ctrl+C` is zero field. Cleanup order is electrical outputs first, then magnet zero request, then final state logging and disconnect.

APS100 hardware power-fail, quench, and shutdown-input behavior remains an independent protection layer. It does not guarantee that a Python crash returns the magnet to zero.

## Gate SMUs

The Three-SMU hardware contract defines up to three Keithley 2400 semantic roles and gives
`smu_bias`, `gate_top`, and `gate_bottom` independent `max_abs_voltage_v` and
`max_abs_current_a` boundaries. Every requested source value is checked before a
write, and every actual voltage/current readback is checked against both limits.

The scan-plan `role` is the only enable state. A `fixed` or `sweep` role requires
its complete same-name hardware table. An `off` role may omit that table and is
not parsed, opened, queried, written, cleaned up, or recorded. Its physical
source/output state is therefore unknown, never inferred to be zero or off. The
three hardware roles use one flat table shape; the Three-SMU VISA timeout is a
fixed 5000 ms and is not an operator-configurable safety value.

An off run-plan role is normalized without parsing the values of recognized
scan-only fields (`fixed`, `points`, `ranges`, `start`, `stop`, `step`, or
`bidirectional`). This permits temporarily disabling a role without editing its
saved vector. Misspelled/unknown field names are still rejected, and changing
the role back to `fixed` or `sweep` restores the complete strict validation.
An active sweep accepts exactly one of explicit `points` or ordered `ranges`.
Range expansion is purely offline and feeds the same pre-write target-limit
validation as explicit points; legacy active top-level `start/stop/step` is rejected.

Keithley compliance remains an instrument protection setting, but it is not a
second user-entered boundary. Voltage-source roles derive current compliance from
`max_abs_current_a`; current-source roles derive voltage compliance from
`max_abs_voltage_v`. The adapter queries compliance and source/measurement ranges
after configuration and fails closed if the compliance readback exceeds the
approved absolute limit. Source and measurement autorange are required for
formal acquisition because the current schema has no fixed-range fields.

The user-approved 2400 initialization exception (2026-09-30) permits a temporary
minimum sense range only after confirmed source zero/output OFF, to satisfy the
manual's nominal-range 0.1% compliance floor. AUTO and compliance are queried
again before output enable. Only a first +822-only compliance-setting rejection
permits one audited range-preparation/retry with fresh zero/OFF confirmation.
Other/repeated errors and communication failures fail closed. This is neither a
raised protection limit nor an acquisition retry. Initial/configuration error
queues and partial command/readback audits are retained separately from cleanup.

Compliance programming uses a round-trip-safe scientific SCPI literal through
the existing QCoDeS transport, for both current and voltage protection. The same
literal is retained in configuration_audit. QCoDeS compliance setters using
{:f} round 100 nA to 0.000000; this is a confirmed software cause of +822 in the
reported run, distinct from range compatibility. Actual compliance readback and
all zero/OFF, AUTO and error-queue guards still apply; the fix never raises the
requested maximum. A real output-OFF acceptance check is still pending.

The Three-SMU path intentionally has no software ramp, source min/max, readback
tolerance, separate leakage threshold, or per-device settle time. A formal point
uses one direct target write per active role, the shared `delay_s`, then records
the actual readback. Cleanup directly requests zero, waits `delay_s`, records the
readback, and disables output. A communication failure never proves 0 V or
output-off; the last confirmed state is retained and the instrument must be
checked manually.

The Three-SMU `run` terminal panel and live Notebook may display a formal sample
only after the session has recorded it and placed it in an in-process memory FIFO.
Those presentation consumers must not issue a second hardware read, write, or
status-queue query. If a formal sample is unsafe, it is published before the
session raises its fail-closed error so its existing readback/status evidence can
be displayed and retained without changing cleanup order.

The legacy model-independent simulation gate controller retains its own ramp and
leakage test fixtures; those fields are not part of the real Three-SMU daily
hardware TOML.

Signed resistance is `Vxx_X / I_rms`. The software never infers `I_rms` from the
SR830 amplitude unless the operator explicitly supplies the complete excitation
path resistance, including series components and termination/loading effects.

## Vendor files

Whether the vendor DLL binary is tracked is a repository packaging decision, not
a hardware-safety or commissioning restriction. Runtime-specific DLL paths still
belong in `config/hardware.local.toml`; local paths must not be copied into tracked
configuration or raw public exports.
