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
