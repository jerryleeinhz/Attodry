# DC photoresponse work package

Updated: 2026-10-13. User-selected branch: `module/NKT-photonics`.

## Updated operator acceptance contract (2026-10-13)

- P0/P1 reuse the station and optical configuration in LK_setup's
  `Integration-photonics`; the operator explicitly confirmed wiring and isolation.
  SuperK code/configuration reuse does not require another real validation.
- P2/P3 require instrument control without a sample, not a valid scientific
  photovoltage/current dataset. Fresh overload replies and empty-load compliance
  remain rejected raw measurements; their correct handling can pass control tests.
- P6/P7/P9 and Three-SMU joint scanning require reference-source reuse and offline
  acceptance only. No extra real SMU, temperature or magnetic operation is needed.
- P5/P8 real CLD/PM control is authorized. Authorization does not turn asserted or
  unresolved protection into a passing measurement; current limits stay 30 mA.

## Scope and delivered software

The complete user goal, reconfirmed on 2026-10-13, is **combination-based optical
and electrical acquisition with measured-power feedback**. NKT varies wavelength
and optical power; CLD holds its diode wavelength and varies optical power through
LD current. PM100D measures W at its explicitly named measurement plane and feeds
bounded source adjustments until the selected power target is stable. Each formal
electrical window retains fresh detector readings, optical power evidence and
6221 settings/status when that instrument is selected. The 6221 may supply DC
excitation or serve only as the 2182A communication relay.

Combinations select optical points, target powers, bias points, repeats and optional
temperature/field points. Every leaf condition records its requested coordinates
and actual readings; omitted hardware is not opened. Failed formal windows remain
raw audit and are not assembled into an accepted condition. Acquisition is
sequential with measured timestamps, not hardware-triggered simultaneous sampling.

**Scope correction:** the earlier P1-P7 completion claim covered the open-loop
dark/light/dark implementation only. PM100D feedback and the complete combination
entry point were omitted from that scope, which did not satisfy the user goal.
They are core requirements, not a deferred PEM work package. Integration is
offline complete using the already implemented PM100D and power-window strategies from
`codex/pem-module`, plus the existing combination design. The separate branch's
historical NKT acceptance does not commission CLD or this experiment's optical path.

Photovoltage and photocurrent share a dark-before/light/dark-after coordinator.
Optical roles: SuperK EXTREME/VARIA through existing NKT SDK, or CLD1010LP through
USB/VISA SCPI or TL4000. Electrical roles: no external excitation, 6221 DC current, or 2400 voltage
bias; fresh voltage from 2182A/2400 and current from 2400. Existing SR830 workflows
remain independent. This implementation does not use lock-ins in the photoresponse path.

| Stage | Software status | Physical acceptance still required |
|---|---|---|
| P0 physical contract | Operator-confirmed wiring/isolation and Integration configuration reuse | Runtime protection/readback still must pass; unspecified diode model remains allowed |
| P1 simulation/transport | Three modes, NKT/CLD simulations, audited VISA and RS-232 relay, fault tests | Stable shared-bus operation and actual response formats |
| P2 photovoltage | No-sample control lifecycle passed: configuration, fresh READ and cleanup | Floating-input overload is retained, not accepted as scientific data |
| P3 current bias | Five output-off current settings/readbacks passed; output protection stop and zero/off confirmed | Actual five-point current-delivery sweep was not passed; outside the revised control-only criterion |
| P4 photocurrent | 2400 V-source/I-measure and 0 A/fixed-range voltmeter, 24-bit status checks | Station 2400 is gate-bottom/source; channel detector wiring and acceptance pending |
| P5 CLD | USB/VISA SCPI or TL4000 and shared TEC/interlock/current controller | Passive 26 C TEC stability verified; connection diagnostic qualification still blocks automatic LD acceptance |
| P6 environment | Existing attoDRY driver/config reuse, explicit T/Bx/Bz points, stability, 3 T and cleanup | Offline acceptance is sufficient under the revised operator scope |
| P7 data | JSON/JSONL, SQLite WAL/FULL audit, new-attempt retry, accepted-only Notebook/CSV | Target offline validation recorded in development stages; scientific noise/settling validation remains experimental |
| P8 measured-power feedback | Offline complete: reused PM100D, PowerWindow, NKT strategies and bounded CLD current actuator | PM identity/sensor, current measurement plane, range/calibration and new optical-path acceptance |
| P9 combination entry point | Ordered optical/power/bias/environment/SMU/repeat coordinates, full leaf records and reused Three-SMU | Offline acceptance is sufficient; no hardware synchronization inferred |

