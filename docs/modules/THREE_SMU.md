# Three-SMU / dual-gate 模块

## 当前阶段

状态：`S0 offline complete`（2026-08-26）。本分支实现了三台 Keithley 2400 的独立
QCoDeS 扫描模块、无 GUI CLI、调用同一 generator 的实时 Notebook、accepted-only 分析
Notebook，以及独立的 query-only 终端实时监控。所有验证均为离线 fake-instrument 测试；
真实 SMU 连接、查询和写命令数均为 **0**。

本包落实 `PROJECT_MODULE_DEVELOPMENT_GUIDE.md` 的单配置、fail-closed、审计和 SSH 分析
要求，并保留已建立的三角色范围：

- `smu_bias`：source-drain bias；可选 voltage 或 current source；
- `gate_top`：top gate；独立选择 voltage/current source；voltage 模式检查 leakage；
- `gate_bottom`：bottom gate；独立选择 voltage/current source；voltage 模式检查 leakage。

通用 `docs/modules/SMU.md` 中的两-gate 规划不能删除或替换这个 `smu_bias` 角色。本模块
不接入 Lock-in、冷台、磁场或主 acquisition。

日常使用与全部参数见 [`../THREE_SMU_DAILY_OPERATION.md`](../THREE_SMU_DAILY_OPERATION.md)。

## 架构与配置

```text
hardware.local.toml
  ├─ [smu_bias]                 bias 独立 V/I 安全边界与 2400 参数
  ├─ [gate_top]/[gate_bottom]   各自独立的 source/V/I/leakage 安全限值
  │    └─ [.smu]                仅 timeout/NPLC/range/four-wire 等 2400 参数
  └─ [three_smu_run]            run name/note/输出目录/扫描计划
       └─ [smu_bias|gate_top|gate_bottom]  off/fixed/sweep
             ↓
three_smu_config → ThreeSmuSession/generator → CLI 或 live Notebook
             ↓                                      ↓
metadata.json + raw.jsonl + data.csv       accepted-only analysis Notebook
```

日常只复制 `config/hardware.example.toml` 为 ignored 的 `config/hardware.local.toml`。
模块 loader 只解析本模块相关表，因此别的未完成模块表不会阻塞 Three-SMU 的独立 `describe`。
现有 `config.py` 也允许这些表共存，但不重复解释 SMU 细节。

Legacy 的 `three_smu_hardware.local.toml` / `three_smu_scan.local.toml` 双文件入口仍保留；新的
日常 CLI 和 Notebook 一律使用单一 `hardware.local.toml`。安全 schema 不兼容旧的无单位
`source_min/source_max/ramp_step/readback_tolerance`：操作者必须迁移为明确 `_v`/`_a` 字段并
补齐独立 V/I 边界，loader 不会从旧范围猜测安全限值。既有 run 数据保持只读，不需迁移。

## 已实现的安全行为

1. 静态 TOML、地址唯一性、角色/扫描形状和全点范围验证在 driver import 或连接前完成。
2. `run` 默认读取 `config/hardware.local.toml`，但在 QCoDeS/VISA import 或连接前显示完整
   扫描摘要，并要求每次终端精确输入 `RUN THREE SMU`。拒绝/EOF 均不会打开资源；该确认明确涵盖
   设置写入与 run 所需的 error-queue 消耗。
3. `finish_action = "hold"` 还要求第二个 `HOLD OUTPUTS` 确认。它不会成为日常默认，也不会因
   配置文件中已有 hold 而自动保留输出。
4. 授权连接后的 query-only preflight 记录 identity、source mode/setpoint、output、V/I、
   compliance/range、remote-sense 和状态。身份重复、output 已开、mode 不符、当前 source
   变量非零、任一 V/I 绝对边界越界或脏状态都会在设置写入前拒绝；voltage-source
   gate 另执行通用 gate 的零电压/leakage 预检。
5. `GateBackend`/`SafeGateController` 现在也要求以真实 `GatePreflightState` 确认安全状态，
   不能把内存中的初始“零/off”当成仪器读回。Three-SMU 的 voltage-source gate 复用该预检。
6. 每个角色都有独立的 `max_abs_voltage_v` 与 `max_abs_current_a` 软件硬边界，且两者始终
   检查。voltage source 使用 `_v` source/ramp/tolerance 与 current compliance；current
   source 使用 `_a` 字段与 voltage compliance。compliance 不得高于对应绝对边界，source
   范围也必须位于边界内；voltage-source gate 的 leakage 阈值须低于 current compliance。
7. 配置与每个 setpoint 从零开始；输出只在零 setpoint 和读回确认后开启。所有 target 以
   当前 source 单位的明确最大步长 ramp，每步检查 source/output/V/I、trip、near-compliance，
   并仅对 voltage-source gate 检查 leakage。
