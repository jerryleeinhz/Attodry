# Project handoff

Last updated: 2026-10-16

## Current stage

Power stability / bounded recovery (2026-10-16), current delivery:
- Implements separate opt-in tuning precision, relative/absolute fluctuation and
  timestamp-based drift slope, with existing formal tolerance meaning unchanged.
  Coarse/fine bounded current steps and same-wavelength measured response history
  remain subordinate to fresh PM evidence; adjustments still happen emission-off.
- Dark-first Combination keeps outer environment/optical ownership. Post-optical
  full electrical check is followed by fresh PM evidence before enabling sources;
  2400 configuration/enable are separated by this barrier. Formal windows freeze
  the light setting, with both PM brackets and full detector pre/post checks.
- Only typed PowerDeviation can retry. Stop/verify excitation and emission first,
  retune/requalify/recheck, and repeat the whole failed window. Same dark reference
  and age are preserved. Failed windows stay in power_attempts, successful windows
  alone feed default analysis. Retry count and total budget span the power visit.
  Hard/unknown/audit/cleanup errors stop; no tolerance expansion or search for light.
- Monitor shows tuning/stability/retry reason, target/actual/deviation/current,
  attempts and cleanup. Unified plotting reuses measured PM power X and labels
  the monitor measurement plane, explicitly not sample-calibrated. Audit views
  show failed attempts separately from successful statistics.
- Existing 15-to-7 verification dedup retained. Relay default restored to proven
  100 ms; faster polling remains explicit offline injection, not production.
  No-excitation relay checks output OFF AND current setting zero, without repairs.
- Final local full regression: 784/784 in 89.179 s. Public Combination simulation
  completed, run a70e85bdf4ce4e819635b4c692d9a57a, 5/5 conditions and cleanup clean.
  Earlier 781/782/783 full runs and the additional persistence tests are retained.
- LK_setup final isolated full regression: 784/784 in 73.958 s, no hardware.
  Owner checks found no acquisition and three idle kernels; operator TOML hash
  unchanged. First wrapper failed on PowerShell native stderr handling, not a
  code failure; named .cmd wrapper then preserved the successful complete log.
- Authorized small trial launched 11:13:20 UTC, run
  db46c3dcc6fd48d8a480be60313a1c33: dark + 5/55/65/75 uW,594.4nm,10nm,
  CH2/10V/NPLC1,3 reads, no excitation. It rejected at serial identity preflight,
  0/5 collected. Raw ENTER contained a DCL line plus an IDN line, separated by
  newline/XON/XOFF; response ownership was rejected rather than silently stripped.
  No optical/output setting command was sent. Last preflight 6221 readback was
  output=0 and current=0; these are NOT post-cleanup confirmation. Automatic
  laser/electrical safe flags remain false in the unchanged rejected record.
  A separate authorized read-only status session then confirmed NKT emission OFF
  and 6221 output OFF/current zero, with no errors or setting writes. This is a
  distinct post-failure observation, not promotion of the rejected cleanup/run.
  No serial access/retry, queue drain, clear, or complete scan followed.
  Real optical convergence, speed and parameter acceptance remain UNVERIFIED.
- Code/test bundle f120d4edea54fd195ad929b8e0dfe206939e1d0b60a15e3b04a5314925735636
  remains under NKT/run_data/power-stability-release-20261016T111000Z, the filled
  local trial under NKT/run_data/power-stability-hardware-20261016T111000Z.
  Source site TOML, active checkout and notebooks were not updated. This Git
  delivery packages the approved speed/stability implementation, tests and docs;
  target active deployment is not included. Do not rerun the failed trial until the
  independent serial communication fault is resolved; current output OFF/zero
  was independently confirmed in postfailure_status.json.
- Final monitor-only follow-up explicitly renders target/variation/trend rejection
  and the exact attempt cleanup in compact/terminal monitor. Delivery source/test
  bundle b35f54fe8bd9349e04d4dafa22cfbf33908d0f96bef3ea9e3dd7a5987c36d8fb
  is verified in NKT/run_data/power-stability-delivery-20261016T112100Z.
  No new instrument run followed that display patch. Operator also confirmed both
  outputs OFF, corroborating the independent read-only status record.
- See docs/POWER_STABILITY_OPTIMIZATION.md for parameters, provenance, retry/data
  contract, timing evidence, example configs and outstanding validation. Evidence
  stays ignored in run_data/power_recovery_20261016. Unrelated existing notebooks,
  PM100D documentation/figures and debug.log are preserved.

The following speed-only entry is historical and superseded by the current
stability/recovery policy and conservative polling default above.


Measurement-speed implementation (2026-10-16): operator approved code changes
and a persistent Markdown review rule. Local implementation and offline acceptance
complete; not committed/pushed/deployed in this delivery, no hardware opened.
The existing electrical adapters/runner now reuse only the exact READ's full
2182A before/after setting evidence at outer sample boundaries; source safety
queries remain fresh. One full check after long optical preparation remains.
Normal single-electrical-coordinate/three-read dark-first power points now use
7 full verification rounds instead of 15, with all 23 queries per round intact.
No passive-detector cache crosses samples, optical visits or attempts; raw
records carry detector_before/after and explicit verification provenance.
Source-capable 2400 guards, 6221 limits/ramp/compliance and cleanup are retained.
Serial relay polling starts at 20 ms and backs off through 40/80/100 ms on
empty replies, under one SEND/ENTER lock and shared injected monotonic clock.
The deadline begins before SEND, clamps requested sleep and rejects even
nonempty late responses; failed sessions remain poisoned. Actual 20 ms device
readiness and real speedup still need authorized target comparison.

New regression first reproduced 15-vs-7 full checks and late-response acceptance.
Ten new fake-only tests cover the command budget, preserved measurement results,
post-optical settings drift, pre/post-READ drift, empty/late replies, deadline
and polling policy bounds. Final full regression: 758/758 in 71.216 s, with
normal Windows TEMP. An earlier sandbox attempt had only TEMP permission errors;
logs remain in ignored run_data/speed_optimization_20261016. NKT and CLD common
CLI simulations completed; CLD monitor confirmed 9/9 accepted with no error.
The immediate-ready fake comparison (dark + 2 lights / 9 READs) reduced serial
queries 1106 -> 554; fake times 111.00 -> 55.80 s at 100 ms polling, then 11.48 s
with 20 ms polling. These are fake-clock results, NOT measured hardware speed.

