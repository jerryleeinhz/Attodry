# Development stages

Update this file whenever a feature is completed. A stage is complete only when its tests and documentation are complete.

## Joint hardware acceptance blocked by field mismatch (2026-09-24)

- User-authorized apply-toml completed: XY 50 mV, XX 1 V, clear statuses,
  4 mV excitation. Existing 300 ms time constants retained; settling 5.1 s.
- Three-point ordered field test at target 2 K/gate 0 V acquired the first
  zero-field condition, then rejected the second. Actual Z exceeded 0.02 T
  after an acknowledged 0.005-T waypoint; later read-only Z was 0.05 T.
- Electrical cleanup confirmed safe outputs. Field-envelope validation blocked
  BOTH magnetic zero and temperature cleanup before their commands. Zero remains
  unverified; manual recovery/panel verification requested. Further scans stopped.
- SQLite integrity passes and default analysis excludes the failed run.
  No runtime code or safety-policy changes; private configurations/data ignored.
- This is not completed hardware commissioning. Field scaling/controller behavior
  and cleanup coupling require resolution. See JOINT_ACCEPTANCE_20260924.md.

## Integration publication / acceptance preparation (2026-09-24)

- User requested origin publication of the integration branch and authorized small
  joint hardware acceptance; the source/config/tests remain identical to the
  final 638-test offline snapshot. Earlier uncommitted status below is historical.
  Publication-day guarded rerun: 638/638 passed, zero failures/errors/skips, 111.757 s.
- Code published as b3570c5. User confirmed sample, separate gate SMU (<=1 V/1 uA),
  XX <=0.2 Vrms without external series resistor, estimated 100 Gohm device,
  XY disconnection and Z 0/0.01/0 T. The 0.01-ohm TOML entry is a placeholder.
- Private single-point smoke config passed local/target offline parsing; no
  instrument I/O issued. Existing daily TOML is not an acceptance plan. XX 1 V
  range allowed; XY remains 20 mV because XY 1 V is outside the project allowlist.
- Real acceptance awaits confirmation that the open Jupyter kernel is not running
  another controller/scan/monitor-live. Do not terminate it or start competing I/O.
- Preserve remote Notebook changes and keep hardware-local settings/data out of Git.

## Integration I2b — shared cryostat / four-module point interfaces (2026-09-23)

- Local and target offline implementation complete; **joint hardware
  commissioning NOT performed**. Four selected module subsets/orders (64), one
  shared T/B connection, exact ordered/repeated/segmented field points, universal
  integrated 3 T limit including float32 endpoints/corners/readbacks.
- Reuses existing field execution, thermal dwell and formal-window temperature
  statistics. Requalifies temperature after field changes; checks inner T resets
  and bounded cooldown. Actual coordinates derive from fresh formal-window reads.
  Environmental brackets are synchronous, not continuous while VISA blocks.
- Separate cryostat authorization; all preflights before writes; global independent
  cleanup and close. Earlier cleanup failure invokes later failure policies;
  communication loss retains last confirmed evidence and requires manual review.
- 17 new fake-resource tests plus one primary-error-preservation coordinator
  regression; full final local guarded suite **638/638**, zero failures,
  errors or skips, 117.888 s. No DLL loads or real instrument imports allowed.
  Final LK_setup lyr Python 3.12.13: **638/638**, zero failures/errors/skips,
  145.473 s, exit 0. Verified isolated snapshot/hash/log provenance is in
  PROJECT_HANDOFF; 112 runtime/test/config/Notebook files match the local revision.
  Existing electrical/analysis/apply-toml changes preserved. No commit/push.
- Remaining: scoped real joint acceptance,
  optional Lock-in frequency axis. Software pulses and hardware resume rejected.

## Integration I2a — real electrical point interfaces (2026-09-23)

- Local and target offline complete: active SMU × fixed-frequency dual-SR830 excitation,
  both loop directions and single-module subsets, no per-leaf full sweep/cleanup.
  The only new TOML table specifies order, repeats, sample count and run metadata;
  existing hardware/grid/range/harmonic/safety configuration remains authoritative.
- Single owner; all preflights before configuration; fresh readings at each leaf;
  actual V/I and selected per-role h1/h2/h3 recorded separately from setpoints.
  Partial role reads, source/config hashes, status/range evidence and rejected
  attempts remain in canonical SQLite; file-only monitor never queries hardware.
- Final cleanup: Lock-in minimum/h1/range/Reserve, then active SMU zero/OFF,
  independent resource closes. Audit failure cannot skip remaining cleanup;
  unsafe/preflight/communication/cleanup failures do not produce accepted data.
- 24 new fake-resource tests pass; full local guarded regression 620/620,
  zero skips/failures/errors, 74.274 s. Source/test/tool compileall and diff
  check pass. Initial full-run Notebook fixture and discovery incompatibilities
  were corrected; three new repeatability-statistic tests also pass.
- Real pyvisa/qcodes/serial imports and DLL loads are blocked by
  `tools/run_guarded_tests.py`. Final target-offline passed 620/620 with no skips/
  failures/errors in 88.395 s, exact LK_setup lyr Python 3.12.13, user-site off.
  New isolated directory under Yuanrong Li; existing experimental checkouts and
  ignored hardware TOMLs untouched. First snapshot passed 619 tests in 89.854 s.
  The final revision also conservatively requires manual review for every failed/
  interrupted hardware attempt, including non-OSError VISA exceptions even after
  apparently successful cleanup. No real I/O, existing-target overwrite or push.
  Final archive SHA-256:
  `28cfe061a7b575a683921bef50ce6799f5cbe462707cb6a8c9029a82cdf63276`;
  source/receipt paths and provenance are recorded in PROJECT_HANDOFF.
- Remaining: shared T/B attoDRY adapter and formal-window qualification/audits,
  frequency-sweep Lock-in points, full hardware combinations and joint commissioning.
  Hardware resume/software pulses remain blocked. Separate DC/AC compliance checks
  are not proof of safe combined physical wiring or total sample drive.

## Multi-run SR830 repeatability analysis (2026-09-23; implementation pending verification)

- The commissioning Notebook now allows multiple frequency, excitation, and
  frequency×excitation records to be selected independently, with a baseline
  run and selectable X/Y/R/phase metrics.
- Multi-run statistics remain partitioned by source file. Curves show each
  run's mean and within-run spread; difference panels pair only shared requested
  coordinates (frequency, SINE OUT voltage, or both), with circular phase
  differences. Plot axes continue to use recorded readbacks.
- The Notebook displays archived lock-in/excitation/sweep settings for condition
  review and computes harmonic fits and condensed reports independently per run.
  It also presents paired-coordinate mean-absolute, RMS, and maximum differences.
  The selection manifest records chosen files, baselines, metrics, repeatability
  summaries, and per-run fit results; original raw records remain unchanged.
- Verification is pending: no Notebook execution or tests were run in this
  change; do not mark the stage complete until offline verification succeeds.

## TOML-driven SR830 settings apply command (2026-09-23)

- Added `lockin_test apply-toml --role lockin_xx|lockin_xy` to apply only one
  selected role's fixed input/filter/sensitivity/Reserve settings from the
  strict station TOML, with explicit write and latch-consumption confirmations.
- The command leaves reference, frequency, harmonic, phase, and SINE OUT
  untouched. It accepts a target-role input/Reserve overload only when the
  configured sensitivity is wider, then requires a clean post-settle status;
  it does not roll back to a narrower range after a failed transition.
- Completed and rejected attempts are saved atomically in the configured
  commissioning output directory, including both roles' before/after readbacks
  and the last confirmed state.
- All 97 `test_sr830.py` fake-resource tests pass. No real VISA resource was
  opened, no hardware write was issued, and no remote workstation was accessed.

## Continuing four-module branch (2026-09-23)

- Operator selected codex/integration-four-module-scan for continuation/publication.
- Preserve module/integration's unique documentation commit and uncommitted
  Notebook changes; no branch/worktree deletion or main merge.
- Publication includes the existing V/I sensing and safe temperature-startup
  fixes, tests, bounded acceptance reports, and src/monitor documentation.
- No instrument operation or LK_setup deployment is part of this publication.
- Full local guarded regression: 588 passed, zero errors/failures/skips, 65.706 s.
  An initial user-site-disabled run lacked NumPy; the successful rerun used the
  existing user-site NumPy 2.4.6, with no dependency installation.

## Worktree imports and Lock-in monitor guidance (2026-09-23)

- Document resolved src PYTHONPATH and actual import-path verification for every
  new terminal/worktree switch, retaining the same lyr interpreter.
- Explain that empty PYTHONPATH is clearing, not src setup, and shared editable
  installations may still point to a different checkout.
- Clarify direct VISA monitor-live versus file-only JSONL progress monitors,
  status-latch side effects, and query duration plus inter-frame wait.
- Documentation-only change; no instrument access, runtime modification or remote sync.
- Verification: 98 related fake-SR830/file-progress tests passed in 3.434 s with
  hardware imports/DLL loads blocked; earlier sandbox attempts hit temporary-folder
  permission errors. Current-src import verified; no real monitor was started.

## Explicit V/I sensing and safe temperature startup (2026-09-22)

- Keithley configuration enables concurrent voltage/current sensing, both sense
  autoranges, and an explicit VOLT,CURR return format. Configuration and every
  formal read verify those settings. Output-OFF reads and unexpected element
  counts fail closed. Five new unit tests; target guarded full suite 585/585.
- Real unloaded bias test: five points 0,+1,0,-1,0 mV, 3 s per-point settling,
  |V| <= 0.01 V / |I| <= 1 microampere. Five clean samples; independently confirmed
  zero/OFF, dual sense functions, correct return format, error zero and clean
  cleanup. Historical current-only-sense records are not relabelled.
