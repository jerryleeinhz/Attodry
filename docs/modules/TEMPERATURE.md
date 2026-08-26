# Temperature module work package

## 当前状态

attoDRY legacy DLL 适配器已经完成离线 fake-DLL 实现。目标电脑曾在单独授权下
完成 10 秒真实只读连接：10/10 状态读取成功，sample temperature 约
1.7242--1.7246 K，VTI 约 1.7138--1.7143 K，用户设定值为 2.0 K，控制标志
关闭且错误码为零。

T0 contract audit 和 T1 offline behavior tests 已于 2026-08-21 完成。温度公共
接口收敛为 `read_state()`、`ensure_temperature_control(enabled)`、
`set_temperature(target_k)` 和 `wait_for_temperature(target_k)`；不包含 PID、
磁场或跨硬件扫描组合。fake-DLL 已覆盖温度读失败、控制状态、setpoint 读回、连续
稳定窗口、错误和超时。T4 所需的显式授权 commissioning CLI 已完成 fake-DLL
验证。首次真实 T4 尝试已发送一次 1.75 K setpoint，但因 DLL 立即读回仍为
2.0 K 而 fail closed，未开启温控；随后 5 次只读状态均确认 setpoint 已异步更新为
1.75 K。第二次尝试的温控 toggle 也因立即读回仍关闭而 fail closed，后续 5 次
只读状态均确认温控已异步开启且错误码为零。T2 target offline validation 已在明确授权下通过
Git 分支完成：`LK_setup` 的 64 位 Python 3.12.13 `lyr` 对提交 `e9a7b8c`
运行 35 项温度测试和 156 项完整测试均通过，`compileall` 通过；没有加载
vendor DLL、调用 `begin/connect` 或发送硬件命令，临时 clone 已删除。

本轮已新增独立的 `temperature_commissioning.local.toml` 参数入口：从示例复制后，
在文件开头填写本次 T4 参数，不需要查找或修改 Python，也不改硬件 TOML。除已经
commissioned 为 0.2 K 的 `max_overshoot_k` 外，示例保留不可执行的 `CHANGE_ME`
占位符；文件不含授权，连接和写入授权仍须逐次在命令行给出。
提交 `609b456` 已在 `LK_setup` 的 64 位 Python 3.12.13 `lyr` 上再次完成离线验证：
159 项完整测试全部通过、0 skipped，`compileall` 和新 CLI help 通过；未加载 DLL、
调用 `begin/connect` 或发送硬件命令，临时 clone 已删除。

当前已经证明连接、实际温度读回、setpoint 更新、温控开启和 heater 升温响应。
2026-08-21 操作者据此接受 T4：实验不再以 30 分钟内进入严格稳定窗口作为模块
通过条件；超过 30 分钟后可按当时的实际样品温度开始测量，但每次测量必须记录
`sample_temperature_k`，不能把 setpoint 当成实际温度。现有 acquisition/storage
路径已经同时保存 sample/user/VTI temperature。严格 tolerance/range/dwell 结果
仍作为诊断数据保留，不删除此前未进入稳定窗口的事实。

日常运行现已独立于 commissioning：`hardware.local.toml` 的
`[temperature_run]` 集中保存 target、250 K 最大变化、0.2 K 超温保护、1800 s
测量前等待和 1 s 轮询。`attodry-temperature-run` 不要求授权 flags；调用命令本身
会连接和写温度。它在30分钟监测结束后记录实际 `sample_temperature_k` 并允许进入
测量，不要求命中严格稳定窗口。完整用户说明见 `docs/TEMPERATURE_RUN_GUIDE.md`。
实现提交 `a20fa3f` 已在 `LK_setup` 的 64 位 Python 3.12.13 `lyr` 上通过
compileall、全部217项测试（0 skipped）和新命令 help；该验证没有加载 vendor
DLL、连接设备或发送硬件命令。
兼容性提交 `d045421` 让日常温控只严格解析同一 `hardware.local.toml` 中与本模块
有关的表，不要求补写或修改 Lock-in/SMU 参数；未知顶层表仍被拒绝。该提交在
`LK_setup` 通过 compileall 和全部218项测试（0 skipped），同样没有加载 DLL。

