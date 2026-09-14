# Four-module combination scans — offline contract

Status: local integration development, 2026-09-14. **Not a real hardware runner.**

## What is available

The isolated branch `codex/integration-four-module-scan` includes temperature /
Lock-in integration be6071f, Three-SMU direct points 02f04ec, and magnetic 7d4417f.
Their existing commands, audit formats and analysis Notebooks remain available.
The original dirty main and integration worktrees have not been overwritten.

New `combination_*.py` modules provide an executable **simulation-only** contract:

- any nonempty subset of temperature / magnetic / SMU / Lock-in;
- array order defines literal outer-to-inner loops; a one-point axis is fixed,
  and an omitted module is inactive (not assumed physically off);
- magnetic X/Z is one atomic vector point list; duplicates, segment, direction,
  repeat and point indices are preserved, never sorted or deduplicated;
- SMU point values can contain one or more semantic roles, with voltage or
  current source units explicit. No fictitious zero channels for absent roles;
- set all changed axes, qualify the whole condition, then take new measurements
  at **every leaf and every sample**, including all active SMUs after an excitation
  change. Qualification is repeated after field changes;
- one station owner, deterministic cleanup order: Lock-in, all active SMUs,
  magnetic, temperature, close; every cleanup is attempted even after another
  cleanup/audit failure. This is simulated behavior, not a verified device state.

The simulator only models h1 XX/XY and a deliberately excitation-dependent SMU
response, `I = V / [1000 * (1 + excitation)]`. This is a regression fixture, **not**
a transport model, calibration, temperature-stability test or physical trajectory.
Real modules keep their existing harmonic/range/stability/safety implementations.

## Offline commands

Run from this branch's worktree, with its `src` first on the import path:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
python -m attodry_control.combination_cli describe --config config/combination.simulation.toml
New-Item -ItemType Directory -Force run_data/combination_demo
python -m attodry_control.combination_cli simulate --config config/combination.simulation.toml --database run_data/combination_demo/scan.sqlite --run-id demo
python -m attodry_control.combination_cli monitor --database run_data/combination_demo/scan.sqlite --run-id demo --once
```

Remove `--once` to refresh while the archived run state is active. The monitor
never reads VISA/COM/DLL or consumes a status latch. It reports run status,
accepted/total conditions, the current attempt, last recorded instrument reads,
cleanup, primary error and last-event timestamp. `process_liveness="unknown"` is
intentional: an active DB record does not prove the process still lives.
Last-recorded readings are historical evidence, not guaranteed present states.

The example contains three SMU voltages × two excitation values. It is NOT a
second daily hardware configuration file. The eventual real runner must obtain
device configuration and existing expanded grids from the single ignored
`hardware.local.toml`; the offline file cannot authorize or configure instruments.
There is no `hardware` backend or combined write-authorization flag.

## Data contract

Canonical integrated storage remains SQLite WAL, with FULL synchronous writes.
New version-1 tables are prefixed `combination_`; existing RunStore tables and
standalone JSON/JSONL/CSV are unchanged. Raw read events are committed before
formal sample promotion. At most one accepted attempt exists per condition.
The run's terminal status and its terminal event are committed together.

Each formal sample exports a wide/long-form observation row (one complete sample
per row, not a multidimensional array):

| Field family | Meaning |
| --- | --- |
| run_id / condition_id / attempt_index / sample_index | Unique sample identity; no coordinate deduplication |
| sequence_index / repeat_index / loop_order | Actual acquisition order |
| axes.<module>.index / segment / direction | Literal path position and branch |
| requested.* | Complete combination setpoints, including fixed axes |
| actual.* | Readbacks in that sample window, distinct from requests |
| measured.* | Available SMU V/I and semantic Lock-in role/harmonic measurements |
| timestamps.* / started_at_utc / finished_at_utc | Sequential read timing, not a simultaneous acquisition claim |
| status.* / clean / accepted / run_status | Quality and acceptance; raw partial events remain separately auditable |
| simulated / schema_version / implementation_sha256 | Synthetic-data flag, schema and archived implementation fingerprint |

Implementation fingerprint is in the archived plan. An axis's fixed value is
repeated in each full condition; inactive axes remain absent. Failed partial
reads stay in events; they do not become fabricated complete formal rows.
Non-finite numbers are recorded as explicit `invalid_numeric` evidence, rejected
for formal acceptance, never coerced to zero.

Default analysis requires accepted attempt + clean sample + completed run +
verified cleanup. Audit opt-in exposes formal samples of interrupted/rejected/
active runs without promoting them. Partial events remain available through SQL.

## Your SMU-outer / Lock-in-inner example

```python
from attodry_control.combination_analysis import load_combination_rows, select_series, plot_series