P8/P9 now extend the earlier scoped implementation with PM100D feedback and
combination integration, bounded CLD current feedback and formal-window drift rejection.
Validation: 568/568 full local tests, including 129 photoresponse and 24 PM100D tests;
actual CLI simulation and accepted CSV checks passed for NKT 8 and CLD 6 conditions.
Physical experiment commissioning remains separate. No watts or responsivity
are inferred from source settings. Delta/current reversal is not automatically enabled because it changes
the measured observable and can cancel current-even signals.

## Required measurement contract

| Selected role | Commanded coordinate | Actual evidence retained |
|---|---|---|
| NKT | Wavelength/filter bandwidth plus source level or ND actuator | Filter/source readback, emission/protection, PM100D W before/after electrical read |
| CLD1010LP | LD current actuator; diode nominal wavelength is fixed | LD I/V, TEC actual temperature/status, emission/protection, PM100D W |
| PM100D | Wavelength correction and permitted range/averaging | Fresh READ sample, acquisition times, sensor/console identity, measurement plane, stability evidence |
| 2182A | Channel/range/NPLC configuration | Fresh measured voltage, status and acquisition time; current remains null |
| 6221 DC | Approved current points and voltage compliance | Programmed current/range, compliance/output/status; measured current remains null |
| 6221 relay only | No excitation/output setting | Query-only host settings/status alongside 2182A readings; no source-output controller |
| Optional 2400 channel detector | Source-drain voltage bias and current compliance | Measured V/I and status; gate-source wiring cannot be labelled channel photocurrent |
| Optional attoDRY | Temperature and <=3 T vector targets | Actual temperature/Bx/Bz before/after readings and stability/protection |

An optical target in W is independent of the actuator setting. Feedback must not
infer a universal linear power/current calibration, increase light when the meter
cannot establish a valid signal, or accept saturated/uncertain meter samples.
Changing wavelength requires updating and verifying PM wavelength correction with
emission off. Formal acquisition uses a new stable window after any adjustment;
dark measurements do not cause automatic search for light.

## Module ownership and reuse

- `nkt_config.py`, `nkt_control.py`, `nkt_sdk.py`: existing optical point sessions and
  SDK; no second implementation of source/filter commands. Real LLTF remains blocked.
- `photoresponse_config.py`, `photoresponse_hardware.py`: strict active-mode contracts,
  explicit role/resource ownership and orchestration. Inactive device tables do not open devices.
- `photoresponse_transport.py`: serialized SEND/ENTER relay on a shared host session,
  exact-address VISA locks, raw response auditing, XON/XOFF parsing and timeout poisoning.
- `photoresponse_keithley.py`: passive query contracts; FETCh remains stale diagnostic data.
- `photoresponse_devices.py`: authorized writes/fresh reads, no reset/recall, bounded steps,
  source preflight at zero/off, readback verification and independent cleanup attempts.
- `photoresponse_cld.py`: USB/VISA SCPI and TL4000 x64 adapters, constant-current LD, stable-temperature TEC,
  interlock/protection checks, emission gating and original TEC state restoration.
- `photoresponse_environment.py`: existing attoDRY connection, vector path and stability
  code; temperature feedback enabled before target, explicit overshoot/step limits,
  source-off environment changes and actual temperature/field sampling.
- `photoresponse.py`: three-window coordinator, raw timing and linear drift interpolation.
- `photoresponse_combination.py`: selected modules, ordered Cartesian axes, duplicate
  point indices and preflight environment-wrap checks; paired optical/environment rows.
- `pm100d.py`, `power_feedback.py`, `power_config.py`: reused PM driver and stability
  strategies, strict power/actuator domains and independent PM authorization/ownership.
- `photoresponse_power.py`: off-state wavelength correction, bounded tuning before
  the formal light window, fresh power brackets, drift rejection and raw PM audit.
- `photonic_smu/`: three unchanged reference source files pinned to `016d741`,
  with SHA-256 provenance and reference tests; legacy independent SMU APIs remain.
- `photoresponse_smu.py`: thin point-session adapter, active-role selection,
  exact identity/resource conflict checks, per-leaf source/readback/leakage and cleanup.
- `photoresponse_storage.py`: separate photo_* tables; failed attempts retained; explicit
  resume creates a new full attempt, never splices windows. Unique accepted condition/series.
