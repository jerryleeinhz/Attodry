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

### Relative complex frequency-response calibration

At the end of `notebooks/sr830_commissioning_sweeps.ipynb`, first load one
completed frequency or f × e file in the normal browser and apply any point
exclusions. Click **Refresh loaded runs**, select the calibration file, choose
`Vxx h1` or `Vxy h1`, and click **Fit and plot h1 response**. `Ref Hz` must be a
measured requested frequency; 0 chooses the lowest. The figure plots relative
gain `|Q|`, relative unwrapped phase `arg Q`, and complex residual RMS voltage.
Frequency-only data use `b(f)=mean[(X+iY)/U]` and cannot identify an intercept.
For f × e, each frequency fits `X+iY=b(f)U+a(f)` with complex intercept and
at least three distinct SINE OUT readbacks. `U` is the recorded *actual*
readback; repeated formal samples are averaged within point before fitting.
Runs lacking SINE OUT readback, or with h1 frequency readbacks differing from
their requested frequencies by more than 1% (minimum 0.1 Hz), are rejected
for calibration rather than fitted under a mislabeled frequency.
`Q(f)=b(f)/b(f0)` uses the selected measured reference. These are empirical
transfer ratios, not automatically device impedance or proof of zero phase.

To apply h2 calibration, choose an excitation level, h2 role and **H2 model**.
`Excitation squared` applies `V2/Q(f)^2` only if the h1 response represents the
excitation path and the h2 readout is flat. `Same-channel readout` applies
`V2/[Q(2f)/Q(2f0)]` only if the h1 and h2 detector channel is the same and the
input path is flat. A general combined input/readout response cannot be inferred
from h1 alone. Complex interpolation between calibration frequencies uses log
frequency, log magnitude and locally unwrapped phase. It does not extrapolate:
uncovered h2 rows remain raw with a reason in `h2_derived.csv`. Corrected
phase is relative to the chosen h1 baseline, not an absolute phase-zero claim.

An optional complex LCR anchor (R + iX ohm) must be measured at the selected
reference frequency and at the same electrical reference plane. With a
voltage drive and h1 proportional to current, choose `Z=Z0/Q`; only if h1 is
proportional to impedance choose `Z=Z0 Q`. This yields a conditional *total*
impedance estimate, not separate sample, contact, wiring and amplifier terms.
Enter the anchor, select the assumption, and calculate. **Export derived
calibration** explicitly writes a new timestamped folder under ignored
`analysis_output/sr830_commissioning` with response and h2 figures,
`calibration_manifest.json`, `frequency_response.csv`, and available derived
CSVs; it never edits raw
acquisition JSON. Rebuild the response after changing a source run or filters.

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

Set `DATA_DIRECTORY` once, click `Refresh records`, and tick the checkbox to the
right of each wanted file under the three separate categories: `Frequency`,
`Excitation`, and `f × e`. Each category supports multiple files. Click
`Load selected records` to analyze those selections together. Labels sit above
the controls, long filenames wrap, and catalog refresh preserves checked files
that still exist in the filtered catalog. This works
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
alongside the CSV/PNG/PDF/SVG outputs so the selected files, filters, and manual
exclusions are reproducible. The manifest also records the exact
`PHASE_MINIMUM_AMPLITUDE_V` and
`PHASE_MAXIMUM_STANDARD_DEVIATION_DEG` values used for the exported figures, so
phase plots made from the same raw data under different trust thresholds remain
distinguishable and reproducible.
Both Python UTF-8 records and PowerShell UTF-16/BOM records are detected and
opened automatically.

For `f × e`, two separate exclusion lists show the distinct requested frequencies
and SINE OUT excitation values across all selected combined records. Excluding a
frequency removes every excitation at that frequency; excluding an excitation
removes that amplitude at every frequency. Either match removes the sample, in
every selected combined file, across all roles and harmonics. Requested coordinates
are used so small readback variations do not fragment a row or column. Apply the
exclusions and rerun the figure cell; clearing and reapplying restores the loaded
data. Loading a new file selection resets exclusions. Standalone frequency and
excitation exclusions still operate on individual points identified by source
file and point index. The export manifest records the two combined exclusion lists.

The notebook creates six frequency figures and six current--voltage figures:
separate Vxx/Vxy figures for h1, h2, and h3. Each uses aligned, shared-x panels
for SR830 `R` (voltage magnitude) and measured phase; it does not use potentially
misleading dual y axes. Frequency is logarithmic and its title states the
calibrated RMS current. Current--voltage plots use the same SINE OUT-derived RMS
current on the x axis. A missing harmonic is labeled as missing rather than
interpolated or combined with another order.

All commissioning and temperature figures use a scoped publication style that
does not alter the user's global Matplotlib settings. The style uses editable
Type 42 PDF fonts, editable SVG text, an opaque white background, restrained
major grids, inward ticks, and redundant color/marker/line-style encodings.
Error-bar legends identify ordinary or circular sample standard deviation. The
style is a general manuscript starting point, not a claim of compliance with a
specific journal. Optional export writes 600 dpi PNG plus PDF and SVG vectors.
Temperature-stacked I--V curves use a bright warm `plasma` sequence, while
multi-frequency I--V curves use a distinct cool `viridis` sequence. Both retain
marker and line-style redundancy, so series identity never depends on color alone.

