# Dual-SR830 实时状态显示

`monitor-live` 是**没有 sweep 运行时**检查两台 SR830 当前状态的只读终端面板。它从同一份被 Git
忽略的 `config/hardware.local.toml` 读取 XX/XY 的语义地址和 VISA 超时；不需要填写
电阻、量程、频率或写入授权参数。

注意：这里的“只读”只表示不改变仪器设置，不表示不与仪器通信。
每轮都会在已打开的 VISA 会话中顺序调用 XX、XY 的 `read_diagnostic()`，
发送 `*IDN?`、`FREQ?`、`SLVL?`、`SNAP? 1,2,3,4,9` 等查询；
带 `--consume-status-latches` 时还查询并清除 `LIAS?` / `ERRS?`。
它不订阅扫描进程的内存，也不读取扫描 JSONL。

先进入目标 worktree 根目录；`src` 布局每个新终端/切换 worktree 后都要设置：

```powershell
conda activate lyr
$env:PYTHONPATH = (Resolve-Path -LiteralPath ".\src").Path
python -c "import sys, attodry_control; print(sys.executable); print(attodry_control.__file__)"
python -m attodry_control.lockin_test monitor-live --consume-status-latches
```

上面的最后一条命令会真实查询仪器，只能在没有扫描/其他仪器 owner 时运行。
不要用 `$env:PYTHONPATH = ''` 代替 src 路径；详见 [README](../README.md)。

默认首轮立即查询，之后每轮先等待 1 s 再查询，持续到 `Ctrl+C`；实际周期还包含
两台仪器的查询耗时，并不是精确 1 Hz 采样。停止监视不会触发 sweep cleanup，因为此命令从不
修改 SR830 设置。需要有限次刷新时，例如排线检查，可使用：

```powershell
python -m attodry_control.lockin_test monitor-live --samples 10 --interval-s 1 --consume-status-latches
```

## 面板内容

每一帧都顺序读取 XX 和 XY，并显示：

- `X`、`Y`、`R`（V RMS）和 `phase`（deg）；
- `FREQ?` 设定参考频率与 `SNAP f` 实测频率；
- `harm`、`SENS`、XX/XY 的 `SINE OUT` 读回；
- `lock`、`overload` 和 `error` 状态，以及配置/频率不匹配等 warnings。

`SENS` 如果显示为 `code N*`，代表设备返回了驱动完整 SR830 电压量程表之外的原始
代码；面板只如实报告它，绝不会为了显示而改写量程。某个硬件支持但尚未纳入日常
`lockin_safety.toml` 白名单的档位会显示其物理 full-scale，但 sweep 仍会在 VISA 前拒绝。

## 锁定与状态锁存位

`LIAS?` 和 `ERRS?` 会读取并清除 SR830 的锁存状态位。因此默认不查询它们：不带
`--consume-status-latches` 时，电压/相位/频率仍会显示，但 lock、overload、error 会
明确标为 `not queried`，不能据此判定测量安全。

日常物理检查需要实时锁定、过载和错误状态时，显式加入
`--consume-status-latches`。这会显示当次已读出的状态，但也会消费锁存位；不要把它
与任何正在运行的 sweep、commissioning 或其他访问同一对 VISA 地址的程序并行运行。
先停止写入/扫描命令，再启动监视；若面板出现 `UNLOCKED`、`OVERLOAD`、非零 error 或
warnings，应停止后续测量并按前面板和接线手动核实。

## 扫描期间请使用纯文件 monitor

正在进行 `sweep-frequency`、`sweep-excitation`、`sweep-frequency-excitation` 或
`temperature_excitation_scan` 时，不要启动本命令。即使不带
`--consume-status-latches`，VISA 查询仍会与 acquisition 竞争同一资源并破坏可审计时序。

改用 [`FILE_PROGRESS_MONITORS.md`](FILE_PROGRESS_MONITORS.md) 的
`temperature_progress_monitor` 或 `lockin_progress_monitor`。它们只读取扫描已写入并
flush 的 JSONL，不访问 COM/VISA，也不清除锁存位。

例如在另一个已设置好 `lyr` / `PYTHONPATH` 的终端中：

```powershell
python -m attodry_control.lockin_progress_monitor --directory run_data\commissioning
```

目录须替换为本次扫描实际输出目录；更稳妥的是用 `--progress "<本次进度文件>.jsonl"`
指定准确文件，避免选到别的运行。刷新频率不会增加采集次数，文件不更新就没有新读数。
不能因为换终端、Python 进程或 worktree 就认为仪器已隔离：它们仍指向同一物理设备。
并发查询可能交错请求/响应或改变时序；第二个读者还可能抢先消费扫描需要的告警锁存。
本命令没有自动转发到采集 owner 的机制，不要把它当成并发安全的监控端。

## 写入边界

该命令仅发送查询；不会发送 `SENS`、`HARM`、`FREQ`、`SLVL` 或 cleanup 写入。它不是
扫描预检的替代：实际频率/幅值扫描仍会用自己的完整预检、量程读回、审计 JSON 和
fail-closed cleanup。量程模式和激励路径电阻的日常修改说明见
[`LOCKIN_DAILY_OPERATION.md`](LOCKIN_DAILY_OPERATION.md)。
