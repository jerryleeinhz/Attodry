# 项目开发与测试失败复盘

更新日期：2026-10-15。代码参考版本：`14dcde8`。

本文总结 attoDRY 温度/磁场、Lock-in、SMU 及组合扫描开发中已经遇到的问题，
供后续开发、测试和交接使用。它不授予新的硬件操作权限，也不改变实验配置。
原始实验记录、仪器地址、个人路径和电脑身份保留在本地，不随本文发布。

## 1. 先明确当前状态和复盘范围

- 当前普通硬件 `run`/`resume` 已按用户要求移除 OS 进程、PID、Jupyter/GUI 检查，
  以及原进程互斥锁和 station lease；没有自动创建周期监控或稳定范围重试任务。
- 仪器授权、接线声明、温度/磁场稳定性、电学保护、实际状态和清理验证仍然生效。
- 历史进程守卫及其修补属于问题复盘，不能照搬成当前运行要求。
- 同一 run 的整条件恢复已实现；完成后 Sample Exchange 和特定实验的自动放宽策略
  属于本地单次 operator，普通 CLI 不自动安装这些策略。
- 离线实现、目标机离线验证、实机开始、实机完成和最终清理通过是不同的验收状态。
  下文不把任何一次失败扫描或测试通过写成完整实机验收。

当前行为以 [硬件与安全说明](HARDWARE_AND_SAFETY.md)、
[组合恢复说明](COMBINATION_RECOVERY.md) 和 [项目交接](PROJECT_HANDOFF.md) 为准。

## 2. 项目开发中有效的做法

### 2.1 按物理职责分层，复用已有实现

保持依赖方向：配置与点列 → 纯策略 → 厂商适配 → 单点执行/组合编排 →
原始审计 → 只读分析与 Notebook。

- Integration 复用温控、锁相和 SMU 的单点接口，不启动完整独立扫描来拼组合结果。
- CLI 与恢复路径复用配置解析、点生成、状态校验和清理逻辑；
  Notebook 仅复用只读分析、筛选、换算与拟合逻辑。
- 厂商 adapter 翻译命令并保留返回码；编排层决定步骤，不在多处重写同一保护规则。
- 将 clock、sleep、DLL 和 resource factory 注入测试，才能快速覆盖超时和故障路径。
- 一个已验证的冷台连接只由明确的执行上下文使用；交接已有连接时重置事件接收器和
  qualification 上下文，避免上层再次初始化同一设备。

### 2.2 配置只表达一份事实，角色与接线分开

使用 `lockin_xx`、`lockin_xy`、`gate_bottom` 等语义角色，避免设备编号成为隐含逻辑。
角色名称仍不能替代接线核验；型号、参考源、物理激励连接与当前配置要分别记录。

扫描目标/顺序、稳定判据、硬保护和测量设置应分开。特别是：

- `stable_range_k`、目标容差、dwell 和等待超时含义不同，调整一个不自动修改另外几个。
- Lock-in Reserve、灵敏度满量程和实际输入状态不能互相替代；不同型号的原生状态码
  也不能共用解释表。
- SMU 自动量程不是 compliance；软件读回边界和硬件限流要有清楚的对应关系。
- 修改配置应显示语义差异，不能只比较整段文本或 JSON 字符串。

### 2.3 从运行开始就设计可恢复的数据模型

使用 `run → execution → condition → attempt → raw sample` 分层：

1. run 固定完整有序点列，包括重复端点、轴顺序、谐波、组数和 repeats。
2. 每次恢复新增 execution，归档当段源码、配置及 review。
3. accepted 条件保留；失败条件整条件重测，不拼接不同尝试的部分谐波。
4. 数据库事务绑定恢复时的父状态，并限制每条件最多一个 accepted attempt。
5. 原始 rejected/invalid、主错误、清理错误和旧人工复核标记永不被新成功覆盖。

恢复能力不能靠重跑 `run --json` 获得；`--json` 只改变输出格式。
变更点列或采样结构应新建 run。分析按每段归档配置解释数据，不用今天的 TOML
重新解释昨天的样本。

### 2.4 把“动作发生”和“状态已确认”分开

| 证据 | 能确认的内容 | 不能据此确认的内容 |
|---|---|---|
| 创建进程/命令返回成功 | 请求已被软件或操作系统接受 | 实际采集已开始、扫描完成 |
| `recovery_preflight_verified` 及条件事件 | 新执行段通过预检并进入对应步骤 | 全部剩余条件已经成功 |
| 清理 `attempted`/`verified` | 相应复位动作及读回的结果 | 旧故障消失、总体 `clean=true` |
| 最后温度/场读回 | 该时间点的已记录状态 | 此刻状态，或通信失败后的零场/OFF |
| 厂家升温 start 返回成功 | 厂家接受启动调用 | 实际升温已 ACK、已到室温或可取样 |

