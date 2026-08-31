# Three-SMU / dual-gate 模块

## 状态与范围

状态：`target offline complete`；当前 bottom-only active 计划已完成一次有界
`read-only commissioned` 验收（2026-09-01 更新）。模块最多控制三台 Keithley 2400，语义角色为
`smu_bias`、`gate_top`、`gate_bottom`。它提供统一 TOML、无 GUI CLI、调用同一
`ThreeSmuSession` generator 的实时 Notebook、query-only 终端监控和 accepted-only 分析。

第一版不连接、不读取、不记录 Lock-in，也不控制冷台或磁场。除当前 bottom-only 计划的一次明确
授权 query-only 验收外，其余角色的真实 VISA 验收、状态队列消费和任何设置写入均未完成。

`[three_smu_run.<role>].role` 是启用角色的唯一事实来源。`fixed`/`sweep` 角色
必须有完整的同名硬件表，并且只有这些 active 角色会被验证、打开、读写、
监控、cleanup 和记录。`off` 角色的硬件表可缺省且完全不触碰；其物理状态始终
为未知，不能推断已归零或 output-off。

## 单一配置契约

日常唯一入口是 ignored 的 `config/hardware.local.toml`，模板为
`config/hardware.example.toml`。旧的 `three_smu_hardware.example.toml`、
`three_smu_scan.example.toml` 及 CLI `--hardware/--plan/--output-dir` 已删除。

每台 SMU 只保留两条用户确认的安全边界：

- `max_abs_voltage_v`
- `max_abs_current_a`

程序在任何 source 写入前用当前 `source_mode` 对照对应边界，并对每次实际 V/I 读回同时
检查两条边界。已删除独立 source min/max、软件 ramp、readback tolerance、settle、leakage
limit 和用户填写的 compliance 字段。

Keithley 2400 的 compliance 是真实硬件保护，不等于量程：voltage-source 自动把
`max_abs_current_a` 写为 current compliance；current-source 自动把
`max_abs_voltage_v` 写为 voltage compliance。配置后查询 compliance、source range 和
measurement range；compliance 读回高于对应 `max_abs_*` 时 fail closed。source 和
measurement autorange 当前必须为 `true`，避免未配置的固定档位成为隐藏状态。配置还检查
标准 2400 的 210 V、1.05 A、约 22.05 W 工作包络及最小可编程 compliance。

`nplc` 保留并默认 `1.0`。芬兰电网 50 Hz，因此 1 PLC 对应 20 ms；NPLC 是周期数，不写成
`0.020`。

`smu_bias`、`gate_top`、`gate_bottom` 硬件参数均直接写在同名单表中，不再使用
`[gate_top.smu]`/`[gate_bottom.smu]`。Three-SMU VISA timeout 在代码中固定为
`5000 ms`，TOML 中的 `timeout_ms` 会被 strict loader 拒绝。

## 扫描计划

每个角色独立选择：

- `role = "off"`：不扫描也不解析其已知扫描值；暂存的 `fixed`/`points`/
  `ranges`/`start`/`stop`/`step`/`bidirectional` 可保留，但推荐删除以便下次启用时显式配置；
- `role = "fixed"`：填写 `fixed`，且 `bidirectional = false`；
- `role = "sweep"`：填写 `points = [...]` 或 `ranges = [...]`，二选一；旧的 active
  `start/stop/step` 顶层写法会被拒绝。

`points` 非空、有限，保留任意顺序、重复值和非单调序列。`ranges` 是按顺序排列的 inline
table 数组：每段包含 `min`、`max`、`scale`；`linear` 段再从 `step`/`points` 中选一个，
`log` 段必须使用 `points` 且两个端点为正。每段包含端点，要求 `max > min`；多段严格按
TOML 顺序拼接，并保留相邻段重复的共同端点。loader 随即把 ranges 展开为同一底层点向量。
`bidirectional = true` 在完整向量展开后追加反向路径；例如 `[1, 3, 7, 2]` 展开为
`[1, 3, 7, 2, 7, 3, 1]`，转折点不重复。off 只忽略上述已知字段的值；拼错的未知字段仍由
strict loader 拒绝。