The final Notebook section provides a separate condensed report figure without
changing the channel-by-channel analysis. `REPORT_AMPLITUDE_CHANNELS` accepts any
available `("xx" | "xy", 1 | 2 | 3)` combinations. `REPORT_PHASE_MODE="right"`
adds the selected `REPORT_PHASE_CHANNELS` to a separately labelled right y axis;
use `"none"` with an empty phase tuple for an amplitude-only figure. Each amplitude
channel displays only fit-qualified points and one `scalar_selected_free_model`
curve over the measured current range. Its right-side legend gives the fitted
`R(I) = b + A(I/Iref)^p` equation, exponent confidence interval, R-squared, and
relative RMSE. Optional PNG/PDF/SVG export also writes a JSON report manifest.

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

The first Notebook cell also exposes `SCALING_PLOT_METHODS`. Its default
`("log", "scalar", "complex")` draws all three views for model comparison.
After choosing a method, use a one-item tuple such as `("scalar",)`, `("log",)`,
or `("complex",)`. `scalar` means the phase-blind R fit in linear voltage
coordinates; it does not force the fitted exponent to one. This switch changes
only the curves, equations, verdicts, and residuals shown in the figure. All
three fits are still computed and retained in the export manifest.

The optional export records the exact `SCALING_RULES` values and every fit
result in `selection_manifest.json`, plus the active
`harmonic_scaling_plot_methods`, alongside one PNG/PDF/SVG fit figure per
available channel. Fit figures show observed means with sample-SD error bars,
the selected fitted curves, and their aligned relative residuals. The right-side
legend gives each drawn fit its numerically substituted, current-normalized equation,
exponent (and available interval), R², relative RMSE, and AICc or log-model
ΔAICc; it states only the selected methods' verdicts in its title. Scalar equations use
`R(I)`, while complex equations preserve phase-bearing `Z(I)=X(I)+iY(I)`
coefficients. Full-precision values remain in the manifest, and the Notebook
does not print raw result tables or a separate formula panel. This makes results
produced with different judgment rules reproducible and distinguishable.

The daily source of truth for the variable path values is the ignored
`config/hardware.local.toml` `[lockin_sweep]` table:
`external_series_resistance_ohm` and `approximate_device_resistance_ohm`.
The known SR830 output resistance is a fixed 50 Ω. New sweeps store stable
resolved settings once in a sibling `measurement-profile-<sha256>.json` file and
put a relative `measurement_profile_ref` in each run. The content-addressed ID
is verified when reading; the profile contains all three path components and
their total. Analysis does **not** reread the computer's current local TOML or
require a duplicate notebook constant. It uses
`I_rms = V_sine_out_rms / total_path_resistance_ohm`, taking a recorded SINE OUT
readback when available and otherwise the archived setpoint.
The path includes the same approximate device resistance used by excitation
preflight's nominal current and device-voltage estimates; it is an estimate, not
an independent current measurement or worst-case device model.

For normal daily JSON, the notebooks and plotting API resolve the shared
profile's `excitation_path`; per-run requested points and timing remain in
`run_configuration`. Derived safety estimates remain with the run, while
resistance and confirmed limits are stored only in the profile. Keep profile
sidecars with run JSON files when copying/moving a data directory. Missing,
changed, or hash-mismatched profiles fail closed. Older JSON with inline
`measurement_config.excitation_path` remains supported. A selection containing
different recorded path snapshots is rejected rather than silently mixing
current calibrations. Older JSON that lacks the snapshot requires the visible
`EXCITATION_PATH_OVERRIDE` object; this is an explicit legacy-only analysis
override and applies to every selected file. It does not write an instrument and
cannot replace the safety review required before the next acquisition.

Phase uses circular rather than arithmetic statistics across the -180/180-degree
wrap. The commissioning notebook exposes two display-only quality controls:
`PHASE_MINIMUM_AMPLITUDE_V` (default 1 µVrms) and
`PHASE_MAXIMUM_STANDARD_DEVIATION_DEG` (default 5 degrees). A plotted phase point
must satisfy both controls; the remaining contiguous qualified segments are
unwrapped across the ±180-degree boundary, but never across an omitted point.
This prevents a low-amplitude or internally unstable phase from looking like a
physical discontinuity while leaving every raw phase value available for audit.
Set the amplitude control to `0.0` and the standard-deviation control to `None`
to display all raw phase points. CSV, PNG, PDF, and SVG export is disabled by default
and writes only beneath `analysis_output/sr830_commissioning` when explicitly
enabled.

The general figure workflow was informed by Timothy Kassis, Vinayak Agarwal,
Yuhuan He, Darshil Patel, and Aubrey M. Brueckner (2026),
[Scientific Agent Skills: A Library of Procedural Knowledge for Research
Agents](https://doi.org/10.48550/arXiv.2609.00065).

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
