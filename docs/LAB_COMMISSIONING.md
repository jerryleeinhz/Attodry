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

Do not store local addresses or machine-specific DLL paths in Git. Put those
values only in the ignored `config/hardware.local.toml`; this does not set a
policy for whether the DLL binary itself is packaged in the repository.

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
state plus sample/VTI heater power: temperatures, Bx/Bz readback and setpoints,
control flags, error code, `sample_w`, and `vti_w`. No setting write is authorized
by this step. If any read fails, retain the last confirmed full state and verify
the magnet manually. Heater-power return errors, non-finite values, and negative
values fail closed.

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
full-state and heater-power reads, then calls Disconnect and end. Connection
authorization does not authorize any later setting write.

Acceptance record (2026-08-20): the authorized 10-second run completed 10/10
full-state reads with writes disabled, zero error codes, zero Bx/Bz readbacks and
setpoints, and both control flags disabled. Disconnect and end completed normally.
The raw JSON and station-local connection details remain on ignored paths only.

Heater-power extension acceptance (2026-08-21): the exact implementation passed
compileall and all 166 offline tests without skips on 64-bit Python 3.12.13 `lyr`,
and a no-connect DLL load confirmed all 23 required symbols. A GUI-held-resource
failure was retained without samples; after GUI Disconnect, a separate authorized
read-only run completed 10/10 samples and normal disconnect/end. Sample-heater
power was 0.2036--0.2037 W and VTI-heater power was 0.0004 W. Sample temperature
was 1.7335--1.7340 K with a constant 1.75 K setpoint, enabled temperature control,
zero errors, and zero field readbacks/setpoints. No write was authorized or sent.

Stability-monitor record (2026-08-21): after GUI Disconnect, a separate authorized
601-sample, 600.622-second run completed with empty stderr and normal
Disconnect/end. It sent no write. Temperature rose from 1.7342 to 1.7369 K and
ranged 1.7335--1.7372 K (3.70 mK peak-to-peak); sample-heater power was
0.2106--0.2217 W, VTI-heater power was 0.0004 W, control stayed enabled, and
setpoint/errors/field values remained valid. However, all samples were below 1.74 K,
the configured lower tolerance edge. This is a failed temperature-stability record,
not authorization to change PID, heater configuration, or setpoint.

Thirty-minute follow-up (2026-08-21): after retaining a resource-busy pre-sample
failure and releasing the competing GUI/connection, the read-only retry recorded
1801 samples over 1801.803 s, empty stderr, and normal Disconnect/end. It began at
1.7401 K, but no 600 s stable window formed; the longest continuous tolerance
interval was 319.313 s. Sample readback reached 1.7289 K and then rose continuously
to 1.9651 K in about 25 s before decaying to 1.7746 K. VTI changed only from about
1.717 K to 1.724 K during the event, and sample-heater output was
0.0927--0.2413 W. Setpoint, temperature-control, error, and field invariants stayed
valid. Treat this as a failed stability/overshoot diagnostic requiring manual PID,
thermal-contact, and sensor-loop review; it is not permission to alter settings.

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
`restore-initial`; `failure_policy` is `disable-control` or `restore-initial`. The
tool rejects placeholders, malformed parameter files, configured-range violations,
and requested movements from the initial sample-temperature sensor reading larger
than `max_delta_k` before any write. `max_overshoot_k` is a separate live guard:
each triggering sample is recorded, and a sample temperature greater than or equal
to `target_k + max_overshoot_k` fails the run and invokes the configured failure
policy. The resulting absolute limit must remain inside the configured temperature
range. The initial user setpoint delta is also
recorded for audit, but a stale setpoint while temperature control is disabled is
not treated as physical sample movement. A new setpoint or temperature-control
toggle is confirmed with complete state/error polling for at most 30 seconds because
the vendor DLL can update both readbacks asynchronously. Commissioning first
confirms full temperature control enabled, then writes the sample-temperature target.
If control just changed from disabled to enabled, it deliberately reapplies the
target even when the setpoint readback already matches; otherwise identical target
and control states remain idempotent. `restore-initial`
restores the original setpoint and control flag; if the original control was
disabled, it does not claim
that the sample temperature returned to the original value. `disable-control`
leaves the last requested setpoint in place but uses an idempotent read-before-toggle
operation and bounded readback confirmation to turn full temperature control off.
Before recovery, failures capture the last confirmed complete state and sample/VTI
heater powers; a diagnostic read failure is recorded without hiding the primary
error. Any communication, recovery, final-read, or close failure requires manual
verification of setpoint and control state.

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