- A fresh cryostat status check exposed stored 300 K with control disabled at
  1.6796 K. All four active temperature entry points now preload/acknowledge the
  requested target before enabling and preserve the post-enable reapply.
  Rejected preload never enables control. 96 focused fake-DLL/VISA tests passed.
- User now authorizes temperature testing within 2–3 K. Isolated commissioning
  targets are 2.0,2.2,2.4 K, software target ceiling 3 K, max step from current
  sensor 0.5 K, overshoot margin 0.2 K, abort-on-interruption. No field movement
  is authorized by this update. Target guarded full regression passed 588 tests
  in 75.672 s, with no failures/errors/skips.
- Real low-T run completed all three targets with 603 raw polls. Coarse test
  acceptance: +/-0.2 K, 10 s window, <=20 mK peak-to-peak. Accepted actual window
  means 1.802786/2.003700/2.205586 K; this is not precision equilibration at the
  nominal targets. Command receipt proves preload-before-enable and no field
  write. Normal finish holds target 2.4 K with temperature control enabled.
- Independent 10:01:45 UTC cryostat read confirmed 2.331900 K actual / 2.4 K
  target, error 0, X/Z actual and setpoint zero, field control OFF. Connections
  were closed after checks; no continuous watchdog or long-term qualification.
- Electrical final check confirmed bias zero/OFF/1 microampere and both SR830
  4 mV, but retained a failed strict check on XY LIAS=4. Three later paired
  checks were clean without setting changes; root cause is not established.
- Raw-data audit and unsmoothed 603-poll trace completed; PNG visually reviewed,
  PNG/PDF metadata and provenance retained in ignored evidence. Details:
  LOW_T_AND_VI_ACCEPTANCE_20260922.md. No commit/push/main update.
- Arbitrary-order four-module real execution remains a separate unfinished stage.

## Bias-only unloaded real acceptance (2026-09-22)

- Operator corrected the physical condition to no sample connected and authorized
  autonomous scanning within existing 0.01 V / 1 microampere ceilings. Only bias
  hardware was accessed; all other roles remained untouched and physically unknown.
- Exact 1a87f03 target snapshot / lyr Python 3.12.13. Added missing QCoDeS 0.59.0
  and dependencies without replacing existing scientific/VISA packages; before
  environment list and install report retained. Full guarded offline regression:
  580 tests, zero failures/errors/skips, 68.048 s.
- Real read-only identity: Keithley 2400 serial 4414633, firmware C34, voltage
  source/two-wire/OFF/zero. First preflight stopped on recorded error 601 before
  any settings writes; retained failure plus three clean status checks preceded
  one retry. No reset or deliberate buffer-clear command.
- Zero trace 9/9, 0/+1 mV/0 9/9, +/-9 mV bidirectional 39/39, literal ordered
  duplicates 15/15: all clean/accepted. Soft KeyboardInterrupt at +2 mV retained
  3 formal samples as interrupted/rejected by default analysis.
- All five runs confirmed zero then output OFF, cleanup_errors empty and no
  manual-verification flag. Hardware current compliance read back 1 microampere.
  Three final independent OFF-state queries confirmed zero/OFF/compliance/error 0.
- Stream verification: 41 events, 39 sample payloads identical to raw JSONL.
  Standalone, Notebook data loader and unified legacy adapter returned exactly
  72 accepted rows; interrupted rows loaded only by explicit audit opt-in.
  Source target order, repeats, segments and the three raw artifact hashes per
  run were validated. Diagnostic PNG inspected at 1440x900, no fit/averaging.
- Scope limitation: SENS:FUNC reports CURR:DC only; reported voltage is not
  certified as independent terminal-voltage measurement. No load/accuracy or
  physical compliance-trip test, current-source/four-wire test, hard crash,
  communication-loss test, or joint four-module hardware commissioning.
- No runtime source/schema/safety-policy change, Git commit/push/main merge or
  alteration of pre-existing target checkouts. See BIAS_SMU_ACCEPTANCE_20260922.md.

## Integration I1b target-offline receipt (2026-09-22)

- User authorized SSH and target work under LK_setup's Yuanrong Li directory.
  Real specimen reported; 0.01 V / 1 microampere stated, but active SMU roles and
  exact voltage/scope semantics remain to be confirmed before configuring outputs.
- Preserved all existing dirty main/temperature-excitation checkouts and clean
  magnetic checkout. No hardware.local.toml or lockin_safety.toml was changed.
  Vendor attoDRY interface was running during inspection; left untouched.
- Deployed a DLL-free source archive of 1a87f03 into a new isolated
  Attodry_combination_offline_1a87f03_20260922/source directory.
  Exact archive SHA-256 is recorded in PROJECT_HANDOFF.
- Exact lyr Python 3.12.13, 64-bit, snapshot src first, user-site disabled:
  580 tests passed, 0 failures/errors/skips, 73.197 s. Real VISA/QCoDeS/serial
  imports and Windows DLL loaders were explicitly blocked during the test run.
- Zero real hardware connection, status consumption or writes. This receipt
  applies only to the simulator/record/analysis checkpoint; the real combination
  station and joint hardware commissioning are still incomplete.

## Integration I1b - combination data and simulator checkpoint (2026-09-14)

- Implemented literal arbitrary-subset/order Cartesian conditions, atomic X/Z
  ordered points, fixed one-point axes, omitted inactive modules, source-unit
  coordinates, duplicate/segment/direction/repeat preservation, and fresh reads
  at every complete condition. Simulator-only: no device adapter imported.
- Added versioned additive SQLite WAL/FULL audit, raw-before-promote, one accepted
  attempt, clean completed-run analysis defaults, deterministic all-module cleanup,
  explicit non-magnetic recovery and fail-closed magnetic/unknown-active recovery.
- Added a read-only state monitor, actual/requested coordinate table, legacy SMU
  and temperature-excitation adapters, grouping/filters without implicit averaging,
  and a common read-only Notebook with explicit sample exclusions and manifest.
- Full offline suite: 580 tests, 0 failures, 0 skips, 63.299 s; 26 new tests include
  all 64 subset/order permutations with multi-point axes. Final plot/Notebook
  refinement passed 2 focused tests in 1.737 s; compileall and diff check passed.
  Synthetic six-point plot inspected with 2 groups, 6 observations, 1380x720 PNG
  preview plus PDF and selection manifest. No publication-compliance claim.
- Real combination station/adapters, physical readiness/cleanup transcripts,
  target lyr validation and joint hardware commissioning remain incomplete.
  No actual DLL/VISA device was opened, no remote sync or main update performed.

## Integration I0 - four-module merge checkpoint (2026-09-14)

- Combined integration be6071f, Three-SMU direct points 02f04ec and magnetic
  7d4417f in isolated `codex/integration-four-module-scan`; preserved dirty main
  and the original integration worktree.
- Reconciled stage documents and the two independent configuration loaders;
  retained standalone magnetic safety history and the stricter universal-3-T
  boundary for the new integrated coordinator.
- Fixed obsolete temperature test-grid substitutions and test package resolution.
- Validation: full offline unittest discovery, 554 tests, zero failures/skips
  (42.806 s), local AI Python 3.12.13 / Matplotlib 3.10.9. Earlier bundled-runtime
  attempts lacked plotting dependencies and hit sandbox temporary-directory ACLs;
  those were not accepted as passing evidence.
- No hardware connection, setting/status command, remote sync or main update.

## Magnetic M4/M5 extension passed - X/Z bipolar and discrete circle (2026-09-14)

- Operator authorized the X/Z bipolar tests and 0.05 T X/Z circle, then freshly
  confirmed magnet about 4 K, zero field, no alarms, readiness and vendor-client
  exit. Each run checked clean runtime `75c5d630e46e7593081a0ee722fc3e1fbd93d22d`,
  the exact target lyr interpreter, config hash and absence of competing controllers.
- Four separate ten-sample read-only preflights passed with zero actual/setpoint,
  error 0, writes_authorized=false and clean disconnect. Only the ignored magnetic
  run table changed between runs, with backups and offline describe validation.
- All runs: direct, max_step 0.05 T, tolerance 0.001 T, range 0.0005 T, dwell 10 s,
  minimum samples 3, poll 1 s, wait timeout 7200 s, normal/exception policy zero.
  No APS100 rate/protection, temperature, Lock-in/SMU, fault reset, reboot or wiring change.

| Run | Formal targets | Duration | Successful command pairs |
| --- | --- | --- | --- |
| X bipolar | 0,-0.05,-0.10,-0.05,0,+0.05,+0.10,+0.05,0 T; Z setpoint 0 | 970.182 s | 14 |
| Z single target | Z +0.1 T; X setpoint 0, then monitored zero | 164.783 s | 3 |
| Z bipolar | Same nine-point sequence on Z; X setpoint 0 | 608.060 s | 13 |
| X/Z circle | 0.05 T, 0..360 degrees from +Z toward +X, 30-degree steps, 13 points | 890.981 s | 26 |

- X bipolar ran 07:11:08.164--07:27:18.346 UTC; accepted X rounded to
  0,-0.0502,-0.1002,-0.0499,0,+0.0502,+0.1002,+0.0499,0 T, accepted Z all zero.
  File-only monitor passed 1113 events, 9/9 points, no integrity errors.
- Z single ran 07:29:29.848--07:32:14.631 UTC; accepted Z approximately +0.0999 T,
  X zero. File-only monitor passed 193 events, 1/1 points, no integrity errors.
- Z bipolar ran 07:34:57.895--07:45:05.955 UTC; accepted Z rounded to
  0,-0.0499,-0.0999,-0.0501,0,+0.0499,+0.1002,+0.0502,0 T, accepted X all zero.
  File-only monitor passed 751 events, 9/9 points, no integrity errors.
- Both bipolar plans used three segments. Float32-aware returns used internal
  +/-0.075 and +/-0.025 T waypoints, not extra formal points. Transient cross-axis
  residual readbacks were retained and checked, not silently treated as exact zero.
