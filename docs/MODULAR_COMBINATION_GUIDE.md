# Modular Combination: optics and electrical/environment modules

Status: code and hardware-free acceptance for the new module contracts. This
change does not commission new hardware combinations, deploy over an active
scan, or start a complete real scan. Use confirmed local identities, wiring and
limits for real operation; synthetic examples are not physical ratings.

## 1. Daily commands and configuration selection

```powershell
python -m attodry_control.combination_cli validate-config --config config/modular_optics_simulation.toml
python -m attodry_control.combination_cli simulate --config config/modular_optics_simulation.toml
python -m attodry_control.combination_cli monitor --config config/modular_optics_simulation.toml --once
```

For a completed, reviewed local hardware profile:

```powershell
python -m attodry_control.combination_cli run --config config/my_modular.local.toml
python -m attodry_control.combination_cli monitor --config config/my_modular.local.toml
```

Executing `run` authorizes the selected acquisition modules and the configured
route. No `--authorize-*` flags are needed. Internal configuration, identity,
output-state, wiring, bounds, protection, readback and cleanup checks still apply.
`simulate` rejects hardware backends. `validate-config` and `describe-hardware`
expand and validate the plan without opening any resource.

Both daily CLIs use `config/hardware.local.toml` by default for run, simulation
and configuration checks. Combination monitor uses the same default path to
locate its registered run/database. Run from the NKT project root. A missing run
or validation configuration is an error; no old trial-file fallback is attempted. Pass
`--config` when selecting another file. The launch output records the
absolute path, file hash, selected modules, coordinate plan and run ID. It does
not guess a profile from the most recently modified file.

Both `combination_cli` and the compatible `photoresponse_cli` select the modular
service when the chosen TOML contains `[combination_scan]`. Existing
`[photoresponse_combination]` files remain supported with their existing meaning;
do not put both order tables in one file. `--database` overrides SQLite storage.
`--resume-run RUN_ID` creates a fresh whole attempt only for the same archived
plan. Previous failed windows and dark baselines are not spliced into that
attempt. `--json` emits the final structured record without human progress.

Complete offline examples:

- `config/modular_optics_simulation.toml`: NKT + PM100D + 2182A, two wavelengths,
  dark plus three bright powers per wavelength, eight conditions.
- `config/modular_electrical_simulation.toml`: 6221 + 2182A, three current targets,
  no optical device configuration.
- `config/modular_environment_simulation.toml`: two independent temperatures ×
  two magnetic vectors × 2182A, four conditions.
- `config/modular_optics_hardware.example.toml`: deliberately incomplete hardware
  handover template. Its `CHANGE_ME` values and false wiring confirmations reject
  operation until replaced with confirmed facts in an ignored local copy.

## 2. Selecting modules and controlling the Cartesian product

```toml
[combination_scan]
backend = "simulation" # hardware for a fully configured real station
order = ["temperature", "magnetic", "optics", "current_source", "smu", "nanovoltmeter"]
samples_per_condition = 3
repeats = 1
run_name = "my_scan"
note = "Describe the experiment and configuration provenance."
output_directory = "../run_data/my_scan"
database_path = "../run_data/my_scan/audit.sqlite" # Optional; otherwise output_directory/audit.sqlite
```

The left module is outermost. Each module supplies its ordered point table; the
Combination traverses the Cartesian product. All selected modules are read at
each condition as appropriate to their capabilities. Module order determines
coordinate traversal; the coordinator separately enforces safe output-enable
order and response ownership.

| Module | Coordinate(s) | Meaning |
|---|---|---|
| `temperature` | `temperature_k` | Independent temperature targets |
| `magnetic` | `field_x_t`, `field_z_t` | Complete vector-field targets |
| `optics` | Wavelength, bandwidth, requested power or direct actuator | One coordinated laser/PEM/power-detector chain |
| `current_source` | `bias_current_a` | Keithley 6221 DC current excitation |
| `smu` | Selected bias/top/bottom voltage or current coordinates | Existing Three-SMU point engine |
| `nanovoltmeter` | No scan coordinate; one measurement point | 2182A voltage, or existing voltage-meter adapter |

