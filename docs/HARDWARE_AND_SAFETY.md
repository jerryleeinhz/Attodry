# Hardware and safety guide

## 2026-10-24 Positioner 启动幅频初始化（本地离线代码）

本次批准仅实现代码和硬件阻断验证，未部署 LK_setup、连接仪器或重新启动扫描。
此前 positioner 失败、零样本和 manual/unknown 状态仍按原始记录保留。
用户批准提交并推送 origin；LK_setup 正有进程运行，本轮不部署或干扰该进程。

`open/preflight` 仍只读，可记录有限完整的初始幅频差异；先取得原有完整稳定原点、
冻结一次相对起点，并验证完整逐轴/循环/返回路径，才允许初始化。重新确认 XYZ
身份、位置在限、三轴输出 OFF、静止、传感器及 EOT 健康后，仅按 TOML 写不同的
幅度/频率，每项一次、最多六次。每次写前须新鲜三轴状态、光 OFF/环境守卫及
共享 deadline 通过；每项立即读回并严格核验，最终完整 XYZ 读回通过后才准备
目标和输出。SDK 分辨率为 1 mV/1 Hz；不静默取整，读回不符即终止，不猜测硬上限。

写入、读回、审计、守卫或超时错误终止，不重试、不回滚、不自动重开；运行中的
幅频变化仍严格失败，不自动修复。只开放该一次启动配置阶段，不改预设/LUT/DC、
Home/坐标、flash，不调用 `ANC_saveParams`。原始写入/读回和部分未知均保留；
不把设置 ACK、断开成功或输出 OFF 等同位置到位。完整契约见
[POSITIONER](modules/POSITIONER.md)；以下 2026-10-23 条目保留首版历史边界。

## 2026-10-23 操作者批准的显式磁场 ACK 容差

setpoint_ack_tolerance_t 默认0.0001T，允许显式有限正数至
max(默认0.0001T, 当前实际readback/field_stability容差)。保留历史窄实际容差配置的默认
兼容；本次选择0.0002T且实际容差0.0015T。该关联不是额外物理场余量，不扩大命令/实际
轴上限、双轴合场、混合拐角、非工作轴或实际零场判据，也不豁免错误/控制/通信未知。
ACK用于确认SDK目标寄存器，还用于计划起点/转场、正式窗口目标保持及回零目标一致性；
真实场到达和连续判稳独立执行。扩大ACK不能只靠DLL返回0或目标值接近即取得采样资格。
新增扫描须全站空闲及原清理已核、私有配置备份和精确单次启动，不在活动采集中改参数。
后续容差或等待调整仍须有明确的数值/稳定证据；未知故障不能靠不断扩大参数或自动重发解决。

离线验证：132项相关硬件阻断回归通过（24项/6.012s与108项/23.264s）；新7项覆盖
本次目标读回偏差及实际到场/稳定、回零与硬边界仍独立。目标机验证与重新启动尚未执行。

## 2026-10-23 Positioner 运动实现边界

位移台生产代码已完成离线实现；不代表获得真实运动授权或已测定安全行程/间隙。
ANC350 RES 的 SDK 0/1/2 对应用户确认的 X/Y/Z，坐标单位 um，SDK 米；ANC300 精扫
状态不自动相加。必须显式填写范围、最大单步、容差、顺序、固件/DLL/预设/LUT/幅频、
坐标系确认和标定条件。无输出参数/预设自动修复，无 Home/清零/自动关闭输出。

连接时三轴须输出 OFF/静止/无传感器或 EOT 故障。moving=false 不能证明原生 auto 已关闭，
故不接管初始 ON。完整原点稳定窗和全部路径通过后，OFF 状态先以 fresh actual 写目标，
随后 targetGround(false)/targetRange/输出准备，再完整原点判稳；ACK 只表示命令完成，
不是未暴露的目标/range getter。普通 hold 保留输出，下次 run 前需操作者释放。
每步确认光 OFF 并保留环境守卫；无光路所有权必须明确 external_light_off_confirmed。
实际 XYZ、焦面误差、完整稳定窗和采样前后守卫决定资格，不能仅靠 target_reached。
漂移/非有限/未知/通信/审计错误 fail closed，拒绝不确定重发；最后确认位置与未知状态
分开保存。停止本 session 已发起的 auto、核验静止、断开彼此独立，失败不回原点。

焦面拒绝凸包外目标与逐轴路径；只证明配置几何，不保证安装物理间隙。温度声明存在时
须新鲜温度读回匹配，没有声明也不意味着温变下焦面仍有效。三点零残差不认证聚焦精度。
同步 SDK 调用无可配置取消接口：共享截止时间防止迟到结果取得资格，不能承诺中途抢断
阻塞的原生调用；禁止用 kill 代替有证据的停止。详见 [POSITIONER](modules/POSITIONER.md)。

## 2026-10-22 开放 readback_tolerance_t（本地修复，待当前扫描结束部署）

操作者明确要求 readback_tolerance_t 可编辑，移除解析器/策略/驱动构造/控制接管的1.5mT固定上限。
该字段接受有限正数，沿用统一到场/采样目标误差、控制接管、非工作轴残余和回零范数判据；
完整稳定dwell、range及所有读取/写入/核验/等待调度保留。配置容差不扩大物理轴上限或双轴3T边界。
非工作轴上限还受配置本轴硬上限约束。旧 field_tolerance_t/ACK 契约保留，历史快照不重解释。
详情见 [磁场组合选项](MAGNETIC_COMBINATION_OPTIONS.md)。

