# 光电压 / 光电流运行指南

日常统一使用 `python -m attodry_control.combination_cli run` 和 `monitor`。
终端、只读监控及 unified plotting 操作见 [Combination 光电响应指南](COMBINATION_PHOTORESPONSE_GUIDE.md)。
原 photoresponse_cli 入口保持兼容，下文历史配置/保护规则继续适用。

本项目完整目标为 combination 组合测量：NKT 扫波长和光功率，CLD 固定波长调功率，
PM100D 反馈稳定实际光功率，并在正式窗口保存电学读数和各仪器状态。
此前实现为开环 NKT/CLD 光源、6221 DC、2182A 新鲜读数、
2400 电压/电流测量、可选温磁场扫描、SQLite 审计与新尝试恢复。
**PM100D 闭环与组合入口已完成离线集成；此前完成声明仅适用于开环流程，硬件联合验收尚未完成。**
程序不会扫描总线、复位仪器或自动接管已开启的输出。工作边界见 [工作包](modules/PHOTORESPONSE.md)。

## 单次暗态与外层功率扫描（2026-10-15）

新增可选协议将 `dark` 放在每轮功率扫描的首点。原 `target_power_w` /
`target_powers_w` 配置仍执行逐点暗—亮—暗；要使用新协议，在 `[power_feedback]`
中用下面的字段替换目标功率字段（W），保留原反馈限值、容差及窗口配置：

```toml
[power_feedback]
power_points = ["dark", 50e-6, 100e-6, 150e-6, 200e-6]
initial_actuator_mode = "previous" # point（默认）| previous
# 其余 actuator、范围、步长、功率边界、容差、window 等仍须填写。

[photoresponse_combination]
order = ["repeat", "environment", "optical", "target_power_w", "bias", "smu"]
```

在 `[photoresponse]` 可加 `dark_recovery_s`；省略时使用 `settle_s`。
两者是操作者确定的样品响应时间，不是 PM 积分时间或锁相时间常数。
新的可直接运行离线示例：
`config/photoresponse_dark_nkt_simulation.toml`、
`config/photoresponse_dark_cld_simulation.toml`。示例值均为合成值。

执行顺序：完整扫描重复 → 温磁组合点（外层判稳）→ 光学点（NKT 波长/带宽，
CLD 固定波长）→ dark/各亮态功率 → 全部 bias/SMU 电学点。
配置顺序不符合这些外内层约束时，在打开仪器前拒绝。

- `dark` 确认关光、等待恢复并扫描全部电学坐标。它不是 0 W 反馈目标，
  不执行最小亮态信号检查或寻光。PM 只记录配置/状态核对，未触发暗功率 READ。
- 每个亮点：所有电学输出已安全关闭 → 准备光学设置 → PM 调功并判稳 →
  核对电学配置 → 允许开启激励/测量 → 在照明保持期间完成全部内层电学点。
- 内层持续记录实际温磁及质量标记，执行原安全/漂移检查，不重新判稳温磁。
  光功率在正式读数前后依次测量；漂移超限拒绝并退出，不在正式采集中调功。
- 每个光学点结束，电学/SMU 零关闭核实后才关光并进入下一点。
  外层回访、外层重复、恢复运行都重新执行外层判稳和新暗态扫描。
- `previous` 仅复用同一次功率扫描的上一完整合格亮点控制坐标，记录来源；
  不跨 dark、环境/光学点或进程恢复使用，不复用稳定性证明。每个亮点重新判稳。
  沿用原电流/功率/ND 上限、步长和低信号保护；它不保证所有目标序列都更快。

在相同 bias/SMU 坐标上计算 `delta_voltage_v = light_mean - dark_mean` 或
`delta_current_a = light_mean - dark_mean`，方法名为 `single_dark_baseline_v1`。
结果含 `dark_condition_id`、暗/亮均值、采集时刻、`baseline_age_s` 和
`baseline_drift_corrected=false`。不插值、不补测第二暗态。暗态无效或配对不符时
整次尝试拒绝；恢复重新运行完整计划，不把旧暗态拼入新尝试。

2182A 测电压；6221 的电流设定/状态不伪装成电流测量。6221 有偏置时的
电压差是该偏置下的光致电压变化。电流测量仍要求电流检测通道；底栅接线的
2400 只代表栅支路偏置/漏电，不能自动改成源漏光电流通道。

