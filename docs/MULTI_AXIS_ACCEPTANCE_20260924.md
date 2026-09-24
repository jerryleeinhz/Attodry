# Multi-axis hardware acceptance — bounded path passed

Date: 2026-09-24. Branch: codex/integration-four-module-scan.
Runtime unchanged from the previously tested cleanup-recovery source; this stage
adds private configurations and evidence, not a new driver or relaxed policy.

## Authorization and scope

Operator reported no visible XY OVLD flashing and explicitly requested completion
of multi-module validation. Sample/wiring and prior limits remain unchanged:
the sole active legacy smu_bias role is a separate gate, not transport bias;
1 V / 1 uA ceilings; XX source ceiling 0.2 Vrms; XY SINE OUT disconnected.
No external series resistor; the 0.01-ohm TOML placeholder and nominal 100-Gohm
device estimate are not measured resistance or a current limiter.

Actual test values: gate 0/0.01 V; XX 4/8 mVrms at 17.777 Hz; XX/XY ranges
1 V/50 mV; both h1/h2, 300-ms time constants and 5.1-s configured settling.
Temperature targets 2/2.1 K; X target zero and ordered Z 0/0.05/0 T.
Temperature stable-readback dwell 30 s/range 0.05 K/minimum response 0.02 K;
field dwell 10 s/tolerance 1 mT/range 0.5 mT, timeout 7200 s and max_step 0.05 T.
X/Z/resultant scan ceilings remain 0.02/0.051/0.051 T. No ramp-rate or scale
correction, no bypass of status checks, no forced faults or limit excursions.

The vendor GUI was absent; the existing Jupyter kernel was idle, with last
activity before the earlier acceptance. Four bounded status-consuming SR830
diagnostic samples were all clear before the first run. All three formal
preflights and runs then passed without automatic retry. Earlier overload
rejections remain failed and retained; their cause is not established.

## Runs and results

| Run ID | Active loop order (outer to inner) | Varying axes | Accepted samples | UTC start / finish |
| --- | --- | --- | --- | --- |
| joint-gate-excitation-20260924-v2 | T, SMU, B, Lock-in | gate x excitation | 4/4 | 09:09:18 / 09:15:32 |
| joint-reversed-20260924-v1 | T, B, Lock-in, SMU | excitation x gate | 4/4 | 09:16:24 / 09:22:07 |
| joint-four-axis-20260924-v1 | T, B, SMU, Lock-in | 2 T targets x 3 ordered B points x 2 gates x 2 excitations | 24/24 | 09:22:41 / 10:01:25 |

Each run completed with process exit 0, verified clean cleanup, no primary error,
no communication uncertainty and no manual-verification flag. First two runs
held target T 2 K and B zero. The 24-point run took 2324.378 s (38 min 44 s);
all four axes varied. It records 2964 events and 1371 full cryostat states.
Two temperature commands and seven magnetic command attempts have matching
successful results: two repetitions of Z 0.05 -> internal 0.025 -> 0 T,
then final sweep-to-zero. The internal return waypoint is not a formal point.

## Measured values, not just requested coordinates

- All formal X values were zero and formal Z values matched 0/0.05/0 T within
  the configured tolerance. Full-state transient |X| reached 0.0008 T; Z ranged
  -0.0003 to 0.050000000745 T. Do not describe the transient X field as exactly zero.
- Full-run measured temperature range: 1.998399973-2.096499920 K; all cryostat
  error codes zero. The 2-K target's formal-window means were 1.9997694-2.0006867 K.
- Important thermal limitation: for target 2.1 K, formal-window means ranged
  2.0231913-2.0957144 K. The existing stable-readback criterion accepted a slowly
  rising temperature within its window/range and minimum-response bounds.
  This is successful execution under that criterion, NOT strict equilibrium
  at 2.1 K or proof that all twelve points share one actual temperature.
  Analyze actual.temperature_k; a strict-target experiment needs separately
  chosen target tolerance/stability settings before running.
