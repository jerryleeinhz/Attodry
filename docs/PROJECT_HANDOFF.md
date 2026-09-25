# Project handoff

Last updated: 2026-09-24

## Current stage

### SR830 sweep comparison X-axis scale (offline; 2026-09-25)

The multi-run paired comparison in `notebooks/sr830_commissioning_sweeps.ipynb`
now has an X-axis selector for per-scan defaults, logarithmic, or linear scale.
The default preserves frequency-log and excitation/frequency×excitation-linear
behavior. Log scale rejects non-finite or non-positive plotted coordinates, and
the selected scale is recorded in the export manifest. The guarded analysis and
notebook suite ran 45 tests with 7 optional skips; Matplotlib rendering checks
were skipped because that optional dependency is unavailable in this interpreter.

### SR830 TOML auto-alignment in all sweep entry points (offline complete; 2026-09-24)

All three standalone sweep commands and the four-module combination Lock-in
session now align XX/XY input mode, shield grounding, input coupling, time
constant, filter slope, Reserve, and SENS with the resolved local TOML before
raising excitation; XX frequency is set at 4 mVrms and verified on both roles.
Writes are limited to differing fixed settings, with per-field readback and
fixed-setting transition-latch audit. Expected OFLT-change status is consumed;
overload/unlock/error at that boundary rejects before higher excitation.
After a run, XX returns to 4 mVrms/h1 and the configured base frequency; both
roles retain TOML base SENS/RMOD and fixed settings, with final readback checked.
Reference roles, identity, physical XY SINE disconnect, and address are not
auto-written. The 129 offline fake-VISA SR830/combination tests pass, including
failed write/readback and integrated frequency cases; the expanded sweep,
combination, analysis, and notebook suite ran 222 tests with 8 optional skips.
No real hardware
connection or write was made in this change; on-station acceptance remains.

### SR830 excitation-frequency guard and analysis-axis switch (offline complete; 2026-09-24)

`sweep-excitation` now applies the configured XX `frequency_hz` at verified
minimum output before a point can raise the source level. Transition status and
XX/XY readbacks are saved in `frequency_setup`; a frequency mismatch rejects
before formal samples. Each point rechecks the requested frequency. The sweep
and combination notebooks now allow nominal converted current or actual SINE OUT
readback voltage as the excitation X axis. Current-based harmonic fits retain
per-run archived resistance; old records are never relabeled from requested
frequency. Focused guarded fake-resource, analysis, and notebook tests passed
(143 tests, 6 optional skips). The full guarded 652-test suite had one
unrelated report-plotting error because this local interpreter lacks
Matplotlib. Real instrument verification remains for the operator; no hardware
was connected.

### SR830 browser checkboxes and combined-axis exclusions (2026-09-24)

The commissioning notebook retains separate Frequency, Excitation, and f × e
categories, now collapsed until opened. Each row shows the file name, raw
formal-sample count, and XX/XY harmonic channels found in formal readings, with
a checkbox on the right. Archived run-condition summaries are hidden, and
multi-run paired-comparison settings are in a separate collapsed section.
Refresh preserves checked files still in the catalog.

Frequency/excitation exclusions are global requested-axis coordinates across all
selected records. Labels show only requested frequency or estimated excitation
current plus point index/indices, not source JSON names. f × e exclusions remove
requested frequency rows or excitation columns globally. Unequal point counts do
not block overlay plotting; paired differences use only coordinates common to the
baseline. The export manifest records coordinate exclusions.

New sweep run JSONs reference a content-addressed sibling
`measurement-profile-<sha256>.json` via `measurement_profile_ref`. Profiles
deduplicate stable resolved instrument/safety/resistance settings; the per-run
`run_configuration` retains scan-specific points/timing, and run JSON keeps only
per-run safety estimates. Hashes are verified during analysis; absent/modified
profiles fail closed. Legacy inline `measurement_config` remains supported, but
profile sidecars must accompany copied/moved runs.

Validation: 137 commissioning notebook, SR830, and profile/analysis tests passed
(6 optional skips), including sample/channel catalog labels, unequal point
counts, global exclusions, profile deduplication, legacy loading, hash checking,
collapsed sections, and notebook code compilation. No hardware I/O.

### Four-changing-axis bounded hardware validation passed (2026-09-24)

Operator confirmed no visible XY OVLD and authorized completion. Fresh four-sample
SR830 diagnosis was clear. Three runs passed: gate x excitation 4/4, reversed
excitation x gate 4/4, then T x ordered B x gate x excitation 24/24; all exit 0,
clean verified cleanup, no retry within this stage. Runtime/safety policy unchanged.
Full grid: T targets 2/2.1 K, Z 0/0.05/0 T, X zero, gate 0/0.01 V at 1-uA compliance,
XX 4/8 mVrms; XX/XY h1/h2, ranges 1 V/50 mV. Normal final actual/setpoint X/Z zero,
gate zero/OFF, both SR830 h1/4 mV, temperature held at target 2.1 K, actual 2.0958 K.
Important: stable-readback is not strict target equilibration. The 2.1-K target's
formal-window actual means spanned 2.02319-2.09571 K; use actual.temperature_k.
File-only monitoring, integrity/hash verification, exact ordered 24-point path,
32-row combined loading and excitation-indexed gate regrouping passed. All 64
module subsets/orders remain fake-tested, not all individually hardware-tested.
Fresh local guarded regression: 644/644, 126.899 s, no failures/errors/skips.
Prior target 644-test receipt retained; 113 deployed source hashes reverified.
No protection bypass; historical XY overload and 5-mT mismatch causes unresolved.
Details, raw-data locations, SHA-256 and scope limitations:
[MULTI_AXIS_ACCEPTANCE_20260924.md](MULTI_AXIS_ACCEPTANCE_20260924.md).

### Gate/excitation 2x2 acceptance blocked at preflight (2026-09-24)

Historical checkpoint; the later operator-confirmed validation above supersedes
the request to wait for panel review, but does not relabel the rejected run.

After approval to continue, prepared and offline-validated gate 0/0.01 V x XX
4/8 mVrms, fixed target T 2 K and X/Z zero; same 1 V/1 uA SMU ceilings, XX/XY
1 V/50 mV ranges and all existing stability/cleanup guards. No runtime edits.
Target's 113 source hashes still matched the tested snapshot. Vendor GUI absent;
existing Jupyter kernel idle with last activity before the earlier acceptance.
Run joint-gate-excitation-20260924-v1 stopped at 08:49:33 UTC, process exit 2:
XY preflight LIAS=1 (input/reserve overload), ERRS=0. Zero condition attempts,
zero samples, no setting/output/field/temperature writes. Preflight confirmed
SMU zero setpoint/output OFF, actual/setpoint X/Z zero, T 2.000799894 K/target 2 K.
Four subsequent status-consuming diagnostic snapshots at 3-s requested intervals
were clear on both SR830 units, still h1/4 mV and XX/XY 1 V/50 mV. This is not
proof the intermittent problem is resolved. No retry or auto-clear policy added;
recurrent overload requires operator panel/input-path review before another run.
Raw SQLite integrity and local/remote hash match, accepted-only loader gives
zero rows, scan process exited. Full receipt in JOINT_ACCEPTANCE_20260924.md.
The earlier 50-mT pass below remains valid only for its own bounded scope.

### Scoped cleanup recovery / 50-mT joint acceptance (2026-09-24; bounded real pass)

Operator reports manual zero and explicitly approves Z 0 -> 0.05 -> 0 T, X 0,
then confirms vendor GUI exited and permits field commands. This supersedes the
prior manual-recovery request, but does not independently prove current zero.
New fake-DLL regression reproduced the missing zero command after a 0.02-T trip.
Integrated cleanup now permits only zero/temperature-disable recovery readbacks
inside the universal 3-T envelope; original target/scan/hold limits remain intact.
Recovered scan-limit violations still reject the run, including first violations
during normal zero. Invalid reads/errors/disabled field control cannot certify zero;
scanning cannot resume after cleanup. Standalone readback policy is unchanged.
Local guarded regression: 644/644, zero failures/errors/skips, 121.119 s.
Six new regressions cover the real envelope failure, slow/stuck zero, independent
temperature shutdown, hard bounds/errors/nonfinite/communication/control failures,
normal hold and a normal-zero trip. Target lyr guarded regression also passed
644/644 in 143.182 s, zero failures/errors/skips, exit 0. The first target launch
was interrupted by PowerShell treating unittest stderr progress as a terminating
NativeCommandError; its incomplete receipt is retained, not counted as passing.
Fresh read-only preflight passed 10/10 samples with actual/setpoint X/Z zero.
The first retry (joint-z50mT-20260924-v2) rejected an XY input/reserve overload
before setting writes. Four subsequent status samples, 3 s apart, were clear;
one identical-plan retry (joint-z50mT-20260924-v3) completed with exit 0 and all
three conditions accepted, 07:08:13-07:15:27 UTC. Both attempts are retained.
All four modules participated, but only the ordered field axis varied; this is
not real validation of every module subset/order or a multi-axis changing grid.
Final verified state: actual/setpoint X/Z zero, gate setpoint zero/output OFF,
both Lock-ins h1/4 mV with clear statuses, T control holding 2 K (actual 1.9999 K).
Normal zero cleanup passed; out-of-scan-envelope recovery was tested with fakes,
not deliberately induced on hardware. Earlier 5-mT mismatch remains unresolved.
SQLite integrity and local/remote SHA-256 match; default analysis returns exactly
3 accepted rows for v3 and zero for v2. See JOINT_ACCEPTANCE_20260924.md.