New docs/MEASUREMENT_PERFORMANCE.md records real timing evidence, current fixes,
safety/verification lifetimes, axis stability, remaining waits, serial ownership,
exclusive timing, failure cost and acceptance procedure. AGENTS.md adds it to
required reading; the module development guide and README/operator guide link it.
Future changes must assess per-point query/write budgets and speed evidence.
No TOML, NPLC, samples-per-window, power tolerances or raw archives were changed.
Automatic power-drift requalification/point retry and separate tuning margin are
documented follow-up design items; current formal drift still rejects the run.
Existing operator Notebook and unrelated local PM100D docs/figures are preserved.

Operator-edited continuation (2026-10-16): explicitly authorized from 562.2 nm
with target tolerance 40%. Fresh owner checks found no acquisition and three
idle plotting kernels. Initial source TOML had only tolerance changed; during
preparation the operator saved it again, removing 530 nm and the 95 uW target.
The stale preparation was refused before profile installation or instrument
access, and retained in ignored run_data/continue562-40pct-20261016T100636Z.
The final operator TOML is used byte-for-byte: nine wavelengths 562.2–820 nm,
10 nm bandwidth, dark then 5/15/25/35/45/55/65/75/85 uW, 90 conditions / 270
formal voltage reads, initial current 32%, 2182A CH2 / 10 V, no excitation.
Only the operator's target-quality tolerance changed; physical limits, minimum
signal and 5 uW spread guard remain. Original file and raw parent data are not
overwritten; parent 100d4413 remains rejected under its original 10% criteria.
Existing --resume-run requires an unchanged whole plan, so this continuation
is a new run linked in its manifest, with a fresh dark for every wavelength.
No partial old baseline/data is promoted into the new accepted attempt.
Local and target synthetic verification passed 90/90 conditions / 270 reads
and all per-wavelength dark matches, without real instrument access.
User default TOML SHA-256:
3bdb6c428ddac6ec594fe69bbc5eb3eb064a7eafb54e87b00bb4406b73772145.
Source code unchanged at 7efdc1c. Run 54e4796fb3ad428a954d9f9a38b2d40a started
10:11:46 UTC (13:11 Helsinki), acquisition PID 6984 / parent 14036, with birth
time/command/hash recorded. Initial readonly checks confirm 2/90 collected
(562.2 nm fresh dark and 5 uW light), progression to 15 uW and no recorded error.
First light formal PM checks included 3.92416 and 3.76846 uW, within the selected
3–7 uW interval. This is progress under the newly selected 40% quality criterion,
not final whole-run acceptance or cleanup.
Evidence/manifest/config snapshot: ignored target
run_data/continue562-40pct-20261016T100955Z. The legacy default TOML filename
and data-directory names remain; actual arrays/run_id/hash identify this scan.
Final readonly status: continuation rejected after 18/90 collected conditions.
At 594.4 nm / 75 uW, the qualified last window decreased monotonically
47.282163 -> 46.484936 -> 45.864872 -> 45.359873 uW (mean 46.247961 uW),
within the user-selected 45–105 uW interval. First formal PM check 15.985 s
later read 44.241187 uW and failed the lower target bound; spread 3.04098 uW
was below its unchanged 5 uW limit. Failure timestamp 10:33:46 UTC
(13:33:46 Helsinki). Process exited/native exit 1; raw laser-OFF and
electrical-safe cleanup independently confirmed with no errors. Parent data
and this rejected record remain unmodified. Final JSON SHA-256:
41e0e8d4304e67e3b0f0d334fd7cec7f31dd40652a0954e2d4f407157d60c006.

Speed analysis requested by operator, files only; no production/config edits,
hardware access or restart. A consistent snapshot of the first 16 completed
conditions (48 formal voltage reads) shows mean cycle 72.062 s, bright-only
73.088 s. Exclusive phase accounting avoids nested-state double counting.
2182 READ round trip averages 0.212 s, whereas wrapped acquisition is 7.832 s;
each full settings verification has 23 serial queries. Normal power points
perform about 15 full verifications (3 in prepare/begin and 4 per voltage read).
Forwarded setting queries average 0.158 s; 5832 completed transactions up to
the snapshot all returned on their first 0.1 s poll (583.2 s requested waits).
This does not establish readiness at a shorter delay. Explicit optical/electrical
settle requests are zero: elapsed communication already satisfies them. Bright
qualification averages 8.561 s, while power-qualification-to-first-formal gap
averages 15.992 s. NKT already avoids unchanged setting writes. Candidate
priorities: deduplicate same-boundary verification while keeping fresh pre/post
READ evidence and the post-optical check; benchmark shorter bounded serial
polling; prevent short trend windows near acceptance edges and optionally
requalify rejected points with finite retry/audit. No optimizations implemented.
Local evidence: run_data/resume562_20261016T100955Z/speed-breakdown.json and
latest-failure-summary.json. Target SQLite snapshots used short read-only
transactions (0.163 s conditions/phase snapshot; 0.553 s I/O snapshot), closed
before compression. No deployment should occur over any later active scan.

