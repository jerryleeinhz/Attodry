# DC photoresponse work package

Updated: 2026-10-12. User-selected branch: `module/NKT-photonics`.

## Scope and delivered software

Photovoltage and photocurrent share a dark-before/light/dark-after coordinator.
Optical roles: SuperK EXTREME/VARIA through existing NKT SDK, or CLD1010LP through
TL4000. Electrical roles: no external excitation, 6221 DC current, or 2400 voltage
bias; fresh voltage from 2182A/2400 and current from 2400. Existing SR830 workflows
remain independent. This implementation does not use lock-ins in the photoresponse path.

| Stage | Software status | Physical acceptance still required |
|---|---|---|
| P0 physical contract | Strict mode, exact identity, wiring/limit and permission checks | Sample wiring/limits; exact CLD diode/TEC/pinout; optical route |
| P1 simulation/transport | Three modes, NKT/CLD simulations, audited VISA and RS-232 relay, fault tests | Stable shared-bus operation and actual response formats |
| P2 photovoltage | Fresh 2182A READ acquisition; no-excitation mode; direct or 6221 relay | Complete dark/light/dark voltage with real optics |
| P3 current bias | Bounded DC ramps, readback, compliance stop and zero/off cleanup | Authorized unloaded five-point 6221 test still not completed |
| P4 photocurrent | 2400 V-source/I-measure and 0 A/fixed-range voltmeter, 24-bit status checks | 2400 is on a sample; no real access authorized today |
| P5 CLD | TL4000 binding and point controller with TEC/interlock/current checks | Installed DLL ABI/firmware, exact diode and wiring, optical commissioning |
| P6 environment | Existing attoDRY driver, explicit T/Bx/Bz points, stability, 3 T and cleanup | New joint experiment commissioning; optional gate scans remain separate Three-SMU work |
| P7 data | JSON/JSONL, SQLite WAL/FULL audit, new-attempt retry, accepted-only Notebook/CSV | Target offline validation recorded in development stages; scientific noise/settling validation remains experimental |

Software implementation and offline tests are complete when the development-stage
validation passes. This is not a claim that the physical experiment is commissioned.
Power-meter/PEM integration is a separate existing work package; no watts or responsivity
are inferred here. Delta/current reversal is not automatically enabled because it changes
the measured observable and can cancel current-even signals.

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
- `photoresponse_cld.py`: TL4000 x64 API adapter, constant-current LD, stable-temperature TEC,
  interlock/protection checks, emission gating and original TEC state restoration.
- `photoresponse_environment.py`: existing attoDRY connection, vector path and stability
  code; temperature feedback enabled before target, explicit overshoot/step limits,
  source-off environment changes and actual temperature/field sampling.
- `photoresponse.py`: three-window coordinator, raw timing and linear drift interpolation.
- `photoresponse_storage.py`: separate photo_* tables; failed attempts retained; explicit
  resume creates a new full attempt, never splices windows. Unique accepted condition/series.
- `photoresponse_cli.py`, `photoresponse_analysis.py`, `notebooks/Photoresponse_Analysis.ipynb`:
  CLI with stage-specific flags; read-only accepted results and CSV/selection manifest.
- Existing SMU bounded-ramp semantics are reused; the historical three-gate configuration
  and accepted-six-SR830-readings storage contract are not repurposed as detector control.

## Verified and pending laboratory evidence

User-authorized SSH discovery previously identified a 6221 and downstream 2182A via
GPIB-to-RS-232 relay; station addresses and full identities remain in ignored local audit.
The separately authorized empty-load plan is 0.1 V compliance, 4 pA to 10 nA in five
linear points. The attempted test received an empty 6221 IDN and stopped before any
source setting/output write. A later diagnostic timed out. The user confirmed an SR830
experiment on the same GPIB bus, so all real bus operations stopped. This is a communication
failure, not passed output commissioning; shared-bus activity is a possible, unproven cause.

2400 is attached to a sample and must not be opened today. CLD has no wiring yet and
must not be opened today. Tomorrow's tests require the user-confirmed device wiring,
limits and stage authorization. Existing permission to finish offline code does not authorize
an unknown sample or diode output. No future test is scheduled automatically.

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
After the SR830 bus is released: diagnose identities/settings, perform the authorized unloaded
6221 test, then separately commission 2182A fresh readings, optical dark/light control, 2400,
CLD and the joint environment experiment. Unknown physical parameters remain hard gates.
