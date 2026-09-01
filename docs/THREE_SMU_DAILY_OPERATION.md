# Three-SMU 独立日常操作

本模块可在不配合 Lock-in、冷台和磁场的情况下独立配置、测试和（commissioning 后）运行。
当前软件路径已完成 target-offline 验证；仅 `gate_bottom` 的一次有界读写验收已完成。
`smu_bias`、`gate_top` 及任何新的真实运行仍需要各自的明确授权和前面板确认。

## 每日入口

先确认当前 checkout：

```powershell
git status --short --branch
git log -1 --oneline
python -c "import attodry_control; print(attodry_control.__file__)"
```

首次建立本机配置：

```powershell
Copy-Item config\hardware.example.toml config\hardware.local.toml
```

`hardware.local.toml`、真实地址和实验数据均被 Git ignore，不得提交。离线检查命令为：

```powershell
python -m attodry_control.three_smu_cli describe
```

成功输出包含 `"hardware_opened": false`。这只证明 TOML 与扫描形状自洽，不证明接线或器件
安全。

## 每台 SMU 的参数

三台语义角色使用完全相同的单表格式；不再有 `[gate_top.smu]` 或
`[gate_bottom.smu]` 子表：

```toml
[smu_bias]
model = "Keithley2400"
address = "CHANGE_ME_BIAS_SMU_VISA_ADDRESS"
source_mode = "voltage"       # "voltage" 或 "current"
max_abs_voltage_v = "CHANGE_ME"
max_abs_current_a = "CHANGE_ME"
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_top]                     # gate_bottom 独立填写
model = "Keithley2400"
address = "CHANGE_ME_TOP_GATE_VISA_ADDRESS"
source_mode = "voltage"
max_abs_voltage_v = "CHANGE_ME"
max_abs_current_a = "CHANGE_ME"
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false
```

VISA timeout 由程序固定为 `5000 ms`，TOML 不再接受 `timeout_ms`。这是通信命令
最多等待 5 s，不是每个扫描点的等待时间。

`[three_smu_run.<role>]` 的 `role` 是启用状态的唯一事实来源。若某角色为
`role = "off"`，它的硬件表可以整段省略；loader 不解析它、不打开它的
VISA resource、不读取或记录它。这不能证明该仪器已关闭或已归零；其物理
状态必须视为未知。任一 `fixed` 或 `sweep` 角色都必须有完整的同名硬件表。

两个 `max_abs_*` 是每台设备独立的硬边界：任何请求 source 值先与 source-mode 对应边界比较，
每次实际 V/I 读回再同时与两条边界比较。不得用 top gate 的值代替 bottom gate，也不得用模板
示例推断样品限值。

compliance 是 Keithley 2400 的硬件输出保护参数；autorange 是测量/源量程选择，两者不同：

- voltage source：软件把 `max_abs_current_a` 设置为 current compliance；
- current source：软件把 `max_abs_voltage_v` 设置为 voltage compliance；
- 设置后查询 compliance 和两个 range；实际 compliance 高于边界立即停止。

因此 TOML 不再单独填写 compliance。标准 2400 还要求边界处于型号能力和 V-I 功率包络内。
当前必须保留两个 autorange 为 `true`，因为本 schema 没有固定量程字段。

compliance 数值本身不是只能从几个离散档位中选择，但会受 measurement range 约束。标准
2400 的 current ranges 为 1 µA、10 µA、100 µA、1 mA、10 mA、100 mA、1 A；current
compliance 上限可到所选 range 的约 1.05 倍，且 compliance 不能低于当前 measurement range
的约 0.1%。voltage compliance 的可编程范围约为 200 µV–210 V。autorange 负责选择 range，
程序不把 `max_abs_*` 四舍五入成某个 range，而是写入该数值作为 compliance 并查询仪器实际
接受的值；实际值高于批准边界即拒绝。

`nplc = 1.0` 表示一个电网周期。芬兰 50 Hz 下一个周期为 20 ms；不要写 `nplc = 0.020`。
`delay_s` 是设完一个正式点后、读取 formal sample 前的唯一软件等待时间。

已删除且 loader 会拒绝的旧字段包括：独立 compliance、`leakage_limit_a`、所有
`source_min_*`/`source_max_*`、`ramp_step_*`、`readback_tolerance_*` 和 `settle_s`。
`timeout_ms` 也已从 Three-SMU 角色配置删除并会被 strict loader 拒绝。