JSON/SQLite 保留全部 dark、light、失败样本及清理证据。默认分析/CSV 导出亮态
差分行；Python `load_results(..., include_dark=True)` 可同时读取暗参考行。
只有整次完成且清理通过才接受任何行。旧配置的快照形状保持兼容已有恢复哈希。

速度优化与真实耗时证据见 [耗时分析](PHOTORESPONSE_TIMING.md)。

## 已知实验状态（2026-10-13）

- 最新验收范围：P0/P1 接线隔离及参考配置复用已确认；P2/P3 按无样品控制流程验收，
  P6/P7/P9 与 SMU 联合扫描按复用源码和离线测试验收。SuperK 不再追加实测。
- 当前完整本地回归 **648/648**；LK_setup `lyr` 隔离验证 **307/307**。
  `f609d2c` 已同步到目标 NKT 分支，九份原配置/说明经 SHA-256 核对未修改。
- P2 配置、触发、新鲜读数及清理流程通过，过载保留为拒绝数据；P3 五个关闭输出状态下
  的电流设定/读回通过，开启输出的空载保护停止与最终零/关闭已验证。
  这不表示五点实际供流通过。部署后只读快照确认零/关闭、未 armed，未重触发 READ。
- PM100D 身份/探头匹配参考配置，波长校正及平均设置已验证；暗噪声含正负值，
  严格亮态测量规则拒绝负读数，尚未取得有光稳定反馈验收。
- CLD 本轮检查发现保存限流被设为 140 mA，已在 LD 关闭时恢复批准的 30 mA；
  未改 TEC 目标/开启状态。面板无 ERROR 与 CONN/MEAS bit13 冲突仍未解释，
  保留自动出光阻塞。以下早期测试条目是历史证据，不代替本轮状态。
- 2182A 输入由用户确认悬空；保留通信/配置通过及过载拒绝结果，暂不重复有效电压验收。
- 2400 实际连接底栅极（SiO2/Si）与 source，属于 `gate_bottom` 偏置/漏电角色。
  先前 0 V / 10 nA 尝试仍被拒绝，已确认回零、关闭、HIMP；该路径不能作为沟道光电流探测器。
- 6221 已批准的空载五点测试在 0 A 开启输出时达到 0.1 V compliance，尚未施加第一个
  4 pA 点。五点扫描未完成；已确认 0 A、输出关闭。转发 2182A 的 EOI 通信修复已验收。
- CLD 默认可用 `backend="visa"`，无需 TL4000 DLL；真实 USB 查询已验收。
  LD 上限已降至 30 mA。后续手动操作后的设定为 30 mA、LD 关闭；用户明确允许
  型号暂不确定，元数据须如实记录。
- 用户已确认光束收集装置。TEC-only 测试采用缓存目标/限流，配置 PROT 保护并短暂开启 TEC，
  首次状态检查发现连接保护而停止；已确认 LD 零/关闭及 TEC 关闭，未开启 LD。
  这是保留的历史尝试；其“已确认物理连接故障”结论由后续证据撤回。
- 用户随后报告手动出光正常，将目标改为 26 °C。只读生产驱动验证了 5 秒稳定温控，
  TEC 保持开启，实际温度约 26.000 °C、TEC 电流约 0.158 A、温度保护为 0，LD 关闭。
  未改动当前设置或输出。CONN=1 与 MEAS bit13 仍存在且无 SCPI 错误，诊断含义待核实；
  不反转为连接正常，不再冠以不适用 CLD 的通用 cable 故障名称，仍阻止自动开光。
- 温度保护窗口是全宽，实际允许区间为目标 ± 窗口/2；软件稳定容差不能超过窗口/2。
  温度保护 trip 允许 LD 关闭的 TEC 预热，但准备/开启 LD 或正式发光验证必须清除。
- CLD 的电压 MAX 查询是测量范围，不能作为二极管允许电压。恒流调光无需另设
  LD 电压；额外的软件电压监测上限 `max_voltage_v` 为可选项，省略该行即可不用该阈值，
  归档配置记录为 null。填写正数时仍执行越界拒绝；未填写时仍记录电压并拒绝非法、
  非有限、负数和过载读数，保留电流限幅、TEC、互锁、连接诊断和通信保护。
