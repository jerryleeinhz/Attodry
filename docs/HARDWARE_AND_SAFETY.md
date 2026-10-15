# Hardware and safety guide

## Magnet coordinates and limits

The active software coordinate system is X/Z:

- Z is the 9 T axial coil in the factory system sheet.
- X is the 3 T transverse coil called Y in the factory system sheet.
- No mechanical rotator is controlled.

The hardware ratings are retained as metadata, but every experiment command is constrained by the user-confirmed project limit:

```text
Bmag = sqrt(Bx^2 + Bz^2)
Bmag <= 3 T
```

This means a pure-Z command is also limited to 3 T in this project even though the Z coil hardware rating is 9 T. Raising the project limit requires a deliberate user-approved change to configuration, documentation, and tests.

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
- validate the full target vector before setting either component;
- record both component setpoints and readbacks;
- never infer zero after a failed read.

Changing both components may produce a transient path that differs from the requested direction. Constant-direction or constant-magnitude ramps therefore require coordinated intermediate vector points and readback verification; setting X and Z independently once is not sufficient to promise the path.

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

The model-independent controller requires every safety value explicitly: absolute
voltage limit, current compliance, leakage trip, maximum ramp step, settle time,
and voltage-readback tolerance. It sets compliance before enabling an output,
enables only at 0 V, verifies every ramp step, and attempts a stepped return to
zero followed by output disable after any write, readback, or leakage failure.

No vendor command adapter is active until the exact top/bottom SMU models and
manuals are confirmed. A communication failure does not prove 0 V or output-off;
the last confirmed state is retained and the instrument must be checked manually.

The checked-in hardware template deliberately leaves VISA/DLL/COM values and all
six per-gate limits as `CHANGE_ME`. `require_hardware_ready()` rejects these
placeholders before a hardware driver is constructed; replacing them requires
operator-confirmed station values, not copied example limits.

Signed resistance is `Vxx_X / I_rms`. The software never infers `I_rms` from the
SR830 amplitude unless the operator explicitly supplies the complete excitation
path resistance, including series components and termination/loading effects.

## DC photoresponse profile (2026-10-13)

This independent profile has offline-tested acquisition adapters and explicit hardware
entry points. It does not construct SR830 or open unselected instruments. Loading and
validating configuration perform no I/O. Real run requires complete physical limits,
confirmed wiring/optical route and connection/write authorization; environment control
requires its separate flag. `diagnose` is electrical-query-only; `test-6221` opens only
its source. Neither operation opens a laser or unused detector.

- No excitation is a physically isolated external source path, not a 0 A command.
  A relay-host 6221 may still be queried, but no output controller is constructed.
- 6221 starts at zero/off, disarms waveform/sweep before DC setup, verifies each
  bounded step and rejects compliance. Minimum voltage compliance is 0.1 V.
  Inactive COMP may remain asserted after an empty-load stop. Preserve that raw
  bit; it does not indicate active current delivery when output is positively OFF.
  Relay-only operation still verifies identity, physical isolation, unarmed modes,
  OFF, MEAS unsafe bits and QUES clear. Check compliance immediately after every
  output enable, including 0 A. Do not clear/reset status to manufacture a pass.
- 2182A READ changes trigger state and requires write authorization; stale FETCh
  readings are never accepted as new samples. The error queue also needs write permission.
- 2400 voltmeter uses its documented 0 A/fixed-range, OUTPUT-on configuration.
  Confirm loading/compliance before use. Current mode sources voltage. Both return
  to zero and confirmed HIMP/off; an already active sample is not taken over.
  The photocurrent detector must use the source-drain path. Bottom-gate/source
  wiring is a semantic gate-bottom leakage role, not a channel-current detector.
