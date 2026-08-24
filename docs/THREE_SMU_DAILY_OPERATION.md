# Three-SMU 独立日常操作

本页是三台 Keithley 2400（`smu_bias`、`gate_top`、`gate_bottom`）的日常操作手册。
模块可以脱离 Lock-in 和冷台单独配置、离线检查、运行和分析；第一版不连接或记录
Lock-in，也不控制 attoDRY。

当前验收状态为 **S0 offline complete**。现在允许执行本页的离线步骤；目标电脑验收、
真实 SMU 连接和任何设置写入尚未获授权。看到 `--authorize-writes` 或把 Notebook 中
`AUTHORIZE_WRITES` 改为 `True`，都表示将越过真实写入门槛，不能作为普通离线检查执行。

## 每天最短流程（当前可执行）

在 `module/three-smu` checkout 中打开 PowerShell，激活项目环境，然后执行：

```powershell
python -m attodry_control.three_smu_cli describe `
  --hardware config/three_smu_hardware.local.toml `
  --plan config/three_smu_scan.local.toml
```

`describe` 只读取并严格验证两个 TOML，计算扫描点和样本数，不导入 QCoDeS、不打开
VISA 资源，也不发送仪器命令。成功输出中应有 `"hardware_opened": false`。这只证明
配置在软件层面自洽，不证明地址可访问、接线正确或限值对器件安全。

每日离线流程：

1. 确认 checkout/branch：`git status --short --branch` 应显示 `module/three-smu`。
2. 检查两个本地 TOML，尤其是角色、单位、compliance、leakage、range、ramp 和
   `finish_action`。
3. 执行上面的 `describe`，核对模式、点数、样本数和 `hardware_opened`。
4. 只读分析已有数据时打开 `notebooks/three_smu_analysis.ipynb`。
5. 当前不要执行 `run`，也不要运行实时 Notebook 的硬件单元格。

## 首次建立本地配置

仓库提交的是可共享模板，实际地址和实验限值必须放在被忽略的本地文件中：

```powershell
Copy-Item config\three_smu_hardware.example.toml config\three_smu_hardware.local.toml
Copy-Item config\three_smu_scan.example.toml config\three_smu_scan.local.toml
```

- `three_smu_hardware.example.toml` 故意包含 `CHANGE_ME`，不能直接连接硬件。
- `three_smu_scan.example.toml` 中的数值仅演示格式，不是器件认可的安全参数。
- `.local.toml` 不提交；不要把 VISA 地址、器件限值或实验数据加入 Git。
- 每次复制/更新后都先运行 `describe`。

## 三个语义角色

| TOML 表 | 物理职责 | 允许的源模式 |
|---|---|---|
| `[smu_bias]` | source-drain bias | `voltage` 或 `current` |
| `[gate_top]` | top-gate | 只能是 `voltage` |
| `[gate_bottom]` | bottom-gate | 只能是 `voltage` |

三台仪器必须使用不同 VISA 地址；连接后的 identity 也必须互不重复。不要用“Keithley
#1/#2/#3”代替语义角色，线缆标签、TOML 和样品端连接必须三者一致。

## 硬件 TOML 参数

以下字段在三个角色表中都必须存在。`smu_bias` 的 source 单位取决于 `source_mode`；
两个 gate 的 source 单位固定为 V。

| 字段 | 单位/类型 | 含义与约束 |
|---|---|---|
| `model` | 字符串 | 当前必须为 `Keithley2400`。 |
| `address` | VISA 字符串 | 本地实际地址；三台必须不同，不能含 `CHANGE_ME`。 |
| `timeout_ms` | ms，整数 | VISA 超时，必须至少为 1。 |
| `source_mode` | 枚举 | `voltage`/`current`；gate 只能为 `voltage`。 |
| `compliance_current_a` | A | 正数；电压源模式下限制输出电流。 |
| `compliance_voltage_v` | V | 正数；电流源模式下限制输出电压。 |
| `source_min` | V 或 A | 软件允许的最小 source；必须有限且不大于 0。 |
| `source_max` | V 或 A | 软件允许的最大 source；必须有限且不小于 0，并大于 `source_min`。 |
| `ramp_step` | V 或 A | 每次 source 变化的最大步长，必须为正。 |
| `readback_tolerance` | V 或 A | source readback 与目标的最大允许偏差，必须为正。 |
| `settle_s` | s | 每个 ramp 步后的等待时间，必须非负。 |
| `nplc` | power-line cycles | 测量积分时间，必须为正。越大通常越慢、抗噪更强。 |
| `source_auto_range` | bool | source autorange 开关。 |
| `measure_auto_range` | bool | measure autorange 开关。 |
| `four_wire` | bool | 是否启用四线测量；必须与实际接线一致。 |
| `leakage_limit_a` | A | 仅 gate 必填；正数且不能高于该 gate 的 `compliance_current_a`。 |

