# Three-SMU / dual-gate module work package

## 当前状态

状态：`planned`。本文件只定义下一 Chat 的目标、接口、安全边界、使用方式和
验收条件，不代表生产代码、目标电脑安装或真实 SMU 验收已经完成。

该模块使用三台 SMU：

- `smu_bias`：source-drain bias；第一版可选择电压源或电流源；
- `gate_top`：top gate，电压源、电流测量；
- `gate_bottom`：bottom gate，电压源、电流测量。

第一版不读取或记录 Lock-in。以后把 `smu_bias` 替换为 Lock-in 激励时，应替换
bias provider，而不是重写 gate 扫描、记录和分析模块。

## 已确认的实现选择

1. 硬件依赖使用 QCoDeS，Keithley 2400 调用方式参考已成功运行的本地程序：
   `Electrical measurement/keithley2400_qcodes_ui.py`。
2. 不移植 GUI、PySide 或 pyqtgraph 控制界面。
3. 只保留一个底层 Python 控制与扫描引擎，提供两条上层路线：
   - 无 GUI 的 CLI，适合可重复、可审计的正式运行；
   - Jupyter Notebook 调用同一底层模块，适合逐点实时画图。
4. 三台仪器的地址、量程和全部安全参数只放在 ignored local TOML 中，由用户
   填写；checked-in 示例只能使用 `CHANGE_ME`，不能含看似可直接使用的限值。
5. 保留参考程序中的 compliance、NPLC、source/measure autorange、四线制、
   output 状态和 compliance-trip 检查，但采用本项目的 fail-closed 规则。
6. 不复刻参考 GUI 的 G6--G9 界面验证阶段。生产代码和 fake-instrument 测试
   通过后，由用户选择第一次正式扫描并单独授权；真实运行自身仍必须执行完整
   preflight、限值检查、逐步 ramp、读回和 cleanup。

## 模块目标

1. 通过语义角色控制三台 SMU，不使用 `SMU1/2/3` 之类位置编号。
2. 在打开 QCoDeS driver 前完成 strict TOML、扫描计划和安全范围验证。
3. 同时支持固定值、关闭和扫描三种通道角色，并保留以下扫描模式：
   - time trace；
   - bias I-V；
   - top-gate transfer；
   - bottom-gate transfer；
   - paired top/bottom-gate sweep；
   - 一至三通道 multi-SMU map；
   - software pulse。
4. 扫描计划支持单向/双向、二维 serpentine、每点等待和每点多次采样。
5. 每一点顺序读取三台 SMU并保存各自时间戳；不得把顺序读回描述为同步采样。
6. 保存 source setpoint、V/I/R 读回、output、compliance/trip、gate leakage、
   状态、错误、扫描坐标和 cleanup 结果。
7. 默认分析只使用 `completed/accepted/clean` 正式样本；rejected、partial、
   interrupted 和 cleanup 数据保留但只能显式选择。
8. CLI 和 Notebook 的输出数据、状态判据和 cleanup 完全一致。

## 非目标

- 第一版不连接 SR830，不记录 X/Y/R/phase，也不计算 Lock-in resistance。
- 不控制 attoDRY 温度或磁场，不修改现有 acquisition 主路径。
- 不提供 GUI，不在 Notebook 中直接实例化 QCoDeS driver。
- 不猜测 VISA 地址、compliance、leakage、source limit、ramp step、NPLC、
  settle time、readback tolerance 或四线制设置。
- 不把参考程序中直接跳变 setpoint、吞掉 I/O 异常或 compliance 后继续运行的
  行为带入新模块。

## 配置设计

计划增加两个文件：

- `config/three_smu_hardware.example.toml`：三台 SMU 的本地硬件模板；
- `config/three_smu_scan.example.toml`：可复制修改的扫描计划模板。

本地实际文件建议为：

```text
config/three_smu_hardware.local.toml
config/three_smu_scan.local.toml
```

两者必须加入 `.gitignore`。硬件 TOML 每个语义角色至少包含：

```toml
[smu_bias] # gate_top / gate_bottom 结构相同
model = "Keithley2400"
address = "CHANGE_ME"
timeout_ms = "CHANGE_ME"
compliance_current_a = "CHANGE_ME"
compliance_voltage_v = "CHANGE_ME"
source_min = "CHANGE_ME"
source_max = "CHANGE_ME"
ramp_step = "CHANGE_ME"
readback_tolerance = "CHANGE_ME"
settle_s = "CHANGE_ME"
nplc = "CHANGE_ME"
source_auto_range = true
measure_auto_range = true
four_wire = false
```

