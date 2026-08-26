# Three-SMU 实时状态监控

`three_smu_cli monitor-live` 是 Three-SMU 模块当前 active Keithley 2400 的终端状态面板。它不依赖 Lock-in、
冷台或主 acquisition，也不使用 QCoDeS 的扫描/设置 adapter。

当前 Three-SMU 仍处于 `S0 offline complete`：本文件是未来实机 query-only 阶段的操作边界，
本次开发没有连接、查询或写入真实 SMU。

## 未来获授权后的命令

```powershell
python -m attodry_control.three_smu_cli monitor-live
```

默认从 ignored 的 `config/hardware.local.toml` 读取扫描计划。它只加载和打开
`role = "fixed"` 或 `role = "sweep"` 的语义角色；`off` 角色无需硬件表，也不会连接、
读取或消费状态。面板将它显示为“not connected / physical state unknown”。每秒显示一帧；
按 `Ctrl+C` 停止。有限次数采样例如：

```powershell
python -m attodry_control.three_smu_cli monitor-live --samples 10 --interval-s 2
```

面板的每一帧按 `smu_bias`、`gate_top`、`gate_bottom` 语义顺序读取其中的
active 子集，包含：

- TOML 中的 scan 角色（off/fixed/sweep）和实际 source mode/setpoint；
- 实际 voltage、current、计算的 resistance、output 与 compliance trip；
- active compliance、source/measurement range、2/4-wire sense、`*IDN?`；
- 适用的安全 warning：source mode、实际 V/I 绝对边界、compliance 高于对应
  `max_abs_*`、trip、output 已开启，以及 identity 重复。

这是一帧内依次读取的快照，不是 active SMU 的同步触发测量。显示的电阻仅为该帧
`V/I`；电流为零时
显示为不可定义。

## 严格的 query-only 边界

监控 adapter 只使用 SCPI query，不包含 configure、set source、set output、ramp 或 cleanup
方法。它仅设置本地 VISA handle 的 timeout；关闭 handle 只释放本地资源，不改变仪器 setpoint
或 output。监控不会创建 run directory、写 metadata/raw/data，也不会自动处理 warning。

`:READ?` 是测量 query，不是设置写命令；它仍会要求未来针对本次真实 VISA 查询的明确授权。
任何通信失败都不会推断 output 已关闭或 source 已归零，应以最后一条确认的面板读回和仪器前面板
为准。

## Error queue 是显式、消费式操作

默认面板不发送 `:SYST:ERR?`，并明确显示 error status 未查询。原因是 2400 的 error queue
每次查询都会取走一个队列项目，不能伪装为无副作用读取。

仅当本次消耗状态队列也获得授权，且操作者需要记录/清除一条当前状态时，才使用：

```powershell
python -m attodry_control.three_smu_cli monitor-live --consume-status-queue
```

此选项每一帧、每台 active SMU 最多消费一个 queue 项。非零/未知响应只会显示 warning；监控不会清除
其他状态、不会恢复 output，也不会继续代表操作者执行任何动作。

## 与扫描的关系

不要与 `three_smu_cli run` 或 live Notebook 同时监控同一台 SMU：额外测量查询和可选的 status
queue 消耗会扰乱扫描时序/审计。监控不是扫描 preflight 的替代品，也不授权后续写入。

若面板显示 output ON、trip、V/I 或 compliance 越界、mode 不符、身份重复或错误队列 warning：
停止后续动作，保留终端输出，并按实验室步骤人工检查 active 仪器的前面板、接线与器件状态。
off 仪器从未被读取，因此不得从面板推断其 output 或 source 状态。不要通过提高
TOML 限值来消除 warning。
