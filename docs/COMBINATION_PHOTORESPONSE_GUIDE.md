# Combination 光电响应

本分支采用 Integration-photonics 的运行、监控和绘图习惯，保留 DC 光电响应采集内核。

## 日常使用

在 NKT 项目根目录、lyr 环境中执行：

```powershell
python -m attodry_control.combination_cli run
# 另一终端，只读监控
python -m attodry_control.combination_cli monitor
```

默认读取 config/photoresponse_trial/11_nkt_630nm_dark_50-300uW_7pt_2182.local.toml。
文件名不代表当前参数；启动摘要和归档快照描述实际配置。切换实验使用 --config。
旧 photoresponse_cli run 调用同一服务，仍兼容旧参数。执行 run 即确认选中模块和光路；
内部限值、身份、接线/隔离、读回、状态与清理保持。部署及离线测试不包含实机启动授权。

启动显示配置路径/SHA-256、模块、轴顺序、条件数、波长/带宽、功率目标、探测通道/量程、
run_id 和数据库；过程显示阶段、功率、采样与进度，主错误和清理错误分别报告。
--json 关闭人类进度并输出最终结构化记录。

```powershell
python -m attodry_control.combination_cli describe-hardware
python -m attodry_control.combination_cli simulate --config config/photoresponse_dark_nkt_simulation.toml
python -m attodry_control.combination_cli monitor --once
python -m attodry_control.combination_cli monitor --database <audit.sqlite> --run-id <id> --once --json
```

流程保持：外层温磁场进入点判稳 → 光学点 → 本轮首个暗点 → 亮功率点稳定 →
核对电学设置 → 激励/采集。亮点扣除本轮相同电学坐标的暗态；回访、重复、新尝试
重新取得暗态和稳定性证据。历史三窗口配置继续使用原插值规则。

## 文件监控

项目 run_data/latest-combination-run.json 原子登记已创建运行，支持嵌套 TOML 路径。
monitor 选定 run_id 后固定跟踪它。旧运行可由配置数据库或显式参数定位。
监控只读 SQLite/WAL，不导入采集器或仪器驱动，不额外查询仪器。新运行逐事件维护
小型 photo_progress；旧库读取有限事件尾部和最终记录，不迁移旧数据或重放全部日志。

显示已采集/总点数、最终接受数、坐标、阶段耗时、事件年龄、最近记录的读数和清理。
无事件时定期刷新年龄。active 仅为归档状态，进程存活为 unknown；读数不是此刻实机状态。
整轮完成且清理通过后才接受数据，运行中 accepted=0 并不表示没有采集。

CLI run/simulate 始终创建审计数据库；没有 photoresponse_storage 配置时使用输出目录的
audit.sqlite。显式数据库、原配置快照与新尝试恢复语义保留；恢复仍要求显式数据库配置
和完全匹配的计划。原始 JSON/JSONL/SQLite 保留。

## Unified plotting

打开 notebooks/unified_plotting.ipynb，运行初始化与 dashboard。
Data directory 推荐实验输出子目录。Refresh records → 勾选数据源 → SQLite 选择 Run ID
→ Load / refresh runs。同一轮选择 JSON 或 SQLite 中的一种，避免重复观测。

| 用途 | 字段 |
|---|---|
| 功率 X | actual.optical_power_w，PM100D 标定测量面 W |
| 光生量 Y | measured.delta_voltage_v 或 measured.delta_current_a |
| 亮点筛选 | requested.optical_state = light |
| 暗/亮值 | measured.dark_voltage_v / measured.light_voltage_v，电流对应 _current_a |
| 波长/带宽 | requested.optical_wavelength_nm / requested.optical_bandwidth_nm |
| 激励 | requested.bias_current_a / requested.bias_voltage_v 与 actual 设置读回 |
| 环境/栅极 | requested/actual 温磁场、带单位栅坐标及独立漏电 |

多波长、偏置或栅极变化时使用筛选/分组，保留未解决维度检查。6221 读回是源设定，
不是实测样品电流；无激励不制造电流值。measurement_plane 保留测量位置，不自动给出
未经样品功率标定的 A/W、V/W。

每行是条件均值和已记录差分，共享暗态不作为独立重复复制；保存暗点 ID、baseline_age_s
及公式版本，不跨 run/attempt 拼接。Mean ± SD/SEM 对单行结果为 n=1，误差未定义。
detector_sample_count 和 detector_within_window_sd 单独表示窗口内采样与散布。

默认沿用整轮/清理接受规则。失败或运行中数据需显式勾选 Include rejected/problem records；
图及导出标记 AUDIT，无效窗口继续受 Quality 策略控制。缺失差分保持空值与错误；
+9.9E37 保留原始审计文本，不画成电压。导出 PNG/PDF/SVG、选中数据、统计与来源 manifest；
仿真标记 SIMULATED。Photoresponse_Analysis.ipynb 保留为简易兼容入口。

## 复用和验收

来源 integration-photonics-nonlinear-hall：39e7e9fb1881a269d77dc7df2e823691ae33e719，
与核对时 LK_setup Integration-photonics HEAD 一致。复用 scientific_plotting、analysis_observations、
unified_plotting 及原有 31 项测试，以及 combination_store/analysis/registry/terminal 的公共接口；
增加 DC schema 分派、窄监控/绘图适配、嵌套配置定位及 DC 终端视图。
公共包导出按需加载，文件工具不导入驱动。参考分支的硬件编排未移植；DC 保护、清理及
3 T 不变量仍使用本分支实现。

验收覆盖命令兼容、WAL 共存、历史库、失败/清理、登记、驱动导入隔离、差分/单位/无效值、
交互选择、统计与导出。离线通过不等于实机验收。目标机隔离验证后再快进部署，
保留本地配置；活动扫描或不明占用时不更新活动 checkout。