本轮新增了日常温控的中断策略。`interrupt_policy` 缺省为 `abort`，保持原有
fail-closed 行为；`continue` 在完整状态仍安全时自动恢复一次，随后要求
`resume_recheck_s`（默认 30 s）的新读回；`wait-confirmation` 保持已确认的目标和
温控状态并询问操作者。第二次自动中断转为等待确认。过冲、非零错误、通信失败、
控制或 setpoint 无法确认等硬故障不受这些策略放宽，始终执行安全清理。SQLite
acquisition 的中断事件现在明确记录 `repeat-interrupted-condition`；恢复会从首个
未 accepted 的 condition 重测，并保留中断 attempt 的原始 rejected 数据。
每个 run/target 的 error-free、温控开启资格也会持久化；同一目标的后续模拟 condition
只执行短读回复查，不重复完整温度等待。真实 Integration 使用该资格前必须重新读取
attoDRY 并确认目标、控制和错误状态。
该策略和恢复语义已用 fake-DLL、模拟站和 SQLite 离线测试覆盖，尚未在真实硬件上
人为触发中断恢复。

新增的单模块 `temperature_scan` commissioning 编排按 `[temperature_scan]` 的
1.7--2.7 K、0.1 K 升温网格逐点调用同一 attoDRY 安全接口。它复用
`[temperature_stability]` 的 tolerance/range/dwell/timeout 和
`[temperature_run]` 的 max-delta、0.2 K overshoot 与中断策略，不复制这些事实。
每点保存请求值、实际 setpoint、实际样品温度、首次进入容差和完整稳定所需时间；
JSONL 逐样本持久化，最终另存 summary JSON 与 CSV。中断后当前点的稳定窗口重启，
进程恢复从第一个未完成点继续。该路径当前只完成 fake-DLL 离线验证，真实多点扫描
尚未授权或 commissioned。

## 模块目标

1. 独立验证温度状态读取、设定、控制启停和稳定判据。
2. 所有温控切换使用 read-before-toggle，避免盲目 toggle。
3. 用容差、稳定范围、连续 dwell、轮询间隔和总超时共同定义稳定。
4. 任何错误或通信失败保留最后确认状态，不虚构稳定或安全状态。
5. 对外提供最小温度控制接口，供 Integration 组合而不暴露 DLL 细节。

## 非目标

- 不控制磁场、SR830、SMU 或扫描组合。
- 不在本模块决定真实目标温度、升降温速率或 PID 参数。
- 不把 10 秒只读结果当成真实温控写入验收。

## 必须注意

- 真实写入前需要用户给出目标温度、允许范围、稳定容差、dwell 和 timeout。
- 每次 DLL 调用都检查返回码；初始化、连接和稳定等待都有超时。
- 控制状态不明时不发送 toggle；先读回，只有状态确定且不同才切换。
- 稳定必须同时满足：控制已开、错误码为零、全部窗口样本在容差内、窗口
  peak-to-peak 小于配置阈值、未超时。
- 通信失败后保留 `last_confirmed_state`；不能报告目标已到达。
- 日常入口的 `Ctrl+C` 策略已由 `[temperature_run].interrupt_policy` 明确：默认
  `abort`，也可选择 `continue` 或 `wait-confirmation`；硬故障仍由本模块
  fail-closed，Integration 组合时必须保留这一覆盖规则。
- DLL、COM5 和本地路径只能存在于 ignored 的 `hardware.local.toml`。

## 阶段和验收条件

### T0 - contract audit（complete：2026-08-21）

- 对照 `attodry.py`、`stability.py` 和 fake-DLL 测试列出现有接口与缺口。
- 确认 Integration 需要的最小方法：读状态、确保控制状态、设定温度、等待稳定。
- 完成条件：不改硬件、不扩大接口到 PID 或未确认功能。

