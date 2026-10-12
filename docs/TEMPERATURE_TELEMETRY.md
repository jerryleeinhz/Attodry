# 温度与加热功率记录

## 四个通道

2026-10-12 起，`temperature_run`、`temperature_scan`、
`temperature_excitation_scan` 和选择 temperature 的 combination 记录：

| 字段 | 含义 | 单位 |
| --- | --- | --- |
| `state.sample_temperature_k` | 样品温度读回 | K |
| `state.vti_temperature_k` | VTI 温度读回 | K |
| `heater_power.sample_w` | 样品加热功率读回 | W |
| `heater_power.vti_w` | VTI 加热功率读回 | W |

不需要增加 TOML 参数。目标、温度稳定判据、过冲保护、PID、激励参数及正常/异常
结束策略沿用原配置。这里只增加观测，不能用功率轨迹替代温度保护。

## 顺序、时间戳及失败

采集进程复用自己的 DLL 连接，依次读取完整状态、保存原始温度事件、执行原有安全
检查，再顺序读取样品和 VTI 功率。不会另开一个连接或后台硬件轮询线程。
稳定等待、恢复检查、正式测量前后和最终快照都记录功率。仅磁场扫描不增加功率查询。

原始事件及其 `temperature_telemetry` 补全事件使用相同 `sample_id`。原始事件先落盘，
所以过冲、漂移或设备错误导致拒绝时，触发温度仍可审计，功率保持 `null`。
读取功率失败也会保存补全事件并按原有故障路径退出；若样品功率已成功而 VTI 失败，
前者保留，后者为空。不会把缺失功率写成零或沿用上一点。
故障诊断功率在确认温控已禁用后才读取，触发状态和清理后的功率对应状态分别保存。

`state_read_timing`、`heater_power_read_timing` 和 `heater_power_reads` 保存主机查询
起止的 Unix/monotonic 时间。正式窗口原有 `captured_monotonic_s` 仍对应温度观测。
这些时间描述顺序查询，**不是仪器固件的采样时间，也不表示四个通道同时采样**。
额外查询及落盘需要时间，因此实际轮询间隔可能长于配置的 sleep 间隔；测量等待和
稳定窗口继续使用实际经过时间。

## 文件及纯文件监控

- 温度扫描和温度—激励扫描沿用原进度 JSONL/摘要文件。
- 日常 `temperature_run` 新增 `*_temperature_run_progress.jsonl`，最终输出包含
  `progress_jsonl` 路径。优先使用 `[temperature_scan].output_directory`；没有该表时
  使用配置目录上一级的 `run_data/temperature_commissioning`。
- combination 写入原 `scan.sqlite` 的事件表，并在正式环境窗口/清理记录保留补全快照。
- `temperature_progress_monitor` 只读文件，显示两温度、两功率及最后记录状态。
  新温度事件到来时先清空旧功率，直到同一样本的补全事件到达。

```powershell
python -m attodry_control.temperature_progress_monitor `
  --progress "run_data\temperature_commissioning\<name>_temperature_run_progress.jsonl"
```

## 导出与绘图（无硬件通信）

对 JSONL 文件：

```powershell
python -m attodry_control.temperature_trace_analysis `
  --progress "run_data\temperature_commissioning\<name>_temperature_scan_progress.jsonl" `
  --output-directory "analysis\temperature_trace_01" --plot
```

对 combination 数据库，必须明确指定 run ID：

```powershell
python -m attodry_control.temperature_trace_analysis `
  --database "run_data\<scan>\scan.sqlite" --run-id "<exact-run-id>" `
  --output-directory "analysis\temperature_trace_02" --plot
```

输出 CSV、清单，以及 `--plot` 启用时的 PNG/PDF。数据库以 SQLite `mode=ro` 和
`query_only=ON` 读取；可以对运行中的文件取一致性快照，JSONL 尾部未完成的一行留待
下次读取。导出目录应与原始文件目录分开，已有导出不覆盖。

图中温度和功率共用时间轴，分别使用各自主机查询时间；显示目标/设定读回、已记录的
target 模式容差及可用的设定/稳定/错误/温控事件。缺失功率显示断点，不插值、不平滑。
CSV 保留所有原始观测，包括拒绝与不完整记录，并附原始事件 JSON；它是诊断轨迹，
不能把其中所有观测当成通过验收的正式电学数据。

旧文件可读取，但原来没有记录的连续功率无法补回；空白代表未记录。
绘图需安装项目的 analysis 依赖。
