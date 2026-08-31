# Three-SMU 实时状态监控

`three_smu_cli monitor-live` 是 Three-SMU 模块当前 active Keithley 2400 的终端状态面板。它不依赖 Lock-in、
冷台或主 acquisition，也不使用 QCoDeS 的扫描/设置 adapter。

Three-SMU 已完成 `target offline complete`。2026-09-01 在明确授权下，对当前只启用
`gate_bottom` 的计划完成了一次有界 real read-only 验收；其余语义角色以及任何设置写入仍未验收。

## 每次单独获只读授权后的命令

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
- output 已经是 ON 时的实际 voltage、current 和计算 resistance，以及任何状态下的 output 与
  compliance trip；output 为 OFF 时不发送 `:READ?`，V/I/R 显示 `n/a`；
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

`:READ?` 是测量 query，不是设置写命令，但 Keithley 2400 在 output OFF 且 auto-output-off 未启用时
不能完成该命令。因此 monitor 会先查询 `:OUTP?`：仅在 output 已经是 ON 时发送 `:READ?`；output
为 OFF 时保持 OFF，不尝试启动测量或改变设置，并将 V/I/R 明确标为不可用。所有真实 VISA 查询仍
要求针对当次操作的明确授权。
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

## 2026-09-01 目标电脑只读验收记录

- `lyr` 中 23 项 monitor/Keithley/CLI 聚焦测试和完整 402 项离线测试通过，`src/tests`
  `compileall` 通过；
- 当前计划只打开 `gate_bottom`，其他两个 off 角色未连接；成功读到 Keithley 2400 identity、
  0 V source setpoint、output OFF、compliance/range、2-wire sense 和 trip-clear；
- output OFF 路径没有发送 `:READ?`，V/I/R 显示 `n/a`；默认没有消费 `:SYST:ERR?`，没有发送
  source、compliance、range、sense 或 output 设置写命令；
- 目标机同时启用未使用的 NI GPIB passport 和 Keithley KUSB passport 时，VISA 打开失败；已在
  目标机停用未使用的 NI passport 并保留 Keithley passport。一次选择性 GPIB device clear 解除了
  仪器先前卡住的 parser/output queue；没有执行 `*RST`，也没有改变 source/output/compliance。