Dense wavelength/power operator run (2026-10-16): configured and launched on
the operator's explicit request; first two attempts rejected by low-signal
protection, latest operator-authorized 32% retry rejected by formal power drift.
Full-grid hardware acceptance is pending. Existing
Combination production code is unchanged at 7efdc1c. The wavelength outer axis
uses ten centers 530.0/562.2/594.4/626.7/658.9/691.1/723.3/755.6/787.8/820.0 nm,
each at 10 nm bandwidth. Ideal linspace centers are rounded to the existing
0.1 nm VARIA grid (maximum error <0.045 nm). Each wavelength starts with one
fresh dark, followed by 5/15/25/35/45/55/65/75/85/95 uW; the ten bright points
subtract only that wavelength's matching dark. Total 110 conditions / 330 formal
voltage reads, 2182A CH2 / 10 V / NPLC 1, no current excitation. All physical
identities, isolation, source-current domain and PM100D qualification limits
remain unchanged; unreachable targets must fail rather than relax those limits.
Local and LK_setup offline full-grid verification passed with explicit synthetic
resources/fake clock, including all dark matches; no real device was opened by
those checks. The ignored default trial TOML was backed up and updated after
fresh idle-owner checks; its other 13 local profiles remain unchanged. Config
Initial profile SHA-256 b2422442a0bf2967e3f763a3032d19e867ba8d98828c631dcc17dec1521c4ab8.
First run 9a6e968df180416d9176e7423fb1fead recorded one dark, then rejected the
first 530 nm / 5 uW qualification: fresh PM100D reading 0.0546941301 uW at
26.1% source current was below the unchanged 0.1 uW minimum-signal guard.
No feedback adjustment was attempted. Raw cleanup separately confirms laser
OFF and electrical safe; native exit 1. Preserve the entire rejected run in
ignored run_data/dense-wave-power-20261016T092132Z (raw JSON SHA-256
493d322b4e1c5020673d548846f0d8545c2a55766f1f267ed50b1321c8f6fad2).
The operator confirmed the beam path and explicitly requested retry with 28%
initial current. Only that initial value and note/comments changed; all guards
and actuator bounds remain. Both local and target offline full-grid checks
passed again. New default SHA-256
ef579c601882fbca57ba6f5d92d5333a9429120566a875e55428bceac4dcc97b.
Traceable retry 42b1a3cedcd549e5819803c1ff4827ff started 09:36:26 UTC,
acquisition PID 4956 / parent 1452, birth time and command recorded.
It also recorded only its dark, then rejected its first light qualification:
PM100D read 0.0519594217 uW, NKT current readback 28%, filter 525–535 nm,
PM wavelength 530 nm and questionable status zero. No feedback adjustment
occurred. Raw cleanup confirms laser OFF/electrical safe, native exit 1; the
source JSON hash is e8786854c272a5848d823976216c40aee18a5317185bf7b74f8a50ad64f0789d.
These are single startup reads, not proof of persistent low power or a blocked
path. Requested the operator's stable manual PM100D reading at the same
530 nm / 10 nm / 28% settings. The operator later explicitly cancelled the
standalone 28% read and requested a direct 32% retry; no separate PM read was run.
Evidence/config backup/manifest: ignored run_data/dense-wave-power-28pct-20261016T093434Z
on LK_setup; data database: run_data/nkt530-820_dark5-95uW_10x10/audit.sqlite.
Only initial source current and note/comments changed to 32%; local and target
110-condition/330-read offline checks passed again. The current ignored default
SHA-256 is 2e143a78442ee1b4c9c7af8f2687bf8cdeacfa1d7cdc8a5a97720095a0ce1510.
Third run 100d4413cc2f4f02960e5d86100a236a started 09:44:09 UTC (12:44 Helsinki),
PID 1684 / parent 6300, birth time and command recorded. It passed preflight
and collected the 530 nm dark and first 5 uW light (2/110 conditions); the
first light's formal PM brackets were 5.1555–5.33159 uW, within target tolerance.
Readonly monitor showed no error and progression to 15 uW. This is initial
progress, not final acceptance or cleanup of the full active run.
New evidence/config backup/manifest:
ignored run_data/dense-wave-power-32pct-20261016T094237Z on LK_setup.
Final read-only diagnosis: the 32% run collected all eleven 530 nm conditions
and the 562.2 nm dark (12/110 collected, 36 valid voltage reads), then stopped
before the first voltage read of the 562.2 nm / 5 uW light. Qualification's last
window was 5.032687–5.354479 uW (mean 5.194956 uW); first formal PM read about
15.989 s later was 5.814519 uW, outside the unchanged 4.5–5.5 uW target interval.
The reported combined spread 0.781832 uW was BELOW its 5 uW limit: target error,
not the spread check, triggered rejection. Source current readback stayed 30.3%
after qualification. Electrical begin/prepare took 7.664/3.760 s in the gap.
The 2 s power window plus 0.5 s hold accepted a short in-tolerance interval;
later readings show upward drift. Logs do not establish its physical cause.
Failure PM timestamp 10:00:43 UTC (13:00:43 Helsinki). Process exited, native
exit 1; raw laser-OFF and electrical-safe cleanup independently confirmed,
no cleanup error. Entire run remains rejected, partial raw data retained.
Raw source JSON SHA-256 5cc9ffd261dd354db2aec98b04e08698821ca5097eac7ded398db0b468e257d8.
File-only diagnosis is local run_data/dense_retry32_20261016T094237Z/drift-diagnosis.json.
No retry, hardware connection, threshold/configuration change or deployment was
performed for this diagnosis. Keep all
raw failures; do not lower the minimum signal threshold, automatically search
for light after a low-signal stop, or deploy over an active scan. The legacy
default TOML filename remains; its arrays and config hash identify the new grid.

Real six-point operator check (2026-10-16): the explicitly authorized single run
fbf086a490de4eab8ef6ba34160b764e completed 6/6 conditions on source 54b2375.
NKT 630 nm / 10 nm, dark then 20/40/60/80/100 uW, 2182A CH2 / 10 V, no excitation;
6221 remained a relay host. Launch PID 4444 was matched by parent, creation time
08:38:00.622862 UTC and command. Final monitor arrived 08:46:16 UTC (~495 s).
Raw completion confirms laser-OFF and electrical-safe cleanup, with no errors.
The file-only monitor observed collected counts 1 through 6, kept acceptance at
zero while active, then displayed completed/accepted 6/6 and exited zero; both
stderr logs are empty. No hardware fault was deliberately injected.
Unified plotting loaded the exact run, verified one shared dark subtraction and
rendered two five-light-point cards through widget callbacks. PNG/PDF/SVG/CSV,
plot setup and an independent unified_result Notebook are in ignored target
run_data/real-six-point-20261016T083423Z; original JSON/config hashes stayed unchanged.
Existing operator Notebook edits were not overwritten. This is one scan with
three reads per condition, one shared baseline and no time-drift correction.
The scan exposed stale final Attempt wording in monitor. The read-only projection
now derives the last condition's final status from its stored accepted flag, keeping
all raw progress and failure records intact. Correction b5bf8a7 passed 748/748
locally and in a fresh LK_setup clone, plus read-only checks of this completed
run and the historical rejected run. After a fresh idle-owner check, active NKT
fast-forwarded 54b2375 -> b5bf8a7; the active monitor now reports the final Attempt
as accepted, with 6/6 accepted and verified recorded cleanup. All 14 local TOMLs,
three operator Notebook files and the original JSON retained their hashes.
No second real scan or extra instrument access occurred for the display fix.
Its offline/deployment receipts are in run_data/deploy-unified-operator-20261016T085516Z.
The detached cmd wrapper's exit receipt was blank (redirection syntax); retain it
and do not claim a native acquisition exit code. Completion/cleanup are established
by the raw application record. No second real scan is authorized or needed here.