- Circle ran 07:47:32.541--08:02:23.522 UTC. The 13 targets matched Bx=0.05*sin(theta),
  Bz=0.05*cos(theta), with cardinal components exactly zero and repeated endpoint.
  Sequential component writes do not prove continuous constant-radius rotation.
  These are scan-control tests, not measurements of specimen hysteresis.
- The circle continued after chat interruption; its original attached process
  returned exit 0, completed 13/13 and 26 command attempt/result pairs with
  field_command_audit_complete=true. All four terminal receipts report completed,
  zero_verified/disconnected/audit_complete=true, manual_verification_required=false.
  Final circle actual/setpoint Bx=Bz=0, error 0, control ON, DLL disconnected;
  zero-field control was not disabled.
- Final file-only circle monitor passed 1064 events, 13/13 points and no integrity
  errors or incomplete tail. Accepted readback magnitudes ranged from 0.04972575
  to 0.05060000 T under the approved 1 mT component tolerance; targets, not every
  actual readback or transient, have exactly 0.05 T magnitude. No scan process remained.

Canonical files in target ignored `run_data/magnetic_field_commissioning/`:

- `20260914T071108Z_m5_x_bipolar_0p1T_scan_c91adb9a.jsonl`, SHA-256
  `94f8a56a63017da14ea522071c5b89519153e2dd0bf687d1e22d4bda42bc5caf`.
- `20260914T072929Z_m4_z_0p1T_single-target_3db5c019.jsonl`, SHA-256
  `bc05601944218b76e2d15fd2ddf1947cd34766f414b2c49b3ff6c1732dcce156`.
- `20260914T073457Z_m5_z_bipolar_0p1T_scan_91c81284.jsonl`, SHA-256
  `c01ec9be9efb9ccee993dc2e7a2fe544bf0f73cf3f9e4575fdd035b735152152`.
- `20260914T074732Z_m5_xz_circle_0p05T_scan_efd8251b.jsonl`, SHA-256
  `63fe1c1e54606fef8dd99b5c0c7604caad4fb06d51adea233230552d3e0a924a`.

Local config SHA-256 in execution order: X bipolar
`9eb8abfd76d5f9782bd6a1a9408d28255fde5bc9e63acbb9aba42bed21fa3754`, Z single
`c2e4795d078794c8beef6f107e3e41dd474e06f47a0f6790201b92e1da0ee4c2`, Z bipolar
`d9f7e0925fef2f26e7931640766821bcaa01a8b10fa8c9fb8187d1f2c6a6b3ac`, circle
`e8bb5f29ceac7057b6a451782a31146a628abc726f3265e42c60dcf474accc50`.
The target config retains the circle plan. Backups, patches and all four preflight/
stdout/stderr groups remain in ignored subdirectory `xz_extension_20260914T070500Z`;
the initial backup matches the Sep 13 M5 hash. No raw data or local config is committed.
Prior rejected evidence remains intact. This closes the requested small-field magnetic
work only; larger fields, physical-axis calibration, real fault injection and
multi-instrument acquisition are separate commissioning scopes.

Documentation-only delivery verification: 54 magnetic CLI/monitor tests passed
in 6.816 s and 10 field-segment tests passed in 0.579 s; `git diff --check` passed.
The initial sandbox run hit temporary-directory ACL errors; the unchanged fake-only
tests passed outside that sandbox. Intermittent tool approval-service capacity
errors delayed file checks but did not issue hardware commands or restart scans.
No runtime changes are included. Historical full target validation remains 491/491
tests on the same runtime; this delivery did not rerun the full suite.

## Magnetic-field M5 passed - X five-point segmented roundtrip (2026-09-13)

- After M4, operator confirmed panel zero/no alarms and explicitly approved
  X 0,+0.05,+0.10,+0.05,0 T / Z setpoint 0, direct, max_step 0.05 T, tolerance
  0.001 T, range 0.0005 T, dwell 10 s, poll 1 s, timeout 7200 s and normal zero.
- Backed up the previous ignored local TOML, then applied only this M5 config:
  axis="x"; ascending segment min=0/max=0.1/points=3; descending segment
  min=0/max=0.05/step=0.05. This yields exactly five points with segment indices
  0,0,0,1,1; the turnaround is not duplicated. run_name/note identify M5 and shared
  cleanup.normal_end_field_policy is now "zero" rather than "hold".
  All other hardware/stability/limit settings are unchanged. The target local
  TOML is retained in this M5 form, SHA-256
  `87b3e4b783ec6b76a172a693e2fd7fa56e22e51de53ef7014dd39b723897c502`.
- Backup `hardware.local.before_m5.toml`, applied `config-change.patch`, ten-sample
  read-only preflight and scan stdout/stderr are retained under target ignored
  `run_data/magnetic_field_commissioning/m5_x_roundtrip_20260913T115400Z/`.
  Backup SHA-256 equals the previously recorded M4 config hash. Offline describe
  validated the exact five points/segments/end-zero/settings. Fresh preflight
  passed: zero actual/setpoint, control ON, error 0 and clean disconnect.
- Real scan used clean source `75c5d630e46e7593081a0ee722fc3e1fbd93d22d`, exact
  lyr Python, checkout src on PYTHONPATH and user-site disabled. No competing
  vendor/controller process. Command: `magnetic_field_cli scan --config
  config/hardware.local.toml --authorize-connection --authorize-field-writes
  --authorize-ordered-field-scan`. Exit 0; 11:57:54.480--12:06:05.435 UTC,
  duration 490.955 s. Runtime code was not edited.
- Accepted X readbacks by formal point: 0, +0.0500999987, +0.1001000032,
  +0.0498999991, 0 T; accepted Z readbacks all zero. Formal waypoint counts
  were 0,1,1,2,2. The float32-aware planner used internal +0.075 and +0.025 T
  waypoints on descending transitions; these are not extra formal scan points.
  Z setpoints remained zero; do not claim every transient Z readback was exactly zero.
- Seven successful durable command pairs (all DLL returns 0): X settings
  0.05000000074505806 (cdcc4c3d), 0.10000000149011612 (cdcccc3d),
  0.07500000298023224 (9a99993d), 0.05000000074505806 (cdcc4c3d),
  0.02500000037252903 (cdcccc3c), 0 (00000000) T, then sweepFieldToZero.
  No field-control toggle was needed because control was already ON; no Z setting
  command was sent. Complete symbols/context/float32 audit remain in JSONL.
- Canonical ignored target record:
  `run_data/magnetic_field_commissioning/20260913T115754Z_m5_x_0p1T_roundtrip_scan_dc52d781.jsonl`;
  SHA-256 `4c79633dfd9a10a639f1378d74aa8226fb2d5e7e7409c803d40b57f458937935`.
  File-only monitor passed: 568 events, 5/5 completed, audit_complete=true,
  no incomplete tail/integrity errors, zero_verified=true, disconnected=true,
  manual_verification_required=false. All historical rejected evidence retained.
- Final confirmed actual/setpoint Bx=Bz=0, field control ON, error 0; no test
  process remained. Last Sample/VTI 101.4002/90.5785 K; no temperature or other
  instrument setting was written. No fault reset, reboot, cabling or APS100
  charge-rate/protection change. Operator may perform final panel verification.
- M4/M5 are passed only for the tested positive-X 0--0.1 T scope, not Z/dual-axis,
  negative-field/high-field operation or integrated transport measurements.
  Local documentation updated and diff checked; no commit/push. Raw data,
  local hardware config, backups and local addresses remain uncommitted.

## Magnetic-field M4 automated acceptance passed (2026-09-13)

- Operator reconfirmed Magnet temperature 4 K and write-stage permission. A first
  preconnection check detected the vendor GUI and stopped without connecting;
  after the operator closed it, process checks found no matching controller.
- Ten new read-only samples passed with zero field/setpoints and error 0, followed
  by normal disconnect. Ignored target stdout/stderr directory:
  `run_data/magnetic_field_commissioning/m4_retry_20260913T114441151Z/`.
  This directory also contains the completed single-target stdout/stderr.
- Exact source `75c5d630e46e7593081a0ee722fc3e1fbd93d22d`, clean target checkout,
  exact lyr interpreter, PYTHONPATH pointing to checkout src, user-site disabled.
  Approved ignored M4 TOML SHA-256 remained
  `88c8f57139d9c1216934b421764c479532e75b041274621b8cf8085a515239a3` before/after.
- `magnetic_field_cli single-target --config config/hardware.local.toml
  --authorize-connection --authorize-field-writes` exited 0. Canonical interval:
  11:45:44.152 to 11:51:09.645 UTC, 325.494 s. Plan: X +0.1 T / Z 0, direct,
  max_step 0.05 T, tolerance 0.001 T, range 0.0005 T, dwell 10 s, poll 1 s,
  wait timeout 7200 s. Two internal X waypoints: 0.05 and 0.1 T.
- Four durable successful command attempt/result pairs, all DLL returns 0:
  `toggleMagneticFieldControl`; `setUserMagneticFieldX` float32 0.05000000074505806 T
  (bits cdcc4c3d); `setUserMagneticFieldX` float32 0.10000000149011612 T
  (bits cdcccc3d); `sweepFieldToZero`. Full symbols/context are retained in JSONL.
  No Z-component setting was sent. No error 35 recurred in this attempt.
- Accepted target readback: X +0.10019999742507935 T, Z 0, error 0. Final monitored
  zero: actual/setpoint Bx=Bz=0, field control ON, error 0. DLL disconnected;
  no test process remained. Zero-field control was not disabled. Last Sample/VTI
  were 113.7317/102.2586 K; no temperature or other-instrument setting was changed.
