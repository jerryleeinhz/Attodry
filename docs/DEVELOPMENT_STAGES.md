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

Status: complete (2026-08-20).

- Strict TOML loader with unknown/missing-field rejection. Complete (2026-08-20).
- Added simulation cryostat, `lockin_xx`, `lockin_xy`, top gate, and bottom gate.
- Added platform-independent condition, attempt, raw reading, and accepted-result models.
- Added voltage, frequency, X/Z field, temperature-field, gate-grid, and paired-gate scan-point generation.
- Added deterministic timeout, unlock, overload, gate-leakage, communication-failure,
  Ctrl+C, and cleanup-order tests.

## Stage 2 - storage, resume, monitoring, and audit

Status: complete (2026-08-20).

- Added SQLite runs, events, conditions, attempts, raw instrument samples,
  cryostat/gate station samples, transport readings, and checkpoints.
- Enforced the `condition_id`, `attempt_index`, and `accepted` integrity contract,
  including one accepted attempt per condition and one safe station snapshot plus
  six safe xx/xy × h1/h2/h3 readings before acceptance.
- Added explicit `scan_id` storage and a non-inferential schema migration: legacy
  rows are isolated by condition for plotting rather than silently merged.
- Added WAL-safe URI read-only monitoring and the `attodry-monitor` CLI.
- Added resume/retry handling that converts incomplete attempts to audited
  rejections without deleting raw samples or contaminating accepted analysis.
- Persisted every cleanup action, zero/hold confirmation, and the last confirmed
  cryostat state in audit events; interrupted attempts retain station samples and
  any already captured raw lock-in readings.

## Stage 3 - dual SR830 real driver

Status: integrated 1/2/3-harmonic laboratory validation complete (2026-08-20).

- Added query-only diagnostics and an explicitly authorized minimum-output
  reference-role configuration tool, verified against fake VISA resources.
- Added the standalone device-test procedure and acceptance criteria; real
  instrument validation is still required.
- Added two SR830 adapters distinguished by xx/xy roles.
- Added #1 internal excitation and #2 TTL external-reference configuration.
- Added ordered paired 1/2/3 harmonic measurement: both instruments are set to
  each harmonic before their per-instrument coherent SNAP queries; pair reads
  remain explicitly sequential.
- Added lock, overload, instrument-error, and communication fail-closed behavior
  with partial raw readings retained.
- Added read-only identity/reference/input diagnostics before writes.
- Diagnostics explicitly distinguish query success from complete safety status,
  and duplicate physical SR830 identity aborts configuration before any write.
- The standalone diagnostics and minimum-output configuration read semantic
  addresses, VISA timeout, and frequency from the ignored station-local TOML;
  explicit command-line values remain temporary overrides.
- Laboratory commissioning confirmed two distinct SR830 units at 17.777 Hz,
  4 mVrms excitation through 100 kohm external plus 50 ohm source resistance,
  differential A-B/Float voltage inputs, and physically disconnected `lockin_xy`
  SINE OUT. The calculated device current is about 39.58 nArms for the approximate
  1 kohm device.
- A Vxx A/B reversal inverted X and Y, preserved R to within about 1%, and shifted
  phase by 179.78 degrees. The restored current wiring completed 59 consecutive
  post-latch-clear samples without unlock, overload, or instrument error; the
  operator accepted the stable approximately 0.11 mV Vxx magnitude as the new
  bench baseline.
- Lab data showed up to 0.9 mHz sequential pair-readback variation with no unlock.
  Frequency checks now allow one 1 mHz readback step plus floating-point margin,
  while a 2 mHz mismatch remains rejected.
- An authorized range/filter refinement set both units to 1 mV sensitivity and
  300 ms time constant while preserving 17.777 Hz, 4 mVrms, and 24 dB/oct. All
  60 samples read those settings back with complete status and no unlock,
  overload, or instrument error. Vxx averaged about 104.70 uV; Vxy was at the
  instrument's near-zero quantized floor (maximum about 59.6 nV), which the
  operator accepted as the device's normal zero-field baseline.
