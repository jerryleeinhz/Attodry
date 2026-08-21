# Magnetic-field module work package

## 当前状态

X/Z 矢量模型、3 T 合成场约束、安全中间路径、稳定等待和 monitored zero 已在
fake-DLL 中实现和测试。目标电脑真实 10 秒只读连接只确认当时 Bx/Bz 及 setpoint
均为零、field control 关闭、错误码为零。

真实磁场设定、field-control toggle、小幅运动、路径跟随和归零写入均未验收。

## 模块目标

1. 对所有目标和中间点强制执行 `sqrt(Bx^2 + Bz^2) <= 3 T`。
2. 保留硬件 X/Z 额定信息，同时项目中的纯 Z 命令也限制在 3 T。
3. 独立验证读回、设定、read-before-toggle、协调路径、稳定等待和 monitored zero。
4. 通信失败后保存最后确认的 Bx/Bz、setpoint、control 和 error，不推断零场。
5. 对外提供最小矢量场接口，供 Integration 使用。

## 非目标

- 不控制温度、SR830、SMU 或机械 rotator。
- 不把 attoDRY 软件中的 X 与工厂表的 transverse Y 混用。
- 不在未经校准时声明正负方向对应样品的物理方向。
- 不把“已发出归零命令”描述成“已经归零”。

## 必须注意

- 软件坐标使用 X/Z：X 是工厂表中称为 Y 的 3 T 横向线圈，Z 是 9 T 轴向线圈。
- 本项目不使用 9 T 能力；任何实验目标和路径点合成场都不得超过 3 T。
- 两分量分别一次写入不能保证中间路径安全。必须预先生成并验证协调矢量点，
  每一步结合读回确认。
- `ensure_field_control` 必须先读状态再决定是否 toggle。
- 稳定同时要求读回接近目标、滚动窗口范围合格、控制开启、错误码清零且未超时。
- cleanup 只有在最终 Bx/Bz 读回满足零场容差时才能记为 verified zero。
- 通信中断、电脑崩溃、DLL 返回失败或 timeout 后需要人工检查 attoDRY/APS100；
  软件不能声称零场。
- 第一次真实运动必须由用户选择最小实际 Bx/Bz 目标、容差、timeout 和恢复策略，
  并单独授权每类写命令。

## 阶段和验收条件

### M0 - contract audit（当前：planned）

- 对照 `models.py`、`safety.py`、`attodry.py` 和 tests 盘点现有矢量接口。
- 确认路径、稳定和归零的状态记录契约。
- 完成条件：3 T 不变量在目标、路径、数据库和测试中表述一致。

### M1 - offline safety and failure tests

- 覆盖纯 X、纯 Z、边界、斜向超限、非有限值和每个中间点。
- 覆盖 toggle、分量写失败、读回失败、稳定 timeout、zero timeout 和 Ctrl+C。
- 验证失败后最后确认场不被零值覆盖。
- 完成条件：相关测试和完整离线测试通过，零真实 DLL 连接。

### M2 - target offline validation

- 在 `LK_setup` 的 `lyr` 环境运行全部 fake-DLL/安全测试。
- 完成条件：记录提交号、解释器和测试结果，不调用 `begin/connect`。

### M3 - real read-only commissioning

- 需要新的连接授权，仅读取 Bx/Bz、setpoint、field-control 和 error。
- 完成条件：连续读回完整，断开正常，零设置/toggle 写命令。

### M4 - smallest single-axis movement

- 用户分别确认最小 X 或 Z 目标、顺序、容差、dwell、timeout 和失败恢复；明确
  授权 field-control、setpoint、sweep/zero 所需命令。
- 一次只验收一个轴和一个小目标，不与温度、Lock-in 或 gate 扫描组合。
- 完成条件：目标和归零均以读回验证，原始记录完整。

### M5 - coordinated X/Z path

- 只在 M4 通过后申请新的授权。
- 使用多个已验证中间点测试斜向或定模路径，全路径保持合成场不超过 3 T。
- 完成条件：每个路径点和最终归零均有读回、状态和 timeout 证据。

## 预计文件所有权

- `src/attodry_control/models.py`
- `src/attodry_control/safety.py`
- `src/attodry_control/attodry.py`
- `src/attodry_control/stability.py`（只使用通用稳定契约）
- `tests/test_models.py`
- `tests/test_safety.py`
- `tests/test_attodry.py`

## 新 Chat 启动提示

```text
请负责 Magnetic-field 模块。先按 AGENTS.md 顺序完整阅读四份必读文档，再阅读
docs/modules/README.md 和 docs/modules/MAGNETIC_FIELD.md。检查 git status 和当前
提交，从最早未完成阶段开始。必须保持 sqrt(Bx^2+Bz^2)<=3 T，通信失败不得推断
零场。默认只使用 fake DLL，不连接或写真实 attoDRY；真实只读、toggle、运动和
归零分别等我明确授权。LK_setup 上只能使用 lyr。结束时按模块交付格式报告。
```