## 扫描向量

运行公共参数：

```toml
[three_smu_run]
output_directory = "../data/three_smu"
run_name = "sample-A-dual-gate-map"
note = ""
mode = "multi_smu_map"
samples_per_point = 1
delay_s = 0.1
serpentine = true
finish_action = "zero_disable"
point_count = 1
pulse_high_s = 0.0
pulse_period_s = 0.0
```

每台 SMU 的 `bidirectional` 单独设置。三种角色写法：

```toml
[three_smu_run.smu_bias]
role = "fixed"
bidirectional = false
fixed = 0.001

[three_smu_run.gate_top]
role = "sweep"
bidirectional = true
points = [1.0, 3.0, 7.0, 2.0]

[three_smu_run.gate_bottom]
role = "sweep"
bidirectional = false
ranges = [
  { min = -1.0, max = 1.0, scale = "linear", step = 0.05 },
]
```

active sweep 的 `points` 与 `ranges` 二选一；旧的顶层 `start/stop/step` 不再接受。显式
`points` 保持输入顺序、重复和非单调点；上述 top gate 双向展开为
`[1,3,7,2,7,3,1]`。

`ranges` 支持以下三种段，并可在一个数组中任意依次组合：

linear 按步长（包含 min/max）：

```toml
ranges = [
  { min = -0.1, max = 0.1, scale = "linear", step = 0.05 },
]
```

linear 按总点数等间距（包含 min/max）：

```toml
ranges = [
  { min = -0.1, max = 0.1, scale = "linear", points = 5 },
]
```

log 在正数区间按总点数对数等间距（包含 min/max）：

```toml
ranges = [
  { min = 1e-6, max = 1e-3, scale = "log", points = 10 },
]
```

多段按列出顺序拼成一个最终向量：

```toml
ranges = [
  { min = -1.0, max = -0.2, scale = "linear", step = 0.1 },
  { min = -0.1, max = 0.1, scale = "linear", points = 11 },
  { min = 0.2, max = 1.0, scale = "linear", step = 0.1 },
]
```

每段必须 `max > min`。linear 段的 `step`/`points` 必须且只能写一个；log 段只接受
`points` 且 min/max 都必须大于 0。多段边界不会自动去重：若前一段 max 等于后一段 min，
该值会按配置出现两次。`bidirectional=true` 在所有 ranges 完整拼接后再追加反向路径，
且不重复最终转折点。需要降序或任意轨迹时直接使用显式 `points`。

`off` 表推荐只保留 `role = "off"`。为了方便暂时关闭某台 SMU，off 表中已知的
`bidirectional`/`fixed`/`points`/`ranges`/`start`/`stop`/`step` 可以暂时保留，loader 不解析
或验证它们，内部统一归一为 off。字段名拼错仍会被拒绝。将该角色改回
`fixed` 或 `sweep` 时，对应参数会重新严格校验。`fixed` 表必须使用 `fixed` 且
`bidirectional=false`。
例如只扫 bottom gate 时，可完全删除 `[smu_bias]` 和 `[gate_top]` 硬件表：

```toml
[gate_bottom]
model = "Keithley2400"
address = "CHANGE_ME_BOTTOM_GATE_VISA_ADDRESS"
source_mode = "voltage"
max_abs_voltage_v = "CHANGE_ME"
max_abs_current_a = "CHANGE_ME"
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[three_smu_run.smu_bias]
role = "off"
bidirectional = false

[three_smu_run.gate_top]
role = "off"
bidirectional = false

[three_smu_run.gate_bottom]
role = "sweep"
bidirectional = false
points = [-1.0, -0.3, 0.0, 0.8]
```

七种 `mode`：

| mode | sweep 角色 |
|---|---|
| `time_trace` | 无 |
| `bias_iv` | 仅 `smu_bias` |
| `top_gate_transfer` | 仅 `gate_top` |
| `bottom_gate_transfer` | 仅 `gate_bottom` |
| `paired_gate` | 两个 gate；各自双向展开后长度必须相同 |
| `multi_smu_map` | 任意 1–3 台；各自展开后做笛卡尔积 |
| `software_pulse` | 恰好一台、恰好两个值、禁止 bidirectional |

固定 bias、扫描两个 gate 使用 `multi_smu_map`，把 bias 设为 `fixed`、两个 gate 设为 `sweep`。