Target delivery resumed at operator request (2026-10-16). The independent scan
d4dfbb1e completed all six conditions at 08:07:54 UTC, with recorded laser-OFF and
electrical-safe cleanup; its process exited and both plotting kernels were idle.
The operator profile already contains dark/20/40/60/80/100 uW, 630 nm / 10 nm,
2182A CH2 / 10 V and no excitation. Preserve its physical settings and update stale
comments only (plus descriptive note metadata). An isolated target run exposed a
startup-summary error when the valid voltage-only profile omits a current bound.
Fixed the DC summary to display only configured bounds; the new regression
reproduced the failure then passed. Local full regression passed 748/748 and the
six-point CLI simulation completed with one shared dark baseline. Fixed release
696e338 then passed 748/748 on LK_setup lyr in a fresh clone, plus the six-point
simulation, read-only monitor, shared-baseline/plot/export checks, module entry
with PYTHONPATH unset and historical failed-record analysis. The completed real
six-point archive also plotted successfully without modification; this is
file-analysis evidence, not a new hardware run.
After a fresh idle-owner check, active NKT fast-forwarded 6d1ea2f -> 696e338.
Photoresponse_Analysis and the prior trial TOML were backed up before update;
the new unified_plotting Notebook matches the tested release. All 13 other local
TOML hashes and the edited SR830 Notebook stayed byte-for-byte unchanged.
The default trial filename/output directory retain their legacy 50-300uW/dark7
names, but the documented authoritative array is dark/20/40/60/80/100 uW.
Only stale comments and note metadata changed in the real TOML; its numerical
parameters, source-current mode and physical protections were preserved.
Active root-module/config and monitor checks passed; monitor reads the completed
d4dfbb1e run with 6/6 accepted conditions. Records/backups/exports:
run_data/deploy-unified-operator-20261016T082501Z. Failed isolated attempts
080951Z/081701Z remain for audit; the first short public smoke profile exceeded
its 3 s simulated qualification budget, and the second exposed the summary bug.
The requested six-point offline copy explicitly uses the synthetic actuator
domain (0.1-10%, initial 1%) instead of the real 26-100% domain; hardware values
were not relaxed. No instrument access or real scan start occurred in this delivery.

Unified Combination operator workflow (2026-10-16): approved by the operator.
`combination_cli run` calls the same DC acquisition service as the compatible
photoresponse entry, with existing default TOML/run-command permission and
unchanged hardware limits, dark matching and cleanup. Launch/progress/error views,
atomic latest-run registration, WAL-only monitor and unified plotting now use
the reference public interfaces from Integration-photonics `39e7e9f`.
DC adapters preserve photo_* storage and legacy archives. Monitor distinguishes
collected points from final acceptance and reports recorded timestamps, not live
instrument state/process liveness. New compact progress avoids full event replay.
File tools do not import drivers; shared package exports are lazy and compatible.
Plotting retains dark/light/delta, baseline provenance, requested/actual power,
source settings, T/B and semantic gate/leakage columns. Failed/active records need
explicit audit selection; overload sentinels never become numeric plot values.
Condition means are not duplicated as independent repeats (n=1 error undefined).
Local full regression: 747/747, plus NKT/CLD public-entry simulations and monitor.
Functional release `2ca9e20` and fixture correction `0b482d9` are committed/pushed
on module/integration-photovoltage/current. Target validation/deployment is now
complete with the voltage-only correction recorded above.
The first isolated suite exposed an old
mocked CLI test's dependency on uncommitted hardware.local.toml; fixed its explicit
simulation-file fixture and retained the failed clone/logs before retrying.
The next target owner check found a newly started independent photoresponse run;
validation stopped before cloning/updates. The operator initially requested code
delivery only and deferred deployment, so target HEAD stayed 6d1ea2f at that time.
The later idle-only deployment request is completed above. Earlier checks found two idle
plotting kernels and edits in Photoresponse_Analysis/SR830 sweep notebooks.
The authorized backup/update of Photoresponse_Analysis and preservation of the
SR830 Notebook have now been verified. Recheck active scans before future updates.
See `COMBINATION_PHOTORESPONSE_GUIDE.md` for usage and pinned reuse provenance.

Latest read-only real-run diagnosis (2026-10-16, run 9cf67ba6): dark and 200/400 uW
windows were recorded; the 600 uW formal check read 609.405 uW within target +/-5%,
but combined reference/formal spread 7.434 uW exceeded 5 uW. Entire run rejected,
raw records retained, laser OFF/electrical safe cleanup confirmed. No restart or
threshold change was performed. This is separate from the earlier register-0x37 failure.

Simple daily photoresponse run (2026-10-16): the operator approved executing
`python -m attodry_control.photoresponse_cli run` as the confirmation to acquire
with the selected TOML/modules/route, without repeated authorization flags or
new TOML authorization switches. `run` defaults to the existing ignored
`config/photoresponse_trial/11_nkt_630nm_dark_50-300uW_7pt_2182.local.toml`;
`--config` and old flags remain accepted. Missing default never falls back.
CLI passes execution permission only to selected modules; internal safety gates,
bounds, physical confirmations, cleanup and diagnostic/bench permissions remain.
Run JSON/JSONL/SQLite record config path/hash, selected modules and run-command
permission source. A project-root package entry loads this checkout's src even
when lyr's existing editable install points at another project; shared installation
is untouched. Feature `8b33188` is committed, pushed and deployed from `313db00`;
both local and isolated LK_setup full suites passed 694/694 plus target NKT/CLD
simulations and a module-entry check with PYTHONPATH unset. All 14 station TOML
hashes stayed unchanged; no real instrument connection or scan was started.