LK_setup 当前采集19356与monitor18444于16:59:33/17:00:09UTC创建；本次只读发现时仍在运行，
用户已明确选择保留当前扫描，结束后再部署。不得修改活动源码/计划/配置，不Stop/kill/中断/重启。
先保存本地修复/测试/提交与受控部署包；之后固定run只读跟进，真正terminal、原身份退出、monitor退出、
所有DB终态/Notebookidle/全部owner释放/哈希通过后才部署文件并运行硬件阻断验收。
不连接仪器，不启动扫描或改目标，不把普通等待当故障，进度无变化保持安静。
证据及后续部署结果：.test-tmp/readback-open-20261022/。当前用户TOML值不自动替换，部署时保留其修改。

## 2026-10-22 当前磁场组合契约（操作者批准）

当前光学组合中的 magnetic 轴使用 [磁场组合选项](MAGNETIC_COMBINATION_OPTIONS.md)。
该新版本替代本页历史光学条目中“纯轴也一律合场3T”及“整轮用过X/Z就属vector”的限定。
单轴按配置标称上限；双轴同时非零才额外合场<=3T；精确命令/转场实际场/非工作轴残余均硬保护，
不增加旧读回余量。回零沿用前非零命令上下文并验证实际零。较低TOML上限仍有效。
record_continue只放行完整资格后的目标/范围质量警告，故障/未知/控制或目标变化仍中止。
原判稳、I/O、转场中间点、等待、清理不提速。以下 dated standalone/实验/光学条目保留其历史适用范围。

## User-authorized CLD startup recheck (2026-10-19, software-only)

Only the first complete snapshot immediately following an owned LD ON command
may classify a finite voltage in [-0.0004,0) V as a pending startup observation.
After all other existing guards pass, wait once for two seconds with fresh full
state guards at the existing poll interval; then require the original strict
nonnegative voltage and all existing point guards. No sample/power qualification
occurs during this wait. The classification is bounded measurement handling,
not a permissible physical reverse-voltage rating. Normal, formal, preflight,
OFF and cleanup guards keep their existing contracts. Larger/nonfinite voltage,
overcurrent, temperature/interlock, settings/identity, changed diagnostics,
communication or unknown-state failures do not become a delayed retry. Preserve
raw values and the startup-wait event; never clamp or take absolute voltage.
No additional LD enable write or automatic scan restart is allowed. One shared
deadline includes the wait and its I/O; late evidence cannot qualify the point.
Software-only tests/deployment do not claim illuminated hardware acceptance.


## User-approved optional third detector (2026-10-18, offline stage)

The internal-XX profile may explicitly enable lockin_aux=SR865A. XX TTL OUT
fans out to XY SR830, AUX SR865A and the chopper; AUX has a separate sample
voltage input, high-impedance external TTL reference and disconnected SINE OUT.
XX remains the sole source. Do not activate the historical PEM/XY-SINE cascade.
Passive fan-out waveform/loading and actual lock need separate commissioning.
All three identities/preflights precede writes; unknown/communication/settings
faults stop the group. Per-role overload invalidates that role's raw/delta data;
reference unlock remains abort. Shared guarded remaining waits, serial response
ownership, independent source protection and complete resource close/audit apply.
See [three-channel configuration and limits](THREE_LOCKIN_GUIDE.md).
The subsequent user-authorized file deployment is recorded in PROJECT_HANDOFF.
New instrument connection, configuration writes and illumination remain separate
real commissioning stages; deployment does not certify the TTL distribution.

## Current dual-SR830 optical profile and offline boundary (2026-10-17)

The current operator-selected profile uses two SR830 units: XX internal reference
TTL OUT -> XY REF IN and Thorlabs optical chopper EXT REF IN. Both detectors
measure h1; chopper numerator/denominator are 1/1 for this planned topology.
The chopper's multiplication/division is distinct from the detector's harmonic.
Verify the controller identity, exact blade/illuminated track, allowed mechanical
frequency and actual reference selection before real writes. The nominal PEM
50 kHz setting is not a chopper-frequency default. No real chopper commissioning
or control is performed by this software delivery.

XX SINE OUT may excite the sample or be physically disconnected. The declaration
is separate from TTL clock distribution; a retained instrument setpoint is not
proof of sample excitation or sample zero. XY SINE OUT remains disconnected.
PEM is optional in optics; existing explicit PEM-source profiles and their
different model/wiring/cleanup semantics remain historical supported paths.

NKT source percentages retain current/power mode meaning. Mode changes require
explicit `nkt_source.allow_control_mode_change=true`, confirmed healthy OFF
state and the configuration stage after all preflights; preflight itself writes
nothing. Unknown/active source state blocks switching. Existing ND write
qualification remains independent. CLD uses amperes and the diode's declared
nominal wavelength; it cannot tune wavelength or imitate a VARIA bandwidth.
Connection diagnostics, TEC/interlock/current protections and OFF/zero/TEC
cleanup verification remain required. Operator acknowledgments apply only to
their exact recorded pattern and never generalize an unknown connection state.
The native TL4000 SDK is an exception to read-only discovery: even reset=false
initialization writes status configuration/clear commands, and some getters
consume the error queue. It requires explicit write authorization before DLL
load/init; use a separately authorized VISA identity path for read-only diagnosis.

