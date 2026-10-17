# Thorlabs MC2000B optional chopper

Status: **offline complete; firmware 4.16 identity/settings read-only checked;
write/rotation commissioning pending**. Confirm the actual blade/beam track and
chosen frequency before acquisition. A blade register cannot prove which physical
wheel or beam path is installed. Non-unity-ratio `input?` semantics remain pending
physical comparison; the original strict guard is retained and may reject them.

The protocol source is Thorlabs **MC2000B, MC2000B-EC User Guide**,
TTN102010-D02, Rev B, March 20, 2023, printed pages 16–19 and 24–25
([manufacturer PDF, university-hosted copy](https://moodle2.units.it/pluginfile.php/731176/mod_folder/content/0/MC2000B-EC-Manual.pdf)).

## Wiring and frequency meaning

This experiment uses XX SR830 internal reference. XX rear TTL OUT feeds XY REF IN
and the chopper's EXT REF INPUT. XX and XY both measure h1. Their h1 frequency is
the XX reference frequency. XX SINE OUT may be physically connected to the sample
or disconnected; that declaration is separate from TTL synchronization.

The chopper's external-reference numerator and denominator give
`selected reference = external frequency * harmonic_n / harmonic_d`.
These are independent of lock-in harmonic settings. For the approved h1/h1 scan,
the optical chopping frequency must also match the detector frequency. The
Combination validator checks this; it does not silently alter either harmonic.

For high-precision blades, `reference_track` selects the synchronized inner or
outer track. For MC2F dual-frequency blades, the controller synchronizes the
outer track; the inner track chops at `outer frequency * inner_slots / outer_slots`.
Ordinarily software selects the **actual beam track** output, never TARGET,
so `refoutfreq?` represents the selected actual output. This is frequency evidence,
not a remote measurement of PLL phase lock.

### One transmitted opening per rotation

`single_opening_per_rotation = true` is an explicit operator declaration that
the beam passes the inner track of an MC2F blade once per complete rotation,
with the outer sensing track unmodified. The initial implementation accepts
only MC2F inner-track geometry; the default is `false`. It does not infer a mask
from a frequency readback or change the blade register.

For the operator-confirmed MC2F47 arrangement, three of the four inner openings
are blocked. XX supplies 8 Hz and the controller retains N=7, D=1. The native
outer control reference is 56 Hz, the unmasked inner track is nominally 32 Hz,
and the single transmitted opening gives 56/7 = **8 Hz at the beam**. In this
mode software selects actual OUTER (`output=1`) and verifies its 56 Hz sensor
readback before dividing by seven. It does not claim that nominal INNER
(`output=2`) measures the masked 8 Hz beam. An unmasked MC2F47 inner beam with
these N/D settings remains 32 Hz and is rejected for an 8 Hz h1 scan.

All native motor/outer-track limits still apply before the beam division. The
mask cannot make an otherwise invalid motor speed valid. This geometry is a
physical declaration; sensor frequency plus geometry derives the beam frequency
and does not independently measure optical modulation or PLL phase.

MC2000B firmware 4.16 station readback establishes `input?` as the reference
after N/D: with XX=8 Hz, N=7 and D=1 it returns 56 Hz. Readiness therefore checks
that native reference separately from actual selected sensor output. `freq?`
returns the retained internal reference setting, which is not the active external
reference in external mode. TARGET (`output=0`) is never accepted as actual
sensor evidence. Raw replies retain their original names and values; snapshots
separately record expected native input/output, nominal track, the mask
declaration and beam frequency derived from the actual sensor.

| Blade | Register | Inner/single slots | Outer slots | Chopping ranges (Hz) |
|---|---:|---:|---:|---|
| MC1F2 | 0 | 2 | 100 | inner 4–200; outer 200–10000 |
| MC1F10 | 1 | 10 | — | 20–1000 |
| MC1F15 | 2 | 15 | — | 30–1500 |
| MC1F30 | 3 | 30 | — | 60–3000 |
| MC1F60 | 4 | 60 | — | 120–6000 |
| MC1F100 | 5 | 100 | — | 200–10000 |
| MC1F10HP | 6 | 10 | 100 | inner 20–1000; outer 200–10000 |
| MC1F2P10 | 7 | 2 | 100 | inner 4–200; outer 200–10000 |
| MC1F6P10 | 8 | 6 | — | 12–600 |
| MC1F10A | 9 | 10 | — | 20–1000 |
| MC2F330 | 10 | 3 | 30 | inner 6–300; outer 60–3000 |
| MC2F47 | 11 | 4 | 7 | inner 8–400; outer 14–700 |
| MC2F57B | 12 | 5 | 7 | inner 10–500; outer 14–700 |
| MC2F860 | 13 | 8 | 60 | inner 16–800; outer 120–6000 |
| MC2F5360 | 14 | 53 | 60 | inner 106–5300; outer 120–6000 |

Outer ranges for the high-precision blades follow the manufacturer's 2–100 rev/s
motor range and outer slot count. Both physical track and configured register
must be confirmed. The driver never sends `blade=...` to disguise a mismatch.

## TOML choices

Omission or `enabled = false` opens no device. Unknown fields are always errors;
disabled known fields may retain placeholders. The following is a portable
disabled template, not an approved station configuration:

```toml
[chopper]
enabled = false
backend = "serial"             # simulation | serial | manual
model = "MC2000B"               # MC2000B | MC2000B-EC; confirm label/firmware
port = "CHANGE_ME_EXPLICIT_COM_PORT"
expected_idn = "CHANGE_ME_EXACT_ID_QUERY_RESPONSE"
blade = "CHANGE_ME_CONFIRMED_INSTALLED_BLADE" # choose exact name from table
reference_source = "external"   # external | internal
reference_track = "inner"       # single for single blades; inner | outer otherwise
single_opening_per_rotation = false # true only for confirmed MC2F inner single-opening beam; outer sensor intact
harmonic_n = 1                  # integer 1..15; external multiplier
harmonic_d = 1                  # integer 1..15; external divider
# phase_deg = 0                 # optional integer 0..360; omission preserves/read-checks phase
run_enabled = true              # true = rotate; false = standby, no optical modulation
finish_rotation = "stop"        # stop | preserve; caller closes laser before stop
frequency_tolerance_hz = 1.0    # absolute input/actual-output comparison, not phase lock
io_timeout_s = 2.0              # 0.01..60 s; each exchange also honors operation deadline
ready_timeout_s = 30.0          # 0.01..3600 s; includes open/configure/poll work
poll_interval_s = 1.0           # 0.001..ready_timeout_s
consecutive_good = 3            # 1..100; initial frequency-ready polls
manual_phase_lock_confirmed = false # only true after front-panel confirmation for this run
```

Simulation uses explicit known blade/track values and records its synthetic
backend. Real serial connections require explicit authorization, an explicit
port, exact expected identity and confirmed front-panel phase lock. There is no
automatic port scan or automatically guessed station identity. This initial
driver has no dedicated CLI; Combination owns its lifecycle and authorization.

`manual` performs no serial I/O and records user declarations. It never reports
measured input/output frequency or remotely verified phase lock. Its cleanup
requires manual rotation verification. A manual declaration is not equivalent
to the serial backend's frequency readback evidence.

## Lifecycle, safety and audit

1. Validate every reference frequency against blade/track limits before opening.
2. With laser OFF, identify the controller and read its configuration. Reject an
   identity/blade mismatch and unknown state before taking write ownership.
3. `verbose?` must already be zero. The driver refuses asynchronous verbose
   messages instead of clearing them or silently changing their configuration.
4. If static settings differ while the motor is running, request standby, verify
   it, set only changed values, read each value back, then enable rotation.
5. Wait within one total deadline for consecutive good enable/configuration and
   actual-frequency polls. The controller's front-panel PLL indication remains
   separately confirmed by the operator; the current driver does not commission
   or use remote PLL readback.
6. Keep rotation during illuminated and matched-dark measurements. Re-entering
   unchanged, already qualified settings performs fresh verification without
   another run/standby cycle or repeated multi-poll wait. Settings/frequency
   drift still fails; hardware errors are not converted to record-and-continue.
7. The optical owner closes the laser first, then protects electrical outputs,
   then performs chopper cleanup and closes resources. Failed communication
   retains the last confirmed state and requires manual verification.

`enable=0` means rotation disabled. A stopped blade can leave the beam open.
`optical_off_confirmed` is therefore always false in chopper cleanup. The laser
owner must provide independent optical-OFF evidence. An acknowledged command
alone cannot certify stopped rotation; cleanup also queries `enable?`.

The serial protocol is fixed at **115200 baud, 8 data bits, no parity, one stop
bit, no flow control**, CR-terminated commands, and `>` response prompt. Parsing
allows only the exact optional command echo. Every response is owned by one
request; stale, partial, ambiguous or command-error frames poison the session.
No late response is assigned to the next request and no automatic flush hides
raw evidence. OS serial exclusivity and an in-process port registry prevent
duplicate owners. Original replies and failed frames are retained.
After a fully received terminal prompt, a separately arriving CR/LF/space/TAB
suffix is attached to that preceding raw response, with its receipt time and
raw bytes recorded. It is never assigned to the next command. Before the first
completed response, whitespace remains unowned and is rejected; any non-whitespace
late input poisons the session before another command is written.

Snapshots contain backend, identity, requested reference and derived optical
frequency, last confirmed settings/frequencies, current-state validity,
per-command monotonic timing and transcripts. `phase_lock_verified_by_query`
is always false; `manual_phase_lock_confirmed` remains separate.

## Offline verification and performance

```powershell
python tools/run_guarded_tests.py tests.test_chopper
```

The guard denies real serial/VISA imports and instrument DLL loads. Tests cover
all blade/track boundaries and mappings, zero-I/O unauthorized calls, exact
transcripts, read-before-write/readback, shared deadlines, reuse of stable
settings, drift, malformed/late frames, ownership, partial cleanup and manual
evidence. No instrument was connected by these tests.

A full fresh capture has ten query exchanges. Initial preparation performs
identity/verbose and state preflight before any changed-setting writes. Each
write gets its own readback; unchanged preparation adds exactly one ten-query
fresh capture, zero writes and zero repeated stability sleeps. This is a fake
communication/wait budget, not measured real motor locking or USB speed.

Real read-only commissioning must first confirm firmware response shape,
optional echo/prompt, all readback meanings, installed blade/track and phase-lock
indication. A separately authorized laser-OFF smallest-write test then confirms
start/standby and cleanup before illuminated use. No Ctrl+C path may kill an
owner instead of running the same optical/electrical/chopper cleanup sequence.
