# Project handoff

Last updated: 2026-09-01

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
- Completed the Lock-in L0--L3 offline module on `codex/module-lockin`. XY is
  fixed at 1 mV; XX starts at 10 mV and is bounded at 20 mV with target occupancy
  0.85, two consecutive fit samples before narrowing, and one adjustment per
  condition preflight. The pure policy is deterministic and fail-closed. The
  fake-VISA transition path separately gates `SENS` writes and `LIAS?/ERRS?`
  consumption, performs exact readback and two five-time-constant waits, retains
  transition/verification samples, and freezes the formal range. Failure lowers
  excitation to the software minimum and attempts sensitivity restoration. No
  real VISA resource was opened or command sent for this offline stage.
- Completed Lock-in L4 target-offline validation on `LK_setup`. Commit `2199460`
  was cloned into a dedicated Documents directory and run with `lyr` Python
  3.12.13. Setting the clone's `src` on `PYTHONPATH` was necessary to avoid an
  unrelated legacy editable install; with that isolation, all 166 tests and source
  compilation passed. The 79,704-byte wheel built without dependencies or build
  isolation had SHA-256
  `c5ffe7d7daf3c59796a46f4263916162092164aeee902840a9fdde1a843c479c` and contained
  no local hardware configuration, DLL, run-data, SQLite, or secret file. No VISA
  resource was opened.
- Completed the explicitly authorized Lock-in L5 real read-only diagnostic on
  `LK_setup`. The dedicated clone received an ignored local TOML copied from the
  existing station-local file plus the user-confirmed strict Lock-in fields; the
  legacy local file was not changed and the new one parsed strictly. The two VISA
  resources returned distinct SR830 identities. Roles, TTL rising edge, A-B,
  Float, AC, 300 ms, 24 dB/oct, and XY 1 mV (`SENS=17`) matched the contract.
  XX instead read back 1 mV (`SENS=17`) while the new bounded policy expects its
  10 mV start (`SENS=20`); the mismatch was recorded and not corrected. The raw
  diagnostic and empty stderr file remain only under ignored target `run_data`.
  No setting command, `APHS`, `LIAS?`, or `ERRS?` was sent, so latch status is
  explicitly unknown. A separate L6 write authorization is required before any
  `SENS` change.
- Completed the fixed-start portion of Lock-in L6 on the isolated `LK_setup`
  commit `77e7d7e` clone. Its strict policy parsed and all 170 offline tests plus
  source compilation passed before connection. The explicitly authorized first
  preflight retained an XY overload latch and therefore failed closed with zero
  setting writes. Ten subsequent latch-consuming, read-only recovery samples were
  all clear. One authorized retry then wrote only XX `SENS 20`; all preflight,
  transition, and formal status windows were clear, XY remained at `SENS=17`, and
  both `PHAS?` settings remained unchanged. Raw accepted and rejected audit files
  remain solely under ignored target `run_data`; neither XY writes nor `APHS` were
  sent. The real bounded-auto narrowing branch remained uncommissioned and needed
  a distinct authorization.
- Added an offline-only, separately authorization-gated L6 command to commission
  the two-safe-sample narrowing branch without raising excitation or changing
  frequency. From a verified XX 10 mV state it temporarily stages only XX at
  20 mV, records full dual-SR830 status windows, requires the deterministic
  `KEEP` then `NARROW` decisions, and returns only XX to 10 mV. Any unsafe sample
  or nonzero unapproved status latch fails closed to XX 4 mVrms/10 mV cleanup;
  XY is never written and `APHS` is absent. Fake-VISA cases cover the three
  authorizations, success, unsuitable samples, and an unexpected transition
  latch. A new isolated `LK_setup` clone at commit `d1e6201` strictly parsed the
  policy, passed all 174 offline tests and source compilation, then completed the
  explicitly authorized real command. XX read back `SENS 20 -> 21 -> 20`; two
  real maximum-range fit samples gave the required `KEEP` then `NARROW` decisions.
  XY remained `SENS=17`, every status/error window was clear, and both phase
  settings stayed unchanged. Raw audit files remain only under ignored target
  `run_data`. No excitation increase or overload was induced, so the
  threshold/overload-triggered widening branch alone remains uncommissioned and
  requires a new explicit authorization when a real qualifying condition exists.
- Repeated the authorized device-only frequency scan through 100 kHz with ten
  logarithmic points and three formal samples per point. The initial strict
  attempts rejected readback quantization at 316.159 Hz and 5622.802 Hz but fully
  restored the baseline each time. The final accepted grid retained the 100 ppm
  rule and substituted only the observed 316.1 Hz and 5622 Hz quantization
  points (about 0.02% deviations); all 30 samples passed and cleanup verified
  17.777 Hz, 4 mVrms, XX `SENS=20`, XY `SENS=17`, and clear status/error words.
- Completed the user-authorized 4–400 mVrms device excitation sweep at fixed
  17.777 Hz. The confirmed 100 kΩ series resistor, approximately 500 Ω device,
  5 mArms current cap, 0.5 Vrms voltage cap, and no external 50 Ω termination
  yielded conservative preflight bounds of 3.998 µArms and 0.4 Vrms. Eleven
  points with three formal samples each completed without sample problems. Only
  the temporary XX `SENS=21` and SINE OUT setting changed; cleanup verified the
  4 mVrms/`SENS=20` XX baseline, unchanged XY `SENS=17`, and zero final
  status/error words. Raw acquisition files remain ignored on the target clone.
- Extended the offline, read-only Lock-in commissioning analysis. The main sweep
  notebook now begins with record/sample filters, Browse, and explicit complete
  excitation-path resistance controls (currently 100000 Ω external series, 50 Ω
  SR830 output, and 500 Ω approximate device). It produces separate XX/XY ×
  h1/h2/h3 twin-axis figures for frequency and SINE OUT-current scans, retaining
  phase on the right axis and avoiding all inferred/mixed missing harmonics.
  The loader prefers recorded SINE OUT readback and only uses the old frequency
  setpoint where no readback was recorded. This analysis-only work imports no
  hardware path and has passed loader, notebook, and matplotlib-render checks.
- The commissioning notebook now exposes its native-file `Browse…` button and
  completed-record/status filters as visible Jupyter controls. Selecting a file
  switches catalog discovery to its raw-data directory while leaving the clone
  and all instruments untouched.
- Added an offline-tested `--all-harmonics` switch for the device-only sweeps.
  It is opt-in (the existing default remains h1-only) and records h1/h2/h3 at
  every point with paired HARM writes, settling, strict status rejection, and
  h1/4 mVrms cleanup. Fake-VISA success and h2-failure recovery pass; the new
  real sweep still needs current physical confirmation and write authorization.
- The first authorized all-harmonic frequency retry recorded 22 formal pairs
  before XX `LIAS=18` and XY `LIAS=16` stopped it at 121.122062 Hz/h2. These are
  retained as a rejected formal sample; excitation did not start. Cleanup was
  fully verified at h1, XX 4 mVrms/10 mV/17.777 Hz, XY 1 mV, and zero
  status/error words. The revised offline path separates the observed HARM
  transition's filter-overload/frequency-range latches into discarded records,
  consumes them, and waits again before unchanged strict formal sampling. Any
  other transition problem and every formal nonzero safety bit remain failures;
  fresh write authorization is required before its real retry.