Fresh power acquisition keeps complete PM settings/status captures immediately
before and after each READ. Only duplicate outer captures for that same READ
are removed. Source/modulator/gate/environment guards remain fresh and serial.
Matching a previous setting does not authorize skipping a status check.

Formal windows freeze optical/electrical actuators. Optional bounded recovery
applies only to an ordinary measured-power deviation and repeats every module
with new before/after brackets. Mixed faults, hard limits, settings/identity,
communication/unknown state, audit failures and unverified cleanup stop recovery.
The shared deadline covers retuning and the final READ; a late final reply cannot
promote the window. Rejected/partial windows and original primary/cleanup errors
remain available independently from accepted data.

Combination `run` authorizes only the modules selected in the validated TOML.
Optical authorization/route CLI flags are optional legacy arguments; the backend
API still requires explicit authorization. File hash, station ownership, identity,
route declarations, physical limits and independent cleanup checks remain.

Dark acquisition turns the laser off (or uses separately verified shutter control)
while preserving reference/modulator timing. A stationary chopper may leave the
beam open; motor stop is never certified optical darkness. Matched darks bind
condition visit, gate/environment, excitation, reference/harmonic and phase
context plus finite age. Invalid dark/light channels cannot produce qualified
differences. Subtract X+iY and then derive delta magnitude/phase; zero complex
difference has undefined phase and must not become a fabricated angle or NaN.
Enabling per-gate matched backgrounds adds explicit dark visits even under the
otherwise continuously illuminated gate policy.
Optional `capture_mode="once_before_scan"` captures one group before the first
formal illuminated sample. Optical coordinates may change, while electrical,
environment and reference/phase conditions still match. Expired/mismatched
backgrounds suppress qualified differences without changing raw light quality
or causing a hardware retry. A new run constructs a fresh background owner.
Differences are qualified per channel; companion overload cannot qualify an
invalid channel or exclude a separately proven healthy one. Independent dark-run
analysis requires archived OFF/settings evidence and an operator declaration of
unchanged wiring and phase continuity; missing phase readback is not inferred.

The active photonics profile preserves `sqrt(Bx^2+Bz^2) <= 3 T` for requested,
commanded and accepted actual fields. Historical single-axis limits/margins
below describe other profiles/archives and do not expand this boundary. Unknown
field/voltage/output after communication loss requires the existing manual
verification; OFF/source-zero alone is not proof of actual gate discharge.

The original 2026-10-17 implementation stage was local, offline and hardware-blocked.
Subsequent publication, deployment and authorized read-only identity checks are
recorded separately in PROJECT_HANDOFF.md; they do not authorize an illuminated
scan or settings writes. No running acquisition may be altered by deployment.
Real read-only checks and illuminated A/B remain separately authorized stages.

## Photonics timing optimization boundary (2026-10-15)

The approved offline optimization reuses per-role monotonic settling deadlines.
Actual source/harmonic or sample-condition changes restart the relevant deadline;
only elapsed time after that change can count. Formal entry checks the complete
configured TC-times-multiplier duration even if qualification was skipped.
Optical tuning finishes before its signal-change notification; SMU settings
finish before their notification. Existing guarded optical dwell and SMU delay
can then overlap detector settling. Reference recovery retains its shared deadline,
consecutive-good requirement, complete filter dwell and optical guard.

Temperature is qualified once per axis entry; magnetic execution already owns
arrival/dwell and its safety waypoints. Fresh formal readbacks and existing hard
temperature/field/control/communication checks remain. Duplicate/revisited/repeated
entries do not inherit old qualification. Actual-zero cleanup is unchanged.
This implementation is not real commissioning or permission to alter an active
scan. The operator separately authorized source publication and LK_setup deployment
on 2026-10-15. Real A/B still waits for confirmation; deployment must preserve any
active acquisition owner and local configuration.

## User-approved continuous illumination and gate transitions (2026-10-06)

The operator approves optical qualification before a gate loop and small-step gate
voltage transitions; the follow-up authorization permits direct gate jumps.
Explicit `combination_scan.illumination_policy="continuous_gate_scan"`
requires optical -> smu -> lockin, the existing PEM topology, one fixed excitation
point, lit power-stabilized PM measurement and only voltage-source gate roles
with optional per-channel ramps. A continuous direct gate requires explicit
`zero_readback_tolerance_v`, finite/positive and within its physical voltage limit.
Bias, current/pulse or environment axes are rejected offline. Omission retains
OFF-before-every-axis behavior.

Prepare/select the fixed electrical point while dark, then qualify the light
before the first gate write. Inner gate changes preserve illumination; later
fixed-lockin axis applications verify/reuse the setting. Per-gate electrical
settling, fresh power guards, reference checks and all formal sample brackets
remain. No target-following correction occurs inside the gate loop. Each optical
index and repeat gets its own qualification; optical changes and global cleanup
remain OFF-first.

Optional `three_smu_run.gate_bottom.ramp` / `gate_top.ramp` carries explicit
max_step_v, step_interval_s, readback_tolerance_v and timeout_s, all positive and
finite, with tolerance below half a step. These are transition/verification
settings, not replacement sample V/I limits. Every transition starts from fresh
sensed voltage, records attempted writes and validates sensed V/I, output,
current compliance and errors at each intermediate step. Held-light guards
continue during steps. Intermediates are audit records, not formal grid points.
Small discrete steps do not guarantee current-peak or hardware slew bounds.

