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
验证，但真实写入仍未进行。T2 target offline validation 已在明确授权下通过
Git 分支完成：`LK_setup` 的 64 位 Python 3.12.13 `lyr` 对提交 `e9a7b8c`
运行 35 项温度测试和 156 项完整测试均通过，`compileall` 通过；没有加载
vendor DLL、调用 `begin/connect` 或发送硬件命令，临时 clone 已删除。

这只证明连接和读回。真实温度设定、温控启停、稳定等待和异常恢复尚未进行写入
验收，不能描述为 commissioned。

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

### T3 - real read-only commissioning（read-only commissioned：2026-08-21）

- 需要新的明确连接授权，只读取温度、VTI、setpoint、control 和 error。
- 完成条件：连续记录完整，Disconnect/end 正常，无写设置或 toggle。

本次授权记录满足 read-only 边界：10/10 一秒间隔完整状态、零错误、正常
Disconnect/end、`writes_authorized=false`，无设置写入或 toggle。sample temperature
为 1.7242--1.7246 K，VTI 为 1.7138--1.7143 K，Bx/Bz 读回和设定值均为零，
温度与磁场控制均关闭；该结果仍不能证明温控写入。

### T4 - smallest temperature write commissioning（offline tool ready；real write pending）

- 需要用户提供最小实际目标、容差、dwell、timeout 和异常时保持/恢复策略，
  并明确授权允许的 setpoint/control 写命令。
- 完成条件：写前/写后读回、滚动稳定窗口和原始数据齐全；失败不声称稳定。

离线准备：

- 新增 `attodry-temperature-test` / `python -m
  attodry_control.temperature_test`。缺少 connection 或 temperature-write 任一授权
  flag 时，在加载 DLL 前拒绝。
- 每次运行必须显式给出 target、最大允许步长、tolerance、stable range、dwell、
  poll interval、timeout，以及成功和失败后的 hold/restore 策略。
- 目标先按配置温区检查；连接后的初始完整状态用于检查
  `abs(target - initial_setpoint) <= max_delta`，通过前不发送写命令。
- 记录初始状态、目标/恢复的每个滚动窗口样本、恢复动作、最终状态和断开结果；
  读回、恢复或 close 失败均保留原始错误且不虚构稳定/恢复/断开成功。
- fake-DLL 已覆盖授权门、越界/过大步长、hold-target、restore-initial、超时恢复、
  hold-current 和 Disconnect 失败。真实执行仍必须等待用户给值并逐项授权。

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