- Canonical ignored target record:
  `run_data/magnetic_field_commissioning/20260913T114544Z_m4_x_0p1T_single-target_d689e43a.jsonl`;
  SHA-256 `6b60d8d73879338cc8f1311f51fa6d126782f42e5cb1d70d2d1f0813a0b770d6`.
  File-only monitor passed: 357 events, 1/1 points, completed, audit_complete=true,
  no incomplete tail or integrity errors, zero_verified=true, disconnected=true,
  manual_verification_required=false. Historical rejected records are retained.
- M4 automated evidence passed; operator subsequently confirmed panel zero/no
  alarms and approved the exact M5 plan. M5 results are recorded above. Neither
  run certifies physical sample-axis sign, high-field/Z behavior or continuous
  trajectories beyond the tested discrete small-X points.
- Runtime source/config unchanged; only local stage/handoff/module documentation
  updated, with diff check. No commit/push, fault reset, reboot or wiring change.

## Magnetic-field retry preflight - no new writes (2026-09-13)

- Operator requested renewed M4/M5 testing. Clean target 75c5d63, exact lyr and
  approved M4 local-config hash were unchanged; offline describe passed with
  X +0.1 T / Z 0, direct and the previously approved step/stability settings.
- No matching vendor/controller process was found. Ten real read-only samples
  passed (exit 0): Bx/Bz and setpoints zero, field control OFF, error 0, clean
  disconnect; last Sample/VTI approximately 122.09/119.66 K. No toggle, component
  setting or zero command was issued. Original evidence is retained in target
  ignored commissioning directory `m4_retry_20260913T113700Z`.
- Await current magnet-temperature/readiness and prior error-35 resolution
  confirmation. Read-only error 0 is not proof of successful remote control.
  Proposed M5 X list 0, +0.05, +0.10, +0.05, 0 T / Z zero remains unapproved and
  unconfigured. M4/M5 are not passed; no new write-path attempt has begun.

## Current magnetic-field commissioning - M4 rejected (2026-09-11 20:12 UTC)

- Supersedes historical M4-pending-authorization statements. Operator authorized
  X +0.1 T / Z 0, direct, 0.05 T internal step, 1 mT tolerance, 0.5 mT range,
  10 s dwell, 1 s polling, 7200 s timeout and monitored-zero completion. Operator
  reported Magnet temperature about 4 K, confirmed prior cable/interlock concern
  resolved, and explicitly requested the attempt despite unclear Remote display.
- Source was clean `75c5d630e46e7593081a0ee722fc3e1fbd93d22d` on LK_setup; exact
  `lyr` Python 3.12.13/64-bit, checkout src on PYTHONPATH, user-site disabled.
  No matching vendor/controller process was found. Prepared ignored local TOML
  SHA-256: `88c8f57139d9c1216934b421764c479532e75b041274621b8cf8085a515239a3`.
  Its earlier original backup remains under the ignored commissioning directory.
- Fresh `attodry_test --samples 10 --interval-s 1 --authorize-connection` passed:
  ten zero field/setpoint states, errors 0, writes disabled and clean disconnect.
  Preflight stdout/stderr are in target ignored commissioning subdirectory
  `m4_x_0p1T_20260911T201100Z` alongside the subsequent single-target stdout/stderr.
- Real `magnetic_field_cli single-target --authorize-connection
  --authorize-field-writes --config config/hardware.local.toml` exited 2.
  Exactly one `AttoDRY_Interface_toggleMagneticFieldControl` call returned 0;
  acknowledgement readback showed control OFF and device error 35 about 1 s later.
  There were ZERO X/Z component-setting calls and ZERO sweep-to-zero calls.
  Exception cleanup began, but its error check refused additional writes.
- Last confirmed field and setpoint: Bx=Bz=0; control OFF; error 35. Cleanup zero
  was not verified: require on-site verification, not a zero-field assurance.
  DLL disconnected normally and no test process remained. No automatic retry,
  fault reset, restart, APS100 parameter change, cabling change or M5 execution.
- Canonical target ignored record:
  `run_data/magnetic_field_commissioning/20260911T201241Z_m4_x_0p1T_single-target_06667b45.jsonl`;
  SHA-256 `79a141fc3374f81aca42729488a105784d374e646ede637dca9f1bf60817ef5e`.
  File-only monitor validated 15 events, 1 attempt/result pair, no integrity errors,
  rejected outcome, zero_verified=false, disconnected=true and required manual
  verification. Raw rejected evidence is retained, not committed or accepted data.
- attoDRY2100 V2.1 manual p.34: error 35 means the required magnet controller
  is not connected/detected; error 36 separately denotes missing Remote mode.
  Communication/initialization needs investigation; hot-start initialization is
  a possibility, not an established diagnosis. M4 remains NOT PASSED; M5 gated.
  Runtime source/config were unchanged during this attempt. These doc updates
  are local only; no additional commit/push or target source synchronization.

## Current magnetic-field update - composable segments (2026-09-11)

- Implemented exclusive explicit-points / axis+segments configuration, signed X/Z
  linear ranges, inclusive count or exact-dividing positive step, per-segment
  ascending/descending order and preserved shared endpoints. Expansion is bounded
  to 10000 segment points before allocation; all existing safety/path checks remain.
- Added offline `magnetic_field_cli describe` and optional JSONL v1 `segment_plan`
  plus segment/direction point metadata; monitor verifies archived expansion.
- Added copyable commented M4/M5/return-branch/pure-Z examples with direct/via_zero
  explanations, strict endpoint semantics and normal hold/zero caveats. Active
  tracked target remains zero; local hardware config and APS100 rates are unchanged.
- Operator confirmed current-lead/interlock issue resolved and permits M4/M5 tests
  in principle. No real test ran; exact timing/M5 path remain to be selected and M4
  must precede M5. Target-offline is now complete as recorded below; live preflight
  and real M4/M5 remain unexecuted.
- Validation: new 10-test segment/config/CLI/fake-DLL/monitor suite passed (0.534 s);
  full unittest discovery ran 491 tests in 36.501 s, OK with 5 optional skips
  (486 passed). Final magnetic-focused rerun passed all 223 tests in 29.238 s.
  Compileall, CLI help, default-example offline describe and diff check passed.
  No real DLL, connection or hardware writes.
- Target-offline validation completed via authorized SSH in the operator-named
  `C:/Users/LK_Setup/Yuanrong Li/Attocube_control-magnetic` checkout at exact
  implementation commit `42f9799e4142110265d4d611a8f937c96eb94a67`.
  Fast-forward update from 89591ce preserved the ignored hardware.local.toml
  SHA-256, and Git remained clean. Exact interpreter:
  `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`, Python 3.12.13, 64-bit;
  PYTHONPATH points at that checkout's src and user-site is disabled.
  Compileall passed; 223 focused tests passed in 27.459 s; full discovery passed
  all 491 tests in 44.477 s with no skips. File-monitor import isolation passed
  without importing the attoDRY driver. Actual local-config offline describe
  passed: one zero target, via_zero, 0.05 T internal step, 1 mT tolerance,
  0.5 mT range, 10 s dwell, 1 s polling, 7200 s timeout; normal scan end is hold.
  These are existing configuration values, not approved M4/M5 movement parameters.
  Unlike the historical DLL-free snapshot, this validation used the real checkout
  with vendor files present, but all instrument execution used fakes; no real DLL
  was loaded, no instrument connected and no hardware command sent.

## Current magnetic-field update - axis-dependent limits (2026-09-11)

This update supersedes historical universal-3-T and M3-pending statements below.

- Operator-approved envelope: pure X +/-3 T, pure Z +/-9 T; both components
  nonzero additionally require resultant <=3 T. Exact zeros only for requests
  and readbacks. `experiment_vector_max_t` now applies only to dual-axis fields;
  reduced axis ceilings still apply to single-axis fields and cannot exceed 3/9 T.
- Shared safety checks cover configuration, planning, float32 endpoints, selected
  component corners, takeover and live monitoring. A changed component is also
  checked against the other coil's latest actual readback. Unsafe direct
  intermediates fail without implicit zero detours. APS100 rates are unchanged.
- Canonical JSONL adds `field_limit_policy = single-axis-hardware_combined-vector-v1`.
  The file-only monitor checks archived limits and the chosen safe corner; the
  unchosen alternative remains diagnostic. Unknown declarations fail closed,
  and old undeclared files retain original 3 T semantics.
- M3 is operator-confirmed from the supplied successful ten-sample read-only run
  (writes disabled, zero errors, clean disconnect); the pasted output did not
  identify exact source provenance. First M4 target selected: Bx=+0.1 T, Bz=0 T,
  for configuration preparation only; step/timing, transition policy and
  connection/write authorization remain pending. M4/M5 are uncommissioned.
  This revision needs target-offline validation and a read-only state recheck
  before writes. No new hardware operation occurred.
- Local validation: 213 focused safety/stability/config/driver/magnetic/monitor
  tests passed; full `unittest discover -s tests -q` ran 481 tests in 35.522 s,
  OK with 5 skips (476 passed). `compileall -q src tests`, single-target/scan
  CLI help, and `git diff --check` passed. The first two sandboxed focused runs
  hit Windows temporary-directory ACL errors; the unmodified offline tests passed
  outside that sandbox using the bundled 64-bit Python with user-site disabled.
  All instrument paths used fakes; no real DLL or hardware was opened. No new
  target-host evidence or hardware write commissioning is claimed.

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
  writes the selected files, filters, exclusions, and exact phase minimum-amplitude
  and maximum-circular-spread thresholds to `selection_manifest.json` for
  reproducibility. This analysis-only path has no hardware imports.
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
and real read-only connection validation complete (2026-08-21). The standalone
magnetic-field M0--M2 implementation and DLL-free `LK_setup` target-offline
validation are complete (2026-09-03). Magnetic-field M3--M5 remain uncommissioned
and separately gated.

- Added safe 64-bit vendor DLL loading and explicit function signatures.
- Added separately authorized COM connection and initialization timeout.
- Added temperature, VTI, X/Z field, setpoint, control, and error readback with
  last-confirmed-state preservation.
