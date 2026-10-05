# 光学激发与非线性 Hall 联合扫描

最后更新：2026-10-05。

## 当前接线更正（2026-10-05，优先于下文历史检查点）

用户最新确认 **lockin_xx=SR830、lockin_xy=SR865A**。当前连接为：

```text
PEM REF OUT（1f，约 50 kHz）→ XY SR865A REF IN（external_ttl / rising）
XY SINE OUT+ → XX SR830 REF IN（external_sine / sine_zero_crossing）
XX SINE OUT → 样品（无外部 50 Ω 终端；确认样品/串联电阻总负载远大于 50 Ω）
```

新显式拓扑为 `reference_topology="pem_xy_xx_sine"`，不是旧的 `pem_xx_xy`。
两路 `sine_output_connected=true`：XX 用于样品激励，XY 只向 XX 提供正弦参考。
新增 `[photonics_lockin.reference_output]` 单独记录 XY 的接法、幅值及 DC，要求
`destination="lockin_xx_ref_in"`、`sample_connected=false`；程序只核验并保留
这路参考，不扫描或降低其 SLVL，不改写未使用的 BlazeX 模式。样品激励及清理
仍只由 XX 的 `[photonics_lockin.source]` 管理。依据用户最新的总负载确认，
`source.load="high_impedance"`；具体样品端分压不从 SLVL 推断，样品电光限值
仍保留占位。

本轮已授权 SSH 只读核验两台锁相、PEM、PM100D 及 NKT/VARIA；CONTROL 关闭后
NKT 查询成功，当次 emission 读回为 OFF。未写入仪器设置、消费 SR830 状态锁存、
发射激光或扫描。本次身份/设置读回不构成完整级联验收：分时单次频率读回中
PEM 与 XX 的一次差约 0.545 Hz，大于模板 0.5 Hz 容差，不自动放宽；XY 的 PHAS
当前读回来自 h1，也不能代表 h2 已标定。P5/P6 正式参考及有光验收仍待执行。

配置字段、允许选项、激励 points/step/log 与明确点的互斥写法、实际关光/重新稳定
时序见 [当前操作指南](../PHOTONICS_COMBINATION_SCAN_GUIDE.md) 和
[光电模板](../../config/photonics.example.toml)。下文保留此前型号/接线及阶段的
历史原文，不能拿其旧连接去配置当前实验。

### 此前型号确认（历史原文）

当前型号更新：用户确认 `lockin_xx` 已换为 **SR865A**，`lockin_xy` 仍是 **SR830**，
并报告现场已验证 SR865A 可锁住 PEM。这个结果属于用户现场确认，尚无本项目适配器的
自动读回/完整级联验收记录。把 XY 也换为 SR865A 是正在讨论的方案，尚不是已完成的
硬件变更。下文 2026-10-04 的失败与恢复状态专指当时的双 SR830；历史记录不改写。
型号适配、参数语义和实施顺序见第 9 节。用户于 2026-10-05 同意该计划，离线适配
开发的 P2/P3 本地实现与 fake 联测已完成；真实仪器接入与新联合扫描尚未验收。
统一 TOML/combination 实现见第 10 节，配置及命令见
[操作指南](../PHOTONICS_COMBINATION_SCAN_GUIDE.md)。

## 1. 项目状态与本次范围

用户要求把 four-module Integration 与 PEM/NKT 光学模块组合，扫描激发光功率和
波长，同时施加来自 `lockin_xx` 的电压激励并读取 Vxx、Vxy。本文记录目标和分阶段
工作包；新的联合入口、配置、逐点扫描和共享审计已完成本地离线实现。目标电脑的
独立 worktree 已加载代码并完成 guarded 离线验证，尚未操作真实仪器。

- 新分支：`integration-photonics-nonlinear-hall`。用户给出的分支名含空格，Git
  分支不允许空格，因此将其规范为连字符；未添加其他前缀。
- 基线：`codex/integration-four-module-scan` 的 `ae93eee`。
- 光学输入：`codex/pem-module` 的 `a8d991a`，已增量引入 NKT/PEM/PM100D 与反馈模块；
  保留四模块共享文件的集成实现，没有整体覆盖或 merge 旧共享树。
- 两条来源分支的共同祖先为 `b9e50f7`；2026-10-04 初建项目时只建立分支和计划。
  2026-10-05 按批准计划增量实现联合扫描，并加载到 LK_setup 独立 worktree；
  没有整体 merge 旧共享树、commit 或 push。
- P0 项目创建与计划：complete。P1 双 SR830 诊断完成，但 PEM REF OUT → XX TTL 同步验收
  failed：两次最小测试均失锁；PEM 激活并稳定后仍未锁定。P2/P3 本地离线完成；
  P4 target offline 已通过 634 项 guarded 测试；P5/P6 实机阶段仍待执行。
  [本次实测报告](../PEM_REFERENCE_COMMISSIONING_20261004.md) 保留失败与恢复证据。

