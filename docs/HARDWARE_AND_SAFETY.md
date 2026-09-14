# Hardware and safety guide

## Magnet coordinates and limits

Integration boundary (2026-09-14): the new four-module offline coordinator keeps
an additional universal resultant cap of 3 T, including pure Z, per the current
integration task. The standalone magnetic envelope below is retained for history
and standalone use; merging it does not authorize higher-field integrated runs.

The active software coordinate system is X/Z:

- Z is the 9 T axial coil in the factory system sheet.
- X is the 3 T transverse coil called Y in the factory system sheet.
- No mechanical rotator is controlled.

The operator approved this replacement envelope on 2026-09-11. It supersedes the
former universal 3 T cap, but does not authorize real hardware execution:

```text
abs(Bx) <= 3 T
abs(Bz) <= 9 T
if Bx != 0 and Bz != 0: sqrt(Bx^2 + Bz^2) <= 3 T
```

Pure X permits +/-3 T; pure Z permits +/-9 T. Only exact zero (including signed
zero) selects the single-axis envelope. Small nonzero requests or residual
readbacks remain dual-axis even below acknowledgement/stability tolerance.
Apply the same rule to requested values, exact float32 commands and readbacks.
`[magnet].experiment_vector_max_t` now limits dual-axis fields only (maximum 3 T).
Axis limits may be reduced, never raised above factory X 3 T / Z 9 T. To restrict
pure-axis operation, lower its axis limit, not `experiment_vector_max_t`.

The envelope is not convex: high pure Z to a dual-axis target can have unsafe
direct intermediate points despite valid endpoints. Reject the plan, never
silently switch to `via_zero`. Setpoint acknowledgement does not prove the other
coil has ramped down: before a changed component write, also validate that
component against the other axis's latest actual readback. Residual cross-axis
field blocks high-Z operation rather than widening a zero tolerance. Magnet
temperature/readiness and factory charging parameters still apply; this change
does not alter APS100 sweep rates or establish a continuous physical trajectory.

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
approved absolute limit. Source and measurement autorange are required because
the current schema has no fixed-range fields.

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
