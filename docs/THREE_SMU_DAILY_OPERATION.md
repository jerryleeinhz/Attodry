# Three-SMU 独立日常操作

本模块可在不配合 Lock-in、冷台和磁场的情况下独立配置、测试和（commissioning 后）运行。
当前仍是 `S0 offline complete`：真实 SMU 连接、查询和写入尚未授权。

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

`smu_bias` 直接包含 Keithley 参数；两个 gate 的 Keithley 参数放在各自 `.smu` 子表：

```toml
[smu_bias]
model = "Keithley2400"
address = "CHANGE_ME_BIAS_SMU_VISA_ADDRESS"
timeout_ms = "CHANGE_ME"
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

[gate_top.smu]
timeout_ms = "CHANGE_ME"
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false
```

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
start = -1.0
stop = 1.0
step = 0.05
```

`points` 与 `start/stop/step` 二选一。显式向量保持输入顺序、重复和非单调点；上述 top gate
双向展开为 `[1,3,7,2,7,3,1]`。range 的 `step` 必须为正，方向由 start/stop 决定。
`off` 表只写 `role` 和 `bidirectional=false`；`fixed` 表只额外写 `fixed`。

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

它显示三台的 identity、source mode/setpoint、V/I/R、input/output 状态、compliance/trip、
source/measurement range 和 2/4-wire。默认不消费 error queue；只有单独授权后才用：

```powershell
python -m attodry_control.three_smu_cli monitor-live --consume-status-queue
```

不得与 scan 并发占用同一 VISA resource。

commissioning 后的短运行命令为：

```powershell
python -m attodry_control.three_smu_cli run
```

它自动读取 `config/hardware.local.toml`，在打开资源前打印摘要并要求精确输入
`RUN THREE SMU`。`finish_action="hold"` 另要求 `HOLD OUTPUTS`；日常默认应为
`zero_disable`。

正式点执行“每台直接写一次目标 → 等 `delay_s` → 读回并记录”，没有软件 ramp 和独立
settle。cleanup 执行“直接写 0 → 等 `delay_s` → 读回记录 → output off → 再读回”。通信失败
时不能声称已经归零或关闭，必须查看三台前面板。

## 数据与分析

每个 run 目录保存 schema v4 `metadata.json`、`raw.jsonl`、`data.csv`。requested target 与实际
source setpoint/V/I 分开记录；实际数值差异本身不触发 tolerance rejection，但 V/I 绝对越界、
trip、output 状态或错误队列问题仍会拒绝。

`notebooks/three_smu_analysis.ipynb` 默认只读取 completed/accepted/clean formal samples；原始
rejected/problem 记录只能显式 opt-in 审计。`notebooks/three_smu_live.ipynb` 调用同一 session
generator，不是另一套写路径，且授权开关默认必须保持 `False`。

## 常见停止原因

- `CHANGE_ME`、缺字段或旧字段：按新模板迁移后重新 `describe`；
- target exceeds absolute limit：缩小计划，不要未经确认提高 `max_abs_*`；
- compliance readback exceeds max：停止，检查档位/型号/前面板，不要绕过；
- output already enabled、identity 重复、mode 不符、V/I 越界、trip 或脏状态：停止并人工检查；
- 通信/cleanup 失败：以最后确认读回为准，人工确认零与 output-off。

设计和阶段边界见 [`modules/THREE_SMU.md`](modules/THREE_SMU.md)。