审计结果：

- `AttoDryDriver` 已提供上述四个最小方法；DLL 细节保持在适配器内部。
- `stability.py` 的纯函数负责 tolerance、stable range、dwell 和最少样本数；
  驱动负责控制标志、错误码、poll interval 和总 timeout。
- `CryostatController` 通用协议尚未声明稳定等待，而仿真接口当前按
  `max_polls` 驱动。Integration 组合前需统一这一调用契约，不能让调用方猜测。
- 未增加 PID、升降温速率或自动修改控制参数；中断恢复只在完整状态已确认安全时
  保留目标并重新读回。

### T1 - offline behavior tests（offline complete：2026-08-21）

- 补齐返回码、初始化超时、读失败、toggle 幂等、设定读回、稳定/超时测试。
- 测试通信失败不会覆盖最后确认状态。
- 完成条件：温度相关测试和完整离线测试通过，零 DLL 真实连接。

完成内容：

- 控制读回只接受明确的 0/1；其它值 fail closed，且不会发送 toggle。
- 温控切换使用 read-before-toggle，并验证读回；重复请求不重复 toggle。
- 温度 setpoint 写后读取完整状态、检查错误码并验证设定值；成功状态更新
  `last_confirmed_state`，读失败或不匹配不虚构确认值。
- 等待温稳会先检查目标范围；控制关闭会清空已有窗口，不能跨控制中断拼接
  dwell；错误、通信失败和总超时均失败关闭。
- 独立 `codex/module-temperature` worktree 的本地完整离线套件为 156 tests
  passed、2 skipped（缺少 matplotlib）；没有
  加载 vendor DLL、调用 `begin/connect` 或发送真实写命令。

### T2 - target offline validation（target offline complete：2026-08-21）

- 在 `LK_setup` 的 `lyr` 环境运行 fake-DLL 和完整测试。
- 完成条件：记录提交号、解释器路径和测试结果，不调用 `begin/connect`。

完成记录：

- 分支 `codex/module-temperature`，验证提交
  `e9a7b8c280e947c921f77dc7095ac50e47c622b2`。
- 解释器 `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`，Python 3.12.13、
  64 位。
- 温度/fake-DLL/stability：35 tests passed；完整套件：156 tests passed、
  0 skipped；`compileall -q src tests` 通过。
- 只运行 Git、unittest 和 compileall；未加载 DLL、未调用 `begin/connect`、未
  发送设置写入。验证后已检查绝对路径并删除目标机临时 clone。
- 参数文件入口提交 `609b456` 也已在同一环境复验：159 tests passed、0 skipped，
  `compileall -q src tests` 和 `temperature_test --help` 通过；同样没有加载 DLL、
  调用 `begin/connect` 或发送硬件命令，临时 clone 已删除。

### T3 - real read-only commissioning（read-only commissioned：2026-08-21）

- 需要新的明确连接授权，只读取温度、VTI、setpoint、control 和 error。
- 完成条件：连续记录完整，Disconnect/end 正常，无写设置或 toggle。

本次授权记录满足 read-only 边界：10/10 一秒间隔完整状态、零错误、正常
Disconnect/end、`writes_authorized=false`，无设置写入或 toggle。sample temperature
为 1.7242--1.7246 K，VTI 为 1.7138--1.7143 K，Bx/Bz 读回和设定值均为零，
温度与磁场控制均关闭；该结果仍不能证明温控写入。

### T4 - smallest temperature write commissioning（operator accepted：2026-08-21）

- 需要用户提供最小实际目标、容差、dwell、timeout 和异常时保持/恢复策略，
  并明确授权允许的 setpoint/control 写命令。
- 完成条件：写前/写后读回、实际升温响应、连续实际温度和原始数据齐全；任何
  measurement 使用并保存当时的 `sample_temperature_k`，不声称它等于 setpoint。