`source_min/max` 是独立软件边界，不等于仪器额定范围。所有扫描点会在构造硬件驱动前
与这里的边界比较；超界时应修改扫描计划或由操作者重新确认安全限值，不能靠扩大范围
绕过错误。

## 扫描 TOML 参数

### `[scan]`

| 字段 | 类型/单位 | 说明 |
|---|---|---|
| `mode` | 枚举 | `time_trace`、`bias_iv`、`top_gate_transfer`、`bottom_gate_transfer`、`paired_gate`、`multi_smu_map` 或 `software_pulse`。 |
| `samples_per_point` | 整数 | 每个正式点的重复样本数，至少 1。 |
| `delay_s` | s | 到达正式目标后、采样前的公共等待时间，必须非负。 |
| `bidirectional` | bool | 扫完正向路径后追加反向路径。 |
| `serpentine` | bool | 多维 map 使用蛇形路径；一般只对 `multi_smu_map` 有意义。 |
| `finish_action` | 枚举 | 推荐 `zero_disable`；`hold` 还要求额外 `--authorize-hold`。 |
| `point_count` | 整数 | `time_trace` 的点数或 `software_pulse` 的周期数，至少 1。 |
| `pulse_high_s` | s | 软件脉冲高电平时间；非脉冲模式必须为 0。 |
| `pulse_period_s` | s | 软件脉冲周期；非脉冲模式必须为 0，脉冲时不得小于 `pulse_high_s`。 |

### `[smu_bias]`、`[gate_top]`、`[gate_bottom]`

| 字段 | 类型/单位 | 说明 |
|---|---|---|
| `role` | 枚举 | `off`、`fixed` 或 `sweep`。 |
| `fixed` | V 或 A | `fixed` 时的目标；`off` 时通常保持 0。 |
| `start` | V 或 A | `sweep` 起点；软件脉冲模式中为 low。 |
| `stop` | V 或 A | `sweep` 终点；软件脉冲模式中为 high。 |
| `step` | V 或 A | sweep 步长的正幅值；即使角色为 `off/fixed` 也需保留正值。 |

扫描单位始终跟随对应硬件角色的 `source_mode`。例如 voltage-source bias 的
`start = 0.001` 表示 1 mV；current-source bias 则表示 1 mA。不要从数值大小猜单位。

## 模式和角色组合

| `mode` | 合法的 sweep 组合 | 关键规则 |
|---|---|---|
| `time_trace` | 无 sweep | 三台只能 `off/fixed`；使用 `point_count`。 |
| `bias_iv` | 仅 `smu_bias` | gate 可 `off/fixed`。 |
| `top_gate_transfer` | 仅 `gate_top` | bias/bottom 可 `off/fixed`。 |
| `bottom_gate_transfer` | 仅 `gate_bottom` | bias/top 可 `off/fixed`。 |
| `paired_gate` | top 和 bottom | 两个 gate 生成的点数必须相同。 |
| `multi_smu_map` | 1–3 个角色 | 可组合 bias/top/bottom，支持 serpentine。 |
| `software_pulse` | 恰好 1 个角色 | `start=low`、`stop=high`，使用脉冲时间和 `point_count`。 |

非 `software_pulse` 模式必须令 `pulse_high_s = 0.0`、`pulse_period_s = 0.0`。
`bidirectional` 会追加返程点；预计记录数还要乘以 `samples_per_point`。最终以
`describe` 输出的点数和样本数为准。

## 真实运行（当前未授权）

下面是接口说明，不是当前执行许可。只有完成目标电脑离线验收、填写并人工复核本地
限值，而且用户对本次真实连接和写入另行明确授权后，才可运行：

```powershell
python -m attodry_control.three_smu_cli run `
  --hardware config/three_smu_hardware.local.toml `
  --plan config/three_smu_scan.local.toml `
  --output-dir run_data/three_smu `
  --authorize-writes
```

若 `finish_action = "hold"`，CLI 还会要求 `--authorize-hold`。日常默认使用
`zero_disable`；`hold` 会让输出在正常结束后保持启用，不能当作方便选项。

真实运行前必须逐项确认：

- 三台仪器的物理角色、VISA 地址、线缆和样品端一致，且各不重复；
- 三台前面板均显示 output off，source setpoint 为零；
- 器件允许的 bias/gate 极限、compliance、gate leakage limit 和 ramp step 已由操作者确认；
- `four_wire`、source mode 和实际接线一致；
- 完整扫描目标均在硬件 TOML 的 source range 内；
- 默认结束行为为 `zero_disable`，输出目录有足够空间；
- 已明确本次授权边界。授权连接不自动等于授权写入，授权小范围测试也不等于授权完整扫描。