- Added read-before-toggle idempotent temperature/field-control operations.
- Added project-limit validation, explicit `direct`/`via_zero` setpoint planning,
  rolling stable waits, and monitored verified zeroing. `direct` is segmented
  adjacent-vector planning, whereas `via_zero` is the explicit conservative
  detour; software never silently substitutes one for the other. These discrete
  requested/read-back setpoints do not establish the continuous physical
  trajectory, constant angle, constant magnitude, or physical ramp rate between
  setpoints.
- Added fake-DLL return-code, timeout, write-authorization, setpoint-planning,
  stability, and vector-limit contract tests before laboratory use.
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

### Stage 4 follow-up - standalone X/Z magnetic-field module

Status: M0--M2 complete; M3--M5 commissioned in the small-field scope recorded
at the top of this file (updated 2026-09-14). X/Z +0.1 T single targets, each
axis's +/-0.1 T bipolar scan and a 0.05 T discrete X/Z circle passed. New runs,
larger fields and integrated acquisition still require their own authorization.
The implementation notes below retain their original dated offline context.

- Added a module-specific strict loader for `[magnetic_field_run]`, reusing only
  the required project, cryostat, magnet, and cleanup tables. The explicit nonempty
  X/Z `points` list is neither sorted nor deduplicated and is never expanded into
  a Cartesian grid; duplicate entries retain their own point indices and events.
- Added pure planning and standalone execution for one target or an ordered point
  list. `transition_policy` is explicit and required: `direct` segments adjacent
  vector endpoints while `via_zero` chooses the conservative zero detour. Every
  target, generated float32 setpoint waypoint, and both X→Z / Z→X mixed corners
  are prevalidated against component limits and `sqrt(Bx^2 + Bz^2) <= 3 T`.
  A waypoint is executed only with a dynamically selected verified axis order;
  there is no fixed X-first assumption. `max_step_t` must exceed the 1e-5 T
  acknowledgement resolution. The plan is recalculated from the last confirmed
  setpoint after connection/control acknowledgement and before setting writes.
- `max_step_t` constrains requested setpoint-waypoint spacing only. The module does
  require setpoint acknowledgement and actual-field stability at every internal
  waypoint, explicit target, and cleanup zero. It still does not observe or
  control the vendor controller's continuous motion between stable waypoints and
  therefore makes no physical-path, constant-angle,
  constant-magnitude, straight-line, or ramp-rate claim.
- Field control remains read-before-toggle and now receives bounded full-state
  acknowledgement. Initialization and control flags are strict 0/1. Before an
  OFF→ON takeover, actual field must match the latent setpoint within the
  configured field tolerance (at most 1 mT), and both possible mixed corners must
  satisfy the 3 T invariant; otherwise no toggle is sent. Changed X and Z
  components are written separately and each receives a bounded complete setpoint
  readback. Actual field must be stable at the starting setpoint and every
  waypoint before a subsequent setting write. A disabled-control interval resets
  the dwell window; final point readiness is owned by one explicit-target dwell.
  The first post-toggle acknowledgement read uses measured elapsed time and enforces
  the deadline. The retained sample just before a jittered dwell cutoff participates
  in both tolerance and rolling-range qualification, not only time coverage.
- Added `attodry-magnetic-field single-target` with separate connection and field-
  write gates. It accepts exactly one configured point and always performs
  monitored zero on normal completion. `scan` requires an additional ordered-scan
  gate, preserves every listed point, and applies the configured normal `hold` or
  `zero` policy. Both routes require exception policy `zero`.
- Normal zero and failure/`Ctrl+C` cleanup wait for a zero setpoint acknowledgement,
  actual-field stability within the configured tolerance, enabled field control,
  and clear error state. `isZeroingField`, vendor action/error messages, and vendor
  logs are optional diagnostics, never zero proof. Communication uncertainty is
  never reclassified as verified zero even if a later cleanup read appears safe. If
  the normal-zero path fails or is interrupted, cleanup makes an independent
  monitored-zero retry. Failed/unknown zero, close, connection, audit, non-finite
  readback, or last-state evidence sets `manual_verification_required` and retains
  the last confirmed state. Cleanup does not disable field control: it disconnects
  with control confirmed enabled at zero, or at the final target for a normal `hold`
  scan.
- Added one canonical per-run JSONL audit stream. Every event carries schema/run/
  index/time metadata and each append is flushed and fsynced. The start record
  includes config hash, source provenance, interface, authorizations, full
  stability/driver protocol, limits, points, transition policy, cleanup policy,
  and the required exact-field-command audit descriptor. Every field-control
  toggle, X/Z component command, and sweep-to-zero command records a durable
  pre-command attempt plus a DLL-return/post-acknowledgement result, including
  IEEE-754 binary32 component bits and command context. Planned and executed
  waypoint/path evidence is retained. Partial, rejected, interrupted,
  setpoint-transition, stability, cleanup, disconnect, and terminal events remain
  in that stream; no secondary final JSON/CSV is treated as truth. An audit-write/
  `fsync` failure is latched as rejection and stops the normal write path but cannot
  interrupt an otherwise possible best-effort zero cleanup. The writer best-effort
  rolls back an uncertain append, so a failed terminal `fsync` cannot leave a
  certified completion.
- Added `attodry-field-monitor`, which reads only a supplied JSONL file. It imports
  no attoDRY driver, opens no DLL/controller, validates stream/terminal integrity,
  and reports a torn/no-terminal/inconsistent stream as incomplete and requiring
  manual verification. It also rejects a contradictory completed record when zero
  was required but not verified or when the final confirmed state is missing.
- Final focused safety/stability/config/attoDRY/magnetic/monitor command passed all
  182 tests in 6.533 s. The full suite passed all 450 tests in 14.373 s with 5
  optional-matplotlib skips;
  `python -m compileall -q src tests`, both magnetic CLI `--help` commands, and
  `git diff --check` passed (diff check emitted only CRLF warnings). All execution
  used local fakes only: no real DLL load, `begin/connect`, field-control toggle,
  setpoint, sweep-to-zero, or other hardware command occurred.
- Completed M2 target-offline validation for implementation commit
  `e0924f1666b8e1b0b8e6e0c08daad2ab9f9ac4c4` (short `e0924f1`). The exact source
  archive `attodry_m2_e0924f1.zip` is 482705 bytes with SHA-256
  `231F649FA8A77B6139F67239F4E322275AA06B0BA3B4F0E628DEAFBFD32569F1`.
  It was copied to `C:\Users\LK_Setup\attodry_m2_e0924f1.zip` and extracted as
  `C:\Users\LK_Setup\attodry_m2_e0924f1` on `LK_setup`.
- The target used Conda environment `lyr` with exact interpreter
  `C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe`, Python 3.12.13, 64-bit.
  The archive explicitly excluded `vendor/`; the target snapshot had no `vendor/`
  and recursively contained 0 DLLs. A dedicated file-monitor import-isolation
  check imported the monitor from the snapshot without importing the attoDRY driver.
- Target compileall and both magnetic CLI `--help` checks passed. The same focused
  safety/stability/config/attoDRY/magnetic/monitor selection passed 182 tests in
  4.675 s; the full discovery suite passed all 450 tests in 20.299 s with no skips
  reported. The target-validation shell did not invoke a hardware-execution CLI or
  supply authorization flags; authorization-path unit tests used injected fakes.
  No DLL was loaded, and no hardware was connected or operated. This
  completes M2 only; the earlier generic 10-second attoDRY record still does not
  replace M3.
- The subsequent local-only transition/audit-contract extension keeps that stage
  boundary intact: it adds required `direct`/`via_zero` policy selection, exact
  float32 endpoint/corner verification, dynamic verified axis order, complete
  execution-waypoint/path evidence, and command attempt/result transcript checks
  against pure/fake DLLs. It loaded no real DLL and made no connection or hardware
  command. The historical `e0924f1` target snapshot must not be cited as target-
  offline validation of this later revision; a later M2 target claim needs a fresh
  DLL-free target run.
- M3 remains unexecuted. Its exact current-revision write-disabled command is
  `python -m attodry_control.attodry_test --config config/hardware.local.toml
  --samples 10 --interval-s 1 --authorize-connection`. It requires a new connection
  authorization and has no write authorization.
- The exact float32 toggle/component command-attempt/result transcript is now an
  offline M4 prerequisite rather than a missing feature. M4 nevertheless remains
  gated by M3 read-only commissioning, fresh user-selected smallest-movement and
  connection/write authorization, real evidence, and the vendor GUI plus every
  competing attoDRY controller process being disconnected. M3--M5 remain
  uncommissioned.

## Stage 5 - gate SMUs and integrated acquisition

Status: model-independent offline core and Three-SMU target-offline validation are
complete. Bounded read-only monitoring and one minimum bottom-gate write scan passed
for the current bottom-only active plan (updated 2026-09-01); other roles and
integration remain pending separate authorization.

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
- Updated `docs/modules/THREE_SMU.md` and `docs/THREE_SMU_DAILY_OPERATION.md`
  (2026-08-25) to use one `hardware.local.toml`, preserve the bias role beside
  two gates, document every run parameter, and retain a no-hardware daily path.
- Added independent `smu_bias`, `gate_top`, and `gate_bottom` Keithley 2400
  configuration and QCoDeS adapter modules. All placeholders, duplicate
  addresses/identities, incomplete active-mode fields, invalid source ranges,
  compliance above the corresponding absolute limit, and leakage above current
  compliance fail before production writes.
- Added one shared scan generator/session for time trace, bias I-V, top/bottom
  transfer, paired gates, one-to-three-channel serpentine maps, and software
  pulses. CLI and live Notebook consume that same generator; the first version
  neither connects nor records Lock-in data.
