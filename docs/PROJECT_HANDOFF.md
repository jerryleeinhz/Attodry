# Project handoff

Last updated: 2026-08-21

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
- Added write-gated `sweep-frequency` and `sweep-excitation` device-only commands.
  They use increasing predefined points, latch-consuming xx/xy samples, strict
  unlock/overload/error/readback rejection, retained partial raw points, and
  verified restoration of the 17.777 Hz/4 mVrms baseline. The excitation scan
  requires explicit circuit and device limits, validates worst-case bounds before
  opening VISA, temporarily changes only xx sensitivity, then restores its
  original readback. Real execution remains pending separate authorization.
- The first authorized frequency run safely stopped at 25 Hz on an XY unlock
  latch after accepting the 17.777 Hz baseline. Cleanup restored 17.777 Hz,
  4 mVrms, and the original 1 mV sensitivity; the cleanup record retained the
  latch and therefore did not falsely claim verification. A subsequent 10-sample
  read-only recovery record was fully clear. The revised sweep now separates and
  records expected external-reference transition latches, clears them after an
  initial settle, waits again, and applies the unchanged fail-closed checks to
  the formal sample window. The revised real retry is not yet authorized.
- The authorized retry with transition separation accepted 25, 35.5, and 50 Hz,
  then rejected only the third 70.7 Hz sample: XY remained locked and error-free
  but reported 70.6978 Hz, 2.2 mHz or 31 ppm below the source. Cleanup verification
  completed with both status/error words clear at 17.777 Hz and 4 mVrms. The
  initial 50 ppm sweep-only external-readback tolerance covered that jitter while
  leaving unlock, overload, error, and non-sweep harmonic criteria unchanged.
  Its real retry and the excitation scan are not yet authorized.
- The next authorized run passed through 35.5 Hz and stopped at the first 50 Hz
  formal sample because XX reported output overload at about 1.09 mV on its 1 mV
  sensitivity range. Final 17.777 Hz/4 mVrms/1 mV readback and both status words
  were clear, but the rejected overload record is retained and the excitation
  scan did not run. The frequency command now temporarily uses SENS 21 (20 mV)
  and restores the original xx range only after returning to the baseline and
  settling. A new authorization must include this frequency-scan SENS write.
- The SENS-authorized retry first stopped during preflight on a stale XY overload
  latch before any write. Its separate 10-sample read-only recovery was entirely
  clear, and the same authorized run then accepted every formal frequency point
  through 200 Hz with XX on the temporary 20 mV range. At the 282 Hz transition,
  XY returned `LIAS=26` (filter overload, reference unlock, and frequency range
  changed), before any formal 282 Hz sample. Cleanup fully verified the original
  17.777 Hz/4 mVrms/1 mV state and clear status/error words; the excitation scan
  did not run. The revised scanner retains and consumes transition-only overload
  latches together with unlock/range-change latches, then settles again; it still
  rejects any overload, unlock, or error in the formal sample window. This revised
  behavior requires a new explicit authorization before another real run.
- The subsequent authorized retry passed 25 and 35.5 Hz, then rejected only the
  second formal 50 Hz sample: XY was locked, overload-free, and error-free but
  read 49.9973 Hz, 2.7 mHz or 54 ppm below the requested frequency. Cleanup again
  fully verified the original 17.777 Hz/4 mVrms/1 mV state and clear status/error
  words; the excitation scan did not run. The sweep-only tolerance is now 100 ppm,
  providing margin over the retained 31 and 54 ppm observations while leaving
  all formal unlock, overload, and error checks unchanged. This tolerance change
  requires new explicit authorization before another real run.
- The authorized 100 ppm retry completed every formal frequency point through
  1 kHz and fully verified restoration to 17.777 Hz, 4 mVrms, and the original
  1 mV XX sensitivity with clear final status/error words. The first excitation
  invocation then stopped in read-only preflight on a stale XY overload latch; a
  10-sample recovery was fully clear. Its retry acquired all 11 points from 4 to
  400 mVrms: all 33 formal samples had zero status/error bits and no problems. At
  400 mVrms, nominal current was 3.958 uArms, mean Vxx R was about 5.384 mV, and
  mean Vxy R was about 1.748 uV. Cleanup restored 4 mVrms and the original 1 mV
  range, but its immediate final read retained XX `LIAS=4` from the sensitivity
  transition, so the raw run remains rejected; the following 10-sample read-only
  record was fully clear. Cleanup now records and consumes one XX-only overload
  transition after restoring the narrow range, settles again, and retains strict
  final status checks. A new authorization is required for the revised excitation
  scan.
