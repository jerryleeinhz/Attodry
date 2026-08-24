# Dual SR830 standalone device test

This procedure tests a device with the two SR830 units only. It does not use or
import attoDRY, magnet, gate-SMU, PPMS, ETO, or rotator control.

The command definitions and instrument limits below are taken from the
[official SRS SR830 manual](https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf).

## Stop conditions

Stop before connecting the device if any of these are unknown:

- the maximum allowed device RMS current and voltage;
- whether the voltage probes are wired for differential A-B input;
- the series resistance or attenuation in the excitation path;
- which VISA address belongs to `lockin_xx` and which belongs to `lockin_xy`;
- whether `lockin_xy` SINE OUT is physically disconnected.

The SR830 SINE OUT cannot be switched off. Its minimum setting is 4 mVrms, and
it remains active in external-reference mode. Software must never treat the
minimum setting as an electrical disconnect.

## Confirmed roles and cabling

| Semantic role | Reference | Signal input | Other output wiring |
| --- | --- | --- | --- |
| `lockin_xx` | Internal, 17.777 Hz initially | Vxx, preferably differential A-B | SINE OUT drives the current path; rear TTL SYNC OUT drives `lockin_xy` reference |
| `lockin_xy` | External TTL, rising edge | Vxy, preferably differential A-B | SINE OUT physically disconnected |

Both lock-ins start at first-harmonic detection. Do not connect either CH1/CH2
analog output to the device excitation path.

For differential A-B voltage measurement, route the A and B cables together to
minimize loop area. The SRS manual specifies 10 MOhm + 25 pF voltage inputs,
no more than 50 V at either input, and no more than 1 V on the shields. These
instrument ratings do not override the usually much lower device limits.

## 1. Calculate the minimum-excitation current

Before connecting SINE OUT, calculate the approximate current:

```text
I_rms = 0.004 V / (50 ohm + R_series + R_device)
```

The 50 ohm term is the nominal SR830 output impedance. Include all intentional
attenuation and other series impedance. Do not use a 50 ohm termination unless
it is deliberately part of the circuit; a 50 ohm load halves the programmed
high-impedance output amplitude.

Require `I_rms` to be below the user-confirmed device current limit with a
deliberate safety margin. If 4 mVrms is too high, add a passive attenuator or a
larger series resistor before proceeding.

## 2. Initial front-panel settings

With the device excitation still disconnected:

1. Set both SINE OUT amplitudes to 4 mVrms.
2. Set `lockin_xx` to Internal, 17.777 Hz, harmonic 1.
3. Connect `lockin_xx` rear TTL SYNC OUT to the `lockin_xy` reference input.
4. Set `lockin_xy` to External, TTL rising, harmonic 1.
5. Confirm that `lockin_xy` does not show UNLOCK.
6. For a first voltage-input test, a conservative starting point is A-B input,
   Float shield, AC coupling, line filters out, 100 mV sensitivity, Normal
   reserve, 1 s time constant, and 24 dB/oct roll-off.

The corresponding SR830 setting codes are `ISRC 1`, `IGND 0`, `ICPL 0`,
`ILIN 0`, `SENS 23`, `RMOD 1`, `OFLT 10`, and `OFSL 3`. The standalone tool
does not write these device-dependent input settings; set and verify them on
the front panels. Start with a wide sensitivity and reduce it only after the
absence of overload is confirmed. Do not start with Auto Gain or Auto Phase.

## 3. Install and identify the VISA addresses

Install the package and the hardware-only PyVISA extra in a Python 3.11 virtual
environment. A system VISA implementation such as NI-VISA must already be
installed for the user's adapter.

```powershell
python -m pip install -e ".[hardware]"
python -m attodry_control.lockin_test discover
```

On each SR830, press **Setup** repeatedly until the Reference display shows
`ADDRESS`; the displayed number is the GPIB device address. Turn the knob to
change it. The official manual describes 1-30 as usable GPIB device addresses;
assign a different address to each physical SR830. Press Phase, Freq, Ampl,
Harm#, or Aux Out to leave Setup. Also use the Setup `GPIB/RS232` page to make
sure the response interface is GPIB.

