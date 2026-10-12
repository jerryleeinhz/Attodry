# DC photoresponse work package

Updated: 2026-10-12. Branch: `module/NKT-photonics` (user-selected existing branch).

## Confirmed objective

Both photovoltage and photocurrent, implemented in stages. Use SuperK or a
CLD1010LP-controlled laser diode (approximately 760 nm; exact diode model unknown),
6221 DC current or no electrical excitation, 2182A/2400 voltage detection and
2400 current detection. Reuse attoDRY temperature/field and existing SMU assets.
The optical source and excitation source are separate semantic roles.

## Current delivery and boundaries

- Strict, module-specific `PhotoresponseConfig`; unused hardware tables need not
  be configured. Unknown active fields and incompatible bias/meter modes fail.
- A hardware-free dark-before/light/dark-after runner for three electrical modes,
  explicit NKT optical points, bias points and repeat indices.
- Public NKT `begin`, `prepare_point`, `set_emission`, `verify_point`, `finish`
  barriers reuse the existing controller. Fresh settings are verified before
  emission and around each electrical sample. Faulted sessions can only finish.
- `none` has no current-source object/commands and a null current setting.
  `dc_current` at 0 A remains a connected, enabled simulated source.
- JSON/JSONL preserves partial raw readings, window identity, acquisition times,
  settings, primary/cleanup errors and last-confirmed states. Accepted promotion
  waits until all conditions and cleanup succeed. Default analysis is accepted-only.
- Differential voltage/current uses actual sample midpoint times to interpolate
  the dark baseline. No resistance, photocurrent, power, environment, uncertainty
  or responsivity is invented from unavailable measurements.
- Injected-resource 6221/2182 query adapters have fake transcript tests only.
  They do not open resources, write settings, reset, trigger or consume error queues.
  A source snapshot cannot prove DC takeover is safe; a fetched 2182 reading is stale.

This is an **offline foundation**, not completion of P0 physical contract, real
Keithley output/trigger adapters, P2 hardware acquisition, target validation or
commissioning. CLD1010LP, joint SQLite/resume, environment integration, power-meter
integration, Notebook UI and real electrical source safety controllers remain planned.
The simulation deliberately has no production hardware entry point or auto-resume.

## Development sequence

| Stage | Work | Acceptance |
|---|---|---|
| P0 | Physical modes, wiring, exact manuals/identity, limits, dark switching, shutdown | Operator-confirmed contract; unknown values block hardware, not fake work |
| P1 | Strict config, NKT barriers, simulated electrical states, query adapters, faults | Offline foundation delivered; complete write/trigger adapters after contract audit |
| P2 | SuperK + 2182A, no excitation, repeated dark/light/dark voltage | Full raw trace, verified optical windows and cleanup; real commissioning required |
| P3 | 6221 bounded DC bias and current sweeps | Mode-specific preflight, compliance, bounded changes, final readback |
| P4 | 2400 voltage-bias/current measurement and independent voltmeter mode | Photocurrent and dark/light I-V; no conflicting source ownership |
| P5 | CLD1010LP exact SDK/diode/TEC contract and adapter | Same sampling interface; separate optical/electrical safety commissioning |
| P6 | Temperature, vector field, wavelength/intensity and optional gates | Stable actual environment, full 3 T path validation and joint cleanup |
| P7 | SQLite/checkpoints, notebook, export, daily operation | Reproducible accepted results; explicit restart preflight; audit-safe recovery |

Every hardware module follows offline -> target offline (`LK_setup`, `lyr`) ->
authorized real queries -> separately authorized status consumption if needed ->
smallest write -> standalone acceptance -> integration. These stages do not inherit
permissions from earlier SR830/temperature experiments.

## Reuse and file ownership

- `nkt_config.py`, `nkt_control.py`, `nkt_sdk.py`: existing NKT settings and safety;
  this change exposes point barriers without duplicating vendor commands.
- `photoresponse_config.py`: new strict mode/bias/sample contract.
- `photoresponse.py`: independent records, simulation and coordinator.
- `photoresponse_keithley.py`: conservative injected-resource query contracts.
- `photoresponse_cli.py`, `photoresponse_analysis.py`: offline entry and read-only results.
- `config/photoresponse_simulation.toml`: executable synthetic example. Station values
  remain in ignored `hardware.local.toml`; no laboratory limits inferred from demo values.
- Original acquisition/storage require six SR830 harmonic samples and retain their
  existing semantics. A future versioned photoresponse SQLite extension must preserve
  old readers, one accepted attempt per condition, raw-first writes and checkpoints.
- Temperature uses actual `measurement_temperature_k`; field writes remain separately
  uncommissioned. Three-SMU remains an active source-measure runner, not a voltmeter.

## Physical items still needed

1. Exact CLD1010LP diode model, pinout, wavelength, LD current/voltage limits, TEC
   thermistor/temperature limits, interlock and optical route.
2. Sample wiring and allowable V/I/power, measurement scale/impedance, grounding,
   2/4-wire and source-off behavior for every selected mode.
3. 6221/2182A/2400 interface, exact identity/firmware and actual available cabling.
   Keep station addresses only in local config and ignored raw audit records.
4. Dark switching: shutter or qualified emission cycling; settle/recovery duration,
   sample heating and required time resolution.
5. If A/W or V/W is needed: calibrated optical-power measurement plane and bandwidth
   correction. NKT percentages/monitor percentages and diode current are not sample watts.

## Adapter manual audit

- [6220/6221 reference manual Rev C](https://download.tek.com/manual/622x-901-01%20%28C%20-%20Oct%202008%29%28Ref%29.pdf):
  `OUTP:STAT?`, source range/compliance queries and `STAT:MEAS:COND?` (bit 3 compliance).
  `SOUR:CURR?` follows the documented general query syntax; verify this exact query
  during real read-only commissioning. The returned value is a setting, not measured I.
- [2182/2182A manual Rev B](https://download.tek.com/manual/2182A-900-01_May_2017.pdf):
  settings queries and `FETCh?`; `READ?` performs ABORt/INITiate/FETCh and is not
  included. Existing fetched values are never accepted as new formal samples.
  The finite overload sentinel `+9.9E37` must be rejected. Full exact IDN is operator
  supplied; do not infer that MODEL 2182 and MODEL 2182A identify the same instrument.
- [2400 manual](https://download.tek.com/manual/2400S-900-01_K-Sep2011_User.pdf):
  measure-only operation has source/output/compliance/range requirements; its generic
  gate cleanup must not be imported as a detector shutdown procedure.
- [CLD1010LP](https://www.thorlabs.com/thorproduct.cfm?partnumber=CLD1010LP):
  laser diode/temperature controller. SDK binding and mounted diode contract pending.

DC acquisition is the first goal. Delta/current reversal is not enabled automatically;
it changes the measured observable and can cancel current-even voltage components.

## Development and synchronization

Current implementation stays on the user-selected `module/NKT-photonics`. Preserve
pre-existing PM100D/PEM notes and figures outside this feature's commit. Shared files
have one writer; parallel agents perform read-only review. Update development stages
and current handoff after each completed feature. Push this branch only; do not merge
main or deploy to `LK_setup` implicitly. No remote environment was changed here.