- The authorized cleanup-aware retry completed all 11 excitation points and all
  33 formal samples with zero status/error bits and no problems. At 400 mVrms,
  nominal current was 3.958 uArms, mean Vxx R was about 5.363 mV, and mean Vxy R
  was about 1.748 uV. Cleanup retained the expected XX-only `LIAS=4` transition
  latch while XY remained clear, then strictly verified 17.777 Hz, 4 mVrms, the
  original 1 mV XX sensitivity, and zero final status/error words on both units.
  The frequency and excitation device-only sweeps are now both commissioned;
  their accepted raw JSON files remain only on the ignored control-computer path.

Stage 4 - attoDRY legacy-DLL adapter: offline implementation, target-computer
DLL ABI preflight, and real read-only connection validation complete; setting
writes remain uncommissioned and require separate explicit authorization.

Completed offline in Stage 4:

- Added 64-bit/path checks and explicit ctypes signatures for every used symbol.
- Added separately authorized begin/connect/initialization polling and timeout.
- Every DLL call checks its return code; read failures preserve the prior
  `last_confirmed_state` rather than inferring a new field value.
- Added full temperature/VTI/X/Z/setpoint/control/error state reads and
  read-before-toggle idempotent control operations.
- Added safe zero-detour coordinated vector setpoints, rolling stable waits, and
  monitored vendor sweep-to-zero behavior against a fake DLL.
- Target preflight found vendor DLL version 2.0 and confirmed 64-bit AMD64 PE32+
  plus all 21 required exports without calling begin/connect. The confirmed
  station-local COM port and DLL path are stored only in the ignored local TOML.
- Added `attodry_test`, an explicitly connection-authorized read-only state CLI
  that constructs the driver with setting writes disabled, retains confirmed
  state on read failure, and disconnects/ends after sampling. Connection failure
  after a successful begin now attempts end without masking the primary error.
- The authorized 10-second real connection completed 10/10 full-state samples
  with `writes_authorized=false`, then disconnected and ended normally. Sample
  temperature ranged from 1.7251 to 1.7255 K and VTI temperature from 1.7146 to
  1.7153 K. Bx/Bz readbacks and setpoints remained zero, both control flags
  remained disabled, and all error codes were zero. Raw output remains only on
  the ignored control-computer path.

Current boundary: all hardware-free work through Stage 7, the independent
Three-SMU QCoDeS S0 module, integrated dual-SR830 harmonic validation, and the
attoDRY read-only connection are complete. Three-SMU target-computer validation,
all real SMU connections/writes, attoDRY setting writes, integration of the
independent SMU module into the main acquisition, and real end-to-end acquisition
still require staged authorization.

Module handoff packages are available under `docs/modules/` for separate Chat
follow-up:

- `LOCKIN.md` records the dual-SR830 configuration/phase/autorange objectives and
  turns the completed laboratory experience into implementation and acceptance
  rules.
- `TEMPERATURE.md` and `MAGNETIC_FIELD.md` separate their offline, target-offline,
  real read-only, and future write-commissioning stages without overstating the
  completed 10-second attoDRY read-only connection.
- `THREE_SMU.md` now records the three-Keithley QCoDeS S0 module as offline
  complete: semantic bias/top-gate/bottom-gate roles, one shared CLI/Notebook
  generator, retained scan modes, strict operator-filled safety configuration,
  auditable data, accepted-only analysis, and fail-closed cleanup. No real SMU
  connection or setting write was performed.
- `THREE_SMU_DAILY_OPERATION.md` now provides the operator-facing independent
  daily workflow and full parameter reference. It clearly separates the currently
  permitted offline `describe`/analysis path from future, separately authorized
  connection and write steps; adding the guide did not perform hardware actions.
- `INTEGRATION.md` requires commit IDs, tests, hardware-action reports, and known
  limitations from the four device modules before combination.
- `docs/modules/README.md` defines shared permissions, `lyr` use, branch/worktree
  isolation, status terminology, and the completion-report format for each Chat.

These files are planning and handoff artifacts. They do not authorize hardware
connections, status-latch consumption, or setting writes, and no such action was
performed while creating them.

Stage 5 - gate safety and integrated acquisition: model-independent offline core
and independent Three-SMU QCoDeS S0 module complete; target/real commissioning
and main-acquisition integration remain pending.

Completed offline in Stage 5:

- Added explicit write authorization, configured absolute-voltage limit,
  compliance setup, controlled ramps, voltage/leakage readback checks, and
  best-effort zero/disable behavior for model-independent gate backends.
- Hardware readiness rejects unresolved addresses and all per-gate compliance,
  leakage, voltage, ramp-step, readback-tolerance, and settle-time placeholders
  before any hardware driver can be constructed.