- 一维扫描只展开该角色；
- `paired_gate` 分别展开两个 gate，最终长度不同则配置错误；
- `multi_smu_map` 分别展开各 sweep 角色，再做笛卡尔积；
- `software_pulse` 要求恰好两个 sweep 值并拒绝 bidirectional；
- `time_trace` 不允许 sweep。

支持七种模式：`time_trace`、`bias_iv`、`top_gate_transfer`、
`bottom_gate_transfer`、`paired_gate`、`multi_smu_map`、`software_pulse`。

## 写入、读回与清理

获得真实运行授权后，顺序为：离线验证全部点 → 仅打开 active 资源 → query-only preflight →
直接设零/配置 → output enable → 每个正式点对每台 active SMU 只写一次目标 → 全局
`delay_s` → 读取并记录 source setpoint、V、I、R、output、trip 和 status。软件不插入 ramp
中间点，也不因 requested/readback 数值差异本身拒绝数据；实际 V/I 越界、compliance trip、
output 状态错误、非有限值或错误队列异常仍会拒绝。

正常、异常、Ctrl+C 与 generator 提前关闭共享 cleanup：每台 active SMU 直接写 0，等待 `delay_s`，读取并
记录，再关闭 output 并读回。通信失败不证明零或 output-off；保留最后确认状态并要求人工检查。

`finish_action = "hold"` 仍需要独立的 `HOLD OUTPUTS` 终端确认。

## 接口与记录

```powershell
python -m attodry_control.three_smu_cli describe
python -m attodry_control.three_smu_cli monitor-live
python -m attodry_control.three_smu_cli run
```

`describe` 完全离线。`monitor-live` 只有获得真实查询授权后才能使用；默认不消费
`:SYST:ERR?`，`--consume-status-queue` 仍需单独授权。monitor 仅在仪器 output 已经 ON 时发送
`:READ?`；output OFF 时保持 OFF 并把 V/I/R 显示为 `n/a`。`run` 无需长授权参数，但会在打开硬件前
要求精确输入 `RUN THREE SMU`。

每个 run 保存 schema v5 `metadata.json`、`raw.jsonl` 和 `data.csv`。schema v5 保留 v4
的 direct-point 契约，并新增 `active_roles`/`off_roles`；硬件快照只包含 active 角色。
CSV 保留稳定列，off 角色字段为空。配置快照只含两条绝对边界，并记录 configure 后的 compliance/range
读回事件。requested source 与实际 setpoint/V/I 分开保存。默认分析只加载
`completed + accepted + clean` formal samples；rejected/problem 需要显式 opt-in。

本次 monitor 修复的 23 项 Keithley/live/CLI 聚焦测试通过；完整离线回归 402 项通过，
`src/tests` compileall 通过。当前仅启用 `gate_bottom` 的一次真实 query-only 样本通过：确认 output
OFF、0 V setpoint、compliance/range/sense/trip，未发送 `:READ?`、未消费 error queue，也未发送
设置写命令。

完整操作者说明见 [`../THREE_SMU_DAILY_OPERATION.md`](../THREE_SMU_DAILY_OPERATION.md)，实时
监控边界见 [`../THREE_SMU_LIVE_MONITOR.md`](../THREE_SMU_LIVE_MONITOR.md)。

## 下一阶段

下一步是在分别授权下完成其他计划角色的 M4 real read-only 验收；状态队列消费和最小写入仍需各自
的新授权。第一次写入前仍须人工确认本次 active SMU 的实际地址/identity、接线、2/4-wire、guard/ground/common、
每台 source mode、两条绝对边界、容性负载/互锁和 output-off 语义。