In a VISA resource such as `GPIB0::8::INSTR`, `GPIB0` is the computer's first
GPIB controller and `8` is the address displayed by the SR830.

`discover` lists resources but does not open an instrument. Match each address
to a physical SR830 by temporarily powering or disconnecting one interface at a
time, or by comparing serial numbers from `*IDN?`. Record the semantic mapping;
do not call the units only "#1" and "#2" in configuration or data.

Create the ignored station-local configuration once:

```powershell
Copy-Item config\hardware.example.toml config\hardware.local.toml
notepad config\hardware.local.toml
```

For this standalone step, edit `[visa].timeout_ms`, both `[lockin_*].address`
values, and the shared `frequency_hz`. Other `CHANGE_ME` values may remain until
their hardware stages, but the full integrated hardware path will reject them.

## 4. Query both instruments without setting writes

Use the station-local configuration:

```powershell
python -m attodry_control.lockin_test diagnose `
  --config config\hardware.local.toml
```

This path sends query commands only. It reads IDN, reference and input settings,
sensitivity, time constant, filter slope, and a coherent per-instrument
`SNAP? 1,2,3,4,9` of X, Y, R, phase, and reference frequency.

The JSON field `safety_status_complete` is `false` in this mode and `limitations`
states that unlock, overload, and instrument-error acceptance is still unknown.
Two different VISA strings that return the same full SR830 identity are reported
as a problem, because they may be aliases for one physical instrument.

To include overload, unlock, and error status, add
`--consume-status-latches`. `LIAS?` and `ERRS?` clear their respective latched
status bits when read, so the flag is intentionally explicit. Only samples taken
with this flag can have `safety_status_complete: true`.

## 5. Configure only the confirmed reference roles

Do this only with `lockin_xy` SINE OUT physically disconnected and the device
excitation path either disconnected or confirmed safe at 4 mVrms:

```powershell
python -m attodry_control.lockin_test configure-minimum `
  --config config\hardware.local.toml `
  --authorize-writes `
  --confirm-xy-sine-disconnected
```

The tool verifies both IDNs before writing. It then sets both SINE OUT levels to
4 mVrms, configures `lockin_xx` as internal/first-harmonic, configures
`lockin_xy` as external-TTL-rising/first-harmonic, and verifies readback. On a
caught failure after writes begin, it separately retries the 4 mVrms setting on
both units. A communication failure is reported as failure, never as a verified
safe output state. If both addresses return the same full identity, the command
stops before the first write.

## 6. Connect and measure the device

1. Connect `lockin_xx` SINE OUT through the calculated series impedance to the
   device current terminals.
2. Connect Vxx to the `lockin_xx` A-B centers and Vxy to the `lockin_xy` A-B
   centers. Verify the station-specific shield scheme before connecting shields.
3. Wait at least five time constants after connecting or changing a setting.
4. Confirm no input/reserve, filter, or output overload and no XY reference
   unlock.
5. Reduce each sensitivity one step at a time only if it remains above the
   observed signal with margin.
6. Preserve X and Y as measured. Do not use Auto Phase during the first test to
   redefine sign or hide a wiring phase shift.

Collect one JSON line per sample, including status:

```powershell
python -m attodry_control.lockin_test diagnose `
  --config config\hardware.local.toml `
  --samples 60 `
  --interval-s 1 `
  --consume-status-latches |
  Tee-Object -FilePath "dual_sr830_test.jsonl"
```

The two instruments are queried sequentially; each instrument's X/Y/R/phase/
frequency values are internally coherent, but the pair is not simultaneous.
This limitation must be retained in the test record.

Pair-frequency comparisons allow one 1 mHz SR830 readback step plus floating-
point margin. A 2 mHz difference still fails, and any `LIAS?` reference-unlock
bit remains an unconditional failure.

`--xx-address`, `--xy-address`, `--timeout-ms`, and `--frequency-hz` remain
available as explicit one-command overrides; they take precedence over the TOML
values and do not edit the local file.

