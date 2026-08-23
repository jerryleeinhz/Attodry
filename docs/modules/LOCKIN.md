# Lock-in module work package

## 当前状态

双 SR830 独立器件验收、1/2/3 次谐波验收、17.777 Hz--100 kHz 有界谐波
扫频和 4--400 mVrms 激励扫描已经完成。当前真实台架最后确认的基线是：

- `lockin_xx`：内部参考，SINE OUT 驱动器件，测量 Vxx；
- `lockin_xy`：来自 XX TTL SYNC OUT 的外部 TTL 上升沿参考，测量 Vxy，
  SINE OUT 物理断开；
- 两台输入均为 A-B、Float；
- 17.777 Hz、4 mVrms、谐波 1；
- XX 为 10 mV full-scale（`SENS=20`）、XY 为 1 mV full-scale
  （`SENS=17`）、300 ms、24 dB/oct；
- 100 kohm 外部串联电阻，无额外 50 ohm 终端；计算电流时包括 SR830
  约 50 ohm 输出电阻和约 500 ohm 器件电阻。

扫频和扫幅现在也支持严格的命名区间表：线性区间使用 `min`/`max` 加 `step` 或
`points`（二选一），对数区间使用 `min`/`max`/`points`；区间不能重叠或共享端点。区间可分别用
`xx_full_scale_v`、`xy_full_scale_v` 覆盖固定量程，省略时沿用角色配置，且
`bounded_auto` 角色不允许区间覆盖。展开点、区间索引、量程切换和读回都写入每次 sweep
的审计 JSON；旧的点数组仍作为过渡格式接受。

这些是上次真实读回，不是对当前面板状态的持续保证。受版本控制的
`hardware.example.toml` 模板已按操作者最新文件改为 XX 1 V、XY 10 mV、100/150 Ω
电阻参数和分段扫幅网格；这不改变上面记录的历史真实读回。受版本控制的硬件和模拟
TOML 模板现已声明输入、屏蔽、耦合、时间常数、滤波、灵敏度和 TTL 边沿；本地
硬件配置仍需操作者手工同步，模板期望值也不能代替真实读回。

## 模块目标

1. 把两台 SR830 的期望设置用物理量和语义角色写入严格 TOML 契约。
2. 在所有采集路径保留 X、Y、R、原始相位、频率、谐波和时间信息。
3. 查询并记录 SR830 的 `PHAS?` 相移设置，禁止隐式 Auto Phase。
4. 提供固定量程和显式启用的受限自动量程；日常 sweep 仅能按已解析的本地 TOML
   策略写入/恢复 `SENS`，独立 commissioning 命令仍要求各自的显式授权。
5. 把转换期状态和正式采样状态分开，保留全部原始记录并严格验收正式窗口。
6. 保持独立 Lock-in 测试不导入 cryostat、magnet 或 gate 控制路径。

## 非目标

- 本模块不控制温度、磁场、SMU 或门电压。
- 不把 TOML 中的期望值当成真实读回或写入授权。
- 不自动校正相位，不从接近噪声底的相位推断器件物理机制。
- 不删除 rejected、transition 或 cleanup 数据。
- 不把两台顺序读取描述成同步读取。

## 推荐配置契约

TOML 使用物理量，SR830 代码只存在于驱动映射层。建议字段包括：

```toml
[lockin_xx]
reference_source = "internal"
input_mode = "a_minus_b"
shield_grounding = "float"
input_coupling = "ac"
time_constant_s = 0.3
filter_slope_db_oct = 24
# Current station example: fixed XX at 1 V. The project also permits the
# narrower 10--50 mV bounded-auto ladder; see LOCKIN_DAILY_OPERATION.md.
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 1.0
reserve_mode = "normal"
# To opt in to the three-level XX bounded_auto ladder, change the mode and use:
# sensitivity_full_scale_v = 0.010
# autorange_min_full_scale_v = 0.010
# autorange_max_full_scale_v = 0.050
# autorange_target_occupancy = 0.85
# autorange_stable_samples = 2
# autorange_max_steps = 2
settle_time_constants = 5.0

[lockin_xy]
reference_source = "external_ttl"
external_reference_edge = "rising"
input_mode = "a_minus_b"
shield_grounding = "float"
input_coupling = "ac"
time_constant_s = 0.3
filter_slope_db_oct = 24
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.010
reserve_mode = "normal"
# To opt in to XY bounded_auto, change the mode and use this complete policy:
# autorange_min_full_scale_v = 0.001
# autorange_max_full_scale_v = 0.010
# autorange_target_occupancy = 0.85
# autorange_stable_samples = 2
# autorange_max_steps = 1
settle_time_constants = 5.0
```

2026-08-22 日常默认改为两台都固定：XX 20 mV、XY 1 mV。只有操作者在本机
`hardware.local.toml` 把某一角色明确切换为 `bounded_auto` 时，才执行该角色的
自动判断。推荐自动阶梯为 XX 10 mV--20 mV--50 mV、XY 1 mV--10 mV；两者使用 0.85
目标占用率、缩窄前两个连续安全样本。XX 每条连续扫描最多两次、且每次仅在相邻的项目
确认档位间转换；XY 最多一次。10 mV 对已记录最大
Vxx 5.384 mV 的占比约 53.8%，但这不保证新的温度、磁场、门压或频率条件不会超过
8.5 mV 阈值。

## 参数与配置含义（当前日常策略）

所有电压均为 SR830 full-scale 或 RMS 的 SI 单位（V），不是仪器前面板代码。
当前项目白名单为 1 mV、10 mV、20 mV、50 mV，分别对应 `SENS` 代码 17、20、21、22。
本地 `hardware.local.toml` 只能由操作者维护且必须忽略提交；模板默认的 XX/XY GPIB
地址仅适用于本站，其他控制机必须在本机覆盖后才可连接。