Latest real evidence (2026-10-15): CH2 at 1 V still returned +9.9E37. The operator
then authorized 10 V range while retaining the 1 V sample bound: three dark READs
were valid (2.698/2.914/2.437 mV), but the full run remained rejected after NKT
requested 2.1% from 6% to reach 50 uW and register 0x37 readback mismatched.
Laser OFF and electrical safe cleanup were confirmed. No bright voltage point
was accepted; source minimum/attenuation needs separate resolution. Later operator
edits changed power targets/caps; preserve the current local configuration and
validate its bounds rather than silently changing experimental parameters.

2182A channel selection correction (2026-10-15): the operator confirms the
sample voltage is wired to channel 2. Earlier no-excitation scan attempts
remain rejected: COM8 was held by NKT CONTROL; after its authorized closure,
one relay identity reply contained an extra DCL line; a fresh read-only session
passed, then the channel-1 dark READ returned +9.9E37. No bright point was
measured; the last attempt confirmed laser OFF and electrical safe cleanup.
Added optional `photoresponse_2182.channel` (1/2, legacy default 1), channel-specific
ranges and explicit CHAN2 range/filter/relative settings and readbacks. Channel 2
offers 0.1/1/10 V, not channel 1's 0.01/100 V. The station's reviewed 630 nm dark+7
profile selects channel 2 at the existing 1 V range and retains its 1 V sample
bound; alternatives are comments. This change does not restart acquisition or
claim channel-2 hardware acceptance. Offline validation/deployment is recorded
in `docs/DEVELOPMENT_STAGES.md`. Functional commit `8ca4e88` is pushed and deployed;
both local and isolated LK_setup full suites passed 684/684, plus target NKT/CLD
simulations. The operator TOML was backed up and updated separately; no instrument
connection or acquisition occurred during this correction.

Dark-first photoresponse and timing optimization (2026-10-15): software/offline
complete. Explicit `power_feedback.power_points = ["dark", positive W, ...]`
selects one dark electrical scan per outer repeat/environment/optical visit;
light leaves subtract the matching bias/SMU dark coordinate. New outer ordering
holds light throughout inner electrical scans and qualifies power once before
electrical configuration/output. Legacy dark/light/dark configs and their plan
hash shape remain supported. Actual temperature/field plus quality are retained,
with strict safety/drift checks and the 3 T invariant unchanged.

Reduced nested passive-2182 configuration queries and same-call duplicate
post-READ verification; kept fresh source/relay and full 2400 guards. Monotonic
remaining waits credit optical qualification and confirmed 6221/2400 setting
time; pinned SMU waits stay conservative. Optional same-scan previous actuator
start never reuses qualification proof. Timing audit retains failed operations.
All raw data, single baseline ID/age, source configuration and cleanup remain
recorded; defaults export accepted light differences only.

Validation: **678/678 local tests**, including 21 new dark-scan/timing tests;
**283/283 isolated LK_setup lyr offline tests** (180 photoresponse, 24 PM100D,
35 NKT, 44 legacy SMU), 172-file manifest verified, both new NKT/CLD CLI
simulations passed locally and on target. No instrument connection/output or
active target checkout update during offline validation. The operator subsequently
confirmed both Jupyter kernels are plotting only and authorized commit, push and
deployment to the current `module/integration-photovoltage/current` branch.
Functional release `0b05f904eb8b47da1369e204aa5ca06d42ae97ae` is committed,
pushed and deployed. LK_setup NKT fast-forwarded from `46c7b82` to `0b05f90`;
both local and station branches now track `module/integration-photovoltage/current`.
A fresh origin clone on lyr passed **678/678 full tests** plus NKT/CLD simulations
with confirmed zero exit codes before deployment. All **13 local TOML config
hashes** stayed unchanged; active tracked files are clean. Independent plotting
and offline-validation processes were left alone. The first deployment verifier
lost the native ExitCode despite an OK log; its raw attempt was retained, the
verifier repaired and validation repeated in a new clone before any update.
Delivery records live in ignored `run_data/deploy-dark-20261015T1846Z` on target.
This delivery changed no experimental parameters and contacted no instruments;
real timing/transient comparison remains separate from these offline results.
Usage: `docs/PHOTORESPONSE_GUIDE.md`; timing evidence and limitations:
`docs/PHOTORESPONSE_TIMING.md`.

CLD1010LP (2026-10-15): the persistent CONN=1/1, MEAS=8192 readings were a
software gate, not an instrument refusal. With the operator's explicit
acknowledgment (`acknowledged_connection_diagnostics`, exact pattern only) the LD
was driven at 10/15/20 mA and in a PM100D current-mode scan. Threshold is near
26 mA and slope about 21 uW/mA; 50 uW qualified at 28.1 mA, but the controller's
own LD current-limit detection tripped at 29.6 mA (MEAS bit 3), so 100-200 uW
need a higher, datasheet-confirmed current limit. Bit 3 stayed set after cleanup
and blocks the next LD enable until it clears. Details in
`docs/DEVELOPMENT_STAGES.md`.

NKT constant-power real scan (2026-10-15): accepted 50/100/150/200 uW at
633 nm / 10 nm with PM100D feedback in constant-power mode (formal means
49.95/101.71/150.42/201.49 uW; setpoints 11.9-48.6%). Emission off and both
instruments closed afterwards; the source is left in constant-power mode at a
48.6% setpoint. Details in `docs/DEVELOPMENT_STAGES.md`.

NKT constant-power feedback (2026-10-15, offline complete): PM100D feedback can
now drive the SuperK power setpoint (`actuator = "source_power"`, proportional
step capped by `source_power_step_pct`) with `[nkt_source] control = "power"`.
Optional `apply_control = true` lets preflight switch a healthy emission-off
source between constant current and constant power. Every step still turns
emission off. No NKT hardware was contacted; real mode switching, single-step
writes and illuminated qualification each need their own authorization. See
`docs/DEVELOPMENT_STAGES.md`.

CLD-only power commissioning preparation (2026-10-15):