All observed SMU compliance/limit, unknown-status and communication faults abort;
lock-in continuation policies never excuse a gate fault. Guarded direct changes
use fresh sensed V/I, source, output, compliance and error checks before their
single target write and after the shared delay, with held optical guards.
The post-write source register must equal the requested target; mismatch is a
hard failure, with actual V/I retained. No new actual-voltage target tolerance
is inferred from the separate cleanup zero tolerance.
Normal direct return-to-zero uses one zero request and fresh measured-zero
verification within the explicit tolerance before certification and disable;
configured ramps retain their measured-zero verification. Failed/untrusted
transitions prioritize output disable, preserve last confirmed values and
uncertified actual-zero state; source-zero/OFF alone is not a discharged-capacitor
certificate. Legacy non-continuous direct/no-ramp behavior is retained.
Current station direct mode carries the prior 0.05 V zero tolerance and preserves
the 10 uA current limit. No current limit is silently widened. This user-approved
exception supersedes the earlier unconditional laser-OFF/direct-point statements
only for the opted-in path. Offline/target-offline verification is not real
illuminated commissioning.


## User-approved post-tuning optical target deviation (2026-10-06)

An additional explicit `power_feedback.target_mapping = "cartesian"` expands
each approved optical setting row across the validated target-power array.
Default `paired` retains one-to-one mapping. Expansion happens offline, is
limited to 10000 optical points and preserves all source/filter/PEM constraints.
Changing the number of requested conditions does not authorize higher power,
electrical excitation or different routing. Preview exposes the expanded plan.

The operator approves approximate target tuning followed by acquisition at
actual measured power. Optional `optical_scan.target_deviation_policy` is
`abort` (omitted default) or `record_continue`. Initial target tolerance,
window/hold qualification, bounded feedback and actuator stepping are unchanged.
Only subsequent target mismatch becomes informational, including post-tuning
dwell, requalification, formal brackets and reference-recovery monitoring.
Hold optical settings; no target-following writes inside electrical acquisition.
Actual watts, requested target, deviation and independently timestamped power
brackets are retained. Off-target alone does not invalidate Vxx/Vxy.

This exception does not cover hard power limits, reduction thresholds, minimum
valid illumination, PM saturation/invalid readbacks, settings, identity,
communication, reference or cleanup faults. The common optical point session
also supplies PEM diagnostic guards; that path uses the same explicit policy.
The standalone optical retuning loop rejects the non-default policy before I/O.
Historical data remains unchanged. See the photonics combination guide.

## Isolated-XX PEM internal-reference diagnostic (2026-10-06)

The user-selected SR865A diagnostic is a separate, explicitly authorized
development path. Its real entry requires confirmed physical disconnection
of XX SINE OUT from the sample, an available/unarmed XY capture buffer and
the optical authorization/route flags. Software 4 mV protection is not
physical isolation. The station's NKT power/current, PEM, PM and model limits
remain unchanged; original combination reference guards are not bypassed.

Prepare approved internal frequency while XY is still external before
switching reference source. Unknown, communication, settings, identity,
power and ownership/cleanup faults stop the run. Existing confirmed overload
and unlock record/continue policies retain invalid raw diagnostic buffers.
Check optical guards across each buffer, turn light OFF before analysis,
protect XX, restore XY, then finish PEM and close all resources. Every failed
cleanup action stays in the final audit even if a later action succeeds.

Internal clocks supply no PEM phase or signed Hall response. R has positive
noise bias, and a longer wait cannot recover amplitude suppressed by an
inappropriate low-pass time constant. No automatic inverse-filter correction
or conversion from measured voltage to photocurrent is performed. Full
operator instructions: [PEM_INTERNAL_DIAGNOSTIC_GUIDE.md](PEM_INTERNAL_DIAGNOSTIC_GUIDE.md).

## Bounded reference-transient recovery (2026-10-05)

The operator approves `reference_transient_policy = "wait_stable"` and a new
illuminated scan with the existing approved settings. Omission defaults to
`abort`. Only identified SR830 RANGE (bit 4), valid finite reference readings
outside the selected interval, reference-pair/PEM mismatch and SR865A detection
frequency mismatch may enter recovery. This does not prove a query glitch.
Actual configuration changes, SR830 TC bit 5, SR865A configuration/filter faults,
unknown status, communication, instrument and optical/environment faults retain
hard failures. No frequency, power, source or field limit is widened.

Recovery requires three consecutive one-second good polls, real lock evidence,
verified settings and the configured filter settling. A single 45 s deadline
covers recovery and retries for the failed operation; another anomaly cannot
reset it. Optical settings and approved electrical outputs remain held while
fresh power/state checks continue. No feedback retune or optical timeout reset
is authorized by recovery. A contaminated sample is retained as rejected audit,
then the entire before/all-modules/after window is reacquired. Existing pure
unlock/overload record-continue and default analysis invalidity remain intact.

## User-selected absolute photonics reference tolerance (2026-10-05)

