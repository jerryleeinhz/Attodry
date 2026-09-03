# attoDRY Transport Control

用于 attoDRY2100XL、两台 SR830 和双栅 SMU 的低温输运测量项目。

当前仓库已完成阶段 1–2、阶段 3–7 可在无硬件条件下完成的离线实现、Three-SMU QCoDeS S0 离线模块、双 SR830 集成 1/2/3 次谐波器件验收，以及 Temperature module 的操作者验收。新增的 standalone magnetic-field 模块只完成了本机 fake-DLL 的 M0–M2 所需实现与验证；M2 的 `LK_setup`/`lyr` target-offline 运行尚未执行，M3–M5 真实读写均未授权。温控验收确认先开启控制再写 setpoint 可以产生升温，并要求测量保存实际 `sample_temperature_k`；commissioned `max_overshoot_k` 为 0.2 K。项目包括严格配置、完整仿真、平台记录、安全扫描与清理、SQLite/WAL 审计与恢复、双 SR830 驱动、fake-DLL attoDRY 驱动、Three-SMU CLI/Notebook 共用 generator、accepted-only 分析和实验室 commissioning 清单。日常温控和独立 Three-SMU 命令均读取统一的 `hardware.local.toml`；真实 magnetic-field、SMU 连接/写入、独立模块的主 acquisition 集成和端到端硬件路径仍需分阶段显式授权。

## 已确认硬件

- attoDRY2100XL，USB 虚拟串口加 `attoDRYxyz64bit.dll` 接口。
- 两轴矢量磁体：控制软件轴名为 X/Z；出厂规格表将横向轴写为 Y。
- 硬件额定值：X 轴 3 T，Z 轴 9 T；本项目所有实验命令额外限制合成场不超过 3 T。
- SR830 #1：内部参考、SINE OUT 交流激励、测量 Vxx。
- SR830 #2：从 #1 TTL OUT 获取外参考、测量 Vxy、SINE OUT 物理断开。
- 三台 Keithley 2400 的独立模块使用 `smu_bias`、`gate_top`、
  `gate_bottom` 语义角色；每台可独立选择 voltage/current source，并始终检查各自的
  V/I 绝对边界；voltage-source gate 的 leakage 与 compliance 保护已离线实现。
- 无旋转台；场方向由 Bx/Bz 计算。

## 安全不变量

```text
|Bx| <= 3 T
|Bz| <= 9 T（硬件额定值）
sqrt(Bx^2 + Bz^2) <= 3 T（项目实验上限）
```

可捕获的测量异常和 `Ctrl+C` 默认策略是：先安全关闭锁相激励与 SMU 输出，再请求 X/Z 磁场归零并监视读回。电脑硬崩溃或通信断开时软件不能保证归零，必须人工检查 attoDRY/APS100 状态。

## 首次在 Codex 中继续

1. 在 Codex 中打开本目录。
2. 新建任务并输入：

   ```text
   请先阅读 AGENTS.md 和 docs/PROJECT_HANDOFF.md，检查 git diff 与测试，然后继续当前阶段的下一项。
   ```

3. 在 PowerShell 中创建本地环境：

   ```powershell
   py -3.11 -m venv .venv
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -e .
   python -m unittest discover -s tests -v
   ```

如果电脑没有 `py` 命令，先安装 64 位 Python 3.11，并在安装器中勾选 Python Launcher。Conda 不是必需条件。更完整的 Codex、虚拟环境和离线 wheelhouse 工作流见 `docs/CODEX_AND_OFFLINE_SETUP.md`。

## 分模块继续开发

后续工作已经整理为可交给独立 Chat 的工作包，总入口是
[`docs/modules/README.md`](docs/modules/README.md)：

- [`Lock-in`](docs/modules/LOCKIN.md)：双 SR830 配置、相位、量程、自动量程和
  已完成实验的经验规则；
- [`Temperature`](docs/modules/TEMPERATURE.md)：attoDRY 温度读回、控制和稳定；
- [`Magnetic field`](docs/modules/MAGNETIC_FIELD.md)：本地 fake-DLL 的 X/Z
  单目标/有序点列、3 T 限制、canonical JSONL、文件 monitor 和 monitored cleanup；
  target-offline 与真实 M3–M5 仍待分别验收；