两个 gate 还必须设置 `leakage_limit_a`。Gate 第一版只允许 voltage-source；
`smu_bias` 允许 voltage-source 或 current-source。所有 `CHANGE_ME`、重复地址、
非法范围、leakage 高于 current compliance 等情况必须在构造 driver 前失败。

扫描 TOML 使用 `[scan]` 加三张角色表。每个角色选择 `off/fixed/sweep`，扫描表
设置 mode、samples per point、delay、bidirectional、serpentine 和结束动作。
结束动作默认且推荐 `zero_disable`；若保留 `hold`，CLI 必须要求额外的显式
`--authorize-hold`，不能仅凭 TOML 留在带输出状态。

## QCoDeS 适配器边界

计划增加一个窄的 Keithley 2400 adapter，参考程序中已证实的调用包括：

```text
mode("VOLT"/"CURR")
compliancei(...) / compliancev(...)
nplci(...) / nplcv(...)
volt(...) / curr(...)
output("on"/"off")
:SOUR:VOLT:RANG:AUTO ON / :SOUR:CURR:RANG:AUTO ON
:SENS:CURR:RANG:AUTO ON / :SENS:VOLT:RANG:AUTO ON
:SYST:RSEN ON/OFF
:READ?
SENS:CURR:PROT:TRIP? / SENS:VOLT:PROT:TRIP?
```

QCoDeS import candidates需兼容参考程序使用过的模块路径。任何命令错误都必须
向上报告，不能用空 `except` 继续。QCoDeS 是硬件驱动依赖；第一版数据审计以
JSONL/CSV/metadata 为必需输出，QCoDeS dataset 可作为镜像，但不能替代原始
rejected/cleanup 记录。

## 安全状态机

真实运行必须满足以下顺序：

1. CLI/Notebook 先加载并验证硬件 TOML 和扫描 TOML。
2. 没有 `authorize_writes=True` 或 CLI `--authorize-writes` 时，不能导入 driver、
   连接仪器或发送任何写命令。
3. 连接后只读三台 `*IDN?`、source mode/setpoint 和 output；地址与物理身份必须
   各不相同。
4. 任一仪器连接时 output 已开启，停止并要求人工确认；不能静默接管未知输出。
5. output 关闭时，先在当前模式将残留 setpoint 归零，再配置 source mode、
   compliance、NPLC、range 和 four-wire。
6. 在 0 setpoint 设置 compliance 后才允许开启输出。
7. 所有目标使用配置的最大步长 ramp；每一步等待、读 V/I、验证 source readback、
   compliance trip/near-limit 和 gate leakage。
8. 任一读写、读回、compliance 或 leakage 失败立即停止正式扫描，保存 partial/
   rejected 原始记录并进入 best-effort cleanup。
9. cleanup 顺序：`smu_bias` 逐步归零并关闭，然后 `gate_top`、`gate_bottom` 逐步
   归零并关闭，最后读取并保存可确认的状态，再断开。
10. 通信失败不能记录成 0 或 output-off；保留最后确认状态，并明确提示人工查看
    三台 SMU 面板。电脑/内核硬崩溃无法依靠 Python cleanup。

## 计划的代码边界

```text
src/attodry_control/three_smu_config.py    strict hardware/scan TOML
src/attodry_control/keithley2400.py        QCoDeS Keithley 窄适配器
src/attodry_control/three_smu.py            会话、安全 ramp、扫描 generator、记录
src/attodry_control/three_smu_cli.py        describe/run CLI
src/attodry_control/three_smu_analysis.py   accepted-only loader 与绘图
notebooks/three_smu_live.ipynb              调用底层 generator 的实时图
notebooks/three_smu_analysis.ipynb          只读 Browse/筛选/分析
tests/test_three_smu_*.py                   fake driver、配置、扫描、cleanup、分析
```

为减少与其他模块 worktree 的冲突，第一版不要修改 `config.py`、`acquisition.py`、
`storage.py` 或现有 gate controller；先交付独立的三 SMU 模块，最终由 Integration
Chat 决定如何接入主 acquisition。

## 两条使用路线

### CLI

预期接口如下，实际命令以实现后的 `--help` 为准：

```powershell
python -m attodry_control.three_smu_cli describe `
  --hardware config/three_smu_hardware.local.toml `
  --plan config/three_smu_scan.local.toml

python -m attodry_control.three_smu_cli run `
  --hardware config/three_smu_hardware.local.toml `
  --plan config/three_smu_scan.local.toml `
  --output-dir run_data/three_smu `
  --authorize-writes
```

