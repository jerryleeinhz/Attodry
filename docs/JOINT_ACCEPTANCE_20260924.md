# Joint hardware acceptance — bounded 50-mT pass and retained failure evidence

## Latest result: three-point joint acceptance passed

Date: 2026-09-24. Run: `joint-z50mT-20260924-v3`, LK_setup exact lyr Python.
UTC 07:08:13.216842 to 07:15:27.652545 (434.436 s); process exit 0.
All three conditions accepted; one formal sample per condition; clean cleanup,
no communication uncertainty and no manual verification required by this run.
This supersedes the earlier current-state/blocked statements below, but does not
alter either rejected attempt or resolve the original field mismatch.

### Scope and authorization

After operator-reported manual zero, the operator explicitly approved ordered
Z 0 -> 0.05 -> 0 T with X target zero, confirmed vendor GUI exit and permitted
field commands. A new isolated source/config directory was used; existing
experimental checkouts and data were not overwritten. All four modules were
active, but temperature, gate and excitation were fixed: this validates this
bounded joint path, not every subset/order or a grid varying all four axes.

- Temperature target 2 K, stable-readback dwell 30 s; gate 0 V, 1 V/1 uA ceilings.
- XX excitation 4 mVrms at 17.777 Hz; XX/XY full scales 1 V/50 mV, TC 300 ms,
  settling 5.1 s. Both roles measured h1 and h2. XY SINE OUT remained disconnected.
- Z/resultant scan ceilings 0.051 T, X ceiling 0.02 T; max waypoint step 0.05 T,
  field timeout 7200 s, unchanged tolerance 0.001 T/range 0.0005 T/dwell 10 s.
- The longer timeout is not a ramp-rate setting. No supported ramp-rate setter
  was found in the supplied public header; no guessed scale correction was made.
  Float32-aware planning inserted a 0.025-T return waypoint, not a formal point.

### Preflight and retained rejection

Read-only cryostat preflight at 07:04:56-07:05:05 UTC passed 10/10 samples:
actual/setpoint X/Z zero, error zero, both controllers ON, temperature
1.9993-2.0018 K. The first joint attempt, `joint-z50mT-20260924-v2`, then rejected
an XY input/reserve overload in SR830 preflight before any setting writes or
condition attempts. Exit 2; raw rejected record remains excluded from analysis.
Four status-consuming diagnostic queries at 3-s intervals were subsequently
clear on both SR830 units; no settings were changed. One same-plan retry passed.
The intermittent overload's cause remains unknown; its guard was not bypassed.

### Observed measurements and cleanup

| Formal point | Requested Z (T) | Actual Z (T) | Actual temperature (K) |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 2.0001292 |
| 1 | 0.05 | 0.050000000745 | 1.9995797 |
| 2 | 0 | 0 | 2.0000591 |

All formal X readbacks were zero. Across 321 full-state snapshots, Z ranged
from -0.000300000014 to 0.050000000745 T; transient |X| reached 0.000899999985 T
despite its zero target. Temperature ranged 1.998399973-2.001600027 K; all device
error codes were zero. No configured field ceiling was exceeded.

Four magnetic commands have matching successful audit results (DLL return 0):
Z 0.050000000745, Z 0.025000000373, Z 0, then sweepFieldToZero. The first
50-mT change took approximately 54.25 s to reach the magnetic stable-point
criterion; the return-zero point approximately 76.34 s from its first waypoint.
These are observed timings, not configured/guaranteed ramp rates.

Final cleanup confirmed actual AND setpoint X/Z zero, with field control ON;
this is controlled zero, not a claim of persistent mode. Gate SMU setpoint was
zero and output OFF with 1-uA compliance; no post-OFF measured-voltage claim is
made. Both SR830 units were h1/4 mV with clear LIAS/ERRS and retained 1 V/50 mV
ranges. Temperature control stayed ON, target 2 K, final actual 1.999899983 K.
Normal zero recovery was exercised on hardware. Out-of-envelope recovery was
not deliberately induced and remains covered by fake-device regression tests.

### Verification and provenance

Scoped cleanup fix: six new regressions; full guarded suite 644/644 locally
(121.119 s) and on exact target lyr (143.182 s), zero failures/errors/skips.
The initial target test launcher interrupted on PowerShell stderr handling;
that incomplete receipt is retained separately and is not a passing result.
All 113 archive source/test/tool/template/Notebook files were hash-verified
before target tests; archive SHA-256:
`1321dac57c11efc38f3b006647d72ba4f83dbb1ce098966f757293cfc31cb2df`.

The successful SQLite has 711 events and passes integrity_check. Offline
canonical analysis returns exactly three accepted, nonsimulated rows, including
both roles' h1/h2 fields; the rejected v2 returns zero default rows. Local and
remote successful database SHA-256 match:
`b3ae148e378e860bf431c7d725ef56902b9be94cbf20bdb883261f98521a3471`.
Private TOMLs, raw databases and receipts remain ignored under local
`run_data/recovery_20260924` and remote
`C:/Users/LK_Setup/Yuanrong Li/Integration_recovery_20260924/source/run_data/recovery`.

The earlier 0.005-T-command/0.05-T-actual discrepancy remains unresolved.
Matching DLL file hashes do not identify its cause, and this successful 50-mT
test does not validate 5-mT targeting. No automatic larger grid was started.

