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
  overloads or instrument errors and final restoration was fully verified. An
  initial 50 ppm relative tolerance applied only to frequency-sweep external
  readback jitter; all unlock/error checks and the established harmonic-path
  tolerance remain unchanged. Another real retry is pending authorization.
- The next authorized retry stopped at 50 Hz on a real XX output-overload latch:
  Vxx had risen to about 1.09 mV on the 1 mV sensitivity range. Final baseline
  readback was clear, but the overload remains retained as a rejected attempt and
  the excitation scan did not start. Frequency sweeps now temporarily use the
  20 mV xx sensitivity range, without changing the 4 mVrms source, and restore
  the original sensitivity only after frequency restoration and settling. This
  added SENS write requires a new explicit authorization.
- The SENS-authorized retry first found a stale XY overload latch during preflight
  before any write; a separate 10-sample read-only recovery was fully clear. The
  same authorized run then accepted every formal point through 200 Hz using the
  temporary 20 mV XX range. At the 282 Hz transition read, XY returned `LIAS=26`
  (filter overload, reference unlock, and frequency range changed), so no formal
  282 Hz sample was taken. Cleanup fully verified 17.777 Hz, 4 mVrms, the original
  1 mV XX range, and clear status/error words, and the excitation scan did not
  start. Transition-only overload latches are now retained and consumed alongside
  unlock/range-change latches before a second settling interval; the unchanged
  formal window still rejects every unlock, overload, or instrument error. A new
  explicit authorization is required for this revised behavior.
- The next authorized retry passed 25 and 35.5 Hz, then rejected only the second
  formal 50 Hz sample because the locked, overload-free, error-free XY readback
  was 49.9973 Hz, 2.7 mHz or 54 ppm low. Cleanup fully verified the original
  17.777 Hz/4 mVrms/1 mV state and clear status/error words, and the excitation
  scan did not start. The sweep-only tolerance is now 100 ppm, leaving measured
  margin above the retained 31 and 54 ppm jitter while preserving strict formal
  unlock, overload, and error rejection. A new explicit authorization is required.
- The authorized 100 ppm retry completed every formal frequency point through
  1 kHz and fully verified baseline restoration. The excitation path then had one
  stale XY preflight overload latch before any write; its 10-sample recovery was
  fully clear. The retry acquired all 11 points from 4 to 400 mVrms, and all 33
  formal samples had zero status/error bits and no problems. At 400 mVrms, nominal
  current was 3.958 uArms and mean Vxx/Vxy R values were about 5.384 mV/1.748 uV.
  Cleanup restored 4 mVrms and the original 1 mV XX range, but its immediate final
  read retained an XX `LIAS=4` output-overload latch from the range restoration,
  so the raw run remains rejected; the following 10-sample read-only record was
  fully clear. Cleanup now records and consumes only XX overload latches during
  the sensitivity transition, waits again, and retains strict final status checks.
  A new explicit authorization is required for a completed excitation record.
- The authorized cleanup-aware retry completed all 11 excitation points and all
  33 formal samples with zero status/error bits and no problems. At 400 mVrms,
  nominal current was 3.958 uArms and mean Vxx/Vxy R values were about
  5.363 mV/1.748 uV. Cleanup recorded the expected XX-only `LIAS=4` transition
  latch while XY remained clear, then strictly verified 17.777 Hz, 4 mVrms, the
  original 1 mV XX sensitivity, and zero final status/error words on both units.
  Frequency and excitation device-only commissioning is complete.
- Completed the Lock-in L0--L3 offline module on `codex/module-lockin`. The
  user-confirmed policy fixes XY at 1 mV and bounds XX to 10--20 mV with 0.85
  target occupancy, two consecutive fit samples before narrowing, and one
  adjustment per condition preflight. The pure decision state machine fails
  closed at the widest bound. Its fake-VISA transition executor requires separate
  write and latch-consumption authorization, exact readback, two five-time-
  constant waits, retained transition/verification samples, and freezes the
  formal range. Failure minimizes excitation and attempts range restoration.
  No real VISA resource was opened.
- Completed Lock-in L4 target-offline validation on `LK_setup`. Commit `2199460`
  was cloned into a dedicated Documents directory and validated with the target
  `lyr` Python 3.12.13. With the clone's `src` explicitly set on `PYTHONPATH` to
  avoid an unrelated legacy editable install, all 166 tests and source compilation
  passed. A 79,704-byte no-dependency/no-isolation wheel
  (`c5ffe7d7daf3c59796a46f4263916162092164aeee902840a9fdde1a843c479c`) contained
  no local hardware configuration, DLL, run-data, SQLite, or secret file. No VISA
  resource was opened.
