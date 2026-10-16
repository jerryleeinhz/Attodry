# 2182A settings and condition boundaries

Status: offline implemented and tested. No real instrument was opened for this
change. Reference: Keithley 2182A-900-01 Rev. C (July 2022), sections 4 and 13,
[official manual](https://download.tek.com/manual/2182A-900-01C_July_2022_User.pdf).

## Configuration

The modular `[nanovoltmeter]` settings normalize into the existing strict
`[photoresponse_2182]` hardware contract. These additional fields also work in
the legacy hardware table; channel, voltage range and NPLC remain required by
that existing contract. Exact identity, address and sample limits stay in local
configuration and must not be copied from illustrative examples.

```toml
[photoresponse_2182]
# Keep this station's confirmed identity/address and sample/range settings.
channel = 2
voltage_range_v = 10.0
nplc = 1.0
analog_lowpass_enabled = false
autozero = true
front_autozero = true

[photoresponse_2182.digital_filter]
enabled = false
type = "repeating"        # "moving" or "repeating"
count = 10                # 1..100 internal conversions
window_percent = 1.0      # 0..10, percent of selected voltage range
```

Missing new fields retain the established behavior: analog and digital filters
OFF, autozero and front autozero ON. The digital filter table requires a Boolean
`enabled`; omitted type/count/window fields normalize to repeating/10/1.0.
Unknown fields and invalid types are rejected before opening any resource.
Disabled digital-filter parameters are validated and archived, but do not add
queries or writes for inactive averaging controls.

The filter window is an instrument voltage-range percentage. It is unrelated to
optical target accuracy, PM100D stability or acceptance tolerance. For example,
1% on the 10 V range is a ±0.1 V window; this is not 1% of the voltage reading.

## Commands and evidence

`CHAN<X>` below is the selected channel; this driver does not change the other
channel's filter settings.

| Setting | Set/query command | Validation |
|---|---|---|
| NPLC | `SENS:VOLT:DC:NPLC` | 0.01..50 at 50 Hz; up to 60 only with confirmed 60 Hz |
| Digital filter enabled | `SENS:VOLT:CHAN<X>:DFIL` | Boolean |
| Digital filter type | `SENS:VOLT:CHAN<X>:DFIL:TCON` | `MOV` / `REP` |
| Digital filter count | `SENS:VOLT:CHAN<X>:DFIL:COUN` | integer 1..100 |
| Digital filter window | `SENS:VOLT:CHAN<X>:DFIL:WIND` | numeric 0..10 percent |
| Analog low-pass | `SENS:VOLT:CHAN<X>:LPAS` | Boolean |
| Autozero | `SYST:AZER` | Boolean |
| Front autozero | `SYST:FAZ` | Boolean |

Configuration uses read-before-write and verifies the returned settings. Formal
`READ?` remains bracketed by complete identity/settings checks. No normal READ
rewrites autozero, filter or range settings; an external change rejects the
acquisition instead of silently correcting it. Requested normalized settings,
readback responses, timestamps and raw detector results are preserved.

NPLC above 50 performs `SYST:LFR?` before the first settings write and during
later verification. Any frequency other than 60 rejects this domain. Ordinary
NPLC <=50 configurations retain their existing query budget. NPLC applies to
both voltage channels; their nominal voltage ranges remain channel dependent.

After each formal filtered READ, `STAT:OPER:COND?` must report B8 (Filter Settled).
This is a nonconsuming condition query. No event register or status reset is
used to manufacture stability. An unsettled filter result remains raw failed
data and cannot become a valid sample.

## Dark/light and other condition transitions

`prepare_condition(guard=None)` is called once after the final relevant optical,
environment, bias or gate change and before a formal window. Recovery starts a
new boundary as well. Do not call it between samples within the same window.

The manual documents no standalone filter-clear command. The implementation
does not assume that `ABOR` or toggling the filter empties its history:

- Repeating averaging flushes its stack after each reported result. It needs no
  discarded warmup READs.
- Moving averaging retains a FIFO of conversions. The boundary takes `count-1`
  fresh READs as excluded transition data. The next formal READ supplies the
  last conversion required to displace all previous-condition history.
- Every moving warmup has fresh source/module guards before and after, bounded
  voltage validation, an error check and raw timestamps. A partial failure keeps
  all earlier warmups and its incomplete raw attempt. The coordinator archives
  transition evidence and checks fresh optical acceptance before measurement.
- Filter OFF adds no boundary I/O. Moving `count=1` also needs no warmup READ.

The driver returns boundary generation and transition evidence. Subsequent
moving outputs can share internal conversions and carry
`filter_samples_may_overlap=true`. Do not interpret consecutive moving outputs
as independent repeats. Internal filter `count` and software
`samples_per_condition` describe different levels of averaging.

## Timing and limitations

The timeout check uses a conservative planning budget:

```text
1 s + filter_count × (1 + front_autozero) × (1 + autozero)
      × (NPLC / 50 Hz + analog_filter_delay)
```

Here filter_count is 1 when filtering is disabled; analog_filter_delay is
approximately 0.125 s when enabled, otherwise zero. This preserves the old
`1 + 4 × NPLC / 50` budget with both autozero modes ON and filters OFF. The AZ
multiplier is a conservative allowance, not a claim of exact internal timing.
Moving first-window warmup cost is recorded separately from each READ timeout.
Transport overhead, window restarts and real device behavior still require
small-scale hardware validation; this estimate is not a response-time guarantee.

Legacy full verification remains 23 identity/settings queries. Enabled digital
filtering adds three settings queries per verification and one filter-condition
query per formal READ. Moving boundaries add `count-1` READ/error-check pairs,
two full settings checks when warmups are needed, and the required safety guards.
No new fixed software sleep, parallel serial query or reset is introduced.

Analog low-pass attenuation is useful primarily on the 10 mV range according
to the manual; its switch existing on another range does not guarantee the same
noise benefit. Disabling autozero/front autozero can change offset, drift and
accuracy. Their values are explicit measurement settings, never automatic speed
optimizations. Cleanup does not recall an old potentially armed setup or restore
the user's autozero choice to a different value.