- 精密滚动稳定窗口仍可配置和审计，但不再是本实验进入测量的强制门槛。

离线准备：

- 新增 `attodry-temperature-test` / `python -m
  attodry_control.temperature_test`。缺少 connection 或 temperature-write 任一授权
  flag 时，在加载 DLL 前拒绝。
- 每次运行可选择一个参数来源：直接显式给出 target、最大允许步长、tolerance、
  stable range、dwell、poll interval、timeout 和 hold/restore 策略，或使用
  ignored 的 `config/temperature_commissioning.local.toml`。后者从
  `temperature_commissioning.example.toml` 复制，所有本次参数集中在文件开头；
  两种来源不能混用。
- 参数文件缺失、未知字段、`CHANGE_ME` 占位符或非法数值都会在加载 DLL 前拒绝。
  参数文件不包含授权；每次仍须有 connection 和 temperature-write 两个 CLI 授权。
- 目标先按配置温区检查；连接后的初始完整状态用于检查
  `abs(target - initial_sample_temperature) <= max_delta`，通过前不发送写命令。
  初始用户设定值差同时写入审计记录，但温控关闭时的陈旧设定值不被当成样品
  实际移动。
- 记录初始状态、目标/恢复的每个滚动窗口样本、恢复动作、最终状态和断开结果；
  读回、恢复或 close 失败均保留原始错误且不虚构稳定/恢复/断开成功。
- fake-DLL 已覆盖授权门、越界/过大步长、hold-target、restore-initial、失败后
  disable-control、失败诊断和 Disconnect 失败。
- 真实写入表明 vendor DLL 的 user-temperature setpoint 和 temperature-control
  flag 都可能异步更新。一般相同设定值/控制状态保持幂等；commissioning 动作按
  人工 GUI 验证得到的设备顺序，先确认温控开启，再写目标温度。若温控刚从关闭
  切到开启，即使设定值读回已经等于目标，也强制重发一次目标值；其它情况下不
  重复发送命令。新值写入或 toggle 后按本次
  `poll_interval_s` 轮询完整状态，最多等待固定的 30 s acknowledgement 上限，
  未确认则 fail closed。30 s 是驱动协议安全上限，不是温度稳定判据，也不改变
  用户配置的 1800 s 总稳定超时。
- 最终运行开始时 setpoint 已确认 1.75 K、温控已确认开启，幂等检查没有重复发送
  setpoint 或 toggle。1800.187 s 内记录 1799 个完整样本：温度范围
  1.7237--1.7251 K、首值约 1.7240 K、末值约 1.7250 K，零个样本进入
  1.74--1.76 K 容差带；全部样本的 setpoint 均为 1.75 K、温控均开启、错误码均
  为零。该历史运行当时按 `hold-current` 不发送恢复动作，最终状态仍为
  1.75 K/温控开启，
  Disconnect/end 正常。原始 JSON 保留在目标机 ignored 临时路径。
- 人工 GUI 只读核查确认 sample-heater 配置并非零：maximum power 5.00 W、
  heater resistance 115.00 ohm、wire resistance 3.00 ohm。因此“未配置 heater
  上限/电阻”不能解释样品没有接近 1.75 K；GUI 的设定输入框也不能代替 DLL
  `getUserTemperature` 读回。
- `attodry_test` 已离线扩展为每个完整状态样本同时读取 sample/VTI heater power，
  JSON 使用明确的 `sample_w`、`vti_w` 单位字段。两个 getter 都检查 DLL 返回码，
  非有限或负值 fail closed，诊断驱动仍固定 `writes_authorized=false`。目标机的
  64-bit Python 3.12.13 `lyr` 已通过 compileall、全部 166 个测试（0 skipped）和
  23 个 DLL 导出符号的无连接加载检查。首次只读连接因 GUI 占用资源而在取样前
  拒绝，失败 stderr 已保留；GUI Disconnect 后的新记录完成 10/10 样本并正常断开。
  Sample heater 为 0.2036--0.2037 W，VTI heater 为 0.0004 W；样品温度为
  1.7335--1.7340 K，setpoint 始终 1.75 K、温控始终开启、错误码始终为零，
  Bx/Bz 读回与设定均为零。该结果排除“heater output 为零”，但 10 秒记录不能
  证明温度稳定或 PID 正确；诊断未授权或发送任何 setpoint、toggle 或 heater 设置。