- 当前完整离线回归 519/519、光响应 104/104 通过；目标机 `lyr` 中 104/104 通过。
  目标机内存故障经用户授权停止一个遗留只读 PowerShell 后恢复；未停止其他实验进程。
  地址、完整身份、缓存、原始通信和内存诊断均保存在被 Git 忽略的本地审计中。
- 本次功率／组合集成的完整本地回归为 **568/568**，其中光响应 **129/129**、
  PM100D **24/24**。实际 CLI 仿真与 CSV 核对通过：NKT 8 条件、CLD 6 条件。
  LK_setup 的 `lyr` 另通过 **153/153** 光响应／PM 测试，代码已同步到目标 NKT 目录。
  本次没有连接功率计或开启任何真实输出。
- 原 `02_cld_2182_no_excitation.local.toml` 被设为 1000 ms / NPLC=1，低于
  当前 1080 ms 最低通信预算，原文件保留。另准备
  `02_cld_2182_no_excitation_timeout_fixed.local.toml`，采用 2000 ms 并省略无用电流边界，
  已通过纯配置校验；操作者可比较后选用。其余原本地配置和数据保持原样。

## 离线运行

```powershell
$env:PYTHONPATH = 'src'
python -m attodry_control.photoresponse_cli describe
python -m attodry_control.photoresponse_cli validate-config --config config/photoresponse_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_cld_simulation.toml
```

成功输出 `SIMULATION completed: <JSON路径>`，退出码 0；采集失败为 1，配置/文件错误为 2。
没有后台进程。`simulate` 拒绝真实 backend，不会偷偷替换硬件配置。
示例限值只是合成数据参数，不能作为样品或激光二极管的安全参数。
控制电脑须使用 `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`，不能用裸系统 Python。
本地 Anaconda 若存在用户目录 NumPy/Matplotlib 冲突，可设置 `PYTHONNOUSERSITE=1` 并用 `python -s`。

## 测量模式

| mode | detector | 偏置字段 | 结果与电路含义 |
|---|---|---|---|
| `open_circuit_voltage` | `keithley2182a` / `keithley2400` | 两个偏置数组都必须省略 | 光电压；外部激励路径须物理隔离 |
| `dc_current_voltage` | `keithley2182a` / `keithley2400` | `bias_current_points_a` | 6221 恒流下的光致电压变化 |
| `voltage_bias_current` | `keithley2400` | `bias_voltage_points_v` | source–drain 沟道电压偏置下的光电流；0 V 点用于短路光电流 |
| `smu_current_voltage` | `keithley2182a` | 偏置数组省略，使用 `three_smu_run.smu_bias` | 独立 SMU 恒流激励下测电压 |
| `smu_voltage_voltage` | `keithley2182a` | 偏置数组省略，使用 `three_smu_run.smu_bias` | 独立 SMU 电压激励下测电压 |

2400 的电流探测接线须为沟道 source–drain 路径；底栅–source 电流是栅漏电流。
当前站点 2400 属于底栅角色，不能勾选该光电流探测配置的接线确认。

`none` 不创建 6221 电流源控制器、不发送其输出设置。若 2182A 经 6221 转发，
仍需连接并查询 6221 的身份、关闭状态和 Delta/Pulse Delta/DCON 是否处于 armed 状态。
`none` 不等于 DC 0 A。6221 的电流设置不是实际样品电流，2182A 电压也不能当作电流。
`open_circuit_voltage` 且探测器为 2182A 时可省略 `max_abs_current_a`，归档值为 null。
6221 DC 激励和所有 2400 模式仍需要明确的样品电流边界；旧配置填写的值继续有效。

2400 作电压表时采用厂商所述的 0 A 电流源模式、最低源电流量程、固定电压测量量程，
且测量期间必须开启其 OUTPUT。这与 2182A 的被动电压输入不同，需单独确认负载影响。
2400 电压 compliance 须高于预期电压；电流模式则配置电流 compliance。两种模式均记录
V/I 与读数状态、禁用电阻测量/处理功能，结束后回零、关闭并确认 HIMP 关闭模式。

旧配置的默认扫描顺序：环境点（可选）→ 光学点 → 偏置点 → repeat → dark_before/light/dark_after。
每个窗口采集 `samples_per_window` 个新读数。`settle_s` 是每次切光后的等待；
`sample_interval_s` 是读数前的等待，不代表保证的采样周期。串口转发与状态检查有额外开销。
硬件样本时间取主机发送 READ? 到得到响应的区间，不包含周围的配置核对时间；不是仪器内部时间戳。