- Added a read-only phase-quality view to the commissioning notebook. It retains
  all raw circular phase statistics, but the displayed phase defaults to R at
  least 1 µVrms and within-point circular spread at most 5 degrees; qualifying
  contiguous sections unwrap at ±180 degrees without bridging omitted points.
  Both controls are visible and can be set to `0.0`/`None` for raw-phase audit.
  No phase setting or raw record is modified. The then-current offline-only
  acquisition correction required `--settle-s >= 1.5` s before VISA opens and
  waited two intervals after every actual SINE OUT change (3.0 s at the current
  300 ms, 24 dB/oct setting), recording `source_step_settle_s` in the JSON. It
  was superseded by the schema-12 automatic multiplier contract below. Fake-VISA
  and offline matplotlib tests cover the behavior; no new hardware command was
  issued.
- A retained all-harmonic scan reached 38.3104813 kHz/h3, which requires
  114.931 kHz and exceeds the SR830 102 kHz reference limit; XX correctly
  remained at h2. The attempt is retained as rejected. Its final readback was
  h1, 17.777 Hz, XX 4 mVrms/10 mV, XY 1 mV, and zero final status/error words;
  the XY transient-unlock cleanup record remains audited.
- The scanner therefore validates every `harmonic * frequency` product before
  VISA opens. The selected coverage policy retains h1 at all ten 17.777 Hz--100
  kHz points, h2 at the supported first nine, and h3 at the supported first
  eight. `--skip-unsupported-harmonics` requires `--all-harmonics`, records each
  omitted order and 102 kHz-limit reason, and never writes an unsupported HARM;
  strict invocations without the flag fail before VISA opens.

- A later explicitly authorized target-computer run of that bounded policy
  completed with 81 clean formal xx/xy pairs: h1 at all 10 points, h2 at the
  supported first 9, and h3 at the supported first 8. The unsupported orders
  were recorded as skips rather than written to either instrument. Cleanup was
  verified at h1, 17.777 Hz, XX 4 mVrms/10 mV, XY 1 mV, with clear final
  status/error words.
- The separately authorized 17.777 Hz, 4--400 mVrms all-harmonic excitation
  rerun completed all 99 formal xx/xy pairs (11 source points × h1/h2/h3 × 3
  samples). It used the confirmed 100 kΩ external series resistance, 500 Ω
  approximate device resistance, 5 mArms/0.5 Vrms device limits, no external
  50 Ω termination, and the two-interval source-step wait. Its cleanup was
  likewise verified at the same baseline; raw records remain only in the
  ignored target `run_data` directory.
- Analysis of these accepted records confirms that reference lock is not a
  signal-quality assertion. At fixed 17.777 Hz, XX h1 passed the 1 µVrms and
  5-degree circular-spread display criteria at all 11 source levels; XY h1
  passed only at the top two levels, and XY h2/h3 passed nowhere. The frequency
  response of XX h1 was internally repeatable but phase changed smoothly with
  frequency and crossed the ±180-degree wrap. Raw low-amplitude phase remains
  available for audit, but is intentionally omitted from the default plots and
  must not be interpreted physically without a higher-SNR control measurement.

- On 2026-08-22, the device-only frequency/excitation sweep contract moved into
  strict `[lockin_sweep]` hardware TOML: the requested grids, h1/h2/h3 coverage,
  bounded high-frequency skips, temporary 20 mV XX range, timing, 100 kohm +
  50 ohm + approximately 500 ohm path, 5 mArms/0.5 Vrms limits, and absence of
  external 50 ohm termination. Daily sweep commands now default to that config
  without per-run confirm/authorize flags; they preflight the pair, fail closed,
  and atomically archive each opened-pair attempt as `completed`, `rejected`, or
  `interrupted` under the configured `run_data/commissioning` directory. Every
  result embeds an address-free resolved-TOML `measurement_config`, while actual
  readbacks remain in the preflight/point/cleanup records. This change was
  verified without connecting to real instruments or issuing setting writes.
- On 2026-08-22, sweep setup was extended to actively verify the configured
  fixed XY 1 mV range alongside the temporary 20 mV XX range. It writes either
  range only when preflight differs, rejects setup overload/unlock, records the
  transition, and restores only the ranges actually changed. Required
  `[lockin_sweep]` `run_name` and `note` now give every JSON record an auditable
  operator label and note; the label is safely included in the filename. The
  fake-VISA coverage and full offline suite passed (231 tests, 4 skipped), with
  no real instrument connection or write.
- Completed offline dual-role range and live-status integration (2026-08-22):
  XX and XY now independently choose `fixed` or opt-in `bounded_auto` in their
  own TOML tables. The daily defaults are fixed XX 20 mV and fixed XY 1 mV; the
  obsolete sweep-level temporary-XX field was removed. Auto policy is constrained
  to XX 10--20 mV or XY 1--10 mV, 0.85 occupancy, two consecutive h1 fit probes
  before narrowing, and one adjustment per continuous sweep. Probe/transition
  records, verified range readbacks, and cleanup restoration are retained in
  JSON without entering formal curves. A new `monitor-live` command displays
  paired X/Y/R, phase, frequency, harmonic, sensitivity, output, and optional
  latch status through queries only; it cannot send settings writes. Fake-VISA
  coverage and the full offline suite passed (248 tests, 5 matplotlib-dependent
  skips); no real instrument was opened or written.
- Completed offline analysis-calibration handoff (2026-08-22): frequency and
  current plots default to the per-sweep archived
  `measurement_config.excitation_path`, rather than duplicating resistance
  values in a notebook. The snapshot contains the configured external-series and
  approximate-device resistance, the fixed 50 ohm SR830 output resistance, and
  the total. Legacy files require an explicit analysis-only override, and mixed
  archived paths are rejected. Targeted analysis/notebook tests passed (19 tests,
  4 matplotlib-dependent skips) plus source compilation; no hardware resource
  was opened or written.
- During main integration, the strict temperature-only configuration loader was
  updated to recognize `[lockin_sweep]` as an unrelated optional table. Daily
  temperature operation therefore continues to use the unified local TOML
  without parsing or acting on Lock-in fields; the merged offline suite passed.
- On 2026-08-22, the daily Lock-in guide was expanded into the authoritative
  field-value reference for `[lockin_xx]`, `[lockin_xy]`, and `[lockin_sweep]`,
  including exact `fixed`/`bounded_auto` contracts and an import-path check for
  obsolete CLI help. Station-specific GPIB/VISA overrides remain only in ignored
  `hardware.local.toml`, so Git updates preserve them without replacing local
  hardware data. The timing behavior described in this historical entry was
  superseded by the schema-12 single `[lockin_sweep]` multiplier contract below.
  The 67 Lock-in SR830 tests and source compilation passed with fake VISA only;
  no real instrument resource was opened or written.