## 7. Validate ordered first/second/third-harmonic reads

This is a separate write-authorized stage. With the confirmed device limits,
`lockin_xy` SINE OUT still physically disconnected, and both inputs free of
overload, run:

```powershell
python -m attodry_control.lockin_test measure-harmonics `
  --config config\hardware.local.toml `
  --settle-s 2 `
  --authorize-writes `
  --confirm-xy-sine-disconnected |
  Tee-Object -FilePath "dual_sr830_harmonics.json"
```

The command consumes status latches while verifying the existing 4 mVrms
reference-role configuration, then writes only harmonic settings. It does not
rewrite the reference mode or change frequency, sensitivity, time constant,
input mode, shield grounding, or filter slope. For each harmonic it writes both
instruments before either SNAP read, waits for the requested settling interval,
consumes `LIAS?`/`ERRS?`, and retains the six ordered xx/xy readings. It restores
both instruments to harmonic 1 after success. On failure or interruption it
attempts harmonic-1 restoration and 4 mVrms minimum-output cleanup, but the
operator must still confirm both front panels before disconnecting the device.

## 8. Frequency and excitation sweeps

These are separate configuration-controlled device-only tests. The frequency scan uses
4 mVrms and configured h1/h2/h3 detection at ten logarithmically spaced points
from 17.777 Hz through 100 kHz. It writes only the internal frequency on
`lockin_xx`; `lockin_xy` follows the external TTL reference. After each actual
frequency change, the tool waits 1.5 seconds, records and clears transition
status, waits another 1.5 seconds, then retains three sequential xx/xy samples
0.3 seconds apart. Transition-period unlock, frequency-range-change, and overload
latches are retained as discarded transition data rather than accepted samples.
An instrument error, unexpected time-constant change, or XX internal-reference
unlock during transition fails immediately. Any unlock, overload, or error after
the second settling interval in the formal sample window also fails the scan.

Before formal sampling, each role follows the range policy declared in its own
`[lockin_xx]` or `[lockin_xy]` table. The daily defaults are fixed XX code 21
(20 mV) and fixed XY code 17 (1 mV); a role uses `SENS` only when its preflight
readback differs. An operator may instead explicitly select `bounded_auto` for
either role, with that role's complete, restricted `autorange_*` policy. Every
range decision, write, readback, transition status, and final formal range is
audited. This prevents the genuine output overload seen when Vxx reached about
1.09 mV at 50 Hz on the 1 mV range without silently enabling XY auto-ranging.
When any role is automatic, each sweep point first retains a sequential h1
preprobe for both instruments, carries its state through the continuous scan,
and freezes the resulting range before formal h1/h2/h3 samples. No range
transition changes the 4 mVrms source. Cleanup returns to 17.777 Hz while the
selected range remains active, then restores each original range only if that
role was changed and verifies both readbacks.

Sweep frequency requests and SR830 `FREQ?`/`SNAP?` readbacks are all recorded;
numeric XX/XY differences and display quantization do not reject a sweep. The
tool still rejects non-finite or out-of-range (0.001--102000 Hz) observations,
and any unlock, overload, instrument error, or unsafe transition remains a
fail-closed condition. `actual_frequency_hz` is the XX readback used by analysis;
harmonic eligibility uses the higher of requested and both actual frequencies.

Both sweep commands clear pending VISA responses before their first query and
again before cleanup after an abort. After a hard interruption, the transport
layer can be cleared manually without changing settings:

```powershell
python -m attodry_control.lockin_test recover-interface
```

```powershell
python -m attodry_control.lockin_test sweep-frequency
```

The excitation scan is a source-voltage scan with a nominal current calculated
from the complete configured series path. The confirmed path is a 100 kohm
external resistor, the SR830 50 ohm output resistance, and an approximate
500 ohm device. Run:

```powershell
python -m attodry_control.lockin_test sweep-excitation
```

Both commands require the strict `[lockin_sweep]`, `[lockin_xx]`, and
`[lockin_xy]` tables in `config\hardware.local.toml`. The checked-in example
documents every field:

