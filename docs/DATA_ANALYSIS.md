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
browse and plot the standalone frequency/excitation or combined
frequency×excitation JSON records under `run_data/commissioning`. The notebook is read-only unless its final
`SAVE_OUTPUTS` switch is explicitly enabled. Its first controls cell provides
record/sample filters, remote-directory frequency/excitation/combined selectors, and the
complete excitation-path resistance calibration.

The catalog filters record status as `completed`, `rejected`, `diagnostic`,
`other`, or `invalid`. Ordinary plotting defaults to `completed` only. Loading a
rejected or incomplete sweep requires the separate `include_rejected=True` audit
opt-in. Formal samples can be filtered as `clean`, `problem`, `unlocked`,
`overload`, or `instrument_error`; transition and cleanup payloads are excluded
from the plotted rows.

Set `DATA_DIRECTORY` once, click `Refresh records`, select a frequency record,
an excitation record, a frequency×amplitude record, or any combination, then click `Load selected records`. This works
when the kernel is running remotely through VSCode/SSH because it lists files
on the kernel computer rather than opening a desktop dialog. The visible `Only
completed records` checkbox defaults to selected; formal-sample status is a
multi-select UI and rejected records still require the separate audit checkbox.
Loading immediately populates the point selectors; then apply any exclusions and
run the plot cell. Rerun the formal-samples cell only after changing the
formal-sample filter. A frequency-only selection produces only frequency figures;
an excitation-only selection produces only current--voltage figures. A combined
record produces one current--Vxx/Vxy curve per actual frequency; the amplitude
axis is the SINE OUT readback converted through the recorded excitation path.

`clean` is the default automatic quality screen. It excludes formal samples
already marked `problem`, `unlocked`, `overload`, or `instrument_error` while
leaving every raw record untouched. The loaded-point selectors show the retained
scan points with coordinate, selected-row count, and available role/harmonic
channels. Select a suspect point, click `Apply point exclusions`, and rerun the
plot cell; clearing the selections and applying restores all automatically
retained points. The final optional export writes `selection_manifest.json`
alongside the CSV/PNG/PDF outputs so the selected files, filters, and manual
exclusions are reproducible. The manifest also records the exact
`PHASE_MINIMUM_AMPLITUDE_V` and
`PHASE_MAXIMUM_STANDARD_DEVIATION_DEG` values used for the exported figures, so
phase plots made from the same raw data under different trust thresholds remain
distinguishable and reproducible.
Both Python UTF-8 records and PowerShell UTF-16/BOM records are detected and
opened automatically.

The notebook creates six frequency figures and six current--voltage figures:
separate Vxx/Vxy figures for h1, h2, and h3. Each uses SR830 `R` (voltage
magnitude) on the left axis and measured phase on the right axis. Frequency is
logarithmic and its title states the calibrated RMS current. Current--voltage
plots use the same SINE OUT-derived RMS current on the x axis. A missing harmonic
is labeled as missing rather than interpolated or combined with another order.

For a combined record, `plot_multi_frequency_iv_curves` accepts `x_v`, `y_v`,
`amplitude_v`, or `phase_deg` and groups points by the actual SR830 frequency
readback. This keeps frequency-dependent I--V curves separate and makes any
frequency quantization visible in the legend.

### Harmonic current-power-law fitting

When an excitation-amplitude record is loaded, the commissioning notebook also
fits every available XX/XY and h1/h2/h3 channel independently. The current is
calculated from the recorded path snapshot:

```text
I_RMS = V_SINE_OUT,RMS / (R_external + 50 ohm + R_device)
```

For a channel with harmonic order `n`, the raw-magnitude audit compares the
free-exponent model and the expected fixed-order model in log-log space:

```text
log(R) = log(A) + p log(I)       (p is fitted)
log(R) = log(A) + n log(I)       (n is fixed to 1, 2, or 3)
```

This raw `R` comparison has no additive background term. Its direct exponent evidence is the fitted `p` and its approximate confidence
interval. `delta_aicc_fixed_minus_free` is `AICc_fixed - AICc_free`; values at
or below 2 mean that the fixed-order model is competitive, values above 6 are
evidence for the free-exponent model, and values above 10 are strong evidence.
The result also reports fixed/free R-squared, relative RMSE, current span in
decades, replicate-based SNR, and the phase slope in degrees per current decade.
R-squared is contextual only and is never used as the sole decision rule.

For a phase-unstable channel, the notebook also performs an independent
phase-blind scalar fit directly on the measured magnitude. It uses the same
retained current points and replicate amplitude SEM weights, but never reads
phase, X, or Y. The models are `R = b + A (I / I_ref)^n` and
`R = b + A (I / I_ref)^p`, with the no-background pair setting `b=0`.
The background pair fits `b` and `A` with non-negative constraints.
`scalar_background_mode = "auto"` selects the fixed-order background
treatment by corrected AIC, while `"none"` and `"with_offset"` force a
choice. `scalar_R_verdict` is therefore the result to use when phase is not
trusted. It is separate from `amplitude_verdict` (the log-space fit) and
from the complex X/Y result. The exported `scalar_phase_ignored = true` flag
makes the phase-blind decision explicit. AICc values are only comparable
within one residual space; do not rank scalar, log, and complex AICc against
each other.

The offset-aware physical-order result instead fits the complex lock-in vector
`Z = X + iY`. It calculates all four models below, where `B` is a complex
background (independent amplitude and phase) and `I_ref` is the geometric mean
of the retained current range:

