# Laboratory commissioning checklist

This checklist is deliberately staged. Stop after each section and record the
result. Never start a later write-enabled section merely because an earlier
query succeeded.

## 0. Required operator inputs

Before any real connection or write, record:

- `lockin_xx` and `lockin_xy` VISA addresses and their physical role mapping;
- device maximum RMS excitation current and voltage, approximate resistance,
  all series resistance/attenuation, and whether any 50-ohm termination is used;
- lock-in input mode (`A` or `A-B`) and shield (`Float` or `Ground`);
- confirmation that `lockin_xy` SINE OUT is physically disconnected;
- top/bottom gate SMU manufacturer and exact model, VISA addresses, safe
  absolute voltage limits, compliance, leakage trip, ramp step, settle time,
  and acceptable voltage-readback error;
- attoDRY COM port, installed 64-bit DLL path/version, and confirmation of the
  legacy DLL device type;
- sign-to-physical-direction mapping for controller X and Z.

Do not store local addresses or DLL paths in Git. Put them only in the ignored
`config/hardware.local.toml`.

## 1. Dual SR830 device-only test

Follow [`DUAL_SR830_DEVICE_TEST.md`](DUAL_SR830_DEVICE_TEST.md). Begin with the
front-panel/manual minimum-output test, then use resource discovery and
query-only diagnostics. Setting writes require separate operator authorization.

Acceptance record:

- both IDNs and semantic roles;
- TTL lock stable on `lockin_xy`;
- no input/filter/output overload;
- excitation current calculation and device limit margin;
- h1/h2/h3 xx/xy readings and noise-floor comparison;
- safe stop at 4 mVrms on `lockin_xx`, with `lockin_xy` output still physically
  disconnected.

## 2. attoDRY read-only connection

After explicit connection authorization, connect only long enough to record full
state: temperatures, Bx/Bz readback and setpoints, control flags, and error code.
No setting write is authorized by this step. If any read fails, retain the last
confirmed state and verify the magnet manually.

After the station-local COM port and DLL path have been confirmed, use:

```powershell
python -m attodry_control.attodry_test `
  --config config\hardware.local.toml `
  --samples 10 `
  --interval-s 1 `
  --authorize-connection |
  Tee-Object -FilePath "attodry_read_state.json"
```

The command constructs the driver with `writes_authorized=False`; it cannot call
temperature, field, control-toggle, or sweep-to-zero setters. It records only
full-state reads, then calls Disconnect and end. Connection authorization does
not authorize any later setting write.

Acceptance record (2026-08-20): the authorized 10-second run completed 10/10
full-state reads with writes disabled, zero error codes, zero Bx/Bz readbacks and
setpoints, and both control flags disabled. Disconnect and end completed normally.
The raw JSON and station-local connection details remain on ignored paths only.

## 3. Gate SMU zero-bias validation

This stage cannot be coded until the exact SMU models are supplied. After the
model adapters are reviewed, start with outputs disabled, configure compliance,
command 0 V, enable one gate at a time, and verify voltage and leakage readback.
Any mismatch or excess leakage must attempt controlled zero and output disable.

## 4. Small controlled movements

Each movement needs a new explicit write authorization and operator-selected
limits:

1. smallest practical temperature change and stable readback;
2. small pure-X field and verified zero;
3. small pure-Z field and verified zero;
4. small gate ramp on each gate independently and verified zero.

At every vector point, enforce `sqrt(Bx^2 + Bz^2) <= 3 T`.

For the temperature movement, first make the ignored, station-local parameter
file. Its first table contains every per-attempt temperature parameter; it is
separate from the hardware path/address TOML. Replace every placeholder and
review each policy before adding the two authorization flags. Merely having this
command in the repository does not authorize a connection or write:

```powershell
Copy-Item config\temperature_commissioning.example.toml `
  config\temperature_commissioning.local.toml
notepad config\temperature_commissioning.local.toml

python -m attodry_control.temperature_test `
  --config config\hardware.local.toml `
  --commissioning-config config\temperature_commissioning.local.toml `
  --authorize-connection `
  --authorize-temperature-write |
  Tee-Object -FilePath "attodry_temperature_movement.json"
```

The parameter file accepts the same values that the previous direct options did;
the two sources cannot be mixed. `success_policy` is `hold-target` or
`restore-initial`; `failure_policy` is `hold-current` or `restore-initial`. The
tool rejects placeholders, malformed parameter files, configured-range violations,
and requested movements from the initial sample-temperature sensor reading larger
than `max_delta_k` before any write. The initial user setpoint delta is also
recorded for audit, but a stale setpoint while temperature control is disabled is
not treated as physical sample movement. `restore-initial` restores the original
setpoint and control flag; if the original control was disabled, it does not claim
that the sample temperature returned to the original value. Any communication or
close failure requires manual verification of setpoint and control state.

## 5. End-to-end run and deliberate safe failure

Run one low-excitation, zero-gate, near-zero-field condition first. Confirm six
safe xx/xy × h1/h2/h3 readings are accepted. Then deliberately cause a benign
lock-in rejection (for example, a planned reference-unlock test) and verify that
the raw rejected attempt remains in the database while default analysis excludes
it. Confirm electrical safeing, requested field-zero readback, audit events, and
resume behavior.

## 6. Offline release

On an online Windows computer with the same 64-bit Python 3.11 runtime, build and
download the wheelhouse as described in `CODEX_AND_OFFLINE_SETUP.md`. Install it
on the offline control computer with `--no-index`, run the full tests, then run an
audited simulation before enabling any hardware configuration.
