# Magnetic-field module work package

## 当前状态

### 2026-09-11 最新边界（优先于下方历史记录）

- 用户确认纯 X 最高 +/-3 T、纯 Z 最高 +/-9 T；X/Z 同时非零时，才额外要求
  `sqrt(Bx^2 + Bz^2) <= 3 T`。只有严格零（含 signed zero）才算单轴，不以容差
  忽略非零请求或残余读回。较低的 local 每轴上限仍生效，不能配置超过额定值。
- `experiment_vector_max_t` 保持原字段名，现在只表示双轴合场上限（最大 3 T）；
  单轴由 `hardware_x_max_t` / `hardware_z_max_t` 限制。示例仍仅零目标，不是 M4 参数。
- 新日志声明 `field_limit_policy = single-axis-hardware_combined-vector-v1`，monitor
  按归档限值和选定的安全 mixed corner 验证；未选的超限候选仅保留作审计。
  旧日志保留原 3 T 检查，未知声明或无效限值不能认证完成。
- 操作者已确认此前 M3 成功：10 次只读、无错误、零场读回、正常断开；输出未附
  exact source provenance。首次 M4 已选择 `Bx=+0.1 T, Bz=0 T`，仅用于准备配置；
  步长、时序、transition policy 和真实连接/写入授权待确认。新限值版本需
  target-offline 与写入前只读复核；本次只做离线修改。

首次 X 轴目标片段如下，只在控制电脑的 ignored `config/hardware.local.toml`
现有 `[magnetic_field_run]` 表内替换 `points`，不要新增重复表。此片段不是完整的
可执行配置；尚未修改任何 local hardware 配置，也不授权运行：

```toml
points = [{ bx_t = 0.1, bz_t = 0.0 }]
```

最终流程仍为 `single-target`：确认初始状态、到达该小目标、稳定读回、monitored
zero 并验证、断开。`transition_policy`、`max_step_t` 和 `[magnet]` 的稳定判据需
在实机前确认；tracked example 的参数不因本次目标选择而自动成为获批参数。

#### 本次离线交付记录

```text
模块：Magnetic Field。
目标与最终日常命令：更新单轴/双轴限值；首次 X 目标 +0.1 T。
  计划使用 magnetic_field_cli single-target；日常实机命令尚未获批。
完成到的阶段：本次限值修改 offline complete；此前 M3 为操作者确认；M4 未执行。
分支/worktree：codex/magnetic-field-m0-m2；C:/Users/liy56/.codex/worktrees/8fd7/Attocube_control。
提交号（如已提交）：本次未提交；本地 HEAD 为 89067c9。
修改文件：safety/config/attodry/magnetic_field_cli/magnetic_field_monitor、对应测试、
  AGENTS、README、两个 tracked 配置示例、安全/验收/阶段/handoff/模块文档。
配置字段和单一事实来源：local TOML 的每轴上限及 experiment_vector_max_t；
  safety.py 统一校验。目标片段仅作准备，未改 hardware.local.toml。
记录/schema 变化：外层 v1 不变；新增 field_limit_policy 与归档限值校验，旧记录语义保留。
测试命令与结果：六个 focused unittest 模块 213 tests OK（最终重跑 29.303 s）；
  unittest discover -s tests -q：481 tests，476 passed / 5 skipped，35.522 s；
  compileall、single-target/scan --help、git diff --check 通过。
真实硬件是否连接：本次未连接、未加载真实 DLL。
读取/消费状态/写入的授权范围：本次没有真实操作授权；0.1 T 回复仅用于配置准备。
实际发送的写命令：无。
保存的原始记录位置（不得提交）：本次没有实机记录；未来由 local TOML output_directory 决定。
最终确认状态及人工核验要求：本次未取得新实机状态，不能沿用历史零场作为当前状态；
  写入前须核实磁体运行条件并复查只读状态，通信不确定时人工核验。
本机/GitHub/LK_setup commit 与 dirty 状态：本机 89067c9 + 未提交修改；
  上次确认远端该分支为 89067c9，本次未 push；LK_setup 当前 commit/dirty 未检查。
仍待用户确认的物理参数：step、transition policy、tolerance、stable range、dwell、poll、timeout；
  以及计划级连接/写入授权。正常单目标结束必须 monitored zero。
Integration 需要知道的接口或限制：签名与配置键不变，合场上限仅用于双轴；
  严格零判定、逐分量实际读回保护；不改变 APS100 扫速、不证明连续物理轨迹。
```

