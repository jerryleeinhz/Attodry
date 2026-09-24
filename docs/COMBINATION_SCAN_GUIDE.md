# Four-module combination scans — offline-tested hardware coordinator

Status: 2026-09-23. The real-driver coordinator now includes temperature, magnetic,
SMU and fixed-frequency Lock-in excitation. It is **not jointly hardware
commissioned**. All work in this checkpoint uses fake DLL/VISA, never instruments.

Final verification: **638/638 guarded tests** locally (117.888 s) and on LK_setup
lyr Python 3.12.13 (145.473 s), zero errors/failures/skips. Includes all 64 selected
module permutations with fake devices. Exact source hashes and isolated target
test logs are in PROJECT_HANDOFF's current stage; this is not real commissioning.

## 真实驱动组合入口（目前仅离线验收）

使用 `combination_hardware.py`、`cryostat_points.py`、`lockin_points.py`
和 Three-SMU 单点接口。四模块支持任意非空子集及排列（共 64 种），
`order` 从外层到内层；单点轴表示固定条件，省略的模块不做设置。
每个叶节点重新读取所有 active SMU，按各角色配置保存真实 h1/h2/h3 锁相读数。
不调用完整独立扫描来拼接；连接和配置只做一次，最终集中 cleanup。

仍不支持：Lock-in frequency 扫描、软件脉冲、实机 resume。
这些请求在连接前拒绝。固定频率下若所选谐波越过 102 kHz，也在连接前拒绝，
不静默减少谐波。此前四模块模拟器及专用温度×激励命令仍保留。

在 ignored `hardware.local.toml` 中增加：

```toml
[combination_scan]
backend = "hardware"
order = ["temperature", "smu", "magnetic", "lockin"]  # 外 -> 内；按需求删减/排列
samples_per_condition = 1
repeats = 1
run_name = "four_module_combination"
note = "填写本次接线与样品说明"
```

扫描点、source mode、各 SMU 的 V/I 限值、delay 仍来自 `three_smu_run` 和
对应硬件表；Lock-in 沿用 excitation 网格、角色谐波、量程/Reserve、时间常数、
电阻与器件 AC 安全限值。两套独立命令的 `samples_per_point` 不再叠加，
由 `samples_per_condition` 决定每叶完整采样次数。一份采样包含所选谐波，
各设备/谐波是顺序读取，不能称为同时采样。

温度点来自 `[temperature_scan]`；没有该表时用 `temperature_run.target_k`
作为一个固定点。磁场来自 `[magnetic_field_run]` 的 ordered points 或 segments，
保留重复端点、反向段、段号和条件索引；`direct`/`via_zero` 策略不自动替换。
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

可立即执行的**纯离线预览**：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python -m attodry_control.combination_cli describe-hardware --config config/hardware.local.toml
```

下面是未来**专门授权联合实机验收后**的写命令，不是本次已运行的命令：

```powershell
New-Item -ItemType Directory -Force run_data/combination
python -m attodry_control.combination_cli run --config config/hardware.local.toml --database run_data/combination/scan.sqlite --run-id UNIQUE_RUN_ID --authorize-combination --authorize-cryostat --confirm-xy-sine-disconnected
python -m attodry_control.combination_cli monitor --database run_data/combination/scan.sqlite --run-id UNIQUE_RUN_ID
```

`--authorize-combination` 授权所选组合；旧 `--authorize-electrical-combination`
是兼容别名。只要包含温度或磁场，还必须额外给 `--authorize-cryostat`，
否则在连接任何设备/创建数据库之前拒绝；纯电学扫描不需要该额外开关。
包含 Lock-in 时必须确认 XY SINE OUT 物理断开。XX 内参考/XY TTL、
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

SQLite 仍为 combination-v1，硬件记录明确 `simulated=false`；配置快照保存解析值、
实际 role/resource、源码哈希，preflight 保存 IDN，原始角色读数先于完整样本保存。
默认分析只接收 completed + accepted + clean + verified cleanup。
`notebooks/combination_analysis.ipynb` 无需按照扫描顺序重写：
例如 X=`measured.smu_bias_voltage_v`，Y=`measured.lockin_xy_h2_amplitude_v`，
group=`requested.lockin_excitation_v_rms`；不会把 h1 当作 h2。

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

`notebooks/combination_analysis.ipynb` is the new common read-only entry:
set DATA_DIRECTORY once, refresh/select/load sources, inspect available columns,
optionally exclude exact sample IDs, choose X/Y/group/filter, then plot/export.
Old Notebooks remain for their specialized phase, fit, map and commissioning
views. Their science models were not replaced by a generic fitter.

Export creates a new directory, selected-sample CSV and selection manifest with
IDs, filters, exclusions, protected group keys, selection hash and analysis-source
hash; optional PNG/PDF accompany it. No raw file is overwritten. Figure integrity
follows the scientific-visualization skill's preservation/missing-data/redundant-
encoding guidance and reuses the project's scoped plotting style. This is not a
journal-specific figure or compliance claim.

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

1. Joint hardware commissioning requires newly scoped connection/status/write
   authorization, current wiring and DC+AC total sample-limit review. Start with
   one fixed point, then tiny two-module grids before a four-module grid; test
   interruption and verify physical cleanup. Offline results are not acceptance.
2. If needed, add Lock-in frequency/frequency-excitation axes separately, retaining
   harmonic-range and transition policies. Current Lock-in axis is excitation only.

The code implements the shared four-module path, but its real joint operation,
thermal behavior, magnetic history and physical cleanup remain uncommissioned.
