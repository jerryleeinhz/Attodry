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

These are separate write-authorized device-only tests. The frequency scan uses
4 mVrms and first-harmonic detection at 17.777, 25, 35.5, 50, 70.7, 100, 141,
200, 282, 398, 562, 794, and 1000 Hz. It writes only the internal frequency on
`lockin_xx`; `lockin_xy` follows the external TTL reference. After each actual
frequency change, the tool waits 1.5 seconds, records and clears transition
status, waits another 1.5 seconds, then retains three sequential xx/xy samples
0.3 seconds apart. A transition-period XY unlock or frequency-range-change latch
is recorded as expected; any overload, instrument error, unexpected setting
change, XX internal-reference unlock, or renewed XY unlock in the formal sample
window still fails the scan.

```powershell
python -m attodry_control.lockin_test sweep-frequency `
  --config config\hardware.local.toml `
  --authorize-writes `
  --confirm-xy-sine-disconnected
```

The excitation scan is a source-voltage scan with a nominal current calculated
from the complete supplied series path. Its default source points are 4, 6, 10,
16, 26, 40, 64, 100, 160, 252, and 400 mVrms. For the confirmed 100 kohm
external resistor, 50 ohm SR830 output resistance, and approximate 1 kohm device,
run:

```powershell
python -m attodry_control.lockin_test sweep-excitation `
  --config config\hardware.local.toml `
  --series-resistance-ohm 100000 `
  --device-resistance-ohm 1000 `
  --max-device-current-a 0.005 `
  --max-device-voltage-v 5 `
  --xx-sensitivity-code 21 `
  --authorize-writes `
  --confirm-xy-sine-disconnected `
  --confirm-no-50ohm-termination
```

Sensitivity code 21 is the 20 mV range and is applied only to `lockin_xx` for
the excitation sweep. The original xx sensitivity is recorded and restored;
`lockin_xy` sensitivity is not changed. Neither sweep writes FMOD, RSLP, HARM,
ISRC, IGND, ICPL, ILIN, RMOD, OFLT, or OFSL. Both consume `LIAS?`/`ERRS?`, stop
at the first unsafe or mismatched point, retain the rejected sample, and attempt
to restore `lockin_xx` to 4 mVrms and 17.777 Hz. A communication failure still
requires manual front-panel verification.

The first real frequency attempt stopped at 25 Hz on a transition-period XY
unlock latch and did not proceed to the excitation scan. The restored settings
read back correctly, but the retained latch made the immediate cleanup result
unverified. A subsequent 10-sample read-only recovery record at 17.777 Hz had
zero unlock, overload, and error bits throughout. The transition/status-window
separation above was added from that retained result; it requires a newly
authorized real retry.

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