Standalone attoDRY X/Z 磁场模块的 M0--M2 已完成：严格的
`[magnetic_field_run]` 配置、纯 setpoint 计划、fake-DLL adapter 行为、单目标/有序
扫描 CLI、逐事件 fsync 的 canonical JSONL 和只读文件 monitor 均已在本地实现并测试，
随后通过 M2 target-offline 验证。本地证据只来自 fake DLL；M2 snapshot 则明确不含
vendor 内容或 DLL。两个阶段都没有加载真实 vendor DLL，没有调用真实 `begin/connect`，
也没有向 attoDRY 或磁体发送命令。

M2 已在 `LK_setup` 的 exact `lyr` Python 和隔离 snapshot 中完成。M3 真实只读、M4
最小单轴运动和 M5 有序 X/Z 扫描仍分别受新的明确授权 gate 约束。项目早期 10 秒
`attodry_test` 只读记录只说明当时通用 driver 能读到零场状态，不能替代当前 revision
的 M3 验收。

本文件还记录随后扩展的离线 transition/audit contract：它只使用 pure policy 和 fake
DLL，未加载真实 DLL、未连接设备，也不把历史 `e0924f1` snapshot 误写成后续 revision
的 target-offline 证据。任何后续 revision 若要声称 M2 target-offline complete，必须以
该 revision 重新完成 DLL-free target validation；无论如何都不授权 M3--M5。

本次最终本地证据为：

- 以下 focused safety/stability/config/attoDRY/magnetic/monitor 命令：182 tests，
  6.533 s，OK：

  ```powershell
  $env:PYTHONPATH='src'
  python -m unittest -v tests.test_safety tests.test_stability tests.test_config tests.test_attodry tests.test_magnetic_field tests.test_magnetic_field_monitor
  ```

- `python -m unittest discover -s tests -v`：450 tests，14.373 s，OK（5 skipped，
  均为 optional matplotlib）；
- `python -m compileall -q src tests`、两个 magnetic CLI 的 `--help` 和
  `git diff --check`：通过（diff check 只有 CRLF warnings）。

M2 target-offline evidence（2026-09-03）：

- tested implementation commit：
  `e0924f1666b8e1b0b8e6e0c08daad2ab9f9ac4c4`（short `e0924f1`）；
- source archive：`attodry_m2_e0924f1.zip`，482705 bytes，SHA-256
  `231F649FA8A77B6139F67239F4E322275AA06B0BA3B4F0E628DEAFBFD32569F1`；复制到
  `C:\Users\LK_Setup\attodry_m2_e0924f1.zip`，解压 snapshot 为
  `C:\Users\LK_Setup\attodry_m2_e0924f1`；
- target：`LK_setup`，Conda environment `lyr`，exact interpreter
  `C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe`，Python 3.12.13，64-bit；
- archive 明确排除了 `vendor/`；target snapshot 确认 `vendor/` 不存在，递归 DLL count
  为 0。单独的 monitor import-isolation 检查从该 snapshot 导入 monitor，且没有导入
  `attodry_control.attodry` driver；
- `python -m compileall -q src tests` 和两个 magnetic CLI `--help` 检查通过；
- focused target 命令：

  ```powershell
  python -m unittest -v tests.test_safety tests.test_stability tests.test_config tests.test_attodry tests.test_magnetic_field tests.test_magnetic_field_monitor
  ```

  182 tests，4.675 s，OK；
- `python -m unittest discover -s tests -v`：450 tests，20.299 s，OK；target output
  未报告 skip；
- target-validation shell 没有使用 authorization flag 调用 hardware-execution CLI；
  suite 内的 authorization-path tests 只使用 injected fakes。未加载 DLL，也未连接或
  操作硬件。

## 已实现的独立入口

只查看帮助不会加载 DLL：

```powershell
python -m attodry_control.magnetic_field_cli --help
python -m attodry_control.magnetic_field_cli single-target --help
python -m attodry_control.magnetic_field_cli scan --help
python -m attodry_control.magnetic_field_monitor --help
```

未来经过对应阶段授权后，写路径的命令形状为：

```powershell
python -m attodry_control.magnetic_field_cli single-target `
  --config config/hardware.local.toml `
  --authorize-connection `
  --authorize-field-writes