- Completed Lock-in L5 real read-only commissioning under a scope limited to
  non-latch-clearing queries. An ignored strict local TOML was created only in the
  dedicated L4 clone by copying the existing station-local config and appending
  user-confirmed Lock-in fields; its legacy source remained unchanged. Distinct
  SR830 identities, semantic roles, TTL rising edge, A-B, Float, AC, 300 ms,
  24 dB/oct, and XY 1 mV (`SENS=17`) matched. XX still read back 1 mV
  (`SENS=17`) rather than the new policy's 10 mV start (`SENS=20`); it was retained
  as a configuration mismatch without a write. Raw output remains only in ignored
  target `run_data`. No setting command, `APHS`, `LIAS?`, or `ERRS?` was sent, so
  the run is read-only commissioned but does not establish latch-clear status.
- Completed the fixed-start portion of Lock-in L6 on `LK_setup`. The dedicated
  commit `77e7d7e` clone strictly parsed the local policy, passed all 170 offline
  tests and source compilation, then ran `set-xx-sensitivity` under explicit
  write, latch-consumption, and physical-XY-disconnection authorization. Its
  first preflight retained and rejected an XY overload latch before any write.
  A subsequent ten-sample, latch-consuming, read-only recovery was completely
  clear; the single authorized retry then wrote only XX `SENS 20`. Its preflight,
  transition, and formal verification windows were all lock/overload/error-free;
  XY remained `SENS=17` and both phase settings were unchanged. Raw accepted and
  rejected audit files remain only in ignored target `run_data`. The real bounded
  auto-range narrowing branch remained pending a separately scoped authorization.
- Added the separately gated L6 narrowing-branch commissioning command offline.
  It starts only from the verified 10 mV XX baseline, temporarily stages XX at
  20 mV, requires two real safe maximum-range samples to produce the policy's
  `KEEP` then `NARROW` decisions, and returns only XX to 10 mV. Every state
  window reads both instruments and consumes status latches; the final window
  requires all status bits clear. No XY write or `APHS` path exists. Unsafe
  samples or any nonzero unapproved latch retain raw output and restore 4 mVrms/
  10 mV. Fake-VISA authorization, success, unsafe-sample, and nonzero-latch
  cases passed locally. A new isolated `LK_setup` clone at commit `d1e6201`
  strictly parsed the policy, passed all 174 offline tests and source compilation,
  then completed the explicitly authorized real narrowing run. XX read back
  `SENS 20 -> 21 -> 20`; its two real fit samples generated `KEEP` then `NARROW`.
  XY remained `SENS=17`, every status/error window was clear, and both phase
  settings were unchanged. Raw audit files remain only in ignored target
  `run_data`. The threshold/overload-triggered widening branch was not induced
  and requires a new authorization when a real qualifying condition exists.
- Repeated the authorized device-only frequency scan from 17.777 Hz to 100 kHz
  at ten logarithmic points and three formal samples per point. Two strict,
  rejected attempts exposed SR830 readback quantization at 316.159 Hz and
  5622.802 Hz; cleanup was fully verified after each. Keeping the 100 ppm
  acceptance rule unchanged, those two requested values were replaced with the
  observed 316.1 Hz and 5622 Hz quantization points (each about 0.02% from the
  mathematical grid). The final scan accepted all 30 samples and restored
  17.777 Hz, 4 mVrms, XX `SENS=20`, XY `SENS=17`, and zero status/error words.
- Completed the separately authorized 4–400 mVrms excitation sweep at the
  17.777 Hz baseline. The operator-confirmed 100 kΩ series resistance, 500 Ω
  approximate device resistance, 5 mArms current limit, 0.5 Vrms voltage limit,
  and absence of external 50 Ω termination gave pre-VISA conservative bounds of
  3.998 µArms and 0.4 Vrms. All 11 points and 33 formal samples were accepted.
  The temporary XX `SENS=21` setting and SINE OUT changes were fully restored to
  4 mVrms and `SENS=20`; XY remained `SENS=17` without writes, and final
  status/error words were clear. The raw audit record remains ignored on the
  control computer.
- Extended the read-only SR830 commissioning analysis and notebook with explicit
  record/sample filters, Browse support, and complete excitation-path resistance
  controls. It now produces six separate XX/XY × h1/h2/h3 twin-axis figures for
  each frequency and current--voltage scan, never combining or inventing missing
  harmonics. Current is calculated from recorded SINE OUT RMS voltage and the
  explicit external-series/SR830-output/device-resistance path; the current
  notebook defaults are 100000/50/500 Ω. No hardware module is imported or
  connected. Loader, current-calibration, notebook-syntax, and matplotlib-render
  checks passed.
- Replaced the notebook's manual Browse switch with an interactive `Browse…`
  button and visible `Only completed records` checkbox, plus formal-sample
  status multi-select and rejected-audit checkbox. Selecting a JSON file changes
  the catalog to its directory so the paired sweep can be found without copying
  raw data into the analysis clone. The controls remain read-only.
