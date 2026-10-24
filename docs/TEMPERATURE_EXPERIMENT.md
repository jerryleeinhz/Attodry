# 独立温度优化试验

`python -m attodry_control.temperature_experiment`是显式启用的独立温度试验入口。
仅使用一个attoDRY会话，不构造光学、锁相、SMU或旋转器驱动。
原`temperature_scan`及组合扫描入口没有改动。

## 使用方式

先准备独立站点TOML和JSON试验计划。JSON固定站点文件与运行代码SHA256、
绝对截止时间、逐点科学目标、到稳预算和诊断保持时间。
站点TOML仅保留project、cryostat、magnet、temperature_stability和temperature_run。
私有地址、TOML及原始试验数据不提交Git。

```powershell
# 只解析、核验哈希和展示计划，不加载硬件DLL。
python -m attodry_control.temperature_experiment --plan <plan.json> --preview

# 本次获准的只读冷台检查，不设温、不切换温控。
python -m attodry_control.temperature_experiment --plan <readiness-plan.json> `
  --readiness --authorize-temperature-experiment

# 获准的独立温度试验。
python -m attodry_control.temperature_experiment --plan <plan.json> `
  --authorize-temperature-experiment
```

同一个输出目录只允许启动一次；events.jsonl以独占创建方式打开，不能覆盖或
复用旧run。不自动resume。启动前必须核验全部数据库无active、采集进程退出、
Jupyter闲置及前次清理证据。入口自身再独占所有现存站点owner别名，核验哈希。

## 计划字段

顶层必须有run_id、root、station_config、station_sha256、output_directory、
deadline_utc、code_hashes、points。可选poll_interval_s默认1.5秒，
heater_interval_s默认5秒（null关闭），cleanup_reserve_s默认600秒。

每点必须有label、target_k；可选method、max_wait_s、hold_s、commands。

| method | 行为 |
|---|---|
| fixed（默认） | 直接写最终科学目标，执行原标准判稳 |
| shadow | 与fixed相同的写入和等待；另外记录近期斜率，不据此控制 |
| sequence | 执行有界、已验证的临时目标序列，最后恢复科学目标，再完整判稳 |

sequence还需要匹配该组具体试验点的candidate_evidence路径及SHA，且证据必须
明确independent_validation_passed=true。未验证模型不能仅因程序支持sequence
就用于真实写入。shadow的斜率也不是经过验证的稳定时间预测。

commands包含after_s与target_k；第一项时间0，最多7项（含最终恢复），
相邻计划时间至少30秒，实际确认后也至少等待30秒再写下一项。
所有临时目标均在本次起始实际温度与最终科学目标之间；最后一项必须是最终目标。
ACK及策略执行计入同一个max_wait_s，不能为各台阶重置超时。

小步预算3600秒；本次获准的20/40 K大步可预先设5400秒。
hold_s为判稳后的诊断观察，0–600秒。到稳标准固定为target±0.10 K、
连续120秒、峰峰差≤0.05 K；恢复最终目标确认之前的样本不得借给该窗口。
首次进入目标带单独记为first_target_arrival，不能当作point_qualified。

## 保护与结束

保留站点绝对温度范围及max_delta/max_overshoot；本次入口不允许范围扩大到
1.6–300 K之外、max_delta超过260 K或overshoot超过15 K。
科学目标限定1.7–251.7 K。实际和设定合场均严格≤3 T；未知读回不推断零场。
场设定/控制被外部修改、温控关闭、温度设定被外部修改、通信/非有限读数、
审计错误和截止时间均阻止继续。入口不写磁场，也不改厂商PID或直接heater输出。

每点仅在剩余预算足够容纳其到稳上限、hold及清理预留时启动。
运行中的取消标记为输出目录下`stop-request`文件，由执行器正常处理并清理；
禁止强杀。正常结束保持最后目标并关闭会话；失败/中断在已尝试温度修改时
独立尝试禁用温控、读取最终状态及关闭连接。清理错误与主错误分别归档。
禁用温控可能继续降温，不代表维持温度。没有自动故障重启。

## 记录与监控

- events.jsonl：run/访问上下文、每次完整cryostat_state、heater功率、
  命令尝试/确认、首次到达、判稳、保持及终态。
- state.json：小型当前阶段快照或终态；原始样本仍在events中。
- state_captured_at_utc：读回采集时间；写快照的时间不能冒充仪器采集时间。

活动state.json必须用短时.NET FileStream、FileShare.ReadWrite|Delete读取后
立即Dispose；不能SCP活动state或长持有。监控只读文件/CIM，不开第二仪器连接。
Windows短暂拒绝快照替换（错误5/32/33）时，仅重试已经关闭并fsync的同一
pending文件，最多5次替换尝试，额外请求等待合计最多0.37秒；成功恢复或重试
耗尽均记入events。不会重新查询仪器或重发温度命令。文件操作耗时仍计入原
总预算，快照后再次检查截止时间。持续拒绝、其他文件/审计错误仍失败清理；
这不是实验自动重启，也不豁免故障后的人工审阅。
终态汇总的primary_error、cleanup、final_state及其采集时间应一起解读；
没有新读回时，保留最后已存字段和错误，不能称当前实时状态。

英文终端示例：

```text
Temperature point: 2 | Phase: qualification | Latest temperature: 86.513000 K | Target: 86.7 K | Elapsed: 600.0 s
Temperature point: 2 | Target: 86.7 K | First arrival: 370.0 s | Qualified: 3159.0 s | Hold range: 0.041000 K
```

该示例只解释格式，不是本次实测。判稳后持续5–10分钟的变化与首次到达、
完整到稳和整条多点扫描耗时分别报告。温度试验结果不能直接认证有光负载性能。