日常运行的完整字段值域、`fixed`/`bounded_auto` 示例、时间常数等待关系和旧终端导入
排错入口统一见 [`../LOCKIN_DAILY_OPERATION.md`](../LOCKIN_DAILY_OPERATION.md)，不要在
本页和日常手册维护两份独立的取值表。

安全边界现在拆成两个职责：被忽略的 `hardware.local.toml` 保存本站地址、接线事实和
本次扫描网格；同目录受版本控制的 `lockin_safety.toml` 保存项目允许的 full-scale
白名单、autorange 阶梯、0.85 占用率、2 个稳定样本、最小 5 tau 和 4 mVrms cleanup。
两条日常 sweep 自动读取后者，操作者不需要先运行 `validate-config`；该命令只是可选
的无 VISA 离线检查。每条 JSON 的 `measurement_config` 保存解析后的策略和 SHA-256，
便于按历史记录复现安全边界。驱动层仍映射完整 SR830 电压量程（包含 1 V/SENS 26），
但只有安全文件列出的档位能用于日常运行。

| 字段 | 含义与确认值 | 约束 |
| --- | --- | --- |
| `reference_source` | XX 为 `internal`，即 XX 提供激励与参考；XY 为 `external_ttl`。 | 角色不可交换；XY 的 SINE OUT 必须物理断开。 |
| `external_reference_edge` | XY 的 TTL 外参考边沿，固定为 `rising`。 | 仅 XY 必填；这不是 A-B 电压信号的极性。 |
| `input_mode` | `a_minus_b`，差分 A-B 测量。 | 两台都固定，避免把共模信号当作输运信号。 |
| `shield_grounding` | `float`，输入屏蔽浮地。 | 两台都固定。 |
| `input_coupling` | `ac`，交流耦合。 | 两台都固定。 |
| `time_constant_s` | 数字滤波时间常数，固定 0.3 s。 | 本模块不自动选择时间常数。 |
| `filter_slope_db_oct` | 低通滤波斜率，固定 24 dB/oct。 | 与时间常数共同定义带宽，不能在正式采样中变化。 |
| `settle_time_constants` | 每次设置转换后等待的时间常数个数，最小 5.0。 | `[lockin_sweep].settle_s` 必须不小于两台的 `time_constant_s × settle_time_constants` 最大值；当前最短为 `5 × 0.3 s = 1.5 s`。 |
| `sensitivity_mode` | XX 与 XY 都默认 `fixed`；各自可显式选择 `bounded_auto`。 | 自动模式绝不因新版本默认开启；XY SINE OUT 的物理断开与模式无关。 |
| `sensitivity_full_scale_v` | 固定模式的目标量程；自动模式的起始且最窄量程。日常默认 XX 20 mV、XY 1 mV。 | 自动模式中必须等于 `autorange_min_full_scale_v`。实际量程以 `SENS?` 读回为准。 |
| `reserve_mode` | `high_reserve`/`normal`/`low_noise`，对应 `RMOD` 0/1/2；当前日常策略为 `normal`。 | 还必须出现在该角色 `lockin_safety.toml` 的 `allowed_reserve_modes` 中；改变时先降 SINE OUT，读回确认并在 cleanup 恢复。 |
| `autorange_min_full_scale_v` / `autorange_max_full_scale_v` | 选中角色的自动范围边界。推荐 XX 10--50 mV 三档阶梯、XY 1--10 mV。 | 仅 `bounded_auto` 使用；XX 10--50 mV 自动按 10→20→50 mV 逐档转换。 |
| `autorange_target_occupancy` | 0.85。 | 到达或超过阈值、或报告过载时，才允许上移一个档位；最大档仍不安全则 fail closed。 |
| `autorange_stable_samples` | 2 个连续安全样本。 | 符合更窄的相邻档位两次后，才允许下移一档；任一不合格样本重置计数。 |
| `autorange_max_steps` | XX 三档为 2；XY 两档为 1。 | 每条连续扫描中允许的总转换次数，必须等于该项目确认阶梯的相邻转换数。 |

`bounded_auto` 是可审计的预备阶段状态机，不是 SR830 的 `AGAN` 命令替代品。每次
允许的 `SENS` 转换都必须读回新代码、保存转换记录、至少等待 1.5 s、消费并记录
转换锁存、再次等待，并在正式样本前冻结量程。固定模式仍会在预检不匹配时采用相同
的读回和审计要求。

## Lock-in 实验经验和必须落实的规则

### 接线与参考

- XX SINE OUT 必须实际接入激励路径；只看到 LOCK 状态不能证明器件获得激励。
- XY 使用 XX 的 TTL SYNC OUT 作为外参考；XY SINE OUT 必须保持物理断开。
- TTL 是 XY 的参考信号，A-B 上的 Vxx/Vxy 仍是模拟被测信号。
- 不应为了“确保设置”反复重写已正确的 `FMOD/RSLP`。真实谐波首轮测试曾因
  重写已正确的 XY 外参考模式产生瞬态 unlock。
- 语义角色始终使用 `lockin_xx` 和 `lockin_xy`，不能用易交换的 #1/#2 代替。

### 极性和相位

- Vxx A/B 反接使 X/Y 约反号，R 保持在约 1% 内，相位改变约 179.78 度。
  因此必须记录接线极性，不能用 Auto Phase 隐藏接线变化。
- 正式数据必须保留 X、Y、R 和 `phase_deg`。还应保存 `PHAS?` 相移设置，且
  代码不得隐式发送 `APHS`。