温度、功率和环境查询按顺序发生，时间戳要保留，不能称为同时采样。
先持久化原始故障读回，再验证；否则最能说明失败原因的样本可能丢失。

### 2.5 分析界面使用同一份筛选结果

原始 snapshot 保持不变；从它产生一份显示/拟合共同使用的 selection。
排除某频率或某激励值时，两种排除累计生效：保留条件为
`f 不在排除集合 AND U 不在排除集合`。

- Apply 应直接重绘当前快照，不要求用户猜测是否还需 Refresh。
- Refresh 才读取新数据，并保留已应用排除；新增坐标默认纳入。
- 拟合与图必须使用相同的行和质量筛选，不能一个用缓存、另一个用原始全集。
- 拟合开关、所选文件和 Applied selection 状态应可见；失败刷新要清除旧图和旧拟合。
- Notebook 是只读分析入口，不因执行绘图 cell 自动连接仪器或启动后台计时器。

## 3. 实际失败案例与经验

### 3.1 温度稳定超时或偏离已确认基线

**类别：实机运行失败，不等同于单元测试失败。**

已发生两类不同终止：等待稳定超时，以及 qualification 后正式采样阶段偏离基线。
`interrupt_policy=continue` 的既有含义不能推广为忽略所有温度故障；过载继续策略也
不适用于温漂、失锁、通信或限流问题。

经验：先核对失败 execution 的目标、qualification 窗口/均值和实际触发读回，
再决定需要调整哪一个参数。清理阶段温度后来回到目标附近，不能推翻此前的失败。
历史自动放宽仅处理获授权的精确范围错误，不能把 generic timeout 或同时违反目标
容差的故障改分类后继续。当前没有自动放宽任务。

证据入口：[恢复说明](COMBINATION_RECOVERY.md)、
[test_combination_resume_hardware.py](../tests/test_combination_resume_hardware.py)。

### 3.2 频率量化与判据不一致

**类别：实机边界暴露的软件一致性问题。**

联合路径曾使用比既有扫频路径更严的频率读回阈值，使仪器量化差异导致正式采样或
清理失败。修复是统一已审查的 sweep frequency policy，并归档策略版本，
而不是关闭所有频率、失锁或错误检查。

经验：同一个物理判据必须覆盖预检、转场、formal 和 cleanup。测试同时覆盖容差内、
边界外、部分采样保留、回到基准频率和失锁拒绝，不能只测试“能设到目标”。

证据入口：[test_combination_frequency.py](../tests/test_combination_frequency.py)、
[阶段记录](DEVELOPMENT_STAGES.md)。

### 3.3 预检后再次连接冷台

**类别：实机初始化失败与连接生命周期问题。**

历史恢复先取得物理验证连接，随后正式编排又开始一次连接，第二阶段出现设备初始化
错误。已有证据支持修正连接生命周期；厂家内部为什么返回该错误码未被证实。

处理：正式执行接管同一已验证 driver，重新绑定事件与上下文，保留单次关闭路径。
测试注入初始化、断开和 end 的组合错误，验证未出现 ACK 时不能报告测量已恢复。

证据入口：[test_combination_initialisation_resolution.py](../tests/test_combination_initialisation_resolution.py)。

### 3.4 PID 复用、分析进程退出被当成扫描冲突

**类别：历史监管假阳性。**

曾发生已退出 Python 的 PID 被只读 PowerShell 检查复用，以及注册的分析进程退出
使恢复守卫失败。PID 数字不是长期身份；分析进程变化也不直接证明仪器受干扰。

历史修补曾使用完整身份和 Windows 事件关联。最终用户明确要求移除进程守卫，
当前普通 run/resume 已移除这些检查、mutex/lease 和自动监控。不能为了保持旧测试
或旧 operator 的假设而偷偷重新启用。

经验：进程清单不能代替仪器通信、状态与物理读回；监管机制本身也需要误报测试。

证据入口：[test_combination_guard_identity_resolution.py](../tests/test_combination_guard_identity_resolution.py)、
[test_combination_process_guard_removal.py](../tests/test_combination_process_guard_removal.py)。

### 3.5 文件名和 JSON 表示造成假阻挡

**类别：外围工具/文件比较错误。**

历史只读监控把带有 identity 字样的源码 helper 误判为升温运行文件；另有准备/封存
步骤因为 `30` 与 `30.0`、JSON 对象键顺序或 PowerShell 数组展开而停止。
这些现象不能直接解释为仪器故障或“厂家升温已启动”。

经验：只识别明确的 runtime claim/结果结构，不靠模糊文件名；数值做有限数语义比较，
拒绝 bool/NaN/inf；对象按字段比较、数组保持顺序和层级。claim 前失败与 claim 后
结果不明要区分，不能把 file-only 准备失败当成硬件动作失败。

