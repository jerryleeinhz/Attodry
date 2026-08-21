# Module work packages

本目录把后续工作拆成五个可以由独立 Chat 跟进的工作包。这里定义的是
目标、阶段、文件边界和验收条件，不代表尚未授权的硬件工作已经完成。

## 依赖顺序

```text
Lock-in ───────┐
Temperature ───┼──> Integration
Magnetic field ┤
Three-SMU ─────┘
```

- [`LOCKIN.md`](LOCKIN.md)：双 SR830 配置契约、相位、量程和自动量程。
- [`TEMPERATURE.md`](TEMPERATURE.md)：attoDRY 温度读回、控制和稳定判据。
- [`MAGNETIC_FIELD.md`](MAGNETIC_FIELD.md)：X/Z 矢量磁场、安全路径和归零。
- [`THREE_SMU.md`](THREE_SMU.md)：三台 Keithley、双栅极、bias、CLI/Notebook。
- [`INTEGRATION.md`](INTEGRATION.md)：只在四个设备模块分别验收后进行组合。

四个设备模块可以独立审核和测试。Integration 不重新发明各模块的安全逻辑，
只组合已经通过测试并带有明确提交号的接口。

## 所有 Chat 的共同规则

1. 开始改代码前，按 `AGENTS.md` 的顺序完整阅读：
   `PROJECT_HANDOFF.md`、`HARDWARE_AND_SAFETY.md`、
   `DEVELOPMENT_STAGES.md`、`README.md`，然后阅读对应模块文件。
2. 默认只允许离线代码和 fake-instrument 测试。读取真实仪器、读取并清除
   状态锁存、写设置和运动/控温分别需要明确授权，不能互相替代。
3. 在 `LK_setup` 上运行任何项目命令时，必须使用 Conda 环境 `lyr`，或直接
   调用 `C:/Users/LK_Setup/anaconda3/envs/lyr/python.exe`。
4. 不提交 `hardware.local.toml`、实验原始数据、vendor DLL、仪器本地地址、
   密钥或个人路径。
5. 每个代码改动必须同时带相应测试；完成后更新
   `DEVELOPMENT_STAGES.md` 和 `PROJECT_HANDOFF.md`。
6. 保留 rejected/transition/cleanup 原始数据，但默认分析只能使用
   completed/accepted/clean 正式数据。
7. 通信失败不能证明输出关闭、温度稳定或磁场为零；必须保存最后确认读回并
   要求人工检查。

## 多 Chat 工作区规则

不要让多个 Chat 同时修改同一个 checkout。两种安全做法任选其一：

- 同一工作区一次只运行一个修改代码的 Chat；或
- 为每个模块建立独立 Git branch 和 worktree，再由 Integration Chat 合并。

建议分支名：

```text
module/lockin
module/temperature
module/magnetic-field
module/three-smu
module/integration
```

`config.py`、`attodry.py`、`acquisition.py`、阶段文档是共享文件。若两个模块
都需要修改它们，不能在同一工作树并行编辑；应由各模块先完成独立提交，再在
Integration 分支解决冲突并重新运行完整测试。

## 阶段状态词

- `planned`：仅形成目标和验收条件。
- `offline complete`：生产代码、fake-instrument 测试和文档通过，未接硬件。
- `target offline complete`：在目标电脑 `lyr` 环境通过离线测试，未开仪器。
- `read-only commissioned`：在单独授权下完成真实只读验收。
- `write commissioned`：在明确限定写命令和安全目标的授权下完成真实写入验收。

不能用较低级别的完成状态暗示较高级别已经完成。

## 每个模块的交付格式

每个模块 Chat 结束时应报告：

```text
模块：
完成到的阶段：
提交号：
修改文件：
测试命令与结果：
真实硬件是否连接：
实际发送的写命令：
保存的原始记录位置（不得提交）：
仍待用户确认的参数：
Integration 需要知道的接口或限制：
```

只有提交号、测试结果和硬件权限边界都明确，Integration 才能接收该模块。

