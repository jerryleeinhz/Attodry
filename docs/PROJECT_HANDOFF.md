# Project handoff

Last updated: 2026-08-20

## Current stage

Stage 0 - confirmed design and safety scaffold: complete.

Next stage: Stage 1 - configuration models, simulation devices, and platform-independent acquisition records.

This repository does not yet control real hardware. Do not claim that a real attoDRY, SR830, SMU, SQLite run, monitor, or publication plot has been implemented.

## User-confirmed requirements

- Replace PPMS/MultiPyVu/ETO with attoDRY2100XL.
- Use two SR830 lock-ins.
- `lockin_xx`: internal reference, SINE OUT excitation, Vxx.
- `lockin_xy`: TTL external reference from `lockin_xx`, Vxy, SINE OUT physically disconnected.
- Both lock-ins will be configured in software and measured by semantic role.
- No rotator.
- Control Bx and Bz; derive total field and direction from readback components.
- The experiment field invariant is `sqrt(Bx^2 + Bz^2) <= 3 T`.
- A caught exception or `Ctrl+C` requests field zero after electrical outputs are made safe.
- Normal completion field behavior is configurable, default `hold` in the example configuration.
- Gate SMU control, compliance/leakage protection, monitoring, offline installation, notebook analysis, and paper-oriented plots remain project goals.

## Confirmed equipment details

Factory system specification:

- attoDRY2100XL system 213348/211090.
- AMI Maxes MX-039-70 vector magnet, serial 15270.
- Factory table: Z axis 9 T at 4.2 K, transverse Y axis 3 T at 4.2 K.
- attoDRY `xyz` software exposes the same transverse coil as X.
- Rated currents: Z 82.43 A, transverse 77.25 A.
- Field/current ratios: Z 1092 Gauss/A, transverse 388.4 Gauss/A.

The code must call the active axes X and Z while preserving the factory-name note in documentation.

## Stable reuse boundary from the PPMS project

Reuse behavior and tests around:

- semantic xx/xy channels and 1/2/3 harmonic readings;
- raw attempts plus accepted/rejected status;
- long-form transport records;
- SQLite WAL, audit events, monitoring, resume, and plotting;
- gate compliance and leakage protection;
- notebook as a read-only analysis surface;
- offline wheelhouse workflow.

Do not copy active PPMS, MultiPyVu, ETO, SR865A, or rotator abstractions into the new control path.

The new storage schema must include `condition_id`, `attempt_index`, and `accepted`. Default analysis must use accepted attempts only while retaining rejected raw data for audit.

## attoDRY reference behavior

The reviewed reference tree is:

`C:/Users/liy56/OneDrive - Aalto University/Aalto University/Work/Experiment operation/Cryostat/Reference/Ruihuan`

Useful verified behavior:

- `AttoDRY(1)`, `begin()`, `connect("COM5")`, then poll device initialization.
- Read/write sample temperature and X/Z field/setpoint through the legacy DLL.
- Read temperature-control and field-control flags.
- Latest wrapper adds P/I/D gain setters.
- Latest GUI reads temperature, VTI temperature, X/Z field and X/Z setpoints once per second.
- GUI field scans use 1 mT tolerance and ten consecutive one-second checks.
- GUI temperature scans use a configurable-looking but hard-coded 10 or 100 mK tolerance and ten consecutive checks.

Do not copy these defects:

- missing timeout and error-code checks;
- blind toggle-based control;
- missing exception cleanup;
- ignored DLL return values;
- misspelled `setUserMageticFieldX/Z` public methods;
- one GUI field-control branch checks the temperature-control flag;
- one X-field stability branch reads Z during its loop.

## Required cleanup semantics

Caught acquisition exception or `Ctrl+C`:

1. Set the SR830 #1 excitation to its minimum safe amplitude.
2. Set both gate targets to zero using controlled ramps.
3. Disable both gate outputs after verified zero/readback when possible.
4. Request attoDRY field sweep to zero.
5. Monitor Bx, Bz, control/error state, and timeout.
6. Disconnect only after final confirmed state is recorded.

A communication failure must not be reported as successful zeroing. A hard process crash cannot be cleaned up by Python and requires manual inspection.

## Immediate next implementation tasks

1. Add strict TOML loading and validation without opening hardware.
2. Add simulation implementations of the cryostat, two lock-ins, and two gates.
3. Define condition, attempt, accepted-result, and raw transport record models.
4. Add failure-injection tests for timeout, unlock, overload, gate leakage, and cleanup order.
5. Update `docs/DEVELOPMENT_STAGES.md` when Stage 1 is actually complete.

