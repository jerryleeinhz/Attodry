# Temperature module 运行指南

## 结论

Temperature 已经是独立模块。日常使用者不需要阅读或修改 Python 文件，只需要：

1. 在 `config/hardware.local.toml` 的 `[temperature_run]` 中修改目标温度；
2. 确认 attoDRY GUI 已经 Disconnect；
3. 运行一条命令；
4. 使用输出中的实际 `sample_temperature_k`，而不是把 setpoint 当成实际温度。

日常命令没有额外的授权参数。执行 `attodry-temperature-run` 本身就会连接
attoDRY、开启 Full Temperature Control，并写入配置的样品温度目标。

## 模块结构

文件较多是因为硬件通信、安全流程、日常运行、诊断和数据保存分别负责不同工作。
日常使用不需要逐个运行它们。

| 文件 | 作用 | 日常是否修改 |
| --- | --- | --- |
| `config/hardware.local.toml` | 硬件地址和所有日常温控参数 | 是，通常只改 `target_k` |
| `src/attodry_control/temperature_run.py` | 日常温控运行入口 | 否 |
| `src/attodry_control/attodry.py` | attoDRY DLL 驱动和读回检查 | 否 |
| `src/attodry_control/temperature_test.py` | commissioning/严格稳定性诊断 | 否 |
| `src/attodry_control/acquisition.py`、`storage.py` | 测量时保存实际温度 | 否 |

`temperature_test.py` 的许多直接参数是开发和诊断接口，不是推荐的日常运行方式。

## 一次性配置

如果控制电脑还没有本地配置，先复制模板：

```powershell
Copy-Item config\hardware.example.toml config\hardware.local.toml
notepad config\hardware.local.toml
```

`hardware.local.toml` 已被 Git 忽略，其中可以保存本机 DLL、COM 端口和设备地址。
不要提交该文件。

日常温控参数全部位于同一个表：

```toml
# Daily temperature operation. Normally only target_k needs to change.
[temperature_run]
target_k = 1.8
max_delta_k = 250.0
max_overshoot_k = 0.2
pre_measure_wait_s = 1800.0
poll_interval_s = 1.0
```

参数含义：

| 参数 | 含义 |
| --- | --- |
| `target_k` | 本次样品温度 setpoint，日常通常只修改这一项 |
| `max_delta_k` | 相对连接时实际样品温度允许的最大目标变化；当前按操作者决定为 250 K |
| `max_overshoot_k` | 实际样品温度允许超过目标的最大值；commissioned 值为 0.2 K |
| `pre_measure_wait_s` | 写入目标后持续监测多久才允许进入测量；当前为 1800 s |
| `poll_interval_s` | 完整状态读取和实际温度记录间隔；当前为 1 s |

例如 `target_k = 1.8` 时，实际样品温度达到或超过 2.0 K 会触发失败清理。

## 日常运行

开始前：

- 确认 attoDRY GUI 显示设备正常，没有告警；
- 确认 GUI 已经 Disconnect，避免 DLL/COM 资源被占用；
- 在项目根目录运行命令；
- 控制电脑使用 `lyr` 环境。

安装项目命令后运行：

```powershell
attodry-temperature-run
```

或者直接使用控制电脑解释器：

```powershell
C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe `
  -m attodry_control.temperature_run `
  --config config\hardware.local.toml
```

如果当前目录不是项目根目录，必须给 `--config` 提供正确路径。默认路径就是
`config/hardware.local.toml`。

## 程序实际执行顺序

1. 严格解析整个 `hardware.local.toml`，未知或缺失字段直接停止；
2. 加载 vendor DLL，连接并读取完整初始状态；
3. 用初始 `sample_temperature_k` 检查 `max_delta_k`；
4. 先开启并确认 Full Temperature Control；
5. 再写入并确认 `target_k`；
6. 每隔 `poll_interval_s` 记录完整状态；
7. 连续监测 `pre_measure_wait_s`；
8. 时间达到后将当时的实际状态写入 `measurement_state`，并标记
   `measurement_ready = true`；
9. 保持目标温度和温控开启，只断开 Python 对 DLL 的连接。

进入测量不要求实际温度在30分钟内严格等于 setpoint。测量数据必须保存当时的
`sample_temperature_k`；现有 acquisition/storage 路径已经同时保存：

- `sample_temperature_k`：实际样品传感器温度；
- `user_temperature_k`：用户 setpoint；
- `vti_temperature_k`：VTI 温度。

## 正常输出

命令结束时输出 JSON。关键字段：

```text
completed = true
measurement_ready = true
measurement_state.sample_temperature_k = 实际样品温度
measurement_state.user_temperature_k = setpoint
temperature_samples = 30分钟内的完整温度记录
disconnected = true
```

`disconnected = true` 只表示 Python 已正常断开 DLL/COM；正常完成后 Full
Temperature Control 和目标温度保持开启。

如需单独保留这次温控 JSON，可以使用：

```powershell
attodry-temperature-run |
  Tee-Object -FilePath run_data\temperature_run.json
```

正式测量的实际温度仍由 SQLite acquisition/storage 记录，不能只依赖 setpoint。

## 失败和安全行为

以下情况不会标记 measurement-ready：

- 实际温度达到 `target_k + max_overshoot_k`；
- 温控标志在监测期间关闭；
- setpoint 发生变化；
- attoDRY 返回非零错误码；
- DLL/COM 读取失败；
- 用户按下 `Ctrl+C`。

如果写入阶段已经开始，程序会尝试幂等关闭 Full Temperature Control，读取最终状态，
记录 heater power 和触发状态，然后 Disconnect/end。通信或清理读回失败时，程序不会
声称设备已经安全，必须人工检查 GUI。

## Commissioning 诊断工具

`attodry-temperature-test` 仍然保留，用于严格 tolerance/range/dwell 诊断和历史
commissioning 复现。它不是日常测量入口，仍使用独立 commissioning 参数及其原有
诊断授权门。日常运行只使用 `attodry-temperature-run` 和
`hardware.local.toml`。