## PM100D 稳功率与 combination

闭环配置同时包含 `[pm100d]` 和 `[power_feedback]`。NKT 的每个光学点与
`target_powers_w` 做笛卡尔组合；CLD 保持二极管标称波长，只组合目标功率和电学条件。
光学点中的百分比／电流是反馈起点，目标 W 是独立坐标，不表示预先知道功率与电流的关系。

`[photoresponse_combination].order` 按外层到内层列出扫描轴，例如：

```toml
[photoresponse_combination]
order = ["optical", "target_power_w", "bias", "repeat"]
```

允许的轴为 `optical`、`target_power_w`、`bias`、`environment`、`smu` 和 `repeat`。
所有多点轴都必须列出；单点轴可省略，但固定模块仍参与叶节点的读回和保护。
光学行内的波长／带宽／初始调节量成组，环境行内的温度／Bx／Bz 成组；
模块由光源、测量模式、探测器和可选 PM／环境表选择。未选硬件不连接。
这是一条复用 combination 语义的光响应入口；旧输运／Three-SMU 独立模块仍保留。

| 配置项 | 含义 |
|---|---|
| `pm100d.measurement_plane` | 探头所在测量面；只有实际置于样品面且标定后才能解释为样品入射功率 |
| `pm100d.resource / expected_serial / expected_sensor_serial` | 当前功率计和探头身份，真实设置前必须填写 |
| `pm100d.max_power_w` | 当前光路与探头批准的上限，不能复制仿真数值当安全限值 |
| `power_feedback.target_powers_w` | 每个光学点需要稳定到的目标 W 数组 |
| `target_tolerance_w` 或 `target_tolerance_fraction` | 二选一的目标容差 |
| `power_feedback.window` | 稳定持续时间、最少样本数、采样间隔和峰峰值上限 |
| `actuator="source_current"` | NKT 源电流百分比反馈；设置百分比范围、单步和最小可信信号 |
| `actuator="source_power"` | NKT 恒功率模式（`[nkt_source] control="power"`）下调功率设定百分比（寄存器 0x37）；设置 `source_power_min_pct / source_power_max_pct / source_power_step_pct` 和最小可信信号 |
| `actuator="varia_nd"` | NKT ND 反馈；需已确认调光方向、范围和单步，真实路径须另行验收 |
| `actuator="cld_current"` | CLD 电流反馈；`current_min_a / current_max_a / current_step_a` 受 LD 上限约束 |

`source_power` 每步按 `当前% × 目标W / 实测W` 估算下一设定，再受单步上限和
最小／最大值约束；估算小于 0.1 % 时仍按方向走 0.1 %。反向时与 `source_current` 一样把单步减半。
恒功率模式下设定与输出近似成比例，远离目标时比固定步长少走几步；比例只用来选步长，
是否合格仍完全由 PM100D 稳定窗口判定。示例见
`config/photoresponse_power_nkt_power_mode_simulation.toml`。

`[nkt_source] apply_control = true`（可选，默认 false）表示预检阶段把设备切到 `control`
指定的恒流／恒功率模式：仅在发射已确认关闭、联锁正常、无故障位时写 Setup 寄存器 0x31 并读回，
事件记录 `control_mode_request` / `control_mode_applied`。外部调制等其他 Setup 模式不会被覆盖。
不写该项时保持原规则：设备模式必须已经与 `control` 一致，否则拒绝运行。
恒流和恒功率的百分比是不同标尺，切换模式时要重新确认 `max_level_pct` 和各点初始设定。

准备波长和 PM 校正时保持关光。亮态功率达到目标并通过稳定窗口与 hold 后，
才开始正式电学窗口；每次有界调节都先关光，再重建稳定证据。
正式亮窗中功率越出目标容差或峰峰值上限时，拒绝整次暗—亮—暗尝试；
不在已有正式窗口中边调边拼接数据。功率计无可信信号、饱和或通信不确定时停止。
读数按顺序采集并记录各自时间，不宣称功率与电压硬件同步或连续监测。
暗窗只核对 PM 的设置／状态和波长，记录 `measurement_performed=false`、`power_w=null`；
暗噪声不参与调节，也不触发自动加流寻光。正式亮窗使用新鲜 `READ?`。

