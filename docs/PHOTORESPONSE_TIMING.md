# DC photoresponse timing and optimization

Date: 2026-10-15. The operator approved single-dark subtraction and software
optimization after reviewing the plan. Hardware comparison must not disturb
any running scan. Private addresses, configs and raw logs remain ignored.

## Measured baseline

The completed standalone optical tests below are not combined electrical scan
benchmarks. Times use the canonical results timestamps, not duplicated nested
JSONL events. Older CLD/GPIB logs have command-start UTC only, so inter-command
gaps cannot be attributed entirely to bus I/O.

| Evidence | Total | Breakdown / outcome |
|---|---:|---|
| NKT 4-target test, code `14a0253`, 633 nm / 10 nm | 191.41 s | Qualification/preparation 131.67 s; formal reading/off tails 58.54 s; other initialization/cleanup about 1.20 s. All accepted. |
| CLD 10/15/20 mA control | 27.22 s | Prescribed holds 15 s; other checks, communication, setup and cleanup 12.22 s. Control passed. |
| CLD feedback, initial 10 mA | 9.55 s | Below minimum optical signal; rejected without increasing current. |
| CLD feedback, initial 20 mA | 211.46 s | First 50 uW point: 92.13 s preparation/qualification, 17.46 s formal/off. Second point failed at a changed protection pattern. Entire run rejected; cleanup confirmed. |

NKT targets 50/100/150/200 uW each restarted at 6%, taking 2/4/6/9
adjustments and approximately 15.70/25.83/37.26/52.88 s to qualify.
Its ten formal readings per point each include a configured one-second delay;
these 40 seconds are prescribed sampling delay, not redundant qualification.

CLD first qualified 50 uW at 28.1 mA after 15 adjustments from 20 mA. The next
point repeated the low-power ascent from 20 mA, then failed at 29.6 mA with
MEAS changing from 8192 to 8200 (LD limit bit 3). Optimization must not expand
the acknowledged diagnostic tuple or the 30 mA bound. Reusing a starting
current cannot establish that higher targets are reachable.

## Changes and applicability of reference code

Reference: `accacb089edc5690f71d010c46f31faa053c6867`, delivery
`e2d66effc93a9eaac8fa88b81e1d6a57bd30b49e`, on
`origin/codex/integration-four-module-scan`.

1. Environment qualification is scoped to axis entry, including the complete
   outer coordinate prefix. Inner leaves retain actual readbacks and quality
   flags; existing drift/safety rejection remains strict. Outer repeats/revisits
   requalify. No inner temperature or field qualification loop is added.
2. Dark-first optical entry qualifies once and owns illumination throughout
   inner bias/SMU leaves. Electrical sources remain off during feedback. Only
   after optical qualification is electrical configuration checked and output
   permitted. Dark/light subtraction matches electrical indices and the scan
   visit, retaining its baseline ID and age. This changes the observable from
   interpolated dark/light/dark to a single reference; it is opt-in.
3. The reference's monotonic remaining-wait principle is used with this branch's
   configured sample-response time. The last confirmed emission change resets
   the deadline, including every feedback re-enable. For 6221/2400 the final
   setting completion is recorded before driver waiting/checks, so elapsed work
   is credited. Pinned Three-SMU waits stay intact, with a conservative timestamp
   after preparation because its reused driver lacks that callback.
4. One full 2182A verification contains 23 serial queries. Each query currently
   waits at least 0.1 s: at least 2.3 s per full verification, excluding bus and
   ADC time. PM-only guards now check fresh source/relay safety without claiming
   passive 2182A configuration verification. Full detector checks remain at
   acquisition boundaries. The exact post-READ verification is returned directly
   to the enclosing electrical state, eliminating one immediately repeated
   verification. 2400 guards stay complete because it can source into the DUT.
5. PM preparation captures again only if a setting was written. The first
   complete identity/sensor/settings/status check is still required; each
   write retains OPC and post-setting verification. PM READ retains its before
   and after readiness checks. The SDK already skips unchanged NKT writes;
   its read-before-write logic is reused rather than duplicated.
6. Optional previous-actuator initialization follows the existing optical point
   session's `previous` concept in `integration-photonics-nonlinear-hall`.
   It is adapted for current, power and ND coordinates, same-scan only. It
   reuses no qualification proof. Nonlinear CLD response is not replaced with
   a proportional current-power assumption.

SR830 sensitivity/reserve transitions and time-constant parameters do not apply
to 2182A. SEND/ENTER keeps one shared lock and uncertain replies still poison
the session; polling delay and GPIB termination are unchanged. No concurrent
transactions are introduced on the shared controller.

## Offline workload comparison

Same synthetic light targets and electrical leaves, default point initialization:

| Fixture | Protocol | Light leaves | Electrical reads | Optical qualifications | PM READs | Adjustments |
|---|---|---:|---:|---:|---:|---:|
| NKT | Legacy dark/light/dark | 8 | 72 | 8 | 104 | 8 |
| NKT | Single dark per scan | 8 | 36 | 4 | 76 | 4 |
| CLD | Legacy dark/light/dark | 6 | 36 | 6 | 93 | 15 |
| CLD | Single dark per scan | 6 | 18 | 2 | 47 | 5 |

These are workload counts, not real speedup percentages. Simulated zero-latency
I/O cannot predict station wall time. The NKT real adjustment sequence suggests
previous initialization may avoid roughly ten steps (estimated 45-60 s on that
route), but this remains an unvalidated estimate, dependent on target order and
response. One fewer full relay verification saves at least 2.3 s of prescribed
polling per electrical READ. Formal response settling requirements are retained.

## Timing evidence and validation

Records include inclusive runner phase timings (`optical_prepare`,
`power_qualification`, `electrical_begin/prepare/state/acquisition/cleanup`,
`environment_prepare`, `settle`, `audit_event`) plus PM sample/transaction and
VISA transaction timings. Relay query timing separately includes SEND, polling
and ENTER, with requested polling delay counted. VISA transaction duration ends
before the audit callback. Nested timings overlap: do not add them all to obtain
wall time. UTC is for correlation; monotonic clocks measure duration/deadlines.

Offline tests cover ordering, matched signed V/I differences, selected SMU
coordinates, repeated environment qualification, fresh dark on resume, actual
source changes, remaining waits, slow serial forwarding, 2400 settings drift,
PM preparation, dark/read/power/transition/final-close faults, raw retention,
SQLite accepted promotion and CSV selection. Original pinned SMU files and
their provenance hashes remain unchanged.

Hardware comparison should use the same route, target sequence, detector
settings and limits; report qualification/adjustment counts, wall time, formal
power variation, electrical transient and cleanup independently. At least one
small sample time trace is needed to validate response/recovery delays. Floating
2182A overload is communication evidence only. A gate-wired 2400 cannot validate
channel photocurrent. Existing CLD protection state must be resolved before any
new output attempt.

Deployment and new hardware access remain separate from offline correctness.
Only passive process/file inspection is allowed while ownership is unresolved;
unknown Jupyter kernels may hold instruments. Do not update an active checkout,
clear the bus, consume other sessions' replies, or stop an unrelated process.
