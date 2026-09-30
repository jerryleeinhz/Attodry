# Four-module combination scans — bounded hardware validation complete

2026-09-30 更新：短命令启动和 Lock-in frequency / frequency-excitation 模式已加入，
完成离线假仪器验证；新增扫频路径尚未实机验收。下面的 2026-09-24 实机范围只涵盖固定频率。

Status: 2026-09-24. The real-driver coordinator includes temperature, magnetic,
SMU and fixed-frequency Lock-in excitation. Bounded joint hardware validation
passed: two four-point electrical nesting orders and one 24-point grid with all
four axes varying, clean verified cleanup, file-only monitoring and analysis.
See [the acceptance record](MULTI_AXIS_ACCEPTANCE_20260924.md) for exact scope,
actual temperature ranges, data hashes and unresolved historical faults.

Historical 2026-09-24 verification: **644/644 guarded tests**, local rerun 126.899 s;
unchanged runtime previously passed target lyr 644/644 in 143.182 s. No failures,
errors or skips. All 64 module subsets/orders are fake-tested; this does not mean
all 64 physical orders, fault cases or output ranges have been commissioned.

## 真实驱动组合入口（已完成限定范围实机验收）

使用 `combination_hardware.py`、`cryostat_points.py`、`lockin_points.py`
和 Three-SMU 单点接口。四模块支持任意非空子集及排列（共 64 种），
`order` 从外层到内层；单点轴表示固定条件，省略的模块不做设置。
每个叶节点重新读取所有 active SMU，按各角色配置保存真实 h1/h2/h3 锁相读数。
不调用完整独立扫描来拼接；连接和配置只做一次，最终集中 cleanup。

仍不支持：软件脉冲、实机 resume。
这些请求在连接前拒绝。固定频率下若所选谐波越过 102 kHz，也在连接前拒绝，
不静默减少谐波。此前四模块模拟器及专用温度×激励命令仍保留。

在 ignored `hardware.local.toml` 中增加：

```toml
[combination_scan]
backend = "hardware"
order = ["temperature", "smu", "magnetic", "lockin"]  # 外 -> 内；按需求删减/排列
lockin_mode = "excitation"  # 或 "frequency" / "frequency_excitation"
samples_per_condition = 1
repeats = 1
run_name = "four_module_combination"
run_id = "auto"
note = "填写本次接线与样品说明"
```

扫描点、source mode、各 SMU 的 V/I 限值、delay 仍来自 `three_smu_run` 和
对应硬件表；Lock-in 沿用所选模式的网格、角色谐波、量程/Reserve、时间常数、
电阻与器件 AC 安全限值。两套独立命令的 `samples_per_point` 不再叠加，
由 `samples_per_condition` 决定每叶完整采样次数。一份采样包含所选谐波，
各设备/谐波是顺序读取，不能称为同时采样。

温度点来自 `[temperature_scan]`；没有该表时用 `temperature_run.target_k`
作为一个固定点。磁场来自 `[magnetic_field_run]` 的 ordered points、单轴
segments 或固定模长 angle_segments，
保留重复端点、反向段、段号和条件索引；`direct`/`via_zero` 策略不自动替换。
角度段按 `Bx=B sin θ, Bz=B cos θ` 展开正式目标，但转场路径不保证恒模长。
内层磁场每次从上轮终点返回首点也按配置的路径执行，可能改变磁场历史，
请在离线预览中确认这种嵌套是否符合实验目的。

温度与磁场只创建一个 attoDRY 连接；所有 active 模块 preflight 通过后才开始设置。
每个组合条件在全部轴设置完成后重新确认磁场和温度稳定，因此扫场后的温漂不会
被前一次的温度稳定标记掩盖。温度轴的降温返回也检查最大位移：冷却途中上限为
`max(起始实测温度, 新目标) + max_overshoot_k`，不允许借此接受仍然过热的平台；
正式采样恢复新目标的严格 overshoot 上限。无法冷却/稳定会超时并拒绝数据。
稳定判据沿用 `[temperature_stability]`（含 stable-readback 最小响应）；同一温度
重复点重新等待 dwell，不要求对同一目标再次产生温度响应。
和现有 temperature scan 一样，不叠加单点 `pre_measure_wait_s` 固定等待；
组合中断统一 abort/cleanup，不采用独立温控的交互 resume。