python -m attodry_control.magnetic_field_cli scan `
  --config config/hardware.local.toml `
  --authorize-connection `
  --authorize-field-writes `
  --authorize-ordered-field-scan
```

这些命令只是已经离线测试的能力，不是当前的真实硬件操作许可。`single-target` 必须在
`points` 中恰有一个目标，并要求 connection 与 field-write 两个 gate；正常结束也
强制 monitored zero。`scan` 还必须通过独立的 ordered-scan gate；正常结束才按
`[cleanup].normal_end_field_policy` 选择 `hold` 或 `zero`。任何异常或 `Ctrl+C` 的
field policy 必须为 `zero`。

已有 JSONL 后，下面的 monitor 只读指定文件：

```powershell
python -m attodry_control.magnetic_field_monitor `
  --progress run_data/magnetic_field_commissioning/<record>.jsonl
```

它不导入、加载或连接 vendor DLL，也不查询控制器。它只汇总完整 JSONL 行中的事件数、
完成点数、当前点、outcome、最后确认状态、zero/disconnect/manual-verification 标志，
并报告是否存在一个未写完的末行。它还检查 schema/run ID、连续 event index、时间、
唯一首行/末行、ordered-point prefix 和 terminal flags；无 terminal、integrity error、
要求 zero 却未验证 zero 的矛盾 completed record，或 completed record 缺少 final confirmed
state，一律报告 `outcome="incomplete"`、`audit_complete=false` 和需要人工核验，不能证明
控制进程仍在运行。

## 配置契约

独立 loader 只严格解析本模块需要的 `project`、`cryostat`、`magnet`、`cleanup` 和
`magnetic_field_run`；已知但与本模块无关的不完整 Lock-in、temperature 或 SMU 表
不会阻止离线解析。本机 COM、DLL path 和真实目标继续只放在 ignored
`config/hardware.local.toml`。

```toml
[magnetic_field_run]
points = [
  { bx_t = 0.0, bz_t = 0.0 },
]
transition_policy = "via_zero"
max_step_t = 0.05
run_name = "magnetic_field_offline_example"
note = "Replace only after staged magnetic-field commissioning approval."
output_directory = "../run_data/magnetic_field_commissioning"
```

- `points` 是非空、显式、按书写顺序执行的 X/Z 点列；不排序、不去重，也不展开为
  Cartesian grid。重复点仍保留独立 point index、point events 和稳定性确认；若
  setpoint readback 已在 acknowledgement tolerance 内，则该点不发送多余 component
  write，但仍重新进行 target stability measurement。
- `transition_policy` 是必填枚举：`direct` 将每一对相邻 confirmed/requested vector
  endpoints 分段为不超过 `max_step_t` 的离散 waypoint；`via_zero` 明确选择从起点到
  zero、再从 zero 到 target 的保守序列。不得根据目标值、日志或 GUI 状态偷偷改选另一
  策略，也不得透明插入 zero detour。
- 每个 `bx_t`/`bz_t`、每个生成的 float32 setpoint waypoint，以及两种可能的 X→Z / Z→X
  mixed setpoint 都按上述单轴/双轴边界检查，只执行安全的角点，否则在 setting
  write 前失败。校验使用 DLL 实际接收的 IEEE-754 binary32 values，避免量化越界；
  每个 waypoint 只在两个 mixed corners 都已经检查后，才根据安全角点选择
  实际 axis order。
- `max_step_t` 只限制相邻**请求 setpoint waypoints** 的矢量间距。实现会为不同目标
  按已选 `transition_policy` 生成并逐分量等待 setpoint readback acknowledgement。每个
  内部 waypoint 随后也按 field stability contract 等待 actual Bx/Bz 收敛；显式 target
  和 cleanup zero 同样需要稳定读回。`direct` 只描述已验证的离散 command endpoints，
  而不是 vendor controller 的连续直线运动；实现仍不测量或控制相邻稳定 waypoint 之间的
  continuous physical path，因此不能据此宣称 constant-angle、constant-magnitude、实际
  straight-line trajectory、ramp rate 或任意其它 physical-path 性质。
- `max_step_t` 必须大于 1e-5 T 的 setpoint acknowledgement resolution；配置的
  `field_tolerance_t` 不得超过已确认的 1 mT 上限。