- Excitation-sweep device-voltage preflight now uses the circuit divider and a
  required, operator-confirmed `maximum_device_resistance_ohm` rather than
  treating SINE OUT itself as the device voltage. It validates the largest
  possible declared device resistance, records both approximate and maximum
  values, and still rejects before VISA opens whenever that calculated terminal
  voltage exceeds the RMS device limit. The 2 Vrms / 100 kΩ / 50 Ω / 500 Ω
  fake-VISA case records a 9.95 mVrms bound; no hardware resource was opened or
  written.
- The read-only commissioning notebook now selects remote files without a
  desktop dialog: its one editable `DATA_DIRECTORY` is refreshed into separate
  frequency/excitation record lists, and an explicit load button passes the
  selected pair to the existing filtered plotting cells. This supports Jupyter
  kernels reached through VSCode/SSH; no hardware module is imported.
- The commissioning notebook now accepts either scan type independently. A
  frequency-only selection renders only its six frequency figures; an
  excitation-only selection renders only its six current--voltage figures.
  `clean` formal samples remain the visible automatic screen, and a point-level
  multi-select applies reproducible manual exclusions without changing raw JSON.
  When optional export is enabled, `selection_manifest.json` records the files,
  filters, retained rows, excluded point keys, and the exact amplitude and
  circular-spread phase-display thresholds used for the figures. This is
  analysis-only and has no hardware imports or instrument operations.
- Rerunning the notebook's formal-samples cell now first re-synchronizes the
  current widget filters. Consequently, an explicitly allowed rejected audit
  record cannot be silently retried using an earlier rejected-disabled filter;
  this is a read-only notebook fix with no hardware imports or operations.
- The daily sweep guide now explicitly records the cleanup amplitude contract:
  a started sweep restores XX SINE OUT to the fixed 4 mVrms
  `MINIMUM_SINE_OUTPUT_V`; changing `source_voltage_v` alone cannot make cleanup
  restore 20 mVrms because sweep preflight requires both source fields to remain
  at 4 mVrms.
- Frequency sweeps now take their fixed XX excitation from strict
  `[lockin_sweep].frequency_source_voltage_v_rms` (0.004--5.0 Vrms). The value is
  checked against the archived circuit/device limits before VISA opens, set and
  read back once before the first formal point, and recorded with the derived
  nominal current at every point. Cleanup remains fail-closed at 4 mVrms. The
  templates, configuration parser, fake-VISA tests, and daily documentation were
  updated without connecting to or writing a real instrument.
- The two daily sweep commands now resolve formal harmonic selections per scan
  and role from `[lockin_sweep].frequency_xx_harmonics`,
  `frequency_xy_harmonics`, `excitation_xx_harmonics`, and
  `excitation_xy_harmonics`. Each permits an ascending h1/h2/h3 subset or `[]`;
  every scan requires at least one selected role. The union is still set and read
  on both SR830s, so selecting only XX or XY never bypasses companion status,
  lock, overload, readback, or cleanup checks. Formal JSON samples archive
  `selected_roles`, and offline analysis only emits the actually selected curves.
  Shared and legacy harmonic fields remain compatibility-only and cannot mix with
  the four new fields. Fake-VISA, strict-config, loader, and legacy-record tests
  passed; no instrument resource was opened or written.
- The remote Notebook's load button now also performs the formal-row load and
  fills point-exclusion options immediately. The standalone formal-samples cell
  remains only for explicitly refreshing after a filter change, so a normal
  load-and-exclude workflow cannot apply a blank selector to zero rows. This is
  read-only analysis only; no hardware path is imported.
- SINE OUT request/readback differences are now audit-only for both daily sweep
  commands: a quantized `SLVL?` value no longer rejects a safe scan. Records keep
  the requested amplitude and requested nominal current separately from the
  readback amplitude and readback-derived nominal current. The readback still
  undergoes the existing SR830 source-range and complete-path device-current/
  device-voltage safety calculation before sampling. Offline fake-VISA tests
  cover the observed 83.2 mVrms request to 82 mVrms readback case; no real VISA
  resource was opened.

- The project-approved daily SR830 sensitivity mapping includes 50 mV
  (`SENS 22`). `bounded_auto` remains role-limited: XX now supports the
  fail-closed three-level 10--20--50 mV ladder with at most two total,
  one-rung transitions per continuous sweep; XY remains 1--10 mV with one.
  Defaults (XX 20 mV, XY 1 mV) and all excitation/device protection limits are
  unchanged. Each range change is read back and audited, and 50 mV remains the
  largest project-approved input full scale. Pure-policy, strict-config, and
  fake-VISA two-widening cases passed as part of the 265-test offline suite
  (5 matplotlib-dependent skips); no instrument resource was opened or written.

Stage 4 - attoDRY legacy-DLL adapter: Temperature operation is operator-accepted;
DLL ABI preflight and real read-only connection validation are complete. Magnetic
field writes remain uncommissioned and require separate explicit authorization.

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
  temperature ranged from 1.7242 to 1.7246 K and VTI temperature from 1.7138 to
  1.7143 K; the user setpoint remained 2.0 K. Bx/Bz readbacks and setpoints
  remained zero, both control flags remained disabled, and all error codes were
  zero. Raw output remains only on
  the ignored control-computer path.
- Completed the Temperature-module T0 contract audit and T1 offline behavior
  coverage. The public surface is limited to state read, read-before-toggle
  temperature-control assurance, bounded setpoint write, and stable wait. Control
  flags must be exactly 0/1, successful setpoint writes require a complete
  post-write state/error/readback confirmation, and any disabled-control interval
  resets the continuous dwell window. Communication failures retain the prior
  `last_confirmed_state`.
- Added an offline-tested, dual-authorization `attodry-temperature-test` command
  for the future smallest-movement write stage. Every target, maximum sample-sensor
  movement, stability parameter, timeout, and success/failure hold-or-restore policy
  is explicit. It retains target/restoration samples and never infers recovery or
  disconnect after failed readback/close. No real attoDRY connection or write was
  performed for this addition.
- Added `config/temperature_commissioning.example.toml`, copied only to the
  ignored station-local counterpart for a T4 attempt. It places every per-attempt
  parameter in one editable top table, separate from hardware paths and limits.
  The CLI rejects placeholders, malformed fields, or any mixture of that file
  with direct parameter options before loading the DLL; its dual authorization
  flags remain mandatory and are not stored in the file.
- Revalidated this parameter-file entry point on `LK_setup` at commit `609b456`
  using Python 3.12.13 `lyr`: all 159 offline tests passed with no skips,
  compileall passed, and the CLI help exposed `--commissioning-config`. No DLL
  was loaded and no `begin/connect` or hardware command ran; the temporary clone
  was removed after verification.
- The ignored local T4 file now contains the operator-selected 1.75 K target,
  0.05 K maximum sample-sensor movement, 0.01 K tolerance/range, 600 s dwell,
  1 s polling, 1800 s timeout, `hold-target` success, and `hold-current` failure.
  `max_delta_k` is checked against the initial sample-temperature sensor reading;
  the possibly stale initial user-setpoint delta is recorded separately for audit.
  Local compilation, all 34 attoDRY tests, and all 160 project tests passed
  (2 optional plotting tests skipped), without DLL loading, connection, or any
  hardware command. Real setting writes still require new explicit authorization.
