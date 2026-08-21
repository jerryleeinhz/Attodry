# Temperature module work package

## 当前状态

attoDRY legacy DLL 适配器已经完成离线 fake-DLL 实现。目标电脑曾在单独授权下
完成 10 秒真实只读连接：10/10 状态读取成功，sample temperature 约
1.7242--1.7246 K，VTI 约 1.7138--1.7143 K，用户设定值为 2.0 K，控制标志
关闭且错误码为零。

T0 contract audit 和 T1 offline behavior tests 已于 2026-08-21 完成。温度公共
接口收敛为 `read_state()`、`ensure_temperature_control(enabled)`、
`set_temperature(target_k)` 和 `wait_for_temperature(target_k)`；不包含 PID、
磁场或扫描组合。fake-DLL 已覆盖温度读失败、控制状态、setpoint 读回、连续
稳定窗口、错误和超时。T4 所需的显式授权 commissioning CLI 已完成 fake-DLL
验证。首次真实 T4 尝试已发送一次 1.75 K setpoint，但因 DLL 立即读回仍为
2.0 K 而 fail closed，未开启温控；随后 5 次只读状态均确认 setpoint 已异步更新为
1.75 K。第二次尝试的温控 toggle 也因立即读回仍关闭而 fail closed，后续 5 次
只读状态均确认温控已异步开启且错误码为零。T2 target offline validation 已在明确授权下通过
Git 分支完成：`LK_setup` 的 64 位 Python 3.12.13 `lyr` 对提交 `e9a7b8c`
运行 35 项温度测试和 156 项完整测试均通过，`compileall` 通过；没有加载
vendor DLL、调用 `begin/connect` 或发送硬件命令，临时 clone 已删除。

本轮已新增独立的 `temperature_commissioning.local.toml` 参数入口：从示例复制后，
在文件开头填写本次 T4 参数，不需要查找或修改 Python，也不改硬件 TOML。示例保留
不可执行的 `CHANGE_ME` 占位符；文件不含授权，连接和写入授权仍须逐次在命令行给出。
提交 `609b456` 已在 `LK_setup` 的 64 位 Python 3.12.13 `lyr` 上再次完成离线验证：
159 项完整测试全部通过、0 skipped，`compileall` 和新 CLI help 通过；未加载 DLL、
调用 `begin/connect` 或发送硬件命令，临时 clone 已删除。

当前已经证明连接、读回、异步 setpoint 更新和异步温控开启。最终 1800 s 稳定
运行没有达到目标：1799 个样品均未进入 1.75 ± 0.01 K，尽管 setpoint/控制状态
全程正确且错误码始终为零。因此 T4 未通过，下一步必须先人工核查 attoDRY 前面板/
GUI 的温控模式和 heater response，不能继续自动重试或描述为 commissioned。

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
- `Ctrl+C` 或异常的温度策略应在 Integration 中明确，不能由本模块擅自关闭
  cryostat 控制。
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
- 未增加 PID、升降温速率、异常时擅自关闭温控等未确认功能。

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

### T4 - smallest temperature write commissioning（real stability failed；manual verification required）

- 需要用户提供最小实际目标、容差、dwell、timeout 和异常时保持/恢复策略，
  并明确授权允许的 setpoint/control 写命令。
- 完成条件：写前/写后读回、滚动稳定窗口和原始数据齐全；失败不声称稳定。

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

## 预计文件所有权

- `src/attodry_control/attodry.py`
- `src/attodry_control/stability.py`
- `src/attodry_control/models.py`（仅温度状态必要修改）
- `src/attodry_control/attodry_test.py`（只读验收）
- `src/attodry_control/temperature_test.py`（写入 commissioning，双重授权）
- `tests/test_attodry.py`
- `tests/test_stability.py`

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