```text
Z = C (I / I_ref)^n
Z = C (I / I_ref)^p
Z = B + C (I / I_ref)^n
Z = B + C (I / I_ref)^p
```

This is more appropriate than fitting a scalar `R = A·I^n + b`: the background
and response can have different phases, so in general
`|B + C·I^n|` is not equal to `b + A·I^n`. In `complex_background_mode = "auto"`,
corrected AIC first chooses the lower-AIC fixed-order model with or without `B`,
then compares it with the free-exponent model using the same background choice.
`"none"` forces the no-background pair and `"with_offset"` forces the
background pair. The free complex exponent is profiled over 0.05–6.0; a result
at a search boundary has no exponent confidence interval and remains
`ambiguous` rather than being accepted.

The notebook keeps the thresholds in its first code cell inside the editable
`SCALING_RULES` block. The defaults are:

| Rule | Default | Meaning |
| --- | ---: | --- |
| `confidence_level` | 0.95 | Confidence level for the exponent interval |
| `minimum_points` | 6 | Minimum current points used by a fit |
| `minimum_current_decades` | 1.0 | Required `log10(Imax/Imin)` span |
| `minimum_snr` | 3.0 | Exclude a point only when replicate SEM is available and SNR is lower |
| `max_exponent_ci_width` | 0.5 | Maximum allowed width of the exponent interval |
| `max_delta_aicc_consistent` | 2.0 | Fixed-order model remains competitive |
| `min_delta_aicc_inconsistent` | 6.0 | Free-exponent model is clearly preferred |
| `max_relative_rmse` | 0.10 | Maximum relative error of the fixed-order fit |
| `max_phase_slope_deg_per_decade` | 5.0 | Phase-stability limit for the complex-response verdict |
| `max_phase_span_deg` | 10.0 | Total unwrapped phase-span limit |
| `scalar_background_mode` | `"auto"` | Choose `"auto"`, force `b=0` with `"none"`, or fit non-negative `b` with `"with_offset"` |
| `complex_background_mode` | `"auto"` | Choose `"auto"`, force no background with `"none"`, or force a complex background with `"with_offset"` |
| `complex_free_exponent_min` / `max` | 0.05 / 6.0 | Visible search interval for the free complex exponent |

Four conclusions are intentionally returned. `amplitude_verdict` asks whether
the raw magnitude follows `I^n` in log space; `scalar_R_verdict` asks the same
question in voltage-amplitude space while ignoring phase; `complex_response_verdict`
is the raw-phase stability audit; and `complex_power_law_verdict` is the
background-aware complex result to use when deciding the physical harmonic
order. A raw phase can rotate with current solely because `B` and `C·I^n` have
different phases, while either magnitude or complex fit remains consistent.
`scalar_models` and `complex_models` record all fitted parameters, AICc and
residuals. The notebook returns `insufficient_data` when point count or current
range is too small, `ambiguous` when indicators disagree, and does not silently
remove low-SNR or manually excluded points. Optional numerical thresholds can
be set to `None` in the notebook to disable that individual criterion.

The comparison summary also reports leave-one-current-point-out relative RMSE
for the log, scalar-R, and complex fixed-order curves
(`*_leave_one_out_relative_rmse`). These values are computed in the common
measured-amplitude space, so they are useful for choosing among methods; they
are descriptive cross-validation errors, not additional safety gates.

The optional export records the exact `SCALING_RULES` values and every fit
result in `selection_manifest.json`, alongside one PNG/PDF fit figure per
available channel. Each fit figure includes a formula panel with the numerical
coefficients, exponent, AICc, and relative RMSE for every log/scalar/complex
model; the plotted curves are identified in the legend. This makes results
produced with different judgment rules reproducible and distinguishable.

The daily source of truth for the variable path values is the ignored
`config/hardware.local.toml` `[lockin_sweep]` table:
`external_series_resistance_ohm` and `approximate_device_resistance_ohm`.
The known SR830 output resistance is a fixed 50 Ω. Each sweep archives all three
components and their total in `measurement_config.excitation_path`, so analysis
does **not** reread the computer's current local TOML or require a duplicate
notebook constant. It uses
`I_rms = V_sine_out_rms / total_path_resistance_ohm`, taking a recorded SINE OUT
readback when available and otherwise the archived setpoint.

For normal daily JSON, the notebooks and plotting API use that per-record
snapshot by default. A selection containing different recorded path snapshots is
rejected rather than silently mixing current calibrations. Older JSON that lacks
the snapshot requires the visible `EXCITATION_PATH_OVERRIDE` object; this is an
explicit legacy-only analysis override and applies to every selected file. It
does not write an instrument and cannot replace the safety review required before
the next acquisition.

Phase uses circular rather than arithmetic statistics across the -180/180-degree
wrap. The commissioning notebook exposes two display-only quality controls:
`PHASE_MINIMUM_AMPLITUDE_V` (default 1 µVrms) and
`PHASE_MAXIMUM_STANDARD_DEVIATION_DEG` (default 5 degrees). A plotted phase point
must satisfy both controls; the remaining contiguous qualified segments are
unwrapped across the ±180-degree boundary, but never across an omitted point.
This prevents a low-amplitude or internally unstable phase from looking like a
physical discontinuity while leaving every raw phase value available for audit.
Set the amplitude control to `0.0` and the standard-deviation control to `None`
to display all raw phase points. CSV, PNG, and PDF export is disabled by default
and writes only beneath `analysis_output/sr830_commissioning` when explicitly
enabled.

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
