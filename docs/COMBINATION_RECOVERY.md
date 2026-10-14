# Combination scan recovery

`run` starts a new scan. `resume` explicitly continues an existing hardware run
with its original ordered conditions and a separate execution/configuration
record for every startup. `--json` only changes formatting. Recovery is an
operator action after a stopped scan; `interrupt_policy=continue` retains its
existing Ctrl+C meaning in the standalone temperature path.

## Use

Use `lyr` and verify that the imported package belongs to Integration. Review
the original failure, cleanup actions and last confirmed states, then edit the
supported settings in `hardware.local.toml`.

```powershell
$env:PYTHONPATH = (Resolve-Path ./src).Path
python -m attodry_control.combination_cli resume --run-id ORIGINAL_RUN_ID --check
```

`--check` uses SQLite `mode=ro` and `query_only`. It opens no instruments,
consumes no latches, creates no execution and does not migrate/update the DB.
It reports accepted/remaining counts, the next condition, parameter differences,
original error and required declarations. Exit 2 means declarations remain or
recovery is blocked. Add `--json` for structured output. After reviewing:

```powershell
python -m attodry_control.combination_cli resume --run-id ORIGINAL_RUN_ID --check --acknowledge-failure
python -m attodry_control.combination_cli resume --run-id ORIGINAL_RUN_ID --acknowledge-failure
```

The last command explicitly authorizes selected-module recovery I/O, status
consumption and existing cleanup. It repeats file review and atomically binds
the execution to the current DB state before connection. `--database PATH`
selects the original DB; otherwise the TOML project database is used. An exact
original run ID is required; latest/auto selection is refused.

Uncertain or missing cleanup additionally requires physical resolution and
`--confirm-manual-verification`. Raising SMU compliance/software protection
requires `--approve-protection-changes` after confirming the new values fit the
sample's approved limits. These declarations never replace fresh hardware checks.

## Allowed changes and fixed plan

| Module | Recovery changes | Fixed constraints |
|---|---|---|
| Temperature | Target tolerance, stable range, dwell, minimum samples, poll/timeout | Acceptance mode, targets, response and hard/overshoot limits |
| Lock-in | Base/selected harmonic ranges and Reserve, supported autorange, TC/filter, settling/sample interval, existing overload policy | Roles/model/address, wiring/reference/input, source/frequency targets, harmonic identities, circuit/safety model and safety TOML |
| SMU | NPLC, point delay, counter-axis compliance/protection | Source mode, source-axis limit, V/I/power capability, targets, wiring, finish policy |
| Magnetic | Stable/readback tolerance within existing caps, dwell/poll/timeout, reduced step | Targets/order, transition policy, ACK tolerance, hard bounds and recovery vector ceiling 3 T |

For voltage sourcing, SMU `max_abs_current_a` controls both hardware compliance
and the software readback bound. The current-source counterpart is
`max_abs_voltage_v`. Increases are explicitly highlighted, never automatic.

The expanded points/order, duplicates, loop order, repeats, groups and selected
roles/harmonics are immutable. Changes require a new run. Formatting, metadata
and unused-module settings do not change the scan. Selected harmonic overrides
may be added/removed without changing the effective harmonic identities.
Unsupported settings fail closed.

## Handoff and failures

Normal combination launches and recovery share a station file lease and Windows
resource mutexes covering overlapping instruments across checkouts/sessions.
Recovery checks complete PID/creation/command identities and config/safety/source
hashes. Unknown Python, replacement kernels and instrument GUIs block execution;
no process is killed. Analysis kernels must be idle at the initial handoff.
The exact known file-analysis identities may subsequently plot. File-only
monitors remain allowed. Before recovery, manually reload analysis imports or
restart/rerun the analysis kernel to load the new execution-aware analysis code;
do not restart/create kernels while recovery is running.

All selected preflights pass before writes. Lock-in actual 4 mV/h1 and complete
clear status are required; an overloaded baseline needs separately authorized
repair. New range preparation widens before Reserve and narrows afterwards.
SMU actual zero/OFF, source mode, limits and status are checked. Cryostat actual
temperature, Bx/Bz, registers, controls and errors are read fresh. Recovery plans
the pending transition from actual state and retains the 3 T ceiling.

The first pending condition reapplies all axes and completes a new qualification.
No old dwell, baseline, partial group or harmonic is reused. A new magnetic
execution does not establish continuous magnetic history across interruption.
Owner/hash guards continue during acquisition. On guard failure, stop writes,
preserve last confirmed state, close connections and require manual verification;
other faults use the existing owned cleanup procedures. Never infer zero/OFF
after uncertain communication.

Old failure/cleanup remains recorded even when individual reset actions were
verified. New review/preflight establishes the new handoff without rewriting old
history. If all conditions were accepted but cleanup failed, recovery explicitly
selects cleanup-only: requalify the last environment, take no formal samples and
certify final cleanup. Unknown active/orphaned/started attempts are refused;
elapsed time or saved PID does not prove an owner exited.

## Records and analysis

Additive execution and attempt-mapping tables preserve the original plan, raw
samples and IDs. Each segment archives config/source/owner/review/error/cleanup.
Schema-1 failures receive a legacy execution at actual recovery only. The
reviewed 79ed5b4 LF/CRLF source set and identical current source are supported;
unknown archived versions are refused.

Only whole conditions become accepted. Failed partial attempts remain rejected
and the condition is remeasured. A unique database constraint prevents duplicate
accepted results. `record_continue` accepted-but-invalid overloads remain
excluded from default analysis and are not automatically replaced by recovery.

Monitors and exports retain execution/config provenance and historical failures.
Different configurations split curves/heatmaps; same-configuration segments may
share a curve while preserving per-observation provenance. Default analysis
excludes rejected/invalid data and retains saved exclusion point IDs.

## This 6/342 failure

Run `20261014T124726025034Z_T-e-sweep_17-250K_0.05-3V_200Hz` is in
`run_data/tempearure-lockin_261012/scan.sqlite`. The six accepted conditions remain
intact. Recovery begins at `condition-000006`: 11.7 K, 0.52 V RMS, 200 Hz. Its old
temperature drift and rejected attempt are retained. New stability criteria
apply only to the new execution.

The integration regression recreates the 18 x 19 grid using fake DLL/VISA,
injects failure at condition seven, changes stable range, and verifies 336 new
conditions, 342 unique accepted conditions, unchanged old sample hashes and
certified cleanup. Offline verification does not start a real scan.

The operator's actual six accepted samples also passed local validation of
their archived raw status, harmonic/settings readbacks, timing and original
hard bounds. Only a sanitized fixture was copied; private configuration,
addresses and paths were omitted and the remote database was not changed.