- tracked `hardware.example.toml` 故意只给零场点；它不是首次真实运动参数。
- `output_directory` 相对所选 TOML 解析。一个运行只写一个 canonical JSONL；没有另一个
  summary JSON 或 CSV 可以取代它。
- shared `[project].database_path` 仍由严格 project contract 解析，但这个 standalone
  命令不打开或写 SQLite；运行证据只在上述 JSONL 中。

## 运行、安全与审计行为

1. 配置、全部显式目标、`transition_policy` 及相应的静态 setpoint 计划在 DLL load
   前校验；缺少对应 authorization 时也在 DLL load 和输出目录创建前停止。`direct`
   和 `via_zero` 是记录在配置、point event 与 plan 中的两个不同请求，不能相互替代。
2. 连接初始化状态只接受严格的 0/1。连接后读取完整状态，拒绝非零 error、超限
   actual field 或超限 setpoint。field control 使用 read-before-toggle；执行 OFF→ON
   takeover 前，actual field 必须在 configured（且不大于 1 mT）tolerance 内匹配 latent
   setpoint，而且因 controller 内部轴顺序未知，两种 actual/setpoint mixed corners 都要
   满足上述单轴/双轴限值。否则不发送 toggle。toggle 后第一条 acknowledgement readback 的
   elapsed 使用真实计时并受 acknowledgement timeout 约束，不能固定记为零或越过 deadline。
3. 在第一条 component-setting write 前，从实际初始 setpoint 重新计划；control
   acknowledgement 后再从最新确认 setpoint 重算一次，并先确认 actual field 已在
   starting setpoint 稳定。每个重算的 float32 endpoint、相邻步长和两个 mixed corners
   都必须再次通过 safety validation；无法确定安全 order 时停止而不是猜测。
4. 每个 waypoint 都从已确认前一个 setpoint 动态选择已验证的 X→Z 或 Z→X order，只对
   改变的分量写命令；该 selected order、两种 corners、float32 endpoint、前驱和完整
   executed waypoint/path 都要审计。每次分量写后等待完整 setpoint readback 确认，然后
   在前往下一 waypoint 前按 `[magnet]` 的 tolerance、stable range、dwell、polling 和
   timeout 确认该 waypoint 的 actual Bx/Bz 稳定。显式 target 也保留自己的 point-level
   稳定确认。为覆盖 polling jitter 而保留的 dwell cutoff 前一条样本也参与
   target-tolerance 与 rolling-range 判定；它若不合格，不能只凭后续样本提前通过。control
   暂时关闭会清空连续稳定窗口；状态错误、setpoint 改变或超限读回均 fail closed。稳定
   waypoint 仍只是离散证据，不能证明 waypoint 之间的 continuous physical path。
5. JSONL 每行带 `schema_version`、`run_id`、单调 `event_index` 和 capture time；每次
   append 都 flush 并 `fsync`。起始 metadata 还保存 config SHA-256、source provenance、
   interface/config、authorization scope、完整 field-stability criteria（包括
   `minimum_samples`）、driver acknowledgement protocol、limits、points、transition policy、
   cleanup policy 以及 required exact-field-command audit descriptor。每个实际
   field-control toggle、X/Z component command 与 sweep-to-zero command 都先记录 durable
   attempt（command index、symbol、context 和 IEEE-754 binary32 component bits），再记录
   DLL return code 与 post-command full-state/readback acknowledgement result；失败也保留
   result，不能用后续日志补写为成功。preflight、计划、control/setpoint acknowledgement、
   field samples、point completion、failure、zero、disconnect 和 terminal outcome 均进入
   同一 canonical stream。rejected/interrupted/partial 事件不删除；audit append/`fsync`
   failure 会锁存为拒绝原因并阻止正常继续写入，但这些审计故障都不得中断仍可执行的
   best-effort monitored-zero cleanup。不确定的 append 会尽力回滚，因此失败的 terminal
   `fsync` 不能留下已认证的 completion。