- Added explicit write authorization, query-only three-device preflight,
  unknown-active-output refusal, residual-zero verification, compliance/NPLC/
  autorange/four-wire configuration, step-bounded ramps, sequential timestamped
  V/I/R reads, compliance/leakage/readback checks, and ordered bias/top/bottom
  zero-disable cleanup. A cleanup communication failure rejects the run and
  requires manual front-panel verification.
- Added per-run `metadata.json`, `raw.jsonl`, and `data.csv` audit artifacts,
  plus a read-only loader and Notebook that default to completed/accepted/clean
  formal samples and require explicit rejected/problem audit opt-in. Schema v3
  stores unit-explicit requested V/I configuration plus requested-versus-actual preflight, run name/note, config,
  import, Git/dirty provenance, and structured cleanup errors.
- The daily loader now reuses `[gate_top]`/`[gate_bottom]` safety limits and
  reads module-only settings from `[gate_*.smu]`; the scan plan is in the same
  TOML. Legacy two-file loaders remain only for workflow compatibility; their
  local hardware values still require the explicit safety-schema migration below.
- The 2026-08-25 safety refinement gives all three roles independent voltage/current
  source selection and independent `max_abs_voltage_v`/`max_abs_current_a`
  software boundaries. Both actual readbacks are checked during preflight and
  every run read regardless of source mode. Unit-explicit `_v`/`_a` source,
  ramp, and tolerance fields replace ambiguous unit-by-mode names; voltage-source
  gates retain their earlier leakage trip, while current-source roles use voltage
  compliance without mislabelling sourced current as leakage.
  Old local hardware TOML must be explicitly migrated; the loader deliberately
  does not infer independent V/I safety bounds from an ambiguous source range.
- Added explicit status-queue-consumption authorization, non-zero/mode/status
  preflight rejection, and common `GatePreflightState` validation. The remote
  analysis notebook enumerates a data directory rather than opening a desktop
  chooser, and a bias slice can be selected for a two-gate map.
- Added `three_smu_cli monitor-live`: an independent raw-VISA, query-only terminal
  monitor for the three configured semantic roles. It reports plan role, actual
  V/I/R when output is already on, source/output, compliance/trip/ranges/sense/identity and non-corrective
  safety warnings; it has no configure/ramp/output/cleanup path. Its default does
  not consume `:SYST:ERR?`; `--consume-status-queue` remains explicit, and the
  monitor may not run concurrently with a scan.
- The daily `describe`, `monitor-live`, and `run` commands now default to the
  ignored `config/hardware.local.toml`. Before any scan driver/VISA resource is
  opened, `run` prints the validated plan and requires exact `RUN THREE SMU`;
  `finish_action = "hold"` separately requires `HOLD OUTPUTS`. This replaces
  repetitive CLI flags without making writes or error-queue consumption automatic.
- 71 focused fake-instrument/config/adapter/CLI/Notebook/analysis/gate tests
  pass, including query-only monitor/error-queue and exact-confirmation coverage.
  No real VISA resource was opened and no real setting command was sent.
- Direct-points safety-contract follow-up (2026-08-26): removed Three-SMU
  source min/max, software ramp, readback tolerance, per-device settle, leakage
  threshold, and user-entered compliance fields. Each role now has only independent
  `max_abs_voltage_v`/`max_abs_current_a`; the Keithley adapter derives hardware
  compliance from the opposite physical limit, queries compliance/ranges after
  configuration, and requires source/measurement autorange. `nplc=1.0` is the
  tracked 50 Hz/20 ms default. Formal points and cleanup use direct single writes,
  shared `delay_s`, and recorded actual readbacks.
- The same follow-up adds non-empty arbitrary `points` vectors as an alternative
  to `start/stop/step`, moves `bidirectional` into each role, expands paired/map
  roles independently, and rejects bidirectional software pulses. It deletes the
  duplicate split TOML templates and hidden legacy CLI path; unified
  `hardware.local.toml` is now the only operation source. Audit schema v4 removes
  `near_compliance` and records configuration compliance/range readback.
- Verification for this follow-up: 104 focused Three-SMU/Keithley/config tests
  passed; the full hardware-free suite passed all 389 tests with five optional
  plotting tests skipped, and `compileall` passed for `src` and `tests`. No real
  instrument library/resource was opened.
- Active-role configuration follow-up (2026-08-26): the run-plan `role` is now
  the sole enable state. Only `fixed`/`sweep` roles require and parse a same-name
  hardware table; `off` roles may omit it and are never opened, queried, written,
  cleaned up, or recorded. Monitor output explicitly marks them not connected
  with unknown physical state. Hardware parameters for bias/top/bottom now use
  the same flat table, legacy `[gate_*.smu]` and per-role `timeout_ms` are rejected,
  and adapter/monitor timeout is fixed at 5000 ms. Audit schema v5 records
  `active_roles`/`off_roles`, stores only active hardware snapshots, and leaves
  stable CSV columns blank for off roles. Focused fake/config regression: 109
  tests passed. The complete hardware-free suite passed all 394 tests with five
  optional matplotlib tests skipped, and `compileall` passed for `src` and `tests`.
  No real instrument library/resource was opened.
- Off-role scan-value follow-up (2026-08-31): `role = "off"` now normalizes the
  recognized `bidirectional`/`fixed`/`points`/`start`/`stop`/`step` values without
  parsing them, so temporarily disabled channels cannot fail `describe` because
  of dormant scan values. Misspelled field names remain strict errors, and all
  value/type/exclusivity checks return when the role becomes `fixed` or `sweep`.
  `hardware.example.toml` now shows an ordered vector, arbitrary non-monotonic/
  repeated vector, `start/stop/step`, descending direction, and bidirectional
  expansion. The 42 focused config/CLI/fake-session tests and all 396 offline
  tests passed (five optional matplotlib skips); `compileall` passed. No real
  hardware library/resource was opened and no query or write occurred.
- Ordered-range scan follow-up (2026-08-31): an active Three-SMU sweep now accepts
  exactly one of an explicit ordered `points` vector or an ordered `ranges` array.
  Linear ranges accept exactly one of positive `step` or point count; logarithmic
  ranges require positive endpoints and point count. Every segment includes both
  endpoints, multiple segments concatenate in TOML order without hidden boundary
  de-duplication, and per-role bidirectional expansion occurs after concatenation.
  The loader expands ranges into the existing point vector before the shared scan
  generator, target-limit checks, session, and record path, so no hardware or audit
  schema changed. Active top-level `start/stop/step` is now rejected; dormant known
  fields, including malformed `ranges`, remain unparsed while a role is off. The
  unified example and operator/module/safety guides document explicit, linear-step,
  linear-count, log-count, and multi-segment forms. All 76 focused Three-SMU,
  Keithley, CLI, Notebook, analysis, and gate tests passed; the full offline suite
  passed all 400 tests with five optional plotting skips, and `compileall` passed.
  No real hardware library/resource was opened and no real query or write occurred.
- Target read-only monitor follow-up (2026-09-01): the target `lyr` environment
  passes all 402 offline tests plus `src/tests` compilation. A bounded real sample
  opened only the current active `gate_bottom` Keithley 2400 and confirmed identity,
  0 V setpoint, output OFF, compliance/ranges, 2-wire sense, and trip-clear without
  consuming the error queue or sending setting writes. The monitor now skips
  `:READ?` while output is OFF and reports V/I/R as unavailable; it never enables
  output. Twenty-three focused fake/CLI regressions cover both output states. The
  target's unused NI GPIB passport was disabled in favor of the installed Keithley
  KUSB passport, and one selective device clear recovered a previously stuck parser
  without `*RST` or source/output/compliance changes.
- Target bottom-gate write commissioning (2026-09-01): Keithley 2400 firmware C32
  does not permit `:READ?` or the protection-trip query while output is OFF. The
  QCoDeS preflight/configuration/cleanup path now records confirmed output/setpoint
  state without inventing V/I, enables only after a confirmed 0 V setpoint, and
  takes its first V/I read after output is ON. The query-only monitor likewise shows
  V/I/R/trip as unavailable while OFF and Ctrl+C exits without a traceback. After
  draining errors left by the former illegal queries, the SNOM `gate_bottom`
  Keithley 2400 serial 4029737 completed an authorized five-point -0.1 to +0.1 V
  scan. All five formal samples were clean; run `20260901_110258_e2b23039` was
  completed/accepted and cleanup independently confirmed 0 V, output OFF, and a
  clean status queue. Forty-one focused tests and the complete 404-test offline
  suite passed. Bias/top roles and integrated acquisition remain uncommissioned.
- Direct-run live-panel follow-up (2026-09-01): normal `three_smu_cli run` no
  longer requires a per-run `RUN THREE SMU` prompt; `finish_action = "hold"`
  retains the separate exact `HOLD OUTPUTS` confirmation. `ThreeSmuSession.run`
  now publishes each already-recorded formal sample to an optional in-process
  callback before a problem sample triggers its fail-closed exception. The CLI
  consumes that FIFO in the same single session and prints only sample progress,
  repeat, segment, elapsed time, source-setpoint readback, V/I/R, output state,
  and `CLEAN`/`PROBLEM`; status/error queue evidence and problem reasons print
  only for a retained problem sample. The live Notebook uses the same FIFO and
  makes no separate hardware query. Twenty-six focused fake-instrument/session/
  CLI/Notebook tests and the complete 405-test offline suite passed (five optional
  plotting skips); no real resource was opened, queried, consumed, or written
  during this follow-up.
- Terminal-table follow-up (2026-09-01): the direct-run panel now prints a single
  fixed-width table header in terminals at least 96 columns wide, then appends each
  formal sample's progress line and active-role setpoint/V/I/R/output readback in
  engineering units. Narrow terminals use compact per-role lines rather than wrap
  the table. Problem status/error details remain conditional on `PROBLEM`, and the
  renderer still only reads the in-process FIFO. Eleven focused CLI/Notebook fake
  tests and the complete 406-test offline suite passed (five optional plotting
  skips); no real resource was opened, queried, consumed, or written.

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
- The current source-based main/Lock-in/Temperature/Magnetic-field/Three-SMU
  suite passes all 450 tests in 14.373 s, with five optional matplotlib rendering
  skips; source/test compilation and magnetic CLI help checks pass. All validation
  was hardware-free.