- 相位平均使用圆周统计，不能把 +179 度和 -179 度普通平均成 0 度。
- 当 R 接近噪声底时仍保留相位原始值，但应标记低信噪比；此时相位不适合直接
  做物理解释。
- 主 commissioning notebook 的显示阈值为 `PHASE_MINIMUM_AMPLITUDE_V=1e-6`
  Vrms 和 `PHASE_MAXIMUM_STANDARD_DEVIATION_DEG=5`；二者只筛选绘图相位，
  不会删除原始样本。通过的连续段在 ±180 度处展开，断开的低信噪比点不会被
  跨越连接。将前者改为 `0.0`、后者改为 `None` 可审计全部原始相位。
- 单台 `SNAP? 1,2,3,4,9` 内部相干，但 XX/XY 是顺序读取。记录中必须保留
  顺序读取事实和可获得的每台时间戳。

### 时间常数和采样

- 最后验收时间常数为 300 ms，滤波为 24 dB/oct。设置或连接改变后至少等待
  5 个时间常数，即当前至少 1.5 s。
- 对每个实际 SINE OUT (`SLVL`) 幅值改变，激励扫幅使用两个上述 interval：当前
  至少 3.0 s，才读回输出并采集 h1；任一日常 sweep 的 `settle_s` 小于 1.5 s 都会在
  打开 VISA 前失败。JSON 的 `source_step_settle_s` 记录该实际等待时间，不修改任何
  相位设置。
- 以 0.3 s 间隔采集的样本仍相关，不能把它们当作完全独立样本计算误差。
- 第一版不要自动选择时间常数；保持固定噪声带宽，避免额外转换锁存和不可比数据。

### 灵敏度和自动量程

- 两台最后基线为 1 mV full-scale。XX 在约 50 Hz、R 约 1.09 mV 时曾产生
  真实 output overload，扫频因此临时使用 20 mV 量程。
- 自动量程必须是可审计的受限状态机：**output overload** 或达到 full-scale 占用率
  阈值时才立即放宽；input/reserve、filter overload 均保守拒绝。只有连续稳定低占比
  样本才缩窄；达到用户配置边界或最大步数即停止。
- 每次改变 `SENS` 后记录转换期状态、等待至少 5 tau、消费明确允许的锁存，
  再次等待后才进入正式采样。
- 自动量程只在每个实验条件的预备阶段运行，正式样本期间冻结量程。
- XY 零场信号接近量化/噪声底，不能无下界地自动缩窄量程或追逐噪声。
- 直接调用 SR830 `AGAN` 不能绕过项目边界、状态记录、等待和日常 TOML/commissioning
  写入范围约束。

### 状态锁存与扫频

- `LIAS?` 和 `ERRS?` 会清除锁存位。日常 sweep 只能在已解析 TOML 所声明的预检、
  转换和正式窗口中读取它们，并在清除前保存原始值、时间和所处阶段；独立
  commissioning 命令继续要求显式授权。
- 改频率时曾观察到 XY 的 unlock、frequency-range-change 和 overload 转换锁存。
  转换期记录可以单独消费，但正式采样窗口出现任何 unlock/overload/error 仍失败。
- XY 外参考读回在保持锁定时观察到显示量化偏差。扫频时不再用数值相等或固定 ppm
  门限拒绝请求值、XX `FREQ?` 和 XY `FREQ?` 的差异；三者全部记录。仍然拒绝非有限、
  低于 1 mHz 或高于 102 kHz 的读回，并保留 unlock、overload、instrument-error 和
  不安全转换的 fail-closed 判据。每点以 XX 的 `FREQ?` 作为记录和分析用的
  `actual_frequency_hz`，但谐波 102 kHz 上限按请求值和两台实际读回中较高者判定。
- 两个 sweep 在第一次查询前自动清理两台 VISA 接口，异常或 Ctrl+C 时在 cleanup 前
  再次尽力清理；状态写入 `interface_clear` 审计字段。`recover-interface` 可在硬中断后
  手动清理，且不改变 SR830 设置。诊断和实时监控仍不自动清理锁存。
- 恢复 XX 窄量程曾产生预期的 `LIAS=4` 转换锁存。必须记录、等待并在最终读回
  严格确认为零，不能直接宣布 cleanup 成功。

### 电流与安全

- 当前 4 mVrms、100 kohm 串联、50 ohm 输出和约 1 kohm 器件对应约
  39.58 nArms；400 mVrms 对应约 3.958 uArms。
- 每次激励扫描从忽略的 `hardware.local.toml` `[lockin_sweep]` 读取完整路径阻抗、
  器件最大 RMS 电流和最大 RMS 电压；日常命令不再带这些参数。任何一项缺失、格式错误
  或超出上限都在打开 VISA 前失败，并把已解析值归档到 JSON。
- 器件电压保护使用已确认的 `maximum_device_resistance_ohm`，而非直接把 SINE OUT
  电压当作器件端电压：`Vsine × Rdevice,max / (Rseries + 50 Ω + Rdevice,max)`。
  `approximate_device_resistance_ohm` 只用于名义电流和分析；它不能替代高阻状态的
  电阻上界。上界不明确时必须保持保守值，不能为了通过预检而填入平均电阻。
- SR830 的软件最小输出不是电气断开。异常 cleanup 后仍需人工确认实际接线和
  前面板读回。