Target isolated source: `C:/Users/LK_Setup/Yuanrong Li/Integration_recovery_20260924/source`.
Archive SHA-256: `1321dac57c11efc38f3b006647d72ba4f83dbb1ce098966f757293cfc31cb2df`;
all 113 tracked source/test/tool/template/Notebook files were hash-verified before
target tests. Runtime/test files match this local working revision; docs changed
after packaging. Exact lyr interpreter, source-first PYTHONPATH and user-site off.
No ignored hardware configs or DLLs were in the archive; the private plan was
transferred separately. Earlier source/experiments and failed raw data preserved.

Executed private plan: Z 0/0.05/0 T, max_step 0.05 T, timeout 7200 s, unchanged
1-mT tolerance/0.5-mT range/10-s dwell/1-s polling. Z/resultant scan guard 0.051 T
includes the existing 1-mT tolerance; X guard remains 0.02 T. Initial T target 2 K,
gate 0 V (1 V/1 uA ceilings), XX 4 mVrms, XX/XY ranges 1 V/50 mV unchanged.
The float32-aware return plan includes an internal 0.025-T waypoint, not another
formal measurement point. No ramp-rate API or guessed factor-of-ten adjustment.
The historical magnetic and current integration DLL files have identical SHA-256;
this does not explain the earlier actual/setpoint discrepancy. Retain that failure.

### Historical joint attempt stopped; field recovery then unverified (2026-09-24)

This checkpoint is retained as failure evidence; current recovery/acceptance
status is above. Its stop instructions preceded the later explicit authorization.

Supersedes the not-started/pending-operator statements in the preparation
checkpoint below. The operator confirmed exclusive instrument access and
explicitly requested wider-range apply-toml. XY 50 mV applied and verified;
XX remained 1 V full scale, both sources 4 mV, statuses clear. Retained actual
300 ms time constants with 5.1 s settling after a pre-write mismatch rejection.

Run joint-wide-20260924-v1 accepted one zero-field condition, then failed while
moving toward Z=0.01 T: the first 0.005-T waypoint was acknowledged, but actual
Z crossed 0.02 T. Electrical cleanup verified minima/zero-OFF. Magnetic AND
temperature cleanup were blocked by the audited reader's field-envelope check;
no zero command was issued. Independent read-only check at 06:22:45 UTC:
X=0, Z=0.0500000007 T, Z setpoint=0.0049999999 T, temperature target 2 K,
actual 1.7896 K, both controllers ON, error zero. This is not current or zero
confirmation. Operator manual recovery/panel verification was requested.
Do not run another sweep, assume zero, increase limits, or apply a guessed
factor-of-ten correction. Default analysis correctly excludes the failed run.
See JOINT_ACCEPTANCE_20260924.md for receipts, limits and unresolved blockers.

### Publication and scoped hardware authorization (2026-09-24)

The user requested publication to origin/codex/integration-four-module-scan and
authorized small joint hardware acceptance. The 112 runtime/test/tool/config/
Notebook/pyproject files still exactly match the final 638-test offline snapshot
below; the publication includes the previously approved apply-toml and repeatability
analysis changes. The September 23 no-commit statements below describe that older
checkpoint, not a requirement to leave this revision unpublished.
Publication-day guarded regression: 638 tests passed, zero failures/errors/skips,
111.757 s with local AI Python; real instrument imports/DLL loads blocked.

Published code commit: b3570c53361b3c8625ffd5bb5712b79135cd2e3c; origin was
verified at the same commit after push. A detached copy is prepared at
`C:/Users/LK_Setup/Yuanrong Li/Integration_joint_acceptance_20260924`.

Hardware acceptance has NOT started. The user subsequently confirmed a connected
sample, XY SINE OUT physically disconnected, and Z 0 -> 0.01 -> 0 T / X 0.
The sole SMU physically drives a GATE, not a sample electrode and not in parallel
with XX. Its authorized ceiling is now 1 V / 1 uA; use much smaller initial
points. XX source ceiling is 0.2 Vrms. Test temperature remains 2-3 K.
There is NO external excitation series resistor: the updated local TOML uses
0.01 ohm as an operator-supplied placeholder because the parser requires a positive
value. Its approximate and maximum device resistance are both 100 Gohm; this is
an estimate, not measured impedance or a current limiter. Never interpret the
legacy smu_bias channel current as sample transport current in this setup.

Current Integration local TOML still has SMU address/limit placeholders, a
+/-0.1 V SMU grid, excitation up to 0.45 V, and no combination_scan table; do not
execute it as the acceptance plan. The original remote checkout/Notebook edits
are preserved. A private smoke hardware.local.toml and unchanged lockin_safety
were prepared under the isolated worktree's run_data/joint_acceptance_20260924/smoke
and validated offline with both local AI and target lyr: one condition at 2 K,
gate 0 V, zero field, XX 4 mVrms, h1/h2; normal field zero and SMU zero/OFF.
Planned later grid (only after smoke passes): T 2/2.1 K, gate 0/0.01 V,
Z 0/0.01/0 T, XX 4/8 mVrms. Keep configured dwell/settling requirements.
XX 1 V full scale is allowed; XY 1 V is NOT in the project allowlist, so its
existing 20 mV is retained. This distinction was communicated to the user; no
safety-policy expansion was made to facilitate the test.

One Jupyter kernel remains open on LK_setup. Operator confirmation that no other
scan/monitor-live/controller is accessing the instruments is pending; an open
kernel alone is not proof of activity or inactivity. Do not kill that kernel or
start competing I/O. No instrument connection, status consumption, setting or
output write has occurred in this stage. Private settings/run data stay uncommitted.

### Integration I2b — shared four-module point coordinator (2026-09-23)

Implemented locally in `codex/integration-four-module-scan`: any nonempty subset
and ordering of temperature, magnetic, SMU and fixed-frequency SR830 excitation.
Temperature/magnetic share exactly one attoDRY session. Existing temperature dwell
and field point/float32/corner/ack/zero engines are reused; integrated effective
axis limits cap pure Z at 3 T as well as enforcing the resultant envelope.
Ordered magnetic duplicates and segment/direction identities remain literal.
Inner temperature resets are statically bounded; cooldown uses a bounded
transient ceiling but cannot accept a still-hot plateau above the formal ceiling.
Every condition requalifies temperature after all axis writes. Formal acquisition
has synchronous whole-sample and per-harmonic environment brackets, not continuous
background polling. Temperature coordinates use the existing time-weighted formal
window helper; field coordinates use the arithmetic window mean. Raw endpoints,
times, rejected/partial electrical reads and last confirmed state remain auditable.

All active preflights precede writes. `--authorize-combination` (old electrical flag
remains an alias) does NOT alone authorize environmental writes: selected T/B axes
also require `--authorize-cryostat`. Missing authorization fails before any I/O.
Global cleanup: electrical minima/zero-OFF, then field normal configured hold/zero
or failure monitored-zero, then T normal hold/failure disable, shared close once.
An earlier cleanup failure switches later axes to failure policy. No writes to
untouched environmental axes; a failed communication never implies zero field.
Combined interrupts abort/cleanup (no interactive thermal resume). Scan dwell,
not temperature_run.pre_measure_wait_s, governs temperature qualification.

17 new fake-DLL/VISA tests cover all 64 nonempty permutations, active/inactive
ownership, fresh brackets, temperature actual vs target, reset/cooldown, repeated
and descending field points, static/readback/float32 limits, failed writes,
communication uncertainty, audit failure, Ctrl+C, and partial connect/close.
One additional coordinator regression preserves the primary instrument error
when the subsequent environmental read AND its audit write fail (reproduced
before the fix). Secondary errors still require manual review and cannot skip
cleanup. Full final guarded local suite: **638 passed**, zero errors/failures/skips, 117.888 s,
AI Python 3.12.13; real pyvisa/qcodes/serial imports and DLL loading blocked.
An initial focused regression exposed the simulator's old expectation that later
cleanup still receives failed=false after earlier cleanup fails; that expectation
was updated to the new fail-closed policy and is covered by hardware-fake tests.
The initial revision also passed 637 tests on LK_setup lyr (146.872 s). Final
target-offline revision passed **638 tests**, zero errors/failures/skips,
145.473 s, exit code 0, using
`C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe` (3.12.13), user-site disabled,
snapshot src first and the same hardware-import/DLL guard. Final isolated source:
`C:/Users/LK_Setup/Yuanrong Li/Attodry_four_module_offline_9d729d4f_20260923/final-source`.
Sibling `final-offline.stdout.txt` and `final-offline.stderr.txt` retain the receipt.
Final source archive SHA-256:
`022e5e4fa8d5c0e7393ce62352948e7ef31b56c69cf218097f0cf2ab68da4898`
(732598 bytes, 139 files). Every archive file hash was verified before target
tests; no DLL/local hardware config/raw experiment data was packaged. All 112
runtime/test/tool/config/Notebook/pyproject files match the local tested revision;
only delivery documentation changed afterward. Earlier `source`/637-test logs
are retained separately. Source/test/tool compilation and normal Git diff check pass.

No real instrument connection/status consumption/write was performed. No commit,
push or overwrite of existing LK_setup experiments/configuration. This dirty-tree
revision is based on e85206c and preserves the previous apply-toml/analysis work.
Remaining: separately authorized small-grid joint hardware
commissioning and DC+AC circuit/total-sample-limit approval. Lock-in frequency axes,
software-pulse timing and hardware resume remain unsupported. Hold does not mean
persistent mode or long-term magnet safety certification.
See COMBINATION_SCAN_GUIDE.md for configuration, authorization and limitations.