闭环例子：

```powershell
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_power_nkt_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_power_cld_simulation.toml
```

对应的 `photoresponse_power_nkt_hardware.example.toml` 和
`photoresponse_power_cld_hardware.example.toml` 用于填写实机参数。
旧配置不含 PM／反馈表时仍按开环运行，实测光功率为 null。
每个闭环条件保存目标与实际 W、测量面、PM 原始读数、调节历史和稳定证据；
CSV 输出功率及组合坐标。源设定百分比、电流与实际 W 始终分列，
不会自动推算样品的 A/W 或 V/W。

## 配置模板

LK_setup 的 `NKT/config/photoresponse_trial/` 已新增六份独立本机配置，原文件保留：

| 前缀 | 组合 | 条件数 |
|---|---|---|
| 05 | NKT + PM + 2182A，无激励 | 6 |
| 06 | CLD + PM + 2182A，无激励 | 6 |
| 07 | NKT + PM + 底栅 + 2182A | 66 |
| 08 | CLD + PM + 6221 五点激励 + 2182A | 30 |
| 09 | NKT + PM + 底栅 + 温磁场未来模板 | 66 |
| 10 | CLD + PM + 2400 通道光电流未来模板 | 6 |

同目录的 `validate_reused_profiles.py` 只校验配置，不连接仪器；
`README_组合复用配置.md` 说明可修改字段。光路/PM/栅极参数来自参考有效配置。
目标功率的 Cartesian 关系映射为组合轴；每条件从光学行指定的初始电流开始，
不沿用参考配置的 `previous` 热启动。参考磁场 readback tolerance 映射为
`field_tolerance_t`，原 setpoint ack 字段不在本项目配置接口中。
09 的温度/磁场点和步长是可修改计划示例，不是现场读回。10 仍保留未确认接线。

Three-SMU 联合配置增加 `[photoresponse_smu]`，设 `enabled=true`，复用原
`[three_smu_run]` 与活动角色的 `[smu_bias] / [gate_top] / [gate_bottom]` 表。
`order` 中加入 `smu`；其点由原 Three-SMU generator 生成。`fixed` 角色每个叶节点
仍读取，`off` 角色不打开。栅极电流导出为 leakage，不能当作样品光电流。
SMU 替代激励使用新增两种 mode，并且省略 photoresponse 的偏置数组。
`open_circuit_voltage` 只允许栅极角色，活动 `smu_bias` 必须有明确激励 mode。
真实联合运行另外需要 `--authorize-smu`、活动角色的精确身份和接线确认；
本轮按操作者要求只做 SMU／温磁场离线验收。

```powershell
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_smu_nkt_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_smu_cld_simulation.toml
```

新版 NKT 配置可省略 `nd_pct` 和未选择的 pulse picker：程序保持实际值，
不写这些寄存器，并在每次正式核对中检测外部改动。

日常 `run` 默认读取被忽略的
`config/photoresponse_trial/11_nkt_630nm_dark_50-300uW_7pt_2182.local.toml`；
其他命令的默认本地文件仍是 `config/hardware.local.toml`。默认扫描文件不存在时直接报错，
不会回退到其他配置。`--config` 可覆盖当前命令的路径。合并需要的表，保留已有站点配置。
也可把临时配置放在被忽略的 `run_data/` 下。所有相对输出/数据库路径相对该 TOML 文件解析。

| 模板 | 用途 |
|---|---|
| `config/photoresponse_hardware.example.toml` | NKT + 6221 + 串口转发 2182A |
| `config/photoresponse_cld_hardware.example.toml` | CLD1010LP + 2400 光电流 |
| `config/photoresponse_6221_test.example.toml` | 用户授权的独立空载 6221 五点测试；不连接探测器/光源 |
| `config/photoresponse_environment_simulation.toml` | 可附加到仿真配置的温磁场扫描表 |

真实模板故意含 `CHANGE_ME`，不能直接运行。未知身份、限值、接线、DLL、步长或不匹配的模式
在连接前拒绝。切换电压表/电流表时必须重新检查单位、量程和 compliance。
`photoresponse.light_source` 选择 `nkt` 或 `cld1010lp`，不同时连接两个光源。