相关一次性 PowerShell 审计/准备工具和原证据保留在本地 ignored `run_data`；
它们没有作为通用运行功能发布。

### 3.6 Ctrl+C 请求成功，但扫描没有立即退出

**类别：异步停止确认问题。**

历史隐藏进程接受 Ctrl+C 后，短时间等待仍未得到终态，之后才记录中断与清理。
接受信号和执行完清理是两个事件；客户端等待超时也不能被写成原进程退出码。

经验：保留请求回执、等待结果、数据库终态和清理证据；结果不明时先读回确认，
不重发启动、覆盖 claim、拼历史样本或用进程消失推断仪器已经安全。

### 3.7 Notebook 排除/绘图/拟合状态不同步

**类别：用户交互缺陷及回归覆盖不足。**

用户报告先排除 f 再排除 U，图仍表现为只应用频率排除；同时 Apply、Refresh 和
harmonic fitting 的使用方式不清楚。当前回归从完整用户操作序列验证累计排除，
而不是只检查两个下拉列表存在。

新增五项合成控件测试覆盖：无频率预选拟合、f/U 累计与 Apply 重绘、Refresh/Restore、
质量筛选与拟合开关、刷新失败清除旧结果。图和拟合收到的 selection 必须一致。

证据入口：[test_saved_progress_notebook_controls.py](../tests/test_saved_progress_notebook_controls.py)。

### 3.8 旧 Notebook 入口断言过期

**类别：测试断言过期。**

发布前首次 70 项 Notebook/分析测试出现 1 个 failure 和 9 个 errors。
其中 failure 是测试仍要求旧 `build_progress_panel` 调用，而 Notebook 已使用
`build_notebook_progress_panel`；这不是排除功能再次失败的证据。

处理：更新为验证新入口及实际 plot/fit callbacks，继续保留代码编译、空输出、
无硬件入口和无自动 Timer 的约束，并增加行为回归。不能用删除断言让测试变绿。

证据入口：[test_commissioning_progress_widgets.py](../tests/test_commissioning_progress_widgets.py)。

### 3.9 Windows 沙箱 TEMP 权限错误

**类别：测试环境失败。**

上述 9 个 errors 来自可选导出测试的临时目录创建/清理 `PermissionError`。
定位后仅把测试进程的 TEMP/TMP 指向仓库内 ignored 目录，没有修改产品导出逻辑。
加入新回归后，四个相关模块共 75 项通过。

经验：先看 traceback 中实际失败的文件操作；产品断言失败、环境错误和硬件错误要分开。
记录解释器、临时目录策略、重跑命令和返回码。首次错误仅保存在当时工具输出，
不能声称仓库中存在独立保存的首次失败日志。

### 3.10 发布前发现测试夹具是真实实验记录

**类别：发布审查发现的数据边界问题。**

真实 Windows XML、身份信息及测量记录曾被放在未提交的 tests 目录；改名字或删除
姓名不足以把整段真实实验轨迹变成合成数据。

处理：原件与哈希留在 ignored 本地备份，公开 fixture 从零生成虚构身份、时间、
XML 和读回值，保留故障形状、时间关联、原子 claim 与反例测试；无引用快照不纳入提交。
合成测试通过，不代表原站点证据被修改或原故障已物理解除。

证据入口：[generate_synthetic.py](../tests/guard_identity_fixtures/generate_synthetic.py)。

### 3.11 跳过可选分析 cell 后导出报错

**类别：Notebook 执行顺序造成的软件缺陷。**

历史上用户只运行频率或激励分析，未运行温度分析 cell，导出却仍读取温度状态，
导致 `NameError`。处理是仅导出实际存在的可选分析状态，缺失部分明确记为空，
而不是要求用户为了导出频率图先执行全部温度分析。

经验：测试 Notebook 的合理部分执行组合；验证“未运行”“没有数据”“加载失败”
与“已运行但结果为空”这些不同状态。导出使用已缓存图和对应 manifest，不在导出
时悄悄重新拟合或给旧图重新贴标签。

证据入口：[test_notebook.py](../tests/test_notebook.py)。

### 3.12 测试依赖、skip 与 stderr 进度误判

**类别：测试环境和运行器错误。**

历史记录还暴露两类“测试没有真正跑完”的情况：

- 禁用 user-site 后，所选解释器找不到原先可用的分析依赖，产生缺依赖/skip。
  应核对依赖来源和导入路径，不能把跳过功能当成该功能通过。
- unittest 正常把进度写到 stderr，目标 PowerShell 包装器却把它当成终止性
  `NativeCommandError`，在原生测试完成前中断了验证。