6. 单目标正常完成后必须执行 vendor sweep-to-zero，并同时确认零 setpoint、实际场在
   configured tolerance 内、enabled field control、clear error、连续稳定窗口和最终完整
   状态。`isZeroingField`、vendor action/error message 和 vendor log 只能附加为 diagnostic，
   不能替代这些 zero 判据。scan 的 normal `hold` 也要
   再读状态并确认 final setpoint、actual Bx/Bz 在 target tolerance 内、control 与
   error；若 final target 恰为 zero，还要通过 magnitude criterion。`hold` 本身不是
   zero。当前 cleanup 不会把 field control toggle 为 disabled：verified zero 后保持
   control enabled at zero，normal hold 后保持 control enabled at final target，然后
   断开 DLL session。
7. 异常或 `Ctrl+C` 在已经连接时进入同一 best-effort monitored-zero cleanup，然后才
   尝试关闭连接。若 normal-zero 流程失败或被中断，cleanup 仍独立重试一次 monitored
   zero。zero/close/audit 失败、未知连接状态、没有 last confirmed state、非有限读回或
   任何 communication uncertainty 都要求人工核验。若主错误是通信不确定，即使后续读回
   看似到零，也不能把它记为可信的 verified zero。
8. Python 硬崩溃、进程被强杀或机器掉电不能保证 cleanup。文件 monitor 也不能恢复硬件；
   必须查看 attoDRY/APS100 状态。

## 模块目标

1. 对显式目标、生成的 setpoint 和实际读回执行单轴/双轴限值。
2. 纯 X 最高 3 T、纯 Z 最高 9 T；双轴非零时合场最高 3 T，严格零才算单轴。
3. 独立验证读回、read-before-toggle、setpoint acknowledgement、稳定等待、monitored
   zero、异常 cleanup 和 canonical audit。
4. 通信失败后保存最后确认的 Bx/Bz、setpoint、control 和 error，不推断零场。
5. 在 standalone commissioning 后向 Integration 提供最小矢量场接口；当前尚未集成。

## 非目标

- 不控制温度、SR830、SMU 或机械 rotator。
- 不把 attoDRY 软件中的 X 与工厂表的 transverse Y 混用。
- 不在未经校准时声明正负方向对应样品的物理方向。
- 不从离散 setpoint 计划推断连续 physical path、constant angle 或 constant magnitude。
- 不把“已发出归零命令”描述成“已经归零”。
- 不把本地 fake-DLL 通过误报为 target-host、真实 DLL 或真实磁体 evidence。

## 阶段和验收条件

### M0 - contract audit（当前：local complete）

- X/Z 命名、单轴 3/9 T 与双轴合场 3 T 边界、read-before-toggle、状态/readback、异常 zero 和
  last-confirmed-state 契约已对照现有 model、safety、attoDRY adapter 与 tests。
- 当前文档明确区分 setpoint 序列与未观测的连续 physical path。

### M1 - offline safety and failure tests（当前：local complete）

- 本地 pure/fake-DLL coverage 包括边界/超限、非有限输入与读回、ordered duplicate points、
  `direct`/`via_zero` policy、ascending/descending/reverse X/Z transitions、strict
  initialization flags、safe field-control takeover、两种 mixed setpoint order、float32
  endpoint/corner/step validation、component-write failure、delayed acknowledgement、control
  loss、stable starting setpoint/waypoints/targets、zero-vector magnitude hold、first-post-
  toggle elapsed/timeout、acknowledgement/zero timeout、jitter-cutoff sample qualification、
  communication failure、异常与 normal-zero retry cleanup、audit-write/terminal-fsync
  failure，以及 exact command-attempt/result 和 contradictory-terminal stream-integrity
  validation。
- focused 与完整 suite 的最终本地 count 见“当前状态”。
- 没有真实 DLL load、connection 或 hardware command。

### M2 - target offline validation（当前：complete，2026-09-03）

- Commit `e0924f1` 的 DLL-free source snapshot 已在 `LK_setup` 的 exact 64-bit Python
  3.12.13 `lyr` interpreter 下通过 compileall、两个 magnetic CLI help、182 focused
  tests 和全部 450 tests；exact archive、path、hash、timing 和 module-import 证据见
  “当前状态”。
- snapshot 递归包含 0 个 DLL；单独的 monitor import-isolation check 未导入 attoDRY
  driver。target-validation shell 未向 hardware-execution CLI 提供 authorization flag，
  authorization-path tests 仅使用 injected fakes；没有 DLL load、真实 `begin/connect` 或
  hardware operation。M2 只证明 target-offline 可复现性，不授权或替代 M3--M5。

### M3 - real read-only commissioning（当前：gated / 未授权）