rows = load_combination_rows("run_data/combination_demo/scan.sqlite")
series = select_series(
    rows,
    x="measured.smu_bias_voltage_v",
    y="measured.smu_bias_current_a",
    group_by=("requested.lockin_excitation_v_rms",),
)
figure, axis = plot_series(
    series, x="measured.smu_bias_voltage_v", y="measured.smu_bias_current_a"
)
```

The result is two excitation-indexed SMU I–V groups, three points each.
Regrouping does not undo thermal drift, hysteresis or the fact that different
excitation curves were sampled interleaved. Per-instrument timestamps and the
original sequence remain available. Changing only the plotting order is not
equivalent to repeating the measurement with reversed physical loops.

Group by requested conditions for stable grouping; use actual values for plotted
coordinates. Explicit actual-value grouping is allowed but has no implicit
tolerance/binning. Other varying requested coordinates and run/repeat/sample/
segment/direction remain automatic group keys. Revisited non-X axis indices are
kept separate. Plotting does not average duplicates or fill missing values.
Scatter markers avoid implying measurements through missing observations.

## Legacy analysis and Notebook

`load_legacy_three_smu(path, audit=False)` adapts existing metadata + CSV through
its established loader. Source-mode units come only from the archived hardware
snapshot. Missing old mode/attempt fields stay unknown, not inferred from today's
TOML or fabricated as voltage mode.

`load_legacy_temperature_lockin(path)` adapts completed/clean summary JSON or
formal CSV through the existing temperature loader. It preserves actual formal-
window temperature and recorded current. Per-role/harmonic rows stay distinct;
missing original timestamps/sequence/attempt IDs stay absent. This adapter does
not add rejected-condition loading beyond the old loader's contract; inspect
the original JSONL for that audit. It does not invent SMU or magnetic data.

`notebooks/combination_analysis.ipynb` is the new common read-only entry:
set DATA_DIRECTORY once, refresh/select/load sources, inspect available columns,
optionally exclude exact sample IDs, choose X/Y/group/filter, then plot/export.
Old Notebooks remain for their specialized phase, fit, map and commissioning
views. Their science models were not replaced by a generic fitter.

Export creates a new directory, selected-sample CSV and selection manifest with
IDs, filters, exclusions, protected group keys, selection hash and analysis-source
hash; optional PNG/PDF accompany it. No raw file is overwritten. Figure integrity
follows the scientific-visualization skill's preservation/missing-data/redundant-
encoding guidance and reuses the project's scoped plotting style. This is not a
journal-specific figure or compliance claim.

## Resume and safety boundaries

- Resume only explicitly terminal failed/interrupted **non-magnetic** simulations
  with verified cleanup and identical archived plan/schema/implementation.
- Accepted conditions can be skipped; failed conditions get a new attempt index.
  Never overwrite or promote a former rejection.
- A killed process left active is not automatically taken over. No lease timeout
  is interpreted as proof that hardware is safe.
- Magnetic resume is deliberately rejected even if cleanup succeeded. Recovery
  needs a separately approved path/history policy; never automatically jump over
  ordered field points or insert a via-zero route.
- The new integrated contract retains universal resultant <=3 T, including pure
  Z, per this task. Standalone magnetic code retains its separately documented
  axis-dependent envelope; merging does not silently raise integrated limits.

## Next integration work — not complete in this checkpoint

1. Expose narrow prepare-point / sample / finish interfaces on the existing SMU
   and Lock-in controllers; preserve their existing raw command/status audits.
   Do not call a complete standalone sweep or cleanup at every Cartesian leaf.
2. Implement a real single-owner station using one attoDRY driver for temperature
   and magnetic control. Reuse execute_field_target and temperature qualification,
   preserve exact float32 field audits, recheck temperature after field changes,
   and bracket every formal window with actual environmental state.
3. Add the hardware-local combination loader using existing module grids and
   archived safety/config/identity snapshots. Real hardware must not pass through
   the simulator or inherit its lack of device-specific SMU boundaries.
4. Integrate global verified cleanup/close with all three SMU roles and magnetic
   monitored-zero protocol. Keithley READ remains before output OFF; no new
   requirement to read V/I after disabling output.
5. Fake-DLL/fake-VISA combined transcript/failure tests, then exact `LK_setup lyr`
   target-offline verification. Only afterward request separately scoped real
   connection/status-consumption/write commissioning.

This checkpoint proves module coexistence, arbitrary-loop data modeling,
simulation lifecycle and analysis regrouping. It does **not** prove the real
four-module execution path is implemented or commissioned.
