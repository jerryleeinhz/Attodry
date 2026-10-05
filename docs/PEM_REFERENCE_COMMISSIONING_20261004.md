# PEM 1f → SR830 XX 参考同步诊断记录

日期：2026-10-04。项目分支：`integration-photonics-nonlinear-hall`，基线 `ae93eee`。

## 结论与范围

P1 诊断已完成，**TTL 同步验收未通过**。两次最小设置测试均出现 XX 参考失锁
`LIAS=8`；第二次已先启用 PEM 并读到 `STABLE=true`，仍未锁定。
因此不能把失败仅归因于第一次 PEM 未稳定，也不能把用户报告的约 3.4 V
直接认定为已证实的唯一原因。没有取得 PEM → XX → XY 的完整同步证据。

最终恢复并确认：XX internal、5000 Hz、h1、4 mVrms；XY external TTL rising、
5000.05 Hz、h1、4 mVrms；两台最终 `LIAS=0`、`ERRS=0`，cleanup errors 为空。
第二次测试前后均只读确认 NKT OFF，SDK 写命令列表为空。PEM 收到 disable ACK，
但不能据此宣称物理调制输出已直接测量为关闭。

本次没有增加激励，没有 NKT 设置或发光命令，没有磁体、温控或 SMU I/O，
没有执行光功率/波长或联合采集扫描。停止自动重试；下一步依赖连接点的实际波形/
负载证据，或另行审查并授权的触发方式/电平调理方案。

## 接线和只读基线

用户确认 PEM 使用 1f（约 50 kHz）参考，XX SINE OUT 已接样品，XY SINE OUT
物理断开。目标链为 PEM REF OUT → XX REF IN，XX TTL SYNC OUT → XY REF IN。

原始只读记录：`20261004T170550_query_only_7b4d4574.json`。

| 设备 | 切换前实际读回 |
|---|---|
| lockin_xx | internal，5000 Hz，h1，4 mVrms |
| lockin_xy | external TTL rising，5000.05 Hz，h1，4 mVrms |
| PEM V01 | 50027.73 Hz，AMP 160.749999 nm，STABLE=false；物理输出状态不可查询 |

该基线只证明原 5 kHz 电学参考链状态，不能作为 PEM 同步结果。

## 第一次 TTL 测试：PEM 尚未稳定

原始记录：`20261004T171023_ttl_1f_b1c3bdba.json`，结局 rejected。

- PEM 读回 `STABLE=false`。本次不启用 PEM，也不对 NKT 执行 I/O。
- XX 的实际设置写命令只有 `FMOD 0`，以及 cleanup 的 `FMOD 1`；4 mVrms、
  h1 和其余设置保持原值。
- 切换 XX 为外参考后等待 5 s，转换读回 XX `LIAS=8`、`FREQ=5000 Hz`。
- 再等待 5 s，第一条正式 XX 读回仍为 `LIAS=8`、`FREQ=5000 Hz`，立即拒绝，
  没有继续读取正式 XY 样本。
- 转换期 XY 曾读到 4565.33 Hz；这不是约 50 kHz PEM 锁定证据，也不是正式接受点。
- cleanup 恢复 XX 内参考；后续最终读回以本报告的恢复状态为准。失败原始记录保留。

## 第二次 TTL 测试：启用并稳定 PEM 后复核

原始记录：`20261004T171526_pem_active_ttl_1f_60fc56aa.json`，结局 rejected。

- 先只读确认 NKT OFF，结束后再次只读确认 OFF；前后读回均为 emission=0、
  status=0、interlock=2，NKT SDK `writes=[]`。interlock 原始值保留，不从它
  另行推断光路状态。本步骤仅验证关光，没有 NKT 参数设置或 emission 命令。
- 保持 PEM AMP 160.749999 nm；发送 `:SYS:PEMO 1` 并收到 ACK，随后读到
  `STABLE=true`、频率 50027.0429 Hz。没有为此修改 PEM 幅度。
