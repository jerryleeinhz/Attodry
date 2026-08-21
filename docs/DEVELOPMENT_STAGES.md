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

## Stage 4 - attoDRY real driver

Status: offline implementation, target-computer DLL ABI preflight, and real
read-only connection validation complete (2026-08-21); setting writes remain
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
  with `writes_authorized=false`. Sample temperature was 1.7242--1.7246 K and
  VTI temperature was 1.7138--1.7143 K; the user setpoint remained 2.0 K,
  Bx/Bz readbacks and setpoints stayed at zero, temperature and field control
  stayed disabled, and every error code was zero.
  The run disconnected and ended normally; its raw JSON remains only on the
  ignored control-computer path.
- Completed the Temperature-module T0 contract audit and T1 offline behavior
  tests. Control flags now accept only explicit 0/1 values, temperature setpoint
  writes require a full post-write state/error/readback confirmation, invalid
  wait targets fail before polling, and a disabled-control interval resets the
  continuous stability window. Read and communication failures preserve the
  prior `last_confirmed_state`.
- Added a separate dual-authorization smallest-temperature-movement CLI for the
  future T4 commissioning run. It requires explicit target, maximum sample-sensor
  movement, stability criteria, timeout, and success/failure policies; records every
  target/restoration sample and action; and never claims successful recovery or
  disconnect after a failed read or close. Fake-DLL tests cover all policies and
  authorization/limit gates. No real connection or setting write was performed.
- Added an ignored local per-attempt temperature-commissioning TOML template so
  target, step, stability, timeout, and policy values are editable together
  without changing Python or the hardware-address TOML. The T4 CLI accepts either
  that strict template or all direct options, never both; placeholders, malformed
  fields, and mixed sources fail before DLL loading. The parameter file contains
  no authorization, so both connection and setting-write flags remain mandatory.
- Revalidated parameter-file support on `LK_setup` for commit `609b456` with
  64-bit Python 3.12.13 in `lyr`: all 159 offline tests passed with no skips,
  source compilation passed, and `temperature_test --help` showed the new
  parameter-file option. Only Git, unittest, compileall, and help output ran;
  the vendor DLL was not loaded and no `begin/connect` or hardware command was
  issued. The verified temporary clone was removed.
- Recorded the operator-selected T4 candidate values in the ignored
  `config/temperature_commissioning.local.toml`: 1.75 K target, 0.05 K maximum
  sample-sensor movement, 0.01 K tolerance/range, 600 s dwell, 1 s polling,
  1800 s timeout, `hold-target` success, and `hold-current` failure. The movement
  gate now compares the target with the initial sample-temperature sensor reading;
  the initial user-setpoint delta is retained separately in the raw audit record.
  Local compilation, all 34 attoDRY tests, and all 160 project tests passed
  (2 optional plotting tests skipped). This was offline only; no DLL was loaded
  and no connection or hardware command was issued.
- Revalidated commit `b64eb74` on `LK_setup` with 64-bit Python 3.12.13 in
  `lyr`: source/test compilation and all 160 offline tests passed with no skips.
  Only Git, compileall, and unittest ran; the vendor DLL was not loaded and no
  `begin/connect` or hardware command was issued. The exact temporary clone path
  was verified before removal, and cleanup was confirmed.
- The first explicitly authorized real T4 attempt passed the 0.05 K sensor-movement
  gate from an initial 1.7237 K sample reading and sent one 1.75 K setpoint write.
  Its immediate setpoint readback remained 2.0 K, so the command failed closed
  before enabling temperature control, recorded the final confirmed unchanged
  state, and disconnected normally. A subsequent authorized five-sample read-only
  check confirmed that the DLL had asynchronously applied the 1.75 K setpoint;
  sample temperature remained 1.7240--1.7241 K, temperature control remained
  disabled, and every error code was zero.
