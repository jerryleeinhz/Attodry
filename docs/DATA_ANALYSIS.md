# Read-only transport analysis and publication plots

The analysis path opens SQLite with URI `mode=ro` and `PRAGMA query_only`. It
uses accepted attempts by default. `--include-rejected` exists only for audit;
the publication generator filters nonaccepted rows again and reports their count
in its manifest.

## Install

```powershell
python -m pip install -e ".[analysis]"
```

The project pins matplotlib 3.10.9 for reproducible offline wheelhouses. Analysis
does not import attoDRY, SR830, SMU, PPMS, MultiPyVu, ETO, or rotator control.

## Standalone SR830 commissioning sweeps

Open `notebooks/sr830_commissioning_sweeps.ipynb` in the `lyr` environment to
browse and plot the standalone frequency/excitation JSON records under
`run_data/commissioning`. The notebook is read-only unless its final
`SAVE_OUTPUTS` switch is explicitly enabled. Its first controls cell provides
record/sample filters, the optional native Windows Browse dialog, and the
complete excitation-path resistance calibration.

The catalog filters record status as `completed`, `rejected`, `diagnostic`,
`other`, or `invalid`. Ordinary plotting defaults to `completed` only. Loading a
rejected or incomplete sweep requires the separate `include_rejected=True` audit
opt-in. Formal samples can be filtered as `clean`, `problem`, `unlocked`,
`overload`, or `instrument_error`; transition and cleanup payloads are excluded
from the plotted rows.

Set `OPEN_BROWSER=True` in the Browse cell to open the native Windows file
chooser and load a JSON/JSONL file directly. If the notebook kernel is running
without access to the Windows desktop, set `selected_path = Path(...)` instead.
Directory discovery remains available in either case.
Both Python UTF-8 records and PowerShell UTF-16/BOM records are detected and
opened automatically.

The notebook creates six frequency figures and six current--voltage figures:
separate Vxx/Vxy figures for h1, h2, and h3. Each uses SR830 `R` (voltage
magnitude) on the left axis and measured phase on the right axis. Frequency is
logarithmic and its title states the calibrated RMS current. Current--voltage
plots use the same SINE OUT-derived RMS current on the x axis. A missing harmonic
is labeled as missing rather than interpolated or combined with another order.

Change `EXTERNAL_SERIES_RESISTANCE_OHM`,
`SR830_OUTPUT_RESISTANCE_OHM`, and `APPROXIMATE_DEVICE_RESISTANCE_OHM` in that
first controls cell when the physical path changes. Their current defaults are
100000 Ω, 50 Ω, and 500 Ω, respectively, for a total of 100550 Ω. The analysis
uses `I_rms = V_sine_out_rms / total_path_resistance_ohm`; it uses a stored
SINE OUT readback when available and only falls back to the recorded setpoint for
older frequency records that lack a readback. These constants are analysis-only:
changing them sends no instrument command and must not be mistaken for a new
hardware safety authorization.

Phase uses circular rather than arithmetic statistics across the -180/180-degree
wrap. CSV, PNG, and PDF export is disabled by default and writes only beneath
`analysis_output/sr830_commissioning` when explicitly enabled.

## XY-only frequency and amplitude sweeps

Use `notebooks/sr830_xy_sweeps.ipynb` to plot only XY from the completed
frequency and excitation-amplitude JSON records. `xy_sweep_analysis` discards
XX at the loader boundary, retains the completed/clean defaults and rejected
audit opt-in, and labels every figure with the single harmonic order represented
by that sweep (for example `XY · h1`). The notebook keeps both scan types and
plots XY X, Y, R, and phase for each.

## CSV and a single trace

```powershell
attodry-analyze --database PATH --run-id RUN_ID --csv analysis_output/run.csv

attodry-analyze --database PATH --run-id RUN_ID `
  --plot analysis_output/field.png --x-axis field_magnitude_t `
  --role xx --harmonic 1
```

Resistance is never calculated unless a current is explicitly supplied. For a
single trace, use `--current-a-rms`. For the publication suite, provide the
complete excitation-path resistance described below.

## Publication suite

```powershell
attodry-analyze --database PATH --run-id RUN_ID `
  --publication-dir analysis_output/RUN_ID `
  --total-series-resistance-ohm 4000000 `
  --format png --format pdf
```

`total-series-resistance-ohm` must include the complete known path used to turn
the programmed SR830 RMS amplitude into estimated RMS current. It is not an
independent current measurement. Omitting it causes resistance/current-dependent
products to be recorded as `skipped`, not guessed.

Every condition carries an explicit `scan_id`. Two-dimensional maps and sweep
curves are grouped within that identity and fixed conditions; the software does
not combine unrelated scans merely because their axes happen to overlap.

Outputs:

- `analysis_manifest.json`: generated/skipped status, reasons, input counts,
  selected fixed-condition groups, calibration, and limitations;
- `analysis_records.csv`: accepted long-form rows plus optional estimated current
  and signed resistance;
- `fit_summary.csv`: harmonic-scaling slope, intercept, and R²;
- supported PNG/PDF figures for current, harmonics, frequency, temperature,
  vector field, angle, γ, T–|B|, gate resistance, gate leakage, and n–D.

Nernst maps, Hall coefficient, scattering rate, sample geometry corrections, and
microscopic mechanism claims remain explicitly skipped because the recorded
voltages do not prove them.

## Gate n–D calibration

Copy `config/gate_calibration.example.toml` to the ignored
`config/gate_calibration.local.toml` and replace every `CHANGE_ME` from device
geometry or an independent calibration:

```text
n = [Ct(Vt - Vt0) + Cb(Vb - Vb0)] / e + n0
D = [Cb(Vb - Vb0) - Ct(Vt - Vt0)] / 2 + D0
```

Then add:

```powershell
--gate-calibration config/gate_calibration.local.toml
```

The software never names a resistance feature a zero-electric-field line by
itself. Use a user/calibration supplied relation and document its sign convention.
