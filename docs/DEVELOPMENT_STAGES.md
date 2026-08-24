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
- A retained all-harmonic frequency attempt reached 38.3104813 kHz/h3, where
  the required 114.931 kHz detection frequency exceeds the SR830 102 kHz
  reference limit and the instrument remained at h2. The record was rejected;
  its final readback returned h1, 17.777 Hz, XX 4 mVrms/10 mV, XY 1 mV, and zero
  final status/error words, while an XY transient-unlock cleanup record retained
  the audit failure. The scanner now validates each requested
  `harmonic * frequency` product before VISA opens.
- The selected coverage policy preserves the ten-point 17.777 Hz--100 kHz grid:
  h1 at all ten points, h2 at the supported first nine, and h3 at the supported
  first eight. `--skip-unsupported-harmonics` requires `--all-harmonics`, records
  every omitted order with its required detection frequency and the 102 kHz
  limit, and never writes an unsupported HARM setting. Without that explicit
  flag, an unsupported all-harmonic grid remains a pre-VISA failure.
- A subsequent explicitly authorized target-computer execution validated the
  bounded policy with 81 clean formal xx/xy pairs (10 h1, 9 h2, and 8 h3
  conditions, each sampled three times). It completed normally, retained the
  high-frequency omissions as explicit metadata, and strictly verified cleanup
  to h1/17.777 Hz/XX 4 mVrms and 10 mV/XY 1 mV with clear status and error
  words.
- The following explicitly authorized 4--400 mVrms excitation rerun used the
  confirmed 100 kΩ external series resistor, 500 Ω approximate device
  resistance, 5 mArms and 0.5 Vrms device ceilings, and no external 50 Ω
  termination. All 99 h1/h2/h3 formal xx/xy pairs at 11 source levels completed
  cleanly. The two-interval (3.0 s) source-step settling rule was exercised on
  actual SINE OUT changes; cleanup returned the same confirmed baseline.
- The retained raw records were rendered with the completed-record/clean-sample
  analysis path. The 1 µVrms and 5-degree circular-spread defaults correctly
  retain stable XX h1 phase while suppressing low-SNR XY and higher-harmonic
  phase display values. A locked reference therefore remains necessary but not
  sufficient for phase interpretation; follow-up wiring/pickup controls are
  required before assigning physical meaning to the low-SNR phase.

- Consolidated the current frequency/excitation sweep grids, h1/h2/h3 policy,
  temporary XX range, sampling timing, complete excitation path, and device
  limits in a strict `[lockin_sweep]` hardware-TOML table (2026-08-22).
  Daily sweep commands now default to that validated config without per-run
  confirm/authorize flags. Each opened-pair attempt is atomically archived under
  the configured `run_data/commissioning` directory with its outcome and an
  address-free resolved-TOML `measurement_config`; no real instrument connection
  was made for this change.
- Daily sweeps now actively verify the configured fixed XY 1 mV range before
  formal samples. They conditionally stage it only when preflight differs,
  record the dual-range transition, reject any setup overload/unlock, and restore
  only the XX/XY ranges actually changed. `[lockin_sweep]` now also requires
  `run_name` (safe JSON filename label) and `note` (JSON audit metadata) for
  each run. Fake-VISA range/cleanup cases and the full offline suite passed:
  231 tests, 4 skipped; no real instrument was connected or written (2026-08-22).
- Completed offline dual-role range and live-status integration (2026-08-22):
  XX and XY now independently select `fixed` or opt-in `bounded_auto` in their
  own TOML tables; the daily defaults are fixed XX 20 mV and fixed XY 1 mV, and
  the deprecated sweep-level temporary-XX field is gone. Auto policy is limited
  to XX 10--20 mV or XY 1--10 mV, 0.85 occupancy, two consecutive samples before
  narrowing, and one adjustment per continuous sweep. Per-point h1 probes,
  readbacks, transitions, and cleanup restoration are auditable but excluded
  from formal curves. `monitor-live` adds a separate read-only panel for paired
  X/Y/R, phase, frequency, harmonic, sensitivity, output, and explicit latch
  status; it performs no setting writes. Fake-VISA coverage and the complete
  offline suite passed (248 tests, 5 matplotlib-dependent skips); no hardware
  resource was opened or written.
- Completed offline analysis-calibration handoff (2026-08-22): current plots now
  default to each sweep JSON's archived `measurement_config.excitation_path`,
  including the configured external-series and approximate-device resistances,
  fixed 50 ohm SR830 output resistance, and total. Legacy JSON requires an
  explicit analysis-only override, and mixed archived paths are rejected rather
  than silently combined. Targeted analysis/notebook tests passed (19 tests,
  4 matplotlib-dependent skips) and source compilation passed; no hardware
  resource was opened or written.