- 只读扫频/扫幅分析的电流不是新的独立测量值，而是 `SINE OUT Vrms / 完整串联路径
  电阻`。日常扫描路径的可变电阻只在忽略提交的 `hardware.local.toml` 的
  `[lockin_sweep]` 表中设置：`external_series_resistance_ohm` 和
  `approximate_device_resistance_ohm`；固定 SR830 输出阻抗为 50 Ω。每次 sweep 把三项
  和总阻抗归档进 JSON 的 `measurement_config.excitation_path`。Notebook 默认使用该
  历史快照，不会复制或重读当前本机 TOML；只有没有该快照的旧 JSON 才需显式的
  `EXCITATION_PATH_OVERRIDE`。该覆盖只改变只读绘图标尺，不写仪器，也不能替代下一次
  激励扫描所需的安全确认。

## 阶段和验收条件

## 实施计划（2026-08-21 记录）

本轮推进不接仪器的 L0--L3 配置、相位和受限量程切片，计划和权限边界如下：

1. 以最后一次真实验收值作为严格配置默认值：A-B、Float、AC、300 ms、
   24 dB/oct、1 mV full-scale、至少 5 个时间常数；XY 额外要求 TTL 上升沿。
2. 新增独立的物理量到 SR830 代码纯映射，配置层只接受本项目当前确认过的值；
   映射本身不打开 VISA，也不发送命令。
3. XY 只接受固定 1 mV；XX 受限量程只接受用户确认的 10--20 mV、0.85、
   两个稳定样本和单步调整，不扩展到其他范围或策略。
4. 用严格配置和纯映射单元测试覆盖缺失、未知、不支持值、角色/TTL 边沿错误，
   然后运行完整离线测试。
5. L2 只用 fake VISA 验证诊断、固定设置、精确读回、等待和失败恢复；真实只读、
   清锁存和任何设置写入仍分别等待新授权。
6. L3 将纯决策与 I/O 分离；量程变化需要独立的写授权和锁存消费授权，变化后
   两次等待至少 5 tau，保留 transition/verification 样本后才冻结正式量程。

该计划不把 TOML 期望值当作当前仪器读回，也不改变已经验收的真实台架状态。

2026-08-22 更新：以上 L0--L3 段落保留为当时的历史验收范围。当前**日常** sweep
契约已允许 XX 与 XY 分别 opt-in `bounded_auto`，默认仍是两台 fixed；旧的
`commission-xx-autorange-narrow` commissioning helper 则继续严格只服务 XX，不能
用它对 XY 进行量程写入。

### L0 - contract（当前：complete）

- 确认上述配置字段、TTL 上升沿、固定 300 ms 和固定 1 mV 默认值。
- 用户分别确认 XX/XY 自动量程允许边界；未确认前自动量程默认关闭。
- 完成条件：配置命名和权限边界无歧义，不接仪器。

2026-08-21：固定设置字段、TTL 上升沿和权限边界已落实；随后用户确认 XY 固定
1 mV 和 XX 10--20 mV 的完整受限量程参数，L0 契约已闭合。

### L1 - strict configuration（当前：fixed offline complete）

- 在 `config.py` 中增加严格字段和 SR830 支持值验证。
- 新增纯映射模块，例如 `sr830_settings.py`，把物理量精确映射到 SCPI 代码。
- 更新 hardware/simulation 示例，不修改或提交 `hardware.local.toml`。
- 测试未知、缺失、不支持数值、错误角色、错误边沿和非法自动量程边界。
- 完成条件：相关单元测试和完整离线测试通过；零硬件 I/O。

2026-08-21：两个示例 TOML 已声明固定设置；`sr830_settings.py` 将当前确认的
物理量映射到 SR830 代码，严格配置和映射测试通过。完整离线套件为 155 项通过、
2 项因未安装 matplotlib 跳过；未打开 VISA。

### L2 - phase and fixed settings（当前：offline complete）

- 为时间常数、灵敏度、滤波和 `PHAS?` 增加小型读回接口。
- 所有写路径先诊断、写后精确读回；未授权时 fake VISA 证明零写命令。
- 保留 X/Y/R/phase/frequency/harmonic 和顺序读取元数据。
- 测试驱动从不发送 `APHS`，相位经模型、JSON、SQLite 和分析往返不丢失。
- 完成条件：故障注入、部分记录和 cleanup 测试通过。

2026-08-21：新增 `SENS?`、`OFLT?`、`OFSL?`、`PHAS?` 查询接口和显式授权的
双角色固定设置流程。流程在首个写命令前完成两台完整诊断，写后等待至少 5 tau
（当前 1.5 s）并精确读回；写入或验证失败时，两台先降至软件最小输出，再尽力
恢复原固定设置，且不掩盖主错误。驱动只查询 `PHAS?`，从不发送 `APHS` 或写入
`PHAS`；相移设置和每台顺序采样的 UTC 时间戳现经模型、JSON、SQLite schema v3、
分析加载和 CSV 往返保留。未授权和等待不足测试均证明零 VISA 查询/写入；本阶段
只使用 fake VISA，没有打开真实资源、读取/清除状态锁存或发送硬件命令。

### L3 - bounded auto range（当前：offline complete）

- 新增纯决策模块，例如 `lockin_autorange.py`；策略与 VISA I/O 分离。
- 测试扩大、缩小、迟滞、边界、最大步数、过载、低信号和无法收敛。
- 集成后每次变化都读回、等待、记录转换锁存并冻结正式量程。
- 完成条件：所有状态转换确定、可重放，失败时不产生 accepted 样本。