- Updated setpoint confirmation for the observed asynchronous DLL behavior:
  an already confirmed identical setpoint is idempotent, while a new setpoint is
  polled through complete state/error reads for at most 30 s using the configured
  temperature polling interval. Failure still preserves the last confirmed state.
  A second attempt then sent one temperature-control toggle; its immediate flag
  readback remained disabled, so it also failed closed and disconnected normally.
  Five later read-only samples confirmed the control flag had asynchronously become
  enabled with the 1.75 K setpoint and zero errors. The same bounded acknowledgement
  polling now covers temperature-control toggles without changing field-control code.
  Local compilation, all 38 attoDRY tests, and all 164 project tests passed
  (2 optional plotting tests skipped).
- Revalidated commit `aaafabc` on `LK_setup` with 64-bit Python 3.12.13 `lyr`:
  compileall and all 164 offline tests passed with no skips before the final real run.
- The final authorized T4 run began with the 1.75 K setpoint and temperature control
  already confirmed, so the idempotent path sent no redundant setpoint or toggle.
  It recorded 1799 complete samples through 1800.187 s and timed out: sample
  temperature was 1.7237--1.7251 K (about 1.7240 K first and 1.7250 K last), and
  zero samples entered the 1.75 +/- 0.01 K band. Every sample retained the 1.75 K
  setpoint, enabled temperature control, and zero error code. `hold-current` sent
  no recovery action; the final confirmed state remained 1.75 K/control enabled,
  and disconnect/end completed normally. Raw records remain only on ignored target
  paths. T4 is not commissioned; manual front-panel/GUI temperature-mode and heater
  response verification is required before any further automated retry.
- Manual GUI readback then confirmed the sample-heater configuration is present:
  5.00 W maximum power, 115.00 ohm heater resistance, and 3.00 ohm wire resistance.
  Added the two vendor heater-power getters to the connection-authorized read-only
  CLI, with explicit `sample_w`/`vti_w` output and rejection of DLL errors,
  non-finite values, or negative power. Local compileall, all 40 attoDRY tests, and
  all 166 project tests passed (2 optional plotting tests skipped). On `LK_setup`,
  64-bit Python 3.12.13 `lyr` passed compileall and all 166 tests with no skips;
  loading the vendor DLL confirmed all 23 required exports without begin/connect.
  The first authorized read-only connection was rejected before sampling because
  the GUI held the resource; that stderr record was retained. After GUI Disconnect,
  a fresh authorized record completed 10/10 samples with writes disabled and normal
  disconnect/end. Sample-heater output was 0.2036--0.2037 W and VTI-heater output
  0.0004 W; sample temperature was 1.7335--1.7340 K while setpoint remained 1.75 K,
  temperature control remained enabled, all error codes were zero, and field
  readbacks/setpoints remained zero. This rules out zero heater output but does not
  establish temperature stability or PID correctness from a ten-second record.
- A following GUI-disconnected, connection-authorized 601-sample read-only monitor
  spanned 600.622 s and disconnected normally with an empty stderr record. It sent
  no write: sample temperature rose from 1.7342 to 1.7369 K (range
  1.7335--1.7372 K; 3.70 mK peak-to-peak), while the 1.75 K setpoint, enabled
  temperature control, zero errors, and zero field readbacks/setpoints persisted.
  Sample-heater power was 0.2106--0.2217 W and VTI-heater power 0.0004 W. The
  range/control portions of the criterion passed, but all 601 samples were below
  the 1.74 K tolerance lower bound. T4 stability therefore remains failed; do not
  claim progression or alter PID/heater settings without a separately authorized
  manual diagnosis or control change.
- Completed Temperature T2 target-offline validation for commit `e9a7b8c` on
  `LK_setup` with 64-bit Python 3.12.13 in `lyr`: 35 temperature tests and all
  156 offline tests passed with no skips, and source compilation passed. Only
  Git, unittest, and compileall ran; the vendor DLL was not loaded and no
  `begin/connect` or hardware command was issued. The temporary clone was removed.

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
- The full offline suite covers 156 tests and passes in the minimal environment
  with two matplotlib rendering tests skipped; source compilation passes without
  hardware. The plotting code is unchanged from its prior rendered validation.
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