A module is active when included in `order`. Removing it removes its scan axis
and control actions; unselected devices are not opened. A selected one-point
module is a fixed setting, with its normal safety checks and readbacks. Keeping
a device table in the file does not independently enable that module.

An exception is a physical communication dependency: a 2182A connected through
the 6221 serial relay still requires that 6221 session. Without `current_source`
in `order`, the host is relay-only and its current setting zero/output OFF are
verified. It does not become excitation merely because its table is present.

`samples_per_condition` is the number of formal fresh reads at each Cartesian
condition. `repeats` repeats the complete grid and has a new visit identity.
Repeated numerical points remain separate points with distinct input indices.
Nothing is automatically sorted or deduplicated.

Temperature and magnetic axes must precede optics and electrical axes. Their
full stability checks belong to axis entry; inner measurements retain actual
readback and abnormal-state checks. Revisits, repeats, retries and new attempts
invalidate the relevant qualification. Planned temperature steps include the
wrap from the last to first point when an outer axis or repeat causes a revisit.
The field invariant `sqrt(Bx²+Bz²) <= 3 T` applies to every vector and driver path.

Examples of freely selected combinations:

```toml
order = ["optics", "nanovoltmeter"]
order = ["current_source", "nanovoltmeter"]
order = ["temperature", "magnetic", "nanovoltmeter"]
order = ["optics", "current_source", "nanovoltmeter"]
order = ["current_source", "optics", "nanovoltmeter"]
```

Only one `order` belongs in an actual table. In the last two examples traversal
differs, but excitation is still disabled during optical preparation and enabled
only after optical readiness, electrical verification and fresh power evidence.
A module-only readback experiment need not select a laser or voltage detector.

## 3. Optics: laser → optional PEM → optional power detector

```toml
[optics]
laser = "nkt" # nkt / cld1010lp
use_pem = false
use_power_meter = true
mode = "power_stabilized" # power_stabilized / direct
initial_source_level_pct = 32.0 # An example input, NOT a hardware approval

[optics_scan]
order = ["wavelength", "bandwidth", "power"]
wavelength_ranges_nm = [{min=530.0,max=820.0,scale="linear",points=10}]
bandwidth_points_nm = [10.0]
power_ranges_w = [{min=10e-6,max=90e-6,scale="linear",points=9}]
dark_first = true
```

These optical values produce ten wavelength groups, each dark then nine bright
power points: 100 conditions. Two temperatures and three magnetic points outside
this module produce `2 × 3 × 100 = 600` conditions; three reads each produce
1,800 formal voltage reads. Counts are shown before any hardware session opens.

To use a single wavelength, replace `wavelength_ranges_nm` with:

```toml
wavelength_points_nm = [630.0]
```

The module owns laser configuration, PEM wavelength/retardance coordination,
PM100D wavelength correction, feedback, optical state and associated evidence.
Existing `nkt_source`, `nkt_varia`, `nkt_run`, `cld1010`, `pm100d` and
`power_feedback` device/policy tables supply identities, backend, physical limits
and instrument behavior. Scan coordinates come from `optics_scan`; do not also
define `power_feedback.target_power_w`, `target_powers_w` or `power_points`.

An explicit initial source percentage may alternatively come from the existing
first `nkt_run.points` row. A new file can omit those rows entirely. Existing ND
and pulse-picker fixed settings are retained when provided in that seed row.
The complete expanded optical points and original input are archived.

NKT VARIA ranges are resolved to its 0.1 nm wavelength grid. Both ideal generated
wavelength and resolved value are retained in point metadata; collisions remain
distinct input visits. Explicit lists are not silently rounded. Filter-edge,
bandwidth, mode, source-domain and pulse-picker validation still come from the
shared NKT contract. LLTF hardware capability has not been added by this feature.