- [`Three-SMU`](docs/modules/THREE_SMU.md)：三台 Keithley、双栅极与 bias 的
  QCoDeS CLI/Notebook 双路线；日常配置、离线检查、运行和分析步骤见
  [`THREE_SMU_DAILY_OPERATION.md`](docs/THREE_SMU_DAILY_OPERATION.md)；
- [`Integration`](docs/modules/INTEGRATION.md)：各设备模块分别验收后的组合流程。

每个工作包都包含当前真实验收边界、目标、非目标、分阶段验收条件、预计文件
所有权和可复制的新 Chat 启动提示。多个 Chat 并行修改时应使用独立 branch 和
worktree；同一 checkout 不要并行编辑。

## 双 SR830 独立器件测试

在接入 attoDRY、磁体或栅极 SMU 之前，先按
[`docs/DUAL_SR830_DEVICE_TEST.md`](docs/DUAL_SR830_DEVICE_TEST.md) 完成两台
SR830 的独立测试。诊断模式只查询；独立 commissioning 设置写入需要显式授权并
确认 `lockin_xy` 的 SINE OUT 已物理断开。日常双 SR830 sweep 则仅按已解析的
`hardware.local.toml` 允许范围写入，并保留完整审计记录。

```powershell
python -m pip install -e ".[hardware]"
python -m attodry_control.lockin_test discover
Copy-Item config\hardware.example.toml config\hardware.local.toml
python -m attodry_control.lockin_test diagnose --config config\hardware.local.toml
python -m attodry_control.lockin_test monitor-live --help
python -m attodry_control.lockin_test recover-interface --help
python -m attodry_control.lockin_test measure-harmonics --help
python -m attodry_control.lockin_test sweep-frequency --help
python -m attodry_control.lockin_test sweep-excitation --help
python -m attodry_control.attodry_test --help
python -m attodry_control.temperature_test --help
python -m attodry_control.temperature_run --help
python -m attodry_control.temperature_scan --help
python -m attodry_control.magnetic_field_cli --help
python -m attodry_control.magnetic_field_cli single-target --help
python -m attodry_control.magnetic_field_cli scan --help
python -m attodry_control.magnetic_field_monitor --help
python -m attodry_control.lockin_test --help
```

日常温控参数统一写在已忽略的 `config/hardware.local.toml` 的
`[temperature_run]` 表中，通常只修改 `target_k`。运行
`attodry-temperature-run` 不需要额外授权参数；该命令会先开启温控、再设置目标，
监测30分钟并记录实际样品温度。中断策略也在同一表中配置：
`interrupt_policy = "abort"`（默认）、`"continue"` 或
`"wait-confirmation"`，并用 `resume_recheck_s = 30.0` 指定继续前的重新确认窗口。
详细步骤见
[`docs/TEMPERATURE_RUN_GUIDE.md`](docs/TEMPERATURE_RUN_GUIDE.md)。原
`temperature_commissioning.local.toml` 和 `attodry-temperature-test` 仅保留给
严格稳定性诊断。

逐点温度稳定性计时使用同一个 `hardware.local.toml` 中的
`[temperature_scan]` 网格，并复用 `[temperature_stability]` 和
`[temperature_run]` 的安全/中断参数。新命令当前只完成离线验证，真实多点写入仍需
单独确认并带 `--authorize-temperature-scan`；操作、实时 JSONL 和断点恢复说明见
[`docs/TEMPERATURE_SCAN_GUIDE.md`](docs/TEMPERATURE_SCAN_GUIDE.md)。

## Standalone X/Z 磁场模块

`[magnetic_field_run].points` 是一个非空的显式点列，按 TOML 中的原顺序执行并保留
重复项；它不会排序、去重或生成 Cartesian grid。每个 target、生成的 zero-detour
setpoint waypoint，以及 X 分量先写、Z 分量后写时的 mixed setpoint 都要通过
`sqrt(Bx^2 + Bz^2) <= 3 T` 和分量边界检查，包括 DLL 接收的 float32 command
values。`max_step_t` 必须大于 1e-5 T acknowledgement resolution，且只约束离散请求
setpoint 的间距；starting setpoint、内部 waypoint、显式 target 和 cleanup zero 都要求
setpoint/actual field 的稳定读回。当前离线实现仍没有测量 vendor controller 在相邻稳定
waypoint 之间的连续运动，因此不声明 physical path、constant angle、constant magnitude
或实际 ramp rate。