### Earlier I2a electrical checkpoint (2026-09-23; target offline complete)

The selected integration worktree now has a real-driver electrical runner:
`combination_cli describe-hardware` validates offline; `run` requires explicit
combined connection/write/status authorization and physical XY SINE disconnection
confirmation. It supports SMU, fixed-frequency SR830 excitation, and both loop
orders, reusing existing expanded grids, role activation, limits and harmonic
selections. The shared coordinator still protects the simulation-only entry.
Every leaf/sample takes fresh active SMU reads and selected XX/XY h1/h2/h3 reads;
complete role-level reads survive companion failure. SQLite combination-v1,
file-only monitoring and the existing unified analysis work for hardware records
marked simulated=false. No standalone sweep/cleanup runs at each leaf.

ThreeSmuSession exposes begin/set/sample/cleanup points while preserving its
existing configuration, compliance, trip and zero-before-OFF engine. SR830 points
reuse the daily sensitivity/Reserve/autorange/harmonic safety helpers. Both
preflights precede configuration; input/filter/time-constant panel mismatches
are rejected before writes. Normal/exception cleanup restores XX 4 mV and h1/
original ranges/Reserve, then zeros/disables active SMUs and independently closes
all resources. Startup uncertainty, communication or cleanup/audit failures
cannot certify safety. Disk failure does not suppress remaining physical cleanup.
Non-finite SMU V/I now explicitly fails safety checks.

24 new electrical fake-resource tests pass. Full local guarded suite: 620 tests
passed, zero errors/failures/skips, 74.274 s with AI Python 3.12.13; real
pyvisa/qcodes/serial imports and Windows DLL loads blocked. Compilation and diff
check passed. The first full attempt exposed stale multi-select/report Notebook
test assumptions and a test discovery-path issue; these were fixed, and three
repeatability-statistic regressions were added. The full passing suite includes
the earlier uncommitted apply-toml and analysis work, which remain preserved.

The first LK_setup isolated snapshot passed 619 tests (89.854 s); the final
revision additionally makes every failed/interrupted hardware attempt require
manual review even if later cleanup reads succeed (VISA errors need not inherit
OSError). Its cleanup readbacks remain recorded, not erased or claimed unsafe
merely because review is required. Final target-offline passed all 620 tests in
88.395 s, zero failures/errors/skips, using exact
`C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe` (3.12.13), user-site disabled,
snapshot src first and real hardware imports/DLL loads blocked.
Final isolated source:
`C:/Users/LK_Setup/Yuanrong Li/Attodry_electrical_offline_9d729d4f_20260923/final-source`.
Sibling `final-offline.stdout.txt` and `final-offline.stderr.txt` retain the receipt.
Archive SHA-256:
`28cfe061a7b575a683921bef50ce6799f5cbe462707cb6a8c9029a82cdf63276`
(715514 bytes, 137 files). All archive file hashes were verified before tests;
no DLL/local hardware config/raw experiment data is packaged. The 109 local
runtime/test/tool/config/Notebook files match the target-tested snapshot.
Only delivery documentation changed afterward. This is an uncommitted working-
tree snapshot based on e85206c, not a new published Git commit.
No real instrument
was connected or written. No commit/push/main update or deployment over existing
LK_setup checkouts/configuration. T/B shared cryostat integration, environmental
formal-window bracketing, Lock-in frequency axes and real joint commissioning
remain incomplete. Hardware resume and software-pulse plans are rejected.
Independent DC and AC limit checks do not certify the superposed sample drive;
joint wiring/total-device-limit approval is still required before real testing.
See COMBINATION_SCAN_GUIDE.md for current commands and exact scope.

### Multi-run SR830 repeatability analysis (2026-09-23; verification pending)

The commissioning Notebook now supports multiple selected records per scan
type, an independently chosen baseline, and X/Y/R/phase comparison. It groups
repeats within each source file, overlays each run's mean ± within-run spread,
and plots paired differences only at shared requested coordinates. The match
keys are requested frequency, requested SINE OUT RMS voltage, or both for
frequency×excitation; plotted axes remain based on recorded readbacks. Circular
statistics/differences are used for phase. A side-by-side archived-settings
table helps flag condition changes, and a paired-coordinate summary reports
mean-absolute, RMS, and maximum differences. Excitation harmonic fits and
condensed reports are generated per run rather than pooling records. The
selection manifest records selected runs, baselines, metrics, repeatability
summaries, and per-run fits; raw records are unchanged. Offline Notebook
verification and tests remain pending; do not treat this stage as complete yet.

### TOML-driven SR830 settings apply CLI (2026-09-23)

Added `python -m attodry_control.lockin_test apply-toml --role lockin_xy|lockin_xx`
for explicitly applying one semantic role's TOML-defined fixed input/filter/
sensitivity/Reserve settings. The selected device alone receives ISRC, IGND,
ICPL, OFLT, OFSL, SENS, and RMOD writes; reference, frequency, harmonic, phase,
and SINE OUT are not changed. Writes require explicit operator flags, confirmed
physical XY SINE OUT disconnection, and consumed/read-back status. A pre-existing
input/Reserve overload is accepted only when the configured range is strictly
wider; a repeated post-write overload is rejected without restoring the
narrower range. Completed and rejected attempts are atomically retained in the
configured commissioning output directory. All 97 SR830 fake-resource tests
pass. This was offline-only: no real VISA resource was opened, no instrument
write was issued, and LK_setup was not accessed. See
`docs/LOCKIN_DAILY_OPERATION.md` for the operator command and constraints.

### Selected continuation branch and publication scope (2026-09-23)

The operator selected codex/integration-four-module-scan as the continuing
integration worktree and authorized committing/publishing it to origin.
Keep module/integration and its temperature-excitation-worktree: it contains
three uncommitted Notebook edits, checkpoints and the local documentation commit
2a9bdd4 (not pushed). Do not delete it merely because its runtime baseline is older.
Equivalent import/monitor guidance is included here without merging its dirty
Notebook work. Publishing this branch does not update main, deploy to LK_setup,
or complete the real four-module runner. Local configs and raw evidence stay ignored.
Pre-publication local regression: 588 tests passed in 65.706 s, zero errors,
failures or skips, with real pyvisa/qcodes/serial and DLL loads blocked.
The first run with user-site disabled lacked NumPy (2 errors, 16 skips); rerunning
with the existing user-site NumPy 2.4.6 passed without installing dependencies.

### Worktree import setup and monitoring boundary documentation (2026-09-23)

README and Lock-in operation/monitor guides now explicitly set process-local
PYTHONPATH to the selected worktree's resolved src directory, check interpreter
and module paths, and require repeating setup after a new terminal/worktree switch.
An empty PYTHONPATH does not add src. The shared lyr interpreter need not change.
Source inspection confirms lockin_test monitor-live performs sequential real VISA
diagnostics, not acquisition-memory reads; optional LIAS?/ERRS? consumes latches.
Do not run it alongside acquisition of the same devices, even without latch reads.
Use lockin_progress_monitor on the run's flushed JSONL during scans. Documentation
only; no hardware connection, remote deployment or runtime behavior change.
Verification: 98 SR830 fake-resource and file-progress tests passed (3.434 s),
with real pyvisa/qcodes/serial imports and DLL loading blocked. Initial sandbox
runs encountered temporary-directory PermissionError; the guarded rerun outside
that sandbox passed. Resolved-src import verification and CLI help checks are
offline only.

### V/I sensing and bounded low-temperature startup verified (2026-09-22)

The current operator authorization permits an unloaded temperature test in 2–3 K.
Use the separate local 2.0/2.2/2.4 K commissioning plan, not the existing target
computer's high-temperature grid. Do not infer authorization for a field movement.

Real bias-only verification on LK_setup passed five ordered points
0,+1,0,-1,0 mV with a 3 s settling delay, 0.01 V / 1 microampere limits, and
zero-then-OFF cleanup. Explicit concurrent VOLT/CURR sensing and returned element
order were confirmed; actual terminal-voltage readings are now distinguishable
from source setpoints. This does not calibrate accuracy or validate loaded trips,
current-source mode, four-wire wiring or the two inactive gates. Previous raw
records remain unchanged. Full target guarded offline suite: 585 tests passed.

Status-only cryostat checks at 09:28 UTC found sample 1.6796 K, stored target
300 K, temperature control OFF, measured/setpoint X/Z zero, field control OFF,
error zero. No temperature/field write occurred during those checks. XX/XY were
both at 4 mV; initial latched status was retained, followed by two clean pairs.
No SR830 setting was changed. This is not a field/persistent-mode certification.

Added set_temperature_and_enable: preload and confirm the requested target BEFORE
enabling control, then retain the established forced post-enable reapply when
starting from OFF. Unacknowledged or invalid preloads fail without enabling.
Temperature commissioning, daily single-target, scan and temperature/excitation
scan all use this path. 96 related fake-DLL/VISA tests passed, including stale
300 K startup at all four entry points. Target guarded full suite: 588 tests
passed, zero errors/failures/skips, 75.672 s.

Real temperature-only run 20260922T094241Z_lowT_2p0_2p4 completed all three
2.0/2.2/2.4 K targets and retained 603 temperature polls. The deliberately coarse
test criterion was target +/-0.2 K, 10 s window, peak-to-peak <=0.02 K; accepted
window means were 1.802786/2.003700/2.205586 K, NOT precise target equilibration.
The raw command receipt confirms 2 K preload, enable, 2 K reapply, 2.2 K, 2.4 K,
with no magnetic mutation. Normal finish holds the 2.4 K target/controller ON.
Independent 10:01:45 UTC read: actual 2.331900 K, target 2.4 K, cryostat error 0,
X/Z readback and setpoint zero, field control OFF. Software then disconnected;
this is not a continuous 3 K watchdog or a long-term hold qualification.