- The previously built local project wheel was built and import-checked without
  downloading dependencies; its filename and SHA-256 are recorded in
  `PROJECT_HANDOFF.md`. It predates the standalone magnetic-field module, so a
  new wheel and frozen wheelhouse remain pending.
- Added `docs/modules/` work packages for independent Lock-in, Temperature,
  Magnetic-field, and Integration Chat follow-up. Each package records its
  current real-hardware boundary, goals/non-goals, staged acceptance criteria,
  file ownership, safety cautions, and a copyable startup prompt. The Lock-in
  package converts the completed bench-test experience into explicit rules for
  wiring, phase preservation, settling, sensitivity transitions, latch handling,
  frequency tolerance, sequential pair reads, and cleanup. This is a planning
  and handoff deliverable only; it does not commission any new hardware writes.
- Pending: remaining-role Three-SMU read/write commissioning and integration into
  the main acquisition, frozen hardware wheelhouse, and offline-control-computer
  installation verification.

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
  Each role retains only its discrete SR830 `time_constant_s`; the complete
  allowed hardware range is recorded below. The single
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
- Added all SR830 `OFLT` time constants (2026-08-24): configuration now maps the
  complete discrete 10 µs--30 ks hardware range, while rejecting non-hardware
  values such as 5 s. Documentation lists all 20 TOML values and codes, explains
  derived sweep waits, and records the >30 s / >200 Hz harmonic-detection
  restriction. The 140 directly relevant configuration/mapping/fake-VISA tests
  pass. In the full local run, 295 tests pass and 4 skip; the sole unrelated
  publication-plot test is blocked by the workstation's NumPy/Matplotlib binary
  mismatch. No VISA resource was opened.
- Added reproducible phase-quality export metadata (2026-08-25): the commissioning
  notebook writes its exact minimum-amplitude and maximum-circular-spread phase
  thresholds into `selection_manifest.json`. The targeted analysis suite passed
  15 tests with 2 matplotlib-dependent skips, and all notebook code cells compile;
  no hardware resource was opened or written.
- Added editable excitation harmonic-scaling analysis (2026-08-25): the notebook
  exposes `SCALING_RULES` for point count, current span, SNR, exponent confidence,
  AICc, RMSE, and phase stability. The analysis compares free `I^p` and fixed
  `I^n` models for every available XX/XY × h1/h2/h3 channel, reports separate
  amplitude and complex-response verdicts, exports fit figures/results and the
  exact rules in `selection_manifest.json`, and adds synthetic-order tests.
  This is read-only analysis; no hardware resource was opened or written.
- Extended the harmonic-scaling analysis with an optional complex background
  (2026-08-25): all available channels now additionally compare `Z=C(I/Iref)^n`,
  `Z=C(I/Iref)^p`, `Z=B+C(I/Iref)^n`, and `Z=B+C(I/Iref)^p` using X/Y directly.
  The editable `complex_background_mode` selects automatic AICc selection,
  no-background, or forced-background comparisons. The exported result records
  fitted background/response vectors, AICc, residuals, exponent confidence, and
  a background-aware `complex_power_law_verdict`; raw-phase stability remains a
  distinct audit. Synthetic tests cover correct quadratic data with a complex
  background, a wrong background-corrected exponent, and forced no-offset mode.
  This is read-only analysis; no hardware resource was opened or written.
- Added a phase-blind scalar-amplitude comparison (2026-08-25): every available
  excitation channel now also compares `R=b+A(I/Iref)^n` and
  `R=b+A(I/Iref)^p`, with non-negative background/response terms and an
  editable `scalar_background_mode`. This fit uses only measured amplitude R;
  phase and X/Y are ignored and the exported `scalar_phase_ignored` flag makes
  that choice auditable. Notebook figures overlay the log, scalar-R, and
  background-aware complex fits, while the summary displays all three verdicts.
  Synthetic phase-rotation tests and notebook compilation pass; no hardware
  resource was opened or written.
- Updated harmonic-scaling figures (2026-09-03): all plot legends are outside
  the axes on the right. Every fitted curve carries its substituted,
  current-normalized scalar `R(I)` or phase-preserving complex `Z(I)` equation,
  exponent/available interval, R², relative RMSE, and AICc (or log-model
  ΔAICc); the legend title retains the log/scalar/phase/complex verdicts. Full
  precision remains in the optional selection manifest, while the Notebook does
  not render raw fit-result tables or a separate formula panel. This is
  read-only presentation logic and introduces no hardware path.
- Added an explicit harmonic-scaling display selector (2026-09-04). The Notebook
  initially draws log, scalar-R, and complex methods for comparison, then accepts
  a one-item tuple to show only the selected method's curves, equations, verdicts,
  and residuals. Commented examples cover all three choices and clarify that
  `scalar` is a linear-coordinate R fit, not a forced unit exponent. All methods
  remain computed and exported; the manifest records the active display choice.
  Offline API, Notebook-source, compile, and Matplotlib-render tests passed; no
  hardware resource was opened or written.
- Added frequency×excitation matrix sweeps (2026-08-25): the new
  `sweep-frequency-excitation` command traverses the configured frequency grid
  outside and the ascending SINE OUT grid inside, returning to 4 mVrms before
  frequency changes. Combined harmonic selections inherit the excitation role
  lists unless explicitly overridden, and each JSON archives actual frequency,
  SINE OUT readback, current, range, status, and grid-index records. The
  analysis loader now accepts combined records and provides multi-frequency
  current–Vxx/Vxy curves grouped by actual frequency; the notebook exposes a
  combined-record selector and exports it in `selection_manifest.json`. Offline
  tests and notebook compilation pass; no hardware resource was opened.
## Stage 7 follow-up - temperature interruption and point recovery

Status: offline implementation complete (2026-08-24); real interruption/recovery
remains pending explicit hardware authorization.

- Added `[temperature_run].interrupt_policy` with `continue`, `abort` (default), and
  `wait-confirmation`; omitted policy values remain backward-compatible with the
  existing abort behavior.
- Added `[temperature_run].resume_recheck_s`, default 30 s. A continue or confirmed
  resume requires a fresh full-state recheck before the temperature run can become
  measurement-ready. A second automatic continue request changes to confirmation.
- Overshoot, nonzero attoDRY errors, communication failures, and unconfirmed
  control/setpoint states remain hard fail-closed paths regardless of policy.
- Extended SQLite acquisition interruption/resume audit payloads to identify the
  interrupted condition; resume repeats that condition with a new attempt index and
  keeps partial raw data rejected for audit.
- Persisted an error-free, enabled temperature qualification per run/target. A later
  pending condition at the same target uses a short simulated readback recheck rather
  than repeating the full temperature wait; the real integration must perform the
  corresponding hardware recheck before relying on the token.
- Added fake-DLL, simulated-station, configuration, and SQLite tests. No vendor DLL
  was loaded and no real instrument was connected for this feature.

## Stage 7 follow-up - ascending temperature stability scan

Status: target-offline complete (2026-08-25); real multi-point commissioning
remains pending separate authorization.

- Added strict `[temperature_scan]` configuration for the 1.7--2.7 K, 0.1 K
  ascending grid, `run_name`, `note`, and a TOML-relative output directory.
  Stability timing remains solely in `[temperature_stability]`; movement,
  0.2 K overshoot, and interruption behavior remain solely in `[temperature_run]`.
- Added the explicitly gated `attodry-temperature-scan` entry point. The entire
  grid and consecutive movement bound are validated before DLL loading; the real
  path is unavailable without `--authorize-temperature-scan`.
- Every point preserves the commissioned control-before-setpoint order and records
  requested setpoint, actual setpoint readback, actual sample temperature,
  time-to-first-tolerance, time-to-stable, and stable-window range statistics.
- Raw state/transition/interruption records are fsynced incrementally to JSONL.
  Final JSON and CSV retain completed, rejected, and interrupted outcomes, resolved
  configuration, Git commit, cleanup errors, and last confirmed state.
- Soft interruption recovery requires a fresh full-state recheck and restarts the
  current dwell window. `--resume-progress` verifies the archived configuration,
  preserves contiguous completed points, and repeats the first incomplete point.
- Failure attempts idempotently disable temperature control when a write may have
  occurred. Normal completion holds the final target with control enabled. A
  communication/readback failure never claims confirmed shutdown.
- Source/test compilation and the complete offline suite passed: 315 tests, with
  5 optional matplotlib-dependent skips. Fake-DLL cases cover authorization before
  DLL load, successful timing, overshoot cleanup, soft interruption, and process
  resume. No real DLL was loaded, no attoDRY connection was opened, and no hardware
  command was sent.
- Target-offline passed from an isolated, DLL-free source snapshot on `LK_setup`
  using `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`, Python 3.12.13 64-bit.
  Import resolved to that snapshot's `src`; compileall and all 315 tests passed
  with 0 skips. The strict example parsed to all 11 points, CLI help passed, and
  invoking the scan without authorization returned the expected pre-DLL error.
  Snapshot SHA-256 was
  `CB8CAC713B92FB414E6382710878DA8E7DA39CAA5EB26CB765FB90F331BA3DBC`.
  The dedicated target directory and transferred archive were path-verified,
  removed after validation, and confirmed absent.
  No existing `hardware.local.toml` was found under the target user profile, so
  this stage validated the tracked example rather than claiming station-local
  configuration readiness.
- A real run requires new authorization plus creation/verification of the ignored
  local TOML and DLL path. The recommended first orchestration write is
  1.7--1.8 K before the full 1.7--2.7 K scan.

