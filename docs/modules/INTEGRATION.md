# Integration module work package

## 当前状态

项目已经具备模拟 acquisition、SQLite 审计/恢复、cleanup、accepted-only 分析、
双 SR830 真实独立验收和 attoDRY 真实只读连接。真实 attoDRY 设置写入、vendor
SMU 和端到端硬件 acquisition 尚未验收。

Integration 只能组合 Lock-in、Temperature、Magnetic-field 和 Three-SMU 模块
已经通过的接口与提交，不能代替它们各自的实验室 commissioning。

### 温度—激励组合切片（offline，真实联合运行未验收）

本 Integration branch 新增的温度—激励编排只组合两个已经存在的窄接口：外层为
升序 temperature condition，内层为完整双 SR830 excitation sweep。一个温度点只有在
稳定判据通过、整条内层扫描完成、SR830 cleanup 已验证且温度后检查通过后，才算
`completed` 并可前往下一点。

该实现还没有执行任何真实联合 run。它不把单独的 temperature write acceptance 或
Lock-in device-only commissioning 解释为组合硬件验收；未来的 DLL/VISA 连接、温度
写入、SR830 写入和状态锁存消费仍需要一次单独、范围明确的真实硬件授权。

组合记录刻意保存两种温度：开始 excitation 前的稳定窗口样品温度均值用于说明测量
准备度；每个 formal paired Lock-in sample 前后都顺序读取 attoDRY 状态，利用时间戳
做带时间权重的实际测量窗口温度平均。后者才是 formal sample 的温度坐标，前者不能
悄悄替代它。

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
6. 对温度—激励扫描，保持“稳定窗口均值”和“正式测量窗口的实际带时间权重温度”
   两个不同的审计字段，并保留每个正式样本的时间上下文。

## 不能在 Integration 中越过的边界

- TOML 或先前授权不能自动授权新的真实写命令。
- attoDRY temperature/magnetic 的 read-only 验收不能证明设置写入可用。
- SR830 独立验收不能证明与扫温、扫场和 gate 同时运行已经安全。
- vendor SMU 模型、地址、compliance、leakage、ramp 和 readback 参数未确认前，
  不能构造真实端到端硬件路径。
- 通信失败不能把 field、gate 或 excitation 标记为安全零值。

## 集成顺序

### I0 - merge and contract verification（planned）

- 在独立 Integration branch 合并 L/T/M/SMU 的已验收提交。
- 解决共享文件冲突后逐项核对接口，不做无关格式化或大规模重构。
- 完成条件：工作树只包含可追溯修改，相关模块测试和完整测试通过。

### I1 - full simulation

- 覆盖正常运行、retry/resume、Lock-in unlock/overload、温度 timeout、磁场失败、
  gate leakage、Ctrl+C 和 cleanup 顺序。
- 检查 station snapshot、raw samples、accepted promotion 和 checkpoint。
- 完成条件：所有注入故障都有确定的 rejected/audit 结果，无静默继续。

### I1a - temperature × excitation orchestration（offline implementation）

- 以 temperature scan 的升序网格作为外层；每个稳定温度点内运行完整的
  `sweep-excitation` 核心路径，而不是只读取一个锁相样本。
- 为每个正式成对样本同步记录 pre/post attoDRY state，并计算测量窗口的带时间权重
  样品温度；稳定窗口均值仍独立保留。
- 进度 JSONL 立即保存温度状态、Lock-in formal/transition/cleanup 样本和部分失败数据；
  summary/默认 formal CSV 仅把完整 temperature condition 作为可分析结果。
- `--resume-progress` 只跳过连续完成的 temperature condition；从未完整结束的温度点
  重新开始，不从幅值或 harmonic 中间续跑。
- 完成条件：配置、fake DLL/fake VISA、故障 cleanup、温度均值、JSONL/summary/CSV 和
  condition-boundary resume 测试通过；随后才可申请 I2 target-offline 检查。此阶段不
  包含真实硬件连接或写入。

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
- 温度—激励的 formal 数据应带同步的实际测量窗口温度；稳定窗口均值只作为 readiness
  证据，不能被当作扫描期间每个样本的实际温度。
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
- `src/attodry_control/temperature_excitation_scan.py`
- `src/attodry_control/lockin_test.py`（仅复用已验收的 excitation executor）
- `src/attodry_control/config.py` 与相应温度—激励配置/测试
- 相应 acquisition/storage/simulation/cleanup 测试。

Integration 可以对共享适配层做最小修改，但不应把已分离的设备安全逻辑复制到
`acquisition.py`。

## 新 Chat 启动提示

```text
请负责 Integration 模块。先按 AGENTS.md 顺序完整阅读四份必读文档，再阅读
docs/modules/README.md、LOCKIN.md、TEMPERATURE.md、MAGNETIC_FIELD.md、
THREE_SMU.md 和 INTEGRATION.md。先收集四个设备模块的提交号、完成阶段、测试和
硬件动作报告；缺失时只做离线审查，不猜测完成状态。从 I0 开始，先完整
simulation，再做 target lyr 离线验证。任何真实连接、清锁存或写命令必须等我
另行明确授权。结束时按模块交付格式报告合并提交、测试、冲突和剩余边界。
```