正式采样整体前后、每个 XX/XY 谐波读数对前后顺序读取环境状态，保存在
`environment_sample`/`environment_formal_window` 和样本 status 内。
`actual.temperature_k` 使用这些实测点的带时间权重梯形均值，不用目标值或稳定窗口均值；
`actual.field_x_t/field_z_t` 为正式窗口读回的算术均值，并保存各原始点及时间戳。
正式窗口同时检查温漂、磁场误差和范围。它是**同步前后夹取，不是连续监控**，
无法证明阻塞 VISA 调用期间没有短暂异常；不另开 DLL/VISA 后台读取线程。

集成路径把有效 X/Z 硬件上限都收紧到 <=3 T，合场始终 <=3 T；目标、float32 命令、
中间分量拐角和所有完整读回使用同一有效边界。即使 standalone 允许纯 Z 9 T，
这里也不允许；只选温度时遇到超出集成边界的初始磁场也会拒绝接管。

Lock-in 模式与参数来源：

| lockin_mode | 频率 | 激励 | 正式谐波选择 |
|---|---|---|---|
| `excitation`（缺省） | `lockin_xx.frequency_hz` | excitation 网格 | excitation_xx/xy_harmonics |
| `frequency` | frequency 网格 | frequency_source_voltage_v_rms | frequency_xx/xy_harmonics |
| `frequency_excitation` | frequency 网格 | excitation 网格 | combined_xx/xy_harmonics |

网格仍在 `[lockin_sweep]`，frequency_points_hz / frequency_ranges 二选一，
excitation_points_v_rms / excitation_ranges 二选一，不在 combination 表重复填写。
f×e 内部固定为频率外层、激励内层。例如 order=[magnetic,smu,lockin]，
实际为磁场 → SMU/gate 条件 → 频率 → 激励。总条件数为各模块点数相乘再乘 repeats；
lockin 的点数在 f×e 模式为 Nf×Nu。samples_per_condition 是每条件的完整采样组数。

每个频率检查 h×f<=102000 Hz：h2 基频最高51 kHz，h3最高34 kHz。
扫频模式沿用 skip_unsupported_harmonics：true 时仅跳过部分超限阶，摘要、原始记录
和数据缺口明确保留；某频率所有所选阶均不可测时在打开设备前拒绝。
false 时任何超限阶都拒绝。原 excitation 模式仍严格拒绝不支持的所选阶。
频率段、激励段都设置同角色量程时沿用独立 f×e 的激励段优先规则；
省略的激励段量程回退到频率段。与 harmonic_settings 冲突的配置仍拒绝。

频率改变，包括外层循环末频率返回首频率时，先请求并确认4 mV，再按现有
谐波安全接口停靠超限检测阶，设置XX频率、等待并核对XX/XY读回，最后恢复目标激励。
同一频率仅换激励不重复写FREQ。结束使用既有清理：4 mV → h1 → TOML基准频率、
基准SENS/Reserve，并保存最后读回。过载继续策略不取消失锁、通信、频率或环境边界。