With `use_power_meter=true`, use `power_stabilized`: PM100D closes the feedback
loop. Target µW is distinct from measured µW. Measured power is labeled at the
configured PM100D measurement plane and is not asserted to be sample power
without calibration. A bare PD needs its own supported readout and calibration;
an arbitrary voltage readout is not automatically a watt measurement.

Without a power meter, use direct actuator coordinates:

```toml
[optics]
laser = "nkt"
use_pem = false
use_power_meter = false
mode = "direct"

[optics_scan]
order = ["wavelength", "bandwidth", "source_level"]
wavelength_points_nm = [630.0]
bandwidth_points_nm = [10.0]
source_level_points_pct = [30.0, 35.0, 40.0]
dark_first = true
```

Direct mode does not accept W targets or invent measured optical power.

CLD uses a fixed nominal diode wavelength, no bandwidth coordinate, and current
in amperes. `initial_current_a` replaces initial percentage in stabilized mode;
direct mode uses `current_points_a` or `current_ranges_a`. All values still obey
the confirmed `cld1010` LD/TEC bounds and protections. Changing a nominal
wavelength field does not tune a diode.

To enable PEM, provide its confirmed complete `[pem]` contract, set
`optics.use_pem=true` and explicitly set `optics.peak_retardance_waves`. Its peak
retardance target is wavelength × waves and must fit that head's limits. With a
2182A this remains voltage acquisition; a PEM does not give the voltmeter lock-in
1f/2f demodulation. PEM disable acknowledgement is not a physical-off sensor.

## 4. Point generation, order and paired coordinates

Each scalar coordinate accepts exactly one explicit list or range array:

| Coordinate | Explicit list | Generated ranges |
|---|---|---|
| Temperature | `temperature_scan.points_k` | `temperature_scan.ranges_k` |
| 6221 current | `current_source_scan.points_a` | `current_source_scan.ranges_a` |
| Optical wavelength | `optics_scan.wavelength_points_nm` | `optics_scan.wavelength_ranges_nm` |
| Optical bandwidth | `optics_scan.bandwidth_points_nm` | `optics_scan.bandwidth_ranges_nm` |
| Requested optical power | `optics_scan.power_points_w` | `optics_scan.power_ranges_w` |
| Direct NKT setting | `optics_scan.source_level_points_pct` | `optics_scan.source_level_ranges_pct` |
| Direct CLD current | `optics_scan.current_points_a` | `optics_scan.current_ranges_a` |

Inclusive linear ranges use `min`, `max`, `scale="linear"` and exactly one of
`points` or `step`. A step must land exactly on max. Log ranges require positive
endpoints, `scale="log"` and `points`. A range has at least two points and max
greater than min. Use an explicit list for one fixed value, descending visits or
a return scan. Multiple segments preserve their boundaries/duplicate indices;
an optional `name` labels each segment. There is no implicit deduplication.

Optical dimensions use Cartesian products by default. `pattern="paired"` pairs
wavelength/bandwidth arrays, permitting a singleton fixed dimension to broadcast.
With `dark_first=true`, power/actuator remains the inner dimension, scanned in
full for every paired optical group. Explicit `order` must include every
multi-point dimension and must put this intensity dimension last for dark-first.

Magnetic vector tables preserve coordinated pairs:

```toml
[magnetic_scan]
points = [{bx_t=0.0,bz_t=0.0}, {bx_t=0.01,bz_t=0.02}]
```

For example, add `temperature` to the left of `optics` in `order` and define:

```toml
[temperature_scan]
points_k = [1.6, 2.0]
```

This repeats the complete optical grid at each temperature. Keep that station's
confirmed cryostat, bounds and stability tables. Temperature control requires
its temperature stability/step/overshoot
policy; magnetic control requires its field stability/step/end policy. An
unselected control axis does not require the other axis's movement policy.
Shared actual-temperature safety bounds and vector-field limits still apply
to the connected environmental module.

Alternatively, independently configure both component sources:

```toml
[magnetic_scan]
bx_points_t = [0.0, 0.1]
bz_ranges_t = [{min=0.0,max=0.2,scale="linear",points=3}]
pattern = "cartesian" # 2×3 vectors; paired requires compatible lengths
```

When `[smu_scan]` is absent, selecting `smu` reuses the existing module's complete
`three_smu_run` scan configuration through the pinned public loader and target
validator. This includes fixed/sweep roles, its ranges, paired/Cartesian mode
and ordered points. No second scan grid is required. The parsed module tables
must match the current source TOML; a missing or changed source fails before any
instrument can open. Coordinates retain their source plan's point identities.

For an explicit Combination grid override, `smu_scan.points` accepts paired dictionaries containing selected
`smu_bias_v/a`, `gate_top_v/a` or `gate_bottom_v/a`. Alternatively use e.g.
`gate_top_points_v`, `gate_bottom_ranges_v`, with `pattern="cartesian"` or
`"paired"`. One role cannot source voltage and current simultaneously. The
existing Three-SMU tables still define source modes, compliance, ramp/readback
limits, wiring and selected roles; scan points cannot change those facts.

Plans reject over 10,000 conditions or 100,000 formal samples before expanding
the complete condition list. Adding a supported module changes the selected
module list and its scan table, not the acquisition loop.

## 5. Dark references and formal measurement sequence

For each optical group, `dark_first=true` inserts one dark state before the
bright intensity points. With inner electrical scans it collects the dark
electrical subgrid first, then matches every bright condition to its exact dark
coordinate. For example:

```text
dark:  I1 I2 I3
10 uW: I1 I2 I3, each minus its matching dark current coordinate
20 uW: I1 I2 I3, each minus its matching dark current coordinate
```

A dark reference includes repeat/visit identity, temperature, vector field,
wavelength, bandwidth, PEM configuration, source/SMU coordinates and detector
configuration. Duplicate optical input visits have distinct group identities.
Dark age and reference ID are archived. An ordinary power retry does not silently
remeasure dark; a whole new attempt establishes new references. A dark row has
no measured target W. Zero in the pure plan is only an explicit dark placeholder,
not evidence that PM100D measured exactly zero power.

The sequence is:

1. Enter selected environmental axis points, set and qualify each once.
2. Prepare wavelength, bandwidth, optional PEM and PM wavelength correction.
3. Measure matching dark coordinates with optical emission verified off.
4. Tune and qualify bright power; settings remain fixed during a formal window.
5. Verify electrical configuration, then obtain fresh optical/power evidence.
6. Enable required excitation and collect fresh detector readings with retained
   pre/post electrical and optical checks.
7. Save raw windows, actual module readbacks, quality, dark reference and timing.

Current-source absence means no excitation. A configured relay host remains
OFF/zero. Safe typed power deviations have bounded requalification/full-window
retry; no-signal, hard-limit, protection, unknown communication and cleanup
failures stop. Failed windows are retained and never merged into a successful
window. Existing separate tuning, fluctuation/drift and acceptance criteria
remain distinct and are never automatically widened.

## 6. Nanovoltmeter controls and sample limits

```toml
[measurement]
max_abs_voltage_v = 1.0 # Example sample limit, not a universal safe setting
max_abs_current_a = 1e-8 # Required for current excitation
sample_interval_s = 0.1
settle_s = 1.0
dark_recovery_s = 1.0 # Optional for a selected dark-first optical sequence

[nanovoltmeter]
model = "keithley2182a"
channel = 2
voltage_range_v = 10.0
nplc = 1.0
analog_lowpass_enabled = false
autozero = true
front_autozero = true

[nanovoltmeter.digital_filter]
enabled = false
type = "repeating" # moving / repeating
count = 10
window_percent = 1.0
```

See [NANOVOLTMETER_SETTINGS.md](NANOVOLTMETER_SETTINGS.md) for exact instrument
domains, SCPI commands, readback verification, condition-history isolation,
autozero and timeout budgeting. NPLC, instrument filter count and software
samples are separate quantities. A moving average may share internal
conversions between readings; do not interpret these as independent repeats.
The filter window is a voltage-range percentage, not PM100D power tolerance.