- 随后在 GUI Disconnect 后进行 601 个一秒样本的 600.622 s 纯只读稳定性检测。
  完整记录正常 Disconnect/end，stderr 为空，且没有任何写命令：温度从
  1.7342 K 升至 1.7369 K，范围为 1.7335--1.7372 K（peak-to-peak 3.70 mK）；
  所有样本均保持 1.75 K setpoint、温控开启、零错误和零磁场读回/设定。Sample
  heater 为 0.2106--0.2217 W，VTI heater 为 0.0004 W。范围和连续控制判据满足，
  但 601/601 样本都低于 1.74 K 容差下限，故 T4 温度稳定性仍然失败，不能进入
  “已稳定”后的下一阶段，也不能据此自动调整 PID 或 heater 参数。
- 经再次明确授权，后续 1801 个一秒样本覆盖 1801.803 s；首次连接因资源占用在
  取样前拒绝并保留 stderr，GUI/其他连接释放后重试完成，stderr 为空且正常
  Disconnect/end。记录开始时样品为 1.7401 K，但所有连续容差段中最长仅
  319.313 s，没有 600 s 稳定窗口。样品传感器最低 1.7289 K，随后在约 25 s 内
  持续升至 1.9651 K 峰值，而非单点毛刺；最终仍为 1.7746 K。同期 VTI 仅约从
  1.717 K 升至 1.724 K 后回落，Sample heater 为 0.0927--0.2413 W，温控标志、
  1.75 K setpoint、零错误和零磁场状态始终有效。该局部样品温度过冲与缓慢回落
  表明明显热延迟/积分累积或样品传感器回路异常的可能性；这是一项诊断推断，
  尚不能区分 PID tuning、热接触和传感器问题。T4 仍失败，必须人工诊断并单独
  授权任何 PID/heater/control 更改，不能自动进入下一阶段。

### T4 参数含义

| 参数 | 单位 | 含义和安全作用 |
| --- | --- | --- |
| `target_k` | K | 本次动作的目标样品温度。必须落在 `hardware.local.toml` 配置的温区内；稳定判据使用 DLL `getSampleTemperature` 传感器读数检查它，而非仅检查软件设定值。它不是初始温度的自动推断值。 |
| `max_delta_k` | K | 相对连接后初始 `sample_temperature_k` 传感器读数允许的最大物理移动。若 `abs(target_k - initial_sample_temperature) > max_delta_k`，在任何写命令前终止；初始用户设定值差另行记录，它不替代温区上下限。 |
| `max_overshoot_k` | K | 运行中允许高于目标的最大样品温度增量。每个完整状态样本先写入审计；若 `sample_temperature_k >= target_k + max_overshoot_k`，立即失败并执行 `failure_policy`。阈值本身也必须在配置温区内。 |
| `tolerance_k` | K | 稳定窗口内每个样品温度样本与 `target_k` 的最大允许绝对误差；必须为正。 |
| `stable_range_k` | K | 同一连续 dwell 窗口内样品温度的 peak-to-peak 最大允许范围，即 `max(sample) - min(sample)`；可为零。 |
| `dwell_s` | s | 连续稳定窗口的最短时间跨度。窗口还必须有至少 3 个样本；控制中断、错误或通信失败会清空窗口。 |
| `poll_interval_s` | s | 两次状态采样之间的轮询间隔；只影响采样频率，不放宽 tolerance、stable range 或 dwell 判据。 |
| `timeout_s` | s | 等待目标稳定的总超时；必须覆盖 `dwell_s`。超时即失败，不声称已稳定。 |
| `success_policy` | — | `hold-target`：目标稳定后保持目标设定值且温控开启；`restore-initial`：目标稳定后恢复连接前的用户设定值和温控开关状态（若原来开启，还等待恢复温度稳定）。 |
| `failure_policy` | — | `disable-control`：写入已发生且动作失败时，保留当前设定值但幂等关闭温控并验证读回；`restore-initial`：尝试恢复初始设定值和控制状态。两者在恢复失败时都保留原始错误并记录恢复错误。原 `hold-current` 已因可能长期保持失控加热而移除。 |
| `--authorize-connection` | — | 明确允许本次连接/读取。缺少时在加载 DLL 前拒绝。 |
| `--authorize-temperature-write` | — | 明确允许本次温度 setpoint 和温控开关写入。缺少时在加载 DLL 前拒绝；只读授权不能替代它。 |

