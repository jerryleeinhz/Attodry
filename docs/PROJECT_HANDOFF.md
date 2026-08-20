# Project handoff

Last updated: 2026-08-20

## Current stage

Stage 0 - confirmed design and safety scaffold: complete.

Stage 1 - strict configuration and full simulation: complete.

Completed in Stage 1:

- Strict, hardware-free TOML loading for both checked-in configuration templates.
- Missing and unknown fields are rejected, as are mode/backend mismatches.
- The confirmed 3 T project limit, cleanup zero-field policy, semantic SR830 roles,
  distinct lock-in addresses, matched frequency, and gate protection ordering are
  validated before any driver could be constructed.
- Added deterministic simulation cryostat, semantic xx/xy lock-ins, and top/bottom
  gates with injected timeout, communication, unlock, overload, and leakage faults.
- Added condition, attempt, raw-reading, and accepted-result records with explicit
  `condition_id`, `attempt_index`, and accepted/rejected contracts.
- Added numeric, X/Z vector, temperature-field, gate-grid, and paired-gate scans.
- Added deterministic cleanup with raw rejected readings retained, Ctrl+C cleanup,
  and last-confirmed field preservation when zero readback fails.

Stage 2 - SQLite storage, resume, monitoring, and audit: complete.

Completed in Stage 2:

- Added WAL/FULL-synchronous SQLite storage for runs, events, conditions,
  attempts, raw instrument samples, cryostat/gate station samples, transport
  readings, and checkpoints.
- Enforced a database-level single accepted attempt per condition and promoted
  raw/transport/station rows to accepted only after one safe station snapshot and
  six safe xx/xy × h1/h2/h3 readings.
- Rejected and interrupted raw attempts remain stored and are excluded by the
  default accepted-only loader.
- Added monotonic checkpoints, pending-condition resume, retry numbering, and
  a URI `mode=ro`/`query_only` monitor plus `attodry-monitor` CLI.
- Added explicit `scan_id` storage. Migrated legacy rows use `legacy`, and the
  publication layer isolates those rows by condition instead of inventing scan
  boundaries.
- Cleanup audit events persist each action, zero/hold confirmation, and the full
  last-confirmed cryostat state. `KeyboardInterrupt` also persists station data
  and any raw lock-in readings captured before interruption.

Stage 3 - integrated dual-SR830 driver: integrated 1/2/3-harmonic laboratory
validation complete (2026-08-20).

Completed in Stage 3:

- Added semantic dual-controller orchestration on top of the SR830 command adapter.
- Both units receive each harmonic setting before per-unit coherent SNAP reads;
  xx/xy pair timing remains explicitly sequential rather than falsely simultaneous.
- Unlock, input/reserve/filter/output overload, instrument error, readback mismatch,
  and communication failure all fail closed to minimum-output attempts while
  retaining partial raw readings.
- All setting paths perform full query-only diagnostics before their first write.
- Query-only output marks latched safety status incomplete unless explicitly
  consumed, and identical full IDNs from the two addresses block all writes.
- `lockin_test diagnose/configure-minimum --config hardware.local.toml` reuses
  semantic addresses, VISA timeout, and frequency from the unified local TOML;
  CLI values override the file without modifying it.
- Standalone laboratory commissioning confirmed distinct SR830 identities,
  internal xx and external-TTL-rising xy reference roles, 17.777 Hz, 4 mVrms,
  A-B/Float inputs, and physically disconnected xy SINE OUT. The confirmed
  100 kohm external series resistor plus the SR830 50 ohm output resistance and
  approximate 1 kohm device give about 39.58 nArms.
- The Vxx A/B reversal produced X/Y ratios of about -1.012/-0.986, retained R
  within about 1%, and shifted phase by 179.78 degrees. The final accepted bench
  state has a stable approximately 0.11 mV Vxx magnitude; the final record has
  59 consecutive post-latch-clear samples with no unlock, overload, or instrument
  error. Raw commissioning JSONL remains only on the ignored control-computer path.
- Sequential SR830 frequency readbacks differed by at most 0.9 mHz without
  unlock. The shared check now allows one 1 mHz readback step plus floating-point
  margin and still rejects a 2 mHz mismatch.
- A later authorized optimization set both lock-ins to 1 mV sensitivity and
  300 ms time constant without changing 17.777 Hz, 4 mVrms, or 24 dB/oct. The
  60-second readback record was complete and contained no unlock, overload, or
  instrument error. Vxx averaged about 104.70 uV, while Vxy remained at the
  near-zero quantized floor (maximum about 59.6 nV). The operator accepted this
  as the device's normal zero-field Vxy baseline.
- Added a separately authorized `measure-harmonics` CLI that consumes status
  latches while validating the existing minimum-output role configuration, then
  writes only paired harmonics 1/2/3, retains partial rejected data, and restores
  harmonic 1. Failure also attempts harmonic-1 and minimum-output cleanup before
  requiring manual readback confirmation.
- The first authorized real attempt stopped at harmonic 1 on an XY unlock latch
  generated by unnecessarily rewriting its existing external-reference mode.
  Partial readings were retained, and the cleanup readback confirmed both units
  restored to harmonic 1 and 4 mVrms with zero status/error bits. The revised CLI
  now performs read-only preflight validation and does not rewrite reference mode
  or frequency; the subsequent authorized retry used this revision.
