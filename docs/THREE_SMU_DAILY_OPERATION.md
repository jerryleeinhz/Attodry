# Three-SMU 独立日常操作

本页适用于三台 Keithley 2400：`smu_bias`、`gate_top`、`gate_bottom`。它可脱离
Lock-in、冷台和磁场模块独立使用；第一版不读取或记录 Lock-in，也不控制 attoDRY。

当前状态是 **S0 offline complete（离线实现完成）**。允许的操作只有配置检查、fake-
instrument 测试和只读数据分析。尚未获授权连接真实 SMU、查询真实仪器，或发送任何写命令。

## 每日离线流程

在 `module/three-smu` worktree 中，先确认位置和分支：

```powershell
git status --short --branch
```

它应显示 `## module/three-smu...`。然后从共享模板建立一个仅本机保存的配置：

```powershell
Copy-Item config\hardware.example.toml config\hardware.local.toml

python -m attodry_control.three_smu_cli describe `
  --config config\hardware.local.toml
```

`describe` 只读 TOML、验证限值和扫描形状、计算点数；它不会导入 QCoDeS、打开 VISA 或发送
仪器命令。成功输出必须含有 `"hardware_opened": false`。模板中的 `CHANGE_ME` 是故意的，
因此只有填写完整后 `describe` 才会成功；成功也只证明软件配置自洽，不证明接线或器件安全。

每天先做：

1. 检查 `git status`，不要在主 checkout 编辑本模块。
2. 审核一个 `hardware.local.toml` 中的角色、单位、compliance、leakage、范围、ramp 和结束动作。
3. 运行上面的 `describe` 并核对 mode、点数、样本数。
4. 要分析既有数据时，仅打开 `notebooks/three_smu_analysis.ipynb`。
5. 当前不要运行 `run`，不要把实时 Notebook 的任一授权开关改为 `True`。

`hardware.local.toml` 被 `.gitignore` 排除。地址、器件限值、实验 note 和数据目录都不能提交。

## 一个配置文件的结构

`config/hardware.example.toml` 是唯一日常模板。已有的 `[gate_top]` 和 `[gate_bottom]`
仍是 gate 安全参数的唯一来源；不要另抄一份 gate 限值。

```toml
[smu_bias]
model = "Keithley2400"
address = "CHANGE_ME_BIAS_SMU_VISA_ADDRESS"
source_mode = "voltage"       # 或 "current"
compliance_current_a = "CHANGE_ME"
compliance_voltage_v = "CHANGE_ME"
max_abs_voltage_v = "CHANGE_ME"
max_abs_current_a = "CHANGE_ME"
source_min_v = "CHANGE_ME"
source_max_v = "CHANGE_ME"
ramp_step_v = "CHANGE_ME"
readback_tolerance_v = "CHANGE_ME"
source_min_a = "CHANGE_ME"
source_max_a = "CHANGE_ME"
ramp_step_a = "CHANGE_ME"
readback_tolerance_a = "CHANGE_ME"
settle_s = "CHANGE_ME"
timeout_ms = "CHANGE_ME"
nplc = "CHANGE_ME"
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_top]                     # gate_bottom 相同
model = "Keithley2400"
address = "CHANGE_ME_TOP_GATE_VISA_ADDRESS"
source_mode = "voltage"       # 或 "current"；两个 gate 分开选择
compliance_a = "CHANGE_ME"    # voltage source 的 current compliance
compliance_voltage_v = "CHANGE_ME"
leakage_limit_a = "CHANGE_ME"
max_abs_voltage_v = "CHANGE_ME"
max_abs_current_a = "CHANGE_ME"
source_min_v = "CHANGE_ME"
source_max_v = "CHANGE_ME"
ramp_step_v = "CHANGE_ME"
readback_tolerance_v = "CHANGE_ME"
source_min_a = "CHANGE_ME"
source_max_a = "CHANGE_ME"
ramp_step_a = "CHANGE_ME"
readback_tolerance_a = "CHANGE_ME"
settle_s = "CHANGE_ME"