其中，`target_k` 用于样品温度稳定判据，`max_delta_k` 保护“从当前样品传感器
读数到目标”的实际温度移动，`max_overshoot_k` 保护运行中的正向过冲；三者必须
同时满足。

推荐在 `config/temperature_commissioning.local.toml` 文件开头修改上表的十个
本次参数，而不修改 Python 或 `hardware.local.toml`。该 local 文件已被 Git 忽略；
它存在也不会代替每次的两个命令行授权。

用户为首次 T4 提供的候选参数为：`target_k=1.75`、`max_delta_k=0.05`、
`tolerance_k=0.01`、`stable_range_k=0.01`、`dwell_s=600`、
`poll_interval_s=1`、`timeout_s=1800`、成功 `hold-target`、失败
历史失败策略为 `hold-current`。TOML 中目标必须写成数值 `1.75`，不能写成
字符串 `"1.75K"`。
这些值已经获得本次真实 T4 的明确连接和温度写入授权。setpoint 和温控开启均已
由后续只读状态确认，但 1800 s 内没有任何样本进入 tolerance，600 s 连续稳定
窗口未形成。保持这些参数不变，等待人工硬件核查后再决定是否重试。

人工 GUI 设温已确认可工作。随后离线代码把失败策略收紧为
`disable-control`：异常发生时先记录最后确认的完整状态（含样品/VTI 温度）、
sample/VTI heater power 和触发时间，再通过现有 read-before-toggle、DLL 返回码检查
和异步读回确认关闭温控；最终状态仍会再次读取。PID 参数没有被读取或修改。
下一候选目标为 1.8 K；用户明确选择 `max_overshoot_k=0.2 K`，因此实时终止线为
2.0 K。每秒完整状态轮询首次读到样品温度大于等于 2.0 K 时，触发样本会保留，
随后按 `disable-control` 关闭并验证温控。该阈值高于此前 1.9651 K 峰值，所以不会
对同等幅度的过冲提前动作；这是用户明确接受的限制，而不是软件对安全余量的推断。

提交 `d4a6487` 在本地通过 170 个测试（2 个可选绘图测试 skipped），在
`LK_setup` 的 Python 3.12.13 `lyr` 上通过 compileall 和全部 170 个测试（0
skipped）。用户随后明确把本次 `max_delta_k` 改为 250 K，实际取消了 1.8 K
动作的起始步长限制；运行时 2.0 K 终止线仍保留。真实只读预检为：样品
1.7242 K、旧设定值 1.7000 K、温控开启、错误码 0、sample/VTI heater
0.0091/0.0004 W，并正常断开。