The user explicitly removes the software-only
`pair_tolerance_hz <= reference_min_hz * 0.001` profile ceiling. The configured
tolerance must still be a positive finite number and is the sole difference
threshold for XX/XY/PEM fundamental-reference comparisons and SR865A
`FREQDET / harmonic` versus reference readbacks in this photonics path.
Actual reference intervals and model/harmonic detection limits remain enforced;
frequency disagreement beyond the configured tolerance still aborts, independently
of the status-only reference-unlock continuation policy. Source, power, field,
identity/settings and cleanup protection retain their existing contracts.

## User-authorized photonics reference-unlock continuation (2026-10-05)

The current user explicitly authorizes
`photonics_lockin.reference_unlock_policy = "record_continue"` on XX and XY,
then another illuminated scan. This supersedes the unlock-only rejection below
for this opted-in photonics profile; omission still defaults to `abort` and
ordinary electrical acquisition is unchanged. Current and consumed latched
reference-unlock events remain recorded in setup, transitions, qualification
and formal sampling. Any unlock within a formal pair or its probe brackets
invalidates both Vxx/Vxy roles because they depend on the common reference chain;
raw diagnostic data is retained but default analysis excludes the affected pair.
The finite 1 s/45 s reference wait records unlock timeout and continues only
under this explicit policy; frequency range/pair consistency, model capabilities,
identity/settings, source amplitude/DC, unknown status, PON, synchronous-filter,
instrument/communication and optical power checks keep their existing guards.
No sample-source, current, power or magnetic-field limit changes. Separate
overload policy remains responsible only for overload faults.

## User-authorized photonics overload continuation (2026-10-05)

For the current illuminated photonics scan, the user explicitly authorizes
`photonics_lockin.overload_policy = "record_continue"` for both XX SR830 and
XY SR865A. This scoped exception also covers overload observations during
reference polling, setup, transitions and qualification. Raw current/latched
status is retained; affected formal readings are invalid for analysis, while
an unaffected companion role remains usable. Missing policy defaults to `abort`.
Reference unlock still waits only in an expected reference transition and fails
in formal acquisition. Communication, identity/settings, unknown status, PON,
instrument errors, SR865A synchronous-filter faults, frequency, excitation and
optical power checks retain their original guards. No range or power limit is
increased. Real acquisition is explicitly authorized in the current session;
the historical read-only commissioning notes below describe earlier stages.

## New photonics profile precedence (updated 2026-10-05; offline implemented)

Current user-confirmed wiring supersedes the earlier arrangement below for the
explicit `pem_xy_xx_sine` profile: PEM REF OUT -> XY SR865A REF IN -> XY SINE OUT+
-> XX SR830 REF IN; XX SINE OUT excites the sample. Both sine outputs are
physically connected, but only XX reaches the sample. XX uses sine zero crossing,
not a TTL edge. XY source amplitude/DC and unused BlazeX mode are verified and
preserved without writes; source protection/cleanup applies to XX. No external
50 ohm sample termination is present; the user also confirms the total sample/
series-resistor load is much larger than 50 ohm (`high_impedance`). Instrument
SLVL settings still do not establish the actual sample voltage.
New-profile input shield grounding is explicitly configured as float or ground;
the historical profile retains its float requirement. This stage authorizes
read-only identity/settings queries, not output changes or illuminated acquisition.
Single sequential frequency/settings readbacks are not complete lock acceptance.

The current user request creates a separate
[photonics / nonlinear Hall work package](modules/INTEGRATION_PHOTONICS_NONLINEAR_HALL.md).
For this project, preserve `sqrt(Bx^2+Bz^2) <= 3 T`, including pure-axis operation.
The historical single-Z 9 T policy and readback margins below do not authorize
their use in the new profile. The new combination profile now applies this
strict invariant to plans, transitions, actual readbacks and cleanup, with
offline boundary tests. Real integrated commissioning remains pending.

The user explicitly selects PEM REF OUT -> **external-reference lockin_xx** ->
XX sync output -> external-reference lockin_xy for this profile. XX remains the
SINE OUT excitation source/Vxx channel and XY SINE OUT remains disconnected.
The ordinary internal-reference XX contract below and its existing scan code
remain unchanged. Do not apply those setup/sweep paths to the new topology.
The user subsequently confirmed SR865A for XX, with XY still SR830, and reports
manual PEM-to-XX lock. This explicitly selected SR865A is a narrow exception to
the historical excluded-model rule for the new photonics profile. Replacing XY
with SR865A is only being considered. The approved adapter and point session
are wired into the explicit `pem_xx_xy` combination profile; local fake tests
do not certify real software acquisition or the complete physical cascade.
Its BlazeX sync mode, source wiring/load/DC, independent input range and output
scale must be verified; SR830 source/command/status assumptions cannot carry over.
PEM-profile setup and cleanup must preserve external frequency ownership.
The current task implements unified TOML and optical/electrical combination
without connecting to instruments. Source wiring/load, zero DC/mode and allowed
amplitude/cleanup values are explicit validated inputs. Fixed 24 dB/oct RC
filters require at least 10 time constants; model input and output ranges are
separate. All preflights precede settings, laser OFF precedes axis changes, and
every PEM adjustment requires XX protection then reference requalification
before restoring excitation/emission. Cleanup disables PEM only after explicit
verified XX source protection; unknown output leaves the reference active or
unknown, closes communication and requires manual verification. Formal power
drift or invalid/unknown lock status rejects the point. See the
[photonics guide](PHOTONICS_COMBINATION_SCAN_GUIDE.md).
Earlier SR830 diagnostic authorization is not treated as authorization for new
SR865A commissioning, NKT emission or full optical/electrical/environment scans.
The SR865A manual describes sync as 2.5 V on printed page 62 and +/-2 V or
0–2 V on printed page 98. Neither establishes the SR830's reliable >3.5 V TTL
high condition. Do not infer XX-to-XY synchronization from PEM-to-XX success.
Keep source/load/trigger observations and verify both stages independently.