- New-chat guide: [goals, development workflow and CLD next steps](PHOTORESPONSE_NEW_CHAT_HANDOFF.md).
- Latest operator evidence: manual laser output works and the front panel shows
  no ERROR. No simultaneous diagnostic snapshot during manual emission exists
  yet. Automatic acquisition was blocked by software preflight, not by an
  observed rejection of an LD-enable command. Do not infer hardware damage or
  a harmless status flag; compare documented diagnostics in OFF and manually
  operating states before considering a state-specific policy change.
- Operator requested six linear PM100D power targets, 0.05/0.64/1.23/1.82/2.41/3 mW,
  at the confirmed 730 nm diode wavelength. The operator confirms the cryostat
  sample is blocked and only the power meter is illuminated for this test.
- Prepared a single-use, ignored CLD/PM entry that reuses CldController and
  PhotoresponsePower directly. It never constructs electrical, SMU, SR830 or
  environment hardware. Current remains limited to 30 mA; the test's measured
  power ceiling is narrowed to 3.3 mW, not asserted as a detector rating.
- Connection diagnostics are checked before any settings write. All six points
  require the existing stable-power qualification, ten fresh formal readings and
  verified LD zero/off, original TEC switch state and independent resource close.
- Twelve synthetic entry tests passed, including zero-write rejection of the
  actual diagnostic pattern, meter/open/close failures, STOP, audit failures and
  no electrical hardware factory. Existing CLD VISA 31 and power 26 tests passed;
  power tests required an explicitly writable workspace temporary directory.
- Target lyr passed offline configuration and 118-file manifest validation.
  A separate, previously registered query-only CLD preflight still reports the
  same unresolved CONN/MEAS indications with LD off and stable TEC operation.
  No LD enable or six-point power acquisition occurred. No power-feedback hardware
  acceptance is claimed; production protection logic and electrical configuration
  remain unchanged. The operator questioned the restart suggestion: restart is
  an optional diagnostic, not an established requirement or proven remedy.
- Follow-up checked current official DOC-104772 Rev B (2026-05-27), DOC-104674
  Rev B (2026-05-18), and the published firmware 2.3.0 release notes. The newer
  documents retain the connection/socket-failure definitions and provide no
  model/polarity exception or supported software recovery. A separate fixed VISA
  query supplement confirmed keylock and LD overtemperature trips clear, expected
  AG polarity, and TEC power-on auto-start disabled; connection indicators persist.
  This narrows diagnostics but does not certify physical damage or a harmless flag.
- Static vendor-source review found that native TL4000 initialization clears
  status/error state even with reset=false and native getters normally consume
  the error queue. Do not use native DLL initialization as a query-only cross-check;
  the current real diagnostic uses VISA and performs neither clear nor reset.

DC photoresponse - reference reuse, Three-SMU and control-only acceptance (2026-10-13):

- Operator now confirms wiring/isolation and selects Integration-photonics local
  configuration reuse for P0/P1. SuperK code/configuration is reused without more
  real verification. P6/P7/P9 and SMU joint scanning require offline acceptance only.
  P2/P3 are no-sample control tests; scientific data acceptance is not required.
- Reused three original Three-SMU files byte-for-byte from source `016d741` in
  `photonic_smu/`, isolating newer point-session APIs from the legacy independent
  module. Added the thin selected-role adapter, smu combination axis, complete
  V/I/leakage evidence, zero-disable cleanup and consistent SQLite/analysis acceptance.
- New `smu_current_voltage` / `smu_voltage_voltage` select an independent SMU source
  and 2182A voltage detector; 6221/2400 simultaneous sample sources and duplicate
  resource ownership reject before opening hardware. OFF roles remain unopened.
- NKT source-current profiles may omit ND/PP as in the reference configuration:
  no unselected register write occurs, actual settings are pinned and drift rejects.
  Temperature/field remain paired points with the existing <=3 T invariant.
- Reference hardware.local.toml contains duplicate keys. Extracted valid station
  tables into a separate ignored profile; the valid photonics.local.toml supplies
  PM/source/gate configuration. No original reference or local file was overwritten.
- P2 real configuration, fresh READ and cleanup completed. Input still returns
  +9.9E37; this is rejected raw voltage, not an accepted sample measurement.
- P3 verified all five approved current settings with output OFF, then zero/OFF.
  The authorized output attempt again hit empty-load compliance and safely stopped;
  five-point current delivery is not passed, while its control/protection path is tested.
- Real 6221 OFF still reports COMP=8. Fixed only the inactive interpretation:
  preserve the bit, check OFF/identity/unarmed/unsafe/QUES, and do not treat it as
  active supply compliance. Every output enable, including 0 A, checks compliance
  immediately. No status clear/reset was introduced.
- PM console/sensor match the reused configuration. Wavelength correction and
  averaging writes/readbacks passed; fresh READ produced positive/negative dark
  noise. No illuminated closed-loop power measurement is accepted.
- CLD LD was OFF, TEC near 26 C. Lowered saved LD limit from 140 mA back to the
  approved 30 mA without changing TEC. KEYL/OTEM/INTL/TEMP/TEC protections are clear
  and TEC compliance mode is PROT; mount70 is reported, while CONN=1/1 and MEAS=8192
  persist. Operator reports no front-panel error. Official SDK defines CLD mount/
  socket bit13 separately from TED/ITC cable bit12 and provides no LP70/FW exception.
  Cause remains unresolved, and automatic LD enable is still blocked; no bypass,
  installer, driver update, hardware SMU or environment write was performed.
- Local full regression **648/648** passed. Actual CLI/CSV simulations passed:
  NKT 16 conditions and CLD 8 conditions with selected SMU roles. LK_setup `lyr`
  passed **307/307** isolated offline tests: photoresponse 152, pinned SMU 54,
  PM100D 24, NKT 33 and legacy SMU 44. Source `f609d2c` was fast-forwarded to its
  active NKT checkout, with nine original local files unchanged by SHA-256.
- Prepared six new ignored 05-10 combination profiles, a read-only validator and
  a Chinese guide under the target photoresponse_trial directory. All profiles
  validate: NKT/PM 6, CLD/PM 6, NKT/gate/PM 66, CLD/6221/PM 30,
  environment/gate/PM future 66 and channel-current future 6 conditions.
  Unknown top/bias SMU roles stay OFF; channel-current wiring stays unconfirmed.