- Added the separately authorized `measure-harmonics` laboratory command. It
  validates the existing reference configuration without rewriting it, records
  partial rejected data, measures paired harmonics 1/2/3 in order, and restores
  harmonic 1 after success or attempts harmonic-1/minimum-output cleanup after
  failure.
- The first real harmonic attempt was safely rejected at harmonic 1 because
  rewriting the already-correct XY external-reference mode created a transient
  unlock latch. The retained partial readings showed no overload; the immediate
  cleanup readback confirmed both units at harmonic 1 and 4 mVrms with zero
  status/error bits. The CLI now uses a latch-consuming read-only preflight and
  does not rewrite an already verified reference configuration.
- The authorized retry completed all six ordered xx/xy readings with no unlock,
  overload, instrument error, or pair-frequency rejection, then read both units
  back at harmonic 1. Measured R values for xx h1/h2/h3 were approximately
  100.26/6.02/11.09 uV; xy h1/h2/h3 were approximately 0/1.43/0.60 uV.
  The raw accepted record remains only on the ignored control-computer path.
- Added separately authorized frequency and excitation sweep commands for the
  next device-only tests. Both consume status latches, retain rejected point
  samples, stop on unlock/overload/error/readback mismatch, and verify restoration
  of 4 mVrms and the 17.777 Hz baseline. The excitation path additionally checks
  explicit device current/voltage bounds before opening VISA, temporarily widens
  only the xx sensitivity, and restores its original readback. Real sweep
  execution remains pending a new setting-write authorization.
- The first authorized frequency sweep accepted the 17.777 Hz baseline, then
  stopped at 25 Hz because XY reported a latched external-reference unlock. XX
  restored to 17.777 Hz, 4 mVrms, and its original 1 mV sensitivity; a subsequent
  10-sample read-only recovery record had zero status/error bits throughout.
  The scanner now records and clears the expected transition-period XY unlock
  latch after an initial settling interval, waits a second settling interval,
  and still rejects any unlock in the formal measurement window. A real retry
  and the excitation sweep remain pending authorization for the revised commit.
- The authorized transition-aware retry passed the formal windows at 25, 35.5,
  and 50 Hz, then retained and rejected the third 70.7 Hz sample solely because
  the locked XY frequency readback was 70.6978 Hz (31 ppm low). There were no
  overloads or instrument errors and final restoration was fully verified. A
  separate 50 ppm relative tolerance now applies only to frequency-sweep external
  readback jitter; all unlock/error checks and the established harmonic-path
  tolerance remain unchanged. Another real retry is pending authorization.
- The next authorized retry stopped at 50 Hz on a real XX output-overload latch:
  Vxx had risen to about 1.09 mV on the 1 mV sensitivity range. Final baseline
  readback was clear, but the overload remains retained as a rejected attempt and
  the excitation scan did not start. Frequency sweeps now temporarily use the
  20 mV xx sensitivity range, without changing the 4 mVrms source, and restore
  the original sensitivity only after frequency restoration and settling. This
  added SENS write requires a new explicit authorization.

## Stage 4 - attoDRY real driver

Status: offline implementation, target-computer DLL ABI preflight, and real
read-only connection validation complete (2026-08-20); setting writes remain
uncommissioned and require separate explicit authorization.

- Added safe 64-bit vendor DLL loading and explicit function signatures.
- Added separately authorized COM connection and initialization timeout.
- Added temperature, VTI, X/Z field, setpoint, control, and error readback with
  last-confirmed-state preservation.
- Added read-before-toggle idempotent temperature/field-control operations.
- Added project-limit validation, safe zero-detour coordinated vector setpoints,
  rolling stable waits, and monitored verified zeroing.
- Added fake-DLL return-code, timeout, write-authorization, path, stability, and
  vector-path contract tests before laboratory use.