- Main integration keeps the temperature-only configuration loader strict while
  recognizing `[lockin_sweep]` as an unrelated optional table, so the daily
  temperature command can continue to use the same station-local TOML without
  parsing or acting on Lock-in fields. The merged full offline suite passed.
- Completed the daily Lock-in configuration clarification and settling-contract
  fix (2026-08-22). Station-specific VISA overrides remain only in ignored
  `hardware.local.toml`, so Git updates do not overwrite them. The daily guide now
  lists every accepted Lock-in/sweep field, including exact `fixed` and
  `bounded_auto` contracts and a terminal import-path check for obsolete CLI help.
  This historical timing contract was superseded on 2026-08-24 by the single
  `[lockin_sweep]` multiplier contract recorded below. The 67 Lock-in SR830 tests
  and source compilation passed; no hardware resource was opened or written.
- Updated excitation-voltage preflight to calculate the device-terminal RMS
  voltage through the confirmed series divider, using a required
  `maximum_device_resistance_ohm` that cannot be below the analysis-only
  approximate resistance. The old direct-SINE-OUT comparison was removed; a
  2 Vrms / 100 kΩ / 50 Ω / 500 Ω fake-VISA sweep verifies the resulting
  9.95 mVrms bound. No hardware resource was opened or written.
- Replaced the commissioning notebook's desktop-only file chooser with a
  remote-compatible directory selector: set one `DATA_DIRECTORY`, refresh the
  discovered records, choose frequency and excitation JSON files separately,
  then load the pair. It retains the completed/rejected and formal-sample
  filters, imports no hardware path, and requires no manually typed file path.
- Extended the remote commissioning notebook so frequency and excitation records
  load independently: it plots only the available six-figure set when one scan
  type is absent and both sets when both are selected. `clean` remains the
  default explicit quality screen; a point-level multi-select lets the operator
  remove suspect retained points without altering raw JSON. Optional export
  writes the selected files, filters, and exclusions to `selection_manifest.json`
  for reproducibility. This analysis-only path has no hardware imports.
- Added role-specific harmonic selection in `[lockin_sweep]`:
  `frequency_xx_harmonics`, `frequency_xy_harmonics`,
  `excitation_xx_harmonics`, and `excitation_xy_harmonics` independently select
  formal XX/XY h1/h2/h3 curves; `[]` excludes one role, while every scan still
  requires at least one selection. Both SR830s are set and read at the union of
  selected orders, so an unselected companion's unsafe status still rejects the
  run. Each sample records `selected_roles`; analysis honours it and creates only
  selected role/order figures. The older shared fields remain strict compatibility
  paths and cannot mix with role-specific fields. Fake-VISA, loader, template,
  and legacy-record tests passed without opening or writing a hardware resource.
- Added offline named-range sweep plans (2026-08-23): frequency and excitation grids
  accept linear `min`/`max` plus exactly one of `step`/`points`, or logarithmic
  `min`/`max`/`points` segments,
  with optional independent fixed XX/XY full-scale overrides at segment boundaries.
  Expanded points, segment metadata, range transitions, readbacks, and cleanup restoration
  are archived in schema-version 7 JSON. Legacy point arrays remain compatible; bounded-auto
  roles reject segment overrides. Configuration and fake-VISA coverage passed without
  opening or writing a real instrument.
- Corrected the remote Notebook selection workflow: clicking `Load selected
  records` now immediately loads the selected formal rows and fills the
  point-exclusion lists. The later formal-samples cell is retained only to
  refresh after a filter change, preventing a blank selector from being applied
  to zero rows. This is read-only analysis behavior with no hardware imports.
- Corrected rerunning the notebook's formal-samples cell after changing a
  checkbox: it now re-synchronizes the displayed record/status filters before
  loading, so an explicitly allowed rejected audit record is not silently
  retried with the stale completed-only/rejected-disabled filter. This remains
  read-only analysis behavior with no hardware imports.
- Documented the sweep cleanup contract: after any started sweep, XX SINE OUT is
  restored to the fixed 4 mVrms minimum (`MINIMUM_SINE_OUTPUT_V`), not to an
  arbitrary pre-scan value or a changed TOML `source_voltage_v`; the current
  preflight therefore continues to require both source fields to be 4 mVrms.