- 需要新的 connection/read-only 授权；只读取 Bx/Bz、setpoint、field-control 和 error，
  不发送 toggle、component setpoint 或 sweep-to-zero。
- 当前 revision 的 M3 使用已有 read-only CLI；它没有 write-authorization 参数：

  ```powershell
  python -m attodry_control.attodry_test `
    --config config/hardware.local.toml `
    --samples 10 `
    --interval-s 1 `
    --authorize-connection
  ```

- 运行前必须让 attoDRY vendor GUI/其它 controller client 断开，并确认没有另一个
  magnetic-field 或 attoDRY process 并发占用 DLL/resource。保存 stdout/stderr 到 ignored
  target path；本命令仍未在当前 revision 上执行。
- 早期通用 `attodry_test` 的 10 秒零场记录不能替代本模块、当前 commit 和当前 local
  config 的只读验收。

### M4 - smallest single-axis movement（当前：gated / 未授权）

- 用户分别确认最小 X 或 Z 目标、`max_step_t`、tolerance、dwell、timeout、最终 zero
  策略和前面板初态，并明确授权 connection 与 field writes。GUI/其它 client 必须断开，
  且不得与任何其它 attoDRY field process 并发运行。
- 一次只验收一个轴和一个小目标；使用 `single-target`，正常结束必须 monitored zero；
  不与温度、Lock-in 或 SMU 组合。
- 完成需要原始 canonical JSONL、目标/zero 完整读回、人工核验，以及每条实际发出的
  float32 toggle/component command attempt/result。离线 writer/monitor 已要求并验证此
  transcript contract；它只是 M4 的必要审计前提，不能替代 M3 read-only、用户选择的
  最小目标、connection/write authorization 或一次真实 M4 证据，因此当前仍不能申请日常
  运行。

### M5 - ordered X/Z scan（当前：gated / 未授权）

- 只在 M4 通过后申请新的 ordered-scan 授权，并逐项审阅完整 `points` 列表；重复点也
  是独立授权目标。GUI/其它 client 必须断开，且不得并发运行其它 attoDRY field process。
- 验收范围只是有序 setpoint/读回/稳定性和 cleanup；除非另有测量与硬件证据，不得
  声称 continuous physical path、constant angle 或 constant magnitude。
- 完成需要每个 point index、setpoint acknowledgement、稳定读回、terminal cleanup
  和 manual-verification 判定均可从 canonical JSONL 审计。

## 当前文件所有权

- `src/attodry_control/magnetic_field.py`
- `src/attodry_control/magnetic_field_cli.py`
- `src/attodry_control/magnetic_field_monitor.py`
- `src/attodry_control/field_audit.py`
- `src/attodry_control/attodry.py`
- `src/attodry_control/config.py`
- `src/attodry_control/safety.py`
- `config/hardware.example.toml`
- `pyproject.toml`（仅 console entry points）
- `tests/test_attodry.py`
- `tests/test_magnetic_field.py`
- `tests/test_magnetic_field_monitor.py`

## 新 Chat 启动提示

```text
请负责 Magnetic-field 模块。先按 AGENTS.md 顺序完整阅读五份必读文档，再阅读
docs/modules/README.md 和 docs/modules/MAGNETIC_FIELD.md。检查 git status 和当前
提交，从最早未完成阶段开始。M0--M2 已完成，包括历史 commit e0924f1 在
LK_setup/lyr 的 DLL-free target-offline 验证；后续 revision 必须重新取得自己的 target-
offline evidence。M3--M5 分别需要新授权。必须保持纯 X<=3 T、纯 Z<=9 T（均为绝对值），
双轴非零时 sqrt(Bx^2+Bz^2)<=3 T；严格零才算单轴。通信失败
不得推断零场，也不得把离散 setpoint 计划描述为 continuous physical path、constant
angle 或 constant magnitude。默认不加载真实 DLL、不连接或写真实 attoDRY。
`transition_policy` 必须明确为 `direct` 或 `via_zero`；验证 float32 endpoints 和两种
mixed corners 后再选择 axis order，并保存 complete execution waypoint/path。JSONL 必须
含每次 toggle/component/zero command 的 attempt/result evidence，以及每条 component
command 的 exact float32 payload；vendor zero/action/error diagnostics 不得当作 zero
proof。结束时按模块交付格式报告。
```