- `frequency_points_hz`: ten logarithmically spaced fundamentals from 17.777 Hz
  to 100 kHz.
- `excitation_points_v_rms`: 4, 6, 10, 16, 26, 40, 64, 100, 160, 252, and
  400 mVrms at the fixed 17.777 Hz fundamental.
- `frequency_xx_harmonics` / `frequency_xy_harmonics`: formal XX/XY harmonic
  orders for the frequency sweep. Each is an ascending subset of `[1, 2, 3]`; an
  empty list excludes that role from formal curves.
- `excitation_xx_harmonics` / `excitation_xy_harmonics`: the corresponding
  excitation-sweep selection. For example XX `[1, 3]` plus XY `[2]` is valid.
  Both SR830s are still read at every selected harmonic and either safety failure
  rejects the run.
- `skip_unsupported_harmonics = true`: at high fundamentals, retain supported
  orders and record h2/h3 as skipped when their detection frequency would exceed
  the SR830 102 kHz limit.
- `lockin_xx.sensitivity_mode` / `lockin_xy.sensitivity_mode`: both defaults
  to `fixed`, with XX 20 mV (code 21) and XY 1 mV (code 17). Change only the
  chosen role to `bounded_auto` when automatic judgment is wanted; it is opt-in,
  never an implicit sweep default.
- `sensitivity_full_scale_v`: the fixed target, or the minimum range in that
  role's automatic policy. A fixed range is verified and written only when
  preflight differs.
 - `autorange_min_full_scale_v`, `autorange_max_full_scale_v`,
  `autorange_target_occupancy = 0.85`, and `autorange_stable_samples = 2` are
  required only for a role explicitly set to `bounded_auto`. The selected pair
  must match a ladder in `config/lockin_safety.toml`; there is no user-facing
  total-step field. Each adjacent transition is fail-closed and recorded, and
  widening can repeat within one point when the new range is still above the
  target occupancy.
- `run_name` and `note`: required per-run audit metadata. The nonempty, safe
  filename label `run_name` is included in the JSON name; the nonempty `note`
  remains in the JSON record.
- `settle_s`, `samples_per_point`, and `sample_interval_s`: transition settling,
  number of formal samples per point, and spacing between those samples.
- `external_series_resistance_ohm`, `approximate_device_resistance_ohm`,
  `max_device_current_a_rms`, and `max_device_voltage_v_rms`: current conversion
  and fail-closed device bounds. Current is calculated with the configured
  external resistor plus the SR830 50 ohm output resistance plus the approximate
  device resistance.
- `external_50_ohm_termination = false`: records the confirmed wiring and is
  rejected by the strict loader if changed to true.
- `output_directory = "../run_data/commissioning"`: record destination relative
  to `hardware.local.toml`. The default assumes that TOML is in `config/`, so it
  aligns with the analysis notebooks' existing root `run_data/commissioning`.

See [`LOCKIN_DAILY_OPERATION.md`](LOCKIN_DAILY_OPERATION.md) for the daily
sequence and a concise explanation of the full table.

The daily sweep commands no longer accept per-run wiring confirmations or an
arming flag. `[lockin_sweep]` is the source for sweep limits and timing, while
the two strict Lock-in tables are the source for range policies, semantic
reference roles, and the XY SINE OUT disconnection. This does not replace the
operator's physical inspection: the preflight can validate readbacks, lock,
overload and error states, but cannot see a cable. A preflight failure stops the
scan before any sweep setting is written.

Each point records h1, h2, and h3 in order: both instruments receive the paired
`HARM` setting, wait for the configured settling time, and then collect the
formal samples. Cleanup restores both instruments to h1. Neither sweep writes
FMOD, RSLP, ISRC, IGND, ICPL, ILIN, RMOD, OFLT, or OFSL. Both consume
`LIAS?`/`ERRS?`, stop at the first unsafe or mismatched point, retain the
rejected sample, and attempt to restore `lockin_xx` to 4 mVrms and 17.777 Hz.
If sweep setup changed XX or XY sensitivity, cleanup also restores that role's
preflight range and verifies it. A communication failure still requires manual
front-panel verification.

