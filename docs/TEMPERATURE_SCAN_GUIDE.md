# Temperature stability scan 运行指南

## 当前边界

`attodry-temperature-scan` 是独立的 Temperature commissioning 模块，用于按
设定点逐步升温，并在实际样品温度稳定后记录可用于测量的系统读回值。目标 setpoint
仍会写入并审计，但 `stable-readback` 模式不要求样品精确命中 setpoint。

当前实现已经完成真实 commissioning：`LK_setup` 使用 commit `cba448b` 和 `lyr`
环境执行了 1.7 K 到 3.7 K、步长 0.2 K 的 11 点扫描。运行结果为
`outcome=completed`，全部点均记录稳定读回，最终 setpoint 为 3.7 K、样品读回为
3.569 K，Full Temperature Control 保持开启且错误码为 0。后续每次真实连接和写入
仍须由操作者明确授权，并在启动前核对 ignored 的 `hardware.local.toml`、DLL 路径和
COM 端口占用。

## 单一配置入口

所有日常值保存在 ignored 的 `config/hardware.local.toml`。扫描表只保存网格、记录
名称和输出目录：

```toml
[temperature_scan]
start_k = 1.7
stop_k = 2.7
step_k = 0.1
run_name = "temperature_1p7_to_2p7"
note = "First stepwise temperature-stability timing scan."
output_directory = "../run_data/temperature_commissioning"
```

稳定定义不在扫描表中重复，继续使用 `[temperature_stability]`：

```toml
acceptance_mode = "stable-readback"
stable_range_k = 0.05
stable_dwell_s = 30.0
poll_interval_s = 1.5
wait_timeout_s = 1800.0
min_response_k = 0.02
```

`stable-readback` 只要求稳定窗口内样品读回的 peak-to-peak 范围满足
`stable_range_k`；窗口成立后，程序把窗口平均值写入 `measurement_temperature_k`。
除第一个点外，`min_response_k` 要求实际温度相对该点开始时至少发生一次可见移动，
避免同一个平台被重复记录成多个扫描点。若使用旧的 `target` 模式，则必须另外提供
`tolerance_k`，并要求窗口内每个样品读数都落在目标容差内。

安全和中断继续使用 `[temperature_run]` 中已有的 `max_delta_k`、
`max_overshoot_k`、`interrupt_policy` 和 `resume_recheck_s`。
`pre_measure_wait_s` 与 `[temperature_run].target_k` 不参与稳定扫描；它们仍只服务
原有单点日常运行。这样网格、稳定判据和安全事实各有一个来源。

当前命令只接受升温网格。降温时同一个正向过冲判据含义不同，因此在另行确认降温
安全和验收规则前不会把负步长默认为安全。

## 离线检查（不会加载 DLL）

在项目根目录、`lyr` 环境中先确认 checkout：

```powershell
git status --short --branch
git log -1 --oneline
C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe -c `
  "import attodry_control; print(attodry_control.__file__)"
```

检查配置和网格：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe -c `
  "from attodry_control.config import load_temperature_operation_config as load; from attodry_control.scans import temperature_scan_points; c=load('config/hardware.local.toml'); s=c.temperature_scan; print(s); print(temperature_scan_points(s.start_k,s.stop_k,s.step_k))"
C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe -m `
  attodry_control.temperature_scan --help
```

不要在 target-offline 阶段添加 `--authorize-temperature-scan`。缺少该 flag 时，命令
在加载 DLL、创建连接或写 setpoint 前拒绝。

## 经单独授权后的真实命令

确认 GUI 已 Disconnect、没有其他进程占用 attoDRY，并确认本次允许完整网格后运行：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe -m `
  attodry_control.temperature_scan `
  --config config\hardware.local.toml `
  --authorize-temperature-scan
```

程序对每个点执行：读取完整状态，检查样品温度移动，确认 Full Temperature Control
开启，确认 setpoint，随后从 setpoint 确认时刻开始对实际样品读回计时。正常完成后
保持最后一个目标和温控开启，只断开 Python 的 DLL/COM 连接。

## 记录内容

输出目录中生成三份同名前缀文件：

- `*_progress.jsonl`：每个状态样本、转换、中断和完成点实时追加并 flush；
- `*_summary.json`：最终 completed/rejected/interrupted 摘要；
- `*_stable_times.csv`：每个已稳定点一行，便于直接查看耗时。

每点分别记录请求 setpoint、DLL 实际 setpoint 读回、最终样品读回、
`measurement_temperature_k`、`time_to_response_s`、`time_to_stable_s`，以及稳定窗口的
minimum、maximum、mean、standard deviation、peak-to-peak 和样本数。`target` 模式
额外记录 `time_to_first_tolerance_s`；稳定读回模式不把 setpoint 误当成测量温度。
记录还包含 resolved 配置、run name/note、Git commit、错误、cleanup 和最后确认状态。

通信失败不代表温控已关闭。若最终读回或 close 失败，记录只保存最后确认状态并要求
人工查看 GUI，不会写成安全完成。

## 中断和恢复

`abort` 或硬故障会保留当前点的所有原始样本，尝试关闭 Full Temperature Control，
然后退出。`continue` 和 `wait-confirmation` 只有在 setpoint、控制、错误码和过冲判据
仍全部确认安全时才继续；继续后当前点的稳定窗口从头计时，不拼接中断前的 dwell。

进程退出后，可用同一 TOML 和进度文件从第一个未完成点继续：

```powershell
C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe -m `
  attodry_control.temperature_scan `
  --config config\hardware.local.toml `
  --authorize-temperature-scan `
  --resume-progress run_data\temperature_commissioning\<run>_progress.jsonl
```

恢复前会逐字段比较当前 TOML 和进度文件归档的扫描契约。已完成点保留；中断的部分点
会重新确认控制和 setpoint，并从新的稳定窗口开始。已完成的进度文件不能再次恢复。

## 停止条件

以下任一情况立即拒绝当前扫描并进入失败清理：

- 实际样品温度达到当前点 `target + max_overshoot_k`；
- Full Temperature Control 关闭或 setpoint 改变；
- attoDRY 返回非零错误码；
- DLL/COM 读取、写入、readback 或记录持久化失败；
- 当前样品温度移动超过 `max_delta_k`；
- 当前点超过 `temperature_stability.wait_timeout_s` 仍未稳定；
- `Ctrl+C` 的有效策略最终选择中止。

真实升温耗时由样品、热接触和控制器决定。脚本不会自动修改 PID 或 heater 参数；
heater output 只作为状态/诊断读回。`measurement_temperature_k` 是测量时应使用的
实际样品温度，不能用请求 setpoint 替代。