Independent electrical postcheck confirmed bias zero/OFF/1 microampere and both
SR830 at 4 mV. Its strict all-clean assertion FAILED on XY LIAS=4; that original
failure is retained. Three subsequent paired status checks at 10:03:34–40 UTC
were all LIAS=0/ERRS=0, without setting changes. The transient cause is unknown;
do not claim every postcheck was clean. See LOW_T_AND_VI_ACCEPTANCE_20260922.md.

The real arbitrary-order four-module station remains incomplete. These bounded
checks do not constitute a completed four-module combined experiment.

### LK_setup unloaded bias acceptance completed within tested scope (2026-09-22)

This supersedes the earlier specimen-connected, address-unknown and no-write
statements below. The operator explicitly confirmed NO SAMPLE connected,
identified the bias VISA role in ignored local configuration, and authorized
autonomous bounded scanning and acceptance. Only smu_bias was opened; gate,
Lock-in, temperature and magnetic hardware were not operated. Electrical
ceilings stayed |V| <= 0.01 V and |I| <= 1 microampere. Scan targets were bounded
at +/-0.009 V to leave margin inside the voltage ceiling.

The existing exact 1a87f03 source snapshot on LK_setup, using lyr Python 3.12.13,
completed zero/time trace (9 samples), 0/+1 mV/0 (9), bipolar forward/reverse
(13 points, 39), and literal duplicate/nonmonotonic points (5 points, 15).
All 72 formal samples were clean/completed/accepted. A controlled Python
KeyboardInterrupt after the third point at +2 mV retained three samples as
interrupted/not accepted; normal and interruption cleanup both confirmed
zero setpoint/output OFF without cleanup errors. This is not OS-kill/SSH-loss
or physical-disconnection fault commissioning.

Keithley 2400 identity/serial 4414633 and C34 firmware were confirmed. Hardware
current compliance was changed from 105 microamperes to 1 microampere and read
back before output enable. Three final independent status-only checks confirmed
0 V setpoint, output OFF, 1 microampere compliance, two-wire, and error 0.
Output-OFF V/I were correctly unavailable, not read or invented.
Initial preflight retained error 601 (Reading buffer data lost) and stopped with
zero settings writes; three subsequent clean OFF/zero checks preceded one retry.
The rejected preflight was not reclassified as accepted.

IMPORTANT measurement limitation: final :SENS:FUNC? returned CURR:DC only,
matching QCoDeS mode setup. The archived voltage field is NOT certified as an
independently sensed terminal voltage. This acceptance proves control, current
readout and data/cleanup paths, not terminal-voltage accuracy, loaded compliance
trip response, wiring/guard suitability for a specimen, or real four-module
integration. Review and test explicit voltage/current sensing before relying
on that voltage field as an independent overvoltage measurement.

The live HTTP stream contained 39 samples identical to the bipolar raw events;
72 rows loaded through standalone and unified legacy adapters, while all three
interrupted rows were excluded by default and available through audit opt-in.
Read-only verification retained point order, duplicate identities, forward/reverse
segments, source hashes and raw files; the inspected diagnostic plot uses source
setpoint vs measured current, no fits or averaging.

Target lyr lacked QCoDeS: installed 0.59.0 plus missing dependencies after dry-run
review; existing NumPy/PyVISA/Matplotlib were not replaced. Full hardware-guarded
offline suite then passed 580 tests, zero failures/errors/skips in 68.048 s.
Runtime src remained byte-identical to archive 1a87f03; only ignored one-off
configs/harness/logs and local documentation were added. No main update/push.
Details and canonical record identifiers: BIAS_SMU_ACCEPTANCE_20260922.md.

### LK_setup combination snapshot target-offline check (2026-09-22)

Operator confirmed LK_setup, requested continued work under the existing
Yuanrong Li directory, authorized SSH, and stated that a real specimen is wired.
The operator subsequently confirmed only smu_bias is active, with absolute
ceilings 0.01 V and 1 microampere; neither number is a requested formal target.
gate_top/gate_bottom must not be connected, queried or controlled by this run.
Their physical output state is unknown, not inferred off. Source mode and wiring
still need preflight/operator confirmation. No hardware-write permission was
inferred from the confirmed limits.

Follow-up file-only inspection found no usable bias VISA mapping in any of the
three target configurations: the main table is absent and the other two still
use CHANGE_ME_BIAS_SMU_VISA_ADDRESS. Do not identify bias by bus order or silently
borrow another SMU's address. An incomplete bias-only local draft records the
limits and off gates; its missing address must fail readiness before any device
open. Its voltage/two-wire defaults and zero fixed point are preparation only,
not operator confirmation of wiring or permission to enable output.

SSH filesystem/environment inspection found the existing main checkout at
b9e50f7 and temperature-excitation checkout at d87ecb1 with local tracked changes;
the magnetic checkout was clean at 75c5d63. None contained combination_cli.
Existing SMU tables still contained CHANGE_ME placeholders. These checkouts,
their local safety files and hardware.local.toml were not edited. A vendor
attoDRY interface process was present during inspection and was not stopped.

Created only a new isolated target-offline snapshot under the operator's
Yuanrong Li directory: Attodry_combination_offline_1a87f03_20260922/source.
Source is a Git archive of exact commit 1a87f03, not a remote Git checkout.
Archive SHA-256: E60920559E1C4F1B0732EB7F1C59A4145FA83DC5FD41F928D553187DB077DE18.
The source package excludes vendor binaries, ignored hardware configuration and
experimental data; existing target directories were neither pulled nor reset.

Exact target lyr Python 3.12.13 / 64-bit imported this snapshot's src.
Matplotlib 3.10.9, NumPy 2.5.2 and ipywidgets 8.1.9 were already installed.
All 580 offline tests passed in 73.197 s, zero skips/errors/failures, with
user-site disabled and explicit guards rejecting real pyvisa/qcodes/serial imports,
ctypes.WinDLL and windll.LoadLibrary. The snapshot contains no vendor directory.
Test terminal prompts and electrical values were fake fixture output only.
No real DLL was loaded; no instrument identity/status/measurement was queried;
no output, excitation, temperature or field command was sent.

This verifies the existing simulation/data/analysis checkpoint on LK_setup,
not a newly implemented or commissioned real combination runner. Real station
adapter integration remains pending as described below. No push or main update.

### Combination record/analysis and simulation checkpoint (2026-09-14)

Built on four-module merge commit 3913406. New `combination_scan` / `combination_store`
provide a simulation-only arbitrary-subset/order coordinator and additive v1 SQLite
WAL tables; `combination_cli` exposes describe/simulate/file-only monitor.
`combination_analysis` and its read-only Notebook provide actual-coordinate plotting,
requested-condition grouping, explicit audit/exclusions and legacy Three-SMU /
temperature-excitation adapters without modifying their original schemas.

Every leaf takes new active-module reads; SMU-outer/excitation-inner regression
produces six fresh SMU reads and two three-point I--V groups. Full ordered magnetic
point identities, repeated endpoints, segments and direction remain intact.
The simulator has h1-only Lock-in responses and does not validate real stability,
hardware cleanup or physical trajectories. It deliberately has no real backend.

Verification: 580 full offline tests passed without skips (63.299 s), including
26 new tests covering all 64 nonempty subsets/orders with multi-point T/B/SMU/
Lock-in axes, failures, Ctrl+C, conservative resume, storage, legacy adapters,
read-only monitoring and Notebook refresh/load/exclusions/missing channels.
After final plotting-label/open-marker refinement, both focused plot/Notebook
tests passed (1.737 s). Source/test compileall and diff check passed. The synthetic
six-point demo, selected CSV/manifest and visually inspected PNG/PDF are under
ignored `run_data/combination_demo_20260914/`; these are not experimental data.
Test-created empty temporary directories were removed; no user data was deleted.

Still incomplete: a real single-owner station joining the existing point/sample
controllers, one shared attoDRY session, live temperature bracketing and field
audits, full hardware-local configuration/identity provenance, verified all-role
global cleanup, and fake-DLL/fake-VISA combined transcripts. Magnetic resume is
blocked rather than guessing a history; killed active runs require recovery.
See `docs/COMBINATION_SCAN_GUIDE.md` for exact boundaries and next work.
No hardware connection, remote commands, main update, push or LK_setup sync.

### Four-module merge checkpoint (2026-09-14)

The isolated local branch `codex/integration-four-module-scan` combines
`module/integration` at be6071f, `module/three-smu-direct-points` at 02f04ec,
and `codex/magnetic-field-m0-m2` at 7d4417f. Existing main/integration worktree
changes were left untouched. Shared configuration loaders retain both magnetic
and temperature-excitation tables without requiring inactive modules.
The merged baseline passed all 554 offline tests, zero skips, using the local
AI Python 3.12.13 and Matplotlib 3.10.9 with its existing NumPy dependency.
The temperature test fixture now edits the active grid table; local test-package
imports no longer resolve an unrelated installed tests package.
No real instruments, DLL or VISA resources were opened. No main merge, push or
LK_setup synchronization was performed. This does not commission a four-module run.

### Magnetic M3--M5 passed in the tested small-field scope (2026-09-14)