- Added signed Vxx/I and excitation-current helpers that do not guess the sample
  path impedance, plus explicit linear paired-gate relations.
- Added a separate `THREE_SMU.md` QCoDeS work package with one bias SMU and two
  voltage-source gate SMUs. Its S0 implementation now
  provides one shared CLI/Jupyter session and deliberately excludes Lock-in
  recording.
- Added `THREE_SMU_DAILY_OPERATION.md` as the Stage 5 operator guide for local
  templates, strict parameter review, scan modes, CLI/Notebook use, accepted-only
  analysis, cleanup interpretation, and manual-verification failures.
- Added strict independent Three-SMU hardware/scan TOML, a narrow exception-
  transparent QCoDeS Keithley 2400 adapter, offline `describe`, write-gated
  `run`, and a shared safety/session generator. Supported plans cover time,
  bias I-V, separate or paired gates, one-to-three-channel maps, and software
  pulses with directional/serpentine options and repeated samples.
- Each formal point records sequential per-role timestamps, source setpoint,
  V/I/R, output, compliance, gate leakage, status, scan coordinates, and cleanup
  results in `metadata.json`, `raw.jsonl`, and `data.csv`. Raw rejected,
  interrupted, partial, and cleanup events are retained; the new analysis loader
  and Notebook default to completed/accepted/clean formal rows.
- Fake instruments validate authorization-before-driver-import, query-only
  preflight, duplicate address/identity and active-output refusal, ramp bounds,
  compliance, leakage, readback mismatch, communication failure, Ctrl+C, and
  ordered zero-disable cleanup. Cleanup uncertainty rejects otherwise clean data
  and preserves last-confirmed state for manual verification.
- Added audited simulation execution across SQLite start/raw/complete events,
  retry, resume, checkpoints, normal hold/zero cleanup, and failure cleanup.

Stage 6 - accepted-only analysis: offline implementation complete.

Completed offline in Stage 6:

- Added read-only accepted-attempt long-form loading and explicit rejected-audit
  opt-in, CSV export, accepted gate-leakage loading, Bx/Bz/magnitude/angle
  metadata, transport traces, and 2D gate-map preparation/plotting.
- Added `attodry-analyze` and a notebook that imports only the analysis surface.
- Added a separate read-only SR830 commissioning analysis module and
  `sr830_commissioning_sweeps.ipynb`. It recursively catalogs JSON/JSONL files,
  provides a native Windows Browse/open path, defaults to completed records and
  clean formal samples, requires explicit rejected-data audit opt-in, and filters
  problem/unlock/overload/instrument-error samples without mixing transition or
  cleanup payloads into curves. It plots XX/XY X/Y/R/phase mean and sample standard
  deviation versus frequency, source voltage, or nominal current and can export
  CSV/PNG/PDF only when explicitly enabled. The loader detects both UTF-8 and
  PowerShell UTF-16/BOM commissioning records, and executed notebooks close
  figures after display to prevent duplicate inline output.
- Added the read-only `xy_sweep_analysis` module and `sr830_xy_sweeps.ipynb`;
  it discards XX while retaining both frequency and excitation-amplitude scans,
  labels each XY figure by harmonic order, and preserves the clean/rejected gate.
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
- The complete hardware-free suite contains 174 tests and passes in the current
  minimal environment with two optional matplotlib rendering tests skipped. Source compilation
  passes. The plotting path is unchanged from its prior rendered validation;
  the current system matplotlib/numpy binary mismatch is an environment issue.
- The local `attodry_transport_control-0.1.0-py3-none-any.whl` was rebuilt
  without downloading dependencies, inspected, and isolated-import checked after
  the final offline changes. SHA-256:
  `0cb4b12e7ab76dbc8b2141e391955ba2c3f0b89167f3d254800bd747edc9d6b2`.
  This is not yet the frozen hardware wheelhouse.
- The integrated acquisition path still cannot construct real SMU hardware. The
  integrated 1/2/3-harmonic SR830 path and attoDRY read-only connection are
  commissioned, but do not claim write-enabled attoDRY, SMU, or real end-to-end
  acquisition.

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

1. Perform staged attoDRY small-movement commissioning only after a new explicit
   write authorization and operator-selected smallest practical targets.
2. Run Three-SMU S1 target-offline validation in `LK_setup` `lyr`, then fill
   the ignored local addresses and safety values. Any real connection or setting
   write still requires a separate plan-specific authorization.
3. Freeze and verify the complete hardware wheelhouse on the offline control
   computer after its Python/VISA environment is known.