## Stage 7 follow-up - stable-readback measurement acceptance

Status: real hardware commissioned on `LK_setup` (2026-08-26).

- Added `temperature_stability.acceptance_mode = "stable-readback"`, which uses the
  actual sample-temperature plateau for measurement readiness and keeps the requested
  setpoint as an audited command rather than an analysis coordinate.
- Added `min_response_k` for points after the first and archived
  `measurement_temperature_k`, response time, stable-window mean, standard deviation,
  range, and sample count in JSON and CSV.
- Fixed rolling stability at non-exact polling boundaries by retaining one sample
  before the dwell cutoff. A 1.501 s polling-jitter regression test now covers the
  failure seen during the previous real scan.
- PID gains and heater settings remain read-only; the scan does not write either.
- Offline tests passed with the existing target mode and the new readback mode.
- The authorized `LK_setup` run at commit `cba448b` completed all 11 requested points
  from 1.7 K through 3.7 K in 0.2 K steps. The final confirmed state retained a
  3.7 K user setpoint with a 3.569 K sample readback, temperature control enabled,
  error code zero, and a clean DLL disconnect. Every point archived its stable-window
  measurement and timing; PID and heater settings were not written.

## Stage 7 follow-up - temperature–excitation orchestration

Status: offline implementation complete (2026-08-26); no real combined
temperature/SR830 run has occurred and real integration remains uncommissioned.

- Added the temperature–excitation orchestration contract: temperature points are
  ascending on the outside; after one point has passed the existing stability
  predicate, its entire configured dual-SR830 excitation sweep runs on the inside
  before the next temperature point may start.
- The outer temperature preparation continues to archive the stable-window sample
  temperature mean, standard deviation, range and sample count. That readiness
  value remains distinct from each formal Lock-in sample's actual measurement-window
  temperature: attoDRY state is read before and after the paired formal sample and
  its sample temperature is reduced with timestamp-based, time-weighted averaging.
- Incremental progress JSONL preserves state/transition/formal/cleanup evidence and
  partial raw data. The final summary and default formal-sample CSV promote only
  a temperature condition whose full excitation path, Lock-in cleanup and
  post-excitation temperature check completed; rejected/interrupted raw data is
  intentionally excluded from default analysis but retained for audit.
- Resume validates the archived resolved configuration and only skips contiguous
  completed temperature conditions. It never resumes in the middle of an amplitude
  or harmonic: the first incomplete condition repeats from temperature stabilization.
- The future command is
  `python -m attodry_control.temperature_excitation_scan --config ... --authorize-temperature-excitation-scan`.
  That flag is an explicit combined-operation gate, not evidence that real DLL/VISA
  operations have been authorized or run. A distinct future authorization must state
  the allowed temperature and SR830 actions, latch consumption, physical wiring and
  cleanup expectations.
- Offline validation passed: the dedicated fake-DLL/fake-VISA tests cover
  authorization before DLL/VISA/output creation, the actual XX→XY callback bracket,
  stable-window versus formal-window temperatures, formal-window-only averaging,
  parent JSONL/CSV records, inner-sweep failure cleanup, and condition-boundary resume.
  The complete offline suite passed with 396 tests and 5 optional
  matplotlib-dependent skips. No real DLL or
  real VISA resource was opened, and no hardware command was sent for this feature.
- Added read-only temperature-stacked I–V analysis (2026-09-01). The commissioning
  Notebook now discovers completed temperature–excitation summary JSON/formal CSV
  records from a separate remote-friendly directory, filters formal sample status
  and selected temperature conditions, and makes separate amplitude and phase
  figures for every available XX/XY × h1/h2/h3 channel. Curves use the archived
  readback-derived current and actual formal-window mean temperature. Phase repeats
  use circular statistics and are unwrapped only along increasing current for
  display; raw values are unchanged. Synthetic summary/CSV, filtering, multi-run,
  circular-phase, Notebook-compilation, and Matplotlib-render tests passed. No
  hardware module was imported or instrument operation performed.
- The temperature–excitation browser also accepts optional lower/upper archived
  RMS-current bounds. It plots and exports only their intersection with selected
  temperature conditions, records both bounds in the selection manifest, and places
  each temperature legend outside the plot frame on the right. Legend labels show
  only the actual formal-window mean temperature and omit the requested setpoint.
  This remains analysis-only; no instrument path is imported.
- Unified the remaining analysis plots with that presentation: legends are
  outside the right-hand edge, and the commissioning Notebook renders figures
  without printing its raw fitting records. The optional export manifest retains
  all numerical results for audit. This remains analysis-only; no instrument
  path is imported.
- Added a general publication-style plotting layer (2026-09-03). Notebook figures
  now use scoped Matplotlib settings, aligned shared-x magnitude/phase panels in
  place of twin y axes, redundant color/marker/line encodings, explicit sample-SD
  or circular-sample-SD legend text, 600 dpi PNG, PDF, and editable-text SVG
  export. Data selection, filtering, aggregation, scales, and fits are unchanged;
  the style does not claim compliance with a specific journal.
- Refined ordered I–V colors (2026-09-04): temperature-stacked curves use the
  brighter warm `plasma` sequence and multi-frequency curves use the distinct cool
  `viridis` sequence. Existing marker/line redundancy, data, filters, fits, and
  instrument paths are unchanged.
- Added a condensed read-only I–V report figure (2026-09-04). A separate Notebook
  cell selects arbitrary Vxx/Vxy h1/h2/h3 amplitudes and optional phases. Amplitudes
  share the left axis; phase is either absent or placed on an explicit right axis.
  Each amplitude channel has exactly one final selected free-exponent scalar-R
  curve, limited to the measured fit range, with its equation and fit metrics in
  the outside legend. Optional export includes a provenance manifest. Existing
  detailed plots and fit comparisons remain unchanged; no hardware path is used.
- Corrected direct Notebook execution from a source checkout (2026-09-01). Before
  importing `attodry_control`, the first cell now resolves a repository root when
  Jupyter starts in either the root or `notebooks`, verifies `src/attodry_control`,
  and prepends that exact `src` directory to `sys.path`. This removes the editable-
  install assumption without treating a Git branch/worktree name as a Python package.
  The clean checkout executed successfully without `PYTHONPATH`; no hardware path
  was imported or instrument operation performed.

## Stage 7 follow-up - segmented temperature grids

Status: offline implementation complete (2026-08-26); no real hardware operation.

- `[temperature_scan]` now accepts the recommended `temperature_ranges` array:
  each inclusive, strictly ascending segment defines `min`, `max`, `scale`, and
  either linear `step`/`points` or logarithmic `points`. Segments may not overlap
  or share endpoints; every expanded point remains inside the cryostat and
  overshoot limits before DLL loading.
- The legacy `start_k`/`stop_k`/`step_k` single linear grid remains supported for
  existing local configurations, but it cannot be mixed with `temperature_ranges`.
  Both standalone temperature and temperature–excitation scans now use the same
  archived expanded point sequence and segment metadata.

## Stage 7 follow-up - file-only progress monitors

Status: offline implementation complete (2026-09-01); no hardware connection.

- Added `temperature_progress_monitor` and `lockin_progress_monitor`, two
  standard-library-only terminal readers for the fsynced JSONL records. Neither
  module imports a cryostat DLL, VISA backend, SR830 adapter, or hardware config;
  they cannot open COM/GPIB or consume `LIAS?`/`ERRS?` latches.
- The combined scan now writes `lockin_point_ready`, `lockin_formal_sample`, and
  `lockin_point_completed` with the current point index, SINE OUT request and
  existing `SLVL?` readback, readback-derived nominal current, frequency context,
  and formal Vxx/Vxy phase/status data. No extra instrument query was introduced.
- Standalone frequency, excitation, and frequency-by-excitation sweeps now create
  incremental `*_lockin_<scan>_progress.jsonl` files with the same point/formal
  sample contract. Existing final JSON results and rejected/interrupted evidence
  remain unchanged.
- JSONL tailing handles an incomplete final line without inventing a sample; old
  records with no point context display missing fields rather than guessed values.
  Offline tests cover tailing, current temperature/Lock-in rendering, latest-file
  discovery, CLI `--once`, and sweep-point propagation. No real DLL or VISA
  resource was created.
## Three-SMU follow-up - unified live and historical plotting UI

Status: offline implementation complete (2026-09-01).

- Replaced the separate Three-SMU live and analysis Notebooks with one read-only
  `notebooks/three_smu.ipynb` dashboard. Historical data remains completed/accepted/clean
  by default; rejected runs and problem samples require visible Audit opt-in.
- `three_smu_cli run` binds a loopback-only (`127.0.0.1:8765`) event stream before
  opening the session. One formal sample is durably recorded once, then fanned out to the
  terminal FIFO and Notebook stream; no presentation consumer can query, write, or consume
  an SMU status queue. Bind failure occurs before any QCoDeS/VISA resource is opened.
- The Notebook normalizes both sources into sample-wide records and supports arbitrarily
  added line/scatter/live-incomplete-map panels. X/Y/colour can use point/repeat/time or any
  active role's coordinate, source readback, U/I/R/G; series, segment/repeat, and coordinate
  slice controls support gate-indexed multi-curve bias I--V plots without mixing segments.
- Added standard analysis dependency `ipywidgets>=8,<9`, pure live/archived plot conversion,
  loopback stream tests, notebook syntax/import-boundary checks, and fake-session CLI coverage.
  The full offline suite passed 410 tests with 6 optional Matplotlib rendering skips. No real
  instrument was connected, queried, status-consumed, or written.
- The live panels render each Matplotlib figure to an explicit `ipywidgets.Image` PNG rather
  than relying on asynchronous `display(fig)` capture. This keeps charts visible when live
  samples arrive through the Notebook event task; no hardware path changed.