2026-08-21：新增纯 `lockin_autorange.py` 状态机。XX 在 10 mV 上达到 0.85
占比或过载时立即放宽到 20 mV；在 20 mV 上只有连续两个样本都不超过 8.5 mV
才缩回 10 mV。每个预备阶段最多改变一次，达到 20 mV 后仍超阈值则失败关闭；
XY 固定 1 mV，不进入状态机。fake-VISA 执行层在写前验证原量程，写后精确读回，
执行两次至少 1.5 s 等待，保留 transition 和严格 verification 样本并冻结正式
量程；异常时将 XX 激励降到软件最小值并尽力恢复原量程。未提供写授权或锁存
消费授权时零 I/O。本阶段未打开 VISA，也未发送真实 `SENS` 或读取真实锁存。

### L4 - target offline validation（当前：target offline complete）

- 将提交同步到 `LK_setup`，仅用 `lyr` 执行全部离线测试。
- 检查 wheel 不包含本地配置、DLL 或实验数据。
- 完成条件：记录提交号、Python 路径、测试数量和结果；不打开 VISA。

2026-08-21：经用户授权，完整 Git bundle 被传至 `LK_setup` 的专用 Documents
目录并克隆；该副本位于 `C:\Users\LK_Setup\Documents\attodry_control_lockin_l4`，
提交为 `2199460`。使用
`C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe`（Python 3.12.13）并显式
设置该副本的 `PYTHONPATH=src` 后，完整离线测试 166 项全部通过，源码编译通过。
目标机有一份旧项目会污染默认导入路径，因此 L4 命令必须保留该 `PYTHONPATH`
设置。用 `--no-deps --no-build-isolation` 构建的 wheel 为 79,704 bytes，SHA-256
`c5ffe7d7daf3c59796a46f4263916162092164aeee902840a9fdde1a843c479c`；内容检查未
发现 `hardware.local.toml`、DLL、`run_data`、SQLite 或 secrets。未打开 VISA。

### L5 - real read-only commissioning（当前：完成，锁存状态未读取）

- 需要新的明确授权；若读取 `LIAS?/ERRS?`，授权必须单独写明会清锁存。
- 对比 TOML 期望值和实际 `FMOD/RSLP/ISRC/IGND/ICPL/SENS/OFLT/OFSL/PHAS`。
- 完成条件：原始记录保存在 ignored 路径，零设置写命令。

本轮 L5 的授权仅限不清锁存的诊断：查询 `*IDN?`、`FMOD?`、`RSLP?`、`FREQ?`、
`HARM?`、`SLVL?`、`ISRC?`、`IGND?`、`ICPL?`、`ILIN?`、`SENS?`、`RMOD?`、
`OFLT?`、`OFSL?`、`PHAS?` 和 `SNAP? 1,2,3,4,9`。其中 `PHAS?` 是参考相移设置，
`SNAP?` 的第 4 项是本次测量相位；两者不能混为一谈。该诊断明确不发送 `APHS`、
不发送任何设置命令，也不查询会清除状态锁存的 `LIAS?`/`ERRS?`。因此记录会标注
“安全状态不完整”，不能用来宣布无 unlock、overload 或仪器错误。

2026-08-21：在 `LK_setup` 的 L4 专用 clone 中，从旧的被忽略站点配置复制并补齐
本节已确认字段；旧配置未修改，新配置严格解析通过。随后完成一次双 SR830 的
真实只读诊断，两个身份不同，语义角色、TTL 上升沿、A-B、Float、AC、300 ms、
24 dB/oct 和 XY 的 1 mV（`SENS=17`）均与 TOML 一致。XX 实际仍为 1 mV
（`SENS=17`），与新策略起始 10 mV（`SENS=20`）不一致；该差异已保留在原始记录
中，未尝试修正。原始 JSON 和空的标准错误文件仅保存在该 clone 的忽略
`run_data` 路径。没有发出写命令、`APHS`、`LIAS?` 或 `ERRS?`，故锁存状态仍未知。
将 XX 改为 10 mV 属于 L6，仍需要单独写入授权和 XY SINE OUT 物理断开确认。

### L6 - real write commissioning（当前：10 mV 起始量程、自动缩窄和设备扫频/扫幅已实机验收）

- 每一种写命令、量程边界、激励、接线和恢复目标必须重新明确授权。
- 先固定设置小范围验收，再单独验收受限自动量程；失败后恢复已确认基线。
- 完成条件：正式窗口状态全清、设置恢复读回通过、失败数据仍保留。

2026-08-21：新增 `set-xx-sensitivity --config ...` 的最小 L6 命令，并仅在
TOML 的 XX 起始量程为 10 mV 时允许目标 `SENS 20`。它要求
`--authorize-writes`、`--authorize-status-latch-consumption` 和
`--confirm-xy-sine-disconnected` 三项独立旗标，缺少任一项即在打开 VISA 前失败。
成功路径只写 `lockin_xx` 的 `SENS 20`，随后各等待至少 1.5 s，并在转换期和正式
窗口分别读取完整双机诊断及 `LIAS?`/`ERRS?`。预检、转换和正式窗口均要求角色、
频率、4 mVrms 输出、输入/滤波/相位读回和状态字通过；不重写 XY，也不发送
`APHS`。写后失败时，命令记录失败原始数据、尝试 XX 4 mVrms 最小输出和恢复先前
量程，再作严格的最终读回。fake-VISA 覆盖了三重授权、锁存/输出预检拒绝、XX-only
写入和两次验证。

