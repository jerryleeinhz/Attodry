# attoDRY Transport Control

用于 attoDRY2100XL、两台 SR830 和双栅 SMU 的低温输运测量项目。

当前仓库已完成阶段 1–2、阶段 3–7 可在无硬件条件下完成的离线实现、双 SR830 集成 1/2/3 次谐波器件验收，以及 attoDRY 的 10 秒只读连接验收。包括严格配置、完整仿真、平台记录、安全扫描与清理、SQLite/WAL 审计与恢复、双 SR830 驱动、fake-DLL attoDRY 驱动、模型无关的栅极安全、端到端仿真执行、accepted-only 出版级分析和实验室 commissioning 清单。attoDRY 的任何设置写入、SMU 和端到端硬件路径仍需分阶段显式授权；具体 SMU 命令等待确认型号。

## 已确认硬件

- attoDRY2100XL，USB 虚拟串口加 `attoDRYxyz64bit.dll` 接口。
- 两轴矢量磁体：控制软件轴名为 X/Z；出厂规格表将横向轴写为 Y。
- 硬件额定值：X 轴 3 T，Z 轴 9 T；本项目所有实验命令额外限制合成场不超过 3 T。
- SR830 #1：内部参考、SINE OUT 交流激励、测量 Vxx。
- SR830 #2：从 #1 TTL OUT 获取外参考、测量 Vxy、SINE OUT 物理断开。
- 两台栅极 SMU、漏电流与 compliance 保护沿用原输运项目的目标。
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

## 双 SR830 独立器件测试

在接入 attoDRY、磁体或栅极 SMU 之前，先按
[`docs/DUAL_SR830_DEVICE_TEST.md`](docs/DUAL_SR830_DEVICE_TEST.md) 完成两台
SR830 的独立测试。工具的诊断模式只查询；任何设置写入都需要显式授权并
确认 `lockin_xy` 的 SINE OUT 已物理断开。

```powershell
python -m pip install -e ".[hardware]"
python -m attodry_control.lockin_test discover
Copy-Item config\hardware.example.toml config\hardware.local.toml
python -m attodry_control.lockin_test diagnose --config config\hardware.local.toml
python -m attodry_control.lockin_test measure-harmonics --help
python -m attodry_control.lockin_test sweep-frequency --help
python -m attodry_control.lockin_test sweep-excitation --help
python -m attodry_control.attodry_test --help
python -m attodry_control.lockin_test --help
```

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
python -m attodry_control.analysis --database PATH --run-id RUN_ID --csv analysis_output/run.csv
python -m attodry_control.analysis --database PATH --run-id RUN_ID --publication-dir analysis_output/RUN_ID --format png --format pdf
```

这些命令中的状态、仿真、监视和分析路径都不会连接真实仪器。绘图需要安装
`.[analysis]` 可选依赖。分析输入、显式电流/栅极校准和输出清单见
[`docs/DATA_ANALYSIS.md`](docs/DATA_ANALYSIS.md)；实验室步骤见
[`docs/LAB_COMMISSIONING.md`](docs/LAB_COMMISSIONING.md)。
