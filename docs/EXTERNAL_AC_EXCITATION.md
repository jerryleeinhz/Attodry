# 6221 AC excitation with SR830 XX / SR865A XY

Status: implemented and tested with injected fake instruments only. Source-code
deployment does not commission a real waveform, reference wiring or sample limits.

Output connections are now explicit: `inner_shield = "output_low" | "guard"`,
`output_low_grounding = "floating" | "earth"`, and `phase_marker_enabled = true`.
See the [complete parameter choices](KEITHLEY6221_PARAMETERS.md) and
[25 Hz six-point review template](../config/hardware.6221_25Hz.example.toml).
Set/read-verify connections only while OFF; capture actual connections in formal
brackets. Cleanup never switches grounding. Source deployment is not commissioning.

## Two explicit topologies

The existing configuration defaults to `combination_scan.excitation_source =
"sr830"`. Its Lock-in voltage/frequency grids, XX internal reference, XY external
reference, source-voltage safety policy, cleanup and old analysis remain intact.

The new topology selects `"keithley6221"`. The `excitation` module owns a sine
current waveform and both SR830 XX and SR865A XY are external TTL receivers.
Both detector SINE OUT connections must be declared physically disconnected.
Detector setup and cleanup never write source amplitude, frequency, offset or
phase. Semantic roles, native sensitivity/status mappings and channel labels
remain separate from instrument model names. SR865A has native SCAL and IRNG
settings; SR830 SENS/Reserve/LIAS codes are not applied to it.

Start from [the external-source template](../config/hardware.6221.example.toml).
It deliberately contains placeholder addresses and unconfirmed SR865A firmware
status capability, so it cannot be run unchanged. Keep private addresses and DUT
limits in the ignored local copy. The sibling `lockin_safety.toml` still applies.

```toml
[combination_scan]
backend = "hardware"
order = ["temperature", "excitation", "lockin"]
excitation_source = "keithley6221"
lockin_mode = "detector"
samples_per_condition = 1
repeats = 1
run_name = "temperature_current"
note = "Reviewed local limits and reference wiring"
```

Include the existing temperature tables when selecting temperature. Magnetic
and SMU outer coordinates continue to use their existing modules and limits.
This feature does not change their hardware capabilities or environmental
qualification policies. The source must occur before the Lock-in detector stage;
invalid orders reject offline, without silently reordering the plan.