- Added the strict `[lockin_sweep].frequency_source_voltage_v_rms` setting for a
  configurable fixed XX SINE OUT amplitude during frequency scans. The value is
  safety-checked against the confirmed complete series path, device current and
  device voltage limits before VISA opens; the scan sets/reads it once, records
  the resulting source voltage and nominal current at every frequency point, and
  still restores the fixed 4 mVrms cleanup baseline. Configuration, templates,
  fake-VISA coverage, and operator documentation were updated without opening
  or writing a real instrument.
- Changed daily frequency and excitation sweeps so a requested SINE OUT value
  and its `SLVL?` readback are independently recorded rather than match-gated.
  Readback values now determine recorded nominal current and downstream analysis;
  requested values remain preserved for audit. Invalid or unsafe readbacks still
  fail closed using the configured SR830 source range and full device-path
  current/voltage checks. Fake-VISA tests cover a safe 83.2 mVrms request with an
  82 mVrms readback; no real hardware was opened.

- The project-approved daily SR830 sensitivity mapping includes 50 mV
  (`SENS 22`). `bounded_auto` remains role-limited: XX now supports the
  fail-closed three-level 10--20--50 mV ladder with at most two total,
  one-rung transitions per continuous sweep; XY remains 1--10 mV with one.
  Defaults (XX 20 mV, XY 1 mV) and all excitation/device protection limits are
  unchanged. Each range change is read back and audited, and 50 mV remains the
  largest project-approved input full scale. Pure-policy, strict-config, and
  fake-VISA two-widening cases passed as part of the 265-test offline suite
  (5 matplotlib-dependent skips); no instrument resource was opened or written.

## Stage 4 - attoDRY real driver

Status: Temperature operation operator-accepted; target-computer DLL ABI preflight
and real read-only connection validation complete (2026-08-21). Magnetic-field
writes remain uncommissioned and require separate explicit authorization.

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
- A separately authorized 1801-sample follow-up spanned 1801.803 s. Its first
  connection was rejected before sampling by a busy resource and retained for
  audit; after the GUI/other connection released the resource, the retry completed
  with empty stderr and normal disconnect/end. The run began at 1.7401 K, but the
  longest continuous in-tolerance interval was only 319.313 s. Sample readback
  reached a 1.7289 K minimum and then rose continuously to 1.9651 K over about
  25 s, excluding a single-sample spike; it ended at 1.7746 K. VTI changed only
  from about 1.717 K to 1.724 K around the event. Sample-heater output ranged
  0.0927--0.2413 W while setpoint, enabled control, zero errors, and zero field
  states stayed valid. The localized overshoot and slow decay are consistent with
  thermal delay/integral accumulation or a sample-sensor-loop issue, but the
  read-only evidence cannot distinguish PID tuning, thermal contact, and sensor
  behavior. T4 remains failed and no automatic progression or control change is
  permitted.
- After the operator confirmed that manual GUI temperature setting works, the
  commissioning failure path was tightened offline. Obsolete `hold-current` is
  rejected; `disable-control` now captures the trigger time, last confirmed full
  state (including sample/VTI temperature), and both heater powers, then uses the
  existing idempotent read-before-toggle and bounded readback checks to disable
  temperature control. PID values remain untouched.
- Added the operator-selected `max_overshoot_k=0.2 K` live guard for the requested
  1.8 K attempt. Every trigger sample is audited; a sample readback at or above
  2.0 K raises the primary failure and therefore invokes verified
  `disable-control`. The absolute threshold is validated against configured limits.
  The selected 2.0 K line is above the prior 1.9651 K peak and therefore would not
  have tripped on an excursion of the same size; no tighter margin is inferred.
- Commit `d4a6487` passed local compileall and all 170 tests (2 optional plotting
  skips), then passed compileall and all 170 tests without skips on `LK_setup`'s
  Python 3.12.13 `lyr`. The operator explicitly raised `max_delta_k` to 250 K for
  the 1.8 K attempt, effectively disabling its pre-write step gate while retaining
  the 2.0 K live cutoff. A read-only preflight showed sample 1.7242 K, prior
  setpoint 1.7000 K, control enabled, zero errors, and sample/VTI heater power
  0.0091/0.0004 W. The authorized write run recorded 1799 samples over 1800.079 s:
  sample temperature rose from 1.7241 K to a 1.7886 K maximum near 1776 s and ended
  at 1.7883 K. No sample entered the 1.79--1.81 K tolerance band or reached 2.0 K;
  setpoint/control/error invariants held throughout. Timeout diagnostics captured
  sample/VTI heater power 0.1054/0.0004 W, verified `disable-control` left the
  1.8 K setpoint with control off and error code zero, and disconnect/end succeeded.
  T4 therefore remains failed.
