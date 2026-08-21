# Lock-in module work package

## 当前状态

双 SR830 独立器件验收、1/2/3 次谐波验收、17.777 Hz 到 1 kHz 扫频和
4--400 mVrms 激励扫描已经完成。当前真实台架最后确认的基线是：

- `lockin_xx`：内部参考，SINE OUT 驱动器件，测量 Vxx；
- `lockin_xy`：来自 XX TTL SYNC OUT 的外部 TTL 上升沿参考，测量 Vxy，
  SINE OUT 物理断开；
- 两台输入均为 A-B、Float；
- 17.777 Hz、4 mVrms、谐波 1；
- 两台均为 1 mV full-scale、300 ms、24 dB/oct；
- 100 kohm 外部串联电阻，无额外 50 ohm 终端；计算电流时包括 SR830
  约 50 ohm 输出电阻和约 1 kohm 器件电阻。

这些是上次真实读回，不是对当前面板状态的持续保证。受版本控制的硬件和模拟
TOML 模板现已声明输入、屏蔽、耦合、时间常数、滤波、灵敏度和 TTL 边沿；本地
硬件配置仍需操作者手工同步，模板期望值也不能代替真实读回。

## 模块目标

1. 把两台 SR830 的期望设置用物理量和语义角色写入严格 TOML 契约。
2. 在所有采集路径保留 X、Y、R、原始相位、频率、谐波和时间信息。
3. 查询并记录 SR830 的 `PHAS?` 相移设置，禁止隐式 Auto Phase。
4. 提供固定量程和显式启用的受限自动量程；任何实际 `SENS` 写入继续需要授权。
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
sensitivity_mode = "bounded_auto"
sensitivity_full_scale_v = 0.01
autorange_min_full_scale_v = 0.01
autorange_max_full_scale_v = 0.02
autorange_target_occupancy = 0.85
autorange_stable_samples = 2
autorange_max_steps = 1
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
sensitivity_full_scale_v = 0.001
settle_time_constants = 5.0
```

2026-08-21 用户确认：XY 固定 1 mV；XX 以 10 mV 为启动和最窄量程，20 mV
为最宽量程，目标占比 0.85，缩窄前需要两个连续安全样本，每个实验条件的预备
阶段最多调整一次。10 mV 对已记录最大 Vxx 5.384 mV 的占比约 53.8%，但这不
保证新的温度、磁场、门压或频率条件不会超过 8.5 mV 阈值。

## 参数与配置含义（2026-08-21 确认）

所有电压均为 SR830 full-scale 或 RMS 的 SI 单位（V），不是仪器前面板代码。
代码映射只保存在驱动层：1 mV、10 mV、20 mV 分别对应 `SENS` 代码 17、20、21。
本地 `hardware.local.toml` 只能由操作者维护且必须忽略提交；模板中的地址占位符
不能用于连接仪器。

| 字段 | 含义与确认值 | 约束 |
| --- | --- | --- |
| `reference_source` | XX 为 `internal`，即 XX 提供激励与参考；XY 为 `external_ttl`。 | 角色不可交换；XY 的 SINE OUT 必须物理断开。 |
| `external_reference_edge` | XY 的 TTL 外参考边沿，固定为 `rising`。 | 仅 XY 必填；这不是 A-B 电压信号的极性。 |
| `input_mode` | `a_minus_b`，差分 A-B 测量。 | 两台都固定，避免把共模信号当作输运信号。 |
| `shield_grounding` | `float`，输入屏蔽浮地。 | 两台都固定。 |
| `input_coupling` | `ac`，交流耦合。 | 两台都固定。 |
| `time_constant_s` | 数字滤波时间常数，固定 0.3 s。 | 本模块不自动选择时间常数。 |
| `filter_slope_db_oct` | 低通滤波斜率，固定 24 dB/oct。 | 与时间常数共同定义带宽，不能在正式采样中变化。 |
| `settle_time_constants` | 每次连接或设置转换后等待的时间常数个数，固定 5.0。 | 当前最短等待为 `5 × 0.3 s = 1.5 s`；这不是缩短等待的授权。 |
| `sensitivity_mode` | XX 为 `bounded_auto`；XY 为 `fixed`。 | XY 不自动量程，以免零场噪声底触发追逐；L5 不会执行任何量程写入。 |
| `sensitivity_full_scale_v` | 固定模式的量程；XX 自动模式的起始且最窄量程。XX 10 mV，XY 1 mV。 | XX 自动模式中必须等于 `autorange_min_full_scale_v`。它是期望策略，实际量程以 `SENS?` 读回为准。 |
| `autorange_min_full_scale_v` / `autorange_max_full_scale_v` | XX 可用范围为 10 mV / 20 mV。 | 仅 XX 的 `bounded_auto` 使用；不可扩大边界。 |
| `autorange_target_occupancy` | 0.85，即 XX 10 mV 时的阈值是 8.5 mV。 | 到达或超过阈值、或报告过载时，才允许向 20 mV 放宽；20 mV 仍超阈值则 fail closed。 |
| `autorange_stable_samples` | 在 20 mV 时，连续 2 个不超过 8.5 mV 的样本才可缩窄到 10 mV。 | 任一不合格样本重置计数；这形成迟滞而不是逐点抖动。 |
| `autorange_max_steps` | 每个实验条件的预备阶段最多调整 1 次。 | 正式采样前冻结量程；扫描中的大幅变化不会由自动量程追随。 |

`bounded_auto` 是可审计的预备阶段状态机，不是 SR830 的 `AGAN` 命令替代品。
每次许可的 `SENS` 转换都必须单独获写入授权、读回新代码、保存转换记录、至少等
待 1.5 s、在已单独授权的前提下消费转换锁存、再次等待，并在正式样本前冻结量程。

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
  至少 3.0 s，才读回输出并采集 h1；`--settle-s` 小于 1.5 s 会在打开 VISA 前
  失败。JSON 的 `source_step_settle_s` 记录该实际等待时间，不修改任何相位设置。
- 以 0.3 s 间隔采集的样本仍相关，不能把它们当作完全独立样本计算误差。
- 第一版不要自动选择时间常数；保持固定噪声带宽，避免额外转换锁存和不可比数据。

### 灵敏度和自动量程

- 两台最后基线为 1 mV full-scale。XX 在约 50 Hz、R 约 1.09 mV 时曾产生
  真实 output overload，扫频因此临时使用 20 mV 量程。
- 自动量程必须是可审计的受限状态机：过载或接近 full-scale 时立即放宽；只有
  连续稳定低占比样本才缩窄；达到用户配置边界或最大步数即停止。
- 每次改变 `SENS` 后记录转换期状态、等待至少 5 tau、消费明确允许的锁存，
  再次等待后才进入正式采样。
- 自动量程只在每个实验条件的预备阶段运行，正式样本期间冻结量程。
- XY 零场信号接近量化/噪声底，不能无下界地自动缩窄量程或追逐噪声。
- 直接调用 SR830 `AGAN` 不能绕过项目边界、状态记录、等待和写入授权。

### 状态锁存与扫频

- `LIAS?` 和 `ERRS?` 会清除锁存位。读取前必须明确授权，并在清除前保存原始值、
  时间和所处阶段。
- 改频率时曾观察到 XY 的 unlock、frequency-range-change 和 overload 转换锁存。
  转换期记录可以单独消费，但正式采样窗口出现任何 unlock/overload/error 仍失败。
- XY 外参考读回在保持锁定时观察到最高约 54 ppm 偏差。100 ppm 只适用于已验收
  扫频中的 XY 外参考读回，不得扩展到谐波测量或状态判据。
- 恢复 XX 窄量程曾产生预期的 `LIAS=4` 转换锁存。必须记录、等待并在最终读回
  严格确认为零，不能直接宣布 cleanup 成功。

### 电流与安全

- 当前 4 mVrms、100 kohm 串联、50 ohm 输出和约 1 kohm 器件对应约
  39.58 nArms；400 mVrms 对应约 3.958 uArms。
- 每次激励扫描都必须由用户提供完整路径阻抗、器件最大 RMS 电流和最大 RMS
  电压；任何一项缺失都在打开 VISA 前失败。
- SR830 的软件最小输出不是电气断开。异常 cleanup 后仍需人工确认实际接线和
  前面板读回。
- 只读扫频/扫幅分析的电流不是新的独立测量值，而是 `SINE OUT Vrms / 完整串联路径
  电阻`。在 `notebooks/sr830_commissioning_sweeps.ipynb` 开头的 controls cell 修改
  `EXTERNAL_SERIES_RESISTANCE_OHM`、`SR830_OUTPUT_RESISTANCE_OHM` 和
  `APPROXIMATE_DEVICE_RESISTANCE_OHM`；当前为 100000 Ω、50 Ω、500 Ω，总计 100550 Ω。
  该改动只改变绘图标尺，不写仪器，也不能替代下一次激励扫描所需的安全确认。

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
采集三个正式样本。原始数学网格先在 316.159 Hz 和 5622.802 Hz 分别因 SR830 读回的
316.1 Hz 和 5622 Hz 超出 100 ppm 扫频读回界限而 fail-closed；两次 rejected 记录都
完整保留，且 cleanup 均确认 17.777 Hz、4 mVrms、XX 10 mV、XY 1 mV 和零状态字。
没有放宽验收容差，而是采用两处已观测到的仪器可读回量化点 316.1 Hz、5622 Hz；它们
各相对原对数点偏离约 0.02%。第三次十点扫描取得 30 个正式样本、无样本问题，最终
cleanup 再次验证上述基线。频率扫描期间 XX 临时为 20 mV；XY 没有写入。

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
changed 锁存作为 discarded transition 记录、消费并再次等待；unlock、input/reserve
或 output overload、time-constant change、error 仍立即失败，之后的正式样本对全部
状态位保持零容忍。fake-VISA 覆盖成功、二阶正式失败恢复和这组观察到的转换锁存；
修订后的真实三阶扫频仍需新的明确授权。

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