`describe` 必须完全离线；`run` 才能请求真实硬件授权。在 `LK_setup` 上所有命令
必须在 `lyr` 环境运行，或直接调用该环境的 Python。

### Notebook

Notebook 只使用公共会话 API，不能直接调用 QCoDeS：

```python
AUTHORIZE_WRITES = False

with ThreeSmuSession.open(
    hardware,
    authorize_writes=AUTHORIZE_WRITES,
) as session:
    for sample in session.run(plan):
        live_plot.update(sample)
```

Notebook 默认 `AUTHORIZE_WRITES = False`，运行单元格前由操作者手工改为 `True`。
离开 `with`、点击中断或正常结束时都必须走同一个 cleanup。实时图只是消费者，
不能拥有第二套 setpoint 或 safety 逻辑。

## 数据与分析要求

每次 run 使用独立目录，至少包含：

```text
metadata.json     配置摘要、代码版本、开始/结束状态、cleanup 结果
raw.jsonl         start/preflight/configure/ramp/sample/error/cleanup 全事件
data.csv          正式逐点三 SMU 平铺数据
```

默认 loader 只返回完整 run 中无错误、无 compliance、无 gate leakage 的正式样本。
Browse 功能选择 run 目录或数据文件；筛选项至少包含 completed/rejected、clean/
problem、scan segment 和 SMU role。分析需提供 bias I-V、gate transfer、time trace、
gate leakage、二维 map；不从三 SMU 数据推导 Lock-in 相位或电阻。

## 开发与验收阶段

### S0 - offline implementation

- 完成严格配置、点生成器、QCoDeS adapter、共享 session、CLI、两个 Notebook。
- fake instrument 覆盖正常、重复地址/身份、未知 active output、超限目标、
  compliance、leakage、readback mismatch、通信失败、Ctrl+C 和 cleanup 顺序。
- Notebook JSON/语法检查通过，且 Notebook 不直接导入 QCoDeS。
- 相关测试和完整离线测试通过；无真实连接。

### S1 - target offline

- 在 `LK_setup` 的 `lyr` 环境安装 QCoDeS 和项目依赖。
- 运行测试、CLI `describe`、Notebook import/compile；不连接 VISA。
- 记录 Python/QCoDeS/PyVISA 版本和测试结果。

### S2 - first real scan

- 用户填写三台地址、安全范围、compliance、leakage、ramp/NPLC 和第一份计划。
- 用户单独授权该计划的连接与写命令后直接运行，不新增 GUI G6--G9 阶段。
- 运行前仍执行状态机中的 read-only preflight；第一次从最小实际范围开始。
- 保存 ignored 原始目录，并报告写命令范围、cleanup 和人工面板确认。

## 下一 Chat 开始前仍需用户填写

代码阶段可以全部用 fake instrument 完成，不需要先回答以下值。第一次真实运行前
必须填写：

- 三台 Keithley 的 VISA 地址和从 `*IDN?` 读到的可区分身份；
- `smu_bias` 采用 voltage-source 还是 current-source；
- 三台 source min/max、最大 ramp step、settle 和 readback tolerance；
- current/voltage compliance、两个 gate leakage trip；
- NPLC、source/measure autorange、four-wire；
- 第一次正式扫描的 mode、范围、步长、delay、双向/serpentine 和结束动作。

## 新 Chat 启动提示

```text
请负责 Three-SMU / dual-gate 模块。工作区使用独立 branch `module/three-smu`
和现有 three-smu worktree，不要在主 checkout 修改。先按 AGENTS.md 顺序完整
阅读四份必读文档，再阅读 docs/modules/README.md 和 THREE_SMU.md。实现三台
Keithley 2400（smu_bias、gate_top、gate_bottom）的 QCoDeS 无 GUI 模块：一个
共享底层 Python session，同时提供 CLI 和调用同一 generator 的 Jupyter 实时图，
并提供 accepted-only Browse/分析 Notebook。保留 TOML 中所有 compliance、
leakage、range、ramp、NPLC、autorange、four-wire 和 finish 配置；第一版不连接
或记录 Lock-in。参考本地 Electrical measurement/keithley2400_qcodes_ui.py 中
已经成功的 QCoDeS 命令，但不要复制 GUI、直接跳变、吞异常或 compliance 后继续。
默认只做离线代码与 fake-instrument 测试，不连接真实 SMU；任何真实连接或写命令
等待我另行明确授权。完成后更新 DEVELOPMENT_STAGES 和 PROJECT_HANDOFF，提交到
module/three-smu，并按模块交付格式报告。
```