8. 正常、异常、`Ctrl+C` 和 generator 提前关闭共享 cleanup：`smu_bias`、`gate_top`、
   `gate_bottom` 依次回零/关 output。无法确认时 run 被拒绝，保留最后确认状态和 cleanup 错误，
   要求人工检查；通信失败从不被记成零或 output-off。
9. `monitor-live` 使用没有 configure/set-source/set-output/cleanup 方法的独立 raw-VISA
   query adapter。它显示三台的 V/I/R、source/output、compliance/trip、range/sense、identity
   和安全 warning；只关闭本地 handle，绝不改变 SMU 状态。默认不读 `:SYST:ERR?`，只有显式
   `--consume-status-queue` 才会消费 error queue。它不得与 scan 并发，也不替代 preflight。

未完成的硬件信息（型号/固件差异、实际地址、器件限值与接线）不能由代码猜测。任何实机
步骤均需要该次计划的明确用户授权。

## 数据与分析

每次 run 的独立目录包含：

- `metadata.json`（schema v3）：请求的单位明确 V/I 硬件配置/计划、实际 preflight、run name/note、配置路径、
  import 路径、Git commit/dirty、主错误、cleanup 结果和清理错误；
- `raw.jsonl`：保留 start/preflight/configure/ramp/sample/error/cleanup 原始事件；
- `data.csv`：requested coordinate/source 与实际 readback V/I/R、output、trip/status 并列。

所有 rejected、partial、interrupted 记录保留。`load_three_smu_rows()` 和分析 Notebook 默认
只返回 `completed + accepted + clean` 正式样本，需显式 opt-in 才会审计 rejected/problem。

分析 notebook 通过 `DATA_DIRECTORY` 枚举本地、SSH 挂载或网络目录；不使用 Tk 文件选择器，
跳过不完整 run。`multi_smu_map` 支持 bias/top/bottom 任意 1–3 个扫描轴。绘制 top-vs-bottom
map 时，若 bias 也在扫描，`build_map(..., fixed_coordinates={'smu_bias': value})` 必须选择一个
bias slice；固定 bias 的常见双 gate map 不需额外选择。

## 接口

离线配置检查：

```powershell
python -m attodry_control.three_smu_cli describe
```

将来（当前未授权）的 query-only 状态接口：

```powershell
python -m attodry_control.three_smu_cli monitor-live
python -m attodry_control.three_smu_cli monitor-live --consume-status-queue
```

将来（当前未授权）的真实扫描接口：

```powershell
python -m attodry_control.three_smu_cli run
```

`run` 会显示计划并要求精确的 `RUN THREE SMU`；`finish_action = "hold"` 再要求
`HOLD OUTPUTS`。Notebook 的两个授权 flag 默认均为 `False`，且调用同一 `ThreeSmuSession`，
不允许直接建立 QCoDeS driver 或使用另一套 ramp。实时监控的完整操作边界见
[`../THREE_SMU_LIVE_MONITOR.md`](../THREE_SMU_LIVE_MONITOR.md)。

## 代码与测试所有权

```text
config/hardware.example.toml               单一日常本地模板
src/attodry_control/three_smu_config.py    严格模块 loader / 扫描点生成
src/attodry_control/keithley2400.py        窄 QCoDeS 2400 adapter
src/attodry_control/gates.py               共享 gate 预检/安全控制器
src/attodry_control/three_smu.py            session、审计、cleanup、generator
src/attodry_control/three_smu_live.py       query-only 实时监控的警告/终端面板
src/attodry_control/three_smu_cli.py        无 GUI describe/monitor-live/run
src/attodry_control/three_smu_analysis.py   accepted-only 加载、发现和绘图
notebooks/three_smu_live.ipynb              同 generator 的实时绘图
notebooks/three_smu_analysis.ipynb          SSH-friendly read-only 分析
tests/test_three_smu*.py                    配置、fake run、CLI/Notebook、分析
tests/test_keithley2400.py / test_gates.py  adapter 与共享 gate 核心
```

本阶段执行的 focused fake-instrument 回归为 **71 项通过**（Three-SMU、Keithley 2400、
gate safety 和共享 config，含 live-monitor/error-queue 与确认流程），随后完整离线套件
**193 项通过（2 项可选绘图跳过）**。没有执行
VISA discovery、QCoDeS 连接或真实仪器写入。

## 下一阶段：S1 target offline

只有用户授权 S1 后，才能在 `LK_setup` 的 `lyr` 环境做下列 **不连接 VISA** 的工作：安装/导入
QCoDeS，运行测试，执行 `describe`，记录 Python/QCoDeS/PyVISA 版本。之后仍须另外授权实机
query-only preflight，再另外授权小范围/单向写入扫描。

第一次实机前操作者必须填写并复核：三台地址与可区分 identity、实际确切型号/手册适配、每台
source mode、各自 V/I 绝对边界、对应单位的 source 范围/ramp/readback、两种 compliance、
适用的 gate leakage、settle/NPLC/range/four-wire 值、
样品接线，以及最小可接受扫描计划。不得以模板示例或 fake 测试值替代这些确认。