- Configuration migration is explicit: Cartesian targets become an ordered axis;
  initial current resets to the optical row per condition rather than `previous`
  warm start. Reference field readback tolerance maps to field_tolerance_t; the
  legacy setpoint acknowledgement field is not exposed. Future environment points
  and step limits are editable plans, not confirmed present temperature/field.
- A post-deployment passive electrical snapshot passed, opening only the 6221
  host and querying 2182A. It confirmed zero/OFF and unarmed modes; MEAS was now
  0, so this is communication evidence, not a fresh reproduction of OFF-COMP=8.
  No additional READ, output write or status clear was issued. Private addresses,
  local configuration and all real audit records stay ignored.

DC photoresponse - full-goal correction and power/combination integration (2026-10-13):

- The user's complete goal is NKT wavelength/power combinations or fixed-wavelength
  CLD power control, PM100D feedback to stabilize measured W, and electrical
  windows retaining 2182A voltage plus selected 6221 settings/status/excitation.
  Optional 2400 channel current and paired temperature/field points use the same
  selected-module approach. Existing Three-SMU roles remain independent reusable
  modules; this correction does not claim their photoresponse integration.
  Setpoints never become fabricated measurements.
- Audit of source commit `d801dc4` found no PM100D connection in PhotoresponseRunner
  or its CLI and no power-target axis. NKT's optional standalone PowerMeter injection
  was not used by the photoresponse path. CLD controlled LD current without optical
  power feedback. The fixed Cartesian dark/light/dark loop was not the complete
  combination entry point requested by the user.
- Withdraw the broad interpretation of earlier "P1-P7 software complete". That
  statement described the narrower open-loop work package, not the complete user
  goal. PM100D feedback is a core requirement of this experiment; it must not be
  postponed merely because PEM was developed in a separate work package.
- Reuse the committed PM100D driver and PowerWindow/feedback strategy from
  `codex/pem-module` (`a8d991a`) and the combination semantics in
  `integration-photonics-nonlinear-hall` (`016d741`). Their historical optical
  acceptance is separate evidence, not acceptance of the current CLD/sample path.
  Do not wholesale merge incompatible SR865A or >3 T active paths.
- Integration is offline complete. Preserve current controller/cleanup protections,
  explicit wavelength calibration and PM measurement-plane metadata. Real-time
  records are sequentially acquired with timestamps; no simultaneous trigger claim.
  No additional hardware connection or output is part of this offline correction.
- Delivered optional PM100D feedback, bounded source%/ND/CLD-current tuning, stable
  qualification and frozen formal light windows, target/spread drift rejection,
  independent meter cleanup, ordered optical/power/bias/environment/repeat axes,
  complete selected-module evidence and power/axis CSV fields. Dark phases query
  meter status without READ or feedback search; actual W is measured during light.
- No-excitation 2182A may omit its unused current bound. Relay-only 6221 snapshots
  are recorded query-only; setpoints remain settings, not delivered-current readings.
- Full local regression **568/568**, including **129 photoresponse** and **24 PM100D**,
  passed. Independent real CLI simulation and CSV checks passed for NKT 8 and CLD
  6 conditions. LK_setup `lyr` (Python 3.12.13) passed **153/153** photoresponse/PM
  tests in a separate temporary directory. Its NKT checkout was fast-forwarded to
  source commit `5b6194b` on the same branch, with seven local profile/guide files
  verified unchanged by SHA-256. No instrument was opened.
- The operator's 02 no-excitation profile has timeout_ms=1000 and NPLC=1, below
  the existing 1080 ms minimum. Preserved that file and prepared a separate ignored
  `02_cld_2182_no_excitation_timeout_fixed.local.toml` with timeout_ms=2000 and
  omitted unused max_abs_current_a; its actual schema validation passed. Other
  previously prepared profiles remain unchanged. Real PM identities/power limits
  and optical/electrical joint commissioning are still required for hardware runs.
- Older PM100D/PEM planned handoff notes are historical to this checkout. Later completed
  code exists in the branches above; do not use those old notes to justify rebuilding
  the driver or to claim it was never implemented.

DC photoresponse - editable trial configuration deployment (2026-10-13):

- The operator confirmed 2182A input is floating. Retain successful identity/
  configuration communication and the rejected overload; do not repeat voltage
  acceptance until an appropriate input fixture is connected.
- The station 2400 is connected between bottom gate (SiO2/Si) and device source.
  It is a **gate-bottom bias/leakage role**, not the source-drain photocurrent
  detector. The earlier 0 V / 10 nA attempt remains rejected and zero/off/HIMP
  was confirmed. No gate-current data are to be labelled channel photocurrent.
  The existing station TOML has no active photoresponse profile; a detector profile
  must keep wiring unconfirmed until a separate channel connection is established.
- Added production **CLD USB/VISA SCPI** backend using the existing VisaTransport
  and CldController; no TL4000 DLL is required for this backend. Native TL4000
  remains selectable. Strict enums map OFF/ENAB/PROT to 0/1/2, each write checks
  readback/error state, current limits are checked before LD enable, and individual
  USB transactions are capped at 10 s independently of the thermal timeout.
- CLD cleanup independently attempts LD-off, current-zero, owned TEC-off and final
  confirmation even after an earlier failure. Qualified TEC sensor/overtemperature/
  compliance protections block all preparation. LD temperature trips and unresolved
  connection diagnostics block point preparation and emission, while TEC may settle
  with LD off. Unknown communication/readback still fails closed.
- The operator confirmed a suitable beam collector, 30 mA maximum LD current,
  explicitly unspecified diode model, and permission to inspect cached TEC values.
  The reported 8.5 V MAX response is a measurement capability, **not a cached
  diode voltage rating**. In constant-current mode, the extra software voltage
  threshold is optional; do not require a guessed diode voltage to operate.
  Omit `max_voltage_v` to disable only this extra threshold; snapshots record null.
  Voltage readbacks remain recorded and invalid/overload values are rejected.
  Configured positive thresholds still reject readings above them; current limits,
  TEC/interlock/protection, wiring and authorization requirements are retained.