- Manual GUI operation then established that this controller must enable full
  temperature control before applying the sample-temperature target. The
  commissioning order now confirms idempotent control enable first and writes the
  target second. When control actually starts disabled, the target is deliberately
  reapplied even if its readback already matches, avoiding the stale 1.8 K setpoint
  left by failure cleanup. Other matching-state operations remain idempotent;
  command-order audit fields, DLL checks, the 2.0 K cutoff, and failure-disable
  behavior are preserved.
- Commit `eaa3ba0` passed local compileall/all 172 tests (2 optional plotting
  skips) and `LK_setup` compileall/all 172 tests without skips. Two resource-busy
  preflights were rejected before sampling or writes until the GUI disconnected.
  The first real run began with control already enabled and a 1.6 K setpoint, so it
  confirmed control then wrote 1.8 K without a toggle/forced reapply; 1800 samples
  over 1800.969 s reached only 1.7785 K and timed out. Its verified cleanup left
  control disabled and setpoint 1.8 K, enabling an exact second test. That audit
  records initial control false and forced reapply true: it toggled/confirmed
  control, then resent 1.8 K. Across 1799 samples and 1800.016 s, temperature rose
  from 1.7254 K to 1.7893 K, about 10.8 mK higher than the first run, but never
  reached the 1.7900 K tolerance edge. No sample reached 2.0 K; setpoint, control,
  and zero errors held during both waits. Both timeouts verified control disabled,
  retained setpoint 1.8 K, and disconnected normally. T4 remains failed.
- On 2026-08-21 the operator accepted the Temperature module under the experiment's
  operational criterion: confirmed control-first setpoint application, measurable
  heater-driven warming, and storage of the actual sample temperature are sufficient;
  entering the former strict stability window within 30 minutes is diagnostic rather
  than a stage gate. T4 is therefore operator-accepted. The commissioned
  `max_overshoot_k` is 0.2 K, and Integration must persist actual
  `sample_temperature_k` with each measurement instead of treating the setpoint as
  the measured temperature. The historical stability failures above remain valid.
- Added the operator-requested daily `attodry-temperature-run` entry point. Its
  complete runtime configuration is the strict `[temperature_run]` table in
  `hardware.local.toml`; normally only `target_k` changes. Invoking the command
  itself authorizes its connection and temperature writes, so it has no separate
  authorization flags. It confirms control enabled before applying the target,
  records complete actual-temperature samples for 1800 s, then returns the actual
  measurement state without requiring the former strict stability window. A
  sample at `target_k + 0.2 K`, control loss, setpoint change, device error, or
  communication failure prevents readiness and attempts verified control disable.
  Fake-DLL tests cover the virtual 1800 s path, actual-temperature recording,
  command order, and overshoot cleanup without loading real hardware.
  Commit `a20fa3f` then passed compileall, all 217 tests with no skips, and the
  new command help on `LK_setup` using 64-bit Python 3.12.13 `lyr`. Only Git,
  Python compilation, unittest, and `--help` ran; no vendor DLL was loaded and
  no connection or hardware command was issued.
  Follow-up commit `d045421` isolates this daily loader from unrelated Lock-in
  and SMU table completeness while retaining strict validation of every
  temperature-relevant table and rejecting unknown top-level tables. It passed
  compileall and all 218 tests without skips on the same target environment.
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
- The merged main/Lock-in/Temperature offline suite contains 218 passing tests
  in the minimal environment, with three matplotlib rendering tests skipped
  because matplotlib is unavailable; source compilation passes without hardware.
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

## Stage 7 follow-up - Lock-in safety policy and sweep readback robustness

Status: offline implementation complete (2026-08-23); no hardware was opened.

- Added versioned `config/lockin_safety.toml`, automatically loaded beside every
  hardware TOML. It owns the project full-scale allowlists, bounded-auto ladders,
  0.85 occupancy target, two stable samples, source bounds, and cleanup amplitude;
  daily sweep timing is configured separately in `[lockin_sweep]`, while the
  complete SR830 hardware mapping remains separate.
- Daily `sweep-frequency` and `sweep-excitation` now run directly after TOML loading;
  `validate-config` is an optional offline summary and never a prerequisite or VISA
  connection step. Sweep JSON records include the resolved policy and hash.