- Laser emission checks source identity, settings and interlocks. CLD additionally
  requires a confirmed current limit, wiring and TEC range/protection.
  In constant-current mode an additional software voltage threshold is optional,
  not a required voltage setpoint. Always record voltage and reject invalid/overload
  readings; enforce the extra threshold when configured. Omission does not select
  a guessed diode rating or use the instrument measurement range as that rating.
  Record the diode model truthfully; the operator may explicitly elect an unspecified
  model for staged testing. Approximate wavelength and measurement range do not
  establish a diode voltage rating. USB/VISA and native TL4000 paths never reset.
  Qualified TEC sensor/overtemperature/compliance faults block preparation.
  A temperature trip may be expected during LD-off TEC settling, but blocks LD
  preparation/enable and formal emission. The protection window is a full span;
  both target +/- span/2 and software tolerance must fit their approved bounds.
  Generic TL4000 cable faults do not apply to CLD. Raw CLD CONN/MEAS diagnostics
  retain unqualified firmware semantics; asserted or unreadable values block LD
  enable without being mislabelled as a proven cable failure. ENAB does not
  substitute for PROT; no reset/clear or polarity inversion bypasses protection.
  Native TL4000 initialization is not query-only even when reset=false: the
  vendor C driver unconditionally sends status-clear/preset commands and its
  default error-checking getters consume SYST:ERR?. Do not use that initialization
  for a read-only diagnostic or to make an unresolved protection indication pass.
- Optional PM100D feedback requires an explicit meter/sensor identity, wavelength
  correction, measurement plane, power bounds and bounded actuator domain before
  connecting the selected hardware. Source settings do not establish measured W.
  Feedback adjustments occur with emission off and restart stability proof; low or
  zero signal never permits increasing output to search for a beam. The formal
  bright window freezes the actuator and rejects target/spread drift. Dark windows
  verify meter settings/status without using dark noise for feedback. Meter failure
  or saturation rejects the complete attempt; raw replies remain in audit.
  PM ownership is independent of laser ownership, and meter-close failure must
  not skip optical/electrical/environment cleanup. Power/electrical samples are
  sequential guarded reads with timestamps, not continuous or simultaneous sensing.
- Environment changes occur with light and electrical sources off. Reuse the
  commissioned feedback-before-temperature sequence, bounded overshoot, existing field
  path validation and `sqrt(Bx^2+Bz^2) <= 3 T`. Record actual temperature/field.
- Optional Three-SMU photoresponse control reuses the pinned point-session driver.
  Select roles explicitly and reject repeated VISA ownership or two sample sources
  before opening resources. Gate V/I is gate bias/leakage, distinct from detector
  voltage/current. Omitted roles remain unopened. Environment movement requires
  confirmed source/SMU zero-disable; failed confirmation remains sticky and unknown.
  Cleanup failures cannot be overwritten by a later no-op finish or promoted by
  SQLite/default analysis. Real SMU control requires its explicit stage authorization.
- Cleanup independently attempts optical off, electrical zero/off, then field zero
  on failure (hold/zero on normal completion). A failed action does not skip later
  attempts. Communication failure leaves state unknown and preserves last-confirmed
  readback; require manual verification, never infer zero/off.
- VISA resource locks do not establish exclusive use of an entire GPIB controller.
  Coordinate other programs sharing the bus. No discovery, interface clear or bus reset.
  A timeout prevents continuing acquisition in that session; shutdown writes alone
  cannot certify physical safety.
- All failed/rejected raw data remain in local audit; default analysis accepts only
  wholly completed runs with verified cleanup. Resume creates a fresh whole attempt.

Real access resumed on 2026-10-13. 6221 unloaded commissioning and the initial
2400 gate-path trial stopped on compliance with verified zero/off cleanup. The
2182A input is confirmed floating. CLD passive USB/VISA verification now confirms
stable operator-controlled TEC operation at 26 C with LD off and temperature trip
clear. CONN/MEAS indicators remain inconsistent with normal operation; preserve
raw values and keep automatic LD enable blocked until their semantics are
confirmed. The extra software voltage threshold is optional in constant-current mode. Do not take over the operator's active TEC.
See [photoresponse guide](PHOTORESPONSE_GUIDE.md) for current role/wiring evidence.

## Vendor files

Whether the vendor DLL binary is tracked is a repository packaging decision, not
a hardware-safety or commissioning restriction. Runtime-specific DLL paths still
belong in `config/hardware.local.toml`; local paths must not be copied into tracked
configuration or raw public exports.