`[excitation_6221]` accepts ordered `frequency_points_hz` / `current_rms_points_a`
or corresponding `frequency_ranges_hz` / `current_rms_ranges_a`. Each coordinate
uses exactly one form; `*_points_*` accepts numeric lists, not range tables.
A linear range contains `min`, `max`, `scale="linear"` and exactly one of integer
`points >= 2` or positive finite `step`. The step must divide `max-min` exactly
in decimal arithmetic; neither truncation nor an extra endpoint is added. A log
range uses `scale="log"` and `points` only. Both forms include endpoints. A fixed
frequency uses a one-element explicit list. Ranges expand offline before connection
into the same point contract: frequency outer, current inner, declared order and repeated
endpoints retained. Counts and the 100000-condition source grid are checked before
allocation. Equivalent list/range forms retain same-run recovery compatibility;
changing the actual grid requires a new run. See [range choices](KEITHLEY6221_PARAMETERS.md#列表与范围).
`lockin` is a single detector stage with no source coordinate/grid of its own.
For example, 18 temperatures × 2 frequencies × 3 currents produce 108 conditions,
not another Cartesian product with the old Lock-in sweep. `[lockin_acquisition]`
selects role harmonics, settling, sample interval and overload policy.
`[lockin_sweep]` is rejected in this topology to prevent competing source settings.
The legacy role `frequency_hz` and `source_voltage_v` fields remain schema fields;
they are not the external source coordinates and are never written in this mode.

## Source values and lifecycle

- User coordinates are RMS amperes. The 6221 `SOUR:WAVE:AMPL` command accepts peak
  amperes; for sine waves the driver sends `sqrt(2) * current_rms_a`.
- `abs(offset_a) + AC peak` must remain within the explicitly configured
  `max_current_peak_a` and the instrument envelope. Compliance is a separate
  explicitly configured voltage limit, verified by readback. Values, units,
  finite numbers, instrument bounds and every harmonic/frequency combination are
  validated before resource access. Bool values are not numeric coordinates.
- Source preflight requires a known 6221 identity, inactive waveform, exact DC
  zero, output OFF, clear errors, known status and asserted interlock. It does not
  take over an already active source. All selected-device preflights precede
  configuration writes.
- The source configures sine waveform, BEST range, explicit DC offset, infinite
  duration, disabled waveform external-trigger input, and an explicit phase
  marker connector line and phase. The marker line must not conflict with the
  trigger output line. Its documented 1 microsecond pulse is a reference signal,
  not proof of electrical current through the sample.
- For each changed source point the first implementation verifies stop/OFF,
  programs the waveform, then arms and initiates it. Output and the native
  Wave Started condition must confirm execution. It does not assume that a
  returned command means the waveform started. An unchanged point is read-only.
- Source settings and raw condition/event/error evidence are read before and
  after the complete formal sample group. Transient compliance events are retained
  even if the current condition bit subsequently clears. Unknown/malformed status,
  source-setting changes, error or lost output fails the condition.
- Cleanup attempts waveform abort and output OFF, then independently verifies
  inactive waveform, OFF and DC zero. Only that proof permits detector cleanup.
  Unknown communication or failed verification never certifies OFF. Raw partial
  results and the last confirmed state remain available; cleanup is not retried
  through a new session. Other selected-module cleanup/close attempts still run.

Output remains at the last source point during subsequent outer-axis movement,
as in the existing continuous electrical combination behavior, until the next
source change or cleanup. This is not a pulsed/dark-window excitation feature.

## Detector timing and range rules

Source transitions can legitimately remove the reference. Such transition
latches are archived separately; formal acquisition still requires fresh known
lock, frequency and native status. The mixed detector session reuses the same
harmonic, range-rule, raw-pair, overload and monotonic settling engine. A changed
source or detector setting disturbs the appropriate shared deadline. Final
harmonic selection precedes the remaining wait; qualification does not add a
second complete wait for settings already settled. XX and XY share the wait but
their transport reads are sequential, with separate timestamps.

Outer `lockin_range_rules` may match temperature, field, SMU, current/frequency or
expanded source point indices if those modules precede `lockin`. The compiled
rule applies before detector qualification/formal capture, without creating new
conditions. Fixed model/station-approved full-scale rules remain exclusive with
auto/per-harmonic overrides. Partial application fails the condition; no mixed
setting data are accepted. Existing temperature qualification occurs once per
axis visit, with subsequent temperature telemetry and hard protections retained.

## Records, analysis and recovery

SQLite tables/schema stay compatible; new quantized scans declare
`measurement_contract = "electrical-excitation-detectors-v4"` and
`excitation_source = "keithley6221"`. Source `actual` coordinates are
`excitation_current_rms_a` and `excitation_frequency_hz`. They are **source setting
readbacks, not measured current**. Peak amplitude, offset, phase-marker settings,
compliance, native status, event flags and errors remain in the source record.
The Lock-in read owns the usual `lockin_xx_hN_*` / `lockin_xy_hN_*` measurements,
not a duplicate source coordinate. Formal pairs retain their source provenance.

Unified plotting dynamically exposes RMS-current and source-frequency axes with
setting-readback labels. Source/measurement contracts and archived configurations
separate curves, statistics and heatmap panels. New records do not acquire a
voltage-to-current estimate; old SR830 voltage and legacy estimated-current data
remain supported. Original invalid/rejected data and model-specific status remain
auditable and excluded from default analysis. Immutable loaded SQLite snapshots
continue rendering while acquisition appends records.

Recovery preserves the accepted prefix and repeats the entire pending condition.
It validates source before/after evidence along with all native detector pairs
against each execution's archived settings. A new process starts with fresh OFF
preflight and configures the pending source; it never inherits in-memory output
state. Switching source topology, grid, wiring/reference contract, current limit,
compliance or source setup requires a new run. Existing reviewed detector range,
Reserve, timing and environment changes retain their normal review requirements.
Legacy v1–v3 evidence remains readable under its original validation contract,
without invented connection or quantization evidence. Contract migration requires
a new run, not a silent recovery migration.

Offline preview uses `combination_cli describe-hardware --config <local-copy>`;
it does not open resources. The separate `run` command performs real acquisition.
2182 acquisition, passive SMU detection, optical gates/backgrounds, multiple sources
and arbitrary source/detector routing remain future work; they are not silently
enabled by adding the 6221 source module.

## References and verification

New scans use `electrical-excitation-detectors-v4`: RMS targets remain immutable,
while amplitude commands use the documented source-range programming grid.
The archived quantization policy is checked by live acquisition, recovery and
analysis. Seven-digit AMPL? serialization is a separate bounded comparison;
neither it nor the 16-bit waveform DAC is claimed to be the SCPI parameter LSB.
See [the grid, limits, preview and legacy compatibility](KEITHLEY6221_PARAMETERS.md#幅值量化设置读回与真实电流).

The waveform amplitude convention, phase marker and lifecycle/status commands
were checked against the manufacturer's [6221 reference manual](https://download.tek.com/manual/622x-901-01(B-Jun2005)(Ref).pdf)
and [6221 user manual](https://download.tek.com/manual/622x-900-01%20(C%20-%20Oct%202008)(User).pdf).
Tests inject command/response transports and a fake cryostat. The guarded runner
blocks real VISA/serial imports and instrument DLL loading. Fake passes establish
software behavior, not real reference lock, current calibration or DUT safety.
The duration query currently requires a documented literal `INF`/`INFINITY`;
an unknown numeric encoding is rejected before waveform startup rather than
interpreted as a valid infinite duration. Firmware-specific responses must be
confirmed during a separately authorized commissioning stage.
Command/wait budgets are recorded in [measurement performance](MEASUREMENT_PERFORMANCE.md).

## Requalification after an external-source change

SR865A CUROVLDSTAT is current state; LIAS retains events until read. After a
controlled source change, archive transition latches, require current SR865A
lock and a fresh clear SR830 reference latch, and check reference mode/slope/
frequency. If an unlock was observed, restart one complete shared settle at lock
confirmation. Otherwise elapsed time still counts. Repeated qualification does
not reopen the window. New settled/formal unlock remains fatal regardless of
record_continue. All polls are nonformal external_reference_requalification
records; the aggregate formal status interpretation remains unchanged.

Relock is limited to 30 s / 301 polls. ABOR acknowledgement is limited to 2 s /
21 reads with no repeated stop commands. Compliance including its event latch,
interlock, DC zero, unknown/error status and transport failures remain blocking.
Preflight still requires an immediately known inactive source. Neither timeout
certifies lock or OFF. Historical rejected data remains excluded by default.

If the first locked detector frequency observation is finite and in range but
outside the requested-target tolerance, keep it as a pending qualification
observation inside that same 30 s / 301-poll window. Re-observe only with clean
native status and unchanged detector settings, preserving actual values and
query times. A later reading must satisfy the original 100 ppm / 0.00101 Hz rule
before the original full shared settle. Unknown/invalid readings, other faults
and changed settings reject; settled/formal target mismatches are not retried.
Known transition-unlock latches remain archived, while current reference lock is
required during frequency re-observation. No frequency rewrite or tolerance
expansion is performed. The separate SR865A detection/reference consistency check
retains its own documented 5 mHz floor.

### Recovered output-scale latches under record_continue

After a controlled source transition and the existing settle, `record_continue`
may requalify an output-only SR865A latch when its current status is known clean.
For example, `CUROVLDSTAT?=0` with `LIAS?=1537` records CH1 and data-channel output
scale overloads that occurred since the preceding latch read; it does not prove
a currently persisting overload or establish the physical transient mechanism.

Keep the candidate's raw responses and acquisition times, then observe one fresh
complete shared settling window. Continue only after strict clean native status,
settings and coordinate verification. Repeated output latches, current overload,
other faults or unknown evidence still reject. The budget belongs to the current
point; another qualification call cannot reopen it. No status reset or setting
rewrite is used. Other policies and formal/cleanup validation remain unchanged.

### Frequency coordinates before formal capture

After qualification, the coordinate gate immediately before formal detector
capture can encounter a new counter observation outside the original target or
XX/XY pair tolerance. Only a finite, in-range mismatch can use up to two further
observations, 1 s apart, with strict clean status and unchanged settings around
the waits. The same original tolerances must pass before any formal detector
read; no acquired sample is retried. Preserve every mismatch/value/native query
time and reject persistent, unknown, faulted or unaudited outcomes. Failed proof
budgets cannot be reused. Other topologies retain their single-read behavior.

### Commissioning Notebook support

`sr830_commissioning_sweeps.ipynb` now includes an explicit SQLite/run selector
for 6221 RMS-current curves, all three existing harmonic fit methods and reports.
It uses the archived source RMS readback directly and retains execution-aware
accepted-prefix recovery and native selected-channel filtering. Old SR830
JSON/JSONL plots and voltage-derived current remain supported. See
[Notebook operation, exclusions and fit thresholds](DATA_ANALYSIS.md#6221-current-sweeps-in-sr830_commissioning_sweepsipynb).