- `photoresponse_cli.py`, `photoresponse_analysis.py`, `notebooks/Photoresponse_Analysis.ipynb`:
  CLI with stage-specific flags; read-only accepted results and CSV/selection manifest.
- Existing SMU bounded-ramp semantics are reused; the historical three-gate configuration
  and accepted-six-SR830-readings storage contract are not repurposed as detector control.

## Verified and pending laboratory evidence

Real electrical commissioning and the 6221 relay-framing corrections are recorded
in the run guide. The user confirmed the 2182A input is floating; its overload is
retained and excluded from acceptance. The 6221 empty-load test reached 0.1 V
compliance at 0 A before the first current point; zero/off was confirmed.

The user clarified that the 2400 connects bottom gate (SiO2/Si) to source. This is
gate bias/leakage. Its rejected 0 V / 10 nA trial does not validate channel
photocurrent. The instrument was returned to zero/off/HIMP; do not run the channel
detector profile until source-drain detector wiring is independently confirmed.

CLD USB/VISA is now a production backend using the existing controller/transport.
No native driver DLL is needed for this choice. Read-only real snapshots were
verified after 96 target offline tests. Current limit and operator-set stored
current are 30 mA; passive reads confirmed LD off. The operator confirmed a beam collector and explicitly unspecified diode
model; model metadata is retained truthfully. Voltage measurement capability is
not a cached diode voltage rating. Constant-current operation needs no separate
LD voltage setpoint; `max_voltage_v` is an optional extra software monitor. Omission
records null and supplies no guessed bound. Invalid/overload readings still reject
operation, and a configured positive threshold remains enforced.

The initial TEC-only attempt briefly enabled TEC and stopped on raw connection
indications, without enabling LD; both outputs were confirmed off. The operator
subsequently reported normal manual emission and selected a 26 C target. Updated
passive checks passed a five-second thermal dwell with TEC on and temperature
trip clear. CONN=1/MEAS bit13 persisted without SCPI errors. Withdraw the prior
proven physical cable/socket-fault claim, retain raw diagnostics as unqualified,
and continue to block automatic LD enable while they remain asserted/unreadable.
The CLD-inapplicable generic native cable API is no longer used to label them.
Temperature windows are validated as full spans and explicit LD temperature trips
are checked before and during emission. Do not clear/reset or invert diagnostics. All raw trials and host-memory recovery
records remain ignored. No future hardware test is scheduled automatically.

## Manufacturer contracts

- [6220/6221 reference manual](https://download.tek.com/manual/622x-901-01%20%28C%20-%20Oct%202008%29%28Ref%29.pdf)
  and [Rev B reference](https://download.tek.com/manual/622x-901-01(B-Jun2005)(Ref).pdf):
  explicit DC source, waveform/sweep abort, serial SEND/ENTER and status conditions.
  Measurement bit 1 means interlock asserted (output permitted), bit 3 is compliance.
  Minimum compliance is 0.1 V. Settings readbacks are not actual delivered current.
- [2182A manual](https://download.tek.com/manual/2182A-900-01_May_2017.pdf):
  channel 1 DC voltage, bounded fixed range/NPLC, processing disabled and fresh READ
  acquisition. Overload sentinels and malformed/partial responses reject the attempt.
- [2400 manual](https://download.tek.com/manual/2400S-900-01_K-Sep2011_User.pdf):
  section 3 measure-only, section 18 reading format/status, source/measurement and
  HIMP output-off configuration. The 24-bit reading status includes separate real
  compliance and range compliance; both reject formal data.
- [Thorlabs official examples](https://github.com/Thorlabs/Current_And_Temperature_Controller_Examples):
  CLD1010 ctypes example and TL4000 XML reference supply API names/semantics. Binding
  follows 64-bit VISA types with init idQuery=true, reset=false. Fake ABI tests cannot
  replace matching the installed DLL/header and actual CLD firmware during commissioning.

## Delivery and next hardware steps

Use [运行指南](../PHOTORESPONSE_GUIDE.md) and the tracked placeholder templates. Preserve
local hardware addresses/data and pre-existing PM100D/PEM notes outside this feature's commit.
The current branch is synchronized without merging main. Remote offline validation uses a
fresh temporary folder and the existing `lyr` environment, not an overwrite of the active lab checkout.
The revised scope does not require a sample/load or new environment/SMU commissioning.
Current real blocker is CLD protection-readback qualification before automatic LD
enable and illuminated PM feedback. Sample scientific acceptance remains a future
experiment rather than a software-completion requirement.