- Added an offline-validated, opt-in `--all-harmonics` mode for the existing
  device-only frequency and excitation sweeps. The default remains h1-only;
  the flag records h1/h2/h3 at every point, waits after each paired h2/h3
  setting, and restores both instruments to h1 before the next point and during
  cleanup. Fake-VISA tests cover success and a rejected h2 overload with retained
  partial data, 4 mVrms cleanup, and h1 restoration. Real execution remains
  pending a fresh physical-confirmation and write authorization.
- The first authorized 10-point real all-harmonic frequency attempt retained
  22 formal pairs and stopped at 121.122062 Hz/h2 when XX reported `LIAS=18`
  (filter overload plus frequency-range change) and XY `LIAS=16` (frequency
  range change). It did not continue to excitation; cleanup strictly restored
  h1, XX 4 mVrms/10 mV/17.777 Hz, XY 1 mV, and clear final status/error words.
  The revised offline path records, consumes, and re-settles only these two
  expected HARM-transition latches; it still rejects unlock, input/reserve or
  output overload, time-constant change, error, and every nonzero formal-window
  latch. Fake-VISA success, formal h2 failure cleanup, and observed-transition
  tests pass. A fresh real authorization is required before retrying.
- Added offline-tested phase-quality display handling for the read-only
  commissioning notebook: it retains raw circular phase statistics, omits only
  display points below an explicit 1 µVrms amplitude or above a 5-degree
  within-point circular spread, and unwraps qualified contiguous segments at
  ±180 degrees. The controls are visible and adjustable; raw JSON/CSV values are
  never changed. The excitation-sweep acquisition path now rejects `--settle-s`
  below 1.5 s before VISA opens and records a two-interval source-step wait
  (3.0 s at the current 300 ms/24 dB/oct configuration) after each actual SINE
  OUT change. This was tested only against fake VISA and offline matplotlib;
  no new hardware command was issued.

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
- Completed the Temperature-module T0 contract audit and T1 offline behavior
  tests. Control flags now accept only explicit 0/1 values, temperature setpoint
  writes require a full post-write state/error/readback confirmation, invalid
  wait targets fail before polling, and a disabled-control interval resets the
  continuous stability window. Read and communication failures preserve the
  prior `last_confirmed_state`.
- Added a separate dual-authorization smallest-temperature-movement CLI for the
  future T4 commissioning run. It requires explicit target, maximum setpoint
  delta, stability criteria, timeout, and success/failure policies; records every
  target/restoration sample and action; and never claims successful recovery or
  disconnect after a failed read or close. Fake-DLL tests cover all policies and
  authorization/limit gates. No real connection or setting write was performed.
- T2 target validation has only confirmed the `LK_setup` `lyr` interpreter as
  64-bit Python 3.12.13. The target has no project copy, and the source/test
  snapshot was not transferred without separate authorization, so T2 remains
  pending.

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
- Added a dedicated read-only SR830 commissioning browser/analysis module and
  notebook. It opens JSON/JSONL files through directory discovery or a native
  Windows Browse dialog, defaults to completed records and clean formal samples,
  requires explicit rejected-data audit opt-in, filters unsafe sample statuses,
  excludes transition/cleanup payloads from curves, and plots/exports XX/XY
  X/Y/R/phase statistics for frequency and excitation scans. UTF-8 and
  PowerShell UTF-16/BOM records are detected automatically; displayed figures
  are closed after rendering so executed notebooks contain one copy per plot.
- Added `xy_sweep_analysis` and `sr830_xy_sweeps.ipynb` for XY-only frequency
  and excitation-amplitude figures. XX is discarded at load time; both sweep
  types remain available and every figure identifies the harmonic order.
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
- The merged main/Lock-in offline suite contains 197 passing tests in the minimal
  environment, with three matplotlib rendering tests skipped because matplotlib
  is unavailable; source compilation passes without hardware.
- Built and import-checked the local project wheel without downloading
  dependencies; the final filename and SHA-256 are recorded in
  `PROJECT_HANDOFF.md`.
- Added `docs/modules/` work packages for independent Lock-in, Temperature,
  Magnetic-field, and Integration Chat follow-up. Each package records its
  current real-hardware boundary, goals/non-goals, staged acceptance criteria,
  file ownership, safety cautions, and a copyable startup prompt. The Lock-in
  package converts the completed bench-test experience into explicit rules for
  wiring, phase preservation, settling, sensitivity transitions, latch handling,
  frequency tolerance, sequential pair reads, and cleanup. This is a planning
  and handoff deliverable only; it does not commission any new hardware writes.
- Pending: exact SMU adapters, real-instrument checks, frozen hardware wheelhouse,
  and offline-control-computer installation verification.