Historical dual-SR830 P1 diagnosis completed but TTL synchronization acceptance failed: XX
remained unlocked even after PEM activation/stability. SR830 baseline restoration
was verified; PEM disable ACK is not physical-off proof. The second diagnostic
queried NKT OFF before/after with no SDK writes. No automatic retry or unreviewed
trigger/level-conditioning change; see the
[commissioning evidence](PEM_REFERENCE_COMMISSIONING_20261004.md).

## Opt-in SR830 overload acquisition policy (2026-09-29)

At the user's request, `lockin_sweep.overload_policy` may explicitly allow
continued acquisition with invalid overload readings. `abort` remains the
default. `continue_unselected` only permits an unselected instrument's overload;
`record_continue` retains selected overloaded samples for diagnostic use.
Neither option excuses communication, reference unlock, configuration/readback,
excitation/environment or magnetic-limit failures. Initial preflight/setup and
final cleanup retain their strict checks. No inferred zero after communication
loss, automatic retry, or increased limit/range/reserve is introduced.

Keep raw faults and explicit per-role validity. Acquisition completion is not
proof of clean measurements. See [operation details](LOCKIN_DAILY_OPERATION.md)
and [high-impedance XX attenuation notes](LOCKIN_XX_ATTENUATION.md).

## Magnet coordinates and limits

The operator explicitly approved a separate software readback-policy change on
2026-09-30. The following rules supersede the previous exact-zero **readback**
classification; strict nominal command limits remain. This implementation has
offline validation only. It is not factory accuracy certification, station
deployment, authorization to replay failed scans, or real commissioning.

The active software coordinate system is X/Z:

- Z is the 9 T axial coil in the factory system sheet.
- X is the 3 T transverse coil called Y in the factory system sheet.
- No mechanical rotator is controlled.

Nominal requests and exact float32 commands use the configured X/Z limits,
bounded by the 2026-09-11 X 3 T / Z 9 T envelope. The 2026-09-30 combination
follow-up removes its extra single-Z 3 T cap; whole-plan vector targets remain
within the configured resultant limit (at most 3 T):

```text
abs(Bx) <= 3 T
abs(Bz) <= 9 T
if Bx != 0 and Bz != 0: sqrt(Bx^2 + Bz^2) <= 3 T
```

The current explicit `readback_tolerance_t` configuration archives
`planned-axis-readback-v3`. It retains the following complete-plan classification;
legacy `field_tolerance_t` configurations and v2 records retain their original rules.

`planned-axis-readback-v2` fixes mode from the **complete requested point list**:

- `single_x`: at least one nonzero X target; every Z target exactly zero.
- `single_z`: at least one nonzero Z target; every X target exactly zero.
- `vector`: all other plans, including a fixed nonzero other axis, an X-to-Z
  switch across separate points, and an all-zero plan. Every vector-plan target
  satisfies the nominal resultant limit, even pure-axis endpoints.

The legacy v2 actual readback limits are separate from command limits:

| Mode | Actual readback acceptance | Other axis |
|---|---|---|
| Single X | abs(Bx) <= configured X limit + 0.0005 T | abs(Bz) <=0.0005 T |
| Single Z | abs(Bz) <= configured Z limit + margin | abs(Bx) <=0.0005 T |
| Vector | each axis within its configured limit + margin; hypot(Bx,Bz) <= configured vector limit + 0.0005 T | Both raw axes retained |

Axis margin is 0.0005 T only for nominal limits <=3 T; larger Z limits,
including 9 T, get no margin. Thus 3 T becomes 3.0005 T **for actual readbacks**.
Pure-Z combination plans may request 4/9 T within their configured Z ceiling.
Reduced configured limits are retained
and receive the same fixed readback margin. The exact DLL binary32 representation
of 0.0005 T is accepted at the inactive-axis boundary; its effective threshold
(0.0005000000237487257 T) is archived. No readback is rounded to zero.

Preflight, each monitored read, mixed command/actual corner, formal-window data
and hold use the same run-wide mode. Current/error/control/stability/timeout and
non-finite-value protections remain. Raw Bx/Bz, calculated norm, mode, nominal
limits, effective thresholds and per-sample readback assessment are recorded.
Nonzero inactive readbacks and nominal-limit excursions inside the accepted
margin are explicit audit flags. An inactive-axis value beyond the bound still
aborts; it does not silently switch the run to vector mode.

The fixed 0.5 mT boundary margin is the operator's software acceptance policy,
not a manufacturer-specified worst-case residual-field error. New configuration:

```toml
[magnet]
readback_tolerance_t = 0.0015
setpoint_ack_tolerance_t = 0.0001
```