真实 1.8 K 运行通过 0.0758 K 的起始差值检查，确认 1.8 K setpoint 和温控开启，
随后记录 1799 个完整样本、覆盖 1800.079 s。样品从 1.7241 K 缓慢升温，范围
1.7237--1.7886 K，最高值出现在约 1776 s，末值 1.7883 K；1799 个样本均未进入
1.79--1.81 K 容差带，也没有样本达到 2.0 K。VTI 范围为
1.7131--1.7176 K；等待期间 setpoint 始终 1.8 K、温控始终开启、错误码始终为
零。1800 s 后稳定性超时，失败快照记录样品 1.7883 K、sample/VTI heater
0.1054/0.0004 W。`disable-control` 随后确认关闭温控；最终样品 1.7882 K、
setpoint 仍为 1.8 K、错误码 0，并正常 Disconnect/end。原始 JSON/stderr 保留在
`LK_setup` ignored 临时路径。T4 仍未通过，不能进入下一阶段。

随后人工操作确认了一个设备顺序要求：必须先开启 full temperature control，再
设置 sample temperature，温度才会按目标正常响应。commissioning 流程已据此改为
先 `ensure_temperature_control(True)` 并确认读回，再调用 `set_temperature`。由于
上次失败清理后控制关闭但 setpoint 仍为 1.8 K，新流程在 off-to-on 情况下会强制
重发一次 1.8 K，而不是被一般 setpoint 幂等检查跳过。审计记录明确保存动作顺序和
是否请求了强制重发；PID、2.0 K 终止线和失败关闭逻辑不变。

提交 `eaa3ba0` 在本地通过全部 172 个测试（2 个可选绘图测试 skipped），并在
`LK_setup` 的 Python 3.12.13 `lyr` 上通过 compileall 和全部 172 个测试（0
skipped）。第一次真实预检因 GUI 资源占用连续两次在采样/写入前被 Connect
错误拒绝；GUI Disconnect 后预检正常。此时样品 1.7241 K、setpoint 1.6000 K、
温控已经开启，所以第一轮只确认控制开启再写 1.8 K，没有 toggle 或 forced
reapply。1800 个样本覆盖 1800.969 s，样品范围 1.7240--1.7785 K，零样本进入
1.79--1.81 K；超时后确认温控关闭、错误码 0、正常断开。

该清理形成了关键初态：温控关闭而 setpoint 保留 1.8 K。第二轮审计确认初始控制
为 false、`setpoint_force_reapply_requested=true`，因此实际执行了 off-to-on
toggle、确认开启、再强制重发相同的 1.8 K。1799 个样本覆盖 1800.016 s，样品
范围 1.7245--1.7893 K，最高约在 1781 s；仍无样本达到 1.7900 K 容差下限，且
无样本达到 2.0 K。等待期间 setpoint/control/error 均保持 1.8 K/开启/零错误。
超时快照 sample/VTI heater 为 0.1059/0.0004 W；最终样品 1.7893 K、setpoint
1.8 K、温控确认关闭、错误码 0，并正常 Disconnect/end。该顺序比未发生 toggle
的第一轮最高值提高约 10.8 mK，但仍未满足稳定判据，不能将 T4 标为通过。两轮
JSON/stderr 均保留在 `LK_setup` ignored 临时路径。

最终验收决定（2026-08-21）：操作者确认“能够升温并记录当时实际温度”满足本
实验的 Temperature module 目标，30 分钟后允许进入测量而不要求命中原严格稳定
窗口。`max_overshoot_k` 的 commissioned 值为 0.2 K；对 1.8 K 目标即 2.0 K
实时终止线。该决定不改写历史数据，也不表示样品达到 setpoint；Integration 必须
把每次测量的 `sample_temperature_k` 与 setpoint 一起保存。

### T5 - ascending stability-timing scan（target offline complete：2026-08-25）

- 新增严格 `[temperature_scan]` 网格和 `attodry-temperature-scan` CLI；只有显式
  `--authorize-temperature-scan` 才能加载 DLL 和进入真实写路径。
- 1.7--2.7 K/0.1 K 的 11 点路径在打开资源前完整展开并验证，只允许升温；降温保护
  语义尚未确认，因此不接受负步长。
- 每点保持已验收的 control-before-setpoint 顺序，并用真实 sample sensor 判定
  tolerance/range/dwell。正常结束保持最终目标与控制；失败尝试关闭并确认温控。