用户已授权 SSH 连接 `LK_setup` 查看并测试 PEM REF OUT 能否被 XX 识别，且声明
接线完成。此授权针对参考同步检查；不包含本次发射 NKT 激光、功率/波长扫描、
增加样品激励、操作磁体、温控或 SMU。用户随后确认：PEM 输出为 1f（约 50 kHz），
XX SINE OUT 已接样品，XY SINE OUT 物理断开。线缆/终端和 3.4 V 测量条件仍未
完整确认；这些影响同步可靠性的解释。

## 2. 目标参考链与角色

```text
PEM REF OUT ──> lockin_xx REF IN（external TTL）
                     │
                     ├── SINE OUT ──> 已确认的电阻/样品激励路径
                     └── TTL SYNC OUT ──> lockin_xy REF IN（external TTL）

样品纵向电压 ──> lockin_xx A/B ──> Vxx
样品横向电压 ──> lockin_xy A/B ──> Vxy
lockin_xy SINE OUT：保持物理断开

NKT + VARIA ──> PEM 与经确认的光学路径 ──> 样品
PM100D：按实际接线记录功率测量平面；需要 W 反馈时参与闭环
```

用户这次指定的 **XX 外部参考** 是新光学 profile 的明确例外；既有普通电学扫描的
XX 内部参考、XY 外部参考契约不因此改变。XX 仍是电压激励输出和 Vxx 测量角色，
XY 仍只测量 Vxy，不能用仪器编号替换语义角色。

来源代码的 `config.py`、`sr830.py` 和 `LockinPointSession` 仍有 XX 内参考约束；
不能通过篡改通用配置或直接运行旧的 configure/sweep 命令绕开它。新 profile 必须
显式表达参考源、实际参考频率和每个角色的检测阶次，并针对外参考接管、失锁和清理
重新验证。PEM 控制器不负责偷偷重配两台锁相。
XX 为 SR865A 时，图中的同步输出通过 BlazeX 的 sync 模式实现；不能把该端口默认
当作旧 SR830 的固定 TTL OUT，必须保存和核对实际输出选择。
SR865A 手册印刷第 98 页把 sync 描述为 ±2 V 或 0–2 V，第 62 页又称 2.5 V。
这些描述均不提供 SR830 所要求的 >3.5 V 可靠 TTL 高电平保证。因此用户已经验证的
PEM→XX 锁定不能证明混合型号的 XX→XY 级联；须单独记录该级的实际触发/负载和
连续锁定证据，不自动修改触发、接线或加入电平转换器。

外参考下，XX 激励频率与所锁定的参考相关，不能把原有低频激励参数原封不动套到
约 50 kHz。SR830 官方手册明确 HARM 只改变检测阶次，不改变 SINE OUT 或 TTL
SYNC OUT 输出频率；选择 XX h2 不会让 XY 自动接收倍频参考。未来仍需实际验收
各角色阶次、外参考锁定和相位标签，不能把手册能力当成本次链路已通过。

## 3. 参考电平与频率边界

[SRS SR830 官方手册](https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf)
要求可靠 TTL 参考具有高于 3.5 V 的高电平、低于 0.5 V 的低电平。
用户报告约 3.4 V，应记录其测量位置、仪器输入阻抗及高/低电平定义；该数值低于
手册的可靠高电平条件，不能据此保证锁定，也不能只凭这一数值断言一定不能锁定。