- Revalidated commit `b64eb74` on `LK_setup` with 64-bit Python 3.12.13 `lyr`:
  compileall and all 160 offline tests passed with no skips. Only Git, compileall,
  and unittest ran; no vendor DLL, `begin/connect`, or hardware command ran. The
  verified one-purpose temporary clone was removed and confirmed absent.
- The first authorized real T4 attempt passed the sample-movement gate from
  1.7237 K and sent one 1.75 K setpoint write. Immediate readback still reported
  2.0 K, so it failed closed before the control toggle and disconnected normally.
  A later five-sample read-only check confirmed the DLL had asynchronously applied
  1.75 K; sample temperature was 1.7240--1.7241 K, control remained disabled, and
  errors remained zero. Both raw records remain on the ignored target-computer path.
- Setpoint writes now treat an already confirmed identical target idempotently and
  poll complete state/error readback for up to 30 s after a new write. This bounded
  acknowledgement is separate from temperature stability waiting. A second attempt
  sent one temperature-control toggle and failed closed when its
  immediate flag readback still reported disabled; five later read-only samples
  confirmed the control had asynchronously enabled with zero errors. Temperature
  control now uses the same bounded acknowledgement polling without changing the
  field-control path. Local compilation, all 38 attoDRY tests, and all 164 project
  tests passed (2 optional plotting tests skipped); the continued real T4 stability
  run remains pending target validation.
- Commit `aaafabc` then passed compileall and all 164 tests without skips on
  `LK_setup`. Its final authorized T4 run started with 1.75 K/control enabled and
  correctly sent no duplicate command. Across 1799 samples and 1800.187 s, sample
  temperature stayed at 1.7237--1.7251 K and never entered the 1.75 +/- 0.01 K
  band, while setpoint, enabled control, and zero error status remained valid for
  every sample. It timed out, applied no `hold-current` recovery action, retained
  1.75 K/control enabled, and disconnected normally. T4 remains uncommissioned;
  manually verify the attoDRY front-panel/GUI temperature mode and heater response
  before another automated attempt. Raw audit files remain on ignored target paths.
- Manual GUI getter readbacks subsequently showed a configured sample heater:
  5.00 W maximum power, 115.00 ohm heater resistance, and 3.00 ohm wire resistance.
  The read-only `attodry_test` path now also queries the vendor sample/VTI heater
  power getters and records explicit watt-valued fields. It fails on getter return
  errors, non-finite values, or negative power while preserving the preceding full
  confirmed state. Local compileall, all 40 attoDRY tests, and all 166 project tests
  passed (2 optional plotting tests skipped). The exact commit then passed compileall
  and all 166 tests without skips on 64-bit Python 3.12.13 `lyr`; a no-connect DLL
  load confirmed all 23 required exports. The first real read-only attempt was
  rejected before sampling with the GUI-held resource busy, and its stderr remains
  retained. After GUI Disconnect, a new 10/10-sample record completed with writes
  disabled and normal disconnect/end: sample-heater output was 0.2036--0.2037 W,
  VTI-heater output was 0.0004 W, and sample temperature was 1.7335--1.7340 K.
  Setpoint remained 1.75 K, temperature control stayed enabled, errors stayed zero,
  and field readbacks/setpoints stayed zero. Heater output is therefore not zero;
  this short diagnostic does not establish temperature stability or PID correctness.
- A subsequent GUI-disconnected 601-sample, 600.622-second read-only monitor then
  completed with no writes, empty stderr, and normal disconnect/end. Sample
  temperature rose from 1.7342 to 1.7369 K and ranged 1.7335--1.7372 K (3.70 mK
  peak-to-peak); setpoint stayed 1.75 K, temperature control stayed enabled, errors
  and field readbacks/setpoints stayed zero, sample-heater power was 0.2106--0.2217
  W, and VTI-heater power was 0.0004 W. All 601 samples were nevertheless below
  the 1.74 K lower edge of the configured tolerance, so T4 remains a real stability
  failure. Do not advance the temperature stage or infer a PID/heater correction;
  manual diagnosis or a separately authorized control-setting change is required.
- A further explicitly authorized 1801-sample read-only monitor covered 1801.803 s.
  A resource-busy pre-sample failure was retained; its retry completed with empty
  stderr and normal disconnect/end after the competing GUI/connection released the
  device. Starting at 1.7401 K, the longest continuous tolerance interval was only
  319.313 s. Sample readback fell to 1.7289 K, then rose continuously to 1.9651 K
  over about 25 s and slowly decayed to 1.7746 K; the sustained trace rules out a
  one-sample spike. VTI moved only from roughly 1.717 K to 1.724 K during the event,
  sample-heater output ranged 0.0927--0.2413 W, and setpoint/control/error/field
  invariants remained valid. This localized overshoot suggests thermal delay with
  integral accumulation or a sample-sensor-loop problem, but does not identify
  whether PID tuning, thermal contact, or sensor behavior is responsible. T4 remains
  failed; do not advance or change settings without manual diagnosis and new
  authorization.
- The operator subsequently confirmed that manual GUI setpoint control works.
  Offline commissioning behavior now rejects the former `hold-current` failure
  policy. `disable-control` records trigger time, the last confirmed full state,
  and sample/VTI heater power before using the existing idempotent read-before-toggle,
  DLL-return-code checks, and bounded acknowledgement polling to disable temperature
  control. PID parameters are unchanged.
- The operator selected `max_overshoot_k=0.2 K` for the 1.8 K retry. The live
  sample guard records its trigger state and fails at or above 2.0 K, which routes
  through verified `disable-control`; it also rejects an absolute guard outside
  configured temperature limits. This explicit 2.0 K line is higher than the prior
  1.9651 K peak and would not have caught an excursion of the same magnitude.
- Commit `d4a6487` passed local compileall and all 170 tests (2 optional plotting
  skips), and then passed compileall plus all 170 tests without skips on
  `LK_setup`'s Python 3.12.13 `lyr`. The operator explicitly changed
  `max_delta_k` to 250 K for the real 1.8 K attempt, effectively removing the
  pre-write step restriction while retaining the 2.0 K live cutoff. A read-only
  preflight confirmed sample 1.7242 K, old setpoint 1.7000 K, control enabled,
  zero errors, and sample/VTI heater power 0.0091/0.0004 W. The authorized run
  recorded 1799 samples over 1800.079 s: sample temperature rose from 1.7241 K to
  a 1.7886 K maximum near 1776 s and ended at 1.7883 K. No sample entered the
  1.79--1.81 K tolerance band or reached 2.0 K; the 1.8 K setpoint, enabled control,
  and zero error code persisted throughout. Timeout diagnostics recorded
  sample/VTI heater power 0.1054/0.0004 W, verified temperature control disabled
  with the 1.8 K setpoint retained and error code zero, and disconnected normally.
  Raw JSON/stderr remain only on ignored `LK_setup` temporary paths. T4 remains
  failed and no automatic stage progression is justified.