- 在相同 4 mVrms 最小激励条件下再次测试 XX TTL 外参考：切换后等待 5 s，
  转换读回 FMOD=0、FREQ=5000 Hz、LIAS=8、ERRS=0；再等待 5 s，首条正式
  XX 仍为 FREQ=5000 Hz、LIAS=8、ERRS=0。立即拒绝，没有正式 XY 读取。
  XY 转换读回为 4565.33 Hz、LIAS=0、ERRS=0，不是 PEM 同步证据。
  PEM 稳定不等于接收端已识别 TTL 参考。
- cleanup 恢复原 SR830 参考状态并发送 `:SYS:PEMO 0`，收到 disable ACK；
  PEM 物理输出关闭仍缺少可查询/实测证据，不扩大结论。
- 最终 XX/XY 频率、参考模式、h1、4 mVrms 与本报告开头一致，状态/错误均为零，
  没有 cleanup error。

两次尝试的转换、正式/部分读取与恢复数据均保留。失败结局不因 cleanup 成功而
改写为 accepted；也不因启用 PEM 后它自身稳定而宣称双 SR830 链路 commissioned。

## 电平解释与检测频率

[SR830 官方手册](https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf)
规定可靠 TTL 参考的高电平应高于 3.5 V、低电平低于 0.5 V。约 3.4 V 低于该可靠
高电平条件，是合理待查因素；本次没有直接测量接收端波形，不能证明它就是失锁原因。
需要保留 REF OUT/REF IN 实际测量位置、终端阻抗、低电平、边沿和负载条件。

[PEM200 Rev C 官方手册](https://www.hindsinstruments.com/wp-content/uploads/PEM-200-User-Manual.pdf)
第 13 页描述 3.3 V、第 49 页列出 5 V，两处口径不一致。应以实际控制器/输出和
现场测量解释此次结果，不能任取一处标称数字替代证据。

SR830 手册明确：HARM 改变检测阶次，**不改变 SINE OUT 或 TTL SYNC OUT 的
输出频率**。因此不能把选择 XX h2 推断成 XY 接收到倍频参考。今后仍需实测验收
外参考 profile 的各阶次、同步状态和相位标签。约 50.027 kHz 基频下，h2 约
100.054 kHz，接近但不超过 102 kHz 检测上限；h3 约 150.081 kHz，超限而不可运行。

## 文件、验证与后续状态

上述三个 JSON 以及两次 TTL 测试的配套 JSONL 都在 `LK_setup` 的 ignored
`run_data/photonics_reference` 中；本机新 worktree 的相同 ignored 相对目录保留副本。
保留原始 rejected 记录，不提交地址或实验数据。一次性 commissioning helpers 与
fake 测试保留在 ignored `tmp`，未修改生产控制代码。

更早的 `20261004T170533_query_only_1407d026.json` 也保留：首次只读启动将配置中的
`default` 错当作 VISA 库名，在打开仪器之前失败；修正为默认 VISA backend 后取得
上述有效只读记录。该启动失败不属于仪器同步测试。

诊断 helper 的本机/目标 SHA-256 一致：

| Helper 版本 | SHA-256 |
|---|---|
| 原始最小 TTL 诊断 | `73e084b11a37fc2b40c821c93b68520aba4c806fc328df0644db8e13d0cf812c` |
| PEM activation 变体 | `15c2d291f1c9d59b78356e6ab59c9d2b6662137dbcbe847d090b591955c77f44` |

执行前验证：第一版 helper 的 9 项本地 fake 测试通过；包含 PEM 启用变体的
13 项本地和 13 项 `LK_setup` fake 测试均通过。它们验证诊断流程的参数检查、顺序、
失败/恢复行为，不证明真实 TTL 电平兼容，也不是完整联合扫描的验收。

P0 项目计划完成；P1 诊断完成但 TTL synchronization acceptance failed，后续
同步验收等待物理波形/连接证据或单独审查的替代方案。P2–P6 仍为 planned。
没有生产代码合并、commit 或 push，没有部署新的日常联合扫描入口。
最终 Git 核查：本机新分支 HEAD=`ae93eee`，tracked 变更仅为本项目文档；原 NKT
checkout 的既有 dirty 文件未变。目标光学 checkout HEAD=`a8d991a`，tracked-clean。
临时 helper 的目标运行不表示新的 integration 分支已部署。
后续目标、接口和待确认实验参数见
[项目工作包](modules/INTEGRATION_PHOTONICS_NONLINEAR_HALL.md)。
