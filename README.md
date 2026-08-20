# attoDRY Transport Control

用于 attoDRY2100XL、两台 SR830 和双栅 SMU 的低温输运测量项目。

当前仓库是安全骨架（阶段 0），不会连接或控制真实仪器。已经实现并测试的是平台无关的数据模型、X/Z 矢量场安全边界和连续稳定窗口判据。真实 DLL、VISA 驱动、扫描执行、SQLite、监视和绘图将在后续阶段逐步迁移和验证。

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
```

第二条命令只显示骨架状态，不会连接仪器。