2026-08-21：在 `LK_setup` 的独立 L6 clone（提交 `77e7d7e`）上，严格配置解析、
170 项离线测试和源代码编译均通过后，操作者确认 XY SINE OUT 保持物理断开，并明确
授权锁存读取和该受限写入。首次实机预检读到 XY overload 锁存，命令按 fail-closed
策略拒绝，未发送 `SENS` 或任何 cleanup 写入；原始拒绝记录保留。随后十个每秒一次的
只读、锁存消费恢复样本均锁定、无 overload、无仪器错误，且两台仍为 `SENS=17`。
同一受限授权下的一次重试成功：仅 `lockin_xx` 从 `SENS=17` 写为 `SENS=20`；预检、
转换期和正式窗口的两台状态字均清零，XY 始终为 `SENS=17`，两台 `PHAS?` 设置均未
改变，标准错误为空。所有原始 JSON/JSONL 和标准错误文件只保留在目标 clone 的忽略
`run_data` 目录；没有写 XY、没有发送 `APHS`。这只完成 XX 固定 10 mV 起始量程的
实机验收；受限 `bounded_auto` 的真实转换（以及任何 `SENS 21`）仍须新的独立授权。

为不改变 4 mVrms 激励而验证低占比缩窄分支，`commission-xx-autorange-narrow` 只接受
严格 TOML 和与上述写入、锁存消费、XY 断开相同的三重授权。它要求 XX 已在 10 mV，
暂时写入 20 mV，完成一段完整状态窗口后采集两个间隔至少 1.5 s 的真实安全样本；
只有状态机依次给出 `KEEP`、`NARROW` 才写回 10 mV。两个转换均有完整双机窗口，缩窄
转换期只可记录 XX 的 output-overload 锁存，正式窗口仍要求完全清零。任何失败均将 XX
激励设为 4 mVrms、恢复 10 mV 并记录 rejected 审计数据；不写 XY、不发送 `APHS`。
该命令不伪造阈值/过载，因此自动放宽分支仍只能在未来真实达到阈值时验收。

2026-08-21：在新的 `LK_setup` 独立 L6 自动量程 clone（提交 `d1e6201`）中，严格
配置解析、174 项离线测试和源代码编译均通过后，执行已授权的缩窄验收。XX 从已验收的
10 mV（`SENS=20`）暂时置于 20 mV（`SENS=21`）；两个间隔 1.5 s 的实际双机安全样本
分别得到 `KEEP`、`NARROW`，随后仅 XX 返回 `SENS=20`。预检、最大量程转换、两个 fit
样本、缩窄转换和正式窗口的 XY 均为 `SENS=17`；所有状态/错误字均为零（缩窄转换也
没有出现可允许的 XX output-overload），两台 `PHAS?` 设置不变，标准错误为空。原始
JSON 和标准错误文件只保存在该 clone 的忽略 `run_data` 目录；没有写 XY 或发送
`APHS`。未人为增加激励或制造 overload，故“在 10 mV 上达到 8.5 mV 阈值/过载后自动
放宽到 20 mV”的分支仍是唯一尚未实机触发的行为，未来触发该分支须重新授权。

2026-08-21：在相同隔离 clone 中重做 17.777 Hz–100 kHz 的十点对数频率扫描，每点
采集三个正式样本。原始数学网格曾在 316.159 Hz 和 5622.802 Hz 分别收到 SR830 的
316.1 Hz 和 5622 Hz 读回；这些 rejected 记录和 cleanup 均完整保留。当前离线规则将
请求频率与 XX `FREQ?` 的正常显示量化分开处理，并同时保留请求网格和实际读回，避免
为了适配显示精度而手动改写扫描点。第三次十点扫描取得 30 个正式样本、无样本问题，
最终 cleanup 再次验证 17.777 Hz、4 mVrms、XX 10 mV、XY 1 mV 和零状态字。频率扫描
期间 XX 临时为 20 mV；XY 没有写入。

2026-08-21：操作者为 4–400 mVrms 幅值扫描确认 100 kΩ 外部串联电阻、约 500 Ω
器件电阻、5 mArms 器件电流上限、0.5 Vrms 器件电压上限，以及无外部 50 Ω 端接。
扫描在固定 17.777 Hz 下使用 11 个递增点、每点三个正式样本；打开 VISA 前的保守界限
为 3.998 µArms（仅计 100 kΩ 串联和 SR830 内部 50 Ω）与 0.4 Vrms，均低于确认上限。
实机运行取得 33 个无问题正式样本，并完成可验证 cleanup：XX 回到 4 mVrms 与
`SENS=20`，XY 保持 `SENS=17`，两机最终状态和错误字均为零。期间只临时写 XX
`SENS=21` 和 XX SINE OUT；没有写 XY 或发送 `APHS`。原始审计 JSON 和 stderr 仅保留
在隔离 target clone 的忽略 `run_data` 中。

2026-08-21：扩展只读 commissioning 分析。主 notebook 现在在开头提供可点击的
`Browse…` 按键、`Only completed records` checkbox、record/sample 筛选和完整激励路径
电阻 controls；默认 completed/clean。选择文件后 catalog 自动切到该文件目录，重新运行
其下的 catalog/plot cells 即可找到配对扫描。它为频率
扫描和电流--电压扫描分别生成 XX/XY × h1/h2/h3 的六张双 y 轴图：左轴为 SR830 R
电压幅值，右轴为相位。频率标题给出由 SINE OUT 算得的 RMS 电流；电流--电压横轴
使用同一计算。若扫频/扫幅记录没有某个谐波，只明确标记缺失而不推断。计算优先
使用保存的 SINE OUT 读回，旧扫频记录没有读回时才采用记录的 4 mV 设定值；不打开
VISA、不读取状态锁存、不写设置。

