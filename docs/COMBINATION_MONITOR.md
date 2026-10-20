# Combination monitor: progress and phase

`monitor` 的终端字段使用英文。监控只读 SQLite，使用所选 run 归档的点表，
不连接仪器，也不根据后来编辑的 TOML 重算旧扫描。

```powershell
python -m attodry_control.combination_cli monitor --config config/three-lockin.local/photonics-cld.local.toml
```

也可使用 `--database PATH --run-id ID` 明确选择记录；`--once` 查看一次，
`--json` 返回同一快照的结构化字段。启动后始终跟踪同一 run。

## 英文字段及计数含义

以下为运行示意，并非当前实验状态：

```text
Status: active | Accepted conditions: 4/303 | Process liveness: unknown
Temperature point: 2/101 | Fully completed in scope: 1/101 | target 4.2 K
Optical point: 2/3 | Fully completed in scope: 1/3 | power target: 30 uW
Point scope: temperature point 2
Formal samples in attempt: 2/3 | started (provisional until accepted)
Current phase (last recorded): lockin -> Formal sampling | sample 2
Phase started: ... UTC | Elapsed: ...
Last database event: ... UTC | Age: ...
```

| 字段 | 含义 |
|---|---|
| `Temperature point: 2/101` | 本次进入第 2 个温度点，不代表已经完成两个点 |
| `Fully completed in scope: 1/101` | 当前重复轮次／外层点下，内层所有条件均 accepted 且正式样本齐全的温度点数 |
| `Optical point: 2/3` | 当前温度下第 2 个光学点；点可同时包含波长、起始电流和功率目标 |
| `Point scope` | 该轴计数所属的外层轴索引，索引从 1 开始显示 |
| `Accepted conditions` | 全部重复轮次的 accepted 组合条件总数，与温度点数分别显示 |
| `Formal samples in attempt` | 当前尝试已存的正式样本；started 时仍可能最终被拒绝，不作为 accepted 数据 |
| `Repeat` | 配置有多次重复时显示当前轮次；尚无条件开始时显示 `-` |
| `Current phase (last recorded)` | 采集器最近写入的阶段开始标记；普通 raw／timing 事件不覆盖它 |
| `Phase started / Elapsed` | 最近阶段标记时间及到本次显示时的经过时间 |
| `Last database event / Age` | 最近任意事件的时间及记录年龄，不是进程心跳或仪器实时查询 |

重复的相同温度值仍按轴索引分别计数；内外层顺序改变时，按对应的重复轮次和
外层索引计算。跳过、被拒绝和未完成的条件不能计为完整测完的温度点。
没有选温度轴时不显示 Temperature point。增加第四个功率点后，点表自动显示
4 个光学点；101 温度 × 4 功率 × 1 锁相设置对应 404 条件。

## 阶段

阶段标记在开始相应操作前写入，可能显示：

| Phase label | 含义 |
|---|---|
| `Startup checks` | 所选模块的启动预检 |
| `Applying axis target` | 应用当前轴目标 |
| `Axis qualification` | 轴进入判稳／资格核验，包括温度判稳 |
| `Dark acquisition` | 暗背景采集 |
| `Optical power qualification` | 光源／功率初始资格核验 |
| `Lock-in settling / sampling interval` | 既有锁相滤波等待或采样间隔；结束后恢复暗态／正式采样阶段 |
| `Formal sampling` | 正式采样窗口，module 标明当前模块 |
| `Condition complete` | 当前组合条件已接受 |
| `Cleanup` | 模块清理；后续终态覆盖活动阶段 |

中断／失败／完成后，`Current phase` 显示 `interrupted`／`failed`／`completed`
及 `(terminal)`，旧阶段只显示为 `Last recorded phase`。阶段标记不是关光证明，
也不提供安全调光路窗口。清理证据和人工确认仍分别保留。

旧 run 没有 `scan_phase` 标记时，显示 `unavailable (no explicit phase record)`；
保留 Last event，不推断当前正在执行的阶段。`Process liveness` 仍为 `unknown`，
长时间没有数据库更新不能单独证明进程已退出。显示年龄仅在快照变化或 `--once`
输出时计算；每秒数据库轮询不会为了刷新计时而不断打印同一状态。

## 性能及兼容性

新增仪器查询、设置写入、等待均为 0；锁相等待、温度判稳、重试和清理规则沿用
原流程。采集器新增少量 `scan_phase` SQLite 事件，产生实际磁盘写入成本；
不在每次仪器查询上添加事件。monitor 在同一只读事务内汇总归档条件、正式样本
计数和阶段，无需新表或历史数据迁移，也不加载全部 sample payload。

本地 404 条件的合成 SQLite 对照：40 次快照的中位耗时从约 1.60 ms 到 6.07 ms。
这是该离线样例的监控开销，不是实机测量耗时保证。硬件阻断回归同时验证阶段
事件不改变 fake 仪器 query/write 列表、既有等待时间，以及故障后的独立清理。
