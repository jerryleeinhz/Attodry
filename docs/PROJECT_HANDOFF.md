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
  No phase setting or raw record is modified. The offline-only acquisition
  correction also requires `--settle-s >= 1.5` s before VISA opens and waits two
  intervals after every actual SINE OUT change (3.0 s at the current 300 ms,
  24 dB/oct setting), recording `source_step_settle_s` in the JSON. Fake-VISA
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
- Completed the Temperature-module T0 contract audit and T1 offline behavior
  coverage. The public surface is limited to state read, read-before-toggle
  temperature-control assurance, bounded setpoint write, and stable wait. Control
  flags must be exactly 0/1, successful setpoint writes require a complete
  post-write state/error/readback confirmation, and any disabled-control interval
  resets the continuous dwell window. Communication failures retain the prior
  `last_confirmed_state`.
- Added an offline-tested, dual-authorization `attodry-temperature-test` command
  for the future smallest-movement write stage. Every target, maximum setpoint
  delta, stability parameter, timeout, and success/failure hold-or-restore policy
  is explicit. It retains target/restoration samples and never infers recovery or
  disconnect after failed readback/close. No real attoDRY connection or write was
  performed for this addition.
- T2 remains pending: SSH read-only inspection confirmed `LK_setup` uses 64-bit
  Python 3.12.13 in `lyr`, but no repository copy exists there and no private
  source/test snapshot was transferred without separate authorization.

Current boundary: all hardware-free work through Stage 7, integrated dual-SR830
harmonic validation, and the attoDRY read-only connection are complete. attoDRY
setting writes, SMUs, and real end-to-end acquisition still require staged
authorization.

Module handoff packages are available under `docs/modules/` for separate Chat
follow-up:

- `LOCKIN.md` records the dual-SR830 configuration/phase/autorange objectives and
  turns the completed laboratory experience into implementation and acceptance
  rules.
- `TEMPERATURE.md` and `MAGNETIC_FIELD.md` separate their offline, target-offline,
  real read-only, and future write-commissioning stages without overstating the
  completed 10-second attoDRY read-only connection.
- `INTEGRATION.md` requires commit IDs, tests, hardware-action reports, and known
  limitations from the three device modules before combination.
- `docs/modules/README.md` defines shared permissions, `lyr` use, branch/worktree
  isolation, status terminology, and the completion-report format for each Chat.

These files are planning and handoff artifacts. They do not authorize hardware
connections, status-latch consumption, or setting writes, and no such action was
performed while creating them.

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
- The merged main/Lock-in hardware-free suite contains 197 passing tests in the
  minimal environment, with three matplotlib rendering tests skipped because
  matplotlib is unavailable. Source compilation passes. The plotting path is
  unchanged from its prior rendered validation;
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
2. Add the two vendor SMU adapters only after exact models, limits, and command
   references are supplied.
3. Freeze and verify the complete hardware wheelhouse on the offline control
   computer after its Python/VISA environment is known.