2026-08-21：为重做的 1/2/3 阶扫频和扫幅增加离线验证的显式
`--all-harmonics` 选项。默认 sweep 仍只测 h1；只有提供该旗标时，才会在每个
扫描点依次对两台仪器写 h2、h3，分别等待完整 `settle_s`，采样并在下一个点前
恢复 h1。首次真实三阶扫频在 121.122062 Hz 的 h2 第一个正式样本停下：XX
`LIAS=18`（filter overload + frequency range changed），XY `LIAS=16`
（frequency range changed），没有 unlock 或 instrument error。部分样本已保留；
cleanup 严格验证 h1、XX 4 mVrms / 10 mV / 17.777 Hz、XY 1 mV 和零状态字，幅值
扫描未启动。修订后每个 HARM 转换会把仅有的 filter-overload / frequency-range
changed 锁存作为 discarded transition 记录、消费并再次等待；若第一条只有
input/reserve overload，则再等待一个 interval 做一次复核，复核必须清零；unlock、
重复 input/reserve、output overload、time-constant change、error 仍失败，之后的正式
样本对全部状态位保持零容忍。fake-VISA 覆盖成功、二阶正式失败恢复和这组观察到的转换锁存；
修订后的真实三阶扫频仍需新的明确授权。

2026-08-21：随后的真实三阶扫频在 38.3104813 kHz 的 h3 切换被安全拒绝；h3 所需
检测频率为 114.931 kHz，超过 SR830 102 kHz 上限，仪器保持 h2，78 个此前正式配对
样本和拒绝尝试均已保留。最终读回确认两机回到 h1、17.777 Hz、XX 4 mVrms/10 mV、
XY 1 mV 且状态/错误字为零；但 cleanup 过程记录一次 XY transient unlock，因此该
原始记录不是 completed。新的 fail-fast 预检会在打开 VISA 前验证每个
`harmonic × frequency <= 102000 Hz`，避免再次对超限点写入 HARM。h3 最高 34 kHz，
h2 最高 51 kHz；100 kHz 端点只能测 h1。高频段的记录策略需操作者确认。

操作者随后选择覆盖优先策略：保留原 17.777 Hz--100 kHz 十个点，h1 测十点、h2 测
前九点（至 38.310 kHz）、h3 测前八点（至 14.677 kHz）。为避免隐式缺失，新的
`--skip-unsupported-harmonics` 只能与 `--all-harmonics` 同用；每个超限阶数都会在
本点的 `skipped_harmonics` 中记录阶数、所需检测频率、102 kHz 上限与原因。
不提供这个旗标时，严格 all-harmonics 仍在任何 VISA I/O 前拒绝整张超限网格。

2026-08-21：在完成目标机离线验证后，操作者授权按上述有界覆盖策略重新执行真实
扫频。10 个频点全部完成，每个被支持的阶次采集 3 个正式 xx/xy 配对样本，共 81 个；
h1 为 10 个条件、h2 为 9 个、h3 为 8 个。超出 102 kHz 检测参考上限的阶次只以
`skipped_harmonics` 审计元数据保留，未向仪器写入超限 HARM。所有正式窗口没有问题，
cleanup 严格验证 h1、17.777 Hz、XX 4 mVrms/10 mV、XY 1 mV，以及两台零状态/错误字。

2026-08-21：随后授权的固定 17.777 Hz 幅值扫描使用 4、6、10、16、26、40、64、100、
160、252、400 mVrms 的 11 个点，逐点采集 h1/h2/h3 各 3 个 xx/xy 配对样本，共 99 个。
安全计算使用 100 kΩ 外部串联、SR830 约 50 Ω 输出、约 500 Ω 器件、5 mArms 和
0.5 Vrms 上限；400 mVrms 对应约 3.98 µArms，安全余量充足。每次实际 `SLVL` 改变后
均等待两个 1.5 s interval。正式窗口全清，cleanup 再次严格验证相同的 XX/XY 基线；
原始 JSON、stderr 和 PNG 仅存在目标 clone 的忽略 `run_data`，绝不提交。

2026-08-21：这两条 completed 记录也解释了“LOCK 但相位不恒定”的现象。`LOCK` 只说明
SR830 的参考 PLL 已同步，不能证明被测 R 足够大或相位可用于物理解释。固定频率扫幅的
XX h1 在 11/11 点达到默认相位质量门槛（R >= 1 µVrms 且三次样本的圆周标准差 <= 5 度），
而 XY h1 仅在最高两个幅值点达到；XY h2/h3 没有通过点。4 mVrms 扫频的 XX h1 在十点
均通过且相位随频率平滑变化、跨过 ±180 度包裹；这不是随机失锁。低于阈值的相位保留
在原始 JSON 中，但默认绘图会留空，不能据此判断器件相位。高频相干 XY 或高幅值 h2/h3
在宣称物理机制前仍需用已知电阻/短路、屏蔽与接地、以及串扰控制实验区分真实响应、
电缆/输入电容传递函数和激励源谐波失真。

2026-08-22：将重复的 sweep 参数集中到严格的 `[lockin_sweep]` TOML 表。当前站点
配置固定保存 17.777 Hz--100 kHz 的十个对数等距频点、4--400 mVrms 的十一个幅值点、
h1/h2/h3、有界高频跳过、1.5 s settle、每点三个样本、0.3 s 间隔，以及 100 kΩ
外部串联、50 Ω SR830 输出、约 500 Ω 器件、5 mArms/0.5 Vrms 上限和无外部 50 Ω
端接。范围策略不再由该表覆盖，而是由 `[lockin_xx]` 与 `[lockin_xy]` 各自的
`sensitivity_mode` 和范围字段唯一决定。sweep 现在必须使用严格 hardware TOML；两个
逐次接线 confirm 不再是运行参数。日常只需 `sweep-frequency` 或
`sweep-excitation`；严格预检失败时不会开始扫描，但 TOML 不能替代对 XY SINE OUT 实际
断开的物理检查。每次已打开双机的尝试都会原子保存到 `output_directory`（默认仓库根
`run_data/commissioning`），带 `completed`/`rejected`/`interrupted` 状态。JSON 的无
VISA 地址 `measurement_config` 是已解析 TOML 请求；实际读回保留在 preflight、point 和
cleanup。扫频点也记录由配置的 4 mVrms 与完整串联路径算出的名义电流。日常操作顺序和
所有字段说明见
[`../LOCKIN_DAILY_OPERATION.md`](../LOCKIN_DAILY_OPERATION.md)。