Once the VISA pair has opened, every attempted scan writes an atomic JSON result
under `output_directory`, including `completed`, `rejected`, and `interrupted`
outcomes. `run_metadata` preserves the configured per-run name and note.
`measurement_config` is an address-free snapshot of the resolved TOML request
(scan points, harmonics, sensitivity, timing, and circuit/device limits); the
actual SR830 readbacks remain in `preflight`, `sensitivity_setup`, each point,
and `cleanup`. Failure to write the audit file fails the command rather than
reporting an unarchived measurement as complete.

`--settle-s` is a transition-settling interval. Both daily sweep commands refuse
an interval below 1.5 s before opening either VISA resource. At the current
300 ms / 24 dB/oct bench setting, every actual `SLVL` (SINE OUT) change waits two
intervals before the output readback and formal h1 sample: 3.0 s by default.
This duration is recorded as root-level `source_step_settle_s` and for each
written source point; a 4 mVrms baseline point that did not write `SLVL` records
zero for its point-specific value. It is an acquisition-settling parameter, not
a safety limit or an automatic phase correction.

The daily frequency coverage policy comes from the union of
`frequency_xx_harmonics` and `frequency_xy_harmonics`, together with
`skip_unsupported_harmonics = true` in `[lockin_sweep]`. It retains only the
selected and supported orders at each point and writes an audited
`skipped_harmonics` entry for every frequency-limit omission; no missing order
is inferred in analysis. On the confirmed ten-point 17.777 Hz--100 kHz grid,
this yields h1 at all ten points, h2 through 38.310 kHz (nine points), and h3
through 14.677 kHz (eight points). Change the TOML before a future scan if that
policy needs to change.

The HARM transition record is deliberately excluded from formal curves. It may
contain filter/frequency-range latches while the reference moves to the selected
detection harmonic. A first-read input/reserve-only latch is retained as a
transient candidate, consumed, and checked once more after a full settling
interval; only a clean second read permits formal sampling. Reference unlock,
output overload, a time-constant change, an instrument error, or any repeated
input/reserve latch remains a failure. Every formal h1/h2/h3 sample rejects
every unlock, overload, and error bit without exception.

After restoring any range changed by sweep setup, cleanup waits, records the
range-transition status, waits again, and then performs the strict final
diagnostic. Only an XX `LIAS=4` output-overload latch is accepted, and only when
the original XX range was restored; a setup transition accepts no such latch.
Any XY overload, either reference unlock, unexpected frequency or time-constant
change, or instrument error still fails cleanup. Any status bit that reappears
in the final diagnostic also fails cleanup.

The first real frequency attempt stopped at 25 Hz on a transition-period XY
unlock latch and did not proceed to the excitation scan. The restored settings
read back correctly, but the retained latch made the immediate cleanup result
unverified. A subsequent 10-sample read-only recovery record at 17.777 Hz had
zero unlock, overload, and error bits throughout. The transition/status-window
separation above was added from that retained result; it requires a newly
authorized real retry.

The next transition-aware retry accepted 25, 35.5, and 50 Hz, then stopped on
the third 70.7 Hz sample solely because the locked XY frequency readback differed
by 2.2 mHz (31 ppm). Final 17.777 Hz/4 mVrms restoration was fully verified.
The initial sweep-only 50 ppm readback tolerance was added from that retained
result and also requires a newly authorized retry.

The following retry stopped on an actual XX output-overload latch in the first
50 Hz formal sample, where R was about 1.09 mV on the 1 mV sensitivity range.
Final restoration was clear, but the overload attempt remains rejected and the
excitation scan did not start. The temporary 20 mV xx frequency-sweep range above
was added from that result and requires a new SENS-write authorization.