- 每个 raw sample 和 transition 增量写入 JSONL；summary JSON 和 CSV 记录每点实际
  setpoint、实际样品温度、首次进入容差和满足稳定窗口的耗时。
- `continue`/`wait-confirmation` 后重新复查状态并重启当前点稳定窗口；进程退出后的
  `--resume-progress` 只跳过连续完成点，拒绝配置不一致或已完成记录。
- fake-DLL、配置、网格、过冲清理、软中断和进程恢复测试通过；未加载真实 DLL，未
  连接 attoDRY，未发送 setpoint 或 toggle。
- `LK_setup` target-offline 使用确切的 64 位 Python 3.12.13 `lyr` 和独立 DLL-free
  快照完成：compileall、315 项测试（0 skipped）、11 点配置展开、CLI help 与无授权
  pre-DLL 拒绝均通过；快照 SHA-256 为
  `CB8CAC713B92FB414E6382710878DA8E7DA39CAA5EB26CB765FB90F331BA3DBC`。
  验证后的目标目录和传输压缩包已逐路径核对、删除并确认不存在。
- 目标用户目录下没有现存 `hardware.local.toml`，因此真实执行前仍需建立/核对 ignored
  本地配置和 DLL 路径。真实执行需另行确认，建议先验收 1.7--1.8 K，再决定完整
  1.7--2.7 K。

## Stable-readback measurement mode (offline implementation)

The scan now supports `temperature_stability.acceptance_mode = "stable-readback"`.
In this mode the requested setpoint remains a commanded and audited value, while
measurement readiness is decided from the actual sample-temperature readback:

- `stable_range_k` and `stable_dwell_s` define the continuous plateau;
- `min_response_k` requires each point after the first to move measurably from its
  point-start sample temperature;
- `measurement_temperature_k` is the mean of the stable readback window and is the
  temperature coordinate for downstream measurement;
- the requested setpoint is never substituted for the actual sample temperature;
- PID gains and heater configuration are not written. Heater power remains a
  diagnostic readback.

The stability evaluator retains one sample before the rolling-window cutoff, so
normal polling jitter cannot prevent a dwell window from reaching its required
duration. The legacy `target` mode remains available and requires `tolerance_k`.
Offline fake-DLL and jitter tests cover both modes. A real run using this mode is
still a separate hardware test and must retain the existing control, error,
setpoint, overshoot, logging, and cleanup checks.

## 预计文件所有权

- `src/attodry_control/attodry.py`
- `src/attodry_control/stability.py`
- `src/attodry_control/models.py`（仅温度状态必要修改）
- `src/attodry_control/attodry_test.py`（只读验收）
- `src/attodry_control/temperature_test.py`（写入 commissioning，双重授权）
- `src/attodry_control/temperature_run.py`（无额外授权参数的日常运行入口）
- `src/attodry_control/temperature_scan.py`（显式授权的多点 commissioning 入口）
- `tests/test_attodry.py`
- `tests/test_config.py`
- `tests/test_stability.py`
- `docs/TEMPERATURE_RUN_GUIDE.md`
- `docs/TEMPERATURE_SCAN_GUIDE.md`

`attodry.py` 同时服务 Magnetic 模块。若两个 Chat 并行，必须使用不同 worktree，
并由 Integration 重新运行冲突后的完整测试。

## 新 Chat 启动提示

```text
请负责 Temperature 模块。先按 AGENTS.md 顺序完整阅读四份必读文档，再阅读
docs/modules/README.md 和 docs/modules/TEMPERATURE.md。检查 git status 和当前提交，
从 TEMPERATURE.md 最早未完成阶段开始，只修改温度相关行为和测试。默认只使用
fake DLL，不连接真实 attoDRY，不发送温度设定或 toggle；真实动作等我逐阶段授权。
LK_setup 上只能使用 lyr。结束时按模块交付格式报告。
```