2182A 直接连接时用 `detector_transport="visa"` 并填写 `[photoresponse_2182].address`；
经 6221 时用 `via_6221`，省略该 address，只填 `[photoresponse_6221].address`。
无激励而使用转发时，6221 表只保留 `address` 与 `expected_idn`，不应含输出参数。
2400 只支持直接 VISA，使用 `[photoresponse_2400]`。未选中的仪器不会被打开。

`[photoresponse_2182].channel` 选择电压输入通道 `1` 或 `2`，旧配置省略时仍用 `1`。
按 [Keithley 官方手册](https://download.tek.com/manual/2182A-900-01C_July_2022_User.pdf)，
通道 1 可选 0.01、0.1、1、10、100 V，通道 2 仅可选 **0.1、1、10 V**。
量程固定、自动量程关闭；通道 2 的量程、滤波和相对值用显式 `CHAN2` 命令设置及读回，
`SENS:VOLT:DC:RANG` 省略通道的命令实际属于通道 1，不能当作当前选中通道的量程。
积分时间 NPLC 是两个电压通道共用的设置。通道/量程/处理设置发生外部改变仍拒绝采集。

```toml
[photoresponse_2182]
# 保留本机原有 expected_idn；直接 VISA 时还需 address。
channel = 2
voltage_range_v = 1.0  # 当前选中 1 V 档；以下是注释备选，只启用一个值。
# voltage_range_v = 0.1  # 100 mV 档：同时将 photoresponse.max_abs_voltage_v 收紧到 <= 0.1。
# voltage_range_v = 10.0 # 10 V 档：样品 max_abs_voltage_v 仍保持已批准边界，不自动放宽。
nplc = 1.0
```

软件样品边界 `max_abs_voltage_v` 与量程分开，必须不大于选中量程；更大量程不代表
允许更高样品电压。过载原值（例如 `+9.9E37`）保留在失败记录，不能作为暗态基线。

NKT 复用现有 EXTREME/VARIA SDK 模块；LLTF 的真实 SDK 仍不可用。`nkt_run.emit=true`，
`dwell_s=0`，由光响应窗口控制明暗和等待。NKT 百分比不代表样品瓦特。
CLD 默认使用 `backend="visa"` 和已有 NI-VISA；也可选 `tl4000` 并提供 x64 DLL。
VISA 单次通信最多等待 10 秒；两条路径均不 reset。先检查互锁、LD auto-on、TEC 故障、模式与保护，
关闭激光配置电流，TEC 稳定后才允许发光。CLD 的 760 nm 是二极管标称值，不能扫描波长。
USB 固件查询已经实测，出光/温控仍待验收；型号可按用户明确指示记录为未确定，
不能虚构规格。电流限幅、TEC 电流/温度、保护窗口、接线和光路须满足当前阶段要求。
`max_voltage_v` 仅为可选软件监测阈值，不向恒流控制器写入 LD 电压设定；未配置时不猜默认值。
ENAB 是输出允许模式，PROT 是越界关断模式；真实控制要求 PROT。故障阻止输出。
结束独立尝试关闭 LD、回零并恢复 TEC 原状态；任何失败都会保留未知状态和清理错误。

## 实机命令（只有对应阶段已获授权且总线空闲时运行）

```powershell
# 只查电学身份与现有设置；不 READ、不 reset、不消费错误队列，不打开光源。
python -m attodry_control.photoresponse_cli diagnose --config config/hardware.local.toml --authorize-connect

# 仅 6221 空载测试。使用独立五点测试表；不会打开 2182A、2400 或光源。
python -m attodry_control.photoresponse_cli test-6221 --config config/hardware.local.toml --authorize-connect --authorize-writes

# 在 NKT 项目目录和 lyr 环境中，执行 run 即确认按默认 TOML 启动所选仪器和光路。
python -m attodry_control.photoresponse_cli run

# 需要其他测量配置时：
python -m attodry_control.photoresponse_cli run --config config/hardware.local.toml
```

`run` 不再要求 `--authorize-connect`、`--authorize-writes`、`--confirm-route`、
`--authorize-environment` 或 `--authorize-smu`，也不新增 TOML 授权开关。旧参数可继续传入。
执行 `run` 包含配置中选中的源/激光设置、触发/新鲜 READ、必要的错误队列读取，以及所选
温磁场/SMU 控制；未选模块不连接。硬件边界、接线与隔离确认、状态检查和 cleanup 不变。
`diagnose` 和 `test-6221` 保持原有权限语义。Agent 的部署/离线测试授权不包含实际启动扫描。

启动前打印实际配置路径和选中模块，并在 JSON、JSONL、SQLite 记录 `invocation`：实际路径、
配置 SHA-256、`authorization_source="run_command"` 和选中模块。解析期间文件变化会拒绝。
在项目根目录执行 `python -m` 会加载本 checkout 的 `src`，不依赖其他目录的 editable 安装。
前台扫描用 Ctrl+C 请求正常停止和清理；进程退出不等于硬件已关闭，须核对清理读回。

VISA 使用显式地址和资源独占锁，但这不能防止其他程序使用同一控制器的其他地址。
实际测试前仍须协调共享 GPIB 总线。通信超时后不盲目重试可能滞留的响应；本会话只尝试安全关闭，
不把关闭命令当作关闭确认。6221 空载进入 compliance 会停止，不将限幅点算作正常测量。

## 可选温度/磁场扫描

使用现有 `[cryostat]`、`[magnet]`、`[temperature_stability]`，另加
`[photoresponse_environment]` 的明确温度/Bx/Bz 点、最大温度步长、最大超调及磁场步长。
执行 `run` 已包含所选温磁场控制，无需额外参数。不配置该表就不创建 attoDRY 驱动。

变温/变场前关闭光与电学源；温控沿用先启用反馈、后写温度目标的验收顺序。
等温磁场稳定后再准备电学采集。保留 `sqrt(Bx²+Bz²) <= 3 T` 的路径与读回检查。
样本保存实际 `measurement_temperature_k` 和实际 Bx/Bz，不能用目标温度替代实测温度。
正常结束按 hold/zero 配置，失败先尝试关光及电学清理，再请求磁场归零。通信失败时保留
最后确认的非零磁场，`state_current=false`，要求人工核实；不能推断零场。

## 数据、恢复和分析

JSONL 逐事件 flush；配置 `[photoresponse_storage].database_path` 后另存 SQLite WAL/FULL 审计。
采用独立 `photo_runs/photo_events/photo_conditions` 表，不改历史 SR830 数据表。
最终 JSON 保存原始窗口、命令/响应日志位置、配置、环境、错误、清理及软件版本。
每次 run 全部条件与清理通过后才接受；失败/中断的原始尝试永久保留，默认分析排除。
源码不同或计划不同的重试应作为新系列，不能强行与旧计划拼接。

失败后显式指定 `--resume-run <原run_id>` 创建同一系列的新 attempt，重新 preflight 并重测
整次计划；不拼接不完整的暗亮窗口，不自动重新开启输出。计划 hash 必须一致，成功系列禁止重复接受。
若上次进程崩溃，查询数据库中的 `photo_runs` 获得 run_id；旧 running 尝试会标记 interrupted。
输出目录/数据库进程锁阻止重复任务同时运行；不同输出目录不能代替实验台资源协调。

暗背景按三个窗口实际采样中点进行线性插值：

```text
w = (t_light - t_dark_before) / (t_dark_after - t_dark_before)
dark_at_light = dark_before + (dark_after - dark_before) * w
delta = light - dark_at_light
```

保留暗漂移、权重和公式版本；不声称消除非线性漂移。缺少功率计时没有 A/W、V/W，
光源百分比/LD 电流不是测得的样品光功率。仿真标记明确，未配置环境时环境为 null。

```powershell
python -m attodry_control.photoresponse_cli analyze --record 'run_data/实际文件_completed.json'
python -m attodry_control.photoresponse_cli export --record 'run_data/实际文件_completed.json' --output 'analysis_output/新结果.csv'
```

[分析 Notebook](../notebooks/Photoresponse_Analysis.ipynb) 只读原始 JSON，不导入仪器驱动。
导出 CSV 与选择 manifest，保存输入 SHA-256，不覆盖既有文件。需要诊断失败尝试时可显式调用
`load_results(path, include_rejected=True)`，这些结果仍标记 accepted=false。

## 离线验证

```powershell
python -s -m unittest discover -s tests -p 'test_photoresponse*.py' -q
python -s -m unittest discover -s tests -q
```

测试只使用 fake/simulated resources；通过离线测试不表示实机接线、固件、测量噪声、
光功率或样品允许值已经验收。当前验证总数和目标机验证见 [开发阶段](DEVELOPMENT_STAGES.md)。
