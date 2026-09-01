# 温度与 Lock-in 纯文件进度监控

这两个命令只读取正在追加的 JSONL 文件。它们不加载 attoDRY DLL、不打开 COM 口、
不打开 VISA/GPIB、不发送 SR830 查询，也不读取或清除 `LIAS?`/`ERRS?`。因此可与
同一台电脑上的温度、Lock-in 或温度—激励扫描并行运行。

它们不是独立硬件诊断工具：显示的是扫描进程已经读回、flush 并写入 JSONL 的审计数据。
如果扫描进程停止更新，monitor 只能报告最后一条记录，不能替代对前面板、接线和错误
日志的人工核验。

## 温度 monitor

独立温度扫描和温度—激励扫描均可使用：

```powershell
conda activate lyr
python -m attodry_control.temperature_progress_monitor `
  --progress "run_data\temperature_excitation_commissioning\..._temperature_excitation_progress.jsonl"
```

也可以从目录选择最近更新的温度进度文件：

```powershell
python -m attodry_control.temperature_progress_monitor `
  --directory run_data\temperature_excitation_commissioning
```

输出包括当前温度点、请求温度、attoDRY setpoint、sample/VTI 温度、温控开关、错误码，
以及当前处于稳定等待、Lock-in 测量还是完成/失败阶段。只想检查一次当前状态时加入
`--once`；默认每秒继续读取新行，可用 `Ctrl+C` 停止 monitor 而不影响扫描。

## Lock-in monitor

温度—激励扫描的同一个 parent JSONL 可同时由 Lock-in monitor 读取：

```powershell
python -m attodry_control.lockin_progress_monitor `
  --progress "run_data\temperature_excitation_commissioning\..._temperature_excitation_progress.jsonl"
```

新的独立 `sweep-frequency`、`sweep-excitation` 和
`sweep-frequency-excitation` 也会创建独立的：

```text
*_lockin_<scan>_progress.jsonl
```

可以直接从 commissioning 输出目录选择最新一份：

```powershell
python -m attodry_control.lockin_progress_monitor `
  --directory run_data\commissioning
```

在每个点完成 SR830 `SLVL?` 与频率读回后，扫描立即写入
`lockin_point_ready`。因此在时间常数/谐波稳定期间即可看到当前点，而不用等第一条
formal sample。随后每条 formal sample 显示：

- 温度—激励扫描中的请求温度和正式窗口平均温度；
- point 编号、频率、harmonic 与 sample 编号；
- SINE OUT 请求值 `source_v_rms`；
- SR830 `SLVL?` 读回 `source_readback_v_rms`；
- 按本次归档完整电阻路径计算的 `nominal_current_a_rms`；
- Vxx/Vxy 的 `R` 与 phase，以及 lock/overload 和已记录的 problems。

`source_readback_v_rms` 是 XX SR830 对已编程 SINE OUT 幅值的 `SLVL?` 读回，
不是器件端电压的独立模拟测量；`nominal_current_a_rms` 是由该读回和归档电阻路径计算的
名义电流，也不是独立电流表读数。XY 的 SINE OUT 仍必须物理断开，monitor 不会访问它。

## 与 `monitor-live` 的边界

`lockin_test monitor-live` 仍适用于**没有任何 scan 占用两台 SR830**时的即时独立诊断。
如果使用 `--consume-status-latches`，它会读取并清除锁存位，绝不能与 sweep 并行。

扫描期间请使用本页的纯文件 monitor；不要同时运行 `monitor-live`，即使不带
`--consume-status-latches` 也会与扫描竞争同一对 VISA 资源并使时间顺序不可审计。

旧 JSONL 不会被修改。旧版 `lockin_formal_sample` 若没有 `sweep_point`，Lock-in monitor
会显示缺失字段为 `--`，不会按样本数猜测电压点或电流。
