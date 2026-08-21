# Temperature module work package

## 当前状态

attoDRY legacy DLL 适配器已经完成离线 fake-DLL 实现。目标电脑曾在单独授权下
完成 10 秒真实只读连接：10/10 状态读取成功，sample temperature 约
1.7251--1.7255 K，VTI 约 1.7146--1.7153 K，控制标志关闭且错误码为零。

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

### T0 - contract audit（当前：planned）

- 对照 `attodry.py`、`stability.py` 和 fake-DLL 测试列出现有接口与缺口。
- 确认 Integration 需要的最小方法：读状态、确保控制状态、设定温度、等待稳定。
- 完成条件：不改硬件、不扩大接口到 PID 或未确认功能。

### T1 - offline behavior tests

- 补齐返回码、初始化超时、读失败、toggle 幂等、设定读回、稳定/超时测试。
- 测试通信失败不会覆盖最后确认状态。
- 完成条件：温度相关测试和完整离线测试通过，零 DLL 真实连接。

### T2 - target offline validation

- 在 `LK_setup` 的 `lyr` 环境运行 fake-DLL 和完整测试。
- 完成条件：记录提交号、解释器路径和测试结果，不调用 `begin/connect`。

### T3 - real read-only commissioning

- 需要新的明确连接授权，只读取温度、VTI、setpoint、control 和 error。
- 完成条件：连续记录完整，Disconnect/end 正常，无写设置或 toggle。

### T4 - smallest temperature write commissioning

- 需要用户提供最小实际目标、容差、dwell、timeout 和异常时保持/恢复策略，
  并明确授权允许的 setpoint/control 写命令。
- 完成条件：写前/写后读回、滚动稳定窗口和原始数据齐全；失败不声称稳定。

## 预计文件所有权

- `src/attodry_control/attodry.py`
- `src/attodry_control/stability.py`
- `src/attodry_control/models.py`（仅温度状态必要修改）
- `src/attodry_control/attodry_test.py`（只读验收）
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