Supersedes older X-only/uncommissioned descriptions below. Clean runtime 75c5d63
and the exact target lyr interpreter completed the operator-authorized X and Z
nine-point bipolar scans, an initial Z +0.1 T single target and the 0.05 T X/Z
discrete circle. Each had a fresh ten-sample zero/error-free read-only preflight.
Both bipolar scans used 0,-0.05,-0.10,-0.05,0,+0.05,+0.10,+0.05,0 T with the
other axis setpoint zero. The circle used 13 targets at 0..360 degrees in 30-degree
increments, Bx=0.05*sin(theta), Bz=0.05*cos(theta), from +Z toward +X, endpoint repeated.
Parameters stayed direct, max_step 0.05 T, tolerance 1 mT, range 0.5 mT, dwell 10 s,
poll 1 s, timeout 7200 s; normal/exception cleanup both monitored zero.

All four runs returned exit 0, all points completed, verified zero and normal
disconnect. Final circle receipt: 13/13 points, 26 successful command pairs,
audit_complete=true, manual_verification_required=false, actual/setpoint Bx=Bz=0,
error 0, field control ON. DLL disconnect does not disable zero-field control.
The circle continued after chat interruption; its original process returned normally.
The final file-only monitor independently verified all 1064 events, no integrity
errors and safe terminal flags; no scan process remained. The documentation delivery
passed 54 magnetic CLI/monitor plus 10 segment tests and diff checking, without runtime changes.
This proves ordered field control, not specimen hysteresis or continuous constant-radius
rotation. High fields, physical sample-axis calibration, real fault injection and
integrated transport acquisition remain outside this acceptance.

The ignored target TOML retains the circle plan. Backups, patches and preflight/
stdout/stderr are in `run_data/magnetic_field_commissioning/xz_extension_20260914T070500Z/`.
Canonical filenames, hashes and validation details are in DEVELOPMENT_STAGES.
No runtime, APS100 rate/protection, wiring, temperature or other-instrument setting
was changed. Raw logs/local configuration remain uncommitted. The user requested
closure and push of the magnetic work only; do not start another module or experiment.

### M4 and M5 passed within X 0--0.1 T scope (2026-09-13)

Supersedes the paused/rejected status below. Operator reconfirmed Magnet
temperature 4 K and permission to test, then closed the detected vendor GUI.
After verifying its exit and another ten-sample zero/error-free read-only
preflight, M4 ran from 11:45:44 to 11:51:09 UTC (325.494 s) on clean target 75c5d63
using the unchanged approved M4 config and exact lyr interpreter.

X +0.1 T / Z 0, direct, internal 0.05 T steps and the approved stability criteria
passed; accepted actual X was +0.1001999974 T, Z 0. Monitored zero then passed with
actual/setpoint Bx=Bz=0, error 0, field control ON. DLL disconnected normally;
the program did NOT disable zero-field control. Four command attempt/result pairs
all succeeded: enable field control, X 0.05 T, X 0.1 T, sweep-to-zero. No Z setting,
temperature/APS100-parameter change, fault reset, reboot or cabling operation.

Canonical target ignored log:
`20260913T114544Z_m4_x_0p1T_single-target_d689e43a.jsonl` in the commissioning
directory. File-only monitor verified 357 events, completed, zero_verified=true,
disconnected=true, manual_verification_required=false, audit complete and no
integrity errors. This proves the automated checks for this run, not sample-axis
calibration, high-field/Z commissioning or continuous-path behavior. Operator
subsequently confirmed panel zero/no alarms and explicitly approved M5.

M5 then completed the exact X points 0,+0.05,+0.10,+0.05,0 T / Z setpoint 0, direct,
same step/stability criteria and normal monitored-zero completion. Two configured
segments expanded to exactly five formal points (three ascending, two descending).
After another ten-sample read-only preflight, the real scan ran 11:57:54--12:06:05 UTC
(490.955 s), exit 0. File-only monitor validated 568 events, 5/5 points, seven
successful command pairs and no integrity errors. Zero/disconnect verified; no
manual-verification fault flag. Final actual/setpoint Bx=Bz=0, control ON, error 0.
No run process remained. Canonical target ignored log:
`20260913T115754Z_m5_x_0p1T_roundtrip_scan_dc52d781.jsonl`.

The target ignored hardware.local.toml now retains the approved M5 segments and
normal_end_field_policy="zero" (previously hold); its SHA-256 is
`87b3e4b783ec6b76a172a693e2fd7fa56e22e51de53ef7014dd39b723897c502`.
The previous M4 config, patch and preflight/scan stdout/stderr are preserved in
target ignored commissioning subdirectory `m5_x_roundtrip_20260913T115400Z`.
No driver, APS100 parameter or other-instrument setting was changed. Both stages
passed only for this small positive-X scope: Z motion, dual-axis motion, negative
fields, larger fields and integrated transport measurements remain uncommissioned.
Raw success and prior rejection logs remain uncommitted.
Detailed provenance/hash/commands are in DEVELOPMENT_STAGES. Documentation changes
are local only; source HEAD on local/origin/target remains 75c5d63.

### M4/M5 retry request - fresh read-only complete, writes paused (2026-09-13)

Operator requested M4/M5 again. Target source remains clean 75c5d63 and the local
M4 config hash remains 88c8f57139d9c1216934b421764c479532e75b041274621b8cf8085a515239a3.
No matching vendor/controller process was found. Ten fresh read-only samples
passed: Bx/Bz and setpoints zero, control OFF, error 0, clean disconnect. Last
sample was approximately 122.09 K Sample / 119.66 K VTI; neither is magnet
temperature. This turn sent no setting command and did not retry single-target.
Evidence remains in the target ignored commissioning subdirectory
`m4_retry_20260913T113700Z/preflight_readonly.json` and accompanying stderr.

Await current on-site Magnet temperature, no-fault/readiness/client-disconnected
confirmation and explanation of how error 35 was addressed; the old 4 K report
does not establish current readiness. Error 0 while read-only does not prove
that the prior control-enable error is resolved. M4 parameters remain the prior
X +0.1 T / Z 0 plan; no M4/M5 pass is claimed. Proposed M5 X points 0, +0.05,
+0.10, +0.05, 0 T (Z zero), direct and monitored-zero completion were sent for
operator approval, not written into config or executed. Recheck state before writes.

### Latest real M4 attempt - rejected, error 35 (2026-09-11 20:12 UTC)

This entry supersedes older M4-unauthorized/unexecuted and unchanged-local-config
statements below. The operator authorized X=+0.1 T, Z=0, direct, max_step=0.05 T,
1 mT tolerance, 0.5 mT range, 10 s dwell, 1 s polling, 7200 s timeout and monitored
zero; reported Magnet temperature approximately 4 K and explicitly requested an
attempt despite the unresolved front-panel Remote indication. Prior cable/interlock
concerns were operator-confirmed resolved. No M5 scan was authorized/executed here.

Target exact `lyr` Python ran clean source `75c5d630e46e7593081a0ee722fc3e1fbd93d22d`.
The ignored local TOML was backed up and prepared for this M4 earlier; approved
configuration SHA-256 is `88c8f57139d9c1216934b421764c479532e75b041274621b8cf8085a515239a3`.
A fresh ten-sample read-only preflight passed with zero field/setpoint, error 0
and clean disconnect. No matching vendor/controller process was found before M4.
`single-target` then issued exactly one field-control toggle: DLL return 0, but
device acknowledgement failed with error 35 after approximately one second.
No component setpoint or sweep-to-zero command was sent. Cleanup entered its
zero path but refused further writes while error 35 remained. Final last-confirmed
Bx/Bz and setpoints were zero, control OFF, error 35; this is NOT verified zero.
Process exited, DLL disconnected; manual verification is required. Do not retry,
clear faults, restart equipment or proceed to M5 without resolving the cause.

Canonical record (target ignored `run_data/magnetic_field_commissioning/`):
`20260911T201241Z_m4_x_0p1T_single-target_06667b45.jsonl`.
File-only monitor: 15 events, outcome rejected, audit complete, no integrity errors,
zero_verified=false, disconnected=true, manual_verification_required=true.
Manufacturer attoDRY2100 V2.1 manual p.34 defines 35 as no connected magnet
controller for the requested operation; 36 is the separate Remote-mode error.
Investigate cryostat-to-APS100 communication/initialization with the operator;
the root cause and current physical field still need on-site confirmation.
No APS100 parameters, wiring, source code or firmware were changed by this attempt.
Local documentation is updated only; local/origin/target HEAD remains 75c5d63.

### Latest magnetic segment configuration update (2026-09-11)

Supersedes earlier target-pending and cable/interlock-blocked statements: the
operator selected M4 X=+0.1 T/Z=0 T and confirmed the cable/interlock issue resolved,
allowing M4/M5 testing in principle. This change performs no hardware operation;
M4/M5 are not commissioned. Timing/transition parameters and the exact M5 point
list still need confirmation, with M4 evidence preceding M5 and a fresh read-only
check before writes. Target-offline passed as recorded below. No reboot or cabling
action was taken.