- The operator reproduced a controller ordering requirement manually: full
  temperature control must be toggled on before applying sample temperature. The
  commissioning path now confirms control enabled first, then writes the target.
  An off-to-on transition forces one target reapplication even when the setpoint
  readback already equals 1.8 K, while all other matching-state operations remain
  idempotent. The audit records the confirmed order and force-reapply decision;
  PID behavior, the 2.0 K live cutoff, DLL checks, and failure-disable cleanup are
  unchanged.
- Commit `eaa3ba0` passed local compileall/all 172 tests (2 optional plotting
  skips) and `LK_setup` compileall/all 172 tests without skips. Two resource-busy
  preflights failed before any sample or write until the GUI disconnected. A first
  real run started with control already enabled and setpoint 1.6 K, so it confirmed
  control and then wrote 1.8 K without a toggle/forced reapply. Its 1800 samples
  over 1800.969 s reached 1.7785 K and timed out; verified cleanup disabled control
  while retaining 1.8 K. The resulting exact off/1.8 K initial state then exercised
  the new sequence: audit recorded control initially false and forced reapply true,
  followed by confirmed enable and confirmed setpoint. Its 1799 samples over
  1800.016 s rose from 1.7254 K to 1.7893 K, approximately 10.8 mK higher than the
  first run, but zero samples entered the 1.79--1.81 K band or reached 2.0 K.
  Timeout diagnostics recorded sample/VTI heater power 0.1059/0.0004 W, verified
  control disabled with setpoint 1.8 K and zero error, and disconnected normally.
  Both raw JSON/stderr pairs remain on ignored `LK_setup` temporary paths. The
  controller-order effect is supported, but T4 remains failed.
- On 2026-08-21 the operator accepted T4 using the experiment's operational
  criterion: the control-first command order produces measurable warming and the
  actual sample temperature is recorded, so measurement may begin after the
  30-minute wait without requiring the former strict stability window. The
  commissioned `max_overshoot_k` is 0.2 K. This acceptance does not reinterpret
  setpoint as measured temperature or erase the retained stability failures;
  Integration must persist `sample_temperature_k` for every measurement.
- Added the operator-requested daily `attodry-temperature-run` entry point and
  consolidated its target, 250 K movement limit, 0.2 K overshoot guard, 1800 s
  pre-measure wait, and 1 s polling in `[temperature_run]` inside the ignored
  `hardware.local.toml`. The command has no separate authorization flags; invoking
  it is the explicit action that connects and writes. It preserves the confirmed
  control-before-setpoint order, records every complete state, and exposes the
  actual `measurement_state` after the timed wait without imposing strict
  stability. Unsafe or failed monitoring attempts disable temperature control.
  The virtual 1800 s, actual-temperature, command-order, and overshoot-cleanup
  paths pass against the fake DLL; no real DLL was loaded for this addition.
  Exact commit `a20fa3f` passed compileall, all 217 tests without skips, and
  `temperature_run --help` on `LK_setup` with 64-bit Python 3.12.13 `lyr`.
  That target validation used no vendor DLL, connection, or hardware command.
  Follow-up `d045421` makes the daily loader independent of incomplete unrelated
  Lock-in/SMU tables while strictly validating the shared top level and every
  temperature-relevant table. It passed compileall and all 218 tests without
  skips on `LK_setup`; again no DLL or hardware operation ran.
- Completed Temperature T2 target-offline validation for commit `e9a7b8c` using
  `LK_setup`'s 64-bit Python 3.12.13 `lyr`: all 35 temperature tests and all 156
  offline tests passed without skips, and compileall passed. No vendor DLL was
  loaded, no `begin/connect` or hardware command ran, and the temporary clone was
  removed after its absolute cleanup path was verified.

Current boundary: all hardware-free work through Stage 7, integrated dual-SR830
harmonic validation, the independent Three-SMU QCoDeS S0 module plus query-only
monitor, and the attoDRY read-only connection are complete. The first
attoDRY temperature setpoint/control actions, control-first ordering, actual sensor
recording, and heater-driven warming are operator-accepted for this experiment.
The 1.75 K and 1.8 K runs did not meet the former strict stability criterion; that
fact remains diagnostic rather than being rewritten as stability. The commissioned
0.2 K overshoot guard gives a 2.0 K live abort line for a 1.8 K target. Daily
temperature operation uses the unified hardware TOML and dedicated command without
additional authorization flags. Three-SMU target-computer validation, all real SMU
connections/writes, integration of the independent SMU module into the main
acquisition, other attoDRY setting writes, and real end-to-end acquisition still
require staged authorization.

Temperature interruption follow-up (2026-08-24): `[temperature_run]` now accepts
`interrupt_policy = "continue"`, `"abort"` (default), or `"wait-confirmation"`, plus
`resume_recheck_s` (default 30 s). `continue` performs one automatic safe-state
recheck and then requires confirmation on another interruption; `wait-confirmation`
asks before retrying. Overshoot, nonzero device errors, communication failures, and
unconfirmed control/setpoint states remain hard fail-closed faults. The SQLite
acquisition audit now records that a resumed run repeats the interrupted condition;
partial attempts remain rejected and retained. A confirmed error-free temperature
state is persisted per run/target so a later same-target simulated condition uses a
short recheck instead of repeating the full wait; a real integration must revalidate
that state against the device before using it. Fake-DLL, simulated-station, and
SQLite tests cover the new paths. No real hardware interruption/recovery was
performed, and the full real attoDRY measurement engine is still pending.

Temperature stability-scan follow-up (2026-08-25): the Temperature branch now has
an offline-complete, explicitly gated ascending scan CLI. The unified hardware TOML
adds `[temperature_scan]` with the planned 1.7--2.7 K/0.1 K grid, run metadata, and
output directory while reusing the existing stability, movement, 0.2 K overshoot,
and interruption settings. The full grid is validated before DLL loading. Each point
keeps control-before-setpoint order and archives requested/actual setpoint, actual
sample temperature, time to first tolerance, time to stable, and the stable-window
range. Incremental JSONL, final JSON, and CSV retain partial and completed evidence;
soft interruption restarts the current dwell, and process resume skips only
contiguous completed points after an exact configuration check. Failure attempts
disable temperature control; normal completion holds the final target/control.
Source/test compilation and all 315 offline tests passed (5 optional matplotlib
skips). Target-offline then passed from a DLL-free isolated snapshot on
`LK_setup` with exact 64-bit Python 3.12.13 `lyr`: compileall and all 315 tests
passed with 0 skips, the example expanded to 11 points, CLI help passed, and the
unauthorized command stopped before DLL loading. Snapshot SHA-256 was
`CB8CAC713B92FB414E6382710878DA8E7DA39CAA5EB26CB765FB90F331BA3DBC`.
The target snapshot directory and transferred archive were verified, removed,
and confirmed absent after validation.
No existing `hardware.local.toml` was found below the target user profile, so the
ignored station-local config and DLL path still need creation/verification before
hardware use. No real DLL, connection, setpoint, or toggle was used. Real
1.7--1.8 K and full 1.7--2.7 K execution each require fresh operator authorization.

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
  connection or setting write was performed. The 2026-08-25 update reconciles
  the module with the generic SMU plan without removing `smu_bias`, and makes
  each gate parent table the single source of truth for that gate in one local
  TOML. The latest safety refinement allows every role to select voltage/current
  source independently while always enforcing its own absolute V/I boundaries.
  The 2026-08-26 update adds a raw-VISA query-only three-SMU terminal monitor and
  converts daily scan consent to exact, per-run terminal confirmations before any
  QCoDeS/VISA resource is opened.
