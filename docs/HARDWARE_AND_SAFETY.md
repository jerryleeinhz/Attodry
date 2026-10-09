# Hardware and safety guide

## SR865A electrical XY receiver exception (offline; 2026-10-07)

The user explicitly approved software support for replacing only `lockin_xy`
with SR865A. XX remains SR830, internal reference and the sole connected
excitation source. Its TTL OUT feeds XY REF IN; XY SINE OUT must be physically
disconnected. Dual SR830 remains the default. This software work does not
authorize real connection, commissioning or deployment.

SR865A uses native SCAL for sensitivity and independent IRNG in V peak, without
SR830 Reserve or line-notch settings. Range comparisons use physical volts,
not native code ordering. Reference impedance and current-status query support
are explicit local facts. Unknown safety status, unlock/error/configuration/
power-on flags and output-scale overload remain blocking. Optional overload
continuation covers only declared input/filter faults and retains invalid data.

Setup and formal checks preserve XY PHAS, SLVL, SOFF, REFM and BLAZEX. The
measurement filters ADVFILT/SYNC are separately disabled and verified. Cleanup
returns XX excitation to 4 mV, then verifies h1, native measurement settings,
input range and unchanged XY source state. Exact-zero SR865A X/Y has undefined
phase, archived as null. Magnetic, SMU and temperature policies are unchanged.

The commissioned daily `sweep-excitation` command carries operator write
authorization; `--authorize-writes` remains accepted for compatibility.
Mixed frequency and frequency/excitation sweeps still require that explicit
flag before any instrument I/O. Combination and temperature–excitation
propagate existing run authorization; the receiver/station cannot grant itself
authorization. Configuration, status, wiring, readback and cleanup gates remain
unchanged. Legacy dual-SR830
commissioning commands reject mixed pairs before connection. See
[configuration and scope](LOCKIN_DAILY_OPERATION.md#sr865a-作为-xy-接收机).

## Bounded SR865A frequency observation (2026-10-09)

The operator authorized up to two one-second rechecks of an in-range External
frequency inconsistency. The 100 ppm / fundamental 5 mHz threshold is unchanged.
Every wait requires a complete clean current/latched status window, with
explicit status capability and latch-consumption authorization. All raw pairs,
statuses and timestamps survive success or failure. Unlock, overload, device or
unknown status, invalid frequency, mode/harmonic drift and communication faults
stop immediately; a recovered pair still needs the final ordinary status check.
This only repeats frequency/status observations, with no settings write,
acquisition retry, failed-run restart or relaxation of setup/cleanup gates.
See [the exact policy and audit fields](LOCKIN_DAILY_OPERATION.md).

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
