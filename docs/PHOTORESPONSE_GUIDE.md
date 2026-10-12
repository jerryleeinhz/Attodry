# 光响应离线运行指南

当前阶段：仅仿真与 fake-resource 测试。没有实机连接或输出命令入口。
开发计划、物理待确认项和厂商资料见 [工作包](modules/PHOTORESPONSE.md)。

## 运行

在项目安装环境运行，或在 PowerShell 中先设置本 checkout 的源码路径：

```powershell
$env:PYTHONPATH = 'src'
python -c "import attodry_control; print(attodry_control.__file__)"
python -m attodry_control.photoresponse_cli describe
python -m attodry_control.photoresponse_cli validate-config --config config/photoresponse_simulation.toml
python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_simulation.toml
```

成功输出 `SIMULATION completed: <完整JSON路径>`。命令在完成清理与文件写入后退出，
返回码为0；运行失败为1，配置或文件错误为2。没有后台进程或自动恢复。
`run_data/photoresponse_simulation/` 保存本次 JSON 和逐事件 flush 的 JSONL，均被 Git 忽略。
JSON 名称包含 UTC 时间、run_name、随机后缀和 outcome，不覆盖历史尝试。

默认不传 `--config` 时读取 `config/hardware.local.toml`。可将所需 NKT 与
`[photoresponse]` 表放在这个统一文件中进行离线运行；不要覆盖已有的站点配置。
当前流程遇到 `backend = "nkt_sdk"` 会拒绝，**不会把真实配置偷偷改成仿真**。
示例中的数值仅用于合成信号，不是样品安全上限。

## 三种模式

| mode | detector | 必需偏置字段 | 结果 |
|---|---|---|---|
| `open_circuit_voltage` | `keithley2182a` 或 `keithley2400` | 禁止两个偏置数组 | `delta_voltage_v` |
| `dc_current_voltage` | `keithley2182a` 或 `keithley2400` | `bias_current_points_a` | `delta_voltage_v` |
| `voltage_bias_current` | `keithley2400` | `bias_voltage_points_v` | `delta_current_a` |

当前各 detector 名称只是仿真角色，不代表对应实机已经可运行。none 不构造电流源，
源设置为 null；恒流0 A仍记录为有源连接。2182A电压和6221电流设定不能推导实际电流。

扫描顺序是 NKT光学点 -> 偏置点 -> repeat -> dark_before/light/dark_after。
每个 condition 保存各维索引。`samples_per_window` 指每个明暗窗口的读数数量；
`repeats` 重复整组三窗口。`settle_s` 是每次切光后的等待，`sample_interval_s` 是
每次正式读数前的等待。硬件时间分辨率未验收，不能把该间隔视为可承诺的采样率。

`nkt_run.dwell_s` 必须为0，避免与光响应窗口的等待重复定义。光学点、百分比上限和
run_name/note/output_directory继续使用同一份NKT配置，`output_directory` 相对TOML解析。
NKT光源输出和滤光片的设置及状态会在电学采样前后复查，开光前也重新核对设置。

## 数据解释

- 每条样本包含UTC时间、从run起点起算的高分辨率单调时间、电压/电流原始值、前后状态。
- 光功率和环境在当前版本没有测量，因此为 null；仿真不调用温控或磁场模块。
- 合成电压亮暗差为5 µV、合成电流亮暗差为2 nA，仅用于测试，不是器件模型或功率标定。
- 每个窗口使用样本的平均值和采样中点的平均时间。暗基线按实际时间线性插值：

```text
w = (t_light - t_dark_before) / (t_dark_after - t_dark_before)
dark_at_light = dark_before + (dark_after - dark_before) * w
delta = light - dark_at_light
```

该算法修正线性暗漂移，不声称消除非线性漂移。保留插值权重、暗漂移和公式版本。
有电压数据时只计算电压差，不自动生成光电流、光响应度或阻值。

## 失败、清理与分析

正常、异常和Ctrl+C均执行关光读回及仿真电学清理，并释放资源。未知preflight不接管。
一次run只要任何采样或清理失败，全部condition均不accepted；先前已完成的原始值和
诊断差分仍保留。JSONL早期sample事件的accepted保持false，只有最终JSON确认有效性。

通信失败保存最后确认读回及state_current=false，不把关闭命令当成成功读回。
光源清理失败仍尝试电学清理。若磁场今后加入，需先实现独立的归零及最终确认。
实机版尚不存在；不得照此仿真命令推断前面板或接线已经安全。

```powershell
python -m attodry_control.photoresponse_cli analyze --record 'run_data/photoresponse_simulation/实际文件名_completed.json'
```

分析默认仅返回completed/accepted且cleanup确认的结果；带有cleanup错误的记录即使
误留accepted标志也不会进入默认分析。需要审计时可显式调用
`photoresponse_analysis.load_results(path, include_rejected=True)`；结果仍标记accepted=false。
分析不改原文件。当前只有只读CLI/loader；Notebook、SQLite断点恢复和自动重试待开发。

## 验证

```powershell
python -m unittest discover -s tests -p 'test_photoresponse*.py' -v
python -m unittest discover -s tests -p 'test_nkt.py' -v
python -m unittest discover -s tests -v
```

所有这些测试使用fake/simulated instruments；不加载真实仪器DLL或打开VISA。
Windows受限运行环境若拒绝Python临时目录，可在获准的普通离线开发环境重跑，
不要因此放松仪器权限检查。