可立即执行的**纯离线预览**：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python -m attodry_control.combination_cli describe-hardware
```

日常交互式启动从项目目录执行：

```powershell
python -m attodry_control.combination_cli run
```

默认配置为当前工作目录下的 config/hardware.local.toml；找不到即报错，不搜索其他实验配置。
数据库从 `[project].database_path` 读取，相对路径相对该TOML所在目录；例如
`../run_data/combination/scan.sqlite`。命令行 `--database` 覆盖它，保持原先相对工作目录的语义。
父目录在确认后自动创建。一份SQLite可以容纳多次不同run ID的扫描。
`run_name`是可重复的实验名称，`run_id`标识一次运行，在同一数据库内不可重复。
省略run_id或填写auto会生成UTC微秒时间戳+净化的run_name；也可在TOML写固定ID，
或用 `--run-id` 临时覆盖。重复ID拒绝，不能覆盖记录或重放磁场历史。

启动前离线展示配置与数据库绝对路径、run name/ID、外到内循环、点数/首末目标/段方向、
总条件和采样数、正式谐波、量程/Reserve（包括谐波覆盖）、过载策略及cleanup。
输入RUN才连接仪器，RUN同时确认本次写入/状态消费和XY SINE OUT物理断开（启用lockin时）。
空输入、其他文字、EOF均取消；Ctrl+C按CLI中断退出。取消不创建数据库或连接设备。
确认期间TOML或lockin_safety变化会拒绝，需重新启动审阅。

SSH后台/定时启动保留显式授权命令，不等待终端输入：

```powershell
python -m attodry_control.combination_cli run --config config/hardware.local.toml --database run_data/combination/scan.sqlite --run-id UNIQUE_RUN_ID --authorize-combination --authorize-cryostat --confirm-xy-sine-disconnected
python -m attodry_control.combination_cli monitor --database run_data/combination/scan.sqlite --run-id UNIQUE_RUN_ID
```

提供授权开关时必须完整：`--authorize-combination`（旧 electrical-combination别名保留），
启用温度/磁场时加 `--authorize-cryostat`，启用Lock-in时加 `--confirm-xy-sine-disconnected`。
部分开关或非交互环境缺少授权时在连接/创建数据库前拒绝，避免后台等待输入。
完整旧命令也先打印相同摘要；SQLite配置快照保存摘要与确认方式。
授权不是操作系统提权，也不能代替接线的物理核验。XX 内参考/XY TTL、
初始 4 mV/h1、输入与滤波读回必须符合配置；需要改面板固定设置时，先单独
`apply-toml`。off SMU 不访问。未选温度/磁场轴不写入，但共享 DLL 的 full-state
读回会包含两者，不推断未选模块为 off/zero。不要并行运行
`monitor-live`、vendor GUI 或另一个控制进程。

联合接线必须另行确认：分别通过 DC SMU 限值和 AC 激励限值，不等于证明两种源
叠加后的样品总电压/电流安全。不能把旧空载测试或历史 AC 参数当作当前样品授权。

正常、异常和 Ctrl+C 都执行：XX 4 mV、双 SR830 h1/原量程与 Reserve 恢复，
再 active SMU 归零并 OFF；不沿用独立 SMU 的 hold 选项。
然后磁场正常结束按 `[cleanup].normal_end_field_policy = "hold"/"zero"` 执行，
异常尝试受监控回零；温度正常 hold，异常关闭温控，最后关闭共享连接一次。
任何前序 cleanup 失败都会让后续模块采用异常策略。未尝试设置的环境轴不额外写入。
hold 是保留控制器当前目标/控制状态，不是 persistent-mode 命令，也不是长期励磁安全认证；
不会自动回 base temperature。通信失败保留 `last_confirmed_state_not_current`，
绝不能把它当作当前状态或把失败当作已经零场。
审计写盘失败也不能跳过剩余 cleanup；失败/中断的真实运行一律保留读回证据并要求
人工核验，不能因后续清理读回正常而抹去通信不确定性。
硬退出/断电不能由 Python 保证安全；不允许在旧 run 上自动重开硬件。

联合清理的越界恢复（2026-09-24）：正常扫描、目标写入和 hold 仍严格使用本次 TOML
限值。只有已接管轴的 monitored-zero / failure-disable 动作，才允许读取超出较小
实验限值但仍在通用合场 <=3 T 内的状态；不会把扫描限值改为 3 T，也不会继续扫描。
非有限/不完整读回、设备 error 和未知控制状态仍拒绝；回零要求磁场控制已开启，
不因 cleanup 自动开启它。回零超时后仍独立尝试关闭已设置的温控。
新增 recovery-action、readback envelope 和首次 scan-limit violation 审计。
即使最终回零验证成功，清理期间越界也使整次运行失败、默认分析排除并要求人工核验。
普通 standalone 磁场驱动保持原来的实验限值检查，不隐式采用此联合恢复策略。

`[magnetic_field_run].max_step_t` 是内部请求点间距，不是 T/min。
`[magnet].wait_timeout_s` 是等待稳定的最长时间，不会延后越界保护；
`stable_dwell_s` / `poll_interval_s` 分别控制稳定确认窗口与查询间隔。
当前 legacy DLL 公开头文件没有 ramp-rate setter。历史小场验收使用 7200 s 超时，
不代表任何点必须等待两小时，也不能保证物理 ramp 速度或忽略实际场越界。

SQLite 仍为 combination-v1，硬件记录明确 `simulated=false`；配置快照保存解析值、
实际 role/resource、源码哈希，preflight 保存 IDN，原始角色读数先于完整样本保存。
默认分析只接收 completed + accepted + clean + verified cleanup。
`notebooks/combination_analysis.ipynb` 无需按照扫描顺序重写：
例如 X=`measured.smu_bias_voltage_v`，Y=`measured.lockin_xy_h2_amplitude_v`，
group=`requested.lockin_excitation_v_rms`；不会把 h1 当作 h2。

新增频率坐标已经进入同一SQLite表和两份Notebook共用的只读绘图界面。
画磁场/gate曲线时固定或分组frequency；画频率曲线时固定磁场/gate并按激励分组。
f×U图用 actual.lockin_frequency_hz、actual.lockin_excitation_v_rms 作XY，所选谐波作颜色。
未固定/分组的变化条件会明确报错，不将不同频率混成一条曲线。跳过的谐波缺失，不能补零。
条件grid索引、两个段索引和双机实际频率存入审计；旧SQLite保持只读兼容，无迁移要求。

## What is available

The isolated branch `codex/integration-four-module-scan` includes temperature /
Lock-in integration be6071f, Three-SMU direct points 02f04ec, and magnetic 7d4417f.
Their existing commands, audit formats and analysis Notebooks remain available.
The original dirty main and integration worktrees have not been overwritten.

The original simulator provides this **four-module simulation** contract:

- any nonempty subset of temperature / magnetic / SMU / Lock-in;
- array order defines literal outer-to-inner loops; a one-point axis is fixed,
  and an omitted module is inactive (not assumed physically off);
- magnetic X/Z is one atomic vector point list; duplicates, segment, direction,
  repeat and point indices are preserved, never sorted or deduplicated;
- SMU point values can contain one or more semantic roles, with voltage or
  current source units explicit. No fictitious zero channels for absent roles;
- set all changed axes, qualify the whole condition, then take new measurements
  at **every leaf and every sample**, including all active SMUs after an excitation
  change. Qualification is repeated after field changes;
- one station owner, deterministic cleanup order: Lock-in, all active SMUs,
  magnetic, temperature, close; every cleanup is attempted even after another
  cleanup/audit failure. This is simulated behavior, not a verified device state.

The simulator only models h1 XX/XY and a deliberately excitation-dependent SMU
response, `I = V / [1000 * (1 + excitation)]`. This is a regression fixture, **not**
a transport model, calibration, temperature-stability test or physical trajectory.
Real modules keep their existing harmonic/range/stability/safety implementations.

## Offline commands

Run from this branch's worktree, with its `src` first on the import path:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
python -m attodry_control.combination_cli describe --config config/combination.simulation.toml
New-Item -ItemType Directory -Force run_data/combination_demo
python -m attodry_control.combination_cli simulate --config config/combination.simulation.toml --database run_data/combination_demo/scan.sqlite --run-id demo
python -m attodry_control.combination_cli monitor --database run_data/combination_demo/scan.sqlite --run-id demo --once
```