下面的 help 和 JSONL monitor 都不会连接硬件：

```powershell
python -m attodry_control.magnetic_field_cli single-target --help
python -m attodry_control.magnetic_field_cli scan --help
python -m attodry_control.magnetic_field_monitor --progress PATH_TO_PROGRESS.jsonl
```

未来 M3 当前 revision 的真实只读验收使用下面的既有 write-disabled 命令；它仍未执行，
命令不包含任何 write authorization，只有获得新的 connection authorization 后才能运行：

```powershell
python -m attodry_control.attodry_test `
  --config config/hardware.local.toml `
  --samples 10 `
  --interval-s 1 `
  --authorize-connection
```

M3–M5 运行前必须让 vendor GUI 和其它 attoDRY controller process 断开。

`single-target` 真实执行要求 `points` 恰有一个 entry，并同时提供
`--authorize-connection` 与 `--authorize-field-writes`；正常结束始终 monitored zero。
`scan` 还要求 `--authorize-ordered-field-scan`，授权范围包含列出的每一个点和重复点；
正常结束按 `[cleanup].normal_end_field_policy` hold 或 zero；hold 仍会复核 final
setpoint、actual field/tolerance、control 和 error；exact-zero target 还检查 magnitude。
异常与 `Ctrl+C` 都进入 best-effort monitored-zero cleanup；normal zero 失败或被中断时，
cleanup 会独立重试 monitored zero。通信不确定、非有限读回、zero/close/audit 未确认或
没有完整最终状态时不会声称零场，并要求人工查看 attoDRY/APS100。当前 cleanup 不关闭
field control：
zero 时保持 control enabled at zero，hold 时保持 control enabled at final target，随后
断开 DLL session。OFF→ON takeover 还要求 actual field 与 latent setpoint 在 configured
（且不大于 1 mT）tolerance 内相符，并验证两种可能的 mixed corners 均不超过 3 T。

每次运行只有一个 canonical JSONL；每个事件 append 后都会 flush/`fsync`，partial、
rejected、interrupted、stability、cleanup 和 terminal evidence 不删除。field monitor
只读取这个文件，不导入 DLL/driver，也不查询仪器；它会把 torn、integrity failure 或
缺少 terminal event 的 stream 标记为 incomplete/manual-verification，也会拒绝要求 zero
却未验证 zero 或缺少 final confirmed state 的矛盾 completed record，而不会推断控制进程
仍在运行。当前 JSONL 还没有 M4 所需的 exact float32 toggle/component
command-attempt/result 列表。所有新功能只在本机 fake DLL 验证；M2 target-offline 和
M3–M5 真实阶段仍待完成。完整边界见
[`docs/modules/MAGNETIC_FIELD.md`](docs/modules/MAGNETIC_FIELD.md)。

实际 sweep 网格、安全限制、时序与每次运行的备注统一保存在 ignored 的
`config\hardware.local.toml` 的 `[lockin_sweep]` 中。XX 与 XY 的量程模式则分别
保存在 `[lockin_xx]` 与 `[lockin_xy]`：当前 `hardware.example.toml` 示例固定为
XX 1 V、XY 10 mV；只有把某一角色显式改为 `bounded_auto` 时才启用该角色的自动量程。配置完成后，频率扫描、幅值
扫描或频率×幅值二维扫描分别直接运行：

扫频期间的固定 XX SINE OUT 幅值填写在
`[lockin_sweep].frequency_source_voltage_v_rms`（0.004--5.0 Vrms）；扫频结束后仍
按安全协议恢复 4 mVrms。扫幅的逐点幅值由 `excitation_ranges` 控制：线性区间使用
`min`/`max` 加 `step` 或 `points`（二选一），对数区间使用 `min`/`max`/`points`；
旧 `excitation_points_v_rms` 数组仅保留兼容。

两条 sweep 会自动读取与 `hardware.local.toml` 同目录的受版本控制
`lockin_safety.toml`；日常运行不要求先执行验证命令。需要离线检查时可运行
`python -m attodry_control.lockin_test validate-config`，它不会打开 VISA。安全文件
控制日常量程白名单和 bounded-auto 阶梯，完整 SR830 硬件量程映射（包括 1 V）不代表
该档位已经获准用于日常扫描。

```powershell
python -m attodry_control.lockin_test sweep-frequency
python -m attodry_control.lockin_test sweep-excitation
python -m attodry_control.lockin_test sweep-frequency-excitation
```