- `THREE_SMU_DAILY_OPERATION.md` now provides the operator-facing independent
  daily workflow and full parameter reference. It clearly separates the currently
  permitted offline `describe`/analysis path from future, separately authorized
  connection and write steps; its current version documents one local config,
  dual-gate maps at fixed bias, status-queue authorization, and SSH-friendly
  accepted-only analysis. `THREE_SMU_LIVE_MONITOR.md` records the no-write,
  non-concurrent monitor boundary and opt-in error-queue consumption. Adding the
  guides did not perform hardware actions.
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
  independently configured gate SMUs. Its S0 implementation now
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
- The 2026-08-25 Three-SMU refinement adds one-file operation loading, shared
  gate preflight validation, explicit Keithley status-queue-consumption consent,
  nonzero/mode/status fail-closed checks before settings writes, metadata schema
  v3 unit-explicit requested V/I configuration plus provenance/cleanup errors, and remote-directory analysis with bias slices
  for a two-gate map. The legacy two-file entry remains workflow-compatibility-only.
- A further 2026-08-25 safety refinement gives `smu_bias`, `gate_top`, and
  `gate_bottom` independent voltage/current source modes and mandatory per-role
  absolute voltage/current software boundaries. Explicit `_v`/`_a` source,
  ramp, and readback fields remove unit ambiguity; both measured V/I values are
  checked at preflight and throughout a run. Instrument compliance must remain
  inside the corresponding software boundary, and leakage remains a stricter
  voltage-source-gate-only trip. Existing local hardware TOML requires explicit
  field migration because the loader will not infer new V/I limits from old
  unit-ambiguous source ranges; recorded run data remains unchanged.
- The 2026-08-26 daily-operation refinement defaults `describe`, `monitor-live`,
  and `run` to the ignored local TOML. A scan displays the complete validated plan
  and requires exact `RUN THREE SMU` before opening QCoDeS/VISA; a hold run also
  requires `HOLD OUTPUTS`. This retains deliberate human consent while removing
  routine authorization flags from the command line.
- Added an independent raw-VISA query-only Three-SMU monitor that displays all
  three roles' plan state, source/output, V/I/R, compliance/trip/ranges/sense,
  identity and safety warnings without configure/ramp/output/cleanup methods.
  Default monitoring leaves the consumptive Keithley error queues untouched;
  `--consume-status-queue` is explicit and monitoring is prohibited during scans.
- Each formal point records sequential per-role timestamps, source setpoint,
  V/I/R, output, compliance, gate leakage, status, scan coordinates, and cleanup
  results in `metadata.json`, `raw.jsonl`, and `data.csv`. Raw rejected,
  interrupted, partial, and cleanup events are retained; the new analysis loader
  and Notebook default to completed/accepted/clean formal rows.
- Fake instruments validate authorization-before-driver-import, query-only
  preflight, duplicate address/identity and active-output refusal, ramp bounds,
  both source modes, independent V/I bounds, compliance, leakage, readback
  mismatch, communication failure, Ctrl+C, and
  ordered zero-disable cleanup. Cleanup uncertainty rejects otherwise clean data
  and preserves last-confirmed state for manual verification.
- The focused Three-SMU/gate/config/adapter/Notebook suite now has 71 passing
  offline tests, including live monitor/error-queue and exact-confirmation paths.
  No real SMU connection, status query, or write was performed.
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
- The merged main/Lock-in/Temperature/Three-SMU hardware-free suite passes all
  385 tests in the minimal environment, with five optional matplotlib rendering
  tests skipped. Source compilation passes. The plotting path is unchanged from
  its prior rendered validation;
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

Current Lock-in safety-policy follow-up (2026-08-23):

- Added versioned `config/lockin_safety.toml`, automatically loaded beside the
  selected hardware TOML. It contains the project full-scale allowlists and
  bounded-auto ladders (XX 10→20→50 mV, XY 1→10 mV), 0.85 occupancy, two stable
  samples, source bounds, and 4 mVrms cleanup. Sweep timing is instead in the
  user-facing `[lockin_sweep]` table. The driver
  retains the complete SR830 voltage-input map, including 1 V/SENS 26, but the
  hardware map is not itself daily write authorization.
- Daily `sweep-frequency` and `sweep-excitation` require no preceding validation
  command; they parse the policy automatically and fail closed before VISA when
  it is absent or inconsistent. `validate-config` is an optional offline summary
  and does not open VISA. Sweep JSON `measurement_config` records the resolved
  policy and SHA-256.
- Sweep frequency/excitation records requested, XX actual, and both raw frequency
  readbacks without rejecting numeric display-bin or XX/XY differences. Analysis
  defaults to the XX actual frequency, and harmonic eligibility uses the higher of
  the requested and both actual frequencies. Non-finite/out-of-range values and
  strict unlock, input/filter-overload, instrument-error, and unsafe-transition checks remain
  fail-closed. Offline configuration/fake-VISA, record, analysis, and boundary
  coverage passed; no real instrument was opened.
- Both sweeps clear pending VISA responses before their first query and again before
  cleanup after an abort; the result records `interface_clear` and the prior schema
  version 9 clear-audit fields.
  The explicit `recover-interface` command clears only transport-layer pending I/O
  and reports `settings_changed=false`; diagnose and monitor-live remain read-only.
- Added semantic `reserve_mode` fields to both lock-in TOML tables and
  `allowed_reserve_modes` to the versioned safety policy. `high_reserve`, `normal`,
  and `low_noise` map to SR830 RMOD 0/1/2; the checked-in daily policy currently
  permits only `normal`. Sweep records audit target/original/readback RMOD values,
  lower SINE OUT before reserve writes, restore any changed mode during cleanup,
  and fail closed on status/readback errors. After HARM writes, a first
  input/reserve-overload-only latch is retained as a discarded transition and
  receives one additional settled verification read; repeated bit 0/bit 1 or any other
  unsafe transition status rejects the point. LIAS bit 2 is record-only for sweeps.
  The offline suite passed with no hardware I/O; its former schema-11 record is
  superseded by the schema-12 timing contract below.
- The Lock-in daily and module guides now document the SR830 reserve gain split:
  Reserve dB is a sensitivity-dependent interference ratio and allocation of
  gain after the demodulator, not added total gain. They include the project-used
  sensitivity table, a 20 mV AC/DC gain example, the distinction from output SNR,
  and operational selection rules. This was documentation only; the checked-in
  safety policy still permits only `normal`, and no hardware I/O occurred.