- TEC-only attempt used the saved 17 C / 0.85 A settings, changed LD temperature
  mode to PROT, narrowed the protection window and enabled TEC briefly. Its first
  diagnostic sample reported connection protection; the attempt was rejected and
  **LD zero/off and TEC off were confirmed**. No LD output-on was issued.
- The operator subsequently reported normal manual laser operation and changed
  the temperature target to 26 C. Passive reads confirm TEC on, about 26.000 C,
  0.158 A TEC current, LD off and 30 mA stored current/limit; temperature protection
  is clear. A five-second passive thermal dwell passed without output/settings writes.
  Preserve these operator settings; do not restore the obsolete 17 C target.
- Both CONN queries still return 1 and MEAS condition remains 8192 with no SCPI
  errors while TEC works. Withdraw the earlier claim of a proven physical cable/
  socket fault. The native generic cable API excludes CLD; raw SCPI diagnostics
  remain semantically unresolved on this firmware, not inverted into connection-ok.
  Asserted/unreadable diagnostics still block automatic LD enable; never bypass
  them or reset/clear them merely to obtain a pass.
- Corrected temperature protection as a full span: the approved interval must
  contain target +/- span/2, and stability tolerance cannot exceed span/2. LD
  temperature protection is explicitly queried and must be clear before emission.
- Validation: **503/503 local offline tests**, including **88 photoresponse tests**;
  **88/88** also passed in LK_setup's `lyr` in a fresh temporary directory. The new
  backend's real read-only snapshot and output-blocking check passed. Target RAM
  exhaustion was traced to an orphaned read-only PowerShell; with explicit user
  approval only that process was stopped. Python recovered; other jobs were retained.
- Follow-up validation: **511/511 local offline tests**;
  **96/96 photoresponse tests** on LK_setup. The updated
  production backend passed passive thermal verification. No remote LD enable
  has been issued; connection diagnostic qualification remains pending for
  automatic emission. The former mandatory voltage-bound requirement is withdrawn.
- Optional voltage-monitor validation: **519/519 local tests** and
  **104/104 target photoresponse tests** passed offline. No instrument
  was opened in this follow-up; existing hardware state was not changed.
- Prepared editable target-local trial profiles in `config/photoresponse_trial/`:
  CLD + 6221/2182A voltage, CLD + relay 2182A without excitation, future
  CLD + 2400 channel current, and an independent 6221 five-point test. Created
  the absent default `config/hardware.local.toml` as the first profile's copy.
  These are independent files; subsequent edits are not silently synchronized.
- Target configuration fills confirmed identities/resources and approved limits,
  with initial LD 10 mA / TEC 26 C and an omitted optional LD voltage threshold.
  Electrical wiring/isolation confirmations remain false, consistent with the
  floating 2182A and gate/source 2400. No protective diagnostic is bypassed.
- All four profiles and the default copy passed actual schema/constraint checks
  in the target's existing `lyr` environment using source commit `08ad479`.
  The reusable validation script and parameter/command guide are installed beside
  the profiles. No instrument connection, output/settings write or acquisition was
  performed. Source code was already current and was not replaced.
- New local profile files are Git-ignored through a scoped target-local exclude;
  the default was already ignored. Existing SDK folders/bundles and other files
  were preserved. SuperK physical parameters and optional attoDRY joint setup
  remain separate confirmed profiles; they are not inferred from this CLD trial.
- 6221 empty-load five-point sweep, valid voltage/current input fixtures, full
  optical dark/light/dark and attoDRY integration remain pending. Raw attempts,
  station addresses, identities, protection states and runtime diagnostics stay ignored.
  Preserve pre-existing PM100D/PEM notes and figures separately.

NKT Photonics offline implementation update (2026-09-22):

- User approved scripts first, manual optical routing, LK_setup as target, and
  operator-supplied future experiment/power settings. No real instrument testing
  is authorized by this stage. CONNECT range and detailed topology are deferred.
- Added strict NKT configuration, three-mode simulation, a standalone controller,
  explicit-point scans, optional PowerMeter Protocol, and JSON/JSONL audit. Added
  a minimal ctypes adapter verified against official SDK 2.1.16.3027 headers for
  EXTREME/VARIA. No new pip dependencies. See [script guide](NKT_CONTROL_GUIDE.md).
- Source/ND percentages and filter-edge setting readbacks remain separate from
  actual optical measurements. Optional VARIA monitor uses percent; absent
  monitors/meters produce null, not fabricated W. LLTF bandwidth is not writable.
- EXTREME/VARIA adapter and N1 logic are offline complete. LLTF supports simulation
  and the shared backend contract, but its real SDK/ABI is unavailable and hardware
  mode fails before DLL loading. Complete N0 closure and N3-N7 remain pending.
- Authorized SSH software inventory on LK_setup found no NKT CONTROL, PHySpec,
  LLTF or NKTP SDK in uninstall entries/common directories (search depth four);
  NKTP_SDK_PATH is unset. lyr Python exists. No install, GUI launch, port discovery,
  driver load, instrument command, code deployment or target test was performed.
- Local offline tests cover strict config, fake registers/C ABI, intermediate
  emission, motion timeout, identity, authorization, Ctrl+C and failed cleanup.
  Current validation totals are recorded in DEVELOPMENT_STAGES.md. Existing
  uncommitted SR830 figure work remains separate from this module commit.

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
- Harmonic-scaling figures now include a right-hand formula panel listing every
  log/scalar/complex model and its fitted coefficients, exponent, AICc, and
  relative RMSE. Curve legends use the corresponding numerical formula, making
  the model comparison directly readable without inspecting the JSON.
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

## Immediate next implementation tasks

1. Integrate `measurement_temperature_k` into the downstream measurement coordinator
   and hardware-test the interruption/resume path separately; do not infer or alter
   PID values automatically.
2. Perform staged attoDRY small-movement commissioning only after a new explicit
   write authorization and operator-selected smallest practical targets.
3. Run Three-SMU S1 target-offline validation in `LK_setup` `lyr`, then fill
   the ignored local addresses and safety values. Any real connection or setting
   write still requires a separate plan-specific authorization.
4. Freeze and verify the complete hardware wheelhouse on the offline control
   computer after its Python/VISA environment is known.