- Across the 32 formal samples, maximum |gate voltage| was 0.01000886 V and
  maximum |gate current| 7.285066e-11 A (72.85 pA), below approved ceilings.
  Gate current must not be interpreted as sample transport current.
- XX/XY h1/h2 fields are present in every formal sample. This validates acquisition
  and data routing, not signal calibration, adequate SNR at these wide ranges,
  physical reproducibility or the significance of small/zero harmonic readings.

## Final confirmed cleanup

At completion of the 24-point run:

- Actual AND setpoint X/Z zero; field control ON, device error zero.
- Gate setpoint zero, output OFF, compliance 1 uA, no instrument error.
  The pre-OFF voltage readback was about 6.959 uV; voltage/current after OFF are
  unavailable, not reported as measured zero.
- Both SR830 units h1 and 4 mV, LIAS=0/ERRS=0, ranges XX 1 V/XY 50 mV retained;
  reference roles unchanged and XY output remains physically disconnected.
- Temperature control ON holding target 2.1 K (float32 2.099999905 K);
  final actual temperature 2.095799923 K. No return to base temperature was requested.
- Connections closed and scan process exit independently confirmed. These are
  completion-time readbacks, not a continuing unattended monitor.

## Monitoring, analysis and offline verification

During scans, monitoring queried only SQLite in read-only/query-only transactions;
no second VISA/DLL controller was started. Run/condition/attempt/cleanup and
last-recorded environment status were inspected throughout.

All three SQLite files pass integrity checks, and local/remote hashes match.
Default analysis loads 4 + 4 + 24 = 32 accepted, nonsimulated samples. Each
electrical-order run regroups into two excitation-indexed gate curves of two
points. The full grid regroups into twelve two-point gate curves, preserving
temperature, excitation and the distinct before/after zero-field indices.
Loading all three together gives sixteen two-point curves, separately for XY h1
and h2; run boundaries remain protected. No averaging or duplicate removal.
Exact 24-condition order and all actual field coordinates were checked.

Local full hardware-blocked regression rerun: 644/644, 126.899 s, zero failures,
errors or skips, including all 64 nonempty module permutations with fake devices.
The prior exact-target lyr 644/644 receipt remains applicable: runtime unchanged,
all 113 deployed archive-file hashes verified during this stage before later
documentation updates. Private test helpers only read files; no new runtime API.

## Evidence locations and hashes

Target source: C:/Users/LK_Setup/Yuanrong Li/Integration_recovery_20260924/source.
First run remains under run_data/gate_excitation_20260924; reverse and full-grid
runs under run_data/multi_axis_20260924. Local ignored run_data/multi_axis_20260924
contains copied databases, remote-evidence, audit helpers and audit JSON receipts.

| Database | SHA-256 |
| --- | --- |
| joint-gate-excitation-v2.sqlite | c38aa9e06d7fb1aad5345a6c31dff8095d41abd6f7dfaae1bf833f14bfb48168 |
| joint-reversed-v1.sqlite | 6b15cbf5265e30bf4b4704aa800c4c1581f870a2fd23292b2df3b2dc13c4c4d2 |
| joint-four-axis-v1.sqlite | 4dd64e83f2c0190a3fea9b823cd5416ae94e976e2bc1b5b79967a395d94b6ecf |

Raw data/configs/addresses remain uncommitted. Earlier failures are described in
[the preceding acceptance record](JOINT_ACCEPTANCE_20260924.md).

## What this closes and what it does not

This closes bounded real validation of the shared four-changing-axis path,
both electrical nesting orders, repeated ordered field endpoints, file-only
monitoring, normal cleanup and accepted-only regrouping. The code supports
module subsets/orders; all 64 are fake-tested, not each physically commissioned.
Real communication-loss/interrupt/fault recovery was not deliberately induced.
Earlier intermittent XY preflight overload and 5-mT-command/readback mismatch
remain unexplained; no claim that this run repaired their cause.
Lock-in frequency scanning, hardware resume, high-field/high-output operation,
strict 2.1-K equilibrium and precision/reproducibility studies are outside this pass.