Replace `field_tolerance_t` with `readback_tolerance_t`; providing both rejects.
The unified positive tolerance, capped at the approved 1.5 mT, is used for each
actual-axis target error, arrival dwell, formal sampling and hold, and the
single-axis inactive readback guard. Verified zero instead requires the **actual
vector norm** <= that same value for the full stable dwell. X=Z=1.2 mT is within
each component tolerance but its 1.70 mT norm cannot certify zero. Both raw axes
are preserved. This does not extend nominal command limits, the 0.5 mT active-axis/
vector boundary margin, or the unextended high-Z ceiling. Actual ramp samples are
not rejected merely for distance to the destination; arrival waits for convergence.

Setpoint acknowledgement is a separate component comparison, default and maximum
0.1 mT, with the existing 30 s command timeout. It confirms controller register
receipt, not physical arrival. Actual stability timeout remains independently
configured per wait. Exact float32 command bits and strict endpoint/corner/step
validation remain; register quantization never changes the requested command.
Attempts/results archive both tolerances and confirmed setpoint errors.
Legacy `field_tolerance_t` remains capped at 1 mT and retains the v2 inactive
guard. Old saved driver acknowledgement protocols remain unchanged.
Generic diagnostics without a complete magnetic plan retain strict
legacy checks. Historical records without the new version retain their archived
rules; unknown or inconsistent declarations cannot certify completion.

A high-Z-to-X plan is vector mode and cannot include a >3 T endpoint. Command
endpoints and both candidate component-write corners remain strictly validated
after float32 conversion. Reject unsafe transitions, never silently switch to
`via_zero`. Setpoint acknowledgement does not prove the other coil has ramped
down: check the latest complete actual readback and each mixed command/actual
corner using the declared readback policy before writes. Existing cleanup gates
remain; failed communication cannot certify zero. Magnet temperature/readiness,
factory charging parameters and APS100 sweep rates are unchanged. Discrete
checks do not establish a continuous physical trajectory.

Combination snapshots now declare `planned-axis-configured-v2`; historical
`universal-3T` acquisitions retain their archived envelope. Owned zero/disable
recovery for a pure-Z plan can read up to the factory 9 T Z ceiling while still
requiring X setpoint zero and actual X within the independent 0.5 mT guard.
Other magnetic modes retain the recovery vector envelope of 3.0005 T and strict
3 T setpoints. Recovery cannot resume scanning or certify zero after unknown
communication, disabled field control or out-of-envelope readbacks.

Angle is reported relative to +Z:

```text
theta_deg = atan2(Bx, Bz) * 180 / pi
```

The sign-to-physical-direction convention must be confirmed during staged hardware commissioning and stored in the station snapshot.

## Field control

The legacy attoDRY interface uses a USB virtual COM port and a vendor `attoDRYxyz64bit.dll`. The driver must:

- verify the exact DLL path and architecture;
- check every DLL return code;
- wait for device initialization with a timeout;
- read current field, setpoint, control state, and error state before any write;
- implement idempotent `ensure_field_control(enabled)` by read-then-toggle only when required;
- require an explicit field-transition policy: `direct` segments the adjacent
  vector transition, while `via_zero` requests the conservative zero detour;
  software must never insert a zero detour transparently;
- convert every command endpoint to IEEE-754 binary32 before validating it, and
  validate the endpoint plus both possible X-then-Z and Z-then-X mixed corners
  against the single-axis/dual-axis envelope above; check both candidates and
  execute only an order whose intermediate corner is safe;
- choose only a verified mixed-corner write order for each waypoint, and retain
  the complete planned and executed waypoint path rather than assuming X-first;
- record both component setpoints and readbacks, with a durable pre-command
  attempt and post-acknowledgement result for each field-control toggle, changed
  X/Z component command, and sweep-to-zero command; every component command must
  include its exact float32 bits;
- never infer zero after a failed read.

Changing both components may produce a transient path that differs from the requested direction. Constant-direction or constant-magnitude ramps therefore require coordinated intermediate vector points and readback verification; setting X and Z independently once is not sufficient to promise the path.

`isZeroingField`, vendor action/error strings, and vendor logs may be retained as
optional diagnostics only. Verified zero requires the project checks on the
confirmed zero setpoint, actual Bx/Bz, enabled control, clear error state, and the
configured dwell; a diagnostic flag or log message cannot substitute for any of
those checks.

## Temperature stability

The reference programs demonstrated useful continuous checks but did not include timeout/error handling. The project definition of stable is:

- control is enabled;
- error code is clear;
- all samples in the configured dwell window are within the setpoint tolerance;
- the maximum minus minimum readback in that window is below the configured stable range;
- the wait has not exceeded its timeout.

All tolerances and dwell periods are configuration values and are stored with the run.

## Dual SR830 wiring

`lockin_xx`:

- internal reference;
- SINE OUT drives the sample excitation path;
- measures Vxx.

`lockin_xy`:

- external reference from `lockin_xx` TTL OUT;
- measures Vxy;
- SINE OUT is physically disconnected;
- software sets its output level to the known minimum but must not treat that as an electrical disconnect.

Before acquisition, diagnostics must verify distinct VISA addresses, both IDNs, reference modes, frequency consistency, harmonic, lock state, overload state, time constant, sensitivity, and source readback.