- Bounded-auto no longer exposes `autorange_max_steps`; the selected min/max pair
  resolves to the unique ladder in `lockin_safety.toml`. Widening moves only to the
  adjacent rung and may repeat within one point when occupancy remains at least
  0.85; narrowing still needs two consecutive fits. Sweep status handling records
  LIAS bit 2 (`output_overload`) but ignores it because CH1/CH2 output is unused.
  Input/reserve and filter overload candidates receive one settled recheck before
  fail-closed rejection. The former schema-11 timing record is superseded by the
  schema-12 timing contract below; this behavior was verified offline with fake
  VISA and no hardware I/O.
- The SR830 low-pass filter choices are now documented: 6/12/18/24 dB/oct map to
  `OFSL` codes 0/1/2/3. The checked-in project policy still accepts only 24 dB/oct;
  another slope requires synchronized code, safety, test, settling, and hardware
  confirmation changes.
- Simplified daily sweep timing (2026-08-24): `settle_s`, per-role
  `settle_time_constants`, and `lockin_safety.toml` timing settings are removed.
  Each role now has only its discrete SR830 `time_constant_s`; the complete
  allowed hardware range is recorded below. `[lockin_sweep].settle_time_constants`
  (at least 5.0) and
  `sample_interval_time_constants` are the two user-controlled timing
  multipliers. The sweep derives every second value from the slower role, archives
  `slowest_time_constant_s`, `settle_interval_s`, `post_setting_settle_s`, and
  `sample_interval_s`, and treats repeat samples as stability readings rather than
  independent replicas. Measurement schema version is 12. Configuration,
  fake-VISA, and full offline tests passed; no hardware resource was opened.
- Simplified Reserve configuration (2026-08-24): removed
  `allowed_reserve_modes` from `lockin_safety.toml`. Each role's
  `hardware.local.toml` now selects one confirmed SR830 mode
  (`high_reserve`/`normal`/`low_noise`) directly. The existing safe write order,
  RMOD readback/status verification, audit record, and cleanup restoration remain
  unchanged.
- Added the complete SR830 `OFLT` 0--19 mapping (2026-08-24), allowing every
  discrete hardware time constant from 10 µs through 30 ks in each role's
  `time_constant_s`. Arbitrary values such as 5 s remain invalid. The daily guide
  records every TOML value/code and the SR830 restriction on time constants above
  30 s when harmonic detection exceeds 200 Hz; sweep readback/status checks remain
  fail closed. All 140 directly relevant offline tests pass; the full local run
  reports 295 passed, 4 skipped, and one unrelated publication-plot failure from
  this workstation's NumPy/Matplotlib binary mismatch.
- The commissioning notebook export now archives the exact
  `PHASE_MINIMUM_AMPLITUDE_V` and
  `PHASE_MAXIMUM_STANDARD_DEVIATION_DEG` values in
  `selection_manifest.json`. Figures exported from the same raw data with
  different phase-trust thresholds are therefore distinguishable and
  reproducible. This is analysis-only and performs no hardware operation.
- The commissioning notebook now exposes an editable `SCALING_RULES` block for
  excitation harmonic power-law fits. Each available XX/XY × h1/h2/h3 channel
  compares free `R=A·I^p` and fixed `R=A·I^n` models, records exponent confidence,
  AICc, RMSE, current span, phase slope/span, and separate amplitude/complex
  response verdicts. Optional exports archive the exact rules and fit results in
  `selection_manifest.json` and write one fit figure per available channel.
  The analysis core and synthetic-order tests are offline-only; no hardware
  resource was opened or written.
- The same notebook now provides an offset-aware complex harmonic fit for the
  cases where a scalar `R=A·I^n+b` would be physically misleading. It fits
  `Z=X+iY` using no-background/fixed-order, no-background/free-order,
  complex-background/fixed-order, and complex-background/free-order models.
  `SCALING_RULES.complex_background_mode` can automatically select by AICc or
  force either background treatment. Results archive the selected model, complex
  background and response vectors, AICc/RMSE/exponent confidence, and a separate
  `complex_power_law_verdict`; raw phase slope/span remain a non-destructive
  audit. Synthetic offline tests cover a true quadratic response obscured by a
  complex background and a wrong exponent. No hardware resource was opened or
  written.
- The notebook now adds a phase-blind scalar-amplitude fit for the same channels.
  It compares `R=b+A(I/Iref)^n` with `R=b+A(I/Iref)^p`, constrains the
  amplitude/background terms non-negative, and exposes
  `scalar_background_mode` (`auto`/`none`/`with_offset`). It uses no
  phase, X, or Y; `scalar_phase_ignored` and `scalar_R_verdict` are retained
  in the fit export. Figures overlay log-space, scalar-R, and complex curves,
  and the notebook summary presents all three verdicts. Offline phase-rotation
  and wrong-order tests pass; no hardware resource was opened or written.
- Harmonic-scaling figures keep their legends outside the axes on the right and
  use concise model labels. Numerical coefficients, verdicts, AICc, and residual
  metrics remain in the optional `selection_manifest.json`; the Notebook no
  longer renders raw fit-result tables or a formula panel.
- Sweep grids now also accept named, non-overlapping linear or logarithmic range
  segments. Linear segments use inclusive `min`/`max` plus exactly one of `step` or
  `points`; logarithmic segments use `min`/`max`/`points`. Optional `xx_full_scale_v`
  and `xy_full_scale_v` overrides
  apply only to fixed roles at segment boundaries; bounded-auto roles continue with
  their policy and reject such overrides. Expanded plans and all range transitions are
  archived in measurement schema 7, while legacy point arrays remain accepted. This
  feature was verified with configuration and fake-VISA tests only; no hardware was
  opened or written.
- Added the frequency×excitation matrix sweep and its read-only analysis path
  (2026-08-25). `sweep-frequency-excitation` traverses frequency outside and
  ascending SINE OUT amplitude inside, returns to 4 mVrms before each frequency
  change, and archives actual frequency/source readbacks, derived current,
  range/status evidence, and grid indices in `frequency_excitation` JSON. The
  combined harmonic lists default to the excitation lists but can be selected
  independently. The analysis loader and Notebook now select combined records
  and plot one current–Vxx/Vxy curve per actual frequency; exports include the
  combined files and selection metadata. Fake-VISA, analysis, full offline suite,
  and Notebook compilation passed; no hardware resource was opened or written.

## Current profile update - operator-supplied hardware example

- Merged the operator-supplied station profile into the tracked hardware example:
  fixed XX 1 V, fixed XY 10 mV, excitation segments 4 mV--400 mV and 0.45--5 V,
  excitation formal roles XX h1 and XY h1/h2/h3, run name/note
  `test145degree`/`45degree`, and 100/150 ohm approximate/maximum device
  resistance. XX 1 V is now explicitly allowed by the safety policy. The tracked
  template deliberately retains address placeholders; station VISA addresses
  remain only in ignored `hardware.local.toml`.

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

## Current temperature scan implementation update (2026-08-26)

The temperature scan now has a hardware-commissioned `stable-readback` acceptance mode.
It still writes and verifies each requested setpoint, but measurement readiness is
based on the actual sample-temperature plateau. The stable-window mean is archived as
`measurement_temperature_k`; downstream measurement must use that value, not the
requested setpoint. `min_response_k` prevents later points from silently reusing an
unchanged plateau.