两条命令默认读取 `config\hardware.local.toml`；仅当配置文件位于其他位置时才
附加 `--config <path>`。日常参数、严格预检、自动 JSON 归档和故障后的人工确认流程见
[`docs/LOCKIN_DAILY_OPERATION.md`](docs/LOCKIN_DAILY_OPERATION.md)。
每次运行前，在同一 `[lockin_sweep]` 表填写 `run_name` 和 `note`；名称进入 JSON
文件名，备注保留在审计记录。

同一张表中的 `frequency_xx_harmonics`、`frequency_xy_harmonics`、
`excitation_xx_harmonics`、`excitation_xy_harmonics` 分别决定每类扫描正式保留的
XX/XY 谐波。每项可填 `[1, 3]`、`[2]` 或 `[]`；每类扫描至少选择一个角色。未正式选择的
伴随通道仍会读取并参与安全判决，完成或失败清理仍会恢复两台 SR830 到 h1。
二维扫描可用 `combined_xx_harmonics` 与 `combined_xy_harmonics` 单独选择；省略时
继承 `excitation_*`。它按频率外层、幅值内层遍历两个配置区间，并将实际频率和 SINE
OUT 读回值保存到同一个 JSON。

在没有任何 sweep 或其他程序占用同一对 VISA 地址时，可用下面的只读面板实时查看
XX/XY 的电压、相位、频率、量程和锁定状态：

```powershell
python -m attodry_control.lockin_test monitor-live --consume-status-latches
```

该选项会读取并清除 `LIAS?`/`ERRS?` 锁存位；完整边界和停止方式见
[`docs/LOCKIN_LIVE_MONITOR.md`](docs/LOCKIN_LIVE_MONITOR.md)。

已完成的独立扫频和激励JSON可用
[`notebooks/sr830_commissioning_sweeps.ipynb`](notebooks/sr830_commissioning_sweeps.ipynb)
浏览和绘图。Notebook默认只选择 `completed` 记录和 `clean` 正式样本；可通过
远程目录列表分别选择扫频、幅值扫描或两者，也可显式切换到 rejected/diagnostic 或
unlock/overload/error 审计筛选。只选择一种扫描时，Notebook只画相应的六张图；点级
排除控件不会修改原始 JSON，并在可选导出中记录筛选清单。转换期和 cleanup 数据不会
进入默认曲线。

只查看频率扫描和幅值扫描中的 XY 信号时，使用
[`notebooks/sr830_xy_sweeps.ipynb`](notebooks/sr830_xy_sweeps.ipynb)。
该 Notebook 在读取阶段丢弃 XX，保留 completed/clean 和 Browse 筛选，
分别绘制 XY 的频率扫描与幅值扫描，并在图例和标题中标记谐波阶数。

## 目录

```text
config/                 仿真配置和真实硬件配置模板
docs/                   交接、安全、阶段和安装指南
src/attodry_control/    平台无关核心与后续硬件适配器
tests/                  安全边界和稳定判据测试
```

## 当前可运行检查

```powershell
python -m unittest discover -s tests -v
python -m attodry_control
python -m attodry_control.monitor --database PATH --run-id RUN_ID
python -m attodry_control.simulate --database run_data/demo.sqlite --run-id demo --inject-first-unlock
python -m attodry_control.simulate --database run_data/demo.sqlite --run-id demo --resume
python -m attodry_control.analysis --database PATH --run-id RUN_ID --csv analysis_output/run.csv
python -m attodry_control.analysis --database PATH --run-id RUN_ID --publication-dir analysis_output/RUN_ID --format png --format pdf
python -m attodry_control.magnetic_field_monitor --progress PATH_TO_PROGRESS.jsonl
python -m attodry_control.three_smu_cli describe
python -m attodry_control.three_smu_cli monitor-live --help
python -m attodry_control.three_smu_cli run --help
```

这些命令中的状态、仿真、监视和分析路径都不会连接真实仪器。绘图需要安装
`.[analysis]` 可选依赖。分析输入、显式电流/栅极校准和输出清单见
[`docs/DATA_ANALYSIS.md`](docs/DATA_ANALYSIS.md)；实验室步骤见
[`docs/LAB_COMMISSIONING.md`](docs/LAB_COMMISSIONING.md)。