`magnetic_field_run` accepts exactly one of explicit `points` or `axis`+`segments`.
Single-axis linear segments use min<max and exactly one of inclusive points>=2
or positive step that divides the span; direction is ascending (default) or
descending. Input segment order and shared endpoints are retained, with at most
10000 expanded segment points. Axis x/z explicitly sets the other target to zero.
Existing limit, waypoint, acknowledgement, stability and cleanup checks are reused.
No angle generator, continuous-path assertion, ramp-rate change or Lock-in readout.
`magnetic_field_cli describe` expands the same config offline without DLL loading
or run-data creation; it identifies its assumed zero start and need for live revalidation.
JSONL v1 adds optional `segment_plan` plus per-point segment/direction metadata;
the file monitor regenerates and cross-checks it, while old explicit-point records
remain compatible. Copyable M4, M5 ascending, M5 hysteresis and pure-Z syntax-only
examples are commented in `config/hardware.example.toml`; active default stays zero.
Implementation `42f9799e4142110265d4d611a8f937c96eb94a67` is committed/pushed to
origin/codex/magnetic-field-m0-m2 and fast-forward synchronized via authorized SSH
to `C:/Users/LK_Setup/Yuanrong Li/Attocube_control-magnetic`. At that exact commit,
target `lyr` Python 3.12.13 / 64-bit imported this checkout's src, passed compileall,
223 focused tests (27.459 s), all 491 full tests (44.477 s, no skips), file-monitor
driver-import isolation and offline describe of the unchanged local hardware TOML.
The checkout was clean and the local TOML hash unchanged after validation. Vendor
files exist in this checkout; no real DLL was loaded and all test hardware was fake.
The local TOML still has one zero target, via_zero and normal scan-end hold;
it has not been changed to the selected +0.1 T M4 target or an M5 segment plan.
Confirmed existing timing values are recorded in DEVELOPMENT_STAGES. M4 parameter
approval and absence of competing GUI/controller clients still need confirmation.
This evidence is target-offline only, not M4/M5 commissioning or current zero proof.

### Latest magnetic-field safety update (2026-09-11)

This entry supersedes older universal-3-T and M3-pending statements below.
The operator approved pure X +/-3 T, pure Z +/-9 T, and resultant <=3 T only
when BOTH components are nonzero. Exact zero is required for single-axis; no
readback/stability tolerance changes the envelope. `experiment_vector_max_t`
now means dual-axis resultant only. Reduced configured axis limits remain binding;
configuration cannot raise the factory X 3 T / Z 9 T ceilings.
Requests, float32 endpoints, executed corners and monitored readbacks share this
rule. Each changed component is additionally checked against the other coil's
latest actual readback before writing. Unsafe `direct` intermediate waypoints
are rejected without implicit `via_zero` substitution.

New canonical JSONL records declare `field_limit_policy` as
`single-axis-hardware_combined-vector-v1` with resolved limits. The file monitor
checks the chosen safe corner; the unchosen candidate is diagnostic. Historical
undeclared records retain old 3 T semantics. No raw records are rewritten, and
the outer schema remains v1 with an explicit policy discriminator.

The operator supplied successful ten-sample M3 output and confirmed M3 success:
writes disabled, all errors zero, zero field/setpoint readbacks, normal disconnect.
Exact imported source/target revision was not captured in the pasted output;
this is operator-confirmed evidence for that run, not target-offline evidence
for the new policy. The operator selected first M4 target Bx=+0.1 T, Bz=0 T
for configuration preparation only; step, timing, transition policy and
connection/write authorization remain pending. Revalidate the changed revision offline on LK_setup
and recheck read-only state before writes. No hardware was connected or written
for this change. Local test evidence is in the current development-stage update.

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
DLL ABI preflight and real read-only connection validation are complete. The
standalone magnetic-field M0--M2 implementation, local fake-DLL evidence, and
the historical DLL-free `LK_setup` target-offline validation are complete. A later
offline-only extension adds explicit direct/via-zero transition policy and exact
field-command transcript requirements; the historical target snapshot applies only
to its pinned revision. Magnetic-field M3--M5 remain uncommissioned and separately
gated.

Completed offline in Stage 4:

- Added 64-bit/path checks and explicit ctypes signatures for every used symbol.
- Added separately authorized begin/connect/initialization polling and timeout.
- Every DLL call checks its return code; read failures preserve the prior
  `last_confirmed_state` rather than inferring a new field value.
- Added full temperature/VTI/X/Z/setpoint/control/error state reads and
  read-before-toggle idempotent control operations.
- Added explicit `direct`/`via_zero` setpoint planning, rolling stable waits, and
  monitored vendor sweep-to-zero behavior against a fake DLL. `direct` segments
  adjacent vectors while `via_zero` is the explicit conservative detour; there is
  no transparent zero insertion. Discrete requested/read-back setpoints do not
  establish the continuous physical trajectory, constant angle, constant magnitude,
  or physical ramp rate between setpoints.
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

Current standalone magnetic-field module update (2026-09-06; offline contract
extension, no real DLL/hardware):

- Completed the local implementation and fake-DLL evidence needed across M0--M2:
  a module-specific strict `[magnetic_field_run]` loader, pure setpoint planning,
  standalone executor/CLI, canonical durable JSONL audit, and file-only monitor.
  M2 target-offline is now also complete for the exact implementation revision
  described below. M3 real read-only, M4 smallest single-axis movement, and M5
  ordered X/Z scan remain separately gated and uncommissioned.
- The explicit nonempty `points` array is executed exactly as written: order and
  duplicate entries are preserved, with no sorting, deduplication, or Cartesian
  expansion. `single-target` requires exactly one entry plus separate connection
  and field-write authorization. `scan` also requires its own ordered-scan
  authorization covering every listed entry.
- `transition_policy` is required and explicit: `direct` segments each adjacent
  vector transition into validated discrete steps; `via_zero` requests the
  conservative zero detour. Ordered points and duplicates remain literal, and no
  policy transparently becomes the other. Every target, float32 waypoint, and both
  possible X→Z / Z→X mixed corners is prevalidated against component limits and
  `sqrt(Bx^2 + Bz^2) <= 3 T`. The driver dynamically selects only a verified axis
  order from the latest confirmed setpoint, then records the full planned and
  executed waypoint/path. `max_step_t` must exceed the 1e-5 T acknowledgement
  resolution. Actual field must be stable at the starting setpoint and each
  internal waypoint before the next setting write; each explicit target and cleanup
  zero also has stability evidence. Continuous motion between stable waypoints
  remains unobserved, so no physical-path, constant-angle, constant-magnitude,
  straight-line, or ramp-rate behavior has been measured or claimed.
- Field-control enable is read-before-toggle with bounded full-state
  acknowledgement, and initialization/control states are strict 0/1. Before an
  OFF→ON takeover, actual field must match the latent setpoint inside the configured
  tolerance (at most 1 mT), and both possible mixed corners must satisfy 3 T;
  otherwise no toggle is sent. Each changed X or Z setting receives a bounded
  complete setpoint readback. Stability monitoring validates actual and setpoint
  fields, control and error state on every sample; control loss resets the dwell.
  The first post-toggle acknowledgement uses measured elapsed time and enforces the
  timeout. A retained sample immediately before a jittered dwell cutoff still
  participates in tolerance and rolling-range qualification.
- A normal single-target run always executes monitored zero. An ordered scan uses
  the configured normal `hold` or `zero` policy; every exception/`Ctrl+C` path
  requires best-effort monitored zero before close. Zero is verified only after
  zero setpoint acknowledgement, actual-field stability inside the configured
  tolerance, enabled field control, and clear error state. `isZeroingField`, vendor
  action/error messages, and vendor logs may be recorded as optional diagnostics but
  cannot prove zero. Normal hold rechecks actual field against the final target as
  well as its setpoint/control/error state, including the magnitude criterion for an
  exact zero target. If normal zero fails or is interrupted, cleanup independently
  retries monitored zero. Communication uncertainty remains unverified even if a
  later cleanup read appears zero, and any uncertain zero/connection/close/audit
  state, non-finite readback, or missing last state requires manual attoDRY/APS100
  verification with the last confirmed state retained. Cleanup does not disable
  field control: it disconnects with control confirmed enabled at zero, or at the
  final target for a normal `hold` scan.
- One JSONL is the canonical run record. Its start event includes config hash,
  source provenance, interface/config, authorization scope, full stability and
  acknowledgement protocol, limits, points, transition policy, cleanup policy, and
  a required exact-field-command audit descriptor. Every field-control toggle,
  changed X/Z component, and sweep-to-zero command writes an attempt event before
  the DLL call and a result after the return/readback acknowledgement, with command
  index, context, DLL return code, and IEEE-754 binary32 component bits. Audit
  failure blocks normal writes but preserves best-effort zero cleanup evidence.
  Every event is appended, flushed, and fsynced; partial/rejected/interrupted/
  transition/stability/cleanup/disconnect evidence remains together. An uncertain
  append is rolled back best-effort, so a failed terminal `fsync` cannot leave a
  certified completion. The separate file-only monitor validates schema/run/index/
  point/terminal and command-transcript integrity and reports a torn, inconsistent,
  or no-terminal stream as incomplete and requiring manual verification, never as
  proof that the producer is alive. It rejects contradictory completed records when
  required zero is not verified or the final confirmed state is missing.
- Final focused safety/stability/config/attoDRY/magnetic/monitor verification passed
  all 182 tests in 6.533 s. The full suite passed all 450 tests in 14.373 s with 5
  optional-matplotlib skips; `python -m compileall -q src tests`, both magnetic CLI
  `--help` commands, and `git diff --check` passed (diff check emitted only CRLF
  warnings). No real vendor DLL was loaded, no `begin/connect` occurred, and no
  real toggle, setpoint, sweep-to-zero, or other hardware command was sent.
- Completed M2 on `LK_setup` against implementation commit
  `e0924f1666b8e1b0b8e6e0c08daad2ab9f9ac4c4` (short `e0924f1`). The exact
  `attodry_m2_e0924f1.zip` archive is 482705 bytes with SHA-256
  `231F649FA8A77B6139F67239F4E322275AA06B0BA3B4F0E628DEAFBFD32569F1`.
  It was copied to `C:\Users\LK_Setup\attodry_m2_e0924f1.zip`; the target snapshot
  was `C:\Users\LK_Setup\attodry_m2_e0924f1`.
