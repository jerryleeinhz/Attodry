# Three-channel internal-reference lock-in measurements

User-approved design: 2026-10-18. Software and file deployment verified; real commissioning pending.

## Wiring and roles

XX SR830 is the sole internal clock and optional sample-excitation source.
Its TTL OUT fans out to XY SR830 REF IN, AUX SR865A REF IN and MC2000B EXT REF IN.
XY and AUX SINE OUT are physically disconnected from the sample.
The AUX measurement input connects to the third sample voltage channel.
TTL distribution and sample excitation are independent declarations.

Keep reference_topology="internal_xx_xy"; an explicitly enabled lockin_aux adds
another receiver to the same XX-owned topology. Preserve the actual chopper
blade/masking/N/D settings. Receiving 8 Hz does not prove chopper optical frequency
or physical PLL phase. Passive T distribution needs actual input loading,
waveform/edge and lock verification at commissioning; AUX REFZ is fixed at1 Mohm.

## Configuration

Merge config/third_lockin.example.toml into the local Combination TOML and
config/third_lockin_safety.example.toml into its adjacent lockin_safety.toml.
The template is disabled; provide the actual distinct VISA address and enable
only after wiring/model/settings are confirmed. Addresses and station limits
remain local, uncommitted. Third-channel source output is never configured.

Station handoff may use config/three-lockin.local/photonics.local.toml with its
own adjacent lockin_safety.toml. The entire directory is ignored. This keeps
the optional AUX allowlist out of the shared two-SR830 policy used by standalone
commands. Relative data paths must be resolved against the original config
location when copying a station profile into this directory.

XX/XY remain the existing SR830 semantic roles. Both retain their existing phase
settings; AUX phase is explicit and independent. Model-specific fields include
SR865A peak input range, RMS output sensitivity, high reference impedance,
preserved unused sync output and verified current-status support.
A detector full scale does not establish sample/source safety.

The first three-role profile uses excitation-only points at the configured XX
frequency, fixed sensitivities, A-B voltage input, AC coupling and24dB/oct RC.
Role-specific harmonic lists1/2/3 are supported; multiple harmonics are sequential.
Frequency sweeps, bounded auto and per-harmonic/segment sensitivity overrides
are rejected before opening devices in this profile. Existing dual profiles
retain those capabilities. The optional AUX does not activate historical PEM
external-reference or XY-SINE-to-XX routing.

The command remains:
python -m attodry_control.combination_cli run --config config/photonics.local.toml
For the separate three-detector station handoff, select
--config config/three-lockin.local/photonics.local.toml instead.
Read-only progress remains combination_cli monitor; it never opens an instrument.

## Timing, status and data

Reuse the existing physical-unit adapters and audited formal/bracket sampling.
All identities/preflights pass before configuration writes. Distinct addresses
and identities, current settings, native status, reference/detection frequencies,
source protection, audit and close remain independently checked.

Each role has a monotonic settling deadline. Common optical/gate/source changes
affect all three. A role-local range/harmonic change affects that role. Wait once
for the longest remaining deadline, with the existing optical/shared-deadline
guard; do not pay three full detector waits. Already elapsed tuning time counts
only after the last relevant change. Communication remains serial on the bus.
Raw I/O timestamps, per-role read intervals and read-start span are saved.
Shared reference is not simultaneous acquisition.

Common role metadata includes active_roles and channel_labels. Additional
measurements are lockin_aux_h<n>_x_v/y_v/amplitude_v/phase_deg. Zero SR865A
X/Y has undefined phase; no fabricated angle is accepted.
Known overloading under record_continue retains raw invalid AUX and independent
healthy companions. Transport/unknown/configuration/identity/frequency errors
stop. Reference unlock remains abort in this internal profile; no new reference
exception or automatic retry is enabled.

Matched darks include all active roles, identities, reference/phase/context and
age. Per-condition captures one dark group before each condition's light reads.
Subtract each role's complex X+iY independently and derive magnitude/phase.
Independent dark-run analysis uses archived formal settings; missing/changed
evidence cannot be replaced with today's TOML. Old dual archives remain readable.

Unified plotting includes AUX axes, raw/matched/independent-dark responses and a
Three detectors · h1 comparison card. Default channel-quality exclusions remain.
The stored channel label is used on the AUX comparison panel.

## Verification and handoff

Run tools/run_guarded_tests.py: hardware imports and DLL loads are blocked.
Verify missing/wrong/duplicate devices before writes, shared remaining waits,
third-read partial audit, source protection after failures, close of all resources,
per-channel overload exclusion, dark subtraction, database load and three-panel
plotting. Preserve legacy dual SR830, mixed PEM profiles and standalone diagnostics.
Fake communication counts are not claims of real speed or settling validity.

Deploy only after separate deployment authorization. First real stage is identity,
reference/settings and TTL distribution verification, then a separately authorized
small dark/light run. Current production files and old raw scans are preserved.## 按外层扫描条件选择固定量程（2026-10-23）

`[[combination_scan.lockin_range_rules]]` 可为温度、磁场、光功率/光谱、波片角度或
已有 SMU 外层点选择完整的 XX/XY/AUX 灵敏度；第一版限固定灵敏度、激励幅度扫描。
每个外层条件必须唯一匹配一条规则，各路仍检查型号档位和本站 allowlist。换档在
首次准备 optical/lockin 轴之前核验并完成原有等待，先于光学参考转换、正式窗口
和匹配暗背景；实际温度/磁场波动不会触发换档。内部 XX 两路规则会话与三路一样
要求 A−B/AC/24 dB RC；未配置规则时原有会话保持原支持范围。
此功能独立于被拒绝的逐谐波/原有逐点量程覆盖，不改变 AUX 输入范围或激励源。
完整 TOML、模块坐标、往返磁场点序号、背景兼容性及离线预览见
[按外层扫描点设置锁相量程](LOCKIN_RANGE_RULES.md)。