[gate_top.smu]
timeout_ms = "CHANGE_ME"
nplc = "CHANGE_ME"
source_auto_range = true
measure_auto_range = true
four_wire = false
```

三台设备分别设置，不能用一个 gate 的值替代另一个。每台都必须填写
`max_abs_voltage_v` 和 `max_abs_current_a`；无论选择哪种 source mode，程序都会持续检查
实际 V/I 读回，任一绝对值越界即拒绝 run。`source_min_v/source_max_v` 与
`source_min_a/source_max_a` 是允许请求的 source 范围，不是仪器量程，且必须包含零并位于对应
`max_abs_*` 边界内。

`source_mode = "voltage"` 时使用 `*_v` 的 source/ramp/readback 字段，Keithley 使用
current compliance（bias 表叫 `compliance_current_a`，gate 父表为兼容通用 gate 配置仍叫
`compliance_a`）。`source_mode = "current"` 时使用 `*_a` 字段并设置
`compliance_voltage_v`。两个 compliance 与两个 `max_abs_*` 都必须明确填写，并满足：

```text
compliance_current_a <= max_abs_current_a
compliance_voltage_v <= max_abs_voltage_v
```

`leakage_limit_a` 是 voltage-source gate 的更早停止阈值，必须满足
`leakage_limit_a <= compliance_a <= max_abs_current_a`。current-source gate 的电流是主动施加量，
不能称为 leakage，因此该模式不执行 leakage 判据，仍执行电压/电流绝对边界与 voltage
compliance。`smu_bias` 不使用 gate leakage 判据。三台地址必须不同。

`[gate_*.smu]` 只保存 2400 专属的 timeout、NPLC、量程和四线制设置；它不拥有
source、leakage、compliance、ramp 或 settle 限值。每个 gate 自己的父表分别是它唯一的
安全参数来源。

旧 Three-SMU 本地文件若仍使用无单位的 `source_min/source_max/ramp_step/readback_tolerance`，
新版 loader 会拒绝而不会猜测。把当前 source mode 对应的值迁移到 `_v` 或 `_a` 字段，并由
操作者另行确认两个 `max_abs_*`、两个 compliance 和另一 source mode 的参数；不要从旧 source
范围自动推断器件的 V/I 绝对安全边界。既有 run 数据不需要改写。

扫描和记录参数在同一个文件的 `[three_smu_run]`：

```toml
[three_smu_run]
output_directory = "../data/three_smu"  # 相对本 TOML 文件
run_name = "sample-A-dual-gate-map"
note = "operator note; no secrets"
mode = "multi_smu_map"
samples_per_point = 1
delay_s = 0.1
bidirectional = false
serpentine = true
finish_action = "zero_disable"
point_count = 1
pulse_high_s = 0.0
pulse_period_s = 0.0

[three_smu_run.smu_bias]
role = "fixed"
fixed = 0.001
start = 0.0
stop = 0.0
step = 1.0

[three_smu_run.gate_top]
role = "sweep"
fixed = 0.0
start = -1.0
stop = 1.0
step = 0.02
```

每个角色都有 `role = "off" | "fixed" | "sweep"`，以及 `fixed/start/stop/step`。
这些数值的单位跟随该角色的 `source_mode`：voltage 为 V，current 为 A。`step` 始终填正数；
实际扫向由 start/stop 决定。`multi_smu_map` 可以扫描 1–3 个角色，
因此支持“固定 bias、扫描两个 gate”。例如上例中再令
`[three_smu_run.gate_bottom] role = "sweep"`，即可形成双 gate map；bias 保持 `fixed`。

| mode | sweep 角色 |
|---|---|
| `time_trace` | 无 |
| `bias_iv` | 仅 `smu_bias` |
| `top_gate_transfer` / `bottom_gate_transfer` | 对应一个 gate |
| `paired_gate` | 两个 gate，点数必须相同 |
| `multi_smu_map` | 1–3 个角色，可 serpentine |
| `software_pulse` | 恰好一个角色 |

非 pulse 模式的两个 pulse 时间必须为零。`finish_action` 日常应为 `zero_disable`；
`hold` 还要求 CLI 的额外 `--authorize-hold`，不能作为便利默认值。

## 未来真实运行门槛（当前未授权）

以下命令是接口说明，不是操作许可：

```powershell
python -m attodry_control.three_smu_cli run `
  --config config\hardware.local.toml `
  --authorize-writes `
  --authorize-status-consumption
```

