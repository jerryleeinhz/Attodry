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
