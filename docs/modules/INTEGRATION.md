# Integration module work package

## 当前状态

项目已经具备模拟 acquisition、SQLite 审计/恢复、cleanup、accepted-only 分析、
双 SR830 真实独立验收和 attoDRY 真实只读连接。真实 attoDRY 设置写入、vendor
SMU 和端到端硬件 acquisition 尚未验收。

Integration 只能组合 Lock-in、Temperature 和 Magnetic-field 模块已经通过的
接口与提交，不能代替它们各自的实验室 commissioning。

## 前置输入

开始合并前，每个模块必须提供：

- 明确的完成阶段；
- Git 提交号和干净工作树；
- 测试命令及结果；
- 真实硬件连接/写入清单；
- 未确认参数和已知限制；
- ignored 原始记录位置（如有）。

缺少任何一项时，Integration 只允许做离线审查，不能扩大硬件范围。

## 模块目标

1. 使用稳定的窄接口组合 SR830、温度、磁场、gate、存储和 cleanup。
2. 先完成全 simulation，再逐层申请只读和写入授权。
3. 确保每个 condition 只接受一个安全 attempt，失败原始数据仍保留。
4. 发生异常时按已确认顺序处理电气输出、gate、磁场和最终状态记录。
5. 确保分析默认只加载 accepted/clean 数据，并完整保留相位和状态元数据。

## 不能在 Integration 中越过的边界

- TOML 或先前授权不能自动授权新的真实写命令。
- attoDRY temperature/magnetic 的 read-only 验收不能证明设置写入可用。
- SR830 独立验收不能证明与扫温、扫场和 gate 同时运行已经安全。
- vendor SMU 模型、地址、compliance、leakage、ramp 和 readback 参数未确认前，
  不能构造真实端到端硬件路径。
- 通信失败不能把 field、gate 或 excitation 标记为安全零值。

## 集成顺序

### I0 - merge and contract verification（planned）

- 在独立 Integration branch 合并 L/T/M 的已验收提交。
- 解决共享文件冲突后逐项核对接口，不做无关格式化或大规模重构。
- 完成条件：工作树只包含可追溯修改，相关模块测试和完整测试通过。

### I1 - full simulation

- 覆盖正常运行、retry/resume、Lock-in unlock/overload、温度 timeout、磁场失败、
  gate leakage、Ctrl+C 和 cleanup 顺序。
- 检查 station snapshot、raw samples、accepted promotion 和 checkpoint。
- 完成条件：所有注入故障都有确定的 rejected/audit 结果，无静默继续。

### I2 - target offline release

- 在 `LK_setup` 使用 `lyr` 运行完整测试、构建 wheel、检查 wheel 内容和哈希。
- 不打开 VISA、不加载真实 DLL、不连接仪器。
- 完成条件：记录解释器、依赖、测试、wheel 文件和 SHA-256。

### I3 - integrated read-only snapshot

- 需要明确授权同时连接哪些仪器、是否读取会清锁存的状态，以及采样时长。
- 不启用温控/场控，不写 SR830 设置，不写 gate。
- 完成条件：每台设备身份和状态明确、时间戳完整、断开正常、零写命令。

### I4 - staged write integration

- 只有对应单模块已经 write commissioned 才能加入。
- 一次只增加一个已验收写动作，例如先 Lock-in 固定配置，再温度小目标，再磁场
  小目标；每次需要新的限定授权和失败恢复计划。
- 完成条件：每次扩展都有独立原始记录、cleanup 验证和回归测试。

### I5 - real end-to-end acquisition

- 必须先完成确切 vendor SMU 适配器和安全参数验收。
- 从最小激励、零 gate、已确认温度/磁场条件开始，不直接启动完整研究扫描。
- 完成条件：正常、人工中断和注入失败路径均经过实验室验收，accepted-only
  数据可由 notebook 重现。

## 集成时重点测试

- `sqrt(Bx^2 + Bz^2) <= 3 T` 对目标和全部中间路径成立。
- Lock-in X/Y/R/phase/harmonic/frequency 及设置上下文完整保存。
- 两台 SR830 顺序读取事实未丢失，转换样本不进入 accepted 曲线。
- 温度/磁场稳定均要求控制状态、error、窗口、容差和 timeout。
- 清理顺序为：降低 XX 激励、gate 归零/关闭、请求磁场归零、监视读回、记录
  最终状态、最后断开。
- 任一通信失败都保留最后确认状态并要求人工检查。
- rejected 数据可审计但不被默认分析加载。

## 预计文件所有权

- `src/attodry_control/interfaces.py`
- `src/attodry_control/acquisition.py`
- `src/attodry_control/cleanup.py`
- `src/attodry_control/records.py`
- `src/attodry_control/storage.py`
- `src/attodry_control/simulation.py`
- `src/attodry_control/simulate.py`
- 相应 acquisition/storage/simulation/cleanup 测试。

Integration 可以对共享适配层做最小修改，但不应把已分离的设备安全逻辑复制到
`acquisition.py`。

## 新 Chat 启动提示

```text
请负责 Integration 模块。先按 AGENTS.md 顺序完整阅读四份必读文档，再阅读
docs/modules/README.md、LOCKIN.md、TEMPERATURE.md、MAGNETIC_FIELD.md 和
INTEGRATION.md。先收集三个模块的提交号、完成阶段、测试和硬件动作报告；缺失时
只做离线审查，不猜测完成状态。从 I0 开始，先完整 simulation，再做 target lyr
离线验证。任何真实连接、清锁存或写命令必须等我另行明确授权。结束时按模块交付
格式报告合并提交、测试、冲突和剩余边界。
```