Hardware mode additionally requires the existing explicit electrical transport,
identity/address, isolation and wiring tables. `[current_source]` is the modular
name for the existing 6221 hardware table; define one or the other, not both.
The 6221 is a current source and serial relay, not a replacement current meter.
2182A measures voltage. Source-drain photocurrent and a gate leakage role remain
distinct; the supported current-mode 2400 legacy path is not inferred from this
voltmeter module.

## 7. Monitor, archives and unified plotting

Monitor reads only committed SQLite/file evidence. It does not open instruments.
It reports actual total conditions including repeats, current condition, module
visit/stage, power tuning/stability/retry, raw valid/invalid detector values,
primary error and independent cleanup. Collected and accepted counts differ
until complete-run acceptance and verified cleanup. PID metadata is not a
process-liveness guarantee.

JSON/JSONL and SQLite preserve original TOML, expanded module plan and indices,
resolved coordinates, requested instrument settings, every failed attempt,
actual readings, software/config hashes and cleanup evidence. Default analysis
uses completed accepted attempts. Audit views retain rejected/incomplete rows.

For module-only experiments, genuine module readbacks are preserved even when no
2182A is selected: `quantity` stays absent/None and no detector voltage/current
observation is fabricated. Per-condition `module_means` and matching dark
`module_delta` project into fields such as
`measured.smu_gate_bottom_leakage_current_a` and
`measured.delta_smu_gate_bottom_leakage_current_a`. These retain the gate leakage
role; they are not channel photocurrent. SMU bias current/voltage measurements
remain distinct from 6221 source settings, which are never treated as measured
current or dark-subtracted data. Direct optics without a calibrated detector
does not manufacture a measured power field.

On Windows the output filename budgets the longest outcome suffix within the
259-character path limit. A long human run name may be shortened in the filename;
the full name remains in the archived plan, and the UTC label/unique token stay
intact. An output directory too long to fit this minimum is rejected before a
run store or instrument session opens. Existing files are never renamed.

Open `notebooks/unified_plotting.ipynb`, load the chosen run, and choose:

- PM100D actual optical power for the measured-power X coordinate, or requested
  target power when comparing the nominal grid.
- Measured detector mean voltage for the voltmeter's actual mean; it does not
  subtract dark.
- Measured light-minus-dark voltage for matched optical response. Select light
  states; dark rows are reference evidence, not an invented zero-response point.
- Fixed wavelength/other physical coordinates or explicit stacking when those
  dimensions vary.

Feedback source percentage remains available as diagnostic data but does not
force source-setting grouping for source-current/source-power feedback curves.
Restart an old analysis kernel after loading updated Python modules. Viewing an
active/rejected run requires the audit option; this does not promote its data.

## 8. Speed, verification and remaining real acceptance

Follow [MEASUREMENT_PERFORMANCE.md](MEASUREMENT_PERFORMANCE.md). Module activation
and plan preview add no hardware queries. Runtime axis scopes prevent inner
temperature/field dwell repetition. Same-setting writes are compared/read back;
the passive 2182A retains the established one post-optical verification plus
pre/post checks for each formal READ. Remaining waits use the last actual
change, and shared GPIB/6221 serial responses remain sequentially owned.

New filter settings may require buffer-history preparation, settling checks or
longer read timeout. Those costs are measurement requirements, not redundant
communication. Compare speed using unchanged NPLC/filter, sample count, optical
quality and dark policy, including failed attempts and cleanup. Fake-clock
timings and synthetic convergence do not establish physical speed or noise.

Offline tests cover plan expansion/identities, 3 T and step limits, optional
resources, direct and stabilized optics, exact dark matching, recovery/audit,
instrument setting behavior and public CLI/monitor persistence. Local complete
example parsing and synthetic public execution are covered. Target-offline and
small real module comparisons must be recorded separately; this document does
not assert that an untested PEM/CLD/environment combination is commissioned.
