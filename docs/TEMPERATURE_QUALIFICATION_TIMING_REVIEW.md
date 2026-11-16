# Temperature qualification timing review (2026-11-16)

This review reads existing archives only. It changes no dwell, tolerance, PID,
temperature target, field limit or instrument setting. Completed qualification
windows in failed executions remain separate from later successful executions;
they do not turn the failed attempt into accepted data.

The reviewed run is
`20261113T225655511464Z_6221_T46p7-121p7K_0p1-1p5uArms_300-1300Hz`.
Its archived numeric coordinates, rather than the historical run-name text,
select the actual temperatures. Across its executions there are 53 completed
qualification windows. Their archived criteria are target mode, 60 s dwell,
0.5 K target tolerance and 0.3 K peak-to-peak range. Replaying the sampled
windows with the current evaluator reproduces all 53 recorded completion times.
Incomplete waits without a completed-window event are outside this comparison.

## Observed waiting

| Execution segment | Completed windows | Total qualification elapsed | Time after first target-band entry |
| --- | ---: | ---: | ---: |
| Initial, later failed | 2 | 355.2 s | 120.4 s |
| First productive recovery, later failed | 29 | 19,327.1 s | 14,401.2 s |
| Final successful recovery | 22 | 21,114.3 s | 15,577.4 s |

The last segment's exclusive temperature qualification stage is 21,062.6 s
within 26,662.6 s overall (about 79%). Raw window elapsed includes synchronous
read/audit work, so it is not identical to exclusive stage time.

For those final 22 windows the median first target-band entry is 234.1 s;
median completed qualification is 998.8 s. First arrival alone is insufficient:
every sampled value in the trailing dwell, including its coverage predecessor,
must meet both target and range requirements at the same time.

| Target | First target-band entry | First target-only dwell | First range-only dwell | Joint qualification |
| --- | ---: | ---: | ---: | ---: |
| 152 K | 173.1 s | 1,071.9 s | 359.8 s | 1,071.9 s |
| 157 K | 245.4 s | 1,091.5 s | 436.6 s | 1,091.5 s |
| 197 K | 254.5 s | 314.7 s | 442.7 s | 1,072.0 s |
| 252 K | 197.2 s | 745.2 s | 370.4 s | 745.2 s |

For 15 of the final 22 windows, the first complete target-only dwell coincides
with final qualification. For the other seven, target-only and range-only
windows each occurred earlier, but did not coincide until later. None is
explained by the first range-only dwell occurring last. These are overlapping
diagnostics, not additive causes or a prediction that removing one criterion
would save the corresponding time. A stable plateau away from the target can
pass the range-only check and must still be rejected by the target check.

This evidence does not support automatically widening `stable_range_k` as a
general solution. Review the recorded sample/VTI/heater traces for departure
from the target band, overshoot and slow return. Any PID, ramp or acceptance
change needs a separate bounded validation; communications deduplication alone
will not remove the observed hundreds of seconds of thermal waiting.

## Reproduce without hardware

```powershell
python -m attodry_control.temperature_qualification_analysis --database PATH_TO_SCAN_SQLITE --run-id EXPLICIT_RUN_ID --output NEW_REPORT_JSON
```

The tool uses SQLite URI `mode=ro` and `query_only=ON`, materializes only the
execution plans and completed qualification events in one transaction, then
closes the connection before replay. It refuses to overwrite the report file.
It retains condition/attempt/execution identities and resolves criteria from
each execution's archived plan. No live polling or instrument imports occur.

The replay covers the sampled temperature criteria only. Control, field,
overshoot, response, communication and cleanup checks are outside its scope;
`qualification_certified=false` is always recorded. Historical evaluator
differences or missing evidence must be investigated rather than treated as a
new permission to acquire or recover.