- The separately authorized retry completed xx/xy harmonics 1, 2, and 3 with all
  six readings locked, no overload or instrument error, accepted sequential pair
  frequencies, and verified restoration of both units to harmonic 1. Approximate
  R values were xx 100.26/6.02/11.09 uV and xy 0/1.43/0.60 uV for h1/h2/h3.
  The accepted JSON remains only on the ignored control-computer path.

Stage 4 - attoDRY legacy-DLL adapter: offline implementation complete; real DLL
ABI and laboratory validation remain pending.

Completed offline in Stage 4:

- Added 64-bit/path checks and explicit ctypes signatures for every used symbol.
- Added separately authorized begin/connect/initialization polling and timeout.
- Every DLL call checks its return code; read failures preserve the prior
  `last_confirmed_state` rather than inferring a new field value.
- Added full temperature/VTI/X/Z/setpoint/control/error state reads and
  read-before-toggle idempotent control operations.
- Added safe zero-detour coordinated vector setpoints, rolling stable waits, and
  monitored vendor sweep-to-zero behavior against a fake DLL.

Current boundary: all hardware-free work through Stage 7 and integrated dual-SR830
harmonic validation are complete. attoDRY, SMUs, and real end-to-end acquisition
still require staged authorization.

Stage 5 - gate safety and integrated acquisition: model-independent offline core
complete; vendor SMU adapters remain pending exact models and safety parameters.

Completed offline in Stage 5:

- Added explicit write authorization, configured absolute-voltage limit,
  compliance setup, controlled ramps, voltage/leakage readback checks, and
  best-effort zero/disable behavior for model-independent gate backends.
- Hardware readiness rejects unresolved addresses and all per-gate compliance,
  leakage, voltage, ramp-step, readback-tolerance, and settle-time placeholders
  before any hardware driver can be constructed.
- Added signed Vxx/I and excitation-current helpers that do not guess the sample
  path impedance, plus explicit linear paired-gate relations.
- Added audited simulation execution across SQLite start/raw/complete events,
  retry, resume, checkpoints, normal hold/zero cleanup, and failure cleanup.

Stage 6 - accepted-only analysis: offline implementation complete.

Completed offline in Stage 6:

- Added read-only accepted-attempt long-form loading and explicit rejected-audit
  opt-in, CSV export, accepted gate-leakage loading, Bx/Bz/magnitude/angle
  metadata, transport traces, and 2D gate-map preparation/plotting.
- Added `attodry-analyze` and a notebook that imports only the analysis surface.
- Added an auditable publication suite for current/harmonic/frequency/
  temperature/field/angle/gamma, T-|B|, gate-resistance, gate-leakage, and n-D
  results, with an explicit generated/skipped manifest and fit summary.
- Derived current/resistance and n-D outputs require operator-supplied path
  resistance and gate calibration. Unsupported Hall/Nernst/scattering/geometry/
  mechanism products are skipped, never guessed.
- The pinned matplotlib analysis environment rendered the representative suite;
  curve, heat-map, leakage, and n-D outputs were visually inspected.

Stage 7 - offline commissioning scaffold: complete; laboratory work pending.

- Added `attodry-simulate` for a full no-hardware run and deliberate first-unlock
  rejection/retry test.
- Added `LAB_COMMISSIONING.md` with all manual authorization checkpoints.
- The complete hardware-free suite contains 118 tests and passes in the minimal
  environment with one matplotlib rendering test skipped. Source compilation
  passes. The plotting path is unchanged from its prior rendered validation;
  the current system matplotlib/numpy binary mismatch is an environment issue.
- The local `attodry_transport_control-0.1.0-py3-none-any.whl` was rebuilt
  without downloading dependencies, inspected, and isolated-import checked after
  the final offline changes. SHA-256:
  `7e1d6df604ceeeb71f6d96239468f065dda344c964a99d2f441629b4569dab8b`.
  This is not yet the frozen hardware wheelhouse.
- The integrated acquisition path still cannot construct real SMU hardware. The
  integrated 1/2/3-harmonic SR830 path is commissioned, but do not claim an
  attoDRY, SMU, or real end-to-end acquisition.

User-priority SR830 bench-test slice completed in the laboratory:

- `docs/DUAL_SR830_DEVICE_TEST.md` defines safe cabling, minimum-excitation
  calculation, front-panel setup, commands, acceptance criteria, and stop steps.
- `python -m attodry_control.lockin_test discover` lists VISA resources without
  opening instruments.
- `diagnose` sends queries only; status-latch consumption requires an explicit flag.
- `configure-minimum` requires explicit write authorization and physical XY SINE
  OUT disconnection confirmation, records before/after readback, and retries the
  4 mVrms minimum on both units after a caught write failure.
- The standalone first-harmonic device test, physical Vxx sign reversal, and
  integrated 1/2/3-harmonic write path are complete.

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
- Real vendor-specific gate SMU control and laboratory validation remain project
  goals; offline compliance/leakage protection, monitoring, installation workflow,
  notebook analysis, and paper-oriented plotting are implemented.

## Confirmed equipment details

Control-computer runtime:

- On `LK_setup`, run every project command in the Conda environment `lyr`.
- SSH automation must either activate `lyr` or invoke
  `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe` directly; never use the
  control computer's bare system `python`.

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

1. Perform staged attoDRY read-only and small-movement commissioning only after
   explicit connection/write authorization.
2. Add the two vendor SMU adapters only after exact models, limits, and command
   references are supplied.
3. Freeze and verify the complete hardware wheelhouse on the offline control
   computer after its Python/VISA environment is known.
