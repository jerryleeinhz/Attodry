# Dual-SR830 实时状态显示

`monitor-live` 是日常检查两台 SR830 当前状态的只读终端面板。它从同一份被 Git
忽略的 `config/hardware.local.toml` 读取 XX/XY 的语义地址和 VISA 超时；不需要填写
电阻、量程、频率或写入授权参数。

从仓库根目录运行：

```powershell
conda activate lyr
python -m attodry_control.lockin_test monitor-live --consume-status-latches
```

默认每秒刷新一次并持续到 `Ctrl+C`。停止监视不会触发 sweep cleanup，因为此命令从不
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

## 写入边界

该命令仅发送查询；不会发送 `SENS`、`HARM`、`FREQ`、`SLVL` 或 cleanup 写入。它不是
扫描预检的替代：实际频率/幅值扫描仍会用自己的完整预检、量程读回、审计 JSON 和
fail-closed cleanup。量程模式和激励路径电阻的日常修改说明见
[`LOCKIN_DAILY_OPERATION.md`](LOCKIN_DAILY_OPERATION.md)。