## Historical attempt: stopped on field readback mismatch

Date: 2026-09-24. Runtime source: b3570c5 on LK_setup, lyr Python.
This is a failed, bounded hardware attempt, not completed four-module commissioning.

## Authorized setup

Sample connected; sole active SMU physically drives a separate gate, using the
legacy smu_bias role. Limits: 1 V / 1 microampere; requested gate point 0 V.
XX excitation was 4 mVrms at 17.777 Hz (operator ceiling 0.2 Vrms).
XY SINE OUT was confirmed physically disconnected. No external series resistor;
the private TOML retains the operator's 0.01-ohm parser placeholder and estimated
100-Gohm device resistance. The estimate is not measured impedance or a limiter.

User explicitly requested apply-toml with a wider range. XY was widened from
20 to 50 mV, the maximum in the unchanged XY policy; XX remained 1 V full scale.
An initial apply attempt rejected the TOML/panel time-constant mismatch before
writes. The dedicated configuration then retained the existing 300 ms on both
instruments and used 17 time constants (5.1 s) for settling.
The successful apply record reports write_performed=true, completed=true,
XY sensitivity code 22, and both LIAS/ERRS zero after application.
Only the selected XY fixed-setting commands were applied; source excitation
remained 4 mVrms. Inactive gate placeholder tables satisfy the standalone
configuration loader; both roles remain off and were never accessed.

The combination plan validated offline: temperature target 2 K, gate 0 V,
ordered X/Z points (0,0), (0,0.01), (0,0) T, XX 4 mVrms, XX/XY h1 and h2.
Field component/resultant ceilings were 0.02 T, waypoint step 0.005 T,
field dwell 10 s, and temperature stable-readback dwell 30 s.

## Observed outcome

Run ID: joint-wide-20260924-v1. SQLite integrity check: ok.

- First zero-field condition completed one clean sample. Formal-window actual
  temperature was 1.7464088 K, not the requested 2 K. Stable-readback acceptance
  is not target equilibration. Gate readback was 6.875118 microvolts and
  -17.29773 picoamperes. Both Lock-ins' h1/h2 fields are present.
- During the second condition the driver issued one Z waypoint command,
  0.004999999888241291 T (float32 bits 0ad7a33b), and confirmed the same setpoint.
  DLL return code was zero. No 0.05-T command was issued.
- Actual Z subsequently read 0.026 T, exceeding the private 0.02-T ceiling.
  The run stopped and condition 1 was rejected; condition 2 was never attempted.
- Electrical cleanup confirmed XX 4 mV, both Lock-ins h1 with clear statuses,
  XY 50 mV/XX 1 V ranges, and SMU setpoint zero/output OFF with 1-microampere
  compliance. The SMU terminal-voltage measurement precedes output disable.
- Magnetic and temperature cleanup failed: the audited full-state reader rejects
  the out-of-envelope field before either cleanup can execute. No sweep-to-zero
  command was emitted. Last cleanup Z readback was 0.0394 T.
- A separate read-only check at 06:22:45 UTC reported X=0, Z=0.0500000007 T,
  Z setpoint=0.0049999999 T, both controllers ON, temperature target 2 K,
  actual temperature 1.7896000 K, and error code zero. It then disconnected.
  This is a historical readback, not a current-state or zero-field claim.

The database records failed and manual_verification_required=true. Default
combination analysis returns zero rows despite the earlier accepted condition,
as required for a failed run/unverified cleanup. Raw data remain available for
audit. The remote launcher's printed process exit code was empty; it must not
be used to infer success. The database and terminal run result are authoritative.

## Blockers and next work

Subsequent operator update: manual return to zero was reported, and a distinct
Z 0 -> 0.05 -> 0 T / X 0 retry was approved. The operator also confirmed vendor
GUI exit and authorized field commands. These statements do not change the
failed run's cleanup or certify a fresh independent readback. The integrated
zero/disable recovery fix and its new verification are tracked in the current
PROJECT_HANDOFF section; the chronology below describes the original stop.

Operator was asked to use the laboratory's normal field-zero procedure and
report panel actual/setpoint X/Z. No further hardware I/O should compete with
that manual recovery. Zero has NOT been confirmed by this session.

The observed tenfold actual/setpoint difference is unresolved. Current code
passes the float32 Tesla value directly; the supplied vendor header labels both
setter and getter parameters in Tesla. Neither evidence establishes whether
the discrepancy is controller behavior, calibration, DLL interpretation, or
another cause. Do not compensate with an assumed factor of ten or widen the
test envelope to continue.

The cleanup coupling is a real blocker: field-envelope rejection in
_AuditedDriver.read_state prevents monitored zero and temperature shutdown.
It needs an independently reviewed/tested recovery design before further field
commissioning. Preserve strict scan limits and all out-of-range readback evidence.
The standalone apply-toml command also requires existing non-sensitivity fixed
settings to match before writing; it did not automatically correct the time
constant mismatch in this attempt.

Private configuration, apply records, SQLite and logs remain under ignored
run_data/joint_acceptance_20260924. A local remote-evidence copy preserves the
database/logs and successful apply record. No hardware addresses, local settings,
or raw experiment data are included in this documentation commit.