除了本次扫描的明确连接/写入授权，还必须给 `--authorize-status-consumption`。Keithley 的
错误队列查询会消费队列项目，程序不会把这种有副作用的状态读取伪装成普通只读查询。若使用
`finish_action = "hold"`，还必须另给 `--authorize-hold`。

获授权的程序仍会先验证：身份唯一、source mode 正确、output 已关闭、source setpoint 与当前
source mode 对应的实际读回在零附近、实际 V/I 均未越过各自绝对边界、已查询的状态干净。
voltage-source gate 还复用通用 gate 的零电压/leakage 预检。任一项失败时不配置或接管仪器。
之后才可配置 compliance/NPLC/range/four-wire、从零开启输出、按当前单位的受限步长 ramp，
并在每步记录和检查 V/I、setpoint、output、trip、near-compliance、适用时的 leakage 和状态。

异常、Ctrl+C 与正常的 `zero_disable` 都走同一 cleanup：先 `smu_bias`，再 top/bottom gate，
逐步回零并关闭输出。通信失败绝不表示仪器已归零；记录保留最后确认读回，并要求人工查看面板。

## Notebook 和数据分析

`notebooks/three_smu_live.ipynb` 使用同一 `ThreeSmuSession` generator，不直接导入 QCoDeS。
它默认：

```python
AUTHORIZE_WRITES = False
AUTHORIZE_STATUS_CONSUMPTION = False
```

只有未来针对本次真实扫描的明确授权才能同时修改这两个值。

每个 run 目录含：

| 文件 | 内容 |
|---|---|
| `metadata.json` | schema v3、requested 的单位明确 V/I 配置与计划、实际 preflight、run name/note、Git/import/config provenance、结束状态和 cleanup 错误。 |
| `raw.jsonl` | start/preflight/configure/ramp/sample/error/cleanup 原始审计事件。 |
| `data.csv` | 每点 requested source、实际 V/I/R、setpoint、状态和质量标记。 |

`notebooks/three_smu_analysis.ipynb` 不使用桌面文件选择器。设置 `DATA_DIRECTORY` 为本地目录、SSH
挂载目录或网络盘；它默认列出 completed/accepted run，跳过不完整目录，并选最新一个。也可手动
设置 `RUN_PATH`。默认：

```python
INCLUDE_REJECTED = False
INCLUDE_PROBLEM = False
```

问题/拒绝数据只用于审计，不能混入默认分析。双 gate map 若 bias 也被扫描，绘图时须选择一个
bias slice，例如 `fixed_coordinates={'smu_bias': 0.001}`；若 bias 固定，则不需要 slice。

## 常见停止原因

- `CHANGE_ME` / 缺字段：仍在用模板或配置不完整；修 TOML，再运行 `describe`。
- address / identity 重复：停止，核对 VISA、序列号和线缆标签，不要交换软件角色规避。
- target outside source range：检查 `source_mode`、对应的 `_v`/`_a` 字段和计划；未经新的器件
  安全确认不要扩大范围或 `max_abs_*`。
- output already enabled、non-zero preflight 或状态不干净：不自动接管；人工检查前面板和样品。
- voltage/current absolute limit、leakage、trip、near-compliance、readback mismatch：保留审计
  记录并停止；不要通过提高阈值继续。

相关设计与阶段边界见 [`modules/THREE_SMU.md`](modules/THREE_SMU.md)。