- The target used Conda `lyr` with exact interpreter
  `C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe`, Python 3.12.13, 64-bit.
  The source archive explicitly excluded `vendor/`; the snapshot verified
  `vendor/` absent and recursively 0 DLLs. A dedicated file-monitor import-isolation
  check imported the monitor from that snapshot without importing the attoDRY
  driver. Compileall and both magnetic CLI
  `--help` checks passed.
- The target focused safety/stability/config/attoDRY/magnetic/monitor command ran
  182 tests in 4.675 s, OK. Full unittest discovery ran 450 tests in 20.299 s, OK,
  with no skips reported. The target-validation shell did not invoke a
  hardware-execution CLI or supply authorization flags; authorization-path unit
  tests used injected fakes. No DLL was loaded, and no hardware was connected or
  operated.
  This evidence completes M2 only and grants no M3--M5 authorization.
- The later direct/via-zero and exact-command-audit extension is local fake-DLL
  work only. It preserves literal ordered/duplicate points, validates float32
  endpoints, adjacent steps, and both mixed corners, and fails closed if a safe
  execution order or durable audit evidence cannot be established. It did not load
  a real DLL, call `begin/connect`, or issue a hardware command. The `e0924f1`
  target snapshot remains historical evidence for that commit only; a later revision
  needs its own DLL-free target-offline validation before making a new M2 target
  claim.
- The tracked example deliberately contains only a zero target. Real COM/DLL paths,
  real targets, and run data remain ignored/local. M3 is the next defined stage,
  but it remains unexecuted and requires new connection authorization; M4 and M5
  remain separately gated.
- M3 remains unexecuted. Its exact current-revision write-disabled command is
  `python -m attodry_control.attodry_test --config config/hardware.local.toml
  --samples 10 --interval-s 1 --authorize-connection`. It requires a new connection
  authorization, has no write authorization, and must run without a competing GUI/
  controller client.
- The offline JSONL/monitor now require exact float32 toggle/component command-
  attempt/result evidence, so that former audit-contract gap is closed only in
  offline code. M4 is still not ready to operate hardware: it first needs the
  separately authorized M3 read-only record, a user-selected smallest target,
  fresh connection/write authorization, a real canonical record, and no competing
  vendor GUI/controller client.

Current boundary: all hardware-free work through Stage 7, the standalone
magnetic-field M0--M2 local implementation/fake evidence and target-offline
validation, integrated dual-SR830 harmonic validation, the independent Three-SMU
QCoDeS S0 module plus query-only monitor, and the earlier generic attoDRY read-only
connection are complete. The magnetic M3--M5 stages remain gated. The first
attoDRY temperature setpoint/control
actions, control-first ordering, actual sensor recording, and heater-driven warming
are operator-accepted for this experiment.
The 1.75 K and 1.8 K runs did not meet the former strict stability criterion; that
fact remains diagnostic rather than being rewritten as stability. The commissioned
0.2 K overshoot guard gives a 2.0 K live abort line for a 1.8 K target. Daily
temperature operation uses the unified hardware TOML and dedicated command without
additional authorization flags. Three-SMU target-offline validation, bounded
bottom-only monitoring, and one authorized minimum bottom-gate write scan are
complete; remaining-role queries/writes, integration of the independent SMU module
into the main acquisition, other attoDRY setting writes, and real end-to-end
acquisition still require staged authorization.

Three-SMU direct-points follow-up (2026-08-26): the unified hardware TOML is now
the only Three-SMU configuration entry. Each role keeps only independent
`max_abs_voltage_v` and `max_abs_current_a`; user-entered compliance, source
min/max, ramp, readback tolerance, leakage and per-device settle fields were
removed. Keithley compliance is still programmed as a hardware protection value,
derived from the opposite absolute limit and queried with source/measurement
ranges after configuration. Both autoranges are required and `nplc=1.0` records
the Finland 50 Hz/20 ms default.

Scan roles now accept either an arbitrary `points` vector or `start/stop/step`;
`bidirectional` is per role. Paired-gate and map modes independently expand each
role, while software pulse rejects bidirectional. Formal targets and cleanup use
one direct write, shared `delay_s`, then recorded readback; no software ramp or
tolerance rejection remains. Schema v4 drops `near_compliance` while retaining
requested target and actual source/V/I. The two split example TOMLs and hidden
legacy CLI arguments were deleted. This work used only fake adapters and offline
tests: 104 focused tests and the full 389-test suite passed (five optional plotting
tests skipped), and source/test compilation passed. No real VISA resource, query,
status consumption, or setting write occurred. The ignored local TOML in this
checkout was schema-migrated without changing existing Lock-in values; all unknown
SMU addresses/timeouts/absolute limits remain `CHANGE_ME` and must be operator-filled.

Three-SMU active-role follow-up (2026-08-26): `[three_smu_run.<role>].role`
is the sole activation source. The loader requires a flat same-name hardware
table only for `fixed`/`sweep` roles and ignores absent or stale hardware
configuration for `off` roles. Sessions and the live monitor construct, query,
write, clean up, and record only that active subset; an off instrument remains
physically unknown, never assumed zero or output-off. Bias/top/bottom now share
one flat hardware-table schema, `[gate_*.smu]` and per-role `timeout_ms` are
rejected, and the Keithley adapter plus query monitor use a fixed 5000 ms timeout.
Schema v5 adds `active_roles`/`off_roles`, filters the hardware snapshot, and
leaves stable CSV columns blank for off roles. A bottom-only fake run proves that
bias/top factories and resources are never touched. Focused regression: 109
tests pass. The complete hardware-free suite passed all 394 tests with five
optional matplotlib tests skipped, and `compileall` passed for `src` and `tests`.
No real instrument library/resource was opened and no real query or write ran.

Three-SMU off-role scan-value follow-up (2026-08-31): an off run-plan role now
ignores the values of recognized dormant scan fields (`bidirectional`, `fixed`,
`points`, `start`, `stop`, and `step`) and normalizes its internal channel plan
to off/false/empty. This lets an operator temporarily switch a role off without
deleting its saved vector. Unknown/misspelled field names remain errors, and
switching back to fixed/sweep restores strict type, completeness, exclusivity,
and numeric validation. The tracked hardware example now documents ordered and
arbitrary explicit vectors, range expansion, descending direction, and
bidirectional behavior. No record schema or hardware path changed. The 42
focused config/CLI/fake-session tests and the complete 396-test offline suite
passed with five optional matplotlib skips; `compileall` passed. No real VISA
resource was opened and no query, status consumption, or setting write occurred.

Three-SMU ordered-range follow-up (2026-08-31): active sweep roles now require
exactly one of an explicit ordered `points` vector or an ordered `ranges` array;
the former active top-level `start/stop/step` form is rejected. A linear segment
uses exactly one of positive `step` or a point count, while a logarithmic segment
requires positive endpoints and a point count. Segments include their endpoints,
concatenate in listed order without automatic boundary de-duplication, and then
receive that role's bidirectional expansion. The loader resolves ranges immediately
to the existing final point vector, so generator, target validation, session,
cleanup, and schema-v5 recording are unchanged. An off role still ignores dormant
recognized values, now including malformed `ranges`, while misspelled keys remain
strict errors. The unified example and Three-SMU daily/module/safety documents now
show all supported forms. All 76 focused Three-SMU/Keithley/fake tests and the full
400-test offline suite passed (five optional plotting skips); `compileall` passed.
No VISA resource was opened and no real query, status consumption, or setting write
occurred.

Three-SMU target read-only monitor follow-up (2026-09-01): the SNOM target's `lyr`
environment passed 23 focused monitor/Keithley/CLI tests, all 402 offline tests, and
`src/tests` compilation. Under explicit query-only authorization, the current plan
opened only active `gate_bottom` and confirmed a Keithley 2400 identity, 0 V source
setpoint, output OFF, compliance/ranges, 2-wire sense, and no compliance trip. The
monitor now avoids `:READ?` while output is OFF, displays V/I/R as `n/a`, never turns
output on, and leaves `:SYST:ERR?` unconsumed by default. No setting write or `*RST`
was sent. The target's unused NI GPIB passport was disabled while retaining the
Keithley KUSB passport; one selective device clear recovered a stuck parser/output
queue without changing source/output/compliance. At that checkpoint other roles,
consumptive status queries, smallest writes, and integration were uncommissioned;
the bottom-gate write follow-up below supersedes only the smallest-write item.

Three-SMU target bottom-gate write commissioning (2026-09-01): the Keithley 2400
C32 firmware does not permit `:READ?` or the protection-trip query while output is
OFF. QCoDeS preflight, configure confirmation, and cleanup now query output/source
state without claiming unavailable V/I; the session confirms a 0 V setpoint before
enabling output and obtains its first measurement only after output is ON. The raw
VISA monitor follows the same OFF-state rule and handles Ctrl+C without a traceback.
After consuming the backlog created by the former illegal queries, SNOM
`gate_bottom` (Keithley 2400 serial 4029737) completed the explicitly authorized
five-point -0.1, -0.05, 0, +0.05, +0.1 V scan. Run
`data/three_smu/20260901_110258_e2b23039` contains five clean formal samples and is
`completed`/`accepted`; structured cleanup confirms source setpoint 0 V, output OFF,
status `0,"No error"`, and no manual verification requirement. A subsequent
three-sample query-only monitor independently confirmed 0 V/OFF. Forty-one focused
tests and the complete 404-test offline suite passed. Bias/top real commissioning
and integrated acquisition remain pending.

