# Joint hardware acceptance — stopped on field readback mismatch

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