The SENS-authorized retry first encountered a stale XY overload latch during
preflight, before any write. A separate 10-sample read-only recovery record was
clear throughout, so the same authorized run was retried. With XX temporarily on
the 20 mV range, every formal point through 200 Hz passed. The transition read at
282 Hz then returned XY `LIAS=26` (filter overload, reference unlock, and frequency
range changed), so that implementation rejected the run before collecting a
formal 282 Hz sample. Cleanup fully verified 17.777 Hz, 4 mVrms, the original
1 mV XX range, and zero status/error bits on both lock-ins; the excitation scan
did not start. The retained result motivated treating transition-only overload
latches like the already separated unlock/range-change latches while keeping the
formal sample window unchanged and strict.

The next authorized retry accepted 25 and 35.5 Hz, then rejected the second
formal 50 Hz sample solely because the locked, overload-free, error-free XY
readback was 49.9973 Hz: 2.7 mHz or 54 ppm low, just beyond the 50 ppm sweep-only
tolerance. Cleanup fully verified 17.777 Hz, 4 mVrms, the original 1 mV XX range,
and clear status/error words on both units; the excitation scan did not start.
The 100 ppm sweep-only tolerance above provides measured margin for this jitter
without relaxing any status criterion and requires new explicit authorization.

The next authorized 100 ppm run completed all 13 formal frequency points through
1 kHz and fully verified restoration to 17.777 Hz, 4 mVrms, and the original
1 mV XX range with clear final status/error words. The first excitation invocation
then stopped during preflight on a stale XY overload latch before any write; a
10-sample read-only recovery was entirely clear. The retry acquired all 11 source
points from 4 to 400 mVrms and all 33 formal samples had zero status/error bits
and no reported problem. At 400 mVrms, nominal current was 3.958 uArms, mean Vxx R
was about 5.384 mV, and mean Vxy R was about 1.748 uV. The source and XX sensitivity
read back at 4 mVrms and the original 1 mV range after cleanup, but the immediate
final read retained one XX `LIAS=4` output-overload latch from restoring the narrow
range, so the raw run remains rejected. A following 10-sample read-only record was
fully clear. The range-transition cleanup separation above requires a new explicit
authorization before repeating the excitation scan for a completed record.

The authorized cleanup-aware retry completed all 11 source points and all 33
formal samples with zero status/error bits and no reported problem. At 400 mVrms,
nominal current was 3.958 uArms, mean Vxx R was about 5.363 mV, and mean Vxy R
was about 1.748 uV. Cleanup recorded the expected XX-only `LIAS=4` overload latch
after restoring the narrow range while XY remained clear, then the strict final
read verified 17.777 Hz, 4 mVrms, the original 1 mV XX range, and zero status/error
words on both units. This is the accepted excitation-sweep commissioning record.

At 400 mVrms the nominal current is about 3.958 uArms and the nominal device
voltage about 3.958 mVrms. The conservative short-circuit current bound is about
3.998 uArms, and the conservative open-circuit device-voltage bound is 0.4 Vrms;
both are checked against the supplied limits before VISA is opened.

## Acceptance criteria

- Both IDNs identify Stanford Research Systems SR830 units with distinct VISA
  addresses and recorded serial numbers.
- `lockin_xx` reads internal reference, harmonic 1, and the intended frequency.
- `lockin_xy` reads external reference, TTL rising, harmonic 1, the same
  frequency, and no unlock.
- The first latch-consuming sample records and clears status accumulated before
  the observation window. Every later sample must remain free of input/reserve,
  filter, and output overload; a persistent or reappearing overload fails.
- Both report zero instrument-error status.
- X, Y, R, and phase settle over the chosen observation window; raw samples are
  retained even if the result is rejected.
- Reversing the device current leads or a voltage-probe pair produces the
  expected sign change before a physical sign convention is accepted.
- The harmonic-stage record contains xx and xy readings for harmonics 1, 2, and
  3, with no unlock, overload, instrument error, or frequency mismatch, and both
  instruments read back harmonic 1 afterward.

## Stop and disconnect

Set `lockin_xx` back to 4 mVrms, verify the front-panel amplitude, then
physically open the device excitation path. Leave `lockin_xy` SINE OUT
disconnected. If communication is lost, do not assume either amplitude or
device current is safe; verify both instruments and the circuit manually.