Remove `--once` to refresh while the archived run state is active. The monitor
never reads VISA/COM/DLL or consumes a status latch. It reports run status,
accepted/total conditions, the current attempt, last recorded instrument reads,
cleanup, primary error and last-event timestamp. `process_liveness="unknown"` is
intentional: an active DB record does not prove the process still lives.
Last-recorded readings are historical evidence, not guaranteed present states.

The example contains three SMU voltages × two excitation values. It is NOT a
second daily hardware configuration file. The electrical real runner obtains
device configuration and existing expanded grids from the single ignored
`hardware.local.toml`; the offline file cannot authorize or configure instruments.
The separate electrical hardware entry above uses the daily station TOML, never
this simulation-only file.

## Data contract

Canonical integrated storage remains SQLite WAL, with FULL synchronous writes.
New version-1 tables are prefixed `combination_`; existing RunStore tables and
standalone JSON/JSONL/CSV are unchanged. Raw read events are committed before
formal sample promotion. At most one accepted attempt exists per condition.
The run's terminal status and its terminal event are committed together.

Each formal sample exports a wide/long-form observation row (one complete sample
per row, not a multidimensional array):

| Field family | Meaning |
| --- | --- |
| run_id / condition_id / attempt_index / sample_index | Unique sample identity; no coordinate deduplication |
| sequence_index / repeat_index / loop_order | Actual acquisition order |
| axes.<module>.index / segment / direction | Literal path position and branch |
| requested.* | Complete combination setpoints, including fixed axes |
| actual.* | Readbacks in that sample window, distinct from requests |
| measured.* | Available SMU V/I and semantic Lock-in role/harmonic measurements |
| timestamps.* / started_at_utc / finished_at_utc | Sequential read timing, not a simultaneous acquisition claim |
| status.* / clean / accepted / run_status | Quality and acceptance; raw partial events remain separately auditable |
| simulated / schema_version / implementation_sha256 | Synthetic-data flag, schema and archived implementation fingerprint |