Three-SMU direct-run live-panel follow-up (2026-09-01): routine `python -m
attodry_control.three_smu_cli run` now starts after its printed validated-plan
summary without `RUN THREE SMU`; only `finish_action = "hold"` still requires
the separate exact `HOLD OUTPUTS` confirmation. One session remains the sole
hardware path. After each formal sample is durably recorded, the session publishes
it to an in-process FIFO before rejecting an unsafe sample; the CLI prints sample
number/total, repeat, segment, elapsed time, source-setpoint readback, V/I/R, output and
`CLEAN`/`PROBLEM` from that FIFO. It prints the already-read status/error queue and
reason only for a problem sample. The live Notebook plots the same FIFO rather than
making any independent SMU query. Twenty-six focused fake-instrument/session/CLI/
Notebook tests and the complete 405-test offline suite passed (five optional plotting
skips). This follow-up opened no real resource and sent, queried, or consumed no
real hardware command/status entry.

Three-SMU terminal-table follow-up (2026-09-01): the FIFO-backed direct-run panel
now renders a single fixed-width table header in terminals at least 96 columns wide,
then appends each sample's progress and active-role setpoint/V/I/R/output readback
with engineering units. Narrow terminals use compact per-role lines instead of
wrapping the table. `PROBLEM` status/error details remain conditional and the
renderer has no hardware access. Eleven focused fake CLI/Notebook tests passed;
the complete 406-test offline suite passed with five optional plotting skips. No
real resource was opened, queried, consumed, or written.

Three-SMU unified plotting follow-up (2026-09-01): `notebooks/three_smu.ipynb`
replaces the old separate live and accepted-only notebooks. The CLI alone owns the
SMU session and, before opening it, binds a loopback-only Server-Sent Events endpoint at
`127.0.0.1:8765/events`. It publishes the same already-durable formal samples that feed
the terminal FIFO; Notebook consumers cannot create a session, import the hardware path,
query VISA, write settings, or consume status queues. The dashboard defaults historical
loading to completed/accepted/clean data, labels live data provisional until finalization,
and makes rejected/problem evidence explicit Audit opt-in. It supports added line/scatter/
incomplete-map panels, cross-role coordinate/readback U/I/R/G axes, gate-value series and
slicing, so bias I--V families at different gate voltages can be displayed together without
mixing forward/reverse segments. The feature is offline-only: 23 focused CLI/analysis/
stream/Notebook tests passed (one optional Matplotlib rendering test skipped because this
development environment lacks the analysis extra); source/test compilation and the complete
offline suite (410 passed, 6 optional Matplotlib skips) passed. No real instrument resource
was opened and no hardware command or status query ran.

Three-SMU live-plot rendering correction (2026-09-01): live stream receipt was confirmed by
the dashboard status counter, but asynchronous `display(fig)` did not reliably render inside
the VS Code/Jupyter widget callback. Each plot card now writes Matplotlib PNG bytes directly
to an `ipywidgets.Image`, so the same event that increments the formal-sample counter visibly
refreshes the chart. This changes presentation only; session, stream, safety and hardware
ownership are unchanged. Notebook JSON/code syntax and 14 focused fake CLI/stream/Notebook
tests passed (one optional Matplotlib test skipped); no instrument operation ran.

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
- `TEMPERATURE.md` separates its offline, target-offline, real read-only, and
  write-commissioning stages.
- `MAGNETIC_FIELD.md` now documents the standalone local fake-DLL implementation,
  exact ordered/duplicate point semantics, separate single-target/write/scan
  gates, canonical fsynced JSONL, file-only monitoring, and monitored cleanup.
  It records M2 target-host verification complete while leaving M3--M5 hardware
  commissioning pending, and does not convert discrete setpoints into a
  physical-path claim.
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

Stage 5 - gate safety and integrated acquisition: model-independent offline core,
Three-SMU target-offline checks, and the current bottom-only read-only monitor path
are complete; remaining-role/read-write commissioning and main-acquisition
integration remain pending.

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
- The current source-based main/Lock-in/Temperature/Magnetic-field/Three-SMU
  suite passes all 450 tests in 14.373 s with five optional matplotlib rendering
  skips; source/test compilation and magnetic CLI help checks pass. The plotting
  path is unchanged from its prior rendered validation;
  the current system matplotlib/numpy binary mismatch is an environment issue.
- The previously built local `attodry_transport_control-0.1.0-py3-none-any.whl`
  was built without downloading dependencies, inspected, and isolated-import
  checked. SHA-256:
  `0cb4b12e7ab76dbc8b2141e391955ba2c3f0b89167f3d254800bd747edc9d6b2`.
  It predates the standalone magnetic-field module and is not evidence for that
  implementation. A new wheel has not yet been reported; this is not the frozen
  hardware wheelhouse.
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
- Harmonic-scaling figures keep their legends outside the axes on the right.
  Every fitted curve includes a substituted, current-normalized scalar `R(I)` or
  phase-preserving complex `Z(I)` equation plus exponent/available interval,
  R², relative RMSE, and AICc (or log-model ΔAICc); the legend title records the
  log/scalar/phase/complex verdicts. Full precision remains in the optional
  `selection_manifest.json`; the Notebook does not render raw fit-result tables
  or a separate formula panel.
- The first Notebook cell now exposes `SCALING_PLOT_METHODS`. Its default draws
  log, scalar-R, and complex fits together for comparison; changing it to
  `("scalar",)`, `("log",)`, or `("complex",)` limits the fit curves, equations,
  verdicts, and residuals shown without changing any computed fit. The selection
  is archived as `harmonic_scaling_plot_methods`, while full results for all
  methods remain in `selection_manifest.json`. Commented one-method examples are
  included beside the setting; `scalar` means a linear-coordinate R fit and does
  not force exponent one. This is analysis-only and opens no hardware path.
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
- Current envelope (2026-09-11): `abs(Bx) <= 3 T`, `abs(Bz) <= 9 T`;
  if both components are nonzero, also require `sqrt(Bx^2 + Bz^2) <= 3 T`.
  Only exact zero selects single-axis; smaller configured axis limits remain binding.
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

The reference's `isZeroingField` state, GUI action/error strings, and vendor log
output are retained only as optional diagnostics in the independent module's audit.
They do not select a transition policy, schedule a command, or prove zero: this
project requires confirmed setpoint, actual Bx/Bz, field-control/error state, and
the configured dwell. The reference GUI/driver is not imported or copied into the
active hardware path.

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
frame on the right, so a long temperature list does not cover the curves. Its labels
show only the actual formal-window mean temperature and omit the requested setpoint.

All other analysis plotting functions follow the same right-side legend layout.
The commissioning Notebook displays figures only; raw numerical fit records are
preserved for optional export rather than emitted during plotting.

The 2026-09-03 publication-style pass replaces lock-in twin y axes with aligned
shared-x magnitude/phase panels, adds scoped typography and vector-font settings,
uses redundant color/marker/line encodings, and explicitly names sample-SD versus
circular-sample-SD error bars. Optional export now writes 600 dpi PNG, PDF, and
editable-text SVG. These are general manuscript defaults, not journal-specific
compliance, and they do not change any data, filter, scale, or fit result.
Temperature I–V figures now use a brighter warm `plasma` sequence, whereas
multi-frequency I–V figures use a separate cool `viridis` sequence. Marker and
line-style redundancy remains in both plot families for grayscale accessibility.

A new independent condensed-report cell and `report_plotting` module combine any
selected Vxx/Vxy h1/h2/h3 amplitudes in one I–V figure. Optional selected phases
use an explicit right y axis; amplitude-only mode creates no second axis. For each
amplitude channel the report shows fit-qualified observations and exactly one
`scalar_selected_free_model` curve, with its numerical equation and fit metrics in
the outside legend. The curve is restricted to the measured fit-current range.
Optional vector/raster export records channels, source files, phase treatment,
model parameters, and excluded-point counts in a companion JSON manifest. Existing
per-channel figures and three-method fit comparison behavior are unchanged.

For each available XX/XY × h1/h2/h3 channel, analysis produces a separate R-amplitude
figure and phase figure. Each actual formal-window mean temperature is a separate
curve versus the archived readback-derived RMS current. Phase repeats use circular
mean/standard deviation and are unwrapped along increasing current only for display;
the raw record is never changed. Optional CSV/PNG/PDF/SVG export records the selected
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

## Current Lock-in excitation resistance estimate update (2026-09-24)

The daily Lock-in excitation preflight now uses a single nominal device-resistance
estimate for both current and device-terminal voltage calculations. It computes
`Iestimate = Vsine / (Rseries + 50 Ω + Rdevice,estimate)` and
`Vdevice,estimate = Iestimate × Rdevice,estimate`, then compares each estimate
with its configured RMS threshold before opening VISA. The separate
`maximum_device_resistance_ohm` input was removed; delete that key from any local
TOML before using the new loader. This unified nominal model does not guarantee
protection against changing device impedance, open/short faults, or wiring errors.
Existing point-current and analysis fields remain named `nominal_current_a_rms`
to preserve the data contract. No real instrument was queried or written.

## Immediate next implementation tasks

1. Obtain a distinct, limited real-hardware authorization before any combined DLL/VISA
   operation. Hardware-test interruption/resume separately; do not infer or alter
   PID values automatically.
2. Perform staged attoDRY small-movement commissioning only after a new explicit
   write authorization and operator-selected smallest practical targets.
3. Complete Three-SMU read/write commissioning for the remaining planned roles and
   integrate the independent module only under separate plan-specific authorization
   and front-panel safety review.
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