- Target-computer preflight confirmed 64-bit Python, an AMD64 PE32+ vendor DLL
  version 2.0, and all 21 required exports without calling begin/connect. The
  operator-confirmed station-local COM port and DLL path are stored only in the
  ignored local configuration.
- Added an explicitly connection-authorized `attodry_test` read-only state CLI.
  It always disables setting writes, retains last-confirmed state on read failure,
  and disconnects/ends after sampling. Failed connection initialization now also
  attempts `end()` without masking the original error.
- The authorized real read-only run completed 10/10 one-second full-state reads
  with setting writes disabled. Sample temperature was 1.7251--1.7255 K and VTI
  temperature was 1.7146--1.7153 K; Bx/Bz readbacks and setpoints stayed at zero,
  temperature and field control stayed disabled, and every error code was zero.
  The run disconnected and ended normally; its raw JSON remains only on the
  ignored control-computer path.

## Stage 5 - gate SMUs and integrated acquisition

Status: model-independent offline core complete (2026-08-20); real SMU adapters
require the user's exact models, limits, and command sets.

- Added an explicitly write-authorized, model-independent gate controller with
  configured absolute-voltage limit, current compliance, stepped ramps, voltage
  readback verification, leakage trip, and best-effort zero/disable on failure.
- Added signed Vxx/I resistance using only an explicitly supplied RMS current and
  a separate current helper requiring the complete known series-path resistance.
- Added serpentine two-dimensional Vg1/Vg2 grids and an explicit user/calibration
  supplied linear gate relation for zero-electric-field-line workflows.
- Added hardware-free end-to-end retry, resume, monitor, checkpoint, normal-end
  hold/zero, Ctrl+C, and exception cleanup orchestration against SQLite.
- Added a hardware-readiness gate that rejects unresolved VISA/DLL/SMU addresses
  and all six per-gate safety values before any hardware driver can be built.

## Stage 6 - analysis and notebook migration

Status: offline implementation and rendering QA complete (2026-08-20).

- Added a URI/query-only SQLite loader that defaults to accepted attempts only;
  rejected rows require explicit audit mode.
- Added long-form CSV export, accepted-only gate-leakage loading,
  signed-resistance output gated on explicit RMS current, transport traces, and
  rectangular two-gate maps.
- Added Bx, Bz, resultant field, and signed angle-from-+Z metadata and axes.
- Added the `attodry-analyze` batch CLI and a read-only analysis notebook that
  imports no hardware-control modules.
- Added an auditable publication suite for current/harmonic/frequency/
  temperature/field/angle/gamma, T-|B|, gate-resistance, gate-leakage, and n-D
  outputs. Every generated or unsupported result is recorded in a JSON manifest.
- Required explicit complete series-path resistance and gate calibration for
  derived products; Hall, Nernst, scattering-rate, geometry, and mechanism claims
  are skipped rather than inferred from insufficient measurements.
- Rendered and visually inspected representative PNG figures with the pinned
  matplotlib analysis dependency.

## Stage 7 - laboratory commissioning and offline release

Status: offline checklist and simulation release checks complete (2026-08-20);
real laboratory commissioning and a frozen hardware wheelhouse remain pending.

- Added `LAB_COMMISSIONING.md` with operator-input gates and staged read-only,
  minimum-output, small-movement, zero-bias, and failure-injection checkpoints.
- Added `attodry-simulate`, including deliberate first-attempt unlock injection,
  raw rejection retention, retry, accepted completion, and monitor verification.
- The full offline suite covers 127 tests and passes in the minimal environment
  with one matplotlib rendering test skipped; source compilation passes without
  hardware. The plotting code is unchanged from its prior rendered validation.
- Built and import-checked the local project wheel without downloading
  dependencies; the final filename and SHA-256 are recorded in
  `PROJECT_HANDOFF.md`.
- Pending: exact SMU adapters, real-instrument checks, frozen hardware wheelhouse,
  and offline-control-computer installation verification.