2026-08-22：日常 sweep 的双角色量程契约改为 opt-in：默认 XX 固定 20 mV、XY 固定
1 mV；操作者可独立把任一角色改为 `bounded_auto`。自动策略的候选范围、占用率阈值、
稳定样本数和最大调整步数必须完整地写入同一角色表，且不会因扫描命令或另一个角色启用
auto 而隐式打开。每个固定确认或自动决策的写入、读回、状态转换和 cleanup 恢复都应保留
在 JSON 审计记录。任何 XY 量程转换的失锁、输入/滤波过载或未许可过载都会拒绝该次扫描；
只有已记录的 XY 缩窄转换可消费 `LIAS=4`，且后续验证必须完全干净。`[lockin_sweep]` 中
的必填 `run_name` 与 `note` 仍分别作为安全 JSON 文件名标签和审计备注；两项都应在每次
日常运行前填写。

2026-08-22：新增 `monitor-live` 作为独立的只读双 SR830 状态面板。它显示 XX/XY 的
X/Y/R、测量相位、设定与 SNAP 频率、谐波、当前 SENS 代码、SINE OUT 和可选的锁定/过载/
错误状态；不会发送 `SENS`、`HARM`、`FREQ`、`SLVL` 或 cleanup 写入。真实的
`LIAS?`/`ERRS?` 状态需要显式 `--consume-status-latches`，因其会消费锁存位；因此不得
同 sweep 或其他访问相同 VISA 地址的程序并行使用。完整日常命令与字段解释见
[`../LOCKIN_LIVE_MONITOR.md`](../LOCKIN_LIVE_MONITOR.md) 和
[`../LOCKIN_DAILY_OPERATION.md`](../LOCKIN_DAILY_OPERATION.md)。

2026-08-23：日常 sweep 的正式曲线可以按扫描类型、角色和谐波独立选择：
`frequency_xx_harmonics`、`frequency_xy_harmonics`、
`excitation_xx_harmonics`、`excitation_xy_harmonics`。每项只接受有序 h1/h2/h3
组合或 `[]`；每类扫描至少选择一个角色。选择只决定哪一台的数据进入正式曲线，并不
减少双机 HARM 设置、读回或安全检查：任一伴随读数的 unlock、overload、error 或频率
不匹配仍使扫描 fail closed。JSON 样本以 `selected_roles` 记录正式归属；分析加载器
保留旧配对记录的兼容性，并只为实际选择的 XX/XY × 阶数生成图。该变更仅经离线
fake-VISA/加载器测试，未连接或写入真实仪器。

2026-08-23：扫频固定激励幅值从 Lock-in 基线字段中分离，新增严格的
`[lockin_sweep].frequency_source_voltage_v_rms`（0.004--5.0 Vrms）。扫频开始前
只设置并读回一次 XX `SLVL`，所有频率点记录同一实际 SINE OUT 和由完整串联路径
计算的名义电流；该幅值在打开 VISA 前按确认的器件电流/电压上限做 fail-closed 预检。
扫频清理仍将 XX SINE OUT 恢复到固定 4 mVrms 安全基线。`source_voltage_v` 继续表示
两台 Lock-in 的最小基线，而不再是扫频运行幅值；配置字段、测量记录和日常说明已同步，
仅 fake-VISA/离线测试覆盖，未连接真实仪器。

2026-08-23：SINE OUT 请求值与 `SLVL?` 读回值现分开归档。扫频和扫幅均不再因两者
不匹配而拒绝，例如请求 83.2 mVrms、读回 82 mVrms 会继续扫描。`source_v_rms` 保留
请求，`source_readback_v_rms` 是电流和分析的依据；每点同时保留请求与读回计算的名义
电流。读回值仍须通过 SR830 输出范围及完整串联路径的器件电流/电压安全复核，故这不是
取消安全上限。该改动仅通过 fake-VISA 离线验证，未连接或写入真实仪器。

## 预计文件所有权

- 配置：`config.py`、`config/hardware.example.toml`、`config/simulation.toml`。
- 驱动：`sr830.py`、可选新增 `sr830_settings.py`。
- 策略：可选新增 `lockin_autorange.py`。
- 独立 CLI：`lockin_test.py`。
- 数据：`models.py`、`records.py`、`storage.py`，只做相位/设置元数据所需修改。
- 测试：`test_config.py`、`test_sr830.py`、`test_storage.py`、分析测试及新增策略测试。

不要仅为文件名整齐而移动已经稳定的 `DualSr830Controller`；只有明确减少交叉依赖
时才重构。

## 新 Chat 启动提示

```text
请负责 Lock-in 模块。先按 AGENTS.md 顺序完整阅读四份必读文档，再完整阅读
docs/modules/README.md 和 docs/modules/LOCKIN.md。先检查 git status 和当前提交，
只继续 LOCKIN.md 中尚未完成的最早阶段。每个代码改动同时写 fake-VISA/单元测试，
不得连接真实仪器；任何真实连接、清锁存或设置写入都等我单独授权。在 LK_setup
运行时只能使用 lyr。结束时按模块交付格式报告提交号、测试和硬件权限边界。
```