[Hinds PEM200 Rev C 官方手册](https://www.hindsinstruments.com/wp-content/uploads/PEM-200-User-Manual.pdf)
第 13 页描述 3.3 V 参考输出，而第 49 页列出 5 V，存在文档口径差异。
实际控制器版本、REF OUT 插口、1f/2f 选择、线缆与负载必须记录；不据手册中的
5 V 字样否定现场 3.4 V 读数。没有波形测量证据时，软件读到频率/锁定状态只证明
这一配置下的实际同步表现，不证明电平裕量、占空比、边沿质量或长期可靠性。
本次不擅自添加电平转换器、更换接线或以软件改变硬件电平。

历史 PEM 读回约为 50.027 kHz，属于来源模块的证据，不是本次实时测量值。
以下表格专指 SR830 的限制；实际检查使用 PEM 和每个角色对应型号的当次读回：

| REF OUT 对应频率 | h1 | h2 | h3 |
|---|---|---|---|
| 约 50.027 kHz（1f） | 约 50.027 kHz | 约 100.054 kHz | 约 150.081 kHz，不支持 |
| 约 100.054 kHz（2f） | 约 100.054 kHz | 约 200.108 kHz，不支持 | 约 300.162 kHz，不支持 |

SR830 检测频率上限为 102 kHz。仍使用 SR830 的角色必须在任何 HARM 写入前检查
`harmonic × actual_reference_frequency_hz <= 102000`；h2 靠近上限仍需实际频率
裕量和稳定性验证。不得默认沿用旧的 h1/h2/h3 全阶扫描，也不得把未测阶次填成零。
新实验需要的 XX/XY 阶次及其相对于光调制、电激励的物理含义仍待确认。

[SR865A 官方手册](https://www.thinksrs.com/downloads/pdfs/manuals/SR865Am.pdf)
Rev 2.11 的规格页给出 4 MHz 频段、谐波阶数不超过 99，且谐波检测要求
`n * f_ref < 4 MHz`。因此两台都换成 SR865A 后，两路都不再受 102 kHz 限制；
但仍有型号频段、项目批准范围和样品/接线响应限制。当前混合型号下只对 XY 保留
SR830 的检测上限，不能把 XX 一起截成 102 kHz，也不能一起放宽到 4 MHz。
约 50.027 kHz 的 PEM 1f 下，SR865A 的 h1/h2/h3 分别约为 50.027/100.054/
150.081 kHz，均在其频段内；XY 仍为 SR830 时 h3 不允许。PEM 固定谐振基频不因
换锁相而变为可自由扫频，普通外参考模式下不向 XX 发送 FREQ 来强行改变这条参考链。

## 4. 分阶段实施与验收

| 阶段 | 状态 | 工作与验收证据 |
|---|---|---|
| P0 项目创建 | complete | 独立分支、目标文档、来源版本、权限范围与待确认项明确 |
| P1 REF 同步检查 | 双 SR830 历史失败；新型号待软件验收 | 2026-10-04 失败记录保留；2026-10-05 用户报告 XX SR865A 已现场锁住 PEM，完整级联及软件读回尚待验收 |
| P2 接口与纯策略 | 本地离线完成 | 独立型号适配、严格统一 TOML/安全策略、各路独立谐波、源接法/DC/保护与严格 3 T |
| P3 fake 联合扫描 | 本地离线完成 | 光学与电学两种嵌套顺序、逐点新读数、功率/参考检查、原始拒绝记录和统一 cleanup |
| P4 target offline | complete | `LK_setup` 的 Integration-photonics worktree，lyr Python 3.12.13 / 64-bit，新 src 导入/编译与 634 项 guarded 测试通过，182.286 s；未打开真实资源 |
| P5 最小有光联合点 | planned | 用户确认光路、样品激励/光功率边界和具体点后单独授权；先一个点验证参考、功率、双通道和 cleanup |
| P6 有界功率/波长扫描 | planned | 仅执行批准点表，完成 accepted-only 数据、只读分析、故障处理与日常使用文档 |

P1 先检查当前运行进程和资源占用，再使用 semantic role 的已保存本机地址查询；
不以无差别资源扫描接管其他仪器。读取前保存版本和配置来源，记录 IDN、参考源、
边沿、频率、HARM、SLVL、输入/滤波/量程与现有状态。`LIAS?`、`ERRS?` 会消费
锁存位，审计必须单独列出；测试中的转换锁存不能混同正式窗口的持续失锁。

任何必要的型号对应参考源/触发/HARM 或输出保护设置都应在确认样品连接/边界后，以
read-before-write 方式限制为本次同步测试所需的最小集合。保留写前快照、实际命令、
每次写后读回和状态。不得为“锁上”而提高激励，也不得发送 APHS 或改动未需要的
相位、输入、量程。失败时保留未知状态，按已确认的安全基线尝试 cleanup；不能盲目
恢复一个可能不安全的历史输出幅值。没有现场必要事实时完成可做的只读部分并说明
阻塞点。NKT emission、磁场、温度和 SMU 不参与这一阶段。

正式同步窗口应保存足够连续的参考频率和 lock/error 证据，并区分“XX 识别 PEM”
与“XY 也从 XX 级联同步”两个结论。读取 Vxx/Vxy 不自动构成有光联合实验验收。
P1 的原始文件位置、发送命令、拒绝原因和最终确认状态已记录在
[实测报告](../PEM_REFERENCE_COMMISSIONING_20261004.md)。不能从预期频率或
单次成功查询推断成功。

### P1 只读基线与最终诊断（2026-10-04）

原始记录 `20261004T170550_query_only_7b4d4574.json` 留在 `LK_setup` 的 ignored
数据位置，不提交。XX 读回 internal、5000 Hz、h1、4 mVrms；XY 读回 external TTL
rising、5000.05 Hz、h1、4 mVrms。这是切换前旧参考链的状态，不是 XX 已锁到 PEM。
PEM V01 读回 50027.73 Hz、AMP 160.749999 nm、STABLE=false；物理输出状态
仍不可查询，不能据频率字段推断调制已稳定或输出打开。

两次最小测试均保持 4 mVrms，实际 XX 写入仅切换外参考/恢复内参考；第一次 PEM
尚未稳定，第二次启用后读到 STABLE=true、50027.0429 Hz，XX 仍失锁 LIAS=8。
最终 XX internal/5000 Hz/h1/4 mVrms，XY external TTL rising/5000.05 Hz/h1/
4 mVrms，两台 LIAS=0、ERRS=0，无 cleanup errors。PEM disable ACK 已收到，
物理关闭状态仍不可直接证明。第二次仅对 NKT 作前后只读 OFF 验证，SDK writes=[]；
没有 NKT 设置/发光命令，没有磁体、温控或 SMU I/O。

两份 rejected JSON/JSONL 保留在 ignored `run_data/photonics_reference`。
诊断已完成，同步尚未验收；不自动重复。3.4 V 低于手册可靠高电平条件，但原因仍需
实际 REF IN 波形/负载证据，或单独审查触发方式/电平调理方案；本次未实施这些改变。

## 5. 接口复用与资源所有权

| 层 | 复用基础 | 新联合流程需补足的边界 |
|---|---|---|
| 四模块编排 | `HardwareCombinationStation` 的 open/apply/qualify/read/cleanup/close，既有 plan/store | 新光学轴与参考 profile；只有启用模块能打开资源 |
| 双锁相 | 既有 `Sr830`、`LockinPointSession`、语义角色和审计 | 增加 SR865A 适配器；公共编排使用物理量和按型号的状态，详见第 9 节 |
| NKT | `NktController.begin_session/prepare_point/verify_point/start_emission/turn_off/finish_session` | 每次发光前需要其他模块的准备屏障；仍由 NKT 控制器拥有其 SDK session |
| PEM | `Pem.capture/prepare/verify_ready/finish` | 按本点实际滤波中心派生延迟目标；明确是否需要保持参考以及结束策略 |
| PM100D/反馈 | `PowerWindow`、原有功率计 adapter 和已验收反馈策略 | 测量平面、目标 W、稳定窗口与双通道采样的时间对齐 |
| 数据/分析 | condition/attempt/raw/accepted、SQLite WAL 与只读绘图 | 光学实际坐标、参考链和光/电阶次标签、缺失阶次与质量判据 |

以上光学接口已增量引入本分支。新增 `OpticalPointSession` 复用原有 NKT/PEM/
PM100D 准备、状态检查和功率反馈，不嵌套调用 `OpticalScan.run()`；combination
拥有唯一的全局扫描/cleanup。新的 `PhotonicsLockinPointSession` 通过物理量 backend
按角色选择 SR830/SR865A，不改变旧 `LockinPointSession` 的普通电学行为。

每个物理资源只由一个会话控制。监控使用落盘事件/SQLite，不另开 VISA/COM 或消费
状态锁存。共享 `config.py`、配置模板、记录和阶段文档的变更应由集成分支统一审查。
不引入 PPMS、MultiPyVu、ETO 或 rotator。用户当前明确选用 SR865A，因此本光学
profile 的型号适配计划覆盖此前文档中排除 SR865A 的旧约定；这不表示已启用真实
控制路径，也不是将旧 PPMS/SR865A 程序整体移植进来。

## 6. 扫描、功率和数据语义

功率扫描与波长扫描各自使用一张明确点表，随后如需要才组合二维点表或外层环境轴。
实际波长范围、带宽、功率目标/允许误差、反馈方式、dwell、重复次数和顺序全部待用户
确认。保留 PM100D 驱动源电流反馈的已验收实现作为候选；不能把寄存器 ND 百分比
当成已验证的光学衰减功能，也不能默认继承其他实验的电流/光功率上限。

请求值、设备读回和分析坐标分开保存：源电流百分比、ND 设置百分比、VARIA monitor
百分比均不是瓦特。`measured_power_w` 只能来自相应测量平面的有效功率测量；缺失时
为 null，不从百分比换算伪造 W。需要实际 W 反馈却缺少功率计或稳定证据时拒绝运行，
不自动退化成开环。滤波边界读回的中心不是经过光谱仪验证的实际光谱中心。

每个 condition 至少保存：光学点索引/重复索引、请求及读回波长/带宽、源设置、目标与
实测 W/测量平面、PEM 请求/读回延迟及频率、参考链、XX/XY 实际频率/阶次、激励
Vrms、X/Y/R/phase、lock/overload/error、时间戳、配置/软件版本、正式与转换状态。
两台锁相仍是顺序读取，不能把时间接近写成硬件同时采样。

原始 rejected/interrupted/transition/cleanup 数据全部保留。每个 condition 最多一个
accepted attempt；只有完整数据、有效同步、功率/环境合格和约定 cleanup 均通过后
才能 promote。默认分析只加载 completed/accepted/clean 正式样本，缺失阶次保留原因，
不插值补零。只读分析按实际光学坐标、角色和阶次绘图；手动筛选及导出保留 manifest。
稳定 profile 可复用内容哈希档案，但不得用今天的 TOML 解释历史数据。

## 7. 安全、结束与故障

本用户当前要求对本项目始终保留 `sqrt(Bx^2 + Bz^2) <= 3 T`，包含纯 X 和纯 Z。
来源四模块代码有历史批准的纯 Z 9 T 路径与独立读回裕量；它们**不构成新模块已满足
本次要求**。P2/P3 已在新入口、完整点表、float32 命令、中间角点、读回和 cleanup
中落实本项目严格 3 T 约束，并完成离线边界测试；没有继承扩大后的读回界限。
普通电学的历史策略保留，新 profile 的真实验收仍待执行。

新联合扫描采用统一失败清理：先尝试并确认 NKT 发光关闭，保护电激励与有源 SMU，
再按配置处理磁体和温控，记录每项结果。PEM 结束只在 XX 输出保护得到明确验证后
执行；否则关闭 PEM 通信、保留参考 active/unknown 并要求人工确认。设备或审计失败
不能阻止其余独立清理尝试；不得因模块未使用而声称其物理状态为 off/zero。
历史 P1 不接管当次未授权设备，也不对它们执行 cleanup。

失锁、通信异常、超限、非法/非有限读数和清理未确认时 fail closed。保存 primary
error、cleanup errors、部分读数及 last-confirmed state。不能在通信失败后推断零场、
最小激励或关光。PEM disable ACK 不是物理光学调制已关闭的直接证据；其输出状态查询
能力不足必须保留。需要人工核验时明确到设备/接线，不静默继续扫描。

## 8. 待确认事项与验证矩阵

P1 已确认 PEM 使用 1f、XX SINE OUT 接样品、XY SINE OUT 物理断开，最小设置测试
保持已读回的 4 mVrms。仍需保留实际 REF OUT/REF IN 连接、线缆/终端及 3.4 V
测量条件的记录；不重复询问已确认事实。正式实验的高频激励安全边界仍应独立确认。

正式实验开始前还需要：

- XX 激励 Vrms/器件允许 V/I、完整串联路径及在该频率下的负载；不能用低频电阻模型
  默认为高频电流保证。
- XX/XY 各自检测阶次、相位含义、读数时序与 settling；是否测光响应基频、光/电混频
  或电学二阶信号，不能只由“nonlinear Hall”名称推定。
- 光路/偏振器/PEM 轴角、样品处功率或代理平面、是否实时 PM100D 反馈；激光启用条件
  和样品/探头的独立上限。
- 波长、带宽、功率点表，PEM 延迟目标，固定或扫描温度/磁场/栅压及正常结束状态。

P0 文档验证：相对链接存在、代码 fence 配对、分支/来源版本一致、修改范围只含
授权文档。P2/P3 至少覆盖：未授权零 I/O；XX 内/外参考隔离；1f/2f 与 102 kHz 边界；
3 T 纯轴/向量/中间角点；重复资源拒绝；功率漂移重新稳定；任一角色失锁；部分读回、
超时、中断、cleanup 失败；accepted promotion；旧电学扫描与光学 standalone 回归。
P4 记录 exact Python/import/commit 与相关测试结果，P5/P6 保存真实原始记录后才能
声明相应的 read-only commissioned / write commissioned / 联合验收状态。

## 9. SR830 / SR865A 适配实施计划（2026-10-05）

### 9.1 结构和文件责任

目标是让同一光学编排支持当前 SR865A/ SR830 混合对，以及将来可能采用的双 SR865A。
role 决定物理职责，model 决定命令和硬件能力；二者不绑定。保留普通双 SR830 回归。

```text
hardware.local.toml（角色、型号、物理参数、已批准实验边界）
  -> strict config + resolved reference plan + per-role capabilities
  -> LockinPointSession / harmonic coordinator（V、s、Hz、语义状态）
  -> model factory
       -> Sr830（原驱动）
       -> Sr865a（新增驱动）
  -> 统一读数、原始型号证据、accepted-only 存储与分析
```

| 文件 | 计划中的具体工作 |
|---|---|
| 新增 `src/attodry_control/lockin_backend.py` | 最小 `LockinBackend` Protocol、不可变能力/读回结构、按 model 选择后端的工厂；不做全自动总线发现或任意型号插件系统 |
| 既有 `sr830.py` / `sr830_settings.py` | 保留 SR830 协议、编码和既有入口；增加物理量接口的薄封装，SR830 数字代码只留在该驱动内 |
| 新增 `src/attodry_control/sr865a.py` | SR865A 命令、离散表、identity/固件验证、采样、状态解码与输出读回；不继承/伪装为 Sr830，不在构造或导入时打开硬件 |
| `config.py` / 配置模板 / `lockin_safety.toml` | 以 model 区分合法字段；解析角色和新外参考拓扑，验证每路能力与实验允许值；原模板和安全文件按 schema 版本兼容迁移 |
| `lockin_points.py` / `lockin_harmonics.py` | 提取公共逐点/谐波状态机；消除对 `lockin_test` 私有函数、`DualSr830Controller`、全局 102 kHz 和 raw code 大小比较的依赖 |
| `lockin_test.py` / `lockin_autorange.py` / `lockin_overload.py` | CLI 调公共服务；纯量程策略接收物理单位阶梯；按型号解码过载后再应用策略，不复用 SR830 位掩码或 reserve 编码 |
| `combination_hardware.py` / `interfaces.py` | 接入已验证公共点会话；区分“设置内参考频率”与“核验外参考频率”，保持单一资源所有者 |
| `models.py` / 记录与分析入口 | 增加可版本化的型号、检测频率、参数来源和原始状态证据；历史记录按原 schema 读取，不按今天型号重新解释 |

公共接口按实际任务表达，例如 `read_identity()`、`read_settings()`、
`configure_reference(plan)`、`set_time_constant(seconds)`、
`set_sensitivity(full_scale_v)`、`set_harmonic(n)`、`read_sample(...)` 和
`apply_cleanup(plan)`。当前底层实现了身份、设置、时间常数/灵敏度/阶次和采样的
物理量契约；公共参考拓扑/cleanup、激励写入与完整点会话仍属计划，不能整体视为
已经可调用的联合扫描 API。
输入范围、内参考调频及同步输出配置只在 capabilities 声明支持时调用；不支持的
请求必须在硬件设置之前拒绝。公共层不传递 `SENS 20` 一类厂家档位数字。
PEM profile 的初始化、采集和 cleanup 均不得调用旧路径的内部调频或默认恢复 XX
内参考；结束策略必须按本次参考链、已批准的输出保护目标和最后确认状态执行。

### 9.2 参数如何表示和映射

日常参数仍集中于 ignored `config/hardware.local.toml`。共享字段用物理单位和
明确枚举；型号专属字段放在对应角色的 `sr830` 或 `sr865a` 子表。字段名是拟议
schema，实施时同时更新 example、strict loader、记录和测试，不能现在直接用于旧 CLI。

| 参数语义 | 公共层/日常配置 | 型号适配与拒绝规则 |
|---|---|---|
| 角色与型号 | `lockin_xx.model`、`lockin_xy.model` 独立选择 | 先静态校验，连接后核对 `*IDN?`；型号不符在任何设置前停止，不自动改配置 |
| 参考链 | 新光学 profile 的 `reference_topology="pem_xx_xy"` 作为拓扑事实来源 | 派生两路外参考及来源；边沿和输入阻抗按角色保存。旧电学入口继续验证原内参考拓扑 |
| 时间常数 | `time_constant_s` | 适配器各自查离散表；同一个 1 s 在 SR830 / SR865A 分别对应 OFLT 10 / 12；不允许用编码大小推断等待 |
| 滤波 | `filter_slope_db_oct` 和显式滤波类型 | SR865A 的 advanced/synchronous filter 状态需要读回，不能只核对斜率就沿用 RC 等待；第一阶段限定已确认 RC 模式 |
| 输出灵敏度 | `sensitivity_full_scale_v` | SR830 映射 SENS，SR865A 映射 SCAL；范围比较全部在 V 上进行，编码顺序不参与公共决策 |
| SR865A 前端范围 | 型号子表 `input_range_v_peak` | 单独映射 IRNG，峰值范围与 demodulated RMS/输出灵敏度不是同一参数；不能从小的 R 值断言输入未过载 |
| SR830 reserve | SR830 子表 `reserve_mode` | 只接受 SR830 合法枚举；传给 SR865A 时明确配置错误，不静默丢弃或猜等效值 |
| 参考输入与 sync 输出 | SR865A 子表的参考输入阻抗、XX sync 模式 | 分别映射 REFZ 与 BlazeX；值按实际接线确定，不默认将所有参考端设成 50 ohm |
| 激励与结束幅值 | 保留 `source_voltage_v` 为仪器设置值，另有显式 source wiring/幅值定义和已批准 cleanup 目标 | 新型号必须检查 SLVL 定义、单端/差分、负载、DC offset 与 DC mode；不把同一显示数值默认为相同样品电压/电流 |
| 检测阶次 | 各角色独立的 `harmonics` / per-harmonic settings | 按每路实际参考 Hz 校验，再在改变阶次前复查；不能让伴随角色被强制写入它不支持的阶次 |

SR865A 手册给出的源设置范围为 1 nVrms–2 Vrms，标称基于差分/50 ohm 负载，
单端和高阻接法会影响实际幅值。它是硬件能力，不能替代样品批准的限值，也不能
直接继承旧代码的 4 mV“硬件最小值”和 5 V 上限。cleanup 目标按型号、接法和
实验边界显式验证；降低 AC 幅值并不自动消除 DC offset。未知接法/偏置时可先只读
诊断，不能以软件默认值证明电激励安全。没有经确认的电路/阻抗模型时，不推导样品电流。

第一阶段 SR865A 使用固定且明确的 IRNG 和 SCAL；已有 SR830 bounded-auto 保留。
后续 SR865A 自动范围应单独实现前端 IRNG 保护和 SCAL 输出尺度策略，并分别测试、
验收。不能复制“调大 sensitivity 就能消除输入过载”的旧路径；正式采样窗口内冻结
设置，调整后的转换样本单独保留并重新等待。

### 9.3 频率、采样和状态

硬件能力由适配器持有；项目批准范围由 safety policy 持有；日常扫描/阶次属于
local TOML。实际允许值取这些约束的交集，不把 102000 全局替换为 4000000。

- 为每个角色分开验证参考输入频率与 `n * f_ref` 检测频率；SR865A 按规格采用
  `<4 MHz` 检测边界、SR830 使用其 102 kHz 边界，边界包含性进入能力契约和测试。
- 开资源前用计划范围完成预检，接管和每次 HARM 转换前再用实际频率复查。外参考
  漂移越界时拒绝并清理；不静默降低阶次、截频或补零。
- XX h3 / XY h2 的混合型号组合可以分别规划，不能先把两个角色阶次求并集再让
  两台都遍历该并集。两路基频应一致，检测频率在阶次不同的情况下允许不同。
- 首期保留项目已有 h1/h2/h3 数据语义；硬件最高 99 阶不意味着本次顺便扩展全部
  阶次。以后超过 h3 时再显式扩展 `LockinReading`、CLI、schema 和分析测试。
- SR865A 默认计划用 `SNAP? X,Y` 获取同次采样的 X/Y，再在 adapter 中计算 R 与
  phase，标明它们是 derived；X=Y=0 时 phase 为无定义。保留原始回复，不能伪装成
  独立原生 R/phase 读数。旧 SR830 的原生读数和历史数据不重算。
- 参考/检测频率另外查询并记录时间：SR865A 使用 FREQEXT?/FREQDET?；SR830 没有
  对等检测频率读回时由已核验 harmonic/reference 派生并标明来源。频率与 X/Y、
  两台锁相之间均不宣称硬件同时采样。若以后使用 SNAPD?，须验证四个显示通道映射。
- 公共状态包含 locked、input_overload、output_scale_overload、instrument_error、
  validity 和 observed_at；未读取或不支持的项为 unknown，不填 false。raw LIAS/
  ERRS/即时状态、型号/固件和 latch 消费行为同时归档。相同位号不默认同义。

数据保留 requested / readback / derived、每路完整型号/identity、实际 time constant、
filter mode、输入范围与输出尺度、参考链、h、基频/检测频率、激励幅值定义和 DC 读回。
未知状态、失锁、超限或未确认 cleanup 不能 promote 成 accepted。读入历史记录时
使用当次 schema/配置快照，不能因今天 XY 换型号而重新接受昨天被拒绝的 h3。

### 9.4 实施顺序与完成证据

1. 离线契约：确认型号/固件与源输出接法；收敛上述公共结构、config 和参考拓扑，
   增加 SR865A transcript fake adapter，移除公共策略的 raw code 依赖。
2. 离线点会话：旧 SR830 路径通过回归后接入混合对/双 SR865A fake；先固定范围，
   再单独加入有依据的 SR865A 范围策略，不一次性重写无关温控、磁体或 SMU。
3. `LK_setup` target offline：确认 `lyr`、import path、版本、无硬件测试通过。
4. 授权的真实只读：型号、参考/BlazeX 配置、实际频率、幅值/DC、量程、状态；
   用户现场“能锁住”的结果单独保留，不自动替代完整同步链的软件验收。
5. 授权的最小设置：确认电路和幅值边界后按角色执行 read-before-write；独立验收
   PEM→XX、XX sync→XY、每路 h1/h2/h3（仅支持者）、失锁中断和完整 cleanup。
6. 光学 P3 fake 可在离线阶段并行推进；完整锁相验收后才进入 P5/P6 有光实验。
   换成双 SR865A 后主要改变每角色 model 和合法参数，
   上层扫描/存储接口不再为型号复制一份。

最低测试覆盖：错误型号零设置写；合法/未知/不适用参数；各型号离散表；1 s 编码；
IRNG peak 与 SCAL RMS 分离；角色独立的 102 kHz / 4 MHz 边界与外参考漂移；
混合 XX h3 / XY h2 不误写 XY h3；旧双 SR830 回归；读取截断/超时/不同状态位；
PEM 初始化/cleanup 不发送内部调频或恢复内参考；DC/接法不明与不安全输出拒绝；
中断/partial/cleanup failure；accepted-only 和旧 schema。

用户已批准上述计划。离线适配开发从底层驱动、公共能力/物理量契约和 fake 测试开始；
阶段验收结果以 PROJECT_HANDOFF 和 DEVELOPMENT_STAGES 当前记录为准。底层测试
不能代替点会话/安全配置迁移、完整 combination 接入或真实仪器软件验收。

2026-10-05 本地离线结果：P2/P3 已实现；634 项不同测试通过，最后 63 项相关复跑
通过。覆盖真实驱动接口的 fake 联合扫描、两种循环顺序、参考检查先于激励升高/发光、
功率漂移、部分启动与输出保护失败、严格 3 T、实际记录分析与旧路径回归。
测试阻断真实硬件导入和 DLL 加载；编译、diff 与文档检查通过。随后按用户要求在
`C:\Users\LK_Setup\Yuanrong Li\Integration-photonics` 建立同名分支 worktree，
以基线 HEAD 加未提交源码快照加载最新实现；210 文件哈希核对，lyr 3.12.13 下
634 项 guarded 测试再次通过。config/hardware.local.toml 按要求原样复制普通
hardware.example.toml，仍是需填写的旧电学模板。实际源接法/边界配置和完整软件
级联、有光点验收仍待 P5/P6；未连接仪器或 commit/push。

## 10. 统一 TOML 与 combination scan（用户确认后的目标）

日常操作以一份 ignored `config/hardware.local.toml` 为实验配置入口，光学和电学参数
都从该文件解析。普通电学保留既有 `lockin_safety.toml`；新 profile 通过
`photonics_lockin.safety_file` 引用本机独立型号安全文件，并核对其哈希。
“统一入口”不意味着取消安全策略，也不自动改变已批准上限。

| TOML 区域 | 职责与来源 |
|---|---|
| `nkt_source` / `nkt_varia` / `nkt_run` | 复用光学分支的连接、源设置、波长/带宽点表和发光策略 |
| `pem` | PEM 连接、延迟定义与边界；延迟依实际波长计算，参考频率取实际读回 |
| `pm100d` / `optical_scan` / `power_feedback` | 功率测量平面、实际 W、反馈目标和稳定窗口；实际启用的模式决定必需字段 |
| `lockin_xx` / `lockin_xy` / `lockin_sweep` | 各角色型号、输入/滤波/量程、阶次和激励点；新外参考 profile 禁止内部扫频 |
| `combination_scan` | 启用模块、外到内循环顺序、重复次数、每条件采样数、run 名称及记录 |

`order=["optical", "lockin"]` 表达每个光学点下执行电激励点列；固定激励时电轴只有
一个点。用户要求反向顺序时也由同一编排器支持，每个叶节点都重新采样。表名来自现有
两模块和现已实现的集成规则。统一 loader 支持光学表，只有启用模块会解析和创建设备。
温度/磁场/栅压可作为条件轴，新 profile 对目标、转场、实际读回和 cleanup 均严格
执行合场 3 T 上限，包含纯轴。模板具体未知参数必须填写后才能通过检查。

同一实验点的执行顺序为：

1. 离线解析全部启用模块，验证点表、型号能力、安全边界和唯一资源所有权。
2. 打开所需资源、保存身份与初始状态；准备各模块，发光前完成全部必要准备。
3. 每次轴变化先确认关光；每次 PEM 准备前保护 XX 输出，再设置 NKT/VARIA、PEM
   延迟和功率计波长。参考稳定且 PEM/XX/XY 实际频率一致后恢复本点电激励，按固定
   24 dB/oct RC 至少 10τ 等待，再发光、完成功率稳定/反馈并重新核验参考链。
4. 在同一条件下读取 Vxx/Vxy；电学样本前后保存光学/功率核验、各设备时间戳和状态。
5. 所有条件与有效性通过后接受该点；失败/中断保留 rejected 证据并执行统一 cleanup。

“联合/同时光电测试”指同一条件的协调采集，不宣称两台锁相或功率计硬件同时采样。
同一 run/condition 保存请求与实际波长、功率 W/测量平面、源设置百分比、PEM 状态、
电激励、Vxx/Vxy 与阶次和时间戳；设置百分比不作为实测瓦特。

实现已从 PEM 分支增量引入光学模块及回归测试，没有用旧共享文件覆盖四模块集成树。
可复用的 `OpticalPointSession` 与独立 optical CLI 共用安全准备/功率检查原语；
不在每个 combination 点嵌套调用拥有整个扫描与 cleanup 的 `OpticalScan.run()`。
全局 strict loader 同时认识合法光学表，但 inactive 模块不创建设备；只有一个控制器拥有
每个物理资源。增加 optical 轴时一起更新真实值校验、逐点功率窗口、cleanup 顺序和历史
schema 兼容读取，而不只修改命令入口。