- Sweep frequency records requested, XX actual, and both raw readbacks; analysis
  defaults to the XX actual frequency. Harmonic eligibility uses the higher of
  requested and both actual frequencies. Numeric display-bin differences are not
  rejected; non-finite/out-of-range values, unlock, overload, and instrument-error
  checks remain fail-closed. Offline configuration, fake-VISA, record, analysis,
  and boundary tests cover the behavior.
- Updated the sweep policy after observed SR830 display quantization: requested,
  XX, and XY frequency values are now recorded without numeric mismatch rejection.
  Only non-finite/out-of-range frequency values and the independent lock, overload,
  error, and unsafe-transition checks fail closed. Both sweeps clear pending VISA
  responses before their first query and again before cleanup after an abort; the
  new read/write-free `recover-interface` command provides manual recovery after a
  hard interruption. The JSON schema was version 9 for the clear-audit release;
  the current reserve-aware sweep record is version 10.
- Added semantic SR830 `reserve_mode` configuration and versioned safety-policy
  allowlists (schema 10). `high_reserve`, `normal`, and `low_noise` map to RMOD
  0/1/2; the checked-in daily policy permits `normal` until separate hardware
  confirmation expands it. Sweep records target/readback/original RMOD values and
  restores any changed mode after lowering SINE OUT. A first HARM-transition
  input/reserve-overload-only latch is retained as a discarded candidate and gets
  one additional settled verification read; a repeated latch or any other unsafe
  bit remains fail-closed. Fake-VISA coverage and the complete offline suite passed;
  no hardware resource was opened or written.
- Documented the SR830 reserve gain-distribution model (2026-08-24): Reserve dB
  is the dynamic-interference ratio and post-demodulation DC-gain allocation, not
  extra total measurement gain. The daily and module guides now include the
  sensitivity-dependent table, a 20 mV worked example, SNR distinction, and
  fail-closed mode-selection guidance. This documentation-only change did not
  alter configuration, code, policy allowlists, or hardware state.
- Merged the operator-supplied station sweep profile into
  `config/hardware.example.toml`: fixed XX 1 V, fixed XY 10 mV, 4 mV--400 mV
  linear plus 0.45--5 V linear excitation segments, excitation XX h1 only with
  XY h1/h2/h3, `test145degree`/`45degree` audit metadata, and 100/150 ohm
  approximate/maximum device resistance. The XX 1 V full scale is now explicitly
  present in the safety allowlist. Local VISA addresses remain placeholders in the
  tracked example by repository policy and belong only in ignored
  `hardware.local.toml`. Configuration validation and the full fake-VISA suite pass.
- Updated bounded-auto and sweep status handling (2026-08-24): the user-facing
  `autorange_max_steps` field was removed; the versioned safety ladder now defines
  adjacent transitions. A point may widen repeatedly (10→20→50 mV) when the new
  range remains above 0.85, while narrowing still requires two consecutive fits.
  LIAS bit 2 (`output_overload`) is retained as raw audit data but ignored for sweep
  acceptance/autorange because CH1/CH2 output is unused. Bit 0 and bit 1 candidates
  receive one settled recheck; repeated overload, unlock, or instrument errors remain
  fail-closed. Sweep measurement schema is now version 11; all verification was
  offline with fake VISA only.
- Documented the complete SR830 low-pass filter-slope choices: 6/12/18/24 dB/oct
  (`OFSL` 0/1/2/3). The current project remains intentionally fixed at 24 dB/oct;
  changing it requires synchronized driver, safety-policy, test, and settling-time
  updates plus fresh hardware confirmation.
- Simplified daily sweep timing (2026-08-24): removed user-facing `settle_s`,
  per-role settling multipliers, and all `lockin_safety.toml` timing settings.
  Each role retains only `time_constant_s` (confirmed 0.3 s or 1.0 s). The single
  `[lockin_sweep].settle_time_constants` value (minimum 5.0) and
  `sample_interval_time_constants` derive seconds from the slower role. Every
  record now archives the slowest time constant, transition interval, two-interval
  post-setting wait, and repeat-sample spacing in schema version 12. Repeated
  samples are documented as stability readings, not statistically independent
  replicas. Configuration, fake-VISA, and full offline test coverage passed; no
  hardware resource was opened or written.
- Simplified Reserve configuration (2026-08-24): removed
  `allowed_reserve_modes` from the safety policy and its duplicate validation.
  A role selects its valid SR830 Reserve mode directly in `hardware.local.toml`;
  every actual RMOD change still lowers SINE OUT first, verifies readback/status,
  is audited, and is restored during cleanup.