Implementation fingerprint is in the archived plan. An axis's fixed value is
repeated in each full condition; inactive axes remain absent. Failed partial
reads stay in events; they do not become fabricated complete formal rows.
Non-finite numbers are recorded as explicit `invalid_numeric` evidence, rejected
for formal acceptance, never coerced to zero.

Default analysis requires accepted attempt + clean sample + completed run +
verified cleanup. Audit opt-in exposes formal samples of interrupted/rejected/
active runs without promoting them. Partial events remain available through SQL.

## Your SMU-outer / Lock-in-inner example

```python
from attodry_control.combination_analysis import load_combination_rows, select_series, plot_series

rows = load_combination_rows("run_data/combination_demo/scan.sqlite")
series = select_series(
    rows,
    x="measured.smu_bias_voltage_v",
    y="measured.smu_bias_current_a",
    group_by=("requested.lockin_excitation_v_rms",),
)
figure, axis = plot_series(
    series, x="measured.smu_bias_voltage_v", y="measured.smu_bias_current_a"
)
```

The result is two excitation-indexed SMU I–V groups, three points each.
Regrouping does not undo thermal drift, hysteresis or the fact that different
excitation curves were sampled interleaved. Per-instrument timestamps and the
original sequence remain available. Changing only the plotting order is not
equivalent to repeating the measurement with reversed physical loops.

Group by requested conditions for stable grouping; use actual values for plotted
coordinates. Explicit actual-value grouping is allowed but has no implicit
tolerance/binning. Other varying requested coordinates and run/repeat/sample/
segment/direction remain automatic group keys. Revisited non-X axis indices are
kept separate. Plotting does not average duplicates or fill missing values.
Scatter markers avoid implying measurements through missing observations.

## Legacy analysis and Notebook

`load_legacy_three_smu(path, audit=False)` adapts existing metadata + CSV through
its established loader. Source-mode units come only from the archived hardware
snapshot. Missing old mode/attempt fields stay unknown, not inferred from today's
TOML or fabricated as voltage mode.

`load_legacy_temperature_lockin(path)` adapts completed/clean summary JSON or
formal CSV through the existing temperature loader. It preserves actual formal-
window temperature and recorded current. Per-role/harmonic rows stay distinct;
missing original timestamps/sequence/attempt IDs stay absent. This adapter does
not add rejected-condition loading beyond the old loader's contract; inspect
the original JSONL for that audit. It does not invent SMU or magnetic data.

`notebooks/combination_analysis.ipynb` now uses the shared unified plotting dashboard,
opening a four-channel Vxx/Vxy h1/h2 card with mean ± SD by default. Curve and
XY–Z cards, source selection, exact sample exclusions, per-channel overload filtering,
statistics, setup save/restore and exports are available from widgets.
See [DATA_ANALYSIS.md](DATA_ANALYSIS.md#unified-and-combination-plotting) for the
workflow and statistical/quality contract. `unified_plotting.ipynb` uses the same
implementation and opens a general curve card. The Python raw-row example above
retains its original no-aggregation behavior. Specialized calibration notebooks
and acquisition models are separate from this plotting change.

## Resume and safety boundaries

- Resume only explicitly terminal failed/interrupted **non-magnetic** simulations
  with verified cleanup and identical archived plan/schema/implementation.
- Accepted conditions can be skipped; failed conditions get a new attempt index.
  Never overwrite or promote a former rejection.
- A killed process left active is not automatically taken over. No lease timeout
  is interpreted as proof that hardware is safe.
- Magnetic resume is deliberately rejected even if cleanup succeeded. Recovery
  needs a separately approved path/history policy; never automatically jump over
  ordered field points or insert a via-zero route.
- The new integrated contract retains universal resultant <=3 T, including pure
  Z, per this task. Standalone magnetic code retains its separately documented
  axis-dependent envelope; merging does not silently raise integrated limits.

## Remaining acceptance work

1. The bounded normal four-axis path and two electrical orders have passed real
   validation. Other physical orders, sample/wiring/output envelopes and deliberate
   interruption/fault cases need scoped authorization and their own evidence.
   Historical XY overload and 5-mT-command/readback discrepancy remain unresolved.
   For strict temperature-target equilibrium, choose appropriate target-mode
   tolerance/dwell rather than treating stable-readback acceptance as equilibration.
2. The new frequency and frequency-excitation modes have passed offline fault,
   ordering, cleanup and analysis tests. Real acceptance of frequency transitions
   in a combination scan requires a separately authorized bounded experiment.

The shared real path is validated within the recorded small-range experiment;
this is not certification of all physical trajectories, faults or precision.