连接后会先做只查询 preflight。若发现任一 output 已开，程序应停止且不进行配置写入，
必须人工检查前面板和接线。只有 output 已关时，程序才会确认零 setpoint、配置安全参数，
然后按有界 ramp 执行。三个角色的正式读数是依次读取并各自带时间戳，不是同时采样。

## 实时 Notebook（当前未授权）

`notebooks/three_smu_live.ipynb` 与 CLI 调用同一个 `ThreeSmuSession` 和扫描 generator，
不是另一套控制逻辑。默认：

```python
AUTHORIZE_WRITES = False
```

保持 `False` 时会 fail closed；不要为了“看看图”改成 `True`。只有真实运行获得明确授权
后，才可复核 `HARDWARE_TOML`、`PLAN_TOML`、`OUTPUT_DIR`，再人工改变这一门槛。
实时图显示 bias 电流随时间变化，但数据仍由底层 session 写入标准 run 目录。

## 数据、状态和 accepted-only 分析

每次真实 run 会在 `run_data/three_smu` 下建立独立目录，包含：

| 文件 | 内容 |
|---|---|
| `metadata.json` | 配置快照、run 状态、accepted 与 cleanup 结果。 |
| `raw.jsonl` | 原始事件审计；保留 rejected、partial、interrupted 和 cleanup 事件。 |
| `data.csv` | 便于检查的长表数据。 |

打开 `notebooks/three_smu_analysis.ipynb`，将 `RUN_PATH` 指向 run 目录、
`metadata.json` 或 `data.csv`。默认保持：

```python
INCLUDE_REJECTED = False
INCLUDE_PROBLEM = False
```

默认 loader 只返回 run 为 `completed`、`accepted=true` 且样本为 clean 的数据。
`INCLUDE_REJECTED/INCLUDE_PROBLEM` 只用于审计，不能把问题数据混入默认科学分析。
Notebook 支持 bias I-V、gate transfer、time trace、gate leakage 和 2D map；第一版没有
Lock-in 数据，也不计算基于 Lock-in 的电阻或相位。

## 正常结束、中断和异常

- `zero_disable`：依次对 bias、top gate、bottom gate ramp 到零并关闭输出，再保存最终确认读回。
- `Ctrl+C` 或异常：记录 interrupted/partial/rejected 原始事件并进入同一清理路径。
- 任一步通信失败：不能推断 source 为零或 output 已关。只相信最后一次确认读回，停止继续实验，
  在记录中查明未确认角色，并人工查看三台前面板。
- cleanup 未确认成功：run 不应 accepted；不得只看进程已经退出就拔线或改接线。
- 电脑硬崩溃无法保证软件清理，恢复后必须先人工确认三台 SMU 状态。

## 常见问题

### `CHANGE_ME`、缺字段或类型错误

正在使用模板或本地配置未填完。修正 `.local.toml` 后重新执行 `describe`；不要修改解析器
来接受占位符。

### duplicate address / identity

两个语义角色指向同一资源，或连接后两台返回相同 identity。停止，不要交换软件角色来
规避错误；检查 VISA 地址、GPIB 地址、USB 序列号和实际线缆标签。

### target outside source range

扫描点超出本地硬件边界。先检查单位与角色，再缩小 scan；只有操作者重新确认器件安全
边界时才能修改 `source_min/max`。

### output already enabled

preflight 发现既有输出。程序应在配置写入前停止。人工确认三台前面板、样品状态和最后
已知 setpoint；不得自动关闭后立即重试。

### leakage、compliance、near-compliance 或 readback mismatch

视为安全拒绝，不继续后续扫描。保留 raw audit，检查器件、接线、量程、compliance 和
限值；获得新的实验决定前不要提高阈值。

### QCoDeS/VISA 找不到或导入失败

`describe` 不会验证 VISA 可用性。真实连接阶段再核对当前 Python 环境、QCoDeS driver、
VISA backend 和系统资源管理器；排障过程仍不得发送设置写命令，除非该步骤另有授权。

## 相关文档

- 模块设计、阶段边界和验收条件：[`modules/THREE_SMU.md`](modules/THREE_SMU.md)
- 全项目硬件规则：[`HARDWARE_AND_SAFETY.md`](HARDWARE_AND_SAFETY.md)
- 当前交接状态：[`PROJECT_HANDOFF.md`](PROJECT_HANDOFF.md)
- 开发阶段：[`DEVELOPMENT_STAGES.md`](DEVELOPMENT_STAGES.md)
