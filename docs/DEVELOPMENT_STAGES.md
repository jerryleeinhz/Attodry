# Development stages

Update this file whenever a feature is completed. A stage is complete only when its tests and documentation are complete.

## Stage 0 - design and safety scaffold

Status: complete (2026-08-20).

- Confirmed attoDRY legacy COM/DLL interface.
- Confirmed X/Z naming and factory Y/X discrepancy.
- Confirmed 3 T maximum resultant experiment field.
- Confirmed dual SR830 wiring and roles.
- Confirmed no rotator.
- Confirmed caught-exception zero-field policy.
- Added tested vector model, safety validation, and rolling stability predicate.
- Added Codex, hardware, offline-install, and handoff guides.

## Stage 1 - strict configuration and full simulation

Status: next.

- Strict TOML loader with unknown/missing-field rejection.
- Simulation cryostat, `lockin_xx`, `lockin_xy`, top gate, and bottom gate.
- Platform-independent condition, attempt, raw reading, and accepted-result models.
- Voltage, frequency, X/Z field, temperature-field, gate-grid, and paired-gate scan-point generation.
- Failure injection and deterministic cleanup-order tests.

## Stage 2 - storage, resume, monitoring, and audit

Status: pending.

- SQLite schema with runs, events, conditions, attempts, raw instrument samples, transport readings, and checkpoints.
- `condition_id`, `attempt_index`, `accepted` integrity contract.
- WAL-safe read-only monitor.
- Resume and retry without contaminating accepted analysis.

## Stage 3 - dual SR830 real driver

Status: pending.

- Two SR830 adapters distinguished by xx/xy roles.
- #1 internal excitation and #2 TTL external-reference configuration.
- Synchronized 1/2/3 harmonic measurement.
- Lock and overload fail-closed behavior.
- Read-only identity/reference diagnostics before writes.

## Stage 4 - attoDRY real driver

Status: pending.

- Safe 64-bit vendor DLL loading and explicit function signatures.
- COM connection and initialization timeout.
- Temperature, VTI, X/Z field, setpoint, control, and error readback.
- Idempotent temperature/field-control operations.
- Coordinated vector setpoints, stable wait, and verified zeroing.
- Fake-DLL contract tests before laboratory use.

## Stage 5 - gate SMUs and integrated acquisition

Status: pending.

- Two gate SMUs with voltage ramping, leakage readback, and compliance protection.
- Small AC excitation and signed Vxx/I resistance.
- Two-dimensional Vg1/Vg2 resistance mapping and zero-electric-field-line workflow.
- Retry, resume, monitor, and exception cleanup end to end.

## Stage 6 - analysis and notebook migration

Status: pending.

- Accepted-attempt-only loader.
- Reuse paper-oriented transport plots from the PPMS project.
- X/Z vector metadata and angle-dependent plots.
- Read-only notebook and batch CLI.
- No hardware control imports in the analysis notebook.

## Stage 7 - laboratory commissioning and offline release

Status: pending.

- Read-only identity/status diagnostics.
- Minimum-output SR830 checks.
- Small temperature and X/Z movements with manual confirmation.
- Gate zero-bias and leakage checks.
- Small end-to-end run with deliberate failure injection.
- Frozen wheelhouse and offline installation verification on the control computer.