## 实时监控与真实运行边界

以下命令只有获得当次真实查询授权后才能运行：

```powershell
python -m attodry_control.three_smu_cli monitor-live
```

它只连接并显示 `fixed`/`sweep` 角色的 identity、source mode/setpoint、V/I/R、
input/output 状态、compliance/trip、source/measurement range 和 2/4-wire。Keithley 2400 C32
在 output OFF 时不接受 `:READ?` 或 protection-trip query，因此 monitor 保持 output OFF，且把
V/I/R/trip 显示为 `n/a`；output ON 时才查询这些值。Ctrl+C 会关闭 VISA resource 并正常停止，
不打印 traceback。默认不消费 error queue；只有单独授权后才用：

```powershell
python -m attodry_control.three_smu_cli monitor-live --consume-status-queue
```

不得与 scan 并发占用同一 VISA resource。

commissioning 后的短运行命令为：

```powershell
python -m attodry_control.three_smu_cli run
```

它自动读取 `config/hardware.local.toml`，在打开资源前打印摘要后直接开始单次 session，
不再要求输入 `RUN THREE SMU`。`finish_action="hold"` 仍要求精确输入 `HOLD OUTPUTS`；
日常默认应为 `zero_disable`。

### `run` 内嵌实时面板

每个正式样本完成后，终端立即显示：

- 当前样本序号/总数、repeat、segment 和累计运行时间；
- 每台 active SMU 的 source setpoint readback、V、I、R 和 output ON/OFF；
- `CLEAN` 或 `PROBLEM`。

这是已记录 formal sample 的内存 FIFO 展示，不增加任何硬件查询、写入、状态队列消费或
第二个 SMU session。若 `PROBLEM` 样本触发 fail-closed 中止，终端才会额外显示该样本已经
读取的 status/error queue 与问题说明；随后原有 cleanup 仍照常执行。正常 `CLEAN` 样本
不显示 status/error queue。

在宽度至少 96 列的终端中，首个样本会打印一次固定列宽表头，随后每个 sample 追加一条
进度摘要和每个 active role 的读回行；单位自动使用工程前缀，例如 `100 mV`、`500 nA` 和
`2.00 GΩ`。较窄的 PowerShell/SSH 终端自动改为每个 role 一行的紧凑格式，以避免折行破坏
列对齐。两种格式都是同一内存样本，不改变记录、扫描或硬件访问。

正式点执行“每台直接写一次目标 → 等 `delay_s` → 读回并记录”，没有软件 ramp 和独立
settle。cleanup 只对本次 active 角色执行“直接写 0 → 等 `delay_s` → 在 output ON 时读回 V/I →
output off → 查询确认 0 setpoint/output OFF”。关闭后的 V/I 明确为 unavailable，不会伪造读回。
通信失败时不能声称已经归零或关闭；本次 active
仪器必须查看前面板，off 角色始终保持“未连接/物理状态未知”。

## 数据与分析

每个 run 目录保存 schema v5 `metadata.json`、`raw.jsonl`、`data.csv`。metadata 明确记录
`active_roles` 与 `off_roles`，硬件快照仅含 active 角色；CSV 保留稳定的三角色列，off
角色的列留空。requested target 与实际
source setpoint/V/I 分开记录；实际数值差异本身不触发 tolerance rejection，但 V/I 绝对越界、
trip、output 状态或错误队列问题仍会拒绝。

`notebooks/three_smu_analysis.ipynb` 默认只读取 completed/accepted/clean formal samples；原始
rejected/problem 记录只能显式 opt-in 审计。`notebooks/three_smu_live.ipynb` 调用同一 session
generator，不是另一套写路径，且授权开关默认必须保持 `False`。它的实时图只从 session 回调
填入的内存 FIFO 消费 formal samples，不直接访问硬件。

## 常见停止原因

- `CHANGE_ME`、缺字段或旧字段：按新模板迁移后重新 `describe`；
- target exceeds absolute limit：缩小计划，不要未经确认提高 `max_abs_*`；
- compliance readback exceeds max：停止，检查档位/型号/前面板，不要绕过；
- output already enabled、identity 重复、mode 不符、V/I 越界、trip 或脏状态：停止并人工检查；
- 通信/cleanup 失败：以最后确认读回为准，人工确认零与 output-off。

设计和阶段边界见 [`modules/THREE_SMU.md`](modules/THREE_SMU.md)。