The pre-integration laboratory procedure is
[`DUAL_SR830_DEVICE_TEST.md`](DUAL_SR830_DEVICE_TEST.md). Its setting-write path
requires explicit authorization and confirmation that `lockin_xy` SINE OUT is
physically disconnected. The ordinary diagnostic path sends queries only;
reading `LIAS?` or `ERRS?` is separately opted in because those queries consume
latched status bits.

The user-approved daily combination CLI policy (2026-09-30) treats invoking
combination_cli run as authorization for its validated selected modules, including
status consumption. No repeated RUN prompt or mandatory CLI authorization flags.
When Lock-in is selected, TOML lockin_xy.sine_output_connected=false is the
operator's wiring declaration; true rejects before connection. Backend entry
points retain explicit authorization guards. Configuration/safety hash rechecks,
preflight, nominal <=3 T targets, declared readback bounds, error handling and cleanup remain.
describe-hardware is offline; monitor reads only saved files/SQLite and never
connects instruments. Removing a redundant prompt is not a new commissioning
authorization or permission to deploy/run experiments through this development task.

## Exception handling

The agreed default for a caught exception or `Ctrl+C` is zero field. Cleanup order is electrical outputs first, then magnet zero request, then final state logging and disconnect.

APS100 hardware power-fail, quench, and shutdown-input behavior remains an independent protection layer. It does not guarantee that a Python crash returns the magnet to zero.

## Gate SMUs

The Three-SMU hardware contract defines up to three Keithley 2400 semantic roles and gives
`smu_bias`, `gate_top`, and `gate_bottom` independent `max_abs_voltage_v` and
`max_abs_current_a` boundaries. Every requested source value is checked before a
write, and every actual voltage/current readback is checked against both limits.

The scan-plan `role` is the only enable state. A `fixed` or `sweep` role requires
its complete same-name hardware table. An `off` role may omit that table and is
not parsed, opened, queried, written, cleaned up, or recorded. Its physical
source/output state is therefore unknown, never inferred to be zero or off. The
three hardware roles use one flat table shape; the Three-SMU VISA timeout is a
fixed 5000 ms and is not an operator-configurable safety value.

An off run-plan role is normalized without parsing the values of recognized
scan-only fields (`fixed`, `points`, `ranges`, `start`, `stop`, `step`, or
`bidirectional`). This permits temporarily disabling a role without editing its
saved vector. Misspelled/unknown field names are still rejected, and changing
the role back to `fixed` or `sweep` restores the complete strict validation.
An active sweep accepts exactly one of explicit `points` or ordered `ranges`.
Range expansion is purely offline and feeds the same pre-write target-limit
validation as explicit points; legacy active top-level `start/stop/step` is rejected.

Keithley compliance remains an instrument protection setting, but it is not a
second user-entered boundary. Voltage-source roles derive current compliance from
`max_abs_current_a`; current-source roles derive voltage compliance from
`max_abs_voltage_v`. The adapter queries compliance and source/measurement ranges
after configuration and fails closed if the compliance readback exceeds the
approved absolute limit. Source and measurement autorange are required for
formal acquisition because the current schema has no fixed-range fields.

The user-approved 2400 initialization exception (2026-09-30) permits a temporary
minimum sense range only after confirmed source zero/output OFF, to satisfy the
manual's nominal-range 0.1% compliance floor. AUTO and compliance are queried
again before output enable. Only a first +822-only compliance-setting rejection
permits one audited range-preparation/retry with fresh zero/OFF confirmation.
Other/repeated errors and communication failures fail closed. This is neither a
raised protection limit nor an acquisition retry. Initial/configuration error
queues and partial command/readback audits are retained separately from cleanup.

Compliance programming uses a round-trip-safe scientific SCPI literal through
the existing QCoDeS transport, for both current and voltage protection. The same
literal is retained in configuration_audit. QCoDeS compliance setters using
{:f} round 100 nA to 0.000000; this is a confirmed software cause of +822 in the
reported run, distinct from range compatibility. Actual compliance readback and
all zero/OFF, AUTO and error-queue guards still apply; the fix never raises the
requested maximum. A real output-OFF acceptance check is still pending.

The Three-SMU path intentionally has no software ramp, source min/max, readback
tolerance, separate leakage threshold, or per-device settle time. A formal point
uses one direct target write per active role, the shared `delay_s`, then records
the actual readback. Cleanup directly requests zero, waits `delay_s`, records the
readback, and disables output. A communication failure never proves 0 V or
output-off; the last confirmed state is retained and the instrument must be
checked manually.

The Three-SMU `run` terminal panel and live Notebook may display a formal sample
only after the session has recorded it and placed it in an in-process memory FIFO.
Those presentation consumers must not issue a second hardware read, write, or
status-queue query. If a formal sample is unsafe, it is published before the
session raises its fail-closed error so its existing readback/status evidence can
be displayed and retained without changing cleanup order.

The legacy model-independent simulation gate controller retains its own ramp and
leakage test fixtures; those fields are not part of the real Three-SMU daily
hardware TOML.

Signed resistance is `Vxx_X / I_rms`. The software never infers `I_rms` from the
SR830 amplitude unless the operator explicitly supplies the complete excitation
path resistance, including series components and termination/loading effects.

## Vendor files

Whether the vendor DLL binary is tracked is a repository packaging decision, not
a hardware-safety or commissioning restriction. Runtime-specific DLL paths still
belong in `config/hardware.local.toml`; local paths must not be copied into tracked
configuration or raw public exports.