经验：分别保存 stdout、stderr、原生退出码和最终测试统计；stderr 有内容不等于
测试失败，也不能仅凭包装器返回成功就认定底层测试成功。记录是否使用 `-s`、
是否缺依赖及 skip 原因，未生成完整 receipt 时保持“未完成”状态。

### 3.13 最终配置合法，但切换中间状态不合法

**类别：实机设置顺序问题。**

历史混合 Lock-in 准备中，最终量程配置合法，但先降低 Reserve、后扩大灵敏度满量程，
使中间状态出现输入/Reserve 过载。只检查最终 TOML 不能发现这一问题。

处理：在最小激励下先扩大允许的灵敏度满量程，再切 Reserve，最后按目标缩小；
各步验证实际读回和状态。保留相位、参考源及未授权的接收机输出设置。
测试验证命令顺序、每步故障和最终基线，不能只断言最后写了哪些数值。

证据入口：[双角色 TOML 准备说明](HARDWARE_AND_SAFETY.md#explicit-dual-role-toml-application-2026-10-09)、
[test_combination_resume_hardware.py](../tests/test_combination_resume_hardware.py)。

## 4. 可重复的测试与排错流程

### 4.1 先分类，再缩小到能证伪的范围

1. 保存首次 traceback、命令、返回码、解释器和源码版本；不要只留下最后一次 OK。
2. 判断属于配置拒绝、产品断言、测试环境、厂商返回/通信、物理条件还是外围监管。
3. 找到最小失败输入：一个条件、一条状态帧、一次控件操作序列或一组 XML 事件。
4. 修复规则所属层，保留失败反例；不要靠调大所有容差或增加无限重试掩盖原因。
5. 运行相关回归，并解释验证范围；剩余实机风险单独记录。

### 4.2 测试层次

| 层次 | 验证重点 |
|---|---|
| 纯配置/策略 | 未知字段、单位、有限数、边界、点顺序、重复端点、允许修改范围 |
| fake DLL/VISA | 命令顺序、状态消费、坏返回、部分读回、初始化/关闭、故障清理 |
| SQLite/恢复 | 父状态事务、单次 claim、整条件重测、accepted 唯一性、原始数据不变 |
| 分析/Notebook | JSON/cell 编译、实际控件行为、累计排除、同一绘图/拟合 selection、失败清旧图 |
| 目标机离线 | 实际 Python/import 路径、Windows/PowerShell 语义、硬件 I/O 被阻断 |
| 限定实机验收 | 明确授权范围内的实际 ACK、formal 数据、状态、返回码及最终清理 |

硬件阻断入口为 [tools/run_guarded_tests.py](../tools/run_guarded_tests.py)。
它阻断真实 VISA/串口及仪器 DLL；历史所有权测试允许预加载的系统 kernel32，
不能把这个例外扩大成任意 DLL。

临时目录有问题时，可在仓库根目录仅为本次离线测试设置并恢复环境变量：

```powershell
$testTemp = Join-Path (Get-Location) 'run_data/offline-test-tmp'
New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    $env:TEMP = $testTemp
    $env:TMP = $testTemp
    python -s -B tools/run_guarded_tests.py tests.test_saved_progress_notebook_controls
}
finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
}
```

### 4.3 通过数量必须对应版本与范围

`14dcde8` 发布前通过三组离线测试：硬件/恢复/存储/策略 133 项、历史能力与合成
证据 50 项、Notebook/分析 75 项。合计 258 次测试执行，不声明为 258 个互不重复
场景；unittest 导入的测试类可能在不同套件中重复计数。

这些结果证明对应软件路径在 fake 条件下通过。它们不能证明长期温度稳定、当前接线
正确、扫描全量完成、加热器已经 OFF，或所有模块排列都经过实机 commissioning。
本文这次文档提交不重新运行硬件回归，只检查文档差异与本地链接。

## 5. Git 交付与后续改进

- 一个模块使用已有独立 checkout，推送前确认分支；不要把光学模块改动混入电学交付。
- 只 stage 明确文件；local TOML、数据库、日志、原始夹具和 operator claim 留在本地。
- 推送成功后核对 origin 分支的实际提交号；目标电脑 HEAD、源码字节和本地改动另行说明。
- 发布成功不等于实机已更新，更不等于扫描或升温完成。
- 项目交接只保留当前状态和可追溯摘要，详细运行时间线留在私有记录，避免旧 active
  或旧监管策略长期出现在“Current”章节中。
- 后续改进应优先减少 Notebook 本地重复分析逻辑、统一状态文案与可见选择，
  并逐步把确有复用价值的 operator 能力整理成通用模块；先定义契约与测试，不直接
  复制带有站点身份和实验预算的单次脚本。本段是建议，不是本次已实现功能。

开发接口、模块交付模板和既有规范见
[项目模块开发指南](PROJECT_MODULE_DEVELOPMENT_GUIDE.md)。