The rolling-window evaluator now retains one sample before the dwell cutoff, so
ordinary polling jitter does not make a nominal 30 s dwell impossible. PID gains and
heater configuration remain untouched; heater power is diagnostic readback only.
Offline tests cover target mode, stable-readback mode, and jitter. The previous rejected
run remains rejected. A separate authorized run on `LK_setup` at commit `cba448b`
completed all 11 points from 1.7 K through 3.7 K in 0.2 K steps. The final confirmed
state had a 3.7 K user setpoint, 3.569 K sample readback, temperature control enabled,
error code zero, and a clean DLL disconnect. PID and heater settings were not written.

## Current temperature–excitation integration update (2026-08-26)

The Integration worktree now contains the offline-complete temperature–excitation
coordinator. It traverses the existing temperature grid in ascending order. At each
condition, the existing stable-readback gate completes first; only then does the
coordinator execute the entire configured dual-SR830 excitation sweep, perform the
Lock-in cleanup/post-temperature check, and advance to the next temperature point.

The record deliberately preserves two non-interchangeable temperature quantities:

- the stable-window sample-temperature mean that established readiness before the
  excitation sweep; and
- the actual sample-temperature average over each formal Lock-in measurement window.
  The latter is constructed from synchronous attoDRY state reads immediately before
  and after the sequential XX/XY formal pair, with timestamp-based time weighting;
  it is the formal sample's temperature coordinate.

Incremental JSONL retains temperature states, Lock-in transition/formal/cleanup
records, partial data and every failure. Summary/default formal data only promote a
temperature condition after its full inner sweep, verified Lock-in cleanup and
temperature post-check complete. Resume verifies the archived configuration and skips
only contiguous completed temperature conditions; an incomplete condition never
continues from a partial amplitude or harmonic.

The dedicated fake-DLL/fake-VISA coverage verifies authorization before DLL/VISA/output
creation, the actual XX→XY temperature-callback bracket, stable-window versus
formal-window means, formal-window-only time weighting, parent JSONL/CSV contents,
inner-sweep failure cleanup, and condition-boundary resume. The complete offline suite
passed with 396 tests and 5 optional matplotlib-dependent skips. No real DLL or VISA
resource was opened and no hardware command was sent for this feature.

This is an offline integration implementation, **not** a report of a real combined
temperature/SR830 experiment. No real DLL/VISA combined run has been performed for
this feature. The future command
`python -m attodry_control.temperature_excitation_scan --config ... --authorize-temperature-excitation-scan`
is intentionally gated and still needs a separate explicit real-hardware authorization
that names the temperature writes, SR830 writes, latch consumption, physical wiring,
limits and cleanup scope. Use `lyr` on `LK_setup` for any future target validation or
authorized run. The detailed contract is in
[`TEMPERATURE_EXCITATION_SCAN_GUIDE.md`](TEMPERATURE_EXCITATION_SCAN_GUIDE.md).

## Current temperature–excitation analysis update (2026-09-01)

The read-only commissioning Notebook now has a dedicated remote-friendly
temperature–excitation browser. It discovers summary JSON/formal CSV records under
`TEMPERATURE_DATA_DIRECTORY`, defaults to `clean` formal samples, and lets the
operator select one or more files and individual completed temperature conditions.
Matching summary/CSV pairs are de-duplicated in favor of the summary; independently
selected runs retain their source-file and temperature-index identity.

The same browser now also accepts optional lower/upper bounds on the archived
readback-derived RMS current. It retains only the intersection of those current
bounds and the selected temperature conditions; the bounds are saved in the
selection manifest. Every temperature I–V legend is positioned outside the plot
frame on the right, so a long temperature list does not cover the curves.

All other analysis plotting functions follow the same right-side legend layout.
The commissioning Notebook displays figures only; raw numerical fit records are
preserved for optional export rather than emitted during plotting.

For each available XX/XY × h1/h2/h3 channel, analysis produces a separate R-amplitude
figure and phase figure. Each actual formal-window mean temperature is a separate
curve versus the archived readback-derived RMS current. Phase repeats use circular
mean/standard deviation and are unwrapped along increasing current only for display;
the raw record is never changed. Optional CSV/PNG/PDF export records the selected
files, statuses, temperature conditions, and phase treatment in its manifest.
Synthetic summary and formal-CSV loading, status filters, multi-run identity,
circular phase, Notebook compilation, and actual Matplotlib rendering were tested.
This change imports no hardware control path and performed no instrument I/O.

The Notebook first cell now also supports direct execution from a clean source
checkout without an editable package installation. When Jupyter starts from the
repository root or its `notebooks` directory, it validates and prepends that
checkout's `src` directory before importing `attodry_control`. Branch and worktree
names are never used as Python package names. A clean-checkout execution without
`PYTHONPATH` passed; this is analysis-only and performs no instrument I/O.

## Current file-only monitoring update (2026-09-01)

Two new terminal commands tail only the incremental JSONL files already produced by
the scan process: `temperature_progress_monitor` shows temperature point/state, and
`lockin_progress_monitor` shows the current sweep point, SINE OUT requested and
`SLVL?` readback, readback-derived nominal current, frequency, harmonic, Vxx/Vxy
R/phase and recorded status. They import only standard-library file-reading helpers:
they do not load the attoDRY DLL, open COM5, open VISA/GPIB, query instruments, or
consume status latches. They are therefore the required observation path while a
temperature, Lock-in, or combined scan owns hardware resources.

The combined scan now records a `lockin_point_ready` event immediately after its
existing SINE OUT/frequency readbacks, followed by formal and completed-point events.
The earlier absence of SINE OUT fields in live JSONL was only an event-context
omission: no new GPIB operation was needed. New standalone SR830 sweeps also emit
matching `*_lockin_<scan>_progress.jsonl` records. Existing JSONL and final summary
formats remain readable; old events without point context deliberately render as
unknown rather than being reconstructed from event count. Offline tests passed with
fake/file-only inputs. No real DLL or VISA resource was opened and no hardware
command was sent.

## Immediate next implementation tasks

1. Obtain a distinct, limited real-hardware authorization before any combined DLL/VISA
   operation. Hardware-test interruption/resume separately; do not infer or alter
   PID values automatically.
2. Perform staged attoDRY small-movement commissioning only after a new explicit
   write authorization and operator-selected smallest practical targets.
3. Run Three-SMU S1 target-offline validation in `LK_setup` `lyr`, then fill
   the ignored local addresses and safety values. Any real connection or setting
   write still requires a separate plan-specific authorization.
4. Freeze and verify the complete hardware wheelhouse on the offline control
   computer after its Python/VISA environment is known.

## Current segmented temperature-grid update (2026-08-26)

`[temperature_scan]` now supports lock-in-style `temperature_ranges`: each
inclusive non-overlapping ascending segment selects linear or logarithmic spacing
and an exact `step` or number of `points` as applicable. The expanded point sequence
and segment metadata are the single contract used by both the standalone
temperature scan and the temperature–excitation outer loop. The former
`start_k`/`stop_k`/`step_k` format remains a read-compatible single-linear-grid
fallback and cannot be combined with ranges. This is an offline configuration
change only; it does not authorize or report a real DLL/VISA operation.
